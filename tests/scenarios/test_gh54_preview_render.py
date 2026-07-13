"""GH #54 W4 scenarios — preview AUQ render + the REAL ``aqp:`` tap path.

Black-box at the public seams:

  * the RENDER seam (the poller's ``handle_interactive_ui``): a real 2.1.197
    side-by-side PREVIEW picker + its PreToolUse side file must resolve
    ``side_file_ok`` (labels consistent under the W1.2 wrap-canonical rule, the
    pane parsed WITH ANSI via the capture spine) so that (a) the 📋 full-details
    message posts BEFORE the short selection card and (b) the card carries
    labels-only ``aqp:`` buttons — without the spine the 2.1.197 pane is
    chevron-less, parses 0 options, and fell to the pre-W1 ``bail_partial`` raw
    dump (no ctx card, no buttons);
  * the CALLBACK seam (wave-2 review P1): tapping an ``aqp:`` button on a
    chevron-less 2.1.207 wrap-label preview drives the ACTUAL handler wiring —
    ``dispatch_callback`` → ``validate_and_consume`` (whose inner ``_capture``
    must request ANSI: a plain frame parses NO cursor there, so the token was
    CONSUMED and the dispatch then dead-tapped ``cursor_unknown`` forever) →
    nav → verify → Enter → confirmed ``dispatched``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cctelegram.terminal_parser as terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import auq_ledger, interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

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


# ── the REAL aqp: tap path on a chevron-less wrap preview (wave-2 review P1) ──

_WRAP_C1_ANSI = (_FIX / "auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt").read_text()
_WRAP_C2_ANSI = (_FIX / "auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt").read_text()
_WRAP_SESSION_ID = "44444444-4444-4444-8444-444444444444"
# The AUTHORITY labels (side-file truth). Option 2's pane reconstruction is
# LOSSY (mid-word wrap inserts spurious spaces), so it matches ONLY via the
# W1.2 wrap-canonical leg — the shape the whole lane exists for.
_WRAP_AUTHORITY = [
    "Hyperconsolidated observability megadashboard variant",
    "Supercalifragilisticexpialidociousantidisestablishmentarianism dashboard",
    "Short label",
]
_RESOLVED_PANE = "user@host repo % \n"


def _wrap_plain(ansi: str) -> str:
    cap = terminal_parser.normalize_capture(ansi)
    assert cap is not None
    return cap.plain


class _SgrAdvancingFake:
    """Cursor-aware ANSI-serving fake: Down moves cursor1→cursor2, Enter resolves.

    The cursor on these REAL 2.1.207 frames is SGR-only (no ``❯``), so every
    consumer that needs the cursor must have requested ANSI — a plain-only
    capture anywhere in the tap path parses NO cursor and the dispatch bails.
    """

    def __init__(self, scenario: ScenarioHarness, wid: str) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self.state = 0  # 0 = cursor1, 1 = cursor2, 2 = resolved

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del scrollback_lines
        if window_id != self._wid:
            return ""
        if self.state == 2:
            return _RESOLVED_PANE
        frame = _WRAP_C1_ANSI if self.state == 0 else _WRAP_C2_ANSI
        return frame if with_ansi else _wrap_plain(frame)

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid:
            return False
        if keys == "Down" and self.state == 0:
            self.state = 1
        elif keys == "Up" and self.state == 1:
            self.state = 0
        elif keys == "Enter" and self.state in (0, 1):
            self.state = 2
        return True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_SgrAdvancingFake":
        for target in (_real_tmux, self._fake):
            monkeypatch.setattr(target, "send_keys", self.send_keys, raising=False)
            monkeypatch.setattr(
                target, "capture_pane", self.capture_pane, raising=False
            )
        return self


def _write_wrap_side_file() -> None:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pending / f"{_WRAP_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _WRAP_SESSION_ID,
                "tool_use_id": "tool-use-wrap-preview",
                "written_at": time.time(),
                "tool_input": {
                    "questions": [
                        {
                            "question": "Which dashboard variant should we build?",
                            "header": "Dashboard",
                            "multiSelect": False,
                            "options": [
                                {"label": label, "description": f"About {label[:20]}"}
                                for label in _WRAP_AUTHORITY
                            ],
                        }
                    ]
                },
            }
        )
    )


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_parser=terminal_parser,
    )


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [
                b.callback_data
                for row in markup.inline_keyboard
                for b in row
                if b.callback_data.startswith(CB_ASK_PICK)
            ]
    raise AssertionError("no reply markup recorded")


@pytest.mark.asyncio
async def test_wrap_preview_tap_dispatches_through_the_real_callback_path(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave-2 review P1: the live ``aqp:`` tap wiring (the executor's OWN
    injected ``_capture``) must see the SGR cursor on a chevron-less preview —
    the pre-fold plain capture validated OK (cursor-blind fingerprint),
    CONSUMED the token, then bailed ``cursor_unknown`` before any keystroke:
    every preview tap dead-tapped indefinitely."""
    wid = scenario.add_window(
        window_name="repo",
        cwd="/repo",
        pane_text=_wrap_plain(_WRAP_C1_ANSI),
        pane_text_ansi=_WRAP_C1_ANSI,
    )
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_WRAP_SESSION_ID
    )
    _write_wrap_side_file()

    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )
    picks = _pick_callbacks(scenario)
    assert len(picks) == 3

    fake = _SgrAdvancingFake(scenario, wid).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    update = make_update_callback(
        picks[1],  # option 2 — the mid-word-wrapped label
        thread_id=42,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )

    # The dispatch REACHED the nav (cursor proven via SGR — never
    # ``cursor_unknown``) and committed: Down (1→2) then Enter, no bare digit.
    keys = [k for _w, k, _e, _l in scenario.tmux.sent_keys]
    assert "Down" in keys, keys
    assert keys[-1] == "Enter", keys
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)
    assert fake.state == 2  # the picker resolved — Enter landed on option 2

    # ...and the ledger holds the terminal ``dispatched`` — the token was spent
    # on a CONFIRMED dispatch, not consumed-then-dead.
    parts = picks[1].removeprefix(CB_ASK_PICK).split(":")
    key = auq_ledger.make_ledger_key(parts[0], parts[1], int(parts[2]))
    row = auq_ledger.lookup(key)
    assert row is not None and row.state == "dispatched", row
