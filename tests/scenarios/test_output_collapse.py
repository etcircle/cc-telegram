"""Scenario: post-turn digest collapse (W1) + sub-agent collapse (W2).

Plan v4 PR-2 (temp/2026-06-11-telegram-output-compactness-plan-v4.md §2/§3):
finished tool history collapses instead of persisting as scrollback.

  - W1 ``summary`` (the ``standard`` default): when the turn finalizes, the
    activity digest collapses to ONE line — run-state header + tool /
    sub-agent counts + duration. No body lines, no "Activity:" line. Later
    refreshes keep it collapsed (stable text). ``verbose`` (``keep``) stays
    today-shaped — pinned by the existing test_tool_lifecycle scenario.
  - W1 ``delete`` (the /settings "Done card" knob): finalize deletes the
    card message and drops the state slot; a later refresh cannot resurrect
    it; the next turn starts a fresh card.
  - W2 ``summary``: a sidechain's own end-of-turn (final text with
    stop_reason) collapses its ↳ card to one line; a sidechain that never
    flushes a visible final text is collapsed by the parent-finalize
    backstop; a late sidechain block after collapse does NOT re-inflate the
    card. The 🤖✅ report path is untouched.
  - /settings exposes the two PR-2 knob rows (Done card, Sub-agent cards).

/history remains the full-fidelity escape hatch (unchanged).
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import message_queue
from cctelegram.handlers.callback_data import CB_SETTINGS
from cctelegram.session_monitor import NewMessage
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_THREAD_ID = 42
_OWNER_ID = 12345


async def _drain_route(route: tuple[int, int, str]) -> None:
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


def _bind(scenario: ScenarioHarness, *, session_id: str = "sess-1"):
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id=session_id,
    )
    return wid, (scenario.user_id, _THREAD_ID, wid)


async def _tool_turn(scenario: ScenarioHarness, route, *, tool_use_id: str = "t1"):
    """Drive one tool_use + tool_result pair through the public seam."""
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**(ls -la)",
            content_type="tool_use",
            tool_use_id=tool_use_id,
            tool_name="Bash",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**(ls -la)\n  ⎿  Output 3 lines",
            content_type="tool_result",
            tool_use_id=tool_use_id,
            tool_name="Bash",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)


async def _finalize_turn(scenario: ScenarioHarness, route):
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="done with the task",
            content_type="text",
            role="assistant",
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    await _drain_route(route)


def _digest_messages(scenario: ScenarioHarness) -> list[str]:
    """All texts that ever painted the activity digest (sends + edits)."""
    texts = []
    for s in scenario.bot.sent:
        if s.method in ("send_message", "edit_message_text"):
            text = s.kwargs.get("text", "")
            if "— repo" in text:
                texts.append(text)
    return texts


@pytest.mark.asyncio
async def test_summary_collapse_on_done(scenario: ScenarioHarness) -> None:
    """standard (digest_on_done=summary): the finalized digest is ONE line —
    counts + duration, no body lines, no Activity: line; the assistant text
    still arrives as its own message."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    await _tool_turn(scenario, route)
    await _finalize_turn(scenario, route)

    digests = _digest_messages(scenario)
    assert digests, "a digest must have been painted"
    final = digests[-1]
    assert "✅ Done — repo" in final
    assert "1 tool" in final
    assert "Activity:" not in final
    assert "•" not in final
    assert "\n" not in final.strip(), "collapsed digest is a single line"
    # The prose still arrived.
    assert any(
        "done with the task" in s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if s.method == "send_message"
    )


@pytest.mark.asyncio
async def test_collapsed_digest_stable_across_refresh(
    scenario: ScenarioHarness,
) -> None:
    """A poller-driven refresh after finalize must not re-expand the
    collapsed card (the render is state-derived and frozen)."""
    wid, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    await _tool_turn(scenario, route)
    await _finalize_turn(scenario, route)
    before = _digest_messages(scenario)[-1]

    await message_queue.refresh_activity_digest_if_present(
        scenario.bot, scenario.user_id, _THREAD_ID, wid
    )
    after = _digest_messages(scenario)[-1]
    assert after == before
    assert "•" not in after


@pytest.mark.asyncio
async def test_delete_policy_removes_card_without_resurrection(
    scenario: ScenarioHarness,
) -> None:
    """digest_on_done=delete (the /settings knob): finalize deletes the card
    message; a later refresh is a no-op; the next turn starts fresh."""
    wid, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")
    scenario.session_manager.set_user_setting(scenario.user_id, "done", "delete")

    await _tool_turn(scenario, route)
    # Force the debounced live card out so the delete is observable.
    await message_queue._flush_activity_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID
    )
    digest_sends = [
        s
        for s in scenario.bot.sent
        if s.method == "send_message" and "Activity:" in s.kwargs.get("text", "")
    ]
    assert digest_sends, "the live digest must exist before finalize"
    digest_msg_id = digest_sends[0].message_id

    await _finalize_turn(scenario, route)
    deletes = [s for s in scenario.bot.sent if s.method == "delete_message"]
    assert any(d.kwargs.get("message_id") == digest_msg_id for d in deletes)

    sent_count = len(scenario.bot.sent)
    await message_queue.refresh_activity_digest_if_present(
        scenario.bot, scenario.user_id, _THREAD_ID, wid
    )
    assert len(scenario.bot.sent) == sent_count, "refresh must not resurrect"

    # Next turn paints a fresh card.
    await _tool_turn(scenario, route, tool_use_id="t2")
    await message_queue._flush_activity_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID
    )
    fresh = [
        s
        for s in scenario.bot.sent[sent_count:]
        if s.method == "send_message" and "Activity:" in s.kwargs.get("text", "")
    ]
    assert fresh, "next turn must create a fresh digest card"


