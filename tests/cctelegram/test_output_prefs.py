"""Unit tests for handlers/output_prefs — the per-user verbosity resolver.

Pins the plan v4 §4 layering contract: stored user override > EXPLICITLY-set
legacy env default > preset. Env vars are defaults, never ceilings; junk
stored values are inert (validated on read).
"""

from __future__ import annotations

import pytest

from cctelegram.config import config
from cctelegram.handlers import output_prefs
from cctelegram.session import session_manager


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch):
    session_manager.user_settings.clear()
    # Neutral env layer: nothing explicitly set, default preset = verbose.
    monkeypatch.setattr(config, "default_verbosity", "verbose")
    monkeypatch.setattr(config, "env_show_tool_calls_set", False)
    monkeypatch.setattr(config, "env_show_user_messages_set", False)
    monkeypatch.setattr(config, "env_context_footer_set", False)
    monkeypatch.setattr(config, "env_agent_preview_set", False)
    yield
    session_manager.user_settings.clear()


_UID = 4242


def test_default_resolves_verbose_preset():
    prefs = output_prefs.resolve(_UID)
    assert prefs.verbosity == "verbose"
    # The verbose preset mirrors today's module constants (PR-1 neutrality).
    assert prefs.digest_live_lines == 10
    assert prefs.digest_line_chars == 400
    assert prefs.result_snippet_chars == 240
    assert prefs.subagent_live_lines == 12
    assert prefs.user_echo is True
    assert prefs.tool_activity is True
    assert prefs.digest_card is True


def test_stored_preset_wins_over_env_default_preset(monkeypatch):
    monkeypatch.setattr(config, "default_verbosity", "compact")
    session_manager.user_settings[_UID] = {"verbosity": "standard"}
    assert output_prefs.resolve(_UID).verbosity == "standard"


def test_env_default_preset_applies_without_stored_choice(monkeypatch):
    monkeypatch.setattr(config, "default_verbosity", "quiet")
    prefs = output_prefs.resolve(_UID)
    assert prefs.verbosity == "quiet"
    assert prefs.digest_card is False
    assert prefs.agent_dispatch_msg is False


def test_unknown_stored_verbosity_falls_back(monkeypatch):
    monkeypatch.setattr(config, "default_verbosity", "bogus-env")
    session_manager.user_settings[_UID] = {"verbosity": "bogus-stored"}
    assert output_prefs.resolve(_UID).verbosity == output_prefs.DEFAULT_PRESET


def test_explicit_env_show_tool_calls_false_maps_full_suppression(monkeypatch):
    """The faithful legacy mapping: tool_activity off (Agent surfaces
    included) + sub-agent cards off; thinking unchanged (hermes r1 P2-8)."""
    monkeypatch.setattr(config, "env_show_tool_calls_set", True)
    monkeypatch.setattr(config, "show_tool_calls", False)
    prefs = output_prefs.resolve(_UID)
    assert prefs.tool_activity is False
    assert prefs.subagent_cards == output_prefs.SUBAGENT_CARDS_OFF
    assert prefs.thinking_line is True


def test_explicit_env_user_messages_false_is_default_not_ceiling(monkeypatch):
    monkeypatch.setattr(config, "env_show_user_messages_set", True)
    monkeypatch.setattr(config, "show_user_messages", False)
    assert output_prefs.resolve(_UID).user_echo is False
    # A stored per-knob override RE-ENABLES what the env default suppressed.
    session_manager.user_settings[_UID] = {"echo": True}
    assert output_prefs.resolve(_UID).user_echo is True


def test_stored_knob_override_wins_over_preset():
    session_manager.user_settings[_UID] = {"verbosity": "standard", "lines": 400}
    prefs = output_prefs.resolve(_UID)
    assert prefs.verbosity == "standard"
    assert prefs.digest_line_chars == 400  # override
    assert prefs.digest_live_lines == 6  # preset value untouched


def test_junk_stored_values_are_inert():
    session_manager.user_settings[_UID] = {
        "lines": 9999,  # not a valid choice
        "echo": "banana",
        "unknown_knob": True,
    }
    prefs = output_prefs.resolve(_UID)
    assert prefs.digest_line_chars == 400
    assert prefs.user_echo is True


def test_bool_knob_validation_is_type_strict():
    """Codex PR-1 review P2-2: `1 == True` in Python — malformed stored JSON
    like {"echo": 1} must stay inert, never coerce into a bool knob."""
    session_manager.user_settings[_UID] = {"echo": 0, "footer": 1}
    prefs = output_prefs.resolve(_UID)
    assert prefs.user_echo is True  # verbose preset value, junk ignored
    assert prefs.context_footer is True
