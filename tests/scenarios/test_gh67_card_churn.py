"""Scenario: GH #67 — the interactive-card churn loop (send → delete → resend).

The loop, verified on main:

  A. ``status_polling.update_status_message``'s detected branch republishes
     whenever the pane still shows an interactive UI and the route is off-mode.
  B. ``bot.handle_new_message``'s generic seam cleared the card for ANY parent
     block carrying ``has_interactive_surface`` — sound for an AUQ/EPM
     resolution, UNSOUND for a pane-detected gate (which has no transcript
     resolution event at all) and for a stale backlog block that PREDATES the
     surface.
  C. the clear pops ``_last_published_ui_hash``, so
  D. the next watchdog tick re-detects the unchanged pane and republishes —
     each delete flapping ``has_interactive_surface`` False and re-posting the
     phantom 🔔 notification card past its dwell.

The fix conditions the seam on the delivered block's RAW facts (gate veto,
matching-resolution parity, timestamp veto) derived UNDER the clear's own route
lock against the published surface's provenance (``surface_kind`` /
``surface_born_at``), and id-parity-gates the two OTHER AUQ teardown paths (the
explicit ``tool_result`` branch and the AFK late-answer conversion).

Black-box at the Telegram seam: real ``handle_new_message`` / real poller /
real render → fake tmux + fake bot. Test bodies seed substrate (panes, side
files, ledger rows) and read module state; they never monkeypatch handler
internals.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import md_capture, route_runtime, terminal_parser
from cctelegram.handlers import (
    attention,
    auq_ledger,
    auq_source,
    interactive_ui,
    pick_token,
    status_polling,
)
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.session_monitor import NewMessage, SessionInfo, SessionMonitor
from cctelegram.tmux_manager import tmux_manager as real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_SESSION_ID = "66666666-6666-4666-8666-666666666666"
_THREAD_ID = 55
_FIXTURES = Path(__file__).parent.parent / "cctelegram" / "fixtures"

_TOOL_INPUT_A: dict[str, Any] = {
    "questions": [
        {
            "question": "Which rollout lane should we take?",
            "header": "Rollout lane",
            "multiSelect": False,
            "options": [
                {"label": "A) Ship now", "description": "Ship the hotfix today."},
                {"label": "B) Bake first", "description": "Soak on canary first."},
            ],
        }
    ]
}
_TOOL_INPUT_C: dict[str, Any] = {
    "questions": [
        {
            "question": "Which database should the new service use?",
            "header": "Datastore",
            "multiSelect": False,
            "options": [
                {"label": "C) Postgres", "description": "Relational, boring, proven."},
                {"label": "D) SQLite", "description": "Single-file, embedded."},
            ],
        }
    ]
}


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _picker_pane(question: str = "Which rollout lane should we take?") -> str:
    """A live, pane-confirmed single-select AUQ picker."""
    return (
        "← ☐ Rollout lane  ✔ Submit →\n"
        f"{question}\n"
        "\n"
        "❯ 1. A) Ship now\n"
        "  2. B) Bake first\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )


def _picker_pane_c() -> str:
    return (
        "← ☐ Datastore  ✔ Submit →\n"
        "Which database should the new service use?\n"
        "\n"
        "❯ 1. C) Postgres\n"
        "  2. D) SQLite\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )


def _exitplan_pane() -> str:
    """A live ExitPlanMode plan-approval pane (NO PreToolUse side file)."""
    return (
        "  Would you like to proceed?\n"
        "  ─────────────────────────────────\n"
        "  Yes     No\n"
        "  ─────────────────────────────────\n"
        "  ctrl-g to edit in vim\n"
    )


def _gone_pane() -> str:
    """A non-interactive, idle pane (no picker, no anchors)."""
    return "> \n\nClaude is ready.\n"


@pytest.fixture
def gate_on():
    """Enable gate detection for the test body (root reset clears it after)."""
    terminal_parser.set_permission_prompts_enabled(True)
    yield
    terminal_parser.set_permission_prompts_enabled(False)


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Zero the off-mode pane-capture watchdog so "the next watchdog tick" is
    the next poll instead of ~10 s of real time. A cleared card leaves the route
    off-mode, and only a watchdog tick re-captures the pane."""
    monkeypatch.setattr(status_polling, "WATCHDOG_INTERVAL", 0.0)


def _side_file_path(session_id: str = _SESSION_ID) -> Path:
    return app_dir() / "auq_pending" / f"{session_id}.json"


def _write_side_file(
    tool_input: dict[str, Any],
    *,
    tool_use_id: str,
    session_id: str = _SESSION_ID,
) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _bind(scenario: ScenarioHarness, pane: str, *, name: str = "repo") -> str:
    wid = scenario.add_window(window_name=name, cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        _THREAD_ID, wid, display_name=name, cwd="/repo", session_id=_SESSION_ID
    )
    return wid


async def _render(scenario: ScenarioHarness, wid: str) -> bool:
    return bool(
        await interactive_ui.handle_interactive_ui(
            scenario.bot,
            scenario.user_id,
            wid,
            _THREAD_ID,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )
    )


async def _poll(scenario: ScenarioHarness, wid: str, n: int = 1) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot,
            user_id=scenario.user_id,
            window_id=wid,
            thread_id=_THREAD_ID,
        )


def _has_surface(scenario: ScenarioHarness) -> bool:
    return interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


def _meta(scenario: ScenarioHarness):
    return interactive_ui._interactive_msg_meta.get((scenario.user_id, _THREAD_ID))


