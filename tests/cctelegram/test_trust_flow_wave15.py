"""GH #65 wave 15 — the read's END, and who took ownership.

* P1-A the one-read stamp recorded when the coroutine LAUNCHED the read, not
  when tmux sampled, so a kill landing mid-read published a pre-kill snapshot
  under a stamp the caller's floor accepted. (A wave-14 test PINNED that
  defect; it is replaced.)
* P1-B the late-creation reaper keyed on "we timed out", missing caller
  CANCELLATION and a setup failure AFTER ``new_window`` succeeded. It now keys
  on whether the CALLER TOOK OWNERSHIP.
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
_THREAD = 15151
_USER = 9090
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


# ── P1-B: the reaper keys on OWNERSHIP, not on the timeout ────────────────


@pytest.mark.asyncio
async def test_a_cancelled_creation_still_reaps_its_late_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller CANCELLATION propagates without ever setting a timeout flag.

    The worker keeps going and its window had no owner and no reaper — the arm
    a timeout-keyed condition structurally cannot see.
    """
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

    async def _slow_create(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            started.set()
            await asyncio.sleep(0.3)
            return True, "created", "late", "@cancelled-late"
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _slow_create)

    task = asyncio.create_task(
        real_tmux.create_window("/tmp", window_name="w15", start_claude=False)
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
    assert killed == ["@cancelled-late"], (
        "a window created after the CALLER WAS CANCELLED must be reaped — the "
        "caller never took ownership of it"
    )
    real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_a_partial_creation_reports_its_window_id_and_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup failing AFTER ``new_window`` succeeded leaves a REAL window.

    Returning an empty id there made it unreachable: nobody could kill what
    nobody could name.
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

    async def _partial(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            # new_window SUCCEEDED, later setup did not.
            return False, "Failed to create window: boom", "", "@partial"
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _partial)

    ok, _msg, _name, wid = await real_tmux.create_window(
        "/tmp", window_name="w15", start_claude=False
    )
    assert ok is False, "a partial creation is not a success"
    assert wid == "@partial", (
        "a window whose creation partially succeeded must still be NAMED, or "
        "nobody can reap it"
    )

    for _ in range(100):
        await asyncio.sleep(0.02)
        if killed:
            break
    assert killed == ["@partial"], "…and it is reaped, since no caller took it"
    real_tmux.reset_kill_pending_for_tests()


# ── P2-C: the shared double MIRRORS the protocol ──────────────────────────


@pytest.mark.asyncio
async def test_the_shared_double_marks_kills_pending_until_they_settle() -> None:
    """A permissive double makes every seam-ordering bug invisible.

    If the double reports "nothing pending, fresh == cached", a test passes
    while production adopts a window with a kill in flight or reads a stale
    cache. The double must therefore MODEL the protocol, not wave it through.
    """

    class _Recording(_Tmux):
        def __init__(self) -> None:
            super().__init__(pane="")
            self.released = asyncio.Event()

        async def kill_window(self, window_id: str) -> bool:
            self.kill_calls.append(window_id)
            await self.released.wait()
            return True

    tmux = _Recording()
    assert tmux.window_kill_pending(_FAKE_WID) is False

    inner = tmux.begin_kill_locked(_FAKE_WID)
    assert tmux.window_kill_pending(_FAKE_WID) is True, (
        "the mark must be set BEFORE the work is dispatched, as production does"
    )
    assert await tmux.await_kill_settled(_FAKE_WID) is False, (
        "…and a settlement wait must actually WAIT while it is pending"
    )

    tmux.released.set()
    assert await tmux.finish_kill(_FAKE_WID, inner) is True
    await asyncio.sleep(0)
    assert tmux.window_kill_pending(_FAKE_WID) is False, (
        "the mark clears only when the kill genuinely finished"
    )


@pytest.mark.asyncio
async def test_the_shared_double_distinguishes_fresh_from_cached() -> None:
    """A killed window vanishes from FRESH reads but can linger in CACHED ones.

    That difference is the whole reason adoption probes must ask for fresh — a
    double where they are identical cannot catch a probe that forgot.
    """

    class _Listing(_Tmux):
        def __init__(self) -> None:
            super().__init__(pane="")
            self._listing = [
                SimpleNamespace(window_id=_FAKE_WID, window_name="w", cwd="/x")
            ]

        async def kill_window(self, window_id: str) -> bool:
            self.kill_calls.append(window_id)
            return True

    tmux = _Listing()
    assert await tmux.find_window_by_id(_FAKE_WID, fresh=True) is not None

    inner = tmux.begin_kill_locked(_FAKE_WID)
    await tmux.finish_kill(_FAKE_WID, inner)
    await asyncio.sleep(0)

    assert await tmux.find_window_by_id(_FAKE_WID, fresh=True) is None, (
        "a FRESH probe must not see a window a confirmed kill removed"
    )
    assert await tmux.find_window_by_id(_FAKE_WID) is not None, (
        "…while the CACHED view still can — which is exactly what makes a "
        "non-fresh adoption probe fail its test instead of passing by accident"
    )
