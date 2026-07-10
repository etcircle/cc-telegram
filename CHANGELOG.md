# Changelog

All notable changes to cc-telegram. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project's package version is bumped per release, not per deploy (see the `--no-cache` note in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

## [0.3.0] — 2026-07-10

The "typing truth + supervision surfaces" release: ~159 commits since v0.2.1 making the bridge
tell you honestly what your machine is doing — background agents, Workflows, agent-teams
teammates, and background shells all keep the `typing…` indicator and 🟡/🔔 signals accurate —
plus new bot-side surfaces (`/dashboard`, `/settings`, `/update`, `/cost` + `/usage`, file
downloads, AFK late-answers, opt-in approval/decision cards) that make Telegram a place you can
actually supervise from, not just watch.

### Added
- **Background-agent + Workflow busy signals, made complete.** `run_in_background` Agents, the
  `Workflow` tool, and background shells now keep typing + 🟡 Busy on while they work, even after
  the parent turn ends and even across a bot restart:
  - GH #44 snapshot projection lifts a stored-idle route to visible RUNNING while a live
    background key exists; the ISSUE-6 `wf-task:` Workflow bracket + mtime heartbeat and the
    background-Bash `backgroundTaskId` lane extend it to Workflows and background shells.
  - `↳` sub-agent display cards for Workflow sidechains, collapsing to one line on completion
    (ISSUE-6 Fix 5), plus a startup reconciler that re-lights still-running background Agents and
    Workflows after a `launchctl kickstart`.
  - A persistent, audible **"🔔 Claude needs a decision"** card for approval waits that leave no
    JSONL trace, with a typed clear-reason channel so it dismisses only on genuine resolution.