def _born_at(scenario: ScenarioHarness) -> datetime:
    rec = _meta(scenario)
    assert rec is not None and rec.surface_born_at is not None
    return datetime.fromisoformat(rec.surface_born_at)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _now_iso(offset_s: float = 0.0) -> str:
    return _iso(datetime.now(UTC) + timedelta(seconds=offset_s))


_INTERACTIVE_CB_PREFIXES = ("aq:", "aqp:", "aqt:", "dcp:")


def _is_interactive_markup(markup: Any) -> bool:
    if markup is None:
        return False
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith(_INTERACTIVE_CB_PREFIXES):
                return True
    return False


def _interactive_sends(scenario: ScenarioHarness) -> list[Any]:
    """Every FRESH interactive-card publish (a new Telegram message)."""
    return [
        s
        for s in scenario.bot.sent
        if s.method == "send_message"
        and _is_interactive_markup(s.kwargs.get("reply_markup"))
    ]


def _deletes(scenario: ScenarioHarness) -> list[Any]:
    return [s for s in scenario.bot.sent if s.method == "delete_message"]


async def _parent_text(
    scenario: ScenarioHarness, text: str, *, timestamp: str | None
) -> None:
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text=text,
            content_type="text",
            role="assistant",
            timestamp=timestamp,
        ),
        scenario.bot,
    )


async def _parent_tool_result(
    scenario: ScenarioHarness,
    *,
    tool_name: str,
    tool_use_id: str | None,
    timestamp: str | None,
    text: str = "Answered.",
    tool_result_meta: dict[str, Any] | None = None,
) -> None:
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text=text,
            content_type="tool_result",
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            role="assistant",
            timestamp=timestamp,
            tool_result_meta=tool_result_meta,
        ),
        scenario.bot,
    )


async def _wait_until_lock_contended(lock: asyncio.Lock) -> None:
    """Yield until at least one OTHER task is queued on ``lock``."""
    for _ in range(200):
        if lock.locked() and getattr(lock, "_waiters", None):
            return
        await asyncio.sleep(0)
    raise AssertionError("no task ever queued on the route lock")


# ── 1. the gate veto kills the sustained churn ────────────────────────────


@pytest.mark.asyncio
async def test_gate_card_survives_parent_blocks_and_still_tombstones(
    scenario: ScenarioHarness, gate_on
) -> None:
    """A pane-detected Permission gate is NOT torn down by parent narration —
    and repeated blocks over N poll ticks produce ZERO additional interactive
    sends (the sustained-churn bound). The absent-streak tombstone still clears
    the card once the pane genuinely moves on."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)
    assert _has_surface(scenario)

    baseline_sends = len(_interactive_sends(scenario))
    baseline_deletes = len(_deletes(scenario))

    for i in range(3):
        await _parent_text(scenario, f"Running ruff pass {i}…", timestamp=_now_iso())
        await _poll(scenario, wid, 1)

    assert _has_surface(scenario), (
        "a pane-detected gate has no transcript resolution event — a parent "
        "block must never tear its card down"
    )
    assert len(_deletes(scenario)) == baseline_deletes, "no topic_delete churn"
    assert len(_interactive_sends(scenario)) == baseline_sends, (
        "no re-publish churn: the card was never cleared, so the poller's "
        "in-mode hash dedup holds"
    )

    # The pane moves on → the healthy 3/3 absent-streak tombstone still clears.
    scenario.tmux.set_pane(wid, _gone_pane())
    await _poll(scenario, wid, 5)
    assert not _has_surface(scenario), (
        "the absent-streak tombstone remains the gate card's clear path"
    )


@pytest.mark.asyncio
async def test_gate_card_forget_is_not_called_by_a_parent_block(
    scenario: ScenarioHarness, gate_on
) -> None:
    """The seam's ``forget_ask_tool_input`` is conditional now: a vetoed clear
    must leave the window's AUQ lifecycle state alone."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    assert await _render(scenario, wid)
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT_A, "toolu_A")
    interactive_ui._auq_context_posted[wid] = "toolu_A"

    await _parent_text(scenario, "Still working…", timestamp=_now_iso())

    assert interactive_ui._last_completed_ask_tool_input.get(wid) is not None
    assert interactive_ui._auq_context_posted.get(wid) == "toolu_A"


# ── 3. the timestamp veto ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_parent_block_preserves_auq_card_fresh_one_clears(
    scenario: ScenarioHarness,
) -> None:
    """A backlog block that PREDATES the surface proves nothing about it; a
    block newer than the surface's birth still tears it down."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    born = _born_at(scenario)

    await _parent_text(
        scenario,
        "…an A-era narration line",
        timestamp=_iso(born - timedelta(seconds=5)),
    )
    assert _has_surface(scenario), "a block older than the card must not clear it"

    await _parent_text(
        scenario, "Done — moving on.", timestamp=_iso(born + timedelta(seconds=5))
    )
    assert not _has_surface(scenario), "a fresh parent block still clears the card"


# ── 4. the matching-resolution bypass ─────────────────────────────────────


@pytest.mark.asyncio
async def test_auq_resolution_older_than_birth_still_clears(
    scenario: ScenarioHarness,
) -> None:
    """The slow-send case: ``surface_born_at`` is minted only AFTER the awaited
    Telegram send, so a terminal answer during a slow send yields a genuine
    tool_result whose transcript ts predates the stamp. Vetoing it would
    preserve an already-resolved card."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    born = _born_at(scenario)

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_iso(born - timedelta(seconds=5)),
    )
    assert not _has_surface(scenario)


# ── 5. surface replacement re-stamps the birth ────────────────────────────


