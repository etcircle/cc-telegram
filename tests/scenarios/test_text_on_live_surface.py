"""Scenario floor: GH #50 — inbound text on a LIVE interactive surface.

Black-box, at the public Telegram seam. Today ``text_handler`` DETECTS the
surface and sends anyway; voice / photo / document have no check at all; and the
aggregator flushes from a background debounce task, so any offer-time check is
TOCTOU. The delivery gate closes all four at the single choke point
(``session_manager.send_to_window``).

The failure modes being pinned:

  M1 — the Enter commits option 1 on EVERY blocking surface. Rig-verified worst
       cases: ExitPlanMode ⇒ the plan is APPROVED (option 1 is "Yes, and bypass
       permissions"); folder-trust ⇒ trust GRANTED and persisted to
       ``~/.claude.json``; ``Switch model?`` ⇒ the model is switched + saved.
  M2 — a bare digit is a live HOTKEY (commits with NO Enter) on
       single-select-shaped surfaces.
  M3 — a bare-shell pane EXECUTES the payload as a shell command (``/esc`` on
       folder-trust EXITS Claude, leaving a shell in a still-bound window).
  M4 — the bot is BLIND to ``Switch model?`` (footer-less ⇒
       ``parse_generic_decision`` returns None). A live blocking prompt the
       parser cannot see — which is why the gate is POSITIVE evidence, never
       "no known prompt matched".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from cctelegram import terminal_parser
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram.handlers.inbound_aggregator import aggregator_flush_route
from tests.conftest import (
    IDLE_PANE_V2_1_207,
    ScenarioHarness,
    _make_message,
    _make_user,
    make_update_command,
    make_update_text,
    pane_fixture,
)

pytestmark = pytest.mark.scenario


# Every blocking family the owner's deployment can actually hit, plus the two
# the parser CANNOT recognize (the M4 pin + a synthetic never-shipped prompt).
BLOCKING_PANES = {
    "auq_single": pane_fixture("auq_single_picker_v2.1.207.txt"),
    "auq_multi": pane_fixture("auq_multi_picker_v2.1.207.txt"),
    "exit_plan_mode": pane_fixture("gate_epm_v2.1.207.txt"),
    "folder_trust": pane_fixture("folder_trust_arrival_plain_v2.1.207.txt"),
    "switch_model": pane_fixture("switch_model_live_v2.1.207.txt"),
    "workflow": pane_fixture("gate_workflow_v2.1.207.txt"),
    "permission": pane_fixture("gate_permission_v2.1.207.txt"),
    "settings": pane_fixture("settings_warning_v2170.txt"),
    "restore_checkpoint": (
        "  Restore checkpoint?\n"
        "  This will revert the conversation and your files.\n"
        "\n"
        "  ❯ 1. Restore conversation and code\n"
        "    2. Restore conversation only\n"
        "    3. Cancel\n"
        "\n"
        "  Enter to confirm · Esc to cancel\n"
    ),
    "unknown_prompt": (
        "  Reticulate the splines?\n"
        "  A prompt shape that has never shipped in any Claude Code version.\n"
        "\n"
        "  ❯ 1. Absolutely\n"
        "    2. Never\n"
        "\n"
        "  Press any key · Esc to bail\n"
    ),
    "tasks_mode": pane_fixture("inputbox_tasks_mode_v2.1.207.txt"),
}


def _bind(scenario: ScenarioHarness, *, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    return wid


def _typed(scenario: ScenarioHarness) -> list[str]:
    """The literal payload segments actually written into the pane."""
    return scenario.tmux.written_texts


async def _notices(scenario: ScenarioHarness) -> list[str]:
    """Every in-topic message the bot posted (the refusal disclosure lane)."""
    return [str(t) for t in scenario.bot.texts()]


# ── Every blocking surface REFUSES (M1 + M4) ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(BLOCKING_PANES))
async def test_text_on_a_live_surface_is_refused(
    scenario: ScenarioHarness, surface: str
) -> None:
    wid = _bind(scenario, pane=BLOCKING_PANES[surface])
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("hello", thread_id=42), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.refused, surface
    assert _typed(scenario) == [], f"{surface}: a keystroke reached a live prompt"
    assert not scenario.tmux.committed, f"{surface}: Enter reached a live prompt"


@pytest.mark.asyncio
async def test_switch_model_refuses_regardless_of_the_detector(
    scenario: ScenarioHarness,
) -> None:
    """The M4 pin, re-based for GH #52. ``Switch model?`` was footer-less and the
    Decision detector used to be BLIND to it (the M4 hazard: a NEGATIVE "no known
    prompt matched" gate would type into it and Enter would switch the model + save
    it as the default). GH #52 now DETECTS it as a footerless Decision with the
    flags ON — but the delivery gate is POSITIVE input-box evidence and never
    consults ``_active_ui_patterns``, so it refuses REGARDLESS of the detector."""
    pane = BLOCKING_PANES["switch_model"]
    terminal_parser.set_permission_prompts_enabled(True)
    terminal_parser.set_decision_cards_enabled(True)
    try:
        # GH #52: the detector now names it Decision (flags ON) — the gate refusal
        # below is INDEPENDENT of that (it is positive input-box proof).
        content = terminal_parser.extract_interactive_content(pane)
        assert content is not None and content.name == "Decision"
        wid = _bind(scenario, pane=pane)
        route = (scenario.user_id, 42, wid)

        await bot_module.text_handler(
            make_update_text("yes do it", thread_id=42), scenario.context
        )
        assert (await aggregator_flush_route(route)).refused
        assert _typed(scenario) == []
    finally:
        terminal_parser.reset_for_tests()


@pytest.mark.asyncio
async def test_flags_off_still_refuses_folder_trust(scenario: ScenarioHarness) -> None:
    """Flag-independence (plan §1.1): the gate never consults
    ``_active_ui_patterns``, so the display kill-switches cannot reopen the hole.
    The suite already pins both detector flags OFF."""
    pane = BLOCKING_PANES["folder_trust"]
    assert terminal_parser.extract_interactive_content(pane) is None  # detector blind
    wid = _bind(scenario, pane=pane)
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("hi", thread_id=42), scenario.context
    )

    assert (await aggregator_flush_route(route)).refused
    assert _typed(scenario) == []


@pytest.mark.asyncio
async def test_refusal_posts_an_actionable_in_topic_notice(
    scenario: ScenarioHarness,
) -> None:
    """§1.4: the debounced flush is fire-and-forget, so a refused payload is
    DROPPED WITH A NOTICE — never silently, never auto-replayed."""
    wid = _bind(scenario, pane=BLOCKING_PANES["auq_single"])
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("hello", thread_id=42), scenario.context
    )
    await aggregator_flush_route(route)

    notices = await _notices(scenario)
    assert any("Answer the card first" in n for n in notices), notices


# ── The unguarded seams: voice / caption / attachment (M1) ───────────────


def _voice_update(thread_id: int) -> MagicMock:
    voice = MagicMock(name="Voice")
    voice.duration = 5
    voice_file = MagicMock(name="VoiceFile")
    voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x00"))
    voice.get_file = AsyncMock(return_value=voice_file)
    msg = _make_message(thread_id=thread_id, voice=voice)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def _document_update(thread_id: int, tmp_path: Path, caption: str | None) -> MagicMock:
    document = MagicMock(name="Document")
    document.file_size = 8
    document.file_name = "notes.txt"
    document.file_unique_id = "uid42"

    async def _download(dest):  # noqa: ANN001
        Path(dest).write_bytes(b"\x00")
        return dest

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    document.get_file = AsyncMock(return_value=tg_file)
    msg = _make_message(thread_id=thread_id, caption=caption, document=document)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_voice_transcription_on_a_live_surface_is_refused(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The voice handler has NO surface check today — the gate is what saves it."""
    wid = _bind(scenario, pane=BLOCKING_PANES["exit_plan_mode"])
    route = (scenario.user_id, 42, wid)
    monkeypatch.setattr(
        inbound_module, "transcribe_voice", AsyncMock(return_value="approve the plan")
    )
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")

    await bot_module.voice_handler(_voice_update(42), scenario.context)
    result = await aggregator_flush_route(route)

    assert result.refused
    assert _typed(scenario) == []  # the plan was NOT approved


