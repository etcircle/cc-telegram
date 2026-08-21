"""GH #61 — a bound session must stay monitored when its transcript RELOCATES.

An ``EnterWorktree`` makes Claude Code MOVE a session's JSONL into a project
dir keyed on the new cwd. Two independent breaks followed:

  * ``scan_projects`` dropped the session silently — the new dir has no
    ``sessions-index.json``, so the glob branch derived the project from the
    FIRST ``cwd`` in the file (still the ORIGINAL path), which is no longer any
    pane's cwd → ``continue``. Nine hours dark, no error, no warning.
  * ``tracked.file_path`` is written once at registration and never re-pointed,
    and five consumers derive directories from it — so sidechain/teammate
    tailing stays dead even once discovery is fixed.

The fix: a session-id bypass of the cwd filter (with single-winner dedup) plus
a relocation sync invoked from TWO seams (the live ``check_for_updates`` block,
before the mtime shortcut, and the public ``reconcile_relocated_paths`` called
from ``bot.post_init`` before any consumer of ``tracked.file_path``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cctelegram import session_monitor as sm
from cctelegram.handlers.response_builder import TeammateIdle
from cctelegram.monitor_state import TrackedSession
from cctelegram.session_monitor import SessionMonitor, _TeammateRec

SID = "f30c7b09-relocated-session"
ORIG_CWD = "/Users/tester/dev-workspaces/di-copilot"
WORKTREE_CWD = "/Users/tester/dev-workspaces/di-copilot/.claude/worktrees/gh288"
ORIG_DIR = "-Users-tester-dev-workspaces-di-copilot"
WORKTREE_DIR = "-Users-tester-dev-workspaces-di-copilot--claude-worktrees-gh288"


@pytest.fixture
def monitor(tmp_path):
    mon = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    _patch_cwds(mon, {str(Path(WORKTREE_CWD))})
    return mon


def _patch_cwds(monitor: SessionMonitor, cwds: set[str]) -> None:
    """Stub the tmux cwd enumeration (``scan_projects`` shells out otherwise)."""

    async def _cwds() -> set[str]:
        return cwds

    monitor._get_active_cwds = _cwds  # type: ignore[method-assign]


def _project(tmp_path, name: str) -> Path:
    d = tmp_path / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _entry(text: str, *, cwd: str = ORIG_CWD, role: str = "assistant") -> dict:
    return {
        "type": role,
        "message": {"content": [{"type": "text", "text": text}], "role": role},
        "sessionId": SID,
        "cwd": cwd,
        "timestamp": "2026-08-20T22:40:00.000Z",
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries),
        encoding="utf-8",
    )


def _bytes_of(entries: list[dict]) -> int:
    return len("".join(json.dumps(e) + "\n" for e in entries).encode("utf-8"))


def _track(monitor: SessionMonitor, path: Path, offset: int, sid: str = SID) -> None:
    monitor.state.update_session(
        TrackedSession(session_id=sid, file_path=str(path), last_byte_offset=offset)
    )


def _track_sub(
    monitor: SessionMonitor, key: str, path: Path, offset: int, parent: str = SID
) -> None:
    monitor.state.update_session(
        TrackedSession(
            session_id=key,
            file_path=str(path),
            last_byte_offset=offset,
            parent_session_id=parent,
        )
    )


# ── 1. the incident repro ────────────────────────────────────────────────────


class TestIncidentRepro:
    """A bound session whose JSONL moved to a NEW, index-less project dir whose
    first-``cwd`` is no longer any pane's cwd is still discovered, and resumes
    at the STORED offset (not 0, not EOF)."""

    @pytest.mark.asyncio
    async def test_relocated_bound_session_is_discovered_and_resumes(
        self, monitor, tmp_path
    ):
        backlog_head = [_entry("read before the move")]
        backlog_tail = [_entry("Yes, it is working"), _entry("second unread line")]
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, backlog_head + backlog_tail)
        # The record still points at the ORIGINAL (now empty) project dir.
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, _bytes_of(backlog_head))

        found = await monitor.scan_projects({SID})
        assert [s.session_id for s in found] == [SID]
        assert found[0].file_path == new_path

        msgs = await monitor.check_for_updates({SID})

        texts = [m.text for m in msgs]
        assert "Yes, it is working" in texts
        assert "second unread line" in texts
        assert "read before the move" not in texts  # resumed at the STORED offset
        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.file_path == str(new_path)
        assert tracked.last_byte_offset == new_path.stat().st_size

    @pytest.mark.asyncio
    async def test_unbound_relocated_session_is_still_cwd_filtered(
        self, monitor, tmp_path
    ):
        """The bypass is keyed on the BOUND id set — nothing else changes."""
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("hello")])

        assert await monitor.scan_projects(set()) == []


# ── offset validation is a LINE-BOUNDARY proof ───────────────────────────────


class TestOffsetValidation:
    """``_check_offset_against_file`` must prove a JSONL LINE boundary — a bare
    ``{`` test passes on every nested object inside a line, which would accept a
    mid-line offset and resume the read mid-object."""

    def _file(self, tmp_path) -> tuple[Path, bytes]:
        line1 = json.dumps({"a": {"nested": 1}}).encode() + b"\n"
        line2 = json.dumps({"b": 2}).encode() + b"\n"
        p = tmp_path / "s.jsonl"
        p.write_bytes(line1 + line2)
        return p, line1

    def test_zero_eof_and_newline_preceded_brace_are_valid(self, tmp_path):
        p, line1 = self._file(tmp_path)
        size = p.stat().st_size
        assert sm._check_offset_against_file(p, 0).valid is True
        assert sm._check_offset_against_file(p, size).valid is True
        assert sm._check_offset_against_file(p, len(line1)).valid is True

    def test_a_nested_brace_mid_object_is_INVALID(self, tmp_path):
        p, _ = self._file(tmp_path)
        mid = p.read_bytes().index(b'{"nested"')
        assert p.read_bytes()[mid : mid + 1] == b"{"  # a bare-'{' test would pass
        assert sm._check_offset_against_file(p, mid).valid is False

    def test_a_negative_offset_is_INVALID(self, tmp_path):
        p, _ = self._file(tmp_path)
        assert sm._check_offset_against_file(p, -1).valid is False

    def test_past_eof_is_invalid_and_a_missing_file_does_not_exist(self, tmp_path):
        p, _ = self._file(tmp_path)
        past = sm._check_offset_against_file(p, p.stat().st_size + 1)
        assert (past.exists, past.valid) == (True, False)
        gone = sm._check_offset_against_file(tmp_path / "nope.jsonl", 0)
        assert (gone.exists, gone.valid) == (False, False)


# ── 2. single-winner dedup ───────────────────────────────────────────────────


class TestDuplicateCandidateDedup:
    """The SAME id in TWO project dirs must collapse to ONE SessionInfo — both
    callers would otherwise process both files against ONE shared offset/path."""

    @pytest.mark.asyncio
    async def test_newest_mtime_wins_with_a_warning(self, monitor, tmp_path, caplog):
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("stale leftover")])
        _write_jsonl(fresh, [_entry("live copy")])
        os.utime(stale, (1_000_000, 1_000_000))
        os.utime(fresh, (2_000_000, 2_000_000))

        with caplog.at_level("WARNING"):
            found = await monitor.scan_projects({SID})

        assert len(found) == 1
        assert found[0].file_path == fresh
        assert any(
            "project dirs" in r.getMessage() and str(fresh) in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_callers_never_see_both_copies(self, monitor, tmp_path):
        """``check_for_updates`` emits the backlog ONCE, not once per copy."""
        entries = [_entry("only once")]
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, entries)
        _write_jsonl(fresh, entries)
        os.utime(stale, (1_000_000, 1_000_000))
        os.utime(fresh, (2_000_000, 2_000_000))
        _track(monitor, stale, 0)

        msgs = await monitor.check_for_updates({SID})

        assert [m.text for m in msgs] == ["only once"]

    @pytest.mark.asyncio
    async def test_warning_is_rate_limited_on_candidate_set_change(
        self, monitor, tmp_path, caplog
    ):
        """A persistent leftover warns ONCE, then stays silent until the
        candidate SET changes (r2 P2-4 — the scan runs at 1 Hz)."""
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])

        def _dupe_warnings() -> int:
            return sum("project dirs" in r.getMessage() for r in caplog.records)

        with caplog.at_level("WARNING"):
            for _ in range(3):
                await monitor.scan_projects({SID})
            assert _dupe_warnings() == 1

            third = _project(tmp_path, "-Users-tester-elsewhere") / f"{SID}.jsonl"
            _write_jsonl(third, [_entry("c")])
            await monitor.scan_projects({SID})
            assert _dupe_warnings() == 2

            await monitor.scan_projects({SID})
            assert _dupe_warnings() == 2

    @pytest.mark.asyncio
    async def test_resolved_duplicate_warns_again_when_it_returns(
        self, monitor, tmp_path, caplog
    ):
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])

        with caplog.at_level("WARNING"):
            await monitor.scan_projects({SID})
            stale.unlink()
            await monitor.scan_projects({SID})
            _write_jsonl(stale, [_entry("a")])
            await monitor.scan_projects({SID})

        assert sum("project dirs" in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_a_vanished_duplicate_set_that_reappears_warns_again(
        self, monitor, tmp_path, caplog
    ):
        """Two → ZERO (the id disappears entirely) → the SAME two again: the
        cache entry must be dropped whenever the id has <= 1 candidates, not
        only when it has exactly one, or the identical reappearance is silent."""
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])

        with caplog.at_level("WARNING"):
            await monitor.scan_projects({SID})
            stale.unlink()
            fresh.unlink()  # the id vanishes from the scan ENTIRELY
            await monitor.scan_projects({SID})
            assert monitor._duplicate_scan_candidates == {}
            _write_jsonl(stale, [_entry("a")])
            _write_jsonl(fresh, [_entry("b")])
            await monitor.scan_projects({SID})

        assert sum("project dirs" in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_a_missing_projects_root_still_sweeps_the_warn_cache(
        self, monitor, tmp_path, caplog
    ):
        """EVERY return path out of ``scan_projects`` must sweep: an early
        return that skipped ``_dedupe_scan_candidates`` let the cache survive a
        scan that observed ZERO candidates, so an identical recurrence was
        silent."""
        root = tmp_path / "projects"
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])

        with caplog.at_level("WARNING"):
            await monitor.scan_projects({SID})
            root.rename(tmp_path / "projects-away")  # the root vanishes
            assert await monitor.scan_projects({SID}) == []
            assert monitor._duplicate_scan_candidates == {}
            (tmp_path / "projects-away").rename(root)  # …and comes back
            await monitor.scan_projects({SID})

        assert sum("project dirs" in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_the_empty_cwds_early_return_also_sweeps_the_warn_cache(
        self, monitor, tmp_path, caplog
    ):
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])

        with caplog.at_level("WARNING"):
            await monitor.scan_projects({SID})
            _patch_cwds(monitor, set())
            assert await monitor.scan_projects(set()) == []  # the other early return
            assert monitor._duplicate_scan_candidates == {}
            _patch_cwds(monitor, {str(Path(WORKTREE_CWD))})
            await monitor.scan_projects({SID})

        assert sum("project dirs" in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_parent_teardown_drops_the_rate_limit_caches(self, monitor, tmp_path):
        stale = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        fresh = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(stale, [_entry("a")])
        _write_jsonl(fresh, [_entry("b")])
        _track(monitor, fresh, 0)
        await monitor.scan_projects({SID})
        assert SID in monitor._duplicate_scan_candidates
        monitor._relocation_defer_warned.add(SID)

        # The startup stale-session sweep is one of the parent-teardown seams.
        await monitor._cleanup_all_stale_sessions()

        assert SID not in monitor._duplicate_scan_candidates
        assert SID not in monitor._relocation_defer_warned


# ── 3. stale index entries must not suppress the glob branch ─────────────────


class TestStaleIndexEntry:
    @pytest.mark.asyncio
    async def test_index_entry_pointing_at_a_gone_file_does_not_suppress(
        self, monitor, tmp_path
    ):
        """r1 P1-2: ``indexed_ids`` is claimed BEFORE the ``fullPath`` exists
        check, so a stale entry hid the valid file sitting in the SAME dir."""
        proj = _project(tmp_path, WORKTREE_DIR)
        real = proj / f"{SID}.jsonl"
        _write_jsonl(real, [_entry("live", cwd=WORKTREE_CWD)])
        (proj / "sessions-index.json").write_text(
            json.dumps(
                {
                    "originalPath": WORKTREE_CWD,
                    "entries": [
                        {
                            "sessionId": SID,
                            "fullPath": str(proj / "gone" / f"{SID}.jsonl"),
                            "projectPath": WORKTREE_CWD,
                        }
                    ],
                }
            )
        )

        # No id bypass: the discovery must come from the glob branch alone.
        found = await monitor.scan_projects(set())

        assert [s.file_path for s in found] == [real]

    @pytest.mark.asyncio
    async def test_live_index_entry_still_wins_the_branch(self, monitor, tmp_path):
        proj = _project(tmp_path, WORKTREE_DIR)
        real = proj / f"{SID}.jsonl"
        _write_jsonl(real, [_entry("live", cwd=WORKTREE_CWD)])
        (proj / "sessions-index.json").write_text(
            json.dumps(
                {
                    "originalPath": WORKTREE_CWD,
                    "entries": [
                        {
                            "sessionId": SID,
                            "fullPath": str(real),
                            "projectPath": WORKTREE_CWD,
                        }
                    ],
                }
            )
        )

        found = await monitor.scan_projects(set())

        assert [s.file_path for s in found] == [real]


# ── 4. the live sync seam runs BEFORE the mtime shortcut, and persists ───────


class TestLiveSyncPlacement:
    @pytest.mark.asyncio
    async def test_quiet_relocated_file_is_synced_and_persisted_on_disk(
        self, monitor, tmp_path
    ):
        """The unchanged-file shortcut ``continue``s past every later seam, so a
        QUIET relocated file only gets re-pointed if the sync runs first."""
        entries = [_entry("everything already read")]
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, entries)
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, new_path.stat().st_size)
        # Arm the mtime shortcut: cached mtime ahead + offset at EOF.
        monitor._file_mtimes[SID] = new_path.stat().st_mtime + 3600
        monitor.state.save()

        msgs = await monitor.check_for_updates({SID})

        assert msgs == []
        on_disk = json.loads((tmp_path / "monitor_state.json").read_text())
        assert on_disk["tracked_sessions"][SID]["file_path"] == str(new_path)
        assert on_disk["tracked_sessions"][SID]["last_byte_offset"] == (
            new_path.stat().st_size
        )


# ── 5. the startup seam ──────────────────────────────────────────────────────


class TestStartupSeam:
    @pytest.mark.asyncio
    async def test_post_init_reconciles_before_every_file_path_consumer(self):
        """Ordering pin: ``reconcile_relocated_paths`` must precede the
        pending-tools replay (which reads ``tracked.file_path`` directly) and
        ``monitor.start()`` (whose loop runs the startup BUSY reconciler)."""
        src = Path("src/cctelegram/bot.py").read_text()
        body = src[src.index("async def post_init(") :]
        reconcile_idx = body.index(
            "deferred_relocations = await monitor.reconcile_relocated_paths()"
        )
        replay_idx = body.index(
            "await _replay_pending_tools_at_startup(monitor, deferred_relocations)"
        )
        start_idx = body.index("monitor.start()")
        # The replay both FOLLOWS the reconcile and consumes ITS result — the
        # skip must key on the DEFERRAL, never on a path heuristic.
        assert reconcile_idx < replay_idx < start_idx

    @pytest.mark.asyncio
    async def test_reconcile_repoints_the_path_for_both_startup_consumers(
        self, monitor, tmp_path
    ):
        """Behavioral half: after ``reconcile_relocated_paths`` the pending-tools
        replay parses the RELOCATED JSONL and the BUSY reconciler discovers the
        NEW tree's ``subagents/workflows`` dirs."""
        from cctelegram import route_runtime

        proj = _project(tmp_path, WORKTREE_DIR)
        new_path = proj / f"{SID}.jsonl"
        wf_dir = proj / SID / "subagents" / "workflows" / "wf_run61"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "agent-aaa.jsonl").write_text("")
        launch = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": (
                            "Workflow launched in background. Task ID: task61\n"
                            "Run ID: wf_run61\n"
                            f"Transcript dir: {wf_dir}\n"
                        ),
                    }
                ]
            },
            "timestamp": "2026-08-20T22:00:00.000Z",
        }
        open_tool = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_open",
                        "name": "Bash",
                        "input": {},
                    }
                ],
            },
            "sessionId": SID,
            "timestamp": "2026-08-20T22:10:00.000Z",
        }
        _write_jsonl(new_path, [launch, open_tool])
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, 0)

        await monitor.reconcile_relocated_paths()

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.file_path == str(new_path)
        # Consumer 1: the pending-tools replay.
        assert route_runtime.parse_pending_tools_from_jsonl(tracked.file_path)
        # Consumer 2: the startup BUSY reconciler.
        await monitor._reconcile_workflow_brackets_on_startup({"@1": SID})
        brackets = monitor._open_workflow_brackets.get(SID, {})
        assert "task61" in brackets
        assert brackets["task61"].wf_dir == wf_dir

    @pytest.mark.asyncio
    async def test_a_startup_deferral_skips_that_session_in_the_replay(
        self, monitor, tmp_path, monkeypatch
    ):
        """A DEFERRED startup sync must skip that session's pending-tool replay.

        The old-path-missing case fails safe on its own (``OSError`` ⇒ ``{}``),
        but a READABLE STALE leftover does not: it can hold an unresolved
        AskUserQuestion ``tool_use`` whose ``tool_result`` exists only in the
        relocated copy. Seeding from it sets WAITING_ON_USER; the next
        successful sync then finds an already-VALID EOF offset, so the result is
        never replayed, and pane-idle reconciliation deliberately cannot clear a
        transcript-set WAITING_ON_USER — a false "🔔 Waiting on you" with no
        clearing path. Skipping leaves the route UNSEEDED, which is the
        documented post-restart degradation.

        Driven through the REAL seams: ``reconcile_relocated_paths`` →
        ``bot._replay_pending_tools_at_startup`` → ``check_for_updates``.
        """
        from cctelegram import bot as bot_module
        from cctelegram import route_runtime
        from cctelegram.route_runtime import RunState

        auq_use = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_auq",
                        "name": "AskUserQuestion",
                        "input": {},
                    }
                ],
            },
            "sessionId": SID,
            "timestamp": "2026-08-20T22:00:00.000Z",
        }
        auq_result = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_auq",
                        "content": "answered",
                    }
                ],
            },
            "sessionId": SID,
            "timestamp": "2026-08-20T22:00:05.000Z",
        }
        # The STALE leftover: readable, and its AUQ still looks unresolved.
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _write_jsonl(old_path, [auq_use])
        # The relocated copy carries the RESULT.
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, [auq_use, auq_result])
        _track(monitor, old_path, old_path.stat().st_size)
        # A second, NON-relocated session with its own open tool — the skip must
        # be PER-SESSION, not global.
        other_sid = "other-session-not-relocated"
        other_path = _project(tmp_path, WORKTREE_DIR) / f"{other_sid}.jsonl"
        _write_jsonl(
            other_path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_task",
                                "name": "Task",
                                "input": {},
                            }
                        ],
                    },
                    "sessionId": other_sid,
                    "timestamp": "2026-08-20T22:00:00.000Z",
                }
            ],
        )
        _track(monitor, other_path, 0, sid=other_sid)

        # The new file is transiently unstattable at STARTUP only.
        real_check = sm._check_offset_against_file
        transient = {"fail": True}

        def _flaky(path, offset):
            if transient["fail"] and Path(path) == new_path:
                return sm._OffsetCheck(exists=False, valid=False, size=0)
            return real_check(path, offset)

        monkeypatch.setattr(sm, "_check_offset_against_file", _flaky)

        route: route_runtime.Route = (1, 42, "@7")
        other_route: route_runtime.Route = (1, 43, "@8")
        route_runtime.reset_for_tests()
        try:

            async def fake_find(session_id: str):
                if session_id == SID:
                    return [(1, "@7", 42)]
                if session_id == other_sid:
                    return [(1, "@8", 43)]
                return []

            monkeypatch.setattr(
                bot_module.session_manager, "find_users_for_session", fake_find
            )

            deferred = await monitor.reconcile_relocated_paths()
            assert deferred == {SID}
            seeded = await bot_module._replay_pending_tools_at_startup(
                monitor, deferred
            )

            # The relocated session seeded NOTHING — no false WAITING.
            assert route not in route_runtime._state
            assert (
                route_runtime.snapshot(route).run_state is not RunState.WAITING_ON_USER
            )
            # …while the untouched session still replayed (per-session skip).
            assert seeded == 1
            assert (
                route_runtime.snapshot(other_route).run_state is RunState.RUNNING_TOOL
            )

            # The transient clears: the live seam syncs on the next tick. The
            # stored offset is a genuine line boundary in the byte-preserving
            # relocated copy, so it is KEPT (never EOF-jumped, never reset) and
            # the AUQ tool_result the stale copy lacked is delivered normally.
            transient["fail"] = False
            msgs = await monitor.check_for_updates({SID})

            tracked = monitor.state.get_session(SID)
            assert tracked is not None
            assert tracked.file_path == str(new_path)
            assert tracked.last_byte_offset == new_path.stat().st_size
            assert any("answered" in m.text for m in msgs)
            assert (
                route_runtime.snapshot(route).run_state is not RunState.WAITING_ON_USER
            )

            # …and fresh activity processes normally.
            with open(new_path, "a") as f:
                f.write(json.dumps(_entry("brand new line")) + "\n")
            msgs = await monitor.check_for_updates({SID})

            assert [m.text for m in msgs] == ["brand new line"]
        finally:
            route_runtime.reset_for_tests()


