"""GH #54 W5 — the card copy is a DRY-RUN of the free-text executor (r1→r4).

Three dishonest notices (plus ``card_hint``) used to promise "send your answer
(as text)" where the executor would REFUSE the send. Three predicate rounds —
flag × license × affordance (r1), a mirrored gate list (r2), a shared shape
gate + anchor EXISTENCE (r3) — each left reproduced over-advertising states;
the r4 class diagnosis is that ANY parallel predicate loses. The copy is now
decided by ``free_text.advertises_free_text``, which DRY-RUNS
``free_text.plan_pre_keystroke`` — the ONE callable ``try_answer`` consumes
for its own pre-keystroke phase — on the SAME raw inputs the executor would
see: the RAW captured pane pair (never the resolver-MERGED form, which
restores missing options / forces completeness / synthesizes option-1's
cursor), the executor's anchor reader with anchor–pane AGREEMENT, the
stranded-draft brake, the Claude proof-of-life, and the version license.

These drive the public seam (``handle_interactive_ui``) with a fake bot /
fake tmux, FRESH ``side_file_ok`` shapes (the r3 tests concealed the merged-
form hole by using AGED side files, which force the pane/bail form), and the
REAL predicate + reader end-to-end. The delegation pins (both consumers call
the SAME function) live in ``test_free_text_parser.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from cctelegram.handlers import auq_source, free_text, interactive_ui
from cctelegram.tmux_manager import tmux_manager as _tmux_singleton
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
    _tmux_singleton.reset_stranded_drafts_for_tests()
    yield
    free_text.set_enabled(True)
    _tmux_singleton.reset_stranded_drafts_for_tests()


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


# ── The r4 reproduced states — FRESH side_file_ok shapes (never aged) ─────────
#
# The r3 scenarios used AGED side files, which force the pane/bail form and so
# CONCEALED the merged-form hole: on a FRESH consistent side file the resolver
# returns side_file_ok and ``resolve_ask_form`` restores missing options,
# forces ``options_complete=True`` and synthesizes option-1's cursor — the
# probe-confirmed premise each test below asserts before its copy assertion.
# The executor reparses the RAW pane at send time and refuses these shapes,
# so the copy must be nav-only whatever the merged form claims.


@pytest.mark.asyncio
async def test_r4a_partial_raw_pane_with_FRESH_side_file_is_nav_only(
    scenario: ScenarioHarness,
):
    """(i) The reviewer shape: raw pane shows [2,3] (incomplete) but the FRESH
    consistent side file merges to a complete cursored form — the r3
    form-based predicate advertised here while the executor refuses."""
    pane = _partial_pane([(2, _LABELS[1]), (3, _LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=False)

    # Premise guard — the merged form IS the over-claiming shape.
    r = auq_source.resolve_auq_source_for_render(wid, pane)
    assert r.decision == "side_file_ok"
    assert r.form is not None and r.form.options_complete is True
    assert [o.number for o in r.form.options] == [1, 2, 3]

    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_NO_FREE_TEXT in picker
    assert free_text.HINT_FREE_TEXT not in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_r4a_cursorless_raw_pane_with_FRESH_side_file_is_nav_only(
    scenario: ScenarioHarness,
):
    """(ii) The raw pane parses NO cursor; the merged form SYNTHESIZES
    option-1's cursor. The executor's fresh parse has no proven cursor ⇒ the
    send would bail ⇒ nav-only copy."""
    pane = (
        f"{_TITLE}\n"
        "\n"
        f"  1. {_LABELS[0]}\n"
        f"  2. {_LABELS[1]}\n"
        f"  3. {_LABELS[2]}\n"
        "  4. Type something.\n"
        "\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    wid = _bind(scenario, pane)
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=False)

    # Premise guard — the merged form carries a SYNTHESIZED cursor.
    r = auq_source.resolve_auq_source_for_render(wid, pane)
    assert r.form is not None and any(o.cursor for o in r.form.options)

    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_NO_FREE_TEXT in picker
    assert free_text.HINT_FREE_TEXT not in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_r4b_mismatched_side_file_with_complete_pane_is_nav_only(
    scenario: ScenarioHarness,
):
    """(iii) A VALID side file whose labels do not describe this pane: the
    anchor EXISTS (the r3 leg passed) but the executor's ``derive_identity``
    requires anchor–pane AGREEMENT and declines — a stable state, not the
    render→send race."""
    wid = _bind(scenario, _complete_pane())
    _write_side_file(
        _single_q_input(["X) other", "Y) other", "Z) other"], title="Different?"),
        aged=False,
    )
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_NO_FREE_TEXT in picker
    assert free_text.HINT_FREE_TEXT not in picker
    assert _TEXT_SUFFIX not in picker


@pytest.mark.asyncio
async def test_r4c_braked_window_with_fresh_agreeing_card_is_nav_only(
    scenario: ScenarioHarness,
):
    """(iv) The stranded-draft brake: every send is refused while it is up, so
    a fresh card must not advertise a text answer. The brake is part of the
    executor's pre-keystroke phase, which the copy dry-runs."""
    wid = _bind(scenario, _complete_pane())
    _write_side_file(_single_q_input(_LABELS, title=_TITLE), aged=False)
    _tmux_singleton.mark_window_stranded_draft(wid)
    await _render(scenario, wid)
    picker = _picker_text(scenario)
    assert free_text.HINT_NO_FREE_TEXT in picker
    assert free_text.HINT_FREE_TEXT not in picker
    assert _TEXT_SUFFIX not in picker
