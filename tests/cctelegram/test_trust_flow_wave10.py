"""GH #65 wave 10 — pinned evidence, the adoption gate, honest final copy.

Round 10's P1s were both "the fix held, but only inside a window":

  * P1-A the completion note's 300s TTL kept ageing while a live flow's replay
    could lawfully wait far longer, so a teardown starting after expiry found
    `bind_committed()` False again and re-created the round-9 misclassification.
    Evidence for a LIVE matching generation must not expire.
  * P1-B cancelling the async wrapper cannot stop a libtmux kill already running
    in `to_thread`, so the killer's side can never close the TOCTOU. It is
    closed on the ADOPTION side instead: a window id with a kill in flight
    cannot be adopted, so a straggler can only kill a window nobody owns.

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
_THREAD = 10101
_USER = 4545
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


# ── P1-A: evidence for a LIVE generation must not expire ──────────────────


@pytest.mark.asyncio
async def test_a_committed_bind_is_still_evidence_past_the_note_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live flow's replay may outlast the TTL — its evidence must not.

    The note is the only durable proof a synchronous ``bind_thread`` committed,
    and the tail that wrote it can lawfully sit at the payload replay far longer
    than 300s (it waits on Telegram, and on the pane). If the note aged out
    underneath the still-live flow, ``bind_committed`` went False at all three
    outcome sites and round 9's misclassification came straight back.
    """
    monkeypatch.setattr(trust_flow, "_COMPLETION_NOTE_TTL_S", 0.05)
    user_data: dict[str, Any] = {}
    entry = _seed(user_data)
    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()
    at_replay = asyncio.Event()
    never = asyncio.Event()

    async def _parked_replay(route: Any, user_data_: Any) -> Any:
        del route, user_data_
        at_replay.set()
        await never.wait()

    flow = await trust_flow.start_trust_wait(
        bot=_Bot(),
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry.get(ENTRY_TOKEN_KEY),
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
        replay=_parked_replay,
    )
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(at_replay.wait(), timeout=5)
    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], "premise: bind committed"

    await asyncio.sleep(0.2)
    assert trust_flow.bind_committed(flow) is True, (
        "evidence for a LIVE matching generation must not expire"
    )

    completed = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD), timeout=10
    )
    assert completed is True, (
        "a teardown starting after the TTL must still see the committed bind"
    )
    assert tmux.kill_calls == [], "and must not kill the bound window"


