"""GH #65 wave 5 — teardown by POSITIVE PROOF, not by stale snapshot.

The round-5 P1s shared one root: teardown reasoned from the phase it captured
ONCE and had no handle on the task actually doing the work, so settlement was
timer inference. Wave 5 registers the claim's owner on the flow, makes teardown
a LOOP over the flow's CURRENT phase, and settles a claim by cancelling and
awaiting the task that holds it.

Every claim in here is held by a REAL task genuinely parked inside it — never a
synthetic phase write.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
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
_THREAD = 5150
_USER = 8080
# A window id shape tmux CANNOT mint, so a seam that regressed to the live
# ``tmux_manager`` could never resolve it to a real pane.
_FAKE_WID = "@fake-trust-test"


@pytest.fixture(autouse=True)
def _lane(monkeypatch: pytest.MonkeyPatch) -> Any:
    terminal_parser.set_decision_cards_enabled(True)
    decision_token.set_trust_card_dispatch_enabled(True)
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 30.0)
    monkeypatch.setattr(config, "hook_timeout_override", 0.05)
    monkeypatch.setattr(config, "hook_timeout_extension_s", 0.05)
    monkeypatch.setattr(trust_flow, "SLICE_S", 0.01)
    monkeypatch.setattr(trust_flow, "PANE_POLL_EVERY_S", 0.0)
    monkeypatch.setattr(trust_flow, "DISPATCH_SETTLE_BUDGET_S", 0.2)
    # The OVERALL teardown budget (production 120s) — no test needs it.
    monkeypatch.setattr(trust_flow, "TEARDOWN_BUDGET_S", 1.0)
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

    async def pane_current_command(self, window_id: str) -> str | None:
        del window_id
        return self.command

    async def capture_pane(self, window_id: str, **kwargs: Any) -> str:
        del window_id, kwargs
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

    def texts(self) -> list[str]:
        return [str(e.get("text") or "") for e in self.edits]


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

    def iter_thread_bindings(self) -> Any:
        # The INJECTED binding authority — a unit test never reads (or seeds)
        # the live ``session_manager``.
        return list(self.binds)

    def peek_session_id_for_window(self, window_id: str) -> str | None:
        # The INJECTED registered-proof, for the same reason.
        return getattr(self.window_states.get(window_id), "session_id", None) or None

    def read_session_id_for_window_fresh(self, window_id: str) -> str | None:
        return self.peek_session_id_for_window(window_id)


async def _no_replay(route: Any, user_data: Any) -> Any:
    """A pending-payload replay that goes NOWHERE.

    Injected by every test so the completion tail can never reach the real
    delivery path, which resolves the live ``session_manager`` and the live
    ``tmux_manager`` — and would type the pending payload into whatever window
    id happened to exist on the developer's default tmux server.
    """
    del route, user_data
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
    wid: str = _FAKE_WID,
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
        replay=_no_replay,
    )


async def _park_claim(
    flow: trust_flow.TrustFlow,
    user_data: dict[str, Any],
    *,
    cancel: bool = False,
) -> tuple[asyncio.Task[None], asyncio.Event, asyncio.Event]:
    """Park a REAL task inside a real claim (the shape production produces)."""
    entered = asyncio.Event()
    released = asyncio.Event()

    async def _callback() -> None:
        claim = (
            await trust_flow.claim_for_cancel(
                _USER,
                _THREAD,
                user_data=user_data,
                card_generation=flow.generation,
            )
            if cancel
            else await trust_flow.claim_for_dispatch(
                _USER, _THREAD, user_data=user_data
            )
        )
        assert claim.ok
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            await trust_flow.release_claim(
                flow,
                expect=trust_flow.PHASE_CANCELLING
                if cancel
                else trust_flow.PHASE_DISPATCHING,
                to=claim.previous_phase or trust_flow.PHASE_AWAITING_TRUST,
            )
            released.set()

    task = asyncio.create_task(_callback())
    await asyncio.wait_for(entered.wait(), timeout=2)
    return task, entered, released


# ── P1-B: settlement is positive proof ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_claim_registers_the_task_that_holds_it() -> None:
    user_data: dict[str, Any] = {}
    _entry(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    assert flow.claim_task is None

    task, _entered, _released = await _park_claim(flow, user_data)
    assert flow.claim_task is task

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    assert flow.claim_task is None, "the handle dies with the claim"
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_teardown_cancels_and_awaits_the_claim_owner() -> None:
    """Teardown settles by CANCELLING + AWAITING, not by waiting out a timer.

    The proof: the claim's owner has genuinely finished (its ``finally`` ran)
    before teardown touches the window at all.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    task, _entered, released = await _park_claim(flow, user_data)

    won = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=5,
    )

    assert won is False
    assert task.done() and released.is_set(), (
        "the claim's owner must be settled with positive proof"
    )
    assert tmux.kill_calls == [_FAKE_WID]
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None


