# Topic-first attention notifications

Date: 2026-05-01
Branch: `local/hermes-style-messaging` (commit `9c4192a`)
Owner: em.tanev@gmail.com

## 1. Problem

CCBot occasionally fails to surface "Claude is waiting for you" moments. The
recent hotfix on this branch papers over that by sending direct DMs when:

- An interactive UI (AskUserQuestion / ExitPlanMode / Permission) is detected
  (`handlers/interactive_ui.py::_notify_waiting_dm`).
- Final assistant text trips the `_looks_like_attention_request` heuristic
  (`handlers/message_queue.py::_attention_dm_if_needed`).

DMs lose all context: they are decoupled from the topic where the Claude
session lives, the user has to mentally map a free-form notification back to
"which window/topic", and the topic itself remains visually idle, so the user
keeps missing the attention signal in the place they are already reading.

The user wants topic-first notifications. DMs should be a strict
last-resort emergency channel.

## 2. Root diagnosis

### 2.1 Why notifications get lost in topics

- The interactive UI handler (`handlers/interactive_ui.py::handle_interactive_ui`)
  prefers `bot.edit_message_text` over re-send. **Telegram does not push
  notifications for edits**, so the topic stays cosmetically updated but the
  user gets no badge / sound. The DM was added to compensate.
- Final assistant text *is* sent as a fresh message in the topic, but if the
  topic chat is muted or backgrounded, the bell may not be loud enough,
  especially when buried under the editable activity digest (also edits-only)
  and a "🟡 Busy …" status (also edit-in-place). The user sees the digest
  count tick up but no separate "you're up" cue.
- There is no semantic "attention" message in the topic — only generic
  content/digest/status. The reader cannot scan and see "Claude is waiting"
  at a glance.

### 2.2 Why some topics genuinely fail

`launchd.err.log` shows persistent rejections for chat `-1003917381258`,
threads `10` (window `@3` etcircle-dev) and `2` (window `@4`
etcircle-dev-2):

```
ERROR - Failed to send message to -1003917381258: Message thread not found
```

Sequence:

- `bot.send_message(message_thread_id=10)` raises `BadRequest: Message thread
  not found`.
- `send_with_fallback` in `handlers/message_sender.py:54-84` catches *all*
  exceptions, retries once without parse_mode (still fails), then logs the
  generic `Failed to send message to {chat_id}: {error}` and returns `None`.
- `_process_status_update_task` / `_process_content_task` in
  `handlers/message_queue.py` see the `None`, call `_dm_fallback`, and mark
  the topic in `_bad_topic_threads` so subsequent sends DM-fall back too.

Two issues with this path:

1. **No operation-specific signal**: the log line is the same for every send,
   edit, delete, photo, and across status/content/digest/interactive paths.
   We cannot tell from logs whether it was a status edit on a closed topic,
   an emergency attention card, or a routine content message that failed.
2. **Topic-existence probe (`status_polling.py:130`)** only classifies
   `Topic_id_invalid` as "topic deleted". `Message thread not found` is a
   different Telegram error string and the probe never reaches that branch,
   so dead topics keep their bindings and keep failing every poll cycle.

### 2.3 Existing seams we want to keep

- `ActivityDigestState` and `_process_activity_task` already collapse the
  noisy tool/thinking traffic into one editable Hermes-style card per topic —
  this is the right scaffold for stage 4.
- `session_manager.resolve_chat_id` and `_delivery_target` already centralise
  topic-vs-DM routing.
- `clear_topic_state` in `handlers/cleanup.py` already exists as a single
  teardown point, used when a topic dies.

## 3. Proposed design

### 3.1 Topic-first attention card (the centrepiece)

Introduce a new per-`(user_id, thread_id)` artefact: the **attention card**.
One bold, audible message in the topic that means "Claude is waiting for
you". State machine:

```
       Claude becomes idle/finishes
                 │
                 ▼
   ┌─────────── IDLE ───────────┐
   │                            │
   │  send fresh card           │  user replies / interactive
   │  (sound on)                │  UI cleared / new tool starts
   │                            │
   ▼                            │
WAITING ─── edit card ──────────┘
   │
   │  state remains WAITING but
   │  prompt content changed
   │
   └── edit-only (silent)
```

