"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import logging
import secrets
import time
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from ..config import config
from ..session import session_id_for_window, session_manager
from ..terminal_parser import (
    AskUserQuestionForm,
    build_form_from_tool_input,
    extract_interactive_content,
    is_interactive_ui,
    parse_ask_user_question,
)
from ..tmux_manager import tmux_manager
from . import attention
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    TopicSendOutcome,
    topic_delete,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}


# ── PR 2b: structured option-pick callback tokens ────────────────────────
#
# When ``handle_interactive_ui`` lands a structured AskUserQuestion card, it
# mints one callback token per option button. The token resolves server-side
# (via ``_pick_tokens``) to the (window, fingerprint, option_number,
# option_label) bound at mint time. On click, the callback handler:
#
#   1. Looks up the token. Missing / expired → "Card expired, refresh".
#   2. Re-captures the pane and re-runs the parser. None → "Form gone".
#   3. Compares ``form.fingerprint()`` to the token's pinned value. Mismatch
#      → "Form changed, refreshing" + repaint the card. Do NOT dispatch
#      the key — that's the load-bearing staleness check Hermes flagged.
#   4. Sends the literal digit via tmux_manager.send_keys(literal=True,
#      enter=False). Marks the token used (single-use).
#
# Token lifetime is short (5 minutes) because the form is interactive and
# the user will either resolve or abandon it within minutes. No daily GC
# needed; ``_prune_expired_pick_tokens`` runs on every mint so the map
# stays bounded.

# Conservative TTL — Claude Code's AskUserQuestion picker stays open at most
# a few minutes in practice. 300s is comfortably longer than the slowest
# turnaround but short enough that a forgotten token can't pile up.
_PICK_TOKEN_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class _PickTokenEntry:
    """Server-side state bound to a single option-button click.

    Frozen because once minted, the entry must not mutate (the staleness
    check compares the *minted* fingerprint against the *current* parse).
    Marking entries used is done by popping from the map, not flipping a
    field, so single-use semantics are enforced by ``consume_pick_token``.
    """

    window_id: str
    user_id: int
    thread_id: int | None
    fingerprint: str  # form.fingerprint() at the moment the keyboard rendered
    option_number: int  # the numeric shortcut to send (1-9)
    option_label: str  # human label, used for log messages + sanity
    is_review_submit: bool  # True iff this click should submit the review screen
    expires_at: float  # monotonic clock deadline


_pick_tokens: dict[str, _PickTokenEntry] = {}

# Stable per-route cache so a re-render of the same form (same fingerprint)
# reuses the same callback tokens. Without this, every status-polling tick
# would mint fresh random tokens, the reply_markup would never match the
# previous edit, Telegram would never return MESSAGE_NOT_MODIFIED, and the
# bot would re-edit the card every poll cycle while the user is reading it.
# Hermes peer review flagged this as a no-ship before fix.
#
# Key: (user_id, thread_id_or_0, window_id, fingerprint)
# Value: list[token] — one token per option button, in the order the
#        keyboard builder emitted them.
_pick_token_cache: dict[tuple[int, int, str, str], list[str]] = {}


def _prune_expired_pick_tokens(now: float | None = None) -> None:
    """Drop expired tokens from the in-memory map.

    Runs on every mint — the map is small (≤ #options per active picker, so
    typically ≤ 10) so the O(n) scan is cheap. Cache entries pointing at
    expired tokens are pruned too so a stale fingerprint can't pin a dead
    token list.
    """
    if now is None:
        now = time.monotonic()
    stale = [tok for tok, e in _pick_tokens.items() if e.expires_at <= now]
    for tok in stale:
        _pick_tokens.pop(tok, None)
    if stale:
        stale_set = set(stale)
        for cache_key, tokens in list(_pick_token_cache.items()):
            if any(t in stale_set for t in tokens):
                _pick_token_cache.pop(cache_key, None)


