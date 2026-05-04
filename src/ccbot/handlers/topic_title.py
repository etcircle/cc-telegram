"""Bake Claude Code's context-window size into the Telegram forum topic title.

The forum topic title is the only Telegram surface that's always visible at
the top of the chat regardless of scroll position, so it's the right home
for an "always-on" context-headroom indicator. Source of truth is
``transcript_parser.read_latest_usage`` (cached by status_polling into
``busy_indicator._context_usage``); this module just decides when to call
``bot.edit_forum_topic``.

Design:
  - The bot owns the suffix ``· Nk/Mk`` (e.g. ``· 113k/200k``) and nothing
    else. The base name stays whatever the user (or the directory-browser
    flow) put there.
  - User renames captured via ``forum_topic_edited`` strip the suffix before
    storing, so the cached base never gains a trailing ctx fragment.
  - Per-route debounce (min token delta + min interval) keeps editForumTopic
    well under Telegram's per-chat admin-action rate ceiling.
  - When usage is unknown (no JSONL yet, post-/clear), do nothing — keep
    the last visible title rather than flapping.
"""

from __future__ import annotations

import logging
import re
import time

from telegram import Bot
from telegram.error import TelegramError

from ..config import config
from .busy_indicator import ContextUsage

logger = logging.getLogger(__name__)


# Matches the trailing suffix this module appends, e.g. " · 113k/200k" or
# " · 324k/1M". Tolerant of whitespace and the `·`/`•` separator. Also
# matches the legacy "ctx NN%" form so existing topic titles get cleaned up
# on the first new edit.
_RE_CTX_SUFFIX = re.compile(
    r"\s*[·•]\s*(?:ctx\s*\d{1,3}%|\d+(?:\.\d+)?[km]\s*/\s*\d+(?:\.\d+)?[km])\s*$",
    re.IGNORECASE,
)

# Telegram forum topic name hard limit.
_MAX_TITLE_CHARS = 128


def strip_ctx_suffix(name: str) -> str:
    """Remove our trailing ctx suffix from a topic name, if present."""
    return _RE_CTX_SUFFIX.sub("", name).rstrip()


def format_tokens(tokens: int) -> str:
    """Render a token count compactly: ``113k``, ``324k``, ``1.2M``."""
    if tokens >= 1_000_000:
        m = tokens / 1_000_000
        s = f"{m:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    return f"{round(tokens / 1000)}k"


def format_max(max_tokens: int) -> str:
    """Render the cap label: ``200k`` or ``1M``."""
    if max_tokens >= 1_000_000:
        return "1M"
    return f"{max_tokens // 1000}k"


def format_suffix(usage: ContextUsage) -> str:
    """Render ``" · 113k/200k"`` for a usage snapshot."""
    return f" · {format_tokens(usage.tokens)}/{format_max(usage.max_tokens)}"


def format_title(base: str, usage: ContextUsage) -> str:
    """Compose ``"<base> · Nk/Mk"``, trimming base if needed to fit 128 chars."""
    base = strip_ctx_suffix(base).rstrip()
    suffix = format_suffix(usage)
    budget = _MAX_TITLE_CHARS - len(suffix)
    if budget < 1:
        return suffix.lstrip()
    if len(base) > budget:
        base = base[: budget - 1].rstrip() + "…"
    return f"{base}{suffix}"


# Per-(chat_id, thread_id) bookkeeping for debouncing.
_RouteKey = tuple[int, int]
_last_tokens: dict[_RouteKey, int] = {}
_last_max: dict[_RouteKey, int] = {}
_last_base: dict[_RouteKey, str] = {}
_last_rename_at: dict[_RouteKey, float] = {}


def reset_for_tests() -> None:
    _last_tokens.clear()
    _last_max.clear()
    _last_base.clear()
    _last_rename_at.clear()


def clear_route(chat_id: int, thread_id: int | None) -> None:
    """Drop all bookkeeping for a route — called from clear_topic_state."""
    key = (chat_id, thread_id or 0)
    _last_tokens.pop(key, None)
    _last_max.pop(key, None)
    _last_base.pop(key, None)
    _last_rename_at.pop(key, None)


async def maybe_rename_topic(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    base_name: str,
    usage: ContextUsage | None,
) -> None:
    """Edit the forum topic title to ``"<base> · Nk/Mk"`` when warranted.

    No-ops on:
      - ``context_in_title`` config disabled
      - ``thread_id`` falsy (only forum topics have editable titles)
      - ``usage`` is None (don't flap on transient parse failures)
      - empty ``base_name``
      - same (base, tokens, max) we last sent
      - debounce: token delta below threshold AND last rename within interval
    """
    if not config.context_in_title:
        return
    if not thread_id:
        return
    if usage is None:
        return
    base_name = strip_ctx_suffix(base_name).strip()
    if not base_name:
        return

    key = (chat_id, thread_id)
    last_tokens = _last_tokens.get(key)
    last_max = _last_max.get(key)
    last_base = _last_base.get(key)
    last_at = _last_rename_at.get(key, 0.0)
    now = time.monotonic()

    if (
        last_tokens == usage.tokens
        and last_max == usage.max_tokens
        and last_base == base_name
    ):
        return
    if (
        last_tokens is not None
        and last_max == usage.max_tokens
        and last_base == base_name
    ):
        delta = abs(usage.tokens - last_tokens)
        elapsed = now - last_at
        if (
            delta < config.context_title_min_delta_tokens
            and elapsed < config.context_title_min_interval_seconds
        ):
            return

    new_title = format_title(base_name, usage)
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=new_title,
        )
    except TelegramError as e:
        msg = str(e).lower()
        if "not_modified" in msg or "not modified" in msg:
            _last_tokens[key] = usage.tokens
            _last_max[key] = usage.max_tokens
            _last_base[key] = base_name
            _last_rename_at[key] = now
            return
        logger.debug(
            "edit_forum_topic chat=%d thread=%s name=%r failed: %s",
            chat_id,
            thread_id,
            new_title,
            e,
        )
        return

    _last_tokens[key] = usage.tokens
    _last_max[key] = usage.max_tokens
    _last_base[key] = base_name
    _last_rename_at[key] = now
    logger.debug(
        "edit_forum_topic chat=%d thread=%d name=%r ok (tokens=%d max=%d)",
        chat_id,
        thread_id,
        new_title,
        usage.tokens,
        usage.max_tokens,
    )
