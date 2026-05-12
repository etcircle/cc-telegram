# cc-telegram post-GPT-Pro release blocker tracker

Created: 2026-05-12
Repo: `/Users/felixcardix/dev-workspaces/cc-telegram`
Branch: `main`
Starting HEAD: `6978351 docs: add merged main GPT review brief`
External response artifact: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-12_071931-cctelegram-merged-main-post-remediation-review/RESPONSE.md`

## Operating model

Every item gets:
1. Hermes implementation agent.
2. Parent targeted verification.
3. Independent Hermes peer reviewer.
4. Narrow fix loop if reviewer finds a blocker.
5. Parent commit before the next item.
6. Final full-suite verification + red-team.

No push without explicit user instruction.

## Items

### H1 — Picker callbacks stale-actionable after pending ownership is gone
- Severity: MUST FIX / release blocker
- Scope: `src/cctelegram/bot.py`, stale picker tests
- Expected behavior: every directory/session/window picker callback requires active expected picker state and matching `_pending_thread_id`. Missing owner is stale, not recoverable. Remove session fallback from callback topic.
- Verification: focused stale picker tests + pending route payload tests + pyright/ruff on touched files.

### H2 — Attention tokens survive dismiss/card replacement
- Severity: MUST FIX / release blocker
- Scope: `src/cctelegram/handlers/attention.py`, `src/cctelegram/bot.py`, attention callback tests
- Expected behavior: `dismiss()` and card replacement revoke stale tokens so delayed old yes/no/type cannot inject after typed reply or replacement.

### H3 — Pending first-turn payload silently lost on bind/create flush failure
- Severity: MUST FIX / release blocker
- Scope: `src/cctelegram/bot.py`, inbound aggregator tests/pending route tests
- Expected behavior: create/bind flush result is consumed; UI does not claim first-turn delivery on failure; cleanup/preservation behavior is explicit and tested.

### M1 — Test/CI config imports not hermetic
- Severity: MUST FIX before push/CI
- Scope: root/conftest and `.github/workflows/check.yml`
- Expected behavior: tests set dummy `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, and isolated `CC_TELEGRAM_DIR` before app config imports; CI does not depend on local `.env`.

### M2 — Topic close pending media cleanup gaps
- Severity: SHOULD FIX
- Scope: `src/cctelegram/bot.py`, `src/cctelegram/handlers/cleanup.py`, tests
- Expected behavior: topic close clears pending payload/files when pending topic matches, including unbound pending topics.

### M3 — Unbound photo/document captions lose reply context
- Severity: SHOULD FIX
- Scope: `src/cctelegram/bot.py`, pending route/media tests
- Expected behavior: reply context is applied before stashing unbound media captions, respecting media-group caption guard.

### M4 — Legacy hook detection too broad
- Severity: SHOULD FIX
- Scope: `src/cctelegram/hook.py`, hook tests
- Expected behavior: tighten legacy matching or document/test intentional substring behavior. Prefer tightening unless wrappers require substring behavior.

### M5 — Subagent digest clobber race TODO
- Severity: SHOULD FIX
- Scope: `src/cctelegram/handlers/message_queue.py`, message queue tests
- Expected behavior: remove post-await stale writes per TODO and add regression.

### M6 — Non-resume hook timeout can leave unmonitored tmux window
- Severity: SHOULD FIX
- Scope: `_create_and_bind_window()` in `src/cctelegram/bot.py`, tests
- Expected behavior: kill created tmux window or clearly surface cleanup when hook/session timeout fails before binding.

### L1 — Historical plan docs old naming
- Severity: LOW
- Scope: docs only
- Expected behavior: label dated `docs/plans` as historical context if necessary; do not rewrite history docs broadly.

### H-A — GPT Pro follow-up: pending owner can change after validation
- Severity: MUST FIX / release blocker
- Scope: `src/cctelegram/bot.py`, pending route payload tests
- Expected behavior: owner is revalidated after awaited work and before bind/flush; `_flush_pending_route_payload()` refuses to clear/replay if active pending owner no longer matches the target route; stale non-resume creates clean up the just-created tmux window.

