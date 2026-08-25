"""Scenario: GH #65 — the folder-trust creation flow, at the Telegram seam.

A brand-new window opens on "Do you trust the files in this folder?", which
never registers in ``session_map``. The pre-#65 flow killed the window and told
the user their session failed. These scenarios drive the REAL handler stack
(directory callback → launch-deferred create → in-pane version probe → the
classifying WAIT task → the 🔐 trust card → the ``tst:`` tap → bind) against
fake tmux + fake bot, using the REAL 2.1.239 / 2.1.241 rig pane captures.
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
from cctelegram.handlers.inbound_aggregator import aggregator_flush_route
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    ensure_picker_entry,
    picker_entry,
)
from tests.conftest import (
    IDLE_PANE_V2_1_207,
    ScenarioHarness,
    make_update_real_callback,
    render_cursor,
    make_update_text,
    make_update_topic_closed,
)

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_TRUST_ARRIVAL = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_POST_ENTER_BLANK = (
    _FIXTURES / "folder_trust_postenter_t1_plain_v2.1.241.txt"
).read_text()
_POST_ESC_SHELL = (_FIXTURES / "folder_trust_postesc_t4_plain_v2.1.241.txt").read_text()
_LICENSED = "2.1.241"
_CLAUDE_CMD = "claude"  # the Linux/WSL pane-command shape
_THREAD = 42


@pytest.fixture(autouse=True)
def _lane_on(scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Production posture for this lane: ceiling armed, trust dispatch ON.

    Depends on ``scenario`` so it runs AFTER ``fresh_handler_state``'s reset
    (which clears the parser + decision_token flags).
    """
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


async def _tap(
    scenario: ScenarioHarness, data: str, *, thread_id: int = _THREAD
) -> Any:
    update = make_update_real_callback(
        data,
        bot=scenario.bot,
        thread_id=thread_id,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    return update


class _TrustPane:
    """A cursor-aware fake of the live folder-trust pane.

    Arrows move the ``❯`` and **WRAP** (the rig finding that contradicts the AUQ
    picker's clamp); ``Enter`` commits the CURSORED option and blanks the pane
    (the real 2.1.241 transitional frame). ``moves=False`` freezes the cursor so
    the pre-Enter verify fails.
    """

    def __init__(
        self,
        scenario: ScenarioHarness,
        wid: str,
        *,
        moves: bool = True,
        command: str = _CLAUDE_CMD,
    ) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self._moves = moves
        self.command = command
        self.cursor = 1
        self.committed: int | None = None
        # What the pane shows once Enter has committed. The REAL 2.1.241 T+1s
        # frame is all-blank (alt-screen cleared, welcome not yet painted); a
        # test that models the settled REPL swaps in the idle input box.
        self.post_commit = _POST_ENTER_BLANK

    def _pane(self) -> str:
        if self.committed is not None:
            return self.post_commit
        # The cursor is relocated on the SAME capture: two folder-trust prompts
        # for DIFFERENT directories have different body-inclusive fingerprints,
        # and the dispatch's identity gate rightly rejects a cross-run frame.
        return render_cursor(_TRUST_ARRIVAL, self.cursor)

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid or self.committed is not None:
            return window_id in self._fake.windows
        if keys == "Down" and self._moves:
            self.cursor = 2 if self.cursor == 1 else 1  # WRAP, never clamp
        elif keys == "Up" and self._moves:
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

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _TrustPane:
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


async def _open_browser(scenario: ScenarioHarness, text: str = "hello claude") -> None:
    """Drive the public seam: first message in an unbound topic."""
    await bot_module.text_handler(
        make_update_text(text, thread_id=_THREAD), scenario.context
    )
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None and entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    entry[BROWSE_PATH_KEY] = "/repo"
    # The card coordinates the trust card will be edited into.
    entry[CARD_CHAT_ID_KEY] = scenario.chat_id
    entry[CARD_MSG_ID_KEY] = 4242


async def _confirm_directory(scenario: ScenarioHarness) -> None:
    await _tap(scenario, CB_DIR_CONFIRM)


async def _settle(times: int = 12) -> None:
    """Let the WAIT task run a handful of slices."""
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
    version: str | None = _LICENSED,
    command: str = _CLAUDE_CMD,
    moves: bool = True,
) -> tuple[str, _TrustPane]:
    """Open the browser, confirm the directory, and settle into the trust card."""
    scenario.tmux.probe_version_response = version
    await _open_browser(scenario)
    # The window the flow will create; seed its pane BEFORE the create so the
    # first classifying slice sees the live trust prompt.
    pane = _TrustPane(scenario, "@0", moves=moves, command=command)
    pane.install(monkeypatch)
    await _confirm_directory(scenario)
    await _settle()
    return "@0", pane


