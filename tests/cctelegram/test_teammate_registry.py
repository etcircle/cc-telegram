"""GH #46 PR-2 — teammate first-class background keys: discriminators, the
generational registry, always-resumed relight-at-binding (r7 item 3: every
teammate bind relights via the tombstone-popping ``resumed`` lane, never
``launched``), the wake lane, and discovery-quarantine severing.

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
    TEAMMATE_RETRACT_RESUME_EPSILON_S,
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

    # 1. Spawn registers the teammate (gen-1), no relight signal yet.
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}  # relight deferred to bind
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 1 and rec.current_key is None

    # 2. The sidechain file appears (mtime + first entry AFTER the spawn) → BIND
    #    relights via the resumed lane at discovery (r7 item 3, never launched;
    #    zero parsed run-state entries).
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == set()
    assert activity[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    parsing — so the relight signal (the resumed lane, r7 item 3) MUST be emitted
    at DISCOVERY, once-only, with no parsed run-state ticks."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})  # first-seen EOF
    act = monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    assert act[PARENT].launched == set()
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
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    rec1 = monitor._teammate_registry[PARENT][_NAME]
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act1 = monitor.pop_sidechain_activity()
    assert act1[PARENT].launched == set()
    assert act1[PARENT].resumed == {
        key1: rec1.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    act_bind2 = monitor.pop_sidechain_activity()
    assert act_bind2[PARENT].launched == set()
    assert act_bind2[PARENT].resumed == {
        key2: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }

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
async def test_respawn_new_gen_file_already_on_disk_at_rotation_still_binds(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Adversarial-review P1 (over-quarantine): under poll lag the GENUINE new-gen
    sidechain file ALREADY exists at rotation-parse time. The rotation's
    disk-snapshot quarantine must NOT retire it (its first entry is >= the new
    spawn) — only genuinely STALE prior-gen files are quarantined; the new file
    binds normally. Regression: an unconditional-by-name quarantine darkened the
    respawn permanently."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60  # spawn events in the past (poll lags CC's write)
    key1 = _key_for(_NAME, "1111aaaa1111aaaa")
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec1 = monitor._teammate_registry[PARENT][_NAME]
    sc1 = _write_sidechain(sub_dir, key1, _iso(base + 0.1))
    import os

    os.utime(sc1, (base + 0.1, base + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act_bind1 = monitor.pop_sidechain_activity()
    assert act_bind1[PARENT].launched == set()
    assert act_bind1[PARENT].resumed == {
        key1: rec1.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }

    # The RESPAWN + the new-gen file BOTH land on disk BEFORE the monitor observes
    # the respawn tool_result (the realistic lag). The new file's first entry is at
    # ~the respawn event ts.
    respawn_event = base + 10
    key2 = _key_for(_NAME, "2222bbbb2222bbbb")
    sc2 = _write_sidechain(sub_dir, key2, _iso(respawn_event + 0.1))
    os.utime(sc2, (respawn_event + 0.1, respawn_event + 0.1))
    # Only NOW does the monitor parse the respawn (rotation runs with the new file
    # already present on disk).
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(respawn_event)
    )
    await monitor.check_for_updates({PARENT})
    # The old key1 is tombstoned at rotation; the new key2 is NOT retired.
    rot = monitor.pop_sidechain_activity()
    assert key1 in rot[PARENT].teammate_parks
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert key1 in rec.retired_keys
    assert key2 not in rec.retired_keys  # the genuine new-gen file survives

    # The new-gen file binds on the next sidechain sweep.
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key2: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert monitor._teammate_registry[PARENT][_NAME].current_key == key2


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
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        new_key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    # NOT dark — binds despite the poll lag (via the resumed relight lane).
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
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
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert act2[PARENT].launched == set()
    assert act2[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }


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
async def test_public_discovery_two_candidates_binds_none_sticky(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, caplog
):
    """Dual-review r1 item 2 (BOTH engines converged), PUBLIC path: TWO same-name
    candidates pass the gate in one ``check_sidechain_updates`` sweep ⇒ bind NONE
    + WARN + the rec goes STICKY-ambiguous — filesystem enumeration order must
    never pick the "genuine" key. The unresolved candidates are NOT arbitrarily
    quarantined/severed, stay OUT of run-state (item 3), and the ambiguity never
    self-resolves (gate inputs are static): a later still-passing tick binds
    nothing. Only the NEXT rotation clears it, after which the fresh gen binds
    cleanly."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    k1 = _key_for(_NAME, "1111111122222222")
    k2 = _key_for(_NAME, "3333333344444444")
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    # BOTH candidates appear in the same sweep, both gate-passing.
    _write_sidechain(sub_dir, k1, _iso(time.time() + 0.2))
    _write_sidechain(sub_dir, k2, _iso(time.time() + 0.2))
    with caplog.at_level(logging.WARNING):
        await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()  # bind NONE
    assert rec.current_key is None and rec.ambiguous is True  # sticky
    assert any("binding NONE" in r.message for r in caplog.records)
    # NOT arbitrarily quarantined/severed (item 2).
    assert k1 not in rec.retired_keys and k2 not in rec.retired_keys
    assert monitor._severed_teammate_stems.get(PARENT, set()) == set()

    # Item 3: the unresolved candidates never feed run-state — activity on both
    # produces ZERO ticks and no launched key.
    for k in (k1, k2):
        _append(
            sub_dir / f"agent-{k}.jsonl",
            [
                make_jsonl_entry(
                    "assistant",
                    [{"type": "text", "text": "work"}],
                    timestamp=_iso(time.time() + 1),
                )
            ],
        )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert act2.get(PARENT, ParentSidechainActivity()).ticks == {}
    assert act2.get(PARENT, ParentSidechainActivity()).launched == set()
    assert monitor._teammate_registry[PARENT][_NAME].current_key is None  # sticky

    # The NEXT rotation clears the ambiguity; the fresh gen file binds cleanly.
    respawn_at = time.time() + 5
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(respawn_at)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.ambiguous is False and rec.spawn_generation == 2
    k3 = _key_for(_NAME, "5555555566666666")
    sc3 = _write_sidechain(sub_dir, k3, _iso(respawn_at + 0.05))
    import os

    os.utime(sc3, (respawn_at + 0.05, respawn_at + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    act3 = monitor.pop_sidechain_activity()
    assert act3[PARENT].launched == set()
    assert act3[PARENT].resumed == {
        k3: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert monitor._teammate_registry[PARENT][_NAME].current_key == k3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_line",
    [
        "this is never json\n",  # permanently malformed — never parses
        '{"type": "assistant", "message": {"content": []}}\n',  # timestamp-less
    ],
    ids=["malformed", "timestamp_less"],
)
async def test_registered_unresolved_candidate_never_feeds_run_state(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, first_line
):
    """Dual-review r1 item 3 (BOTH engines converged): a REGISTERED-name candidate
    whose gate stays INDETERMINATE forever (permanently malformed OR
    timestamp-less first line) must NEVER feed run-state — zero ticks, zero
    launched/background keys — even after subsequent valid activity, and a
    GENUINE user turn (which resets runtime tombstones) changes nothing: the
    darkness is the monitor-side classification, not a tombstone. Pre-fix, the
    indeterminate gate fell through to feed_run_state=True and the unbound
    candidate minted SidechainTicks (the strand re-entry)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    # A PERMANENTLY indeterminate first line + valid activity after it.
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(first_line)
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "working"}],
                timestamp=_iso(time.time() + 1),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})  # registers at EOF
    await monitor.check_sidechain_updates({PARENT})
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "more work"}],
                timestamp=_iso(time.time() + 2),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).ticks == {}
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    # Unresolved — not quarantined (retry stays possible), just dark.
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert key not in rec.retired_keys and rec.current_key is None

    # A genuine user turn on the parent, then more sidechain activity — STILL
    # zero ticks (tombstone-reset immune: classification, not tombstones).
    _append(
        parent_jsonl, [make_jsonl_entry("user", "a genuine human", session_id=PARENT)]
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "still dark"}],
                timestamp=_iso(time.time() + 3),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert act2.get(PARENT, ParentSidechainActivity()).ticks == {}
    # An UNREGISTERED name keeps legacy behavior (control: ticks flow).
    other = sub_dir / "agent-abc999.jsonl"
    other.write_text("")
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()
    _append(
        other,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "legacy"}],
                timestamp=_iso(time.time() + 4),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act3 = monitor.pop_sidechain_activity()
    assert "abc999" in act3[PARENT].ticks


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
    rec = monitor._teammate_registry[PARENT][_NAME]
    _write_sidechain(sub_dir, key1, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    act_bind = monitor.pop_sidechain_activity()
    assert act_bind[PARENT].launched == set()
    assert act_bind[PARENT].resumed == {
        key1: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }

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


# ── generation-scope parks (dual-review r1 item 4, Codex P1) ─────────────


@pytest.mark.asyncio
async def test_prior_generation_park_dropped_from_pending_slot(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """The Codex repro: respawn at T2 → a DELAYED prior-gen park (park_ts T1 < T2)
    arrives while gen-2 is unbound. Pre-fix it was buffered into pending_park and
    applied at bind, tombstoning the FRESH key (no activity/resume stamp yet to
    defend it). Now a parseable park that predates the current generation's
    spawned_ts is DROPPED — the key binds LIVE; the genuine park T3 closes it."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    key1 = _key_for(_NAME, "aaaa1111aaaa1111")
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    sc1 = _write_sidechain(sub_dir, key1, _iso(base + 0.05))
    import os

    os.utime(sc1, (base + 0.05, base + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Respawn at T2; the delayed prior-gen park T1 (< T2) lands AFTER the
    # rotation, while gen-2 is unbound.
    t2 = base + 10
    t1 = base + 5
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(t2))
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(t1))
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.pending_park is None  # the stale park was DROPPED, not buffered

    # gen-2 binds → NO park applied → the fresh key is LIVE.
    key2 = _key_for(_NAME, "bbbb2222bbbb2222")
    sc2 = _write_sidechain(sub_dir, key2, _iso(t2 + 0.05))
    os.utime(sc2, (t2 + 0.05, t2 + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key2: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert key2 not in act[PARENT].teammate_parks  # stays live

    # The GENUINE park T3 (> T2) closes it.
    t3 = t2 + 20
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(t3))
    await monitor.check_for_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert key2 in act2[PARENT].teammate_parks


@pytest.mark.asyncio
async def test_prior_generation_park_dropped_direct_on_bound_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """The direct (bound) variant: gen-2 already bound, the delayed prior-gen park
    T1 < spawned_ts arrives ⇒ dropped (no teammate_parks entry — the fresh key is
    never tombstoned by a park reporting the PRIOR leg's idleness)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    t2 = base + 10
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(t2))
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    key2 = _key_for(_NAME, "cccc3333cccc3333")
    sc2 = _write_sidechain(sub_dir, key2, _iso(t2 + 0.05))
    import os

    os.utime(sc2, (t2 + 0.05, t2 + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # Delayed prior-gen park (T1 < T2) against the BOUND gen-2 key → dropped.
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(base + 5))
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key2 not in act.get(PARENT, ParentSidechainActivity()).teammate_parks


@pytest.mark.asyncio
async def test_unparseable_park_still_records_after_rotation(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """An UNPARSEABLE park cannot be generation-checked, so it keeps unconditional
    dominance (fail-dark doctrine; the disclosed residual: it darkens the new gen
    until a wake / the next genuine park)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    key = _key_for(_NAME, "dddd4444dddd4444")
    sc = _write_sidechain(sub_dir, key, _iso(base + 0.05))
    import os

    os.utime(sc, (base + 0.05, base + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    # An unparseable-ts park (no timestamp field) → recorded (dominates).
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                (
                    "Another Claude session sent a message:\n"
                    f'<teammate-message teammate_id="{_NAME}" color="blue">\n'
                    f'{{"type":"idle_notification","from":"{_NAME}"}}\n'
                    "</teammate-message>\n"
                ),
                session_id=PARENT,
            )
        ],
    )
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].teammate_parks[key] == (None, True)


# ── result-before-use retro-pairing (dual-review r1 item 1, Hermes P1) ───


def _append_spawn_result_before_use(
    parent_jsonl, name: str, make_jsonl_entry, make_tool_use_block, spawn_ts: str
):
    """The GH #42 ordering (27/40 real session files): the tool_result line is
    flushed BEFORE its tool_use line. The parser consumes the result with
    tool_name=None, so the tool_name-gated spawn branch can't fire on it."""
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_ooo_spawn",
                        "content": [{"type": "text", "text": "Spawned successfully."}],
                    }
                ],
                session_id=PARENT,
                timestamp=spawn_ts,
                tool_use_result=_spawn_meta_for(name),
            ),
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_ooo_spawn", "Agent", {"prompt": "go"})],
                session_id=PARENT,
                timestamp=spawn_ts,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_result_before_use_spawn_registers(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Item 1: a spawn tool_result flushed BEFORE its tool_use (same batch) must
    still register the teammate — the stashed structured signal is applied at the
    tool_use (retro-pairing), with spawned_ts anchored to the RESULT's event ts."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 30
    _append_spawn_result_before_use(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.spawn_generation == 1
    assert abs(rec.spawned_ts - base) < 0.01  # anchored to the RESULT event ts

    # And the registry is functional: the sidechain binds + launches.
    key = _key_for(_NAME)
    sc = _write_sidechain(sub_dir, key, _iso(base + 0.05))
    import os

    os.utime(sc, (base + 0.05, base + 0.05))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == set()
    assert act[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }


@pytest.mark.asyncio
async def test_result_before_use_spawn_across_batches(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Item 1 cross-batch: the result lands in batch N, its tool_use in batch N+1
    — the stash persists across batches and the retro-pairing still registers."""
    parent_jsonl, _sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 30
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_split",
                        "content": [{"type": "text", "text": "Spawned successfully."}],
                    }
                ],
                session_id=PARENT,
                timestamp=_iso(base),
                tool_use_result=_spawn_meta_for(_NAME),
            )
        ],
    )
    await monitor.check_for_updates({PARENT})  # batch N: result only
    monitor.pop_sidechain_activity()
    assert _NAME not in monitor._teammate_registry.get(PARENT, {})
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_split", "Agent", {"prompt": "go"})],
                session_id=PARENT,
                timestamp=_iso(base),
            )
        ],
    )
    await monitor.check_for_updates({PARENT})  # batch N+1: the tool_use
    assert monitor._teammate_registry[PARENT][_NAME].spawn_generation == 1


@pytest.mark.asyncio
async def test_result_before_use_wake_relights(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Item 1: a wake tool_result flushed BEFORE its SendMessage tool_use must
    still relight the bound key — the input.to cross-check runs at retro-pair
    time against the tool_use's input."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    ts = _iso(time.time() + 1)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "tu_ooo_w", "content": "ok"}],
                session_id=PARENT,
                timestamp=ts,
                tool_use_result={
                    "success": True,
                    "routing": {"sender": "team-lead", "target": f"@{_NAME}"},
                },
            ),
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu_ooo_w", "SendMessage", {"to": _NAME})],
                session_id=PARENT,
                timestamp=ts,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key in act[PARENT].resumed


@pytest.mark.asyncio
async def test_result_before_use_wake_mismatch_refuses(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, caplog
):
    """Item 1: the input.to cross-check applies on the RETRO path too — a
    mismatching tool_use input refuses the wake + WARNs."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    _write_sidechain(sub_dir, key, _iso(time.time() + 0.1))
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    ts = _iso(time.time() + 1)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "user",
                [{"type": "tool_result", "tool_use_id": "tu_ooo_m", "content": "ok"}],
                session_id=PARENT,
                timestamp=ts,
                tool_use_result={
                    "success": True,
                    "routing": {"sender": "team-lead", "target": f"@{_NAME}"},
                },
            ),
            make_jsonl_entry(
                "assistant",
                [
                    make_tool_use_block(
                        "tu_ooo_m", "SendMessage", {"to": "someone-else"}
                    )
                ],
                session_id=PARENT,
                timestamp=ts,
            ),
        ],
    )
    with caplog.at_level(logging.WARNING):
        await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert key not in act.get(PARENT, ParentSidechainActivity()).resumed
    assert any("!= input.to" in r.message for r in caplog.records)


