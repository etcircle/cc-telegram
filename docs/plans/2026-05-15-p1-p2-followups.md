<!-- /autoplan restore point: /Users/felixcardix/.gstack/projects/etcircle-cc-telegram/main-autoplan-restore-20260515-185048.md -->
# Plan — Interactive UI P1/P2 Follow-ups (post-multi-tab)

**Date:** 2026-05-15
**Branch:** to be cut from `main` (currently clean, all multi-tab PRs #9-#14 merged)
**Status:** Draft for /autoplan review
**Origin:** Hermes EOD peer review against the merged multi-tab AskUserQuestion work. Raw notes archived in `docs/handovers/2026-05-15-multi-tab-eod.md`.

---

## Goal

Close the 5 P1 and 2 P2 defects: 4 P1 + 2 P2 from the EOD Hermes review of the single-card AskUserQuestion flow, plus 1 P1 (P1.5 — stale-session quoted reply dropped) raised by the user during /autoplan. All six involve **stale state surviving past its useful life** — old keystroke buttons firing into the next picker, old scrollback matching as "live UI", old pick-tokens outliving the card they were minted for, or the JSONL cache lagging behind a pane the poller already sees. The product win is **eliminate the wrong-action class of bug** before re-enabling the dormant multi-tab dispatch (#11 / #13 — currently gated off in #14).

The single-card flow is the live behaviour. Multi-tab state-machine code is dormant but still reachable through the resolver (P1.2) and other shared infrastructure (route lock, pick-token cache). Fixing these P1/P2s is a precondition for **safely** flipping multi-tab back on later.

## Non-goals

- Re-enabling multi-tab dispatch. That comes after these fixes, in a separate plan.
- Refactoring the route-lock contract beyond what P2.1 forces (decide: remove from single-card OR honor the comment; nothing more).
- The pre-existing deferred-render race at `bot.py:3076-3082` (PR #5 shipped one half; the other half is still owed). Mentioned in the handover, distinct from P1.4, not in scope here.
- Description-rendering test gap (called P3 in the handover) — fold into P1.2's regression test rather than ship a separate piece of work.

---

## The six defects

Files and line ranges verified against the current tree at HEAD `20250e6`.

### P1.1 — Stale nav-keystroke dispatch
**Where:** `src/cctelegram/bot.py:2739-2858` — **9 callbacks**: `CB_ASK_UP/DOWN/LEFT/RIGHT/ESC/ENTER/SPACE/TAB/REFRESH` (F1: REFRESH was missed in original plan).
**What's wrong:** Each callback runs `reject_stale_window_callback(window_id)` then unconditionally `send_keys`. No check that (a) an interactive surface is still owned by this route, (b) the callback's window matches the active interactive window, or (c) the pane currently shows an interactive UI.
**Why it matters:** A user tapping nav buttons on a stale Telegram card dispatches Tab / Arrow / Enter into tmux even when no picker exists. Those keystrokes get buffered by the terminal and consumed by the **next** AskUserQuestion that opens — auto-resolving it with whatever cursor positions happen to be active when each Enter lands. **Wrong-action bug class.**
**Evidence:** 2026-05-15 18:07:09 — a fresh Test 5 question landed in the JSONL with `tool_use` + `tool_result` paired in the same batch (<2s gap), answered with cursor positions the user never deliberately selected. Same family as the 2026-05-08 stale-attention-callback bug.
**Fix shape (post-eng-review):** extract a helper, do not inline 9× (F3):
```python
async def assert_nav_dispatchable(query, user_id, thread_id, window_id) -> tmux.Window | None:
    """Returns the live tmux window or None after answering the callback.
    Used by all 9 nav callbacks to guard send_keys."""
    if not has_interactive_surface(user_id, thread_id):
        await query.answer("No live interactive UI"); return None
    if get_interactive_window(user_id, thread_id) != window_id:
        await query.answer("Window changed"); return None
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        await query.answer("Window not found"); return None
    # CB1 + CB5: liveness check with PRESENT/ABSENT/UNKNOWN ternary.
    visible = await tmux_manager.capture_pane(window_id, scrollback_lines=0)
    if not visible:
        return w  # UNKNOWN — empty visible capture (alt-screen / tmux race). Proceed.
    if not is_interactive_ui(visible):
        # Try anchored detection on picker bottom-border before declaring ABSENT.
        # See CB5 for the anchor approach — `╰─` near visible-pane bottom indicates
        # the picker is live even if the question text pushed the header off the top.
        if not _has_picker_anchor(visible):
            await query.answer("Picker closed, refreshing"); return None
    return w
```
**ESC carve-out (F2):** ESC bypasses `assert_nav_dispatchable`. The desired behaviour for ESC on a stale picker is to **still run `clear_interactive_msg`** (cleanup is idempotent and what the user wants). ESC keeps its existing `reject_stale_window_callback` check, then does `send_keys("Escape")` if a window exists AND `clear_interactive_msg` unconditionally.
**Perf gate (F5):** PR A adds one extra `capture_pane` per callback. Add a micro-benchmark: 100-cycle capture timing before/after. Assert <50% throughput regression.

### P1.2 — Review-screen options corrupted (highest severity — wrong action)
**Where:** `src/cctelegram/terminal_parser.py:892-910` — multi-question branch of `resolve_ask_form`.
**What's wrong:** When `pane_form.is_review_screen=True` and the JSONL has multiple questions, the resolver still runs `current_q = jsonl_form.questions[current_idx]` and `options = _overlay_cursor(current_q.options, pane_form.options)`. The visible pane options are **Submit / Cancel**, but the resolver returns Q1's options with `is_review_screen=True`.
**Why it matters:** The renderer prints "Ready to submit your answers?" with Q1's labels under it ("1. A / 2. B"). Pick-token mint uses Q1's labels but the picker treats digit 1 as **Submit**. Tapping "1. A" submits the whole form. **Wrong-action bug, not display bug.**
**Evidence:** Hermes verified with a constructed pane that the resolver produces the corrupt shape end-to-end.
**Fix shape (post-eng-review):**
1. **Review-screen short-circuit (original P1.2 fix):** before the current-question overlay in `resolve_ask_form`, when `pane_form.is_review_screen` is true on a multi-question JSONL, return a form built from the pane (Submit/Cancel options + cursor) with `questions` preserved from JSONL for tab-strip context, and `current_question_title=None`. Set `current_tab_inferred=False` on this branch (the pane authoritatively shows review; no inference happened).
2. **CB6 — Strong-match requirement before overlay:** when not a review screen and the multi-question branch infers `current_q`, require that `current_q.title` matches a non-trivial substring of `pane_form.current_question_title` OR that option-label overlap ≥50%. If neither holds, the resolver returns the **JSONL-only** form with no pane overlay AND **no pick buttons mintable** (manual-nav only mode). This prevents minting buttons for one question and validating against another.
3. **Field-policy table** (for the PR description, eliminates ambiguity): document for the review-screen branch what `tabs`, `current_tab_inferred`, `is_free_text`, `pane_excerpt`, `questions` should be set to. Without this, the pick-token fingerprint can disagree between mint and validate.
**Regression tests:** multi-question JSONL + review-screen pane → resolver returns Submit/Cancel options, renderer shows no Q1 option text. **Drift test:** pane shows Q3 (title not in JSONL) + JSONL has Q1/Q2 → resolver returns no pane overlay, no pick buttons. **Fingerprint round-trip:** mint → validate same pane → fingerprints match across the new short-circuit branch.

### P1.3 — `is_interactive_ui` matches stale scrollback
**Where:** `src/cctelegram/handlers/interactive_ui.py:1375-1390` (`handle_interactive_ui`) and `src/cctelegram/bot.py:2911-2914` (pick-token validator).
**What's wrong:** Both sites call `capture_pane(window_id, scrollback_lines=100)` then use `is_interactive_ui(pane_text)` as the liveness predicate. `is_interactive_ui` is **not** "current screen only" — handed scrollback, it matches stale historical pickers still sitting in the buffer.
**Why it matters:** The bot can believe a picker is live when it closed minutes ago. Pick-token validator then validates against historical UI in scrollback instead of the live screen. Combined with P2.2, stale tokens stay valid even after the route's card is gone.
**Fix shape:** two-phase capture at both sites:
1. `visible = await capture_pane(window_id)` (scrollback=0).
2. If `not is_interactive_ui(visible)` → bail/return.
3. Only then `scrollback_pane = await capture_pane(window_id, scrollback_lines=100)` for the structured parse that needs scrollback for long questions whose options have scrolled off-screen.

### P1.4 — Status poller races JSONL monitor → partial pane render
**Where:** `src/cctelegram/handlers/status_polling.py` (caller) + `src/cctelegram/handlers/interactive_ui.py:handle_interactive_ui` (renderer).
**What's wrong:** The 1Hz status poller can detect an interactive UI in the pane **before** `session_monitor` has parsed the matching `tool_use` JSONL entry and called `remember_ask_tool_input`. With the cache empty, `_resolve_ask_tool_input` returns `None`, `resolve_ask_form` falls back to `parse_ask_user_question(pane)` alone, and the pane-only parse can miss options 1-N if the visible pane shows only the trailer ("Type something" / "Chat about this").
**Evidence:** screenshot `~/.cc-telegram/images/1778866630_AQADHg5rG6AuQFB9.jpg` from window @34 at 18:35 — card rendered with only options 4 and 5; the actual options 1-3 from Claude's AskUserQuestion never appeared.
**Fix shape (post-eng-review — CB2: bounded wait + fallback):**
1. **Per-window defer state:** introduce a `_deferred_since: dict[window_id, float]` tracking the timestamp of the first defer for each window. On `_resolve_ask_tool_input(window_id, None) is None` AND `pane_form.options[0].number > 1`, record `_deferred_since[window_id] = time.time()` on first occurrence; return False with `logger.info("Deferring poller render on empty JSONL cache for window %s", window_id)`.
2. **Bounded wait:** if `time.time() - _deferred_since[window_id]` exceeds `RENDER_FALLBACK_TIMEOUT_S` (default 8s, env-overridable `CC_RENDER_FALLBACK_TIMEOUT_S`), **force-render with the pane form only** AND **mint no pick buttons** (manual-nav mode, same as CB6 weak-inference branch). Add `(partial)` marker to the Telegram message so the user sees this is a fallback render.
3. **Cache-arrival upgrade:** when JSONL later arrives and `remember_ask_tool_input` fires, the next `handle_interactive_ui` cycle re-renders the card with the full form and pick buttons; **invalidate any old pane-only pick-tokens for this window** during the upgrade (the new tokens carry full-form fingerprint).
4. **Clear on tool_result:** `_deferred_since.pop(window_id, None)` whenever a `tool_result` is parsed for this window.
**Rejected alternative:** Hermes's option (b) `asyncio.sleep(0.5)` — simpler but still has the indefinite-stall failure mode if JSONL never arrives.
**Logging:** every defer at INFO (not DEBUG) so a stalled session_monitor is visible in production logs. Every fallback render at WARNING so it's visible in monitoring.

### P2.1 — Route lock is dead weight in single-card flow
**Where:** `src/cctelegram/handlers/interactive_ui.py:155-179` (lock contract), `:1464-1482`, `:1585-1601`, `:1605-1678`.
**What's wrong:** Comment at `:155` claims the lock protects `_interactive_msgs / _pick_token_cache / _pick_tokens / _interactive_mode / _latest_ask_tool_input`. In practice, with multi-tab dispatch disabled in `handle_interactive_ui`, the lock is only acquired around the dormant `_multi_tab_sessions.pop(...)` path in `clear_interactive_msg`. Single-card reads and writes to `_interactive_msgs` and `_interactive_mode` happen outside the lock. A cleanup can interleave with a send/edit and leave orphans or resurrect `_interactive_msgs` after `clear_interactive_msg`.
**Why it matters:** Quiet correctness drift. Comments and code disagree; future reader trusts the wrong one.
**Fix shape (post-eng-review — CB4: scoped option (a)):**
- **Scope:** remove the lock from `_interactive_msgs / _interactive_mode` reads/writes in single-card paths. **Keep the lock for `_pick_token_cache` / `_pick_tokens` mutations** (P2.2 pruning depends on it; mint code paths also write these and must serialize with prune).
- **Comment rewrite at `:155-179`** must reflect this scoping precisely:
  - "Lock protects: `_pick_token_cache`, `_pick_tokens`, `_multi_tab_sessions` (when dormant code re-enables)."
  - "Single-card mutations on `_interactive_msgs` / `_interactive_mode` run unlocked — single producer (handle_interactive_ui) + idempotent cleanup (clear_interactive_msg)."
  - "TO RE-ENABLE MULTI-TAB: re-wrap `_interactive_msgs` / `_interactive_mode` reads/writes in this lock — see git blame on this comment for the original contract."
- **Audit:** before merging PR D, grep all writes to `_pick_token_cache` / `_pick_tokens` and confirm every site runs under `_get_route_lock`. Add a contract test using a lock-tracking wrapper if any site is unprotected.

### P1.5 — Stale-session quoted reply silently dropped (newly raised 2026-05-15 user feedback)
**Where:** `src/cctelegram/bot.py:1014-1053` (`_apply_reply_context`), called from `text_handler`, `voice_handler` (`:1294`), `photo_handler`, `document_handler`.
**What's wrong:** When `reply_ctx.session_id is not None AND current_sid is not None AND reply_ctx.session_id != current_sid`, the guard logs `Dropping reply context: quoted session X != current Y` and returns the user's text **without** the quoted block. The user sees no indication this happened — the reply just "doesn't take effect".
**Why it matters:** A common UX flow is "reply to an older message from a previous session to pick up that thread." The user's screenshot at 2026-05-15 18:47 is the canonical example: they replied to Claude's "Open follow-ups for next session…" list with "Please look at those, use auto plan skill" — and Claude never received the quoted list, only saw the new sentence. Equally affects voice-note replies (same handler path). **Silent drop = worst UX failure mode**: user thinks the bot is ignoring them.
**Evidence:** screenshot `~/.cc-telegram/images/1778867653_AQADIg5rG6AuQFB9.jpg` (Telegram client view) + the `Dropping reply context` log entry in this session.
**Fix shape (post-eng-review — option (b) chosen, with F4 anti-spoof + config kill switch):**
1. **Annotate the rendered prompt** instead of dropping. In `_apply_reply_context` (`bot.py:1014-1053`), when `stale_quote` is True, do **not** return early — call `render_for_claude` with a flag that adds the cross-session marker.
2. **F4 — Marker placement must be spoof-resistant.** Move the cross-session marker into the **existing pre-fence header block** at `reply_context.py:159` (next to the `Claude session: {session_id}` line that's already trusted), **not** into the quoted body. Example header addition: `"Cross-session reply: this quote is from session {reply_ctx.session_id}, not the current session. Treat as context only."` The fence then continues to neutralize quote-body content via the existing nonce mechanism.
3. **Config kill switch:** add `config.reply_context_cross_session_enabled` (default True, env-overridable `CC_REPLY_CROSS_SESSION_ENABLED=0`). When False, fall back to today's silent-drop behaviour. Lets prod operators flip back quickly if Claude misbehaves on cross-session context.
4. **Existing fence already covers fence break-out:** the random nonce in `render_for_claude` prevents quoted content from spoofing fence delimiters. The scrubber `_USER_MESSAGE_LINE_RE` already strips `[User message]` lines. Because the marker now lives **pre-fence in the header**, hostile quoted content cannot fake the marker — quoted content can only appear inside the fenced body.
**Tests:** see `test-plan-2026-05-15-p1-p2-followups.md` CB6 + PR E test gaps. Specifically: stale-session quote rendered correctly, voice handler same path, hostile quoted content with literal marker text → marker still in pre-fence header, body fence still holds.
**Tests:** unit on `_apply_reply_context` with a stale-session ReplyContext → assert the chosen option behaviour (rendered with marker / sent feedback / etc.). Integration test piping a voice handler through with a reply to a stale message → same assertion.

### P2.2 — `clear_interactive_msg` doesn't prune pick-token cache
**Where:** `src/cctelegram/handlers/interactive_ui.py:1605-1678`.
**What's wrong:** Drops `_interactive_msgs / _multi_tab_sessions` and deletes Telegram cards, but does **not** clean `_pick_tokens / _pick_token_cache` for the cleared route. Old structured-pick callbacks remain live until the 5-minute TTL.
**Why it matters:** Combined with P1.3 (scrollback-based liveness), a stale pick-token can validate against a stale picker in scrollback even after the card is deleted. Another wrong-action vector.
**Fix shape (post-eng-review — CB3 + CB4):**
1. **In `clear_interactive_msg`, under the route lock acquired at `:1620`:** snapshot `_interactive_mode[ikey].window_id` BEFORE popping; then pop the route's `_pick_tokens` entries (iterate, pop tokens whose entry matches user_id+thread_id+window_id) AND remove the corresponding `_pick_token_cache` entries. **This stays inside the lock** (CB4 — token cache mutations must serialize with concurrent mints in `handle_interactive_ui`).
2. **Fix `consume_pick_token` order at `bot.py:2860-2885` (CB3 — SECURITY).** Current order: consume → check user_id. Wrong user can burn another user's token. New order:
   - **Under lock:** look up token entry without consuming. Validate `entry.user_id == update.effective_user.id` AND `entry.window_id == /* route's bound window */`. If validation fails, return early without modifying state.
   - **Still under lock:** atomically consume the token AND its sibling cache entry (move the existing two-step into one critical section).
3. **Token entries carry route + fingerprint** (already via `_pick_tokens[token].entry`): document explicitly that `entry.window_id` and `entry.fingerprint` are the authorization keys. The refresh handler at `bot.py:2872` uses `entry.window_id`, not "current interactive window" guessed from user/thread.

---

## PR strategy

Handover suggested "1 PR per P1, P2s ride with related P1s." That maps cleanly:

| PR | Scope | Why bundled |
|---|---|---|
| **A — P1.1 + P1.3** | Nav-callback liveness guard + two-phase capture | P1.1's guard depends on the visible-only capture pattern P1.3 introduces. Same file (`bot.py`) for one of the two P1.3 sites; the other is `interactive_ui.py` but the change is one-line per site. |
| **B — P1.2** | Review-screen short-circuit in resolver + regression test | Pure parser change. Isolated. Highest severity → easiest to ship first if PR A drags. |
| **C — P1.4** | Defer-on-empty-cache in poller path | Single condition added to `handle_interactive_ui`. No coupling to A/B. |
| **D — P2.1 + P2.2** | Route-lock cleanup + pick-token pruning in clear | P2.2's snapshot-then-pop must respect P2.1's decided lock contract. Same function family. |
| **E — P1.5** | Stale-session quoted reply handling | Pure change in `_apply_reply_context` + per-handler integration check. Architecturally separate from the interactive-UI fixes (A-D). Can ship independently. |

**Ordering (post-eng-review — adopted: B → E → D → A → C):**
1. **PR B (P1.2)** — first. Wrong-action in live single-card path, smallest blast radius, parser-only change. Ship within 24h.
2. **PR E (P1.5)** — second. Architecturally independent, visible UX win, kill-switched via config. Can ship right after B without coupling.
3. **PR D (P2.1 + P2.2)** — third. Token-consume security fix (CB3) lands here; A's "picker closed" branch depends on D's pruning being complete. Land D BEFORE A.
4. **PR A (P1.1 + P1.3)** — fourth. Largest surface (9 callbacks), depends on D's lock contract, includes long-question pressure test resolving R1, includes perf gate.
5. **PR C (P1.4)** — last. Lowest user impact (missed render → next cycle catches up); race semantics easier to reason about once A and D have stabilized. Includes bounded-wait timeout (CB2).

**Rationale for this ordering:** prioritizes (a) user-visible severity early (B, E), (b) security blocker not-too-late (D), (c) coupling-required prerequisites before dependents (D before A), and (d) lowest-impact race fix last (C). Codex preferred `E → A → C → B → D` (capture semantics first); subagent preferred this order. Convergence point: B and E both ship early, D before A.

**Rollback contract:** each PR ships behind its own commit; revertable in isolation. PR D's revert reverts PR A's pruning assumptions — call this out explicitly in PR D description.

## Tests

- **P1.1** — async test: mock tmux, mint a fresh window, clear the interactive surface, fire a nav callback for that window → assert no `send_keys` call, assert "No live interactive UI" reply. Repeat for the "window changed" and "picker closed" branches.
- **P1.2** — golden test (new) feeding the resolver a constructed pane with Submit/Cancel + a multi-question JSONL. Assert returned form has Submit/Cancel options, `is_review_screen=True`, `questions` preserved. **Renderer test:** feed that form into the renderer, assert output contains "Submit" / "Cancel" and does NOT contain Q1 option labels.
- **P1.3** — unit on the two-phase capture helper (if extracted); integration test driving `handle_interactive_ui` with a scrollback pane that contains a stale picker but visible pane that is a shell prompt → assert returns False, no render.
- **P1.4** — drive `handle_interactive_ui` via the poller path with an empty `_latest_ask_tool_input` and a pane form whose first option number > 1 → assert returns False, no render. Then populate the cache, drive again → assert renders normally.
- **P2.1** — depends on chosen direction. If (a): test that single-card cleanup is idempotent under concurrent send/edit (asyncio gather of clear + edit). If (b): assert the lock IS acquired around single-card mutations (instrumentation or contract test).
- **P2.2** — mint a pick-token for a route, call `clear_interactive_msg`, assert the token is no longer in `_pick_tokens` and the route's cache entry is gone.

Re-enable the skipped `TestMultiTabPostN::test_post_n_cards_for_multi_question` only when multi-tab is re-enabled (out of scope here).

## Risks and unknowns

- **R1 — visible-only capture changes detection in long-question cases.** P1.3 says "visible only for the liveness check, scrollback for the parse." Need to be sure the liveness predicate `is_interactive_ui(visible)` is reliable when the question text has pushed the picker border off the visible pane. Mitigation: tmux pane is large in practice (terminal is usually 50+ rows); pick a small scrollback window like 10-20 lines if pure visible misses too often. **/autoplan should pressure-test this.**
- **R2 — P1.4 option (a) could starve renders.** If the JSONL parse is slow for some reason, the poller keeps returning False until the cache catches up. Bounded latency = one poll cycle (~1s) per missed render attempt, which is acceptable. Worth instrumenting with a debug log.
- **R3 — P2.1 (a) leaves the multi-tab path's lock contract intact and removes single-card lock usage entirely.** When multi-tab re-enables, the lock contract has to be re-extended to cover single-card mutations again. Document this explicitly in the contract comment.
- **R4 — Bundling P1.1 + P1.3 in PR A means two related-but-distinct changes land together.** If a regression appears, harder to bisect which one. Mitigation: separate commits within the PR, clear commit messages.

## Out of scope

- Re-enabling multi-tab dispatch (the gate-off in `handle_interactive_ui` near "Multi-tab dispatch DISABLED at user request").
- The pre-existing deferred-render race at `bot.py:3076-3082`.
- `_route_locks` keyspace cleanup (never pruned — small in practice per the comment).
- Pick-token TTL change (5 minutes is fine once P2.2 prunes on teardown).

## What already exists in the tree

- `reject_stale_window_callback` — stale-window guard pattern, called by every nav callback today. P1.1 adds **more** checks on top of this, not a replacement.
- `has_interactive_surface` / `get_interactive_window` — already exist in `interactive_ui.py`; used by the structured-pick callback at `bot.py:2911`. P1.1 reuses them.
- `tmux_manager.capture_pane(window_id, scrollback_lines=0)` — default is scrollback=0; explicit override to 100 is in `interactive_ui.py:1375` and `bot.py:2911`. P1.3's two-phase pattern uses both modes intentionally.
- `is_interactive_ui` / `extract_interactive_content` / `parse_ask_user_question` — parser entry points, used by both the JSONL path and the pane fallback. P1.3 changes only the liveness predicate's input, not the parser.
- `_pick_tokens / _pick_token_cache` + `_prune_expired_pick_tokens` — TTL-based pruning exists. P2.2 adds explicit route-scoped pruning on `clear_interactive_msg`.

## Rollout / deploy

- Each PR: ruff + pyright + 770-test suite must pass.
- After each merge: `uv tool install --reinstall --force --from . cc-telegram && launchctl kickstart -k gui/501/com.felixcardix.ccbot` (per CLAUDE.md note: launchd-managed, not restart.sh).
- Post-merge live check: open a topic, fire an AskUserQuestion in tmux, exercise the relevant codepath. For P1.2 specifically: open a multi-question form, navigate to the review screen, verify the card shows Submit/Cancel and not Q1 options.

## Dream-state delta

**Today (post-#14):** single-card AskUserQuestion works well for the user. Six known correctness bugs in the supporting infrastructure (one of them — P1.2 — is reachable through any multi-question form's review step). Multi-tab dispatch is dormant.

**After this plan:** zero known correctness bugs in the single-card path. Pick-token lifecycle matches card lifecycle. Liveness checks use the live screen, not history. Nav callbacks can't fire into the wrong picker. Multi-tab dispatch is **safe to re-enable** in a follow-up plan — P1.2 specifically was a multi-question artifact and would re-surface immediately if multi-tab flipped back on without this fix.

**12-month ideal:** structured interactive UI handling generalizes beyond AskUserQuestion (ExitPlanMode, permission prompts, future tools). Liveness becomes an explicit lifecycle (open → live → submitted → closed) rather than scrollback heuristics. This plan moves toward that by hardening the lifecycle assumptions, not yet by formalizing them.

---

## GSTACK REVIEW REPORT — Phase 1 CEO

### CODEX SAYS (CEO — strategy challenge)

Codex challenged the framing: AskUserQuestion has "too many semi-authoritative sources" — JSONL tool input, tmux visible pane, tmux scrollback, Telegram card state, pick-token cache, route lock, dormant multi-tab session state. Patching each leak preserves the layer cake. Better 10x reframing: **event-source AskUserQuestion from JSONL**, treat tmux as actuation/liveness only, invalidate cards on `tool_result` / session change / next `tool_use`. Stop parsing scrollback for semantic truth.

Premises Codex flagged as weakest:
1. "Multi-tab is desirable later" — user already prefers single-card; multi-tab is sunk-cost dormant infra.
2. "Stale-state hardening is the right framing" — half-wrong; for nav/tokens yes, for resolver corruption / poller race it's an **authority conflict** problem.
3. "Single-card is the right pattern" — probably right, but "terminal-scraping + JSONL cache" is not. Right pattern is single-card with **explicit form identity + lifecycle**.

Codex's bundling recommendation:
- PR 1: collapse P1.1/P1.2/P1.3/P1.4/P2.2 into one lifecycle PR (form generation, JSONL-primary rendering).
- PR 2: P1.5 alone (different defect class, different UX call).
- PR 3: delete or hard-quarantine multi-tab dead state.

### CLAUDE SUBAGENT (CEO — strategic independence)

Independent Claude subagent (no prior-phase context) converged on the same reframing without seeing Codex output: **introduce `InteractivePickerSession` keyed by `(window_id, generation)` with explicit transitions** (`pending → live → submitted → closed`). Every nav callback, pick-token validator, and poller path checks `session.is_live(generation)` instead of `is_interactive_ui(scrollback)`. P1.1 + P1.3 + P1.4 + P2.1 + P2.2 all collapse into "is this callback's generation current?" — one predicate replaces five guards.

Subagent's specific tier ranking:
- **P1.2** is in a different tier — only wrong-action bug in the **live** single-card path. Ship alone immediately.
- **P1.5** is architecturally unrelated to the rest. Ship alone immediately. ~20-line change.
- **P1.1 / P1.3 / P1.4 / P2.1 / P2.2** — defer until multi-tab fate is decided. If multi-tab dies, P2.1 evaporates and P2.2 shrinks 80%.

Subagent also flagged dismissed alternatives the plan didn't argue:
- Always render from JSONL only, kill the poller-driven render entirely (~1s render lag acceptable).
- Make `is_interactive_ui` take a `current_screen_only=True` arg instead of capturing twice.
- Replace pick-token TTL with generation-based invalidation.

6-month regret: "You shipped 5 PRs of scrollback-inference hardening; multi-tab never came back; the same class of bug surfaced in the ExitPlanMode and permission-prompt handlers; you re-wrote it all with a lifecycle model anyway. **Net waste: 4 of 5 PRs.**"

### CEO DUAL VOICES — CONSENSUS TABLE

```
═══════════════════════════════════════════════════════════════════════
  Dimension                                  Codex   Subagent  Consensus
  ──────────────────────────────────────────  ──────  ────────  ────────
  1. Right problem framing?                   NO      NO        DISAGREE WITH PLAN
  2. Premise "multi-tab returns" valid?       NO      NO        DISAGREE WITH PLAN
  3. 7-defect 5-PR scope calibrated?          NO      NO        DISAGREE WITH PLAN
  4. P1.5 belongs in this plan?               NO      NO        DISAGREE WITH PLAN
  5. P1.2 should ship alone first?            YES     YES       CONFIRMED
  6. Lifecycle reframing is the real fix?     YES     YES       CONFIRMED
  7. 6-month risk of rework if shipped as-is? HIGH    HIGH      CONFIRMED HIGH
═══════════════════════════════════════════════════════════════════════
```

**Both voices agree 7/7 dimensions.** This is a strong cross-model signal that the plan-as-written is patching symptoms rather than addressing the substrate. Both independently proposed the same reframing (lifecycle / form-generation / JSONL-primary) without seeing each other's analysis.

### USER CHALLENGE (per /autoplan classification)

Both models agree the user's stated direction (5-PR stale-state hardening) should change. This is the highest-confidence kind of finding /autoplan can produce: **independent convergence on a structural reframing**. Per /autoplan rules, this is presented at the premise gate — the user has context the models lack (e.g., whether multi-tab is committed for a future product reason, whether the lifecycle refactor cost is affordable now).

What both models recommend:
- Ship **P1.2** and **P1.5** alone, today/this week. Both are user-visible, both are zero-coupling, both are architectural one-offs.
- Defer P1.1, P1.3, P1.4, P2.1, P2.2 until a separate **lifecycle plan** is drafted (form-generation invalidation, JSONL-primary rendering).
- Make an explicit decision on multi-tab: **delete** the dormant `_MultiTabSession` / `_route_locks` / `_handle_multi_tab_ask` code, or write down the concrete user reason to keep it.

If we're wrong, the cost is: ~2 weeks delay on already-flagged correctness bugs, and another round of planning before the bigger lifecycle PR. The defects don't get worse in the meantime; the live UX (single-card) is what's shipped.

### Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | Phase 0 | Skip Phase 2 (Design) | Mechanical | P3 pragmatic | Scope regex flagged false positives; plan has no new UI design surface | Full pipeline |
| 2 | Phase 0 | Skip Phase 3.5 (DX) | Mechanical | P3 pragmatic | No new developer-facing API/CLI surface | Full pipeline |
| 3 | Phase 1 | Run dual voices (Codex + Claude subagent) | Mechanical | P6 bias toward action | Get cross-model signal regardless of Hermes prior review | N/A |
| 4 | Phase 1 | Premise + scope reframing requires user judgment | User Challenge | N/A — never auto-decided | Both models converge on lifecycle reframing; user has context on multi-tab future + refactor cost | N/A |
| 5 | Phase 1 | User rejected reframing → ship 5-PR hardening plan as drafted | User Challenge resolved | User decision | User has context not in plan (multi-tab future commitments / refactor budget). Models documented the risk; user accepted it. Eng review will focus on the plan as drafted. | Lifecycle reframing (rejected); delete multi-tab (rejected) |

---

## GSTACK REVIEW REPORT — Phase 3 Eng

### CODEX SAYS (Eng — architecture challenge)

Codex grounded in real source files. Key new findings beyond the plan:
- **Pick-token consume order is a security bug.** `bot.py:2865` consumes the token *before* checking `entry.user_id` at `:2880`. A wrong user can burn someone else's valid button. `consume_pick_token` mutates `_pick_tokens / _pick_token_cache` outside the route lock despite the contract claiming protection. **Fix: lookup without consuming, validate user/window/route under lock, then atomically consume token+sibling cache under same lock.**
- **P1.4 risk reframed:** "no JSONL = no render" is the exact failure mode the Telegram bridge cannot afford. Bounded wait + pane-only fallback (no pick buttons) is mandatory.
- **P1.2 fingerprint matching must be strong.** If pane_form title/options drift from JSONL, currently the resolver still merges and mints buttons. Require title match or option-label overlap before overlay; otherwise render manual-nav only.
- **PRESENT / ABSENT / UNKNOWN ternary.** All `is_interactive_ui` callers should treat empty/ambiguous capture as UNKNOWN and never destructively clear from UNKNOWN.

### CLAUDE SUBAGENT (Eng — independent review)

Independent subagent (no Codex context) converged on most of the same blockers plus:
- **`CB_ASK_REFRESH` at `bot.py:2854` follows the same pattern as the 8 nav callbacks and was missed by the plan.** PR A must cover 9 callbacks, not 8.
- **ESC asymmetry:** The plan's enumerated guard would block ESC from clearing a stale card. Carve out: on "picker closed" branch, ESC should still call `clear_interactive_msg` (cleanup is the desired behaviour).
- **`assert_nav_dispatchable` helper, not 8-9x copy-paste.** Plan implies copy-paste; subagent says extract.
- **R1 (long-question pressure test) is the highest hand-wave** in the plan. Real fix: anchor on picker bottom-border (`╰─`) detection, not "visible vs scrollback split."
- **P1.5 (b) header marker `[quoted from previous session ...]` is spoofable** — `_USER_MESSAGE_LINE_RE` doesn't scrub it. Either extend the scrubber or move the marker into the existing pre-fence header block (`reply_context.py:159` is the right home).
- **Performance:** P1.3 two-phase capture doubles tmux syscall rate per active window. Plan should include a micro-benchmark gate (<50% regression).

### ENG DUAL VOICES — CONSENSUS TABLE

```
═══════════════════════════════════════════════════════════════════════
  Dimension                                  Codex   Subagent  Consensus
  ──────────────────────────────────────────  ──────  ────────  ────────
  1. Empty visible capture must be non-destr  YES     YES       CONFIRMED CRITICAL
  2. P1.4 needs bounded wait + fallback       YES     YES       CONFIRMED CRITICAL
  3. Token consume order is broken (security) YES     —         CODEX-ONLY HIGH (single critical → flag)
  4. Lock contract must include token cache   YES     YES       CONFIRMED HIGH
  5. R1 long-question not pressure-tested     YES     YES       CONFIRMED HIGH
  6. P1.2 fingerprint match before overlay    YES     YES       CONFIRMED HIGH
  7. P1.5 (b) marker spoofable                —       YES       SUBAGENT-ONLY HIGH (single critical → flag)
  8. CB_ASK_REFRESH missing from PR A list    —       YES       SUBAGENT-ONLY MEDIUM (flag)
  9. ESC asymmetry carve-out                  —       YES       SUBAGENT-ONLY MEDIUM (flag)
  10. Extract assert_nav_dispatchable helper  —       YES       SUBAGENT-ONLY MEDIUM (flag)
  11. Two-phase capture perf gate             —       YES       SUBAGENT-ONLY MEDIUM (flag)
  12. Suggested merge order                   E→A→C→B→D  B→E→D→A→C  DISAGREE (taste)
═══════════════════════════════════════════════════════════════════════
```

Strong consensus on 6 critical/high blockers. 5 single-voice findings that should still flag (per "Single critical finding from one voice = flagged regardless"). One taste disagreement on merge order.

### Architecture ASCII diagram (existing dependencies)

```
┌─────────────────────────────────────────────────────────────────────┐
│                  Inbound (user → Claude)                            │
│                                                                      │
│  text_handler ─┐                                                    │
│  voice_handler ├──→ _apply_reply_context ──→ aggregator             │
│  photo_handler ┘    (stale-session guard — P1.5)                    │
│                                                                      │
│                                                                      │
│                  Interactive UI (Claude → user)                     │
│                                                                      │
│  session_monitor ── parses JSONL ── tool_use ──→ remember_ask_tool_input
│        │                                              │             │
│        │                                              ▼             │
│        │                                       _latest_ask_tool_input
│        │                                              │             │
│        ▼                                              ▼             │
│  status_polling ──── 1Hz poll ─────────→ handle_interactive_ui     │
│  (P1.4 race)                              ├── capture_pane (P1.3)  │
│                                            ├── resolve_ask_form    │
│                                            │   (P1.2)              │
│                                            ├── mint pick-tokens    │
│                                            │   (P2.2 leak)         │
│                                            └── send/edit Telegram  │
│                                                       │             │
│                                                       ▼             │
│  Telegram callback ── nav button (P1.1) OR pick (token validate)   │
│        │                                              │             │
│        ▼                                              ▼             │
│  send_keys to tmux                          consume_pick_token      │
│                                              (token order bug)     │
│                                                                      │
│  clear_interactive_msg (P2.1 lock drift, P2.2 missing prune)       │
└─────────────────────────────────────────────────────────────────────┘
```

### Test diagram (codepath → coverage)

| Codepath | Existing coverage | Gap | Test to add |
|---|---|---|---|
| Nav callback w/ live picker | yes | new guards | P1.1 happy-path test per nav button |
| Nav callback w/ empty visible capture | no | CRITICAL — risk of false-clear | Empty-capture test: assert non-destructive |
| Nav callback ESC w/ stale picker | partial | ESC must still clear | ESC carve-out test |
| `resolve_ask_form` review-screen + multi-Q | no | wrong-action bug | P1.2 fixture (constructed pane + JSONL) |
| `resolve_ask_form` drift case | no | risk of wrong-tab mint | Pane Q3 + JSONL Q1/Q2 → assert no overlay or manual-nav |
| `handle_interactive_ui` poller path w/ empty cache | no | starvation risk | P1.4 bounded-wait + fallback test |
| `handle_interactive_ui` long question off-visible | no | false-negative liveness | 80-line question fixture |
| Pick-token validate wrong user | no | CRITICAL — burn other user's token | Wrong-user-validates test |
| Pick-token consume race | no | concurrent callback double-dispatch | asyncio.gather test |
| `clear_interactive_msg` + concurrent mint | no | orphan / leaked token | Concurrent clear+mint test |
| Pick-token cache after `clear_interactive_msg` | no | P2.2 leak | Mint-clear-assert-gone test |
| Cross-PR: PR A guard ↔ PR D pruning | no | interaction risk | Mint → clear → tap nav → assert both fire |
| `_apply_reply_context` stale-session quote | partial | P1.5 silent drop | Pre/post-fix behaviour assertion |
| `_apply_reply_context` hostile quoted content | no | option (b) injection | `[quoted from previous session ...]` in quoted body |
| Voice handler reply to stale message | partial | same as text path | Add voice integration test |

### Test plan artifact path

Will be written to `~/.gstack/projects/etcircle-cc-telegram/test-plan-2026-05-15-p1-p2-followups.md` at gate-approval time.

### Decision Audit Trail (continued)

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 6 | Phase 3 | Dual voices ran; 6 critical blockers + 5 single-voice findings | Mechanical | P6 bias toward action | Both voices grounded in source; findings cite specific files/lines | N/A |
| 7 | Phase 3 | Token consume order security bug (Codex-only) → escalate to gate | Mechanical | P1 completeness | Single-voice critical = flag per skill rule | Silent acceptance |
| 8 | Phase 3 | P1.5 header marker spoofing (subagent-only) → escalate to gate | Mechanical | P1 completeness | Single-voice critical = flag | Silent acceptance |
| 9 | Phase 3 | Merge order disagreement → taste decision at gate | Taste | N/A (taste) | Both orderings defensible; user picks | Auto-pick one |

