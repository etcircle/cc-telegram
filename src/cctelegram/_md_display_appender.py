#!/usr/bin/env python3
"""MessageDisplay hook appender — the tiny, fast capture leg of Bug 2's fix.

Claude Code's ``MessageDisplay`` hook fires with each batch of newly completed
lines while an assistant message streams to the screen — crucially, BEFORE
Claude co-flushes the whole turn (prose + the trailing AskUserQuestion /
ExitPlanMode ``tool_use``) to the session JSONL at resolution. cc-telegram
derives content from the JSONL via byte-offset reads, so during a live prompt
the explanatory prose is not yet on the bridge and the Telegram user chooses
blind. This appender captures that prose live.

It does ONE thing, as fast as possible (the streaming display path runs the
hook with ``forceSyncExecution`` — a slow hook stalls Claude's output): parse
the stdin payload, derive the session key, and append the raw payload as one
NDJSON line to ``<CC_TELEGRAM_DIR>/msg_display/<session>.ndjson``. The bot
(``md_capture.read_prose_records``) accumulates the per-flush ``delta`` values
into completed prose on demand at picker-render time. Accumulation lives
bot-side because each hook invocation is a fresh process and cannot accumulate.

Design constraints (mirroring ``hook.py``'s observer contract):
  * **Stdlib only, no package imports.** Importing ``cctelegram`` would load
    config / telegram / many modules (measured ~70ms+ for ``cc-telegram hook``,
    ~0.5s for a SessionStart hook) — far over the latency budget. Run directly
    as ``python3 _md_display_appender.py``; the package is never imported.
  * **Never raise, always exit 0.** A hook bug must never break Claude's
    output. Every failure path returns 0 silently.
  * **Session key = ``Path(transcript_path).stem``, not ``payload.session_id``.**
    Under ``claude --resume <orig>`` the SessionStart hook reports a NEW
    session_id but messages keep writing to the ORIGINAL JSONL file, and the
    bot tracks that original id. ``transcript_path`` always points at the file
    actually being written, so its stem matches the id the bot reads by — in
    both fresh and resumed launches.

Scoped to bot-launched sessions via ``claude --settings <md_hook_settings.json>``
(``md_capture.ensure_capture_settings``); it is never installed into the global
``~/.claude/settings.json``.
"""

import json
import os
import sys
import time
from pathlib import Path

_DIRNAME = "msg_display"


def _base_dir() -> Path:
    raw = os.environ.get("CC_TELEGRAM_DIR", "")
    return Path(raw).expanduser() if raw else Path.home() / ".cc-telegram"


def main() -> int:
    # ONE broad guard around the whole body. A force-sync hook must NEVER raise
    # or exit nonzero (it would stall / error Claude's streaming output), so any
    # failure is swallowed → exit 0: a malformed payload, an unwritable dir, or
    # an embedded-NUL ``transcript_path`` (which raises ``ValueError`` from
    # ``os.open`` — NOT ``OSError`` — and so must be caught here, not narrowly).
    try:
        raw = sys.stdin.read()
        if not raw:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0

        transcript_path = payload.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path:
            return 0
        key = Path(transcript_path).stem
        # ``.stem`` never contains a path separator, but reject any path-special
        # / NUL key defensively so a hostile transcript_path can neither escape
        # the capture dir nor trip a late ValueError.
        if not key or key in (".", "..") or "\x00" in key or "/" in key:
            return 0

        target_dir = _base_dir() / _DIRNAME
        # mode applies only to dirs created here; the bot also ensures 0700 on
        # startup (prose can carry sensitive context).
        os.makedirs(target_dir, mode=0o700, exist_ok=True)

        # Wrap with a capture wall-clock: the payload's own JSONL timestamp is
        # generation time, not write time (the Bug 2 timestamp trap), so the
        # bot's freshness gate needs an honest capture instant. Compact dumps
        # guarantees one line per event (no embedded newlines).
        line = json.dumps(
            {"captured_at": time.time(), "payload": payload}, separators=(",", ":")
        )
        target = target_dir / f"{key}.ndjson"
        fd = os.open(str(target), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