# ── 6. open Workflow brackets are re-pointed ─────────────────────────────────


class TestBracketRepoint:
    @pytest.mark.asyncio
    async def test_bracket_wf_dir_follows_the_relocation(self, monitor, tmp_path):
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("x")])
        old_wf = proj_old / SID / "subagents" / "workflows" / "wf_run61"
        new_wf = proj_new / SID / "subagents" / "workflows" / "wf_run61"
        new_wf.mkdir(parents=True, exist_ok=True)
        (new_wf / "agent-aaa.jsonl").write_text("{}\n")
        _track(monitor, old_path, 0)
        monitor._open_workflow_brackets[SID] = {
            "task61": sm._WorkflowBracket(
                wf_dir=old_wf, last_seen_mtime=0.0, launch_wall=0.0
            )
        }

        await monitor._sync_relocated_session(monitor.state.get_session(SID), new_path)

        assert monitor._open_workflow_brackets[SID]["task61"].wf_dir == new_wf
        # And the heartbeat keeps firing from the NEW tree.
        monitor._emit_workflow_bracket_heartbeats(SID)
        activity = monitor.pop_sidechain_activity()
        assert "wf-task:task61" in activity[SID].bracket_heartbeats

    @pytest.mark.asyncio
    async def test_missing_recomputed_dir_sets_wf_dir_to_none(self, monitor, tmp_path):
        """A recomputed dir that does not exist must NOT keep walking the OLD
        tree (which may still exist as a stale leftover) — the bracket becomes
        the documented never-heartbeats shape and ages out."""
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("x")])
        old_wf = proj_old / SID / "subagents" / "workflows" / "wf_run61"
        old_wf.mkdir(parents=True, exist_ok=True)
        (old_wf / "agent-aaa.jsonl").write_text("{}\n")  # the stale tree survives
        _track(monitor, old_path, 0)
        monitor._open_workflow_brackets[SID] = {
            "task61": sm._WorkflowBracket(
                wf_dir=old_wf, last_seen_mtime=0.0, launch_wall=0.0
            )
        }

        await monitor._sync_relocated_session(monitor.state.get_session(SID), new_path)

        assert monitor._open_workflow_brackets[SID]["task61"].wf_dir is None
        monitor._emit_workflow_bracket_heartbeats(SID)
        assert monitor.pop_sidechain_activity() == {}


