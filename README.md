# cc-telegram

A Telegram ↔ Claude Code bridge — control Claude Code sessions remotely
through Telegram forum topics. Each topic maps 1:1 to a tmux window running
one Claude Code instance, so the terminal remains the source of truth and
you can always `tmux attach` to pick up where you left off.

> **Fork notice.** This is a polished, daily-driver fork of
> [`six-ddc/ccbot`](https://github.com/six-ddc/ccbot). The upstream had the
> right shape; this fork has been hardened across reliability, latency,
> media handling (photos + text + voice), and forum-topic UX so it holds up
> when you actually live in the bot across many concurrent topics. See
> [`docs/plans/`](docs/plans/) for active design notes.

## Why this fork

The upstream codebase already had the right shape. This fork concentrates on
reliability and UX issues that surface once you actually live in the bot
across many concurrent topics:

- **Latency under load.** A `find_users_for_session` hot path was opening
  every bound window's JSONL file (multi-MB) on every emitted message. With
  ~14 topics open the queue would back up minutes deep and content arrived
  long after the user gave up. Now answered from in-memory window-state in
  microseconds.
- **First-reply loss after fresh window creation.** New sessions were
  initialized at end-of-file by the monitor, dropping the seed user message
  and Claude's first reply. New sessions are now pre-registered at offset 0
  before the pending text is forwarded.
- **Stale "🟡 Busy" indicator.** Stuck after Claude finished because the
  spinner+summary line stays visible. Now cleared after 4 s of confirmed
  idle (time-based, robust to long poll cycles).
- **Echoed user messages.** Telegram-typed prompts came back as "👤 …"
  bubbles after Claude logged them. Now deduped: text the bot just typed
  via `send_to_window` is recorded and the matching JSONL user-message is
  suppressed exactly once per send.
- **Activity digest truncation.** Tool inputs were capped at 200 chars and
  tool-result snippets at the first 18 words, so most bash invocations were
  unreadable. Caps raised to 600 / 400 with first-line snippet logic.
- **In-topic "Claude needs a decision" card** for assistant text removed —
  Claude's reply already lands in the topic, so the card was duplicate
  noise. Interactive-UI cards (permission prompts, ExitPlanMode) retained.
- **Status polling parallelized** via `asyncio.gather` so per-binding cost
  no longer scales with N_topics.
- **Native Telegram typing indicator** in the topic while Claude is
  actively running — fired before any `skip_status` guard so a queue
  backlog doesn't suppress it.

[`docs/plans/2026-05-02-event-driven-busy-and-route-queues.md`](docs/plans/2026-05-02-event-driven-busy-and-route-queues.md)
captures the next round: switching the typing indicator from pane-scrape to
JSONL-event-driven, plus a sqlite-backed durable queue so restarts don't
lose pending content.

## Features

- **Topic-based sessions** — Each Telegram topic = one tmux window = one
  Claude session. Internal routing keyed by tmux window ID, so the same
  directory can host multiple parallel sessions.
- **Real-time forwarding** — Assistant text, thinking, tool use/result, and
  local command output stream into the topic as they're written to JSONL.
- **Photos + text + voice** — Telegram photos (and photos with captions)
  are forwarded to Claude as base64 image blocks alongside the text, so
  you can hand off screenshots, mockups, or diagrams without leaving
  Telegram. Voice notes are transcribed via OpenAI and forwarded as text.
- **Interactive UI** — `AskUserQuestion`, `ExitPlanMode`, and permission
  prompts surface as inline keyboards so you can answer without opening
  the laptop.
- **Slash command forwarding** — Any `/command` (`/clear`, `/compact`,
  `/cost`, `/usage`, …) is forwarded straight to the underlying Claude.
- **Directory-browser session creation** — First message in an unbound
  topic opens a directory picker; pre-existing Claude sessions in the
  chosen directory are listed for resume.
- **Persistent state** — Thread bindings, group chat IDs, read offsets,
  and monitor state all survive bot restarts.
- **Hook-based session tracking** — Claude Code's `SessionStart` hook
  writes the window→session map; the bot picks up `/clear` and resumes
  automatically.

## Tech stack

Python 3.12+, [`python-telegram-bot[rate-limiter]`](https://docs.python-telegram-bot.org/),
[`libtmux`](https://libtmux.git-pull.com/), [`uv`](https://docs.astral.sh/uv/),
[`ruff`](https://docs.astral.sh/ruff/), [`pyright`](https://microsoft.github.io/pyright/),
[`pytest`](https://docs.pytest.org/) (313 tests passing).

## Prerequisites

- **tmux** in PATH
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

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | from `@BotFather` |
| `ALLOWED_USERS` | required | comma-separated Telegram user IDs |
| `CCBOT_DIR` | `~/.ccbot` | config + state directory |
| `TMUX_SESSION_NAME` | `ccbot` | tmux session the bot drives |
| `CLAUDE_COMMAND` | `claude` | command for new windows |
| `MONITOR_POLL_INTERVAL` | `2.0` | JSONL poll seconds |
| `CCBOT_SHOW_USER_MESSAGES` | `true` | echo direct-tmux user input back to Telegram |
| `CCBOT_SHOW_TOOL_CALLS` | `true` | include tool use/result in stream |
| `OPENAI_API_KEY` | — | enables voice transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | for OpenAI-compatible proxies |

## Recommended settings

> ⚠️ **Read this before copying.** The settings below trade safety for
> ergonomics. They assume you trust the machine the bot runs on, you
> understand what `--dangerously-skip-permissions` does, and you've locked
> `ALLOWED_USERS` to your own Telegram account(s). Do **not** run these on
> a shared host or expose the bot to anyone you wouldn't hand a root shell
> to. If any of that sounds wrong, stick to the defaults.

The combination we actually run day-to-day:

```ini
# ~/.ccbot/.env
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=<your_id>

# Run Claude in sandbox + auto-approve mode so you don't get blocked on
# tool-permission prompts from a phone. IS_SANDBOX=1 tells Claude it's
# already inside a constrained environment; --dangerously-skip-permissions
# disables the per-tool confirmations.
CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions

# Snappier streaming for foreground use (default 2.0s)
MONITOR_POLL_INTERVAL=1.0

# Voice → text. Worth the API cost; talking is faster than typing.
OPENAI_API_KEY=sk-...

# Optional: hide tool-call chatter if you only care about Claude's prose.
# Defaults are both true; flip to false if your topics get noisy.
# CCBOT_SHOW_TOOL_CALLS=false
# CCBOT_SHOW_USER_MESSAGES=false
```

Why each one matters:

- **`--dangerously-skip-permissions`** — Without it, every `Bash`, `Edit`,
  `Write`, etc. blocks on a permission prompt that you can only answer
  from the local terminal. From a phone, that means dead-air. Skipping
  permissions makes the bot genuinely usable on the move; the tradeoff
  is that anything Claude decides to do, it does. Pair it with a
  sandboxed working directory and `IS_SANDBOX=1` so Claude is aware it's
  in a constrained environment.
- **`IS_SANDBOX=1`** — Lets Claude know it's running unsupervised and
  should self-impose stricter scoping (don't `rm -rf`, don't touch
  unrelated paths). Belt-and-braces alongside the flag above.
- **`MONITOR_POLL_INTERVAL=1.0`** — Halves perceived latency on
  Claude → Telegram streaming. CPU cost is negligible at typical session
  counts.
- **`OPENAI_API_KEY`** — Enables `/voice` transcription. Without this,
  voice notes are ignored.
- **`CCBOT_SHOW_TOOL_CALLS=false`** *(optional)* — If you only want
  Claude's prose in the topic, this hides the tool_use/tool_result
  stream.

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

`scripts/restart.sh` restarts the bot inside the `ccbot:__main__` tmux
window. **Caveat: today's restart wipes the in-memory message queue —
see the durable-queue plan in [docs/plans/](docs/plans/) for the fix in
flight.**

## Repository layout

```
src/ccbot/                     core package
src/ccbot/handlers/            telegram interaction layer
  attention.py                 idle↔waiting state machine for "Claude needs you" cards
  message_queue.py             per-user FIFO queue + worker (merging, rate limit)
  message_sender.py            safe send/edit/delete with MarkdownV2 fallback
  status_polling.py            per-binding poll loop (parallelized)
  interactive_ui.py            AskUserQuestion / ExitPlanMode / permission UI
  directory_browser.py         directory + session picker for new topics
  cleanup.py                   centralized topic teardown
tests/                         pytest, asyncio_mode=auto
.claude/rules/                 architecture notes (loaded by Claude Code)
docs/plans/                    design plans for upcoming changes
doc/                           upstream protocol notes
```

## License

MIT — see [LICENSE](LICENSE). Original work © 2024–2026 the upstream
contributors; fork modifications © 2026 etcircle.