@pytest.mark.asyncio
async def test_gate_to_auq_replacement_restamps_birth(
    scenario: ScenarioHarness, gate_on
) -> None:
    """A gate card refreshed in place into an AUQ is a NEW logical surface: a
    block newer than the ORIGINAL gate birth but older than the AUQ re-stamp
    does NOT clear it; a block newer than the re-stamp does."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    assert await _render(scenario, wid)
    gate_born = _born_at(scenario)
    assert _meta(scenario).surface_kind == "Permission"

    scenario.tmux.set_pane(wid, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    auq_born = _born_at(scenario)
    assert _meta(scenario).surface_kind == "AskUserQuestion"
    assert auq_born > gate_born, "a kind change must RE-STAMP surface_born_at"

    mid = gate_born + (auq_born - gate_born) / 2
    await _parent_text(scenario, "gate-era narration", timestamp=_iso(mid))
    assert _has_surface(scenario), (
        "a block from the REPLACED surface's era must not clear the replacement"
    )

    await _parent_text(
        scenario,
        "post-replacement narration",
        timestamp=_iso(auq_born + timedelta(seconds=1)),
    )
    assert not _has_surface(scenario)


# ── 6. no phantom 🔔 re-post behind a live gate ───────────────────────────


@pytest.mark.asyncio
async def test_notification_card_stays_dismissed_across_parent_blocks(
    scenario: ScenarioHarness, gate_on
) -> None:
    """Each churn delete flapped ``has_interactive_surface`` False and re-posted
    the generic "🔔 needs a decision" card past its dwell. With the card
    surviving, the dismissed decision card stays dismissed."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    await route_runtime.mark_inbound_sent((scenario.user_id, _THREAD_ID, wid))
    notify_dir = app_dir() / "notify_pending"
    notify_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (notify_dir / f"{_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "ts": time.time(),
                "window_key": f"{real_tmux.session_name}:{wid}",
                "generation": "gen-1",
                "kind": "permission",
            }
        )
    )
    await status_polling._consume_notification_signal(
        scenario.bot, scenario.user_id, _THREAD_ID, wid
    )
    key = (scenario.user_id, _THREAD_ID)
    assert attention._attention_state[key].state == "waiting"

    await _poll(scenario, wid, 2)
    assert _has_surface(scenario)
    assert attention._attention_state[key].state == "idle"

    decision_sends_before = len(
        [
            s
            for s in scenario.bot.sent
            if s.method == "send_message"
            and "needs a decision" in (s.kwargs.get("text") or "")
        ]
    )
    for i in range(3):
        await _parent_text(scenario, f"narration {i}", timestamp=_now_iso())
        await _poll(scenario, wid, 1)

    assert attention._attention_state[key].state == "idle", (
        "no phantom 🔔 re-post: the gate card never flapped"
    )
    decision_sends_after = len(
        [
            s
            for s in scenario.bot.sent
            if s.method == "send_message"
            and "needs a decision" in (s.kwargs.get("text") or "")
        ]
    )
    assert decision_sends_after == decision_sends_before


# ── 7. the accepted F2/F3 residual is bounded ─────────────────────────────


@pytest.mark.asyncio
async def test_external_clear_with_live_pane_republishes_exactly_once(
    scenario: ScenarioHarness, gate_on, fast_watchdog
) -> None:
    """The ``/esc``-style dispatcher clear pops the published hash without
    pane-absence proof. If the pane still shows the surface the watchdog
    re-raises ONCE, then ``_interactive_mode`` is set again and the in-mode hash
    dedup holds — the accepted residual cannot recreate sustained churn."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)
    baseline = len(_interactive_sends(scenario))

    # The unconditional clear every non-seam caller still uses.
    await interactive_ui.clear_interactive_msg(
        scenario.user_id, scenario.bot, _THREAD_ID, session_mgr=scenario.session_manager
    )
    await _poll(scenario, wid, 6)

    assert len(_interactive_sends(scenario)) == baseline + 1


# ── 7b. the stale AFK conversion ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_afk_result_does_not_convert_the_live_card(
    scenario: ScenarioHarness,
) -> None:
    """An AFK tool_result whose ``tool_use_id`` differs from the LIVE side
    file's id belongs to an older AUQ — it must not convert the newer live
    card."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_LIVE")
    assert await _render(scenario, wid)
    edits_before = len(
        [s for s in scenario.bot.sent if s.method == "edit_message_text"]
    )

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_OLD",
        timestamp=_now_iso(-120),
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert _has_surface(scenario), "the live card must survive a stale AFK result"
    assert _side_file_path().exists(), "the live AUQ's side file must survive"
    assert (
        len([s for s in scenario.bot.sent if s.method == "edit_message_text"])
        == edits_before
    ), "no ⏰ late-answer conversion happened"


@pytest.mark.asyncio
async def test_unknown_id_afk_result_still_converts(
    scenario: ScenarioHarness,
) -> None:
    """Unknown parity keeps today's behavior on the AUQ-kind card: the AFK
    result still converts the picker into the ⏰ card."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="")
    assert await _render(scenario, wid)

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id=None,
        timestamp=_now_iso(-120),
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert any(
        "Claude proceeded after ~60s" in (s.kwargs.get("text") or "")
        for s in scenario.bot.sent
        if s.method == "edit_message_text"
    )


# ── 7c. the stale EXPLICIT resolution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_explicit_resolution_spares_the_new_auq(
    scenario: ScenarioHarness,
) -> None:
    """An older AUQ's answer arriving while a NEWER AUQ is live must not unlink
    the new side file, must not release the window's ledger rows, and must not
    bypass the timestamp veto at the generic seam."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_NEW")
    assert await _render(scenario, wid)
    born = _born_at(scenario)

    auq_ledger.record(
        "rh:fpnew:1",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="ff" * 20,
        option_number=1,
        option_label="A) Ship now",
    )
    auq_ledger.record("rh:fpnew:1", state="dispatched")

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_OLD",
        timestamp=_iso(born - timedelta(seconds=30)),
    )

    assert _side_file_path().exists(), "the LIVE AUQ's side file must survive"
    row = auq_ledger.lookup("rh:fpnew:1")
    assert row is not None and row.state == "dispatched", (
        "a stale resolution must not release the live window's ledger rows"
    )
    assert _has_surface(scenario), (
        "a tool_result proven to belong to a DIFFERENT AUQ is not a matching "
        "resolution — it takes the ordinary (stale) timestamp veto"
    )


