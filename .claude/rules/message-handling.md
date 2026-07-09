# Message Handling

## Message Queue Architecture

Per-route message queues + worker pattern for all send tasks. A route is `(user_id, thread_id_or_0, window_id)`:
- Messages are sent in receive order (FIFO) **per route**
- Each route has its own worker, content queue, and latest-wins ephemeral slot
- Multi-user / multi-topic concurrent processing without interference

**Per-route status semantics**: Per-route workers drain the ephemeral slot
after every content task. Status updates are coalesced — only the latest
text per route survives between drains. Across routes, no global ordering
is enforced; each route's content-then-status order is independent of
others. (Under the previous per-user FIFO, status-after-content was a
global invariant; now it's a per-route invariant only, which is the
intended Stage 2 trade-off so a backlog in one topic doesn't delay
status / interactive prompts in another.)

**Message merging**: The worker automatically merges consecutive mergeable content messages on dequeue:
- Content messages for the same window can be merged (including text, thinking)
- tool_use breaks the merge chain and is sent separately (message ID recorded for later editing)
- tool_result breaks the merge chain and is edited into the tool_use message (preventing order confusion)
- Merging stops when combined length exceeds 3800 characters (to avoid pagination)

## Per-user output verbosity + post-turn digest collapse (plan v4)

`handlers/output_prefs.resolve(user_id)` is the single per-recipient
verbosity authority (stored `/settings` choice > explicitly-set legacy env
default > preset; a stored PRESET choice overrides the entire env layer).
Production default preset is `standard`; the TEST SUITE pins `verbose`
(≡ pre-settings behavior) in conftest so the scenario floor stays
today-shaped. Digest renderers take per-recipient line/snippet/live-line
budgets; quiet (`digest_card=False`) never creates digest state (including
the Agent counter path — images + attention-dismiss still fire).

**W1 collapse-on-done** (`digest_on_done`): at `_finalize_activity_digest`,
`summary` (default) collapses the activity card to ONE line — run-state
header (a post-turn 🔔 survives) + tool/sub-agent counts + duration, all
frozen on state at finalize so repaints are edit-stable; `keep` is today's
full card; `delete` removes the card via the cancellation-safe protocol:
both debounce schedulers shield the LOCK-HOLDING flush (a cancel only ever
lands in the sleep), the upsert re-checks tombstone + slot identity under
the lock before any send, and the finalize-delete takes the lock,
tombstones, deletes best-effort (a RetryAfter never wedges content), and
pops the slot — no resurrection by `refresh_activity_digest_if_present` or
the poller repaint. Restart-mid-protocol orphan = accepted residual
(digest state is in-memory, matching today's restart behavior).

**W2 sub-agent collapse** (`subagent_cards`): the sidechain's own
end-of-turn — a final visible text whose `MessageTask.stop_reason` (plumbed
from `NewMessage`) is end-turn — triggers the synchronous
`_collapse_subagent_digest` (cancel pending debounce, render the one-line
`↳ Sub-agent · xxx ✅ N tools` under the per-key lock, `last_text` =
collapsed render). `_finalize_activity_digest` is the BACKSTOP sweep for
empty-final sidechains (`lifecycle_only` end markers never reach the
display path). The collapsed slot is a tombstone: late re-detected blocks
never re-inflate the play-by-play; a new run has a new key. `off` never
creates a card. The 🤖✅ report message (full, expandable) is untouched at
every policy; sidechain keep-alive (Wave A) fires from session_monitor and
is unaffected. **Fix 5 (ISSUE-6): the Workflow sub-agent shape rides this
SAME contract** — it collapses on its own `end_turn`+`text` (path 1), via
the unchanged parent-finalize backstop (path 2), AND via a new deterministic
**route-FIFO close collapse** (path 3: the `<task-notification>` close marks
the bracket `closing`, `check_sidechain_updates` tails the final tail then
emits a `NewMessage(subagent_collapse_prefix)` → `enqueue_subagent_collapse`
→ a summary-gated `subagent_collapse` control task) that guarantees an
empty-final Workflow card collapses even when paths 1/2 can't fire.

## Status Message Handling

**Conversion**: The status message is edited into the first content message, reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: Background task polls terminal status for all active windows at 1-second intervals. Send-layer rate limiting ensures flood control is not triggered.

**Deduplication**: The worker compares `last_text` when processing status updates; identical content skips the edit, reducing API calls.

## Run-state and idle reconciliation

`route_runtime` is the **sole** run-state / context-usage / idle-clear
authority — a single per-route state machine that exposes immutable
`RouteRuntimeSnapshot` reads. Every mutation (`ingest_transcript_event`,
`mark_*`) acquires a per-route `asyncio.Lock`, applies the transition,
and freezes an immutable snapshot read via `snapshot(route)` (no
observer/push channel). Snapshot
fields: `run_state`, `open_tools`, `waiting_on_user_tools`,
`context_usage`, `last_event_at`, `idle_clear_at`, `pane_idle_clear_at`,
`typing_eligible`, `status_card_visible`, `status_card_msg_id`,
`interactive_pending`, `notification_pending`, `notification_set_at`,
`notification_generation`, `notification_clear_reason`, `background_agents`,
`background_only`.
The two
idle deadlines are distinct:
`idle_clear_at` is the run-state `IDLE_RECENT → IDLE_CLEARED` decay
(armed by a transcript end-of-turn), while `pane_idle_clear_at` is the
debounced "🟡 Busy" *card-clear* deadline (armed by `status_polling`
on a confirmed-idle pane via `arm_pane_idle_clear`, read back via
`pane_idle_clear_due`, committed by `commit_pane_idle_clear`; activity
re-arms/cancels it inside `ingest_transcript_event` /
`mark_inbound_sent`). The consumers — `typing_action_loop`, the
activity-digest renderer, and the status-card lifecycle in
`message_queue` — read only from `route_runtime.snapshot(route)`. The
shared types `RunState`, `ContextUsage`, and `IDLE_CLEAR_DELAY_SECONDS`
live in `route_runtime`.

**`message_queue` boundary** — `message_queue` remains the only
sender/editor of status cards. It owns `_status_msg_info[skey]` as the
send-layer cache but mirrors `mark_status_card_published(route, msg_id)`
/ `mark_status_card_cleared(route)` into `route_runtime` so the
snapshot's `status_card_visible` flag is accurate for external
consumers. If a change ever needs to mutate `message_queue` internals
beyond that boundary, the kill criterion fires — promote a Route Outbox
slice now.

**Pane-set `WAITING_ON_USER` (live AUQ / ExitPlanMode "🔔 Waiting on you")** —
Claude Code buffers the interactive `tool_use` (AskUserQuestion / ExitPlanMode)
in JSONL until the prompt resolves, so `route_runtime` never ingests it and the
route would otherwise stay `RUNNING` ("🟡 Busy" + false "typing…"). The
lower-authority `pane_interactive_pending` bit is a **derivation input** (NOT a
parallel `run_state`): the deriver folds it into the empty-`open_tools` branch
(`WAITING_ON_USER` if the bit else `RUNNING`), so the single committed
`run_state` flips and the digest header + `typing_eligible` follow. The mutator
pair: `mark_interactive_pending` PROMOTES an **active `RUNNING` route with an
empty `open_tools` set** (the only state where setting the bit derives a clean
pane-set `WAITING`; `RUNNING` does not imply empty — a user turn mid-tool leaves
a stale entry) and re-arms the pane-idle debounce; `mark_interactive_cleared` is
the sole programmatic retract (NO-OP against a transcript-set `WAITING`).
**SET is pane-confirmed only**, fired by `status_polling.update_status_message`
at the live-picker proof points — site (a) `ui_content` present, site (b)
`is_picker_anchor_visible`, site (d) first-render dispatch — so the bit is True
⟺ a pane-set `WAITING`. **Site (c) (`side_file_live_for_window`, obscured pane)
is BIT-NEUTRAL**: it preserves the card but never promotes, so the bit shares
the AUQ card's liveness boundary and a double-`--resume` sibling (whose pane
never shows the picker) is never falsely lit. **CLEAR** is: the transcript
reclaim (primary — the `tool_use`/known-`tool_result`/end-of-turn/user branches
zero the bit when the buffered turn flushes; plain-text/thinking and an
unknown-id `tool_result` preserve it); the poller **mode-ended liveness
reconciliation** in the `interactive_window != window_id` block (gap-free —
covers mode-popped / window-switch / ExitPlanMode-no-flush, no flush dependency);
the **in-mode tombstone** (`mark_interactive_cleared` alongside the
`clear_interactive_msg(tombstone=True)`); and route teardown — the bit is
dropped wherever route_runtime state is cleared: **directly** at the
`inbound_telegram` stale-window unbinds (`clear_route`) and via
`mark_session_reset` (`/clear`), and via `clear_topic_state` →
`route_runtime.clear_routes_for_topic(user, thread)` on topic-close /
poller window-gone. The topic seam is **route_runtime's own** — it drops every
route under `(user, thread)` and is NOT derived from
`message_queue._route_queues` (a route can carry run-state /
`pane_interactive_pending` via `mark_inbound_sent` / replay /
`mark_interactive_pending` with no queue worker, so a `_route_queues`-only
enumeration would strand it; hermes round-2 P2). The digest header repaints on a
run-state transition via the poller's `_maybe_repaint_digest_on_transition`
(seeds without an edit on first observation; fires
`message_queue.refresh_activity_digest_if_present` once per change, both
directions; backed by the poller-local self-healing `_prev_run_state` dedup
cache, torn down only in the window-gone path — popping it on the bot-less
interactive-clear seam would mask the post-clear repaint). Pull-only; no
observer channel (c313657 stays forbidden). The bot-less `_on_interactive_clear`
seam is UNCHANGED — it touches neither the bit nor `_prev_run_state`.

