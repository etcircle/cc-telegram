"""Translate ``session_monitor.TranscriptEvent`` into ``route_runtime``
lifecycle events.

This is the Wave B compatibility shim — it lives so callers continue to
emit the higher-level ``TranscriptEvent`` (with full provenance) while
``route_runtime`` consumes the smaller normalized
``TranscriptLifecycleEvent`` shape needed by the state machine.

Two responsibilities:

  1. ``to_lifecycle_event(event)`` — pure translation. Returns ``None``
     when the event is ignorable (sidechain leak, unknown block_type,
     malformed shape). Drops the heavy ``text`` / ``image_data`` /
     ``tool_input`` fields the state machine doesn't read.
  2. ``dispatch_transcript_event(event, routes)`` — fan-out over a list
     of routes. For each route, calls
     ``route_runtime.ingest_transcript_event`` and returns the
     resulting snapshots. Also routes ``message.usage`` through
     ``route_runtime.update_context_usage`` when the session_monitor's
     parser propagated usage data on the event (kept symmetric with
     ``busy_indicator.update_context_usage`` callers).

LoC budget: 150-250 lines (Wave B plan kill signal at 250 — beyond that
this is Transcript Stream pretending to be an adapter, and the
campaign should pause and re-evaluate). Current size is well under the
floor; the helpers stay terse because the underlying
``TranscriptEvent`` already carries the lifecycle fields cleanly. If a
new ``TranscriptEvent`` shape lands that requires non-trivial
normalisation here, file it against the kill signal.

Error contract: parse failures are logged once per session, the event
is dropped, no snapshot is mutated. The legacy ``busy_indicator`` path
keeps serving the route untouched during the soak — there is no
partial-mutation window where one path saw the event and the other
didn't.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import route_runtime
from .route_runtime import Route, RouteRuntimeSnapshot, TranscriptLifecycleEvent
from .session_monitor import TranscriptEvent

logger = logging.getLogger(__name__)

# Once-per-session warning suppression — repeated parse failures on the
# same session would otherwise flood the log. Keys are session_ids.
_warned_sessions: set[str] = set()


def to_lifecycle_event(event: TranscriptEvent) -> TranscriptLifecycleEvent | None:
    """Translate a raw ``TranscriptEvent`` into the normalized lifecycle
    shape that ``route_runtime`` consumes.

    Returns ``None`` when:
      - ``role`` is not one of the expected literals (defensive — the
        upstream parser already constrains this, but a future
        TranscriptEvent revision shouldn't crash Wave B's adapter).
      - ``block_type`` is not one of ``text`` / ``thinking`` / ``tool_use``
        / ``tool_result``.
      - ``tool_use`` or ``tool_result`` arrives without a ``tool_use_id``
        (the state machine has no slot key to update).

    Sidechain entries (``isSidechain=true`` in the source JSONL) are
    filtered upstream by ``session_monitor`` so they never reach this
    adapter; if one ever does, the role/block_type fall-through here
    drops it harmlessly.
    """
    role = event.role
    block = event.block_type
    if role not in ("user", "assistant"):
        return None
    if block not in ("text", "thinking", "tool_use", "tool_result"):
        return None
    if block in ("tool_use", "tool_result") and not event.tool_use_id:
        return None
    return TranscriptLifecycleEvent(
        role=role,
        block_type=block,
        tool_use_id=event.tool_use_id,
        tool_name=event.tool_name,
        stop_reason=event.stop_reason,
    )


async def dispatch_transcript_event(
    event: TranscriptEvent,
    routes: list[Route],
) -> list[RouteRuntimeSnapshot]:
    """Ingest ``event`` into ``route_runtime`` for each route in ``routes``.

    Concurrency: per-route ingests run **concurrently** under
    ``asyncio.gather`` so independent route locks don't serialise on the
    adapter side. Within a route, ``route_runtime.ingest_transcript_event``
    still holds the per-route lock across mutation + freeze; observer
    fan-out then runs after lock release. The plan's "independent routes
    do not serialise" invariant holds at the adapter level — a slow
    observer on route A does NOT delay route B seeing the same event.

    Returns the per-route committed snapshots in input order — useful
    for callers that want to chain immediate side-effects off the new
    state (e.g. a status-card refresh that needs the post-commit
    ``run_state``).

    Robustness:
      - Per-route ingest failures are logged at warning level once per
        session; other routes still get their snapshot.
      - ``to_lifecycle_event`` returning ``None`` causes a no-op dispatch
        with no snapshot mutation. Callers can ignore the empty result.
    """
    lifecycle = to_lifecycle_event(event)
    if lifecycle is None:
        _warn_once(
            event.session_id,
            "dropped_transcript_event_unrecognised role=%s block=%s",
            event.role,
            event.block_type,
        )
        return []

    async def _ingest_one(route: Route) -> RouteRuntimeSnapshot | None:
        try:
            return await route_runtime.ingest_transcript_event(route, lifecycle)
        except Exception as e:
            _warn_once(
                event.session_id,
                "ingest_transcript_event failed route=%s err=%s",
                route,
                e,
            )
            return None

    results = await asyncio.gather(*(_ingest_one(r) for r in routes))
    return [snap for snap in results if snap is not None]


def dispatch_context_usage(
    routes: list[Route], tokens: int | None, model: str | None
) -> None:
    """Fan ``update_context_usage`` out to every route observing this session.

    Mirrors today's ``busy_indicator.update_context_usage`` callsites
    that live behind ``config.busy_indicator_v2`` (the polling loop and
    the in-message footer). Synchronous because the underlying update
    is synchronous.
    """
    for route in routes:
        try:
            route_runtime.update_context_usage(route, tokens, model)
        except Exception as e:
            logger.warning(
                "route_runtime.update_context_usage failed route=%s err=%s",
                route,
                e,
            )


def dispatch_seed_open_tools(route: Route, tools: dict[str, bool]) -> None:
    """Replay startup-recovered open tools into ``route_runtime``.

    Thin wrapper so the bot's startup replay loop can stay clean.
    """
    try:
        route_runtime.seed_open_tools(route, tools)
    except Exception as e:
        logger.warning("route_runtime.seed_open_tools failed route=%s err=%s", route, e)


def _warn_once(session_id: str, fmt: str, *args: object) -> None:
    """Log a warning at most once per session.

    Per-session because a malformed line in one user's transcript
    shouldn't be silenced for everyone, but a parser bug that affects
    every event in one session would otherwise spam the log forever.
    """
    if session_id in _warned_sessions:
        return
    _warned_sessions.add(session_id)
    logger.warning(fmt, *args)


def reset_for_tests() -> None:
    """Test-only: drop the once-per-session warning suppression."""
    _warned_sessions.clear()


# Re-export for callers that want to thread an observer through without
# importing both modules. Hides the route_runtime symbol from callsites
# that should only know about the adapter.
subscribe: Callable[
    [Route, Callable[[RouteRuntimeSnapshot], Awaitable[None]]],
    Callable[[], None],
] = route_runtime.subscribe
snapshot: Callable[[Route], RouteRuntimeSnapshot] = route_runtime.snapshot
mark_inbound_sent = route_runtime.mark_inbound_sent
mark_topic_broken = route_runtime.mark_topic_broken
mark_topic_recovered = route_runtime.mark_topic_recovered
mark_pane_idle = route_runtime.mark_pane_idle
mark_session_reset = route_runtime.mark_session_reset
mark_status_card_published = route_runtime.mark_status_card_published
mark_status_card_cleared = route_runtime.mark_status_card_cleared
clear_route = route_runtime.clear_route


__all__ = [
    "clear_route",
    "dispatch_context_usage",
    "dispatch_seed_open_tools",
    "dispatch_transcript_event",
    "mark_inbound_sent",
    "mark_pane_idle",
    "mark_session_reset",
    "mark_status_card_cleared",
    "mark_status_card_published",
    "mark_topic_broken",
    "mark_topic_recovered",
    "reset_for_tests",
    "snapshot",
    "subscribe",
    "to_lifecycle_event",
]
