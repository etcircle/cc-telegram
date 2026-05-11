"""Doctor and migration preflight for CC Telegram.

Owns the explicit one-shot migration from ~/.ccbot to ~/.cc-telegram and the
bot-start guard that prevents accidental fresh-state startup after the rename.
"""

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

from .utils import app_dir

LEGACY_DIR_NAME = ".ccbot"
NEW_DIR_NAME = ".cc-telegram"
OBVIOUS_STATE_FILES = (
    "state.json",
    "session_map.json",
    "monitor_state.json",
    "message_refs.db",
)


def _default_legacy_dir() -> Path:
    return Path.home() / LEGACY_DIR_NAME


def migration_command(
    legacy_dir: Path | None = None, new_dir: Path | None = None
) -> str:
    """Return the explicit shell command for copying legacy state."""
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    return f"mkdir -p {shlex.quote(str(target))} && cp -R {shlex.quote(str(legacy))}/. {shlex.quote(str(target))}/"


def migration_needed(
    legacy_dir: Path | None = None, new_dir: Path | None = None
) -> bool:
    """Return True when legacy state exists and the new app dir is absent."""
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    return legacy.exists() and not target.exists()


def preflight_or_exit(
    legacy_dir: Path | None = None,
    new_dir: Path | None = None,
) -> None:
    """Abort bot startup if legacy state needs an explicit migration.

    Setting CC_TELEGRAM_DIR is treated as an explicit operator choice and skips
    the legacy-dir guard. Hook and doctor subcommands never call this function.
    """
    if os.environ.get("CC_TELEGRAM_DIR"):
        return
    legacy = legacy_dir or _default_legacy_dir()
    target = new_dir or app_dir()
    if not migration_needed(legacy, target):
        return
    print(
        "CC Telegram state migration required before starting the bot.\n"
        f"Legacy state exists: {legacy}\n"
        f"New state dir missing: {target}\n\n"
        "Run:\n"
        f"  {migration_command(legacy, target)}\n\n"
        "Or choose an explicit config directory with CC_TELEGRAM_DIR=...",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _copy_tree_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _describe_target_state_files(target: Path) -> list[str]:
    """Return human-readable status lines for well-known target state files."""
    return [
        f"  - {name}: {'present' if (target / name).exists() else 'missing'}"
        for name in OBVIOUS_STATE_FILES
    ]


def doctor_main(argv: list[str] | None = None) -> int:
    """Run migration diagnostics, optionally copying legacy state."""
    parser = argparse.ArgumentParser(
        prog="cc-telegram doctor",
        description="Check CC Telegram config/state migration status.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Copy ~/.ccbot contents into ~/.cc-telegram if migration is needed.",
    )
    args = parser.parse_args(argv)

    legacy = _default_legacy_dir()
    target = app_dir()

    if migration_needed(legacy, target):
        if args.migrate:
            _copy_tree_contents(legacy, target)
            print(f"Migrated {legacy} -> {target}")
            return 0
        print("Migration available:")
        print(f"  legacy: {legacy}")
        print(f"  target: {target}")
        print("Run:")
        print(f"  {migration_command(legacy, target)}")
        print("Or run: cc-telegram doctor --migrate")
        return 0

    if legacy.exists() and target.exists():
        if args.migrate:
            print("ERROR: migration skipped because target state dir already exists.")
            print(f"  legacy: {legacy}")
            print(f"  target: {target}")
            print("Target obvious state files:")
            for line in _describe_target_state_files(target):
                print(line)
            print("\nNo files were copied to avoid overwriting existing state.")
            print("Review both directories, then migrate manually if intended:")
            print(f"  {migration_command(legacy, target)}")
            return 1
        print(f"OK: both legacy and new state dirs exist ({legacy}, {target}).")
        print("Runtime uses only the new state dir unless CC_TELEGRAM_DIR is set.")
        return 0

    print(f"OK: CC Telegram state dir is {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(doctor_main())
