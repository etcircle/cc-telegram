"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, RestoreCheckpoint,
    Settings, and — behind the ``CC_TELEGRAM_PERMISSION_PROMPTS`` flag —
    Permission and Workflow approval gates) via regex-based UIPattern
    matching with top/bottom delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Permission / Workflow approval-gate detection is RE-ENABLED behind the
``CC_TELEGRAM_PERMISSION_PROMPTS`` flag (default OFF). It was removed in
Wave 2 on the assumption the deployment always runs Claude Code with
``--dangerously-skip-permissions`` — but bridged user-launched (resumed,
non-bypass) sessions DO render tool-permission prompts, and the ``Workflow``
tool's own dynamic-workflow-launch gate fires even under bypass. When the
flag is ON, ``Permission`` (tool-permission prompts) and ``Workflow`` (the
dynamic-workflow-launch approval) surface as cards. As of PR-1 they are
DISPLAY-ONLY (a labels card + the existing manual ↑/↓/⏎/Esc nav keyboard);
no semantic option-button dispatch yet. ExitPlanMode and AskUserQuestion
remain detected unconditionally (they also appear in the JSONL stream as
``tool_use`` events and are detected via pane scrape as a redundant safety
net).

A SECOND, independent flag ``CC_TELEGRAM_DECISION_CARDS`` (default OFF) gates a
last-priority generic ``Decision`` pattern (Stage B1) that surfaces titled
numbered-option confirmation prompts no NAMED pattern covers (the "Switch
model?" / folder-trust family) as a display-only card. It is strict-or-None
with a Permission/Workflow veto so it never shadows a named pattern or
re-surfaces a flag-OFF gate.

Both flags are LOCAL ``os.getenv`` reads (``_PERMISSION_PROMPTS_ENABLED`` /
``_DECISION_CARDS_ENABLED``, re-readable via ``reset_for_tests`` /
``set_permission_prompts_enabled`` / ``set_decision_cards_enabled``) — this
module is a pure stdlib leaf and MUST NOT import ``config`` (it raises without a
bot token, which would force a token into parser unit tests). The bot's
``config.py`` owns the canonical ``CC_TELEGRAM_PERMISSION_PROMPTS`` /
``CC_TELEGRAM_DECISION_CARDS`` declarations for documentation; the parser just
reads the same env vars.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output(),
parse_permission_prompt(), parse_workflow_approval(), parse_generic_decision().
"""

import hashlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)


# ── Permission / Workflow approval-gate detector kill-switch ──────────────
#
# A LOCAL parser flag (Hermes P2-3): ``terminal_parser`` is a pure stdlib
# leaf and must NOT ``from .config import config`` (config raises without a
# bot token). The bot's ``config.py`` OWNS the canonical
# ``CC_TELEGRAM_PERMISSION_PROMPTS`` declaration for docs / the README sync
# rule; this module reads the same env var locally so parser unit tests can
# toggle it WITHOUT a token, via the ``reset_for_tests`` /
# ``set_permission_prompts_enabled`` seam (the repo's reset-seam protocol).


def _read_permission_prompts_env() -> bool:
    """Truthiness of ``CC_TELEGRAM_PERMISSION_PROMPTS`` (default OFF)."""
    return os.getenv("CC_TELEGRAM_PERMISSION_PROMPTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_PERMISSION_PROMPTS_ENABLED: bool = _read_permission_prompts_env()


def permission_prompts_enabled() -> bool:
    """True when Permission/Workflow gate detection is enabled (flag ON)."""
    return _PERMISSION_PROMPTS_ENABLED


def set_permission_prompts_enabled(value: bool) -> None:
    """Test/runtime seam: override the gate-detection flag explicitly."""
    global _PERMISSION_PROMPTS_ENABLED  # noqa: PLW0603
    _PERMISSION_PROMPTS_ENABLED = bool(value)


# ── Generic "Decision" prompt detector kill-switch ────────────────────────
#
# A SECOND LOCAL parser flag (Stage B1), independent of the Permission /
# Workflow flag above and seeded the same way (``main._run_bot`` reads
# ``config`` and calls ``set_decision_cards_enabled`` to dodge the import-order
# race). When ON, the last-priority ``Decision`` ``UIPattern`` surfaces generic
# titled numbered-option confirmation prompts (the "Switch model?" / folder-trust
# family) that no NAMED pattern covers as a DISPLAY-ONLY card. Default OFF — a
# flag-OFF deploy adds ZERO new detection (``_active_ui_patterns`` drops it).
# ``config.py`` owns the canonical ``CC_TELEGRAM_DECISION_CARDS`` declaration for
# docs / the README sync rule; the parser reads the same env var locally so it
# stays a config-free stdlib leaf.


def _read_decision_cards_env() -> bool:
    """Truthiness of ``CC_TELEGRAM_DECISION_CARDS`` (default OFF)."""
    return os.getenv("CC_TELEGRAM_DECISION_CARDS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_DECISION_CARDS_ENABLED: bool = _read_decision_cards_env()


def decision_cards_enabled() -> bool:
    """True when the generic Decision-prompt detector is enabled (flag ON)."""
    return _DECISION_CARDS_ENABLED


def set_decision_cards_enabled(value: bool) -> None:
    """Test/runtime seam: override the Decision-detection flag explicitly."""
    global _DECISION_CARDS_ENABLED  # noqa: PLW0603
    _DECISION_CARDS_ENABLED = bool(value)


def reset_for_tests() -> None:
    """Re-read BOTH parser flags from the environment (reset-seam).

    Registered in the leaf's conftest reset protocol so a test that set either
    flag (or its env var) does not leak into the next test.
    """
    global _PERMISSION_PROMPTS_ENABLED  # noqa: PLW0603
    global _DECISION_CARDS_ENABLED  # noqa: PLW0603
    _PERMISSION_PROMPTS_ENABLED = _read_permission_prompts_env()
    _DECISION_CARDS_ENABLED = _read_decision_cards_env()


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction normally scans lines top-down: the first line matching any
    `top` pattern marks the start, the first subsequent line matching any
    `bottom` pattern marks the end. Both boundary lines are included in the
    extracted content. Patterns with ``bottom_up=True`` scan from the live
    footer/bottom marker upward so old scrollback regions cannot shadow the
    currently visible picker.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient. This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)
    bottom_up: bool = False  # scan bottom marker first, then matching top upward
    # Additional pre-top-found bail markers. While walking back from
    # ``bottom_idx`` to find a top anchor, encountering any of these is
    # treated as evidence of a stale picker between the current top
    # candidate (above) and the live bottom (below) — bail with None so
    # the next pattern in UI_PATTERNS can try. The existing
    # ``pattern.bottom``-based bail catches the case where the OLDER
    # picker still has its footer intact; this extra list catches the
    # case where Claude Code has collapsed the older picker into a
    # ``… +N lines (ctrl+o to expand)`` placeholder (cga incident,
    # 2026-05-20 13:38:25: multi-tab AUQ #A's tab header at scrollback
    # line 130 combined with AUQ #B's live ``Enter to select`` near line
    # 220 because AUQ #A's footer had been collapsed; the bot rendered
    # AUQ #A's options on the live card).
    bail_markers: tuple[re.Pattern[str], ...] = ()
    # OPTIONAL strict post-validation gate (S-8 fail-closed). When set, a loose
    # top/bottom match is NOT sufficient: ``extract_interactive_content`` runs
    # ``validator(full_pane_text)`` (the WHOLE pane, so the validator can apply
    # its own bottom-terminal requirement) and only returns this pattern when
    # the validator returns a non-None form — whose ``pane_excerpt`` becomes the
    # returned content. ``None`` (the default) keeps the loose-match-only
    # behavior — AUQ / EPM / Settings / RestoreCheckpoint stay byte-identical.
    # Used ONLY by the flag-gated Permission / Workflow approval gates, whose
    # loose anchors would otherwise light a card on assistant prose that QUOTES
    # a gate (verified false positives, 2026-06-24 peer review).
    validator: "Callable[[str], AskUserQuestionForm | None] | None" = None


# Marks a collapsed Claude Code TUI region — Bash output, file reads, or an
# answered/dismissed AskUserQuestion picker. The token appears at the spot the
# original content used to occupy and is rendered on its OWN line with this
# exact shape: ``     … +17 lines (ctrl+o to expand)``. For a LIVE picker, the
# collapse placeholder never appears as a standalone line inside the picker
# region (the user needs to see options to interact). Anchoring the regex
# with ``^`` … ``$`` rejects matches embedded inside model-supplied option
# descriptions (codex P2, 2026-05-20: a description quoting this text would
# otherwise be misread as a stale-picker boundary and bail detection).
_RE_COLLAPSED_REGION = re.compile(
    r"^\s*(?:…|\.\.\.)\s+\+\d+\s+lines?\s+\(ctrl[+-]o\s+to\s+expand\)\s*$"
)


# ── Permission / Workflow approval-gate anchors (flag-gated patterns) ──────
#
# Verified against the Wave-0 v2.1.190 fixtures
# (``tests/cctelegram/fixtures/permission_*.txt`` / ``workflow_*.txt``).
# Corrections to plan v4 §1 are folded per
# ``gate_fixtures_v2.1.190_NOTES.md``:
#   - Permission TOP verbs vary (allow / proceed / create); the verb set is
#     broadened and the ``Claude wants to`` alternative is kept.
#   - Permission BOTTOM accepts EITHER an inline ``(esc)``-tailed option line
#     (WebFetch — no separate footer) OR an ``Esc to cancel · Tab to amend``
#     footer (Bash / Write).
#   - The footer family (``Esc to cancel`` / ``Tab to amend`` / ``ctrl+g to
#     edit script``) collides across Permission / Workflow / EPM; the patterns
#     are ordered LAST and disambiguated on their TOP anchors.

# Permission TOP — the verb set is intentionally broad (NOTES correction #1):
# the co-occurring option-block + bottom footer carry the specificity, so a
# loose verb here cannot light a card on prose alone (S-8: the bottom anchor
# must co-occur within ``min_gap``).
_RE_PERMISSION_TOP_QUESTION = re.compile(
    r"^\s*Do you want to (?:allow|proceed|make|create|run|read|edit|write|"
    r"fetch|search|delete|move|install|update|execute|apply|modify)\b"
)
# The "Claude wants to …" preamble line (WebFetch / Bash variants render it
# above the question).
_RE_PERMISSION_TOP_PREAMBLE = re.compile(r"^\s*Claude wants to ")

# Permission BOTTOM (any match) — char-class tolerant for ``(Esc)`` / drift.
#   (a) an inline ``(esc)``-tailed numbered option (the "No, … (esc)" row —
#       WebFetch carries the affordance inline and has NO separate footer);
#   (b) the ``Esc to cancel · Tab to amend`` footer (Bash / Write).
_RE_PERMISSION_BOTTOM_INLINE_ESC = re.compile(
    r"^\s*[❯›▶*)>↓\s]?\s*\d+\.\s+.*\([eE]sc\)\s*$"
)
_RE_PERMISSION_BOTTOM_FOOTER = re.compile(r"^\s*Esc to cancel\b")

# Workflow TOP (any match) — ``Run a dynamic workflow?`` is the tightest
# (NOTES correction #5); the other two appear in the body.
_RE_WORKFLOW_TOP = (
    re.compile(r"^\s*Run a dynamic workflow\?"),
    re.compile(r"^\s*This dynamic workflow will\b"),
    re.compile(r"^\s*Dynamic workflows can use\b"),
)
# Workflow BOTTOM — anchored on the ``Esc to cancel`` footer line (the real
# v2.1.190 footer is ``Esc to cancel · Tab to amend`` on ONE line, so the
# ``Esc to cancel`` prefix matches it). The bare ``^\s*Tab to amend`` alt was
# DROPPED (codex P3): it never matches the real one-line footer (which leads
# with ``Esc to cancel``) and only widened the anchor surface — the strict
# ``parse_workflow_approval`` validates the full footer + label shape. The
# ``ctrl+g to edit script`` line is also EXCLUDED: it renders on its OWN line
# BELOW the ``Esc to cancel`` footer, so anchoring there would make the
# bottom-up scan cross the upper footer during walk-back and trip the
# pre-top-found bail. Anchoring on the upper footer line avoids the cross-bail.
_RE_WORKFLOW_BOTTOM = (re.compile(r"^\s*Esc to cancel\b"),)

# A trailing ``(esc)`` / ``(Esc)`` affordance on a permission option label —
# stripped deterministically + identically on every parse (so the cursor-blind
# fingerprint and the label match see the same text on mint and re-parse).
_RE_ESC_AFFORDANCE_SUFFIX = re.compile(r"\s*\([eE]sc\)\s*$")


def _strip_esc_affordance(label: str) -> str:
    """Remove a trailing ``(esc)`` / ``(Esc)`` affordance from an option label.

    Deterministic + idempotent: ``parse_permission_prompt`` carries the FULL
    option text (S-6: "Yes" vs "Yes, and don't ask again" must not collide),
    minus only the terminal-affordance hint. Applied on every parse so the
    minted label and any verify re-parse compare equal.
    """
    return _RE_ESC_AFFORDANCE_SUFFIX.sub("", label).rstrip()


# ── Generic "Decision" prompt anchors (Stage B1, flag-gated, LAST) ────────
#
# A generic titled numbered-option confirmation prompt (the "Switch model?"
# confirmation, the folder-trust prompt, and peers) that no NAMED pattern
# covers. Its footer MUST carry a live ``Enter to (confirm|continue)`` component
# — the affirmative-commit half of a confirmation dialog (verified on both real
# targets: ``Enter to confirm · Esc to cancel``). It deliberately EXCLUDES
# ``Enter to select`` (AUQ pattern 3's footer — first-match-wins already routes
# those to AUQ). Requiring ``Enter to (confirm|continue)`` (rather than accepting
# a bare ``Esc to cancel`` / ``Esc to exit``) STRUCTURALLY closes the verb-drift
# veto bypass (Codex P2): the Permission / EPM footer family
# (``Esc to cancel · Tab to amend``, bare ``Esc to cancel``) has NO
# ``Enter to confirm`` line, so a permission gate whose verb is outside
# ``parse_permission_prompt``'s whitelist (e.g. ``Do you want to open …?``) can
# no longer match Decision's footer at all — independent of the strict veto,
# which is KEPT as defense-in-depth. Ordered LAST + flag-gated.
_RE_DECISION_TOP_OPTION = re.compile(r"^\s*[❯›▶*)>]?\s*\d+\.\s+\S")
_RE_DECISION_FOOTER = re.compile(r"\bEnter to (?:confirm|continue)\b")


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
            # v2.1.170 renders the footer as ``ctrl+g`` (PLUS); pre-.170 used
            # ``ctrl-g`` (hyphen). Tolerate both — mirrors ``ctrl[+-]o`` above.
            # The .170 plan-approval also dropped the ``Esc to cancel`` line
            # (replaced by ``shift+tab to approve``), so this footer is the SOLE
            # bottom anchor on .170 and MUST match.
            re.compile(r"^\s*ctrl[+-]g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
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
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
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
            # v2.1.170 startup "Settings Warning" pane (invalid permission rule
            # etc.) — title is "Settings Warning", not "Settings:"; its body is
            # the blocking Continue/Fix/Exit picker the user must answer. The
            # bottom anchors ("Enter to confirm" / "Esc to cancel") already match.
            re.compile(r"^\s*Settings Warning\b"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
    # ── Interactive approval gates (ordered LAST; flag-gated) ─────────────
    # These two MUST come after every AUQ/EPM/Settings/RestoreCheckpoint
    # pattern so first-match-wins never lets a gate steal an AUQ/EPM/Settings
    # pane (and vice-versa). They are filtered OUT of the detector when
    # ``CC_TELEGRAM_PERMISSION_PROMPTS`` is OFF (see
    # ``_active_ui_patterns``) — a flag-OFF deploy adds ZERO new detection.
    # Each is disambiguated on its TOP anchor (the footer family overlaps).
    UIPattern(
        name="Permission",
        top=(_RE_PERMISSION_TOP_QUESTION, _RE_PERMISSION_TOP_PREAMBLE),
        bottom=(_RE_PERMISSION_BOTTOM_INLINE_ESC, _RE_PERMISSION_BOTTOM_FOOTER),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    UIPattern(
        name="Workflow",
        top=_RE_WORKFLOW_TOP,
        bottom=_RE_WORKFLOW_BOTTOM,
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    # ── Generic decision prompt (ordered LAST; flag-gated) ────────────────
    # Appended AFTER every named pattern (incl. the Permission / Workflow
    # gates) so first-match-wins never lets it steal a named pane. Filtered
    # OUT of the detector when ``CC_TELEGRAM_DECISION_CARDS`` is OFF (see
    # ``_active_ui_patterns``). ``parse_generic_decision`` is the strict-or-None
    # validator (wired below via ``replace``) and carries a Permission /
    # Workflow veto so a flag-OFF gate is never re-surfaced here.
    UIPattern(
        name="Decision",
        top=(_RE_DECISION_TOP_OPTION,),
        bottom=(_RE_DECISION_FOOTER,),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
]

# Names of the flag-gated approval-gate patterns. The detector filters these
# out of ``UI_PATTERNS`` when ``CC_TELEGRAM_PERMISSION_PROMPTS`` is OFF.
_GATE_PATTERN_NAMES: Final[frozenset[str]] = frozenset({"Permission", "Workflow"})

# The last-priority generic decision pattern. Filtered out of the detector when
# ``CC_TELEGRAM_DECISION_CARDS`` is OFF (its OWN flag, independent of the gate
# flag above).
_DECISION_PATTERN_NAME: Final[str] = "Decision"


def _active_ui_patterns() -> list[UIPattern]:
    """``UI_PATTERNS`` with the flag-gated patterns filtered by their flags.

    When ``CC_TELEGRAM_PERMISSION_PROMPTS`` is OFF (default) the ``Permission``
    / ``Workflow`` patterns are excluded; when ``CC_TELEGRAM_DECISION_CARDS`` is
    OFF (default) the ``Decision`` pattern is excluded. Each flag is
    independent — a flag-OFF deploy adds NO detection, no card, no
    ``WAITING_ON_USER`` promotion for its patterns (gated at the DETECTOR).
    """
    if _PERMISSION_PROMPTS_ENABLED and _DECISION_CARDS_ENABLED:
        return UI_PATTERNS
    active = UI_PATTERNS
    if not _PERMISSION_PROMPTS_ENABLED:
        active = [p for p in active if p.name not in _GATE_PATTERN_NAMES]
    if not _DECISION_CARDS_ENABLED:
        active = [p for p in active if p.name != _DECISION_PATTERN_NAME]
    return active


# ── ExitPlanMode plan-file footer ─────────────────────────────────────────

# The EPM footer is "ctrl+g to edit in  Vim  · ~/.claude/plans/<slug>.md"
# (the ctrl[+-]g tolerance mirrors the ExitPlanMode bottom anchor above). The
# plan file referenced there exists on disk during the live prompt (the agent
# Write-s it), so the bot can read it to post the plan BEFORE the picker card.
_RE_EPM_FOOTER = re.compile(r"ctrl[+-]g to edit")
_RE_EPM_PLAN_PATH = re.compile(r"(~/\.claude/plans/\S+\.md)")


def extract_epm_plan_file_path(pane_text: str) -> str | None:
    """Extract the ``~/.claude/plans/<slug>.md`` path from an ExitPlanMode
    footer. Anchors on the ``ctrl[+-]g to edit`` footer line FIRST (the sole
    bottom anchor on v2.1.170) so a stray plan-path mention elsewhere in
    scrollback can't win. If no footer line carries the path (e.g. tmux wrapped
    the footer onto two lines), falls back to the LAST plan path in the pane —
    the footer is at the bottom, so the bottom-most mention beats a stale
    scrollback mention above it. Returns the path string or None."""
    fallback: str | None = None
    for line in pane_text.split("\n"):
        m = _RE_EPM_PLAN_PATH.search(line)
        if not m:
            continue
        if _RE_EPM_FOOTER.search(line):
            return m.group(1)
        fallback = m.group(1)  # keep the LAST (bottom-most) match
    return fallback


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
    bottom delimiter varies by tab). ``bottom_up`` patterns find the live
    footer/bottom boundary first and then walk backward to the nearest top
    marker, preventing historic scrollback pickers from shadowing the active
    one after larger AUQ captures.
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    if pattern.bottom_up:
        if pattern.bottom:
            for i in range(len(lines) - 1, -1, -1):
                if any(p.search(lines[i]) for p in pattern.bottom):
                    bottom_idx = i
                    break
        else:
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip():
                    bottom_idx = i
                    break
        if bottom_idx is None:
            return None
        found_top = False
        # Walk back from bottom_idx - 1: the bottom line itself can't be
        # the top, and starting one above lets us bail cleanly when we
        # cross an OLDER instance of the bottom marker (which would
        # indicate that the top we're about to find belongs to a stale
        # picker, not the live one).
        for i in range(bottom_idx - 1, -1, -1):
            if any(p.search(lines[i]) for p in pattern.top):
                top_idx = i
                found_top = True
                continue
            if found_top:
                stripped = lines[i].strip()
                if (
                    not stripped
                    or all(c == "─" for c in stripped)
                    or lines[i].startswith((" ", "\t"))
                ):
                    continue
                break
            # Pre-top-found bail: when walking back from the live footer
            # to find a matching top, encountering an OLDER instance of
            # the same bottom marker means there's a complete prior
            # picker between bottom_idx and any candidate top above. The
            # earlier picker's footer is at lines[i]; whatever top we'd
            # find above it belongs to the OLDER picker, not the live
            # one anchored at bottom_idx. Bail so a later pattern in
            # UI_PATTERNS can try (e.g. plain-numbered after
            # single-tab-checkbox). Without this guard, a checkbox AUQ
            # in scrollback above a live plain-numbered AUQ shadowed
            # the live picker — the checkbox pattern walked past the
            # live plain-numbered options to find an old ☐ top.
            if pattern.bottom and any(p.search(lines[i]) for p in pattern.bottom):
                return None
            # Same bail, broader marker set: Claude Code may collapse an
            # OLDER picker's footer into ``… +N lines (ctrl+o to expand)``
            # so the bottom-pattern bail above can't see it. Detecting the
            # collapse placeholder anywhere on the walk-back path closes
            # that gap (cga incident, 2026-05-20 13:38:25).
            if pattern.bail_markers and any(
                p.search(lines[i]) for p in pattern.bail_markers
            ):
                return None
    else:
        for i, line in enumerate(lines):
            if top_idx is None:
                if any(p.search(line) for p in pattern.top):
                    top_idx = i
            elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
                bottom_idx = i
                break

        if top_idx is not None and not pattern.bottom:
            for i in range(len(lines) - 1, top_idx, -1):
                if lines[i].strip():
                    bottom_idx = i
                    break

    if top_idx is None or bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
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
    for pattern in _active_ui_patterns():
        result = _try_extract(lines, pattern)
        if not result:
            continue
        # S-8 fail-closed strict post-validation: a loose top/bottom match is
        # not enough for a validated pattern (the gate patterns) — run the
        # strict variant parser over the FULL pane (so its bottom-terminal
        # requirement applies) and only return the gate when it parses. On a
        # None we CONTINUE the pattern loop (a quoted/non-bottom gate must not
        # win — and must not block a later pattern). Use the strict form's
        # ``pane_excerpt`` as the content (the trusted gate region, not the
        # looser ``_try_extract`` slice). AUQ/EPM/Settings/RestoreCheckpoint
        # have no validator → byte-identical behavior.
        if pattern.validator is not None:
            form = pattern.validator(pane_text)
            if form is None:
                continue
            return InteractiveUIContent(
                content=_shorten_separators(form.pane_excerpt.rstrip()),
                name=pattern.name,
            )
        return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# Picker bottom-border markers that anchor on the *visible pane*. When the
# question prose is long enough to push the picker top anchor off the visible
# slice (~50 lines), ``extract_interactive_content`` over the visible pane
# alone returns None even though the picker IS live. The footer/border lives
# at the picker bottom — which always stays on the visible pane — so checking
# the last few visible lines for these markers is a robust "is the picker
# still on screen right now" predicate.
_PICKER_ANCHOR_MARKERS = (
    re.compile(r"Enter to select"),  # AskUserQuestion / RestoreCheckpoint footer
    re.compile(r"Enter to confirm"),  # Settings footer
    re.compile(r"ctrl[+-]g to edit"),  # ExitPlanMode footer (v2.1.170: ctrl+g)
    re.compile(r"Esc to (cancel|exit)"),  # generic dismiss footer
    re.compile(r"╰─"),  # picker frame bottom-left corner
    # Multi-question AUQ Submit-confirmation screen has none of the above
    # — no Enter/Esc footer, no ╰─ border. When the tab header and the
    # "Ready to submit" prompt scroll above the visible bottom 5 lines,
    # only the numbered Submit/Cancel options stay anchored. Match the
    # ``1. Submit answers`` line itself (cursor-aware) and the prompt
    # above it. Without these anchors, the visible-only liveness check
    # returns "absent" on the Submit screen and the card gets cleared
    # mid-AUQ workflow, leaving the user with no way to commit answers.
    re.compile(r"Ready to submit your answers"),
    re.compile(r"^\s*[❯›▶*)>\s]?\s*\d+\.\s+Submit answers\s*$"),
)


def is_picker_anchor_visible(visible_pane: str, *, window_lines: int = 5) -> bool:
    """True when the last ``window_lines`` of ``visible_pane`` contain a
    picker footer/border anchor.

    Used as the CB5 fallback in liveness checks: when ``is_interactive_ui``
    over the visible pane returns False on a long-question case (top
    anchor pushed off screen), this check still returns True if the picker
    footer sits at the visible bottom.
    """
    if not visible_pane:
        return False
    tail = visible_pane.rstrip("\n").split("\n")[-window_lines:]
    return any(p.search(line) for line in tail for p in _PICKER_ANCHOR_MARKERS)


def visible_pane_liveness(visible_pane: str | None) -> str:
    """Three-state liveness predicate over the *visible* tmux pane (no scrollback).

    Returns one of:
      * ``"present"`` — an interactive UI is on screen now. Safe to dispatch
        nav keystrokes; do not destructively clear.
      * ``"absent"`` — no interactive UI on screen. Safe to clear / refresh /
        bail out of nav dispatch.
      * ``"unknown"`` — empty / whitespace-only capture (alt-screen mode,
        tmux redraw race, terminal cleared mid-cycle). MUST NOT be treated
        as absent: a destructive clear here can erase a live picker the
        very next frame brings back.

    Implementation:
      1. Empty/whitespace → ``"unknown"``.
      2. ``is_interactive_ui(visible)`` → ``"present"``.
      3. ``is_picker_anchor_visible(visible)`` → ``"present"`` (CB5 long-
         question fallback — top anchor scrolled off but footer is visible).
      4. Otherwise → ``"absent"``.
    """
    if not visible_pane or not visible_pane.strip():
        return "unknown"
    if is_interactive_ui(visible_pane):
        return "present"
    if is_picker_anchor_visible(visible_pane):
        return "present"
    return "absent"


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
    r"^\s*(?P<cursor>[❯›▶*)>↓]?)\s*(?P<num>\d+)\.\s+(?P<label>.+?)\s*$"
)

# Option-row checkbox — ASCII brackets, NOT ☐/☒ (those are tab-header only).
_RE_OPTION_CHECKBOX = re.compile(r"^\s*[❯›▶*)>↓\s]?\s*\d+\.\s+\[(?P<mark>[ ✔xX])\]\s")

# Matches the picker's "Enter to select / Tab / Esc" footer.
_RE_PICKER_FOOTER = re.compile(r"Enter to select")

# Matches the review-screen footer that asks the user to confirm submission.
_RE_REVIEW_HEADER = re.compile(r"^\s*Review your answers\s*$")
_RE_SUBMIT_PROMPT = re.compile(r"^\s*Ready to submit your answers\?\s*$")

# Literal label of the review-screen's "Submit answers" row (always option 1).
# The single source of the literal that the cursor-blind Submit predicate
# (``AskUserQuestionForm.review_submit_dispatchable``) and the mint-site tags
# anchor on, so a relabeled/reordered review layout SAFELY DECLINES.
REVIEW_SUBMIT_LABEL = "Submit answers"

# Matches a free-text "Type something" option (variant where the user can
# type free text instead of picking a numbered option).
_RE_FREE_TEXT_OPTION = re.compile(r"Type something")
_AFFORDANCE_TRAILING_CHARS = " \t\r\n.!?…。:;,，、"


def is_affordance_label(label: str) -> bool:
    """True for Claude Code picker affordances that are not real options."""
    normalized = label.strip().rstrip(_AFFORDANCE_TRAILING_CHARS).strip()
    return (
        bool(_RE_FREE_TEXT_OPTION.fullmatch(normalized))
        or normalized == "Chat about this"
    )


# Matches ``(Recommended)`` suffix on an option label. Case-insensitive
# because Claude Code (and skill prompts) sometimes emit the tag lowercase
# — observed 2026-05-19 in cgc-fork's "Query core grill 2a" AUQ where the
# JSONL labels carried ``(recommended)``. Without IGNORECASE the flag
# never set and the literal text leaked into the pick-button label.
_RE_RECOMMENDED = re.compile(r"\(Recommended\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class AskOption:
    """One picker option inside an AskUserQuestion form."""

    label: str  # e.g. "C — Parallel tracks: stabilize core + scaffold copilot"
    recommended: bool  # True if "(Recommended)" suffix present
    cursor: bool  # True if this option is the current selection (❯ / › prefix)
    number: int | None  # 1-9 numeric shortcut, or None when not rendered
    # Per-option reasoning text from the JSONL tool_use.input. Empty for
    # pane-only parses (the pane scrape doesn't reliably attribute description
    # lines to specific options). Used by the renderer to inline reasoning
    # under each label. Excluded from the fingerprint canonical (descriptions
    # can vary cosmetically across redraws and shouldn't invalidate tokens).
    description: str = ""
    # Multi-select display state from pane checkbox glyphs. True = [✔]/[x]/[X],
    # False = [ ], None = unknown/off-screen/non-checkbox single-select.
    # Excluded from equality/canonical/fingerprint: toggles must not stale
    # sibling tokens, and off-screen unknown must not collapse to False.
    selected: bool | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AskQuestion:
    """One question inside a multi-question AskUserQuestion form.

    Mirrors the JSONL ``tool_use.input.questions[i]`` shape. ``options`` here
    is the full ordered list from the structured payload — independent of
    pane visibility.
    """

    title: str  # the human-readable question text (``question`` field in JSONL)
    header: str  # short label used for tab cells (``header`` field in JSONL)
    options: tuple[AskOption, ...]
    multi_select: bool = False


@dataclass(frozen=True)
class AskTab:
    """One question-tab in a multi-question AskUserQuestion form."""

    label: str  # e.g. "Approach" — may be empty for the submit cell
    answered: bool  # ☒ filled (question has an answer)
    is_submit: bool  # ✔ marker — the synthetic "Submit" cell
    is_current: bool  # the tab the user is currently viewing


def _questions_digest(questions: tuple["AskQuestion", ...]) -> str:
    """Stable digest over the multi-question matrix for the fingerprint.

    Covers question titles + per-question ordered option labels + option
    counts. A label rename, an option reorder, or a count change all flip
    the digest → ``handle_interactive_ui`` tears down stale cards and
    re-renders. Descriptions are excluded (cosmetic-only redraws shouldn't
    invalidate live tokens). Uses ``\\x1f`` (unit separator) as a delimiter
    that cannot appear in JSONL-derived text — naive ``"|".join`` would
    collide on labels containing ``|``.
    """
    parts: list[str] = []
    for q in questions:
        labels = "\x1f".join(o.label for o in q.options)
        parts.append(f"{q.title}\x1e{len(q.options)}\x1e{labels}")
    payload = "\x1d".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# AUQ PreToolUse hook content-digest surface.
#
# Used by the PreToolUse hook (``hook.py``) to write a content-only
# fingerprint into the AUQ side file, and by the bot's pretool reader
# (``handlers/interactive_ui.py``) as a logging identifier + a self-
# integrity check on the file (recomputed digest must match the stored
# ``input_fingerprint``).
#
# NOT the acceptance criterion. Side-file acceptance is the projection-
# based predicate in ``_record_consistent_with_pane`` (handlers/
# interactive_ui.py) — it compares projected fields, not hashes, so the
# title-skip / multi-tab-subset edge cases each have a principled answer.
#
# Encoding mirrors ``_questions_digest`` so future readers can compare
# the two surfaces side-by-side.
#
# Separator-collision note (codex P2 round 1): the encoding uses
# ASCII unit/record/group separators ``\x1f`` / ``\x1e`` / ``\x1d``.
# JSON string values CAN legally carry these escaped control bytes —
# i.e. ``("A\x1fB", "C")`` and ``("A", "B\x1fC")`` would produce the
# same encoded payload. In practice, AskUserQuestion labels round-
# trip through Claude Code's TUI renderer which strips control bytes,
# so the collision risk is theoretical, not practical. The digest is
# a logging/cache identifier (NOT the side-file acceptance criterion;
# acceptance is the projection predicate in handlers/interactive_ui.py),
# so even a theoretical collision wouldn't cause wrong-action dispatch.
def questions_content_digest(
    pairs: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    """Content-only digest over ordered (question_title, option_labels) pairs."""
    parts: list[str] = []
    for title, labels in pairs:
        joined = "\x1f".join(labels)
        parts.append(f"{title}\x1e{len(labels)}\x1e{joined}")
    payload = "\x1d".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def questions_content_pairs_from_tool_input(
    tool_input: Any,
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Extract content pairs from a JSONL/hook AskUserQuestion ``tool_input``.

    Shape expected: ``{"questions": [{"question": str, "options":
    [{"label": str, "description": str?}, ...]}, ...]}``. Required keys
    (``question`` on each question, ``options`` array on each question,
    ``label`` on each option) must be present AND well-typed; missing
    keys are treated as shape errors, not silently coerced to empty
    strings (codex P2 round 1: tightened to match the docstring contract).
    Returns ``None`` on any shape mismatch.
    """
    if not isinstance(tool_input, dict):
        return None
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for q in raw_questions:
        if not isinstance(q, dict):
            return None
        if "question" not in q or not isinstance(q["question"], str):
            return None
        title = q["question"]
        if "options" not in q or not isinstance(q["options"], list):
            return None
        labels: list[str] = []
        for o in q["options"]:
            if not isinstance(o, dict):
                return None
            if "label" not in o or not isinstance(o["label"], str):
                return None
            labels.append(o["label"])
        pairs.append((title, tuple(labels)))
    return tuple(pairs)


