"""GH #46 PR-2 — teammate first-class background keys: discriminators, the
generational registry, launch-at-binding, the wake lane, and discovery-
quarantine severing.

PR-2 makes an agent-teams teammate a first-class background key so typing stays
ON while it genuinely works across parent turns, promptly drops when it parks,
and re-lights when it is re-woken — WITHOUT stranding on a stale same-name
sidechain file. route_runtime gains ZERO new mutators: the registry drives the
existing launched / resumed / done marks through the bot fan-out.

Load-bearing pins:
  - the five-way disjoint structured discriminators (spawn / wake) — never
    borrow another lane's ownership field; name-validated (a glob + regex seam);
  - the generational registry: spawn/bind/wake/park, same-name respawn rotation,
    the mtime + first-entry-timestamp binding gates (gen-dependent strictness,
    fail-DARK on ambiguity), the causal pending-park reduction (typed slot,
    UnknownDone dominates);
  - the discovery-quarantine sever: a non-current same-name key can NEVER be
    recorded live (the sequential-ambiguity strand pin + tombstone-reset
    immunity).

Fixtures are Claude Code 2.1.197 agent-teams shapes (the live incident).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cctelegram.handlers.response_builder import (
    TeammateSpawnInfo,
    teammate_send_target_from_meta,
    teammate_spawn_info_from_meta,
)
from cctelegram.session_monitor import (
    ParentSidechainActivity,
    SessionInfo,
    SessionMonitor,
    TrackedSession,
    _PendingPark,
    _merge_pending_park,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _spawn_meta() -> dict:
    """The real ``teammate_spawned`` toolUseResult (fixture line 0)."""
    lines = [
        json.loads(ln)
        for ln in (_FIXTURES / "teammate_idle_notification_v2.1.197.jsonl")
        .read_text()
        .splitlines()
    ]
    return lines[0]["toolUseResult"]


# ── teammate_spawn_info_from_meta ────────────────────────────────────────


def test_spawn_from_real_meta():
    info = teammate_spawn_info_from_meta(_spawn_meta())
    assert info == TeammateSpawnInfo(
        name="explore-backend-workflows",
        teammate_id="explore-backend-workflows@session-4f543a1a",
        agent_type="explorer",
    )


def test_spawn_requires_status():
    meta = dict(_spawn_meta())
    meta["status"] = "async_launched"
    assert teammate_spawn_info_from_meta(meta) is None


def test_spawn_requires_nonempty_name():
    meta = dict(_spawn_meta())
    meta["name"] = ""
    assert teammate_spawn_info_from_meta(meta) is None
    meta["name"] = None
    assert teammate_spawn_info_from_meta(meta) is None


def test_spawn_none_on_non_dict():
    assert teammate_spawn_info_from_meta(None) is None
    assert teammate_spawn_info_from_meta("string") is None
    assert teammate_spawn_info_from_meta(42) is None


@pytest.mark.parametrize(
    "ownership_field", ["agentId", "taskId", "backgroundTaskId", "resumedAgentId"]
)
def test_spawn_disjoint_refuses_other_ownership_field(ownership_field):
    """Five-way disjointness (in-parser): a spawn meta carrying ANY other lane's
    ownership field returns None — never double-recorded across lanes."""
    meta = dict(_spawn_meta())
    meta[ownership_field] = "someid"
    assert teammate_spawn_info_from_meta(meta) is None


def test_spawn_metacharacter_name_refused(caplog):
    """The name feeds a glob and a regex — a metacharacter name fails DARK with
    a WARNING rather than becoming an unescaped pattern."""
    import logging

    for bad in ["../etc", "na*me", "a b", "name\n", "x" * 65, "na.me"]:
        meta = dict(_spawn_meta())
        meta["name"] = bad
        with caplog.at_level(logging.WARNING):
            assert teammate_spawn_info_from_meta(meta) is None


def test_spawn_optional_fields_best_effort():
    """teammate_id / agent_type are best-effort — a spawn with only status+name
    still parses (name is the load-bearing field)."""
    info = teammate_spawn_info_from_meta({"status": "teammate_spawned", "name": "solo"})
    assert info is not None
    assert info.name == "solo"
    assert info.teammate_id is None
    assert info.agent_type is None


# ── teammate_send_target_from_meta ───────────────────────────────────────


def test_send_target_from_real_meta():
    meta = {
        "success": True,
        "message": "Message sent to explore-frontend-builder's inbox",
        "msg_id": "ad7c40f6-3147-4e12-84ca-490334234883",
        "routing": {"sender": "team-lead", "target": "@explore-frontend-builder"},
    }
    assert teammate_send_target_from_meta(meta) == "explore-frontend-builder"


def test_send_target_requires_success_true():
    meta = {
        "success": False,
        "routing": {"target": "@explore-frontend-builder"},
    }
    assert teammate_send_target_from_meta(meta) is None


def test_send_target_requires_at_prefix():
    meta = {"success": True, "routing": {"target": "explore-frontend-builder"}}
    assert teammate_send_target_from_meta(meta) is None
    meta = {"success": True, "routing": {"target": ""}}
    assert teammate_send_target_from_meta(meta) is None


def test_send_target_none_when_resumed_lane():
    """The Fix C nudge lane (resumedAgentId) keeps ownership — a send-target
    parse must decline so the two lanes never both fire."""
    meta = {
        "success": True,
        "resumedAgentId": "aexplore-x-deadbeef",
        "routing": {"target": "@explore-x"},
    }
    assert teammate_send_target_from_meta(meta) is None


@pytest.mark.parametrize("ownership_field", ["agentId", "taskId", "backgroundTaskId"])
def test_send_target_disjoint_refuses_other_ownership_field(ownership_field):
    meta = {
        "success": True,
        "routing": {"target": "@explore-x"},
    }
    meta[ownership_field] = "someid"
    assert teammate_send_target_from_meta(meta) is None


def test_send_target_metacharacter_refused():
    meta = {"success": True, "routing": {"target": "@na*me"}}
    assert teammate_send_target_from_meta(meta) is None


def test_send_target_none_on_non_dict():
    assert teammate_send_target_from_meta(None) is None
    assert teammate_send_target_from_meta("x") is None


@pytest.mark.asyncio
async def test_prose_spawn_without_meta_warns_and_does_not_register(
    tmp_path, make_jsonl_entry, make_tool_use_block, caplog
):
    """STRUCTURED-ONLY: a prose ``Spawned successfully.`` Agent tool_result WITHOUT
    the structured ``teammate_spawned`` meta fires a rate-limited drift WARNING and
    NEVER registers (the T1.6 pattern)."""
    import logging

    from cctelegram.session_monitor import SessionInfo, SessionMonitor, TrackedSession

    mon = SessionMonitor(
        projects_path=tmp_path / "projects", state_file=tmp_path / "ms.json"
    )
    proj = tmp_path / "projects" / "-p"
    proj.mkdir(parents=True)
    pj = proj / "parent-sid.jsonl"
    pj.write_text("")
    (proj / "parent-sid" / "subagents").mkdir(parents=True)
    mon.state.update_session(
        TrackedSession(session_id="parent-sid", file_path=str(pj), last_byte_offset=0)
    )

    async def _scan():
        return [SessionInfo(session_id="parent-sid", file_path=pj)]

    mon.scan_projects = _scan  # type: ignore[method-assign]
    with open(pj, "a") as f:
        f.write(
            json.dumps(
                make_jsonl_entry(
                    "assistant",
                    [make_tool_use_block("tu", "Agent", {"prompt": "x"})],
                    session_id="parent-sid",
                )
            )
            + "\n"
        )
        f.write(
            json.dumps(
                make_jsonl_entry(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu",
                            "content": [
                                {"type": "text", "text": "Spawned successfully.\nx"}
                            ],
                        }
                    ],
                    session_id="parent-sid",
                    # No tool_use_result meta at all.
                )
            )
            + "\n"
        )
    with caplog.at_level(logging.WARNING):
        await mon.check_for_updates({"parent-sid"})
    assert mon._teammate_registry.get("parent-sid", {}) == {}
    assert any(
        "spawn-result format may have drifted" in r.message for r in caplog.records
    )


# ── monitor-level registry: spawn / bind / wake / park ───────────────────
#
# These drive the REAL code paths: a spawn arrives via ``check_for_updates``
# (parent JSONL Agent tool_result carrying ``toolUseResult.status ==
# "teammate_spawned"``); its sidechain file appears and BINDS via
# ``check_sidechain_updates``; a wake arrives via ``check_for_updates``
# (SendMessage); a park arrives via ``check_for_updates`` (user-text teammate
# envelope). The bot fan-out is not run — the assertions read the drained
# ``ParentSidechainActivity`` (launched / resumed / teammate_parks) the fan-out
# consumes, and the registry state directly.

PARENT = "parent-sid"
_NAME = "explore-backend"  # a slug + pure-hex residual disambiguates nested names


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


def _append(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _spawn_meta_for(name: str) -> dict:
    return {
        "status": "teammate_spawned",
        "teammate_id": f"{name}@session-abcd1234",
        "agent_id": f"{name}@session-abcd1234",
        "agent_type": "explorer",
        "name": name,
        "color": "blue",
    }


def _append_spawn(
    parent_jsonl,
    name: str,
    make_jsonl_entry,
    make_tool_use_block,
    spawn_ts: str | None = None,
):
    """A teammate spawn: an Agent tool_use + its tool_result carrying the
    structured ``teammate_spawned`` toolUseResult (its text is snake ``agent_id:``,
    which the plain-Agent ``agentId:`` regex never matches).

    ``spawn_ts`` is the tool_result's JSONL EVENT timestamp (CC's write instant) —
    ``_record_teammate_spawn`` anchors ``spawned_ts`` to it, NOT the monitor's
    parse instant (the adversarial-review P1 fix). It must be the SAME CC-clock as
    the sidechain's first-entry ts so the binding gate compares apples to apples.
    Defaults to the current UTC instant (correct-TZ, unlike ``make_jsonl_entry``'s
    local-as-UTC strftime default), so a same-clock sidechain first entry binds."""
    if spawn_ts is None:
        spawn_ts = _iso(time.time())
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_spawn", "Agent", {"prompt": "go"})],
                session_id=PARENT,
                timestamp=spawn_ts,
            ),
            make_jsonl_entry(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_spawn",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Spawned successfully.\n"
                                    f"agent_id: {name}@session-abcd1234\n"
                                    f"name: {name}\nThe agent is now running."
                                ),
                            }
                        ],
                    }
                ],
                session_id=PARENT,
                timestamp=spawn_ts,
                tool_use_result=_spawn_meta_for(name),
            ),
        ],
    )


def _append_wake(
    parent_jsonl, name: str, make_jsonl_entry, make_tool_use_block, ts: str
):
    """A SendMessage wake to ``name``: the paired tool_use input.to == name and
    the tool_result's routing.target == @name (no resumedAgentId)."""
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_wake", "SendMessage", {"to": name})],
                session_id=PARENT,
                timestamp=ts,
            ),
            make_jsonl_entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "tu_wake", "content": "ok"}],
                session_id=PARENT,
                timestamp=ts,
                tool_use_result={
                    "success": True,
                    "message": f"Message sent to {name}'s inbox",
                    "routing": {"sender": "team-lead", "target": f"@{name}"},
                },
            ),
        ],
    )


