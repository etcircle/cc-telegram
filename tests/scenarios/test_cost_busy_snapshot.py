"""Scenario: /cost + /usage on a BUSY pane post a bridge-side snapshot.

Black-box at the Telegram command seam: a bound topic whose tmux pane is
mid-generation. ``/cost`` (and its ``/usage`` twin) must refuse to inject any
keystroke (typing "/cost"+Enter into a live pane is the /esc hazard) AND — the
v5 change — reply with a bridge-side "cost snapshot" (context % from
route_runtime) instead of a dead-end refusal. The two commands share the one
``_run_usage_overlay`` scaffold, so they behave identically (the grouping pin).
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime
from tests.conftest import ScenarioHarness, make_update_command

pytestmark = pytest.mark.scenario

_SEP = "─" * 56

BUSY_PANE = f"""\
✻ Cooking… (esc to interrupt)

{_SEP}
❯
{_SEP}
  esc to interrupt
"""


def _seed(scenario: ScenarioHarness, thread_id: int = 42) -> str:
    wid = scenario.add_window(window_name="proj", pane_text=BUSY_PANE)
    scenario.bind_thread(thread_id, wid, session_id="sess-cost")
    # Seed a context-usage reading so the snapshot has a metric to show.
    route = (scenario.user_id, thread_id, wid)
    route_runtime.update_context_usage(route, 90_000, None)
    return wid


@pytest.mark.asyncio
async def test_cost_busy_pane_posts_snapshot_no_keystrokes(
    scenario: ScenarioHarness,
) -> None:
    wid = _seed(scenario)
    update = make_update_command("cost", thread_id=42)

    await bot_module.cost_command(update, scenario.context)

    # Zero keystrokes into the busy pane.
    assert not [k for k in scenario.tmux.sent_keys if k[0] == wid]
    # A snapshot card was replied with the context %.
    reply = update.message.reply_text
    reply.assert_awaited()
    body = (
        reply.call_args.args[0]
        if reply.call_args.args
        else reply.call_args.kwargs.get("text", "")
    )
    assert "45%" in body  # 90000/200000


@pytest.mark.asyncio
async def test_usage_busy_pane_identical(scenario: ScenarioHarness) -> None:
    wid = _seed(scenario)
    update = make_update_command("usage", thread_id=42)

    await bot_module.usage_command(update, scenario.context)

    assert not [k for k in scenario.tmux.sent_keys if k[0] == wid]
    reply = update.message.reply_text
    reply.assert_awaited()
    body = (
        reply.call_args.args[0]
        if reply.call_args.args
        else reply.call_args.kwargs.get("text", "")
    )
    assert "45%" in body
