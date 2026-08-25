"""Telegram inbound handlers for reliable text, media, and voice delivery.

Extracts the inbound-side of bot.py: every code path that runs when a user
sends content into a Telegram topic. Each handler resolves the topic → tmux
window → Claude session route, applies §2.5 reply-context rendering, and
hands off to the aggregator/queue layer.

Core responsibilities:
  - text_handler / photo_handler / voice_handler / document_handler: the
    four inbound MessageHandler entrypoints registered in
    ``bot.create_bot()``. Photo and document handlers also drive the
    media-group bundling path via ``aggregator_offer_{photo,document}``.
  - Pending-route-payload state machine (``_clear_pending_route_payload``,
    ``_flush_pending_route_payload``, ``_pending_owner_matches``): stashes
    text + attachments in the thread's per-topic picker entry (GH #66) while
    an unbound topic is in the directory/session/window picker, and flushes
    them onto the freshly-bound route once the picker commits.
  - Window-creation helpers (``_create_and_bind_window``,
    ``_abort_created_window_after_pending_owner_change``,
    ``_cleanup_unbound_created_window``): shared by text_handler's
    auto-create path and by the callback dispatch in bot.py for
    CB_DIR_CONFIRM / CB_SESSION_NEW / CB_SESSION_SELECT.
  - ``_apply_reply_context``: §2.5 quote-rendering used by every
    inbound handler so a reply via voice/photo/document carries the same
    quote block as a text reply.
  - ``_capture_bash_output`` / ``_cancel_bash_capture``: text_handler's
    background tmux-pane capture for ``!`` bash commands.

Key callers in bot.py: command + callback handlers re-import these names
from ``handlers.inbound_telegram`` so the original ``bot.<name>``
attribute access (used in tests and a couple of module-level lookups)
keeps resolving to the same function objects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from telegram import (
    Bot,
    CallbackQuery,
    Message,
    Update,
    User,
)
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import ContextTypes

from .. import delivery, route_runtime
from . import artifacts, decision_token, pane_signals, trust_flow, usage_cache
from ..config import config
from ..delivery import DeliveryResult
from ..markdown_v2 import convert_markdown
from ..session import peek_session_id_for_window, session_manager
from ..terminal_parser import extract_bash_output, is_interactive_ui
from ..tmux_manager import LifecycleTimeout, tmux_manager
from ..transcribe import transcribe_voice
from ..utils import app_dir
from . import attention
from . import reply_context as reply_context_mod
from .directory_browser import (
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    build_directory_browser,
    drop_picker_entry,
    entry_token,
    picker_entry,
)
from .inbound_aggregator import (
    AggregatorReplayAttachment,
    Provenance,
    aggregator_clear_route,
    aggregator_offer_document,
    aggregator_offer_photo,
    aggregator_offer_text,
    aggregator_offer_voice,
    aggregator_replay_payload,
)
from .interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import (
    clear_status_msg_info,
    enqueue_status_update,
    set_route_last_user_message,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    safe_answer,
    safe_edit,
    safe_reply,
    send_with_fallback,
)
from .reply_context import extract_reply_context, render_for_claude

logger = logging.getLogger(__name__)

_VOICE_DOWNLOAD_ATTEMPTS = 3
_VOICE_DOWNLOAD_BACKOFFS_S = (1.0, 3.0)


async def _typing_action_best_effort(message: Message, thread_id: int | None) -> None:
    """Send the cosmetic TYPING chat action; never let it kill the handler.

    The typing hint is pure decoration ahead of payload delivery — a transient
    Telegram network error here must not drop the inbound message (GH #51: a
    TimedOut on this call silently ate a successfully transcribed voice note).
    """
    try:
        await message.chat.send_action(ChatAction.TYPING)
    except Exception:
        logger.warning("inbound typing action failed (non-fatal) thread=%s", thread_id)


def _voice_failure_classification(error: Exception) -> str:
    """Classify a transcription failure without exposing user content."""
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, ValueError):
        return "empty"
    if isinstance(error, httpx.TransportError):
        return "transport"
    return "other"


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


@dataclass(frozen=True)
class PendingAttachment:
    path: str
    caption: str
    media_group_id: str | None
    # GH #50 PR-2 (plan §2.3 [r4 P2-1]): the provenance fact must survive the
    # unbound-topic stash, or the pending-bind replay would have to GUESS whether
    # the caption carries a reply-context quote. Defaulted so an older
    # in-flight/persisted tuple keeps working.
    has_reply_context: bool = False


# The provenance facts of a stashed pending TEXT payload (the same reason). Kept
# inside this thread's picker entry beside ``_pending_thread_text``.
_PENDING_TEXT_FACTS_KEY = "_pending_thread_text_facts"


def _pending_owner_matches(user_data: dict | None, thread_id: int | None) -> bool:
    """Return True when ``thread_id`` still owns a live pending picker entry.

    GH #66: ownership is now per-thread — an entry exists in ``_pending_pickers``
    for exactly the threads that have a picker/payload in flight.
    """
    if thread_id is None:
        return False
    return picker_entry(user_data, thread_id) is not None


def _clear_pending_route_payload(
    user_data: dict | None,
    thread_id: int | None,
    *,
    delete_files: bool,
) -> list[PendingAttachment]:
    """Drop ``thread_id``'s whole picker entry and optionally delete its files.

    Pending text/photo/document data lives inside the thread's picker entry
    while the user is choosing a directory/window/session. Cancel, bind failure,
    and the successful-flush clear all drop the entry as a unit (state, browse
    caches, text, and attachments) — otherwise a later bind could forward media
    the user already cancelled. GH #66: thread-scoped, so dropping one topic's
    entry never touches another's.
    """
    entry = drop_picker_entry(user_data, thread_id)
    if entry is None:
        return []
    attachments: list[PendingAttachment] = list(
        entry.get("_pending_thread_attachments", []) or []
    )
    if delete_files:
        for attachment in attachments:
            try:
                Path(attachment.path).unlink(missing_ok=True)
            except OSError as e:
                logger.debug(
                    "failed to delete pending attachment %s: %s", attachment.path, e
                )
    return attachments


def _clear_pending_route_payload_for_thread(
    user_data: dict | None,
    thread_id: int,
    *,
    delete_files: bool,
) -> list[PendingAttachment]:
    """Clear ``thread_id``'s pending payload (thread-scoped by construction).

    GH #66: with per-thread keying, dropping the entry is already scoped to
    ``thread_id`` and cannot touch another topic's newer payload, so this is a
    thin alias over ``_clear_pending_route_payload`` kept for the topic-close
    call site's readable intent.
    """
    return _clear_pending_route_payload(user_data, thread_id, delete_files=delete_files)


def _delete_pending_attachment_files(attachments: list[PendingAttachment]) -> None:
    """Delete downloaded files that belonged to a failed pending-route payload."""
    for attachment in attachments:
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError as e:
            logger.debug(
                "failed to delete pending attachment %s: %s", attachment.path, e
            )


def _remember_picker_card(entry: dict | None, sent: Message | None) -> None:
    """Record the picker card's Telegram coordinates in the thread's entry.

    GH #66 (part D): stored so a teardown of this entry (topic close) can
    disable the orphaned card. Best-effort — a missing entry / send is a no-op.
    """
    if entry is None or sent is None:
        return
    entry[CARD_CHAT_ID_KEY] = sent.chat_id
    entry[CARD_MSG_ID_KEY] = sent.message_id


async def _flush_pending_route_payload(
    route: tuple[int, int, str],
    user_data: dict | None,
) -> DeliveryResult | None:
    """Synchronously replay the pending first-turn payload for a new binding.

    Returns the STRUCTURED delivery result when a pending payload was attempted,
    and ``None`` when there was no pending payload. GH #50 §1.4: the bare
    ``bool`` discarded the reason, and this IS the fresh-session folder-trust
    case — the very first message into a brand-new window lands while Claude is
    blocked on "Do you trust the files in this folder?", so the refusal must
    name it.

    Pending picker state is cleared before sending to make callback
    double-clicks idempotent; on failure, route buffers are cleared and
    downloaded pending files are deleted so the user gets an explicit resend
    prompt instead of a hidden retry that could duplicate a manual resend.
    """
    if user_data is not None and not _pending_owner_matches(user_data, route[1]):
        logger.warning(
            "Refusing to flush pending payload for route %s because thread %s no "
            "longer owns a pending picker entry",
            route,
            route[1],
        )
        return None

    entry = picker_entry(user_data, route[1])
    pending_text = entry.get("_pending_thread_text") if entry else None
    pending_facts = entry.get(_PENDING_TEXT_FACTS_KEY) if entry else None
    pending_attachments: list[PendingAttachment] = (
        list(entry.get("_pending_thread_attachments") or []) if entry else []
    )
    if user_data is not None:
        _clear_pending_route_payload(user_data, route[1], delete_files=False)

    if not pending_text and not pending_attachments:
        return None

    replay_attachments = [
        AggregatorReplayAttachment(
            path=Path(attachment.path),
            caption=attachment.caption,
            media_group_id=attachment.media_group_id,
            has_reply_context=attachment.has_reply_context,
        )
        for attachment in pending_attachments
    ]
    text_provenance = (
        Provenance(
            typed_text=bool(pending_facts.get("typed_text")),
            reply_context=bool(pending_facts.get("reply_context")),
        )
        if isinstance(pending_facts, dict)
        else None
    )

    try:
        result = await aggregator_replay_payload(
            route,
            text=pending_text if isinstance(pending_text, str) else None,
            attachments=replay_attachments,
            text_provenance=text_provenance,
        )
    except Exception as e:
        logger.error("pending route payload replay raised for route %s: %s", route, e)
        result = delivery.refuse(delivery.REASON_SEND_FAILED, written=False)

    if not result.ok:
        aggregator_clear_route(route)
        _delete_pending_attachment_files(pending_attachments)
    return result


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


async def _build_browser_payload() -> trust_flow.BrowserPayload:
    """Render the directory browser — the LAST pre-processing await.

    GH #65 review r1 P1-2: this is built OUTSIDE the ownership critical section
    (it lists tmux windows and scans a directory), so it must be produced BEFORE
    the decision that consumes it — never between the decision and the mutation.
    """
    unbound_count = len(await _list_unbound_windows(tmux_manager, session_manager))
    start_path = str(config.browse_root)
    msg_text, keyboard, subdirs = build_directory_browser(
        start_path, unbound_count=unbound_count
    )
    return trust_flow.BrowserPayload(
        text=msg_text,
        keyboard=keyboard,
        subdirs=subdirs,
        unbound_count=unbound_count,
        start_path=start_path,
    )


async def _list_unbound_windows(
    tmux_mgr: Any,
    session_mgr: Any,
    *,
    listing: list[Any] | None = None,
) -> list[tuple[str, str, str]]:
    """Return tmux windows not currently bound to any topic, as (id, name, cwd).

    "Unbound" is NOT the same as "adoptable" (GH #65 review r12 P1-C). A
    trust-lane creation flow binds its window as the LAST step of its tail, so
    for the whole life of the flow the window is unbound — and offering it here
    let another topic legitimately grab it mid-flow, producing two routes to one
    window. Ownership by a live flow is the third state between "bound" and
    "free", and excluding it closes that race AT THE SOURCE rather than merely
    detecting it in the trust tail's exclusivity re-check.
    """
    from cctelegram.handlers import trust_flow

    # ``listing`` lets an ADOPTION caller pass the DIRECT read it already took
    # under the lifecycle lock (review r16), so no adoption decision consults
    # the TTL cache even indirectly. Display callers pass nothing and get the
    # cached view, which is all the browser render needs.
    all_windows = listing if listing is not None else await tmux_mgr.list_windows()
    bound_ids = {bid for _, _, bid in session_mgr.iter_thread_bindings()}
    owned_ids = trust_flow.windows_owned_by_live_flows()
    return [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids and w.window_id not in owned_ids
    ]


def _ensure_private_media_dir(path: Path) -> Path:
    """Create-and-repair an attachment dir at mode 0700 and return it.

    User uploads can carry sensitive content, so these dirs follow the same
    0700/0600 posture as every other sensitive store (auq_pending/,
    msg_display/). The chmod ALWAYS runs — ``mkdir(mode=...)`` is a no-op on
    an existing dir, so an upgraded install's loose 0755 dir must be
    repaired. OSError → log WARNING + continue (never silent, never fatal).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as e:
        logger.warning("could not ensure %s at mode 0700: %s", path, e)
    return path


def _restrict_download_perms(path: Path) -> None:
    """Chmod a downloaded attachment to 0600 (owner-only).

    Downloads land with umask defaults (0644); tighten after write. OSError →
    log WARNING + continue — never fail the download over a perms repair.
    """
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("could not chmod %s to mode 0600: %s", path, e)


# --- Image directory for incoming photos ---
_IMAGES_DIR = _ensure_private_media_dir(app_dir() / "images")

# --- File directory for incoming documents ---
_FILES_DIR = _ensure_private_media_dir(app_dir() / "files")


async def _apply_reply_context(
    message: Message,
    user_id: int,
    thread_id: int | None,
    text: str,
) -> tuple[str, bool]:
    """Render the §2.5 quote block onto ``text`` for a reply-aware message.

    Returns ``(rendered_text, applied)``. ``applied`` is True IFF a quote block
    was actually rendered into the text — GH #50 PR-2 (plan §2.3 [r4 P2-1]) needs
    the provenance fact OBSERVED, not guessed: by the time the aggregator sees
    the string, a rendered quote is indistinguishable from prose the user typed.

    A reply-context payload **IS** free-text-eligible (OWNER DECISION 2026-07-12,
    superseding plan §2.3, which made it ineligible): the owner's dominant gesture
    at a card is a VOICE NOTE sent as a REPLY to it, so the as-planned rule refused
    their most natural way of answering. Claude receives the FULL rendered payload
    — the quoted context AND the user's words — exactly as it would for any other
    send. The FACT is still observed and carried through merges and pending-bind
    replay; only its effect on eligibility changed (see
    ``inbound_aggregator.Provenance.free_text_eligible``).

    ``(text, False)`` when the kill switch is off, when there is no quoted
    referent, or when the quote points at a stale (e.g. /clear-ed) session and
    the cross-session switch drops it — the same stale-quote guard the
    text_handler had inline. Used by text/voice/photo/document handlers so a
    reply made via voice or photo+caption carries the same quote-injection block
    as a text reply.
    """
    if not config.reply_context_enabled:
        return text, False
    reply_ctx = extract_reply_context(message)
    if reply_ctx is None:
        return text, False
    reply_ctx = await reply_context_mod.resolve(reply_ctx, message.chat.id)
    # The stale-quote check needs only the window's CURRENT session id, which is
    # the in-memory session_map mirror — NOT the transcript. Use the read-only
    # id peek (P1) instead of resolve_session_for_window, which parsed the whole
    # JSONL just to hand back the same id.
    bound_wid = session_manager.resolve_window_for_thread(user_id, thread_id)
    current_sid = peek_session_id_for_window(bound_wid)
    stale_quote = (
        reply_ctx.session_id is not None
        and current_sid is not None
        and reply_ctx.session_id != current_sid
    )
    if stale_quote:
        # P1.5: render the quote with a cross-session marker rather than
        # dropping silently. The §2.5.4 routing rule still applies — the
        # topic's current window binding remains the routing authority;
        # the marker only tells Claude the quoted body is from a prior
        # session so it doesn't treat it as part of this conversation's
        # transcript. Kill switch ``CC_TELEGRAM_REPLY_CROSS_SESSION=false``
        # restores the pre-P1.5 silent-drop behaviour.
        if not config.reply_context_cross_session_enabled:
            logger.info(
                "Dropping reply context (cross-session kill switch on): "
                "quoted session %s != current %s (window=%s, thread=%s)",
                reply_ctx.session_id,
                current_sid,
                bound_wid,
                thread_id,
            )
            return text, False
        logger.info(
            "Rendering cross-session reply context: quoted session %s != "
            "current %s (window=%s, thread=%s)",
            reply_ctx.session_id,
            current_sid,
            bound_wid,
            thread_id,
        )
        return render_for_claude(text, reply_ctx, cross_session=True), True
    return render_for_claude(text, reply_ctx), True


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)

    # Download the highest-resolution photo (we need a path either way:
    # bound topic feeds the aggregator, unbound topic stashes the path so
    # the directory-pick flush has the file ready).
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)
    _restrict_download_perms(file_path)

    caption = update.message.caption or ""
    media_group_id = update.message.media_group_id

    # GH #65 review r3 P2-4: an initially-unbound payload that resolves BOUND
    # after the download race has already had its reply context rendered, so the
    # bound path below must not render it a SECOND time (two quote blocks for
    # one message). One flag, set the moment it is applied.
    reply_context_applied = False
    has_reply_ctx = False
    if wid is None:
        # §2.5: render reply-context before stashing an unbound-topic caption
        # so the later directory/window/session-picker flush preserves the
        # same quote block as the bound aggregator path below. Keep the same
        # media-group guard as the bound path: non-caption-bearing album items
        # must not each synthesize their own quote block.
        reply_context_applied = True
        if caption or media_group_id is None:
            caption, has_reply_ctx = await _apply_reply_context(
                update.message, user.id, thread_id, caption
            )

        def _stash_photo(entry: dict[str, Any]) -> None:
            # §2.8.3 photo-in-unbound-topic: stash the path so the picker's
            # flush can feed the aggregator for the freshly-bound route.
            entry.setdefault("_pending_thread_attachments", []).append(
                PendingAttachment(
                    str(file_path), caption, media_group_id, has_reply_ctx
                )
            )

        # GH #65 Fix 5 (+ review r1 P1-2): the download, the reply-context
        # resolution AND the browser build are all AWAITS a creation flow or a
        # binding can complete inside. ``claim_unbound_inbound`` decides and
        # mutates in ONE critical section — the browser is built before it, and
        # a still-free topic is CLAIMED inside it.
        decision = await trust_flow.claim_unbound_inbound(
            user.id,
            thread_id,
            context.user_data,
            session_manager,
            build_browser=_build_browser_payload,
            stash=_stash_photo,
        )
        if decision.kind == "trust_owned":
            await safe_reply(update.message, trust_flow.TRUST_NUDGE)
            return
        if decision.kind == "picker_owned":
            # Mid-picker: stashing is enough — re-emitting the picker would
            # stomp on the user's browse progress.
            return
        if decision.kind == "browser":
            # The entry was claimed (state + browse caches + stash) under the
            # lock; all that is left is the send. Always the directory browser
            # for unbound topics — the opt-in "🖥 Bind existing window" row
            # appears when unbound tmux windows exist, but the directory choice
            # stays primary.
            assert decision.browser is not None
            sent = await safe_reply(
                update.message,
                decision.browser.text,
                reply_markup=decision.browser.keyboard,
            )
            _remember_picker_card(decision.entry, sent)
            return
        # The binding appeared while we downloaded: deliver THIS payload
        # through the normal bound path below instead of a stale picker.
        assert decision.window_id is not None
        wid = decision.window_id

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        # /cost overlay cache: the vanished window's cached usage overlay dies
        # with the binding — a later window reusing the id must not inherit it.
        usage_cache.clear_route((user.id, thread_id or 0, wid))
        # B2.3 review fold P2-A: the unbound route's Decision tokens + nav
        # generation die with the binding — a stale dcp:/gate-nav tap must
        # never survive into a window id a later binding may reuse.
        decision_token.teardown_route(user.id, thread_id, wid)
        # Artifact delivery lane: the unbound route's 📎 download cards die with
        # the binding — a stale dlf: tap must never survive into a window id a
        # later binding may reuse.
        artifacts.invalidate_window(wid)
        # P1: the vanished window's post-/exit quarantine dies with the
        # binding — a later window reusing the id must not inherit it.
        tmux_manager.clear_window_quarantine(wid, reason="stale-window unbind")
        # GH #50 peer-review P1: the stranded-draft brake is DELIBERATELY NOT
        # cleared here. ``find_window_by_id`` reads the 1s ``list_windows``
        # cache, so a transient tmux failure reports "gone" for a LIVE window —
        # and this unbind holds no ``window_send_lock``, so dropping the brake
        # would let a send already queued on that lock append to the leftover
        # draft and commit both. A brake entry for a genuinely dead window is
        # inert (``_deliver_locked`` refuses ``window_gone`` before it consults
        # the brake) and is reaped by the real proofs: an empty-box capture, or
        # tmux's own kill_window / create_window seams.
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await _typing_action_best_effort(update.message, thread_id)
    clear_status_msg_info(user.id, thread_id)

    # §2.5.2: anchor the next assistant-text response to the LATEST inbound
    # photo's message_id (matches Telegram's "reply to most recent" UX).
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.5: render reply-context onto the caption so a photo reply carries
    # the same quote-injection block as a text reply. Skip when this update
    # is a non-caption-bearing item of a media group — Telegram puts the
    # caption on item 1 only, and rendering with empty caption on items 2-N
    # would re-emit the quote block multiple times (the random nonce in
    # ``render_for_claude`` defeats the aggregator's exact-string dedup).
    if not reply_context_applied:
        if caption or media_group_id is None:
            caption, has_reply_ctx = await _apply_reply_context(
                update.message, user.id, thread_id, caption
            )

    # §2.8: feed photo + caption + media_group_id into the aggregator. The
    # bundle's flush handler builds the §2.8.2 single-text + grouped-paths
    # shape so a media-group with one caption stops fragmenting across
    # N Claude turns.
    route = (user.id, thread_id, wid)
    await aggregator_offer_photo(
        route,
        file_path,
        caption,
        media_group_id,
        bot=context.bot,
        has_reply_context=has_reply_ctx,
    )

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        # /cost overlay cache: the vanished window's cached usage overlay dies
        # with the binding — a later window reusing the id must not inherit it.
        usage_cache.clear_route((user.id, thread_id or 0, wid))
        # B2.3 review fold P2-A: the unbound route's Decision tokens + nav
        # generation die with the binding — a stale dcp:/gate-nav tap must
        # never survive into a window id a later binding may reuse.
        decision_token.teardown_route(user.id, thread_id, wid)
        # Artifact delivery lane: the unbound route's 📎 download cards die with
        # the binding — a stale dlf: tap must never survive into a window id a
        # later binding may reuse.
        artifacts.invalidate_window(wid)
        # P1: the vanished window's post-/exit quarantine dies with the
        # binding — a later window reusing the id must not inherit it.
        tmux_manager.clear_window_quarantine(wid, reason="stale-window unbind")
        # GH #50 peer-review P1: the stranded-draft brake is DELIBERATELY NOT
        # cleared here. ``find_window_by_id`` reads the 1s ``list_windows``
        # cache, so a transient tmux failure reports "gone" for a LIVE window —
        # and this unbind holds no ``window_send_lock``, so dropping the brake
        # would let a send already queued on that lock append to the leftover
        # draft and commit both. A brake entry for a genuinely dead window is
        # inert (``_deliver_locked`` refuses ``window_gone`` before it consults
        # the brake) and is reaped by the real proofs: an empty-box capture, or
        # tmux's own kill_window / create_window seams.
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    raw_duration = update.message.voice.duration
    duration_s = raw_duration if isinstance(raw_duration, int) else None
    logger.info("voice note received duration_s=%r thread=%s", duration_s, thread_id)

    # Download voice as in-memory bytes
    ogg_data: bytes | None = None
    download_error: Exception | None = None
    attempts_made = 0
    for attempt in range(1, _VOICE_DOWNLOAD_ATTEMPTS + 1):
        attempts_made = attempt
        try:
            voice_file = await update.message.voice.get_file()
            ogg_data = bytes(await voice_file.download_as_bytearray())
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            NetworkError,
        ) as e:
            if attempt == _VOICE_DOWNLOAD_ATTEMPTS:
                download_error = e
                break
            await asyncio.sleep(_VOICE_DOWNLOAD_BACKOFFS_S[attempt - 1])
        except Exception as e:
            download_error = e
            break
        else:
            break

    if download_error is not None:
        logger.info(
            "voice download failed classification=%s attempts=%d thread=%s",
            _voice_failure_classification(download_error),
            attempts_made,
            thread_id,
        )
        try:
            await safe_reply(
                update.message,
                "⚠ Couldn't download your voice note from Telegram (network error) "
                "— please resend.",
            )
        except Exception:
            logger.warning(
                "voice download failure reply failed user=%d thread=%s",
                user.id,
                thread_id,
            )
        return

    assert ogg_data is not None
    logger.info(
        "voice transcription received duration_s=%r bytes=%d thread=%s",
        duration_s,
        len(ogg_data),
        thread_id,
    )

    # Transcribe
    transcribe_started = time.monotonic()
    try:
        text = await transcribe_voice(ogg_data, duration_s=duration_s)
    except TimeoutError as e:
        logger.info(
            "voice transcription failed classification=%s thread=%s",
            _voice_failure_classification(e),
            thread_id,
        )
        await safe_reply(
            update.message,
            "⚠ Voice transcription timed out — it may not have completed; "
            "please resend or send shorter segments",
        )
        return
    except ValueError as e:
        logger.info(
            "voice transcription failed classification=%s thread=%s",
            _voice_failure_classification(e),
            thread_id,
        )
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.info(
            "voice transcription failed classification=%s thread=%s",
            _voice_failure_classification(e),
            thread_id,
        )
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return
    logger.info(
        "voice transcription succeeded latency_ms=%d text_len=%d thread=%s",
        round((time.monotonic() - transcribe_started) * 1000),
        len(text),
        thread_id,
    )

    await _typing_action_best_effort(update.message, thread_id)
    clear_status_msg_info(user.id, thread_id)

    # §2.5.2 + §2.8: voice messages take the same path as text — anchor the
    # outbound response to the user's voice-message Telegram id, then feed
    # the transcribed text into the aggregator so a voice-then-text or
    # voice-then-photo bundle still lands as one Claude turn.
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)
    route = (user.id, thread_id, wid)
    if not text:
        # ``aggregator_offer_voice`` (and its underlying
        # ``aggregator_offer_text``) silently no-op on empty text. Surface
        # that in logs so an empty-transcription failure mode is visible
        # rather than looking like the bot dropped the voice message.
        logger.debug(
            "voice transcription empty for user=%d thread=%s",
            user.id,
            thread_id,
        )
    # Show the raw transcription to the user (echo bubble) before wrapping
    # the prompt with §2.5 reply context — the echo is for the human, the
    # rendered text is what Claude actually sees.
    echo = text
    rendered, has_reply_ctx = await _apply_reply_context(
        update.message, user.id, thread_id, text
    )
    await aggregator_offer_voice(
        route, rendered, bot=context.bot, has_reply_context=has_reply_ctx
    )

    try:
        await safe_reply(update.message, f'🎤 "{echo}"')
    except Exception:
        logger.warning(
            "voice transcription echo failed user=%d thread=%s",
            user.id,
            thread_id,
        )


