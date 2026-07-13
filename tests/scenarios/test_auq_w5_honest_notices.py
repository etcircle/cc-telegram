"""GH #54 W5 — the three partial/untrusted-pane notices are copy-honest.

Three dishonest notices used to promise "send your answer (as text)" on EVERY
partial/untrusted AUQ pane — including preview single-selects and unlicensed
versions where PR-1's gate REFUSES a plain message. All three now route through
the SAME ``free_text.advertises_free_text`` predicate (flag ON × licensed CC
version × the live free-text affordance): only where a plain message would
actually be taken does the notice keep the text suggestion; otherwise it points
at the ↑/↓/⏎ keys.

These drive the public seam (``handle_interactive_ui``) with a fake bot / fake
tmux and flip the shared predicate to pin BOTH branches of each notice. The
predicate's own (real) logic is pinned in ``test_free_text_parser.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from cctelegram.handlers import free_text, interactive_ui
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "55555555-5555-4555-8555-555555555555"

_TEXT_SUFFIX = "send your answer as text"
_NAV_ONLY = "use the ↑/↓/⏎ keys below."


def _single_q_input(labels: list[str], *, title: str) -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": title,
                "header": "Scope",
                "multiSelect": False,
                "options": [{"label": label, "description": ""} for label in labels],
            }
        ]
    }


def _multi_q_input(labels: list[str], *, title: str) -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": title,
                "header": "A",
                "multiSelect": False,
                "options": [{"label": label, "description": ""} for label in labels],
            },
            {
                "question": "Second question?",
                "header": "B",
                "multiSelect": False,
                "options": [
                    {"label": "Yes", "description": ""},
                    {"label": "No", "description": ""},
                ],
            },
        ]
    }


def _partial_pane(rows: list[tuple[int, str]], *, cursor_number: int) -> str:
    lines: list[str] = []
    for number, label in rows:
        prefix = "❯" if number == cursor_number else " "
        lines.append(f"{prefix} {number}. {label}")
        lines.append(f"     description for option {number}")
    lines.append("")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _write_side_file(tool_input: dict[str, Any], *, aged: bool) -> None:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pending / f"{_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-w5",
                "written_at": time.time() - (1000 if aged else 1),
                "tool_input": tool_input,
            }
        )
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


def _picker_text(scenario: ScenarioHarness) -> str:
    for sent in reversed(scenario.bot.sent):
        text = str(sent.kwargs.get("text") or "")
        if (
            sent.kwargs.get("reply_markup") is not None
            and "AskUserQuestion" not in text
        ):
            return text.replace("\\", "")
    # Fall back to the last non-details message.
    for sent in reversed(scenario.bot.sent):
        text = str(sent.kwargs.get("text") or "")
        if not text.startswith("📋"):
            return text.replace("\\", "")
    return ""


_LABELS = ["A) alpha", "B) beta", "C) gamma"]
_TITLE = "What should we do next?"


def _pin_predicate(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(free_text, "advertises_free_text", lambda *a, **k: value)


# ── Notice 1: the partial-pane "Only options N-M visible" line ────────────────


@pytest.mark.asyncio
async def test_notice1_partial_pane_nav_only_when_no_text_answer(
    scenario: ScenarioHarness, monkeypatch
):
    _pin_predicate(monkeypatch, False)
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    # A MULTI-question side file: the single-Q swap (notice 2) declines, so
    # notice 1 survives.
    _write_side_file(_multi_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Only options 2-3 are visible" in picker
    assert _NAV_ONLY in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_notice1_partial_pane_text_when_answer_is_taken(
    scenario: ScenarioHarness, monkeypatch
):
    _pin_predicate(monkeypatch, True)
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file(_multi_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Only options 2-3 are visible" in picker
    assert _TEXT_SUFFIX in picker
    assert _NAV_ONLY not in picker


# ── Notice 2: the recovered-side-file "Tap-to-select is off" line ─────────────


@pytest.mark.asyncio
async def test_notice2_recovered_swap_nav_only_when_no_text_answer(
    scenario: ScenarioHarness, monkeypatch
):
    _pin_predicate(monkeypatch, False)
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Tap-to-select is off on a scrolled screen" in picker
    assert _NAV_ONLY in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_notice2_recovered_swap_text_when_answer_is_taken(
    scenario: ScenarioHarness, monkeypatch
):
    _pin_predicate(monkeypatch, True)
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Tap-to-select is off on a scrolled screen" in picker
    assert _TEXT_SUFFIX in picker
    assert _NAV_ONLY not in picker


# ── Notice 3: the rescue "live screen is busy" line ───────────────────────────
#
# The third notice ("⚠️ The live screen is busy … buttons are disabled …") fires
# ONLY on a RESCUE render — a pane whose AUQ chrome ``extract_interactive_content``
# still recognizes yet whose 500-line-scrollback structured parse yields no
# options (``dispatch_trusted`` False, not the partial-pane shape). That coupling
# cannot be synthesized deterministically through the live ``handle_interactive_ui``
# gate (a pane parseable enough to be recognized as AUQ also parses its options,
# so it is never a rescue). It composes its suffix from the SAME ``_nav_suffix``
# local the render computes ONCE per call — the identical predicate + suffix
# pinned above for notices 1 and 2 and unit-tested in ``test_free_text_parser``.
# A per-render divergence between the three notices is therefore unreachable by
# construction.
