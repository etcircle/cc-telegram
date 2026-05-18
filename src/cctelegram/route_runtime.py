"""Per-route snapshot state machine — the Wave B successor to the dual
``busy_indicator`` + ``status_polling._idle_state`` state machines.

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

What it replaces (during Wave B coexistence; replacement completes after
the ≥48h soak):

  - ``busy_indicator._run_state``: now ``snapshot.run_state``.
  - ``busy_indicator._open_tools``: now ``snapshot.open_tools`` /
    ``snapshot.waiting_on_user_tools``.
  - ``busy_indicator._context_usage``: now ``snapshot.context_usage``.
  - ``busy_indicator._pre_broken_state``: internal-only here, exposed
    via ``snapshot.broken_topic`` (bool) and recovery logic.
  - ``status_polling._idle_state``: now ``snapshot.idle_clear_at`` plus
    the ``mark_pane_idle`` consumer.
  - ``handlers.message_queue._status_msg_info[skey]`` lifecycle
    (visibility, not msg_id storage — message_queue stays the sole
    sender/editor): now ``snapshot.status_card_visible`` /
    ``snapshot.status_card_msg_id``.

What it does **not** replace in Wave B:

  - ``message_queue._status_msg_info`` as the send-layer cache.
    message_queue remains the sole sender/editor of status cards. It
    queries ``snapshot.status_card_visible`` to pick edit-vs-send and
    calls ``mark_status_card_published`` after a successful send. If
    Wave B needs to mutate message_queue internals beyond that, **stop
    and promote Route Outbox** — that's the kill signal.

Concurrency contract:

  - One per-route ``asyncio.Lock``. Independent routes do not serialize.
  - Async mutators (``ingest_transcript_event``, ``mark_inbound_sent``,
    ``mark_topic_broken`` / ``mark_topic_recovered``, ``mark_pane_idle``,
    ``mark_session_reset``) acquire the route's lock, mutate, freeze a
    snapshot, **then** release the lock before fanning observers out
    against the committed snapshot. Observers can therefore call back
    into ``snapshot(other_route)`` / ``ingest_*`` without deadlocking.
  - **Synchronous side-band writes** (``mark_status_card_published`` /
    ``mark_status_card_cleared``, ``update_context_usage``,
    ``seed_open_tools``, ``clear_route``) intentionally bypass the
    lock. They are bookkeeping for read-side flags — they don't change
    ``run_state`` (no transition table runs), don't fire observers
    (no fan-out), and don't await between their initial read of
    ``_state`` and the field write. Safe under Python's single-threaded
    asyncio scheduling because no suspension point separates the read
    from the write. **Do not call these from a thread** — they assume
    event-loop-thread execution.
  - Every committed transition increments ``_global_seq``; the snapshot
    carries ``monotonic_seq`` so subscribers can dedupe / detect drops.
  - Pane snapshots (``mark_pane_idle``) are reconciliation events with
    **lower authority** than transcript lifecycle events: they preserve
    ``WAITING_ON_USER`` and ``BROKEN_TOPIC``, only clearing
    ``RUNNING`` / ``RUNNING_TOOL`` to ``IDLE_CLEARED`` after the
    debounce delay has elapsed. This mirrors today's
    ``busy_indicator.mark_pane_idle`` behaviour so the visible "🟡 Busy"
    card and the run-state machine stay in sync.

Persistence policy:

  - In-memory by default. ``open_tools`` reconstructs from JSONL replay
    on startup via ``seed_open_tools`` (the same pattern as
    ``busy_indicator``). ``broken_topic`` and ``status_card_*`` are
    not persisted in Wave B — restart-induced loss is self-healing
    (next event clears BROKEN_TOPIC; next status-card send re-publishes
    the msg_id). When persistence is needed it will land via a
    state.json ``schema_version`` bump in a follow-up wave.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from .handlers.busy_indicator import (
    IDLE_CLEAR_DELAY_SECONDS,
    ContextUsage,
    RunState,
)
from .handlers.interactive_ui import INTERACTIVE_TOOL_NAMES

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]

# stop_reasons that signal "this assistant turn is over". Mirrors
# ``busy_indicator._TURN_END_REASONS``; kept inline so route_runtime can
# survive a future detangle of busy_indicator without an import dance.
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})

# Same threshold as busy_indicator: a route observed strictly above this
# token count must be on the 1M variant.
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
      - ``status_polling._on_busy_activity`` (legacy) no longer reads
        snapshots; under v2 the snapshot itself replaces the
        ``_idle_state`` re-arm bookkeeping.

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
    typing_eligible: bool
    status_card_visible: bool
    status_card_msg_id: int | None
    broken_topic: bool
    monotonic_seq: int


@dataclass
class _RouteState:
    """Mutable internal state — lives behind the route's lock."""

    run_state: RunState = RunState.IDLE_CLEARED
    open_tools: dict[str, bool] = field(default_factory=dict)
    context_usage: ContextUsage | None = None
    last_event_at: float = 0.0
    idle_clear_at: float | None = None
    pre_broken_state: RunState | None = None
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
    """Same derivation as ``busy_indicator._state_from_open_tools``.

    Inlined so route_runtime is self-contained for the soak window;
    cleanup wave deletes the busy_indicator duplicate, not this copy.
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
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        broken_topic=st.run_state is RunState.BROKEN_TOPIC,
        monotonic_seq=_next_seq(),
    )


def _default_snapshot(route: Route) -> RouteRuntimeSnapshot:
    """Snapshot of an unknown route — used by ``snapshot()`` for routes
    that have never been observed.

    Mirrors ``busy_indicator.state(unknown_route) == IDLE_CLEARED``.
    Does NOT consume a sequence number — pure observation, no commit.
    """
    return RouteRuntimeSnapshot(
        route=route,
        run_state=RunState.IDLE_CLEARED,
        open_tools=frozenset(),
        waiting_on_user_tools=frozenset(),
        context_usage=None,
        last_event_at=0.0,
        idle_clear_at=None,
        typing_eligible=False,
        status_card_visible=False,
        status_card_msg_id=None,
        broken_topic=False,
        monotonic_seq=0,
    )


# ── pure transitions (callers hold the lock) ────────────────────────────


def _decay_idle_in_place(st: _RouteState) -> None:
    """Apply lazy IDLE_RECENT → IDLE_CLEARED decay.

    Mirrors ``busy_indicator._maybe_decay_idle``. Called immediately
    before freezing a snapshot when no state-affecting event ran — i.e.
    during pure reads via ``snapshot()`` — so a stale IDLE_RECENT
    doesn't survive past its delay just because nothing else has
    happened on the route.
    """
    if st.run_state is RunState.IDLE_RECENT and st.idle_clear_at is not None:
        if _now() >= st.idle_clear_at:
            st.run_state = RunState.IDLE_CLEARED
            st.idle_clear_at = None


def _recover_from_broken(st: _RouteState) -> None:
    """If the route was BROKEN_TOPIC, restore pre-broken state.

    Runs before event-specific rules in ``_apply_lifecycle_event`` so
    subsequent rules operate on the recovered state — e.g. a tool_result
    that closes the last open tool still walks to RUNNING.
    """
    if st.run_state is RunState.BROKEN_TOPIC:
        st.run_state = st.pre_broken_state or RunState.RUNNING
        st.pre_broken_state = None


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

    Identical logic to ``busy_indicator._apply_event``, restructured to
    operate on ``_RouteState`` without async (the lock is the caller's
    responsibility).
    """
    _recover_from_broken(st)
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
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        broken_topic=st.run_state is RunState.BROKEN_TOPIC,
        monotonic_seq=_global_seq,  # read-only — no commit
    )