_TEAMMATE_ENVELOPE = (
    "Another Claude session sent a message:\n"
    '<teammate-message teammate_id="{name}" color="blue">\n'
    '{{"type":"idle_notification","from":"{name}","timestamp":"{ts}",'
    '"idleReason":"available"}}\n'
    "</teammate-message>\n"
)


def _append_park(parent_jsonl, name: str, make_jsonl_entry, ts: str):
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                _TEAMMATE_ENVELOPE.format(name=name, ts=ts),
                session_id=PARENT,
            )
        ],
    )


def _write_sidechain(sub_dir, key: str, first_ts: str):
    """Write a teammate sidechain file ``agent-<key>.jsonl`` with one first entry
    carrying ``first_ts`` (the binding gate's first-entry-ts anchor)."""
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi"}]},
                "sessionId": "x",
                "timestamp": first_ts,
            }
        )
        + "\n"
    )
    return sc


def _key_for(name: str, hexid: str = "deadbeefcafe0011") -> str:
    return f"a{name}-{hexid}"


# ── typed pending-park slot dominance (Codex r4 pin) ─────────────────────


def test_pending_park_unknown_dominates_permanently():
    """The typed slot pins the causal-reduction dominance: once UnknownDone, a
    later PARSEABLE park can NEVER downgrade it back to a ParkAt (a tuple
    last-write-wins would lose the unconditional-tombstone evidence)."""
    slot = _merge_pending_park(None, park_ts=None, unparseable=True)
    assert slot == _PendingPark(unknown_done=True, ts=None)
    slot = _merge_pending_park(slot, park_ts=500.0, unparseable=False)
    assert slot.unknown_done is True  # dominance holds
    assert slot.ts is None


