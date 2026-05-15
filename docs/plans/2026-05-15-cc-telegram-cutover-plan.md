# cc-telegram cutover: `~/.ccbot` → `~/.cc-telegram`

**Status:** Ready to execute.
**Owner:** Emiliyan
**Date:** 2026-05-15
**Supersedes:** `2026-05-14-cctelegram-onename-rename-plan-SUPERSEDED.md` (the one-word rename was dropped after CEO review surfaced the multi-transport regret risk — see that plan's audit trail for context).

## Goal

Retire `~/.ccbot` as the live state directory. Migrate to `~/.cc-telegram` (the already-documented target). Repoint launchd, refresh the hook, verify the bot resumes seamlessly.

**Out of scope:** any source-level rename. The identity stays exactly as it is today: distribution `cc-telegram`, import package `cctelegram`, CLI `cc-telegram`, config dir `~/.cc-telegram`, env prefix `CC_TELEGRAM_*`. Hyphen/no-hyphen split is standard Python convention and not worth churning.

## Why now

`~/.ccbot` is live but rotting: 80 MB launchd log, 12 MB runtime log, a Whisper proxy script with no other home. The `~/.cc-telegram` target dir exists empty (created during a previous setup attempt, never populated). The migration is a 30-minute task that unblocks the rename of identity surfaces that already shipped in May.

## Code prep (one PR, ~half-day)

Before the live cutover, four small code changes that came out of the dual-voice review of the superseded plan and still apply:

1. **`src/cctelegram/doctor.py` — retry-safe migration.** Currently `migration_needed()` returns false the moment `~/.cc-telegram` exists, so a partial-copy retry refuses to proceed. Refactor `_copy_tree_contents` to:
   - Stage into `~/.cc-telegram.migrating.<pid>`.
   - Validate the expected files are present (`state.json`, `session_map.json`, `message_refs.db`).
   - Atomic-rename to `~/.cc-telegram`.
   - Write a `.migration-complete` sentinel.
   - On retry, if the sentinel exists, exit OK; if a `.migrating.*` dir exists, clean it up.

2. **`src/cctelegram/doctor.py:48-72` — preflight error message.** Today it tells the user to run `mkdir -p ... && cp -R ...`. That's the unsafe pre-doctor path. Lead with the right command:
   ```
   State migration required.

   Run:
     cc-telegram doctor --migrate

   Manual fallback:
     mkdir -p ~/.cc-telegram && cp -R ~/.ccbot/. ~/.cc-telegram/

   Or point at a different config dir with CC_TELEGRAM_DIR=/path.
   ```
   Also, when `CC_TELEGRAM_DIR` is set, emit a one-line warning to stderr if it resolves to a legacy-looking dir (`.ccbot` substring, or contains a `ccbot:` session_map key) instead of silently bypassing the guard.

3. **`src/cctelegram/doctor.py` — fresh-setup health checks.** Extend `cc-telegram doctor` (no flag) beyond the migration check. Verify:
   - `TELEGRAM_BOT_TOKEN` set in resolved `.env`.
   - `ALLOWED_USERS` non-empty.
   - `tmux` on PATH.
   - `claude` on PATH.
   - SessionStart hook present in `~/.claude/settings.json` and matches `_CURRENT_HOOK_COMMAND_SUFFIX`.
   - Config dir exists and is writable.
   Each check prints OK / WARN / FAIL with a one-line fix hint. Output is grep-friendly so future-me (or `cc-telegram doctor 2>&1 | tee`) gets an actionable readout.

4. **`scripts/restart.sh` deletion + doc scrub.** The script doesn't match how the bot actually starts on this machine (launchd-managed; the global CLAUDE.md memory explicitly forbids running it). Delete it. In the same commit:
   - Remove the `./scripts/restart.sh` line from `CLAUDE.md` "Common Commands".
   - Remove any equivalent reference from `README.md` (if present).
   - Add a "Restart the service" snippet under README that uses `launchctl kickstart -k gui/$(id -u)/com.felixcardix.ccbot`.

**Verification before merge:**
```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/cctelegram/
uv run pytest
```

Plus a manual sanity check: in a throwaway dir, `CC_TELEGRAM_DIR=/tmp/cc-telegram-test cc-telegram doctor` should print the health-check readout without trying to migrate.

## Wave 0 — Safety net (before touching anything live)

1. `cp ~/Library/LaunchAgents/com.felixcardix.ccbot.plist /tmp/com.felixcardix.ccbot.plist.bak.$(date +%Y%m%d-%H%M%S)` — rollback artifact.
2. `git tag pre-cc-telegram-cutover` on this branch.

## Wave 1 — Live cutover (~30 minutes, single sitting)

Ordering matters. Don't shuffle these steps.

1. **Stop the bot.**
   ```
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist
   ```

2. **Checkpoint SQLite WAL (now safe — no writer).**
   ```
   sqlite3 ~/.ccbot/message_refs.db 'PRAGMA wal_checkpoint(TRUNCATE);'
   ```

3. **Snapshot the source directory.**
   ```
   ts=$(date +%Y%m%d-%H%M%S)
   ditto ~/.ccbot ~/.ccbot.backup.$ts
   ```

4. **Clear the empty target so doctor will accept the migration.**
   ```
   rmdir ~/.cc-telegram   # safe — verified empty earlier
   ```

5. **Run the migration.**
   ```
   cc-telegram doctor --migrate
   ```
   (After the code-prep PR, this stages into `.migrating.<pid>`, validates, atomic-renames, writes sentinel.)

6. **Strip junk that came along.**
   ```
   rm -f ~/.cc-telegram/launchd.err.log \
         ~/.cc-telegram/launchd.out.log \
         ~/.cc-telegram/runtime.log \
         ~/.cc-telegram/whisper-proxy.*.log \
         ~/.cc-telegram/whisper_openai_proxy.py
   # Decide separately whether to trim images/ files/ backups/
   ```

7. **Rewrite the live `.env` if needed.** Any `CCBOT_*` env entries → drop them (most are default-equivalent in current code). Confirm `TMUX_SESSION_NAME="ccbot"` is preserved — session_map keys are `ccbot:@N` and changing the tmux session name silently breaks every binding.

8. **Reinstall the Claude SessionStart hook.**
   ```
   cc-telegram hook --install
   ```
   This rewrites any legacy `ccbot hook` entry to `cc-telegram hook` in `~/.claude/settings.json`. The binary path doesn't change, so no race window.

9. **Rewrite the launchd plist** in place (keep filename and Label `com.felixcardix.ccbot` — cosmetic mismatch is fine, not worth a bootout/bootstrap dance):
   - `Program`: still `/Users/felixcardix/.local/bin/cc-telegram`.
   - `EnvironmentVariables`: **drop `CCBOT_DIR`**. Add `CC_TELEGRAM_DIR=/Users/felixcardix/.cc-telegram`.
   - `StandardErrorPath`: `/Users/felixcardix/.cc-telegram/launchd.err.log`.
   - `StandardOutPath`: `/Users/felixcardix/.cc-telegram/launchd.out.log`.

10. **Start the bot.**
    ```
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist
    launchctl kickstart -k gui/$(id -u)/com.felixcardix.ccbot
    ```

11. **Verify.**
    ```
    cc-telegram doctor                                     # should print healthy readout
    pgrep -fl cc-telegram                                  # one process
    tail -100 ~/.cc-telegram/launchd.err.log               # clean
    jq 'keys' ~/.cc-telegram/session_map.json | head       # entries start with "ccbot:"
    ```
    Plus a live smoke test: send a Telegram message to an existing bound topic; confirm round-trip. Then open a fresh tmux window in the `ccbot` session; confirm a new entry appears in `~/.cc-telegram/session_map.json` (proves the hook is wired to the right dir).

## Wave 1.5 — Rollback (only if Wave 1 fails between step 1 and step 11)

```bash
ts=<the-timestamp-from-Wave-1-step-3>
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist 2>/dev/null || true
rm -rf ~/.cc-telegram ~/.cc-telegram.migrating.*
ditto ~/.ccbot.backup.$ts ~/.ccbot
cp /tmp/com.felixcardix.ccbot.plist.bak.* ~/Library/LaunchAgents/com.felixcardix.ccbot.plist
cc-telegram hook --install   # re-asserts hook against the old plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist
```

Before running Wave 1 step 10 (`bootstrap`), print the rollback script with the real `$ts` substituted and confirm each artifact exists. Don't execute it — just confirm presence.

## Wave 2 — Doc additions (same PR as code prep, or a follow-up)

Three small README sections that the code-prep PR doesn't cover but future-you-on-a-fresh-machine will need:

1. **README "Quick start"** — zero to working bot in ~5 commands:
   ```
   git clone <repo> && cd cc-telegram
   uv tool install --force .
   mkdir -p ~/.cc-telegram && $EDITOR ~/.cc-telegram/.env  # TELEGRAM_BOT_TOKEN, ALLOWED_USERS, TMUX_SESSION_NAME, CLAUDE_COMMAND
   cc-telegram hook --install
   cc-telegram doctor       # verify all green
   # Then either: cc-telegram (foreground) or install the launchd plist
   ```
2. **README "After upgrading from ccbot"** — `cc-telegram doctor --migrate`, `cc-telegram hook --install`, restart launchd.
3. **README "Config directory override"** — `CC_TELEGRAM_DIR=/path cc-telegram` as the official override, with the note that overriding bypasses the migration preflight unless doctor warns about it (per the code-prep step 2 change).

## Wave 3 — Aftercare (after 24h of stable operation)

1. `mv ~/.ccbot ~/.ccbot.archived-$(date +%Y%m%d)`.
2. Audit anything that still reads from `~/.ccbot`:
   - `whisper_openai_proxy.py` (already in `~/.ccbot.backup.$ts` and the archive — decide whether to move it somewhere stable like `~/.local/share/whisper-proxy/`).
   - Any cron / launchd / shell aliases referencing the old path.
3. After 7 days of confidence: delete `~/.ccbot.backup.$ts` and `~/.ccbot.archived-*` (the backup chain only matters during the bedding-in period).

## Risks

- **`TMUX_SESSION_NAME` drift.** session_map keys are `ccbot:@N`. Changing the tmux session name during this cutover would silently break every binding. Mitigation: explicit verify step in Wave 1 step 11 (`jq 'keys' ... | head` should show `ccbot:` prefix).
- **Whisper proxy orphan.** Lives in `~/.ccbot`, referenced by some other service. Mitigation: Wave 3 audit before archival.
- **Hook race (residual).** A Claude SessionStart that fires between bootout (step 1) and bootstrap (step 10) runs `cc-telegram hook` against the new state dir but the bot isn't up to read the resulting map. The hook just writes to `~/.cc-telegram/session_map.json` (or wherever `CC_TELEGRAM_DIR` resolves) — when the bot starts, it picks up that entry. Low-risk because the binary path is unchanged.

## Decisions inherited from the superseded plan

- D1: env vars stay `CC_TELEGRAM_*` (underscore form, SCREAMING_SNAKE).
- D3 (overridden 2026-05-15): the original D3 was "proceed with one-word rename"; user reversed after considering multi-transport future. This plan is the materialization of "Option B" from D3 (cutover-only).
- T1: launchd Label stays `com.felixcardix.ccbot` — gratuitous to rename.
- T2: `cc-telegram doctor` grows fresh-setup health checks (code-prep step 3).

## Out of scope (defer)

- Any source-level rename to `cctelegram` or other one-word form.
- GitHub repo/remote rename.
- Working directory rename on disk.
- Multi-transport refactor (Slack / Discord / Web). When that happens, revisit the product name then.
