"""GH #55: classify the MarkdownV2→plain fallback on the SEND paths.

The plain re-send exists for exactly ONE failure class — the formatted
content was provably NOT delivered: either the MarkdownV2 conversion raised
(pre-network) or Telegram returned ``BadRequest`` (server rejection). Every
other class — ``TimedOut`` / ``NetworkError`` (delivery ambiguous), ``Forbidden``
/ unknown (would fail identically) — must NEVER re-send content, or a
client-side timeout that Telegram actually delivered mints the owner-observed
duplicate.

PTB 22.7 hierarchy trap (pinned): ``BadRequest`` (and ``TimedOut``) subclass
``NetworkError``, so the eligibility test is a positive ``isinstance``-
``BadRequest`` test, never a NetworkError-family test.

The EDIT paths (``safe_edit`` / ``topic_edit``) are OUT OF SCOPE and
byte-untouched — an edit can never mint a second message.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from cctelegram.handlers import message_sender
from cctelegram.handlers.message_sender import (
    TopicSendOutcome,
    _plain_fallback_eligible,
    safe_reply,
    safe_send,
    send_with_fallback,
    topic_send,
)

PARSE_ERROR = BadRequest("Can't parse entities: unexpected end of tag at byte 12")


# ── Helper-level classifier ─────────────────────────────────────────────────


class TestPlainFallbackEligible:
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(BadRequest("Can't parse entities: ..."), id="parse_error"),
            pytest.param(BadRequest("message is not modified"), id="not_modified"),
            pytest.param(BadRequest("message to edit not found"), id="not_found"),
        ],
    )
    def test_bad_request_is_eligible(self, exc: BaseException):
        assert _plain_fallback_eligible(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(TimedOut(), id="timed_out"),
            pytest.param(NetworkError("boom"), id="network_error"),
            pytest.param(Forbidden("blocked"), id="forbidden"),
            pytest.param(RuntimeError("weird"), id="runtime_error"),
        ],
    )
    def test_transient_and_definitive_are_ineligible(self, exc: BaseException):
        # NetworkError pins the PTB hierarchy trap: BadRequest subclasses it,
        # yet a bare NetworkError must be ineligible.
        assert _plain_fallback_eligible(exc) is False


# ── send_with_fallback ──────────────────────────────────────────────────────


class TestSendWithFallback:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [TimedOut(), NetworkError("net"), Forbidden("no"), RuntimeError("x")],
        ids=["timed_out", "network_error", "forbidden", "runtime"],
    )
    async def test_ineligible_does_not_re_send(self, exc: BaseException):
        bot = AsyncMock()
        bot.send_message.side_effect = exc
        result = await send_with_fallback(bot, 42, "**hi**")
        assert result is None
        assert bot.send_message.call_count == 1  # NO plain re-send

    @pytest.mark.asyncio
    async def test_bad_request_fires_plain_fallback(self):
        bot = AsyncMock()
        sent = MagicMock()
        bot.send_message.side_effect = [PARSE_ERROR, sent]
        result = await send_with_fallback(bot, 42, "**hi**")
        assert result is sent
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_conversion_raise_goes_straight_to_plain(self):
        bot = AsyncMock()
        sent = MagicMock()
        bot.send_message.return_value = sent
        with patch.object(
            message_sender, "_ensure_formatted", side_effect=ValueError("bad md")
        ):
            result = await send_with_fallback(bot, 42, "**hi**")
        assert result is sent
        assert bot.send_message.call_count == 1  # plain is the FIRST attempt
        assert bot.send_message.call_args.kwargs.get("parse_mode") is None

    @pytest.mark.asyncio
    async def test_retry_after_re_raised_on_formatted_attempt(self):
        bot = AsyncMock()
        bot.send_message.side_effect = RetryAfter(timedelta(seconds=1))
        with pytest.raises(RetryAfter):
            await send_with_fallback(bot, 42, "**hi**")
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_after_re_raised_on_plain_attempt(self):
        bot = AsyncMock()
        bot.send_message.side_effect = [PARSE_ERROR, RetryAfter(timedelta(seconds=1))]
        with pytest.raises(RetryAfter):
            await send_with_fallback(bot, 42, "**hi**")
        assert bot.send_message.call_count == 2


# ── safe_send ───────────────────────────────────────────────────────────────


class TestSafeSend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [TimedOut(), NetworkError("net")],
        ids=["timed_out", "network_error"],
    )
    async def test_ineligible_does_not_re_send(self, exc: BaseException):
        bot = AsyncMock()
        bot.send_message.side_effect = exc
        await safe_send(bot, 42, "**hi**")
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_bad_request_fires_plain_fallback(self):
        bot = AsyncMock()
        bot.send_message.side_effect = [PARSE_ERROR, MagicMock()]
        await safe_send(bot, 42, "**hi**")
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_conversion_raise_goes_straight_to_plain(self):
        bot = AsyncMock()
        with patch.object(
            message_sender, "_ensure_formatted", side_effect=ValueError("bad md")
        ):
            await safe_send(bot, 42, "**hi**")
        assert bot.send_message.call_count == 1
        assert bot.send_message.call_args.kwargs.get("parse_mode") is None

    @pytest.mark.asyncio
    async def test_retry_after_re_raised_both_attempts(self):
        bot = AsyncMock()
        bot.send_message.side_effect = RetryAfter(timedelta(seconds=1))
        with pytest.raises(RetryAfter):
            await safe_send(bot, 42, "**hi**")
        assert bot.send_message.call_count == 1

        bot2 = AsyncMock()
        bot2.send_message.side_effect = [PARSE_ERROR, RetryAfter(timedelta(seconds=1))]
        with pytest.raises(RetryAfter):
            await safe_send(bot2, 42, "**hi**")
        assert bot2.send_message.call_count == 2


# ── safe_reply (terminal path RAISES) ───────────────────────────────────────


class TestSafeReply:
    @pytest.mark.asyncio
    async def test_timed_out_is_re_raised_exactly_one_call(self):
        message = MagicMock()
        message.reply_text = AsyncMock(side_effect=TimedOut())
        with pytest.raises(TimedOut):
            await safe_reply(message, "**hi**")
        assert message.reply_text.call_count == 1  # NO plain re-send

    @pytest.mark.asyncio
    async def test_network_error_is_re_raised_exactly_one_call(self):
        message = MagicMock()
        message.reply_text = AsyncMock(side_effect=NetworkError("net"))
        with pytest.raises(NetworkError):
            await safe_reply(message, "**hi**")
        assert message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_bad_request_fires_plain_fallback(self):
        message = MagicMock()
        sent = MagicMock()
        message.reply_text = AsyncMock(side_effect=[PARSE_ERROR, sent])
        result = await safe_reply(message, "**hi**")
        assert result is sent
        assert message.reply_text.call_count == 2

    @pytest.mark.asyncio
    async def test_conversion_raise_goes_straight_to_plain(self):
        message = MagicMock()
        sent = MagicMock()
        message.reply_text = AsyncMock(return_value=sent)
        with patch.object(
            message_sender, "_ensure_formatted", side_effect=ValueError("bad md")
        ):
            result = await safe_reply(message, "**hi**")
        assert result is sent
        assert message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_after_re_raised(self):
        message = MagicMock()
        message.reply_text = AsyncMock(side_effect=RetryAfter(timedelta(seconds=1)))
        with pytest.raises(RetryAfter):
            await safe_reply(message, "**hi**")
        assert message.reply_text.call_count == 1


# ── topic_send (formatted branch) ───────────────────────────────────────────


class TestTopicSend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [TimedOut(), NetworkError("net")],
        ids=["timed_out", "network_error"],
    )
    async def test_transient_returns_none_other_no_re_send(self, exc: BaseException):
        bot = AsyncMock()
        bot.send_message.side_effect = exc
        with patch.object(message_sender, "_spawn_ref_insert") as record:
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=1,
                chat_id=100,
                thread_id=7,
                window_id="@0",
                text="**hi**",
            )
        assert sent is None
        assert outcome is TopicSendOutcome.OTHER
        assert bot.send_message.call_count == 1  # NO plain re-send
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_request_fires_plain_and_records(self):
        bot = AsyncMock()
        plain_msg = MagicMock()
        plain_msg.message_id = 555
        bot.send_message.side_effect = [PARSE_ERROR, plain_msg]
        with patch.object(message_sender, "_spawn_ref_insert") as record:
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=1,
                chat_id=100,
                thread_id=7,
                window_id="@0",
                text="**hi**",
            )
        assert sent is plain_msg
        assert outcome is TopicSendOutcome.OK
        assert bot.send_message.call_count == 2
        record.assert_called_once()  # the plain send is recorded

    @pytest.mark.asyncio
    async def test_topic_shaped_early_return_preserved(self):
        bot = AsyncMock()
        bot.send_message.side_effect = BadRequest("Message thread not found")
        with patch.object(message_sender, "_spawn_ref_insert") as record:
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=1,
                chat_id=100,
                thread_id=7,
                window_id="@0",
                text="**hi**",
            )
        assert sent is None
        assert outcome is TopicSendOutcome.TOPIC_NOT_FOUND
        assert bot.send_message.call_count == 1  # no plain retry
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversion_raise_goes_straight_to_plain(self):
        bot = AsyncMock()
        plain_msg = MagicMock()
        plain_msg.message_id = 777
        bot.send_message.return_value = plain_msg
        with (
            patch.object(
                message_sender, "_ensure_formatted", side_effect=ValueError("bad md")
            ),
            patch.object(message_sender, "_spawn_ref_insert"),
        ):
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=1,
                chat_id=100,
                thread_id=7,
                window_id="@0",
                text="**hi**",
            )
        assert sent is plain_msg
        assert outcome is TopicSendOutcome.OK
        assert bot.send_message.call_count == 1
        assert bot.send_message.call_args.kwargs.get("parse_mode") is None

    @pytest.mark.asyncio
    async def test_retry_after_re_raised_both_attempts(self):
        bot = AsyncMock()
        bot.send_message.side_effect = RetryAfter(timedelta(seconds=1))
        with patch.object(message_sender, "_spawn_ref_insert"):
            with pytest.raises(RetryAfter):
                await topic_send(
                    bot,
                    op="content",
                    user_id=1,
                    chat_id=100,
                    thread_id=7,
                    window_id="@0",
                    text="**hi**",
                )
        assert bot.send_message.call_count == 1

        bot2 = AsyncMock()
        bot2.send_message.side_effect = [PARSE_ERROR, RetryAfter(timedelta(seconds=1))]
        with patch.object(message_sender, "_spawn_ref_insert"):
            with pytest.raises(RetryAfter):
                await topic_send(
                    bot2,
                    op="content",
                    user_id=1,
                    chat_id=100,
                    thread_id=7,
                    window_id="@0",
                    text="**hi**",
                )
        assert bot2.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_plain_true_branch_untouched(self):
        # plain=True has no second attempt: a transient is classified and
        # returned, never re-sent (byte-identical to before).
        bot = AsyncMock()
        bot.send_message.side_effect = TimedOut()
        with patch.object(message_sender, "_spawn_ref_insert"):
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=1,
                chat_id=100,
                thread_id=7,
                window_id="@0",
                text="raw",
                plain=True,
            )
        assert sent is None
        assert outcome is TopicSendOutcome.OTHER
        assert bot.send_message.call_count == 1


# ── Caller lane pin: the content worker never re-sends on (None, OTHER) ──────


class TestContentWorkerAtMostOnce:
    """The content lane is AT-MOST-ONCE: a transient `(None, OTHER)` from
    ``topic_send`` advances the retry-resume cursor and is never re-sent, so
    the post-fix loss is possible but a duplicate is not (GH #55, r2 caller
    audit — message_queue.py:~3098).
    """

    @pytest.mark.asyncio
    async def test_content_task_does_not_re_send_on_none_other(self):
        from cctelegram.handlers import message_queue

        message_queue.reset_for_tests()
        try:
            calls = {"n": 0}

            async def fake_send(bot, *, op, text, **kw):
                calls["n"] += 1
                return None, TopicSendOutcome.OTHER

            task = message_queue.MessageTask(
                task_type="content",
                window_id="@0",
                parts=["hello world"],
                content_type="text",
                thread_id=100,
            )
            with (
                patch.object(message_queue, "topic_send", side_effect=fake_send),
                patch.object(
                    message_queue, "_emergency_dm", new_callable=AsyncMock
                ) as emergency,
                patch.object(
                    message_queue, "_check_and_send_status", new_callable=AsyncMock
                ),
                patch.object(
                    message_queue, "_finalize_activity_digest", new_callable=AsyncMock
                ),
                patch.object(
                    message_queue, "_maybe_attention_or_dismiss", new_callable=AsyncMock
                ),
                patch.object(
                    message_queue,
                    "_convert_status_to_content",
                    AsyncMock(return_value=None),
                ),
                patch.object(
                    message_queue, "_do_clear_status_message", new_callable=AsyncMock
                ),
                patch.object(
                    message_queue.session_manager, "resolve_chat_id", return_value=100
                ),
            ):
                await message_queue._process_content_task(AsyncMock(), 1, task)

            assert calls["n"] == 1, "content worker re-sent on (None, OTHER)"
            assert task.parts_sent == 1, "retry-resume cursor did not advance"
            # OTHER is not a topic-broken outcome, so no emergency DM either.
            emergency.assert_not_called()
        finally:
            message_queue.reset_for_tests()
