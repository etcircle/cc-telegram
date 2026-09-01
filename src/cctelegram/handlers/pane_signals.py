"""Pane-derived per-route decoration signals — the background-jobs store (GH #43).

A tiny in-memory leaf holding the latest pane-parsed background-task counts
per route — shells, monitors and the mixed-family ``background task(s)``
fallback (GH #86) — written by ``status_polling`` on every full pane capture
and PULLED by the renderers (the collapsed done-card in ``message_queue`` and
the ``/dashboard`` row) — a decoration, NEVER a run-state input:
``route_runtime`` stays the sole run-state authority, nothing here observes or
pushes (the c313657 pattern stays forbidden), and typing never fires off this
store (user decision recorded on GH #43 and reaffirmed on GH #86: typing
promises imminent output; a background shell or a watcher does not).

Core responsibilities:
  - ``record_background_jobs(route, shells, monitors, tasks, *, now)`` —
    poller write; returns whether the rendered PHRASE changed so the caller
    can trigger a digest repaint (a pull-side refresh, not an observer).
  - ``describe_background_jobs(shells, monitors, tasks)`` — the ONE phrase
    both renderers use (without the ⏳ glyph); ``None`` when nothing is live.
  - ``peek_background_jobs(route, *, now)`` — renderer read with staleness:
    a record older than ``BG_JOBS_MAX_AGE_S`` (3× the poller's 10s capture
    watchdog) reads as ``None`` so a dead window can't advertise a phantom
    job forever.
  - ``clear_route`` / ``clear_routes_for_topic`` — teardown seams, wired
    beside every ``route_runtime`` route-clearing callsite.

True leaf: imports nothing from the application (keeps the import graph
acyclic by construction; the subprocess import-cycle gate covers it).
"""

from __future__ import annotations

from dataclasses import dataclass

# (user_id, thread_id_or_0, window_id) — structurally route_runtime.Route,
# re-declared locally so this module stays a leaf.
Route = tuple[int, int, str]

# Staleness horizon for a recorded count: 3× status_polling's 10s capture
# watchdog — a live window refreshes the record well inside this; past it
# the decoration silently hides rather than showing a stale ⏳.
BG_JOBS_MAX_AGE_S = 30.0


@dataclass(frozen=True)
class BackgroundJobs:
    """One pane observation: per-family counts + capture wall-clock."""

    shells: int
    monitors: int
    tasks: int
    captured_at: float


_signals: dict[Route, BackgroundJobs] = {}


def describe_background_jobs(shells: int, monitors: int, tasks: int) -> str | None:
    """The ONE rendered phrase for a set of counts, WITHOUT the ⏳ glyph.

    ``1 background shell`` · ``waiting on 2 monitors`` · ``1 background shell ·
    waiting on 2 monitors`` · ``3 background tasks`` (the mixed-family
    fallback, appended to whatever else is live). ``None`` when nothing is
    live — which is what HIDES the decoration at both renderers.
    """
    parts: list[str] = []
    if shells > 0:
        parts.append(f"{shells} background shell{'s' if shells != 1 else ''}")
    if monitors > 0:
        parts.append(f"waiting on {monitors} monitor{'s' if monitors != 1 else ''}")
    if tasks > 0:
        parts.append(f"{tasks} background task{'s' if tasks != 1 else ''}")
    return " · ".join(parts) if parts else None


def record_background_jobs(
    route: Route, shells: int, monitors: int, tasks: int, *, now: float
) -> bool:
    """Record the pane-parsed background-task counts for ``route``.

    The counts are the parser's non-``None`` result — all-zero means "chrome
    present, positively nothing in flight" and is recorded (it HIDES the
    decoration; recording it is what lets a finished shell's ⏳ disappear).
    ``None`` results (no chrome / failed capture) must not reach here — the
    caller skips them so a bad frame can't erase a fresh record.

    Returns True iff the RENDERED PHRASE changed (hermes GH #43 diff P2):
    what renders is ``describe_background_jobs`` over a FRESH record vs
    nothing at all, so the comparison is between rendered states, not raw
    counts — a record that went STALE and is now re-observed at the same
    counts must repaint (the card dropped nothing while stale only because
    nothing re-rendered; the next render after this record must be
    triggered). The caller uses True to fire a digest repaint; a
    same-rendered-value refresh only re-stamps freshness.
    """
    prev = _signals.get(route)
    prev_phrase: str | None = None
    if prev is not None and (now - prev.captured_at) <= BG_JOBS_MAX_AGE_S:
        prev_phrase = describe_background_jobs(prev.shells, prev.monitors, prev.tasks)
    _signals[route] = BackgroundJobs(
        shells=shells, monitors=monitors, tasks=tasks, captured_at=now
    )
    if prev is None:
        return True
    return prev_phrase != describe_background_jobs(shells, monitors, tasks)


def peek_background_jobs(
    route: Route, *, now: float, max_age: float = BG_JOBS_MAX_AGE_S
) -> BackgroundJobs | None:
    """Return the fresh record for ``route``; ``None`` when absent or stale."""
    rec = _signals.get(route)
    if rec is None or now - rec.captured_at > max_age:
        return None
    return rec


def clear_route(route: Route) -> None:
    """Drop the record for one route (window-gone / session-reset seams)."""
    _signals.pop(route, None)


def clear_routes_for_topic(user_id: int, thread_id_or_0: int) -> None:
    """Drop every route under ``(user_id, thread_id_or_0)`` (topic teardown)."""
    for key in [k for k in _signals if k[0] == user_id and k[1] == thread_id_or_0]:
        _signals.pop(key, None)


def reset_for_tests() -> None:
    """Test-only: drop all records."""
    _signals.clear()


__all__ = [
    "BG_JOBS_MAX_AGE_S",
    "BackgroundJobs",
    "Route",
    "clear_route",
    "clear_routes_for_topic",
    "describe_background_jobs",
    "peek_background_jobs",
    "record_background_jobs",
    "reset_for_tests",
]
