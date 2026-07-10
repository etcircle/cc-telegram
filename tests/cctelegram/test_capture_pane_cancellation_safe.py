"""Tests for ``TmuxManager.capture_pane_cancellation_safe``.

The /cost preflight + post-send captures run under ``asyncio.wait_for`` with a
hard deadline. ``capture_pane`` has no subprocess timeout, so a hung tmux would
be cancelled by ``wait_for`` — and the raw ``capture_pane`` would ORPHAN its
subprocess. The cancellation-safe wrapper best-effort ``proc.kill()`` +
``await proc.wait()`` in a ``finally`` on ``CancelledError`` before re-raising,
so repeated /cost against a hung tmux never accumulates zombies.

The DEFAULT ``capture_pane`` semantics must stay byte-identical for every other
caller — only the new method reaps on cancellation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.tmux_manager import TmuxManager


def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _hanging_proc():
    """A proc whose communicate() never returns (until cancelled)."""
    proc = MagicMock()

    async def _never(*_a, **_kw):
        await asyncio.Event().wait()  # blocks forever

    proc.communicate = _never
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.fixture
def manager() -> TmuxManager:
    return TmuxManager()


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_captured_text(self, manager: TmuxManager) -> None:
        with patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_proc(stdout=b"pane body\n")),
        ):
            out = await manager.capture_pane_cancellation_safe("@1")
        assert out == "pane body\n"

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none(self, manager: TmuxManager) -> None:
        with patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_make_proc(stderr=b"boom", returncode=1)),
        ):
            out = await manager.capture_pane_cancellation_safe("@1")
        assert out is None


class TestCancellationReaping:
    @pytest.mark.asyncio
    async def test_wait_for_timeout_kills_and_reaps_proc(
        self, manager: TmuxManager
    ) -> None:
        proc = _hanging_proc()
        with patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    manager.capture_pane_cancellation_safe("@1"), timeout=0.05
                )
        # The orphan subprocess was killed AND reaped in the finally.
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_direct_cancel_kills_and_reaps(self, manager: TmuxManager) -> None:
        proc = _hanging_proc()
        with patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            task = asyncio.create_task(manager.capture_pane_cancellation_safe("@1"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_exception_is_swallowed_and_cancelled_reraises(
        self, manager: TmuxManager
    ) -> None:
        proc = _hanging_proc()
        proc.kill = MagicMock(side_effect=ProcessLookupError())
        with patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    manager.capture_pane_cancellation_safe("@1"), timeout=0.05
                )
