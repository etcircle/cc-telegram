"""Unit tests for interactive_ui.convert_interactive_msg_to_late_answer.

The Wave A AFK conversion (plan §A3/§A4): ONE route-lock-owned critical
section (snapshot → Phase-1 pop → forget_ask_tool_input → ledger release, no
await gaps), then the SHIELDED post-lock steps (_fire_clear + the Phase-2
Telegram edit). These tests pin the lock/cancellation/prune invariants that
the black-box scenario can't reach (they manipulate the route lock and the
_interactive_mode map directly — unit scope, not scenario scope).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import auq_source, interactive_ui, late_answer, pick_token
from cctelegram.utils import app_dir

USER_ID = 12345
THREAD_ID = 42
WID = "@8"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
TOOL_USE_ID = "toolu_afk_expected"

_TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which lane should we take?",
            "header": "Lane",
            "multiSelect": False,
            "options": [
                {"label": "Lane A", "description": "The safe lane."},
                {"label": "Lane B", "description": "The fast lane."},
            ],
        }
    ]
}


class _RecordingBot:
    """Minimal Bot stand-in recording topic_edit/delete traffic."""

    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    async def edit_message_text(self, *, chat_id: int, message_id: int, **kw: Any):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, **kw})
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        self.deletes.append({"chat_id": chat_id, "message_id": message_id})
        return True


class _BlockingBot(_RecordingBot):
    """Bot whose edit blocks until released — for the cancellation test."""

    def __init__(self) -> None:
        super().__init__()
        self.edit_started = asyncio.Event()
        self.release = asyncio.Event()

    async def edit_message_text(self, *, chat_id: int, message_id: int, **kw: Any):
        self.edit_started.set()
        await self.release.wait()
        return await super().edit_message_text(
            chat_id=chat_id, message_id=message_id, **kw
        )


_SESSION_MGR = SimpleNamespace(resolve_chat_id=lambda _u, _t=None: -100500)


@pytest.fixture(autouse=True)
def _reset():
    interactive_ui.reset_for_tests()
    late_answer.reset_for_tests()
    pick_token.reset_for_tests()
    auq_source.reset_for_tests()
    route_runtime.reset_for_tests()
    yield
    interactive_ui.reset_for_tests()
    late_answer.reset_for_tests()
    pick_token.reset_for_tests()
    auq_source.reset_for_tests()
    route_runtime.reset_for_tests()


def _seed_surface(
    *, wid: str = WID, msg_id: int = 777, tool_use_id: str | None = TOOL_USE_ID
) -> None:
    interactive_ui.set_interactive_mode(USER_ID, wid, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = msg_id
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT, tool_use_id)


def _seed_window_session(wid: str = WID, session_id: str = SESSION_ID) -> None:
    from cctelegram.session import WindowState, session_manager

    session_manager.window_states[wid] = WindowState(
        session_id=session_id, cwd="/repo", window_name="repo"
    )


def _write_side_file(
    tool_input: dict[str, Any],
    *,
    session_id: str = SESSION_ID,
    tool_use_id: str = "",
) -> None:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )


async def _convert(
    bot: Any, *, wid: str = WID, expected_tool_use_id: str | None = TOOL_USE_ID
) -> None:
    await interactive_ui.convert_interactive_msg_to_late_answer(
        bot,
        USER_ID,
        THREAD_ID,
        wid,
        expected_tool_use_id=expected_tool_use_id,
        session_mgr=_SESSION_MGR,
    )


def _mint_pick_token(window_id: str) -> str:
    """Mint a cache-backed token row the way production does (mint_row) so
    ``prune_for_route`` — which sweeps ``_pick_token_cache`` rows — sees it."""
    tokens, _fresh = pick_token.mint_row(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        window_id=window_id,
        fingerprint="ff" * 20,
        source_kind="pane",
        source_fingerprint="sfp",
        specs=[
            pick_token._MintSpec(
                option_number=1, option_label="Lane A", is_review_submit=False
            )
        ],
    )
    return tokens[0]


# ── lock atomicity + cancellation ([R1 Hermes P2] / [R2 Hermes P2-2]) ─────


@pytest.mark.asyncio
async def test_afk_conversion_atomic_vs_poller_lock() -> None:
    """A poller-held route lock defers the ENTIRE conversion — no teardown
    is observable until the lock releases; then the conversion lands."""
    _seed_surface()
    bot = _RecordingBot()
    lock = interactive_ui._get_route_lock(USER_ID, THREAD_ID)

    async with lock:  # the poller holding the lock across Telegram I/O
        task = asyncio.create_task(_convert(bot))
        await asyncio.sleep(0.05)
        assert not task.done(), "conversion must wait for the route lock"
        assert interactive_ui.has_interactive_surface(USER_ID, THREAD_ID) is True, (
            "no teardown may happen while the poller holds the lock"
        )
        assert bot.edits == []

    await task
    assert interactive_ui.has_interactive_surface(USER_ID, THREAD_ID) is False
    assert len(bot.edits) == 1 and bot.edits[0]["message_id"] == 777
    assert bot.deletes == []


@pytest.mark.asyncio
async def test_afk_conversion_poller_tombstoned_first_degrades_to_skip() -> None:
    """If a poller tick tombstoned the card before the converter got the
    lock, the conversion degrades to the disclosed no-surface skip — never a
    re-post, never a surviving pick-token row (A10.2)."""
    _seed_surface()
    token = _mint_pick_token(WID)
    bot = _RecordingBot()
    lock = interactive_ui._get_route_lock(USER_ID, THREAD_ID)

    async with lock:
        task = asyncio.create_task(_convert(bot))
        await asyncio.sleep(0.02)
        # Simulate the tombstone's Phase 1 (state pops + prune) under the lock.
        interactive_ui._clear_interactive_msg((USER_ID, THREAD_ID))
        cleared = interactive_ui._interactive_mode.pop((USER_ID, THREAD_ID), None)
        assert cleared == WID
        pick_token.prune_for_route(USER_ID, THREAD_ID, cleared)

    await task
    assert bot.edits == [] and bot.deletes == [], "no-surface AFK must skip quietly"
    assert pick_token.peek(token) is None, "no pick-token row may survive"
    # No late-answer card was minted for the dead surface.
    assert late_answer.lookup(token) is None


@pytest.mark.asyncio
async def test_afk_phase2_edit_survives_caller_cancellation() -> None:
    """[R2 Hermes P2-2] once Phase 1 commits, cancelling the CALLER must not
    strand the old picker visibly tappable — the shielded Phase-2 edit still
    lands (the W1 delete-protocol precedent)."""
    _seed_surface()
    bot = _BlockingBot()

    task = asyncio.create_task(_convert(bot))
    await asyncio.wait_for(bot.edit_started.wait(), timeout=2.0)
    # Phase 1 has committed (the edit only starts post-lock).
    assert interactive_ui.has_interactive_surface(USER_ID, THREAD_ID) is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bot.release.set()
    for _ in range(20):
        if bot.edits:
            break
        await asyncio.sleep(0.01)
    assert len(bot.edits) == 1 and bot.edits[0]["message_id"] == 777, (
        "the shielded Phase-2 edit must complete despite caller cancellation"
    )


@pytest.mark.asyncio
async def test_afk_prunes_popped_window_not_caller(caplog) -> None:
    """[R2 Hermes P2-1] the prune target is the POPPED _interactive_mode
    window (@9), never the caller's wid (@8) blindly; a WARNING flags the
    mismatch."""
    # The route's surface belongs to @9; the caller's tool_result names @8.
    interactive_ui.set_interactive_mode(USER_ID, "@9", THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    interactive_ui.remember_ask_tool_input("@8", _TOOL_INPUT, TOOL_USE_ID)
    token_9 = _mint_pick_token("@9")
    token_8 = _mint_pick_token("@8")
    bot = _RecordingBot()

    with caplog.at_level(logging.WARNING, logger="cctelegram.handlers.interactive_ui"):
        await _convert(bot, wid="@8")

    assert pick_token.peek(token_9) is None, "the POPPED window's tokens are pruned"
    assert pick_token.peek(token_8) is not None, (
        "the caller's wid must NOT be pruned blindly on a stale mismatch"
    )
    assert any(
        "AFK_CONVERT" in r.getMessage() and "@9" in r.getMessage()
        for r in caplog.records
    ), "a WARNING must flag cleared_window_id != wid"


# ── snapshot trust (id parity) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_afk_snapshot_id_mismatch_falls_back_to_side_file() -> None:
    """A cached tool_input whose tool_use_id mismatches the expected id is
    MISTRUSTED; the side file (matching id) supplies the snapshot."""
    _seed_window_session()
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    stale_input = {
        "questions": [
            {
                "question": "A STALE prior question?",
                "multiSelect": False,
                "options": [{"label": "Old", "description": "old"}],
            }
        ]
    }
    interactive_ui.remember_ask_tool_input(WID, stale_input, "toolu_STALE")
    side_input = {
        "questions": [
            {
                "question": "The REAL AFK question?",
                "multiSelect": False,
                "options": [
                    {"label": "Fresh A", "description": "a"},
                    {"label": "Fresh B", "description": "b"},
                ],
            }
        ]
    }
    _write_side_file(side_input, tool_use_id=TOOL_USE_ID)
    bot = _RecordingBot()
    try:
        await _convert(bot)
    finally:
        (app_dir() / "auq_pending" / f"{SESSION_ID}.json").unlink(missing_ok=True)

    assert len(bot.edits) == 1
    text = bot.edits[0]["text"]
    assert "The REAL AFK question?" in text
    assert "A STALE prior question?" not in text
    markup = bot.edits[0].get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Fresh A", "Fresh B"]


@pytest.mark.asyncio
async def test_afk_snapshot_both_mistrusted_text_only() -> None:
    """Cache AND side file both id-mismatched → snap=None → generic text-only
    notice (no Question line, no keyboard)."""
    _seed_window_session()
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    interactive_ui.remember_ask_tool_input(WID, _TOOL_INPUT, "toolu_STALE")
    _write_side_file(_TOOL_INPUT, tool_use_id="toolu_ALSO_STALE")
    bot = _RecordingBot()
    try:
        await _convert(bot)
    finally:
        (app_dir() / "auq_pending" / f"{SESSION_ID}.json").unlink(missing_ok=True)

    assert len(bot.edits) == 1
    text = bot.edits[0]["text"]
    assert "⏰ Claude proceeded after ~60s (no response)." in text
    assert "Question:" not in text
    assert "Reply in text to send a correction." in text
    assert bot.edits[0].get("reply_markup") is None


@pytest.mark.asyncio
async def test_afk_side_file_unknown_id_trusted() -> None:
    """The side file's tool_use_id 'may be \"\"' (hook payload without one) —
    an EMPTY captured id is 'unknown', which the id-parity rule treats like
    None (either-is-None → trusted), not as a disqualifying mismatch."""
    _seed_window_session()
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    _write_side_file(_TOOL_INPUT, tool_use_id="")
    bot = _RecordingBot()
    try:
        await _convert(bot)
    finally:
        (app_dir() / "auq_pending" / f"{SESSION_ID}.json").unlink(missing_ok=True)

    assert len(bot.edits) == 1
    assert "Which lane should we take?" in bot.edits[0]["text"]
    assert bot.edits[0].get("reply_markup") is not None


# ── card shapes (§A4) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_afk_multi_question_converts_without_buttons() -> None:
    multi_q = {
        "questions": [
            {"question": "Q1?", "multiSelect": False, "options": [{"label": "A"}]},
            {"question": "Q2?", "multiSelect": False, "options": [{"label": "B"}]},
        ]
    }
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    interactive_ui.remember_ask_tool_input(WID, multi_q, TOOL_USE_ID)
    bot = _RecordingBot()

    await _convert(bot)

    assert len(bot.edits) == 1
    text = bot.edits[0]["text"]
    assert "Question: Q1?" in text
    assert "Reply in text to send a correction." in text
    assert bot.edits[0].get("reply_markup") is None


@pytest.mark.asyncio
async def test_afk_multi_select_converts_without_buttons() -> None:
    multi_sel = {
        "questions": [
            {
                "question": "Pick many?",
                "multiSelect": True,
                "options": [{"label": "A"}, {"label": "B"}],
            }
        ]
    }
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    interactive_ui.remember_ask_tool_input(WID, multi_sel, TOOL_USE_ID)
    bot = _RecordingBot()

    await _convert(bot)

    assert len(bot.edits) == 1
    text = bot.edits[0]["text"]
    assert "Question: Pick many?" in text
    assert "Reply in text to send a correction." in text
    assert bot.edits[0].get("reply_markup") is None


@pytest.mark.asyncio
async def test_afk_no_snapshot_generic_notice() -> None:
    """No cached input, no side file → generic text-only notice."""
    interactive_ui.set_interactive_mode(USER_ID, WID, THREAD_ID)
    interactive_ui._interactive_msgs[(USER_ID, THREAD_ID)] = 777
    bot = _RecordingBot()

    await _convert(bot)

    assert len(bot.edits) == 1
    text = bot.edits[0]["text"]
    assert text.splitlines() == [
        "⏰ Claude proceeded after ~60s (no response).",
        "Reply in text to send a correction.",
    ]
    assert bot.edits[0].get("reply_markup") is None


@pytest.mark.asyncio
async def test_afk_edit_failure_no_delete_fallback() -> None:
    """A Phase-2 edit failure logs and returns — NEVER a delete fallback
    (mirror of the tombstone rule)."""

    class _FailingBot(_RecordingBot):
        async def edit_message_text(self, **kw: Any):
            raise RuntimeError("boom")

    _seed_surface()
    bot = _FailingBot()

    await _convert(bot)

    assert bot.deletes == [], "edit failure must not fall back to delete"
    # Phase 1 still committed.
    assert interactive_ui.has_interactive_surface(USER_ID, THREAD_ID) is False


# ── Codex round-2 P2: Phase 2 must be genuinely never-raise ───────────────


@pytest.mark.asyncio
async def test_afk_phase2_retry_after_swallowed_no_strand(caplog, monkeypatch) -> None:
    """[Codex r2 P2] topic_edit RE-RAISES RetryAfter, and asyncio.shield does
    NOT contain exceptions raised INSIDE the shielded coroutine — so a
    RetryAfter from the Phase-2 edit escaped to the caller AFTER Phase 1
    committed (the stranded-dead-card class via a different exit path). The
    converter must return normally: Phase-1 state stays torn down, ONE
    WARNING logged, and dismiss_if_kind is still attempted."""
    from unittest.mock import AsyncMock

    from telegram.error import RetryAfter

    from cctelegram.handlers import attention

    class _RetryAfterBot(_RecordingBot):
        async def edit_message_text(self, **kw: Any):
            raise RetryAfter(7)

    _seed_surface()
    bot = _RetryAfterBot()
    dismiss = AsyncMock()
    monkeypatch.setattr(attention, "dismiss_if_kind", dismiss)

    with caplog.at_level(logging.WARNING, logger="cctelegram.handlers.interactive_ui"):
        # Must NOT raise — a raise here is exactly the stranded-card escape.
        await _convert(bot)

    assert interactive_ui.has_interactive_surface(USER_ID, THREAD_ID) is False, (
        "Phase 1 stays committed"
    )
    assert bot.deletes == [], "no delete fallback on an edit raise"
    assert any(
        "AFK_CONVERT" in r.getMessage() and "edit raised" in r.getMessage()
        for r in caplog.records
    ), "one WARNING must record the edit raise (no delete fallback)"
    dismiss.assert_awaited_once(), "dismiss_if_kind must still be attempted"


@pytest.mark.asyncio
async def test_afk_phase2_dismiss_raise_swallowed(caplog, monkeypatch) -> None:
    """[Codex r2 P2] a raise from attention.dismiss_if_kind must not escape
    the converter either — best-effort, warning logged."""
    from cctelegram.handlers import attention

    async def _dismiss_boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("dismiss boom")

    _seed_surface()
    bot = _RecordingBot()
    monkeypatch.setattr(attention, "dismiss_if_kind", _dismiss_boom)

    with caplog.at_level(logging.WARNING, logger="cctelegram.handlers.interactive_ui"):
        await _convert(bot)  # must NOT raise

    assert len(bot.edits) == 1, "the card edit still landed"
    assert any(
        "AFK_CONVERT" in r.getMessage() and "dismiss" in r.getMessage()
        for r in caplog.records
    )
