"""GH #65 — the folder-trust creation-flow lane (Wave 3 / 3B).

A brand-new Claude Code window opens on "Do you trust the files in this
folder?", which never reaches JSONL and never registers in ``session_map``. The
pre-#65 creation flow read that as "the SessionStart hook timed out", KILLED the
window and told the user their session failed. This module replaces that kill
with a CLASSIFYING wait: while the pane positively shows the folder-trust
prompt, the picker card becomes a 🔐 trust card whose [✅ Trust this folder]
button navigates→verifies→Enter the live pane (the shipped ``dcp:`` dispatch
DISCIPLINE) and whose [❌ Cancel] kills the window WITHOUT any keystroke.

Core responsibilities:
  - ``start_trust_wait`` — spawn the per-creation WAIT task and own its
    terminalizer (guarded cleanup / spare-and-release on EVERY exit path).
  - The flow registry + the per-``(user, thread)`` creation lock: the state
    machine ``awaiting_trust → dispatching → awaiting_registration |
    cancelling | completing_bind → terminal (entry dropped)``. The picker ENTRY
    is the ownership token; the flow record is the task's working set.
  - ``probe_version_and_launch`` — Fix 0's per-creation, in-pane, nonce-
    delimited ``--version`` probe, followed UNCONDITIONALLY by the launch.
  - ``classify_slice`` — the pure slice classifier (registration-map first,
    pane COMMAND before any pane TEXT, blank ⇒ indeterminate).
  - ``cleanup_created_window`` — the typed cleanup arbitration whose FRESH
    session-map read is the declared linearization point.
  - ``claim_unbound_inbound`` — Fix 5's ONE-critical-section decide-and-mutate
    for the three unbound-topic inbound handlers.
  - ``teardown_thread`` / ``shutdown`` — Fix 6's reachable teardown with the
    normative lock choreography.

Pull-only; no observers. ``route_runtime`` / ``message_queue`` / the
interactive-surface store are untouched — the trust card IS the picker card.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Final, Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..markdown_v2 import convert_markdown
from ..session import (
    peek_session_id_for_window,
    read_session_id_for_window_fresh,
    session_manager,
)
from ..terminal_parser import (
    decision_prompt_fingerprint,
    extract_interactive_content,
    parse_generic_decision,
)
from ..tmux_manager import pane_command_is_claude, pane_command_is_shell
from . import auq_ledger, decision_token
from .callback_data import CB_TRUST_PICK, checked_callback_data
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_UNBOUND_COUNT_KEY,
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    drop_picker_entry,
    ensure_picker_entry,
    picker_entry,
)

logger = logging.getLogger(__name__)


def _wall() -> float:
    """The lane's monotonic clock (one seam, so tests can reason about it)."""
    return time.monotonic()


# The creation-flow picker state. Present in the thread's picker entry for
# exactly as long as this lane OWNS the topic; every unbound-topic inbound
# handler treats it as owned state (Fix 5) and no browser rebuild may race it.
STATE_AWAITING_TRUST: Final[str] = "awaiting_trust"

# Entry keys (the ownership token's payload — the flow record holds the rest).
TRUST_GENERATION_KEY: Final[str] = "_trust_generation"
TRUST_WID_KEY: Final[str] = "_trust_window_id"
TRUST_CLI_VERSION_KEY: Final[str] = "_trust_cli_version"

# The family this lane exists for. A pane that parses as ANY other Decision
# family is "another blocking surface", never a trust card.
TRUST_FAMILY: Final[str] = "folder-trust"

# Loop cadence. Module-level so the test conftest can shrink them without any
# test body reaching into handler internals (the ``_fast_delivery_settles``
# precedent).
SLICE_S: float = 0.5
PANE_POLL_EVERY_S: float = 2.0
# Added on top of ``max(registration budget, trust ceiling)`` for the GLOBAL
# observation ceiling — the terminal bound for a pane that stays INDETERMINATE.
GLOBAL_CEILING_MARGIN_S: float = 30.0
# A trust frame still standing this long after a dispatch SENT Enter demotes
# ``awaiting_registration`` back to ``awaiting_trust`` (the Enter did not
# commit) and re-renders the card.
TRUST_SETTLE_MARGIN_S: float = 5.0
# Token deadlines are re-stamped per slice while the card is live (the D3-β
# analogue — the poller never sees this lane, so the lane's own task does it).
TOKEN_REFRESH_MIN_REMAINING_S: float = 120.0
# Pane tail length surfaced in the honest diagnostic card edits.
PANE_TAIL_LINES: Final[int] = 12


# ── Phases ───────────────────────────────────────────────────────────────────

PHASE_AWAITING_TRUST: Final[str] = "awaiting_trust"
PHASE_DISPATCHING: Final[str] = "dispatching"
PHASE_AWAITING_REGISTRATION: Final[str] = "awaiting_registration"
PHASE_CANCELLING: Final[str] = "cancelling"
PHASE_COMPLETING_BIND: Final[str] = "completing_bind"

# Every NONTERMINAL phase is OWNED state: it blocks a directory-browser rebuild
# and refuses a second creation flow for the topic.
NONTERMINAL_PHASES: Final[frozenset[str]] = frozenset(
    {
        PHASE_AWAITING_TRUST,
        PHASE_DISPATCHING,
        PHASE_AWAITING_REGISTRATION,
        PHASE_CANCELLING,
        PHASE_COMPLETING_BIND,
    }
)

_PICKER_CHROME_STATES: Final[tuple[str, ...]] = (
    STATE_BROWSING_DIRECTORY,
    STATE_SELECTING_WINDOW,
    STATE_SELECTING_SESSION,
)


class SliceKind(Enum):
    """The classification of ONE creation-wait slice (pure, fail-closed)."""

    REGISTERED = "registered"
    SHELL = "shell"
    TRUST_FRAME = "trust_frame"
    OTHER_SURFACE = "other_surface"
    RUNNING = "running"
    INDETERMINATE = "indeterminate"


class CleanupOutcome(Enum):
    """Typed, exhaustive outcome of the guarded created-window cleanup."""

    KILLED = "killed"
    SPARED_BOUND = "spared_bound"
    SPARED_REGISTERED = "spared_registered"
    KILL_FAILED = "kill_failed"


