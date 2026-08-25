"""GH #65 wave 4 — claim acquisition/release symmetry, at the EDGES.

Wave 3 made every phase transition a CAS; wave 4 makes every ACQUISITION have
exactly one owner, a release that matches ITS phase, a ``finally`` covering it
from the instant of acquisition, and a defined terminal action at every bounded
wait's expiry.

Each test drives the REAL path — a genuinely stuck callback, a real Cancel tap,
a real teardown — rather than releasing a synthetic claim by hand.
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
from tests.cctelegram._adoption_protocol import AdoptionProtocolMixin

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = (_FIXTURES / "folder_trust_arrival_plain_v2.1.241.txt").read_text()
_IDLE = (_FIXTURES / "inputbox_idle_v2.1.207.txt").read_text()
_THREAD = 4242
_USER = 7070
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
    # The OVERALL teardown budget (production 120s) — no test needs it.
    monkeypatch.setattr(trust_flow, "TEARDOWN_BUDGET_S", 1.0)
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
        self.kill_result: bool = True

    async def pane_current_command(self, window_id: str) -> str | None:
        del window_id
        return self.command

    async def capture_pane(self, window_id: str, **kwargs: Any) -> str:
        del window_id, kwargs
        return self.pane

    async def kill_window(self, window_id: str) -> bool:
        self.kill_calls.append(window_id)
        return self.kill_result


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


def _seed(user_data: dict[str, Any], thread_id: int = _THREAD) -> dict[str, Any]:
    entry = ensure_picker_entry(user_data, thread_id)
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


# ── P3: the invariant is structural ────────────────────────────────────────


def test_phase_is_read_only_and_not_a_constructor_argument() -> None:
    """A direct write is a TypeError, and a flow always starts at the initial
    phase — the single-mutator rule is enforced by the type, not by review."""
    assert isinstance(trust_flow.TrustFlow.phase, property)
    assert trust_flow.TrustFlow.phase.fset is None
    import dataclasses

    names = {f.name for f in dataclasses.fields(trust_flow.TrustFlow) if f.init}
    assert "phase" not in names and "_phase" not in names


@pytest.mark.asyncio
async def test_a_direct_phase_assignment_raises() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None
    with pytest.raises(AttributeError):
        flow.phase = trust_flow.PHASE_TERMINAL  # type: ignore[misc]
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-A: Cancel's release matches the phase it claimed ────────────────────


@pytest.mark.asyncio
async def test_a_stale_generation_cancel_never_claims_a_newer_flow() -> None:
    """A stale card's generation is validated INSIDE the acquisition.

    The pre-fix shape claimed first and checked after, then released through a
    ``dispatching``-only CAS that could never match — permanently wedging the
    newer flow against every later claim.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    claim = await trust_flow.claim_for_cancel(
        _USER,
        _THREAD,
        user_data=user_data,
        card_generation=flow.generation - 1,  # a stale scrollback card
    )

    assert claim.ok is False
    assert claim.reason == "stale_generation"
    assert flow.phase == trust_flow.PHASE_AWAITING_TRUST, (
        "a stale card must never claim a newer flow"
    )
    # …and the flow is still fully claimable by everyone else.
    assert await trust_flow.claim_terminal(flow) is True
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_a_spared_bound_cancel_releases_its_cancelling_claim() -> None:
    """The SPARED_BOUND exit must RELEASE — it used to leak the claim.

    Driven through the real Cancel path: the window is already bound, so the
    guarded cleanup spares it, and the flow must not be left in ``cancelling``
    where the registration CAS and the terminalizer both lose forever.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    sessions = _Sessions()
    # The window is bound — through the INJECTED authority only.
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    outcome = await trust_flow.cancel_flow(flow, _Bot(), tmux)

    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.kill_calls == []
    assert flow.phase != trust_flow.PHASE_CANCELLING, (
        "every non-kill Cancel exit must release the claim it made"
    )


@pytest.mark.asyncio
async def test_a_spared_registered_cancel_routes_into_the_completion_seam() -> None:
    """Cancel racing a registration BINDS, through the one completion seam."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    # The registered proof comes from the INJECTED authority — a unit
    # test never seeds the live ``session_manager``.
    sessions.window_states[_FAKE_WID] = SimpleNamespace(
        session_id="sid-raced", cwd="/repo", window_name="repo"
    )
    claim = await trust_flow.claim_for_cancel(
        _USER, _THREAD, user_data=user_data, card_generation=flow.generation
    )
    assert claim.ok
    outcome = await trust_flow.cancel_flow(flow, _Bot(), tmux)
    assert outcome is trust_flow.CleanupOutcome.SPARED_REGISTERED
    assert tmux.kill_calls == []
    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], (
        "a Cancel that loses to a registration must BIND, not report cancelled"
    )
    assert flow.phase != trust_flow.PHASE_CANCELLING


