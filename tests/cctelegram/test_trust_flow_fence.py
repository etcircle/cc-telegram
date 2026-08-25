"""GH #65 wave 6 — the teardown fence, claim ownership everywhere, and the
two dispositions of a SPARED_BOUND cancel.

Round 6's P1s were the last places where an actor could act without a handle on
whoever else was working: a teardown loop that could be STARVED by successive
claims and ended in a blind registry drop; ``claim_terminal`` acquisitions with
no registered owner, so a live ``cancelling`` claim polled as "settled"; and a
WAIT loop that EXITED when its registration CAS lost, leaving the flow with no
observer if the winner then aborted.

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

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_IDLE = (_FIXTURES / "inputbox_idle_v2.1.207.txt").read_text()
_THREAD = 6161
_USER = 9090
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


# ── P1-A: the fence ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_teardown_fence_refuses_new_claims() -> None:
    """While a teardown runs, no NEW claim may be acquired.

    Without it a stream of taps could starve the loop forever — and the old
    pass counter's exhaustion ended in a blind registry drop.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    async with trust_flow.creation_lock(_USER, _THREAD):
        # The fence is OWNED now (review r8 P2-A) — raised through its seam, not
        # by assigning a flag, so only its owner can lower it again.
        flow.raise_fence()
    assert flow.teardown_fenced

    assert (
        await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    ).ok is False
    assert (
        await trust_flow.claim_for_cancel(
            _USER, _THREAD, user_data=user_data, card_generation=flow.generation
        )
    ).ok is False
    assert await trust_flow.claim_terminal(flow) is False, "WAIT's cleanup claim too"
    # …but TEARDOWN's own claim is not locked out of its own topic.
    assert await trust_flow.claim_terminal(flow, ignore_fence=True) is True
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_a_storm_of_taps_cannot_starve_the_teardown() -> None:
    """The starvation shape, driven for real: a task claims and releases in a
    tight loop while teardown runs. The fence must let teardown finish."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    stop = asyncio.Event()

    async def _storm() -> None:
        while not stop.is_set():
            claim = await trust_flow.claim_for_dispatch(
                _USER, _THREAD, user_data=user_data
            )
            if claim.ok:
                await trust_flow.release_claim(
                    flow,
                    expect=trust_flow.PHASE_DISPATCHING,
                    to=trust_flow.PHASE_AWAITING_TRUST,
                )
            await asyncio.sleep(0)

    storm = asyncio.create_task(_storm())
    try:
        won = await asyncio.wait_for(
            trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
            timeout=5,
        )
    finally:
        stop.set()
        storm.cancel()
        try:
            await storm
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    assert won is False
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert tmux.kill_calls == [_FAKE_WID], "the window is still properly settled"
    assert picker_entry(user_data, _THREAD) is None


@pytest.mark.asyncio
async def test_the_teardown_budget_force_settles_it_never_drops_blindly() -> None:
    """On budget expiry the window is CLEANED, not abandoned.

    A task that ignores its own cancellation holds the claim past the budget;
    teardown must force-settle — cancel the WAIT task, guarded cleanup, drop —
    rather than releasing tokens and dropping the registry entry blind.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    entered = asyncio.Event()

    async def _uncancellable() -> None:
        claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
        assert claim.ok
        entered.set()
        # Swallows cancellation for longer than the teardown budget, then lets
        # go on its OWN wall clock — a task that never lets go would hang the
        # event loop's shutdown, which is not what is under test here.
        import time as _t

        until = _t.monotonic() + 3.0
        while _t.monotonic() < until:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass

    stuck = asyncio.create_task(_uncancellable())
    await asyncio.wait_for(entered.wait(), timeout=2)

    won = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=8,
    )

    assert won is False
    assert tmux.kill_calls == [_FAKE_WID], (
        "the budget's expiry must CLEAN the window, never drop it blind"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None
    # The stuck task exits on its own clock; just let it finish.
    try:
        await asyncio.wait_for(asyncio.shield(stuck), timeout=6)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


# ── P1-B: claim ownership on EVERY acquisition ────────────────────────────


@pytest.mark.asyncio
async def test_claim_terminal_registers_its_owner_too() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    assert await trust_flow.claim_terminal(flow) is True
    assert flow.claim_task is asyncio.current_task(), (
        "EVERY acquisition registers the task that holds it"
    )
    assert flow.claim_cancellable is False, "a cooperative claim is not cancelled"
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_two_concurrent_teardowns_do_not_drop_beneath_each_other() -> None:
    """One teardown wins; the other WAITS and then observes terminal.

    Codex's proof case: with no owner registered for a ``cancelling`` claim, the
    settle fallback polled only for ``dispatching`` and reported the live claim
    settled on its first poll — so the second teardown dropped the flow while
    the first was still inside ``kill_window``.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    inside_kill = asyncio.Event()
    flow_alive_during_kill: list[Any] = []

    async def _slow_kill() -> None:
        inside_kill.set()
        await asyncio.sleep(0.25)
        flow_alive_during_kill.append(trust_flow.get_flow(_USER, _THREAD))

    tmux.on_kill = _slow_kill

    first = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr)
    )
    await asyncio.wait_for(inside_kill.wait(), timeout=3)
    second = asyncio.create_task(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr)
    )

    await asyncio.wait_for(asyncio.gather(first, second), timeout=8)

    assert flow_alive_during_kill == [flow], (
        "the second teardown must NOT drop the flow beneath the first's kill_window"
    )
    assert tmux.kill_calls == [_FAKE_WID], "and the window is killed exactly once"
    assert trust_flow.get_flow(_USER, _THREAD) is None


@pytest.mark.asyncio
async def test_a_teardown_does_not_drop_beneath_the_wait_terminalizer() -> None:
    """Same shape, with the WAIT terminalizer holding the cleanup claim."""
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
        await asyncio.sleep(0.25)
        alive.append(trust_flow.get_flow(_USER, _THREAD))

    tmux.on_kill = _slow_kill
    # Cancel the WAIT task: its terminalizer claims ``cancelling`` and runs the
    # guarded cleanup, which parks inside kill_window.
    wait_task = flow.wait_task
    assert wait_task is not None
    wait_task.cancel()
    await asyncio.wait_for(inside_kill.wait(), timeout=3)

    await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=8,
    )

    assert alive == [flow], (
        "teardown must not drop the flow beneath the terminalizer's kill_window"
    )
    assert tmux.kill_calls == [_FAKE_WID]
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P1-C: WAIT keeps observing after a lost registration CAS ─────────────


@pytest.mark.asyncio
async def test_wait_keeps_observing_when_its_registration_cas_loses() -> None:
    """A registration landing after Cancel's linearization point loses the CAS —
    and WAIT must NOT exit.

    Then Cancel is CANCELLED mid-``kill_window`` and its ``finally`` restores the
    acquired open phase. The restored flow must still have a live observer, and
    its ceilings must still fire.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    await asyncio.sleep(0.03)
    wait_task = flow.wait_task
    assert wait_task is not None and not wait_task.done()

    inside_kill = asyncio.Event()
    hold = asyncio.Event()

    async def _park_in_kill() -> None:
        inside_kill.set()
        await hold.wait()

    tmux.on_kill = _park_in_kill

    from cctelegram.callback_dispatcher import trust as trust_cb

    class _Q:
        message = None

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

    cancel_task = asyncio.create_task(
        trust_cb._handle_cancel(_Q(), _Ctx(), _U(), _THREAD, str(flow.generation), tmux)
    )
    await asyncio.wait_for(inside_kill.wait(), timeout=3)
    # The registration lands AFTER Cancel took the claim: WAIT's CAS will lose.
    sessions.registered = True
    await asyncio.sleep(0.1)
    assert not wait_task.done(), "a lost registration CAS must NOT end the WAIT loop"

    # Cancel is cancelled mid-kill; its finally restores the ACQUIRED phase.
    cancel_task.cancel()
    hold.set()
    try:
        await cancel_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await asyncio.sleep(0.05)

    # The abort restored an OPEN phase — and because WAIT never left, that flow
    # is OBSERVED again: the registration it could not act on before is now
    # honoured, which is the strongest possible proof the observer survived.
    assert flow.phase != trust_flow.PHASE_CANCELLING
    await asyncio.wait_for(wait_task, timeout=5)
    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], (
        "the restored flow must still be observed — its registration binds"
    )
    assert flow.phase == trust_flow.PHASE_TERMINAL
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P2: SPARED_BOUND has two dispositions ────────────────────────────────


