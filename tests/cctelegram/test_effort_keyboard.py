"""Unit tests for the /effort inline picker keyboard.

Guards the level set (incl. the `auto` and `ultracode` options) and that every
button mints valid, within-limit callback_data of the expected
``eff:<level>:<window_id>`` shape.
"""

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
