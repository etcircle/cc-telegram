"""Regression coverage for ``bot._build_context_footer``.

Pins the 8a migration of the ``📊 Nk/200k`` context-window footer onto
``route_runtime`` as the single authority for both the write and the read:

  - the 200k→1M cap **latch** (once usage crosses 200k the denominator
    latches to 1M and stays there even when usage falls back below 200k);
  - **flag-independence** — the footer renders identically regardless of
    ``config.route_runtime_v2``, because the route_runtime write is now
    unconditional rather than gated behind the soak flag.

Before 8a this function had zero coverage (thinnest spot in the audit).
"""

from __future__ import annotations

import pytest

from cctelegram import bot, route_runtime
from cctelegram.config import config
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


async def _footer(monkeypatch: pytest.MonkeyPatch, *, v2: bool) -> str | None:
    monkeypatch.setattr(config, "route_runtime_v2", v2)
    return await bot._build_context_footer(USER_ID, THREAD_ID, WINDOW_ID)


@pytest.mark.parametrize("v2", [True, False])
async def test_footer_renders_below_200k_in_both_configs(
    monkeypatch: pytest.MonkeyPatch, v2: bool
):
    """Footer reads from route_runtime regardless of the soak flag.

    Since the route_runtime write is now unconditional, the footer must
    render identically whether ``route_runtime_v2`` is on or off — if the
    write stayed gated, the read would blank in the flag-off config.
    """
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 50_000)

    footer = await _footer(monkeypatch, v2=v2)

    assert footer == "_📊 50k / 200k_"


@pytest.mark.parametrize("v2", [True, False])
async def test_footer_latches_to_1m_after_crossing_200k(
    monkeypatch: pytest.MonkeyPatch, v2: bool
):
    """200k→1M latch: once usage crosses 200k the denominator latches to 1M
    and stays there even after usage falls back below 200k.

    Asserted in BOTH flag configs to prove the unconditional write keeps the
    latch state populated regardless of ``route_runtime_v2``.
    """
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 250_000)
    assert await _footer(monkeypatch, v2=v2) == "_📊 250k / 1M_"

    # Same route drops back to 80k — the cap must remain latched at 1M, not
    # snap back to 200k.
    _install_usage(monkeypatch, 80_000)
    assert await _footer(monkeypatch, v2=v2) == "_📊 80k / 1M_"


@pytest.mark.parametrize("v2", [True, False])
async def test_footer_latch_cleared_by_session_reset_in_both_configs(
    monkeypatch: pytest.MonkeyPatch, v2: bool
):
    """Session reset drops the latch in ALL configs (Codex 8a finding).

    The footer reads route_runtime.context_usage unconditionally, so the
    reset that DROPS that cache (``mark_session_reset``, fired on /clear and
    on session-change cleanup) must also be unconditional — otherwise the 1M
    latch would survive a session reset in the flag-off config and the new
    session's footer would render the stale larger window.
    """
    route = (USER_ID, THREAD_ID, WINDOW_ID)
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, 250_000)
    assert await _footer(monkeypatch, v2=v2) == "_📊 250k / 1M_"

    # The session rotates (/clear or session-change cleanup) → reset drops the
    # cached usage + the latched cap for this route.
    await route_runtime.mark_session_reset(route)

    # New session reports 80k — the cap must be a FRESH 200k, not the stale 1M.
    _install_usage(monkeypatch, 80_000)
    assert await _footer(monkeypatch, v2=v2) == "_📊 80k / 200k_"


async def test_footer_blank_when_no_usage_observed(monkeypatch: pytest.MonkeyPatch):
    """No assistant turn yet → no footer (read returns None cleanly)."""
    route_runtime.reset_for_tests()
    _install_usage(monkeypatch, None)

    assert await _footer(monkeypatch, v2=True) is None