@dataclass
class TrustFlow:
    """The WAIT task's working set for ONE creation flow.

    ``user_data`` is the PTB per-user ``user_data`` MAPPING REFERENCE, held
    DELIBERATELY: the object is stable for the process's lifetime in PTB and is
    exactly what ``_flush_pending_route_payload(route, user_data)`` requires at
    bind time. Holding the mapping is NOT holding a ``CallbackContext`` (which
    is per-update and must never be captured by a long-lived task).
    """

    generation: int
    user_id: int
    thread_id: int
    chat_id: int | None
    card_chat_id: int | None
    card_msg_id: int | None
    created_wid: str
    window_name: str
    selected_path: str
    create_message: str
    resume_id: None
    cli_version: str | None
    user_data: dict[str, Any] | None
    phase: str = PHASE_AWAITING_TRUST
    # Minted trust-button identity (Fix 7's retained row identifiers).
    fingerprint: str | None = None
    ledger_key: str | None = None
    token: str | None = None
    # Budgets (monotonic deadlines).
    started_at: float = 0.0
    registration_deadline: float = 0.0
    trust_deadline: float | None = None
    global_deadline: float = 0.0
    enter_sent_at: float | None = None
    # When the flow ENTERED ``awaiting_registration`` — stamped on BOTH entry
    # paths (a dispatch that sent Enter, AND the manual in-tmux answer). The
    # demotion keys on THIS, not on ``enter_sent_at``: keying on the dispatch
    # stamp made the demotion unreachable for the manual path (review r1 P1-3),
    # stranding a flow in ``awaiting_registration`` with a dead card whenever
    # the user's own answer did not take.
    awaiting_registration_at: float | None = None
    trust_seen: bool = False
    terminal_committed: bool = False
    wait_task: asyncio.Task[None] | None = None
    bind_task: asyncio.Task[None] | None = field(default=None)


_FlowKey = tuple[int, int]

_flows: dict[_FlowKey, TrustFlow] = {}
# RETAINED per-(user, thread) creation locks: the registry is bounded by real
# user×thread cardinality, and evicting one with a live waiter is the bug.
# Generation validation ALWAYS runs AFTER acquisition.
_locks: dict[_FlowKey, asyncio.Lock] = {}
_generation_counter: int = 0


