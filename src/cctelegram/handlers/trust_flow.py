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
from pathlib import Path
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Final, Literal

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
    ENTRY_TOKEN_KEY,
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
# Per-call bound on EVERY tmux await a slice makes (review r2 P1-C). A wedged
# tmux subprocess must not park the WAIT task past the global observation
# ceiling — the ceiling is a TERMINAL bound, not a best-effort one. Mirrors the
# repo's ``capture_pane_cancellation_safe`` discipline: only ``TimeoutError``
# classifies (as INDETERMINATE); a genuine cancellation propagates untouched.
SLICE_TMUX_TIMEOUT_S: float = 5.0


# ── Phases ───────────────────────────────────────────────────────────────────

PHASE_AWAITING_TRUST: Final[str] = "awaiting_trust"
PHASE_DISPATCHING: Final[str] = "dispatching"
PHASE_AWAITING_REGISTRATION: Final[str] = "awaiting_registration"
PHASE_CANCELLING: Final[str] = "cancelling"
PHASE_COMPLETING_BIND: Final[str] = "completing_bind"
# The explicit end state. Before it existed "terminal" was implicit (a flow
# removed from the registry plus a ``terminal_committed`` flag), which meant a
# CAS had nothing to refuse against between the last side effect and the drop.
PHASE_TERMINAL: Final[str] = "terminal"

# OPEN phases: nobody owns the flow, so a claim may be made.
OPEN_PHASES: Final[frozenset[str]] = frozenset(
    {PHASE_AWAITING_TRUST, PHASE_AWAITING_REGISTRATION}
)
# CLAIMED phases: exactly ONE actor owns the flow and is mid-side-effect. Every
# terminal claim must refuse ALL of these — two cleanups can never both win, and
# a cleanup can never fire underneath a dispatch or a bind.
CLAIMED_PHASES: Final[frozenset[str]] = frozenset(
    {PHASE_DISPATCHING, PHASE_CANCELLING, PHASE_COMPLETING_BIND, PHASE_TERMINAL}
)
# The claimed phases held by a CALLBACK TASK (as opposed to ``completing_bind``,
# held by the retained bind tail, and ``terminal``, held by nobody). Teardown
# settles these by cancelling + awaiting ``flow.claim_task``.
_TASK_CLAIMED_PHASES: Final[frozenset[str]] = frozenset(
    {PHASE_DISPATCHING, PHASE_CANCELLING}
)
# When True, an invariant violation RAISES instead of logging. Set by
# ``reset_for_tests``, so the suite is strict and production is resilient.
_STRICT_INVARIANTS: bool = False

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

# Every phase a flow can be in — the ``expect`` set for a FORCED claim.
ALL_PHASES: Final[frozenset[str]] = NONTERMINAL_PHASES | {PHASE_TERMINAL}

# How long a teardown that finds a dispatch in flight waits for it to finish
# before proceeding. The ``tst:`` callback's ``finally`` guarantees the phase
# leaves ``dispatching``, so this is a safety bound, not the mechanism.
DISPATCH_SETTLE_BUDGET_S: float = 45.0
# Extra time a teardown gives a RETAINED BIND TAIL past its own budget. A bind
# that is about to win must never be force-claimed out from under itself.
BIND_TAIL_GRACE_S: float = 10.0
# How long the aborted-teardown orphan arm may spend on its guarded cleanup
# (review r8 P1-A). It runs while we are ALREADY unwinding, so it is shielded
# against re-cancellation and bounded against a wedged tmux; the flow is dropped
# on every outcome regardless.
ORPHAN_CLEANUP_BUDGET_S: float = 15.0
_DISPATCH_SETTLE_POLL_S: float = 0.05

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


class TrustBindRefused(Exception):
    """A TYPED refusal from the completion bind's adoption gate (review r11 P1-B).

    Raised BEFORE ``bind_thread``, so it is always a clean "nothing happened":
    no binding, no completion note, and therefore ``bind_committed`` stays False
    and the caller's existing failure arm runs the guarded cleanup with honest
    copy. Distinct from an unexpected exception only in intent — the tail's
    outcome machinery already treats "raised before the bind" as a failure, and
    this makes that path deliberate rather than incidental.
    """


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
    # The picker entry's identity token this flow was installed against. Any
    # later claim re-validates it, so an entry cleared and recreated underneath
    # the flow is detected rather than silently adopted.
    entry_token: str | None = None
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
    # THE CARD IS FINALIZED (review r10 P2-A). Set by an arm that has posted the
    # LAST, most ACTIONABLE copy the user should see — today that is the
    # committed-but-unfinished bind's "your pending message may not have been
    # delivered" warning. A later generic edit (``_terminal_cleanup``'s
    # cancelled-copy on the forced path) would otherwise overwrite it, and the
    # user would be told the setup was cancelled when in fact the topic is bound
    # and their message is in limbo — the worst of both readings. The actionable
    # disclosure must be the last copy on every path that produced it.
    card_finalized: bool = False
    wait_task: asyncio.Task[None] | None = None
    bind_task: asyncio.Task[None] | None = field(default=None)
    # The task that currently HOLDS a ``dispatching`` / ``cancelling`` claim
    # (review r5 P1-B). Registered at acquisition, cleared by the CAS that
    # leaves the claimed phase. Teardown cancels + AWAITS it — positive proof
    # the side effect is over, instead of inferring settlement from a timer.
    claim_task: asyncio.Task[Any] | None = field(default=None)
    # Whether the claim's owner may be CANCELLED to settle it. True for the
    # callback claims (a tap's transaction); False for the cooperative ones
    # (teardown's own, the WAIT terminalizer's) — cancelling those mid-cleanup
    # is the very "drop beneath another actor's kill_window" bug (r6 P1-B).
    claim_cancellable: bool = False
    # THE TEARDOWN FENCE (review r6 P1-A). Raised under the lock when a teardown
    # starts; while it is up, every claim ACQUISITION is refused, so successive
    # claims cannot starve the teardown loop. In-flight claims still settle
    # normally — the fence stops new ones, it does not steal existing ones.
    #
    # It is OWNED (review r8 P2-A). A bare boolean had no owner, so the FIRST of
    # two concurrent teardowns lowered the fence the SECOND had raised (or one a
    # replacement generation was relying on) and the survivor ran unfenced —
    # exactly the starvation the fence exists to prevent. The owner is the
    # raising task PLUS the generation it raised for, and only that pair may
    # lower it.
    fence_owner: asyncio.Task[Any] | None = None
    fence_generation: int | None = None
    # The collaborators every arbitration path needs, so ANY actor (the WAIT
    # task, a callback, teardown) can run the full choreography without the
    # caller having to thread them through.
    bot: Any = None
    tmux_mgr: Any = None
    session_mgr: Any = None
    # The pending-payload REPLAY, INJECTED (never reached for through a module
    # global). This is the one call in the lane that can put real keystrokes
    # into a real pane, so it must be substitutable the same way ``bot`` and
    # ``tmux_mgr`` are: a unit test injects a fake and CANNOT reach the live
    # ``session_manager`` / ``tmux_manager`` singletons by accident. Production
    # passes nothing and gets ``inbound_telegram._flush_pending_route_payload``.
    replay: Any = None
    # PRIVATE (review r4 P3): the single-mutator invariant is enforced by the
    # type, not by convention — ``phase`` below is read-only, the field is not a
    # constructor argument (a flow always starts at ``awaiting_trust``), and the
    # ONLY writers are ``try_transition_locked`` and ``force_terminal_claim``.
    _phase: str = field(default=PHASE_AWAITING_TRUST, init=False, repr=False)

    @property
    def phase(self) -> str:
        """The flow's current phase. READ-ONLY — mutate via the CAS seam."""
        return self._phase

    @property
    def teardown_fenced(self) -> bool:
        """Whether ANY teardown currently holds the fence."""
        return self.fence_owner is not None

    def raise_fence(self) -> None:
        """Claim the fence for the CURRENT task. Caller holds the creation lock.

        Re-raising an unowned fence, or one this task already owns, is the
        normal per-pass refresh. A fence a LIVE task owns is left alone — it is
        already up, which is all this caller needs.

        A fence whose owner is DEAD is RECLAIMED (review r9 P2-A). Ownership was
        introduced so one teardown could not lower another's fence, but it also
        meant a teardown killed uncleanly — before its ``finally`` could lower
        it — left the topic fenced by a corpse: no later teardown could take
        ownership, so nothing could ever lower it again and every claim
        acquisition was refused for the life of the process.
        """
        owner = self.fence_owner
        if owner is not None and owner is not asyncio.current_task():
            if not owner.done():
                return
            logger.warning(
                "trust flow reclaiming a fence from a DEAD owner "
                "(window=%s, thread=%s)",
                self.created_wid,
                self.thread_id,
            )
        self.fence_owner = asyncio.current_task()
        self.fence_generation = self.generation

    def lower_fence_if_owned(self) -> bool:
        """Release the fence only if THIS task raised it for THIS generation."""
        if self.fence_owner is not asyncio.current_task():
            return False
        if self.fence_generation != self.generation:
            return False
        self.fence_owner = None
        self.fence_generation = None
        return True


_FlowKey = tuple[int, int]

_flows: dict[_FlowKey, TrustFlow] = {}
# GH #65 review r2 P2-A: a DISCOVERABLE completion record. The WAIT task's
# terminalizer removes the flow as soon as its bind tail finishes, so a teardown
# that snapshotted a nonterminal phase and reacquires the lock afterwards finds
# NOTHING — and would report "no completion", silently skipping the bound-topic
# teardown its caller owes. The tail therefore leaves this note behind; teardown
# CONSUMES it on a reacquire-sees-None, and falls back to the authoritative
# binding as a second proof.
_completed_binds: dict[_FlowKey, "_CompletionNote"] = {}
_binding_baselines: dict[_FlowKey, "_BaselineNote"] = {}
# The PRE-FLOW binding baseline, GENERATION-QUALIFIED and independent of the
# flow's lifetime (review r4 P2-C). Keeping it on the flow meant it died in
# ``_drop_flow`` — exactly when the completion fallback needs it — so a
# pre-existing binding, a rebind, or a reused tmux window id after a tmux-server
# restart could read as this flow's completion. Same TTL/cap sweep as the
# completion notes; never dropped with the flow.
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


def flow_owner_for_card(chat_id: int | None, message_id: int | None) -> int | None:
    """The user_id owning the creation-flow card at these coordinates, or None.

    The ``tst:`` card lives in a shared forum topic, so ANOTHER allowed user can
    tap it. Their PTB ``user_data`` is a different dict, so an ownership check
    keyed on the entry alone would read "expired" and EDIT the owner's card
    (review r1 P2-4). Ownership therefore resolves the TAPPED CARD, not the
    thread (review r2 P2-E): two allowed users can each have a creation flow in
    ONE topic, and a thread-only match would reject the second user's tap on
    their OWN card. ``None`` = no live flow owns this card, so the caller's
    normal entry-keyed self-heal applies.
    """
    if chat_id is None or message_id is None:
        return None
    for (uid, _tid), flow in _flows.items():
        if (
            flow.card_chat_id == chat_id
            and flow.card_msg_id == message_id
            and flow.phase in NONTERMINAL_PHASES
        ):
            return uid
    return None


def flow_task(user_id: int, thread_id: int) -> asyncio.Task[None] | None:
    """The topic's WAIT task (public so teardown + tests can join it)."""
    flow = _flows.get((user_id, thread_id))
    return flow.wait_task if flow is not None else None


def lane_enabled() -> bool:
    """False when ``CC_TELEGRAM_TRUST_PROMPT_CEILING_S=0`` disables the lane."""
    return config.trust_prompt_ceiling_s > 0


def enable_strict_invariants() -> None:
    """Make every invariant violation RAISE instead of logging.

    Called ONCE at suite import (review r8 P3). Arming it only inside
    ``reset_for_tests`` made strictness test-ORDER dependent: the very first
    test in a process — or any single test run in isolation, which is exactly
    how a failure gets reproduced — ran with the invariants merely logging, so
    the assertions that are supposed to be the lane's safety net were silently
    inert in the one run where they matter most.
    """
    global _STRICT_INVARIANTS
    _STRICT_INVARIANTS = True


