"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


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
    from ccbot.handlers import attention
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard.

        Topic-first attention card also fires (in the same chat/thread, not as
        a DM). We assert: (a) the keyboard message lands in the topic with the
        nav keyboard, and (b) no send goes to the user_id-as-chat (i.e. no DM).
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("ccbot.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "etcircle-dev"

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True

        keyboard_calls = [
            c
            for c in mock_bot.send_message.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        assert len(keyboard_calls) == 1
        kw = keyboard_calls[0].kwargs
        assert kw["chat_id"] == 100
        assert kw["message_thread_id"] == 42

        # No DM: every send_message went to chat_id=100 (the topic).
        for call in mock_bot.send_message.call_args_list:
            assert call.kwargs["chat_id"] == 100, (
                f"unexpected DM-shaped send_message: {call.kwargs}"
            )

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)