@pytest.mark.asyncio
async def test_subagent_card_collapses_on_its_end_of_turn(
    scenario: ScenarioHarness,
) -> None:
    """W2 primary trigger: the sidechain's own final text (stop_reason
    end_turn) collapses its ↳ card to one line."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")
    sub_key = "sub:sess-1:agent-abc123"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Read**(file.py)\n  ⎿  Read 10 lines",
            content_type="tool_use",
            tool_use_id="st1",
            tool_name="Read",
            role="assistant",
            subagent_key=sub_key,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    # Flush the debounced card so the collapse is observable as an edit.
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, sub_key
    )
    live = [
        s
        for s in scenario.bot.sent
        if s.method == "send_message" and "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert live, "the live sub-agent card must exist"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="my final report",
            content_type="text",
            role="assistant",
            subagent_key=sub_key,
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    await _drain_route(route)

    sub_paints = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    final = sub_paints[-1]
    assert "✅" in final
    assert "1 tool" in final
    assert "•" not in final
    assert "Activity:" not in final


@pytest.mark.asyncio
async def test_parent_finalize_backstop_collapses_straggler_subagent_card(
    scenario: ScenarioHarness,
) -> None:
    """W2 backstop: a sidechain with NO visible final text is collapsed when
    the parent turn finalizes (codex r2 P2-3 — the empty-final case)."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")
    sub_key = "sub:sess-1:agent-def456"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Grep**(pattern)\n  ⎿  Found 2 matches",
            content_type="tool_use",
            tool_use_id="st2",
            tool_name="Grep",
            role="assistant",
            subagent_key=sub_key,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, sub_key
    )

    await _finalize_turn(scenario, route)

    sub_paints = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert sub_paints, "the sub-agent card must have been painted"
    final = sub_paints[-1]
    assert "✅" in final
    assert "•" not in final


@pytest.mark.asyncio
async def test_late_sidechain_block_does_not_reinflate_collapsed_card(
    scenario: ScenarioHarness,
) -> None:
    """Tombstone: a sidechain block re-detected AFTER collapse must not
    repaint the play-by-play (plan v4 §3)."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")
    sub_key = "sub:sess-1:agent-late99"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="final words",
            content_type="text",
            role="assistant",
            subagent_key=sub_key,
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    paints_before = len(
        [s for s in scenario.bot.sent if "↳ Sub" in s.kwargs.get("text", "")]
    )

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**(echo late)",
            content_type="tool_use",
            tool_use_id="late1",
            tool_name="Bash",
            role="assistant",
            subagent_key=sub_key,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, sub_key
    )
    paints_after = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert len(paints_after) == paints_before or "•" not in paints_after[-1], (
        "a collapsed card must never re-show play-by-play lines"
    )


# ── Fix 5 PR-B: the Workflow ↳ shape rides the W2 collapse contract + the ────
#   deterministic route-FIFO close collapse (run-id-qualified key). DISPLAY
#   ONLY — synthetic ids, no PII.

_WF_RUN = "wf_run01abcd"
_WF_PREFIX = f"sub:sess-1:{_WF_RUN}:"
_WF_KEY = f"{_WF_PREFIX}agent-aaa111"


@pytest.mark.asyncio
async def test_workflow_card_collapses_on_own_end_of_turn(
    scenario: ScenarioHarness,
) -> None:
    """⭐ B-i path 1 / B-c: a run-id-qualified workflow ↳ card collapses to one
    line on its OWN end-of-turn under summary — identical to the Agent shape."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Read**(syn.py)\n  ⎿  Read 10 lines",
            content_type="tool_use",
            tool_use_id="wst1",
            tool_name="Read",
            role="assistant",
            subagent_key=_WF_KEY,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, _WF_KEY
    )

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="my final report",
            content_type="text",
            role="assistant",
            subagent_key=_WF_KEY,
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    await _drain_route(route)

    sub_paints = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    final = sub_paints[-1]
    assert "✅" in final
    assert "1 tool" in final
    assert "•" not in final
    # The run-id middle segment never reaches the rendered header (id6 only).
    assert _WF_RUN not in final