Only the **idle→waiting** transition sends a new message (and therefore a
notification). All subsequent updates inside a single waiting episode are
edits, so the topic does not spam the user. When the user responds, the
card transitions back to idle: it is either edited into a compact "✅
acknowledged" trailer or deleted, depending on configuration.

Sources that drive transitions:

- `handle_interactive_ui` calls into the attention manager when a new
  interactive UI appears (or its prompt fingerprint changes after a quiet
  period). This replaces the current DM path.
- `_process_content_task`, when assistant text matches
  `_looks_like_attention_request`, calls into the attention manager. This
  replaces `_attention_dm_if_needed`.
- The activity digest finalisation hook (`_finalize_activity_digest`) and
  any user-reply handler (`bot.py` text handler) call
  `attention.dismiss(user_id, thread_id)` so the card flips back to idle.

The card body uses a stable, scannable format:

```
🔔 Claude is waiting for input — di-copilot
Tap to open the topic and respond.
[truncated 1-line prompt preview]
```

It is sent with `disable_notification=False` (default true-push), and lives
**alongside** the existing interactive keyboard message, not replacing it,
because the keyboard message stays in edit mode for keystroke routing.

### 3.2 Operation-specific topic send/edit/delete primitive

Add a thin wrapper in `handlers/message_sender.py`:

```python
class TopicSendOutcome(Enum):
    OK
    TOPIC_NOT_FOUND   # Message thread not found / Topic_id_invalid
    TOPIC_CLOSED      # Topic_closed
    FORBIDDEN         # bot kicked / no permission
    RATE_LIMITED      # 429 (re-raised, also reported)
    OTHER             # everything else
```

Wrappers:

- `topic_send(bot, user_id, thread_id, text, *, op: str, ...) -> (Message|None, TopicSendOutcome)`
- `topic_edit(bot, user_id, thread_id, message_id, text, *, op, ...) -> outcome`
- `topic_delete(bot, user_id, thread_id, message_id, *, op) -> outcome`

`op` is a short label (`"status"`, `"content"`, `"activity"`, `"attention"`,
`"interactive"`, `"tool_result"`) that is included in every log line and in
metrics. The wrapper:

- always uses `_delivery_target` so a known-bad topic keeps DM fallback.
- catches `BadRequest`, classifies via substring match on `e.message`, and
  returns the outcome plus the `Message`.
- logs `topic_send op=<op> user=%d thread=%s window=%s outcome=%s` — one
  structured line per operation. We never log a generic "Failed to send
  message to chat_id" again.

Existing `safe_send`/`safe_edit`/`send_with_fallback` stay for DM and
non-topic paths; they get refactored to share `_classify_bad_request`.

### 3.3 Topic repair before DM emergency

When `topic_send`/`topic_edit` returns `TOPIC_NOT_FOUND` or `TOPIC_CLOSED`:

1. **Reopen** if `TOPIC_CLOSED`: call `bot.reopen_forum_topic(chat_id,
   message_thread_id=thread_id)` once, then retry. Throttled per-topic to
   one attempt per N minutes.
2. **Recreate (rescue topic)** if `TOPIC_NOT_FOUND`: call
   `bot.create_forum_topic(chat_id, name=f"Rescue: {display}")`, rebind the
   thread (`session_manager.bind_thread(user_id, new_thread_id, window_id,
   display)`), unbind the old thread, and retry the send in the new topic.
   Throttled per-window.
3. **Emergency DM** only after the above has been attempted and failed.
   The DM contains: display name, window_id, original topic link, "rebind
   instructions" (`/rebind <new_topic>`). Strict per-topic dedupe + 5-minute
   cooldown.

The probe loop in `status_polling.py` is updated to recognise both
`Topic_id_invalid` and `Message thread not found` as "topic gone", and to
trigger the repair pipeline rather than only deleting the binding.

### 3.4 DM removal semantics

After this change, DMs are emitted only in three explicit cases:

- The attention manager attempted a topic send and the send classified as
  `TOPIC_NOT_FOUND` after both reopen + rescue topic creation failed.
- A content message from the queue worker exhausted the same path (so the
  user does not silently lose Claude output).
