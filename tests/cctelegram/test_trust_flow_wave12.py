"""GH #65 wave 12 — one root cause, three expressions.

Round 11 made every adoption seam check the kill-pending state; round 12 found
all three checks were CHECK-THEN-ACT, plus two ways the check could read stale
truth:

  * P1-A a kill could REGISTER after an adopter's gate check and land after its
    verification. Closed with a WINDOW-LIFECYCLE LOCK owned by tmux_manager,
    which makes kill-registration and adoption mutually exclusive rather than
    merely ordered. The lock is INNERMOST and the bounded settlement wait stays
    OUTSIDE it.
  * P1-B the "fresh" existence probes read the 1 s listing cache, and the kill
    worker's cache invalidation was skipped when cancellation exited at the
    shielded await — so revalidation could bind a cached corpse.
  * P1-C exclusivity was checked in ONE direction only (is this THREAD bound
    elsewhere), and the directory browser offered mid-flow trust windows as
    "unbound" — two routes to one window.

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
_THREAD = 12121
_USER = 6767
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


# ── P1-A: registration and adoption are MUTUALLY EXCLUSIVE ────────────────


@pytest.mark.asyncio
async def test_a_kill_cannot_register_between_an_adopters_check_and_its_commit() -> (
    None
):
    """The gates were check-then-act; the lock makes them atomic.

    Driven for real: an adopter holds the lifecycle lock (as ``create_window``
    and both bind seams now do across check→commit), and a kill tries to
    REGISTER during that hold. The registration must not interpose — it lands
    strictly before or strictly after, never in between.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()

    observed_during_hold: list[bool] = []
    holding = asyncio.Event()
    release = asyncio.Event()
    kill_registered = asyncio.Event()

    async def _adopter() -> None:
        async with real_tmux.window_lifecycle_lock():
            holding.set()
            await release.wait()
            observed_during_hold.append(real_tmux.any_kill_pending())

    async def _killer() -> None:
        async with real_tmux.window_lifecycle_lock():
            real_tmux._kill_pending_windows[_FAKE_WID] = 1
            kill_registered.set()

    adopter = asyncio.create_task(_adopter())
    await asyncio.wait_for(holding.wait(), timeout=2)

    killer = asyncio.create_task(_killer())
    await asyncio.sleep(0.05)
    assert not kill_registered.is_set(), (
        "a kill REGISTERED while an adopter held the lifecycle lock — the gate "
        "is still check-then-act"
    )

    release.set()
    await asyncio.wait_for(adopter, timeout=2)
    await asyncio.wait_for(killer, timeout=2)

    assert observed_during_hold == [False], (
        "the adopter's commit decision must not see a kill that had to wait"
    )
    assert real_tmux.window_kill_pending(_FAKE_WID) is True, "…which lands after"
    real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_kill_window_registers_its_mark_under_the_lifecycle_lock() -> None:
    """The registration itself must take the lock, or the exclusion is a fiction."""
    import threading

    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    in_worker = threading.Event()
    release_worker = threading.Event()

    def _blocking_get_session() -> Any:
        in_worker.set()
        release_worker.wait(timeout=10)
        return None

    original = real_tmux.get_session
    real_tmux.get_session = _blocking_get_session  # type: ignore[method-assign]
    try:
        async with real_tmux.window_lifecycle_lock():
            task = asyncio.create_task(real_tmux.kill_window(_FAKE_WID))
            await asyncio.sleep(0.05)
            assert real_tmux.window_kill_pending(_FAKE_WID) is False, (
                "kill_window registered its mark WITHOUT taking the lifecycle "
                "lock — an adopter holding it could still be interposed on"
            )
        await asyncio.get_running_loop().run_in_executor(None, in_worker.wait, 5)
        assert real_tmux.window_kill_pending(_FAKE_WID) is True
        release_worker.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release_worker.set()
        real_tmux.get_session = original  # type: ignore[method-assign]
        real_tmux.reset_kill_pending_for_tests()


# ── P1-B: probes must be FRESH, and a landed kill always invalidates ──────


