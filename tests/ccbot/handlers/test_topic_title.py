"""Tests for the forum-topic-title context indicator.

Covers:
  - strip_ctx_suffix removes our suffix (new "Nk/Mk" form + legacy "ctx NN%")
  - format_title trims the base when total exceeds 128 chars
  - maybe_rename_topic: skips when disabled, no thread_id, or usage=None
  - debounces by token-delta and min-interval per route
  - clears bookkeeping on clear_route
  - "topic not modified" Telegram error treated as success
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from ccbot.config import config
from ccbot.handlers import topic_title
from ccbot.handlers.busy_indicator import ContextUsage


def _u(tokens: int, max_tokens: int = 200_000) -> ContextUsage:
    return ContextUsage(tokens=tokens, max_tokens=max_tokens)


@pytest.fixture(autouse=True)
def _reset() -> None:
    topic_title.reset_for_tests()


def test_strip_ctx_suffix_removes_new_form() -> None:
    assert topic_title.strip_ctx_suffix("foo · 113k/200k") == "foo"
    assert topic_title.strip_ctx_suffix("foo  ·  324k/1M  ") == "foo"
    assert topic_title.strip_ctx_suffix("foo • 1.2M/1M") == "foo"


def test_strip_ctx_suffix_removes_legacy_pct_form() -> None:
    assert topic_title.strip_ctx_suffix("foo · ctx 16%") == "foo"
    assert topic_title.strip_ctx_suffix("foo · CTX 99%") == "foo"


def test_strip_ctx_suffix_leaves_user_names_alone() -> None:
    assert topic_title.strip_ctx_suffix("Project · v2") == "Project · v2"
    assert topic_title.strip_ctx_suffix("113k/200k") == "113k/200k"
    assert topic_title.strip_ctx_suffix("foo · ctx done") == "foo · ctx done"


def test_format_title_trims_long_base() -> None:
    long_base = "x" * 200
    title = topic_title.format_title(long_base, _u(50_000))
    assert len(title) <= 128
    assert title.endswith(" · 50k/200k")


def test_format_title_strips_existing_suffix_first() -> None:
    title = topic_title.format_title("foo · 113k/200k", _u(16_000))
    assert title == "foo · 16k/200k"


def test_format_title_renders_1m_cap() -> None:
    title = topic_title.format_title("foo", _u(324_000, max_tokens=1_000_000))
    assert title == "foo · 324k/1M"


@pytest.mark.asyncio
async def test_maybe_rename_no_thread_id_skips() -> None:
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, None, "foo", _u(16_000))
    bot.edit_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_rename_usage_none_skips() -> None:
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", None)
    bot.edit_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_rename_disabled_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "context_in_title", False)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    bot.edit_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_rename_first_call_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    bot.edit_forum_topic.assert_called_once_with(
        chat_id=100, message_thread_id=7, name="foo · 16k/200k"
    )


@pytest.mark.asyncio
async def test_maybe_rename_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    assert bot.edit_forum_topic.call_count == 1


@pytest.mark.asyncio
async def test_maybe_rename_debounces_small_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    monkeypatch.setattr(config, "context_title_min_delta_tokens", 5_000)
    monkeypatch.setattr(config, "context_title_min_interval_seconds", 60.0)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    # +1k tokens within interval — should be debounced.
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(17_000))
    assert bot.edit_forum_topic.call_count == 1


@pytest.mark.asyncio
async def test_maybe_rename_passes_when_delta_threshold_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    monkeypatch.setattr(config, "context_title_min_delta_tokens", 5_000)
    monkeypatch.setattr(config, "context_title_min_interval_seconds", 60.0)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(22_000))
    assert bot.edit_forum_topic.call_count == 2


@pytest.mark.asyncio
async def test_maybe_rename_passes_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    monkeypatch.setattr(config, "context_title_min_delta_tokens", 5_000)
    monkeypatch.setattr(config, "context_title_min_interval_seconds", 0.0)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    # +1k tokens but interval elapsed — should pass.
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(17_000))
    assert bot.edit_forum_topic.call_count == 2


@pytest.mark.asyncio
async def test_maybe_rename_max_change_always_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    monkeypatch.setattr(config, "context_title_min_delta_tokens", 100_000)
    monkeypatch.setattr(config, "context_title_min_interval_seconds", 60.0)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(180_000))
    # Cap latched 200k → 1M; tokens delta is small but max changed, so edit.
    await topic_title.maybe_rename_topic(
        bot, 100, 7, "foo", _u(180_500, max_tokens=1_000_000)
    )
    assert bot.edit_forum_topic.call_count == 2


@pytest.mark.asyncio
async def test_clear_route_drops_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    monkeypatch.setattr(config, "context_title_min_delta_tokens", 5_000)
    monkeypatch.setattr(config, "context_title_min_interval_seconds", 60.0)
    bot = AsyncMock()
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    topic_title.clear_route(100, 7)
    # After clear, even a tiny delta should pass (no debounce state).
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(17_000))
    assert bot.edit_forum_topic.call_count == 2


@pytest.mark.asyncio
async def test_maybe_rename_handles_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = BadRequest("Bad Request: TOPIC_NOT_MODIFIED")
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    bot.edit_forum_topic.reset_mock()
    # Subsequent same-state call short-circuits via the idempotent path.
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    bot.edit_forum_topic.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_rename_swallows_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "context_in_title", True)
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = BadRequest("Bad Request: rate limit")
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    # Bookkeeping NOT updated on error — next call retries.
    bot.edit_forum_topic.side_effect = None
    await topic_title.maybe_rename_topic(bot, 100, 7, "foo", _u(16_000))
    assert bot.edit_forum_topic.call_count == 2
