# cc-telegram

**Your real Claude Code, on your own machine, driven from Telegram.**

cc-telegram is not a hosted chatbot and not an API wrapper. It's a bridge to the actual `claude` CLI running in tmux on your computer — with your files, your tools, your skills, your MCP servers, your logged-in account. You talk to it from a Telegram forum. Each topic is one live Claude Code session in its own tmux window. The terminal stays the source of truth; Telegram is the remote control. Open a new topic to start a session. The forum *is* your session list — no web UI, no extra app, just topics.

Why it's worth running:

**Buttons that don't lie.** When Claude asks a question, proposes a plan, or hits a permission gate, you get real tappable buttons — not a screenshot you squint at. And a tap doesn't fire blind. The bot navigates the live terminal cursor to your choice, re-reads the pane to confirm it landed, *then* presses Enter. If the screen changed under you, the tap bails instead of pressing the wrong thing.

**Honest presence.** The "typing…" indicator means the machine is actually working — foreground turns, background agents, workflows, and shells all count. When only background work is running and the foreground is quiet, a **⏳ Background work running** line explains the silence instead of leaving you to guess. When Claude is blocked waiting on *you*, a **🔔** card says so — once, out loud.

**Files come to you.** Mention a file in prose — `report.md`, `chart.png` — and it turns into a 📎 tap-to-download button. `/file <path>` fetches any file type, scoped to the session's folders and a size cap. Images a tool produces arrive as real photos, and `/screenshot` grabs the terminal itself as an image whenever you want the raw pane.

**Talk to it like a person.** Send a voice note and it's transcribed into a prompt. Send a photo or a document and it's downloaded and handed to Claude. Reply to a message to quote it back with role-aware context.

**The rest just works.** Unknown slash commands forward straight into Claude Code, so `/clear`, `/compact`, `/model`, and `/cost` all work from your phone. `/dashboard` puts a live overview of every session in one pinned message. `/update` upgrades the CLI and restarts your idle sessions in place — without dropping a single topic binding.

**It survives things.** Bot restart, tmux restart, phone in a tunnel — sessions live in tmux, state lives on disk, and the bridge reconciles on startup. Duplicate taps are caught by an append-only action ledger, so a double-press after a restart answers "already received" instead of doing the thing twice. 2,800+ tests keep it honest.

One caveat up front: this is a single-operator tool. You lock it to your own Telegram user id and point it at your own machine. It is not multi-tenant and doesn't pretend to be.

## Features

The pitch above covers the headline behavior. Also in the box:

- **One topic = one window = one session.** All routing is keyed by tmux window id, so `/clear` and resumed sessions stay attached to the right topic. The same directory can back several topics.
- **Streaming output.** Assistant text, thinking, tool use/result summaries, interactive prompts, and local command output all flow into the topic as they happen.
- **Per-route queues.** Every `(user, topic, window)` has its own worker, so one noisy topic never stalls another.
- **Collapsing activity digest.** A compact digest shows tool activity, context-window %, and busy/waiting state while a turn runs, then collapses to a one-line summary (`✅ Done — repo · 14 tools · 2 sub-agents · 3m 41s`) when it finishes. `/history` keeps the full log.
- **Live prose before prompts.** When Claude writes an explanation in the same turn as a question, Claude Code buffers the whole turn until you answer — so you'd normally choose blind. A lightweight `MessageDisplay` hook captures that prose live and the bot delivers it *ahead* of the picker.
- **Late answers after the 60s AFK auto-resolve.** On Claude Code ≥2.1.198 an unanswered question self-resolves after ~60s. Instead of losing it, the card converts to "⏰ Claude proceeded after ~60s (no response)." whose buttons send your pick as a plain course-correction message.
- **Per-user output verbosity.** `/settings` gives you presets (`verbose` / `standard` / `compact` / `quiet`) plus knobs, stored per user and applied to everything the bot sends *to you*. Another allowed user tapping your panel changes nothing.
- **📎 downloads have a strict trust boundary.** Auto-offers come from Claude's own prose only — never tool output, sub-agent narration, or web URLs. Any deliverable file type is offered (documents, images, audio, video, archives, office/data formats — source-code extensions stay excluded so incidental `.py`/`.ts` paths never mint a card). A file is only offered if it actually resolves under the session's working directory (or `CC_TELEGRAM_ARTIFACT_ROOTS`) within the size cap, buttons answer only to you (owner-checked taps), and `/settings` has a 📎 Files toggle to turn the cards off. `/file` applies the same folder/size validation to any file type.
- **Reply context, voice, photos, documents.** Telegram replies inject fenced, role-aware quote context. Voice notes are transcribed (OpenAI-compatible). Photos go in as image blocks; documents up to 20 MB are downloaded and forwarded.
- **Reactive broken-topic fallback.** If Telegram says a topic is gone/closed/forbidden, output falls back to DM rather than vanishing.
- **Opt-in approval-gate and decision cards.** Behind `CC_TELEGRAM_PERMISSION_PROMPTS` / `CC_TELEGRAM_DECISION_CARDS`, permission prompts and generic confirmation prompts surface as cards (display-only by default; `CC_TELEGRAM_DECISION_DISPATCH` adds verified one-tap buttons for known-good prompt families on characterized CC versions). Off by default; a flag-off deploy changes nothing.

