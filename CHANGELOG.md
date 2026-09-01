# Changelog

All notable changes to cc-telegram. Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
this project's package version is bumped per release, not per deploy (see the `--no-cache` note in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).

## [Unreleased]

### Fixed
- **Long messages arrived at Claude with their beginning cut off (GH #84).** A 1429-character
  voice transcription reached the terminal as its last 407 characters; a long reply-quote lost its
  head the same way. There was no error and no notice — the message simply started mid-sentence.
  Claude Code 2.1.246 changed how it reads a burst of typed text: anything above one 1022-byte
  read is discarded except the final read, most reliably on the first big message of a fresh
  session. The behaviour is still present in 2.1.252.

  Both inbound paths — an ordinary message into the input box, and a free-text answer typed into a
  card's "Type something." row — now type a long message as a series of 512-byte writes about a
  tenth of a second apart, committed by the same single Enter as before. On the rig this commits
  the message byte-exactly on 2.1.238, 2.1.247 and 2.1.252, including where a write boundary falls
  inside a run of blank lines, while the same payload sent as one burst still truncates. Chunked
  writes also render as ordinary text rather than a collapsed `[Pasted text]` block, so the first
  Enter submits instead of merely expanding the paste.

  A message the bot cannot type safely is now refused before anything is typed, with a new notice
  ("this message is too long, or has too many consecutive blank lines"): above 16 KB, or a run of
  900+ consecutive blank lines. Nothing else about the delivery gate moved — the same checks run in
  the same order, a message at or below 512 bytes is written exactly as before, and a message
  containing a line that is just a digit is still refused for that reason. Splitting a long message
  never creates such a line where the original had none.

## [0.4.16] — 2026-08-31

### Changed
- **Reply-quote prompt wrapper trimmed from 17 rows to 6 (GH #83).** Replying to a message wrapped
  the quote in a ~15-row scaffold — a six-line guardrail header, a four-line "Referenced message"
  block, three blank lines — so a one-line reply to a one-line quote became a 17-row prompt. The
  wrapper is now a single header line carrying the sender role, the Telegram message id and the
  nonce that opens the fenced block, followed directly by the fence, the quote, `[User message]`
  and the text: 6 rows, no blank lines. Both header variants — ordinary replies and replies to one
  of the bot's own status/activity cards — carry that same role + message-id pair, as the old
  "Referenced message" block did. Nothing about the security contract moved: the same per-render
  nonce fence still bounds the quoted body, the same demotion sentence still says the quote is
  context and not new instructions, and the cross-session notice still lives pre-fence where a
  hostile quote cannot spoof it. The `Claude session:` line is gone from ordinary replies; the
  session id now appears only on a cross-session reply, inside that notice.

  Those 6 rows are 6 rows on screen, not just 6 newlines: every header and notice line is at most
  158 characters even at its worst case (longest role, a 10-digit message id, a 36-character
  session uuid), and the bot's panes are 160 columns wide, so no scaffold line wraps and the
  prompt's line count is the count the terminal actually shows. That is the point of the change.
  At 17 rows every reply-quote draft pushed the input box's top rule out of the delivery gate's
  20-row chrome window, so each one took the GH #56 tall-draft fallback — the fragile leg that
  GH #62, #73 and #81 each broke in turn, wedging the topic every time. At 6 rows most
  reply-quotes now sit inside the window and take the gate's primary two-rule input-box proof
  instead.

## [0.4.15] — 2026-08-31

### Fixed
- **Reply-quoted messages were typed but never sent (GH #81).** Every reply-quoted Telegram
  message landed in the input box with its Enter withheld — "❌ Not delivered — the terminal
  changed while your message was being typed" — and had to be submitted by hand. Plain messages
  were unaffected. Claude Code 2.1.24x started wrapping the right-aligned `/rc` status pill in an
  OSC 8 hyperlink, and the parser's escape stripper only understood CSI-shaped sequences: it bit
  off the first few bytes of the hyperlink and left the session URL sitting on the status row as
  visible text. The delivery gate's fallback for a tall draft — the one every reply-quote takes,
  because the quote block pushes the input box's top rule out of view — proves the lone rule it
  can see is the box's bottom by checking that a status bar sits under it. With the URL on that
  row it no longer looked like one, so the gate concluded there was no input box at all. OSC
  sequences are now stripped properly, both terminators, in the status-row read and in the
  ghost-suggestion tokenizer. An unterminated one still degrades as before rather than eating the
  rows below it.

## [0.4.14] — 2026-08-27

### Fixed
- **Submit on a multi-question review card never worked (GH #78).** Every tap on "Submit
  answers" answered "Form changed, refreshing." and nothing was sent — permanently, until the
  card's tokens expired ~5 minutes later. The card and the tap were reading different sources:
  the render path sees a PreToolUse side file that cannot match a review screen (whose only rows
  are Submit/Cancel), so it bails to the pane and mints pane-sourced buttons — while the tap
  re-ran a *differently ordered* resolver that fell through to the buffered JSONL tool input,
  whose question text overlays the pane and changes the form fingerprint. The two could never
  agree. A tap now validates against the source its button was minted from, resolved once before
  the first keystroke and carried through the navigate/verify/commit transaction, so a source
  that legitimately disappears when the answer lands cannot retract a successful dispatch. Each
  per-source trust check is unchanged — a side file the live pane contradicts is still refused,
  so a stale question can never be answered by a tap.
- **The poller re-rendered a drifting card every second and starved its tokens (GH #78).** The
  same resolver disagreement made the source-drift re-mint a treadmill (observed: 124 re-renders
  in 6.5 minutes), and each re-mint skipped the deadline refresh that keeps a visible card
  tappable — so the card timed out while the user was looking at it. The re-mint now fires once
  per observed source and never suppresses the refresh.

### Changed
- A pick rejected because its minted source vanished or was replaced now reports `source_drift`
  rather than `stale_form`, and both rejections log the minted/computed fingerprints and the
  source resolution alongside the outcome.

## [0.4.13] — 2026-08-26

### Fixed
- **A background job could silently kill Telegram delivery for the session that started it
  (GH #76).** Messages stopped arriving for an interactive session — no error, no warning, the
  topic still showing a live pane — after a hook-spawned headless run (`claude --print`, e.g. a
  threshold- or SessionEnd-triggered `/self-curate`) started in the same window. The headless
  child inherits the parent's `TMUX_PANE` and cwd, so its own SessionStart hook registered it in
  `session_map.json` under the *interactive* window's key; last-writer-wins then handed both the
  routing authority and the tracking authority to the headless session id, untracking the
  interactive session along with its per-parent sidechain registries. `CLAUDE_CODE_ENTRYPOINT`
  cannot separate the two — the child inherits that too — so the hook now inspects the nearest
  `claude` ancestor's argv and declines to register when it finds an exact `-p` or `--print`
  token. The fix has to be preventive: once a hijack lands the dropped registries cannot be
  recovered. Fail-open by construction — no `/proc` (macOS), a vanished pid, an unreadable
  status file or any other probe failure all register normally, because a fail-closed bug here
  would stop *all* session registration. Residual: the guard is inert on macOS. The related
  bridge-side rebind gate remains open on #76.

### Added
- `CC_TELEGRAM_HEADLESS_REGISTRATION_GUARD` (default true) turns the headless-registration guard
  above off.

## [0.4.12] — 2026-08-26

### Changed
- **An unbound topic's first message is a knock, not a payload (GH #74, PR #75).** The text that
  opens the directory picker is no longer stashed and replayed into the freshly bound window — it
  only opens the picker, and the picker says so up front. This deletes the whole first-contact
  noise class (refusal card + alert + "please resend it here" for a trigger message the user never
  wanted delivered, worst on `--resume` where the TUI takes seconds to paint). Pending
  ATTACHMENTS still replay after bind; a legacy pre-#74 text stash is scrubbed on access. Built
  and peer-reviewed on the WSL box (two Codex rounds, both PASS, all findings folded).
## [0.4.11] — 2026-08-26

### Fixed
- **CC 2.1.246's right-aligned `/rc` status pill no longer wedges delivery (GH #73).** 2.1.246
  paints a remote-control pill (`/rc connecting…` / `/rc active` / `/rc reconnecting` /
  `/rc failed`, abbreviating to bare `/rc`, optionally suffixed `· Enter to view`) right-aligned
  on the SAME row as the status bar. The GH #62 whole-row grammar refused such rows, so the
  tall-draft recovery leg (GH #56) failed `no_input_box` on panes showing the pill. The grammar
  now strips a licensed right block — every variant derived from the 2.1.246 binary's own footer
  composer, never guessed — before classifying the row; acceptance rules are unchanged, a bare
  `/rc` row alone still refuses (non-widening), rows without a right block are processed
  byte-identically (pinned), and the leg-3 / idle alphabets are untouched (measured unaffected).
  Five real rig fixtures including the tall-draft regression pin; 34 new tests. Also verified for
  GH #70: `← for agents` was already accepted and pinned since GH #62; the custom `statusLine`
  fail-closed residual is now confirmed against the 2.1.246 source and documented.

## [0.4.10] — 2026-08-25

### Added
- **Folder-trust one-tap licensed on CC 2.1.246 (GH #72, partial).** The CLI auto-updated to
  2.1.246 hours after v0.4.9 shipped, so fresh trust cards rendered display-only (the designed
  fail-closed shape for an un-characterised version). An isolated rig re-ran the full E0–E4 + V1
  battery against 2.1.246: the prompt block is byte-identical to 2.1.239/2.1.241, arrows still
  wrap without committing, Enter commits the cursored option, bare digits still commit instantly
  (and stay forbidden), Escape cancels. `folder-trust` is now licensed on 2.1.246; fixtures and
  the per-version licensing regression test are committed alongside
  (`decision_trust_folder_v2.1.246_keystrokes.md`). Remaining #72 scope (free-text + `dcp:`
  lanes on 2.1.246) stays open; the new 2.1.246 `/rc` status-bar element is tracked as #73.

## [0.4.9] — 2026-08-25

The "a new session that asks you to trust the folder is not a failure" release.

### Added
- **The folder-trust card (GH #65).** A brand-new Claude Code window opens on "Do you trust the
  files in this folder?", which never reaches a transcript and never registers a session — so the
  creation flow read it as a hook timeout, KILLED the fresh window, and reported a failed session.
  The inline wait is now a classifying one: while the pane positively shows the trust prompt, the
  picker card becomes a 🔐 trust card with **Trust this folder** and **Cancel — close the window**.
  Trust drives the live prompt through the shipped navigate → verify → Enter discipline (arrows
  never commit, the pre-Enter verify is the only licence to press Enter, digits are forbidden
  because they commit instantly); Cancel closes the window without typing a single key. Answering
  in tmux works too — the bot notices, binds the topic, and replays your first message.
- **Per-creation version licensing that works on Linux/WSL.** The window is created without its
  launch command, and the bot runs a nonce-delimited `--version` probe *in that pane's own shell*
  (preserving any `NAME=value` prefix from `CLAUDE_COMMAND`, and requiring a literal
  `N.N.N (Claude Code)` reply) before launching. That is the version the pane will actually run, so
  the keystroke licence no longer depends on `pane_current_command`, which reports a bare `claude`
  on Linux/WSL. A failed probe simply makes the card display-only; it never blocks or delays the
  launch. Characterised versions: 2.1.204, 2.1.206, 2.1.207, 2.1.239, 2.1.241.
- **Three new environment variables**, all documented in the README:
  `CC_TELEGRAM_TRUST_PROMPT_CEILING_S` (how long a trust prompt keeps the window alive; `0`
  disables the lane), `CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S` (grace on top of
  `CC_TELEGRAM_HOOK_TIMEOUT`, restarted whenever you answer the trust prompt), and
  `CC_TELEGRAM_TRUST_CARD_DISPATCH` (default on; an explicit
  `CC_TELEGRAM_DECISION_DISPATCH=false` turns this lane off too).

### Fixed
- **A window the bot cannot read is left alive, not killed.** Every kill is now a typed decision
  with an explicit ownership re-check immediately before it, and the outer observation ceiling's
  terminal action *spares* the window, releases the topic and tells you to use "Bind to Existing
  Window". A dead pane that still shows the trust prompt text (Claude Code leaves it on screen
  after a commit or Escape) is recognised by the process running in the pane, so it is cleaned up
  rather than shown as a live card.
- **A message sent while a new session is starting is queued, not swallowed.** Text, photos and
  documents arriving into a topic whose session is still coming up now re-read the topic's state
  after their own downloads finish: if the session bound in the meantime the message is delivered
  normally, and if the trust prompt is still up the message is queued and you get a short nudge
  instead of a second directory browser.
- **Closing a topic (or `/start`) during session creation now cleans up.** The no-binding branch of
  topic-close previously skipped teardown entirely, leaving the wait running and the new window
  orphaned.

## [0.4.8] — 2026-08-25

The "multi-question prompts get their details card back" release.

### Fixed
- **Every multi-question AskUserQuestion on Claude Code 2.1.237 lost its "📋 full details" card
  and showed a question clipped mid-sentence.** 2.1.237 started drawing a multi-question prompt's
  question text inside a left `│` gutter box, wrapped across lines. The bridge read only the first
  physical line, gutter glyph included, so it no longer matched the question the PreToolUse hook
  had recorded — the details card (the only place per-option descriptions live) was dropped on
  every such prompt, and the picker card's preamble was the same gutter-prefixed fragment, cut off
  mid-sentence. Question text is now compared with the gutter stripped from the PANE-observed
  side only (the recorded question text is never canonicalized — a record that itself begins
  with a gutter glyph fails closed), and the wrapped lines are rejoined for display. Single-question prompts were never affected.
- **The picker preamble shows the whole question again.** The preamble is still capped so the
  option rows can't be pushed off the card, but it now clips the full question instead of a
  fragment, and the `│` glyph is gone. The "📋 full details" card itself is uncapped, so a long
  boxed question is no longer truncated in the one place that promises the whole thing.

## [0.4.7] — 2026-08-24

The "your approval card stops blinking at you" release.

### Fixed
- **A live approval card no longer churns (send → delete → resend) while Claude narrates
  (GH #67).** Any parent block reaching the bridge tore down the topic's interactive card, the
  poller re-detected the unchanged pane and republished it, and each delete re-posted the phantom
  "🔔 needs a decision" card — a loop that ran for as long as the prompt stayed up. The teardown is
  now conditioned on what the delivered block actually proves: a pane-detected Permission /
  Workflow / Decision gate has no transcript resolution event at all, so a narration block never
  clears it (only the existing absent-streak tombstone does), and a backlog block that predates the
  published card is ignored. A genuine AskUserQuestion / ExitPlanMode `tool_result` still clears
  its own surface immediately, including one that raced a slow card send.
- **A stale AskUserQuestion answer can no longer damage the AUQ that replaced it.** An older AUQ's
  `tool_result` (or its ~60 s AFK auto-resolve) arriving while a newer prompt is live used to
  unlink the new prompt's PreToolUse side file and release the window's action-ledger rows — dead
  buttons and a false "Action already received" for the rest of that prompt's life. Both teardown
  paths are now identity-gated: a resolution proven to belong to a different prompt is skipped, and
  one whose card a different surface has replaced retires only its own state.
- **Honest churn logging.** The poller's "content changed — refreshing keyboard" line now
  distinguishes a genuine content change from a re-publish after an external clear, and a parent
  block arriving without a transcript timestamp is warned about once per topic (it makes the
  stale-block check fail open for that topic).

## [0.4.6] — 2026-08-24

The "two topics can browse directories at the same time" release.

### Fixed
- **Directory/session picker state is now keyed per topic, so a picker open in one topic no longer
  displaces another topic's pending picker (GH #66).** Pending-picker state moved from a flat
  per-user slot to a per-thread map, letting multiple topics browse concurrently. The stale-topic
  mismatch machinery this displacement required is gone with it; a missing/invalid entry remains
  the restart-orphan signal.
- **Same-topic window creation race can no longer kill the winning window (GH #63 §2b).** An abort
  may kill a created window only when it is neither the topic's current bound window nor the live
  pending owner's window, decided atomically inside the same ownership critical section the abort
  uses — never a separate peek-then-kill.
- **An expired/orphaned picker card now disables itself** — tapping it (or `/start`) edits the card
  to an inert "picker expired — send a message here to reopen" notice instead of a popup-only
  reject.

## [0.4.5] — 2026-08-24

The "your AskUserQuestion details show up before the picker again" release.

### Fixed
- **AUQ context card ("📋 … full details") now posts BEFORE the picker card again (regression from the
  monitor head-of-line fix).** Removing the ~5 s per-tick transcript parse restored the status poller to a
  true ~1 Hz on large sessions, which exposed a first-publish race: the poller grabbed a fresh
  AskUserQuestion pane before it settled, the context source bailed, and the picker card published before
  the details card (which then landed below it). The first picker publish of a fresh AUQ is now deferred
  until the context source resolves, so details and picker emit together (details first) in one send. The
  deferral is bounded by a monotonic per-route counter that is only cleared by a real publish or the
  interactive-clear lifecycle — never reset by a transient pane frame — so the card can never be suppressed.

## [0.4.4] — 2026-08-24

The "the bridge runs on Linux/WSL again" release.

### Fixed
- **Linux/WSL: delivery gate now accepts the `claude` executable-name shape tmux reports on Linux
  (`/proc/<pid>/comm`), not just the macOS version-string process title; `node` stays excluded
  (GH #63).** On Linux, tmux derives `pane_current_command` from the executable name, so the
  running TUI reports `claude` rather than a version string like `2.1.201`; the fail-closed
  delivery gate therefore refused every send, making the bridge unusable on Linux/WSL. The
  `pane_command_is_claude` proof-of-life predicate now accepts the `claude` binary name (and a
  full path via its basename) alongside the version shape. `node` remains excluded on purpose —
  any Node program would match it, reopening the hazard the gate exists for.

## [0.4.3] — 2026-08-21

The "a new Claude Code can't wedge your quoted replies" release. One fix, found live twice in a
single day: a reply-quoted (or any tall multi-line) message was typed into the terminal but never
submitted, and the topic wedged behind the stranded-draft brake — the same *symptom* 0.4.1 fixed,
back with a different cause.

### Fixed
- **The delivery gate's status-bar grammar is re-derived for Claude Code 2.1.238 (GH #62).**
  The tall-draft fallback proves the input box by matching the status bar below it against a
  whole-row whitelist grammar, and that grammar's alphabet was pinned on CC 2.1.209–2.1.217.
  CC 2.1.238 rewrote most of the footer — new permission modes (`auto mode on`, now the
  fresh-session default, and `don't ask on`), monitor counts (`1 shell, 1 monitor`, a bare
  `2 monitors`), the current-PR footer link (`PR #309`), memories/feedback-draft counters and a
  batch of new hints — so every tall draft on an ordinary pane was refused with Enter withheld.
  Short messages kept working, which is why only quoted replies wedged.
  - The alphabet is now derived from the CC binary's **own footer renderer** (the versioned
    install ships its JS bundle in plaintext), not from pane sampling: mode texts are bound to
    their real glyphs from the mode table, the tasks slot models the composer's exact variants
    (only the shell/monitor pair comma-joins; mixed task families collapse to
    `N background task(s)`), and counts are bound singular/plural only where the composer's
    `n === 1` conditional proves it.
  - Coupled alphabet fixes: `auto mode on` / `don't ask on` join the idle alphabet (so `/update`
    and `/cost` work on those panes), and leg 3's shell token widens to monitors.
  - Pinned by fresh 2.1.238 rig fixtures (the failing capture flips to deliverable), all eight
    live-sampled status bars, and a recombination/count-shape refusal corpus; zero behavior
    change across the entire pre-2.1.238 fixture corpus.
  - Known limits (all fail-closed refusals, never wrong commits): rebound keybinding chords, the
    IDE `⧉` footer indicator, user-configured footer links, and custom `statusLine` rows remain
    outside the grammar.

## [0.4.2] — 2026-07-22

The "a ghost can't wedge your topic" release. One fix, found live: a topic suddenly refused
every message — including `/clear` itself — with "an autocomplete overlay is open in the
terminal", when nothing had been typed at all.

### Fixed
- **A dim ghost suggestion no longer wedges the delivery gate (GH #60).** Claude Code ≥2.1.206
  renders a contextual *ghost suggestion* in the empty input row (fully dim, SGR-2). After a
  session wound down, CC suggested `/clear` — and the delivery gate, which strips ANSI colors
  before reading the row, saw a bare `/clear` "draft" and treated it as an armed autocomplete
  overlay. Every send was refused pre-write, and since the ghost only disappears when someone
  types, the refusal was self-sustaining until manual terminal intervention. Prose-shaped ghosts
  always passed (which is why this hid for months); any ghost shaped like a bare `/command`, a
  trailing `@word`, or a numbered `1. …` row wedged.
  - The gate's input-box classifier now runs the same SGR-2 ghost-blanking pre-clean the
    stranded-draft release probe already used: a row whose entire post-prompt text is dim blanks
    to an empty row; a real draft or any dim/normal mix is untouched (fail-closed). The
    "real drafts are never dim" empirical basis is re-pinned on a fresh CC 2.1.217 rig capture.
  - The braked-`/esc` recovery path now captures with ANSI, so a brake held up by a ghost-only
    input box releases keylessly instead of firing a pointless double-Escape and staying stuck.
  - Pinned by the verbatim incident row, real + disclosed-synthetic ghost fixtures, a
    transaction-level delivery test, and a classification-equivalence sweep over the full
    127-fixture pane corpus (zero drift).

## [0.4.1] — 2026-07-14

The "your reply-quoted message actually sends" release. Three fixes, all found in live use of
0.4.0: a long or quoted message was typed into the terminal but never submitted (and then wedged
the whole topic), question cards carrying ASCII mockups rendered as garbage, and the bot posted
duplicate copies of its own messages whenever the network was slow.

### Fixed
- **A tall multi-line draft no longer false-refuses, and no longer wedges the topic (GH #56).**
  A reply-quoted message (~700 chars over ~18 rendered rows — under Claude Code's paste-collapse
  threshold, so it renders in full) pushed the input box's top border above the delivery gate's
  fixed 20-line scan window. The gate concluded the input box had vanished, withheld the Enter,
  armed the stranded-draft brake, and then refused the *next* message too. Two legitimate messages
  refused, one left sitting unsent in the pane — on the single most common way of replying.
  - The gate now scans **upward** for the box's top border when only one border is in view,
    authorized by a three-part structural proof that the border it found is really the box's
    bottom: a canonical status bar directly below it, no option-row-shaped line below it, and a
    prompt-glyph row directly under the located top border. The brake's release probe inherits the
    fix through the same seam.
  - **The status-bar recognizer took six review rounds and one approach change.** Each round, a
    different malformed row slipped through (fragment matching, empty segments, cross-products,
    repeated segments, two modes at once, mode + paste-hint, Unicode digits, non-breaking spaces,
    mismatched glyphs) — and every one of those would have let a **live question card be read as a
    ready input box**, i.e. the exact "type into a prompt and commit the highlighted option" hazard
    the 0.4.0 gate exists to prevent. Per-fragment validation was abandoned for a canonical ordered
    grammar in which malformed rows are *unrepresentable* rather than merely rejected.
  - **Soundness is not enough — completeness matters too.** An intermediate design whitelisted whole
    rows drawn from the test fixtures; it was safe, and it would have silently kept the bug alive on
    real machines, because live sessions render status bars (`… · ctrl+t to hide tasks · …`) that the
    fixtures never captured. The shipped grammar is pinned against live-sampled bars as well as the
    fixture corpus.
- **`/esc` can finally clear a stranded draft (GH #56).** On Claude Code 2.1.209 a **single Escape
  clears nothing** — not even a one-line draft — so the refusal message telling you to use `/esc`
  was wrong for *every* draft, not just tall ones. Two rapid Escapes are the only safe full clear
  (Ctrl+U kills just one line; Ctrl+C clears but a second press exits Claude to a bare shell, so the
  bridge never sends it). `/esc` on a braked window now performs that double-Escape — but only after
  proving the box actually holds text, and it sends **zero keystrokes** if a card or an unreadable
  frame is on the pane. Refusal copy corrected to match reality.
- **No more duplicate messages when the network is slow (GH #55).** The MarkdownV2→plain-text
  fallback caught *every* exception, including `TimedOut` — but a client-side timeout does not mean
  the request failed: Telegram usually delivered the formatted message anyway, so the "fallback"
  posted a second, plain copy. The fallback now fires only when the content provably did **not**
  reach Telegram (a `BadRequest` rejection, or a formatting error before the request left). Ambiguous
  transients log and stop.
  - Scoped to the four *send* paths. The edit lanes deliberately keep the broad fallback: an edit
    cannot mint a second message, and removing it would have pushed message recreation up into the
    callers.
  - Trade-off, accepted: a timeout whose request genuinely never arrived now loses that message
    (visible in the log; `/history` and the transcript remain the escape hatch) — better than routine
    duplicates under load.

### Added
- **Option previews in question cards (GH #54).** Claude Code ≥2.1.197 lets an `AskUserQuestion`
  option carry a `preview` — a multi-line ASCII mockup. These panes previously parsed as garbage:
  no details card, no option buttons. Now the mockups render as monospace blocks in the 📋 details
  message, posted before a short labels-only selection card, and the option buttons work — a tap
  navigates, verifies, and commits, including the wrapped-label case where Claude Code drops the `❯`
  cursor and marks the selection with styling alone. Multi-select previews are shown too (the
  terminal doesn't render them at all, so the details card is the only place they're visible).

## [0.4.0] — 2026-07-12

The "safe to type at a live prompt" release. Sending a message while Claude was waiting on a
question card, a plan approval, or a folder-trust dialog used to type your text into the terminal —
where the text was discarded and the trailing Enter **committed the highlighted option**. On a plan
approval that option is *"Yes, and bypass permissions"*, so a stray "ok thanks" could approve a plan
with permissions bypassed. That is now closed twice over: every payload must first prove Claude's
input box is actually there, and on a **question card** your message no longer bounces at all — it
becomes the answer, in your own words, by voice or by text.

### Added
- **Answer a question card in prose (GH #50 PR-2).** A voice note, a typed message, a caption, or a
  quoted reply now *answers* a live `AskUserQuestion` card instead of being refused. The bridge
  navigates to the card's own free-text row, types your words, and commits them. Quoted replies keep
  their quote.
  - **The guard is a landing proof, taken before a single byte is typed:** the row under the cursor
    must be the *dim* `Type something.` placeholder. A rig on Claude Code 2.1.207 established that
    dim holds for exactly one shape — the selected, untyped placeholder — and that a real option row
    is never dim, not even when highlighted. So the bridge cannot begin typing while parked on an
    option, and a mis-identified card cannot commit one. Verified against an overshoot onto a real
    option, an undershoot onto a real option, and the payload `"Yes, but use postgres"` against an
    option literally labelled `Yes`.
  - Card identity is the `PreToolUse` hook's per-invocation `tool_use_id` (mandatory — no id, no
    dispatch), re-read around every capture, with a fresh `session_map.json` generation read and
    structural option-label agreement.
  - **Accepted, disclosed residual:** a successor card with the same option labels, appearing in the
    window between the last look and Enter, can receive the prose meant for its predecessor. Your
    answer reaches a different question; you see it and correct it. It is never an option commit.
  - **Plan approvals are out of scope by decision** — an `ExitPlanMode` card falls through to the
    delivery gate and is refused, with an explanation.
- **README: the two things that actually matter, up front** — that this is in practice a
  bypass-permissions tool (and what that does to your security boundary), and that `/screenshot` is
  the always-available fallback whenever you cannot tell what the terminal is doing.

### Fixed
- **Messages are never typed into a live prompt (GH #50 PR-1).** `deliver_to_window` is now the one
  choke point every payload crosses — text, voice, captions, attachments, forwarded slash commands,
  the late-answer card, the pending-bind replay — and it refuses to write unless it has *positive
  structural proof* of Claude's ready input box. Positive proof, never "no known prompt matched":
  the `Switch model?` dialog is footer-less and the parser is blind to it, so absence-of-match is
  worthless. Every blocking prompt replaces the input box, which is why its presence is the one
  signal that holds for prompts nobody has seen yet.
  - A **stranded-draft brake** stops the follow-on failure: if a payload was typed but its Enter
    withheld, the next message would otherwise append to it and commit both. The window refuses
    further sends until the input box is observed empty (or the window dies).
  - Refusals carry the actual reason and actionable copy, exactly once, on every path.
- **Raw control bytes are refused before any keystroke (GH #50).** `tmux send-keys -l` stops tmux
  interpreting key *names* but passes escape bytes to the terminal verbatim, so a payload carrying
  `ESC [ A` could move the cursor and fire a hotkey before anything was verified. All C0 control
  characters except newline, plus DEL and C1, are now refused with an explanation rather than
  silently stripped. Ordinary line breaks still work, so voice notes and quoted replies are
  unaffected.
- **Long voice notes stopped stranding (GH #50 PR-1 regression).** Claude Code collapses a large
  pasted payload to `[Pasted text #1 +N lines]` **and replaces the status bar** with
  `paste again to expand`. The gate did not know that shape was still a ready input box, so every
  message past ~800 characters — a voice note carrying a reply quote, typically — was refused, left
  stranded in the input box, and braked the topic.
- **`/update` and `/cost` in any topic where a plan had been approved.** Pre-existing and silent:
  after a plan approval Claude Code pins the plan's slug into the input box's top rule, and the
  pure-dashes pattern stopped matching — so `/update` quietly deferred and `/cost` refused, in that
  topic, forever.
- **`/cost` and `/usage` refused for anyone running background agents.** They had inherited
  `/update`'s background-shells guard, which exists only because `/update` *restarts* the session.
  Reading a usage overlay restarts nothing, so a live `· N shell` token is not a hazard for it —
  but it made `/cost` refuse essentially every time for a heavy background-agent user.

## [0.3.0] — 2026-07-10

The "typing truth + supervision surfaces" release: ~159 commits since v0.2.1 making the bridge
tell you honestly what your machine is doing — background agents, Workflows, agent-teams
teammates, and background shells all keep the `typing…` indicator and 🟡/🔔 signals accurate —
plus new bot-side surfaces (`/dashboard`, `/settings`, `/update`, `/cost` + `/usage`, file
downloads, AFK late-answers, opt-in approval/decision cards) that make Telegram a place you can
actually supervise from, not just watch.

### Added
- **Background-agent + Workflow busy signals, made complete.** `run_in_background` Agents, the
  `Workflow` tool, and background shells now keep typing + 🟡 Busy on while they work, even after
  the parent turn ends and even across a bot restart:
  - GH #44 snapshot projection lifts a stored-idle route to visible RUNNING while a live
    background key exists; the ISSUE-6 `wf-task:` Workflow bracket + mtime heartbeat and the
    background-Bash `backgroundTaskId` lane extend it to Workflows and background shells.
  - `↳` sub-agent display cards for Workflow sidechains, collapsing to one line on completion
    (ISSUE-6 Fix 5), plus a startup reconciler that re-lights still-running background Agents and
    Workflows after a `launchctl kickstart`.
  - A persistent, audible **"🔔 Claude needs a decision"** card for approval waits that leave no
    JSONL trace, with a typed clear-reason channel so it dismisses only on genuine resolution.
- **Agent-teams teammate tracking (GH #46).** A teammate spawned into the session is now a
  first-class background key: its park/idle-notification closes the key promptly instead of
  stranding typing/Busy for two hours (PR-1), and a generational registry keeps typing on while a
  teammate genuinely works across the parent's own turns, relights it when re-woken, and never
  strands on a stale same-name sidechain file (PR-2).
- **Background-only "labeled silence" card** — when only a background task is working and the topic
  is otherwise silent, one `⏳ Background work running` line explains the quiet.
- **Cross-topic dashboard** (`/dashboard`) — one owner+chat-scoped overview message listing every
  bound topic, needs-attention-first (🔔 / 🟡 / ⚪ / ⏳), repainted by the poller; `/dashboard pin`
  opt-in.
- **Per-user output verbosity** (`/settings`) — `verbose` / `standard` / `compact` / `quiet`
  presets plus per-knob overrides, persisted per user; the activity card collapses to a one-line
  summary when a turn ends (per-policy).
- **Artifact download lane** — a `📎` tap-to-download card when Claude names a deliverable file, and
  a durable `/file <path>` escape hatch. Every offer is filesystem-validated (containment,
  `O_NOFOLLOW`, size cap, fd-based upload) and confined to the session directory or a configured
  artifact root. Covers docs/images/audio/video/archives/office/data; source-code paths are never
  offered.
- **AFK late-answer cards.** On Claude Code ≥2.1.198 an unanswered AskUserQuestion self-resolves at
  ~60s; instead of deleting the picker, the bridge converts it in place to an honest
  "⏰ Claude proceeded after ~60s" card whose buttons deliver your choice as a normal course-correction
  message.
- **`/update`** (owner-only) — updates the Claude Code CLI and restarts idle sessions in place
  (`claude --resume`, routing preserved). Scoped to the invoking topic by default; `/update all`
  walks every bound topic. Idle-only, fail-closed, single-flight, with a post-`/exit` quarantine
  that refuses to type into a window until Claude is proven alive again.
- **`/cost` and `/usage`** — bot-side interceptors for the usage/limits TUI overlay (which writes
  no JSONL and would otherwise freeze the topic): idle preflight → capture → parse → conditional
  auto-dismiss. When the session is busy, they reply with a bridge-side snapshot (context usage +
  cached limits) instead of a dead-end refusal.
- **Opt-in approval + decision cards** (flag-gated, default off) — Permission and Workflow-launch
  gates (`CC_TELEGRAM_PERMISSION_PROMPTS`) and generic numbered confirmation prompts
  (`CC_TELEGRAM_DECISION_CARDS`) surface as cards; `CC_TELEGRAM_DECISION_DISPATCH` adds verified
  one-tap dispatch for prompt families and Claude Code versions cc-telegram has explicitly
  characterised.
- **Machine-surface window geometry** — bot-created tmux windows default to `160×50`
  (`CC_TELEGRAM_WINDOW_GEOMETRY`) so a tall picker stays fully on-screen and wide option labels
  stop overflowing; the terminal is a machine surface, so the geometry serves the parser.
- **WSL session-binding support** — `CC_TELEGRAM_HOOK_TIMEOUT` and a tmux-3.4-compatible field
  separator, ported from the original repo, plus a directory-browser fix for Windows mounts.

### Changed
- **`/update` is topic-scoped by default** (owner decision 2026-07-10) — the fleet walk revived
  idle sessions in dormant topics, and a revived idle session is not free (background token drip),
  so fleet-wide restarts moved behind the explicit `/update all`. The scoped form re-resolves its
  target window after the up-to-120s CLI phase, so a topic rebound mid-update still restarts the
  right window.
- **True typing cadence for multi-topic forums** — `typing_action_loop` now holds a real
  start-to-start interval (elapsed-compensated), and `sendChatAction` (typing) is exempted from
  Telegram's per-group 20/60s bucket via a new `TypingAwareRateLimiter`, so the indicator no longer
  blinks with ≥2 busy topics and typing no longer starves content sends.
- **Per-key background-agent TTLs** — launched/`is_background` keys age by a 2 h TTL, foreground-
  presumed keys keep the 30-min one, unifying the typing story across sync and async work.

### Fixed
- **CC 2.1.206 ghost-suggestion false refusals** — dim (SGR-2) contextual suggestion text in the
  empty input row read as a typed draft, causing false `/cost` refusals and `/update` deferrals; a
  full-SGR-state-machine pre-clean blanks a fully-dim ghost while leaving a real draft untouched
  (fail-closed).
- **`/cost` dead-end refusals** — a busy/draft refusal now always appends a bridge-side snapshot
  rather than leaving the user with nothing.
- **AskUserQuestion v2.1.168 dispatch regression** — a bare digit no longer reliably selects, so
  the pick now navigates the cursor to the target, verifies, and presses Enter, recording
  `dispatched` only after the pane confirms the exact advance (restart-safe via the action ledger +
  mint-intent store).
- **Queue-shaped `<task-notification>` close miss (CC 2.1.198)** — a background task completing
  while the parent is busy lands as a `queue-operation`/`enqueue` entry; the parser now synthesizes
  the close so typing drops at completion instead of stranding to the TTL.
- **`idle_prompt` false 🔔** — CC 2.1.204's ~60s post-turn idle nudge is dropped at the notification
  trust boundary (permission prompts and unknown kinds fail open), killing a spurious
  "🔔 Waiting on you" + typing-dark after every turn end.
- **Prose ↔ picker ordering** — long findings prose posted before an AskUserQuestion picker is now
  split at Telegram's 4096-char limit, so it appears before the card instead of failing silently.
- Numerous AUQ card-liveness, source-parity, and restart-recovery correctness fixes carried forward
  from the 0.2.x line.

### Notes
- The package version is bumped per release, not per deploy. Always deploy with
  `uv tool install --force --no-cache .`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- The approval, decision, and decision-dispatch cards are all off by default; enable them per the
  `CC_TELEGRAM_*` flags in the README once you have characterised your Claude Code version.

## [0.2.1] — 2026-06-24

### Fixed
- **AskUserQuestion descriptions card was suppressed for recommended options.** An AUQ whose
  recommended option label ended in the literal `(Recommended)` lost its `📋 AskUserQuestion —
  full details` message (the separate, multi-part-splittable message posted *before* the picker
  card). The pane parser strips `(Recommended)` into a structured flag, but the PreToolUse
  side-file label keeps it verbatim, so the pane-consistency predicate false-mismatched and the
  render resolver bailed (`bail_label_mismatch`) — dropping the descriptions for the *same*
  question (observed live on a busy topic; recurred on every AUQ whose recommended option carried
  the suffix). Fixed by normalizing the trailing recommended suffix on both sides of the
  side-file↔pane label compare (`auq_source._strip_recommended`, reusing the parser's
  `_RE_RECOMMENDED`); confined to the suffix only, so wrong-question protection and mint/validate
  parity are unchanged. The details-message and picker rendering are untouched. Peer-reviewed
  (Codex + Hermes, both PASS); RED-first tests added.

## [0.2.0] — 2026-06-24

The "busy-signal + AskUserQuestion bridge" release: ~190 commits since v0.1.0 making Telegram a
faithful mirror of what Claude Code is actually doing — interactive prompts, background work, and
run-state — plus a deployment-docs pass so another operator (or code agent) can stand the bot up
from scratch.

### Added
- **Cross-topic dashboard** (`/dashboard`) — one owner+chat-scoped overview message listing every
  bound topic grouped needs-attention-first (🔔 / 🟡 / ⚪), repainted by the status poller; `/dashboard pin` opt-in.
- **Per-user output verbosity** (`/settings`) — `verbose` / `standard` / `compact` / `quiet` presets
  plus per-knob overrides (tool-line length, done-card policy, sub-agent cards, 👤 echo, 📊 footer),
  persisted per user in `state.json`. Production default is `standard`.
- **"🔔 Waiting on you" detection** via a new matcher-less `Notification` hook + `notify_pending/`
  side files — covers permission/approval gates (including the Workflow tool's Bash-approval gate)
  that leave no JSONL trace, with a persistent, audible decision card.
- **Live prose before interactive prompts** via a bot-managed `MessageDisplay` hook
  (`md_hook_settings.json` + `msg_display/` capture) — explanatory prose written in the same turn as
  an `AskUserQuestion` / `ExitPlanMode` is delivered *before* the picker, not after resolution.
- **ExitPlanMode plan body before the picker card** (findings → 📋 Plan → card ordering).
- **Background-agent + Workflow run-state** — `run_in_background` Agents and the `Workflow` tool now
  light typing + 🟡 Busy while they work (GH #44 snapshot projection + the ISSUE-6 Workflow bracket),
  with `↳` sub-agent display cards that collapse on completion, and a startup reconciler that
  re-lights still-running background work across a restart.
- **Background-jobs decoration** (GH #43) — `⏳ N background job(s)` on collapsed done-cards + the
  dashboard glyph, parsed from the pane.
- **Docs / deploy ergonomics** — `docs/DEPLOYMENT.md` (end-to-end setup + the `--no-cache` upgrade
  recipe + troubleshooting), top-level `AGENTS.md`, and `bin/install-service.sh` to generate + load
  the `com.cc-telegram` LaunchAgent. Log-rotation LaunchAgent (`bin/install-log-rotate.sh`).
- **Post-turn digest collapse** — the activity card collapses to a one-line summary when the turn
  ends; per-sub-agent cards collapse the same way.

### Changed
- **`route_runtime` is now the sole run-state / context-usage / idle-clear authority** — the old
  `busy_indicator` and observer/callback fan-out (root cause of bug c313657) were removed in favor of
  a pull-only per-route state machine with immutable snapshots.
- **AskUserQuestion pick dispatch navigates the cursor to the target and presses Enter** (validated
  against Claude Code v2.1.168, where a bare digit no longer reliably selects), recording the ledger
  `dispatched` lock only after the pane confirms the expected advance. Restart-safe via an
  append-only action ledger + a durable mint-intent store.
- Interactive-surface teardown is now **parent-only (sidechain-gated)** — a background agent
  narrating no longer tears down the parent's live AUQ/EPM/Permission card.

### Fixed
- **Typing indicator stayed dark for the full 30-min TTL** while a background agent worked
  (parent idle) — `BG_RUNNING` now clears the projected-busy 🔔 on the agent's next heartbeat
  (scoped to the sole-live-plain-Agent shape for safety).
- **AUQ "📋 full details" ctx-card ~28× duplication** in a busy topic while a background Workflow ran.
- **AUQ picker-card churn / duplicate cards** on long-open cards in busy topics (pane↔pane drift
  no-op + transient-edit-keep).
- **Claude Code v2.1.170 interactive-UI detection drift** (EPM footer `ctrl-g`→`ctrl+g` + a new
  "Settings Warning" marker) that hid both the picker and the findings prose.
- Out-of-order JSONL tool pairing / stuck-route eligibility (GH #42).
- Numerous AUQ card-liveness, source-parity, and restart-recovery correctness fixes.

### Notes
- The package version is bumped per release, not per deploy. Always deploy with
  `uv tool install --force --no-cache .` (the wheel cache is version-keyed; without `--no-cache`,
  same-version redeploys reinstall a stale wheel). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## [0.1.0] — 2026-05-17

Initial tagged release: Telegram ↔ Claude Code bridge, topic-only architecture
(1 Topic = 1 tmux window = 1 Claude session), `SessionStart` hook session tracking,
per-route message queues, MarkdownV2 output, streaming tool/thinking/status, photos + voice,
reply context, and SQLite provenance.

[0.3.0]: https://github.com/etcircle/cc-telegram/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/etcircle/cc-telegram/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/etcircle/cc-telegram/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/etcircle/cc-telegram/releases/tag/v0.1.0