### H-B — GPT Pro follow-up: consumed attention token can be resurrected
- Severity: MUST FIX / release blocker
- Scope: `src/cctelegram/bot.py`, `src/cctelegram/handlers/attention.py`, attention tests
- Expected behavior: a token consumed by an in-flight callback must not be rebound after typed reply, dismiss, or card replacement revokes/replaces the live attention generation.

### Real Telegram smoke
- Severity: release gate / blocked by config
- Expected behavior: safe local smoke is not enough. Manual/live Telegram smoke remains required before production release. If no token/config is present, record as blocked; do not fake it.

## Status log

- 2026-05-12: Tracker created from user-pasted GPT Pro response. CGC not available in this repo; using targeted file reads/searches and live tests.
- 2026-05-12: H1 implemented by Hermes agent. Parent targeted proof passed (`46 passed`, ruff clean, `pyright src/cctelegram/bot.py` clean). Independent Hermes reviewer returned PASS. Missing pending picker owner is now stale and session callbacks no longer recover ownership from callback topic.
- 2026-05-12: H2 implemented with two reviewer-driven fix loops. Parent targeted proof passed (`63 passed`, ruff clean, pyright clean). Independent Hermes final re-review returned PASS. Attention tokens are revoked on dismiss/card replacement and synchronously before bound-topic text awaits; same-current-card repeated notifications preserve their live token.
- 2026-05-12: H3 implemented with one reviewer-driven fix loop. Parent targeted proof passed (`53 passed`, ruff clean, pyright clean). Independent Hermes final re-review returned PASS. Pending first-turn replay now uses synchronous observable aggregator replay; failures/exception/split failures surface explicit resend UI and clean pending files/state.
- 2026-05-12: M1 implemented by Hermes agent. Parent stripped-env proof passed (`662 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Root test bootstrap and CI now force dummy config env / disabled dotenv with isolated config dir, while config integration tests re-enable dotenv intentionally.
- 2026-05-12: M2 implemented by Hermes agent. Parent targeted proof passed (`49 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Topic close now clears/deletes matching pending media in bound/unbound cases and preserves unrelated pending payloads.
- 2026-05-12: M3 implemented by Hermes agent. Parent targeted proof passed (`50 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Unbound photo/document captions now render Telegram reply context before pending stash while preserving media-group caption guards and bound behavior.
- 2026-05-12: M4 implemented by Hermes agent. Parent targeted proof passed (`32 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Legacy hook matching now uses exact/path-qualified managed-command semantics and preserves wrapper/comment/shell-chain mentions.
- 2026-05-12: M5 implemented by Hermes agent. Parent targeted proof passed (`82 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Subagent digest upserts no longer rebind stale state after awaited send/edit, matching activity/todo digest race pattern.
- 2026-05-12: M6 implemented by Hermes agent. Parent targeted proof passed (`65 passed`, ruff clean, pyright clean). Independent Hermes reviewer returned PASS. Non-resume hook/session registration timeout now avoids binding/forwarding, best-effort kills the just-created tmux window, surfaces cleanup status, and clears pending payload; resume behavior preserved.
- 2026-05-12: L1 implemented directly. Added `docs/plans/README.md` to label dated plan files as historical implementation context and point current operator guidance to root README / `doc/telegram-bot-features.md`.
- 2026-05-12: Follow-up GPT Pro review of current HEAD returned NOT READY with two high findings: H-A pending owner post-validation race and H-B consumed attention token resurrection race. H-A implemented by Hermes agent. Parent targeted proof passed (`68 passed`, ruff clean, pyright clean, diff-check clean). Independent Hermes reviewer returned PASS. Pending replay now has a final owner guard; create/bind paths revalidate owner after awaits; stale non-resume creates clean up their just-created tmux window.
- 2026-05-12: H-B implemented by Hermes agent. Parent targeted proof passed (`66 passed`, ruff clean, pyright clean, diff-check clean). Independent Hermes reviewer returned PASS. Attention callback entries now carry fingerprint/generation, revocation bumps live generation to invalidate consumed in-flight tokens, and rebind only restores tokens still matching the current waiting card/window/session generation.
