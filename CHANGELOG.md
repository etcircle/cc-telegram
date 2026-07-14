# Changelog

All notable changes to cc-telegram. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project's package version is bumped per release, not per deploy (see the `--no-cache` note in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

## [0.4.1] — 2026-07-14

The "your reply-quoted message actually sends" release. Three fixes, all found in live use of
0.4.0: a long or quoted message was typed into the terminal but never submitted (and then wedged
the whole topic), question cards carrying ASCII mockups rendered as garbage, and the bot posted
duplicate copies of its own messages whenever the network was slow.

### Fixed
- **A tall multi-line draft no longer false-refuses, and no longer wedges the topic (GH #56).**
  A reply-quoted message (~700 chars over ~18 rendered rows — under Claude Code's paste-collapse
  threshold, so it renders in full) pushed the input box's top border above the delivery gate's
  fixed 20-line scan window. The gate concluded the input box had vanished, withheld the Enter,
  armed the stranded-draft brake, and then refused the *next* message too. Two legitimate messages
  refused, one left sitting unsent in the pane — on the single most common way of replying.
  - The gate now scans **upward** for the box's top border when only one border is in view,
    authorized by a three-part structural proof that the border it found is really the box's
    bottom: a canonical status bar directly below it, no option-row-shaped line below it, and a
    prompt-glyph row directly under the located top border. The brake's release probe inherits the
    fix through the same seam.
  - **The status-bar recognizer took six review rounds and one approach change.** Each round, a
    different malformed row slipped through (fragment matching, empty segments, cross-products,
    repeated segments, two modes at once, mode + paste-hint, Unicode digits, non-breaking spaces,
    mismatched glyphs) — and every one of those would have let a **live question card be read as a
    ready input box**, i.e. the exact "type into a prompt and commit the highlighted option" hazard
    the 0.4.0 gate exists to prevent. Per-fragment validation was abandoned for a canonical ordered
    grammar in which malformed rows are *unrepresentable* rather than merely rejected.
  - **Soundness is not enough — completeness matters too.** An intermediate design whitelisted whole
    rows drawn from the test fixtures; it was safe, and it would have silently kept the bug alive on
    real machines, because live sessions render status bars (`… · ctrl+t to hide tasks · …`) that the
    fixtures never captured. The shipped grammar is pinned against live-sampled bars as well as the
    fixture corpus.
- **`/esc` can finally clear a stranded draft (GH #56).** On Claude Code 2.1.209 a **single Escape
  clears nothing** — not even a one-line draft — so the refusal message telling you to use `/esc`
  was wrong for *every* draft, not just tall ones. Two rapid Escapes are the only safe full clear
  (Ctrl+U kills just one line; Ctrl+C clears but a second press exits Claude to a bare shell, so the
  bridge never sends it). `/esc` on a braked window now performs that double-Escape — but only after
  proving the box actually holds text, and it sends **zero keystrokes** if a card or an unreadable
  frame is on the pane. Refusal copy corrected to match reality.
- **No more duplicate messages when the network is slow (GH #55).** The MarkdownV2→plain-text
  fallback caught *every* exception, including `TimedOut` — but a client-side timeout does not mean
  the request failed: Telegram usually delivered the formatted message anyway, so the "fallback"
  posted a second, plain copy. The fallback now fires only when the content provably did **not**
  reach Telegram (a `BadRequest` rejection, or a formatting error before the request left). Ambiguous
  transients log and stop.
  - Scoped to the four *send* paths. The edit lanes deliberately keep the broad fallback: an edit
    cannot mint a second message, and removing it would have pushed message recreation up into the
    callers.
  - Trade-off, accepted: a timeout whose request genuinely never arrived now loses that message
    (visible in the log; `/history` and the transcript remain the escape hatch) — better than routine
    duplicates under load.

### Added
- **Option previews in question cards (GH #54).** Claude Code ≥2.1.197 lets an `AskUserQuestion`
  option carry a `preview` — a multi-line ASCII mockup. These panes previously parsed as garbage:
  no details card, no option buttons. Now the mockups render as monospace blocks in the 📋 details
  message, posted before a short labels-only selection card, and the option buttons work — a tap
  navigates, verifies, and commits, including the wrapped-label case where Claude Code drops the `❯`
  cursor and marks the selection with styling alone. Multi-select previews are shown too (the
  terminal doesn't render them at all, so the details card is the only place they're visible).

## [0.4.0] — 2026-07-12

The "safe to type at a live prompt" release. Sending a message while Claude was waiting on a
question card, a plan approval, or a folder-trust dialog used to type your text into the terminal —
where the text was discarded and the trailing Enter **committed the highlighted option**. On a plan
approval that option is *"Yes, and bypass permissions"*, so a stray "ok thanks" could approve a plan
with permissions bypassed. That is now closed twice over: every payload must first prove Claude's
input box is actually there, and on a **question card** your message no longer bounces at all — it
becomes the answer, in your own words, by voice or by text.

### Added
- **Answer a question card in prose (GH #50 PR-2).** A voice note, a typed message, a caption, or a
  quoted reply now *answers* a live `AskUserQuestion` card instead of being refused. The bridge
  navigates to the card's own free-text row, types your words, and commits them. Quoted replies keep
  their quote.
  - **The guard is a landing proof, taken before a single byte is typed:** the row under the cursor
    must be the *dim* `Type something.` placeholder. A rig on Claude Code 2.1.207 established that
    dim holds for exactly one shape — the selected, untyped placeholder — and that a real option row
    is never dim, not even when highlighted. So the bridge cannot begin typing while parked on an
    option, and a mis-identified card cannot commit one. Verified against an overshoot onto a real
    option, an undershoot onto a real option, and the payload `"Yes, but use postgres"` against an
    option literally labelled `Yes`.
  - Card identity is the `PreToolUse` hook's per-invocation `tool_use_id` (mandatory — no id, no
    dispatch), re-read around every capture, with a fresh `session_map.json` generation read and
    structural option-label agreement.
  - **Accepted, disclosed residual:** a successor card with the same option labels, appearing in the
    window between the last look and Enter, can receive the prose meant for its predecessor. Your
    answer reaches a different question; you see it and correct it. It is never an option commit.
  - **Plan approvals are out of scope by decision** — an `ExitPlanMode` card falls through to the
    delivery gate and is refused, with an explanation.
- **README: the two things that actually matter, up front** — that this is in practice a
  bypass-permissions tool (and what that does to your security boundary), and that `/screenshot` is
  the always-available fallback whenever you cannot tell what the terminal is doing.

### Fixed
- **Messages are never typed into a live prompt (GH #50 PR-1).** `deliver_to_window` is now the one
  choke point every payload crosses — text, voice, captions, attachments, forwarded slash commands,
  the late-answer card, the pending-bind replay — and it refuses to write unless it has *positive
  structural proof* of Claude's ready input box. Positive proof, never "no known prompt matched":
  the `Switch model?` dialog is footer-less and the parser is blind to it, so absence-of-match is
  worthless. Every blocking prompt replaces the input box, which is why its presence is the one
  signal that holds for prompts nobody has seen yet.
  - A **stranded-draft brake** stops the follow-on failure: if a payload was typed but its Enter
    withheld, the next message would otherwise append to it and commit both. The window refuses
    further sends until the input box is observed empty (or the window dies).
  - Refusals carry the actual reason and actionable copy, exactly once, on every path.
- **Raw control bytes are refused before any keystroke (GH #50).** `tmux send-keys -l` stops tmux
  interpreting key *names* but passes escape bytes to the terminal verbatim, so a payload carrying
  `ESC [ A` could move the cursor and fire a hotkey before anything was verified. All C0 control
  characters except newline, plus DEL and C1, are now refused with an explanation rather than
  silently stripped. Ordinary line breaks still work, so voice notes and quoted replies are
  unaffected.
- **Long voice notes stopped stranding (GH #50 PR-1 regression).** Claude Code collapses a large
  pasted payload to `[Pasted text #1 +N lines]` **and replaces the status bar** with
  `paste again to expand`. The gate did not know that shape was still a ready input box, so every
  message past ~800 characters — a voice note carrying a reply quote, typically — was refused, left
  stranded in the input box, and braked the topic.
- **`/update` and `/cost` in any topic where a plan had been approved.** Pre-existing and silent:
  after a plan approval Claude Code pins the plan's slug into the input box's top rule, and the
  pure-dashes pattern stopped matching — so `/update` quietly deferred and `/cost` refused, in that
  topic, forever.
- **`/cost` and `/usage` refused for anyone running background agents.** They had inherited
  `/update`'s background-shells guard, which exists only because `/update` *restarts* the session.
  Reading a usage overlay restarts nothing, so a live `· N shell` token is not a hazard for it —
  but it made `/cost` refuse essentially every time for a heavy background-agent user.

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