def reset_for_tests() -> None:
    """Drop every flow record + lock (co-located reset seam)."""
    global _generation_counter, _STRICT_INVARIANTS
    _STRICT_INVARIANTS = True
    for flow in list(_flows.values()):
        for task in (flow.wait_task, flow.bind_task):
            if task is not None and not task.done():
                task.cancel()
    _flows.clear()
    _locks.clear()
    _completed_binds.clear()
    _binding_baselines.clear()
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


async def _bounded(awaitable: Any, *, what: str, window_id: str) -> Any:
    """Await ``awaitable`` under ``SLICE_TMUX_TIMEOUT_S``; None on timeout.

    ONLY ``TimeoutError`` is swallowed (→ the caller classifies INDETERMINATE
    and keeps waiting under the ceilings). ``CancelledError`` propagates, so a
    teardown still cancels promptly.

    Callers MUST pass a CANCELLATION-SAFE awaitable (review r3 P2-3): the
    deadline firing cancels the inner await, and a tmux helper without a
    kill-on-cancel path would orphan one subprocess per timed-out slice.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=SLICE_TMUX_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.warning(
            "trust slice %s timed out after %.1fs (window=%s) — indeterminate",
            what,
            SLICE_TMUX_TIMEOUT_S,
            window_id,
        )
        return None


async def _probe_pane(tmux_mgr: Any, window_id: str) -> tuple[str | None, str | None]:
    """One slice's pane observation: ``(pane_text, pane_command)``, both bounded.

    ORDER IS LOAD-BEARING (review r3 P2-2): the TEXT is captured FIRST and the
    COMMAND LAST, so a Claude→shell flip that happens between the two is seen by
    the command — the fail-closed half. Reading the command first would let a
    stale "claude" vouch for text captured after the process had already exited.
    Both use the CANCELLATION-SAFE tmux variants so a timed-out slice reaps its
    subprocess instead of orphaning it.
    """
    text = await _bounded(
        _capture_pane_safe(tmux_mgr, window_id),
        what="capture_pane",
        window_id=window_id,
    )
    command = await _bounded(
        _pane_command_safe(tmux_mgr, window_id),
        what="pane_current_command",
        window_id=window_id,
    )
    return text, command


async def _capture_pane_safe(tmux_mgr: Any, window_id: str) -> str | None:
    """The cancellation-safe capture, falling back for a fake/older manager."""
    safe = getattr(tmux_mgr, "capture_pane_cancellation_safe", None)
    if safe is not None:
        return await safe(window_id)
    return await tmux_mgr.capture_pane(window_id)


async def _pane_command_safe(tmux_mgr: Any, window_id: str) -> str | None:
    """The cancellation-safe command probe, with the same fallback."""
    safe = getattr(tmux_mgr, "pane_current_command_cancellation_safe", None)
    if safe is not None:
        return await safe(window_id)
    return await tmux_mgr.pane_current_command(window_id)


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
    session_mgr: Any = None,
    owner_guard: Callable[[], Awaitable[bool]] | None = None,
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
    # INJECTED binding authority (default: the real singleton). A unit test
    # supplies a fake, so it never has to SEED the live ``session_manager`` to
    # reach a ``SPARED_BOUND`` — seeding it is what let a fake-tmux test's
    # replay escape into the user's real tmux server.
    bindings_source = session_mgr or session_manager
    bindings_probe = getattr(bindings_source, "iter_thread_bindings", None)
    if bindings_probe is None:
        bindings_probe = session_manager.iter_thread_bindings
    is_bound = window_id in {wid for _, _, wid in bindings_probe()}
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
    # nothing can interleave between them and the tmux call. Both are resolved
    # through the INJECTED authority when it offers them, so a unit test never
    # has to seed the live singleton to model a registered window.
    cached_probe = getattr(bindings_source, "peek_session_id_for_window", None)
    fresh_probe = getattr(bindings_source, "read_session_id_for_window_fresh", None)
    cached_sid = (
        cached_probe(window_id)
        if cached_probe is not None
        else peek_session_id_for_window(window_id)
    )
    fresh_sid = (
        fresh_probe(window_id)
        if fresh_probe is not None
        else read_session_id_for_window_fresh(window_id)
    )
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
    if owner_guard is not None and not await owner_guard():
        # OWNERSHIP RE-CHECK, immediately before the kill (review r9 P1-B). A
        # cleanup can outlive the flow that started it — the orphan arm bounds
        # it and then cancels, but a straggler already past its bound could
        # still reach this line. By then a NEW flow may have adopted this window
        # id, and killing it would destroy a live session that has nothing to do
        # with us. This is the belt to the leash's suspenders: even a straggler
        # cannot kill a window it no longer owns.
        logger.warning(
            "Skipping cleanup of tmux window %s (%s) after %s: it is no longer "
            "owned by the flow that requested the cleanup",
            window_id,
            window_name,
            reason,
        )
        return CleanupOutcome.SPARED_BOUND
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


def binding_is_current_route(flow: TrustFlow, session_mgr: Any = None) -> bool:
    """True when THIS topic is the one bound to the flow's window.

    ``cleanup_created_window`` only proves the window is bound SOMEWHERE
    (review r6 P2), which is two very different situations: if the binding is
    THIS route's, the bind WON and the spec requires the pending payload to be
    delivered normally; if it belongs to another topic, the window is collateral
    and this flow is simply cancelled — and the card must not claim that this
    topic is bound.
    """
    resolver = session_mgr or flow.session_mgr or session_manager
    try:
        return resolver.get_window_for_thread(flow.user_id, flow.thread_id) == (
            flow.created_wid
        )
    except Exception:  # noqa: BLE001
        return False


def cleanup_note(outcome: CleanupOutcome, window_name: str, window_id: str) -> str:
    """The honest per-outcome sentence appended to a failure card edit."""
    if outcome is CleanupOutcome.KILLED:
        return "The unmonitored tmux window was cleaned up."
    if outcome is CleanupOutcome.SPARED_BOUND:
        # Deliberately does NOT name a topic: the guard only proves the window is
        # bound SOMEWHERE (review r6 P2).
        return "The tmux window was left alone — a session is already bound to it."
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
    if flow.card_finalized:
        # A FINAL, actionable disclosure already stands (review r10 P2-A).
        # Generic later copy must not bury it.
        logger.debug(
            "trust card edit skipped for thread=%s — the card is finalized",
            flow.thread_id,
        )
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


async def refresh_card_if_live(flow: TrustFlow, bot: Any, tmux_mgr: Any) -> bool:
    """Re-render the trust card, but ONLY against a provably LIVE pane.

    The expired/stale-tap refresh. Three gates the naive "capture and
    re-render" shape misses:

    * a dead pane RETAINS the trust prompt text (addendum item 2), so a corpse
      would happily re-mint a Trust button that types into a shell. Positive
      ``pane_command_is_claude`` is REQUIRED before any mint — and the capture
      pair is TEXT FIRST, COMMAND LAST so a Claude→shell flip between them fails
      closed (review r3 P2-2).
    * the capture is an AWAIT, so the flow can be torn down inside it. The
      phase must be exactly ``awaiting_trust`` afterwards — not merely
      "nonterminal", which would let a refresh mint under a cleanup's or a
      dispatch's claim — and the picker entry's identity token and the flow's
      generation must both still match.
    * the VALIDATION and the MINT share ONE lock hold, so nothing can claim the
      flow between "it is still mine" and "here is a fresh token". Only the
      Telegram edit itself happens outside.

    Returns True iff a live card was re-rendered.
    """
    pane, command = await _probe_pane(tmux_mgr, flow.created_wid)

    disable = False
    token: str | None = None
    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is not flow:
            logger.info(
                "trust card refresh skipped: flow gone/replaced (window=%s)",
                flow.created_wid,
            )
            return False
        if flow.phase != PHASE_AWAITING_TRUST:
            logger.info(
                "trust card refresh skipped: flow is %s, not awaiting_trust "
                "(window=%s)",
                flow.phase,
                flow.created_wid,
            )
            return False
        entry = picker_entry(flow.user_data, flow.thread_id)
        if (
            entry is None
            or entry.get(ENTRY_TOKEN_KEY) != flow.entry_token
            or entry.get(TRUST_GENERATION_KEY) != flow.generation
        ):
            logger.info(
                "trust card refresh skipped: entry identity changed (window=%s)",
                flow.created_wid,
            )
            return False
        if not pane_command_is_claude(command):
            logger.info(
                "trust card refresh declined: pane command %r is not Claude "
                "(window=%s)",
                command,
                flow.created_wid,
            )
            _release_tokens(flow)
            flow.token = None
            flow.fingerprint = None
            flow.ledger_key = None
            disable = True
        else:
            token = _mint_trust_token_locked(flow, pane)

    if disable:
        await _edit_card(
            flow,
            bot,
            "⚠️ That prompt is no longer live — Claude is not running in the "
            "window any more.\n\nSend your message again to start a new session.",
            None,
        )
        return False
    text, keyboard = build_trust_card(flow, trust_token=token)
    await _edit_card(flow, bot, text, keyboard)
    return True


def _mint_trust_token_locked(flow: TrustFlow, pane_text: str | None) -> str | None:
    """Mint (or clear) this flow's Trust token. Caller MUST hold the lock.

    Synchronous by construction: the licensing decision and the registry
    mutation cannot be separated by an await, so nothing can claim the flow
    between them (review r3 P2-2).
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
    return token


async def render_trust_card(flow: TrustFlow, bot: Any, pane_text: str | None) -> None:
    """Mint (when licensed) and publish/refresh the trust card.

    The mint is the FIRST of the two gates on the trust flag + the explicit
    operator kill switch (the callback entry is the second). A mint requires,
    additionally: a strict ``parse_generic_decision``, the ``folder-trust``
    family, a clean single-select geometry, and the PROBED version licensed in
    the ONE ``_DECISION_DISPATCH_TABLE``. The registry mutation happens under
    the creation lock; only the Telegram edit is outside it.
    """
    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is not flow:
            return
        # PHASE, not just identity (review r4 P1-C / P2-A): a terminal-marked or
        # claimed flow must never mint a token or repaint a card another actor
        # has already given its final state.
        if flow.phase != PHASE_AWAITING_TRUST:
            return
        token = _mint_trust_token_locked(flow, pane_text)
    text, keyboard = build_trust_card(flow, trust_token=token)
    await _edit_card(flow, bot, text, keyboard)


# ── Fix 3 / Fix 7: terminal transitions ──────────────────────────────────────


def try_transition_locked(
    flow: TrustFlow,
    *,
    expect: frozenset[str],
    to: str,
    acquisition: bool = False,
    ignore_fence: bool = False,
    cancellable: bool = False,
) -> bool:
    """THE ONLY MUTATOR OF ``flow.phase``. The caller MUST hold the creation lock.

    Wave 3's structural consolidation. Three consecutive review rounds found the
    same class of hole — a phase claim or an entry clear that was not atomic
    with its own side effects — so arbitration is now ONE compare-and-swap
    discipline instead of a set of ad-hoc guards:

    **Every actor must WIN its CAS before performing ANY side effect** — a kill,
    a Telegram edit, a bind, a keystroke, a token mint. The WAIT slice
    classification, the ``tst:`` callback (its dispatch claim AND its
    finally-restore), every cleanup path, the terminalizer, teardown and the
    completion tail all go through here. An actor that LOSES does nothing (or
    defers); the winner owns the flow until it transitions again.

    Because it is called with the lock held and never awaits, the read and the
    write are inseparable — the property every previous shape was missing.
    """
    if _flows.get((flow.user_id, flow.thread_id)) is not flow:
        return False
    if acquisition and flow.teardown_fenced and not ignore_fence:
        # A teardown owns this topic's future. New claims are REFUSED so the
        # loop cannot be starved by an endless stream of them (r6 P1-A); a
        # registration losing here is the same accepted, documented loss class
        # as one landing after the fresh-read linearization point.
        return False
    if flow._phase not in expect:
        return False
    if flow._phase in _TASK_CLAIMED_PHASES and to != flow._phase:
        # Leaving a task-held claim: the handle dies with the claim, in the same
        # critical section, so teardown can never await a task that no longer
        # owns anything (review r5 P1-B).
        flow.claim_task = None
        flow.claim_cancellable = False
    flow._phase = to
    _assert_open_implies_observed(flow, to)
    if to in _TASK_CLAIMED_PHASES:
        # CLAIMED IMPLIES OWNED (review r7 P1-B). Registering the owner HERE,
        # inside the same critical section as the phase write, makes it
        # structurally impossible to enter a task-held claim anonymously —
        # which is what let a forced claim and a failed-bind recovery slip
        # through owner-less, so a concurrent teardown saw no owner, timed out,
        # and cleaned up beneath the actor already working.
        flow.claim_task = asyncio.current_task()
        flow.claim_cancellable = cancellable
    _assert_claimed_implies_owned(flow)
    return True


