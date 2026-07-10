"""GH #41: the callback dispatcher writes ``group_chat_ids`` only AFTER the
registry recognizes the callback, so garbage / unknown callback data in a group
topic mints no mapping while a directory-browser callback (registered) still
writes it (the new-topic bootstrap)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cctelegram import callback_dispatcher
from cctelegram.callback_dispatcher import CallbackResult, DispatcherAdapters
from cctelegram.session import SessionManager

_USER_ID = 777
_CHAT_ID = -1009999999999
_THREAD_ID = 42


def _make_update(data: str) -> MagicMock:
    query = MagicMock(name="CallbackQuery")
    query.data = data
    query.message = SimpleNamespace(message_thread_id=_THREAD_ID)
    update = MagicMock(name="Update")
    update.message = None
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=_USER_ID, is_bot=False)
    update.effective_chat = SimpleNamespace(id=_CHAT_ID, type="supergroup")
    return update


def _adapters(mgr: SessionManager) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=mgr,
        tmux_manager=MagicMock(),
        bot=MagicMock(),
        route_runtime=MagicMock(),
        config=MagicMock(),
        terminal_parser=MagicMock(),
    )


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


@pytest.mark.asyncio
async def test_garbage_callback_writes_no_mapping(mgr, monkeypatch) -> None:
    # execute is short-circuited: only the pre-execute write gate is under test.
    async def _fake_execute(_authorized, _adapters):
        return CallbackResult(False)

    monkeypatch.setattr(callback_dispatcher, "execute", _fake_execute)

    update = _make_update("zz-garbage-no-prefix")
    await callback_dispatcher.dispatch_callback(
        update, MagicMock(), _adapters(mgr), is_user_allowed_func=lambda _uid: True
    )

    assert f"{_USER_ID}:{_THREAD_ID}" not in mgr.group_chat_ids


@pytest.mark.asyncio
async def test_directory_browser_callback_writes_mapping(mgr, monkeypatch) -> None:
    async def _fake_execute(_authorized, _adapters):
        return CallbackResult(True)

    monkeypatch.setattr(callback_dispatcher, "execute", _fake_execute)

    # ``db:sel:`` is a registered directory-browser prefix (the new-topic
    # bootstrap), so recognition passes and the mapping is written.
    update = _make_update("db:sel:0")
    await callback_dispatcher.dispatch_callback(
        update, MagicMock(), _adapters(mgr), is_user_allowed_func=lambda _uid: True
    )

    assert mgr.group_chat_ids.get(f"{_USER_ID}:{_THREAD_ID}") == _CHAT_ID
