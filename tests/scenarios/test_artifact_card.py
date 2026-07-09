"""Scenario: artifact delivery lane — 📎 tap-to-download cards + /file.

Black-box per the scenario floor: Update / NewMessage → real handler stack
(``bot_module.handle_new_message`` / ``bot_module.callback_handler`` /
``bot_module.file_command``) → fake tmux / fake bot, with a REAL tmp cwd so the
filesystem validation runs against actual files.

  - assistant prose mentioning a deliverable local file → a 📎 card lands AFTER
    the prose (the route FIFO order is asserted on the real outbound record);
  - a tap uploads the file as a document (the validated fd is the upload source);
  - a restart-wiped tap answers the graceful expired modal;
  - ``/file <path>`` uploads happy-path + surfaces each specific rejection, and
    works for a file already offer-deduped on the card lane (dedup never gates
    /file);
  - quiet preset ⇒ no card; a sidechain block ⇒ no card.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import artifacts, message_queue
from cctelegram.handlers.callback_data import CB_DOWNLOAD_FILE
from cctelegram.session_monitor import NewMessage
from tests.conftest import (
    ScenarioHarness,
    make_update_callback,
    make_update_command,
)

pytestmark = pytest.mark.scenario

_SESSION_ID = "55555555-5555-4555-8555-555555555555"
_THREAD_ID = 42


async def _drain(route: tuple[int, int, str]) -> None:
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


def _bind(scenario: ScenarioHarness, cwd: str) -> tuple[str, tuple[int, int, str]]:
    wid = scenario.add_window(window_name="repo", cwd=cwd)
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd=cwd,
        session_id=_SESSION_ID,
    )
    return wid, (scenario.user_id, _THREAD_ID, wid)


async def _prose(scenario: ScenarioHarness, text: str, **over) -> None:
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text=text,
            content_type="text",
            role="assistant",
            **over,
        ),
        scenario.bot,
    )


def _card_records(scenario: ScenarioHarness) -> list:
    return [
        s
        for s in scenario.bot.sent
        if s.method == "send_message"
        and "📎 Files mentioned" in (s.kwargs.get("text") or "")
    ]


def _card_callback(record) -> str:
    return record.kwargs["reply_markup"].inline_keyboard[0][0].callback_data


def _answers(update) -> list[str]:
    return [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in update.callback_query.answer.await_args_list
        if c.args or c.kwargs.get("text")
    ]


def _reply(update) -> str:
    """The last reply text, un-escaped (safe_reply applies MarkdownV2)."""
    return update.message.reply_text.await_args.args[0].replace("\\", "")


# ── Card lands after the prose ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_card_lands_after_prose(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "report.md").write_bytes(b"# report")
    wid, route = _bind(scenario, str(tmp_path))

    await _prose(scenario, "I wrote the summary to report.md for you.")
    await _drain(route)

    cards = _card_records(scenario)
    assert len(cards) == 1
    card = cards[0]
    assert "• report.md" in card.kwargs["text"]
    cb = _card_callback(card)
    assert cb.startswith(f"{CB_DOWNLOAD_FILE}{wid}:")
    # The prose landed BEFORE the 📎 card (route FIFO order — codex P1-2).
    prose_idx = next(
        i
        for i, s in enumerate(scenario.bot.sent)
        if s.method == "send_message"
        and "wrote the summary" in (s.kwargs.get("text") or "")
    )
    card_idx = scenario.bot.sent.index(card)
    assert prose_idx < card_idx


@pytest.mark.asyncio
async def test_repeat_mention_deduped_no_second_card(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "chart.png").write_bytes(b"PNG")
    _, route = _bind(scenario, str(tmp_path))

    await _prose(scenario, "Saved chart.png.")
    await _drain(route)
    await _prose(scenario, "Again: chart.png is here.")
    await _drain(route)

    assert len(_card_records(scenario)) == 1  # offer-dedup suppresses the repeat


@pytest.mark.asyncio
async def test_nonexistent_path_no_card(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    _, route = _bind(scenario, str(tmp_path))
    await _prose(scenario, "See missing.pdf (does not exist).")
    await _drain(route)
    assert _card_records(scenario) == []


# ── Tap uploads the document ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tap_uploads_document(scenario: ScenarioHarness, tmp_path: Path) -> None:
    (tmp_path / "export.pdf").write_bytes(b"PDFBYTES")
    wid, route = _bind(scenario, str(tmp_path))
    await _prose(scenario, "Your export.pdf is ready.")
    await _drain(route)
    cb = _card_callback(_card_records(scenario)[0])

    update = make_update_callback(
        cb, thread_id=_THREAD_ID, user_id=scenario.user_id, chat_id=scenario.chat_id
    )
    await bot_module.callback_handler(update, scenario.context)

    docs = [s for s in scenario.bot.sent if s.method == "send_document"]
    assert len(docs) == 1
    assert docs[0].kwargs["filename"] == "export.pdf"
    assert any("uploading" in a.lower() for a in _answers(update))


@pytest.mark.asyncio
async def test_restart_wiped_tap_expired(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_bytes(b"n")
    wid, route = _bind(scenario, str(tmp_path))
    await _prose(scenario, "Notes in notes.txt.")
    await _drain(route)
    cb = _card_callback(_card_records(scenario)[0])
    artifacts.reset_for_tests()  # the restart wipes the in-memory registry

    update = make_update_callback(
        cb, thread_id=_THREAD_ID, user_id=scenario.user_id, chat_id=scenario.chat_id
    )
    await bot_module.callback_handler(update, scenario.context)

    assert any("expired" in a.lower() for a in _answers(update))
    assert [s for s in scenario.bot.sent if s.method == "send_document"] == []


# ── quiet + sidechain suppression ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_quiet_preset_no_card(scenario: ScenarioHarness, tmp_path: Path) -> None:
    (tmp_path / "report.md").write_bytes(b"x")
    _, route = _bind(scenario, str(tmp_path))
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "quiet")

    await _prose(scenario, "Wrote report.md.")
    await _drain(route)

    assert _card_records(scenario) == []


@pytest.mark.asyncio
async def test_sidechain_prose_no_card(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "report.md").write_bytes(b"x")
    _, route = _bind(scenario, str(tmp_path))

    await _prose(
        scenario,
        "Sub-agent wrote report.md.",
        subagent_key="sub:parent:run1:agent-abc",
    )
    await _drain(route)

    assert _card_records(scenario) == []


# ── /file command ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_command_happy_path(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deck.pdf").write_bytes(b"DECK")
    _bind(scenario, str(tmp_path))

    update = make_update_command(
        "file", args="sub/deck.pdf", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(update, scenario.context)

    # [fold item 5 — codex P3-2] assert the ACTUAL reply target, not just the
    # command update's thread id: delivery is the reply_document bound to the
    # command's own Message (PTB reply_* auto-inherits chat + thread for topic
    # messages), so the replied-to message's coordinates ARE the destination —
    # and no stray bot-level send_document targets anything else.
    assert update.message.reply_document.await_count == 1
    kw = update.message.reply_document.await_args.kwargs
    assert kw["filename"] == "deck.pdf"
    replied_to = update.message
    assert replied_to.chat_id == scenario.chat_id
    assert replied_to.message_thread_id == _THREAD_ID
    assert replied_to.is_topic_message is True
    assert [s for s in scenario.bot.sent if s.method == "send_document"] == [], (
        "/file must deliver via the reply seam, never a raw bot send"
    )


@pytest.mark.asyncio
async def test_file_command_path_with_spaces(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    (tmp_path / "my report.md").write_bytes(b"data")
    _bind(scenario, str(tmp_path))

    update = make_update_command(
        "file", args="my report.md", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(update, scenario.context)

    assert update.message.reply_document.await_count == 1


@pytest.mark.asyncio
async def test_file_command_not_deduped_by_card_lane(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    """The offer-dedup gates card spam, NOT explicit /file requests (hermes r2
    P3-2): a /file for a path already offered on a card still uploads."""
    (tmp_path / "report.md").write_bytes(b"x")
    _, route = _bind(scenario, str(tmp_path))
    await _prose(scenario, "Wrote report.md.")
    await _drain(route)
    assert len(_card_records(scenario)) == 1  # the file is now offer-deduped

    update = make_update_command(
        "file", args="report.md", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(update, scenario.context)
    assert update.message.reply_document.await_count == 1


@pytest.mark.asyncio
async def test_file_command_error_paths(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    wid, _route = _bind(scenario, str(tmp_path))

    # No argument.
    up = make_update_command("file", thread_id=_THREAD_ID, user_id=scenario.user_id)
    await bot_module.file_command(up, scenario.context)
    assert "Usage: /file <path>" in _reply(up)

    # Not found.
    up = make_update_command(
        "file", args="ghost.pdf", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(up, scenario.context)
    assert "not found" in _reply(up).lower()

    # Traversal escaping the cwd.
    up = make_update_command(
        "file", args="../secret.json", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(up, scenario.context)
    assert "allowed" in _reply(up).lower()

    # Oversize (states the cap).
    (tmp_path / "huge.zip").write_bytes(b"z" * (60 * 1024 * 1024))
    up = make_update_command(
        "file", args="huge.zip", thread_id=_THREAD_ID, user_id=scenario.user_id
    )
    await bot_module.file_command(up, scenario.context)
    assert "large" in _reply(up).lower()


@pytest.mark.asyncio
async def test_file_command_unbound_topic(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    # No binding for this thread.
    update = make_update_command(
        "file", args="x.pdf", thread_id=999, user_id=scenario.user_id
    )
    await bot_module.file_command(update, scenario.context)
    assert "No session bound" in _reply(update)
    assert update.message.reply_document.await_count == 0
