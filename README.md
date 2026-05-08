# cc-telegram

A Telegram ↔ Claude Code bridge — control Claude Code sessions remotely
through Telegram forum topics. Each topic maps 1:1 to a tmux window
running one Claude Code instance, so the terminal stays the source of
truth and you can always `tmux attach` to pick up where you left off.

> **Fork notice.** This is a polished, daily-driver fork of
> [`six-ddc/ccbot`](https://github.com/six-ddc/ccbot). The upstream had
> the right shape; this fork concentrates on what surfaces once you
> actually live in the bot across many concurrent topics — per-route
> queueing, an event-driven busy/run-state machine, activity digests,
> end-of-turn attention prompts with one-tap buttons, photo/voice/reply
> bridging, and a SQLite provenance layer for safe reply-context
> resolution. See [`docs/plans/`](docs/plans/) for active design notes.

## What this fork adds on top of upstream

Concrete changes shipped beyond `six-ddc/ccbot` (every bullet has tests
and is gated behind a feature flag where appropriate so you can opt
in):

- **Per-route message queues.** Replaces the per-user FIFO. Routes are
  keyed by `(user_id, thread_id, window_id)` and each has its own
  worker, so a backlog in one topic no longer stalls status clearing or
  attention prompts in another. Coalesced ephemeral status slot per
  route preserves the status-after-content invariant locally.
- **Event-driven busy / run-state machine.** A `RunState` machine
  (`RUNNING` / `RUNNING_TOOL` / `WAITING_ON_USER` / `IDLE_RECENT` /
  `IDLE_CLEARED` / `BROKEN_TOPIC`) driven by JSONL tool-use /
  tool-result lifecycle events instead of pane scraping. Native "is
  typing…" indicator runs on a dedicated 3-second loop that reads
  state directly with no tmux I/O — so it doesn't expire mid-turn even
  with 14+ active bindings. On by default; toggle with
  `CCBOT_BUSY_INDICATOR_V2=false` to fall back to the legacy V1 path.
- **Activity digest.** A per-turn digest message summarizes tool
  activity (counts, snippets) under a run-state header, with a
  threshold-gated context-% suffix lifted from the JSONL transcript
  (e.g. `· ctx 89%`, `⚠️` past 95%). Final assistant text always lands
  *after* the digest in chronological order.
- **Context-window usage in three places.** The same JSONL-derived
  context-% drives (1) the run-state digest header, (2) an optional
  per-turn footer on end-of-turn assistant messages (`📊 113k / 200k`
  — snapshot at send-time, never edited), and (3) a warning glyph on
  long-running topics. Replaces the old `/context` slash command —
  now removed since the footer covers the same need.
- **Tool-call summary truncation, configurable.** The per-tool input
  surfaced in tool_use lines (e.g. `**Bash**(...)`, `**Read**(...)`)
  is bounded by `CCBOT_TOOL_SUMMARY_MAX_CHARS` (default 40 — compact
  feed; raise to 600 to preserve full bash one-liners at the cost of
  multi-line activity entries).
- **End-of-turn attention card with answer buttons.** Strict trigger
  (assistant text + `stop_reason ∈ {end_turn, stop_sequence}` + final
  paragraph ends with `?` + `WAITING_ON_USER`) surfaces a prominent
  attention card with `[✅ Yes] [❌ No] [💬 Type in chat]` inline
  keyboard, modelled on the official `anthropics/claude-plugins-official`
  Telegram plugin's permission-request pattern. Token-keyed callback
  map (so `callback_data` fits Telegram's 64-byte cap), per-user auth,
  card edits to `✅ Replied: yes` for audit / idempotency. TTL-bounded
  with daily GC.
- **Subagent (Task tool) prominence.** Agent dispatches get promoted
  out of the activity digest into a top-level `🤖 Subagent dispatched`
  message; completion edits the same message in place with `🤖✅` /
  `❌` / `⏹` and the result, so subagent runs don't get buried in the
  per-turn digest.
- **Reply-context bridge — across all message types.** When you reply
  to a Telegram message (with optional Telegram quote), the original
  + the quoted body are forwarded to Claude inside random-token quote
  fences (`<<<QUOTE_xxx>>>` / `<<<END_QUOTE_xxx>>>`) so adversarial
  quoted content can't break out into a fake `[User message]` block.
  SQLite provenance lookups enrich the quote with role / content_type
  — quotes of UI noise (status / activity cards) render under a
  "this is UI state" header instead of being passed to Claude as
  load-bearing instruction. The render covers text, voice
  (transcribed), photo, and document replies; previously voice / photo
  / document silently dropped the quote.
- **Stay out of the topic title.** The bot never modifies forum topic
  titles. Earlier versions appended live tokens / context indicators
  to titles, which spammed Telegram's "topic changed" system events;
  status now lives entirely in messages. On startup, any leftover
  `· Nk/Mk` suffixes from the old behavior are stripped from
  ``window_display_names``.
- **Inbound aggregator.** Per-route 1.5-second debounce coalesces
  caption + media-group + photo-then-text fast-follow into a single
  `send_to_window` call. Multi-screenshot rule: caption appears
  exactly once, all paths grouped under `(attachments: …)` in
  arrival order.
- **Photos → Claude as base64.** Telegram photos (and photos with
  captions) are forwarded as base64 image blocks alongside the text,
  so you can hand off screenshots, mockups, or diagrams without
  leaving Telegram. Voice notes are transcribed via OpenAI and
  forwarded as text.
- **TranscriptEvent layer + transcript_uuid plumbing.** Structured
  lifecycle events (`block_type` / `tool_use_id` / `tool_name` /
  `stop_reason` / `timestamp`) flow below the legacy `NewMessage`
  callback so multiple consumers can read the JSONL lifecycle without
  re-parsing. Every `ParsedEntry` carries its source `transcript_uuid`,
  which is the foundation for the SQLite provenance table.
- **SQLite `telegram_message_refs` table.** aiosqlite-backed
  fire-and-forget provenance for every outgoing Telegram message
  (role / content_type / session_id / window_id / transcript_uuid /
  truncated body + sha256 of the full body). Drives the reply-context
  resolver above. WAL mode, 30-day retention with daily GC, bounded
  text column, and writes never block the send path. DB path
  overridable via `CCBOT_MESSAGE_REFS_DB_PATH`.
- **Reliability hotfixes** that arrived alongside the bigger work:
  - **Silent message loss after `/clear` (3 root causes).** When a
    user hit `/clear` and immediately sent a quoted reply, the bot
    delivered nothing for ~10 minutes while the topic stayed pinned
    "🟡 Busy". Three landed fixes: `session_monitor` calls
    `busy_indicator.clear_route` on a window's session-id flip so
    `_open_tools` from the dead session can't pin RUNNING forever; the
    transcript parser detects `system / turn_duration` empty-turn
    entries and emits a visible "⚠️ Claude finished without
    responding" warning plus a synthetic `end_turn` lifecycle marker;
    and the bot drops the `<<<QUOTE_…>>>` reply-context wrapper when
    the quoted message's session_id no longer matches the topic's
    current bound session (the model returned empty turns when asked
    to reason about a quote that pointed into a `/clear`-ed session).
  - **`session_map.json` cleanup race.** Cleanup paths in
    `SessionManager` were doing read-modify-write on `session_map.json`
    without holding `session_map.lock`. A SessionStart hook write
    landing between read and write was getting clobbered by the bot's
    stale snapshot, leaving the bot tailing dead JSONL files. Both
    cleanup paths now flock the same lock as `hook.py`.
  - **Skip SDK sub-agent SessionStart hooks.** Sub-agents launched
    inside Claude (entrypoint `sdk-cli`) were overwriting
    `session_map.json` with their own short-lived session id, so the
    bot would start tailing a session that disappears when the
    sub-agent finishes — and miss every subsequent user reply.
  - **Status polling: adaptive pane capture + 10 s watchdog.** Pane
    captures back off to coarser intervals when the pane is idle and
    are bounded by a 10 s watchdog so a hung tmux subprocess can't
    stall the polling loop.
  - **tmux call hot path.** Cached `shutil.which`, a TTL cache on
    `list_windows`, and a single subprocess per cache miss for hot
    read paths — measurable cut to the per-poll wall-clock, and
    a much calmer process tree.
  - Status card no longer resurrected by a post-completion pane summary.
  - Bounded `RetryAfter` retry path for content tasks (3 attempts) with
    correct merged-task capture so retries don't re-drain the queue.
  - Removed the destructive 60s `unpin_all_forum_topic_messages` topic
    liveness probe — it was clearing user-pinned messages on success,
    not a no-op. Liveness is now reactive via classified
    `topic_send` / `topic_edit` failures.
  - Directory browser defaults to `~` (overridable via
    `CCBOT_BROWSE_ROOT`) instead of the bot's cwd, so restarting from
    inside the project tree no longer surfaces the bot's own source.
    Unbound topics open the directory browser first; "🖥 Bind existing
    window" is an opt-in button rather than the default flow.
  - `TELEGRAM_BOT_TOKEN` / `ALLOWED_USERS` / `OPENAI_API_KEY`
    scrubbed from `os.environ` after load so they can't leak to the
    Claude subprocess via tmux.

Active design notes live in [`docs/plans/`](docs/plans/) — the
event-driven busy + per-route-queue plan
([`2026-05-02-…`](docs/plans/2026-05-02-event-driven-busy-and-route-queues.md))
and the in-progress lean-rename effort
([`2026-05-05-…-implementation-plan.md`](docs/plans/2026-05-05-cc-telegram-lean-rename-implementation-plan.md))
are the current ones.

## Features

- **Topic-based sessions** — Each Telegram topic = one tmux window =
  one Claude session. Routing keyed by tmux window ID, so the same
  directory can host multiple parallel sessions.
- **Real-time forwarding** — Assistant text, thinking, tool use /
  result, and local command output stream into the topic as they're
  written to JSONL.
- **Photos + text + voice** — Telegram photos forwarded to Claude as
  base64 image blocks; voice notes transcribed via OpenAI and
  forwarded as text.
- **Reply with context** — Reply (or Telegram-quote) to any bot
  message and the quoted body is forwarded to Claude inside a
  fenced quote, with role-aware UI-noise demotion.
- **Activity digest + run-state header** — One digest message per
  turn with a live RunState badge and threshold-gated context%.
- **End-of-turn answer buttons** — `[✅ Yes] [❌ No] [💬 Type in chat]`
  for end-of-turn yes/no questions.
- **Interactive UI** — `AskUserQuestion`, `ExitPlanMode`, and
  permission prompts surface as inline keyboards.
- **Slash command forwarding** — `/clear`, `/compact`, `/cost`,
  `/usage`, `/model`, … forwarded straight to the underlying Claude.
- **Directory-browser session creation** — First message in an unbound
  topic opens a directory picker; existing Claude sessions in the
  chosen directory are listed for resume.
- **Persistent state** — Thread bindings, group chat IDs, read
  offsets, monitor state, and SQLite message refs all survive
  restarts.
- **Hook-based session tracking** — Claude Code's `SessionStart` hook
  writes the window→session map; the bot picks up `/clear` and
  resumes automatically.

## Tech stack

Python 3.12+,
[`python-telegram-bot[rate-limiter]`](https://docs.python-telegram-bot.org/),
[`libtmux`](https://libtmux.git-pull.com/),
[`aiosqlite`](https://aiosqlite.omnilib.dev/),
[`telegramify-markdown`](https://pypi.org/project/telegramify-markdown/),
[`uv`](https://docs.astral.sh/uv/),
[`ruff`](https://docs.astral.sh/ruff/),
[`pyright`](https://microsoft.github.io/pyright/),
[`pytest`](https://docs.pytest.org/).

## Prerequisites

- **tmux** in `PATH`
- **Claude Code** CLI (`claude`) installed
- A Telegram bot token from [@BotFather](https://t.me/BotFather), with
  **Threaded Mode** enabled

## Install

```bash
git clone https://github.com/etcircle/cc-telegram.git
cd cc-telegram
uv sync
```

## Configure

Create `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

Core variables:

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | from `@BotFather` |
| `ALLOWED_USERS` | required | comma-separated Telegram user IDs |
| `CCBOT_DIR` | `~/.ccbot` | config + state directory |
| `TMUX_SESSION_NAME` | `ccbot` | tmux session the bot drives |
| `CLAUDE_COMMAND` | `claude` | command for new windows |
| `CLAUDE_CONFIG_DIR` / `CCBOT_CLAUDE_PROJECTS_PATH` | `~/.claude` | for Claude variants (cc-mirror, zai, …) |
| `MONITOR_POLL_INTERVAL` | `2.0` | JSONL poll seconds |
| `CCBOT_BROWSE_ROOT` | `~` | directory-browser starting point |
| `CCBOT_SHOW_USER_MESSAGES` | `true` | echo direct-tmux user input back to Telegram |
| `CCBOT_SHOW_TOOL_CALLS` | `true` | include tool use / result in stream |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | show dot-directories in the picker |
| `CCBOT_TOOL_SUMMARY_MAX_CHARS` | `40` | truncation for tool input shown in `**Tool**(...)` lines |
| `OPENAI_API_KEY` | — | enables voice transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | for OpenAI-compatible proxies |

Feature flags for fork-specific behavior:

| Variable | Default | Notes |
|---|---|---|
| `CCBOT_BUSY_INDICATOR_V2` | `true` | event-driven RunState (typing-action, digest header, busy state); set `false` for legacy V1 |
| `CCBOT_ATTENTION_BUTTONS` | `true` | yes / no / type-in-chat buttons on end-of-turn cards |
| `CCBOT_ATTENTION_BUTTON_TTL_SECONDS` | `86400` | how long an attention token stays clickable |
| `CCBOT_ATTENTION_QUESTION_PREVIEW_CHARS` | `200` | end-of-turn-question card excerpt length |
| `CCBOT_AGENT_PROMPT_PREVIEW_CHARS` | `400` | excerpt length on the `🤖 Subagent dispatched` card |
| `CCBOT_REPLY_CONTEXT` | `true` | forward Telegram reply / quote to Claude inside fenced quotes |
| `CCBOT_QUOTE_INJECTION_MAX_CHARS` | `1600` | upper bound on the quoted-text excerpt injected into Claude's prompt |
| `CCBOT_AGGREGATOR_DEBOUNCE_SECONDS` | `1.5` | inbound aggregator window for caption + media-group bundling |
| `CCBOT_AGGREGATOR_MAX_ATTACHMENTS` | `10` | per-bundle attachment cap (photos + documents) |
| `CCBOT_MAX_ATTACHMENT_SIZE_BYTES` | `20971520` | upper bound on document downloads (default 20 MB) |
| `CCBOT_CONTEXT_PCT_THRESHOLD` | `80` | digest header shows context-% at or above this |
| `CCBOT_CONTEXT_IN_MESSAGE_FOOTER` | `true` | per-turn `📊 113k / 200k` footer on end-of-turn assistant messages |
| `CCBOT_MESSAGE_REFS_RETENTION_DAYS` | `30` | provenance-table GC retention |
| `CCBOT_MESSAGE_REFS_DB_PATH` | `$CCBOT_DIR/message_refs.db` | SQLite path |

## Recommended settings

> ⚠️ **Read this before copying.** The settings below trade safety for
> ergonomics. They assume you trust the machine the bot runs on, you
> understand what `--dangerously-skip-permissions` does, and you've
> locked `ALLOWED_USERS` to your own Telegram account(s). Do **not** run
> these on a shared host or expose the bot to anyone you wouldn't hand a
> root shell to. If any of that sounds wrong, stick to the defaults.

The combination we actually run day-to-day:

```ini
# ~/.ccbot/.env
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=<your_id>

# Run Claude with permission prompts disabled and IS_SANDBOX=1 set so
# Claude knows it's running unsupervised. Without --dangerously-skip-
# permissions every Bash/Edit/Write blocks on a confirmation that you
# can only answer from the local terminal — from a phone, that's
# dead-air. This makes the bot genuinely usable on the move; the
# tradeoff is that anything Claude decides to do, it does.
CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions

# Snappier streaming for foreground use (default 2.0s).
MONITOR_POLL_INTERVAL=1.0

# Voice → text. Worth the API cost; talking is faster than typing.
OPENAI_API_KEY=sk-...

# Optional: directory picker default. Point at your code root.
CCBOT_BROWSE_ROOT=~/dev

# Optional: hide tool-call chatter if you only care about prose.
# CCBOT_SHOW_TOOL_CALLS=false
# CCBOT_SHOW_USER_MESSAGES=false
```

For headless / VPS deployment, also install the SessionStart hook
(`ccbot hook --install`) so `/clear` and resumes are picked up
automatically, and put the bot behind `tmux` + a process supervisor
(systemd, supervisord, or just `scripts/restart.sh`).

## Run

```bash
uv run ccbot
# Or, if installed as a tool:
ccbot
```

Auto-install the Claude Code SessionStart hook:

```bash
ccbot hook --install
```

## Test

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run pyright src/ccbot/
uv run pytest tests/
```

## Repository layout

```
src/ccbot/                     core package
src/ccbot/handlers/            telegram interaction layer
  attention.py                 §2.6 end-of-turn attention card + token map
  busy_indicator.py            RunState machine (V2)
  inbound_aggregator.py        per-route caption / media-group / photo+text bundler
  reply_context.py             Telegram reply / quote → fenced quote for Claude
  message_queue.py             per-route FIFO worker (merging, rate limit)
  message_sender.py            safe send/edit/delete with MarkdownV2 fallback
  status_polling.py            per-binding poll loop (parallelized) + typing-action loop
  interactive_ui.py            AskUserQuestion / ExitPlanMode / permission UI
  directory_browser.py         directory + session picker for new topics
  topic_repair.py              topic-broken detection + recovery
  history.py                   /history paginator
  cleanup.py                   centralized topic teardown
src/ccbot/message_refs.py      aiosqlite provenance table (telegram_message_refs)
src/ccbot/session_monitor.py   JSONL tail + TranscriptEvent dispatch
src/ccbot/transcript_parser.py JSONL → ParsedEntry / TranscriptEvent
tests/                         pytest, asyncio_mode=auto
.claude/rules/                 architecture notes (loaded by Claude Code)
docs/plans/                    design plans for upcoming changes
doc/                           upstream protocol notes
```

## License

MIT — see [LICENSE](LICENSE). Original work © 2024–2026 the upstream
contributors; fork modifications © 2026 etcircle.
