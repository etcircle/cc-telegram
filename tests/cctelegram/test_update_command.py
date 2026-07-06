"""Bot-seam tests for the owner-gated ``/update`` command (``bot.update_command``).

Owner gate + wiring only — the update/restart orchestration is covered by
``test_updater.py``. The ``claude update`` subprocess is never reached here
(``run_update`` is mocked).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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


class TestUpdateCommand:
    @pytest.mark.asyncio
    async def test_non_owner_is_noop(self):
        update = _make_update(user_id=999)
        context = _make_context()
        safe_reply = AsyncMock()
        run_update = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=False),
            patch("cctelegram.bot.safe_reply", safe_reply),
            patch("cctelegram.handlers.updater.run_update", run_update),
        ):
            from cctelegram.bot import update_command

            await update_command(update, context)

        safe_reply.assert_not_called()
        run_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_message_is_noop(self):
        update = _make_update()
        update.message = None
        context = _make_context()
        run_update = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.handlers.updater.run_update", run_update),
        ):
            from cctelegram.bot import update_command

            await update_command(update, context)

        run_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_sends_status_and_runs_update(self):
        update = _make_update(user_id=1)
        context = _make_context()
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        safe_reply = AsyncMock(return_value=status_msg)
        run_update = AsyncMock()
        fake_settings = MagicMock()
        fake_settings.exists.return_value = False  # → md_settings=""
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot.safe_reply", safe_reply),
            patch("cctelegram.handlers.updater.run_update", run_update),
            patch(
                "cctelegram.md_capture.ensure_capture_settings",
                return_value=fake_settings,
            ),
        ):
            from cctelegram.bot import update_command

            await update_command(update, context)

        safe_reply.assert_awaited_once()  # the "🔄 Updating…" status message
        run_update.assert_awaited_once()
        kwargs = run_update.call_args.kwargs
        assert kwargs["claude_command"]  # config.claude_command passed through
        assert kwargs["md_settings"] == ""  # settings file absent → empty
        for key in ("report", "session_mgr", "tmux", "monitor"):
            assert key in kwargs
        # The report closure edits the status message in place.
        await kwargs["report"]("progress line")
        status_msg.edit_text.assert_awaited_once_with("progress line")