@pytest.mark.asyncio
async def test_teardown_settles_a_cancelling_claim_too() -> None:
    """Not just ``dispatching`` — every TASK-held claim settles the same way."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    task, _entered, released = await _park_claim(flow, user_data, cancel=True)
    assert flow.phase == trust_flow.PHASE_CANCELLING

    await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=5,
    )

    assert task.done() and released.is_set()
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P1-A: teardown re-dispatches on the CURRENT phase ─────────────────────


@pytest.mark.asyncio
async def test_a_claim_taken_after_the_snapshot_is_still_handled() -> None:
    """The exact P1-A shape: the flow is OPEN when teardown looks, and a real
    callback claims it while teardown is mid-choreography.

    The pre-fix teardown acted on its one snapshot and then dropped the flow
    unconditionally — returning while a healthy callback was mid-side-effect.
    The loop must notice the new phase and settle THAT claim too.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    claimed: dict[str, Any] = {}

    async def _late_claimer() -> None:
        # Claim as soon as the flow is open again after teardown's first pass.
        for _ in range(200):
            claim = await trust_flow.claim_for_dispatch(
                _USER, _THREAD, user_data=user_data
            )
            if claim.ok:
                claimed["task"] = asyncio.current_task()
                try:
                    await asyncio.sleep(30)
                finally:
                    await trust_flow.release_claim(
                        flow,
                        expect=trust_flow.PHASE_DISPATCHING,
                        to=trust_flow.PHASE_AWAITING_TRUST,
                    )
                return
            await asyncio.sleep(0.005)

    racer = asyncio.create_task(_late_claimer())
    won = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=5,
    )
    racer.cancel()
    try:
        await racer
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    assert won is False
    assert trust_flow.get_flow(_USER, _THREAD) is None, (
        "teardown must exit only from a terminal reading"
    )
    assert picker_entry(user_data, _THREAD) is None


@pytest.mark.asyncio
async def test_teardown_exits_only_from_terminal() -> None:
    """A flow already marked terminal is dropped, not re-cleaned."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_TERMINAL
    )

    won = await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=5)

    assert won is False
    assert tmux.kill_calls == [], "a terminal flow's window was already settled"
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P1-C: the tail's outcome branch is reachable ──────────────────────────


@pytest.mark.asyncio
async def test_a_raising_tail_reaches_the_guarded_cleanup() -> None:
    """The shield PROPAGATES the tail's exception, so the outcome branch below
    it was dead code on exactly the failure path it was written for.

    Driven for real: the session manager raises inside the tail.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    bot = _Bot()
    tmux = _Tmux(pane=_IDLE)

    class _Boom(_Sessions):
        def get_window_state(self, window_id: str) -> Any:
            raise RuntimeError("state exploded")

    sessions = _Boom()
    flow = await _start(user_data, tmux=tmux, bot=bot, sessions=sessions)
    assert flow is not None
    sessions.registered = True
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=3)

    assert sessions.binds == []
    assert tmux.kill_calls == [_FAKE_WID], (
        "a raising tail must still reach the guarded cleanup"
    )
    assert any("couldn't finish binding" in t for t in bot.texts()), bot.texts()
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None


# ── P1-D: an aborted Cancel leaves an OBSERVED flow ──────────────────────


@pytest.mark.asyncio
async def test_an_aborted_cancel_leaves_a_flow_that_is_still_observed() -> None:
    """A restored claim must hand back a flow something still watches.

    The pre-fix Cancel destroyed the WAIT task BEFORE the cleanup, so an
    abort-path release produced an open flow with no observer: no ceilings, no
    registration, no bind. Here the cleanup raises, the callback's ``finally``
    restores the acquired phase, and the flow's ceilings must still fire.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    wait_task = flow.wait_task
    assert wait_task is not None

    class _Exploding:
        async def kill_window(self, window_id: str) -> bool:
            raise RuntimeError("boom")

    from cctelegram.callback_dispatcher import trust as trust_cb

    class _Q:
        def __init__(self) -> None:
            self.message = None

        async def answer(self, *a: Any, **kw: Any) -> None:
            pass

        async def edit_message_text(self, *a: Any, **kw: Any) -> None:
            pass

    class _Ctx:
        def __init__(self) -> None:
            self.user_data = user_data
            self.bot = _Bot()

    class _U:
        id = _USER

    await trust_cb._handle_cancel(
        _Q(), _Ctx(), _U(), _THREAD, str(flow.generation), _Exploding()
    )

    # kill_window raising classifies KILL_FAILED, which IS a terminal outcome —
    # the contract is that the flow is never left claimed-and-unobserved.
    assert flow.phase != trust_flow.PHASE_CANCELLING
    if trust_flow.get_flow(_USER, _THREAD) is not None:
        assert wait_task is not None and not wait_task.done(), (
            "a restored flow must still have its observer alive"
        )
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_the_wait_task_survives_the_cleanup_under_a_cancel_claim() -> None:
    """The WAIT task stays alive ACROSS the guarded cleanup (r5 P1-D).

    Holding ``cancelling`` already excludes it — every CAS it attempts loses —
    so keeping it means a restored claim hands back an observed flow.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    wait_task = flow.wait_task
    assert wait_task is not None and not wait_task.done()

    alive_during_cleanup: list[bool] = []

    class _Watching(_Tmux):
        async def kill_window(self, window_id: str) -> bool:
            alive_during_cleanup.append(wait_task is not None and not wait_task.done())
            return await super().kill_window(window_id)

    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    await trust_flow.cancel_flow(flow, _Bot(), _Watching())

    assert alive_during_cleanup == [True], (
        "the WAIT task must still be alive while the cleanup runs"
    )
    assert wait_task.done(), "…and settled before the terminal card edit"


