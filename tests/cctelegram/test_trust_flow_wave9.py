"""GH #65 wave 9 — durable evidence, the leash, and the reclaimed fence.

Round 9's P1s were both about state OUTLIVING the thing that owned it:

  * P1-A ``bind_thread`` is SYNCHRONOUS, so a cancellation aimed at the tail
    cannot interrupt it — it lands at the NEXT await, with the binding already
    written. Deriving the outcome from the task flags alone then reported
    "failed" for a bind that had COMMITTED, so ``/start`` skipped the
    bound-topic teardown while a live binding survived and the pending payload's
    files were deleted underneath it.
  * P1-B ``wait_for(shield(...))`` bounds the WAITER, not the WORK: on timeout
    the cleanup task kept running, detached, and could fire ``kill_window``
    after ownership had been released — against a window a new flow had adopted.

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
_THREAD = 9191
_USER = 3434
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


# ── P1-A: outcome derivation consults the DURABLE EVIDENCE ────────────────


@pytest.mark.asyncio
async def test_a_bind_cancelled_at_the_replay_still_counts_as_completed() -> None:
    """A COMMITTED bind cut short at the replay is a completion, not a failure.

    ``bind_thread`` is synchronous: cancellation cannot land inside it, only at
    the NEXT await. Parking a fake replay puts the tail at exactly that await
    with the binding and the completion note already written — the shape the
    task flags misreport.

    What must hold: the caller learns completion (so ``/start`` runs its
    bound-topic teardown), the binding SURVIVES, the guarded cleanup does NOT
    kill the window, and the pending payload's files are NOT silently deleted.
    """
    user_data: dict[str, Any] = {}
    entry = _seed(user_data)
    files_dir = app_dir() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    stashed = files_dir / "wave9-pending.txt"
    stashed.write_text("the user's first message attachment")
    # The PRODUCTION key (review r10 P3-A). Seeding "pending_files" made this
    # assertion vacuous: `_delete_pending_attachments` reads
    # `_pending_thread_attachments`, so nothing was ever a candidate for
    # deletion and the test passed against a delete-everything mutation too.
    entry["_pending_thread_attachments"] = [{"path": str(stashed)}]

    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()
    at_replay = asyncio.Event()
    never = asyncio.Event()

    async def _parked_replay(route: Any, user_data_: Any) -> Any:
        del route, user_data_
        at_replay.set()
        await never.wait()

    entry_token = entry.get(ENTRY_TOKEN_KEY)
    flow = await trust_flow.start_trust_wait(
        bot=_Bot(),
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        user_data=user_data,
        entry_token=entry_token,
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
    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], (
        "premise: the synchronous bind committed before the await"
    )

    completed = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD), timeout=10
    )

    assert completed is True, (
        "a COMMITTED bind must be reported as completed, or /start skips the "
        "bound-topic teardown while a live binding survives the reset"
    )
    assert tmux.kill_calls == [], "the bound window must NOT be killed"
    assert sessions.binds == [(_USER, _THREAD, _FAKE_WID)], "the binding survives"
    assert stashed.exists(), (
        "the pending payload's files must NOT be silently deleted — the replay "
        "may never have run, so the user has to be able to resend"
    )
    stashed.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_tail_that_failed_before_the_bind_is_still_a_failure() -> None:
    """The other side of P1-A: no durable evidence means today's failure arm."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_IDLE)

    class _RaisesBeforeBind(_Sessions):
        def get_window_state(self, window_id: str) -> Any:
            raise RuntimeError("exploded BEFORE bind_thread")

    sessions = _RaisesBeforeBind()
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True

    completed = await asyncio.wait_for(
        trust_flow.teardown_thread(_USER, _THREAD), timeout=10
    )

    assert completed is False, "nothing was bound — this is a genuine failure"
    assert sessions.binds == [], "and no binding exists"


def test_bind_committed_is_generation_and_window_qualified() -> None:
    """The evidence must never let an OLDER flow's note count as this one's."""
    flow = trust_flow.TrustFlow(
        user_id=_USER,
        thread_id=_THREAD,
        chat_id=-100,
        created_wid=_FAKE_WID,
        window_name="repo",
        selected_path="/repo",
        create_message="Created",
        user_data={},
        generation=7,
        card_chat_id=-100,
        card_msg_id=999,
        resume_id=None,
        cli_version="2.1.241",
    )
    assert trust_flow.bind_committed(flow) is False

    trust_flow._note_completion(flow)
    assert trust_flow.bind_committed(flow) is True, "its OWN note counts"

    def _variant(**kwargs: Any) -> trust_flow.TrustFlow:
        base: dict[str, Any] = dict(
            user_id=_USER,
            thread_id=_THREAD,
            chat_id=-100,
            created_wid=_FAKE_WID,
            window_name="repo",
            selected_path="/repo",
            create_message="Created",
            user_data={},
            generation=7,
            card_chat_id=-100,
            card_msg_id=999,
            resume_id=None,
            cli_version="2.1.241",
        )
        base.update(kwargs)
        return trust_flow.TrustFlow(**base)

    assert trust_flow.bind_committed(_variant(generation=8)) is False
    assert trust_flow.bind_committed(_variant(created_wid="@fake-other")) is False
    # And reading it must NOT spend it — teardown still needs to consume it.
    assert trust_flow.bind_committed(flow) is True, "the peek must not consume"
    trust_flow._completed_binds.clear()


