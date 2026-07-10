"""Scenario: the full agent-teams teammate lifecycle at the busy-signal seam
(GH #46 PR-2) — spawn → work (typing ON) → park (typing OFF) → wake (typing back
ON) → a delayed STALE park (stays ON) → a final park (OFF).

Black-box at the public seams — no monkeypatch of run-state internals:

  - the bot's ``apply_sidechain_activity`` fan-out (the sink the monitor's
    teammate registry feeds — a launched key at bind, a ``resumed`` map at wake,
    a ``teammate_parks`` entry at park), and
  - ``transcript_event_adapter.dispatch_transcript_event`` for the parent
    lifecycle,

with reads only from ``route_runtime.snapshot(route)``.

PR-2 makes a teammate a FIRST-CLASS background key: its launched key survives
the parent's end-of-turn (typing stays on across parent turns), a wake relights
it, and a park is its ONLY close signal (a teammate leg ends in plain text — no
sidechain end-of-turn, no ``<task-notification>``). The TEAMMATE done shares the
SIDECHAIN resume ts-gate PLUS a TEAMMATE-only stale-vs-activity gate, so a
redelivered OLD park never darkens a demonstrably-working teammate mid-leg.
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.session_monitor import (
    ParentSidechainActivity,
    SidechainTick,
    TranscriptEvent,
)
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SID = "sess-teammate"
# The teammate's normalized background key (== agent-<key> sidechain stem minus
# the ``agent-`` prefix; ``a<name>-<hex>``).
_KEY = "aexplore-backend-11223344aabbccdd"


def _event(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id=_SID,
        role="assistant",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=None,
        text="",
        image_data=None,
    )
    defaults.update(kw)
    return TranscriptEvent(**defaults)


async def _bind_idle_route(scenario: ScenarioHarness):
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42, window_id=wid, display_name="repo", cwd="/repo", session_id=_SID
    )
    route = (scenario.user_id, 42, wid)
    await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="text", text="spawn a teammate"), [route]
    )
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="text",
            stop_reason="end_turn",
            text="spawned",
            timestamp="2026-07-09T10:00:00.000Z",
        ),
        [route],
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state in (
        route_runtime.RunState.IDLE_RECENT,
        route_runtime.RunState.IDLE_CLEARED,
    )
    return route


@pytest.mark.asyncio
async def test_teammate_full_lifecycle_typing(scenario: ScenarioHarness) -> None:
    route = await _bind_idle_route(scenario)

    # BIND — the teammate's launched key lifts the parent-idle route to typing
    # (survives the end-of-turn prune; a first-class background key).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # WORK — a sidechain heartbeat keeps it live (parent stays idle).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(ticks={_KEY: SidechainTick(max_event_ts=100.0)})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # PARK — the teammate reports idle; its park ts (200) is strictly NEWER than
    # the last activity (100), so it tombstones → typing drops promptly.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (200.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()

    # WAKE — a SendMessage nudge resumes the SAME key (resume ts 300 pops the
    # tombstone) → typing back ON.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: 300.0})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # DELAYED STALE PARK — an OLD park (ts 250, < resume 300) is redelivered a
    # later batch. The resume ts-gate keeps the key LIVE (a redelivered old park
    # must never darken a demonstrably-resumed teammate).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (250.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # NOT killed by the stale park
    assert snap.background_agents == (_KEY,)

    # FINAL PARK — the genuine final park (ts 400 > resume 300) tombstones →
    # typing drops for good.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (400.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_preregistration_retract_then_bind_relights_via_resumed(
    scenario: ScenarioHarness,
) -> None:
    """r2 P1 design-constraint pin: the registration retraction (the DISTINCT
    ``retraction_dones`` slot — r3 P1) emits an UNCONDITIONAL done for a
    pre-registration live key, and the runtime tombstone then NO-OPS a later
    ``launched`` (done-before-launch fail-closes) — so the bind-after-retraction
    relight MUST ride the RESUMED lane (the Fix-C tombstone-popping path).
    Sequence: pre-registration legacy tick lifts typing → the retraction done
    drops it → a plain ``launched`` cannot relight (negative control) → the
    bind's ``resumed`` relights → a genuine later park closes."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    # Pre-registration: the candidate ticks as a LEGACY agent (ts-qualified
    # past the parent's end-of-turn) → live key, typing ON.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=eot + 10)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # Registration retraction (the distinct slot) → typing OFF.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(retraction_dones={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # Negative control: a plain launched CANNOT relight the tombstoned key —
    # exactly why the bind must use the resumed lane.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # The bind relight (resumed lane, ts strictly below the generation's
    # spawned_ts — r3 item 2): pops the tombstone → typing back ON.
    spawn_ts = eot + 20
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: spawn_ts - 0.001})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # A genuine current-generation park (strictly newer than the resume ts)
    # closes it — the strict-newer gate passes.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts + 60, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_same_record_retraction_and_bind_resume_nets_live(
    scenario: ScenarioHarness,
) -> None:
    """r3 P1 REQUIRED pin (both engines): ONE aggregated ParentSidechainActivity
    record carrying BOTH the same-tick retraction AND the bind's resumed relight
    — through the PRODUCTION fan-out. The causal apply order (retraction-dones
    FIRST, resumed after: registration precedes bind) nets the key LIVE even
    when the monitor-side cancel was missed — the pre-fix shape applied resumed
    first and the synthetic park after, permanently tombstoning the just-bound
    key (probe-reproduced by both engines)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    # Pre-registration legacy tick → live key.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=eot + 10)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # ONE record: the retraction AND the bind-resume together.
    spawn_ts = eot + 20
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                retraction_dones={_KEY},
                resumed={_KEY: spawn_ts - 0.001},
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # LIVE — never the permanent dark
    assert snap.background_agents == (_KEY,)

    # Genuine later activity keeps flowing (the pre-fix bug blocked it).
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=spawn_ts + 5)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # And the genuine park still closes.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts + 60, False)})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False


@pytest.mark.asyncio
async def test_genuine_unparseable_park_with_same_record_resume_still_tombstones(
    scenario: ScenarioHarness,
) -> None:
    """r3 P1 dominance preservation: the cancel/causal-order fix is scoped to the
    SYNTHETIC retraction slot ONLY — a GENUINE unparseable park (from a real
    idle_notification envelope) arriving in the same record as a resume STILL
    tombstones (parks apply after resumed; unparseable dominance untouched)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: eot + 20},
                teammate_parks={_KEY: (None, True)},  # genuine unparseable park
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # dominance holds — fail-dark
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_park_at_exactly_spawned_ts_closes_relit_key(
    scenario: ScenarioHarness,
) -> None:
    """r3 item 2 REQUIRED boundary pin (Codex P1, probe-confirmed): a genuine
    park stamped at EXACTLY the generation's spawned_ts passes the generation
    filter (only park_ts < spawned_ts drops) — and with the relight resume ts at
    ``spawned_ts - epsilon`` it is STRICTLY newer than the resume, so it CLOSES
    (pre-fix, resume ts == spawned_ts made the runtime resume gate suppress the
    tie and strand typing to the 2h TTL)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None
    spawn_ts = eot + 20

    # The retraction→bind relight, as the monitor emits it (resume strictly
    # below the spawn instant).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(retraction_dones={_KEY})}
    )
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: spawn_ts - 0.001})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # The tie boundary: a genuine park at EXACTLY spawned_ts → typing drops.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


def _make_hybrid_monitor(tmp_path):
    """A REAL SessionMonitor over tmp_path whose parent is _SID, for the r4
    hybrid pins (real check_for_updates → production apply_sidechain_activity)."""
    import json as _json

    from cctelegram.session_monitor import (
        SessionInfo,
        SessionMonitor,
        TrackedSession,
    )

    mon = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    proj = tmp_path / "projects" / "-p"
    proj.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj / f"{_SID}.jsonl"
    parent_jsonl.write_text("")
    sub_dir = proj / _SID / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    mon.state.update_session(
        TrackedSession(
            session_id=_SID,
            file_path=str(parent_jsonl),
            last_byte_offset=0,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=_SID, file_path=parent_jsonl)]

    mon.scan_projects = _scan  # type: ignore[method-assign]

    def _append(entries):
        with open(parent_jsonl, "a") as f:
            for e in entries:
                f.write(_json.dumps(e) + "\n")

    return mon, sub_dir, _append


def _iso_utc(ts: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _spawn_pair(name: str, ts: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_r4",
                        "name": "Agent",
                        "input": {"prompt": "go"},
                    }
                ]
            },
            "sessionId": _SID,
            "timestamp": ts,
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_r4",
                        "content": [{"type": "text", "text": "Spawned successfully."}],
                    }
                ]
            },
            "sessionId": _SID,
            "timestamp": ts,
            "toolUseResult": {
                "status": "teammate_spawned",
                "name": name,
                "teammate_id": f"{name}@team",
                "agent_type": "explorer",
            },
        },
    ]


def _park_entry(name: str, payload_ts: str | None, entry_ts: str) -> dict:
    payload = f'{{"type":"idle_notification","from":"{name}"'
    if payload_ts is not None:
        payload += f',"timestamp":"{payload_ts}"'
    payload += "}"
    return {
        "type": "user",
        "message": {
            "content": (
                "Another Claude session sent a message:\n"
                f'<teammate-message teammate_id="{name}" color="blue">\n'
                f"{payload}\n"
                "</teammate-message>\n"
            )
        },
        "sessionId": _SID,
        "timestamp": entry_ts,
    }


@pytest.mark.asyncio
async def test_no_registry_stale_park_then_spawn_one_batch_binds_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r4 P2 REQUIRED pin (Codex, probe-reproduced): stem pre-discovered at EOF
    (no registry rec) → ONE parent batch carries a delayed park at T1 followed by
    the fresh spawn at T2 → the park lands via the no-registry fallback (no
    spawned_ts existed to filter against), the spawn registers + binds — and the
    bind's RETROACTIVE generation filter drops the stale parseable park, so
    through real check_for_updates + the PRODUCTION fan-out the key is bound AND
    typing lifts (pre-fix: launched then the stale park → tombstoned dark). A
    genuine park at ts > spawned_ts still closes it."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY  # aexplore-backend-11223344aabbccdd

    # Pre-discover the stem at EOF (tracked, NO registry rec yet).
    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 - 1),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 - 1, t2 - 1))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: the delayed park (T1 < T2) THEN the fresh spawn (T2).
    _append([_park_entry(name, _iso_utc(t2 - 2), _iso_utc(t2 - 2))])
    _append(_spawn_pair(name, _iso_utc(t2)))
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    await bot_module.apply_sidechain_activity(act)

    # Bound + LIVE: the stale park was retroactively generation-filtered.
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (key,)

    # A GENUINE park (ts > spawned_ts) still closes it.
    _append([_park_entry(name, _iso_utc(t2 + 60), _iso_utc(t2 + 60))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_no_registry_unparseable_park_then_spawn_still_tombstones(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r4 dominance variant: an UNPARSEABLE pre-registration park (no timestamp —
    it cannot be generation-checked) is KEPT by the retroactive filter and still
    tombstones the bound key through the production fan-out (fail-dark
    preserved; the retroactive drop is scoped strictly to parseable-and-stale)."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY

    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 - 1),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 - 1, t2 - 1))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: an UNPARSEABLE park (no ts) THEN the spawn.
    _append([_park_entry(name, None, _iso_utc(t2 - 2))])
    _append(_spawn_pair(name, _iso_utc(t2)))
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    assert act[_SID].teammate_parks.get(key) == (None, True)  # kept (dominance)
    await bot_module.apply_sidechain_activity(act)

    assert mon._teammate_registry[_SID][name].current_key == key  # still binds
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # fail-dark holds
    assert snap.background_agents == ()