# ── P2-A: SPARED_BOUND completes its own teardown ────────────────────────


@pytest.mark.asyncio
async def test_a_spared_bound_cancel_leaves_nothing_behind() -> None:
    user_data: dict[str, Any] = {}
    _entry(user_data)
    bot = _Bot()
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()
    # The window is already BOUND — stated through the INJECTED authority. The
    # live ``session_manager`` is never seeded: doing so used to route the
    # completion tail's replay at the real tmux server.
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)
    flow = await _start(user_data, tmux=tmux, bot=bot, sessions=sessions)
    assert flow is not None
    await asyncio.sleep(0.03)
    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    outcome = await trust_flow.cancel_flow(flow, bot, tmux)

    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert trust_flow.get_flow(_USER, _THREAD) is None, "the flow is dropped"
    assert picker_entry(user_data, _THREAD) is None, "the entry is dropped"
    assert flow.token is None or decision_token.peek(flow.token) is None
    assert bot.edits, "and the card gets a final edit"


# ── P2-B: fail-closed generation matching + baseline TTL ─────────────────


def test_completion_matching_without_a_generation_fails_closed() -> None:
    assert (
        trust_flow._completion_won(
            _USER, _THREAD, observed_wid=_FAKE_WID, generation=None
        )
        is False
    )


@pytest.mark.asyncio
async def test_an_expired_baseline_is_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EXPIRED baseline must not be treated as "no pre-existing binding"."""
    user_data: dict[str, Any] = {}
    _entry(user_data)
    sessions = _Sessions()
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    generation = flow.generation
    await trust_flow.teardown_thread(_USER, _THREAD, session_mgr=sessions)
    # Age the baseline past its TTL.
    monkeypatch.setattr(trust_flow, "_COMPLETION_NOTE_TTL_S", 0.0)

    assert (
        trust_flow._completion_won(
            _USER,
            _THREAD,
            observed_wid=_FAKE_WID,
            generation=generation,
            session_mgr=sessions,
        )
        is False
    )


@pytest.mark.asyncio
async def test_the_start_sweep_carries_the_generation() -> None:
    """``/start``'s snapshot must name the generation, or a vanished flow could
    match an OLDER note."""
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
    assert sessions.binds, "the bind completed"
    # The note is this generation's; a sweep that omitted the generation would
    # let ANY later teardown consume it.
    assert (
        trust_flow._consume_completion(
            (_USER, _THREAD), generation=flow.generation, window_id=_FAKE_WID
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_raising_tail_under_cancel_does_not_escape_the_seam() -> None:
    """P1-C, isolated to a caller that is NOT the WAIT task.

    ``cancel_flow`` hands a SPARED_REGISTERED outcome to the completion seam. If
    the seam awaits the retained tail with a bare shield, the tail's exception
    PROPAGATES out of ``cancel_flow`` — its own outcome branch never runs, the
    window is left unbound and the caller's abort path takes over. The seam must
    absorb the tail's failure and run the guarded cleanup itself.
    """
    user_data: dict[str, Any] = {}
    _entry(user_data)
    bot = _Bot()
    tmux = _Tmux(pane=_TRUST)

    class _Boom(_Sessions):
        def get_window_state(self, window_id: str) -> Any:
            raise RuntimeError("state exploded")

    sessions = _Boom()
    flow = await _start(user_data, tmux=tmux, bot=bot, sessions=sessions)
    assert flow is not None
    await asyncio.sleep(0.03)
    # The REGISTERED proof, through the INJECTED authority only.
    sessions.window_states[_FAKE_WID] = SimpleNamespace(
        session_id="sid-raced", cwd="/repo", window_name="repo"
    )
    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    # Must NOT raise out of the seam.
    outcome = await trust_flow.cancel_flow(flow, bot, tmux)

    assert outcome is trust_flow.CleanupOutcome.SPARED_REGISTERED
    assert sessions.binds == [], "the tail genuinely failed"
    assert any("couldn't finish binding" in t for t in bot.texts()), bot.texts()
    assert flow.phase == trust_flow.PHASE_TERMINAL, (
        "a failed tail must still reach a terminal state, not stay claimed"
    )
