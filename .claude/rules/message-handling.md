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

**GH #46 PR-1 (agent-teams teammate park-close).** A Claude Code agent-teams
"teammate" session's background key stranded typing/Busy for up to 2 h. TWO
coupled defects: **(A)** a teammate's `idle_notification` report lands on the
PARENT transcript as a `type:"user"` text entry and USED to classify as a
GENUINE user turn — resetting `background_agents_done` (the re-record amplifier)
so the key relit and never dropped. **(B)** the teammate leg ends in plain text
(`stop_reason=None`), so the sidechain-done detector never fires, AND teammates
emit no `<task-notification>` — the key had NO close signal. **Fix (A):** the
teammate user event is machine-initiated. `utils.is_teammate_message` is stamped
by the adapter as `TranscriptLifecycleEvent.is_teammate_notification`;
`route_runtime`'s machine-initiated branch handles it identically to a
`<task-notification>` (preserved tombstones/stash/pane-bit + stored-idle
preserve; the notification clear stamps `TEAMMATE_NOTIFICATION`, ts-qualified).
**The shared bounded envelope scanner (review P2, r2-hardened):** predicate AND
parser both consume `utils.teammate_envelope_payloads` — byte-0 anchored
`Another Claude session sent a message:`, then EVERY structurally-valid
`<teammate-message>` envelope within the first 64 KiB of UTF-8 BYTES (never a
character count — a multi-byte payload must not stretch the bound). Per
envelope (r2, BOTH engines converged): the tag name must be followed by an
EXPLICIT whitespace/`>` delimiter (`\b` accepted `<teammate-message!broken>`);
the opening tag must COMPLETE via a QUOTE-AWARE `>` scan (a quoted `>` — or a
close token embedded in a quoted attribute — never completes it; the old
quote-blind `find(b">")` accepted a never-completed tag and produced a park;
a raw `<` ANYWHERE before the completing `>` — INCLUDING inside quote state —
REJECTS the opener: Hermes r3 P2 (a malformed opener must never borrow a LATER
opening/closing tag's `>` and then decode foreign JSON as its payload) + r4 P2
(an UNTERMINATED quoted attribute otherwise swallows a later tag boundary — the
quote state rides across it, a later quote char flips closed, and an unquoted
`>` completes on foreign text; legitimate CC attribute values never contain
`<`, so an in-quote `<` is always a crossed boundary — fail-closed));
the payload is decoded with `json.JSONDecoder().raw_decode` from the `{` that
must IMMEDIATELY follow tag completion (whitespace-only gap — Codex r3 P1: a
free-ranging `find("{")` could cross the envelope boundary and borrow FOREIGN
JSON from later text, stamping genuine-user text machine-initiated and minting
a park for a teammate the envelope never named; the immediate rule means the
payload start never crosses the current close tag or a following open tag).
raw_decode stops at the JSON value's TRUE end, so a
literal `</teammate-message>` INSIDE a JSON string no longer terminates the
envelope — a teammate summary quoting the tag now parses correctly; and the
structural close tag must follow the decoded JSON end (+ optional whitespace)
within the bound. Predicate-True now IMPLIES a decodable payload + structural
close — predicate/parser divergence is dead by construction. ACCEPTED
consequence (disclosed): an envelope whose body is not IMMEDIATELY a decodable
JSON object (e.g. a markdown teammate report) classifies as genuine-user
(unknown shape = human — the pre-GH#46 behavior); enumeration STOPS at the
first structurally-invalid envelope — including a non-JSON body (the r3 pinned
stop-on-invalid rule, consistent with the undecodable case; earlier valid
payloads kept). **Fix (B)
— the park-close lane:** `response_builder.parse_teammate_idle_notifications`
(PLURAL — one parent entry can carry MULTIPLE envelopes, real-data verified;
the second envelope of the live 15:56:55Z entry names the teammate whose park
is its ONLY close signal, review P1) filters the scanner's payloads to idle
notifications (`name` = `from`, `park_ts`); the monitor's parent user-text arm
resolves each name → this parent's currently-tracked TOP-LEVEL sidechain stem
key(s) (`sub:<parent>:agent-a<name>-<hex>`, hex 8-32 chars, read from
`tracked_sessions` — NO disk glob; a nested Fix-5 Workflow display key
`sub:<parent>:<runid>:agent-…` NEVER matches, hermes P3 — its close is the
`wf-task:` bracket; PR-1 closes ALL same-name top-level stems, documented safe
degradation) and records parks via `ParentSidechainActivity.merge_teammate_park`
— a CAUSAL REDUCTION, never last-write-wins (r2, BOTH engines: an unparseable
park DOMINATES the key permanently within the tick record — its
unconditional-tombstone evidence survives a later parseable park — else
max(park_ts) wins, so an older redelivered park can never bury the newer one
the downstream strict-newer gates need); the bot fan-out applies each as
`mark_background_agent_done(..., source=BgDoneSource.TEAMMATE,
end_turn_ts=park_ts, ...)`. `TEAMMATE` shares the `SIDECHAIN` cross-file
ts-gate (a stale prior-leg park keeps a resumed key LIVE) **PLUS the
stale-vs-activity gate — a deliberate PLAN AMENDMENT (r2, Codex P1; the v8
plan gated only on `resumed_event_ts`):** a PARSEABLE park strictly OLDER than
the record's own `last_event_ts` is SUPPRESSED (real-data verified: the
multi-envelope entry redelivers park #1 15:56:45.564Z after the key's
sidechain wrote 15:58:10.097Z — tombstoning there darkens a
demonstrably-working teammate mid-leg AND empties the key so the
genuinely-final park #2 15:58:10.253Z has nothing to close); a TIE
(park_ts == last_event_ts) TOMBSTONES (dark-safe — false-dark over
false-typing; the genuine final park is stamped ms after the final write, so
ties are overwhelmingly the genuine shape); an UNPARSEABLE park tombstones
unconditionally, a missing record / `last_event_ts=None` tombstones —
fail-closed to DONE. The stale-vs-activity gate is TEAMMATE-only; `SIDECHAIN`
semantics are byte-untouched. Pull-only; no observer (c313657 stays forbidden).

**GH #46 PR-2 (teammates as FIRST-CLASS background keys — the generational
registry, always-resumed relight-at-binding, wake lane, discovery-quarantine).**
PR-1 closed a
teammate's PARK; PR-2 makes the teammate a first-class background key so typing
stays ON while it genuinely works ACROSS the parent's own turns, relights when
re-woken, and drops promptly at park — WITHOUT ever stranding on a stale
same-name sidechain file. **route_runtime gains ZERO new mutators** — the
registry drives the EXISTING `launched` / `resumed` / `done`
(`BgDoneSource.TEAMMATE`) marks through the bot fan-out. **The structured
discriminators (`handlers/response_builder`, STRUCTURED-ONLY, five-way
disjoint):** `teammate_spawn_info_from_meta` keys on
`status=="teammate_spawned"` + a glob/regex-safe `name` (the id is snake
`agent_id`, NO camelCase `agentId` — verified 2.1.197 — so it is disjoint from
the plain-Agent async-launch lane; a metacharacter/over-long name fails DARK +
WARNs because the name feeds a `glob.escape` + `re.escape`); refuses any of the
four OTHER lanes' ownership fields (`agentId` / `taskId` / `backgroundTaskId` /
`resumedAgentId` — key-PRESENCE checks, never truthiness: a field present with
an empty/None value is still another lane's shape, dual-review r1 item 6b).
`teammate_send_target_from_meta` keys on `success is True` +
`routing.target == "@<name>"`, disjoint from the Fix-C resume lane
(`resumedAgentId`, verified 0-overlap). A prose `Spawned successfully.` line with
NO structured meta fires the rate-limited T1.6-analogue drift WARNING, never a
lift. **Result-before-use retro-pairing (dual-review r1 item 1, Hermes P1):**
Claude Code flushes a tool_result BEFORE its tool_use in 27/40 real session
files (the GH #42 ordering), and the parser hands such a result over with
`tool_name=None` — so the tool_name-gated spawn/wake branches could silently
drop the signal (registry never created / a parked teammate never relit). The
monitor now RETAINS the teammate-shaped parsed signal keyed by `tool_use_id`
(`_early_teammate_signals`, only spawn/wake-shaped metas — bounded, per-parent
drop-oldest cap, torn down with the parent) and applies it when the matching
Agent/Task/SendMessage `tool_use` arrives — whose entry carries the `input` the
wake cross-check needs, so the cross-check runs at retro-pair time; the spawn's
`spawned_ts` still anchors to the RESULT's event ts. The stash REPLACES an
existing id in place and evicts the oldest only for a genuinely NEW id at cap
(r2 P3 — evict-then-overwrite dropped an unrelated live signal); and the apply
seam ALSO clears the persisted parser pending-tool carry for the retro-paired
id (r2 P2 — the id's result was already consumed, so the `PendingToolInfo` the
parser stores for the LATE tool_use would otherwise retain its full input until
teardown, one leak per retro-paired spawn/wake; scoped strictly to the
retro-paired id — the normal in-order display pairing never reaches the seam).
**The registry
(`session_monitor`, per-parent `dict[name -> _TeammateRec]`):**
`_record_teammate_spawn` (anchored to the spawn tool_result's JSONL EVENT ts,
NOT the monitor's parse instant — the poll lags CC's write, so `time.time()`
would set `spawned_ts` after the genuine new-gen file's first entry and the
gen≥2 gate would reject it; an unparseable event ts falls back with a WARNING —
adversarial-review P1) creates a gen-1 rec OR ROTATES it — a same-name RESPAWN
(1) closes the old `current_key` unconditionally (an `end_turn_ts_unparseable=True`
teammate done — bypasses the resume/stale gates by design, never peeks runtime
liveness), (2) moves it to `retired_keys` + bumps `spawn_generation` /
`spawned_ts`, (3) QUARANTINES the genuinely-STALE matching stems tracked OR
present on a single anchored `glob(glob.escape("agent-a<name>-") + "*.jsonl")`
disk snapshot — GATED on the NEW generation's bind gate (adversarial-review P1
over-quarantine fix): only a stem that FAILS the gate (first entry predates the
new spawn ⇒ prior gen) is severed; a same-name file ALREADY on disk at rotation
whose first entry is ≥ the new spawn is the GENUINE new-gen file the poll lagged
and is LEFT for the normal binding path (a mid-write file too), (4) resets
`current_key` + clears the STICKY ambiguity flag (a fresh spawn is new
evidence) and **RE-FILTERS the pending signal slots instead of blind-clearing
them (r6 rule 2, Codex P1b,
probe-reproduced):** pending signals are timestamp-attributed — generation
membership is decided by the SHARED generation filter
(`_generation_filter_park` / `_generation_filter_wake`, the same rule the
orphan drain uses), never by which rec object happened to hold them; a signal
`>= the NEW spawned_ts` CARRIES into the new generation's slot (the blind clear
lost a newer-generation park that gen-1's registration had drained — two
stashed same-name spawns reduced to the newest park T4, gen-1 drained it,
gen-2's rotation cleared it → gen-2 bound without its close), older drops,
UnknownDone carries (dominance, fail-dark), then (5) a PRE-SPAWN scan
attempts binding on surviving tracked unbound stems, then (6) **pre-registration
key RETRACTION (r2 P1, BOTH engines, probe-reproduced):** an already-tracked
matching candidate may have fed run-state as a LEGACY agent BEFORE the spawn
parsed (no registry rec existed → classification returned legacy-True) — if
arbitration just left it UNRESOLVED or sticky-AMBIGUOUS, that already-recorded
runtime key would stay live until the 2h TTL while all future writes are dark
(the strand re-entry; with a post-turn pre-spawn tick this fully recreates
GH #46). Registration therefore emits an UNCONDITIONAL done for every tracked
matching candidate arbitration left unbound — WITHOUT retiring/severing it
(item 2: it stays bind-eligible; an extra tombstone on a never-live key is a
runtime no-op). **DISTINCT
retraction provenance (r3 P1, BOTH engines converged, probe-reproduced):** the
retraction rides the SEPARATE `ParentSidechainActivity.retraction_dones` slot,
NEVER the genuine-park lane — a synthetic `(None, True)` park in
`teammate_parks` shared the genuine unparseable dominance AND the fan-out's
parks-after-resumed order, so a SAME-TICK retraction→bind race (the candidate's
indeterminate first line completes between `check_for_updates` and the
sidechain scan, landing the bind's resumed relight in the SAME record)
permanently tombstoned the just-bound key. Two coupled guarantees: (a)
`_bind_teammate_key` CANCELS a same-tick pending retraction for the key it
binds (`retraction_dones.discard`, UNCONDITIONAL on every bind — a no-op when
nothing is pending; it operates on the per-tick `activity` record, NOT the rec,
so it is unaffected by the r7 `done_retracted_keys` deletion below — the
retraction's premise, "unbound at registration", is falsified by the bind);
(b) the fan-out applies ONE
record in CAUSAL order — **retraction-dones FIRST → resumed → activity →
genuine parks** (registration precedes bind precedes leg activity), each
retraction as `mark_background_agent_done(source=TEAMMATE,
end_turn_ts_unparseable=True)` (the same unconditional effect, zero new
mutators) — so even an uncancelled pair nets LIVE. Genuine unparseable-park
dominance in `teammate_parks` is untouched (a real park in the same record as
a resume still tombstones — pinned). **The relight constraint — ALWAYS-RESUMED
BIND (r7 item 3, Codex P2, probe-reproduced):** the runtime tombstone NO-OPS a
later `launched` (done-before-launch fail-closes), and EVERY teammate bind now
relights through the tombstone-POPPING RESUMED lane — `resumed[key] =
min(spawned_ts, first_entry_ts) - ε` (r8 item 1: the floor is the bound file's
OWN first entry, reducing to `spawned_ts - ε` in the normal case — see the
RESUME-TS FLOOR paragraph below) — for ALL binds, retracted or
not, never `launched`. The r6 code gated this on a monitor-side provenance set
`_TeammateRec.done_retracted_keys` (emit `launched` for a never-retracted key,
`resumed` for a retracted one), but **the monitor CANNOT observe route_runtime's
tombstone state (it is in-memory in a DIFFERENT module, pull-only), so ANY
bind-emission gated on monitor-side provenance is structurally unsound** — two
probe-confirmed holes left a positively-bound key tombstone-blocked → DARK: (i)
rotation CLEARED `done_retracted_keys`, so a stem retracted in late gen-1
registration that binds during gen-2 rotation emitted `launched` which no-ops
against its runtime tombstone; (ii) the dual-write fallback lane could tombstone
an already-tracked eventual key that was NEVER in the set → the same dark bind.
The ONLY uniformly-safe emission is the tombstone-popping lane. `resumed` ==
`launched` semantics on a fresh key (`is_background=True`, 2 h TTL, projected
RUNNING) PLUS a tombstone-pop + a `resumed_event_ts` stamp (the Fix-C
`mark_background_agent_resumed` justification — binding IS positive per-key proof
of new work); a fresh key with no tombstone treats resumed as a plain launch.
So the `done_retracted_keys` field, its membership gate, its rotation clear, and
its rec-side same-tick-cancel bookkeeping are **DELETED**; the retraction
emission + the `retraction_dones` slot + the per-tick same-tick cancel STAY. The
resume ts sits **STRICTLY BELOW** the generation's `spawned_ts` — the emission
is `min(spawned_ts, first_entry_ts) - TEAMMATE_RETRACT_RESUME_EPSILON_S` (1e-3;
the r8-item-1 floor below, reducing to r3's `spawned_ts - ε` in the normal
`first_ts >= spawned_ts` case; r3 item 2, Codex P1,
probe-confirmed): the runtime resume gate suppresses a TEAMMATE/SIDECHAIN done
with ts <= `resumed_event_ts`, so a resume ts of exactly `spawned_ts` stranded
a genuine park stamped at exactly `spawned_ts` to the 2h TTL (it passes the
item-4 generation filter, which only drops park_ts < spawned_ts — the "tie
tombstones" claim belonged to the TEAMMATE stale-vs-activity gate, and the
resume gate wins first). Safety walk: parseable parks < `spawned_ts` are
generation-dropped BEFORE the gate, so nothing in (resume_ts, spawned_ts) can
wrongly close via the PARK lane; a prior-gen SIDECHAIN end_turn ts landing
inside the 1ms epsilon window would tombstone — fail-dark (the accepted
direction) and vanishingly rare. A pending wake (necessarily newer) max-merges
over the relight resume ts. **RESUME-TS FLOOR (r8 item 1, Hermes P1,
probe-reproduced):** the stamp is `min(spawned_ts, first_entry_ts) - ε` —
strictly below the BOUND FILE's OWN first entry, not merely below `spawned_ts`.
The gen-1 bind gate tolerates a first entry up to `spawned_ts -
TEAMMATE_BIND_MTIME_SKEW_TOLERANCE_S` (5s) below the spawn (gen≥2 only 0.1s), so
an accepted look-alike candidate can bind with a first entry — and thus a
TRAILING sidechain end_turn ≥ that first entry, e.g. `spawned_ts - 2s` — BELOW
`spawned_ts - ε`; the r7 `spawned_ts - ε` stamp then SHIELDED that pre-spawn
end_turn at the runtime SIDECHAIN done gate (`end_turn_ts <= resumed_event_ts`
keeps the key LIVE), recreating the 2h strand where the pre-r7 `launched` path
fail-closed to DONE (no `resumed_event_ts` ⇒ a stale end_turn tombstones).
Flooring at the bound file's first entry makes EVERY signal in that file
(including its trailing end_turn, which is ≥ its first entry) STRICTLY NEWER than
the resume, so nothing from the bound file can be shielded. In the normal case
(`first_ts >= spawned_ts`, measured 1–7 ms after) the `min` reduces to
`spawned_ts - ε` — the r3 tie fix and the r7 semantics are preserved
byte-for-byte. The `first_entry_ts` is read at the gate
(`_teammate_bind_gate_passes` now returns `(gate, first_ts)`) and plumbed through
`_arbitrate_and_bind` to `_bind_teammate_key`; a gate-True result always carries
it, so the fallback (`spawned_ts - ε`, logged) is defensive only.
**Binding — SET-BASED arbitration (dual-review
r1 item 2, BOTH engines converged; `_arbitrate_and_bind`, shared by the public
`check_sidechain_updates` pre-pass `_arbitrate_teammate_bindings` AND the
pre-spawn scan — never sequential first-wins, so filesystem enumeration order
can never pick the "genuine" key):** each pass groups this parent's same-name
candidates (LONGEST-name-first stem resolution, pure-hex residual disambiguates
nested names; retired keys excluded) and evaluates ALL gates BEFORE any
`current_key` mutation. The gate = the **mtime prefilter**
(`st_mtime >= spawned_ts - TEAMMATE_BIND_MTIME_SKEW_TOLERANCE_S`, 5.0s) AND the
**first-entry-timestamp generation gate** (`_read_first_entry_ts`: a bounded
byte-capped read of the FIRST JSONL line — cap/no-newline/parse-fail/unparseable
⇒ INDETERMINATE, no bind this pass, RETRY while unbound, never consume-once;
gen-1 tolerates the larger `>= spawned_ts - TEAMMATE_BIND_MTIME_SKEW_TOLERANCE_S`,
gen≥2 tolerates only `>= spawned_ts - TEAMMATE_GEN2_FIRST_TS_TOLERANCE_S` —
**0.1s, FIXTURE-DERIVED (r1 item 5)**: across ALL 18 real 2.1.197 spawns the
teammate's first sidechain entry lands 1–7 ms AFTER the spawn tool_result,
never before, so 0.1s is 14× headroom over the max observed cross-flush gap and
rejects a stale sibling written even 0.2s pre-respawn). Gate-False (stale prior
gen) → quarantine + sever (deterministic); gate-None → unresolved (retry, NOT
quarantined); EXACTLY ONE gate-True → BIND; **>1 gate-True → bind NONE + WARN +
the rec goes STICKY-ambiguous** (the gate inputs are static, so the ambiguity
never self-resolves — it clears ONLY at the next rotation; the passing
candidates are NOT arbitrarily quarantined and stay out of run-state via item
3). On bind it relights the key via the ALWAYS-RESUMED lane at DISCOVERY (r7
item 3 + r8 item 1 — `resumed[key] = min(spawned_ts, first_entry_ts) - ε`,
never `launched`; once-only; a bound
file's later ticks feed run-state normally and never re-emit),
**retroactively generation-filters any pre-existing PARSEABLE park for the
bound key in the current activity record (r4 P2, Codex, probe-reproduced):** a
park recorded via the NO-REGISTRY fallback earlier in the SAME batch had no
`spawned_ts` to filter against (the generation didn't exist yet — a delayed T1
park followed by the T2 spawn in one batch), so the bind re-applies the item-4
floor — parseable `park_ts < spawned_ts` is dropped, UNPARSEABLE dominance
remains (fail-dark); the bind seam alone suffices because a registered name
filters at record time and an unbound candidate's key is tombstoned by the
registration retraction anyway — then
applies the buffered pending signals in CAUSAL order — `pending_wake` first (→
`resumed[key]`), `pending_park` second (→ the merge) — so the runtime ts-gates
arbitrate. **Run-state classification (`_teammate_feed_run_state`, r1 item 3,
BOTH engines):** for a REGISTERED teammate name, ONLY the bound `current_key`
ever feeds run-state ticks — a retired or occupied-newcomer candidate is
quarantined + severed; an UNRESOLVED (permanently-indeterminate gate included)
or sticky-AMBIGUOUS candidate is DARK without quarantine — so an unbound
candidate can never mint a background key that a genuine-user tombstone reset
could resurrect (pre-fix, an indeterminate gate fell through to
`feed_run_state=True` and an unbound candidate emitted SidechainTicks — the
strand re-entry). A name with no registry rec keeps legacy behavior. **Wake
(`_record_teammate_wake`, the `SendMessage` lane):** EVERY wake (registered or
not) FIRST lands a NAMED RAW copy in the orphan buffer UNCONDITIONALLY
(`_retain_orphan_teammate_wake`, r9 item 2, Codex P2, probe-reproduced — the wake
mirror of the r7 item-2 park RETAIN-ALWAYS/UNIVERSAL): a bound OLD generation used
to spend a not-yet-registered NEWER generation's only wake solely on itself —
gen-1 bound → gen-2 spawn result stashed → gen-2 parks at T4 (retained via the r7
park dual-write) → gen-2 wakes at T5 but the wake applied ONLY to gen-1's
`current_key` → the late gen-2 tool_use registers → the drained park (T4) closes
the fresh bind with NO surviving wake → tombstoned though T5 proved it resumed.
The retained copy carries the RAW `event_ts` (NOT the gen-1-`filtered` value), so
the drain re-attributes it against the FUTURE rec's `spawned_ts` and a wake `>=
gen-2 spawned_ts` carries into `pending_wake` and WINS the bind's wake-vs-park
arbitration (`pending_wake` first → `resumed[key]`); a stale wake `< gen-2
spawned_ts` is generation-DROPPED at the SAME drain filter (the r7 item-1 rule,
applied to the wake at the drain seam too). THEN the registered-rec path
GENERATION-FILTERS `event_ts` at the START via the SHARED
`_generation_filter_wake` — the SAME rule the orphan drain and the rotation
re-filter use (r7 item 1, Hermes P1, probe-reproduced): a `None` or
pre-generation (`event_ts < spawned_ts`) wake is REFUSED (INFO) BEFORE
`last_wake_ts` / `resumed` / `pending_wake` are touched. Without it, a
result-before-use wake stashed at a gen-1-era ts retro-paired onto the BOUND
gen-2 key and the runtime resume POPPED its park tombstone (a false relight of a
parked newer generation until the next park / the 2h TTL). Past the filter:
BOUND → `resumed[current_key] = event_ts`; UNBOUND → `pending_wake` (max on
repeats); the universal RAW retention above ALSO covers the NO-registry-rec case
(r6 rule 3, Codex P2, probe-reproduced: dropping a pre-registration wake made a
drained park tombstone a teammate whose LATER wake proved it resumed — false-dark
and a broken transcript-true arbitration; the retained wake is post-cross-check
evidence, max-on-repeats like `pending_wake`); the monitor cross-checks the
paired `SendMessage
input["to"] == <name>` (`transcript_parser` now carries `SendMessage` input
onto its tool_result; the shared `_apply_teammate_wake_crosschecked` also runs
on the item-1 retro path), a mismatch REFUSES + WARNs and an unavailable input
FAILS CLOSED (no wake). **Park (`_record_teammate_park`):** EVERY park (registered
or not) FIRST lands a NAMED retained copy in the orphan buffer via
`_retain_orphan_teammate_park` (r7 item 2, Codex P1, probe-reproduced — the r6
RETAIN-ALWAYS rule extended to ALL parks). A bound OLD generation used to SPEND
the NEW generation's only park: gen-1 bound → a gen-2 spawn result stashed
(result-before-use) → the gen-2 park arrives while the rec is STILL gen-1 (≥
gen-1's `spawned_ts`, not dropped) → it applied ONLY to gen-1's `current_key`
and was GONE → the late gen-2 tool_use rotated with `pending_park=None` → gen-2
bound without its close → the 2 h strand. The retained copy drains at the next
registration through `_generation_filter_park` (`park_ts ≥ gen-2 spawned_ts`
carries into gen-2's `pending_park` → closes at bind). The immediate application
to the bound rec KEEPS today's semantics for the OUTGOING generation (closing it
early is harmless — the rotation retires it anyway), and the buffer's causal
reduction makes the dual-write idempotent (a key binds exactly once, so no key
ever receives both the immediate close AND the buffer copy). Buffer-noise
consequence (disclosed): most retained copies are generation-dropped at the next
drain or TTL-expire unused — bounded by the existing `_ORPHAN_PARK_MAX_NAMES`
(32) cap + the `_ORPHAN_PARK_TTL_S` (2 h) wall TTL. THEN: name IN registry →
**generation scope first (r1 item 4, Codex P1): a PARSEABLE park whose
`park_ts` predates the CURRENT generation's `spawned_ts` is DROPPED (INFO) — it
reports the PRIOR leg going idle and cannot close a generation it predates
(pre-fix, a delayed prior-gen park buffered into `pending_park` after a
rotation tombstoned the FRESH key at bind, which had no activity/resume stamp
yet to defend it); an UNPARSEABLE park keeps unconditional dominance (it cannot
be generation-checked; fail-dark doctrine — disclosed residual: an unparseable
post-rotation stale park darkens the new gen until a wake / the next genuine
park)** — then close `current_key` only (bound) or buffer a TYPED
`_PendingPark` slot (unbound; `_merge_pending_park` — UnknownDone dominates
permanently, else max parseable ts, NEVER a bare tuple last-write-wins); name
NOT in registry → PR-1's all-tracked-stems close verbatim (the documented
no-registry degradation, e.g. a pre-restart spawn). The **UNCONDITIONAL
ORPHAN-RETENTION (r5 P1 + r6 rule 1 RETAIN-ALWAYS + r7 item 2 UNIVERSAL, all
probe-reproduced)** is the FIRST thing the method does above (the universal
dual-write beside every immediate close): retention gated on zero-match let a
tracked-but-INDETERMINATE stem (r6 A, Hermes P1) or a STALE same-name stem
(r6 B, Codex P1a) absorb the park as its own immediate close and SPEND the
eventual bind's only close signal, and r7 extended it to the registered-BOUND
branch (item 2 — a not-yet-registered NEWER generation's park was spent solely
on the currently-bound OLD generation); the immediate closes keep today's
semantics for THOSE stems, and the buffer's causal reduction makes the
dual-write idempotent-safe. In the r5 ordering (spawn tool_result
stashed → GENUINE park → late Agent tool_use registers → sidechain
discovered/bound) the park arrives before ANY anchor exists (a teammate's park
is its ONLY close signal; an unparseable orphan park was a dominance bypass
too). The buffer (`_orphan_teammate_parks`, the signal-lane mirror of the
item-1 spawn stash) holds an `_OrphanPending` **pending PAIR mirroring the rec
slots exactly** — park (causal-reduced via the SAME `_merge_pending_park`
rules) + wake (max-on-repeats, r6 rule 3) — per-parent, name-keyed, bounded
(`_ORPHAN_PARK_MAX_NAMES` 32 — replace-merge in place for an existing name,
**THREE-TIER oldest-first eviction only for a NEW name at cap (r8 item 2 → r9
item 1 → r10 item 1, CONVERGED P1 — Hermes + Codex, all probe-reproduced):**
since r7 made
EVERY park dual-write a named copy — including the high-frequency
registered/bound path — a busy multi-teammate parent churns the buffer with
copies, and a blind `next(iter(buf))` drop-oldest evicted the sole retained
pre-registration ORPHAN the buffer EXISTS to protect. The r8 two-tier fix keyed
the tiers on "name HAS a rec", but that MIS-tiered a park retained under a
STILL-REGISTERED name that belongs to a stashed not-yet-registered NEXT
generation (gen-1 bound, gen-2 spawn stashed, gen-2 park retained under the
registered name → the next gen's ONLY close) into the evictable tier (probe: 32
registered-stale parks + 1 new name evict it → gen-2 binds `pending_park=None`
→ 2h strand). **Fix (r9 item 1): REDUNDANT is redefined via the DRAIN FILTER'S
OWN semantics** (`_orphan_entry_is_generation_droppable` — redundant iff the
drain would generation-DROP the entry; the eviction predicate and the drain
filter must never disagree, mint/validate parity). An entry is tier-1 evictable
iff it has a rec AND EVERY retained signal is generation-dropped (a park with
parseable `ts < rec.spawned_ts` — and since `spawned_ts` is event-ts-anchored
and MONOTONIC across generations, any FUTURE generation spawns even later, so it
can close NO generation; a wake `< rec.spawned_ts`). **Fix (r10 item 1): r9's
tier 2 (evict the oldest PROTECTED entry when ALL survive the drain filter) is
an ORDINARY steady state** — r9's universal wake retention keeps every bound
teammate's own wake (`event_ts >= its spawned_ts`) protected even after its
immediate copy was applied, so a busy parent with 32 distinct bound names all
carrying a self-wake makes the buffer entirely protected and the r9 fallback
evicts a stashed NEXT generation's ONLY close (probe: a gen-2 park retained
under bound gen-1 name `future` + 31 ordinary bound-name wakes + a 33rd name →
`future` evicted → the late gen-2 spawn drains nothing → 2h strand; 33 DISTINCT
names within the 2h TTL, not concurrent teammates — realistic, 18 spawns in the
real incident corpus). The protected class is SPLIT into speculative vs provable
by whether the entry's value can close a not-yet-visible future generation:
**Tier 1** evicts the oldest
REDUNDANT entry (drain-droppable) FIRST; **tier 2** (only when tier 1 is EMPTY)
evicts the oldest SPECULATIVE entry (a name that HAS a rec AND has NO
spawn-shaped early signal in `_early_teammate_signals` — registered
SAME-generation noise whose signals were already applied to the bound
`current_key`; the retained copy only serves a future generation nothing proves
is pending); **tier 3** (LAST resort, only when tiers 1+2 are EMPTY — the TRUE
cap bound) evicts the oldest PROVABLE entry (no rec — a pre-registration orphan;
OR a name WITH a spawn-shaped early signal — a stashed next-generation spawn, so
the retained copy is PROVABLY that generation's ONLY close; an unparseable park
has no tier of its own — it protects its entry from tier 1, since the drain
would NOT drop it, but a registered/no-stash name is tier 2 regardless of
parseability). Tier 2 vs 3 =
speculative vs provable pending value; the eviction may only sacrifice provable
value at TRUE capacity. The stash probe is a bounded name-membership scan over
`_early_teammate_signals[parent]` (≤ the 64-entry cap; the parsed
`TeammateSpawnInfo` carries `.name`), precomputed ONCE per eviction call. The
`_orphan_entry_is_generation_droppable` seam reads `self._teammate_registry` +
the SHARED `_generation_filter_park` / `_generation_filter_wake` (same name-key
space, same filters as the drain) with a per-entry wall TTL
(`_ORPHAN_PARK_TTL_S` 7200s, mirroring the 2h background TTL the eventual key
ages by; lazy sweep at retain, expiry-discard at drain), torn down with the
parent. It DRAINS at registration (step 4.5, before the pre-spawn scan) into
`rec.pending_park` / `rec.pending_wake` — GENERATION-FILTERED at the drain via
the SHARED filters (a parseable orphan signal `< spawned_ts` is dropped, the
r4 case does not regress through the buffer; UnknownDone keeps dominance) — so
the bind applies both through the normal pending causal path (the runtime
resume gate arbitrates wake-vs-park) and the freshly bound key closes — or
stays LIVE when a newer wake proves resumption — instead of stranding. **Discovery-quarantine
severing (`_quarantine_teammate_stem`):** a same-name candidate that
DETERMINISTICALLY cannot bind (`current_key` occupied by a different key /
gate-False stale-prior-gen / retired) is retired + an UNCONDITIONAL teammate
done + PERMANENTLY severed from run-state tick emission — its `tracking_key`
joins `_severed_teammate_stems[parent]`, and the top-level loop passes
`feed_run_state=False` for it forever (still tailed for DISPLAY, the Fix-5
discipline). The sever is MONITOR-SIDE, so it is immune to a runtime tombstone
reset (a genuine user turn clears `background_agents_done` but a severed stem can
never re-record a tick) — with the item-3 unresolved-never-feeds rule, the
STRUCTURAL guarantee that NO non-current same-name key can ever be recorded
live (the sequential-ambiguity strand pin). **Teardown:**
the registry (incl. `retired_keys` + the sticky ambiguity flag) + the severed
set + the item-1 result-before-use stash die with the parent's tracking state
in `_remove_sidechains_for_parent` (session replacement / `/clear` / window
gone); NOT restart-reconciled — a mid-leg teammate is not relit after
kickstart (the disclosed degradation, same class as background-Bash T1.4b).
Binding is a HEURISTIC and intentionally fail-DARK when ambiguous (prefer
dark-until-next-signal over a wrong lift). Pull-only; no observer (c313657 stays
forbidden).

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
(single-select `aqp:` + review Submit/Cancel ONLY).** DIGIT MODEL — CORRECTED on
CC 2.1.207 (GH #50 rig, 2026-07-11): the v2.1.168-era claim "a bare digit only
MOVES the cursor" is DEAD. On 2.1.207 a bare digit is a live HOTKEY on every
single-select-SHAPED surface — it COMMITS the option with NO Enter (rig-confirmed
on AUQ single-select, ExitPlanMode, folder-trust, `Switch model?`); the 17 tested
non-digit single characters are inert, so the in-range digit set IS the complete
hotkey alphabet. AUQ MULTI-select digits still TOGGLE (rig-cleared ⇒ the shipped
`aqt:` lane is SAFE, the historical fast-follow is CLOSED). The
navigate→verify→Enter model stays correct, but its RATIONALE inverts: the digit is
not too WEAK, it is too STRONG — an unverified digit would commit the WRONG option
(and, under the original .168 reading, the form would stick and the bot would
wrongly record `dispatched` → an "Action already received" hard lock). It is also
why the GH #50 delivery gate refuses any payload whose emitted literal segments
carry a bare-digit LINE. Mechanism: `_dispatch_pick`
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
bare digit — rig-CLEARED as safe on 2.1.207 (multi-select digits TOGGLE).**
Validated against Claude Code v2.1.168 and re-characterized on 2.1.207 (GH #50).

## Inbound delivery gate — text on a live interactive surface (GH #50 PR-1)

`SessionManager.deliver_to_window` (and its legacy `(ok, message)` wrapper
`send_to_window`) is the **single choke point** every user payload crosses on its
way into a pane: typed text, a voice transcription, a photo/document caption, an
attachment-only bundle, a forwarded slash command, `/effort`, the `aql:` late
answer, and the pending-bind replay. Before GH #50 `text_handler` DETECTED a live
surface and sent anyway; the voice / photo / document handlers had **no check at
all**; and the aggregator flushes from a background debounce task, so any
offer-time check is TOCTOU. And the AUQ card literally invited the failure
(`(Type something — send a regular message to free-text)`).

**The four failure modes (CC 2.1.207 rig, `temp/rig-20260711-*`).**
`send_keys(literal=True, enter=True)` types the payload and appends Enter.

- **M1 — the Enter COMMITS option 1** (the default cursor row) on every blocking
  surface. Rig-verified worst cases: **ExitPlanMode** ⇒ the plan is APPROVED
  (option 1 is `Yes, and bypass permissions`; the plan file was actually
  written); **folder-trust** ⇒ trust GRANTED and persisted to `~/.claude.json`
  (live-reproduced); **`Switch model?`** ⇒ the model is switched and saved as the
  default.
- **M2 — a bare digit is a live HOTKEY** on a single-select-SHAPED surface (it
  commits with NO Enter). The v2.1.168 model recorded in CLAUDE.md ("a digit only
  MOVES the cursor") is **DEAD on 2.1.207**. *Rig-cleared:* AUQ **multi**-select
  digits still TOGGLE ⇒ the shipped `aqt:` lane is SAFE and needs no fix.
- **M3 — a bare-shell pane EXECUTES the payload.** `/esc` on a folder-trust prompt
  EXITS Claude, leaving a shell in a still-bound window — and `/esc` bypasses
  `send_to_window`, so only `/update` failures used to quarantine.
- **M4 — the bot is BLIND to `Switch model?`** (footer-less ⇒
  `parse_generic_decision` returns None). A live blocking prompt the parser cannot
  see. **This is why the gate must not be "no known prompt matched".**

**The gate is POSITIVE structural evidence** (`terminal_parser.pane_input_box_present`,
five legs, fixture-pinned on 2.1.207 — a TUI-drift audit surface beside
`clean_ghost_input_text` and `pane_command_is_claude`):

1. the BOTTOM pair of `──` rule separators is present;
2. a genuine prompt row sits inside that pair — the glyph is **`❯` OR `!`** (in
   bash mode it is `!`; a `❯`-only leg would refuse EVERY `!command`) — and the
   **FIRST** such row must NOT match `^\d+\.\s` after its glyph (**the picker
   trap**: a live AUQ picker's bottom rule-pair CONTAINS its `❯ 1. Red` option row,
   so legs 1+2 would otherwise BOTH pass on a live picker). **The trap is
   FIRST-ROW-ONLY and PAYLOAD-AWARE (r2 F1).** Unqualified it FALSE-REFUSED any
   message starting with `1. ` — the gate writes the payload and re-verifies
   AFTER, so an ordinary `1. buy milk` renders the box as `❯ 1. buy milk`, the trap
   fired at the re-verify, the Enter was withheld, and the message was NEVER SENT
   (it just sat as a draft; reproduced directly). Fix: `pane_input_box_present` /
   `classify_input_box_failure` take an optional `expected_draft`, passed ONLY at
   the re-verify; when the first prompt row IS that draft (glyph-stripped, exact or
   the wrapped-prefix shape) the trap is SKIPPED — POSITIVE PROOF of authorship,
   since a picker that stole the keystrokes would show ITS OWN label, never our
   text. The PRE-write gate passes no `expected_draft` and keeps the trap
   unconditional (no payload is in the box yet; a `❯ 1. …` row there is a live
   picker or a HUMAN's own numbered draft — refusing is fail-closed, the disclosed
   residual). **The trap is DEFENCE IN DEPTH, not the load-bearing leg — MEASURED:**
   with it disabled ENTIRELY, every blocking pane in the 2.1.207 corpus is still
   refused by another leg (the AUQ single picker by leg 3 `no_ready_chrome` — a
   live picker replaces the ready status bar with its own `Enter to select` footer;
   every other family by leg 1 `no_input_box`) and every deliverable pane still
   passes (pinned by `test_option_row_trap_is_redundant_on_the_real_corpus`). It is
   kept only for a hypothetical picker variant that renders ready-chrome below its
   own footer;
3. ready-for-input status chrome is present BELOW the box, from the observed
   alphabet (`⏵⏵ … (shift+tab to cycle)`, `esc to interrupt`, `← for agents`,
   `· N shell`, `↓ to manage`, `? for shortcuts`, **`! for shell mode`** in bash
   mode, and — **the PASTE-COLLAPSE, see below** — **`paste again to expand`**);
4. the status bar must NOT carry **`Enter to view tasks`** — one `Down` at an empty
   box while a background shell exists arms a mode where legs 1-3 ALL still pass but
   **Enter is STOLEN** (typed text is swallowed entirely; Enter opens the
   Shell-details modal). Reachable in production — the bot's own ungated nav
   keyboard sends `Down`. (Esc reverts it.)
5. no input-capturing completion overlay: the overlay fires ONLY when the cursor
   token is an active trigger — a trailing **`@prefix`** (`please ask @se` ⇒ Enter
   selected `seed.txt` and the message was NEVER sent — **live today**: any Telegram
   message ending in `@word` strands unsent) or a bare **`/prefix`** (`/co`). A
   mid-text `@alice`, an email address, and `tell me about / division` do NOT
   trigger it, and a slash command WITH an argument (`/effort high`) raises no
   overlay at all.

**THE PASTE-COLLAPSE — a paste-collapsed box is a READY box (the PR-1 regression,
rig-reproduced 2026-07-11, fixtures `inputbox_paste_collapsed_v2.1.207.txt` +
`inputbox_paste_collapsed_reverted_v2.1.207.txt`).** A payload written in ONE
`tmux send-keys -l` past **~800 chars / ~13 lines** is consumed by CC as a **PASTE**,
and CC then does TWO things: (a) it collapses the input row to a placeholder
`❯\xa0[Pasted text #1 +12 lines]`, and (b) it **REPLACES THE STATUS BAR** with the
single line `  paste again to expand`. For ~2s NONE of leg 3's other markers is on
the pane, so leg 3 returned `no_ready_chrome` and the post-write **re-verify** —
which fires at `TEXT_SETTLE_S` = 0.5s, squarely inside that window — concluded
there was no input box. Every long / multi-line message (a voice note carrying a
reply-context quote; the owner's live report was 809 chars) was REFUSED, left as a
**stranded draft**, and **braked the topic** — even though the box was right there
holding the text with Enter ready to submit it. It IS ready-for-input chrome (box
present, cursor in it, Enter submits), so `paste again to expand` joins leg 3's
alphabet. ~2s later CC restores the normal mode line while the collapsed draft
remains (the `_reverted` fixture — the shape the owner's pane was left in); that
one already passed and is pinned as a non-regression.

*Why widening leg 3 cannot let a blocking prompt through (MEASURED, not asserted).*
A blocking prompt **REPLACES** the input box, so it fails **leg 1** (`no_input_box`)
or **leg 2** (`prompt_row_is_option`) regardless of what leg 3's alphabet says — leg
3 is not the leg that refuses gates. `test_paste_hint_below_a_blocking_pane_still_refuses`
adversarially APPENDS the paste hint below every blocking fixture in the corpus and
every one still refuses.

*The shared-constant question (answered, no split needed).* The marker goes in
`_INPUT_READY_CHROME_MARKERS` (the input-box lane's extension tuple) and
**deliberately NOT** in `_READY_STATUS_MARKERS` (the IDLE-status-bar alphabet
`pane_looks_idle` / `classify_pane_idle_failure` consume): a paste-collapsed pane is
**not idle** — it holds an uncommitted draft, so `/update` must still defer (a
restart would discard it) and `/cost` must still refuse. (`pane_looks_idle`'s
empty-input-row leg already rejects it, so this is the semantically correct split,
not a behavior change; pinned by `test_a_paste_collapsed_pane_is_NOT_idle`.) The
interactive-**GATE**-rejection lane (`_only_chrome_below`) consumes **no marker set
at all** — it is a structural ALLOW-LIST (blank / bare separator / the gate's own
`ctrl+<x>` hints) — so the paste hint ALREADY rejects a quoted gate rendered above a
live paste-collapsed box, which is exactly right: the hint PROVES the input box is
live, so the "gate" above it is not the active bottom prompt (pinned by
`test_the_paste_hint_rejects_a_QUOTED_gate`). The two lanes were already
structurally independent; nothing was coupled and nothing had to be split.

**The NON-BREAKING SPACE (load-bearing, now normalized + pinned).** The empty input
row is `❯\xa0` and the paste-collapsed row is `❯\xa0[Pasted text #1 +12 lines]` —
**U+00A0**, not ASCII U+0020. The code coped only *incidentally* (`str.strip()`
drops NBSP; Python's Unicode-aware `\s` matches it), and that incidental behavior
decides whether the input row reads EMPTY — the stranded-draft brake's ONLY release
condition. It is now folded explicitly by `_normalize_input_row`, applied at the
SINGLE seam `_input_box_rows` — the one path every input-box-lane reader goes
through (`_prompt_row_content` / `_completion_overlay_armed` /
`classify_input_box_failure` / `pane_input_row_empty`). It deliberately does **not**
touch the rule-separator scan, the chrome region below the box, or any other parser:
a global NBSP fold would change unrelated matching (option labels, gate footers,
prose). Pinned on the real captured rows. Past user turns also render with `❯`, so
the bottom-rule-pair anchoring stays load-bearing.

**The rule separator may be LABELLED (CC 2.1.207, fixture-pinned).** A few seconds
after a plan is approved, CC pins the plan slug into the input box's TOP rule
(`──────… add-ok-to-note ──`) and it PERSISTS for the rest of the session (only
`/clear` drops it). `_RE_RULE_SEPARATOR` matched pure dashes only, so
`_input_box_rows` could not find the bracket at all — which broke
`pane_input_box_present` (the gate would have refused EVERY message in that topic)
**and, PRE-EXISTING and shipped long before GH #50, `pane_looks_idle`: `/update`
silently deferred and `/cost` refused in any topic where a plan had been
approved.** The regex now tolerates a labelled rule; both predicates are pinned on
the real post-resolution rig captures (`epm_after_approve_*`, `epm_plan_label_*`,
`auq_after_answer_*`, `trust_after_accept_*`, `control_gitrepo_branch_no_label`,
plus the live-prompt positive controls `*_before_*`).

**Deliberately NOT asserted:** no-active-run, background-shell absence, and
input-row emptiness. **Queueing a message while Claude is BUSY is a first-class
flow** and MUST keep working (rig design-killer A2: the rule-pair + prompt row +
ready chrome persist through EVERY busy shape), and a pre-existing / soft-wrapped /
multi-line draft must still deliver (rig D10: continuation rows carry NO glyph). So
this is **not** `pane_looks_idle` and `clean_ghost_input_text` is NOT needed here
(it only matters for emptiness — dropped as cargo-cult).

**Why the inversion works** (rig-confirmed on all six blocking families — AUQ
single + multi, ExitPlanMode, folder-trust, `Switch model?`, Permission, Workflow):
a live blocking prompt **REPLACES** the input box + status chrome. The positive
proof therefore fails on *every* prompt — known, unknown, unparsed, half-drawn —
without the parser recognizing it. The gate never consults `_active_ui_patterns`,
so it is **flag-independent by construction**: `CC_TELEGRAM_PERMISSION_PROMPTS` /
`CC_TELEGRAM_DECISION_CARDS` cannot reopen the hole. The recognizer probes
(`is_interactive_ui`, `parse_unknown_blocking_prompt`, and the recognizer-free
`pane_blocking_prompt_shape` bottom-cursor-row check) are **purely a LABELLING
aid, and the ordering ENFORCES it (r1 P1, probe-reproduced)**: `_input_box_reason`
consults the positive proof FIRST and returns immediately when it passes; the
recognizers run ONLY on an already-FAILED, INDETERMINATE reason, and only to
upgrade it to the actionable `prompt_present` copy ("answer the card first")
instead of burning the retry budget on generic "couldn't confirm the input box".
They may **never pre-empt the proof**. Two independent reasons: (a) they buy NO
safety — across all 25 real 2.1.207 pane fixtures the positive proof ALONE refuses
every blocking surface (all six gate families, the bare shell, the /cost overlay,
both completion overlays, the tasks mode) and passes every deliverable shape; and
(b) pre-empting is a FALSE REFUSAL of legitimate messages, in front of EVERY
inbound message. The concrete case: an **ANSWERED** AUQ / ExitPlanMode prompt whose
rendering is still on-screen ABOVE the restored input box still matches
`is_interactive_ui` — the AUQ/EPM `UIPattern`s carry no strict validator, so unlike
Permission/Workflow/Decision they have no `_only_chrome_below` guard — and
pre-empting there refused EVERY message in the topic until the picker scrolled off.
`pane_blocking_prompt_shape` already documented this discipline ("Only consulted
when the input-box proof has ALREADY failed"); the other two now follow it. Pinned
by `test_answered_prompt_above_a_live_input_box_still_delivers` +
`test_positive_proof_alone_refuses_every_blocking_surface`.

**The transaction** (inside the EXISTING `window_send_lock`, beside the `/update`
quarantine re-check — every step fail-closed):

0. **The SEGMENT-aware, PER-LINE lone-hotkey refusal** (`delivery.lone_hotkey_line`):
   refuse if ANY LINE of ANY literal segment the writer will actually emit is an
   ASCII `[0-9]` fullmatch (**ASCII, not Python `\d`** — Unicode digits are not
   terminal hotkeys). SEGMENT-aware because the `!` writer emits `"!"` and the
   remainder as SEPARATE literal writes, so `!1` passes a payload-level test yet
   emits `"1"` as its own write (rig C7: CONFIRMED FIRES). PER-LINE because a
   bare-digit LINE inside a multi-line single write ALSO fires (rig §5 finding 3:
   `first line\n2\nthird line` written as ONE `send-keys -l` **COMMITTED option 2**
   on a live picker). Fires BEFORE any capture — never written, even onto an idle
   pane (the gate→write window is exactly what makes a digit dangerous). `"12"` and a
   digit embedded WITHIN a longer line are delivered — an empirically narrowed,
   **NON-proof** case (pty chunking could still split a write), disclosed rather
   than closed. Rig C8 RESOLVED the alphabet: 17 single non-digit characters
   (`a y n q z Y N space - ? . ,` …) fire nothing and move nothing; out-of-range
   digits are inert; digit `4` (the `Type something.` row) selects the free-text row
   ⇒ it stays in the refusal set.
0b. **The RAW-CONTROL-BYTE refusal** (`delivery.unsafe_control_char`) — also before
   any capture. **`send-keys -l` is not a sanitizer.** `-l` stops *tmux* interpreting
   KEY NAMES; it does NOT make C0/ESC bytes inert to the program on the other side of
   the pty. **RIG-CONFIRMED** (`tmux -L ccrig`, `cat -v` in the pane): a payload built
   with `printf 'A\033[B2B'` lands as the literal bytes `A^[[B2B` — so Claude's TUI
   reads `A`, a **CURSOR-DOWN escape sequence**, then `2`. That is a complete commit
   primitive: `lone_hotkey_line` cannot see it (the line is not a lone digit), the pane
   gate has already passed, and the re-verify (step 5) runs *after* the write — so an
   embedded `ESC [ B` + digit moves the cursor off the row that was proved and fires a
   digit HOTKEY (which on a single-select-shaped surface COMMITS with **no Enter**)
   before anything re-observes the pane. It is refused outright at this ONE gated seam
   — `deliver_to_window` owns the single refusal, so exactly one ❌ reaches the user.
   The byte set is **everything in C0 except LF**, plus DEL and C1 (`U+0080–U+009F`,
   which a UTF-8 terminal decodes back into the C1 control range). **`\n` is ALLOWED
   and load-bearing** — a payload written in ONE `send-keys -l` is consumed
   PASTE-SHAPED and commits whole (rig: 947-char/9-line and 5 274-char/30-line payloads
   both landed intact), and every voice note and reply-context quote is multi-line;
   newline handling is **untouched** by this rule. `\t` and `\r` are REFUSED
   deliberately: Tab is a live TUI *key* (it advances a picker; it drives completion in
   the input box) and CR **is Enter at the pty** — an embedded one would commit
   mid-payload. **Disclosed cost:** a pasted tab-indented code snippet is refused, with
   actionable copy. Stripping or converting the bytes would silently change what Claude
   receives, which is worse than an honest refusal.
   `free_text.try_answer` consults the SAME predicate to DECLINE (it never
   refuses — the PR-1 gate it falls through to owns the message): one rule, one
   owner, exactly one ❌.
1. **A bounded, cancellation-safe capture** (`capture_pane_cancellation_safe` under
   `asyncio.wait_for`; ONLY `asyncio.TimeoutError` classifies — a genuine
   caller/shutdown cancellation PROPAGATES, never swallowed into a refusal), plus an
   overall transaction budget checked at the phase boundaries (**never** a `wait_for`
   around the WRITE — cancelling mid-write would strand a half-typed payload;
   exhaustion before the write ⇒ `not_written`, after ⇒ `draft_written`).
2. **`pane_command_is_claude`** — the strict version-string fullmatch, now on EVERY
   send (not just quarantined windows), on a **BOUNDED** probe (r2 F4:
   `pane_current_command` shells out to tmux with no timeout of its own; only
   `asyncio.TimeoutError` classifies → `cmd_probe_timeout`, a genuine cancellation
   PROPAGATES). Closes M3. A quarantined window keeps its EXACT
   `QUARANTINE_SEND_REFUSED_MSG` contract string.
2b. **The stranded-draft brake** (r2 F2, below) — zero cost (one set lookup) unless
   the window is braked.
3. **`pane_input_box_present`** — with a bounded RETRY on an INDETERMINATE frame
   (`capture_empty` / `no_input_box` / `no_prompt_row` / `no_ready_chrome` — a
   mid-redraw), and an IMMEDIATE refusal on a POSITIVE hazard
   (`prompt_row_is_option` / `tasks_mode` / `completion_overlay` / `prompt_present`),
   exactly one capture. The /cost preflight precedent.
4. **The write with the Enter WITHHELD.** A **mode-aware writer** reproduces the `!`
   bash-mode two-step explicitly (send `!` → settle → send the remainder), because
   `send_keys` performs its own two-step ONLY when `literal and enter` are BOTH true
   (`tmux_manager.py:782`) — calling it with `enter=False` would silently take the
   generic path and change bash-mode behavior. **EVERY post-write-attempt failure is
   classified WRITTEN (r2 F5)** — a `False` from `send_keys` does NOT prove zero
   bytes reached the pane (tmux may have failed after writing; and a later segment's
   failure certainly leaves the earlier ones there), so the old `written = i > 0`
   was an unproven claim. Fail-closed: it arms the brake, whose empty-input-row
   self-heal releases it if nothing actually landed.
5. **The RE-VERIFY** (`session._reverify_input_box`) immediately before the commit:
   `pane_command_is_claude` AND `pane_input_box_present` still hold. This is the
   window the re-verify genuinely closes. **ORDER IS LOAD-BEARING (r2 F4):** the
   bounded command probe runs FIRST and the pane CAPTURE is the **LAST** observation
   before the stamp + Enter. The old order captured the pane, then awaited an
   UNBOUNDED `pane_current_command` — a probe stalling while a blocking prompt was
   drawn let a STALE input-box frame authorize the Enter (which commits option 1).
   **It carries the SAME bounded INDETERMINATE retry as the pre-write gate**
   (`GATE_CAPTURE_RETRIES` × `GATE_RETRY_DELAY_S`; the order above is
   RE-ESTABLISHED on every attempt — a retry that re-captured WITHOUT re-probing
   would let a 0.3s-stale liveness proof authorize the commit). It originally had
   **no retry at all** and refused on the FIRST non-None reason, so a single
   mid-redraw frame false-refused **and** stranded the draft **and** braked the
   topic — the most expensive failure in the transaction (the pre-write gate merely
   declines; this one leaves state behind). **A POSITIVE hazard STILL refuses
   immediately, on exactly one capture, with zero further keystrokes** — a real
   prompt drawn in the gate→write window (`prompt_present` / `prompt_row_is_option`),
   the Enter-stealing `tasks_mode`, an armed `completion_overlay`, or a pane that is
   no longer Claude (`not_claude`). That is the safety property and the retry never
   weakens it. The overall deadline is re-checked
   after every await. From here on ANY failure is **`draft_written`** — the text
   sits in the input box and the Enter is withheld — with **NEUTRAL** copy ("the
   terminal changed while your message was being typed; your text was NOT
   submitted"), because a post-write structural failure does NOT prove a prompt
   appeared (it may be a `/`-command overlay, bash-mode rendering, wrap drift, a
   capture failure, or an ordinary redraw). **NO automatic cleanup is attempted** —
   Esc / Ctrl-U have surface-specific semantics and **Esc on folder-trust KILLS
   Claude**. The re-verify is PAYLOAD-AWARE (`expected_draft=text`, leg 2 above), so
   an ordinary `1. buy milk` is not mistaken for a picker cursor. A bare `/command`
   payload legitimately arms the `/` completion overlay once written and Enter runs
   the sorted-first entry (the mechanism `forward_command_handler` has ALWAYS relied
   on), so the re-verify exempts the `/` arm for exactly that shape
   (`delivery.is_bare_slash_payload`) **AND ONLY when the input row's content IS
   that exact payload (r2 F6)** — keyed on the payload SHAPE alone the exemption
   also covered a PRE-EXISTING `/co` draft a human left in the box, so Enter would
   have run `/copy` on text the bot never authored. The exemption demands the EXACT
   first line, never a prefix (a prefix is precisely the hazard). The `@` arm is
   NEVER exempt — it is pure data loss. The bare-ambiguous-prefix misfire itself
   (`/co` + Enter ran `/copy`, live-reproduced) is **GH #53, filed separately and
   explicitly out of scope**; the narrowing only refuses to WIDEN it.
6. **The pre-commit user-turn stamp** (see below).
7. **Enter.** A `False` from the Enter `send_keys` does NOT prove the key never
   reached the pty, so it is **`COMMIT_UNKNOWN`** (r2 F3), never "draft_written"
   (which asserts a deliberate withhold). Honest copy: "Your message may or may not
   have been submitted — check the window (`/screenshot`) before resending." The
   turn stamp STANDS for it (see the invariant below), and it arms the brake: if the
   Enter did not land the draft IS stranded, and if it did the empty-input-row
   self-heal releases the brake on the next send.

**The stranded-draft brake (r2 F2) — the commit chain the gate itself created.**
A `draft_written` / `commit_unknown` transaction leaves the payload sitting in the
input box with its Enter withheld, and the user is TOLD it was not delivered. But a
live input box holding a pre-existing draft is legitimately DELIVERABLE (rig D10, a
hard non-regression) — so the NEXT message passed the gate, was APPENDED to the
stranded text, and its Enter committed BOTH: silently submitting a message the bot
had already disclaimed, concatenated with the new one. Two coupled fixes:

**THE BRAKE IS ABOUT A STRANDED *DRAFT*, NOT ABOUT "ANY KEYSTROKE" (round-5 P2, the
finding we DECLINED).** The reviewer asked for the brake to be armed at the free-text
lane's ARROW keys too, because `_WriteAttempt` is set only before the literal payload
write, so a post-nav bail leaves the cursor moved without arming it. That is correct
as an observation and wrong as a fix. The hazard the brake exists to break is
`stranded text` + `the next message's Enter` ⇒ **both** committed. **Arrow keys leave
no text**, so there is nothing to append to, and the pane they touched is a LIVE CARD
— which PR-1's gate refuses for every subsequent payload anyway (`no_input_box` /
`prompt_row_is_option`), so the chain cannot form. Arming the brake there would be
actively harmful: while a card owns the pane there IS no input row, so the brake's
only release proof (`pane_input_row_empty`) is permanently INDETERMINATE and the topic
would be **WEDGED** — every later message refused with *"an earlier message is still
sitting UNSENT in this window's input box"*, which would be a **lie**, until the user
kills the window. The nav is also `Down`-only by construction (the affordance row is
the LAST row, so `delta = target − cursor ≥ 0`), which is what keeps it away from the
one arrow that could put text anywhere: `Up` recalls history INTO a restored input
box. `Down` into a stray input box can at worst arm the `Enter to view tasks` mode,
which PR-1's gate leg 4 positively detects and refuses with the Esc instruction. Pinned
by `TestArrowKeysAreNotADraft`.

  - **(i) Callers STOP on the first non-OK result.** `aggregator_replay_payload`
    used to keep sending the remaining split bundles after a refusal (so split 2
    would be typed onto split 1's stranded text); it now aborts and returns the
    first refusal. The four FORCED-flush callers ignored the returned
    `DeliveryResult` entirely — `bot.forward_command_handler` (the §2.8 pre-flush),
    `callback_dispatcher/effort.py`, `callback_dispatcher/late_answer.py`, and the
    replay's own split loop — and each now ABORTS its own subsequent send when that
    flush refused, surfacing the real reason (the `aql:` card is re-armed with its
    original keyboard for the retry). **Refusal OWNERSHIP is therefore explicit and
    single (`report_refusal`, peer-review P2):** a caller that inspects the result
    and posts its own ❌ would otherwise get a SECOND ❌ from the aggregator for the
    same event (buffered message + an immediate slash command while Claude is
    blocked ⇒ the forced flush refuses, and BOTH disclose). The FIRE-AND-FORGET
    flushes (the debounce timer, the media-group boundary, the attachment cap)
    keep reporting inside `_send_bundle` — nobody is awaiting their result and the
    photo/document handlers already acked "sent"; the SYNCHRONOUS forced-flush
    callers (the three above) and the pending-bind replay (whose own callers
    surface the reason in their bind edit) pass `report_refusal=False` and own the
    single response. No path drops a refusal silently — it is reported either by
    the aggregator or by the caller that suppressed it, never by both.
  - **(i-b) A RAISED delivery is a refusal too (peer-review P2).** THE INVARIANT:
    **every refusal — from a RETURNED `DeliveryResult` OR from a RAISED exception —
    reaches the user EXACTLY ONCE, on every flush path.** The `report_refusal` fold
    itself broke the RAISE half: `_send_bundle`'s `except Exception` arm built its
    result and **`return`ed immediately, jumping over the reporting block**. The
    debounced / media-group-boundary / attachment-cap flushes are FIRE-AND-FORGET —
    nobody awaits that result — so the popped payload vanished with only a log line
    and the user was never told: the exact OPPOSITE failure of the double-report the
    fold was introduced to fix. It matters doubly now, because a raise PAST a write
    attempt also arms the stranded-draft brake (`session._WriteAttempt`), so the
    user must be told why their NEXT message will be refused too. The arm now
    ASSIGNS `result` and FALLS THROUGH to the single reporting seam (the NEUTRAL
    written-state copy: the raise may have landed before or after the payload was
    typed, and "if you see it in the input box, clear it" is the right advice for
    both). `report_refusal=False` still transfers ownership on the raise path — the
    synchronous caller receives the structured result and posts the single ❌.
    `CancelledError` is a `BaseException`: it is NOT caught, must never be swallowed
    into a `DeliveryResult`, and must never be posted as an ordinary refusal.
  - **(ii) A per-window brake** (`mark_window_stranded_draft` /
    `window_has_stranded_draft` / `clear_window_stranded_draft`, the registry living
    in `tmux_manager` beside the post-/exit quarantine it mirrors — see (ii-b) for
    WHY; `session.py` keeps the four names as the delivery-path vocabulary): a
    `draft_stranded` outcome MARKS the window; while marked, `deliver_to_window`
    REFUSES with the
    `stranded_draft` reason + actionable copy ("an earlier message is still sitting
    UNSENT in this window's input box … clear it (Esc, or Ctrl+U), then resend. /esc
    sends that Escape for you — but if Claude is mid-run it will ALSO interrupt the
    run"). **NOTHING is auto-cleared** (Esc on folder-trust KILLS Claude; mid-run it
    interrupts) — the cost is stated, not hidden. The brake is released ONLY on
    POSITIVE proof: one extra capture whose `terminal_parser.pane_input_row_empty`
    is True (ANSI-cleaned via `clean_ghost_input_text`, so a CC ≥2.1.206 DIM ghost
    suggestion never strands it forever); an INDETERMINATE frame — a capture
    failure, a mid-redraw, a live prompt, or a picker-shaped prompt row — KEEPS it.
    Zero cost for an unbraked window (one dict lookup).
  - **(ii-b) BINDING-LEVEL TEARDOWN MUST NOT CLEAR IT — the brake is a property of
    the PANE, and its only other release proof is WINDOW DEATH (peer-review P1).**
    Round 1 dropped the brake at `cleanup.clear_topic_state` + the four
    `inbound_telegram` stale-window unbinds, beside the tmux quarantine those seams
    already drop. That re-opened the exact commit chain the brake exists to break:
    delivery A writes its payload, fails the re-verify and arms the brake INSIDE
    the send lock; concurrently `/unbind` — which **deliberately leaves the tmux
    window RUNNING** — runs `clear_topic_state`, which cleared the brake with **no
    synchronization against `window_send_lock`**; send B (an already-popped boundary
    flush, or a slash command), BLOCKED on that same lock the whole time, then
    acquires it, finds a structurally valid input box that still holds A's draft,
    appends its own payload and presses Enter — committing BOTH, including the one
    the user was told was NOT delivered. Unbinding a topic says nothing about
    whether the draft is still in the box. So the registry **moved into
    `tmux_manager`** (beside the post-/exit quarantine it mirrors:
    `mark_window_stranded_draft` / `window_has_stranded_draft` /
    `clear_window_stranded_draft`; `session.py` keeps the four names as the
    delivery-path vocabulary + the test seam) and is released by exactly two proofs:
    the empty-input-row capture above, or **proof the WINDOW IS DEAD** — a
    **CONFIRMED `kill_window`** (gated on the `True` return for the same reason the
    send lock is: a FAILED kill can leave the window alive with the draft intact),
    or **`create_window`** minting a brand-new window under that id (tmux ids RESET
    to `@0` on a tmux-SERVER restart, which a launchd-kept bot process outlives, so
    an entry armed on the old `@0` could otherwise meet a fresh `@0`). Topic close
    and `/kill` DO kill the window, so the brake still drops there — at the kill,
    under the right proof. **Disclosed residual:** a window that dies WITHOUT a
    `kill_window` (an external `tmux kill-window`, the poller's stale-binding path)
    leaks an entry. It is inert, not a wedge — `_deliver_locked` refuses
    `window_gone` on `find_window_by_id` BEFORE it ever consults the brake, and the
    empty-box self-heal reclaims any id later reused.
  - **(iii) The brake is armed through ONE seam, and a CANCELLATION after a write
    arms it too (peer-review P1).** Arming only from the RETURNED `DeliveryResult`
    left the F2 hazard reachable through the cancellation door: a `CancelledError`
    (or any unexpected raise) during the settle, the re-verify, the user-turn stamp,
    or the ENTER await propagates out of `_deliver_locked` with NO result, so the
    brake stayed UNARMED — and the next delivery passed the gate (a box holding a
    draft IS a writable box), APPENDED its text and committed BOTH. That is
    reachable in production: `cleanup.clear_topic_state` cancels per-topic tasks,
    shutdown cancels in-flight work, and a cancelled `to_thread`/subprocess await
    can still have COMPLETED its tmux write. The arming condition is **a WRITE was
    ATTEMPTED** — a `_WriteAttempt` flag set immediately BEFORE the first
    `send_keys` literal write (never after: a cancelled write may still have
    landed), which is the SAME information the `DRAFT_WRITTEN` classification
    already uses (r2 F5), so it adds no new imprecision. On any raise past that
    flag `deliver_to_window` arms the brake **INSIDE the send lock** (a queued send
    waiting on `window_send_lock` can never slip in first) and then **RE-RAISES —
    `CancelledError` always propagates, never swallowed into a `DeliveryResult`.**
    Cancellation during the Enter counts as potentially-stranded (the key may not
    have landed); if it DID land, the empty-input-row self-heal releases the brake
    on the next send — fail-closed and self-correcting. A raise BEFORE any write
    attempt does NOT arm it: that is the hard non-regression (a raise proves nothing
    about the pane, and arming on "any raise" would false-refuse a HUMAN's
    pre-existing draft after an unrelated tmux error).
  - **Disclosed residuals:** (a) a bot RESTART wipes the brake (in-memory, exactly
    like the quarantine registry), so a draft stranded before the restart is no
    longer braked and the next message can concatenate onto it; (b) a braked window
    whose pane is ALSO showing a live prompt reports `stranded_draft` rather than
    `prompt_present` — the copy is still the correct action (clear the box); (c) a
    process KILL (SIGKILL — no exception, no unwind) between the write and the Enter
    strands a draft unbraked, the same class as (a); (d) a window that dies WITHOUT a
    `kill_window` (an external `tmux kill-window`, the poller's stale-binding path)
    leaks its entry — inert, not a wedge (`_deliver_locked` refuses `window_gone`
    before it consults the brake, and `create_window` / the empty-box self-heal
    reclaim any id later reused); (e) a topic braked while the user is AWAY stays
    braked (no auto-Esc — surface-specific semantics), and `/unbind` no longer
    releases it because the pane, not the binding, owns the draft. The user-reachable
    exits are always available and every refusal names them: clear the box in the
    terminal (`Esc` / `Ctrl+U`, or `/esc` — which also interrupts a mid-run Claude),
    or `/kill` the window.

**The user-turn stamp is a CONSTRAINED seam, and ALL FOUR sites migrated.** Timing
is right (immediately before the Enter preserves the live-prose turn boundary) but
`window_send_lock`'s contract forbids holders from touching `route_runtime`. So
`deliver_to_window` takes a **narrowly-typed internal pre-commit hook request**
(`delivery.UserTurnStamp` — the route identity, nothing else), fires exactly one
SYNCHRONOUS `message_queue.set_route_user_turn_at` after all gates pass and
immediately before the Enter, and the **lock contract gains an EXPLICIT, NAMED
exception** for that one stamp (`tmux_manager`'s module docstring). It may not
await, may not schedule work, and may not mutate anything else; a hook exception ⇒
`draft_written`, **no Enter, no stamp** (fail-closed). All four pre-existing stamp
sites migrated — `inbound_aggregator._send_bundle`, `bot.forward_command_handler`,
`callback_dispatcher/effort.py`, `callback_dispatcher/late_answer.py` — because the
direct paths stamped BEFORE the gated send and would therefore stamp REFUSALS.

**THE INVARIANT, stated so it is actually TRUE (r2 F3): _no PROVABLY-NOT-COMMITTED
refusal is stamped._** The stronger form ("no refusal receives a turn stamp") was
FALSE: the stamp fires immediately before the Enter, so a FAILED Enter left the
stamp standing. That is not a bug to roll back — it is the fail-closed direction,
and the outcome is now typed honestly. `NOT_WRITTEN` and `DRAFT_WRITTEN` are BOTH
decided BEFORE the Enter (a stamp that RAISES is one of them — it never committed),
so neither can carry a stamp; the ONE outcome that can is `COMMIT_UNKNOWN`, and it
KEEPS the stamp deliberately: a possibly-committed turn must move the live-prose
turn boundary, or a prose block from that turn would be posted as if it belonged to
the previous one. The stamp is never rolled back —
`message_queue.set_route_user_turn_at` mutates two stores, and a rollback is
strictly worse than the honest disclosure the user already gets (pinned by
`test_no_provably_uncommitted_outcome_can_carry_a_stamp` +
`test_a_failed_enter_is_commit_unknown_and_keeps_its_stamp`).

**Refusal reporting (§1.4).** `_report_quarantine_refusal` HARDCODED
`QUARANTINE_SEND_REFUSED_MSG` and equality-matched it, so only that one refusal ever
reached the topic. It is generalized to `_report_delivery_refusal`, carrying the
ACTUAL reason. The structured **`delivery.DeliveryResult`** (outcome `delivered` /
`not_written` / `draft_written` + a machine `reason` + per-reason ACTIONABLE copy) is
threaded through `aggregator_replay_payload` and `_flush_pending_route_payload`
(both previously bare bools) so **pending-bind replay** — which IS the fresh-session
folder-trust case (a brand-new window's very first turn lands while Claude blocks on
"Do you trust the files in this folder?") — surfaces the real reason instead of
"failed to send". The photo/document handlers ack "sent" BEFORE the delayed flush can
refuse, so the later notice must not contradict the ack — it names the reason.
`REFUSAL_COPY` is exhaustive over `DELIVERY_REFUSAL_REASONS` (⊇
`terminal_parser.INPUT_BOX_FAILURE_REASONS`), pinned by a STRICT key-set-equality
test — the /cost busy-path precedent. Copy examples: prompt-present ⇒ "answer the
card first (tap an option, or use the ↑/↓/⏎ keys), then resend"; not-Claude (M3) ⇒
"Claude isn't running in this window … send /update to restart"; lone digit ⇒ "a
message that is just a number can be read as a keypress by the terminal — send it
with a word (e.g. `option 1`)". **Refused payloads are DROPPED with the notice,
never auto-replayed.**

**Observability (§1.6).** ONE INFO per refusal carrying the machine reason + the
written-state outcome — **never pane text, never message content**. And a
**non-exec `CLAUDE_COMMAND` wrapper** keeps the wrapper SHELL as the pane's
foreground process, so `pane_current_command` reports the shell while Claude is
alive ⇒ the gate would refuse EVERY message; `bot._warn_if_non_exec_claude_wrapper`
detects the shape at startup (a `#!` script with no `exec` line) and logs a loud
WARNING. CLAUDE.md already documents the same requirement for `/update`, where the
failure mode is the DANGEROUS direction.

**UNGATED by design:** `/esc`, the bash quick-keys, and the AUQ / Decision
dispatchers key into a LIVE surface ON PURPOSE (they re-validate the pane form
themselves and never send arbitrary text + Enter). They call `tmux_manager.send_keys`
directly and must never route through this gate — it would refuse the very pane they
target.

**Disclosed residuals (bounded, NOT closed).** (1) **gate → write** (the M2 window):
a prompt appearing between the gate capture and the first written byte can still take
a keystroke. Mitigated by step 0 and empirically by the paste shape — a multi-char
payload written in ONE `send-keys -l` is consumed paste-shaped and is **inert** (rig:
`lets do 3 things first` left a live picker completely intact) — but the pty-chunking
split of a multi-digit payload remains a NON-proof case. (2) **final capture → Enter**
(the M1 window): one tmux round-trip. **No terminal protocol can make this atomic** —
Claude redraws independently and a human attached to the session can act at any
moment. This is the IDENTICAL residual the shipped `_dispatch_pick` /
`_dispatch_decision` already accept and disclose. Transitional coexistence (a prompt
drawn while stale bottom chrome is briefly still present) is part of the same
disclosure — stable frames never coexist (rig), but redraws are not atomic. (3) At
the PRE-write gate (no `expected_draft`) a HUMAN's own draft whose first visual row
reads like a picker option (`❯ 1. buy milk`, typed in the terminal) fails leg 2 and
the send is refused — fail-closed, and rare. The bot's OWN numbered payload is NOT
affected (r2 F1). (4) A bot RESTART wipes the in-memory stranded-draft brake, so a
draft stranded before the restart is no longer braked. (5) A stranded draft can only
be cleared BY THE USER (no auto-Esc — surface-specific semantics), so a topic braked
while the user is away stays braked; every refusal says exactly how to clear it.
**What this honestly claims:** the
danger window shrinks from *~500 ms + the full network/aggregator delay* (today the
pane is checked, if at all, at *offer* time) to *one tmux round-trip*; every emitted
literal segment that is a lone hotkey character is refused outright; and the remaining
exposure is an acknowledged residual, not a proof of safety.

PR-2 (the free-text lane — making an AUQ single-select / ExitPlanMode card actually
answerable in prose, with the SGR-2 typed-state verifier and per-surface card copy)
ships separately. Pull-only; no observer (c313657 stays forbidden).

## Free-text answers on a live AUQ card (GH #50 PR-2, flag `CC_TELEGRAM_FREE_TEXT_ANSWERS`, default ON)

PR-1 refuses every payload at a live blocking surface — correct, but a dead end:
the AUQ card literally invited the user to "send a regular message to free-text".
PR-2 makes that invitation TRUE for the ONE surface it ships for, and FIXES the
card copy everywhere else (§2.5).

| Surface | Row | Effect (rig-verified, 2.1.207) |
|---|---|---|
| **AskUserQuestion** (single-select) | N+1 `Type something.` | the prose IS the answer |

**SCOPE: ExitPlanMode is OUT (owner decision 2026-07-12).** An earlier revision
drove EPM's own affordance row (row 4, `Tell Claude what to change` — a plain
message REJECTED the plan with that message as the feedback and PRESERVED plan
mode, rig-verified). It worked, but its safety rested ENTIRELY on a NEW
`PreToolUse(ExitPlanMode)` hook + an `epm_pending/` side file + its own trust
boundary, because **nothing else can name a plan prompt**: every ExitPlanMode
renders the same three real options, and the `planFilePath` is a per-SESSION slug
Claude REUSES, rewriting the file in place on each re-plan (rig-verified on
2.1.207: three consecutive prompts, one slug, three distinct `tool_use_id`s). The
owner runs `--dangerously-skip-permissions` anyway, so hardening a plan-approval
surface did not justify that machinery. It is **REMOVED, not disabled** — no
vestigial surface constant, no half-wired lane. **An ExitPlanMode card therefore
falls through to PR-1's gate, which REFUSES the message with its normal actionable
copy: a plan card cannot be answered in prose.** That is the intended, safe
degradation, and it is pinned by an explicit scenario (`OUT_OF_SCOPE`
`exit_plan_mode`). The pre-PR-2 EPM machinery — the `📋 Plan` body post,
`extract_epm_plan_file_path` (still strictly footer-scoped), the EPM interactive
card — is untouched.

**THE GUARD IS THE PRE-TYPE LANDING PROOF (SGR-2), AND NOTHING ELSE IS.** Before a
single byte is written, the row under the cursor must satisfy ALL THREE of:
`cursor` is on it, its label is EXACTLY `Type something.`, and that label is SGR-2
**DIM** (`terminal_parser.parse_free_text_row`). `dim == True` holds for exactly ONE
shape on a picker — the SELECTED, UNTYPED placeholder — and **a real option row is
NEVER dim, not even when it is the selected row**. That is what makes an OPTION
COMMIT unreachable from this lane, and it is rig-MEASURED (2026-07-12), not argued:
an overshoot onto a real option DECLINES; an undershoot parking on a real option at
`target_row` DECLINES; a payload `Yes, but use postgres` against a card whose option 1
is literally `Yes` DECLINES. `Down` CLAMPS on 2.1.207 (it never wraps) and the nav is
`Down`-only by construction (the affordance row is the LAST row, so `delta >= 0`), so
the wrap-to-option-1 hazard is unreachable; and typing while parked on a real option
row is SWALLOWED entirely (the pane stays byte-identical — no auto-jump, no
insertion). A TUI-DRIFT AUDIT SURFACE beside `clean_ghost_input_text` (the other SGR-2
consumer) and `pane_command_is_claude`; it is why the lane is version-licensed.

**THE POST-WRITE LEGS ARE CORROBORATION, NOT DEFENCE IN DEPTH — three of them are
MEASURABLY WEAK, and the docs no longer claim otherwise (2026-07-12).** (i)
`terminal_parser.auq_free_text_row_active` (the `ctrl+g` footer hint) is NOT an exact
row proof: the hint is ALSO present on the `N+2. Chat about this` row, so it proves
"the cursor is on SOME text-field row", never which. (ii) `free_text.payload_tail_landed`
is a WHOLE-PANE substring check and passes SPURIOUSLY (rig: it matched prose echoed in
the transcript scrollback from a previous answer, on a card that had received nothing).
(iii) the SGR-2 flip read POST-write (`dim is False`) PASSES on a real option row, and
`_label_is_our_draft("Yes", "Yes, but use postgres")` is True — so that pair is a
consistency check, not a guard. They decide only whether the Enter may be sent, and
they fail closed; they do not decide where the bytes went.

**THE ADDITIVE INVARIANT (the property that makes default-ON safe).** EVERY bail
BEFORE the first keystroke returns `None`, and the caller falls through to the
normal gated `deliver_to_window`, which then owns the decision (PR-1 refuses on a
live prompt, or delivers into an input box). So the lane can only ever turn a
REFUSED message into a delivered ANSWER — it can never make a message PR-1 would
have handled correctly come out worse, and it never invents its own refusal for a
payload it has not touched. That covers: flag off, an unlicensed CC version, a
non-Claude pane, a capture failure, a non-AUQ surface (**ExitPlanMode**,
multi-select, review screen, multi-question, folder-trust, `Switch model?`,
Permission, Workflow), an incomplete/scrolled option list (the `options_complete`
proof — a partial pane BAILS rather than guessing N), an unidentifiable card
(below), a nav send failure, an unproven landing, and **a window under PR-1's
STRANDED-DRAFT BRAKE**. A LONE DIGIT payload also falls through:
`deliver_to_window`'s step 0 applies the SAME `delivery.lone_hotkey_line` rule and
refuses it — ONE rule, ONE owner, never two ❌. Once the payload has been TYPED the
lane OWNS the outcome and must not fall through (a second delivery attempt would
APPEND to the text sitting in the row).

**THE STRANDED-DRAFT BRAKE IS CHECKED FIRST (peer-review P1).** PR-1 raises the
brake whenever a payload may still be sitting UNSENT in a window — including one
THIS lane left in a card's affordance row (`DRAFT_WRITTEN`), and including a
`COMMIT_UNKNOWN` whose Enter may in fact have landed and advanced Claude to
ANOTHER live card. While it is up, PR-1 refuses every send until the pane is
PROVEN clear. The free-text lane must not be a way around that: navigating and
typing into whatever is on the pane NOW is exactly the append-and-commit chain the
brake exists to break. `try_answer` therefore checks
`tmux_manager.window_has_stranded_draft` INSIDE the send lock, BEFORE any
navigation or keystroke, and DECLINES — so PR-1 owns the single refusal and the
single user-facing notice (never a second ❌). The lane NEVER clears the brake: its
release rules (an empty-input-row capture, or confirmed window death) are PR-1's,
and they are the only proofs that mean anything.

**SURFACE IDENTITY — WHICH CARD (the wrong-QUESTION narrowing, NOT a wrong-option
guard).** Another controller — the poller, an AFK auto-resolve, a button tap, or
Claude itself — can resolve card A and render card B *while* the executor navigates
or types, and card B then satisfies every POST-WRITE leg: it owns the pane (no input
box), our bytes ARE on it (we typed them into B's row), its row N+1 carries our
cursor and our text at normal intensity, and its footer says a text-field row is
active. The Enter would then commit the user's answer to the WRONG QUESTION — an
annoyance, never an option selection (the pre-type landing proof owns that). So
`free_text.SurfaceIdentity` is captured BEFORE the first key and RE-CHECKED at both
observation points that bracket a keystroke: **after the navigation** (a failure
there has typed nothing ⇒ DECLINE, PR-1 owns the refusal) and **in the final
pre-Enter capture** (⇒ `DRAFT_WRITTEN`, the brake goes up, honest notice). It
catches a card that TURNS OVER mid-transaction (the side file moves ⇒ the anchor
moves); it does NOT catch a same-labelled successor whose record was already written
before our first observation — the disclosed residual below. Both points also
require
`extract_interactive_content(pane).name == "AskUserQuestion"` (first-match-wins, so
AUQ→ExitPlanMode and card→gate/no-surface all refuse there — and AUQ→EPM is the
most dangerous swap available, since EPM's option 1 is "Yes, and bypass
permissions").

*The DRIFT TRAP, and how the identity is made stable across it.* The executor
MUTATES the very pane it must re-identify — it moves the cursor onto the affordance
row, then REPLACES that row's label with the user's prose. A naive form fingerprint
moves under it and every commit would refuse (a self-inflicted denial of service,
not a safety property). Two properties make it stable BY CONSTRUCTION:

  - **cursor-blind** — `AskUserQuestionForm._canonical_repr` already is (the AUQ
    pick-dispatch lane needs exactly that property, for exactly this reason); and
  - **target-row-blind** — `terminal_parser.free_text_surface_identity` drops every
    option at or below the affordance row BEFORE taking the canonical. This is the
    load-bearing half: `_parse_numbered_options` DROPS a row whose label
    `is_affordance_label` ("Type something."), so a pristine AUQ parses 3 options
    and the instant our text lands in row 4 it stops being an affordance and parses
    as a FOURTH REAL OPTION, so `OPTS:` moves.

What survives is exactly the part of the surface the transaction never touches: the
REAL options `1..target_row-1`. Requiring that prefix to be COMPLETE and contiguous
is what makes a missing block fail CLOSED (`None`) instead of degrading to a
shorter, weaker identity. The canonical is the repo's EXISTING
`AskUserQuestionForm.fingerprint()` — never a new hash (mint/validate parity).

*Two components — and the anchor is MANDATORY, because it is the only
OCCURRENCE-unique one (peer-review round-2 P1).* `SurfaceIdentity.pane` (above) is
the strong self-contained discriminator whenever the option block is on screen —
but it identifies a SHAPE, not an OCCURRENCE, and two different cards can share a
shape. `SurfaceIdentity.anchor` is the OUT-OF-BAND, scroll-independent
surface-GENERATION id, and `derive_identity` returns **`None`** when it cannot be
read, so an anchor-less pane never yields an identity at all:

- **AUQ** → the PreToolUse side file's occurrence identity
  (`auq_source.peek_surface_identity_for_window` →
  `auq:sid:<session>:tu:<tool_use_id>`; a new AUQ rewrites the file, a resolved one
  unlinks it). **It used to be OPTIONAL**, so a missing / lagging / GC'd side file
  silently degraded identity to the PANE — and `current_question_title` is normally
  ABSENT from a pure-pane parse, so two DIFFERENT AUQs with identical option labels
  produce the IDENTICAL pane identity. Worse, an identity captured with
  `anchor=None` SKIPPED the anchor comparison entirely, so a successor's non-`None`
  anchor was IGNORED rather than refused — card A's text could be committed onto
  card B. **No side file ⇒ the lane DECLINES** (pre-keystroke, so PR-1 owns the
  single refusal and nothing is typed). There is no second occurrence-unique source
  to fall back on: the AUQ `tool_use` is buffered in JSONL until resolution, so the
  PreToolUse hook is the ONLY pre-resolution witness of *which* AUQ this is. **This
  makes `PreToolUse` a REQUIREMENT of the free-text lane (user-visible → README);**
  the bot already warns at startup when it is missing, `cc-telegram doctor` reports
  it, and `cc-telegram hook --install` installs it.

**THE ANCHOR IS READ BEFORE THE PANE — the OTHER half of the round-3 fix, and the
half a change of anchor SOURCE alone would NOT have closed.** `derive_identity` used
to READ the anchor itself, i.e. AFTER its caller had already captured the pane. That
mints a CHIMERA whenever the card turns over inside the gap — `(pane@t1, anchor@t2)`
with `t2 > t1` ⇒ `(OLD pane, NEW anchor)` — and because the pane component is
degenerate across same-shaped occurrences (a re-asked question renders byte-identical
option rows), that chimera MATCHES every later observation: the transaction types into
the successor and presses Enter. **REPRO-CONFIRMED:** restoring the old order turns
`test_a_card_that_turns_over_INSIDE_the_capture_never_mints_a_chimera` RED.

`derive_identity` therefore **TAKES** the anchor (`anchor: str | None`) and never reads
one; `read_surface_anchor` runs STRICTLY BEFORE every pane capture, at all three
observation points (plan / post-nav / final pre-Enter). The safety argument: a LIVE,
unresolved prompt means Claude is BLOCKED on it, so it cannot be invoking the next
prompt — and the hook fires BEFORE its prompt renders. Therefore "prompt P is live on
the pane at t1" implies "the side file at t1 is P's", and the side file only ever moves
FORWARD. With the anchor read at t0 < t1, the only constructible chimera is `(NEWER
pane, OLDER anchor)`, which FAILS CLOSED on the next `still_holds` comparison. This is
the SAME "probe FIRST, capture LAST" discipline `_reverify_input_box` already applies to
its liveness probe (r2 F4) — a stale-frame authorization is the identical bug class.

**THE ANCHOR CARRIES THE SESSION GENERATION — the round-4 P1, and it defeated the
anchor ENTIRELY.** The anchor is only as good as the SESSION it is resolved for, and it
used to be resolved through the CACHED `WindowState.session_id` — a MIRROR of the
hook-written `session_map.json`, refreshed only when the monitor's poll loop reloads it,
so it LAGS by up to a poll cycle (longer when the monitor is busy). The interleaving:

    1. card A is live in window @N (session A);
    2. the user /clears — SessionStart writes session B into the map;
    3. session B renders its OWN AskUserQuestion card;
    4. the bot's CACHED WindowState.session_id still says A;
    5. so ALL THREE observations read session A's side file while capturing session
       B's pane. They AGREE WITH EACH OTHER — a self-consistent fiction — and a
       re-asked question is pane-degenerate, so nothing refuses;
    6. Enter commits the user's answer onto card B: THE WRONG QUESTION.

Session A's side file is still on disk throughout (the monitor unlinks it on its own
poll cycle, and the whole transaction runs inside that window), which is what made the
lag lethal rather than merely stale: the cached read RESOLVES, consistently, to a card
that is no longer on the pane. A per-window predicate could not have caught it either —
**both sessions occupy the SAME tmux window**, so any `window_key` check matches.

`session.read_session_id_for_window_fresh` reads the hook-written map at every anchor
read (never the cache), and the id is EMBEDDED in the anchor
(`auq:sid:<session>:tu:<tool_use_id>`) — so the session generation is RE-PROVEN at each
of the three observation points: a rotation between any two of them changes the anchor
and `still_holds` refuses (pre-write ⇒ decline to PR-1; post-write ⇒ DRAFT_WRITTEN +
the brake). A rotation whose successor has NO side file yields `None`, which refuses
too — **the fresh read never falls back to a cached/older session**. The ordering that
makes this sound is the same one the anchor already relies on: SessionStart writes the
map BEFORE the new session can render anything, and PreToolUse writes its side file
BEFORE the prompt renders, so "card X is live on the pane" implies "the map names X's
session and X's side file exists". **RED-first repro:**
`test_a_session_rotation_mid_transaction_never_answers_the_NEW_CARD` drives the GENUINE
`auq_source` reader over a genuine `session_map.json` + real side files; with the
pre-fix cached read it FAILS with "the answer was committed onto the NEW session's
card".

**AN EMPTY `tool_use_id` DECLINES — the round-4 P2.** `hook.py` writes `""` when the
payload carries no id, and the anchor path then SYNTHESIZED one from `(written_at,
canonical content fingerprint)`. That is not an occurrence witness: it is a wall-clock
stamp plus a content hash — same-session siblings can share a clock quantum, and (there
being no read-TTL, by design) a stale record stays "valid" forever. On the only licensed
CC version the rig confirms the id is ALWAYS present, so its absence is a broken
contract, not a degradation to paper over. **Scoped to the ANCHOR path**: the GH #48
recap surface-identity lane builds its own composite from `read_side_file_for_recovery`
and is untouched — a guessy identity there costs a duplicate recap, not a wrong
keystroke.

**THE ANCHOR IS BOUND TO THE PANE, NOT TO THE READ ORDER — the round-5 P1-B, and
"anchor before pane" was BACKWARDS.** The round-3 argument above rests on a premise it
never states: that a card the user has ALREADY ANSWERED stops looking *live* on the
pane. `PreToolUse` writes card B's record **before B renders** (`hook.py`), so the
reachable interleaving is:

    1. card A is on the pane; another controller resolves it;
    2. B's PreToolUse hook writes anchor B (the side file is per-SESSION — it is
       OVERWRITTEN, not appended);
    3. B has not drawn yet, so the pane still holds A's picker;
    4. the initial observation mints `(pane A, anchor B)` — the DANGEROUS chimera;
    5. B renders with IDENTICAL option geometry;
    6. the post-nav and pre-Enter observations both see `(pane B, anchor B)`. They
       AGREE. The Enter commits onto B. **THE WRONG CARD.**

Reading the anchor first does not close that, and neither does the round-4 fresh
`session_map.json` read (it is the same session). **REPRO-CONFIRMED on the live call
path**: with the round-5 guards reverted,
`test_the_reviewers_interleaving_never_answers_the_SUCCESSOR_card` fails with *"the
answer was committed onto the SUCCESSOR card"* — `pane.enter_sent is True`.

Three folds, none of them a bet on ordering:

  - **The card must OWN the pane.** `plan_from_pane` now requires
    `pane_input_box_present(pane) is False`. A live blocking prompt REPLACES the input
    box; a resolved one RESTORES it (rig: `auq_after_answer_t{0,1,5,30}`). This is the
    round-3 premise, turned from an assumption into a cheap requirement. *Honest about
    what it buys*: MEASURED, today's parser already declines that shape independently
    (a restored input box makes `is_free_text` go False), so this leg is defence in
    depth against a TUI drift, not the load-bearing leg.
  - **Every observation is a SANDWICH** (`free_text._observe`): read the anchor →
    capture the pane → read the anchor AGAIN → require the two EQUAL. Because the side
    file only ever moves FORWARD, `anchor(t0) == anchor(t2)` proves it did not move
    anywhere in `[t0, t2]` — and therefore not at `t1`, when the pane was captured. A
    hook write landing mid-observation is now **DETECTED** rather than reasoned about.
    The cost is one small local file read per observation.
  - **The anchor RECORD's CONTENT must AGREE with the pane it is paired with**
    (`auq_source.anchor_pane_agreement`, TARGET-ROW-BLIND for the same reason the pane
    identity is — a typed affordance row parses as a FOURTH real option). Three states:
    `match` / `mismatch` (⇒ `derive_identity` returns `None` ⇒ refuse) /
    `indeterminate` (no complete real-option prefix — the overflow shape, where the
    caller has no pane component either and the anchor stands alone).

**THE REUSED HELPER WAS VERIFIED, NOT ASSUMED** (this repo's recorded
`feedback_reuse_claim_needs_liveness_verification` rule). Measured on the live call
path: `_record_consistent_with_pane` **DOES** reject a record whose option labels
differ from the pane (`label_mismatch`), and **DOES NOT** reject one whose labels are
identical but whose QUESTION differs — a pure-pane parse yields
`current_question_title is None` (so its title check skips) and empty option
descriptions, so the labels are the only pane-observable content it has.

**THAT LIMIT IS THE ACCEPTED RESIDUAL (owner decision 2026-07-12), and the machinery
that tried to close it has been DELETED.** Earlier revisions added a question-text
binding to separate two same-labelled cards — a pane QUESTION-REGION extractor
(`terminal_parser.auq_question_region`), a measured wrap column
(`terminal_parser.pane_wrap_column`), and a row-CONSUMPTION WALK in
`auq_source._question_binds_to_pane`. It failed three straight review rounds on its
own injectivity (a whole-pane substring search matched an option LABEL; a
whitespace-squash fallback equated `Is nowhere safe?` with `Is now here safe?`; the
region cap returned a strictly weaker SUFFIX), and the hazard it was defending against
was **over-scoped**. All of it is gone: `auq_question_region`, `pane_wrap_column`,
`_question_binds_to_pane`, the `bind_question_text` parameter, and their tests +
fixtures. `anchor_pane_agreement` now binds the record's OPTION LABELS to the pane and
nothing else, at every observation.

**THE RESIDUAL, STATED HONESTLY.** A SUCCESSOR AUQ card with the **same option
labels**, whose `PreToolUse` record was written BEFORE our first observation but which
had not yet DRAWN, pairs card A's pane with card B's anchor — and every later
observation then agrees with that chimera. **Consequence: the prose answer reaches a
DIFFERENT QUESTION.** The user sees it land on the wrong card immediately and answers
again. **It is NOT an option commit** — the PRE-TYPE LANDING PROOF above makes that
unreachable whatever card is on the pane, because the dim placeholder is the only row
shape that satisfies it. A recoverable annoyance, not a security event. (Two AUQs
sharing the same labels AND the same question were never separable on a pane anyway —
that was the disclosed residual even WITH the question binding.) Pinned by
`test_a_same_label_successor_CAN_get_the_answer_DISCLOSED_RESIDUAL` and
`TestThePreTypeLandingProofIsTheGuard`.

**SCOPING (unchanged, and the reason it now costs nothing):** the shared
`_record_consistent_with_pane` — consumed by the picker RENDER path, the `aqp:`
dispatch's `validate_and_consume`, `status_polling`'s source-drift re-mint and the
GH #48 recap identity — never had a question leg and stays **byte-untouched**. Those
consumers CAN reach a keystroke (`validate_and_consume` → the `aqp:` navigate→Enter
dispatch), but that lane re-validates its EXACT minted form fingerprint and source
fingerprint against the live pane before any key, so it is protected independently;
tightening the shared predicate would have flipped render decisions (`side_file_ok` →
`bail`/`rescue`) and dropped real cards' context / pick buttons.

**ALSO CARRIED FORWARD — the pre-existing GH #50 M2 residual:** a pty-level split of a
single `send-keys -l` could in principle land a digit as a lone HOTKEY with no Enter.
Empirically a whole multi-char payload is consumed PASTE-shaped and is inert on a live
picker, and `delivery.lone_hotkey_line` refuses any bare-digit LINE outright — an
empirical narrowing, **not a proof**, and it stays on record.

`still_holds` is therefore: the surface must match; **the anchors must be EQUAL**
(both sides always have one — a live derivation without one is `None` and dies on
rule 1, so there is no "None matches None" and a captured `None` can never silently
accept a later non-`None`); and a pane identity we HAD must still be EQUAL **or** be
genuinely unrecoverable, forgiven ONLY because the matching occurrence anchor carries
the proof by itself (the overflow shape).

**The transaction** (under `window_send_lock`, mirroring `_dispatch_pick` /
`_dispatch_decision_pane_locked`): (0) the STRANDED-DRAFT BRAKE check (above) —
before any capture or key; (1) a FRESH in-lock `pane_command_is_claude` +
`(surface × CC-version)` license re-read immediately before the first key (the AUQ
round-2 P1-1 rule — a `/update`-swapped TUI inside the window-list cache TTL can
never be arrow-keyed); (2) the strict surface parse (`parse_ask_user_question`) →
the target row (N+1, since affordances are DROPPED from `options`) **+ the SURFACE
IDENTITY** (declines if the card cannot be identified at all); (3) MONOTONIC arrow
nav, never a wrap shortcut (over-counting past the last row wraps to row 1, and the
user's prose would silently become "option 1"); **a cursor already parked on the
affordance row is a ZERO-KEYSTROKE nav, not a decline** — see below; (4) the LANDING
PROOF — **the identity still holds** + cursor on the target row + the label is still
the placeholder + the placeholder is SGR-2 **DIM**; (5) ONE literal write with the
Enter WITHHELD (the `!` two-step is deliberately NOT reproduced: bash mode is a
property of the INPUT BOX, and a live card owns the keyboard); (6) the IDENTITY +
TYPED-STATE VERIFY (below); (7) the pre-commit `UserTurnStamp` — **PR-2 is the FIFTH
Enter-commit path and a free-text answer IS a user turn** [r5 P1-1]; a hook raise ⇒
DRAFT_WRITTEN, no Enter, no stamp; (8) Enter; (9) a bounded advance confirmation — a
committed answer TEARS THE SURFACE DOWN, so its continued presence is the honest
`commit_unconfirmed` signal, NEVER auto-retried.

**A RAISE PAST A WRITE IS REPORTED AS DRAFT_WRITTEN (round-4 P2).** Both gated
transactions (`session.deliver_to_window` and `free_text.try_answer`) arm the
per-window stranded-draft brake INSIDE the send lock, immediately before re-raising,
whenever the raise lands past a write attempt (`_WriteAttempt`). But
`inbound_aggregator`'s exception arm HARDCODED `delivery.refuse(REASON_SEND_FAILED,
written=False)`, so the machine-visible outcome said NOT_WRITTEN while bytes may well
have been sitting in the pane — the brake stayed up only as an out-of-band side effect,
and every consumer of the `DeliveryOutcome` was reading a claim the transaction had
already contradicted. The arm now READS the brake registry (`window_has_stranded_draft`)
— the authority, already committed by the time the exception is caught — so a braked
window reports DRAFT_WRITTEN (with the honest NEUTRAL copy telling the user to clear the
box, which is exactly what the brake will demand of their next message) and an unbraked
one reports NOT_WRITTEN as a PROVEN claim. `CancelledError` is a `BaseException`: still
not caught, still propagates, still never reported as an ordinary refusal.

**THE CURSOR MAY ALREADY BE ON THE FREE-TEXT ROW (peer-review P2).** The card's own
↑/↓ nav buttons let the user land on `Type something.` — which is exactly what the
card invites them to do — and then send prose. `_parse_numbered_options` DROPS the
affordance row and, because an affordance `❯` is the bottom-most (hence live)
cursor, deliberately CLEARS every real option's cursor, so the form reports NO
cursor at all. Reading that as "we can't find the cursor" and DECLINING meant the
most natural gesture the card invites was the one gesture that got REFUSED.
`_auq_shape` now reads the affordance row directly
(`parse_free_text_row(ansi, number=N+1).cursor`) and takes a ZERO-NAV plan — while
STILL requiring the SGR-2 DIM landing proof before a single byte is typed.

**The POST-WRITE VERIFY** (`_typed_state_reason`; every leg AND-ed, a failure
WITHHOLDS the Enter — but see the corrected framing above: this is CORROBORATION, and
the decision that mattered was already made by the pre-type landing proof): (A) a
bounded `pane_command_is_claude` re-probe FIRST so the pane CAPTURE is the LAST
observation before the Enter (the r2-F4 ordering); (B) `pane_input_box_present` is
**FALSE** — the blocking surface still owns the pane (if the card AFK-resolved
mid-type, the input box is back and Enter would submit a half-typed message); called
WITHOUT `expected_draft`, because the picker trap is exactly what must fire; (C)
**IDENTITY — WHICH CARD**: the extracted surface is still `AskUserQuestion` AND
`SurfaceIdentity.still_holds` — this catches a card that TURNED OVER mid-transaction
(its hook rewrote the side file ⇒ the anchor moved), and does NOT catch the disclosed
same-labels successor; (D) **the row, or — in the overflow shape — the footer**: (D1)
the affordance row is on the pane, carries the cursor, is NOT SGR-2 dim and its label
is a prefix of what we typed — **WEAK: both halves pass on a selected REAL option row
whose label prefixes the payload**; or (D2) the row scrolled off, but the live picker's
footer carries `ctrl+g to edit` — **WEAK: that hint is also present on the `N+2. Chat
about this` row**, so it proves "the cursor is on SOME text-field row", never which row
and never which card; (E) the payload TAIL occurs on the pane — **WEAK: a whole-pane
substring test that passes spuriously on scrollback echoing a previous answer**.

**`auq_free_text_row_active` is SCOPED to the LIVE picker, and it is NOT an exact row
proof (2026-07-12 — this CORRECTS the original claim that "it tracks the cursor
exactly").** The hint is absent on rows 1/2/3 and present on row N+1 — but it is ALSO
present on the `N+2. Chat about this` row, which is a text field too. So it proves
"the cursor is on SOME text-field row of this live picker" and is used ONLY as
post-write corroboration in the overflow shape. Scoping (kept): the first cut was an
OR over the WHOLE pane, so a footer left in SCROLLBACK by an EARLIER picker — or a
transcript the user pasted — satisfied it while the LIVE picker's own footer said the
opposite. The pane must extract as a live `AskUserQuestion`, and the footer consulted
is the **BOTTOM-MOST** picker footer on the pane (the repo's bottom-most-is-live rule).

**THE OVERFLOW SHAPE (rig-measured).** A long answer wraps to more rows than the pane
has, and a TUI runs on the ALTERNATE SCREEN — `capture-pane -S` recovers nothing
(measured: 51 lines), so what scrolls off is genuinely unobservable. The AUQ picker is
**BOTTOM-anchored**: its option block — the `❯ N+1.` cursor row INCLUDED — scrolls off
the TOP while the footer stays, so D2 carries the ROW and the side-file ANCHOR carries
the CARD. (With no PreToolUse hook installed there is no anchor, nothing identifies the
card, and an overflowing answer REFUSES — fail-closed, disclosed.) Measured boundary on
a 160x50 pane: ~947 chars and ~2.6 k chars both keep the whole block visible; ~5.3 k
triggers overflow (and Enter still committed all 5 274 chars — JSONL-verified).

**NO PASTE-COLLAPSE ON AN AFFORDANCE ROW (the question PR-1's regression forced).**
A payload written in ONE `send-keys -l` past ~800 chars collapses the INPUT BOX to
`❯\xa0[Pasted text #1 +12 lines]` and replaces the status bar with `paste again to
expand` (the shipped PR-1 regression, 5ba9b5e). A live CARD's affordance row does
**NOT** do this: 947 chars / 9 lines and 5.3 k chars / 30 lines both render as
LITERAL wrapped text, the row keeps its number and cursor, and the label is PLAIN.
So the SGR-2 discriminator holds on the owner's primary path (a long voice note) and
the verifier commits it. Fixtures: `auq_freetext_row_typed_large_v2.1.207.ansi.txt`,
`auq_freetext_overflow_v2.1.207.txt`.

**PROVENANCE: explicit composable FACTS, never a `kind`** [r3 P1-2]. `_PendingBundle`
flattens typed prose, a voice transcription, a caption and a reply-context-rendered
quote into indistinguishable `text_parts`, so "is this pure user prose?" is NOT
recoverable from the string — it must be OBSERVED at the offer site. The bundle
carries an `inbound_aggregator.Provenance` (`typed_text` / `voice` / `caption` /
`reply_context` / `attachment`), OR-composed across every merge. **Eligible = (typed
prose OR voice) AND NONE of caption / attachment.** Voice IS eligible (it is the user
speaking — the flow PR-2 exists for). `_apply_reply_context` returns
`(rendered_text, applied: bool)` so the fact is OBSERVED, not guessed [r4 P2-1];
`PendingAttachment` + the pending-text store carry the facts so **pending-bind replay
preserves them**; a bundle created AFTER a media-group boundary / cap flush starts
EMPTY and takes its facts from the NEW item (it must never inherit the popped
bundle's). Slash commands never reach the lane at all — `forward_command_handler`
force-flushes and then sends the command through `send_to_window` directly.

**REPLY-CONTEXT IS ELIGIBLE (OWNER DECISION 2026-07-12 — supersedes plan §2.3,
which made it INELIGIBLE).** The owner's dominant gesture at a card is a VOICE NOTE
sent as a REPLY to it (both live test messages were), so the as-planned rule refused
their most natural way of answering — precisely the friction this lane exists to
remove. **Claude receives the FULL rendered payload — the quoted context AND the
user's words — exactly as the bot renders it for any other send**: the quote is
CONTEXT for the answer, not a competing intent, and the affordance row is rig-proven
to take 5 k+ chars of multi-line text and commit it whole (the 947-char rig capture
IS a reply-quoted voice note). `has_caption`, `has_attachment` and command payloads
remain INELIGIBLE, unchanged. The `reply_context` FACT is still observed and carried
— only its effect on eligibility changed, so the decision is one line and reversible.

**Integration seam = the AGGREGATOR FLUSH** (`_send_bundle` → `_try_free_text_answer`)
— the only place that knows the provenance [r2 P1-5]. NOT `send_to_window` (provenance
is flattened by then) and NOT `text_handler` (the debounce makes any offer-time check
TOCTOU). Gated FIRST on the cheap, in-memory, route-keyed
`interactive_ui.has_interactive_surface`, so an ordinary send (no card up) pays
NOTHING — no extra capture, no lock churn.

**Version-licensing is MANDATORY** (`free_text._FREE_TEXT_LICENSE_TABLE`, the
`decision_token` precedent): the row index, the placeholder label, the SGR-2 styling
and the `ctrl+g` footer proof are per-CC-version TUI empirics. Seeded with `2.1.207`,
fixture-pinned. It stays a TABLE keyed by surface rather than a bare version set,
because the surface IS the unit of characterization. **Top residual (disclosed):**
every CC upgrade empties the effective allowlist → the lane degrades to PR-1's refusal
until the surface is re-characterized against fresh rig captures (honest, INFO-logged,
never a wrong keystroke).

**§2.5 — the false hint is fixed in lockstep.** `interactive_ui` used to print
`(Type something — send a regular message to free-text)` on EVERY picker with a
free-text affordance, including the multi-select and unlicensed-version cases where
PR-1 REFUSES such a message. The line is now per-surface (`free_text.card_hint`,
resolved at the callsite that holds the live CC version): licensed AUQ-single ⇒
"💬 Send a message to answer in your own words."; multi-select ⇒ "Use the option
buttons, then Submit."; unlicensed / flag-off / no affordance ⇒ "Answer with the
buttons or the ↑/↓/⏎ keys."

**Other disclosed residuals.**

  - **THE SAME-LABELS SUCCESSOR** (the headline one, owner-accepted — see above): a
    successor AUQ card carrying the SAME option labels, whose `PreToolUse` record was
    written BEFORE our first observation but which had not yet DRAWN, can receive the
    prose intended for its predecessor. **Consequence: your answer reaches a different
    QUESTION — you see it immediately and correct it. NOT an option commit** (the
    pre-type landing proof makes that unreachable). Deliberately NOT closed; the
    machinery that tried to (question region + wrap column + consumption walk) was
    deleted for failing its own injectivity three rounds running.
  - **The GH #50 M2 residual, carried forward:** a pty-level split of a single
    `send-keys -l` could in principle land a digit as a lone HOTKEY with no Enter.
    Empirically a whole multi-char payload is consumed PASTE-shaped and is inert on a
    live picker, and `delivery.lone_hotkey_line` refuses any bare-digit LINE outright —
    an empirical narrowing, not a proof.
  - **The verify→Enter TOCTOU** is the SAME residual `_dispatch_pick` /
    `_dispatch_decision` already accept and disclose: one tmux round-trip, which no
    terminal protocol can make atomic. Bounded by the fail-closed `commit_unconfirmed`
    — and, again, bounded to WHICH QUESTION, never to which option.
  - **On an install with NO `PreToolUse(AskUserQuestion)` hook the free-text lane is
    OFF** — every message at a card takes PR-1's refusal, pre-keystroke. That is the
    round-2 P1 fold: the alternative was trusting a pane identity that cannot tell two
    same-shaped prompts apart. It is a DEGRADATION, not a hazard (the option buttons
    and the ↑/↓/⏎ keys are unaffected), it is user-visible and documented in the
    README, the bot warns at startup, `cc-telegram doctor` reports it, and
    `cc-telegram hook --install` fixes it.
  - **A re-asked AUQ always gets a NEW `tool_use_id`**, even when the question text is
    byte-identical — so a "byte-identical re-ask is indistinguishable" residual is
    CLOSED by construction: the occurrence id changes, and an answer typed for the old
    card refuses. The converse (the poller re-RENDERING the same live card without a
    new tool call) keeps the same id, which is correct — it IS the same card.
  - **A double-`--resume` sibling** shares one session id, and the AUQ side file carries
    no `window_key`, so two windows resolving the same session read the same record.
    The session-generation embedding (round-4) does not close that — it closes the
    session ROTATION, which is a different failure. Unchanged, pre-existing, disclosed
    in the AUQ card-liveness contract above.

Pull-only; no observer (c313657 stays forbidden).

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
route-ordering delivery subsequence ONLY** (aggregator flush → the GH #50
``UserTurnStamp`` pre-commit request — the late answer is a genuine user turn, so
live-prose turn-boundary + dashboard 🔔 semantics match a typed message, but only
when it is ACTUALLY delivered → ``send_to_window`` with the ``(bool, str)`` return
honored → ``mark_inbound_sent``). Success: "✅ Late answer sent: <label>"; failure:
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
fallback (still fail-closed):** a RELATIVE candidate that is genuinely
FILE-NOT-FOUND under the session cwd — the ONLY fallback reason; any
existing-but-rejected cwd resolution (oversize / non-regular / an escaping
symlink or `../` traversal's "outside" reject, hermes P3c — a fallthrough there
would silently substitute a different main-root file for the entry the prose
referred to) OWNS the name — retries the join against the derived main-repo
root when the resolved cwd carries the harness `.claude/worktrees/<name>` shape
(`_worktree_main_root`: the prefix before the `.claude`/`worktrees` segment
pair, pure string logic — no git subprocess; the cwd must be INSIDE a worktree,
at least one segment after `worktrees` — the bare `~/.claude/worktrees`
container must never derive the home directory, codex P1); the cwd hit ALWAYS
wins (same-named file in both → the session's own copy), the main-root hit is
pinned + displayed relative to the main root, and a `../`-escape /
symlink-escape rejects under BOTH roots (containment + O_NOFOLLOW + fstat
unchanged). Only the harness layout is covered — a general `git worktree add`
elsewhere is NOT. The card path
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

**AUQ recap after a normal miss (GH #48, R2 only).** This path runs only for
`AskUserQuestion`, only when normal finalized selection plus the existing
stream-wait posted nothing, and before the 📋 question card; EPM and permission
gates never enter it. The AUQ side file is read once atomically and supplies
both `emitted_at` and the surface occurrence identity: non-empty `tool_use_id`
is primary, otherwise `written_at!r` plus the FULL canonical content fingerprint
forms the composite. No live side file means no guessed identity and a
`no_anchor` miss. On first sight of surface S,
`md_capture.get_or_create_surface_floor` appends a `surface_floor` marker with
`render_at` and a frozen `floor_at` equal to the latest predecessor surface's
render time; retries of S return the stored floor, while only S+1 uses S's
render time. `effective_floor=max(not_before-or-0, floor_at-or-0)`.

The freshest finalized record can recap only when `not_before` is non-None,
`first_seen_at > effective_floor`, `final_at > not_before`, and
`final_at < emitted_at - _EMIT_ANCHOR_LOOKBACK_S` (the normal anchor-reject
class). Thus a record already considered at S cannot leak into a chained S+1,
and a spanning record first seen before S but finalized after S is rejected at
S+1. Restart loses the in-memory `not_before`, so recap deliberately fails
closed; the card still renders and JSONL remains the delivery fallback.

Delivery is best-effort, normally once. The source is headed
`📌 Context (recap)` and divided by RENDERED MarkdownV2 cost using the same
escape function as the expandable-quote renderer; every chunk has its own
complete sentinel pair and uses `topic_send(plain=False)`, avoiding the
renderer truncation path. After every chunk succeeds, a `recap_shown` marker
keyed `(norm_hash, emitted_at)` is appended to the same session NDJSON. A send
failure writes no marker and never blocks the card; retry may send again, and
ambiguous Telegram completion can still duplicate cosmetically. Quiet
(`digest_card=False`) suppresses recap. These `surface_floor` and `recap_shown`
marker kinds do not participate in PR-D; `filter_live_prose_duplicates` and the
finalized shown-live/consumed lane remain unchanged.

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
**AUQ** → the `written_at` from one atomic
`auq_source.read_side_file_for_recovery(session_id)` result (the PreToolUse
side-file stamp ≈ the tool_use invocation; read-TTL-free, future-skew guarded;
the same result also supplies recap surface identity) with
`_EMIT_ANCHOR_EPS_S` (2s) / `_EMIT_ANCHOR_LOOKBACK_S` (10s);
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
only as trustworthy as the mapping, so the **trust boundary** (hermes R2 P1,
hardened by GH #41): `group_chat_ids` is written by the topic message seams
(`text/photo/voice/document_handler`, `forward_command_handler` — now only
with a real `thread_id`, `topic_edited_handler`) and by registry-RECOGNIZED
callback taps (`callback_dispatcher` writes only when `registry.lookup`
recognizes the callback data — unknown data never writes); an UNBOUND
`(user, thread)` write remains legitimate (the directory-browser bootstrap
into a brand-new topic). The load-bearing enforcement moved INTO
`set_group_chat_id` itself (GH #41 sticky-when-BOUND): an existing entry
with a DIFFERENT chat_id is REFUSED overwrite while the user holds a live
thread BINDING for that thread — a colliding cross-forum thread id cannot
steal a bound topic's mapping. Disclosed residual: the guard checks
`thread_bindings` (bound), not tmux liveness, so a STALE binding freezes the
old mapping until the stale-window unbind clears it, after which the write
self-heals. `/dashboard` itself still NEVER writes `set_group_chat_id`,
because thread ids are chat-local and a host claim in chat B's unbound
thread N would overwrite the mapping of chat A's bound topic N and leak it
onto chat B's dashboard. The dashboard instead carries its OWN
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
(single writer ⇒ same-ts by construction), fired inside the GH #50 gated
delivery transaction immediately before the Enter — never on a refusal;
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
