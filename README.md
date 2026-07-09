# cc-telegram

A Telegram ↔ Claude Code bridge for running Claude sessions from Telegram forum topics.

Each Telegram topic maps to one tmux window running one Claude Code process. The terminal remains the source of truth, and Telegram becomes the remote control / notification layer.

## What it does

- **Topic-based sessions** — one Telegram topic = one tmux window = one Claude session.
- **Hook-based session tracking** — Claude Code `SessionStart` writes `session_map.json`, so `/clear` and resumed sessions stay attached to the right topic.
- **AskUserQuestion descriptions and multi-select toggles** — a `PreToolUse` hook captures the structured `AskUserQuestion` payload before Claude renders the picker, so each option's full description shows in Telegram right away. Single-select options submit through the restart-safe `aqp:` pick flow; multi-select options toggle with non-ledgered `aqt:` bare-digit callbacks, then final Submit/Cancel reuses the review-screen `aqp:` flow. A single-select pick / review Submit **navigates the live cursor to the tapped option with arrow keys, then presses Enter**, and only records the dispatch once the pane confirms the expected advance — version-stable on Claude Code v2.1.168, where a bare digit no longer reliably selects. (The `aqt:` multi-select toggle still uses a bare digit.)
- **Live prose before interactive prompts** — when Claude writes explanatory prose in the same turn as an `AskUserQuestion` / `ExitPlanMode`, Claude Code buffers the whole turn in the session JSONL until the prompt resolves, so without help the Telegram user would see only the picker and choose blind. A lightweight `MessageDisplay` hook captures that prose live (before the picker blocks) so the bot can deliver it ahead of the picker card.
- **Late answers after the ~60s AFK auto-resolve** — on Claude Code ≥2.1.198 an unanswered `AskUserQuestion` self-resolves after ~60 seconds and Claude proceeds on its own judgment. Instead of silently losing the question, the picker card converts to an honest "⏰ Claude proceeded after ~60s (no response)." card whose buttons send your choice as a normal correction message ("Re your earlier question … my answer is … Please course-correct"), so you can still steer Claude after the fact.
- **Waiting-on-you detection** — a `Notification` hook writes a window-keyed marker when Claude blocks on a permission / approval prompt (including the Workflow tool's Bash-approval gate, which leaves no JSONL trace), so the topic shows "🔔 Waiting on you" instead of an eternal "🟡 Busy".
- **Interactive approval-gate cards (opt-in)** — with `CC_TELEGRAM_PERMISSION_PROMPTS=true`, tool-permission prompts (bridged user-launched / resumed, non-bypass sessions) and the Workflow tool's dynamic-workflow-launch approval surface as a card you can answer from Telegram via the manual ↑/↓/⏎/Esc keyboard (the Workflow card also shows the phases + token-cost warning). This release is **display-only** — the card honestly labels the nav controls as raw, un-cursor-verified live-terminal keystrokes; there is no one-tap option button yet. Default OFF; a flag-OFF deploy detects and changes nothing.
- **Generic decision cards (opt-in)** — with `CC_TELEGRAM_DECISION_CARDS=true`, titled numbered-option confirmation prompts that no built-in interactive pattern covers (the "Switch model?" confirmation, the folder-trust prompt, and peers) surface as a display-only card with the same manual nav keyboard. Last-priority + strict-or-None, so it never shadows a built-in prompt type and never re-exposes a permission/workflow gate its own flag left off. Independent of the approval-gate flag; default OFF; a flag-OFF deploy detects and changes nothing.
- **Tappable Decision dispatch (opt-in, canary)** — with `CC_TELEGRAM_DECISION_DISPATCH=true` **and** the decision cards flag ON, a decision prompt from a *known-good family on a characterized Claude Code version* (currently the folder-trust prompt on the versions in `handlers/decision_token.py`'s dispatch table) also gets one-tap option buttons: a tap arrow-navigates the live cursor to the choice, verifies it landed, and presses Enter — the AUQ v2.1.168 navigate→verify→Enter discipline through a parallel, Decision-specific lane. Anything else (unknown family, an un-characterized CC version, a busy/quoted pane) stays display-only. Default OFF; a flag-OFF deploy mints no buttons and the callback declines.
- **📎 Tap-to-download file cards** — when Claude's prose mentions a deliverable local file (`report.md`, `chart.png`, `export.pdf`, and other document/data/image formats), the bot posts a compact 📎 card with one button per file; a tap uploads that file to the topic as a Telegram document. Detection is parent-prose only (never tool output, sub-agent narration, or web URLs) and every file is fully validated before upload — it must resolve (through symlinks) to a real regular file under the session's working directory (or an extra root in `CC_TELEGRAM_ARTIFACT_ROOTS`) and be within the size cap (worktree sessions also resolve a relative path against their main repo root, so a handoff written up a level still uploads); the upload itself goes through a re-validated file descriptor (`O_NOFOLLOW` + `fstat`), never a re-opened pathname. A tap is owner-only. `/file <path>` is the always-available manual escape hatch (it accepts any file type, not just the auto-offered formats, and paths with spaces). Governed by the `/settings` **📎 Files** toggle (on for every preset except `quiet`). **Note:** because detection is prose-driven, files with secrets-adjacent extensions (`.json`, `.log`, `.txt`) can be offered when Claude names them — a tap still uploads the file to the topic, so treat the card as you would any file share.
- **Machine-surface terminal geometry** — bot-created tmux windows default to `160x50` (`CC_TELEGRAM_WINDOW_GEOMETRY`), and existing windows are resized once at startup. Nobody looks at the terminals directly — Telegram is the UI — so the geometry serves the parser: 50 rows keep a tall `AskUserQuestion` picker fully on-screen (real cursor visible from the first frame), 160 columns keep long option labels from overflowing.
- **`/update` — update the CLI + restart idle sessions in place (owner-only)** — updates the Claude Code CLI binary, then restarts each **idle** bound session inside its existing tmux window (via `claude --resume`, so it adopts the new version and the topic keeps its window id / routing). Busy or waiting sessions are **deferred**, never interrupted — as are sessions with live background shells (the `· N shell` status-bar token: a restart would kill those jobs): it is fail-closed and idle-only (a session that won't cleanly exit within the bounded ~15s wait is skipped, not force-killed — the summary then warns the session may be dead and to check the window before sending messages, since `/exit` was already sent). A skipped window is additionally **quarantined**: the bot refuses to type new messages into it until it observes Claude running there again (a message typed into a dead session's bare shell would be *executed* by the shell), replying with an explicit "Message NOT delivered" error in the topic instead. "Claude running" is strict: the pane must report Claude's own version-string process — any other foreground command, including one you started yourself while checking the window (vim, python, ssh), keeps sends blocked. A relaunch counts as successful only once Claude is actually *observed* running in the pane (a bounded ~10s check after the relaunch keystroke, since a broken `CLAUDE_COMMAND` or auth failure can drop straight back to the shell); an unconfirmed relaunch is reported in the summary and keeps the quarantine. The quarantine clears automatically once Claude is seen alive, on a later confirmed restart, or when the window/topic is torn down; it is in-memory — a bot restart clears it. (Residuals: a Claude that crashes *after* being confirmed is out of scope, and if a future Claude Code changes how the pane reports its process, quarantined sends keep refusing — fail-closed — until a `/update` rerun, window recreate, or bot restart.) Restarts run one at a time; a second `/update` while one is running is rejected. It reports a progressive summary (`♻️ Restarted N idle · deferred M busy · skipped K`). No scheduler — run it when you want the running sessions on a freshly-updated CLI. Note: `CLAUDE_COMMAND` must exec the claude binary directly (or via an exec-ing wrapper) — a non-exec shell wrapper makes the pane report the wrapper shell while Claude is still alive, defeating `/update`'s shell-detection safety gate in the dangerous direction.
- **Streaming output** — assistant text, thinking, tool use/result summaries, interactive prompts, and local command output flow into Telegram.
- **Per-route queues** — each `(user_id, thread_id, window_id)` has its own worker, so one noisy topic does not stall another.
- **Run-state digest** — compact activity digests show tool activity, context-window percentage, and busy/waiting state. When the turn finishes, the digest **collapses to a one-line summary** (`✅ Done — repo · 14 tools · 2 sub-agents · 3m 41s`) by default — the play-by-play is valuable live, scrollback noise afterwards; `/history` keeps the full log. Per-sub-agent cards collapse the same way when the sub-agent finishes (its 🤖✅ report message stays, full and expandable) — including the `Workflow` tool's background sub-agents, which now also surface as `↳` cards and collapse at the workflow's close.
- **Per-user output verbosity** — `/settings` (any topic or DM) opens a personal panel with presets (`verbose` / `standard` / `compact` / `quiet`) plus quick knobs (tool-line length, done-card policy keep/collapse/delete, sub-agent cards keep/collapse/off, 👤 echo, 📊 footer, 📎 file cards). Choices persist in `state.json` and apply to everything the bot sends *to you*, in every topic; another allowed user tapping your panel changes nothing. Default preset is `standard`; `verbose` restores the pre-settings behavior exactly. Errors, interactive prompts, and the 🤖✅ sub-agent report stay visible at every preset.
- **Cross-topic dashboard** — `/dashboard` run inside any forum topic claims that topic as your dashboard host: one passive message listing every topic you have bound **in that forum** (per-chat scoped — a dashboard never lists another chat's topics, and a topic whose chat can't be resolved is excluded, fail-closed), grouped needs-attention-first (🔔 waiting on you · 🟡 running · ⚪ idle), repainted by the status poller when content changes. Re-running `/dashboard` in another topic moves it; `/dashboard pin` pins the message (opt-in only — never automatic). 🔔 also covers an idle topic whose last assistant turn ended after your last message (the "unanswered turn"); after a bot restart those in-memory wall-clock stamps are gone, so the dashboard renders state-only until fresh turns repopulate them. **Visibility note:** the dashboard is owner-*filtered*, not private — any member of the shared forum can read it.
- **Reply context** — Telegram replies/quotes are injected into Claude with fenced, role-aware context for text, voice, photo, and document messages.
- **Photos and voice** — photos are forwarded as base64 image blocks; voice notes are transcribed through OpenAI-compatible transcription.
- **Attention cards** — when Claude is waiting on you and the structured picker can't be delivered to the topic, a single bold "Claude is waiting for you" card is pushed (notified once per episode, then silently kept current).
- **SQLite provenance** — outgoing Telegram messages are indexed for safer reply-context resolution.
- **Reactive broken-topic fallback** — if Telegram says a topic is gone/closed/forbidden, the bot falls back to DM rather than silently dropping Claude output.

## Quick start

Zero to working bot in a handful of commands:

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv tool install --force --no-cache .   # --no-cache REQUIRED — see note below
mkdir -p ~/.cc-telegram && $EDITOR ~/.cc-telegram/.env  # TELEGRAM_BOT_TOKEN + ALLOWED_USERS (the only two required)
cc-telegram hook --install
cc-telegram doctor       # checks token/users/tmux/claude/SessionStart-hook/config-dir
cc-telegram              # foreground, or daemonize on macOS with: bash bin/install-service.sh
```

> **`--no-cache` is mandatory.** uv's wheel cache is keyed on the package version, and the version is not bumped on every deploy — so `uv tool install --force .` *alone* silently reinstalls a stale cached wheel (exits 0, your code never ships). See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** for the full end-to-end guide: launchd setup, the upgrade recipe and why, Claude Code auth, verification, and troubleshooting. New code agent? Start at **[AGENTS.md](AGENTS.md)**.

## Requirements

- Python 3.12+
- `uv`
- `tmux`
- Claude Code CLI (`claude`) in `PATH`, **independently authenticated** — run `claude` once interactively to log in (cc-telegram manages no Anthropic credentials; it only drives the `claude` binary, so an unauthenticated CLI shows opaque failures inside the topic)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram supergroup with forum topics enabled

> **Windows:** the bot requires tmux and therefore runs under WSL2 — the bot, tmux, and the Claude Code sessions it drives all live inside WSL. See **[docs/windows-wsl.md](docs/windows-wsl.md)** for setup and for sharing Claude Code skills/settings between WSL and a native Windows install.

## Install

Two modes — pick one.

**Install as a tool (production / the deploy path):**

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv tool install --force --no-cache .   # puts `cc-telegram` on PATH at ~/.local/bin
```

Then use bare `cc-telegram …`. `--no-cache` is required because the version is not bumped on every deploy (see the Quick start note).

**Run from source (development):**

```bash
git clone https://github.com/etcircle/cc-telegram.git && cd cc-telegram
uv sync --all-extras          # creates the dev .venv; does NOT put `cc-telegram` on PATH
```

Then always prefix commands with `uv run` (e.g. `uv run cc-telegram doctor`, `uv run cc-telegram`).

## Configure

Create `~/.cc-telegram/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

Core variables:

- `TELEGRAM_BOT_TOKEN` — required; from BotFather.
- `ALLOWED_USERS` — required; comma-separated Telegram user IDs.
- `CC_TELEGRAM_DIR` — config/state directory; default `~/.cc-telegram`.
- `TMUX_SESSION_NAME` — tmux session driven by the bot; default `cc-telegram`.
- `CLAUDE_COMMAND` — command used for new windows; default `claude`. Must exec the claude binary directly (or via an exec-ing wrapper); a non-exec shell wrapper defeats `/update`'s shell-detection gate (see the `/update` bullet above).
- `CLAUDE_CONFIG_DIR` — Claude config root; projects default to `$CLAUDE_CONFIG_DIR/projects`.
- `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` — explicit Claude projects directory override. Precedence: `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` > `CLAUDE_CONFIG_DIR/projects` > `~/.claude/projects`.
- `MONITOR_POLL_INTERVAL` — JSONL poll interval; default `2.0`.
- `CC_TELEGRAM_BROWSE_ROOT` — directory picker root; default `~`.
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` — optional voice transcription provider.

Useful behavior knobs:

- `CC_TELEGRAM_VERBOSITY` — default output preset (`verbose` / `standard` /
  `compact` / `quiet`) for users who have not picked one via `/settings`;
  default `standard` (collapsed post-turn digests, 160-char tool lines, user
  echo off — `verbose` restores the pre-settings firehose). Per-user
  `/settings` choices always win over env defaults — the env vars below are
  knob-precise **defaults, not ceilings**.
- `CC_TELEGRAM_SHOW_USER_MESSAGES` — echo user messages from tmux; default `true`.
  When set explicitly it becomes the default for the per-user 👤-echo
  preference; a user's stored `/settings` choice overrides it.
- `CC_TELEGRAM_SHOW_TOOL_CALLS` — show tool use/result stream; default `true`.
  Setting it to `false` suppresses **display only** (the faithful legacy
  mapping: all tool surfaces including the 🤖 sub-agent dispatch/report and
  the per-sub-agent cards): sidechain transcripts are still tailed and their
  activity still feeds the run-state truth (busy indicator / typing), so a
  long subagent run doesn't read as idle. A user's stored `/settings` choice
  overrides it.
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

### State files

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

All state files are safe to delete — the bot re-creates what it needs on next start (you will lose interactive picker continuity and bound topic mappings).

## Voice transcription

Voice notes are transcribed via a standard OpenAI `POST $OPENAI_BASE_URL/audio/transcriptions` call with `Authorization: Bearer $OPENAI_API_KEY`. The transcription model is **hardcoded to `gpt-4o-transcribe`** (`transcribe.py`; no override env var), so the backend must expose that exact model name. `OPENAI_API_KEY` is required **for voice only** — without it, voice notes fail with a raw 401. Point `OPENAI_BASE_URL` at anything that speaks that shape:

- `https://api.openai.com/v1` — the default.
- A local LiteLLM, vLLM, or other OpenAI-compatible gateway that serves `gpt-4o-transcribe`.
- A backend exposing only a different STT model (e.g. OpenRouter's `whisper-1`) will return a model-not-found error unless fronted by a model-name-translating proxy.

If your backend doesn't natively speak OpenAI's STT shape (e.g., a local `whisper.cpp` server with its `/inference` endpoint), or serves a different model name, front it with a small shape-translating proxy and point `OPENAI_BASE_URL` at that. (An external `whisper-openai-proxy` example — a ~130-line stdlib-only shim — is an optional companion; it is not part of this repo.)

## Install the Claude Code hook

```bash
uv run cc-telegram hook --install
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

The `SessionStart` hook writes `session_map.json` so the bot can route messages back to the right tmux window. The `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured question payload before Claude renders the picker — see the next section. The `Notification` hook (matcher-less) writes a window-keyed `notify_pending/<session_id>.json` marker when Claude blocks on a permission / approval prompt, so the bot can flip the topic to "🔔 Waiting on you" — the only detection path for approval gates that never reach the session JSONL. No notification text is stored in the marker. If either the `PreToolUse` or the `Notification` entry is missing, the bot logs a one-time startup warning; re-run `cc-telegram hook --install` to repair.

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

## Run

```bash
uv run cc-telegram
```

If installed as a tool:

```bash
cc-telegram
```

For day-to-day use, run it under launchd (below) or a process supervisor.

## Run under launchd (macOS)

**No main-bot plist ships in the repo.** Generate and load the LaunchAgent (label `com.cc-telegram`) with the bundled installer:

```bash
bash bin/install-service.sh          # writes ~/Library/LaunchAgents/com.cc-telegram.plist, then bootstrap + enable
bash bin/install-service.sh --print  # dry-run: print the plist it would write (still needs cc-telegram on PATH)
```

`cc-telegram` must already be on PATH (install as a tool, above). The script sets an explicit `PATH` in the plist so launchd can find `cc-telegram`/`tmux`/`claude`, enables `KeepAlive`+`RunAtLoad`, and redirects stdout/stderr to `$CC_TELEGRAM_DIR/launchd.{out,err}.log`. Hand-written-plist instructions and the full rationale are in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** section 7.

## Restart the service

Once the LaunchAgent exists, restart (kill + relaunch) the bot with:

```bash
launchctl kickstart -k gui/$(id -u)/com.cc-telegram
```

## Log rotation

`launchd.err.log` and `launchd.out.log` are written by launchd's
stderr/stdout redirect, not by Python's logging — so the bot can't
rotate them itself. A small LaunchAgent handles rotation: every 30
minutes it checks both files, gzips a dated copy into
`~/.cc-telegram/log-archive/` if either exceeds 50MB, and truncates
the original in place (safe under the bot's `O_APPEND` write).
Archives older than 14 days are deleted automatically. Install with:

```bash
bash bin/install-log-rotate.sh
```

The script is idempotent — re-running replaces the existing agent.
Override thresholds via env in the plist `EnvironmentVariables` block
(`CC_TELEGRAM_LOG_ROTATE_THRESHOLD_MB`,
`CC_TELEGRAM_LOG_ROTATE_MAX_AGE_DAYS`).

Force a rotation pass now:

```bash
launchctl kickstart gui/$(id -u)/com.cc-telegram.log-rotate
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.cc-telegram.log-rotate
rm ~/Library/LaunchAgents/com.cc-telegram.log-rotate.plist
```

Without this, a crash-loop (e.g. a startup AttributeError under
`KeepAlive=true`) can balloon `launchd.err.log` to hundreds of
megabytes and trigger Telegram `getUpdates` rate-limiting via the
restart spam. The rotation cap also caps the blast radius.

## Config directory override

Default config dir: `~/.cc-telegram`.

Override with the `CC_TELEGRAM_DIR` env var:

```bash
CC_TELEGRAM_DIR=/path/to/state cc-telegram
```

Useful for testing or running multiple profiles against the same install.

## Recommended daily-driver `.env`

Only use this if the bot runs on a machine you trust and `ALLOWED_USERS` is locked to you. `--dangerously-skip-permissions` means Claude can act without local confirmation.

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

## Test

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
uv run pytest -m scenario -q          # behavior floor (tests/scenarios/)
bin/post-wave-check.sh                # repo health diff (LoC + brittleness signals)
```

`tests/scenarios/` holds the black-box behavior floor: each file drives a
single user-visible scenario through the real handler stack (no
monkeypatch of handler internals in test bodies). See
`tests/scenarios/README.md` for the scenario → behavior map.

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
