"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, RestoreCheckpoint,
    Settings) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

PermissionPrompt and BashApproval detection has been intentionally removed:
the deployment runs Claude Code with ``--dangerously-skip-permissions``
(YOLO mode), so neither prompt ever renders in the pane and the patterns
were dead code wasting capture cycles. ExitPlanMode and AskUserQuestion
remain because they still appear in the JSONL stream as ``tool_use``
events and are also detected via pane scrape as a redundant safety net.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    # Plain single-select AskUserQuestion (no checkbox glyphs). Claude Code
    # renders simple A/B/C/D questions as numbered options + ``Enter to select``
    # footer, with no leading ☐/✔/☒. The two patterns above only match the
    # multi-select / multi-tab variants. This pattern catches the rest.
    # Top anchor is a numbered option line; the cursor prefix varies across
    # Claude Code versions (❯, ›, ▶, *, ), >) or may be plain indent.
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[❯›▶*)>]?\s*\d+\.\s+\S"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=0,
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# ── AskUserQuestion structured parser ───────────────────────────────────
#
# Background: ``extract_interactive_content`` above answers "is there an
# AskUserQuestion picker on screen?" and returns the raw pane region for
# verbatim relay to Telegram. That's enough to surface the picker, but
# leaves the user with arrow-key buttons on a phone — useless for
# multi-tab forms with 4+ options per question.
#
# This parser produces a structured view of the same region so a future
# renderer (PR 2) can build option buttons matched to each tab and
# question, and a callback handler can validate that the form hasn't
# shifted under it before dispatching keystrokes.
#
# Strict-or-``None`` rule, per peer review: any partial / ambiguous /
# mid-redraw parse returns ``None`` so the existing keystroke fallback
# stays in charge. Hermes flagged this as load-bearing.
#
# Anchor lines (multi-tab):  ``^\s*←\s+[☐☒✔]``  (tab header)
# Anchor lines (single-tab): a numbered-options block ending in
#                            ``Enter to select``.
#
# Pane-text is an unstable adapter — Claude Code reworks its TUI between
# versions. The parser is biased toward returning ``None`` rather than
# guessing when markers shift. Fixture coverage in tests is the safety net.


# Matches a tab cell: state glyph (☐ ☒ ✔) followed by optional label.
# The submit cell is sometimes rendered as ``✔`` with no label, sometimes as
# ``✔ Submit``. Both are valid.
_RE_TAB_CELL = re.compile(r"(?P<state>[☐☒✔])\s*(?P<label>[^☐☒✔→]*?)\s*(?=[☐☒✔]|→|$)")

# Matches the multi-tab header line: ``←  ☐ X  ☒ Y  ✔ Submit  →`` (or similar).
# The trailing ``→`` is required so we don't confuse this with a stray ``←``
# in narrative text.
_RE_TAB_HEADER = re.compile(r"^\s*←\s+(?P<body>.*?)\s*→\s*$")

# Matches a numbered option: ``❯ 1. Some option label`` or ``  2. Another``.
# Cursor markers Claude Code uses: ❯, ›, ▶, * .
_RE_NUMBERED_OPTION = re.compile(
    r"^(?P<cursor>[❯›▶*]\s+|\s{2,})(?P<num>\d+)\.\s+(?P<label>.+?)\s*$"
)

# Matches the picker's "Enter to select / Tab / Esc" footer.
_RE_PICKER_FOOTER = re.compile(r"Enter to select")

# Matches the review-screen footer that asks the user to confirm submission.
_RE_REVIEW_HEADER = re.compile(r"^\s*Review your answers\s*$")
_RE_SUBMIT_PROMPT = re.compile(r"^\s*Ready to submit your answers\?\s*$")

# Matches a free-text "Type something" option (variant where the user can
# type free text instead of picking a numbered option).
_RE_FREE_TEXT_OPTION = re.compile(r"Type something")

# Matches ``(Recommended)`` suffix on an option label.
_RE_RECOMMENDED = re.compile(r"\(Recommended\)\s*$")


@dataclass(frozen=True)
class AskOption:
    """One picker option inside an AskUserQuestion form."""

    label: str  # e.g. "C — Parallel tracks: stabilize core + scaffold copilot"
    recommended: bool  # True if "(Recommended)" suffix present
    cursor: bool  # True if this option is the current selection (❯ / › prefix)
    number: int | None  # 1-9 numeric shortcut, or None when not rendered


