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
    """Mix into any tmux double used with the trust lane."""

    def window_lifecycle_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_lifecycle_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_lock = lock
        return lock

    def window_kill_pending(self, window_id: str) -> bool:
        del window_id
        return False

    def any_kill_pending(self) -> bool:
        return False

    async def await_kill_settled(self, window_id: str, **kwargs: Any) -> bool:
        del window_id, kwargs
        return True

    async def await_all_kills_settled(self, **kwargs: Any) -> bool:
        del kwargs
        return True

    async def _bounded_lifecycle(self, coro: Any, *, what: str, **kwargs: Any) -> Any:
        del what, kwargs
        return await coro

    async def kill_window_locked(self, window_id: str) -> bool:
        # Delegates so a double that records ``kill_window`` calls keeps working
        # unchanged — the split is a locking concern, not a behavioural one.
        return await self.kill_window(window_id)  # type: ignore[attr-defined]

    def begin_kill_locked(self, window_id: str) -> Any:
        """Two-phase kill: dispatch under the lock, await outside it.

        The double has no worker thread, so "dispatch" is just the coroutine and
        ``finish_kill`` awaits it — the recorded ``kill_window`` call still
        happens exactly once, at the same point in the sequence.
        """
        return asyncio.ensure_future(self.kill_window(window_id))  # type: ignore[attr-defined]

    async def finish_kill(self, window_id: str, inner: Any) -> bool:
        del window_id
        return await inner

    async def list_windows_fresh(self) -> Any:
        return await self.list_windows()  # type: ignore[attr-defined]

    async def find_window_by_id(self, window_id: str, *, fresh: bool = False) -> Any:
        del fresh
        for window in await self.list_windows():  # type: ignore[attr-defined]
            if getattr(window, "window_id", None) == window_id:
                return window
        return None

    async def list_windows(self) -> Any:
        # A double that models no windows still satisfies "the listing worked
        # and did not disprove anything" — absence must be PROVEN, and an empty
        # listing proves nothing (the r11 P1-A rule).
        return []
