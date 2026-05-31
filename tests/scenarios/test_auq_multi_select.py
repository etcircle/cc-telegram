"""Scenario coverage for AskUserQuestion multi-select rendering and toggles.

Exercises the public Telegram callback seam for the PR-C ``aqt:`` toggle path:
render uses the pane-aware side-file source, toggles send a bare digit without
ledgering or consuming sibling tokens, and Submit/Cancel remains the existing
review-screen ``aqp:`` flow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module, terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import auq_ledger, interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_PICK, CB_ASK_TOGGLE
from cctelegram.session_monitor import NewMessage
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _safeguards_input(*, multi: bool = True) -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick the implementation safeguards to include.",
                "header": "Safeguards",
                "multiSelect": multi,
                "options": [
                    {"label": "A) Verify cursor row from tmux pane before Space"},
                    {"label": "B) Keep PreToolUse side file alive across toggles"},
                    {"label": "C) Suppress tabbed multi-question forms"},
                    {"label": "D) Add Submit and Cancel buttons"},
                ],
            }
        ]
    }


def _compressed_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick evidence.",
                "header": "Evidence",
                "multiSelect": True,
                "options": [
                    {"label": "A) Parser fixture parity"},
                    {"label": "B) Callback dispatch parity"},
                    {"label": "C) Unknown-mode suppression"},
                    {"label": "D) Tool-result cleanup proof"},
                    {"label": "E) Review-screen submit reuse"},
                ],
            }
        ]
    }


def _two_toggled_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick two cc-telegram safeguards.",
                "header": "Safeguards",
                "multiSelect": True,
                "options": [
                    {"label": "A) Verify cursor row before Space"},
                    {"label": "B) Preserve side file through toggles"},
                    {"label": "C) Suppress tabbed multi-question forms"},
                    {"label": "D) Use submit ledger for final Enter"},
                ],
            }
        ]
    }


def _multi_question_input() -> dict[str, Any]:
    data = _safeguards_input()
    data["questions"].append(
        {
            "question": "Second question",
            "header": "Second",
            "multiSelect": True,
            "options": [{"label": "A) Second"}, {"label": "B) Other"}],
        }
    )
    return data


def _bind(
    scenario: ScenarioHarness, pane: str, *, session_id: str = _SESSION_ID
) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42,
        wid,
        display_name="repo",
        cwd="/repo",
        session_id=session_id,
    )
    return wid


def _write_side_file(session_id: str, tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": "tool-use-1",
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


def _last_markup(scenario: ScenarioHarness) -> Any:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return markup
    raise AssertionError("no reply markup recorded")


def _callbacks(scenario: ScenarioHarness) -> list[str]:
    markup = _last_markup(scenario)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(scenario: ScenarioHarness) -> str:
    return "\n---\n".join(scenario.bot.texts())


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _tap(
    scenario: ScenarioHarness, callback_data: str, *, user_id: int | None = None
) -> None:
    update = make_update_callback(
        callback_data,
        thread_id=42,
        user_id=user_id or scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )


def _prefixes(callbacks: list[str], prefix: str) -> list[str]:
    return [cb for cb in callbacks if cb.startswith(prefix)]


@pytest.mark.asyncio
async def test_happy_path_toggle_tab_review_submit_and_tool_result_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())

    await _render(scenario, wid)
    callbacks = _callbacks(scenario)
    toggles = _prefixes(callbacks, CB_ASK_TOGGLE)
    assert len(toggles) == 4
    assert not _prefixes(callbacks, CB_ASK_PICK)
    assert not any(cb.startswith(("aqm:", "aqs:", "aqx:")) for cb in callbacks)

    await _tap(scenario, toggles[1])
    assert scenario.tmux.sent_keys == [(wid, "2", False, True)]
    assert side_file.exists(), "aqt toggles must not clean side files"

    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_2_toggled_tmux_capture.txt"))
    await _render(scenario, wid)
    assert "☑ 2. B)" in _texts(scenario)

    tab = next(cb for cb in _callbacks(scenario) if cb.startswith("aq:tab:"))
    scenario.tmux.set_pane(
        wid, _fixture("auq_multiselect_ready_to_submit_tmux_capture.txt")
    )
    await _tap(scenario, tab)
    review_callbacks = _callbacks(scenario)
    assert _prefixes(review_callbacks, CB_ASK_PICK)
    assert not _prefixes(review_callbacks, CB_ASK_TOGGLE)

    submit = _prefixes(review_callbacks, CB_ASK_PICK)[0]
    await _tap(scenario, submit)
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]
    assert side_file.exists(), "review aqp dispatch is not the cleanup event"

    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text="AskUserQuestion answered",
            content_type="tool_result",
            tool_use_id="tool-use-1",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )
    assert not side_file.exists()


@pytest.mark.asyncio
async def test_toggle_off_retap_sends_same_digit_twice(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    await _tap(scenario, opt2)
    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    await _tap(scenario, opt2)
    assert [k for _, k, _, _ in scenario.tmux.sent_keys] == ["2", "2"]


@pytest.mark.asyncio
async def test_compressed_pane_with_side_file_renders_unknowns_and_toggles(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(
        scenario,
        _fixture("auq_multiselect_compressed_long_cursor_only_tmux_capture.txt"),
    )
    side_file = _write_side_file(_SESSION_ID, _compressed_input())
    await _render(scenario, wid)
    text = _texts(scenario)
    assert "☐ 3. C) Unknown-mode suppression" in text
    assert "· 1. A) Parser fixture parity" in text
    assert "· 5. E) Review-screen submit reuse" in text
    toggles = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    assert len(toggles) == 5
    await _tap(scenario, toggles[3])
    assert scenario.tmux.sent_keys == [(wid, "4", False, True)]
    assert side_file.exists()


@pytest.mark.asyncio
async def test_overlay_correctness_with_visible_selected_and_offscreen_unknowns(
    scenario: ScenarioHarness,
) -> None:
    pane = """←  ☒ Safeguards  ✔ Submit  →