# ── public API: mark_* mutations ────────────────────────────────────────


async def mark_inbound_sent(route: Route) -> RouteRuntimeSnapshot:
    """A Telegram-originated prompt was delivered to Claude's tmux window.

    Idempotent: never downgrades RUNNING_TOOL / WAITING_ON_USER /
    BROKEN_TOPIC. Otherwise transitions to RUNNING so the typing
    indicator and status card show activity before the first JSONL
    event lands.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state not in (
            RunState.RUNNING_TOOL,
            RunState.WAITING_ON_USER,
            RunState.BROKEN_TOPIC,
        ):
            _set_run_state(st, RunState.RUNNING)
        else:
            st.last_event_at = _now()
            st.seen = True
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


async def mark_topic_broken(route: Route) -> RouteRuntimeSnapshot:
    """Outbound Telegram send classified as ``TOPIC_BROKEN_OUTCOMES`` —
    transition to ``BROKEN_TOPIC``, remembering the prior state.

    Idempotent: repeated calls don't overwrite ``pre_broken_state``
    with another ``BROKEN_TOPIC`` sentinel.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state is not RunState.BROKEN_TOPIC:
            st.pre_broken_state = st.run_state
            _set_run_state(st, RunState.BROKEN_TOPIC)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


async def mark_topic_recovered(route: Route) -> RouteRuntimeSnapshot:
    """Restore a ``BROKEN_TOPIC`` route to its pre-broken state.

    No-op if the route isn't currently ``BROKEN_TOPIC``.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state is RunState.BROKEN_TOPIC:
            prior = st.pre_broken_state or RunState.RUNNING
            st.pre_broken_state = None
            _set_run_state(st, prior)
        snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


async def mark_pane_idle(route: Route) -> RouteRuntimeSnapshot:
    """Pane has been confirmed idle for ``IDLE_CLEAR_DELAY_SECONDS``.

    Reconciliation-only — pane snapshots have **lower authority** than
    transcript lifecycle events:

      - ``WAITING_ON_USER`` is preserved (interactive prompt is open).
      - ``BROKEN_TOPIC`` is preserved (recovery gates on a real event).
      - Otherwise drops any lingering open tools and transitions to
        ``IDLE_CLEARED``.

    Mirrors ``busy_indicator.mark_pane_idle`` so the visible "🟡 Busy"
    card removal and the run-state machine stay in sync.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state in (RunState.WAITING_ON_USER, RunState.BROKEN_TOPIC):
            snap = _freeze(route, st)
        else:
            st.open_tools.clear()
            st.invalidate_tool_cache()
            _set_run_state(st, RunState.IDLE_CLEARED)
            snap = _freeze(route, st)
    await _fan_out(route, snap)
    return snap


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
        st.pre_broken_state = None
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

    Mirrors ``busy_indicator.update_context_usage`` semantics: ``None``
    or non-positive tokens drops the entry. The 1M cap latches once
    observed (a 200k session can't legitimately exceed its cap; below
    threshold defaults to 200k).
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

    Wave B exit criterion: this is the **single** test-side reset seam
    that covers the surfaces previously needing separate
    ``reset_for_tests`` calls in ``busy_indicator``, ``message_queue``
    (for ``_status_msg_info`` lifecycle), ``status_polling``
    (for ``_idle_state``), and ``interactive_ui``. The other modules'
    seams stay during the soak window; the cleanup wave consolidates
    after the legacy paths are deleted.
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
    "clear_route",
    "ingest_transcript_event",
    "mark_inbound_sent",
    "mark_pane_idle",
    "mark_session_reset",
    "mark_status_card_cleared",
    "mark_status_card_published",
    "mark_topic_broken",
    "mark_topic_recovered",
    "reset_for_tests",
    "seed_open_tools",
    "snapshot",
    "subscribe",
    "update_context_usage",
]
