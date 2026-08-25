"""GH #65 wave 14 — finishing the wave-13 mechanisms everywhere.

Round 14 was mostly INCOMPLETE APPLICATION rather than new design: the
generation guard fell back to the observation it had just rejected; the trust
path's probe was still unbounded and still feature-sniffed; a creation timeout
could orphan a real window; the kill bound did not cover a lawful cumulative
hold; the reservation was freed before the cleanup that follows it; and the
legacy seam still read the cached listing.
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
_THREAD = 14141
_USER = 8989
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


# ── P1-A: the terminal fallback never returns a rejected observation ──────


@pytest.mark.asyncio
async def test_a_read_raced_by_an_invalidation_is_never_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sample taken across an invalidation must be DISCARDED, not stamped.

    This test replaces a wave-14 test that PINNED THE DEFECT: it asserted the
    raced sample was published under its starting generation, on the (wrong)
    argument that "the read began after the caller's invalidation" implies "the
    read observed post-invalidation state". It does not — a kill landing WHILE
    ``_list_windows_direct`` awaits bumps the generation after the start stamp
    was taken, so the pre-kill sample got published under a stamp the caller's
    floor accepts. Matching the START and END generations is what closes it.
    """
    from cctelegram.tmux_manager import TmuxWindow, tmux_manager as real_tmux

    real_tmux._invalidate_list_cache()
    reads: list[int] = []

    async def _raced_then_settled() -> Any:
        reads.append(1)
        if len(reads) == 1:
            # A kill lands DURING this read: the sample is pre-kill.
            real_tmux._invalidate_list_cache()
            return [TmuxWindow(window_id="@doomed", window_name="w", cwd="/x")]
        # The retry samples the world AFTER the kill.
        return [TmuxWindow(window_id="@alive", window_name="w", cwd="/y")]

    monkeypatch.setattr(real_tmux, "_list_windows_direct", _raced_then_settled)

    found = await real_tmux.find_window_by_id("@doomed", fresh=True)

    assert found is None, (
        "a sample taken ACROSS an invalidation was accepted — that is the "
        "pre-kill corpse the generation guard exists to reject"
    )
    assert len(reads) >= 2, "the raced sample must be discarded and re-read"
    assert real_tmux._list_cache is not None
    assert "@doomed" not in real_tmux._list_cache, (
        "the raced sample must never be PUBLISHED for anyone else either"
    )


@pytest.mark.asyncio
async def test_an_unstable_listing_refuses_rather_than_return_a_corpse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On exhaustion a freshness-critical probe REFUSES.

    Returning the last rejected read — the wave-13 shape — hands back exactly
    the observation the guard rejected. A typed refusal is the honest answer.
    """
    from cctelegram.tmux_manager import TmuxWindow, tmux_manager as real_tmux

    real_tmux._invalidate_list_cache()

    async def _always_raced() -> Any:
        real_tmux._invalidate_list_cache()
        return [TmuxWindow(window_id="@doomed", window_name="w", cwd="/x")]

    monkeypatch.setattr(real_tmux, "_list_windows_direct", _always_raced)

    with pytest.raises(tmux_mod.ListingUnstable):
        await real_tmux.find_window_by_id("@doomed", fresh=True)

    assert real_tmux._list_cache is None, (
        "nothing may be published from a listing that never settled"
    )


@pytest.mark.asyncio
async def test_an_ordinary_cached_read_still_works_under_the_same_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1 Hz poller must not start raising because a kill is busy.

    Callers that demand no freshness get their observation back UNPUBLISHED —
    so a raced sample is never handed to anybody else, and the pollers keep
    working.
    """
    from cctelegram.tmux_manager import TmuxWindow, tmux_manager as real_tmux

    real_tmux._invalidate_list_cache()

    async def _always_raced() -> Any:
        real_tmux._invalidate_list_cache()
        return [TmuxWindow(window_id="@w", window_name="w", cwd="/x")]

    monkeypatch.setattr(real_tmux, "_list_windows_direct", _always_raced)

    listed = await real_tmux.list_windows()
    assert [w.window_id for w in listed] == ["@w"], "the poller still gets an answer"
    assert real_tmux._list_cache is None, (
        "…but a raced sample is never published for a fresh caller to accept"
    )


# ── P1-B: the trust probe is bounded, and the protocol is unconditional ───