- A power-user explicit `/dm` command (existing behaviour, untouched).

Cooldown and dedupe are kept (and tightened — the existing 60s/300s
windows cause re-DM noise during a single broken episode). DM body always
contains the topic link if the topic still exists, and a one-line
recovery instruction otherwise.

### 3.5 Hermes-style activity digest, kept and tightened

The current digest (`_render_activity_digest`) is good. Two surgical
adjustments:

- When the attention manager flips to `WAITING`, the digest header gets a
  `🔔 awaiting input` annotation. This makes the digest itself a glanceable
  status and survives even if the attention card is suppressed.
- When the digest goes from "Busy" → "Done" *with* a fresh assistant text
  message that was an attention request, the digest is **not** finalised
  silently — the attention card is the explicit cue.

## 4. Implementation plan

Each stage is independently mergeable.

### Stage 1 — Topic send classifier + structured logs

Goal: deterministic, op-tagged logs and a single classification point
without changing user-visible behaviour yet.

Files:

- `src/ccbot/handlers/message_sender.py` (+ `TopicSendOutcome`,
  `_classify_bad_request`, `topic_send/edit/delete`).
- `src/ccbot/handlers/message_queue.py` — replace direct `bot.edit_message_text`
  / `send_with_fallback` calls in `_process_status_update_task`,
  `_process_content_task`, `_upsert_activity_digest`,
  `_convert_status_to_content`, `_do_send_status_message`,
  `_do_clear_status_message` with the new `topic_*` wrappers. Pass `op=`.
- `src/ccbot/handlers/interactive_ui.py` — replace inline send/edit/delete
  with `topic_*` wrappers, `op="interactive"`.
- `src/ccbot/handlers/status_polling.py` — extend the probe classifier to
  treat `Message thread not found` the same as `Topic_id_invalid`.

Tests / verification:

- New `tests/ccbot/handlers/test_topic_send.py`: parametrised tests over
  representative `BadRequest` messages → expected `TopicSendOutcome`.
- Update `tests/ccbot/test_telegram_sender.py` to assert that the new
  wrappers do not regress markdown/plain fallback.
- Manually tail `launchd.err.log`, force a bad thread (`reopen` a topic
  then close it), confirm a single line of the form
  `topic_send op=status user=… thread=… outcome=TOPIC_NOT_FOUND` per try.

Rollout: ship as one PR. No state migration. Behaviour-preserving.

Rollback: revert PR. Wrappers are pure shims.

### Stage 2 — Topic-first attention card

Goal: replace both DM paths with a topic-resident attention card driven by
an idle↔waiting state machine.

Files:

- New `src/ccbot/handlers/attention.py`:
  - `AttentionState` dataclass: `message_id`, `window_id`, `last_fingerprint`,
    `state: Literal["idle","waiting"]`, `last_send_at`.
  - `notify_waiting(bot, user_id, thread_id, window_id, prompt_text, *, kind)`
    — fingerprint is `sha1(window_id || kind || prompt[:1000])`. Idle→waiting
    triggers `topic_send` (audible). Same waiting episode triggers
    `topic_edit` (silent). On `TOPIC_NOT_FOUND`, hands off to repair (Stage 3).
  - `dismiss(user_id, thread_id)` — edits the card to a one-line "✅
    acknowledged" trailer (or deletes; toggleable via constant).
  - `clear(user_id, thread_id)` — used by `cleanup.clear_topic_state`.
- `src/ccbot/handlers/interactive_ui.py`:
  - Remove `_notify_waiting_dm`, `_interactive_dm_fingerprints`,
    `_interactive_dm_last_sent`, `INTERACTIVE_DM_COOLDOWN_SECONDS`.
  - Call `attention.notify_waiting(..., kind="interactive_ui",
    prompt_text=text)` when an interactive UI appears or its content
    materially changes.
  - Call `attention.dismiss(...)` from `clear_interactive_msg`.
- `src/ccbot/handlers/message_queue.py`:
  - Remove `_attention_dm_seen`, `ATTENTION_DM_COOLDOWN_SECONDS`,
    `_attention_dm_if_needed`. Replace its call site in
    `_process_content_task` with `attention.notify_waiting(...,
    kind="assistant_text")`.
  - On any non-attention text task or new tool task, call
    `attention.dismiss(...)`.