# ── 7d. the same-KIND replacement ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_first", [True, False], ids=["hook_first", "jsonl_cache"])
async def test_auq_a_to_auq_b_replacement_restamps_birth(
    scenario: ScenarioHarness, hook_first: bool
) -> None:
    """A consecutive AUQ-A → AUQ-B in-place replacement is a NEW logical
    surface (identity, not just kind): an A-era block newer than A's birth but
    older than B's re-stamp does NOT clear B.

    ``hook_first`` is the NORMAL production shape — the PreToolUse side file
    exists before the picker renders and Claude Code buffers the AUQ `tool_use`
    in JSONL until the answer, so ``_last_auq_tool_use_id`` is UNSET. Stamping
    the meta from that cache alone left both cards carrying `tool_use_id=None`,
    the identity re-stamp never fired, and A-era narration cleared B.
    """
    wid = _bind(scenario, _picker_pane())
    if not hook_first:
        interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT_A, "toolu_A")
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    born_a = _born_at(scenario)
    assert _meta(scenario).tool_use_id == "toolu_A", (
        "the published meta must carry the AUTHORITATIVE side-file identity"
    )

    scenario.tmux.set_pane(wid, _picker_pane_c())
    if not hook_first:
        interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT_C, "toolu_B")
    _write_side_file(_TOOL_INPUT_C, tool_use_id="toolu_B")
    assert await _render(scenario, wid)
    born_b = _born_at(scenario)
    assert _meta(scenario).tool_use_id == "toolu_B"
    assert born_b > born_a, "an AUQ identity change must RE-STAMP surface_born_at"

    mid = born_a + (born_b - born_a) / 2
    await _parent_text(scenario, "A-era narration", timestamp=_iso(mid))
    assert _has_surface(scenario)


# ── 7e. interleaving: the decision uses POST-replacement meta ─────────────


@pytest.mark.asyncio
async def test_clear_queued_behind_a_replacement_decides_on_the_new_surface(
    scenario: ScenarioHarness,
) -> None:
    """A queued generic clear that acquires the route lock AFTER a refresh
    replaced the surface must decide on the POST-replacement meta — a
    seam-side "read the kind, then await the clear" would have decided on the
    stale one."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    ikey = (scenario.user_id, _THREAD_ID)

    lock = interactive_ui._get_route_lock(scenario.user_id, _THREAD_ID)
    await lock.acquire()
    try:
        task = asyncio.create_task(
            _parent_text(scenario, "fresh narration", timestamp=_now_iso(60))
        )
        await _wait_until_lock_contended(lock)
        # The replacement lands while the clear waits for the lock.
        interactive_ui._interactive_msg_meta[ikey] = interactive_ui._InteractiveMsgMeta(
            msg_id=interactive_ui._interactive_msgs[ikey],
            window_id=wid,
            session_id=_SESSION_ID,
            tool_use_id=None,
            created_at=_now_iso(),
            surface_kind="Permission",
            surface_born_at=_now_iso(),
        )
    finally:
        lock.release()
    await task

    assert _has_surface(scenario), (
        "the locked re-derivation must see the gate that replaced the AUQ"
    )


@pytest.mark.asyncio
async def test_clear_queued_behind_a_gate_to_auq_replacement_proceeds(
    scenario: ScenarioHarness, gate_on
) -> None:
    """The mirror ordering: the queued clear was enqueued against a GATE (which
    would veto it) and acquires the lock after an AUQ replaced it. Deciding on
    the pre-replacement meta would wrongly PRESERVE the AUQ; the locked
    re-derivation clears it."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    assert await _render(scenario, wid)
    assert _meta(scenario).surface_kind == "Permission"
    ikey = (scenario.user_id, _THREAD_ID)

    lock = interactive_ui._get_route_lock(scenario.user_id, _THREAD_ID)
    await lock.acquire()
    try:
        task = asyncio.create_task(
            _parent_text(scenario, "Done — moving on.", timestamp=_now_iso())
        )
        await _wait_until_lock_contended(lock)
        # The AUQ replaces the gate while the clear waits for the lock; its
        # birth predates the block, so nothing vetoes.
        interactive_ui._interactive_msg_meta[ikey] = interactive_ui._InteractiveMsgMeta(
            msg_id=interactive_ui._interactive_msgs[ikey],
            window_id=wid,
            session_id=_SESSION_ID,
            tool_use_id="toolu_A",
            created_at=_now_iso(-60),
            surface_kind="AskUserQuestion",
            surface_born_at=_now_iso(-60),
        )
    finally:
        lock.release()
    await task

    assert not _has_surface(scenario), (
        "the gate veto belonged to the surface the refresh REPLACED — the "
        "locked re-derivation must decide on the AUQ that owns the card now"
    )


