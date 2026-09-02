# GH #88 — folder-trust Decision family keystroke transcript (CC v2.1.258)

Isolated rig capture, 2026-09-02. Private socket `tmux -L ccrig`, sessions
`gh88a` / `gh88s` / `gh88d1` / `gh88d2` / `gh88e1` / `gh88e2` / `gh88esc` /
`gh88p7`, geometry **160x50**, scratch `CC_TELEGRAM_DIR`, a FRESH throwaway cwd
per E-run. The production tmux server (`ccbot`) and the real `~/.cc-telegram`
were never touched; the pre-existing rig session `rig` was left alone.

**CC version: `2.1.258 (Claude Code)`** — the NATIVE installer
(`~/.local/bin/claude -> ~/.local/share/claude/versions/2.1.258`), so
`pane_current_command` reports the bare version string `2.1.258` while the TUI
runs (the .241/.246 `claude.exe` rig anomaly of #69 does NOT apply here — the
version-string shape IS observed on this rig).

Launch shape (exactly `tmux_manager._compose_launch_command`, default
`CLAUDE_COMMAND=claude`, no `--dangerously-skip-permissions`):

```
tmux -L ccrig new-session -d -s <s> -x 160 -y 50 -c <fresh-dir> \
  -e CC_TELEGRAM_DIR=<scratch>/rigstate -e DISABLE_AUTOUPDATER=1 \
  "claude --settings ~/.cc-telegram/md_hook_settings.json"
```

**Isolation VERIFIED**: SessionStart wrote `<scratch>/rigstate/session_map.json`
with keys `gh88e2:@13` / `gh88s:@6` only; the real
`~/.cc-telegram/session_map.json` sha1 was
`64fd4bb582a254cbaff467518db35a2e0565cdd4` before AND after the whole battery.

---

## THE BREAKING CHANGE — the prompt is REDESIGNED, not drifted

2.1.258 **removed the `N.` numbering AND inverted the option order**. Every
licensed version (2.1.204 … 2.1.246) rendered:

```
 ❯ 1. Yes, I trust this folder
   2. No, exit
```

2.1.258 renders:

```
 ❯ No, exit
   Yes, I trust this folder
```

Three simultaneous changes, each of which alone breaks the lane:

1. **Options are UNNUMBERED** → `_RE_NUMBERED_OPTION` matches neither row →
   `_gate_options_above` returns `()` → `_parse_footered_decision` returns
   `None`.
2. **Order INVERTED** — the destructive option is now FIRST.
3. **Default cursor moved to the destructive option** (`❯ No, exit`), where it
   used to sit on `Yes, I trust this folder`.

The surrounding block is otherwise unchanged: same `Accessing workspace:` title,
same security prose, same `Security guide` link row, same
`Enter to confirm · Esc to cancel` footer, same `─` top rule.

### The second (new) variant — a `permissions.allow` pre-approval warning

A cwd whose `.claude/settings.json` carries a `permissions.allow` list renders an
extra `⚠` block between the prose and `Security guide`:

```
 ⚠ This folder pre-approves 18 tool permissions in .claude/settings.json:
   Write, mcp__context7__resolve-library-id, …, and 10 more
 These will apply without asking. Only proceed if you trust this configuration.
```

The option/footer geometry is IDENTICAL to the plain variant; only the body
grows (arrival rows 19/20/22 instead of 14/15/17). Verified independently: the
whole ⚠ block is byte-stable across a `Down` nav
(`SETTINGS_BLOCK_UNCHANGED_DURING_NAV`).

### ANSI styling (from `folder_trust_arrival_plain_v2.1.258_ansi.txt`)

```
 ESC[38;5;153m❯ESC[39m ESC[38;5;153mNo,ESC[39m ESC[38;5;153mexitESC[39m
   Yes, I trust this folder
```

- Cursored row (`❯` + every label word): `ESC[38;5;153m`; the non-cursored row is
  fully UNSTYLED. **There is no `N.` number to style any more.**
- Title words: `ESC[1m` + `ESC[38;5;220m`; top rule `ESC[38;5;220m`; cwd `ESC[1m`.
- Footer, word by word: `ESC[38;5;246m`.
- ⚠ heading words: `ESC[1m` + `ESC[38;5;220m`; the permission list is unstyled;
  the "These will apply without asking." line is `ESC[38;5;246m`.