# ── P1-B: the orphan cleanup is LEASHED, and the kill honors ownership ────


@pytest.mark.asyncio
async def test_a_straggler_cleanup_cannot_kill_a_window_a_new_flow_adopted() -> None:
    """A cleanup that outlives its flow must never kill an adopted window."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    tmux = _Tmux(pane=_TRUST)

    inside_kill = asyncio.Event()
    release_kill = asyncio.Event()

    async def _park_in_kill() -> None:
        inside_kill.set()
        await release_kill.wait()

    tmux.on_kill = _park_in_kill
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert flow is not None
    await asyncio.sleep(0.03)

    teardown = asyncio.create_task(trust_flow.teardown_thread(_USER, _THREAD))
    await asyncio.wait_for(inside_kill.wait(), timeout=5)
    teardown.cancel()
    try:
        await asyncio.wait_for(teardown, timeout=10)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    assert trust_flow.get_flow(_USER, _THREAD) is None, "the old flow was dropped"

    # A NEW flow adopts the very same window id — through the SAME tmux manager,
    # which is what production has: one singleton for every flow. A separate
    # fake per flow would make this assertion vacuous.
    fresh_data: dict[str, Any] = {}
    _seed(fresh_data)
    adopter = await _start(fresh_data, tmux=tmux, bot=_Bot(), sessions=_Sessions())
    assert adopter is not None
    assert adopter.created_wid == _FAKE_WID
    kills_before_adoption = len(tmux.kill_calls)

    # Let any straggler run to completion.
    release_kill.set()
    await asyncio.sleep(0.3)

    assert len(tmux.kill_calls) == kills_before_adoption, (
        f"a straggler cleanup killed a window the NEW flow now owns: {tmux.kill_calls}"
    )
    assert trust_flow.get_flow(_USER, _THREAD) is adopter, "the new flow survives"

    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_the_owner_guard_refuses_a_kill_for_an_adopted_window() -> None:
    """The belt, tested directly: ownership is re-checked before the kill."""
    tmux = _Tmux(pane=_TRUST)

    async def _not_ours() -> bool:
        return False

    outcome = await trust_flow.cleanup_created_window(
        _FAKE_WID,
        "repo",
        tmux,
        reason="straggler",
        session_mgr=_Sessions(),
        owner_guard=_not_ours,
    )
    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.kill_calls == [], "a disowned window must never be killed"


# ── P2-A: a dead fence owner is reclaimed ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_fence_left_by_a_dead_owner_can_be_reclaimed() -> None:
    """Ownership must not let a corpse fence a topic forever."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    raised = asyncio.Event()
    never = asyncio.Event()

    async def _fences_then_dies() -> None:
        async with trust_flow.creation_lock(_USER, _THREAD):
            flow.raise_fence()
        raised.set()
        await never.wait()

    corpse = asyncio.create_task(_fences_then_dies())
    await asyncio.wait_for(raised.wait(), timeout=2)
    corpse.cancel()
    try:
        await corpse
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    assert corpse.done()
    assert flow.teardown_fenced, "the corpse still holds the fence"

    async with trust_flow.creation_lock(_USER, _THREAD):
        flow.raise_fence()
        assert flow.fence_owner is asyncio.current_task(), (
            "a second teardown could not reclaim a fence from a DEAD owner"
        )
        assert flow.lower_fence_if_owned() is True
    assert not flow.teardown_fenced

    await trust_flow.teardown_thread(_USER, _THREAD)


@pytest.mark.asyncio
async def test_a_live_fence_owner_is_still_respected() -> None:
    """Reclaim must be scoped to DEAD owners — a live one keeps its fence."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    flow = await _start(
        user_data, tmux=_Tmux(pane=_TRUST), bot=_Bot(), sessions=_Sessions()
    )
    assert flow is not None

    raised = asyncio.Event()
    release = asyncio.Event()

    async def _holds_the_fence() -> None:
        async with trust_flow.creation_lock(_USER, _THREAD):
            flow.raise_fence()
        raised.set()
        await release.wait()

    holder = asyncio.create_task(_holds_the_fence())
    await asyncio.wait_for(raised.wait(), timeout=2)

    async with trust_flow.creation_lock(_USER, _THREAD):
        flow.raise_fence()
        assert flow.fence_owner is not asyncio.current_task(), (
            "a LIVE owner's fence was stolen"
        )
        assert flow.lower_fence_if_owned() is False

    release.set()
    await holder
    await trust_flow.teardown_thread(_USER, _THREAD)
