# AskUserQuestion multi-tab cards (one Telegram card per question) — v5

**Date:** 2026-05-15
**Branch target:** `main`
**Status:** Plan v5 — revised after fourth round of autoplan eng dual-voice review (v4 split verdict: Claude subagent PASS, Hermes FAIL). v4's three core fixes (FA5+ inference safety, lock-mutations-only contract, rerender_guard) were confirmed correct by both reviewers. v5 closes Hermes's remaining internal-consistency issues: INF encoding direction, step-7 cleanup wording matching the lock contract, resolver matrix wording explicitly stating no-pick-buttons-when-defaulted, fingerprint snippet INF line, PR 3 invariant checklist INF gate, risks line matching step 8 orphan-rollback contract, step 8 v4 label, rerender_guard sentinel + content-digest equality.
**Review history:** v1 FAIL/FAIL → v2 PASS/FAIL → v3 PASS/FAIL → v4 PASS/FAIL → v5 (this).

## Problem

`AskUserQuestion` tool calls from Claude Code can carry **multiple questions** (a multi-tab picker, e.g. D1..D5 each with several lettered options + ELI10 / Completeness / Recommendation context). Today the cc-telegram bridge renders only the **first** question, with **no per-option description**, so the user sees a tappable button row but cannot make an informed choice.

Concretely two regressions, both in `build_form_from_tool_input` (`src/cctelegram/terminal_parser.py:438`):

1. **L472:** `q = questions[0]` — drops `questions[1..n]`.
2. **L484-486:** reads `opt["label"]` only — silently drops `opt["description"]` (the per-option reasoning text Claude emits).

Visual proof: screenshot `~/.cc-telegram/images/1778845515_AQAD9w1rG6AuOFB-.jpg` — terminal shows D1..D5 with full descriptions; Telegram side rendered only the review-screen Submit/Cancel because that was the last sub-render and the prior tabs never produced a card.

## Goal

For a multi-question `AskUserQuestion`, post **one Telegram card per question-tab** (in order), each carrying that tab's question text + option list (with descriptions). Only the **currently-focused** tab's card is interactive (option-pick buttons + keystroke nav keyboard). On tab advance, **edit existing cards in place** — never re-post the full bundle.

Single-question forms behave exactly as today (one card, with buttons), plus per-option description text inlined.

## Non-goals

