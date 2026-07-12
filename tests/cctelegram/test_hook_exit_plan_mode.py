"""GH #50 PR-2 r3 — the ``PreToolUse(ExitPlanMode)`` hook handler + installer.

RIG-VERIFIED on Claude Code 2.1.207 (a scratch ``tmux -L ccrig`` session driven
through three consecutive ExitPlanMode prompts). The payload below is the REAL
observed shape:

    {"hook_event_name": "PreToolUse",
     "tool_name": "ExitPlanMode",
     "tool_use_id": "toolu_01FfhZG6H6fRzTyJZL5P5qoF",   # DISTINCT per invocation
     "tool_input": {"plan": "...", "planFilePath": "/Users/…/plans/<slug>.md"},
     "session_id": …, "transcript_path": …, "cwd": …}

and ``TMUX_PANE`` IS exported to the hook, so the record can carry a
``window_key`` and its reader can hard-predicate on it.

Crucially, all THREE prompts shared ONE ``planFilePath`` and the file was
rewritten in place each time — which is why the plan artifact cannot name the
prompt occurrence and this hook must (see ``handlers/epm_source``).

Covers: the record shape + permissions (dir 0700 / file 0600), the NO-plan-BODY
privacy rule, the fail-closed no-TMUX_PANE path, always-exit-0 robustness,
installer idempotency (the SECOND PreToolUse entry, added without disturbing the
AskUserQuestion one), and the bot-startup warning when the entry is missing.
"""

from __future__ import annotations

import io
import json
import stat
import sys

import pytest

from cctelegram import hook as hook_mod
from cctelegram.hook import (
    _AUQ_MATCHER,
    _EPM_MATCHER,
    _install_hook,
    _is_pre_tool_use_installed,
    hook_main,
)

_SID = "550e8400-e29b-41d4-a716-446655440000"
_PLAN_BODY = "# Change app.py\n\nSECRET-INFRA-DETAIL: deploy to 192.168.0.54\n"
_PLAN_PATH = "/Users/x/.claude/plans/read-app-py-logical-pumpkin.md"
_TUID = "toolu_01FfhZG6H6fRzTyJZL5P5qoF"


def _epm_payload(**overrides) -> dict:
    payload = {
        "session_id": _SID,
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "tool_use_id": _TUID,
        "tool_input": {"plan": _PLAN_BODY, "planFilePath": _PLAN_PATH},
    }
    payload.update(overrides)
    return payload


def _run_hook(
    monkeypatch: pytest.MonkeyPatch, payload: dict, *, pane: str = "%3"
) -> int:
    monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    if pane:
        monkeypatch.setenv("TMUX_PANE", pane)
    else:
        monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(
        hook_mod,
        "_resolve_tmux_window_key",
        lambda pane_id: ("cc-telegram", "@5", "proj"),
    )
    return hook_main()


class TestEpmPreToolUseHandler:
    def test_writes_the_occurrence_witness(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook(monkeypatch, _epm_payload()) == 0

        target = tmp_path / "epm_pending" / f"{_SID}.json"
        assert target.exists()
        rec = json.loads(target.read_text())
        assert rec["schema_version"] == 1
        assert rec["session_id"] == _SID
        assert rec["tool_use_id"] == _TUID  # THE occurrence id
        assert rec["window_key"] == "cc-telegram:@5"
        assert isinstance(rec["written_at"], float)
        assert rec["plan_file_path"] == _PLAN_PATH
        assert rec["plan_fingerprint"]

    def test_the_plan_BODY_is_never_stored(self, monkeypatch, tmp_path):
        """Minimal privilege: the consumer needs a NAME for the prompt, not its
        contents — and the plan text already reaches the user through the bot's
        own plan-body post. A plan can name infra, hosts and decisions."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _epm_payload())

        raw = (tmp_path / "epm_pending" / f"{_SID}.json").read_text()
        assert "SECRET-INFRA-DETAIL" not in raw
        assert "192.168.0.54" not in raw
        assert "plan" not in json.loads(raw)

    def test_a_successor_prompt_overwrites_with_a_NEW_tool_use_id(
        self, monkeypatch, tmp_path
    ):
        """The rig's exact sequence: re-plan ⇒ same path, rewritten file, NEW id."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _epm_payload())
        _run_hook(
            monkeypatch,
            _epm_payload(
                tool_use_id="toolu_01GwV29afXsN7j3biBdEx1iB",
                tool_input={"plan": "# A different plan\n", "planFilePath": _PLAN_PATH},
            ),
        )

        rec = json.loads((tmp_path / "epm_pending" / f"{_SID}.json").read_text())
        assert rec["tool_use_id"] == "toolu_01GwV29afXsN7j3biBdEx1iB"
        assert rec["plan_file_path"] == _PLAN_PATH  # the slug is REUSED

    def test_file_and_dir_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _epm_payload())

        d = tmp_path / "epm_pending"
        f = d / f"{_SID}.json"
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_no_TMUX_PANE_writes_NOTHING(self, monkeypatch, tmp_path):
        """Fail-closed: the window_key is MANDATORY in this lane (it is the
        double-``--resume`` sibling predicate on a bypass-permissions surface).
        No pane ⇒ no key ⇒ no record ⇒ the free-text lane declines."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook(monkeypatch, _epm_payload(), pane="") == 0
        assert not (tmp_path / "epm_pending" / f"{_SID}.json").exists()

    def test_a_malformed_tool_input_never_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook(monkeypatch, _epm_payload(tool_input="not-a-dict")) == 0
        assert not (tmp_path / "epm_pending" / f"{_SID}.json").exists()

    def test_the_AUQ_lane_is_untouched(self, monkeypatch, tmp_path):
        """An AskUserQuestion PreToolUse still writes auq_pending/, not epm_pending/."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(
            monkeypatch,
            {
                "session_id": _SID,
                "cwd": "/tmp",
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_use_id": "toolu_AUQ",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Which colour?",
                            "header": "Colour",
                            "options": [
                                {"label": "Red", "description": "warm"},
                                {"label": "Blue", "description": "cool"},
                            ],
                        }
                    ]
                },
            },
        )
        assert (tmp_path / "auq_pending" / f"{_SID}.json").exists()
        assert not (tmp_path / "epm_pending" / f"{_SID}.json").exists()


