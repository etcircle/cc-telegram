"""GH #65 wave 8 — the two invariants, the hung tail, and the owned fence.

Round 8's P1s were both reachability holes on TEARDOWN's own path:

  * P1-A the aborted-teardown restore handed a flow back to an observer that
    teardown had ALREADY killed, recreating the round-6 "no observer" park on a
    different door. Wave 8 adopts it as an INVARIANT — a transition into any
    OPEN phase is legal only while a LIVE ``wait_task`` exists — enforced inside
    ``try_transition_locked`` so no future caller can reopen the class.
  * P1-B a genuinely hung bind tail deadlocked teardown against its own WAIT
    task, whose terminalizer was shield-awaiting that same tail unbounded.

Every flow here is driven through the public seams with REAL tasks; nothing
reconstructs phase state by hand.
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
_THREAD = 8181
_USER = 2323
# A window id that CANNOT exist on a real tmux server.
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
    monkeypatch.setattr(trust_flow, "BIND_TAIL_GRACE_S", 0.3)
    monkeypatch.setattr(trust_flow, "ORPHAN_CLEANUP_BUDGET_S", 0.3)
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

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        del window_id, timeout
        self.poll_times.append(trust_flow._wall())
        # FAITHFUL to production: immediate return once the entry exists.
        if self.registered:
            return True
        await asyncio.sleep(interval)
        return False

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

    def peek_session_id_for_window(self, window_id: str) -> str | None:
        return getattr(self.window_states.get(window_id), "session_id", None) or None

    def read_session_id_for_window_fresh(self, window_id: str) -> str | None:
        return self.peek_session_id_for_window(window_id)

    def iter_thread_bindings(self) -> Any:
        return list(self.binds)


def _seed(user_data: dict[str, Any]) -> dict[str, Any]:
    entry = ensure_picker_entry(user_data, _THREAD)
    assert entry is not None
    entry[CARD_CHAT_ID_KEY] = -100
    entry[CARD_MSG_ID_KEY] = 999
    return entry


async def _no_replay(route: Any, user_data: Any) -> Any:
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


# ── P1-A: OPEN implies OBSERVED ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_aborted_teardown_never_restores_an_unobserved_flow() -> None:
    """The abort-restore must leave a LIVE observer, or terminalize.

    Teardown cancels and awaits the WAIT task BEFORE the guarded cleanup, so if
    that cleanup then raises the outer ``finally`` used to restore
    ``cancelling → awaiting_trust`` and lower the fence — handing the flow back
    to an observer that is already DEAD. Nothing then drives the ceilings, the
    registration or the bind: the topic is parked forever and every inbound
    message nudges a flow nothing will ever advance.

    Driven for real: teardown is CANCELLED while parked INSIDE the guarded
    cleanup — the shape a topic-close or a shutdown actually produces. By then
    it has already cancelled and awaited the WAIT task, so the observer is
    genuinely dead when the ``finally`` runs. (A raising ``kill_window`` would
    NOT do: cleanup types that outcome as ``KILL_FAILED`` rather than
    propagating, so it never reaches the abort arm.)
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)

    inside_cleanup = asyncio.Event()
    never = asyncio.Event()

    async def _park_inside_the_cleanup() -> None:
        inside_cleanup.set()
        await never.wait()

    tmux.on_kill = _park_inside_the_cleanup
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    teardown = asyncio.create_task(trust_flow.teardown_thread(_USER, _THREAD))
    await asyncio.wait_for(inside_cleanup.wait(), timeout=5)
    observer_at_cancel = flow.wait_task
    assert observer_at_cancel is not None and observer_at_cancel.done(), (
        "premise: teardown kills the observer BEFORE the guarded cleanup"
    )
    teardown.cancel()
    try:
        await teardown
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await asyncio.sleep(0.05)

    observer = flow.wait_task
    survivor = trust_flow.get_flow(_USER, _THREAD)
    if survivor is not None:
        # THE INVARIANT: a surviving flow in an OPEN phase must still be watched.
        assert observer is not None and not observer.done(), (
            "an aborted teardown restored an OPEN flow whose observer is DEAD — "
            "nothing will ever advance this topic again"
        )
    else:
        assert flow.phase == trust_flow.PHASE_TERMINAL, (
            "a flow dropped by the abort path must be TERMINAL, not merely gone"
        )


