"""Unit tests for the /update orchestration (``handlers.updater``).

Covers ``run_update`` bucketing (restart idle, defer busy, skip gone/no-session),
single-flight rejection, the ``shlex.split`` CLI-update executable extraction,
the idle gate (``route_is_idle``), and the routing re-association — including the
offset-reset regression: a stale ``tracked_offset > filesize`` must NOT trigger
the reset-to-0 history flood. Fake tmux + injected collaborators; the
``claude update`` subprocess is always mocked, never really run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import updater
from cctelegram.monitor_state import TrackedSession
from cctelegram.session_monitor import SessionMonitor
from cctelegram.tmux_manager import RestartOutcome

FIX = Path(__file__).parent / "fixtures"

_SEP = "─" * 56
IDLE_PANE = f"""\
✻ Cooked for 2s

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


@pytest.fixture(autouse=True)
def _reset_singleflight(monkeypatch):
    updater.reset_for_tests()
    # Safety net: NEVER let a test spawn a real `claude update`. Individual
    # TestRunCliUpdate tests re-``monkeypatch`` this with their own asserting
    # mock (which wins for that test); everything else gets a benign success.
    _proc = MagicMock()
    _proc.communicate = AsyncMock(return_value=(b"", b""))
    _proc.returncode = 0
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=_proc)
    )
    yield
    updater.reset_for_tests()


class _WS:
    def __init__(self, session_id: str = "", cwd: str = "/proj"):
        self.session_id = session_id
        self.cwd = cwd


class FakeSessionMgr:
    def __init__(self, bindings, window_states=None, names=None, file_path=None):
        self._bindings = list(bindings)
        self._ws = window_states or {}
        self._names = names or {}
        self._file_path = file_path
        self.saves = 0

    def iter_thread_bindings(self):
        return iter(self._bindings)

    def get_display_name(self, wid):
        return self._names.get(wid, wid)

    def get_window_state(self, wid):
        return self._ws.setdefault(wid, _WS())

    def _build_session_file_path(self, sid, cwd):
        return self._file_path

    def _save_state(self):
        self.saves += 1


class FakeTmux:
    """Fake tmux: find/capture + a restart that (optionally) drives the closures."""

    def __init__(
        self,
        *,
        windows,
        outcome_map=None,
        invoke_closures=False,
        pane=None,
        raise_for=None,
    ):
        self._windows = set(windows)
        self._outcome_map = outcome_map or {}
        self._invoke = invoke_closures
        self._pane = pane
        self._raise_for = set(raise_for or ())
        self.restart_calls: list[tuple] = []

    async def find_window_by_id(self, wid):
        return SimpleNamespace(window_id=wid) if wid in self._windows else None

    async def capture_pane(self, wid, **kw):
        return self._pane

    async def restart_claude_in_window(
        self,
        wid,
        tracked_sid,
        md_settings,
        *,
        claude_command,
        idle_recheck,
        reassociate,
        **kw,
    ):
        self.restart_calls.append((wid, tracked_sid, md_settings, claude_command))
        if wid in self._raise_for:
            raise RuntimeError(f"boom on {wid}")
        if self._invoke:
            if not await idle_recheck():
                return RestartOutcome.SKIPPED_NOT_IDLE
            await reassociate()
        return self._outcome_map.get(wid, RestartOutcome.RESTARTED)


def _patch_snapshot(monkeypatch, run_states):
    def _snap(route):
        return SimpleNamespace(
            run_state=run_states.get(route, route_runtime.RunState.IDLE_CLEARED)
        )

    monkeypatch.setattr(route_runtime, "snapshot", _snap)


