"""Per-route snapshot state machine — the sole run-state / context-usage /
idle-clear authority.

Single source of truth for "what is this route doing right now":

  - ``RouteRuntimeSnapshot`` is immutable; every read returns a frozen
    point-in-time view.
  - Mutations go through ``ingest_*`` / ``mark_*`` functions, which acquire
    a per-route ``asyncio.Lock``, apply the transition, freeze a snapshot,
    release the lock, then fan out to observers against the committed
    snapshot. Observers cannot see partial state.
  - ``snapshot(route)`` is the primary read seam — pull-based, fast, no
    coroutine machinery. ``subscribe(route, cb)`` is secondary, for UI
    surfaces that benefit from push notifications.

This module owns the run-state machine, the context-usage cache, and the
debounced pane-idle card-clear. The surfaces that consume it:

  - ``snapshot.run_state`` — run-state (RUNNING / RUNNING_TOOL /
    WAITING_ON_USER / IDLE_RECENT / IDLE_CLEARED).
  - ``snapshot.open_tools`` / ``snapshot.waiting_on_user_tools`` — the
    in-flight tool set (replay-seeded on startup via ``seed_open_tools``).
  - ``snapshot.context_usage`` — the context-window cache with the 1M latch.
  - ``snapshot.idle_clear_at`` — the run-state decay deadline (transcript
    end-of-turn → IDLE_RECENT → IDLE_CLEARED).
  - ``snapshot.pane_idle_clear_at`` plus the ``arm_pane_idle_clear`` /
    ``pane_idle_clear_due`` / ``commit_pane_idle_clear`` debounce triad —
    the visible "🟡 Busy" status-card clear, armed by ``status_polling``
    on a confirmed-idle pane observation.
  - ``snapshot.status_card_visible`` / ``snapshot.status_card_msg_id`` —
    status-card visibility (``message_queue`` mirrors its send-layer cache
    here).

The ``message_queue`` boundary:

  - ``message_queue._status_msg_info`` remains the send-layer cache.
    message_queue is the sole sender/editor of status cards. It queries
    ``snapshot.status_card_visible`` to pick edit-vs-send and calls
    ``mark_status_card_published`` after a successful send. If a change
    needs to mutate message_queue internals beyond that, **stop and
    promote Route Outbox** — that's the kill signal.

Concurrency contract:

  - One per-route ``asyncio.Lock``. Independent routes do not serialize.
  - Async mutators (``ingest_transcript_event``, ``mark_inbound_sent``,
    ``mark_pane_idle``,
    ``commit_pane_idle_clear``, ``mark_session_reset``) acquire the
    route's lock, mutate, freeze a snapshot, **then** release the lock
    before fanning observers out against the committed snapshot.
    Observers can therefore call back into ``snapshot(other_route)`` /
    ``ingest_*`` without deadlocking.
  - **Synchronous side-band writes** (``mark_status_card_published`` /
    ``mark_status_card_cleared``, ``update_context_usage``,
    ``seed_open_tools``, ``arm_pane_idle_clear``, ``clear_route``)
    intentionally bypass the lock. They are bookkeeping for read-side
    flags — they don't change ``run_state`` (no transition table runs),
    don't fire observers (no fan-out), and don't await between their
    initial read of ``_state`` and the field write. Safe under Python's
    single-threaded asyncio scheduling because no suspension point
    separates the read from the write. ``pane_idle_clear_due`` is a pure
    synchronous read in the same vein. **Do not call these from a
    thread** — they assume event-loop-thread execution.
  - Every committed transition increments ``_global_seq``; the snapshot
    carries ``monotonic_seq`` so subscribers can dedupe / detect drops.
  - Pane snapshots (``mark_pane_idle`` / ``commit_pane_idle_clear``) are
    reconciliation events with **lower authority** than transcript
    lifecycle events: they preserve ``WAITING_ON_USER``, only clearing
    ``RUNNING`` / ``RUNNING_TOOL`` to ``IDLE_CLEARED`` after the debounce
    delay has elapsed, keeping the visible "🟡 Busy" card and the
    run-state machine in sync.

Persistence policy:

  - In-memory by default. ``open_tools`` reconstructs from JSONL replay
    on startup via ``seed_open_tools`` /
    ``parse_pending_tools_from_jsonl``. ``status_card_*`` is not
    persisted — restart-induced loss is self-healing (next status-card
    send re-publishes the msg_id). When persistence is needed it will
    land via a state.json ``schema_version`` bump in a follow-up wave.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]

# Tool names whose open tool_use means "the run-state is WAITING_ON_USER".
# Owned HERE (the run-state authority that classifies them) rather than imported
# from the heavy ``handlers.interactive_ui`` UI layer — importing UI from the
# core authority created a circular import (route_runtime → interactive_ui →
# callback_dispatcher → …) that made ``import cctelegram.route_runtime`` fail
# standalone and only work by bot.py's import order. interactive_ui and bot.py
# import this constant FROM route_runtime now (one-way dependency).
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})


class RunState(Enum):
    RUNNING = "RUNNING"
    RUNNING_TOOL = "RUNNING_TOOL"
    WAITING_ON_USER = "WAITING_ON_USER"
    IDLE_RECENT = "IDLE_RECENT"
    IDLE_CLEARED = "IDLE_CLEARED"


# Wall-clock seconds an IDLE_RECENT route stays "recent" before decaying to
# IDLE_CLEARED, and the confirmed-idle debounce before the visible "🟡 Busy"
# card is cleared. Time-based rather than poll-count-based because the polling
# loop iterates all bindings sequentially — with N bound topics any single
# window is only polled every N seconds, so a poll-count threshold would make
# the perceived clear delay scale with how many topics the user has open. 4s
# of confirmed idle is comfortably longer than Claude's slowest UI transition
# while still feeling responsive.
IDLE_CLEAR_DELAY_SECONDS = 4.0


@dataclass(frozen=True)
class ContextUsage:
    """Snapshot of a route's context-window state.

    ``tokens`` is the next-turn input size (input + cache_read + cache_creation
    from the latest assistant ``message.usage``). ``max_tokens`` is the
    detected window cap — 200_000 by default, latched up to 1_000_000 once
    we observe a session exceeding the 200k threshold (the ``[1m]`` model
    variant doesn't carry a suffix in JSONL ``message.model``, so observed
    overflow is the only signal).
    """

    tokens: int
    max_tokens: int


# stop_reasons that signal "this assistant turn is over".
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})

# A route observed strictly above this token count must be on the 1M variant.
_CONTEXT_DETECT_1M_THRESHOLD = 200_001


@dataclass(frozen=True)
class TranscriptLifecycleEvent:
    """A transcript event normalized for state-machine ingestion.

    Lossless w.r.t. the fields the state machine reads from a raw
    ``session_monitor.TranscriptEvent``. The adapter (see
    ``transcript_event_adapter``) does the translation.
    """

    role: Literal["user", "assistant"]
    block_type: Literal["text", "thinking", "tool_use", "tool_result"]
    tool_use_id: str | None
    tool_name: str | None
    stop_reason: str | None


@dataclass(frozen=True)
class RouteRuntimeSnapshot:
    """Immutable point-in-time view of a route's runtime state.

    Surfaces consume the snapshot rather than the underlying dicts:

      - ``message_queue._upsert_activity_digest`` reads ``run_state`` and
        ``context_usage`` to render the header.
      - ``status_polling.typing_action_loop`` reads ``typing_eligible``
        to decide whether to re-emit the native typing indicator.
      - ``status_polling`` reads ``pane_idle_clear_at`` to drive the
        debounced "🟡 Busy" card clear: it arms the deadline on the first
        confirmed-idle pane observation and clears the card once
        ``now >= pane_idle_clear_at``.

    ``idle_clear_at`` vs ``pane_idle_clear_at`` — two *distinct* deadlines:
      - ``idle_clear_at`` is the run-state decay deadline. It is armed by a
        transcript end-of-turn (``_set_run_state(IDLE_RECENT)``) and drives
        the lazy ``IDLE_RECENT → IDLE_CLEARED`` decay read by the digest
        header. It has nothing to do with the pane.
      - ``pane_idle_clear_at`` is the *card-clear* debounce deadline. It is
        armed by ``status_polling`` on a confirmed-idle pane observation
        (``arm_pane_idle_clear``) and drives the visible status-card
        removal. Activity re-arms (cancels) it. They are kept separate
        because the card-clear trigger (confirmed-idle pane, not transcript
        end_turn) is distinct from run-state decay.

    Equality is by value — comparing two snapshots tells you whether
    *anything* the route observes has changed. Distinct ``monotonic_seq``
    values are guaranteed for any two ``ingest_*`` results on the same
    route (or different routes) regardless of whether the visible fields
    moved.
    """

    route: Route
    run_state: RunState
    open_tools: frozenset[str]
    waiting_on_user_tools: frozenset[str]
    context_usage: ContextUsage | None
    last_event_at: float
    idle_clear_at: float | None
    pane_idle_clear_at: float | None
    typing_eligible: bool
    status_card_visible: bool
    status_card_msg_id: int | None
    monotonic_seq: int


@dataclass
class _RouteState:
    """Mutable internal state — lives behind the route's lock."""

    run_state: RunState = RunState.IDLE_CLEARED
    open_tools: dict[str, bool] = field(default_factory=dict)
    context_usage: ContextUsage | None = None
    last_event_at: float = 0.0
    idle_clear_at: float | None = None
    # Card-clear debounce deadline (distinct from ``idle_clear_at``). Armed
    # by ``arm_pane_idle_clear`` on the first confirmed-idle pane
    # observation; the visible status card clears once ``now`` reaches it.
    # ``None`` means "not armed". Stores the *deadline* (first-observed-at +
    # IDLE_CLEAR_DELAY_SECONDS), not the first-observed timestamp.
    pane_idle_clear_at: float | None = None
    # "Cleared this idle stretch" sentinel: the card was cleared, so further
    # idle ticks are no-ops until activity re-arms (resets it). Without this
    # a second arm after the clear would re-fire and re-enqueue a
    # status_clear.
    pane_idle_cleared: bool = False
    status_card_msg_id: int | None = None
    seen: bool = False  # have we ever observed this route?
    # Cached frozensets — invalidated on any open_tools mutation.
    # Most snapshots happen with no open tools (idle route), so the
    # cache pays off heavily on pure read traffic.
    _open_tools_cache: frozenset[str] | None = None
    _waiting_tools_cache: frozenset[str] | None = None

    def invalidate_tool_cache(self) -> None:
        self._open_tools_cache = None
        self._waiting_tools_cache = None

    def open_tools_frozen(self) -> frozenset[str]:
        if self._open_tools_cache is None:
            self._open_tools_cache = frozenset(self.open_tools.keys())
        return self._open_tools_cache

    def waiting_tools_frozen(self) -> frozenset[str]:
        if self._waiting_tools_cache is None:
            self._waiting_tools_cache = frozenset(
                tid for tid, interactive in self.open_tools.items() if interactive
            )
        return self._waiting_tools_cache


