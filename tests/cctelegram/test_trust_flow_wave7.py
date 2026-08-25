"""GH #65 wave 7 — the last edges of the arbitration discipline.

Round 7's findings were all "an actor leaves something behind": a fence that a
cancelled teardown never lowers, two acquisitions that still entered a claimed
phase ANONYMOUSLY, an expiry that force-claimed a bind about to win, a clear
that terminalized an open flow without cleaning its window, and a `continue`
that jumped over the ceilings.

Every claim here is held by a REAL task parked inside it.
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
from tests.cctelegram._adoption_protocol import AdoptionProtocolMixin

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_IDLE = (_FIXTURES / "inputbox_idle_v2.1.207.txt").read_text()
_THREAD = 7171
_USER = 1212
# A window id that CANNOT exist on a real tmux server, so even a future
# regression in an injection seam cannot address a live pane.
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
    monkeypatch.setattr(trust_flow, "TEARDOWN_BUDGET_S", 1.0)
    monkeypatch.setattr(trust_flow, "BIND_TAIL_GRACE_S", 0.5)
    (app_dir() / "session_map.json").unlink(missing_ok=True)
    yield
    trust_flow.reset_for_tests()
    decision_token.reset_for_tests()
    terminal_parser.reset_for_tests()
    (app_dir() / "session_map.json").unlink(missing_ok=True)


class _Tmux(AdoptionProtocolMixin):
    def __init__(self, *, command: str = "claude", pane: str = "") -> None:
        self.command = command
        self.pane = pane
        self.kill_calls: list[str] = []
        self.on_kill: Any = None

    async def pane_current_command(self, window_id: str) -> str | None:
        del window_id
        return self.command

    async def capture_pane(self, window_id: str, **kwargs: Any) -> str:
        del window_id, kwargs
        return self.pane

    async def kill_window(self, window_id: str) -> bool:
        if self.on_kill is not None:
            await self.on_kill()
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
        self.poll_times: list[float] = []
        self.on_bind: Any = None

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        del window_id, timeout
        self.poll_times.append(trust_flow._wall())
        # FAITHFUL to production (review r8 P2-B): the real implementation
        # returns IMMEDIATELY once the session-map entry exists and only sleeps
        # ``interval`` between misses. Sleeping unconditionally here made the
        # fake, not the WAIT loop, provide the inter-slice pacing — which is
        # precisely what hid the loop's missing per-slice floor.
        if self.registered:
            return True
        await asyncio.sleep(interval)
        return False

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

    def peek_session_id_for_window(self, window_id: str) -> str | None:
        # The INJECTED registered-proof, so a test never seeds the live
        # ``session_manager.window_states`` to model a registered window.
        state = self.window_states.get(window_id)
        return getattr(state, "session_id", None) or None

    def read_session_id_for_window_fresh(self, window_id: str) -> str | None:
        return self.peek_session_id_for_window(window_id)

    def iter_thread_bindings(self) -> Any:
        # The INJECTED binding authority. A test must NEVER seed the real
        # ``session_manager`` to reach a ``SPARED_BOUND``: the completion
        # tail's replay resolves through it, and a plausible window id
        # there escapes into the user's REAL tmux server.
        return [(uid, tid, wid) for uid, tid, wid in self.binds]


def _seed(user_data: dict[str, Any]) -> dict[str, Any]:
    entry = ensure_picker_entry(user_data, _THREAD)
    assert entry is not None
    entry[CARD_CHAT_ID_KEY] = -100
    entry[CARD_MSG_ID_KEY] = 999
    return entry


async def _no_replay(route: Any, user_data: Any) -> Any:
    """A pending-payload replay that goes NOWHERE.

    Injected by every unit test so the completion tail can never
    reach the live delivery path — which resolves the real
    ``session_manager`` and the real ``tmux_manager``, and would
    type into whatever window id the test happened to use.
    """
    del route, user_data
    return None


async def _start(
    user_data: dict[str, Any], *, tmux: Any, bot: Any, sessions: Any
) -> trust_flow.TrustFlow | None:
    entry = picker_entry(user_data, _THREAD)
    return await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry.get(ENTRY_TOKEN_KEY) if entry else None,
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
        replay=_no_replay,
    )


# ── P1-A: the fence is lowered on a cancelled teardown ────────────────────


@pytest.mark.asyncio
async def test_a_cancelled_teardown_lowers_its_fence() -> None:
    """A cancelled teardown must not leave the topic permanently fenced.

    Pre-fix, every future acquisition — a Trust tap, a Cancel, the WAIT's own
    cleanup claim, a registration — was refused forever, so ONLY another
    teardown could ever act on that flow again.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    inside_kill = asyncio.Event()
    hold = asyncio.Event()

    async def _park() -> None:
        inside_kill.set()
        await hold.wait()

    tmux.on_kill = _park
    teardown = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr)
    )
    await asyncio.wait_for(inside_kill.wait(), timeout=3)
    assert flow.teardown_fenced is True

    teardown.cancel()
    hold.set()
    try:
        await teardown
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await asyncio.sleep(0.05)

    if trust_flow.get_flow(_USER, _THREAD) is not None:
        assert flow.teardown_fenced is False, (
            "a cancelled teardown must LOWER the fence it raised"
        )
        # …and the topic is genuinely usable again.
        assert await trust_flow.claim_terminal(flow) is True
        await trust_flow.release_claim(
            flow,
            expect=trust_flow.PHASE_CANCELLING,
            to=trust_flow.PHASE_AWAITING_TRUST,
        )
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-B: claimed implies owned, with no exceptions ──────────────────────