@pytest.mark.asyncio
async def test_document_caption_on_a_live_surface_is_refused(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    wid = _bind(scenario, pane=BLOCKING_PANES["folder_trust"])
    route = (scenario.user_id, 42, wid)

    await bot_module.document_handler(
        _document_update(42, tmp_path, "look at this"), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.refused
    assert _typed(scenario) == []


@pytest.mark.asyncio
async def test_attachment_only_bundle_on_a_live_surface_is_refused(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    """An attachment-ONLY bundle (no caption) is still a payload + Enter."""
    wid = _bind(scenario, pane=BLOCKING_PANES["auq_single"])
    route = (scenario.user_id, 42, wid)

    await bot_module.document_handler(
        _document_update(42, tmp_path, None), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.refused
    assert _typed(scenario) == []


# ── M3: the /esc-into-shell case ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_pane_after_esc_never_executes_the_payload(
    scenario: ScenarioHarness,
) -> None:
    """``/esc`` on folder-trust EXITS Claude (rig), leaving a bare shell in a
    still-bound window — and ``/esc`` bypasses ``send_to_window`` entirely, so
    only /update failures used to quarantine. The proof-of-life check on EVERY
    send closes it."""
    wid = _bind(scenario, pane=pane_fixture("shell_after_esc_v2.1.207.txt"))
    scenario.tmux.set_pane_command(wid, "zsh")
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("rm -rf ./scratch", thread_id=42), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.refused
    assert result.reason == "not_claude"
    assert _typed(scenario) == []
    assert any("/update" in n for n in await _notices(scenario))


# ── M2: a lone digit is never written ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["1", "!1", "do this\n2\nthen that"])
async def test_lone_digit_segment_is_never_written(
    scenario: ScenarioHarness, payload: str
) -> None:
    """Even on an IDLE pane: the gate→write window is exactly what makes a bare
    digit dangerous (it is a live hotkey on a single-select surface, committing
    with NO Enter)."""
    wid = _bind(scenario, pane=IDLE_PANE_V2_1_207)
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text(payload, thread_id=42), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.refused
    assert result.reason == "lone_hotkey_segment"
    assert _typed(scenario) == []
    assert any("just a number" in n for n in await _notices(scenario))


# ── Slash commands / pending-bind replay while a surface is live ─────────


@pytest.mark.asyncio
async def test_slash_command_on_a_live_surface_is_refused(
    scenario: ScenarioHarness,
) -> None:
    """Typing "/clear" + Enter into a live picker would COMMIT the highlighted
    option before Claude ever saw the command."""
    _bind(scenario, pane=BLOCKING_PANES["auq_single"])

    await bot_module.forward_command_handler(
        make_update_command("clear", thread_id=42), scenario.context
    )

    assert _typed(scenario) == []
    assert not scenario.tmux.committed


@pytest.mark.asyncio
async def test_pending_bind_replay_on_folder_trust_surfaces_the_reason(
    scenario: ScenarioHarness,
) -> None:
    """THE fresh-session case: a brand-new window's very first turn lands while
    Claude blocks on "Do you trust the files in this folder?". The replay used to
    return a bare bool, so the bind reply said "failed to send" — now it names it."""
    from cctelegram.handlers.inbound_aggregator import aggregator_replay_payload

    wid = _bind(scenario, pane=BLOCKING_PANES["folder_trust"])
    route = (scenario.user_id, 42, wid)

    result = await aggregator_replay_payload(route, text="hello", attachments=[])

    assert result.refused
    assert _typed(scenario) == []
    assert "Answer the card first" in result.message


# ── Non-regression: the delivery paths that MUST keep working ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pane_name",
    [
        "inputbox_idle_v2.1.207.txt",
        "inputbox_busy_tool_v2.1.207.txt",  # BUSY pane — queueing is first-class
        "inputbox_busy_thinking_v2.1.207.txt",
        "inputbox_draft_typed_v2.1.207.txt",  # a pre-existing terminal draft
        "inputbox_wrapped_draft_v2.1.207.txt",
        "inputbox_multiline_draft_v2.1.207.txt",
        "idle_ghost_input_row_v2.1.206.txt",  # the ghost-suggestion pane
        "inputbox_bgshell_v2.1.207.txt",
    ],
)
async def test_deliverable_panes_still_deliver(
    scenario: ScenarioHarness, pane_name: str
) -> None:
    wid = _bind(scenario, pane=pane_fixture(pane_name))
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("keep working", thread_id=42), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.ok, pane_name
    assert "keep working" in _typed(scenario)[0]
    assert scenario.tmux.committed


@pytest.mark.asyncio
async def test_multiline_payload_delivers(scenario: ScenarioHarness) -> None:
    wid = _bind(scenario, pane=IDLE_PANE_V2_1_207)
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("line one\nline two\nline three", thread_id=42),
        scenario.context,
    )

    assert (await aggregator_flush_route(route)).ok
    assert _typed(scenario) == ["line one\nline two\nline three"]


@pytest.mark.asyncio
async def test_bang_command_delivers_with_the_two_step(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, pane=pane_fixture("inputbox_bashmode_draft_v2.1.207.txt"))
    route = (scenario.user_id, 42, wid)

    await bot_module.text_handler(
        make_update_text("!echo hi", thread_id=42), scenario.context
    )

    assert (await aggregator_flush_route(route)).ok
    # The `!` lands FIRST (the TUI switches to bash mode), then the remainder.
    assert _typed(scenario) == ["!", "echo hi"]
    assert scenario.tmux.committed


@pytest.mark.asyncio
async def test_slash_command_on_an_idle_pane_still_delivers(
    scenario: ScenarioHarness,
) -> None:
    _bind(scenario, pane=IDLE_PANE_V2_1_207)

    await bot_module.forward_command_handler(
        make_update_command("clear", thread_id=42), scenario.context
    )

    assert _typed(scenario) == ["/clear"]
    assert scenario.tmux.committed


@pytest.mark.asyncio
async def test_esc_command_stays_ungated_on_a_live_surface(
    scenario: ScenarioHarness,
) -> None:
    """``/esc`` keys into a LIVE surface BY DESIGN (it is the user's escape
    hatch) — it must NOT route through the delivery gate."""
    wid = _bind(scenario, pane=BLOCKING_PANES["auq_single"])

    await bot_module.esc_command(
        make_update_command("esc", thread_id=42), scenario.context
    )

    assert any(
        w == wid and keys == "\x1b" for w, keys, _e, _l in scenario.tmux.sent_keys
    ), scenario.tmux.sent_keys


# ── THE PASTE-COLLAPSE: the owner's exact failing case (GH #50 PR-1 regression) ──
#
# The owner sent a VOICE NOTE as a reply. With the reply-context quote it was an
# 809-char, multi-line payload. CC consumed the single literal write as a PASTE:
# it collapsed the input row to `❯\xa0[Pasted text #1 +12 lines]` and REPLACED the
# status bar with `paste again to expand`. The re-verify (TEXT_SETTLE_S = 0.5s,
# squarely inside that ~2s window) saw none of the ready-chrome markers, concluded
# there was no input box, refused, stranded the draft and BRAKED the topic — even
# though Enter would have submitted the message correctly.
#
#   session - DEBUG - deliver_to_window: window_id=@36, text_len=809
#   tmux_manager - WARNING - stranded-draft brake ARMED for window @36
#   session - INFO - DELIVERY REFUSED reason=reverify_failed outcome=draft_written
#
# This hit essentially EVERY long or multi-line message (long voice notes, replies
# carrying quoted context, long text) — a large fraction of real usage.

_LONG_REPLY_PAYLOAD = "\n".join(
    ["> Voice note reply to: the deployment plan and the follow-up items."]
    + [
        f"line {i}: a sentence of reply context long enough to push the single "
        f"literal write past the threshold at which CC treats it as a paste."
        for i in range(1, 8)
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collapsed_pane",
    [
        # The `paste again to expand` chrome — the frame that broke the gate.
        "inputbox_paste_collapsed_v2.1.207.txt",
        # ~2s later CC restores the mode line while the collapsed draft remains
        # (the shape the owner's live pane was left in).
        "inputbox_paste_collapsed_reverted_v2.1.207.txt",
    ],
)
async def test_a_long_multiline_reply_delivers_through_the_paste_collapse(
    scenario: ScenarioHarness, collapsed_pane: str
) -> None:
    assert len(_LONG_REPLY_PAYLOAD) > 800, "must exceed CC's paste threshold"
    wid = _bind(scenario, pane=IDLE_PANE_V2_1_207)
    route = (scenario.user_id, 42, wid)

    # The write is consumed as a PASTE and the pane collapses under us.
    scenario.tmux.on_write = lambda: scenario.tmux.set_pane(
        wid, pane_fixture(collapsed_pane)
    )

    await bot_module.text_handler(
        make_update_text(_LONG_REPLY_PAYLOAD, thread_id=42), scenario.context
    )
    result = await aggregator_flush_route(route)

    assert result.ok, result.reason
    assert _typed(scenario) == [_LONG_REPLY_PAYLOAD]
    assert scenario.tmux.committed, "the Enter PR-1 was withholding"
    # …and the topic is NOT braked: the next message must still go through.
    scenario.tmux.on_write = None
    await bot_module.text_handler(
        make_update_text("and one more thing", thread_id=42), scenario.context
    )
    assert (await aggregator_flush_route(route)).ok


# ── r2 F2: the stranded-draft commit chain (the P1 the gate itself created) ──


@pytest.mark.asyncio
async def test_a_stranded_draft_is_never_committed_by_the_next_message(
    scenario: ScenarioHarness,
) -> None:
    """A ``draft_written`` refusal leaves the payload in the input box with its
    Enter withheld — and the user is TOLD it was not delivered. But a live input
    box holding a pre-existing draft is legitimately DELIVERABLE, so without a
    brake the NEXT message passed the gate, was APPENDED to the stranded text, and
    its Enter committed BOTH — silently submitting a message the bot had already
    disclaimed, concatenated with the new one.
    """
    wid = _bind(scenario, pane=IDLE_PANE_V2_1_207)
    route = (scenario.user_id, 42, wid)

    # Message 1: a prompt is drawn between the WRITE and the re-verify ⇒ the text
    # is in the box, the Enter is withheld, the user is told it was not delivered.
    scenario.tmux.on_write = lambda: scenario.tmux.set_pane(
        wid, BLOCKING_PANES["auq_single"]
    )
    await bot_module.text_handler(
        make_update_text("first message", thread_id=42), scenario.context
    )
    first = await aggregator_flush_route(route)
    assert first.outcome.value == "draft_written"
    assert not scenario.tmux.committed

    # The prompt resolves; the input box is back — but our unsent draft is in it.
    scenario.tmux.on_write = None
    scenario.tmux.set_pane(wid, pane_fixture("inputbox_draft_typed_v2.1.207.txt"))

    # Message 2 must NOT be typed onto it, and NOTHING may be committed.
    await bot_module.text_handler(
        make_update_text("second message", thread_id=42), scenario.context
    )
    second = await aggregator_flush_route(route)

    assert second.refused
    assert second.reason == "stranded_draft"
    assert _typed(scenario) == ["first message"]
    assert not scenario.tmux.committed
    assert any("still sitting UNSENT" in n for n in await _notices(scenario))