async def transition(
    flow: TrustFlow,
    *,
    expect: frozenset[str],
    to: str,
    acquisition: bool = False,
    ignore_fence: bool = False,
    cancellable: bool = False,
) -> bool:
    """Acquire the creation lock and CAS (the seam for callers not holding it)."""
    async with creation_lock(flow.user_id, flow.thread_id):
        return try_transition_locked(
            flow,
            expect=expect,
            to=to,
            acquisition=acquisition,
            ignore_fence=ignore_fence,
            cancellable=cancellable,
        )


def _assert_claimed_implies_owned(flow: TrustFlow) -> None:
    """A task-held claim with no owner is a PROGRAMMING ERROR, not a race.

    Raises under ``_STRICT_INVARIANTS`` (the test posture) and logs loudly in
    production, where refusing to proceed would be worse than the bug.
    """
    if flow._phase in _TASK_CLAIMED_PHASES and flow.claim_task is None:
        message = (
            f"trust flow entered {flow._phase} with no owner "
            f"(window={flow.created_wid}, thread={flow.thread_id})"
        )
        if _STRICT_INVARIANTS:
            raise AssertionError(message)
        logger.error(message)


def _assert_open_implies_observed(flow: TrustFlow, to: str) -> None:
    """OPEN IMPLIES OBSERVED (review r8 P1-A) — the lane's second invariant.

    An OPEN phase is not a resting state: it means "nobody holds this flow, and
    its WAIT task is watching it". The ceilings, the registration poll, the
    trust-card render and every terminal decision live in that task. Restoring a
    flow to an OPEN phase when its observer is DEAD therefore parks the topic
    forever — no ceiling can fire, no registration can complete, and every
    inbound message nudges a flow nothing will ever advance. Round 6 fixed one
    door into that room (the WAIT loop's own lost-CAS return); this closes the
    class, so no future caller can reopen it from a path we have not thought of.

    ``wait_task is None`` is the legitimate pre-observer window: ``start_trust_wait``
    installs the flow at ``awaiting_trust`` and only then creates the task.

    Raises under ``_STRICT_INVARIANTS`` (the test posture) and logs loudly in
    production, where refusing to proceed would be worse than the bug.
    """
    if to not in OPEN_PHASES:
        return
    task = flow.wait_task
    if task is None or not task.done():
        return
    message = (
        f"trust flow restored to OPEN phase {to} with a DEAD observer "
        f"(window={flow.created_wid}, thread={flow.thread_id}) — it must "
        "terminalize instead"
    )
    if _STRICT_INVARIANTS:
        raise AssertionError(message)
    logger.error(message)


async def force_terminal_claim(flow: TrustFlow) -> bool:
    """CAS from ANY phase into ``cancelling``. RESERVED — two callers only.

    The escape hatch for a claim that never came back (review r4 P1-B): the
    teardown-expiry path, after its bounded wait for a dispatch to settle has
    truly expired, and shutdown. Every other actor must win an ordinary CAS.

    **Disclosed residual:** forcing means an in-flight dispatch's ``Enter`` may
    still land on the pane after teardown has claimed the flow. It is bounded to
    the topic-close / shutdown paths, where the window is about to be killed
    anyway, and it is strictly better than the alternative — a flow wedged in
    ``dispatching`` forever, which is what three earlier rounds produced.
    """
    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is not flow:
            return False
        logger.warning(
            "trust flow FORCED out of %s (window=%s, thread=%s) — a claim never "
            "returned; an in-flight keystroke may still land",
            flow._phase,
            flow.created_wid,
            flow.thread_id,
        )
        # Through the ONE seam, so the FORCING task becomes the owner (r7 P1-B):
        # a forced claim used to land owner-less, and a concurrent teardown that
        # saw no owner timed out and cleaned up beneath the forcing actor.
        return try_transition_locked(
            flow,
            expect=ALL_PHASES,
            to=PHASE_CANCELLING,
            ignore_fence=True,
        )


async def release_claim(flow: TrustFlow, *, expect: str, to: str) -> bool:
    """Release a claim THIS actor made. The CAS matches the phase it acquired.

    Acquisition and release must be symmetric (review r4 P1-A): a Cancel that
    claimed ``cancelling`` releasing through a ``dispatching``-only CAS never
    releases at all, which wedges the flow against every later claim. Each actor
    names the phase it holds.
    """
    return await transition(flow, expect=frozenset({expect}), to=to)


async def _cancel_and_await(
    task: asyncio.Task[Any] | None, *, budget: float | None = None
) -> None:
    """Cancel a task and await it, swallowing its outcome.

    ``budget`` bounds the wind-down wait: a task that refuses to die must not
    park the caller forever (the leash in the orphan arm relies on this).
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        if budget is None:
            await task
        else:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001
        pass


async def _bounded_shield(coro: Any, *, budget: float, what: str) -> None:
    """Run ``coro`` so a cancellation aimed at US cannot abandon it half-done.

    Used for the steps that MUST happen on every outcome (review r9 P1-B): the
    shield keeps the work running, the bound keeps us from parking on it, and
    the task handle means nothing is left detached.
    """
    task = asyncio.create_task(coro)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001
        logger.warning("trust %s did not complete within its budget", what)
    finally:
        if not task.done():
            await _cancel_and_await(task, budget=budget)


async def claim_terminal(
    flow: TrustFlow,
    phase: str = PHASE_CANCELLING,
    *,
    ignore_fence: bool = False,
    cancellable: bool = False,
) -> bool:
    """CLAIM the flow for a cleanup. EXCLUSIVE against every claimed phase.

    Refuses when the flow is in ANY claimed or terminal phase — ``dispatching``,
    ``completing_bind``, ``cancelling`` or ``terminal`` — so two cleanups can
    never both win, and a cleanup can never fire underneath a dispatch or a
    bind. Only the two OPEN phases are claimable.

    Like EVERY acquisition (review r6 P1-B) it registers its owner, so whoever
    needs to settle this claim has a handle on the task actually doing the work
    instead of only a phase to poll. ``ignore_fence`` is for teardown's own
    claim — the fence exists to keep OTHERS out, not to lock teardown out of the
    topic it is tearing down.
    """
    async with creation_lock(flow.user_id, flow.thread_id):
        return try_transition_locked(
            flow,
            expect=OPEN_PHASES,
            to=phase,
            acquisition=True,
            ignore_fence=ignore_fence,
            cancellable=cancellable,
        )


@dataclass(frozen=True)
class _CompletionNote:
    """Proof that ONE specific flow generation's bind tail completed."""

    generation: int
    window_id: str
    at: float


@dataclass(frozen=True)
class _BaselineNote:
    """What the topic was bound to when ONE flow generation was installed."""

    generation: int
    binding: str | None
    at: float


# Bounded: consumed on read, dropped with the flow, and swept by age/cap so a
# note can never outlive the flow it describes (review r3 P2-1).
_COMPLETION_NOTE_TTL_S: Final[float] = 300.0
_COMPLETION_NOTE_CAP: Final[int] = 64


def _note_completion(flow: TrustFlow) -> None:
    """Record that THIS flow generation's bind completed (review r2 P2-A).

    Written ONLY by a trust-flow bind tail — never by an ordinary binding — and
    qualified by generation + window so a later flow on the same topic can never
    consume an older one's note.
    """
    _prune_completion_notes()
    _completed_binds[(flow.user_id, flow.thread_id)] = _CompletionNote(
        generation=flow.generation, window_id=flow.created_wid, at=_wall()
    )


def _evidence_pinned(key: _FlowKey, note: "_CompletionNote | _BaselineNote") -> bool:
    """EVIDENCE FOR A LIVE MATCHING GENERATION MUST NOT EXPIRE (review r10 P1-A).

    The note is the ONLY durable proof that a synchronous ``bind_thread``
    committed, and the tail that wrote it can lawfully sit at the payload replay
    for far longer than the TTL — a replay waits on Telegram and on the pane. If
    the note aged out underneath a flow that is still live, ``bind_committed``
    went False at all three outcome sites and the round-9 misclassification came
    straight back: a committed bind reported as a failure, ``/start`` skipping
    the bound-topic teardown, files deleted under a live binding.

    So the TTL only ever prunes evidence NOBODY IS STILL USING: a note whose
    generation and window match a flow still in the registry is PINNED, and
    starts ageing only once that flow is gone. The wall-clock bound is preserved
    for every note that is genuinely orphaned, which is the case the TTL exists
    for.
    """
    flow = _flows.get(key)
    if flow is None:
        return False
    if flow.generation != note.generation:
        return False
    # A completion note names the window it proves; a baseline names what the
    # topic was bound to BEFORE the flow, which is not this flow's window — for
    # a baseline the generation match is the whole test.
    if isinstance(note, _CompletionNote):
        return flow.created_wid == note.window_id
    return True


def _note_expired(key: _FlowKey, note: "_CompletionNote | _BaselineNote") -> bool:
    """True when the note is past its TTL *and* no live flow is relying on it."""
    if _wall() - note.at <= _COMPLETION_NOTE_TTL_S:
        return False
    return not _evidence_pinned(key, note)


def _prune_completion_notes() -> None:
    for store in (_completed_binds, _binding_baselines):
        for key, note in list(store.items()):
            if _note_expired(key, note):
                store.pop(key, None)
        while len(store) > _COMPLETION_NOTE_CAP:
            # The cap must not evict evidence a LIVE flow still needs either
            # (review r10 P1-A) — same rule as the TTL. Spend the UNPINNED notes
            # first; only at true capacity, with every note pinned, does the
            # memory bound win over the oldest one.
            unpinned = [
                (key, note)
                for key, note in store.items()
                if not _evidence_pinned(key, note)
            ]
            pool = unpinned or list(store.items())
            oldest = min(pool, key=lambda kv: kv[1].at)[0]
            store.pop(oldest, None)


def _consume_completion(
    key: _FlowKey, *, generation: int | None = None, window_id: str | None = None
) -> bool:
    """Pop this topic's completion note when it MATCHES (single-use).

    A note for a different generation or window is left in place — consuming it
    would let an unrelated teardown claim someone else's completion.
    """
    note = _completed_binds.get(key)
    if note is None:
        return False
    if _note_expired(key, note):
        _completed_binds.pop(key, None)
        return False
    if generation is not None and note.generation != generation:
        return False
    if window_id is not None and note.window_id != window_id:
        return False
    _completed_binds.pop(key, None)
    return True


def _peek_completion(
    key: _FlowKey, *, generation: int | None = None, window_id: str | None = None
) -> bool:
    """``_consume_completion`` WITHOUT the pop — the same match rules.

    Outcome derivation must be able to read the evidence without spending it:
    the note is single-use because teardown consumes it to decide accounting,
    and a derivation that consumed it would silently steal that decision.
    """
    note = _completed_binds.get(key)
    if note is None:
        return False
    if _note_expired(key, note):
        return False
    if generation is not None and note.generation != generation:
        return False
    if window_id is not None and note.window_id != window_id:
        return False
    return True


def bind_committed(flow: TrustFlow) -> bool:
    """DURABLE EVIDENCE that this flow's bind actually landed (review r9 P1-A).

    Task flags are NOT sufficient. ``session_mgr.bind_thread`` is SYNCHRONOUS,
    so a cancellation aimed at the tail cannot interrupt it — it lands at the
    NEXT await (the payload replay, or the final card edit), by which time the
    binding AND this note have both been written. The task then reports
    ``cancelled()``, and deriving "failed" from that flag alone made the caller
    run the failure arm: ``/start`` skipped the bound-topic teardown while a
    LIVE binding survived the reset, and the pending payload's files were
    deleted underneath it.

    The note is written in the SAME synchronous stretch as ``bind_thread`` —
    ``bind_thread`` → ``_note_completion`` → ``_release_tokens`` with no await
    between — so "note present" is exactly equivalent to "bind committed", with
    no window in which one is true and the other is not. It is generation- AND
    window-qualified, so an older flow's note can never be read as this one's.
    """
    return _peek_completion(
        (flow.user_id, flow.thread_id),
        generation=flow.generation,
        window_id=flow.created_wid,
    )