**Notification-set `WAITING_ON_USER` (Workflow / permission approval "🔔 Waiting
on you" — Wave B)** — the SECOND lower-authority derivation input,
`notification_pending`, beside the pane bit above. A Workflow/permission
approval gate blocks Claude WITH its (non-interactive) `tool_use` open and no
JSONL trace, so the route sat `RUNNING_TOOL` ("🟡 Busy") forever. The Claude
Code `Notification` hook writes `notify_pending/<session_id>.json`
(`{ts, window_key, generation, kind}` — NO message text);
`handlers/notify_source.py` is the trust boundary (HARD
`window_key == "tmux_session:window_id"` read predicate — a double-`--resume`
sibling never lights; schema + future-skew validation; deliberately NO
read-TTL). The poller consumes it at the TOP of the per-binding path
(`_consume_notification_signal`, BEFORE the transition repaint and the
adaptive capture gating — a capture-skipped tick still consumes, and a 🔔
transition repaints the digest the SAME tick). `mark_notification_pending`
returns a `NotificationMarkResult` that DRIVES the generation-guarded unlink
(committed-live → unlink AFTER the commit; redundant-transcript-waiting /
stale-unlinked → unlink; ignored-no-unlink → never unlink, never seed).
Deriver precedence: transcript-interactive open id > `notification_pending`
(over ANY open_tools, incl. the open Workflow id, or empty) >
`pane_interactive_pending` (empty only) > RUNNING_TOOL > RUNNING — the two
bits clear INDEPENDENTLY and the pane bit's contract is untouched. The ONE
idle exception: IDLE(pane) with a non-empty `suspended_tools` stash is
positive live proof the pane clear was false — the mark RESTORES the stash
and derives WAITING (the second stash-restore path). CLEAR: a transcript
`user` event unconditionally; `tool_result` / end-of-turn / task-notification
events only when their JSONL timestamp is strictly NEWER than
`notification_set_at` (None/older preserves — buffered pre-notification JSONL
must not re-hide the wait; a preserved bit at end-of-turn keeps WAITING
instead of idling; an unknown `tool_result` preserves). **Fix 1 (ISSUE-5 arm
A): plain assistant `text`/`thinking` narration NO LONGER clears the bit** —
a Workflow blocked on an approval gate narrates *while* blocked, and the
buffered-flush timestamp is not causal order vs the gate, so a newer
narration block must not bury the wait; the narration branches call
`_clear_notification_if_setat_invalid` (the corrupt `set_at=None` invariant
repair ONLY), never the causal `_maybe_clear_notification_by_ts`. The poller's
pane-RUNNING observation at a
capture taken strictly after `set_at + NOTIFY_PANE_CLEAR_MARGIN_S` (LEVEL +
margin, NOT an idle→active edge — the adaptive capture can skip the blocked
approval frame, so an edge requirement strands the bit when the last
pre-notification capture was already running; the blocked prompt replaces
the run chrome, so a status-active frame sufficiently after the hook fired
is positive proof the user approved, and the margin keeps a same-tick
capture of the pre-prompt frame from clearing early); the
`NOTIFY_TTL_SECONDS` (30 min) runtime TTL evaluated from the
SNAPSHOT every tick independent of side-file existence (pending-without-
set_at = invariant violation = expired); and route teardown
(`mark_session_reset` / `clear_route` / `clear_routes_for_topic`). Side-file
lifecycle: unlinked per the mark result, on session replacement / `/clear`
(OLD session id) / topic close, 24h startup GC with the injected
`is_live_session` conservative-skip. Pull-only; no observer (c313657 stays
forbidden).

**Fix #1 — `BG_RUNNING`: a background-agent heartbeat clears a §3.6
projected-busy 🔔 (the dominant 30-min typing-dark strand).** When the PARENT
foreground is idle and the only live work is a background agent, the §3.6
commit (`mark_notification_pending` on stored-idle + a live bg key) lights 🔔
and `typing_eligible` goes False — but the fast `PANE_RUNNING` clear requires
the PARENT pane observed RUNNING, which never happens (parent idle), so the
bit strands for the full 30-min TTL while the agent demonstrably works
(verified: route @4, 🔔 17:42→18:12 ttl-expiry, agent sidechain writing
throughout). Fix: `mark_background_agent_activity` clears the bit on a
heartbeat that is positive proof THAT agent's bg work resumed — the background
analogue of `PANE_RUNNING` (new reason `NotificationClearReason.BG_RUNNING`),
scoped by FOUR conjunctive gates: (1) **shape** — stored `run_state` is
`IDLE_RECENT`/`IDLE_CLEARED` (the §3.6 commit leaves stored state idle), so a
transcript- or pane-set WAITING and the foreground Workflow-approval
`RUNNING_TOOL` 🔔 are NEVER touched; (2) **sole-live-plain-key** — the live bg
set is EXACTLY the heartbeating key AND it is a plain `run_in_background` Agent
(not `wf-task:`). The 🔔 is a single route-level bit
with NO per-agent linkage, so a heartbeat is resume-proof ONLY for its own
agent; with >1 live key — sibling plain Agents, OR a Workflow whose DIR-WIDE
`*.jsonl` mtime collapses all its sub-agents to one key — a sibling's write
could clear a 🔔 that may be ANOTHER agent's genuine decision, so it FAILS CLOSED
(hermes review P1); (3) **strict-newer** `event_ts > notification_set_at` (a
buffered pre-notification flush fails closed, mirrors
`_maybe_clear_notification_by_ts`); (4) **margin** `_wall_now() > set_at +
NOTIFY_BG_CLEAR_MARGIN_S` (1.5s, the bg analogue of `NOTIFY_PANE_CLEAR_MARGIN_S`;
a same-tick pre-prompt frame fails closed). Stored state stays idle; the
projection (rule 3, live bg key) lifts the next freeze to RUNNING → typing on.
`_reconcile_decision_card` dismisses the audible card on `BG_RUNNING` (the
agent resumed) exactly like `PANE_RUNNING`. **Accepted residual (safety-bounded):
a 🔔 on a route with >1 live background agent (multiple plain Agents, or any
Workflow) is held to the 30-min TTL** — the runtime can't bind the route-level
🔔 to a specific agent (no per-agent linkage; the `kind` field does not carry an
agent id — a per-agent-binding limitation, NOT the type-space concern the Fix A
kind-gate below characterizes), so it conservatively never auto-clears when the
live set is ambiguous (the prompt stays discoverable on the pane). Pull-only; no
observer (c313657 stays forbidden).

**Fix A — the `idle_prompt` kind-gate at the notification trust boundary
(2026-07-08).** CC 2.1.204 fires a matcher-less `Notification` ~60s after EVERY
turn end (`notification_type: "idle_prompt"`, "Claude is waiting for your
input"). On a stored-idle route with live background keys the §3.6 commit turned
that nudge into a false "🔔 Waiting on you" + typing-dark + a spurious decision
card (the multi-leg orchestration failure). **2.1.204 characterization (rig,
supersedes the Fix-#1-era "kind field is unreliable" caveat for the type-space):
exactly TWO observed `notification_type` values — `idle_prompt` (the 60s idle
nudge) and `permission_prompt` (approval gates, tool-agnostic across
Bash/Write) — and `hook.py` stores it VERBATIM as the side-file `kind` (Wave B
schema; no hook change).** The gate lives at the POLLER consume seam
(`status_polling._consume_notification_signal`; `route_runtime` stays
kind-agnostic): a record with `kind == "idle_prompt"` is DROPPED —
generation-guarded unlink (as the stale/on-disk-TTL paths), INFO log, NO
`mark_notification_pending`, NO card. **Exact consume order (Hermes r1 P2):**
rec-None → runtime-TTL → on-disk-TTL unlink/return → **the idle_prompt drop** →
the same-generation reflected early-return → `mark_notification_pending`; the
drop sits BEFORE the same-gen return so a reflected same-generation idle record
cannot bypass it. **Fail-open for everything else:** `permission_prompt`, empty
`""`, and any FUTURE unknown kind keep today's full commit-or-stale path (the rig
could not exhaustively enumerate CC's type space; unknown-kind-commits preserves
approval-gate safety). Rationale: `idle_prompt` means "the turn ended and Claude
is at the input box" — the transcript end-of-turn already renders exactly that;
the notification BIT exists only for approval gates (Wave B design intent).
Disclosed residual: the reverse overwrite (an idle_prompt burying an unconsumed
permission_prompt in the latest-event-wins side file) would drop a real 🔔,
bounded to <1 poll tick and implausible ordering; the pane/TTL paths and the
pane-discoverable prompt remain. Pull-only; no observer (c313657 stays
forbidden).

**Notification clear-reason channel + durable decision card (ISSUE-5 Fix
3a/3b/3c/3d).** Every `notification_pending` True→False transition stamps a
typed `NotificationClearReason` (`USER` / `TOOL_RESULT` / `END_OF_TURN` /
`TASK_NOTIFICATION` / `INVARIANT` / `PANE_RUNNING` / `BG_RUNNING` / `TTL` /
`TEARDOWN`),
surfaced on the snapshot as `notification_clear_reason` (`_clear_notification_in_place`
takes a REQUIRED `reason`; `mark_notification_cleared(route, *, reason)` — the
poller passes `TTL` / `PANE_RUNNING`; reset to None on each fresh commit). The
🔔 now drives a **persistent, audible decision card** (`attention.notify_waiting(...,
kind="notification_decision")` → the "🔔 Claude needs a decision" header; NO
notification text stored — privacy). The poller posts it on `COMMITTED_LIVE`
BEFORE the side-file unlink, gated by `interactive_ui.has_interactive_surface`
(Fix 3d — never double-cards over a live AUQ/EPM surface; gate on the surface,
NOT the pane bit). `status_polling._reconcile_decision_card` runs at the END of
every consume: **retry-while-pending** (re-post idempotently while
`notification_pending`, so a transient first-post failure never strands the
route on the silent digest header); **KEEP** while cleared with reason
`END_OF_TURN` AND a live `background_agents` key still projects Busy (the
EOT-gap — a 🔔 raised by a Workflow's own approval gate survives the parent's
end-of-turn); **DISMISS** kind-aware (`attention.dismiss_if_kind(...,
kind="notification_decision")`) on every other reason. **EOT-gap grace (codex
P2):** the monitor applies the parent end-of-turn (clearing 🔔) DURING
`check_for_updates` but the same-batch Workflow launch (the bg key) only via
the later `apply_sidechain_activity` fan-out, so a reconcile can land in
between (bit cleared, bg key not yet visible) and dismiss prematurely — the
END_OF_TURN-with-empty-bg dismiss is therefore HELD for
`DECISION_CARD_EOT_GRACE_S` (poller-local `_decision_card_eot_grace` deadline)
so a lagging launch becomes visible; only after the grace elapses with still no
key (a genuine no-workflow end-of-turn) is it dismissed. **Dismiss audit (Fix
3c):** every generic display-layer `attention.dismiss` (`message_queue` ×4,
`interactive_ui` clear_interactive_msg, `inbound_telegram` user-reply) became
`dismiss_if_kind("interactive_ui")` so display-path cleanup / narration can
NEVER ack a `notification_decision` card — the decision card dismisses ONLY via
the reason-driven poller path (the genuine-user dismissal flows through the
route_runtime `user` clear → reason `USER` → reconcile). `AttentionState.set_at`
is a WALL stamp. Pull-only; no observer (c313657 stays forbidden).

**Background-agent projected Busy (GH #44 — typing + 🟡 while a
`run_in_background` agent works).** A background async agent keeps writing its
sidechain for minutes-to-hours after the parent's authoritative end-of-turn,
with its output visibly streaming into the topic — but sidechain blocks are
display-path `NewMessage`s, never lifecycle events, so the route used to
render idle (no typing) the whole time. The fix is a THIRD lower-authority
route_runtime input, `background_agents`, applied as a **snapshot-time
PROJECTION**: the stored `run_state` is never mutated on an agent's account;
the single snapshot builder lifts a stored-idle route with a live
(non-expired, non-tombstoned) key to a visible RUNNING — `typing_eligible`,
the digest header, and /dashboard all follow from the snapshot. Precedence:
a committed `notification_pending` projects WAITING_ON_USER above the lift
(user-action-needed beats machine-busy), and `mark_notification_pending` now
COMMITS on stored-idle + a live background key (the second idle exception
beside the pane-stash resurrect) so a 🔔 raised by the background agent's own
approval gate is never stale-dropped. **Keys** (always through
`utils.normalize_background_agent_key` — agentId == sidechain stem minus
`agent-` == task-id): `mark_background_agent_activity(route, key, max_ts)` is
the keyed Wave A successor (heartbeat + UNqualified pane-false-idle
resurrection preserved verbatim; a NEW key on a stored-idle route records
ONLY when `event_ts > last_assistant_turn_ended_at`, both non-None, strict —
a buffered pre-end-of-turn flush fails closed; active/WAITING recording is
unconditional but foreground-presumed); `mark_background_agent_launched`
registers `is_background=True` from the parent's async-launch tool_result so
the key survives the parent's end-of-turn regardless of sidechain batching.
It is fed by THREE launch sources the monitor collects on the parent parse
path, **each with its OWN anchoring — they are NOT uniformly structured** (the
round-1 fold-log correction): **(1) the plain Agent/Task `agentId`** — at the
LIVE monitor seam this branch is **PROSE-anchored ONLY**
(`extract_async_agent_launch_id` on the `agentId:` tool_result line); its
structured discriminator `async_agent_launch_id_from_meta` runs ONLY in the
startup reconciler, so LIVE Agent launch recording is NOT meta-drift-proof — a
CC version that drops/renames the prose line while keeping the structured
`agentId` silently stops live Agent launches from recording (a known,
disclosed drift surface). **(2) the Workflow `wf-task:<taskId>` bracket key**
— structured-PRIMARY (`workflow_launch_info_from_meta` over the entry-level
`toolUseResult`) with a WARNING-logged prose fallback. **(3)
(typing-unification T1.2, 2026-07-08) the background Bash
`backgroundTaskId`** — structured-ONLY
(`response_builder.background_bash_task_id_from_meta` over the tool_result's
entry-level `toolUseResult`; keyed on `backgroundTaskId` PRESENCE only — the
three async-launch META shapes are disjoint, so an Agent/Workflow meta returns
None here and a Bash meta returns None from the other two meta parsers; prose
NEVER lifts). The background Bash key is the **bare** task id
(no `wf-task:` prefix), so it EQUALS the completion `<task-notification>`
`<task-id>` — the launch/close key parity, with NO bracket (a background Bash
has no sidechain dir to heartbeat; it ages by the background TTL and closes on
its `<task-notification>`). A prose-only BASH launch announcement (structured
meta absent) NEVER lifts — the Bash-scoped, rate-limited (once per
tool_use_id) T1.6 drift WARNING fires instead. **Clears**: `mark_background_agent_done` on the agent's
own sidechain end-of-turn (lifecycle-only markers included) and on the
parent's `<task-notification>` task-id (extracted monitor-side, applied
after lifecycle dispatch). **Queue-shaped close lane (CC 2.1.198 OBSERVED
invariant, 2026-07-08):** when a background task completes while the PARENT is
BUSY, CC does NOT write a `type:"user"` delivery entry — it writes the
`<task-notification>` as a `{"type":"queue-operation","operation":"enqueue",
"content":<envelope>}` entry (the COMPLETION timestamp), then an
`attachment`/`queued_command` entry (same COMPLETION timestamp) which never
becomes a user entry. `transcript_parser.parse_entries` dropped both, so the
close never tombstoned and typing stranded to the 2 h `is_background` TTL. Fix:
the parser SYNTHESIZES a `lifecycle_only` user-text entry from the enqueue line
(top-level `content`, `utils.is_task_notification` gated — the SAME predicate
the adapter stamps with), so it rides the EXISTING extraction branch
(`rec.completed` → `mark_background_agent_done` + the Fix C resume-vs-done NET
in true transcript order + the `wf-task:` bracket close) identically to the
parent-idle user-entry shape; the `attachment` lane stays intentionally unparsed
(strictly redundant with the enqueue line — attachment-only delivery is a
documented UNSUPPORTED shape). The `queue-operation` line carries a **COMPLETION**
timestamp; the parent-idle `type:"user"` delivery a **DELIVERY** timestamp
(~74 ms later) — ts-qualified notification clears compare against whichever
event carries the clear. The startup reconciler scans read the SAME queue-op
lane (tx/plain-text only, so it can never mint a launch — the restart
false-relight fix). Older CC without queue-op lines degrades to the user-entry
path (no regression). The wall-clock heartbeat TTL (`_wall_now()`
injectable; expire-before-classify deletes a stale record before NEW/EXISTING
classification so a late None-ts batch can never relift) — **PER-KEY since the
typing-unification T2 split (2026-07-08): a foreground-presumed key
(`is_background=False`) ages by `BG_AGENT_TTL_SECONDS` (30 min, the original
heartbeat-staleness bound); a launched / post-turn background key
(`is_background=True`) is positive structured proof of a known-async task and
ages by the longer `BG_BACKGROUND_TTL_SECONDS` (2 h)** — applied at BOTH TTL
seams (`_live_background_keys` filter + `_expire_background_agents_in_place`)
via the shared `_bg_ttl_for(rec)` selector; the provenance-only foreground
prune at the authoritative end-of-turn (synchronous agents always finish
before their parent's turn ends — `is_background` keys are NEVER pruned); and
route teardown. Done keys are TOMBSTONED — reset only on a GENUINE user turn.
A task-notification user event (`TranscriptLifecycleEvent.is_task_notification`,
stamped by the adapter via the public `response_builder.is_task_notification`)
is machine-initiated: it counts as activity but preserves the pane bit, the
stash, and the tombstones, clears the notification bit timestamp-qualified
only, and RE-DERIVES with the preserved gates (never a forced RUNNING — the
`interactive_pending ⟺ pane-set WAITING` invariant holds). **typing-unification
T1.3 (2026-07-08):** on a STORED-idle route with empty `open_tools` and NO
preserved gate (no surviving notification bit, no pane bit, no suspended
stash), the task-notification branch now PRESERVES the stored idle instead of
re-deriving RUNNING — for a completing background bash/agent whose paired
`mark_background_agent_done` tombstone lands via the LATER bot fan-out, a
forced RUNNING would have no live key left to project idle again and would
strand typing until the parent's next end-of-turn; the preserve leaves a clean
idle snapshot so typing drops at close (the parent's own lifecycle events
re-light RUNNING if it actually wakes). A preserved gate still derives WAITING;
the branch is shared with Agent/Workflow task-notifications. The status CARD
stays pane-driven and may clear on the idle pane while the lift holds —
typing + digest/dashboard Busy are the contracted surfaces (recorded product
decision). Restart degradation: all in-memory; the stamp-None guard keeps
post-restart sidechain batches from lifting (no false Busy), so the route
renders idle until fresh parent activity. A background BASH specifically is
**not restart-relit** (typing-unification T1.4b): unlike the Workflow/Agent
startup reconciler there is no sidechain file to stat, so after a restart a
still-running background bash stays typing-dark until fresh parent activity —
the recorded GH #44 degradation shape, and the T2 window widening does NOT
change it. Pull-only throughout (no observer; c313657 stays forbidden).

**Background-only episode card ("labeled silence").** When the projection keeps
a PARENT-idle route Busy purely on live background keys — typing on, topic
silent (a background Bash has no sidechain to stream) — the topic looks frozen.
The snapshot exposes a DERIVED read field `background_only` (computed ONLY in
`_build_snapshot`: stored `run_state` idle AND the lift projected RUNNING on the
TTL-filtered `background_agents`; False whenever a committed
`notification_pending` outranks the lift to WAITING — the 🔔 decision card owns
that state — so the two never double-signal). The poller
(`status_polling._maybe_post_bg_only_card`, sited AFTER the window-gone return
and BEFORE the capture-gating early returns, so a capture-skipped tick still
posts/clears) posts ONE silent line per episode —
`⏳ Background work running (N task[s]) — the topic will resume when it
finishes.` (the count from `len(snapshot.background_agents)`) via
`message_sender.topic_send(plain=True, disable_notification=True)` to
`session_manager.resolve_chat_id(user, thread)`. Edge-triggered off the
poller-local one-shot `_bg_only_card_posted` cache (the `_prev_run_state`
precedent): post + set on False→True; clear the flag on True→False (a LATER
episode posts a fresh card — the card itself STAYS in history, v1: no
edit/delete). A failed send (`sent is None` / topic-shaped outcome) leaves the
flag UNSET so the next tick retries (idempotency is the flag, never the send; a
dead topic retries each tick — the attention-card tolerance). The `quiet` preset
(`output_prefs.resolve(user).digest_card` False) gets no card. Torn down beside
`_prev_run_state` at the window-gone pop and `clear_route_caches_for_topic`.
Pull-only; no observer (c313657 stays forbidden).

**Fix C (2026-07-08) — resume as the FOURTH launch source (relight a nudged
agent).** A `SendMessage` to an already-EXISTING background agent (the standing
multi-leg "nudge" pattern) resumes it, but its prior stop tombstoned the key AND
tombstones reset only on a GENUINE user turn (the machine-initiated parent wake
preserves them), so neither the launched key nor the sidechain-activity fallback
fired and the resumed agent ran fully dark. The FOURTH launch source closes it,
sharing the GH #44 `background_agents` machinery. **Discriminator
(structured-ONLY):** `response_builder.resumed_agent_id_from_meta(meta)` reads
the resume tool_result's entry-level `toolUseResult` (`{success, message,
resumedAgentId}`, verified real JSONL 2.1.204) — keyed on non-empty-str
`resumedAgentId` PRESENCE only, FOUR-WAY DISJOINT with the Agent/Workflow/Bash
meta shapes. The monitor's SendMessage-scoped tool_result branch records the id
into `ParentSidechainActivity.resumed`, a MAP `key -> resume_ts` (NEVER a bare
set — Hermes r3; the value is the resume tool_result's EVENT timestamp, never
wall time / a tick max). **`mark_background_agent_resumed(route, key,
resume_ts)`** (+ the seed-idle twin for an unseeded post-restart parent) POPS the
per-key done tombstone — the SECOND, KEYED exception to "tombstones reset only on
a genuine user turn" (a structured resume is positive per-key proof of new work
for exactly that agent; all OTHER keys' tombstones untouched) — then applies
`mark_background_agent_launched` semantics (`is_background=True`, survives the
EOT prune, 2 h TTL) and stamps `resumed_event_ts` on the record (max-monotonic
preferring parseable; an unparseable later ts never erases an older parseable
one). **The cross-file resume-vs-done resolution (Codex r3 cross-batch fold):** a
resume and a done for the same key can occur in EITHER order, and "done" has TWO
sources with DIFFERENT ordering guarantees, so `mark_background_agent_done`
carries a `BgDoneSource`: a **PARENT** `<task-notification>` done (same file as
the resume — the monitor already net-resolves a same-batch resume/done pair by
transcript order, dropping the loser from `.resumed`/`.completed`) tombstones
UNCONDITIONALLY; a **SIDECHAIN** end-of-turn done (a DIFFERENT file, no shared
order) is timestamp-gated on the RECORD's `resumed_event_ts` — it keeps the key
LIVE iff the record has a `resumed_event_ts` AND the end_turn ts (`SidechainTick.
max_end_turn_ts`, the max PARSEABLE end-turn ts, kept STRICTLY separate from
`max_event_ts` activity) is NOT strictly newer (a stale prior-leg end_turn, this
batch or ANY later one, ≤ resume → LIVE); it tombstones on a strictly-newer
end_turn (genuine fast-finish), on a MISSING record / no `resumed_event_ts`
(plain-launch, byte-identical to today), or on any unparseable end_turn ts
(`SidechainTick.end_turn_ts_unparseable`, fail-closed to DONE — false dark is
annoying, false typing after completion is the historical bug class here). The
bot fan-out applies launched → **resumed(map)** → activity → done (sidechain,
then parent) so a same-tick resume is never blocked by the tombstone its own
batch is popping. Close parity holds with ZERO new close code: the resumed
agent's next stop emits a `<task-notification>` whose task-id == agentId == the
key → the existing parent done re-tombstones; multi-leg agents cycle
launch→done→resume→done… correctly. TTL edge (must-have 5): resume → record
TTL-expires → a stale sidechain done tombstones (accepted — the runtime already
judged the agent too silent) → a LATER resume pops the tombstone and relights
(expiry never permanently poisons future legs). Workflow resumes are out of
scope (one-shot). Restart: a mid-leg resumed agent is not restart-relit beyond
the existing Fix-#5 reconciler's original-launch scan. Pull-only; no observer
(c313657 stays forbidden).

**Fix B (2026-07-08) — true typing cadence.** `status_polling.typing_action_loop`
already fans out its per-route typing sends CONCURRENTLY (`_typing_action_tick` →
`asyncio.gather(return_exceptions=True)`), but the old loop slept a FULL
`TYPING_ACTION_INTERVAL` (3.0s) AFTER the tick, so start-to-start cadence was
`tick-elapsed + INTERVAL` (measured 6-12s live vs Telegram's ~5s typing TTL → the
indicator blinked). The loop now MEASURES each tick and sleeps
`max(TYPING_TICK_FLOOR_S, INTERVAL - elapsed)` (`_typing_sleep_delay`; the 0.1s
floor keeps a chronically over-interval tick from hot-looping — Hermes r1 P3), so
the cadence holds at `INTERVAL` regardless of sweep cost; a tick that overruns the
interval triggers a rate-limited WARNING (`_maybe_warn_typing_overrun`, once per
60s — the future-regression observability hook). The per-iteration body is
extracted (`_typing_action_tick`) for direct-drive tests; the concurrency is a
PRESERVATION pin. Send-layer only; no run-state / route_runtime interaction. The
send-layer group-bucket exemption (`TypingAwareRateLimiter`, see § Rate Limiting)
completes this true-cadence contract for multi-busy-topic forums — without it the
concurrent per-route typing sends re-serialize behind the 20/60s group bucket.

**Workflow-tool bracket (ISSUE-6 — extends GH #44 to the `Workflow` tool).**
GH #44 only detected the `Agent` tool's `run_in_background` (`agentId:` launch +
single-level `subagents/agent-*.jsonl` glob); the `Workflow` tool has a
DIFFERENT shape (subagents one level deeper at `subagents/workflows/wf_*/`, a
launch tool_result with `Task ID:` mid-line and a separate `Run ID`, and a
`<task-notification>` close keyed by the Task ID), so a Workflow run rendered
idle (no typing). The fix reuses the SAME `background_agents` machinery via a
**parent-transcript bracket** keyed `wf-task:<task_id>` (passes
`normalize_background_agent_key` as identity — no `agent-` prefix — so it never
aliases the Agent/Task namespace). **Launch anchor = STRUCTURED-primary (PR-2):**
the launch parse reads the ENTRY-level `toolUseResult`
(`{status:"async_launched", taskId, runId, transcriptDir, …}`, plumbed onto the
tool_result `ParsedEntry` as `tool_result_meta` by `transcript_parser`) via
`response_builder.workflow_launch_info_from_meta` — the robust,
version-drift-proof source; `transcriptDir` IS the validated `wf_dir` (no
run-id-topology derivation, no glob). It keys on the Workflow fields (`taskId`),
NEVER on `status` alone — the Agent/Task `run_in_background` async launch ALSO
carries `status=="async_launched"` but a DIFFERENT shape (`agentId`, no
`taskId`; verified 54-vs-40 in the JSONL history) and must return None.
`response_builder.extract_workflow_launch_info` (regex `(?im)^.*\bTask ID:\s*…` —
Task ID is MID-LINE, verified against real launches; the captured id ==
the `<task-notification>` close key, the open/close parity invariant) is the
PROSE FALLBACK, used ONLY when the structured field is genuinely ABSENT
(`tool_result_meta is None`: older Claude Code / a future whole-field rename /
a non-dict coerced to None) and logged with a WARNING for drift detectability.
A PRESENT structured dict that does not parse as an async_launched Workflow is
AUTHORITATIVE — the prose is NOT consulted (so a stale/quoted `Task ID:` line
can't open a bogus bracket; hermes P2). NOTE: this structured-primary anchor is
the LIVE-MONITOR path only — the PR-1 startup reconciler
`_scan_workflow_launches_and_closes` (below) stays PROSE-only by design (a
disclosed follow-up: widening its `Task ID` byte-prefilter to `async_launched`
to read the structured field there would JSON-parse the common Agent
async-launch lines and turn one malformed line into a fail-closed no-lift for an
unrelated live Workflow). `session_monitor` adds the raw
`wf-task:<id>` to `.launched` (→ `mark_background_agent_launched`,
`is_background=True`, survives the parent end-of-turn prune → typing + 🟡) and
opens a persistent `_WorkflowBracket`. **Fix 2c heartbeat (DESIGN B — separate
channel):** each poll, `_emit_workflow_bracket_heartbeats` stats the bracket's
`wf_dir` for the freshest `*.jsonl` mtime and emits a `wf-task:<id>` refresh
into `ParentSidechainActivity.bracket_heartbeats` (→ `mark_background_agent_activity`)
ONLY on an mtime ADVANCE (real new sidechain writes) — never by parsing
sidechain ENTRIES (run-state consumes only the bracket + a dir stat); no new
writes → the key ages out via the background heartbeat TTL
`BG_BACKGROUND_TTL_SECONDS` (2 h post the T2 split — a launched `wf-task:` key
is `is_background=True`; the dead/never-completed backstop); a `wf_dir`-less
bracket never heartbeats (ages out one TTL from `launch_wall`). **Close =
GATE-ON-BRACKET ONLY:** the
`<task-notification>` emits the `wf-task:<id>` close key (→
`mark_background_agent_done` tombstone) IFF a live open bracket exists — never
guessing a Workflow id from its character set; an isolated close with no
bracket has no route_runtime key to tombstone, so the bare key suffices. The
close is caught in BOTH observed CC 2.1.198 shapes: a parent-idle
`type:"user"` delivery (DELIVERY timestamp) AND — new (2026-07-08) — a
busy-parent `queue-operation`/`enqueue` entry (COMPLETION timestamp) that the
parser now synthesizes into the same `<task-notification>` user-text entry, so
the `wf-task:` bracket closes even when the parent was busy at completion (the
same extraction branch fires for both; the startup scan reads the queue-op lane
tx-only too).
Out-of-order done-before-launch fail-closes (the done tombstone no-ops the
later launch). The bracket is now MARKED `closing` (not popped immediately) so
the Fix 5 display path tails its `wf_dir` one final time before teardown (see
below); `_emit_workflow_bracket_heartbeats` skips closing brackets. This
`wf-task:` key is ALSO what makes ISSUE-5 arm B fire: a stored-idle route with
a live `wf-task:` key lets `mark_notification_pending` re-commit (§3.6) instead
of STALE_UNLINK, so a 🔔 raised by the Workflow's own approval gate is durable.

**BUSY restart reconciler (PR-1 Half B — re-arm typing + 🟡 + ↳ from the
filesystem after `launchctl kickstart`).** All the bracket / `background_agents`
state above is IN-MEMORY, so a restart of a still-running Workflow renders the
topic idle until a fresh parent turn — the owner's highest-frequency symptom.
`session_monitor._reconcile_workflow_brackets_on_startup(current_map)` runs ONCE
in `_monitor_loop` startup (beside `_hydrate_ask_tool_input_cache`, before the
poll loop): for each tracked parent with NO live open bracket (idempotency —
skip a parent that already has one), STAT-glob
`<project>/<parent_sid>/subagents/workflows/wf_*` (anchored, never `rglob`) and,
for any `wf_*` dir whose freshest `*.jsonl` mtime is within
`_RECONCILE_FRESH_WINDOW_S` (7200s post the T2 split — it mirrors
`BG_BACKGROUND_TTL_SECONDS`, the `is_background` TTL the reconciler's launched
Workflow/Agent keys age by, WITHOUT importing route_runtime), recover its Task
ID + close-state from ONE bounded
parent-JSONL scan (`_scan_workflow_launches_and_closes` — the
`_auq_tool_result_present` byte-prefilter pattern, matching the launch's Run ID /
Transcript-dir basename to `wf_dir.name`; fail-closed `({}, set())` on any read
error). **Three-state rule:** (1) task_id recovered + NO `<task-notification>`
close → LIFT: reopen a `_WorkflowBracket` (steady-state heartbeat + Fix-5 ↳
display resume) AND emit the raw `wf-task:<id>` into
`_parent_activity(sid).launched` — the bot fan-out
(`apply_sidechain_activity` → `route_runtime.seed_idle_and_mark_background_agent_
launched`) SEEDS the unseeded parent route IDLE and lifts it to projected
RUNNING (the B1-FIX: a bare `mark_background_agent_launched` would no-op on the
unseeded route); (2) close FOUND → NO runtime lift (a Workflow that finished just
before the deploy must not false-relight) — open a DISPLAY-ONLY `closing` bracket
for the final ↳ tail + collapse, then it's dropped; (3) task_id UNRECOVERABLE /
scan failed → DO NOT LIFT (fail-closed — prefer dark-until-next-turn over a false
🟡). STAT-only discovery (the parent JSONL is read ONLY when a fresh `wf_*` dir
exists — the cost-bound property), a per-tick `_RECONCILE_MAX_WF_DIRS` cap (16),
and the whole pass try/except-guarded so it can never break startup. No-reflood:
a reopened bracket's sub-files resume from the persisted `monitor_state.json`
offset and a first-seen post-restart file starts at EOF
(`_track_and_emit_sidechain_file`), so pre-restart ↳ blocks never replay. The
steady-state idle-route re-scan (B3b) is deferred — the startup pass covers the
post-kickstart symptom. Pull-only; no observer.

**Fix #5 — the reconciler ALSO re-lights plain `run_in_background` Agents.** PR-1
Half B covered only Workflows (`subagents/workflows/wf_*`); a plain background
Agent (sidechain `subagents/agent-*.jsonl`, one level UP) ran dark across a
kickstart. `_reconcile_agents_for_parent(session_id, jsonl_path, now)` runs for
EVERY tracked parent (independent of the Workflow block + its bracket-idempotency
continue): STAT-glob `subagents/agent-*.jsonl` (non-recursive — Workflow
sub-agents are a different glob), fresh-mtime filter (`_RECONCILE_FRESH_WINDOW_S`)
+ a `_RECONCILE_MAX_AGENT_FILES` (16) cap newest-first, then ONE bounded parent
scan (`_scan_agent_async_launches_and_closes`, a SEPARATE `b"agentId"` byte
prefilter so a malformed Agent line can't fail-close an unrelated Workflow).
**STRUCTURED-PRIMARY discriminator** (`response_builder.async_agent_launch_id_from_meta`
reads the entry-level `toolUseResult` `{status:"async_launched", isAsync:True,
agentId}` — version-robust, mirrors the Workflow PR-2 precedent + the TUI-drift
warning), with the prose `agentId:` line (`extract_async_agent_launch_id`, tool_result
lane only) as FALLBACK. **Three-state** (mirrors Workflow): STATE 1 fresh + agentId
in the async-launch set + NO `<task-notification>` close → emit the PLAIN `<agentId>`
launched key (the bot fan-out seeds the route IDLE + lifts to projected RUNNING; NO
bracket — the live ↳ + keep-alive already run via the top-level agent glob); STATE 2
close found → no lift; STATE 3 not async-launched (sync / unrecoverable) → no lift
(fail-closed). **NO persisted-`tracked_sessions` idempotency skip** (the design-review
break): an Agent already tracked before the kickstart is the DOMINANT case and MUST
re-light — the launched key + seed are idempotent and no-reflood is handled by the
display path's EOF/offset registration. Pull-only; no observer.

**Fix 5 (ISSUE-6 owner decision #2 — SHIPPED): the `↳` sub-agent DISPLAY cards
for Workflow sidechains.** A Workflow's sub-agents live one level deeper at
`subagents/workflows/wf_<runid>/agent-*.jsonl`, so a single-level glob missed
them. `check_sidechain_updates` adds a SECOND, anchored
`bracket.wf_dir.glob("agent-*.jsonl")` enumeration over THIS parent's OPEN
brackets (the SAME `wf_dir` the heartbeat stats — one shared discovery), driven
through the shared `_track_and_emit_sidechain_file(..., feed_run_state=False)`
helper so Workflow sidechain ENTRIES NEVER feed run-state (the `wf-task:`
bracket + mtime heartbeat stay the SOLE Workflow run-state input — `ticks` stays
empty, `route_runtime`/`apply_sidechain_activity`/`_finalize_activity_digest`
UNCHANGED). The tracking key is run-id-qualified `sub:<parent>:<runid>:<stem>`
(two concurrent runs under one parent never collide on a same-stem agent file;
keeps the `sub:<parent>:` teardown prefix; `_short_subagent_id`'s
`rsplit(":", 1)[-1]` lands on the `agent-<id>` stem so the rendered header is
identical to an Agent/Task card). DISPLAY ONLY — these cards ride the existing
per-recipient `subagent_cards` gating + the W2 collapse-on-done, identically to
the Agent/Task shape (path 1 = the agent's own `text`+`end_turn`; path 2 = the
unchanged parent-finalize backstop). PLUS a THIRD, **deterministic close
collapse on the route FIFO** for the empty-final case (a Workflow agent ending
lifecycle-only never self-collapses and may have no later parent finalize):
the `<task-notification>` marks the bracket `closing` (not popped);
`check_sidechain_updates` tails the closing bracket's `wf_dir` ONE final time
(final display tail), THEN appends a `NewMessage(subagent_collapse_prefix=
"sub:<parent>:<runid>:")` AFTER the cards, THEN pops the bracket;
`bot.handle_new_message` routes that marker to
`message_queue.enqueue_subagent_collapse(route, prefix)` → a
`task_type="subagent_collapse"` route-FIFO control task that the per-route
worker runs AFTER the run's content tasks (the cards exist when it fires) →
the summary-gated `collapse_subagent_cards_with_prefix` (early-returns on
`keep`/verbose — the play-by-play stays live — and `off` has no slot). The
control task is ordered + retryable like content (`_RETRYABLE_TASK_TYPES =
{"content", "subagent_collapse"}` at the three `_run_with_retry` flood/retry
gates) so a flood-control window or a `RetryAfter` during the collapse's own
edit never silently drops it (the collapse is idempotent). Discovery is
bracket-gated (live only) and anchored (never `rglob`); restart degrades in
lockstep with run-state (in-memory brackets ⇒ no cards until a fresh launch
re-opens a bracket). Pull-only; no observer.

**Interactive-surface teardown is PARENT-only (sidechain blocks never tear
down a live card).** `bot.handle_new_message` clears a live interactive card
on the parent route via two seams: the explicit AUQ `tool_result`
invalidation (`forget_ask_tool_input` + `auq_ledger.release_window`) and the
generic *"any non-interactive message ⇒ interaction complete"* teardown
(`if has_interactive_surface(user, thread): clear_interactive_msg(...);
forget_ask_tool_input(wid)`). Both are now GATED on `msg.subagent_key is None`,
mirroring the interactive-HANDLING branch at the top of the loop and the
routing-bypass intent in `session_monitor`'s sidechain emit (*"those apply
only to the parent's own blocks"*). A sidechain / background-agent block is
emitted with the PARENT's `session_id` and a non-None `subagent_key`
(`"sub:<parent>:…"`, `session_monitor.py:1599-1614`), so it resolves to the
parent's route; without the gate, a background Workflow/Agent narrating while
the parent is BLOCKED on a live prompt tore the card down — `clear_interactive_msg`
`topic_delete`-s the picker and `forget_ask_tool_input` pops the by-window
`_auq_context_posted` dedup marker, so the 1 Hz poller re-detects the
still-live pane prompt and re-posts (the 2026-06-23 DiCopilot ~28× ctx-card
duplication; the EPM `📋 Plan` re-post twin via `md_capture.teardown_session`).
`has_interactive_surface` is route-keyed + UI-type-agnostic, so one gate covers
AUQ + ExitPlanMode + Permission. The day-one (v0.1.0) asymmetry — handling
branch gated, teardown branch not — was a dead branch until sidechain DISPLAY
emission became unconditional (`ef086f1`, 2026-06-11) and was extended to the
Workflow sidechain shape by Fix 5. The gate must NOT widen to skip GENUINE
parent blocks: a parent non-interactive block (`subagent_key is None`) after a
bypassPermissions auto-resolution still legitimately tears the card down (the
regression-pinned case). Every prior AUQ-churn fix lived in `status_polling` /
`interactive_ui` / `auq_source` (the *poller's* re-render heuristics); the
re-post is the poller faithfully re-detecting a real live picker, so only this
upstream `bot.py` gate — never a poller-side change — stops the marker-pop that
re-armed duplication. Pull-only; no observer (c313657 forbidden).

**AUQ card-liveness authority (pane is lower authority than the
lifecycle)** — `status_polling`'s pane-absent clear gate must not tombstone
an AskUserQuestion card on visible-pane absence alone. The visible tmux pane
is only a *display*: a Claude task-list overlay, a scrolled/compressed
multi-step Submit screen, or tool-output spam can push the picker/Submit
anchors out of the captured pane while the question is still genuinely
pending on the Claude side (2026-05-31 @4/msg48427 — a live multi-select
card was tombstoned after the task-list overlay defeated both pane
predicates for 3 polls). The lifecycle authority is the PreToolUse side
file `auq_pending/<session>.json`, queried via
`auq_source.side_file_live_for_window(window_id)` (presence + schema +
future-skew, **deliberately NOT** the 5-min read-TTL and **NOT** the
pane-consistency check — a live-but-unanswered AUQ has not "expired on the
other side of the bridge", and `resolve_record` cannot be used because it
needs a pane-parsed form that is `None` under exactly the obstructing
overlay). While the side file is live the gate refreshes/keeps the card
and never enters the absent-streak countdown; the card is cleared only by
the genuine resolution (`tool_result` → `forget_ask_tool_input` unlinks the
side file), a window switch, a topic close, or the 1h startup `gc_stale`.
**Orphan reconciliation** — an *answered* AUQ whose side file was never
unlinked would keep the liveness probe `True` forever and strand a *dead*
card (the inverse failure the TTL-drop must not introduce). Two paths close
it: (1) **at the source** — `bot.handle_new_message` runs the AUQ
`tool_result` `forget_ask_tool_input` (which unlinks the side file) *before*
the awaited `clear_interactive_msg`, so a raise in the card clear can't
orphan it; (2) **on startup** — the monitor advances its byte offset inside
`check_for_updates` before the callback runs, so a crash/down-bot at that
moment leaves an orphan that path (1) can't catch;
`session_monitor._hydrate_ask_tool_input_cache` reconciles it on startup: for
each bound session whose JSONL shows **no pending AUQ**
(`_find_latest_pending_auq` is `None`) it unlinks any live side file via
**`side_file_live_for_session(session_id)` keyed on the same `current_map`
session it then unlinks** — never the window-keyed wrapper, whose `peek →
window_states` lookup can disagree with `current_map` at startup (checking one
source while unlinking another is the mint/validate parity trap). So presence
again tracks genuine liveness. Off-contract limitation: the
side file is keyed by *session*, so under a double-`--resume` of one session
into two windows a dead card on the sibling can linger (bounded by the
tool_result fan-out + window-switch + topic-close + 1h GC + the startup
reconciliation); a `tool_use_id` correlation would not help (the JSONL
`tool_use` / `_last_auq_tool_use_id` and the side file's `tool_use_id` are
typically unavailable during the live window), but a schema-v2 side file
carrying the hook-captured `window_id` could discriminate — deferred as
off-contract.

**Pick-token deadline refresh (D3-β — a live card's tokens track its OBSERVED
lifetime).** `pick_token._PICK_TOKEN_TTL_SECONDS = 300.0` bounds MEMORY only, not
correctness: a user can leave a live AUQ picker open for tens of minutes to
hours, and the old assumption that the token TTL outlives the picker was false —
a long idle pruned the option token out from under a still-on-screen card, so
the first tap hit `peek_none` and the handler *refreshed instead of
dispatching* (the dead-first-tap). Fix: at EVERY live-card-preserve branch where
`status_polling` resets the absent-streak and returns without re-rendering
(same-hash idle, `is_picker_anchor_visible` Submit, `side_file_live_for_window`
preservation), the poller calls `await
pick_token.refresh_route_deadlines(user, thread, window,
min_remaining_s=_DEADLINE_REFRESH_MARGIN_S)`. It re-stamps each live, non-expired
token within the margin of its deadline by REPLACING the frozen `PickTokenEntry`
with `expires_at = now + TTL` — **same token string, fingerprint, source tags,
and `row_generation`**, so the keyboard stays byte-identical (`MESSAGE_NOT_MODIFIED`,
no churn) and `_commit_phase_c`'s generation logic is untouched. It never
resurrects an already-expired token (the `now < expires_at` guard) or a
tombstoned row (`consumed_generation is None`), gated on the same liveness
authorities the clear-gate trusts; a genuinely-abandoned card's tokens still
prune at 300s. A fresh mint prunes prior-generation non-tombstoned rows for the
route so the refresh only keeps the CURRENT card alive. Pull-only (rides the 1 Hz
poll; no observer — c313657 forbidden). The residual cases — a restart (in-memory
tokens wiped) or a liveness-gate false-negative — degrade to the honest
`_refresh_pick_card` MODAL "↻ Refreshed — tap your choice again." (D3-α,
`show_alert=True` at the `peek_none`/`expired` callsites only; the ledger-state
callers keep their specific non-modal warnings).

**Source-drift re-mint (item 1 — a live card's TOKENS track its OBSERVED SOURCE;
the D3-β sibling).** D3-β keeps the token *deadlines* fresh but PRESERVES the
minted *source tags* (`dataclasses.replace(entry, expires_at=...)`). So a
single-select picker left open >300s ages its PreToolUse side file past the
read-TTL, `resolve_auq_source` flips `side_file`→`pane`, and the same-hash idle
branch — which only `refresh_route_deadlines` and returns — keeps the stale
`side_file` tokens. The user's first tap then hits `validate_and_consume`'s
source check → `source_drift` (swallowed + a misleading "Form changed,
refreshing."; self-heals on the 2nd tap via the existing source_drift re-render).
Fix (item 1): the read-TTL is **UNTOUCHED** (it stays the orphan time-bound —
nothing about side-file trust/lifetime changes), and the poller's same-hash idle
branch, BEFORE `refresh_route_deadlines`, re-resolves
`resolve_auq_source(window, None, pane)`, parses the live form via
`resolve_ask_form` (added to `status_polling`'s imports — the poller had only
`ui_content`, not a parsed form, and the parse also gates out non-AUQ panes like
the /model Settings picker), and compares the displayed card's minted
`(source_kind, source_fingerprint)` — read via the PURE, tombstone-aware
`pick_token.peek_route_source` — against the live source. On a mismatch it
re-renders via `handle_interactive_ui` (re-mint to the CURRENT source) instead of
refreshing deadlines, so the first tap dispatches. **Route-based lookup (the
item-1 P1 fix):** production mints a side_file card at the SIDE-FILE form's
fingerprint (the side-file dict carries the question TITLE), but after the side
file ages out the poller can only see the PANE form, whose
`current_question_title=None` on single-select panes — so the side-file-form and
pane-form fingerprints DIFFER (verified `3f00e2a2…` side-file vs `d24b9db9…` pane
on `auq_single_select_with_affordances_*`). The earlier fingerprint-keyed
`peek_route_source` therefore MISSED the row and never detected the drift. The fix
looks the displayed card up by ROUTE (`user, thread or 0, window`) across ALL
fingerprints — `mint_row`'s stale-row hygiene drops every OTHER non-tombstoned
row for a route on each fresh mint, so there is AT MOST ONE live card row per
route and the search is unambiguous (0 or, defensively, >1 live rows → None).
**Loop-safe (exactly ONE re-mint):** the drift re-mint fresh-mints `pane` and the
hygiene drops the old side_file-fp row, so the next tick finds the single pane row
→ live `pane` == minted `pane` → no further re-render.
`peek_route_source` skips TOMBSTONED rows (`consumed_generation is not None`) so a
just-consumed card is never falsely drifted into a re-render of a dead card. Being
fingerprint-agnostic, the route-based lookup also fixes the MULTI-question shape
(a pane fingerprint that shifts on ageout no longer hides the row). Pull-only
(rides the 1 Hz poll; no observer — c313657 forbidden). Residuals (all safe): a
≤1-poll-cycle boundary race at the 300s ageout (one tap routes through the
existing source_drift re-render, the 2nd dispatches); and a scrolled pane (visible
options start >1) where the re-mint drops the keyboard (`p14_suppress_picks`).

**Pane↔pane drift is a no-op (the di-copilot long-open-card churn fix — Fix A).**
The "next tick sees live `pane` == minted `pane` → no further re-render"
loop-safety above held ONLY for the `side_file`→pane flip, where both
fingerprints hash the SAME capture. For a pane↔pane comparison they do NOT: the
poller resolves `live` from a `scrollback=0` pane capture, while the card's pane
token was minted by `handle_interactive_ui` from a `scrollback=500` capture, and
the two `_pane_fingerprint`s differ PERMANENTLY for a busy/scrolled long-open AUQ
(the 500-line scrollback recovers options the 0-line visible pane lost). So a
`bail_aged` AUQ (side file aged past the 300s read-TTL → `kind=pane`) re-minted
EVERY ~1s tick forever — a per-tick in-place re-edit that periodically timed out
and recreated the card (the duplicate-card churn the owner saw in di-copilot).
Fix: `_remint_on_source_drift` now SHORT-CIRCUITS (returns False, no re-render)
when `minted[0] == "pane" and live.kind == "pane"` — a pane↔pane "drift" is just
capture noise, never a real source change (there is exactly ONE source when no
side file / `jsonl_cache` exists; the resolver itself documents the pane kind can
never legitimately `source_drift`). `_remint` stays armed for the genuine
`side_file`→pane / `jsonl_cache`→pane FLIP (`minted kind != "pane"`), so item-1
is untouched. RED-first: `test_same_hash_pane_to_pane_drift_does_not_remint`
(+ the existing `side_file`→pane drift tests stay green).

**Transient edit-outcome KEEPS the card (the churn's visible trigger — Fix B).**
The ~1Hz interactive re-edit (whether from the source-drift loop above or any
busy-topic re-render) periodically TIMES OUT against Telegram
(`telegram.error.TimedOut` → `_classify_bad_request` → `TopicSendOutcome.OTHER`).
`handle_interactive_ui`'s edit gate previously accepted only `OK` /
`MESSAGE_NOT_MODIFIED` and treated everything else as "edit failed → fresh send",
deleting the old card and sending a new one — a new message + notification PER
timeout (the user-visible spam; ~37 re-creates/hour on a 99-minute AUQ). Fix: a
transient `OTHER` / `RATE_LIMITED` edit outcome now KEEPS the existing card and
returns (the next poll re-edits in place); ONLY `MESSAGE_NOT_FOUND` (provably
gone) and the topic-broken outcomes (`TOPIC_NOT_FOUND` / `TOPIC_CLOSED` /
`FORBIDDEN`, which must reach the send-failed DM escalation) fall through to the
delete-old + send-new path. Mirrors the dashboard self-heal rule (`dashboard.py`
— never re-send on a transient, or the still-live message orphans; hermes Wave C
review P2-2). Behavior-narrowing (strictly FEWER sends) so it can never increase
Telegram traffic. **Residual (P3, visual-only):** the poller advances the
published render hash BEFORE the `handle_interactive_ui` edit (a concurrency
guard), so if a transient edit in the genuine *new-UI* branch is KEPT (not
recreated), that one render transition's visual update is dropped until the next
genuine UI change (the same-hash branch won't retry it). Never a wrong dispatch
(tokens / keyboard / pane-validated dispatch unaffected), and a strict
improvement over the recreate-churn it replaces. RED-first:
`TestInteractiveEditTransientOutcomeKeepsCard` (incl. the topic-broken
fall-through case).

**Render-only rescue resolver + render-identity loop kill (PR-3 PR-B — the busy
long-card render + duplicate-card loop).** A long-description AUQ in a BUSY topic
rendered BROKEN and SPAMMED duplicate "📋 details" cards every ~20s: the live tmux
pane mis-parsed / churned while the PreToolUse side file held the real question,
and the render path was gated behind a successful pane parse (so the side-file
rescue + the 📋 card were dropped exactly when needed), while the 1 Hz dedup hash
over the raw interactive-content excerpt CHURNED as scrollback scrolled under the
picker → a fresh re-render every tick. PR-A fixed the parser mis-parse; PR-B fixes
the render path + the loop. `auq_source.resolve_auq_source_for_render(window_id,
pane_text, explicit)` is the RENDER-path resolver (DISTINCT from the strict
`resolve_auq_source` that `validate_and_consume` + `_remint_on_source_drift` still
use UNCHANGED). It reads the side file READ-TTL-FREE then decides: **side_file_ok**
— side file consistent with the pane AND within the 300s read-TTL → render from it
+ mint TRUSTED tokens (the ONLY trusted side-file path; the `within_ttl` gate makes
it mirror the TTL'd strict resolver `validate_and_consume` re-resolves, so
mint/validate parity holds and a long-open card flips cleanly to `bail` at the TTL
boundary instead of stranding a trusted token the TTL'd validate rejects — no
dead-tap, and `_remint_on_source_drift` stays loop-safe because render's trusted
decision still agrees with the strict resolver it compares against); **bail** — the
pane is itself a COMPLETE coherent picker (`pane_form_is_complete_picker`) that
disagrees with the side file → a genuinely different / advanced live question →
render the PANE (trusted; never serve the stale side file); **rescue** — the pane
is unparseable / incomplete (busy scrollback) and the side file is the truth →
render the side file's full content DISPLAY-ONLY (`dispatch_trusted=False`, PURE
`build_form_from_tool_input` form — no pane overlay so the render identity can't
leak pane/scrollback churn); **explicit_jsonl / jsonl_cache / pane** — no side file
→ the pre-existing fallback (all trusted). `dispatch_trusted` GATES token minting
at the `_build_pick_button_rows` callsite: ANY untrusted render (rescue OR a
partial-pane bail) mints NO `pick_token` / `pick_intent` rows, calls
`prune_for_route` UNCONDITIONALLY — BEFORE the `p14_suppress_picks` skip, since an
untrusted partial bail is also p14 (hermes round-2: leaving a stale trusted token
row would make `_remint_on_source_drift` see minted≠live every tick → the very
re-render loop this PR kills; the trusted path self-prunes via `mint_row`'s
stale-row hygiene) — and adds a manual-nav notice (a busy/partial-pane digit can't
be verified against the live picker → would dead-tap). The ctx
(📋 full-descriptions) card is driven off the decision: side_file_ok / rescue post
the side file's descriptions (rescue is the V1/V2 fix — the card was previously
DROPPED because `resolve_record`'s pane-consistency check rejected on the busy pane);
**bail posts NO stale side-file card**. **Loop kill:** both `status_polling` dedup
hash sites (`_ui_render_hash`) hash the render IDENTITY for AskUserQuestion
(`auq_source.peek_render_identity` = the render decision + `render_signature` over
the render/keyboard-determining form fields — tabs, is_free_text, select_mode,
is_review_screen, options_complete, current_tab_inferred, len(questions),
`current_question_title`, and per-option number/label/cursor/selected/recommended)
instead of the raw interactive-content excerpt. `render_signature` uses
`current_question_title` ONLY — NEVER `pane_walkback_title` (scraped from the
churning scrollback above the option block; folding it in re-rendered the
title-less `bail`/`pane` card every tick, the dominant live single-select shape —
internal-review regression catch). This mirrors `_canonical_repr` and the OLD
`ui_content.content` hash, both of which excluded the title region above the
picker block, so the identity stays STABLE under scrollback churn (a rescue's
pure side-file form has no pane fields; a complete picker's parsed form ignores
scrollback above it) yet changes on every GENUINE transition (cursor move,
multi-select toggle, tab advance, review screen, complete↔incomplete,
JSONL-title, free-text, tab-inference loss). NEVER the cursor-blind pick-token
`fingerprint()` (the renderer paints the `❯` cursor + `selected` glyphs, so a
cursor/selection change MUST re-render — a separate render-only signature).
Non-AUQ interactive UIs (ExitPlanMode / permission) keep the raw-content hash.
**Disclosed residuals (all untrusted-display, never a wrong dispatch).** (1) The
≤1-poll-cycle boundary race at the 300s ageout (unchanged from item-1) — a
side_file_ok token minted just before the TTL and tapped just after it (before the
poller re-mints to `bail`/pane) routes through the existing source_drift
re-render and the 2nd tap dispatches; PR-B does not worsen it (it cleans the
>300s STEADY state, where render now picks `bail`→pane matching the strict
validate resolver). (2) A `rescue` renders the side-file question even if the side
file is STALE relative to a genuinely-different INCOMPLETE live pane (the OLD path
showed the partial live pane). Bounded — the PreToolUse hook overwrites the side
file on every AUQ, so the common sequential case stays fresh; staleness requires a
double-`--resume` sibling (session-keyed side file), a restart orphan, or a hook
write lag. dispatch_trusted=False (no buttons) so it is wrong-DISPLAY only, and it
is strictly better than the pre-PR-3 broken render (a raw scrollback blob); the
loop-kill FREEZES the rescue card so it self-corrects only when the side file is
overwritten / the pane becomes a complete picker. (3) A multi-question `rescue`
renders Q1 (`build_form_from_tool_input` defaults to the first question) even if
the live picker is on an advanced tab — only reachable when the pane is so
degraded its `←…→` tab header is unparseable (else PR-A → bail/side_file_ok with
the inferred tab); untrusted, and the 📋 ctx card still enumerates ALL questions.
Pull-only; no observer (c313657 forbidden).

**Restart re-dispatch (D2 — the durable mint-intent net for the case D3-β can't
cover).** D3-β keeps a live card's tokens alive only while the process is up; a
bot **restart** wipes the in-memory `_pick_tokens`/`_pick_token_cache`, and the
published card keeps its old keyboard with dead token strings, so the first tap
hits `peek_none` for the card's whole remaining life. D2 persists the per-token
mint intent at the fresh `aqp:` single-select/Submit render to a new leaf store
(`pick_intent.py` → `pick_intent.jsonl`; `aqt:` toggles excluded) and the
`peek_none` / `expired` callback branches call `_attempt_pick_recovery` →
`pick_token.recover_and_consume` to re-dispatch that tap. It is the **idle net's
sibling, not its overlap**: recovery fires ONLY on **positive proof of in-memory
loss** — no `_pick_token_cache` row at the reconstructed
`(user, thread_or_0, window, full_fingerprint)` cache_key (a live row ⇒ the normal
`validate_and_consume` path owns it; a tombstoned row ⇒ this process just consumed
it) — so an idle-kept-alive token (D3-β) never enters recovery. Recovery is
**row-scoped single-use** (a `_recovery_row_reservations[cache_key]` for concurrent
sibling taps + a per-sibling action-ledger guard for the restart-durable /
crash-between-`accepted`-and-tomb case + a `consume_row` tomb for hygiene), adds
the full **owner + `reject_stale_window_callback`** auth pair the `peek_none`
branch historically lacked plus a callback-payload parity check vs the stored
intent, and re-validates **read-TTL-free** source parity
(`auq_source.read_side_file_for_recovery` comparing `_canonical_dict_fingerprint`,
NOT the 12-hex `input_fingerprint`; pane fallback only when the side file is
genuinely gone via `side_file_live_for_session`). The `accepted` claim is written
at the reconstructed ledger key INSIDE the row reservation (no release-then-claim
gap; a re-check of the cache-row + sibling proofs precedes it), and the action
ledger stays the **24h durable single-use authority** — `pick_intent.jsonl` is a
SEPARATE token-keyed store (writing recovery state into the latest-wins action
ledger would clobber a `dispatched` row). The store is **NOT a `route_runtime`
field** — render-path write, callback-path read, pull-only, no observer (c313657
forbidden). Tombed at `forget_ask_tool_input` (AUQ/EPM resolution + the `/clear`
race via the OLD-window `forget_ask_tool_input(wid)` call) and `clear_topic_state`;
orphan-safe via the recovery-time form/source re-validation + the 24h GC.
Off-contract residual (safe DECLINE, never a wrong dispatch): a `jsonl_cache`-minted
card DECLINES (its in-process getter is wiped on restart). The form fingerprint is
now cursor-blind on **every** screen — `AskUserQuestionForm._canonical_repr` omits
the per-option cursor bit UNCONDITIONALLY (not just when `is_review_screen`), and
`auq_source._pane_fingerprint` hashes the SAME `_canonical_repr` so the pane source
fingerprint collapses in lockstep. The cursor-blind fingerprint stays load-bearing
under the v2.1.168 navigate-to-target dispatch (the bot MOVES the cursor to the
target before committing, so the form identity must not shift as the cursor moves —
else the nav-verify re-parse would no longer match the minted fingerprint and every
pick would bail). A moved cursor — Submit↔Cancel on the review screen OR any option
on a non-review picker — no longer rotates the pick token (live OR across a
restart), and D2 recovery SURVIVES a moved cursor on **every** screen (**the former
D3-γ non-review DECLINE is RETIRED**). Both the live and recovery Submit guards
share the cursor-blind `AskUserQuestionForm.review_submit_dispatchable`
predicate (anchored on `is_review_screen` + option #1 + the literal
`REVIEW_SUBMIT_LABEL` "Submit answers" + the minted label; verified on Claude Code
v2.1.161/.167/.168). The `_pane_fingerprint` ⇄ `_canonical_repr` shared-canonical
coupling is load-bearing for this fix — a refactor giving the pane source its own
fingerprint basis would re-break it; the fingerprint-EQUALITY-across-cursor-move
tests (for BOTH the review screen and non-review pickers) guard the coupling.

**AUQ pick dispatch NAVIGATES the cursor to the target, VERIFIES, then Enter
(v2.1.168 model — single-select `aqp:` + review Submit/Cancel ONLY).** On Claude
Code v2.1.168 a richer "notes side-panel" picker variant makes a bare digit only
MOVE the cursor (no select), so the form sticks and the bot would wrongly record
`dispatched` → an "Action already received" hard lock. Fix: `_dispatch_pick`
(shared by the live `aqp:` pick path AND D2 recovery) finds the live `❯` cursor in
`current_form`, computes `delta = target − cursor.number`, sends `Down`/`Up` ×
|delta| (`send_keys(enter=False, literal=False)`, MONOTONIC — never a wrap
shortcut, each return-checked), waits `NAV_SETTLE` (0.5s), re-parses to VERIFY the
cursor landed on the target (same cursor-blind `fingerprint` + `vc.number ==
target` + `_loose_label_match(vc.label, minted_label)` + the
`review_submit_dispatchable` anchor for Submit), presses `Enter` (`enter=False,
literal=False` — the version-stable commit, True in every variant), waits
`COMMIT_SETTLE` (0.5s), re-parses, and records `dispatched` ONLY after
`_classify_advance` confirms the EXACT expected transition (a positive forward
advance / resolution — over-advance, wrong-tab, no-flip all fail CLOSED). Ledger
non-success states: a **pre-commit bail** (`cursor_unknown` / `nav_send_failed` /
`verify_failed` — Enter provably never sent) records `not_advanced` and the
callback **falls through** (a fresh-token re-tap re-validates against the live
form; safe because nothing was committed); once `Enter` is sent an unconfirmed
advance (`commit_unconfirmed` / `confirm_capture_failed` / `confirm_parse_failed` —
a parse-fail with picker markers still present is AMBIGUOUS, never `dispatched`)
records `commit_unconfirmed` and the callback **refreshes-only, never
auto-redispatches** (no re-tap can re-send the commit key). The bare digit + the
`auq_ledger` `digit_sent` / `failed_*_digit` states are now **legacy-only** (kept
for on-disk compat). The nav `⏎ Enter` button (`CB_ASK_ENTER`) + arrow nav still
send Enter — the orthogonal navigation path, unchanged, AND the user's manual
escape if a future variant defeats the auto-dispatch. **Scoped to single-select
`aqp:` + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a
bare digit — a filed fast-follow (AUQ is NOT globally fixed).** Validated against
Claude Code v2.1.168 terminal behavior.

## Tappable Decision dispatch (`dcp:` lane — Stage B2.3, flag `CC_TELEGRAM_DECISION_DISPATCH`)

A PARALLEL, Decision-specific dispatch lane that gives the B1 `Decision` cards
verified one-tap option buttons. It reuses the AUQ dispatch DISCIPLINE — per-window
send lock + `_lock_busy` reject-if-held, monotonic arrow nav,
settle→re-parse→verify, `Enter` as the ONLY commit key, fail-closed advance
classification, `auq_action_ledger.jsonl` idempotency — but NEVER the AUQ
`resolve_auq_source` / `resolve_ask_form` machinery (a Decision pane returns None
there — the P1-C dead-tap). Default OFF; a flag-OFF deploy mints no buttons and the
`dcp:` callback declines ("Dispatch disabled — use the nav keys."). Requires
`CC_TELEGRAM_DECISION_CARDS` ON to matter.

**Render mint** (`interactive_ui._build_decision_pick_rows`, in the
`content.name == "Decision"` gate branch): mints `dcp:<route_hash>:<fp8>:<opt>:<token>`
buttons ONLY when the flag is ON, the strict `parse_generic_decision` form matches a
`decision_token.identify_family` (which requires a non-None title — the §5a mint
gate), `decision_token.lookup(family, w.pane_current_command)` licenses the family ×
the CACHED CC-version, and the geometry is a clean single-select numbered picker
(exactly one `❯`, no checkbox markers, contiguous 1..N); else display-only,
byte-identical to B1. `fp8` = `terminal_parser.decision_prompt_fingerprint[:8]` — a
body-inclusive canonical with a `decision:` DOMAIN PREFIX, so the shared ledger key
can NEVER collide with the AUQ lane's bare-`_canonical_repr` fp8 (§8). The row is
minted through `decision_token.mint_row` (§3(3) sibling-burn: a winning consume
tombstones the whole route row).

**Dispatch transaction** (`callback_dispatcher/interactive._dispatch_decision` →
`_dispatch_decision_pane_locked`): tap → dispatch-flag check → ledger lookup FIRST
(the AUQ collision matrix copied: owner-mismatch → live-token-peek collision test →
else `WRONG_USER_PICK_TEXT`; per-state matrix — `dispatched` "already received" /
`accepted` "in progress" / `unknown`+`commit_unconfirmed` refresh-only /
`not_advanced` falls through) → token peek → owner → stale-window lease → consume by
exclusive reservation → `accepted` ledger claim → under `window_send_lock` (reject
if held): (a) extractor parity (`extract_interactive_content(pane).name ==
"Decision"` — a Settings/AUQ pane that merely decision-parses bails, the named
`settings_warning_v2170.txt` decline) → (b) `decision_prompt_fingerprint` identity +
geometry/family gates → (c) the **FRESH** `pane_current_command` version-license
re-read (`pane_command_is_claude` + `lookup`, INSIDE the lock, immediately before the
first key — a /update-swapped TUI inside the 1s list-cache TTL can never be
arrow-keyed; the AUQ round-2 P1-1 fix) → (d) nav→settle→verify with a MOTION proof
(delta≠0: cursor moved to target AND ≠ pre-nav; delta==0: the WIGGLE — one arrow away
then back, requiring the `❯` to move — a quoted block can't) → (e) loose landing-label
match → (f) `Enter` → `_classify_decision_advance` — **confirm-side extractor parity
(review r1 P2-B):** the confirm runs the FULL `extract_interactive_content(pane2)`
(the SAME first-match-wins semantics as render + pre-commit; never the bare
`parse_generic_decision`, a WEAKER recognizer — a Settings/AUQ pane that merely
decision-parses would fp-compare as a "different Decision" and wrongly confirm):
extractor→Decision ⇒ fingerprint compare (`dispatched` ONLY when the committed
fingerprint is proven GONE; a live same-fp form is the round-3 zero-absence variant →
`commit_unconfirmed`); extractor→ANOTHER named UI or None ⇒ `dispatched` only when NO
decision footer/marker line remains (a still-present footer under a named UI /
unparseable frame is AMBIGUOUS → `commit_unconfirmed`, never dispatched — pinned by
`test_commit_into_named_ui_pane_records_commit_unconfirmed` on the settings_warning
fixture). **Ledger discipline:** `accepted → dispatched` +
`auq_ledger.release_key(key)` on the confirmed-gone proof; a **pre-commit bail**
records `not_advanced` (Enter provably never sent → falls through / re-renders fresh
tokens); once Enter is sent, an unconfirmed advance records `commit_unconfirmed`
(refresh-only, UNRELEASED). A **busy send lock at dispatch downgrades the
already-written `accepted` to `not_advanced`** (fall through, never a
crash-ambiguous `accepted`).

**§5b(b) dispatch-terminal teardown** (`interactive_ui.finalize_decision_dispatch`,
NOT `clear_interactive_msg` — that deletes/tombstones): pops the PERSISTED
interactive surface (a stale raw-nav tap then fails `has_interactive_surface` —
restart-safe) + `decision_token.teardown_route`, fires the lifecycle hooks (the
poller's `_on_interactive_clear` drops `_absent_streak` + `_last_published_ui_hash`
→ a fast byte-identical re-raise renders FRESH), then edits the card to the inert
"✅ … sent" final state. **Ordering (review r1 P2-C, the plan §3 text is normative):
on `dispatched` the finalize runs FIRST, THEN the callback answer** — answering
first left a crash/network window where the callback was acked but the persisted
surface was not yet terminal (pinned by
`test_dispatched_finalizes_before_callback_answer`). **§5b(c)/O-6
generation-suffixed nav** (closes the
pre-existing window-keyed raw-nav replay hole): every GATE card render (Decision AND
Permission/Workflow per O-6) rotates `decision_token`'s per-window nav generation and
suffixes its ↑/↓/⏎/Esc callbacks `aq:*:<window>:g<gen>`; non-gate (AUQ/EPM/
RestoreCheckpoint) renders CLEAR the generation and stay un-suffixed (byte-neutral,
the non-regressive constraint). `assert_nav_dispatchable` parses `(window_id, gen)`
BEFORE `reject_stale_window` (guardrail 1) and validates (guardrail 2): gen present
must equal the window's current gen; gen absent + a live gate generation → refuse (a
pre-B2 un-suffixed gate card). **gen absent + no gate generation is AMBIGUOUS, not
automatically legacy (review r1 P1, BOTH engines):** the registry is in-memory, so
after EVERY restart/deploy it is empty — a gate card published pre-B2.3 (raw
un-suffixed `aq:enter:@N` callbacks) tapped before the poller re-renders it would
otherwise raw-dispatch into a live gate pane. No in-memory/persisted authority
records the surface's UI KIND, so that shape is discriminated on the LIVE pane —
reusing guard 4's EXISTING visible capture (the suffixed / gen-registered paths gain
NO pane capture): `extract_interactive_content(visible).name in {Decision,
Permission, Workflow}` → refuse fail-closed before any key (the poller re-renders a
fresh suffixed card within ~1s); an AUQ/EPM/other pane proceeds down the legacy path
unchanged (byte-neutral, pinned by the AUQ-pane companion test). The generation is
invalidated IN-LOCK at `dispatched` (covering the lock-release→teardown gap) and
wiped on restart → a suffixed tap fails closed ("Card refreshed — use the current
card").

**§8 restart + long-lived cards:** in-memory tokens + nav generations die; the
ledger-first gate answers a `dispatched` duplicate; NO durable `pick_intent`-style
recovery (Decision re-mints from the live pane trivially — the poller's Decision
same-hash branch calls `decision_token.refresh_route_deadlines`, the D3-β analogue,
so a long-open `/update`-AFK card's tokens never TTL-prune). **Teardown seams
(review r1 P2-A):** `decision_token.teardown_route` is wired beside the existing
pane_signals/route_runtime teardown calls at `clear_interactive_msg` /
`finalize_decision_dispatch` (surface end), the **`/clear` `mark_session_reset`
seams** (`bot.forward_command_handler`'s /clear branch AND the monitor's
session-rotation sweep), and the `inbound_telegram` stale-window unbind
`clear_route` sites — a /clear-rotated window keeps its id, so a same-fingerprint
Decision (same-cwd folder-trust) re-raised by the NEW session within the 300s token
TTL would otherwise validate a STALE `dcp:` tap end-to-end (extractor parity +
fingerprint + license all pass); only the teardown stops it (pinned by
`test_clear_invalidates_decision_tokens_same_fp_reraise_refuses`). **Top residual
(disclosed):** the `decision_token._DECISION_DISPATCH_TABLE` allowlist is per
`(family × CC-version)` — every CC upgrade empties the effective allowlist → buttons
revert to display-only until re-characterized (honest degradation, INFO logs at mint
+ tap; never a wrong keystroke). Verify→Enter TOCTOU is disclosed + minimized (same
class as AUQ's), bounded by the `commit_unconfirmed` fail-closed. Pull-only
throughout; no observer (c313657 stays forbidden).

## AFK auto-resolve conversion + late answer (aql:) — Wave A

On Claude Code ≥2.1.198 an unanswered AskUserQuestion **self-resolves at ~60s**
(undocumented, no knob — GH #30740 closed not-planned) with a synthetic
tool_result ("No response after 60s — the user may be away from keyboard. …")
whose entry-level ``toolUseResult`` carries the full ``questions`` array and
``answers: {}`` (empty; an ``afkTimeoutMs`` field is also observed — preserved
in the fixture as a candidate future discriminator, NOT part of the detection
contract). Pre-Wave-A that tool_result tore the picker card down exactly like
a genuine answer, leaving the bridged owner a topic with no card and no way to
answer. The bridge ADAPTS (owner-approved; the CLI default is never defeated):

**Detection (two-factor, ``handlers/late_answer.is_afk_auto_resolve``).**
Factor 1: an unanchored, drift-tolerant regex (`No response after \d+
\s*(s|secs?|seconds?|m|mins?|minutes?)\b`, case-insensitive) over ``msg.text``
(the raw content wrapped in ``EXPANDABLE_QUOTE`` sentinels — hence unanchored).
Factor 2 (authoritative): ``tool_result_meta.answers`` a NON-EMPTY dict →
False regardless of the regex (a genuine free-text answer may ECHO the AFK
phrase). ``tool_result_meta`` is the entry-level ``toolUseResult`` plumbed
onto ``NewMessage`` at the PARENT emit site only (sidechain emits stay None).
Meta ABSENT (None / non-dict — the Esc-rejection's ``toolUseResult`` is a
plain string) → the HARDENED rule: sentinel-strip → the negative wrappers
("Your questions have been answered:" / "The user doesn't want to proceed")
reject FIRST → then the stripped content must BEGIN with the AFK phrase
(anchored). Best-effort by design: the monitor's pending-tool
``**AskUserQuestion**(…)`` summary prefix makes the anchored match
false-NEGATIVE — the safe direction (today's teardown); the meta-PRESENT path
is the real detection path. False negative = today's silent teardown; the
Esc-rejection never matches (correct — the user acted in the terminal).

**Conversion (bot.py's explicit AUQ tool_result branch — the ONLY caller).**
Non-AFK: today's teardown byte-identical (``forget_ask_tool_input`` +
``auq_ledger_release_window`` at their exact prior positions). AFK: ONE call —
``interactive_ui.convert_interactive_msg_to_late_answer`` — owning the ENTIRE
teardown+conversion inside a single ``_get_route_lock`` critical section with
NO await between steps: (1) **snapshot** under the id-parity trust rule
(window cache via ``peek_ask_tool_use_id`` == the tool_result's id OR either
unknown; fallback ``auq_source.read_side_file_for_recovery`` vs
``peek_side_file_tool_use_id`` under the same rule — the side file's captured
id "may be ''", treated as unknown; both mistrusted → snap=None); (2) the
exact ``clear_interactive_msg`` **Phase-1 mirror** — ``_clear_interactive_msg``
+ ``_interactive_mode.pop`` + ``pick_token.prune_for_route`` on the POPPED
window ONLY (never the caller's wid blindly; WARNING on mismatch);
(3) ``forget_ask_tool_input`` (side-file unlink still before ANY awaited
Telegram I/O — the orphan-safety ordering; ``late_answer.invalidate_window``
fires inside it, safe — the mint happens later); (4)
``auq_ledger.release_window`` (AFK is genuine resolution — the tombstone is
correct). Post-lock, ``_fire_clear(cleared_window_id)`` + the Phase-2 edit run
best-effort **SHIELDED** once Phase 1 commits (the W1 delete-protocol
precedent) so a caller cancellation cannot strand a visibly-tappable dead
picker; a poller tick that tombstoned the card first degrades to the disclosed
no-surface skip (never a re-post, never a surviving pick-token row).

**Card (Phase 2, EDIT-only v1).** ``topic_edit(op="interactive", plain=True)``
edits the picker message into "⏰ Claude proceeded after ~60s (no response)."
+ ``Question: <q>`` (``_clip_card_title``, omitted when snap=None) + an
``aql:`` keyboard ONLY for single-question single-select (labels ≤64, one per
row — full descriptions stay in the still-standing 📋 details message);
multi-Q / multi-select / snap=None → text-only "Reply in text to send a
correction." No surface → log ``AFK_CONVERT no_surface`` and return; edit
failure → log, NO delete-fallback (the tombstone rule). **The converted card
is NOT a live interactive surface** — ``has_interactive_surface`` goes False,
the generic teardown later in the loop skips, run-state clears via the
transcript path exactly as today (NO route_runtime change). One token per
CARD in the in-memory ``late_answer`` registry (``live → in_flight →
consumed``); NOT persisted, NOT a route_runtime field, no observers (c313657).

**aql: executor (``callback_dispatcher/late_answer.py``).** Parse
``aql:<window_id>:<opt>:<token>`` → registry lookup (None → graceful
"expired — reply in text instead" modal + best-effort keyboard-clear
preserving ``query.message.text``) → owner check (``WRONG_USER_PICK_TEXT``) →
stale window (payload/registry parity + the lease + ``find_window_by_id``
None) → freshness guards (``has_interactive_surface`` OR
``side_file_live_for_window`` → "A newer prompt is live in this topic —
answer that instead."; the PreToolUse hook writes the side file BEFORE a new
picker renders, closing the JSONL-buffered-tool_use gap) → ``begin_send``
single-use → sending-state edit with the keyboard REMOVED → the **effort.py
route-ordering delivery subsequence ONLY** (aggregator flush → PRE-SEND
``set_route_user_turn_at`` — the late answer is a genuine user turn, so
live-prose turn-boundary + dashboard 🔔 semantics match a typed message →
``send_to_window`` with the ``(bool, str)`` return honored →
``mark_inbound_sent``). Success: "✅ Late answer sent: <label>"; failure:
single-use reset to live + the ORIGINAL keyboard re-attached for the retry
tap (the reason effort.py is NOT copied line-for-line — it clears the
keyboard pre-delivery). Delivery text (single line, ALL whitespace runs
collapsed — an embedded newline would submit early): ``Re your earlier
question "<question≤200>" (it auto-resolved after 60s while I was away): my
answer is "<label>". Please course-correct based on this.``

**Lifecycle / invalidation.** ``late_answer.invalidate_window`` at (a)
``forget_ask_tool_input`` (the primary seam — next AUQ's tool_result, /clear /
session replacement, the generic surface clear) and (b)
``remember_ask_tool_input``'s tool_use_id-rotation branch (a BACKSTOP only —
rotation fires late because a new live picker is JSONL-buffered; the real
protection is the executor's freshness guards); (c) topic close via the
topic-keyed ``late_answer.invalidate_topic`` beside
``route_runtime.clear_routes_for_topic`` in ``clear_topic_state`` (NOT inside
the queued-routes loop, whose ``_route_queues`` enumeration would strand a
queue-less route's card — the same gap that gave route_runtime its own seam).

**Residuals (disclosed, plan A10).** Restart wipes the in-memory registry
(the tap answers the graceful expired modal and clears the dead keyboard);
no-surface AFK skips (EDIT-only v1); the send-into-new-picker race is closed
to a sub-second hook-write window; multi-Q/multi-select late answers are
text-only; EPM 60s behavior is unobserved → ExitPlanMode is OUT of scope;
labels are clipped to 64 chars on buttons AND in the correction message.
Pull-only throughout; no observer (c313657 stays forbidden).

## Artifact delivery lane (📎 tap-to-download + `/file`)

Parent-route assistant PROSE that names a deliverable local file
(`report.md` / `chart.png` / … in `artifacts.ARTIFACT_EXTS`) gets a compact
`📎` follow-up card with one `dlf:<window_id>:<token>` button per file; a tap
uploads that file to the topic as a Telegram document. `/file <path>` is the
durable escape hatch.

**Detection seam (`bot._maybe_offer_artifacts`).** Runs at the parent
assistant-text block (`msg.role=="assistant" and msg.content_type=="text" and
msg.subagent_key is None`), gated on the per-recipient `prefs.artifact_card`
(preset-only knob; `quiet=off`). NO detection in tool_results / Bash output /
thinking / sidechain narration / web URLs (the anti-spam core — tool output is
full of incidental paths). cwd comes from the window state (empty ⇒ skip,
fail-closed); `max_bytes` + extra roots are read from `config` at the callsite
and INJECTED into the config-free `artifacts` leaf. **Ordering:** the card is
`enqueue_artifact_card`-ed STRICTLY AFTER the block's `enqueue_content_message`
(codex P1-2), so the route FIFO delivers prose → card. Cap 6 buttons/card;
overflow disclosed in the card text (`…and N more — send /file <path> using a
path from the message above.`). **Card body is PATHLESS (owner decision
2026-07-09 — TLD auto-linkification):** the body is a single static line
`📎 Tap to download:`, never the detected paths — Telegram clients auto-linkify
a bare path whose extension collides with a TLD (`.md` = Moldova, `.zip`, …)
into a dead blue link, and the triggering prose directly above the card always
names the file(s), so the body repetition added nothing. The (clipped) button
labels carry the names.

**Validation + validated-fd upload (`handlers/artifacts.py` leaf).**
`resolve_artifacts` / `resolve_single`: expanduser → cwd-join a relative
candidate → `Path.resolve()` (FOLLOWS symlinks — an in-cwd symlink pointing
outside RESOLVES outside and fails containment) → MUST be `is_relative_to` a
RESOLVED allowed root (cwd + `CC_TELEGRAM_ARTIFACT_ROOTS`; empty cwd contributes
no root — fail-closed) → regular file + `st_size <= max_bytes`. **Worktree
fallback (still fail-closed):** a RELATIVE candidate that misses under the
session cwd (file-not-found / not-contained — NOT an oversize/type reject, which
the cwd copy OWNS) retries the join against the derived main-repo root when the
resolved cwd carries the harness `.claude/worktrees/<name>` shape
(`_worktree_main_root`: the prefix before the `.claude`/`worktrees` segment pair,
pure string logic — no git subprocess); the cwd hit ALWAYS wins (same-named file
in both → the session's own copy), the main-root hit is pinned + displayed
relative to the main root, and a `../`-escape / symlink-escape rejects under BOTH
roots (containment + O_NOFOLLOW + fstat unchanged). Only the harness layout is
covered — a general `git worktree add` elsewhere is NOT. The card path
drops rejections silently; `/file` surfaces the specific reason (not found /
outside roots / too large [states the cap] / no working directory). The SEND
closes the TOCTOU hole: `open_validated_artifact` re-checks containment against
the roots **PINNED in the registry row at mint time** (codex r2 P2-1 — never a
recomputed, mutable `WindowState.cwd`), `os.open(path, O_RDONLY |
getattr(os,"O_NOFOLLOW",0))` (a final-component symlink swapped in after
validation → open FAILS), `os.fstat` → `S_ISREG` + size ON THE FD, and passes
THAT open file object to `message_sender.send_document` — the pathname is NEVER
re-opened. `send_document` returns `(ok, reason)` and RE-RAISES `RetryAfter`
(the executor handles it). Disclosed residual: an intermediate-DIRECTORY symlink
swap between resolve and open is not covered by `O_NOFOLLOW` — accepted on a
single-owner box; the fstat still guarantees regular-file + size.

**Token registry + card task.** In-memory `dlf:` tokens, single-FLIGHT not
single-use (`begin_send` gates concurrent taps; `finish_send(ok)` returns the
row to `live` either way — a re-tap re-uploads the current bytes, benign +
serialized). A row PINS the resolved path + the resolved allowed roots. Offer-
dedup keyed `(route, resolved_path)` (30 min) makes a mid-turn repeat cheap;
24h lazy token TTL. The card rides a `message_queue` `artifact_card` control
task (route-FIFO, `_RETRYABLE_TASK_TYPES`) sent `plain=True` (no MarkdownV2
escaping of paths) with the rows wrapped into an `InlineKeyboardMarkup` in
`message_queue` (the leaf never imports telegram).

**Executor (`callback_dispatcher/artifacts.py`).** `aql:`-style guard order:
lookup (None → graceful "expired — use /file" modal) → owner check → stale-
window (payload/registry parity + lease + live-window existence) → single-
FLIGHT `begin_send` → **ANSWER THE CALLBACK FIRST** ("Uploading <name>…", since
an upload can exceed the callback-answer deadline) → `open_validated_artifact`
→ `send_document(open fd)` → success `finish_send(True)` / failure
`finish_send(False)` + in-topic `❌ Upload failed: <reason>` / RetryAfter
`finish_send(False)` + "Rate-limited — tap again shortly."; the fd is closed in
a `finally`.

**Teardown.** `artifacts.invalidate_topic(owner, thread)` in
`cleanup.clear_topic_state` (the COVERING seam — topic close/delete + the
status-poller window-gone path all route through it), topic-keyed (mirrors
`late_answer.invalidate_topic`); `artifacts.invalidate_window(window_id)` at the
four `inbound_telegram` stale-window unbinds (beside `decision_token.teardown_route`).
`forget_ask_tool_input` is deliberately NOT a seam (AUQ-specific); `/clear` and
session rotation deliberately do NOT invalidate (tokens are path-anchored, cwd
survives rotation, and SEND-TIME revalidation is the real guard). Restart wipes
the registry (a dead button answers the expired modal; the prose above the card
names the paths + `/file` cover it — the body itself is pathless). Pull-only
throughout; no observer (c313657 stays forbidden).

## MessageDisplay live-prose capture (Bug 2)

Assistant free-text prose written in the same turn as an `AskUserQuestion` /
`ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at
resolution, so during a live prompt the monitor's byte-offset read sees no new
bytes and the prose is not on the bridge — the Telegram user would see only the
picker card and choose blind. Claude Code's `MessageDisplay` hook fires with
each streaming `delta` of an assistant message BEFORE the picker blocks; the
tiny stdlib appender (`_md_display_appender.py`) writes each raw payload as one
NDJSON line to `msg_display/<session>.ndjson`, keyed by
`Path(transcript_path).stem` (resume-safe). The hook is scoped to bot-launched
sessions via a bot-managed `md_hook_settings.json` passed as `claude
--settings` (it merges with the global `SessionStart` / `PreToolUse` hooks and
is never installed into `~/.claude/settings.json`).

`MessageDisplay.message_id` has no JSONL counterpart and `delta` is per-flush
(`final=True` marks end-of-message), so **accumulation is bot-side**:
`md_capture.read_prose_records(session_id)` reads the per-session NDJSON ON
DEMAND (pull-only — no background tailer / observer; c313657 stays forbidden),
groups deltas by `message_id`, concatenates them in index order, and returns one
`ProseRecord` per FINALIZED message (`{session_id, transcript_path,
md_message_id, text, raw_hash, norm_hash, first_seen_at, final_at}`) ordered by
`final_at`. It tolerates a missing file, corrupt / partially-written lines, and
not-yet-final messages (omitted — the render-path bounded retry re-reads).
`md_capture.normalize_prose` (CR/CRLF→LF + per-line trailing-trim + edge strip,
NO interior collapse) is the SINGLE normalization used for both the live
`norm_hash` here and the post-resolution JSONL dedup, so the two compare equal
regardless of streaming-vs-flush quirks — the mint/validate parity that keeps
dedup from silently failing.

The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin`
marker through `ParsedEntry` / `TranscriptEvent` / `NewMessage` (a single
backfill stamps every entry of an assistant line with its `message.id`; the
synthetic ExitPlanMode plan body — emitted as `content_type="text"` from
`input.plan` — is marked `BLOCK_ORIGIN_EXIT_PLAN` so dedup never suppresses real
prose by matching it).

**Live delivery (PR-C).** `interactive_ui.handle_interactive_ui`, under the
route lock and BEFORE the picker card / AUQ context message,
`_maybe_post_live_prose` reads the freshest finalized capture
(`md_capture.select_fresh_prose`), posts it as its own message, and records a
**shown-live marker** in the same per-session capture file. Idempotent via
`md_capture.was_shown_live` (consume-INCLUSIVE: a re-render / poll re-detect /
post-`kickstart` / the dedup having consumed the marker all skip a re-post). A
miss is a silent no-op — the JSONL copy delivers post-resolution exactly as
before (no marker, no dedup, never a delayed picker). A bounded ≤250ms retry
covers the rare same-tick race. Render-path state only — NOT a RouteRuntime
field (Bug-1 contract intact). The four `_maybe_post_live_prose` early returns
log a miss-classification line (`no_session` / `card_exists` / `capture_absent`
/ `not_before_reject` / `ttl_and_anchor_reject` / `empty_text` /
`already_shown_live`) so the next miss is diagnosable (PR-1 A6).

**Late-finalize stream-wait.** `_maybe_post_live_prose`'s base catch-up budget
is 250ms (`_LIVE_PROSE_RETRY_BUDGET_S`); the common clean case finalizes prose
BEFORE the picker is detected, so the first read hits. If the budget expires
with no finalized prose AND `md_capture.is_prose_streaming(session_id)` is True
(a message has deltas, no `final` yet, and its LATEST delta is within an 8s
recency window — the latest-delta anchor keeps a long stream live while a
crash-orphan ages out), the wait extends ONCE by
`_LIVE_PROSE_STREAM_WAIT_BUDGET_S` (3.0s) so a prose finalizing mid-stream still
posts BEFORE the card. A prose-less picker (no streaming) bails at the base
budget (zero added delay); a never-finalizing stream degrades to today's miss on
expiry (card created, JSONL delivers) — never hangs, never churns, pull-only.

**ExitPlanMode plan body BEFORE the card.** The EPM card carries no plan text
(only "Claude has written up a plan … proceed?" + options + a `ctrl+g … ·
~/.claude/plans/<slug>.md` footer), and the plan BODY is the tool's `input.plan`
— a synthetic `BLOCK_ORIGIN_EXIT_PLAN` text block buffered in JSONL until
resolution — so the user used to approve blind and get the plan AFTER. Fix:
`interactive_ui._maybe_post_epm_plan` (called from `handle_interactive_ui` AFTER
`_maybe_post_live_prose`, BEFORE the card, under the route lock → ordering
findings→plan→card) posts a "📋 Plan" message before the picker. The plan text
is `tool_input.plan` (replay) or, for a LIVE pane card (`tool_input` None), read
from the `~/.claude/plans/<slug>.md` file named in the pane footer
(`terminal_parser.extract_epm_plan_file_path`, footer-line-anchored; the read is
path-traversal-guarded to `~/.claude/plans/` + `asyncio.to_thread`). Idempotent
across poll re-renders + restart via an `md_capture` marker keyed by the plan's
`prose_norm_hash` (`record/was/read/consume_epm_plan_shown_live`, stored in the
same per-session NDJSON so `teardown_session` reclaims it). The post-resolution
JSONL copy is suppressed by a SECOND arm in
`session_monitor.filter_live_prose_duplicates` that aggregates the
`BLOCK_ORIGIN_EXIT_PLAN` block, hashes it via the SAME `prose_norm_hash` (the
plan-file text normalize-equals `input.plan` — mint/validate parity), and
matches the SEPARATE `epm_plan_shown_live` marker (never cross-matches real
prose; >1 group sharing a marker suppresses none). FAIL-OPEN: a hash mismatch /
missing file only fails to suppress (benign double-post) or skips the pre-post
(plan via JSONL) — never a wrong/lost post, never a crash. Pull-only; no
observer.

**Emission-anchor freshness — the additive-OR (PR-1, the dominant-miss fix).**
The original freshness was render-time `now` only: `now - final_at <= TTL`
(`AUQ_PROSE_TTL_S` 8s / `EPM_PROSE_TTL_S` 12s). The baked-in premise that "the
prose finalizes ~0.68s before the picker blocks" was INVERTED — measured (Wave-0
capture, Claude Code 2.1.172) the prose finalizes a gap BEFORE the picker is
DETECTED: ~5.44s idle, up to ~20.7s under bot load (the poller only scrapes on
its ~1s cadence and the adaptive watchdog can skip the blocked frame). So a fixed
render-time TTL routinely aged the matching prose out and the prose never posted.
`select_fresh_prose` now ORs the TTL leg with an **emission-anchor leg** keyed to
a STABLE picker-emission instant `emitted_at`: keep `r` iff
`(now - final_at <= ttl)  OR  (emitted_at is not None and  emitted_at -
emit_anchor_lookback_s <= final_at <= emitted_at + emit_anchor_eps_s)`, all still
AND-ed with the `not_before` turn boundary below. The OR can only WIDEN over the
TTL leg → provably non-regressive on the upper bound. The anchor SOURCE + its
eps/lookback constants are selected by modality in `_maybe_post_live_prose`:
**AUQ** → `auq_source.peek_side_file_written_at(session_id)` (the PreToolUse
side-file `written_at` ≈ the tool_use invocation; read-TTL-free, future-skew
guarded) with `_EMIT_ANCHOR_EPS_S` (2s) / `_EMIT_ANCHOR_LOOKBACK_S` (10s);
**ExitPlanMode** → `status_polling.peek_epm_surface_emitted_at(...)` (the poller's
FIRST-DETECTION stamp — EPM has no side file) with `_EMIT_ANCHOR_EPS_EPM_S` (2s)
/ `_EMIT_ANCHOR_LOOKBACK_EPM_S` (30s). The EPM lookback is LARGER because its
poller-stamp anchor lags the tool_use by the whole detect latency, whereas AUQ's
hook stamp sits ~at the tool_use; the AUQ lookback stays tight because it is ALSO
the restart-asymmetry guard — across a restart the on-disk AUQ `written_at`
survives (so `emitted_at` is non-None) while the in-memory `not_before` delivery
stamp is wiped to None, so the lookback is the ONLY floor left and must reject a
stale prior-turn prose finalized well before this picker's tool_use (EPM has no
on-disk anchor → `emitted_at` is None post-restart → the OR leg simply doesn't
fire, so its generous lookback is safe). The EPM stamp is poller-local
state: `status_polling._epm_surface_first_seen_at[route]`, `setdefault`-stamped
(first-detect, never a sliding window) wherever `ui_content.name ==
"ExitPlanMode"` is observed (the new-UI dispatch + the in-mode block), POPPED at
every EPM lifecycle end (the interactive-clear callback PRIMARY, the poller
mode-end / in-mode-absence / window-switch / window-gone seams, and
`clear_route_caches_for_topic`) so the NEXT EPM in the topic anchors to its OWN
instant; route-keyed so a double-`--resume` sibling never lights. Pull-only; no
observer.

**Turn-boundary anchor (Item 3 / P2-1 — the prior-turn-prose leak).** Freshness
was session + TTL only, so a PRIOR turn's leftover prose (still in the per-session
file because teardown only fires at AUQ/EPM resolution, and still within the TTL)
could be posted above a picker whose OWN turn produced no prose. Fix: a
**delivery-seam `not_before` anchor**. `message_queue.set_route_user_turn_at`
stamps the route's wall-clock delivery instant (`time.time()`) **PRE-SEND** —
immediately BEFORE `send_to_window` at the user-turn delivery seams
(`inbound_aggregator._send_bundle`, the slash-command `bot.forward_command_handler`,
and the `/effort` callback) so a fast prose→AUQ turn can't finalize its prose
before the stamp lands. `_maybe_post_live_prose` reads it non-consumingly
(`peek_route_user_turn_at`, resolved INSIDE the function so the 22
`handle_interactive_ui` callers are untouched — auto-closes the inbound:1061
on-pane + restart first-render holes) and passes it as `not_before` to
`select_fresh_prose`, which adds a **STRICT `final_at > not_before`** gate: the
current turn's prose is captured AFTER delivery, a prior turn's BEFORE it
(`==` boundary is excluded — not causally after the delivered message). The stamp
shares the appender's `captured_at` clock, so they compare directly. The store is
torn down with the route (beside `_route_last_user_message`) and cleared by
`reset_for_tests`; it is **render/callback-path state, NOT a RouteRuntime field**
(pull-only; c313657 forbidden). **Residuals (all safe):** after a **restart** the
in-memory stamp is gone → `not_before=None` disables THIS turn-boundary filter
(PR-1 NOTE: the AUQ emission-anchor `written_at` survives the restart, so its
lookback lower bound now carries the restart-asymmetry prior-turn guard — see the
additive-OR; the freshness falls to pure TTL-only only when `emitted_at` is ALSO
None, e.g. EPM or no side file — documented degradation, never a false-negative
on the live path); a rare **wall-clock-backwards** jump could mis-order a stamp vs a
`captured_at` (NO epsilon is added — accepted as a rare residual); the per-session
file's tracked-idle disk retention is unchanged (teardown still owns reclaim). A
**concurrent-send clobber** — a LATER delivery whose stamp overwrites the route's
single boundary BEFORE an earlier, not-yet-rendered picker first-renders — can
suppress that earlier picker's prose (it then arrives post-resolution via JSONL,
never a wrong post). The common "send while a picker is on the pane" case is
defused upstream: `inbound_telegram` renders the on-pane picker with the prior
stamp BEFORE offering the new message; the only residual is delivering into a
still-streaming Claude before its picker appears (bounded, degrades to JSONL).
A per-picker boundary would close it but is disproportionate for this benign,
already-degenerate edge.

**Dedup (PR-D).** `session_monitor.filter_live_prose_duplicates` runs on the
poll BATCH before per-message dispatch (the prose text block and its sibling
interactive `tool_use` are separate `NewMessage`s of one `message_id`, prose
first — only the batch sees the pairing). For each `(session_id, message_id)`
group with an AskUserQuestion / ExitPlanMode `tool_use`, it aggregates the REAL
text blocks (excludes `BLOCK_ORIGIN_EXIT_PLAN`), hashes via the SINGLE shared
`md_capture.prose_norm_hash`, matches an unconsumed shown-live marker, and
suppresses + consumes (consume-once, restart-safe). EPM ambiguity safety: >1
group sharing one `(session, norm_hash)` marker → suppress NONE. Multi-block
parity: aggregation joins parser-stripped blocks with `\n` — exact for
single-block (Bug 2's observed shape) and adjacent multi-block, a benign
double-post only for the rare blank-line-between-blocks case. Within one poll
batch the dedup runs BEFORE the dispatch that triggers teardown, so it reads the
marker first; the only gap is the split-batch edge (prose and its tool_use land
in SEPARATE poll batches — unlikely given the turn co-flushes atomically), where
the prose batch can dispatch undeduped and teardown can fire before the later
tool_use batch → another benign double-post, never a crash.

**Lifecycle.** `md_capture.teardown_session` (unlinks the per-session capture +
its markers) is wired at AUQ/EPM resolution (`forget_ask_tool_input`, the
primary seam — fires for both via `bot.handle_new_message`'s
`has_interactive_surface` branch), the `/clear` race + deleted windows
(`session_monitor` via the OLD session id), and topic close (`clear_topic_state`
→ the thread's bound window). The 1h startup `gc_stale` is the backstop. The
shown-live / consumed marker lines live in the SAME `msg_display/<session>.ndjson`
as the capture deltas (the delta reader ignores `marker` lines and vice-versa),
so they share that lifecycle. **Startup-GC liveness gate (Item 3 / P2-2).**
`gc_stale` previously reaped ANY `*.ndjson` >1h with no liveness check, so a
long-open picker's capture file (which carries its shown_live/consumed dedup
markers) was reaped at startup → the post-resolution dedup double-posted. Fix: an
**INJECTED `is_live_session` predicate** — the `bot.py` callsite passes
`lambda sid: monitor.state.get_session(sid) is not None` (keyed by the file STEM =
the original session id the monitor tracks under `--resume`, covering BOTH AUQ and
EPM since it is session-keyed, not prompt-typed). After the age test, a `True` →
**SKIP** (keep the live file + its markers); a predicate **raise** → conservative
SKIP (never delete on uncertainty; caught around the predicate call only so the
pass continues); and a **re-`stat` before `unlink`** is the TOCTOU guard (a
concurrent append refreshing the mtime within `max_age` → skip). The predicate is
NEVER imported into `md_capture` (it stays a leaf — only stdlib + `utils`). Pull-only
throughout (no observer; c313657 forbidden).

## Cross-topic dashboard (Wave C)

One passive, owner+chat-scoped overview message per `(chat_id, owner_user_id)`,
owned by `handlers/dashboard.py` and persisted as the `dashboards` key in
`state.json` through SessionManager's single `_load_state`/`_save_state` path
(sync named mutators: `get/set/clear_dashboard`, `update_dashboard_msg_id`,
`set_dashboard_pinned`). `/dashboard` in any topic claims THAT topic as the
host (DM/General rejected; re-run elsewhere MOVES it, old message deleted
best-effort; `/dashboard pin` is the only pin path — never automatic, persisted
only on pin-API success). The whole Telegram-I/O-spanning claim/move/self-heal
flow serializes on a per-`(chat, owner)` `asyncio.Lock` with a post-send
loser-cleanup re-read (pre-C fix 1).

**Update driver is PULL-ONLY**: `maybe_refresh_dashboards` rides the existing
1s status-poll sweep (called once per sweep, not per binding — no observer,
c313657 forbidden). It renders the owner's view from
`session_manager.iter_thread_bindings()` + `route_runtime.snapshot(route)`,
**chat-scoped** (hermes review P1): `render_dashboard(owner_id, chat_id)`
includes only bindings whose persisted `group_chat_ids` mapping
(`session_manager.get_group_chat_id`) resolves to the dashboard's own chat —
FAIL CLOSED, an unresolvable chat is excluded from every dashboard, so a
dashboard in forum A never exposes forum B's topic names/states. That filter is
only as trustworthy as the mapping, so the **trust boundary** (hermes R2 P1):
`group_chat_ids` is written ONLY by the genuine bound-topic message seams
(`text/photo/voice/document_handler`, `forward_command_handler`,
`topic_edited_handler`) — `/dashboard` itself NEVER writes
`set_group_chat_id`, because thread ids are chat-local and a host claim in
chat B's unbound thread N would overwrite the mapping of chat A's bound topic
N and leak it onto chat B's dashboard. The dashboard instead carries its OWN
chat explicitly (the command's `effective_chat.id` at claim time, the
`dashboards` record key afterwards) through every
`topic_send`/`topic_edit`/`topic_delete` — those helpers take an explicit
`chat_id` and never resolve via `group_chat_ids`. It hashes the
rendered body and edits only on change — the hash covers state
lines, display names, and the binding set, so run-state transitions AND
bind/unbind/rename all repaint without a dedicated trigger; ages are
minute-coarse so the hash is stable within the minute (the implicit 60s age
tick). `MESSAGE_NOT_MODIFIED` is success (W8 precedent). Self-heal (re-send +
`update_dashboard_msg_id` under the lock) fires ONLY on `MESSAGE_NOT_FOUND` —
the distinctly-classified "message to edit not found" `BadRequest` in
`message_sender._classify_bad_request` — meaning the message is provably
deleted; a generic `OTHER` edit failure (timeout / unclassified transient)
logs and leaves the persisted msg_id + render hash alone so the next sweep
retries the edit (review P2-2 — re-sending on a transient would orphan the
still-live old message, unboundedly). The same rule applies to the same-topic
`/dashboard` rerun. A topic-shaped outcome
(`TOPIC_NOT_FOUND`/`TOPIC_CLOSED`/`FORBIDDEN`) clears the record — never a
self-heal loop into a dead topic — and the **chat-scoped** teardown seam
`dashboard.clear_dashboards_in_thread(thread_id, chat_id=…)` covers the host
topic closing: thread ids are chat-local (review P2-3), so only the
`(chat_id, thread_id)` records are cleared (`chat_id=None` — genuinely
unresolvable — falls back to the old all-chats sweep WITH a warning, never
stranding a record silently). Wired from `cleanup.clear_topic_state` (chat
resolved via `group_chat_ids`) AND from `bot.topic_closed_handler`'s
no-binding branch (review P2-4): a dedicated dashboard host topic has no
bound window, so without that branch its record would survive close until the
send-failure backstop (the host may have no bound window, so binding-centric
cleanup alone would miss it; pre-C fix 3).

**🔔 unanswered-turn derivation**: a route renders 🔔 when `run_state` is
`WAITING_ON_USER`, OR when it is idle and
`snapshot.last_assistant_turn_ended_at > snapshot.last_user_turn_at` — two
WALL-CLOCK stamps on the same `time.time()` clock. `last_user_turn_at` is
mirrored into route_runtime INSIDE `message_queue.set_route_user_turn_at`
(single writer ⇒ same-ts by construction) at the PRE-SEND delivery seams;
`last_assistant_turn_ended_at` is written only by the authoritative
end-of-turn branch from the event's JSONL timestamp, max-monotonic by event
time (out-of-order resume/rewind events never regress it; `None` timestamp
never updates). Either stamp `None` ⇒ never classified unanswered — the
documented **restart degradation**: the stamps are in-memory, so after a
restart the dashboard renders state-only until fresh turns repopulate them.
Boundary: `dashboard.py` sends via `message_sender` helpers only and never
touches message-queue internals or mutates route_runtime. Visibility is
honest: owner-filtered, NOT private — any forum member can read the message.

## Rate Limiting

- `TypingAwareRateLimiter(max_retries=5)` (an `AIORateLimiter` subclass in `rate_limiter.py`) on the Application (30/s global)
- On 429, AIORateLimiter pauses all concurrent requests (`_retry_after_event`) and retries after the ban
- On restart, the global bucket is pre-filled (`_level=max_rate`) to avoid burst against Telegram's persisted server-side counter
- **sendChatAction exemption (2026-07-08):** `TypingAwareRateLimiter.process_request` presents a positive dummy `chat_id` to the classifier for `sendChatAction` only, so typing actions SKIP the per-GROUP bucket (20/60s) while KEEPING the overall 30/s limiter + the RetryAfter machinery. PTB classifies buckets purely on `data["chat_id"]` and ignores `endpoint`; a forum's negative chat_id otherwise routes each typing action through the same message budget as content — which paced multi-topic typing past its ~5s TTL (the indicator blinked with ≥2 busy topics) and starved content sends. Typing sends no message, so group-bucketing it is a classification artifact, not a Telegram limit. The real request body (in `args`) is untouched — `data` is classification metadata only (pinned by `test_rate_limiter.py` against a PTB upgrade). This completes the Fix-B true-cadence contract for multi-busy-topic forums.
- Status polling interval: 1 second (skips enqueue when queue is non-empty)

## Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache, skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records `last_byte_offset`, reading only new content. File truncation (offset > file_size) is detected and offset is auto-reset.

## No Message Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages) are always kept in full — no character-level truncation at the parsing layer. Long text is handled exclusively at the send layer: `split_message` splits by Telegram's 4096-character limit; real-time messages get `[1/N]` text suffixes, history pages get inline keyboard navigation.
