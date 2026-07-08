"""Fix A (2026-07-08) — the ``idle_prompt`` kind-gate at the Notification
trust boundary (``status_polling._consume_notification_signal``).

CC 2.1.204 fires a matcher-less ``Notification`` ~60s after every turn end with
``notification_type: "idle_prompt"`` ("Claude is waiting for your input"). On a
stored-idle route with live background keys the §3.6 commit turned that nudge
into a false "🔔 Waiting on you" + typing-dark + a spurious decision card. The
gate DROPS a ``kind == "idle_prompt"`` record (generation-guarded unlink,
reconcile, return — NO commit, NO card); everything else (``permission_prompt``,
empty/unknown kinds) keeps today's full commit-or-stale path (fail-open).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import attention, status_polling
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent
from cctelegram.session import WindowState, session_manager
from cctelegram.tmux_manager import tmux_manager as real_tmux

_SID = "550e8400-e29b-41d4-a716-446655440000"
_WID = "@5"
_USER = 1
_THREAD = 42
_ROUTE = (_USER, _THREAD, _WID)
_KEY = "a1951c4043e2c9561"


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 999
    bot.send_message.return_value = sent
    return bot


@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    session_manager.window_states[_WID] = WindowState(cwd="/tmp/x", session_id=_SID)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()
    status_polling._decision_card_eot_grace.clear()
    yield tmp_path
    session_manager.window_states.pop(_WID, None)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()
    status_polling._decision_card_eot_grace.clear()


def _write_record(
    cc_dir: Path,
    *,
    kind: str,
    ts: float | None = None,
    generation: str = "g1",
) -> Path:
    d = cc_dir / "notify_pending"
    d.mkdir(mode=0o700, exist_ok=True)
    rec = {
        "schema_version": 1,
        "session_id": _SID,
        "ts": ts if ts is not None else time.time(),
        "window_key": f"{real_tmux.session_name}:{_WID}",
        "generation": generation,
        "kind": kind,
    }
    path = d / f"{_SID}.json"
    path.write_text(json.dumps(rec))
    return path


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    stop_reason: str | None = None,
    timestamp: float | None = None,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=None,
        tool_name=None,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


async def _tick(mock_bot, pane_text: str | None = None) -> None:
    window = MagicMock()
    window.window_id = _WID
    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(status_polling, "enqueue_status_update", AsyncMock()),
        patch.object(
            status_polling.session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=None),
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        await status_polling.update_status_message(
            mock_bot, user_id=_USER, window_id=_WID, thread_id=_THREAD
        )


async def _idle_with_live_bg_key() -> None:
    """Stored-idle route + one live background key (the §3.6 projected-busy
    shape that Defect A hit on every 60s nudge)."""
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    await route_runtime.mark_background_agent_launched(_ROUTE, _KEY)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.run_state is RunState.RUNNING  # projected busy
    assert snap.typing_eligible is True


# ── the live-incident regression pin ─────────────────────────────────────


async def test_idle_prompt_on_projected_busy_route_is_dropped(_env, mock_bot):
    """Defect A: stored-idle + live bg keys + idle_prompt → NO commit, NO card,
    typing STAYS lifted (the §3.6 false-🔔 is gone)."""
    await _idle_with_live_bg_key()
    path = _write_record(_env, kind="idle_prompt")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False  # NOT committed
    assert snap.run_state is RunState.RUNNING  # projection lift intact
    assert snap.typing_eligible is True  # typing NOT dark
    assert not path.exists()  # generation-guarded unlink of the dropped record
    # No decision card was posted (the spurious "🔔 needs a decision" nudge).
    assert not mock_bot.send_message.called


async def test_idle_prompt_on_plain_idle_route_is_dropped(_env, mock_bot):
    """Even without a bg key, idle_prompt is dropped (never commits, never
    cards) — the nudge is exactly what the transcript end-of-turn renders."""
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    path = _write_record(_env, kind="idle_prompt")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert not path.exists()
    assert not mock_bot.send_message.called


# ── fail-open: permission_prompt / empty / unknown still commit ──────────


async def test_permission_prompt_commits_byte_identically(_env, mock_bot):
    """A REAL approval gate (permission_prompt) still commits via §3.6 on the
    projected-busy route — the gate is NOT loosened for real prompts."""
    await _idle_with_live_bg_key()
    path = _write_record(_env, kind="permission_prompt")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER  # 🔔 outranks the lift
    assert not path.exists()  # committed-live → unlink after commit


async def test_empty_kind_commits_fail_open(_env, mock_bot):
    """kind="" (older hook / missing field) FAILS OPEN — commits as today."""
    await route_runtime.mark_inbound_sent(_ROUTE)  # RUNNING
    path = _write_record(_env, kind="")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    assert not path.exists()


async def test_unknown_kind_commits_fail_open(_env, mock_bot):
    """A FUTURE unknown kind fails open (the rig could not enumerate CC's whole
    type space; unknown-kind-commits preserves approval-gate safety)."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    path = _write_record(_env, kind="some_future_prompt")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert not path.exists()


# ── consume-ORDER pin: the idle-drop sits BEFORE the same-generation return ──


async def test_idle_prompt_drop_precedes_same_generation_return(_env, mock_bot):
    """Hermes r1 P2: a reflected same-generation idle record must NOT bypass the
    drop. Commit a real permission at gen g1, then overwrite the side file with
    an idle_prompt at the SAME generation g1. The idle-drop (which unlinks) must
    run BEFORE the same-generation early-return (which does NOT unlink), so the
    idle_prompt file is gone."""
    await _idle_with_live_bg_key()
    _write_record(_env, kind="permission_prompt", generation="g1")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    # Overwrite with an idle_prompt at the SAME generation.
    path = _write_record(_env, kind="idle_prompt", generation="g1")
    await _tick(mock_bot)
    # The drop's generation-guarded unlink removed it (proving order): a
    # same-gen return would have left the file in place.
    assert not path.exists()


# ── unlink/generation semantics of the drop ─────────────────────────────


async def test_idle_prompt_drop_is_generation_guarded(_env, mock_bot):
    """A hook re-fire between read and unlink (a NEW generation) survives the
    idle-drop's generation-guarded unlink."""
    await _idle_with_live_bg_key()
    _write_record(_env, kind="idle_prompt", generation="g1")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0

    # Race: after the consume reads g1 but before it unlinks, a re-fire writes
    # g2. The generation-guarded unlink must NOT delete g2.
    real_unlink = status_polling.notify_source.unlink_if_generation_matches

    def _racing_unlink(session_id: str, generation: str) -> bool:
        _write_record(_env, kind="idle_prompt", generation="g2")
        return real_unlink(session_id, generation)

    with patch.object(
        status_polling.notify_source,
        "unlink_if_generation_matches",
        side_effect=_racing_unlink,
    ):
        await _tick(mock_bot)

    path = _env / "notify_pending" / f"{_SID}.json"
    assert path.exists()  # g2 survived the g1-guarded unlink
    assert json.loads(path.read_text())["generation"] == "g2"