@pytest.mark.asyncio
async def test_resolution_bypass_refused_against_a_replacement(
    scenario: ScenarioHarness,
) -> None:
    """The r3 ordering: a tool_result that WOULD qualify as A's matching
    resolution arrives, a refresh replaces A with B before the lock is
    acquired, and the locked re-derivation REFUSES the bypass."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    born = _born_at(scenario)
    ikey = (scenario.user_id, _THREAD_ID)

    lock = interactive_ui._get_route_lock(scenario.user_id, _THREAD_ID)
    await lock.acquire()
    try:
        task = asyncio.create_task(
            _parent_tool_result(
                scenario,
                tool_name="AskUserQuestion",
                tool_use_id="toolu_A",
                timestamp=_iso(born - timedelta(seconds=30)),
            )
        )
        await _wait_until_lock_contended(lock)
        interactive_ui._interactive_msg_meta[ikey] = interactive_ui._InteractiveMsgMeta(
            msg_id=interactive_ui._interactive_msgs[ikey],
            window_id=wid,
            session_id=_SESSION_ID,
            tool_use_id=None,
            created_at=_now_iso(),
            surface_kind="ExitPlanMode",
            surface_born_at=_now_iso(),
        )
    finally:
        lock.release()
    await task

    assert _has_surface(scenario), (
        "B's identity wins: an AUQ result is not a resolution of an EPM surface"
    )


# ── 7e2 / 7e4 / 7e5. AFK behind a replacement → the NARROW cleanup ────────


async def _seed_auq_then_replacement(
    scenario: ScenarioHarness,
    *,
    replacement_pane: str,
    gate_flag: bool = False,
    hook_first: bool = True,
) -> str:
    """Publish AUQ-A's card (side file + ctx state), then replace it in place.

    ``hook_first`` (the default, and the NORMAL production shape) skips the
    ``remember_ask_tool_input`` preload: the JSONL `tool_use` is still buffered,
    so `_last_auq_tool_use_id` is unset, the ctx record carries NO tool_use_id
    and the ctx marker is `pretool:<source_fingerprint[:16]>`.
    """
    wid = _bind(scenario, _picker_pane())
    if not hook_first:
        interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT_A, "toolu_A")
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)
    marker = interactive_ui._auq_context_posted.get(wid)
    assert marker is not None, (
        "AUQ-A's 📋 context card must be posted for the retirement assertions"
    )
    if hook_first:
        recovery = auq_source.read_side_file_for_recovery(_SESSION_ID)
        assert recovery is not None
        assert marker == f"pretool:{recovery.source_fingerprint[:16]}", (
            "the hook-first marker is keyed on the side file's SOURCE "
            "FINGERPRINT, not on any tool_use_id — the shape the narrow "
            "cleanup's attribution must recognise"
        )
        assert interactive_ui._auq_context_msgs[wid].tool_use_id is None
    if gate_flag:
        terminal_parser.set_permission_prompts_enabled(True)
    scenario.tmux.set_pane(wid, replacement_pane)
    assert await _render(scenario, wid)
    return wid


@pytest.mark.asyncio
async def test_afk_behind_epm_replacement_does_only_the_narrow_cleanup(
    scenario: ScenarioHarness,
) -> None:
    """An AFK result for AUQ-A arriving after an EPM replaced A's card in place
    converts NOTHING and performs ONLY the narrow identity-guarded cleanup:
    A's side file is unlinked, the ledger rows are untouched,
    ``forget_ask_tool_input`` is NOT called (its MessageDisplay teardown would
    delete the successor EPM's plan-body dedup marker), and the generic seam
    that runs next does not clear the replacement."""
    wid = await _seed_auq_then_replacement(scenario, replacement_pane=_exitplan_pane())
    assert _meta(scenario).surface_kind == "ExitPlanMode"
    born = _born_at(scenario)

    auq_ledger.record(
        "rh:fpA:1",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="aa" * 20,
        option_number=1,
        option_label="A) Ship now",
    )
    auq_ledger.record("rh:fpA:1", state="dispatched")
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    md_capture.record_epm_plan_shown_live(
        _SESSION_ID, norm_hash="planhash67", shown_at=time.time()
    )

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_iso(born - timedelta(seconds=30)),
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert _has_surface(scenario), "the EPM replacement is never popped or converted"
    assert not _side_file_path().exists(), (
        "A's side file MUST be unlinked — a retained one holds the replacement "
        "card's absent-streak tombstone open and arms the startup reconciler"
    )
    row = auq_ledger.lookup("rh:fpA:1")
    assert row is not None and row.state == "dispatched", (
        "the narrow cleanup never releases the window's ledger rows"
    )
    assert md_capture.was_epm_plan_shown_live(_SESSION_ID, "planhash67") is True, (
        "forget_ask_tool_input must NOT run — it would reap the successor EPM's "
        "plan-body dedup marker"
    )
    assert auq_source.side_file_live_for_window(wid) is False, (
        "continuation (i): the replacement card's tombstone is not held open"
    )


@pytest.mark.asyncio
async def test_non_afk_resolution_behind_decision_replacement_spares_dcp_rows(
    scenario: ScenarioHarness,
) -> None:
    """r6 P1-1 + the 7e5 ledger-residual pin: a NON-AFK AUQ-A tool_result behind
    a replacement gate leaves the gate's ledger rows and the session's
    MessageDisplay markers untouched (only A's side file is unlinked) — and A's
    OWN dispatched row stays un-released, the disclosed ≤24h degradation."""
    wid = await _seed_auq_then_replacement(
        scenario,
        replacement_pane=_load("permission_bash_v2.1.190.txt"),
        gate_flag=True,
    )
    assert _meta(scenario).surface_kind == "Permission"

    for key, label in (("rh:fpA:1", "A) Ship now"), ("decision:fpB:1", "Yes")):
        auq_ledger.record(
            key,
            state="accepted",
            user_id=scenario.user_id,
            window_id=wid,
            full_fingerprint="bb" * 20,
            option_number=1,
            option_label=label,
        )
        auq_ledger.record(key, state="dispatched")
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    md_capture.record_epm_plan_shown_live(
        _SESSION_ID, norm_hash="planhash67b", shown_at=time.time()
    )

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-30),
    )

    assert _has_surface(scenario), "the gate veto protects the replacement card"
    assert not _side_file_path().exists()
    dcp_row = auq_ledger.lookup("decision:fpB:1")
    assert dcp_row is not None and dcp_row.state == "dispatched", (
        "a window-scoped release would unmask the replacement Decision's "
        "dcp: single-use rows"
    )
    a_row = auq_ledger.lookup("rh:fpA:1")
    assert a_row is not None and a_row.state == "dispatched", (
        "DISCLOSED residual: A's rows stay un-released, so a same-day "
        "byte-identical AUQ answers 'Action already received' until the 24h "
        "read cutoff"
    )
    assert md_capture.was_epm_plan_shown_live(_SESSION_ID, "planhash67b") is True


@pytest.mark.asyncio
async def test_afk_behind_gate_replacement_does_only_the_narrow_cleanup(
    scenario: ScenarioHarness,
) -> None:
    """The 7e2 sibling sub-shape: AFK-behind-GATE. The gate card is never popped
    or converted, only A's own state is retired, and the generic seam that runs
    next is stopped by the GATE veto (not by the timestamp veto), so a FRESH
    block ts can't clear it either."""
    wid = await _seed_auq_then_replacement(
        scenario,
        replacement_pane=_load("permission_bash_v2.1.190.txt"),
        gate_flag=True,
    )
    assert _meta(scenario).surface_kind == "Permission"
    edits_before = len(
        [s for s in scenario.bot.sent if s.method == "edit_message_text"]
    )

    auq_ledger.record(
        "decision:fpGate:1",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="cc" * 20,
        option_number=1,
        option_label="Yes",
    )
    auq_ledger.record("decision:fpGate:1", state="dispatched")

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(),  # FRESH — only the gate veto can stop this
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert _has_surface(scenario), "a gate card is never converted or cleared"
    assert (
        len([s for s in scenario.bot.sent if s.method == "edit_message_text"])
        == edits_before
    ), "no ⏰ late-answer conversion of the gate card"
    assert not _side_file_path().exists()
    row = auq_ledger.lookup("decision:fpGate:1")
    assert row is not None and row.state == "dispatched"


@pytest.mark.asyncio
async def test_non_afk_resolution_behind_epm_replacement_spares_the_plan_marker(
    scenario: ScenarioHarness,
) -> None:
    """The 7e2 sibling sub-shape: a NON-AFK AUQ-A tool_result behind an EPM-B
    leaves B equally intact — B's plan-body dedup marker survives (no
    ``forget_ask_tool_input``) and the generic seam's KIND-MATCH refuses the
    resolution bypass, so the stale block takes the timestamp veto."""
    wid = await _seed_auq_then_replacement(scenario, replacement_pane=_exitplan_pane())
    assert _meta(scenario).surface_kind == "ExitPlanMode"
    born = _born_at(scenario)
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    md_capture.record_epm_plan_shown_live(
        _SESSION_ID, norm_hash="planhash67c", shown_at=time.time()
    )
    auq_ledger.record(
        "rh:fpA:1",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="dd" * 20,
        option_number=1,
        option_label="A) Ship now",
    )
    auq_ledger.record("rh:fpA:1", state="dispatched")

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_iso(born - timedelta(seconds=30)),
    )

    assert _has_surface(scenario), "the EPM replacement survives the stale block"
    assert not _side_file_path().exists()
    assert md_capture.was_epm_plan_shown_live(_SESSION_ID, "planhash67c") is True
    row = auq_ledger.lookup("rh:fpA:1")
    assert row is not None and row.state == "dispatched"


@pytest.mark.asyncio
async def test_narrow_cleanup_runs_with_the_side_file_already_gone(
    scenario: ScenarioHarness,
) -> None:
    """The cleanup licence is the SAME positive proof the parity gate uses, not
    a stricter side-file-specific one. With A's side file already unlinked (an
    earlier teardown, the startup GC, a `/clear` race) the identity still
    resolves from the published meta — so the cleanup must still retire A's
    replay + context state instead of reporting ``narrow`` and cleaning
    nothing, which would leave that state to poison the next AUQ."""
    wid = await _seed_auq_then_replacement(
        scenario, replacement_pane=_exitplan_pane(), hook_first=False
    )
    _side_file_path().unlink()
    assert (
        interactive_ui._interactive_msg_meta[(scenario.user_id, _THREAD_ID)].tool_use_id
        == "toolu_A"
    ), "the identity survives on the published meta"
    assert interactive_ui._auq_context_posted.get(wid) is not None

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-30),
    )

    assert _has_surface(scenario), "the EPM replacement is still protected"
    assert interactive_ui._last_auq_tool_use_id.get(wid) is None
    assert interactive_ui._last_completed_ask_tool_input.get(wid) is None
    assert interactive_ui._auq_context_posted.get(wid) is None
    assert interactive_ui._auq_context_msgs.get(wid) is None


