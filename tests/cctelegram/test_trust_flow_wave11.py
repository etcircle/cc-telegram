"""GH #65 wave 11 — the adoption seams' CONSUMPTION of the kill-pending state.

Round 10 built the registry; round 11 found that the seams reading it did so
unsoundly:

  * P1-A ``create_window`` checked the gate AFTER creating, so a kill that
    landed on the reused id and cleared its counter on the same event-loop turn
    left the branch skipped and SUCCESS reported for a dead window. The id
    cannot be checked before the fact either — tmux assigns it — so the gate has
    to be the GLOBAL one, and success has to be PROVEN by an existence probe.
  * P1-B the other two seams never re-validated after the settlement wait: the
    trust bind ignored the wait's boolean entirely, and the directory bind
    validated existence/exclusivity/ownership BEFORE a wait that can last
    seconds and then bound the stale window object.

Every flow here is driven through the public seams with REAL tasks.
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
_THREAD = 11011
_USER = 5656
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


# ── P1-A: create_window's gate is LINEARIZABLE ────────────────────────────


@pytest.mark.asyncio
async def test_create_window_refuses_while_a_kill_is_still_in_flight() -> None:
    """The gate runs BEFORE ``tmux new-window``, and its False is a REFUSAL.

    Checking after creation was not linearizable: the kill's done-callback can
    clear the counter on the SAME event-loop turn on which it killed the reused
    id, so the post-hoc branch saw a clean counter and reported success for a
    dead window. And the id cannot be checked individually before the fact —
    tmux assigns it — so the only sound question is the global one.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux._kill_pending_windows["@0"] = 1
    try:
        assert real_tmux.any_kill_pending() is True
        # Bounded so the test does not sit on the production 10s default.
        settled = await real_tmux.await_all_kills_settled(timeout=0.1)
        assert settled is False, "an unsettled kill must REFUSE, not proceed"

        ok, msg, name, wid = await real_tmux.create_window(
            "/tmp", window_name="w11", start_claude=False
        )
        assert ok is False, "create must not proceed past an unsettled kill"
        assert (wid, name) == ("", ""), "and must not report a window id"
        assert "still being closed" in msg, msg
    finally:
        real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_create_window_proves_the_window_exists_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success is PROVEN by an existence probe, never inferred from a counter.

    This is the same-tick shape: the kill lands on the reused id and its
    done-callback clears the counter before we look, so a counter-based
    inference reports a live window. The probe is what refuses.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()

    real_to_thread = asyncio.to_thread

    async def _to_thread(func: Any, *a: Any, **kw: Any) -> Any:
        # Intercept ONLY the creation closure; every other to_thread call in
        # this path (the window list, the probes) must stay real.
        if getattr(func, "__name__", "") == "_create_and_start":
            return True, "created", "w11", "@0"
        return await real_to_thread(func, *a, **kw)

    from cctelegram.tmux_manager import TmuxWindow

    async def _listing_without_our_window() -> Any:
        # A listing that WORKED (it returned other windows) and does not
        # contain ours — the only shape that PROVES absence. An empty or failed
        # listing is indeterminate and must not be read as a dead window.
        return [
            TmuxWindow(window_id="@41", window_name="someone-else", cwd="/tmp"),
            TmuxWindow(window_id="@42", window_name="another", cwd="/tmp"),
        ]

    monkeypatch.setattr(real_tmux, "list_windows", _listing_without_our_window)
    monkeypatch.setattr(real_tmux, "list_windows_fresh", _listing_without_our_window)
    monkeypatch.setattr(asyncio, "to_thread", _to_thread)

    ok, msg, name, wid = await real_tmux.create_window(
        "/tmp", window_name="w11", start_claude=False
    )
    assert ok is False, (
        "a window the tmux server does not have must NEVER be reported as created"
    )
    assert (wid, name) == ("", "")
    assert "removed before it could be used" in msg, msg


# ── P1-B: every precondition is re-validated AFTER the wait ───────────────


@pytest.mark.asyncio
async def test_the_trust_bind_refuses_when_settlement_times_out() -> None:
    """A settlement wait that TIMES OUT means the kill can still land.

    That is precisely when binding is least safe, and the old code ignored the
    boolean entirely. The refusal lands BEFORE ``bind_thread``, so it is a clean
    nothing-happened and the existing failure arm owns the copy.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)

    reached = asyncio.Event()

    async def _never_settles(window_id: str, **kwargs: Any) -> bool:
        del window_id, kwargs
        reached.set()
        return False

    tmux.await_kill_settled = _never_settles  # type: ignore[attr-defined]
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    # The gate must actually be REACHED, or this asserts nothing.
    await asyncio.wait_for(reached.wait(), timeout=5)

    completed = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD), timeout=10
    )

    assert sessions.binds == [], "a window that may be about to die is NOT bound"
    assert completed is False, "and nothing is reported as a completion"
    assert trust_flow.bind_committed(flow) is False, "no note was written"


@pytest.mark.asyncio
async def test_the_trust_bind_refuses_a_window_that_died_during_the_wait() -> None:
    """Existence is re-proven with a FRESH probe after the wait."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)

    probed = asyncio.Event()

    async def _listing_without_it() -> Any:
        # A listing that WORKED and does not contain our window — the only shape
        # that PROVES absence (an empty listing proves nothing).
        probed.set()
        return [SimpleNamespace(window_id="@fake-someone-else")]

    tmux.list_windows_fresh = _listing_without_it  # type: ignore[attr-defined]
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(probed.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert sessions.binds == [], "a corpse must never be bound"


@pytest.mark.asyncio
async def test_the_trust_bind_refuses_when_another_topic_took_the_thread() -> None:
    """Exclusivity is re-read after the wait: 1 topic = 1 window = 1 session."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)

    raced = asyncio.Event()

    async def _slow_settle(window_id: str, **kwargs: Any) -> bool:
        del window_id, kwargs
        # A competing bind lands DURING the wait.
        sessions.binds.append((_USER, _THREAD, "@fake-other-window"))
        raced.set()
        return True

    tmux.await_kill_settled = _slow_settle  # type: ignore[attr-defined]
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(raced.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert (_USER, _THREAD, _FAKE_WID) not in sessions.binds, (
        "the trust bind must refuse a thread another topic already claimed"
    )


# ── P2: the settle copy has THREE arms ────────────────────────────────────


@pytest.mark.asyncio
async def test_settle_copy_for_a_topic_rebound_to_a_different_window() -> None:
    """REBOUND is not UNBOUND.

    ``binding_is_current_route`` False conflated the two, so a topic that is
    perfectly usable — just bound to a different session — was told there is
    "nothing to send to here". That is false and the opposite of actionable.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    bot = _Bot()
    sessions = _Sessions()
    flow = await _start(user_data, tmux=_Tmux(pane=_IDLE), bot=bot, sessions=sessions)
    assert flow is not None
    sessions.bind_thread(_USER, _THREAD, "@fake-some-other-window")

    await trust_flow._settle_committed_but_unfinished_bind(flow, bot)

    last = bot.texts()[-1]
    assert "nothing to send to here" not in last, (
        f"a REBOUND topic is usable — this copy is false: {last!r}"
    )
    assert "bound to another session" in last, last
    assert "may not have been delivered" in last, (
        "the payload uncertainty must still be disclosed"
    )