# ── gen-2 tolerance boundaries (dual-review r1 item 5, fixture-derived) ──


@pytest.mark.asyncio
async def test_gen2_tolerance_boundary_inside_binds(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A gen-2 candidate whose first entry is INSIDE the fixture-derived tolerance
    below the spawn event ts binds (absorbs clock rounding — the observed real
    cross-flush gap is ≤7ms, always AFTER the spawn). r8 item 1: since this first
    entry is BELOW ``spawned_ts``, the relight resume ts is floored at the bound
    file's OWN first entry (``min(spawned_ts, first_ts) - ε``), NOT ``spawned_ts -
    ε`` — so nothing from the bound file (incl. a trailing end_turn ≥ its first
    entry) can be shielded by the runtime resume gate."""
    from cctelegram.session_monitor import TEAMMATE_GEN2_FIRST_TS_TOLERANCE_S as TOL

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    t2 = base + 10
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(t2))
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    key = _key_for(_NAME, "eeee5555eeee5555")
    sc = _write_sidechain(sub_dir, key, _iso(t2 - TOL * 0.5))  # inside tolerance
    import os

    os.utime(sc, (t2 + 0.01, t2 + 0.01))
    first_ts = monitor._read_first_entry_ts(sc)  # what the bind floors against
    await monitor.check_sidechain_updates({PARENT})
    rec = monitor._teammate_registry[PARENT][_NAME]
    act = monitor.pop_sidechain_activity()
    assert act[PARENT].launched == set()
    assert first_ts is not None and first_ts < rec.spawned_ts  # below-spawn bind
    assert act[PARENT].resumed == {
        key: min(rec.spawned_ts, first_ts) - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    # The floor is strictly below the bound file's first entry (r8 item 1).
    assert act[PARENT].resumed[key] < first_ts


@pytest.mark.asyncio
async def test_gen2_tolerance_boundary_beyond_rejects(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A gen-2 candidate whose first entry precedes the spawn by MORE than the
    fixture-derived tolerance is a stale sibling → quarantined, never bound (the
    r1 item-5 poisoning shape: a file written 0.2-0.4s pre-respawn now FAILS)."""
    from cctelegram.session_monitor import TEAMMATE_GEN2_FIRST_TS_TOLERANCE_S as TOL

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    t2 = base + 10
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(t2))
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()

    key = _key_for(_NAME, "ffff6666ffff6666")
    sc = _write_sidechain(sub_dir, key, _iso(t2 - (TOL + 0.05)))  # beyond tolerance
    import os

    os.utime(sc, (t2 + 0.01, t2 + 0.01))
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    assert act.get(PARENT, ParentSidechainActivity()).launched == set()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert key in rec.retired_keys  # quarantined (deterministically stale)


