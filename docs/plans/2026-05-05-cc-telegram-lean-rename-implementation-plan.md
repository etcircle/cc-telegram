# CC Telegram lean rename — implementation plan

Status: draft for parent (Hermes) review. CC author: Claude Code agent in `/Users/felixcardix/dev-workspaces/cc-telegram` on `main` @ `07a220a`.

Inputs:

- External review: `~/.hermes/handoffs/chatgpt-pro/2026-05-05_122828-cc-telegram-lean-rename-review/RESPONSE.md`
- Brief: `docs/plans/2026-05-05-cc-telegram-lean-rename-gpt-pro-external-review-brief.md`
- Project rules: `CLAUDE.md`, `.claude/rules/architecture.md`, `.claude/rules/topic-architecture.md`, `.claude/rules/message-handling.md`

Tone: blunt, principal-level. Old garbage is called old garbage. No permanent compatibility shims; explicit one-time migration / preflight only.

---

## 1. Goal and non-goals

**Goal.** Turn the forked `ccbot`/`ccmux` repo into a clean `cc-telegram` product without losing the daily-driver Telegram ↔ Claude Code bridge mid-flight. One product name, one CLI, one config dir, one env prefix. No fallback identity. Operational ugliness that is load-bearing stays.

**In scope.**

- Identity rename: distribution `cc-telegram`, import package `cctelegram`, console script `cc-telegram`, log namespace `cctelegram`, config dir `~/.cc-telegram` (env override `CC_TELEGRAM_DIR`), env prefix `CC_TELEGRAM_*`, user-facing copy `CC Telegram`.
- One-shot, explicit migration paths: `cc-telegram hook --install` rewrites legacy `ccbot hook` entries; `cc-telegram doctor` (or equivalent preflight) reports/copies `~/.ccbot/*` state to `~/.cc-telegram/`.
- Delete old garbage: `topic_repair.py` scaffold, fork/upstream README narrative, translated READMEs, old window-name re-resolution, old session-map key migration, busy V1 flag, `CCBOT_AGGREGATOR_MAX_PHOTOS`, `thread_id_or_0`/General-topic compatibility, old history-callback payload format.
- Fix at-most-once delivery before deletion waves.
- Lock topic-only routing into `RouteKey(thread_id: int)` with no `or 0` fallbacks; key per-topic UI pending state by `(user_id, thread_id)`.
- Fix the documented subagent-digest race in `handlers/message_queue.py`.
- Modularize `bot.py` and `handlers/message_queue.py` after behavior is pinned.

**Non-goals.**

- New features. No new tools, no new transports, no new admin commands beyond what migration requires.
- Permanent backward compatibility. No `ccbot` console alias. No silent dual-read of `~/.ccbot` and `~/.cc-telegram`. No keeping `CCBOT_*` env vars past Wave 4.
- Rewriting load-bearing operational code: MarkdownV2 fallback, `split_message`, topic liveness probe, emergency DM, startup replay of open tools, tmux window-ID routing, group-chat-ID storage for forum topics, tmux direct-CLI hot path. Comments and naming may change; behavior does not.
- Switching tmux/process model, dropping libtmux fallback, or rewriting `tmux_manager.py`. Documented in Wave 6 as a future call.

---

## 2. Current repo truths and path normalization notes

**Repo layout (verified).** `pyproject.toml` lives at the repo root. Package source is `src/ccbot/`. Tests are `tests/ccbot/`. There is no `src/src/...` and no `src/tests/...`. The external review's `src/src/ccbot/...` and `src/tests/...` paths are bundle artifacts from the ChatGPT zip. Treat every `src/src/ccbot/` reference as `src/ccbot/` and every `src/tests/` reference as `tests/`.

**Confirmed identity surfaces still on `ccbot`.**

- `pyproject.toml`: `name = "ccbot"`, `[project.scripts] ccbot = "ccbot.main:main"`, `[tool.hatch.build.targets.wheel] packages = ["src/ccbot"]`, `[tool.coverage.run] source = ["ccbot"]`.
- `src/ccbot/utils.py`: `CCBOT_DIR_ENV = "CCBOT_DIR"`, `ccbot_dir()` defaults to `~/.ccbot`.
- `src/ccbot/hook.py`: `_HOOK_COMMAND_SUFFIX = "ccbot hook"`, `_find_ccbot_path()` shells out for `ccbot`, `_is_hook_installed()` treats legacy entries as already installed and returns 0 from `_install_hook()` without rewriting them. **Critical rename hazard.**
- `src/ccbot/main.py`: log namespace `"ccbot"`.
- `src/ccbot/config.py`: `CCBOT_*` env knobs throughout, including `CCBOT_AGGREGATOR_MAX_PHOTOS` alias and `CCBOT_BUSY_INDICATOR_V2` flag.
- `.env.example`, `scripts/restart.sh`, `.github/workflows/check.yml`, `README.md`, `README_CN.md`, `README_RU.md`, `doc/`, `docs/plans/`.

**Confirmed delete candidate.** `src/ccbot/handlers/topic_repair.py` exists and is unwired Stage 3 scaffold (env flag `CCBOT_TOPIC_REPAIR`, body is TODO). Test pin `tests/ccbot/handlers/test_topic_repair.py` exists and only asserts disabled/stub behavior.

**Path correction vs. the original CC ACP context.** `tests/ccbot/handlers/test_topic_send.py` *does* exist in this repo — the prior CC note saying it does not is wrong. Treat it as live and preserve it through the rename (keep its assertions about MarkdownV2 fallback / topic send outcomes; rename imports only).

**State files we keep by name.** `state.json`, `session_map.json`, `monitor_state.json`, `message_refs.db`. Generic, function-named, not fork identity. Only the directory they live in moves.

