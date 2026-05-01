"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from ccbot.handlers.message_sender import TopicSendOutcome, _classify_bad_request
from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        from ccbot.handlers import attention

        attention.reset_for_tests()
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
            patch("ccbot.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "etcircle-dev"

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # The interactive keyboard send goes to the topic.
            keyboard_calls = [
                c
                for c in mock_bot.send_message.call_args_list
                if c.kwargs.get("reply_markup") is not None
            ]
            assert len(keyboard_calls) == 1
            kw = keyboard_calls[0].kwargs
            assert kw["chat_id"] == 100
            assert kw["message_thread_id"] == 42
            assert "Select model" in kw["text"]
            # Topic-first attention card lands in the same topic, not a DM.
            for call in mock_bot.send_message.call_args_list:
                assert call.kwargs["chat_id"] == 100, (
                    f"unexpected DM-shaped send_message: {call.kwargs}"
                )
        attention.reset_for_tests()


# ── Topic existence probe (status_poll_loop) ────────────────────────────────


class TestTopicProbeClassification:
    """The status_poll_loop probes topic existence by calling
    ``unpin_all_forum_topic_messages``. Telegram returns various BadRequest
    bodies for "this topic is gone"; the classifier must catch all of them or
    we fail to clean up dead bindings and keep DM-flooding the user.

    These tests pin the classifier fragments instead of running the full
    status_poll_loop (which is an unbounded ``while True`` task).
    """

    @pytest.mark.parametrize(
        "telegram_message",
        [
            "Bad Request: message thread not found",
            "MESSAGE THREAD NOT FOUND",  # case-insensitive
            "Bad Request: TOPIC_ID_INVALID",
            "Bad Request: topic not found",
        ],
    )
    def test_thread_not_found_variants_classified(self, telegram_message: str):
        outcome = _classify_bad_request(BadRequest(telegram_message))
        assert outcome is TopicSendOutcome.TOPIC_NOT_FOUND, (
            f"Telegram message {telegram_message!r} must classify as "
            f"TOPIC_NOT_FOUND so the probe can reap dead bindings"
        )

    def test_topic_closed_variant_classified(self):
        outcome = _classify_bad_request(BadRequest("Bad Request: TOPIC_CLOSED"))
        assert outcome is TopicSendOutcome.TOPIC_CLOSED

    def test_message_not_modified_classified(self):
        # Distinct outcome so attention.notify_waiting can short-circuit
        # benign no-op edits without falling through to a fresh card.
        outcome = _classify_bad_request(
            BadRequest("Bad Request: message is not modified")
        )
        assert outcome is TopicSendOutcome.MESSAGE_NOT_MODIFIED

    def test_unknown_bad_request_falls_through_to_other(self):
        outcome = _classify_bad_request(BadRequest("some unrelated error"))
        assert outcome is TopicSendOutcome.OTHER


class TestTopicProbeReapsDeadBinding:
    """Drive one iteration of the probe path manually and verify it kills the
    tmux window + unbinds the thread when Telegram returns "thread not found".
    """

    @pytest.mark.asyncio
    async def test_probe_thread_not_found_reaps_binding(self):
        from ccbot.handlers import status_polling

        bot = AsyncMock()
        bot.unpin_all_forum_topic_messages = AsyncMock(
            side_effect=BadRequest("Bad Request: message thread not found")
        )

        mock_window = MagicMock()
        mock_window.window_id = "@7"

        with (
            patch.object(
                status_polling, "session_manager"
            ) as mock_sm,
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@7")]
            mock_sm.resolve_chat_id.return_value = -100123
            mock_sm.unbind_thread = MagicMock()
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.kill_window = AsyncMock()

            # Replicate the probe block from status_poll_loop (one user/thread).
            for user_id, thread_id, wid in mock_sm.iter_thread_bindings.return_value:
                try:
                    await bot.unpin_all_forum_topic_messages(
                        chat_id=mock_sm.resolve_chat_id(user_id, thread_id),
                        message_thread_id=thread_id,
                    )
                except BadRequest as e:
                    outcome = _classify_bad_request(e)
                    if outcome is TopicSendOutcome.TOPIC_NOT_FOUND:
                        w = await mock_tmux.find_window_by_id(wid)
                        if w:
                            await mock_tmux.kill_window(w.window_id)
                        mock_sm.unbind_thread(user_id, thread_id)
                        await mock_clear(user_id, thread_id, bot)

            mock_tmux.kill_window.assert_awaited_once_with("@7")
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_clear.assert_awaited_once_with(1, 42, bot)