# ── ownership-field PRESENCE checks (dual-review r1 item 6b, Codex P3) ───


@pytest.mark.parametrize("falsy", ["", None])
@pytest.mark.parametrize(
    "ownership_field", ["agentId", "taskId", "backgroundTaskId", "resumedAgentId"]
)
def test_spawn_disjoint_refuses_present_but_falsy_ownership_field(
    ownership_field, falsy
):
    """Item 6b: the guard is key-PRESENCE, not truthiness — an ownership field
    present with an empty/None value is still another lane's shape."""
    meta = dict(_spawn_meta())
    meta[ownership_field] = falsy
    assert teammate_spawn_info_from_meta(meta) is None


@pytest.mark.parametrize("falsy", ["", None])
def test_send_target_refuses_present_but_falsy_ownership_field(falsy):
    meta = {
        "success": True,
        "routing": {"target": "@explore-x"},
        "resumedAgentId": falsy,
    }
    assert teammate_send_target_from_meta(meta) is None


# ── pre-registration key retraction + resumed-lane relight (r2 P1) ───────


@pytest.mark.asyncio
async def test_registration_retracts_preexisting_unresolved_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """Dual-review r2 P1 (BOTH engines, probe-reproduced): an already-tracked
    candidate emits run-state ticks as a LEGACY agent BEFORE the spawn tool_result
    parses (the registry doesn't exist yet). If registration then leaves it
    UNRESOLVED (indeterminate gate), the already-recorded key would stay live to
    the 2h TTL while all future writes are dark. Registration must RETRACT it —
    an unconditional teammate-done — WITHOUT retiring/severing (it stays
    bind-eligible)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)

    # Pre-spawn: the candidate is tracked and TICKS as a legacy agent (the
    # malformed first line keeps the gate indeterminate forever; later lines
    # parse fine and fed run-state pre-registration).
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text("this is never json\n")
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "pre-spawn work"}],
                timestamp=_iso(time.time()),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    pre = monitor.pop_sidechain_activity()
    assert key in pre[PARENT].ticks  # the legacy live key exists on the runtime

    # The spawn registers the name; the candidate stays unresolved → RETRACT.
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    # r3 P1: the retraction rides its DISTINCT provenance slot — never the
    # genuine-park lane (whose unparseable dominance + parks-after-resumed
    # fan-out order would tombstone a same-tick bind).
    assert key in act[PARENT].retraction_dones
    assert key not in act[PARENT].teammate_parks
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert key not in rec.retired_keys  # NOT retired — stays bind-eligible
    assert f"sub:{PARENT}:agent-{key}" not in monitor._severed_teammate_stems.get(
        PARENT, set()
    )

    # Future writes stay dark (classification), and no re-retraction spam.
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "post-spawn work"}],
                timestamp=_iso(time.time() + 1),
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    act2 = monitor.pop_sidechain_activity()
    assert act2.get(PARENT, ParentSidechainActivity()).ticks == {}


@pytest.mark.asyncio
async def test_registration_retracts_both_keys_on_ambiguity(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r2 P1 ambiguity variant: TWO pre-tracked candidates both pass the gate at
    registration → bind NONE + sticky-ambiguous → BOTH potentially pre-existing
    keys are retracted (done'd), neither retired; rotation resolves later."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time()
    k1 = _key_for(_NAME, "aaaa1111bbbb2222")
    k2 = _key_for(_NAME, "cccc3333dddd4444")
    for k in (k1, k2):
        _write_sidechain(sub_dir, k, _iso(base))
    await monitor.check_sidechain_updates({PARENT})  # track both (legacy)
    monitor.pop_sidechain_activity()

    # Spawn at base+1 — both first entries within the gen-1 skew → both pass →
    # ambiguous → both unbound → both retracted.
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base + 1)
    )
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.ambiguous is True and rec.current_key is None
    assert {k1, k2} <= act[PARENT].retraction_dones  # the distinct slot (r3 P1)
    assert k1 not in act[PARENT].teammate_parks
    assert k2 not in act[PARENT].teammate_parks
    assert k1 not in rec.retired_keys and k2 not in rec.retired_keys


@pytest.mark.asyncio
async def test_retracted_candidate_bind_relights_via_resumed_lane(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r2 P1 relight: the registration done TOMBSTONES the key, and a runtime
    tombstone no-ops a later ``launched`` (done-before-launch fail-closes) — so
    when the retracted candidate later PASSES the gate and binds, the bind must
    emit through the RESUMED lane (tombstone-popping; resume ts = the
    generation's spawned_ts so a genuine later park still closes)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)

    # Tracked pre-spawn with a MID-WRITE first line (no newline → gate None).
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text('{"type":"assistant","timestamp":"' + _iso(time.time()))
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    monitor.pop_sidechain_activity()

    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert key in act[PARENT].retraction_dones  # the distinct slot (r3 P1)

    # The first line completes (valid, within the gen-1 skew) → the next sweep
    # binds — via RESUMED (never launched), ts STRICTLY BELOW the generation's
    # spawned_ts (r3 item 2: a resume ts of exactly spawned_ts would suppress a
    # genuine park stamped at exactly spawned_ts at the runtime resume gate).
    from cctelegram.session_monitor import TEAMMATE_RETRACT_RESUME_EPSILON_S

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
    assert act2[PARENT].launched == set()  # NOT the tombstone-blocked lane
    assert act2[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }  # the popping lane, strictly below the spawn instant
    assert monitor._teammate_registry[PARENT][_NAME].current_key == key

    # Park/close still works after the relight (park strictly newer than the
    # resume ts) — the monitor emits it; the runtime strict-newer gate closes.
    _append_park(parent_jsonl, _NAME, make_jsonl_entry, _iso(rec.spawned_ts + 60))
    await monitor.check_for_updates({PARENT})
    act3 = monitor.pop_sidechain_activity()
    park_ts, unparseable = act3[PARENT].teammate_parks[key]
    assert unparseable is False and park_ts is not None
    assert park_ts > rec.spawned_ts  # strictly newer → tombstones at the runtime


# ── retro-pair parser-carry cleanup (r2 P2, Codex) ───────────────────────


@pytest.mark.asyncio
async def test_retro_pair_clears_parser_pending_carry(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r2 P2: the retro-paired id's tool_result was already consumed, so the
    PendingToolInfo the parser stores for the LATE tool_use would be retained
    (with its full input) until teardown — one leak per retro-paired spawn/wake.
    The apply seam must clear the persisted parser carry for that id."""
    parent_jsonl, _sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 30
    _append_spawn_result_before_use(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    assert monitor._teammate_registry[PARENT][_NAME].spawn_generation == 1
    # The retro-paired id must NOT be retained in the persisted parser carry.
    assert "tu_ooo_spawn" not in monitor._pending_tools.get(PARENT, {})


# ── stash duplicate-at-cap replacement (r2 P3, Hermes) ───────────────────


def test_stash_duplicate_at_cap_replaces_in_place(monitor):
    """r2 P3: a duplicate tool_use_id arriving at capacity must REPLACE its
    existing slot — never evict an unrelated oldest signal first (which left the
    stash one short and dropped a live spawn/wake)."""
    from cctelegram.session_monitor import _EARLY_TEAMMATE_SIGNALS_MAX

    meta = _spawn_meta_for(_NAME)
    for i in range(_EARLY_TEAMMATE_SIGNALS_MAX):
        monitor._stash_early_teammate_signal(PARENT, f"tu{i}", meta, _iso(1000.0 + i))
    stash = monitor._early_teammate_signals[PARENT]
    assert len(stash) == _EARLY_TEAMMATE_SIGNALS_MAX

    # Duplicate at cap: replace in place — same size, nothing unrelated dropped.
    monitor._stash_early_teammate_signal(PARENT, "tu5", meta, _iso(9999.0))
    assert len(stash) == _EARLY_TEAMMATE_SIGNALS_MAX
    assert set(stash) == {f"tu{i}" for i in range(_EARLY_TEAMMATE_SIGNALS_MAX)}
    assert stash["tu5"][2] == _iso(9999.0)  # the replacement took

    # A genuinely NEW id at cap evicts exactly the oldest.
    monitor._stash_early_teammate_signal(PARENT, "tu_new", meta, _iso(10000.0))
    assert len(stash) == _EARLY_TEAMMATE_SIGNALS_MAX
    assert "tu0" not in stash and "tu_new" in stash


@pytest.mark.asyncio
async def test_same_tick_retraction_then_bind_cancels_retraction(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r3 P1 (BOTH engines, probe-reproduced): registration retracts in
    check_for_updates, then the candidate's indeterminate first line completes
    BEFORE the same tick's sidechain scan and the bind lands in the SAME
    activity record. The bind must CANCEL the pending retraction (its premise —
    "unbound at registration" — is falsified); the drained record carries ONLY
    the resumed relight, so the fan-out can never tombstone the just-bound key."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    key = _key_for(_NAME)
    from cctelegram.session_monitor import TEAMMATE_RETRACT_RESUME_EPSILON_S

    # Tracked pre-spawn with a MID-WRITE first line (gate None at registration).
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text('{"type":"assistant","timestamp":"' + _iso(time.time()))
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    monitor.pop_sidechain_activity()

    # ONE tick, NO pop in between: registration (→ retraction) …
    _append_spawn(parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block)
    await monitor.check_for_updates({PARENT})
    # … the first line completes between the two scans (the race) …
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
    # … and the sidechain scan of the SAME tick binds.
    await monitor.check_sidechain_updates({PARENT})
    act = monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    assert rec.current_key == key
    assert act[PARENT].retraction_dones == set()  # CANCELLED at bind
    assert act[PARENT].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert act[PARENT].launched == set()
    assert key not in act[PARENT].teammate_parks  # genuine-park lane untouched


# ── orphan-park buffer hygiene (r5 P1 — TTL + cap) ───────────────────────


@pytest.mark.asyncio
async def test_orphan_park_buffer_cap_and_ttl_hygiene(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r5 P1 buffer hygiene: parks for never-registered names cannot grow the
    buffer unboundedly — the per-parent cap evicts the OLDEST name only for a
    genuinely NEW name (an existing name replace-merges in place), and a
    TTL-expired entry is discarded at the drain instead of closing a fresh
    generation's key."""
    import time as _time

    from cctelegram.handlers.response_builder import TeammateIdle
    from cctelegram.session_monitor import (
        _ORPHAN_PARK_MAX_NAMES,
        _ORPHAN_PARK_TTL_S,
        _OrphanPending,
        _PendingPark,
    )

    parent_jsonl, _sub_dir = _setup_parent(monitor, tmp_path)

    # Fill the cap with distinct never-registered names.
    for i in range(_ORPHAN_PARK_MAX_NAMES):
        monitor._retain_orphan_teammate_park(
            PARENT,
            TeammateIdle(name=f"tm{i}", park_ts=100.0 + i, park_ts_unparseable=False),
        )
    buf = monitor._orphan_teammate_parks[PARENT]
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES

    # An EXISTING name at cap replace-merges in place (max park_ts wins) — no
    # unrelated eviction, size unchanged.
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="tm5", park_ts=9999.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert set(buf) == {f"tm{i}" for i in range(_ORPHAN_PARK_MAX_NAMES)}
    assert buf["tm5"].park == _PendingPark(unknown_done=False, ts=9999.0)
    # And the causal reduction holds: an OLDER park never downgrades the slot.
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="tm5", park_ts=50.0, park_ts_unparseable=False)
    )
    assert buf["tm5"].park == _PendingPark(unknown_done=False, ts=9999.0)
    # r6: the WAKE slot rides the same entry, max-on-repeats (the rec's
    # pending_wake reduction mirrored).
    monitor._retain_orphan_teammate_wake(PARENT, "tm5", 500.0)
    monitor._retain_orphan_teammate_wake(PARENT, "tm5", 400.0)  # older ignored
    assert buf["tm5"].wake == 500.0
    assert buf["tm5"].park == _PendingPark(unknown_done=False, ts=9999.0)

    # A genuinely NEW name at cap evicts exactly the oldest.
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="tm_new", park_ts=200.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert "tm0" not in buf and "tm_new" in buf

    # TTL: an entry aged past _ORPHAN_PARK_TTL_S is discarded at the DRAIN —
    # never applied to a fresh generation's pending slot.
    now = _time.time()
    buf["tm_stale"] = _OrphanPending(
        park=_PendingPark(unknown_done=False, ts=now + 100),
        wall=now - _ORPHAN_PARK_TTL_S - 10,
    )
    _append_spawn(
        parent_jsonl, "tm_stale", make_jsonl_entry, make_tool_use_block, _iso(now)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT]["tm_stale"]
    assert rec.pending_park is None  # expired at drain — discarded
    assert "tm_stale" not in monitor._orphan_teammate_parks.get(PARENT, {})

    # And the lazy sweep evicts expired entries at retain time too.
    buf2 = monitor._orphan_teammate_parks.setdefault(PARENT, {})
    buf2["tm_old"] = _OrphanPending(
        park=_PendingPark(unknown_done=False, ts=1.0),
        wall=now - _ORPHAN_PARK_TTL_S - 10,
    )
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="tm_fresh", park_ts=300.0, park_ts_unparseable=False)
    )
    assert "tm_old" not in buf2 and "tm_fresh" in buf2


