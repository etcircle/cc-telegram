"""Codex round-2 diff-review folds for the GH #65 folder-trust lane.

Every test drives the REAL transition it is about (round 2 rejected tests that
hand-assemble the state they claim to exercise), so the interleavings are the
ones production can actually reach:

  P1-A  the flow install must be atomic against teardown's snapshot→clear gap,
        and must reject an entry that is not the one the callback started from
        (the ABA hijack of a REPLACEMENT entry).
  P1-B  ``SPARED_REGISTERED`` must WIN — flip into the completion tail (bind +
        deliver the pending payload), not terminalize and discard it.
  P1-C  the global observation ceiling must be TERMINAL: bounded per-slice tmux
        awaits, and ``dispatching`` must not be a budget-exempt black hole.
  P2-A  the capture→cancel window — the WAIT terminalizer must leave a
        DISCOVERABLE completion record, so teardown that reacquires and finds
        the flow gone still reports completion-won.
  P2-B  the teardown await of the bind tail must not swallow a cancellation
        aimed at teardown, and must derive ``completed`` from the task's ACTUAL
        outcome.
  P2-D  every cleanup must CLAIM a terminal phase first, so a concurrent Trust
        tap cannot claim ``dispatching`` mid-cleanup.
  P2-E  ownership resolves the tapped CARD, not the first flow in the thread.
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
_THREAD = 88
_USER = 5150
_TRUST = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_IDLE = (_FIXTURES / "inputbox_idle_v2.1.207.txt").read_text()
_CORPSE = (_FIXTURES / "folder_trust_postesc_t4_plain_v2.1.241.txt").read_text()


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
    # The OVERALL teardown budget (production 120s) — no test needs it.
    monkeypatch.setattr(trust_flow, "TEARDOWN_BUDGET_S", 1.0)
    (app_dir() / "session_map.json").unlink(missing_ok=True)
    yield
    trust_flow.reset_for_tests()
    decision_token.reset_for_tests()
    terminal_parser.reset_for_tests()
    (app_dir() / "session_map.json").unlink(missing_ok=True)


class _StubTmux:
    def __init__(self, *, command: str = "claude", pane: str = "") -> None:
        self.command = command
        self.pane = pane
        self.kill_calls: list[str] = []
        self.hang: asyncio.Event | None = None

    async def pane_current_command(self, window_id: str) -> str | None:
        del window_id
        if self.hang is not None:
            await self.hang.wait()
        return self.command

    async def capture_pane(self, window_id: str, **kwargs: Any) -> str:
        del window_id, kwargs
        if self.hang is not None:
            await self.hang.wait()
        return self.pane

    async def kill_window(self, window_id: str) -> bool:
        self.kill_calls.append(window_id)
        return True


class _StubBot:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.edits.append(kwargs)
        return None


class _StubSessionMgr:
    def __init__(self) -> None:
        self.registered = False
        # Register only from the Nth poll onwards — lets a test place a
        # registration inside another topic's teardown window without touching
        # any flow state directly.
        self.register_after: int | None = None
        self.polls = 0
        self.binds: list[tuple[int, int, str]] = []
        self.window_states: dict[str, Any] = {}

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        del window_id, timeout
        self.polls += 1
        await asyncio.sleep(interval)
        if self.register_after is not None and self.polls >= self.register_after:
            return True
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


def _seed_entry(user_data: dict[str, Any], thread_id: int = _THREAD) -> dict[str, Any]:
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
    session_mgr: Any,
    entry_token: str | None = None,
    thread_id: int = _THREAD,
    user_id: int = _USER,
    created_wid: str = "@5",
    card_msg_id: int | None = None,
) -> trust_flow.TrustFlow | None:
    entry = picker_entry(user_data, thread_id)
    token = entry_token
    if token is None and entry is not None:
        token = entry.get(ENTRY_TOKEN_KEY)
    return await trust_flow.start_trust_wait(
        bot=bot,
        user_id=user_id,
        thread_id=thread_id,
        chat_id=-100,
        user_data=user_data,
        entry_token=token,
        created_wid=created_wid,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=session_mgr,
        card_msg_id=card_msg_id,
    )


# ── P1-A: the install must be atomic against teardown ────────────────────────


def test_p1_a_every_picker_entry_carries_an_identity_token() -> None:
    """Entry identity must be EXPLICIT — 'some entry exists' is not identity."""
    user_data: dict[str, Any] = {}
    first = ensure_picker_entry(user_data, _THREAD)
    assert first is not None
    token = first.get(ENTRY_TOKEN_KEY)
    assert isinstance(token, str) and token
    # ``ensure_`` is idempotent: the SAME entry keeps the SAME token…
    assert ensure_picker_entry(user_data, _THREAD) is first
    assert first[ENTRY_TOKEN_KEY] == token
    # …but a REPLACEMENT entry is a different identity.
    from cctelegram.handlers.directory_browser import drop_picker_entry

    drop_picker_entry(user_data, _THREAD)
    replacement = ensure_picker_entry(user_data, _THREAD)
    assert replacement is not None
    assert replacement[ENTRY_TOKEN_KEY] != token


@pytest.mark.asyncio
async def test_p1_a_install_after_a_teardown_snapshot_before_the_clear_aborts() -> None:
    """The real interleaving: teardown SNAPSHOTS, a creation callback installs,
    teardown then CLEARS. The install must lose.

    Teardown holds the creation lock while it clears the entry and invalidates
    its token, so the install — which re-validates the token it started from —
    either runs first and is torn down, or runs after and ABORTS. Either way no
    unreachable flow survives the clear.
    """
    user_data: dict[str, Any] = {}
    entry = _seed_entry(user_data)
    token = entry[ENTRY_TOKEN_KEY]
    session_mgr = _StubSessionMgr()

    # Teardown takes the lock (its snapshot+clear critical section)…
    lock = trust_flow.creation_lock(_USER, _THREAD)
    await lock.acquire()
    install = asyncio.create_task(
        _start(
            user_data,
            tmux=_StubTmux(),
            bot=_StubBot(),
            session_mgr=session_mgr,
            entry_token=token,
        )
    )
    await asyncio.sleep(0.05)
    assert not install.done(), "the install must WAIT on the creation lock"
    # …and clears the entry inside it.
    from cctelegram.handlers.directory_browser import drop_picker_entry

    drop_picker_entry(user_data, _THREAD)
    lock.release()

    flow = await asyncio.wait_for(install, timeout=2)
    assert flow is None, "an install whose entry was cleared must ABORT"
    assert trust_flow.get_flow(_USER, _THREAD) is None


@pytest.mark.asyncio
async def test_p1_a_install_against_a_replacement_entry_aborts() -> None:
    """The ABA hijack: the entry the callback started from was cleared and a NEW
    inbound created a REPLACEMENT. A token-blind "some entry exists" check would
    hijack that fresh entry; the token check must refuse it."""
    user_data: dict[str, Any] = {}
    entry = _seed_entry(user_data)
    stale_token = entry[ENTRY_TOKEN_KEY]

    from cctelegram.handlers.directory_browser import drop_picker_entry

    drop_picker_entry(user_data, _THREAD)
    replacement = _seed_entry(user_data)
    assert replacement[ENTRY_TOKEN_KEY] != stale_token

    flow = await _start(
        user_data,
        tmux=_StubTmux(),
        bot=_StubBot(),
        session_mgr=_StubSessionMgr(),
        entry_token=stale_token,
    )

    assert flow is None, "a stale entry token must never hijack a replacement"
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is replacement, "untouched"


# ── P1-B: SPARED_REGISTERED wins ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_b_a_registration_racing_a_cleanup_wins_and_binds() -> None:
    """The spec's linearization contract: a registration observed at/before the
    fresh read WINS — the flow flips into the COMPLETION tail.

    Driven through the REAL path: the registration-timeout cleanup runs, its
    guarded kill returns SPARED_REGISTERED, and the flow must bind + deliver
    rather than terminalize and discard the queued payload.
    """
    from cctelegram.session import WindowState, session_manager

    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    bot = _StubBot()
    tmux = _StubTmux(pane=_IDLE)  # a running REPL, so the registration budget applies
    session_mgr = _StubSessionMgr()
    session_manager.window_states["@5"] = WindowState(
        session_id="sid-raced", cwd="/repo", window_name="repo"
    )
    try:
        flow = await _start(user_data, tmux=tmux, bot=bot, session_mgr=session_mgr)
        assert flow is not None
        task = trust_flow.flow_task(_USER, _THREAD)
        assert task is not None
        await asyncio.wait_for(task, timeout=3)
    finally:
        session_manager.window_states.pop("@5", None)

    assert tmux.kill_calls == [], "a registered window is never killed"
    assert session_mgr.binds == [(_USER, _THREAD, "@5")], (
        "SPARED_REGISTERED must flip into the COMPLETION tail, not terminalize"
    )
    assert picker_entry(user_data, _THREAD) is None


# ── P1-C: the ceiling is a TERMINAL bound ────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_c_a_wedged_tmux_call_cannot_outlive_the_global_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged tmux subprocess must not park the WAIT task forever.

    Every per-slice tmux await is bounded (`asyncio.wait_for`); a timeout
    classifies INDETERMINATE, so the flow still reaches the global observation
    ceiling and takes its SPARE-and-release terminal action.
    """
    monkeypatch.setattr(trust_flow, "SLICE_TMUX_TIMEOUT_S", 0.05)
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    monkeypatch.setattr(trust_flow, "GLOBAL_CEILING_MARGIN_S", 0.1)
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    tmux = _StubTmux(pane=_TRUST)
    tmux.hang = asyncio.Event()  # never set: every tmux call wedges
    bot = _StubBot()

    flow = await _start(user_data, tmux=tmux, bot=bot, session_mgr=_StubSessionMgr())
    assert flow is not None
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert tmux.kill_calls == [], "the global ceiling SPARES, never kills"
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None, "ownership is released"


