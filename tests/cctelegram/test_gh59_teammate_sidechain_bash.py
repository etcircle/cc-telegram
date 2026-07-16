"""GH #59 — a teammate's OWN ``run_in_background`` Bash launch is sidechain-only.

The T1.2 structured background-Bash lane reads only the PARENT transcript, and
the sidechain tailing lane never looked at ``tool_result_meta``, so a bound
teammate that launches a background bash (e.g. a long pytest suite) then PARKS
went typing/🟡 dark for the whole post-park run — the bash ran unseen while the
teammate stem key was (correctly) tombstoned by the park (GH #46).

The fix records the SAME bare bash key from a run-state-authoritative teammate
sidechain's parsed entries (the ``feed_run_state`` authority — unregistered OR
registered-bound), inside the EXISTING ``feed_run_state`` tick block in
``_track_and_emit_sidechain_file``. The launch id == the ``<task-notification>``
close id, so the EXISTING parent queue-op close lane + the 2 h ``is_background``
TTL close it; the teammate PARK closes ONLY the stem key, so the bash key
deliberately survives the park.

These pins cover: T1 launch recording (incl. the isolated result-only recovery
shape), T2 the four authority-policy cases through the production monitor path,
T3 park-survival + parent-close via route_runtime primitives, T6 the disjointness
guarantees, and T7 the disclosed truncation-replay residual.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import BgDoneSource, RunState
from cctelegram.session_monitor import (
    SessionInfo,
    SessionMonitor,
    TrackedSession,
    _TeammateRec,
)

PARENT = "parent-sid"
# The incident sidechain stem (== fixture agentId): normalized key drops the
# ``agent-`` prefix → ``avis2-backend-7041d9b743d26f2e`` = ``a`` + the teammate
# name ``vis2-backend`` + ``-`` + a 16-hex residual.
STEM = "avis2-backend-7041d9b743d26f2e"
NAME = "vis2-backend"
BASH_KEY = "bgbnvxcbx"

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_entries() -> list[dict]:
    """The REAL teammate-sidechain background-Bash launch batch (CC 2.1.211):
    text → Bash tool_use (run_in_background) → tool_result carrying the
    entry-level ``toolUseResult.backgroundTaskId`` → thinking."""
    return [
        json.loads(line)
        for line in (_FIXTURES / "teammate_sidechain_bash_v2.1.211.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]


@pytest.fixture
def monitor(tmp_path):
    return SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )


def _setup_parent(monitor, tmp_path, parent_sid: str = PARENT):
    proj_dir = tmp_path / "projects" / "-tmp-fake"
    proj_dir.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj_dir / f"{parent_sid}.jsonl"
    if not parent_jsonl.exists():
        parent_jsonl.write_text("")
    sub_dir = proj_dir / parent_sid / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    monitor.state.update_session(
        TrackedSession(
            session_id=parent_sid,
            file_path=str(parent_jsonl),
            last_byte_offset=parent_jsonl.stat().st_size,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=parent_sid, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    return parent_jsonl, sub_dir


def _append_entries(path: Path, entries: list[dict]) -> None:
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def _register_and_load(monitor, sc: Path, entries: list[dict]):
    """Register the sidechain at EOF (skip history), append ``entries``, poll,
    and return the drained ``ParentSidechainActivity`` for PARENT (or None)."""
    sc.parent.mkdir(parents=True, exist_ok=True)
    if not sc.exists():
        sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    _append_entries(sc, entries)
    await monitor.check_sidechain_updates({PARENT})
    return monitor.pop_sidechain_activity().get(PARENT)


# ── T1: the launch is recorded (unregistered legacy path + recovery shape) ────


@pytest.mark.asyncio
async def test_t1_sidechain_bash_launch_recorded_from_fixture(
    monitor, tmp_path, caplog
):
    """The REAL in-order launch batch on an UNREGISTERED (legacy, feed_run_state)
    teammate sidechain → the bare ``bgbnvxcbx`` key in ``.launched``; INFO logged.

    On-disk order is tool_use→tool_result yielding ``tool_name='Bash'`` (r1 P3-b —
    NOT the isolated result-only shape, which is the separate recovery pin below).
    """
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    with caplog.at_level("INFO"):
        activity = await _register_and_load(monitor, sc, _fixture_entries())
    assert activity is not None
    assert BASH_KEY in activity.launched
    assert any(
        "teammate-sidechain" in r.message.lower() and BASH_KEY in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_t1b_isolated_result_only_recovery_shape_records(monitor, tmp_path):
    """r1 P3-b: an ISOLATED bash tool_result (the result-before-use recovery
    shape — parses ``tool_name=None`` with the meta intact) still mints the key,
    because the scan admits ``tool_name in (None, "Bash")``."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    # entries[2] is the standalone tool_result carrying backgroundTaskId.
    result_only = [_fixture_entries()[2]]
    activity = await _register_and_load(monitor, sc, result_only)
    assert activity is not None
    assert BASH_KEY in activity.launched


