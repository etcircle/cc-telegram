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
  route_runtime.py            ─ The sole per-route run-state / context-usage /
                                idle-clear authority. A lock-protected
                                RouteRuntimeSnapshot interface; owns RunState,
                                ContextUsage, IDLE_CLEAR_DELAY_SECONDS, and the
                                JSONL replay parser (parse_pending_tools_from_jsonl).
                                Also owns the lower-authority pane_interactive_pending
                                bit + mark_interactive_pending / mark_interactive_cleared
                                (PROMOTE an active RUNNING route → WAITING_ON_USER for a
                                buffered interactive tool_use; see the concurrency contract).
  transcript_event_adapter.py ─ Translates session_monitor.TranscriptEvent →
                                route_runtime.TranscriptLifecycleEvent and fans out
                                per-route. 150-250 LoC budget (kill signal at 250 —
                                beyond that it's Transcript Stream).
  md_capture.py               ─ Bug 2 MessageDisplay live-prose: bot-side reader.
                                Resolves the appender path + writes the bot-managed
                                `--settings` file (ensure_capture_settings), reads
                                the per-session NDJSON on demand and accumulates the
                                per-flush `delta`s into completed-prose ProseRecords
                                (read_prose_records — pull-only, no tailer/observer),
                                owns normalize_prose (the SINGLE dedup-parity
                                normalization shared with PR-C+D), and the lifecycle
                                (teardown_session / gc_stale). Imports utils only.
  _md_display_appender.py     ─ The MessageDisplay hook itself: a tiny stdlib-only
                                appender run directly by the interpreter (NEVER
                                imports the package — forceSyncExecution latency).
                                Keys the per-session file by Path(transcript_path).stem
                                (resume-safe), appends the raw payload as one NDJSON
                                line via a single O_APPEND os.write, always exits 0.

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + rate_limit_send
  message_queue.py    ─ Per-user queue + worker (merge, status dedup)
  status_polling.py   ─ Background status line polling (1s interval). Its
                        pane-absent AUQ-card clear gate consults
                        auq_source.side_file_live_for_window (the PreToolUse
                        side-file lifecycle authority) before tombstoning, so
                        an obscured pane (task-list overlay / scrolled Submit
                        screen) can't tear down a still-live question's card.
                        Also drives the pane-confirmed WAITING_ON_USER promotion
                        (mark_interactive_pending at SET sites a/b/d; site c is
                        bit-neutral), the mode-ended liveness reconciliation +
                        in-mode tombstone retract (mark_interactive_cleared), and
                        the per-tick digest repaint (_maybe_repaint_digest_on_transition
                        + the poller-local _prev_run_state dedup cache).
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
  md_hook_settings.json    ─ Bug 2 bot-managed Claude Code settings registering
                             the MessageDisplay hook; passed to bot-launched
                             sessions via `claude --settings` (NOT in global
                             ~/.claude/settings.json); merges with global hooks.
  msg_display/<sid>.ndjson ─ Bug 2 MessageDisplay live-prose capture; one per
                             session keyed by the transcript filename stem
                             (resume-safe); dir mode 0700, files mode 0600.
                             The appender appends each streaming delta; the bot
                             accumulates by MessageDisplay.message_id into
                             completed prose read at picker-render. Swept by a
                             1h startup GC; the teardown_session primitive is
                             wired to AUQ/EPM resolution / session replacement /
                             /clear / topic close in a later step (PR-C+D).
  message_refs.db          ─ SQLite provenance index for reply-context resolution
  log-archive/             ─ gzipped rotations (only if rotation LaunchAgent installed)
```

## Key Design Decisions

- **Topic-centric** — Each Telegram topic binds to one tmux window. No centralized session list; topics *are* the session list.
- **Window ID-centric** — All internal state keyed by tmux window ID (e.g. `@0`, `@12`), not window names. Window IDs are guaranteed unique within a tmux server session. Window names are kept as display names via `window_display_names` map. Same directory can have multiple windows.
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **PreToolUse(AskUserQuestion) side files** — the `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured `tool_input` to `auq_pending/<session_id>.json` before Claude renders the picker. The bot reads the side file at picker render time so each option's full description is visible in the Telegram context message immediately, before terminal completion. Side files are mode 0600 under a 0700 directory; multi-select `aqt:` toggles keep them alive, and cleanup happens when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when the session is replaced, or via startup GC. Bot logs a one-time warning if `PreToolUse` is missing from `~/.claude/settings.json`; `cc-telegram hook --install` reinstalls both hooks.
- **MessageDisplay live-prose capture (Bug 2)** — assistant free-text prose written in the same turn as an `AskUserQuestion` / `ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at resolution, so during a live prompt the prose is not on the bridge and the Telegram user would choose blind. Claude Code's `MessageDisplay` hook fires with each streaming `delta` BEFORE the picker blocks; a tiny stdlib appender (`_md_display_appender.py`, never imports the package — `forceSyncExecution` latency budget) writes each `delta` to `msg_display/<session>.ndjson` keyed by `Path(transcript_path).stem` (resume-safe: under `--resume` the JSONL is the original session's file the bot tracks, not the new hook-reported id). The hook is scoped to bot-launched sessions via a bot-managed `md_hook_settings.json` passed as `claude --settings` (merges with the global hooks; never in `~/.claude/settings.json`). The bot accumulates the per-flush deltas by `MessageDisplay.message_id` (no JSONL counterpart, so grouping is bot-side) into completed prose, read on demand at picker-render (`md_capture.read_prose_records` — pull-only, no tailer/observer; c313657 stays forbidden). `md_capture.normalize_prose` is the SINGLE normalization used for both the live `norm_hash` and the post-resolution JSONL dedup, so the two compare equal (mint/validate parity). The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin` marker (`BLOCK_ORIGIN_EXIT_PLAN`) through `ParsedEntry` / `TranscriptEvent` / `NewMessage` so dedup can group prose with its sibling interactive `tool_use` and exclude the synthetic ExitPlanMode plan text. The live-delivery surface, freshness gate, shown-live marker, and JSONL dedup land in PR-C+D; PR-B ships the capture + read + normalization + lifecycle primitives.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → window_id → session).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. The pre-2026-02-11 `window_name`-keyed `state.json`/`session_map.json` format is no longer migrated: any non-`@` legacy keys found on load are dropped with a one-shot per-map `logger.warning` (`window_states` / `thread_bindings` / `user_window_offsets` in `session.py`; `session_map` entries in `session_monitor._load_current_session_map`). The live SessionStart hook only ever emits `@N` keys.
- **RouteRuntime concurrency contract** — `route_runtime` is the sole run-state / context-usage / idle-clear authority, exposing a single per-route state machine via `ingest_transcript_event(route, event)`, `mark_*(route)`, and `snapshot(route)`. Per-route `asyncio.Lock` serialises mutations within a route; independent routes do not serialise. Reads come only from `snapshot(route)` — each mutation freezes a committed, frozen `RouteRuntimeSnapshot` and there is no push/observer channel. Pane snapshots (`mark_pane_idle` / `commit_pane_idle_clear`) are reconciliation events with lower authority than transcript lifecycle: they preserve `WAITING_ON_USER`, only clear `RUNNING` / `RUNNING_TOOL`. Pane signals may also **PROMOTE an active `RUNNING` route** (empty `open_tools`) to `WAITING_ON_USER` via `mark_interactive_pending` — fired by `status_polling` from a **pane-confirmed** live AUQ picker / ExitPlanMode plan-approval while Claude Code buffers the interactive `tool_use` in JSONL — retracted via `mark_interactive_cleared`. Strictly lower authority than the transcript (deriver checks `open_tools` first; the `tool_use` / known-`tool_result` / end-of-turn / user branches zero the `pane_interactive_pending` bit, plain-text/thinking and an unknown `tool_result` preserve it); never resurrects idle, seeds an unseen route, overrides `RUNNING_TOOL`, or clobbers a transcript-set `WAITING_ON_USER`. Cleared by the transcript reclaim, the poller's mode-ended liveness reconciliation (`interactive_window != window_id`) / in-mode tombstone, or route teardown — dropped wherever route_runtime state is cleared: `mark_session_reset` (`/clear`), the `inbound_telegram` stale-window unbinds (direct `clear_route`), and `clear_topic_state` → `route_runtime.clear_routes_for_topic(user, thread)` on topic-close / poller window-gone (route_runtime's OWN topic-teardown seam — NOT derived from `message_queue._route_queues`, so a queue-less route is torn down too). The digest header repaints on a run-state transition via the poller (`_maybe_repaint_digest_on_transition` → `message_queue.refresh_activity_digest_if_present`; pull-only, no observer). No `register_*_callback` fan-out — that pattern (which produced bug c313657) is precisely what `RouteRuntime` replaced. Topic-broken handling is the **reactive** path in `message_queue` (`_bad_topic_threads` / `_emergency_dm` / `_TOPIC_BROKEN_OUTCOMES` / `probe_topic_liveness`), not a run-state — there is no `BROKEN_TOPIC` run-state.
- **Restart-safe AUQ pick dispatch (Wave 3)** — option-pick callback_data carries a stable `(route_hash, fp8, opt)` triplet in addition to the opaque token: `aqp:<route_hash>:<fp8>:<opt>:<token>`. The triplet is the key into `auq_action_ledger.jsonl` (append-only JSONL ledger). The callback handler consults the ledger BEFORE the in-memory `_pick_tokens` table, so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of dispatching the digit to tmux twice. Authorization remains the in-memory token + owner check — the ledger is for *idempotency*, not authentication. v4 §7.2 contract: owner-mismatch lookups peek the live token map and fall through to the token path only when the clicker holds a live token reconstructing the same key (legitimate collision); otherwise return `WRONG_USER_PICK_TEXT`. The keyed `aqp:<route_hash>:<fp8>:<opt>:<token>` shape is the only one the callback handler parses; the pre-Wave-3 `aqp:<token>` legacy shape is no longer accepted (a stray 1-part callback falls through to the malformed `else` → "Card expired, refreshing.").
- **AUQ multi-select toggles** — multi-select option buttons use `aqt:<route_hash>:<fp8>:<opt>:<token>` and route to the interactive executor. `aqt:` validates the live token/window/form, dispatches a bare digit to tmux with no Enter, then re-renders from the pane. Toggles are not ledgered and do not consume sibling tokens; final Submit/Cancel is reached by Tab on the Claude Code review screen and reuses the existing `aqp:` pick/ledger flow.
