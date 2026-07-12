"""Tests for CC Telegram doctor health checks.

Covers the fresh-setup health-check readout: env precedence, tmux/claude on
PATH, all three managed hooks, and config dir writability. All tests run
against tmp_path; HOME is monkeypatched.
"""

import json
from pathlib import Path

import pytest

from cctelegram import doctor


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / ".cc-telegram"
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))
    return target


def _stub_environment_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)


def _stub_environment_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    monkeypatch.setenv("ALLOWED_USERS", "1234")
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"tmux", "claude"} else None,
    )
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": "cc-telegram hook"}]},
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "AskUserQuestion",
                            "hooks": [{"command": "cc-telegram hook"}],
                        }
                    ],
                    "Notification": [
                        {"hooks": [{"command": "cc-telegram hook"}]},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )


class TestHealthChecks:
    def test_health_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        (target / ".env").write_text(
            'TELEGRAM_BOT_TOKEN="abc"\nALLOWED_USERS=123\n',
            encoding="utf-8",
        )

        # tmux_manager.py monkey-patches process-wide shutil.which to cache the
        # tmux binary path. Override the doctor module's shutil.which directly
        # so the health probe sees what we want regardless of import order.
        fake_which = {"tmux": "/usr/local/bin/tmux", "claude": "/usr/local/bin/claude"}
        monkeypatch.setattr(
            doctor.shutil, "which", lambda cmd, *a, **k: fake_which.get(cmd)
        )

        # Hook installed in fake home settings.json.
        settings = tmp_path / "home" / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "cc-telegram hook",
                                    }
                                ]
                            }
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "AskUserQuestion",
                                "hooks": [{"command": "cc-telegram hook"}],
                            }
                        ],
                        "Notification": [
                            {"hooks": [{"command": "cc-telegram hook"}]},
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

        _stub_environment_clean(monkeypatch)
        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "OK   TELEGRAM_BOT_TOKEN" in out
        assert "OK   ALLOWED_USERS" in out
        assert "OK   tmux on PATH" in out
        assert "OK   claude on PATH" in out
        assert "OK   SessionStart hook" in out
        assert "OK   PreToolUse(AskUserQuestion) hook" in out
        assert "OK   Notification hook" in out
        assert "OK   config dir writable" in out
        assert "8 ok / 0 warn / 0 fail" in out

    def test_health_reports_missing_token_and_missing_tools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        # No .env, no env vars, no tmux/claude binaries discoverable.
        _stub_environment_clean(monkeypatch)
        monkeypatch.setattr(doctor.shutil, "which", lambda cmd, *a, **k: None)

        # Settings file absent so hook check fails too.
        assert doctor.doctor_main([]) == 1

        out = capsys.readouterr().out
        assert "FAIL TELEGRAM_BOT_TOKEN" in out
        assert "FAIL ALLOWED_USERS" in out
        assert "FAIL tmux not on PATH (fix: brew install tmux)" in out
        assert "FAIL claude not on PATH (fix: install Claude Code CLI)" in out
        assert "FAIL SessionStart hook" in out
        assert "WARN PreToolUse(AskUserQuestion) hook" in out
        assert "WARN Notification hook" in out
        # config dir is writable; that one is OK.
        assert "OK   config dir writable" in out
        summary = [line for line in out.splitlines() if line.endswith(" fail")][-1]
        assert summary.startswith("1 ok / 2 warn / 5 fail")

    def test_health_warns_when_hook_missing_with_settings_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        _stub_environment_healthy(monkeypatch)
        # Override the healthy stub: settings file exists but contains no hook.
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "WARN SessionStart hook" in out
        assert "WARN PreToolUse(AskUserQuestion) hook" in out
        assert "WARN Notification hook" in out
        assert "cc-telegram hook --install" in out


@pytest.mark.parametrize("name", ["TELEGRAM_BOT_TOKEN", "ALLOWED_USERS"])
@pytest.mark.parametrize("config_present", [False, True])
@pytest.mark.parametrize("local_present", [False, True])
@pytest.mark.parametrize("env_state", ["set", "empty", "absent"])
def test_required_key_presence_precedence_matrix(
    name: str,
    config_present: bool,
    local_present: bool,
    env_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _isolate_home(tmp_path, monkeypatch)
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    if config_present:
        (target / ".env").write_text(f"{name}=config-value\n", encoding="utf-8")
    if local_present:
        (tmp_path / ".env").write_text(f"{name}=local-value\n", encoding="utf-8")

    if env_state == "set":
        monkeypatch.setenv(name, "env-value")
        expected = "env-value"
    elif env_state == "empty":
        monkeypatch.setenv(name, "")
        expected = ""
    else:
        monkeypatch.delenv(name, raising=False)
        expected = (
            "local-value" if local_present else "config-value" if config_present else ""
        )

    assert doctor._check_env_value(name, target) == expected


def test_empty_environment_value_shadows_dotenv_and_fails_health_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _isolate_home(tmp_path, monkeypatch)
    target.mkdir()
    (target / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=config-token\nALLOWED_USERS=1234\n", encoding="utf-8"
    )
    _stub_environment_healthy(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    assert doctor.doctor_main([]) == 1
    assert "FAIL TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_whitespace_only_value_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _isolate_home(tmp_path, monkeypatch)
    target.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")

    assert doctor._check_env_value("TELEGRAM_BOT_TOKEN", target) == ""
