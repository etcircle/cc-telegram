"""Scenario coverage: the CC 2.1.237 multi-question AUQ gutter layout.

The live, deterministic bug this hotfix closes: on Claude Code 2.1.237 EVERY
multi-question AskUserQuestion lost its 📋 details card and got a clipped
one-line picker preamble.

Chain (see ``tests/cctelegram/test_auq_gutter_layout_v2_1_237.py`` for the unit
floor): 2.1.237 draws the question behind a left ``│`` gutter → the parser's
multi-tab title scan keeps ONE gutter-prefixed physical line →
``_record_consistent_with_pane`` step 5.b finds no prefix relation in either
direction → ``title_mismatch`` → ``resolve_auq_source_for_render`` returns a
TRUSTED complete-picker ``bail`` → the ctx-gate maps that to ``bail_no_ctx``
(the ``bail`` rescue branch is gated on ``not dispatch_trusted``, unreachable
here) → NO details card, and the preamble falls back to the same clipped line.

The fix is the gutter canonicalization alone. An "identity proof" override that
would have posted the details card on ANY ``title_mismatch``-only bail whose
side file matched a held ``tool_use_id`` was DROPPED in review as unsound: under
in-place turnover AUQ-A's stamped identity and its side file can BOTH be stale
while the complete pane has already advanced to AUQ-B, so the override would
post A's details beside B's picker — the exact class GH #67 exists to prevent,
with no pane-bindable witness available to separate them (labels-only identity
is the accepted GH #50 residual, and the question-region binder was deleted for
failing injectivity). The trusted bail therefore stays FAIL-CLOSED, pinned by
``test_trusted_bail_on_genuine_mismatch_still_posts_no_ctx_card`` below.

These tests drive the public seam (``handle_interactive_ui``) with the fake bot
/ fake tmux and the REAL captured pane, and assert on ``scenario.bot.sent`` —
no monkeypatch of handler internals in test bodies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram.handlers import auq_source, interactive_ui
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "44444444-4444-4444-8444-444444444444"
_TOOL_USE_ID = "toolu_gutter_multiq"

_GUTTER_PANE = (
    Path(__file__).parent.parent
    / "cctelegram"
    / "fixtures"
    / "auq_multiq_gutter_pane_v2.1.237.txt"
)

# The FULL question Claude Code wrote into the tool_input. The pane renders
# exactly this, wrapped over two gutter lines — the second of which the
# pre-fix one-line clip threw away.
_FULL_QUESTION = (
    "Dynamics CRM integration: which direction? (Full memo in "
    "temp/dynamics_mcp_auth_options_memo.md. Note: metering — Dataverse MCP "
    "calls bill Copilot Credits on the client tenant unless users hold D365 "
    "Premium/M365 Copilot licenses; worth checking their posture regardless.)"
)

# The point where the pre-fix clip severed the question mid-sentence.
_CLIP_BOUNDARY = "Credits on"
# Text present ONLY in the second gutter line.
_CLIPPED_TAIL = "worth checking their posture regardless."

_Q1_OPTIONS = [
    ("Option A spike", "~2-day spike on a real Dataverse tenant."),
    ("Option B direct", "Skip MCP: app-only credential + CallerObjectId."),
    ("Entra SSO variant first", "Add Entra SSO as a DI Copilot login method."),
    ("Park it", "Hold the item; the memo stands as the decision record."),
]


def _pane() -> str:
    return _GUTTER_PANE.read_text()


def _multi_q_input() -> dict[str, Any]:
    """The three-question tool_input the real 2.1.237 tab header advertises."""
    return {
        "questions": [
            {
                "question": _FULL_QUESTION,
                "header": "Dynamics",
                "multiSelect": False,
                "options": [
                    {"label": label, "description": desc} for label, desc in _Q1_OPTIONS
                ],
            },
            {
                "question": "How should we scope the todo list?",
                "header": "Todo scope",
                "multiSelect": False,
                "options": [
                    {"label": "Everything open", "description": "All 66 items."},
                    {"label": "This sprint only", "description": "Just the sprint."},
                ],
            },
            {
                "question": "Authorize the #387 ops settings row now?",
                "header": "#387 ops fix",
                "multiSelect": False,
                "options": [
                    {"label": "Yes, insert it", "description": "One settings row."},
                    {"label": "Wait for the code fix", "description": "Ship together."},
                ],
            },
        ]
    }


def _write_side_file(
    tool_input: dict[str, Any], *, tool_use_id: str = _TOOL_USE_ID
) -> Path:
    """A FRESH PreToolUse side file (inside the 300s render read-TTL)."""
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": tool_use_id,
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="di-copilot", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42, wid, display_name="di-copilot", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


async def _render(scenario: ScenarioHarness, wid: str) -> bool:
    return await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


def _unescape(text: str) -> str:
    """Strip MarkdownV2 escapes so substring needles (which carry ``)`` / ``.``)
    match the rendered card text the fake bot stores."""
    return text.replace("\\", "")


def _sent_texts(scenario: ScenarioHarness) -> list[str]:
    return [_unescape(str(s.kwargs.get("text") or "")) for s in scenario.bot.sent]


def _details_indexes(scenario: ScenarioHarness) -> list[int]:
    return [
        i
        for i, text in enumerate(_sent_texts(scenario))
        if text.startswith("📋 AskUserQuestion — full details")
    ]


def _picker_index(scenario: ScenarioHarness) -> int:
    for i in range(len(scenario.bot.sent) - 1, -1, -1):
        if scenario.bot.sent[i].kwargs.get("reply_markup") is not None:
            return i
    raise AssertionError("no picker card recorded")


# ── THE main-RED seam test ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gutter_multiq_posts_details_card_before_the_picker(
    scenario: ScenarioHarness,
) -> None:
    """THE main-RED seam test.

    A real 2.1.237 multi-question gutter pane + a fresh consistent side file.

    RED on main: ``_details_indexes`` is ``[]`` — the details card is never
    posted, because the trusted complete-picker bail hard-maps to
    ``bail_no_ctx``.

    GREEN after Part A: the side file reconciles with the pane
    (``side_file_ok``), so the details card posts, and it PRECEDES the picker
    (the details-before-picker invariant).
    """
    pane = _pane()
    wid = _bind(scenario, pane)
    _write_side_file(_multi_q_input())

    # Premise guard (holds on BOTH main and the fix, so the load-bearing
    # assertion below is what goes RED): the pane is a COMPLETE, TRUSTED
    # picker — the branch whose bail posts no ctx card at all.
    resolved = auq_source.resolve_auq_source_for_render(wid, pane)
    assert resolved.dispatch_trusted is True

    assert await _render(scenario, wid)

    # (1) the details card IS posted, exactly once.
    #     RED on main: ``details == []`` (title_mismatch → bail → bail_no_ctx).
    details = _details_indexes(scenario)
    assert len(details) == 1

    # The side file now reconciles with the pane, so the render is trusted from
    # the side file rather than bailing to the pane.
    assert resolved.decision == "side_file_ok"

    # (2) details BEFORE picker — the ordering invariant.
    assert details[0] < _picker_index(scenario)

    # (3) it carries all three questions and Q1's per-option descriptions.
    body = _sent_texts(scenario)[details[0]]
    assert "How should we scope the todo list?" in body
    assert "Authorize the #387 ops settings row now?" in body
    assert "Entra SSO variant first" in body
    assert "~2-day spike on a real Dataverse tenant." in body
    # The details card carries the question UNCLIPPED — including the tail the
    # pane's one-physical-line title lost.
    assert _CLIPPED_TAIL in body


@pytest.mark.asyncio
async def test_gutter_picker_preamble_carries_the_whole_question(
    scenario: ScenarioHarness,
) -> None:
    """The bug's SECOND face: the picker preamble.

    With no side file at all the render falls through to the pure PANE form, so
    the preamble is whatever the parser produced.

    RED on main: the preamble is the parser's one-PHYSICAL-LINE clip — it still
    carries the ``│`` gutter and stops dead at "Credits on", mid-sentence.

    GREEN after Part B: the gutter is gone and the preamble is drawn from the
    WHOLE joined question, so it reaches into the second physical line.

    NOTE the preamble is still capped at ``_SELCARD_TITLE_MAX_CHARS`` (200) —
    that is a DELIBERATE pre-existing UX decision (a long question must not push
    the option rows off the card; see ``test_auq_selcard_preamble_clip.py``) and
    this hotfix does not touch it. The difference is WHAT gets clipped: a clip
    of the full question, not a clip of an already-clipped physical line. The
    complete question lives in the 📋 details card.
    """
    pane = _pane()
    wid = _bind(scenario, pane)  # deliberately NO side file

    assert await _render(scenario, wid)

    picker = _sent_texts(scenario)[_picker_index(scenario)]
    assert "│" not in picker  # the gutter is gone
    # Text that exists ONLY on the second gutter line — proof the preamble is
    # no longer severed at the physical-line boundary.
    assert "the client tenant unless users hold D365" in picker
    assert not picker.split("\n\n")[1].endswith(_CLIP_BOUNDARY)


# ── the trusted bail stays FAIL-CLOSED (the dropped-Part-C pin) ───────────────


@pytest.mark.asyncio
async def test_trusted_bail_on_genuine_mismatch_still_posts_no_ctx_card(
    scenario: ScenarioHarness,
) -> None:
    """A complete-picker (TRUSTED) bail posts NO details card, full stop.

    Pins the review decision to drop the identity-proof override. A side file
    whose question no longer reconciles with the pane is exactly the shape that
    an in-place AUQ-A→AUQ-B turnover produces — a STALE record whose
    ``tool_use_id`` may still match the identity the bot holds. Posting its
    details beside the live picker would show the user the WRONG question's
    options, so the contract is: on a trusted bail the stale side-file card is
    never posted, whatever the reason code or the identity says.

    Also green on bare main — its value is pinning that the hotfix does not
    widen the bail.
    """
    pane = _pane()
    wid = _bind(scenario, pane)

    drifted = _multi_q_input()
    drifted["questions"][0]["question"] = "A question the pane no longer renders"
    _write_side_file(drifted)
    # The bot holds an identity that MATCHES the side file's — under the dropped
    # override this alone would have unlocked the card.
    interactive_ui._last_auq_tool_use_id[wid] = _TOOL_USE_ID

    resolved = auq_source.resolve_auq_source_for_render(wid, pane)
    assert resolved.decision == "bail"
    assert resolved.dispatch_trusted is True
    assert resolved.reason == "bail_title_mismatch"

    assert await _render(scenario, wid)

    assert _details_indexes(scenario) == []
