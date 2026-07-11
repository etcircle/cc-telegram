"""Unit tests for the /effort inline picker keyboard.

Guards the level set (incl. the `auto` and `ultracode` options) and that every
button mints valid, within-limit callback_data of the expected
``eff:<level>:<window_id>`` shape.
"""

import pytest

from cctelegram.callback_dispatcher.effort import (
    EFFORT_LABELS,
    EFFORT_LEVELS,
    build_effort_keyboard,
)
from cctelegram.handlers.callback_data import CB_EFFORT, checked_callback_data


def test_every_level_has_a_label():
    assert set(EFFORT_LEVELS) == set(EFFORT_LABELS)


def test_auto_and_ultracode_are_selectable():
    assert "auto" in EFFORT_LEVELS
    assert "ultracode" in EFFORT_LEVELS
    assert EFFORT_LABELS["auto"] == "Auto"
    assert EFFORT_LABELS["ultracode"] == "Ultracode"


def test_keyboard_covers_all_levels_with_valid_callback_data():
    wid = "@28"
    kb = build_effort_keyboard(wid)
    buttons = [b for row in kb.inline_keyboard for b in row]

    seen: set[str] = set()
    for b in buttons:
        assert b.callback_data.startswith(CB_EFFORT)
        level = b.callback_data[len(CB_EFFORT) :].rsplit(":", 1)[0]
        seen.add(level)
        assert b.text == EFFORT_LABELS[level]
        # Stays within Telegram's 64-byte callback_data limit.
        assert checked_callback_data(b.callback_data) == b.callback_data

    assert seen == set(EFFORT_LEVELS)


@pytest.mark.asyncio
async def test_a_refused_pre_flush_aborts_the_effort_send(monkeypatch):
    """GH #50 r2 F2(i): /effort mirrors the slash-command route-ordering
    subsequence, so it must ALSO honor the forced flush's result — a refused
    flush means the user's earlier message may be sitting UNSENT in the input
    box, and typing /effort onto it would commit both on our Enter."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cctelegram import delivery
    from cctelegram.callback_dispatcher import effort as effort_mod

    refusal = delivery.refuse(delivery.REASON_STRANDED_DRAFT, written=False)

    async def refused_flush(_route, *, report_refusal=True):
        # /effort owns the single refusal message (the aggregator must not
        # post a second ❌ for the same event — peer-review P2).
        assert report_refusal is False
        return refusal

    monkeypatch.setattr(effort_mod, "aggregator_flush_route", refused_flush)
    monkeypatch.setattr(effort_mod, "safe_answer", AsyncMock())
    edits = AsyncMock()
    monkeypatch.setattr(effort_mod, "safe_edit", edits)

    send = AsyncMock()
    adapters = SimpleNamespace(
        session_manager=SimpleNamespace(send_to_window=send),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5"))
        ),
        route_runtime=SimpleNamespace(mark_inbound_sent=AsyncMock()),
    )
    query = SimpleNamespace(data="eff:high:@5")
    authorized = SimpleNamespace(
        command=SimpleNamespace(data="eff:high:@5"),
        ctx=SimpleNamespace(
            query=query,
            user=SimpleNamespace(id=1),
            user_id=1,
            thread_id=10,
            chat_id=-100,
            bot=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        effort_mod,
        "window_lease",
        lambda *_a, **_k: SimpleNamespace(
            reject_stale_window=AsyncMock(return_value=False)
        ),
    )

    await effort_mod.execute_effort_callback(authorized, adapters)

    send.assert_not_called()
    body = "".join(str(c.args) + str(c.kwargs) for c in edits.await_args_list)
    assert "Effort NOT set" in body
