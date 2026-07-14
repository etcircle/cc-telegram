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
``CC_TELEGRAM_PERMISSION_PROMPTS`` flag (default ON since 2026-07-11; explicit
falsy value disables). It was removed in
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

A SECOND, independent flag ``CC_TELEGRAM_DECISION_CARDS`` (default ON since
2026-07-11; explicit falsy value disables) gates a last-priority generic
``Decision`` pattern (Stage B1) that surfaces titled numbered-option
confirmation prompts no NAMED pattern covers (the "Switch model?" /
folder-trust family) as a display-only card. It is strict-or-None with a
Permission/Workflow veto so it never shadows a named pattern or re-surfaces a
flag-OFF gate.

Both flags are LOCAL ``os.getenv`` reads (``_PERMISSION_PROMPTS_ENABLED`` /
``_DECISION_CARDS_ENABLED``, re-readable via ``reset_for_tests`` /
``set_permission_prompts_enabled`` / ``set_decision_cards_enabled``) — this
module is a pure stdlib leaf and MUST NOT import ``config`` (it raises without a
bot token, which would force a token into parser unit tests). The bot's
``config.py`` owns the canonical ``CC_TELEGRAM_PERMISSION_PROMPTS`` /
``CC_TELEGRAM_DECISION_CARDS`` declarations for documentation; the parser just
reads the same env vars.

