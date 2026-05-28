# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - Topic-based routing: 1 topic = 1 window = 1 session             │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture tmux pane as PNG                           │
│  - /esc: Send Escape to interrupt Claude                           │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code                                │
│  - Create sessions via directory browser in unbound topics         │
│  - Tool use → tool result: edit message in-place                   │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - Parse status line (spinner + working text)                      │
└──────────┬──────────────────────────────────────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (tmux keys)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴──────────────────────┐
│  SessionMonitor         │    │  TmuxManager (tmux_manager.py)  │
│  (session_monitor.py)   │    │  - list/find/create/kill windows│
│  - Poll JSONL every 2s  │    │  - send_keys to pane            │
│  - Detect mtime changes │    │  - capture_pane for screenshot  │
│  - Parse new lines      │    └──────────────┬─────────────────┘
│  - Track pending tools  │                   │
│    across poll cycles   │                   │
└──────────┬──────────────┘                   │
           │                                  │
           ▼                                  ▼
┌────────────────────────┐         ┌─────────────────────────┐
│  TranscriptParser      │         │  Tmux Windows           │
│  (transcript_parser.py)│         │  - Claude Code process  │
│  - Parse JSONL entries │         │  - One window per       │
│  - Pair tool_use ↔     │         │    topic/session        │
│    tool_result         │         └────────────┬────────────┘
│  - Format expandable   │                      │
│    quotes for thinking │              SessionStart hook
│  - Extract history     │                      │
└────────────────────────┘                      ▼
                                    ┌────────────────────────┐
┌────────────────────────┐         │  Hook (hook.py)        │
│  SessionManager        │◄────────│  - Dispatch by         │
│  (session.py)          │  reads  │    hook_event_name:    │
│  - Window ↔ Session    │  map    │    SessionStart →      │
│    resolution          │         │      write session_map │
│  - Thread bindings     │         │    PreToolUse(AUQ) →   │
│    (topic → window)    │         │      write auq_pending │
│  - Message history     │────────►│      side file         │
│    retrieval           │  reads  │  - Receive hook stdin  │
└────────────────────────┘  JSONL  └────────────────────────┘

┌────────────────────────┐         ┌────────────────────────┐
│  MonitorState          │         │  Claude Sessions       │
│  (monitor_state.py)    │         │  ~/.claude/projects/   │
│  - Track byte offset   │         │  - sessions-index      │
│  - Prevent duplicates  │         │  - *.jsonl files       │
│    after restart       │         └────────────────────────┘
└────────────────────────┘

