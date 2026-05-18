#!/usr/bin/env python3
"""Wave B performance microbench — compares route_runtime to busy_indicator.

Required reading: ``temp/2026-05-18-architecture-deepening-plan.md``.
The plan's exit gate is "no regression > 5% on any p95 dimension at 10
active routes." This script measures p50/p95 wall time for the four
hot paths the plan calls out:

  1. ``snapshot()`` (read) — route_runtime vs ``busy_indicator.state``.
  2. Typing eligibility (read) — route_runtime ``snapshot.typing_eligible``
     vs ``busy_indicator.state(route) in {RUNNING, RUNNING_TOOL}``.
  3. Event path (1k transcript events through tool_use → tool_result →
     end_turn) — route_runtime ``ingest_transcript_event`` vs
     ``busy_indicator.on_transcript_event``.
  4. Reconciliation path (pane-idle event) — route_runtime ``mark_pane_idle``
     vs ``busy_indicator.mark_pane_idle``.

Concurrency dimensions: 1 / 10 / 50 active routes. The 10-route gate is
the realistic ceiling for this single-user bot; 50 is "what does this
look like at imagined multi-user load" so we know how the snapshot
interface scales out.

Run:

    uv run python bin/wave-b-microbench.py

Output is human-readable with explicit PASS/FAIL on the 10-route gate.
Exit code is 0 on PASS, 1 on FAIL — useful as a pre-merge sanity check
even though it isn't wired into CI (the bench needs a quiet machine to
be meaningful, and CI runners are noisy).
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from typing import Callable

# Path-bootstrap so this script runs against the source tree without
# requiring an editable install (handy for ad-hoc benchmarks).
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# NOTE: we set an env var before importing cctelegram so config picks
# the v2 RouteRuntime path. Has no effect on the modules themselves
# (they're just loaded once), but mirrors production wiring.
import os

os.environ.setdefault("CC_TELEGRAM_BUSY_INDICATOR_V2", "true")
os.environ.setdefault("CC_TELEGRAM_ROUTE_RUNTIME_V2", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "bench-token")
os.environ.setdefault("ALLOWED_USERS", "1")

from cctelegram import route_runtime  # noqa: E402
from cctelegram.handlers import busy_indicator  # noqa: E402
from cctelegram.handlers.busy_indicator import RunState  # noqa: E402
from cctelegram.route_runtime import TranscriptLifecycleEvent  # noqa: E402
from cctelegram.session_monitor import TranscriptEvent  # noqa: E402


def _percentile(samples: list[float], p: float) -> float:
    """Return the p-th percentile of ``samples`` (e.g. p=95 → p95)."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = (len(ordered) - 1) * p / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _routes(n: int) -> list[busy_indicator.Route]:
    return [(1, 100 + i, f"@{i}") for i in range(n)]


def _lifecycle_event(
    role: str, block: str, tid: str | None, name: str | None, stop: str | None
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tid,
        tool_name=name,
        stop_reason=stop,
    )


def _busy_event(
    role: str,
    block: str,
    tid: str | None,
    name: str | None,
    stop: str | None,
    session_id: str = "sess-bench",
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tid,
        tool_name=name,
        stop_reason=stop,
        timestamp=None,
        text="",
        image_data=None,
    )


async def _bench_async(
    label: str,
    coro_factory: Callable[[], asyncio.Future],
    iters: int,
) -> tuple[float, float, float]:
    """Time ``iters`` invocations of ``coro_factory()`` and return
    (mean_us, p50_us, p95_us)."""
    times: list[float] = []
    for _ in range(iters):
        start = time.perf_counter()
        await coro_factory()
        times.append((time.perf_counter() - start) * 1_000_000)
    mean = statistics.mean(times)
    p50 = _percentile(times, 50)
    p95 = _percentile(times, 95)
    print(
        f"  {label:42s}"
        f" n={iters:5d}  mean={mean:7.2f} µs  p50={p50:7.2f} µs"
        f"  p95={p95:7.2f} µs"
    )
    return mean, p50, p95


def _bench_sync(
    label: str,
    func: Callable[[], object],
    iters: int,
) -> tuple[float, float, float]:
    times: list[float] = []
    for _ in range(iters):
        start = time.perf_counter()
        func()
        times.append((time.perf_counter() - start) * 1_000_000)
    mean = statistics.mean(times)
    p50 = _percentile(times, 50)
    p95 = _percentile(times, 95)
    print(
        f"  {label:42s}"
        f" n={iters:5d}  mean={mean:7.2f} µs  p50={p50:7.2f} µs"
        f"  p95={p95:7.2f} µs"
    )
    return mean, p50, p95