def _release_tokens(flow: TrustFlow) -> None:
    """Fix 7's non-dispatch terminal token op: ROW REMOVAL, ledger untouched.

    Disclosed residue: a token consumption tombstones its row BEFORE the
    dispatch runs, and the busy-lock ``accepted → not_advanced`` downgrade fires
    only when the lock is OBSERVED busy — so an exception/cancellation landing
    after the ``accepted`` write can leave an ``accepted`` row that later reads
    as ``unknown`` (refresh-only). Fail-closed: no redispatch can result.
    """
    decision_token.teardown_route(flow.user_id, flow.thread_id, flow.created_wid)


def _drop_entry(flow: TrustFlow, *, delete_attachments: bool = True) -> None:
    """Drop the ownership token LAST (never before the window is settled).

    This is normally a TERMINAL drop — the stashed first-turn payload was never
    delivered — so the downloaded attachment files go with it. Without that,
    a flow that terminalized before its caller could read the entry (the
    forced-teardown path, which drops it first) leaked those files.

    ``delete_attachments=False`` is for the ONE case where the payload's fate is
    genuinely UNKNOWN (review r9 P1-A): a bind that committed but whose tail was
    cancelled at the replay await. Deleting there would destroy the user's
    message on a topic that is now live and usable, so the files are kept and
    the card says so.
    """
    entry = picker_entry(flow.user_data, flow.thread_id)
    if entry is not None and entry.get(TRUST_GENERATION_KEY) == flow.generation:
        dropped = drop_picker_entry(flow.user_data, flow.thread_id)
        if dropped is not None and delete_attachments:
            _delete_pending_attachments(dropped)


def _delete_pending_attachments(entry: dict[str, Any]) -> None:
    """Best-effort deletion of an undelivered payload's downloaded files."""
    for attachment in list(entry.get("_pending_thread_attachments") or []):
        path = getattr(attachment, "path", None)
        if path is None and isinstance(attachment, dict):
            path = attachment.get("path")
        if not isinstance(path, (str, Path)):
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("failed to delete pending attachment %s: %s", path, e)


def _drop_flow(flow: TrustFlow) -> None:
    key = (flow.user_id, flow.thread_id)
    if _flows.get(key) is flow:
        _flows.pop(key, None)
        _restamp_unpinned_evidence(key, flow)


def _restamp_unpinned_evidence(key: _FlowKey, flow: TrustFlow) -> None:
    """Start the TTL clock when the flow ENDS, not when the bind happened.

    The completion note is PINNED while its flow is live (review r10 P1-A), so
    dropping the flow is the moment it becomes eligible to age. Without this it
    would un-pin already-expired: a bind that committed early in a long replay
    would have its evidence vanish in the same breath the flow was dropped —
    and the teardown that dropped it consults ``_completion_won`` immediately
    afterwards to decide whether completion won. Re-stamping makes the TTL mean
    what it says, "how long we keep evidence nobody is using", and keeps the
    wall-clock bound intact.
    """
    now = _wall()
    completion = _completed_binds.get(key)
    if completion is not None and completion.generation == flow.generation:
        _completed_binds[key] = replace(completion, at=now)
    baseline = _binding_baselines.get(key)
    if baseline is not None and baseline.generation == flow.generation:
        _binding_baselines[key] = replace(baseline, at=now)


async def _terminal_cleanup(
    flow: TrustFlow,
    bot: Any,
    tmux_mgr: Any,
    *,
    reason: str,
    body: str,
    session_mgr: Any = None,
    allow_completion: bool = True,
) -> CleanupOutcome:
    """Guarded cleanup + honest card edit + token teardown. Caller drops entry.

    **A SPARED_REGISTERED outcome WINS, it does not merely spare** (review r2
    P1-B). The linearization contract says a registration observed at or before
    the fresh session-map read beats the cleanup — so the flow flips into the
    COMPLETION tail (bind the topic, replay the queued payload) instead of being
    terminalized, which would have left a live registered window unbound and
    silently discarded the user's first message. ``session_mgr`` is required for
    that flip; without it the spare is reported honestly and the window is left
    alive and unbound — every in-tree caller now passes one, so that degradation
    is a defensive default rather than a live path.

    ``allow_completion=False`` is passed by the callers that are ALREADY handling
    a failed completion: re-entering the completion seam from there would recurse
    (failed tail → cleanup → still registered → tail → …). Those paths report the
    spare honestly instead.
    """
    outcome = await cleanup_created_window(
        flow.created_wid,
        flow.window_name,
        tmux_mgr,
        reason=reason,
        session_mgr=session_mgr or flow.session_mgr,
        owner_guard=_owner_guard_for(flow),
    )
    if (
        outcome is CleanupOutcome.SPARED_REGISTERED
        and session_mgr is not None
        and allow_completion
    ):
        logger.info(
            "trust flow cleanup lost to a registration — completing the bind "
            "instead (window=%s, thread=%s, reason=%s)",
            flow.created_wid,
            flow.thread_id,
            reason,
        )
        # We hold ``cancelling``; hand the flow to the completion tail through
        # the SAME seam every other completion uses. A lost CAS means another
        # actor owns the flow now, and it is theirs to finish.
        await _run_completion_tail(
            flow, bot, session_mgr, expect=frozenset({PHASE_CANCELLING})
        )
        return outcome
    _release_tokens(flow)
    note = cleanup_note(outcome, flow.window_name, flow.created_wid)
    await _edit_card(flow, bot, f"{body}\n\n{note}", None)
    await transition(flow, expect=frozenset({PHASE_CANCELLING}), to=PHASE_TERMINAL)
    flow.terminal_committed = True
    return outcome


async def _run_completion_tail(
    flow: TrustFlow,
    bot: Any,
    session_mgr: Any,
    *,
    expect: frozenset[str] = OPEN_PHASES,
) -> bool:
    """CAS into ``completing_bind``, then run the retained tail to completion.

    The ONE seam that enters the completion tail, so the tail is always a
    SEPARATELY-TRACKED task teardown can await (never an inline coroutine it
    would have to cancel). Returns False when the CAS LOSES — a registration
    landing while ``cancelling`` holds the claim loses, and that loss is
    accepted by the same linearization argument as the fresh-read one: the
    window is already dying.
    """
    async with creation_lock(flow.user_id, flow.thread_id):
        if not try_transition_locked(
            flow, expect=expect, to=PHASE_COMPLETING_BIND, acquisition=True
        ):
            return False
        inner = asyncio.create_task(_complete_bind(flow, bot, session_mgr))
        flow.bind_task = inner
    # The shield PROPAGATES the tail's exception and its cancellation (review r5
    # P1-C): awaiting it bare meant the outcome branch below was dead code on
    # exactly the paths it was written for. Swallow the tail's own failure here
    # and decide from its recorded outcome; a cancellation aimed at US is
    # re-raised (the retained task keeps running, and whoever cancelled us owns
    # the flow).
    try:
        await asyncio.shield(inner)
    except asyncio.CancelledError:
        if not inner.cancelled():
            raise
    except Exception as e:  # noqa: BLE001
        logger.error(
            "trust completion tail raised (window=%s, thread=%s): %s",
            flow.created_wid,
            flow.thread_id,
            e,
        )

    # FINALIZE on the tail's ACTUAL outcome (review r4 P2-B), UNCONDITIONALLY.
    # A tail that raised before ``bind_thread`` is NOT a completion: leaving the
    # flow in ``completing_bind`` would strand it, and treating it as success
    # would drop everything with the window unbound.
    if _bind_task_succeeded(inner):
        async with creation_lock(flow.user_id, flow.thread_id):
            if _flows.get((flow.user_id, flow.thread_id)) is flow:
                try_transition_locked(
                    flow,
                    expect=frozenset({PHASE_COMPLETING_BIND}),
                    to=PHASE_TERMINAL,
                )
                _release_tokens(flow)
                _drop_entry(flow)
                _drop_flow(flow)
        return True
    if bind_committed(flow):
        # THE BIND COMMITTED, the tail just did not get to finish (review r9
        # P1-A). ``bind_thread`` is synchronous, so a cancellation lands at the
        # NEXT await — the replay or the card edit — with the binding already
        # written. This is a COMPLETION, not a failure: the guarded cleanup must
        # not run (it would kill a window the topic is now bound to), and the
        # caller must do bound-topic accounting.
        await _settle_committed_but_unfinished_bind(flow, bot)
        return True
    # FAILED: take the claim back from ``completing_bind`` and settle the window
    # ourselves, with the honest ❌ — the same outcome branch the terminalizer
    # runs, so every caller of the tail gets identical semantics.
    logger.error(
        "trust completion tail did not bind (window=%s, thread=%s) — running the "
        "guarded cleanup",
        flow.created_wid,
        flow.thread_id,
    )
    if await transition(
        flow,
        expect=frozenset({PHASE_COMPLETING_BIND}),
        to=PHASE_CANCELLING,
    ):
        await _terminal_cleanup(
            flow,
            bot,
            flow.tmux_mgr,
            reason="completion tail failed",
            body=(
                "❌ The session started but I couldn't finish binding this "
                "topic.\n\nSend your message again to retry."
            ),
            session_mgr=session_mgr,
            allow_completion=False,
        )
        async with creation_lock(flow.user_id, flow.thread_id):
            if _flows.get((flow.user_id, flow.thread_id)) is flow and (
                flow.phase == PHASE_TERMINAL
            ):
                _drop_entry(flow)
                _drop_flow(flow)
    return True


def _replay_for(flow: TrustFlow) -> Any:
    """The flow's pending-payload replay — injected, or the production default.

    Resolved lazily so the ``trust_flow → inbound_telegram`` import edge stays
    function-local (the repo's cycle-dodging convention).
    """
    if flow.replay is not None:
        return flow.replay
    from .inbound_telegram import _flush_pending_route_payload

    return _flush_pending_route_payload


async def _drop_orphan(user_id: int, thread_id: int, orphaned: TrustFlow) -> None:
    """Commit terminal and drop an orphaned flow. Idempotent, lock-taking."""
    async with creation_lock(user_id, thread_id):
        if _flows.get((user_id, thread_id)) is orphaned:
            try_transition_locked(
                orphaned, expect=NONTERMINAL_PHASES, to=PHASE_TERMINAL
            )
            _release_tokens(orphaned)
            _drop_entry(orphaned)
            _drop_flow(orphaned)


def _owner_guard_for(flow: TrustFlow) -> Callable[[], Awaitable[bool]]:
    """A last-moment "do I still own this window?" check for the guarded kill.

    Evaluated immediately before ``kill_window`` (review r9 P1-B). The hazard is
    a cleanup that outlives the flow that started it: it is bounded and then
    cancelled, but a straggler already past its bound could still reach the kill
    — by which time a NEW flow may have adopted the same window id, and the kill
    would destroy a live session that is not ours.

    The registry read is a single dict lookup with no ``await``, so it is
    already atomic against every other actor — and it deliberately does NOT take
    the creation lock: ``creation_lock`` is a plain non-reentrant
    ``asyncio.Lock``, and the guard runs inside ``_terminal_cleanup``, which
    several callers reach from paths that must stay lock-free. Taking it here
    would risk a deadlock to buy nothing a single read does not already give.
    """

    async def _still_owns() -> bool:
        current = _flows.get((flow.user_id, flow.thread_id))
        if current is None or current is flow:
            return True
        # A DIFFERENT flow owns this topic now. It only blocks us if it has
        # adopted the very window we were about to kill.
        return current.created_wid != flow.created_wid

    return _still_owns