def test_an_orphaned_note_still_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin is scoped to LIVE flows — a note nobody uses still ages out."""
    monkeypatch.setattr(trust_flow, "_COMPLETION_NOTE_TTL_S", 0.0)
    key = (_USER, _THREAD)
    trust_flow._completed_binds[key] = trust_flow._CompletionNote(
        generation=1, window_id=_FAKE_WID, at=trust_flow._wall() - 10.0
    )
    assert trust_flow.get_flow(*key) is None, "premise: no live flow"
    assert (
        trust_flow._consume_completion(key, generation=1, window_id=_FAKE_WID) is False
    ), "an ORPHANED note past its TTL must still expire"
    trust_flow._completed_binds.clear()


# ── P1-B: the adoption gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_id_with_a_kill_in_flight_cannot_be_adopted() -> None:
    """A window id a kill is still aimed at must not be handed to a new owner."""
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    assert real_tmux.window_kill_pending(_FAKE_WID) is False

    real_tmux._kill_pending_windows[_FAKE_WID] = 1
    assert real_tmux.window_kill_pending(_FAKE_WID) is True

    settled = await real_tmux.await_kill_settled(_FAKE_WID, timeout=0.15)
    assert settled is False, "adoption must be REFUSED while the kill can land"

    real_tmux.reset_kill_pending_for_tests()
    assert await real_tmux.await_kill_settled(_FAKE_WID, timeout=0.15) is True, (
        "…and permitted once the kill has settled"
    )


@pytest.mark.asyncio
async def test_the_kill_pending_mark_survives_a_cancelled_wrapper() -> None:
    """The mark must track the WORKER THREAD, not the coroutine that started it.

    Driven through the REAL ``kill_window``: ``get_session`` is made to block
    inside the worker thread, so the libtmux call is genuinely in flight. A
    plain ``finally`` on ``kill_window`` would clear the mark the instant WE are
    cancelled — while the worker can still kill the window, which is exactly the
    interval an adoption must stay refused. The done-callback on the inner
    future is what makes the mark honest.
    """
    import threading

    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    in_worker = threading.Event()
    release = threading.Event()

    def _blocking_get_session() -> Any:
        in_worker.set()
        release.wait(timeout=10)
        return None  # a falsy session ends _sync_kill with False

    original = real_tmux.get_session
    real_tmux.get_session = _blocking_get_session  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(real_tmux.kill_window(_FAKE_WID))
        await asyncio.get_running_loop().run_in_executor(None, in_worker.wait, 5)
        assert real_tmux.window_kill_pending(_FAKE_WID) is True, (
            "the mark must be set BEFORE the work is dispatched"
        )

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

        assert real_tmux.window_kill_pending(_FAKE_WID) is True, (
            "cancelling the wrapper cannot stop the worker — the mark must "
            "persist while the kill can still land"
        )

        release.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not real_tmux.window_kill_pending(_FAKE_WID):
                break
        assert real_tmux.window_kill_pending(_FAKE_WID) is False, (
            "…and clear once the worker genuinely finished"
        )
    finally:
        release.set()
        real_tmux.get_session = original  # type: ignore[method-assign]
        real_tmux.reset_kill_pending_for_tests()


# ── P2-A: the actionable disclosure is the LAST copy ──────────────────────


@pytest.mark.asyncio
async def test_the_resend_warning_is_not_overwritten_by_generic_copy() -> None:
    """A finalized card must survive a later generic edit."""
    user_data: dict[str, Any] = {}
    entry = _seed(user_data)
    bot = _Bot()
    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()
    at_replay = asyncio.Event()
    never = asyncio.Event()

    async def _parked_replay(route: Any, user_data_: Any) -> Any:
        del route, user_data_
        at_replay.set()
        await never.wait()

    flow = await trust_flow.start_trust_wait(
        bot=bot,
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry.get(ENTRY_TOKEN_KEY),
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        cli_version="2.1.241",
        tmux_mgr=tmux,
        session_mgr=sessions,
        replay=_parked_replay,
    )
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(at_replay.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    texts = bot.texts()
    assert texts, "the card was edited"
    assert "may not have been delivered" in texts[-1], (
        f"the actionable disclosure must be the LAST copy, got: {texts[-1]!r}"
    )


# ── P2-B: the copy follows the CURRENT binding ────────────────────────────


@pytest.mark.asyncio
async def test_settle_copy_says_send_here_only_when_still_bound() -> None:
    """Bound ⇒ 'send messages here'. The accounting is unchanged either way."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    bot = _Bot()
    sessions = _Sessions()
    flow = await _start(user_data, tmux=_Tmux(pane=_IDLE), bot=bot, sessions=sessions)
    assert flow is not None
    sessions.bind_thread(_USER, _THREAD, _FAKE_WID)

    await trust_flow._settle_committed_but_unfinished_bind(flow, bot)

    last = bot.texts()[-1]
    assert "Send messages here" in last, last
    assert "may not have been delivered" in last, last


@pytest.mark.asyncio
async def test_settle_copy_does_not_claim_a_usable_topic_when_unbound() -> None:
    """Note present, binding ABSENT (``/unbind`` beat the trust teardown).

    The note still wins the ACCOUNTING question — the bind committed and the
    payload is uncertain — but 'Send messages here' on an unbound topic is
    simply false, so the USABILITY copy must follow the CURRENT binding.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    bot = _Bot()
    sessions = _Sessions()
    flow = await _start(user_data, tmux=_Tmux(pane=_IDLE), bot=bot, sessions=sessions)
    assert flow is not None
    assert sessions.get_window_for_thread(_USER, _THREAD) is None, "premise: unbound"

    await trust_flow._settle_committed_but_unfinished_bind(flow, bot)

    last = bot.texts()[-1]
    assert "Send messages here" not in last, (
        f"a false 'send here' on an UNBOUND topic: {last!r}"
    )
    assert "unbound or closed" in last, last
    assert "may not have been delivered" in last, (
        "the payload uncertainty must still be disclosed"
    )
