"""Codex round-1 diff-review folds for the GH #65 folder-trust lane.

One test per finding, each written to FAIL on the pre-fold code:

  P1-1  ``start_trust_wait`` claimed ownership with no lock and no entry
        re-validation, so a concurrent ``/start`` / topic close landing in the
        two awaited Telegram calls before it left an UNREACHABLE flow.
  P1-3  the ``awaiting_registration → awaiting_trust`` demotion was unreachable
        on the MANUAL path (it required ``enter_sent_at``), and a single
        INDETERMINATE slice wrongly took the manual-answer transition.
  P2-1  the declared FRESH session-map read (the linearization point) was
        short-circuited by the cached peek.
  P2-3  teardown's reacquisition re-checked identity but not PHASE, so a
        transition to ``completing_bind`` inside the capture→cancel→reacquire
        window was not honored.
  P3-1  the probe's version regex accepted a non-conforming suffix.

The Telegram-seam folds (P1-2, P2-2, P2-4, and the P2-5 matrix) live in
``tests/scenarios/test_trust_card_flow.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cctelegram import terminal_parser, tmux_manager as tmux_mod
from cctelegram.config import config
from cctelegram.handlers import decision_token, trust_flow
from cctelegram.handlers.directory_browser import (
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    STATE_KEY,
    ensure_picker_entry,
    picker_entry,
)
from cctelegram.utils import app_dir

_FIXTURES = Path(__file__).parent / "fixtures"
_THREAD = 77
_USER = 4242


def _fx(name: str) -> str:
    return (_FIXTURES / name).read_text()


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


class _StubTmux:
    """Minimal tmux stand-in for the wait loop (no Telegram, no real tmux)."""

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


class _StubBot:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.edits.append(kwargs)
        return None


class _StubSessionMgr:
    """Only what the WAIT task touches; registration is opt-in per test."""

    def __init__(self) -> None:
        self.registered = False

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        del window_id, timeout
        await asyncio.sleep(interval)
        return self.registered


async def _make_flow(
    user_data: dict[str, Any],
    *,
    tmux: Any,
    bot: Any,
    session_mgr: Any,
    version: str | None = "2.1.241",
) -> trust_flow.TrustFlow | None:
    entry = ensure_picker_entry(user_data, _THREAD)
    assert entry is not None
    entry[CARD_CHAT_ID_KEY] = -100
    entry[CARD_MSG_ID_KEY] = 999
    return await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        created_wid="@5",
        window_name="repo",
        selected_path="/repo",
        create_message="Created window 'repo' at /repo",
        cli_version=version,
        tmux_mgr=tmux,
        session_mgr=session_mgr,
    )


# ── P1-1 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_1_start_refuses_to_install_a_flow_with_no_ownership_token() -> None:
    """A flow may only be installed while the picker ENTRY still exists.

    ``_create_and_bind_window`` awaits two Telegram calls after its last owner
    check, so a concurrent ``/start`` or topic close can clear the entry in that
    window. Installing anyway left an UNREACHABLE flow: every ``tst:`` claim
    requires the entry, so neither Trust nor Cancel could reach it, and a later
    registration bound nothing.
    """
    user_data: dict[str, Any] = {}
    tmux = _StubTmux()
    # No ``ensure_picker_entry`` — this models the entry having been cleared.
    flow = await trust_flow.start_trust_wait(
        bot=_StubBot(),
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        created_wid="@5",
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=_StubSessionMgr(),
    )

    assert flow is None, "an entry-less creation must NOT install a flow"
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert trust_flow.flow_task(_USER, _THREAD) is None


@pytest.mark.asyncio
async def test_p1_1_install_is_atomic_under_the_creation_lock() -> None:
    """The install must happen INSIDE the creation lock.

    Holding the lock must block the install, so a teardown holding it cannot be
    interleaved between the entry re-validation and the registry write.
    """
    user_data: dict[str, Any] = {}
    ensure_picker_entry(user_data, _THREAD)
    lock = trust_flow.creation_lock(_USER, _THREAD)
    await lock.acquire()
    try:
        task = asyncio.create_task(
            _make_flow(
                user_data,
                tmux=_StubTmux(),
                bot=_StubBot(),
                session_mgr=_StubSessionMgr(),
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "start_trust_wait must WAIT on the creation lock"
        assert trust_flow.get_flow(_USER, _THREAD) is None
    finally:
        lock.release()
    flow = await asyncio.wait_for(task, timeout=2)
    assert flow is not None
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-3 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_3_manual_answer_then_persisting_prompt_demotes_and_re_renders() -> (
    None
):
    """The demotion must key on ENTERING ``awaiting_registration``, not on Enter.

    Manual path: the user answers in tmux (the prompt disappears ⇒ the flow
    enters ``awaiting_registration`` with a rebased budget), then the prompt is
    seen AGAIN past the settle margin — proof the answer did not take. That must
    demote back to ``awaiting_trust`` with a fresh render. Pre-fold the demotion
    required ``enter_sent_at``, which the manual path never sets, so the flow
    was stranded in ``awaiting_registration`` with a dead card.
    """
    user_data: dict[str, Any] = {}
    bot = _StubBot()
    tmux = _StubTmux(pane=_fx("inputbox_idle_v2.1.207.txt"))
    session_mgr = _StubSessionMgr()
    flow = await _make_flow(user_data, tmux=tmux, bot=bot, session_mgr=session_mgr)
    assert flow is not None
    # 1) the trust prompt appears
    tmux.pane = _fx("folder_trust_arrival_plain_v2.1.241.txt")
    await asyncio.sleep(0.08)
    assert flow.phase == trust_flow.PHASE_AWAITING_TRUST
    assert flow.trust_seen is True
    # 2) the user answers in tmux — a POSITIVE non-trust classification
    tmux.pane = _fx("inputbox_idle_v2.1.207.txt")
    await asyncio.sleep(0.08)
    assert flow.phase == trust_flow.PHASE_AWAITING_REGISTRATION
    # 3) the prompt is back past the settle margin ⇒ the answer did not take
    trust_flow.TRUST_SETTLE_MARGIN_S  # documented knob
    flow.awaiting_registration_at = trust_flow._wall() - 60.0
    tmux.pane = _fx("folder_trust_arrival_plain_v2.1.241.txt")
    await asyncio.sleep(0.08)

    assert flow.phase == trust_flow.PHASE_AWAITING_TRUST, (
        "a persisting trust frame past the settle margin must DEMOTE"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_p1_3_an_indeterminate_slice_never_takes_the_manual_answer_path() -> None:
    """INDETERMINATE keeps waiting — it is NOT proof the prompt was answered.

    A blank / unreadable capture must leave ``awaiting_trust`` intact; only a
    POSITIVE non-trust classification (a running REPL, another surface) is the
    manual-answer signal.
    """
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("folder_trust_arrival_plain_v2.1.241.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    await asyncio.sleep(0.08)
    assert flow.phase == trust_flow.PHASE_AWAITING_TRUST
    assert flow.trust_seen is True

    tmux.pane = ""  # the blank transitional frame ⇒ INDETERMINATE
    await asyncio.sleep(0.08)

    assert flow.phase == trust_flow.PHASE_AWAITING_TRUST, (
        "an indeterminate slice must not be read as a manual answer"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2-1 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p2_1_the_fresh_session_map_read_is_unconditional() -> None:
    """The FRESH read is the declared linearization point: it must ALWAYS run.

    Pre-fold it sat behind ``peek(...) or read_fresh(...)``, so a positive
    CACHED peek short-circuited it and the linearization point never executed.
    """
    from cctelegram.session import WindowState, session_manager

    calls: list[str | None] = []
    real = trust_flow.read_session_id_for_window_fresh

    def _counting(window_id: str | None) -> str | None:
        calls.append(window_id)
        return real(window_id)

    # A positive CACHED peek — the exact short-circuit condition.
    session_manager.window_states["@9"] = WindowState(
        session_id="sid-cached", cwd="/repo", window_name="repo"
    )
    try:
        trust_flow.read_session_id_for_window_fresh = _counting  # type: ignore[assignment]
        outcome = await trust_flow.cleanup_created_window(
            "@9", "repo", _StubTmux(), reason="t"
        )
    finally:
        trust_flow.read_session_id_for_window_fresh = real  # type: ignore[assignment]
        session_manager.window_states.pop("@9", None)

    assert outcome is trust_flow.CleanupOutcome.SPARED_REGISTERED
    assert calls == ["@9"], "the fresh read must execute even on a positive peek"


# ── P2-3 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p2_3_teardown_honors_a_completing_bind_transition_in_its_window() -> (
    None
):
    """A phase moving to ``completing_bind`` during teardown must be honored.

    Teardown captures the phase under the lock, releases it, cancels + awaits
    the WAIT task, then reacquires. If the flow reached ``completing_bind`` in
    that window the retained inner task must be AWAITED (never abandoned), and
    teardown must report that completion WON so the caller runs the normal
    bound-topic teardown.
    """
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("inputbox_idle_v2.1.207.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    # Freeze the flow in the exact race shape: the WAIT task is gone (as it
    # would be right after teardown's cancel) and an inner completion tail is
    # in flight.
    if flow.wait_task is not None:
        flow.wait_task.cancel()
        try:
            await flow.wait_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    completed = asyncio.Event()

    async def _tail() -> None:
        await asyncio.sleep(0.05)
        completed.set()

    trust_flow._flows[(_USER, _THREAD)] = flow
    flow.phase = trust_flow.PHASE_COMPLETING_BIND
    flow.bind_task = asyncio.create_task(_tail())

    won = await trust_flow.teardown_thread(_USER, _THREAD)

    assert completed.is_set(), "the retained completion tail must be AWAITED"
    assert won is True, "teardown must report that completion won"
    assert trust_flow.get_flow(_USER, _THREAD) is None


@pytest.mark.asyncio
async def test_p2_3_teardown_of_a_waiting_flow_reports_no_completion() -> None:
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("folder_trust_arrival_plain_v2.1.241.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    await asyncio.sleep(0.05)

    won = await trust_flow.teardown_thread(_USER, _THREAD)

    assert won is False
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None


# ── P2-5: the ownership matrix + the terminalizer + lock retention ───────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        trust_flow.PHASE_AWAITING_TRUST,
        trust_flow.PHASE_DISPATCHING,
        trust_flow.PHASE_AWAITING_REGISTRATION,
        trust_flow.PHASE_CANCELLING,
        trust_flow.PHASE_COMPLETING_BIND,
    ],
)
async def test_every_nonterminal_phase_refuses_a_browser_rebuild(phase: str) -> None:
    """EVERY nonterminal state is OWNED — no inbound may rebuild the browser."""
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("folder_trust_arrival_plain_v2.1.241.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    flow.phase = phase

    class _NoBinding:
        def get_window_for_thread(self, *_a: Any) -> str | None:
            return None

    decision = await trust_flow.claim_unbound_inbound(
        _USER, _THREAD, user_data, _NoBinding(), browse_start_path="/home"
    )

    assert decision.kind == "trust_owned", f"{phase} must own the topic"
    entry = picker_entry(user_data, _THREAD)
    assert entry is not None
    assert entry[STATE_KEY] == trust_flow.STATE_AWAITING_TRUST, (
        "the browser must never overwrite an owned creation state"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_terminalizer_runs_the_guarded_cleanup_on_an_exception() -> None:
    """A WAIT task that RAISES must still settle the window and drop the entry."""
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("inputbox_idle_v2.1.207.txt"))
    bot = _StubBot()

    class _Boom(_StubSessionMgr):
        async def wait_for_session_map_entry(
            self, window_id: str, timeout: float = 5.0, interval: float = 0.5
        ) -> bool:
            raise RuntimeError("poll exploded")

    flow = await _make_flow(user_data, tmux=tmux, bot=bot, session_mgr=_Boom())
    assert flow is not None
    task = trust_flow.flow_task(_USER, _THREAD)
    assert task is not None
    await asyncio.wait_for(task, timeout=2)

    assert tmux.kill_calls == ["@5"], "the terminalizer must run the guarded cleanup"
    assert trust_flow.get_flow(_USER, _THREAD) is None
    assert picker_entry(user_data, _THREAD) is None, "the entry is dropped LAST"
    assert bot.edits, "the card must be edited to an honest failure state"
    assert not trust_flow.creation_lock(_USER, _THREAD).locked()


@pytest.mark.asyncio
async def test_the_creation_lock_is_retained_across_a_completed_flow() -> None:
    """Locks are RETAINED per (user, thread) — evicting one with a live waiter
    is the bug — and generation validation runs AFTER acquisition."""
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("folder_trust_arrival_plain_v2.1.241.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    first_lock = trust_flow.creation_lock(_USER, _THREAD)
    first_gen = flow.generation

    await trust_flow.teardown_thread(_USER, _THREAD)

    assert trust_flow.creation_lock(_USER, _THREAD) is first_lock
    # A second flow on the same topic gets a STRICTLY GREATER generation, so a
    # post-acquisition generation check can always tell them apart.
    ensure_picker_entry(user_data, _THREAD)
    second = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert second is not None
    assert second.generation > first_gen
    assert trust_flow.creation_lock(_USER, _THREAD) is first_lock
    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_dispatch_rebase_fires_far_past_the_hook_timeout() -> None:
    """P1-4: a Trust dispatch confirmed at t ≫ hook_timeout restarts the budget.

    Human wait time must never consume the machine's registration budget, so an
    already-EXPIRED registration deadline must be pushed into the future rather
    than firing an immediate cleanup.
    """
    user_data: dict[str, Any] = {}
    tmux = _StubTmux(pane=_fx("folder_trust_arrival_plain_v2.1.241.txt"))
    flow = await _make_flow(
        user_data, tmux=tmux, bot=_StubBot(), session_mgr=_StubSessionMgr()
    )
    assert flow is not None
    # Simulate a long human wait: the registration budget is long gone.
    flow.registration_deadline = trust_flow._wall() - 3600.0
    flow.trust_seen = True

    trust_flow.note_dispatch_enter_sent(flow)

    assert flow.registration_deadline > trust_flow._wall()
    assert flow.trust_deadline is None
    assert flow.awaiting_registration_at is not None
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2-5: the production-default (lane ON) posture ──────────────────────────


def test_production_defaults_arm_the_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO env overrides the lane is ON — the shipped posture, not dark.

    The suite floor pins ``CC_TELEGRAM_TRUST_PROMPT_CEILING_S=0`` so pre-#65
    scenarios keep their shape, which would otherwise leave the production
    default untested.
    """
    from cctelegram.config import Config

    for name in (
        "CC_TELEGRAM_TRUST_PROMPT_CEILING_S",
        "CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S",
        "CC_TELEGRAM_TRUST_CARD_DISPATCH",
        "CC_TELEGRAM_DECISION_DISPATCH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_USERS", "1")

    fresh = Config()

    assert fresh.trust_prompt_ceiling_s == pytest.approx(900.0)
    assert fresh.hook_timeout_extension_s == pytest.approx(15.0)
    assert fresh.trust_card_dispatch_enabled is True
    assert fresh.decision_dispatch_force_disabled is False
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", fresh.trust_prompt_ceiling_s)
    assert trust_flow.lane_enabled() is True