@pytest.mark.asyncio
async def test_the_cas_refuses_to_open_a_flow_whose_observer_is_dead() -> None:
    """The invariant is enforced at the ONE mutator, not at each call site.

    Round 6 fixed one door into this room (the WAIT loop's own lost-CAS
    return) and round 8 found another. Enforcing it inside
    ``try_transition_locked`` closes the CLASS: any future path that tries to
    park a flow OPEN without an observer trips here.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    # Kill the observer the way teardown does, then try to hand the flow back.
    wait_task = flow.wait_task
    assert wait_task is not None
    wait_task.cancel()
    try:
        await wait_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    assert wait_task.done()

    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_CANCELLING
    )

    with pytest.raises(AssertionError, match="DEAD observer"):
        await trust_flow.transition(
            flow,
            expect=frozenset({trust_flow.PHASE_CANCELLING}),
            to=trust_flow.PHASE_AWAITING_TRUST,
        )

    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-B: a hung bind tail must not deadlock teardown ─────────────────────


@pytest.mark.asyncio
async def test_a_genuinely_hung_bind_tail_cannot_deadlock_teardown() -> None:
    """Force-settle means the bind tail DIES.

    The deadlock: teardown's force branch cancels and awaits the WAIT task,
    whose terminalizer was shield-awaiting the SAME unfinished ``bind_task``
    with no timeout. ``/start``, topic-close and shutdown hung forever.

    The tail here waits on an Event nobody sets — it swallows nothing, so
    cancellation genuinely cancels it, which is the whole point: the fix is that
    somebody actually cancels it.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_IDLE)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    wait_task = flow.wait_task
    assert wait_task is not None

    never = asyncio.Event()
    entered = asyncio.Event()

    async def _hung_tail() -> None:
        entered.set()
        await never.wait()

    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_COMPLETING_BIND
    )
    flow.bind_task = asyncio.create_task(_hung_tail())
    await asyncio.wait_for(entered.wait(), timeout=2)

    # Must RETURN — inside the teardown budget plus the tail grace, nowhere near
    # the 10s ceiling this assertion would otherwise blow through.
    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert flow.bind_task is not None and flow.bind_task.done(), (
        "the hung tail must be CANCELLED, not left running"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is None, "the flow is settled"
    assert tmux.kill_calls == [_FAKE_WID], (
        "and the window is cleaned up — a force-settled flow must not leak it"
    )


# ── P2-A: the fence has an owner ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_teardown_cannot_lower_a_fence_another_teardown_raised() -> None:
    """The fence is OWNED (task + generation), not a shared boolean.

    With a bare flag the FIRST of two concurrent teardowns to finish cleared the
    fence the SECOND was relying on, so the survivor ran unfenced and a stream
    of new claims could starve it — the exact failure the fence exists to stop.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    async def _other_teardown_raises_the_fence() -> None:
        async with trust_flow.creation_lock(_USER, _THREAD):
            flow.raise_fence()

    owner = asyncio.create_task(_other_teardown_raises_the_fence())
    await owner
    assert flow.teardown_fenced, "the other task owns the fence"

    # A DIFFERENT task must not be able to lower it.
    async with trust_flow.creation_lock(_USER, _THREAD):
        lowered = flow.lower_fence_if_owned()
    assert lowered is False, "a non-owner lowered another teardown's fence"
    assert flow.teardown_fenced, "…and the fence must still be up"

    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2-B: the WAIT loop has its own per-slice floor ───────────────────────


@pytest.mark.asyncio
async def test_the_wait_loop_paces_itself_when_the_registration_poll_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REGISTERED slice that stays in the loop must still cost a slice.

    ``wait_for_session_map_entry`` returns IMMEDIATELY once the entry exists, so
    the loop's only pacing on that branch was the fake's own sleep. In
    production a registered flow whose completion tail keeps losing its CAS
    (a claim is held) re-read ``session_map.json`` continuously.

    The claim is held by a REAL parked task, which is what keeps the tail losing.
    """
    monkeypatch.setattr(trust_flow, "SLICE_S", 0.05)
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None

    entered = asyncio.Event()

    async def _hold_the_claim() -> None:
        claim = await trust_flow.claim_for_cancel(
            _USER, _THREAD, user_data=user_data, card_generation=flow.generation
        )
        assert claim.ok
        entered.set()
        await asyncio.sleep(30)

    holder = asyncio.create_task(_hold_the_claim())
    await asyncio.wait_for(entered.wait(), timeout=2)

    sessions.registered = True
    sessions.poll_times.clear()
    await asyncio.sleep(0.4)

    polls = len(sessions.poll_times)
    assert polls > 1, "the loop must keep observing"
    # With SLICE_S = 0.05 over ~0.4s, a paced loop polls on the order of ten
    # times; an unpaced one spins thousands of times re-reading the map.
    assert polls < 30, f"the WAIT loop is hot-spinning: {polls} polls in 0.4s"

    holder.cancel()
    try:
        await holder
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P3: strictness is armed at import, not by test order ──────────────────


def test_invariants_are_strict_without_any_reset_having_run() -> None:
    """A single test run in ISOLATION must already be strict.

    Arming strictness only inside ``reset_for_tests`` made it test-ORDER
    dependent — and a failure is normally reproduced by running ONE test.
    """
    assert trust_flow._STRICT_INVARIANTS is True