async def _revalidate_bind_preconditions(flow: TrustFlow, session_mgr: Any) -> None:
    """Re-prove EVERY precondition after the settlement wait (review r11 P1-B).

    Three questions, all of which could have changed while we were parked:

    1. **Does the window still exist?** A fresh probe, not the stale object we
       resolved before the wait — binding a topic to a corpse is the exact harm
       the gate exists to prevent.
    2. **Is this topic still free?** Exclusivity is re-read: a competing flow or
       a manual bind may have claimed it, and two topics on one window is a
       routing corruption (1 topic = 1 window = 1 session).
    3. **Do we still own the picker?** The generation check runs at the top of
       the tail, but the wait reopens the window in which a newer generation can
       take over.

    Raises ``TrustBindRefused``, which lands BEFORE ``bind_thread`` — so it is
    always a clean nothing-happened and the caller's failure arm owns the copy.
    """
    finder = getattr(flow.tmux_mgr, "find_window_by_id", None)
    if finder is not None and await finder(flow.created_wid) is None:
        raise TrustBindRefused(
            f"window {flow.created_wid} no longer exists after the adoption wait"
        )

    owner = getattr(session_mgr, "get_window_for_thread", None)
    if owner is not None:
        bound = owner(flow.user_id, flow.thread_id)
        if bound is not None and bound != flow.created_wid:
            raise TrustBindRefused(
                f"thread {flow.thread_id} was bound to {bound} during the adoption wait"
            )

    entry = picker_entry(flow.user_data, flow.thread_id)
    if entry is None or entry.get(TRUST_GENERATION_KEY) != flow.generation:
        raise TrustBindRefused(
            "this flow no longer owns the picker after the adoption wait"
        )


async def _settle_committed_but_unfinished_bind(flow: TrustFlow, bot: Any) -> None:
    """Terminalize a flow whose bind COMMITTED but whose tail never finished.

    The window is BOUND and usable, so there is no guarded cleanup to run — only
    honest accounting and honest copy. The payload's fate is genuinely UNKNOWN:
    the note is written BEFORE the replay, so a tail cancelled at that await may
    or may not have delivered the first message, and nothing durable records
    which. We therefore KEEP the downloaded attachment files (deleting them
    would destroy the user's message on a topic that now works) and say plainly
    that the message may not have arrived. Re-running the replay instead was
    rejected: it is not safely re-derivable — a replay that DID run would be
    delivered twice, and a duplicate first message is worse than an honest
    prompt to resend.

    ACCOUNTING vs USABILITY are decided SEPARATELY (review r10 P2-B). The note
    is the completion evidence and it always wins the ACCOUNTING question — the
    bind did commit, so the caller must do bound-topic teardown and the payload
    is uncertain. But the topic may since have been UNBOUND (``/unbind``, or a
    topic close whose own teardown ran before ours), and telling the user "send
    messages here" on an unbound topic is simply false. So the COPY is chosen
    from the CURRENT binding, not from the note.
    """
    logger.warning(
        "trust completion tail was cut short AFTER a committed bind "
        "(window=%s, thread=%s) — keeping the binding and the pending files",
        flow.created_wid,
        flow.thread_id,
    )
    payload_warning = (
        "\n\n⚠️ Your pending message may not have been delivered — resend it if "
        "you don't see a reply."
    )
    # THREE arms, not two (review r11 P2). ``binding_is_current_route`` False
    # conflated "unbound" with "REBOUND to a different live window", and the
    # unbound copy told a user whose topic is perfectly usable that there is
    # nothing to send to — false, and the opposite of actionable.
    current_binding: str | None = None
    owner = getattr(flow.session_mgr, "get_window_for_thread", None)
    if owner is not None:
        current_binding = owner(flow.user_id, flow.thread_id)
    if current_binding == flow.created_wid:
        body = f"✅ {flow.create_message}\n\nCreated. Send messages here."
    elif current_binding is not None:
        body = (
            f"✅ {flow.create_message}\n\nCreated — and this topic is now bound "
            "to another session, so messages here go there."
        )
    else:
        body = (
            f"✅ {flow.create_message}\n\nCreated — but this topic was unbound or "
            "closed straight afterwards, so there is nothing to send to here."
        )
    await _edit_card(flow, bot, body + payload_warning, None)
    # The disclosure above is the LAST word on this card (review r10 P2-A): a
    # later generic "setup was cancelled" edit would bury the one line the user
    # can act on.
    flow.card_finalized = True
    flow.terminal_committed = True
    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is flow:
            try_transition_locked(
                flow,
                expect=NONTERMINAL_PHASES,
                to=PHASE_TERMINAL,
            )
            _release_tokens(flow)
            # KEEP the attachments — see above.
            _drop_entry(flow, delete_attachments=False)
            _drop_flow(flow)


def _bind_task_succeeded(task: asyncio.Task[Any]) -> bool:
    """True only when the retained tail RAN TO COMPLETION without raising."""
    if not task.done() or task.cancelled():
        return False
    return task.exception() is None


async def _terminal_spare(flow: TrustFlow, bot: Any, *, body: str) -> None:
    """The GLOBAL observation ceiling's terminal action: SPARE + release.

    NEVER kills. The window stays alive, creation ownership is released (entry +
    tokens), and the card carries recovery copy — fail-open preserved, and no
    permanent ownership of the topic.
    """
    _release_tokens(flow)
    await _edit_card(flow, bot, body, None)
    await transition(flow, expect=frozenset({PHASE_CANCELLING}), to=PHASE_TERMINAL)
    flow.terminal_committed = True


# ── Fix 3: the completion tail ───────────────────────────────────────────────


