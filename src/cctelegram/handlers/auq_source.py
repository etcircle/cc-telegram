"""Trust boundary for the PreToolUse AskUserQuestion side file + the single AUQ-source resolver.

This neutral leaf owns two things behind a tiny interface:

  1. The *untrusted-file trust boundary* — nothing outside this module
     reads ``auq_pending/<session_id>.json`` or constructs that path.
     Path-traversal defense, schema/fingerprint validation (recompute,
     never trust the stored fingerprint), TTL + future-skew guards, the
     pane-projection consistency predicate (with the ``checked_any``
     vacuous-true fail-closed guard), and the bot-startup GC all live here.

  2. The *single AUQ-source resolver* — ``resolve_auq_source(...)`` returns a
     typed ``ResolvedAuqSource`` with a per-kind ``source_fingerprint``. Both
     the render/mint path (``interactive_ui``) and the validate path
     (``pick_token``) import this resolver, so mint/validate source parity is
     a call-graph fact rather than a comment promise, and the fingerprint is
     the measurable parity witness.

Core responsibilities:
  - Resolve "what is the trustworthy AUQ source for this window right now?"
    via the priority chain side_file → jsonl_cache → pane.
  - Own the per-window ``_pretool_ask_records`` cache (revalidate-on-every-call;
    never stale-serve).
  - Stay a true leaf: imports neither ``interactive_ui`` nor ``pick_token``;
    the in-process JSONL cache is read through an injected getter.

Key components:
  - ``ResolvedAuqSource`` / ``resolve_auq_source`` — the typed resolver.
  - ``PreToolAskRecord`` / ``resolve_record`` — the side-file trust boundary.
  - ``peek_sticky_source`` — re-resolve the EXACT source a callback was minted
    against (side_file / jsonl_cache), pane-AGNOSTIC, so the ``aqt:`` toggle can
    pin its minted source through a transient render→tap source flip.
  - ``side_file_live_for_session`` / ``side_file_live_for_window`` —
    pane-INDEPENDENT "is the AUQ still live?" authority for the card-clear
    gate and the startup orphan reconciler (presence + schema + future-skew,
    deliberately NO read-TTL and NO pane-consistency check). The session-keyed
    form is canonical; the window form is a ``peek``-resolving wrapper.
  - ``unlink_for_session`` / ``forget_for_window`` / ``gc_stale`` — lifecycle.
  - ``set_jsonl_cache_getter`` / ``reset_for_tests`` — injection + test seam.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, Literal

from ..session import peek_session_id_for_window, read_session_id_for_window_fresh
from ..utils import app_dir

if TYPE_CHECKING:
    # Annotation-only — kept out of module import time so ``auq_source`` stays a
    # leaf with no eager ``terminal_parser`` edge (the parser fns it calls are
    # deferred-imported inside the functions that use them).
    from ..terminal_parser import AskUserQuestionForm

logger = logging.getLogger(__name__)


# ── Injected in-process JSONL cache getter (avoids the import cycle) ─────────
#
# The ``jsonl_cache`` branch needs to read ``interactive_ui``'s in-process
# ``_last_completed_ask_tool_input`` cache, but this leaf must not import
# ``interactive_ui``. So the getter is INJECTED, with a pinned lifecycle:
#   - default = the no-op below, so the module is importable and resolves the
#     ``pane`` branch correctly even before any wiring;
#   - ``set_jsonl_cache_getter`` rebinds it once at ``bot.post_init``;
#   - ``reset_for_tests`` rebinds it back to the no-op default.
_jsonl_cache_getter: Callable[[str], dict | None] = lambda _window_id: None  # noqa: E731


def set_jsonl_cache_getter(getter: Callable[[str], dict | None]) -> None:
    """Inject the in-process JSONL cache reader for the ``jsonl_cache`` branch.

    Called once at ``bot.post_init`` with a closure over
    ``interactive_ui._last_completed_ask_tool_input.get``. Keeping this an
    injection (rather than importing ``interactive_ui``) is what makes this
    module a true leaf with no import cycle.
    """
    global _jsonl_cache_getter
    _jsonl_cache_getter = getter


# ── The typed AUQ source ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedAuqSource:
    """The trustworthy AUQ source for a window, plus a parity fingerprint.

    ``kind`` records which priority branch won; ``payload`` is the source
    ``tool_input`` dict for ``side_file``/``jsonl_cache`` and ``None`` for the
    ``pane`` branch (no source dict exists). ``source_fingerprint`` is a stable
    sha over the canonical source representation (per-kind, below). Because the
    resolver is called identically at render/mint and at validate, a consumer
    that records this fingerprint at mint and recomputes it at validate can
    surface a render/tap source mismatch as a MEASURABLE ``source_drift``
    outcome rather than a silent pass.
    """

    kind: Literal["side_file", "jsonl_cache", "pane"]
    payload: dict | None
    source_fingerprint: str


def _canonical_dict_fingerprint(payload: dict) -> str:
    """sha256 over the canonical JSON of a source ``tool_input`` dict.

    Used by the ``side_file`` and ``jsonl_cache`` kinds. Canonical JSON
    (sorted keys, no whitespace) makes the digest stable across dict
    construction order, so the same logical source always fingerprints the
    same and a mutated source dict fingerprints differently.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _pane_fingerprint(pane_text: str) -> str:
    """sha256 over the resolved form's canonical repr for the ``pane`` kind.

    The pane-only case has no source dict, so the resolved FORM is the source
    representation. Hashing ``AskUserQuestionForm._canonical_repr()`` — the
    SAME canonical input ``terminal_parser.fingerprint()`` hashes — keeps the
    pane branch fingerprintable. NOTE: because this shares the canonical input
    with the FORM fingerprint, and ``validate_and_consume`` checks the form
    fingerprint BEFORE the source fingerprint, a changed pane always returns
    ``stale_form`` first; the pane kind can never yield ``source_drift``.
    """
    from ..terminal_parser import resolve_ask_form

    form = resolve_ask_form(None, pane_text) if pane_text else None
    canonical = form._canonical_repr() if form is not None else ""
    return hashlib.sha256(canonical.encode()).hexdigest()


def resolve_auq_source(
    window_id: str,
    explicit: dict | None,
    pane_text: str,
) -> ResolvedAuqSource:
    """Resolve the trustworthy AUQ source for a window, with a parity fingerprint.

    Priority chain (UNCHANGED — formerly the resolver inlined in interactive_ui):

      1. ``side_file``  — a validated ``PreToolAskRecord`` matching the live pane
                          (``resolve_record`` below). ``payload`` = the record's
                          ``tool_input``; fingerprint over its canonical JSON.
      2. ``jsonl_cache`` — else the ``explicit`` JSONL dict if given, else the
                          INJECTED in-process cache. ``payload`` = that dict;
                          fingerprint over its canonical JSON.
      3. ``pane``       — no source dict. ``payload`` = ``None``; fingerprint over
                          the resolved form's ``_canonical_repr()``.

    INVARIANT: render and validate both call THIS function. The returned
    ``source_fingerprint`` is the parity witness a pick-token consumer records
    at mint and recomputes at validate, so source parity is a call-graph fact
    rather than a comment promise. (As of R5 the consumer does not yet record
    it; R4's ``pick_token.validate_and_consume`` wires the mint/validate
    compare.)
    """
    from ..terminal_parser import resolve_ask_form

    pane_form = resolve_ask_form(None, pane_text) if pane_text else None
    pretool_record = resolve_record(window_id, pane_form)
    if pretool_record is not None:
        return ResolvedAuqSource(
            kind="side_file",
            payload=pretool_record.tool_input,
            source_fingerprint=_canonical_dict_fingerprint(pretool_record.tool_input),
        )
    if explicit is not None:
        return ResolvedAuqSource(
            kind="jsonl_cache",
            payload=explicit,
            source_fingerprint=_canonical_dict_fingerprint(explicit),
        )
    cached = _jsonl_cache_getter(window_id)
    if cached is not None:
        return ResolvedAuqSource(
            kind="jsonl_cache",
            payload=cached,
            source_fingerprint=_canonical_dict_fingerprint(cached),
        )
    return ResolvedAuqSource(
        kind="pane",
        payload=None,
        source_fingerprint=_pane_fingerprint(pane_text),
    )


# ── The RENDER-path AUQ source (PR-3 PR-B) ───────────────────────────────────


@dataclass(frozen=True)
class RenderAuqSource:
    """The RENDER-path AUQ source: which source to render from + whether a tap
    on the resulting card can be TRUSTED to dispatch.

    Distinct from :class:`ResolvedAuqSource` (mint/validate parity). The render path
    must handle a BUSY-topic pane that mis-parses or is unparseable while the
    PreToolUse side file holds the real question — WITHOUT ever serving a STALE
    side file over a genuinely different live picker. ``decision`` records the
    branch:

      - ``side_file_ok`` — the side file is consistent with the live pane AND
        within the read-TTL → render from it, mint TRUSTED tokens. The ONLY
        trusted side-file path (mirrors the TTL'd strict resolver that
        ``pick_token.validate_and_consume`` re-resolves at tap, so mint/validate
        parity holds).
      - ``bail`` — the pane is itself a COMPLETE coherent picker that disagrees
        with the side file (a genuinely different / advanced live question) or a
        consistent-but-TTL-aged one → render from the PANE (trusted; never serve
        the stale side file).
      - ``rescue`` — the pane is unparseable / incomplete (busy scrollback) and
        the side file is the truth → render the side file's full content
        DISPLAY-ONLY (``dispatch_trusted=False`` → NO pick tokens, manual-nav
        notice). Read-TTL-FREE so an aged-but-present side file still rescues.
      - ``explicit_jsonl`` / ``jsonl_cache`` / ``pane`` — no side file: the
        pre-existing explicit > jsonl_cache > pane fallback (all trusted).

    ``dispatch_trusted`` GATES token minting at the ``_build_pick_button_rows``
    callsite: a False value means render the card but mint NO ``pick_token`` /
    ``pick_intent`` rows (so a tap can never dispatch an unverified digit).
    ``form`` is the form to render (never None for a decision that renders a
    structured card). ``source_fingerprint`` + ``kind`` feed the trusted mint so
    validate re-resolves the same tags.
    """

    decision: Literal[
        "side_file_ok", "bail", "rescue", "explicit_jsonl", "jsonl_cache", "pane"
    ]
    kind: Literal["side_file", "jsonl_cache", "pane"]
    payload: dict | None
    form: AskUserQuestionForm | None
    source_fingerprint: str
    dispatch_trusted: bool
    reason: str