class TestRunUpdateBucketing:
    @pytest.mark.asyncio
    async def test_restart_idle_defer_busy_skip_gone_and_no_session(self, monkeypatch):
        R = route_runtime.RunState
        bindings = [
            (1, 10, "@1"),  # idle → restart
            (1, 20, "@2"),  # running → deferred (pre-gate)
            (1, 30, "@3"),  # idle but window gone → skipped
            (1, 40, "@4"),  # idle, window present, no session id → skipped
        ]
        _patch_snapshot(
            monkeypatch,
            {
                (1, 10, "@1"): R.IDLE_CLEARED,
                (1, 20, "@2"): R.RUNNING,
                (1, 30, "@3"): R.IDLE_CLEARED,
                (1, 40, "@4"): R.IDLE_CLEARED,
            },
        )
        sm = FakeSessionMgr(
            bindings,
            window_states={
                "@1": _WS(session_id="s1"),
                "@4": _WS(session_id=""),  # no session id
            },
        )
        tmux = FakeTmux(windows={"@1", "@2", "@4"})  # @3 gone
        reports: list[str] = []

        async def report(t):
            reports.append(t)

        await updater.run_update(
            report=report,
            session_mgr=sm,
            tmux=tmux,
            monitor=None,
            claude_command="claude",
            md_settings="",
        )

        # Only @1 was restarted (busy @2 never touched tmux).
        assert [c[0] for c in tmux.restart_calls] == ["@1"]
        summary = reports[-1]
        assert "Restarted 1 idle: @1" in summary
        assert "Deferred 1 busy: @2" in summary
        assert "@3 (window gone)" in summary
        assert "@4 (no session id)" in summary

    @pytest.mark.asyncio
    async def test_outcome_bucketing(self, monkeypatch):
        bindings = [(1, i * 10, f"@{i}") for i in range(1, 6)]
        _patch_snapshot(monkeypatch, {})  # all IDLE_CLEARED
        sm = FakeSessionMgr(
            bindings,
            window_states={f"@{i}": _WS(session_id=f"s{i}") for i in range(1, 6)},
        )
        tmux = FakeTmux(
            windows={f"@{i}" for i in range(1, 6)},
            outcome_map={
                "@1": RestartOutcome.RESTARTED,
                "@2": RestartOutcome.SKIPPED_BUSY_LOCKED,  # deferred
                "@3": RestartOutcome.SKIPPED_NOT_IDLE,  # deferred
                "@4": RestartOutcome.SKIPPED_NO_EXIT,  # skipped (didn't exit)
                "@5": RestartOutcome.ERROR,  # skipped (restart error)
            },
        )
        reports: list[str] = []

        async def report(t):
            reports.append(t)

        await updater.run_update(
            report=report,
            session_mgr=sm,
            tmux=tmux,
            monitor=None,
            claude_command="claude",
            md_settings="",
        )
        summary = reports[-1]
        assert "Restarted 1 idle: @1" in summary
        assert "Deferred 2 busy: @2, @3" in summary
        assert "@4 (didn't exit)" in summary
        assert "@5 (restart error)" in summary

    @pytest.mark.asyncio
    async def test_single_flight_rejects_concurrent(self, monkeypatch):
        updater._update_running = True  # simulate an in-flight /update
        try:
            sm = FakeSessionMgr([(1, 10, "@1")])
            tmux = FakeTmux(windows={"@1"})
            reports: list[str] = []

            async def report(t):
                reports.append(t)

            await updater.run_update(
                report=report,
                session_mgr=sm,
                tmux=tmux,
                monitor=None,
                claude_command="claude",
                md_settings="",
            )
        finally:
            updater._update_running = False

        assert reports == ["⏳ An update is already in progress — try again shortly."]
        assert tmux.restart_calls == []  # never iterated bindings

    @pytest.mark.asyncio
    async def test_progressive_reports(self, monkeypatch):
        _patch_snapshot(monkeypatch, {})
        sm = FakeSessionMgr([])  # no bindings
        tmux = FakeTmux(windows=set())
        reports: list[str] = []

        async def report(t):
            reports.append(t)

        await updater.run_update(
            report=report,
            session_mgr=sm,
            tmux=tmux,
            monitor=None,
            claude_command="claude",
            md_settings="",
        )
        assert reports[0] == "🔄 Updating Claude Code CLI…"
        assert "Restarting idle sessions" in reports[1]
        assert "Restarted 0 idle" in reports[-1]