# Per-route state + lock + observer maps. Keys are ``Route`` tuples; the
# tuple itself is the natural identity, so we use a plain dict (not a
# defaultdict) and lazy-init under ``_lock_for_route``.
_state: dict[Route, _RouteState] = {}
_locks: dict[Route, asyncio.Lock] = {}
_observers: dict[Route, list[Callable[[RouteRuntimeSnapshot], Awaitable[None]]]] = {}

# Monotonic sequence — increments on every committed transition. Snapshots
# carry the value seen at commit time so an observer can detect dropped
# / reordered notifications independently of event payload.
_global_seq: int = 0


# ── lock helpers ────────────────────────────────────────────────────────


def _lock_for_route(route: Route) -> asyncio.Lock:
    """Return (lazy-creating) the lock that serialises ``route``'s mutations.

    ``dict.setdefault`` is atomic under the GIL, so the lock is unique per
    route even if two tasks race on first observation. The cost of the
    discarded ``asyncio.Lock()`` on the losing task is a single object
    allocation — negligible.
    """
    lock = _locks.get(route)
    if lock is None:
        lock = _locks.setdefault(route, asyncio.Lock())
    return lock


def _state_for_route(route: Route) -> _RouteState:
    """Return (lazy-creating) the mutable state for ``route``.

    Must only be called from inside the route's lock.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    return st


# ── snapshot helpers ────────────────────────────────────────────────────


def _now() -> float:
    return time.monotonic()


def _next_seq() -> int:
    global _global_seq
    _global_seq += 1
    return _global_seq


def _state_from_open_tools(open_tools: dict[str, bool]) -> RunState:
    """Derive the run state from the current open-tool set.

    Empty → RUNNING (turn still in flight, no tools pending).
    Any interactive id → WAITING_ON_USER (user input gates progress).
    Otherwise → RUNNING_TOOL (model is waiting on tool output).
    """
    if not open_tools:
        return RunState.RUNNING
    if any(open_tools.values()):
        return RunState.WAITING_ON_USER
    return RunState.RUNNING_TOOL


def _freeze(route: Route, st: _RouteState) -> RouteRuntimeSnapshot:
    """Snapshot ``st`` under the route's lock. Must be called with the
    lock held. Increments ``_global_seq``."""
    return RouteRuntimeSnapshot(
        route=route,
        run_state=st.run_state,
        open_tools=st.open_tools_frozen(),
        waiting_on_user_tools=st.waiting_tools_frozen(),
        context_usage=st.context_usage,
        last_event_at=st.last_event_at,
        idle_clear_at=st.idle_clear_at,
        pane_idle_clear_at=st.pane_idle_clear_at,
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        monotonic_seq=_next_seq(),
    )


def _default_snapshot(route: Route) -> RouteRuntimeSnapshot:
    """Snapshot of an unknown route — used by ``snapshot()`` for routes
    that have never been observed.

    Unknown routes default to ``IDLE_CLEARED`` (treat-as-idle): a surface
    asking about a route the runtime has never seen is best off rendering
    "no busy state" rather than fabricating one. Does NOT consume a sequence
    number — pure observation, no commit.
    """
    return RouteRuntimeSnapshot(
        route=route,
        run_state=RunState.IDLE_CLEARED,
        open_tools=frozenset(),
        waiting_on_user_tools=frozenset(),
        context_usage=None,
        last_event_at=0.0,
        idle_clear_at=None,
        pane_idle_clear_at=None,
        typing_eligible=False,
        status_card_visible=False,
        status_card_msg_id=None,
        monotonic_seq=0,
    )


# ── pure transitions (callers hold the lock) ────────────────────────────


def _decay_idle_in_place(st: _RouteState) -> None:
    """Apply lazy IDLE_RECENT → IDLE_CLEARED decay.

    On-read instead of a scheduled ``call_later`` so we don't leak timer
    handles for routes that get torn down mid-decay. Called immediately
    before freezing a snapshot when no state-affecting event ran — i.e.
    during pure reads via ``snapshot()`` — so a stale IDLE_RECENT
    doesn't survive past its delay just because nothing else has
    happened on the route.
    """
    if st.run_state is RunState.IDLE_RECENT and st.idle_clear_at is not None:
        if _now() >= st.idle_clear_at:
            st.run_state = RunState.IDLE_CLEARED
            st.idle_clear_at = None


def _rearm_pane_idle_in_place(st: _RouteState) -> None:
    """Cancel any pending / completed pane-idle card-clear on real activity.

    Real activity on a route MUST cancel a pending clear and reset the
    "already cleared this stretch" sentinel so the next confirmed-idle pane
    observation re-arms from scratch — this is the c313657 guard: a
    sub-agent / quick-tool turn that finishes between two pane scrapes must
    not leave the route stuck having cleared too early.

    Called unconditionally from ``ingest_transcript_event`` /
    ``mark_inbound_sent`` (even on same-state refreshes) so *every* activity
    event re-arms, not only state changes.
    """
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = False


def _set_run_state(st: _RouteState, new: RunState) -> None:
    """Commit a run-state change. Updates ``idle_clear_at`` when entering
    IDLE_RECENT so the lazy decay rule has a definite deadline."""
    st.last_event_at = _now()
    if new is RunState.IDLE_RECENT:
        st.idle_clear_at = st.last_event_at + IDLE_CLEAR_DELAY_SECONDS
    elif new in (RunState.RUNNING, RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
        # Active run states clear any pending idle deadline.
        st.idle_clear_at = None
    elif new is RunState.IDLE_CLEARED:
        st.idle_clear_at = None
    st.run_state = new
    st.seen = True


def _apply_lifecycle_event(st: _RouteState, event: TranscriptLifecycleEvent) -> None:
    """Run the §2.2.1 transition table on the route's state.

    Operates on ``_RouteState`` without async (the lock is the caller's
    responsibility).
    """
    st.seen = True

    role = event.role
    block = event.block_type
    stop_reason = event.stop_reason

    # tool_use: open the tool. is_interactive bit travels with the id so
    # parallel turns settle correctly when each tool_result lands.
    if role == "assistant" and block == "tool_use" and event.tool_use_id:
        is_interactive = bool(
            event.tool_name and event.tool_name in INTERACTIVE_TOOL_NAMES
        )
        st.open_tools[event.tool_use_id] = is_interactive
        st.invalidate_tool_cache()
        _set_run_state(st, _state_from_open_tools(st.open_tools))
        return

    # tool_result: close the slot if known. Stale ids (e.g. pre-startup
    # tools we never saw the tool_use for) are ignored. Role is not
    # checked because transcript_parser flips tool_result role to
    # ``assistant`` for rendering, while the JSONL envelope is
    # role="user" — block_type + tool_use_id are already specific
    # enough.
    if block == "tool_result" and event.tool_use_id:
        if event.tool_use_id not in st.open_tools:
            return
        st.open_tools.pop(event.tool_use_id, None)
        st.invalidate_tool_cache()
        _set_run_state(st, _state_from_open_tools(st.open_tools))
        return

    # End-of-turn: thinking or text with end_turn / stop_sequence AND no
    # open tools → IDLE_RECENT. With open tools we stay in
    # RUNNING_TOOL / WAITING_ON_USER until the matching tool_result.
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and not st.open_tools
    ):
        _set_run_state(st, RunState.IDLE_RECENT)
        return

    # Plain assistant text: at least RUNNING. Preserve RUNNING_TOOL /
    # WAITING_ON_USER (open tools still gate).
    if role == "assistant" and block == "text":
        if st.run_state in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            st.last_event_at = _now()
            return
        _set_run_state(st, RunState.RUNNING)
        return

    # Assistant thinking without end-of-turn: light up if route was idle.
    # Preserve RUNNING_TOOL / WAITING_ON_USER.
    if role == "assistant" and block == "thinking":
        if not st.seen or st.run_state in (
            RunState.IDLE_CLEARED,
            RunState.IDLE_RECENT,
        ):
            _set_run_state(st, RunState.RUNNING)
            return
        st.last_event_at = _now()
        return

    # User non-tool_result: user prompted Claude — RUNNING.
    if role == "user" and block != "tool_result":
        _set_run_state(st, RunState.RUNNING)
        return

    # Fallback: refresh activity timer without state change.
    st.last_event_at = _now()


# ── public API: ingest + snapshot ───────────────────────────────────────


async def ingest_transcript_event(
    route: Route, event: TranscriptLifecycleEvent
) -> RouteRuntimeSnapshot:
    """Apply ``event`` to ``route``'s state and return the committed snapshot.

    Locks the route, applies the transition, freezes a snapshot, releases
    the lock, then fires observers against the committed snapshot.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        _apply_lifecycle_event(st, event)
        # Activity re-arms the pane-idle debounce (cancels a pending clear).
        _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