# ── 6b. empty active_cwds + the cost bound ───────────────────────────────────


class TestCwdEnumerationEdges:
    @pytest.mark.asyncio
    async def test_empty_active_cwds_still_discovers_bound_sessions(
        self, monitor, tmp_path
    ):
        """r2 P2-3: a transient empty tmux enumeration must not blank the bound
        sessions; unbound candidates are still dropped."""
        _patch_cwds(monitor, set())
        bound = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        other = _project(tmp_path, ORIG_DIR) / "some-other-session.jsonl"
        _write_jsonl(bound, [_entry("bound")])
        _write_jsonl(other, [_entry("unbound")])

        found = await monitor.scan_projects({SID})

        assert [s.session_id for s in found] == [SID]

    @pytest.mark.asyncio
    async def test_empty_cwds_and_no_bound_ids_returns_immediately(
        self, monitor, tmp_path
    ):
        _patch_cwds(monitor, set())
        _write_jsonl(
            _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl", [_entry("bound")]
        )

        assert await monitor.scan_projects(set()) == []


class TestCostBoundPreserved:
    @pytest.mark.asyncio
    async def test_unbound_historical_session_in_a_foreign_dir_is_dropped(
        self, monitor, tmp_path
    ):
        foreign = _project(tmp_path, "-Users-tester-some-old-project")
        _write_jsonl(
            foreign / "historical-session.jsonl",
            [_entry("ancient", cwd="/Users/tester/some/old/project")],
        )

        assert await monitor.scan_projects({SID}) == []


