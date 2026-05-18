"""Doctor health checks for CC Telegram.

Reports the state of the local install: required env vars, tmux + claude on
PATH, the Claude Code SessionStart hook, and the config directory.
"""

import argparse
import json
import os
import shutil
from pathlib import Path

from .utils import app_dir


def _check_env_value(name: str, app_dir_path: Path) -> str:
    """Return env var value, falling back to value parsed from app_dir/.env."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_file = app_dir_path / ".env"
    if not env_file.is_file():
        return ""
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() != name:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
                val = val[1:-1]
            return val
    except OSError:
        return ""
    return ""


def _check_hook_installed(settings_file: Path) -> tuple[str, str]:
    """Return (status, detail) where status is OK | WARN | FAIL."""
    if not settings_file.is_file():
        return "FAIL", f"{settings_file} not found"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return "FAIL", f"could not parse {settings_file}: {e}"
    if not isinstance(settings, dict):
        return "FAIL", f"{settings_file} is not a JSON object"
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", []) if isinstance(hooks, dict) else []
    if not isinstance(session_start, list):
        return "FAIL", "hooks.SessionStart is not a list"
    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            continue
        for h in inner:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if isinstance(cmd, str) and "cc-telegram hook" in cmd:
                return "OK", ""
    return "WARN", "SessionStart hook missing"


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
    hook_status, hook_detail = _check_hook_installed(settings_file)
    label = "SessionStart hook"
    if hook_status == "OK":
        emit("OK", label)
    elif hook_status == "WARN":
        emit("WARN", f"{label}: {hook_detail}", "run `cc-telegram hook --install`")
    else:
        emit("FAIL", f"{label}: {hook_detail}", "run `cc-telegram hook --install`")

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