def snapshot(route: Route) -> RouteRuntimeSnapshot:
    """Return the current snapshot for ``route``.

    Pure read — no lock acquisition for the common case (the underlying
    dict reads are atomic under the GIL, and the snapshot is built from
    a single ``_RouteState`` reference which is never re-assigned). Lazy
    IDLE_RECENT decay is applied so a stale ``run_state=IDLE_RECENT``
    doesn't survive past its deadline just because nothing else hit the
    route.

    Unknown routes return ``_default_snapshot`` (``IDLE_CLEARED``).
    """
    st = _state.get(route)
    if st is None:
        return _default_snapshot(route)
    # Lazy decay is observation-only — it doesn't fire observers and
    # doesn't consume a sequence number unless decay actually fires.
    if (
        st.run_state is RunState.IDLE_RECENT
        and st.idle_clear_at is not None
        and _now() >= st.idle_clear_at
    ):
        st.run_state = RunState.IDLE_CLEARED
        st.idle_clear_at = None
        return _freeze(route, st)
    return RouteRuntimeSnapshot(
        route=route,
        run_state=st.run_state,
        open_tools=st.open_tools_frozen(),
        waiting_on_user_tools=st.waiting_tools_frozen(),
        context_usage=st.context_usage,
        last_event_at=st.last_event_at,
        idle_clear_at=st.idle_clear_at,
        pane_idle_clear_at=st.pane_idle_clear_at,
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        monotonic_seq=_global_seq,  # read-only — no commit
    )