def test_pending_park_keeps_max_parseable():
    slot = _merge_pending_park(None, park_ts=100.0, unparseable=False)
    slot = _merge_pending_park(slot, park_ts=50.0, unparseable=False)
    assert slot == _PendingPark(unknown_done=False, ts=100.0)  # older never downgrades
    slot = _merge_pending_park(slot, park_ts=200.0, unparseable=False)
    assert slot == _PendingPark(unknown_done=False, ts=200.0)


def test_pending_park_parseable_then_unparseable_dominates():
    slot = _merge_pending_park(None, park_ts=100.0, unparseable=False)
    slot = _merge_pending_park(slot, park_ts=None, unparseable=True)
    assert slot == _PendingPark(unknown_done=True, ts=None)


# ── spawn / bind / wake / park happy path ────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_bind_wake_park_happy_path(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)

    # 1. Spawn registers the teammate (gen-1), no launched key yet.
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}  # launched deferred to bind
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 1 and rec.current_key is None

    # 2. The sidechain file appears (mtime + first entry AFTER the spawn) → BIND
    #    emits the launched key at discovery (zero parsed run-state entries).
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == {key}
    assert monitor._teammate_registry[PARENT][_NAME].current_key == key

    # 3. Wake relights the bound key (resumed).
    _append_wake(
        parent_jsonl,
        _NAME,
        make_jsonl_entry,
        make_tool_use_block,
        _iso(time.time() + 1),
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert key in activity[PARENT].resumed

    # 4. Park closes the current key (typing drops).
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(time.time() + 2))
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert key in activity[PARENT].teammate_parks


