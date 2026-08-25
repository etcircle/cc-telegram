"""GH #65 wave 16 — the adoption-listing replacement and the two-party handshake.

* STRUCTURAL: adoption decisions no longer read the TTL cache at all. Three
  consecutive rounds found a defect in the previous round's fix to that seam,
  so the design was replaced rather than patched again.
* P1-A: the reaper's callback could fire BEFORE the shielded waiter resumed,
  read "not taken", and kill a window the caller was about to return
  successfully. Reaping now needs BOTH facts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser, tmux_manager as tmux_mod
from cctelegram.config import config
from cctelegram.callback_dispatcher import directory as directory_module
from cctelegram.handlers import decision_token, trust_flow
from cctelegram.handlers import inbound_telegram as inbound_module
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
_THREAD = 16161
_USER = 1616
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


# ── P1-A: the two-party disposition handshake ─────────────────────────────


@pytest.mark.asyncio
async def test_a_successful_creation_is_never_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback can fire BEFORE the shielded waiter resumes.

    That is the whole race: the worker finishes, its done-callback runs on the
    same loop iteration, sees the caller has not recorded "taken" yet — because
    the caller has not been scheduled back — and kills a window that is about to
    be returned successfully.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()

    killed: list[str] = []

    async def _record_kill(window_id: str) -> bool:
        killed.append(window_id)
        return True

    monkeypatch.setattr(real_tmux, "kill_window", _record_kill)

    real_to_thread = asyncio.to_thread

    async def _immediate(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            # Completes WITHOUT yielding back to the waiter first — the
            # done-callback therefore runs while the caller is still suspended.
            return True, "created", "w16", "@good"
        return await real_to_thread(func, *a, **kw)

    async def _listing() -> Any:
        return [tmux_mod.TmuxWindow(window_id="@good", window_name="w", cwd="/x")]

    monkeypatch.setattr(asyncio, "to_thread", _immediate)
    monkeypatch.setattr(real_tmux, "adoption_listing", _listing)

    ok, _msg, _name, wid = await real_tmux.create_window(
        "/tmp", window_name="w16", start_claude=False
    )
    assert ok is True and wid == "@good"

    for _ in range(20):
        await asyncio.sleep(0.01)
    assert killed == [], (
        "a SUCCESSFULLY created window was reaped — the callback decided alone, "
        "before the caller could record that it was taking ownership"
    )
    real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_a_cancelled_creation_reaps_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation reaps — and reaps ONCE, not once per party."""
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()

    killed: list[str] = []

    async def _record_kill(window_id: str) -> bool:
        killed.append(window_id)
        return True

    monkeypatch.setattr(real_tmux, "kill_window", _record_kill)

    started = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def _slow(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            started.set()
            await asyncio.sleep(0.2)
            return True, "created", "w16", "@cancelled"
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _slow)

    task = asyncio.create_task(
        real_tmux.create_window("/tmp", window_name="w16", start_claude=False)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    for _ in range(100):
        await asyncio.sleep(0.02)
        if killed:
            break
    await asyncio.sleep(0.1)
    assert killed == ["@cancelled"], (
        f"a cancelled creation must reap EXACTLY once, got {killed}"
    )
    real_tmux.reset_kill_pending_for_tests()


def test_the_handshake_needs_both_facts_and_reaps_once() -> None:
    """The state machine itself, in both orders.

    Neither party may decide alone, and whichever learns the SECOND fact is the
    one that reaps — exactly once, in either arrival order.
    """
    # Worker first, then a declining caller.
    d = tmux_mod._CreateDisposition()
    assert d.record_worker("@w") is False, "the worker alone must not reap"
    assert d.record_caller(tmux_mod.CallerDisposition.DECLINED) is True
    assert d.record_caller(tmux_mod.CallerDisposition.DECLINED) is False, "once"

    # Caller first, then the worker.
    d = tmux_mod._CreateDisposition()
    assert d.record_caller(tmux_mod.CallerDisposition.DECLINED) is False, (
        "the caller alone must not reap — the worker outcome is unknown"
    )
    assert d.record_worker("@w") is True
    assert d.record_worker("@w") is False, "once"

    # A TAKEN caller is never reaped, in either order.
    d = tmux_mod._CreateDisposition()
    assert d.record_worker("@w") is False
    assert d.record_caller(tmux_mod.CallerDisposition.TAKEN) is False
    # …and a later defaulted DECLINE cannot overwrite it.
    assert d.record_caller(tmux_mod.CallerDisposition.DECLINED) is False, (
        "first decision wins — the finally must not undo a recorded TAKEN"
    )

    # Nothing to reap when the worker made nothing.
    d = tmux_mod._CreateDisposition()
    assert d.record_worker("") is False
    assert d.record_caller(tmux_mod.CallerDisposition.DECLINED) is False


# ── STRUCTURAL: adoption reads are DIRECT ─────────────────────────────────


@pytest.mark.asyncio
async def test_no_adoption_seam_reads_the_ttl_cache() -> None:
    """The design replacement, asserted at the seam.

    Adoption correctness no longer depends on the cache, so an adoption seam
    must produce ZERO cache reads — unrelated invalidations cannot participate,
    and the three paths cannot drift in freshness semantics again.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)
    tmux._listing = [
        SimpleNamespace(window_id=_FAKE_WID, window_name="repo", cwd="/repo")
    ]

    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    for _ in range(200):
        await asyncio.sleep(0.01)
        if sessions.binds:
            break

    assert sessions.binds, "the bind completed"
    assert tmux.cache_reads == 0, (
        f"an adoption seam read the TTL cache {tmux.cache_reads} times — "
        "adoption must read tmux directly"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P2: the double's finish_kill is SHIELDED, like production ─────────────


@pytest.mark.asyncio
async def test_cancelling_a_kill_waiter_leaves_the_mark_pending() -> None:
    """Cancelling the WAITER must not cancel the kill.

    Awaiting ``inner`` bare let a cancelled waiter cancel the fake kill and
    clear the pending mark — masking exactly the cancelled-kill race the
    protocol exists to test, and making adoption look safe while a kill was
    still in flight.
    """

    class _Parking(_Tmux):
        def __init__(self) -> None:
            super().__init__(pane="")
            self.release = asyncio.Event()

        async def kill_window(self, window_id: str) -> bool:
            self.kill_calls.append(window_id)
            await self.release.wait()
            return True

    tmux = _Parking()
    inner = tmux.begin_kill_locked(_FAKE_WID)
    waiter = asyncio.create_task(tmux.finish_kill(_FAKE_WID, inner))
    await asyncio.sleep(0)

    waiter.cancel()
    try:
        await waiter
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    assert tmux.window_kill_pending(_FAKE_WID) is True, (
        "cancelling the waiter cancelled the KILL and cleared the mark — a "
        "later adopter would see a free window while the kill can still land"
    )

    tmux.release.set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not tmux.window_kill_pending(_FAKE_WID):
            break
    assert tmux.window_kill_pending(_FAKE_WID) is False, (
        "…and the mark clears only when the kill genuinely completed"
    )


# ── P1-B: every typed failure is handled at every adoption door ───────────


@pytest.mark.asyncio
async def test_a_lifecycle_timeout_at_the_trust_door_refuses_cleanly() -> None:
    """Fault injection: the only typed failure the adoption path can raise."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)
    tmux._listing = [
        SimpleNamespace(window_id=_FAKE_WID, window_name="repo", cwd="/repo")
    ]
    raised = asyncio.Event()

    async def _always_times_out(coro: Any, *, what: str, **kwargs: Any) -> Any:
        del kwargs
        coro.close()
        raised.set()
        raise tmux_mod.LifecycleTimeout(f"injected at {what}")

    tmux._bounded_lifecycle = _always_times_out  # type: ignore[attr-defined]

    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(raised.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert sessions.binds == [], "a timed-out adoption check must never bind"
    assert trust_flow.get_flow(_USER, _THREAD) is None, "and the flow is settled"


@pytest.mark.asyncio
async def test_a_refused_legacy_bind_cleans_and_releases() -> None:
    """The legacy seam owns creation, so its refusal must also CLEAN.

    This is the door that leaked a reserved, alive, unbound window when a typed
    failure escaped unhandled.
    """
    trust_flow.reset_reservations_for_tests()
    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()

    trust_flow.reserve_window(_FAKE_WID, "tok-16")
    assert _FAKE_WID in trust_flow.windows_owned_by_live_flows()

    outcome = await trust_flow.cleanup_created_window(
        _FAKE_WID, "repo", tmux, reason="legacy bind refused", session_mgr=sessions
    )
    trust_flow.release_window_reservation(_FAKE_WID)

    assert outcome is trust_flow.CleanupOutcome.KILLED
    assert tmux.kill_calls == [_FAKE_WID], "the unowned window is cleaned up"
    assert _FAKE_WID not in trust_flow.windows_owned_by_live_flows(), (
        "and the reservation is released only after that settlement"
    )
    trust_flow.reset_reservations_for_tests()


def test_every_adoption_door_handles_the_typed_failures_it_can_raise() -> None:
    """No adoption seam may let a typed adoption failure escape.

    Swept as source, because the failure is what a USER sees: an unhandled
    typed refusal is a stack trace and a leaked window, not a message.
    """
    doors = {
        "trust_flow.py": Path(trust_flow.__file__),
        "inbound_telegram.py": Path(inbound_module.__file__),
        "directory.py": Path(directory_module.__file__),
    }
    for name, path in doors.items():
        text = path.read_text()
        assert "adoption_listing()" in text, (
            f"{name} does not use the direct adoption listing"
        )
        assert "LifecycleTimeout" in text, (
            f"{name} performs a bounded adoption await but never handles "
            "LifecycleTimeout — the one typed failure it can raise"
        )
