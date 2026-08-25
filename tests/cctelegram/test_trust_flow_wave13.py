"""GH #65 wave 13 — the last three holes in the adoption protocol.

  * P1-A ``fresh=True`` invalidated BEFORE taking the list lock, so a refresh
    already in flight could publish its PRE-KILL snapshot afterwards and the
    waiting "fresh" caller accepted it. Closed with an invalidation GENERATION:
    a refresh publishes only if the generation it started under is still
    current, and a fresh caller demands a snapshot at or above its own.
  * P1-B the lifecycle lock was held across UNBOUNDED tmux awaits, so one
    wedged tmux operation blocked every other window's lifecycle — including
    the kills that topic teardown and forced trust cleanup depend on.
  * P1-C two ownership gaps: the window existed unowned between creation and
    flow install, and the legacy resume / lane-disabled seam bound with no
    lock, no fresh probe and no exclusivity check.

Every flow here is driven through the public seams with REAL tasks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser, tmux_manager as tmux_mod
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
_THREAD = 13131
_USER = 7878
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


# ── P1-A: the fresh refresh is GENERATION-GUARDED ─────────────────────────


@pytest.mark.asyncio
async def test_a_stale_in_flight_refresh_cannot_publish_over_an_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that started BEFORE a kill must not publish its snapshot after.

    The old ``fresh=True`` invalidated and then queued on the list lock, so a
    refresh already running could publish its PRE-KILL listing afterwards — and
    the waiting fresh caller took it straight off the cache fast path. That is
    the cached corpse an adoption would then bind.
    """
    from cctelegram.tmux_manager import TmuxWindow, tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux._invalidate_list_cache()

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _slow_direct() -> Any:
        calls.append(1)
        if len(calls) == 1:
            # The FIRST (stale) read is in flight when the kill lands.
            started.set()
            await release.wait()
            return [TmuxWindow(window_id=_FAKE_WID, window_name="doomed", cwd="/x")]
        # Post-kill truth: the window is gone.
        return [TmuxWindow(window_id="@other", window_name="alive", cwd="/y")]

    monkeypatch.setattr(real_tmux, "_list_windows_direct", _slow_direct)

    # 1) A refresh is IN FLIGHT, holding the list lock, with the pre-kill view.
    stale = asyncio.create_task(real_tmux.list_windows())
    await asyncio.wait_for(started.wait(), timeout=2)

    # 2) A kill lands and invalidates.
    real_tmux._invalidate_list_cache()

    # 3) The fresh probe starts NOW — it invalidates and then queues on the list
    #    lock the stale refresh is holding. This is the interleaving: the stale
    #    snapshot is published while the fresh caller is already waiting.
    fresh = asyncio.create_task(real_tmux.find_window_by_id(_FAKE_WID, fresh=True))
    await asyncio.sleep(0.05)

    # 4) The stale refresh publishes its PRE-KILL snapshot and releases.
    release.set()
    await asyncio.wait_for(stale, timeout=5)

    found = await asyncio.wait_for(fresh, timeout=5)
    assert found is None, (
        "the fresh probe accepted a snapshot that STARTED before its own "
        "invalidation — it is reading a cached corpse"
    )


@pytest.mark.asyncio
async def test_an_uncontended_fresh_probe_still_uses_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not turn every fresh probe into a retry storm."""
    from cctelegram.tmux_manager import TmuxWindow, tmux_manager as real_tmux

    real_tmux._invalidate_list_cache()
    calls: list[int] = []

    async def _direct() -> Any:
        calls.append(1)
        return [TmuxWindow(window_id=_FAKE_WID, window_name="w", cwd="/x")]

    monkeypatch.setattr(real_tmux, "_list_windows_direct", _direct)

    found = await real_tmux.find_window_by_id(_FAKE_WID, fresh=True)
    assert found is not None
    assert calls == [1], f"expected exactly one refresh, got {len(calls)}"


# ── P1-B: no unbounded tmux await under the lifecycle lock ────────────────


@pytest.mark.asyncio
async def test_a_wedged_create_releases_the_lifecycle_lock_within_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One wedged tmux call must not freeze every other window's lifecycle.

    The lock serializes kill-registration against adoption, so an unbounded
    await inside it is a GLOBAL stall — including for the kills that topic
    teardown and forced trust cleanup depend on to recover.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    monkeypatch.setattr(tmux_mod, "LIFECYCLE_TMUX_TIMEOUT_S", 0.2)

    real_to_thread = asyncio.to_thread

    async def _wedged(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            await asyncio.sleep(30)
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _wedged)

    create = asyncio.create_task(
        real_tmux.create_window("/tmp", window_name="w13", start_claude=False)
    )
    await asyncio.sleep(0.05)
    assert real_tmux.window_lifecycle_lock().locked(), "premise: the hold is taken"

    ok, msg, _n, _w = await asyncio.wait_for(create, timeout=5)
    assert ok is False, "a wedged create must fail honestly, not hang"
    assert "took too long" in msg, msg
    assert not real_tmux.window_lifecycle_lock().locked(), (
        "the lifecycle lock must be RELEASED when the bound expires"
    )


@pytest.mark.asyncio
async def test_a_concurrent_kill_proceeds_while_a_create_is_wedged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the bound: a kill still makes progress."""
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    monkeypatch.setattr(tmux_mod, "LIFECYCLE_TMUX_TIMEOUT_S", 0.2)

    real_to_thread = asyncio.to_thread

    async def _wedged(func: Any, *a: Any, **kw: Any) -> Any:
        name = getattr(func, "__name__", "")
        if name == "_create_and_start":
            await asyncio.sleep(30)
        if name == "_sync_kill":
            return True
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _wedged)

    create = asyncio.create_task(
        real_tmux.create_window("/tmp", window_name="w13", start_claude=False)
    )
    await asyncio.sleep(0.05)

    killed = await asyncio.wait_for(real_tmux.kill_window("@other"), timeout=5)
    assert killed is True, (
        "a kill must not be hostage to a wedged create — topic teardown and "
        "forced trust cleanup both run through it"
    )
    await asyncio.wait_for(create, timeout=5)