def creation_lock(user_id: int, thread_id: int) -> asyncio.Lock:
    """The retained per-``(user, thread)`` creation-flow lock."""
    key = (user_id, thread_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _next_generation() -> int:
    global _generation_counter
    _generation_counter += 1
    return _generation_counter


def get_flow(user_id: int, thread_id: int) -> TrustFlow | None:
    """The live flow record for this topic, or None."""
    return _flows.get((user_id, thread_id))


def flow_owner_for_thread(thread_id: int | None) -> int | None:
    """The user_id owning a live creation flow in ``thread_id``, or None.

    The ``tst:`` card lives in a shared forum topic, so ANOTHER allowed user can
    tap it. Their PTB ``user_data`` is a different dict, so an ownership check
    keyed on the entry alone would read "expired" and EDIT the owner's card
    (review r1 P2-4). This gives the callback entry a positive owner to compare
    against BEFORE it touches any state.
    """
    if thread_id is None:
        return None
    for (uid, tid), flow in _flows.items():
        if tid == thread_id and flow.phase in NONTERMINAL_PHASES:
            return uid
    return None


def flow_task(user_id: int, thread_id: int) -> asyncio.Task[None] | None:
    """The topic's WAIT task (public so teardown + tests can join it)."""
    flow = _flows.get((user_id, thread_id))
    return flow.wait_task if flow is not None else None


def lane_enabled() -> bool:
    """False when ``CC_TELEGRAM_TRUST_PROMPT_CEILING_S=0`` disables the lane."""
    return config.trust_prompt_ceiling_s > 0


def reset_for_tests() -> None:
    """Drop every flow record + lock (co-located reset seam)."""
    global _generation_counter
    for flow in list(_flows.values()):
        for task in (flow.wait_task, flow.bind_task):
            if task is not None and not task.done():
                task.cancel()
    _flows.clear()
    _locks.clear()
    _generation_counter = 0


# ── Fix 0: the per-creation in-pane version probe ────────────────────────────


async def probe_version_and_launch(
    window_id: str,
    tmux_mgr: Any,
    *,
    resume_session_id: str | None = None,
) -> str | None:
    """Probe the pane's OWN CLI version, then ALWAYS launch Claude in it.

    Fix 0. The window was created in ``defer_launch`` mode, so the pane is a
    fresh interactive shell nothing else has typed into. A probe failure of ANY
    kind (un-extractable binary, send failure, timeout, no nonce-delimited
    match, a wrapper failing the ``(Claude Code)`` proof) returns ``None`` ⇒ the
    trust card is DISPLAY-ONLY — it NEVER blocks or delays the launch beyond the
    probe's own bounded budget, and never raises.
    """
    version: str | None = None
    try:
        version = await tmux_mgr.probe_cli_version(window_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("trust version probe failed (window=%s): %s", window_id, e)
    try:
        launched = await tmux_mgr.launch_claude_in_window(
            window_id, resume_session_id=resume_session_id
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("deferred launch raised (window=%s): %s", window_id, e)
        return version
    if not launched:
        logger.error("deferred launch send failed (window=%s)", window_id)
    return version


# ── Fix 1: the pure slice classifier ─────────────────────────────────────────


def classify_slice(
    *,
    registered: bool,
    pane_command: str | None,
    pane_text: str | None,
) -> SliceKind:
    """Classify one creation-wait slice. Pure; fail-closed; ORDER IS NORMATIVE.

    1. **REGISTRATION first** — a registered session flips the flow to the
       completion tail even if the pane has already returned to a shell.
    2. **Pane COMMAND before any pane TEXT** (Phase-0 addendum item 2): a dead
       pane RETAINS the trust-prompt text after a digit commit or Esc, so a
       text-only match false-positives on a corpse. ``pane_command_is_shell``
       positive ⇒ SHELL (guarded cleanup) BEFORE any text is looked at.
    3. A command that is neither claude nor shell (e.g. the npm launcher named
       ``claude.exe``, which ``pane_command_is_claude`` rejects) ⇒ INDETERMINATE
       even with trust text visible: no card, no kill — the flow runs to the
       global observation ceiling's SPARE. Widening ``pane_command_is_claude``
       is a SEPARATE issue, not this lane's.
    4. A blank / missing capture under a live claude ⇒ INDETERMINATE (addendum
       item 3: the post-Enter transitional frame is all-blank on 2.1.241),
       NEVER a failure.
    """
    if registered:
        return SliceKind.REGISTERED
    if pane_command_is_shell(pane_command):
        return SliceKind.SHELL
    if not pane_command_is_claude(pane_command):
        return SliceKind.INDETERMINATE
    if not pane_text or not pane_text.strip():
        return SliceKind.INDETERMINATE
    form = parse_generic_decision(pane_text)
    if form is not None and decision_token.identify_family(form) == TRUST_FAMILY:
        return SliceKind.TRUST_FRAME
    if extract_interactive_content(pane_text) is not None:
        return SliceKind.OTHER_SURFACE
    return SliceKind.RUNNING


def pane_tail(pane_text: str | None, lines: int = PANE_TAIL_LINES) -> str:
    """The last non-blank ``lines`` of a capture, for the honest failure copy."""
    if not pane_text:
        return ""
    rows = [row.rstrip() for row in pane_text.splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


# ── Fix 4: typed cleanup arbitration ─────────────────────────────────────────


async def cleanup_created_window(
    window_id: str,
    window_name: str,
    tmux_mgr: Any,
    *,
    reason: str,
) -> CleanupOutcome:
    """Guarded kill of a created-but-unbound window, with a TYPED outcome.

    The **FRESH session-map read is the declared LINEARIZATION POINT** (plan
    Fix 4): a registration observed at or before that read WINS (the caller
    flips into the completion tail); one that lands AFTER it and before the tmux
    kill LOSES — the window dies and the orphaned ``session_map`` entry is
    reaped by the existing startup/poll map sweeps. Holding a cross-process lock
    over the hook writer is out of proportion, so "registration always wins" is
    NOT claimed. Both proofs are read SYNCHRONOUSLY with no ``await`` between
    them and the kill: the Wave-2 bound-OR-registered guard is preserved and
    STRENGTHENED (the cached peek can lag; the fresh read cannot).

    GH #63 §2b (Codex Q1), carried forward verbatim in intent: NEVER kill a
    window that holds a live/won session. The bring-up ordering is
    register(session_map) → pending-owner recheck → BIND, so a WINNER can be
    REGISTERED and already running a live Claude in its pane during the gap
    BEFORE it is bound. Spare on EITHER proof — BOUND (it appears in the
    authoritative ``thread_bindings``, the same source ``clear_topic_state``
    consults) or REGISTERED. "Registered" cannot distinguish a winner from a
    superseded loser, so this guard deliberately takes the SAFE direction: it
    may SPARE a registered-but-superseded LOSER (leaking that unbound tmux
    window — a rare, minor resource leak a future janitor could reap) in order
    to NEVER kill a registered WINNER (a live session, the actual §2b bug). The
    loser-leak is an ACCEPTED residual.
    """
    if not window_id:
        logger.error(
            "Cannot clean up unbound tmux window '%s' after %s: no window_id",
            window_name,
            reason,
        )
        return CleanupOutcome.KILL_FAILED
    is_bound = window_id in {
        wid for _, _, wid in session_manager.iter_thread_bindings()
    }
    if is_bound:
        logger.warning(
            "Skipping cleanup of tmux window %s (%s) after %s: it is BOUND",
            window_id,
            window_name,
            reason,
        )
        return CleanupOutcome.SPARED_BOUND
    # ``peek`` is kept as the pre-#65 (cached) proof so the guard is a strict
    # superset of Wave-2's; the FRESH read follows it UNCONDITIONALLY and is the
    # LAST observation before the kill (review r1 P2-1 — a short-circuiting
    # ``peek(...) or fresh(...)`` would skip the declared linearization point
    # exactly when the cache happens to be warm). Neither read awaits, so
    # nothing can interleave between them and the tmux call.
    cached_sid = peek_session_id_for_window(window_id)
    fresh_sid = read_session_id_for_window_fresh(window_id)
    is_registered = cached_sid is not None or fresh_sid is not None
    if is_registered:
        logger.warning(
            "Skipping cleanup of tmux window %s (%s) after %s: it holds a "
            "REGISTERED session (won or live) — not collateral",
            window_id,
            window_name,
            reason,
        )
        return CleanupOutcome.SPARED_REGISTERED
    try:
        killed = await tmux_mgr.kill_window(window_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Failed to clean up unbound tmux window %s (%s) after %s: %s",
            window_id,
            window_name,
            reason,
            e,
        )
        return CleanupOutcome.KILL_FAILED
    if killed:
        logger.warning(
            "Cleaned up unbound tmux window %s (%s) after %s",
            window_id,
            window_name,
            reason,
        )
        return CleanupOutcome.KILLED
    logger.error(
        "Could not clean up unbound tmux window %s (%s) after %s",
        window_id,
        window_name,
        reason,
    )
    return CleanupOutcome.KILL_FAILED


def cleanup_note(outcome: CleanupOutcome, window_name: str, window_id: str) -> str:
    """The honest per-outcome sentence appended to a failure card edit."""
    if outcome is CleanupOutcome.KILLED:
        return "The unmonitored tmux window was cleaned up."
    if outcome is CleanupOutcome.SPARED_BOUND:
        return "The tmux window was left alone — it is already bound."
    if outcome is CleanupOutcome.SPARED_REGISTERED:
        return "The tmux window was left alone — a live session registered in it."
    return (
        f"I couldn't close the tmux window '{window_name}' "
        f"({window_id or 'unknown id'}) — please check tmux."
    )


# ── Card rendering ───────────────────────────────────────────────────────────


async def _edit_card(
    flow: TrustFlow, bot: Any, text: str, keyboard: Any = None
) -> None:
    """Edit the picker card in place (MarkdownV2 with a plain-text fallback).

    Internal UI code calls the bot API directly with its own fallback (the
    repo's documented pattern for queue/UI surfaces). Best-effort: a failed edit
    NEVER blocks a teardown or a cleanup.
    """
    if bot is None or not flow.card_chat_id or not flow.card_msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=flow.card_chat_id,
            message_id=flow.card_msg_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        await bot.edit_message_text(
            chat_id=flow.card_chat_id,
            message_id=flow.card_msg_id,
            text=text,
            reply_markup=keyboard,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("trust card edit failed (window=%s): %s", flow.created_wid, e)


async def edit_card_public(flow: TrustFlow, bot: Any, text: str) -> None:
    """Public keyboard-less card edit (the ``tst:`` lane's post-dispatch copy)."""
    await _edit_card(flow, bot, text, None)


def build_trust_card(
    flow: TrustFlow, *, trust_token: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """The 🔐 trust card body + keyboard.

    ``trust_token`` present ⇒ the LICENSED shape with a one-tap
    [✅ Trust this folder]; ``None`` ⇒ display-only (flag off, explicit operator
    kill switch, probe ``None``, or an un-characterized CC version) with an
    advisory to answer in tmux. [❌ Cancel] is present in BOTH shapes and never
    types a keystroke.
    """
    ceiling_min = max(1, int(config.trust_prompt_ceiling_s // 60))
    lines = [
        "🔐 *Claude is asking you to trust this folder*",
        "",
        f"`{flow.selected_path}`",
        "",
        "Claude Code will be able to read, edit, and execute files here.",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    if trust_token is not None:
        lines.append("")
        lines.append(
            f"Tap ✅ and I'll answer the prompt for you, then bind this topic. "
            f"I'll keep the window open for up to ~{ceiling_min} min."
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Trust this folder",
                    callback_data=checked_callback_data(
                        f"{CB_TRUST_PICK}t:{trust_token}"
                    ),
                )
            ]
        )
    else:
        lines.append("")
        lines.append(
            "Answer it in the tmux window — I'll bind this topic as soon as you "
            f"do. I'll keep the window open for up to ~{ceiling_min} min."
        )
    buttons.append(
        [
            InlineKeyboardButton(
                "❌ Cancel — close the window",
                callback_data=checked_callback_data(
                    f"{CB_TRUST_PICK}c:{flow.generation}"
                ),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def render_trust_card(flow: TrustFlow, bot: Any, pane_text: str | None) -> None:
    """Mint (when licensed) and publish/refresh the trust card.

    The mint is the FIRST of the two gates on the trust flag + the explicit
    operator kill switch (the callback entry is the second). A mint requires,
    additionally: a strict ``parse_generic_decision``, the ``folder-trust``
    family, a clean single-select geometry, and the PROBED version licensed in
    the ONE ``_DECISION_DISPATCH_TABLE``.
    """
    token: str | None = None
    form = parse_generic_decision(pane_text) if pane_text else None
    if (
        form is not None
        and decision_token.trust_card_dispatch_enabled()
        and flow.cli_version
        and decision_token.identify_family(form) == TRUST_FAMILY
        and decision_token.lookup(TRUST_FAMILY, flow.cli_version)
        and sum(1 for o in form.options if o.cursor) == 1
        and not any(o.selected is not None for o in form.options)
        and form.options_contiguous_from_one()
        and form.select_mode == "single"
    ):
        target = next(
            (
                o
                for o in form.options
                if o.number == 1 and o.label and o.label.lower().startswith("yes")
            ),
            None,
        )
        if target is not None and target.number is not None:
            fingerprint = decision_prompt_fingerprint(form)
            tokens = decision_token.mint_row(
                user_id=flow.user_id,
                thread_id=flow.thread_id,
                window_id=flow.created_wid,
                fingerprint=fingerprint,
                specs=[
                    decision_token.DecisionMintSpec(
                        option_number=target.number, option_label=target.label
                    )
                ],
            )
            token = tokens[0]
            flow.fingerprint = fingerprint
            flow.token = token
            flow.ledger_key = auq_ledger.make_ledger_key(
                auq_ledger.make_route_hash(
                    flow.user_id, flow.thread_id, flow.created_wid
                ),
                fingerprint[:8],
                target.number,
            )
    if token is None:
        flow.token = None
        flow.fingerprint = None
        flow.ledger_key = None
    text, keyboard = build_trust_card(flow, trust_token=token)
    await _edit_card(flow, bot, text, keyboard)


# ── Fix 3 / Fix 7: terminal transitions ──────────────────────────────────────


def _release_tokens(flow: TrustFlow) -> None:
    """Fix 7's non-dispatch terminal token op: ROW REMOVAL, ledger untouched.

    Disclosed residue: a token consumption tombstones its row BEFORE the
    dispatch runs, and the busy-lock ``accepted → not_advanced`` downgrade fires
    only when the lock is OBSERVED busy — so an exception/cancellation landing
    after the ``accepted`` write can leave an ``accepted`` row that later reads
    as ``unknown`` (refresh-only). Fail-closed: no redispatch can result.
    """
    decision_token.teardown_route(flow.user_id, flow.thread_id, flow.created_wid)


def _drop_entry(flow: TrustFlow) -> None:
    """Drop the ownership token LAST (never before the window is settled)."""
    entry = picker_entry(flow.user_data, flow.thread_id)
    if entry is not None and entry.get(TRUST_GENERATION_KEY) == flow.generation:
        drop_picker_entry(flow.user_data, flow.thread_id)


def _drop_flow(flow: TrustFlow) -> None:
    key = (flow.user_id, flow.thread_id)
    if _flows.get(key) is flow:
        _flows.pop(key, None)


async def _terminal_cleanup(
    flow: TrustFlow,
    bot: Any,
    tmux_mgr: Any,
    *,
    reason: str,
    body: str,
) -> CleanupOutcome:
    """Guarded cleanup + honest card edit + token teardown. Caller drops entry."""
    outcome = await cleanup_created_window(
        flow.created_wid, flow.window_name, tmux_mgr, reason=reason
    )
    _release_tokens(flow)
    note = cleanup_note(outcome, flow.window_name, flow.created_wid)
    await _edit_card(flow, bot, f"{body}\n\n{note}", None)
    flow.terminal_committed = True
    return outcome


async def _terminal_spare(flow: TrustFlow, bot: Any, *, body: str) -> None:
    """The GLOBAL observation ceiling's terminal action: SPARE + release.

    NEVER kills. The window stays alive, creation ownership is released (entry +
    tokens), and the card carries recovery copy — fail-open preserved, and no
    permanent ownership of the topic.
    """
    _release_tokens(flow)
    await _edit_card(flow, bot, body, None)
    flow.terminal_committed = True


# ── Fix 3: the completion tail ───────────────────────────────────────────────


async def _complete_bind(flow: TrustFlow, bot: Any, session_mgr: Any) -> None:
    """The EXACT tail of today's creation flow, run from the WAIT task.

    Owner recheck → monitor pre-register → ``bind_thread`` → pending-payload
    replay → final card edit. Runs as a SEPARATELY-TRACKED inner task that
    teardown AWAITS (never cancels) so a half-bound topic can never be stranded.
    """
    from .inbound_telegram import _flush_pending_route_payload

    entry = picker_entry(flow.user_data, flow.thread_id)
    if entry is None or entry.get(TRUST_GENERATION_KEY) != flow.generation:
        logger.warning(
            "Trust flow lost ownership before bind (window=%s, thread=%s)",
            flow.created_wid,
            flow.thread_id,
        )
        await _edit_card(
            flow,
            bot,
            "⚠️ This picker is stale — another flow now owns this topic. "
            f"The tmux window '{flow.window_name}' was left unbound.",
            None,
        )
        flow.terminal_committed = True
        return

    ws = session_mgr.get_window_state(flow.created_wid)
    track_sid = ws.session_id
    track_cwd = ws.cwd or flow.selected_path
    if track_sid:
        from cctelegram import bot as _bot_module

        if _bot_module.session_monitor is not None:
            file_path = session_mgr._build_session_file_path(track_sid, track_cwd)
            if file_path is not None:
                _bot_module.session_monitor.register_session(
                    track_sid, file_path, offset=0
                )

    session_mgr.bind_thread(
        flow.user_id, flow.thread_id, flow.created_wid, window_name=flow.window_name
    )
    _release_tokens(flow)

    route = (flow.user_id, flow.thread_id, flow.created_wid)
    pending = await _flush_pending_route_payload(route, flow.user_data)
    if pending is not None and not pending.ok:
        await _edit_card(
            flow,
            bot,
            f"✅ {flow.create_message}\n\nCreated, but the first message was not "
            f"delivered.\n\n⚠️ {pending.message}\n\n"
            "The pending payload was cleared; please resend it here.",
            None,
        )
    else:
        note = " First message sent." if pending is not None and pending.ok else ""
        await _edit_card(
            flow,
            bot,
            f"✅ {flow.create_message}\n\nCreated.{note} Send messages here.",
            None,
        )
    flow.terminal_committed = True


# ── Fix 1 + Fix 3: the WAIT task ─────────────────────────────────────────────


def _rebase_registration_budget(flow: TrustFlow, *, reason: str) -> None:
    """Grant a FRESH full registration budget.

    Fires when a dispatch transaction SENT Enter (``dispatched`` OR
    ``commit_unconfirmed`` — addendum r1 P1: both end the human trust wait in
    wall-clock terms) and when the trust prompt disappears after a manual
    in-tmux answer. Human wait time must never consume the machine's budget.
    """
    flow.registration_deadline = time.monotonic() + registration_budget_s()
    logger.info(
        "trust flow registration budget rebased (%s) window=%s",
        reason,
        flow.created_wid,
    )


def registration_budget_s(resume: bool = False) -> float:
    """``hook_timeout`` + one ``CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S``."""
    default_hook_timeout = 15.0 if resume else 5.0
    hook_timeout = (
        config.hook_timeout_override
        if config.hook_timeout_override is not None
        else default_hook_timeout
    )
    return hook_timeout + config.hook_timeout_extension_s


def note_dispatch_enter_sent(flow: TrustFlow) -> None:
    """Called by the ``tst:`` lane after a transaction that SENT Enter."""
    flow.enter_sent_at = _wall()
    flow.awaiting_registration_at = _wall()
    flow.trust_deadline = None
    _rebase_registration_budget(flow, reason="trust dispatch sent Enter")


async def _wait_loop(
    flow: TrustFlow, bot: Any, tmux_mgr: Any, session_mgr: Any
) -> None:
    """The classifying wait. NON-RESUME creation only."""
    last_pane_poll = 0.0
    pane_text: str | None = None
    pane_command: str | None = None
    while True:
        registered = await session_mgr.wait_for_session_map_entry(
            flow.created_wid, timeout=SLICE_S, interval=SLICE_S
        )
        now = time.monotonic()
        if not registered and now - last_pane_poll >= PANE_POLL_EVERY_S:
            last_pane_poll = now
            pane_command = await tmux_mgr.pane_current_command(flow.created_wid)
            pane_text = await tmux_mgr.capture_pane(flow.created_wid)
        kind = classify_slice(
            registered=registered,
            pane_command=pane_command,
            pane_text=pane_text,
        )

        if kind is SliceKind.REGISTERED:
            async with creation_lock(flow.user_id, flow.thread_id):
                if _flows.get((flow.user_id, flow.thread_id)) is not flow:
                    return
                flow.phase = PHASE_COMPLETING_BIND
                inner = asyncio.create_task(_complete_bind(flow, bot, session_mgr))
                flow.bind_task = inner
            await asyncio.shield(inner)
            return

        if kind is SliceKind.SHELL:
            async with creation_lock(flow.user_id, flow.thread_id):
                if _flows.get((flow.user_id, flow.thread_id)) is not flow:
                    return
                flow.phase = PHASE_CANCELLING
            tail = pane_tail(pane_text)
            body = (
                "❌ Claude exited before the session started.\n\n"
                "Send your message again to retry."
            )
            if tail:
                body += f"\n\n```\n{tail}\n```"
            await _terminal_cleanup(
                flow, bot, tmux_mgr, reason="pane returned to a shell", body=body
            )
            return

        if kind is SliceKind.TRUST_FRAME:
            if not lane_enabled():
                await _terminal_cleanup(
                    flow,
                    bot,
                    tmux_mgr,
                    reason="trust lane disabled",
                    body=(
                        "❌ Claude is asking you to trust this folder, and the "
                        "trust card is disabled on this deployment.\n\n"
                        "Trust the folder in tmux, then send your message again."
                    ),
                )
                return
            if not flow.trust_seen:
                flow.trust_seen = True
                flow.trust_deadline = time.monotonic() + config.trust_prompt_ceiling_s
                await render_trust_card(flow, bot, pane_text)
            elif (
                flow.phase == PHASE_AWAITING_REGISTRATION
                and flow.awaiting_registration_at is not None
                and _wall() - flow.awaiting_registration_at > TRUST_SETTLE_MARGIN_S
            ):
                # The DOCUMENTED demotion: the trust frame is STILL standing
                # past the settle margin, so whatever moved us to
                # ``awaiting_registration`` — our Enter, or the user's own
                # answer in tmux — did not in fact commit. Fresh render, fresh
                # tokens, back to ``awaiting_trust``.
                flow.phase = PHASE_AWAITING_TRUST
                flow.enter_sent_at = None
                flow.awaiting_registration_at = None
                flow.trust_deadline = _wall() + config.trust_prompt_ceiling_s
                await render_trust_card(flow, bot, pane_text)
            elif flow.phase == PHASE_AWAITING_TRUST:
                if flow.trust_deadline is None:
                    flow.trust_deadline = (
                        time.monotonic() + config.trust_prompt_ceiling_s
                    )
                await decision_token.refresh_route_deadlines(
                    flow.user_id,
                    flow.thread_id,
                    flow.created_wid,
                    min_remaining_s=TOKEN_REFRESH_MIN_REMAINING_S,
                )
        elif (
            kind in (SliceKind.RUNNING, SliceKind.OTHER_SURFACE)
            and flow.trust_seen
            and flow.phase == PHASE_AWAITING_TRUST
        ):
            # The trust prompt is POSITIVELY gone (a running REPL / another
            # named surface) without a dispatch of ours ⇒ the user answered it
            # in tmux. Fresh registration budget (Fix 1). An INDETERMINATE slice
            # is deliberately NOT this signal (review r1 P1-3): a blank or
            # unreadable capture proves nothing, so it keeps waiting.
            flow.phase = PHASE_AWAITING_REGISTRATION
            flow.awaiting_registration_at = _wall()
            flow.trust_deadline = None
            _rebase_registration_budget(flow, reason="prompt answered in tmux")

        now = time.monotonic()
        if flow.phase == PHASE_DISPATCHING:
            # A dispatch transaction owns the pane right now; no budget may fire
            # a kill underneath it.
            continue
        if now >= flow.global_deadline:
            await _terminal_spare(
                flow,
                bot,
                body=(
                    "⚠️ I couldn't read the new window — it's still alive, so "
                    "nothing was closed.\n\n"
                    "Use *Bind to Existing Window* from a new message here, or "
                    "close the topic to clean it up."
                ),
            )
            return
        if flow.trust_deadline is not None and now >= flow.trust_deadline:
            await _terminal_cleanup(
                flow,
                bot,
                tmux_mgr,
                reason="trust prompt ceiling",
                body=(
                    "⏰ Timed out waiting for you to trust the folder.\n\n"
                    "Send your message again when you're ready."
                ),
            )
            return
        if (
            flow.trust_deadline is None
            and kind in (SliceKind.RUNNING, SliceKind.OTHER_SURFACE)
            and now >= flow.registration_deadline
        ):
            if kind is SliceKind.OTHER_SURFACE:
                tail = pane_tail(pane_text)
                body = "❌ Claude is blocked on a prompt I can't answer for you."
                if tail:
                    body += f"\n\n```\n{tail}\n```"
                body += "\n\nAnswer it in tmux, then send your message again."
                reason = "unhandled blocking surface"
            else:
                body = (
                    "❌ Claude session didn't register in time. The SessionStart "
                    "hook may be missing — run `cc-telegram doctor`.\n\n"
                    "Send your message again to retry."
                )
                reason = "SessionStart hook timeout"
            await _terminal_cleanup(flow, bot, tmux_mgr, reason=reason, body=body)
            return


async def _wait_task_body(
    flow: TrustFlow, bot: Any, tmux_mgr: Any, session_mgr: Any
) -> None:
    """The WAIT task with its TERMINALIZER (plan Fix 3).

    On ANY exit — return, exception, cancellation — the ``finally`` reacquires
    the creation lock, re-validates the generation, and (unless a terminal state
    was already committed) performs the guarded cleanup or the spare-and-release
    per the exit cause, disables the card + token rows, and drops the entry
    LAST. The registry's done-callback only logs; cleanup lives HERE.
    """
    cancelled = False
    try:
        await _wait_loop(flow, bot, tmux_mgr, session_mgr)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(
            "trust wait task failed (window=%s, thread=%s): %s",
            flow.created_wid,
            flow.thread_id,
            e,
        )
    finally:
        # A completion tail in flight is RETAINED and awaited BEFORE the lock is
        # taken — dropping the entry underneath it would strand a half-bound
        # topic (the tail still needs the pending-payload stash).
        inner = flow.bind_task
        if inner is not None and not inner.done():
            try:
                await asyncio.shield(inner)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        async with creation_lock(flow.user_id, flow.thread_id):
            if _flows.get((flow.user_id, flow.thread_id)) is flow:
                if not flow.terminal_committed:
                    if flow.phase == PHASE_COMPLETING_BIND:
                        # The bind tail owns its own terminal copy; never kill.
                        _release_tokens(flow)
                    elif cancelled:
                        # Teardown cancelled us: settle the window, stay honest.
                        await _terminal_cleanup(
                            flow,
                            bot,
                            tmux_mgr,
                            reason="creation flow torn down",
                            body="⚠️ This session setup was cancelled.",
                        )
                    else:
                        await _terminal_cleanup(
                            flow,
                            bot,
                            tmux_mgr,
                            reason="creation flow failed",
                            body=(
                                "❌ Something went wrong while setting up this "
                                "session.\n\nSend your message again to retry."
                            ),
                        )
                _drop_entry(flow)
                _drop_flow(flow)


def _log_task_result(task: asyncio.Task[None]) -> None:
    """Done-callback: retrieve the exception so it is never swallowed silently."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("trust wait task ended with %r", exc)


async def start_trust_wait(
    *,
    bot: Any,
    user_id: int,
    thread_id: int,
    chat_id: int | None,
    user_data: dict[str, Any] | None,
    created_wid: str,
    window_name: str,
    selected_path: str,
    create_message: str,
    cli_version: str | None,
    tmux_mgr: Any,
    session_mgr: Any,
) -> TrustFlow | None:
    """Claim the topic ATOMICALLY and spawn the classifying WAIT task.

    The caller has ALREADY answered the callback and edited the card to a
    "starting Claude…" state — the ENTIRE wait (including the first hook-timeout
    phase) runs here, so the callback coroutine is never held open for a
    minutes-scale wait.

    **Returns ``None`` when the topic is no longer claimable** (review r1 P1-1).
    ``_create_and_bind_window`` awaits two Telegram operations after its last
    owner check, so a concurrent ``/start`` or topic close can clear the picker
    entry in that window. Installing a flow anyway left it UNREACHABLE: the
    ENTRY is the ownership token, so every ``tst:`` claim would refuse, neither
    Trust nor Cancel could reach the flow, and a later registration would bind
    nothing. The re-validation and the registry write therefore share ONE
    critical section under the creation lock; the caller runs the guarded abort
    (kill/spare + the stale-picker edit) on ``None``.
    """
    async with creation_lock(user_id, thread_id):
        entry = picker_entry(user_data, thread_id)
        if entry is None:
            logger.warning(
                "Refusing to start a trust flow for window %s: thread %s no "
                "longer owns a picker entry",
                created_wid,
                thread_id,
            )
            return None
        existing = _flows.get((user_id, thread_id))
        if existing is not None and existing.phase in NONTERMINAL_PHASES:
            logger.warning(
                "Refusing to start a trust flow for window %s: thread %s "
                "already has a live flow in %s",
                created_wid,
                thread_id,
                existing.phase,
            )
            return None
        generation = _next_generation()
        entry[STATE_KEY] = STATE_AWAITING_TRUST
        entry[TRUST_GENERATION_KEY] = generation
        entry[TRUST_WID_KEY] = created_wid
        entry[TRUST_CLI_VERSION_KEY] = cli_version
        card_chat_id = entry.get(CARD_CHAT_ID_KEY)
        card_msg_id = entry.get(CARD_MSG_ID_KEY)
        now = _wall()
        budget = registration_budget_s()
        flow = TrustFlow(
            generation=generation,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            card_chat_id=card_chat_id,
            card_msg_id=card_msg_id,
            created_wid=created_wid,
            window_name=window_name,
            selected_path=selected_path,
            create_message=create_message,
            resume_id=None,
            cli_version=cli_version,
            user_data=user_data,
            started_at=now,
            registration_deadline=now + budget,
            global_deadline=now
            + max(budget, config.trust_prompt_ceiling_s)
            + GLOBAL_CEILING_MARGIN_S,
        )
        _flows[(user_id, thread_id)] = flow
        task = asyncio.create_task(_wait_task_body(flow, bot, tmux_mgr, session_mgr))
        task.add_done_callback(_log_task_result)
        flow.wait_task = task
    logger.info(
        "trust flow started window=%s thread=%s version=%s gen=%d",
        created_wid,
        thread_id,
        cli_version,
        generation,
    )
    return flow


# ── The tst: callback surface ────────────────────────────────────────────────


@dataclass(frozen=True)
class TrustClaim:
    """The outcome of a synchronous state claim on a ``tst:`` tap."""

    ok: bool
    flow: TrustFlow | None
    reason: str | None = None
    previous_phase: str | None = None


async def claim_for_dispatch(
    user_id: int, thread_id: int | None, *, user_data: dict[str, Any] | None
) -> TrustClaim:
    """Claim ``awaiting_trust → dispatching`` (the Trust tap's state claim).

    ``concurrent_updates`` is ON, so an entry-existence check alone is NOT a
    serialization: the claim runs under the creation lock with NO await between
    the read and the write, so exactly one of two concurrent Trust/Cancel taps
    wins and the other refuses gracefully.
    """
    if thread_id is None:
        return TrustClaim(False, None, "no_thread")
    async with creation_lock(user_id, thread_id):
        flow = _flows.get((user_id, thread_id))
        if flow is None:
            return TrustClaim(False, None, "missing_flow")
        entry = picker_entry(user_data, thread_id)
        if entry is None or entry.get(TRUST_GENERATION_KEY) != flow.generation:
            return TrustClaim(False, None, "missing_entry")
        if flow.phase != PHASE_AWAITING_TRUST:
            return TrustClaim(False, flow, "wrong_state", flow.phase)
        flow.phase = PHASE_DISPATCHING
        return TrustClaim(True, flow, None, PHASE_AWAITING_TRUST)


async def release_dispatch_claim(flow: TrustFlow, *, phase: str) -> None:
    """Move a ``dispatching`` flow to its next phase under the lock."""
    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is flow:
            flow.phase = phase


async def claim_for_cancel(
    user_id: int, thread_id: int | None, *, user_data: dict[str, Any] | None
) -> TrustClaim:
    """Claim ``awaiting_trust → cancelling`` (the Cancel tap's state claim)."""
    if thread_id is None:
        return TrustClaim(False, None, "no_thread")
    async with creation_lock(user_id, thread_id):
        flow = _flows.get((user_id, thread_id))
        if flow is None:
            return TrustClaim(False, None, "missing_flow")
        entry = picker_entry(user_data, thread_id)
        if entry is None or entry.get(TRUST_GENERATION_KEY) != flow.generation:
            return TrustClaim(False, None, "missing_entry")
        if flow.phase not in (PHASE_AWAITING_TRUST, PHASE_AWAITING_REGISTRATION):
            return TrustClaim(False, flow, "wrong_state", flow.phase)
        previous = flow.phase
        flow.phase = PHASE_CANCELLING
        return TrustClaim(True, flow, None, previous)


async def cancel_flow(flow: TrustFlow, bot: Any, tmux_mgr: Any) -> CleanupOutcome:
    """Cancel: NO keystrokes into the pane — kill the window under the guard.

    Dispatching option 2 / Esc would exit Claude to a bare shell anyway, so
    killing the window is the same end state with zero keystroke risk. A cancel
    that RACES a registration is SPARED by the typed guard, and the caller flips
    into the completion tail instead of reporting "cancelled".
    """
    outcome = await cleanup_created_window(
        flow.created_wid, flow.window_name, tmux_mgr, reason="trust card cancel"
    )
    if outcome is CleanupOutcome.KILLED:
        _release_tokens(flow)
        await _edit_card(
            flow,
            bot,
            "❌ Cancelled — the new tmux window was closed.\n\n"
            "Send a message here to start again.",
            None,
        )
        flow.terminal_committed = True
    elif outcome is CleanupOutcome.KILL_FAILED:
        _release_tokens(flow)
        await _edit_card(
            flow,
            bot,
            "⚠️ Cancelled, but "
            + cleanup_note(outcome, flow.window_name, flow.created_wid),
            None,
        )
        flow.terminal_committed = True
    return outcome


async def finish_cancelled_flow(flow: TrustFlow) -> None:
    """Terminal bookkeeping after a cancel: cancel the WAIT task, drop entry."""
    task = flow.wait_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    async with creation_lock(flow.user_id, flow.thread_id):
        _drop_entry(flow)
        _drop_flow(flow)


# ── Fix 5: the unbound-topic inbound re-read ─────────────────────────────────


@dataclass(frozen=True)
class BrowserPayload:
    """A directory-browser render, built BEFORE the ownership critical section.

    Building it is the last pre-processing await an unbound-topic handler makes
    (`_list_unbound_windows` + the directory scan), so it must happen OUTSIDE
    the lock — and therefore before the decision it might feed, never after it.
    """

    text: str
    keyboard: Any
    subdirs: list[str]
    unbound_count: int
    start_path: str


@dataclass(frozen=True)
class InboundDecision:
    """What an unbound-topic inbound handler must do, decided UNDER the lock.

    ``browser`` / ``entry`` are populated for the ``browser`` kind only: the
    entry was CLAIMED (state + browse caches + stash written) inside the same
    critical section that decided, so all the caller has left to do is send.
    """

    kind: Literal["bound", "trust_owned", "picker_owned", "browser"]
    window_id: str | None = None
    picker_state: str | None = None
    browser: BrowserPayload | None = None
    entry: dict[str, Any] | None = None


TRUST_NUDGE: Final[str] = (
    "🔐 Claude is waiting for you to trust the folder — tap ✅ on the card "
    "above, or answer in the tmux window. Your message is queued and will be "
    "sent as soon as the session is up."
)

PICKER_NUDGES: Final[dict[str, str]] = {
    STATE_BROWSING_DIRECTORY: "Please use the directory browser above, or tap Cancel.",
    STATE_SELECTING_WINDOW: "Please use the window picker above, or tap Cancel.",
    STATE_SELECTING_SESSION: "Please use the session picker above, or tap Cancel.",
}


async def claim_unbound_inbound(
    user_id: int,
    thread_id: int,
    user_data: dict[str, Any] | None,
    session_mgr: Any,
    *,
    build_browser: Callable[[], Any] | None = None,
    browse_start_path: str | None = None,
    stash: Callable[[dict[str, Any]], None] | None = None,
    stash_on_picker: bool = True,
) -> InboundDecision:
    """Fix 5 — decide AND mutate inside ONE critical section (review r1 P1-2).

    Every unbound-topic handler does async pre-processing (reply-context
    resolution, attachment download) and, on the browser path, one more await to
    LIST tmux windows and scan the directory. The pre-fold shape read the
    ownership state under the lock, RELEASED it, and only then built and wrote
    the browser — a textbook check-then-act: a binding or a creation flow
    appearing in that gap was overwritten with browser state, and the payload
    was stashed into a picker for an already-bound topic (silently discarded).

    So the resolution runs at most TWICE:

      * pass 1 — under the lock, read the binding and the entry. A bound topic
        or an OWNED entry returns immediately, and no browser is ever built for
        it (the common case pays nothing).
      * otherwise the browser is built OUTSIDE the lock (the remaining awaits)…
      * pass 2 — under the lock AGAIN, re-read both. Anything that appeared in
        the build window wins; only a still-free topic is CLAIMED, and the claim
        (state + browse caches + the payload stash) happens in that same
        critical section, so the decision and the mutation are inseparable.

    ``stash`` runs INSIDE the lock so a payload can never land in an entry a
    concurrent teardown already dropped. ``stash_on_picker=False`` keeps
    text_handler's pre-#65 behavior (a text message arriving mid-picker is a
    nudge, not a stash).
    """
    browser: BrowserPayload | None = None
    for _ in range(2):
        async with creation_lock(user_id, thread_id):
            wid = session_mgr.get_window_for_thread(user_id, thread_id)
            if wid:
                return InboundDecision("bound", window_id=wid)
            entry = picker_entry(user_data, thread_id)
            state = entry.get(STATE_KEY) if entry is not None else None
            flow = _flows.get((user_id, thread_id))
            if (
                entry is not None
                and state == STATE_AWAITING_TRUST
                and flow is not None
                and flow.phase in NONTERMINAL_PHASES
                and entry.get(TRUST_GENERATION_KEY) == flow.generation
            ):
                if stash is not None:
                    stash(entry)
                return InboundDecision("trust_owned", picker_state=state)
            if entry is not None and state in _PICKER_CHROME_STATES:
                if stash is not None and stash_on_picker:
                    stash(entry)
                return InboundDecision("picker_owned", picker_state=state)
            if browser is not None or build_browser is None:
                claimed = ensure_picker_entry(user_data, thread_id)
                if claimed is not None:
                    if stash is not None:
                        stash(claimed)
                    claimed[STATE_KEY] = STATE_BROWSING_DIRECTORY
                    if browser is not None:
                        claimed[BROWSE_PATH_KEY] = browser.start_path
                        claimed[BROWSE_PAGE_KEY] = 0
                        claimed[BROWSE_DIRS_KEY] = browser.subdirs
                        claimed[BROWSE_UNBOUND_COUNT_KEY] = browser.unbound_count
                    elif browse_start_path is not None:
                        claimed[BROWSE_PATH_KEY] = browse_start_path
                        claimed[BROWSE_PAGE_KEY] = 0
                return InboundDecision("browser", browser=browser, entry=claimed)
        # Still free: build the browser OUTSIDE the lock, then re-decide.
        browser = await build_browser()
    raise AssertionError("unreachable: pass 2 always has a browser")  # pragma: no cover


# ── Fix 6: teardown ──────────────────────────────────────────────────────────


async def teardown_thread(
    user_id: int,
    thread_id: int | None,
    *,
    bot: Any = None,
    user_data: dict[str, Any] | None = None,
    reason: str = "topic teardown",
) -> bool:
    """Tear down a live creation flow for this topic (NORMATIVE choreography).

    Returns **True when the flow's COMPLETION WON** — the bind tail ran to the
    end — so the caller knows to run the normal BOUND-topic teardown on the
    now-bound topic.

    Acquire the creation lock ONLY to capture phase + task identities (+
    generation), RELEASE it BEFORE cancelling or awaiting either task (the WAIT
    terminalizer reacquires that same lock — awaiting it while holding the lock
    DEADLOCKS), then REACQUIRE and re-check. The re-check tests the PHASE, not
    just identity (review r1 P2-3): a transition to ``completing_bind`` inside
    the capture→cancel→reacquire window must be HONORED — the retained inner
    task is awaited rather than abandoned — so the loop runs at most twice.
    """
    del bot, user_data, reason  # the flow record carries its own card + entry
    if thread_id is None:
        return False
    key = (user_id, thread_id)
    completed = False
    for _ in range(2):
        async with creation_lock(user_id, thread_id):
            flow = _flows.get(key)
            if flow is None:
                return completed
            phase = flow.phase
            wait_task = flow.wait_task
            bind_task = flow.bind_task
            generation = flow.generation

        if phase == PHASE_COMPLETING_BIND and bind_task is not None:
            # Do NOT cancel — the completion tail is a separately-tracked inner
            # task that is RETAINED and awaited (not a bare ``asyncio.shield``),
            # so the topic can never be left half-bound.
            try:
                await bind_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            completed = True
        elif wait_task is not None and not wait_task.done():
            wait_task.cancel()
            try:
                await wait_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        async with creation_lock(user_id, thread_id):
            current = _flows.get(key)
            if current is None or current.generation != generation:
                return completed
            if not completed and current.phase == PHASE_COMPLETING_BIND:
                # The transition landed in our window — honor it on pass 2.
                continue
            _release_tokens(current)
            _drop_entry(current)
            _drop_flow(current)
            return completed
    return completed


async def teardown_all_for_user(user_id: int, *, user_data: Any = None) -> list[int]:
    """Tear down EVERY topic's creation flow for one user (the ``/start`` reset).

    Returns the thread ids whose COMPLETION WON during teardown, so the caller
    can run the normal bound-topic teardown on each (review r1 P2-3).
    """
    del user_data
    completed: list[int] = []
    for uid, tid in [key for key in _flows if key[0] == user_id]:
        if await teardown_thread(uid, tid, reason="/start reset"):
            completed.append(tid)
    return completed


async def shutdown() -> None:
    """Cancel + await every live flow task at bot shutdown."""
    for uid, tid in list(_flows.keys()):
        await teardown_thread(uid, tid, reason="bot shutdown")
