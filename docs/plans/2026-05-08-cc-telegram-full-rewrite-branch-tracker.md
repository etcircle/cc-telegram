# CC Telegram full rewrite branch tracker

Branch: `dev/cc-telegram-full-rewrite`
Base: `main` / `origin/main` @ `5a55b9f`

## Scope shift from 2026-05-05 plan

The old plan was written at `07a220a` and ended with “Wave 0 + Wave 1 only”. This branch deliberately widens scope: do the full rewrite on an isolated dev branch, keeping only load-bearing migration affordances and current production fixes.

## Post-plan fixes that must survive the rewrite

- `1568589` — `/clear` silent-loss chain fix:
  - clear busy route when `session_id` flips,
  - empty-turn warning + synthetic `end_turn`,
  - stale reply-context guard for `/clear`-ed sessions.
- `93287bc` — configurable tool-call summary truncation. Rename `CCBOT_TOOL_SUMMARY_MAX_CHARS` to `CC_TELEGRAM_TOOL_SUMMARY_MAX_CHARS`.
- `fab8d7d` — reply-context applies to text, voice, photo, and document handlers. Preserve `_apply_reply_context` and media-group caption skip.
- `5a55b9f` — README already partially describes the post-2026-05-04 product; rewrite it as canonical `cc-telegram` docs, not fork narrative.

## Execution fence

Own:
- identity rename `ccbot` → `cctelegram`, script `cc-telegram`, config dir `~/.cc-telegram`, env prefix `CC_TELEGRAM_*`;
- `cc-telegram hook --install` rewrites legacy `ccbot hook` entries in place;
- `cc-telegram doctor [--migrate]` handles one-shot `~/.ccbot` → `~/.cc-telegram` state copy/preflight;
- delete dead topic-repair scaffold, stale translated READMEs, fork/upstream current-doc narrative, and non-load-bearing compatibility aliases;
- preserve load-bearing fallbacks: MarkdownV2 plain-text fallback, libtmux fallback, Telegram retry handling, current reply/session-loss fixes.

Do not own in this branch unless explicitly added later:
- new Telegram/product features;
- live tmux session migration;
- permanent `ccbot` console alias;
- source-history rewriting.

## Baseline

Command run on branch before edits:

```bash
uv sync --all-extras && \
uv run ruff check src/ tests/ && \
uv run ruff format --check src/ tests/ && \
uv run pyright src/ccbot/ && \
uv run pytest --tb=short -q
```

Result: `589 passed, 24 warnings`, ruff clean, format clean, pyright 0.

## Verification gates

After rewrite:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/cctelegram/
uv run pytest --tb=short -q
uv run cc-telegram --help
CC_TELEGRAM_DIR="$(mktemp -d)" uv run cc-telegram hook --install
uv run cc-telegram doctor
```

Identity scan expected to retain only explicit migration strings/tests for legacy `ccbot hook` and `~/.ccbot`.

## Current implementation result

Completed on `dev/cc-telegram-full-rewrite`:

- renamed package/runtime from `ccbot` to `cctelegram` with `cc-telegram` console script;
- moved default config/state dir to `~/.cc-telegram` and env prefix to `CC_TELEGRAM_*`;
- added `cc-telegram hook --install` with legacy `ccbot hook` rewrite;
- added `cc-telegram doctor [--migrate]` and bot-start preflight for `~/.ccbot` state;
- preserved post-plan fixes: reply context for all inbound media/text paths, configurable tool summary truncation, `/clear` stale-session guards;
- deleted unwired topic-repair scaffold and translated READMEs;
- rewrote README/CLAUDE/current architecture docs to canonical `cc-telegram` identity.

Verification:

```bash
uv run ruff format --check src/ tests/   # 65 files already formatted
uv run ruff check src/ tests/            # All checks passed
uv run pyright src/cctelegram/           # 0 errors
uv run pytest --tb=short -q              # 593 passed, 24 warnings
HOME=$(mktemp -d) CC_TELEGRAM_DIR=$(mktemp -d) uv run cc-telegram hook --install
HOME=$(mktemp -d) CC_TELEGRAM_DIR=$(mktemp -d) uv run cc-telegram doctor
```

Hook rewrite smoke also passed with a temp `~/.claude/settings.json` containing `ccbot hook`; installer rewrote it to an absolute `cc-telegram hook` command.