def pane_form_is_complete_picker(form: AskUserQuestionForm | None) -> bool:
    """True when ``form`` is a COMPLETE, coherent live picker (PR-3 PR-B).

    Used by :func:`resolve_auq_source_for_render` to distinguish a genuinely
    different / advanced live question (the pane shows a full picker → BAIL to
    it) from a busy-scrollback mis-parse / unparseable pane (→ RESCUE from the
    side file). "Complete" = options present, numbered contiguously from 1, AND
    the bottom-of-list affordance row was captured (``options_complete``, which
    proves the whole list is on screen rather than a scrolled tail) — OR a
    review/Submit screen (whose two Submit/Cancel rows are the complete picker).
    Conservative: anything short of that is treated as NOT-complete so the
    resolver prefers the side-file rescue over serving a partial pane.
    """
    if form is None or not form.options:
        return False
    if form.is_review_screen:
        return form.options_contiguous_from_one()
    return form.options_complete


def resolve_auq_source_for_render(
    window_id: str,
    pane_text: str,
    explicit: dict | None = None,
    *,
    ansi_text: str | None = None,
) -> RenderAuqSource:
    """Resolve the RENDER-path AUQ source + trust for a window (PR-3 PR-B).

    Decision tree (side-file-centric; the live AUQ's ``tool_use`` is buffered in
    JSONL, so the PreToolUse side file is the authoritative source):

      record read READ-TTL-FREE (so a busy >TTL pane can still RESCUE):
        - consistent-with-pane AND within the read-TTL → ``side_file_ok``
          (TRUSTED; mirrors the TTL'd strict resolver ``validate_and_consume``
          re-resolves → mint/validate parity, no dead-tap);
        - else, pane is a COMPLETE coherent picker → ``bail`` to the pane
          (trusted; a genuinely different/advanced question, or a
          consistent-but-aged one — never serve the stale side file);
        - else (pane unparseable / incomplete) → ``rescue`` from the side file
          DISPLAY-ONLY (``dispatch_trusted=False``; pure
          ``build_form_from_tool_input`` form so the render identity can't leak
          pane/scrollback churn).
      no side file → the pre-existing explicit > jsonl_cache > pane fallback.

    READ-TTL-FREE only changes the RESCUE liveness — ``side_file_ok`` applies
    the SAME TTL the strict resolver does, so a long-open consistent card flips
    cleanly to ``bail`` (pane) at the TTL boundary instead of stranding a
    trusted side-file token a TTL'd ``validate_and_consume`` would reject. MUST
    NOT mutate ``_pretool_ask_records`` (``resolve_record`` stays its sole
    mutator; ``_read_live_pretool_record`` doesn't write it).
    """
    from ..terminal_parser import build_form_from_tool_input, resolve_ask_form

    # GH #54 capture spine: with ``ansi_text`` present, tier-2 SGR cursor
    # detection works on chevron-less (wrapping-label / 2.1.197) preview panes,
    # so the resolved form's cursor — and thus ``render_signature`` and the
    # poller hash — track an SGR-only cursor move. Plain-only callers pass None
    # and keep today's behaviour byte-identical.
    pane_form = (
        resolve_ask_form(None, pane_text, ansi_text=ansi_text) if pane_text else None
    )
    record = _read_live_pretool_record(window_id, apply_ttl=False)
    if record is not None:
        consistent, reason = _record_consistent_with_pane(record, pane_form)
        # The freshness floor keeps a LIVE side-file card trusted past the read-TTL
        # (the poller refreshes it at every observed-live preserve branch). Folded
        # via ``max(written_at, floor)`` — the on-disk ``written_at`` is unchanged.
        within_ttl = (
            time.time() - max(record.written_at, _freshness_floor(window_id))
        ) <= _PRETOOL_TTL_SECONDS
        if consistent and within_ttl:
            # The side file carries the question TITLE the pane lacks; overlay
            # the live pane (cursor / tab / review) onto it.
            form = resolve_ask_form(record.tool_input, pane_text, ansi_text=ansi_text)
            return RenderAuqSource(
                decision="side_file_ok",
                kind="side_file",
                payload=record.tool_input,
                form=form,
                source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
                dispatch_trusted=True,
                reason="consistent",
            )
        if pane_form is None or (
            not pane_form.options
            and not _zero_option_form_contradicts_record(record, pane_form)
        ):
            # The pane carries NO parseable picker AT ALL (busy scrollback /
            # obscured), OR it parsed a form with ZERO options AND no
            # CONTRADICTING evidence — the same "pane proves nothing" definition
            # ``_record_consistent_with_pane`` uses (``no_pane_form``; auq_source
            # mint/validate parity, GH #54 W2). A 0-option form is exactly the
            # pre-W1 preview-layout shape (F3/F4) and any pane the
            # region-discovery prepass could not parse: without this leg it fell
            # through to the untrusted ``bail_partial`` raw dump instead of the
            # clean side-file RESCUE, dropping the 📋 ctx card and the buttons.
            # Defense-in-depth — it ships even if W1's preview parse slips, and
            # is byte-identical for a normal (options-bearing) pane. NARROWED
            # (wave-2 review P2-B): a zero-option form still carrying a parsed
            # TAB HEADER / current title that CONTRADICTS the (TTL-free, so
            # possibly stale — restart orphan / double-resume sibling) side file
            # is POSITIVE proof a DIFFERENT question is live, so it keeps the
            # pre-W2 ``bail_partial`` instead of rendering the wrong question.
            # The side file is the ONLY content we have. RESCUE it
            # DISPLAY-ONLY. This is the SOLE branch that serves the side file
            # OVER the pane, and it is reserved for "the pane proves nothing",
            # so a (possibly STALE) side file can NEVER overwrite a
            # genuinely-different LIVE picker that the pane DID parse (hermes +
            # internal-review wrong-question fix). PURE side-file form (no pane
            # overlay) → render identity stable under scrollback churn.
            side_form = build_form_from_tool_input(record.tool_input)
            return RenderAuqSource(
                decision="rescue",
                kind="side_file",
                payload=record.tool_input,
                form=side_form,
                source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
                dispatch_trusted=False,
                reason="unparseable_rescue",
            )
        if pane_form_is_complete_picker(pane_form):
            # The pane is itself a COMPLETE live picker — a different/advanced
            # question (inconsistent) or a consistent-but-aged one. BAIL to it
            # (trusted; the whole list is on screen so a tap is safe).
            return RenderAuqSource(
                decision="bail",
                kind="pane",
                payload=None,
                form=pane_form,
                source_fingerprint=_pane_fingerprint(pane_text),
                dispatch_trusted=True,
                reason=("bail_aged" if consistent else f"bail_{reason}"),
            )
        # The pane parses a DIFFERENT, INCOMPLETE picker (scrolled/partial,
        # inconsistent or TTL-aged). Render the PANE display-only — NEVER the
        # stale side file (the wrong-question fix: a parseable live picker, even
        # partial, is the user's real current question). Picks are suppressed
        # (incomplete → unsafe to dispatch); no stale side-file ctx card (bail).
        return RenderAuqSource(
            decision="bail",
            kind="pane",
            payload=None,
            form=pane_form,
            source_fingerprint=_pane_fingerprint(pane_text),
            dispatch_trusted=False,
            reason=f"bail_partial_{reason}",
        )
    # No side file — preserve the existing explicit > jsonl_cache > pane order.
    if explicit is not None:
        return RenderAuqSource(
            decision="explicit_jsonl",
            kind="jsonl_cache",
            payload=explicit,
            form=resolve_ask_form(explicit, pane_text, ansi_text=ansi_text),
            source_fingerprint=_canonical_dict_fingerprint(explicit),
            dispatch_trusted=True,
            reason="explicit",
        )
    cached = _jsonl_cache_getter(window_id)
    if cached is not None:
        return RenderAuqSource(
            decision="jsonl_cache",
            kind="jsonl_cache",
            payload=cached,
            form=resolve_ask_form(cached, pane_text, ansi_text=ansi_text),
            source_fingerprint=_canonical_dict_fingerprint(cached),
            dispatch_trusted=True,
            reason="jsonl_cache",
        )
    return RenderAuqSource(
        decision="pane",
        kind="pane",
        payload=None,
        form=pane_form,
        source_fingerprint=_pane_fingerprint(pane_text),
        dispatch_trusted=True,
        reason="pane",
    )


