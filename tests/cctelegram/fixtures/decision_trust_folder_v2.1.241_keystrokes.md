# GH #65 / Wave 3 rig — folder-trust Decision family keystroke transcript (CC v2.1.241)

Isolated rig capture, 2026-08-25. Private socket `tmux -L ccrig`, session `rig`,
geometry **160x50**, scratch `CC_TELEGRAM_DIR`, a FRESH throwaway cwd per E-run.
The production tmux server and the real `~/.cc-telegram` were never touched.

**CC version: `2.1.241 (Claude Code)` — verified BEFORE and AFTER the battery,
identical** (`DISABLE_AUTOUPDATER=1` on every rig invocation). See
`meta_v2.1.241.txt`.

Binary — ISOLATED npm prefix, NOT on `PATH`:

```
npm install -g --prefix <scratch>/cc241 @anthropic-ai/claude-code@2.1.241
<scratch>/cc241/bin/claude -> ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
```

Launch shape (mirrors `tmux_manager._compose_launch_command`, full path to the
isolated binary, valid minimal `--settings`):

```
cd <fresh-dir> && CC_TELEGRAM_DIR=<scratch>/ccdir241 DISABLE_AUTOUPDATER=1 \
  '<scratch>/cc241/bin/claude' --dangerously-skip-permissions --settings <scratch>/md_settings.json
```

**Isolation VERIFIED**: SessionStart wrote `<scratch>/ccdir241/session_map.json`
key `rig:@10`; the real `~/.cc-telegram/session_map.json` sha1 was unchanged
(`7feffc7a58ffc84ffb1ea8856d09fa8651d1440c`) before and after.

## Prompt shape — IDENTICAL to 2.1.239

Title line: `Accessing workspace:`
Options: `("Yes, I trust this folder", "No, exit")`
Footer: `Enter to confirm · Esc to cancel`

A `diff` of the 2.1.239 and 2.1.241 arrival frames, with the (differing) echoed
launch-command lines and the cwd paths stripped, is **empty** — the prompt block
is byte-identical across the two versions. ANSI styling is identical too:
cursored row (`❯`, `N.`, every label word) in `ESC[38;5;153m`, footer in
`ESC[38;5;246m`, no SGR-2 dim.

## ⚠ RIG ANOMALY — `pane_current_command` is `claude.exe`, NOT a version string

