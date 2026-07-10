"""GH #39: the startup-GC liveness predicate consults session_map.json in
addition to monitor.state (which LAGS the map at startup), and fails conservative
(skip all GC) on a session_map read/parse error."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cctelegram import bot as bot_module
from cctelegram.config import config


def _monitor(tracked: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            get_session=lambda sid: object() if sid in tracked else None
        )
    )


def _write_map(tmp_path, obj) -> None:
    (tmp_path / "session_map.json").write_text(
        obj if isinstance(obj, str) else json.dumps(obj)
    )


@pytest.fixture
def _map_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    return tmp_path


def test_live_if_in_session_map_but_not_tracked(_map_path) -> None:
    _write_map(_map_path, {"cc-telegram:@0": {"session_id": "sid-map"}})
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("sid-map") is True
    assert pred("sid-unknown") is False


def test_live_if_tracked_but_not_in_map(_map_path) -> None:
    _write_map(_map_path, {})
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor({"sid-tracked"}))
    assert pred("sid-tracked") is True
    assert pred("sid-other") is False


def test_missing_file_skips_all_gc(_map_path) -> None:
    # No session_map.json → indistinguishable from deletion / a lagging startup
    # state, so the predicate fails conservative: skip ALL GC this startup.
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("sid-anything") is True
    assert pred("sid-other") is True


def test_non_utf8_file_skips_all_gc(_map_path) -> None:
    # Invalid UTF-8 raises UnicodeDecodeError from read_text; it must be
    # caught as a read failure (skip-all), never abort startup.
    (_map_path / "session_map.json").write_bytes(b"\xff\xfe{not utf8}")
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("sid-anything") is True


def test_malformed_json_skips_all_gc(_map_path) -> None:
    _write_map(_map_path, "{not valid json")
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("anything") is True
    assert pred("another") is True


def test_non_dict_json_skips_all_gc(_map_path) -> None:
    _write_map(_map_path, ["not", "a", "dict"])
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("anything") is True


def test_ignores_entries_without_session_id(_map_path) -> None:
    _write_map(
        _map_path,
        {
            "cc-telegram:@0": {"session_id": "sid-good"},
            "cc-telegram:@1": {"cwd": "/tmp"},  # no session_id
            "cc-telegram:@2": "not-a-dict",
        },
    )
    pred = bot_module._build_startup_gc_liveness_predicate(_monitor(set()))
    assert pred("sid-good") is True
    assert pred("") is False
