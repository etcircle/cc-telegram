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
    sm: SessionManager, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EXISTING tracked path answers with NO cold-start fallback and NO
    ``_get_session_direct`` parse — the hot path pays only the single stat."""
    live = projects / "live-sid-A.jsonl"
    live.write_text("{}\n")
    sm.window_states["@0"] = WindowState(session_id="sid-A", cwd="/proj")
    sm.set_tracked_path_getter(lambda sid: str(live) if sid == "sid-A" else None)

    def _boom_cold(*_a: object, **_k: object) -> None:
        raise AssertionError("cold-start fallback must not run on the hot path")

    async def _boom_direct(*_a: object, **_k: object) -> None:
        raise AssertionError("_get_session_direct must not run on the hot path")

    monkeypatch.setattr(sm, "_cold_start_session_path", _boom_cold)
    monkeypatch.setattr(sm, "_get_session_direct", _boom_direct)

    assert await sm.resolve_session_path_for_window("@0") == str(live)


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
async def test_relocated_tracked_path_returns_new_path(
    sm: SessionManager, projects: Path
) -> None:
    """After a relocation the monitor repoints its record; the SAME unchanged
    window binding then resolves to the NEW (existing) path. Both are real
    files, so the stat-checked fast path returns each directly."""
    old = projects / "old-sid-A.jsonl"
    new = projects / "new-worktree-sid-A.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    sm.window_states["@0"] = WindowState(session_id="sid-A", cwd="/proj")
    location = {"path": str(old)}
    sm.set_tracked_path_getter(lambda sid: location["path"])

    assert await sm.resolve_session_path_for_window("@0") == str(old)
    location["path"] = str(new)  # monitor relocation sync repoints the record
    assert await sm.resolve_session_path_for_window("@0") == str(new)


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


def test_lru_evicts_oldest_at_capacity() -> None:
    """The 65th distinct entry evicts the OLDEST (Codex fold 5)."""
    from cctelegram.session import _SESSION_DIRECT_CACHE_MAX, _remember_session_direct

    _session_direct_cache.clear()
    for i in range(_SESSION_DIRECT_CACHE_MAX):
        _remember_session_direct(f"/p/{i}", (i, i), None)
    assert len(_session_direct_cache) == _SESSION_DIRECT_CACHE_MAX
    assert "/p/0" in _session_direct_cache  # still present at capacity

    _remember_session_direct("/p/new", (999, 999), None)  # one past capacity
    assert len(_session_direct_cache) == _SESSION_DIRECT_CACHE_MAX
    assert "/p/0" not in _session_direct_cache  # oldest evicted
    assert "/p/new" in _session_direct_cache


@pytest.mark.asyncio
async def test_lru_hit_promotion_via_get_session_direct(
    sm: SessionManager, projects: Path
) -> None:
    """A cache HIT in ``_get_session_direct`` PROMOTES the entry (move_to_end),
    so a later insertion evicts a colder entry instead — the promoted one
    survives (Codex fold 5)."""
    from cctelegram.session import _SESSION_DIRECT_CACHE_MAX, _remember_session_direct

    _session_direct_cache.clear()
    proj = projects / SessionManager._encode_cwd("/proj")
    proj.mkdir(parents=True)
    f = proj / "keep.jsonl"
    f.write_text(json.dumps({"type": "summary", "summary": "keep"}) + "\n")

    assert await sm._get_session_direct("keep", "/proj") is not None
    fkey = str(f)
    assert fkey in _session_direct_cache

    # Fill to capacity; keep's entry is now the OLDEST.
    for i in range(_SESSION_DIRECT_CACHE_MAX - 1):
        _remember_session_direct(f"/dummy/{i}", (i, i), None)
    assert len(_session_direct_cache) == _SESSION_DIRECT_CACHE_MAX

    # Cache HIT on keep → promoted to most-recent (unchanged file).
    assert await sm._get_session_direct("keep", "/proj") is not None

    # One more entry evicts the now-oldest dummy, NOT the promoted keep.
    _remember_session_direct("/dummy/new", (999, 999), None)
    assert fkey in _session_direct_cache  # promoted → survived
    assert "/dummy/0" not in _session_direct_cache  # oldest evicted


# ── Codex fold-round: fast-path stat, swap safety, re-arbitration ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_path",
    [
        "/gone/register-seeded/sid-S.jsonl",  # register_session build path
        "/gone/updater-reassoc/sid-S.jsonl",  # updater.reassociate_routing path
    ],
)
async def test_stale_tracked_path_falls_to_arbitration(
    sm: SessionManager, projects: Path, stale_path: str
) -> None:
    """A tracked path that does NOT exist (a non-arbitrated build path seeded by
    register_session / updater reassociation) fails the fast-path stat and
    falls through to arbitration, which resolves the REAL on-disk file."""
    proj = projects / SessionManager._encode_cwd("/proj")
    proj.mkdir(parents=True)
    real = proj / "sid-S.jsonl"
    real.write_text("{}\n")
    sm.window_states["@0"] = WindowState(session_id="sid-S", cwd="/proj")
    sm.set_tracked_path_getter(lambda sid: stale_path)

    assert await sm.resolve_session_path_for_window("@0") == str(real)


@pytest.mark.asyncio
async def test_relocation_during_delay_resolves_newer_copy(
    sm: SessionManager, projects: Path
) -> None:
    """A same-id relocation while delivery is delayed: the tracked record still
    names the pre-move (now gone) path → fast-path stat fails → arbitration
    picks the NEWER of the on-disk same-id copies."""
    stale_dir = projects / SessionManager._encode_cwd("/proj")
    stale_dir.mkdir(parents=True)
    stale = stale_dir / "sid-R.jsonl"
    stale.write_text("{}\n")

    moved_dir = projects / "-relocated"
    moved_dir.mkdir()
    moved = moved_dir / "sid-R.jsonl"
    moved.write_text("{}\n")

    os.utime(stale, ns=(1_000_000_000, 1_000_000_000))
    os.utime(moved, ns=(2_000_000_000, 2_000_000_000))  # newer

    sm.window_states["@0"] = WindowState(session_id="sid-R", cwd="/proj")
    # The tracked record points at a path that no longer exists (pre-move).
    sm.set_tracked_path_getter(lambda sid: "/gone/pre-move/sid-R.jsonl")

    assert await sm.resolve_session_path_for_window("@0") == str(moved)


@pytest.mark.asyncio
async def test_swap_during_cold_resolve_does_not_clear_new_session(
    sm: SessionManager,
) -> None:
    """If ``load_session_map`` swaps the window to a DIFFERENT session while the
    off-thread cold resolution is awaiting, the missing-file cleanup must NOT
    clear the new session's binding (compare-and-clear — Codex fold 1)."""
    sm.window_states["@0"] = WindowState(session_id="sess-A", cwd="/proj-a")

    def _swap_then_miss(session_id: str, cwd: str) -> None:
        # Simulate the concurrent load_session_map swap A -> B mid-await, then
        # report "no file found" for the (now-superseded) session A.
        st = sm.window_states["@0"]
        st.session_id = "sess-B"
        st.cwd = "/proj-b"
        return None

    sm._cold_start_session_path = _swap_then_miss  # type: ignore[method-assign]

    assert await sm.resolve_session_path_for_window("@0") is None
    st = sm.window_states["@0"]
    assert st.session_id == "sess-B"  # NOT cleared
    assert st.cwd == "/proj-b"
    assert sm._save_calls["n"] == 0  # type: ignore[attr-defined]


