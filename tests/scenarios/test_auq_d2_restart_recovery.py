"""Scenario (D2 restart-recovery RED gate): the first AUQ pick tap after a bot
restart is currently SWALLOWED.

The bug (D2): D3-β keeps a live card's in-memory pick tokens un-killable while
the poller observes the card on-pane, so an *idle* picker no longer dies. But a
bot **restart** wipes the in-memory ``_pick_tokens`` / ``_pick_token_cache`` /
``_reservations`` (all in-memory). The already-published Telegram card keeps its
old keyboard with the now-dead token strings baked into the callback_data, so the
first tap hits ``pick_token.peek(token) → None`` and degrades to the honest D3-α
modal ("↻ Refreshed — tap your choice again.") — no dispatch — for the card's
whole remaining lifetime.

These are RED baselines in the repo's assert-current-baseline / flip-in-fix-PR
style (cf. Bug 1 PR-A commit 1dcccff, Bug 2 PR-A commit beb40b1): each test
asserts TODAY's buggy behavior so CI stays green. The D2 fix PR (PR-2C) FLIPS
each assertion to the recovered behavior:

  * post-restart first tap DISPATCHES the carried option (digit + Enter) exactly
    once and writes the ledger ``accepted → dispatched`` lifecycle, instead of
    the modal;
  * a fresh render writes a durable per-token mint-intent row to
    ``pick_intent.jsonl`` (PR-2B) so recovery has the original intent to read;
  * a wrong-user post-restart tap answers ``WRONG_USER_PICK_TEXT`` (recovery adds
    the owner-auth the ``peek_none`` branch lacks today), still no dispatch.

Plan: temp/2026-06-06-auq-d2-restart-recovery-plan-v3.md (dual-PASS).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import (
    WRONG_USER_PICK_TEXT,
    DispatcherAdapters,
    dispatch_callback,
)
from cctelegram.handlers import interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_SESSION_ID = "33333333-3333-4333-8333-333333333333"
_D3A_MODAL = "↻ Refreshed — tap your choice again."


def _single_select_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Choose the post-restart recovery lane.",
                "header": "Recovery lane",
                "multiSelect": False,
                "options": [
                    {"label": "A) First", "description": "First option rationale."},
                    {"label": "B) Second", "description": "Second option rationale."},
                    {"label": "C) Third", "description": "Third option rationale."},
                ],
            }
        ]
    }


def _compressed_pane() -> str:
    return """← ☐ Recovery lane  ✔ Submit →
Choose the post-restart recovery lane.

❯ 2. B) Second
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _write_side_file(tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-d2",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_parser=terminal_parser,
    )


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _tap(scenario: ScenarioHarness, callback_data: str, *, user_id: int) -> Any:
    update = make_update_callback(
        callback_data, thread_id=42, user_id=user_id, chat_id=scenario.chat_id
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    return update


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _token(callback_data: str) -> str:
    assert callback_data.startswith(CB_ASK_PICK)
    return callback_data.removeprefix(CB_ASK_PICK).split(":")[-1]


def _digit_sends(scenario: ScenarioHarness, wid: str) -> list[tuple[str, str]]:
    return [
        (w, keys)
        for (w, keys, _, _) in scenario.tmux.sent_keys
        if w == wid and (keys.isdigit() or keys == "Enter")
    ]


async def _render_and_pick(scenario: ScenarioHarness) -> tuple[str, list[str]]:
    pane = _compressed_pane()
    wid = _bind(scenario, pane)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3
    return wid, picks


@pytest.mark.asyncio
async def test_red_baseline_post_restart_first_tap_swallowed(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline (the core D2 bug): after a restart wipes the in-memory token
    store, the first tap on the still-open card is NOT dispatched — it hits
    ``peek_none`` and shows the honest D3-α modal.

    PR-2C flips this: the tap DISPATCHES ``2`` + ``Enter`` exactly once (recovered
    from the durable mint-intent) and writes the ledger ``accepted → dispatched``.
    """
    wid, picks = await _render_and_pick(scenario)
    token = _token(picks[1])  # option 2 (cursor row)
    assert pick_token.peek(token) is not None

    # Simulate a bot restart: the in-memory token store / cache / reservations
    # are wiped, exactly as a process restart would. The published card and its
    # side file survive.
    pick_token.reset_for_tests()
    assert pick_token.peek(token) is None, "restart wiped the in-memory token"

    update = await _tap(scenario, picks[1], user_id=scenario.user_id)

    # RED baseline: today the first post-restart tap is swallowed.
    assert _digit_sends(scenario, wid) == [], (
        "RED baseline: no dispatch on the first post-restart tap. PR-2C flips "
        "this to assert [(wid,'2',...),(wid,'Enter',...)]."
    )
    answer = update.callback_query.answer.await_args.args[0]
    assert answer == _D3A_MODAL, (
        "RED baseline: the dead first tap shows the D3-α refresh modal. PR-2C "
        "recovers + dispatches instead."
    )


@pytest.mark.asyncio
async def test_red_baseline_no_durable_intent_written_at_render(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline (the missing mechanism): a fresh render does NOT persist a
    durable per-token mint-intent, so after a restart there is nothing to recover
    from.

    PR-2B flips this: the aqp: fresh-mint callsite writes a row to
    ``pick_intent.jsonl`` keyed by the token string.
    """
    await _render_and_pick(scenario)
    assert not (app_dir() / "pick_intent.jsonl").exists(), (
        "RED baseline: no durable mint-intent store exists yet. PR-2B adds "
        "pick_intent.jsonl written at the fresh aqp: mint."
    )


@pytest.mark.asyncio
async def test_red_baseline_post_restart_wrong_user_tap_not_owner_gated(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline (the owner-auth gap): the ``peek_none`` branch has no owner
    check, so a wrong-user tap after a restart gets the generic refresh modal,
    not ``WRONG_USER_PICK_TEXT`` (and, like the owner, no dispatch).

    PR-2C flips the answer to ``WRONG_USER_PICK_TEXT`` — recovery adds owner-auth
    from the stored intent — while keeping no dispatch.
    """
    wid, picks = await _render_and_pick(scenario)
    pick_token.reset_for_tests()

    update = await _tap(scenario, picks[1], user_id=scenario.user_id + 1)

    assert _digit_sends(scenario, wid) == [], "no dispatch for a wrong-user tap"
    answer = update.callback_query.answer.await_args.args[0]
    assert answer == _D3A_MODAL, (
        "RED baseline: peek_none has no owner check, so a wrong-user post-restart "
        "tap gets the generic modal. PR-2C answers WRONG_USER_PICK_TEXT instead."
    )
    assert answer != WRONG_USER_PICK_TEXT