@pytest.mark.asyncio
async def test_a_cancel_that_raises_mid_flight_releases_its_claim() -> None:
    """The ``finally`` covers the claim from the INSTANT of acquisition."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None

    class _Exploding(AdoptionProtocolMixin):
        async def kill_window(self, window_id: str) -> bool:
            raise RuntimeError("tmux exploded")

    from cctelegram.callback_dispatcher import trust as trust_cb

    query = _Query()
    context = _Context(user_data, _Bot())
    await trust_cb._handle_cancel(
        query,
        context,
        _User(_USER),
        _THREAD,
        str(flow.generation),
        _Exploding(),
    )

    # kill_window raising is classified KILL_FAILED by the guard, so the flow
    # settles rather than escaping — but the claim is released either way.
    assert flow.phase != trust_flow.PHASE_CANCELLING


class _Query:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.edits: list[Any] = []
        self.message = None

    async def answer(self, *a: Any, **kw: Any) -> None:
        self.answers.append((a, kw))

    async def edit_message_text(self, *a: Any, **kw: Any) -> None:
        self.edits.append((a, kw))


class _Context:
    def __init__(self, user_data: dict[str, Any], bot: Any) -> None:
        self.user_data = user_data
        self.bot = bot


class _User:
    def __init__(self, uid: int) -> None:
        self.id = uid


# ── P1-B: a stuck dispatch is force-claimed at the bounded wait's expiry ───


@pytest.mark.asyncio
async def test_teardown_force_claims_a_dispatch_that_never_returns() -> None:
    """A claim that never comes back must not own the topic forever.

    Driven through the REAL callback: its transaction hangs, so the claim is
    still held when teardown's bounded wait expires. Teardown must FORCE the
    claim, cancel the WAIT task, run the guarded cleanup and drop the entry —
    never return leaving the topic owned.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    hang = asyncio.Event()

    async def _stuck_callback() -> None:
        claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
        assert claim.ok
        await hang.wait()  # the transaction never returns

    stuck = asyncio.create_task(_stuck_callback())
    await asyncio.sleep(0.05)
    assert flow.phase == trust_flow.PHASE_DISPATCHING

    won = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD, session_mgr=flow.session_mgr),
        timeout=5,
    )

    assert won is False
    assert trust_flow.get_flow(_USER, _THREAD) is None, (
        "teardown must never return with the topic still owned"
    )
    assert picker_entry(user_data, _THREAD) is None
    assert tmux.kill_calls == [_FAKE_WID], "the forced path still settles the window"
    hang.set()
    stuck.cancel()
    try:
        await stuck
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_shutdown_never_abandons_a_stuck_flow() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    hang = asyncio.Event()

    async def _stuck_callback() -> None:
        claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
        assert claim.ok
        await hang.wait()

    stuck = asyncio.create_task(_stuck_callback())
    await asyncio.sleep(0.05)

    await asyncio.wait_for(trust_flow.shutdown(), timeout=5)

    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert flow.wait_task is not None and flow.wait_task.done()
    hang.set()
    stuck.cancel()
    try:
        await stuck
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


# ── P1-C: the entry clear refuses a CLAIMED phase ─────────────────────────


@pytest.mark.asyncio
async def test_the_entry_clear_cannot_steal_a_live_claim() -> None:
    """``clear_topic_entry`` must not yank a flow out of a live claim.

    It CASes only from OPEN ∪ {terminal}; a claimed phase makes it run the FULL
    teardown first (which force-claims at its own expiry), so it converges
    without ever stealing a claim mid-side-effect.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    hang = asyncio.Event()

    async def _stuck_callback() -> None:
        claim = await trust_flow.claim_for_dispatch(_USER, _THREAD, user_data=user_data)
        assert claim.ok
        await hang.wait()

    stuck = asyncio.create_task(_stuck_callback())
    await asyncio.sleep(0.05)
    assert flow.phase == trust_flow.PHASE_DISPATCHING

    cleared = await asyncio.wait_for(
        trust_flow.clear_topic_entry(
            _USER, _THREAD, user_data, session_mgr=flow.session_mgr
        ),
        timeout=6,
    )

    del cleared  # the teardown inside the clear may have dropped it already
    assert picker_entry(user_data, _THREAD) is None
    assert trust_flow.get_flow(_USER, _THREAD) is None
    hang.set()
    stuck.cancel()
    try:
        await stuck
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_a_terminal_marked_flow_exits_its_wait_loop_promptly() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    task = flow.wait_task
    assert task is not None and not task.done()

    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_TERMINAL
    )
    await asyncio.wait_for(task, timeout=2)

    assert task.done(), "the WAIT loop must exit once its flow goes terminal"


@pytest.mark.asyncio
async def test_a_terminal_marked_flow_never_repaints_its_card() -> None:
    """P2-A: neither the renderer nor the refresh may overwrite a final card."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    bot = _Bot()
    tmux = _Tmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=bot, sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)
    assert await trust_flow.transition(
        flow, expect=trust_flow.OPEN_PHASES, to=trust_flow.PHASE_TERMINAL
    )
    before = len(bot.edits)

    await trust_flow.render_trust_card(flow, bot, _TRUST)
    assert await trust_flow.refresh_card_if_live(flow, bot, tmux) is False

    assert len(bot.edits) == before, "a terminal flow must not repaint its card"


