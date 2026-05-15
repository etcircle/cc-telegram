<!-- /autoplan restore point: /Users/felixcardix/.gstack/projects/etcircle-cc-telegram/main-autoplan-restore-20260514-231523.md -->
# cctelegram one-word rename + ccbot → cctelegram migration

**Status:** Draft for review (CEO + Eng + DX via /autoplan).
**Owner:** Emiliyan
**Date:** 2026-05-14

## Goal

Collapse the current split identity (`cctelegram` import package / `cc-telegram` everywhere else) onto a single one-word name `cctelegram`. Cut over the live bot from `~/.ccbot` directly to `~/.cctelegram` in one hop (the previously planned `~/.cc-telegram` hop was never actually executed: the dir exists empty, no state landed there).

Single product name. One CLI. One config dir. One env prefix. Done.

## Why now

1. `~/.cc-telegram` is empty. The hyphenated identity has zero production footprint, so we pay for one cutover instead of two.
2. The `doctor --migrate` code path was built specifically for the `~/.ccbot` → `~/.cc-telegram` migration that never ran. We can retarget it to `~/.cctelegram` without losing functionality.
3. Hyphen/underscore split forces every reader to remember which surface uses which form. One word removes the foot-gun.

## Identity surfaces (current → new)

| Surface | Now | Target |
|---|---|---|
| Distribution (pyproject `name`) | `cc-telegram` | `cctelegram` |
| Console script entry | `cc-telegram` | `cctelegram` |
| Import package | `cctelegram` | `cctelegram` (no change) |
| Log namespace | `cctelegram` | `cctelegram` (no change) |
| Config directory | `~/.cc-telegram` | `~/.cctelegram` |
| Env var prefix | `CC_TELEGRAM_*` | `CC_TELEGRAM_*` (unchanged — gated D1) |
| User-facing copy | `CC Telegram` / `cc-telegram` | `cctelegram` |
| GitHub repo / cwd | `cc-telegram` | (out of scope; rename later if desired) |

Operational knobs that stay generic: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `TMUX_SESSION_NAME`, `CLAUDE_COMMAND`, `CLAUDE_CONFIG_DIR`, `OPENAI_*`, `MONITOR_POLL_INTERVAL`.

## Scope inventory (counts via grep)

- `src/`: 76 occurrences of `cc-telegram`/`CC_TELEGRAM`/`CC Telegram`/`.cc-telegram` across ~12 files.
- `tests/`: 67 occurrences across ~9 files.
- Root + docs + `.claude/` + `scripts/`: 63 occurrences (pyproject, README, CLAUDE.md, arch rules, restart script, plan history).
- 23 `CC_TELEGRAM_*` env var names in code.
- One launchd plist (`~/Library/LaunchAgents/com.felixcardix.ccbot.plist`).
- One uv-installed console binary (`~/.local/bin/cc-telegram`).
- One Claude SessionStart hook entry (`cc-telegram hook` → becomes `cctelegram hook`).

## Wave structure

### Wave 0 — Baseline + safety net

1. Snapshot the current launchd plist: `cp ~/Library/LaunchAgents/com.felixcardix.ccbot.plist /tmp/com.felixcardix.ccbot.plist.bak.$(date +%Y%m%d-%H%M%S)` (so rollback can restore the old service identity).
2. Tag git: `git tag pre-cctelegram-rename`.
3. *(WAL checkpoint moved to Wave 2 after bootout — see eng consensus H2: checkpointing a live SQLite is unreliable. Snapshot the running dir via `ditto` only after the bot is stopped.)*

### Wave 1 — Mechanical rename in the repo

Single search-replace pass on tracked files only:

- `cc-telegram` → `cctelegram` (everywhere it's an identifier or path component, not in historical plan filenames or commit messages).
- ~~`CC_TELEGRAM_` → `CCTELEGRAM_`~~ — **dropped per D1**: env vars keep underscore form for SCREAMING_SNAKE readability.
- `CC Telegram` → `cctelegram` (display copy).
- `~/.cc-telegram` → `~/.cctelegram`.

Specifics:

- `pyproject.toml`: `name = "cctelegram"`, `[project.scripts] cctelegram = "cctelegram.main:main"`.
- **Argparse `prog=` strings** (DX consensus #3): `src/cctelegram/main.py:16` and `src/cctelegram/doctor.py:96` hardcode `prog="cc-telegram"`/`prog="cc-telegram doctor"`. These appear in `--help` output and are invisible to lint/types. Search-replace must catch them; smoke gate must assert `cctelegram --help` and `cctelegram doctor --help` contain no `cc-telegram` substring.
- **`doctor.py:63-71` preflight error message** (DX consensus #2 — CRITICAL): currently emits "Run: mkdir -p ... && cp -R ...". Rewrite to lead with `cctelegram doctor --migrate` (the retry-safe path from Wave 1), and demote the raw shell command to "Manual fallback". Example new body: `"State migration required.\n\nRun:\n  cctelegram doctor --migrate\n\nManual fallback:\n  {migration_command(...)}\n\nOr override the config dir with CC_TELEGRAM_DIR=/path."`
- `src/cctelegram/utils.py`: `CC_TELEGRAM_DIR_ENV` constant stays (env var name), but default path → `~/.cctelegram`.
- `src/cctelegram/hook.py`: `_CURRENT_HOOK_COMMAND_SUFFIX = "cctelegram hook"`. **Structural change required** (eng consensus M2/H3): convert `_LEGACY_HOOK_COMMAND_SUFFIX` (str) → `_LEGACY_HOOK_COMMAND_SUFFIXES = ("ccbot hook", "cc-telegram hook")` (tuple). Update `_command_contains_legacy_hook` to iterate. Add tests for each legacy entry (exact + path-qualified).
- `src/cctelegram/doctor.py`: `NEW_DIR_NAME = ".cctelegram"`. `LEGACY_DIR_NAME = ".ccbot"` stays. **Eng consensus C2/H3**: make `migration_needed()` retry-safe — migrate into `~/.cctelegram.migrating.<pid>`, validate required files (`state.json`, `session_map.json`, `message_refs.db`), then atomic rename to `~/.cctelegram`. Add a `.migration-complete` sentinel. **Eng consensus C1**: `preflight_or_exit` should emit a loud warning when `CC_TELEGRAM_DIR` is set AND points at a legacy dir (not just silently bypass). Drop or ignore the empty `~/.cc-telegram` dir at runtime (warn if it exists with state files).
- `scripts/restart.sh`: delete. **DX consensus #4**: in the same commit, scrub `CLAUDE.md` "Common Commands" and `README.md` of any `./scripts/restart.sh` reference. Replace with the launchd kickstart one-liner if a "restart the service" snippet is still useful.
- Historical plan filenames in `docs/plans/`: leave as-is (they're history; renaming would lie about what shipped when).
- Bot user-facing strings: any messages containing "cc-telegram" or "CC Telegram" become "cctelegram".

Verification (eng consensus H4 — the standard suite misses packaging surfaces):

```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/cctelegram/
uv run pytest
```

Plus a packaging smoke test that lint/types/pytest cannot catch:

```bash
# In a throwaway venv:
uv venv /tmp/cctelegram-smoke && source /tmp/cctelegram-smoke/bin/activate
uv pip install -e .
which cctelegram && cctelegram --help && cctelegram doctor
# Confirm console-script entry actually wires to cctelegram.main:main.
```

Plus an explicit grep gate for forbidden runtime strings outside historical plan files (DX consensus #3 + #4 — must include CLAUDE.md and README.md, exclude only `docs/plans/`):

```bash
! grep -rn "cc-telegram\|\.cc-telegram\|CC Telegram" \
    src/ tests/ pyproject.toml CLAUDE.md README.md .claude/rules/ scripts/ \
    --exclude-dir=__pycache__
# Should return non-zero (no matches).

! cctelegram --help        2>&1 | grep -i "cc-telegram"
! cctelegram doctor --help 2>&1 | grep -i "cc-telegram"
# Both should exit non-zero.
```

All green before merging Wave 1.

### Wave 2 — Live cutover

**Eng consensus C1/H3 — revised ordering to close the hook race window and avoid preflight bypass.** The old order let a SessionStart fire between bootout and hook reinstall, hitting either a removed binary or the old hook path. Revised order: rewrite the hook entry FIRST (so any SessionStart routes to a known target), then keep `~/.local/bin/cc-telegram` as a transitional symlink during the cutover so a mid-window hook fire still resolves.

1. **Pre-stop hook rewrite.** While the bot is still running, rewrite `~/.claude/settings.json`'s SessionStart entry to invoke `cctelegram hook` (the legacy `cc-telegram hook` entry stays handled via the tuple from Wave 1, so this is idempotent). At this point `~/.local/bin/cctelegram` doesn't exist yet — that's fine, no Claude session is starting *right now* and we're about to install it.
2. `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist`.
3. **Now-safe WAL checkpoint (eng consensus H2):** `sqlite3 ~/.ccbot/message_refs.db 'PRAGMA wal_checkpoint(TRUNCATE);'` — bot is stopped, so this completes deterministically.
4. Snapshot: `ditto ~/.ccbot ~/.ccbot.backup.$(date +%Y%m%d-%H%M%S)`.
5. Install the new binary with a transitional **deprecation wrapper** (DX consensus #5 — bare symlink dies silently; wrapper prints a deprecation warning before exec'ing the real binary):
   ```
   uv tool install --force /Users/felixcardix/dev-workspaces/cc-telegram
   cat > ~/.local/bin/cc-telegram <<'EOF'
   #!/bin/sh
   echo "cc-telegram is renamed to cctelegram; this shim will be removed in next release" >&2
   exec ~/.local/bin/cctelegram "$@"
   EOF
   chmod +x ~/.local/bin/cc-telegram
   ```
   Wrapper stays for one release (answers open question #2 with YES). Wave 3 step 3 removes it explicitly, not silently.
6. Migrate state with the retargeted, retry-safe doctor: `cctelegram doctor --migrate`. (Per the doctor.py changes in Wave 1: migrates into `~/.cctelegram.migrating.<pid>`, validates files, atomic-renames to `~/.cctelegram`, writes `.migration-complete` sentinel.)
7. Rewrite `~/.cctelegram/.env`: rename any `CCBOT_*` flags to their `CC_TELEGRAM_*` equivalents (or drop, since most are default-equivalent in current code). Env var prefix stays `CC_TELEGRAM_*` per D1. Confirm `TMUX_SESSION_NAME="ccbot"` is preserved (session_map keys are still `ccbot:@N`).
8. Strip junk the migration copy pulled in: `launchd.err.log`, `runtime.log`, `whisper-proxy.*.log`, `whisper_openai_proxy.py`. **Must happen before step 11 (bootstrap) per eng consensus H1**, else the new bot reopens the stale log lineage. Keep `state.json`, `session_map.json`, `monitor_state.json`, `message_refs.db` (+ `-wal`, `-shm`), `.env`. Decide on `images/`, `files/`, `backups/` (probably trim).
9. Confirm hook still resolves: `cctelegram hook --install` re-asserts the entry (idempotent). Verify `_LEGACY_HOOK_COMMAND_SUFFIXES` tuple matches both `ccbot hook` and `cc-telegram hook`.
10. Rewrite the launchd plist **in place** (per D4/T1: skip the Label rename — gratuitous and adds bootout/bootstrap risk per CEO F5):
    - Keep filename `com.felixcardix.ccbot.plist` and Label `com.felixcardix.ccbot`. Yes, it's a cosmetic mismatch with the new product name; the cost of correcting it isn't worth the risk window.
    - Program: `/Users/felixcardix/.local/bin/cctelegram`.
    - EnvironmentVariables: **drop `CCBOT_DIR` entirely AND drop or update any existing `CC_TELEGRAM_DIR` value** (eng consensus C1 — the old value bypasses preflight). Set `CC_TELEGRAM_DIR=/Users/felixcardix/.cctelegram`.
    - StandardErrorPath / StandardOutPath: `~/.cctelegram/launchd.err.log` / `.out.log`.
11. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.cctelegram.plist && launchctl kickstart -k gui/$(id -u)/com.felixcardix.cctelegram`.
12. Verify:
   - `cctelegram doctor` — confirms `~/.cctelegram` in use.
   - `pgrep -fl cctelegram` shows one process.
   - `tail -100 ~/.cctelegram/launchd.err.log` is clean.
   - Open a new tmux window in session `ccbot`; confirm a fresh entry appears in `~/.cctelegram/session_map.json` (proves hook is wired).
   - Send a Telegram message to an existing bound topic; confirm round-trip.

### Wave 2.5 — Rollback drill (eng consensus M3)

Before bootstrap (step 11), document and dry-run-print the rollback. If Wave 2 fails between step 2 (bootout) and step 12 (verify), recover with:

```bash
# Stop whatever ended up running
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.cctelegram.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist     2>/dev/null || true

# Restore live state from the Wave 2 step-4 snapshot
ts=<the-timestamp-from-step-4>
rm -rf ~/.cctelegram ~/.cctelegram.migrating.*
ditto ~/.ccbot.backup.$ts ~/.ccbot   # restore on top of any partial mutation

# Restore the launchd plist from Wave 0 step 1
cp /tmp/com.felixcardix.ccbot.plist.bak.* ~/Library/LaunchAgents/com.felixcardix.ccbot.plist

# Restore the hook entry to `cc-telegram hook` (legacy installer flow)
~/.local/bin/cc-telegram hook --install   # symlink from Wave 2 step 5 still resolves

# Bring the old service back up
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.felixcardix.ccbot.plist
```

Print this script (with the real `$ts` substituted) before step 11 and confirm each path exists. Don't run it dry — just confirm the artifacts are there.

### Wave 2.75 — README + CLAUDE.md DX surfaces (folded into Wave 1 commit if convenient)

DX consensus #1, #6, #7 — three small doc additions that turn the rename from "operator-only" into "future-you-friendly":

1. **README "Quick Start"** (before any existing content): zero-to-working bot in ~5 commands. `uv tool install`, write `.env`, `cctelegram hook --install`, set up launchd (or `cctelegram` foreground), bind a topic.
2. **README "After upgrading from cc-telegram/ccbot"** section: `cctelegram doctor --migrate`, `cctelegram hook --install`, `cctelegram doctor`, restart launchd.
3. **README "Config directory override"**: document `CC_TELEGRAM_DIR=/path cctelegram` as the official override; note that override bypasses the legacy-migration preflight (so doctor explicitly warns about that case per Wave 1's C1 change).
4. **doctor extension** (per D4/T2: include): `cctelegram doctor` (no flag) also checks `TELEGRAM_BOT_TOKEN` present, `ALLOWED_USERS` non-empty, `tmux` on PATH, `claude` on PATH, hook installed and pointing at `cctelegram hook` (not legacy). Currently doctor only does the migration check. Land in same wave as the rename so future-you-on-fresh-machine can run `cctelegram doctor` and get a real readout.

### Wave 3 — Aftercare

1. Once stable for 24h: archive `~/.ccbot` (`mv ~/.ccbot ~/.ccbot.archived-$(date +%Y%m%d)`), preserving the Whisper proxy script in a known location if it's still referenced elsewhere.
2. Remove `~/.cc-telegram` (empty dir).
3. Uninstall the old uv tool: `uv tool uninstall ccbot` and (if still present) `uv tool uninstall cc-telegram`.
4. Optionally: rename the working dir `dev-workspaces/cc-telegram` → `dev-workspaces/cctelegram`. Update CLAUDE.md path references. Defer if it complicates other agent state.

## Risks + mitigations

- **`TMUX_SESSION_NAME` drift.** Session_map keys are `<tmux_session>:<window_id>` = `ccbot:@N`. Changing the tmux session name silently breaks every binding. Mitigation: explicit verification step in Wave 2.
- **Whisper proxy still points at `~/.ccbot`.** It's a separate launched service. Wave 3 archive must not delete files it reads. Mitigation: audit before archive; consider moving the proxy too if it's small.
- **Test-only `CC_TELEGRAM_*` env var leakage.** Some tests may set env vars by string. Wave 1 search-replace covers tests/, but a CI run is the real check.
- **Hook race.** Reinstalling the hook while live tmux Claude sessions exist means a SessionStart fired between bootout and hook reinstall would write to the old path. Window is seconds; mitigated by doing hook reinstall *after* state copy so even a misrouted write has the source data.
- **Doctor preflight bypass via `CC_TELEGRAM_DIR`.** Setting the env var in the plist skips the legacy-dir guard, which is the desired behavior post-migration but masks any future migration mistake. Acceptable.

## Out of scope (defer)

- GitHub remote rename / repo URL.
- Working directory rename on disk.
- Hard-deprecating the `CC_TELEGRAM_*` env vars (no users besides this one; new vars + .env rewrite is enough).
- README rewrite beyond search-replace.
- Module splits, route refactors, busy-indicator work.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail (via /autoplan)

| # | Phase | Decision | Class | Principle | Rationale |
|---|---|---|---|---|---|
| D1 | CEO premise gate | Env vars stay `CC_TELEGRAM_*` (not collapse to `CCTELEGRAM_*`) | Gate | P5 explicit over clever | SCREAMING_SNAKE convention uses underscores; collapsing creates readability regression |
| D2 | CEO premise gate | Other three premises hold (empty `~/.cc-telegram`, `cctelegram` target, single wave) | Gate | P6 bias toward action | Verified on disk; matches prior package decision |
| D3 | CEO USER CHALLENGE | Proceed with original rename (override both models' "ship cutover only" recommendation) | User Challenge | User context | User has product-direction conviction; CEO findings F1/F2/F4 logged for the audit |
| A1 | Eng C1 | Doctor warns loudly when CC_TELEGRAM_DIR set + points at legacy | Mechanical | P1 completeness | Closes silent preflight bypass |
| A2 | Eng C2 | migration_needed retry-safe via temp dir + atomic rename + sentinel | Mechanical | P1 completeness | Closes partial-copy retry hole |
| A3 | Eng H2 | WAL checkpoint moved to Wave 2 step 3 (after bootout), `TRUNCATE` not `PASSIVE` | Mechanical | P5 explicit | Live checkpoints unreliable |
| A4 | Eng H3 | Pre-stop hook rewrite + deprecation wrapper script (not bare symlink) | Mechanical | P1 completeness | Closes hook race window |
| A5 | Eng H3 / DX #5 | Transitional `cc-telegram` wrapper prints deprecation msg | Mechanical | P5 explicit | Silent symlink → silent removal = silent breakage |
| A6 | Eng H4 / DX #3 | Smoke test: install wheel, assert `--help` contains no `cc-telegram` | Mechanical | P1 completeness | Lint/types miss console-script entry |
| A7 | Eng M2 | `_LEGACY_HOOK_COMMAND_SUFFIX` (str) → `_LEGACY_HOOK_COMMAND_SUFFIXES` (tuple) | Mechanical | P5 explicit | Scalar can't represent two legacy entries |
| A8 | Eng M3 | Explicit rollback script in Wave 2.5, dry-run path-check before bootstrap | Mechanical | P1 completeness | "ditto backup" alone doesn't restore the service |
| A9 | DX #2 | `doctor.py:63-71` preflight leads with `cctelegram doctor --migrate` | Mechanical | P5 explicit | Current msg teaches the unsafe shell pipeline |
| A10 | DX #4 | Grep gate extends to CLAUDE.md, README.md, .claude/rules/, scripts/ | Mechanical | P1 completeness | Original gate missed half the surface |
| A11 | DX #4 | `scripts/restart.sh` deletion + CLAUDE.md/README scrub in same commit | Mechanical | P5 explicit | Orphan reference is a self-own |
| A12 | DX #1/#6/#7 | Add Wave 2.75: README Quick Start + post-upgrade + CC_TELEGRAM_DIR docs | Mechanical | P1 completeness | Future-you needs the path written down |
| T1 | CEO F5 | Launchd Label rename (`com.felixcardix.ccbot` → `cctelegram`) | TASTE | P3 pragmatic | Cosmetic, adds bootout/bootstrap risk; surfaced at gate |
| T2 | DX #1 step 4 | Extend `cctelegram doctor` with fresh-setup health checks | TASTE | P3 pragmatic | High-value DX add but technically out-of-scope for a rename; surfaced at gate |

## Cross-phase themes (concerns surfaced in ≥2 phases independently)

- **Theme A — Hook/preflight surface is structurally fragile across cutovers.** Eng (C1, H3) and DX (#6, #7) independently flagged the hook command + `CC_TELEGRAM_DIR` env interaction. Mitigations: pre-stop hook rewrite, retry-safe doctor, deprecation wrapper, loud preflight warnings. **Status: closed in plan revisions A1–A5, A7, A9.**
- **Theme B — Documentation has more blast radius than the source rename.** DX (#1, #3, #4, #6, #7) flagged argparse strings, README/CLAUDE.md restart.sh references, missing Quick Start, missing post-upgrade instructions, missing escape-hatch docs. Lint/types/tests catch none of these. **Status: closed by extended grep gate (A10), restart.sh scrub (A11), Wave 2.75 doc additions (A12).**

## Open questions for the review skills

1. Is collapsing distribution name and import package to the same string worth the loss of PEP-503-canonical hyphenation? (CEO/DX.)
2. Should we keep a transitional `cc-telegram` console-script alias for one release, or hard-break? (Eng.)
3. Is the launchd Label rename worth the extra step, or keep `com.felixcardix.ccbot` indefinitely? (Eng.)
4. Should the Whisper proxy migrate in the same cutover or stay as a separate workstream? (CEO scope.)
5. Is "cctelegram" the right user-facing copy, or should the human-readable name be different from the technical identifier (e.g., "CC Telegram" the product, `cctelegram` the binary)? (CEO/DX.)
