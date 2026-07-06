"""Wave 3a: per-window send-lock registry + FIFO ``send_to_window`` (finding 9).

The registry lives on ``TmuxManager`` (``window_send_lock``); the FIFO contract
is that two concurrent ``SessionManager.send_to_window`` calls on the SAME
window serialize their whole text→settle→Enter transactions (strict
textA, EnterA, textB, EnterB), while sends to DIFFERENT windows do not
serialize against each other.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cctelegram import session as session_mod
from cctelegram.session import session_manager
from cctelegram.tmux_manager import TmuxManager, tmux_manager as real_tmux

Event = tuple[str, str, str]  # (phase, window_id, text)


def _recording_send(
    events: list[Event],
    *,
    settle: float = 0.01,
    block_until: dict[str, asyncio.Event] | None = None,
):
    """Fake ``tmux_manager.send_keys`` that records the text→Enter transaction.

    Models the production literal+enter path (text, 500ms settle, Enter) with a
    small await between the two phases so concurrent unserialized callers
    provably interleave pre-fix.
    """

    async def fake_send_keys(
        window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        events.append(("text", window_id, text))
        if block_until is not None and window_id in block_until:
            await block_until[window_id].wait()
        else:
            await asyncio.sleep(settle)
        events.append(("enter", window_id, text))
        return True

    return fake_send_keys


async def _fake_find(window_id: str):
    await asyncio.sleep(0)  # a real lookup always yields at least once
    return SimpleNamespace(window_id=window_id)


@pytest.fixture(autouse=True)
def _fresh_locks():
    real_tmux.reset_window_send_locks_for_tests()
    yield
    real_tmux.reset_window_send_locks_for_tests()


# ── finding-9 FIFO contract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_sends_same_window_strict_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent sends to ONE window serialize as whole transactions."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))

    await asyncio.gather(
        session_manager.send_to_window("@7", "A"),
        session_manager.send_to_window("@7", "B"),
    )
    assert events == [
        ("text", "@7", "A"),
        ("enter", "@7", "A"),
        ("text", "@7", "B"),
        ("enter", "@7", "B"),
    ]


@pytest.mark.asyncio
async def test_sends_to_different_windows_do_not_serialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow transaction on @1 must NOT delay an independent send to @2."""
    events: list[Event] = []
    release_w1 = asyncio.Event()
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(
        real_tmux,
        "send_keys",
        _recording_send(events, block_until={"@1": release_w1}),
    )

    slow = asyncio.create_task(session_manager.send_to_window("@1", "slow"))
    # Wait until @1's transaction is mid-flight (text sent, Enter pending).
    for _ in range(200):
        if ("text", "@1", "slow") in events:
            break
        await asyncio.sleep(0.001)
    assert ("text", "@1", "slow") in events

    ok, _ = await asyncio.wait_for(session_manager.send_to_window("@2", "fast"), 1.0)
    assert ok
    assert ("enter", "@2", "fast") in events
    assert ("enter", "@1", "slow") not in events  # @1 still mid-transaction

    release_w1.set()
    await slow
    assert events[-1] == ("enter", "@1", "slow")


# ── registry lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_send_lock_identity_per_window() -> None:
    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    assert mgr.window_send_lock("@1") is lock1
    assert mgr.window_send_lock("@2") is not lock1


@pytest.mark.asyncio
async def test_failed_kill_keeps_lock_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave 3a Hermes P3: a FAILED kill can leave the window alive with an
    in-flight holder — dropping the entry would hand a later acquirer a
    fresh lock for the same live window (the split-lock class). Keep it."""
    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    monkeypatch.setattr(mgr, "get_session", lambda: None)  # kill fails
    assert await mgr.kill_window("@1") is False
    assert mgr.window_send_lock("@1") is lock1


@pytest.mark.asyncio
async def test_successful_kill_drops_lock_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeWindow:
        def kill(self) -> None:
            pass

    class _FakeWindows:
        def get(self, window_id: str) -> _FakeWindow:
            return _FakeWindow()

    class _FakeSession:
        windows = _FakeWindows()

    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    monkeypatch.setattr(mgr, "get_session", lambda: _FakeSession())
    assert await mgr.kill_window("@1") is True
    assert mgr.window_send_lock("@1") is not lock1


# ── P1 post-/exit quarantine gate at the send seam ─────────────────────────
#
# A /update that irrevocably sent /exit but could not confirm a relaunch
# quarantines the window; ``send_to_window`` must re-check the live pane
# command BEFORE typing user text into what may be a bare shell.


@pytest.fixture
def _fresh_quarantine():
    real_tmux.reset_window_quarantines_for_tests()
    yield
    real_tmux.reset_window_quarantines_for_tests()


@pytest.mark.asyncio
async def test_quarantined_window_shell_pane_refuses_send(
    monkeypatch: pytest.MonkeyPatch, _fresh_quarantine
) -> None:
    """THE P1 scenario: a message queued behind the restart's lock flushes
    after SKIPPED_NO_EXIT and the pane is now a bare shell — the send must be
    REFUSED (nothing typed; typing + Enter would EXECUTE it)."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))
    monkeypatch.setattr(
        real_tmux, "pane_current_command", AsyncMock(return_value="zsh")
    )
    real_tmux.mark_window_quarantined("@1")

    ok, msg = await session_manager.send_to_window("@1", "rm -rf ./scratch")

    assert ok is False
    assert msg == session_mod.QUARANTINE_SEND_REFUSED_MSG
    assert "NOT delivered" in msg and "/update" in msg
    assert events == []  # nothing was typed into the bare shell
    assert real_tmux.window_quarantined("@1") is True  # kept — still a shell