# ── public API: mark_* mutations ────────────────────────────────────────


async def mark_inbound_sent(route: Route) -> RouteRuntimeSnapshot:
    """A Telegram-originated prompt was delivered to Claude's tmux window.

    Idempotent: never downgrades RUNNING_TOOL / WAITING_ON_USER.
    Otherwise transitions to RUNNING so the typing indicator and status
    card show activity before the first JSONL event lands.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state not in (
            RunState.RUNNING_TOOL,
            RunState.WAITING_ON_USER,
        ):
            _set_run_state(st, RunState.RUNNING)
        else:
            st.last_event_at = _now()
            st.seen = True
        # Inbound delivery is real activity — re-arm the pane-idle debounce.
        _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


def _reconcile_pane_idle_in_place(st: _RouteState) -> None:
    """Reconcile a confirmed-idle route to ``IDLE_CLEARED`` in place.

    Pane snapshots have **lower authority** than transcript lifecycle
    events:

      - ``WAITING_ON_USER`` is preserved (interactive prompt is open).
      - Otherwise drops any lingering open tools and transitions to
        ``IDLE_CLEARED``.

    Shared by ``mark_pane_idle`` (immediate reconciliation seam) and
    ``commit_pane_idle_clear`` (the debounced production card-clear) so
    both apply identical reconciliation. Caller holds the route's lock.
    """
    if st.run_state is RunState.WAITING_ON_USER:
        return
    st.open_tools.clear()
    st.invalidate_tool_cache()
    _set_run_state(st, RunState.IDLE_CLEARED)


async def mark_pane_idle(route: Route) -> RouteRuntimeSnapshot:
    """Pane has been confirmed idle — reconcile immediately (no debounce).

    Reconciliation-only — pane snapshots have **lower authority** than
    transcript lifecycle events:

      - ``WAITING_ON_USER`` is preserved (interactive prompt is open).
      - Otherwise drops any lingering open tools and transitions to
        ``IDLE_CLEARED``.

    This is the *immediate* clear; the production status-card path uses the
    debounced ``arm_pane_idle_clear`` / ``pane_idle_clear_due`` /
    ``commit_pane_idle_clear`` triad instead. Retained as a direct
    reconciliation seam (and exercised by the route_runtime tests).
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        _reconcile_pane_idle_in_place(st)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