def questions_content_pairs_from_form(
    form: "AskUserQuestionForm",
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Extract content pairs from a parsed ``AskUserQuestionForm``.

    For multi-question forms (``form.questions`` non-empty — set by
    ``resolve_ask_form`` when JSONL is available), emits one pair per
    question.

    For single-question forms (the pane-only parse case, which is what
    the PreToolUse-hook reader sees pre-JSONL), uses
    ``form.current_question_title`` (or empty string if missing) plus
    ``form.options[].label``.

    Returns ``None`` when the form carries no visible options at all.
    """
    if form.questions:
        pairs: list[tuple[str, tuple[str, ...]]] = []
        for q in form.questions:
            pairs.append((q.title, tuple(o.label for o in q.options)))
        return tuple(pairs) if pairs else None
    if not form.options:
        return None
    title = form.current_question_title or ""
    return ((title, tuple(o.label for o in form.options)),)


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
    # Multi-question matrix from the JSONL ``tool_use.input.questions`` list.
    # Empty for single-question forms (the existing ``options`` / ``current_question_title``
    # fields carry the same data and the renderer / fingerprint stay on the
    # single-tab path). Populated by ``resolve_ask_form`` when JSONL carries
    # ``len(questions) > 1``.
    questions: tuple[AskQuestion, ...] = ()
    # True when ``current_tab_idx`` was successfully matched from pane content
    # against the JSONL questions matrix. False means the resolver fell through
    # to ``current_tab_idx = 0`` because neither title-match nor option-overlap
    # could pin a tab — typically a corrupt or scrolled-back pane. When False,
    # the renderer MUST NOT mint option-pick buttons (FA5+ safety rule): the
    # pane parse and JSONL render share the same defaulted state, fingerprint
    # parity would pass, and dispatching a digit could answer the wrong tab.
    current_tab_inferred: bool = True
    select_mode: Literal["single", "multi", "unknown"] = "single"
    # Source-of-truth fields used in fingerprinting are above this line.
    # Anything appended below MUST be excluded from ``_canonical_repr`` so
    # adding diagnostic state doesn't break callback tokens minted by
    # earlier renders.
    _meta: dict[str, str] = field(default_factory=dict, compare=False)
    # Display-only question title captured from the pane walk-back when no
    # JSONL data is available. Populated by ``parse_ask_user_question``
    # only — ``resolve_ask_form`` does NOT propagate this through its
    # merged-form constructors because every JSONL overlay path already
    # has the authoritative title in ``current_question_title``. The
    # renderer reads ``current_question_title or pane_walkback_title``
    # so a fresh single-tab picker (before Claude Code flushes the AUQ
    # ``tool_use`` line to JSONL) still gets a header in Telegram.
    # MUST NOT be used by ``_strong_match`` or any other identity check:
    # the walk-back can capture assistant prose or stale scrollback as a
    # title (hermes review 2026-05-21), and substring-matching that
    # against a JSONL question would mis-overlay stale labels onto a
    # live pane (wrong-action class bug).
    pane_walkback_title: str | None = field(default=None, compare=False)
    options_complete: bool = field(default=False, compare=False)

    def _canonical_repr(self) -> str:
        """Stable string form used by ``fingerprint``.

        Excludes ``pane_excerpt`` (carries cursor noise and re-flows on
        redraw) and ``_meta`` (diagnostic). Order is fixed; if you add a
        field that should influence callback freshness, append a new line
        here — don't reorder existing ones.

        Single-question forms (``len(questions) <= 1``) produce the exact
        5-line canonical that pre-multi-tab code did, so callback tokens
        minted against single-question forms keep validating across the
        deploy that introduces ``questions`` / ``current_tab_inferred``.
        The ``QS:`` and ``INF:`` lines only appear for multi-tab forms,
        where there is no live single-question token to invalidate.

        The per-option canonical is **cursor-blind** on every screen
        (review and non-review): on Claude Code v2.1.167 dispatch is a
        bare digit (the option IS the digit, cursor-independent), so the
        terminal cursor ``❯`` position must NOT feed the form identity —
        a cursor move would otherwise rotate the pick token and pop a
        still-live card (peek_none / stale_form). The ``RVW:`` line, not
        the cursor, distinguishes review from non-review forms.
        """
        tabs_str = "|".join(
            f"{t.label}:{'A' if t.answered else 'E'}"
            f":{'C' if t.is_current else '_'}"
            f":{'S' if t.is_submit else '_'}"
            for t in self.tabs
        )
        opts_str = "|".join(
            f"{o.number}:{o.label}:{'R' if o.recommended else '_'}"
            for o in self.options
        )
        lines = [
            f"TABS:{tabs_str}",
            f"Q:{self.current_question_title or ''}",
            f"OPTS:{opts_str}",
            f"RVW:{'1' if self.is_review_screen else '0'}",
            f"FT:{'1' if self.is_free_text else '0'}",
        ]
        if self.select_mode != "single":
            lines.append(f"SEL:{self.select_mode}")
        if len(self.questions) > 1:
            lines.append(f"QS:{_questions_digest(self.questions)}")
            lines.append(f"INF:{'1' if self.current_tab_inferred else '0'}")
        return "\n".join(lines)

    def options_contiguous_from_one(self) -> bool:
        """True when visible option numbers are exactly 1..len(options)."""
        if not self.options:
            return False
        return [o.number for o in self.options] == list(range(1, len(self.options) + 1))

    def fingerprint(self) -> str:
        """Stable 16-char hex digest over the structured form state.

        Used by the (PR 2) renderer to mint callback tokens. On click, the
        handler reparses the pane and compares fingerprints — a mismatch
        means the form changed under us (user navigated, skill advanced,
        Claude Code redrew) and the click must not be dispatched verbatim.
        """
        return hashlib.sha1(self._canonical_repr().encode()).hexdigest()[:16]

    def review_submit_dispatchable(self, option_label: str) -> bool:
        """True iff this is a review screen whose Submit row (option 1) is the literal
        REVIEW_SUBMIT_LABEL AND still matches the minted option_label — CURSOR-BLIND.
        The digit dispatch activates Submit regardless of the terminal cursor (verified
        on Claude Code v2.1.161), so the guard no longer requires the cursor on Submit;
        is_review_screen + option#1 + literal label + minted-label anchors mean a
        non-review screen, a relabeled Submit, or a reordered review layout all SAFELY
        DECLINE (never a wrong dispatch)."""
        return bool(
            self.is_review_screen
            and self.options
            and self.options[0].number == 1
            and self.options[0].label == REVIEW_SUBMIT_LABEL
            and self.options[0].label == option_label
        )


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


def _checkbox_selected_from_line(line: str) -> bool | None:
    """Return checkbox selected state for an option row, or None if absent."""
    match = _RE_OPTION_CHECKBOX.match(line)
    if match is None:
        return None
    mark = match.group("mark")
    return mark in ("✔", "x", "X")


def _strip_option_checkbox(label: str) -> str:
    """Remove a leading ``[ ]`` / ``[✔]`` checkbox from a parsed option label."""
    return re.sub(r"^\[[ ✔xX]\]\s+", "", label, count=1)


def _normalize_pick_label(label: str) -> str:
    """Canonicalize an option label for the cursor-landing verify compare.

    Lowercase, collapse internal whitespace runs to a single space, strip a
    leading checkbox glyph (``[ ]`` / ``[x]`` / ``[X]`` / ``[✔]``, trailing
    whitespace OPTIONAL so ``[✔]Foo`` normalizes the same as ``[✔] Foo``) and a
    trailing ``(recommended)`` suffix (case-insensitive), then edge-strip. The
    live pane label and the minted label go through the SAME normalization so a
    checkbox redraw, a recommended tag, or trailing whitespace never spuriously
    fails the confirm. The checkbox strip is done locally (not via the shared
    ``_strip_option_checkbox``, whose required trailing whitespace other callers
    depend on) so the no-space ``[✔]Foo`` case strips too.
    """
    stripped = re.sub(r"^\[[ xX✔]\]\s*", "", label.strip(), count=1)
    stripped = re.sub(r"\(recommended\)\s*$", "", stripped, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _loose_label_match(live: str, minted: str) -> bool:
    """True iff the live cursor's label is the minted label (truncation-tolerant).

    Both sides are normalized via ``_normalize_pick_label``. An empty normalized
    side is rejected (an empty match would accept anything — a wrong-option commit
    hazard). This is the cursor-landing sanity guard alongside the NUMBER +
    FINGERPRINT checks, so it tolerates the .168 picker clipping long option text
    (the minted token may carry the full label while the pane clips the live one),
    while still rejecting an unrelated option.

    Accepts iff (both non-empty) the normalized strings are EQUAL, or the live
    label is a string PREFIX of the minted label (``minted.startswith(live)`` — the
    pane truncated a longer option). This rejects semantic extension
    (live ``"Approve with conditions"`` vs minted ``"Approve"`` → False) and accepts
    truncation (live ``"Approve with cond"`` vs minted ``"Approve with conditions"``
    → True). The asymmetry is deliberate: only the LIVE side is ever clipped by the
    terminal, so the minted label is never the truncated one.
    """
    nl = _normalize_pick_label(live)
    nm = _normalize_pick_label(minted)
    if not nl or not nm:
        return False
    return nl == nm or nm.startswith(nl)


# Raw-pane markers that prove an AskUserQuestion picker / review screen is up.
# Used by the v2.1.168 confirm step to distinguish "picker still rendered but
# unparseable" (AMBIGUOUS — never record ``dispatched``) from "picker positively
# gone" (the tool resolved). Footer phrases + the review-screen headers.
_PICKER_MARKERS: Final[tuple[str, ...]] = (
    "to select",
    "to navigate",
    "to cancel",
    "Review your answers",
    "Ready to submit",
)

# A numbered-option row carrying a real selection cursor glyph (``❯``/``›``/``▶``).
# This is the cursor-glyph fallback for ``_pane_looks_like_picker``: a still-live
# picker whose footer/header markers are scrolled off / truncated / outside the
# captured slice can still be proven up by a cursor-led numbered option. Restricted
# to the genuine cursor glyphs — ``↓`` is the scroll indicator and ``*``/``>``/``)``
# are noise, so they are deliberately excluded.
_RE_PICKER_CURSOR_ROW = re.compile(r"^\s*[❯›▶]\s*\d+\.\s")


def _pane_looks_like_picker(pane: str) -> bool:
    """True iff the raw pane text carries any AskUserQuestion picker marker.

    A coarse raw-text scan (no parse) for the footer phrases and review-screen
    headers an AUQ picker always renders, OR a numbered-option line carrying a
    real selection cursor glyph (``❯``/``›``/``▶`` via ``_RE_PICKER_CURSOR_ROW``)
    — the cursor-glyph fallback covers a still-live picker whose footer/header
    markers are scrolled off / truncated / outside the captured slice. The
    confirm step uses it as the tie-breaker when ``resolve_ask_form`` returns
    None: a match means the picker is still up but the parse failed (AMBIGUOUS →
    ``commit_unconfirmed``, never ``dispatched``); no match means the picker
    positively disappeared (the tool resolved).
    """
    if any(marker in pane for marker in _PICKER_MARKERS):
        return True
    return any(_RE_PICKER_CURSOR_ROW.match(line) for line in pane.splitlines())


def _pane_glyph_signal(lines: list[str]) -> Literal["single", "multi", "unknown"]:
    """Classify pane option rows by checkbox glyph presence."""
    option_rows = []
    for line in lines:
        match = _RE_NUMBERED_OPTION.match(line)
        if match is None:
            continue
        label = _strip_option_checkbox(match.group("label").strip())
        if is_affordance_label(label):
            continue
        option_rows.append(line)
    if not option_rows:
        return "unknown"
    with_checkbox = sum(1 for line in option_rows if _RE_OPTION_CHECKBOX.match(line))
    if with_checkbox == len(option_rows):
        return "multi"
    if with_checkbox == 0:
        return "single"
    return "unknown"


_WARNED_MALFORMED_MULTISELECT = False


def _warn_malformed_multiselect_once() -> None:
    """Warn once for malformed JSONL ``multiSelect`` values."""
    global _WARNED_MALFORMED_MULTISELECT  # noqa: PLW0603
    if _WARNED_MALFORMED_MULTISELECT:
        return
    _WARNED_MALFORMED_MULTISELECT = True
    logger.warning("AskUserQuestion multiSelect must be boolean when present")


def _tool_input_select_mode(
    tool_input: dict[str, Any],
) -> Literal["single", "multi", "unknown"]:
    """Resolve JSONL/side-file select mode from question.multiSelect fields."""
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return "single"
    saw_multi = False
    for question in questions:
        if not isinstance(question, dict) or "multiSelect" not in question:
            continue
        value = question.get("multiSelect")
        if not isinstance(value, bool):
            _warn_malformed_multiselect_once()
            return "unknown"
        saw_multi = saw_multi or value
    return "multi" if saw_multi else "single"


def _resolve_select_mode(
    source_mode: Literal["single", "multi", "unknown"] | None,
    pane_signal: Literal["single", "multi", "unknown"],
    *,
    is_review_screen: bool,
) -> Literal["single", "multi", "unknown"]:
    """Apply the PR-B source-vs-pane select-mode decision table."""
    if is_review_screen:
        return "single"
    if source_mode == "unknown" or pane_signal == "unknown":
        return "unknown" if source_mode is not None else pane_signal
    if source_mode is None:
        return pane_signal
    if source_mode != pane_signal:
        return "unknown"
    return source_mode


def _parse_numbered_options(lines: list[str]) -> tuple[AskOption, ...]:
    """Walk lines top-down collecting consecutive numbered options.

    Stops at the first non-option, non-blank line so a description line
    following an option doesn't get folded into the next option's label.
    Returns ``()`` if no numbered options are found or numbering has a
    gap (a gap usually means we're mid-redraw — caller should treat as a
    parse failure).
    """
    options: list[AskOption] = []
    # True when the live cursor ``❯`` is parked on a free-text affordance row
    # ("Type something" / "Chat about this"). Affordances ALWAYS trail the real
    # options, so an affordance cursor is the bottom-most ``❯`` on screen — i.e.
    # the live one — which means every ``❯`` on a real option above it is stale
    # scrollback. We track this so the bottom-most-cursor dedup below can clear
    # the surviving stale real-option cursor instead of painting a phantom.
    affordance_cursor_seen = False
    for line in lines:
        m = _RE_NUMBERED_OPTION.match(line)
        if m is None:
            if options:
                stripped = line.strip()
                if not stripped:
                    continue
                # Picker footer ends the option block.
                if _RE_PICKER_FOOTER.search(line) or _RE_TAB_HEADER.match(line):
                    break
                # Anything else (description text, separator runs, pros/cons
                # bullets) is treated as continuation of the previous option
                # and silently skipped. Earlier the loop broke on any
                # non-numbered line, which dropped every option past the
                # first when Claude Code rendered multi-line descriptions.
                continue
            continue
        try:
            num = int(m.group("num"))
        except ValueError:
            return ()
        label = m.group("label").strip()
        selected = _checkbox_selected_from_line(line)
        label = _strip_option_checkbox(label)
        # Free-text affordances ("Type something", "Chat about this") render as
        # numbered rows in the TUI but are NOT real picker options. The
        # side-file source and the pane-signal classifiers (``_pane_glyph_signal``,
        # ``auq_source._record_consistent_with_pane``) already exclude them, so
        # including them here gave a pure-pane parse N+1 options vs the side
        # file's N → fingerprint mismatch → silent toggle reject. Skip them so a
        # render→tap source flip keeps the fingerprint stable. Affordances always
        # trail the real options, so skipping them preserves the 1-based numbering
        # and the contiguity guard below stays satisfied. We still note when the
        # live cursor sits on a (dropped) affordance so the dedup below doesn't
        # promote a stale scrollback cursor on a real option to "live".
        if is_affordance_label(label):
            if m.group("cursor").strip() in ("❯", "›", "▶", "*"):
                affordance_cursor_seen = True
            continue
        # ``↓`` is the picker's scroll-more indicator, NOT a selection cursor.
        # Claude Code paints it at the left edge of the top visible option when
        # earlier options have scrolled off the viewport. Empirically (live
        # ``tmux capture-pane -S -500`` of a scrolled picker): the real ``❯``
        # cursor sits in the frozen scrollback rows while the live viewport's
        # top row carries ``↓``. It stays in ``_RE_NUMBERED_OPTION``'s cursor
        # char-class so the row still parses as an option, but it must not set
        # ``cursor`` — doing so painted a phantom ❯ on the scroll-boundary row.
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
                selected=selected,
            )
        )
    # Contiguity guard: keep only the longest monotonic +1 prefix starting at
    # whichever number the first option uses. The pane's visible region can
    # scroll past option 1 (questions with long descriptions push earlier
    # options off the top), so anchoring strictly at 1 dropped the entire
    # block. Trailing special rows like ``0. Dismiss`` (Claude Code's feedback
    # survey) still break the numeric run and get dropped from the structured
    # view; the keystroke fallback (Enter/digit keys) still reaches them.
    if not options or options[0].number is None:
        return ()
    kept: list[AskOption] = []
    expected: int = options[0].number
    for opt in options:
        if opt.number != expected:
            break
        kept.append(opt)
        expected += 1
    # Bottom-most-cursor dedup. Claude Code can leave MORE than one ``❯`` in a
    # captured pane, from two sources that the renderer must collapse to a
    # single live cursor:
    #
    #   1. Stale scrollback — a ``tmux capture-pane -S -<n>`` of a SCROLLED
    #      picker retains the pre-scroll top rows, INCLUDING a frozen ``❯`` on
    #      whatever option was the cursor before the viewport scrolled. (Long
    #      AUQs need the ``-S`` capture so off-screen options are recovered.)
    #   2. Decorative Recommended marker — older Claude Code TUIs painted a
    #      second ``❯`` on the ``(Recommended)`` row as well as the live cursor
    #      row (this no longer occurs in Claude Code v2.1.x, which puts the
    #      recommendation on a description line and never decorates with ``❯``).
    #
    # In BOTH cases the spurious ``❯`` is physically ABOVE the live cursor row:
    # scrollback history sits above the live viewport, and the Recommended row
    # is reordered to the top. So the live cursor is unambiguously the
    # BOTTOM-MOST ``❯`` (closest to the footer). When >1 cursor survives, keep
    # only the last and clear the rest; this also satisfies the "≥1 cursor
    # visible" renderer invariant (we never clear the sole survivor).
    #
    # This MUST run as the final cursor authority — an earlier recommended-only
    # dedup would strip the live cursor when it lands on a Recommended option
    # below a stale scrollback ``❯`` (reported the card as frozen on option 1).
    # Validated against live 80x24 captures at cursor positions 1-5 (both nav
    # directions) and the legacy Bug-C dual-cursor / restore cases, which all
    # resolve to the bottom-most ``❯``.
    cursor_idxs = [i for i, o in enumerate(kept) if o.cursor]
    # When the live cursor is on a (dropped) trailing affordance, every real
    # option ``❯`` is stale scrollback above it — clear them all so no real
    # option is mislabelled as the cursor. Otherwise keep only the bottom-most
    # real-option ``❯`` (the live cursor) and clear the stale ones above it.
    if affordance_cursor_seen:
        clear_idxs = list(cursor_idxs)
    elif len(cursor_idxs) > 1:
        clear_idxs = cursor_idxs[:-1]
    else:
        clear_idxs = []
    for i in clear_idxs:
        opt = kept[i]
        kept[i] = AskOption(
            label=opt.label,
            recommended=opt.recommended,
            cursor=False,
            number=opt.number,
            description=opt.description,
            selected=opt.selected,
        )
    return tuple(kept)


def _parse_question_options(options_input: Any) -> tuple[AskOption, ...]:
    """Build a tuple of ``AskOption`` from one JSONL ``question.options`` list.

    Skips entries that aren't strings or dicts, and drops entries whose label
    is empty. The returned tuple preserves source order; ``number`` is the
    1-based index. ``description`` carries the per-option reasoning text
    when the JSONL payload provides it; ``""`` otherwise.
    """
    if not isinstance(options_input, list):
        return ()
    options: list[AskOption] = []
    for idx, opt in enumerate(options_input, start=1):
        if isinstance(opt, str):
            label, description = opt, ""
        elif isinstance(opt, dict):
            raw_label = opt.get("label")
            label = raw_label if isinstance(raw_label, str) else ""
            raw_desc = opt.get("description")
            description = raw_desc if isinstance(raw_desc, str) else ""
        else:
            continue
        label = label.strip()
        if not label:
            continue
        recommended = bool(_RE_RECOMMENDED.search(label))
        if recommended:
            label = _RE_RECOMMENDED.sub("", label).rstrip()
        options.append(
            AskOption(
                label=label,
                recommended=recommended,
                cursor=False,
                number=idx,
                description=description.strip(),
                selected=None,
            )
        )
    return tuple(options)


def build_form_from_tool_input(
    tool_input: dict[str, Any] | None,
) -> AskUserQuestionForm | None:
    """Build an ``AskUserQuestionForm`` directly from a JSONL ``tool_use`` input.

    The tmux pane scrape captures only the visible region, so long question
    text pushes earlier options off the top of the screen — the user sees
    options 2..N and option 1 is gone. The structured ``tool_use.input`` in
    the session JSONL carries the complete option list and is order-stable.
    Prefer this over ``parse_ask_user_question`` for AskUserQuestion dispatch
    when the input dict is available.

    Returns ``None`` when the input is missing, malformed, or contains no
    parseable options. Callers should fall back to the pane parser.

    The structured payload Claude Code emits for AskUserQuestion is shaped:

        {
          "questions": [
            {"question": "...", "header": "...", "multiSelect": false,
             "options": [{"label": "...", "description": "..."}, ...]},
            ...
          ]
        }

    Multi-question forms populate ``form.questions`` with the full matrix.
    The legacy single-question fields (``current_question_title``, ``options``)
    mirror ``questions[0]`` so the existing renderer + fingerprint paths
    keep working without conditionals at every call site — ``resolve_ask_form``
    overlays the correct current-tab focus on top for multi-question forms.

    The picker UI also appends a "Type something" / "Chat about this" pair
    at the bottom — those are picker-internal and not part of the tool_use
    payload. We mint pick buttons only for the structured options; the
    keystroke fallback still reaches the picker-internal entries.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None

    parsed_questions: list[AskQuestion] = []
    multiselect_present = any(
        isinstance(q, dict) and "multiSelect" in q for q in questions_raw
    )
    select_mode = _tool_input_select_mode(tool_input)
    for q in questions_raw:
        if not isinstance(q, dict):
            continue
        title = q.get("question") or q.get("header") or ""
        header = q.get("header") or ""
        options = _parse_question_options(q.get("options"))
        if not options:
            # A question without parseable options is dropped — same as v1
            # behaviour for the single-question case. The render still
            # surfaces the other tabs; an empty tab would just produce a
            # body with no actionable options.
            continue
        parsed_questions.append(
            AskQuestion(
                title=title.strip() if isinstance(title, str) else "",
                header=header.strip() if isinstance(header, str) else "",
                options=options,
                multi_select=q.get("multiSelect") is True,
            )
        )

    if not parsed_questions:
        return None

    first = parsed_questions[0]
    return AskUserQuestionForm(
        tabs=(),
        current_question_title=first.title or None,
        options=first.options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt="",
        questions=tuple(parsed_questions),
        # No pane context here — defer to ``resolve_ask_form`` to decide
        # whether the current tab can be inferred. When this helper is
        # called in isolation (tests, legacy single-question callers),
        # default to True for back-compat with the single-question render
        # path (which never gates on this flag).
        current_tab_inferred=True,
        select_mode=select_mode,
        options_complete=True,
        _meta={"multiselect_present": "1" if multiselect_present else "0"},
    )


def _footer_block_contiguous_with_header(
    lines: list[str], block_top_idx: int, tab_header_idx: int
) -> bool:
    """True iff the footer-anchored option block is CONTIGUOUS with the tab header.

    PR-3 PR-A — footer-anchored stale-tab-header demotion. A multi-tab
    ``←…→`` header governs the option parse ONLY when it sits directly above
    the live footer-anchored option block: walking UP from the block's top to
    ``tab_header_idx`` crosses ONLY blank lines and question-title prose — NO
    picker-STRUCTURE marker.

    Genuine multi-tab layout — ``header, [blank], title-prose, [blank],
    options`` — reaches the header crossing only blanks + the title (contiguous
    → GOVERN). The title may span multiple physical lines AND multiple
    blank-separated paragraphs: Claude Code renders the whole ``question`` field
    as prose, so a wrapped / multi-paragraph title must NOT trigger a demote
    (hermes review — demoting on "second paragraph" was a false-demote on a live
    multi-tab AUQ).

    A STALE header left in deep scrollback by a PRIOR answered AUQ sits above
    that prior picker's STRUCTURE — ``─`` separators, its own ``←…→`` header,
    and answered/option ``☐``/``☒`` checkbox glyphs. Crossing ANY of those
    walking up means the header is NOT directly above the live block → the
    caller DEMOTES it and parses the footer-anchored live picker instead. These
    markers never appear between a genuine header and its option block, and
    prose titles never contain them, so they cleanly separate the two shapes
    (structural markers, not separator-count / line-gap, are the signal).

    Residual (disclosed, non-blocking — codex review): a stale single-question
    header with NOTHING but blanks + one prose line between it and a live block
    is indistinguishable from a genuine live multi-tab without another signal,
    so it cosmetically governs (stale ``tabs`` are discarded in JSONL
    resolution; the live options/title still render).
    """
    if block_top_idx <= tab_header_idx:
        # Degenerate: block top is at/above the header — treat as governing.
        return True
    for i in range(block_top_idx - 1, tab_header_idx, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue  # blank — allowed
        # Picker-STRUCTURE markers from a PRIOR (stale) picker → not contiguous.
        if all(c == "─" for c in stripped):
            return False  # separator row
        if _RE_TAB_HEADER.match(lines[i]):
            return False  # a second (prior) tab header
        if stripped[0] in ("☐", "☒"):
            return False  # answered/option checkbox-glyph row
        # else: question-title prose (possibly multi-line / multi-paragraph) —
        # allowed; keep walking toward the header.
    # Reached the header crossing only blanks + title prose.
    return True


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

    # Locate the picker footer ("Enter to select") near the bottom of the pane.
    # The single-tab options block sits immediately above this line. Scan
    # bottom-up so a stale footer earlier in the scrollback can't shadow
    # the live one, and search the entire captured buffer (scrollback may
    # extend far above the visible region for long question text).
    footer_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_PICKER_FOOTER.search(lines[i]):
            footer_idx = i
            break
    has_footer = footer_idx is not None

    # Review-screen + free-text markers stay scoped to the last 25 lines —
    # they only matter for the live picker state, not historic scrollback.
    recent_tail = lines[-25:]
    is_review = any(_RE_REVIEW_HEADER.match(line) for line in recent_tail) and any(
        _RE_SUBMIT_PROMPT.match(line) for line in recent_tail
    )
    is_free_text = any(_RE_FREE_TEXT_OPTION.search(line) for line in recent_tail)

    if tab_header_idx is None and not has_footer and not is_review:
        return None

    # Walk-back (single-tab path) captures a display-only question-title
    # candidate from the line above the options block. Tracked separately
    # from ``current_question_title`` (which goes into the fingerprint and
    # into ``_strong_match`` for JSONL-stale detection): the walk-back is
    # a heuristic guess that can accidentally pick up assistant prose or
    # stale scrollback, so feeding it into the matcher would risk
    # mis-overlaying stale JSONL labels onto a live pane (wrong-action
    # class). The renderer falls back to ``pane_walkback_title`` when
    # ``current_question_title`` is None — this gives the user context
    # for fresh pickers before Claude Code has flushed the AUQ
    # ``tool_use`` line to JSONL (2026-05-21 D5 incident at 22:49).
    walkback_stop_idx: int | None = None
    walkback_blank_gap: int = 0

    # Footer-anchored upward walk: compute the live single-tab option block's
    # top (``footer_block_top``) whenever a footer exists, INDEPENDENT of the
    # tab header. This is the live picker's anchor; the multi-tab header only
    # GOVERNS the parse when it is contiguous with this block (PR-3 PR-A
    # stale-header demotion below). Scan upward from the footer to find the
    # contiguous numbered-options block: walk backward until we hit a line
    # that's clearly not part of the options block (anything other than a
    # numbered option, a description continuation, a blank line, or a
    # separator). This captures option 1 even when the question text is long
    # enough to push it well above the last 25 lines.
    footer_block_top: int | None = None
    if footer_idx is not None:
        start_idx = footer_idx
        for j in range(footer_idx - 1, -1, -1):
            line = lines[j]
            stripped = line.strip()
            if not stripped:
                start_idx = j
                walkback_blank_gap += 1
                continue
            if _RE_NUMBERED_OPTION.match(line):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Separator line (only ─ chars).
            if all(c == "─" for c in stripped):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Description continuation — non-empty indented text within
            # ~7 lines (in either direction) of a numbered option. The
            # symmetric scan handles the LAST option's descriptions,
            # which only have a numbered option ABOVE them in file
            # order (the footer is below). Without the upward arm the
            # walk-back terminated at the last desc line, leaving
            # ``pane_opts=0`` and forcing ``_build_pick_button_rows``'s
            # ``fa5_guard`` to suppress option buttons on multi-Q AUQs
            # that Claude Code renders without a multi-tab header.
            # Bounded distance still rejects stale indented scrollback
            # that has no nearby option.
            if line.startswith(("  ", "\t")) and (
                any(
                    _RE_NUMBERED_OPTION.match(lines[k])
                    for k in range(j + 1, min(j + 8, footer_idx + 1))
                )
                or any(
                    _RE_NUMBERED_OPTION.match(lines[k]) for k in range(max(0, j - 7), j)
                )
            ):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Non-pattern line — title-display candidate. Only set
            # ``walkback_stop_idx`` here so the for-loop falling off
            # the top of the buffer (no break) keeps it at None: a
            # buffer that is entirely pattern lines has no title to
            # capture. Also reject indented lines as title candidates
            # — Claude Code's question text is rendered at column 0,
            # and indented lines above the topmost option are
            # invariably scrollback noise (hermes review, 2026-05-21).
            if not line.startswith(("  ", "\t")):
                walkback_stop_idx = j
            break
        footer_block_top = start_idx

    # Governance decision (PR-3 PR-A). The bare "any ←…→ header wins" rule
    # let a STALE tab header in deep scrollback hijack the parse whenever a
    # prior multi-tab AUQ was answered and a NEW single-tab picker rendered
    # below it: the bottom-up header scan found the stale header, the
    # multi-tab branch grabbed prose between it and the next separator as
    # "options", and the live footer-anchored picker was never parsed.
    if tab_header_idx is None:
        tab_header_governs = False
    elif footer_idx is None or footer_idx <= tab_header_idx:
        # No live footer below the header → the header IS the live picker.
        tab_header_governs = True
    else:
        # A footer sits below the header. The header governs ONLY when it is
        # contiguous with the footer-anchored live block (else it is stale
        # scrollback and is DEMOTED to the footer-anchored parse).
        assert footer_block_top is not None
        tab_header_governs = _footer_block_contiguous_with_header(
            lines, footer_block_top, tab_header_idx
        )

    tabs: tuple[AskTab, ...] = ()
    if tab_header_governs:
        assert tab_header_idx is not None
        parsed_tabs = _parse_tab_header(lines[tab_header_idx])
        if parsed_tabs is None:
            return None
        tabs = parsed_tabs

    # Collect options below the tab header (multi-tab, when it governs) or in
    # the picker region above the footer (single-tab / demoted-header). For
    # multi-tab, options live between the header and the next separator / next
    # tab header / picker footer.
    if tab_header_governs:
        assert tab_header_idx is not None
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
        # Walk-back title fields belong to the single-tab path only; the
        # multi-tab in-region title scan below sets current_question_title.
        walkback_stop_idx = None
        walkback_blank_gap = 0
    elif footer_block_top is not None:
        # footer_block_top is set only inside the ``footer_idx is not None``
        # block above, so the footer index is non-None here.
        assert footer_idx is not None
        options_region = lines[footer_block_top : footer_idx + 1]
    else:
        options_region = recent_tail

    options = _parse_numbered_options(options_region)
    pane_signal = _pane_glyph_signal(options_region)
    select_mode = _resolve_select_mode(
        None,
        pane_signal,
        is_review_screen=is_review,
    )

    # Multi-tab in-region title scan — sets the authoritative
    # ``current_question_title`` for layouts where Claude Code prints
    # the question text between the tab header and the first option.
    # Inputs to ``_strong_match`` and the fingerprint canonical come
    # from this field, so we only populate it from a region anchored
    # by a GOVERNING tab header (a strong "this is the picker" signal).
    # A demoted (stale-scrollback) header must not seed the title from
    # prose — the single-tab walk-back fills ``pane_walkback_title`` instead.
    current_question_title: str | None = None
    if tab_header_governs:
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

    # ``pane_walkback_title`` (display only): walked-back title for the
    # single-tab path. Bounded gap (≤2 blanks between candidate and
    # topmost option) keeps us from pulling in pre-picker scrollback.
    # Multi-line wraps capped at 3 physical lines so an entire stray
    # paragraph cannot get glued together and accidentally match a
    # JSONL substring (hermes review, 2026-05-21). The renderer falls
    # back to this field when ``current_question_title`` is None.
    pane_walkback_title: str | None = None
    if walkback_stop_idx is not None and walkback_blank_gap <= 2:
        parts: list[str] = [lines[walkback_stop_idx].strip()]
        for k in range(walkback_stop_idx - 1, -1, -1):
            if len(parts) >= 3:
                break
            prev_line = lines[k]
            prev_stripped = prev_line.strip()
            if not prev_stripped:
                break
            if _RE_NUMBERED_OPTION.match(prev_line):
                break
            if all(c == "─" for c in prev_stripped):
                break
            if prev_line.startswith(("  ", "\t")):
                # Indented prior content is either an option-description
                # continuation or unrelated bullet text — not part of the
                # title. (Tmux's pane capture does not re-indent
                # soft-wrapped lines, so a wrapped title's continuation
                # would start at column 0.)
                break
            parts.append(prev_stripped)
        pane_walkback_title = " ".join(reversed(parts))

    # Build a pane excerpt for verbatim fallback rendering. Pin it to the
    # GOVERNING tab header, else the footer-anchored live block (so a demoted
    # stale header's scrollback is excluded — PR-3 PR-A), else the last ~25
    # lines — the renderer won't use the full pane scrollback.
    if tab_header_governs:
        assert tab_header_idx is not None
        excerpt_start = tab_header_idx
    elif footer_block_top is not None:
        excerpt_start = footer_block_top
    else:
        excerpt_start = max(0, len(lines) - 25)
    pane_excerpt = "\n".join(lines[excerpt_start:]).rstrip()

    options_contiguous = bool(options) and [o.number for o in options] == list(
        range(1, len(options) + 1)
    )
    # A pure-pane picker is "complete" when we can see option 1 (contiguous
    # from 1 = top of the list present) AND an affordance OPTION ROW
    # ("Type something" / "Chat about this") was actually parsed in the option
    # block. Claude Code always renders those affordance rows at the BOTTOM of
    # the option list, so a parsed affordance row proves we captured the whole
    # list rather than a scrolled tail. We require an affordance *row in the
    # option block* — NOT the weaker ``is_free_text`` tail-substring scan, which
    # could be tripped by question text or an option description containing the
    # phrase "Type something" (hermes review 2026-05-31). Conservative: if no
    # affordance row is in-block or numbering doesn't start at 1,
    # options_complete stays False (toggle buttons suppressed → keystroke-nav
    # fallback), never a wrong dispatch.
    affordance_row_in_block = any(
        (_m := _RE_NUMBERED_OPTION.match(line)) is not None
        and is_affordance_label(_strip_option_checkbox(_m.group("label").strip()))
        for line in options_region
    )
    options_complete = options_contiguous and affordance_row_in_block

    return AskUserQuestionForm(
        tabs=tabs,
        current_question_title=current_question_title,
        options=options,
        is_review_screen=is_review,
        is_free_text=is_free_text,
        pane_excerpt=pane_excerpt,
        pane_walkback_title=pane_walkback_title,
        select_mode=select_mode,
        options_complete=options_complete,
    )


# ── Interactive approval-gate parsers (Permission / Workflow) ─────────────
#
# Strict-or-``None`` parsers modeled on ``parse_ask_user_question``. Each
# emits a single-question ``AskUserQuestionForm`` (``select_mode="single"``,
# ``is_review_screen=False``) whose ``AskOption`` rows come from the
# ``❯ N. <label>`` block above the gate footer. The FULL option label is
# carried (only a trailing ``(esc)`` affordance is stripped — deterministic
# on every parse) so PR-2's ``_loose_label_match`` cannot confuse
# "Yes" with "Yes, and don't ask again …" (S-6). DISPLAY-ONLY in PR-1.


def _gate_options_above(lines: list[str], footer_idx: int) -> tuple[AskOption, ...]:
    """Collect the BOTTOM-MOST contiguous ``❯ N. <label>`` option block above
    ``footer_idx``.

    Walks UP from the footer over blank / separator / option / indented-
    description lines, but ONLY extends across a numbered line while it keeps the
    block contiguous DOWNWARD (its ``N`` is exactly one less than the topmost
    option seen so far). This isolates the live option block (``1. Yes, run it /
    2. View raw script / 3. No``) from a SEPARATE numbered run higher up — the
    Workflow phase list (``1. Sweep / 2. Verify / 3. Dossier``), which sits above
    the option block and would otherwise be absorbed as the options (codex P2).
    The bottom-most block's first number resets the contiguity, so the walk-up
    stops at the phase list's footer-side boundary. Then reuses
    ``_parse_numbered_options`` (contiguity + cursor + checkbox handling). A
    trailing ``(esc)`` affordance is stripped from each label deterministically
    (S-6 full-label parity on mint==verify).
    """
    start_idx = footer_idx
    # The lowest option number seen so far in the contiguous block (the block is
    # bottom-up, so this tracks the TOP of the run as it grows upward).
    block_top_num: int | None = None
    for j in range(footer_idx - 1, -1, -1):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            start_idx = j
            continue
        m = _RE_NUMBERED_OPTION.match(line)
        if m is not None:
            try:
                num = int(m.group("num"))
            except ValueError:
                break
            if block_top_num is None:
                # First (bottom-most) numbered line — anchors the block.
                block_top_num = num
                start_idx = j
                continue
            if num == block_top_num - 1:
                # Extends the contiguous run upward (3 ← 2 ← 1).
                block_top_num = num
                start_idx = j
                continue
            # A numbered line that does NOT continue the run (a reset, e.g. a
            # higher phase ``3.`` directly above the option block's ``1.``) — the
            # bottom-most block is complete. Stop here so the phase list is not
            # folded in.
            break
        if all(c == "─" for c in stripped):
            start_idx = j
            continue
        # Indented description continuation within a few lines of an option.
        if line.startswith(("  ", "\t")) and any(
            _RE_NUMBERED_OPTION.match(lines[k])
            for k in range(j + 1, min(j + 6, footer_idx + 1))
        ):
            start_idx = j
            continue
        break
    region = lines[start_idx : footer_idx + 1]
    raw = _parse_numbered_options(region)
    return tuple(
        AskOption(
            label=_strip_esc_affordance(o.label),
            recommended=o.recommended,
            cursor=o.cursor,
            number=o.number,
            description=o.description,
            selected=o.selected,
        )
        for o in raw
    )


# ── Bottom-terminal requirement (S-8 fail-closed; round-2 Codex P1) ────────
#
# A genuine LIVE approval gate is the ACTIVE bottom prompt: when Claude blocks
# on it, the gate REPLACES the entire input box / status bar — the option block
# + footer (plus the gate's own ``ctrl+<x>`` footer continuations) is the LAST
# semantic content in the pane. EMPIRICAL RESOLUTION (round-2,
# ``permission_webfetch_bgshells_v2.1.190.txt``, captured WITH 2 background
# shells running): a live gate has NO ``❯`` input box, NO ``? for shortcuts``
# status bar, and NO ``· N shell`` line below its footer — the ``· 2 shells``
# line lives in the scrollback ABOVE the gate, never below it.
#
# So ``_only_chrome_below`` is an ALLOW-LIST (round-2 tightening over the
# round-1 version, which wrongly allowed the input box + status bar and let a
# fully-quoted gate-in-scrollback + the pane's normal input box still pass):
# below the footer only BLANK lines, BARE box-drawing separators, and the
# gate's OWN ``ctrl+<x>`` footer-continuation hints (``ctrl+g to edit script`` /
# ``ctrl+e to explain`` / other ``ctrl+<x>`` continuations) are allowed. The
# READY-FOR-INPUT chrome that only renders when the gate is NOT the live prompt
# — the ``❯`` input box, the ``? for shortcuts`` / ``← for agents`` /
# ``↓ to manage`` / ``esc to interrupt`` status bar, the ``· N shell(s)``
# background-jobs line, the ``◐ … /effort`` indicator, the model/context status
# bar — and any non-blank assistant prose all REJECT (a live gate replaces
# them). Hermes's "a live gate with ``· N shell`` below its footer would be
# false-negatived" worry is REFUTED by the bgshells capture (no status line
# below a live footer), so the check is deliberately NOT loosened for it.
#
# Codex was correct (the input-box/status chrome reject closes the realistic
# quoted-gate false positive); Hermes's false-negative is refuted by data.
#
# DEFERRED RESIDUAL (now NARROW; PR-2, do NOT fix in PR-1): a fully-quoted gate
# that is the LITERAL last semantic content in the pane — with NO ready-for-input
# chrome (no input box / status bar) below it — is indistinguishable from a live
# gate by pane content alone, so it still passes. This is rare: it requires the
# pane to be captured with the quoted gate at the very bottom AND Claude not
# showing its input box (e.g. the capture landed between frames). In PR-1
# (display-only) that is at worst a cosmetic bogus card — no dispatch, no
# auto-approval. The definitive close belongs in PR-2 (where dispatch makes it
# matter): gate the gate-card render/promotion on the route's
# ``route_runtime.snapshot(route).notification_pending`` bit — a GENUINE gate
# fires the Notification hook; quoted prose does not. It is deliberately NOT
# coupled here in PR-1 (PR-1 stays pane-only per the plan): tying render to the
# notification bit risks delaying a legitimate card on the hook's timing, and
# PR-1 ships no dispatch. The empirically-tightened chrome check closes the
# realistic case.

# A BARE box-drawing / separator / banner line (no other content). Tolerated
# below the footer ONLY when nothing ready-for-input follows it — a separator
# that FRAMES an input box is harmless on its own (the input-box rule rejects
# the ``❯`` line itself).
_RE_GATE_TRAILING_SEPARATOR = re.compile(r"^\s*[─╌╭╮╰╯│┌┐└┘├┤┬┴┼━┃▐▌▛▜▝▘▗▖█\s]+$")
# The gate's OWN footer continuation: a ``ctrl+<x> …`` hint line (``ctrl+g to
# edit script`` for Workflow, ``ctrl+e to explain``, etc.) that renders on its
# own line BELOW the ``Esc to cancel`` footer. char-class tolerant on the
# ``+``/``-`` join.
_RE_GATE_TRAILING_CTRL_HINT = re.compile(r"^\s*ctrl[+-]\S")


def _only_chrome_below(lines: list[str], footer_idx: int) -> bool:
    """True iff every non-blank line BELOW ``footer_idx`` is the gate's OWN
    footer chrome (round-2 ALLOW-LIST, Codex P1).

    The bottom-terminal requirement: a live gate is the ACTIVE bottom prompt and
    REPLACES the input box / status bar, so below the footer ONLY blank lines,
    BARE box-drawing separators, and the gate's own ``ctrl+<x>`` footer
    continuations are allowed. The ``❯`` input box, the ``? for shortcuts`` /
    ``← for agents`` / ``↓ to manage`` / ``esc to interrupt`` status bar, the
    ``· N shell(s)`` background-jobs line, the ``◐ … /effort`` indicator, the
    model/context status bar, and any assistant prose all mean the gate is NOT
    the live prompt (it is QUOTED in scrollback above a still-ready pane) ⇒
    return False. Note the option cursor ``❯ 1.`` is ABOVE the footer, so any
    ``❯`` line below it is the input box.
    """
    for i in range(footer_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if _RE_GATE_TRAILING_CTRL_HINT.match(line):
            continue
        if _RE_GATE_TRAILING_SEPARATOR.match(line):
            continue
        # Anything else — an ``❯`` input box, a status-bar / shell-count line,
        # the ``◐ /effort`` indicator, or arbitrary assistant prose — is
        # ready-for-input chrome / quoted prose, never a live gate's own footer.
        return False
    return True


def parse_permission_prompt(pane_text: str) -> AskUserQuestionForm | None:
    """Strict-or-None parse of a tool-permission approval gate (Gate A).

    Anchors on a ``Do you want to <verb>…?`` question TOP line (REQUIRED — the
    ``Claude wants to …`` preamble is OPTIONAL context only, never a sufficient
    standalone anchor; Hermes P2) and a bottom anchor that is EITHER an inline
    ``(esc)``-tailed option (WebFetch, no footer) OR an ``Esc to cancel`` footer
    (Bash / Write). Enforces the bottom-terminal requirement (only chrome below
    the footer — S-8). Returns a single-question ``AskUserQuestionForm``
    (``select_mode="single"``) with the full, affordance-stripped option labels,
    or ``None`` when the pane is not a recognizable LIVE permission gate. Cursor
    / number / checkbox handling and contiguity are inherited from
    ``_parse_numbered_options``.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")

    # Bottom anchor — lowest-on-screen match wins (live footer beats a stale
    # scrollback one). Accept EITHER the inline ``(esc)`` option OR the footer.
    footer_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_PERMISSION_BOTTOM_INLINE_ESC.match(
            lines[i]
        ) or _RE_PERMISSION_BOTTOM_FOOTER.match(lines[i]):
            footer_idx = i
            break
    if footer_idx is None:
        return None

    # Bottom-terminal requirement (S-8): only chrome may follow the footer, else
    # this is a QUOTED gate (prose continues below) — not the live prompt.
    if not _only_chrome_below(lines, footer_idx):
        return None

    # TOP question line — the lowest ``Do you want to <verb>…?`` ABOVE the
    # footer. REQUIRED (Hermes P2): the ``Claude wants to …`` preamble is OPTIONAL
    # context (it enriches the card body / preamble) and is NOT a sufficient
    # standalone anchor — a quoted ``Claude wants to …`` line without the real
    # question must not light a card.
    question_idx: int | None = None
    preamble_idx: int | None = None
    for i in range(footer_idx, -1, -1):
        if question_idx is None and _RE_PERMISSION_TOP_QUESTION.match(lines[i]):
            question_idx = i
        if preamble_idx is None and _RE_PERMISSION_TOP_PREAMBLE.match(lines[i]):
            preamble_idx = i
    if question_idx is None:
        return None

    options = _gate_options_above(lines, footer_idx)
    if not options:
        return None

    title = lines[question_idx].strip()

    # Body excerpt starts at the optional ``Claude wants to …`` preamble when it
    # sits just above the question (display context), else the question line.
    excerpt_start = question_idx
    if preamble_idx is not None and preamble_idx < question_idx:
        excerpt_start = preamble_idx
    pane_excerpt = "\n".join(lines[excerpt_start : footer_idx + 1]).rstrip()

    return AskUserQuestionForm(
        current_question_title=title,
        options=options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt=pane_excerpt,
        select_mode="single",
        options_complete=options[0].number == 1,
    )


# Workflow body lines: the token-cost warning sentence + the phase list.
_RE_WORKFLOW_WARNING = re.compile(r"Dynamic workflows can use\b")

# Known Workflow option labels (codex P2). The launch gate is a fixed
# 3-option shape: ``Yes, run it`` / ``View raw script`` / ``No``. Validating the
# parsed labels against this shape rejects a numbered block that is actually the
# PHASE list (``Sweep`` / ``Verify`` / ``Dossier``) — the option-1 ``Yes`` anchor
# is the load-bearing check (the costly resolving option), with ``View raw
# script`` as a secondary confirmation that this is the launch gate and not some
# other numbered region. Case-insensitive substring tolerates trailing drift.
_RE_WORKFLOW_OPT_YES = re.compile(r"^\s*Yes,\s*run it\b", re.IGNORECASE)
_RE_WORKFLOW_OPT_VIEW = re.compile(r"\bView raw script\b", re.IGNORECASE)


def _is_workflow_option_shape(options: tuple[AskOption, ...]) -> bool:
    """True iff ``options`` look like the Workflow launch gate's 3-option shape.

    Requires option 1 == ``Yes, run it`` (the costly resolving option, S-6
    load-bearing) AND that a ``View raw script`` option is present — so the
    Workflow PHASE list (``1. Sweep / 2. Verify / 3. Dossier``), which shares the
    ``N. <text>`` shape, is rejected (codex P2).
    """
    if not options:
        return False
    if not _RE_WORKFLOW_OPT_YES.match(options[0].label):
        return False
    return any(_RE_WORKFLOW_OPT_VIEW.search(o.label) for o in options)


def parse_workflow_approval(pane_text: str) -> AskUserQuestionForm | None:
    """Strict-or-None parse of the dynamic-workflow-launch approval gate (Gate B).

    Anchors on a Workflow TOP (``Run a dynamic workflow?`` /
    ``This dynamic workflow will`` / ``Dynamic workflows can use``) and the
    ``Esc to cancel`` / ``Tab to amend`` footer. Returns a single-question
    ``AskUserQuestionForm`` (``select_mode="single"``) with the full option
    labels (``Yes, run it`` / ``View raw script`` / ``No``). The phases list
    and the token-cost warning are stashed in ``_meta`` (display-only body
    text for the card) so PR-1 can surface them without re-scraping. Returns
    ``None`` when the pane is not a recognizable Workflow gate.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")

    # Footer (lowest-on-screen) — the ``Esc to cancel`` footer line.
    footer_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if any(p.match(lines[i]) for p in _RE_WORKFLOW_BOTTOM):
            footer_idx = i
            break
    if footer_idx is None:
        return None

    # Bottom-terminal requirement (S-8): only chrome may follow the footer (the
    # ``ctrl+g to edit script`` line is chrome) — else this is a QUOTED gate
    # (assistant prose continues below the footer), not the live prompt.
    if not _only_chrome_below(lines, footer_idx):
        return None

    # TOP — anchor the region on the FIRST (topmost) Workflow anchor line that
    # belongs to the live gate, i.e. the topmost anchor in the CONTIGUOUS run
    # of anchor / body / option / blank / separator lines above the footer.
    # ``Run a dynamic workflow?`` heads the gate; the phase list + token-cost
    # warning sit between it and the options. Walking up from the footer over
    # those line kinds captures the whole gate region (phases included).
    top_anchor_idx: int | None = None
    for i in range(footer_idx, -1, -1):
        if any(p.match(lines[i]) for p in _RE_WORKFLOW_TOP):
            top_anchor_idx = i
    if top_anchor_idx is None:
        return None

    options = _gate_options_above(lines, footer_idx)
    if not options:
        return None

    # Option-shape validation (codex P2): the parsed block must be the Workflow
    # launch options (``Yes, run it`` / ``View raw script`` / ``No``), not the
    # PHASE list (``Sweep`` / ``Verify`` / ``Dossier``) — same ``N. <text>`` shape.
    if not _is_workflow_option_shape(options):
        return None

    # The question title prefers the ``Run a dynamic workflow?`` line when
    # present (the gate's heading), else the topmost matched anchor line.
    title = lines[top_anchor_idx].strip()
    for i in range(top_anchor_idx, footer_idx + 1):
        if re.match(r"^\s*Run a dynamic workflow\?", lines[i]):
            title = lines[i].strip()
            break

    # Region: from the gate heading (``Run a dynamic workflow?`` when present,
    # else the topmost anchor) down to the footer, so the phases + warning are
    # in the body.
    region_start = top_anchor_idx
    for i in range(top_anchor_idx, footer_idx + 1):
        if re.match(r"^\s*Run a dynamic workflow\?", lines[i]):
            region_start = i
            break
    region = lines[region_start : footer_idx + 1]
    pane_excerpt = "\n".join(region).rstrip()

    # Display-only body text for the card: the phase list + the token-cost
    # warning. The whole gate region (``pane_excerpt``) reliably carries both,
    # and the phase bullets (``1. Summarize …``) are visually
    # indistinguishable from the option block by regex alone (both are
    # ``N. <text>``), so we deliberately surface the full region rather than a
    # fragile body slice. Stashed in ``_meta`` (excluded from the fingerprint —
    # display-only) so PR-1's render branch can show phases + warning without
    # re-scraping the pane. ``has_token_warning`` flags the warning sentence.
    has_warning = any(_RE_WORKFLOW_WARNING.search(line) for line in region)
    return AskUserQuestionForm(
        current_question_title=title,
        options=options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt=pane_excerpt,
        select_mode="single",
        options_complete=options[0].number == 1,
        _meta={
            "workflow_body": pane_excerpt,
            "has_token_warning": "1" if has_warning else "0",
        },
    )


# ── Generic decision-prompt parser (Stage B1, flag-gated, LAST) ───────────


def _decision_option_block_top(lines: list[str], footer_idx: int) -> int | None:
    """Index of the TOP-most line of the bottom-most contiguous ``N. <label>``
    option block above ``footer_idx``.

    Mirrors ``_gate_options_above``'s contiguity walk (extends across a numbered
    line ONLY while its ``N`` keeps the run monotonic downward, so a separate
    higher numbered run — a stray list above the prompt — is not folded in), so
    the Decision title search starts just above the REAL option block. Returns
    ``None`` when no numbered option is found.
    """
    block_top_idx: int | None = None
    block_top_num: int | None = None
    for j in range(footer_idx - 1, -1, -1):
        stripped = lines[j].strip()
        if not stripped:
            continue
        m = _RE_NUMBERED_OPTION.match(lines[j])
        if m is not None:
            try:
                num = int(m.group("num"))
            except ValueError:
                break
            if block_top_num is None:
                block_top_num = num
                block_top_idx = j
                continue
            if num == block_top_num - 1:
                block_top_num = num
                block_top_idx = j
                continue
            break
        if all(c == "─" for c in stripped):
            continue
        # An indented description continuation within a few lines of an option.
        if lines[j].startswith(("  ", "\t")) and any(
            _RE_NUMBERED_OPTION.match(lines[k])
            for k in range(j + 1, min(j + 6, footer_idx + 1))
        ):
            continue
        break
    return block_top_idx


# The prompt block above the option block can span several lines (a heading, a
# subtitle, a body paragraph) separated by SINGLE blank lines. The card must
# show the heading, so the excerpt extends UP through that contiguous block —
# bounded so a runaway scrollback walk can't absorb unrelated content.
_DECISION_PROMPT_BLOCK_MAX_LINES: Final[int] = 10


def _decision_prompt_block_top(lines: list[str], block_top_idx: int) -> int | None:
    """Index of the TOP meaningful line of the contiguous prompt block above the
    option block at ``block_top_idx`` (the heading the display-only card shows).

    Walks UP from just above the option block, tolerating SINGLE blank lines
    (paragraph spacing WITHIN one prompt), and STOPS at a CLEAN TERMINATOR — a
    run of ≥2 consecutive blank lines (a gap to unrelated scrollback), a chrome /
    box-drawing separator line (the welcome banner / rule above the prompt), or
    the top of the pane (BOF) — returning the top meaningful line seen so far.

    §5a / P3-3 fix: when the ``_DECISION_PROMPT_BLOCK_MAX_LINES`` bound is
    exhausted WITHOUT hitting a clean terminator (an unbounded paragraph runs
    straight into the options), the title becomes **None** rather than a
    mid-paragraph fragment — the fragment fed the card, the confirmation text,
    and (in B2) BOTH dispatch fingerprints, so a stray mid-line was a stability
    hazard. A title-less Decision still renders (the excerpt falls back to the
    option block). Returns the top meaningful line's index, ``None`` on
    bound-overflow-without-terminator, or ``None`` when there is no prompt
    content above the options at all.
    """
    top_idx: int | None = None
    blank_run = 0
    meaningful = 0
    for j in range(block_top_idx - 1, -1, -1):
        stripped = lines[j].strip()
        if not stripped:
            blank_run += 1
            if blank_run >= 2:
                # Clean terminator: a ≥2-blank gap to unrelated scrollback.
                return top_idx
            continue
        if all(c == "─" for c in stripped) or _RE_GATE_TRAILING_SEPARATOR.match(
            lines[j]
        ):
            # Clean terminator: a chrome / box-drawing separator line.
            return top_idx
        blank_run = 0
        meaningful += 1
        if meaningful > _DECISION_PROMPT_BLOCK_MAX_LINES:
            # §5a / P3-3: bound exhausted with NO clean terminator — the block
            # runs into an unbounded paragraph. Return None (a title-less
            # Decision still renders) instead of a mid-paragraph fragment.
            return None
        top_idx = j
    # Reached the top of the pane (BOF) — a clean start-of-block boundary.
    return top_idx


# ── Decision footer-shape allow-list (§4 / P3-1) ──────────────────────────
#
# A live Decision confirmation FOOTER is a single line of ``·``-separated
# KEY-HINT segments (``Enter to confirm`` / ``Esc to cancel`` / ``Tab to amend``
# / ``ctrl+g to edit script`` / ``↑/↓ to navigate``) — NOT a numbered option
# that merely CONTAINS the footer phrase. A mid-redraw AUQ frame can transiently
# render the footer text INSIDE an option label (``3. Enter to confirm · Esc to
# cancel``); the bare ``_RE_DECISION_FOOTER.search`` accepted that option row as
# the footer and folded it into the option block, a wrong-target mint hazard
# once the B2 buttons dispatch. Each ``·``-segment must be a ``<key> to
# <action>`` hint; over-strict is the safe direction (a footer with an
# unrecognized segment simply isn't detected — it costs only detection).
#
# WHOLE-segment validation with a BOUNDED action tail (Codex wave-1 P1): each
# segment must FULLMATCH ``<key(s)> to <word>`` + at most TWO further words. A
# prefix-only ``.match`` accepted ``Esc to cancel was shown in a quoted
# example`` (a quoted/prose footer line — a false-Decision enabler, violating
# the §4 "decompose ENTIRELY into key-hint segments" contract); a bare
# end-anchor over a GREEDY word tail would accept the same prose. The ≤3-word
# tail fits every REAL observed footer hint (``Enter to confirm`` / ``Enter to
# continue`` / ``Esc to cancel`` / ``Esc to exit`` / ``Tab to amend`` /
# ``Shift+Tab to navigate`` / ``ctrl+g to edit script`` / ``ctrl+e to explain``
# / ``↑/↓ to navigate`` — pinned by test) while a prose continuation overruns
# the bound and fails.
_RE_DECISION_HINT_SEGMENT = re.compile(
    r"(?:"
    r"(?:enter|esc|escape|tab|space|return|shift[+-]tab|del|delete)"
    r"|ctrl[+-]\S+"
    r"|[↑↓←→](?:\s*/\s*[↑↓←→])*"
    r")"
    r"\s+to\s+\S+(?:\s+\S+){0,2}",
    re.IGNORECASE,
)


def _is_decision_footer_line(line: str) -> bool:
    """True iff ``line`` is a live Decision confirmation FOOTER (§4 / P3-1).

    Beyond carrying the required ``Enter to (confirm|continue)`` component, a
    footer candidate must (i) NOT be a numbered option, (ii) NOT be a
    ``❯``-cursored prompt row, and (iii) be footer-SHAPED — decompose ENTIRELY
    into ``·``-separated key-hint segments, each FULLMATCHING the bounded
    ``_RE_DECISION_HINT_SEGMENT`` shape (Codex wave-1 P1: prefix-only matching
    accepted a hint head with a prose tail). This rejects a mid-redraw AUQ
    option row whose LABEL embeds the footer phrase and a quoted/prose footer
    line, both of which the bare ``_RE_DECISION_FOOTER.search`` would accept.
    Mirrors ``_only_chrome_below``'s allow-list approach.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # (iii-a) the required affirmative-commit component.
    if not _RE_DECISION_FOOTER.search(line):
        return False
    # (i) a numbered option is never a footer (the poisoned-label case).
    if _RE_NUMBERED_OPTION.match(line):
        return False
    # (ii) a cursored prompt row is never a footer.
    if stripped[0] in "❯›▶*":
        return False
    # (iii-b) every ·-separated segment must be ENTIRELY a recognized, bounded
    # key hint (fullmatch — a hint head with a prose tail fails).
    return all(
        seg.strip() != "" and _RE_DECISION_HINT_SEGMENT.fullmatch(seg.strip())
        for seg in stripped.split("·")
    )


def parse_generic_decision(pane_text: str) -> AskUserQuestionForm | None:
    """Strict-or-None parse of a GENERIC titled numbered-option confirmation
    prompt (Stage B1 — the "Switch model?" confirmation, the folder-trust
    prompt, and peers that no NAMED pattern covers).

    Behind the ``CC_TELEGRAM_DECISION_CARDS`` flag (default OFF) and ordered
    LAST in ``UI_PATTERNS`` (``extract_interactive_content`` reaches it only
    when every named pattern — AUQ / EPM / Settings / RestoreCheckpoint /
    Permission / Workflow — declined first-match-wins). All requirements
    fail-closed → ``None``:

      1. a bottom-most FOOTER-SHAPED line (``_is_decision_footer_line``) that
         carries a live ``Enter to (confirm|continue)`` component (the
         affirmative-commit half of a confirmation dialog — verified on both
         real targets: ``Enter to confirm · Esc to cancel``). §4 / P3-1: the
         candidate must decompose ENTIRELY into ``·``-separated key hints and
         must NOT be a numbered option / ``❯``-cursored row, so a mid-redraw AUQ
         option whose LABEL embeds the footer phrase (``3. Enter to confirm ·
         Esc to cancel``) is rejected instead of folded into the option block.
         REQUIRING ``Enter to (confirm|continue)`` — rather than accepting a
         bare ``Esc to cancel`` / ``Esc to exit`` — STRUCTURALLY closes the
         verb-drift veto bypass (Codex P2): the Permission / EPM footer family
         (``Esc to cancel · Tab to amend``) has no ``Enter to confirm`` line, so
         a permission gate whose verb is outside ``parse_permission_prompt``'s
         whitelist (e.g. ``Do you want to open …?``) can no longer match here at
         all, independent of the veto below. ``Enter to select`` is DELIBERATELY
         excluded (AUQ pattern 3's footer);
      2. ``_only_chrome_below`` True — the live-bottom-prompt guard: a QUOTED
         prompt with a ready-for-input input box / status bar below it rejects;
      3. ``_gate_options_above`` → ≥2 contiguous numbered options AND a
         resolved live ``❯`` cursor;
      4. the STRICT-VALIDATOR VETO (Hermes P2-4; KEPT as defense-in-depth beside
         the footer narrowing): if ``parse_permission_prompt`` OR
         ``parse_workflow_approval`` parses this pane, return ``None`` — a real
         permission / workflow gate is NEVER re-surfaced as a generic Decision
         even when its OWN flag (``CC_TELEGRAM_PERMISSION_PROMPTS``) is OFF (the
         cross-flag re-exposure fix; strict validators, never a loose regex).

    Returns a single-question ``AskUserQuestionForm`` (``select_mode="single"``,
    ``is_review_screen=False``). ``current_question_title`` is the TOP meaningful
    line of the contiguous prompt block above the options (the heading, e.g.
    "Switch model?"); ``pane_excerpt`` spans that whole block → footer so the
    card body shows the heading + context + options (Hermes P3 / Codex).

    ACCEPTED NARROW RESIDUAL (Codex P1 / Hermes P2, flag-OFF by default,
    display-only): a QUOTED decision block that is the LITERAL last content in
    the pane with NO ready-for-input chrome below it (no input box / status bar)
    passes ``_only_chrome_below`` and would surface + promote RUNNING→WAITING.
    In a REAL running pane the input box + status bar are ALWAYS below the prose
    and ``_only_chrome_below`` rejects it, so this is a narrow capture-race case
    (the frame landed between the prose and the input box) — the SAME class as
    the existing Permission / Workflow gate residual. Cosmetic-only in Stage B1
    (no dispatch); the definitive live-signal close (gate on
    ``notification_pending``) is deferred to Stage B2, where dispatch makes it
    matter. Deliberately NOT closed with a heading/family allowlist — the
    detector is intentionally GENERIC.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")

    # (1) Bottom-most confirmation footer (live footer beats a stale scrollback
    # one). §4 / P3-1: the candidate must be footer-SHAPED — not a numbered
    # option / not ``❯``-cursored / all ``·``-separated key hints — so a
    # mid-redraw option row whose LABEL embeds the footer phrase is rejected.
    footer_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _is_decision_footer_line(lines[i]):
            footer_idx = i
            break
    if footer_idx is None:
        return None

    # (2) Bottom-terminal requirement — only the prompt's own chrome may follow.
    if not _only_chrome_below(lines, footer_idx):
        return None

    # (3) Option block: ≥2 contiguous numbered options + a resolved live cursor.
    options = _gate_options_above(lines, footer_idx)
    if len(options) < 2:
        return None
    if not any(o.cursor for o in options):
        return None

    # (4) STRICT-VALIDATOR VETO — never re-surface a permission / workflow gate
    # (even when its own flag filtered it out of the detector).
    if parse_permission_prompt(pane_text) is not None:
        return None
    if parse_workflow_approval(pane_text) is not None:
        return None

    # Title + excerpt (Hermes P3 / Codex): capture the FULL contiguous prompt
    # block above the option block so the actual heading is visible in the
    # display-only card (not just the body line nearest the options). Title =
    # the TOP meaningful line of that block; ``pane_excerpt`` = block heading →
    # footer (the trusted region shown as the card body).
    block_top_idx = _decision_option_block_top(lines, footer_idx)
    title: str | None = None
    excerpt_start = block_top_idx if block_top_idx is not None else footer_idx
    if block_top_idx is not None:
        prompt_top_idx = _decision_prompt_block_top(lines, block_top_idx)
        if prompt_top_idx is not None:
            title = lines[prompt_top_idx].strip()
            excerpt_start = prompt_top_idx
    pane_excerpt = "\n".join(lines[excerpt_start : footer_idx + 1]).rstrip()

    return AskUserQuestionForm(
        current_question_title=title,
        options=options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt=pane_excerpt,
        select_mode="single",
        options_complete=options[0].number == 1,
    )


def decision_prompt_fingerprint(form: AskUserQuestionForm) -> str:
    """Body-inclusive identity for a Stage-B2 ``Decision`` prompt (§3b).

    The AUQ ``fingerprint`` (``_canonical_repr``) cannot tell two folder-trust
    prompts for DIFFERENT directories apart — their title + option labels are
    identical and only the body path differs — so a stale ``dcp:`` tap on
    prompt A could dispatch into a byte-identical prompt B for another
    directory. This canonical folds the prompt BODY in.

    Assembled ONLY from STRUCTURED parse fields — never regex-stripped raw text:

      - a literal ``"decision:"`` DOMAIN PREFIX — so the hashed input, and hence
        the 8-char ``fp8`` slice used for the shared ``auq_action_ledger.jsonl``
        key, can NEVER collide with the AUQ lane's bare ``_canonical_repr``
        (cross-lane fp8 collision is impossible BY CONSTRUCTION — §8);
      - the title VERBATIM (``current_question_title``; ``None`` → empty — NO
        regex mutation);
      - the excerpt BODY lines between the prompt-block top and the option-block
        top, VERBATIM (only per-line trailing-whitespace trim + blank-line drop
        — leading bytes preserved);
      - per-option ``number:label`` pairs exactly as ``_parse_numbered_options``
        emitted them (the parser isolates the leading ``❯`` cursor STRUCTURALLY,
        so a label never carried the glyph).

    NO glyph stripping of title / body bytes, EVER: a directory path carrying a
    literal ``❯`` / ``☑`` / ``[x]`` keeps its bytes, so it can never collide with
    its stripped twin (round-2 P1-2). Cursor-blindness comes from EXCLUDING the
    per-option cursor METADATA, not from mutating text — moving the ``❯`` cursor
    across option rows does NOT rotate the identity (the ``dcp:`` dispatch
    NAVIGATES the cursor before committing, so the identity must stay
    cursor-stable, mirroring ``_canonical_repr``).

    Returns a stable 16-char hex digest (``sha1[:16]`` — the repo fingerprint
    convention, matching ``AskUserQuestionForm.fingerprint``).
    """
    title = form.current_question_title or ""
    body_lines: list[str] = []
    excerpt_lines = form.pane_excerpt.split("\n") if form.pane_excerpt else []
    if excerpt_lines:
        # The strict footer predicate, NOT the bare _RE_DECISION_FOOTER: a
        # validated form's excerpt always ends at a strict footer, so both
        # find the same line on parser-produced forms — but a manually built
        # form (or a future non-parser caller) must never have its body
        # boundary picked by the poisoned-label-tolerant bare regex the
        # B2.1 hardening exists to reject (wave-2 review fold, Hermes P3).
        footer_idx: int | None = None
        for i in range(len(excerpt_lines) - 1, -1, -1):
            if _is_decision_footer_line(excerpt_lines[i]):
                footer_idx = i
                break
        option_top = (
            _decision_option_block_top(excerpt_lines, footer_idx)
            if footer_idx is not None
            else None
        )
        # ``parse_generic_decision`` sets ``excerpt_start = prompt_top_idx`` when a
        # title was resolved, so the title occupies excerpt line 0; with no title
        # the excerpt begins at the option block and there is no body to walk.
        body_start = 1 if form.current_question_title is not None else 0
        body_end = option_top if option_top is not None else len(excerpt_lines)
        for line in excerpt_lines[body_start:body_end]:
            trimmed = line.rstrip()
            if trimmed:
                body_lines.append(trimmed)
    parts = ["decision:", f"T:{title}"]
    parts.extend(f"B:{b}" for b in body_lines)
    parts.extend(f"O:{o.number}:{o.label}" for o in form.options)
    canonical = "\n".join(parts)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# Wire the strict variant parsers as the gate patterns' S-8 post-validators
# (the parsers are defined here, far below ``UI_PATTERNS``; ``UIPattern`` is
# frozen, so rebuild the validated entries in place via ``replace``). After
# this, ``extract_interactive_content`` runs ``parse_permission_prompt`` /
# ``parse_workflow_approval`` / ``parse_generic_decision`` after a loose match
# and only returns the pattern when it strictly parses — closing the
# quoted-prompt false positives (S-8). The ordering (gates + Decision LAST) is
# preserved (same list positions).
UI_PATTERNS = [
    replace(p, validator=parse_permission_prompt)
    if p.name == "Permission"
    else replace(p, validator=parse_workflow_approval)
    if p.name == "Workflow"
    else replace(p, validator=parse_generic_decision)
    if p.name == "Decision"
    else p
    for p in UI_PATTERNS
]


def _infer_current_tab_idx(
    questions: tuple[AskQuestion, ...],
    pane_form: AskUserQuestionForm | None,
) -> tuple[int, bool]:
    """Match pane-visible content against the JSONL questions matrix.

    Returns ``(idx, inferred)``. ``inferred`` is True when at least one
    matcher pinned a single tab; False when every matcher tied or no signal
    was available, in which case ``idx`` defaults to 0 and the caller must
    suppress option-pick buttons (FA5+ safety: dispatching a digit against a
    defaulted tab can answer the wrong tab in the live TUI).

    Match order:
      1. Primary — exact title match (pane's ``current_question_title`` ==
         a question's ``title`` OR ``header``). Falls through on ambiguity
         (two questions share the same title) or on truncated/wrapped pane
         titles.
      2. Secondary — option-label overlap. Score each question by how many
         of its option labels appear in the pane form's options. Unique
         winner wins; tie → fall through.
      3. Fallback — return ``(0, False)``.
    """
    if pane_form is None or not questions:
        return 0, False

    # Primary: exact title match.
    pane_title = (pane_form.current_question_title or "").strip()
    if pane_title:
        title_matches: list[int] = []
        for i, q in enumerate(questions):
            if pane_title == q.title.strip() or pane_title == q.header.strip():
                title_matches.append(i)
        if len(title_matches) == 1:
            return title_matches[0], True

    # Secondary: option-label overlap. The pane carries the visible labels
    # for the current tab only; whichever question has the most labels in
    # the pane form's options is the active one.
    pane_labels = {o.label for o in pane_form.options if o.label}
    if pane_labels:
        scored: list[tuple[int, int]] = []
        for i, q in enumerate(questions):
            q_labels = {o.label for o in q.options if o.label}
            scored.append((i, len(pane_labels & q_labels)))
        # Drop zero scores so a pane with no overlap with any question
        # doesn't accidentally pick idx 0 as the "winner".
        scored = [(i, s) for (i, s) in scored if s > 0]
        if scored:
            scored.sort(key=lambda pair: pair[1], reverse=True)
            top_score = scored[0][1]
            top = [i for (i, s) in scored if s == top_score]
            if len(top) == 1:
                return top[0], True

    return 0, False


def resolve_ask_form(
    tool_input: dict[str, Any] | None,
    pane_text: str,
) -> AskUserQuestionForm | None:
    """Unified AskUserQuestion form resolution.

    Used by both the render path (``handle_interactive_ui``) and the
    pick-token callback validator. Returning byte-identical forms from
    both call sites is what makes the fingerprint staleness check sound
    for multi-tab forms — if render uses the JSONL overlay but validate
    re-parses only the pane, fingerprints will never match on multi-tab.

    Inputs:
      * ``tool_input``: JSONL ``tool_use.input`` dict, or None when the
        cache has been evicted (post-restart, post-tool_result).
      * ``pane_text``: live tmux pane capture.

    Output shapes:

    1. Single-question JSONL: returns the legacy single-tab form (same
       canonical_repr as today). Pane is consulted only for cursor /
       free-text / review-screen flags.
    2. Multi-question JSONL + pane parses: ``questions`` matrix populated
       from JSONL; ``current_question_title`` + ``options`` overlay the
       matched tab; ``current_tab_inferred`` reflects whether matching
       succeeded.
    3. Multi-question JSONL + pane fails: ``current_tab_idx = 0`` and
       ``current_tab_inferred = False`` — the renderer MUST NOT mint pick
       buttons under this state.
    4. JSONL missing: fall back to ``parse_ask_user_question(pane_text)``
       — preserves the pane-only path for sessions where the JSONL cache
       was lost.
    5. Both missing: returns None.
    """
    pane_form = parse_ask_user_question(pane_text) if pane_text else None

    jsonl_form = build_form_from_tool_input(tool_input)
    if jsonl_form is None:
        # No JSONL — pure pane fallback.
        return pane_form

    # JSONL-stale detection. Claude buffers an assistant turn before
    # writing it to JSONL, so a fresh AskUserQuestion tool_use can be
    # live on the pane while ``tool_input`` still points at the
    # *previous* AUQ. The render then overlays a pane that doesn't
    # reconcile with the cached questions:
    #
    #   * single-q stale → wrong-action class: pick buttons render
    #     JSONL labels but a click dispatches the digit against the
    #     pane's different question (e.g. clicking "1. Old answer A"
    #     submits "Option 1 of the new question").
    #   * multi-q stale → FA5+ guard suppresses pick buttons (correct
    #     defense, but the user is stuck with no working surface).
    #
    # Detection: pane has non-empty options AND no JSONL question
    # ``_strong_match``-es the pane. Skip on review screens (pane is
    # already authoritative there and the existing branches preserve
    # the JSONL questions matrix for tab-strip context). Falling back
    # to ``pane_form`` gives the renderer a clean single-tab shape
    # whose option labels match the live pane — pick buttons dispatch
    # against the right question, and the cursor overlay works.
    if (
        pane_form is not None
        and not pane_form.is_review_screen
        and pane_form.options
        and not any(_strong_match(q, pane_form) for q in jsonl_form.questions)
    ):
        logger.info(
            "resolve_ask_form JSONL STALE: pane has %d options that don't "
            "match any of %d JSONL questions; falling back to pane-only. "
            "pane_title=%r jsonl_titles=%r",
            len(pane_form.options),
            len(jsonl_form.questions),
            (pane_form.current_question_title or "<none>")[:80],
            [q.title[:80] for q in jsonl_form.questions],
        )
        # Tag the form so the renderer can distinguish "pane-only
        # because no JSONL was ever cached" (cache_empty) from
        # "pane-only because the JSONL cache held a DIFFERENT question"
        # (cache stale). The contiguous-from-1 mint gate downstream
        # protects both cases when the pane shows only a tail of the
        # option list, but the tag remains useful for diagnostic logs
        # and the callback-rerender notice path. ``_meta`` is
        # ``compare=False`` and excluded from ``_canonical_repr`` /
        # ``fingerprint``, so this tag doesn't invalidate live pick-token
        # callbacks minted against earlier renders.
        pane_form._meta["stale_fallback"] = "1"
        return pane_form

    if len(jsonl_form.questions) <= 1:
        # Single-question review screen: pane is authoritative, same as the
        # multi-question short-circuit below. Claude Code's single-question
        # AUQ TUI has two steps — picker then Submit/Cancel confirmation;
        # the picker's JSONL options are the original answers, but the
        # confirmation step's pane shows ``1. Submit answers`` /
        # ``2. Cancel``. Without this branch, the single-question resolver
        # always returned the original answer options grafted onto
        # ``is_review_screen=True``, producing a mislabelled card AND a
        # wrong-action-class bug: clicking the rendered "option 2" would
        # dispatch ``2 + Enter`` against the live Submit/Cancel picker
        # (Cancel) while the button reads as one of the original answers.
        # ``current_question_title`` stays from JSONL so single-question
        # review fingerprints don't collapse onto a single canonical repr
        # (``_canonical_repr`` omits QS:/INF: for len(questions) <= 1, so
        # the title is the only remaining identity carrier here).
        if pane_form is not None and pane_form.is_review_screen:
            return AskUserQuestionForm(
                tabs=pane_form.tabs,
                current_question_title=jsonl_form.current_question_title,
                options=pane_form.options,
                is_review_screen=True,
                is_free_text=pane_form.is_free_text,
                pane_excerpt=pane_form.pane_excerpt,
                questions=jsonl_form.questions,
                current_tab_inferred=False,
                select_mode="single",
                options_complete=True,
            )
        # Single-question: keep the JSONL-derived shape but graft live pane
        # state (cursor on the right option, free-text / review-screen
        # flags). Without the pane overlay the form would always claim
        # cursor on option 1, breaking the existing single-tab behaviour.
        if pane_form is not None:
            return AskUserQuestionForm(
                tabs=jsonl_form.tabs,
                current_question_title=jsonl_form.current_question_title,
                options=_overlay_cursor_and_selection(
                    jsonl_form.options, pane_form.options
                ),
                is_review_screen=pane_form.is_review_screen,
                is_free_text=pane_form.is_free_text,
                pane_excerpt=pane_form.pane_excerpt,
                questions=jsonl_form.questions,
                current_tab_inferred=True,
                select_mode=_jsonl_resolved_select_mode(jsonl_form, pane_form),
                options_complete=True,
            )
        return jsonl_form

    # Multi-question: detect review screen FIRST. On a review screen, the
    # pane's visible options are Submit/Cancel — not Q1's options — and
    # overlaying them onto Q1's labels mints buttons whose label disagrees
    # with the action that the cursor will dispatch (wrong-action class).
    # Pane is authoritative for the review screen's options + cursor; the
    # JSONL `questions` matrix stays for tab-strip context only.
    if pane_form is not None and pane_form.is_review_screen:
        return AskUserQuestionForm(
            tabs=pane_form.tabs,
            current_question_title=None,
            options=pane_form.options,
            is_review_screen=True,
            is_free_text=pane_form.is_free_text,
            pane_excerpt=pane_form.pane_excerpt,
            questions=jsonl_form.questions,
            # No inference happened — the pane authoritatively says "review".
            # The mint gate has a review-screen EXCEPTION: it still mints the
            # Submit/Cancel pick buttons from the pane's own options (these are
            # the real review-screen labels, not JSONL Q-labels), so the user
            # can submit / cancel via the Telegram keyboard as well as keystroke
            # nav. `current_tab_inferred=False` only marks that no tab inference
            # ran here.
            current_tab_inferred=False,
            select_mode="single",
            options_complete=True,
        )

    # Multi-question: infer the current tab from pane content.
    current_idx, inferred = _infer_current_tab_idx(jsonl_form.questions, pane_form)
    # Strong-match requirement before overlay: even if _infer_current_tab_idx
    # returned (idx, True) on a single matching option, demote to inferred=False
    # unless we have a non-trivial title substring match OR ≥50% option-label
    # overlap. This prevents minting Q1's buttons when the pane is actually
    # showing Q2 with one coincidentally-shared option label.
    if inferred and pane_form is not None:
        if not _strong_match(jsonl_form.questions[current_idx], pane_form):
            inferred = False
    current_q = jsonl_form.questions[current_idx]
    # Overlay the live cursor onto the chosen tab's options only when the
    # match is strong. On weak/no inference we keep JSONL options as-is so
    # the validator and renderer see a stable shape; pick buttons are
    # suppressed downstream because current_tab_inferred is False.
    options = (
        _overlay_cursor_and_selection(current_q.options, pane_form.options)
        if pane_form is not None and inferred
        else current_q.options
    )
    # Diagnostic: when inference fails on a multi-question form, the FA5+
    # guard in ``_build_pick_button_rows`` suppresses pick buttons and the
    # user is left with keystroke nav only. Log the inputs so future repros
    # tell us whether (a) pane_form was None, (b) options weren't extracted,
    # (c) title didn't match, or (d) strong-match demoted. Only log on the
    # failure path to keep noise low; the success path is the common case.
    if not inferred:
        pane_title = (
            (pane_form.current_question_title or "<none>") if pane_form else "<no pane>"
        )
        pane_opts = len(pane_form.options) if pane_form else -1
        jsonl_titles = [q.title for q in jsonl_form.questions]
        logger.info(
            "resolve_ask_form multi-q inference FAILED: questions=%d pane_opts=%d "
            "pane_title=%r jsonl_titles=%r",
            len(jsonl_form.questions),
            pane_opts,
            pane_title[:80] if isinstance(pane_title, str) else pane_title,
            [t[:80] for t in jsonl_titles],
        )
    return AskUserQuestionForm(
        tabs=pane_form.tabs if pane_form is not None else (),
        current_question_title=current_q.title or None,
        options=options,
        is_review_screen=pane_form.is_review_screen if pane_form is not None else False,
        is_free_text=pane_form.is_free_text if pane_form is not None else False,
        pane_excerpt=pane_form.pane_excerpt if pane_form is not None else "",
        questions=jsonl_form.questions,
        current_tab_inferred=inferred,
        select_mode=_jsonl_resolved_select_mode(jsonl_form, pane_form),
        options_complete=True,
    )


def _strong_match(q: AskQuestion, pane_form: AskUserQuestionForm) -> bool:
    """Stricter inference check than ``_infer_current_tab_idx``.

    The inference helper accepts any unique winner, including the degenerate
    case "one label happened to match." That can mint Q1's buttons against
    a pane showing Q2 if Q1 and Q2 share one option label. Require:

      * the question title is a non-trivial substring of the pane title
        (or vice versa) — case-insensitive, ≥8 chars or full title length, OR
      * ≥50% of the pane's option labels appear in the question's options.

    Reject if neither holds. Caller demotes ``inferred`` to False; the mint
    code then suppresses pick buttons and keystroke nav stays available.
    """
    q_title = q.title.strip().lower()
    pane_title = (pane_form.current_question_title or "").strip().lower()
    if q_title and pane_title:
        # Substring match in either direction; reject trivially-short overlaps
        # (e.g., "Pick." in any pane title would otherwise pass).
        shorter = min(q_title, pane_title, key=len)
        threshold = min(8, len(shorter))
        if threshold > 0 and (
            (q_title in pane_title or pane_title in q_title)
            and len(shorter) >= threshold
        ):
            return True

    pane_labels = {o.label for o in pane_form.options if o.label}
    if not pane_labels:
        return False
    q_labels = {o.label for o in q.options if o.label}
    overlap = len(pane_labels & q_labels)
    # ≥50% of pane labels recognized in this question's option set.
    return overlap * 2 >= len(pane_labels)


def _overlay_cursor_and_selection(
    jsonl_options: tuple[AskOption, ...],
    pane_options: tuple[AskOption, ...],
) -> tuple[AskOption, ...]:
    """Apply pane cursor and visible checkbox selection to JSONL options by number.

    Cursor follows the existing overlay rule: if no cursor is visible, default
    to option 1. Selection is stricter: only visible pane rows are known; JSONL
    options not present in the pane get ``selected=None`` rather than False.
    """
    cursor_at: int | None = None
    selected_by_num: dict[int, bool | None] = {}
    for opt in pane_options:
        if opt.number is None:
            continue
        selected_by_num[opt.number] = opt.selected
        # Prefer the LAST cursor flag in pane order. ``_parse_numbered_options``
        # already dedups to a single (bottom-most) cursor, but a raw or future
        # pane_options tuple could still carry a stale-scrollback ``❯`` above
        # the live one — the live cursor is always the bottom-most. Overwrite
        # rather than first-wins so the overlay tracks the live cursor.
        if opt.cursor:
            cursor_at = opt.number
    if cursor_at is None and jsonl_options:
        cursor_at = jsonl_options[0].number
    if cursor_at is None:
        return jsonl_options
    return tuple(
        AskOption(
            label=o.label,
            recommended=o.recommended,
            cursor=(o.number == cursor_at),
            number=o.number,
            description=o.description,
            selected=selected_by_num.get(o.number) if o.number is not None else None,
        )
        for o in jsonl_options
    )


def _jsonl_resolved_select_mode(
    jsonl_form: AskUserQuestionForm,
    pane_form: AskUserQuestionForm | None,
) -> Literal["single", "multi", "unknown"]:
    """Resolve select mode when a JSONL/side-file source is present."""
    if pane_form is None:
        return jsonl_form.select_mode
    source_mode: Literal["single", "multi", "unknown"] | None = jsonl_form.select_mode
    if jsonl_form._meta.get("multiselect_present") == "0":
        source_mode = None
    return _resolve_select_mode(
        source_mode,
        pane_form.select_mode,
        is_review_screen=pane_form.is_review_screen,
    )


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])

# Leading characters of the spinner's attached task-progress block — the
# ``⎿ ✔ …`` / ``✔ …`` / ``◼ …`` todo lines Claude Code (v2.1.168) renders
# BETWEEN the spinner line and the chrome separator while a run is in
# flight. ``parse_status_line`` skips these (like blanks) when walking up
# from the separator; treating them as "not a spinner → no status" read an
# ACTIVE pane as idle and let the pane-idle clear falsely commit mid-run
# (2026-06-11 @4 stuck-route incident).
_TASK_PROGRESS_PREFIXES = ("⎿", "✔", "✗", "◼", "☐", "□", "■")

# How many lines the chrome-anchor scan covers from the bottom of the
# capture. Sized for the v2.1.168 agent task-list footer, which renders
# BELOW the ``⏵⏵ … esc to interrupt`` chrome line (one row per agent) and
# would push the separator out of the previous 10-line window on busy
# multi-agent runs. Captures here are visible-only (no scrollback), so the
# wider window stays bottom-anchored.
_CHROME_SCAN_LINES = 20


def _find_chrome_separator(lines: list[str]) -> int | None:
    """Locate the topmost ``──`` chrome separator in the bottom scan window."""
    search_start = max(0, len(lines) - _CHROME_SCAN_LINES)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return i
    return None


def has_pane_chrome(pane_text: str) -> bool:
    """Return True iff the frame contains Claude Code's bottom-chrome anchor.

    The anchor is the chrome separator — a full line of ``─`` (≥20 chars) in
    the last ``_CHROME_SCAN_LINES`` (20) lines — the SAME structural anchor
    ``parse_status_line`` and
    ``strip_pane_chrome`` already trust to locate the bottom chrome. Its
    presence is positive evidence the capture is a fully-rendered live
    Claude Code pane (not an empty/truncated/mid-redraw frame). Used by
    ``status_polling._process_idle_clear_only`` as the positive half of its
    "confirmed idle" predicate (chrome present AND not ``is_status_active``).
    """
    if not pane_text:
        return False
    return _find_chrome_separator(pane_text.split("\n")) is not None


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears above the chrome
    separator (a full line of ``─`` characters). We locate the separator
    first, then check the lines just above it — this avoids false
    positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    Note: blank lines AND the spinner's attached task-progress block
    (``⎿ ✔ …`` / ``◼ …`` todo lines — rendered between the spinner and the
    separator while a run is in flight on v2.1.168) are tolerated here
    (the latter was the 2026-06-11 stuck-route incident: returning None on
    an active pane fed ``is_running=False`` into the pane-idle clear). To
    distinguish "Claude is actively running" from "post-completion
    summary", use ``is_status_active`` instead.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")
    chrome_idx = _find_chrome_separator(lines)
    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Walk up from the separator, skipping blanks and task-progress lines
    # (bounded — the spinner sits at most a small block above the chrome).
    for i in range(chrome_idx - 1, max(chrome_idx - 16, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        if line.startswith(_TASK_PROGRESS_PREFIXES):
            continue
        # First other non-empty line above the separator → no status
        return None
    return None


_RE_BG_SHELLS_BAR = re.compile(r"(?:^|·\s)(\d+)\s+shells?(?=\s*·|\s*$)")
_RE_BG_SHELLS_CHURN = re.compile(r"·\s*(\d+)\s+shells?\s+still\s+running\b")


def parse_background_jobs(pane_text: str) -> int | None:
    """Extract Claude Code's background-shell count from a pane frame (GH #43).

    A turn that ends with a backgrounded shell still executing shows the
    count in two chrome-region places (v2.1.168, real-fixture verified):

      - the status bar below the bottom separator:
        ``⏵⏵ bypass permissions on · 1 shell · ← for agents · ↓ to manage``
      - the churn/spinner line above the top separator:
        ``✻ Brewed for 6s · 1 shell still running``

    The scan is anchored to the CHROME REGION ONLY (never body prose — a
    ``· 3 shells ·`` string in Claude's output must not count): the status
    bar is the first ``⏵`` line below the LAST separator; the churn line is
    the spinner line found by the same bounded walk-up
    ``parse_status_line`` uses. The status bar is the primary anchor (the
    2026-06-11 incident frame defeated ``parse_status_line`` via the
    task-progress overlay while its status bar still showed ``· 1 shell``);
    on conflicting tokens the MAX wins.

    Returns ``None`` when no chrome separator is visible (untrusted /
    truncated frame — callers must not record), and ``0`` when the chrome
    is present but neither token is (positively no background shells).
    NOTE: a mid-run frame may read 0 even with live shells (the running
    status bar truncates and the active spinner carries no token) — callers
    only RENDER the count on idle routes, where the idle frame restores it
    within one capture watchdog interval.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")
    if _find_chrome_separator(lines) is None:
        return None

    # All separator lines in the bottom scan window. The input box renders
    # as a PAIR (top + bottom separator); anchoring the churn scan on the
    # second-to-last separator — not the topmost — keeps a quoted ``────``
    # inside body output from hijacking the anchor (hermes GH #43 diff P3).
    search_start = max(0, len(lines) - _CHROME_SCAN_LINES)
    sep_idxs = [
        i
        for i in range(search_start, len(lines))
        if len(lines[i].strip()) >= 20 and all(c == "─" for c in lines[i].strip())
    ]

    counts: list[int] = []

    # Status bar: first ⏵ line below the LAST separator in the frame.
    last_sep = sep_idxs[-1]
    for i in range(last_sep + 1, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        if line.startswith("⏵"):
            m = _RE_BG_SHELLS_BAR.search(line)
            if m:
                counts.append(int(m.group(1)))
            break

    # Churn line: the spinner line above the input box's TOP separator
    # (the second-to-last separator when the pair is visible), same bounded
    # walk-up as parse_status_line — blanks and task-progress lines skipped.
    churn_anchor = sep_idxs[-2] if len(sep_idxs) >= 2 else sep_idxs[-1]
    for i in range(churn_anchor - 1, max(churn_anchor - 16, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            m = _RE_BG_SHELLS_CHURN.search(line)
            if m:
                counts.append(int(m.group(1)))
            break
        if line.startswith(_TASK_PROGRESS_PREFIXES):
            continue
        break

    return max(counts) if counts else 0


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

    # Anchor on the chrome separator when present: the ``⏵⏵ …`` chrome bar
    # carrying the marker sits a fixed few lines below it, but the v2.1.168
    # agent task-list footer renders BELOW the chrome bar (one row per
    # agent), so a fixed bottom window can push the marker out of scope on
    # busy multi-agent runs and read an ACTIVE pane as idle (the 2026-06-11
    # @4 false pane-idle clear class). Fall back to the last 8 lines when
    # no separator is found (e.g. heavily truncated frames).
    lines = pane_text.split("\n")
    chrome_idx = _find_chrome_separator(lines)
    scan = lines[chrome_idx:] if chrome_idx is not None else lines[-8:]
    return any("esc to interrupt" in line.lower() for line in scan)


# The bottom-chrome ``──`` rule separator (≥20 dashes) — the SAME anchor
# ``_find_chrome_separator`` trusts. Claude Code brackets its input box with a
# PAIR of these (top + bottom); the input row lives strictly between them.
_RE_RULE_SEPARATOR = re.compile(r"^─{20,}$")

# Positive ready-for-input status-bar markers Claude Code renders BELOW the
# input box when it is NOT running (the mode indicator, the shortcuts hint, the
# agents/manage bar, the effort indicator). Their PRESENCE is positive proof the
# frame is a fully-rendered idle status bar — a mid-redraw capture that dropped
# the footer has NONE of them and therefore fails closed. (``is_status_active``
# separately rejects the ACTIVE bar, which carries ``esc to interrupt`` on this
# same line.)
_READY_STATUS_MARKERS = (
    "? for shortcuts",
    "shift+tab to cycle",
    "← for agents",
    "↓ to manage",
    "/effort",
    "bypass permissions on",
    "accept edits on",
    "plan mode on",
)


def _is_rule_separator(line: str) -> bool:
    """True iff ``line`` is a full ``──`` chrome rule separator (≥20 dashes)."""
    return bool(_RE_RULE_SEPARATOR.match(line.strip()))


def pane_looks_idle(visible_pane: str | None) -> bool:
    """Ground-truth cross-check that a pane is idle at an EMPTY input box.

    The ``/update`` command's REQUIRED second gate beside
    ``route_runtime.snapshot(route).run_state == IDLE_CLEARED`` — the run-state
    machine can LAG a pane that just started a new generation, so a pane read is
    the authoritative "not mid-work" proof before a restart quits Claude.

    STRUCTURAL + POSITIVE-EVIDENCE + FAIL-CLOSED. Returns True ONLY when ALL
    hold:

      1. No active-run signal (``is_status_active`` — the reliable
         ``esc to interrupt`` scan over the whole bottom chrome region).
      2. No live interactive surface (``is_interactive_ui`` — AUQ / ExitPlanMode
         / Permission / Workflow / Settings).
      3. The BOTTOM pair of ``──`` rule separators brackets an input row that is
         the EMPTY ``❯`` prompt (only whitespace after the cursor glyph). A body
         Markdown ``> blockquote`` line sits ABOVE this pair, so it can NEVER
         satisfy the proof; a typed-but-unsent draft (``❯ some text``) is NOT
         idle (a restart would discard it).
      4. POSITIVE ready-for-input status-bar chrome is present below the box (a
         ``_READY_STATUS_MARKERS`` hit) — so a mid-redraw capture that dropped
         the footer, or any frame without the rendered idle status bar, fails
         closed rather than being read as idle on absence alone.
      5. No LIVE background shells: ``parse_background_jobs`` (the GH #43
         chrome-anchored ``· N shell`` token scan) reads a count ≥ 1 → not
         restart-safe (``/exit`` would silently kill the user's backgrounded
         jobs). ``None`` (no chrome parse) and ``0`` (chrome present, no token)
         do NOT block — the frame already passed the positive ready-chrome
         proof above, and refusing on an unknown count would make ``/update``
         defer every restart.

    Anything else returns False so ``/update`` DEFERS the window rather than risk
    ``/exit``-ing into live work.
    """
    if not visible_pane:
        return False
    lines = visible_pane.split("\n")
    # (1) Active generation → not idle.
    if is_status_active(visible_pane):
        return False
    # (2) A live interactive surface is "waiting on the user", not idle.
    if is_interactive_ui(visible_pane):
        return False
    # (3) Locate the input box STRUCTURALLY: the bottom pair of rule separators.
    search_start = max(0, len(lines) - _CHROME_SCAN_LINES)
    sep_idxs = [
        i for i in range(search_start, len(lines)) if _is_rule_separator(lines[i])
    ]
    if len(sep_idxs) < 2:
        return False  # no rendered input-box bracket → fail closed
    top, bottom = sep_idxs[-2], sep_idxs[-1]
    prompt_seen = False
    for i in range(top + 1, bottom):
        s = lines[i].strip()
        if not s:
            continue
        # Only the ``❯`` prompt cursor may sit inside the box.
        if s[0] not in ("❯", "›", ">"):
            return False
        # The input row must be EMPTY — a typed draft is not restart-safe.
        if s[1:].strip():
            return False
        prompt_seen = True
    if not prompt_seen:
        return False
    # (4) POSITIVE ready-for-input chrome below the box (idle status bar).
    below = "\n".join(lines[bottom + 1 :])
    if not any(marker in below for marker in _READY_STATUS_MARKERS):
        return False
    # (5) Live background shells (GH #43 `· N shell` chrome token) → a restart
    # would silently kill them. None/0 never block (see the docstring).
    jobs = parse_background_jobs(visible_pane)
    if jobs is not None and jobs >= 1:
        return False
    return True


# The COMPLETE set of leg names ``classify_pane_idle_failure`` can return
# (non-None). The /cost fallback copy map is exhaustiveness-tested against this
# set — adding a new leg name to the classifier without mapped action copy in
# ``bot._USAGE_FALLBACK_ACTION`` (directly or via the bot's indeterminate
# normalization) fails that test.
PANE_IDLE_FAILURE_REASONS = frozenset(
    {
        "capture_empty",
        "active_status",
        "interactive",
        "no_input_box",
        "input_not_empty",
        "no_ready_chrome",
        "background_shells",
    }
)


def classify_pane_idle_failure(visible_pane: str | None) -> str | None:
    """Name the FIRST ``pane_looks_idle`` leg that a non-idle pane fails.

    Diagnostic-only, REPLAY-only, NEVER authoritative — ``pane_looks_idle`` is
    the decider (the deliberately fail-closed five-gate proof ``/update`` and the
    ``/cost`` overlay interceptor rely on). This helper walks the SAME legs in the
    SAME order purely to LABEL the first failure for logging + reason-specific
    fallback copy. Its body mirrors ``pane_looks_idle`` line-for-line so the
    invariant holds: it returns ``None`` iff ``pane_looks_idle`` returns ``True``
    (pinned by an agreement test across every pane fixture). Never returns pane
    text — only a fixed leg name.

    Reason names:

      - ``"capture_empty"`` — empty / None capture (indeterminate).
      - ``"active_status"`` — leg 1, a live ``esc to interrupt`` run signal.
      - ``"interactive"``  — leg 2, a live AUQ / EPM / gate / Settings surface.
      - ``"no_input_box"`` — leg 3, no rendered input-box separator pair or no
        ``❯`` prompt row (a mid-redraw / no-chrome frame — indeterminate).
      - ``"input_not_empty"`` — leg 3, a non-empty / non-cursor input row (a
        typed-but-unsent draft, or a ``> blockquote`` between separators).
      - ``"no_ready_chrome"`` — leg 4, no ready-for-input status marker below the
        box (a dropped-footer mid-redraw — indeterminate).
      - ``"background_shells"`` — leg 5, a live ``· N shell`` background-jobs
        token.
      - ``None`` — all legs pass (the pane is idle).
    """
    if not visible_pane:
        return "capture_empty"
    lines = visible_pane.split("\n")
    # (1) Active generation.
    if is_status_active(visible_pane):
        return "active_status"
    # (2) Live interactive surface.
    if is_interactive_ui(visible_pane):
        return "interactive"
    # (3) Structural input box: the bottom pair of rule separators.
    search_start = max(0, len(lines) - _CHROME_SCAN_LINES)
    sep_idxs = [
        i for i in range(search_start, len(lines)) if _is_rule_separator(lines[i])
    ]
    if len(sep_idxs) < 2:
        return "no_input_box"
    top, bottom = sep_idxs[-2], sep_idxs[-1]
    prompt_seen = False
    for i in range(top + 1, bottom):
        s = lines[i].strip()
        if not s:
            continue
        if s[0] not in ("❯", "›", ">"):
            return "input_not_empty"
        if s[1:].strip():
            return "input_not_empty"
        prompt_seen = True
    if not prompt_seen:
        return "no_input_box"
    # (4) Ready-for-input chrome below the box.
    below = "\n".join(lines[bottom + 1 :])
    if not any(marker in below for marker in _READY_STATUS_MARKERS):
        return "no_ready_chrome"
    # (5) Live background shells.
    jobs = parse_background_jobs(visible_pane)
    if jobs is not None and jobs >= 1:
        return "background_shells"
    return None


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


# The /usage (== /cost) modal tab bar, verbatim on Claude Code 2.1.206:
#     "   Settings  Status   Config   Usage   Stats"
# This same bar renders for BOTH /cost and /usage (fixture-verified — the two
# frames are byte-identical templates). The anchor requires the five ORDERED
# whole tokens separated by whitespace only, with NO other word characters on
# the line (leading/trailing non-word chrome — spaces, box glyphs, a scroll
# indicator — is tolerated). Unordered / concatenated / prose-embedded probes
# ("Stats Usage Config Status Settings", "SettingsStatusConfigUsageStats",
# a sentence containing the five words) must NOT match (round-1 converged P3).
_RE_USAGE_TAB_BAR = re.compile(
    r"^[^\w]*Settings\s+Status\s+Config\s+Usage\s+Stats[^\w]*$"
)

# A full-width box-drawing rule (the modal body's top rule / a bare separator)
# — one of the structural-evidence anchors below.
_RE_USAGE_RULE_LINE = re.compile(r"^[▔▁─]{20,}$")


def _usage_overlay_anchor(lines: list[str]) -> int | None:
    """Return the index of the /usage modal's tab-bar line, or None.

    A matching tab-bar line alone is NOT enough (arbitrary pane prose could
    reproduce it — the round-1 P3): the match must be corroborated by
    STRUCTURAL overlay evidence — any of (fixture-supported on 2.1.206):

    - the full-width box-drawing rule that opens the modal body, within the 3
      non-blank lines ABOVE the tab bar (present in every capture, including
      the scrolled day/week toggles);
    - the overlay's own ``Esc to …`` footer anywhere BELOW it;
    - the ``Session`` sub-header within the 6 lines BELOW it (the unscrolled
      top-of-modal shape).
    """
    for i, line in enumerate(lines):
        if not _RE_USAGE_TAB_BAR.match(line.strip()):
            continue
        # (a) the modal's top rule above the tab bar (skip blank lines).
        seen = 0
        for j in range(i - 1, -1, -1):
            stripped_above = lines[j].strip()
            if not stripped_above:
                continue
            if _RE_USAGE_RULE_LINE.match(stripped_above):
                return i
            seen += 1
            if seen >= 3:
                break
        # (b) the footer below / (c) the Session sub-header just below.
        for k in range(i + 1, len(lines)):
            stripped_below = lines[k].strip()
            if stripped_below.startswith("Esc to"):
                return i
            if k <= i + 6 and stripped_below == "Session":
                return i
        # A tab-bar-shaped line without structural evidence — keep scanning.
    return None


def usage_overlay_present(pane_text: str | None) -> bool:
    """True when the captured pane shows the live /usage (== /cost) modal.

    The conditional-dismiss gate for ``bot._run_usage_overlay``: Escape is sent
    ONLY when this returns True — an Escape into a pane where the overlay never
    opened would interrupt an active generation (the /esc hazard; round-1 P1).
    """
    if not pane_text:
        return False
    return _usage_overlay_anchor(pane_text.strip().split("\n")) is not None


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage (== /cost) overlay.

    The /cost and /usage commands open the SAME full-screen modal (fixture
    verified on 2.1.206): a tab bar ``Settings  Status  Config  Usage  Stats``
    at the top of the modal body and an ``Esc to cancel`` footer at the bottom
    of the scrollable body. The parser anchors on that stable chrome — the
    ORDERED whole-token tab bar corroborated by structural overlay evidence
    (``_usage_overlay_anchor``) — takes everything AFTER the tab-bar line and
    BEFORE ``Esc to cancel`` (or the end of the captured pane if the footer
    scrolled off), strips the box-drawing rule + progress-bar block characters,
    and returns the readable body lines.

    Tolerant by design: the goal is a readable Telegram message, not a lossless
    model. Version drift that moves the tab bar / footer → ``None`` → the
    command's fail-open raw-pane fallback (``bot.usage_command``).

    Returns UsageInfo with cleaned lines, or None if the modal isn't detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # The validated tab-bar anchor marks the top of the modal body. Anything
    # above it (a welcome card / limit banner left in scrollback above the
    # overlay — see the usage_overlay fixture) is ignored.
    anchor = _usage_overlay_anchor(lines)
    if anchor is None:
        return None
    start_idx = anchor + 1  # skip the tab-bar header itself
    end_idx: int | None = None

    for i in range(start_idx, len(lines)):
        # The overlay's own footer bottom-anchors the body.
        if lines[i].strip().startswith("Esc to"):
            end_idx = i
            break

    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping the top box-drawing rule, progress-bar
    # block characters, and scroll indicators the overlay paints at the margin.
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        stripped = line.strip()
        if not stripped:
            continue
        # Drop a full-width box-drawing rule (the rule that opens the modal
        # body, or a bare separator).
        if all(c in "▔▁─" for c in stripped):
            continue
        # Strip a trailing scroll indicator (up/down arrow at the right margin).
        stripped = stripped.rstrip("↑↓ ").rstrip()
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
