"""GH #54 W4 scenario — a preview AUQ renders details-before-card at the seam.

Black-box at the render seam (the poller's ``handle_interactive_ui``): a real
2.1.197 side-by-side PREVIEW picker + its PreToolUse side file must resolve
``side_file_ok`` (labels consistent under the W1.2 wrap-canonical rule, the pane
parsed WITH ANSI via the capture spine) so that:

  (a) the 📋 full-details message (option descriptions) posts BEFORE the short
      selection card;
  (b) the selection card carries labels-only ``aqp:`` option buttons (one per
      real option), never the descriptions.

Without the ANSI capture spine the 2.1.197 pane is chevron-less and would parse
0 options → the pre-W1 ``bail_partial`` raw dump (no ctx card, no buttons); this
pins the fixed path at the public render seam.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cctelegram.handlers import interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_FIX = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_SIDE_FILE = json.loads((_FIX / "auq_preview_side_file.json").read_text())
_SESSION_ID = _SIDE_FILE["session_id"]
_PLAIN = (_FIX / "auq_preview_sidebyside_v2.1.197.aligned.txt").read_text()
_ANSI = (_FIX / "auq_preview_sidebyside_v2.1.197.ansi.txt").read_text()
_LABELS = [o["label"] for o in _SIDE_FILE["tool_input"]["questions"][0]["options"]]
_DESCRIPTIONS = [
    o["description"] for o in _SIDE_FILE["tool_input"]["questions"][0]["options"]
]


def _write_side_file() -> None:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pending / f"{_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-preview",
                "written_at": time.time(),
                "tool_input": _SIDE_FILE["tool_input"],
            }
        )
    )


def _bind(scenario: ScenarioHarness) -> str:
    wid = scenario.add_window(
        window_name="repo", cwd="/repo", pane_text=_PLAIN, pane_text_ansi=_ANSI
    )
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


@pytest.mark.asyncio
async def test_preview_auq_posts_details_before_labels_only_card(
    scenario: ScenarioHarness,
) -> None:
    _bind(scenario)
    _write_side_file()

    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        scenario.session_manager.thread_bindings[scenario.user_id][42],
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )

    sent = scenario.bot.sent
    # The card is the message carrying the inline keyboard.
    card_idx = next(
        i for i, s in enumerate(sent) if s.kwargs.get("reply_markup") is not None
    )
    details_idx = next(
        i
        for i, s in enumerate(sent)
        if str(s.kwargs.get("text") or "").startswith("📋 AskUserQuestion")
    )
    # (a) details BEFORE the card.
    assert details_idx < card_idx, "the 📋 details must post before the short card"

    # (a cont.) the details carry the option DESCRIPTIONS.
    details = str(sent[details_idx].kwargs.get("text") or "")
    for desc in _DESCRIPTIONS:
        assert desc[:30] in details

    # (b) the card is a SHORT selection card — labels-only aqp: buttons, one per
    # real option, and NO descriptions in the card body.
    markup = sent[card_idx].kwargs.get("reply_markup")
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    picks = [cb for cb in callbacks if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == len(_LABELS), (picks, _LABELS)
    card_text = str(sent[card_idx].kwargs.get("text") or "")
    for desc in _DESCRIPTIONS:
        assert desc[:30] not in card_text
