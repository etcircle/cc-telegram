# B2.0 rig — folder-trust Decision family keystroke transcript (CC v2.1.204)

Isolated rig capture, 2026-07-08. Production tmux sessions (`ccbot`, `auqcap`)
were never touched — the rig ran in a throwaway session `b2rig-<pid>` in a fresh
`mktemp -d` scratch dir, `CC_TELEGRAM_DIR` pointed at the scratch, and
`DISABLE_AUTOUPDATER=1` set to pin the version across instances.

**CC version:** `2.1.204 (Claude Code)`. `pane_current_command` reports the bare
version string `2.1.204` while `claude` runs (the Feature-A tap-gate assumption
holds on this version). NOTE: the plan (§1) characterized this family on
`2.1.201`; the live binary auto-updated `2.1.201 → 2.1.203 → 2.1.204` during the
session's lifetime, so this capture set + transcript is pinned to **2.1.204**.
The keystroke SEMANTICS below are UNCHANGED from the plan's `2.1.201` findings —
only the version number differs (the expected per-version re-characterization,
O-5).

## Prompt shape (unchanged from .200/.201)

Title line: `Accessing workspace:`
Options (exact ordered label tuple = the family signature):
  `("Yes, I trust this folder", "No, exit")`
Footer: `Enter to confirm · Esc to cancel`

Family signature for the `_DECISION_DISPATCH_TABLE` (§2b):
  - normalized title anchor: `^Accessing workspace:`
  - option-label tuple: `("Yes, I trust this folder", "No, exit")`

## Keystroke → observed outcome (E1/E2/E3)

- **E1: bare digit `2` COMMITTED immediately (claude exited).** With the trust
  prompt confirmed up and the cursor on option 1, sending the bare digit `2`
  selected "No, exit" and `claude` exited instantly (the tmux session died). Digit
  = select+commit, NO verify window. Bare digits stay FORBIDDEN in the dispatch
  lane (plan §0). MATCHES .201.
- **E2: arrow `Down`/`Up` MOVE the `❯` cursor WITHOUT committing.** From the
  cursor on option 1, `Down` moved `❯` to option 2 (`❯ 2. No, exit`), the prompt
  stayed LIVE (no commit); `Up` moved `❯` back to option 1 (`❯ 1. Yes, I trust
  this folder`), still live. Arrows are the licensed nav key for this
  family/version. MATCHES .201. (Fixtures: `_postdown_` = cursor on option 2,
  `_postup_` = cursor back on option 1.)
- **E3: `Enter` COMMITTED the cursored option.** With the cursor on option 1,
  `Enter` selected "Yes, I trust this folder" — the trust prompt disappeared and
  `claude` proceeded into its normal welcome/input UI (folder trusted). `Enter`
  is the version-stable commit key. MATCHES .201.
- **E4 (resume-from-summary family): BLOCKED — not characterized.** Not trivially
  raisable in a fresh scratch dir (needs a compacted, summary-bearing session).
  Per the plan (§1 E4) this family stays BLOCKED from the §2b dispatch table until
  its digit/arrow/Enter semantics are characterized on a named CC version. This
  is a table-entry gate, not a ship gate.

## Fixtures in this set (all CC v2.1.204, real `tmux capture-pane -p`)

- `decision_trust_folder_v2.1.204.txt`          — initial frame, `❯` on option 1
- `decision_trust_folder_postdown_v2.1.204.txt` — after one `Down`, `❯` on option 2 (E2)
- `decision_trust_folder_postup_v2.1.204.txt`   — after `Up`, `❯` back on option 1 (E2)

## Per-family dispatch-table-entry criteria (plan §1 — BINDING for B2.2)

A prompt family enters `_DECISION_DISPATCH_TABLE` (§2b) for a given CC version
ONLY with ALL THREE of the following, all captured on the SAME named CC version:

  (a) a **fixture set** — a real `capture-pane` of the live prompt (initial +
      arrow-moved frames);
  (b) an **arrow-move-only transcript** — proof that `Down`/`Up` move the `❯`
      cursor WITHOUT committing (the license to send arrows into this shape);
  (c) an **Enter-commits transcript** — proof that `Enter` commits the cursored
      option (the only commit key the lane ever sends).

NO-GO is decided **per family, per CC version** (never per feature). Every CC
upgrade EMPTIES the effective allowlist for a family until it is re-characterized
by this same rig ritual (§11-1 top risk; §14 O-5). The folder-trust family on
CC **2.1.204** satisfies (a)+(b)+(c) above and is table-eligible for `2.1.204`.