@pytest.mark.asyncio
async def test_restart_reconciler_after_narrow_cleanup_releases_nothing(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """Continuation (ii): a restart after the narrow cleanup finds NO A side
    file, so the startup reconciler's positive-proof branch can't fire and the
    replacement Decision's ``dcp:`` rows survive the restart.

    Drives the REAL reconciler (``SessionMonitor._hydrate_ask_tool_input_cache``)
    over a JSONL that carries A's tool_use AND its tool_result — the exact shape
    that WOULD license ``release_window`` if A's side file were still there.
    """
    wid = await _seed_auq_then_replacement(
        scenario,
        replacement_pane=_load("permission_bash_v2.1.190.txt"),
        gate_flag=True,
    )
    auq_ledger.record(
        "decision:fpB:1",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="ee" * 20,
        option_number=1,
        option_label="Yes",
    )
    auq_ledger.record("decision:fpB:1", state="dispatched")

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-30),
    )
    assert not _side_file_path().exists()

    jsonl = tmp_path / f"{_SESSION_ID}.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {
                    "type": "assistant",
                    "sessionId": _SESSION_ID,
                    "cwd": "/repo",
                    "timestamp": "2026-08-24T12:00:00.000Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_A",
                                "name": "AskUserQuestion",
                                "input": _TOOL_INPUT_A,
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "sessionId": _SESSION_ID,
                    "cwd": "/repo",
                    "timestamp": "2026-08-24T12:00:30.000Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_A",
                                "content": "Answered.",
                            }
                        ]
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )

    async def _scan(_active_ids=None):
        return [SessionInfo(session_id=_SESSION_ID, file_path=jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    await monitor._hydrate_ask_tool_input_cache({wid: _SESSION_ID})

    row = auq_ledger.lookup("decision:fpB:1")
    assert row is not None and row.state == "dispatched", (
        "with A's side file already retired the reconciler has no positive "
        "proof to act on — it must not broadly release the window"
    )


@pytest.mark.asyncio
async def test_unreleased_row_answers_action_already_received(
    scenario: ScenarioHarness,
) -> None:
    """7e5, the disclosed cost asserted through the REAL callback: A's
    dispatched row is never released by the narrow cleanup, so a same-day
    byte-identical AUQ re-tap answers "Action already received" instead of
    dispatching. Bounded by the ledger's 24h read retention."""
    wid = await _seed_auq_then_replacement(scenario, replacement_pane=_exitplan_pane())
    fingerprint = "ff" * 20
    entry = pick_token.PickTokenEntry(
        window_id=wid,
        user_id=scenario.user_id,
        thread_id=_THREAD_ID,
        fingerprint=fingerprint,
        option_number=1,
        option_label="A) Ship now",
        is_review_submit=False,
        expires_at=time.monotonic() + 300.0,
        source_kind="pane",
        source_fingerprint="sfp",
        row_generation=1,
    )
    token = pick_token.mint(entry)
    route_hash = auq_ledger.make_route_hash(scenario.user_id, _THREAD_ID, wid)
    ledger_key = auq_ledger.make_ledger_key(route_hash, fingerprint[:8], 1)
    auq_ledger.record(
        ledger_key,
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint=fingerprint,
        option_number=1,
        option_label="A) Ship now",
    )
    auq_ledger.record(ledger_key, state="dispatched")

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-30),
    )

    update = make_update_callback(
        f"{CB_ASK_PICK}{route_hash}:{fingerprint[:8]}:1:{token}",
        thread_id=_THREAD_ID,
        user_id=scenario.user_id,
    )
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    assert update.callback_query.answer.await_args.args[0] == (
        "Action already received: A) Ship now"
    ), "the un-released dispatched row is the DISCLOSED cost of the narrow cleanup"


