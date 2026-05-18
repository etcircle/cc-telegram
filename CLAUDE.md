# CLAUDE.md

cc-telegram — Telegram bot that bridges Telegram Forum topics to Claude Code sessions via tmux windows. Each topic is bound to one tmux window running one Claude Code instance.

Tech stack: Python, python-telegram-bot, tmux, uv.

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/cctelegram/        # Type check — MUST be 0 errors before committing
uv run pytest -m scenario -q          # Scenario floor — black-box behavior tests at the public Telegram seam
bin/post-wave-check.sh                # Architecture deepening health diff (LoC, brittleness, tool status)
cc-telegram hook --install            # Auto-install Claude Code SessionStart hook
```

## Core Design Constraints

- **1 Topic = 1 Window = 1 Session** — all internal routing keyed by tmux window ID (`@0`, `@12`), not window name. Window names kept as display names. Same directory can have multiple windows.
- **Topic-only** — no backward-compat for non-topic mode. No `active_sessions`, no `/list`, no General topic routing.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit).
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text). Internal queue/UI code calls bot API directly with its own fallback.
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.
- **Scenario test floor** — `tests/scenarios/*.py` are black-box behavior tests at the public Telegram seam (`@pytest.mark.scenario`). They drive `Update` → real handler stack → fake tmux / fake bot, with no monkeypatch of handler internals in test bodies. Architecture changes must preserve these scenarios green.
- **RouteRuntime is the snapshot seam under `CC_TELEGRAM_ROUTE_RUNTIME_V2`** — Wave B introduces `cctelegram.route_runtime` and `cctelegram.transcript_event_adapter`. Mutations go through `ingest_transcript_event` / `mark_*`; reads come from `route_runtime.snapshot(route)`. Per-route `asyncio.Lock` only; no new `register_state_callback` / `register_activity_callback` fan-out (that pattern produced bug c313657 and is precisely what `RouteRuntime` replaces). `message_queue` remains the only sender/editor of status cards; it queries `snapshot.status_card_visible` and writes back via `mark_status_card_published(route, msg_id)` — if it ever needs to mutate `message_queue` internals beyond that, the plan's kill criterion fires (promote Route Outbox). The env var defaults to `false` during the ≥48h soak; production flips it manually and observes before the legacy deletion ships as a follow-up commit.

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Configuration

- Config directory: `~/.cc-telegram/` by default, override with `CC_TELEGRAM_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files: `state.json` (thread bindings), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).

## Hook Configuration

Auto-install: `cc-telegram hook --install`

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "cc-telegram hook", "timeout": 5 }]
      }
    ]
  }
}
```

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.