A GH #47-R1 helper ``parse_unknown_blocking_prompt`` (pure, replay-only, NEVER
authorizes a keystroke) recognizes an UNRECOGNIZED bottom-most blocking
numbered-option prompt (no named UI owns the pane) so the poller's absent-streak
clear can render an honest text-only excerpt card instead of a misleading
"resolved" tombstone.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output(),
parse_permission_prompt(), parse_workflow_approval(), parse_generic_decision(),
parse_unknown_blocking_prompt().
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
    """Truthiness of the permission flag (default ON since 2026-07-11; falsy disables)."""
    return os.getenv("CC_TELEGRAM_PERMISSION_PROMPTS", "true").strip().lower() in (
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
# family) that no NAMED pattern covers as a DISPLAY-ONLY card. Default ON since
# 2026-07-11; an explicit falsy value disables it. A flag-OFF deploy adds ZERO
# new detection (``_active_ui_patterns`` drops it).
# ``config.py`` owns the canonical ``CC_TELEGRAM_DECISION_CARDS`` declaration for
# docs / the README sync rule; the parser reads the same env var locally so it
# stays a config-free stdlib leaf.


def _read_decision_cards_env() -> bool:
    """Truthiness of Decision cards (default ON since 2026-07-11; falsy disables)."""
    return os.getenv("CC_TELEGRAM_DECISION_CARDS", "true").strip().lower() in (
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

    When ``CC_TELEGRAM_PERMISSION_PROMPTS`` is OFF the ``Permission``
    / ``Workflow`` patterns are excluded; when ``CC_TELEGRAM_DECISION_CARDS`` is
    OFF the ``Decision`` pattern is excluded. Each flag is
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

# How far BELOW the live footer line its wrapped continuation may land. tmux
# wraps a long line onto the IMMEDIATELY following row(s), so 2 is generous.
_EPM_FOOTER_WRAP_LINES: Final = 2


def extract_epm_plan_file_path(pane_text: str) -> str | None:
    """The ``~/.claude/plans/<slug>.md`` path of the **LIVE** ExitPlanMode footer.

    STRICTLY SCOPED TO THAT FOOTER (GH #50 PR-2 peer-review round-2 P1). The
    earlier version fell back to "the LAST plan path anywhere in the pane"
    whenever no footer line carried one — so a pane with NO live footer at all,
    but a stale ``~/.claude/plans/…`` mention in scrollback (an earlier prompt, a
    quoted transcript, the bot's own posted plan), returned that UNRELATED path.
    ``interactive_ui._maybe_post_epm_plan`` — the one caller — would then have
    posted the WRONG plan body above the picker.

    So: find the BOTTOM-MOST ``ctrl[+-]g to edit`` line (a TUI renders the live
    prompt at the bottom; everything above is frozen scrollback). No footer ⇒
    ``None``, never a scrollback path. If that footer line carries the path,
    return it. Otherwise consult only its WRAPPED CONTINUATION — the next
    ``_EPM_FOOTER_WRAP_LINES`` rows, where tmux puts the overflow of a long
    footer. Nothing there ⇒ ``None`` (fail closed), never a path from above.
    """
    lines = pane_text.split("\n")
    footer_idx: int | None = None
    for i, line in enumerate(lines):
        if _RE_EPM_FOOTER.search(line):
            footer_idx = i  # bottom-most is the LIVE one
    if footer_idx is None:
        return None
    m = _RE_EPM_PLAN_PATH.search(lines[footer_idx])
    if m:
        return m.group(1)
    for line in lines[footer_idx + 1 : footer_idx + 1 + _EPM_FOOTER_WRAP_LINES]:
        m = _RE_EPM_PLAN_PATH.search(line)
        if m:
            return m.group(1)
    return None


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


def _walk_back_from_picker_footer(
    lines: list[str], footer_idx: int
) -> tuple[int, int | None, int]:
    """Walk UP from the picker footer to the live option block's top.

    Returns ``(block_top, stop_idx, blank_gap)``:

      * ``block_top``  — the topmost line still belonging to the live option
        block (numbered options, their indented descriptions, the separators and
        blanks between them);
      * ``stop_idx``   — the first NON-block line above it, when it is a
        column-0 candidate for the question text (``None`` when the walk fell off
        the top of the buffer, or the line was indented — Claude Code renders the
        question at column 0, and indented lines above the topmost option are
        invariably scrollback noise);
      * ``blank_gap``  — how many blank lines separate ``stop_idx`` from the
        topmost option (the caller bounds it, so pre-picker scrollback can't be
        pulled in).

    Extracted from :func:`parse_ask_user_question` (its only caller, which owns
    the two display fields it feeds) — one walk, one definition of "the block".
    """
    start_idx = footer_idx
    stop_idx: int | None = None
    blank_gap = 0
    for j in range(footer_idx - 1, -1, -1):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            start_idx = j
            blank_gap += 1
            continue
        if _RE_NUMBERED_OPTION.match(line):
            start_idx = j
            blank_gap = 0
            continue
        # Separator line (only ─ chars).
        if all(c == "─" for c in stripped):
            start_idx = j
            blank_gap = 0
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
            or any(_RE_NUMBERED_OPTION.match(lines[k]) for k in range(max(0, j - 7), j))
        ):
            start_idx = j
            blank_gap = 0
            continue
        # Non-pattern line — title-display candidate. Only set ``stop_idx``
        # here so the for-loop falling off the top of the buffer (no break)
        # keeps it at None: a buffer that is entirely pattern lines has no
        # title to capture. Also reject indented lines as title candidates
        # — Claude Code's question text is rendered at column 0, and indented
        # lines above the topmost option are invariably scrollback noise
        # (hermes review, 2026-05-21).
        if not line.startswith(("  ", "\t")):
            stop_idx = j
        break
    return start_idx, stop_idx, blank_gap


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
        footer_block_top, walkback_stop_idx, walkback_blank_gap = (
            _walk_back_from_picker_footer(lines, footer_idx)
        )

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

    Behind the ``CC_TELEGRAM_DECISION_CARDS`` flag (default ON since
    2026-07-11; explicit falsy value disables) and ordered
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

    ACCEPTED NARROW RESIDUAL (Codex P1 / Hermes P2, display-only): a QUOTED
    decision block that is the LITERAL last content in
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


def parse_unknown_blocking_prompt(pane_text: str) -> str | None:
    """Excerpt of a bottom-most UNRECOGNIZED blocking numbered-option prompt, or
    ``None`` (GH #47-R1 successor-frame fallback).

    PURE + replay-only — it NEVER authorizes a keystroke; it only decides whether
    the poller's absent-streak clear should render an honest TEXT-ONLY excerpt
    card instead of the misleading "🪦 AskUserQuestion resolved…" tombstone. The
    2026-07-09 incident: a tracked interactive card's pane advanced to an
    unrecognized blocking confirm (a footer-less "Switch model?" prompt) and the
    absent-streak tombstone claimed the AUQ had resolved while the prompt still
    blocked the pane.

    POSITIVE + fail-closed — ALL must hold, else ``None``:
      1. ``extract_interactive_content`` returns ``None`` — no NAMED UI
         (AUQ/EPM/Settings/RestoreCheckpoint/Permission/Workflow/Decision) owns
         the pane. Re-checked here so the helper is safe standalone (the
         absent-streak branch already implies it, but the helper never trusts the
         caller);
      2. a bottom-most contiguous numbered-option block (≥2 options) with a
         resolved live ``❯`` cursor (reuses the gate machinery
         ``_gate_options_above`` / ``_decision_option_block_top``);
      3. only gate-chrome below the option block (``_only_chrome_below``) — a
         ready ``❯`` input box, a ``? for shortcuts`` / status bar, a
         ``· N shell`` line, or any trailing assistant prose ⇒ ``None`` (a
         resolved pane / a quoted block in scrollback above a live input box
         never fires).

    Returns the pane excerpt = the contiguous prompt block above the options (a
    bounded walk-up, ``_decision_prompt_block_top``) through the option lines —
    the trusted region only, never the input box / status chrome below.
    """
    if not pane_text:
        return None
    # (1) A named UI owning the pane is handled by its own lane — never here.
    if extract_interactive_content(pane_text) is not None:
        return None
    lines = pane_text.split("\n")

    # (2) Bottom-most numbered-option line with ONLY gate chrome below it. Scan up
    # from the pane bottom; the first numbered option found is the bottom of the
    # block. Everything below it must be chrome (blank / bare separator /
    # ctrl-hint) — a ready input box / status bar / prose below rejects
    # (``_only_chrome_below`` is the shared live-bottom-prompt guard; the option
    # cursor ``❯ 1.`` is ABOVE this index, so any ``❯`` below is the input box).
    bottom_opt_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_NUMBERED_OPTION.match(lines[i]):
            bottom_opt_idx = i
            break
    if bottom_opt_idx is None:
        return None
    if not _only_chrome_below(lines, bottom_opt_idx):
        return None

    # (3) The contiguous option block: ≥2 options + a resolved live cursor.
    options = _gate_options_above(lines, bottom_opt_idx + 1)
    if len(options) < 2:
        return None
    if not any(o.cursor for o in options):
        return None

    # Excerpt = the prompt block above the options (bounded walk-up) → the last
    # option line. The trusted region only (never the chrome below).
    block_top_idx = _decision_option_block_top(lines, bottom_opt_idx + 1)
    excerpt_start = block_top_idx if block_top_idx is not None else bottom_opt_idx
    if block_top_idx is not None:
        prompt_top_idx = _decision_prompt_block_top(lines, block_top_idx)
        if prompt_top_idx is not None:
            excerpt_start = prompt_top_idx
    excerpt = "\n".join(lines[excerpt_start : bottom_opt_idx + 1]).rstrip()
    return excerpt or None


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
#
# The TOP rule may carry a trailing LABEL (GH #50 rig, CC 2.1.207): a few seconds
# after a plan is APPROVED, Claude pins the plan slug into it —
#
#     ───────────────────────────────────────────────── add-ok-to-note ──
#     ❯
#     ────────────────────────────────────────────────────────────────────
#       ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
#
# — and the label PERSISTS for the rest of the session (it survives later turns
# and never decays; only ``/clear`` or a restart drops it). The pure-dash-only
# ``^─{20,}$`` therefore stopped recognizing the input box in EVERY topic where
# the owner approved a plan, which broke:
#   - ``pane_looks_idle`` ⇒ ``/update`` never restarts that session and ``/cost``
#     / ``/usage`` refuse their preflight (a PRE-EXISTING bug, rig-found here);
#   - ``pane_input_box_present`` ⇒ the GH #50 delivery gate would refuse every
#     message in that topic, permanently.
# The label is plan-specific: a fresh session on a git branch renders a CLEAN
# rule (rig ``control_gitrepo_branch_no_label.txt``), so this is not a branch
# indicator. The BOTTOM rule stays pure — only the top one is labeled.
#
# The label segment is bounded to non-``─`` characters and must be re-closed by a
# dash run, so an arbitrary body line can't pose as a rule. Fixture-pinned on
# 2.1.207; a TUI-drift audit surface beside ``clean_ghost_input_text``.
_RE_RULE_SEPARATOR = re.compile(r"^─{20,}(?:\s+[^─\s][^─]*\s+─+)?$")

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


# A CSI (Control Sequence Introducer) escape sequence: ``ESC [ <params> <final>``.
# The final byte is any char in the 0x40-0x7E range; SGR (styling) uses the ``m``
# final. We match the general CSI so a non-SGR sequence (cursor moves, etc.) is
# also consumed and stripped, and isolate SGR params for dim-state tracking.
_RE_CSI = re.compile(r"\x1b\[([0-9;]*)([\x40-\x7e])")
# Any remaining ESC-introduced sequence (non-CSI: OSC, single-char, etc.) — a
# catch-all so ``clean_ghost_input_text`` returns text with EVERY escape removed.
_RE_ANSI_ANY = re.compile(r"\x1b[\[\]][0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# The input-row prompt cursor glyphs ``pane_looks_idle`` accepts (kept in lockstep
# with its ``s[0] not in (...)`` gate). A ghost-suggestion pre-clean only ever
# rewrites a line whose visible content starts with one of these.
_GHOST_PROMPT_GLYPHS: Final = ("❯", "›", ">")


def _strip_ansi(text: str) -> str:
    """Remove ALL ANSI escape sequences, returning plain text.

    Local (leaf-safe) strip — ``screenshot.py`` has a richer ANSI parser but it
    imports PIL, which ``terminal_parser`` must not. Handles CSI sequences (the
    common SGR/styling + cursor family) plus a catch-all for the rarer
    ESC-introduced forms.
    """
    text = _RE_CSI.sub("", text)
    return _RE_ANSI_ANY.sub("", text)


def _sgr_updates_dim(params: str, dim: bool) -> bool:
    """Fold one SGR parameter list into the running dim (SGR-2) state.

    ``ESC[2m`` sets dim; ``ESC[0m`` (reset-all) and ``ESC[22m`` (normal
    intensity) clear it. Colour / other SGR codes (e.g. ``39``, ``38;5;244``)
    do NOT touch dim — a 256-colour selector (``38;5;N`` / ``48;5;N``) or a
    truecolour selector (``38;2;R;G;B``) consumes its trailing sub-parameters
    so a ``2`` inside ``38;5;2`` is never misread as the dim code.
    """
    # Empty param list ``ESC[m`` == ``ESC[0m`` (reset).
    if params == "":
        return False
    parts = params.split(";")
    i = 0
    while i < len(parts):
        code = parts[i]
        if code in ("38", "48"):
            # Extended colour selector — skip its sub-parameters so their
            # digits can never be misread as intensity codes.
            if i + 1 < len(parts) and parts[i + 1] == "5":
                i += 3  # 38;5;N
                continue
            if i + 1 < len(parts) and parts[i + 1] == "2":
                i += 5  # 38;2;R;G;B
                continue
            i += 1
            continue
        if code in ("0", ""):  # reset-all (a bare '' inside a list == 0)
            dim = False
        elif code == "2":
            dim = True
        elif code == "22":  # normal intensity
            dim = False
        i += 1
    return dim


def clean_ghost_input_text(ansi_text: str | None) -> str:
    """Blank a DIM ghost-suggestion in the input row, then strip ANSI.

    Claude Code (CC 2.1.206) renders a contextual GHOST suggestion inside the
    idle input row — e.g. ``❯ ok fix it and let me know when I can test`` —
    styled ENTIRELY DIM (SGR-2, ``ESC[2m…ESC[0m``). tmux plain capture returns
    it as literal text, so ``pane_looks_idle``'s empty-input-row leg reads a
    genuinely idle pane as a typed draft (``input_not_empty``) and ``/cost`` /
    ``/update`` false-refuse. This pre-clean (fed an ANSI capture,
    ``capture_pane(..., with_ansi=True)``) BLANKS the ghost text so the cleaned
    plain text passes ``pane_looks_idle``.

    Contract:
      - Operates ONLY on input-row lines: the visible content (ANSI-stripped)
        starts with a prompt glyph (``❯`` / ``›`` / ``>``). Every other line is
        ANSI-stripped verbatim.
      - The prompt-text region qualifies for blanking ONLY when EVERY visible
        non-whitespace char after the glyph falls inside an ACTIVE dim (SGR-2)
        region. If ANY such char is non-dim — a real typed draft, or a
        dim/normal MIX — the line is left untouched (ANSI-stripped only), so the
        idle gate FAILS CLOSED to today's refusal.
      - Dim state is tracked as a running SGR state across the line
        (``ESC[2m`` on — including combined forms like ``ESC[1;2m`` /
        ``ESC[0;2m`` / ``ESC[38;5;244;2m``; ``ESC[0m`` / ``ESC[22m`` off;
        colours do not clear it).
      - Returns plain text (all ANSI removed) suitable for ``pane_looks_idle``.
        A capture with no input row / no ANSI passes through equivalently
        (ANSI-stripping is then a no-op).

    EMPIRICAL BASIS (FIXTURE-PINNED on CC 2.1.206 — a documented TUI-drift
    audit surface, alongside the /update ``pane_command_is_claude`` A.0 note):
    dim is EXCLUSIVELY the ghost suggestion on this version. Probed live, both
    input-row-with-real-text states render the typed text at DEFAULT intensity
    with NO SGR-2: a draft typed WHILE Claude runs
    (``ESC[38;5;246m❯ ESC[39m<text>``) and the SAME draft at rest after the
    turn ended (``ESC[39m❯ <text>`` —
    ``fixtures/idle_real_draft_input_row_v2.1.206.txt``), vs the ghost
    (``ESC[39m❯ ESC[2m<text>ESC[0m`` —
    ``fixtures/idle_ghost_input_row_v2.1.206.txt``). A FUTURE CC version that
    renders a queued/real draft dim would make this blanking unsafe — the
    failure mode is blanking a genuine draft and letting /cost type over it —
    which is exactly why the fixture pin + this drift note exist: re-verify
    both captures on the next TUI-drift audit before trusting the SGR-2
    discriminator on a new version.

    Pure, stdlib-only, leaf-safe. ``pane_looks_idle`` /
    ``classify_pane_idle_failure`` are BYTE-UNTOUCHED — the pre-clean is applied
    at the callsite before them.
    """
    if not ansi_text:
        return ansi_text or ""
    out_lines: list[str] = []
    for raw_line in ansi_text.split("\n"):
        out_lines.append(_clean_ghost_input_line(raw_line))
    return "\n".join(out_lines)


def _clean_ghost_input_line(raw_line: str) -> str:
    """Clean ONE line: blank a fully-dim ghost after the prompt, then strip ANSI.

    Returns the ANSI-stripped line. When the line is an input row whose entire
    post-prompt text is dim, the text is replaced by nothing (a bare prompt +
    single trailing space, preserving everything up to and including the glyph).
    Otherwise the plain ANSI-stripped line is returned unchanged in content.
    """
    # Fast path: a line with NO escape byte at all is plain text — pass it
    # through unchanged (stripping would be a no-op). Every ESC-carrying line
    # runs through the SGR state machine below: a substring probe for the dim
    # code is NOT safe — valid COMBINED forms (``ESC[1;2m``, ``ESC[0;2m``,
    # ``ESC[38;5;244;2m``) carry dim without a literal ``ESC[2m`` byte shape,
    # and mis-classifying them would leave the ghost visible → the very false
    # refusal this helper exists to prevent (codex review P3).
    if "\x1b" not in raw_line:
        return raw_line

    # Walk the raw line, tracking dim state, and classify each VISIBLE char.
    # The SAME walker the PR-2 typed-state discriminator uses
    # (``_visible_chars_with_dim``) — one SGR state machine, two consumers.
    visible = _visible_chars_with_dim(raw_line)

    plain = "".join(c for c, _ in visible)
    stripped = plain.strip()
    if not stripped or stripped[0] not in _GHOST_PROMPT_GLYPHS:
        return plain  # not an input row → ANSI-stripped verbatim

    # Find the glyph position in ``visible`` and inspect the text after it.
    glyph_idx = next(i for i, (c, _) in enumerate(visible) if not c.isspace())
    after = visible[glyph_idx + 1 :]
    non_ws_after = [(c, d) for c, d in after if not c.isspace()]
    if not non_ws_after:
        return plain  # bare prompt already → nothing to blank
    if not all(d for _, d in non_ws_after):
        # A real draft or a dim/normal MIX → leave the text (fail closed).
        return plain
    # Every visible non-whitespace char after the prompt is dim → ghost. Blank
    # the text, preserving everything up to and including the prompt glyph plus
    # a single trailing space so the row reads ``❯ `` (empty input box).
    prefix = "".join(c for c, _ in visible[: glyph_idx + 1])
    return prefix + " "


def pane_looks_idle(
    visible_pane: str | None, *, allow_background_shells: bool = False
) -> bool:
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

    ``allow_background_shells`` (default False — ``/update``'s behavior is
    BYTE-IDENTICAL) SKIPS leg 5 ONLY. Leg 5 is a RESTART-specific guard: it
    exists because ``/update`` sends ``/exit``, which would silently kill the
    user's backgrounded shells. A caller that restarts NOTHING — the read-only
    ``/cost`` + ``/usage`` overlay interceptor, which types a slash command into
    an idle input box, captures the modal, and presses Escape — has no such
    hazard, and a running background shell is simply irrelevant to it (the
    owner's background agents kept ``· N shell`` on the status bar permanently,
    so ``/cost`` refused ~100% of the time). Every OTHER leg is unchanged: an
    active generation, a live interactive surface, a typed draft, and missing
    ready chrome still refuse in BOTH modes.
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
    # would silently kill them. None/0 never block (see the docstring). SKIPPED
    # for a caller that restarts nothing (``allow_background_shells``).
    if not allow_background_shells:
        jobs = parse_background_jobs(visible_pane)
        if jobs is not None and jobs >= 1:
            return False
    return True


# The COMPLETE set of leg names ``classify_pane_idle_failure`` can return
# (non-None) with the background-shells guard ENGAGED (the default /update
# mode). The /cost fallback copy map is exhaustiveness-tested against the
# reason set for ITS mode (``pane_idle_failure_reasons(allow_background_shells=
# True)``) — adding a new leg name to the classifier without mapped action copy
# in ``bot._USAGE_FALLBACK_ACTION`` (directly or via the bot's indeterminate
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

# The leg names that can ONLY fire while the background-shells guard is engaged.
# A caller opting out (``allow_background_shells=True`` — the read-only /cost +
# /usage overlay lane) can never observe them.
PANE_IDLE_BACKGROUND_SHELL_REASONS = frozenset({"background_shells"})


def pane_idle_failure_reasons(
    *, allow_background_shells: bool = False
) -> frozenset[str]:
    """The COMPLETE reason set ``classify_pane_idle_failure`` can return in a mode.

    The exhaustiveness anchor for a caller's fallback-copy map: a lane that opts
    OUT of the background-shells leg can never produce ``"background_shells"``,
    so mapping copy for it would be dead (and the copy — "the safety gate defers
    this until they finish" — would be a lie for a lane with no such gate).
    """
    if allow_background_shells:
        return PANE_IDLE_FAILURE_REASONS - PANE_IDLE_BACKGROUND_SHELL_REASONS
    return PANE_IDLE_FAILURE_REASONS


def classify_pane_idle_failure(
    visible_pane: str | None, *, allow_background_shells: bool = False
) -> str | None:
    """Name the FIRST ``pane_looks_idle`` leg that a non-idle pane fails.

    Diagnostic-only, REPLAY-only, NEVER authoritative — ``pane_looks_idle`` is
    the decider (the deliberately fail-closed five-gate proof ``/update`` and the
    ``/cost`` overlay interceptor rely on). This helper walks the SAME legs in the
    SAME order purely to LABEL the first failure for logging + reason-specific
    fallback copy. Its body mirrors ``pane_looks_idle`` line-for-line so the
    invariant holds: it returns ``None`` iff ``pane_looks_idle`` returns ``True``
    (pinned by an agreement test across every pane fixture, PARAMETRIZED over
    BOTH values of ``allow_background_shells``). Never returns pane text — only a
    fixed leg name.

    ``allow_background_shells`` MUST be passed the SAME value the authority was
    called with — the classifier and the authority stay in agreement only in
    lockstep (a caller that opts the leg out of the authority but not out of the
    classifier would LABEL a refusal that never happened).

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
        token (never returned when ``allow_background_shells`` is True).
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
    # (5) Live background shells (skipped in lockstep with the authority).
    if not allow_background_shells:
        jobs = parse_background_jobs(visible_pane)
        if jobs is not None and jobs >= 1:
            return "background_shells"
    return None


# ── Ready-input-box proof (GH #50 — text on a live interactive surface) ──
#
# ``pane_input_box_present`` is the POSITIVE structural proof that Claude Code
# is at its READY INPUT BOX — the one pane state where "type text, then press
# Enter" is the correct action. It is the gate ``SessionManager.send_to_window``
# runs before (and again after) writing a user payload.
#
# It is deliberately NOT ``pane_looks_idle``: that predicate additionally
# requires the input row to be EMPTY and every non-blank row inside the box to
# start with the prompt glyph — conditions we must NOT inherit here. Queueing a
# message while Claude is BUSY is a first-class flow (rig A2: the rule-pair +
# prompt row + ready chrome persist through every busy shape), and a
# pre-existing / wrapped / multi-line draft must still deliver (rig D10:
# continuation rows carry NO glyph).
#
# EMPIRICAL BASIS — CC 2.1.207 rig (2026-07-11, `temp/rig-20260711-s5/`,
# fixture-pinned as ``inputbox_*_v2.1.207.txt`` / ``gate_*_v2.1.207.txt``). A
# TUI-drift audit surface, like ``clean_ghost_input_text`` and
# ``pane_command_is_claude``.

# Leg 2 — the input-row prompt glyphs. ``❯`` in normal mode; ``!`` in Claude
# Code's BASH mode (rig C9: ``!\xa0echo hi``). A ``❯``-only leg would refuse
# EVERY ``!command`` payload. NOTE: the EMPTY input row is ``❯\xa0`` — a
# NON-BREAKING space, not ASCII (``str.strip()`` removes it, ``\s`` matches it).
_INPUT_PROMPT_GLYPHS: Final = ("❯", "!")

# Leg 2 — the picker trap. A live AUQ picker's BOTTOM rule-pair CONTAINS its
# option rows (``❯ 1. Red`` … ``4. Type something.``), so legs 1+2 would BOTH
# pass on a live picker. A glyph row of this shape is a picker cursor, never a
# ready prompt row.
#
# TWO NARROWINGS (r2 F1, both required — the unqualified trap FALSE-REFUSED any
# message starting with ``1. ``, which the delivery gate then left as a stranded
# draft, never sent):
#
#   (a) FIRST PROMPT ROW ONLY. The trap is a statement about the row the cursor
#       is on, not about every glyph row in the box. (Continuation rows of a
#       wrapped/multi-line draft carry no glyph, so in practice the first glyph
#       row IS the only one — but scanning every row made a numbered line
#       ANYWHERE in a multi-line draft trip it.)
#   (b) PAYLOAD-AWARE. The delivery gate WRITES the payload and re-verifies
#       AFTER, so an ordinary ``1. buy milk`` message legitimately renders the
#       box as ``❯ 1. buy milk``. When the caller passes ``expected_draft`` and
#       the first prompt row IS that draft (positive proof: a picker that stole
#       the keystrokes would show ITS OWN label, never our text), the trap is
#       SKIPPED. The PRE-write gate never passes ``expected_draft`` — there is
#       no payload in the box yet, so a ``❯ 1. …`` row there is either a live
#       picker or a human's own numbered draft, and refusing is fail-closed (the
#       disclosed residual).
#
# SAFETY (measured, not asserted): with the trap disabled ENTIRELY, every
# blocking pane in the 2.1.207 corpus is still refused by another leg (the AUQ
# single picker by leg 3 ``no_ready_chrome`` — a live picker replaces the ready
# status bar with its own ``Enter to select`` footer; every other blocking family
# by leg 1 ``no_input_box``), and every deliverable pane still passes. The trap is
# therefore kept as DEFENCE IN DEPTH for a hypothetical picker variant that
# renders ready-chrome below its own footer — it is not the load-bearing leg, so
# narrowing it costs no safety on the real corpus (pinned by
# ``test_option_row_trap_is_redundant_on_the_real_corpus``).
_RE_INPUT_OPTION_ROW: Final = re.compile(r"^[❯!]\s*\d+\.\s")
# The same shape tested against a prompt row whose glyph has ALREADY been
# stripped (``_prompt_row_content``). ``pane_blocking_prompt_shape`` keeps the
# glyph-inclusive form above — it scans raw pane rows.
_RE_OPTION_ROW_CONTENT: Final = re.compile(r"^\d+\.\s")

# Leg 3 — the ready-for-input status chrome alphabet observed BELOW the box on
# 2.1.207. A SUPERSET of ``_READY_STATUS_MARKERS``: ``esc to interrupt`` (a
# BUSY-but-queueable pane), ``! for shell mode`` (bash mode) and the
# PASTE-COLLAPSED hint (below) are ready-input chrome too — this gate never
# asserts idleness.
#
# THE PASTE-COLLAPSE (the GH #50 PR-1 regression, rig-reproduced 2026-07-11 —
# ``inputbox_paste_collapsed_v2.1.207.txt``). A payload written in ONE
# ``tmux send-keys -l`` past ~800 chars / ~13 lines is consumed by CC as a PASTE,
# and CC then does TWO things:
#
#   1. collapses the input row to a placeholder — ``❯\xa0[Pasted text #1 +12 lines]``
#      (NON-BREAKING space after the glyph; see ``_normalize_input_row``); and
#   2. **REPLACES THE STATUS BAR** with the single line ``  paste again to expand``.
#
# For ~2s NONE of the other ready markers is on the pane, so leg 3 returned
# ``no_ready_chrome`` and the delivery gate's post-write RE-VERIFY (which fires at
# ``TEXT_SETTLE_S`` = 0.5s, squarely inside that window) concluded there was no
# input box — even though the box is right there, holding the user's text, with
# Enter ready to submit it. Every long / multi-line message (a voice note with a
# reply-context quote — the owner's 809-char report) was refused AND left as a
# stranded draft AND braked the topic.
#
# It IS ready-for-input chrome: box present, cursor in it, Enter submits. And
# accepting it CANNOT let a blocking prompt through — a blocking prompt REPLACES
# the box, so it fails leg 1 (``no_input_box``) or leg 2
# (``prompt_row_is_option``) regardless of what leg 3's alphabet says. That is
# MEASURED, not asserted: ``test_paste_hint_below_a_blocking_pane_still_refuses``
# appends this very line to every blocking fixture and every one still refuses.
#
# Deliberately NOT added to ``_READY_STATUS_MARKERS`` (the IDLE-status-bar
# alphabet ``pane_looks_idle`` / ``classify_pane_idle_failure`` consume): a
# paste-collapsed pane is NOT idle — it holds an uncommitted draft, and
# ``/update`` restarting there would discard it. (``pane_looks_idle``'s
# empty-input-row leg already rejects it, so this is the semantically correct
# split, not a behavior change.) The interactive-GATE rejection lane
# (``_only_chrome_below``) consumes NO marker set at all — it is a structural
# ALLOW-LIST (blank / bare separator / the gate's own ``ctrl+<x>`` hints), so this
# line ALREADY rejects a quoted gate rendered above a live paste-collapsed box,
# which is exactly right: the hint proves the input box is live, so the "gate"
# above it is not the active bottom prompt. No coupling, no split needed.
_INPUT_PASTE_COLLAPSED_MARKER: Final = "paste again to expand"
# ``⏸ manual mode on`` (CC 2.1.208/2.1.209) — a ready input box whose status bar
# is the manual-mode indicator (rig ``inputbox_tall_draft_v2.1.209``). Added to
# the LEG-3 alphabet ONLY (not ``_READY_STATUS_MARKERS``, the idle alphabet
# ``pane_looks_idle`` / ``classify_pane_idle_failure`` consume) — the
# paste-collapse precedent: this widens the delivery gate's readiness proof
# without asserting the pane is idle. Its absence was the COUPLED alphabet gap in
# the GH #56 tall-draft repro: leg 3 returned ``no_ready_chrome`` on a
# manual-mode pane holding a draft, the same false-refusal class as the
# paste-collapse regression (memory rule: marker-alphabet completeness).
_INPUT_MANUAL_MODE_MARKER: Final = "manual mode on"
_INPUT_READY_CHROME_MARKERS: Final = _READY_STATUS_MARKERS + (
    "esc to interrupt",
    "! for shell mode",
    _INPUT_PASTE_COLLAPSED_MARKER,
    _INPUT_MANUAL_MODE_MARKER,
)
# ``· 1 shell ·`` — the background-shell status bar (rig D10). ASCII digits
# only (GH #56 r3 audit — ``\d`` is Unicode-wide, and this token is ACCEPT-side
# chrome proof; CC renders ASCII). The refusal-side traps
# (``_RE_INPUT_OPTION_ROW`` / ``_RE_OPTION_ROW_CONTENT``) and the idle-lane
# bg-shells parsers deliberately KEEP ``\d``: there, wider matching is the
# fail-closed direction (more refusals / more /update deferrals).
_RE_INPUT_READY_SHELL_TOKEN: Final = re.compile(r"·\s*[0-9]+\s+shells?\b")

# Leg 4 — the Enter-stealing background-tasks mode (rig §5 finding 1). One
# ``Down`` at an empty box while a background shell exists arms a mode where
# legs 1-3 ALL still pass but Enter is STOLEN: typed text is swallowed entirely
# and Enter opens the Shell-details modal. Reachable in production — the bot's
# own ungated nav keyboard sends ``Down``. (Esc reverts it.)
_INPUT_TASKS_MODE_MARKER: Final = "Enter to view tasks"

# The COMPLETE set of leg names ``classify_input_box_failure`` can return.
# The refusal-copy map in ``session.py`` is exhaustiveness-tested against it.
INPUT_BOX_FAILURE_REASONS: Final = frozenset(
    {
        "capture_empty",
        "no_input_box",
        "no_prompt_row",
        "prompt_row_is_option",
        "no_ready_chrome",
        "tasks_mode",
        "completion_overlay",
    }
)

# The INDETERMINATE subset — a frame that may simply be mid-redraw. The delivery
# gate RETRIES these; every other reason is a POSITIVE hazard and refuses on the
# first capture (the /cost preflight precedent).
INPUT_BOX_INDETERMINATE_REASONS: Final = frozenset(
    {
        "capture_empty",
        "no_input_box",
        "no_prompt_row",
        "no_ready_chrome",
    }
)


# The Unicode spaces CC renders INSIDE the input box. The empty input row is
# ``❯\xa0`` and the paste-collapsed row is ``❯\xa0[Pasted text #1 +12 lines]`` —
# a NON-BREAKING space (U+00A0) after the prompt glyph, not ASCII U+0020.
#
# Today's code happens to cope (``str.strip()`` drops NBSP, and ``\s`` matches it
# under Python's Unicode-aware ``re``), but that is INCIDENTAL and load-bearing:
# it decides whether the input row reads EMPTY (the stranded-draft brake's only
# release) and where the prompt-row content starts. So it is normalized
# EXPLICITLY and pinned on the real captured rows.
#
# SCOPE: applied ONLY to the rows INSIDE the input-box bracket, in
# ``_input_box_rows`` — the single seam every input-box-lane reader goes through
# (``_prompt_row_content`` / ``_completion_overlay_armed`` /
# ``classify_input_box_failure`` / ``pane_input_row_empty``). It deliberately does
# NOT touch the rule-separator scan, the chrome region below the box, or any
# other parser: normalizing NBSP→space globally would change unrelated matching
# (option labels, gate footers, prose).
_INPUT_ROW_UNICODE_SPACES: Final = (
    "\xa0",  # NO-BREAK SPACE — the one CC actually emits (rig 2.1.207)
    " ",  # FIGURE SPACE
    " ",  # NARROW NO-BREAK SPACE
    "﻿",  # ZERO WIDTH NO-BREAK SPACE (BOM) — width-0, but strips like space
)


def _normalize_input_row(row: str) -> str:
    """Fold CC's Unicode spaces in an input-box row to plain ASCII spaces.

    See ``_INPUT_ROW_UNICODE_SPACES`` — scoped to the input-box lane only.
    """
    for ch in _INPUT_ROW_UNICODE_SPACES:
        if ch in row:
            row = row.replace(ch, " ")
    return row


# GH #56: the exactly-one-separator fallback scans UPWARD for the input box's TOP
# rule when a TALL multi-line draft has pushed it above the ``_CHROME_SCAN_LINES``
# window (a reply-quoted message renders a ~18-row draft inside the box; the top
# rule sat outside the 20-line tail and the post-write re-verify concluded
# ``no_input_box``, withholding Enter and arming the stranded-draft brake). Bounded
# by this many rows — covers a full 160x50 pane; the gate/brake captures are
# visible-only (``scrollback_lines=0``), so the bound only protects a future
# scrollback-carrying caller. A TUI-drift audit surface beside
# ``clean_ghost_input_text``.
_INPUT_BOX_TOP_SCAN_LINES: Final = 60

# The status-row grammar: a WHOLE-ROW ORDERED TEMPLATE (GH #56 r3 fold — the
# APPROACH change that ends the segment-recombination class).
#
# Three rounds of per-segment validation each closed one combination and Codex
# found another: r1's SUBTRACTIVE grammar passed ``❯ /effort?``; r2's fullmatch
# skipped EMPTY ``·`` segments (``· /effort ·``) and cross-producted decorations
# (``/effort (manual mode on)``, ``⏵◐⏸/effort``); r3's enumerated whitelist still
# validated each segment INDEPENDENTLY, so ANY recombination of individually-
# valid segments passed — ``/effort · /effort``, ``paste again to expand · paste
# again to expand``, TWO incompatible mode markers (``⏸ manual mode on · ⏵⏵
# bypass permissions on``), and a UNICODE-digit shell token (``١ shell`` — ``\d``
# is Unicode-wide). Root cause: the segment set is small but the ROW space
# (order, count, repeats, digit width, marker compatibility) is large, so
# per-segment validation is the wrong SHAPE.
#
# r4 then showed the SLOT MACHINE was the same class a FIFTH time: ordering +
# at-most-once does not imply COMPATIBILITY, so mutually exclusive slots still
# coexisted (``⏸ manual mode on · paste again to expand`` passed, though the
# paste hint REPLACES the whole status bar), and normalizing Unicode spaces let
# an NBSP variant of a real row through.
#
# THE LESSON, recorded: ANY per-part predicate over segments/slots is
# unsoundable here — each round closes one combination and the next finds
# another. The terminal fix is WHOLE-ROW ENUMERATION (below): one anchored,
# complete-row template per real observed status bar, with only genuinely
# variable parts parameterized. Compatibility, ordering, repetition and
# emptiness all stop being separate rules — they are simply not expressible.
#
# THE CANONICAL GRAMMAR (r5 — the terminal shape).
#
# r4 tried a literal ENUMERATION of complete rows. It was SOUND but TOO NARROW
# against reality: sampling the owner's three LIVE bot panes (2.1.208/2.1.209)
# surfaced ``⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt ·
# ctrl+t to hide tasks · ← for agents`` — a hint the fixture corpus simply does
# not contain. Enumeration had mistaken "what our fixtures happen to hold" for
# "what CC renders", and it fail-closed EXACTLY on the busy/tasks panes where the
# owner's reply-quoted messages actually wedge. Hand-adding one more literal would
# repeat the mistake, so the shape is a CANONICAL ORDERED GRAMMAR: sound against
# every recombination the five review rounds found, and complete against every
# well-formed real bar.
#
# THE GRAMMAR (whole-row, anchored; every ``·``-split segment must be consumed):
#
#   ROW := EXCLUSIVE | BAR
#   EXCLUSIVE := "paste again to expand" | "! for shell mode"      (the WHOLE row)
#   BAR  := [MODE] [· SHELL] [· EFFORT-PAIR] [· HINT]*
#           …requiring MODE or ≥1 HINT (a bare shell/effort row is not a status bar)
#
#   MODE       — AT MOST ONE, never two, never repeated.
#   SHELL      — ASCII ONLY: ``[0-9]+ shell(s)[ still running]`` (``[0-9]``, NEVER
#                ``\d``, which is Unicode-wide — the ``١ shell`` spoof).
#   EFFORT-PAIR— the spinner + ``/effort`` as TWO consecutive segments, both or
#                neither (a bare ``/effort`` never validates).
#   HINT       — from a FIXED set, each AT MOST ONCE.
#
# THE EXCLUSIVE forms are WHOLE-ROW alternatives, so they are structurally unable
# to combine with a mode bar (the r4 P1a spoof ``⏸ manual mode on · paste again to
# expand``): the paste hint REPLACES the entire status bar, and that is expressed
# by making it a row, not a segment.
#
# HINT ORDER IS NOT ENFORCED — and that is a DELIBERATE, disclosed choice. The
# corpus + the three live rows pin ``esc → ctrl+t → ← → ↓`` and ``? → ←``, but NO
# observed row contains BOTH ``? for shortcuts`` and ``esc to interrupt``, so their
# relative order cannot be established without GUESSING. Order-freedom adds NO
# unsoundness: a valid bar's hints are all valid hints, while REPEATS and UNKNOWN
# text are still rejected (a hint may appear at most once, and every segment must
# be consumed).
#
# SOUNDNESS: this accepts exactly the WELL-FORMED status bars and rejects every
# malformed recombination the review rounds produced — two modes, a repeated
# segment, mode+paste, wide digits, NBSP variants, and embedded prose. The residual
# — pane content that reproduces a genuinely VALID status bar — is IDENTICAL for
# the enumeration and for this grammar (both accept valid bars by definition), so
# the grammar costs NO safety versus r4 while removing the false-refusal cliff.
# That is the honest trade, and it is why we are not hand-listing rows.
#
# BYTE DISCIPLINE: the chrome region is explicitly OUTSIDE
# ``_normalize_input_row``'s contract (scoped to the rows INSIDE the box), so this
# lane does NOT normalize Unicode spaces, trims ASCII-only, and uses NO
# Unicode-wide ``\s``/``\d``. An NBSP variant of a real row REFUSES.
#
# ACCEPTED COST: a real status bar whose SHAPE is outside this grammar still fails
# CLOSED — the tall-draft fallback does not fire on that pane, i.e. exactly today's
# shipped behavior (refuse), never a wrong commit.
_STATUS_ROW_EXCLUSIVE: Final = frozenset(
    {
        "paste again to expand",  # the paste-collapse hint REPLACES the bar
        "! for shell mode",  # bash mode
    }
)
# AT MOST ONE per row. The ``accept edits on`` / ``plan mode on`` siblings exist but
# their glyph decoration is not corpus-observed, so BOTH glyphs and BOTH the bare
# and parenthesized forms are accepted — the safe direction for COMPLETENESS, and
# it adds no recombination power (still exactly one mode segment).
_RE_STATUS_MODE: Final = re.compile(
    r"(?:⏵⏵|⏸)[ ]+"
    r"(?:bypass permissions on|accept edits on|plan mode on|manual mode on)"
    r"(?:[ ]+\(shift\+tab to cycle\))?"
)
# ASCII digits ONLY (``\d`` is Unicode-wide — the r3 ``١ shell`` spoof).
_RE_STATUS_SHELL: Final = re.compile(r"[0-9]+[ ]+shells?(?:[ ]+still[ ]+running)?")
_RE_STATUS_EFFORT_SPINNER: Final = re.compile(r"[◐◑◒◓][ ]+(?:low|medium|high|xhigh)")
_STATUS_EFFORT_TAIL: Final = "/effort"
# Each AT MOST ONCE; order-free (see the grammar note above).
_STATUS_ROW_HINTS: Final = frozenset(
    {
        "esc to interrupt",
        "ctrl+t to hide tasks",
        "ctrl+t to show tasks",
        "← for agents",
        "↓ to manage",
        "? for shortcuts",
        # The tasks-mode bar is a REAL status bar; accepting it lets the box be
        # located so leg 4 can refuse it precisely as ``tasks_mode`` (a positive
        # hazard) instead of the blunt ``no_input_box``.
        "Enter to view tasks",
    }
)


def _is_status_row(line: str) -> bool:
    """True iff ``line`` is a WHOLE Claude Code ready-status-bar row (GH #56).

    The canonical ordered grammar documented above: an EXCLUSIVE standalone form,
    or a BAR of ``[MODE] · [SHELL] · [EFFORT-PAIR] · [HINT…]`` with at-most-once
    membership, ASCII-only digits and spaces, no empty segments, and every segment
    consumed. The exactly-one-separator fallback in ``_input_box_rows`` uses it to
    prove the lone in-window separator is the input box's BOTTOM rule: the first
    non-blank row below a genuine bottom rule is the status bar.
    """
    s = _strip_ansi(line).strip(" \t\r\n")
    if not s:
        return False
    if s in _STATUS_ROW_EXCLUSIVE:
        return True

    segs = [seg.strip(" \t\r\n") for seg in s.split("·")]
    if any(not seg for seg in segs):
        return False  # a bare leading/trailing/double separator is never chrome

    i = 0
    has_mode = bool(segs) and _RE_STATUS_MODE.fullmatch(segs[0]) is not None
    if has_mode:
        i = 1
    if i < len(segs) and _RE_STATUS_SHELL.fullmatch(segs[i]):
        i += 1
    if (
        i + 1 < len(segs)
        and _RE_STATUS_EFFORT_SPINNER.fullmatch(segs[i])
        and segs[i + 1] == _STATUS_EFFORT_TAIL
    ):
        i += 2

    seen: set[str] = set()
    for seg in segs[i:]:
        if seg not in _STATUS_ROW_HINTS or seg in seen:
            return False  # unknown text, a repeat, or an out-of-place segment
        seen.add(seg)

    # A bare shell / effort row is not a status bar.
    return has_mode or bool(seen)


def _input_box_rows(lines: list[str]) -> tuple[int, int, list[str]] | None:
    """Locate the input-box rule-pair and return ``(top, bottom, non-blank rows)``.

    Two paths (GH #56):

    - **≥2 rule separators in the ``_CHROME_SCAN_LINES`` (20) tail** — byte-identical
      to the original: the last two ARE the pair. Every existing fixture keeps its
      exact result.
    - **Exactly 1** — a tall multi-line draft has pushed the box's TOP rule out of
      the window; the lone in-window separator is the candidate BOTTOM rule. The
      fallback fires ONLY on a THREE-PART STRUCTURAL proof (substring markers are
      spoofable by model-controlled prompt content, so each part kills an
      independently-reproduced spoof):
        (a) the FIRST NON-BLANK row below the lone separator FULLMATCHES
            ``_is_status_row`` (a genuine bottom rule is followed by the status
            bar — not a picker footer, and not model prose that merely embeds a
            marker substring);
        (b) NO row below the lone separator matches the picker option-row shape
            (Codex r2's "lone separator is a live prompt's TOP rule, ``❯ 1. Yes``
            below it" shape refuses here even if (a) were spoofed);
        (c) after an UPWARD scan (bounded by ``_INPUT_BOX_TOP_SCAN_LINES``, NEAREST
            rule) finds the candidate TOP rule, the FIRST non-blank row below it is
            a prompt-glyph (``❯``/``!``) row — the box's cursor row is always the
            first row inside the bracket (a draft that merely CONTAINS a rule-like
            ``─…`` line pairs with the draft-internal rule → no glyph row below →
            fail-closed refusal, unchanged from today).
      Any part failing / no reachable top rule → ``None`` (unchanged fail-closed).
    - **0** — ``None`` (unchanged).

    The returned rows are NBSP-normalized (``_normalize_input_row``) — this is the
    single seam every input-box-lane reader goes through.
    """
    search_start = max(0, len(lines) - _CHROME_SCAN_LINES)
    sep_idxs = [
        i for i in range(search_start, len(lines)) if _is_rule_separator(lines[i])
    ]
    if len(sep_idxs) >= 2:
        top, bottom = sep_idxs[-2], sep_idxs[-1]
    elif len(sep_idxs) == 1:
        located = _locate_tall_draft_pair(lines, sep_idxs[0])
        if located is None:
            return None
        top, bottom = located
    else:
        return None
    rows = [
        stripped
        for i in range(top + 1, bottom)
        if (stripped := _normalize_input_row(lines[i]).strip())
    ]
    return top, bottom, rows


def _locate_tall_draft_pair(lines: list[str], bottom: int) -> tuple[int, int] | None:
    """The GH #56 exactly-one-separator fallback: prove ``bottom`` is the input
    box's bottom rule and scan UPWARD for its top rule. See ``_input_box_rows``.

    ``lines`` are already ANSI-stripped by the callers, but each candidate row is
    re-stripped / whitespace-trimmed before the full-row and option-row matches
    (defensive, r3 P3).
    """
    # (a) The first non-blank row below the lone separator must BE a status bar.
    first_below = next(
        (lines[i] for i in range(bottom + 1, len(lines)) if lines[i].strip()),
        None,
    )
    if first_below is None or not _is_status_row(first_below):
        return None

    # (b) No picker option-row may sit below the lone separator (a live picker's
    #     body would — the presumed bottom rule would then be the prompt's TOP).
    for i in range(bottom + 1, len(lines)):
        if _RE_INPUT_OPTION_ROW.match(_normalize_input_row(lines[i]).strip()):
            return None

    # (c) The NEAREST rule separator above (bounded) is the candidate TOP rule, and
    #     the first non-blank row inside the bracket must be a prompt-glyph row.
    lower = max(0, bottom - _INPUT_BOX_TOP_SCAN_LINES)
    top = next(
        (i for i in range(bottom - 1, lower - 1, -1) if _is_rule_separator(lines[i])),
        None,
    )
    if top is None:
        return None
    first_inside = next(
        (
            stripped
            for i in range(top + 1, bottom)
            if (stripped := _normalize_input_row(lines[i]).strip())
        ),
        None,
    )
    if first_inside is None or first_inside[0] not in _INPUT_PROMPT_GLYPHS:
        return None
    return top, bottom


def _prompt_row_content(rows: list[str]) -> str | None:
    """The FIRST glyph row's text with the prompt glyph stripped, or ``None``.

    ``None`` iff no row inside the input-box bracket starts with a prompt glyph.
    Continuation rows of a wrapped / multi-line draft carry NO glyph (rig D10),
    so the first glyph row is the row the cursor sits on.
    """
    for row in rows:
        if row and row[0] in _INPUT_PROMPT_GLYPHS:
            return row[1:].strip()
    return None


def _row_is_our_draft(
    row: str | None, expected_draft: str | None, *, exact: bool
) -> bool:
    """True iff the input row's content is the payload WE just wrote.

    Positive proof of authorship: a blocking prompt that stole our keystrokes
    renders ITS OWN label in that row, never our text. Compared against the
    FIRST LINE of the payload, because the terminal soft-wraps a long line across
    continuation rows.

    ``exact=False`` accepts the wrapped shape (the visual row is a PREFIX of our
    first line) — used by the picker trap, where a prefix cannot be confused with
    a picker label. ``exact=True`` demands the full first line and is used by the
    ``/`` completion-overlay exemption, where a PREFIX is precisely the hazard
    (a half-written ``/co`` arms the overlay and Enter would run ``/copy`` — GH
    #53; the exemption must never be handed to a prefix).
    """
    if row is None or not expected_draft:
        return False
    first_line = expected_draft.split("\n", 1)[0].strip()
    candidate = row.strip()
    if not first_line or not candidate:
        return False
    if exact:
        return candidate == first_line
    return candidate == first_line or first_line.startswith(candidate)


def _completion_overlay_armed(rows: list[str], *, allow_slash: bool) -> bool:
    """True iff the input box's cursor token arms an Enter-STEALING overlay.

    Rig §5 finding 2 (2.1.207): the completion overlay fires ONLY when the
    cursor token is an active trigger —

      - a trailing ``@prefix`` (``please ask @se`` ⇒ Enter selected ``seed.txt``
        and the message was NEVER sent — this is live TODAY: any Telegram
        message ending in ``@word`` strands unsent), or
      - a bare ``/prefix`` (``/co`` ⇒ Enter executed ``/copy``).

    A mid-text ``@alice``, an email address, and ``tell me about / division`` do
    NOT trigger it, and a slash command WITH an argument (``/effort high``)
    raises no overlay at all.

    ``allow_slash`` exempts the ``/`` arm. It is set ONLY at the post-write
    re-verify, and ONLY when the payload the bot just wrote IS itself a bare
    ``/command`` **AND the input row's content IS that exact payload** (r2 F6 —
    the caller AND-s ``allow_slash_completion`` with ``_row_is_our_draft(...,
    exact=True)``): that overlay is the shipped slash-command mechanism
    (``forward_command_handler`` has always relied on Enter running the
    sorted-first entry, which for an EXACT command is the right one). Keying the
    exemption on the payload SHAPE alone also exempted a PRE-EXISTING ``/co``
    draft a human left in the box — Enter would then run ``/copy`` on text we
    never authored. The bare-ambiguous-prefix misfire itself is GH #53, filed
    separately and explicitly out of scope; this narrowing only refuses to WIDEN
    it. The ``@`` arm is NEVER exempt — it is pure data loss.
    """
    if not rows:
        return False
    # The prompt row is the FIRST glyph row; continuation rows of a wrapped /
    # multi-line draft carry no glyph (rig D10).
    prompt_idx = next(
        (i for i, s in enumerate(rows) if s and s[0] in _INPUT_PROMPT_GLYPHS),
        None,
    )
    if prompt_idx is None:
        return False
    typed_rows = list(rows[prompt_idx:])
    typed_rows[0] = typed_rows[0][1:].strip()  # drop the glyph

    # ``/`` arm: a BARE ``/word`` — the whole input is one whitespace-free token.
    if not allow_slash:
        joined = " ".join(r for r in typed_rows if r)
        if joined.startswith("/") and not any(c.isspace() for c in joined):
            return True

    # ``@`` arm: the LAST visual row's LAST token starts with ``@``.
    last = next((r for r in reversed(typed_rows) if r), "")
    tokens = last.split()
    return bool(tokens) and tokens[-1].startswith("@")


def classify_input_box_failure(
    pane_text: str | None,
    *,
    allow_slash_completion: bool = False,
    expected_draft: str | None = None,
) -> str | None:
    """Name the FIRST ``pane_input_box_present`` leg a pane fails, or ``None``.

    ``None`` iff ``pane_input_box_present`` returns ``True`` (pinned by an
    agreement test over every pane fixture — the ``classify_pane_idle_failure``
    precedent). Never returns pane text — only a fixed leg name from
    ``INPUT_BOX_FAILURE_REASONS``.

    The five legs, in order:

      1. the BOTTOM pair of ``──`` rule separators is present (``no_input_box``);
      2. a genuine prompt row sits inside that pair — glyph ``❯`` or ``!`` — and
         the FIRST such row is NOT a numbered picker-option row (``no_prompt_row``
         / ``prompt_row_is_option``);
      3. ready-for-input status chrome is present BELOW the box
         (``no_ready_chrome``);
      4. the status bar does NOT carry ``Enter to view tasks`` (``tasks_mode``);
      5. no input-capturing completion overlay is armed (``completion_overlay``).

    ``expected_draft`` is the payload the DELIVERY GATE just wrote into the box.
    It is passed ONLY at the post-write re-verify and is PURE EVIDENCE of
    authorship: when the first prompt row IS that draft, leg 2's picker trap is
    skipped (an ordinary ``1. buy milk`` message must not be mistaken for a
    picker cursor) and leg 5's ``/`` exemption is unlocked (only for the EXACT
    payload — see ``_completion_overlay_armed``). It can never make a pane pass
    that a picker owns: a picker that stole the keystrokes shows ITS label, not
    our text, and legs 1/3 refuse it regardless.

    Tolerates an ANSI capture (escapes are stripped locally) so the caller may
    pass either form.
    """
    if not pane_text:
        return "capture_empty"
    lines = _strip_ansi(pane_text).split("\n")

    # (1) The structural input-box bracket.
    located = _input_box_rows(lines)
    if located is None:
        return "no_input_box"
    _top, bottom, rows = located

    # (2) A genuine prompt row inside the bracket — the FIRST glyph row is the
    # row the cursor sits on.
    prompt_row = _prompt_row_content(rows)
    if prompt_row is None:
        return "no_prompt_row"
    if _RE_OPTION_ROW_CONTENT.match(prompt_row) and not _row_is_our_draft(
        prompt_row, expected_draft, exact=False
    ):
        return "prompt_row_is_option"

    # (3) Ready-for-input status chrome below the box.
    below = "\n".join(lines[bottom + 1 :])
    if not (
        any(marker in below for marker in _INPUT_READY_CHROME_MARKERS)
        or _RE_INPUT_READY_SHELL_TOKEN.search(below)
    ):
        return "no_ready_chrome"

    # (4) The Enter-stealing background-tasks mode.
    if _INPUT_TASKS_MODE_MARKER in below:
        return "tasks_mode"

    # (5) An input-capturing completion overlay. The ``/`` exemption needs BOTH
    # a bare-slash payload (the caller's flag) AND positive proof the row holds
    # exactly that payload (r2 F6) — a pre-existing human ``/co`` draft is never
    # exempted.
    allow_slash = allow_slash_completion and _row_is_our_draft(
        prompt_row, expected_draft, exact=True
    )
    if _completion_overlay_armed(rows, allow_slash=allow_slash):
        return "completion_overlay"

    return None


def pane_input_row_empty(pane_text: str | None) -> bool | None:
    """Is Claude Code's input row EMPTY? ``None`` when it cannot be determined.

    The self-heal probe for the delivery gate's stranded-draft brake (r2 F2): the
    brake is released only on POSITIVE proof that the withheld draft is gone from
    the box. ``None`` is INDETERMINATE and the caller must fail closed — an empty
    capture, no rule-pair, no prompt row, or a prompt row of PICKER-OPTION shape
    (a live picker's option rows sit inside a rule-pair too, so "the box exists"
    cannot be concluded there — and the probe carries no payload evidence to tell
    a picker cursor from a numbered draft).

    Pre-cleans a CC ≥2.1.206 DIM ghost suggestion (``clean_ghost_input_text``,
    the ``/update`` + ``/cost`` precedent) — a plain capture reads it as a typed
    draft and would strand the brake forever. Continuation rows of a wrapped
    draft carry no glyph, so every row from the prompt row down is inspected.
    """
    if not pane_text:
        return None
    lines = clean_ghost_input_text(pane_text).split("\n")
    located = _input_box_rows(lines)
    if located is None:
        return None
    _top, _bottom, rows = located
    prompt_idx = next(
        (i for i, s in enumerate(rows) if s and s[0] in _INPUT_PROMPT_GLYPHS), None
    )
    if prompt_idx is None:
        return None
    typed = list(rows[prompt_idx:])
    typed[0] = typed[0][1:].strip()
    if _RE_OPTION_ROW_CONTENT.match(typed[0]):
        return None
    return not any(t.strip() for t in typed)


def pane_blocking_prompt_shape(pane_text: str | None) -> bool:
    """True iff the pane's BOTTOM carries a live numbered-option cursor row.

    A cheap, RECOGNIZER-FREE positive hazard signal (never authoritative — the
    delivery gate's decision is always ``pane_input_box_present``). It exists so
    a blocking surface the named recognizers miss — a folder-trust prompt with
    the display kill-switches OFF, a Workflow gate, a never-shipped prompt — is
    classified ``prompt_present`` (refuse IMMEDIATELY, with the actionable
    "answer the card first" copy) instead of falling through the INDETERMINATE
    retry path to generic "couldn't confirm the input box" copy.

    Only consulted when the input-box proof has ALREADY failed, so a resolved
    picker still sitting in scrollback ABOVE a live input box can never trip it.
    """
    if not pane_text:
        return False
    lines = [ln.strip() for ln in _strip_ansi(pane_text).split("\n")]
    # A blocking prompt is drawn where the cursor is, so a full-height capture
    # carries a run of TRAILING BLANK rows below it (rig: the folder-trust
    # arrival pane has 22). Anchor on the last non-blank content, not on the raw
    # bottom window (the input-box legs deliberately keep the raw window — a
    # ready box always ends in its status bar).
    while lines and not lines[-1]:
        lines.pop()
    tail = lines[max(0, len(lines) - _CHROME_SCAN_LINES) :]
    return any(_RE_INPUT_OPTION_ROW.match(ln) for ln in tail if ln)


def pane_input_box_present(
    pane_text: str | None,
    *,
    allow_slash_completion: bool = False,
    expected_draft: str | None = None,
) -> bool:
    """POSITIVE proof the pane is at Claude Code's READY INPUT BOX (GH #50).

    The delivery gate for every user payload typed into a window. See
    ``classify_input_box_failure`` for the five legs, ``expected_draft``'s
    evidence role, and ``INPUT_BOX_FAILURE_REASONS`` for the leg names.

    WHY THE INVERSION WORKS (rig-confirmed on all six blocking families —
    AskUserQuestion single + multi, ExitPlanMode, folder-trust, ``Switch
    model?``, Permission, Workflow): a live blocking prompt **REPLACES** the
    input box + status chrome. The positive proof therefore fails on *every*
    prompt — known, unknown, unparsed, half-drawn — WITHOUT the parser having to
    recognize it. That matters: ``Switch model?`` is footer-less and
    ``parse_generic_decision`` returns ``None`` for it, so a negative
    formulation ("no known prompt matched") would deliver straight into it and
    Enter would commit option 1.

    The gate never consults ``_active_ui_patterns``, so it is
    **flag-independent by construction** — the display kill-switches
    (``CC_TELEGRAM_PERMISSION_PROMPTS`` / ``CC_TELEGRAM_DECISION_CARDS``) can
    never reopen the hole.

    Deliberately NOT asserted: no-active-run, background-shell absence, and
    input-row emptiness (see the module comment above).

    Accepted, disclosed residual: at the PRE-write gate (no ``expected_draft``),
    a HUMAN's own draft whose first visual row reads like a picker option
    (``❯ 1. buy milk``, typed in the terminal) fails leg 2 and the send is
    refused — fail-closed, and rare. The bot's OWN numbered payload is NOT
    affected: the re-verify passes ``expected_draft`` and skips the trap.
    """
    return (
        classify_input_box_failure(
            pane_text,
            allow_slash_completion=allow_slash_completion,
            expected_draft=expected_draft,
        )
        is None
    )


# ── The free-text affordance row (GH #50 PR-2) ───────────────────────────
#
# Claude Code auto-appends a free-text affordance row to a picker:
#
#   AskUserQuestion  row N+1  ``Type something.``
#
# Selecting that row and typing turns the user's prose INTO the answer — the lane
# PR-2 drives, so a Telegram message sent at a live card actually answers it.
#
# SCOPE: **AskUserQuestion ONLY** (owner decision 2026-07-12). ExitPlanMode has an
# affordance row too (row 4, ``Tell Claude what to change``) and an earlier PR-2
# revision drove it, but the lane was DROPPED: naming a plan prompt would have
# required a whole new ``PreToolUse(ExitPlanMode)`` hook + side file, and the owner
# runs ``--dangerously-skip-permissions`` anyway. An EPM card now takes PR-1's
# refusal. The helpers below are AUQ-only; nothing here parses an EPM row.
#
# SGR-2 IS THE *PRE-TYPE* DISCRIMINATOR, AND THAT IS THE ONLY CLAIM IT SUPPORTS
# (rig, 2026-07-12 — the earlier "typed-state proof" framing OVERCLAIMED and is
# CORRECTED here). While the affordance row is SELECTED but UNTYPED its label is
# the placeholder and renders DIM (``ESC[2m``); typing replaces the label with
# the user's text at normal intensity. ``dim=True`` therefore holds for EXACTLY
# ONE shape: **the selected, untyped placeholder.** A real option row is never
# dim — not even when it is the selected row.
#
# What that does and does NOT buy:
#
#   * It is the LOAD-BEARING guard of the free-text lane, consumed BEFORE the
#     first byte is typed: the executor requires the row under the cursor to be
#     ``cursor and dim and label == AUQ_FREE_TEXT_LABEL``. Nothing else on a
#     picker satisfies all three, so an overshoot / undershoot / stale-frame nav
#     that parked the cursor on a REAL option cannot pass it, and the payload is
#     never typed there (rig-verified: typing while parked on a real option row
#     is SWALLOWED entirely — the pane stays byte-identical).
#   * It is NOT a post-type identity proof. ``dim=False`` on a cursored row is
#     satisfied by any selected REAL option row, and the executor's own
#     "is this label a prefix of our payload?" check is satisfied by an option
#     labelled ``Yes`` under a payload ``Yes, but use postgres``. The post-write
#     legs are corroboration, not a guard — see ``handlers/free_text``.
#
# Fixtures: ``auq_freetext_row_selected_pretype_v2.1.207.ansi.txt`` (dim),
# ``auq_freetext_row_typed_v2.1.207.ansi.txt`` (plain),
# ``auq_freetext_typed_identical_label_v2.1.207.ansi.txt`` (typing the literal
# placeholder text still renders PLAIN),
# ``auq_freetext_row_typed_large_v2.1.207.ansi.txt`` (a 947-char multi-line
# voice-note-shaped payload), ``auq_freetext_overflow_v2.1.207.txt`` (the ~5.3 k
# answer that scrolls the option block away).
#
# A TUI-DRIFT AUDIT SURFACE, like ``clean_ghost_input_text`` (the other SGR-2
# consumer) and ``pane_command_is_claude``. It is why the lane is
# VERSION-LICENSED (``handlers/free_text``): a CC release that renders the
# placeholder at normal intensity would defeat the pre-type landing proof, so an
# unlicensed version degrades to PR-1's refusal rather than trusting a stale
# empiric.

# The affordance placeholder. EXACT (post-strip) match — the label is half of the
# pre-type landing proof, so a drifted label must NOT be silently accepted.
AUQ_FREE_TEXT_LABEL: Final = "Type something."

# ``ctrl+g to edit in <editor>`` — Claude Code's "you are in a text field" hint.
#
# **NOT AN EXACT ROW PROOF (rig, 2026-07-12 — this CORRECTS an earlier claim that
# it "tracks the cursor exactly").** It is absent on the real option rows 1/2/3
# and present on the affordance row N+1 — but it is ALSO present on the
# ``N+2. Chat about this`` row, which is a text field too. So it proves "the
# cursor is on SOME text-field row", never WHICH row and never WHICH card. It is
# used only as post-write CORROBORATION in the overflow shape (the option block
# has scrolled off, so the row itself is unobservable); the load-bearing guard is
# the PRE-TYPE landing proof above, which runs before any byte is typed.
_RE_FREE_TEXT_EDIT_HINT: Final = re.compile(r"ctrl[+-]g to edit")


@dataclass(frozen=True)
class FreeTextRow:
    """One parse of a free-text affordance row from an ANSI pane capture."""

    number: int
    label: str
    cursor: bool
    # True iff EVERY visible non-whitespace char of the label falls inside an
    # ACTIVE SGR-2 (dim) region ⇒ the row still holds its UNTYPED placeholder.
    # False ⇒ the label is typed text (or a mixed/plain render — fail-closed for
    # the pre-type landing proof, which REQUIRES dim).
    dim: bool


def parse_free_text_row(ansi_pane: str | None, *, number: int) -> FreeTextRow | None:
    """Parse the numbered row ``number`` out of an ANSI pane capture.

    Returns ``None`` when the row is not on the pane (it can legitimately scroll
    off — see the overflow note in ``handlers/free_text``). The BOTTOM-most match
    wins: a scrolled TUI capture can retain a stale copy of the row above the
    live one, and the live render is always the lower (the same bottom-most rule
    ``_parse_numbered_options`` uses for the cursor).

    Requires an ANSI capture (``capture_pane(with_ansi=True)``) — ``dim`` is the
    whole point and a plain capture has thrown the styling away.
    """
    if not ansi_pane:
        return None
    found: FreeTextRow | None = None
    for raw_line in ansi_pane.split("\n"):
        visible = _visible_chars_with_dim(raw_line)
        plain = "".join(c for c, _ in visible)
        m = _RE_NUMBERED_OPTION.match(plain)
        if m is None:
            continue
        try:
            if int(m.group("num")) != number:
                continue
        except ValueError:
            continue
        label = m.group("label").strip()
        if not label:
            continue
        # Map the label back onto the dim-annotated visible chars via the
        # regex span — ``visible`` is exactly ``plain`` char-for-char.
        start, end = m.span("label")
        label_chars = [(c, d) for c, d in visible[start:end] if not c.isspace()]
        dim = bool(label_chars) and all(d for _, d in label_chars)
        found = FreeTextRow(
            number=number,
            label=label,
            cursor=m.group("cursor").strip() in ("❯", "›", "▶", "*"),
            dim=dim,
        )
    return found


def _visible_chars_with_dim(raw_line: str) -> list[tuple[str, bool]]:
    """Walk one ANSI line → ``[(visible_char, is_dim), …]`` (the SGR-2 state).

    The shared SGR machine behind ``clean_ghost_input_text``'s ghost detector and
    the PR-2 typed-state discriminator: combined forms (``ESC[1;2m`` /
    ``ESC[0;2m`` / ``ESC[38;5;244;2m``) carry dim without a literal ``ESC[2m``
    byte shape, so a substring probe would be WRONG — the state machine is the
    only correct reader.
    """
    dim = False
    pos = 0
    out: list[tuple[str, bool]] = []
    n = len(raw_line)
    while pos < n:
        if raw_line[pos] == "\x1b":
            m = _RE_CSI.match(raw_line, pos)
            if m is not None:
                if m.group(2) == "m":
                    dim = _sgr_updates_dim(m.group(1), dim)
                pos = m.end()
                continue
            m2 = _RE_ANSI_ANY.match(raw_line, pos)
            if m2 is not None:
                pos = m2.end()
                continue
            pos += 1
            continue
        out.append((raw_line[pos], dim))
        pos += 1
    return out


def auq_free_text_row_active(pane_text: str | None) -> bool:
    """True iff a LIVE AUQ picker has a TEXT-FIELD row as the active row.

    The signal is the bottom-most picker footer carrying BOTH ``Enter to select``
    (the picker is live and Enter commits the cursor row) AND ``ctrl+g to edit``
    (the cursor row is a TEXT FIELD).

    **WHAT IT IS NOT (rig, 2026-07-12 — this CORRECTS the original docstring,
    which claimed the hint "tracks the cursor exactly" and was therefore an exact
    row proof).** The hint is absent on the real option rows 1/2/3 and present on
    the affordance row N+1 — but it is ALSO present on the ``N+2. Chat about
    this`` row, which is a text field too. So this predicate proves "the cursor is
    on SOME text-field row of this live picker". It does NOT prove WHICH row, and
    it certainly does not prove WHICH CARD.

    It is therefore used for exactly one thing: post-write CORROBORATION in the
    OVERFLOW shape, where a long answer scrolls the whole option block — the
    ``❯ N+1.`` cursor row included — off the top of an alternate screen and the
    row itself is genuinely unobservable (rig: ``capture-pane -S`` recovers
    nothing). By then the payload has already been typed into the row the PRE-TYPE
    landing proof positively identified (cursor + SGR-2 dim + the exact
    placeholder label), which is the guard that decides where the bytes go. This
    predicate never authorizes a keystroke.

    SCOPED TO THE LIVE PICKER (peer-review P1): the pane must extract as a live
    ``AskUserQuestion`` and the footer consulted is the BOTTOM-MOST one (this
    repo's bottom-most-is-live rule) — otherwise a STALE footer in scrollback, or
    a transcript the user pasted, could mint the signal from arbitrary pane text.
    """
    if not pane_text:
        return False
    content = extract_interactive_content(pane_text)
    if content is None or content.name != "AskUserQuestion":
        return False
    footer: str | None = None
    for line in pane_text.split("\n"):
        if _RE_PICKER_FOOTER.search(line):
            footer = line  # bottom-most on the pane is the LIVE one
    if footer is None:
        return False
    return bool(_RE_FREE_TEXT_EDIT_HINT.search(footer))


# ── The STABLE surface-generation identity (peer-review P1) ──────────────
#
# THE DRIFT TRAP. A free-text transaction MUTATES the very pane it must
# re-identify: it moves the cursor onto the affordance row, and then it REPLACES
# that row's label with the user's prose. Both mutations move the naive form
# fingerprint:
#
#   * the cursor — ``AskUserQuestionForm._canonical_repr`` is ALREADY
#     cursor-blind (the AUQ pick-dispatch lane needs that property for exactly
#     the same reason), so this one is free; but
#   * the TYPED LABEL is not. ``_parse_numbered_options`` DROPS a row whose label
#     ``is_affordance_label`` ("Type something." / "Chat about this"), so a
#     pristine AUQ parses 3 options — and the instant the user's text lands in row
#     4 it stops being an affordance and parses as a FOURTH REAL OPTION, so
#     ``OPTS:`` moves and a form fingerprint captured pre-type can NEVER match
#     post-type.
#
# So the identity is made stable BY CONSTRUCTION: it is cursor-blind (inherited)
# and TARGET-ROW-BLIND — every option at or below the affordance row is dropped
# before the canonical is taken. What survives is exactly the part of the surface
# the transaction never touches: the REAL options 1..target_row-1. Requiring that
# prefix to be COMPLETE and contiguous is what makes a missing block fail CLOSED
# (``None``) instead of silently degrading to a shorter, weaker identity.
#
# The canonical itself is the repo's EXISTING one — ``AskUserQuestionForm.
# fingerprint()`` — never a new hash: mint/validate parity is the house rule.

_FREE_TEXT_IDENTITY_VERSION: Final = "ft1"


def free_text_surface_identity(pane_text: str | None, *, target_row: int) -> str | None:
    """The pane-derived surface-generation identity of a free-text-capable AUQ.

    STABLE across the two mutations the executor itself performs (see the block
    comment above): the cursor move onto the affordance row, and the typing that
    replaces its label. DISCRIMINATING across a genuinely different card, because
    the real options carry the question.

    ``None`` ⇒ **not recoverable from this pane** — a scrolled / partial /
    mid-redraw frame, or a pane that is no longer an AskUserQuestion. Callers
    MUST fail closed on ``None``; they must NEVER fall back to a weaker identity.

    ``target_row`` is the affordance row's number (N+1: affordance rows are
    DROPPED from ``options``, so a 3-option picker's ``Type something.`` is 4).
    """
    if not pane_text or target_row < 2:
        return None

    form = parse_ask_user_question(pane_text)
    if form is None:
        return None
    options = form.options
    # ``current_question_title`` only when JSONL overlaid it — NEVER
    # ``pane_walkback_title``, which is scraped from the churning scrollback
    # above the block and is explicitly barred from identity checks.
    title = form.current_question_title

    real = tuple(o for o in options if o.number is not None and o.number < target_row)
    if [o.number for o in real] != list(range(1, target_row)):
        # The real-option prefix is incomplete (scrolled off / mid-redraw /
        # renumbered) ⇒ the identity is UNRECOVERABLE. Fail closed.
        return None

    ident = AskUserQuestionForm(
        # ``tabs`` are deliberately dropped: a single-question form has at most
        # one, it adds no discrimination, and its answered/current flags are one
        # more thing that could drift mid-transaction into a FALSE refusal (which
        # post-type costs a stranded draft).
        tabs=(),
        current_question_title=title,
        options=tuple(
            AskOption(
                label=o.label,
                recommended=o.recommended,
                cursor=False,
                number=o.number,
            )
            for o in real
        ),
        # Pinned so a mid-transaction re-render can't move them.
        select_mode="single",
        is_free_text=True,
        is_review_screen=False,
    )
    return f"{_FREE_TEXT_IDENTITY_VERSION}:AskUserQuestion:{ident.fingerprint()}"


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