- **`Security guide` is an OSC 8 hyperlink** (present on 2.1.246 too):
  `ESC]8;id=zaxmda;https://code.claude.com/docs/en/security ESC\ Security guide ESC[39m ESC]8;;ESC\`
  — visible ONLY in an `-e` capture. The trust lane captures WITHOUT `-e`, so
  this never reaches any classifier today (see § residuals).
- **No SGR-2 dim (`ESC[2m`) anywhere on this surface** (grep count 0) — the
  free-text DIM landing proof stays structurally unreachable here, unchanged.

---

## Keystroke → observed outcome (E0–E4)

| # | keystroke / event | 2.1.246 (licensed) | **2.1.258 (measured)** | fixture |
|---|---|---|---|---|
| E0 | arrival T+2 s vs T+7 s | byte-stable | **byte-stable** (plain AND ansi diff empty) | `folder_trust_arrival_plain_v2.1.258.txt` |
| E0 | default cursor row | `❯ 1. Yes, I trust this folder` | **`❯ No, exit`** (row 1 = the DESTRUCTIVE option) | ″ |
| E0 | settings variant arrival | n/a (not characterized) | ⚠ block renders; geometry otherwise identical | `folder_trust_arrival_settings_v2.1.258.txt` |
| E0 | persistence (≫ T+90 s: ~10 min plain, ~6 min settings) | no self-advance | **no self-advance**, frames diff EMPTY, `pane_current_command` still `2.1.258` | — |
| E2 | `Down` from row 1 | ❯ → option 2 | **❯ → `Yes, I trust this folder`**, prompt LIVE | `folder_trust_postdown_plain_v2.1.258.txt` |
| E2 | `Down` again (WRAP test) | wraps to option 1 | **WRAPS to `No, exit`** (never clamps) | `folder_trust_postdown2_plain_v2.1.258.txt` |
| E2 | `Up` | wraps | **wraps** (`No, exit` → `Yes…`) | `folder_trust_postup_plain_v2.1.258.txt` |
| E2c | compound `Up, Up, Up` | alternates | **alternates**, prompt live throughout, cmd `2.1.258` on every frame | — |
| E2 | settings variant `Down` / `Down` | n/a | identical (moves, then WRAPS); ⚠ block unchanged | `folder_trust_postdown_settings_v2.1.258.txt` |
| **E1** | **bare digit `1`** | **COMMITS instantly** | **COMPLETELY INERT** — no commit, no cursor move; frame byte-identical to arrival at T+1 AND T+4, `pane_current_command` still `2.1.258` | `folder_trust_postdigit1_plain_v2.1.258.txt` |
| **E1** | **bare digit `2`** | **COMMITS instantly** | **COMPLETELY INERT** — same proof | `folder_trust_postdigit2_plain_v2.1.258.txt` |
| E1 | digit `1` / `2` on the settings variant | n/a | **INERT** (cursor unmoved on `❯ No, exit`) | `folder_trust_postdigit1_settings_v2.1.258.txt` |
| E3a | `Enter` on the DEFAULT row (`No, exit`) | (was option 2) | **claude EXITS.** Prod-shape pane: prompt text RETAINED, shell prompt appended, `pane_current_command` → `zsh` | `folder_trust_postenter_noexit_t4_plain_v2.1.258.txt` |
| E3b | `Down` → verify `❯ Yes, I trust this folder` → `Enter` | commits | **COMMITS the cursored option.** NO blank transitional frame: the REPL banner is already painted at **T+1 s**, and the input box + status bar are on rows 47-50 of that SAME T+1 capture | `folder_trust_postenter_t1_plain_v2.1.258.txt`, `trust_after_accept_repl_v2.1.258.txt` |
| E3b | registration | — | `SessionStart` wrote `gh88e2:@13` to the SCRATCH `session_map.json`; `~/.claude.json` shows `hasTrustDialogAccepted: true` for the accepted cwd | — |
| E3b | settings variant `Down` → `Enter` | n/a | identical: commits, registers `gh88s:@6`, `hasTrustDialogAccepted: true` | `trust_after_accept_settings_repl_v2.1.258.txt` |
| E4 | `Escape` | kills claude, text retained | **kills claude**, `2.1.258` → `zsh`, prompt text RETAINED with the shell prompt appended | `folder_trust_postesc_t4_plain_v2.1.258.txt` |

### E1 is the one BEHAVIORAL divergence, and it is a SAFETY IMPROVEMENT

On 2.1.204–2.1.246 a bare in-range digit was a live HOTKEY that committed with no
Enter. On 2.1.258 — with the numbering removed — digits `1` and `2` are
**completely inert on both variants**: the post-digit frame diffs byte-identical
against the arrival frame (modulo the cwd line), and the pane command is
unchanged at T+1 s and T+4 s.

**This does NOT license sending digits.** The measurement covers `1` and `2` on
one CC build; the shipped "digits FORBIDDEN" rule stays, now for a second reason
(they are useless as well as unsafe).

### POST-COMMIT / POST-CANCEL PANE HAZARD — UNCHANGED

After `Enter` on `No, exit` (E3a) and after `Escape` (E4), claude exits but the
whole trust prompt — the `❯` option row and the
`Enter to confirm · Esc to cancel` footer — REMAINS painted, with only the shell
prompt appended below. Text-only liveness still false-positives on a corpse.
Keep keying liveness on `pane_current_command`, never on pane text.

### Post-accept chrome (the 2.1.258 REPL)

```
                                                                       ● high · /effort     <- T+1 only, gone by T+18
────────────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents                              /rc
```

- The status row (mode + `← for agents` + the right-aligned `/rc` pill) FULLMATCHES
  the shipped `_is_status_row` grammar — **verified True** on all six post-accept
  captures. At T+1 the pill reads `/rc connecting…` (also True). **No GH #62-class
  status-bar drift on this version.**
- `● high · /effort` is a transient right-aligned element ABOVE the input box's
  top rule, so it is never the "first non-blank row below the last separator" the
  corpus sweep tests. It is NOT `_is_status_row`-matching on its own.

### Per-family dispatch-table-entry criteria (BINDING, from the 2.1.204 doc)

  (a) fixture set (initial + arrow-moved frames) — **SATISFIED** (both variants)
  (b) arrow-move-only transcript — **SATISFIED** (E2 + E2c, prompt live throughout)
  (c) Enter-commits transcript — **SATISFIED** (E3a on row 1, E3b on row 2)

All three on the SAME named version, `2.1.258`.

## CONCLUSION

The KEYSTROKE MODEL is unchanged and remains dispatchable: arrows nav
non-committingly and **WRAP**, `Enter` commits the CURSORED option, `Escape`
cancels and kills the process, digits stay forbidden (and are now inert anyway).

**But the RECOGNIZER is dead.** `parse_generic_decision` returns `None` on both
2.1.258 variants, so `classify_slice` never returns `TRUST_FRAME` and the lane
degrades all the way back to the pre-GH#65 "didn't register in time" kill.
2.1.258 is therefore **NOT** table-eligible until the parser recognizes the
unnumbered shape and the family match / nav / mint stop assuming "Trust is
option 1".
