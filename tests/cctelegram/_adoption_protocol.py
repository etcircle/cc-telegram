"""The GH #65 adoption protocol, for tmux test doubles.

Wave 14 removed the ``getattr(...)``-style feature-sniffs from the trust lane:
the adoption protocol is UNCONDITIONAL, because a sniff silently degrades to NO
protocol for any object that happens to lack a seam — which is exactly the
failure a protocol exists to prevent. The consequence is that every tmux double
must carry the seams, so they live here once instead of being copied into each
test module's fake.

The defaults are the trivially-satisfied forms: a double has no real tmux to
race, so nothing is ever kill-pending and every bound is instantly met. A test
that wants to exercise a race overrides the specific seam it cares about.
"""

from __future__ import annotations

import asyncio
from typing import Any


class AdoptionProtocolMixin:
    """Mix into any tmux double used with the trust lane.

    The double is FAITHFUL, not permissive (review r15 P2-C). An earlier version
    answered "nothing is pending, every bound is met, fresh == cached", which
    made every seam-ordering bug invisible: a test could pass while production
    read a stale cache or adopted a window with a kill in flight. Here a kill
    genuinely marks the window pending until its completion callback runs, a
    killed window genuinely disappears from the FRESH listing while a warm
    CACHED listing can still show it, and the bounded-op accounting is the real
    one. Tests that want a specific race still override the single seam they
    care about.
    """

    # ── the pending-kill registry ────────────────────────────────────────
    @property
    def _pending(self) -> dict[str, int]:
        pending = getattr(self, "_kill_pending", None)
        if pending is None:
            pending = {}
            self._kill_pending = pending
        return pending

    @property
    def _dead(self) -> set[str]:
        dead = getattr(self, "_dead_windows", None)
        if dead is None:
            dead = set()
            self._dead_windows = dead
        return dead

    def window_kill_pending(self, window_id: str) -> bool:
        return self._pending.get(window_id, 0) > 0

    def any_kill_pending(self) -> bool:
        return any(count > 0 for count in self._pending.values())

    async def await_kill_settled(self, window_id: str, **kwargs: Any) -> bool:
        del kwargs
        for _ in range(200):
            if not self.window_kill_pending(window_id):
                return True
            await asyncio.sleep(0)
        return False

    async def await_all_kills_settled(self, **kwargs: Any) -> bool:
        del kwargs
        for _ in range(200):
            if not self.any_kill_pending():
                return True
            await asyncio.sleep(0)
        return False

    # ── the two-phase kill ───────────────────────────────────────────────
    def begin_kill_locked(self, window_id: str) -> Any:
        """Register the mark, then dispatch — the production ordering.

        The mark OUTLIVES the dispatch and is cleared by the completion
        callback, so an adopter checking mid-flight genuinely sees it.
        """
        self._pending[window_id] = self._pending.get(window_id, 0) + 1

        async def _run() -> bool:
            return await self.kill_window(window_id)  # type: ignore[attr-defined]

        inner = asyncio.ensure_future(_run())

        def _clear(fut: "asyncio.Future[bool]") -> None:
            remaining = self._pending.get(window_id, 0) - 1
            if remaining > 0:
                self._pending[window_id] = remaining
            else:
                self._pending.pop(window_id, None)
            if not fut.cancelled() and fut.exception() is None and fut.result():
                # A CONFIRMED kill makes the window disappear from FRESH reads.
                self._dead.add(window_id)

        inner.add_done_callback(_clear)
        return inner

    async def finish_kill(self, window_id: str, inner: Any) -> bool:
        del window_id
        # SHIELDED, exactly as production does (review r16 P2). Awaiting
        # ``inner`` bare meant cancelling the WAITER cancelled the fake kill and
        # cleared the pending mark — masking the very cancelled-kill race this
        # protocol exists to test. The mark must stay up until the kill itself
        # genuinely completes.
        return await asyncio.shield(inner)

    # ── the listing seams: fresh and cached are genuinely DIFFERENT ──────
    @property
    def cache_reads(self) -> int:
        """How many times a CACHED listing was read (review r16).

        Adoption seams must produce ZERO — the whole point of the design change
        is that no adoption decision consults the cache.
        """
        return getattr(self, "_cache_reads", 0)

    async def adoption_listing(self) -> Any:
        """The DIRECT read every adoption decision uses.

        Reflects reality immediately: a confirmed kill is gone from here at
        once, with no cache, no generation and nothing to go stale.
        """
        return [
            w
            for w in (getattr(self, "_listing", []) or [])
            if getattr(w, "window_id", None) not in self._dead
        ]

    async def list_windows(self) -> Any:
        """The CACHED view — deliberately still shows a killed window.

        Counted, so a test can assert an adoption seam never came here.
        """
        self._cache_reads = self.cache_reads + 1
        return list(getattr(self, "_listing", []) or [])

    async def list_windows_fresh(self) -> Any:
        return [
            w
            for w in await self.list_windows()
            if getattr(w, "window_id", None) not in self._dead
        ]

    async def find_window_by_id(self, window_id: str, *, fresh: bool = False) -> Any:
        listing = (
            await self.list_windows_fresh() if fresh else await self.list_windows()
        )
        for window in listing:
            if getattr(window, "window_id", None) == window_id:
                return window
        return None

    # ── the lock + the bounded-op accounting ─────────────────────────────
    def window_lifecycle_lock(self) -> Any:
        lock = getattr(self, "_lifecycle_lock", None)
        if lock is None:
            # The REAL wrapper, so a double enforces the same per-hold
            # bounded-op accounting production does (review r15 P2-B/P2-C).
            from cctelegram.tmux_manager import _LifecycleLock

            lock = _LifecycleLock()
            self._lifecycle_lock = lock
        return lock

    async def _bounded_lifecycle(self, coro: Any, *, what: str, **kwargs: Any) -> Any:
        """The REAL accounting, so a double enforces the ceiling too."""
        del kwargs
        lock = self.window_lifecycle_lock()
        if lock.locked():
            lock.note_bounded_op()
        del what
        return await coro
