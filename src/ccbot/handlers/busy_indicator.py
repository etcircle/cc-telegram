"""Event-driven run-state machine, sourced from ``TranscriptEvent``.

Single source of truth for "what is Claude doing on this route right now".
Surfaces (status card, native typing action, activity-digest header) read
``state(route)`` instead of re-deriving busy/idle from pane scraping or
content-task ordering.

State transitions are driven by JSONL lifecycle events (``tool_use``,
``tool_result``, ``text``, ``thinking``) plus their carried ``stop_reason``,
per the §2.2.1 transition table in the 2026-05-02 plan. The pane is still
the only signal for interactive UIs that aren't JSONL-visible until they
open — that detection lives in ``status_polling`` and bypasses this module.

Idle decay is on-read (no scheduled tasks) so we don't leak ``call_later``
handles for routes that go away mid-decay.

Public surface:
  - ``RunState``
  - ``register_state_callback(cb)`` — process-lifetime registration; deduped by identity
  - ``state(route)``
  - ``context_pct(route)``
  - ``update_context_pct(route, pct)``
  - ``on_transcript_event(event, routes)``
  - ``mark_topic_broken(route)`` / ``mark_topic_recovered(route)``
  - ``clear_route(route)``
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Awaitable, Callable

from ..session_monitor import TranscriptEvent
from .interactive_ui import INTERACTIVE_TOOL_NAMES

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]


class RunState(Enum):
    RUNNING = "RUNNING"
    RUNNING_TOOL = "RUNNING_TOOL"
    WAITING_ON_USER = "WAITING_ON_USER"
    IDLE_RECENT = "IDLE_RECENT"
    IDLE_CLEARED = "IDLE_CLEARED"
    BROKEN_TOPIC = "BROKEN_TOPIC"


# Seconds an IDLE_RECENT route stays "recent" before decaying to IDLE_CLEARED.
# Mirrors handlers.status_polling.IDLE_CLEAR_DELAY_SECONDS so the visible
# status-card removal and the digest header transition stay in sync.
IDLE_CLEAR_DELAY_SECONDS = 4.0

# stop_reasons that mean "this assistant turn is over"
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})


# Maps tool_use_id → is_interactive. The interactivity bit must travel with
# the id: parallel turns can interleave interactive (AskUserQuestion) and
# non-interactive (Bash) tools, and closing the interactive one alone must
# step the route back to RUNNING_TOOL — not leave it stuck WAITING_ON_USER.
_open_tools: dict[Route, dict[str, bool]] = {}
_run_state: dict[Route, RunState] = {}
_last_event_at: dict[Route, float] = {}
_context_pct: dict[Route, int | None] = {}
# Pre-broken state remembered so a successful event after BROKEN_TOPIC can
# restore where we were rather than guessing.
_pre_broken_state: dict[Route, RunState] = {}

StateCallback = Callable[[Route, RunState, RunState], Awaitable[None]]
_state_callbacks: list[StateCallback] = []


def register_state_callback(callback: StateCallback) -> None:
    """Register a coroutine called on every state transition.

    Callback signature: ``(route, old_state, new_state)``. Multiple callbacks
    are supported; they fire in registration order. Exceptions in one do not
    prevent the next from running.

    Registrations are process-lifetime — there is no unregister. Identity
    dedupe guards against accidental double-registration on bot reload.
    """
    if callback in _state_callbacks:
        return
    _state_callbacks.append(callback)


def _state_from_open_tools(open_tools: dict[str, bool]) -> RunState:
    """Derive the run state from the current open-tool set.

    Empty → RUNNING (turn still in flight, no tools pending).
    Any interactive id → WAITING_ON_USER (user input gates progress).
    Otherwise → RUNNING_TOOL (model is waiting on tool output).
    """
    if not open_tools:
        return RunState.RUNNING
    if any(is_interactive for is_interactive in open_tools.values()):
        return RunState.WAITING_ON_USER
    return RunState.RUNNING_TOOL


def reset_for_tests() -> None:
    """Test-only: drop all state."""
    _run_state.clear()
    _open_tools.clear()
    _last_event_at.clear()
    _context_pct.clear()
    _pre_broken_state.clear()
    _state_callbacks.clear()


def _now() -> float:
    return time.monotonic()


def _maybe_decay_idle(route: Route) -> RunState:
    """Lazily decay IDLE_RECENT → IDLE_CLEARED when read after the delay.

    On-read instead of scheduled call_later avoids leaking timer handles
    for routes that get torn down mid-decay. The cost is one comparison per
    ``state()`` call — trivial.
    """
    current = _run_state.get(route, RunState.IDLE_CLEARED)
    if current is RunState.IDLE_RECENT:
        last = _last_event_at.get(route, 0.0)
        if (_now() - last) >= IDLE_CLEAR_DELAY_SECONDS:
            _run_state[route] = RunState.IDLE_CLEARED
            return RunState.IDLE_CLEARED
    return current


def state(route: Route) -> RunState:
    """Return the route's current ``RunState``.

    Unknown routes default to ``IDLE_CLEARED`` (treat-as-idle): a surface
    asking about a route the indicator has never seen is best off rendering
    "no busy state" rather than fabricating one.
    """
    if route not in _run_state:
        return RunState.IDLE_CLEARED
    return _maybe_decay_idle(route)


def context_pct(route: Route) -> int | None:
    """Return the cached context-window percent for a route, or None."""
    return _context_pct.get(route)


def update_context_pct(route: Route, pct: int | None) -> None:
    """Cache the latest context-window percent observed by status_polling."""
    if pct is None:
        _context_pct.pop(route, None)
    else:
        _context_pct[route] = pct


def clear_route(route: Route) -> None:
    """Drop all state for a route (called from ``teardown_route``)."""
    _run_state.pop(route, None)
    _open_tools.pop(route, None)
    _last_event_at.pop(route, None)
    _context_pct.pop(route, None)
    _pre_broken_state.pop(route, None)


async def _set_state(route: Route, new: RunState) -> None:
    """Mutate state and fire callbacks if it actually changed."""
    old = _run_state.get(route, RunState.IDLE_CLEARED)
    _last_event_at[route] = _now()
    if old is new:
        return
    _run_state[route] = new
    for cb in list(_state_callbacks):
        try:
            await cb(route, old, new)
        except Exception as e:
            logger.error(
                "state callback error route=%s old=%s new=%s: %s",
                route,
                old.value,
                new.value,
                e,
            )


async def _apply_event(event: TranscriptEvent, route: Route) -> None:
    """Apply the §2.2.1 transition table for one route."""
    open_tools = _open_tools.setdefault(route, {})
    # _apply_event uses RUNNING as the prior because we're about to process
    # an event for this route; state() uses IDLE_CLEARED because no event
    # has happened. The two readers see different defaults intentionally.
    current = _run_state.get(route, RunState.RUNNING)

    # Recovery from BROKEN_TOPIC: any successful event restores prior state.
    # We do this BEFORE evaluating event-specific rules so subsequent rules
    # operate on the restored state (e.g. a tool_result that closes the
    # last open tool should still be able to walk to RUNNING).
    if current is RunState.BROKEN_TOPIC:
        prior = _pre_broken_state.pop(route, RunState.RUNNING)
        _run_state[route] = prior
        current = prior

    role = event.role
    block = event.block_type
    stop_reason = event.stop_reason

    # tool_use: open the tool, recording its interactivity. The interactive
    # bit must live with the id so parallel turns mixing AskUserQuestion +
    # Bash settle to the right state when each individual tool_result lands.
    if role == "assistant" and block == "tool_use" and event.tool_use_id:
        is_interactive = bool(
            event.tool_name and event.tool_name in INTERACTIVE_TOOL_NAMES
        )
        open_tools[event.tool_use_id] = is_interactive
        await _set_state(route, _state_from_open_tools(open_tools))
        return

    # tool_result: close the slot if known. Ignore stale ids (could be a
    # pre-startup tool we never saw the matching tool_use for).
    #
    # Why "stale tool_result" is correctly ignored: on bot startup,
    # _open_tools is empty for all routes. A pre-startup tool_result lands
    # here and is dropped — the next assistant event recovers state. The
    # transcript_parser._pending_tools carry-over does NOT seed _open_tools
    # (different layer).
    #
    # Role is intentionally NOT checked: transcript_parser flips tool_result
    # ParsedEntries to role="assistant" so the bubble renders on Claude's
    # side in Telegram, while the raw JSONL envelope is role="user". The
    # block_type + tool_use_id are already specific enough.
    if block == "tool_result" and event.tool_use_id:
        if event.tool_use_id not in open_tools:
            # Stale / pre-startup tool result. Don't touch _last_event_at —
            # that would falsely extend IDLE_RECENT.
            return
        open_tools.pop(event.tool_use_id, None)
        await _set_state(route, _state_from_open_tools(open_tools))
        return

    # Thinking-only message with stop_reason="tool_use" → no transition.
    # The accompanying tool_use event (next message) does the work.
    if role == "assistant" and block == "thinking" and stop_reason == "tool_use":
        _last_event_at[route] = _now()
        return

    # End-of-turn signals: thinking or text with end_turn / stop_sequence
    # AND no open tools → IDLE_RECENT. With open tools we stay in
    # RUNNING_TOOL / WAITING_ON_USER until the matching tool_result lands.
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and not open_tools
    ):
        await _set_state(route, RunState.IDLE_RECENT)
        return

    # Any text event from assistant: route is at least RUNNING. Don't
    # downgrade RUNNING_TOOL / WAITING_ON_USER (open tools still pending).
    if role == "assistant" and block == "text":
        if current in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            _last_event_at[route] = _now()
            return
        await _set_state(route, RunState.RUNNING)
        return

    # Assistant thinking without an end-of-turn stop_reason: keep state,
    # just refresh last_event_at so IDLE_RECENT decay doesn't fire while
    # the model is still thinking.
    if role == "assistant" and block == "thinking":
        _last_event_at[route] = _now()
        return

    # User non-tool_result message (the user prompted Claude): RUNNING.
    if role == "user" and block != "tool_result":
        await _set_state(route, RunState.RUNNING)
        return

    # Fallback: refresh activity timer without state change.
    _last_event_at[route] = _now()


async def on_transcript_event(event: TranscriptEvent, routes: list[Route]) -> None:
    """Apply the transition table for each subscribed route.

    One JSONL event can fan out to multiple routes if multiple users
    follow the same session. ``routes`` is resolved by the bot adapter
    via ``session_manager.find_users_for_session``.
    """
    for route in routes:
        await _apply_event(event, route)


async def mark_topic_broken(route: Route) -> None:
    """Transition a route into BROKEN_TOPIC, remembering the prior state.

    Called by the topic-send classifier when a send lands in
    ``_TOPIC_BROKEN_OUTCOMES``. Recovery happens implicitly on the next
    ``on_transcript_event`` for the route, or explicitly via
    ``mark_topic_recovered``.

    Idempotent: repeated calls do not overwrite the original prior state
    with another BROKEN_TOPIC sentinel.
    """
    current = _run_state.get(route, RunState.RUNNING)
    if current is RunState.BROKEN_TOPIC:
        return
    _pre_broken_state[route] = current
    await _set_state(route, RunState.BROKEN_TOPIC)


async def mark_topic_recovered(route: Route) -> None:
    """Restore a BROKEN_TOPIC route to its pre-broken state.

    Stage 4 wires this into the topic-send success path so a recovery is
    visible in the digest immediately, instead of waiting for the next
    JSONL event (which may never come if Claude already finished its turn).

    No-op if the route isn't currently BROKEN_TOPIC.
    """
    if _run_state.get(route) is not RunState.BROKEN_TOPIC:
        return
    prior = _pre_broken_state.pop(route, RunState.RUNNING)
    await _set_state(route, prior)