@dataclass(frozen=True)
class AskTab:
    """One question-tab in a multi-question AskUserQuestion form."""

    label: str  # e.g. "Approach" — may be empty for the submit cell
    answered: bool  # ☒ filled (question has an answer)
    is_submit: bool  # ✔ marker — the synthetic "Submit" cell
    is_current: bool  # the tab the user is currently viewing


@dataclass(frozen=True)
class AskUserQuestionForm:
    """Structured snapshot of the AskUserQuestion picker visible in a pane.

    The shape covers three Claude Code variants:

    1. Single-question, numbered options: ``tabs == ()``, ``options`` is
       populated. Footer is ``Enter to select``.
    2. Multi-tab form mid-navigation: ``tabs`` populated, ``options`` is the
       set visible under the current tab.
    3. Multi-tab form on the review screen: ``is_review_screen == True``.
       ``options`` may still be populated with the two submit/cancel rows.

    A parse always carries the raw pane excerpt for verbatim fallback
    rendering. The ``fingerprint`` method gives a stable hash over the
    structured fields so callbacks can verify the form hasn't shifted
    between display and dispatch.
    """

    tabs: tuple[AskTab, ...] = ()
    current_question_title: str | None = None
    options: tuple[AskOption, ...] = ()
    is_review_screen: bool = False
    is_free_text: bool = False
    pane_excerpt: str = ""
    # Source-of-truth fields used in fingerprinting are above this line.
    # Anything appended below MUST be excluded from ``_canonical_repr`` so
    # adding diagnostic state doesn't break callback tokens minted by
    # earlier renders.
    _meta: dict[str, str] = field(default_factory=dict, compare=False)

    def _canonical_repr(self) -> str:
        """Stable string form used by ``fingerprint``.

        Excludes ``pane_excerpt`` (carries cursor noise and re-flows on
        redraw) and ``_meta`` (diagnostic). Order is fixed; if you add a
        field that should influence callback freshness, append a new line
        here — don't reorder existing ones.
        """
        tabs_str = "|".join(
            f"{t.label}:{'A' if t.answered else 'E'}"
            f":{'C' if t.is_current else '_'}"
            f":{'S' if t.is_submit else '_'}"
            for t in self.tabs
        )
        opts_str = "|".join(
            f"{o.number}:{o.label}"
            f":{'R' if o.recommended else '_'}"
            f":{'C' if o.cursor else '_'}"
            for o in self.options
        )
        return "\n".join(
            [
                f"TABS:{tabs_str}",
                f"Q:{self.current_question_title or ''}",
                f"OPTS:{opts_str}",
                f"RVW:{'1' if self.is_review_screen else '0'}",
                f"FT:{'1' if self.is_free_text else '0'}",
            ]
        )

    def fingerprint(self) -> str:
        """Stable 16-char hex digest over the structured form state.

        Used by the (PR 2) renderer to mint callback tokens. On click, the
        handler reparses the pane and compares fingerprints — a mismatch
        means the form changed under us (user navigated, skill advanced,
        Claude Code redrew) and the click must not be dispatched verbatim.
        """
        return hashlib.sha1(self._canonical_repr().encode()).hexdigest()[:16]


def _parse_tab_header(line: str) -> tuple[AskTab, ...] | None:
    """Parse ``←  ☐ X  ☒ Y  ✔ Submit  →`` into a tuple of ``AskTab``.

    Returns ``None`` if the line doesn't look like a tab header. Empty tab
    list is treated as a parse failure too — a header with no cells is
    indistinguishable from noise.
    """
    m = _RE_TAB_HEADER.match(line)
    if m is None:
        return None
    body = m.group("body")
    cells: list[AskTab] = []
    # _RE_TAB_CELL uses a lookahead so cells are matched left-to-right with
    # no consumption past the next state glyph. ``finditer`` walks the body
    # in order.
    for cm in _RE_TAB_CELL.finditer(body):
        state = cm.group("state")
        label = cm.group("label").rstrip(":").strip()
        cells.append(
            AskTab(
                label=label,
                answered=state == "☒",
                is_submit=state == "✔",
                # ``is_current`` is reconstructed later — the header line
                # alone doesn't say which tab is being viewed (Claude Code
                # marks the current tab by what's rendered below the
                # header, not by the cell glyph).
                is_current=False,
            )
        )
    if not cells:
        return None
    return tuple(cells)


