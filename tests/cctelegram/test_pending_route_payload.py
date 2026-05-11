"""Regression tests for unbound-topic pending payload cleanup.

Unbound text/photo/document payloads live in ``context.user_data`` while the
user is choosing a directory, existing session, or tmux window. Cancel and
stale-picker paths must clear the whole bundle, including downloaded files, so
old media cannot be forwarded on a later bind.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import (
    CB_DIR_BIND_EXISTING,
    CB_DIR_CANCEL,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
)
from cctelegram.handlers.directory_browser import (
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
)


def _attachment(path: Path) -> bot_module.PendingAttachment:
    return bot_module.PendingAttachment(str(path), "caption", None)


def _pending_user_data(path: Path, *, thread_id: int = 10) -> dict[str, object]:
    return {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        "_pending_thread_id": thread_id,
        "_pending_thread_text": "hello",
        "_pending_thread_attachments": [_attachment(path)],
        "_selected_path": "/tmp/selected",
    }


def test_clear_pending_route_payload_deletes_cancelled_files(tmp_path: Path):
    payload = tmp_path / "cancelled.jpg"
    payload.write_bytes(b"image")
    user_data = _pending_user_data(payload)

    attachments = bot_module._clear_pending_route_payload(user_data, delete_files=True)

    assert attachments == [_attachment(payload)]
    assert not payload.exists()
    assert "_pending_thread_id" not in user_data
    assert "_pending_thread_text" not in user_data
    assert "_pending_thread_attachments" not in user_data
    assert "_selected_path" not in user_data


def test_clear_pending_route_payload_preserves_files_for_successful_flush(
    tmp_path: Path,
):
    payload = tmp_path / "flush.jpg"
    payload.write_bytes(b"image")
    user_data = _pending_user_data(payload)

    attachments = bot_module._clear_pending_route_payload(user_data, delete_files=False)

    assert attachments == [_attachment(payload)]
    assert payload.exists()
    assert "_pending_thread_attachments" not in user_data


def _make_callback_update(data: str, *, thread_id: int = 10) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_thread_id = thread_id
    query.message.chat = MagicMock()
    query.message.chat.id = -100123
    query.message.chat.type = "supergroup"

    update = MagicMock()
    update.message = None
    update.callback_query = query
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = query.message.chat
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data", [CB_DIR_CANCEL, CB_SESSION_CANCEL, CB_WIN_CANCEL]
)
async def test_picker_cancel_clears_pending_attachments_and_deletes_file(
    tmp_path: Path, callback_data: str
):
    payload = tmp_path / "pending.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    update = _make_callback_update(callback_data, thread_id=10)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock) as mock_safe_edit,
    ):
        await bot_module.callback_handler(update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    mock_safe_edit.assert_awaited_once()
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_session_picker_mismatch_clears_pending_attachments(tmp_path: Path):
    payload = tmp_path / "stale.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    update = _make_callback_update(CB_SESSION_NEW, thread_id=99)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module, "_create_and_bind_window", new_callable=AsyncMock
        ) as mock_create,
    ):
        await bot_module.callback_handler(update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    mock_create.assert_not_called()
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        f"{CB_DIR_SELECT}0",
        CB_DIR_UP,
        f"{CB_DIR_PAGE}1",
        CB_DIR_CANCEL,
        CB_DIR_BIND_EXISTING,
    ],
)
async def test_stale_directory_browser_callbacks_clear_pending_attachments(
    tmp_path: Path, callback_data: str
):
    payload = tmp_path / "stale-dir.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    update = _make_callback_update(callback_data, thread_id=99)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock) as mock_safe_edit,
    ):
        await bot_module.callback_handler(update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    mock_safe_edit.assert_not_called()
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_pending_media_is_not_forwarded_on_later_bind(tmp_path: Path):
    payload = tmp_path / "cancel-then-bind.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    cancel_update = _make_callback_update(CB_DIR_CANCEL, thread_id=10)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock),
    ):
        await bot_module.callback_handler(cancel_update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data

    context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
    context.user_data[UNBOUND_WINDOWS_KEY] = ["window-1"]
    bind_update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=10)
    window = MagicMock()
    window.window_id = "window-1"
    window.window_name = "window one"

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread"),
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        patch.object(
            bot_module,
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("window-1", "window one", "/tmp")],
        ),
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer_text,
        patch.object(
            bot_module, "aggregator_offer_attachment", new_callable=AsyncMock
        ) as mock_offer_attachment,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.callback_handler(bind_update, context)

    mock_offer_text.assert_not_called()
    mock_offer_attachment.assert_not_called()
    mock_flush.assert_not_called()
    bind_update.callback_query.answer.assert_awaited_once_with("Bound")