**Generic env vars that stay unchanged.** `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `TMUX_SESSION_NAME`, `CLAUDE_COMMAND`, `CLAUDE_CONFIG_DIR`, `OPENAI_*`, `MONITOR_POLL_INTERVAL`. They are operational, not identity.

**Default tmux session.** Currently `ccbot`. The lean default becomes `cc-telegram`, but operators on the daily driver can keep `TMUX_SESSION_NAME=ccbot` in `.env`. We ship a default; we don't migrate live tmux servers.

**Baseline (pre-Wave-1).** Per the brief: ruff clean, pyright 0 errors, pytest 589 passed. We do not move until Wave 0 reproduces this on the working tree.

---

## 3. Sequencing decision: what happens first and why

**Headline.** ChatGPT Pro recommends "rename now." We agree on direction and **refine the review's ordering** by sharpening one gate inside it: at-most-once delivery in `SessionMonitor` ↔ message queue (offsets advanced before Telegram delivery is acknowledged) plus the subagent digest race documented as a TODO in `handlers/message_queue.py` are the two highest-blast-radius hazards in this repo, and they both belong **before** any large deletion wave. Renaming first (small mechanical surface) and only then doing delivery-ack work is consistent with the review; the explicit refinement is "delivery ack is a hard gate before deletions start," not a different overall sequence.

**So the order is:**

1. **Wave 0** — pin the baseline. No edits. We don't fix what we can't reproduce.
2. **Wave 1** — rename identity, but **do not** delete runtime compatibility. Hook installer must rewrite legacy `ccbot hook` entries (this is non-negotiable: it is the only way new sessions keep writing `session_map.json` after the binary is gone). State migration is a one-shot `doctor` step, not silent dual-read.
3. **Wave 2** — fix delivery ack / replay / shutdown drain / subagent race. Behavior change, no deletions yet. This is the load-bearing wave; everything after assumes durable delivery.
4. **Wave 3** — enforce topic-only `RouteKey` with non-null `thread_id`, plus per-`(user_id, thread_id)` pending UI state. Reject General/private/group routing before state mutation.
5. **Wave 4** — *now* delete the migrations, scaffolds, and feature flags: name-based stale-ID re-resolution, `CCBOT_*` aliases, busy V1 flag, `topic_repair.py`, old callback payloads, old session-map keys.
6. **Wave 5** — extract `RouteState`/`RouteKey` modules, split `bot.py` and `message_queue.py`. No semantic change.
7. **Wave 6** — docs / workflows / `.claude/rules/` cleanup. README rewrite, kill stale translations, drop or keep Claude GitHub Actions per user call.

**Why this beats "rename then refactor."** Wave 1 leaves runtime untouched (other than identity strings and the hook rewrite). If Wave 2 finds delivery problems we did not anticipate, we have not yet deleted any compatibility surface, so backout is cheap. If we deleted compatibility first, a delivery hot-fix would have to reintroduce migration code under pressure.

**Why Wave 1 must not boil the ocean.** It touches a lot of files (every import, every CI ref, every README). It must not also try to fix routing, delete flags, or restructure modules. One concern per wave keeps reviewability sane and rollbacks small.

---

## 4. Wave plan with exact file fences and verification commands

Repo root in all commands: `/Users/felixcardix/dev-workspaces/cc-telegram`. All paths below are rooted there.

### Wave 0 — Pin baseline

**Touch:** none. Inventory only.

**Do:**

- Confirm `git status` matches the brief baseline: only `.claude/scheduled_tasks.lock` and `docs/plans/2026-05-05-cc-telegram-lean-rename-gpt-pro-external-review-brief.md` untracked.
- Reproduce green: ruff, ruff format check, pyright on `src/ccbot/`, pytest.
- Run identity inventory grep so we see where Wave 1 will touch.

**Verify:**

```
uv sync --all-extras
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/ccbot/
uv run pytest --tb=short -q
# Identity inventory — git grep over tracked paths (rg is not installed on this machine).
git grep -n -E '\bccbot\b|CCBot|CCBOT|\.ccbot|six-ddc|upstream|fork|legacy|compat|shim|migration|thread_id_or_0' \
   -- pyproject.toml src tests scripts .github README.md README_CN.md README_RU.md doc docs
