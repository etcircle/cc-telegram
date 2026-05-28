# AUQ multi-select TUI protocol (Claude Code v2.1.152/v2.1.153)

PR-A verification artifact for `temp/2026-05-27-multiselect-picker-plan-v5.md` §4.

This document is intentionally empirical. The pane fixtures under
`tests/cctelegram/fixtures/` are real `tmux capture-pane -p` captures from live
Claude Code `AskUserQuestion` prompts created in this worktree. They are not
hand-written mocks.

## Capture environment

- Worktree: `/Users/felixcardix/dev-workspaces/cc-telegram`
- Claude Code: v2.1.152 and v2.1.153, Sonnet 4.6, interactive TUI in tmux.
- tmux: 3.6a.
- Capture commands used the live pane, e.g. `tmux capture-pane -t <session> -p` or
  `tmux capture-pane -t <session> -p -S -80` after driving keys with
  `tmux send-keys`.

## Fixture inventory

- `tests/cctelegram/fixtures/auq_multiselect_fresh_tmux_capture.txt`
  - Real fresh multi-select AUQ.
  - Cursor on option 1.
  - No selected options.
- `tests/cctelegram/fixtures/auq_multiselect_2_toggled_tmux_capture.txt`
  - Real multi-select AUQ after toggling option 1 and option 2.
  - Cursor on option 2.
  - Options 1 and 2 selected.
- `tests/cctelegram/fixtures/auq_multiselect_ready_to_submit_tmux_capture.txt`
  - Real review/submit screen after `Tab` from the option block.
  - Shows selected answers and `Submit answers` / `Cancel` choices.
- `tests/cctelegram/fixtures/auq_multiselect_compressed_long_cursor_only_tmux_capture.txt`
  - Real visible-pane capture from a long-description multi-select AUQ in a
    short tmux pane.
  - Only one checkbox option row is visible in the captured viewport.
  - This is a real partial/compressed pane shape, not a full-form source.

## Exact glyph set observed

Observed in Claude Code v2.1.152/v2.1.153:

- Option unchecked: `[ ]`
- Option selected: `[✔]`
- Option cursor in normal full view: leading `❯`
- Option cursor in a partial/scroll-constrained visible pane: leading `↓` was
  observed on the visible cursor row in the compressed fixture.
- Header question state unchecked: `☐`
- Header question state has at least one answer: `☒`
- Header submit tab marker: `✔ Submit`

Not observed in these captures for option rows:

- `☐` / `☒` as option checkbox glyphs.
- `[x]` as the selected option glyph.

Implementation implication: PR-B parser work should support the observed ASCII
option checkbox forms (`[ ]`, `[✔]`) at minimum. Keeping `[x]` as a tolerant
alias is fine, but treating only `☐/☒/✔` as option-row checkboxes would miss the
current Claude Code TUI.

## Keystroke transition table

All rows below were observed against live multi-select AUQs in tmux.

| Action | Keystroke | Observed result |
|---|---|---|
| Toggle current row | `Space` | Current row flips `[ ]` -> `[✔]`; header question marker changes `☐` -> `☒`. |
| Toggle current row | `Enter` while cursor is on an option | Current row flips `[ ]` -> `[✔]`; it does not submit from the option block in v2.1.153. |
| Toggle non-current row by digit | `2` while cursor is on option 1 | Option 2 flips to `[✔]`; cursor remains on option 1. |
| Existing bot shape | `2` then `Enter` while cursor is on option 1 | Option 2 flips to `[✔]`, then option 1 flips to `[✔]`; it does not submit in v2.1.153. This differs from the original bug report's destructive-submit description and must be treated as version-sensitive. |
| Navigate down | `Down` | Cursor moves to the next option row. |
| Navigate up from first option | `Up` | Cursor wraps to the last option-like row, observed as `Type something`, not to the last real answer option. |
| Move to review/submit screen | `Tab` | TUI changes from option list to `Review your answers` screen. |
| Submit selected answers | `Tab`, then `Enter` on `Submit answers` | Claude accepts the selected answers and leaves the AUQ picker. |
| Cancel from option block | `Esc` | AUQ picker is dismissed/cancelled back to the normal prompt area. |

## §4.1 invariant answers

### Q1: Can the bot read the cursor row from a pane parse before sending Space?

Answer: yes for the observed live TUI shapes, with one parser caveat.

Evidence:

- Fresh fixture line shape: `❯ 1. [ ] ...`.
- Two-toggled fixture line shape: `❯ 2. [✔] ...`.
- Compressed visible-pane fixture line shape: `↓ 3. [ ] ...`.

The cursor row is encoded directly in the pane text before any toggle key is
sent. PR-B should treat both `❯` and the observed partial-pane `↓` prefix as
cursor markers. If PR-B only recognises the legacy cursor-prefix set from the
plain numbered parser and excludes `↓`, compressed captures will not satisfy the
pre-Space verification guard.