@pytest.mark.asyncio
async def test_a_forced_claim_registers_its_owner() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    assert await trust_flow.force_terminal_claim(flow) is True

    assert flow.phase == trust_flow.PHASE_CANCELLING
    assert flow.claim_task is asyncio.current_task(), (
        "even a FORCED claim must register the forcing task"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_entering_a_claimed_phase_anonymously_is_a_programming_error() -> None:
    """The invariant is checked, not merely intended."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    async with trust_flow.creation_lock(_USER, _THREAD):
        assert trust_flow.try_transition_locked(
            flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_CANCELLING
        )
        assert flow.claim_task is not None
        # Simulate the pre-fix shape: a claimed phase with no owner.
        flow.claim_task = None
        with pytest.raises(AssertionError):
            trust_flow._assert_claimed_implies_owned(flow)
        flow.claim_task = asyncio.current_task()
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_a_concurrent_teardown_waits_for_a_forced_arm() -> None:
    """Round 6's fix, applied to the FORCED arm (review r7 P1-B).

    An owner-less forced claim let a second teardown poll, see nothing, time out
    and clean up beneath the first — so the forced arm must be owned too.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    inside_kill = asyncio.Event()
    alive: list[Any] = []

    async def _slow_kill() -> None:
        inside_kill.set()
        await asyncio.sleep(0.3)
        alive.append(trust_flow.get_flow(_USER, _THREAD))

    tmux.on_kill = _slow_kill
    # A task holds the claim and ignores cancellation, so the first teardown
    # takes its FORCED arm.
    entered = asyncio.Event()

    async def _stubborn() -> None:
        claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
        assert claim.ok
        entered.set()
        import time as _t

        until = _t.monotonic() + 2.0
        while _t.monotonic() < until:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass

    stubborn = asyncio.create_task(_stubborn())
    await asyncio.wait_for(entered.wait(), timeout=2)

    first = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr)
    )
    await asyncio.wait_for(inside_kill.wait(), timeout=6)
    second = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr)
    )
    await asyncio.wait_for(asyncio.gather(first, second), timeout=15)

    assert alive == [flow], (
        "the second teardown must not drop the flow beneath the forced arm's "
        "kill_window"
    )
    assert tmux.kill_calls == [_FAKE_WID], "and the window is cleaned exactly once"
    try:
        await asyncio.wait_for(asyncio.shield(stubborn), timeout=5)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_a_concurrent_teardown_waits_for_a_failed_bind_recovery() -> None:
    """The same, for the completion-failure recovery arm."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_IDLE)
    inside_kill = asyncio.Event()
    alive: list[Any] = []

    async def _slow_kill() -> None:
        inside_kill.set()
        await asyncio.sleep(0.3)
        alive.append(trust_flow.get_flow(_USER, _THREAD))

    tmux.on_kill = _slow_kill

    class _Boom(_Sessions):
        def get_window_state(self, window_id: str) -> Any:
            raise RuntimeError("state exploded")

    sessions = _Boom()
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(inside_kill.wait(), timeout=5)

    teardown = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=sessions)
    )
    await asyncio.wait_for(teardown, timeout=10)

    assert alive == [flow], (
        "teardown must not drop the flow beneath the failed-bind recovery's kill_window"
    )
    assert tmux.kill_calls == [_FAKE_WID]


# ── P1-C: an expiry must not force-claim a bind about to win ─────────────


@pytest.mark.asyncio
async def test_an_expiring_teardown_lets_a_live_bind_tail_win() -> None:
    """The budget expires while a retained bind is MID-FLIGHT, and it wins.

    Pre-fix the expiry arm ran first and force-claimed the live bind, so
    ``completed`` stayed False and ``/start`` skipped the bound-topic teardown
    it owed for a topic that DID bind.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()
    slow = asyncio.Event()

    class _SlowBot(_Bot):
        async def edit_message_text(self, **kwargs: Any) -> Any:
            # The tail's final card edit is slow, so the bind is genuinely
            # in flight when the teardown budget expires.
            slow.set()
            await asyncio.sleep(0.6)
            return await super().edit_message_text(**kwargs)

    flow = await _start(user_data, tmux=tmux, bot=_SlowBot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(slow.wait(), timeout=5)
    assert flow.phase == trust_flow.PHASE_COMPLETING_BIND

    completed = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=sessions),
        timeout=10,
    )

    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], "the bind WON"
    assert completed is True, (
        "a teardown whose budget expires over a WINNING bind must still report "
        "the completion its caller needs"
    )
    assert tmux.kill_calls == [], "a winning bind's window is never killed"


