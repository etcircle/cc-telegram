"""Scenario: the /cost overlay cache is torn down at the real teardown seams.

Black-box at the Telegram handler seam: a cached overlay result for a route
must die when (1) the user sends ``/clear`` through the real
``forward_command_handler`` (the session rotates — a stale usage snapshot must
not survive into the new session), and (2) a bound topic's window vanishes and
``text_handler`` runs the stale-window unbind (a later window reusing the id
must not inherit the cache). Drives the REAL handlers, never usage_cache
directly (review r1 P2). The topic-teardown + monitor-rotation seams are
unit-tested in tests/cctelegram/test_usage_cache.py.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import usage_cache
from tests.conftest import ScenarioHarness, make_update_command, make_update_text

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_clear_command_tears_down_usage_cache(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, session_id="sess-a")
    route = (scenario.user_id, 42, wid)
    usage_cache.record(route, "sess-a", "Total cost: $1.00")
    assert usage_cache.peek(route, "sess-a") is not None

    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    assert usage_cache.peek(route, "sess-a") is None


@pytest.mark.asyncio
async def test_stale_window_unbind_tears_down_usage_cache(
    scenario: ScenarioHarness,
) -> None:
    # Bind the topic to a window id that does NOT exist in tmux — the next
    # text message hits text_handler's stale-window unbind path.
    scenario.bind_thread(thread_id=42, window_id="@99", session_id="sess-b")
    route = (scenario.user_id, 42, "@99")
    usage_cache.record(route, "sess-b", "Total cost: $2.00")
    assert usage_cache.peek(route, "sess-b") is not None

    update = make_update_text("hello", thread_id=42)
    await bot_module.text_handler(update, scenario.context)

    assert usage_cache.peek(route, "sess-b") is None