## Commands

Bot-owned commands (handled by cc-telegram, never forwarded):

| Command | What it does |
|---|---|
| `/start` | Greeting and how to begin — open a topic to start a session. |
| `/history` | Paginated message history for this topic's session. |
| `/screenshot` | Capture the tmux pane as a PNG. |
| `/esc` | Send Escape to interrupt Claude. |
| `/usage` | Pull Claude Code's usage/limits from the TUI overlay. |
| `/cost` | Same as `/usage` (alias) — pull cost/usage/limits from the TUI overlay. |
| `/update` | Update the CLI, then restart idle sessions in place (owner-only). |
| `/dashboard` | Claim this topic as a cross-session overview; `/dashboard pin` pins it. |
| `/settings` | Your personal output-verbosity preferences (presets + knobs). |
| `/file <path>` | Upload a file from the session's directory to the topic (any type, spaces OK). |
| `/unbind` | Detach the topic from its session; the tmux window keeps running. |
| `/kill` | Kill the topic's tmux window; the topic stays open for a new session. |

Any other slash command is forwarded straight into Claude Code — so `/clear`, `/compact`, `/model`, and `/effort` work from Telegram. `/help` and `/memory` open full-screen interactive panels inside Claude Code that can't render over Telegram and would freeze the topic, so the bot blocks them with a note instead of forwarding (use `/screenshot` to view the terminal).

## Install

### The easy way: let Claude set it up

Already have Claude Code? Clone the repo, open it in Claude Code, and say:

> read the README and set cc-telegram up for me

It walks you through the BotFather token, writes your `.env`, installs the hooks, and offers to run it as a background service — asking you only for the parts nobody else can supply (your token, your Telegram user id). The manual steps are right below if you'd rather do it yourself.

### The manual way

**You'll need:**

- macOS or Linux. On Windows the bot runs under WSL2 — see **[docs/windows-wsl.md](docs/windows-wsl.md)**.
- Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and `tmux`.
- The Claude Code CLI (`claude`) on `PATH` and **already logged in** — run `claude` once interactively. cc-telegram manages no Anthropic credentials; it only drives the binary, so an unauthenticated CLI just fails opaquely inside the topic.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A Telegram supergroup with **Topics** turned on, with your bot added.

**Steps:**

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv tool install --force --no-cache .   # puts `cc-telegram` on PATH at ~/.local/bin
```

`--no-cache` is not optional. uv's wheel cache is keyed on the package version, and the version isn't bumped on every deploy — so `uv tool install --force .` alone silently reinstalls a stale cached wheel (exits 0, your code never ships).

Write `~/.cc-telegram/.env` with the two required values:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

Install the Claude Code hooks, sanity-check, and run:

```bash
cc-telegram hook --install    # SessionStart / PreToolUse / Notification hooks
cc-telegram doctor            # checks token/users/tmux/claude/SessionStart hook/config-dir
cc-telegram                   # foreground
```

Then message your bot in a topic — the first message opens a directory picker to bind the topic to a session.

For day-to-day use run it under launchd on macOS with `bash bin/install-service.sh` (below). The full end-to-end deploy, upgrade, and troubleshooting guide is in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. New code agent? Start at **[AGENTS.md](AGENTS.md)**.

**Run from source instead (development):**

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv sync --all-extras          # dev .venv; does NOT put `cc-telegram` on PATH
```

Then prefix everything with `uv run` (e.g. `uv run cc-telegram doctor`, `uv run cc-telegram`).

---

# Reference

Everything below is operator detail: every environment variable, hook, and state file the bot reads or writes.

## Configure

The two required values live in `~/.cc-telegram/.env` (see Install). Everything else has a default.