@pytest.mark.asyncio
async def test_quarantined_window_claude_running_clears_and_delivers(
    monkeypatch: pytest.MonkeyPatch, _fresh_quarantine
) -> None:
    """Claude alive — STRICTLY the version-string pane command
    (``pane_command_is_claude``) — is positive proof: the quarantine clears
    and the message is delivered normally."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))
    monkeypatch.setattr(
        real_tmux, "pane_current_command", AsyncMock(return_value="2.1.201")
    )
    real_tmux.mark_window_quarantined("@1")

    ok, _ = await session_manager.send_to_window("@1", "hello")

    assert ok is True
    assert ("enter", "@1", "hello") in events
    assert real_tmux.window_quarantined("@1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_cmd", ["vim", "python", "node", "ssh"])
async def test_quarantined_window_foreign_command_refuses(
    monkeypatch: pytest.MonkeyPatch, _fresh_quarantine, foreign_cmd: str
) -> None:
    """r2 P1-B: "any non-shell" is NOT "Claude alive" — a user who followed
    the summary's "check the window" advice and ran vim/python/ssh in the
    stranded pane must NOT clear the quarantine; typing + Enter would land in
    THAT program. Only the strict version-string shape is proof of life."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))
    monkeypatch.setattr(
        real_tmux, "pane_current_command", AsyncMock(return_value=foreign_cmd)
    )
    real_tmux.mark_window_quarantined("@1")

    ok, msg = await session_manager.send_to_window("@1", "hello")

    assert ok is False
    assert msg == session_mod.QUARANTINE_SEND_REFUSED_MSG
    assert events == []  # nothing typed into vim/python/ssh
    assert real_tmux.window_quarantined("@1") is True


@pytest.mark.asyncio
async def test_quarantined_window_query_failure_refuses(
    monkeypatch: pytest.MonkeyPatch, _fresh_quarantine
) -> None:
    """A failed pane_current_command query (None) is NOT proof of life —
    fail closed: refuse, keep the quarantine."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))
    monkeypatch.setattr(real_tmux, "pane_current_command", AsyncMock(return_value=None))
    real_tmux.mark_window_quarantined("@1")

    ok, msg = await session_manager.send_to_window("@1", "hello")

    assert ok is False
    assert msg == session_mod.QUARANTINE_SEND_REFUSED_MSG
    assert events == []
    assert real_tmux.window_quarantined("@1") is True


@pytest.mark.asyncio
async def test_unquarantined_send_never_queries_pane(
    monkeypatch: pytest.MonkeyPatch, _fresh_quarantine
) -> None:
    """Zero overhead for normal windows: no pane_current_command subprocess."""
    events: list[Event] = []
    pcc = AsyncMock()
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))
    monkeypatch.setattr(real_tmux, "pane_current_command", pcc)

    ok, _ = await session_manager.send_to_window("@1", "hello")

    assert ok is True
    pcc.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_kill_clears_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown seam: a killed window's quarantine must not leak onto a later
    window that reuses the id (tmux ids reset on server restart)."""

    class _FakeWindow:
        def kill(self) -> None:
            pass

    class _FakeWindows:
        def get(self, window_id: str) -> _FakeWindow:
            return _FakeWindow()

    class _FakeSession:
        windows = _FakeWindows()

    mgr = TmuxManager(session_name="lock-test")
    mgr.mark_window_quarantined("@1")
    monkeypatch.setattr(mgr, "get_session", lambda: _FakeSession())
    assert await mgr.kill_window("@1") is True
    assert mgr.window_quarantined("@1") is False


def test_window_send_lock_survives_event_loop_replacement() -> None:
    """A registry entry from a dead loop is recreated, never reused.

    asyncio.Lock binds to the loop it is first acquired under; tests run a
    fresh loop per test against the module singleton, so reuse across loops
    would raise "is bound to a different event loop".
    """
    mgr = TmuxManager(session_name="lock-test")

    async def grab() -> None:
        async with mgr.window_send_lock("@1"):
            pass

    asyncio.run(grab())
    asyncio.run(grab())  # must not raise