def _mint_pick_token(entry: _PickTokenEntry) -> str:
    """Register a token for an option button. Returns the token id.

    Token is 12 hex chars from ``secrets.token_hex(6)``. The full callback
    payload is ``aqp:<token>`` → 17 chars total, well under Telegram's
    64-byte cap.
    """
    _prune_expired_pick_tokens()
    # 6 bytes = 12 hex chars. Collision space ~2^48; with at most a few
    # tokens live at any moment, accidental clash is astronomically
    # unlikely. Loop on the off chance.
    for _ in range(8):
        token = secrets.token_hex(6)
        if token not in _pick_tokens:
            _pick_tokens[token] = entry
            return token
    # Pathological — shouldn't happen, but signal loudly rather than
    # silently overwrite an existing token.
    raise RuntimeError("Unable to mint a unique pick token")


def consume_pick_token(token: str) -> _PickTokenEntry | None:
    """Pop a token (single-use). Returns the entry or None if missing/expired.

    Also drops the cache entry for the form generation this token belonged
    to: once a click lands, the form is about to advance to the next tab /
    question / review screen, and the next render needs fresh tokens
    against the new fingerprint anyway. Leaving the cache populated would
    keep handing out the just-consumed token (which would then 404 on
    click).
    """
    _prune_expired_pick_tokens()
    entry = _pick_tokens.pop(token, None)
    if entry is not None:
        cache_key = (
            entry.user_id,
            entry.thread_id or 0,
            entry.window_id,
            entry.fingerprint,
        )
        # Remove the cache row AND drop every sibling token belonging to
        # that row — the whole generation is dead now that the user has
        # acted on one of its buttons.
        sibling_tokens = _pick_token_cache.pop(cache_key, None)
        if sibling_tokens:
            for sib in sibling_tokens:
                if sib != token:
                    _pick_tokens.pop(sib, None)
    return entry


def reset_pick_tokens_for_tests() -> None:
    """Clear the pick-token map. Test-only helper."""
    _pick_tokens.clear()
    _pick_token_cache.clear()


# Cross-module emergency DM cooldown lives in ``handlers.attention``
# (``attention.should_emit_emergency_dm``). The interactive-UI surface and
# the assistant-text surface in ``handlers.message_queue`` share that fence
# so a single broken-topic episode cannot fire two DMs from two surfaces.


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _topic_link(chat_id: int, thread_id: int | None) -> str | None:
    """Build a best-effort Telegram private supergroup topic link."""
    if thread_id is None:
        return None
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{thread_id}"


