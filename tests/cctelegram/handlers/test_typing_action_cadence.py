"""Fix B (2026-07-08) — true typing cadence.

``typing_action_loop`` already fans out its per-route typing sends CONCURRENTLY
(``asyncio.gather``); the sole defect was the fixed post-tick
``sleep(TYPING_ACTION_INTERVAL)`` making start-to-start cadence = tick-elapsed +
3.0s (measured 6-12s live vs Telegram's ~5s TTL → the indicator blinked).

Fix: sleep ``max(TYPING_TICK_FLOOR_S, TYPING_ACTION_INTERVAL - elapsed)`` and a
RATE-LIMITED WARNING when a tick exceeds the interval. The per-iteration body is
extracted (``_typing_action_tick``) for direct-drive tests; the concurrency
tests are PRESERVATION pins (they pass pre-fix by design — the pin exists so a
refactor serializing the gather fails a test).
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram.handlers import status_polling
from cctelegram.handlers.status_polling import (
    TYPING_ACTION_INTERVAL,
    TYPING_TICK_FLOOR_S,
)

_R1 = (1, 10, "@1")
_R2 = (1, 20, "@2")
_R3 = (1, 30, "@3")


# ── elapsed-compensated sleep (RED-first — the true new behavior) ─────────


def test_sleep_delay_compensates_for_elapsed():
    # A tick that took 2s leaves 1s until the next 3s start-to-start boundary.
    assert status_polling._typing_sleep_delay(2.0) == pytest.approx(
        TYPING_ACTION_INTERVAL - 2.0
    )


def test_sleep_delay_floors_a_chronically_over_interval_tick():
    # A tick longer than the interval must not hot-loop — clamp to the floor.
    assert status_polling._typing_sleep_delay(5.0) == TYPING_TICK_FLOOR_S
    # Just under a full interval but within the floor window also clamps.
    assert (
        status_polling._typing_sleep_delay(
            TYPING_ACTION_INTERVAL - TYPING_TICK_FLOOR_S / 2
        )
        == TYPING_TICK_FLOOR_S
    )


def test_sleep_delay_zero_eligible_sleeps_full_interval():
    # A near-instant tick (zero eligible routes) sleeps ~the whole interval.
    assert status_polling._typing_sleep_delay(0.0) == pytest.approx(
        TYPING_ACTION_INTERVAL
    )


# ── over-interval WARNING + rate limit ───────────────────────────────────


def test_over_interval_warning_is_rate_limited(monkeypatch):
    monkeypatch.setattr(status_polling, "_last_typing_overrun_warn_at", 0.0)
    # elapsed <= interval never warns.
    assert status_polling._maybe_warn_typing_overrun(1.0, now=100.0) is False
    # First over-interval tick warns.
    assert status_polling._maybe_warn_typing_overrun(9.0, now=100.0) is True
    # A second over-interval tick within the rate-limit window does NOT warn.
    assert status_polling._maybe_warn_typing_overrun(9.0, now=101.0) is False
    # Past the window it warns again.
    assert (
        status_polling._maybe_warn_typing_overrun(
            9.0, now=100.0 + status_polling._TYPING_OVERRUN_WARN_INTERVAL_S + 1
        )
        is True
    )


def test_over_interval_warning_logs_warning(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(status_polling, "_last_typing_overrun_warn_at", 0.0)
    with caplog.at_level(logging.WARNING):
        status_polling._maybe_warn_typing_overrun(9.0, now=100.0)
    assert any("typing" in r.message.lower() for r in caplog.records)


# ── _typing_action_tick: concurrency PRESERVATION + gather isolation ─────


@pytest.fixture
def _bindings(monkeypatch):
    """Patch iter_thread_bindings to yield three routes; reset route_runtime.
    The async tests seed typing eligibility themselves (via mark_inbound_sent)."""
    from cctelegram import route_runtime

    route_runtime.reset_for_tests()
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_thread_bindings",
        lambda: [(u, t, w) for (u, t, w) in (_R1, _R2, _R3)],
    )
    yield
    route_runtime.reset_for_tests()


async def _make_eligible(*routes) -> None:
    from cctelegram import route_runtime

    for r in routes:
        await route_runtime.mark_inbound_sent(r)  # RUNNING → typing_eligible


@pytest.mark.asyncio
async def test_tick_fans_out_concurrently(_bindings, monkeypatch):
    """PRESERVATION pin (NOT RED): all eligible sends are in-flight before any
    completes — a refactor serializing the gather fails this."""
    await _make_eligible(_R1, _R2, _R3)
    started: list[tuple] = []
    gate = asyncio.Event()

    async def _fake_send(bot, user_id, thread_id, wid):
        started.append((user_id, thread_id, wid))
        await gate.wait()

    monkeypatch.setattr(status_polling, "_send_typing_action", _fake_send)

    task = asyncio.create_task(status_polling._typing_action_tick(object()))
    # Yield until all three have STARTED (none can finish — the gate is closed).
    for _ in range(100):
        if len(started) == 3:
            break
        await asyncio.sleep(0)
    assert len(started) == 3  # all concurrent, before any completes
    assert not task.done()
    gate.set()
    await task


@pytest.mark.asyncio
async def test_tick_gather_isolates_one_route_exception(_bindings, monkeypatch):
    """One route's send raising does not abort the others (gather
    return_exceptions=True) — the tick never propagates."""
    await _make_eligible(_R1, _R2, _R3)
    ran: list[tuple] = []

    async def _fake_send(bot, user_id, thread_id, wid):
        ran.append((user_id, thread_id, wid))
        if (user_id, thread_id, wid) == _R2:
            raise RuntimeError("boom")

    monkeypatch.setattr(status_polling, "_send_typing_action", _fake_send)
    await status_polling._typing_action_tick(object())  # must NOT raise
    assert set(ran) == {_R1, _R2, _R3}


@pytest.mark.asyncio
async def test_tick_skips_non_eligible_routes(monkeypatch):
    """A zero-eligible tick does no sends (and the loop then sleeps the full
    interval via _typing_sleep_delay(~0))."""
    from cctelegram import route_runtime

    route_runtime.reset_for_tests()
    monkeypatch.setattr(
        status_polling.session_manager,
        "iter_thread_bindings",
        lambda: [(u, t, w) for (u, t, w) in (_R1, _R2)],
    )
    calls: list = []

    async def _fake_send(bot, user_id, thread_id, wid):
        calls.append((user_id, thread_id, wid))

    monkeypatch.setattr(status_polling, "_send_typing_action", _fake_send)
    await status_polling._typing_action_tick(object())
    assert calls == []  # no route is typing_eligible (all idle)
    route_runtime.reset_for_tests()