def _sanitize_filename_part(part: str, max_len: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    return cleaned[:max_len]


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle documents sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.document:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    document = update.message.document
    file_size = document.file_size
    limit_mb = config.max_attachment_size_bytes / (1024 * 1024)
    if file_size is None:
        await safe_reply(
            update.message,
            f"⚠ File size unknown — refusing to download. Limit is {limit_mb:.0f} MB.",
        )
        return
    if file_size > config.max_attachment_size_bytes:
        size_mb = file_size / (1024 * 1024)
        await safe_reply(
            update.message,
            f"⚠ File too large ({size_mb:.1f} MB). Limit is {limit_mb:.0f} MB.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)

    original = document.file_name or "file"
    stem, ext = os.path.splitext(original)
    safe_stem = _sanitize_filename_part(stem, 100) or "file"
    safe_ext = _sanitize_filename_part(ext, 16) if ext else ""
    filename = f"{int(time.time())}_{document.file_unique_id}_{safe_stem}{safe_ext}"
    file_path = _FILES_DIR / filename

    tg_file = await document.get_file()
    await tg_file.download_to_drive(file_path)
    _restrict_download_perms(file_path)

    caption = update.message.caption or ""
    media_group_id = update.message.media_group_id

    # GH #65 review r3 P2-4: see photo_handler — a payload that resolves BOUND
    # after the download race must carry EXACTLY ONE quote block.
    reply_context_applied = False
    has_reply_ctx = False
    if wid is None:
        # §2.5: render reply-context before stashing an unbound-topic caption
        # so the later picker flush preserves the same quote block as the bound
        # aggregator path below. Keep the same media-group guard as the bound
        # path to avoid duplicate quote blocks for non-caption-bearing items.
        reply_context_applied = True
        if caption or media_group_id is None:
            caption, has_reply_ctx = await _apply_reply_context(
                update.message, user.id, thread_id, caption
            )

        def _stash_document(entry: dict[str, Any]) -> None:
            entry.setdefault("_pending_thread_attachments", []).append(
                PendingAttachment(
                    str(file_path), caption, media_group_id, has_reply_ctx
                )
            )

        # GH #65 Fix 5 (+ review r1 P1-2): one critical section for the
        # decision AND the mutation, with the browser built before it.
        decision = await trust_flow.claim_unbound_inbound(
            user.id,
            thread_id,
            context.user_data,
            session_manager,
            build_browser=_build_browser_payload,
            stash=_stash_document,
        )
        if decision.kind == "trust_owned":
            await safe_reply(update.message, trust_flow.TRUST_NUDGE)
            return
        if decision.kind == "picker_owned":
            return
        if decision.kind == "browser":
            assert decision.browser is not None
            sent = await safe_reply(
                update.message,
                decision.browser.text,
                reply_markup=decision.browser.keyboard,
            )
            _remember_picker_card(decision.entry, sent)
            return
        assert decision.window_id is not None
        wid = decision.window_id

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        # /cost overlay cache: the vanished window's cached usage overlay dies
        # with the binding — a later window reusing the id must not inherit it.
        usage_cache.clear_route((user.id, thread_id or 0, wid))
        # B2.3 review fold P2-A: the unbound route's Decision tokens + nav
        # generation die with the binding — a stale dcp:/gate-nav tap must
        # never survive into a window id a later binding may reuse.
        decision_token.teardown_route(user.id, thread_id, wid)
        # Artifact delivery lane: the unbound route's 📎 download cards die with
        # the binding — a stale dlf: tap must never survive into a window id a
        # later binding may reuse.
        artifacts.invalidate_window(wid)
        # P1: the vanished window's post-/exit quarantine dies with the
        # binding — a later window reusing the id must not inherit it.
        tmux_manager.clear_window_quarantine(wid, reason="stale-window unbind")
        # GH #50 peer-review P1: the stranded-draft brake is DELIBERATELY NOT
        # cleared here. ``find_window_by_id`` reads the 1s ``list_windows``
        # cache, so a transient tmux failure reports "gone" for a LIVE window —
        # and this unbind holds no ``window_send_lock``, so dropping the brake
        # would let a send already queued on that lock append to the leftover
        # draft and commit both. A brake entry for a genuinely dead window is
        # inert (``_deliver_locked`` refuses ``window_gone`` before it consults
        # the brake) and is reaped by the real proofs: an empty-box capture, or
        # tmux's own kill_window / create_window seams.
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await _typing_action_best_effort(update.message, thread_id)
    clear_status_msg_info(user.id, thread_id)

    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.5: see photo_handler for the media-group caption-skip rationale.
    if not reply_context_applied:
        if caption or media_group_id is None:
            caption, has_reply_ctx = await _apply_reply_context(
                update.message, user.id, thread_id, caption
            )

    route = (user.id, thread_id, wid)
    await aggregator_offer_document(
        route,
        file_path,
        caption,
        media_group_id,
        bot=context.bot,
        has_reply_context=has_reply_ctx,
    )

    await safe_reply(update.message, "📎 File sent to Claude Code.")


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)
    wid = (
        session_manager.get_window_for_thread(user.id, thread_id)
        if thread_id is not None
        else None
    )

    # Must be in a named topic — rejected BEFORE the cross-thread stale-picker
    # guards below (matching photo_handler/document_handler ordering). PTB
    # user_data is per-user across chats, so a stray DM/General text would
    # otherwise evaluate ``pending_tid == None`` → False in those guards and
    # destroy another topic's in-progress picker flow (clearing its browse
    # state and deleting its pending attachment files) before dead-ending
    # here anyway (review finding 8). A DM/General message must touch NOTHING.
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    # Capture group chat_id for supergroup forum topic routing. GH #41: written
    # AFTER the thread_id-None reject so a DM/General message never mints a
    # ``user:0`` garbage key (an unbound named topic still writes it — the
    # directory-browser bootstrap needs the mapping before any binding exists).
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # §2.5.1: render any reply-context BEFORE the _pending_thread_text stash
    # paths below — otherwise a brand-new-topic flow (where the directory
    # browser holds the text while the user picks a directory) would lose
    # the quote when it eventually flushes via _create_and_bind_window.
    text, has_reply_ctx = await _apply_reply_context(
        update.message, user.id, thread_id, text
    )

    if wid is None:

        def _stash_text(entry: dict[str, Any]) -> None:
            entry["_pending_thread_text"] = text
            # Carry the OBSERVED provenance across the stash so the pending-bind
            # replay is classified the same way a live offer would be
            # (plan §2.3 [r4 P2-1]).
            entry[_PENDING_TEXT_FACTS_KEY] = {
                "typed_text": True,
                "reply_context": has_reply_ctx,
            }

        # GH #65 Fix 5 (+ review r1 P1-2): ``_apply_reply_context`` above and the
        # browser build below are AWAITS a binding or a creation flow can
        # complete inside, so the ownership read and the mutation it authorizes
        # share ONE critical section. This SUBSUMES the pre-#65 mid-picker nudge
        # block that used to run here: reading a DETACHED entry after the
        # reply-context await could nudge (and DISCARD the payload) for a topic
        # whose binding had just landed. A binding now always wins — the payload
        # is delivered — and a live picker still gets its own state-specific
        # nudge. ``stash_on_picker=False`` keeps the pre-#65 behavior that a text
        # message arriving mid-picker is a nudge, not a stash.
        decision = await trust_flow.claim_unbound_inbound(
            user.id,
            thread_id,
            context.user_data,
            session_manager,
            build_browser=_build_browser_payload,
            stash=_stash_text,
            stash_on_picker=False,
        )
        if decision.kind == "trust_owned":
            await safe_reply(update.message, trust_flow.TRUST_NUDGE)
            return
        if decision.kind == "picker_owned":
            await safe_reply(
                update.message,
                trust_flow.PICKER_NUDGES.get(
                    decision.picker_state or "",
                    "Please use the picker above, or tap Cancel.",
                ),
            )
            return
        if decision.kind == "browser":
            # Unbound topic — always the directory browser. If unbound tmux
            # windows exist it includes a "🖥 Bind existing window" opt-in row
            # that pivots to the window picker. We never auto-default to an
            # existing window's cwd, since that locks the user into a directory
            # they didn't choose. The entry was claimed under the lock.
            assert decision.browser is not None
            logger.info(
                "Unbound topic: showing directory browser "
                "(user=%d, thread=%d, unbound=%d)",
                user.id,
                thread_id,
                decision.browser.unbound_count,
            )
            sent = await safe_reply(
                update.message,
                decision.browser.text,
                reply_markup=decision.browser.keyboard,
            )
            _remember_picker_card(decision.entry, sent)
            return
        # The binding appeared while we resolved reply-context / built the
        # browser: deliver THIS payload through the normal bound path below.
        assert decision.window_id is not None
        wid = decision.window_id

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        # /cost overlay cache: the vanished window's cached usage overlay dies
        # with the binding — a later window reusing the id must not inherit it.
        usage_cache.clear_route((user.id, thread_id or 0, wid))
        # B2.3 review fold P2-A: the unbound route's Decision tokens + nav
        # generation die with the binding — a stale dcp:/gate-nav tap must
        # never survive into a window id a later binding may reuse.
        decision_token.teardown_route(user.id, thread_id, wid)
        # Artifact delivery lane: the unbound route's 📎 download cards die with
        # the binding — a stale dlf: tap must never survive into a window id a
        # later binding may reuse.
        artifacts.invalidate_window(wid)
        # P1: the vanished window's post-/exit quarantine dies with the
        # binding — a later window reusing the id must not inherit it.
        tmux_manager.clear_window_quarantine(wid, reason="stale-window unbind")
        # GH #50 peer-review P1: the stranded-draft brake is DELIBERATELY NOT
        # cleared here. ``find_window_by_id`` reads the 1s ``list_windows``
        # cache, so a transient tmux failure reports "gone" for a LIVE window —
        # and this unbind holds no ``window_send_lock``, so dropping the brake
        # would let a send already queued on that lock append to the leftover
        # draft and commit both. A brake entry for a genuinely dead window is
        # inert (``_deliver_locked`` refuses ``window_gone`` before it consults
        # the brake) and is reaped by the real proofs: an empty-box capture, or
        # tmux's own kill_window / create_window seams.
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await _typing_action_best_effort(update.message, thread_id)
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if pane_text and is_interactive_ui(pane_text):
        # UI detected — show it to user, then send text (acts as Enter)
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await handle_interactive_ui(
            context.bot,
            user.id,
            wid,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=session_manager,
        )
        # Small delay to let UI render in Telegram before text arrives
        await asyncio.sleep(0.3)

    # §2.5.2: stash the latest user message_id at the OFFER site (not at the
    # aggregator flush) so the reply_parameters anchor follows the user's
    # most recent visible Telegram message, not whatever the aggregator
    # happens to flush at.
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.8: feed the aggregator instead of sending direct. The reply-context
    # render above still happened; its output flows through the aggregator
    # and lands in Claude as one coherent turn alongside any caption /
    # photo / fast-follow text within the debounce window.
    route = (user.id, thread_id, wid)
    await aggregator_offer_text(
        route, text, bot=context.bot, has_reply_context=has_reply_ctx
    )

    # User just replied → Claude is no longer waiting. Flip the topic-first
    # attention card back to idle so the next idle→waiting transition fires
    # a fresh notification. Fix 3c (judgment call): kind-aware so this user-text
    # seam acks only the interactive_ui card — the notification_decision card
    # dismisses via the route_runtime USER clear → reason=USER → the poller's
    # reason-driven reconcile, NOT this display seam. (Flagged for codex+hermes:
    # the dismiss-audit classed this as a genuine-resolution path that could ack
    # any card; the contract converts it to keep the decision card's dismissal
    # on the single reason-driven channel.)
    await attention.dismiss_if_kind(
        context.bot, user_id=user.id, thread_id=thread_id, kind="interactive_ui"
    )

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(
            context.bot,
            user.id,
            wid,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=session_manager,
        )


# --- Window creation helper ---


async def _cleanup_unbound_created_window(
    window_id: str,
    window_name: str,
    tmux_mgr,
    *,
    reason: str = "SessionStart hook timeout",
) -> bool:
    """Best-effort kill of a newly-created window that should not be bound.

    GH #65 Fix 4: the arbitration itself now lives in
    ``trust_flow.cleanup_created_window`` with a TYPED outcome
    (``killed | spared_bound | spared_registered | kill_failed``) and a declared
    linearization point (the FRESH session-map read). This wrapper preserves the
    pre-#65 ``bool`` contract for every existing caller byte-for-byte: True for
    a kill or either SPARE, False only when the kill itself failed.
    """
    outcome = await trust_flow.cleanup_created_window(
        window_id, window_name, tmux_mgr, reason=reason
    )
    return outcome is not trust_flow.CleanupOutcome.KILL_FAILED


async def _abort_created_window_after_pending_owner_change(
    query: CallbackQuery,
    *,
    user_data: dict | None,
    user_id: int,
    pending_thread_id: int,
    tmux_mgr,
    created_wid: str,
    created_wname: str,
    resume_session_id: str | None,
) -> None:
    """Surface a stale picker after a window was created but before binding."""
    # THE RESERVATION IS HELD ACROSS THE CLEANUP (review r14 P1-E). Releasing it
    # here — before the guarded cleanup below has settled — exposed the window
    # for adoption DURING that cleanup, and the cleanup's own kill then landed
    # on whoever had just taken it. It is released after the cleanup instead, so
    # a release is always coupled to a SETTLED disposition.
    logger.warning(
        "Pending owner changed before binding created window %s "
        "(user=%d, callback_thread=%d, owner_still_present=%s)",
        created_wid,
        user_id,
        pending_thread_id,
        _pending_owner_matches(user_data, pending_thread_id),
    )
    cleanup_note = ""
    show_alert = False
    if resume_session_id is None:
        cleanup_ok = await _cleanup_unbound_created_window(
            created_wid,
            created_wname,
            tmux_mgr,
            reason="pending owner change before bind",
        )
        cleanup_note = (
            " The newly-created tmux window was cleaned up."
            if cleanup_ok
            else (
                f" The newly-created tmux window '{created_wname}' "
                f"({created_wid or 'unknown id'}) could not be cleaned up "
                "automatically; please inspect tmux."
            )
        )
        show_alert = not cleanup_ok
    else:
        cleanup_note = " The resumed tmux window was left unbound."

    # The disposition has now SETTLED (killed, or deliberately left unbound on
    # the resume path), so the reservation may finally be freed.
    trust_flow.release_window_reservation(created_wid)

    await safe_edit(
        query,
        "⚠️ This picker is stale because another topic now owns the pending "
        f"message.{cleanup_note}",
    )
    await safe_answer(query, "Stale picker", show_alert=show_alert)


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    *,
    tmux_mgr: Any,
    session_mgr: Any,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    if pending_thread_id is not None and not _pending_owner_matches(
        context.user_data, pending_thread_id
    ):
        logger.warning(
            "Refusing to create window for stale picker "
            "(user=%d, callback_thread=%d, owner_still_present=%s)",
            user.id,
            pending_thread_id,
            _pending_owner_matches(context.user_data, pending_thread_id),
        )
        await safe_answer(query, "Stale picker", show_alert=True)
        return

    # GH #65 review r2 P1-A: capture the picker entry's IDENTITY TOKEN before
    # anything is created. ``start_trust_wait`` re-validates it under the
    # creation lock, so a teardown that clears this entry (and with it the
    # token) while the window is being created makes the install ABORT rather
    # than land on a replacement entry a fresh inbound may have created.
    creation_entry_token = entry_token(context.user_data, pending_thread_id)

    # GH #65: the creation-flow trust lane covers NON-RESUME creation ONLY. The
    # resume path keeps today's manual-association fallback byte-identical (its
    # 15s timeout, its window_state override, its messaging) — pinned by a
    # resume-timeout parity test. ``lane_enabled()`` is False when
    # ``CC_TELEGRAM_TRUST_PROMPT_CEILING_S=0`` disables the lane, which also
    # restores the pre-#65 path exactly.
    use_trust_lane = (
        resume_session_id is None
        and pending_thread_id is not None
        and trust_flow.lane_enabled()
    )
    success, message, created_wname, created_wid = await tmux_mgr.create_window(
        selected_path,
        resume_session_id=resume_session_id,
        defer_launch=use_trust_lane,
    )
    if not success and created_wid:
        # CREATED-BUT-UNVERIFIED (review r14 P1-C): the window EXISTS even though
        # creation reported failure, so it must be SETTLED rather than lost. It
        # is reserved first — nothing may adopt it while we clean — and the
        # reservation is released only once the guarded cleanup has settled.
        trust_flow.reserve_window(created_wid, creation_entry_token)
        try:
            await trust_flow.cleanup_created_window(
                created_wid,
                created_wname,
                tmux_mgr,
                reason="creation could not be verified",
                session_mgr=session_mgr,
            )
        finally:
            trust_flow.release_window_reservation(created_wid)

    if success:
        # OWNERSHIP BEGINS AT CREATION (GH #65 review r13 P1-C). Registered
        # BEFORE any probe or Telegram await, so the window is never offered as
        # "unbound" during the interval between creation and flow install.
        # Keyed by the entry token, so it dies with the entry.
        trust_flow.reserve_window(created_wid, creation_entry_token)
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
        )
        # THE FIRST POST-RESERVATION OWNER CHECK IS INSIDE THE PROTECTED
        # REGION (review r15 P2-A). It sat BEFORE the ``try``, so a raise or a
        # cancellation in it — or in the abort helper it awaits — left the
        # reservation with no cleanup owner, which is the very gap the
        # try/finally exists to close. It is now the first thing the protected
        # region does.
        if use_trust_lane:
            # THE WHOLE PRE-FLOW PHASE IS COVERED (review r14 P2). Between here
            # and the flow install there are a version probe, a launch and two
            # Telegram edits — any of which can raise or be cancelled. Every
            # NAMED exit already settles the window, but an UNNAMED one (a crash,
            # a cancellation) left the reservation with no cleanup owner: a later
            # ``/start`` would eventually free it, but nothing knew to settle the
            # WINDOW itself. This makes the invariant unconditional — on any exit
            # that did NOT install the flow, run the guarded cleanup and only
            # THEN release the reservation, so a release is always coupled to a
            # settled disposition.
            disposition_settled = False
            try:
                if pending_thread_id is not None and not _pending_owner_matches(
                    context.user_data, pending_thread_id
                ):
                    await _abort_created_window_after_pending_owner_change(
                        query,
                        user_data=context.user_data,
                        user_id=user.id,
                        pending_thread_id=pending_thread_id,
                        tmux_mgr=tmux_mgr,
                        created_wid=created_wid,
                        created_wname=created_wname,
                        resume_session_id=resume_session_id,
                    )
                    disposition_settled = True
                    return
                # GH #65 Fix 0: the window was created in launch-deferred mode, so
                # the pane is a fresh interactive shell. Probe ITS OWN CLI version
                # (nonce-delimited, shell-resolution parity, positive
                # ``(Claude Code)`` proof) and then ALWAYS launch — a probe failure
                # only makes the trust card display-only, it never blocks a launch.
                cli_version = await trust_flow.probe_version_and_launch(
                    created_wid, tmux_mgr
                )
                assert pending_thread_id is not None
                if not _pending_owner_matches(context.user_data, pending_thread_id):
                    await _abort_created_window_after_pending_owner_change(
                        query,
                        user_data=context.user_data,
                        user_id=user.id,
                        pending_thread_id=pending_thread_id,
                        tmux_mgr=tmux_mgr,
                        created_wid=created_wid,
                        created_wname=created_wname,
                        resume_session_id=resume_session_id,
                    )
                    # AFTER the helper returned (review r15 P2-A). Setting it
                    # BEFORE the await meant a cancellation INSIDE the helper
                    # skipped the ``finally``'s recovery — the flag claimed a
                    # settlement that had not happened yet.
                    disposition_settled = True
                    return
                # GH #65 Fix 3: answer the callback + edit the card BEFORE the wait.
                # The ENTIRE wait (including the first hook-timeout phase) runs in
                # the spawned task, which captures stable primitives only — never
                # the CallbackQuery / CallbackContext objects.
                await safe_edit(query, f"🚀 {message}\n\nStarting Claude…")
                await safe_answer(query, "Created")
                card = getattr(query, "message", None)
                flow = await trust_flow.start_trust_wait(
                    bot=context.bot,
                    user_id=user.id,
                    thread_id=pending_thread_id,
                    chat_id=session_mgr.resolve_chat_id(user.id, pending_thread_id),
                    user_data=context.user_data,
                    entry_token=creation_entry_token,
                    card_chat_id=getattr(card, "chat_id", None),
                    card_msg_id=getattr(card, "message_id", None),
                    created_wid=created_wid,
                    window_name=created_wname,
                    selected_path=selected_path,
                    create_message=message,
                    cli_version=cli_version,
                    tmux_mgr=tmux_mgr,
                    session_mgr=session_mgr,
                )
                if flow is None:
                    # GH #65 review r1 P1-1: the topic stopped being claimable
                    # inside the two Telegram awaits above (a concurrent /start or
                    # topic close). Installing an ownerless flow would leave it
                    # UNREACHABLE, so abort through the guarded cleanup instead.
                    await _abort_created_window_after_pending_owner_change(
                        query,
                        user_data=context.user_data,
                        user_id=user.id,
                        pending_thread_id=pending_thread_id,
                        tmux_mgr=tmux_mgr,
                        created_wid=created_wid,
                        created_wname=created_wname,
                        resume_session_id=resume_session_id,
                    )
                    # AFTER the helper returned (review r15 P2-A) — it settles
                    # the window and releases the reservation itself.
                    disposition_settled = True
                else:
                    # The flow OWNS the window now; ownership passed from the
                    # reservation to the flow record inside ``start_trust_wait``.
                    disposition_settled = True
                return
            finally:
                if not disposition_settled:
                    # Best effort, and it must never mask the original failure.
                    try:
                        await trust_flow.cleanup_created_window(
                            created_wid,
                            created_wname,
                            tmux_mgr,
                            reason="creation flow ended before the flow installed",
                            session_mgr=session_mgr,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "pre-flow cleanup failed for window %s", created_wid
                        )
                    finally:
                        # The disposition has settled (or we tried and failed and
                        # logged it); either way the reservation must not outlive
                        # this phase with nobody owning it.
                        trust_flow.release_window_reservation(created_wid)

        # Wait for Claude Code's SessionStart hook to register in session_map.
        # Resume sessions take longer to start (loading session state), so use
        # a longer default. CC_TELEGRAM_HOOK_TIMEOUT (validated in config)
        # OVERRIDES BOTH defaults when set — the stock 5s can be too tight when
        # Claude starts on a slow filesystem (e.g. WSL DrvFs under /mnt/c) or
        # loads several MCP servers and only reaches SessionStart after
        # ~15-20s, which silently dropped the first message on every bind.
        default_hook_timeout = 15.0 if resume_session_id else 5.0
        hook_timeout = (
            config.hook_timeout_override
            if config.hook_timeout_override is not None
            else default_hook_timeout
        )
        hook_ok = await session_mgr.wait_for_session_map_entry(
            created_wid, timeout=hook_timeout
        )

        if pending_thread_id is not None and not _pending_owner_matches(
            context.user_data, pending_thread_id
        ):
            await _abort_created_window_after_pending_owner_change(
                query,
                user_data=context.user_data,
                user_id=user.id,
                pending_thread_id=pending_thread_id,
                tmux_mgr=tmux_mgr,
                created_wid=created_wid,
                created_wname=created_wname,
                resume_session_id=resume_session_id,
            )
            return

        if not hook_ok and not resume_session_id:
            # A brand-new (non-resume) window that never registers in
            # session_map is unmonitored: binding or sending to it would lose
            # the first response. Since this helper just created the window and
            # has not bound it yet, it is safe to clean up by exact window_id.
            logger.warning(
                "Hook timed out for new window %s — cleaning up before binding "
                "(user=%d, thread=%s)",
                created_wid,
                user.id,
                pending_thread_id,
            )
            cleanup_ok = await _cleanup_unbound_created_window(
                created_wid, created_wname, tmux_mgr
            )
            cleanup_note = (
                "The unmonitored tmux window was cleaned up."
                if cleanup_ok
                else (
                    "The hook timeout remains the primary failure, but the "
                    f"unmonitored tmux window '{created_wname}' ({created_wid or 'unknown id'}) "
                    "could not be cleaned up automatically. Please inspect tmux."
                )
            )
            await safe_edit(
                query,
                f"❌ {message}\n\nClaude session didn't register in time. "
                f"{cleanup_note} Send your message again to retry.",
            )
            if context.user_data is not None and _pending_owner_matches(
                context.user_data, pending_thread_id
            ):
                _clear_pending_route_payload(
                    context.user_data, pending_thread_id, delete_files=True
                )
            await safe_answer(
                query,
                "Hook timeout" if cleanup_ok else "Hook timeout; cleanup failed",
                show_alert=not cleanup_ok,
            )
            return

        # --resume creates a new session_id in the hook, but messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the original session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_mgr.get_window_state(created_wid)
            if not hook_ok:
                # Hook timed out — manually populate window_state so the
                # monitor can still route messages back to this topic.
                logger.warning(
                    "Hook timed out for resume window %s, "
                    "manually setting session_id=%s cwd=%s",
                    created_wid,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = created_wname
                session_mgr._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_mgr._save_state()

        if pending_thread_id is not None:
            # Pre-register the new session in the monitor so the first
            # user/assistant exchange isn't dropped by the default
            # end-of-file offset initialization in
            # ``SessionMonitor.check_for_updates``.
            ws = session_mgr.get_window_state(created_wid)
            track_sid = resume_session_id or ws.session_id
            track_cwd = ws.cwd or selected_path

            if not track_sid:
                # Non-resume + hook timeout: we don't know the session_id, so
                # any pending text we send produces a response the monitor
                # cannot route back. Surface the failure instead of silently
                # dropping the first reply.
                logger.warning(
                    "Hook timed out for new window %s — refusing to forward "
                    "pending text since session is unmonitored",
                    created_wid,
                )
                await safe_edit(
                    query,
                    f"❌ {message}\n\nClaude session didn't register in time. "
                    "Send your message again to retry.",
                )
                if context.user_data is not None and _pending_owner_matches(
                    context.user_data, pending_thread_id
                ):
                    _clear_pending_route_payload(
                        context.user_data, pending_thread_id, delete_files=True
                    )
                await safe_answer(query, "Hook timeout")
                return

            # session_monitor lives in bot.py (mutated in post_init); look it
            # up lazily so this extracted helper still sees the current
            # monitor instance after restart / re-init, and so the
            # ``cctelegram.bot`` ↔ ``cctelegram.handlers.inbound_telegram``
            # import edge stays one-directional (lazy import dodges the
            # circular dependency if anything imports inbound_telegram
            # before bot.py finishes loading).
            from cctelegram import bot as _bot_module

            if _bot_module.session_monitor is not None:
                file_path = session_mgr._build_session_file_path(track_sid, track_cwd)
                if file_path is not None:
                    # Resume: skip pre-existing transcript history. New
                    # sessions: read from the start so the seed message and
                    # first reply are picked up.
                    if resume_session_id and file_path.exists():
                        offset = file_path.stat().st_size
                    else:
                        offset = 0
                    _bot_module.session_monitor.register_session(
                        track_sid, file_path, offset=offset
                    )

            if not _pending_owner_matches(context.user_data, pending_thread_id):
                await _abort_created_window_after_pending_owner_change(
                    query,
                    user_data=context.user_data,
                    user_id=user.id,
                    pending_thread_id=pending_thread_id,
                    tmux_mgr=tmux_mgr,
                    created_wid=created_wid,
                    created_wname=created_wname,
                    resume_session_id=resume_session_id,
                )
                return

            # Thread bind flow: bind thread to newly created window.
            #
            # THE THIRD ADOPTION SEAM (GH #65 review r13 P1-C). This is the
            # pre-#65 legacy path — the resume flow, and any creation with the
            # trust lane disabled — and it bound with NO lifecycle lock, no
            # fresh existence probe and no exclusivity check, across a hook wait
            # of many seconds. It now runs the SAME revalidate→commit protocol
            # as the other two seams. User-visible messaging on the success path
            # is unchanged (the resume-parity pin stays green); only the new
            # refusal arm is added.
            bind_refusal: str | None = None
            try:
                async with tmux_mgr.window_lifecycle_lock():
                    # ABSENCE MUST BE PROVEN, NOT INFERRED — the same rule the
                    # create-verification uses (review r11 P1-A). A bare None
                    # also means "the listing failed", and refusing on that
                    # would break the legacy path whenever enumeration hiccups.
                    # Only a listing that WORKED and lacks our window proves it
                    # is gone.
                    # FRESH (review r14 P1-F). ``list_windows`` reads the 1 s
                    # cache, and this path's own creation-verification warms it
                    # — so a window killed right afterwards stayed "present" for
                    # the rest of the TTL and the legacy seam bound a corpse.
                    # Every adoption probe must bypass the cache.
                    listed = await tmux_mgr._bounded_lifecycle(
                        tmux_mgr.adoption_listing(),
                        what="legacy bind existence probe",
                    )
                    proven_absent = bool(listed) and not any(
                        w.window_id == created_wid for w in listed
                    )
                    if proven_absent:
                        bind_refusal = (
                            "The new window disappeared before it could be "
                            "bound. Please try again."
                        )
                    # The pending check runs AFTER the listing (review r15 P1-A):
                    # a kill that registered while we were listing must refuse,
                    # and checking before the read would miss it.
                    elif tmux_mgr.window_kill_pending(created_wid):
                        bind_refusal = (
                            "That window is being closed right now. Please try "
                            "again in a moment."
                        )
                    else:
                        taken_by = [
                            (uid, tid)
                            for uid, tid, wid in session_mgr.iter_thread_bindings()
                            if wid == created_wid
                            and (uid, tid) != (user.id, pending_thread_id)
                        ]
                        if taken_by:
                            bind_refusal = (
                                "Another topic just claimed that window. Please "
                                "try again."
                            )
                        else:
                            session_mgr.bind_thread(
                                user.id,
                                pending_thread_id,
                                created_wid,
                                window_name=created_wname,
                            )
            except LifecycleTimeout as e:
                logger.error("legacy bind exceeded its lifecycle bound: %s", e)
                bind_refusal = (
                    "Binding the window took too long. Please check tmux and try again."
                )
            if bind_refusal is not None:
                # SETTLE BEFORE RELEASING (r14 self-audit, same class as P1-E).
                # The bind did NOT happen, so the window is alive and unowned —
                # releasing the reservation here without settling it would leave
                # exactly the orphan the reservation exists to prevent. The
                # guarded cleanup is the right seam: if the refusal was "another
                # topic claimed it", that topic's binding makes this a
                # SPARED_BOUND and the window is correctly left alone.
                try:
                    await trust_flow.cleanup_created_window(
                        created_wid,
                        created_wname,
                        tmux_mgr,
                        reason="legacy bind refused",
                        session_mgr=session_mgr,
                    )
                finally:
                    trust_flow.release_window_reservation(created_wid)
                await safe_edit(query, f"⚠️ {bind_refusal}")
                await safe_answer(query, "Could not bind the window", show_alert=True)
                return
            # BOUND is itself a settled disposition — the flow's window now has
            # a real owner, so the reservation has done its job.
            trust_flow.release_window_reservation(created_wid)

            status = "Resumed" if resume_session_id else "Created"

            # Replay pending text and/or attachments through the synchronous
            # aggregator helper so §2.8.2 formatting is preserved without
            # offer-path background/intermediate flushes hiding failures.
            route = (user.id, pending_thread_id, created_wid)
            pending_delivered = await _flush_pending_route_payload(
                route, context.user_data
            )
            if pending_delivered is not None and not pending_delivered.ok:
                # GH #50 §1.4: surface the REAL refusal reason — a brand-new
                # window's very first turn lands on the folder-trust prompt.
                await safe_edit(
                    query,
                    f"✅ {message}\n\n{status}, but the first message was not "
                    f"delivered.\n\n⚠️ {pending_delivered.message}\n\n"
                    "The pending payload was cleared; please resend it here.",
                )
                await safe_answer(
                    query, f"{status}; first message not delivered", show_alert=True
                )
                return

            first_turn_note = (
                " First message sent."
                if pending_delivered is not None and pending_delivered.ok
                else ""
            )
            await safe_edit(
                query,
                f"✅ {message}\n\n{status}.{first_turn_note} Send messages here.",
            )
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if (
            pending_thread_id is not None
            and context.user_data is not None
            and _pending_owner_matches(context.user_data, pending_thread_id)
        ):
            _clear_pending_route_payload(
                context.user_data, pending_thread_id, delete_files=True
            )
    await safe_answer(query, "Created" if success else "Failed")
