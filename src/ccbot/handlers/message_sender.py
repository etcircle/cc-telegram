"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text
  - topic_send / topic_edit / topic_delete: Operation-tagged topic primitives
    that classify Telegram BadRequest errors into TopicSendOutcome and emit
    structured logs so we can tell status edits, content sends, attention
    cards, etc. apart in launchd.err.log.

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import enum
import io
import logging
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, Forbidden, RetryAfter

from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


class TopicSendOutcome(enum.Enum):
    """Classification of a single topic-targeted Telegram operation."""

    OK = "OK"
    TOPIC_NOT_FOUND = "TOPIC_NOT_FOUND"
    TOPIC_CLOSED = "TOPIC_CLOSED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    OTHER = "OTHER"


# Substring fragments that Telegram returns for various topic-related errors.
# Matched case-insensitively against ``BadRequest.message`` so future Telegram
# wording tweaks ("not found" vs "not_found") don't break the classifier.
_TOPIC_NOT_FOUND_FRAGMENTS = (
    "message thread not found",
    "topic_id_invalid",
    "topic not found",
)
_TOPIC_CLOSED_FRAGMENTS = (
    "topic_closed",
    "topic is closed",
)


def _classify_bad_request(exc: BaseException) -> TopicSendOutcome:
    """Map a Telegram exception to a TopicSendOutcome.

    Unknown ``BadRequest`` values fall through to ``OTHER`` and the original
    error message is preserved by the caller's structured log line so we can
    extend the classifier.
    """
    if isinstance(exc, RetryAfter):
        return TopicSendOutcome.RATE_LIMITED
    if isinstance(exc, Forbidden):
        return TopicSendOutcome.FORBIDDEN
    if isinstance(exc, BadRequest):
        msg = (exc.message or "").lower()
        for fragment in _TOPIC_NOT_FOUND_FRAGMENTS:
            if fragment in msg:
                return TopicSendOutcome.TOPIC_NOT_FOUND
        for fragment in _TOPIC_CLOSED_FRAGMENTS:
            if fragment in msg:
                return TopicSendOutcome.TOPIC_CLOSED
        return TopicSendOutcome.OTHER
    return TopicSendOutcome.OTHER


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await target.edit_message_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")


# ── Topic-targeted operation primitives ────────────────────────────────────


def _log_topic_outcome(
    op: str,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None,
    outcome: TopicSendOutcome,
    action: str,
    raw: BaseException | None = None,
) -> None:
    """Emit a single structured log line for a topic operation."""
    if outcome is TopicSendOutcome.OK:
        logger.info(
            "topic_%s op=%s user=%d chat=%d thread=%s window=%s outcome=%s",
            action,
            op,
            user_id,
            chat_id,
            thread_id,
            window_id,
            outcome.value,
        )
        return
    logger.warning(
        "topic_%s op=%s user=%d chat=%d thread=%s window=%s outcome=%s err=%r",
        action,
        op,
        user_id,
        chat_id,
        thread_id,
        window_id,
        outcome.value,
        str(raw) if raw is not None else "",
    )


async def topic_send(
    bot: Bot,
    *,
    op: str,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None,
    text: str,
    plain: bool = False,
    **kwargs: Any,
) -> tuple[Message | None, TopicSendOutcome]:
    """Send a message to a topic with structured outcome reporting.

    Returns the sent ``Message`` (or ``None`` on failure) and a
    ``TopicSendOutcome`` so callers can decide whether to fall back
    (DM, repair, retry). When ``plain=False`` MarkdownV2 is attempted first,
    then plain text. When ``plain=True`` (e.g. raw terminal capture for the
    interactive UI) only the plain-text path is used.
    ``RetryAfter`` is re-raised so the worker's flood-control logic still owns
    rate-limit handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if thread_id is not None:
        kwargs.setdefault("message_thread_id", thread_id)
    if plain:
        try:
            sent = await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "send"
            )
            return sent, TopicSendOutcome.OK
        except RetryAfter:
            raise
        except Exception as exc:
            outcome = _classify_bad_request(exc)
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, outcome, "send", exc
            )
            return None, outcome
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "send"
        )
        return sent, TopicSendOutcome.OK
    except RetryAfter:
        raise
    except Exception as exc:
        outcome = _classify_bad_request(exc)
        # Topic-shaped failures will not improve by stripping markdown.
        if outcome in (
            TopicSendOutcome.TOPIC_NOT_FOUND,
            TopicSendOutcome.TOPIC_CLOSED,
            TopicSendOutcome.FORBIDDEN,
        ):
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, outcome, "send", exc
            )
            return None, outcome
    try:
        sent = await bot.send_message(
            chat_id=chat_id, text=strip_sentinels(text), **kwargs
        )
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "send"
        )
        return sent, TopicSendOutcome.OK
    except RetryAfter:
        raise
    except Exception as exc:
        outcome = _classify_bad_request(exc)
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, outcome, "send", exc
        )
        return None, outcome


async def topic_edit(
    bot: Bot,
    *,
    op: str,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None,
    message_id: int,
    text: str,
    plain: bool = False,
    **kwargs: Any,
) -> TopicSendOutcome:
    """Edit a message in a topic with structured outcome reporting.

    Set ``plain=True`` when the body is raw terminal capture or other
    content that should not run through MarkdownV2 conversion.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if plain:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=strip_sentinels(text),
                **kwargs,
            )
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "edit"
            )
            return TopicSendOutcome.OK
        except RetryAfter:
            raise
        except Exception as exc:
            outcome = _classify_bad_request(exc)
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, outcome, "edit", exc
            )
            return outcome
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "edit"
        )
        return TopicSendOutcome.OK
    except RetryAfter:
        raise
    except Exception as exc:
        outcome = _classify_bad_request(exc)
        if outcome in (
            TopicSendOutcome.TOPIC_NOT_FOUND,
            TopicSendOutcome.TOPIC_CLOSED,
            TopicSendOutcome.FORBIDDEN,
        ):
            _log_topic_outcome(
                op, user_id, chat_id, thread_id, window_id, outcome, "edit", exc
            )
            return outcome
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=strip_sentinels(text),
            **kwargs,
        )
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "edit"
        )
        return TopicSendOutcome.OK
    except RetryAfter:
        raise
    except Exception as exc:
        outcome = _classify_bad_request(exc)
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, outcome, "edit", exc
        )
        return outcome


async def topic_delete(
    bot: Bot,
    *,
    op: str,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None,
    message_id: int,
) -> TopicSendOutcome:
    """Delete a message in a topic with structured outcome reporting."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, TopicSendOutcome.OK, "delete"
        )
        return TopicSendOutcome.OK
    except RetryAfter:
        raise
    except Exception as exc:
        outcome = _classify_bad_request(exc)
        _log_topic_outcome(
            op, user_id, chat_id, thread_id, window_id, outcome, "delete", exc
        )
        return outcome