# ── P3-1 ─────────────────────────────────────────────────────────────────────


def test_p3_1_probe_regex_requires_the_exact_claude_code_shape() -> None:
    """Exact conformance to ``N.N.N (Claude Code)`` — no trailing suffix."""
    assert (
        tmux_mod.parse_probe_version("A\n2.1.241 (Claude Code)\nB\n", "A", "B")
        == "2.1.241"
    )
    for bad in (
        "A\n2.1.241-beta (Claude Code)\nB\n",
        "A\n2.1.241x (Claude Code)\nB\n",
        "A\n2.1 (Claude Code)\nB\n",
        "A\nv2.1.241 (Claude Code)\nB\n",
        "A\n2.1.241 (Claude Code) extra\nB\n",
    ):
        assert tmux_mod.parse_probe_version(bad, "A", "B") is None, bad


def test_p3_2_the_2_1_239_probe_fixture_is_consumed_too() -> None:
    """Both rig probe captures parse (the addendum names both versions)."""
    pane = _fx("version_probe_plain_v2.1.239.txt")
    nonce_a, nonce_b = _probe_nonces(pane)
    assert tmux_mod.parse_probe_version(pane, nonce_a, nonce_b) == "2.1.239"
    # …and the echoed command line, which contains BOTH nonces, is ignored.
    echoed = [
        line
        for line in pane.splitlines()
        if nonce_a in line and line.strip() != nonce_a
    ]
    assert echoed, "the rig capture must contain the echoed probe command"


def _probe_nonces(pane: str) -> tuple[str, str]:
    """Recover the nonces the rig used, from the capture itself."""
    a = next(
        line.strip()
        for line in pane.splitlines()
        if line.strip().startswith("CCTGVERA")
    )
    b = next(
        line.strip()
        for line in pane.splitlines()
        if line.strip().startswith("CCTGVERB")
    )
    return a, b


def test_session_map_helper_is_not_left_behind() -> None:
    """Guard: the fixture-local session_map writes never leak to a neighbour."""
    path = app_dir() / "session_map.json"
    if path.exists():
        assert json.loads(path.read_text()) == {}
