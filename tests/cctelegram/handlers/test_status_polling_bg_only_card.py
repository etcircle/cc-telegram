"""Poller tests — the background-only "labeled silence" episode card.

Pins the plan §2/§3 contract (edge-triggered, pull-only, one card per episode):

  - A stored-idle route lifted to projected RUNNING purely on live background
    keys (``snapshot.background_only``) posts ONE silent line per episode.
  - Exactly one card per episode: a second tick while still background-only
    does NOT re-post.
  - Episode end (``background_only`` False) clears the one-shot flag WITHOUT a
    new send; the card stays in history. A LATER (re-launched) episode posts a
    fresh card.
  - quiet preset (``digest_card`` False) → no card ever.
  - A committed 🔔 outranks the lift (``background_only`` False) → no card (the
    decision card owns that state).
  - A failed send (topic-shaped outcome / sent is None) does NOT set the flag —
    the next tick retries (idempotency is the flag, never the send).
  - Placement pin: the card posts on a capture-SKIPPED tick (the check sits
    before the capture-gating early returns).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from cctelegram import route_runtime
from cctelegram.handlers import attention, status_polling

_SID = "550e8400-e29b-41d4-a716-446655440000"
_WID = "@5"
_USER = 1
_THREAD = 42
_ROUTE = (_USER, _THREAD, _WID)
_KEY = "a1b2c3d4e5f6a7b89"
_KEY2 = "b9f8e7d6c5b4a3210"

_CARD_FRAGMENT = "Background work running"


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 999
    bot.send_message.return_value = sent
    return bot


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    from cctelegram.session import WindowState, session_manager

    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    session_manager.window_states[_WID] = WindowState(cwd="/tmp/x", session_id=_SID)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._bg_only_card_posted.clear()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()
    yield
    session_manager.window_states.pop(_WID, None)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._bg_only_card_posted.clear()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()


def _evt(stop_reason: str | None = None, *, timestamp: float | None = None):
    return route_runtime.TranscriptLifecycleEvent(
        role="assistant",  # type: ignore[arg-type]
        block_type="text",  # type: ignore[arg-type]
        tool_use_id=None,
        tool_name=None,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


async def _make_background_only() -> None:
    """Drive ``_ROUTE`` to a projected-RUNNING background-only episode."""
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("end_turn", timestamp=100.0)
    )
    await route_runtime.mark_background_agent_launched(_ROUTE, _KEY)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.background_only is True


def _bg_only_sends(bot) -> list[str]:
    """The text of every ``send_message`` that is our labeled-silence card."""
    return [
        c.kwargs.get("text", "")
        for c in bot.send_message.call_args_list
        if _CARD_FRAGMENT in c.kwargs.get("text", "")
    ]


# ── direct-helper poller behavior ────────────────────────────────────────


async def test_posts_one_card_per_episode(mock_bot):
    await _make_background_only()
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert status_polling._bg_only_card_posted.get(_ROUTE) is True
    sends = _bg_only_sends(mock_bot)
    assert len(sends) == 1
    assert "1 task" in sends[0]  # singular for a single live key

    # A second tick while STILL background-only must NOT re-post.
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert len(_bg_only_sends(mock_bot)) == 1


async def test_episode_end_clears_flag_without_new_send(mock_bot):
    await _make_background_only()
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert status_polling._bg_only_card_posted.get(_ROUTE) is True

    # Background work done → episode ends. The flag clears; no new send; the
    # card stays in history (v1 — no edit/delete).
    await route_runtime.mark_background_agent_done(_ROUTE, _KEY)
    assert route_runtime.snapshot(_ROUTE).background_only is False
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert _ROUTE not in status_polling._bg_only_card_posted
    assert len(_bg_only_sends(mock_bot)) == 1  # no second send


async def test_re_episode_posts_a_fresh_card(mock_bot):
    await _make_background_only()
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    await route_runtime.mark_background_agent_done(_ROUTE, _KEY)
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert len(_bg_only_sends(mock_bot)) == 1

    # A NEW background task (distinct key) re-opens an episode → a fresh card.
    await route_runtime.mark_background_agent_launched(_ROUTE, _KEY2)
    assert route_runtime.snapshot(_ROUTE).background_only is True
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert len(_bg_only_sends(mock_bot)) == 2


async def test_quiet_preset_posts_nothing(mock_bot):
    await _make_background_only()
    with patch.object(
        status_polling.output_prefs,
        "resolve",
        return_value=SimpleNamespace(digest_card=False),
    ):
        await status_polling._maybe_post_bg_only_card(
            mock_bot, _USER, _THREAD, _WID, _ROUTE
        )
    assert _bg_only_sends(mock_bot) == []
    assert _ROUTE not in status_polling._bg_only_card_posted


async def test_waiting_outranks_no_card(mock_bot):
    """A committed 🔔 projects WAITING above the lift → no labeled-silence card
    (the decision card owns that state)."""
    await _make_background_only()
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.run_state is route_runtime.RunState.WAITING_ON_USER
    assert snap.background_only is False
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert _bg_only_sends(mock_bot) == []
    assert _ROUTE not in status_polling._bg_only_card_posted


async def test_failed_send_does_not_set_flag_and_retries(mock_bot):
    await _make_background_only()
    # A topic-shaped failure: topic_send catches it and returns (None, outcome).
    mock_bot.send_message.side_effect = BadRequest("Message thread not found")
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    # Flag NOT set — the next tick must retry.
    assert _ROUTE not in status_polling._bg_only_card_posted
    first_attempts = mock_bot.send_message.call_count
    assert first_attempts >= 1

    # Next tick: the send is retried (idempotency is the flag, not the send).
    await status_polling._maybe_post_bg_only_card(
        mock_bot, _USER, _THREAD, _WID, _ROUTE
    )
    assert mock_bot.send_message.call_count > first_attempts
    assert _ROUTE not in status_polling._bg_only_card_posted


# ── placement: a capture-SKIPPED full tick still posts ───────────────────


async def test_full_tick_posts_on_capture_skipped_tick(mock_bot):
    """The card check sits AFTER the window-gone return and BEFORE the
    capture-gating early returns, so a watchdog-skipped tick still posts."""
    await _make_background_only()
    window = MagicMock()
    window.window_id = _WID
    # Recent capture + not interactive → the tick skips the pane capture.
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
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
        mock_tmux.capture_pane = AsyncMock(return_value=None)
        await status_polling.update_status_message(
            mock_bot, user_id=_USER, window_id=_WID, thread_id=_THREAD
        )
    # capture_pane must not have been called (proves the capture-skip path)...
    mock_tmux.capture_pane.assert_not_called()
    # ...yet the labeled-silence card was still posted exactly once.
    assert len(_bg_only_sends(mock_bot)) == 1
    assert status_polling._bg_only_card_posted.get(_ROUTE) is True
