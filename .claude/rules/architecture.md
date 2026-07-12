# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - Topic-based routing: 1 topic = 1 window = 1 session             │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture tmux pane as PNG                           │
│  - /esc: Send Escape to interrupt Claude                           │
│  - /update: Update CLI + restart THIS topic's idle session in place │
│    (/update all = every idle session)                              │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code (blocklist floor: /memory,     │
│    /help TUI panels refused, not forwarded)                        │
│  - /cost + /usage: bot-side TUI-overlay interceptors (idle        │
│    preflight → send → capture → parse → Esc ONLY when the overlay │
│    chrome is on the pane; zero keystrokes into a busy/picker pane; │
│    every non-overlay exit posts a bridge-side "cost snapshot"     │
│    (context % + cached last overlay + reason-specific action);    │
│    bounded preflight retry + wait_for deadline; reason-classified │
│    INFO log at every exit)                                        │
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
│  - Tail sidechains      │                   │
│    UNCONDITIONALLY;     │                   │
│    show_tool_calls only │                   │
│    gates display; per-  │                   │
│    tick per-agent ticks │                   │
│    + launch/completion  │                   │
│    signals → keyed bg-  │                   │
│    agent marks (GH #44) │                   │
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
│    retrieval           │  reads  │    Notification →      │
                                   │      write notify_     │
                                   │      pending side file │
                                   │  - Receive hook stdin  │
                                   └────────────────────────┘

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
  rate_limiter.py             ─ TypingAwareRateLimiter (AIORateLimiter subclass,
                                transport-plumbing leaf) wired at the Application
                                construction site. Overrides only process_request
                                to exempt sendChatAction from the per-GROUP 20/60s
                                bucket (positive dummy chat_id to the classifier)
                                while keeping the overall 30/s limiter + RetryAfter
                                — so multi-topic typing cadence isn't paced by the
                                message budget. data is classification-only; the
                                real request body (args) is untouched.
  route_runtime.py            ─ The sole per-route run-state / context-usage /
                                idle-clear authority. A lock-protected
                                RouteRuntimeSnapshot interface; owns RunState,
                                ContextUsage, IDLE_CLEAR_DELAY_SECONDS, and the
                                JSONL replay parser (parse_pending_tools_from_jsonl).
                                Also owns the lower-authority pane_interactive_pending
                                bit + mark_interactive_pending / mark_interactive_cleared
                                (PROMOTE an active RUNNING route → WAITING_ON_USER for a
                                buffered interactive tool_use; see the concurrency contract).
                                Busy-signal Wave A: records idle_source
                                ("transcript" = the authoritative end-of-turn branch;
                                "pane" = a pane clear that reconciled an ACTIVE route —
                                a pane clear on an already-idle route preserves the
                                value; lazy IDLE_RECENT→IDLE_CLEARED decay preserves it;
                                reset to None on leaving idle / mark_session_reset /
                                teardown) plus a suspended_tools stash: the pane-idle
                                reconciliation MOVES open_tools (ids + interactive
                                flags) into the stash instead of dropping them.
                                Restore paths: mark_background_agent_activity
                                resurrection (the keyed GH #44 successor of Wave A's
                                retired mark_subagent_activity), and
                                a transcript tool_result for a suspended id (checked
                                BEFORE the unknown-id branch — restores+closes via the
                                normal pairing). Drop paths: authoritative end-of-turn,
                                user lifecycle event (genuine only — a
                                task-notification user event PRESERVES the stash),
                                mark_inbound_sent,
                                mark_session_reset, route teardown. In-memory only
                                (restart recovery stays parse_pending_tools_from_jsonl
                                + seed_open_tools).
                                mark_background_agent_activity(route, key, ts) is
                                the keyed sidechain keep-alive mutator: on RUNNING /
                                RUNNING_TOOL it refreshes last_event_at + re-arms the
                                pane-idle debounce (no open_tools mutation); on idle
                                with idle_source=="pane" it RESURRECTS (restores the
                                stash → RUNNING_TOOL, or RUNNING on an empty stash;
                                clears idle deadlines — UNqualified, positive live
                                proof); on transcript-idle / None it leaves the
                                STORED state untouched (the GH #44 projection lifts
                                the visible state instead — see below); it never
                                overrides WAITING_ON_USER (transcript- or
                                pane-bit-set) and never seeds an unseen route. Card
                                claim NARROWED: a status clear already enqueued before
                                resurrection MAY still delete the Busy card (no queue
                                generation-guard; no send-layer authority) — it
                                re-publishes on the next active status tick. Accepted
                                residual: a quiet sidechain (no writes) + blank pane is
                                uncovered; pane-spinner activity is the complementary
                                signal.
                                Wave C dashboard turn stamps: two WALL-CLOCK snapshot
                                fields on the same time.time() clock as the delivery
                                stamps — last_user_turn_at (written ONLY by the sync
                                stamp_user_turn, mirrored from message_queue.
                                set_route_user_turn_at, whose FIRING POINT moved
                                INSIDE the GH #50 gated delivery transaction — after
                                every gate, immediately before the Enter — so a
                                REFUSED send is never stamped, while the pre-send
                                ORDERING property is preserved; never
                                mark_inbound_sent, which is post-send and loses
                                the fast-transcript race) and
                                last_assistant_turn_ended_at (written ONLY by the
                                authoritative end-of-turn branch from the EVENT's
                                JSONL timestamp, MAX-monotonic by event time —
                                out-of-order resume/rewind events never regress it;
                                None timestamp ⇒ no update, never ingest-time).
                                Cleared on mark_session_reset / clear_route /
                                clear_routes_for_topic; in-memory only (restart ⇒
                                dashboard renders state-only until repopulated).
                                last_event_at stays monotonic and is NEVER used for
                                the 🔔 unanswered-turn classification (ages only).
                                GH #44 background-agent projection: a THIRD
                                lower-authority input, background_agents
                                (normalized key → {last_seen_wall,
                                last_event_ts, is_background}) + done
                                tombstones, applied at SNAPSHOT time by the
                                single _build_snapshot/_projected_run_state
                                helper (every read path; no duplicate-freeze
                                drift): stored-idle + live key ⇒ visible
                                RUNNING (typing + 🟡 Busy); a committed
                                notification_pending projects WAITING above
                                the lift. The same helper derives the read-only
                                background_only snapshot field (stored-idle +
                                projected RUNNING on live bg keys, False when the
                                🔔 lift outranks) for the poller's labeled-silence
                                card. Marks: mark_background_agent_
                                activity (keyed Wave A successor — heartbeat +
                                pane-false-idle resurrection unqualified; idle
                                key SET strictly ts-qualified vs
                                last_assistant_turn_ended_at, fail-closed),
                                mark_background_agent_launched (⇒
                                is_background, never pruned; fed by FOUR
                                live launch sources, EACH WITH ITS OWN
                                ANCHORING — they are NOT uniformly structured:
                                (1) Agent/Task agentId — at the LIVE monitor
                                seam PROSE-anchored ONLY
                                (extract_async_agent_launch_id on the
                                `agentId:` tool_result line; the structured
                                async_agent_launch_id_from_meta runs ONLY in
                                the startup reconciler, so LIVE Agent launch
                                recording is NOT meta-drift-proof — a prose
                                drift silently stops it; disclosed);
                                (2) Workflow wf-task:<taskId> —
                                structured-PRIMARY
                                (workflow_launch_info_from_meta) with a
                                WARNING-logged prose fallback;
                                (3) T1.2 background Bash backgroundTaskId —
                                structured-ONLY
                                (background_bash_task_id_from_meta, keyed on
                                backgroundTaskId PRESENCE; prose NEVER lifts —
                                the async-launch META shapes are
                                disjoint). The bash key is BARE so it == its
                                <task-notification> close key, no bracket;
                                (4) Fix C (2026-07-08) resumed agent —
                                mark_background_agent_resumed(route, key,
                                resume_ts) from the SendMessage-nudge
                                tool_result (structured-ONLY
                                resumed_agent_id_from_meta, keyed on
                                resumedAgentId PRESENCE; FOUR-WAY disjoint) —
                                POPS the per-key done tombstone (the SECOND
                                keyed exception to genuine-user-turn reset) +
                                stamps resumed_event_ts on the record (the
                                parent tool_result EVENT ts; max-monotonic
                                preferring parseable) for the cross-file
                                resume-vs-sidechain-done guard),
                                seed_idle_and_mark_background_agent_launched
                                (and its resume twin
                                seed_idle_and_mark_background_agent_resumed)
                                (PR-1 Half B: the launched mark but SEEDS an
                                IDLE_CLEARED+seen _RouteState if the route is
                                unseen, in one critical section — the bot
                                fan-out's launched-key handler so the restart
                                reconciler's relit wf-task: key lifts an
                                otherwise-stateless post-kickstart parent; a
                                no-op seed on an already-seeded route),
                                mark_background_agent_done (tombstones; Fix C
                                carries a BgDoneSource: a PARENT
                                <task-notification> done tombstones
                                UNCONDITIONALLY [transcript order is
                                authoritative — the monitor net-resolves a
                                same-batch resume/done pair], a SIDECHAIN
                                end-of-turn done is timestamp-gated on the
                                record's resumed_event_ts [keeps a resumed key
                                LIVE unless the end_turn ts —
                                SidechainTick.max_end_turn_ts, separate from the
                                max_event_ts activity — is STRICTLY newer;
                                missing record / no resumed_event_ts /
                                unparseable end_turn ts all fail closed to
                                DONE]. GH #46: a TEAMMATE source (an agent-teams
                                teammate's park / idle_notification — the ONLY
                                close signal for a teammate leg that ends in
                                plain text, no sidechain end-of-turn + no
                                <task-notification>) SHARES the SIDECHAIN
                                resume ts-gate PLUS (r2, a documented plan
                                amendment) a TEAMMATE-only stale-vs-activity
                                gate: a PARSEABLE park strictly older than the
                                record's own last_event_ts is SUPPRESSED (a
                                redelivered old park must not tombstone a
                                working teammate mid-leg / strand the
                                genuinely-final park); a tie tombstones
                                (dark-safe), unparseable/missing-record
                                tombstones — SIDECHAIN byte-untouched).
                                GH #46 PR-2 (teammates as first-class bg keys)
                                adds ZERO new route_runtime mutators — the
                                session_monitor generational teammate registry
                                (_TeammateRec) drives the EXISTING resumed (r7
                                item 3: EVERY bind relights via the tombstone-
                                popping resumed lane, never launched — the
                                monitor can't see route_runtime tombstones, so
                                only the popping lane is uniformly safe; r8
                                item 1: the resume ts is floored at
                                min(spawned_ts, first_entry_ts) - ε, below the
                                bound file's OWN first entry, so a look-alike's
                                pre-spawn trailing end_turn isn't shielded) /
                                resumed (wake, generation-filtered AND — r9
                                item 2 — universally orphan-retained RAW so a
                                stashed next-gen wake is never spent on the
                                bound old gen) /
                                TEAMMATE-done (park, universally orphan-retained
                                — r8 item 2 → r9 item 1 → r10 item 1: the
                                32-name buffer's at-cap eviction is three-tier
                                oldest-first keyed on the DRAIN FILTER's own
                                semantics — tier 1 evicts a REDUNDANT entry
                                (drain would generation-drop it), tier 2 a
                                SPECULATIVE one (has a rec, no stashed spawn —
                                same-gen noise), tier 3 (last resort) a PROVABLE
                                one (no rec, or a stashed next-gen spawn — the
                                pending gen's only close)) marks; see
                                message-handling.md.
                                Clears: done / a PER-KEY wall-clock heartbeat
                                TTL (_wall_now(), expire-before-classify) —
                                T2 split: foreground-presumed keys age by
                                BG_AGENT_TTL_SECONDS (30 min), launched /
                                post-turn background keys by
                                BG_BACKGROUND_TTL_SECONDS (2 h), via
                                _bg_ttl_for(rec) at BOTH TTL seams /
                                provenance-only foreground prune at
                                end-of-turn / teardown.
                                mark_notification_pending commits on
                                stored-idle + live bg key (🔔 outranks the
                                lift). Task-notification user events
                                (is_task_notification, adapter-stamped) — and
                                (GH #46) agent-teams teammate user events
                                (is_teammate_notification, via
                                utils.is_teammate_message) which ride the SAME
                                machine-initiated branch (clear reason
                                TEAMMATE_NOTIFICATION) —
                                preserve tombstones/pane-bit/stash, clear the
                                notification bit ts-qualified, and re-derive
                                with preserved gates — and (T1.3) PRESERVE the
                                stored idle on a no-gates stored-idle route
                                instead of forcing RUNNING, so a completing
                                background bash/agent's later done tombstone
                                drops typing cleanly at close instead of
                                stranding it. mark_subagent_activity is RETIRED
                                into the keyed mark. In-memory; restart ⇒
                                stamp-None fail-closed (no lift until fresh
                                parent activity; bash is NOT restart-relit — no
                                sidechain to stat).
  delivery.py                 ─ GH #50 delivery-result + payload-shaping leaf (pure
                                stdlib; imports terminal_parser ONLY). Owns
                                DeliveryOutcome (delivered / not_written /
                                draft_written / commit_unknown — the WRITTEN-STATE
                                classification; draft_written PROVES not-committed,
                                commit_unknown means the Enter was ATTEMPTED and
                                tmux reported failure, which does NOT prove the key
                                never reached the pty — r2 F3; `draft_stranded`
                                covers both and arms the per-window brake, whose
                                registry lives in `tmux_manager` beside the
                                post-/exit quarantine — released ONLY by an
                                empty-input-row capture or CONFIRMED window death,
                                never by topic teardown),
                                DeliveryResult (outcome + machine reason + the
                                per-reason ACTIONABLE user copy; `.as_tuple` is the
                                legacy `(ok, message)` shape the sync callers still
                                use), UserTurnStamp (the narrowly-typed pre-commit
                                hook REQUEST — the ONLY route_runtime mutation the
                                window_send_lock contract permits, and only for the
                                one synchronous set_route_user_turn_at), the
                                REFUSAL_COPY map (exhaustive over
                                DELIVERY_REFUSAL_REASONS ⊇
                                terminal_parser.INPUT_BOX_FAILURE_REASONS, pinned by
                                a strict key-set-equality test), and the payload
                                shaping: literal_segments (the writes the mode-aware
                                `!` two-step writer will ACTUALLY emit) +
                                lone_hotkey_line (the SEGMENT-aware, PER-LINE
                                bare-digit refusal) + unsafe_control_char (the
                                round-5 P1-A RAW-CONTROL-BYTE refusal — `-l` stops
                                TMUX interpreting key NAMES but passes C0/ESC to
                                the pty VERBATIM, rig-confirmed, so an embedded
                                ESC[B + digit is a cursor-move + HOTKEY commit
                                fired DURING the write; everything in C0 except LF,
                                plus DEL + C1, is refused at BOTH gated seams. LF
                                stays ALLOWED — paste-shaped multi-line payloads are
                                a first-class flow; \t and \r are refused, a pasted
                                tab-indented snippet being the disclosed cost) +
                                is_bare_slash_payload (the post-write
                                `/`-completion exemption).
  handlers/free_text.py       ─ GH #50 PR-2: free-text answers on a LIVE
                                AskUserQuestion card. The executor that makes a
                                Telegram message ANSWER an AUQ single-select
                                picker by driving its row N+1 "Type something."
                                affordance. **ExitPlanMode is OUT (owner decision
                                2026-07-12)** — an earlier revision drove EPM's
                                own affordance row (row 4), but its safety rested
                                ENTIRELY on a new PreToolUse(ExitPlanMode) hook +
                                epm_pending/ side file, because nothing else can
                                name a plan prompt (every EPM renders the same
                                three real options; the planFilePath is a
                                per-session slug Claude rewrites in place). The
                                owner runs --dangerously-skip-permissions anyway,
                                so that hook + state file + trust boundary was
                                not worth it. REMOVED, not disabled: an EPM card
                                now falls through to PR-1's gate, which REFUSES
                                the message — a plan card cannot be answered in
                                prose (the intended degradation; the pre-PR-2 EPM
                                machinery — the 📋 plan-body post,
                                extract_epm_plan_file_path, the EPM interactive
                                card — is untouched).
                                Reuses the shipped dispatch discipline verbatim
                                (window_send_lock, bounded cancellation-safe
                                captures, a FRESH in-lock pane_command_is_claude
                                + version-license re-read before the first key,
                                monotonic arrow nav, settle→re-parse→verify,
                                Enter as the ONLY commit key). TWO THINGS MUST BE
                                PROVEN, NOT ONE:
                                  * the pane STATE — TYPED-STATE PROOF = SGR-2: the
                                    affordance placeholder renders DIM while the row
                                    is SELECTED and UNTYPED; typed text does not
                                    (rig-verified, and on the adversarial payload
                                    byte-identical to the placeholder); and
                                  * WHICH CARD — SurfaceIdentity, captured before the
                                    first key and RE-CHECKED after the nav and again
                                    in the final pre-Enter capture. Every other leg
                                    is equally satisfied by a SUCCESSOR card holding
                                    our text (another controller can resolve card A
                                    and render card B mid-transaction). Two
                                    components: `pane`
                                    (terminal_parser.free_text_surface_identity —
                                    the REAL options 1..target_row-1, CURSOR-blind
                                    AND TARGET-ROW-blind, so it survives the two
                                    mutations the executor ITSELF performs; None ⇒
                                    unrecoverable, never a weaker prefix) and
                                    `anchor` — the OCCURRENCE-unique, out-of-band,
                                    scroll-independent generation id, MANDATORY
                                    (peer-review round-2 P1; derive_identity returns
                                    None without one, so an anchor-less pane never
                                    yields an identity and "None matches None" is
                                    dead by construction): the PreToolUse side-file
                                    occurrence id
                                    (auq_source.peek_surface_identity_for_window —
                                    the hook's per-invocation tool_use_id, minted
                                    BEFORE the picker renders). The pane cannot tell
                                    two identically-optioned AUQs apart, so NO side
                                    file ⇒ the lane DECLINES; PreToolUse is thus a
                                    REQUIREMENT of the lane, README-documented +
                                    doctor-checked.
                                    **THE ANCHOR IS READ BEFORE THE PANE** (round-3
                                    P1, which a change of anchor SOURCE alone would
                                    NOT have closed): derive_identity used to read
                                    the anchor itself, AFTER its caller captured the
                                    pane, minting the chimera (OLD pane, NEW anchor)
                                    — and since the pane component is degenerate
                                    across same-shaped occurrences, that chimera
                                    MATCHES every later observation and commits onto
                                    the successor. derive_identity now TAKES the
                                    anchor; read_surface_anchor runs strictly BEFORE
                                    every capture at all three observation points, so
                                    the only reachable chimera is (NEWER pane, OLDER
                                    anchor), which fails closed. Same "probe FIRST,
                                    capture LAST" discipline as the PR-1 re-verify
                                    (r2 F4).
                                    **THE ANCHOR CARRIES THE SESSION GENERATION**
                                    (round-4 P1 — the stale session cache defeated
                                    the anchor ENTIRELY). It used to resolve the
                                    window's session via the CACHED
                                    WindowState.session_id — a MIRROR of the
                                    hook-written session_map.json, refreshed only on
                                    the monitor's poll cycle. A /clear in the SAME
                                    tmux window rotates the session while the cache
                                    still names the old one, so all three
                                    observations read the PREDECESSOR session's side
                                    file while capturing the SUCCESSOR's pane: they
                                    agree with each OTHER, nothing refuses, and the
                                    Enter commits the answer onto the WRONG QUESTION
                                    (a per-window predicate cannot see it — both
                                    sessions occupy the same window). auq_source now
                                    resolves through
                                    session.read_session_id_for_window_fresh and
                                    EMBEDS the id in the anchor
                                    (auq:sid:<session>:tu:<tool_use_id>), so a
                                    rotation between any two observation points
                                    changes the anchor and refuses; a successor with
                                    no side file yields None, which refuses too. An
                                    EMPTY hook-captured tool_use_id also yields None
                                    (round-4 P2 — a (written_at, content-hash)
                                    composite is a guessable stand-in for an
                                    occurrence witness, not one; scoped to the ANCHOR
                                    path, the GH #48 recap lane keeps its composite).
                                    **AND THE ANCHOR IS BOUND TO THE PANE, NOT TO
                                    THE READ ORDER** (round-5 P1-B — "anchor before
                                    pane" was BACKWARDS). Round 3's ordering
                                    argument silently assumes that a card the user
                                    ALREADY ANSWERED stops looking LIVE on the pane.
                                    PreToolUse writes card B's record BEFORE B
                                    renders, so a pane still holding the answered
                                    card A pairs with B's anchor — (OLD pane, NEW
                                    anchor) — and two AUQs with identical option
                                    labels cannot tell that chimera apart, so the
                                    Enter commits onto B (REPRO-CONFIRMED: with the
                                    guards reverted the executor sends it). THREE
                                    folds, none a bet on ordering: (a) the card must
                                    OWN the pane (plan_from_pane requires
                                    pane_input_box_present is False — a live prompt
                                    REPLACES the input box, a resolved one RESTORES
                                    it; defence in depth, since today's parser
                                    declines that shape independently); (b) each
                                    capture is SANDWICHED between two EQUAL anchor
                                    reads (_observe — the side file only moves
                                    FORWARD, so equality at t0/t2 proves it did not
                                    move at t1, when the pane was captured); (c) the
                                    anchor RECORD's CONTENT must AGREE with the pane
                                    (auq_source.anchor_pane_agreement,
                                    TARGET-ROW-BLIND; match / mismatch /
                                    indeterminate — the last being the overflow
                                    shape, where the anchor stands alone). The
                                    reused _record_consistent_with_pane was VERIFIED
                                    on the live call path (the recorded reuse-claim
                                    rule): it DOES reject differing labels and does
                                    NOT reject a same-labels different-QUESTION
                                    record (a pure-pane parse carries no title and
                                    no option descriptions), so agreement also binds
                                    the record's QUESTION TEXT to the pane — at
                                    PRE-KEYSTROKE observations ONLY (post-write a
                                    long answer can legitimately scroll the question
                                    off a bottom-anchored picker, and a false refusal
                                    there strands a draft inside a LIVE CARD).
                                    Disclosed residual, WIDER than the reviewer
                                    assumed: two AUQs with the same labels AND the
                                    same question text are pane-indistinguishable.
                                A braked window (PR-1's stranded-draft registry) is
                                checked FIRST and DECLINES — the lane is never a way
                                around the brake, and never clears it. VERSION-LICENSED
                                per (surface × CC-version) — the decision_token
                                precedent; an un-characterized release degrades to
                                PR-1's refusal. THE ADDITIVE INVARIANT: every bail
                                BEFORE the first keystroke returns None and the
                                caller falls through to the normal gated
                                deliver_to_window, so the lane can only turn a
                                REFUSED message into a delivered ANSWER — it can
                                never make a deliverable message undeliverable, and
                                it never invents its own refusal for a payload it
                                did not touch. Post-write it OWNS the outcome:
                                DRAFT_WRITTEN (Enter withheld — arms the SAME
                                per-window stranded-draft brake, since the payload
                                sits in the card's row) or, past the Enter,
                                DELIVERED / COMMIT_UNKNOWN (honest, never
                                auto-retried). Carries the GH #50 UserTurnStamp —
                                PR-2 is the FIFTH Enter-commit path and a free-text
                                answer IS a user turn. Integration seam is the
                                AGGREGATOR FLUSH (the only place that knows the
                                bundle's provenance), gated first on the cheap
                                in-memory has_interactive_surface so an ordinary
                                send pays nothing. Eligible = (typed prose OR voice)
                                AND none of caption / attachment — a REPLY-QUOTED
                                payload IS eligible (owner decision 2026-07-12) and
                                Claude receives the full rendered payload, quote
                                included. Config-free leaf (the flag is seeded by
                                main._run_bot); pull-only, no observer.
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
                                picks the fresh render candidate (select_fresh_prose,
                                the PR-1 additive-OR of the render-time TTL leg with
                                an emission-anchor leg [emitted_at - lookback,
                                emitted_at + eps] — emitted_at is a stable
                                picker-emission instant: AUQ written_at / the EPM
                                poller stamp, selected by modality in interactive_ui;
                                recovers the dominant miss where the poller detected
                                the picker tens of seconds after the prose finalized,
                                blowing the TTL — + the Item-3/P2-1 STRICT `final_at >
                                not_before` turn-boundary filter; not_before is the
                                delivery wall-clock from message_queue, None ⇒
                                filter disabled [the anchor OR leg still applies];
                                only emitted_at=None ⇒ TTL-only),
                                owns the SINGLE dedup-parity hash
                                (normalize_prose / prose_norm_hash) shared with the
                                dedup, the shown-live marker store (record/read/
                                consume + the consume-inclusive was_shown_live idem-
                                potency guard — markers live in the same per-session
                                file), and the lifecycle (teardown_session / gc_stale —
                                gc_stale takes an INJECTED is_live_session predicate,
                                Item 3/P2-2: keep a live session's stale file + its
                                dedup markers, conservative-skip on predicate raise,
                                re-stat-before-unlink TOCTOU guard).
                                Imports utils only (the predicate is injected, never
                                imported — md_capture stays a leaf).
  _md_display_appender.py     ─ The MessageDisplay hook itself: a tiny stdlib-only
                                appender run directly by the interpreter (NEVER
                                imports the package — forceSyncExecution latency).
                                Keys the per-session file by Path(transcript_path).stem
                                (resume-safe), appends the raw payload as one NDJSON
                                line via a single O_APPEND os.write, always exits 0.

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + rate_limit_send
  output_prefs.py     ─ Per-user output-verbosity resolution (plan v4 PR-1):
                        frozen OutputPrefs snapshot per recipient, layering
                        "stored user override > EXPLICITLY-set legacy env
                        default > preset" (env vars are defaults, never
                        ceilings). PRESETS verbose (≡ pre-settings behavior)
                        / standard (the production default since PR-2; the
                        TEST SUITE pins verbose via conftest so the floor
                        stays today-shaped) / compact / quiet. Stateless
                        leaf (imports config + session only); resolve(user_id)
                        is consulted at every emission point: the per-recipient
                        👤-echo gate in bot.handle_new_message (top of the
                        per-user loop, mirroring the removed monitor skip;
                        <task-notification> envelopes exempt via the public
                        response_builder.is_task_notification), the legacy
                        tool_activity gate at the old SHOW_TOOL_CALLS position
                        (drops ALL tool surfaces incl. Agent/Task — the
                        faithful env-false mapping; presets never set it),
                        digest line/snippet/live-line budgets in
                        _compact_*_line/_render_*_digest (live_lines=0 ⇒
                        header-only, NO hidden-events line), quiet's
                        digest_card=False (no digest state EVER created — incl.
                        _bump_agent_activity_counter, hermes r3 P1-1; images +
                        attention-dismiss still fire), subagent_cards=off (no
                        sidechain card; Wave A keep-alive unaffected),
                        agent_dispatch_msg=False (🤖 dispatch bubble suppressed
                        INSIDE _process_agent_task AFTER the _agent_tool_ids
                        stash, so the 🤖✅ report still renders — codex r2
                        P1-1), todo_card, context_footer, and /history's
                        user-echo filter (the ONLY pref history honors —
                        history stays the full-fidelity escape hatch). The
                        monitor-level user-entry skip + sidechain display drop
                        are REMOVED (session_monitor always emits;
                        consume_bot_sent_text stays in the monitor —
                        single-consumer). Stored per-user in state.json
                        "user_settings" via SessionManager named mutators
                        (downgrade loss accepted). UI: /settings command +
                        stg:<field>:<value>:<owner_user_id> callbacks in
                        callback_dispatcher/settings.py — owner check rejects
                        another allowed user's tap; preset tap = clean-slate
                        replace_user_settings. A STORED preset choice
                        overrides the ENTIRE env layer (env = defaults for
                        the un-chosen, never ceilings — hermes PR-1 P1).
                        PR-2 wires the collapse policies: W1
                        digest_on_done (keep / summary / delete) at
                        _finalize_activity_digest — summary = ONE-line
                        terminal render (run-state header survives, so a
                        post-turn 🔔 still shows; counts + duration frozen
                        on state at finalize for edit-stable repaints);
                        delete = the cancellation-safe removal protocol
                        (shield wraps the LOCK-HOLDING flush in both
                        debounce schedulers so cancel only lands in the
                        sleep; upsert re-checks tombstone + slot identity
                        under the lock; finalize-delete takes the lock,
                        tombstones, deletes best-effort, pops the slot —
                        restart-orphan accepted residual). W2
                        subagent_cards summary: the sidechain's own
                        end-of-turn (MessageTask.stop_reason, plumbed from
                        the NewMessage) collapses its ↳ card to one line
                        via the synchronous _collapse_subagent_digest;
                        _finalize_activity_digest is the backstop sweep for
                        empty-final sidechains; the collapsed slot is a
                        tombstone (late blocks never re-inflate; the 🤖✅
                        report is untouched). Fix 5 (ISSUE-6): the Workflow
                        shape rides this same contract PLUS a deterministic
                        route-FIFO close collapse —
                        enqueue_subagent_collapse puts a subagent_collapse
                        control task (flood/RetryAfter-safe via
                        _RETRYABLE_TASK_TYPES) that the per-route worker runs
                        AFTER the run's content tasks →
                        collapse_subagent_cards_with_prefix (summary-gated,
                        prefix-scoped, idempotent).
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
                        + the poller-local _prev_run_state dedup cache). Item 1:
                        its same-hash idle branch ALSO re-mints a live AUQ card
                        on SOURCE drift (side_file aged past the read-TTL → pane)
                        — re-resolve + resolve_ask_form (gates out non-AUQ panes) +
                        pick_token.peek_route_source by ROUTE (fingerprint-agnostic,
                        since the side-file-form and pane-form fingerprints differ)
                        vs the live source; on mismatch re-render via
                        handle_interactive_ui so the first tap dispatches (the
                        read-TTL itself is untouched). The D3-β sibling. Also
                        posts the edge-triggered background-only "labeled
                        silence" card (_maybe_post_bg_only_card off
                        snapshot.background_only + the poller-local one-shot
                        _bg_only_card_posted cache) once per episode when a
                        parent-idle route stays projected-Busy on live bg keys.
  response_builder.py ─ Response pagination and formatting
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode UI + the flag-gated
                        Permission / Workflow approval-gate cards (display-only
                        in PR-1: a labels card + the manual ↑/↓/⏎/Esc nav
                        keyboard, no option-pick buttons; behind
                        CC_TELEGRAM_PERMISSION_PROMPTS)
  directory_browser.py─ Directory selection + session picker UI for new topics
  cleanup.py          ─ Topic state cleanup on close/delete
  callback_data.py    ─ Callback data constants
  auq_ledger.py       ─ Wave 3 restart-safe write-ahead ledger for AUQ
                        option-pick dispatches. JSONL at auq_action_ledger.jsonl
                        keyed by (route_hash, fp8, opt). v2.1.168 state machine:
                        accepted → dispatched (confirmed advance), or
                        not_advanced (pre-commit bail — Enter never sent) /
                        commit_unconfirmed (Enter sent, advance unconfirmed);
                        ``failed_reason`` carries the sub-reason. digit_sent /
                        failed_before_digit / failed_after_digit are legacy-only
                        (on-disk compat). ``released`` tombstones a window's rows
                        on tool_result-confirmed resolution ONLY:
                        ``release_window`` fires at the explicit AUQ
                        ``tool_result`` branch in ``bot.handle_new_message``
                        AND the startup reconciler's positive-proof branch —
                        NEVER at the generic ``forget_ask_tool_input`` teardown
                        (`/clear` / session replacement / surface clear are
                        not resolution proof; releasing there would remove a
                        dispatched-but-UNRESOLVED row's single-use brake) — so
                        a same-day byte-identical AUQ (same content-derived
                        key) is dispatchable again. 24h retention is enforced on READ —
                        load collapses latest-per-key FIRST then drops an
                        expired latest key (never resurrecting an older row);
                        ``lookup()`` re-checks the cutoff and treats a latest
                        ``released`` row as None. Otherwise ``lookup()`` returns
                        raw rows; the **callback handler** projects pre-restart
                        accepted rows to ``unknown`` (via
                        ``process_start_time()``) so it refreshes the card
                        instead of re-dispatching. ``pick_token``'s sibling-
                        claimed recovery guard filters by STATE: not_advanced /
                        released / failed_before_digit do NOT spend the row;
                        ``accepted`` stays claimed REGARDLESS of process epoch
                        (crash-ambiguous — Enter may have been sent).
  notify_source.py    ─ Wave B Notification-hook side-file trust boundary
                        (leaf; imports session.peek + utils, tmux_manager
                        deferred). Owns notify_pending/<session_id>.json:
                        notification_pending_for_window applies the HARD
                        window_key == "tmux_session:window_id" read predicate
                        (double-resume sibling safety) + schema/future-skew
                        validation, deliberately NO read-TTL (staleness is
                        runtime-state-driven via NOTIFY_TTL_SECONDS in the
                        poller); unlink_if_generation_matches is the re-read
                        generation-guarded unlink (a hook re-fire between
                        read and unlink survives); unlink_for_session is the
                        teardown seam; gc_stale is the 24h startup backstop
                        with the injected is_live_session conservative-skip.
  dashboard.py        ─ Wave C cross-topic dashboard: one owner+chat-scoped
                        overview message per (chat_id, owner). Owns /dashboard
                        (claim the invoking topic as host; re-run elsewhere
                        MOVES it; /dashboard pin is opt-in), the pure renderer
                        (render_dashboard(owner_id, chat_id) — bindings filtered
                        to the owner AND to the dashboard's own chat via
                        session_manager.get_group_chat_id, FAIL CLOSED: an
                        unresolvable chat is excluded, never leaked cross-forum
                        (hermes review P1) + route_runtime.snapshot per route.
                        TRUST BOUNDARY (hermes R2 P1 + GH #41): /dashboard
                        NEVER writes set_group_chat_id — thread ids are
                        chat-local, so a host claim in chat B's unbound
                        thread N would poison the mapping of chat A's bound
                        topic N and leak it onto chat B's dashboard.
                        group_chat_ids is written by the topic message seams
                        (text/photo/voice/document, forward_command with a
                        real thread_id, topic_edited) AND — GH #41 — by
                        registry-RECOGNIZED callback taps (unknown callback
                        data never writes); an UNBOUND (user, thread) write
                        is the legitimate directory-browser bootstrap. The
                        enforcement moved INTO set_group_chat_id itself: the
                        sticky-when-BOUND guard REFUSES overwriting an
                        existing entry with a different chat_id while the
                        user holds a live thread BINDING for that thread (a
                        colliding cross-forum thread id can't steal a bound
                        topic's mapping; a STALE binding freezes the old
                        mapping until the stale-window unbind clears it —
                        disclosed residual). The dashboard carries its OWN
                        chat (effective_chat.id at claim, the record key
                        afterwards) explicitly through every
                        topic_send/topic_edit/topic_delete;
                        🔔 = WAITING_ON_USER or idle with
                        last_assistant_turn_ended_at > last_user_turn_at, both
                        non-None; ages minute-coarse from the monotonic
                        last_event_at), and the PULL-ONLY refresh driver
                        maybe_refresh_dashboards (called once per status-poll
                        sweep; rendered-content hash → edit only on change, so
                        run-state transitions AND bind/unbind/rename repaint
                        without an observer; MESSAGE_NOT_MODIFIED = success;
                        MESSAGE_NOT_FOUND — the distinctly-classified "message
                        to edit not found" — self-heals via re-send +
                        update_dashboard_msg_id; a generic OTHER edit failure
                        only logs and retries next sweep, NEVER re-sends
                        (re-sending on a transient would orphan the still-live
                        message — review P2-2); a topic-shaped outcome clears
                        the record — never a self-heal loop into a dead topic).
                        A per-(chat_id, owner_id) asyncio.Lock serializes the
                        whole Telegram-I/O-spanning claim/move/self-heal flow
                        (pre-C fix 1) with a post-send loser-cleanup re-read.
                        BOUNDARY: reads route_runtime.snapshot + session_manager,
                        sends via message_sender ONLY; never enqueues status
                        updates, never touches the message-queue module or its
                        send-layer caches, never mutates route_runtime, registers
                        no observer (c313657 forbidden). Persistence is
                        SessionManager-owned (state.json "dashboards" key, sync
                        get/set/clear/update_msg_id/set_pinned methods through
                        the ONE _load_state/_save_state path);
                        clear_dashboards_in_thread(thread_id, chat_id=…) is the
                        CHAT-SCOPED topic-teardown seam (thread ids are
                        chat-local — review P2-3; chat_id=None falls back to the
                        all-chats sweep with a warning), wired from
                        cleanup.clear_topic_state (chat resolved via
                        group_chat_ids) AND bot.topic_closed_handler's
                        no-binding branch so a dedicated binding-less dashboard
                        host topic is cleaned on close (review P2-4).
  updater.py          ─ /update orchestration (owner-only manual command, no
                        scheduler). run_update(..., scope): (1) updates the CLI
                        binary via shlex.split(config.claude_command)[0] +
                        ["update"] (create_subprocess_exec, NO shell=True;
                        non-zero is non-fatal — runs in BOTH modes), (2) restarts
                        each IDLE_CLEARED + pane-idle route IN PLACE via
                        tmux_manager.restart_claude_in_window (reusing the window
                        id → routing preserved), SEQUENTIALLY; a route projected
                        RUNNING (background agent) or WAITING (pending prompt) is
                        deferred. SCOPE: scope=None walks iter_thread_bindings
                        (the FLEET form, /update all); scope=(user, thread) —
                        the RESOLUTION INPUTS, never a captured window id —
                        restarts ONLY the invoking topic's window, RE-RESOLVED
                        via resolve_window_for_thread AFTER the CLI phase
                        (codex P2: the topic can be unbound/rebound during the
                        up-to-120s update; bot.update_command pre-resolves only
                        for the fast unbound error; unbound post-CLI → honest
                        scoped summary, no restart; rebound → the CURRENT
                        window restarts). Scoped renders _format_scoped_summary
                        (one line); fleet renders _format_summary. The
                        single-flight guard covers both. Idle gate is
                        TWO-factor: route_is_idle =
                        run_state IDLE_CLEARED AND terminal_parser.pane_looks_idle
                        (the pane ground-truth, since run-state can lag a
                        mid-generation pane; pane_looks_idle also rejects a pane
                        whose status bar carries a live `· N shell`
                        background-jobs token — a restart would kill them).
                        /update ALWAYS calls it with the DEFAULT
                        allow_background_shells=False — that leg is
                        restart-specific and only the read-only /cost + /usage
                        overlay lane opts out of it (2026-07-11).
                        reassociate_routing mirrors the
                        proven directory-browser resume path (ws.session_id
                        override + monitor register/offset at the POST-RELAUNCH
                        stat-stable EOF, a bounded stat-until-stable loop —
                        offset is always a real stat so offset <= filesize and a
                        truncating replay never trips the reset-to-0 flood).
                        A SKIPPED_NO_EXIT window's summary line discloses the
                        aftermath ("sent /exit but the pane didn't drop to a
                        shell … the session may be dead; check the window"),
                        and every no-CONFIRMED-relaunch exit QUARANTINES the
                        window (tmux_manager registry; incl. RELAUNCH_
                        UNCONFIRMED — the post-relaunch ~10s confirm poll
                        never observed Claude, its own summary disclosure):
                        send_to_window re-checks pane_current_command before
                        typing into it — ONLY the strict pane_command_is_claude
                        version-string proof clears + delivers; anything else
                        (shell / vim / None) refuses with an in-topic "NOT
                        delivered" error (see the /update design bullet).
                        Module-level
                        single-flight guard rejects a concurrent /update.
                        Collaborators (session_mgr / tmux / monitor) injected;
                        pull-only, no route_runtime mutation, no observer.
  pick_intent.py      ─ D2 restart-recovery: durable per-callback-TOKEN AUQ pick
                        mint-intent store (leaf; imports only utils). Append-only
                        JSONL (row + tombstone lines) at pick_intent.jsonl, 24h
                        retention + compaction. record_row (fresh aqp: render,
                        supersede different-fp rows only) / lookup_intent
                        (validated, sibling-aware) / consume_row (row single-use)
                        / teardown_window / reset_for_tests. pick_token.
                        recover_and_consume reads it to re-dispatch a token-less
                        tap after a restart.
  late_answer.py      ─ Wave A AFK auto-resolve adaptation (leaf; stdlib +
                        callback_data helpers only). Owns is_afk_auto_resolve —
                        the two-factor detection for the ≥2.1.198 ~60s AUQ
                        auto-resolve (unanchored drift-tolerant regex over the
                        sentinel-wrapped tool_result text + the AUTHORITATIVE
                        toolUseResult.answers non-empty ⇒ False qualifier;
                        meta-absent = sentinel-strip → negative wrappers reject
                        FIRST → anchored-start match, best-effort under the
                        **AskUserQuestion**(…) summary prefix — safe false
                        negative). Also the in-memory aql: card registry
                        (token → LateAnswerCard, live → in_flight → consumed
                        single-use; finish_send(False) re-arms the retry),
                        keyboard_rows (plain tuples — the leaf never imports
                        telegram), the ⏰ card-text + single-line correction-
                        message templates, invalidate_window (wired at
                        forget_ask_tool_input + the remember_ask_tool_input
                        rotation backstop) and invalidate_topic (topic close,
                        beside route_runtime.clear_routes_for_topic — NOT the
                        queued-routes loop). NOT persisted (restart ⇒ graceful
                        expired modal), NOT route_runtime, no observers. The
                        observed afkTimeoutMs toolUseResult field is documented
                        as a candidate future discriminator only. Executor:
                        callback_dispatcher/late_answer.py (owner + stale-window
                        auth, has_interactive_surface + side_file_live_for_window
                        freshness guards, sending-state edit, the effort.py
                        route-ordering delivery subsequence with the GH #50
                        UserTurnStamp pre-commit request, failure re-attaches
                        the original keyboard).
  pane_signals.py     ─ GH #43 pane-derived per-route DECORATION store (true
                        leaf — imports nothing from the app; in-memory only).
                        Holds the latest pane-parsed background-shell count per
                        route (terminal_parser.parse_background_jobs: chrome-
                        region anchored — status-bar `· N shell` primary, churn
                        `· N shell(s) still running` fallback, MAX on conflict;
                        0 = chrome-present-no-token, None = no chrome → caller
                        skips so a bad frame never erases a fresh count).
                        Written by status_polling on every full capture
                        (record_background_jobs returns CHANGED → poller fires
                        refresh_activity_digest_if_present — pull-side repaint,
                        no observer); read by the collapsed done-card renderer
                        (`⏳ N background job(s)` suffix, IDLE routes only) and
                        /dashboard (⏳ replaces ⚪ on idle+fresh-count>0; 🔔
                        outranks). peek staleness BG_JOBS_MAX_AGE_S=30s (3× the
                        capture watchdog). NEVER a run_state input, NEVER
                        typing (recorded user decision). Teardown beside every
                        route_runtime clear seam: poller window-gone,
                        cleanup.clear_topic_state (topic-wide), inbound stale-
                        window unbinds, message_queue.teardown_route, bot
                        /clear + session_monitor rotation (mark_session_reset
                        sites).
  decision_token.py   ─ Stage B2 tappable-Decision-dispatch leaf (pure stdlib —
                        NEVER imports pick_token / auq_source / route_runtime /
                        any JSONL resolver; kill criteria in the docstring). Owns
                        three storage concerns: (1) the in-memory single-use
                        `dcp:` token store — mint_row / peek / consume by
                        EXCLUSIVE RESERVATION with §3(3) sibling-burn (a winning
                        consume tombstones the whole route row) + 300s TTL +
                        refresh_route_deadlines (D3-β analogue) + teardown_route;
                        (2) the §5b(c) per-window nav-generation registry —
                        current_nav_generation / rotate_nav_generation (per gate
                        render) / invalidate_on_dispatch (the in-lock op at
                        `dispatched`; restart wipe → fail-closed); (3) the §2b
                        known-good (family × CC-version) `_DECISION_DISPATCH_TABLE`
                        module constant + identify_family (EXACT ordered
                        option-labels + the family's anchored title pattern) +
                        lookup (exact-string version membership). The §7 dispatch
                        flag lives here as a module-local bool (set_/…_enabled),
                        SEEDED from config by main._run_bot (config-free leaf).
  artifacts.py        ─ Artifact delivery lane leaf (stdlib + callback_data
                        only — NEVER config/telegram; max_bytes + extra roots
                        INJECTED at the callsites). Owns: extract_artifact_
                        candidates (pure prose path extraction, ARTIFACT_EXTS
                        allowlist — every deliverable type: docs/images/audio/
                        video/archive/office/data; source-code exts EXCLUDED),
                        resolve_artifacts / resolve_single
                        (expanduser → cwd-join → resolve[follows symlinks] →
                        is_relative_to a RESOLVED allowed root → regular-file +
                        size cap; fail-closed on empty cwd / traversal /
                        symlink-escape; a relative candidate missing under a
                        harness .claude/worktrees cwd RETRIES against the
                        derived main-repo root, cwd hit wins, pinned+displayed
                        to the matched root — rejects under BOTH),
                        open_validated_artifact (the TOCTOU
                        close: re-check containment vs the roots PINNED in the
                        row + O_RDONLY|O_NOFOLLOW open + fstat regular-file/size
                        ON THE FD → the open file object IS the upload source,
                        never a re-opened pathname), and the in-memory dlf: token
                        registry (single-FLIGHT not single-use — a re-tap
                        re-uploads; a row PINS the resolved path + the resolved
                        allowed roots, codex r2 P2-1) + the (route, path)
                        offer-dedup map (30 min) + 24h token TTL. keyboard rows
                        are plain (label, callback_data) tuples (the executor /
                        message_queue wraps them — the leaf never imports
                        telegram). In-memory only; restart wipes it (a dead
                        button answers a graceful expired modal; the card body
                        is PATHLESS — the prose above names the file(s) → /file
                        is the restart net); no observer. Observability
                        (2026-07-10, privacy-hardened): mint logs an INFO with
                        ONLY the minted rows' relative display names + root
                        KINDS (cwd/extra/main-root) + row/overflow counts —
                        never absolute paths or root paths, never the
                        deduped/overflow entries (MintedCard.minted is the
                        exact-correlation carrier); the executor logs INFO at
                        tap / open (ok+reason) / send outcome — the download lane
                        is reconstructable from logs.
                        Executor: callback_dispatcher/artifacts.py (dlf: — the
                        aql: guard order: lookup → owner → stale-window → single-
                        FLIGHT → answer-first → open_validated_artifact →
                        message_sender.send_document(open fd) → success/failure/
                        RetryAfter; fd closed in a finally). Detection seam:
                        bot._maybe_offer_artifacts (parent assistant prose only,
                        gated on prefs.artifact_card; enqueues the card AFTER the
                        block's content task). /file <path> (bot.file_command) is
                        the durable escape hatch (raw arg tail; NOT ext-gated).
                        Teardown: invalidate_topic (clear_topic_state, the
                        covering seam) + invalidate_window (the four inbound
                        stale-window unbinds). Pull-only; no observer (c313657).
  usage_cache.py      ─ /cost + /usage overlay-result cache leaf (pure stdlib;
                        in-memory only, NO state file / env var / route_runtime
                        field / observer). record(route, session_id, text) writes
                        ONLY from the overlay SUCCESS path, keyed ROUTE + the
                        window's CURRENT session identity (a later peek whose
                        session differs is a MISS — window ids recycle);
                        peek(route, session_id) reads within a 30-min TTL
                        (CACHE_TTL_SECONDS — an accepted SHORT staleness window
                        since limit bars reset minutes after capture). Read by
                        bot._build_usage_snapshot for the busy-path fallback card.
                        Teardown clear_route / clear_routes_for_topic mirror the
                        pane_signals route-scoped seams (bot /clear +
                        session_monitor rotation mark_session_reset, the four
                        inbound_telegram stale-window unbinds,
                        cleanup.clear_topic_state); reset_for_tests co-located.
                        Pull-only; no observer (c313657).

State files (~/.cc-telegram/ or $CC_TELEGRAM_DIR/):
  state.json               ─ thread bindings + window states + display names +
                             read offsets + dashboards ("<chat_id>:<owner_id>" →
                             {thread_id, msg_id, pinned} — the /dashboard host
                             record; SessionManager-owned so the fixed-dict
                             state rewrite round-trips it) + user_settings
                             ("<user_id>" → {verbosity, knob overrides} — the
                             per-user /settings output-verbosity store;
                             shape-validated on load, knob values re-validated
                             by output_prefs on read; downgrade loss accepted)
  session_map.json         ─ hook-generated window_id→session mapping (SessionStart)
  monitor_state.json       ─ poll progress (byte offset) per JSONL file
  interactive_state.json   ─ persisted picker msg ids + AUQ context markers
                             (survives launchctl kickstart)
  auq_pending/<sid>.json   ─ PreToolUse side files for AskUserQuestion;
                             captures tool_input before Claude renders picker;
                             dir mode 0700, files mode 0600; kept across
                             multi-select toggles; cleaned on AUQ tool_result,
                             session replacement, or startup GC
  notify_pending/<sid>.json ─ Wave B Notification-hook side files; window-keyed
                             {ts, window_key, generation, kind} markers (mode
                             0600 under dir 0700) — NO notification message
                             text. Written by the hook on a Claude permission/
                             approval prompt; read by notify_source with the
                             hard window_key predicate; consumed by the poller
                             into route_runtime.mark_notification_pending and
                             unlinked generation-guarded per the returned
                             NotificationMarkResult; also unlinked on session
                             replacement, /clear, topic close; 24h startup GC.
  auq_action_ledger.jsonl  ─ Wave 3 append-only ledger of AUQ option-pick
                             lifecycle transitions (mode 0600; latest line per
                             key wins). The callback handler consults this
                             BEFORE the in-memory token table so a duplicate
                             tap after process restart returns "Action already
                             received" instead of re-committing the pick. 24h
                             retention enforced on read (load + lookup; file
                             rewritten only by over-cap compaction). `released`
                             rows tomb a window's keys on tool_result-confirmed
                             AUQ resolution only (the AUQ tool_result branch in
                             bot.handle_new_message + the startup reconciler's
                             positive-proof branch — never the generic
                             forget_ask_tool_input teardown) so a re-asked
                             identical question is dispatchable again. Stage B2
                             SHARES this ledger for the `dcp:` Decision lane: the
                             key's `fp8` derives from the domain-prefixed
                             `decision_prompt_fingerprint` (no cross-lane
                             collision — §8), and `auq_ledger.release_key(key)`
                             tombs a SINGLE Decision key on the confirmed-gone
                             dispatch proof.
  pick_intent.jsonl        ─ D2 restart-recovery: durable per-callback-TOKEN AUQ
                             pick mint-intent store (mode 0600; append-only row +
                             tombstone JSONL; 24h retention + compaction). Written
                             at the fresh aqp: single-select/Submit render. After
                             a restart wipes the in-memory pick tokens, the
                             peek_none/expired branches RECOVER + re-dispatch the
                             first token-less tap (row-scoped single-use; owner +
                             stale-window auth; read-TTL-free source parity).
                             SEPARATE from auq_action_ledger.jsonl by design.
                             Tombed on AUQ/EPM resolution, /clear, topic close.
  md_hook_settings.json    ─ Bug 2 bot-managed Claude Code settings registering
                             the MessageDisplay hook; passed to bot-launched
                             sessions via `claude --settings` (NOT in global
                             ~/.claude/settings.json); merges with global hooks.
  msg_display/<sid>.ndjson ─ Bug 2 MessageDisplay live-prose capture; one per
                             session keyed by the transcript filename stem
                             (resume-safe); dir mode 0700, files mode 0600.
                             The appender appends each streaming delta; the bot
                             accumulates by MessageDisplay.message_id into
                             completed prose, posts it before the picker card,
                             and records shown-live/consumed dedup markers plus
                             AUQ-only surface_floor/recap_shown markers in the
                             SAME file. Removed on AUQ/EPM resolution
                             (forget_ask_tool_input) / session replacement /
                             /clear / topic close; 1h startup GC backstop.
  images/ + files/         ─ downloaded photo/document attachments forwarded to
                             Claude; dir mode 0700, downloads chmod'd 0600 after
                             write (uploads can carry sensitive content). The
                             dirs are create-and-REPAIRED to 0700 at import
                             (mkdir mode is a no-op on an existing dir, so an
                             upgraded install's 0755 is tightened); a chmod
                             OSError logs a WARNING and never fails the download.
  message_refs.db          ─ SQLite provenance index for reply-context resolution
  log-archive/             ─ gzipped rotations (only if rotation LaunchAgent installed)
```

## Key Design Decisions

- **`/update` in-place session restart (owner-only, fail-closed, idle-only; TOPIC-SCOPED by default)** — updating `~/.local/bin/claude` repoints a symlink; a running `claude` process stays pinned to its launch-time version until restarted. `/update` (`handlers/updater.run_update`) updates the CLI, then restarts **idle** bound session(s) IN PLACE inside their existing tmux window so they adopt the new version WITHOUT touching `window_id` / `thread_bindings` / routing. **Scope (owner decision 2026-07-10):** `/update` (no arg) is SCOPED — it restarts ONLY the invoking topic's window. `bot.update_command` pre-resolves `(user, thread) → window_id` via `resolve_window_for_thread` ONLY for the fast unbound-topic error (an unbound topic replies an error and executes nothing, not even the binary update) and passes `scope=(user_id, thread_id)` — the RESOLUTION INPUTS, never a captured window id (codex review P2): the up-to-120s CLI phase runs first and the topic can be unbound / closed / rebound to a DIFFERENT window in that interval, so `run_update` re-resolves the binding AFTER `_run_cli_update`, immediately before the restart walk — unbound by then → an honest scoped summary ("topic was unbound during the update — nothing restarted"); rebound → the CURRENT window restarts (that's what "this topic" means). `/update all` (casefolded) is the FLEET walk of every bound topic (today's behavior byte-preserved, `scope=None`; fleet snapshots bindings after the CLI phase already); any other argument gets a usage reply and executes nothing. The default is scoped because the fleet walk restarts idle sessions in DORMANT topics via `claude --resume`, and a revived idle session is not inert (CC 2.1.206 generates contextual ghost suggestions / background token-drip) — reviving dormant topics must be EXPLICIT (`/update all`), never a side effect. Phase 1 (the `claude update` binary refresh) runs in BOTH modes (global, no tokens); only the restart set differs. The module-level single-flight guard covers BOTH modes. The SCOPED summary is a single line for the one topic (restarted / deferred-with-reason / skipped-with-reason); the FLEET summary keeps the `♻️ Restarted N idle · deferred M busy · skipped K` format. The per-window mechanic (`restart_claude_in_window`, `reassociate_routing`, the two-factor idle gate, quarantine) is byte-identical in both modes. `tmux_manager.restart_claude_in_window` runs the per-window mechanic ENTIRELY inside `window_send_lock(window_id)` (the `esc_command` reject-if-held pattern): re-check idle → send `/exit`+Enter → POLL a fresh stderr-checked `pane_current_command` query until the pane drops to a shell (`pane_command_is_shell`; a TWO-PHASE bounded wait — `~5s` primary + `~10s` grace, 15s total: `/exit` is irrevocable, so a LATE exit inside the grace is RECOVERED with a normal relaunch (INFO-logged) instead of stranding a bare shell in the still-bound topic) → **FAIL-CLOSED: never relaunch into a live TUI** → relaunch `_compose_launch_command(config.claude_command, md_settings, tracked_session_id)` → re-associate routing (`reassociate_routing`: ws.session_id override + monitor offset at the post-relaunch stat-stable EOF). A wait that still expires returns `SKIPPED_NO_EXIT` and the Telegram summary honestly discloses the aftermath ("sent /exit but the pane didn't drop to a shell within 15s — the session may be dead; check the window before sending messages"). **Post-/exit QUARANTINE (Hermes P1 + r2 P1-A/P1-B):** every exit WITHOUT a CONFIRMED relaunch — `SKIPPED_NO_EXIT`, a relaunch-keystroke send failure on a confirmed-shell pane, and `RELAUNCH_UNCONFIRMED` — quarantines the window (`tmux_manager.mark_window_quarantined`; the reassociate-failure `ERROR` is NOT quarantined — Claude was proven alive first). Relaunch is confirmed by PANE OBSERVATION, never keystroke acceptance (r2 P1-A: a broken `CLAUDE_COMMAND` / invalid auth / instant crash drops straight back to the shell): after the relaunch keystroke a bounded confirm poll (`RELAUNCH_CONFIRM_TIMEOUT_S` ~10s, shell-wait cadence) must observe the strict Claude proof before the quarantine clears; expiry keeps the quarantine and returns `RELAUNCH_UNCONFIRMED`, whose summary line discloses it ("relaunched but Claude wasn't seen running within 10s — check the window; sends stay blocked until Claude is seen alive"; a late boot self-heals at the next send's re-check). A Telegram message queued on the window send lock DURING the shell-wait would otherwise flush the instant the abort returns and be typed into (and executed by) a bare shell, so `SessionManager.send_to_window` re-checks `pane_current_command` for a quarantined window INSIDE the send lock before typing. Proof of life at BOTH seams (send re-check + relaunch confirmation) is STRICTLY `pane_command_is_claude` — a fullmatch on the A.0 version-string shape (`2.1.201`; suffix tolerated, a leading name never) — because "not a shell" is NOT proof (r2 P1-B: a user who followed "check the window" may be running vim/python/ssh in the stranded pane, and typing+Enter would land in THAT program): Claude-shaped → clear + deliver; EVERYTHING else (shell, editor, REPL, node, None) REFUSES fail-closed with an explicit "Message NOT delivered … exited during /update" error (`session.QUARANTINE_SEND_REFUSED_MSG`), surfaced in-topic — the synchronous callers (forward_command / effort / aql / pending replay) via their existing `(False, msg)` reporting, the debounced aggregator flush via the bundle-captured handler bot + `safe_send` (equality-matched on the refusal constant; other failures keep the pre-existing log-only shape; the media-group boundary force-flush re-applies the captured bot onto the fresh bundle — r2 P2). Zero overhead for unquarantined windows (one dict lookup). The quarantine also clears on a later successful restart (at the post-relaunch CONFIRMATION), `kill_window`, the inbound stale-window unbinds, and `clear_topic_state`; `/esc`'s Escape, the fixed-literal `/usage` probe, and the interactive nav/digit keystrokes stay UNGATED (no arbitrary text+Enter — inert or harmless at a shell prompt, and the pick dispatch re-validates the live pane form anyway). In-memory only — a bot restart drops the quarantine (accepted residual: the refusal net is gone until a fresh `/update` marks it again). Two further disclosed residuals: a Claude that crashes AFTER confirmation is out of scope (the same class as any post-restart self-exit; the pane stays discoverable), and a future CC version that changes the reported `pane_current_command` shape makes quarantined sends keep refusing — fail-closed, the correct direction; recoverable via a `/update` rerun, a window recreate, or a bot restart (flagged beside the A.0 empirics comment for the next TUI-drift audit). Every `send_keys` return is checked (silent False on a vanished window). Empirically de-risked (A.0, CC 2.1.20x): `claude --resume` reports the SAME session id and pure-appends to the same JSONL, so NO alias / monitor-authority change is needed — the stat-stable-EOF registration (a bounded stat-until-stable loop: re-stat every ~0.3s until the size is unchanged across two consecutive stats, ~5s cap, LAST observed size on expiry — the offset is always a real stat) keeps `offset <= filesize` so a truncating replay never trips the `_read_new_lines` reset-to-0 history flood. Idle is TWO-factor (`route_runtime` `IDLE_CLEARED` AND `terminal_parser.pane_looks_idle`), restarts run SEQUENTIALLY, a module-level single-flight guard rejects a concurrent `/update`, and busy/waiting/background-agent routes are deferred. The idle-gate pane is captured WITH ANSI and pre-cleaned by `terminal_parser.clean_ghost_input_text` before `pane_looks_idle` — CC 2.1.206 renders a DIM (SGR-2) ghost suggestion in the empty input row that a plain capture reads as a typed draft (false-deferring the restart); the pre-clean blanks a fully-dim ghost (bare `❯` = empty) but leaves a real draft / any dim+normal MIX untouched (fail-closed, SGR-2 discriminator, fixture-pinned on 2.1.206 — a documented TUI-drift surface); `pane_looks_idle` itself is byte-untouched. `pane_looks_idle` is STRUCTURAL + positive-evidence + fail-closed: no `esc to interrupt` / no live `is_interactive_ui` surface, the input row must be the EMPTY `❯` prompt bracketed by the BOTTOM pair of `──` rule separators (a body `> blockquote` is above that pair, and a typed-but-unsent draft is rejected), a ready-for-input status-bar marker must be present below the box (a dropped-footer mid-redraw has none → fails closed rather than reading absence as idle), AND the status bar must carry NO live `· N shell` background-jobs token (leg 5: `parse_background_jobs` ≥ 1 → defer — a restart would silently kill the user's background shells; `None`/`0` never block, since the frame already passed the positive ready-chrome proof). **Leg 5 is RESTART-specific and belongs to `/update` alone:** `pane_looks_idle` / `classify_pane_idle_failure` take a keyword-only `allow_background_shells` (**default False — `/update` always uses the default and its guard is byte-identical**) that the read-only `/cost` + `/usage` overlay lane passes `True`, because that transaction restarts nothing and therefore cannot kill a shell (see the TUI-overlay bullet). Never widen the opt-in to a path that sends `/exit`. Accepted residual (manual command): a background agent whose in-memory projection was lost — the post-restart STATE-3 fail-closed reconcile, or a heartbeat-TTL expiry (`BG_BACKGROUND_TTL_SECONDS` 2 h for launched `is_background` keys — the typing-unification T2 split; foreground-presumed keys keep the 30-min `BG_AGENT_TTL_SECONDS`) — projects idle, so `/update` may restart its parent mid-agent. Per-window failures are ISOLATED (a raise in one restart is bucketed as an error and the loop continues; a `reassociate` raise after a successful relaunch returns `ERROR`, never propagates). `config.claude_command` may carry flags, so the update binary is `shlex.split(...)[0]` + `["update"]` (never `shell=True`, bounded by a 120s `wait_for` + kill-on-timeout); a non-zero/timed-out update is non-fatal (the restart adopts on-disk). The relaunch `claude_command` is THREADED from `run_update` through `restart_claude_in_window` (not read from the global config there). NOTE: `CLAUDE_COMMAND` must exec the claude binary directly (or via an exec-ing wrapper) — a non-exec shell wrapper makes `pane_current_command` report the wrapper SHELL while Claude is alive, defeating the shell-detection gate in the dangerous direction (the relaunch keystroke would be typed into a live TUI). Pull-only; no route_runtime mutation; no observer.
- **Every inbound payload is GATED at ONE choke point (GH #50 PR-1)** — `SessionManager.deliver_to_window` (and its `(ok, msg)` wrapper `send_to_window`) is the SINGLE seam every user payload crosses: text, voice transcription, photo/document captions, attachment-only bundles, forwarded slash commands, `/effort`, the `aql:` late answer, and the pending-bind replay. Pre-GH#50 `text_handler` DETECTED a live surface and sent anyway (`inbound_telegram.py:1280-1296`), the media/voice handlers had NO check at all, and the aggregator flushes from a background debounce task so any offer-time check is TOCTOU. **The failure modes:** M1 — the appended Enter COMMITS option 1 on every blocking surface (rig-verified: ExitPlanMode ⇒ the plan is APPROVED, option 1 being `Yes, and bypass permissions`; folder-trust ⇒ trust GRANTED + persisted to `~/.claude.json`; `Switch model?` ⇒ model switched + saved as default). M2 — a bare digit is a live HOTKEY on a single-select-SHAPED surface (commits with NO Enter; CLAUDE.md's v2.1.168 "a digit only MOVES the cursor" is DEAD on 2.1.207 — AUQ MULTI-select digits still TOGGLE, so the `aqt:` lane is rig-CLEARED). M3 — a bare-shell pane EXECUTES the payload (`/esc` on folder-trust EXITS Claude, and `/esc` bypasses `send_to_window`, so only `/update` failures used to quarantine). M4 — the bot is BLIND to `Switch model?` (footer-less ⇒ `parse_generic_decision` returns None), which is exactly why the gate must not be "no known prompt matched". **The transaction** (inside the existing `window_send_lock`, every step fail-closed): (0) `delivery.lone_hotkey_line` — refuse if ANY LINE of ANY literal segment the writer will emit is an ASCII `[0-9]` fullmatch (never Python `\d`); this is SEGMENT-aware because the `!` writer emits `"!"` and the remainder as SEPARATE writes, so `!1` emits `"1"` as its own write (rig C7: CONFIRMED FIRES), and PER-LINE because a bare-digit LINE inside a multi-line single `send-keys -l` ALSO fires (rig §5 finding 3). No capture, never written — even on an idle pane. (1) a bounded, cancellation-safe capture (`capture_pane_cancellation_safe` under `asyncio.wait_for`; ONLY `asyncio.TimeoutError` classifies — a genuine caller/shutdown cancellation PROPAGATES) plus an overall transaction budget checked at the phase boundaries (never a `wait_for` around the WRITE — cancelling mid-write would leave a half-typed payload). (2) `pane_command_is_claude` on EVERY send, not just quarantined windows (closes M3; a quarantined window keeps its EXACT `QUARANTINE_SEND_REFUSED_MSG` contract string). (3) `terminal_parser.pane_input_box_present` — the POSITIVE proof; an INDETERMINATE frame (capture failure / no rule-pair / no prompt row / no ready chrome) retries up to `GATE_CAPTURE_RETRIES`, a POSITIVE hazard (a live prompt, a picker option row under the cursor, the tasks mode, a completion overlay) refuses on the FIRST capture. (4) the write with the Enter WITHHELD — a mode-aware writer reproducing the `!` bash-mode two-step explicitly, because `send_keys` performs its own two-step ONLY when `literal and enter` are BOTH true (`tmux_manager.py:782`); **EVERY post-write-attempt failure classifies WRITTEN** (r2 F5 — a `send_keys` False does not prove zero bytes landed). (5) the RE-VERIFY (`session._reverify_input_box`) — the BOUNDED `pane_command_is_claude` probe FIRST, then the pane capture LAST (r2 F4: the old order awaited an UNBOUNDED `pane_current_command` AFTER the capture, so a stalled probe let a STALE input-box frame authorize the Enter into a freshly-drawn prompt), payload-aware via `expected_draft` so an ordinary `1. buy milk` is not read as a picker cursor (r2 F1) and the `/`-overlay exemption requires the row to BE our exact bare `/command` (r2 F6) — the ONE race window this genuinely closes. It carries the SAME bounded INDETERMINATE retry as the pre-write gate (`GATE_CAPTURE_RETRIES` × `GATE_RETRY_DELAY_S`, the r2-F4 probe→capture order RE-ESTABLISHED on every attempt so a retry never authorizes on a stale liveness proof): it originally had NO retry and refused on the FIRST non-None reason, so one mid-redraw frame false-refused AND stranded the draft AND braked the topic — the most expensive failure in the transaction (the pre-write gate merely declines; this one leaves state behind). A POSITIVE hazard (`prompt_present` / `prompt_row_is_option` / `tasks_mode` / `completion_overlay` / `not_claude`) STILL refuses on exactly ONE capture with zero further keystrokes — the safety property the retry must never weaken; from here on every failure is `draft_written` (text in the box, Enter withheld, NEUTRAL copy, and NO automatic cleanup: Esc/Ctrl-U have surface-specific semantics and **Esc on folder-trust KILLS Claude**). (6) the pre-commit user-turn stamp (§1.5, below). (7) Enter — a FAILED Enter is `commit_unknown`, never "withheld" (r2 F3), and it KEEPS its stamp (the true invariant is "no PROVABLY-NOT-COMMITTED refusal is stamped"). **The stranded-draft brake (r2 F2):** a `draft_written`/`commit_unknown` payload sits UNSENT in the input box, and a live box with a pre-existing draft is legitimately deliverable — so the NEXT send appended to it and its Enter committed BOTH (including the text the user was told was not delivered). A per-window in-memory registry REFUSES further payloads (`stranded_draft`) until `terminal_parser.pane_input_row_empty` proves the box is clear; an indeterminate frame keeps it; a restart wipes it (disclosed). **The registry lives in `tmux_manager`** (`mark_window_stranded_draft` / `window_has_stranded_draft` / `clear_window_stranded_draft`, beside the post-/exit quarantine it mirrors; `session.py` keeps the four names as the delivery-path vocabulary + the test seam) because the brake is a property of the PANE'S CONTENTS, not of a topic binding — **BINDING-LEVEL TEARDOWN MUST NOT CLEAR IT (peer-review P1):** round 1 dropped it at `cleanup.clear_topic_state` + the four `inbound_telegram` stale-window unbinds, which re-opened the very commit chain the brake breaks — `/unbind` DELIBERATELY leaves the window ALIVE, and those seams hold no `window_send_lock`, so a send B already BLOCKED on that lock then acquired it, found a valid input box still holding delivery A's draft, appended its own payload and pressed Enter, committing BOTH. The only other release proof is WINDOW DEATH: a CONFIRMED `kill_window` (gated on the `True` return — a FAILED kill can leave the window alive with the draft intact) or `create_window` minting a brand-new window under that id (tmux ids RESET to `@0` on a tmux-SERVER restart, which a launchd-kept bot outlives). Topic close / `/kill` DO kill the window, so the brake still drops there — at the kill, under the right proof. A window that dies WITHOUT a `kill_window` leaks an inert entry (`_deliver_locked` refuses `window_gone` before it consults the brake; the empty-box self-heal reclaims any reused id). **The brake is armed through ONE seam, and a CANCELLATION after a write arms it too (peer-review P1):** arming only from the RETURNED `DeliveryResult` left the hazard reachable through the cancellation door — a `CancelledError` (topic teardown cancels per-topic tasks; shutdown cancels in-flight work) or any raise during the settle / re-verify / stamp / ENTER await propagates with NO result, so the brake stayed UNARMED and the next send appended to the leftover draft and committed both. The arming condition is a WRITE was ATTEMPTED (a `_WriteAttempt` flag set immediately BEFORE the first literal `send_keys` — never after, since a cancelled write can still have landed; the SAME information `DRAFT_WRITTEN` already uses, so no new imprecision), the brake goes up INSIDE the send lock (a queued send waiting on `window_send_lock` can't slip in first), and the exception RE-RAISES — `CancelledError` always propagates, never swallowed into a `DeliveryResult`. A raise BEFORE any write does NOT arm it (the hard non-regression: a HUMAN's pre-existing draft + an unrelated tmux error must still deliver). Callers also STOP on the first refusal — `aggregator_replay_payload`'s split loop and the four forced-flush callers (`bot.forward_command_handler`, `effort`, `late_answer`) abort their own subsequent send, and they OWN the single user-facing refusal message (`report_refusal=False`; the fire-and-forget flushes keep reporting inside the aggregator — one refusal, exactly one ❌, never two). **And a RAISED delivery is a refusal too (peer-review P2):** `_send_bundle`'s `except Exception` arm built its result and RETURNED immediately, jumping over the reporting block — and the fire-and-forget flushes never await that result, so the popped payload vanished with only a log line (the exact OPPOSITE failure of the double-report the `report_refusal` fold fixed; it matters doubly now, since a raise PAST a write attempt also arms the brake). The arm now FALLS THROUGH to the one reporting seam. THE INVARIANT: **every refusal — returned OR raised — reaches the user exactly once, on every flush path.** `CancelledError` is a `BaseException`: never caught, never swallowed into a `DeliveryResult`, never posted as an ordinary refusal. **Flag-independence by construction:** the gate never consults `_active_ui_patterns`, so `CC_TELEGRAM_PERMISSION_PROMPTS` / `CC_TELEGRAM_DECISION_CARDS` cannot reopen the hole. The recognizer checks (`is_interactive_ui` / `parse_unknown_blocking_prompt` / the recognizer-free `pane_blocking_prompt_shape` bottom-cursor probe) are a LABELLING aid ONLY, and `_input_box_reason`'s ORDERING enforces it (r1 P1): the positive proof runs FIRST and returns immediately when it passes; the recognizers run only on an already-FAILED INDETERMINATE reason, to upgrade it to the actionable `prompt_present` copy. They may never pre-empt the proof — they buy no safety (the proof alone refuses every blocking surface across all 25 real 2.1.207 fixtures), and pre-empting FALSE-REFUSES a legitimate message whenever a resolved AUQ/EPM rendering is still on-screen above a live input box (those two patterns carry no strict validator, hence no `_only_chrome_below` guard). **Hard non-regressions (rig design-killers):** a BUSY pane (thinking / foreground tool / background shell / workflow-waiting) still DELIVERS — queueing while Claude works is a first-class flow — and a pre-existing / soft-wrapped / multi-line draft still delivers; the predicate is deliberately NOT `pane_looks_idle` (it asserts neither idleness nor input-row emptiness, and `clean_ghost_input_text` is NOT needed here — it only matters for emptiness). **Reporting (§1.4):** `_report_quarantine_refusal` is generalized to `_report_delivery_refusal` (it no longer hardcodes / equality-matches `QUARANTINE_SEND_REFUSED_MSG`), and the structured `DeliveryResult` is threaded through `aggregator_replay_payload` / `_flush_pending_route_payload` (both previously bare bools) so the pending-bind replay — which IS the fresh-session folder-trust case — surfaces the REAL reason. One INFO per refusal (reason + outcome ONLY — never pane text, never message content). Refused payloads are DROPPED with the notice, never auto-replayed. **Compat (§1.6):** a non-exec `CLAUDE_COMMAND` wrapper makes `pane_current_command` report the SHELL while Claude is alive ⇒ every message would refuse; `bot._warn_if_non_exec_claude_wrapper` detects the shape at startup and logs a loud WARNING. **UNGATED by design:** `/esc`, the bash quick-keys, and the AUQ/Decision dispatchers key into a LIVE surface on purpose (they re-validate the pane form themselves). **Disclosed residuals (bounded, NOT closed):** the gate→write window (a prompt appearing there can still take a keystroke — mitigated empirically, a multi-char payload written in ONE `send-keys -l` is consumed paste-shaped and is inert, plus the step-0 hotkey refusal; a pty-chunking split of `"12"` remains a NON-proof case), and the final-capture→Enter window (one tmux round-trip — no terminal protocol can make it atomic; the IDENTICAL residual the shipped `_dispatch_pick` / `_dispatch_decision` already accept and disclose). At the PRE-write gate a HUMAN's own draft whose first visual row reads like a picker option (`❯ 1. buy milk`) fails leg 2 and is refused — fail-closed, and rare (the bot's OWN numbered payload is unaffected, r2 F1). A bot RESTART wipes the stranded-draft brake, and a window that dies without a `kill_window` leaks an inert entry. A stranded draft can only be cleared by the USER (no auto-Esc) — `/unbind` no longer releases the brake, since the pane (not the binding) owns the draft; the always-available exits are clearing the box in the terminal (`Esc`/`Ctrl+U`, or `/esc`) or `/kill`, and every refusal names them. PR-2 (the free-text lane that makes a card actually answerable in prose) ships separately. Pull-only; no observer (c313657 forbidden).

**Paste-collapse drift (CC 2.1.207, fixture-pinned — the PR-1 regression, 2026-07-11):** a payload written in ONE `tmux send-keys -l` past ~800 chars / ~13 lines is consumed by CC as a **PASTE**: it collapses the input row to `❯\xa0[Pasted text #1 +12 lines]` AND **REPLACES THE STATUS BAR** with the single line `paste again to expand`. For ~2s none of leg 3's other ready markers is on the pane, so `pane_input_box_present` returned `no_ready_chrome` and the post-write RE-VERIFY (which fires at `TEXT_SETTLE_S` = 0.5s, squarely inside that window) concluded there was no input box — refusing EVERY long / multi-line message (a voice note carrying a reply-context quote; the owner's live report was 809 chars), stranding the draft and BRAKING the topic, though the box was right there holding the text with Enter ready to submit it. `paste again to expand` is therefore in `_INPUT_READY_CHROME_MARKERS` (it IS a ready box: cursor in it, Enter submits) — but deliberately NOT in `_READY_STATUS_MARKERS`, the IDLE alphabet `pane_looks_idle` / `classify_pane_idle_failure` consume: a collapsed paste holds an UNCOMMITTED draft, so `/update` must still defer (a restart would discard it) and `/cost` must still refuse. Widening leg 3 cannot let a blocking prompt through — a blocking prompt REPLACES the box, so it fails leg 1 (`no_input_box`) or leg 2 (`prompt_row_is_option`) regardless of leg 3's alphabet (MEASURED: the hint is adversarially appended below every blocking fixture and every one still refuses). The interactive-GATE rejection lane (`_only_chrome_below`) consumes NO marker set — it is a structural ALLOW-LIST — so the hint ALREADY rejects a quoted gate above a live paste-collapsed box, which is right: the hint proves the box is live. The two lanes were already independent; nothing needed splitting. Fixtures `inputbox_paste_collapsed_v2.1.207.txt` (the hint chrome) + `inputbox_paste_collapsed_reverted_v2.1.207.txt` (~2s later CC restores the mode line while the collapsed draft remains — the shape the owner's pane was left in). **NBSP (load-bearing, now explicit):** the empty row is `❯\xa0` and the collapsed row `❯\xa0[Pasted …]` — U+00A0, not ASCII. The code coped only INCIDENTALLY (`str.strip()` drops NBSP), and that decides whether the row reads EMPTY — the stranded-draft brake's ONLY release condition. `terminal_parser._normalize_input_row` folds CC's Unicode spaces at the SINGLE seam `_input_box_rows` (the one path every input-box-lane reader crosses); it deliberately does NOT touch the rule scan, the chrome region, or any other parser — a global fold would change unrelated matching (option labels, gate footers, prose).

**Rule-separator drift (CC 2.1.207, fixture-pinned):** a few seconds after a plan is approved CC pins the plan slug into the input box's TOP rule (`──────… add-ok-to-note ──`), permanently for the session (only `/clear` drops it). `terminal_parser._RE_RULE_SEPARATOR` matched pure dashes only, so `_input_box_rows` lost the bracket entirely — breaking `pane_input_box_present` (the gate would refuse EVERY message in that topic) AND, **PRE-EXISTING and shipped long before GH #50, `pane_looks_idle`: `/update` silently deferred and `/cost` refused in any topic where a plan had been approved.** The regex now tolerates a labelled rule; both predicates are pinned on the real post-resolution rig captures.
- **TUI-overlay commands: interceptor or blocklist (never raw-forward)** — a Claude Code slash command that opens a full-screen TUI overlay writes nothing to JSONL and matches no UI pattern, so forwarding it raw opens an invisible modal that freezes the topic. Two disposals: (1) `/cost` and `/usage` open the SAME overlay (fixture-verified identical on 2.1.206) and have bot-side INTERCEPTORS (`bot.cost_command` / `usage_command` → the shared `_run_usage_overlay` scaffold), whose whole transaction runs under the window send lock with an **idle PREFLIGHT + conditional Esc** contract (round-1 converged P1): (a) capture the pane (WITH ANSI) and require POSITIVE idle evidence — `terminal_parser.pane_looks_idle(pane, allow_background_shells=True)` (the /update precedent, MINUS its restart-specific leg 5 — see the background-shells carve-out below) AND no live `extract_interactive_content` surface — before sending ANYTHING; a busy generation / live picker refuses with ZERO keystrokes and a helpful in-topic reply (typing "/cost" + Enter into a live AUQ picker would COMMIT the highlighted option). **Background-shells carve-out (2026-07-11, the ~100%-refusal bug):** `pane_looks_idle`'s leg 5 (`parse_background_jobs(pane) >= 1` → not idle) is `/update`-SPECIFIC — it exists because `/update` sends `/exit` and RESTARTS the session, which would silently kill the user's backgrounded shells. The overlay transaction restarts NOTHING (it types a slash command into an idle input box, captures the modal, presses Escape), so a live `· N shell` token is not a hazard for it — but `/cost` inherited the guard, and an owner who runs background agents constantly always has the token on the status bar, so `/cost` refused essentially every time (`reason=background_shells`). Both `pane_looks_idle` and `classify_pane_idle_failure` now take a keyword-only `allow_background_shells` (default False ⇒ `/update`'s behavior is BYTE-IDENTICAL, and its guard is untouched); this lane passes `True` to BOTH (the module-level `bot._USAGE_ALLOW_BG_SHELLS`) so the AUTHORITY and its LABELLER stay in lockstep (the agreement test is parametrized over both values). The carve-out is leg 5 ONLY: an active generation, a live interactive surface / picker, a typed draft, missing ready chrome, and an empty/failed capture ALL still refuse. Consequently `background_shells` is UNREACHABLE for this lane and is deliberately NOT a key of `_USAGE_FALLBACK_ACTION` / `USAGE_FALLBACK_REASONS` (the exhaustiveness test ties the map to `terminal_parser.pane_idle_failure_reasons(allow_background_shells=True)`, so it stays a real guarantee instead of carrying dead — and now untrue — "the safety gate defers this until they finish" copy). The ANSI capture is pre-cleaned by `terminal_parser.clean_ghost_input_text` — CC 2.1.206 renders a contextual GHOST suggestion in the empty input row styled ENTIRELY DIM (SGR-2), which a plain capture reads as a typed draft (`input_not_empty` false-refusal); the pre-clean blanks a fully-dim ghost (bare `❯` = empty) but leaves a real draft / any dim+normal MIX untouched (fail-closed, SGR-2 discriminator, fixture-pinned on 2.1.206 — a documented TUI-drift surface). The pre-clean is applied by the CALLER — `pane_looks_idle` / `classify_pane_idle_failure` bodies are otherwise untouched by it; (b) send → settle → `capture_pane` → `terminal_parser.parse_usage_output` (anchored on the ORDERED whole-token `Settings Status Config Usage Stats` tab bar corroborated by structural overlay evidence — the modal's top rule / `Esc to cancel` footer / `Session` sub-header (`_usage_overlay_anchor`; unordered/concatenated/prose-embedded probes never match — round-1 converged P3) → fail-open raw-pane fallback on body drift); (c) Esc is sent ONLY when the post-settle capture shows the overlay chrome (`terminal_parser.usage_overlay_present`) — if the overlay never opened (or the capture failed), the pane is left UNTOUCHED (a blind Escape into an active generation is the /esc hazard) and the reply is honest ("didn't open; check the window / /esc dismisses a late one"). Both are bot-owned `CommandHandler`s registered BEFORE the catch-all forwarder. **Busy-path snapshot fallback + observability (2026-07-10, plan v5):** run-state idle does NOT imply "safe to inject" — a typed-but-unsent DRAFT in the input row is correctly refused (injecting would corrupt it), so the refusal must never be a dead end. (a) **Reason-classified INFO log** at receipt + EVERY exit (`lock_busy` | `capture_failed` (a None tmux capture, distinct from the empty/mid-redraw `chrome_indeterminate` class — review r1 fold) | `capture_timeout` | the failing `pane_looks_idle` leg name from the pure replay-only `terminal_parser.classify_pane_idle_failure(text) -> str | None` — the helper NAMES the first failing leg but NEVER authorizes (review r1 P1: `pane_looks_idle` alone decides whether a keystroke may be injected; the classifier runs only AFTER the authority said not-idle, so classifier drift stays a labeling bug, never a wrong-keystroke risk); `pane_looks_idle`'s body is byte-untouched, an agreement test pins the helper's None/non-None never disagrees with the authority's True/False, and `terminal_parser.PANE_IDLE_FAILURE_REASONS` is the canonical leg-name set the copy exhaustiveness test ties to | `interactive` | `chrome_indeterminate` | the overlay lifecycle `overlay_present`/`esc_sent`/`parse ok|raw_fallback`); leg names + outcomes only, never pane text. (b) **Pane-free "cost snapshot"** on EVERY non-overlay exit — the refusals AND the post-preflight failure exits (send failure / post-send capture None or timeout / overlay-never-appeared / dismiss failure keep their honest safety text VERBATIM and APPEND the snapshot): context % from `route_runtime.snapshot(route).context_usage` + the cached last successful overlay (`handlers/usage_cache.py`, keyed ROUTE + current session identity — the session id is RE-READ at each peek AND each record, never sampled once pre-transaction (review r1 P2: rotation doesn't take the window send lock, so a mid-transaction rotation must not read the previous session's entry or record under a stale identity); 30-min read TTL, written only by the SUCCESS path; rendered "as of HH:MM, N min ago") + a REASON-SPECIFIC action line (exhaustive over the classifier reasons, pinned by a STRICT key-set-equality test on the copy map + a classifier-reason-set tie; the input-row line is TRUTHFUL-CONDITIONAL — "if you have an unsent draft there, submit or clear it" — since tmux capture can't distinguish a draft from placeholder chrome). The no-data shape (post-`/clear`, empty cache) still renders a card ("no bridge-side metrics cached yet"), never a bare refusal. `usage_cache` teardown mirrors the `pane_signals` route-scoped seams (`mark_session_reset` /clear + monitor rotation, the `inbound_telegram` stale-window unbinds, `cleanup.clear_topic_state`, `reset_for_tests`) — each seam pinned by a test driving the REAL seam function. (c) **Preflight capture retry + deadline:** INDETERMINATE frames only (capture failure/empty, missing chrome) retry up to 2 extra times ~300ms apart; POSITIVE hazards (active status / interactive UI / non-empty input row / bg-shells token) refuse IMMEDIATELY with exactly ONE capture; ONE `asyncio.wait_for(PREFLIGHT_DEADLINE_S=2.5)` bounds the whole preflight and the post-send capture gets its own `POST_SEND_CAPTURE_DEADLINE_S=2.5` (`capture_pane` has no subprocess timeout). ONLY `asyncio.TimeoutError` classifies (`capture_timeout` / `post_send_capture_timeout`) — a genuine caller/shutdown cancellation PROPAGATES, never swallowed into a fallback reply (review r1 P2). Both captures go through the new cancellation-safe `tmux_manager.capture_pane_cancellation_safe` (on `CancelledError`: best-effort `proc.kill()` + a SHIELDED `await proc.wait()` reap that survives a repeated cancellation — a second CancelledError can't leave the killed proc unreaped — then the ORIGINAL cancellation re-raises; a repeated /cost against a hung tmux never accumulates zombies; default `capture_pane` stays byte-identical). A post-send capture timeout sends NO blind Escape (the conditional-Esc contract), replies with the overlay-uncertain safety warning + snapshot, releases the lock. (2) `forward_command_handler` carries a conservative module-level `_TUI_OVERLAY_BLOCKLIST` frozenset (`memory`, `help` — verified interceptor-less panels), matched CASEFOLDED (CC's command lookup is case-insensitive — `/Memory` reopens the same panel, round-1 codex P2), that gets a helpful "can't render over Telegram — blocked … use /screenshot" reply and is NOT forwarded; everything else forwards exactly as before.
- **Topic-centric** — Each Telegram topic binds to one tmux window. No centralized session list; topics *are* the session list.
- **Window ID-centric** — All internal state keyed by tmux window ID (e.g. `@0`, `@12`), not window names. Window IDs are guaranteed unique within a tmux server session. Window names are kept as display names via `window_display_names` map. Same directory can have multiple windows.
- **Machine-surface window geometry (Wave B)** — bot windows are `160x50` by default (`CC_TELEGRAM_WINDOW_GEOMETRY`, `config.window_width`/`window_height`): terminals are a MACHINE surface (nobody attaches), so geometry serves the parser — 50 rows keep a tall AUQ picker fully on-screen (real `❯` from frame 1), 160 cols shrink the `N.Label` overflow class. ONE mechanism, `TmuxManager._cmd_resize_window` (per-window `resize-window -x -y`, stderr-checked, flips `window-size=manual`; never session-level `default-size`/`aggressive-resize`/`window-size` options), at TWO callsites: `create_window` resizes BEFORE the claude launch (a failure logs WARNING and still launches), and `bot._reconcile_window_geometry` resizes every listed window once in `post_init` after `resolve_stale_ids` (idempotent, per-window failures non-fatal). Dispatch logic untouched.
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **PreToolUse(AskUserQuestion) side files** — the `PreToolUse` hook (matcher `AskUserQuestion`) captures the structured `tool_input` to `auq_pending/<session_id>.json` before Claude renders the picker. The bot reads the side file at picker render time so each option's full description is visible in the Telegram context message immediately, before terminal completion. Side files are mode 0600 under a 0700 directory; multi-select `aqt:` toggles keep them alive, and cleanup happens when the AUQ `tool_result` lifecycle calls `forget_ask_tool_input`, when the session is replaced, or via startup GC. The AUQ side file is ALSO the free-text lane's occurrence anchor (GH #50 PR-2 — `free_text.SurfaceIdentity.anchor`), so without this hook a plain message at a card is REFUSED rather than delivered as the answer. Bot logs a one-time warning if `PreToolUse` is missing from `~/.claude/settings.json`; `cc-telegram hook --install` reinstalls all three managed hooks (SessionStart / PreToolUse / Notification), and `cc-telegram doctor` reports each.
- **Interactive approval-gate detection (Permission + Workflow — PR-1, display-only, flag-gated)** — two `UIPattern`s in `terminal_parser.UI_PATTERNS`, ordered **LAST** (after every AUQ/EPM/Settings/RestoreCheckpoint pattern — first-match-wins protects AUQ/EPM and vice-versa): `Permission` (tool-permission prompts: WebFetch/Bash/Edit/Write/…) and `Workflow` (the dynamic-workflow-launch approval). Each is disambiguated on its TOP anchor because the `Esc to cancel` / `Tab to amend` / `ctrl[+-]g` footer family overlaps across Permission/Workflow/EPM: Permission top = `Do you want to (allow|proceed|make|create|run|…)` (the REQUIRED question anchor) with `Claude wants to ` as OPTIONAL preamble context ONLY (peer-review P2 — `Claude wants to ` alone is never sufficient; the strict parser requires the question line); Workflow top = `Run a dynamic workflow?` / `This dynamic workflow will` / `Dynamic workflows can use`. Permission BOTTOM accepts EITHER an inline `(esc)`-tailed numbered option (WebFetch — no footer) OR an `Esc to cancel` footer (Bash/Write); Workflow BOTTOM anchors on the `Esc to cancel` footer line ONLY (the bare `Tab to amend` alt was DROPPED — peer-review P3: the real one-line footer leads with `Esc to cancel`, so the bare alt never matched and only widened the surface; `ctrl+g to edit script` stays excluded — it renders on its own line BELOW that footer and would trip `_try_extract`'s cross-footer pre-top-found bail). **Strict post-validation gate (S-8 fail-closed, peer-review P1):** the two gate `UIPattern`s carry a `validator` hook (`parse_permission_prompt` / `parse_workflow_approval`, wired in post-parser-definition via `dataclasses.replace`); `extract_interactive_content` runs the validator over the FULL pane after a loose top/bottom match and only returns the gate when it strictly parses (else CONTINUES the pattern loop) — the loose anchors alone lit a card on assistant prose QUOTING a gate. The returned content is the strict form's `pane_excerpt` (the trusted region). AUQ/EPM/Settings/RestoreCheckpoint have NO validator → byte-identical. **Bottom-terminal requirement (S-8; round-2 Codex P1 tightening):** each strict parser also requires that below the footer there is an ALLOW-LIST of ONLY the gate's own footer chrome — blank lines, BARE box-drawing separators, and the gate's own `ctrl+<x>` footer-continuation hints (the Workflow `ctrl+g to edit script` line, `ctrl+e to explain`, etc.) — via `_only_chrome_below`. A live gate is the active bottom prompt that REPLACES the input box / status bar, so the ready-for-input chrome that only renders when the gate is NOT live — the `❯` input box (the option cursor `❯ 1.` is ABOVE the footer, so any `❯` below it is the input box), the `? for shortcuts` / `← for agents` / `↓ to manage` / `esc to interrupt` status bar, the `· N shell(s)` background-jobs line, the `◐ … /effort` indicator, the model/context status bar — and any trailing assistant prose all REJECT (a complete-but-QUOTED block sitting in scrollback followed by the pane's normal input box / status bar is rejected, closing the round-1 false positive that allowed input-box/status chrome below the footer). **Empirically resolved** (round-2, `permission_webfetch_bgshells_v2.1.190.txt`, captured WITH 2 background shells running): a live blocking gate has NO `· N shell` / status / input-box line below its footer — the `· 2 shells` line is in the scrollback ABOVE — so Hermes's "a live gate with `· N shell` below its footer would be false-negatived" worry is REFUTED by data, and the check is deliberately NOT loosened for it (Codex was correct; the reject is safe). The strict-or-None parsers emit a single-question `AskUserQuestionForm` (`select_mode="single"`, `is_review_screen=False`) from the BOTTOM-MOST contiguous `❯ N. <label>` block above the footer (`_gate_options_above` extends across a numbered line ONLY while it stays contiguous downward, so a Workflow PHASE list `1. Sweep / 2. Verify / 3. Dossier` directly above the option block is NOT absorbed — peer-review P2), carrying the FULL label minus only a deterministically-stripped trailing `(esc)` (S-6: "Yes" vs "Yes, and don't ask again …" must stay distinct); the Workflow parser additionally validates the option SHAPE (`_is_workflow_option_shape`: option 1 == `Yes, run it` + a `View raw script` option present) so a phase block can't form a bogus gate, and stashes phases + token-cost warning in `_meta["workflow_body"]` for the card. **Deferred residual (now NARROW; PR-2):** after the round-2 tightening, the only residual is a fully-quoted gate that is the LITERAL last semantic content in the pane with NO ready-for-input chrome (no input box / status bar) below it — rare; it requires the pane to be captured with the quoted gate at the very bottom AND Claude not showing its input box (capture landed between frames). Cosmetic-only in display-only PR-1 (no dispatch, no auto-approval); the definitive close belongs in PR-2 (where dispatch makes it matter): gate render/promotion on `route_runtime.snapshot(route).notification_pending` (a genuine gate fires the Notification hook; quoted prose does not), deliberately NOT coupled in PR-1 — PR-1 stays pane-only per the plan (timing risk of delaying legit cards), and the empirically-tightened chrome check closes the realistic case. **Detector kill-switch (P2-3):** a LOCAL `os.getenv("CC_TELEGRAM_PERMISSION_PROMPTS")` parser flag (`_PERMISSION_PROMPTS_ENABLED` + `set_permission_prompts_enabled` + `reset_for_tests`) — NEVER a `config` import (the parser stays a pure stdlib leaf; `config` raises without a bot token; an ISOLATED subprocess test pins the no-token import). The detector defaults ON since 2026-07-11. When OFF, `_active_ui_patterns()` filters BOTH gate patterns out of the detector → a flag-OFF deploy adds NO detection, no card, no `WAITING_ON_USER` promotion (S-9). `config.py` owns the canonical env declaration for docs/README; the parser reads the same var. **§1.1 decision (verified against `permission_write_long_visible_v2.1.190.txt` + the WebFetch/Bash/Write captures):** the planned new `_PICKER_ANCHOR_MARKERS` permission anchor is UNNECESSARY and was NOT added — Claude Code redraws gates IN PLACE (the `Do you want to…?` question stays adjacent to the options at the visible bottom; only the file/content preview scrolls off above), so `visible_pane_liveness(visible)` already returns `"present"` via the `is_interactive_ui` leg (the status poller's `capture_pane(scrollback_lines=0)` visible capture always contains the full gate). **Render (`interactive_ui`):** a thin `content.name in ("Permission","Workflow")` branch posts a DISPLAY-ONLY card — the existing window-keyed manual ↑/↓/⏎/Esc nav keyboard (NO option-pick buttons, S-1) + an honest notice that the controls send raw, un-cursor-verified live-pane keystrokes (P2-1) — and SKIPS `_maybe_post_live_prose` (§6: the AUQ/EPM-only dedup would double a gate prose post). The pane-confirmed `mark_interactive_pending` promotion + WAITING + typing-off machinery is UI-name-agnostic (keys on `ui_content`), so it works unchanged. **Coexistence (§3):** `status_polling._reconcile_decision_card` now DISMISSES the generic "🔔 Claude needs a decision" Notification-hook card (kind-scoped, idempotent) once a live interactive surface owns the topic, so the actionable gate card supersedes the dead-end nudge; the `notification_pending` derivation is untouched. **NOT in PR-1:** no pick buttons, no dispatch, no validator change, no `gate_variant` field, no obscured-pane liveness gate (all PR-2). Pull-only; no observer (c313657 forbidden).
- **Generic decision-prompt detection (Stage B1, display-only, flag-gated)** — a THIRD `UIPattern` in `terminal_parser.UI_PATTERNS`, `Decision`, ordered **LAST** (after Permission/Workflow, so first-match-wins never lets it steal any NAMED pane — AUQ/EPM/Settings/RestoreCheckpoint/Permission/Workflow keep their exact slots). It surfaces GENERIC titled numbered-option confirmation prompts that no named pattern covers (the "Switch model?" confirmation, the folder-trust prompt, and peers — both verified UNCOVERED: their footer is `Enter to confirm · Esc to cancel`, not AUQ's `Enter to select`). Loose anchors: a numbered-option TOP (`^\s*[❯›▶*)>]?\s*\d+\.\s+\S`) + a confirmation footer bottom that MUST carry `Enter to (confirm|continue)` (`_RE_DECISION_FOOTER`, DELIBERATELY excluding `Enter to select`, AUQ pattern 3's footer). **Requiring the affirmative-commit `Enter to (confirm|continue)` component (Codex P2 fold, NOT a bare `Esc to cancel|exit`) STRUCTURALLY closes the verb-drift veto bypass:** a permission gate whose verb is outside `parse_permission_prompt`'s whitelist (e.g. `Do you want to open …?`) has an `Esc to cancel · Tab to amend` footer with no `Enter to confirm` line, so it never matches Decision's footer at all — independent of the veto. **Strict-or-None validator `parse_generic_decision`** (wired via the same `dataclasses.replace` post-definition mechanism as the gate validators): (1) bottom-most confirmation footer (with `Enter to (confirm|continue)`); (2) `_only_chrome_below` True (the shared live-bottom-prompt guard — a quoted prompt with a ready-for-input input box / status bar below it rejects); (3) `_gate_options_above` → ≥2 contiguous numbered options AND a resolved live `❯` cursor; (4) **the Permission/Workflow VETO (Hermes P2-4), KEPT as defense-in-depth** — it runs the STRICT `parse_permission_prompt` / `parse_workflow_approval` (never a loose regex) and returns None if EITHER parses, so a real permission/workflow gate is NEVER re-surfaced as a Decision even when `CC_TELEGRAM_PERMISSION_PROMPTS` filtered it out of the detector (the cross-flag re-exposure fix; after the footer narrowing it is near-unreachable belt-and-suspenders). `current_question_title` = the TOP meaningful line of the contiguous prompt block above the options (the heading, e.g. "Switch model?"); `pane_excerpt` extends UP through that whole block → footer (`_decision_prompt_block_top`, bounded by a ≥2-blank-line gap / a chrome-separator line / 10 lines) so the card body shows the heading + context + options (Hermes P3 fold). **Accepted narrow residual (Codex P1 / Hermes P2):** a fully-quoted decision block that is the LITERAL last pane content with NO ready-for-input chrome below it passes `_only_chrome_below` — the SAME class as the gate residual above; in a real running pane the input box / status bar are always below and reject it. Flag-ON by default since 2026-07-11 + display-only in B1; the tappable-dispatch upgrade is Stage B2 (below), gated by the SEPARATE `CC_TELEGRAM_DECISION_DISPATCH` flag. NOT closed with a heading/family allowlist — the detector stays GENERIC. **Detector kill-switch:** a SECOND LOCAL parser flag `CC_TELEGRAM_DECISION_CARDS` (`_DECISION_CARDS_ENABLED` + `set_decision_cards_enabled` + `decision_cards_enabled` + `reset_for_tests` resets BOTH flags), independent of the gate flag; `_active_ui_patterns()` filters `Decision` out when OFF → a flag-OFF deploy adds ZERO new detection, no card, no `WAITING_ON_USER` promotion. `config.py` owns the canonical `CC_TELEGRAM_DECISION_CARDS` declaration (docs/README) and `main._run_bot()` seeds the parser from it (the import-order-race dodge, mirroring the Permission seed). **Render:** `Decision` is folded into `interactive_ui._GATE_RENDER_NAMES`, so it rides the EXISTING display-only gate branch (title + options body + the manual ↑/↓/⏎/Esc nav keyboard + the honest un-verified-keystroke notice) and the AUQ/EPM-only `_maybe_post_live_prose` skip — NO pick buttons, NO dispatch (that's Stage B2). The pane-confirmed `mark_interactive_pending` promotion is UI-name-agnostic, so a live Decision prompt flips the route to "🔔 Waiting on you" unchanged; a negative pane never promotes (proven at the `status_polling` seam). Pull-only; no observer (c313657 forbidden).
- **Tappable Decision dispatch (Stage B2.3, flag-gated `CC_TELEGRAM_DECISION_DISPATCH`, default OFF)** — a PARALLEL, Decision-specific dispatch lane (`dcp:<route_hash>:<fp8>:<opt>:<token>`) that reuses the AUQ dispatch DISCIPLINE (per-window send lock + `_lock_busy` reject, monotonic arrow nav, settle→re-parse→verify, Enter as the ONLY commit key, fail-closed advance classification, `auq_ledger` idempotency) but NEVER the AUQ `resolve_auq_source`/`resolve_ask_form` machinery (a Decision pane returns None there — the P1-C dead-tap the lane avoids). **Render mint** (`interactive_ui._build_decision_pick_rows`, in the `content.name == "Decision"` gate branch): mints one-tap option buttons ONLY when the §7 flag is ON AND the strict `parse_generic_decision` form matches a known `decision_token.identify_family` (which also requires a non-None title — the §5a mint gate) AND `decision_token.lookup(family, w.pane_current_command)` licenses the family × the CACHED CC-version AND the geometry is a clean single-select numbered picker; else display-only, byte-identical to B1. The `fp8` derives from the body-inclusive `terminal_parser.decision_prompt_fingerprint` (a `decision:` DOMAIN PREFIX so the shared `auq_action_ledger.jsonl` key can NEVER collide with the AUQ lane's bare `_canonical_repr` fp8 — §8). **Dispatch transaction** (`callback_dispatcher/interactive._dispatch_decision` → `_dispatch_decision_pane_locked`, under `window_send_lock` with `_lock_busy` reject-if-held): extractor parity (`extract_interactive_content(pane).name == "Decision"` — a Settings/AUQ pane that merely decision-parses bails, the named `settings_warning_v2170.txt` decline) → `decision_prompt_fingerprint` identity + geometry/family gates → the **FRESH** `pane_current_command` version-license re-read (`pane_command_is_claude` + `lookup`, INSIDE the lock, immediately before the first key — a /update-swapped TUI inside the 1s list-cache TTL can never be arrow-keyed; the AUQ round-2 P1-1 fix) → nav→settle→verify with a MOTION proof (delta≠0: cursor moved to target AND ≠ pre-nav; delta==0: the WIGGLE — one arrow away then back, requiring the ❯ to move) → Enter → `_classify_decision_advance` — the confirm runs the FULL `extract_interactive_content` (review r1 P2-B: first-match-wins parity on the CONFIRM side; never the bare `parse_generic_decision`, whose weaker recognition would fp-compare a Settings/AUQ pane that merely decision-parses as a "different Decision" and wrongly confirm): extractor→Decision ⇒ fp compare (dispatched ONLY when the committed fingerprint is proven GONE; a live same-fp form is the zero-absence variant → `commit_unconfirmed`); extractor→another named UI or None ⇒ dispatched only when NO decision footer/marker remains (still-present footer = ambiguous → `commit_unconfirmed`). **Ledger discipline** (mirrors the AUQ v2.1.168 model): `accepted → dispatched` + `auq_ledger.release_key(key)` on the confirmed-gone proof; a **pre-commit bail** records `not_advanced` (Enter provably never sent → the callback FALLS THROUGH / re-renders fresh tokens); once Enter is sent an unconfirmed advance records `commit_unconfirmed` (refresh-only, UNRELEASED). A busy send lock at dispatch downgrades the already-written `accepted` to `not_advanced` (fall through, never a crash-ambiguous `accepted`). **§5b(b) dispatch-terminal teardown** (`interactive_ui.finalize_decision_dispatch`, NOT `clear_interactive_msg`): pops the PERSISTED interactive surface (a stale raw-nav tap then fails `has_interactive_surface` — restart-safe) + `decision_token.teardown_route`, fires the lifecycle hooks (the poller drops `_absent_streak` + `_last_published_ui_hash` → a fast byte-identical re-raise renders FRESH), then edits the card to the inert "✅ … sent" final state — and (review r1 P2-C, plan §3 normative) the finalize runs BEFORE the callback answer, so a crash/network window can never leave an acked callback with a non-terminal persisted surface. `decision_token.teardown_route` is ALSO wired (review r1 P2-A) at the `/clear` `mark_session_reset` seams (bot /clear branch + the monitor rotation sweep) and the `inbound_telegram` stale-window unbind `clear_route` sites, beside the pane_signals/route_runtime teardown calls — a /clear-rotated window keeps its id, so a same-fingerprint re-raised prompt within the 300s token TTL would otherwise validate a stale `dcp:` tap end-to-end. **§5b(c)/O-6 generation-suffixed nav** (closes the pre-existing window-keyed raw-nav replay hole): every GATE card render (Decision AND Permission/Workflow per O-6) rotates `decision_token`'s per-window nav generation and suffixes its ↑/↓/⏎/Esc callbacks `aq:*:<window>:g<gen>`; non-gate (AUQ/EPM/RestoreCheckpoint) renders CLEAR the generation and stay un-suffixed (byte-neutral). `assert_nav_dispatchable` validates: gen present must equal the window's current gen; gen absent + a live gate generation → refuse (a pre-B2 un-suffixed gate card); gen absent + NO gate generation is AMBIGUOUS, not automatically legacy (review r1 P1, BOTH engines — the registry is in-memory, so after EVERY restart/deploy it is empty and a pre-B2.3 gate card's raw un-suffixed `aq:enter:@N` would otherwise raw-dispatch into a live gate pane): the shape is discriminated on the LIVE pane, reusing guard 4's EXISTING visible capture (no new capture on the suffixed/gen-registered paths) — a gate-named `extract_interactive_content(visible)` refuses fail-closed before any key; an AUQ/EPM pane proceeds down the legacy path unchanged (byte-neutral). The generation is invalidated IN-LOCK at `dispatched` (the lock-release→teardown gap) and wiped on restart → a suffixed tap fails closed. **§8 restart:** in-memory tokens + nav generations die; the ledger-first gate answers a `dispatched` duplicate "already received"; NO durable `pick_intent`-style recovery (Decision re-mints from the live pane trivially). Poller (`status_polling` same-hash Decision branch) calls `decision_token.refresh_route_deadlines` (the D3-β analogue) so a long-open card's `dcp:` tokens never TTL-prune. **Top residual (disclosed):** the `decision_token._DECISION_DISPATCH_TABLE` allowlist is empirically per `(family × CC-version)` — every CC upgrade empties the effective allowlist → buttons revert to display-only until re-characterized (honest degradation, INFO logs at mint + tap; never a wrong keystroke). Pull-only; no observer (c313657 forbidden).
- **Live-safe side-file GC/reconcile (stateless-callback Wave 1 PR-B)** — two side-file-trust hardenings in `auq_source.py` + its startup wiring. (The wave's third piece — the read-TTL-free `resolve_auq_source_for_dispatch` dispatch source, added unit-tested-only for the never-built PR-C — was REMOVED 2026-07-02 when the stateless-callback campaign was retired; the `apply_ttl` keyword on `_read_live_pretool_record` survives, used by the PR-3 render resolver `resolve_auq_source_for_render` and the ctx recovery `recover_consistent_side_file_for_ctx`.) (1) `gc_stale(*, is_live_session=None)` mirrors `md_capture.gc_stale`: after the age test and before the re-stat TOCTOU guard, an INJECTED predicate called with the file STEM (= `<session_id>`) → True skip-keep / Exception conservative-skip, so a live AUQ whose tool_use is buffered (stale-mtime side file but still the card's liveness authority) is not reaped at startup; wired at `bot.py` to `lambda sid: monitor.state.get_session(sid) is not None`. (2) `_hydrate_ask_tool_input_cache`'s startup reconciler now unlinks the side file only on POSITIVE resolution proof — it peeks the side file's captured `tool_use_id` (`auq_source.peek_side_file_tool_use_id`, a thin public accessor over `_read_pretool_side_file`) and unlinks ONLY if a matching AUQ `tool_result` exists in the JSONL tail (`SessionMonitor._auq_tool_result_present`, sharing the new `_read_jsonl_tail` helper with `_find_latest_pending_auq`); a still-BUFFERED tool_use (no tool_result) or an empty captured id → PRESERVE (closes the live-AUQ-side-file-deleted-on-startup latent bug). Session-keyed discipline preserved (peek + unlink the SAME `current_map` session).
- **Render-only rescue resolver + render-identity loop kill (PR-3 PR-B)** — fixes a long-description AUQ in a BUSY topic rendering BROKEN + spamming duplicate "📋 details" cards every ~20s (the live pane mis-parses/churns while the PreToolUse side file holds the real question; PR-A fixed the parser mis-parse, PR-B fixes the render path + the loop). `auq_source.resolve_auq_source_for_render(window_id, pane_text, explicit) -> RenderAuqSource(decision, kind, payload, form, source_fingerprint, dispatch_trusted, reason)` is the RENDER-path resolver — DISTINCT from the strict `resolve_auq_source` that `pick_token.validate_and_consume` + `status_polling._remint_on_source_drift` still use UNCHANGED. It reads the side file READ-TTL-FREE then decides: `side_file_ok` (consistent with the pane AND within the 300s read-TTL → trusted; the `within_ttl` gate mirrors the TTL'd strict resolver validate re-resolves → mint/validate parity, so a long-open card flips cleanly to `bail` at the TTL boundary rather than stranding a trusted token validate rejects, and `_remint_on_source_drift` stays loop-safe), `bail` (the pane is itself a COMPLETE coherent picker — `pane_form_is_complete_picker` — disagreeing with the side file → a genuinely different/advanced live question → render the PANE, trusted; never serve the stale side file), `rescue` (unparseable/incomplete pane → render the side file DISPLAY-ONLY, `dispatch_trusted=False`, PURE `build_form_from_tool_input` form so the render identity can't leak pane churn), or the pre-existing `explicit_jsonl > jsonl_cache > pane` fallback when no side file. `dispatch_trusted` GATES token minting at the `_build_pick_button_rows` callsite (rescue → NO `pick_token`/`pick_intent` rows + `prune_for_route` + manual-nav notice); the ctx 📋 card is driven off the decision (side_file_ok/rescue post the side-file descriptions — rescue is the V1/V2 fix where the card was dropped because pane-consistency rejected on the busy pane; bail posts NO stale side-file card). **Loop kill:** both `status_polling` dedup hash sites (`_ui_render_hash`) hash the render IDENTITY for AskUserQuestion (`auq_source.peek_render_identity` = the decision + `render_signature` over the render/keyboard-determining form fields — using `current_question_title` ONLY, NEVER the scrollback-derived `pane_walkback_title`, which would churn the title-less bail/pane card every tick — internal-review regression catch; mirrors `_canonical_repr`), STABLE under scrollback churn yet re-rendering on every genuine transition; NEVER the cursor-blind pick-token `fingerprint()` (the renderer paints the cursor, so a cursor move must re-render); non-AUQ UIs keep the raw-content hash. MUST NOT mutate `_pretool_ask_records` (`resolve_record` stays the sole mutator). Disclosed residuals (all untrusted-display, never a wrong dispatch): the ≤1-poll-cycle 300s-boundary race (unchanged from item-1; PR-B cleans the >300s steady state); a `rescue` may render a STALE side-file question vs a different incomplete live pane (bounded — sibling/restart/hook-lag — and strictly better than the pre-PR-3 raw-blob render); a multi-Q `rescue` renders Q1 when the pane's tab header is unparseable (the 📋 card still enumerates all questions). Pull-only; no observer (c313657 forbidden).
- **MessageDisplay live-prose capture (Bug 2)** — assistant free-text prose written in the same turn as an `AskUserQuestion` / `ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at resolution, so during a live prompt the prose is not on the bridge and the Telegram user would choose blind. Claude Code's `MessageDisplay` hook fires with each streaming `delta` BEFORE the picker blocks; a tiny stdlib appender (`_md_display_appender.py`, never imports the package — `forceSyncExecution` latency budget) writes each `delta` to `msg_display/<session>.ndjson` keyed by `Path(transcript_path).stem` (resume-safe: under `--resume` the JSONL is the original session's file the bot tracks, not the new hook-reported id). The hook is scoped to bot-launched sessions via a bot-managed `md_hook_settings.json` passed as `claude --settings` (merges with the global hooks; never in `~/.claude/settings.json`). The bot accumulates the per-flush deltas by `MessageDisplay.message_id` (no JSONL counterpart, so grouping is bot-side) into completed prose, read on demand at picker-render (`md_capture.read_prose_records` — pull-only, no tailer/observer; c313657 stays forbidden). `md_capture.normalize_prose` (via `prose_norm_hash`) is the SINGLE normalization used for both the live `norm_hash` and the post-resolution JSONL dedup, so the two compare equal (mint/validate parity). The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin` marker (`BLOCK_ORIGIN_EXIT_PLAN`) through `ParsedEntry` / `TranscriptEvent` / `NewMessage` so dedup can group prose with its sibling interactive `tool_use` and exclude the synthetic ExitPlanMode plan text. **Live delivery (PR-C):** `interactive_ui.handle_interactive_ui` → `_maybe_post_live_prose` posts the freshest finalized capture (`select_fresh_prose`, the PR-1 additive-OR of the render-time TTL leg with an emission-anchor leg `[emitted_at - lookback, emitted_at + eps]` — `emitted_at` a stable picker-emission instant selected by modality: AUQ `auq_source.peek_side_file_written_at` / EPM `status_polling.peek_epm_surface_emitted_at`; recovers the dominant miss where the poller detected the picker tens of seconds after the prose finalized [measured 5.44s idle, ~20.7s loaded — the "~0.68s before the picker" premise was INVERTED], blowing the fixed TTL — + the Item-3/P2-1 turn-boundary filter) before the picker card, records a shown-live marker, and is idempotent via `was_shown_live` (consume-inclusive); a miss is a silent no-op (JSONL delivers post-resolution) logged with a miss-classification reason (PR-1 A6). **Turn-boundary filter (Item 3 / P2-1):** the per-session capture file holds a PRIOR turn's leftover prose until resolution-time teardown, so a still-within-TTL leftover could be posted above a picker whose own turn produced no prose. `select_fresh_prose(not_before=...)` adds a STRICT `final_at > not_before` gate where `not_before` is the wall-clock instant the bot DELIVERED the current user turn into tmux (`message_queue.set_route_user_turn_at`, requested by the `inbound_aggregator._send_bundle` + `bot.forward_command_handler` + `effort` + `aql:` delivery seams and FIRED inside the GH #50 gated transaction immediately before the Enter — the same `time.time()` clock as the appender's `captured_at`; a refused send is never stamped). `_maybe_post_live_prose` resolves the stamp INSIDE itself (`peek_route_user_turn_at`, not threaded through `handle_interactive_ui`'s 22 callers — auto-closes the on-pane + restart first-render holes); `not_before=None` disables the turn-boundary filter (the emission-anchor OR leg still applies when `emitted_at` is non-None; only `emitted_at=None` falls to TTL-only — the restart degradation). **Dedup (PR-D):** `session_monitor.filter_live_prose_duplicates` runs on the poll batch before dispatch — groups by `(session_id, message.id)`, matches a group's REAL-text aggregate `norm_hash` to an unconsumed marker, suppresses + consumes (consume-once, restart-safe); >1 group sharing one marker → suppress none. **Teardown:** `teardown_session` wired at `forget_ask_tool_input` (primary, AUQ+EPM), the `/clear`/deleted-window seams in `session_monitor` (OLD session id), and `clear_topic_state`; 1h startup GC backstop. **Startup-GC liveness gate (Item 3 / P2-2):** `gc_stale(is_live_session=...)` skips reaping a live session's capture file (the dedup markers live in the same file — reaping a live picker's file would double-post at resolution). The predicate is INJECTED at the `bot.py` callsite (`monitor.state.get_session(sid) is not None`, keyed by the ndjson stem = original session id, covering AUQ+EPM); a predicate raise → conservative SKIP; a re-stat before `unlink` is the TOCTOU guard. Pull-only throughout (c313657 forbidden).
- **AUQ findings recap (GH #48, R2 only)** — when normal live delivery posts nothing for an AskUserQuestion, `_maybe_post_auq_recap` may re-surface the freshest older finalized finding before the 📋 question card. The AUQ side file is read once atomically; non-empty `tool_use_id` is the surface identity, otherwise `(written_at, full canonical tool-input fingerprint)` is used. `md_capture.get_or_create_surface_floor` persists `{surface_id, render_at, floor_at}` as a `surface_floor` marker, freezing `floor_at` to the predecessor surface's render time; retries reuse it and only S+1 consumes S's render time. Recap provenance requires an in-memory `not_before`, `first_seen_at > max(not_before, floor_at)`, `final_at > not_before`, and the anchor-reject shape `final_at < emitted_at - lookback`. Missing side file logs `no_anchor`; restart (`not_before=None`) fails closed. Successful sends use `📌 Context (recap)` plus rendered-cost-bounded, independently complete expandable-quote chunks through `topic_send(plain=False)`, then append a `(norm_hash, emitted_at)` `recap_shown` marker. Send failure does not block the card; duplicates remain possible on ambiguous completion. Quiet suppresses recap. AUQ only: EPM, gates, `filter_live_prose_duplicates`, and the finalized marker lane are unchanged. Pull-only; no RouteRuntime field or new state file.
- **Interactive-surface teardown is PARENT-only (sidechain-gated)** — the two `bot.handle_new_message` seams that clear a live interactive card on the parent route — the explicit AUQ `tool_result` invalidation (`forget_ask_tool_input` + `auq_ledger.release_window`) and the generic *"any non-interactive message ⇒ interaction complete"* teardown (`if has_interactive_surface(user, thread): clear_interactive_msg(...); forget_ask_tool_input(wid)`) — are GATED on `msg.subagent_key is None`, mirroring the interactive-HANDLING branch and the sidechain-emit routing-bypass intent (`session_monitor.py:1583-1587`). A sidechain / background-agent block carries the PARENT's `session_id` + a non-None `subagent_key`, so it routes to the parent's route; without the gate a background Workflow/Agent narrating while the parent is BLOCKED on a live prompt tore the card down (`topic_delete` of the picker) and popped the by-window `_auq_context_posted` marker (`interactive_ui.py:443`), so the 1 Hz poller re-detected the still-live pane prompt and re-posted — the 2026-06-23 DiCopilot ~28× ctx-card duplication (+ an EPM `📋 Plan` re-post twin via `md_capture.teardown_session`). `has_interactive_surface` is route-keyed + UI-type-agnostic, so the one gate covers AUQ + ExitPlanMode + Permission. Day-one (v0.1.0) asymmetry — handling gated, teardown not — dormant until unconditional sidechain DISPLAY emission (`ef086f1`) + the Fix 5 Workflow shape. A GENUINE parent block (`subagent_key is None`) still tears the card down (the bypassPermissions auto-resolution case). Pull-only; no observer (c313657 forbidden).
- **Artifact delivery lane (📎 tap-to-download + `/file`)** — parent assistant PROSE mentioning a deliverable local file path (`report.md` / `chart.png` / `clip.mp4` / `build.tar.gz` / … per `artifacts.ARTIFACT_EXTS`, which covers every deliverable type — docs/images/audio/video/archive/office/data — but EXCLUDES source-code extensions so incidental `.py`/`.ts` paths in prose never mint a card) triggers a compact 📎 card with one `dlf:` button per file, posted on the route FIFO STRICTLY AFTER the block's content task (prose → card). A double-extension archive (`build.tar.gz`) extracts + resolves as the WHOLE token, never a truncated `.gz` tail (the right boundary rejects stopping mid-token). Mint + the `dlf:` executor emit INFO logs (minted rows' relative display names + root KINDS + row/overflow counts at mint — never absolute paths / root paths, never the deduped/overflow entries; tap / open / send outcome at the executor) so the download lane is reconstructable. Detection is `bot._maybe_offer_artifacts`, gated on the per-recipient `prefs.artifact_card` (quiet=off) and `subagent_key is None` (never sidechain / tool output / URLs). Every offer is FS-validated (`resolve_artifacts`: expanduser → cwd-join → `resolve()` [follows symlinks so an in-cwd symlink escaping the root fails] → `is_relative_to` a RESOLVED allowed root [cwd + `CC_TELEGRAM_ARTIFACT_ROOTS`; empty cwd ⇒ no root ⇒ fail-closed] → regular-file + `CC_TELEGRAM_ARTIFACT_MAX_MB`). A relative candidate that misses under a harness `.claude/worktrees/<name>` cwd RETRIES the join against the derived main-repo root (a worktree session's handoff written to `<main_repo>/temp/…`), pinned + displayed relative to whichever root matched; the cwd hit always wins, and a `../`-escape / symlink-escape rejects under BOTH roots. A general `git worktree add` elsewhere is NOT covered (no shared string shape). The upload closes TOCTOU: `open_validated_artifact` re-checks containment against the roots PINNED in the registry row at mint time (never a recomputed mutable cwd — codex r2 P2-1), `O_RDONLY|O_NOFOLLOW` opens, `fstat`s regular-file + size ON THE FD, and passes THAT open file object to `message_sender.send_document` (`(ok, reason)`; RetryAfter re-raised) — the pathname is never re-opened. The card body is PATHLESS (owner decision — the prose above names the file(s); a plain-text path gets TLD-auto-linkified by Telegram into a dead link). Tokens are single-FLIGHT (a re-tap re-uploads current content), in-memory (restart ⇒ graceful expired modal; the prose above + `/file` are the restart net), owner-gated, offer-deduped per `(route, path)` for 30 min. `/file <path>` (`bot.file_command`, registered before the catch-all forwarder) is the durable escape hatch — the RAW arg tail (paths with spaces), NOT ext-gated (any file type under the roots). Teardown: `artifacts.invalidate_topic` in `clear_topic_state` (the covering seam) + `invalidate_window` at the four `inbound_telegram` stale-window unbinds. `handlers/artifacts.py` is a config-free/telegram-free leaf (values injected). Pull-only; no observer (c313657 forbidden).
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via thread bindings (topic → window_id → session).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. The pre-2026-02-11 `window_name`-keyed `state.json`/`session_map.json` format is no longer migrated: any non-`@` legacy keys found on load are dropped with a one-shot per-map `logger.warning` (`window_states` / `thread_bindings` / `user_window_offsets` in `session.py`; `session_map` entries in `session_monitor._load_current_session_map`). The live SessionStart hook only ever emits `@N` keys.
- **RouteRuntime concurrency contract** — `route_runtime` is the sole run-state / context-usage / idle-clear authority, exposing a single per-route state machine via `ingest_transcript_event(route, event)`, `mark_*(route)`, and `snapshot(route)`. Per-route `asyncio.Lock` serialises mutations within a route; independent routes do not serialise. Reads come only from `snapshot(route)` — each mutation freezes a committed, frozen `RouteRuntimeSnapshot` and there is no push/observer channel. Pane snapshots (`mark_pane_idle` / `commit_pane_idle_clear`) are reconciliation events with lower authority than transcript lifecycle: they preserve `WAITING_ON_USER`, only clear `RUNNING` / `RUNNING_TOOL`. Pane signals may also **PROMOTE an active `RUNNING` route** (empty `open_tools`) to `WAITING_ON_USER` via `mark_interactive_pending` — fired by `status_polling` from a **pane-confirmed** live AUQ picker / ExitPlanMode plan-approval while Claude Code buffers the interactive `tool_use` in JSONL — retracted via `mark_interactive_cleared`. Strictly lower authority than the transcript (deriver checks `open_tools` first; the `tool_use` / known-`tool_result` / end-of-turn / user branches zero the `pane_interactive_pending` bit, plain-text/thinking and an unknown `tool_result` preserve it); never resurrects idle, seeds an unseen route, overrides `RUNNING_TOOL`, or clobbers a transcript-set `WAITING_ON_USER`. Cleared by the transcript reclaim, the poller's mode-ended liveness reconciliation (`interactive_window != window_id`) / in-mode tombstone, or route teardown — dropped wherever route_runtime state is cleared: `mark_session_reset` (`/clear`), the `inbound_telegram` stale-window unbinds (direct `clear_route`), and `clear_topic_state` → `route_runtime.clear_routes_for_topic(user, thread)` on topic-close / poller window-gone (route_runtime's OWN topic-teardown seam — NOT derived from `message_queue._route_queues`, so a queue-less route is torn down too). The digest header repaints on a run-state transition via the poller (`_maybe_repaint_digest_on_transition` → `message_queue.refresh_activity_digest_if_present`; pull-only, no observer). No `register_*_callback` fan-out — that pattern (which produced bug c313657) is precisely what `RouteRuntime` replaced. Topic-broken handling is the **reactive** path in `message_queue` (`_bad_topic_threads` / `_emergency_dm` / `_TOPIC_BROKEN_OUTCOMES` / `probe_topic_liveness`), not a run-state — there is no `BROKEN_TOPIC` run-state.
- **Notification-hook `notification_pending` bit (Wave B busy-signal)** — the SECOND lower-authority derivation input in `route_runtime`, for the previously invisible Workflow/permission approval waits (the gate blocks Claude with its `tool_use` open and NO JSONL trace, so the topic showed "🟡 Busy" forever). The Claude Code `Notification` hook (matcher-less, managed by `cc-telegram hook --install`; one-time startup warning when missing) writes `notify_pending/<session_id>.json` — `{ts, window_key, generation, kind}`, NO message text — and `handlers/notify_source.py` is its trust boundary: reads are HARD-predicated on `window_key == "tmux_session:window_id"` (a double-`--resume` sibling never lights), schema/future-skew validated, deliberately read-TTL-free. The poller consumes it at the TOP of the per-binding path (BEFORE the transition repaint and the adaptive capture gating, so a capture-skipped tick still consumes and a 🔔 transition repaints the digest the SAME tick) via `mark_notification_pending(route, set_at, generation)`, whose returned `NotificationMarkResult` DRIVES the generation-guarded unlink: `committed-live` → unlink AFTER the commit; `redundant-transcript-waiting` (already a transcript-set WAITING) / `stale-unlinked` (idle(transcript) or idle(pane) with an EMPTY stash) → unlink; `ignored-no-unlink` (unseen route — never seed). **Fix A (2026-07-08 — the `idle_prompt` kind-gate):** at the consume seam, a record with `kind == "idle_prompt"` (CC 2.1.204's ~60s post-turn nudge — exactly TWO observed `notification_type` values, `idle_prompt` + `permission_prompt`, stored verbatim by `hook.py`; this SUPERSEDES the Fix-#1-era "kind field is unreliable" type-space caveat) is DROPPED — generation-guarded unlink, NO `mark_notification_pending`, NO card — sited AFTER the on-disk TTL and BEFORE the same-generation reflected return (a reflected same-gen idle record cannot bypass it). `permission_prompt` / empty / any FUTURE unknown kind FAIL OPEN to today's commit-or-stale path (approval-gate safety). Rationale: `idle_prompt` = "the turn ended, Claude is at the input box" — the transcript end-of-turn already renders that; the bit exists only for approval gates. **Deriver precedence (top wins):** (1) transcript-interactive open id → WAITING_ON_USER; (2) `notification_pending` (over ANY `open_tools`, incl. a non-interactive Workflow id, or empty) → WAITING_ON_USER; (3) `pane_interactive_pending` with empty `open_tools` → WAITING_ON_USER; (4) non-interactive open tools → RUNNING_TOOL; (5) empty+active → RUNNING. The two bits clear INDEPENDENTLY; the pane bit's contract is untouched. **The IDLE(pane)+stash exception:** a notification on an idle route is stale by definition — EXCEPT idle(pane) with a non-empty `suspended_tools` stash, which is positive live proof the pane clear was false: the mark RESTORES the stash into `open_tools` and derives WAITING (the second stash-restore path beside Wave A's sidechain resurrection). **CLEAR rules:** a transcript `user` event clears unconditionally; `tool_result` / authoritative end-of-turn / assistant `tool_use` / `<task-notification>` clear ONLY when the event's JSONL timestamp (plumbed as `TranscriptLifecycleEvent.timestamp` by the adapter; parse failure ⇒ None) is strictly NEWER than `notification_set_at` — None/older PRESERVES (buffered pre-notification JSONL must not re-hide the wait; a preserved bit at end-of-turn keeps the route WAITING instead of idling); an unknown `tool_result` preserves (mirror of the pane bit). **Fix 1 (ISSUE-5 arm A): plain assistant `text`/`thinking` narration NO LONGER clears the bit** — a Workflow narrates *while* blocked, so the narration branches call `_clear_notification_if_setat_invalid` (the corrupt `set_at=None` invariant repair ONLY, reason INVARIANT), never the causal `_maybe_clear_notification_by_ts`. The poller clears when the pane is observed RUNNING at a capture taken strictly after `set_at + NOTIFY_PANE_CLEAR_MARGIN_S` (the user acted in the terminal — LEVEL + margin, NOT an idle→active edge: the adaptive watchdog capture can skip the blocked approval frame entirely, so an edge requirement strands the bit when the last pre-notification capture was already running; the blocked prompt REPLACES the run chrome, so a status-active frame sufficiently after the hook fired is positive proof execution resumed, and the margin keeps a same-tick capture of the pre-prompt frame from clearing early) and enforces `NOTIFY_TTL_SECONDS` (1800s — a product value: prompts are normally acted on within a session; past it the 🔔 silently degrades to 🟡 and the prompt stays discoverable on the pane) from RUNTIME state every tick, independent of side-file existence (a consumed file or a None-timestamp stream can never strand 🔔); pending-without-set_at violates the invariant and is treated as expired. Teardown drops the bit wherever route state clears (`mark_session_reset`, `clear_route`, `clear_routes_for_topic`); the side file is also unlinked on session replacement / `/clear` (old session id) / topic close, with the 24h `notify_source.gc_stale` (injected `is_live_session` conservative-skip) as the startup backstop. Pull-only throughout; no observer (c313657 stays forbidden). **Fix #1 (`BG_RUNNING` clear):** a §3.6 projected-busy 🔔 on a route whose parent is idle (only a background agent works) can't reach the `PANE_RUNNING` clear, so it stranded for the full 30-min TTL while the agent worked. `mark_background_agent_activity` now clears it on a heartbeat that is positive proof the bg work resumed (new reason `NotificationClearReason.BG_RUNNING`, the background analogue of `PANE_RUNNING`), scoped HARD: stored-idle shape only + the heartbeating key is the route's SOLE live bg key AND a plain Agent (no per-agent 🔔 linkage, so with >1 live key — sibling Agents or a `wf-task:` Workflow whose dir-wide mtime collapses its sub-agents — it fails closed lest a sibling's write clear another agent's genuine decision; hermes P1) + strict-newer `event_ts` + a `NOTIFY_BG_CLEAR_MARGIN_S` margin. Residual (safety-bounded): a 🔔 on a route with >1 live background agent is held to the TTL. **Fix #5:** the startup BUSY reconciler (`_reconcile_workflow_brackets_on_startup`) now ALSO re-lights plain `run_in_background` Agents (`subagents/agent-*.jsonl`) via `_reconcile_agents_for_parent` — structured-primary async-launch discriminator (`response_builder.async_agent_launch_id_from_meta`, prose fallback), same three-state fail-closed rule, NO persisted-`tracked_sessions` idempotency skip (the dominant pre-restart-tracked case must still lift).
- **Busy-signal completeness (ISSUE-5 + ISSUE-6) — full contract in `message-handling.md`.** Two coupled gaps closed in one wave (Fixes 1–4); the `↳` sub-agent DISPLAY cards for Workflow sidechains shipped as Fix 5 (see below). **Fix 1 (ISSUE-5 arm A):** plain assistant `text`/`thinking` narration no longer causally clears `notification_pending` (a Workflow narrates *while* blocked) — the narration branches call `_clear_notification_if_setat_invalid` (invariant repair only). **Fix 2 (ISSUE-6 + ISSUE-5 arm B):** the `Workflow` tool's background subagents now light typing + 🟡 via a parent-transcript bracket keyed `wf-task:<task_id>` that reuses the GH #44 `background_agents` marks verbatim (identity through `normalize_background_agent_key`). The launch anchor is STRUCTURED-primary (PR-2): `response_builder.workflow_launch_info_from_meta` reads the ENTRY-level `toolUseResult` (`{status:"async_launched", taskId, runId, transcriptDir}`, plumbed onto the tool_result `ParsedEntry.tool_result_meta` by `transcript_parser`; keyed on `taskId`, NEVER `status` alone — the Agent/Task `agentId` async-launch shares `status` but has no `taskId`), with `response_builder.extract_workflow_launch_info` (Task ID is MID-LINE — `(?im)^.*\bTask ID:…` — the captured id == the `<task-notification>` close key) as the PROSE FALLBACK (WARNING-logged for drift detectability); `transcriptDir` IS the validated `wf_dir` (no run-id-topology/glob). `session_monitor` opens a persistent `_WorkflowBracket`, emits a `bracket_heartbeats` refresh ONLY on a `wf_dir` `*.jsonl` mtime ADVANCE (DESIGN B — a separate channel from `ticks`; no parsing of sidechain entries for run-state), ages out via `BG_BACKGROUND_TTL_SECONDS` (2 h — a launched `wf-task:` key is `is_background=True`; the typing-unification T2 split, foreground-presumed keys keep the 30-min `BG_AGENT_TTL_SECONDS`) when writes stop, and closes GATE-ON-BRACKET (the `<task-notification>` emits the `wf-task:` done key IFF a live bracket exists — no id-format guessing). The close is caught in BOTH observed CC 2.1.198 shapes: a parent-idle `type:"user"` DELIVERY entry AND a busy-parent `queue-operation`/`enqueue` COMPLETION entry that `transcript_parser` synthesizes into the same `<task-notification>` user-text entry (`utils.is_task_notification`-gated, `lifecycle_only`), so a Workflow/bash/agent that completes while the parent is busy still tombstones (2026-07-08); the startup reconciler scans read the same queue-op lane tx-only. The live `wf-task:` key is also what makes ISSUE-5 arm B re-light (§3.6) instead of STALE_UNLINK. **Fix 3 (ISSUE-5 durable surface):** a typed `NotificationClearReason` channel (`notification_clear_reason` snapshot field; every True→False stamps a reason) drives a persistent, audible `attention.notify_waiting(kind="notification_decision")` decision card posted by the poller on `COMMITTED_LIVE` (gated by `has_interactive_surface`), kept via `_reconcile_decision_card` (retry-while-pending; the END_OF_TURN+live-bg-key EOT-gap keep; a `DECISION_CARD_EOT_GRACE_S` grace for the monitor's EOT-before-launch-fanout race), and dismissed kind-aware (`attention.dismiss_if_kind` — all generic display-layer `attention.dismiss` sites converted to `kind="interactive_ui"` so they never ack the decision card). Pull-only throughout; no observer. **Fix 5 (ISSUE-6 owner decision #2 — SHIPPED): Workflow `↳` DISPLAY cards.** `check_sidechain_updates` adds a SECOND, anchored `bracket.wf_dir.glob("agent-*.jsonl")` enumeration over the parent's OPEN brackets (the SAME `wf_dir` the heartbeat stats), driven through `_track_and_emit_sidechain_file(..., feed_run_state=False)` so Workflow sidechain ENTRIES NEVER feed run-state (the `wf-task:` bracket + mtime heartbeat stay the SOLE Workflow run-state input — `route_runtime` / `apply_sidechain_activity` / `_finalize_activity_digest` UNCHANGED). Run-id-qualified key `sub:<parent>:<runid>:<stem>` (concurrent-run disjoint; keeps the `sub:<parent>:` teardown prefix; `_short_subagent_id` renders the `agent-<id>` stem only). DISPLAY ONLY — rides the existing per-recipient `subagent_cards` gating + W2 collapse-on-done (path 1 own end-of-turn / path 2 parent backstop) PLUS a THIRD deterministic **route-FIFO close collapse**: the `<task-notification>` marks the bracket `closing` (not popped); `check_sidechain_updates` tails the final tail, appends a `NewMessage(subagent_collapse_prefix)`, pops the bracket; `bot.handle_new_message` → `message_queue.enqueue_subagent_collapse(route, prefix)` → a `subagent_collapse` route-FIFO control task (flood/RetryAfter-safe via `_RETRYABLE_TASK_TYPES`) → summary-gated `collapse_subagent_cards_with_prefix` (keep/verbose stays live). Bracket-gated + anchored discovery (never `rglob`); restart-degrades in lockstep with run-state. Pull-only; no observer.
- **Restart-safe AUQ pick dispatch (Wave 3 + v2.1.168 navigate-to-target)** — option-pick callback_data carries a stable `(route_hash, fp8, opt)` triplet in addition to the opaque token: `aqp:<route_hash>:<fp8>:<opt>:<token>`. The triplet is the key into `auq_action_ledger.jsonl` (append-only JSONL ledger). The callback handler consults the ledger BEFORE the in-memory `_pick_tokens` table, so a duplicate tap after `launchctl kickstart` answers "Action already received" instead of dispatching twice. Authorization remains the in-memory token + owner check — the ledger is for *idempotency*, not authentication. v4 §7.2 contract: owner-mismatch lookups peek the live token map and fall through to the token path only when the clicker holds a live token reconstructing the same key (legitimate collision); otherwise return `WRONG_USER_PICK_TEXT`. The keyed `aqp:<route_hash>:<fp8>:<opt>:<token>` shape is the only one the callback handler parses; the pre-Wave-3 `aqp:<token>` legacy shape is no longer accepted (a stray 1-part callback falls through to the malformed `else` → "Card expired, refreshing."). **The dispatch NAVIGATES the live cursor to the target option, VERIFIES, then presses Enter (single-select `aqp:` + review Submit/Cancel ONLY).** **DIGIT MODEL — CORRECTED on CC 2.1.207 (GH #50 rig, 2026-07-11).** The v2.1.168-era claim "a bare digit only MOVES the cursor" is **DEAD**. On 2.1.207 a bare digit is a live **HOTKEY** on every single-select-SHAPED surface: it COMMITS the option with NO Enter (rig-confirmed on AUQ single-select, ExitPlanMode, folder-trust, `Switch model?`; digit `4` — the `Type something.` affordance row — selects the free-text row and ARMS a mode). The 17 tested non-digit single characters (`a y n q z Y N space - ? …`) are inert, and out-of-range digits are inert, so the in-range digit set IS the complete hotkey alphabet. **AUQ MULTI-select digits still TOGGLE** — rig-cleared, so the shipped `aqt:` lane is SAFE and needs no fix. The navigate→verify→Enter model stays correct (it is the version-stable commit and it verifies the landing), but its RATIONALE is now the opposite of what was recorded: the digit is not too WEAK, it is too STRONG — an unverified digit would commit the wrong option. This is also why the GH #50 delivery gate refuses any payload whose emitted literal segments contain a bare-digit LINE. So the bot never trusts a bare digit for a single-select commit. `_dispatch_pick` (shared by the live `aqp:` path AND D2 recovery) finds the live `❯` cursor in `current_form`, computes `delta = target − cursor.number`, sends `Down`/`Up` × |delta| (`send_keys(enter=False, literal=False)`, return-checked), waits `NAV_SETTLE`, re-parses to VERIFY the cursor landed on the target (same cursor-blind fingerprint + `vc.number == target` + `_loose_label_match` + the review-Submit anchor for Submit), presses `Enter` (`enter=False, literal=False`), waits `COMMIT_SETTLE`, re-parses, and records `dispatched` ONLY after `_classify_advance` confirms the EXACT expected transition. Ledger non-success states: a **pre-commit bail** (cursor unknown / nav send False / verify fail — Enter provably never sent) records `not_advanced` and the callback **falls through** (a fresh-token re-tap re-validates); once `Enter` is sent an unconfirmed advance (incl. confirm capture/parse fail) records `commit_unconfirmed` and the callback **refreshes-only, never auto-redispatches**. The bare digit + the `auq_ledger.py` `digit_sent` / `failed_*_digit` states are now legacy-only (kept for on-disk compat). D2 restart-recovery inherits this automatically (it shares `_dispatch_pick`). **Scoped to single-select `aqp:` picks + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a bare digit — rig-cleared as SAFE on 2.1.207 (multi-select digits TOGGLE).** Validated against Claude Code v2.1.168 and re-characterized on 2.1.207 (GH #50 rig).
- **AUQ restart-recovery (D2)** — D3-β keeps a live card's *in-memory* pick tokens un-killable while the poller observes it, but a bot **restart** wipes them; the published card keeps its old keyboard with dead token strings, so the first tap hits `peek_none` and (pre-D2) degraded to the honest "tap again" modal for the card's whole life. D2 persists the per-token mint intent to a new leaf store (`pick_intent.py` → `pick_intent.jsonl`, written at the fresh `aqp:` single-select/Submit render; `aqt:` toggles excluded) so the `peek_none` / `expired` branches RECOVER and re-dispatch via `pick_token.recover_and_consume`. The store is keyed by the **token string** (a stale tap for form A can't read a newer same-key row B) and is kept **separate** from `auq_action_ledger.jsonl` — that ledger stays the 24h durable single-use authority; writing recovery state into its latest-wins `(route_hash, fp8, opt)` key would clobber a `dispatched` row and re-open double-dispatch. Recovery is **row-scoped**: a `_recovery_row_reservations[cache_key]` serialises concurrent sibling taps, a per-sibling action-ledger guard makes single-select single-use across siblings even across a crash, and a `consume_row` tomb is hygiene. It reproduces the live path's full **owner + `reject_stale_window_callback`** auth pair (the historic `peek_none` branch had neither) plus a callback-payload parity check against the stored intent, and **read-TTL-free** source parity (`auq_source.read_side_file_for_recovery`, comparing `_canonical_dict_fingerprint` — never the 12-hex `input_fingerprint`; pane fallback only when the side file is genuinely gone). The decisive invariant: recovery fires only on **positive proof of in-memory loss** (no `_pick_token_cache` row at the reconstructed `cache_key`) — a live row means the normal path owns it, a tombstoned row means this process just consumed it — so D2 is strictly the restart net and never double-handles the live path. The `accepted` claim is written INSIDE the row reservation (no release-then-claim gap), with a re-check of the cache-row + sibling proofs before it. Render/callback-path state only — NOT a `route_runtime` field; pull-only, no observer (c313657 stays forbidden). Tombed at `forget_ask_tool_input` (AUQ/EPM resolution + the `/clear` race via the OLD-window `forget_ask_tool_input(wid)` call) and `clear_topic_state`; orphan-safety is the recovery-time form/source re-validation + the 24h GC. Off-contract residual: a `jsonl_cache`-minted card DECLINES (its in-process getter is wiped on restart). The form fingerprint is now **cursor-blind on EVERY screen** — `AskUserQuestionForm._canonical_repr` omits the per-option cursor bit UNCONDITIONALLY (not just when `is_review_screen`); `auq_source._pane_fingerprint` shares that canonical so the pane source fingerprint collapses in lockstep. The cursor-blind fingerprint stays load-bearing under the v2.1.168 navigate-to-target dispatch: the bot MOVES the cursor to the target before committing, so the form identity must NOT change as the cursor moves (else the nav-verify re-parse would no longer match the minted fingerprint and every pick would bail `not_advanced`). A moved cursor — Submit↔Cancel on the review screen OR any option on a non-review picker — no longer rotates the pick token, so D2 recovery survives a cursor move on **every** screen; **the former D3-γ non-review DECLINE is RETIRED** (the non-review twin of the PR #28 review-screen fix). The review-Submit live + recovery guards share the cursor-blind `AskUserQuestionForm.review_submit_dispatchable` predicate (anchored on `is_review_screen` + option #1 + the literal `REVIEW_SUBMIT_LABEL` + the minted label; verified on Claude Code v2.1.161/.167/.168). The `_pane_fingerprint` ⇄ `_canonical_repr` shared-canonical coupling is load-bearing — guarded by the fingerprint-EQUALITY-across-cursor-move tests for BOTH the review screen and non-review pickers.
- **AUQ multi-select toggles** — multi-select option buttons use `aqt:<route_hash>:<fp8>:<opt>:<token>` and route to the interactive executor. `aqt:` validates the live token/window/form, dispatches a bare digit to tmux with no Enter, then re-renders from the pane. Toggles are not ledgered and do not consume sibling tokens; final Submit/Cancel is reached by Tab on the Claude Code review screen and reuses the existing `aqp:` pick/ledger flow. **The `aqt:` toggle still dispatches a bare digit — and the GH #50 2.1.207 rig CLEARED it:** on a MULTI-select picker a bare digit TOGGLES the checkbox (it does not commit), so the `aqt:` lane is SAFE as shipped and the historical "fast-follow" is CLOSED. (On a single-select-SHAPED surface the same digit COMMITS with no Enter — which is why `aqp:` navigates and verifies, and why the GH #50 delivery gate refuses a bare-digit payload line.)
- **AFK auto-resolve conversion + late answer (aql: — Wave A)** — on Claude Code ≥2.1.198 an unanswered AskUserQuestion self-resolves at ~60s (undocumented, no knob) with a "No response after 60s …" tool_result whose entry-level `toolUseResult` carries `answers: {}`. `bot.handle_new_message`'s explicit AUQ tool_result branch forks on `late_answer.is_afk_auto_resolve(msg.text, msg.tool_result_meta)` (two-factor: unanchored drift-tolerant regex + the AUTHORITATIVE answers-non-empty ⇒ False qualifier; meta-absent = sentinel-strip → negative wrappers reject first → anchored start; `tool_result_meta` is plumbed onto `NewMessage` at the PARENT emit site only). Non-AFK keeps today's teardown byte-identical. AFK calls `interactive_ui.convert_interactive_msg_to_late_answer` — ONE route-lock critical section, no await gaps: id-parity-trusted snapshot (window cache via `peek_ask_tool_use_id`, side-file fallback via `read_side_file_for_recovery`/`peek_side_file_tool_use_id`; empty side-file id = unknown), the exact `clear_interactive_msg` Phase-1 mirror (prune the POPPED `_interactive_mode` window ONLY, WARNING on mismatch), `forget_ask_tool_input`, `auq_ledger.release_window`; then `_fire_clear` + the Phase-2 EDIT run SHIELDED once Phase 1 commits (caller cancellation can't strand a tappable dead picker; the W1 delete-protocol precedent). The card is edited in place (plain) to "⏰ Claude proceeded after ~60s (no response)." with an `aql:` keyboard for single-question single-select only (multi-Q/multi-select/no-snapshot → text-only notice; no surface → logged skip; edit failure → no delete-fallback). The converted card is NOT a live interactive surface — `has_interactive_surface` goes False, the generic teardown skips, run-state clears via the transcript path (NO route_runtime change). A tap delivers the choice as a NORMAL user text message through the effort.py route-ordering subsequence (flush → `send_to_window` with the GH #50 `UserTurnStamp` pre-commit request → `mark_inbound_sent`), guarded by owner + stale-window + freshness (`has_interactive_surface` / `side_file_live_for_window` → "a newer prompt is live") + `begin_send` single-use; a send failure re-attaches the ORIGINAL keyboard and re-arms the single-use gate. The aqp:/aqt:/pick_token/pick_intent/auq_ledger dispatch machinery is byte-untouched; ExitPlanMode is OUT of scope (60s behavior unobserved for EPM). Registry is in-memory only (restart ⇒ graceful expired modal). Full contract in `message-handling.md` §"AFK auto-resolve conversion + late answer".