Additional modules:
  screenshot.py               ─ Terminal text → PNG rendering (ANSI color, font fallback)
  transcribe.py               ─ Voice-to-text transcription via OpenAI API (gpt-4o-transcribe)
  main.py                     ─ CLI entry point
  utils.py                    ─ Shared utilities (app_dir, atomic_write_json)
  route_runtime.py            ─ Wave B per-route snapshot state machine (active when
                                CC_TELEGRAM_ROUTE_RUNTIME_V2=true). Subsumes
                                busy_indicator + status_polling._idle_state into a
                                lock-protected RouteRuntimeSnapshot interface.
  transcript_event_adapter.py ─ Translates session_monitor.TranscriptEvent →
                                route_runtime.TranscriptLifecycleEvent and fans out
                                per-route. 150-250 LoC budget (Wave B kill signal
                                at 250 — beyond that it's Transcript Stream).

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + rate_limit_send
  message_queue.py    ─ Per-user queue + worker (merge, status dedup)
  status_polling.py   ─ Background status line polling (1s interval)
  response_builder.py ─ Response pagination and formatting
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode / Permission UI
  directory_browser.py─ Directory selection + session picker UI for new topics
  cleanup.py          ─ Topic state cleanup on close/delete
  callback_data.py    ─ Callback data constants
  auq_ledger.py       ─ Wave 3 restart-safe write-ahead ledger for AUQ
                        option-pick dispatches. JSONL at auq_action_ledger.jsonl
                        keyed by (route_hash, fp8, opt). State machine:
                        accepted → digit_sent → dispatched (or
                        failed_before/after_digit terminals). ``lookup()``
                        returns raw rows; the **callback handler**
                        projects pre-restart accepted/digit_sent rows to
                        ``unknown`` (via ``process_start_time()``) so it
                        refreshes the card instead of re-dispatching.

State files (~/.cc-telegram/ or $CC_TELEGRAM_DIR/):
  state.json               ─ thread bindings + window states + display names + read offsets
  session_map.json         ─ hook-generated window_id→session mapping (SessionStart)
  monitor_state.json       ─ poll progress (byte offset) per JSONL file
  interactive_state.json   ─ persisted picker msg ids + AUQ context markers
                             (survives launchctl kickstart)
  auq_pending/<sid>.json   ─ PreToolUse side files for AskUserQuestion;
                             captures tool_input before Claude renders picker;
                             dir mode 0700, files mode 0600; kept across
                             multi-select toggles; cleaned on AUQ tool_result,
                             session replacement, or startup GC
  auq_action_ledger.jsonl  ─ Wave 3 append-only ledger of AUQ option-pick
                             lifecycle transitions (mode 0600). The callback
                             handler consults this BEFORE the in-memory token
                             table so a duplicate tap after process restart
                             returns "Action already received" instead of
                             re-dispatching the digit to tmux.
  message_refs.db          ─ SQLite provenance index for reply-context resolution
  log-archive/             ─ gzipped rotations (only if rotation LaunchAgent installed)
```

## Key Design Decisions

- **Topic-centric** — Each Telegram topic binds to one tmux window. No centralized session list; topics *are* the session list.
- **Window ID-centric** — All internal state keyed by tmux window ID (e.g. `@0`, `@12`), not window names. Window IDs are guaranteed unique within a tmux server session. Window names are kept as display names via `window_display_names` map. Same directory can have multiple windows.
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **PreToolUse(AskUserQuestion) side files** — the `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured `tool_input` to `auq_pending/<session_id>.json` before Claude renders the picker. The bot reads the side file at picker render time so each option's full description is visible in the Telegram context message immediately, before terminal completion. Side files are mode 0600 under a 0700 directory; multi-select `aqt:` toggles keep them alive, and cleanup happens when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when the session is replaced, or via startup GC. Bot logs a one-time warning if `PreToolUse` is missing from `~/.claude/settings.json`; `cc-telegram hook --install` reinstalls both hooks.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → window_id → session).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. Old state.json files keyed by window name are auto-migrated.
- **RouteRuntime concurrency contract (Wave B, behind `CC_TELEGRAM_ROUTE_RUNTIME_V2`)** — `route_runtime` exposes a single per-route state machine via `ingest_transcript_event(route, event)`, `mark_*(route)`, and `snapshot(route)`. Per-route `asyncio.Lock` serialises mutations within a route; independent routes do not serialise. Observers fan out **after** lock release, against the committed `RouteRuntimeSnapshot` (frozen, monotonic-seq-tagged). Pane snapshots (`mark_pane_idle`) are reconciliation events with lower authority than transcript lifecycle: they preserve `WAITING_ON_USER` and `BROKEN_TOPIC`, only clear `RUNNING` / `RUNNING_TOOL`. No new `register_*_callback` fan-out — that pattern (which produced bug c313657) is precisely what `RouteRuntime` replaces.
- **Restart-safe AUQ pick dispatch (Wave 3)** — option-pick callback_data carries a stable `(route_hash, fp8, opt)` triplet in addition to the opaque token: `aqp:<route_hash>:<fp8>:<opt>:<token>`. The triplet is the key into `auq_action_ledger.jsonl` (append-only JSONL ledger). The callback handler consults the ledger BEFORE the in-memory `_pick_tokens` table, so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of dispatching the digit to tmux twice. Authorization remains the in-memory token + owner check — the ledger is for *idempotency*, not authentication. v4 §7.2 contract: owner-mismatch lookups peek the live token map and fall through to the token path only when the clicker holds a live token reconstructing the same key (legitimate collision); otherwise return `WRONG_USER_PICK_TEXT`. Legacy `aqp:<token>` callbacks (rendered before deploy) still round-trip via the token-only path for one TTL window without ledger interaction.
- **AUQ multi-select toggles** — multi-select option buttons use `aqt:<route_hash>:<fp8>:<opt>:<token>` and route to the interactive executor. `aqt:` validates the live token/window/form, dispatches a bare digit to tmux with no Enter, then re-renders from the pane. Toggles are not ledgered and do not consume sibling tokens; final Submit/Cancel is reached by Tab on the Claude Code review screen and reuses the existing `aqp:` pick/ledger flow.
