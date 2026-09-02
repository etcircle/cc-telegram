"""Scenario: GH #88 — the CC 2.1.258 folder-trust lane, at the Telegram seam.

Two things are driven end-to-end through the REAL handler stack against the REAL
2.1.258 rig captures:

  * the REDESIGNED prompt (unnumbered, inverted, default cursor on the
    DESTRUCTIVE ``No, exit`` row) yields a 🔐 card whose ✅ tap navigates DOWN to
    the affirmative row, verifies it landed, and only then sends Enter — plus the
    DESTRUCTIVE-DEFAULT pin: a frozen cursor sends ZERO Enters;
  * §F's ``UNKNOWN_PROMPT`` HOLD: a live confirmation prompt no recognizer owns
    keeps the window on the trust ceiling with a display-only advisory instead of
    burning the registration budget and killing it, and recovers to the licensed
    🔐 card when the complete frame arrives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.config import config
from cctelegram.handlers import decision_token, trust_flow
from cctelegram.handlers.callback_data import CB_DIR_CONFIRM, CB_TRUST_PICK
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    picker_entry,
)
from tests.conftest import (
    IDLE_PANE_V2_1_207,
    ScenarioHarness,
    make_update_real_callback,
    make_update_text,
)

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_ARRIVAL = (_FIXTURES / "folder_trust_arrival_plain_v2.1.258.txt").read_text()
_POSTDOWN = (_FIXTURES / "folder_trust_postdown_plain_v2.1.258.txt").read_text()
_AFTER_ACCEPT = (_FIXTURES / "trust_after_accept_repl_v2.1.258.txt").read_text()
# A LIVE confirmation prompt nothing recognizes: the strict footer stands, the
# option rows do not. ``has_live_decision_residue`` fires; no parser does.
_UNKNOWN_PROMPT_PANE = "\n".join(
    [
        "",
        " A brand-new confirmation nobody parses",
        "",
        " Enter to confirm · Esc to cancel",
        "",
        "",
    ]
)

_LICENSED = "2.1.258"
_THREAD = 42


@pytest.fixture(autouse=True)
def _lane_on(scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 30.0)
    monkeypatch.setattr(config, "hook_timeout_override", 0.05)
    monkeypatch.setattr(config, "hook_timeout_extension_s", 0.15)
    monkeypatch.setattr(trust_flow, "SLICE_S", 0.01)
    monkeypatch.setattr(trust_flow, "PANE_POLL_EVERY_S", 0.0)
    monkeypatch.setattr(trust_flow, "GLOBAL_CEILING_MARGIN_S", 0.2)
    terminal_parser.set_decision_cards_enabled(True)
    decision_token.set_trust_card_dispatch_enabled(True)
    decision_token.set_decision_dispatch_force_disabled(False)
    yield
    decision_token.reset_for_tests()
    terminal_parser.reset_for_tests()


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=config,
        terminal_parser=terminal_parser,
    )


async def _tap(scenario: ScenarioHarness, data: str) -> None:
    update = make_update_real_callback(
        data,
        bot=scenario.bot,
        thread_id=_THREAD,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )


class _V258Pane:
    """A cursor-aware fake of the LIVE 2.1.258 folder-trust pane.

    Both cursor positions are REAL rig captures (arrival = ``❯ No, exit``,
    post-Down = ``❯ Yes, I trust this folder``) — never a re-rendered string —
    so the fingerprint the dispatch re-computes is the real one. Arrows WRAP.
    ``moves=False`` freezes the cursor on the DESTRUCTIVE default so the pre-Enter
    verify fails.
    """

    def __init__(
        self,
        scenario: ScenarioHarness,
        wid: str,
        *,
        moves: bool = True,
        pane_override: str | None = None,
    ) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self._moves = moves
        self.command = _LICENSED
        self.cursor = 1  # the DESTRUCTIVE default
        self.committed: int | None = None
        self.pane_override = pane_override
        self.post_commit = _AFTER_ACCEPT

    def _pane(self) -> str:
        if self.committed is not None:
            return self.post_commit
        if self.pane_override is not None:
            return self.pane_override
        return _ARRIVAL if self.cursor == 1 else _POSTDOWN

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid or self.committed is not None:
            return window_id in self._fake.windows
        if keys in ("Down", "Up") and self._moves:
            self.cursor = 2 if self.cursor == 1 else 1  # WRAP, never clamp
        elif keys == "Enter":
            self.committed = self.cursor
        return window_id in self._fake.windows

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        return self._pane() if window_id == self._wid else ""

    async def pane_current_command(self, window_id: str) -> str | None:
        return self.command if window_id == self._wid else None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _V258Pane:
        from cctelegram.tmux_manager import tmux_manager as real_tmux

        for target in (real_tmux, self._fake):
            monkeypatch.setattr(target, "send_keys", self.send_keys, raising=False)
            monkeypatch.setattr(
                target, "capture_pane", self.capture_pane, raising=False
            )
            monkeypatch.setattr(
                target,
                "capture_pane_cancellation_safe",
                self.capture_pane,
                raising=False,
            )
            monkeypatch.setattr(
                target, "pane_current_command", self.pane_current_command, raising=False
            )
        return self


async def _settle(times: int = 12) -> None:
    for _ in range(times):
        await asyncio.sleep(0)
        await asyncio.sleep(0.01)


def _card_edits(scenario: ScenarioHarness) -> list[str]:
    return [
        str(s.kwargs.get("text") or "")
        for s in scenario.bot.sent
        if s.method == "edit_message_text"
    ]


def _card_keyboards(scenario: ScenarioHarness) -> list[list[str]]:
    out: list[list[str]] = []
    for s in scenario.bot.sent:
        markup = s.kwargs.get("reply_markup")
        if s.method == "edit_message_text" and markup is not None:
            out.append([b.callback_data for row in markup.inline_keyboard for b in row])
    return out


async def _start_flow(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    moves: bool = True,
    pane_override: str | None = None,
) -> tuple[str, _V258Pane]:
    scenario.tmux.probe_version_response = _LICENSED
    await bot_module.text_handler(
        make_update_text("hello claude", thread_id=_THREAD), scenario.context
    )
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None and entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    entry[BROWSE_PATH_KEY] = "/repo"
    entry[CARD_CHAT_ID_KEY] = scenario.chat_id
    entry[CARD_MSG_ID_KEY] = 4242
    pane = _V258Pane(scenario, "@0", moves=moves, pane_override=pane_override)
    pane.install(monkeypatch)
    await _tap(scenario, CB_DIR_CONFIRM)
    await _settle()
    return "@0", pane


def _trust_button(scenario: ScenarioHarness) -> str:
    cbs = _card_keyboards(scenario)[-1]
    return next(c for c in cbs if c.startswith(f"{CB_TRUST_PICK}t:"))


# ── The GH #88 regression ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_2_1_258_prompt_yields_a_card_not_a_kill(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue: recognition broke, so the lane regressed to the pre-#65 KILL."""
    wid, _pane = await _start_flow(scenario, monkeypatch)

    assert scenario.tmux.kill_calls == []
    assert wid in scenario.tmux.windows
    assert any("trust this folder" in t.lower() for t in _card_edits(scenario))
    cbs = _card_keyboards(scenario)[-1]
    assert any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    assert any(c.startswith(f"{CB_TRUST_PICK}c:") for c in cbs), cbs
    assert not any("didn't register in time" in t for t in _card_edits(scenario)), (
        _card_edits(scenario)
    )