class TestRunCliUpdate:
    @pytest.mark.asyncio
    async def test_shlex_split_extracts_executable_only(self, monkeypatch):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"updated to 2.1.201", b""))
        proc.returncode = 0
        spawn = AsyncMock(return_value=proc)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        line = await updater._run_cli_update("claude --dangerously-skip-permissions")

        args = spawn.call_args.args
        assert args[0] == "claude"  # executable ONLY
        assert args[1] == "update"
        assert "--dangerously-skip-permissions" not in args  # flags dropped
        assert "✅" in line

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_non_fatal_warning(self, monkeypatch):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"network error"))
        proc.returncode = 3
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        line = await updater._run_cli_update("claude")
        assert "exited 3" in line
        assert "on-disk version" in line

    @pytest.mark.asyncio
    async def test_empty_command_is_handled(self, monkeypatch):
        line = await updater._run_cli_update("")
        assert "CLAUDE_COMMAND is empty" in line

    @pytest.mark.asyncio
    async def test_spawn_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("no claude")),
        )
        line = await updater._run_cli_update("claude")
        assert "Could not run" in line

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal_and_kills(self, monkeypatch):
        # Fix 3: a stalled `claude update` is bounded — on timeout the process is
        # killed + reaped and the flow continues onto the on-disk version.
        proc = MagicMock()
        # communicate() awaited by wait_for raises the timeout (no real hang).
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        line = await updater._run_cli_update("claude")
        assert "timed out" in line
        assert "on-disk version" in line
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


class TestRouteIsIdle:
    @pytest.mark.asyncio
    async def test_idle_when_cleared_and_pane_idle(self, monkeypatch):
        _patch_snapshot(monkeypatch, {})  # IDLE_CLEARED
        tmux = SimpleNamespace(capture_pane=AsyncMock(return_value=IDLE_PANE))
        assert await updater.route_is_idle((1, 2, "@1"), "@1", tmux) is True

    @pytest.mark.asyncio
    async def test_not_idle_when_running_short_circuits_pane(self, monkeypatch):
        _patch_snapshot(monkeypatch, {(1, 2, "@1"): route_runtime.RunState.RUNNING})
        cap = AsyncMock()
        tmux = SimpleNamespace(capture_pane=cap)
        assert await updater.route_is_idle((1, 2, "@1"), "@1", tmux) is False
        cap.assert_not_awaited()  # run-state gate short-circuits before the pane read

    @pytest.mark.asyncio
    async def test_not_idle_when_pane_busy(self, monkeypatch):
        _patch_snapshot(monkeypatch, {})  # IDLE_CLEARED but pane says running
        busy = (FIX / "status_busy_160x50_v2.1.198.txt").read_text(encoding="utf-8")
        tmux = SimpleNamespace(capture_pane=AsyncMock(return_value=busy))
        assert await updater.route_is_idle((1, 2, "@1"), "@1", tmux) is False