async def _complete_bind(flow: TrustFlow, bot: Any, session_mgr: Any) -> None:
    """The EXACT tail of today's creation flow, run from the WAIT task.

    Owner recheck → monitor pre-register → ``bind_thread`` → pending-payload
    replay → final card edit. Runs as a SEPARATELY-TRACKED inner task that
    teardown AWAITS (never cancels) so a half-bound topic can never be stranded.
    """

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

    # THE ADOPTION GATE (review r10 P1-B). The completion bind is an adoption
    # too. A kill for this id whose async wrapper was cancelled can still be
    # inside libtmux, and it resolves its target BY ID — so binding the topic
    # first would hand the user a window a straggler is about to remove. Wait
    # for any in-flight kill to settle; the bind itself stays SYNCHRONOUS and
    # immediately followed by the note, which is what makes `bind_committed`
    # exact.
    #
    # EVERY PRECONDITION IS RE-VALIDATED AFTER THE WAIT (review r11 P1-B). The
    # settlement wait is not instantaneous, and everything checked before it —
    # the window's existence, this topic's freedom, this flow's ownership of the
    # picker — can change while we are parked. Ignoring the wait's own boolean
    # was the same class: a wait that TIMED OUT meant the kill could still land,
    # which is precisely when binding is least safe.
    kill_gate = getattr(flow.tmux_mgr, "await_kill_settled", None)
    if kill_gate is not None and not await kill_gate(flow.created_wid):
        raise TrustBindRefused(
            f"a kill for window {flow.created_wid} did not settle — refusing to "
            "bind a window that may be about to disappear"
        )
    await _revalidate_bind_preconditions(flow, session_mgr)

    session_mgr.bind_thread(
        flow.user_id, flow.thread_id, flow.created_wid, window_name=flow.window_name
    )
    # The bind is DONE. Leave the discoverable note BEFORE the payload replay so
    # a teardown racing the terminalizer can still learn completion won even if
    # the replay itself raises (review r2 P2-A).
    _note_completion(flow)
    _release_tokens(flow)

    route = (flow.user_id, flow.thread_id, flow.created_wid)
    pending = await _replay_for(flow)(route, flow.user_data)
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
    slice_started: float | None = None
    while True:
        # THE PER-SLICE FLOOR (review r8 P2-B). ``wait_for_session_map_entry``
        # returns IMMEDIATELY once the entry exists, so a slice that stays in
        # the loop after a REGISTERED classification — the completion tail
        # losing its CAS to a claim-holder, say — costs nothing and the loop
        # rereads session_map.json at full speed for as long as that claim is
        # held. Sleeping the REMAINDER of the previous slice (rather than an
        # unconditional sleep) enforces the floor without adding latency to the
        # common unregistered slice, which already spends SLICE_S inside the
        # poll. It runs regardless of which branch continued.
        if slice_started is not None:
            spent = time.monotonic() - slice_started
            if spent < SLICE_S:
                await asyncio.sleep(SLICE_S - spent)
        slice_started = time.monotonic()
        # A flow marked terminal (a teardown, an entry clear) is not ours any
        # more — leave at once rather than spending another slice on a pane we
        # may no longer touch (review r4 P1-C).
        if flow.phase == PHASE_TERMINAL:
            return
        registered = bool(
            await _bounded(
                session_mgr.wait_for_session_map_entry(
                    flow.created_wid, timeout=SLICE_S, interval=SLICE_S
                ),
                what="wait_for_session_map_entry",
                window_id=flow.created_wid,
            )
        )
        now = time.monotonic()
        if not registered and now - last_pane_poll >= PANE_POLL_EVERY_S:
            last_pane_poll = now
            pane_text, pane_command = await _probe_pane(tmux_mgr, flow.created_wid)
        kind = classify_slice(
            registered=registered,
            pane_command=pane_command,
            pane_text=pane_text,
        )

        if kind is SliceKind.REGISTERED:
            # A registration landing while a cleanup holds ``cancelling`` LOSES
            # the CAS (the window is already being settled) — the same accepted
            # loss class as the fresh-read linearization point.
            #
            # But losing must NOT end the loop (review r6 P1-C). Returning here
            # left the flow with NO OBSERVER the moment the winner aborted and
            # restored an open phase: no ceilings, no registration, no bind.
            # WAIT keeps observing until it sees a TERMINAL phase committed by
            # whoever won — the check at the top of the slice — so a restored
            # flow is automatically observed again.
            #
            # It FALLS THROUGH rather than ``continue``-ing (review r7 P2): a
            # ``continue`` jumped over the deadline block at the bottom of the
            # slice, so if the winner never committed terminal the loop spun on
            # session_map rereads with the global ceiling unreachable. Falling
            # through re-enters the ceiling evaluation every slice; the
            # registration budget's arm is keyed on RUNNING / OTHER_SURFACE, so
            # it stays inert here while the ceilings stay live.
            if await _run_completion_tail(flow, bot, session_mgr):
                return

        if kind is SliceKind.SHELL:
            if not await claim_terminal(flow):
                continue
            tail = pane_tail(pane_text)
            body = (
                "❌ Claude exited before the session started.\n\n"
                "Send your message again to retry."
            )
            if tail:
                body += f"\n\n```\n{tail}\n```"
            await _terminal_cleanup(
                flow,
                bot,
                tmux_mgr,
                reason="pane returned to a shell",
                body=body,
                session_mgr=session_mgr,
            )
            return

        if kind is SliceKind.TRUST_FRAME:
            if not lane_enabled():
                if not await claim_terminal(flow):
                    continue
                await _terminal_cleanup(
                    flow,
                    bot,
                    tmux_mgr,
                    reason="trust lane disabled",
                    session_mgr=session_mgr,
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
                # tokens, back to ``awaiting_trust``. CAS first: a cleanup or a
                # tap may have claimed the flow since the slice was classified.
                if await transition(
                    flow,
                    expect=frozenset({PHASE_AWAITING_REGISTRATION}),
                    to=PHASE_AWAITING_TRUST,
                ):
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
            if await transition(
                flow,
                expect=frozenset({PHASE_AWAITING_TRUST}),
                to=PHASE_AWAITING_REGISTRATION,
            ):
                flow.awaiting_registration_at = _wall()
                flow.trust_deadline = None
                _rebase_registration_budget(flow, reason="prompt answered in tmux")

        now = _wall()
        # The GLOBAL observation ceiling applies in EVERY phase, including
        # ``dispatching`` (review r2 P1-C): a wedged callback must not park the
        # flow forever. It is safe there precisely because its terminal action
        # only SPARES — it never kills a window a tap may be mid-transaction on.
        if now >= flow.global_deadline:
            # DEFER under an active dispatch rather than sparing underneath it
            # (review r3 P1-3): the callback's ``finally`` guarantees the phase
            # leaves ``dispatching``, so this defers at most one bounded
            # dispatch. Anything else must WIN the CAS before acting.
            if not await claim_terminal(flow):
                continue
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
        if flow.phase == PHASE_DISPATCHING:
            # A dispatch transaction owns the pane; the KILL-capable budgets are
            # suspended under it (the global ceiling above still is not).
            continue
        if flow.trust_deadline is not None and now >= flow.trust_deadline:
            if not await claim_terminal(flow):
                continue
            await _terminal_cleanup(
                flow,
                bot,
                tmux_mgr,
                reason="trust prompt ceiling",
                body=(
                    "⏰ Timed out waiting for you to trust the folder.\n\n"
                    "Send your message again when you're ready."
                ),
                session_mgr=session_mgr,
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
            if not await claim_terminal(flow):
                continue
            await _terminal_cleanup(
                flow,
                bot,
                tmux_mgr,
                reason=reason,
                body=body,
                session_mgr=session_mgr,
            )
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
        await _terminalize(flow, bot, tmux_mgr, session_mgr, cancelled=cancelled)


async def _terminalize(
    flow: TrustFlow,
    bot: Any,
    tmux_mgr: Any,
    session_mgr: Any,
    *,
    cancelled: bool,
) -> None:
    """The WAIT task's terminalizer — NO side effect runs under the lock.

    Wave 3 restructure (review r3 P1-4/P1-5). The previous shape ran the guarded
    cleanup — a tmux kill, a Telegram edit, and potentially a whole completion
    tail — INSIDE the creation lock, which both violated the spec's teardown
    choreography and made a ``SPARED_REGISTERED`` completion impossible to run
    correctly. Now:

      1. a retained completion tail is shield-awaited FIRST (dropping the entry
         underneath it would strand a half-bound topic);
      2. the lock is taken ONLY to CAS ``open → cancelling`` — losing means
         another actor (teardown, a cleanup, a tap) owns the flow and will
         finish it, so this returns and touches nothing;
      3. the cleanup + card edit run OUTSIDE the lock, and a SPARED_REGISTERED
         outcome CASes on into ``completing_bind`` through the one completion
         seam;
      4. the lock is retaken only to drop the entry and the flow.
    """
    inner = flow.bind_task
    if inner is not None and not inner.done():
        # BOUNDED (review r8 P1-B). An unbounded shield-await here was the other
        # half of the hung-tail deadlock: teardown cancels and awaits the WAIT
        # task, whose terminalizer sat on a bind tail that never finished, so
        # topic-close and shutdown hung forever. The tail gets a real grace and
        # is then CANCELLED. A killed tail is not a task-level success — but it
        # may still have COMMITTED its bind (review r9 P1-A), which is why
        # `bind_ok` below consults the durable evidence and not just the flags.
        try:
            await asyncio.wait_for(asyncio.shield(inner), timeout=BIND_TAIL_GRACE_S)
        except asyncio.CancelledError:
            if not inner.cancelled():
                raise
        except (TimeoutError, Exception):  # noqa: BLE001
            pass
        if not inner.done():
            logger.warning(
                "trust terminalizer's bind tail outlived its grace "
                "(thread=%s, window=%s) — cancelling it",
                flow.thread_id,
                flow.created_wid,
            )
            await _cancel_and_await(inner)
    # The tail's ACTUAL outcome decides — a bind that raised before
    # ``bind_thread`` is not a success (review r4 P2-B) — where "actual" means
    # the DURABLE EVIDENCE, not only the task flags (review r9 P1-A). A tail
    # cancelled at the replay await has already written the binding.
    bind_ok = inner is not None and _bind_task_succeeded(inner)
    if inner is not None and not bind_ok and bind_committed(flow):
        await _settle_committed_but_unfinished_bind(flow, bot)
        return

    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is not flow:
            return
        if flow.phase == PHASE_TERMINAL:
            _release_tokens(flow)
            _drop_entry(flow)
            _drop_flow(flow)
            return
        if flow.phase == PHASE_COMPLETING_BIND:
            if bind_ok:
                try_transition_locked(
                    flow,
                    expect=frozenset({PHASE_COMPLETING_BIND}),
                    to=PHASE_TERMINAL,
                )
                _release_tokens(flow)
                _drop_entry(flow)
                _drop_flow(flow)
                return
            # The tail FAILED. Take the claim from ``completing_bind`` and run
            # the guarded cleanup, so the window is settled and the card honest
            # instead of the topic being dropped with the window unbound.
            if not try_transition_locked(
                flow,
                expect=frozenset({PHASE_COMPLETING_BIND}),
                to=PHASE_CANCELLING,
            ):
                return
        elif not try_transition_locked(
            flow, expect=OPEN_PHASES, to=PHASE_CANCELLING, acquisition=True
        ):
            # Another actor holds the claim (or a teardown has fenced the flow)
            # — it owns the cleanup AND the drop.
            return

    if not bind_ok and flow.bind_task is not None:
        reason, body = (
            "completion tail failed",
            "❌ The session started but I couldn't finish binding this topic."
            "\n\nSend your message again to retry.",
        )
    elif cancelled:
        reason, body = "creation flow torn down", "⚠️ This session setup was cancelled."
    else:
        reason, body = (
            "creation flow failed",
            "❌ Something went wrong while setting up this session.\n\n"
            "Send your message again to retry.",
        )
    outcome = await _terminal_cleanup(
        flow,
        bot,
        tmux_mgr,
        reason=reason,
        body=body,
        session_mgr=session_mgr,
        allow_completion=bind_ok or flow.bind_task is None,
    )
    if outcome is CleanupOutcome.SPARED_REGISTERED:
        # The completion tail ran (or another actor took the flow); either way
        # the entry belongs to whoever owns it now.
        return

    async with creation_lock(flow.user_id, flow.thread_id):
        if _flows.get((flow.user_id, flow.thread_id)) is flow:
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
    entry_token: str | None = None,
    card_chat_id: int | None = None,
    card_msg_id: int | None = None,
    replay: Any = None,
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
        # IDENTITY, not presence (review r2 P1-A). Teardown clears the entry —
        # and with it this token — inside THIS lock, so a callback that started
        # before the clear either wins the lock first (and is then torn down
        # normally) or finds a token mismatch here and aborts. The mismatch case
        # covers the ABA hijack too: a REPLACEMENT entry a fresh inbound created
        # is a different identity and must never be commandeered.
        live_token = entry.get(ENTRY_TOKEN_KEY)
        if entry_token is not None and live_token != entry_token:
            logger.warning(
                "Refusing to start a trust flow for window %s: thread %s's "
                "picker entry was replaced (entry-token mismatch)",
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
        # The card is the one the TAP will arrive on, so prefer the caller's
        # explicit coordinates (the callback knows its own message) and fall
        # back to what the picker recorded (review r2 P2-E).
        resolved_card_chat_id = (
            card_chat_id if card_chat_id is not None else entry.get(CARD_CHAT_ID_KEY)
        )
        resolved_card_msg_id = (
            card_msg_id if card_msg_id is not None else entry.get(CARD_MSG_ID_KEY)
        )
        now = _wall()
        budget = registration_budget_s()
        flow = TrustFlow(
            generation=generation,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            card_chat_id=resolved_card_chat_id,
            card_msg_id=resolved_card_msg_id,
            created_wid=created_wid,
            window_name=window_name,
            selected_path=selected_path,
            create_message=create_message,
            resume_id=None,
            cli_version=cli_version,
            user_data=user_data,
            entry_token=live_token if isinstance(live_token, str) else None,
            bot=bot,
            tmux_mgr=tmux_mgr,
            session_mgr=session_mgr,
            replay=replay,
            started_at=now,
            registration_deadline=now + budget,
            global_deadline=now
            + max(budget, config.trust_prompt_ceiling_s)
            + GLOBAL_CEILING_MARGIN_S,
        )
        _flows[(user_id, thread_id)] = flow
        # PRE-FLOW BASELINE for the completion fallback (review r3 P2-1): if the
        # topic is somehow ALREADY bound to this window, a later binding to it
        # proves nothing about this flow.
        try:
            _baseline = session_mgr.get_window_for_thread(user_id, thread_id)
        except Exception:  # noqa: BLE001
            _baseline = None
        _prune_completion_notes()
        _binding_baselines[(user_id, thread_id)] = _BaselineNote(
            generation=generation, binding=_baseline, at=now
        )
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
        if not try_transition_locked(
            flow,
            expect=frozenset({PHASE_AWAITING_TRUST}),
            to=PHASE_DISPATCHING,
            acquisition=True,
            cancellable=True,
        ):
            return TrustClaim(False, flow, "wrong_state", flow.phase)
        return TrustClaim(True, flow, None, PHASE_AWAITING_TRUST)


async def release_dispatch_claim(flow: TrustFlow, *, phase: str) -> bool:
    """Release a ``dispatching`` claim into ``phase``. CAS — never a blind set.

    Only the actor that HOLDS ``dispatching`` may release it, so a cleanup that
    won the flow in the meantime is never overwritten.
    """
    return await transition(flow, expect=frozenset({PHASE_DISPATCHING}), to=phase)


async def claim_for_cancel(
    user_id: int,
    thread_id: int | None,
    *,
    user_data: dict[str, Any] | None,
    card_generation: int | None = None,
) -> TrustClaim:
    """Claim ``open → cancelling`` (the Cancel tap's state claim).

    ``card_generation`` — the generation the TAPPED CARD carries — is validated
    inside the SAME lock hold as the acquisition (review r4 P1-A): checking it
    after the claim meant a stale card could claim a NEWER flow and then fail to
    release it, wedging the topic. A mismatch never claims at all.
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
        if card_generation is not None and card_generation != flow.generation:
            return TrustClaim(False, None, "stale_generation")
        previous = flow.phase
        if not try_transition_locked(
            flow,
            expect=OPEN_PHASES,
            to=PHASE_CANCELLING,
            acquisition=True,
            cancellable=True,
        ):
            return TrustClaim(False, flow, "wrong_state", flow.phase)
        # ``previous_phase`` is the ACQUIRED phase (review r5 P1-D): an abort
        # path must restore what it took, not assume ``awaiting_trust``.
        return TrustClaim(True, flow, None, previous)


async def cancel_flow(flow: TrustFlow, bot: Any, tmux_mgr: Any) -> CleanupOutcome:
    """Cancel: NO keystrokes into the pane — kill the window under the guard.

    The caller already holds ``cancelling``. Dispatching option 2 / Esc would
    exit Claude to a bare shell anyway, so killing the window is the same end
    state with zero keystroke risk.

    **ORDER (review r4 P2-A + r5 P1-D).** The WAIT task is left ALIVE across the
    guarded cleanup: holding ``cancelling`` already excludes it — every CAS it
    attempts loses — so it cannot interfere, and keeping it means an abort path
    that RESTORES the claim hands back a flow something still observes (its
    ceilings still fire, a registration is still noticed). Killing it first, as
    the previous shape did, left a restored-open flow that nothing watched. The
    WAIT task is cancelled + awaited only once the cleanup has COMMITTED and
    immediately BEFORE the final card edit, so no slice renderer can repaint
    over the terminal text.

    A cancel that RACES a registration is SPARED and routed into the ONE
    completion seam; a cancel that finds the topic ALREADY BOUND completes its
    own teardown inline (review r5 P2-A) rather than leaking the tokens, the
    entry and the flow.
    """
    outcome = await cleanup_created_window(
        flow.created_wid,
        flow.window_name,
        tmux_mgr,
        reason="trust card cancel",
        session_mgr=flow.session_mgr,
    )
    if outcome is CleanupOutcome.SPARED_REGISTERED or (
        outcome is CleanupOutcome.SPARED_BOUND and binding_is_current_route(flow)
    ):
        # The bind WON — either a registration landed under us, or THIS route is
        # already bound to the window (review r6 P2). Both are completions, so
        # both go through the ONE completion seam: the pending payload is
        # delivered through normal bound delivery and its files are NOT deleted,
        # because they were delivered.
        await _run_completion_tail(
            flow,
            bot,
            flow.session_mgr,
            expect=frozenset({PHASE_CANCELLING}),
        )
        return outcome

    # Every remaining outcome is terminal for this flow: settle the WAIT task,
    # then write the final card text, then release the claim into ``terminal``.
    await _cancel_and_await(flow.wait_task)
    _release_tokens(flow)
    if outcome is CleanupOutcome.KILLED:
        body = (
            "❌ Cancelled — the new tmux window was closed.\n\n"
            "Send a message here to start again."
        )
    elif outcome is CleanupOutcome.SPARED_BOUND:
        # COLLATERAL binding: the current-route arm returned above, so reaching
        # here means the window belongs to a DIFFERENT topic (review r6 P2).
        # This flow IS genuinely cancelled, and the copy names neither topic as
        # bound.
        body = (
            "❌ Cancelled — the tmux window is in use by another topic, so it "
            "was left running.\n\nSend a message here to start again."
        )
    else:
        body = "⚠️ Cancelled, but " + cleanup_note(
            outcome, flow.window_name, flow.created_wid
        )
    await _edit_card(flow, bot, body, None)
    await release_claim(flow, expect=PHASE_CANCELLING, to=PHASE_TERMINAL)
    flow.terminal_committed = True
    if outcome is CleanupOutcome.SPARED_BOUND:
        # Complete the teardown INLINE: the caller returns immediately on this
        # outcome, so nothing else would ever drop the entry or the flow.
        await finish_cancelled_flow(flow)
    return outcome


async def finish_cancelled_flow(flow: TrustFlow) -> None:
    """Terminal bookkeeping after a cancel: drop the entry and the flow.

    The WAIT task was already cancelled + awaited by ``cancel_flow`` (the
    terminal-edit-last ordering), so this is only the registry drop.
    """
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


async def _await_bind_tail(bind_task: asyncio.Task[Any]) -> bool:
    """Await a retained completion tail SAFELY; report its ACTUAL outcome.

    Two things are load-bearing here (review r2 P2-B):

    * **SHIELD.** Awaiting a Task PROPAGATES cancellation to it — verified
      empirically, and the opposite of the common assumption. A cancellation
      aimed at TEARDOWN would therefore abort a bind that is already underway,
      leaving exactly the half-bound topic the retained-task design exists to
      prevent. The shield keeps the tail running and re-raises into teardown.
    * **Never assume.** ``completed`` is derived from the task's real outcome —
      cancelled or raised is NOT a completion — instead of being set to True
      just because control returned.
    """
    try:
        await asyncio.shield(bind_task)
    except asyncio.CancelledError:
        # Ours, or the tail's. Re-raise only when the tail itself survived (the
        # cancellation was aimed at teardown); otherwise fall through and report
        # the tail's real, failed outcome.
        if not bind_task.cancelled():
            raise
    except Exception as e:  # noqa: BLE001
        logger.error("trust completion tail failed: %s", e)
        return False
    if not bind_task.done():
        return False
    if bind_task.cancelled():
        return False
    return bind_task.exception() is None


async def _await_claim_settled(user_id: int, thread_id: int, generation: int) -> bool:
    """Wait (bounded) for a flow to leave ANY task-held claim. True if it did.

    Review r6 P1-B: polling only for ``dispatching`` reported a LIVE
    ``cancelling`` claim settled on the very first poll, which is how a teardown
    could drop beneath another teardown's — or the WAIT terminalizer's —
    ``kill_window``.
    """
    deadline = _wall() + DISPATCH_SETTLE_BUDGET_S
    while _wall() < deadline:
        await asyncio.sleep(_DISPATCH_SETTLE_POLL_S)
        async with creation_lock(user_id, thread_id):
            flow = _flows.get((user_id, thread_id))
            if (
                flow is None
                or flow.generation != generation
                or flow.phase not in _TASK_CLAIMED_PHASES
            ):
                return True
    return False


def _completion_won(
    user_id: int,
    thread_id: int,
    *,
    observed_wid: str | None,
    generation: int | None = None,
    session_mgr: Any = None,
) -> bool:
    """True when the creation flow teardown OBSERVED completed its bind.

    Two proofs, either sufficient (review r2 P2-A): the single-use note the tail
    leaves behind, or the authoritative binding to the flow's OWN window — the
    terminalizer can remove the flow before teardown reacquires, and "flow gone
    + this window bound" is still a completion the caller must act on.

    ``observed_wid`` AND ``generation`` are both REQUIRED — either missing means
    no match. ``observed_wid`` is the reason this is never consulted on a cold
    call: a topic that never had a creation flow must not be reported as a
    completion just because it happens to be bound (that would make ``/start``
    run a bound-topic teardown on every ordinary topic). Only a flow this
    teardown actually saw can have completed under it.
    """
    if observed_wid is None or generation is None:
        # FAIL CLOSED (review r5 P2-B): without a generation we cannot tell this
        # flow's completion from an older one's note, so we claim nothing.
        return False
    if _consume_completion(
        (user_id, thread_id), generation=generation, window_id=observed_wid
    ):
        return True
    # The binding fallback needs a PRE-FLOW BASELINE (r3 P2-1), matched BY
    # GENERATION (r4 P2-C): a topic already bound to this window when THIS
    # generation installed proves nothing, and a baseline recorded for a
    # different generation must never be mistaken for ours. No baseline at all
    # (swept, or a generation we cannot identify) fails CLOSED.
    baseline = _binding_baselines.get((user_id, thread_id))
    if baseline is None:
        return False
    if _note_expired((user_id, thread_id), baseline):
        # An EXPIRED baseline is no baseline (review r5 P2-B) — reading a stale
        # one as "not pre-existing" would resurrect the very false positive the
        # baseline exists to prevent. It is PINNED while its own flow generation
        # is still live (review r10 P1-A), for the same reason the note is.
        _binding_baselines.pop((user_id, thread_id), None)
        return False
    if baseline.generation != generation:
        return False
    if baseline.binding == observed_wid:
        return False
    resolver = session_mgr if session_mgr is not None else session_manager
    try:
        return resolver.get_window_for_thread(user_id, thread_id) == observed_wid
    except Exception:  # noqa: BLE001
        return False


async def _settle_claim_task(
    flow: TrustFlow, *, budget: float = DISPATCH_SETTLE_BUDGET_S
) -> bool:
    """Settle a task-held claim with POSITIVE PROOF, not timer inference.

    Review r5 P1-B. The claim's owner is registered on the flow at acquisition,
    so teardown CANCELS it and AWAITS it: the callback's own ``finally``
    releases the claim, and the per-window send lock serialises any keystroke
    that was already in flight — so once the task is settled, nothing of it
    survives. The timer only bounds the await; it is no longer the mechanism.

    Falls back to polling the phase when there is no handle (a restart-era flow,
    or a claim taken by something that is not a task). Returns True when the
    flow is no longer in a task-held claim.
    """
    task = flow.claim_task
    cancellable = flow.claim_cancellable
    if task is not None and task is not asyncio.current_task() and not task.done():
        if cancellable:
            # A CALLBACK claim: cancelling it is how its ``finally`` runs.
            task.cancel()
        # A COOPERATIVE claim (another teardown, the WAIT terminalizer) is
        # AWAITED, never cancelled (review r6 P1-B) — cancelling one mid-cleanup
        # is exactly the "drop beneath another actor's kill_window" bug.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # The task's own finally is what releases the claim; whether it ends
            # cancelled or raising is not teardown's business.
            pass
    async with creation_lock(flow.user_id, flow.thread_id):
        current = _flows.get((flow.user_id, flow.thread_id))
        if current is not flow:
            return True
        if flow.phase not in _TASK_CLAIMED_PHASES:
            return True
    # No handle (or the task ignored the cancel): fall back to the bounded poll.
    return await _await_claim_settled(flow.user_id, flow.thread_id, flow.generation)


# The OVERALL wall-clock budget for one teardown (review r6 P1-A). A pass
# counter was not a time budget and its exhaustion ended in a BLIND registry
# drop — tokens released and the flow dropped without settling the owner,
# cancelling the WAIT task or cleaning the window. On expiry teardown
# FORCE-SETTLES the current phase instead; it never drops blindly.
TEARDOWN_BUDGET_S: float = 120.0


async def teardown_thread(
    user_id: int,
    thread_id: int | None,
    *,
    bot: Any = None,
    user_data: dict[str, Any] | None = None,
    reason: str = "topic teardown",
    session_mgr: Any = None,
    expected_wid: str | None = None,
    expected_generation: int | None = None,
) -> bool:
    """Tear down a live creation flow — a LOOP over the flow's CURRENT phase.

    Returns **True when the flow's COMPLETION WON** — the bind tail ran to the
    end — so the caller knows to run the normal BOUND-topic teardown on the
    now-bound topic.

    Review r5 P1-A: this used to decide once, from the phase it snapshotted, and
    then act on that stale reading — so a callback that claimed AFTER the
    snapshot was never re-handled, and the reacquisition dropped the flow
    unconditionally while a healthy callback was mid-side-effect. Now every
    reacquisition RE-DISPATCHES on what the phase IS NOW:

      * ``terminal``          → release tokens, drop the entry and the flow, done;
      * ``completing_bind``   → await the retained tail, then look again;
      * ``dispatching`` /
        ``cancelling``        → settle the CLAIM TASK (cancel + await), look again;
      * an OPEN phase         → claim it, cancel the WAIT task, run the guarded
                                cleanup, then look again.

    It exits only from a terminal reading (or a flow that is already gone).

    Two things keep that loop honest (review r6 P1-A). It raises a per-flow
    TEARDOWN FENCE on every pass, so no NEW claim can be acquired while it runs
    — a stream of taps cannot starve it, and a registration losing to the fence
    is the same accepted, documented loss class as one landing after the
    fresh-read linearization point. And its bound is a real WALL-CLOCK budget
    (``TEARDOWN_BUDGET_S``), whose expiry FORCE-SETTLES whatever phase the flow
    is in — settle the registered owner, force the claim, guarded cleanup, drop
    — never the blind registry drop a pass counter used to end in.
    """
    del bot, user_data, reason  # the flow record carries its own card + entry
    if thread_id is None:
        return False
    try:
        return await _teardown_loop(
            user_id,
            thread_id,
            session_mgr=session_mgr,
            expected_wid=expected_wid,
            expected_generation=expected_generation,
        )
    finally:
        # LOWER THE FENCE, and RELEASE OUR OWN CLAIM, on any exit that did not
        # commit terminal (review r7 P1-A). A cancelled or raising teardown used
        # to leave a LIVE flow permanently fenced — every future dispatch,
        # cancel, WAIT-cleanup and registration acquisition refused, so only
        # another teardown could ever act on that topic again. And a teardown
        # that was cancelled while holding ``cancelling`` would leave the claim
        # owned by a DEAD task, which is the same acquisition/release asymmetry
        # every other actor already fixed: the release CAS names the phase THIS
        # actor holds, and hands the flow back to its observer.
        orphaned: TrustFlow | None = None
        async with creation_lock(user_id, thread_id):
            leftover = _flows.get((user_id, thread_id))
            if leftover is not None and leftover.phase != PHASE_TERMINAL:
                leftover.lower_fence_if_owned()
                if (
                    leftover.phase == PHASE_CANCELLING
                    and leftover.claim_task is asyncio.current_task()
                ):
                    observer = leftover.wait_task
                    if observer is not None and observer.done():
                        # OPEN IMPLIES OBSERVED (review r8 P1-A). Teardown
                        # cancels and awaits the WAIT task BEFORE the guarded
                        # cleanup, so by the time an abort lands here the
                        # observer is usually already DEAD. Handing the flow
                        # "back to its observer" then parks the topic forever:
                        # no ceiling, no registration, no bind, and every
                        # inbound message nudging a flow nothing will advance.
                        # With no observer to restore to, the only correct exit
                        # is the terminal one — take it here, outside the lock.
                        orphaned = leftover
                    else:
                        try_transition_locked(
                            leftover,
                            expect=frozenset({PHASE_CANCELLING}),
                            to=PHASE_AWAITING_TRUST,
                        )
        if orphaned is not None:
            # BEST EFFORT, and it must never mask the original failure: this
            # runs on the way out of a teardown that already raised or was
            # cancelled.
            # LEASHED, not merely bounded (review r9 P1-B). ``wait_for`` on a
            # SHIELD only stops US waiting — the shielded coroutine keeps
            # running, so on timeout a kill-capable task was left DETACHED and
            # could fire its ``kill_window`` long after we had released
            # ownership, against a window a new flow had since adopted. We hold
            # the handle, and on expiry we CANCEL it and bounded-await its
            # wind-down, so nothing kill-capable outlives this arm. The
            # ownership re-check inside the cleanup is the second line of
            # defence for a straggler that still gets through.
            cleanup_task = asyncio.create_task(
                _terminal_cleanup(
                    orphaned,
                    orphaned.bot,
                    orphaned.tmux_mgr,
                    reason="teardown aborted with no observer left",
                    body=(
                        "⚠️ This session setup was cancelled while an "
                        "action was still in flight."
                    ),
                    session_mgr=session_mgr or orphaned.session_mgr,
                    allow_completion=False,
                )
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task), timeout=ORPHAN_CLEANUP_BUDGET_S
                )
            except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001
                # Logged, never re-raised: we are already unwinding, and the
                # DROP below is what guarantees the invariant.
                logger.warning(
                    "trust teardown's orphan cleanup did not complete "
                    "(thread=%s, window=%s) — cancelling it and dropping the flow",
                    thread_id,
                    orphaned.created_wid,
                )
            finally:
                if not cleanup_task.done():
                    await _cancel_and_await(
                        cleanup_task, budget=ORPHAN_CLEANUP_BUDGET_S
                    )
                # The flow is orphaned on EVERY outcome — commit terminal and
                # drop it, so no OPEN, unobserved record can survive this path.
                # SHIELDED: a cancellation arriving while we reacquire the lock
                # used to skip the drop entirely, which made
                # "drop-on-every-outcome" only true when nobody cancelled us.
                await _bounded_shield(
                    _drop_orphan(user_id, thread_id, orphaned),
                    budget=ORPHAN_CLEANUP_BUDGET_S,
                    what="orphan drop",
                )


async def _teardown_loop(
    user_id: int,
    thread_id: int,
    *,
    session_mgr: Any = None,
    expected_wid: str | None = None,
    expected_generation: int | None = None,
) -> bool:
    """The teardown choreography itself; see ``teardown_thread``."""
    key = (user_id, thread_id)
    completed = False
    observed_wid: str | None = expected_wid
    generation: int | None = expected_generation
    forced = False
    # The retained bind tail this loop has ALREADY consumed. A ``continue`` must
    # never re-consume the same completed task: the tail can finish WITHOUT
    # moving the phase out of ``completing_bind`` (it raised, or its terminalizer
    # lost its CAS), and re-awaiting an already-done task then spins the loop
    # forever without ever reaching the ceiling below.
    consumed_bind: asyncio.Task[Any] | None = None
    deadline = _wall() + TEARDOWN_BUDGET_S

    while True:
        async with creation_lock(user_id, thread_id):
            flow = _flows.get(key)
            if flow is not None:
                # RAISE THE FENCE (review r6 P1-A) on every pass: while it is up
                # no NEW claim can be acquired, so a stream of taps cannot starve
                # this loop. In-flight claims still settle normally.
                flow.raise_fence()
            if flow is None:
                return completed or _completion_won(
                    user_id,
                    thread_id,
                    observed_wid=observed_wid,
                    generation=generation,
                    session_mgr=session_mgr,
                )
            current = flow
            phase = flow.phase
            wait_task = flow.wait_task
            bind_task = flow.bind_task
            generation = flow.generation
            observed_wid = flow.created_wid

        if phase == PHASE_TERMINAL:
            async with creation_lock(user_id, thread_id):
                if _flows.get(key) is current:
                    _release_tokens(current)
                    _drop_entry(current)
                    _drop_flow(current)
            return completed

        # A LIVE retained bind is handled BEFORE any force claim, even past the
        # budget (review r7 P1-C): force-claiming a bind that is about to WIN
        # both aborts a legitimate completion and loses the ``completed`` report
        # ``/start`` needs to run its bound-topic teardown. The await is bounded
        # by whatever budget remains plus a grace, and ONLY if that expires may
        # the force path below take over.
        must_force = False
        if phase == PHASE_COMPLETING_BIND:
            if bind_task is None:
                # A ``completing_bind`` with no retained task is a bug, not a
                # race — wait it out within budget, then force rather than spin.
                if _wall() < deadline:
                    await asyncio.sleep(_DISPATCH_SETTLE_POLL_S)
                    continue
                must_force = True
            else:
                grace = max(deadline - _wall(), 0.0) + BIND_TAIL_GRACE_S
                try:
                    await asyncio.wait_for(asyncio.shield(bind_task), timeout=grace)
                except asyncio.CancelledError:
                    if not bind_task.cancelled():
                        raise
                except (TimeoutError, Exception):  # noqa: BLE001
                    pass
                if bind_task.done():
                    # DURABLE EVIDENCE, not just the flags (review r9 P1-A): a
                    # tail cancelled at the replay await committed its bind, and
                    # reporting False here made `/start` skip the bound-topic
                    # teardown while a live binding survived the reset.
                    completed = _bind_task_succeeded(bind_task) or bind_committed(
                        current
                    )
                    if consumed_bind is not bind_task:
                        # FIRST observation: re-read the phase, because the
                        # tail's own terminalizer may have moved it.
                        consumed_bind = bind_task
                        continue
                    # The tail is finished and the phase did NOT move. Falling
                    # through to the force path is the only way out — looping
                    # would re-await a completed task and jump the ceiling.
                    must_force = True
                else:
                    logger.warning(
                        "trust teardown's bind tail did not finish within its "
                        "grace (thread=%s, window=%s) — forcing",
                        thread_id,
                        observed_wid,
                    )
                    must_force = True

        if (must_force or _wall() >= deadline) and not forced:
            # The OVERALL budget expired (review r6 P1-A). FORCE-SETTLE the
            # phase we are actually in — settle the registered owner, force the
            # claim, run the guarded cleanup, drop — instead of the blind
            # registry drop the old pass counter ended in.
            logger.warning(
                "trust teardown budget expired in %s (thread=%s, window=%s) — "
                "force-settling",
                phase,
                thread_id,
                observed_wid,
            )
            await _settle_claim_task(current, budget=_DISPATCH_SETTLE_POLL_S)
            if not await force_terminal_claim(current):
                continue
            forced = True
            # FORCE-SETTLE MEANS THE BIND TAIL DIES (review r8 P1-B). Its own
            # bounded wait above has already expired, so the tail is genuinely
            # hung. It must be cancelled BEFORE the WAIT task, because the WAIT
            # task's terminalizer awaits this very tail — cancelling the
            # observer first would park teardown on a task parked on the tail,
            # and `/start`, topic-close and shutdown would hang forever.
            await _cancel_and_await(current.bind_task)
            await _cancel_and_await(current.wait_task)
            await _terminal_cleanup(
                current,
                current.bot,
                current.tmux_mgr,
                reason="teardown budget expired",
                body=(
                    "⚠️ This session setup was cancelled while an action was "
                    "still in flight."
                ),
                session_mgr=session_mgr or current.session_mgr,
                allow_completion=False,
            )
            continue

        if phase in _TASK_CLAIMED_PHASES:
            if not forced and await _settle_claim_task(current):
                continue
            # The claim never came back even after its owner was cancelled and
            # awaited — a wedged runtime. Force, then finish the job here.
            logger.warning(
                "trust teardown FORCING an unsettled %s claim (thread=%s, window=%s)",
                phase,
                thread_id,
                observed_wid,
            )
            if not await force_terminal_claim(current):
                continue
            forced = True
            await _cancel_and_await(wait_task)
            await _terminal_cleanup(
                current,
                current.bot,
                current.tmux_mgr,
                reason="teardown forced an unsettled claim",
                body=(
                    "⚠️ This session setup was cancelled while an action was "
                    "still in flight."
                ),
                session_mgr=session_mgr or current.session_mgr,
            )
            continue

        # OPEN: claim it, settle the WAIT task, run the guarded cleanup, look
        # again (the cleanup either CASes to terminal or hands the flow to the
        # completion seam, and the next pass acts on whichever it is).
        if not await claim_terminal(current, ignore_fence=True):
            continue
        await _cancel_and_await(wait_task)
        await _terminal_cleanup(
            current,
            current.bot,
            current.tmux_mgr,
            reason="topic teardown",
            body="⚠️ This session setup was cancelled.",
            session_mgr=session_mgr or current.session_mgr,
        )

    return completed


async def clear_topic_entry(
    user_id: int,
    thread_id: int | None,
    user_data: dict[str, Any] | None,
    *,
    session_mgr: Any = None,
) -> dict[str, Any] | None:
    """THE ONLY entry-removal seam for teardown — and the LAST step of it.

    It drops the picker entry under the creation lock, destroying its identity
    token so a creation callback still in flight fails its install CAS.

    **It never transitions a live flow itself** (review r7 P1-D). CASing an OPEN
    flow straight to ``terminal`` meant its WAIT terminalizer then only dropped
    bookkeeping — no guarded window cleanup — so a flow installed in the gap
    between ``teardown_thread`` and this call left an ORPHANED tmux window and
    silently discarded the queued payload. Discovering ANY live flow therefore
    runs the FULL teardown loop for it (fence, settle, guarded cleanup) through
    the same seam, and only then clears. Exhaustion follows the same rule as the
    teardown budget's expiry — settle the owner, force the claim, guarded
    cleanup, drop — never an unconditional entry drop.
    """
    if thread_id is None:
        return None
    key = (user_id, thread_id)
    for _ in range(3):
        async with creation_lock(user_id, thread_id):
            if _flows.get(key) is None:
                return drop_picker_entry(user_data, thread_id)
        # A live flow of ANY phase gets the FULL teardown — never a unilateral
        # CAS to terminal.
        await teardown_thread(
            user_id, thread_id, reason="entry clear", session_mgr=session_mgr
        )

    async with creation_lock(user_id, thread_id):
        leftover = _flows.get(key)
    if leftover is not None:
        logger.warning(
            "trust entry clear could not settle the flow for thread %s — "
            "force-settling it",
            thread_id,
        )
        await _settle_claim_task(leftover, budget=_DISPATCH_SETTLE_POLL_S)
        if await force_terminal_claim(leftover):
            await _cancel_and_await(leftover.wait_task)
            await _terminal_cleanup(
                leftover,
                leftover.bot,
                leftover.tmux_mgr,
                reason="entry clear forced an unsettled flow",
                body=(
                    "⚠️ This session setup was cancelled while an action was "
                    "still in flight."
                ),
                session_mgr=session_mgr or leftover.session_mgr,
                allow_completion=False,
            )
        async with creation_lock(user_id, thread_id):
            if _flows.get(key) is leftover:
                _drop_entry(leftover)
                _drop_flow(leftover)
    async with creation_lock(user_id, thread_id):
        return drop_picker_entry(user_data, thread_id)


async def clear_all_topic_entries(
    user_id: int, user_data: dict[str, Any] | None, *, session_mgr: Any = None
) -> None:
    """Drop EVERY thread's picker entry for one user, each under its own lock."""
    for thread_id in list(_picker_thread_ids(user_data)):
        await clear_topic_entry(user_id, thread_id, user_data, session_mgr=session_mgr)


def _picker_thread_ids(user_data: dict[str, Any] | None) -> list[int]:
    """The thread ids this user currently has picker entries for."""
    if user_data is None:
        return []
    pickers = user_data.get("_pending_pickers")
    if not isinstance(pickers, dict):
        return []
    return [tid for tid in pickers if isinstance(tid, int)]


async def teardown_all_for_user(
    user_id: int, *, user_data: Any = None, session_mgr: Any = None
) -> list[int]:
    """Tear down EVERY topic's creation flow for one user (the ``/start`` reset).

    Returns the thread ids whose COMPLETION WON during teardown, so the caller
    can run the normal bound-topic teardown on each (review r1 P2-3).
    """
    del user_data
    completed: list[int] = []
    # Snapshot the WINDOW too, not just the key: a flow can finish on its own
    # between this snapshot and its per-topic teardown below (review r2 P2-A),
    # and the window is what proves the resulting binding was ITS completion.
    snapshot = [
        (uid, tid, flow.created_wid, flow.generation)
        for (uid, tid), flow in _flows.items()
        if uid == user_id
    ]
    for uid, tid, wid, gen in snapshot:
        if await teardown_thread(
            uid,
            tid,
            reason="/start reset",
            expected_wid=wid,
            expected_generation=gen,
            session_mgr=session_mgr,
        ):
            completed.append(tid)
    return completed


async def shutdown() -> None:
    """Cancel + await every live flow task at bot shutdown.

    Never ABANDONS a task (review r4 P1-B): a flow the ordinary teardown could
    not settle is force-claimed and its task cancelled + awaited here, so the
    process does not exit with a live WAIT task still typing into a pane.
    """
    for uid, tid in list(_flows.keys()):
        await teardown_thread(uid, tid, reason="bot shutdown")
    for uid, tid in list(_flows.keys()):
        flow = _flows.get((uid, tid))
        if flow is None:
            continue
        logger.warning(
            "trust flow survived shutdown teardown (thread=%s, window=%s) — forcing",
            tid,
            flow.created_wid,
        )
        await force_terminal_claim(flow)
        for task in (flow.wait_task, flow.bind_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        async with creation_lock(uid, tid):
            if _flows.get((uid, tid)) is flow:
                _drop_entry(flow)
                _drop_flow(flow)