# ── The issue's regression test ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trust_prompt_yields_a_card_not_a_kill(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #65 regression: a folder-trust prompt no longer kills the window."""
    wid, _pane = await _start_flow(scenario, monkeypatch)

    assert scenario.tmux.kill_calls == [], "the trust prompt must NOT kill"
    assert wid in scenario.tmux.windows
    # Launch-deferred create + the in-pane probe ran before the launch.
    assert scenario.tmux.create_calls[-1]["defer_launch"] is True
    assert scenario.tmux.probe_calls == [wid]
    assert scenario.tmux.launch_calls == [wid]
    # The picker card became the 🔐 trust card with BOTH buttons.
    assert any("trust this folder" in t.lower() for t in _card_edits(scenario))
    cbs = _card_keyboards(scenario)[-1]
    assert any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    assert any(c.startswith(f"{CB_TRUST_PICK}c:") for c in cbs), cbs
    # The first message is still queued for the post-bind replay.
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    assert entry["_pending_thread_text"] == "hello claude"
    assert entry[STATE_KEY] == trust_flow.STATE_AWAITING_TRUST


@pytest.mark.asyncio
async def test_probe_failure_renders_a_display_only_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe ``None`` ⇒ no Trust button; Cancel stays live; nothing is killed."""
    await _start_flow(scenario, monkeypatch, version=None)

    cbs = _card_keyboards(scenario)[-1]
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    assert any(c.startswith(f"{CB_TRUST_PICK}c:") for c in cbs), cbs
    assert any("tmux window" in t for t in _card_edits(scenario))
    assert scenario.tmux.kill_calls == []


@pytest.mark.asyncio
async def test_unlicensed_version_renders_a_display_only_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _start_flow(scenario, monkeypatch, version="9.9.9")
    cbs = _card_keyboards(scenario)[-1]
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs


# ── Kill switches ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trust_flag_off_is_display_only(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision_token.set_trust_card_dispatch_enabled(False)
    await _start_flow(scenario, monkeypatch)
    cbs = _card_keyboards(scenario)[-1]
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs
    assert any(c.startswith(f"{CB_TRUST_PICK}c:") for c in cbs), cbs


@pytest.mark.asyncio
async def test_explicit_decision_dispatch_false_forces_display_only(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who turned the Decision kill switch OFF gets both lanes off."""
    decision_token.set_decision_dispatch_force_disabled(True)
    await _start_flow(scenario, monkeypatch)
    cbs = _card_keyboards(scenario)[-1]
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for c in cbs), cbs


# ── The Trust tap ────────────────────────────────────────────────────────────


def _trust_button(scenario: ScenarioHarness) -> str:
    cbs = _card_keyboards(scenario)[-1]
    return next(c for c in cbs if c.startswith(f"{CB_TRUST_PICK}t:"))


@pytest.mark.asyncio
async def test_trust_tap_navigates_verifies_enters_then_binds(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, pane = await _start_flow(scenario, monkeypatch)
    tap = _trust_button(scenario)
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, tap)

    keys = [k for _w, k, _e, _l in scenario.tmux.sent_keys]
    assert "Enter" in keys, keys
    # Digits COMMIT instantly on this surface — the lane must never send one.
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys), (
        scenario.tmux.sent_keys
    )
    assert pane.committed == 1, "Enter must commit the CURSORED first option"

    # Claude finishes booting (the welcome/REPL is painted) and the session
    # registers; the WAIT task completes the bind + replays the queued first
    # message through the normal gated delivery transaction.
    pane.post_commit = IDLE_PANE_V2_1_207
    scenario.session_manager.window_states.clear()
    scenario._write_session_map_entry(wid, "sid-trust", "/repo")
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == wid
    assert picker_entry(scenario.user_data, _THREAD) is None
    assert any("Send messages here" in t for t in _card_edits(scenario))