async def _notify_waiting_dm(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    prompt_text: str,
) -> None:
    """Emergency-only DM fallback when the topic-first attention card fails.

    The normal path is ``attention.notify_waiting`` (in-topic card). This DM
    is reached only when the topic itself cannot be written to (deleted,
    closed, forbidden) so the user still gets a signal that Claude is blocked.

    Cooldown is owned by ``attention.should_emit_emergency_dm`` so repeated
    waiting episodes for the same route don't stack DMs.
    """
    if not attention.should_emit_emergency_dm(user_id, thread_id, window_id):
        logger.debug(
            "Skipping interactive waiting DM due to shared cooldown "
            "user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
        return

    display = session_manager.get_display_name(window_id) or window_id
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    link = _topic_link(chat_id, thread_id)
    message = f"🔔 Claude is waiting for input in {display}"
    if link:
        message += f"\n{link}"
    try:
        await bot.send_message(
            chat_id=user_id,
            text=message,
            link_preview_options=NO_LINK_PREVIEW,
        )
        logger.info(
            "Interactive waiting DM sent to user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
    except Exception as e:
        # Non-fatal: the in-topic interactive UI still exists. This commonly
        # fails if the user has not opened a DM with the bot.
        logger.debug("Failed to send interactive waiting DM to %d: %s", user_id, e)


def _render_ask_user_question(form: AskUserQuestionForm) -> str:
    """Render a structured AskUserQuestion form into Telegram-friendly text.

    The body produced here replaces the raw pane excerpt for picker variants
    that ``parse_ask_user_question`` understands. Two layout modes:

    * ``is_review_screen`` → render the summary header + the resolved answers,
      then the Submit / Cancel choice. This is the screen the user lands on
      after answering every tab.
    * Otherwise → render the tab strip with state glyphs, the current
      question title (if any), and the numbered options below.

    Output is plain text (no markdown conversion downstream) so terminal
    glyphs like ``☒`` / ``☐`` / ``✔`` survive verbatim. The caller still
    sends with ``plain=True``.
    """
    lines: list[str] = []

    if form.is_review_screen:
        lines.append("✔ Review your answers")
        # Tab strip with resolved markers; tabs are in the order they appeared
        # in the picker. Skip the synthetic submit cell — the prompt and
        # button row below carry that information already.
        content_tabs = [t for t in form.tabs if not t.is_submit]
        if content_tabs:
            lines.append("")
            for t in content_tabs:
                glyph = "☒" if t.answered else "☐"
                lines.append(f"  {glyph} {t.label}".rstrip())
        if form.options:
            lines.append("")
            lines.append("Ready to submit your answers?")
            lines.append("")
            for opt in form.options:
                cursor = "❯ " if opt.cursor else "  "
                rec = " (Recommended)" if opt.recommended else ""
                lines.append(f"{cursor}{opt.number}. {opt.label}{rec}")
        return "\n".join(lines).rstrip()

    # Picker layout — tabs (if any) → question title → options
    if form.tabs:
        cells: list[str] = []
        for t in form.tabs:
            if t.is_submit:
                cells.append("✔")
            else:
                glyph = "☒" if t.answered else "☐"
                label = t.label or ""
                cells.append(f"{glyph} {label}".rstrip())
        lines.append("  ".join(cells))
        lines.append("")

    if form.current_question_title:
        lines.append(form.current_question_title)
        lines.append("")

    if form.options:
        for opt in form.options:
            cursor = "❯ " if opt.cursor else "  "
            rec = " (Recommended)" if opt.recommended else ""
            lines.append(f"{cursor}{opt.number}. {opt.label}{rec}")
        if form.is_free_text:
            lines.append("")
            lines.append("  (Type something — send a regular message to free-text)")
        lines.append("")
        lines.append("Enter to select · Tab/Arrow keys to navigate · Esc to cancel")
        return "\n".join(lines).rstrip()

    # No options extracted (mid-redraw, or a layout the parser only partially
    # recognized). Caller falls back to the raw pane excerpt — return an
    # empty string to signal "no structured render available".
    return ""


def _build_pick_button_rows(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    form: AskUserQuestionForm,
) -> list[list[InlineKeyboardButton]]:
    """Build inline-keyboard rows of option-pick buttons for a parsed form.

    One button per option; max 5 per row. Each button mints a single-use
    token bound to ``(window, fingerprint, option_number, option_label)``
    so the callback handler can detect a "form changed under us" race
    before dispatching the keystroke.

    Review-screen Submit/Cancel rows are rendered here too. The Submit
    button is flagged ``is_review_submit=True`` so the callback handler
    can apply a tighter guard (must still be on the review screen) before
    sending Enter / digit 1.

    Returns an empty list when the form has no options — caller drops the
    structured-pick row and falls back to the keystroke keyboard only.
    """
    if not form.options:
        return []

    fingerprint = form.fingerprint()
    deadline = time.monotonic() + _PICK_TOKEN_TTL_SECONDS

    # Filter to options that can be dispatched via literal-N. Tokens are
    # only allocated for these; the keystroke fallback still reaches the
    # rest.
    pickable = [opt for opt in form.options if opt.number is not None]
    if not pickable:
        return []

    cache_key = (user_id, thread_id or 0, window_id, fingerprint)

    def _mint(opt_number: int, label: str, is_submit: bool) -> str:
        return _mint_pick_token(
            _PickTokenEntry(
                window_id=window_id,
                user_id=user_id,
                thread_id=thread_id,
                fingerprint=fingerprint,
                option_number=opt_number,
                option_label=label,
                is_review_submit=is_submit,
                expires_at=deadline,
            )
        )

    # Token-reuse path: if we already minted tokens for this exact form
    # generation (matching fingerprint), re-emit the same callback_data so
    # the rendered reply_markup is byte-identical and Telegram returns
    # MESSAGE_NOT_MODIFIED on the next edit. The cache row is wiped on
    # consume + on fingerprint change, so this can't hand out a stale
    # token bound to a different form.
    cached = _pick_token_cache.get(cache_key)
    if cached is not None and len(cached) == len(pickable):
        # Double-check that every cached token is still alive — TTL eviction
        # may have dropped some out from under us. If any are missing, fall
        # through to fresh-mint so callbacks don't 404 immediately.
        if all(t in _pick_tokens for t in cached):
            tokens: list[str] = cached
        else:
            _pick_token_cache.pop(cache_key, None)
            tokens = [
                _mint(
                    opt.number or 0,
                    opt.label,
                    form.is_review_screen and opt.cursor and opt.number == 1,
                )
                for opt in pickable
            ]
            _pick_token_cache[cache_key] = tokens
    else:
        tokens = [
            _mint(
                opt.number or 0,
                opt.label,
                form.is_review_screen and opt.cursor and opt.number == 1,
            )
            for opt in pickable
        ]
        _pick_token_cache[cache_key] = tokens

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    # Telegram tolerates more than 5 buttons per row, but on a phone the
    # text gets clipped after ~5. Cap conservatively.
    width = 5
    for opt, token in zip(pickable, tokens):
        # ``opt.number is None`` was filtered above, but reassure the type
        # checker.
        assert opt.number is not None
        is_submit = form.is_review_screen and opt.cursor and opt.number == 1
        # Button text: number + truncated label + recommended star
        prefix = "✅ " if is_submit else f"{opt.number}. "
        # Cap label so the whole button stays under Telegram's tap-target
        # readable width. 24 chars before truncation keeps "C — Parallel
        # tracks…" visible. Recommended star adds 1 char.
        max_label = 24
        truncated = (
            opt.label if len(opt.label) <= max_label else opt.label[:max_label] + "…"
        )
        star = " ★" if opt.recommended else ""
        text = f"{prefix}{truncated}{star}"
        row.append(InlineKeyboardButton(text, callback_data=f"{CB_ASK_PICK}{token}"))
        if len(row) >= width:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
    pick_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.

    ``pick_rows`` is the optional output of ``_build_pick_button_rows`` —
    when present, the structured pick row(s) are placed at the top of the
    keyboard, above the keystroke navigation. The keystroke row stays even
    when pick buttons are available so the user can still pick a free-text
    "Type something" option, dismiss with Esc, or refresh.
    """
    vertical_only = ui_name == "RestoreCheckpoint"

    rows: list[list[InlineKeyboardButton]] = []
    if pick_rows:
        rows.extend(pick_rows)
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    tool_input: dict | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.

    ``tool_input`` is the raw JSONL ``tool_use.input`` dict when available.
    For AskUserQuestion, this carries the complete option list independent
    of the tmux pane's visible region. The pane scrape sees only what's
    on screen, so long question text pushes early options off the top —
    using the structured payload here is order-stable and complete. The
    pane is still captured for the verbatim text excerpt and the keystroke
    fallback path.
    """
    ikey = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    # Quick check if it looks like an interactive UI
    if not is_interactive_ui(pane_text):
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_text.strip().split("\n")[-3:],
        )
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text)
    if not content:
        return False

    # For AskUserQuestion specifically, try the structured renderer first.
    # ``parse_ask_user_question`` is strict-or-None: it only returns a form
    # when it can produce a clean structured view. On a non-empty render we
    # use it; otherwise we fall back to the raw pane excerpt (the legacy
    # behavior for every other interactive UI).
    #
    # PR 2b: when the form carries numeric options, also mint a row of
    # option-pick buttons. The keystroke keyboard stays underneath so the
    # user can still navigate manually, dismiss with Esc, or write a free-
    # text reply.
    text = content.content
    pick_rows: list[list[InlineKeyboardButton]] | None = None
    if content.name == "AskUserQuestion":
        # Prefer the JSONL tool_use.input over pane scrape: it carries the
        # full option list even when the question text scrolled option 1
        # off the top of the visible pane. Fall back to the pane parser
        # only when tool_input is absent or doesn't yield options.
        form: AskUserQuestionForm | None = build_form_from_tool_input(tool_input)
        if form is None:
            form = parse_ask_user_question(pane_text)
        if form is not None:
            structured = _render_ask_user_question(form)
            if structured:
                text = structured
            built = _build_pick_button_rows(user_id, thread_id, window_id, form)
            if built:
                pick_rows = built

    # Build message with navigation keyboard (structured rows on top when
    # available, keystroke nav row below for free-text / manual paths).
    keyboard = _build_interactive_keyboard(
        window_id, ui_name=content.name, pick_rows=pick_rows
    )

    # Check if we have an existing interactive message to edit
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        edit_outcome = await topic_edit(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing_msg_id,
            text=text,
            plain=True,
            reply_markup=keyboard,
        )
        # MESSAGE_NOT_MODIFIED means Claude redrew an identical UI; treating it
        # as success keeps the same Telegram message in place (no fresh card,
        # no delete-then-resend churn).
        if edit_outcome in (
            TopicSendOutcome.OK,
            TopicSendOutcome.MESSAGE_NOT_MODIFIED,
        ):
            _interactive_mode[ikey] = window_id
            # The interactive card edit landed in the topic. The separate
            # "🔔 waiting for input" attention card is suppressed here: it
            # was a duplicate of the same content, in the same topic, with
            # a self-pointing link. Telegram's own notification on the
            # edited card already covers the "ping the user" use case; the
            # attention card is reserved for the topic-send-failed branch
            # below where the user genuinely doesn't see the card.
            return True
        # Edit failed — fall through to fresh send while keeping the old id
        # so we can delete it after a new one lands.

    # Send new message (plain text — terminal content is not markdown).
    # §2.5.2: anchor the interactive card to the user's prompt that triggered
    # the tool, when we know it. ``peek`` (not consume) so the same anchor
    # still applies when Claude follows up with assistant text after the
    # user resolves the interactive card — both the card and the trailing
    # text are responses to the same user prompt. The text-side
    # ``_process_content_task`` is the canonical owner of the anchor's
    # lifecycle (it pops on first-part send).
    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    anchor: ReplyParameters | None = None
    if config.reply_context_enabled:
        from .message_queue import peek_route_last_user_message

        anchor_id = peek_route_last_user_message(user_id, thread_id, window_id)
        if anchor_id is not None:
            anchor = ReplyParameters(message_id=anchor_id)
    interactive_session_id = session_id_for_window(window_id)
    if anchor is not None:
        sent, send_outcome = await topic_send(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            plain=True,
            reply_markup=keyboard,
            reply_parameters=anchor,
            role="tool",
            content_type="tool_use",
            session_id=interactive_session_id,
        )
    else:
        sent, send_outcome = await topic_send(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            plain=True,
            reply_markup=keyboard,
            role="tool",
            content_type="tool_use",
            session_id=interactive_session_id,
        )
    if sent is None:
        # Topic send failed — still mark interactive mode (prevents per-poll
        # retry spam) and try the topic-first attention card. If that also
        # cannot reach the topic, emergency-fall back to a direct DM.
        _interactive_mode[ikey] = window_id
        outcome = await attention.notify_waiting(
            bot,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            prompt_text=text,
            kind="interactive_ui",
        )
        if outcome is not TopicSendOutcome.OK and send_outcome in (
            TopicSendOutcome.TOPIC_NOT_FOUND,
            TopicSendOutcome.TOPIC_CLOSED,
            TopicSendOutcome.FORBIDDEN,
        ):
            await _notify_waiting_dm(bot, user_id, window_id, thread_id, text)
        return False
    _interactive_msgs[ikey] = sent.message_id
    _interactive_mode[ikey] = window_id
    # See note above: the interactive card landed in the topic, so the
    # duplicate "🔔 waiting for input" attention card is suppressed. The
    # send-failed branch still fires notify_waiting because that's the
    # only signal the user gets when the topic-send couldn't deliver.
    # New message sent successfully — now safe to delete the old one
    if existing_msg_id:
        await topic_delete(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing_msg_id,
        )
    return True


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        await topic_delete(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=None,
            message_id=msg_id,
        )
    if bot:
        await attention.dismiss(bot, user_id=user_id, thread_id=thread_id)
