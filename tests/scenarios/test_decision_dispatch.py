"""Scenario coverage for the Stage B2.3 tappable Decision dispatch lane.

Drives the public Telegram seam end-to-end against fake tmux for a LICENSED
folder-trust Decision card (the v2.1.204 fixture): render mints ``dcp:`` option
buttons, a tap navigates→verifies→Enter the live pane and terminates the card in
the inert "✅ … sent" state. Plus the flag-OFF inertness pin (no ``dcp:`` buttons,
the callback declines) and the bail-re-mint net (a pre-commit bail leaves the card
re-rendered with FRESH tokens — never a dead card).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import decision_token, interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_ENTER, CB_DECISION_PICK
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from tests.conftest import ScenarioHarness, make_update_callback, render_cursor

pytestmark = pytest.mark.scenario

_SESSION_ID = "33333333-3333-4333-8333-333333333333"
_TRUST = (
    Path(__file__).parents[1]
    / "cctelegram"
    / "fixtures"
    / "decision_trust_folder_v2.1.204.txt"
).read_text()
_RESOLVED_PANE = "user@host repo % \n"
_LICENSED = "2.1.204"


@pytest.fixture(autouse=True)
def _flags(scenario: ScenarioHarness) -> Any:
    # Depends on ``scenario`` so it runs AFTER the ``fresh_handler_state`` reset
    # (which clears the terminal_parser + decision_token flags); otherwise the
    # reset would wipe these back OFF before the test body runs.
    terminal_parser.set_decision_cards_enabled(True)
    decision_token.set_decision_dispatch_enabled(True)
    yield
    decision_token.reset_for_tests()
    terminal_parser.reset_for_tests()


class _DecisionInstaller:
    """Cursor-aware fake of the folder-trust Decision pane for the dispatch tap.

    ``Down``/``Up`` move the ❯ (clamped 1..N); ``Enter`` resolves the prompt (→
    ``_RESOLVED_PANE``). ``moves=False`` freezes the cursor (a quoted / dead pane →
    the wiggle fails → ``not_advanced``). ``command`` is the FRESH
    ``pane_current_command`` the §2b tap gate reads."""

    def __init__(
        self,
        scenario: ScenarioHarness,
        wid: str,
        pane: str,
        *,
        n: int,
        moves: bool = True,
        command: str = _LICENSED,
    ) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self._pane = pane
        self._n = n
        self._moves = moves
        self._command = command
        self.cursor = 1
        self.resolved = False

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid or self.resolved:
            return window_id in self._fake.windows
        if self._moves:
            if keys == "Down":
                self.cursor = min(self.cursor + 1, self._n)
            elif keys == "Up":
                self.cursor = max(self.cursor - 1, 1)
            elif keys == "Enter":
                self.resolved = True
        return window_id in self._fake.windows

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self._wid:
            return ""
        return (
            _RESOLVED_PANE if self.resolved else render_cursor(self._pane, self.cursor)
        )

    async def pane_current_command(self, window_id: str) -> str | None:
        return self._command

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _DecisionInstaller:
        for target in (_real_tmux, self._fake):
            monkeypatch.setattr(target, "send_keys", self.send_keys, raising=False)
            monkeypatch.setattr(
                target, "capture_pane", self.capture_pane, raising=False
            )
            monkeypatch.setattr(
                target, "pane_current_command", self.pane_current_command, raising=False
            )
        return self


def _bind(scenario: ScenarioHarness, *, command: str = _LICENSED) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=_TRUST)
    scenario.tmux.windows[wid].pane_current_command = command  # cached mint gate
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_parser=terminal_parser,
    )


async def _render(scenario: ScenarioHarness, wid: str) -> bool:
    return await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _tap(scenario: ScenarioHarness, callback_data: str) -> None:
    update = make_update_callback(
        callback_data, thread_id=42, user_id=scenario.user_id, chat_id=scenario.chat_id
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )


def _latest_keyboard(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _dcp_buttons(cbs: list[str]) -> list[str]:
    return [c for c in cbs if c.startswith(CB_DECISION_PICK)]


@pytest.mark.asyncio
async def test_licensed_decision_tap_dispatches_and_finalizes(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = _bind(scenario)
    assert await _render(scenario, wid)

    cbs = _latest_keyboard(scenario)
    dcp = _dcp_buttons(cbs)
    assert len(dcp) == 2, f"licensed folder-trust card must mint 2 dcp buttons: {cbs}"
    # The nav keyboard is generation-suffixed (§5b(c)/O-6).
    enter_cb = next(c for c in cbs if c.startswith(CB_ASK_ENTER))
    assert ":g" in enter_cb

    _DecisionInstaller(scenario, wid, _TRUST, n=2).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    # Tap option 1 (cursor already there → delta=0 → the wiggle) then Enter.
    tap = next(c for c in dcp if c.split(":")[3] == "1")
    await _tap(scenario, tap)

    keys = [k for _w, k, _e, _l in scenario.tmux.sent_keys]
    assert "Enter" in keys, keys
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)
    # §5b(b) dispatch-terminal teardown: the surface is popped (a stale raw-nav
    # tap now fails), and the card was edited to the inert "✅ … sent" state.
    assert not interactive_ui.has_interactive_surface(scenario.user_id, 42)
    assert decision_token.current_nav_generation(wid) is None
    assert any(
        "✅" in str(s.kwargs.get("text") or "")
        and "sent" in str(s.kwargs.get("text") or "")
        for s in scenario.bot.sent
    )


@pytest.mark.asyncio
async def test_flag_off_decision_is_dispatch_inert(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision_token.set_decision_dispatch_enabled(False)  # dispatch OFF, cards ON
    wid = _bind(scenario)
    assert await _render(scenario, wid)

    cbs = _latest_keyboard(scenario)
    assert _dcp_buttons(cbs) == [], "flag-OFF Decision card must mint NO dcp buttons"

    # A stale flag-ON-epoch dcp tap declines cleanly (dispatch provably inert).
    _DecisionInstaller(scenario, wid, _TRUST, n=2).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    await _tap(
        scenario,
        f"{CB_DECISION_PICK}deadbeef:11ff01bb:1:abc123abc123",
    )
    assert scenario.tmux.sent_keys == [], "no keystroke on a flag-OFF dispatch"
    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)


@pytest.mark.asyncio
async def test_decision_bail_at_gate_remints_fresh_tokens(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-commit bail (a frozen / quoted cursor → the wiggle fails →
    ``not_advanced``) must leave the card RE-RENDERED with FRESH tokens that
    re-validate against the live pane — never a dead card."""
    wid = _bind(scenario)
    assert await _render(scenario, wid)
    old = _dcp_buttons(_latest_keyboard(scenario))
    old_token = old[0].split(":")[-1]

    # A frozen-cursor pane: the wiggle can't move the ❯ → bail before Enter.
    _DecisionInstaller(scenario, wid, _TRUST, n=2, moves=False).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, old[0])  # tap option 1 → wiggle fails → not_advanced

    assert "Enter" not in [k for _w, k, _e, _l in scenario.tmux.sent_keys]
    # The card is still live (re-rendered, not torn down) with a FRESH token.
    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)
    new = _dcp_buttons(_latest_keyboard(scenario))
    assert len(new) == 2
    new_token = new[0].split(":")[-1]
    assert new_token != old_token, "the re-render must mint a fresh token"
    assert decision_token.peek(new_token) is not None, "fresh token must be live"
    assert decision_token.peek(old_token) is None, "the consumed token is burned"