@pytest.mark.asyncio
async def test_workflow_late_block_does_not_reinflate_after_collapse(
    scenario: ScenarioHarness,
) -> None:
    """B-c tombstone: a workflow keeps writing post-collapse; a late block must
    not re-inflate the play-by-play."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="final words",
            content_type="text",
            role="assistant",
            subagent_key=_WF_KEY,
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    paints_before = len(
        [s for s in scenario.bot.sent if "↳ Sub" in s.kwargs.get("text", "")]
    )

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**(echo late)",
            content_type="tool_use",
            tool_use_id="late1",
            tool_name="Bash",
            role="assistant",
            subagent_key=_WF_KEY,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, _WF_KEY
    )
    paints_after = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert len(paints_after) == paints_before or "•" not in paints_after[-1]


@pytest.mark.asyncio
async def test_empty_final_workflow_card_collapsed_on_bracket_close(
    scenario: ScenarioHarness,
) -> None:
    """⭐ B-i path 3 (Hermes-delta P1-2, end-to-end): an empty-final workflow
    card (lifecycle-only last entry — never self-collapses via path 1) is
    collapsed by the route-FIFO close collapse. The monitor emits a
    NewMessage(subagent_collapse_prefix=...) AFTER the run's content; the bot
    enqueues a subagent_collapse control task; the worker collapses the card."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    # The agent renders a tool_use but NEVER a visible text+end_turn (so path 1
    # cannot fire). The card stays expanded.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Grep**(pattern)\n  ⎿  Found 2 matches",
            content_type="tool_use",
            tool_use_id="wst2",
            tool_name="Grep",
            role="assistant",
            subagent_key=_WF_KEY,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, _WF_KEY
    )
    state_key = (scenario.user_id, _THREAD_ID, _WF_KEY)
    assert message_queue._subagent_msg_info[state_key].collapsed is False

    # The bracket close → the monitor appends the collapse marker on the
    # display lane (after the content). The bot routes it to the route FIFO.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="",
            role="assistant",
            subagent_collapse_prefix=_WF_PREFIX,
        ),
        scenario.bot,
    )
    await _drain_route(route)

    assert message_queue._subagent_msg_info[state_key].collapsed is True
    sub_paints = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert sub_paints
    assert "✅" in sub_paints[-1]
    assert "•" not in sub_paints[-1]


@pytest.mark.asyncio
async def test_keep_recipient_not_collapsed_on_close(
    scenario: ScenarioHarness,
) -> None:
    """⭐ B-i (Hermes-delta P1-3): under keep (verbose) the bracket close does
    NOT collapse the run's cards — the play-by-play stays live."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "verbose")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Grep**(pattern)\n  ⎿  Found 2 matches",
            content_type="tool_use",
            tool_use_id="wst3",
            tool_name="Grep",
            role="assistant",
            subagent_key=_WF_KEY,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, _WF_KEY
    )

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="",
            role="assistant",
            subagent_collapse_prefix=_WF_PREFIX,
        ),
        scenario.bot,
    )
    await _drain_route(route)

    state_key = (scenario.user_id, _THREAD_ID, _WF_KEY)
    assert message_queue._subagent_msg_info[state_key].collapsed is False


@pytest.mark.asyncio
async def test_foreground_agent_card_still_collapsed_by_parent_finalize(
    scenario: ScenarioHarness,
) -> None:
    """⭐ B-i path 2 (no regression): a foreground Agent card (no bracket) is
    STILL collapsed by the UNCHANGED parent-finalize backstop under summary —
    Fix 5 added no skip there."""
    _, route = _bind(scenario)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")
    sub_key = "sub:sess-1:agent-fg9999"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**(ls)\n  ⎿  3 lines",
            content_type="tool_use",
            tool_use_id="fg1",
            tool_name="Bash",
            role="assistant",
            subagent_key=sub_key,
        ),
        scenario.bot,
    )
    await _drain_route(route)
    await message_queue._flush_subagent_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID, sub_key
    )

    await _finalize_turn(scenario, route)

    sub_paints = [
        s.kwargs.get("text", "")
        for s in scenario.bot.sent
        if "↳ Sub" in s.kwargs.get("text", "")
    ]
    assert sub_paints
    assert "✅" in sub_paints[-1]
    assert "•" not in sub_paints[-1]


@pytest.mark.asyncio
async def test_settings_panel_exposes_collapse_knobs(
    scenario: ScenarioHarness,
) -> None:
    """PR-2 adds the Done-card and Sub-agent-card rows (deferred from PR-1 —
    codex PR-1 review P2-1): tapping them persists the stored knob."""
    update = make_update_callback(
        f"{CB_SETTINGS}done:delete:{_OWNER_ID}",
        thread_id=_THREAD_ID,
        user_id=_OWNER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)
    assert scenario.session_manager.get_user_settings(_OWNER_ID).get("done") == "delete"

    update = make_update_callback(
        f"{CB_SETTINGS}subcards:off:{_OWNER_ID}",
        thread_id=_THREAD_ID,
        user_id=_OWNER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)
    stored = scenario.session_manager.get_user_settings(_OWNER_ID)
    assert stored.get("subcards") == "off"