@pytest.mark.asyncio
async def test_a_current_route_binding_delivers_the_pending_payload(
    tmp_path: Path,
) -> None:
    """CURRENT-ROUTE binding: the bind WON, so the payload goes to NORMAL BOUND
    DELIVERY and its file is not deleted — a completion, not a cancellation.

    Everything is INJECTED: the binding authority is the fake session manager
    (never the live ``session_manager`` singleton), and the replay is a fake, so
    this test cannot reach a real tmux server even if a seam regresses.
    """
    payload = tmp_path / "pending.bin"
    payload.write_bytes(b"data")
    user_data: dict[str, Any] = {}
    entry = _seed(user_data)
    entry["_pending_thread_text"] = "hello"
    from cctelegram.handlers.inbound_telegram import PendingAttachment

    entry["_pending_thread_attachments"] = [
        PendingAttachment(str(payload), "", None, False)
    ]
    bot = _Bot()
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()
    replayed: list[Any] = []

    async def _fake_replay(route: Any, ud: Any) -> Any:
        # Stands in for the real delivery: records the route and CONSUMES the
        # entry the way a successful replay does (files kept — delivered).
        replayed.append((route, dict(ud) if isinstance(ud, dict) else ud))
        from cctelegram.handlers.directory_browser import drop_picker_entry

        drop_picker_entry(ud, _THREAD)
        return None

    # THIS route is bound to the window — through the INJECTED authority only.
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)
    flow = await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry[ENTRY_TOKEN_KEY],
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
        replay=_fake_replay,
    )
    assert flow is not None
    await asyncio.sleep(0.03)

    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    outcome = await trust_flow.cancel_flow(flow, bot, tmux)

    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.kill_calls == [], "a bound window is never killed"
    # Routed through the COMPLETION seam: the queued payload went to normal
    # bound delivery, and a DELIVERED payload's file is never deleted.
    assert replayed and replayed[0][0] == (_USER, _THREAD, _FAKE_WID), replayed
    assert payload.exists(), "a DELIVERED payload's file must never be deleted"
    assert not any("Cancelled" in t for t in bot.texts()), bot.texts()
    assert trust_flow.get_flow(_USER, _THREAD) is None