# ── 7. sidechain records: the uniform rule + the normal replay path ──────────


def _sc_entry(text: str, *, stop_reason: str | None = None, ts: str) -> dict:
    return {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
        },
        "sessionId": "sc",
        "timestamp": ts,
    }


def _sc_tool_use(name: str, tool_id: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {"run_in_background": True},
                }
            ],
            "stop_reason": "tool_use",
        },
        "sessionId": "sc",
        "timestamp": ts,
    }


def _sc_bg_bash_result(tool_id: str, task_id: str, ts: str) -> dict:
    """A tool_result carrying the entry-level ``backgroundTaskId`` — the GH #59
    background-Bash launch shape, admitted only for ``tool_name in (None, Bash)``."""
    return {
        "type": "user",
        "isSidechain": True,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Command running in background with ID: {task_id}.",
                }
            ],
        },
        "sessionId": "sc",
        "timestamp": ts,
        "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": task_id},
    }


class TestSidechainRelocation:
    """The ONE uniform rule: VALIDATED ⇒ offset AND carry both kept; NOT
    VALIDATED (invalid offset OR missing file) ⇒ offset → 0 AND carry →
    cleared, together. The record itself is NEVER deleted."""

    def _relocate(self, tmp_path, sub_entries: list[dict]) -> tuple[Path, Path, Path]:
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        _write_jsonl(new_sc, sub_entries)
        return proj_old / f"{SID}.jsonl", new_path, (old_sc, new_sc)  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_validated_record_keeps_offset_and_carry_and_replays_as_digest(
        self, monitor, tmp_path
    ):
        head = [_sc_entry("already seen", ts="2026-08-20T22:00:00.000Z")]
        tail = [
            _sc_entry("backlog line", ts="2026-08-20T22:30:00.000Z"),
            _sc_entry("final", stop_reason="end_turn", ts="2026-08-20T22:31:00.000Z"),
        ]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, head + tail)
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, _bytes_of(head))
        monitor._pending_tools[key] = {"toolu_x": object()}

        await monitor.check_for_updates({SID})

        rec = monitor.state.get_session(key)
        assert rec is not None
        assert rec.file_path == str(new_sc)  # re-rooted
        assert rec.last_byte_offset == _bytes_of(head)  # offset KEPT
        assert key in monitor._pending_tools  # carry KEPT (lockstep)

        msgs = await monitor.check_sidechain_updates({SID})

        # The backlog replays through the per-key ``↳`` digest card — every
        # emitted block carries ``subagent_key``, so nothing lands as a
        # per-block parent topic message (the falsified-premise pin).
        assert msgs, "the behind-offset backlog must replay"
        assert all(m.subagent_key == key for m in msgs)
        assert any("backlog line" in m.text for m in msgs)
        activity = monitor.pop_sidechain_activity()[SID]
        assert activity.ticks["a61"].saw_end_of_turn is True

    @pytest.mark.asyncio
    async def test_at_eof_record_is_untouched_apart_from_its_path(
        self, monitor, tmp_path
    ):
        entries = [_sc_entry("done", ts="2026-08-20T22:00:00.000Z")]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, entries)
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, new_sc.stat().st_size)
        monitor._pending_tools[key] = {"toolu_x": object()}

        await monitor.check_for_updates({SID})

        rec = monitor.state.get_session(key)
        assert rec is not None
        assert rec.file_path == str(new_sc)
        assert rec.last_byte_offset == new_sc.stat().st_size
        assert key in monitor._pending_tools

    @pytest.mark.asyncio
    async def test_severed_stem_stays_dark_across_the_relocation(
        self, monitor, tmp_path
    ):
        entries = [
            _sc_entry("a", ts="2026-08-20T22:00:00.000Z"),
            _sc_entry("b", stop_reason="end_turn", ts="2026-08-20T22:30:00.000Z"),
        ]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, entries)
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, 0)
        monitor._severed_teammate_stems[SID] = {key}

        await monitor.check_for_updates({SID})
        await monitor.check_sidechain_updates({SID})

        # Display still runs; run-state stays severed (monitor-side, immune to
        # any runtime tombstone reset).
        assert monitor.pop_sidechain_activity().get(SID) is None

    @pytest.mark.asyncio
    async def test_invalid_offset_resets_to_zero_and_clears_the_carry(
        self, monitor, tmp_path
    ):
        entries = [_sc_entry("only line", ts="2026-08-20T22:00:00.000Z")]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, entries)
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, new_sc.stat().st_size + 10_000)
        monitor._pending_tools[key] = {"toolu_x": object()}

        await monitor.check_for_updates({SID})

        rec = monitor.state.get_session(key)
        assert rec is not None
        assert rec.last_byte_offset == 0  # NOT EOF — sidechain replay is bounded
        assert key not in monitor._pending_tools  # cleared TOGETHER with the offset

    @pytest.mark.asyncio
    async def test_missing_file_keeps_the_record_and_cannot_mint_a_false_bash_key(
        self, monitor, tmp_path
    ):
        """r5 P1-2: a byte-preserving file that is MISSING at sync time and
        appears later must not resume at the preserved offset — that would
        parse a KNOWN non-Bash tool_result as ``tool_name=None``, which the
        GH #59 sidechain lane admits → a false background-Bash key."""
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        head = [_sc_tool_use("Read", "toolu_r", "2026-08-20T22:00:00.000Z")]
        tail = [_sc_bg_bash_result("toolu_r", "bgfalsekey", "2026-08-20T22:01:00.000Z")]
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, _bytes_of(head))  # file NOT on disk yet
        monitor._pending_tools[key] = {"toolu_r": object()}

        await monitor.check_for_updates({SID})

        rec = monitor.state.get_session(key)
        assert rec is not None, "the record is KEPT (the park stem lookup needs it)"
        assert rec.file_path == str(new_sc)
        assert rec.last_byte_offset == 0
        assert key not in monitor._pending_tools

        # The file appears later, byte-identically.
        _write_jsonl(new_sc, head + tail)
        await monitor.check_sidechain_updates({SID})

        activity = monitor.pop_sidechain_activity().get(SID)
        assert activity is not None
        assert "bgfalsekey" not in activity.launched

    @pytest.mark.asyncio
    async def test_teammate_park_stem_lookup_still_resolves_the_kept_record(
        self, monitor, tmp_path
    ):
        entries = [_sc_entry("x", ts="2026-08-20T22:00:00.000Z")]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, entries)
        new_sc.unlink()
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-avis2-backend-7041d9b743d26f2e"
        _track_sub(monitor, key, old_sc.with_name(f"{key.split(':')[-1]}.jsonl"), 500)

        await monitor.check_for_updates({SID})

        assert monitor.state.get_session(key) is not None
        monitor._record_teammate_park(
            SID,
            TeammateIdle(name="vis2-backend", park_ts=1.0, park_ts_unparseable=False),
        )
        activity = monitor.pop_sidechain_activity()[SID]
        # The park resolved THROUGH the kept record's tracked stem (deleting it
        # would leave the teammate's only close signal with nothing to close).
        assert "avis2-backend-7041d9b743d26f2e" in activity.teammate_parks

    @pytest.mark.asyncio
    async def test_characterization_reset_to_zero_tombstones_a_live_key(
        self, monitor, tmp_path, monkeypatch
    ):
        """DISCLOSED, ADJUDICATED fail-dark residual, driven through the REAL
        ``bot.apply_sidechain_activity`` fan-out onto a seeded route.

        A reset-to-0 replay folds a HISTORICAL end_turn into the tick. The
        parent's resume signal sits behind the parent's VALID offset and is
        therefore NOT replayed, so the runtime record carries no
        ``resumed_event_ts`` and the SIDECHAIN done gate fails CLOSED — the key
        is tombstoned even though the SAME tick replayed strictly NEWER
        activity for it (``BgDoneSource.SIDECHAIN`` has the resume gate but,
        unlike ``TEAMMATE``, NO stale-vs-activity gate).

        Chosen deliberately over EOF, which loses the CLOSE instead and strands
        typing ON for the 2 h TTL — this repo's historically recurring bug
        class ("false dark is annoying, false typing after completion is the
        historical bug class here"). **This test goes RED if a stale-vs-activity
        gate is ever added to the SIDECHAIN done source** — which is the point:
        such a change flips the adjudicated direction and must be a decision,
        not a drive-by.
        """
        from cctelegram import bot as bot_module
        from cctelegram import route_runtime
        from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

        entries = [
            _sc_entry(
                "leg 1 finished",
                stop_reason="end_turn",
                ts="2026-08-20T20:00:00.000Z",  # HISTORICAL
            ),
            _sc_entry("leg 2 still working", ts="2026-08-20T22:30:00.000Z"),  # NEWER
        ]
        old_path, new_path, (old_sc, new_sc) = self._relocate(tmp_path, entries)
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, new_sc.stat().st_size + 10_000)

        await monitor.check_for_updates({SID})
        await monitor.check_sidechain_updates({SID})
        activity = monitor.pop_sidechain_activity()
        tick = activity[SID].ticks["a61"]
        assert tick.saw_end_of_turn is True  # the historical end_turn is replayed

        route: route_runtime.Route = (1, 42, "@7")
        route_runtime.reset_for_tests()
        try:
            # A route that is idle on its own transcript, with a LIVE background
            # key (the agent is genuinely still working).
            await route_runtime.ingest_transcript_event(
                route,
                TranscriptLifecycleEvent(
                    role="assistant",
                    block_type="text",
                    tool_use_id=None,
                    tool_name=None,
                    stop_reason="end_turn",
                    timestamp=100.0,
                ),
            )
            await route_runtime.seed_idle_and_mark_background_agent_launched(
                route, "a61"
            )
            assert route_runtime.snapshot(route).typing_eligible is True

            async def fake_find(session_id: str):
                return [(1, "@7", 42)]

            monkeypatch.setattr(
                bot_module.session_manager, "find_users_for_session", fake_find
            )
            await bot_module.apply_sidechain_activity(activity)

            snap = route_runtime.snapshot(route)
            assert "a61" in route_runtime._state[route].background_agents_done
            assert snap.background_agents == ()
            assert snap.typing_eligible is False
            assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
        finally:
            route_runtime.reset_for_tests()

    @pytest.mark.asyncio
    async def test_ambiguous_teammate_name_stays_dark_through_the_replay(
        self, monitor, tmp_path
    ):
        """Item 3: a sticky-AMBIGUOUS registered name feeds NO run-state — and a
        relocation replay must not become a back door for it. Display is
        unaffected (the Fix-5 discipline)."""
        stem = "avis2-backend-7041d9b743d26f2e"
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / f"agent-{stem}.jsonl"
        new_sc = proj_new / SID / "subagents" / f"agent-{stem}.jsonl"
        _write_jsonl(
            new_sc,
            [
                _sc_entry("a", ts="2026-08-20T22:00:00.000Z"),
                _sc_entry("b", stop_reason="end_turn", ts="2026-08-20T22:30:00.000Z"),
            ],
        )
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-{stem}"
        _track_sub(monitor, key, old_sc, 0)
        # A REGISTERED name that arbitration left sticky-ambiguous: no
        # current_key, so no candidate is the ONE feeder.
        monitor._teammate_registry[SID] = {
            "vis2-backend": _TeammateRec(
                name="vis2-backend",
                teammate_id=None,
                spawn_generation=1,
                spawned_ts=0.0,
                current_key=None,
                ambiguous=True,
            )
        }

        await monitor.check_for_updates({SID})
        msgs = await monitor.check_sidechain_updates({SID})

        assert msgs, "display still runs"
        assert monitor.pop_sidechain_activity().get(SID) is None

    @pytest.mark.asyncio
    async def test_a_replayed_stale_park_cannot_tombstone_past_newer_activity(
        self, monitor, tmp_path, monkeypatch
    ):
        """The shipped GH #46 r2 TEAMMATE stale-vs-activity gate still arbitrates
        after a relocation replay: a REDELIVERED park strictly OLDER than the
        key's own newer activity keeps the teammate LIVE."""
        from cctelegram import bot as bot_module
        from cctelegram import route_runtime
        from cctelegram.route_runtime import TranscriptLifecycleEvent

        agent_key = "avis2-backend-7041d9b743d26f2e"
        # One replayed tick: the key's OWN newer activity plus a redelivered
        # park stamped BEFORE it (the fan-out applies activity, then parks).
        activity = {
            SID: sm.ParentSidechainActivity(
                ticks={agent_key: sm.SidechainTick(max_event_ts=500.0)},
                teammate_parks={agent_key: (200.0, False)},
            )
        }
        route: route_runtime.Route = (1, 42, "@7")
        route_runtime.reset_for_tests()
        try:
            await route_runtime.ingest_transcript_event(
                route,
                TranscriptLifecycleEvent(
                    role="assistant",
                    block_type="text",
                    tool_use_id=None,
                    tool_name=None,
                    stop_reason="end_turn",
                    timestamp=100.0,
                ),
            )
            await route_runtime.seed_idle_and_mark_background_agent_launched(
                route, agent_key
            )

            async def fake_find(session_id: str):
                return [(1, "@7", 42)]

            monkeypatch.setattr(
                bot_module.session_manager, "find_users_for_session", fake_find
            )
            await bot_module.apply_sidechain_activity(activity)

            snap = route_runtime.snapshot(route)
            assert agent_key in snap.background_agents  # NOT tombstoned
            assert snap.typing_eligible is True
        finally:
            route_runtime.reset_for_tests()