@pytest.mark.asyncio
async def test_a_cancelled_kill_still_invalidates_the_listing_cache() -> None:
    """The invalidation below the shielded await is SKIPPED on cancellation.

    The worker still lands the kill, so the 1 s cache kept serving the corpse
    and a revalidating adopter could bind it. Moving the invalidation into the
    done-callback makes a landed kill ALWAYS invalidate.
    """
    import threading

    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    invalidations: list[int] = []
    original_invalidate = real_tmux._invalidate_list_cache

    def _counting_invalidate() -> None:
        invalidations.append(1)
        original_invalidate()

    in_worker = threading.Event()
    release_worker = threading.Event()

    def _blocking_get_session() -> Any:
        in_worker.set()
        release_worker.wait(timeout=10)
        return None

    original_get = real_tmux.get_session
    real_tmux.get_session = _blocking_get_session  # type: ignore[method-assign]
    real_tmux._invalidate_list_cache = _counting_invalidate  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(real_tmux.kill_window(_FAKE_WID))
        await asyncio.get_running_loop().run_in_executor(None, in_worker.wait, 5)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        invalidations.clear()

        release_worker.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if invalidations:
                break
        assert invalidations, (
            "a kill that LANDED after its wrapper was cancelled must still "
            "invalidate the listing cache, or revalidation reads a corpse"
        )
    finally:
        release_worker.set()
        real_tmux.get_session = original_get  # type: ignore[method-assign]
        real_tmux._invalidate_list_cache = original_invalidate  # type: ignore[method-assign]
        real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_the_trust_revalidation_probe_bypasses_the_listing_cache() -> None:
    """Every adoption probe must ask tmux, not the 1 s cache."""
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)
    seen_fresh: list[bool] = []

    async def _fresh_listing() -> Any:
        # Reaching the FRESH listing seam at all is the assertion: the cached
        # ``list_windows`` must never be what an adoption probe consults.
        seen_fresh.append(True)
        return [SimpleNamespace(window_id=_FAKE_WID, window_name="repo", cwd="/repo")]

    async def _cached_listing() -> Any:
        seen_fresh.append(False)
        return [SimpleNamespace(window_id=_FAKE_WID, window_name="repo", cwd="/repo")]

    tmux.list_windows_fresh = _fresh_listing  # type: ignore[attr-defined]
    tmux.list_windows = _cached_listing  # type: ignore[attr-defined]
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    for _ in range(100):
        await asyncio.sleep(0.02)
        if seen_fresh:
            break

    assert seen_fresh, "the revalidation probe must run"
    assert all(seen_fresh), (
        "an adoption probe read the 1 s listing cache — it can be a full second "
        "behind a landed kill"
    )
    await trust_flow.teardown_thread(_USER, _THREAD)


# ── P1-C: exclusivity is TWO-WAY, and mid-flow windows are not adoptable ──


@pytest.mark.asyncio
async def test_the_trust_bind_refuses_when_another_topic_took_the_window() -> None:
    """The OTHER direction of exclusivity.

    Only "is this THREAD bound elsewhere" was checked, so another topic binding
    OUR ``created_wid`` during the settlement wait produced two routes to one
    window — invisible to a thread-only check.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)
    raced = asyncio.Event()

    async def _settle_then_race(window_id: str, **kwargs: Any) -> bool:
        del window_id, kwargs
        sessions.binds.append((_USER, _THREAD + 999, _FAKE_WID))
        raced.set()
        return True

    tmux.await_kill_settled = _settle_then_race  # type: ignore[attr-defined]
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(raced.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert (_USER, _THREAD, _FAKE_WID) not in sessions.binds, (
        "two routes to one window: the trust bind must refuse a window another "
        "topic already claimed"
    )


@pytest.mark.asyncio
async def test_a_live_trust_flows_window_is_not_offered_as_unbound() -> None:
    """Close the race AT THE SOURCE, not just in the detector."""
    from cctelegram.handlers.inbound_telegram import _list_unbound_windows

    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()

    class _ListingTmux(_Tmux):
        async def list_windows(self) -> Any:
            return [
                SimpleNamespace(window_id=_FAKE_WID, window_name="repo", cwd="/repo"),
                SimpleNamespace(
                    window_id="@fake-free", window_name="other", cwd="/other"
                ),
            ]

    tmux = _ListingTmux(pane=_TRUST)
    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    assert flow.created_wid == _FAKE_WID

    unbound = await _list_unbound_windows(tmux, sessions)
    ids = {wid for wid, _, _ in unbound}

    assert _FAKE_WID not in ids, (
        "a window a LIVE trust flow owns must never be offered for adoption"
    )
    assert "@fake-free" in ids, "…while genuinely free windows still are"

    await trust_flow.teardown_thread(_USER, _THREAD)