class TestInstaller:
    def test_installs_the_second_PreToolUse_entry(self, tmp_path):
        settings = tmp_path / "settings.json"
        assert _install_hook(settings) == 0

        cfg = json.loads(settings.read_text())
        matchers = [e.get("matcher") for e in cfg["hooks"]["PreToolUse"]]
        assert _AUQ_MATCHER in matchers
        assert _EPM_MATCHER in matchers
        assert _is_pre_tool_use_installed(cfg, _EPM_MATCHER) == "current"
        assert _is_pre_tool_use_installed(cfg, _AUQ_MATCHER) == "current"

    def test_is_idempotent(self, tmp_path):
        settings = tmp_path / "settings.json"
        _install_hook(settings)
        first = json.loads(settings.read_text())
        _install_hook(settings)
        assert json.loads(settings.read_text()) == first

    def test_an_AUQ_only_install_gains_ONLY_the_epm_entry(self, tmp_path):
        """The upgrade path: a settings.json that predates this change keeps its
        existing AUQ entry byte-for-byte and simply gains the ExitPlanMode one.
        (This is why the two matchers get SEPARATE entries rather than one
        combined regex — a combined matcher could not be added idempotently.)"""
        settings = tmp_path / "settings.json"
        auq_entry = {
            "matcher": _AUQ_MATCHER,
            "hooks": [{"type": "command", "command": "cc-telegram hook", "timeout": 2}],
        }
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [auq_entry]}}))

        assert _install_hook(settings) == 0

        entries = json.loads(settings.read_text())["hooks"]["PreToolUse"]
        assert len(entries) == 2
        assert entries[0] == auq_entry, "the pre-existing AUQ entry is untouched"
        assert entries[1]["matcher"] == _EPM_MATCHER

    def test_a_missing_epm_entry_is_reported_missing(self):
        cfg = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": _AUQ_MATCHER,
                        "hooks": [{"type": "command", "command": "cc-telegram hook"}],
                    }
                ]
            }
        }
        assert _is_pre_tool_use_installed(cfg, _EPM_MATCHER) == "missing"
        assert _is_pre_tool_use_installed(cfg, _AUQ_MATCHER) == "current"


class TestStartupWarning:
    def test_warns_when_the_epm_entry_is_missing(self, tmp_path):
        from cctelegram.handlers.interactive_ui import (
            warn_if_epm_pre_tool_use_hook_missing,
        )

        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": _AUQ_MATCHER,
                                "hooks": [
                                    {"type": "command", "command": "cc-telegram hook"}
                                ],
                            }
                        ]
                    }
                }
            )
        )
        assert warn_if_epm_pre_tool_use_hook_missing(settings) is True

    def test_silent_when_installed(self, tmp_path):
        from cctelegram.handlers.interactive_ui import (
            warn_if_epm_pre_tool_use_hook_missing,
        )

        settings = tmp_path / "settings.json"
        _install_hook(settings)
        assert warn_if_epm_pre_tool_use_hook_missing(settings) is False

    def test_warns_when_settings_file_is_absent(self, tmp_path):
        from cctelegram.handlers.interactive_ui import (
            warn_if_epm_pre_tool_use_hook_missing,
        )

        assert warn_if_epm_pre_tool_use_hook_missing(tmp_path / "nope.json") is True