# ── debounced pane-idle card-clear (route_runtime owns the timer) ────────
#
# The deadline lives here so the card-clear and the run-state reconciliation
# share a single source of truth, and so ``now`` can be injected for
# deterministic tests. ``status_polling`` arms it on a confirmed-idle pane,
# polls ``pane_idle_clear_due``, and commits via ``commit_pane_idle_clear``.
#
#   - missing arm          → ``arm_pane_idle_clear`` sets ``pane_idle_clear_at``
#   - waiting out the delay → ``pane_idle_clear_due`` returns False
#   - delay elapsed         → ``pane_idle_clear_due`` returns True; the caller
#                             then runs ``commit_pane_idle_clear``
#   - already cleared       → ``pane_idle_cleared`` sentinel; arm is a no-op
#   - activity re-arms      → ``_rearm_pane_idle_in_place`` resets both


def arm_pane_idle_clear(route: Route, *, now: float) -> RouteRuntimeSnapshot:
    """Arm the debounced card-clear deadline on the first confirmed-idle
    pane observation.

    Idempotent and synchronous (a read-side bookkeeping write — no
    run-state transition, no observer fan-out, like
    ``mark_status_card_published``):

      - No-op if the route was already cleared this idle stretch
        (``pane_idle_cleared``).
      - No-op if the deadline is already armed (don't push it forward — the
        arm only fires once per stretch).
      - Otherwise sets ``pane_idle_clear_at = now + IDLE_CLEAR_DELAY_SECONDS``.

    ``now`` is injected (no hidden ``time.monotonic()`` call inside the
    transition) so the deadline is deterministically testable. Pass the
    same monotonic clock ``status_polling`` reads.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    if st.pane_idle_cleared:
        return snapshot(route)
    if st.pane_idle_clear_at is None:
        st.pane_idle_clear_at = now + IDLE_CLEAR_DELAY_SECONDS
    return snapshot(route)


def pane_idle_clear_due(route: Route, *, now: float) -> bool:
    """Return True iff an armed pane-idle clear deadline has elapsed.

    Pure query (no mutation). True only when a deadline is actually armed —
    an unarmed or already-cleared route is never "due". ``now`` is injected
    so callers (``update_status_message`` and ``_process_idle_clear_only``)
    decide whether to commit using the same clock they armed with.
    """
    st = _state.get(route)
    if st is None or st.pane_idle_clear_at is None:
        return False
    return now >= st.pane_idle_clear_at


async def commit_pane_idle_clear(route: Route, *, now: float) -> bool:
    """Perform the debounced card clear once the deadline is due.

    Returns ``True`` iff it actually cleared (reconciled run-state, dropped the
    deadline, latched the ``pane_idle_cleared`` sentinel, fanned out); ``False``
    if it no-op'd. Callers enqueue the card clear ONLY on ``True``.

    TOCTOU re-validation (Codex 8b P1): the caller checks
    ``pane_idle_clear_due`` WITHOUT the lock, then ``await``\\s this. Between
    that check and our lock acquisition, a transcript ``ingest_transcript_event``
    or ``mark_inbound_sent`` may have re-armed — ``_rearm_pane_idle_in_place``
    sets ``pane_idle_clear_at=None`` (cancel) or a future deadline. Committing
    a now-stale clear would blank the "🟡 Busy" card mid-turn after fresh
    activity (which may land the route in ``WAITING_ON_USER`` / ``IDLE_RECENT``,
    not only ``RUNNING`` — so the run-state alone cannot tell the caller whether
    a clear happened; this explicit bool can). We re-check the SAME armed-and-due
    predicate under the lock with the caller's ``now`` and return ``False``
    (no clear, no fan-out) if the deadline is no longer armed or not yet due.
    This makes the card-clear strictly race-free: the deadline is
    re-validated under the lock before reconciling, so a clear can never
    blank a card that fresh activity already re-armed.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.pane_idle_clear_at is None or now < st.pane_idle_clear_at:
            # Re-armed or cancelled since the lockless due-check — do not clear.
            return False
        _reconcile_pane_idle_in_place(st)
        st.pane_idle_clear_at = None
        st.pane_idle_cleared = True
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return True