- **Agent-teams teammate tracking (GH #46).** A teammate spawned into the session is now a
  first-class background key: its park/idle-notification closes the key promptly instead of
  stranding typing/Busy for two hours (PR-1), and a generational registry keeps typing on while a
  teammate genuinely works across the parent's own turns, relights it when re-woken, and never
  strands on a stale same-name sidechain file (PR-2).
- **Background-only "labeled silence" card** — when only a background task is working and the topic
  is otherwise silent, one `⏳ Background work running` line explains the quiet.
- **Cross-topic dashboard** (`/dashboard`) — one owner+chat-scoped overview message listing every
  bound topic, needs-attention-first (🔔 / 🟡 / ⚪ / ⏳), repainted by the poller; `/dashboard pin`
  opt-in.
- **Per-user output verbosity** (`/settings`) — `verbose` / `standard` / `compact` / `quiet`
  presets plus per-knob overrides, persisted per user; the activity card collapses to a one-line
  summary when a turn ends (per-policy).
- **Artifact download lane** — a `📎` tap-to-download card when Claude names a deliverable file, and
  a durable `/file <path>` escape hatch. Every offer is filesystem-validated (containment,
  `O_NOFOLLOW`, size cap, fd-based upload) and confined to the session directory or a configured
  artifact root. Covers docs/images/audio/video/archives/office/data; source-code paths are never
  offered.
- **AFK late-answer cards.** On Claude Code ≥2.1.198 an unanswered AskUserQuestion self-resolves at
  ~60s; instead of deleting the picker, the bridge converts it in place to an honest
  "⏰ Claude proceeded after ~60s" card whose buttons deliver your choice as a normal course-correction
  message.
- **`/update`** (owner-only) — updates the Claude Code CLI and restarts idle sessions in place
  (`claude --resume`, routing preserved). Scoped to the invoking topic by default; `/update all`
  walks every bound topic. Idle-only, fail-closed, single-flight, with a post-`/exit` quarantine
  that refuses to type into a window until Claude is proven alive again.
- **`/cost` and `/usage`** — bot-side interceptors for the usage/limits TUI overlay (which writes
  no JSONL and would otherwise freeze the topic): idle preflight → capture → parse → conditional
  auto-dismiss. When the session is busy, they reply with a bridge-side snapshot (context usage +
  cached limits) instead of a dead-end refusal.
- **Opt-in approval + decision cards** (flag-gated, default off) — Permission and Workflow-launch
  gates (`CC_TELEGRAM_PERMISSION_PROMPTS`) and generic numbered confirmation prompts
  (`CC_TELEGRAM_DECISION_CARDS`) surface as cards; `CC_TELEGRAM_DECISION_DISPATCH` adds verified
  one-tap dispatch for prompt families and Claude Code versions cc-telegram has explicitly
  characterised.
- **Machine-surface window geometry** — bot-created tmux windows default to `160×50`
  (`CC_TELEGRAM_WINDOW_GEOMETRY`) so a tall picker stays fully on-screen and wide option labels
  stop overflowing; the terminal is a machine surface, so the geometry serves the parser.
- **WSL session-binding support** — `CC_TELEGRAM_HOOK_TIMEOUT` and a tmux-3.4-compatible field
  separator, ported from the original repo, plus a directory-browser fix for Windows mounts.

### Changed
- **`/update` is topic-scoped by default** (owner decision 2026-07-10) — the fleet walk revived
  idle sessions in dormant topics, and a revived idle session is not free (background token drip),
  so fleet-wide restarts moved behind the explicit `/update all`. The scoped form re-resolves its
  target window after the up-to-120s CLI phase, so a topic rebound mid-update still restarts the
  right window.
- **True typing cadence for multi-topic forums** — `typing_action_loop` now holds a real
  start-to-start interval (elapsed-compensated), and `sendChatAction` (typing) is exempted from
  Telegram's per-group 20/60s bucket via a new `TypingAwareRateLimiter`, so the indicator no longer
  blinks with ≥2 busy topics and typing no longer starves content sends.
- **Per-key background-agent TTLs** — launched/`is_background` keys age by a 2 h TTL, foreground-
  presumed keys keep the 30-min one, unifying the typing story across sync and async work.

### Fixed
- **CC 2.1.206 ghost-suggestion false refusals** — dim (SGR-2) contextual suggestion text in the
  empty input row read as a typed draft, causing false `/cost` refusals and `/update` deferrals; a
  full-SGR-state-machine pre-clean blanks a fully-dim ghost while leaving a real draft untouched
  (fail-closed).
- **`/cost` dead-end refusals** — a busy/draft refusal now always appends a bridge-side snapshot
  rather than leaving the user with nothing.
- **AskUserQuestion v2.1.168 dispatch regression** — a bare digit no longer reliably selects, so
  the pick now navigates the cursor to the target, verifies, and presses Enter, recording
  `dispatched` only after the pane confirms the exact advance (restart-safe via the action ledger +
  mint-intent store).
- **Queue-shaped `<task-notification>` close miss (CC 2.1.198)** — a background task completing
  while the parent is busy lands as a `queue-operation`/`enqueue` entry; the parser now synthesizes
  the close so typing drops at completion instead of stranding to the TTL.
- **`idle_prompt` false 🔔** — CC 2.1.204's ~60s post-turn idle nudge is dropped at the notification
  trust boundary (permission prompts and unknown kinds fail open), killing a spurious
  "🔔 Waiting on you" + typing-dark after every turn end.
- **Prose ↔ picker ordering** — long findings prose posted before an AskUserQuestion picker is now
  split at Telegram's 4096-char limit, so it appears before the card instead of failing silently.
- Numerous AUQ card-liveness, source-parity, and restart-recovery correctness fixes carried forward
  from the 0.2.x line.

### Notes
- The package version is bumped per release, not per deploy. Always deploy with
  `uv tool install --force --no-cache .`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- The approval, decision, and decision-dispatch cards are all off by default; enable them per the
  `CC_TELEGRAM_*` flags in the README once you have characterised your Claude Code version.

## [0.2.1] — 2026-06-24

### Fixed
- **AskUserQuestion descriptions card was suppressed for recommended options.** An AUQ whose
  recommended option label ended in the literal `(Recommended)` lost its `📋 AskUserQuestion —
  full details` message (the separate, multi-part-splittable message posted *before* the picker
  card). The pane parser strips `(Recommended)` into a structured flag, but the PreToolUse
  side-file label keeps it verbatim, so the pane-consistency predicate false-mismatched and the
  render resolver bailed (`bail_label_mismatch`) — dropping the descriptions for the *same*
  question (observed live on a busy topic; recurred on every AUQ whose recommended option carried
  the suffix). Fixed by normalizing the trailing recommended suffix on both sides of the
  side-file↔pane label compare (`auq_source._strip_recommended`, reusing the parser's
  `_RE_RECOMMENDED`); confined to the suffix only, so wrong-question protection and mint/validate
  parity are unchanged. The details-message and picker rendering are untouched. Peer-reviewed
  (Codex + Hermes, both PASS); RED-first tests added.

## [0.2.0] — 2026-06-24

The "busy-signal + AskUserQuestion bridge" release: ~190 commits since v0.1.0 making Telegram a
faithful mirror of what Claude Code is actually doing — interactive prompts, background work, and
run-state — plus a deployment-docs pass so another operator (or code agent) can stand the bot up
from scratch.

### Added
- **Cross-topic dashboard** (`/dashboard`) — one owner+chat-scoped overview message listing every
  bound topic grouped needs-attention-first (🔔 / 🟡 / ⚪), repainted by the status poller; `/dashboard pin` opt-in.
- **Per-user output verbosity** (`/settings`) — `verbose` / `standard` / `compact` / `quiet` presets
  plus per-knob overrides (tool-line length, done-card policy, sub-agent cards, 👤 echo, 📊 footer),
  persisted per user in `state.json`. Production default is `standard`.
- **"🔔 Waiting on you" detection** via a new matcher-less `Notification` hook + `notify_pending/`
  side files — covers permission/approval gates (including the Workflow tool's Bash-approval gate)
  that leave no JSONL trace, with a persistent, audible decision card.
- **Live prose before interactive prompts** via a bot-managed `MessageDisplay` hook
  (`md_hook_settings.json` + `msg_display/` capture) — explanatory prose written in the same turn as
  an `AskUserQuestion` / `ExitPlanMode` is delivered *before* the picker, not after resolution.
- **ExitPlanMode plan body before the picker card** (findings → 📋 Plan → card ordering).
- **Background-agent + Workflow run-state** — `run_in_background` Agents and the `Workflow` tool now
  light typing + 🟡 Busy while they work (GH #44 snapshot projection + the ISSUE-6 Workflow bracket),
  with `↳` sub-agent display cards that collapse on completion, and a startup reconciler that
  re-lights still-running background work across a restart.
- **Background-jobs decoration** (GH #43) — `⏳ N background job(s)` on collapsed done-cards + the
  dashboard glyph, parsed from the pane.
- **Docs / deploy ergonomics** — `docs/DEPLOYMENT.md` (end-to-end setup + the `--no-cache` upgrade
  recipe + troubleshooting), top-level `AGENTS.md`, and `bin/install-service.sh` to generate + load
  the `com.cc-telegram` LaunchAgent. Log-rotation LaunchAgent (`bin/install-log-rotate.sh`).
- **Post-turn digest collapse** — the activity card collapses to a one-line summary when the turn
  ends; per-sub-agent cards collapse the same way.

### Changed
- **`route_runtime` is now the sole run-state / context-usage / idle-clear authority** — the old
  `busy_indicator` and observer/callback fan-out (root cause of bug c313657) were removed in favor of
  a pull-only per-route state machine with immutable snapshots.
- **AskUserQuestion pick dispatch navigates the cursor to the target and presses Enter** (validated
  against Claude Code v2.1.168, where a bare digit no longer reliably selects), recording the ledger
  `dispatched` lock only after the pane confirms the expected advance. Restart-safe via an
  append-only action ledger + a durable mint-intent store.
- Interactive-surface teardown is now **parent-only (sidechain-gated)** — a background agent
  narrating no longer tears down the parent's live AUQ/EPM/Permission card.

### Fixed
- **Typing indicator stayed dark for the full 30-min TTL** while a background agent worked
  (parent idle) — `BG_RUNNING` now clears the projected-busy 🔔 on the agent's next heartbeat
  (scoped to the sole-live-plain-Agent shape for safety).
- **AUQ "📋 full details" ctx-card ~28× duplication** in a busy topic while a background Workflow ran.
- **AUQ picker-card churn / duplicate cards** on long-open cards in busy topics (pane↔pane drift
  no-op + transient-edit-keep).
- **Claude Code v2.1.170 interactive-UI detection drift** (EPM footer `ctrl-g`→`ctrl+g` + a new
  "Settings Warning" marker) that hid both the picker and the findings prose.
- Out-of-order JSONL tool pairing / stuck-route eligibility (GH #42).
- Numerous AUQ card-liveness, source-parity, and restart-recovery correctness fixes.

### Notes
- The package version is bumped per release, not per deploy. Always deploy with
  `uv tool install --force --no-cache .` (the wheel cache is version-keyed; without `--no-cache`,
  same-version redeploys reinstall a stale wheel). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## [0.1.0] — 2026-05-17

Initial tagged release: Telegram ↔ Claude Code bridge, topic-only architecture
(1 Topic = 1 tmux window = 1 Claude session), `SessionStart` hook session tracking,
per-route message queues, MarkdownV2 output, streaming tool/thinking/status, photos + voice,
reply context, and SQLite provenance.

[0.3.0]: https://github.com/etcircle/cc-telegram/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/etcircle/cc-telegram/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/etcircle/cc-telegram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/etcircle/cc-telegram/releases/tag/v0.1.0