def _parse_numbered_options(lines: list[str]) -> tuple[AskOption, ...]:
    """Walk lines top-down collecting consecutive numbered options.

    Stops at the first non-option, non-blank line so a description line
    following an option doesn't get folded into the next option's label.
    Returns ``()`` if no numbered options are found or numbering has a
    gap (a gap usually means we're mid-redraw — caller should treat as a
    parse failure).
    """
    options: list[AskOption] = []
    for line in lines:
        m = _RE_NUMBERED_OPTION.match(line)
        if m is None:
            if options:
                # Allow a trailing blank or description line only if we
                # haven't started collecting; once we start, the block must
                # be contiguous.
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(("·", "—", "-", "▸")):
                    # description continuation for the previous option
                    continue
                break
            continue
        try:
            num = int(m.group("num"))
        except ValueError:
            return ()
        label = m.group("label").strip()
        cursor = m.group("cursor").strip() in ("❯", "›", "▶", "*")
        recommended = bool(_RE_RECOMMENDED.search(label))
        if recommended:
            label = _RE_RECOMMENDED.sub("", label).rstrip()
        options.append(
            AskOption(
                label=label,
                recommended=recommended,
                cursor=cursor,
                number=num,
            )
        )
    # Contiguity guard: trim the block to the prefix that runs 1..N without
    # gaps. The picker doesn't skip numbers within a question, but trailing
    # special rows like ``0. Dismiss`` (Claude Code's feedback survey) break
    # the numeric run — those are dropped from the structured view so PR 2
    # can render options 1..N cleanly. The keystroke fallback (Enter/digit
    # keys) still reaches them.
    kept: list[AskOption] = []
    for expected, opt in enumerate(options, start=1):
        if opt.number != expected:
            break
        kept.append(opt)
    return tuple(kept)