def reset_pane_idle_clear(route: Route) -> None:
    """Cancel a pending / completed pane-idle clear from a *pane* signal.

    Synchronous side-band write (no run-state transition, no fan-out — like
    ``arm_pane_idle_clear``). Called by ``status_polling`` when a pane
    scrape shows the route running again: a fresh idle stretch must re-arm
    from scratch rather than fire on a deadline left over from a previous
    stretch. Distinct from ``_rearm_pane_idle_in_place`` (the
    transcript/inbound re-arm) only in that this is the public pane-driven
    seam.
    """
    st = _state.get(route)
    if st is None:
        return
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = False


async def mark_session_reset(route: Route) -> RouteRuntimeSnapshot:
    """Session_id rotated under this route (e.g. ``/clear`` mid-stream).

    Drops in-flight ``open_tools`` (they belong to the dead session),
    drops the context_usage cache, and resets to ``IDLE_CLEARED``.
    Preserves the ``status_card_msg_id`` — message_queue may still want
    to edit the same card to render the new session's first reply.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        st.open_tools.clear()
        st.invalidate_tool_cache()
        st.context_usage = None
        # Fresh session — drop any pending/completed pane-idle debounce so
        # the new session's first idle stretch arms from scratch.
        _rearm_pane_idle_in_place(st)
        _set_run_state(st, RunState.IDLE_CLEARED)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


def mark_status_card_published(route: Route, msg_id: int) -> None:
    """message_queue published / edited a status card for this route.

    Synchronous: this is bookkeeping for the read-side
    ``snapshot.status_card_visible`` flag, not a state-machine
    transition. Does not fire observers — message_queue is the
    authoritative writer and observers would be reacting to their own
    side-effect.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    st.status_card_msg_id = msg_id