@pytest.mark.asyncio
async def test_a_wedged_trust_revalidation_releases_the_lock_and_refuses() -> None:
    """The trust probe runs UNDER the lifecycle lock, so it must be bounded.

    Awaiting it raw meant ``LifecycleTimeout`` could never fire on the one seam
    whose whole job is to not hang — and the lock stayed held while it didn't.
    """
    user_data: dict[str, Any] = {}
    _seed(user_data)
    sessions = _Sessions()
    tmux = _Tmux(pane=_IDLE)
    probing = asyncio.Event()

    async def _wedged_listing() -> Any:
        probing.set()
        await asyncio.sleep(30)
        return []

    tmux.list_windows_fresh = _wedged_listing  # type: ignore[attr-defined]

    bounded_calls: list[str] = []

    async def _tiny_bound(coro: Any, *, what: str, **kwargs: Any) -> Any:
        del kwargs
        bounded_calls.append(what)
        try:
            return await asyncio.wait_for(coro, timeout=0.15)
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise tmux_mod.LifecycleTimeout("wedged") from e

    tmux._bounded_lifecycle = _tiny_bound  # type: ignore[attr-defined]

    flow = await _start(user_data, tmux=tmux, bot=_Bot(), sessions=sessions)
    assert flow is not None
    sessions.registered = True
    await asyncio.wait_for(probing.wait(), timeout=5)

    await asyncio.wait_for(trust_flow.teardown_thread(_USER, _THREAD), timeout=10)

    assert "trust bind existence probe" in bounded_calls, (
        "the trust revalidation probe runs UNDER the lifecycle lock but was "
        "awaited RAW — LifecycleTimeout can never fire there, so a wedged tmux "
        "holds the lock for every other window"
    )
    assert sessions.binds == [], "a wedged revalidation must never bind"
    assert not tmux.window_lifecycle_lock().locked(), (
        "the lifecycle lock must be RELEASED when the probe's bound expires"
    )


def test_the_trust_bind_does_not_feature_sniff_the_protocol() -> None:
    """The adoption protocol is UNCONDITIONAL.

    A ``getattr(mgr, "seam", None) is not None`` guard silently degrades to NO
    protocol for any manager missing a seam — precisely the failure a protocol
    exists to prevent. The unlocked fallback branch was the same bug in
    structural form.
    """
    source = Path(trust_flow.__file__).read_text()
    for sniffed in (
        '"window_lifecycle_lock", None',
        '"await_kill_settled", None',
        '"find_window_by_id", None',
        '"iter_thread_bindings", None)',
    ):
        assert sniffed not in source, (
            f"trust_flow still feature-sniffs the adoption protocol: {sniffed!r}"
        )


# ── P1-C: a creation timeout must never orphan a real window ──────────────