@pytest.mark.asyncio
async def test_failed_verify_sends_no_further_keys_and_re_renders(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrows WRAP, so a failed verify must NEVER be answered with more arrows.

    A frozen cursor makes the delta==0 wiggle fail its motion proof: the
    transaction bails PRE-COMMIT (``not_advanced``), the card is re-rendered
    with a FRESH token, and no ``Enter`` was ever sent.
    """
    _wid, pane = await _start_flow(scenario, monkeypatch, moves=False)
    old_tap = _trust_button(scenario)
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, old_tap)

    keys = [k for _w, k, _e, _l in scenario.tmux.sent_keys]
    assert "Enter" not in keys, keys
    assert pane.committed is None
    # Bounded, not "keep arrowing": at most the wiggle's away+back.
    assert len([k for k in keys if k in ("Up", "Down")]) <= 2, keys
    new_tap = _trust_button(scenario)
    assert new_tap != old_tap, "the re-render must mint a fresh token"
    old_token = old_tap.split(":")[-1]
    assert decision_token.peek(old_token) is None, "the consumed token is burned"
    assert decision_token.peek(new_tap.split(":")[-1]) is not None


@pytest.mark.asyncio
async def test_blank_post_enter_frame_is_commit_unconfirmed_and_rebases_budget(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addendum r1 P1: prompt-gone-blank ⇒ ``commit_unconfirmed``, never a kill.

    The transaction SENT Enter, so the flow enters ``awaiting_registration``
    with a FRESH registration budget; the global observation ceiling stays
    armed and the window is never killed while the blank frame persists.
    """
    wid, pane = await _start_flow(scenario, monkeypatch)
    flow = trust_flow.get_flow(scenario.user_id, _THREAD)
    assert flow is not None
    before = flow.registration_deadline

    await _tap(scenario, _trust_button(scenario))

    assert pane.committed == 1
    assert flow.phase == trust_flow.PHASE_AWAITING_REGISTRATION
    assert flow.enter_sent_at is not None
    assert flow.registration_deadline > before, "the budget must be REBASED"
    assert flow.trust_deadline is None
    assert scenario.tmux.kill_calls == []
    assert wid in scenario.tmux.windows


# ── Cancel ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_kills_the_window_without_a_single_keystroke(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, pane = await _start_flow(scenario, monkeypatch)
    cancel = next(
        c for c in _card_keyboards(scenario)[-1] if c.startswith(f"{CB_TRUST_PICK}c:")
    )
    scenario.tmux.sent_keys.clear()

    await _tap(scenario, cancel)

    assert scenario.tmux.sent_keys == [], "Cancel must never type into the pane"
    assert pane.committed is None
    assert scenario.tmux.kill_calls == [wid]
    assert picker_entry(scenario.user_data, _THREAD) is None
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
    assert any("Cancelled" in t for t in _card_edits(scenario))


# ── Fix 5: inbound while the flow owns the topic ─────────────────────────────


@pytest.mark.asyncio
async def test_text_during_awaiting_trust_queues_and_nudges(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No browser rebuild may race a live flow; the payload is queued."""
    await _start_flow(scenario, monkeypatch)
    update = make_update_text("second message", thread_id=_THREAD)

    await bot_module.text_handler(update, scenario.context)

    reply = update.message.reply_text.await_args.args[0]
    assert "trust the folder" in reply
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    assert entry[STATE_KEY] == trust_flow.STATE_AWAITING_TRUST
    assert entry["_pending_thread_text"] == "second message"
    assert scenario.tmux.kill_calls == []


@pytest.mark.asyncio
async def test_binding_that_lands_during_preprocessing_delivers_the_payload(
    scenario: ScenarioHarness,
) -> None:
    """Fix 5: the binding is RE-READ under the creation lock after the awaits.

    A binding that appears while an inbound handler is still pre-processing must
    make THAT payload fall through to normal bound delivery — never a stale
    directory browser.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")

    async def _bind_during_reply_context(*args: Any, **kwargs: Any) -> Any:
        scenario.bind_thread(
            _THREAD, wid, display_name="repo", cwd="/repo", session_id="sid-race"
        )
        return args[3], False

    from cctelegram.handlers import inbound_telegram as inbound_module

    original = inbound_module._apply_reply_context
    inbound_module._apply_reply_context = _bind_during_reply_context
    try:
        await bot_module.text_handler(
            make_update_text("race me", thread_id=_THREAD), scenario.context
        )
    finally:
        inbound_module._apply_reply_context = original
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert picker_entry(scenario.user_data, _THREAD) is None, "no browser entry"
    assert scenario.tmux.delivered("race me"), scenario.tmux.sent_keys


# ── Fix 6: teardown ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topic_close_during_awaiting_trust_cancels_and_cleans(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable at the NO-BINDING branch, which historically skipped teardown."""
    wid, _pane = await _start_flow(scenario, monkeypatch)
    assert scenario.session_manager.thread_bindings.get(scenario.user_id, {}) == {}

    await bot_module.topic_closed_handler(
        make_update_topic_closed(thread_id=_THREAD), scenario.context
    )

    assert scenario.tmux.kill_calls == [wid], "the unbound window must be reaped"
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
    assert picker_entry(scenario.user_data, _THREAD) is None
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is None


@pytest.mark.asyncio
async def test_start_command_tears_down_a_live_creation_flow(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, _pane = await _start_flow(scenario, monkeypatch)

    await bot_module.start_command(
        make_update_text("/start", thread_id=None), scenario.context
    )

    assert scenario.tmux.kill_calls == [wid]
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
    assert picker_entry(scenario.user_data, _THREAD) is None


# ── Terminal classifications ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_pane_still_showing_the_prompt_is_cleaned_up_not_carded(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead pane RETAINS the prompt text — the pane COMMAND decides."""
    scenario.tmux.probe_version_response = _LICENSED
    await _open_browser(scenario)
    pane = _TrustPane(scenario, "@0", command="zsh")
    monkeypatch.setattr(pane, "_pane", lambda: _POST_ESC_SHELL, raising=False)
    pane.install(monkeypatch)

    await _confirm_directory(scenario)
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.tmux.kill_calls == ["@0"]
    cbs = _card_keyboards(scenario)
    assert not any(c.startswith(f"{CB_TRUST_PICK}t:") for row in cbs for c in row), (
        "a corpse must never get a live Trust button"
    )
    assert any("Claude exited" in t for t in _card_edits(scenario))


@pytest.mark.asyncio
async def test_unreadable_pane_command_spares_the_window_at_the_global_ceiling(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The npm ``claude.exe`` shape: no card, no kill — SPARE + release.

    Fail-open is preserved and the lane never takes permanent ownership of the
    topic; the recovery copy points at "Bind to Existing Window".
    """
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    scenario.tmux.probe_version_response = _LICENSED
    await _open_browser(scenario)
    _TrustPane(scenario, "@0", command="claude.exe").install(monkeypatch)

    await _confirm_directory(scenario)
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.tmux.kill_calls == [], "the global ceiling must NEVER kill"
    assert "@0" in scenario.tmux.windows, "the window stays alive"
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
    assert picker_entry(scenario.user_data, _THREAD) is None
    assert any("Bind to Existing Window" in t for t in _card_edits(scenario))


@pytest.mark.asyncio
async def test_trust_ceiling_expiry_cleans_up_with_honest_copy(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    scenario.tmux.probe_version_response = _LICENSED
    await _open_browser(scenario)
    _TrustPane(scenario, "@0").install(monkeypatch)

    await _confirm_directory(scenario)
    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.tmux.kill_calls == ["@0"]
    assert any("Timed out waiting for you to trust" in t for t in _card_edits(scenario))


@pytest.mark.asyncio
async def test_a_registered_session_binds_without_any_trust_prompt(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: no trust prompt at all — the flow binds and replays."""
    scenario.tmux.probe_version_response = _LICENSED
    await _open_browser(scenario)
    pane = _TrustPane(scenario, "@0")
    monkeypatch.setattr(
        pane, "_pane", lambda: "user@host repo % claude\n", raising=False
    )
    pane.install(monkeypatch)
    await _confirm_directory(scenario)
    scenario._write_session_map_entry("@0", "sid-happy", "/repo")

    task = trust_flow.flow_task(scenario.user_id, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == "@0"
    assert scenario.tmux.kill_calls == []


# ── Resume parity ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_creation_never_enters_the_trust_lane(
    scenario: ScenarioHarness,
) -> None:
    """The resume path keeps today's manual-association fallback byte-identical.

    It must NOT defer the launch, must NOT probe, and must NOT spawn a flow.
    """
    from cctelegram.handlers import inbound_telegram as inbound_module

    update = make_update_real_callback(
        "x", bot=scenario.bot, thread_id=_THREAD, user_id=scenario.user_id
    )
    query = update.callback_query
    ensure_picker_entry(scenario.user_data, _THREAD)["_pending_thread_text"] = "hi"
    await inbound_module._create_and_bind_window(
        query,
        scenario.context,
        update.effective_user,
        "/repo",
        _THREAD,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
        resume_session_id="sess-resume",
    )

    assert scenario.tmux.create_calls[-1].get("defer_launch") is False
    assert scenario.tmux.probe_calls == []
    assert scenario.tmux.launch_calls == []
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