def render_signature(form: AskUserQuestionForm | None) -> str:
    """Stable signature over ALL render/keyboard-determining form fields (PR-3 PR-B).

    The status-poll loop dedup hashed the raw interactive-content excerpt
    (``ui_content.content``), which CHURNS as unrelated scrollback scrolls under
    a live picker → a fresh re-render every tick → the owner's duplicate-card
    loop. This signature instead covers exactly the fields the renderer +
    keyboard display, so it is STABLE under scrollback churn (a pure side-file
    rescue form has no pane fields at all) yet changes on every REAL transition
    (cursor move, multi-select toggle, tab advance, review screen, complete↔
    incomplete, title change, free-text toggle, tab-inference loss).

    NEVER the cursor-blind pick-token ``fingerprint()`` — that is deliberately
    cursor/selection-blind for token stability, but the renderer DOES paint the
    ``❯`` cursor and ``selected`` glyphs, so a cursor/selection change must
    re-render. This is a SEPARATE render-only signature.
    """
    if form is None:
        return ""
    parts: list[str] = [
        "|".join(
            f"{t.label}:{'A' if t.answered else 'E'}"
            f":{'C' if t.is_current else '_'}:{'S' if t.is_submit else '_'}"
            for t in form.tabs
        ),
        f"FT:{int(form.is_free_text)}",
        f"SEL:{form.select_mode}",
        f"RVW:{int(form.is_review_screen)}",
        f"CMP:{int(form.options_complete)}",
        f"INF:{int(form.current_tab_inferred)}",
        f"NQ:{len(form.questions)}",
        # current_question_title ONLY — NEVER pane_walkback_title. The walkback
        # title is scraped from the line(s) above the option block, which in a
        # BUSY topic is arbitrary churning scrollback; folding it in re-renders
        # the title-less bail/pane card every tick (defeats the loop kill for
        # the dominant live single-select shape). Mirrors _canonical_repr (which
        # excludes pane_walkback_title) and the OLD ui_content.content hash
        # (which hashed only the extracted picker block, never the title region
        # above it) — so this stays STABLE under scrollback churn.
        f"T:{form.current_question_title or ''}",
    ]
    for o in form.options:
        parts.append(
            f"{o.number}|{o.label}|{int(bool(o.cursor))}"
            f"|{o.selected}|{int(bool(o.recommended))}"
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def peek_render_identity(
    window_id: str, pane_text: str, *, ansi_text: str | None = None
) -> str:
    """Render-identity hash for the status-poll loop dedup (PR-3 PR-B).

    Hashes the render DECISION (``decision``/``reason``/``dispatch_trusted``/
    ``kind`` from :func:`resolve_auq_source_for_render`) PLUS
    :func:`render_signature` of the resolved form. Stable under scrollback churn
    (so a long-open rescue / side_file_ok card does not re-render every tick),
    re-renders on every genuine render transition (decision flip, cursor/tab/
    selection/review/title change). Read-only; never mutates resolver state.
    """
    r = resolve_auq_source_for_render(window_id, pane_text, ansi_text=ansi_text)
    head = f"{r.decision}|{r.reason}|{int(r.dispatch_trusted)}|{r.kind}"
    return hashlib.sha256(f"{head}|{render_signature(r.form)}".encode()).hexdigest()


# ── The PreToolUse-hook side-file trust boundary ─────────────────────────────


@dataclass(frozen=True)
class PreToolAskRecord:
    """A PreToolUse-hook AUQ side-file record.

    Carries the structured ``tool_input`` from the AUQ tool_use payload
    PLUS provenance fields (session_id, tool_use_id, written_at,
    input_fingerprint) so the context gate can distinguish this source from
    JSONL-derived cache entries. Acceptance into the cache requires passing
    the projection predicate in ``_record_consistent_with_pane`` — NOT digest
    equality. The fingerprint is only a logging/integrity field.
    """

    tool_input: dict[str, Any]
    session_id: str
    tool_use_id: str  # may be "" if hook payload didn't carry one
    written_at: float
    input_fingerprint: str


# Per-window in-memory cache of accepted PreToolAskRecord. Populated by
# ``resolve_record`` on each gate use; revalidated on every call (no
# stale-serve). Cleared by ``forget_for_window`` when the AUQ resolves.
_pretool_ask_records: dict[str, PreToolAskRecord] = {}

# Per-window in-memory "freshness floor" (window_id → last observed-live WALL
# time). The status poller calls ``refresh_side_file_freshness`` at every
# observed-live preserve branch (the same gating as the pick-token deadline
# refresh), so a LIVE side-file card whose user left it open past the 300s
# read-TTL keeps its trusted ``side_file_ok`` render (and thus its tappable
# option buttons) instead of dropping to an untrusted ``bail_partial``. The
# floor is folded into the TTL age via ``max(record.written_at, floor)`` at the
# two TTL trust-gate sites ONLY — it NEVER mutates the on-disk ``written_at``
# (the prose anchor + the future-skew guard keep the RAW stamp). Deliberately on
# the ``time.time()`` WALL clock (a different clock from the pick-token TTL's
# ``monotonic``), so the two never contaminate each other. Cleared at teardown
# (``forget_for_window``) and ``reset_for_tests``; restart wipes it (an aged
# card then ages normally post-restart — disclosed degradation).
_side_file_freshness: dict[str, float] = {}

_PRETOOL_TTL_SECONDS = 300  # 5 minutes (v4 plan; lowered from v2's 10)
# Codex chunk-3 P1: future-timestamp guard. A side file with
# ``written_at`` far in the future (clock skew, time tamper) would
# otherwise stay valid indefinitely because ``time.time() - written_at``
# is negative and the ``age > TTL`` check passes. Reject anything more
# than this many seconds ahead of the bot's clock.
_PRETOOL_FUTURE_SKEW_SECONDS = 30
_PRETOOL_SCHEMA_VERSION = 1
_PRETOOL_GC_AGE_SECONDS = 3600  # 1h — bot startup cleanup

# Codex chunk-3 P2 (path-traversal defense in depth): require the
# session_id used to construct ``auq_pending/<session_id>.json`` to
# be a canonical UUID. The hook validates this upstream, and the bot
# only resolves session_id via ``session_id_for_window`` which returns
# whatever the session map stored — but defense-in-depth keeps a
# corrupt/maliciously-edited session_map from constructing a side-file
# path outside the pending directory. Use ``fullmatch()`` (codex P3 —
# chunks 3+4) to reject trailing-newline edge cases that ``$`` would
# tolerate.
_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Codex chunk-3 P1: ``input_fingerprint`` is read from the side file
# (UNTRUSTED) and previously logged as-is. A malformed or malicious
# write could inject question text. Recompute the fingerprint from
# the validated tool_input before logging — never trust the stored
# value. Defense-in-depth: also reject anything that isn't a strict
# 12-char hex digest before it enters the log surface.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{12}")


def refresh_side_file_freshness(window_id: str) -> None:
    """Record that ``window_id``'s AUQ side-file card was just observed LIVE.

    Stamps the per-window freshness floor to ``time.time()``. Called by
    ``status_polling`` at the observed-live preserve branches (the same gating as
    the pick-token deadline refresh), so a long-open LIVE side-file card stays on
    the trusted ``side_file_ok`` render path — and keeps its tappable option
    buttons — past the 300s read-TTL. Never touches the on-disk record.
    """
    _side_file_freshness[window_id] = time.time()


def _freshness_floor(window_id: str) -> float:
    """Return the freshness floor (last observed-live wall time) for ``window_id``.

    ``0.0`` when never observed, so ``max(record.written_at, 0.0)`` is a no-op for
    a window the poller never refreshed — the floor only ever WIDENS freshness.
    """
    return _side_file_freshness.get(window_id, 0.0)


def clear_side_file_freshness(window_id: str) -> None:
    """Drop ONLY the in-memory freshness floor for ``window_id`` — never the side file.

    The narrow, side-effect-free teardown for seams that close/unbind a window
    WITHOUT resolving its AUQ (``cleanup.clear_topic_state`` on topic-close /
    window-delete). Distinct from ``forget_for_window``, which ALSO unlinks the
    session-keyed side file — unsafe at topic-close because a double-``--resume``
    sibling window may still hold a LIVE AUQ on the same session, and deleting the
    shared side file would strand the sibling's selection card on pane-only render.
    The floor is in-memory and ``max()``-only-widens, so dropping it here can never
    affect another window's card.
    """
    _side_file_freshness.pop(window_id, None)


def _pretool_side_file_path(session_id: str) -> Path | None:
    """Resolve the side-file path for ``session_id`` after UUID validation.

    Returns ``None`` if ``session_id`` isn't a canonical UUID — defense
    in depth against a corrupt session_map that ever stored e.g. ``../x``
    in the session_id field.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return app_dir() / "auq_pending" / f"{session_id}.json"


def _read_pretool_side_file(session_id: str) -> PreToolAskRecord | None:
    """Read and parse the AUQ PreToolUse side file for ``session_id``.

    Returns ``None`` on missing file (silent — hook hasn't fired yet or
    already cleaned up), invalid session_id (path-traversal defense in
    depth), JSON parse error, schema_version mismatch, or shape mismatch.

    Codex chunk-3 P1 fix: the ``input_fingerprint`` carried in the
    PreToolAskRecord is RECOMPUTED from the validated tool_input — never
    trusted from the file. The stored value could otherwise be poisoned
    by a malformed write and leak through the rejection-reason logs.

    Does NOT validate TTL or pane compatibility — those happen in
    ``resolve_record`` against the live pane.
    """
    path = _pretool_side_file_path(session_id)
    if path is None:
        # session_id failed UUID validation — refuse to construct a
        # path that could escape auq_pending/.
        logger.warning(
            "Pretool side file: refusing to resolve non-UUID session_id=%r",
            session_id,
        )
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Pretool side file unreadable for %s: %s", session_id, e)
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Pretool side file malformed JSON for %s: %s", session_id, e)
        return None
    if not isinstance(rec, dict):
        logger.warning("Pretool side file is not a dict: %s", session_id)
        return None
    if rec.get("schema_version") != _PRETOOL_SCHEMA_VERSION:
        logger.warning(
            "Pretool side file schema_version=%r unknown for %s",
            rec.get("schema_version"),
            session_id,
        )
        return None
    tool_input = rec.get("tool_input")
    if not isinstance(tool_input, dict):
        logger.warning("Pretool side file tool_input invalid for %s", session_id)
        return None
    try:
        written_at = float(rec.get("written_at", 0))
    except (TypeError, ValueError):
        return None

    # Recompute the fingerprint from the validated tool_input — never
    # trust the stored value (codex chunk-3 P1). If pairs extraction
    # fails, the side file is malformed; reject.
    from ..terminal_parser import (
        questions_content_digest,
        questions_content_pairs_from_tool_input,
    )

    pairs = questions_content_pairs_from_tool_input(tool_input)
    if pairs is None:
        logger.warning(
            "Pretool side file tool_input failed shape validation for %s",
            session_id,
        )
        return None
    fingerprint = questions_content_digest(pairs)

    return PreToolAskRecord(
        tool_input=tool_input,
        session_id=str(rec.get("session_id", "") or session_id),
        tool_use_id=str(rec.get("tool_use_id", "") or ""),
        written_at=written_at,
        input_fingerprint=fingerprint,
    )


def _safe_record_labels(question: dict) -> tuple[str, ...] | None:
    """Extract ordered option labels from a tool_input question dict.

    Returns ``None`` on shape mismatch. Mirrors
    ``terminal_parser.questions_content_pairs_from_tool_input`` validation
    so the predicate fails closed on malformed records.
    """
    options = question.get("options")
    if not isinstance(options, list):
        return None
    labels: list[str] = []
    for o in options:
        if not isinstance(o, dict):
            return None
        label = o.get("label")
        if not isinstance(label, str):
            return None
        labels.append(label)
    return tuple(labels)


def _strip_recommended(label: str) -> str:
    """Strip a trailing case-insensitive ``(Recommended)`` suffix from a label.

    The pane parser removes this suffix into the structured ``recommended``
    flag (``terminal_parser._parse_numbered_options``), but the PreToolUse
    side-file label retains it verbatim. Normalizing BOTH sides before a label
    compare keeps a recommended option from spuriously failing the
    pane-consistency check — the di-copilot ``bail_label_mismatch`` false bail
    that dropped the 📋 descriptions card for the SAME question. Confined to the
    recommended suffix ONLY — deliberately NOT lowercasing / whitespace-
    collapsing, which would loosen the wrong-question protection.
    """
    from ..terminal_parser import _RE_RECOMMENDED

    return _RE_RECOMMENDED.sub("", label).rstrip()


def _pane_option_label_matches(option, authority_label: str) -> bool:
    """True iff a pane-parsed option matches the AUTHORITY (side-file / JSONL) label.

    GH #54 W1.2 — the authority-aware comparison for the auq_source label-compare
    call graph (``_record_consistent_with_pane`` via
    ``_pane_labels_match_candidate_by_number`` and the partial-context recovery
    ``_ctx_evidence_floor_ok``). Two legs:

      * leg 1 — the EXISTING single-column rule: ``_strip_recommended`` equality
        (case- and whitespace-PRESERVING, keeping the wrong-question protection
        tight). This is the sole leg for an ordinary option, so single-column
        matching is byte-identical;
      * leg 2 — PREVIEW ONLY (``option.wrap_canonical`` non-empty): a
        preview-layout label is a lossy wrapped-fragment reconstruction whose
        inter-fragment spacing cannot be recovered, so the shared
        ``terminal_parser.label_matches_authority`` accepts either wrap kind via
        an all-whitespace-stripped comparison. Confined to preview options.
    """
    from ..terminal_parser import label_matches_authority

    if _strip_recommended(authority_label) == _strip_recommended(option.label):
        return True
    if not getattr(option, "wrap_canonical", ""):
        return False
    return label_matches_authority(option.label, option.wrap_canonical, authority_label)


def _options_are_subsequence(visible_options, full: tuple[str, ...]) -> bool:
    """True if the visible pane OPTIONS are a contiguous subsequence of ``full``.

    The pane may render only the visible region; earlier options can be pushed
    off the top by long descriptions. We still accept the record if whatever IS
    visible matches the corresponding contiguous slice of the record's labels.
    Each comparison routes through ``_pane_option_label_matches`` (wave-2 review
    P2-C): recommended-suffix-normalized exact equality for ordinary options
    (byte-identical to the historic rule) PLUS the wrap-canonical leg for
    preview options — a wrapped preview label's lossy space-join must not
    ``no_candidate`` the multi-question candidate selection while the
    slot-based accept would take it (mint/validate parity).
    """
    visible = tuple(visible_options)
    if not visible:
        return False
    if len(visible) > len(full):
        return False
    for start in range(len(full) - len(visible) + 1):
        if all(
            _pane_option_label_matches(opt, full[start + i])
            for i, opt in enumerate(visible)
        ):
            return True
    return False


def _labels_are_subsequence(visible: tuple[str, ...], full: tuple[str, ...]) -> bool:
    """Bare-label wrapper over :func:`_options_are_subsequence` (one matcher —
    a label-only caller gets the ordinary exact-equality semantics, since a
    bare label carries no ``wrap_canonical``)."""
    return _options_are_subsequence(
        tuple(SimpleNamespace(label=v) for v in visible), full
    )


# Minimal length an ELLIPSIS-DAMAGED evidence fragment must have before it may
# CONTRADICT the side-file record in ``_zero_option_form_contradicts_record``
# (wave-2 review r3 residual P2). A CC-truncated title/tab strips to a fragment;
# a 2-3 char shred matching nothing proves NOTHING (indeterminate ⇒ rescue, the
# pre-narrowing fail direction), so only a non-trivially-long fragment decides.
# 8 chars — the module's existing ``_CTX_TITLE_MIN_CHARS`` anti-coincidence
# precedent. The floor applies ONLY to damaged (ellipsis-stripped) fragments:
# a CLEAN label is complete evidence and compares at any length (the round-2
# clean-contradiction pins — foreign tabs "Alpha"/"Beta" at 5/4 chars — must
# keep contradicting).
_ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS: Final = 8

# An ellipsis run: the single-char ``…`` (CC's truncation glyph) or ``...``.
_RE_ELLIPSIS_RUN = re.compile(r"…|\.{3,}")


def _ellipsis_fragment(text: str) -> tuple[str, bool]:
    """Split evidence at its FIRST ellipsis run → ``(fragment, was_damaged)``.

    A trailing ``…``/``...`` is stripped; a MID-string ellipsis keeps only the
    pre-ellipsis fragment (the suffix after a truncation marker is not reliable
    evidence — CC renders garbled/re-joined tails around it). ``was_damaged``
    is True iff an ellipsis run was found, which is what scopes the
    ``_ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS`` floor to damaged evidence only.
    """
    m = _RE_ELLIPSIS_RUN.search(text)
    if m is None:
        return text.strip(), False
    return text[: m.start()].strip(), True


def _zero_option_form_contradicts_record(record, pane_form) -> bool:
    """True iff a ZERO-option pane form carries CLEAN POSITIVE evidence a
    DIFFERENT question is live than the side-file record's (wave-2 review P2-B,
    narrowed for damaged evidence by the r3 residual P2).

    The W2 rescue gate treats a zero-option form like ``pane_form is None``
    ("the pane proves nothing"), but the parser deliberately returns zero-option
    forms that still carry a parsed TAB HEADER / current question title during
    redraws — and the render-path side-file read is TTL-free, so a restart
    orphan / double-resume-sibling STALE record could rescue-render old Q1
    content over a pane whose surviving evidence PROVES a different tab /
    question is live. Contradiction = fail back to the pre-W2 ``bail_partial``.

    Contradiction requires CLEAN positive mismatch; DAMAGED / indeterminate
    evidence never contradicts (fail direction: indeterminate ⇒ rescue — the
    pre-narrowing behavior). An ellipsized/garbled variant of the record's OWN
    title (a mid-redraw ``For the Alpha panel, which layout should … primary
    arrangement...``) or a ``…``-truncated tab label is DAMAGE, not
    contradiction: each evidence item is ellipsis-normalized first
    (:func:`_ellipsis_fragment` — strip a trailing run, keep only the
    pre-ellipsis fragment of a mid-string run), a damaged fragment below
    ``_ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS`` is SKIPPED (a shred never
    decides), and the surviving fragment — being a PREFIX of the true text —
    matches through the existing prefix-tolerant rules below.

    Checks (each only when the evidence exists — a bare zero-option form, the
    pure mid-redraw / pre-W1 shape, never contradicts):

      * TITLE — the (normalized) pane ``current_question_title`` fragment must
        bidirectionally prefix-match SOME record question (the
        ``_record_consistent_with_pane`` title rule);
      * TABS — every (normalized) content tab label must match SOME record
        question header under the shared authority-aware normalization
        (``label_matches_authority`` — whitespace-collapse equality plus the
        wrap-canonical all-whitespace-stripped leg, prefix-tolerant for a
        truncated tab render).

    A malformed record (no questions list) never contradicts — the rescue then
    renders whatever the record holds, exactly as for ``pane_form is None``.
    """
    from ..terminal_parser import label_matches_authority

    raw_questions = record.tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return False
    questions = [q for q in raw_questions if isinstance(q, dict)]
    if not questions:
        return False

    pane_title = (pane_form.current_question_title or "").strip()
    if pane_title:
        fragment, damaged = _ellipsis_fragment(pane_title)
        usable = bool(fragment) and (
            not damaged or len(fragment) >= _ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS
        )
        if usable:
            titles = [(q.get("question") or "").strip() for q in questions]
            if not any(
                t and (t.startswith(fragment) or fragment.startswith(t)) for t in titles
            ):
                return True

    content_tabs = [
        t.label.strip() for t in pane_form.tabs if not t.is_submit and t.label.strip()
    ]
    if content_tabs:
        headers: list[str] = []
        for q in questions:
            h = q.get("header")
            if isinstance(h, str) and h.strip():
                headers.append(h.strip())
        if headers:
            for tab in content_tabs:
                fragment, damaged = _ellipsis_fragment(tab)
                if not fragment or (
                    damaged and len(fragment) < _ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS
                ):
                    continue  # damaged shred — indeterminate, never contradicts
                if not any(
                    label_matches_authority(fragment, "", h)
                    or h.lower().startswith(fragment.lower())
                    or fragment.lower().startswith(h.lower())
                    for h in headers
                ):
                    return True
    return False


def _pane_labels_match_candidate_by_number(
    pane_form: AskUserQuestionForm,
    candidate_labels: tuple[str, ...],
) -> bool:
    """True when every visible numbered pane option matches that candidate slot.

    Compressed panes preserve option numbers even when earlier options are
    off-screen, so a visible ``3. Label C`` must match ``candidate_labels[2]`` —
    not just any occurrence of ``Label C``. If a future parser ever emits a
    visible option without a number, fall back to the legacy contiguous
    subsequence check because there is no stable slot to validate against.
    """
    from ..terminal_parser import is_affordance_label

    if any(o.number is None for o in pane_form.options):
        return _options_are_subsequence(pane_form.options, candidate_labels)

    checked_any = False
    for option in pane_form.options:
        assert option.number is not None
        index = option.number - 1
        if 0 <= index < len(candidate_labels):
            if not _pane_option_label_matches(option, candidate_labels[index]):
                return False
            checked_any = True
        else:
            if is_affordance_label(option.label):
                continue
            return False
    return checked_any


def _record_consistent_with_pane(
    record: PreToolAskRecord,
    pane_form: AskUserQuestionForm | None,
) -> tuple[bool, str]:
    """v4 plan step 5: projection-based structural predicate.

    Returns ``(accepted, reason_code)``. On accept: ``(True, "ok")``.
    On reject: ``(False, code)`` where ``code`` is one of:
    ``no_pane_form``, ``no_candidate``, ``title_mismatch``,
    ``label_mismatch``, ``count_sanity``.

    Acceptance is structural — NOT digest equality. We compare projected
    fields one at a time so each edge case (title-missing,
    walkback-only-title, multi-question subset) has a principled answer.
    NEVER computes ``AskUserQuestionForm.fingerprint()`` here — that
    includes cursor/recommended/tab state and would reject valid records.
    """
    if pane_form is None or not pane_form.options:
        return False, "no_pane_form"

    raw_questions = record.tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return False, "no_candidate"

    pane_labels = tuple(o.label for o in pane_form.options)
    pane_title = (pane_form.current_question_title or "").strip()
    candidate: dict | None = None

    # Step 5.a — pick a candidate record-question.
    if len(raw_questions) == 1 and isinstance(raw_questions[0], dict):
        candidate = raw_questions[0]
    elif pane_form.current_tab_inferred and pane_title:
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            qt = (q.get("question") or "").strip()
            if not qt:
                continue
            if qt.startswith(pane_title) or pane_title.startswith(qt):
                candidate = q
                break

    if candidate is None:
        # Multi-question fallthrough (current_tab_inferred=False, or no
        # title match): accept the FIRST question whose labels match the
        # visible labels as a subsequence.
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            q_labels = _safe_record_labels(q)
            if q_labels is None:
                continue
            if _options_are_subsequence(pane_form.options, q_labels):
                candidate = q
                break
    if candidate is None:
        return False, "no_candidate"

    # Step 5.b — TITLE check (conditional).
    # Skip only when:
    #   - pane title empty (including compressed panes where the current
    #     question has scrolled off-screen)
    #   - pane title sourced from walkback only (current_question_title is
    #     empty, pane_walkback_title may be set but is unreliable per the
    #     parser's docstring — DON'T use it for acceptance)
    #   - candidate has no question title
    #
    # Deliberately do NOT require contiguous-from-1 options here: compressed
    # panes can still expose a reliable current_question_title, and accepting
    # a stale side file on labels alone can dispatch the old digit against a
    # different live question. Residual: if the pane title is genuinely
    # unparseable and labels coincidentally match a stale not-overwritten side
    # file within TTL, that edge remains irreducible without question text.
    candidate_title = (candidate.get("question") or "").strip()
    if pane_title and candidate_title:
        if not (
            candidate_title.startswith(pane_title)
            or pane_title.startswith(candidate_title)
        ):
            return False, "title_mismatch"

    # Step 5.c — LABEL check (mandatory).
    candidate_labels = _safe_record_labels(candidate)
    if candidate_labels is None:
        return False, "no_candidate"
    if not _pane_labels_match_candidate_by_number(pane_form, candidate_labels):
        return False, "label_mismatch"

    # Step 5.d — option-count sanity for full match.
    if pane_labels == candidate_labels:
        if not pane_form.options_contiguous_from_one():
            return False, "count_sanity"

    return True, "ok"


def _read_live_pretool_record(
    window_id: str, *, apply_ttl: bool = True
) -> PreToolAskRecord | None:
    """Read the PreToolUse side-file record for ``window_id``, pane-AGNOSTIC.

    Everything ``resolve_record`` does EXCEPT the final
    ``_record_consistent_with_pane`` pane check:
    ``peek_session_id_for_window`` → ``_read_pretool_side_file`` → TTL guard
    (``age > _PRETOOL_TTL_SECONDS``) → future-skew guard
    (``age < -_PRETOOL_FUTURE_SKEW_SECONDS``) → return the ``PreToolAskRecord``,
    else ``None``.

    ``apply_ttl=False`` (the render resolver ``resolve_auq_source_for_render``
    and the ctx recovery ``recover_consistent_side_file_for_ctx``) SKIPS only
    the ``age > _PRETOOL_TTL_SECONDS`` read-TTL block — session-resolve, read,
    and the future-skew guard are KEPT. A long-open card aged past the
    read-TTL must not lose its side-file truth (the item-1 source-drift
    class), but a clock-tampered file is still rejected. (The session-keyed D2
    recovery reader ``read_side_file_for_recovery`` is read-TTL-free too, but
    reads ``_read_pretool_side_file`` directly — it is not a caller here.)

    DELIBERATELY does NOT write ``_pretool_ask_records``: that cache's
    invariant is "consistent-with-pane records only", so ``resolve_record``
    stays its sole mutator. This helper is the pane-agnostic core used by
    both ``resolve_record`` (which then applies the pane check) and
    ``peek_sticky_source`` (which deliberately skips it so a transiently
    degraded pane can't break a source-stickiness pin). Reason codes are
    logged at DEBUG (same as ``resolve_record``); question text is never
    logged.
    """
    # Codex chunk-3 P2: peek (read-only) — never mutate session_manager
    # state by auto-creating a WindowState on miss. session_id_for_window
    # via get_window_state would have that side-effect for unknown windows.
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        logger.debug("Pretool resolve window=%s reason=missing_map", window_id)
        return None

    record = _read_pretool_side_file(session_id)
    if record is None:
        return None

    # Defense-in-depth: only log fingerprints that match the strict
    # hex shape. The reader recomputed it from validated tool_input,
    # so this should always be true, but guard against future drift.
    safe_fp = (
        record.input_fingerprint
        if _FINGERPRINT_RE.fullmatch(record.input_fingerprint)
        else "<invalid>"
    )

    # TTL check + future-skew guard (codex chunk-3 P1). Negative age
    # (timestamp in the future) is rejected to prevent a tampered or
    # clock-skewed file from staying valid indefinitely. The future-skew guard
    # uses the RAW ``age`` (NEVER the floor — a freshness floor must not mask a
    # clock-tampered/future-stamped file); the read-TTL uses a SEPARATE
    # floored ``ttl_age`` so a still-live long-open card stays trusted.
    now = time.time()
    age = now - record.written_at
    if age < -_PRETOOL_FUTURE_SKEW_SECONDS:
        logger.debug(
            "Pretool resolve window=%s reason=future_skew age=%.1fs fp=%s",
            window_id,
            age,
            safe_fp,
        )
        return None
    if apply_ttl:
        ttl_age = now - max(record.written_at, _freshness_floor(window_id))
        if ttl_age > _PRETOOL_TTL_SECONDS:
            logger.debug(
                "Pretool resolve window=%s reason=stale age=%.1fs fp=%s",
                window_id,
                ttl_age,
                safe_fp,
            )
            return None

    return record


def resolve_record(
    window_id: str,
    pane_form: AskUserQuestionForm | None,
) -> PreToolAskRecord | None:
    """Return the PreToolUse side-file record for ``window_id`` if it is
    consistent with the live pane parse, else ``None``.

    The cache invariant is revalidate-on-every-call: a record that no
    longer matches the pane (user navigated, picker advanced, label set
    drifted) MUST be evicted at the next call, not stale-served. This
    keeps wrong-action class bugs out of the cache layer.

    Reason codes for rejection are logged at DEBUG level (not INFO — the
    reader runs on every status-poll iteration when an AUQ is visible,
    and we don't want to flood the log). Question text is NEVER logged
    here; only the reason code + the record's fingerprint.

    The session/read/TTL/skew portion is delegated to
    ``_read_live_pretool_record`` (the pane-agnostic core); this function
    layers on the ``_record_consistent_with_pane`` check and remains the
    SOLE mutator of ``_pretool_ask_records``.
    """
    record = _read_live_pretool_record(window_id)
    if record is None:
        # Missing map, malformed, TTL/skew, or no session — evict any stale
        # cache (matching the pre-refactor behavior on every failure path).
        _pretool_ask_records.pop(window_id, None)
        return None

    # Defense-in-depth: only log fingerprints that match the strict
    # hex shape. The reader recomputed it from validated tool_input,
    # so this should always be true, but guard against future drift.
    safe_fp = (
        record.input_fingerprint
        if _FINGERPRINT_RE.fullmatch(record.input_fingerprint)
        else "<invalid>"
    )

    # Pane-compatibility predicate (revalidated every call).
    consistent, reason = _record_consistent_with_pane(record, pane_form)
    if not consistent:
        logger.debug(
            "Pretool resolve window=%s reason=%s fp=%s",
            window_id,
            reason,
            safe_fp,
        )
        _pretool_ask_records.pop(window_id, None)
        return None

    _pretool_ask_records[window_id] = record
    return record


def peek_sticky_source(
    window_id: str, minted_kind: str, minted_fingerprint: str
) -> dict | None:
    """Return the minted AUQ source's ``tool_input`` IF that exact source is
    still live and UNCHANGED since mint, WITHOUT the pane-consistency check.

    Used by the ``aqt:`` toggle to PIN the source it was minted against, so a
    transient pane degradation that would flip ``resolve_auq_source``
    (side_file → pane) cannot break the toggle. Returns None when the minted
    source is gone, changed (a new question replaced it), or was pane-only.

    Parity: the fingerprint is computed with the SAME
    ``_canonical_dict_fingerprint`` the minter used in ``resolve_auq_source``
    (NOT ``PreToolAskRecord.input_fingerprint``, which is a DIFFERENT digest —
    ``questions_content_digest``). Comparing the wrong digest would silently
    never match (mint/validate source-parity trap).
    """
    if minted_kind == "side_file":
        record = _read_live_pretool_record(window_id)
        if record is not None and (
            _canonical_dict_fingerprint(record.tool_input) == minted_fingerprint
        ):
            return record.tool_input
        return None
    if minted_kind == "jsonl_cache":
        cached = _jsonl_cache_getter(window_id)
        if isinstance(cached, dict) and (
            _canonical_dict_fingerprint(cached) == minted_fingerprint
        ):
            return cached
        return None
    return None


# ── Side-file lifecycle ───────────────────────────────────────────────────────


def unlink_for_session(session_id: str) -> None:
    """Best-effort unlink of the side file for ``session_id``.

    Public helper used by session_monitor when the OLD session_id is known
    at /clear time (the current ``WindowState.session_id`` has already been
    swapped to the new session by then). Silent on missing file / non-UUID
    session_id.
    """
    path = _pretool_side_file_path(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.debug(
            "Pretool side file unlink for session=%s failed: %s",
            session_id,
            e,
        )


def forget_for_window(window_id: str) -> None:
    """Evict the in-memory record for ``window_id`` and unlink its current side file.

    The side-file half of ``interactive_ui.forget_ask_tool_input`` —
    ``interactive_ui`` keeps its own context-marker / pending-claim
    bookkeeping and delegates this part. Resolves the window's CURRENT
    session_id; the /clear race (where the session_id is swapped under us
    with the OLD id) is handled separately by session_monitor via
    ``unlink_for_session`` with the old id.
    """
    _pretool_ask_records.pop(window_id, None)
    # Drop the freshness floor co-located with the side-file cache eviction (the
    # natural teardown — sole caller is ``forget_ask_tool_input`` at AUQ/EPM
    # resolution; covers the /clear race).
    _side_file_freshness.pop(window_id, None)
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        return
    unlink_for_session(session_id)


def side_file_live_for_window(window_id: str) -> bool:
    """Pane-INDEPENDENT "is an AskUserQuestion still live for this window?".

    The lifecycle authority for the card-clear gate. Returns ``True`` iff a
    Thin window-keyed wrapper over :func:`side_file_live_for_session` — it
    resolves the window's session via ``peek_session_id_for_window`` and
    defers all liveness logic to the session-keyed core. Callers that ALREADY
    hold the session_id (e.g. the startup orphan reconciler, which also calls
    ``unlink_for_session``) MUST use :func:`side_file_live_for_session`
    directly so the liveness check and the unlink act on the SAME session —
    checking one source (peek → window_states) and acting on another
    (a session_id from elsewhere) is the mint/validate parity trap.

    Returns ``True`` iff a schema-valid PreToolUse side file exists for the
    window's bound session and its ``written_at`` is not implausibly in the
    future.

    Deliberately UNLIKE ``resolve_record`` in two load-bearing ways:

      1. It does NOT run ``_record_consistent_with_pane``. The whole point
         is that the visible pane may be obstructed (a Claude task-list
         overlay, a scrolled/compressed multi-step Submit screen,
         tool-output spam) while the question is genuinely pending on the
         Claude side. Requiring pane consistency here would re-create the
         exact bug this guards against — a live card torn down because its
         anchors scrolled out of the captured pane.

      2. It does NOT apply the ``_PRETOOL_TTL_SECONDS`` read-TTL. That TTL
         bounds *stale-render* risk on the resolve path; it must NOT bound
         *card liveness*. A question left unanswered longer than the TTL
         has NOT "expired on the other side of the bridge" — it is still
         waiting on the user. Only the future-skew guard is applied
         (rejects clock-tampered timestamps). Orphans are bounded by the
         real unlink paths (``forget_for_window`` on tool_result,
         ``unlink_for_session`` on /clear & window-delete) and the 1h
         startup ``gc_stale`` — never by a clear-gate timer.

    Strictly higher authority than the visible-pane liveness predicates,
    per the RouteRuntime contract that pane snapshots are reconciliation
    events of LOWER authority than the resolution lifecycle.

    Read-only: must NOT mutate ``_pretool_ask_records`` (``resolve_record``
    stays the sole cache mutator); a pure probe used by ``status_polling``'s
    pane-absent clear gate to refuse tombstoning a still-live card.

    Off-contract limitation: the side file is keyed by *session*, not
    *window*. Under 1 topic = 1 window = 1 session this is exact. If two
    windows are bound to one session (only reachable by double-``--resume``
    of the same session into two topics), a live AUQ on one window keeps
    this ``True`` for its sibling, so a *dead* card on the sibling lingers
    until the tool_result fan-out, a window switch, a topic close, or the
    1h GC clears it. A ``tool_use_id`` correlation would NOT help here: the
    JSONL ``tool_use`` (hence ``interactive_ui._last_auq_tool_use_id``) is
    buffered until the answer, and the side file's ``tool_use_id`` "may be
    ''", so both are typically unavailable during the live window. A
    schema-v2 side file capturing the hook-side ``window_id`` (the
    SessionStart hook already resolves it via ``TMUX_PANE``) COULD
    discriminate and is the natural fix if double-resume ever becomes
    supported; it is deferred here as off-contract. The bounded dead-card
    linger is accepted for now.
    """
    return side_file_live_for_session(peek_session_id_for_window(window_id) or "")


def side_file_live_for_session(session_id: str) -> bool:
    """Pane-INDEPENDENT AUQ liveness keyed by SESSION.

    The session-keyed core of :func:`side_file_live_for_window`. The side file
    is itself keyed by session (``auq_pending/<session_id>.json``), so this is
    the canonical form. Returns ``True`` iff a schema-valid side file exists
    for ``session_id`` and its ``written_at`` is not implausibly in the future
    — DELIBERATELY without the ``_PRETOOL_TTL_SECONDS`` read-TTL and without
    the pane-consistency check (see :func:`side_file_live_for_window` for the
    full rationale).

    Read-only: must NOT mutate ``_pretool_ask_records``. Use this (not the
    window wrapper) wherever the caller already holds the session_id and will
    act on it (e.g. pairing the check with ``unlink_for_session(session_id)``),
    so the check and the action target the same session.
    """
    if not session_id:
        return False
    record = _read_pretool_side_file(session_id)
    if record is None:
        return False
    # Future-skew guard ONLY (NOT the read-TTL): a side file written
    # implausibly far in the future (clock tamper) is rejected; an
    # old-but-unanswered AUQ is STILL live.
    if time.time() - record.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return False
    return True


def peek_side_file_tool_use_id(session_id: str) -> str | None:
    """Return the side file's captured ``tool_use_id`` for ``session_id``.

    Thin public accessor over the private side-file reader so callers (the
    startup positive-proof reconciler in ``session_monitor``) need not import
    ``_read_pretool_side_file``. Returns ``None`` when no valid side file exists,
    or the captured ``tool_use_id`` (which "may be ''" if the hook payload didn't
    carry one) when it does. Read-only; pane-AGNOSTIC; no TTL.
    """
    rec = _read_pretool_side_file(session_id)
    return rec.tool_use_id if rec else None


def peek_side_file_written_at(session_id: str) -> float | None:
    """Return the side file's ``written_at`` for ``session_id`` — the PreToolUse
    hook's stamp of the AUQ tool_use INVOCATION instant.

    The AUQ prose-ordering anchor (PR-1): the turn's prose finalizes just before
    the tool_use, so ``final_at <= written_at + EPS`` pairs the prose to THIS
    picker even when the poller detected it long after the TTL aged out. Mirrors
    :func:`peek_side_file_tool_use_id` — a thin public accessor over the private
    reader, read-only, pane-AGNOSTIC, no read-TTL. Returns ``None`` when no valid
    side file exists or its ``written_at`` is implausibly in the future (the same
    future-skew guard the liveness predicate applies — a tampered/skewed stamp
    must not widen the freshness window)."""
    rec = _read_pretool_side_file(session_id)
    if rec is None:
        return None
    if time.time() - rec.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return None
    return rec.written_at


def peek_side_file_tool_use_id_and_written_at(
    session_id: str,
) -> tuple[str, float] | None:
    """Return ``(tool_use_id, written_at)`` from the side file for ``session_id``
    in ONE read, or ``None`` when no valid side file exists.

    The combined accessor the GH #39 empty-id orphan age-cap needs: the reconciler
    decides on ``(tool_use_id, written_at)`` and re-reads the SAME tuple as its
    pre-unlink guard, so both the decision and the guard observe ONE atomic record
    read — an empty id + an exact ``written_at`` match discriminates two empty-id
    generations that the ``tool_use_id`` re-peek alone cannot. Applies the same
    future-skew guard as :func:`peek_side_file_written_at` (a skewed stamp ⇒
    ``None`` ⇒ never aged past the cap). Read-only; pane-AGNOSTIC; no read-TTL.
    """
    rec = _read_pretool_side_file(session_id)
    if rec is None:
        return None
    if time.time() - rec.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return None
    return rec.tool_use_id, rec.written_at


@dataclass(frozen=True)
class SurfaceAnchor:
    """The AUQ occurrence anchor: its KEY **and the record's own content**.

    The key alone is not enough. It is read OUT-OF-BAND, so nothing tied it to
    the pane the caller then captured — and the argument that made that safe ("a
    live prompt means Claude is BLOCKED on it, so the side file must be its")
    silently assumed that a RESOLVED prompt is no longer live-LOOKING on the
    pane. Carrying the record's ``tool_input`` lets the caller BIND the anchor to
    the pane it captured (:func:`anchor_pane_agreement` — the OPTION LABELS)
    instead of betting on read order.

    Both fields come from ONE atomic side-file read, so the key and the content
    can never describe two different records.
    """

    key: str
    tool_input: dict[str, Any]


ANCHOR_MATCH: Final = "match"
ANCHOR_MISMATCH: Final = "mismatch"
ANCHOR_INDETERMINATE: Final = "indeterminate"


def anchor_pane_agreement(
    tool_input: dict[str, Any],
    pane_text: str,
    *,
    target_row: int,
) -> str:
    """Does the hook RECORD describe the card the PANE is showing?

    ``ANCHOR_MATCH`` / ``ANCHOR_MISMATCH`` / ``ANCHOR_INDETERMINATE``.

    WHAT THIS BUYS. The free-text executor reads the anchor out-of-band and
    captures the pane separately, so it can mint a CHIMERA — ``(card A's pane,
    card B's anchor)`` — whenever the side file has already moved to B while the
    pane still renders A (``PreToolUse`` writes B's record BEFORE B draws, so a
    just-answered A can still be on screen). Read order alone cannot close that,
    so the record's CONTENT is bound to the pane it is paired with: a record whose
    OPTION LABELS are not the pane's labels is REFUSED, whatever the read order
    was.

    TARGET-ROW-BLIND, for the same reason the pane identity is: the executor
    types into row ``target_row``, and a typed affordance row parses as a FOURTH
    REAL OPTION. Only the real prefix ``1..target_row-1`` — the part the
    transaction never touches — is compared, so the agreement is stable across
    the executor's own mutations.

    ``INDETERMINATE`` = the pane carries no complete real-option prefix (the
    overflow shape: a long answer scrolls the block off the top of an alternate
    screen). The caller then has no pane component either, and the anchor stands
    alone — the documented, rig-measured AUQ overflow case. It is NOT a licence:
    the caller still requires the anchor KEY to be unchanged.

    **THE LABEL COMPARISON IS THE WHOLE CONTENT BINDING, AND ITS LIMIT IS AN
    ACCEPTED RESIDUAL (owner decision 2026-07-12).** ``_record_consistent_with_pane``
    rejects a record whose option labels differ from the pane (``label_mismatch``)
    and does NOT reject one whose labels are IDENTICAL but whose QUESTION differs
    — a pure-pane parse yields ``current_question_title is None`` and empty option
    descriptions, so the labels are the only pane-observable content it has. An
    earlier revision therefore grew a question-text binding (a pane
    question-REGION extractor, a measured wrap column, and a row-consumption
    walk). **It was DELETED.** It failed three straight review rounds on its own
    injectivity, and the hazard it defended against was over-scoped: a wrong card
    CANNOT receive an OPTION COMMIT, because the free-text lane's PRE-TYPE LANDING
    PROOF (``free_text``: cursor + SGR-2 dim + the exact ``Type something.``
    label) is satisfied by exactly one shape — the selected, UNTYPED placeholder —
    and a real option row is never dim. The worst reachable outcome is that the
    prose answer reaches a DIFFERENT QUESTION (a same-labelled successor's
    free-text row): a recoverable annoyance the user sees immediately, not a
    security event. Disclosed in ``handlers/free_text``'s module docstring and in
    ``.claude/rules/message-handling.md``.
    """
    from ..terminal_parser import resolve_ask_form

    if not pane_text or target_row < 2:
        return ANCHOR_INDETERMINATE
    pane_form = resolve_ask_form(None, pane_text)
    if pane_form is None or not pane_form.options:
        return ANCHOR_INDETERMINATE
    real = tuple(
        o for o in pane_form.options if o.number is not None and o.number < target_row
    )
    if [o.number for o in real] != list(range(1, target_row)):
        # No complete real-option prefix on this pane ⇒ nothing to bind against.
        return ANCHOR_INDETERMINATE

    truncated = dataclasses.replace(pane_form, options=real)
    probe = PreToolAskRecord(
        tool_input=tool_input,
        session_id="",
        tool_use_id="",
        written_at=0.0,
        input_fingerprint="",
    )
    consistent, reason = _record_consistent_with_pane(probe, truncated)
    if not consistent:
        logger.info(
            "AUQ anchor/pane DISAGREE reason=%s — the PreToolUse record does not "
            "describe the card on the pane",
            reason,
        )
        return ANCHOR_MISMATCH
    return ANCHOR_MATCH


def peek_surface_anchor_for_window(window_id: str) -> SurfaceAnchor | None:
    """The AUQ occurrence anchor — key **and content** — from ONE atomic read.

    The KEY is exactly what :func:`peek_surface_identity_for_window` returns (all
    of the round-3 / round-4 contract documented there — the freshly-resolved
    session generation, the mandatory ``tool_use_id``, the future-skew guard, no
    read-TTL). The CONTENT is the SAME record's ``tool_input``, so the caller can
    bind it to the pane it captures (:func:`anchor_pane_agreement`) instead of
    trusting read order alone (round-5 P1-B).

    One read, so the key and the content can never name two different cards.
    """
    session_id = read_session_id_for_window_fresh(window_id)
    if not session_id:
        return None
    record = _read_pretool_side_file(session_id)
    if record is None:
        return None
    if time.time() - record.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return None
    if not record.tool_use_id:
        logger.warning(
            "AUQ anchor read window=%s reason=no_tool_use_id — the PreToolUse "
            "record carries no occurrence witness; the free-text lane DECLINES",
            window_id,
        )
        return None
    if not isinstance(record.tool_input, dict):
        return None
    return SurfaceAnchor(
        key=f"auq:sid:{session_id}:tu:{record.tool_use_id}",
        tool_input=record.tool_input,
    )


def peek_surface_identity_for_window(window_id: str) -> str | None:
    """The AUQ SURFACE-OCCURRENCE identity for a window's live side file.

    The OUT-OF-BAND anchor the GH #50 PR-2 free-text executor re-checks after its
    navigation and again immediately before the Enter (peer-review P1). It is
    independent of the pane, so it survives the one shape where the pane-derived
    identity cannot: a long answer that scrolls the picker's whole option block
    off the top (the AUQ picker is bottom-anchored, and a TUI has no scrollback).

    Identity, NOT liveness — but the two coincide where it matters: the side file
    is written by the ``PreToolUse`` hook BEFORE each AUQ renders and unlinked at
    its ``tool_result``, so a card that resolved-and-was-replaced yields a
    DIFFERENT id (the successor's) or ``None`` (resolved, nothing live) — both of
    which the executor refuses on.

    **THE SESSION GENERATION IS PART OF THE ANCHOR (round-4 P1).** The window's
    session is resolved through ``session.read_session_id_for_window_fresh`` —
    the hook-written ``session_map.json``, never the CACHED
    ``WindowState.session_id``, which only mirrors that map as often as the
    monitor's poll loop reloads it. A ``/clear`` (or any session replacement) in
    the SAME tmux window rotates the session while the cache still names the old
    one, so every anchor read resolved the PREDECESSOR's side file while the pane
    being captured was the SUCCESSOR's card: three observations in perfect
    agreement with each other and with nothing real, and the Enter commits onto
    the wrong card. Embedding the freshly-resolved session id in the key means a
    rotation between two observation points CHANGES the anchor and
    ``SurfaceIdentity.still_holds`` refuses. (This lane's record carries no
    ``window_key``, so the double-``--resume`` sibling residual documented
    elsewhere is unchanged — the fix here is the session GENERATION, not the
    window.)

    **AN EMPTY ``tool_use_id`` DECLINES (round-4 P2).** ``hook.py`` writes ``""``
    when the payload carries no id, and the old ``(written_at, canonical
    fingerprint)`` composite silently upgraded that absence into an ALLOW-capable
    anchor. A wall-clock stamp plus a content hash is not an occurrence witness:
    same-session siblings can share a clock quantum, and (there being no
    read-TTL) a stale record stays "valid" forever. On the only licensed CC
    version the rig confirms the id is always present. **Scoped to the free-text
    ANCHOR path only** — the GH #48 recap surface-identity lane builds its own
    composite from ``read_side_file_for_recovery`` and is untouched, because a
    guessy identity there costs a duplicate recap, not a wrong keystroke.

    Same future-skew guard as :func:`side_file_live_for_session`; deliberately NO
    read-TTL (a card left open for hours is still that card). Read-only — never
    mutates the record cache.

    ``None`` when the window has no session in the fresh map, no side file, a
    clock-skewed one, or one with no occurrence witness — the caller FAILS CLOSED
    on ``None`` wherever it had an id before.
    """
    session_id = read_session_id_for_window_fresh(window_id)
    if not session_id:
        return None
    record = _read_pretool_side_file(session_id)
    if record is None:
        return None
    if time.time() - record.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return None
    if not record.tool_use_id:
        logger.warning(
            "AUQ anchor read window=%s reason=no_tool_use_id — the PreToolUse "
            "record carries no occurrence witness; the free-text lane DECLINES",
            window_id,
        )
        return None
    return f"auq:sid:{session_id}:tu:{record.tool_use_id}"


@dataclass(frozen=True)
class RecoverySideFile:
    """The read-TTL-free side-file payload + its canonical source fingerprint.

    ``source_fingerprint`` is ``_canonical_dict_fingerprint(payload)`` — the SAME
    digest :func:`resolve_auq_source` stores as the ``side_file`` kind's
    ``source_fingerprint`` (NOT ``PreToolAskRecord.input_fingerprint``, a
    different 12-hex questions-content digest). ``payload`` is the side file's
    ``tool_input`` so recovery can reconstruct the FULL-options form (matching the
    minted fingerprint even when the live pane is compressed). ``tool_use_id``
    and ``written_at`` come from that same validated file read, allowing callers
    to derive an occurrence identity without a torn second read.
    """

    payload: dict
    source_fingerprint: str
    tool_use_id: str
    written_at: float


def read_side_file_for_recovery(session_id: str) -> RecoverySideFile | None:
    """Read the side file for D2 restart-recovery, read-TTL-free + pane-agnostic.

    Returns the validated ``tool_input`` payload + its canonical source
    fingerprint, or ``None`` if the side file is absent / invalid.

    DELIBERATELY bypasses the 300s ``_PRETOOL_TTL_SECONDS`` read-TTL and the
    pane-consistency (``resolve_record``) demotion: D2 targets a card a user may
    have left open for hours (well past the read-TTL), and recovery compares the
    canonical digest DIRECTLY to the stored ``source_fingerprint`` and rebuilds
    the full form from ``payload`` — rather than re-resolving through the
    read-TTL'd / pane-demoting resolver, which would falsely report a long-idle
    side file as ``pane`` and wrongly decline.
    """
    record = _read_pretool_side_file(session_id)
    if record is None:
        return None
    return RecoverySideFile(
        payload=record.tool_input,
        source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
        tool_use_id=record.tool_use_id,
        written_at=record.written_at,
    )


_CTX_EVIDENCE_MIN_NUMBERED_MATCHES = (
    2  # defeats a SINGLE coincidental label (round-2 P1b);
)
#                                         LOAD-BEARING — recovers the DiCopilot 3-option case
#                                         (2 surviving matches). N>=3 re-breaks the bug. Do not
#                                         change without re-confirming the DiCopilot positive.
_CTX_TITLE_MIN_CHARS = 8  # GENUINE absolute floor — NOT min(8, len(shorter)),
#                           which is tautological vs `len(shorter) >= threshold`
#                           (see terminal_parser._strong_match:2501-2512). A fixed
#                           literal: a sub-8-char coincidental pane title can NEVER
#                           clear Leg A; only genuine >=8-char corroboration does.


def _ctx_recovery_candidate(record, pane_form):
    """Read-only re-derivation of the SAME candidate question
    _record_consistent_with_pane chose (auq_source.py:808-834), so the floor
    scores against the MATCHED question. FAIL-CLOSED SUBSET of the resolver's
    candidate-pick (single-Q -> q0; tab-inferred+title -> bidirectional-startswith
    title match; else subsequence-first fallthrough) WITHOUT mutating anything.
    Returns the dict or None.

    NOTE (round-3 P3, candidate-parity is a SUBSET, not exact): this helper's
    fallthrough picks the first question whose labels are a CONTIGUOUS
    SUBSEQUENCE of the visible pane labels (_labels_are_subsequence, mirroring
    _record_consistent_with_pane:822-834). But the resolver's FINAL accept
    (_pane_labels_match_candidate_by_number, auq_source.py:745-777) is
    numbered-SLOT based, which can ACCEPT a gappy-but-slot-consistent partial
    pane that this subsequence picker REJECTS. So the helper recovers a STRICT
    SUBSET of the resolver's acceptances (fail-closed: it can only return a
    candidate the resolver also accepts, never a candidate the resolver would
    reject -> never a wrong card; it may decline a card the resolver-loose path
    would have allowed -> a bounded, safe false-negative). The parity unit
    asserts SUBSET (helper-accepts => resolver-accepts), NOT exact equality.

    Intentionally DUPLICATES the resolver's pick (~15 read-only LoC) instead of
    refactoring _record_consistent_with_pane to surface its candidate — that
    would touch a function the plan forbids changing. A shared
    `_pick_candidate_question` is a cleaner follow-up ONLY if a reviewer insists
    (resolver-touching -> out of scope here)."""
    raw = record.tool_input.get("questions")
    if not isinstance(raw, list) or not raw:
        return None
    pane_title = (pane_form.current_question_title or "").strip()
    if len(raw) == 1 and isinstance(raw[0], dict):
        return raw[0]
    if pane_form.current_tab_inferred and pane_title:
        for q in raw:
            if isinstance(q, dict):
                qt = (q.get("question") or "").strip()
                if qt and (qt.startswith(pane_title) or pane_title.startswith(qt)):
                    return q
    for q in raw:
        if isinstance(q, dict):
            ql = _safe_record_labels(q)
            if ql is not None and _options_are_subsequence(pane_form.options, ql):
                return q
    return None


def _ctx_evidence_floor_ok(record, pane_form):
    """Helper-LOCAL anti-coincidence floor (round-2 P1b). Run AFTER
    _record_consistent_with_pane returns True. Requires EITHER a reliable
    current_question_title substring match >= _CTX_TITLE_MIN_CHARS chars (NEVER
    pane_walkback_title) OR >= _CTX_EVIDENCE_MIN_NUMBERED_MATCHES distinct
    non-affordance NUMBERED visible options matching the candidate by slot.
    Read-only; never mutates _pretool_ask_records."""
    from ..terminal_parser import is_affordance_label

    candidate = _ctx_recovery_candidate(record, pane_form)
    if candidate is None:
        return False
    candidate_labels = _safe_record_labels(candidate)
    if candidate_labels is None:
        return False

    # Leg A — reliable title corroboration (current_question_title ONLY).
    # GENUINE fixed-8 floor (round-2 P2): NOT min(8, len(shorter)).
    pane_title = (pane_form.current_question_title or "").strip().lower()
    cand_title = (candidate.get("question") or "").strip().lower()
    if pane_title and cand_title:
        shorter = min(pane_title, cand_title, key=len)
        if len(shorter) >= _CTX_TITLE_MIN_CHARS and (
            pane_title in cand_title or cand_title in pane_title
        ):
            return True

    # Leg B — >= N distinct non-affordance NUMBERED slot-matches.
    matched_slots: set[int] = set()
    for o in pane_form.options:
        if o.number is None:
            return False  # no stable slot to score → Leg B cannot run; fail closed
        if is_affordance_label(o.label):
            continue
        idx = o.number - 1
        if 0 <= idx < len(candidate_labels) and _pane_option_label_matches(
            o, candidate_labels[idx]
        ):
            matched_slots.add(o.number)
    return len(matched_slots) >= _CTX_EVIDENCE_MIN_NUMBERED_MATCHES


def recover_consistent_side_file_for_ctx(window_id, pane_text):
    """Read-TTL-free side-file payload for the CTX card ONLY on a consistent
    PARTIAL-pane bail (the long-open busy-topic card whose top options scrolled
    off the pane). Returns None for: a complete-picker bail (the pane is the
    user's real, genuinely-different/advanced live question — the side file
    would be the WRONG question), an inconsistent partial bail, a coincidental
    single-label match that fails the evidence floor (round-2 P1b), pane_form is
    None (the rescue path owns that), or no side file. Never mutates
    _pretool_ask_records (read-only; resolve_record stays the sole mutator)."""
    from ..terminal_parser import resolve_ask_form

    pane_form = resolve_ask_form(None, pane_text) if pane_text else None
    if pane_form is None or pane_form_is_complete_picker(pane_form):
        return None  # rescue path / complete-picker (trusted) bail handled elsewhere
    record = _read_live_pretool_record(window_id, apply_ttl=False)
    if record is None:
        return None
    consistent, _reason = _record_consistent_with_pane(record, pane_form)
    if not consistent:
        return None  # inconsistent partial bail -> wrong question -> no ctx card
    if not _ctx_evidence_floor_ok(record, pane_form):  # round-2 P1b floor
        return None
    return RecoverySideFile(
        payload=record.tool_input,
        source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
        tool_use_id=record.tool_use_id,
        written_at=record.written_at,
    )


def gc_stale(*, is_live_session: Callable[[str], bool] | None = None) -> int:
    """Delete AUQ side files older than ``_PRETOOL_GC_AGE_SECONDS``.

    Best-effort. Called on bot startup. Returns the number of files
    deleted (useful for tests; the bot's startup log doesn't need it).
    Anything older than 1h is presumed stale — TTL on the read path is
    only 5min, so a 1h file definitely cannot be served. Crashes /
    kickstart-between-AUQs are the typical sources of these orphans.

    LIVENESS GATE (mirrors ``md_capture.gc_stale``): Claude BUFFERS the
    AskUserQuestion tool_use in JSONL until the prompt resolves, so a
    genuinely-live AUQ left open >1h has a stale-mtime side file that is
    STILL the card's liveness authority — reaping it on age alone would
    strand the live card. When ``is_live_session`` is supplied, it is called
    with the file STEM (= the ``<session_id>``) after the age test passes:
    True → SKIP (keep the live side file); an EXCEPTION → conservative SKIP
    (never delete on uncertainty; the raise is caught around the predicate
    call only so the rest of the pass continues). The predicate is INJECTED
    — ``auq_source`` stays a leaf and never imports a session module to learn
    liveness.
    """
    pending_dir = app_dir() / "auq_pending"
    if not pending_dir.is_dir():
        return 0
    cutoff = time.time() - _PRETOOL_GC_AGE_SECONDS
    deleted = 0
    try:
        entries = list(pending_dir.iterdir())
    except OSError as e:
        logger.warning("Pretool GC: iterdir on %s failed: %s", pending_dir, e)
        return 0
    for entry in entries:
        # Skip non-regular files; reject anything that doesn't match the
        # canonical "<uuid>.json" name to avoid touching unexpected files.
        if not entry.is_file():
            continue
        if not entry.name.endswith(".json"):
            continue
        stem = entry.stem
        if not _SESSION_ID_RE.fullmatch(stem):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if is_live_session is not None:
            try:
                if is_live_session(stem):
                    continue  # live AUQ — keep its side file
            except Exception:
                # Never delete on uncertainty; the predicate raising must not
                # abort the whole GC pass either.
                continue
        # Codex P2 (chunk 5): re-check mtime just before unlink. The
        # hook may have replaced this side file (atomic temp+rename)
        # between our initial stat and now; if so, skip — deleting a
        # fresh file would force fallback to labels-only for the
        # next AUQ on this session.
        try:
            current_mtime = entry.stat().st_mtime
        except OSError:
            continue
        if current_mtime >= cutoff:
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError as e:
            logger.debug("Pretool GC: unlink %s failed: %s", entry, e)
    if deleted:
        logger.info("Pretool GC: deleted %d stale side file(s)", deleted)
    return deleted


def reset_for_tests() -> None:
    """Test-only: clear the pretool record cache AND reset the injected getter.

    Rebinds ``_jsonl_cache_getter`` back to the no-op default so a test that
    re-points the getter to a fake cache cannot leak that behavior into the
    next test (the next test re-injects what it needs).
    """
    global _jsonl_cache_getter
    _pretool_ask_records.clear()
    _side_file_freshness.clear()
    _jsonl_cache_getter = lambda _window_id: None  # noqa: E731