```

If untracked files must also be scanned (e.g. local edits not yet `git add`-ed), fall back to a small Python `os.walk` scanner — do **not** rely on `rg`, which is absent from this environment.

**Exit:** ruff clean, ruff format clean, pyright 0, pytest matches brief (589 passed expected). No commits in Wave 0 — this is read-only.

### Wave 1 — Identity rename + hook/config migration only

**Fences (touch).**

- `pyproject.toml` — name → `cc-telegram`, script `cc-telegram = "cctelegram.main:main"`, wheel package `src/cctelegram`, coverage source `cctelegram`.
- `src/ccbot/` → `src/cctelegram/` (directory rename, all internal imports updated).
- `tests/ccbot/` → `tests/cctelegram/` (mirror rename, imports updated).
- `src/cctelegram/utils.py` — `CCBOT_DIR_ENV` → `CC_TELEGRAM_DIR`, default `~/.cc-telegram`. Rename helper to `app_dir()` (keep behavior identical).
- `src/cctelegram/main.py` — `logging.getLogger("ccbot")` → `"cctelegram"`. **CLI shape change (explicit).** Replace the current ad-hoc `sys.argv[1] == "hook"` dispatch with an `argparse` subcommand parser:
    - `cc-telegram` (no args) — start the bot. Backward operational behavior preserved.
    - `cc-telegram hook [...]` — delegate to `hook.hook_main()`. Includes `cc-telegram hook --install`.
    - `cc-telegram doctor [--migrate]` — delegate to `doctor.doctor_main()`. `--migrate` performs the one-shot `~/.ccbot` → `~/.cc-telegram` copy non-interactively; bare `doctor` reports status and prints the `cp -R` command.
    - `cc-telegram --help` / `-h` — argparse usage. Must not import `config`.
    - **Config-dir preflight scope.** The "refuse-to-start when `~/.ccbot` exists and `~/.cc-telegram` is missing" guard runs **only on the bot-start path** (no-arg invocation), not on `--help`, `hook`, or `doctor`. The preflight check is invoked after argparse dispatch decides we are starting the bot, before `config` is imported. `hook` and `doctor` must remain runnable when no `~/.cc-telegram` directory exists yet — that is the entire point of `doctor`.
- `src/cctelegram/doctor.py` — **new file**. Owns `doctor_main(argv: list[str] | None = None) -> int` and the preflight helper `preflight_or_exit() -> None` called from the bot-start path in `main.py`. Kept as a sibling of `main.py` (not under `handlers/`) because it is a CLI surface, not a Telegram handler. Test fixture: `tests/cctelegram/test_doctor.py` exercises `doctor_main` with `CC_TELEGRAM_DIR` and a fake `~/.ccbot` source pointed at via a monkey-patched `Path.home()` (or an injectable `home_dir` argument — see seam discussion in `hook.py` rule below).
- `src/cctelegram/config.py` — every `CCBOT_*` env var → `CC_TELEGRAM_*`. Default `TMUX_SESSION_NAME=cc-telegram`. *Keep* `CCBOT_AGGREGATOR_MAX_PHOTOS` / `CCBOT_BUSY_INDICATOR_V2` reading paths for now (deletion is Wave 4) — but rename their primary names to `CC_TELEGRAM_*` and treat any `CCBOT_*` read as an explicit one-shot deprecation log line, not silent equivalence past Wave 4.
- `src/cctelegram/hook.py` — see "critical rename rule" below.
- `src/cctelegram/bot.py` — `/start` copy "Claude Code Monitor" → "CC Telegram — Claude Code bridge". No other behavior changes.
- `.env.example` — `CCBOT_*` → `CC_TELEGRAM_*`; default `TMUX_SESSION_NAME=cc-telegram`.
- `scripts/restart.sh` — references to `ccbot` binary → `cc-telegram`.
- `.github/workflows/check.yml` — `pyright src/ccbot/` → `pyright src/cctelegram/`, ruff paths unchanged (`src/ tests/`).
- `CLAUDE.md` — update commands to `cc-telegram hook --install` and `pyright src/cctelegram/`.
- `.claude/rules/*.md` — only update the obvious identity references; full rewrite is Wave 6.
- New: `cc-telegram doctor` subcommand wiring (one-shot preflight). Implementation can be minimal: detect `~/.ccbot` exists, print exact `cp -R ~/.ccbot/. ~/.cc-telegram/` instructions, and refuse to start the bot until the user runs it or sets `CC_TELEGRAM_DIR` explicitly.

**Critical rename rule — `hook.py`.** Today `_is_hook_installed()` treats any `ccbot hook` entry as already installed (`src/ccbot/hook.py:59-78`) and `_install_hook()` returns 0 without writing (`src/ccbot/hook.py:99-107`). After this wave, the legacy form must be **rewritten in place**, not skipped:

- Detect any `SessionStart` hook command equal to `ccbot hook` or ending in `/ccbot hook`.
- Replace its `command` field with the resolved `cc-telegram hook` absolute path, preserve `type` and `timeout`.
- Treat that as a successful install on first call. Idempotent on second call.
- Add a regression test that starts with a settings file containing `ccbot hook` and asserts the rewrite.

**Testability seam (mandatory in Wave 1).** The current `hook.py` hardcodes `_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"`. Tests must not write to a real `~/.claude/settings.json`. Refactor as follows:

- `_install_hook(settings_file: Path = _CLAUDE_SETTINGS_FILE) -> int` — accept the path as a keyword argument with the production default. Tests pass a `tmp_path / "settings.json"`.
- `_is_hook_installed(settings: dict) -> Literal["current", "legacy", "missing"]` — return a richer status instead of a bare bool:
    - `"current"` — at least one entry whose `command` matches `cc-telegram hook` (bare or absolute path ending in `/cc-telegram hook`).
    - `"legacy"` — at least one entry whose `command` matches `ccbot hook` (bare or absolute) and no `current` entry. This drives the rewrite path.
    - `"missing"` — neither present. Drives the append path.
- The CLI surface (`hook_main` / argparse) wires `--install` to call `_install_hook(settings_file=_CLAUDE_SETTINGS_FILE)` so production behavior is unchanged; tests call `_install_hook(settings_file=tmp_path / "settings.json")` directly.
- Same seam pattern applies to `doctor.py`: `preflight_or_exit(legacy_dir: Path = Path.home() / ".ccbot", new_dir: Path | None = None)` (where `new_dir` defaults to `app_dir()`) so tests don't touch `$HOME`.

**Do NOT in Wave 1.**

- Do not delete `topic_repair.py`. Do not delete busy V1 flag. Do not delete `thread_id_or_0`. Do not change any routing, queue, or monitor semantics. Do not split `bot.py` or `message_queue.py`. Do not silently dual-read `~/.ccbot`. Do not add a `ccbot` shim console script.

**Verify:**

```
uv sync --all-extras
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
uv run cc-telegram --help                                       # must not require ~/.cc-telegram
CC_TELEGRAM_DIR="$(mktemp -d)" uv run cc-telegram hook --install # must not require ~/.cc-telegram
uv run cc-telegram doctor                                       # must run with no ~/.cc-telegram present
git grep -n -E '\bccbot\b|CCBot|CCBOT|\.ccbot' \
  -- pyproject.toml src tests scripts .github .env.example
```

Allowed remaining hits after Wave 1: explicit migration test fixtures, deprecation-log strings for `CCBOT_*` env reads, and historical doc text (Wave 6 cleans those).

**Wave 1 stop conditions (in addition to §8 generic rules).**

- Rewrite-of-legacy-hook unit test passes but a **live smoke test fails**: install the bot, run `cc-telegram hook --install` against a real `~/.claude/settings.json` (or an isolated copy), open a fresh Claude Code window inside the project tmux session, and confirm `~/.cc-telegram/session_map.json` (or `$CC_TELEGRAM_DIR/session_map.json`) gains a new entry within ~5 s. If it does not, **stop** — do not proceed to Wave 2 even if all unit tests pass. The hook rewrite is the load-bearing migration affordance; a green test suite without a green dry-run is not enough.
- `cc-telegram --help`, `cc-telegram hook --install`, or `cc-telegram doctor` errors out because of the bot-start preflight check leaking out of its scope. Fix the preflight scoping before continuing.

**Rollback:** revert directory rename + pyproject. State at `~/.ccbot` is untouched, so daily driver still works on the previous binary.

### Wave 2 — Delivery ack, shutdown drain, subagent race

**Fences (touch).**

- `src/cctelegram/session_monitor.py` — do not advance `session.last_byte_offset` / `monitor_state.json` until enqueued content has been **durably accepted**. Either make `enqueue_content_message()` return a future the monitor awaits before committing offsets, or split commit into two phases: read-and-stage, then commit-on-ack. Replay on restart from the last committed offset. Dedupe by `transcript_uuid` / `message_refs.db`.
- `src/cctelegram/bot.py` — `handle_new_message()` must mark user read offset only after the corresponding send has completed (or has been durably staged for replay), not immediately after enqueue.
- `src/cctelegram/handlers/message_queue.py`:
  - Bounded shutdown drain (configurable, e.g. 5 s default) before cancelling workers.
  - Remove the post-await `_subagent_msg_info[...] = state` writes; pre-bind in the producer, matching the todo/activity pattern. Add a shielded/locked `_schedule_subagent_flush()`.
- `src/cctelegram/monitor_state.py`, `src/cctelegram/message_refs.py` — extend dedupe to cover replay if needed.

**Behavior contract.** At-least-once with dedupe, never at-most-once. Duplicates after a crash are acceptable and visible (can be eyeballed by users); silent loss of assistant/tool output is not.

**Tests added (must exist before any deletion in Wave 4):**

- Crash mid-send: monitor staged content, process killed before Telegram ack, restart replays.
- Successful send: offset advances, no replay.
- Failed Telegram send (4xx/5xx): retried via existing `AIORateLimiter` semantics; offset only advances after final success or explicit emergency-DM acknowledgement.
- Shutdown: queue drain completes within bound; remaining items are recorded for replay, not dropped.
- Subagent digest race: producer replaces slot while a flush is awaiting; upsert does not clobber fresh state. (This is the `TestSubagentDigest` regression the file's TODO already names.)

**Verify:**

```
uv run pytest tests/cctelegram/test_session_monitor.py \
              tests/cctelegram/handlers/test_message_queue.py \
              tests/cctelegram/test_message_refs.py \
              tests/cctelegram/test_monitor_state.py \
              --tb=short -q
uv run pytest --tb=short -q
```

**Rollback:** revert this wave only; rename and identity are independent.

### Wave 3 — Topic-only `RouteKey`, no `thread_id_or_0`

**Fences (touch).**

- `src/cctelegram/handlers/message_queue.py` — introduce `RouteKey(user_id: int, thread_id: int, window_id: str)` (non-null `thread_id`). Replace `(user_id, thread_id_or_0, window_id)` everywhere. Delete `_route_for()`'s `None → 0` collapse. Sticky-DM normal delivery target removed.
- `src/cctelegram/session.py` — `set_group_chat_id()` requires `thread_id: int` (no Optional). Reject General/private chat IDs before mutating state. Remove `thread_id or 0` storage.
- `src/cctelegram/bot.py` — `text_handler()` and `forward_command_handler()` must reject non-topic messages **before** calling `set_group_chat_id()` or creating any route. Pending picker / directory-browser state moves into `PendingTopicState` keyed by `(user_id, thread_id)`. Callback handlers verify the topic match (and may carry a short topic token in callback data, ≤64 bytes).
- `src/cctelegram/handlers/{busy_indicator,status_polling,inbound_aggregator,attention,cleanup}.py` — propagate `RouteKey`; no compatibility passthroughs.
- Emergency DM stays as an exceptional path keyed by raw `user_id`. It is not a normal delivery sink and never becomes the default route.

**Behavior contract.** A General-topic or private-chat message reaching a topic-only handler is an immediate reject (with a brief user-visible reason if appropriate, no state mutation). The bot's contract is "1 topic = 1 window = 1 session"; this wave makes that compile-checkable.

**Tests added/modified.**

- Two concurrent topics, same user, both running directory browser: callback in topic A cannot consume topic B's pending text.
- General-topic message: rejected, no `set_group_chat_id`, no route.
- Existing `tests/cctelegram/test_session.py` cases that exercised `resolve_chat_id(..., None)` or "fallback to user_id when thread_id is None" — rewritten to assert rejection, not fallback.

**Verify:**

```
uv run pytest tests/cctelegram/test_forward_command.py \
              tests/cctelegram/test_kill_command.py \
              tests/cctelegram/test_session.py \
              tests/cctelegram/handlers/test_inbound_aggregator.py \
              tests/cctelegram/handlers/test_message_queue.py \
              tests/cctelegram/handlers/test_status_polling.py \
              --tb=short -q
uv run pytest --tb=short -q
```

### Wave 4 — Delete old migrations, flags, scaffolds

Pre-req: `cc-telegram doctor`/preflight from Wave 1 is shipped and tested. We do not delete migration code without a replacement migration affordance.

**Delete:**

- `src/cctelegram/handlers/topic_repair.py` and `tests/cctelegram/handlers/test_topic_repair.py`.
- `src/cctelegram/session.py`: `resolve_stale_ids()` name-based migration block, `_cleanup_old_format_session_map_keys()`, all "old-format `(session:window_name)`" handling, `thread_id or 0` group-chat storage.
- `src/cctelegram/session_monitor.py`: `_last_session_map` "transition" comment, old-format key acceptance in `_load_current_session_map()`. After this wave, non-window-ID keys are warned-and-ignored, not migrated.
- `src/cctelegram/hook.py`: `old_key` cleanup at lines around 273–279.
- `src/cctelegram/config.py`: `CCBOT_*` deprecation reads added in Wave 1, `CC_TELEGRAM_BUSY_INDICATOR_V2` flag, `CCBOT_AGGREGATOR_MAX_PHOTOS` alias.
- `src/cctelegram/handlers/message_queue.py`: legacy busy V1 renderer branch in `_render_activity_digest()`, `_session_id_for_window` local alias, `_looks_like_attention_request` backward-compatible alias.
- `src/cctelegram/bot.py`: old history callback payload format support (the no-byte-range branch).
- `src/cctelegram/handlers/callback_data.py`: any `# kept for backward compatibility` prefixes that are no longer in use.

**Tests:** remove tests pinning V1 busy behavior; remove `test_topic_repair.py`; remove tests asserting `thread_id_or_0` semantics; remove tests for old session-map key shapes.

**Decision deferred to Wave 4 review (not pre-decided here):** `Task` vs `Agent` tool naming in `transcript_parser.py` — keep both unless a live transcript sample proves `Task` is dead. If kept, drop "legacy Task" wording from comments/tests but keep the symmetric output handling. (See open questions §11.)

**Verify:**

```
uv run pytest --tb=short -q
git grep -n -E 'old-format|window_name keys|re-resolve|legacy|backward-compatible|compat|shim|migration|thread_id_or_0|CCBOT|ccbot|\.ccbot' \
  -- src tests pyproject.toml .github scripts
```

If untracked files must also be scanned (e.g. local edits not yet `git add`-ed), fall back to a small Python `os.walk` scanner — `rg` is not installed on this machine.

Expected remaining `legacy`/`fallback` hits: real load-bearing fallbacks (MarkdownV2 plain-text fallback, libtmux fallback in `tmux_manager.py`). Anything else is a bug.

### Wave 5 — Modularize `bot.py` and `message_queue.py` (no behavior changes)

**Fences (touch).**

- `src/cctelegram/bot.py` → split into `app.py` (lifecycle + `Application` setup), `commands.py` (`/start`, `/history`, `/screenshot`, `/esc`, `/forward`, `/kill`), `callbacks.py`, `inbound.py` (text/photo/voice handlers + topic guard), `binding.py` (window creation + topic ↔ window).
- `src/cctelegram/handlers/message_queue.py` → `route_queue.py`, `activity_digest.py`, `todo_digest.py`, `subagent_digest.py`, `topic_health.py`. Owner type: `RouteState` (per-route queue, worker, lock, inflight event, ephemeral slot, status/activity/todo/subagent handles). Optional shared module: `src/cctelegram/route.py` exporting `RouteKey`/`RouteState`.

**Behavior contract.** Move code; don't change semantics. Tests stay green at every commit. If a test must change, that's a sign the split changed behavior — back out and resplit.

**Verify:**

```
uv run ruff check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
```

### Wave 6 — Docs and workflows

**Fences (touch).**

- `README.md` — full rewrite as canonical `cc-telegram` docs. Drop the "What this fork adds on top of upstream" framing entirely. Drop pointers to historical plans.
- `README_CN.md`, `README_RU.md` — delete. They reference `six-ddc/ccmux`, `~/.ccbot`, `ccbot hook`. They can be regenerated later.
- `doc/telegram-bot-features.md` — delete or move under `docs/archive/`.
- `docs/plans/*` historical entries — archive under `docs/plans/archive/` or delete; current README must not point to historical plans.
- `.claude/rules/architecture.md`, `.claude/rules/topic-architecture.md`, `.claude/rules/message-handling.md` — update to reflect per-route queues, `~/.cc-telegram`, no name-based stale-ID re-resolution, `RouteKey(thread_id: int)`.
- `.github/workflows/claude-code-review.yml`, `.github/workflows/claude.yml` — **decision-pending in §11.** Default position: delete unless user wants them. They are not part of the Telegram bridge runtime and `claude-code-review.yml` uses `pull_request_target` with write permissions, which is a security surface worth reviewing.
- `.github/workflows/check.yml` — keep, already updated in Wave 1.

**Verify:**

```
uv run ruff check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
# Identity scan over current docs only — archive dirs (docs/archive/, docs/plans/archive/,
# any path segment named "archive") are intentionally excluded, since archived material is
# allowed to retain historical "ccbot"/"six-ddc"/"fork" wording. README.md and current docs
# must be clean. `git grep` lacks a clean exclude syntax for this; use a Python walker:
python3 - <<'PY'
import os, re, sys
roots = ["README.md", "doc", "docs", ".claude"]
pat = re.compile(r"ccbot|six-ddc|upstream|fork|CCBot|CCBOT")
hits = 0
for root in roots:
    if not os.path.exists(root):
        continue
    if os.path.isfile(root):
        targets = [(os.path.dirname(root) or ".", [], [os.path.basename(root)])]
    else:
        targets = os.walk(root)
    for dirpath, dirnames, filenames in targets:
        # Skip any path segment named "archive" (covers docs/archive, docs/plans/archive, etc.)
        dirnames[:] = [d for d in dirnames if d != "archive"]
        if "archive" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pat.search(line):
                            print(f"{p}:{i}:{line.rstrip()}")
                            hits += 1
            except OSError:
                pass
sys.exit(1 if hits else 0)
PY
```

---

## 5. Parent-owned seams vs safe CC/delegation targets

Parent (Hermes / Emiliyan) owns calls that change product contract or security posture:

- **Hook installer rewrite logic.** This is the only thing standing between rename and broken session tracking on the daily driver. Parent reviews the patch. Parent decides whether `cc-telegram hook --install` runs automatically post-Wave-1 install or is documented as a manual step.
- **`cc-telegram doctor` policy.** Refuse-to-start vs. warn-and-start when `~/.ccbot` exists and `~/.cc-telegram` does not. Default proposed: refuse-to-start with a one-line `cp -R` instruction.
- **`.github/workflows/claude*.yml` decision.** Keep, delete, or trim permissions. Parent call.
- **Default `TMUX_SESSION_NAME`.** Parent confirms `cc-telegram` is the new default; daily driver continues with explicit `.env` override.
- **Wave 2 ack-vs-replay design.** "Two-phase commit on offsets" vs "ack-future returned from enqueue" is an architectural choice. Parent picks the shape; CC implements.
- **Wave 4 deletion list final pass.** Especially `transcript_parser.py` `Task` vs `Agent` symmetry.
- **Wave 6 README rewrite.** Voice and product framing.

CC-safe targets (delegate freely after parent sign-off on shape):

- Mechanical rename of imports, env vars, log namespace, copy strings, CI/script paths.
- Directory rename `src/ccbot` → `src/cctelegram`, `tests/ccbot` → `tests/cctelegram` and import fix-ups.
- Wave 2 test scaffolding (crash-replay, drain, subagent race) once design is locked.
- `RouteKey` plumbing in Wave 3 (mechanical once shape is agreed).
- Wave 4 deletions once doctor/preflight has shipped and tests are green.
- Wave 5 module extraction (mechanical, with strict "tests stay green" rule).

---

## 6. Tests to add / delete / modify per wave

(File paths shown post-rename, i.e. `tests/cctelegram/...`. Pre-Wave-1 they are at `tests/ccbot/...`.)

**Wave 1 — add:**

- `tests/cctelegram/test_hook.py::test_install_rewrites_legacy_ccbot_hook` — settings file starts with `{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "ccbot hook"}]}]}}`; after `--install`, command is `cc-telegram hook` (or absolute path to it); idempotent on re-run.
- `tests/cctelegram/test_doctor.py` — `~/.ccbot` exists, `~/.cc-telegram` missing → doctor reports the exact migration command and exits non-zero from the bot start path.
- `tests/cctelegram/test_config.py` — `CC_TELEGRAM_*` env vars are read; `CCBOT_*` env vars produce a one-shot deprecation log and are still read (this assertion is removed in Wave 4).

**Wave 1 — modify:** every existing import; every test referencing `~/.ccbot`, `CCBOT_DIR`, `CCBOT_*` env names, `ccbot` log namespace, "Claude Code Monitor" copy. Mechanical.

**Wave 1 — keep as-is (rename only):** `test_markdown_v2.py`, `test_telegram_sender.py`, `tests/cctelegram/handlers/test_topic_send.py`, `test_session.py` (mostly), `test_session_monitor.py`, `test_message_queue.py`, `test_status_polling.py`, `test_status_polling_wave2.py`, `test_busy_indicator.py`, `test_inbound_aggregator.py`, `test_reply_context.py`, `test_message_refs.py`, `test_tmux_manager_direct.py`, `test_transcript_parser.py`.

**Wave 2 — add:**

- `test_session_monitor.py::test_offset_not_advanced_until_delivery_acked`
- `test_session_monitor.py::test_replay_after_restart_dedupes_via_message_refs`
- `test_message_queue.py::test_shutdown_drain_within_bound`
- `test_message_queue.py::test_shutdown_records_replay_state_for_undrained`
- `test_message_queue.py::TestSubagentDigest::test_producer_replace_during_flush_does_not_clobber`
- `test_message_queue.py::TestSubagentDigest::test_schedule_subagent_flush_is_shielded`

**Wave 3 — add:**

- `test_message_queue.py::test_route_key_thread_id_is_non_null` (type-level + runtime)
- `test_session.py::test_set_group_chat_id_rejects_none_thread_id`
- `test_bot.py::test_general_topic_message_is_rejected_before_state_mutation`
- `test_bot.py::test_pending_picker_state_is_per_topic` (two concurrent topics, same user)
- Modify: any `test_message_queue.py` case using `thread_id_or_0` → assert non-null `RouteKey.thread_id`.
- Modify: `test_session.py` cases asserting "resolve_chat_id falls back to user_id when thread_id is None" → assert rejection.

**Wave 4 — delete:**

- `tests/cctelegram/handlers/test_topic_repair.py` (entire file).
- Busy V1 tests: any `test_legacy_path_unchanged_when_flag_off`-style test in `test_busy_indicator.py` / `test_message_queue.py`.
- Tests asserting `_cleanup_old_format_session_map_keys` behavior.
- Tests asserting old history-callback payload acceptance.

**Wave 4 — modify:** drop "legacy" wording from `test_session_monitor.py` `NewMessage co-emission` tests; keep the test if `NewMessage` remains the content delivery adapter. Decide `Task` vs `Agent` tests (see §11).

**Wave 5 — none expected.** If tests need to move, the split changed behavior — stop and resplit.

**Wave 6 — none.** Doc-only.

---

## 7. Risk register

| Risk | Likelihood | Blast radius | Mitigation |
| --- | --- | --- | --- |
| Hook left pointing at deleted `ccbot` binary; new Claude sessions stop writing `session_map.json` and bot silently loses window→session mapping | High without mitigation | Severe (silent breakage of daily driver) | Wave 1 hook rewrite is mandatory and tested. Doctor reports if any `SessionStart` entry still says `ccbot hook` after install. Manual smoke: open a new Claude window, confirm `session_map.json` updates. |
| At-most-once delivery: monitor offset / user offset advances before Telegram ack; crash → permanent silent loss | Medium ongoing | Severe (assistant/tool output disappears) | Wave 2 ack/replay before any Wave 4 deletions. Dedupe via `transcript_uuid` + `message_refs.db`. Replay tests gate Wave 4. |
| Subagent digest clobber race (file-documented TODO) | Medium during long subagent runs | High (incorrect/missing progress on multi-minute tasks) | Wave 2 fix: pre-bind in producer, drop post-await writes, add shielded flush. Regression test before Wave 4. |
| Config dir migration: user runs new binary against `~/.ccbot` state (or vice versa) | Medium first run | High (perceived data loss; confusion) | Wave 1 doctor refuses to start when `~/.ccbot` exists and `~/.cc-telegram` does not, prints exact `cp -R` command. No silent dual-read. After confirmed migrate, doctor goes quiet. |
| Topic-only enforcement breaks normal topic flow accidentally | Low | High (bot stops responding to topics) | Wave 3 tests isolate General/private rejection from named-topic flow. Smoke test before merge: create topic, send text, photo, voice, /commands, /esc, kill window. |
| Per-`(user_id, thread_id)` pending state regression — callback in one topic clears another | Low post-fix | Medium (UI state leaks across topics) | Dedicated test in Wave 3. Token in callback data verifies topic match. |
| Stale-ID name re-resolution removed → re-binding tmux server changes invalidate routes | Low (tmux server restarts are rare) | Medium (user has to re-bind topics) | Documented behavior change. Doctor can list orphaned topic bindings. We deliberately do not silently rebind by name — that was the bug. |
| Delete `topic_repair.py` while emergency-DM path subtly depends on it | Very low (it's disabled by default) | Medium | Verify by grep there are no live imports outside test scaffolding. Emergency DM in `message_queue._emergency_dm` is independent. |
| Wave 5 module split introduces import cycles or changes init order, causing rare race | Medium during split | Medium | Strict "tests green per commit" rule. Any test failure → revert that commit, resplit. |
| `.github/workflows/claude*.yml` removal disables a workflow the user actually relies on | Unknown | Medium | Decision-pending; do not remove without parent confirmation. |
| ChatGPT bundle path artifacts (`src/src/...`) leak into the plan and CC follows them blindly | Medium without explicit norm | Low (build/test fail loudly) | §2 normalization note above; every wave references real repo paths. |

---

## 8. Stop / rollback criteria

**Per-wave stop conditions (any one → stop and escalate to parent):**

- ruff, ruff format check, pyright, or pytest regress vs. Wave 0 baseline (other than deliberately rewritten tests in that wave).
- Manual smoke: send a message in an existing topic and assistant output does not appear.
- New Claude session does not write `session_map.json` after Wave 1.
- Wave 2 crash-replay test fails or is non-deterministic.
- Wave 3 General-topic rejection test passes but a real-world named topic stops working.
- Wave 4 deletes anything that has not first been replaced or proven dead.
- Wave 5 forces a behavior-test change.

**Rollback by wave.**

- Wave 0: nothing to roll back.
- Wave 1: revert directory rename + pyproject + hook rewrite + doctor commit. `~/.ccbot` was untouched, daily driver returns to previous binary if reinstalled.
- Wave 2: revert delivery-ack commits. Pre-wave behavior was at-most-once but functional; we accept that until a fixed approach is ready.
- Wave 3: revert `RouteKey` change. `thread_id_or_0` behavior returns; some unintended General-topic routing returns with it. Acceptable transient state.
- Wave 4: revert deletion commits individually. Doctor stays.
- Wave 5: revert split. Module names go back to `bot.py`/`message_queue.py`.
- Wave 6: revert docs. Zero runtime impact.

**Forward-only rollback rule.** We do not roll back `~/.cc-telegram` state into `~/.ccbot` automatically. If the user needs to revert binaries post-migration, doctor prints the reverse `cp -R` line; the user runs it explicitly.

---

## 9. What NOT to do in Wave 1

This list exists because Wave 1 touches a lot of files and is tempting to "while we're in here…" creep.

- Do not delete `src/cctelegram/handlers/topic_repair.py` or its tests. (Wave 4.)
- Do not delete the busy V1 flag, V1 renderer branch, `CCBOT_AGGREGATOR_MAX_PHOTOS`, or any `# backward compatibility` aliases. (Wave 4.)
- Do not change routing semantics. `thread_id_or_0` stays. `RouteKey` is Wave 3.
- Do not silently dual-read `~/.ccbot` and `~/.cc-telegram`. The doctor command is the only legitimate touchpoint; runtime reads exactly one dir.
- Do not add a `ccbot` console-script alias. The migration affordance is the hook rewriter, not a permanent shim.
- Do not split `bot.py` or `message_queue.py`. (Wave 5.)
- Do not rewrite the README. (Wave 6.)
- Do not delete translated READMEs yet — they reference `ccbot`/`~/.ccbot`/`ccbot hook`, which is wrong, but Wave 6 owns docs and CC should not start picking docs to delete during a rename wave.
- Do not "fix" the at-most-once delivery on the side. Wave 2 owns it, with proper tests first.
- Do not change `TMUX_SESSION_NAME` in any user's running tmux server. The default in `.env.example` and config moves; live operators stay on their existing override until they choose to migrate.
- Do not re-resolve stale window IDs by name "while we're touching session.py." That is exactly the bug we plan to delete in Wave 4 — but only after we have confidence that bindings on the daily driver are window-ID clean.

---

## 10. Parent / CC review checklist

Use this when reviewing Wave 1 (and re-use shape for later waves):

- [ ] Wave 0 baseline reproduces locally: ruff, ruff format check, pyright, pytest (~589 passed).
- [ ] `git diff` for Wave 1 contains no behavior change to routing, queueing, or monitoring.
- [ ] `pyproject.toml`: name, script, wheel package, coverage source all read `cc-telegram` / `cctelegram`.
- [ ] `src/cctelegram/` exists and `src/ccbot/` does not. `tests/cctelegram/` exists and `tests/ccbot/` does not.
- [ ] `git grep -n -E '\bccbot\b|CCBot|CCBOT|\.ccbot' -- src tests pyproject.toml .github scripts .env.example` returns only deprecation-log strings and migration test fixtures.
- [ ] `cc-telegram --help` works.
- [ ] `cc-telegram hook --install` rewrites a settings file pre-populated with `ccbot hook`. Test exists and passes.
- [ ] `cc-telegram doctor` (or chosen name) blocks startup when `~/.ccbot` exists and `~/.cc-telegram` does not, prints the exact migration command. Test exists.
- [ ] Log lines now show namespace `cctelegram`.
- [ ] `/start` reply text says "CC Telegram", not "Claude Code Monitor".
- [ ] No new console-script aliases. No silent dual-read of config dirs.
- [ ] No deletion of `topic_repair.py`, busy V1 flag, callback aliases, or migration code.
- [ ] CI workflow `check.yml` passes on the rename branch.
- [ ] Manual smoke (live tmux + Telegram, daily driver): send text in a bound topic, receive assistant response; create a new topic and bind via directory browser; restart bot mid-run, no missed assistant output (this is at the limit of Wave 1; Wave 2 adds the durable guarantee).

---

## 11. Open questions

1. **Wave 2 design shape.** Two-phase commit on monitor/user offsets vs. enqueue-returns-future-with-ack. Both work. Parent picks; CC implements. Default lean: enqueue returns an `asyncio.Future` resolved on send completion; monitor awaits it before committing offsets. Less invasive on `monitor_state.json` shape; harder to reason about during shutdown drain.
2. **Doctor behavior.** Refuse-to-start vs. warn-and-start when `~/.ccbot` is present and `~/.cc-telegram` is missing. Default proposed: refuse-to-start. Confirm.
3. **`Task` vs `Agent` tool naming in `transcript_parser.py`.** Keep both with symmetric output handling (drop "legacy" wording), or drop `Task` entirely. Decision needs a live transcript sample. Default: keep both, drop wording, decide for real in Wave 4 review.
4. **`.github/workflows/claude-code-review.yml` and `claude.yml`.** Keep, trim permissions, or delete. Defaults to delete in Wave 6 unless user wants them; `claude-code-review.yml` runs `pull_request_target` with write perms which is a security surface worth not silently inheriting from upstream.
5. **Default `TMUX_SESSION_NAME=cc-telegram`.** Confirm we change the default. Daily driver keeps `TMUX_SESSION_NAME=ccbot` via `.env`. Confirm we are not migrating live tmux servers automatically (we are not).
6. **`tmux_manager.py` libtmux fallback + `shutil.which` monkey-patch.** Keep through this plan and revisit in a separate refactor. Confirm we are not pulling that into Wave 5.
7. **`NewMessage` adapter wording.** It's called legacy in some places but is still the content delivery adapter. Confirm we keep it (yes) and just clean wording.
8. **Migration of `~/.ccbot/state.json` keyed by window name (if any).** The brief assumes daily-driver state is window-ID clean. Doctor should probe this and refuse-to-start if it finds non-window-ID keys, since the post-Wave-4 runtime no longer rebinds by name. Confirm.
9. **`docs/plans/*` archival.** Default: archive, don't delete; keeps history searchable and keeps README/current docs clean. Identity-clean scans must exclude archive directories (`docs/archive/`, `docs/plans/archive/`, and any path segment named `archive`). README/current docs/.claude rules must be clean.

---

## 12. First implementation slice

Start with **Wave 0 + Wave 1 only**.

- Wave 0: reproduce baseline and inventory. No source changes.
- Wave 1: mechanical identity rename, hook rewrite, doctor/preflight, config-dir/env rename, CLI shape, and tests for those seams.
- Parent verifies the full Wave 1 checklist, including hook dry-run/new-session smoke, then commits before Wave 2 starts.
- CC/delegation may own the mechanical rename and test scaffolding, but parent owns the hook rewrite seam, doctor/preflight policy, and final smoke verdict.
- Explicitly out of the first slice: delivery ack changes, `RouteKey`/topic-only routing, source deletions, busy V1 deletion, `topic_repair.py` deletion, README rewrite, and module splitting.

This avoids the classic cleanup fuck-up: a rename wave quietly turning into routing refactor + delivery rewrite + docs purge in one giant unreviewable blob.

---

## CC initial verdict (for parent review)

The external review is right on substance: what to delete, what to keep, and which seams are dangerous. This plan sharpens the gates and makes delivery durability a hard pre-deletion requirement. It keeps the rename as Wave 1 (small mechanical surface, with the mandatory hook-rewrite affordance), inserts delivery-ack/shutdown-drain/subagent-race as Wave 2, then enforces topic-only `RouteKey` in Wave 3, *then* deletes compatibility in Wave 4. Modularization (Wave 5) and docs (Wave 6) come last. No permanent shims; one explicit `doctor`/preflight + one in-place hook rewrite are the only migration affordances.

Recommended initial verdict for parent: **approve Wave 0 + Wave 1 as the first implementation slice, with explicit confirmation on §11 questions 1 (delivery design shape), 2 (doctor refuse-vs-warn), and 4 (Claude GitHub workflows fate) before Wave 2 begins**.
