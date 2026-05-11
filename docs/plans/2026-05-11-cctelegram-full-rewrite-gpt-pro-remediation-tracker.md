# CCTelegram full-rewrite GPT Pro remediation tracker

Created: 2026-05-11
Branch: `dev/cc-telegram-full-rewrite`
Starting HEAD: `4c3357a Clean up pending media stale paths`
External review: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review/RESPONSE.md`

## Goal

Remediate every concrete GPT Pro finding before merge/release readiness, using a Hermes implementer agent and an independent Hermes peer-review agent for each item. Parent owns final verification, final red-team, and commits.

## Current dirty tree at start

- Untracked: `docs/plans/2026-05-11-cctelegram-full-rewrite-current-state-gpt-pro-brief.md`
- This tracker is also untracked until committed with the remediation wave.
- No source/test edits at tracker creation.

## Repo constraints

- Topic-only: `1 Telegram forum topic = 1 tmux window = 1 Claude session`.
- No permanent `ccbot` console alias; migration affordances only where load-bearing.
- Lean/no-shim fixes. Delete or harden stale paths; do not add broad compatibility layers.
- Commit only after implementer + reviewer + parent verification.

## Execution order

### Item 1 — High blocker: stale window-id callbacks

Scope:
- `src/cctelegram/bot.py`
- `src/cctelegram/session.py` only if needed for a helper
- Tests around screenshot / interactive callback / window picker behavior

Required behavior:
- Screenshot refresh callbacks must verify that the callback topic is still bound to the encoded `window_id` before capture/edit.
- Screenshot quick-key callbacks (`CB_KEYS_PREFIX`) must verify current topic binding before sending keys.
- Interactive UI callbacks (`CB_ASK_*`) must verify current topic binding before sending keys/refreshing UI.
- `CB_WIN_BIND` must re-check the selected window is still unbound at click time before binding it to the topic.

Required tests:
- stale screenshot refresh rejected after topic is rebound/unbound
- stale screenshot quick key rejected after topic is rebound/unbound
- stale interactive key rejected after topic is rebound/unbound
- window picker rejects a window that became bound after picker render

### Item 2 — Medium: photo/document cross-topic stale picker state

Scope:
- `src/cctelegram/bot.py`
- `tests/cctelegram/test_pending_route_payload.py` or adjacent focused tests

Required behavior:
- If Topic A has active picker state and Topic B sends unbound photo/document, clear Topic A picker state and pending payload before storing Topic B media.
- Topic B must get its own picker instead of having the old picker state treated as current.

Required tests:
- photo with previous `STATE_BROWSING_DIRECTORY`, `STATE_SELECTING_WINDOW`, `STATE_SELECTING_SESSION` from another thread clears stale state and opens picker
- document equivalent coverage

### Item 3 — Medium: attention false “Replied” on swallowed delivery failure

Scope:
- `src/cctelegram/bot.py`
- `src/cctelegram/handlers/inbound_aggregator.py` only if making delivery observable is cleaner than a pre-flight tmux liveness check
- `tests/cctelegram/test_attention_callback_handler.py`

Required behavior:
- Attention callback must not consume/edit as “Replied” if the target tmux window is no longer live or forced delivery fails.
- Lean acceptable first fix: check `tmux_manager.find_window_by_id(entry.window_id)` immediately before accepting/sending and reject stale if missing.
- Better fix allowed if small: forced aggregator flush returns delivery success.

Required test:
- when target window no longer exists / delivery cannot happen, callback alerts failure/stale and does not edit card to “Replied”.

### Item 4 — Medium: doctor migration both-dirs footgun

Scope:
- `src/cctelegram/doctor.py`
- `tests/cctelegram/test_doctor.py`
- `README.md`

Required behavior:
- If legacy `~/.ccbot` and target `~/.cc-telegram` both exist and `doctor --migrate` is requested, do not silently print OK.
- Either copy missing state files non-destructively while preserving existing `.env`, or fail loudly with exact manual command guidance.
- README existing-user migration guidance should come before “create ~/.cc-telegram/.env” or clearly warn existing users not to create the new dir before migration.

Required tests:
- both dirs + `--migrate` does not silently OK; expected state copy or explicit refusal behavior is asserted.

### Item 5 — Medium: mixed current + legacy hook rewrite

Scope:
- `src/cctelegram/hook.py`
- `tests/cctelegram/test_hook.py`

Required behavior:
- Hook installer rewrites/removes legacy `ccbot hook` entries even when a current `cc-telegram hook` entry already exists.
- No command containing `ccbot hook` remains after install.

Required test:
- settings file containing both current and legacy SessionStart hooks is cleaned so only cc-telegram hook command(s) remain.

### Item 6 — Low: stale public docs

Scope:
- `doc/telegram-bot-features.md`
- Maybe `README.md` if command docs need alignment

Required behavior:
- Public docs say `cc-telegram`, not `ccbot`, except historical/migration context.
- Slash command table reflects current runtime: forwarded Claude Code commands are `clear`, `compact`, `cost`, `model`; bot menu includes `start`, `history`, `screenshot`, `esc`, `kill`, `unbind`, `usage`.

Required verification:
- `rg -n "ccbot|/help|/memory|/context" doc/telegram-bot-features.md README.md CLAUDE.md src/cctelegram tests/cctelegram --glob '!**/__pycache__/**'` inspected for intentional results only.

## Per-item workflow

For each item:
1. Parent launches one Hermes implementation agent scoped to that item.
2. Parent checks actual repo movement and runs targeted tests.
3. Parent launches one independent Hermes review agent for that item.
4. If reviewer requests changes, parent launches a narrow fix agent or patches surgically, then reruns review.
5. Parent marks item PASS in this tracker only after reviewer PASS and parent verification.

## Final verification before commit

- `uv run pytest -q`
- `uv run ruff check src/ tests/`
- `uv run pyright src/cctelegram/`
- `git diff --check`
- Added-lines secret scan
- Final independent red-team over the full remediation diff

## Status log

- 2026-05-11: Tracker created. No item implementation started yet.
- 2026-05-11: Item 1 implemented by Hermes agent, parent targeted verification passed (`19 passed`, ruff clean, `pyright src/cctelegram/bot.py` clean), independent Hermes reviewer returned PASS. Committed as `9b38cef`.
- 2026-05-11: Item 2 implemented by Hermes agent, reviewer caught old replaced-topic callbacks deleting newer pending media, fix agent patched ignored-stale-thread handling, parent verification passed (`29 passed`, ruff clean, `pyright src/cctelegram/bot.py` clean), independent re-review returned PASS. Committed as `91923a5`.
- 2026-05-11: Item 3 implemented by Hermes agent via observable forced aggregator delivery result, parent verification passed (`35 passed`, ruff clean, `pyright src/cctelegram/bot.py src/cctelegram/handlers/inbound_aggregator.py` clean), independent Hermes reviewer returned PASS. Committed as `636da34`.
- 2026-05-11: Item 4 implemented by Hermes agent via loud `doctor --migrate` refusal when both legacy and target dirs exist plus README migration reorder, parent verification passed (`9 passed`, ruff clean, `pyright src/cctelegram/doctor.py` clean), independent Hermes reviewer returned PASS. Committed as `94353ef`.
- 2026-05-11: Item 5 implemented by Hermes agent by cleaning mixed current+legacy hook settings without duplicate current hooks, parent verification passed (`19 passed`, ruff clean, `pyright src/cctelegram/hook.py` clean), independent Hermes reviewer returned PASS. Committed as `fc723be`.
- 2026-05-11: Item 6 implemented by Hermes agent by updating public Telegram feature docs/slash-command tables, parent search/diff proof passed, independent Hermes reviewer returned PASS. Committed as `96981c2`.
- 2026-05-11: Final red-team over `4c3357a..HEAD` initially returned REQUEST_CHANGES for stale history pagination callbacks and text-initiated replacement old-callback deletion; narrow fix agent patched both, parent focused verification passed (`41 passed`, ruff clean, `pyright src/cctelegram/bot.py` clean), independent re-review returned PASS. Ready to freeze/commit.
