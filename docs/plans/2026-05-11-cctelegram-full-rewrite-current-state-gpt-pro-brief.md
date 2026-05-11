# GPT Pro external review brief — cc-telegram full rewrite current state

Created: 2026-05-11
Branch: `dev/cc-telegram-full-rewrite`
HEAD: `4c3357a9ee4e5e9749b2a78cfc125360e488c274`

## Why this exists

Emiliyan asked for a ChatGPT Pro handoff bundle summarising where the `dev/cc-telegram-full-rewrite` branch currently stands and asking whether there are further things worth changing in cc-telegram before merge/release.

This is a local bundle only. It has not been submitted to ChatGPT unless a later `SUBMISSION.md` appears in the handoff folder.

## Bundle paths

- Handoff root: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review`
- Upload zip: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review/bundle.zip`
- Prompt: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review/package/PROMPT.md`
- Brief: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review/package/BRIEF.md`
- Questions: `/Users/felixcardix/.hermes/handoffs/chatgpt-pro/2026-05-11_163304-cctelegram-full-rewrite-current-state-review/package/QUESTIONS.md`

## Current status captured

- Branch clean at `4c3357a9ee4e5e9749b2a78cfc125360e488c274` when the bundle was created.
- `main..HEAD`: `3` commits ahead, `0` behind.
- Latest verification captured in the bundle:
  - `uv run pytest -q` → 610 passed, 24 warnings
  - `uv run ruff check src/ tests/` → clean
  - `uv run pyright src/cctelegram/` → clean

## Questions for GPT Pro

# Questions for GPT Pro

1. Is `dev/cc-telegram-full-rewrite` merge-ready as a lean full rewrite branch, or are there remaining blockers? Be blunt.
2. Did the rename/migration from `ccbot` to `cc-telegram` / `cctelegram` leave any dangerous compatibility holes, stale references, packaging issues, or user-state footguns?
3. Are the two hardening waves sufficient?
   - attention callback freshness / stale-token rejection;
   - pending unbound-topic media cleanup on cancel/stale/failure and non-forwarding on later bind.
4. Are there still ways for text/photo/document payloads, reply context, or attention callbacks to route to the wrong topic/window/session?
5. What tests are still missing that would catch realistic regressions before merge/release?
6. Which changes, if any, should be done before merging this branch to `main`, and which are safe to defer?
7. If you had one hour to improve the branch, what exactly would you change first?


## Submission note

If submitted later, use a fresh generic/plain ChatGPT Pro thread unless Emiliyan specifies a project. Do not submit this non-DI repo into the DI Consultants Copilot project.