@pytest.mark.asyncio
async def test_kill_window_stops_waiting_for_the_lock_and_reports_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill that cannot get the lock fails honestly instead of queueing."""
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    monkeypatch.setattr(tmux_mod, "KILL_LOCK_TIMEOUT_S", 0.15)

    held = asyncio.Event()
    release = asyncio.Event()

    async def _hog() -> None:
        async with real_tmux.window_lifecycle_lock():
            held.set()
            await release.wait()

    hog = asyncio.create_task(_hog())
    await asyncio.wait_for(held.wait(), timeout=2)

    killed = await asyncio.wait_for(real_tmux.kill_window(_FAKE_WID), timeout=5)
    assert killed is False, "an unacquirable lock must yield an honest failure"

    release.set()
    await hog


# ── P1-C: ownership begins at CREATION, and the third seam joins ──────────


@pytest.mark.asyncio
async def test_a_reserved_window_is_not_offered_before_its_flow_installs() -> None:
    """The window exists unowned between create_window and the flow install.

    The version probe and two Telegram edits happen in that interval, and the
    browser offered the window throughout it.
    """
    from cctelegram.handlers.inbound_telegram import _list_unbound_windows

    trust_flow.reset_reservations_for_tests()
    sessions = _Sessions()

    class _ListingTmux(_Tmux):
        async def list_windows(self) -> Any:
            return [
                SimpleNamespace(window_id=_FAKE_WID, window_name="new", cwd="/repo"),
                SimpleNamespace(
                    window_id="@fake-free", window_name="other", cwd="/other"
                ),
            ]

    tmux = _ListingTmux(pane=_TRUST)

    # BEFORE any flow exists — exactly the probe interval.
    trust_flow.reserve_window(_FAKE_WID, "tok-13")
    try:
        ids = {wid for wid, _, _ in await _list_unbound_windows(tmux, sessions)}
        assert _FAKE_WID not in ids, (
            "a window reserved by an in-flight creation must not be offered"
        )
        assert "@fake-free" in ids, "…while genuinely free windows still are"
    finally:
        trust_flow.reset_reservations_for_tests()


def test_a_dying_picker_entry_orphans_but_does_not_free_its_reservation() -> None:
    """Entry death must NOT expose the window (review r14 P1-E).

    Wave 13 freed the reservation when the entry token died. That was premature:
    an aborted creation drops its entry and THEN runs the guarded cleanup, so
    freeing at entry death exposed the window for adoption DURING that cleanup —
    and the cleanup's kill then landed on whoever had just taken it. The
    reservation survives the token, ORPHANED, until the window's disposition
    SETTLES.
    """
    from cctelegram.handlers.directory_browser import (
        drop_picker_entry,
        ensure_picker_entry,
    )

    trust_flow.reset_reservations_for_tests()
    user_data: dict[str, Any] = {}
    entry = ensure_picker_entry(user_data, _THREAD)
    assert entry is not None
    token = entry.get(ENTRY_TOKEN_KEY)
    trust_flow.reserve_window(_FAKE_WID, token)
    assert _FAKE_WID in trust_flow.windows_owned_by_live_flows()

    drop_picker_entry(user_data, _THREAD)

    assert _FAKE_WID in trust_flow.windows_owned_by_live_flows(), (
        "the window must stay unadoptable while its cleanup is still to run"
    )

    # Only a SETTLED disposition frees it.
    trust_flow.release_window_reservation(_FAKE_WID)
    assert _FAKE_WID not in trust_flow.windows_owned_by_live_flows()
    trust_flow.reset_reservations_for_tests()