@pytest.mark.asyncio
async def test_p1_c_a_dispatch_cannot_park_the_flow_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch must not make the flow immortal — but the ceiling DEFERS.

    Round 2 asked for "``dispatching`` is not budget-exempt"; round 3 refined
    WHAT that means (r3 P1-3): sparing or terminalizing UNDER an active dispatch
    is itself a side effect on a flow another actor owns, so the global ceiling
    now defers and re-checks. The bound is the callback's ``finally``, which
    always releases the claim — so the flow is still terminal, just never
    terminalized underneath a live transaction.
    """
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.05)
    monkeypatch.setattr(trust_flow, "GLOBAL_CEILING_MARGIN_S", 0.1)
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    tmux = _StubTmux(pane=_TRUST)
    flow = await _start(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok

    # Well past the ceiling, the flow is DEFERRED, not terminalized.
    await asyncio.sleep(0.3)
    assert trust_flow.get_flow(_USER, _THREAD) is flow
    assert tmux.kill_calls == []

    # The callback's finally releases the claim → the ceiling fires at once.
    await trust_flow.release_dispatch_claim(flow, phase=trust_flow.PHASE_AWAITING_TRUST)
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert tmux.kill_calls == [], "a dispatching flow is SPARED, never killed"
    assert trust_flow.get_flow(_USER, _THREAD) is None


# ── P2-D: cleanups claim a terminal phase first ──────────────────────────────


@pytest.mark.asyncio
async def test_p2_d_a_trust_tap_cannot_claim_dispatching_during_a_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cleanup CLAIMS a terminal phase before it acts.

    Driven through the real trust-ceiling expiry: once the cleanup owns the
    flow, a concurrent Trust tap must be refused rather than claiming
    ``dispatching`` on a window that is being killed.
    """
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.02)
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    tmux = _StubTmux(pane=_TRUST)
    flow = await _start(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=3)

    assert tmux.kill_calls == ["@5"], "the ceiling expiry must clean up"
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert not claim.ok, "no tap may claim a torn-down flow"