# ── T2: the gating policy follows the feed_run_state authority ────────────────


@pytest.mark.asyncio
async def test_t2a_unregistered_teammate_records(monitor, tmp_path):
    """(a) UNREGISTERED teammate (``rec is None`` — the incident shape) →
    feed_run_state True (legacy) → launch RECORDED."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    activity = await _register_and_load(monitor, sc, _fixture_entries())
    assert activity is not None
    assert BASH_KEY in activity.launched


@pytest.mark.asyncio
async def test_t2b_registered_bound_records(monitor, tmp_path):
    """(b) registered + BOUND ``current_key`` == this stem → feed_run_state True
    (the ONE feeder) → launch RECORDED."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    # A registered rec BOUND to this stem — arbitration skips a bound rec, and
    # _teammate_feed_run_state returns True for key == current_key.
    monitor._teammate_registry[PARENT] = {
        NAME: _TeammateRec(
            name=NAME,
            teammate_id=f"{NAME}@team",
            spawn_generation=1,
            spawned_ts=0.0,
            current_key=STEM,
        )
    }
    activity = await _register_and_load(monitor, sc, _fixture_entries())
    assert activity is not None
    assert BASH_KEY in activity.launched


@pytest.mark.asyncio
async def test_t2c_registered_unbound_ambiguous_is_dark(monitor, tmp_path):
    """(c) registered + UNBOUND (sticky-ambiguous) candidate → feed_run_state
    False → launched EMPTY (dark; the item-3 strand-pin)."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    # A registered rec that is UNBOUND and sticky-ambiguous — arbitration freezes
    # it (never binds) and _teammate_feed_run_state returns False.
    monitor._teammate_registry[PARENT] = {
        NAME: _TeammateRec(
            name=NAME,
            teammate_id=f"{NAME}@team",
            spawn_generation=1,
            spawned_ts=0.0,
            current_key=None,
            ambiguous=True,
        )
    }
    activity = await _register_and_load(monitor, sc, _fixture_entries())
    launched = activity.launched if activity is not None else set()
    assert BASH_KEY not in launched


@pytest.mark.asyncio
async def test_t2d_workflow_display_loop_is_dark(monitor, tmp_path):
    """(d) the Workflow display loop calls _track_and_emit_sidechain_file with
    feed_run_state=False → the launch extraction (inside the feed_run_state tick
    block) never runs → launched EMPTY."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    sc.write_text("")
    tracking_key = f"sub:{PARENT}:wf_run1:{sc.stem}"
    # First call registers at EOF; append then call again — mirroring the
    # production wf_dir loop, which always passes feed_run_state=False.
    await monitor._track_and_emit_sidechain_file(
        parent_session_id=PARENT,
        sc_file=sc,
        tracking_key=tracking_key,
        new_messages=[],
        feed_run_state=False,
    )
    _append_entries(sc, _fixture_entries())
    await monitor._track_and_emit_sidechain_file(
        parent_session_id=PARENT,
        sc_file=sc,
        tracking_key=tracking_key,
        new_messages=[],
        feed_run_state=False,
    )
    activity = monitor.pop_sidechain_activity().get(PARENT)
    launched = activity.launched if activity is not None else set()
    assert BASH_KEY not in launched


# ── T3: park-survival + parent-close, via route_runtime primitives ────────────

ROUTE: route_runtime.Route = (1, 42, "@6")


@pytest.fixture(autouse=True)
def _reset_route_runtime():
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


async def _idle_route(end_ts: float = 100.0) -> None:
    await route_runtime.ingest_transcript_event(
        ROUTE,
        route_runtime.TranscriptLifecycleEvent(
            role="assistant",
            block_type="text",
            tool_use_id=None,
            tool_name=None,
            stop_reason="end_turn",
            timestamp=end_ts,
        ),
    )


@pytest.mark.asyncio
async def test_t3_bash_key_survives_park_and_dies_on_parent_close():
    await _idle_route(end_ts=100.0)
    # Both the teammate stem key and the bash key are live (projected RUNNING).
    await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, STEM)
    await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, BASH_KEY)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert set(snap.background_agents) == {STEM, BASH_KEY}

    # The teammate PARK closes ONLY the stem key (GH #46) — the bash key SURVIVES.
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, STEM, source=BgDoneSource.TEAMMATE, end_turn_ts_unparseable=True
    )
    assert snap.run_state is RunState.RUNNING  # still typing on the bash key
    assert snap.background_agents == (BASH_KEY,)

    # The parent queue-op <task-notification> close tombstones the bash key.
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, BASH_KEY, source=BgDoneSource.PARENT
    )
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.background_agents == ()


# ── T6: disjointness — the tool_name scope + the meta-shape guarantees ────────


