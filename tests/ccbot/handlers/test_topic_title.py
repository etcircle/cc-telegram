"""Tests for the surviving topic_title helpers.

The original module rewrote forum topic titles; that was reverted. What
remains:
  - strip_ctx_suffix: still used defensively in topic_edited_handler.
  - format_tokens / format_max: shared by the message footer + /context.
"""

from __future__ import annotations

from ccbot.handlers import topic_title


def test_strip_ctx_suffix_removes_kk_form() -> None:
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


def test_format_tokens_under_1m() -> None:
    assert topic_title.format_tokens(0) == "0k"
    assert topic_title.format_tokens(500) == "0k"  # rounds down
    assert topic_title.format_tokens(113_000) == "113k"
    assert topic_title.format_tokens(999_999) == "1000k"  # boundary, still k


def test_format_tokens_over_1m() -> None:
    assert topic_title.format_tokens(1_000_000) == "1M"
    assert topic_title.format_tokens(1_200_000) == "1.2M"
    assert topic_title.format_tokens(1_500_000) == "1.5M"


def test_format_max() -> None:
    assert topic_title.format_max(200_000) == "200k"
    assert topic_title.format_max(1_000_000) == "1M"