@pytest.mark.asyncio
async def test_unknown_parity_behind_a_replacement_cleans_nothing(
    scenario: ScenarioHarness,
) -> None:
    """Continuation (iii): with id-parity UNKNOWN nothing is unlinked and
    nothing converts — the DELIBERATE fail-closed branch protecting the
    replacement card."""
    wid = _bind(scenario, _picker_pane())
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT_A, "toolu_A")
    _write_side_file(_TOOL_INPUT_A, tool_use_id="")
    assert await _render(scenario, wid)
    scenario.tmux.set_pane(wid, _exitplan_pane())
    assert await _render(scenario, wid)
    assert _meta(scenario).surface_kind == "ExitPlanMode"

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-120),
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert _has_surface(scenario)
    assert _side_file_path().exists(), (
        "unknown parity cleans NOTHING — the side file is not ours to retire"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_first", [True, False], ids=["hook_first", "jsonl_cache"])
async def test_narrow_cleanup_retires_a_s_replay_and_context_state(
    scenario: ScenarioHarness, hook_first: bool
) -> None:
    """7e4: after the narrow cleanup a later AUQ-C must not resolve through A's
    stale replay caches, must get its OWN 📋 context card, and must never
    upgrade A's historical context message with C's text.

    Both marker shapes: the hook-first `pretool:<source_fingerprint>` (the
    normal production one, which carries no tool_use_id anywhere) and the
    JSONL-cache `<tool_use_id>`.
    """
    wid = await _seed_auq_then_replacement(
        scenario, replacement_pane=_exitplan_pane(), hook_first=hook_first
    )
    a_ctx_msg_ids = interactive_ui._auq_context_msgs[wid].message_ids

    await _parent_tool_result(
        scenario,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_A",
        timestamp=_now_iso(-120),
        text="No response after 60s — the user may be away from keyboard.",
        tool_result_meta={"answers": {}},
    )

    assert interactive_ui._last_completed_ask_tool_input.get(wid) is None
    assert interactive_ui._last_auq_tool_use_id.get(wid) is None
    assert interactive_ui._auq_context_posted.get(wid) is None, (
        "A's surviving marker would SUPPRESS a later AUQ-C's 📋 details card"
    )
    assert interactive_ui._auq_context_msgs.get(wid) is None, (
        "A's surviving record would let a later AUQ-C UPGRADE A's message in "
        "place with C's text (the permanently-wrong-content case)"
    )

    # AUQ-C renders on the same window with no hook record of A left.
    edits_before = {
        s.kwargs["message_id"]
        for s in scenario.bot.sent
        if s.method == "edit_message_text"
    }
    scenario.tmux.set_pane(wid, _picker_pane_c())
    _write_side_file(_TOOL_INPUT_C, tool_use_id="toolu_C")
    assert await _render(scenario, wid)

    assert interactive_ui._auq_context_posted.get(wid) is not None, (
        "AUQ-C must receive its OWN context card"
    )
    c_ctx = interactive_ui._auq_context_msgs.get(wid)
    assert c_ctx is not None and c_ctx.message_ids != a_ctx_msg_ids
    edits_after = {
        s.kwargs["message_id"]
        for s in scenario.bot.sent
        if s.method == "edit_message_text"
    }
    assert not (set(a_ctx_msg_ids) & (edits_after - edits_before)), (
        "A's historical context message must never be edited with C's text"
    )


# ── 7e3. the forget runs INSIDE the locked transaction ────────────────────


@pytest.mark.asyncio
async def test_successor_side_file_written_during_phase2_survives(
    scenario: ScenarioHarness,
) -> None:
    """r3 P2-3: the conditional forget moved inside the locked Phase-1. A
    successor surface published in the lock-release window (here: while the
    clear is doing its Telegram delete, outside the lock) keeps its side file —
    pre-fix the seam's post-await forget unlinked it."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)

    original_delete = scenario.bot.delete_message

    async def _delete_then_publish_successor(*, chat_id: int, message_id: int):
        # The poller's successor publish lands here — AFTER the locked Phase-1
        # (state already dropped, forget already run) and BEFORE the seam
        # returns, which is exactly where the pre-fix forget struck.
        _write_side_file(_TOOL_INPUT_C, tool_use_id="toolu_SUCCESSOR")
        return await original_delete(chat_id=chat_id, message_id=message_id)

    scenario.bot.delete_message = _delete_then_publish_successor  # type: ignore[method-assign]
    try:
        await _parent_text(scenario, "Done — moving on.", timestamp=_now_iso())
    finally:
        scenario.bot.delete_message = original_delete  # type: ignore[method-assign]

    assert _side_file_path().exists(), (
        "the successor's side file must survive the old block's teardown"
    )
    assert auq_source.peek_side_file_tool_use_id(_SESSION_ID) == "toolu_SUCCESSOR"


# ── 7e6. the EPM same-kind residual is BOUNDED ────────────────────────────


@pytest.mark.asyncio
async def test_epm_backlog_drain_costs_one_clear_and_one_republish(
    scenario: ScenarioHarness, fast_watchdog
) -> None:
    """The disclosed EPM same-kind residual: a stale A-era block can clear B
    (EPM has no per-instance id), but every republish creates FRESH meta whose
    birth postdates the whole backlog — so each qualifying block costs AT MOST
    one clear + one re-raise and sustained churn is structurally impossible."""
    wid = _bind(scenario, _exitplan_pane())
    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)
    born = _born_at(scenario)
    backlog_ts = _iso(born + timedelta(milliseconds=1))

    sends_before = len(_interactive_sends(scenario))
    deletes_before = len(_deletes(scenario))

    await _parent_text(scenario, "A-era block 1", timestamp=backlog_ts)
    assert not _has_surface(scenario), (
        "the first qualifying block clears (today's behavior)"
    )
    await _poll(scenario, wid, 2)
    assert _has_surface(scenario), "the watchdog re-raises the still-live EPM"

    for i in range(2, 6):
        await _parent_text(scenario, f"A-era block {i}", timestamp=backlog_ts)
        await _poll(scenario, wid, 1)

    assert _has_surface(scenario), (
        "the re-published card's FRESH birth vetoes every remaining older block"
    )
    assert len(_deletes(scenario)) == deletes_before + 1
    assert len(_interactive_sends(scenario)) == sends_before + 1


# ── 7f. the timestamp-less stream degrades honestly ───────────────────────


@pytest.mark.asyncio
async def test_timestampless_blocks_degrade_to_todays_behavior_with_a_warning(
    scenario: ScenarioHarness, caplog
) -> None:
    """The honest fail-open bound: a SUSTAINED stream of timestamp-less parent
    blocks recreates per-block clears for a non-gate surface. Every real
    transcript entry carries a timestamp (pinned by the plumbing unit test), so
    this shape requires a parser regression — which the seam WARNING surfaces
    once per route."""
    wid = _bind(scenario, _picker_pane())
    _write_side_file(_TOOL_INPUT_A, tool_use_id="toolu_A")
    assert await _render(scenario, wid)

    with caplog.at_level("WARNING", logger="cctelegram.bot"):
        await _parent_text(scenario, "no timestamp 1", timestamp=None)
        assert not _has_surface(scenario)
        await _poll(scenario, wid, 2)
        assert _has_surface(scenario), "the watchdog re-raises the still-live picker"
        await _parent_text(scenario, "no timestamp 2", timestamp=None)
        assert not _has_surface(scenario)

    warnings = [r for r in caplog.records if "timestamp=None" in r.getMessage()]
    assert len(warnings) == 1, "one WARNING per route, not per block"
