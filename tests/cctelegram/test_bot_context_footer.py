"""Regression coverage for ``bot._build_context_footer``.

Pins the ``📊 Nk/200k`` context-window footer onto ``route_runtime`` as the
single authority for both the write and the read:

  - the 200k→1M cap **latch** (once usage crosses 200k the denominator
    latches to 1M and stays there even when usage falls back below 200k);
  - ``mark_session_reset`` drops the latched cap so a fresh session renders
    a fresh 200k denominator.
"""

from __future__ import annotations

import pytest

from cctelegram import bot, route_runtime
from cctelegram.session import ClaudeSession
from cctelegram.transcript_parser import LatestUsage

USER_ID = 1
THREAD_ID = 42
WINDOW_ID = "@7"


def _install_usage(
    monkeypatch: pytest.MonkeyPatch, tokens: int | None, model: str = "claude-opus-4-7"
) -> None:
    """Wire ``_build_context_footer`` deps so it observes ``tokens``.

    Patches ``session_manager.resolve_session_for_window`` (module global on
    ``bot``) and ``transcript_parser.read_latest_usage`` (imported inside the
    function) so the footer reads ``tokens`` without touching disk.
    """

    async def _fake_resolve(window_id: str) -> ClaudeSession:
        return ClaudeSession(
            session_id="sess",
            summary="",
            message_count=0,
            file_path="/tmp/does-not-matter.jsonl",
        )

    def _fake_read_latest_usage(jsonl_path: str) -> LatestUsage | None:
        if tokens is None:
            return None
        return LatestUsage(tokens=tokens, model=model)

    monkeypatch.setattr(
        bot.session_manager, "resolve_session_for_window", _fake_resolve
    )
    monkeypatch.setattr(
        "cctelegram.transcript_parser.read_latest_usage", _fake_read_latest_usage
    )


async def _footer() -> str | None:
    return await bot._build_context_footer(USER_ID, THREAD_ID, WINDOW_ID)


async def test_footer_renders_below_200k(monkeypatch: pytest.MonkeyPatch):
    """Footer reads context usage from route_runtime."""
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 50_000)

    assert await _footer() == "_📊 50k / 200k_"


async def test_footer_latches_to_1m_after_crossing_200k(
    monkeypatch: pytest.MonkeyPatch,
):
    """200k→1M latch: once usage crosses 200k the denominator latches to 1M
    and stays there even after usage falls back below 200k."""
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 250_000)
    assert await _footer() == "_📊 250k / 1M_"

    # Same route drops back to 80k — the cap must remain latched at 1M, not
    # snap back to 200k.
    _install_usage(monkeypatch, 80_000)
    assert await _footer() == "_📊 80k / 1M_"


async def test_footer_latch_cleared_by_session_reset(
    monkeypatch: pytest.MonkeyPatch,
):
    """``mark_session_reset`` (fired on /clear and on session-change cleanup)
    drops the cached usage + the latched cap, so the new session's footer
    renders a fresh 200k denominator rather than the stale 1M."""
    route = (USER_ID, THREAD_ID, WINDOW_ID)
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 250_000)
    assert await _footer() == "_📊 250k / 1M_"

    # The session rotates (/clear or session-change cleanup) → reset drops the
    # cached usage + the latched cap for this route.
    await route_runtime.mark_session_reset(route)

    # New session reports 80k — the cap must be a FRESH 200k, not the stale 1M.
    _install_usage(monkeypatch, 80_000)
    assert await _footer() == "_📊 80k / 200k_"


async def test_footer_blank_when_no_usage_observed(monkeypatch: pytest.MonkeyPatch):
    """No assistant turn yet → no footer (read returns None cleanly)."""
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, None)

    assert await _footer() is None