### Q2: Can the bot identify the single-tab checkbox submit shape and reliably suppress/reject tabbed forms?

Answer: partially yes for suppression; no confirmed single-tab-only submit shape
was observed in this environment.

Evidence:

- Every real multi-select AUQ observed in Claude Code v2.1.152/v2.1.153 rendered
  with a header of the form `←  ☐/☒ <question-tab>  ✔ Submit  →`.
- The option block footer is `Enter to select · ↑/↓ to navigate · Esc to cancel`.
- `Tab` moves to a review/submit screen containing `Submit answers` / `Cancel`;
  `Enter` on `Submit answers` submits.
- Direct `Enter` on an option toggles the option; it does not submit in
  v2.1.153.

So the bot can reliably identify and suppress the tabbed/header shape using the
existing top anchor class `^\s*←\s+[☐✔☒]` plus `len(questions) > 1` / `form.tabs`
where available. But PR-A did not reproduce a no-header single-tab checkbox form
where direct `Enter` submits. The plan should not assume direct-Enter submit from
the option block for current Claude Code. PR-D's submit dispatch should be based
on the observed review path (`Tab` -> `Enter`) unless PR-B captures a genuine
single-tab non-header variant proving otherwise.

### Q3: What exact pane states force `select_mode = "unknown"`?

Answer: the suppression set should include these states.

1. Partial checkbox detection: at least one parsed option row has a checkbox
   glyph and at least one parsed option row does not.
2. No parsed option rows inside an AUQ-looking region.
3. Footer-only or bottom-fragment capture: `Enter to select` is visible but the
   question/options header needed to identify the form is absent.
4. Header-only or mid-redraw capture: `← ☐/☒ ... ✔ Submit →` is visible but no
   stable option rows/footer are visible yet.
5. JSONL/side-file says `multiSelect: true`, but the pane shows plain numbered
   options with no checkbox glyphs.
6. JSONL/side-file says `multiSelect: false`, but the pane shows checkbox option
   rows.
7. Non-boolean or malformed `multiSelect` in JSONL/side-file.
8. Multi-question/tabbed multi-select for this wave's callback minting: detection
   may record `multi`, but callback rows must be suppressed. If the parser cannot
   distinguish current tab/question confidently, treat it as `unknown` for pick
   dispatch.
9. Pane-only compressed/partial visible captures, including the committed
   compressed fixture where only one option row is visible. Even if all visible
   option rows have checkboxes, `options_complete=False` must suppress
   `aqm:/aqs:/aqx:` minting unless a complete side-file/tool-input source wins.
10. Any stale-picker boundary/collapsed-region ambiguity (`… +N lines (ctrl+o to
    expand)`) between the candidate top and live footer.

Implementation split: `select_mode` answers type confidence; `options_complete`
answers source completeness. A pane-only compressed capture may be confidently
`multi` by glyphs, but it is still incomplete and must suppress callbacks.

### Q4: Source parity — can render and callback validation resolve the same source/fingerprint for a live pending AUQ?

Answer: not proven by PR-A; the current code does not yet contain the shared
`_resolve_auq_source` resolver described in the plan.

Evidence and boundary:

- The committed compressed fixture proves the pane alone can be incomplete.
- The existing parser patterns prove cc-telegram can identify AUQ pane variants,
  but current PR-A intentionally makes no runtime/source-resolution changes.
- The source-parity requirement is therefore an implementation invariant for
  PR-B/PR-C/PR-D: render and callback validation must call the same resolver and
  must prefer a fresh pane-validated PreToolUse side file over a stale completed
  cache for live pending AUQs.

PR-B should add executable tests using a stale completed cache + fresh side-file
+ compressed pane fixture and assert the render-time and validate-time canonical
fingerprints are byte-identical. PR-A cannot honestly claim that parity is true
before that resolver exists.

## Plan feedback before PR-B

1. The plan's option-row glyph regex must be updated from Unicode-only
   `☐/☒/✔` to include current option glyphs `[ ]` and `[✔]`.
2. The dispatch model should be revised for Claude Code v2.1.153: direct
   `Enter` on an option toggles; submit was observed as `Tab` to review screen,
   then `Enter` on `Submit answers`.
3. Existing bot `digit + Enter` is still wrong for multi-select, but in the
   observed current TUI it toggles the digit option and then toggles the cursor
   option rather than submitting. The bug remains real, but the failure mode is
   version-sensitive.
4. PR-B should recognise `↓` as a cursor marker in compressed/partial visible
   captures, or explicitly classify that shape as incomplete/unknown and avoid
   pre-Space dispatch from it.
5. Q4 source parity should remain a blocking executable PR-B/PR-C invariant; it
   is not empirically provable in a behavior-neutral doc/fixture-only PR-A.