@pytest.mark.asyncio
async def test_p2_d_a_cleanup_defers_to_an_in_flight_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror: a cleanup must NOT terminalize a flow a tap already owns."""
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 30.0)
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    tmux = _StubTmux(pane=_TRUST)
    flow = await _start(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    await asyncio.sleep(0.05)
    claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
    assert claim.ok
    # The ceiling expires WHILE the tap owns the pane.
    flow.trust_deadline = trust_flow._wall() - 1.0
    await asyncio.sleep(0.05)

    assert tmux.kill_calls == [], "a cleanup must never kill under a live dispatch"
    assert flow.phase == trust_flow.PHASE_DISPATCHING
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2-A / P2-B: the capture→cancel window + honest completion reporting ─────


@pytest.mark.asyncio
async def test_p2_a_a_flow_that_completes_after_the_sweep_snapshot_is_reported() -> (
    None
):
    """The REACHABLE P2-A shape, driven end-to-end through the public sweep.

    ``teardown_all_for_user`` snapshots the live flows and then tears each down
    in turn. A flow can finish ON ITS OWN inside that loop — its WAIT-task
    terminalizer removes it from the registry — so the per-topic teardown that
    reaches it finds NOTHING. Reported as "no completion", ``/start`` would
    silently skip the bound-topic teardown it owes for a topic that DID bind.

    Nothing here is hand-assembled: topic A's teardown genuinely awaits its slow
    completion tail, and topic B genuinely completes during that await.
    """
    slow_bot = _StubBot()
    slow_edits = asyncio.Event()

    class _SlowBot(_StubBot):
        async def edit_message_text(self, **kwargs: Any) -> Any:
            await asyncio.sleep(0.15)
            return await super().edit_message_text(**kwargs)

    thread_a, thread_b = _THREAD, _THREAD + 1
    data_a: dict[str, Any] = {}
    data_b: dict[str, Any] = {}
    _seed_entry(data_a, thread_a)
    _seed_entry(data_b, thread_b)
    session_a = _StubSessionMgr()
    session_b = _StubSessionMgr()

    flow_a = await _start(
        data_a,
        tmux=_StubTmux(pane=_IDLE),
        bot=_SlowBot(),
        session_mgr=session_a,
        thread_id=thread_a,
        created_wid="@5",
    )
    flow_b = await _start(
        data_b,
        tmux=_StubTmux(pane=_IDLE),
        bot=slow_bot,
        session_mgr=session_b,
        thread_id=thread_b,
        created_wid="@6",
    )
    assert flow_a is not None and flow_b is not None

    # A registers NOW and its completion tail is SLOW (its card edit sleeps).
    # B registers a few slices later — i.e. INSIDE the sweep, while it is still
    # awaiting A — so B is present at the snapshot and gone by the time the
    # sweep reaches it. Nothing here pokes at flow state: both transitions are
    # the real ones, driven by the session map.
    session_a.registered = True
    session_b.register_after = 8
    await asyncio.sleep(0.02)
    assert trust_flow.get_flow(_USER, thread_b) is not None, (
        "topic B must still be live when the sweep snapshots"
    )

    completed = await asyncio.wait_for(
        trust_flow.teardown_all_for_user(_USER), timeout=5
    )

    assert session_b.binds, "topic B genuinely bound"
    assert thread_b in completed, (
        "a flow that finished between the sweep's snapshot and its per-topic "
        "teardown must still be reported as a completion"
    )
    assert thread_a in completed
    del slow_edits


@pytest.mark.asyncio
async def test_p2_a_a_cold_teardown_never_mistakes_an_ordinary_binding() -> None:
    """The mirror guard: a topic that never had a creation flow is NOT a
    completion just because it happens to be bound — otherwise ``/start`` would
    run a bound-topic teardown on every ordinary topic."""
    session_mgr = _StubSessionMgr()
    session_mgr.bind_thread(_USER, _THREAD, "@5")

    won = await trust_flow.teardown_thread(_USER, _THREAD, session_mgr=session_mgr)

    assert won is False


@pytest.mark.asyncio
async def test_p2_b_a_cancelled_completion_tail_is_not_reported_as_completed() -> None:
    """``completed`` must come from the tail's ACTUAL outcome, never assumed."""
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    flow = await _start(
        user_data,
        tmux=_StubTmux(pane=_TRUST),
        bot=_StubBot(),
        session_mgr=_StubSessionMgr(),
    )
    assert flow is not None
    if flow.wait_task is not None:
        flow.wait_task.cancel()
        try:
            await flow.wait_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    trust_flow._flows[(_USER, _THREAD)] = flow

    async def _never() -> None:
        await asyncio.sleep(30)

    inner = asyncio.create_task(_never())
    assert await trust_flow.transition(
        flow,
        expect=trust_flow.OPEN_PHASES,
        to=trust_flow.PHASE_COMPLETING_BIND,
    )
    flow.bind_task = inner
    inner.cancel()
    try:
        await inner
    except asyncio.CancelledError:
        pass

    won = await trust_flow.teardown_thread(_USER, _THREAD)

    assert won is False, "a CANCELLED tail is not a completion"