- No change to the keystroke-fallback navigation keyboard (Tab / arrows / Enter / Esc) other than what's described below.
- No new dispatch primitive — option buttons still mint a literal-N keystroke for the **current** tab. Users answer tab 1, the picker advances, then tab 2, etc. Mirrors Claude Code's TUI.
- No change to `ExitPlanMode` / Permission Prompt / RestoreCheckpoint paths.
- **No persistence across bot restart.** In-flight multi-tab cards become orphans on restart. Acceptable v1 limitation (matches today's single-card behavior); recovery work is its own follow-up.

---

## Four false assumptions in v1, and the v2 corrections

The autoplan eng review (Hermes + Claude subagent, both FAIL) caught these. v2 addresses each.

### FA1 — "Status poller re-renders interactive UIs on tab advance"

**v1 was wrong.** `src/cctelegram/handlers/status_polling.py:198-202` explicitly short-circuits while interactive mode is active: `if interactive_window == window_id and is_interactive_ui(pane): return`. No edit trigger fires from the poller. v1's "subsequent status-poller redraw" path was fictional.

**v2:** **callback-driven re-render.** Tab advance is *caused* by a keystroke the bot itself sent (button click → digit, or nav-keyboard arrow/Tab/Enter). After dispatching the keystroke, the bot's own callback handler schedules a re-render. Native-terminal tab navigation (user typing into the tmux pane directly) does **not** update cards — accepted limitation, called out in non-goals. Same caveat applies today for the single-card path.

Implementation surface:
- `bot.py:2912` (post-keystroke dispatch on a button click) and the nav-keyboard handlers (`bot.py` around the CB_ASK_TAB / CB_ASK_DOWN / CB_ASK_UP callbacks) already call `handle_interactive_ui` after sending the key. v2's contribution is making that call drive the multi-tab edit cycle, not the post path.

### FA2 — "`topic_edit` is per-route serialized, so concurrent renders order naturally"

**v1 was wrong.** `topic_edit` in `handlers/message_sender.py` calls `edit_message_text` directly. No per-route lock. Two `handle_interactive_ui` calls from different code paths (poller path post-dismissal, callback path, JSONL dispatch) can interleave card-edit operations and corrupt the per-card keyboard placement.

**v2:** explicit `asyncio.Lock` per `(user_id, thread_id_or_0)` ("interactive route lock") held across **all** interactive surface operations (post, edit, clear, token-cache invalidation). Single-card path acquires it too — small additional cost, large correctness gain.

### FA3 — "Fingerprint canonical change is byte-identical for single-question forms"

**v1 was wrong.** Appending `f"QS:{questions_digest}"` to `_canonical_repr` (`terminal_parser.py:319-327`) adds a sixth line even when `questions_digest == ""`. SHA-1 input differs → hash differs → callback tokens minted before deploy don't resolve against forms post-deploy.

**v2:** the `QS:` line is appended **only when `len(form.questions) > 1`**. Single-question forms produce the exact same 5-line canonical repr as today. Pinned in a golden test: `test_canonical_repr_single_question_unchanged` asserts a frozen SHA-1 against a fixture form.

### FA4 — "Pick-token validation already invalidates correctly on tab advance"

**v1 was wrong.** Callback validation at `bot.py:2896-2900` calls `parse_ask_user_question(pane)` only — **does not** use the JSONL resolver. v2's renderer mints fingerprints from a JSONL-aware form; the validator re-parses pane-only and gets a different shape (no `questions` matrix). Mismatch on every multi-tab click → "Form changed, refreshing" loop.

**v2:** introduce a single unified resolver `resolve_ask_form(tool_input, pane_text) -> AskUserQuestionForm | None` (`terminal_parser.py`). Both `handle_interactive_ui` (render) and the pick-token callback handler (validate) call this function. The validator's reparse is guaranteed to use the same JSONL overlay as the original mint.

**v3 sharpening (Hermes v2 concern):** the callback handler in `bot.py:2896` doesn't have `tool_input` in scope. v3 spec: the callback calls `_resolve_ask_tool_input(window_id, None)` (already public in `handlers/interactive_ui.py:87`) to fetch the cached JSONL payload, then passes it into `resolve_ask_form`. Same cache key the render path uses. Spelled out explicitly in §Resolver below.

### FA5+ — Safety: never dispatch when current-tab inference defaulted

**Hermes v3 surfaced a real safety hole** even after v3's title/option-matching mechanism: if both the **render** and **validate** paths fall through to `current_tab_idx = 0` due to pane corruption / scroll-up / partial redraw, the **fingerprints will match** (same wrong inputs to both) and the dispatch goes through — typing digit "1" against Claude Code's actual current tab (tab N, not tab 0). The user would answer tab N with tab 0's option 1.

**v4 fix:** track whether current-tab inference *succeeded* and propagate that into the fingerprint. New field on `AskUserQuestionForm`:

```python
current_tab_inferred: bool = True   # True means resolver matched a tab; False means defaulted to 0
```

Included in `_canonical_repr` only when `len(questions) > 1`:

```
INF:1   # current tab inferred (safe to dispatch)
INF:0   # defaulted to 0 (do NOT mint pick buttons)
```

Encoding matches the field semantics: `INF:1` = `current_tab_inferred=True`.

When `current_tab_inferred == False`:
- Cards render in full (descriptions visible, user can read everything).
- **No option-pick buttons.** Only the keystroke nav keyboard. The user must navigate manually via Tab/Arrow/Enter, which Claude Code's TUI handles correctly regardless of what tab we *think* we're on.
- A small footer line on the would-be-current card: "(Couldn't infer current tab — use keystroke nav.)"

This eliminates the wrong-dispatch path entirely. Trade-off: rare UX degradation in a corruption scenario, instead of a silent wrong answer.

### FA5 — "Pane parse exposes `is_current` per tab"

**v2 was wrong.** `_parse_tab_header` in `terminal_parser.py:340-372` explicitly sets `is_current=False` for every cell (L367) — the header line glyphs alone don't say which tab is being viewed. v2's resolver pulled the current-tab index from `tabs[i].is_current`, which is always False.

**v3:** infer current tab by **matching pane-visible content against the JSONL questions matrix.** Mechanism:

1. Run `parse_ask_user_question(pane_text)` → pane form. The pane form's `current_question_title` (parsed from the line directly above the visible options block in `terminal_parser.py:629-650`) and `options` set reflect the tab that's actually on screen.
2. Match the pane form's `current_question_title` against each JSONL `questions[i].question` / `questions[i].header`. Exact match = current tab.
3. If title matching is ambiguous or the pane title is truncated, secondary match: intersect pane options' labels against each JSONL question's option labels. Highest-overlap question wins.
4. If neither yields a match (pane corrupt, scroll-up, very early redraw), default `current_tab_idx = 0` AND set `current_tab_inferred = False`. All tabs render; **no option-pick buttons** are minted (see FA5+ safety rule). Only the keystroke nav keyboard is available — Claude Code's TUI handles arrow/Tab/Enter regardless of which tab we think we're on. The user can still see all questions and answer via keystrokes.

This is the actual mechanism Claude Code uses to convey current-tab focus through the pane — there's no glyph for it.

---

## Design (v2)

### Resolver — single source of truth

```python
def resolve_ask_form(
    tool_input: dict | None,
    pane_text: str,
) -> AskUserQuestionForm | None:
    """Unified AskUserQuestion form resolution used by render AND callback validate.

    Combines:
      - JSONL tool_input → full questions matrix (titles, options, descriptions)
      - pane_text → current tab focus (via title/option matching), cursor position,
        review-screen detection

    Returns None when neither source yields a parseable form.
    """
```

Behavior matrix:

| `tool_input`   | `pane_text` parse | Result |
|----------------|-------------------|--------|
| 1 question     | any               | Single-question form; pane provides cursor + free-text/review state. Fingerprint canonical = today's 5-line form. |
| ≥2 questions   | parses cleanly    | Multi-tab form. `current_tab_idx` resolved by matching pane's `current_question_title` against JSONL question titles (primary) or option-label intersection (secondary). |
| ≥2 questions   | None or partial   | Multi-tab form. `current_tab_idx = 0`, `current_tab_inferred = False`. All tabs render; **no card carries pick buttons** (FA5+). Only the keystroke nav keyboard is attached, on card 0. |
| missing/None   | parses cleanly    | Fall back to `parse_ask_user_question(pane_text)` — preserves the pane-only legacy path for sessions where the JSONL cache was lost (post-restart, etc.). |
| missing/None   | None              | Return `None`. Caller falls back to verbatim pane excerpt + keystroke-only keyboard. |

**Call site for callback validation:** `bot.py:2896` does NOT have `tool_input` in scope. v3 spec:

```python
# bot.py callback handler (pick-token path)
from .handlers.interactive_ui import _resolve_ask_tool_input
from .terminal_parser import resolve_ask_form

cached = _resolve_ask_tool_input(window_id, None)
current_form = resolve_ask_form(cached, pane) if pane else None
```

Same cache key (`window_id`) the render path uses → render and validate see byte-identical inputs.

### State model

```python
@dataclass
class _MultiTabSession:
    window_id: str
    shape_digest: str                    # sha1 over question titles + option labels + option counts
    message_ids: list[int]               # one per question tab, ordered; review card appended on handoff
    current_tab_idx: int                 # which card carries the keyboard right now
    generation: int                      # increments on cleanup — guards in-flight post/edit
```

**Shape digest (Hermes v2 concern):** v2 used titles + option counts only. If Claude redraws the same tab with the same option count but different labels/descriptions, v2 would miss the change and keep stale cards. v3 digest includes **question titles + per-question ordered option labels + option counts** so any reorder/rename triggers teardown. Descriptions are excluded (they can vary across redraws for cosmetic reasons and shouldn't force teardown).

Stored in `_multi_tab_sessions: dict[tuple[int, int], _MultiTabSession]` (key: `(user_id, thread_id_or_0)`), alongside today's `_interactive_msgs`. **Mutual exclusion:** at most one of `_interactive_msgs[key]` or `_multi_tab_sessions[key]` is set for a given route at any moment.

### Interactive route lock

```python
_route_locks: dict[tuple[int, int], asyncio.Lock] = {}

def _get_route_lock(user_id: int, thread_id: int | None) -> asyncio.Lock:
    key = (user_id, thread_id or 0)
    if key not in _route_locks:
        _route_locks[key] = asyncio.Lock()
    return _route_locks[key]
```

**Lock contract (v4 — must be implemented as written, Hermes v3 flagged earlier wording as contradictory):**

The lock is held around **state mutations** (any read or write of `_multi_tab_sessions`, `_interactive_msgs`, `_pick_token_cache`, `_pick_tokens`, `_latest_ask_tool_input`, `_interactive_mode`, `_interactive_msgs`). It is **released across `await topic_send` / `topic_edit_reply_markup` / `topic_delete`** calls — Telegram round-trips can take seconds and serializing them would stall multi-route concurrency.

The interleaving discipline:

```
acquire lock
  → read state, decide action
  → snapshot generation
release lock
  → await Telegram I/O
acquire lock
  → re-check generation == snapshot
  → if stale: rollback any side-effects from this I/O (delete orphan message); abort
  → else: commit result to state (append message_id, update current_tab_idx, etc.)
release lock
```

This applies to every awaited Telegram call inside `handle_interactive_ui` and `clear_interactive_msg`. The pick-token callback handler follows the same discipline plus the non-reentrant rule below.

**Non-reentrant rule (Hermes v2 concern):** the pick-token callback handler in `bot.py` MUST NOT hold the route lock across `await handle_interactive_ui(...)`. `asyncio.Lock` is non-reentrant; nesting deadlocks the task. Pattern:

```python
# pick-token callback handler — v4 contract
async with _get_route_lock(user_id, thread_id):
    # validate fingerprint, dispatch keystroke, mark token consumed
    # snapshot the JSONL cache state for the re-render guard:
    rerender_cache_snapshot = _resolve_ask_tool_input(window_id, None)
# Lock released. Schedule re-render outside the lock.
await handle_interactive_ui(
    bot, user_id, window_id, thread_id,
    rerender_guard=rerender_cache_snapshot,
)
```

**Re-render guard (Hermes v3 concern — lock-release-rerender race):** between releasing the lock at end-of-callback and entering `handle_interactive_ui`, a concurrent `clear_interactive_msg` (e.g. tool_result lands fast) can fire and clear `_latest_ask_tool_input[window_id]`. If `handle_interactive_ui` then re-resolves and re-renders, it would post an orphan card after the prompt has already advanced. Fix: `handle_interactive_ui` accepts a `rerender_guard` parameter carrying a content-digest snapshot taken before lock release.

```python
_NO_GUARD = object()   # explicit sentinel — distinguishable from None (which is a valid "cache cleared" value)

def _ask_tool_input_digest(payload: dict | None) -> str | None:
    """Stable content-digest of a cached AskUserQuestion tool_input. Used by rerender_guard
    so equality is content-based, not object-identity-based (the cache may return
    structurally-equal-but-distinct dicts across calls).
    """
    if payload is None:
        return None
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
```

Callback handler builds the guard before releasing the lock:

```python
async with _get_route_lock(user_id, thread_id):
    ...  # validate + dispatch + consume token
    guard = _ask_tool_input_digest(_resolve_ask_tool_input(window_id, None))
await handle_interactive_ui(bot, user_id, window_id, thread_id, rerender_guard=guard)
```

`handle_interactive_ui` (after acquiring its own lock on entry) computes the current digest and compares:

```python
if rerender_guard is not _NO_GUARD:
    current = _ask_tool_input_digest(_resolve_ask_tool_input(window_id, None))
    if current != rerender_guard:
        # Cache cleared (tool_result already landed) or replaced (new AskUserQuestion
        # arrived for same window with different tool_input). The world has moved on;
        # the monitor-driven render path will handle the new prompt.
        return False
```

Callers that genuinely want unconditional render (single-card path on first arrival via the monitor) pass the `_NO_GUARD` sentinel (default value of the parameter). `None` for `rerender_guard` means "guard against a present-tool-input that gets cleared", which is the callback-path semantics.

### Data flow

1. **Parse:** extend `AskOption` with `description: str = ""`. Add `AskQuestion(title, header, options)`. Extend `AskUserQuestionForm.questions: tuple[AskQuestion, ...] = ()`. Existing fields (`current_question_title`, `options`, `tabs`) keep describing the **current tab** for fingerprint stability.
2. **Build:** `resolve_ask_form` (new) — see above. `build_form_from_tool_input` becomes a helper used by it.
3. **Fingerprint (v4):**
   ```python
   def _canonical_repr(self) -> str:
       lines = [f"TABS:{...}", f"Q:{...}", f"OPTS:{...}", f"RVW:{...}", f"FT:{...}"]
       if len(self.questions) > 1:
           lines.append(f"QS:{questions_digest(self.questions)}")
           lines.append(f"INF:{'1' if self.current_tab_inferred else '0'}")
       return "\n".join(lines)
   ```
   Single-question forms: byte-identical to today. The `INF:` line only appears for multi-tab forms — its presence in the fingerprint means a `current_tab_inferred=False` re-resolve produces a *different* fingerprint than the inferred=True one, so a stale token from a moment of failed inference can't validate against a later successful inference (and vice versa).
4. **Render — first time:**
   - `len(form.questions) <= 1`: existing single-card flow + **inline option descriptions** under each label (PR 2 surface).
   - `len(form.questions) > 1`:
     - Compute `shape_digest` (titles + ordered option labels + counts).
     - For each question, render text card (header / question / numbered options each with capped description).
     - Post in order. If `form.current_tab_inferred == True`: only the card at `current_tab_idx` carries `reply_markup` (option-pick rows + keystroke nav). If `current_tab_inferred == False` (FA5+): card 0 carries only the **keystroke nav keyboard, no pick buttons**; all other cards have no markup.
     - Record `_MultiTabSession`. Clear `_interactive_msgs[key]` (mutual exclusion).
5. **Render — tab advance (callback-triggered, after the bot sends a keystroke):**
   - Reacquire route lock. Re-resolve form via `resolve_ask_form`.
   - If new form's `shape_digest` matches stored: same form, tab advance.
     - Determine `new_current_idx`. If equal to `current_tab_idx`, no-op.
     - Edit card `current_tab_idx`: `edit_message_reply_markup(reply_markup=None)` (keyboard-only API — body unchanged so no MESSAGE_NOT_MODIFIED on text).
     - Edit card `new_current_idx`: `edit_message_reply_markup(reply_markup=new_keyboard)` (or `reply_markup=keystroke_only` when `current_tab_inferred == False` after re-resolve).
     - Update `current_tab_idx`.
   - If shape digest changed (rare — Claude redrew with different questions): increment `generation`, delete all cards, drop the session, re-render from scratch.
6. **Review screen reached** (`is_review_screen=True` on the resolved form):
   - Strip keyboards from every per-tab card.
   - Post one new "Review your answers" card with Submit/Cancel buttons. Append its message_id to `message_ids`.
   - Future edits go to that card (it becomes `current_tab_idx`).
7. **Cleanup (`tool_result` lands, topic close, window kill, `forget_ask_tool_input`):** follows the v4 lock contract — mutations under lock, I/O outside.
   - Acquire route lock. Increment `generation` on the session (in-flight post/edit coroutines will fail their re-check on next reacquire). Snapshot `message_ids` into a local list. Drop `_multi_tab_sessions[key]` and `_interactive_msgs[key]`. Release lock.
   - Walk the snapshot list outside the lock → delete each via `topic_delete` (best-effort; failures logged, not raised).
   - **Predicate:** add `has_interactive_surface(user_id, thread_id) -> bool` checking either map. Callers in `bot.py:3107-3110`, `status_polling.py:210-212, 217-219` gate on this, not on `get_interactive_msg_id` alone.

8. **Generation guard — orphan rollback contract (Claude v2 concern, v4 lock-aware).** The generation counter alone is insufficient: a render coroutine suspended inside `await topic_send(card_3_of_5)` when cleanup bumps the generation will still complete the await and produce a `sent.message_id`. Without explicit rollback, that ID becomes an orphan card in chat with no owner. v4 contract (matches §Interactive route lock interleaving discipline):

   ```python
   async def _render_multi_tab(...) -> None:
       lock = _get_route_lock(user_id, thread_id)
       async with lock:
           session = _multi_tab_sessions.setdefault(key, _MultiTabSession(...))
           generation_at_entry = session.generation
       orphans: list[int] = []
       try:
           for q in form.questions:
               # Send outside the lock so cleanup can interrupt by bumping gen.
               sent = await topic_send(...)
               if sent is None:
                   continue
               async with lock:
                   current = _multi_tab_sessions.get(key)
                   if current is None or current.generation != generation_at_entry:
                       # Cleanup bumped gen between sends — abandon further posts,
                       # roll back any orphans this coroutine produced.
                       orphans.append(sent.message_id)
                       raise _RenderCancelled()
                   current.message_ids.append(sent.message_id)
       except _RenderCancelled:
           # best-effort delete orphans; failures logged, not raised
           for msg_id in orphans:
               await topic_delete(bot, ..., message_id=msg_id)
   ```

   Per-coroutine orphan list + `finally`-style rollback delete. Same pattern applies to the edit-on-advance path. The lock is released around the `await topic_send/topic_edit` itself — keeping it would serialize every Telegram round-trip and stall multi-route concurrency.

### Card layout (per question)

```
Q1 / 5 · Add clip affordance + clip metadata visibility

[question.question text, verbatim]

1. A — Top toolbar 'Add clip' + always-visible labels  (Recommended)
   [first ~250 chars of description, hard-truncated with …]

2. B — Contextual 'Add clip' button + hover labels
   [first ~250 chars of description]

…
```

- **Description cap: 250 chars per option.** Worst case 6 options × 250 + question (600) + tab strip (100) + labels (180) ≈ 2380 chars — comfortably under 4096 with slack for the question title and Telegram entity overhead.
- **Hard cap on total card body: 3800 chars** (matches `message_queue.py` merge limit). If a rendered card exceeds 3800, descriptions get shortened further until it fits. We do not split a card across multiple messages — splitting breaks the message_ids list invariant.
- **Pickable buttons: options 1-9 only.** Options 10+ render as text in the body but no button (literal "10" would be typed as 1+0 and dispatch wrong). Documented in the option-pick row builder.

### Keyboard movement API

Telegram's `editMessageReplyMarkup` is a distinct endpoint from `editMessageText`. v1 conflated them. v2 adds:

```python
# message_sender.py
async def topic_edit_reply_markup(
    bot: Bot,
    *,
    op: str,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str,
    message_id: int,
    reply_markup: InlineKeyboardMarkup | None,
) -> TopicSendOutcome:
    ...
```

Used exclusively by the multi-tab edit-on-advance path (body unchanged; only markup moves). MESSAGE_NOT_MODIFIED on the body never fires because we don't touch the body.

---

## Implementation order (v3)

v1's PR breakdown wasn't independently shippable (both reviewers flagged). v2 split out PR 4 (review handoff) as a follow-up. Hermes's v2 re-review flagged this as suspect: "multi-tab without review-screen handoff is not truly end-to-end shippable if users reach Submit/Cancel and cards don't transition cleanly." v3 folds PR 4 into PR 3.

- **PR 1 — Parser + resolver (safe, isolated):** `description` on `AskOption`, `AskQuestion`, `AskUserQuestionForm.questions`, `resolve_ask_form` (with current-tab inference via title/option matching), fingerprint gating on `len(questions) > 1`, golden test for single-question canonical repr. Pure parser-side. No caller changes. Behavior unchanged. **Independently shippable.**
- **PR 2 — Single-card descriptions + resolver wired everywhere:** `_render_ask_user_question` inlines descriptions for single-question forms (250-char cap). Wire `resolve_ask_form` into both `handle_interactive_ui` (render) and the pick-token callback validator (`bot.py:2896-2900`); callback fetches cached `tool_input` via `_resolve_ask_tool_input`. Caps card body at 3800. **Independently shippable.** Delivers the user's primary complaint (descriptions visible) for the single-question case.
- **PR 3 — Multi-tab end-to-end (ships atomically, includes review handoff):** introduces `_MultiTabSession`, `_route_locks` with the non-reentrant rule, `topic_edit_reply_markup`, `has_interactive_surface`, the post-N / edit-on-advance / shape-mutation-teardown / generation-guard-with-orphan-rollback logic, the **review-screen handoff** (detect `is_review_screen` → strip per-tab keyboards → post review card → route keyboard to it), and rewrites `clear_interactive_msg` to walk `message_ids`. Updates the option-pick row builder to cap at 9. **Single atomic PR.**

PRs 1+2 alone deliver the description-visible win for the single-question case. PR 3 delivers end-to-end multi-tab.

### PR 3 invariant checklist (Claude v2 concern — for reviewer aid)

Reviewer should verify each of these against the PR 3 diff:

1. **Mutual exclusion:** at most one of `_interactive_msgs[key]` or `_multi_tab_sessions[key]` is set for a given route at any moment. First-time multi-tab render clears `_interactive_msgs[key]`. Single-tab path does the reverse.
2. **Lock on every entry:** `handle_interactive_ui`, `clear_interactive_msg`, and every pick-token callback path acquire `_get_route_lock(user_id, thread_id)`. Pick-token callback releases the lock before `await handle_interactive_ui(...)` (non-reentrant rule).
3. **Cleanup walk on every exit:** every teardown path (tool_result, topic close, window kill, shape-mutation reset) walks `message_ids` via `topic_delete`. `has_interactive_surface` is the gate, not `get_interactive_msg_id`.
4. **Generation guard with orphan rollback:** every `await topic_send`/`topic_edit_reply_markup` inside the multi-tab render captures the result, re-acquires the lock, checks `generation_at_entry == current.generation`, and either appends or rolls back via `topic_delete`.
5. **Resolver parity:** render and pick-token callback both call `resolve_ask_form(_resolve_ask_tool_input(window_id, None), pane)`. No path uses `parse_ask_user_question` directly except as a fallback inside `resolve_ask_form`.
6. **Fingerprint gate:** `_canonical_repr` only appends `QS:` line when `len(questions) > 1`. Golden test pins the single-question hash.
7. **1-9 cap + inference gate:** `_build_pick_button_rows` skips options with `number > 9` (still rendered as text in the body). For multi-tab forms it also returns empty early when `form.current_tab_inferred == False` (FA5+: never mint pick buttons when current-tab inference defaulted). Single-question forms ignore `current_tab_inferred` (always True for them).

---

## Test plan

### Unit

- `resolve_ask_form` — 6 fixtures: missing tool_input (pane fallback), 1Q no description, 1Q with description, 3Q with descriptions + pane shows tab 1 / tab 2, malformed (missing `options`), review-screen pane.
- **`_canonical_repr` golden test** — fixture single-question form, frozen SHA-1 hex; the test fails loudly if anyone changes the canonical line set without bumping the golden.
- `_canonical_repr` multi-question — independent fixture, checks that `QS:` line appears and changes when titles/option-count changes.
- `_render_ask_user_question` single-question with descriptions (250-char cap exercised), card body capped at 3800.
- `_build_pick_button_rows` with 12 options — only 1-9 get buttons; 10-12 absent from markup, present in body text.
- `_MultiTabSession` state machine — post → tab-advance edit → review handoff → cleanup, all with stubbed `topic_send` / `topic_edit_reply_markup` / `topic_delete`. Verify generation guard rejects post-cleanup edits.
- `has_interactive_surface` returns True for both single-card and multi-tab sessions; False after cleanup.

### Concurrency

- Drive two `handle_interactive_ui` calls in parallel for the same route; assert exactly one set of cards is posted and the lock serializes them.
- Drive a button click + a `clear_interactive_msg` in parallel; assert the click either lands (and then cleanup walks the cards) or is rejected as stale (generation bumped).

### Integration (manual, live bot)

1. Trigger a 5-question `AskUserQuestion`. Verify 5 cards post in order with full descriptions; only Q1 has buttons.
2. Tap an option on Q1. Verify Q1's buttons disappear (no body re-edit, just markup), Q2's buttons appear. No new cards.
3. Advance through all tabs via Telegram buttons. Verify review card posts last with Submit/Cancel; per-tab cards have no buttons. Tap Submit. Verify Claude proceeds.
4. Same flow, but answer last tab via terminal native UI. Verify all per-tab + review cards get cleaned up by `clear_interactive_msg` walking the list.
5. Topic-close mid-flow: verify all cards deleted.
6. **6-option × 6-tab form (worst-case sizing):** verify all 36 options visible, no card exceeds 3800 chars, descriptions truncated as needed.
7. **>9-option question:** verify options 1-9 are tappable buttons; options 10+ render as text; user can still use keystroke nav keyboard to reach them.

---

## Risks & edge cases (v2)

- **Concurrent renders** — handled by route lock.
- **Bot restart mid-flow** — accepted limitation. In-memory state lost; next render after restart posts a fresh single card (no recovery). Documented in non-goals.
- **tool_result lands during multi-tab post** — generation guard with orphan rollback (§Data flow step 8): cleanup bumps generation under lock + snapshots+drops state; in-flight post's next reacquire sees generation mismatch, raises `_RenderCancelled`, the `except` walks its per-coroutine `orphans` list and deletes each via `topic_delete`. No reliance on a future "next render" sweep.
- **Shape mutation mid-flow** — `shape_digest` covers question titles + per-question ordered option labels + option counts; teardown + repost on mismatch.
- **Native-terminal tab navigation** — does not update cards. User sees the old current-tab buttons on a card that's no longer Claude's focus. If user taps it, the staleness check via `resolve_ask_form` will see the pane has shifted and reject with "Form changed, refreshing." Lossy UX but safe.
- **Description truncation cap** — 250 chars is data-driven by the worst-case sizing math above. If real-world descriptions commonly exceed 250 meaningfully, revisit with field data.

---

## Rollback

- PR 1: parser-only, zero behavior change. `git revert` is safe.
- PR 2: reverting restores option-label-only rendering for single-question forms and reverts callback validator to `parse_ask_user_question`. Safe.
- PR 3: reverting restores single-card behavior for multi-question forms (the original bug). Safe — multi-tab forms revert to today's "render Q1 only, no descriptions" until PR 3 is fixed and re-landed.

No data migration. No config flags.

---

## Review history

### v1 (autoplan eng dual voices) — FAIL / FAIL
Both Hermes and Claude subagent independently flagged four false assumptions (FA1-FA4) and several P2s. Key v1 → v2 deltas:
- v1 "subsequent status-poller redraw" → v2 callback-driven re-render.
- v1 implicit serialization claim → v2 explicit `asyncio.Lock` per route.
- v1 "byte-identical fingerprint" → v2 fingerprint gated on `len(questions) > 1` + golden test.
- v1 "existing token cache logic already works" → v2 unified `resolve_ask_form` for render + validate paths.
- v1 PR 3-5 sequence → v2 atomic PR 3 (multi-tab + lock + cleanup + 1-9 cap).
- v1 "split_message handles overflow" → v2 3800-char hard cap, no card splitting.
- v1 unbounded options → v2 1-9 pickable cap.

Raw v1 review outputs: `/tmp/autoplan-hermes-eng.txt` (Hermes) + agent transcript (Claude).

### v2 — Claude PASS-with-P2 / Hermes FAIL
Three new Hermes P1s (current-tab inference broken, lock reentrancy deadlock, callback JSONL access under-specified) + Claude P2 (generation-guard orphan rollback) + Claude P3 (resolver doc gap, PR 3 invariant checklist). v2 → v3 deltas:
- v2 "pane parse gives `is_current`" (false) → v3 current-tab inference via title/option matching against JSONL matrix (new FA5 section).
- v2 ambiguous lock contract → v3 explicit non-reentrant rule with lock released before `await handle_interactive_ui` in callback path.
- v2 callback JSONL access implicit → v3 explicit `_resolve_ask_tool_input(window_id, None)` call documented at the validator site.
- v2 generation counter alone → v3 generation + per-coroutine orphan list + rollback delete contract.
- v2 shape digest "titles + counts" → v3 "titles + option labels + counts" (catches label rename).
- v2 PR 4 separation → v3 PR 4 folded into PR 3 (review handoff atomic with multi-tab).
- v2 resolver behavior on JSONL+failed-pane undocumented → v3 behavior matrix table.
- v2 PR 3 invariant list scattered → v3 explicit reviewer checklist subsection.

Raw v2 review outputs: `/tmp/autoplan-hermes-eng-v2.txt` (Hermes) + agent transcript (Claude).

### v3 — Claude PASS / Hermes FAIL
Hermes flagged three remaining issues: (1) title-matching wrong-dispatch safety when both render+validate default to tab 0 with same corrupt pane → fingerprint parity holds, digit dispatched to wrong tab; (2) lock contract internally contradictory (§Interactive route lock said "held around all post/edit work" but §Data flow step 8 said sends outside lock); (3) lock-release-before-rerender race not fully closed (cleanup can fire between release and re-acquire; re-render posts orphan after prompt advanced). Plus two housekeeping nits (rollback section mentioned PR 4 still; risk section shape-digest line out of sync). Claude returned PASS with 5 minor doc nits (orphan rollback `finally` semantics, public-rename of `_resolve_ask_tool_input`, shape-digest encoding, Telegram-outage during cleanup, missing test fixture for identical-options-across-tabs).

v3 → v4 deltas:
- v3 "default current_tab_idx=0 = lossy UX but safe" (false — same default at validate yields fingerprint parity → wrong dispatch) → v4 FA5+ section: track `current_tab_inferred: bool`, gate option-pick buttons on `True`, fall back to keystroke-only nav when defaulted. Eliminates wrong-dispatch.
- v3 ambiguous lock contract → v4 explicit "lock around state mutations only, released across Telegram I/O" with explicit interleaving discipline.
- v3 no re-render guard → v4 `rerender_guard` parameter on `handle_interactive_ui` carrying a snapshot of `_resolve_ask_tool_input(window_id, None)` from before lock-release; abort re-render if cache cleared mid-window.
- v3 rollback section mentioned PR 4 → removed.
- v3 risk-section shape-digest line said "titles + counts only" → updated to match v3 digest (titles + ordered labels + counts).

Raw v3 review outputs: `/tmp/autoplan-hermes-eng-v3.txt` (Hermes) + agent transcript (Claude).
