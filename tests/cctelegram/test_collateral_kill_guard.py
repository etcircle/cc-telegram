"""GH #63 §2b: the unbound-window cleanup must never kill a WINNER.

A failed bring-up attempt's cleanup (``_cleanup_unbound_created_window``, reached
from the hook-timeout path AND the pending-owner-change abort) used to kill its
``created_wid`` unconditionally. The bring-up ordering is register(session_map) →
pending-owner recheck → BIND, so a winner can be BOUND — or merely REGISTERED and
already running a live Claude — during the gap before it is bound. The guard
(Codex Q1) never kills a window that is BOUND (``thread_bindings``) OR REGISTERED
(``peek_session_id_for_window`` non-None). A window that is neither is still
killed. These tests pin the guard directly plus at the abort seam that reaches it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram.session import WindowState, session_manager


@pytest.mark.asyncio
async def test_cleanup_spares_window_bound_to_a_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window bound to a topic (won the race) is NOT killed."""
    monkeypatch.setattr(session_manager, "thread_bindings", {1: {42: "@7"}})
    monkeypatch.setattr(session_manager, "window_states", {})
    tmux = MagicMock()
    tmux.kill_window = AsyncMock(return_value=True)

    result = await inbound_module._cleanup_unbound_created_window(
        "@7", "winner", tmux, reason="test"
    )

    tmux.kill_window.assert_not_called()
    # Spared → from the cleanup contract's view nothing we own is dangling.
    assert result is True


@pytest.mark.asyncio
async def test_cleanup_spares_registered_but_unbound_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Q1: a REGISTERED (live Claude) but NOT-YET-BOUND winner is spared.

    This is the §2b gap — SessionStart fired (session_map has the window) but the
    bind has not happened yet. A losing attempt's cleanup must NOT kill it.
    """
    monkeypatch.setattr(session_manager, "thread_bindings", {})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {"@8": WindowState(session_id="sid-8", cwd="/repo", window_name="live")},
    )
    tmux = MagicMock()
    tmux.kill_window = AsyncMock(return_value=True)

    result = await inbound_module._cleanup_unbound_created_window(
        "@8", "live", tmux, reason="test"
    )

    tmux.kill_window.assert_not_called()
    assert result is True


@pytest.mark.asyncio
async def test_cleanup_kills_window_neither_bound_nor_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window with no binding AND no registered session (a true loser) is killed."""
    monkeypatch.setattr(session_manager, "thread_bindings", {})
    monkeypatch.setattr(session_manager, "window_states", {})
    tmux = MagicMock()
    tmux.kill_window = AsyncMock(return_value=True)

    result = await inbound_module._cleanup_unbound_created_window(
        "@9", "loser", tmux, reason="test"
    )

    tmux.kill_window.assert_awaited_once_with("@9")
    assert result is True


@pytest.mark.asyncio
async def test_abort_after_owner_change_spares_bound_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abort seam that reaches the cleanup must also spare a bound winner."""
    monkeypatch.setattr(session_manager, "thread_bindings", {1: {42: "@7"}})
    tmux = MagicMock()
    tmux.kill_window = AsyncMock(return_value=True)

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()

    await inbound_module._abort_created_window_after_pending_owner_change(
        query,
        user_data={},  # owner gone → the abort premise
        user_id=1,
        pending_thread_id=42,
        tmux_mgr=tmux,
        created_wid="@7",
        created_wname="winner",
        resume_session_id=None,
    )

    tmux.kill_window.assert_not_called()
