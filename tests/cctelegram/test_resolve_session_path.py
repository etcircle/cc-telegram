"""P1 (monitor head-of-line-stall fix) — the path-only session resolver.

Covers ``SessionManager.resolve_session_path_for_window`` and the shared
``session_monitor.select_relocation_winner`` arbitration:

  - the hot path returns the monitor's authoritative tracked path with NO
    transcript open / parse / glob (cost independent of transcript size);
  - the cold-start fallback arbitrates over ALL same-id candidates (build-path
    AND glob) via the shared helper — newest mtime wins, lexical tie-break,
    an unstattable candidate loses — never short-circuiting on the direct path;
  - a genuinely missing file reproduces the pre-P1 window-state clear;
  - ``_get_session_direct`` memoizes on ``(st_mtime_ns, st_size)``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import cctelegram.session as sess_mod
from cctelegram.config import config
from cctelegram.session import SessionManager, WindowState, _session_direct_cache
from cctelegram.session_monitor import select_relocation_winner


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``config.claude_projects_path`` at an isolated tmp projects root."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "claude_projects_path", root)
    return root


@pytest.fixture
def sm(monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    """A SessionManager with state persistence stubbed (no disk touch)."""
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    saved: dict[str, int] = {"n": 0}

    def _fake_save(self: SessionManager) -> None:
        saved["n"] += 1

    monkeypatch.setattr(SessionManager, "_save_state", _fake_save)
    mgr = SessionManager()
    mgr._save_calls = saved  # type: ignore[attr-defined]
    return mgr


# ── The hot path: the monitor's tracked record, no I/O ────────────────────


@pytest.mark.asyncio
async def test_tracked_path_returned_no_glob_no_parse(
    sm: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wired tracked-path getter answers with NO cold-start fallback and NO
    ``_get_session_direct`` parse — the whole point of P1."""
    sm.window_states["@0"] = WindowState(session_id="sid-A", cwd="/proj")
    sm.set_tracked_path_getter(
        lambda sid: "/live/dir/sid-A.jsonl" if sid == "sid-A" else None
    )

    def _boom_cold(*_a: object, **_k: object) -> None:
        raise AssertionError("cold-start fallback must not run on the hot path")

    async def _boom_direct(*_a: object, **_k: object) -> None:
        raise AssertionError("_get_session_direct must not run on the hot path")

    monkeypatch.setattr(sm, "_cold_start_session_path", _boom_cold)
    monkeypatch.setattr(sm, "_get_session_direct", _boom_direct)

    assert await sm.resolve_session_path_for_window("@0") == "/live/dir/sid-A.jsonl"