- `src/ccbot/handlers/cleanup.py`:
  - Add `attention.clear` to `clear_topic_state`.
- `src/ccbot/bot.py`:
  - In the user message handler (text inbound), after successfully writing
    keystrokes to tmux, call `attention.dismiss(user_id, thread_id)`.

Tests / verification:

- New `tests/ccbot/handlers/test_attention.py`: simulate idle→waiting (one
  send, audible), waiting→waiting with same fingerprint (no edit), waiting
  →waiting with new fingerprint (edit only), dismiss (delete or trailer).
- Update `tests/ccbot/handlers/test_interactive_ui.py` to assert no DM
  send call is made on the topic-OK path.
- Smoke: induce an `AskUserQuestion` in window `@5`/topic 184; confirm one
  fresh in-topic message with a 🔔 title arrives (notification fires),
  followed by silent edits if Claude redraws.
- Smoke: deliver an attention-grabbing assistant text in topic 378; same
  expectation.

Rollout: ship as one PR after Stage 1. No state migration. The DM code is
deleted, not feature-flagged — the desired behaviour is "no DMs unless a
topic genuinely fails", and Stage 3 covers the genuine-failure case before
this stage is exercised in production.

Rollback: revert PR; the code is local to three handlers and is removed
in one piece.

### Stage 3 — Topic repair pipeline + emergency DM

Goal: keep the user reachable when the topic is genuinely dead, while
recovering the topic itself when possible.

Files:

- New `src/ccbot/handlers/topic_repair.py`:
  - `try_repair(bot, user_id, thread_id, window_id, *, outcome) -> RepairResult`.
  - Per-thread reopen throttle (5 min), per-window rescue throttle (15
    min), persisted in memory.
  - On `TOPIC_CLOSED`: `bot.reopen_forum_topic`. On success, return
    `RETRY_SAME_THREAD`.
  - On `TOPIC_NOT_FOUND`: `bot.create_forum_topic(name=f"Rescue:
    {display}")`, rebind via `session_manager`, unbind old thread, return
    `RETRY_NEW_THREAD(new_thread_id)`.
  - If reopen/create fail (permission missing, etc.), return
    `EMERGENCY_DM(reason=...)`.
- `src/ccbot/handlers/message_sender.py`:
  - `topic_send`/`topic_edit` accept an optional `repair=True` flag (the
    default for content/attention/activity, off for the probe). When set
    and the outcome is `TOPIC_NOT_FOUND`/`TOPIC_CLOSED`, the wrapper calls
    `topic_repair.try_repair` and retries once with the resolved thread.
- `src/ccbot/handlers/message_queue.py`:
  - Existing `_dm_fallback` becomes `_emergency_dm`: only invoked when
    repair returns `EMERGENCY_DM`. Body includes:
    - display name + window_id;
    - topic link (best-effort, may be the dead one — still useful for
      group nav);
    - the `op=` that failed;
    - a single recovery hint ("/rebind <new_topic_id>" or "/help").
  - Drop the `_bad_topic_threads` permanent flag; rely on the throttled
    repair pipeline instead. A topic can recover after the user fixes it.
- `src/ccbot/handlers/status_polling.py`:
  - **No proactive Telegram probe.** The previous 60s
    `unpin_all_forum_topic_messages` probe was destructive on success
    (clears any user-pinned messages, not a no-op) and was removed.
    Topic existence is detected reactively from real `topic_send` /
    `topic_edit` failures classified into `_TOPIC_BROKEN_OUTCOMES`. If
    a non-mutating liveness probe is ever introduced, it must be a
    read-only Telegram method — never one whose successful result
    mutates topic state.

Tests / verification:

- New `tests/ccbot/handlers/test_topic_repair.py`: outcome → action
  matrix, throttle behaviour, idempotency under concurrent failures.
- Update `tests/ccbot/handlers/test_status_polling.py` to cover both
  `Topic_id_invalid` and `Message thread not found` paths.