# ── P2-B: the terminalizer branches on the tail's ACTUAL outcome ──────────


@pytest.mark.asyncio
async def test_a_completion_tail_that_raises_still_settles_the_window() -> None:
    """A bind that raised before ``bind_thread`` is NOT a success.

    Driven for real: the session manager raises inside the tail, so the topic
    must NOT be dropped with the window unbound — the guarded cleanup runs and
    the card says so.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
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

    assert sessions.binds == [], "the tail never reached bind_thread"
    assert tmux.kill_calls == [_FAKE_WID], (
        "a failed tail must still settle the window, not drop it unbound"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None
    assert any("couldn't finish binding" in t for t in bot.texts()), bot.texts()


# ── P2-C: the baseline survives the flow drop ────────────────────────────


@pytest.mark.asyncio
async def test_the_binding_baseline_outlives_the_flow_it_describes() -> None:
    """The fallback needs the baseline exactly when the flow is already gone."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)  # bound BEFORE install
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    generation = flow.generation
    await trust_flow.teardown_thread(_USER, _THREAD, session_mgr=sessions)
    assert trust_flow.get_flow(_USER, _THREAD) is None

    won = trust_flow._completion_won(
        _USER,
        _THREAD,
        observed_wid=_FAKE_WID,
        generation=generation,
        session_mgr=sessions,
    )

    assert won is False, "a pre-existing binding is never this flow's completion"


@pytest.mark.asyncio
async def test_a_baseline_from_another_generation_is_not_ours() -> None:
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=sessions
    )
    assert flow is not None
    generation = flow.generation
    await trust_flow.teardown_thread(_USER, _THREAD, session_mgr=sessions)
    # Someone binds the window later, unrelated to this flow.
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)

    assert (
        trust_flow._completion_won(
            _USER,
            _THREAD,
            observed_wid=_FAKE_WID,
            generation=generation + 5,
            session_mgr=sessions,
        )
        is False
    )


# ── P2-D: the commit stamp follows the delivery-gate precedent ────────────


@pytest.mark.asyncio
async def test_a_proven_false_enter_is_not_stamped_but_a_raise_is() -> None:
    """A clean False is PROVEN non-delivery; a raise is ambiguous and stamps."""
    from cctelegram.callback_dispatcher import interactive as interactive_cb

    calls: list[str] = []

    from tests.conftest import render_cursor

    class _Pane:
        """A live folder-trust pane whose cursor MOVES, so the wiggle's motion
        proof passes and the transaction actually reaches the Enter."""

        def __init__(self, *, raise_on_enter: bool) -> None:
            self.raise_on_enter = raise_on_enter
            self.window_id = _FAKE_WID
            self.cursor = 1

        async def capture_pane(self, *a: Any, **kw: Any) -> str:
            return render_cursor(_TRUST, self.cursor)

        async def pane_current_command(self, *a: Any, **kw: Any) -> str:
            return "2.1.241"

        async def send_keys(
            self, window_id: str, keys: str, enter: bool = True, literal: bool = True
        ) -> bool:
            if keys == "Enter":
                if self.raise_on_enter:
                    raise RuntimeError("tmux exploded")
                return False  # PROVEN non-delivery
            if keys in ("Down", "Up"):
                self.cursor = 2 if self.cursor == 1 else 1
            return True

    from types import SimpleNamespace

    for raise_on_enter, expected in ((False, []), (True, ["sent"])):
        calls.clear()
        pane = _Pane(raise_on_enter=raise_on_enter)
        w = SimpleNamespace(window_id=_FAKE_WID)
        coro = interactive_cb._dispatch_decision_pane_locked(
            user=SimpleNamespace(id=_USER),
            tmux_manager=pane,
            w=w,
            window_id=_FAKE_WID,
            minted_fingerprint=_trust_fingerprint(),
            option_number=1,
            option_label="Yes, I trust this folder",
            ledger_key=None,
            license_check=lambda family, cmd: True,
            on_commit_sent=lambda: calls.append("sent"),
        )
        if raise_on_enter:
            with pytest.raises(RuntimeError):
                await coro
        else:
            outcome = await coro
            assert outcome.kind == "not_advanced"
        assert calls == expected, (raise_on_enter, calls)


def _trust_fingerprint() -> str:
    from cctelegram.terminal_parser import (
        decision_prompt_fingerprint,
        parse_generic_decision,
    )

    form = parse_generic_decision(_TRUST)
    assert form is not None
    return decision_prompt_fingerprint(form)