@pytest.mark.asyncio
async def test_hot_path_independent_of_transcript_size(
    sm: SessionManager, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver never OPENS the transcript, so its cost is independent of
    transcript size — a 25 MB session resolves as cheaply as an empty one."""
    huge = projects / "huge-sid.jsonl"
    huge.write_text(("x" * 1024 + "\n") * 2048)  # a couple MB of junk
    sm.window_states["@0"] = WindowState(session_id="sid-A", cwd="/proj")
    sm.set_tracked_path_getter(lambda sid: str(huge))

    opens = {"n": 0}
    real_open = sess_mod.aiofiles.open

    def _spy(*a: object, **k: object) -> object:
        opens["n"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(sess_mod.aiofiles, "open", _spy)

    assert await sm.resolve_session_path_for_window("@0") == str(huge)
    assert opens["n"] == 0  # transcript never opened


@pytest.mark.asyncio
async def test_relocated_tracked_path_returns_new_path(sm: SessionManager) -> None:
    """After a relocation the monitor repoints its record; the SAME unchanged
    window binding then resolves to the NEW path (GH #61 arbitration lives in
    the monitor, and the resolver simply trusts it)."""
    sm.window_states["@0"] = WindowState(session_id="sid-A", cwd="/proj")
    location = {"path": "/old/sid-A.jsonl"}
    sm.set_tracked_path_getter(lambda sid: location["path"])

    assert await sm.resolve_session_path_for_window("@0") == "/old/sid-A.jsonl"
    location["path"] = "/new/worktree/sid-A.jsonl"  # monitor relocation sync
    assert await sm.resolve_session_path_for_window("@0") == "/new/worktree/sid-A.jsonl"


@pytest.mark.asyncio
async def test_no_session_binding_returns_none(sm: SessionManager) -> None:
    """No session_id / cwd on the window → None, before any getter/fallback."""
    sm.window_states["@0"] = WindowState(session_id="", cwd="")
    assert await sm.resolve_session_path_for_window("@0") is None


# ── The cold-start fallback: shared relocation arbitration ────────────────


@pytest.mark.asyncio
async def test_cold_start_newest_mtime_wins_over_stale_direct(
    sm: SessionManager, projects: Path
) -> None:
    """No tracked record yet: the fallback collects the build-path AND every
    glob match and picks the NEWEST mtime — even when the (stale) direct
    build-path exists. Never short-circuits on the direct path."""
    direct_dir = projects / SessionManager._encode_cwd("/proj")
    direct_dir.mkdir(parents=True)
    stale = direct_dir / "sid-R.jsonl"
    stale.write_text("{}\n")

    other_dir = projects / "-relocated-proj"
    other_dir.mkdir()
    fresh = other_dir / "sid-R.jsonl"
    fresh.write_text("{}\n")

    os.utime(stale, ns=(1_000_000_000, 1_000_000_000))
    os.utime(fresh, ns=(2_000_000_000, 2_000_000_000))

    sm.window_states["@0"] = WindowState(session_id="sid-R", cwd="/proj")
    # No getter wired → cold-start fallback.
    assert await sm.resolve_session_path_for_window("@0") == str(fresh)


def test_select_relocation_winner_equal_mtime_lexical_tiebreak(
    tmp_path: Path,
) -> None:
    """Equal mtime → the lexicographically LARGER path wins, deterministically."""
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text("{}\n")
    b.write_text("{}\n")
    os.utime(a, ns=(5_000_000_000, 5_000_000_000))
    os.utime(b, ns=(5_000_000_000, 5_000_000_000))
    assert select_relocation_winner([a, b]) == b
    assert select_relocation_winner([b, a]) == b  # order-independent


def test_select_relocation_winner_unstattable_loses(tmp_path: Path) -> None:
    """A candidate that cannot be stat'd ranks below every readable one."""
    real = tmp_path / "real.jsonl"
    real.write_text("{}\n")
    ghost = tmp_path / "gone.jsonl"  # never created
    assert select_relocation_winner([ghost, real]) == real


def test_select_relocation_winner_dedupes_and_empty(tmp_path: Path) -> None:
    """Duplicate paths collapse; an empty candidate set yields None."""
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n")
    assert select_relocation_winner([p, p]) == p
    assert select_relocation_winner([]) is None


# ── Missing-file side effect (parity with resolve_session_for_window) ─────


@pytest.mark.asyncio
async def test_missing_file_clears_window_state(
    sm: SessionManager, projects: Path
) -> None:
    """No tracked path and no candidate file anywhere → None, and the window's
    stale session binding is CLEARED (reproducing the pre-P1 resolver)."""
    sm.window_states["@0"] = WindowState(session_id="sid-gone", cwd="/proj")
    # No getter, nothing on disk under the tmp projects root.
    assert await sm.resolve_session_path_for_window("@0") is None
    state = sm.window_states["@0"]
    assert state.session_id == ""
    assert state.cwd == ""
    assert sm._save_calls["n"] >= 1  # type: ignore[attr-defined]


# ── _get_session_direct memoization ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_direct_memoizes_on_mtime_size(
    sm: SessionManager, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged transcript is parsed ONCE; a content change (size/mtime)
    invalidates the entry and re-parses."""
    _session_direct_cache.clear()
    proj = projects / SessionManager._encode_cwd("/proj")
    proj.mkdir(parents=True)
    f = proj / "sid-M.jsonl"
    f.write_text(json.dumps({"type": "summary", "summary": "hi"}) + "\n")

    opens = {"n": 0}
    real_open = sess_mod.aiofiles.open

    def _spy(*a: object, **k: object) -> object:
        opens["n"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(sess_mod.aiofiles, "open", _spy)

    s1 = await sm._get_session_direct("sid-M", "/proj")
    s2 = await sm._get_session_direct("sid-M", "/proj")
    assert s1 is not None and s1.summary == "hi"
    assert s2 is not None and s2.summary == "hi"
    assert opens["n"] == 1  # second call served from cache

    # A content change (different size) invalidates the (mtime_ns, size) key.
    f.write_text(
        json.dumps({"type": "summary", "summary": "first"})
        + "\n"
        + json.dumps({"type": "summary", "summary": "second"})
        + "\n"
    )
    s3 = await sm._get_session_direct("sid-M", "/proj")
    assert s3 is not None and s3.summary == "second"
    assert opens["n"] == 2