@pytest.mark.asyncio
async def test_a_collateral_binding_is_an_honest_cancellation(
    tmp_path: Path,
) -> None:
    """COLLATERAL binding: the window belongs to ANOTHER topic. This flow is
    cancelled, the copy names neither topic as bound, and the undelivered file
    is cleaned up. Injected authority only — the real singleton is untouched."""
    payload = tmp_path / "pending2.bin"
    payload.write_bytes(b"data")
    user_data: dict[str, Any] = {}
    entry = _seed(user_data)
    from cctelegram.handlers.inbound_telegram import PendingAttachment

    entry["_pending_thread_attachments"] = [
        PendingAttachment(str(payload), "", None, False)
    ]
    bot = _Bot()
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()

    async def _must_not_replay(route: Any, ud: Any) -> Any:
        raise AssertionError("a collateral cancellation must NOT replay")

    # A DIFFERENT topic owns the window.
    sessions.bind_thread(_USER, _THREAD + 1, _FAKE_WID)
    flow = await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry[ENTRY_TOKEN_KEY],
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
        replay=_must_not_replay,
    )
    assert flow is not None
    await asyncio.sleep(0.03)

    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    outcome = await trust_flow.cancel_flow(flow, bot, tmux)

    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.kill_calls == [], "another topic's window is never killed"
    final = bot.texts()[-1]
    assert "Cancelled" in final, final
    assert "bound to this topic" not in final, final
    assert not payload.exists(), "an UNDELIVERED payload's file is cleaned up"
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None
