"""Wave B unit tests — the Notification-hook side-file trust boundary
(``handlers/notify_source.py``).

Covers: schema validation, the hard window-key read predicate
(double-``--resume`` sibling safety), future-skew rejection, the
generation-guarded unlink (a hook re-fire between read and unlink
survives), the unconditional teardown unlink, and the 24h startup GC
with the injected ``is_live_session`` conservative-skip predicate.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cctelegram.handlers import notify_source
from cctelegram.session import WindowState, session_manager
from cctelegram.tmux_manager import tmux_manager

_SID = "550e8400-e29b-41d4-a716-446655440000"
_SID_B = "550e8400-e29b-41d4-a716-446655440001"
_WID = "@5"


@pytest.fixture
def _cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def _bound_window():
    session_manager.window_states[_WID] = WindowState(cwd="/tmp/x", session_id=_SID)
    yield
    session_manager.window_states.pop(_WID, None)


def _window_key(window_id: str = _WID) -> str:
    return f"{tmux_manager.session_name}:{window_id}"


def _write_record(
    cc_dir: Path,
    session_id: str = _SID,
    *,
    ts: float | None = None,
    window_key: str | None = None,
    generation: str = "g1",
    kind: str = "permission",
    schema: int = 1,
) -> Path:
    d = cc_dir / "notify_pending"
    d.mkdir(mode=0o700, exist_ok=True)
    rec = {
        "schema_version": schema,
        "session_id": session_id,
        "ts": ts if ts is not None else time.time(),
        "window_key": window_key if window_key is not None else _window_key(),
        "generation": generation,
        "kind": kind,
    }
    path = d / f"{session_id}.json"
    path.write_text(json.dumps(rec))
    return path


# ── read predicate ───────────────────────────────────────────────────────


def test_valid_record_returned(_cc_dir, _bound_window):
    _write_record(_cc_dir, generation="gen-77", kind="permission")
    rec = notify_source.notification_pending_for_window(_WID)
    assert rec is not None
    assert rec.session_id == _SID
    assert rec.generation == "gen-77"
    assert rec.kind == "permission"
    assert rec.window_key == _window_key()


def test_window_key_mismatch_rejected_double_resume_sibling(_cc_dir, _bound_window):
    """The same session bound to two windows: the record was written for
    window A — window B's read MUST return None (hard read-time predicate)."""
    session_manager.window_states["@9"] = WindowState(cwd="/tmp/x", session_id=_SID)
    try:
        _write_record(_cc_dir, window_key=_window_key(_WID))
        assert notify_source.notification_pending_for_window(_WID) is not None
        assert notify_source.notification_pending_for_window("@9") is None
    finally:
        session_manager.window_states.pop("@9", None)


def test_future_skew_rejected(_cc_dir, _bound_window):
    _write_record(_cc_dir, ts=time.time() + 3600)
    assert notify_source.notification_pending_for_window(_WID) is None


def test_schema_mismatch_rejected(_cc_dir, _bound_window):
    _write_record(_cc_dir, schema=2)
    assert notify_source.notification_pending_for_window(_WID) is None


def test_empty_generation_rejected(_cc_dir, _bound_window):
    _write_record(_cc_dir, generation="")
    assert notify_source.notification_pending_for_window(_WID) is None


def test_malformed_json_rejected(_cc_dir, _bound_window):
    d = _cc_dir / "notify_pending"
    d.mkdir(exist_ok=True)
    (d / f"{_SID}.json").write_text("{not json")
    assert notify_source.notification_pending_for_window(_WID) is None


def test_unbound_window_returns_none(_cc_dir):
    _write_record(_cc_dir)
    assert notify_source.notification_pending_for_window("@404") is None


def test_missing_file_returns_none(_cc_dir, _bound_window):
    assert notify_source.notification_pending_for_window(_WID) is None


def test_non_uuid_session_id_never_builds_path(_cc_dir):
    # Path-traversal defense in depth, mirroring auq_source.
    session_manager.window_states["@6"] = WindowState(
        cwd="/tmp/x", session_id="../evil"
    )
    try:
        assert notify_source.notification_pending_for_window("@6") is None
    finally:
        session_manager.window_states.pop("@6", None)


# ── generation-guarded unlink ───────────────────────────────────────────


def test_unlink_with_matching_generation(_cc_dir, _bound_window):
    path = _write_record(_cc_dir, generation="g1")
    assert notify_source.unlink_if_generation_matches(_SID, "g1") is True
    assert not path.exists()


def test_hook_refire_between_read_and_unlink_survives(_cc_dir, _bound_window):
    """Test 11: the consumed generation no longer matches — the NEWER record
    must survive the unlink."""
    _write_record(_cc_dir, generation="g1")
    # Hook re-fires: a newer record replaces the file.
    path = _write_record(_cc_dir, generation="g2")
    assert notify_source.unlink_if_generation_matches(_SID, "g1") is False
    assert path.exists()
    rec = notify_source.notification_pending_for_window(_WID)
    assert rec is not None and rec.generation == "g2"


def test_unlink_missing_file_is_false(_cc_dir):
    assert notify_source.unlink_if_generation_matches(_SID, "g1") is False


def test_unlink_for_session_unconditional(_cc_dir):
    path = _write_record(_cc_dir, generation="g1")
    notify_source.unlink_for_session(_SID)
    assert not path.exists()
    # Idempotent / silent on missing.
    notify_source.unlink_for_session(_SID)


# ── startup GC ──────────────────────────────────────────────────────────


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_gc_reaps_older_than_24h(_cc_dir):
    path = _write_record(_cc_dir)
    _age(path, 25 * 3600)
    assert notify_source.gc_stale() == 1
    assert not path.exists()


def test_gc_keeps_fresh_files(_cc_dir):
    path = _write_record(_cc_dir)
    assert notify_source.gc_stale() == 0
    assert path.exists()


def test_gc_skips_live_session(_cc_dir):
    path = _write_record(_cc_dir)
    _age(path, 25 * 3600)
    assert notify_source.gc_stale(is_live_session=lambda sid: sid == _SID) == 0
    assert path.exists()


def test_gc_conservative_skip_on_predicate_raise(_cc_dir):
    path = _write_record(_cc_dir)
    _age(path, 25 * 3600)

    def _boom(sid: str) -> bool:
        raise RuntimeError("liveness oracle down")

    assert notify_source.gc_stale(is_live_session=_boom) == 0
    assert path.exists()


def test_gc_reaps_dead_session_with_predicate(_cc_dir):
    path = _write_record(_cc_dir)
    _age(path, 25 * 3600)
    assert notify_source.gc_stale(is_live_session=lambda sid: False) == 1
    assert not path.exists()


def test_gc_ignores_non_uuid_names(_cc_dir):
    d = _cc_dir / "notify_pending"
    d.mkdir(exist_ok=True)
    stray = d / "not-a-uuid.json"
    stray.write_text("{}")
    _age(stray, 25 * 3600)
    assert notify_source.gc_stale() == 0
    assert stray.exists()