Core variables:

- `TELEGRAM_BOT_TOKEN` — required; from BotFather.
- `ALLOWED_USERS` — required; comma-separated Telegram user IDs.
- `CC_TELEGRAM_DIR` — config/state directory; default `~/.cc-telegram`.
- `TMUX_SESSION_NAME` — tmux session driven by the bot; default `cc-telegram`.
- `CLAUDE_COMMAND` — command used for new windows; default `claude`. Must exec the claude binary directly (or via an exec-ing wrapper); a non-exec shell wrapper defeats `/update`'s shell-detection gate (see the `/update` notes below).
- `CLAUDE_CONFIG_DIR` — Claude config root; projects default to `$CLAUDE_CONFIG_DIR/projects`.
- `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` — explicit Claude projects directory override. Precedence: `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` > `CLAUDE_CONFIG_DIR/projects` > `~/.claude/projects`.
- `MONITOR_POLL_INTERVAL` — JSONL poll interval; default `2.0`.
- `CC_TELEGRAM_BROWSE_ROOT` — directory picker root; default `~`.
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` — optional voice transcription provider.

Behavior knobs:

- `CC_TELEGRAM_VERBOSITY` — default output preset (`verbose` / `standard` / `compact` / `quiet`) for users who haven't picked one via `/settings`; default `standard` (collapsed post-turn digests, 160-char tool lines, user echo off — `verbose` restores the pre-settings firehose). Per-user `/settings` choices always win over env defaults — the env vars below are knob-precise **defaults, not ceilings**.
- `CC_TELEGRAM_SHOW_USER_MESSAGES` — echo user messages from tmux; default `true`. When set explicitly it becomes the default for the per-user 👤-echo preference; a user's stored `/settings` choice overrides it.
- `CC_TELEGRAM_SHOW_TOOL_CALLS` — show tool use/result stream; default `true`. Setting it to `false` suppresses **display only** (all tool surfaces including the 🤖 sub-agent dispatch/report and the per-sub-agent cards): sidechain transcripts are still tailed and still feed the run-state truth (busy indicator / typing), so a long subagent run doesn't read as idle. A user's stored `/settings` choice overrides it.
- `CC_TELEGRAM_SHOW_HIDDEN_DIRS` — show dot-directories in picker; default `false`.
- `CC_TELEGRAM_HOOK_TIMEOUT` — seconds to wait for Claude Code's `SessionStart` hook to register a newly-bound window before giving up; overrides **both** built-in defaults (5s fresh / 15s resume) when set. Raise it when Claude starts slowly — a WSL `/mnt/c` DrvFs mount, or several MCP servers, can push `SessionStart` out to ~15-20s, past which the first message is silently dropped on every bind. Unset preserves the stock 5s/15s; an invalid value (non-numeric / non-finite / `<= 0`) falls back to the defaults with a warning.
- `CC_TELEGRAM_WINDOW_GEOMETRY` — `<width>x<height>` geometry for bot-created tmux windows (applied at window creation, before Claude launches, and to every existing window once at startup); default `160x50`. The terminals are a machine surface — nobody attaches to them — so the geometry serves the parser: 50 rows keep tall `AskUserQuestion` pickers fully on-screen, 160 columns keep long option labels from overflowing. Sanity bounds `20–500` × `5–300`; an invalid value falls back to the default with a warning.
- `CC_TELEGRAM_PERMISSION_PROMPTS` — surface tool-permission prompts and the Workflow dynamic-workflow-launch approval gate as Telegram cards (answerable via the manual ↑/↓/⏎/Esc nav keyboard); default `false` (OFF). Display-only in this release — no one-tap option button, and the card labels its controls as un-verified live-terminal keystrokes. Truthy values: `1` / `true` / `yes` / `on`.
- `CC_TELEGRAM_DECISION_CARDS` — surface generic titled numbered-option confirmation prompts that no built-in interactive pattern covers (the "Switch model?" confirmation, the folder-trust prompt, and peers) as a display-only Telegram card with the same manual ↑/↓/⏎/Esc nav keyboard; default `false` (OFF). Independent of `CC_TELEGRAM_PERMISSION_PROMPTS`. Last-priority + strict-or-None: it never shadows a named pattern (AskUserQuestion / ExitPlanMode / Settings / RestoreCheckpoint / Permission / Workflow) and never re-surfaces a permission/workflow gate that its own flag left off. Display-only — no one-tap option button. A flag-OFF deploy detects and changes nothing. Truthy values: `1` / `true` / `yes` / `on`.
- `CC_TELEGRAM_DECISION_DISPATCH` — Stage B2 tappable Decision dispatch; default `false` (OFF). When ON **and** `CC_TELEGRAM_DECISION_CARDS` is ON, a Decision card whose prompt matches a known-good `(family × running-CC-version)` pair in `handlers/decision_token.py`'s dispatch table (currently the folder-trust prompt) also mints one-tap `dcp:` option buttons that navigate→verify→Enter the live pane (the AUQ v2.1.168 dispatch discipline through a parallel Decision-specific lane; body-inclusive `decision_prompt_fingerprint` identity + a FRESH per-tap version-license re-read). Unknown family / un-characterized CC version / busy pane → display-only. A flag-OFF deploy mints no buttons and the `dcp:` callback declines. Truthy values: `1` / `true` / `yes` / `on`.
- `CC_TELEGRAM_ARTIFACT_MAX_MB` — max size of a file the 📎 card / `/file` will upload; default `45` (Telegram's bot upload hard cap is 50 MB). A file over the cap is never offered (and `/file` states the cap).
- `CC_TELEGRAM_ARTIFACT_ROOTS` — comma-separated **absolute** extra roots the 📎 card / `/file` may serve files from, beyond the session's working directory (e.g. a shared scratchpad dir); default empty. **Absolute paths only** — a relative entry is ignored with a warning (never resolved against the bot's launch cwd). Files with secrets-adjacent extensions (`.json` / `.log` / `.txt`) can be offered when Claude names them in prose; a tap uploads them to the topic, so keep the roots scoped.
- `CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS` — max input shown in `**Tool**(...)`; default `40`.
- `CC_TELEGRAM_AGENT_PROMPT_PREVIEW_CHARS` — subagent dispatch excerpt; default `400`.
- `CC_TELEGRAM_REPLY_CONTEXT` — inject reply/quote context; default `true`.
- `CC_TELEGRAM_REPLY_CROSS_SESSION` — when `true` (default), a reply quoting a message from a previous Claude session is rendered with an annotated cross-session marker rather than silently dropped; set `false` to revert to the older silent-drop behavior.
- `CC_TELEGRAM_QUOTE_INJECTION_MAX_CHARS` — max quoted text injected into Claude; default `1600`.
- `CC_TELEGRAM_AGGREGATOR_DEBOUNCE_SECONDS` — media/caption coalescing window; default `1.5`.
- `CC_TELEGRAM_AGGREGATOR_MAX_ATTACHMENTS` — per-bundle attachment cap; default `10`.
- `CC_TELEGRAM_MAX_ATTACHMENT_SIZE_BYTES` — document download cap; default `20971520`.
- `CC_TELEGRAM_CONTEXT_PCT_THRESHOLD` — context-% digest threshold; default `80`.
- `CC_TELEGRAM_CONTEXT_IN_MESSAGE_FOOTER` — per-turn token footer; default `true`.
- `CC_TELEGRAM_MESSAGE_REFS_RETENTION_DAYS` — provenance retention; default `30`.
- `CC_TELEGRAM_MESSAGE_REFS_DB_PATH` — SQLite path; default `$CC_TELEGRAM_DIR/message_refs.db`.
- `CC_TELEGRAM_MESSAGE_REF_TEXT_MAX_CHARS` — stored body cap; default `4000`.

### Recommended daily-driver `.env`

Only use this if the bot runs on a machine you trust and `ALLOWED_USERS` is locked to you. `--dangerously-skip-permissions` lets Claude act without local confirmation.

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=<your_id>
CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
MONITOR_POLL_INTERVAL=1.0
OPENAI_API_KEY=sk-...
CC_TELEGRAM_BROWSE_ROOT=~/dev
# CC_TELEGRAM_SHOW_TOOL_CALLS=false
# CC_TELEGRAM_SHOW_USER_MESSAGES=false
```

