"""Tests for /esc and /usage honest failure replies on a failed tmux send (finding 7).

The repo contract: ``TmuxManager.send_keys`` returns False on failure, never
raises. These commands previously ignored the bool — /esc replied "⎋ Sent
Escape" and /usage presented pane content even when the dispatch was lost.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"


def _make_update(user_id: int = 1, thread_id: int | None = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


def _make_tmux(
    send_results: bool | list[bool],
    pane_text: str | None = "some pane content",
) -> MagicMock:
    tmux = MagicMock()
    # Wave 3b: /esc and /usage consult the per-window send lock; a bare
    # MagicMock attribute would return a truthy ``locked()`` and trip the
    # reject-if-held branch, so hand out a real (free) asyncio.Lock.
    tmux.window_send_lock = MagicMock(return_value=asyncio.Lock())
    window = MagicMock()
    window.window_id = "@1"
    tmux.find_window_by_id = AsyncMock(return_value=window)
    if isinstance(send_results, list):
        tmux.send_keys = AsyncMock(side_effect=send_results)
    else:
        tmux.send_keys = AsyncMock(return_value=send_results)
    tmux.capture_pane = AsyncMock(return_value=pane_text)
    return tmux


class TestEscCommand:
    @pytest.mark.asyncio
    async def test_failed_send_replies_failure_not_sent_escape(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=False)
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import esc_command

            await esc_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == SEND_FAILED_TEXT
        assert "Sent Escape" not in args[1]

    @pytest.mark.asyncio
    async def test_successful_send_replies_sent_escape(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True)
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import esc_command

            await esc_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == "⎋ Sent Escape"


class TestUsageCommand:
    @pytest.mark.asyncio
    async def test_failed_usage_send_skips_capture_and_replies_failure(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=False)
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import usage_command

            await usage_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == SEND_FAILED_TEXT
        # The dependent follow-up (pane capture) must be skipped.
        tmux.capture_pane.assert_not_called()
        # Only the /usage send happened; no dismiss-Escape after a failed send.
        assert tmux.send_keys.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_dismiss_escape_replies_failure_not_usage_output(self):
        update = _make_update()
        context = _make_context()
        # /usage send succeeds, modal-dismiss Escape fails.
        tmux = _make_tmux(send_results=[True, False], pane_text="raw usage pane")
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import usage_command

            await usage_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == SEND_FAILED_TEXT
        assert "raw usage pane" not in args[1]

    @pytest.mark.asyncio
    async def test_successful_sends_present_usage_output(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True, pane_text="raw usage pane")
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import usage_command

            await usage_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert "raw usage pane" in args[1]
        assert tmux.send_keys.await_count == 2


class TestCostCommand:
    """`/cost` is intercepted bot-side (alias of /usage) — same overlay scaffold."""

    @pytest.mark.asyncio
    async def test_cost_sends_slash_cost_and_always_dismisses(self):
        """/cost sends the "/cost" overlay command then an Escape to dismiss it."""
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True, pane_text="raw cost pane")
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import cost_command

            await cost_command(update, context)

        # First key is "/cost" (NOT "/usage"); second is the dismiss Escape.
        calls = tmux.send_keys.await_args_list
        assert calls[0].args[1] == "/cost"
        assert calls[1].args[1] == "Escape"
        assert tmux.send_keys.await_count == 2
        safe_reply_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cost_parse_miss_fails_open_with_note_and_raw(self):
        """A pane that doesn't parse still gets dismissed + a fail-open reply."""
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True, pane_text="unparseable cost pane")
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import cost_command

            await cost_command(update, context)

        # The overlay was still dismissed (Escape sent) even on a parse miss.
        assert tmux.send_keys.await_args_list[1].args[1] == "Escape"
        args, _ = safe_reply_mock.call_args
        assert "Couldn't parse the cost screen" in args[1]
        assert "unparseable cost pane" in args[1]

    @pytest.mark.asyncio
    async def test_cost_parses_real_overlay_fixture(self):
        """A real 2.1.206 overlay capture is parsed to readable body lines."""
        from pathlib import Path

        fixture = (
            Path(__file__).parent / "fixtures" / "cost_overlay_live_v2.1.206.txt"
        ).read_text()
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True, pane_text=fixture)
        safe_reply_mock = AsyncMock()

        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import cost_command

            await cost_command(update, context)

        args, _ = safe_reply_mock.call_args
        assert "Total cost:" in args[1]
        assert "56% used" in args[1]
        # A clean parse does NOT prepend the fail-open note.
        assert "Couldn't parse" not in args[1]