@pytest.mark.asyncio
async def test_a_window_created_after_the_timeout_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait_for`` cancels the WRAPPER; the worker keeps going.

    A window created after we gave up has no caller, no reservation and no
    owner. The done-callback on the inner future outlives our cancellation and
    reaps it.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()
    monkeypatch.setattr(tmux_mod, "LIFECYCLE_TMUX_TIMEOUT_S", 0.15)

    killed: list[str] = []

    async def _record_kill(window_id: str) -> bool:
        killed.append(window_id)
        return True

    monkeypatch.setattr(real_tmux, "kill_window", _record_kill)

    real_to_thread = asyncio.to_thread

    async def _late_create(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            await asyncio.sleep(0.4)  # finishes AFTER the bound expires
            return True, "created", "late", "@late-window"
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _late_create)

    ok, _msg, _n, _w = await real_tmux.create_window(
        "/tmp", window_name="w14", start_claude=False
    )
    assert ok is False, "the creation timed out"

    for _ in range(100):
        await asyncio.sleep(0.02)
        if killed:
            break
    assert killed == ["@late-window"], (
        "a window created AFTER its creation timed out must be reaped, not left "
        "floating unowned"
    )
    real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_a_verification_timeout_returns_the_real_window_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation is CONFIRMED by then, so an empty id would orphan the window."""
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()

    real_to_thread = asyncio.to_thread

    async def _to_thread(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            return True, "created", "w14", "@made-it"
        return await real_to_thread(func, *a, **kw)

    async def _wedged_listing() -> Any:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(real_tmux, "list_windows_fresh", _wedged_listing)
    monkeypatch.setattr(tmux_mod, "LIFECYCLE_TMUX_TIMEOUT_S", 0.15)

    ok, msg, _name, wid = await real_tmux.create_window(
        "/tmp", window_name="w14", start_claude=False
    )
    assert ok is False, "an unverified creation is not a success"
    assert wid == "@made-it", (
        "the caller must receive the REAL window id so it can settle the window "
        "that exists — an empty id orphans it"
    )
    assert msg == tmux_mod.CREATED_BUT_UNVERIFIED_MESSAGE, msg
    real_tmux.reset_kill_pending_for_tests()


# ── P1-E: the reservation outlives the entry, until cleanup settles ───────


@pytest.mark.asyncio
async def test_a_window_stays_unadoptable_until_its_cleanup_settles() -> None:
    """Freeing at entry death exposed the window DURING its own cleanup.

    A competitor adopted it, and the still-running cleanup then killed the new
    owner's window — a collateral kill.
    """
    from cctelegram.handlers.inbound_telegram import _list_unbound_windows

    trust_flow.reset_reservations_for_tests()
    sessions = _Sessions()

    class _ListingTmux(_Tmux):
        async def list_windows(self) -> Any:
            return [SimpleNamespace(window_id=_FAKE_WID, window_name="w", cwd="/x")]

    tmux = _ListingTmux(pane=_TRUST)
    trust_flow.reserve_window(_FAKE_WID, "tok-14")

    # The entry dies (an aborted creation) — but the cleanup has NOT run yet.
    trust_flow.orphan_reservations_for_token("tok-14")

    ids = {wid for wid, _, _ in await _list_unbound_windows(tmux, sessions)}
    assert _FAKE_WID not in ids, (
        "the window was offered for adoption while its cleanup was still to run"
    )

    # Only a settled disposition frees it.
    trust_flow.release_window_reservation(_FAKE_WID)
    ids_after = {wid for wid, _, _ in await _list_unbound_windows(tmux, sessions)}
    assert _FAKE_WID in ids_after, "…and it is adoptable once the cleanup settled"
    trust_flow.reset_reservations_for_tests()


@pytest.mark.asyncio
async def test_a_binding_that_lands_DURING_the_cleanup_still_spares_the_window() -> (
    None
):
    """The binding is re-read UNDER THE LIFECYCLE LOCK, immediately before the kill.

    The pre-existing check at the top of the cleanup only sees bindings that
    already existed when it was called. The dangerous case is a bind that COMMITS
    while the cleanup is in flight — an aborted creation's cleanup races exactly
    the adopters the reservation protects — and killing then destroys the new
    owner's window.

    Driven for real: the authority reports "unbound" on the FIRST read (the
    entry check) and "bound" on the re-read under the lock.
    """
    tmux = _Tmux(pane=_TRUST)
    reads: list[int] = []

    class _BindsMidCleanup(_Sessions):
        def iter_thread_bindings(self) -> Any:
            reads.append(1)
            if len(reads) == 1:
                return []  # the entry check sees a free window
            return [(_USER + 1, _THREAD + 1, _FAKE_WID)]  # …a competitor binds

    outcome = await trust_flow.cleanup_created_window(
        _FAKE_WID,
        "repo",
        tmux,
        reason="racing cleanup",
        session_mgr=_BindsMidCleanup(),
    )

    assert len(reads) >= 2, (
        "the binding must be RE-READ under the lifecycle lock, not just once "
        "before it — otherwise the check is check-then-act"
    )
    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.kill_calls == [], "a window bound mid-cleanup must never be killed"


# ── Self-audit: every reservation release is coupled to a settled disposition ─


def test_no_reservation_release_stands_alone() -> None:
    """Every ``release_window_reservation`` must sit next to a settlement.

    Review r14 P1-E established the rule; the self-audit found the legacy bind's
    REFUSAL arm still releasing a reservation while leaving the window alive and
    unowned — the orphan the reservation exists to prevent. This pins the rule
    across the module rather than at the one site that was wrong.

    A release is "coupled" when the surrounding lines mention a guarded cleanup
    (the window is being settled), a completed bind (the window has a real
    owner), or a settlement already performed by the helper being called.
    """
    source = Path(inbound_module.__file__).read_text().splitlines()
    hits = [i for i, line in enumerate(source) if "release_window_reservation(" in line]
    assert hits, "the audit found no release sites — check the search"

    coupled_markers = (
        "cleanup_created_window",
        "_abort_created_window_after_pending_owner_change",
        "settled disposition",
        "disposition has now settled",
        "disposition_settled",
    )
    for idx in hits:
        window = "\n".join(source[max(0, idx - 22) : idx + 3]).lower()
        assert any(marker.lower() in window for marker in coupled_markers), (
            f"{inbound_module.__file__}:{idx + 1} releases a reservation with no "
            "settled disposition nearby — a released reservation must always be "
            "paired with the window being killed, spared, or bound"
        )