On the isolated npm install, `pane_current_command` while the TUI runs reports
**`claude.exe`** (the npm launcher's filename), at arrival, at T+60 s and at
T+90 s. It is NOT the bare version string that the native installer produces.

Root cause is the BINARY FILENAME, not the CC version:
- native installer → `~/.local/share/claude/versions/2.1.239`, so macOS reports
  the file's name, which happens to BE the version string;
- npm package → `bin/claude.exe`, so macOS reports `claude.exe`.

**Consequence — a genuine product finding, not just a rig artifact:**
`tmux_manager.pane_command_is_claude("claude.exe")` returns **False** (verified
by running the predicate: the version regex does not match and
`os.path.basename("claude.exe")` is not in `_CLAUDE_BINARY_NAMES == {"claude"}`).
An **npm-installed Claude Code on macOS would therefore be refused by the GH #50
delivery gate on every send** — the exact class of bug as GH #63 §1 on Linux
(`feedback_pane_current_command_platform_shape`). Out of scope for #65 itself,
but it should be filed.

**Consequence for THIS characterization (disclosed, not smoothed over):** the
`pane_current_command` VERSION-STRING shape for 2.1.241 was **NOT observed** in
this rig — only `claude.exe`. If any part of the trust lane licenses on
`pane_current_command`, its 2.1.241 shape is UNVERIFIED here. The plan's Fix-0
licensing input is the nonce-delimited `--version` PROBE, which WAS verified on
this exact binary (V1 below), so the #65 licensing path is covered.

## Keystroke → observed outcome (E0–E4)

- **E0 arrival + PERSISTENCE: prompt renders ~2 s after launch and persists
  BYTE-IDENTICALLY at T+60 s and T+90 s** (`diff` empty against both). No
  self-advance, no drift. Fixtures: `folder_trust_arrival_plain_v2.1.241.txt`,
  `folder_trust_arrival_ansi_v2.1.241.txt`, `..._t60_plain_…`, `..._t90_plain_…`.

- **E2 arrows MOVE the `❯` cursor WITHOUT committing — and they WRAP, not
  clamp.** `Down` → `❯ 2`; second `Down` → back to `❯ 1` (**WRAP**); `Up` →
  `❯ 2`; second `Up` → `❯ 1`. Prompt live throughout (footer present,
  `pane_current_command` still `claude.exe`). Fixtures:
  `folder_trust_postdown_plain/ansi_…`, `folder_trust_postdown2_plain_…`,
  `folder_trust_postup_plain/ansi_…`, `folder_trust_postup2_plain_…`.
  Same divergence from the AUQ-picker clamp as on 2.1.239.

- **E3 `Enter` COMMITS the cursored option.** With `❯` on option 1, `Enter`
  selected "Yes, I trust this folder" — the trust prompt was gone at T+1 s and
  the normal welcome/REPL was painted by T+3 s. SessionStart registered
  `rig:@10` in the scratch `session_map.json`.
  **Transitional-frame note:** the T+1 s capture is **51 BLANK lines** — the
  alt-screen had been cleared and the welcome had not yet painted (on 2.1.239
  the welcome was already up at T+1 s). "Trust prompt gone, nothing painted yet"
  is a legitimate state and must not classify as failure. Fixtures:
  `folder_trust_postenter_t1/t3/t10_plain_v2.1.241.txt` (+ `_t10_ansi_`).

- **E2c compound (navigate → verify → Enter):** on a still-live prompt, `Down`
  moved `❯` to option 2, the verify frame confirmed `❯ 2. No, exit`, and `Enter`
  committed **option 2** — `claude` exited to a bare shell. Fixtures:
  `folder_trust_e2c_navto2_plain_…`, `folder_trust_e2c_postenter_plain_…`.

- **E1 bare digit `2` COMMITS immediately — NO verify window.** With `❯` on
  option 1, the literal `2` (no Enter) committed "No, exit";
  `pane_current_command` was already `zsh` at T+0.5 s. Digits stay FORBIDDEN.
  Fixtures: `folder_trust_postdigit2_t0.5_plain_…`, `..._t2_plain/ansi_…`.

- **E4 `Escape` CANCELS — `claude` exits to a bare shell** (`claude.exe` → `zsh`
  within 1 s, still `zsh` at T+4 s). Fixtures: `folder_trust_postesc_t1_plain_…`,
  `folder_trust_postesc_t4_plain/ansi_…`.

- **V1 version probe (plan Fix 0) — WORKS on the isolated binary.**
  `printf '<A>\n'; DISABLE_AUTOUPDATER=1 '<full-path>' --version; printf '<B>\n'`
  produced on separate pane lines: `<A>` / `2.1.241 (Claude Code)` / `<B>`
  (`version_probe_plain_v2.1.241.txt`). **Parser caveat, observed HERE
  concretely:** the echoed command line is long enough to WRAP, so `<A>` lands
  at the end of one echoed pane line and `<B>` at the end of the NEXT — a naive
  "first line containing A, next line containing B, take what's between" scan
  finds an EMPTY region and returns `None`. Fail-closed (display-only), but the
  correct rule is to require the delimiter lines to be an EXACT whole-line
  FULLMATCH of the nonce after strip; the echoed lines never fullmatch, the real
  `printf` output lines always do.

## POST-COMMIT / POST-CANCEL PANE HAZARD (same as 2.1.239)

After the digit commit (E1) and after `Escape` (E4), `claude` exits but the
trust prompt text — `❯` on an option row and the `Enter to confirm · Esc to
cancel` footer — REMAINS on the pane, with the shell prompt appended below. A
text-only liveness check false-positives on a dead pane. Key trust-card
liveness/teardown on the PROCESS (`pane_current_command`), never on pane text.

## Per-family dispatch-table-entry criteria (BINDING, from the 2.1.204 doc)

  (a) fixture set (initial + arrow-moved frames) — **SATISFIED**
  (b) arrow-move-only transcript — **SATISFIED** (E2, four arrows, prompt live)
  (c) Enter-commits transcript — **SATISFIED** (E3 on option 1, E2c on option 2)

All three on the SAME named version, `2.1.241`, verified before and after.

## CONCLUSION — **GO** for `folder-trust` on CC **2.1.241**

Table-eligible for `_DECISION_DISPATCH_TABLE["folder-trust"]` ⊇ {"2.1.241"}
under navigate→verify→Enter: arrows nav (non-committing, WRAPPING), mandatory
pre-Enter verify, `Enter`-only commit, digits FORBIDDEN, `Escape` = cancel that
kills the process.

**One disclosed caveat on this version only:** the characterization was made
against an npm-installed binary whose `pane_current_command` is `claude.exe`.
The KEYSTROKE model is version behavior and is unaffected by the launcher name,
and the Fix-0 `--version` probe (the plan's actual licensing input) was verified
on this binary. But if any code path licenses the trust lane on
`pane_current_command` matching a version string, that shape is UNVERIFIED for
2.1.241 by this rig and must not be assumed.
