"""GH #65 wave 17 — one ownership exit, and fault injection at the real doors.

* P1 the created-but-unverified result carried a window id out through its
  OWN return, skipping the ownership transfer — so the reaper and the caller
  both owned it.
* P2 the legacy and directory fault-injection tests exercised helpers and
  substrings rather than the doors themselves.
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
_THREAD = 17171
_USER = 1717
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


# ── P1: EVERY id-bearing result transfers ownership ───────────────────────


@pytest.mark.asyncio
async def test_a_verification_timeout_hands_over_ownership_and_does_not_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The created-but-unverified result carries an id, so it TRANSFERS.

    The r16 shape returned that result from its own ``return`` statement, which
    skipped the TAKEN transition — so the ``finally`` recorded DECLINED and the
    reaper fired for the very window the caller was simultaneously being told to
    clean up. Two owners, two possible kills.
    """
    from cctelegram.tmux_manager import tmux_manager as real_tmux

    real_tmux.reset_kill_pending_for_tests()
    real_tmux.reset_lifecycle_lock_for_tests()

    reaped: list[str] = []

    async def _record_kill(window_id: str) -> bool:
        reaped.append(window_id)
        return True

    monkeypatch.setattr(real_tmux, "kill_window", _record_kill)

    real_to_thread = asyncio.to_thread

    async def _to_thread(func: Any, *a: Any, **kw: Any) -> Any:
        if getattr(func, "__name__", "") == "_create_and_start":
            return True, "created", "w17", "@unverified"
        return await real_to_thread(func, *a, **kw)

    async def _wedged_listing() -> Any:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(real_tmux, "adoption_listing", _wedged_listing)
    monkeypatch.setattr(tmux_mod, "LIFECYCLE_TMUX_TIMEOUT_S", 0.15)

    ok, msg, _name, wid = await real_tmux.create_window(
        "/tmp", window_name="w17", start_claude=False
    )

    assert ok is False, "an unverified creation is not a success"
    assert wid == "@unverified", "the caller receives the REAL id to settle"
    assert msg == tmux_mod.CREATED_BUT_UNVERIFIED_MESSAGE, msg

    for _ in range(30):
        await asyncio.sleep(0.01)
    assert reaped == [], (
        "the reaper ran for a window whose id was handed to the caller — that "
        "is TWO cleanup owners for one window"
    )
    real_tmux.reset_kill_pending_for_tests()


@pytest.mark.asyncio
async def test_the_caller_settles_an_unverified_window_exactly_once() -> None:
    """The other half of the contract: the single owner actually cleans.

    Ownership transferring is only correct if the receiving side settles the
    window — otherwise "exactly one owner" becomes "none".
    """
    trust_flow.reset_reservations_for_tests()
    tmux = _Tmux(pane=_IDLE)
    sessions = _Sessions()

    trust_flow.reserve_window("@unverified", "tok-17")
    outcome = await trust_flow.cleanup_created_window(
        "@unverified",
        "repo",
        tmux,
        reason="creation could not be verified",
        session_mgr=sessions,
    )
    trust_flow.release_window_reservation("@unverified")

    assert outcome is trust_flow.CleanupOutcome.KILLED
    assert tmux.kill_calls == ["@unverified"], "cleaned up exactly once"
    assert "@unverified" not in trust_flow.windows_owned_by_live_flows()
    trust_flow.reset_reservations_for_tests()


def test_every_id_bearing_exit_routes_through_the_ownership_transfer() -> None:
    """One exit is what makes the rule TOTAL rather than aspirational.

    The r16 rule was correct but not ENFORCED: a second ``return`` bypassed it,
    so the caller and the reaper both owned the same window. This walks the AST
    of ``create_window``'s own body — skipping the nested worker, whose returns
    become the ``result`` that flows through the transfer — and requires every
    return that could carry a window id to go through ``_transfer_ownership``.

    A literal empty string in the id position is the reaper's case and needs no
    transfer; anything else must transfer.
    """
    import ast

    tree = ast.parse(Path(tmux_mod.__file__).read_text())
    create = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_window"
    )

    def _own_returns(node: ast.AST) -> list[ast.Return]:
        """Returns belonging to THIS function, not to a nested one."""
        found: list[ast.Return] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                found.append(child)
            found.extend(_own_returns(child))
        return found

    offenders: list[str] = []
    for ret in _own_returns(create):
        value = ret.value
        if value is None:
            continue
        if isinstance(value, ast.Call) and "_transfer_ownership" in ast.unparse(
            value.func
        ):
            continue
        if isinstance(value, ast.Tuple) and len(value.elts) == 4:
            window_id = value.elts[3]
            if isinstance(window_id, ast.Constant) and window_id.value == "":
                # No id reaches the caller — the reaper owns this outcome.
                continue
        offenders.append(f"line {ret.lineno}: {ast.unparse(ret)[:70]}")

    assert not offenders, (
        "these returns in create_window can carry a window id WITHOUT going "
        "through _transfer_ownership, so the caller would be handed an id the "
        f"reaper also owns: {offenders}"
    )
