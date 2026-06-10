"""Wave C seam tests — the pre-send user-turn stamp mirrors into route_runtime.

Spec section A (v3 C3a): the EXISTING pre-send delivery stamp seams — the
``set_route_user_turn_at`` callsites in ``inbound_aggregator._send_bundle``,
``bot.forward_command_handler``, and the ``/effort`` callback — must land the
SAME ``time.time()`` value into ``route_runtime.snapshot(route).last_user_turn_at``
as into ``message_queue``'s ``_route_user_turn_at`` store. The mirror lives
INSIDE ``message_queue.set_route_user_turn_at`` (one writer, same-ts by
construction), so each seam is covered by calling the shared stamp function;
the aggregator and forward-command seams are additionally exercised
end-to-end. NOT inside ``mark_inbound_sent`` (post-send — loses the
fast-transcript race).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import inbound_aggregator, message_queue


@pytest.fixture(autouse=True)
def _reset():
    route_runtime.reset_for_tests()
    message_queue._route_user_turn_at.clear()
    yield
    route_runtime.reset_for_tests()
    message_queue._route_user_turn_at.clear()


def test_set_route_user_turn_at_mirrors_same_ts_into_route_runtime():
    """The shared stamp function writes BOTH stores with the SAME ts value."""
    message_queue.set_route_user_turn_at(1, 42, "@7")
    mq_ts = message_queue.peek_route_user_turn_at(1, 42, "@7")
    rr_ts = route_runtime.snapshot((1, 42, "@7")).last_user_turn_at
    assert mq_ts is not None
    assert rr_ts == mq_ts


@pytest.mark.asyncio
async def test_aggregator_send_bundle_stamps_route_runtime(monkeypatch):
    """End-to-end through the aggregator delivery seam."""
    from cctelegram.session import session_manager

    route = (1, 42, "@7")
    monkeypatch.setattr(
        session_manager, "send_to_window", AsyncMock(return_value=(True, "ok"))
    )
    await inbound_aggregator.aggregator_offer_text(route, "hello")
    assert await inbound_aggregator.aggregator_flush_route(route)

    mq_ts = message_queue.peek_route_user_turn_at(1, 42, "@7")
    rr_ts = route_runtime.snapshot(route).last_user_turn_at
    assert mq_ts is not None
    assert rr_ts == mq_ts


@pytest.mark.asyncio
async def test_forward_command_handler_stamps_route_runtime():
    """End-to-end through the slash-command delivery seam."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.message = MagicMock()
    update.message.text = "/model"
    update.message.message_thread_id = 42
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}

    with (
        patch("cctelegram.bot.is_user_allowed", return_value=True),
        patch("cctelegram.bot._get_thread_id", return_value=42),
        patch("cctelegram.bot.session_manager") as mock_sm,
        patch("cctelegram.bot.tmux_manager") as mock_tmux,
        patch("cctelegram.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_sm.get_display_name.return_value = "project"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        from cctelegram.bot import forward_command_handler

        await forward_command_handler(update, context)

    mq_ts = message_queue.peek_route_user_turn_at(1, 42, "@5")
    rr_ts = route_runtime.snapshot((1, 42, "@5")).last_user_turn_at
    assert mq_ts is not None
    assert rr_ts == mq_ts


def test_effort_seam_uses_the_shared_stamp_function():
    """The /effort callback imports and calls the SAME shared stamp function
    (the mirror lives inside it), so the seam inherits the same-ts guarantee."""
    from cctelegram.callback_dispatcher import effort

    assert effort.set_route_user_turn_at is message_queue.set_route_user_turn_at