@pytest.mark.asyncio
async def test_wake_input_to_mismatch_refuses(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, caplog
):
    """The wake cross-checks the paired tool_use ``input["to"]`` against the
    ``routing.target`` name (Hermes r2 P2-2): a mismatch REFUSES the wake + WARNs
    (a bound key is NOT relit)."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # A SendMessage whose routing.target=@explore-backend but input.to=someone-else.
    ts = _iso(time.time() + 1)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_w", "SendMessage", {"to": "someone-else"})],
                session_id=PARENT,
                timestamp=ts,
            ),
            make_jsonl_entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "tu_w", "content": "ok"}],
                session_id=PARENT,
                timestamp=ts,
                tool_use_result={
                    "success": True,
                    "routing": {"sender": "team-lead", "target": f"@{_NAME}"},
                },
            ),
        ],
    )
    with caplog.at_level(logging.WARNING):
        await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert (
        act == {}
        or _key_for(_NAME) not in act.get(PARENT, ParentSidechainActivity()).resumed
    )
    assert any("!= input.to" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_wake_unknown_name_no_op(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A wake to a teammate this parent never SPAWNED is not ours to relight
    (unknown name → no-op, no resumed key)."""
    parent_jsonl, _sub_dir = _setup_parent(monitor, tmp_path)
    _append_wake(
        parent_jsonl,
        "never-spawned",
        make_jsonl_entry,
        make_tool_use_block,
        _iso(time.time()),
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_park_unknown_name_falls_back_to_all_stems_no_registry_degradation(
    monitor, tmp_path, make_jsonl_entry
):
    """A park for a name with NO registry rec (e.g. a pre-restart spawn) falls back
    to PR-1's all-tracked-stems close verbatim — the documented no-registry
    degradation."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for("orphan")
    # Track a matching stem WITHOUT a registry rec (simulates a pre-restart spawn).
    _write_sidechain(sub_dir, key, _iso(time.time()))
    monitor.state.update_session(
        TrackedSession(
            session_id=f"sub:{PARENT}:agent-{key}",
            file_path=str(sub_dir / f"agent-{key}.jsonl"),
            parent_session_id=PARENT,
        )
    )
    _append_park(parent_jsonl, "orphan", make_jsonl_entry, _iso(time.time() + 1))
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key in act[PARENT].teammate_parks  # PR-1 all-stems close


@pytest.mark.asyncio
async def test_teardown_drops_registry_and_severed(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """The registry + severed-stem set die with the parent's tracking state."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    assert PARENT in monitor._teammate_registry
    monitor._remove_sidechains_for_parent(PARENT)
    assert PARENT not in monitor._teammate_registry
    assert PARENT not in monitor._severed_teammate_stems


@pytest.mark.asyncio
async def test_first_seen_eof_binds_and_launches_with_zero_parsed_entries(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """The tracker registers a NEW sidechain file at EOF and returns WITHOUT
    parsing — so the launched key MUST be emitted at DISCOVERY, once-only, with no
    parsed run-state ticks."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})  # first-seen EOF
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == {key}
    assert act[PARENT].ticks == {}  # zero run-state ticks at discovery

    # Second tick: already bound → no re-launch, and its own entries feed
    # run-state normally (it is NOT severed).
    _append(
        sub_dir / f"agent-{key}.jsonl",
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "working"}],
                timestamp=_iso(time.time() + 1),
            ),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert act2.get(PARENT, ParentSidechainActivity()).launched == set()
    assert key in act2[PARENT].ticks  # bound teammate DOES feed run-state


@pytest.mark.asyncio
async def test_mtime_stale_file_never_binds(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A same-name sidechain whose mtime predates the spawn beyond the skew is a
    STALE prior-generation file → quarantined (severed), never bound."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    # Spawn NOW; write a file whose mtime + first entry are far in the PAST.
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]

    sc = _write_sidechain(sub_dir, key, _iso(rec.spawned_ts - 3600))
    import os

    os.utime(sc, (rec.spawned_ts - 3600, rec.spawned_ts - 3600))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    # Quarantined: no launch, key retired + severed, an unconditional done park.
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    assert key in monitor._teammate_registry[PARENT][_NAME].retired_keys
    assert f"sub:{PARENT}:agent-{key}" in monitor._severed_teammate_stems[PARENT]


@pytest.mark.asyncio
async def test_retired_key_never_rebinds(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    # Force the key retired, then present its (fresh) file.
    monitor._teammate_registry[PARENT][_NAME].retired_keys.add(key)
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    assert monitor._teammate_registry[PARENT][_NAME].current_key is None
    assert f"sub:{PARENT}:agent-{key}" in monitor._severed_teammate_stems[PARENT]


@pytest.mark.asyncio
async def test_pending_wake_applied_at_binding(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A wake that arrives while UNBOUND is buffered as pending_wake and applied
    (as a resume) at the bind — the key ends LIVE (resumed)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Wake before the sidechain exists → pending_wake.
    _append_wake(
        parent_jsonl,
        _NAME,
        make_jsonl_entry,
        make_tool_use_block,
        _iso(time.time() + 1),
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}  # unbound: no resume emitted yet
    assert monitor._teammate_registry[PARENT][_NAME].pending_wake is not None

    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == {key}
    assert key in act[PARENT].resumed  # pending wake applied at bind


@pytest.mark.asyncio
async def test_park_before_bind_same_tick_tombstones(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Spawn + a park arrive before the sidechain EOF ⇒ pending_park buffered ⇒
    binding applies it ⇒ the key ends TOMBSTONED (parked)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(time.time() + 1))
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    assert monitor._teammate_registry[PARENT][_NAME].pending_park is not None

    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == {key}
    assert key in act[PARENT].teammate_parks  # parked at bind → tombstoned


@pytest.mark.asyncio
async def test_pending_wake_newer_than_pending_park_stays_live_via_gate(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Variant: pending wake T2 > pending park T1 — both applied at bind (wake
    first, then park), so the runtime ts-gate arbitrates. The park park_ts (T1) is
    OLDER than the resume (T2), so the runtime's TEAMMATE stale gate keeps it LIVE.
    We assert both signals reach the fan-out; the runtime resolution is pinned in
    the route_runtime suite."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    base = time.time()
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    # park T1 (older), wake T2 (newer) — both while unbound.
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(base + 1))
    _append_wake(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base + 2)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.pending_wake is not None and rec.pending_park is not None

    _write_sidechain(sub_dir, key, _iso(base + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key in act[PARENT].resumed  # wake applied
    assert key in act[PARENT].teammate_parks  # park applied
    # The resume ts is strictly newer than the park ts → the runtime keeps it LIVE.
    resume_ts = act[PARENT].resumed[key]
    park_ts, unparseable = act[PARENT].teammate_parks[key]
    assert resume_ts is not None and park_ts is not None and resume_ts > park_ts


# ── same-name respawn rotation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_respawn_rotation_relights_only_the_new_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A same-name RESPAWN rotates the generation: the old gen-1 key is tombstoned
    at rotation, and a wake AFTER the respawn relights ONLY the new gen-2 key (the
    old tombstone survives)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key1 = _key_for(_NAME, "aaaa1111bbbb2222")
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    assert monitor.pop_sidechain_activity()[PARENT].launched == {key1}
    assert monitor._teammate_registry[PARENT][_NAME].current_key == key1

    # Respawn same name → rotation: gen-1 key tombstoned + severed, gen bumps.
    respawn_ts = time.time() + 5
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    rot = monitor.pop_sidechain_activity()
    assert key1 in rot[PARENT].teammate_parks  # old key tombstoned at rotation
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 2 and rec.current_key is None
    assert key1 in rec.retired_keys

    # The gen-2 file binds (its first entry is AFTER the respawn).
    key2 = _key_for(_NAME, "cccc3333dddd4444")
    _write_sidechain(sub_dir, key2, _iso(respawn_ts + 1))
    await monitor.check_sidechain_updates({PARENT})
    assert monitor.pop_sidechain_activity()[PARENT].launched == {key2}

    # A wake after respawn relights ONLY the new key.
    _append_wake(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(respawn_ts + 2)
    )
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key2 in act[PARENT].resumed and key1 not in act[PARENT].resumed


@pytest.mark.asyncio
async def test_respawn_with_live_old_key_no_residual_live_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """gen-1 bound + launched + NO sidechain end_turn → same-name respawn ⇒ the
    gen-1 key is tombstoned at rotation; the gen-2 leg parks cleanly ⇒ NO residual
    live key strands typing."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key1 = _key_for(_NAME, "1111aaaa2222bbbb")
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    respawn_ts = time.time() + 5
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    rot = monitor.pop_sidechain_activity()
    assert key1 in rot[PARENT].teammate_parks  # gen-1 tombstoned even though live

    # gen-2 binds then parks cleanly.
    key2 = _key_for(_NAME, "3333cccc4444dddd")
    _write_sidechain(sub_dir, key2, _iso(respawn_ts + 1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(respawn_ts + 3))
    await monitor.check_for_updates({PARENT})
    park = monitor.pop_sidechain_activity()
    assert key2 in park[PARENT].teammate_parks


@pytest.mark.asyncio
async def test_respawn_quarantines_untracked_disk_stem_before_first_bind(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A gen-1 file exists on disk but is NOT yet tracked (never seen by a
    sidechain glob). A gen-2 respawn processed BEFORE discovery must NOT bind it,
    even if the old file's mtime later advances past the gen-2 spawn — the
    disk-snapshot quarantine severs it. Only the genuine gen-2 file binds."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time()
    old_key = _key_for(_NAME, "01d000000aaaa111")
    # gen-1 spawn + its file exists on disk (mtime old), but we DON'T run
    # check_sidechain_updates so it stays untracked.
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    old_sc = _write_sidechain(sub_dir, old_key, _iso(base - 1))
    import os

    os.utime(old_sc, (base - 1, base - 1))

    # gen-2 respawn (the disk-snapshot quarantine runs during rotation).
    respawn_ts = base + 10
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert old_key in rec.retired_keys  # quarantined on sight from the disk snapshot

    # Now the old file's mtime advances PAST the gen-2 spawn (a late write) and it
    # is finally discovered — it must NOT bind (retired) — while the genuine gen-2
    # file binds.
    os.utime(old_sc, (respawn_ts + 5, respawn_ts + 5))
    new_key = _key_for(_NAME, "9999eeee8888ffff")
    _write_sidechain(sub_dir, new_key, _iso(respawn_ts + 1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == {new_key}
    assert monitor._teammate_registry[PARENT][_NAME].current_key == new_key


# ── first-entry-ts binding gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_gen2_binds_when_spawn_event_ts_precedes_monitor_parse(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Adversarial-review P1 regression: the poll LAGS CC's write, so the genuine
    gen-2 file's first entry is at ~the spawn's JSONL EVENT ts, which is EARLIER
    than the monitor's parse wall-clock. ``spawned_ts`` must anchor to the spawn
    EVENT ts (not ``time.time()`` at parse), else the gen>=2 STRICT gate
    (``first_ts >= spawned_ts``) rejects the genuine file and the teammate goes
    DARK on every respawn."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    # A spawn EVENT ts firmly in the PAST relative to the monitor's parse now, so
    # if the code used time.time() the gen-2 gate would reject a same-event-ts file.
    spawn_ts_wall = time.time() - 30
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(spawn_ts_wall)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    # Respawn at a slightly later event ts (still in the past vs parse-now).
    respawn_event = spawn_ts_wall + 5
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(respawn_event)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 2
    # spawned_ts is the EVENT ts (past), NOT parse-now.
    assert rec.spawned_ts < time.time() - 10

    # The genuine gen-2 file's first entry is at the respawn event instant, and its
    # mtime is at the event instant too — both < parse-now. It MUST bind.
    key = _key_for(_NAME, "beadfeed00112233")
    sc = _write_sidechain(sub_dir, key, _iso(respawn_event + 0.1))
    import os

    os.utime(sc, (respawn_event + 0.1, respawn_event + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == {key}  # NOT dark — binds despite the poll lag
    assert monitor._teammate_registry[PARENT][_NAME].current_key == key


@pytest.mark.asyncio
async def test_gen1_file_created_after_rotation_does_not_bind(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A gen-1 file CREATED AFTER a rotation (mtime within skew, so it escapes both
    quarantine passes) must NOT bind gen-2: its FIRST entry predates the gen-2
    spawn (strict gate)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 2

    # File with a FRESH mtime (now, escapes mtime prefilter) but its first entry
    # ts is BEFORE the gen-2 spawn → strict gate rejects.
    key = _key_for(_NAME, "abcdef0011223344")
    _write_sidechain(sub_dir, key, _iso(rec.spawned_ts - 2))  # first entry pre-spawn
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    assert monitor._teammate_registry[PARENT][_NAME].current_key is None


@pytest.mark.asyncio
async def test_unreadable_first_line_no_bind_this_tick_binds_on_retry(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A mid-write first line (no newline yet) ⇒ NO bind this tick, RETRY; once the
    line completes, it binds."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Partial first line: JSON with no trailing newline.
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        '{"type":"assistant","timestamp":"' + _iso(time.time() + 0.1)
    )  # no \n
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    # Not quarantined — an indeterminate gate is a RETRY, not a sever.
    assert key not in monitor._teammate_registry[PARENT][_NAME].retired_keys

    # Complete the line → next tick binds.
    with open(sc, "w") as f:
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": []},
                    "timestamp": _iso(time.time() + 0.2),
                }
            )
            + "\n"
        )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert act2[PARENT].launched == {key}


@pytest.mark.asyncio
async def test_gen2_strict_rejects_first_entry_in_pre_spawn_skew(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """gen>=2 is STRICT: a first entry INSIDE the pre-spawn skew window (>= spawn -
    skew but < spawn) does NOT bind (gen-1 would have)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 2

    key = _key_for(_NAME, "5566778899aabbcc")
    # first entry 2s BEFORE spawn (inside the 5s skew, but gen>=2 is strict).
    _write_sidechain(sub_dir, key, _iso(rec.spawned_ts - 2))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()


@pytest.mark.asyncio
async def test_simultaneous_gen2_multi_candidate_binds_none_and_warns(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, caplog
):
    """gen>=2 with MULTIPLE unretired candidates passing the strict gate at once in
    the pre-spawn scan ⇒ bind NONE + WARN (fail-dark). Drives
    ``_try_bind_tracked_teammate_stems`` directly on a gen-2 rec — the realistic
    respawn path quarantines everything via the disk snapshot, so this pins the
    defense-in-depth guard that protects a residual two-tracked-candidate race."""
    import logging

    from cctelegram.session_monitor import _TeammateRec

    _parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time()
    k1 = _key_for(_NAME, "1111111122222222")
    k2 = _key_for(_NAME, "3333333344444444")
    future = _iso(base + 100)  # both first entries clear the strict gen-2 gate
    sc1 = _write_sidechain(sub_dir, k1, future)
    sc2 = _write_sidechain(sub_dir, k2, future)
    import os

    os.utime(sc1, (base + 100, base + 100))
    os.utime(sc2, (base + 100, base + 100))
    # Track both files (unbound) so the pre-spawn scan sees two candidates.
    for k in (k1, k2):
        monitor.state.update_session(
            TrackedSession(
                session_id=f"sub:{PARENT}:agent-{k}",
                file_path=str(sub_dir / f"agent-{k}.jsonl"),
                parent_session_id=PARENT,
            )
        )
    # A gen-2 rec with NO retired keys and no current_key.
    monitor._teammate_registry[PARENT] = {
        _NAME: _TeammateRec(
            name=_NAME,
            teammate_id=None,
            spawn_generation=2,
            spawned_ts=base,
        )
    }
    with caplog.at_level(logging.WARNING):
        monitor._try_bind_tracked_teammate_stems(PARENT, _NAME)
    assert monitor._teammate_registry[PARENT][_NAME].current_key is None
    assert any("simultaneous gen>=2" in r.message for r in caplog.records)


# ── the structural strand pins ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_sequential_ambiguity_strand_pin(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """The STRAND PIN: an old-gen file appears ALONE and binds; the genuine new-gen
    file appears LATER ⇒ quarantined (retired + done + severed), NO live key ever
    recorded for it, no 2h strand. A park by name still closes the current key; the
    next respawn binds cleanly."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key1 = _key_for(_NAME, "aaaa0000bbbb1111")
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    # gen-1 file binds ALONE.
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    assert monitor.pop_sidechain_activity()[PARENT].launched == {key1}

    # A SECOND same-name file appears later (a sibling / double --resume) — it is
    # quarantined on sight, NEVER records a live key.
    key2 = _key_for(_NAME, "cccc2222dddd3333")
    _write_sidechain(sub_dir, key2, _iso(time.time() + 1))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key2 not in act.get(PARENT, ParentSidechainActivity()).launched
    assert key2 not in act.get(PARENT, ParentSidechainActivity()).ticks
    assert f"sub:{PARENT}:agent-{key2}" in monitor._severed_teammate_stems[PARENT]

    # Even if key2's file keeps writing, it never records a run-state tick.
    _append(
        sub_dir / f"agent-{key2}.jsonl",
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "sibling work"}],
                timestamp=_iso(time.time() + 2),
            ),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert key2 not in act2.get(PARENT, ParentSidechainActivity()).ticks

    # A park by name still closes the CURRENT (key1).
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(time.time() + 3))
    await monitor.check_for_updates({PARENT})
    park = monitor.pop_sidechain_activity()
    assert key1 in park[PARENT].teammate_parks


@pytest.mark.asyncio
async def test_severed_stem_immune_to_tombstone_reset(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A quarantined stem keeps writing; the sever is monitor-side, so even a
    GENUINE user turn (which would reset runtime background_agents_done) leaves the
    stem recording NOTHING — feed_run_state=False at the emission seam."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key1 = _key_for(_NAME, "eeee0000ffff1111")
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()
    # Quarantine a sibling.
    key2 = _key_for(_NAME, "22223333aaaabbbb")
    _write_sidechain(sub_dir, key2, _iso(time.time() + 1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()
    assert f"sub:{PARENT}:agent-{key2}" in monitor._severed_teammate_stems[PARENT]

    # A genuine user turn on the parent (resets runtime tombstones downstream) —
    # here it only matters that the monitor still severs key2.
    _append(
        parent_jsonl,
        [
            make_jsonl_entry("user", "a genuine human message", session_id=PARENT),
        ],
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    # key2 keeps writing.
    _append(
        sub_dir / f"agent-{key2}.jsonl",
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "still working"}],
                timestamp=_iso(time.time() + 2),
            ),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key2 not in act.get(PARENT, ParentSidechainActivity()).ticks


# ── cross-tick delayed park / same-batch ordering ────────────────────────


@pytest.mark.asyncio
async def test_delayed_stale_park_after_wake_cross_tick_stays_live(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """leg-1 park inner ts T1 delayed → wake T2>T1 processed first (relights) → the
    stale park arrives a LATER tick ⇒ the key stays LIVE (the runtime ts gate keeps
    a resumed key alive against an older park); leg-2 park T3>T2 tombstones. We
    assert the monitor emits the right ts-ordered signals; the runtime resolution
    is pinned in the route_runtime suite."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    base = time.time()
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key, _iso(base + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Wake T2 (relights) processed BEFORE the stale park T1 lands.
    _append_wake(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base + 2)
    )
    await monitor.check_for_updates({PARENT})
    wake = monitor.pop_sidechain_activity()
    assert wake[PARENT].resumed[key] is not None

    # The delayed stale park T1 (< T2) arrives a LATER tick.
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(base + 1))
    await monitor.check_for_updates({PARENT})
    stale = monitor.pop_sidechain_activity()
    park_ts, _ = stale[PARENT].teammate_parks[key]
    assert park_ts is not None and park_ts < wake[PARENT].resumed[key]  # older → LIVE

    # leg-2 park T3 > T2 tombstones.
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(base + 3))
    await monitor.check_for_updates({PARENT})
    final = monitor.pop_sidechain_activity()
    final_park, _ = final[PARENT].teammate_parks[key]
    assert final_park is not None and final_park > wake[PARENT].resumed[key]  # → done


@pytest.mark.asyncio
async def test_same_batch_ticks_and_parks_ordering_pin(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """One ParentSidechainActivity carrying BOTH a bound-key run-state tick AND its
    teammate park (same batch) ⇒ the fan-out sees both; the park closes it (the
    runtime resolution — no bg-only flap — is pinned in the route_runtime suite)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    base = time.time()
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key, _iso(base + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Same tick: the sidechain writes an entry (tick) AND the parent parks it.
    _append(
        sub_dir / f"agent-{key}.jsonl",
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "final work"}],
                timestamp=_iso(base + 5),
            ),
        ],
    )
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(base + 6))
    await monitor.check_sidechain_updates({PARENT})  # tick lands here
    await monitor.check_for_updates({PARENT})  # park lands here (same drained record)
    act = monitor.pop_sidechain_activity()
    assert key in act[PARENT].ticks
    assert key in act[PARENT].teammate_parks
    tick_ts = act[PARENT].ticks[key].max_event_ts
    park_ts, _ = act[PARENT].teammate_parks[key]
    assert park_ts is not None and tick_ts is not None and park_ts > tick_ts
