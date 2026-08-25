"""GH #65 wave 3 — the CAS arbitration core, and the round-3 spot fixes.

Three consecutive review rounds found the SAME class of hole: a phase claim or
an entry clear that was not atomic with its own side effects. Per the repo's
"three doors into the same room" rule the arbitration was consolidated into ONE
compare-and-swap discipline rather than patched a fourth time, and this module
pins that discipline directly:

  * ``try_transition_locked`` is the ONLY mutator of ``flow.phase``;
  * terminal claims are EXCLUSIVE against every claimed phase;
  * the registration branch must WIN a CAS before starting a bind;
  * entry clear + flow stop are ONE critical section;
  * teardown WAITS OUT an in-flight dispatch instead of cancelling into it;
  * the terminalizer runs no side effect under the lock, and hands a
    ``SPARED_REGISTERED`` outcome to the one completion seam.

Plus the r3 spot fixes: generation-qualified completion notes with a pre-flow
binding baseline (P2-1), the single-lock-hold refresh with text-first capture
ordering (P2-2), cancellation-safe bounded probes (P2-3), and commit progress
reported the moment Enter is sent (P2-5).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.config import config
from cctelegram.handlers import decision_token, trust_flow
from cctelegram.handlers.directory_browser import (
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    ENTRY_TOKEN_KEY,
    ensure_picker_entry,
    picker_entry,
)
from cctelegram.utils import app_dir

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_IDLE = (_FIXTURES / "inputbox_idle_v2.1.207.txt").read_text()
_THREAD = 909
_USER = 6060


@pytest.fixture(autouse=True)
def _lane(monkeypatch: pytest.MonkeyPatch) -> Any:
    terminal_parser.set_decision_cards_enabled(True)
    decision_token.set_trust_card_dispatch_enabled(True)
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 30.0)
    monkeypatch.setattr(config, "hook_timeout_override", 0.05)
    monkeypatch.setattr(config, "hook_timeout_extension_s", 0.05)
    monkeypatch.setattr(trust_flow, "SLICE_S", 0.01)
    monkeypatch.setattr(trust_flow, "PANE_POLL_EVERY_S", 0.0)
    # A teardown that meets an in-flight dispatch waits this out; the
    # production budget is 45s, which no test needs to spend.
    monkeypatch.setattr(trust_flow, "DISPATCH_SETTLE_BUDGET_S", 0.2)
    (app_dir() / "session_map.json").unlink(missing_ok=True)
    yield
    trust_flow.reset_for_tests()
    decision_token.reset_for_tests()
    terminal_parser.reset_for_tests()
    (app_dir() / "session_map.json").unlink(missing_ok=True)


class _Tmux:
    def __init__(self, *, command: str = "claude", pane: str = "") -> None:
        self.command = command
        self.pane = pane
        self.kill_calls: list[str] = []
        self.order: list[str] = []

    async def pane_current_command(self, window_id: str) -> str | None:
        del window_id
        self.order.append("command")
        return self.command

    async def capture_pane(self, window_id: str, **kwargs: Any) -> str:
        del window_id, kwargs
        self.order.append("text")
        return self.pane

    async def kill_window(self, window_id: str) -> bool:
        self.kill_calls.append(window_id)
        return True


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.edits.append(kwargs)
        return None


class _Sessions:
    def __init__(self) -> None:
        self.registered = False
        self.binds: list[tuple[int, int, str]] = []
        self.window_states: dict[str, Any] = {}

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        del window_id, timeout
        await asyncio.sleep(interval)
        return self.registered

    def get_window_state(self, window_id: str) -> Any:
        from types import SimpleNamespace

        return self.window_states.setdefault(
            window_id, SimpleNamespace(session_id="", cwd="/repo", window_name="repo")
        )

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        del window_name
        self.binds.append((user_id, thread_id, window_id))

    def _build_session_file_path(self, sid: str, cwd: str) -> None:
        del sid, cwd
        return None

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        for uid, tid, wid in self.binds:
            if uid == user_id and tid == thread_id:
                return wid
        return None


def _entry(user_data: dict[str, Any], thread_id: int = _THREAD) -> dict[str, Any]:
    entry = ensure_picker_entry(user_data, thread_id)
    assert entry is not None
    entry[CARD_CHAT_ID_KEY] = -100
    entry[CARD_MSG_ID_KEY] = 999
    return entry


async def _start(
    user_data: dict[str, Any],
    *,
    tmux: Any,
    bot: Any,
    sessions: Any,
    thread_id: int = _THREAD,
    wid: str = "@5",
) -> trust_flow.TrustFlow | None:
    entry = picker_entry(user_data, thread_id)
    return await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=thread_id,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry.get(ENTRY_TOKEN_KEY) if entry else None,
        created_wid=wid,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
    )


# ── The CAS discipline itself ───────────────────────────────────────────────


def test_phase_has_exactly_one_mutator_in_the_module() -> None:
    """Structural pin: ``flow.phase`` is assigned in ONE place only.

    The whole wave-3 argument rests on every actor going through the CAS, so a
    future direct assignment must fail HERE rather than at the next review.
    """
    source = (
        Path(trust_flow.__file__).read_text()  # type: ignore[arg-type]
    )
    assignments = [
        line.strip()
        for line in source.splitlines()
        if "._phase = " in line and not line.strip().startswith("#")
    ]
    assert assignments == ["flow._phase = to", "flow._phase = PHASE_CANCELLING"], (
        assignments
    )
    # Belt AND braces: the public attribute is READ-ONLY, so a direct write is a
    # type error rather than a convention violation (review r4 P3).
    assert isinstance(trust_flow.TrustFlow.phase, property)
    assert trust_flow.TrustFlow.phase.fset is None


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", sorted(trust_flow.CLAIMED_PHASES))
async def test_a_terminal_claim_is_exclusive_against_every_claimed_phase(
    claimed: str,
) -> None:
    """TWO cleanups can never both win, and none may fire under a claim."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    # Move into the claimed phase through the CAS itself.
    assert (
        await trust_flow.transition(flow, expect=trust_flow.OPEN_PHASES, to=claimed)
        or flow.phase == claimed
    )

    assert await trust_flow.claim_terminal(flow) is False
    assert flow.phase == claimed
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_two_concurrent_terminal_claims_have_exactly_one_winner() -> None:
    user_data: dict[str, Any] = {}
    _entry(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    results = await asyncio.gather(
        trust_flow.claim_terminal(flow), trust_flow.claim_terminal(flow)
    )

    assert sorted(results) == [False, True], results
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_a_registration_loses_to_a_cleanup_that_already_claimed() -> None:
    """The registration branch must WIN a CAS before starting the bind tail.

    A registration landing while ``cancelling`` holds the claim LOSES — the
    window is already dying, the same accepted loss class as the fresh-read
    linearization point.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    sessions = _Sessions()
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    assert await trust_flow.claim_terminal(flow) is True

    started = await trust_flow._run_completion_tail(flow, _Bot(), sessions)

    assert started is False, "a cleanup's claim must beat the registration"
    assert sessions.binds == [], "no bind may run under a cancelling claim"
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-2: entry clear + flow stop are ONE critical section ─────────────────


@pytest.mark.asyncio
async def test_clearing_the_entry_also_stops_the_flow_in_one_hold() -> None:
    """The seam invalidates the token AND marks the flow terminal together.

    After it, every actor's CAS fails: no cleanup, no tap, no slice may perform
    a side effect on a flow whose ownership token is gone.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    dropped = await trust_flow.clear_topic_entry(_USER, _THREAD, user_data)

    assert dropped is not None
    assert picker_entry(user_data, _THREAD) is None
    assert flow.phase == trust_flow.PHASE_TERMINAL
    assert await trust_flow.claim_terminal(flow) is False
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok is False
    await trust_flow.teardown_thread(_USER, _THREAD)


def test_no_unlocked_entry_pops_remain_in_the_teardown_paths() -> None:
    """Grep-level proof for the r3 P1-2 rule: teardown never pops directly."""
    import cctelegram.bot as bot_module
    import cctelegram.handlers.cleanup as cleanup_module

    for module in (bot_module, cleanup_module):
        source = Path(module.__file__).read_text()  # type: ignore[arg-type]
        assert "drop_picker_entry(" not in source, module.__name__
        assert "clear_all_picker_entries(" not in source, module.__name__


# ── P1-4: teardown waits out an in-flight dispatch ─────────────────────────


@pytest.mark.asyncio
async def test_teardown_waits_for_an_in_flight_dispatch_instead_of_cancelling() -> None:
    """A dispatch owns the pane: teardown must not cancel or kill underneath it.

    Driven through the real claim: a Trust tap holds ``dispatching`` while
    teardown runs. Teardown must still be waiting when the dispatch is
    released, and only then proceed.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok

    teardown = asyncio.create_task(trust_flow.teardown_thread(_USER, _THREAD))
    await asyncio.sleep(0.1)

    assert not teardown.done(), "teardown must WAIT for the dispatch to settle"
    assert tmux.kill_calls == [], "no kill may run under a live dispatch"
    assert flow.wait_task is not None and not flow.wait_task.cancelled()

    # The callback's finally releases the claim; teardown then proceeds.
    await trust_flow.release_dispatch_claim(flow, phase=trust_flow.PHASE_AWAITING_TRUST)
    await asyncio.wait_for(teardown, timeout=5)
    assert tmux.kill_calls == ["@5"]


@pytest.mark.asyncio
async def test_the_global_ceiling_defers_under_a_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling DEFERS in ``dispatching`` rather than sparing underneath it."""
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    monkeypatch.setattr(trust_flow, "GLOBAL_CEILING_MARGIN_S", 0.05)
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.02)
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok

    # Well past the global ceiling, but a dispatch owns the flow.
    await asyncio.sleep(0.3)
    assert flow.phase == trust_flow.PHASE_DISPATCHING, (
        "the ceiling must DEFER, not terminalize, under a dispatch"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is flow

    # Released → the very next slice takes the ceiling's spare.
    await trust_flow.release_dispatch_claim(flow, phase=trust_flow.PHASE_AWAITING_TRUST)
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)
    assert tmux.kill_calls == [], "the global ceiling SPARES"
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P1-5: the terminalizer runs no side effect under the lock ──────────────