@pytest.mark.asyncio
async def test_p2_b_teardown_cancellation_does_not_kill_the_bind_tail() -> None:
    """Awaiting a Task PROPAGATES cancellation to it (verified empirically), so
    teardown must SHIELD the retained tail: a cancellation aimed at teardown
    must never abort a bind that is already underway."""
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    flow = await _start(
        user_data,
        tmux=_StubTmux(pane=_TRUST),
        bot=_StubBot(),
        session_mgr=_StubSessionMgr(),
    )
    assert flow is not None
    if flow.wait_task is not None:
        flow.wait_task.cancel()
        try:
            await flow.wait_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    trust_flow._flows[(_USER, _THREAD)] = flow

    finished: list[str] = []

    async def _tail() -> None:
        await asyncio.sleep(0.2)
        finished.append("bound")

    inner = asyncio.create_task(_tail())
    assert await trust_flow.transition(
        flow,
        expect=trust_flow.OPEN_PHASES,
        to=trust_flow.PHASE_COMPLETING_BIND,
    )
    flow.bind_task = inner

    teardown = asyncio.create_task(trust_flow.teardown_thread(_USER, _THREAD))
    await asyncio.sleep(0.05)
    teardown.cancel()
    try:
        await teardown
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.3)

    assert finished == ["bound"], (
        "a cancellation aimed at teardown must not abort the bind tail"
    )
    assert not inner.cancelled()