- Manual: close topic 10 in Telegram; confirm the next `topic_send`
  reopens it and message lands. Delete topic 10; confirm a "Rescue:
  etcircle-dev" topic is created and bound, with the originally-intended
  message inside. Revoke the bot's `manage_topics` permission; confirm
  exactly one emergency DM lands per failure episode (cooldown holds).

Rollout: ship behind a settings flag (`CCBOT_TOPIC_REPAIR=1`, default
on). Document the new permission requirement (`manage_topics`).

Rollback: set `CCBOT_TOPIC_REPAIR=0` to disable repair (sends fall back
to emergency DM directly, same as today).

### Stage 4 — Activity digest tightening + cleanup

Goal: make the digest itself a clear waiting indicator and remove the
last attention-DM-shaped affordances now that the rest of the system is
topic-first.

Files:

- `src/ccbot/handlers/message_queue.py`:
  - `_render_activity_digest` adds a `🔔 awaiting input` line when
    `attention.is_waiting(user_id, thread_id)` is true.
  - `_finalize_activity_digest` no longer marks "Done" if the trailing
    assistant text is an attention request (the card is the cue).
  - Delete unused `_dm_fallback_seen`, `DM_FALLBACK_COOLDOWN_SECONDS`,
    `_attention_dm_seen`, `ATTENTION_DM_COOLDOWN_SECONDS` once Stage 2 +
    3 ship.
  - Inline `_looks_like_attention_request` cleanup: keep but extract to
    `attention.is_attention_request(text)` so both stages share it.

Tests / verification:

- Extend `test_attention.py` with digest-integration cases.
- Run `uv run ruff check src/ tests/` and `uv run pyright src/ccbot/` —
  must pass with zero errors before merge (per CLAUDE.md).
- Three-day soak on user's daily flow: target zero DMs in
  `launchd.err.log` for windows with healthy topics; `topic_send op=…
  outcome=OK` lines dominate.

Rollout: ship as cleanup PR after a soak window on Stages 2+3.

Rollback: PR-level revert; small, low-risk diff.

## 5. Risks and open questions

- **Edits do not push, sends do.** The whole stage 2 design assumes
  `bot.send_message` to a topic *does* trigger the user's notification
  even when the chat is muted. Verify with the user whether the group is
  globally muted; if so, we may need `disable_notification=False` plus a
  per-topic mute exception, which Telegram does not currently expose to
  bots. Fallback in that case: also send a "ping" reaction or use
  `send_message` with `protect_content` toggled to force a different code
  path. Open question for the user.

- **Permission: `manage_topics`.** Stage 3 `create_forum_topic` and
  `reopen_forum_topic` require the bot to be an admin in the supergroup
  with `can_manage_topics`. If missing, the repair pipeline degrades to
  emergency DM. Worth a one-time install check at bot startup that warns
  if the permission is missing.

- **Notification spam from idle→waiting flapping.** If Claude rapidly
  alternates "thinking" and "waiting" (e.g. multi-step plans), the
  attention card may re-send. Mitigate with a minimum dwell time
  (≥30s) before a fresh send is allowed; same fingerprint within that
  window stays an edit.

- **Telegram error string brittleness.** Classifier matches on substrings
  of `BadRequest.message`. Telegram occasionally rewords. Treat unknown
  `BadRequest` as `OTHER` and log it raw so we can extend the classifier.

- **`_bad_topic_threads` removal.** Some callers (`_delivery_target`)
  rely on this set for deterministic DM routing. Stage 3 replaces it with
  the repair pipeline. Verify that the polling loop and the worker do
  not race (two workers attempting to rescue-create the same topic). A
  per-window asyncio lock in `topic_repair` prevents this.

- **`/clear` and session_id changes.** When a session rotates, the
  attention card's last fingerprint should not survive across sessions.
  Wire `clear_topic_state` to also `attention.clear`.

- **Existing tests for `_attention_dm_if_needed` / `_notify_waiting_dm`.**
  None today. Adding tests in Stage 2 prevents regression but also means
  there is no reference behaviour to lock against — the design must be
  validated by the user against their preferred notification cadence.

- **Rescue topic name pollution.** Repeated topic deletions could leave
  a chain of "Rescue: di-copilot", "Rescue: Rescue: di-copilot", etc.
  Strip a leading `Rescue: ` before prefixing.