### Config directory override

Default config dir is `~/.cc-telegram`. Override it with `CC_TELEGRAM_DIR`:

```bash
CC_TELEGRAM_DIR=/path/to/state cc-telegram
```

Useful for testing or running multiple profiles against one install.

## The `/update` command in detail

`/update` updates the Claude Code CLI binary, then restarts each **idle** bound session inside its existing tmux window (via `claude --resume`, so it adopts the new version and the topic keeps its window id / routing). It is owner-only, fail-closed, and idle-only:

- Busy, waiting, or background-agent sessions are **deferred**, never interrupted — as are sessions with live background shells (a `· N shell` status-bar token; a restart would kill those jobs).
- A session that won't cleanly exit within a bounded ~15s wait is **skipped**, not force-killed. The summary then warns that the session may be dead and to check the window before sending messages, since `/exit` was already sent.
- A skipped window is **quarantined**: the bot refuses to type new messages into it until it observes Claude running there again (a message typed into a dead session's bare shell would be *executed* by the shell), replying with an explicit "Message NOT delivered" error in the topic instead. "Claude running" is strict — the pane must report Claude's own version-string process; any other foreground command, including vim/python/ssh you started while checking the window, keeps sends blocked. The quarantine clears once Claude is seen alive, on a later confirmed restart, or when the window/topic is torn down; it is in-memory, so a bot restart clears it.
- Restarts run one at a time; a second `/update` while one is running is rejected. There's no scheduler — run it when you want the running sessions on a freshly-updated CLI. It reports a progressive summary (`♻️ Restarted N idle · deferred M busy · skipped K`).

Note: `CLAUDE_COMMAND` must exec the claude binary directly (or via an exec-ing wrapper). A non-exec shell wrapper makes the pane report the wrapper shell while Claude is still alive, which defeats `/update`'s shell-detection safety gate in the dangerous direction.

## State files

Under `$CC_TELEGRAM_DIR` (default `~/.cc-telegram/`):

- `state.json` — thread bindings, window states, display names, read offsets, the `dashboards` map (`"<chat_id>:<owner_user_id>" → {thread_id, msg_id, pinned}` — the `/dashboard` host record, one per chat+owner; cleared when its host topic closes or breaks), and the `user_settings` map (`"<user_id>" → {verbosity, knob overrides}` — per-user `/settings` output-verbosity choices; lost if an **older** binary rewrites state.json, which is accepted: they are re-settable preferences).
- `session_map.json` — hook-generated `window_id → session` mapping (written by the `SessionStart` hook).
- `monitor_state.json` — JSONL byte offsets per tracked session (incremental-read progress).
- `interactive_state.json` — persisted picker message ids + AUQ context markers (survives bot restart so a `launchctl kickstart` doesn't lose interactive state).
- `auq_pending/<session_id>.json` — `PreToolUse` side files (one per active AUQ; mode `0600` under directory mode `0700`). Multi-select `aqt:` toggles keep the side file alive; it is cleaned when the AUQ `tool_result` runs `forget_ask_tool_input`, on session replacement, or by startup GC.
- `notify_pending/<session_id>.json` — `Notification` hook side files (mode `0600` under directory mode `0700`): a window-keyed `{ts, window_key, generation, kind}` marker — **no notification message text is stored**. The poller reads it (rejecting any record whose `window_key` doesn't match the asking window), promotes the route to "🔔 Waiting on you", and unlinks it generation-guarded. While set, the route also posts a persistent, audible "🔔 Claude needs a decision" card so an approval/permission wait survives the run's own streaming output (it is no longer buried within ~5s). Cleared by: a user transcript event (unconditionally); a tool_result / end-of-turn / task-notification event timestamped strictly newer than the notification (**plain assistant text/thinking narration does NOT clear it** — a workflow narrates *while* blocked, so the wait must survive its own streaming text); the pane observed running sufficiently after the notification fired (the user approved in the terminal); a 30-minute runtime TTL; session replacement, `/clear`, or topic close; or 24h startup GC. The decision card is dismissed on the same resolutions.
- `auq_action_ledger.jsonl` — restart-safe write-ahead ledger for AUQ option-pick dispatches (mode `0600`; append-only JSONL; latest line per `(route_hash, fp8, opt)` key wins; the callback handler consults this to detect duplicate taps after a process restart so the same pick is never committed twice). States: `accepted → dispatched` (confirmed advance), `not_advanced` (pre-commit bail — a re-tap falls through), `commit_unconfirmed` (Enter sent, advance unconfirmed — refresh-only), and `released` (the AUQ resolved — appended for the window's rows only on a tool_result-confirmed resolution, at the AUQ tool_result branch in the bot's message handler and by the startup reconciler's positive-proof block, so a re-asked identical question is dispatchable again; generic teardown such as `/clear` or session replacement never releases). 24h retention is enforced on read (load + lookup); the file is rewritten only by over-cap compaction.
- `pick_intent.jsonl` — D2 restart-recovery: durable per-callback-**token** AUQ pick mint-intent store (mode `0600`; append-only JSONL row + tombstone lines; 24h retention + compaction). Written at the fresh single-select / review-Submit (`aqp:`) render; after a bot restart wipes the in-memory pick tokens, the callback handler reads it to RECOVER and re-dispatch the first token-less tap on a still-open card (row-scoped single-use; owner + stale-window auth; read-TTL-free source parity). Deliberately **not** the `(route_hash, fp8, opt)`-keyed action ledger above — writing recovery state there would clobber a `dispatched` row and re-open double-dispatch. Tombed on AUQ/EPM resolution, `/clear`, and topic close.
- `md_hook_settings.json` — bot-managed Claude Code settings file registering the `MessageDisplay` hook. Passed to bot-launched sessions via `claude --settings`, so the live-prose hook is scoped to the bot's own windows (it is never written into the global `~/.claude/settings.json`). Re-written on startup and on each window launch if its content drifts.
- `msg_display/<session_id>.ndjson` — `MessageDisplay` live-prose capture (one file per session keyed by the transcript filename, so it is resume-safe; mode `0600` under directory mode `0700`). The hook appends each streaming `delta`; the bot accumulates them into completed prose, posts it before the picker card, and (in the same file) records shown-live markers used to dedup the post-resolution copy. Removed on prompt resolution / session replacement / `/clear` / topic close, with a 1h startup GC backstop.
- `images/` and `files/` — downloaded photo / document attachments forwarded to Claude (directory mode `0700`, downloads `0600` — uploads can carry sensitive content; the dirs are create-and-repaired to `0700` at startup so an older install's loose `0755` is tightened; a failed chmod logs a warning and never fails the download).
- `message_refs.db` — SQLite provenance index for safer reply-context resolution (path overridable via `CC_TELEGRAM_MESSAGE_REFS_DB_PATH`).
- `log-archive/` — gzipped log rotations (only present if the rotation LaunchAgent is installed; see "Log rotation").

All state files are safe to delete — the bot re-creates what it needs on next start (you'll lose interactive picker continuity and bound topic mappings).

## The Claude Code hooks

```bash
cc-telegram hook --install
```

This writes/updates `~/.claude/settings.json` with three managed hook entries:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 5 }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion",
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 2 }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 2 }
        ]
      }
    ]
  }
}
```

The `SessionStart` hook writes `session_map.json` so the bot can route messages back to the right tmux window. The `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured question payload before Claude renders the picker — see below. The `Notification` hook (matcher-less) writes a window-keyed `notify_pending/<session_id>.json` marker when Claude blocks on a permission / approval prompt, so the bot can flip the topic to "🔔 Waiting on you" — the only detection path for approval gates that never reach the session JSONL. No notification text is stored in the marker. If either the `PreToolUse` or the `Notification` entry is missing, the bot logs a one-time startup warning; re-run `cc-telegram hook --install` to repair.