@pytest.mark.asyncio
async def test_the_tap_navigates_off_the_destructive_default_then_commits(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On 2.1.258 Trust is row TWO and the cursor starts on ``No, exit``: the tap
    must send exactly one ``Down``, verify, and only then ``Enter``."""
    wid, pane = await _start_flow(scenario, monkeypatch)
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, _trust_button(scenario))

    keys = [k for _w, k, _e, _lit in scenario.tmux.sent_keys]
    assert keys == ["Down", "Enter"], keys
    assert not any(k.isdigit() for k in keys), keys  # digits stay forbidden
    assert pane.committed == 2, "Enter must commit the AFFIRMATIVE row, not the default"

    pane.post_commit = IDLE_PANE_V2_1_207
    scenario.session_manager.window_states.clear()
    scenario._write_session_map_entry(wid, "sid-trust-258", "/repo")
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)
    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == wid


@pytest.mark.asyncio
async def test_a_failed_verify_never_enters_on_the_destructive_default(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE higher-consequence invariant of this version: the cursor sits on
    ``No, exit``, so an Enter after a failed verify would EXIT Claude. A frozen
    cursor must produce ZERO Enters and leave the pane untouched."""
    _wid, pane = await _start_flow(scenario, monkeypatch, moves=False)
    old_tap = _trust_button(scenario)
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, old_tap)

    keys = [k for _w, k, _e, _lit in scenario.tmux.sent_keys]
    assert "Enter" not in keys, keys
    assert pane.committed is None
    assert len([k for k in keys if k in ("Up", "Down")]) <= 2, keys
    assert scenario.tmux.kill_calls == []
    # The card re-renders with a FRESH token; the burned one is dead.
    new_tap = _trust_button(scenario)
    assert new_tap != old_tap
    assert decision_token.peek(old_tap.split(":")[-1]) is None