@pytest.mark.asyncio
async def test_start_runs_the_bound_topic_teardown_for_a_racing_bind() -> None:
    """The consequence at the seam: ``/start`` must not skip its obligation."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    slow = asyncio.Event()

    class _SlowBot(_Bot):
        async def edit_message_text(self, **kwargs: Any) -> Any:
            slow.set()
            await asyncio.sleep(0.4)
            return await super().edit_message_text(**kwargs)

    flow = await _start(
        user_data, tmux=_Tmux(pane=_IDLE), bot=_SlowBot(), sessions=sessions
    )
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(slow.wait(), timeout=5)

    completed = await asyncio.wait_for(
        trust_flow.teardown_all_for_user(_USER, session_mgr=sessions), timeout=10
    )

    assert completed == [_THREAD], (
        "the sweep must report the completion so /start runs the bound-topic teardown"
    )


# ── P1-D: a flow installed in the teardown→clear gap ────────────────────


@pytest.mark.asyncio
async def test_a_flow_installed_in_the_teardown_to_clear_gap_is_cleaned_up() -> None:
    """The clear must never terminalize an OPEN flow without cleaning it.

    Driven for real: a flow is installed AFTER ``teardown_thread`` returns and
    BEFORE ``clear_topic_entry`` runs — the exact gap the topic-close and
    ``/start`` sequences leave.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()

    # Nothing exists yet: the first teardown is a no-op.
    assert await trust_flow.teardown_thread(_USER, _THREAD) is False
    # …and a creation callback lands in the gap.
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    entry = picker_entry(user_data, _THREAD)
    assert entry is not None
    await asyncio.sleep(0.03)

    dropped = await asyncio.wait_for(
        trust_flow.clear_topic_entry(_USER, _THREAD, user_data, session_mgr=sessions),
        timeout=10,
    )

    assert tmux.kill_calls == [_FAKE_WID], (
        "a flow discovered by the clear must have its window GUARD-cleaned, "
        "not orphaned"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None
    del dropped


# ── P2: the ceilings are reachable on every slice ───────────────────────


@pytest.mark.asyncio
async def test_a_lost_registration_cas_still_reaches_the_global_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A winner that NEVER terminalizes must not make WAIT hot-spin forever.

    The lost-CAS path used to ``continue`` past the deadline block, so the
    global ceiling was unreachable and the loop re-read session_map at full
    speed. It must still sleep between slices AND still hit the ceiling.
    """
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    monkeypatch.setattr(trust_flow, "GLOBAL_CEILING_MARGIN_S", 0.1)
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    wait_task = flow.wait_task
    assert wait_task is not None

    # A winner takes the claim and NEVER commits terminal.
    entered = asyncio.Event()

    async def _never_finishes() -> None:
        claim = await trust_flow.claim_for_cancel(
            _USER, _THREAD, user_data=user_data, card_generation=flow.generation
        )
        assert claim.ok
        entered.set()
        await asyncio.sleep(30)

    winner = asyncio.create_task(_never_finishes())
    await asyncio.wait_for(entered.wait(), timeout=2)
    sessions.registered = True
    sessions.poll_times.clear()

    # Well past the global ceiling, WAIT is still observing — it CANNOT
    # terminalize while the winner holds the claim, and that is correct: the
    # claim's owner owns the outcome. What must NOT happen is a hot spin.
    await asyncio.sleep(0.4)
    assert not wait_task.done()
    gaps = [b - a for a, b in zip(sessions.poll_times, sessions.poll_times[1:])]
    assert gaps, "the loop must have polled more than once"
    assert min(gaps) >= trust_flow.SLICE_S * 0.5, (
        f"the loop must SLEEP between slices, got gaps {gaps}"
    )

    # The moment the claim is released, the ceiling — which has been evaluated
    # on every slice all along — fires at once.
    winner.cancel()
    try:
        await winner
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await trust_flow.release_claim(
        flow,
        expect=trust_flow.PHASE_CANCELLING,
        to=trust_flow.PHASE_AWAITING_TRUST,
    )
    await asyncio.wait_for(wait_task, timeout=6)

    assert wait_task.done()
    assert tmux.kill_calls == [], "the global ceiling SPARES"


# ── P2 (round 6): a `continue` must never jump the ceiling ─────────────────


@pytest.mark.asyncio
async def test_a_finished_bind_tail_that_leaves_the_phase_cannot_spin_teardown() -> (
    None
):
    """A completed bind tail that does NOT move the phase must still settle.

    ``completing_bind`` is awaited BEFORE any force claim (r7 P1-C), and on a
    done tail the loop re-reads the phase. But a tail can finish WITHOUT
    terminalizing — it raised, or its own terminalizer lost its CAS — and the
    phase then stays ``completing_bind`` forever. Re-awaiting the same completed
    task on every pass spins the loop at full speed and never reaches the
    ceiling: teardown NEVER RETURNS, so ``/start`` and topic-close hang with it.

    Driven for real: a genuine tail task runs to completion and leaves the phase
    exactly where it was.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_IDLE)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    wait_task = flow.wait_task
    if wait_task is not None:
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    finished = asyncio.Event()

    async def _tail_that_leaves_the_phase() -> None:
        await asyncio.sleep(0.02)
        finished.set()

    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_COMPLETING_BIND
    )
    flow.bind_task = asyncio.create_task(_tail_that_leaves_the_phase())

    # The whole point: this must RETURN, well inside the teardown budget.
    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=5)

    assert finished.is_set(), "the retained tail is still AWAITED, never abandoned"
    assert trust_flow.get_flow(_USER, _THREAD) is None, "the flow is settled"