@pytest.mark.asyncio
async def test_sibling_replay_after_dispatch_is_dead(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3(3) sibling-burn: after a CONFIRMED dispatch of option 1, a replayed tap
    of the sibling option-2 callback_data finds only a tomb — no keystroke."""
    wid = _bind(scenario)
    assert await _render(scenario, wid)
    dcp = _dcp_buttons(_latest_keyboard(scenario))
    tap1 = next(c for c in dcp if c.split(":")[3] == "1")
    tap2 = next(c for c in dcp if c.split(":")[3] == "2")

    _DecisionInstaller(scenario, wid, _TRUST, n=2).install(monkeypatch)
    await _tap(scenario, tap1)  # dispatch option 1 → confirmed
    assert not interactive_ui.has_interactive_surface(scenario.user_id, 42)

    scenario.tmux.sent_keys.clear()
    await _tap(scenario, tap2)  # replay the burned sibling
    assert scenario.tmux.sent_keys == [], "a burned sibling must send no key"


@pytest.mark.asyncio
async def test_ledger_dispatched_row_answers_already_received_after_restart(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8 restart idempotency: a duplicate tap whose ledger key is already
    ``dispatched`` answers "already received" — never a second dispatch — even
    when the in-memory token store was wiped (a bot restart)."""
    from cctelegram.handlers import auq_ledger

    wid = _bind(scenario)
    assert await _render(scenario, wid)
    dcp = _dcp_buttons(_latest_keyboard(scenario))
    tap1 = next(c for c in dcp if c.split(":")[3] == "1")
    route_hash, fp8, opt = tap1.removeprefix(CB_DECISION_PICK).split(":")[:3]
    key = auq_ledger.make_ledger_key(route_hash, fp8, int(opt))
    # Seed a ``dispatched`` row for this key (a prior confirmed pick).
    auq_ledger.record(
        key,
        state="dispatched",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint=fp8 + "0" * 8,
        option_number=int(opt),
        option_label="Yes, I trust this folder",
    )
    _DecisionInstaller(scenario, wid, _TRUST, n=2).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    update = make_update_callback(
        tap1, thread_id=42, user_id=scenario.user_id, chat_id=scenario.chat_id
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    assert scenario.tmux.sent_keys == [], "a dispatched key must not re-dispatch"
    answered = [
        c.args[0] for c in update.callback_query.answer.await_args_list if c.args
    ]
    assert any("already received" in str(a) for a in answered), answered


@pytest.mark.asyncio
async def test_pre_deploy_unsuffixed_gate_nav_refuses_after_restart(
    scenario: ScenarioHarness,
) -> None:
    """Review r1 P1 (BOTH engines), the Hermes scenario pin: a PRE-B2.3 gate card
    (raw un-suffixed ``aq:enter:@N`` callbacks) whose persisted interactive
    surface survived a restart — while the nav-generation registry (in-memory)
    is ALWAYS wiped — must refuse BEFORE any key, never raw-dispatch into the
    live Decision pane."""
    wid = _bind(scenario)
    assert await _render(scenario, wid)
    assert decision_token.current_nav_generation(wid) is not None

    # Simulate the restart/deploy: the in-memory registry (and tokens) are wiped;
    # the persisted interactive surface (interactive_state.json) survives; the
    # published PRE-B2.3 card still carries its raw un-suffixed callbacks.
    decision_token.reset_for_tests()
    decision_token.set_decision_dispatch_enabled(True)
    assert decision_token.current_nav_generation(wid) is None
    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)

    scenario.tmux.sent_keys.clear()
    update = make_update_callback(
        f"{CB_ASK_ENTER}{wid}",  # the pre-deploy un-suffixed shape
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
    assert scenario.tmux.sent_keys == [], "no raw key into a live gate pane"
    answered = [
        c.args[0] for c in update.callback_query.answer.await_args_list if c.args
    ]
    assert any("Card refreshed" in str(a) for a in answered), answered


@pytest.mark.asyncio
async def test_clear_invalidates_decision_tokens_same_fp_reraise_refuses(
    scenario: ScenarioHarness,
) -> None:
    """Review r1 P2-A (Hermes lifecycle scenario): /clear tears down the route's
    Decision tokens + nav generation, so a stale ``dcp:`` tap on a SAME-fingerprint
    re-raised prompt (same-cwd folder-trust after /clear, within the 300s token
    TTL) REFUSES — extractor parity + fingerprint + license would all pass, so
    only the teardown stops real keys reaching the NEW session."""
    from cctelegram import bot as bot_module
    from tests.conftest import make_update_command

    wid = _bind(scenario)
    assert await _render(scenario, wid)
    dcp = _dcp_buttons(_latest_keyboard(scenario))
    assert len(dcp) == 2
    stale_tap = dcp[0]
    assert decision_token.current_nav_generation(wid) is not None

    # Drive /clear through the public command seam (bot.forward_command_handler).
    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    # The /clear seam invalidated the Decision tokens + nav generation.
    token = stale_tap.removeprefix(CB_DECISION_PICK).split(":")[-1]
    assert decision_token.peek(token) is None, "/clear must kill the dcp tokens"
    assert decision_token.current_nav_generation(wid) is None

    # The NEW session re-raises the byte-identical (same-fingerprint) prompt.
    scenario.tmux.set_pane(wid, _TRUST)
    scenario.tmux.sent_keys.clear()
    tap_update = make_update_callback(
        stale_tap, thread_id=42, user_id=scenario.user_id, chat_id=scenario.chat_id
    )
    await dispatch_callback(
        tap_update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    # No nav / Enter / digit keystroke reached the new session's pane.
    assert scenario.tmux.sent_keys == [], scenario.tmux.sent_keys