class TestReassociateRouting:
    @pytest.mark.asyncio
    async def test_overrides_diverged_ws_session_id(self):
        ws = _WS(session_id="OLD", cwd="/proj")
        saves: list[int] = []

        class SM:
            def get_window_state(self, wid):
                return ws

            def _save_state(self):
                saves.append(1)

            def _build_session_file_path(self, sid, cwd):
                return None  # skip monitor work

        await updater.reassociate_routing(SM(), None, "@1", "NEW")
        assert ws.session_id == "NEW"
        assert saves  # persisted

    @pytest.mark.asyncio
    async def test_untracked_registers_at_settled_eof(self, tmp_path):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects", state_file=tmp_path / "ms.json"
        )
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"type":"a"}\n{"type":"b"}\n', encoding="utf-8")
        fsize = jsonl.stat().st_size
        sid = "sess-new"

        class SM:
            def get_window_state(self, wid):
                return _WS(session_id=sid, cwd="/proj")

            def _save_state(self):
                pass

            def _build_session_file_path(self, s, cwd):
                return jsonl

        await updater.reassociate_routing(SM(), monitor, "@1", sid)
        tracked = monitor.state.get_session(sid)
        assert tracked is not None
        # Registered at EOF → pre-existing history is NOT re-delivered.
        assert tracked.last_byte_offset == fsize

    @pytest.mark.asyncio
    async def test_stale_offset_does_not_trigger_reset_to_zero_flood(self, tmp_path):
        # Offset-reset regression (Hermes P1-1): a tracked_offset > filesize must
        # NOT reset to 0 and re-deliver the whole transcript.
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects", state_file=tmp_path / "ms.json"
        )
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text('{"type":"a"}\n{"type":"b"}\n{"type":"c"}\n', encoding="utf-8")
        fsize = jsonl.stat().st_size
        sid = "sess-stale"
        # Pre-seed a STALE offset well past EOF (the truncating-replay hazard).
        monitor.state.update_session(
            TrackedSession(
                session_id=sid, file_path=str(jsonl), last_byte_offset=fsize + 5000
            )
        )

        class SM:
            def get_window_state(self, wid):
                return _WS(session_id=sid, cwd="/proj")

            def _save_state(self):
                pass

            def _build_session_file_path(self, s, cwd):
                return jsonl

        await updater.reassociate_routing(SM(), monitor, "@1", sid)
        tracked = monitor.state.get_session(sid)
        assert tracked is not None
        # Clamped to the settled EOF — offset <= filesize by construction.
        assert tracked.last_byte_offset == fsize
        assert tracked.last_byte_offset <= fsize
        # PROVE it end-to-end: the real reader does not reset-to-0 / flood.
        new = await monitor._read_new_lines(tracked, jsonl)
        assert new == []
        assert tracked.last_byte_offset == fsize  # unchanged; no reset-to-0


class TestRunUpdateEndToEnd:
    @pytest.mark.asyncio
    async def test_idle_route_restarts_and_reassociates(self, monkeypatch, tmp_path):
        _patch_snapshot(monkeypatch, {})  # IDLE_CLEARED
        jsonl = tmp_path / "s1.jsonl"
        jsonl.write_text('{"type":"a"}\n', encoding="utf-8")
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects", state_file=tmp_path / "ms.json"
        )
        sm = FakeSessionMgr(
            [(1, 10, "@1")],
            window_states={"@1": _WS(session_id="s1", cwd="/proj")},
            file_path=jsonl,
        )
        tmux = FakeTmux(windows={"@1"}, invoke_closures=True, pane=IDLE_PANE)
        reports: list[str] = []

        async def report(t):
            reports.append(t)

        await updater.run_update(
            report=report,
            session_mgr=sm,
            tmux=tmux,
            monitor=monitor,
            claude_command="mycli --flag",
            md_settings="/md.json",
        )
        # Fix 5: the injected claude_command is threaded through to the restart.
        assert tmux.restart_calls == [("@1", "s1", "/md.json", "mycli --flag")]
        assert "Restarted 1 idle: @1" in reports[-1]
        # Re-association registered the session at EOF.
        assert monitor.state.get_session("s1").last_byte_offset == jsonl.stat().st_size

    @pytest.mark.asyncio
    async def test_mid_loop_window_error_still_summarizes_remaining(self, monkeypatch):
        # Fix 4: a raise in ONE window's restart is bucketed as an error and the
        # loop CONTINUES — later windows are still processed and a summary lands.
        _patch_snapshot(monkeypatch, {})  # all IDLE_CLEARED
        bindings = [(1, 10, "@1"), (1, 20, "@2"), (1, 30, "@3")]
        sm = FakeSessionMgr(
            bindings,
            window_states={f"@{i}": _WS(session_id=f"s{i}") for i in (1, 2, 3)},
        )
        tmux = FakeTmux(windows={"@1", "@2", "@3"}, raise_for={"@2"})
        reports: list[str] = []

        async def report(t):
            reports.append(t)

        await updater.run_update(
            report=report,
            session_mgr=sm,
            tmux=tmux,
            monitor=None,
            claude_command="claude",
            md_settings="",
        )
        # All three windows were attempted (the raise didn't abort the loop).
        assert [c[0] for c in tmux.restart_calls] == ["@1", "@2", "@3"]
        summary = reports[-1]
        assert "Restarted 2 idle: @1, @3" in summary
        assert "@2 (restart error)" in summary
