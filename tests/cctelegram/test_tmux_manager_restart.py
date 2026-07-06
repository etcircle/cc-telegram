"""Unit tests for the /update in-place restart mechanics in ``tmux_manager``.

Covers ``pane_command_is_shell``, ``pane_current_command`` (stderr-checked
real-time query), and ``restart_claude_in_window`` — the exact keystroke order,
the FAIL-CLOSED abort when the pane never becomes a shell (the critical safety
test: NO relaunch is sent), and the send-returns-False aborts. Fake tmux only —
no live tmux / claude is ever driven.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram.tmux_manager import (
    RestartOutcome,
    TmuxManager,
    pane_command_is_shell,
)


class TestPaneCommandIsShell:
    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("zsh", True),
            ("-zsh", True),  # login shell
            ("bash", True),
            ("/bin/zsh", True),  # full path → basename
            ("  zsh  ", True),  # whitespace tolerated
            ("nu", True),  # nushell (P2-1 alt-shell fold)
            ("pwsh", True),  # PowerShell
            ("xonsh", True),
            ("-nu", True),  # login-prefixed alt shell
            ("2.1.201", False),  # Claude Code version string → still running
            ("node", False),
            ("claude", False),
            ("", False),
            (None, False),
        ],
    )
    def test_classification(self, cmd, expected):
        assert pane_command_is_shell(cmd) is expected


def _fresh_tm() -> TmuxManager:
    tm = TmuxManager()
    tm.reset_window_send_locks_for_tests()
    return tm


class TestPaneCurrentCommand:
    @pytest.mark.asyncio
    async def test_success_strips_trailing_newline(self, monkeypatch):
        tm = _fresh_tm()
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"zsh\n", b""))
        proc.returncode = 0
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        assert await tm.pane_current_command("@1") == "zsh"

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none(self, monkeypatch):
        tm = _fresh_tm()
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"can't find window"))
        proc.returncode = 1
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        assert await tm.pane_current_command("@1") is None

    @pytest.mark.asyncio
    async def test_stderr_nonempty_returns_none(self, monkeypatch):
        # stderr-checked (the repo gotcha): tmux/libtmux swallow errors silently.
        tm = _fresh_tm()
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"zsh\n", b"some warning"))
        proc.returncode = 0
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        assert await tm.pane_current_command("@1") is None


def _record_send(sent: list[tuple]):
    async def fake_send_keys(
        window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        sent.append((window_id, text, enter, literal))
        return True

    return fake_send_keys


class TestRestartClaudeInWindow:
    @pytest.mark.asyncio
    async def test_happy_path_keystroke_order_then_reassociate(self):
        tm = _fresh_tm()
        sent: list[tuple] = []
        tm.send_keys = _record_send(sent)  # type: ignore[assignment]
        # "node" still running on the first poll, then a shell.
        tm.pane_current_command = AsyncMock(side_effect=["node", "zsh"])  # type: ignore[assignment]
        idle = AsyncMock(return_value=True)
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid-0",
            "/md.json",
            claude_command="mycli --dangerously-skip-permissions",
            idle_recheck=idle,
            reassociate=reassoc,
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.RESTARTED
        idle.assert_awaited_once()
        # (1) quit "/exit" is the FIRST keystroke, (2) relaunch is the SECOND.
        assert sent[0][0:2] == ("@1", "/exit")
        assert sent[1][0] == "@1"
        # Fix 5: the relaunch composes from the INJECTED claude_command (flags
        # and all), not a global.
        assert sent[1][1].startswith("mycli --dangerously-skip-permissions")
        assert "--resume sid-0" in sent[1][1]
        assert "--settings /md.json" in sent[1][1]
        assert len(sent) == 2
        # Relaunch happened only AFTER the pane became a shell (2 polls).
        assert tm.pane_current_command.await_count == 2
        # Re-association ran (after the successful relaunch).
        reassoc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_late_exit_within_grace_is_recovered_and_relaunched(self):
        # P2-1: ``/exit`` is irrevocable — a pane that drops to a shell only
        # AFTER the primary window (but within the grace) must be RECOVERED with
        # a normal relaunch, not stranded as a bare shell in a still-bound topic.
        tm = _fresh_tm()
        sent: list[tuple] = []
        tm.send_keys = _record_send(sent)  # type: ignore[assignment]
        # 10 not-a-shell polls at ≥0.01s apiece put the shell observation
        # strictly past the 0.05s primary window; the injected grace still has
        # ample room (the budgets are injected — no real 15s waits, mirroring
        # the suite's fake-timing pattern).
        tm.pane_current_command = AsyncMock(  # type: ignore[assignment]
            side_effect=["2.1.201"] * 10 + ["zsh"]
        )
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid-0",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=reassoc,
            shell_poll_timeout_s=0.05,
            shell_poll_grace_s=2.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.RESTARTED
        # The relaunch WAS sent — and only after the (late) shell was observed.
        assert sent[0][0:2] == ("@1", "/exit")
        assert "--resume sid-0" in sent[1][1]
        assert len(sent) == 2
        assert tm.pane_current_command.await_count == 11
        reassoc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fail_closed_pane_never_becomes_shell_no_relaunch(self):
        # THE critical safety test: if the pane never drops to a shell — through
        # the primary window AND the grace extension — ABORT: never relaunch
        # into a live TUI, never re-associate.
        tm = _fresh_tm()
        sent: list[tuple] = []
        tm.send_keys = _record_send(sent)  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="2.1.201")  # type: ignore[assignment]
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid-0",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=reassoc,
            shell_poll_timeout_s=0.05,
            shell_poll_grace_s=0.05,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.SKIPPED_NO_EXIT
        # ONLY the quit keystroke was sent — the relaunch was never dispatched.
        assert sent == [("@1", "/exit", True, True)]
        reassoc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_idle_at_lock_time_aborts_before_any_keystroke(self):
        tm = _fresh_tm()
        send = AsyncMock(return_value=True)
        tm.send_keys = send  # type: ignore[assignment]
        pcc = AsyncMock()
        tm.pane_current_command = pcc  # type: ignore[assignment]
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=False),
            reassociate=reassoc,
        )

        assert outcome is RestartOutcome.SKIPPED_NOT_IDLE
        send.assert_not_awaited()
        pcc.assert_not_awaited()
        reassoc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quit_send_false_is_error_no_poll(self):
        tm = _fresh_tm()
        tm.send_keys = AsyncMock(return_value=False)  # type: ignore[assignment]
        pcc = AsyncMock()
        tm.pane_current_command = pcc  # type: ignore[assignment]
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=reassoc,
        )

        assert outcome is RestartOutcome.ERROR
        pcc.assert_not_awaited()  # never polled — quit provably failed
        reassoc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_relaunch_send_false_is_error_no_reassociate(self):
        tm = _fresh_tm()
        # /exit succeeds; the relaunch send returns False (window vanished).
        tm.send_keys = AsyncMock(side_effect=[True, False])  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="zsh")  # type: ignore[assignment]
        reassoc = AsyncMock()

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=reassoc,
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.ERROR
        reassoc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reassociate_raise_after_relaunch_is_error_not_propagated(self):
        # Fix 4: a re-association failure AFTER a successful relaunch is caught
        # and surfaced as ERROR — never propagated to abort the caller's sweep.
        tm = _fresh_tm()
        tm.send_keys = AsyncMock(return_value=True)  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="zsh")  # type: ignore[assignment]
        reassoc = AsyncMock(side_effect=RuntimeError("monitor blew up"))

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=reassoc,
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.ERROR  # did not raise
        reassoc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skipped_no_exit_marks_window_quarantined(self):
        # Hermes P1: /exit is irrevocable — an expired shell-wait leaves the
        # pane in an UNKNOWN state inside a still-bound topic, so the window
        # must be QUARANTINED (send_to_window re-checks before typing).
        tm = _fresh_tm()
        tm.send_keys = _record_send([])  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="2.1.201")  # type: ignore[assignment]

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=AsyncMock(),
            shell_poll_timeout_s=0.02,
            shell_poll_grace_s=0.02,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.SKIPPED_NO_EXIT
        assert tm.window_quarantined("@1") is True

    @pytest.mark.asyncio
    async def test_relaunch_send_failed_marks_quarantined(self):
        # The pane was CONFIRMED a bare shell and the relaunch keystroke send
        # returned False — no Claude was launched, so the bound topic now
        # fronts a bare shell: quarantine.
        tm = _fresh_tm()
        tm.send_keys = AsyncMock(side_effect=[True, False])  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="zsh")  # type: ignore[assignment]

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=AsyncMock(),
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.ERROR
        assert tm.window_quarantined("@1") is True

    @pytest.mark.asyncio
    async def test_reassociate_failure_is_not_quarantined(self):
        # Claude WAS relaunched successfully before reassociate raised — a
        # live TUI owns the pane, so this ERROR path is NOT quarantined.
        tm = _fresh_tm()
        tm.send_keys = AsyncMock(return_value=True)  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="zsh")  # type: ignore[assignment]

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=AsyncMock(side_effect=RuntimeError("monitor blew up")),
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.ERROR
        assert tm.window_quarantined("@1") is False

    @pytest.mark.asyncio
    async def test_quit_send_failed_is_not_quarantined(self):
        # The /exit keystroke send returned False — /exit provably never
        # reached the pane, so Claude still owns it: no quarantine.
        tm = _fresh_tm()
        tm.send_keys = AsyncMock(return_value=False)  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock()  # type: ignore[assignment]

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=AsyncMock(),
        )

        assert outcome is RestartOutcome.ERROR
        assert tm.window_quarantined("@1") is False

    @pytest.mark.asyncio
    async def test_successful_restart_clears_prior_quarantine(self):
        # A later successful restart is positive proof Claude owns the pane
        # again — a stale quarantine from an earlier attempt clears.
        tm = _fresh_tm()
        tm.mark_window_quarantined("@1")
        tm.send_keys = AsyncMock(return_value=True)  # type: ignore[assignment]
        tm.pane_current_command = AsyncMock(return_value="zsh")  # type: ignore[assignment]

        outcome = await tm.restart_claude_in_window(
            "@1",
            "sid",
            "",
            claude_command="claude",
            idle_recheck=AsyncMock(return_value=True),
            reassociate=AsyncMock(),
            shell_poll_timeout_s=1.0,
            shell_poll_interval_s=0.01,
            relaunch_settle_s=0.0,
        )

        assert outcome is RestartOutcome.RESTARTED
        assert tm.window_quarantined("@1") is False

    @pytest.mark.asyncio
    async def test_busy_send_lock_skips_without_any_keystroke(self):
        tm = _fresh_tm()
        lock = tm.window_send_lock("@1")
        await lock.acquire()  # simulate an in-flight keystroke transaction
        try:
            send = AsyncMock(return_value=True)
            tm.send_keys = send  # type: ignore[assignment]
            reassoc = AsyncMock()
            outcome = await tm.restart_claude_in_window(
                "@1",
                "sid",
                "",
                claude_command="claude",
                idle_recheck=AsyncMock(return_value=True),
                reassociate=reassoc,
            )
        finally:
            lock.release()

        assert outcome is RestartOutcome.SKIPPED_BUSY_LOCKED
        send.assert_not_awaited()
        reassoc.assert_not_awaited()