# ── 7c. parser-carry lockstep at BOTH granularities ──────────────────────────


class TestParserCarryLockstep:
    @pytest.mark.asyncio
    async def test_validated_relocation_preserves_a_live_carry_end_to_end(
        self, monitor, tmp_path
    ):
        """A tool_use whose tool_result arrives AFTER the move keeps its
        recovered tool name — a cleared carry would surface the KNOWN non-Bash
        result as ``tool_name=None`` and mint a GH #59 background-Bash key."""
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        _write_jsonl(old_sc, [])
        _track(monitor, old_path, 0)

        # Register the sidechain, then read the tool_use so the carry is live.
        await monitor.check_sidechain_updates({SID})
        _write_jsonl(
            old_sc, [_sc_tool_use("Read", "toolu_r", "2026-08-20T22:00:00.000Z")]
        )
        await monitor.check_sidechain_updates({SID})
        key = f"sub:{SID}:agent-a61"
        assert key in monitor._pending_tools

        # Relocate the whole tree, then land the result on the NEW file.
        new_sc.parent.mkdir(parents=True, exist_ok=True)
        old_sc.replace(new_sc)
        await monitor.check_for_updates({SID})
        assert key in monitor._pending_tools

        with open(new_sc, "a") as f:
            f.write(
                json.dumps(
                    _sc_bg_bash_result(
                        "toolu_r", "bgfalsekey", "2026-08-20T22:01:00.000Z"
                    )
                )
                + "\n"
            )
        await monitor.check_sidechain_updates({SID})

        activity = monitor.pop_sidechain_activity().get(SID)
        assert activity is not None
        assert "bgfalsekey" not in activity.launched

    @pytest.mark.asyncio
    async def test_parent_eof_jump_clears_only_the_parent_carry(
        self, monitor, tmp_path
    ):
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        _write_jsonl(new_sc, [_sc_entry("x", ts="2026-08-20T22:00:00.000Z")])
        _track(monitor, old_path, new_path.stat().st_size + 10_000)  # invalid
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, 0)  # valid
        monitor._pending_tools[SID] = {"toolu_p": object()}
        monitor._pending_tools[key] = {"toolu_s": object()}

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        await monitor._sync_relocated_session(tracked, new_path)

        assert tracked.last_byte_offset == new_path.stat().st_size  # EOF, never 0
        assert SID not in monitor._pending_tools  # moved ⇒ cleared
        assert key in monitor._pending_tools  # validated ⇒ kept