# ── §F — the UNKNOWN_PROMPT hold ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unrecognized_prompt_holds_the_window_instead_of_killing_it(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural degradation: recognition breaking must never kill again.

    The registration budget (50 ms + 150 ms here) elapses many times over during
    the settle, and the window is still alive with an advisory card whose ONLY
    button is the zero-keystroke Cancel.
    """
    wid, pane = await _start_flow(
        scenario, monkeypatch, pane_override=_UNKNOWN_PROMPT_PANE
    )
    await _settle(30)

    assert scenario.tmux.kill_calls == [], "the hold must not kill"
    assert wid in scenario.tmux.windows
    assert pane.committed is None
    assert scenario.tmux.sent_keys == [], "the advisory must never type"
    edits = _card_edits(scenario)
    assert any("can't read" in t for t in edits), edits
    cbs = _card_keyboards(scenario)[-1]
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    assert any(c.startswith(f"{CB_TRUST_PICK}c:") for c in cbs), cbs
    flow = trust_flow.get_flow(scenario.user_id, _THREAD)
    assert flow is not None
    assert flow.card_kind == trust_flow.CARD_KIND_UNKNOWN
    assert flow.trust_deadline is not None, "the TRUST ceiling owns the hold"
    assert flow.token is None, "the advisory must never carry an authorization"


@pytest.mark.asyncio
async def test_cancel_still_kills_from_the_unknown_prompt_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, pane = await _start_flow(
        scenario, monkeypatch, pane_override=_UNKNOWN_PROMPT_PANE
    )
    cancel = next(
        c for c in _card_keyboards(scenario)[-1] if c.startswith(f"{CB_TRUST_PICK}c:")
    )
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, cancel)

    assert scenario.tmux.sent_keys == [], "Cancel must never type into the pane"
    assert pane.committed is None
    assert scenario.tmux.kill_calls == [wid]
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None


@pytest.mark.asyncio
async def test_a_registration_during_the_hold_binds_immediately(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user answered the prompt in tmux: the hold must not delay the bind."""
    wid, pane = await _start_flow(
        scenario, monkeypatch, pane_override=_UNKNOWN_PROMPT_PANE
    )
    await _settle(5)
    assert scenario.tmux.kill_calls == []

    pane.pane_override = IDLE_PANE_V2_1_207
    scenario.session_manager.window_states.clear()
    scenario._write_session_map_entry(wid, "sid-unknown-258", "/repo")
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == wid
    assert scenario.tmux.kill_calls == []


@pytest.mark.asyncio
async def test_unknown_then_trust_recovers_to_a_licensed_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§F(i): a partial frame raises the advisory; the COMPLETE frame that
    follows must FORCE a fresh licensed mint, not sit on the advisory."""
    _wid, pane = await _start_flow(
        scenario, monkeypatch, pane_override=_UNKNOWN_PROMPT_PANE
    )
    await _settle(5)
    flow = trust_flow.get_flow(scenario.user_id, _THREAD)
    assert flow is not None and flow.card_kind == trust_flow.CARD_KIND_UNKNOWN
    assert flow.token is None

    pane.pane_override = None  # the complete 2.1.258 frame draws
    await _settle(10)

    assert flow.card_kind == trust_flow.CARD_KIND_TRUST
    cbs = _card_keyboards(scenario)[-1]
    assert any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    # …and the recovered token really dispatches.
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, _trust_button(scenario))
    assert pane.committed == 2
