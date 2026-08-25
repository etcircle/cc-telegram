# GH #72 — folder-trust Decision family keystroke transcript (CC v2.1.246)

Isolated rig capture, 2026-08-25. Private socket `tmux -L ccrig`, session `rig`,
geometry **160x50**, scratch `CC_TELEGRAM_DIR`, a FRESH throwaway cwd per E-run.
The production tmux server (`ccbot`) and the real `~/.cc-telegram` were never
touched.

**CC version: `2.1.246 (Claude Code)` — verified BEFORE and AFTER the battery,
identical** (`DISABLE_AUTOUPDATER=1` on every rig invocation). The `meta` record
lives with the rig evidence in the scratch dir (`meta_v2.1.246.txt`), mirroring
the 2.1.239 / 2.1.241 practice of not committing it.

Binary — ISOLATED npm prefix, NOT on `PATH` (same shape as the 2.1.241 rig):

```
npm install -g --prefix <scratch>/cc246 @anthropic-ai/claude-code@2.1.246
<scratch>/cc246/bin/claude -> ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
```

Launch shape (mirrors `tmux_manager._compose_launch_command`, full path to the
isolated binary, valid minimal `--settings`):

```
cd <fresh-dir> && CC_TELEGRAM_DIR=<scratch>/ccdir246 DISABLE_AUTOUPDATER=1 \
  '<scratch>/cc246/bin/claude' --dangerously-skip-permissions --settings <scratch>/md_settings.json
```

**Isolation VERIFIED**: SessionStart wrote `<scratch>/ccdir246/session_map.json`
key `rig:@4`; the real `~/.cc-telegram/session_map.json` sha1 was unchanged
(`efed5eeac20e968564f3c0c3ce444ebe6f9722e1`) before and after the whole rig.

## Prompt shape — IDENTICAL to 2.1.241 *and* 2.1.239

Title line: `Accessing workspace:`
Options: `("Yes, I trust this folder", "No, exit")`
Footer: `Enter to confirm · Esc to cancel`

A `diff` of the 2.1.246 arrival frame against BOTH
`folder_trust_arrival_plain_v2.1.241.txt` and
`folder_trust_arrival_plain_v2.1.239.txt`, with the (differing) echoed
launch-command lines and the cwd paths stripped, is **empty** — the prompt block
is byte-identical across all three versions, including the security prose and
the `Security guide` link row.

### ANSI styling (recorded exactly, from `e0_arrival_ansi`)

The cursored row is:

```
 ESC[38;5;153m❯ESC[39m ESC[38;5;246m1.ESC[39m ESC[38;5;153mYes,ESC[39m … ESC[38;5;153mfolderESC[39m
```

- `❯` glyph and **every label word** of the cursored row: `ESC[38;5;153m`.
- The `N.` NUMBER of **both** rows: `ESC[38;5;246m` (the same grey as the
  footer), and the non-cursored row's LABEL is unstyled.
- Footer, word by word: `ESC[38;5;246m`.
- Title words: `ESC[1m` + `ESC[38;5;220m`; the top rule: `ESC[38;5;220m`.
- **No SGR-2 dim (`ESC[2m`) anywhere on this surface** — matching the 2.1.239 /
  2.1.241 finding, which is what keeps the free-text DIM landing proof
  unreachable here.

**Honest discrepancy against the prose of the earlier docs:** the 2.1.239 and
2.1.241 keystroke docs both state that the `N.` number of the cursored row is
`ESC[38;5;153m`. On 2.1.246 it is measurably `ESC[38;5;246m`. **No ANSI
folder-trust fixture was ever committed for .239/.241**, so this cannot be
byte-diffed against them and it is NOT claimed as a version change — it may
equally be an imprecision in the earlier prose. It is recorded rather than
smoothed over. It is behaviorally inert either way: `parse_generic_decision`
and the whole `dcp:`/`tst:` chain read the PLAIN capture, and no production code
keys on `38;5;153` / `38;5;246` for this surface (verified by grep).

## ⚠ RIG ANOMALY (carried over, already filed as #69) — `pane_current_command` is `claude.exe`