# ── 7d. staged commit + best-effort durability ───────────────────────────────


class TestStagedCommit:
    @pytest.mark.asyncio
    async def test_a_raise_before_the_commit_leaves_no_partial_mutation(
        self, monitor, tmp_path, monkeypatch
    ):
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        _write_jsonl(new_sc, [_sc_entry("x", ts="2026-08-20T22:00:00.000Z")])
        _track(monitor, old_path, 0)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, 4242)
        monitor._pending_tools[key] = {"toolu_s": object()}
        monitor._file_mtimes[key] = 99.0
        old_wf = proj_old / SID / "subagents" / "workflows" / "wf_run61"
        monitor._open_workflow_brackets[SID] = {
            "task61": sm._WorkflowBracket(
                wf_dir=old_wf, last_seen_mtime=0.0, launch_wall=0.0
            )
        }

        calls = {"n": 0}
        real_check = sm._check_offset_against_file

        def _boom(path, offset):
            calls["n"] += 1
            if calls["n"] > 1:  # past the parent check, inside the staging
                raise RuntimeError("injected")
            return real_check(path, offset)

        monkeypatch.setattr(sm, "_check_offset_against_file", _boom)

        with pytest.raises(RuntimeError):
            await monitor._sync_relocated_session(
                monitor.state.get_session(SID), new_path
            )

        # Nothing observable moved — and the path DIFFERENCE (the retry
        # trigger) survives, so the next tick re-runs the whole sync.
        tracked = monitor.state.get_session(SID)
        assert tracked is not None and tracked.file_path == str(old_path)
        rec = monitor.state.get_session(key)
        assert rec is not None
        assert rec.file_path == str(old_sc)
        assert rec.last_byte_offset == 4242
        assert key in monitor._pending_tools
        assert monitor._file_mtimes[key] == 99.0
        assert monitor._open_workflow_brackets[SID]["task61"].wf_dir == old_wf

        monkeypatch.setattr(sm, "_check_offset_against_file", real_check)
        await monitor.check_for_updates({SID})

        tracked = monitor.state.get_session(SID)
        assert tracked is not None and tracked.file_path == str(new_path)
        rec = monitor.state.get_session(key)
        assert rec is not None and rec.file_path == str(new_sc)

    @pytest.mark.asyncio
    async def test_an_unstattable_new_parent_file_DEFERS_the_whole_sync(
        self, monitor, tmp_path, caplog
    ):
        """An unstattable new parent file has NO knowable EOF. Committing the
        path while keeping an unvalidated offset would CONSUME the
        path-difference retry trigger and leave a poisoned offset: once the file
        became readable, an offset past its EOF would fall into the ORDINARY
        truncation path, reset to 0 and replay the whole parent — the flood the
        EOF-never-0 rule forbids. So the sync DEFERS: nothing is committed, one
        rate-limited WARNING, and the surviving difference retries."""
        proj_old = _project(tmp_path, ORIG_DIR)
        proj_new = _project(tmp_path, WORKTREE_DIR)
        old_path = proj_old / f"{SID}.jsonl"
        new_path = proj_new / f"{SID}.jsonl"  # deliberately NOT created yet
        old_sc = proj_old / SID / "subagents" / "agent-a61.jsonl"
        new_sc = proj_new / SID / "subagents" / "agent-a61.jsonl"
        _write_jsonl(new_sc, [_sc_entry("x", ts="2026-08-20T22:00:00.000Z")])
        _track(monitor, old_path, 4_000_000)
        key = f"sub:{SID}:agent-a61"
        _track_sub(monitor, key, old_sc, 4242)
        monitor._pending_tools[SID] = {"toolu_p": object()}
        monitor._pending_tools[key] = {"toolu_s": object()}
        monitor._file_mtimes[SID] = 7.0
        old_wf = proj_old / SID / "subagents" / "workflows" / "wf_run61"
        monitor._open_workflow_brackets[SID] = {
            "task61": sm._WorkflowBracket(
                wf_dir=old_wf, last_seen_mtime=0.0, launch_wall=0.0
            )
        }

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        with caplog.at_level("WARNING"):
            for _ in range(3):  # the live seam retries at 1 Hz
                await monitor._sync_relocated_session(tracked, new_path)

        # ZERO mutations, and the path is NOT synced.
        assert tracked.file_path == str(old_path)
        assert tracked.last_byte_offset == 4_000_000
        rec = monitor.state.get_session(key)
        assert rec is not None
        assert rec.file_path == str(old_sc)
        assert rec.last_byte_offset == 4242
        assert monitor._pending_tools.keys() == {SID, key}
        assert monitor._file_mtimes[SID] == 7.0
        assert monitor._open_workflow_brackets[SID]["task61"].wf_dir == old_wf
        assert sum("deferring" in r.getMessage() for r in caplog.records) == 1

        # Once the file is readable the retry applies the FULL sync.
        _write_jsonl(new_path, [_entry("parent")])
        await monitor._sync_relocated_session(tracked, new_path)

        assert tracked.file_path == str(new_path)
        assert tracked.last_byte_offset == new_path.stat().st_size  # EOF, never 0
        assert SID not in monitor._pending_tools  # moved ⇒ carry cleared
        assert monitor.state.get_session(key).file_path == str(new_sc)
        assert SID not in monitor._relocation_defer_warned

    @pytest.mark.asyncio
    async def test_a_deferred_sync_skips_the_ENTIRE_per_session_block_that_tick(
        self, monitor, tmp_path, caplog, monkeypatch
    ):
        """Through the REAL ``check_for_updates`` — the isolation of the unit
        test above is exactly what hid this.

        A deferral must ``continue`` past the whole per-session block: the
        record still points at the OLD path with an UNVALIDATED offset, so
        falling through hands that offset to ``_read_new_lines`` against the
        NEW file. A transient stat failure inside the sync's validation
        followed by a succeeding stat in the normal path is enough — the
        offset lands past EOF, the ORDINARY truncation path resets it to 0 and
        the parent replays in full, while the WARNING claimed "deferring"."""
        entries = [_entry("old one"), _entry("old two"), _entry("old three")]
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, entries)
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        poisoned = new_path.stat().st_size + 10_000
        _track(monitor, old_path, poisoned)

        # A TRANSIENT stat failure inside the sync's validation only; every
        # later stat (the normal path's) succeeds.
        real_check = sm._check_offset_against_file
        transient = {"fail": True}

        def _flaky(path, offset):
            if transient["fail"] and Path(path) == new_path:
                return sm._OffsetCheck(exists=False, valid=False, size=0)
            return real_check(path, offset)

        monkeypatch.setattr(sm, "_check_offset_against_file", _flaky)

        with caplog.at_level("WARNING"):
            msgs = await monitor.check_for_updates({SID})

        assert msgs == [], "a deferred session must not be read at all"
        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.file_path == str(old_path)  # path difference intact
        assert tracked.last_byte_offset == poisoned  # NOT reset, NOT EOF
        assert SID not in monitor._file_mtimes  # not even stat-cached
        assert sum("deferring" in r.getMessage() for r in caplog.records) == 1

        # The next tick: the file is readable again → sync + normal processing.
        transient["fail"] = False
        msgs = await monitor.check_for_updates({SID})

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.file_path == str(new_path)
        assert tracked.last_byte_offset == new_path.stat().st_size  # EOF, never 0
        assert msgs == []

        with open(new_path, "a") as f:
            f.write(json.dumps(_entry("brand new line")) + "\n")
        msgs = await monitor.check_for_updates({SID})

        assert [m.text for m in msgs] == ["brand new line"]

    def test_the_commit_assigns_the_parent_file_path_last(self):
        """Source pin (r5 P2-2): the parent ``file_path`` is the sync's retry
        trigger, so it must be assigned AFTER every other mutation."""
        src = Path("src/cctelegram/session_monitor.py").read_text()
        body = src[src.index("async def _sync_relocated_session(") :]
        body = body[: body.index("\n    async def _get_active_cwds(")]
        assign_idx = body.index("tracked.file_path = str(new_path)")
        assert body.index("rec.file_path = path_str") < assign_idx
        assert body.index("bracket.wf_dir = wf_dir") < assign_idx
        assert body.index("tracked.last_byte_offset = planned_parent_offset") < (
            assign_idx
        )

    @pytest.mark.asyncio
    async def test_a_failed_save_logs_error_and_the_next_process_reconverges(
        self, monitor, tmp_path, monkeypatch, caplog
    ):
        """Durability is best-effort: ``MonitorState.save`` swallows ``OSError``.
        In-memory state is correct and the still-different PERSISTED path makes
        the next process re-run the sync."""
        from cctelegram import utils

        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, [_entry("parent")])
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, 0)
        monitor.state.save()

        def _fail(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(utils, "atomic_write_json", _fail)

        with caplog.at_level("ERROR"):
            await monitor.check_for_updates({SID})

        assert any("Failed to save state" in r.getMessage() for r in caplog.records)
        tracked = monitor.state.get_session(SID)
        assert tracked is not None and tracked.file_path == str(new_path)
        on_disk = json.loads((tmp_path / "monitor_state.json").read_text())
        assert on_disk["tracked_sessions"][SID]["file_path"] == str(old_path)


# ── 8. the move-BACK direction ───────────────────────────────────────────────


class TestMoveBack:
    @pytest.mark.asyncio
    async def test_exit_worktree_relocates_back_and_the_sync_fires_in_reverse(
        self, monitor, tmp_path
    ):
        entries = [_entry("one"), _entry("two")]
        worktree_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        orig_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _write_jsonl(worktree_path, entries)
        _track(monitor, orig_path, _bytes_of(entries[:1]))

        await monitor.check_for_updates({SID})
        tracked = monitor.state.get_session(SID)
        assert tracked is not None and tracked.file_path == str(worktree_path)
        offset_after_move = tracked.last_byte_offset

        # ExitWorktree moves it BACK, and the pane's cwd follows.
        worktree_path.replace(orig_path)
        _patch_cwds(monitor, {str(Path(ORIG_CWD))})

        msgs = await monitor.check_for_updates({SID})

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.file_path == str(orig_path)
        assert tracked.last_byte_offset == offset_after_move  # valid, never reset
        assert msgs == []


# ── 9. EOF-never-0 (relocation) vs the untouched truncation semantics ────────


class TestOffsetInvalidation:
    @pytest.mark.asyncio
    async def test_relocation_invalid_parent_offset_goes_to_eof_never_zero(
        self, monitor, tmp_path
    ):
        entries = [_entry("a"), _entry("b")]
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, entries)
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, new_path.stat().st_size + 999_999)

        msgs = await monitor.check_for_updates({SID})

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.last_byte_offset == new_path.stat().st_size
        assert msgs == [], "a parent reset-to-0 would re-send the whole transcript"

    @pytest.mark.asyncio
    async def test_relocation_mid_line_parent_offset_goes_to_eof(
        self, monitor, tmp_path
    ):
        entries = [_entry("a"), _entry("b")]
        new_path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(new_path, entries)
        old_path = _project(tmp_path, ORIG_DIR) / f"{SID}.jsonl"
        _track(monitor, old_path, _bytes_of(entries[:1]) // 2)  # mid-line

        await monitor.check_for_updates({SID})

        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.last_byte_offset == new_path.stat().st_size

    @pytest.mark.asyncio
    async def test_non_relocation_truncation_still_resets_to_zero(
        self, monitor, tmp_path
    ):
        """The ``/clear`` shape: SAME path, file SHRINKS → the shipped
        reset-to-0 in ``_read_new_lines`` is byte-untouched by the
        relocation-scoped EOF rule."""
        path = _project(tmp_path, WORKTREE_DIR) / f"{SID}.jsonl"
        _write_jsonl(path, [_entry("old one"), _entry("old two"), _entry("old three")])
        _track(monitor, path, path.stat().st_size)
        _write_jsonl(path, [_entry("fresh after clear")])

        msgs = await monitor.check_for_updates({SID})

        assert [m.text for m in msgs] == ["fresh after clear"]
        tracked = monitor.state.get_session(SID)
        assert tracked is not None
        assert tracked.last_byte_offset == path.stat().st_size
