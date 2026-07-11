"""Tests for forward_command_handler — command forwarding to Claude Code."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.delivery import UserTurnStamp


def _make_update(text: str, user_id: int = 1, thread_id: int = 42) -> MagicMock:
    """Build a minimal mock Update with message text in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestForwardCommand:
    @pytest.mark.asyncio
    async def test_model_sends_command_to_tmux(self):
        """/model → send_to_window called with "/model"."""
        update = _make_update("/model")
        context = _make_context()

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

            mock_sm.send_to_window.assert_called_once_with(
                "@5", "/model", user_turn=UserTurnStamp(1, 42, "@5")
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cmd",
        [
            "/memory",
            "/help",
            "/memory@botname",
            "/help extra",
            # Case-insensitive (round-1 codex P2): Claude Code's command lookup
            # is case-insensitive, so /Memory reopens the same blocking panel.
            "/Memory@MyBot",
            "/HELP@botname",
            "/MEMORY",
        ],
    )
    async def test_tui_overlay_blocklist_refuses_and_does_not_forward(self, cmd):
        """A known interceptor-less TUI panel (/memory, /help) is blocked, not forwarded."""
        update = _make_update(cmd)
        context = _make_context()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch("cctelegram.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from cctelegram.bot import forward_command_handler

            await forward_command_handler(update, context)

            # Never forwarded to tmux.
            mock_sm.send_to_window.assert_not_called()
            # A helpful "blocked" notice was sent.
            assert mock_reply.await_count == 1
            notice = mock_reply.await_args.args[1]
            assert "full-screen terminal panel" in notice
            assert "/screenshot" in notice

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["/compact", "/Compact"])
    async def test_non_blocklisted_command_still_forwards(self, cmd):
        """A command that is neither bot-owned nor blocklisted forwards normally —
        including mixed-case (the casefold must not widen the blocklist)."""
        update = _make_update(cmd)
        context = _make_context()

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

            # Forwarded verbatim (original casing preserved on the wire).
            mock_sm.send_to_window.assert_called_once_with(
                "@5", cmd, user_turn=UserTurnStamp(1, 42, "@5")
            )

    @pytest.mark.asyncio
    async def test_bot_mention_preserves_args(self):
        """/effort@botname max → forwards "/effort max" (args survive mention)."""
        update = _make_update("/effort@felixclaudecode_bot max")
        context = _make_context()

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

            mock_sm.send_to_window.assert_called_once_with(
                "@5", "/effort max", user_turn=UserTurnStamp(1, 42, "@5")
            )

    @pytest.mark.asyncio
    async def test_clear_clears_session(self):
        """/clear → send_to_window + clear_window_session."""
        update = _make_update("/clear")
        context = _make_context()

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

            mock_sm.send_to_window.assert_called_once_with(
                "@5", "/clear", user_turn=UserTurnStamp(1, 42, "@5")
            )
            mock_sm.clear_window_session.assert_called_once_with("@5")


class TestForwardCommandAbortsOnARefusedPreFlush:
    """GH #50 r2 F2(i): the §2.8 pre-flush is FORCED, and its result was ignored.

    A refused flush means the user's earlier message never reached the pane — and
    on a ``draft_written`` refusal it is still sitting UNSENT in the input box.
    Forwarding the slash command anyway would either jump the queue or be typed
    ONTO that stranded draft, whose Enter would then commit BOTH.
    """

    @pytest.mark.asyncio
    async def test_a_refused_flush_aborts_the_command(self):
        from cctelegram import delivery

        update = _make_update("/model")
        context = _make_context()
        refusal = delivery.refuse(delivery.REASON_PROMPT_PRESENT, written=False)

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch("cctelegram.bot.safe_reply", new_callable=AsyncMock) as reply,
            patch(
                "cctelegram.bot.aggregator_flush_route",
                new_callable=AsyncMock,
                return_value=refusal,
            ),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from cctelegram.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_not_called()
            body = reply.await_args.args[1]
            assert "NOT sent" in body
            assert refusal.message in body