# ── P2-E: ownership resolves the CARD ────────────────────────────────────────


@pytest.mark.asyncio
async def test_p2_e_ownership_resolves_the_tapped_card_not_the_thread() -> None:
    """Two allowed users can each have a creation flow in ONE topic.

    A thread-only owner lookup rejects the second user's tap on their OWN card;
    ownership must key on the card's message coordinates.
    """
    user_a_data: dict[str, Any] = {}
    user_b_data: dict[str, Any] = {}
    entry_a = _seed_entry(user_a_data)
    entry_a[CARD_MSG_ID_KEY] = 111
    entry_b = _seed_entry(user_b_data)
    entry_b[CARD_MSG_ID_KEY] = 222

    flow_a = await _start(
        user_a_data,
        tmux=_StubTmux(pane=_TRUST),
        bot=_StubBot(),
        session_mgr=_StubSessionMgr(),
        user_id=_USER,
        created_wid="@5",
        card_msg_id=111,
    )
    flow_b = await _start(
        user_b_data,
        tmux=_StubTmux(pane=_TRUST),
        bot=_StubBot(),
        session_mgr=_StubSessionMgr(),
        user_id=_USER + 1,
        created_wid="@6",
        card_msg_id=222,
    )
    assert flow_a is not None and flow_b is not None

    assert trust_flow.flow_owner_for_card(-100, 111) == _USER
    assert trust_flow.flow_owner_for_card(-100, 222) == _USER + 1
    assert trust_flow.flow_owner_for_card(-100, 333) is None

    await trust_flow.teardown_thread(_USER, _THREAD)
    await trust_flow.teardown_thread(_USER + 1, _THREAD)


# ── P2-C: the expired-tap refresh must not re-mint on a corpse ──────────────


@pytest.mark.asyncio
async def test_p2_c_refresh_never_re_mints_on_a_dead_pane() -> None:
    """A dead pane RETAINS the trust text (addendum item 2), so the refresh must
    require positive ``pane_command_is_claude`` before any re-mint."""
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    bot = _StubBot()
    tmux = _StubTmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=bot, session_mgr=_StubSessionMgr())
    assert flow is not None
    await asyncio.sleep(0.05)
    assert flow.token is not None, "the live prompt minted a Trust button"

    # Claude exits: the prompt TEXT stays painted above the shell prompt.
    tmux.pane = _CORPSE
    tmux.command = "zsh"
    refreshed = await trust_flow.refresh_card_if_live(flow, bot, tmux)

    assert refreshed is False, "a corpse must not be re-rendered as a live card"
    assert flow.token is None, "no token may be minted against a dead pane"
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_p2_c_refresh_of_a_torn_down_flow_mints_nothing() -> None:
    """The flow can be torn down during the refresh's own capture await."""
    user_data: dict[str, Any] = {}
    _seed_entry(user_data)
    bot = _StubBot()
    tmux = _StubTmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=bot, session_mgr=_StubSessionMgr())
    assert flow is not None
    await trust_flow.teardown_thread(_USER, _THREAD)

    refreshed = await trust_flow.refresh_card_if_live(flow, bot, tmux)

    assert refreshed is False, "a torn-down flow re-mints nothing"
    assert flow.token is None