Pick evidence.

  1. [ ] A) Parser fixture parity
❯ 2. [✔] B) Callback dispatch parity
  3. [ ] C) Unknown-mode suppression
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    _write_side_file(_SESSION_ID, _compressed_input())
    await _render(scenario, wid)
    text = _texts(scenario)
    assert "☐ 1. A) Parser fixture parity" in text
    assert "☑ 2. B) Callback dispatch parity" in text
    assert "☐ 3. C) Unknown-mode suppression" in text
    assert "· 4. D) Tool-result cleanup proof" in text
    assert "· 5. E) Review-screen submit reuse" in text


@pytest.mark.asyncio
async def test_hook_missing_compressed_suppresses_full_pane_mints_toggles(
    scenario: ScenarioHarness,
) -> None:
    # Compressed capture (options scrolled past 1, non-contiguous-from-1 → the
    # full list is NOT captured): no toggles + "full list unavailable" notice.
    wid = _bind(
        scenario,
        _fixture("auq_multiselect_compressed_long_cursor_only_tmux_capture.txt"),
    )
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    assert "full list unavailable" in _texts(scenario)

    # Full fresh capture, hook still missing (pure-pane source): the picker is
    # now "complete" — options contiguous from 1 AND the "Type something"
    # free-text affordance is visible at the bottom of the list, proving the
    # whole list was captured. The fix deliberately mints toggles here so a
    # render→tap AUQ-source flip (side file → pure pane) keeps the fast buttons
    # working instead of silently rejecting the tap. (Pre-fix the pure-pane
    # path hardcoded options_complete=False and this minted no toggles.)
    scenario.bot.sent.clear()
    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    await _render(scenario, wid)
    assert len(_prefixes(_callbacks(scenario), CB_ASK_TOGGLE)) == 4


@pytest.mark.asyncio
async def test_unknown_partial_glyphs_suppresses_toggles(
    scenario: ScenarioHarness,
) -> None:
    pane = """←  ☐ Safeguards  ✔ Submit  →

Pick.

❯ 1. [ ] A) One
  2. B) Two
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)


@pytest.mark.asyncio
async def test_staleness_mid_toggle_refreshes_without_dispatch_or_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    scenario.tmux.set_pane(
        wid, _fixture("auq_multiselect_ready_to_submit_tmux_capture.txt")
    )
    await _tap(scenario, opt2)
    assert not scenario.tmux.sent_keys
    assert side_file.exists()


@pytest.mark.asyncio
async def test_failed_digit_no_ledger_and_side_file_survives(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    scenario.tmux.send_keys_response = False
    await _tap(scenario, opt2)
    assert scenario.tmux.sent_keys == [(wid, "2", False, True)]
    assert side_file.exists()
    assert not (app_dir() / auq_ledger.LEDGER_FILENAME).exists()


@pytest.mark.asyncio
async def test_wrong_user_toggle_does_not_dispatch_or_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    await _tap(scenario, _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1], user_id=999)
    assert not scenario.tmux.sent_keys
    assert side_file.exists()


@pytest.mark.asyncio
async def test_status_poll_rerender_after_toggle_keeps_side_file_and_options_complete(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    await _tap(scenario, _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1])
    await _render(scenario, wid)
    assert side_file.exists()
    assert len(_prefixes(_callbacks(scenario), CB_ASK_TOGGLE)) == 4


@pytest.mark.asyncio
async def test_multi_question_suppresses_toggle_buttons(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    _write_side_file(_SESSION_ID, _multi_question_input())
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)


@pytest.mark.asyncio
async def test_single_select_unaffected_still_aqp_digit_enter(
    scenario: ScenarioHarness,
) -> None:
    pane = """Pick one.

❯ 1. A) One
  2. B) Two
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    await _render(scenario, wid)
    picks = _prefixes(_callbacks(scenario), CB_ASK_PICK)
    assert len(picks) == 2
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    await _tap(scenario, picks[0])
    assert scenario.tmux.sent_keys[:2] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]


def test_aqt_prefix_is_registered() -> None:
    from cctelegram.callback_dispatcher.registry import lookup

    entry = lookup("aqt:route:fp:2:token")
    assert entry is not None
    assert entry.executor_name == "execute_interactive_callback"