# ── two-tier eviction priority (r8 item 2 → r9 item 1 — CONVERGED P1) ─────


@pytest.mark.asyncio
async def test_item2_registered_copy_evicted_before_pre_registration_orphan(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r8 item 2 → r9 item 1 (CONVERGED P1, Hermes + Codex, both probe-reproduced):
    the universal park dual-write (r7) copies EVERY park — including the
    high-frequency registered/bound path — into the shared 32-name orphan buffer.
    A blind oldest-eviction then evicted the sole retained pre-registration ORPHAN
    (a park whose name has NO registry rec — its ONLY home) when the buffer filled
    with redundant REGISTERED-name copies. The r9 rule keys the tiers on the DRAIN
    FILTER'S OWN semantics: a GENUINELY STALE registered copy (park_ts <
    rec.spawned_ts — the drain would generation-drop it) is tier-1 REDUNDANT and
    evicts FIRST, so the orphan survives and later binds with its close.

    Probe: retain the sole park for unregistered ``future`` → fill the remaining
    cap-1 slots with distinct GENUINELY-STALE REGISTERED-name parks (park_ts <
    spawned_ts) → the (cap)th distinct stale registered park must evict a STALE
    REGISTERED copy, NOT ``future`` → ``future`` then registers + binds and closes
    via the drained orphan park."""
    from cctelegram.handlers.response_builder import TeammateIdle
    from cctelegram.session_monitor import _ORPHAN_PARK_MAX_NAMES, _TeammateRec

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)

    # 1. The sole pre-registration ORPHAN (no registry rec) — inserted FIRST so it
    #    is the OLDEST entry (a blind next(iter(buf)) drop would take it). No rec ⇒
    #    PROTECTED (tier 2) under the r9 rule.
    monitor._retain_orphan_teammate_park(
        PARENT,
        TeammateIdle(name="future", park_ts=100.0, park_ts_unparseable=False),
    )

    # 2. Fill the rest of the cap with distinct GENUINELY-STALE REGISTERED-name
    #    parks: each name HAS a rec whose spawned_ts (1000.0) is ABOVE the retained
    #    park_ts (100.0 + i), so the drain WOULD generation-drop them → tier-1
    #    REDUNDANT (the r9 predicate). This is the "genuinely stale registered
    #    copy still evicts first" pin.
    recs = monitor._teammate_recs(PARENT)
    for i in range(_ORPHAN_PARK_MAX_NAMES - 1):
        rname = f"reg{i}"
        recs[rname] = _TeammateRec(
            name=rname, teammate_id=None, spawn_generation=1, spawned_ts=1000.0
        )
        monitor._retain_orphan_teammate_park(
            PARENT,
            TeammateIdle(name=rname, park_ts=100.0 + i, park_ts_unparseable=False),
        )
    buf = monitor._orphan_teammate_parks[PARENT]
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert "future" in buf

    # 3. The (cap)th distinct STALE REGISTERED-name park at cap — MUST evict the
    #    oldest tier-1 REDUNDANT entry (reg0, stale), NOT the pre-registration
    #    orphan (future, PROTECTED).
    rname = f"reg{_ORPHAN_PARK_MAX_NAMES - 1}"
    recs[rname] = _TeammateRec(
        name=rname, teammate_id=None, spawn_generation=1, spawned_ts=1000.0
    )
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name=rname, park_ts=500.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert "future" in buf  # the orphan SURVIVES (stale registered copy evicted)
    assert "reg0" not in buf  # the oldest stale registered copy was evicted
    assert rname in buf

    # 4. ``future`` registers + binds → the drained orphan park (100.0) is its
    #    pending_park (a genuine post-spawn park would then close it). The park is
    #    generation-filtered at the drain; a spawn BEFORE the park ts keeps it.
    _append_spawn(
        parent_jsonl, "future", make_jsonl_entry, make_tool_use_block, _iso(50.0)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT]["future"]
    assert rec.pending_park is not None  # drained from the surviving orphan
    assert rec.pending_park.ts == 100.0


@pytest.mark.asyncio
async def test_item2_all_orphans_at_cap_evicts_oldest_orphan(monitor, tmp_path):
    """r8 item 2 → r9 item 1 tier-2 pin: when the buffer is ALL PROTECTED entries
    (here all pre-registration orphans — no registry rec; the TRUE cap bound), a
    NEW name evicts the OLDEST protected entry (insertion order), preserving the
    existing hygiene contract."""
    from cctelegram.handlers.response_builder import TeammateIdle
    from cctelegram.session_monitor import _ORPHAN_PARK_MAX_NAMES

    _setup_parent(monitor, tmp_path)
    for i in range(_ORPHAN_PARK_MAX_NAMES):
        monitor._retain_orphan_teammate_park(
            PARENT,
            TeammateIdle(name=f"orph{i}", park_ts=100.0 + i, park_ts_unparseable=False),
        )
    buf = monitor._orphan_teammate_parks[PARENT]
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    # No registry recs at all → every entry is PROTECTED → tier 1 finds no victim
    # → tier 2 evicts the oldest.
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="orph_new", park_ts=999.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert "orph0" not in buf and "orph_new" in buf  # oldest orphan evicted


@pytest.mark.asyncio
async def test_item1_future_generation_park_under_registered_name_survives_eviction(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r9 item 1 (CONVERGED P1, Hermes + Codex, both probe-reproduced — the exact
    converged probe): a park retained under a STILL-REGISTERED name that belongs to
    a stashed not-yet-registered NEXT generation is the next gen's ONLY close and
    MUST survive eviction. Under the r8 "name has a rec ⇒ redundant" rule that
    copy sat in the evictable tier and 32 registered-stale parks + 1 new name
    evicted it → gen-2 bound pending_park=None → 2h strand.

    Under the r9 rule the copy SURVIVES a rec (park_ts >= rec.spawned_ts, so the
    drain would NOT drop it → PROTECTED tier-2), the 32 genuinely-stale
    registered parks fill tier 1, and the (cap)th stale park evicts a stale copy —
    NOT the future-generation copy. Rotation then drains it into gen-2's
    pending_park and the bind closes."""
    from cctelegram.handlers.response_builder import (
        TeammateIdle,
        TeammateSpawnInfo,
    )
    from cctelegram.session_monitor import _ORPHAN_PARK_MAX_NAMES, _TeammateRec

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    name = "explore"
    k1 = "aexplore-11111111"

    # gen-1 is bound; the gen-2 spawn RESULT is stashed (not applied yet), and the
    # gen-2 PARK arrives while the rec is STILL gen-1. Because the park_ts (135.0)
    # is >= gen-1's spawned_ts (100.0) it is NOT dropped: it applies to gen-1's
    # current_key AND is dual-write retained under the STILL-REGISTERED name.
    recs = monitor._teammate_recs(PARENT)
    recs[name] = _TeammateRec(
        name=name,
        teammate_id="g1",
        spawn_generation=1,
        spawned_ts=100.0,
        current_key=k1,
    )
    monitor._record_teammate_park(
        PARENT, TeammateIdle(name=name, park_ts=135.0, park_ts_unparseable=False)
    )
    monitor.pop_sidechain_activity()  # drop the immediate gen-1 close
    buf = monitor._orphan_teammate_parks[PARENT]
    assert name in buf  # the future-generation copy is retained
    # The retained copy is PROTECTED under the r9 rule: park_ts 135.0 >= gen-1
    # spawned_ts 100.0 → the drain filter would CARRY it (it may be the next gen's
    # only close), so it is NOT tier-1 redundant.
    assert not monitor._orphan_entry_is_generation_droppable(name, buf[name], recs)

    # Fill the rest of the cap with GENUINELY-STALE registered-name parks
    # (park_ts < spawned_ts → drain would drop → tier-1 REDUNDANT), plus one NEW
    # name to trigger eviction. The stale copies must be the victims.
    for i in range(_ORPHAN_PARK_MAX_NAMES - 1):
        rname = f"reg{i}"
        recs[rname] = _TeammateRec(
            name=rname, teammate_id=None, spawn_generation=1, spawned_ts=1000.0
        )
        monitor._retain_orphan_teammate_park(
            PARENT,
            TeammateIdle(name=rname, park_ts=100.0 + i, park_ts_unparseable=False),
        )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    # A NEW name at cap → evicts a tier-1 redundant (stale registered) copy, NOT
    # the future-generation `explore` copy.
    monitor._retain_orphan_teammate_park(
        PARENT,
        TeammateIdle(name="brandnew", park_ts=1.0, park_ts_unparseable=False),
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert name in buf  # the future-generation copy SURVIVED
    assert "reg0" not in buf  # the oldest stale registered copy was evicted
    assert "brandnew" in buf

    # The late gen-2 spawn tool_use rotates the rec to gen-2 and DRAINS the
    # surviving orphan copy into gen-2's pending_park (park_ts 135.0 >= gen-2
    # spawned_ts 130.0 → carries), then a matching sidechain binds and closes.
    monitor._record_teammate_spawn(
        PARENT, TeammateSpawnInfo(name, "g2", "explorer"), 130.0
    )
    rec = monitor._teammate_registry[PARENT][name]
    assert rec.spawn_generation == 2
    assert rec.pending_park is not None  # drained → gen-2's close is present
    assert rec.pending_park.ts == 135.0
    # Bind gen-2 → the pending_park closes it (a genuine post-spawn park).
    k2 = "aexplore-22222222"
    monitor._bind_teammate_key(PARENT, rec, k2, first_entry_ts=130.05)
    activity = monitor.pop_sidechain_activity()[PARENT]
    assert activity.teammate_parks.get(k2) == (135.0, False)  # gen-2 closes


# ── three-tier eviction (r10 item 1 — CONVERGED P1: protected splits into ──
# ── speculative same-generation noise vs provable pending-generation value) ─


@pytest.mark.asyncio
async def test_item1_r10_future_park_with_stashed_spawn_survives_all_protected_cap(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r10 item 1 (CONVERGED P1 — Hermes + Codex, both probe-reproduced): r9's
    tier 2 evicted the oldest PROTECTED entry when ALL entries survive the drain
    filter, and r9's universal wake retention makes that state ORDINARY — every
    bound teammate's own wake (``event_ts >= its spawned_ts``) survives the filter.
    Probe: a gen-2 park retained under bound gen-1 name ``future`` WITH its gen-2
    spawn STASHED (so it is PROVABLY the pending generation's only close) + 31
    ordinary bound-name self-WAKES (all >= their spawned_ts → all protected) → all
    32 protected → a 33rd name must evict a tier-2 SPECULATIVE wake, NOT ``future``
    (tier 3, provable). Then the late gen-2 spawn drains ``future``'s park + the
    bind closes.

    Under r9 (oldest-protected fallback) ``future`` — inserted first, hence the
    oldest — is evicted → the late gen-2 spawn drains nothing → 2h strand."""
    from cctelegram.handlers.response_builder import (
        TeammateIdle,
        TeammateSpawnInfo,
    )
    from cctelegram.session_monitor import _ORPHAN_PARK_MAX_NAMES, _TeammateRec

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    name = "future"
    k1 = "afuture-11111111"

    # gen-1 bound; its gen-2 spawn RESULT is STASHED (result-before-use — the
    # tool_use has not arrived, so no rotation yet). The stash makes ``future``
    # PROVABLE (tier 3): its retained copy is the pending generation's only close.
    recs = monitor._teammate_recs(PARENT)
    recs[name] = _TeammateRec(
        name=name,
        teammate_id="g1",
        spawn_generation=1,
        spawned_ts=100.0,
        current_key=k1,
    )
    monitor._stash_early_teammate_signal(
        PARENT, "tu_future_g2", _spawn_meta_for(name), _iso(130.0)
    )

    # The gen-2 PARK arrives while STILL gen-1 (park_ts 135.0 >= gen-1 spawned_ts
    # 100.0 → applies to gen-1 AND dual-write retained under the registered name,
    # inserted FIRST so it is the OLDEST entry — an r9 oldest-protected fallback
    # would take it).
    monitor._record_teammate_park(
        PARENT, TeammateIdle(name=name, park_ts=135.0, park_ts_unparseable=False)
    )
    monitor.pop_sidechain_activity()  # drop the immediate gen-1 close
    buf = monitor._orphan_teammate_parks[PARENT]
    assert name in buf

    # Fill the rest of the cap with 31 ORDINARY bound-name SELF-WAKES: each name
    # HAS a rec (gen-1, spawned_ts 200.0) and its retained wake (300.0 + i) is
    # >= its spawned_ts, so the drain filter CARRIES it → NOT tier-1 droppable.
    # These are SPECULATIVE (rec present, NO stashed spawn) → tier 2.
    for i in range(_ORPHAN_PARK_MAX_NAMES - 1):
        rname = f"reg{i}"
        recs[rname] = _TeammateRec(
            name=rname, teammate_id=None, spawn_generation=1, spawned_ts=200.0
        )
        monitor._retain_orphan_teammate_wake(PARENT, rname, 300.0 + i)
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    # ALL 32 entries survive the drain filter (none is tier-1 droppable).
    assert all(
        not monitor._orphan_entry_is_generation_droppable(n, e, recs)
        for n, e in buf.items()
    )

    # A NEW name at cap → tier 1 empty (all protected) → tier 2 evicts the OLDEST
    # SPECULATIVE wake (reg0), NOT ``future`` (tier 3, provable via the stash).
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="brandnew", park_ts=1.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert name in buf  # the future-generation copy SURVIVED (provable)
    assert "reg0" not in buf  # the oldest speculative same-gen wake was evicted
    assert "brandnew" in buf

    # The late gen-2 spawn tool_use rotates + DRAINS the surviving orphan copy into
    # gen-2's pending_park (135.0 >= gen-2 spawned_ts 130.0 → carries), then binds.
    monitor._record_teammate_spawn(
        PARENT, TeammateSpawnInfo(name, "g2", "explorer"), 130.0
    )
    rec = monitor._teammate_registry[PARENT][name]
    assert rec.spawn_generation == 2
    assert rec.pending_park is not None
    assert rec.pending_park.ts == 135.0
    k2 = "afuture-22222222"
    monitor._bind_teammate_key(PARENT, rec, k2, first_entry_ts=130.05)
    activity = monitor.pop_sidechain_activity()[PARENT]
    assert activity.teammate_parks.get(k2) == (135.0, False)  # gen-2 closes


@pytest.mark.asyncio
async def test_item1_r10_all_tier3_provable_at_cap_evicts_oldest(monitor, tmp_path):
    """r10 item 1 tier-3 pin: when EVERY entry is PROVABLE — tiers 1 and 2 both
    empty (no droppable entry, and no SPECULATIVE registered-noise entry) — a NEW
    name evicts the OLDEST provable entry (insertion order), the absolute cap
    bound. Here every entry is a no-rec pre-registration orphan (provable — its
    park is its only home), so the bound holds exactly as before r10."""
    from cctelegram.handlers.response_builder import TeammateIdle
    from cctelegram.session_monitor import _ORPHAN_PARK_MAX_NAMES

    _setup_parent(monitor, tmp_path)
    for i in range(_ORPHAN_PARK_MAX_NAMES):
        monitor._retain_orphan_teammate_park(
            PARENT,
            TeammateIdle(name=f"orph{i}", park_ts=100.0 + i, park_ts_unparseable=False),
        )
    buf = monitor._orphan_teammate_parks[PARENT]
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    # No registry recs → tier 1 empty (no droppable) AND tier 2 empty (no rec ⇒
    # never speculative) → tier 3 evicts the oldest.
    monitor._retain_orphan_teammate_park(
        PARENT, TeammateIdle(name="orph_new", park_ts=999.0, park_ts_unparseable=False)
    )
    assert len(buf) == _ORPHAN_PARK_MAX_NAMES
    assert "orph0" not in buf and "orph_new" in buf  # oldest provable orphan evicted


# ── wake universal dual-write (r9 item 2 — Codex P2) ─────────────────────


@pytest.mark.asyncio
async def test_item2_wake_dual_write_keeps_next_generation_bind_live(
    monitor,
    tmp_path,
):
    """r9 item 2 (Codex P2, probe-reproduced — the exact replay): a wake received
    while an OLD generation is bound is applied ONLY to the old key AND (the fix)
    universally orphan-retained RAW, so a stashed NEXT generation's later wake is
    not lost. Replay: gen-1 bound → gen-2 spawn result stashed → gen-2 parks at T4
    (135.0, r7 park dual-write) → gen-2 wakes at T5 (140.0) → late gen-2 tool_use
    rotates. Pre-fix the park survived via retention but the wake was applied only
    to gen-1, so gen-2 bound with the T4 park and NO pending wake → tombstoned
    although T5 proved it resumed. Post-fix the wake's RAW copy drains into gen-2's
    pending_wake and WINS the wake-vs-park arbitration → LIVE."""
    from cctelegram.handlers.response_builder import (
        TeammateIdle,
        TeammateSpawnInfo,
    )
    from cctelegram.session_monitor import _TeammateRec

    _setup_parent(monitor, tmp_path)
    name = "explore"
    k1 = "aexplore-11111111"
    k2 = "aexplore-22222222"

    # gen-1 bound; the gen-2 spawn RESULT is stashed (not applied yet).
    recs = monitor._teammate_recs(PARENT)
    recs[name] = _TeammateRec(
        name=name,
        teammate_id="g1",
        spawn_generation=1,
        spawned_ts=100.0,
        current_key=k1,
    )
    # gen-2 park (T4=135.0) then gen-2 wake (T5=140.0) arrive while STILL gen-1.
    monitor._record_teammate_park(
        PARENT, TeammateIdle(name=name, park_ts=135.0, park_ts_unparseable=False)
    )
    monitor._record_teammate_wake(PARENT, name, 140.0)
    monitor.pop_sidechain_activity()  # drop the immediate gen-1 close + resume

    # The late gen-2 tool_use applies the spawn → rotation to gen-2 → drain the
    # orphan-retained PAIR: park 135.0 AND wake 140.0 both >= gen-2 spawned_ts
    # 130.0 → carry into pending_park / pending_wake.
    monitor._record_teammate_spawn(
        PARENT, TeammateSpawnInfo(name, "g2", "explorer"), 130.0
    )
    rec = monitor._teammate_registry[PARENT][name]
    assert rec.spawn_generation == 2
    assert rec.pending_park is not None and rec.pending_park.ts == 135.0
    assert rec.pending_wake == 140.0  # the fix: the wake survived for gen-2

    # Bind gen-2 → pending_wake first (resumed[k2]=140.0), pending_park second
    # (teammate_parks[k2]=(135.0, False)). The runtime resume gate suppresses a
    # done with ts <= resumed, so the older park (135.0 <= 140.0) is suppressed →
    # gen-2 stays LIVE (the wake wins).
    monitor._bind_teammate_key(PARENT, rec, k2, first_entry_ts=130.05)
    activity = monitor.pop_sidechain_activity()[PARENT]
    assert activity.resumed.get(k2) == 140.0  # the newer wake wins
    assert activity.teammate_parks.get(k2) == (135.0, False)  # older park present


@pytest.mark.asyncio
async def test_item2_stale_wake_is_generation_dropped_at_drain(
    monitor,
    tmp_path,
):
    """r9 item 2 stale-wake pin (the r7 item-1 filter must catch a stale wake at
    the DRAIN seam too): a wake whose ts is BELOW the NEXT generation's spawned_ts
    reports a PRIOR leg and must NOT relight the fresh bind. gen-1 bound → gen-1
    wake at T=110.0 (retained RAW) → late gen-2 spawn at spawned_ts=200.0 rotates
    → the drain generation-filters the retained wake (110.0 < 200.0) and DROPS it
    → gen-2 binds with NO pending wake."""
    from cctelegram.handlers.response_builder import TeammateSpawnInfo
    from cctelegram.session_monitor import _TeammateRec

    _setup_parent(monitor, tmp_path)
    name = "explore"
    k1 = "aexplore-11111111"
    k2 = "aexplore-22222222"

    recs = monitor._teammate_recs(PARENT)
    recs[name] = _TeammateRec(
        name=name,
        teammate_id="g1",
        spawn_generation=1,
        spawned_ts=100.0,
        current_key=k1,
    )
    # A gen-1-era wake at 110.0 — retained RAW under the still-gen-1 name.
    monitor._record_teammate_wake(PARENT, name, 110.0)
    monitor.pop_sidechain_activity()
    buf = monitor._orphan_teammate_parks[PARENT]
    assert buf[name].wake == 110.0  # retained raw

    # gen-2 spawns at a much LATER instant → the drain filters the stale wake out.
    monitor._record_teammate_spawn(
        PARENT, TeammateSpawnInfo(name, "g2", "explorer"), 200.0
    )
    rec = monitor._teammate_registry[PARENT][name]
    assert rec.spawn_generation == 2
    assert rec.pending_wake is None  # the stale wake was generation-dropped

    monitor._bind_teammate_key(PARENT, rec, k2, first_entry_ts=200.05)
    activity = monitor.pop_sidechain_activity()[PARENT]
    # Only the always-resumed relight floor (below spawned_ts), never the stale
    # wake at 110.0 — the fresh key is not falsely relit by a prior-leg wake.
    assert activity.resumed.get(k2) is not None
    assert activity.resumed[k2] < 200.0
    assert activity.resumed[k2] != 110.0


# ── rotation re-filters pending signals (r6 rule 2 rationale pin) ────────


@pytest.mark.asyncio
async def test_rotation_refilters_pending_signals_instead_of_clearing(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """r6 rule 2 rationale: pending signals are TIMESTAMP-ATTRIBUTED — generation
    membership is decided by the shared generation filter, never by which rec
    object happened to hold them. At rotation: a pending park/wake with ts >= the
    NEW generation's spawned_ts CARRIES into the new slot; ts < it DROPS; an
    UnknownDone park carries (dominance, fail-dark)."""
    from cctelegram.session_monitor import _PendingPark

    parent_jsonl, _sub_dir = _setup_parent(monitor, tmp_path)
    base = time.time() - 60
    _append_spawn(
        parent_jsonl, _NAME, make_jsonl_entry, make_tool_use_block, _iso(base)
    )
    await monitor.check_for_updates({PARENT})
    monitor.pop_sidechain_activity()
    rec = monitor._teammate_registry[PARENT][_NAME]
    new_spawn = base + 10

    # CARRY: park + wake at/after the new spawn survive the rotation.
    rec.pending_park = _PendingPark(unknown_done=False, ts=new_spawn + 2)
    rec.pending_wake = new_spawn + 3
    monitor._rotate_teammate_generation(PARENT, rec, new_spawn)
    assert rec.pending_park == _PendingPark(unknown_done=False, ts=new_spawn + 2)
    assert rec.pending_wake == new_spawn + 3

    # DROP: signals older than the newest spawn are prior-generation.
    newer_spawn = new_spawn + 20
    rec.pending_park = _PendingPark(unknown_done=False, ts=newer_spawn - 5)
    rec.pending_wake = newer_spawn - 5
    monitor._rotate_teammate_generation(PARENT, rec, newer_spawn)
    assert rec.pending_park is None
    assert rec.pending_wake is None

    # UnknownDone CARRIES (it cannot be generation-checked — fail-dark).
    rec.pending_park = _PendingPark(unknown_done=True, ts=None)
    monitor._rotate_teammate_generation(PARENT, rec, newer_spawn + 20)
    assert rec.pending_park == _PendingPark(unknown_done=True, ts=None)
