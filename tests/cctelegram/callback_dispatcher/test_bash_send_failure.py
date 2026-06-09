"""Tests for the screenshot quick-key callback's honest send failure (finding 7).

The quick-key path previously ignored the ``send_keys`` bool and answered with
the success key label regardless, then attempted a refresh of a pane the key
never reached.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cctelegram.callback_dispatcher import (
    DispatcherAdapters,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers.callback_data import CB_KEYS_PREFIX

SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=10)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


class FakeSessionManager:
    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return "@1"


class FakeTmuxManager:
    def __init__(self, send_ok: bool) -> None:
        self.find_window_by_id = AsyncMock(return_value=SimpleNamespace(window_id="@1"))
        self.send_keys = AsyncMock(return_value=send_ok)
        # Empty capture → the refresh path bails before image rendering.
        self.capture_pane = AsyncMock(return_value="")


def _ctx(query: FakeQuery, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        update=SimpleNamespace(
            message=None,
            callback_query=query,
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=None,
        ),
        context=SimpleNamespace(user_data={}, bot=SimpleNamespace()),
        user=SimpleNamespace(id=user_id),
        query=query,
        user_id=user_id,
        thread_id=10,
    )


def _adapters(tmux_manager: FakeTmuxManager) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=FakeSessionManager(),
        tmux_manager=tmux_manager,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None,
            mark_inbound_sent=AsyncMock(),
        ),
        config=SimpleNamespace(browse_root="."),
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda _cached_input, _pane: None
        ),
    )


async def _run_quick_key(tmux: FakeTmuxManager) -> FakeQuery:
    query = FakeQuery(f"{CB_KEYS_PREFIX}up:@1")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    with patch("asyncio.sleep", new=AsyncMock()):
        await execute(authorized, _adapters(tmux))
    return query


@pytest.mark.asyncio
async def test_failed_quick_key_send_answers_failure_and_skips_refresh() -> None:
    tmux = FakeTmuxManager(send_ok=False)

    query = await _run_quick_key(tmux)

    assert query.answers == [(SEND_FAILED_TEXT, True)]
    # The dependent refresh must be skipped.
    tmux.capture_pane.assert_not_called()


@pytest.mark.asyncio
async def test_successful_quick_key_send_answers_label_and_refreshes() -> None:
    tmux = FakeTmuxManager(send_ok=True)

    query = await _run_quick_key(tmux)

    assert query.answers == [("↑", False)]
    tmux.capture_pane.assert_called_once()