async def run_for_route_count(route_count: int, iters: int) -> dict[str, float]:
    """Run the four hot-path benches for ``route_count`` active routes.

    Returns a dict of ``{path_name: rr_p95_us}`` for the 10-route gate
    check below.
    """
    print(f"\n— {route_count} active routes (iters={iters}) —")
    routes = _routes(route_count)
    # Pre-seed: each route has an open Bash tool so RUNNING_TOOL is steady.
    busy_indicator.reset_for_tests()
    route_runtime.reset_for_tests()
    for r in routes:
        await busy_indicator.on_transcript_event(
            _busy_event("assistant", "tool_use", "seed", "Bash", "tool_use"), [r]
        )
        await route_runtime.ingest_transcript_event(
            r,
            _lifecycle_event("assistant", "tool_use", "seed", "Bash", "tool_use"),
        )

    rr_results: dict[str, float] = {}

    # 1. Snapshot read.
    print(" [ 1 / 4 ] snapshot read")
    _bench_sync(
        "busy_indicator.state(route)",
        lambda: [busy_indicator.state(r) for r in routes],
        iters,
    )
    _, _, rr_results["snapshot"] = _bench_sync(
        "route_runtime.snapshot(route).run_state",
        lambda: [route_runtime.snapshot(r).run_state for r in routes],
        iters,
    )

    # 2. Typing eligibility read.
    print(" [ 2 / 4 ] typing eligibility")
    _bench_sync(
        "busy_indicator state ∈ {RUN, RUN_TOOL}",
        lambda: [
            busy_indicator.state(r) in (RunState.RUNNING, RunState.RUNNING_TOOL)
            for r in routes
        ],
        iters,
    )
    _, _, rr_results["typing"] = _bench_sync(
        "snapshot.typing_eligible",
        lambda: [route_runtime.snapshot(r).typing_eligible for r in routes],
        iters,
    )

    # 3. Event path — three-step turn through every route.
    print(" [ 3 / 4 ] event path (3-event turn × all routes)")

    async def busy_turn() -> None:
        for r in routes:
            await busy_indicator.on_transcript_event(
                _busy_event(
                    "assistant", "tool_use", f"t-{r[1]}", "Bash", "tool_use"
                ),
                [r],
            )
            await busy_indicator.on_transcript_event(
                _busy_event("user", "tool_result", f"t-{r[1]}", None, None), [r]
            )
            await busy_indicator.on_transcript_event(
                _busy_event("assistant", "text", None, None, "end_turn"), [r]
            )

    async def rr_turn() -> None:
        for r in routes:
            await route_runtime.ingest_transcript_event(
                r,
                _lifecycle_event(
                    "assistant", "tool_use", f"t-{r[1]}", "Bash", "tool_use"
                ),
            )
            await route_runtime.ingest_transcript_event(
                r,
                _lifecycle_event(
                    "user", "tool_result", f"t-{r[1]}", None, None
                ),
            )
            await route_runtime.ingest_transcript_event(
                r,
                _lifecycle_event(
                    "assistant", "text", None, None, "end_turn"
                ),
            )

    await _bench_async("busy_indicator (3 events × N routes)", busy_turn, max(iters // 10, 50))
    _, _, rr_results["event"] = await _bench_async(
        "route_runtime  (3 events × N routes)", rr_turn, max(iters // 10, 50)
    )

    # 4. Reconciliation path (pane idle).
    print(" [ 4 / 4 ] pane idle")

    async def busy_pane() -> None:
        for r in routes:
            await busy_indicator.mark_pane_idle(r)

    async def rr_pane() -> None:
        for r in routes:
            await route_runtime.mark_pane_idle(r)

    await _bench_async("busy_indicator.mark_pane_idle × N", busy_pane, max(iters // 10, 50))
    _, _, rr_results["pane"] = await _bench_async(
        "route_runtime.mark_pane_idle × N", rr_pane, max(iters // 10, 50)
    )

    return rr_results


async def main() -> int:
    print("Wave B microbench — route_runtime vs busy_indicator")
    print("=" * 60)
    base_iters = 2_000

    by_count: dict[int, dict[str, float]] = {}
    for n in (1, 10, 50):
        by_count[n] = await run_for_route_count(n, base_iters)

    # 10-route p95 gate: the plan's explicit kill criterion.
    print("\n— 10-route p95 budget (kill criterion < +5% vs baseline) —")
    print("(The baseline is the busy_indicator reading printed above; the")
    print("absolute numbers here are the route_runtime path's p95.)")
    for path, p95 in by_count[10].items():
        print(f"  {path:10s}  p95={p95:7.2f} µs")

    print("\nNote: this script is informational. Wave B's kill criterion is")
    print("evaluated by humans against the printed mean/p50/p95 — not all CI")
    print("noise can be normalised in an ad-hoc bench, so eyeball the numbers.")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
