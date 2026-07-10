"""In-memory cache of the last successful /cost + /usage overlay result.

Populated ONLY by the overlay SUCCESS path in ``bot._run_usage_overlay`` and
read back by the busy-path snapshot fallback, so a bridge-side "as of HH:MM,
N min ago" line can accompany the refusal instead of a dead end. A true leaf:
stdlib only, in-memory only, no state file, no env var, NOT a ``route_runtime``
field, no observer (pull-only).

Core responsibilities:
  - ``record(route, session_id, text)`` — store the rendered overlay snippet
    keyed by ROUTE + the window's CURRENT session identity at write time (a
    later read whose current session differs is a MISS — window ids recycle,
    so window-only keying would leak across sessions / topics).
  - ``peek(route, session_id) -> CachedOverlay | None`` — read within a 30-min
    TTL and only on a session-identity match.
  - ``clear_route`` / ``clear_routes_for_topic`` — teardown mirroring the
    route-scoped ``pane_signals`` seams.
  - ``reset_for_tests()`` — the co-located reset seam.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Route key: (user_id, thread_id_or_0, window_id) — the same tuple the
# route-scoped caches (pane_signals / route_runtime) key on.
Route = tuple[int, int, str]

# Explicitly-accepted SHORT staleness window: limit bars can reset minutes after
# capture, so any multi-hour TTL would serve post-reset percentages as current.
# Parsing the overlay's own reset times is deliberately NOT attempted (drift-
# fragile for marginal gain over the short window + the rendered age label).
CACHE_TTL_SECONDS = 1800.0


@dataclass(frozen=True)
class CachedOverlay:
    """A stored overlay snippet with its wall-clock write time + session key."""

    text: str
    session_id: str
    written_at: float


# route -> CachedOverlay (the newest successful overlay for that route/session).
_cache: dict[Route, CachedOverlay] = {}


def record(
    route: Route,
    session_id: str | None,
    text: str,
    *,
    now: float | None = None,
) -> None:
    """Store the overlay ``text`` for ``route`` under the current ``session_id``.

    A ``None`` / empty ``session_id`` is NOT stored — without a session identity
    there is nothing to key isolation on (a later read could then leak across a
    recycled window id). Overwrites any prior entry for the same route.
    """
    if not session_id:
        return
    _cache[route] = CachedOverlay(
        text=text,
        session_id=session_id,
        written_at=time.time() if now is None else now,
    )


def peek(
    route: Route,
    session_id: str | None,
    *,
    now: float | None = None,
) -> CachedOverlay | None:
    """Return the cached overlay for ``route`` when fresh + session-matched.

    Miss (``None``) when: no entry, the current ``session_id`` differs from the
    write-time identity (recycled window / rotated session), a ``None`` current
    session, or the entry is older than :data:`CACHE_TTL_SECONDS`.
    """
    entry = _cache.get(route)
    if entry is None:
        return None
    if not session_id or entry.session_id != session_id:
        return None
    wall = time.time() if now is None else now
    if wall - entry.written_at > CACHE_TTL_SECONDS:
        return None
    return entry


def clear_route(route: Route) -> None:
    """Drop the cached overlay for a single route (teardown seam)."""
    _cache.pop(route, None)


def clear_routes_for_topic(user_id: int, thread_id_or_0: int) -> None:
    """Drop every cached overlay under ``(user_id, thread_id_or_0)`` (topic teardown)."""
    for key in [r for r in _cache if r[0] == user_id and r[1] == thread_id_or_0]:
        _cache.pop(key, None)


def reset_for_tests() -> None:
    """Clear the whole cache (co-located reset seam)."""
    _cache.clear()
