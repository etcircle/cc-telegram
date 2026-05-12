"""Tests for the §2.9 attention-button callback handler in bot.py.

These exercise ``attention_callback_handler`` end-to-end:
  - Token resolution (valid → route, missing → "expired" alert).
  - Authorization (clicker user_id must match the route's user_id).
  - Verb dispatch (``yes``/``no`` → aggregator; ``type`` → no-send).
  - Idempotency (a second click on the same token gets the "expired" alert).

The handler is heavily I/O-bound on Telegram update objects, so we mock
``update.callback_query``, ``query.message``, and ``query.from_user``
explicitly rather than constructing real ``Update`` instances.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import attention
from cctelegram.handlers.message_sender import TopicSendOutcome


@pytest.fixture
def _reset_attention():
    attention.reset_for_tests()
    yield
    attention.reset_for_tests()


@pytest.fixture(autouse=True)
def _current_attention_route():
    """Default handler tests to a still-current topic→window binding."""
    with patch.object(
        bot_module.session_manager, "resolve_window_for_thread", return_value="@0"
    ):
        yield


def _make_query(
    *,
    callback_data: str,
    from_user_id: int,
    chat_id: int = -100123,
    thread_id: int = 10,
    message_text: str = '🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"',
) -> MagicMock:
    """Build a mock callback_query with the bits the handler reads."""
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = from_user_id
    query.message = MagicMock()
    query.message.text = message_text
    query.message.chat = MagicMock()
    query.message.chat.id = chat_id
    query.message.message_thread_id = thread_id
    return query


def _make_update(query: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    return update


def _register_token(
    route: tuple[int, int, str],
    *,
    chat_id: int = -100123,
    session_id: str | None = None,
    rendered_text: str = '🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"',
    parse_mode: str | None = "MarkdownV2",
    kind: str = "end_of_turn_question",
) -> str:
    """Mint a token and bind it to ``route``, mimicking notify_waiting."""
    import time as _time

    token = attention._make_attention_callback_token()
    key = (route[0], route[1] or 0)
    generation = attention._bump_attention_generation(key)
    fingerprint = attention._fingerprint(route[2], kind, rendered_text)
    if session_id is None:
        session_id = attention.session_id_for_window(route[2])
    attention._attention_state[key] = attention.AttentionState(
        message_id=555,
        window_id=route[2],
        last_fingerprint=fingerprint,
        generation=generation,
        state="waiting",
        last_send_at=_time.monotonic(),
        kind=kind,
    )
    attention._attention_callback_routes[token] = attention._AttentionCallbackEntry(
        route=route,
        chat_id=chat_id,
        thread_id=route[1],
        window_id=route[2],
        session_id=session_id,
        fingerprint=fingerprint,
        generation=generation,
        created_at=_time.monotonic(),
        rendered_text=rendered_text,
        parse_mode=parse_mode,
    )
    return token


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_unauthorized_rejected():
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=999,  # different from route[0]
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        query.answer.assert_awaited_once()
        args, kwargs = query.answer.await_args
        # First positional is the alert text; show_alert kwarg.
        text = args[0] if args else kwargs.get("text")
        assert text == "Not your session."
        assert kwargs.get("show_alert") is True
        mock_offer.assert_not_called()
        mock_flush.assert_not_called()
        query.edit_message_text.assert_not_called()
        # Bug 3 / route mismatch: the token is re-bound for the legitimate
        # owner so the rightful user can still redeem it.
        assert token in attention._attention_callback_routes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_expired_token_rejected():
    # No token registered — the lookup must miss.
    query = _make_query(
        callback_data="attn:yes:missing-token",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        query.answer.assert_awaited_once()
        args, kwargs = query.answer.await_args
        text = args[0] if args else kwargs.get("text")
        assert text == "Already answered or expired."
        assert kwargs.get("show_alert") is True
        mock_offer.assert_not_called()
        mock_flush.assert_not_called()
        query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_yes_sends_yes_via_aggregator():
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        mock_offer.assert_awaited_once_with(route, "yes")
        mock_flush.assert_awaited_once_with(route)
        query.edit_message_text.assert_awaited_once()
        edit_args, edit_kwargs = query.edit_message_text.await_args
        new_text = edit_args[0] if edit_args else edit_kwargs.get("text")
        assert "✅ Replied: yes" in new_text
        assert edit_kwargs.get("reply_markup") is None
        # Final ack on the query so Telegram drops the click spinner.
        query.answer.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_no_sends_no():
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:no:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        mock_offer.assert_awaited_once_with(route, "no")
        mock_flush.assert_awaited_once_with(route)
        query.edit_message_text.assert_awaited_once()
        edit_args, edit_kwargs = query.edit_message_text.await_args
        new_text = edit_args[0] if edit_args else edit_kwargs.get("text")
        assert "❌ Replied: no" in new_text
        assert edit_kwargs.get("reply_markup") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_type_does_not_send():
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:type:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        # ``type`` is purely a UI cue — no aggregator activity.
        mock_offer.assert_not_called()
        mock_flush.assert_not_called()
        query.edit_message_text.assert_awaited_once()
        edit_args, edit_kwargs = query.edit_message_text.await_args
        new_text = edit_args[0] if edit_args else edit_kwargs.get("text")
        assert "💬 Reply in chat" in new_text
        assert edit_kwargs.get("reply_markup") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_idempotent_second_click():
    route = (1, 10, "@0")
    token = _register_token(route)

    first_query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    first_update = _make_update(first_query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(first_update, MagicMock())
        assert mock_offer.await_count == 1
        assert mock_flush.await_count == 1

        # Second click on the same token: must short-circuit with the
        # "already answered or expired" alert and produce no extra
        # aggregator activity.
        second_query = _make_query(
            callback_data=f"attn:yes:{token}",
            from_user_id=1,
        )
        second_update = _make_update(second_query)
        await bot_module.attention_callback_handler(second_update, MagicMock())

        # No additional aggregator calls.
        assert mock_offer.await_count == 1
        assert mock_flush.await_count == 1
        # Alert raised on second click.
        second_query.answer.assert_awaited_once()
        args, kwargs = second_query.answer.await_args
        text = args[0] if args else kwargs.get("text")
        assert text == "Already answered or expired."
        assert kwargs.get("show_alert") is True
        # No edit on second click — the card was already updated by the first.
        second_query.edit_message_text.assert_not_called()


# ── Bug fix regression tests ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_rejects_topic_rebound_without_rebinding():
    """Stale token must not inject yes/no after the topic is rebound."""
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(callback_data=f"attn:yes:{token}", from_user_id=1)
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value="@1"
        ),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert (args[0] if args else kwargs.get("text")) == "This attention card is stale."
    assert kwargs.get("show_alert") is True
    mock_offer.assert_not_called()
    mock_flush.assert_not_called()
    assert token not in attention._attention_callback_routes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_rejects_wrong_chat_thread_without_rebinding():
    """Callback must originate from the chat/topic captured in the card token."""
    route = (1, 10, "@0")
    token = _register_token(route, chat_id=-100123)

    query = _make_query(
        callback_data=f"attn:no:{token}",
        from_user_id=1,
        chat_id=-100999,
        thread_id=10,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert (args[0] if args else kwargs.get("text")) == "This attention card is stale."
    assert kwargs.get("show_alert") is True
    mock_offer.assert_not_called()
    mock_flush.assert_not_called()
    assert token not in attention._attention_callback_routes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_rejects_wrong_thread_without_rebinding():
    """Callback from the right chat but wrong topic must not redeem the token."""
    route = (1, 10, "@0")
    token = _register_token(route, chat_id=-100123)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
        chat_id=-100123,
        thread_id=99,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert (args[0] if args else kwargs.get("text")) == "This attention card is stale."
    assert kwargs.get("show_alert") is True
    mock_offer.assert_not_called()
    mock_flush.assert_not_called()
    assert token not in attention._attention_callback_routes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_attention_callback_rejects_session_generation_change():
    """Session drift after /clear/restart revokes the old button token."""
    route = (1, 10, "@0")
    token = _register_token(route, session_id="old-session")

    query = _make_query(callback_data=f"attn:yes:{token}", from_user_id=1)
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module, "session_id_for_window", return_value="new-session"),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert (args[0] if args else kwargs.get("text")) == "This attention card is stale."
    assert kwargs.get("show_alert") is True
    mock_offer.assert_not_called()
    mock_flush.assert_not_called()
    assert token not in attention._attention_callback_routes


def test_attention_clear_revokes_route_tokens(_reset_attention):
    """Route teardown must revoke old buttons, not just hide card state."""
    kept = _register_token((1, 11, "@0"))
    revoked_a = _register_token((1, 10, "@0"))
    revoked_b = _register_token((1, 10, "@1"))

    attention.clear(1, 10)

    assert kept in attention._attention_callback_routes
    assert revoked_a not in attention._attention_callback_routes
    assert revoked_b not in attention._attention_callback_routes


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_user_text_revokes_old_yes_token_before_first_awaited_work():
    """Bound-topic text expires old yes/no buttons before a delayed callback."""
    old_token = _register_token((1, 10, "@0"))

    message = MagicMock()
    message.text = "typed reply"
    message.message_thread_id = 10
    message.message_id = 500
    message.chat = MagicMock()
    message.chat.send_action = AsyncMock()
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    update.message = message
    update.callback_query = None
    update.effective_chat = MagicMock(type="supergroup", id=-100123)
    context = MagicMock()
    context.bot = MagicMock()
    context.user_data = {}
    window = MagicMock()
    window.window_id = "@0"

    first_await_entered = asyncio.Event()
    allow_text_handler_to_continue = asyncio.Event()

    async def _pause_reply_context(
        _message: object,
        _user_id: int,
        _thread_id: int | None,
        text: str,
    ) -> str:
        first_await_entered.set()
        await allow_text_handler_to_continue.wait()
        return text

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "_apply_reply_context", side_effect=_pause_reply_context
        ),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value="@0"
        ),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value="@0"
        ),
        patch.object(
            bot_module.tmux_manager, "find_window_by_id", new_callable=AsyncMock
        ) as find_window,
        patch.object(
            bot_module.tmux_manager, "capture_pane", new_callable=AsyncMock
        ) as capture,
        patch.object(bot_module, "enqueue_status_update", new_callable=AsyncMock),
        patch.object(bot_module, "set_route_last_user_message"),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as flush,
        patch.object(
            bot_module.attention, "dismiss", new_callable=AsyncMock
        ) as dismiss,
        patch.object(bot_module, "get_interactive_window", return_value=None),
    ):
        find_window.return_value = window
        capture.return_value = ""

        text_task = asyncio.create_task(bot_module.text_handler(update, context))
        try:
            await asyncio.wait_for(first_await_entered.wait(), timeout=1.0)

            # The old token must already be gone while text_handler is paused
            # in its first awaited operation, before aggregator work or dismiss.
            assert old_token not in attention._attention_callback_routes
            offer.assert_not_called()
            flush.assert_not_called()
            dismiss.assert_not_called()

            delayed_query = _make_query(
                callback_data=f"attn:yes:{old_token}",
                from_user_id=1,
            )
            await bot_module.attention_callback_handler(
                _make_update(delayed_query), context
            )

            delayed_query.answer.assert_awaited_once()
            args, kwargs = delayed_query.answer.await_args
            assert (
                args[0] if args else kwargs.get("text")
            ) == "Already answered or expired."
            assert kwargs.get("show_alert") is True
            delayed_query.edit_message_text.assert_not_called()
            offer.assert_not_called()
            flush.assert_not_called()
            dismiss.assert_not_called()

            allow_text_handler_to_continue.set()
            await text_task
        finally:
            if not text_task.done():
                text_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await text_task

        offer.assert_awaited_once_with((1, 10, "@0"), "typed reply")
        flush.assert_not_called()
        dismiss.assert_awaited_once_with(context.bot, user_id=1, thread_id=10)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_aggregator_failure_rebinds_token_and_alerts():
    """Bug 3: aggregator failure must re-bind the token so the user can retry."""
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    async def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated")

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module, "aggregator_offer_text", side_effect=_raise),
        patch.object(bot_module, "aggregator_flush_route", new_callable=AsyncMock),
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    # Token re-bound — a second consume must succeed against the same entry.
    entry = attention.consume_attention_token(token)
    assert entry is not None
    assert entry.route == route

    # Error alert surfaced; card NOT edited (so the buttons stay usable for
    # a retry).
    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    alert_text = args[0] if args else kwargs.get("text")
    assert (
        "try again" in (alert_text or "").lower()
        or "couldn't" in (alert_text or "").lower()
    )
    assert kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_inflight_delivery_failure_after_dismiss_does_not_rebind_token():
    """A consumed token cannot be resurrected after direct dismiss invalidates it."""
    route = (1, 10, "@0")
    token = _register_token(route)
    query = _make_query(callback_data=f"attn:yes:{token}", from_user_id=1)

    offer_entered = asyncio.Event()
    allow_offer_to_fail = asyncio.Event()

    async def _pause_then_raise(*_args: object, **_kwargs: object) -> None:
        offer_entered.set()
        await allow_offer_to_fail.wait()
        raise RuntimeError("simulated delivery failure")

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", side_effect=_pause_then_raise
        ),
        patch.object(bot_module, "aggregator_flush_route", new_callable=AsyncMock),
        patch.object(
            attention.session_manager, "resolve_chat_id", return_value=-100123
        ),
        patch.object(attention, "topic_edit", new_callable=AsyncMock) as mock_edit,
    ):
        mock_edit.return_value = TopicSendOutcome.OK
        task = asyncio.create_task(
            bot_module.attention_callback_handler(_make_update(query), MagicMock())
        )
        try:
            await asyncio.wait_for(offer_entered.wait(), timeout=1.0)
            assert token not in attention._attention_callback_routes

            await attention.dismiss(MagicMock(), user_id=1, thread_id=10)
            allow_offer_to_fail.set()
            await task
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert token not in attention._attention_callback_routes
    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert "try again" in ((args[0] if args else kwargs.get("text")) or "").lower()
    assert kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_inflight_delivery_failure_after_typed_reply_revocation_does_not_rebind_token():
    """Typed-reply pre-await revocation invalidates consumed in-flight tokens."""
    route = (1, 10, "@0")
    token = _register_token(route)
    query = _make_query(callback_data=f"attn:no:{token}", from_user_id=1)

    offer_entered = asyncio.Event()
    allow_offer_to_fail = asyncio.Event()

    async def _pause_then_raise(*_args: object, **_kwargs: object) -> None:
        offer_entered.set()
        await allow_offer_to_fail.wait()
        raise RuntimeError("simulated delivery failure")

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", side_effect=_pause_then_raise
        ),
        patch.object(bot_module, "aggregator_flush_route", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(
            bot_module.attention_callback_handler(_make_update(query), MagicMock())
        )
        try:
            await asyncio.wait_for(offer_entered.wait(), timeout=1.0)
            assert token not in attention._attention_callback_routes

            attention.revoke_attention_tokens(user_id=1, thread_id=10, window_id="@0")
            allow_offer_to_fail.set()
            await task
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert token not in attention._attention_callback_routes
    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert "try again" in ((args[0] if args else kwargs.get("text")) or "").lower()
    assert kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_inflight_delivery_failure_after_replacement_does_not_rebind_old_token():
    """Replacing the waiting card with a new prompt invalidates old consumed tokens."""
    route = (1, 10, "@0")
    old_prompt = '🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"'
    token = _register_token(route, rendered_text=old_prompt)
    query = _make_query(callback_data=f"attn:yes:{token}", from_user_id=1)

    offer_entered = asyncio.Event()
    allow_offer_to_fail = asyncio.Event()

    async def _pause_then_raise(*_args: object, **_kwargs: object) -> None:
        offer_entered.set()
        await allow_offer_to_fail.wait()
        raise RuntimeError("simulated delivery failure")

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(
            bot_module, "aggregator_offer_text", side_effect=_pause_then_raise
        ),
        patch.object(bot_module, "aggregator_flush_route", new_callable=AsyncMock),
        patch.object(
            attention.session_manager, "resolve_chat_id", return_value=-100123
        ),
        patch.object(
            attention.session_manager, "get_display_name", return_value="cctelegram"
        ),
        patch.object(attention, "topic_edit", new_callable=AsyncMock) as mock_edit,
    ):
        mock_edit.return_value = TopicSendOutcome.OK
        task = asyncio.create_task(
            bot_module.attention_callback_handler(_make_update(query), MagicMock())
        )
        try:
            await asyncio.wait_for(offer_entered.wait(), timeout=1.0)
            assert token not in attention._attention_callback_routes

            outcome = await attention.notify_waiting(
                MagicMock(),
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text='🔔 Awaiting your reply — cc-telegram\n"Different prompt?"',
                kind="end_of_turn_question",
            )
            assert outcome is TopicSendOutcome.OK

            allow_offer_to_fail.set()
            await task
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert token not in attention._attention_callback_routes
    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert "try again" in ((args[0] if args else kwargs.get("text")) or "").lower()
    assert kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_send_to_window_false_rebinds_token_and_does_not_mark_replied():
    """Item 3: swallowed tmux send failure must not become a Replied card."""
    from cctelegram.handlers import inbound_aggregator

    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    update = _make_update(query)
    captured_sends: list[tuple[str, str]] = []

    async def _send_failed(window_id: str, text: str) -> tuple[bool, str]:
        captured_sends.append((window_id, text))
        return False, "Window not found"

    inbound_aggregator._route_pending.clear()
    inbound_aggregator._route_locks.clear()
    try:
        with (
            patch.object(bot_module, "is_user_allowed", return_value=True),
            patch.object(
                inbound_aggregator.session_manager,
                "send_to_window",
                side_effect=_send_failed,
            ),
        ):
            await bot_module.attention_callback_handler(update, MagicMock())
    finally:
        inbound_aggregator._route_pending.clear()
        inbound_aggregator._route_locks.clear()

    assert captured_sends == [("@0", "yes")]

    # Token re-bound — the same button remains retryable instead of becoming
    # an expired token after a delivery failure below the aggregator.
    entry = attention.consume_attention_token(token)
    assert entry is not None
    assert entry.route == route

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    alert_text = args[0] if args else kwargs.get("text")
    assert "couldn't deliver" in (alert_text or "").lower()
    assert "try again" in (alert_text or "").lower()
    assert kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_revoked_user_rejected_before_consume():
    """Item 4: revoked user (not in allow-list) must be rejected before consume."""
    route = (1, 10, "@0")
    token = _register_token(route)

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=False),
        patch.object(
            bot_module, "aggregator_offer_text", new_callable=AsyncMock
        ) as mock_offer,
        patch.object(
            bot_module, "aggregator_flush_route", new_callable=AsyncMock
        ) as mock_flush,
        patch.object(
            attention,
            "consume_attention_token",
            wraps=attention.consume_attention_token,
        ) as mock_consume,
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

        # "Not authorized." alert surfaced.
        query.answer.assert_awaited_once()
        args, kwargs = query.answer.await_args
        alert_text = args[0] if args else kwargs.get("text")
        assert alert_text == "Not authorized."
        assert kwargs.get("show_alert") is True

        # consume_attention_token must NOT be called — the token is still
        # in the map and remains redeemable for legitimate re-grants.
        mock_consume.assert_not_called()
        assert token in attention._attention_callback_routes

        mock_offer.assert_not_called()
        mock_flush.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention")
async def test_card_edit_preserves_parse_mode_and_original_body():
    """Bug 1: edit re-uses the stashed rendered_text + parse_mode verbatim."""
    route = (1, 10, "@0")
    rendered = "🔔 *Test* — `code`"
    token = _register_token(route, rendered_text=rendered, parse_mode="MarkdownV2")

    query = _make_query(
        callback_data=f"attn:yes:{token}",
        from_user_id=1,
    )
    update = _make_update(query)

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module, "aggregator_offer_text", new_callable=AsyncMock),
        patch.object(bot_module, "aggregator_flush_route", new_callable=AsyncMock),
    ):
        await bot_module.attention_callback_handler(update, MagicMock())

    query.edit_message_text.assert_awaited_once()
    edit_args, edit_kwargs = query.edit_message_text.await_args
    new_text = edit_args[0] if edit_args else edit_kwargs.get("text")
    # Original body preserved verbatim — including MarkdownV2 markers.
    assert rendered in new_text
    assert "✅ Replied: yes" in new_text
    # Parse mode round-tripped from the entry.
    assert edit_kwargs.get("parse_mode") == "MarkdownV2"
    assert edit_kwargs.get("reply_markup") is None


@pytest.fixture
def _mock_session_manager():
    """Patch session_manager used inside attention.notify_waiting."""
    with patch("cctelegram.handlers.attention.session_manager") as sm:
        sm.resolve_chat_id.return_value = -100123
        sm.get_display_name.return_value = "cctelegram"
        yield sm


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention", "_mock_session_manager")
async def test_token_registered_before_topic_send_returns():
    """Bug 2: token must be in the map before topic_send is awaited."""
    from cctelegram.handlers import attention as attention_module
    from cctelegram.handlers.message_sender import TopicSendOutcome

    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 42

    # Capture whether the token was registered at the moment topic_send is
    # awaited. We cannot read the freshly-minted token name from inside
    # topic_send, so we just record the registry size at await time.
    seen_tokens: list[str] = []

    async def _topic_send_spy(*_args: object, **_kwargs: object):
        # At this point the entry must already be present in the map.
        seen_tokens.extend(attention_module._attention_callback_routes.keys())
        return sent_msg, TopicSendOutcome.OK

    with (
        patch.object(attention_module, "topic_send", side_effect=_topic_send_spy),
        patch.object(
            attention_module,
            "session_id_for_window",
            return_value="sess",
        ),
    ):
        await attention_module.notify_waiting(
            bot,
            user_id=1,
            thread_id=10,
            window_id="@0",
            prompt_text='🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"',
            kind="end_of_turn_question",
        )

    # The token registered for this notify must already be present when
    # topic_send started executing.
    assert len(seen_tokens) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention", "_mock_session_manager")
async def test_send_failure_pops_orphan_token():
    """Bug 2: a topic_send that returns ``(None, ...)`` must release the token."""
    from cctelegram.handlers import attention as attention_module
    from cctelegram.handlers.message_sender import TopicSendOutcome

    bot = MagicMock()

    async def _topic_send_failed(*_args: object, **_kwargs: object):
        return None, TopicSendOutcome.TOPIC_NOT_FOUND

    with (
        patch.object(attention_module, "topic_send", side_effect=_topic_send_failed),
        patch.object(
            attention_module,
            "session_id_for_window",
            return_value="sess",
        ),
    ):
        outcome = await attention_module.notify_waiting(
            bot,
            user_id=1,
            thread_id=10,
            window_id="@0",
            prompt_text='🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"',
            kind="end_of_turn_question",
        )

    assert outcome is TopicSendOutcome.TOPIC_NOT_FOUND
    # No orphan tokens left behind.
    assert attention_module._attention_callback_routes == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_attention", "_mock_session_manager")
async def test_send_raise_pops_orphan_token():
    """Bug 2: a topic_send that raises must release the pre-registered token."""
    from cctelegram.handlers import attention as attention_module

    bot = MagicMock()

    async def _topic_send_raise(*_args: object, **_kwargs: object):
        raise RuntimeError("network blew up")

    with (
        patch.object(attention_module, "topic_send", side_effect=_topic_send_raise),
        patch.object(
            attention_module,
            "session_id_for_window",
            return_value="sess",
        ),
        pytest.raises(RuntimeError),
    ):
        await attention_module.notify_waiting(
            bot,
            user_id=1,
            thread_id=10,
            window_id="@0",
            prompt_text='🔔 Awaiting your reply — cc-telegram\n"Want me to do X?"',
            kind="end_of_turn_question",
        )

    assert attention_module._attention_callback_routes == {}