@pytest.mark.asyncio
async def test_t6_non_bash_result_with_bg_field_is_refused_by_scope(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """A KNOWN non-Bash tool_result that nevertheless carries a
    ``backgroundTaskId`` is REFUSED by the ``tool_name in (None, "Bash")`` scope
    — the scan never mints for a tool the parser positively identifies as
    non-Bash."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    entries = [
        make_jsonl_entry(
            "assistant",
            [make_tool_use_block("tr", "Read", {"file_path": "/x"})],
        ),
        make_jsonl_entry(
            "user",
            [make_tool_result_block("tr", "contents")],
            tool_use_result={"backgroundTaskId": "bgstolen1"},
        ),
    ]
    activity = await _register_and_load(monitor, sc, entries)
    launched = activity.launched if activity is not None else set()
    assert "bgstolen1" not in launched


def test_t6_meta_disjointness_only_bash_meta_mints():
    """``background_bash_task_id_from_meta`` returns None for the Agent /
    Workflow / resume ownership metas and for a plain result — only a
    ``backgroundTaskId``-bearing meta mints."""
    from cctelegram.handlers.response_builder import background_bash_task_id_from_meta

    agent_meta = {"status": "async_launched", "isAsync": True, "agentId": "abc123"}
    workflow_meta = {
        "status": "async_launched",
        "taskId": "wrnmrbn3s",
        "runId": "r1",
        "transcriptDir": "/d",
    }
    resume_meta = {"success": True, "message": "resumed", "resumedAgentId": "abc123"}
    plain_meta = {"stdout": "ok", "stderr": "", "interrupted": False}

    assert background_bash_task_id_from_meta(agent_meta) is None
    assert background_bash_task_id_from_meta(workflow_meta) is None
    assert background_bash_task_id_from_meta(resume_meta) is None
    assert background_bash_task_id_from_meta(plain_meta) is None
    assert background_bash_task_id_from_meta({"backgroundTaskId": BASH_KEY}) == BASH_KEY


# ── T7: the disclosed truncation-replay residual (RED baseline) ───────────────


@pytest.mark.asyncio
async def test_t7_truncation_replay_re_mints_key_disclosed_residual(monitor, tmp_path):
    """r1 P2-a (disclosed, NOT closed) — the DOCUMENTATION pin for the accepted
    residual, driven through the full production-visible sequence: offsets are
    persisted and first-seen files start at EOF, so ordinary restarts never
    replay — but ``_read_new_lines`` resets a TRUNCATED file's offset to 0 and
    re-reads it. The DANGEROUS sequence: close tombstones the key → a GENUINE
    user turn resets tombstones → the truncation replay re-mints a 2 h
    false-typing key (the route projects RUNNING again). Truncation of an
    append-only CC sidechain is not an observed production event; this DOCUMENTS
    the accepted shape (bounded by the 2 h TTL) as a RED baseline for a future
    tightening.
    """
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{STEM}.jsonl"
    activity = await _register_and_load(monitor, sc, _fixture_entries())
    assert activity is not None and BASH_KEY in activity.launched

    # Fan-out the FIRST launch to route_runtime (the bot fan-out's launched-key
    # seam) on an idle route → projected RUNNING.
    await _idle_route(end_ts=100.0)
    for key in activity.launched:
        await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, key)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.background_agents == (BASH_KEY,)

    # The parent <task-notification> close → tombstoned, projected idle.
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, BASH_KEY, source=BgDoneSource.PARENT
    )
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.background_agents == ()

    # A GENUINE user turn (the real ingest path a user event takes) RESETS the
    # tombstones; the assistant then ends its turn → idle again.
    await route_runtime.ingest_transcript_event(
        ROUTE,
        route_runtime.TranscriptLifecycleEvent(
            role="user",
            block_type="text",
            tool_use_id=None,
            tool_name=None,
            stop_reason=None,
            timestamp=200.0,
        ),
    )
    await _idle_route(end_ts=250.0)

    # TRUNCATE the file to fewer bytes than the tracked offset (offset > size →
    # _read_new_lines resets to 0 and re-reads), keeping the launch pair.
    launch_pair = _fixture_entries()[1:3]  # tool_use + tool_result
    sc.write_text("")
    _append_entries(sc, launch_pair)
    # Bump mtime so the mtime/size short-circuit does not skip the re-read.
    future = os.path.getmtime(sc) + 100
    os.utime(sc, (future, future))
    await monitor.check_sidechain_updates({PARENT})
    replayed = monitor.pop_sidechain_activity().get(PARENT)
    assert replayed is not None
    assert BASH_KEY in replayed.launched  # the replayed launch

    # Fan-out the truncation-replayed launch: with the tombstone reset by the
    # genuine user turn, the key RE-MINTS and the route projects RUNNING again
    # — the disclosed residual (a 2 h false-typing key, TTL-bounded).
    for key in replayed.launched:
        await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, key)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.background_agents == (BASH_KEY,)
