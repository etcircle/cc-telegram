"""Tests for Claude Code session tracking hook."""

import io
import json
import sys

import pytest

from cctelegram.hook import _UUID_RE, _install_hook, _is_hook_installed, hook_main


def _settings_with_commands(commands: list[str]) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 5}
                        for command in commands
                    ]
                }
            ]
        }
    }


def _commands_from_settings(settings: dict) -> list[str]:
    return [
        hook["command"]
        for entry in settings.get("hooks", {}).get("SessionStart", [])
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict) and isinstance(hook.get("command"), str)
    ]


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) == "missing"

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) == "missing"

    @pytest.mark.parametrize(
        "command",
        [
            "echo cc-telegram hook",
            "echo /usr/local/bin/cc-telegram hook",
            "# cc-telegram hook",
            "cc-telegram hook # comment",
            "cc-telegram hook && other-tool hook",
            "true&&/usr/local/bin/cc-telegram hook",
            "CCT=/usr/local/bin/cc-telegram hook",
            "https://example.test/cc-telegram hook",
            "sh -c 'cc-telegram hook'",
            "some-cc-telegram hook",
        ],
        ids=[
            "echo-exact",
            "echo-path-suffix",
            "comment",
            "trailing-comment",
            "shell-chain",
            "shell-chain-no-spaces",
            "assignment-prefix",
            "url-mention",
            "shell-wrapper",
            "substring-command",
        ],
    )
    def test_wrapper_mentions_are_not_classified_as_installed(
        self, command: str, tmp_path, monkeypatch
    ) -> None:
        """Wrapper / comment strings containing the hook command must not count
        as an installed hook, and ``_install_hook`` must add a fresh one
        alongside without rewriting the wrapper."""
        settings = _settings_with_commands([command])
        assert _is_hook_installed(settings) == "missing"

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )

        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        commands = _commands_from_settings(data)
        assert commands == [command, "cc-telegram hook"]

    def test_current_hook_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "cc-telegram hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) == "current"

    def test_path_qualified_current_hook_is_idempotent(
        self, tmp_path, monkeypatch
    ) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps(_settings_with_commands(["/opt/bin/cc-telegram hook"]))
        )
        monkeypatch.setattr(
            "cctelegram.hook._find_cc_telegram_path", lambda: "cc-telegram"
        )

        assert _is_hook_installed(json.loads(settings_file.read_text())) == "current"
        assert _install_hook(settings_file=settings_file) == 0
        data = json.loads(settings_file.read_text())
        assert _commands_from_settings(data) == ["/opt/bin/cc-telegram hook"]


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["cc-telegram", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()
