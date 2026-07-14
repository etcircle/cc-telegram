"""GH #54 W5 — the partial/untrusted-pane notices are copy-honest (r1 P2-1).

Three dishonest notices used to promise "send your answer (as text)" on EVERY
partial/untrusted AUQ pane. All three now route through
``free_text.advertises_free_text``, which REUSES the free-text executor's OWN
gates (r3 structural remedy — never a mirrored list): the shared shape gate
``_auq_form_shape`` (single-question single-select + a live affordance +
COMPLETE contiguous options + a PROVEN cursor), the executor's anchor reader
``read_surface_anchor`` (no PreToolUse side file ⇒ ``surface_anchor_lost`` ⇒
nav-only), and flag ON × licensed CC version. The r1 P2-1 consequence pinned
here: the notices fire
precisely on partial/scrolled/unparseable panes — shapes the EXECUTOR refuses
(``options_complete`` False / no parseable form) — so on a LICENSED version
(the fake tmux default ``pane_current_command`` IS the licensed 2.1.207) they
must STILL say nav-only. The earlier flag × license × affordance predicate
said "send your answer as text" there while the send was refused.

These drive the public seam (``handle_interactive_ui``) with a fake bot / fake
tmux and the REAL predicate — no monkeypatch of the predicate (the r1 P2-1
test defect: boolean substitution hid the executor mismatch). The predicate's
own truth table, including the text-advertising branch (c) that partial panes
can never reach, is pinned in ``test_free_text_parser.py`` with real forms.
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


def _partial_pane(
    rows: list[tuple[int, str]], *, cursor_number: int, affordance: bool = True
) -> str:
    """A scrolled picker pane: options start past 1 ⇒ ``options_complete`` False.

    ``affordance`` appends the ``Type something.`` row so the parsed form is
    ``is_free_text=True`` — the EXACT reviewer shape (licensed + affordance +
    incomplete): only the executor-parity ``options_complete`` leg can make
    the notice honest.
    """
    lines: list[str] = []
    for number, label in rows:
        prefix = "❯" if number == cursor_number else " "
        lines.append(f"{prefix} {number}. {label}")
        lines.append(f"     description for option {number}")
    if affordance:
        next_num = rows[-1][0] + 1
        lines.append(f"  {next_num}. Type something.")
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


@pytest.fixture(autouse=True)
def _flag_on():
    """The REAL predicate with the flag ON — never a boolean substitute."""
    free_text.set_enabled(True)
    yield
    free_text.set_enabled(True)


# ── Notice 1: the partial-pane "Only options N-M visible" line ────────────────
#
# The fake tmux default ``pane_current_command`` is the LICENSED "2.1.207"
# (conftest.CLAUDE_PANE_COMMAND), and the pane carries the ``Type something.``
# affordance — so under the pre-fix flag × license × affordance predicate BOTH
# tests below said "send your answer as text". The executor refuses a scrolled
# pane (``options_complete`` False), so the honest copy is nav-only.


@pytest.mark.asyncio
async def test_notice1_partial_pane_is_nav_only_even_when_licensed(
    scenario: ScenarioHarness,
):
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    # A MULTI-question side file: the single-Q swap (notice 2) declines, so
    # notice 1 survives.
    _write_side_file(_multi_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Only options" in picker
    assert _NAV_ONLY in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_notice1_unlicensed_version_is_nav_only_too(
    scenario: ScenarioHarness,
):
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    scenario.tmux.set_pane_command(wid, "9.9.9")  # un-characterized CC release
    _write_side_file(_multi_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Only options" in picker
    assert _NAV_ONLY in picker
    assert _TEXT_SUFFIX not in picker


# ── Notice 2: the recovered-side-file "Tap-to-select is off" line ─────────────


@pytest.mark.asyncio
async def test_notice2_recovered_swap_is_nav_only_even_when_licensed(
    scenario: ScenarioHarness,
):
    """The swapped body renders the COMPLETE side-file option list, but the
    LIVE pane — what the executor's fresh parse will see — is still scrolled,
    so a text answer would be refused: the notice must stay nav-only."""
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=True)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert "Tap-to-select is off on a scrolled screen" in picker
    assert _NAV_ONLY in picker
    assert _TEXT_SUFFIX not in picker


# ── The anchor leg (r3 P2-1(b)) + the over-suppression control ────────────────


def _complete_pane() -> str:
    return (
        f"{_TITLE}\n"
        "\n"
        f"❯ 1. {_LABELS[0]}\n"
        f"  2. {_LABELS[1]}\n"
        f"  3. {_LABELS[2]}\n"
        "  4. Type something.\n"
        "\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )


@pytest.mark.asyncio
async def test_complete_pane_WITHOUT_side_file_is_nav_only(
    scenario: ScenarioHarness,
):
    """r3 P2-1(b), the FLIPPED r1 control: a complete licensed pane with NO
    PreToolUse side file constructs EXACTLY the state the executor's
    ``_observe`` declines (``surface_anchor_lost``) — the send the old copy
    advertised would be refused, so the hint must point at the buttons. The
    predicate consults the SAME anchor reader the executor trusts
    (``read_surface_anchor`` → ``peek_surface_anchor_for_window``), driven
    REAL here: no side file exists for the bound session."""
    wid = _bind(scenario, _complete_pane())
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_NO_FREE_TEXT in picker
    assert free_text.HINT_FREE_TEXT not in picker
    assert "Only options" not in picker
    assert "Tap-to-select" not in picker


@pytest.mark.asyncio
async def test_complete_pane_WITH_live_side_file_still_promises_free_text(
    scenario: ScenarioHarness,
):
    """The over-suppression control: executor parity must not over-suppress.
    A complete contiguous single-select with the affordance, a licensed
    version, AND a live consistent side file (the anchor the executor
    requires — the harness's ``bind_thread`` writes the fresh
    ``session_map.json`` entry the anchor reader resolves through) renders
    the ``card_hint`` free-text promise."""
    wid = _bind(scenario, _complete_pane())
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=False)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_FREE_TEXT in picker
    assert "Only options" not in picker
    assert "Tap-to-select" not in picker


# ── Notice 3: the rescue "live screen is busy" line ───────────────────────────
#
# The third notice ("⚠️ The live screen is busy … buttons are disabled …") fires
# ONLY on a RESCUE render — a pane whose AUQ chrome ``extract_interactive_content``
# still recognizes yet whose 500-line-scrollback structured parse yields no
# options (``dispatch_trusted`` False, not the partial-pane shape). That coupling
# cannot be synthesized deterministically through the live ``handle_interactive_ui``
# gate (a pane parseable enough to be recognized as AUQ also parses its options,
# so it is never a rescue). It composes its suffix from the SAME ``_nav_suffix``
# local the render computes ONCE per call — the identical executor-parity
# predicate pinned above for notices 1 and 2 and unit-tested in
# ``test_free_text_parser`` (a rescue's unparseable pane yields a form with no
# complete options ⇒ nav-only by the same legs). A per-render divergence
# between the three notices is therefore unreachable by construction.
