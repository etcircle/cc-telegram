# cc-telegram

A Telegram ↔ Claude Code bridge for running Claude sessions from Telegram forum topics.

Each Telegram topic maps to one tmux window running one Claude Code process. The terminal remains the source of truth, and Telegram becomes the remote control / notification layer.

## What it does

- **Topic-based sessions** — one Telegram topic = one tmux window = one Claude session.
- **Hook-based session tracking** — Claude Code `SessionStart` writes `session_map.json`, so `/clear` and resumed sessions stay attached to the right topic.
- **Streaming output** — assistant text, thinking, tool use/result summaries, interactive prompts, and local command output flow into Telegram.
- **Per-route queues** — each `(user_id, thread_id, window_id)` has its own worker, so one noisy topic does not stall another.
- **Run-state digest** — compact activity digests show tool activity, context-window percentage, and busy/waiting state.
- **Reply context** — Telegram replies/quotes are injected into Claude with fenced, role-aware context for text, voice, photo, and document messages.
- **Photos and voice** — photos are forwarded as base64 image blocks; voice notes are transcribed through OpenAI-compatible transcription.
- **Attention cards** — end-of-turn questions can raise a prominent card with yes/no/type buttons.
- **SQLite provenance** — outgoing Telegram messages are indexed for safer reply-context resolution.
- **Reactive broken-topic fallback** — if Telegram says a topic is gone/closed/forbidden, the bot falls back to DM rather than silently dropping Claude output.

## Requirements

- Python 3.12+
- `uv`
- `tmux`
- Claude Code CLI (`claude`) in `PATH`
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram supergroup with forum topics enabled

## Install

```bash
git clone https://github.com/etcircle/cc-telegram.git
cd cc-telegram
uv sync --all-extras
```

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
- `CLAUDE_COMMAND` — command used for new windows; default `claude`.
- `CLAUDE_CONFIG_DIR` — Claude config root; projects default to `$CLAUDE_CONFIG_DIR/projects`.
- `CC_TELEGRAM_CLAUDE_PROJECTS_PATH` — explicit Claude projects directory override.
- `MONITOR_POLL_INTERVAL` — JSONL poll interval; default `2.0`.
- `CC_TELEGRAM_BROWSE_ROOT` — directory picker root; default `~`.
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` — optional voice transcription provider.

Useful behavior knobs:

- `CC_TELEGRAM_SHOW_USER_MESSAGES` — echo user messages from tmux; default `true`.
- `CC_TELEGRAM_SHOW_TOOL_CALLS` — show tool use/result stream; default `true`.
- `CC_TELEGRAM_SHOW_HIDDEN_DIRS` — show dot-directories in picker; default `false`.
- `CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS` — max input shown in `**Tool**(...)`; default `40`.
- `CC_TELEGRAM_BUSY_INDICATOR_V2` — event-driven run-state/digest path; default `true`.
- `CC_TELEGRAM_ATTENTION_BUTTONS` — inline buttons on attention cards; default `true`.
- `CC_TELEGRAM_ATTENTION_BUTTON_TTL_SECONDS` — attention token TTL; default `86400`.
- `CC_TELEGRAM_ATTENTION_QUESTION_PREVIEW_CHARS` — question card excerpt; default `200`.
- `CC_TELEGRAM_AGENT_PROMPT_PREVIEW_CHARS` — subagent dispatch excerpt; default `400`.
- `CC_TELEGRAM_REPLY_CONTEXT` — inject reply/quote context; default `true`.
- `CC_TELEGRAM_QUOTE_INJECTION_MAX_CHARS` — max quoted text injected into Claude; default `1600`.
- `CC_TELEGRAM_AGGREGATOR_DEBOUNCE_SECONDS` — media/caption coalescing window; default `1.5`.
- `CC_TELEGRAM_AGGREGATOR_MAX_ATTACHMENTS` — per-bundle attachment cap; default `10`.
- `CC_TELEGRAM_MAX_ATTACHMENT_SIZE_BYTES` — document download cap; default `20971520`.
- `CC_TELEGRAM_CONTEXT_PCT_THRESHOLD` — context-% digest threshold; default `80`.
- `CC_TELEGRAM_CONTEXT_IN_MESSAGE_FOOTER` — per-turn token footer; default `true`.
- `CC_TELEGRAM_MESSAGE_REFS_RETENTION_DAYS` — provenance retention; default `30`.
- `CC_TELEGRAM_MESSAGE_REFS_DB_PATH` — SQLite path; default `$CC_TELEGRAM_DIR/message_refs.db`.
- `CC_TELEGRAM_MESSAGE_REF_TEXT_MAX_CHARS` — stored body cap; default `4000`.

## Migrate from old `ccbot` state

The runtime uses `~/.cc-telegram` only. It does **not** silently dual-read `~/.ccbot`.

Check state:

```bash
uv run cc-telegram doctor
```

Copy old state when needed:

```bash
uv run cc-telegram doctor --migrate
```

The bot start path refuses to run if `~/.ccbot` exists and `~/.cc-telegram` is missing, unless you explicitly set `CC_TELEGRAM_DIR`.

## Install the Claude Code hook

```bash
uv run cc-telegram hook --install
```

This writes/updates `~/.claude/settings.json` with:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "cc-telegram hook", "timeout": 5 }
        ]
      }
    ]
  }
}
```

Legacy `ccbot hook` entries are rewritten in place.

## Run

```bash
uv run cc-telegram
```

If installed as a tool:

```bash
cc-telegram
```

For day-to-day use, run it inside tmux or a process supervisor. The included helper assumes the default `cc-telegram` tmux session:

```bash
./scripts/restart.sh
```

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
```

## Repository layout

```text
src/cctelegram/                     core package
src/cctelegram/handlers/            Telegram interaction layer
  attention.py                      end-of-turn attention cards
  busy_indicator.py                 RunState machine
  inbound_aggregator.py             caption/media/photo+text bundler
  reply_context.py                  Telegram reply/quote → Claude context
  message_queue.py                  per-route FIFO worker
  message_sender.py                 safe send/edit/delete with MarkdownV2 fallback
  status_polling.py                 poll loop + typing-action loop
  interactive_ui.py                 AskUserQuestion / ExitPlanMode / permission UI
  directory_browser.py              directory + session picker
  history.py                        /history paginator
  cleanup.py                        centralized topic teardown
src/cctelegram/message_refs.py       SQLite provenance table
src/cctelegram/session_monitor.py    JSONL tail + TranscriptEvent dispatch
src/cctelegram/transcript_parser.py  JSONL → ParsedEntry / TranscriptEvent
tests/                              pytest suite
.claude/rules/                      architecture notes loaded by Claude Code
docs/plans/                         design notes and historical plans
```

## License

MIT — see [LICENSE](LICENSE).