On the isolated npm install, `pane_current_command` while the TUI runs reports
**`claude.exe`** (the npm launcher's filename), at arrival, at T+60 s and at
T+90 s — not the bare version string the native installer produces. Root cause
is the BINARY FILENAME, not the CC version, exactly as recorded in the 2.1.241
doc. `tmux_manager.pane_command_is_claude("claude.exe")` is False, so an
npm-installed Claude Code on macOS is refused by the GH #50 delivery gate —
already filed as **#69**, out of scope here.

**Consequence for THIS characterization (disclosed):** the `pane_current_command`
VERSION-STRING shape for 2.1.246 was **NOT observed** in this rig. The trust
lane's licensing input is the nonce-delimited `--version` PROBE, which WAS
verified on this exact binary (V1 below), so the #65 licensing path is covered.

## Keystroke → observed outcome (E0–E4)

- **E0 arrival + PERSISTENCE: prompt renders ~2 s after launch and persists
  BYTE-IDENTICALLY at T+60 s and T+90 s** (`diff` empty against both);
  `pane_current_command` is `claude.exe` at all three. No self-advance, no
  drift. Fixture: `folder_trust_arrival_plain_v2.1.246.txt`.

- **E2 arrows MOVE the `❯` cursor WITHOUT committing — and they WRAP, not
  clamp.** From `❯ 1`: `Down` → `❯ 2`; a SECOND `Down` → back to `❯ 1`
  (**WRAP**); `Up` → `❯ 2`; a second `Up` → `❯ 1`. The footer was present and
  `pane_current_command` was still `claude.exe` on all four frames — the prompt
  stayed LIVE throughout. Fixtures: `folder_trust_postdown_plain_v2.1.246.txt`,
  `folder_trust_postdown2_plain_v2.1.246.txt`,
  `folder_trust_postup_plain_v2.1.246.txt`. Identical to 2.1.239 / 2.1.241, and
  the same DIVERGENCE from the AUQ-picker clamp.

- **E3 `Enter` COMMITS the cursored option.** With `❯` on option 1, `Enter`
  selected "Yes, I trust this folder": the trust prompt was gone at T+1 s and
  the normal welcome/REPL was already painted at T+1 s (so 2.1.246 behaves like
  2.1.239 here, NOT like the 2.1.241 blank transitional frame — the blank frame
  remains a legitimate state that must not classify as failure, it simply did
  not occur on this run). SessionStart registered `rig:@4` in the SCRATCH
  `session_map.json`. **The post-Enter REPL frames are deliberately NOT
  committed as fixtures** — see "Side observation" below for why.

- **E2c compound (navigate → verify → Enter):** on a still-live prompt, `Down`
  moved `❯` to option 2, the verify frame confirmed `❯ 2. No, exit`, and `Enter`
  committed **option 2** — `claude` exited (`pane_current_command` `zsh`).
  Proof that `Enter` commits the CURSORED option, not a fixed default.
  Fixture: `folder_trust_e2c_navto2_plain_v2.1.246.txt`.

- **E1 bare digit `2` COMMITS immediately — NO verify window.** With `❯` on
  option 1, the literal `2` (no Enter) committed "No, exit";
  `pane_current_command` was already `zsh` at T+0.5 s and at T+2 s. Digits stay
  FORBIDDEN. Fixture: `folder_trust_postdigit2_t2_plain_v2.1.246.txt`.

- **E4 `Escape` CANCELS — `claude` exits to a bare shell** (`claude.exe` → `zsh`
  within 1 s, still `zsh` at T+4 s). Fixture:
  `folder_trust_postesc_t4_plain_v2.1.246.txt`.

- **V1 version probe (GH #65 Fix 0) — WORKS on the isolated binary.**
  `printf 'CCTGVERA246\n'; DISABLE_AUTOUPDATER=1 '<full-path>' --version;
  printf 'CCTGVERB246\n'` produced on separate pane lines: `CCTGVERA246` /
  `2.1.246 (Claude Code)` / `CCTGVERB246`
  (`version_probe_plain_v2.1.246.txt`). The 2.1.241 parser caveat reproduces
  EXACTLY: the echoed command line is long enough to WRAP, so the echo puts
  `CCTGVERA246` at the end of one pane line and `CCTGVERB246` at the end of the
  NEXT. The shipped rule — delimiter lines must be an EXACT whole-line
  FULLMATCH of the nonce after strip — is what makes this parse correctly; the
  echoed lines never fullmatch, the real `printf` output lines always do.

## POST-COMMIT / POST-CANCEL PANE HAZARD (same as 2.1.239 / 2.1.241)

After the digit commit (E1), after the E2c `Enter`-on-option-2 commit, and after
`Escape` (E4), `claude` exits but the trust prompt text — `❯` on an option row
and the `Enter to confirm · Esc to cancel` footer — REMAINS on the pane, with
only the shell prompt appended below. A text-only liveness check false-positives
on a dead pane. Key trust-card liveness/teardown on the PROCESS
(`pane_current_command`), never on pane text. Unchanged.

## ⛔ Side observation — a REAL GH #62-class status-bar drift on 2.1.246

**NOT acted on in this wave (folder-trust only). It needs its own
characterization + issue + fix.**

The post-trust REPL frame captured by E3 carries a status bar with vocabulary
the GH #62 (2.1.238) alphabet derivation does not know:

```
⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents                    /rc connecting…
```

Measured against the shipped predicate (read-only probe, nothing changed):

| row | `terminal_parser._is_status_row` |
|---|---|
| `⏵⏵ bypass permissions on` | True |
| `⏵⏵ bypass permissions on (shift+tab to cycle)` | True |
| `⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents` | True |
| the same row **plus** the right-aligned `/rc connecting…` | **False** |
| `/rc connecting…` alone | **False** |
| `/rc active` alone | **False** |

So the culprit is a NEW right-hand status-bar element — `/rc connecting…`
transitioning to `/rc active` — which is not a `·`-joined segment but a
whitespace-padded right-aligned token, and therefore breaks the whole-row
ORDERED-template fullmatch. Consequence (unverified in a live bridge, but the
GH #62 class): on any pane painting this element, `_is_status_row` is False, so
`pane_looks_idle` and the leg-3 `_INPUT_READY_CHROME_MARKERS` path lose the
status bar and the delivery gate fail-closes (`/update` defers, `/cost`
refuses, a send can refuse `no_ready_chrome`). Fail-closed — a refusal, never a
wrong commit — but a real degradation.

**Why the post-Enter REPL frames are NOT committed as fixtures here:** the
repo's corpus sweep `test_every_real_corpus_status_row_matches_a_template`
correctly goes RED on such a frame (it is designed to fail LOUDLY on exactly
this drift). Landing that fixture in this wave would force either an
out-of-scope grammar change or a weakening of that safety test — both refused.
The raw frames stay in the GH #72 rig scratch (`frames/e3_postenter_t{1,3,10}_*`)
as evidence and are the starting fixture set for the follow-up wave.

## Per-family dispatch-table-entry criteria (BINDING, from the 2.1.204 doc)

  (a) fixture set (initial + arrow-moved frames) — **SATISFIED**
  (b) arrow-move-only transcript — **SATISFIED** (E2, four arrows, prompt live)
  (c) Enter-commits transcript — **SATISFIED** (E3 on option 1, E2c on option 2)

All three on the SAME named version, `2.1.246`, verified before and after.

## CONCLUSION — **GO** for `folder-trust` on CC **2.1.246**

Table-eligible for `_DECISION_DISPATCH_TABLE["folder-trust"]` ⊇ {"2.1.246"}
under navigate→verify→Enter: arrows nav (non-committing, WRAPPING), mandatory
pre-Enter verify, `Enter`-only commit, digits FORBIDDEN, `Escape` = cancel that
kills the process.

**Same disclosed caveat as 2.1.241:** the characterization was made against an
npm-installed binary whose `pane_current_command` is `claude.exe`. The KEYSTROKE
model is version behavior and is unaffected by the launcher name, and the Fix-0
`--version` probe (the lane's actual licensing input) was verified on this
binary. If any code path licenses the trust lane on `pane_current_command`
matching a version string, that shape is UNVERIFIED for 2.1.246 by this rig and
must not be assumed.
