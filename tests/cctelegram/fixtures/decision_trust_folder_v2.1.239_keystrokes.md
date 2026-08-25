# GH #65 / Wave 3 rig — folder-trust Decision family keystroke transcript (CC v2.1.239)

Isolated rig capture, 2026-08-25. The production tmux server was never touched:
everything ran on the private socket `tmux -L ccrig`, session `rig`, geometry
**160x50**, with `CC_TELEGRAM_DIR` pointed at a scratch dir and a FRESH
throwaway cwd per E-run (folder trust persists per-directory in `~/.claude.json`,
so a reused dir does not re-prompt).

**CC version: `2.1.239 (Claude Code)` — verified BEFORE and AFTER the battery,
identical** (`DISABLE_AUTOUPDATER=1` on every rig invocation). No committed
frame in this set is from another version. See `meta_v2.1.239.txt`.

Binary: `/Users/felixcardix/.local/bin/claude` →
`/Users/felixcardix/.local/share/claude/versions/2.1.239` (native installer).

Launch shape (mirrors `tmux_manager._compose_launch_command`, incl. a VALID
minimal `--settings` file, unlike the 2.1.204-era capture):

```
cd <fresh-dir> && CC_TELEGRAM_DIR=<scratch>/ccdir DISABLE_AUTOUPDATER=1 \
  claude --dangerously-skip-permissions --settings <scratch>/md_settings.json
```

**Isolation VERIFIED**: the SessionStart hook wrote
`<scratch>/ccdir/session_map.json` with key `rig:@4`; the real
`~/.cc-telegram/session_map.json` sha1 was `7feffc7a58ffc84ffb1ea8856d09fa8651d1440c`
before AND after the whole rig.

## Prompt shape (UNCHANGED from .200/.201/.204)

Title line: `Accessing workspace:`
Options (exact ordered label tuple = the family signature):
  `("Yes, I trust this folder", "No, exit")`
Footer: `Enter to confirm · Esc to cancel`

Family signature for `_DECISION_DISPATCH_TABLE`:
  - normalized title anchor: `^Accessing workspace:`
  - option-label tuple: `("Yes, I trust this folder", "No, exit")`

ANSI (from `folder_trust_arrival_ansi_v2.1.239.txt`): the CURSORED row —
`❯` glyph, the `N.` number and every label word — is painted `ESC[38;5;153m`
(light blue); the non-cursored row is unstyled; the footer is `ESC[38;5;246m`
(dim grey). There is **no SGR-2 dim** anywhere in this surface.

`pane_current_command` while the TUI runs: **`2.1.239`** — the bare version
string (the macOS process-title shape; here it is literally the FILENAME of the
native-installer versioned binary). `pane_command_is_claude` accepts it.

## Keystroke → observed outcome (E0–E4)

- **E0 arrival + PERSISTENCE: the prompt renders ~2 s after launch and persists
  BYTE-IDENTICALLY at T+60 s and T+90 s.** `diff` of the arrival capture against
  both later captures is empty. No self-advance, no timeout, no redraw drift.
  The plan's 900 s trust ceiling is not contradicted by anything observed here
  (observation window is 90 s; a 900 s claim remains an extrapolation).
  Fixtures: `folder_trust_arrival_plain_v2.1.239.txt`,
  `folder_trust_arrival_ansi_v2.1.239.txt`,
  `folder_trust_arrival_t60_plain_v2.1.239.txt`,
  `folder_trust_arrival_t90_plain_v2.1.239.txt`.

- **E2 arrows MOVE the `❯` cursor WITHOUT committing — and they WRAP, they do
  NOT clamp.** From `❯ 1`: `Down` → `❯ 2` (`folder_trust_postdown_plain_…`);
  a SECOND `Down` → back to `❯ 1` (`folder_trust_postdown2_plain_…`) — **this
  is a WRAP, not a clamp**; `Up` → `❯ 2` (`folder_trust_postup_plain_…`);
  a second `Up` → `❯ 1` (`folder_trust_postup2_plain_…`). The prompt stayed
  LIVE through all four arrows (footer present, `pane_current_command` still
  `2.1.239`). Arrows are the licensed nav key for this family/version.
  **DIVERGENCE from the AUQ picker on 2.1.207, which CLAMPS** — a nav planner
  that assumes clamping to "settle" an overshoot would be wrong here. With a
  2-option list the required delta is at most 1, and the mandatory pre-Enter
  verify makes a wrap harmless, but the assumption must not be inherited.

- **E3 `Enter` COMMITS the cursored option.** With `❯` on option 1, `Enter`
  selected "Yes, I trust this folder": the trust prompt was GONE at T+1 s and
  the normal Claude Code welcome/REPL was on the pane
  (`folder_trust_postenter_t1/t3/t10_plain_v2.1.239.txt`). The SessionStart
  hook fired and registered `rig:@4` in the SCRATCH `session_map.json`.