def test_singleton_arbitration_performs_no_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single candidate returns WITHOUT a stat — the 1 s scan pays no
    filesystem I/O for the common one-candidate case (Codex fold 3)."""
    only = tmp_path / "only.jsonl"  # need not exist

    stat_calls = {"n": 0}
    real_stat = Path.stat

    def _spy(self: Path, *a: object, **k: object) -> object:
        stat_calls["n"] += 1
        return real_stat(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", _spy)

    assert select_relocation_winner([only]) == only
    assert stat_calls["n"] == 0


@pytest.mark.asyncio
async def test_cold_winner_vanish_rearbitrates_remaining(
    sm: SessionManager, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the ranked cold winner disappears between ranking and the confirming
    stat, the fallback re-arbitrates the REMAINING candidates rather than
    returning None (Codex fold 4)."""
    d1 = projects / "-p1"
    d1.mkdir()
    winner_file = d1 / "sid-V.jsonl"
    winner_file.write_text("{}\n")

    d2 = projects / "-p2"
    d2.mkdir()
    other = d2 / "sid-V.jsonl"
    other.write_text("{}\n")

    os.utime(winner_file, ns=(3_000_000_000, 3_000_000_000))  # ranks first
    os.utime(other, ns=(1_000_000_000, 1_000_000_000))

    # cwd encodes to a dir with no file, so the build-path candidate is inert.
    sm.window_states["@0"] = WindowState(session_id="sid-V", cwd="/no-such")

    real_stat = Path.stat
    seen = {"n": 0}

    def _spy(self: Path, *a: object, **k: object) -> object:
        if str(self) == str(winner_file):
            seen["n"] += 1
            if seen["n"] >= 2:  # ranking stat OK; the confirming stat vanishes
                raise FileNotFoundError(str(winner_file))
        return real_stat(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", _spy)

    # No getter → straight to the cold fallback.
    assert await sm.resolve_session_path_for_window("@0") == str(other)
