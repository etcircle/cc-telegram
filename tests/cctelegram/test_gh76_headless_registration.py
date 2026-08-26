"""GH #76 — a headless ``claude`` ancestor must never write session_map.

A hook-spawned headless run (``claude --print``, e.g. a threshold- or
SessionEnd-triggered ``/self-curate``) INHERITS the interactive session's
``TMUX_PANE`` and cwd, so its SessionStart hook registered itself in
``session_map.json`` under the INTERACTIVE window's key. Last-writer-wins then
flipped both the routing authority and the tracking authority to the headless
sid, untracking the interactive session plus its per-parent sidechain
registries — every subsequent interactive message was silently lost while
pane-polling kept the topic looking alive. Confirmed twice on 2026-08-26.
``CLAUDE_CODE_ENTRYPOINT`` cannot tell the two apart (the child inherits it),
so detection is argv inspection of the nearest ``claude`` ancestor.

INVARIANT: a hook process with a headless ``claude`` ancestor never writes
``session_map.json``.

CO-INVARIANT (equally load-bearing): the guard FAILS OPEN. Every probe failure
— no ``/proc``, vanished pids, malformed status, depth exhausted, even a
raising helper — must still register, because a fail-closed bug would stop ALL
session registration, strictly worse than the bug being fixed.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from cctelegram import hook
from cctelegram.hook import (
    _HEADLESS_ANCESTOR_MAX_HOPS,
    _find_headless_claude_ancestor,
    hook_main,
)

SID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_SID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
PANE = "%42"
WINDOW_KEY = "main:@7"


# ── fake /proc tree ───────────────────────────────────────────────────────


def _write_proc(
    proc_root: Path,
    pid: int,
    *,
    ppid: int,
    comm: str,
    argv: list[str] | None = None,
) -> None:
    """Materialize one ``<proc_root>/<pid>/`` entry."""
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(f"Name:\t{comm}\nState:\tS (sleeping)\nPPid:\t{ppid}\n")
    (d / "comm").write_text(comm + "\n")
    if argv is not None:
        (d / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())


def _chain(proc_root: Path, entries: list[tuple[str, list[str] | None]]) -> int:
    """Build an ancestry chain rooted at the caller's parent.

    ``entries[0]`` is the immediate parent (hop 1). Returns the pid of the
    LAST entry so a test can assert on ``ancestor_pid``.
    """
    base = 1000
    last = 0
    for i, (comm, argv) in enumerate(entries):
        pid = base + i
        ppid = base + i + 1 if i + 1 < len(entries) else 1
        _write_proc(proc_root, pid, ppid=ppid, comm=comm, argv=argv)
        last = pid
    return last


@pytest.fixture(autouse=True)
def _clear_guard_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the unset (ON by default) state, whatever the
    developer's ambient environment says."""
    monkeypatch.delenv("CC_TELEGRAM_HEADLESS_REGISTRATION_GUARD", raising=False)


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake proc tree whose hop-1 pid is what ``os.getppid()`` returns."""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(hook.os, "getppid", lambda: 1000)
    return root


# ── helper unit tests ─────────────────────────────────────────────────────


class TestFindHeadlessClaudeAncestor:
    @pytest.mark.parametrize("token", ["-p", "--print"])
    def test_exact_headless_token_matches(self, proc_root: Path, token: str) -> None:
        pid = _chain(
            proc_root,
            [
                ("bash", ["bash", "-c", "cc-telegram hook"]),
                ("claude", ["claude", token]),
            ],
        )
        match = _find_headless_claude_ancestor(proc_root)
        assert match is not None
        assert match.ancestor_pid == pid
        assert match.argv_token == token

    @pytest.mark.parametrize(
        "argv",
        [
            ["claude", "--print-foo"],
            ["claude", "-print"],
            ["claude", "--resume", "--no-print"],
            ["claude", "--append-system-prompt", "use -p carefully"],
            ["claude", "--model", "opus"],
        ],
    )
    def test_non_headless_argv_does_not_match(
        self, proc_root: Path, argv: list[str]
    ) -> None:
        """Prefix / substring / interior occurrences are NOT headless flags."""
        _chain(proc_root, [("claude", argv)])
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_interactive_resume_does_not_match(self, proc_root: Path) -> None:
        _chain(
            proc_root,
            [("bash", None), ("claude", ["claude", "--resume", SID])],
        )
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_no_claude_in_ancestry(self, proc_root: Path) -> None:
        _chain(proc_root, [("bash", None), ("tmux", None), ("systemd", None)])
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_claude_at_max_hops_is_found(self, proc_root: Path) -> None:
        chain: list[tuple[str, list[str] | None]] = [("bash", None)] * (
            _HEADLESS_ANCESTOR_MAX_HOPS - 1
        )
        chain.append(("claude", ["claude", "--print"]))
        pid = _chain(proc_root, chain)
        match = _find_headless_claude_ancestor(proc_root)
        assert match is not None and match.ancestor_pid == pid

    def test_claude_one_hop_beyond_max_is_not_found(self, proc_root: Path) -> None:
        chain: list[tuple[str, list[str] | None]] = [("bash", None)] * (
            _HEADLESS_ANCESTOR_MAX_HOPS
        )
        chain.append(("claude", ["claude", "--print"]))
        _chain(proc_root, chain)
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_first_claude_wins_even_if_interactive(self, proc_root: Path) -> None:
        """The walk stops at the NEAREST claude; an outer headless one is not
        this session's parent and must not veto registration."""
        _chain(
            proc_root,
            [
                ("claude", ["claude", "--resume", SID]),
                ("claude", ["claude", "--print"]),
            ],
        )
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_missing_status_mid_walk_fails_open(self, proc_root: Path) -> None:
        _chain(proc_root, [("bash", None), ("claude", ["claude", "-p"])])
        (proc_root / "1000" / "status").unlink()
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_missing_cmdline_fails_open(self, proc_root: Path) -> None:
        _chain(proc_root, [("claude", ["claude", "-p"])])
        (proc_root / "1000" / "cmdline").unlink()
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_malformed_status_fails_open(self, proc_root: Path) -> None:
        _chain(proc_root, [("bash", None), ("claude", ["claude", "-p"])])
        (proc_root / "1000" / "status").write_text("Name:\tbash\nPPid:\tnot-a-number\n")
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_status_without_ppid_fails_open(self, proc_root: Path) -> None:
        _chain(proc_root, [("bash", None), ("claude", ["claude", "-p"])])
        (proc_root / "1000" / "status").write_text("Name:\tbash\nState:\tS\n")
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_vanished_pid_mid_walk_fails_open(self, proc_root: Path) -> None:
        """Races with process exit are ordinary, not exceptional."""
        _write_proc(proc_root, 1000, ppid=2222, comm="bash")
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_ppid_zero_terminates_walk(
        self, proc_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel/reaped parent (pid 0) ends the walk before any file read."""
        monkeypatch.setattr(hook.os, "getppid", lambda: 0)
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_empty_cmdline_fails_open(self, proc_root: Path) -> None:
        """A claude ancestor whose cmdline is empty (zombie/exiting) is not
        a match — empty tokens never equal ``-p``/``--print``."""
        _chain(proc_root, [("claude", [])])
        assert _find_headless_claude_ancestor(proc_root) is None

    def test_missing_proc_root_is_inert(self, tmp_path: Path) -> None:
        """The non-Linux shape: no /proc → guard inert, registration proceeds."""
        assert _find_headless_claude_ancestor(tmp_path / "nope") is None


# ── end-to-end through hook_main ──────────────────────────────────────────


class TestSessionStartGuard:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        headless: hook._HeadlessMatch | None,
        raising: bool = False,
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setenv("TMUX_PANE", PANE)
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        # MANDATORY: the conftest _no_live_tmux fence does not cover
        # subprocess.run, so an un-patched resolve would hit the real server.
        monkeypatch.setattr(
            hook, "_resolve_tmux_window_key", lambda pane: ("main", "@7", "dev")
        )

        def _probe(proc_root: Path = Path("/proc")) -> hook._HeadlessMatch | None:
            if raising:
                raise RuntimeError("probe blew up")
            return headless

        monkeypatch.setattr(hook, "_find_headless_claude_ancestor", _probe)
        payload = {
            "session_id": SID,
            "cwd": "/home/tester/dev/cc-telegram",
            "hook_event_name": "SessionStart",
        }
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert hook_main() == 0

    def test_headless_match_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._run(
            monkeypatch,
            tmp_path,
            headless=hook._HeadlessMatch(ancestor_pid=4242, argv_token="--print"),
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_headless_match_leaves_existing_entry_intact(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The actual #76 shape: the interactive binding must SURVIVE."""
        map_file = tmp_path / "session_map.json"
        map_file.write_text(
            json.dumps(
                {
                    WINDOW_KEY: {
                        "session_id": OTHER_SID,
                        "cwd": "/home/tester/dev/cc-telegram",
                        "window_name": "dev",
                    }
                }
            )
        )
        self._run(
            monkeypatch,
            tmp_path,
            headless=hook._HeadlessMatch(ancestor_pid=4242, argv_token="-p"),
        )
        assert json.loads(map_file.read_text())[WINDOW_KEY]["session_id"] == OTHER_SID

    def test_no_match_registers_as_before(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._run(monkeypatch, tmp_path, headless=None)
        data = json.loads((tmp_path / "session_map.json").read_text())
        assert data[WINDOW_KEY]["session_id"] == SID
        assert data[WINDOW_KEY]["window_name"] == "dev"

    def test_raising_probe_still_registers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FAIL OPEN. Without the local try/except the exception would reach
        hook_main's catch-all, which returns WITHOUT registering."""
        self._run(monkeypatch, tmp_path, headless=None, raising=True)
        data = json.loads((tmp_path / "session_map.json").read_text())
        assert data[WINDOW_KEY]["session_id"] == SID

    def test_flag_off_registers_despite_headless_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_HEADLESS_REGISTRATION_GUARD", "false")
        self._run(
            monkeypatch,
            tmp_path,
            headless=hook._HeadlessMatch(ancestor_pid=4242, argv_token="--print"),
        )
        data = json.loads((tmp_path / "session_map.json").read_text())
        assert data[WINDOW_KEY]["session_id"] == SID

    @pytest.mark.parametrize("value", ["", "banana", "TRUE-ish"])
    def test_empty_or_garbage_flag_value_keeps_guard_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
    ) -> None:
        """Default ON: only an explicit false token disarms the guard (Codex
        r1 P2 — a positive parse silently disabled it on empty exports)."""
        monkeypatch.setenv("CC_TELEGRAM_HEADLESS_REGISTRATION_GUARD", value)
        self._run(
            monkeypatch,
            tmp_path,
            headless=hook._HeadlessMatch(ancestor_pid=4242, argv_token="-p"),
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_flag_on_explicitly_still_guards(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_HEADLESS_REGISTRATION_GUARD", "1")
        self._run(
            monkeypatch,
            tmp_path,
            headless=hook._HeadlessMatch(ancestor_pid=4242, argv_token="--print"),
        )
        assert not (tmp_path / "session_map.json").exists()