def mark_status_card_cleared(route: Route) -> None:
    """message_queue cleared the status card for this route.

    Counterpart to ``mark_status_card_published``. No observer fan-out
    for the same reason.
    """
    st = _state.get(route)
    if st is not None:
        st.status_card_msg_id = None


def update_context_usage(route: Route, tokens: int | None, model: str | None) -> None:
    """Cache the latest ``ContextUsage`` for a route.

    ``None`` or non-positive tokens drops the entry (used after ``/clear``
    when there's no assistant turn yet). The 1M cap latches once observed
    (a 200k session can't legitimately exceed its cap; below threshold
    defaults to 200k). ``model`` is accepted for future use (logging /
    explicit cap overrides) but the cap is derived from observed tokens,
    since JSONL doesn't carry the ``[1m]`` suffix.
    """
    st = _state.get(route)
    if tokens is None or tokens <= 0:
        if st is not None:
            st.context_usage = None
        return
    if st is None:
        st = _RouteState()
        _state[route] = st
    prior_max = st.context_usage.max_tokens if st.context_usage else 200_000
    if tokens >= _CONTEXT_DETECT_1M_THRESHOLD or prior_max >= 1_000_000:
        max_tokens = 1_000_000
    else:
        max_tokens = 200_000
    st.context_usage = ContextUsage(tokens=tokens, max_tokens=max_tokens)
    _ = model  # accepted for future use; cap derives from observed tokens


def seed_open_tools(route: Route, tools: dict[str, bool]) -> None:
    """Replay startup-recovered open tools onto a route.

    No-op when ``tools`` is empty (default IDLE_CLEARED stands) or when
    the route already has live state (a real ingest landed between
    monitor warm-up and the replay walk, and live events have higher
    authority than a JSONL snapshot).
    """
    if not tools:
        return
    st = _state.get(route)
    if st is not None and st.seen:
        return
    if st is None:
        st = _RouteState()
        _state[route] = st
    st.open_tools = dict(tools)
    st.invalidate_tool_cache()
    st.run_state = _state_from_open_tools(st.open_tools)
    st.last_event_at = _now()
    st.seen = True