> **`cc-telegram doctor` only verifies the `SessionStart` hook.** Confirm all three managed entries installed with `grep -c 'cc-telegram hook' ~/.claude/settings.json` (expect `3`); a missing `PreToolUse`/`Notification` also surfaces as the one-time startup-log warning above.

### AskUserQuestion (AUQ) descriptions

When Claude Code calls `AskUserQuestion`, the option descriptions are not visible in the terminal pane until the user picks an option (Claude Code buffers `tool_use` until `tool_result`). The PreToolUse hook captures the structured `tool_input` and writes it to:

```
<CC_TELEGRAM_DIR>/auq_pending/<session_id>.json   (mode 0600; directory mode 0700)
```

The bot reads the side file at picker render time so the Telegram context message shows each option's full description right away, not after-the-fact. Multi-select AUQs render selected/unchecked/off-screen state and use `aqt:` callbacks to send a bare digit to tmux for each toggle; those toggles are reversible and not written to the AUQ ledger. The user then presses Tab to Claude Code's review screen, where Submit/Cancel uses the existing `aqp:` pick path and restart-safe ledger.

The single-select `aqp:` pick and the review-screen Submit/Cancel **navigate the live cursor to the tapped option with arrow keys and then press Enter** — the version-stable commit — and record the ledger `dispatched` lock only after re-parsing the pane confirms the form made the exact expected advance. On Claude Code v2.1.168 a bare digit no longer reliably selects (in the notes side-panel picker variant it only moves the cursor), so dispatch decouples from the digit entirely; arrows are pure navigation in every variant and `Enter to select` is in every picker's footer. A keystroke that is sent but whose advance can't be confirmed is recorded `commit_unconfirmed` (refresh-only, never auto-re-sent), and a pre-commit bail (cursor not found / send failed / cursor didn't land on the target) is `not_advanced` (retryable) — so a tap never over-advances and never falsely locks with "Action already received". The multi-select `aqt:` toggle still sends a bare digit (that path is unchanged for now). (Validated against Claude Code v2.1.168 terminal behavior.)

Side files are:

- Auto-created on each AUQ; the directory and files are mode `0700`/`0600`.
- Preserved across multi-select `aqt:` toggles and final Submit keypresses.
- Cleaned up when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when a session is replaced, or by startup GC.
- Garbage-collected on bot startup (any stale entries older than the TTL).
- Safe to delete the directory at any time; it is re-created on the next AUQ.

If the PreToolUse hook entry is missing from `~/.claude/settings.json`, the bot logs a one-time startup warning and falls back to pane-only descriptions. Re-run `cc-telegram hook --install` to repair.

### Live prose before AskUserQuestion / ExitPlanMode (MessageDisplay hook)

`cc-telegram hook --install` manages the three global hook entries above — `SessionStart`, `PreToolUse`, and `Notification`. A fourth hook — Claude Code's `MessageDisplay` event — is managed **automatically by the bot** and needs no manual install. It is **not** written into the global `~/.claude/settings.json`; instead the bot writes a small settings file and passes it only to the sessions it launches:

```
<CC_TELEGRAM_DIR>/md_hook_settings.json    → claude --settings <that file>
```

So the hook fires only for the bot's own windows (it merges with the global `SessionStart` / `PreToolUse` / `Notification` hooks). The hook itself is a tiny stdlib-only appender (run directly by the Python interpreter, never importing the package) so it stays well under the streaming-display latency budget. It appends each streaming `delta` of an assistant message to:

```
<CC_TELEGRAM_DIR>/msg_display/<session_id>.ndjson   (mode 0600; directory mode 0700)
```

When Claude writes prose in the same turn as an `AskUserQuestion` / `ExitPlanMode`, Claude Code buffers the whole turn in the session JSONL until the prompt resolves — so the explanatory prose would otherwise reach Telegram only after the user already chose. The bot accumulates the captured `delta`s into the completed prose and posts it before the picker card, then dedups the post-resolution JSONL copy so the prose appears exactly once. Capture files are removed on prompt resolution / session replacement / `/clear` / topic close, with a 1h startup GC backstop; the directory is safe to delete at any time.

If the bot cannot write the settings file (e.g. an unwritable config dir), it logs a one-time startup warning and live prose silently falls back to post-resolution delivery — no crash, the picker still works.

## Voice transcription

Voice notes are transcribed via a standard OpenAI `POST $OPENAI_BASE_URL/audio/transcriptions` call with `Authorization: Bearer $OPENAI_API_KEY`. The transcription model is **hardcoded to `gpt-4o-transcribe`** (`transcribe.py`; no override env var), so the backend must expose that exact model name. `OPENAI_API_KEY` is required **for voice only** — without it, a voice note gets a polite "transcription needs an OpenAI API key" reply and no HTTP call is made. Point `OPENAI_BASE_URL` at anything that speaks that shape:

- `https://api.openai.com/v1` — the default.
- A local LiteLLM, vLLM, or other OpenAI-compatible gateway that serves `gpt-4o-transcribe`.
- A backend exposing only a different STT model (e.g. OpenRouter's `whisper-1`) will return a model-not-found error unless fronted by a model-name-translating proxy.

If your backend doesn't natively speak OpenAI's STT shape (e.g., a local `whisper.cpp` server with its `/inference` endpoint), or serves a different model name, front it with a small shape-translating proxy and point `OPENAI_BASE_URL` at that. (An external `whisper-openai-proxy` example — a ~130-line stdlib-only shim — is an optional companion; it is not part of this repo.)

## Run under launchd (macOS)

**No main-bot plist ships in the repo.** Generate and load the LaunchAgent (label `com.cc-telegram`) with the bundled installer:

```bash
bash bin/install-service.sh          # writes ~/Library/LaunchAgents/com.cc-telegram.plist, then bootstrap + enable
bash bin/install-service.sh --print  # dry-run: print the plist it would write (still needs cc-telegram on PATH)
```

`cc-telegram` must already be on PATH (install as a tool, above). The script sets an explicit `PATH` in the plist so launchd can find `cc-telegram`/`tmux`/`claude`, enables `KeepAlive`+`RunAtLoad`, and redirects stdout/stderr to `$CC_TELEGRAM_DIR/launchd.{out,err}.log`. Hand-written-plist instructions and the full rationale are in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** section 7.

Once the LaunchAgent exists, restart (kill + relaunch) the bot with:

```bash
launchctl kickstart -k gui/$(id -u)/com.cc-telegram
```

## Log rotation

`launchd.err.log` and `launchd.out.log` are written by launchd's stderr/stdout redirect, not by Python's logging — so the bot can't rotate them itself. A small LaunchAgent handles rotation: every 30 minutes it checks both files, gzips a dated copy into `~/.cc-telegram/log-archive/` if either exceeds 50MB, and truncates the original in place (safe under the bot's `O_APPEND` write). Archives older than 14 days are deleted automatically. Install with:

```bash
bash bin/install-log-rotate.sh
```

The script is idempotent — re-running replaces the existing agent. Override thresholds via env in the plist `EnvironmentVariables` block (`CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB`, `CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS`).

Force a rotation pass now:

```bash
launchctl kickstart gui/$(id -u)/com.cc-telegram.log-rotate
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.cc-telegram.log-rotate
rm ~/Library/LaunchAgents/com.cc-telegram.log-rotate.plist
```

Without this, a crash-loop (e.g. a startup AttributeError under `KeepAlive=true`) can balloon `launchd.err.log` to hundreds of megabytes and trigger Telegram `getUpdates` rate-limiting via the restart spam. The rotation cap also caps the blast radius.

## Test

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
uv run pytest -m scenario -q          # behavior floor (tests/scenarios/)
bin/post-wave-check.sh                # repo health diff (LoC + brittleness signals)
```

`tests/scenarios/` holds the black-box behavior floor: each file drives a single user-visible scenario through the real handler stack (no monkeypatch of handler internals in test bodies). See `tests/scenarios/README.md` for the scenario → behavior map.

## Repository layout

```text
src/cctelegram/                     core package
src/cctelegram/handlers/            Telegram interaction layer
  attention.py                      end-of-turn attention cards
  inbound_aggregator.py             caption/media/photo+text bundler
  reply_context.py                  Telegram reply/quote → Claude context
  message_queue.py                  per-route FIFO worker
  message_sender.py                 safe send/edit/delete with MarkdownV2 fallback
  output_prefs.py                   per-user verbosity resolution (preset/env/override layering)
  artifacts.py                      📎 file-path detection + validated-fd upload leaf (/file + tap-to-download cards)
  status_polling.py                 poll loop + typing-action loop
  interactive_ui.py                 AskUserQuestion / ExitPlanMode / permission UI
  notify_source.py                  Notification-hook side-file trust boundary (waiting-on-you)
  dashboard.py                      /dashboard cross-topic overview message
  updater.py                        /update CLI-update + idle in-place session restart
  directory_browser.py              directory + session picker
  history.py                        /history paginator
  cleanup.py                        centralized topic teardown
src/cctelegram/message_refs.py            SQLite provenance table
src/cctelegram/session_monitor.py         JSONL tail + TranscriptEvent dispatch
src/cctelegram/transcript_parser.py       JSONL → ParsedEntry / TranscriptEvent
src/cctelegram/route_runtime.py           per-route run-state / context-usage / idle-clear authority
src/cctelegram/transcript_event_adapter.py  TranscriptEvent → route_runtime adapter
src/cctelegram/md_capture.py              MessageDisplay live-prose reader/accumulator + capture-settings/teardown
src/cctelegram/_md_display_appender.py    tiny stdlib MessageDisplay hook (appends deltas; never imports the package)
src/cctelegram/rate_limiter.py            TypingAwareRateLimiter (exempts sendChatAction from the per-group bucket)
tests/                              pytest suite
tests/scenarios/                    black-box behavior floor (@pytest.mark.scenario)
bin/post-wave-check.sh              repo-health diff for the architecture campaign
bin/install-service.sh              generate + load the com.cc-telegram LaunchAgent (macOS)
bin/install-log-rotate.sh           install the log-rotation LaunchAgent
.claude/rules/                      architecture notes loaded by Claude Code
docs/DEPLOYMENT.md                  end-to-end deploy + upgrade + troubleshooting guide
docs/windows-wsl.md                 Windows/WSL2 setup + sharing Claude Code config across WSL and native Windows
AGENTS.md                           top-level orientation for code agents
CLAUDE.md                           build/test commands + core design constraints
```

## License

MIT — see [LICENSE](LICENSE).
