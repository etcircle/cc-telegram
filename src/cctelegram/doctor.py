"""Doctor health checks for CC Telegram.

Reports the state of the local install: required env vars, tmux + claude on
PATH, the three managed Claude Code hooks, and the config directory.

Required-key precedence mirrors ``Config``: an existing process environment
key wins even when empty, followed by cwd-local ``.env`` and then the config-dir
``.env``. Doctor deliberately rejects whitespace-only values, which ``Config``
may technically accept but which indicate a broken deployment. Each dotenv
file is parsed independently; staged cross-file interpolation is out of scope
for this diagnostic.
"""

import argparse
import json
import os
import shutil
from pathlib import Path

from dotenv import dotenv_values

from .utils import app_dir


def _check_env_value(name: str, app_dir_path: Path) -> str:
    """Return the stripped effective required-key value.

    Key presence, including an empty process value, is first-wins. This tracks
    ``config.py``'s two ``load_dotenv(..., override=False)`` calls without
    importing the stateful ``Config`` singleton.
    """
    if name in os.environ:
        return os.environ[name].strip()

    local_values = dotenv_values(Path(".env"))
    config_values = dotenv_values(app_dir_path / ".env")
    if name in local_values:
        return (local_values[name] or "").strip()

    return (config_values.get(name) or "").strip()


def _load_hook_settings(settings_file: Path) -> tuple[dict | None, str]:
    """Return parsed hook settings and an error detail when unavailable."""
    if not settings_file.is_file():
        return None, f"{settings_file} not found"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"could not parse {settings_file}: {e}"
    if not isinstance(settings, dict):
        return None, f"{settings_file} is not a JSON object"
    return settings, ""


def _managed_hook_present(
    settings: dict, event: str, *, matcher: str | None = None
) -> tuple[bool, str]:
    """Return whether one managed command exists for an event/matcher."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False, "hooks is not a JSON object"
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        return False, f"hooks.{event} is not a list"
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if matcher is not None and entry.get("matcher") != matcher:
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if isinstance(cmd, str) and "cc-telegram hook" in cmd:
                return True, ""
    return False, f"{event} hook missing"


def _run_health_checks(target: Path) -> tuple[int, int, int]:
    """Print one line per check and return (ok, warn, fail) counts."""
    ok = 0
    warn = 0
    fail = 0

    def emit(status: str, label: str, fix: str = "") -> None:
        nonlocal ok, warn, fail
        if status == "OK":
            ok += 1
            print(f"OK   {label}")
        elif status == "WARN":
            warn += 1
            suffix = f" (fix: {fix})" if fix else ""
            print(f"WARN {label}{suffix}")
        else:
            fail += 1
            suffix = f" (fix: {fix})" if fix else ""
            print(f"FAIL {label}{suffix}")

    token = _check_env_value("TELEGRAM_BOT_TOKEN", target)
    if token:
        emit("OK", "TELEGRAM_BOT_TOKEN")
    else:
        emit(
            "FAIL",
            "TELEGRAM_BOT_TOKEN",
            f"set in {target}/.env or export TELEGRAM_BOT_TOKEN",
        )

    allowed = _check_env_value("ALLOWED_USERS", target)
    if allowed:
        emit("OK", "ALLOWED_USERS")
    else:
        emit(
            "FAIL",
            "ALLOWED_USERS",
            f"set in {target}/.env or export ALLOWED_USERS",
        )

    if shutil.which("tmux"):
        emit("OK", "tmux on PATH")
    else:
        emit("FAIL", "tmux not on PATH", "brew install tmux")

    if shutil.which("claude"):
        emit("OK", "claude on PATH")
    else:
        emit("FAIL", "claude not on PATH", "install Claude Code CLI")

    settings_file = Path.home() / ".claude" / "settings.json"
    settings, settings_error = _load_hook_settings(settings_file)
    hook_specs = (
        ("SessionStart", None, "SessionStart hook"),
        ("PreToolUse", "AskUserQuestion", "PreToolUse(AskUserQuestion) hook"),
        ("Notification", None, "Notification hook"),
    )
    for event, matcher, label in hook_specs:
        if settings is None:
            status = "FAIL" if event == "SessionStart" else "WARN"
            emit(status, f"{label}: {settings_error}", "cc-telegram hook --install")
            continue
        present, detail = _managed_hook_present(settings, event, matcher=matcher)
        if present:
            emit("OK", label)
        else:
            status = (
                "FAIL"
                if event == "SessionStart" and not detail.endswith("hook missing")
                else "WARN"
            )
            emit(status, f"{label}: {detail}", "cc-telegram hook --install")

    if target.is_dir() and os.access(target, os.W_OK):
        emit("OK", f"config dir writable ({target})")
    else:
        emit(
            "FAIL",
            f"config dir not writable ({target})",
            f"mkdir -p {target} && chmod u+rwx {target}",
        )

    print(f"{ok} ok / {warn} warn / {fail} fail")
    return ok, warn, fail


def doctor_main(argv: list[str] | None = None) -> int:
    """Run fresh-setup health checks."""
    parser = argparse.ArgumentParser(
        prog="cc-telegram doctor",
        description="Check CC Telegram config/health.",
    )
    parser.parse_args(argv)

    target = app_dir()
    print(f"OK: CC Telegram state dir is {target}")
    print()

    _, _, fail = _run_health_checks(target)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(doctor_main())