- **E2c compound (navigate → verify → Enter, the actual dispatch model):** on a
  still-live prompt, `Down` moved `❯` to option 2, the verify frame confirmed
  `❯ 2. No, exit`, and `Enter` then committed **option 2** — `claude` exited to
  a bare shell. Proof that `Enter` commits the CURSORED option, not a fixed
  default. Fixtures: `folder_trust_e2c_navto2_plain_…`,
  `folder_trust_e2c_postenter_plain_…`.

- **E1 bare digit `2` COMMITS immediately — NO verify window.** With `❯` on
  option 1, sending the literal `2` (no Enter) selected "No, exit" and `claude`
  exited; `pane_current_command` was already `zsh` at T+0.5 s. Digits stay
  FORBIDDEN in the dispatch lane. MATCHES 2.1.204.
  Fixtures: `folder_trust_postdigit2_t0.5_plain_…`, `..._t2_plain_…`.

- **E4 `Escape` CANCELS — `claude` exits to a bare shell.**
  `pane_current_command` goes `2.1.239` → `zsh` within 1 s and stays there.
  Fixtures: `folder_trust_postesc_t1_plain_…`, `folder_trust_postesc_t4_plain_…`.

- **V1 version probe (plan Fix 0) — WORKS, with one parser caveat.** Sending
  `printf '<A>\n'; DISABLE_AUTOUPDATER=1 claude --version; printf '<B>\n'` into a
  fresh shell pane produced, on separate pane lines: `<A>` / `2.1.239 (Claude
  Code)` / `<B>` (`version_probe_plain_v2.1.239.txt`). **Caveat: the shell
  ECHOES the whole command line, so a pane line exists that CONTAINS both nonces
  — and when the command is long it WRAPS, putting `<A>` and `<B>` on two
  ADJACENT echoed lines** (observed on the 2.1.241 probe, whose path is longer).
  The parser must therefore require the delimiter lines to be an EXACT
  whole-line FULLMATCH of the nonce (after strip) — the echoed lines never are.
  A weaker "line contains nonce" rule degrades to `None` (fail-closed,
  display-only) in the observed wrap case, but is not robust by construction.

## POST-COMMIT / POST-CANCEL PANE HAZARD (new observation, both versions)

After a digit commit (E1) **and** after `Escape` (E4), `claude` exits, but the
**trust prompt text REMAINS on the pane** — including a `❯` on an option row and
the `Enter to confirm · Esc to cancel` footer — with only the shell prompt line
appended below. A TEXT-ONLY liveness check would therefore report the trust card
as still live on a DEAD pane. The only reliable discriminator observed is
`pane_current_command` (`2.1.239` vs `zsh`), consistent with the existing
"bottom-most is live" + `pane_command_is_claude` invariants. Any trust-card
liveness/teardown logic must key on the process, not on the pane text.

Also: at T+1 s after `Enter`, the pane can be a **fully blank frame** (observed
on 2.1.241; on 2.1.239 the welcome was already painted). "Trust prompt gone,
nothing painted yet" is a legitimate transitional state and must not classify as
failure.

## Per-family dispatch-table-entry criteria (BINDING, from the 2.1.204 doc)

  (a) fixture set (initial + arrow-moved frames) — **SATISFIED**
      (`_arrival_`, `_postdown_`, `_postdown2_`, `_postup_`, `_postup2_`, all
      plain + ANSI for the key frames).
  (b) arrow-move-only transcript — **SATISFIED** (E2, four arrows, prompt live
      throughout; wrap behavior explicitly recorded).
  (c) Enter-commits transcript — **SATISFIED** (E3 on option 1 and E2c on
      option 2, both on this same version).

## CONCLUSION — **GO** for `folder-trust` on CC **2.1.239**

The folder-trust family on 2.1.239 satisfies (a)+(b)+(c) on this one named
version and is **table-eligible for `_DECISION_DISPATCH_TABLE["folder-trust"]`
= {"2.1.239"}** under the navigate→verify→Enter dispatch model:

- nav key = `Down`/`Up` (arrows), non-committing, **WRAPPING** (max delta 1 on a
  2-option list);
- a pre-Enter verify of the cursored row is MANDATORY (the wrap means an
  overshoot silently lands on the other option instead of clamping);
- commit key = `Enter`, and only `Enter`;
- bare digits remain FORBIDDEN (they commit with no verify window);
- `Escape` is a CANCEL that kills the process — matching the plan's
  "Cancel kills instead of typing" decision.
