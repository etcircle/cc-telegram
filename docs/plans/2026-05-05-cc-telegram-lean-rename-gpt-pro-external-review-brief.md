# CC Telegram lean rename GPT Pro external review brief

Why GPT Pro is being used: Emiliyan wants a principal-level second opinion before renaming `ccbot` identity to `cc-telegram` and deleting legacy/fallback/shim/migration baggage from the forked repo.

This is **not DI Copilot**. It is the `cc-telegram` repo at `/Users/felixcardix/dev-workspaces/cc-telegram`.

Bundle folder: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-05_122828-cc-telegram-lean-rename-review`
Upload zip: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-05_122828-cc-telegram-lean-rename-review/bundle.zip`
Prompt path: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-05_122828-cc-telegram-lean-rename-review/package/PROMPT.md`

Local baseline before submission:

- Commit: `07a220a`
- Status:
```text
## main...origin/main
?? .cgcignore
?? .claude/scheduled_tasks.lock
```
- `uv run ruff check src/ tests/`: passed
- `uv run pyright src/ccbot/`: 0 errors
- `uv run pytest -q`: 589 passed, 24 warnings

Core questions:

# Questions for ChatGPT Pro

1. Rename strategy: what should be renamed immediately from `ccbot` to `cc-telegram`/`cctelegram`, and what — if anything — should intentionally remain stable for user config or CLI ergonomics?
2. Legacy cleanup: identify old fork/upstream references, migration code, compatibility shims, feature-flag scaffolding, duplicate paths, or historical docs that should be deleted or collapsed. Be specific: cite files/lines and say keep/delete/refactor.
3. Runtime simplification: where is the architecture overcomplicated for the current topic-only product? Focus on `bot.py`, `message_queue.py`, `session.py`, `session_monitor.py`, `transcript_parser.py`, `busy_indicator.py`, and `tmux_manager.py`.
4. Performance/reliability: given this is a live Telegram↔Claude bridge with many concurrent topics, what cleanup would improve latency, queue behavior, state correctness, or restart safety without reducing features?
5. Migration policy: if the product goal is “lean, no old garbage,” what backwards-compatibility should be deliberately broken, and what minimal transition affordance is still worth keeping to avoid bricking the daily driver?
6. Implementation plan: propose staged waves with exact files/modules, safest order, rollback risks, and verification commands.
7. Testing: which existing tests are essential to preserve, which tests encode legacy behavior and should be rewritten/deleted, and what missing tests should be added before rename/cleanup?

