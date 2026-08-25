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
    ENTRY_TOKEN_KEY,
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
        # Slows every capture, so a scenario can hold the completion tail
        # (which captures the pane to deliver the queued first message) IN
        # FLIGHT while it fires a teardown at it.
        self.capture_delay = 0.0

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
        if self.capture_delay:
            await asyncio.sleep(self.capture_delay)
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


async def _await_phase(
    scenario: ScenarioHarness, phase: str, *, timeout: float = 3.0
) -> Any:
    """Wait until the topic's flow reaches ``phase`` (bounded)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        flow = trust_flow.get_flow(scenario.user_id, _THREAD)
        if flow is not None and flow.phase == phase:
            return flow
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"flow never reached {phase}: "
        f"{getattr(trust_flow.get_flow(scenario.user_id, _THREAD), 'phase', None)}"
    )


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


def _photo_update(scenario: ScenarioHarness, on_download: Any = None) -> Any:
    """A photo Update whose file the fake bot will "download".

    ``on_download`` fires DURING the download — the spec's pre-processing await
    for the photo handler — so a race test can land a binding or a creation flow
    exactly where Fix 5 says the handler must re-read.
    """
    from unittest.mock import AsyncMock, MagicMock

    photo = MagicMock(name="PhotoSize")
    photo.file_unique_id = "puid"
    tg_file = MagicMock()

    async def _download(out_path: Any) -> Any:
        if on_download is not None:
            await on_download()
        Path(out_path).write_bytes(b"\x00")
        return out_path

    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    photo.get_file = AsyncMock(return_value=tg_file)
    update = make_update_text("", thread_id=_THREAD)
    update.message.text = None
    update.message.photo = [photo]
    update.message.caption = "look"
    update.message.media_group_id = None
    return update


def _document_update(scenario: ScenarioHarness, on_download: Any = None) -> Any:
    """As ``_photo_update``, for the document handler's download await."""
    from unittest.mock import AsyncMock, MagicMock

    doc = MagicMock(name="Document")
    doc.file_unique_id = "duid"
    doc.file_name = "notes.txt"
    doc.file_size = 10
    tg_file = MagicMock()

    async def _download(out_path: Any) -> Any:
        if on_download is not None:
            await on_download()
        Path(out_path).write_bytes(b"hello")
        return out_path

    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    doc.get_file = AsyncMock(return_value=tg_file)
    update = make_update_text("", thread_id=_THREAD)
    update.message.text = None
    update.message.document = doc
    update.message.caption = "notes"
    update.message.media_group_id = None
    return update


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
    # Digits COMMIT INSTANTLY on this surface, whether sent literally or as a
    # named key — the lane must never send one in ANY form (review r1 P3-2).
    assert not any(k.isdigit() for _w, k, _e, _lit in scenario.tmux.sent_keys), (
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


# ── Codex review round 1 — the Telegram-seam folds ──────────────────────────


class _Racer:
    """Binds the topic the first time the handler lists tmux windows.

    `_list_unbound_windows` is the LAST await before the directory browser is
    built, so this reproduces the exact P1-2 window: a binding (or a creation
    flow) appearing between the ownership decision and the mutation that acts
    on it. Scripted on the SUBSTRATE (FakeTmux), never on a handler internal.
    """

    def __init__(self, scenario: ScenarioHarness, wid: str, thread_id: int) -> None:
        self._scenario = scenario
        self._wid = wid
        self._thread_id = thread_id
        self.fired = False

    async def __call__(self) -> None:
        self.fired = True
        self._scenario.bind_thread(
            self._thread_id,
            self._wid,
            display_name="repo",
            cwd="/repo",
            session_id="sid-raced",
        )


@pytest.mark.asyncio
async def test_p1_2_text_binding_during_the_browser_build_delivers_the_payload(
    scenario: ScenarioHarness,
) -> None:
    """P1-2: the decision and the mutation must share ONE critical section.

    Pre-fold `decide_unbound_inbound` released the creation lock and the caller
    then awaited `_list_unbound_windows`; a binding appearing in that gap was
    overwritten with browser state and the payload was stashed into a picker
    for an already-bound topic (silently discarded).
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.tmux.on_list_windows = _Racer(scenario, wid, _THREAD)

    await bot_module.text_handler(
        make_update_text("deliver me", thread_id=_THREAD), scenario.context
    )
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert picker_entry(scenario.user_data, _THREAD) is None, (
        "a bound topic must never be given browser state"
    )
    assert scenario.tmux.delivered("deliver me"), scenario.tmux.sent_keys


@pytest.mark.asyncio
async def test_p1_2_photo_binding_during_the_browser_build_delivers_the_payload(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.tmux.on_list_windows = _Racer(scenario, wid, _THREAD)

    await bot_module.photo_handler(_photo_update(scenario), scenario.context)
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert picker_entry(scenario.user_data, _THREAD) is None, (
        "a bound topic must never be given browser state"
    )
    assert scenario.tmux.written_texts, "the photo must reach the bound route"


@pytest.mark.asyncio
async def test_p1_2_document_binding_during_the_browser_build_delivers_the_payload(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.tmux.on_list_windows = _Racer(scenario, wid, _THREAD)

    await bot_module.document_handler(_document_update(scenario), scenario.context)
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert picker_entry(scenario.user_data, _THREAD) is None, (
        "a bound topic must never be given browser state"
    )
    assert scenario.tmux.written_texts, "the document must reach the bound route"


@pytest.mark.asyncio
async def test_p1_2_trust_flow_appearing_during_the_browser_build_is_not_overwritten(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same gap, with a CREATION FLOW rather than a binding appearing."""
    scenario.tmux.probe_version_response = _LICENSED
    _TrustPane(scenario, "@0").install(monkeypatch)

    started: dict[str, Any] = {}

    async def _start_flow_during_list() -> None:
        entry = ensure_picker_entry(scenario.user_data, _THREAD)
        entry[CARD_CHAT_ID_KEY] = scenario.chat_id
        entry[CARD_MSG_ID_KEY] = 4242
        started["flow"] = await trust_flow.start_trust_wait(
            bot=scenario.bot,
            user_id=scenario.user_id,
            thread_id=_THREAD,
            chat_id=scenario.chat_id,
            user_data=scenario.user_data,
            created_wid="@0",
            window_name="repo",
            selected_path="/repo",
            create_message="Created",
            cli_version=_LICENSED,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )

    scenario.add_window(window_id="@0", window_name="repo", cwd="/repo")
    scenario.tmux.on_list_windows = _start_flow_during_list

    await bot_module.text_handler(
        make_update_text("queue me", thread_id=_THREAD), scenario.context
    )

    assert started.get("flow") is not None
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    assert entry[STATE_KEY] == trust_flow.STATE_AWAITING_TRUST, (
        "a live creation flow must never be overwritten by a browser rebuild"
    )
    await trust_flow.teardown_thread(scenario.user_id, _THREAD)


@pytest.mark.asyncio
async def test_p2_2_an_expired_trust_tap_re_renders_the_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-2: a stale/expired Trust tap must RE-RENDER, not just answer.

    The prompt is still live and licensed, so the visible button has to come
    back with a fresh token — answering alone leaves it permanently dead.
    """
    await _start_flow(scenario, monkeypatch)
    live_tap = _trust_button(scenario)
    stale = f"{CB_TRUST_PICK}t:{'0' * 12}"

    update = await _tap(scenario, stale)

    new_tap = _trust_button(scenario)
    assert new_tap.startswith(f"{CB_TRUST_PICK}t:")
    assert new_tap != live_tap, "the expired tap must mint a FRESH token"
    assert decision_token.peek(new_tap.split(":")[-1]) is not None
    assert scenario.tmux.sent_keys == [], "an expired tap types nothing"
    del update


@pytest.mark.asyncio
async def test_p2_4_a_non_owner_tap_never_touches_the_owners_card(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-4: a non-owner tap answers only — no edit, no consume, keyboard intact."""
    await _start_flow(scenario, monkeypatch)
    tap = _trust_button(scenario)
    token = tap.split(":")[-1]
    edits_before = len(_card_edits(scenario))

    update = make_update_real_callback(
        tap,
        bot=scenario.bot,
        thread_id=_THREAD,
        user_id=scenario.user_id + 9999,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )

    assert len(_card_edits(scenario)) == edits_before, (
        "a non-owner tap must NEVER edit the owner's card"
    )
    assert decision_token.peek(token) is not None, "the owner's token is untouched"
    assert scenario.tmux.sent_keys == []
    flow = trust_flow.get_flow(scenario.user_id, _THREAD)
    assert flow is not None and flow.phase == trust_flow.PHASE_AWAITING_TRUST


@pytest.mark.asyncio
async def test_p2_4_a_non_owner_cancel_never_kills_the_owners_window(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, _pane = await _start_flow(scenario, monkeypatch)
    cancel = next(
        c for c in _card_keyboards(scenario)[-1] if c.startswith(f"{CB_TRUST_PICK}c:")
    )
    edits_before = len(_card_edits(scenario))

    update = make_update_real_callback(
        cancel,
        bot=scenario.bot,
        thread_id=_THREAD,
        user_id=scenario.user_id + 9999,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )

    assert scenario.tmux.kill_calls == [], "a non-owner must never kill the window"
    assert len(_card_edits(scenario)) == edits_before
    assert wid in scenario.tmux.windows
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is not None


@pytest.mark.asyncio
async def test_p2_3_start_during_completing_bind_awaits_and_tears_down_bound(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-3: `/start` must await a retained completion tail, then bound-teardown.

    The flow binds under `/start` rather than being abandoned half-bound, and
    the now-bound topic goes through the normal bound-topic teardown.
    """
    wid, pane = await _start_flow(scenario, monkeypatch)
    # The prompt is answered and the REPL is up, so the tail's replay of the
    # queued first message actually lands.
    pane.post_commit = IDLE_PANE_V2_1_207
    pane.committed = 1
    # The session registers, so the very next slice claims ``completing_bind``
    # and starts the tail; the slow capture holds that tail IN FLIGHT while
    # ``/start`` fires at it.
    pane.capture_delay = 0.15
    scenario._write_session_map_entry(wid, "sid-start-race", "/repo")
    await _await_phase(scenario, trust_flow.PHASE_COMPLETING_BIND)

    await bot_module.start_command(
        make_update_text("/start", thread_id=None), scenario.context
    )

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == wid, (
        "a completion that won must not be abandoned half-bound"
    )
    assert any("Send messages here" in t for t in _card_edits(scenario)), (
        "/start must AWAIT the retained completion tail, not abandon it"
    )
    assert scenario.tmux.kill_calls == [], "a bound window is never killed by /start"
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None
    assert picker_entry(scenario.user_data, _THREAD) is None


@pytest.mark.asyncio
async def test_p2_3_topic_close_during_completing_bind_awaits_the_tail(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid, pane = await _start_flow(scenario, monkeypatch)
    pane.post_commit = IDLE_PANE_V2_1_207
    pane.committed = 1
    pane.capture_delay = 0.15
    scenario._write_session_map_entry(wid, "sid-close-race", "/repo")
    await _await_phase(scenario, trust_flow.PHASE_COMPLETING_BIND)

    await bot_module.topic_closed_handler(
        make_update_topic_closed(thread_id=_THREAD), scenario.context
    )

    # Completion won inside teardown, so the topic-close path takes its BOUND
    # branch: the window is killed and the binding removed.
    assert scenario.tmux.kill_calls == [wid]
    assert _THREAD not in scenario.session_manager.thread_bindings.get(
        scenario.user_id, {}
    )
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None


# ── Codex review round 2 — the Telegram-seam folds ──────────────────────────


@pytest.mark.asyncio
async def test_p2_f_photo_binding_during_the_ATTACHMENT_DOWNLOAD_is_delivered(
    scenario: ScenarioHarness,
) -> None:
    """Fix 5's own pre-processing await: the DOWNLOAD, not the browser build.

    A binding that lands while the photo is still downloading must make THAT
    payload fall through to bound delivery — never a stale directory browser.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    racer = _Racer(scenario, wid, _THREAD)

    await bot_module.photo_handler(
        _photo_update(scenario, on_download=racer), scenario.context
    )
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert racer.fired, "the race must fire during the download"
    assert picker_entry(scenario.user_data, _THREAD) is None, (
        "a bound topic must never be given browser state"
    )
    assert scenario.tmux.written_texts, "the photo must reach the bound route"


@pytest.mark.asyncio
async def test_p2_f_document_binding_during_the_ATTACHMENT_DOWNLOAD_is_delivered(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    racer = _Racer(scenario, wid, _THREAD)

    await bot_module.document_handler(
        _document_update(scenario, on_download=racer), scenario.context
    )
    await aggregator_flush_route((scenario.user_id, _THREAD, wid))

    assert racer.fired, "the race must fire during the download"
    assert picker_entry(scenario.user_data, _THREAD) is None
    assert scenario.tmux.written_texts, "the document must reach the bound route"


@pytest.mark.asyncio
async def test_p2_f_a_trust_flow_starting_during_the_download_is_not_overwritten(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same download window, with a CREATION FLOW appearing in it."""
    scenario.tmux.probe_version_response = _LICENSED
    scenario.add_window(window_id="@0", window_name="repo", cwd="/repo")
    _TrustPane(scenario, "@0").install(monkeypatch)

    async def _start_during_download() -> None:
        entry = ensure_picker_entry(scenario.user_data, _THREAD)
        entry[CARD_CHAT_ID_KEY] = scenario.chat_id
        entry[CARD_MSG_ID_KEY] = 4242
        await trust_flow.start_trust_wait(
            bot=scenario.bot,
            user_id=scenario.user_id,
            thread_id=_THREAD,
            chat_id=scenario.chat_id,
            user_data=scenario.user_data,
            entry_token=entry[ENTRY_TOKEN_KEY],
            created_wid="@0",
            window_name="repo",
            selected_path="/repo",
            create_message="Created",
            cli_version=_LICENSED,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )

    await bot_module.photo_handler(
        _photo_update(scenario, on_download=_start_during_download), scenario.context
    )

    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    assert entry[STATE_KEY] == trust_flow.STATE_AWAITING_TRUST, (
        "a live creation flow must never be overwritten by a browser rebuild"
    )
    assert entry.get("_pending_thread_attachments"), "the photo is QUEUED"
    await trust_flow.teardown_thread(scenario.user_id, _THREAD)


@pytest.mark.asyncio
async def test_p2_f_an_expired_tap_on_a_DEAD_pane_never_re_mints(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpse retains the trust text — an expired tap must not re-arm it.

    After Claude exits (a digit commit or Escape) the prompt block stays painted
    above the shell prompt. The refresh must key on the pane COMMAND and disable
    the card instead of minting a Trust button that would type into a shell.
    """
    await _start_flow(scenario, monkeypatch)
    live_tap = _trust_button(scenario)
    # Claude exits: prompt text still on screen, but the pane is a shell now.
    pane = _TrustPane(scenario, "@0", command="zsh")
    monkeypatch.setattr(pane, "_pane", lambda: _POST_ESC_SHELL, raising=False)
    pane.install(monkeypatch)

    keyboards_before = len(_card_keyboards(scenario))

    await _tap(scenario, f"{CB_TRUST_PICK}t:{'0' * 12}")

    # The disable edit carries NO keyboard, so no new keyboard may appear at
    # all — a re-mint would show up here.
    assert len(_card_keyboards(scenario)) == keyboards_before, (
        "a corpse must never be re-armed with a Trust button"
    )
    assert "no longer live" in _card_edits(scenario)[-1], _card_edits(scenario)[-1]
    assert scenario.tmux.sent_keys == [], "an expired tap types nothing"
    assert trust_flow.get_flow(scenario.user_id, _THREAD).token is None
    del live_tap


@pytest.mark.asyncio
async def test_p2_e_a_second_users_tap_on_their_own_card_is_accepted(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership resolves the tapped CARD, so two flows can share one topic.

    A thread-only owner lookup rejected the second user's tap on their OWN card;
    keying on the card's coordinates accepts it.
    """
    wid, _pane = await _start_flow(scenario, monkeypatch)
    owner_flow = trust_flow.get_flow(scenario.user_id, _THREAD)
    assert owner_flow is not None
    # The card is the message the tap arrives on — the callback's own message,
    # which is what ``_create_and_bind_window`` records.
    owner_card_id = owner_flow.card_msg_id
    assert owner_card_id is not None

    # A SECOND allowed user's flow, in the same topic, with its own card.
    other_id = scenario.user_id + 7
    other_data: dict[str, Any] = {}
    other_entry = ensure_picker_entry(other_data, _THREAD)
    assert other_entry is not None
    other_entry[CARD_CHAT_ID_KEY] = scenario.chat_id
    other_entry[CARD_MSG_ID_KEY] = 5150
    scenario.add_window(window_id="@9", window_name="repo2", cwd="/repo2")
    other_flow = await trust_flow.start_trust_wait(
        bot=scenario.bot,
        user_id=other_id,
        thread_id=_THREAD,
        chat_id=scenario.chat_id,
        user_data=other_data,
        entry_token=other_entry[ENTRY_TOKEN_KEY],
        created_wid="@9",
        window_name="repo2",
        selected_path="/repo2",
        create_message="Created",
        cli_version=_LICENSED,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )
    assert other_flow is not None

    # Each card resolves to ITS OWN owner…
    assert (
        trust_flow.flow_owner_for_card(scenario.chat_id, owner_card_id)
        == scenario.user_id
    )
    assert trust_flow.flow_owner_for_card(scenario.chat_id, 5150) == other_id
    # …and the second user tapping the FIRST user's card is still refused.
    assert trust_flow.flow_owner_for_card(scenario.chat_id, owner_card_id) != other_id
    # A thread-only lookup would have collapsed these two to one owner.
    assert owner_card_id != 5150

    await trust_flow.teardown_thread(scenario.user_id, _THREAD)
    await trust_flow.teardown_thread(other_id, _THREAD)
    del wid


@pytest.mark.asyncio
async def test_p1_a_a_creation_install_racing_start_command_cannot_survive_it(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/start` clears every entry under its lock, so a racing install aborts.

    Driven at the seam: the entry token is captured, `/start` runs, and only
    THEN does the install attempt land — exactly the snapshot→clear gap.
    """
    entry = ensure_picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    entry[CARD_CHAT_ID_KEY] = scenario.chat_id
    entry[CARD_MSG_ID_KEY] = 4242
    captured_token = entry[ENTRY_TOKEN_KEY]
    scenario.add_window(window_id="@0", window_name="repo", cwd="/repo")

    await bot_module.start_command(
        make_update_text("/start", thread_id=None), scenario.context
    )

    flow = await trust_flow.start_trust_wait(
        bot=scenario.bot,
        user_id=scenario.user_id,
        thread_id=_THREAD,
        chat_id=scenario.chat_id,
        user_data=scenario.user_data,
        entry_token=captured_token,
        created_wid="@0",
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version=_LICENSED,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )

    assert flow is None, "an install whose entry /start cleared must ABORT"
    assert trust_flow.get_flow(scenario.user_id, _THREAD) is None


@pytest.mark.asyncio
async def test_p2_4_a_raced_photo_carries_exactly_one_quote_block(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload that resolves BOUND after the download race is rendered ONCE.

    The unbound branch applies reply context before the ownership decision; the
    bound fall-through used to apply it a SECOND time, so a raced reply reached
    Claude with TWO quote blocks (review r3 P2-4).
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    racer = _Racer(scenario, wid, _THREAD)
    applied: list[str] = []

    from cctelegram.handlers import inbound_telegram as inbound_module

    original = inbound_module._apply_reply_context

    async def _counting(message: Any, user_id: int, thread_id: Any, text: str) -> Any:
        applied.append(text)
        return f"<quote>{text}", True

    monkeypatch.setattr(inbound_module, "_apply_reply_context", _counting)
    try:
        await bot_module.photo_handler(
            _photo_update(scenario, on_download=racer), scenario.context
        )
        await aggregator_flush_route((scenario.user_id, _THREAD, wid))
    finally:
        monkeypatch.setattr(inbound_module, "_apply_reply_context", original)

    assert racer.fired
    assert len(applied) == 1, (
        f"reply context must be applied EXACTLY once, got {applied}"
    )
    written = "\n".join(scenario.tmux.written_texts)
    assert written.count("<quote>") <= 1, written


@pytest.mark.asyncio
async def test_p2_4_a_raced_document_carries_exactly_one_quote_block(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    racer = _Racer(scenario, wid, _THREAD)
    applied: list[str] = []

    from cctelegram.handlers import inbound_telegram as inbound_module

    original = inbound_module._apply_reply_context

    async def _counting(message: Any, user_id: int, thread_id: Any, text: str) -> Any:
        applied.append(text)
        return f"<quote>{text}", True

    monkeypatch.setattr(inbound_module, "_apply_reply_context", _counting)
    try:
        await bot_module.document_handler(
            _document_update(scenario, on_download=racer), scenario.context
        )
        await aggregator_flush_route((scenario.user_id, _THREAD, wid))
    finally:
        monkeypatch.setattr(inbound_module, "_apply_reply_context", original)

    assert racer.fired
    assert len(applied) == 1, (
        f"reply context must be applied EXACTLY once, got {applied}"
    )