def parse_ask_user_question(pane_text: str) -> AskUserQuestionForm | None:
    """Structured parse of the AskUserQuestion picker in ``pane_text``.

    PR 1 surface: pure parser, no caller change. Returns ``None`` when the
    pane does not contain a recognizable AskUserQuestion picker, or when
    the parse is ambiguous (mid-redraw, unknown variant, gaps in
    numbering). The keystroke-keyboard fallback in ``handle_interactive_ui``
    stays in charge for ``None`` returns.

    Detection is anchored on one of:
      * a multi-tab header line (``← ☐ X  ☒ Y  ✔ Submit →``)
      * a numbered-options block followed by ``Enter to select``

    Returns ``AskUserQuestionForm`` with whichever fields were extractable.
    Empty / partial fields are preserved (e.g. mid-redraw tab header with
    no visible options yet → ``options=()`` rather than ``None``) so the
    fingerprint can still detect that the form is on a particular tab.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Locate the lowest-on-screen tab header (most recent redraw wins).
    # We scan bottom-up so a stale header earlier in the scrollback does
    # not shadow the live one.
    tab_header_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_TAB_HEADER.match(lines[i]):
            tab_header_idx = i
            break

    # Detect picker footer or review-screen markers anywhere in the last
    # ~25 lines — the live picker stays near the bottom of the pane.
    tail = lines[-25:]
    has_footer = any(_RE_PICKER_FOOTER.search(line) for line in tail)
    is_review = any(_RE_REVIEW_HEADER.match(line) for line in tail) and any(
        _RE_SUBMIT_PROMPT.match(line) for line in tail
    )
    is_free_text = any(_RE_FREE_TEXT_OPTION.search(line) for line in tail)

    if tab_header_idx is None and not has_footer and not is_review:
        return None

    tabs: tuple[AskTab, ...] = ()
    if tab_header_idx is not None:
        parsed_tabs = _parse_tab_header(lines[tab_header_idx])
        if parsed_tabs is None:
            return None
        tabs = parsed_tabs

    # Collect options below the tab header (multi-tab) or in the tail
    # window (single-tab). For multi-tab, options live between the header
    # and the next separator / next tab header / picker footer.
    if tab_header_idx is not None:
        end_idx = len(lines)
        for j in range(tab_header_idx + 1, len(lines)):
            line = lines[j]
            if _RE_TAB_HEADER.match(line):
                end_idx = j
                break
            stripped = line.strip()
            if stripped and all(c == "─" for c in stripped):
                end_idx = j
                break
        options_region = lines[tab_header_idx + 1 : end_idx]
    else:
        options_region = tail

    options = _parse_numbered_options(options_region)

    # Question title heuristic: the first non-empty, non-separator line
    # *above* the options block that doesn't look like an option / tab /
    # separator. None when no such line is available within a small window.
    current_question_title: str | None = None
    search_top = tab_header_idx + 1 if tab_header_idx is not None else 0
    for line in options_region:
        stripped = line.strip()
        if not stripped:
            continue
        if _RE_NUMBERED_OPTION.match(line):
            break
        if _RE_TAB_HEADER.match(line):
            continue
        if all(c == "─" for c in stripped):
            continue
        current_question_title = stripped
        break
    _ = search_top  # reserved for a future "search bounded above tabs" tweak

    # Build a pane excerpt for verbatim fallback rendering. We pin it to the
    # tab header (if any) or the last ~25 lines otherwise — the renderer in
    # PR 2 won't use the full pane scrollback.
    excerpt_start = (
        tab_header_idx if tab_header_idx is not None else max(0, len(lines) - 25)
    )
    pane_excerpt = "\n".join(lines[excerpt_start:]).rstrip()

    return AskUserQuestionForm(
        tabs=tabs,
        current_question_title=current_question_title,
        options=options,
        is_review_screen=is_review,
        is_free_text=is_free_text,
        pane_excerpt=pane_excerpt,
    )


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def _find_chrome_separator(lines: list[str]) -> int | None:
    """Locate the topmost ``──`` chrome separator in the last 10 lines."""
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return i
    return None


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears above the chrome
    separator (a full line of ``─`` characters). We locate the separator
    first, then check the lines just above it — this avoids false
    positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    Note: blank lines between the spinner and the chrome are tolerated
    here (the post-completion summary case). To distinguish "Claude is
    actively running" from "post-completion summary", use
    ``is_status_active`` instead.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")
    chrome_idx = _find_chrome_separator(lines)
    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


def is_status_active(pane_text: str) -> bool:
    """Return True iff Claude is actively producing output.

    The reliable signal is the literal ``esc to interrupt`` in the bottom
    chrome bar — Claude only renders that hint while a run is in flight.
    The spinner glyph and the spinner-line text are NOT reliable: Claude
    keeps the spinner+summary line ("✻ Cooked for 2s") visible after a
    run completes, and the gap above the top chrome is the same in both
    active and idle states (Claude always inserts a blank line there).

    Examples:

        Actively running (returns True)::

            ✽ Brewing… (3s · thinking with high effort)

            ──────────────────────────────────
            ❯
            ──────────────────────────────────
              ⏵⏵ bypass permissions on · esc to interrupt

        Post-completion summary (returns False)::

            ✻ Cooked for 2s

            ──────────────────────────────────
            ❯
            ──────────────────────────────────
              ⏵⏵ bypass permissions on (shift+tab to cycle)
    """
    if not pane_text:
        return False

    # Search the last 8 lines so we catch the bottom chrome bar without
    # paying for a full pane scan on every poll.
    last_lines = pane_text.split("\n")[-8:]
    return any("esc to interrupt" in line.lower() for line in last_lines)


# ── Context-window indicator ─────────────────────────────────────────────

# Matches Claude Code's chrome footer line, e.g.
#   "  [Opus 4.6] Context: 89%"
#   "  [Sonnet 4.5] Context: 7%"
_RE_CONTEXT_PCT = re.compile(r"\bContext:\s*(\d{1,3})%")


def extract_context_pct(pane_text: str) -> int | None:
    """Extract the Context-window percentage from Claude Code's chrome.

    Scans the bottom 10 lines for a ``[<model>] Context: NN%`` pattern.
    Returns the integer (0-100) or ``None`` if no match is found or the
    parsed value is out of range. Pure parser — no I/O, no caching.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")
    for line in lines[-10:]:
        match = _RE_CONTEXT_PCT.search(line)
        if match:
            try:
                pct = int(match.group(1))
            except ValueError:
                continue
            if 0 <= pct <= 100:
                return pct
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