def parse_pending_tools_from_jsonl(jsonl_path: str) -> dict[str, bool]:
    """Scan a session's parent JSONL for tool_use entries with no tool_result.

    Returns ``{tool_use_id: is_interactive}`` for the open set, suitable for
    feeding into ``seed_open_tools``. Used at startup to recover the
    in-flight tool state lost when the bot restarts mid-turn — most acutely
    important for sub-agent ``Task`` calls, which can sit open for many
    minutes with no parent-JSONL activity to re-arm the run-state machine.

    Set-difference rather than running open-set: parent JSONL is NOT strictly
    chronological. Branch / rewind / ``--resume`` flows can lay a tool_result
    line down before its tool_use line, so a forward "pop on result" walk
    leaves phantom open tools in finished sessions. Collect all uses and all
    results in one pass, then return ``uses − results``.

    Sidechain entries (``isSidechain=true``) are skipped: they live in a
    separate JSONL but if any leak into the parent, they belong to a
    sub-agent's tool space, not the parent's.

    Malformed lines and unexpected shapes are tolerated — the parent JSONL
    is the source of truth so a few skipped lines just mean we miss a tool.
    A missed tool means the indicator stays dark until the next event;
    that's the pre-replay behavior, so this fails safely.
    """
    uses: dict[str, bool] = {}
    results: set[str] = set()
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("isSidechain"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "tool_use":
                        tool_id = item.get("id")
                        if not isinstance(tool_id, str):
                            continue
                        tool_name = item.get("name")
                        is_interactive = bool(
                            isinstance(tool_name, str)
                            and tool_name in INTERACTIVE_TOOL_NAMES
                        )
                        # Don't downgrade an interactive id that appeared
                        # earlier (idempotent against duplicate tool_use
                        # lines after rewind).
                        uses[tool_id] = uses.get(tool_id, False) or is_interactive
                    elif item_type == "tool_result":
                        tool_id = item.get("tool_use_id")
                        if isinstance(tool_id, str):
                            results.add(tool_id)
    except OSError as e:
        logger.warning("replay: failed to read %s: %s", jsonl_path, e)
        return {}
    return {tid: interactive for tid, interactive in uses.items() if tid not in results}


# ── subscribe / observer fan-out ────────────────────────────────────────


def subscribe(
    route: Route,
    observer: Callable[[RouteRuntimeSnapshot], Awaitable[None]],
) -> Callable[[], None]:
    """Register an observer for ``route``. Returns an unsubscribe callable.

    Observers fire **after** ``ingest_*`` / ``mark_*`` commits a snapshot
    — never during mutation. They cannot see partial state.
    """
    obs = _observers.setdefault(route, [])
    obs.append(observer)

    def _unsubscribe() -> None:
        try:
            _observers.get(route, []).remove(observer)
        except ValueError:
            pass

    return _unsubscribe


async def _fan_out(route: Route, snap: RouteRuntimeSnapshot) -> None:
    """Invoke observers against the committed snapshot.

    Exceptions in one observer never block the next. Observers run
    sequentially in registration order — concurrent fan-out is left for
    a future iteration once the subscriber count proves it's needed
    (kill signal: ≤ 5 subscribers per route during initial Wave B).
    """
    obs = _observers.get(route)
    if not obs:
        return
    for cb in list(obs):
        try:
            await cb(snap)
        except Exception as e:
            logger.error("route_runtime observer error route=%s: %s", route, e)


# ── maintenance ─────────────────────────────────────────────────────────


def clear_route(route: Route) -> None:
    """Drop all state for ``route``. Called from topic teardown.

    Does NOT remove the route's lock — the lock is cheap and may be
    re-acquired immediately if the route is re-bound. Removing it would
    require coordination with any in-flight ``ingest_*`` task to avoid
    racing on a fresh lock object.
    """
    _state.pop(route, None)
    _observers.pop(route, None)


def reset_for_tests() -> None:
    """Test-only: drop all per-route state, locks, observers, and reset
    the monotonic sequence.

    This is the single test-side reset seam for the run-state machine,
    the context-usage cache, the pane-idle debounce, and status-card
    visibility. ``message_queue`` (``_status_msg_info``) and
    ``interactive_ui`` keep their own reset seams for their send-layer
    caches.
    """
    global _global_seq
    _state.clear()
    _locks.clear()
    _observers.clear()
    _global_seq = 0


__all__ = [
    "ContextUsage",
    "Route",
    "RouteRuntimeSnapshot",
    "RunState",
    "TranscriptLifecycleEvent",
    "arm_pane_idle_clear",
    "clear_route",
    "commit_pane_idle_clear",
    "ingest_transcript_event",
    "mark_inbound_sent",
    "mark_pane_idle",
    "mark_session_reset",
    "mark_status_card_cleared",
    "mark_status_card_published",
    "pane_idle_clear_due",
    "parse_pending_tools_from_jsonl",
    "reset_for_tests",
    "reset_pane_idle_clear",
    "seed_open_tools",
    "snapshot",
    "subscribe",
    "update_context_usage",
]