@pytest.mark.asyncio
async def test_a_registration_racing_the_terminalizer_completes_the_bind() -> None:
    """The terminalizer's guarded cleanup can lose to a registration too.

    Driven for real: the WAIT task is cancelled (a teardown), so its
    terminalizer runs the guarded cleanup — which finds the window REGISTERED
    and must hand the flow to the completion tail rather than dropping it.
    """
    from cctelegram.session import WindowState, session_manager

    user_data: dict[str, Any] = {}
    _entry(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    await asyncio.sleep(0.03)
    session_manager.window_states["@5"] = WindowState(
        session_id="sid-late", cwd="/repo", window_name="repo"
    )
    try:
        task = flow.wait_task
        assert task is not None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)
    finally:
        session_manager.window_states.pop("@5", None)

    assert tmux.kill_calls == [], "a registered window is never killed"
    assert sessions.binds == [(_USER, _THREAD, "@5")], (
        "the terminalizer must hand a SPARED_REGISTERED outcome to the completion tail"
    )


# ── P2-1: completion notes are generation-qualified + baselined ────────────


@pytest.mark.asyncio
async def test_a_completion_note_is_not_consumed_by_a_later_generation() -> None:
    user_data: dict[str, Any] = {}
    _entry(user_data)
    sessions = _Sessions()
    flow = await _start(
        user_data, tmux=_Tmux(pane=_IDLE), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    sessions.registered = True
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=3)
    assert sessions.binds, "generation 1 bound"

    # A LATER generation's teardown must not consume generation 1's note.
    assert (
        trust_flow._consume_completion(
            (_USER, _THREAD), generation=flow.generation + 99, window_id="@5"
        )
        is False
    )
    # …nor may a different window's.
    assert (
        trust_flow._consume_completion(
            (_USER, _THREAD), generation=flow.generation, window_id="@nope"
        )
        is False
    )
    assert (
        trust_flow._consume_completion(
            (_USER, _THREAD), generation=flow.generation, window_id="@5"
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_preexisting_binding_is_not_read_as_this_flows_completion() -> None:
    """The binding fallback needs a PRE-FLOW BASELINE.

    A topic already bound to the flow's window at install time proves nothing —
    only a binding that APPEARED after install is a completion.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    sessions = _Sessions()
    sessions.bind_thread(_USER, _THREAD, "@5")  # bound BEFORE the flow installs
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    generation = flow.generation
    # Drop the flow WITHOUT a completion (a cancelled wait task).
    task = flow.wait_task
    assert task is not None
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    won = trust_flow._completion_won(
        _USER,
        _THREAD,
        observed_wid="@5",
        generation=generation,
        session_mgr=sessions,
    )

    assert won is False, "a pre-existing binding is not this flow's completion"


# ── P2-2: the refresh's capture order + single lock hold ───────────────────


@pytest.mark.asyncio
async def test_the_refresh_captures_text_first_and_command_last() -> None:
    """Command-after-text: a Claude→shell flip during the pair fails closed."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    tmux.order.clear()

    await trust_flow.refresh_card_if_live(flow, _Bot(), tmux)

    assert tmux.order == ["text", "command"], tmux.order
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_the_refresh_refuses_under_a_non_awaiting_trust_phase() -> None:
    """A refresh may not mint under a cleanup's or a dispatch's claim."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok

    refreshed = await trust_flow.refresh_card_if_live(flow, _Bot(), tmux)

    assert refreshed is False
    await trust_flow.release_dispatch_claim(flow, phase=trust_flow.PHASE_AWAITING_TRUST)
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_the_refresh_refuses_when_the_entry_identity_changed() -> None:
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    # A replacement entry: same thread, different identity.
    from cctelegram.handlers.directory_browser import drop_picker_entry

    drop_picker_entry(user_data, _THREAD)
    _entry(user_data)

    refreshed = await trust_flow.refresh_card_if_live(flow, _Bot(), tmux)

    assert refreshed is False
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2-3: bounded probes are cancellation-safe ─────────────────────────────


def test_the_bounded_probes_prefer_the_cancellation_safe_variants() -> None:
    """A timed-out slice must reap its subprocess, not orphan one per slice."""
    from cctelegram.tmux_manager import tmux_manager

    assert hasattr(tmux_manager, "capture_pane_cancellation_safe")
    assert hasattr(tmux_manager, "pane_current_command_cancellation_safe")


@pytest.mark.asyncio
async def test_the_slice_probe_uses_the_cancellation_safe_methods() -> None:
    used: list[str] = []

    class _Safe(_Tmux):
        async def capture_pane_cancellation_safe(self, window_id: str) -> str:
            used.append("capture_safe")
            return self.pane

        async def pane_current_command_cancellation_safe(
            self, window_id: str
        ) -> str | None:
            used.append("command_safe")
            return self.command

    tmux = _Safe(pane=_TRUST)
    text, command = await trust_flow._probe_pane(tmux, "@5")

    assert used == ["capture_safe", "command_safe"], used
    assert text == _TRUST and command == "claude"
