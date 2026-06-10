"""Wave B unit tests — the ``Notification`` hook handler + installer.

Covers: payload → ``notify_pending/<session_id>.json`` shape and
permissions (dir 0700 / file 0600), NO notification message text stored,
window_key resolution via the existing tmux pane resolver, always-exit-0
robustness, installer idempotency (``_is_notification_installed`` +
the third entry in ``_install_hook``), and the bot-startup warning when
the Notification hook is missing from settings.json.
"""

from __future__ import annotations

import io
import json
import stat
import sys
from pathlib import Path

import pytest

from cctelegram import hook as hook_mod
from cctelegram.hook import (
    _install_hook,
    _is_notification_installed,
    hook_main,
)

_SID = "550e8400-e29b-41d4-a716-446655440000"
_SECRET = "Claude needs your permission to run rm -rf /tmp/secret-project"


def _notification_payload(**overrides) -> dict:
    payload = {
        "session_id": _SID,
        "cwd": "/tmp",
        "hook_event_name": "Notification",
        "message": _SECRET,
    }
    payload.update(overrides)
    return payload


def _run_hook(monkeypatch: pytest.MonkeyPatch, payload: dict) -> int:
    monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr(
        hook_mod,
        "_resolve_tmux_window_key",
        lambda pane_id: ("cc-telegram", "@5", "proj"),
    )
    return hook_main()


class TestNotificationHandler:
    def test_writes_side_file_with_window_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert _run_hook(monkeypatch, _notification_payload()) == 0
        target = tmp_path / "notify_pending" / f"{_SID}.json"
        assert target.exists()
        rec = json.loads(target.read_text())
        assert rec["schema_version"] == 1
        assert rec["session_id"] == _SID
        assert rec["window_key"] == "cc-telegram:@5"
        assert isinstance(rec["ts"], float)
        assert isinstance(rec["generation"], str) and rec["generation"]
        assert "kind" in rec

    def test_no_message_text_stored(self, monkeypatch, tmp_path):
        """codex P3-6: the side file must NOT carry the notification text."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _notification_payload())
        target = tmp_path / "notify_pending" / f"{_SID}.json"
        raw = target.read_text()
        assert _SECRET not in raw
        assert "message" not in json.loads(raw)

    def test_file_and_dir_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _notification_payload())
        pending_dir = tmp_path / "notify_pending"
        target = pending_dir / f"{_SID}.json"
        assert stat.S_IMODE(pending_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_no_tmux_pane_exits_zero_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps(_notification_payload()))
        )
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert hook_main() == 0
        assert not (tmp_path / "notify_pending").exists()

    def test_unresolvable_pane_exits_zero_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps(_notification_payload()))
        )
        monkeypatch.setenv("TMUX_PANE", "%3")
        monkeypatch.setattr(hook_mod, "_resolve_tmux_window_key", lambda pane_id: None)
        assert hook_main() == 0
        assert not (tmp_path / "notify_pending").exists()

    def test_handler_exception_swallowed_exit_zero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(
            sys, "stdin", io.StringIO(json.dumps(_notification_payload()))
        )
        monkeypatch.setenv("TMUX_PANE", "%3")

        def _boom(pane_id: str):
            raise RuntimeError("tmux exploded")

        monkeypatch.setattr(hook_mod, "_resolve_tmux_window_key", _boom)
        assert hook_main() == 0

    def test_refire_replaces_with_new_generation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        _run_hook(monkeypatch, _notification_payload())
        target = tmp_path / "notify_pending" / f"{_SID}.json"
        gen1 = json.loads(target.read_text())["generation"]
        _run_hook(monkeypatch, _notification_payload())
        gen2 = json.loads(target.read_text())["generation"]
        assert gen1 != gen2


class TestNotificationInstaller:
    def test_is_notification_installed_missing(self):
        assert _is_notification_installed({}) == "missing"
        assert _is_notification_installed({"hooks": {"Notification": []}}) == "missing"

    def test_is_notification_installed_current(self):
        settings = {
            "hooks": {
                "Notification": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "cc-telegram hook",
                                "timeout": 2,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_notification_installed(settings) == "current"

    def test_install_adds_all_three(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        assert _install_hook(settings_file) == 0
        settings = json.loads(settings_file.read_text())
        assert settings["hooks"]["SessionStart"]
        assert settings["hooks"]["PreToolUse"]
        assert settings["hooks"]["Notification"]
        # Notification entry is matcher-less (like SessionStart).
        entry = settings["hooks"]["Notification"][0]
        assert "matcher" not in entry
        assert entry["hooks"][0]["command"].endswith("cc-telegram hook")

    def test_install_is_idempotent(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        _install_hook(settings_file)
        before = settings_file.read_text()
        assert _install_hook(settings_file) == 0
        assert settings_file.read_text() == before

    def test_install_adds_notification_to_existing(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        _install_hook(settings_file)
        settings = json.loads(settings_file.read_text())
        del settings["hooks"]["Notification"]
        settings_file.write_text(json.dumps(settings))
        assert _install_hook(settings_file) == 0
        settings = json.loads(settings_file.read_text())
        assert len(settings["hooks"]["Notification"]) == 1
        # SessionStart / PreToolUse untouched (still single entries).
        assert len(settings["hooks"]["SessionStart"]) == 1
        assert len(settings["hooks"]["PreToolUse"]) == 1


class TestNotificationStartupWarning:
    """The bot-startup hook-health seam also checks Notification (B-misc)."""

    def test_warns_when_notification_missing(self, tmp_path: Path):
        from cctelegram.handlers.interactive_ui import (
            warn_if_notification_hook_missing,
        )

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"hooks": {}}))
        assert warn_if_notification_hook_missing(settings_file) is True

    def test_quiet_when_notification_present(self, tmp_path: Path):
        from cctelegram.handlers.interactive_ui import (
            warn_if_notification_hook_missing,
        )

        settings_file = tmp_path / "settings.json"
        _install_hook(settings_file)
        assert warn_if_notification_hook_missing(settings_file) is False
