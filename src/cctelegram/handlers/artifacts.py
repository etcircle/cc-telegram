"""Artifact delivery lane — detect deliverable file paths + 📎 tap-to-download cards.

When Claude's parent-route assistant prose mentions local files with deliverable
extensions (``report.md``, ``chart.png``, ``export.pdf``, …), the bot posts a
compact "📎" follow-up card with one inline button per file; a tap uploads that
file to the topic as a Telegram document. This leaf owns the pure primitives:

  - ``extract_artifact_candidates`` — string-level path extraction from prose.
  - ``resolve_artifacts`` / ``resolve_single`` — filesystem validation
    (expanduser → cwd-join → resolve → containment under the resolved allowed
    roots → regular-file + size cap). Fail-closed on empty cwd / traversal /
    symlink-escape. A RELATIVE candidate that misses under the session cwd
    ALSO retries against the worktree's main repo root when the resolved cwd
    carries the harness ``.claude/worktrees`` shape (a handoff written to
    ``<main_repo>/temp/…`` from a ``<main_repo>/.claude/worktrees/<name>``
    session); the cwd hit always wins, and the hit is pinned + displayed
    relative to whichever root matched. NOTE: only the harness
    ``.claude/worktrees`` layout is covered — a general ``git worktree add``
    elsewhere is NOT (no shared string shape to derive the main root from).
  - ``open_validated_artifact`` — the TOCTOU-closing OPEN helper: re-check
    containment against the roots PINNED at mint time, ``O_RDONLY|O_NOFOLLOW``
    open, ``fstat`` regular-file + size ON THE FD, return the open file object
    (the pathname is never re-opened by the caller).
  - the in-memory ``dlf:`` token registry (single-FLIGHT, not single-use — a
    re-tap re-uploads) + the (route, resolved_path) offer-dedup map.

Leaf rules: stdlib + ``callback_data`` helpers only — NEVER imports ``config``
(values are injected at callsites) or ``telegram`` (the executor wraps the
plain ``(label, callback_data)`` rows into an ``InlineKeyboardMarkup``). The
registry is in-memory only (restart wipes it — a dead button answers a graceful
expired modal; the prose above the card names the file(s) so ``/file`` is the
restart net — the card BODY is pathless, see ``card_text``),
NOT a route_runtime field, registers no observers (c313657 stays forbidden).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .callback_data import CB_DOWNLOAD_FILE, checked_callback_data

logger = logging.getLogger(__name__)

# ── Extension allowlist (extraction-time filter; §A.1) ────────────────────
# Every DELIVERABLE file type should be tap-to-downloadable (owner 2026-07-10),
# so this allowlist covers docs, images, audio, video, archives, and office/data
# formats. Source-code extensions stay EXCLUDED — the anti-spam core: tool output
# is full of incidental .py/.ts/.rs paths, and code is edited in place, not
# "delivered". The binary/media/archive types here are rarely mentioned
# incidentally in assistant prose, and resolution ALREADY requires the path to
# exist under an allowed root within the size cap (`resolve_artifacts`), so
# widening the extraction set cannot offer a file that isn't actually there.
# The ``/file`` escape hatch is NOT ext-gated (an explicit request can fetch any
# file type under the roots), so this allowlist governs ONLY the auto-offered
# card path.
ARTIFACT_EXTS: frozenset[str] = frozenset(
    {
        # docs / text
        "md",
        "pdf",
        "html",
        "htm",
        "txt",
        "log",
        "rtf",
        "epub",
        # images
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "svg",
        "bmp",
        "tiff",
        "ico",
        # data / spreadsheets
        "csv",
        "tsv",
        "json",
        "parquet",
        # office
        "xlsx",
        "xls",
        "docx",
        "doc",
        "pptx",
        "ppt",
        "odt",
        "ods",
        # audio
        "wav",
        "mp3",
        "m4a",
        "aac",
        "flac",
        "ogg",
        "opus",
        # video
        "mp4",
        "mov",
        "mkv",
        "webm",
        "avi",
        # archives
        "zip",
        "tar",
        "gz",
        "tgz",
        "bz2",
        "7z",
        "rar",
    }
)

# A path-like token ending in an allowlisted extension. Tolerant of a leading
# ``~``/``.``/``..`` + optional slash (absolute, ``~/…``, ``./…``, bare
# relative), and bounded so a longer surrounding token (``report.mdx``,
# ``a/b.jsonl``) never matches. Backticks / parens / markdown-link ``(…)``
# targets / trailing sentence punctuation all fall outside the char class, so
# they are stripped naturally by the boundaries.
#
# RIGHT boundary (fold item 1 — hermes P1 / codex P2-1, converged): the matched
# extension must genuinely END the token — reject when the next characters
# CONTINUE it: any word char (the ``.mdx`` guard), ``/`` or ``~`` (a deeper
# path / editor-backup continuation: ``foo.pdf/assets``), or
# ``.``/``-``/``?``/``#`` immediately followed by a word char
# (``foo.pdf.bak``, ``report.pdf-v2``, ``foo.md?download=1``, ``foo.md#anchor``
# — prose mentioning ``secrets.json.bak`` must never mint a card claiming
# ``secrets.json``). Legit trailing punctuation still matches: a sentence-final
# ``report.md.`` / ``report.md,`` / ``(report.md)`` / ``"report.md"`` all have
# NO word char after the punctuation.
_EXT_ALTERNATION = "|".join(sorted(ARTIFACT_EXTS, key=len, reverse=True))
_CANDIDATE_RE = re.compile(
    r"(?<![\w~./\-])"  # left boundary: not mid-token
    r"((?:~|\.{1,2})?/?(?:[\w.\-]+/)*[\w.\-]+\.(?:" + _EXT_ALTERNATION + r"))"
    r"(?![\w/~])(?![.\-?#]\w)",  # right boundary: the token must END here
    re.IGNORECASE,
)
# Guard against a pathological token (a giant no-space run) — well beyond any
# real path length.
_MAX_CANDIDATE_LEN = 512


# ── Value objects ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Artifact:
    """One validated deliverable file (offer-time context, PINNED at mint)."""

    resolved_path: str
    display_name: str  # root-relative when possible, else the bare basename
    size: int
    # The RESOLVED allowed roots the file was validated under (codex r2 P2-1):
    # send-time revalidation uses THESE, never a recomputed (mutable) cwd.
    allowed_roots: tuple[str, ...]


@dataclass
class OpenResult:
    """Outcome of ``open_validated_artifact``: a live fd on success, else why."""

    file: BinaryIO | None
    size: int
    reason: str | None  # None on success


# ── Extraction (§A.2) ─────────────────────────────────────────────────────


def extract_artifact_candidates(text: str) -> list[str]:
    """Return path-like tokens in ``text`` ending in an allowlisted extension.

    Pure string-level — NO filesystem access. Order-preserving, de-duplicated
    on the raw token. Tolerant of backticks, parens, markdown-link targets, and
    trailing punctuation (all outside the path char class). Supports absolute
    (``/…``), ``~/…``, ``./…``, and bare-relative (``temp/report.md``) shapes.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE_RE.finditer(text):
        token = match.group(1)
        if len(token) > _MAX_CANDIDATE_LEN or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


# ── Validation (§A.3) ─────────────────────────────────────────────────────


def _resolved_roots(cwd: str, extra_roots: list[str]) -> list[Path]:
    """The RESOLVED allowed roots: the session cwd (fail-closed on empty) +
    the injected extra roots. Each ``expanduser().resolve()``- d so a symlinked
    root is compared in its real form."""
    roots: list[Path] = []
    if cwd:  # empty/"" cwd contributes NO root — fail-closed
        try:
            roots.append(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    for raw in extra_roots:
        if not raw:
            continue
        try:
            roots.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            pass
    return roots


def _worktree_main_root(resolved_cwd: Path) -> Path | None:
    """The main-repo root of a HARNESS worktree cwd, else None.

    A harness worktree cwd is ``<main_repo>/.claude/worktrees/<name>``, so the
    main root is the prefix BEFORE the first ``.claude``/``worktrees`` segment
    pair. Pure string/``Path`` logic on the ALREADY-RESOLVED cwd (a prefix of a
    resolved path is itself canonical — no symlinks / ``..`` to re-resolve, no
    git subprocess). The cwd must be INSIDE a worktree — at least one path
    segment AFTER the ``worktrees`` segment (codex P1: the BARE container,
    e.g. ``~/.claude/worktrees`` itself, would otherwise derive the user's
    entire home directory as the fallback root). Returns None when the shape
    is absent or the prefix is not a proper directory (just the filesystem
    anchor). Only this exact layout is covered — a general ``git worktree
    add`` elsewhere has no such shape.
    """
    parts = resolved_cwd.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            if i + 2 >= len(parts):  # the BARE container — not inside a worktree
                return None
            prefix = parts[:i]
            if len(prefix) < 2:  # anchor only (e.g. "/") — never a repo root
                return None
            return Path(*prefix)
    return None


def _within_roots(resolved: Path, roots: list[Path]) -> bool:
    """True iff ``resolved`` is one of, or nested under, a resolved root."""
    for root in roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def _display_name(resolved: Path, roots: list[Path]) -> str:
    """Root-relative display name (shortest across roots), else the basename."""
    best: str | None = None
    for root in roots:
        try:
            rel = str(resolved.relative_to(root))
        except (OSError, ValueError):
            continue
        if best is None or len(rel) < len(best):
            best = rel
    return best if best else resolved.name


# The ONLY cwd-attempt rejection that triggers the worktree fallback: genuine
# file-not-found (nothing servable exists at the candidate's name under cwd).
# Any EXISTING-but-rejected cwd resolution OWNS the name — oversize /
# non-regular, AND an "outside the allowed folders" reject (hermes P3c): the
# only producers of an "outside" cwd-attempt reject on a RELATIVE candidate
# are a `../` traversal (which rejects under the main root too, so the
# fallthrough never served anything) and a cwd-local symlink escaping the
# roots (attacker-shaped — a fallthrough would silently SUBSTITUTE a different
# main-root file for the entry the session's prose actually referred to). So
# "outside" is deliberately NOT a fallback reason; narrowing to file-not-found
# loses no legitimate hit.
_FALLBACK_REASONS = frozenset({"file not found"})


def _validate_resolved(
    resolved: Path, roots: list[Path], max_bytes: int
) -> tuple[Artifact | None, str | None]:
    """Containment → regular-file → size for an already-resolved path.

    ``roots`` is the RESOLVED allowed-root set this resolution was validated
    under; it is what the returned Artifact PINS (re-checked at fd-open time)
    and what the display name is made relative to.
    """
    if not _within_roots(resolved, roots):
        return None, "outside the session's allowed folders"
    try:
        st = resolved.stat()
    except OSError:
        return None, "file not found"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    if st.st_size > max_bytes:
        cap_mb = max_bytes / (1024 * 1024)
        return None, f"file too large (over the {cap_mb:.0f} MB cap)"
    return (
        Artifact(
            resolved_path=str(resolved),
            display_name=_display_name(resolved, roots),
            size=st.st_size,
            allowed_roots=tuple(str(r) for r in roots),
        ),
        None,
    )


def _classify_one(
    candidate: str, cwd: str, roots: list[Path], max_bytes: int
) -> tuple[Artifact | None, str | None]:
    """Validate ONE candidate. Returns (Artifact, None) or (None, reason).

    ``reason`` is a short, user-facing rejection string for ``/file``; the card
    path drops rejections silently (§A.3).

    Absolute candidates: resolve → containment against ``roots`` (unchanged). A
    RELATIVE candidate joins against the session ``cwd`` first; if that yields
    no valid file here AND ``cwd`` is a harness worktree, it retries the join
    against the derived main repo root (pinned + displayed relative to it). The
    cwd hit ALWAYS wins; traversal / symlink-escape reject under BOTH roots.
    """
    raw = candidate.strip()
    if not raw:
        return None, "empty path"
    try:
        p = Path(raw).expanduser()
    except (RuntimeError, ValueError):
        return None, "invalid path"

    if p.is_absolute():
        try:
            # resolve() FOLLOWS symlinks, so a symlink pointing outside a root
            # resolves outside and fails the containment test.
            resolved = p.resolve()
        except (OSError, RuntimeError, ValueError):
            return None, "invalid path"
        return _validate_resolved(resolved, roots, max_bytes)

    # Relative candidate — needs a working directory to join against.
    if not cwd:  # fail-closed
        return None, "this session has no working directory"
    try:
        resolved_cwd = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid path"

    # Attempt 1 — the session cwd (the primary root; cwd hit WINS).
    try:
        resolved = (resolved_cwd / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid path"
    art, reason = _validate_resolved(resolved, roots, max_bytes)
    if art is not None:
        return art, None
    if reason not in _FALLBACK_REASONS:  # an owned cwd rejection — never substitute
        return None, reason

    # Attempt 2 — the harness worktree's main repo root (pinned to it alone, so
    # containment / display are anchored on the matched root). A `../`-escape
    # from main_root fails its containment too → rejected under BOTH roots.
    main_root = _worktree_main_root(resolved_cwd)
    if main_root is None:
        return None, reason  # not a worktree cwd → no fallback invented
    try:
        fb_resolved = (main_root / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, reason
    fb_art, fb_reason = _validate_resolved(fb_resolved, [main_root], max_bytes)
    if fb_art is not None:
        return fb_art, None
    return None, fb_reason


def resolve_artifacts(
    candidates: list[str], cwd: str, extra_roots: list[str], max_bytes: int
) -> list[Artifact]:
    """Validate ``candidates`` → the list of offerable ``Artifact``s.

    Fail-closed: an empty ``cwd`` (and no extra roots) yields no roots, so
    NOTHING is offerable. Within-block dedup by resolved path.
    """
    roots = _resolved_roots(cwd, extra_roots)
    if not roots:
        return []
    out: list[Artifact] = []
    seen: set[str] = set()
    for cand in candidates:
        art, _reason = _classify_one(cand, cwd, roots, max_bytes)
        if art is None or art.resolved_path in seen:
            continue
        seen.add(art.resolved_path)
        out.append(art)
    return out


def resolve_single(
    path: str, cwd: str, extra_roots: list[str], max_bytes: int
) -> tuple[Artifact | None, str | None]:
    """Validate ONE explicit ``/file`` path, surfacing the rejection reason."""
    roots = _resolved_roots(cwd, extra_roots)
    if not roots:
        return None, "this session has no working directory"
    return _classify_one(path, cwd, roots, max_bytes)


# ── TOCTOU-closing open (§A.4) ────────────────────────────────────────────


def open_validated_artifact(
    resolved_path: str, allowed_roots: tuple[str, ...], max_bytes: int
) -> OpenResult:
    """Open ``resolved_path`` for read through the validated-fd contract.

    Re-check containment against the PINNED ``allowed_roots``, ``os.open`` with
    ``O_RDONLY | O_NOFOLLOW`` (a final-component symlink swapped in after
    validation → open FAILS), ``os.fstat`` → regular-file + ``size <= max_bytes``
    ENFORCED ON THE FD, then return the open file object. The caller passes THIS
    object to Telegram and closes it in a ``finally`` — the pathname is never
    re-opened. (Disclosed residual: an intermediate-DIRECTORY symlink swap
    between resolve and open is not covered by ``O_NOFOLLOW`` — accepted on a
    single-owner box; the fd's fstat still guarantees regular-file + size.)
    """
    resolved = Path(resolved_path)
    roots = [Path(r) for r in allowed_roots]
    # Re-check containment against the PINNED roots (pure string; no FS access).
    if not roots or not _within_roots(resolved, roots):
        return OpenResult(file=None, size=0, reason="outside the allowed folders")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(resolved), flags)
    except OSError as exc:
        # ELOOP = the final component is a symlink (swapped in after validation).
        reason = "file not found" if exc.errno == 2 else "could not open the file"
        return OpenResult(file=None, size=0, reason=reason)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return OpenResult(file=None, size=0, reason="could not open the file")
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return OpenResult(file=None, size=0, reason="not a regular file")
    if st.st_size > max_bytes:
        os.close(fd)
        cap_mb = max_bytes / (1024 * 1024)
        return OpenResult(
            file=None, size=0, reason=f"file too large (over the {cap_mb:.0f} MB cap)"
        )
    try:
        file_obj = os.fdopen(fd, "rb")
    except Exception:
        # fdopen failing leaves the raw fd UNCONSUMED — close it here or the
        # validated fd leaks for the process lifetime (fold item 4 — hermes
        # P3-1; this is the security-sensitive fd contract, keep it airtight).
        os.close(fd)
        return OpenResult(file=None, size=0, reason="could not open the file")
    return OpenResult(file=file_obj, size=st.st_size, reason=None)


# ── Card text (§C body) ───────────────────────────────────────────────────
#
# Owner product decision (2026-07-09): the card body is PATHLESS — a single
# static line, never the detected paths. Two reasons: (1) the triggering prose
# message directly above the card ALWAYS names the file(s), so listing them
# again in the body was pure repetition; (2) Telegram clients auto-linkify a
# bare path whose extension collides with a TLD (``.md`` = Moldova, ``.zip``,
# …) into a dead blue link that opens nothing. The (clipped) button labels
# carry the names; ``/file <path>`` — using a path from that prose — is the
# restart-safe fallback.
_CARD_HEADER = "📎 Tap to download:"


def card_text(*, overflow: int = 0) -> str:
    """Render the 📎 card body: the static header + an optional overflow note.

    PATHLESS by owner decision — no detected paths in the body (a plain-text
    path gets TLD-auto-linkified into a dead link, and the prose above the card
    already names the file(s)). Only the ``overflow`` count is dynamic.
    """
    lines = [_CARD_HEADER]
    if overflow > 0:
        lines.append(
            f"…and {overflow} more — send /file <path> using a path from "
            "the message above."
        )
    return "\n".join(lines)


# ── Token registry + offer-dedup (§A.5) ───────────────────────────────────

STATE_LIVE = "live"
STATE_IN_FLIGHT = "in_flight"

# dlf:<window_id>:<token> ≈ 22 bytes ≪ 64 (checked_callback_data enforces).
_TOKEN_BYTES = 9
_MAX_BUTTONS_PER_CARD = 6
_BUTTON_LABEL_MAX_CHARS = 64
OFFER_DEDUP_TTL_S = 30 * 60.0  # a re-mention of the same file within 30 min is cheap
_TOKEN_TTL_S = 24 * 60 * 60.0  # lazy token TTL


def clip_label(label: str) -> str:
    """Clip a BUTTON label to ≤64 chars, keeping the TAIL (fold item 2 — codex
    P2-2 / hermes P2-1). Telegram may reject the whole keyboard on an over-long
    label AFTER the tokens were minted and the offer-dedup marked, so the clip
    happens at mint. The filename is the discriminating part of a path, so
    prefix-ellipsize (``…<tail>``) — the inverse of late_answer's suffix clip.
    """
    if len(label) <= _BUTTON_LABEL_MAX_CHARS:
        return label
    return "…" + label[-(_BUTTON_LABEL_MAX_CHARS - 1) :]


@dataclass
class ArtifactRow:
    """One ``dlf:`` registry row — the offer-time context PINNED at mint."""

    owner_id: int
    thread_id: int
    window_id: str
    resolved_path: str
    display_name: str
    pinned_roots: tuple[str, ...]
    created: float
    state: str = STATE_LIVE


@dataclass
class MintedCard:
    """The card render inputs: buttoned rows + the no-button overflow count.

    The card BODY is pathless (owner decision — see ``card_text``), so the file
    names live ONLY on the (clipped) button labels; there is no ``names`` body
    list.
    """

    # (button_label, callback_data) — the label is CLIPPED to ≤64 chars.
    rows: list[tuple[str, str]] = field(default_factory=list)
    overflow: int = 0
    # The artifacts ACTUALLY minted into buttons (== rows, 1:1, in order) —
    # observability correlation ONLY (round-1 hermes P2: the caller must log
    # exactly what was minted, never the full resolved list, and never
    # absolute paths — it logs these rows' relative display names).
    minted: list[Artifact] = field(default_factory=list)


_rows: dict[str, ArtifactRow] = {}
_offer_dedup: dict[tuple[tuple[int, int, str], str], float] = {}


def _prune_offer_dedup(now: float) -> None:
    stale = [k for k, ts in _offer_dedup.items() if now - ts >= OFFER_DEDUP_TTL_S]
    for k in stale:
        del _offer_dedup[k]


def _recently_offered(route: tuple[int, int, str], path: str, now: float) -> bool:
    ts = _offer_dedup.get((route, path))
    return ts is not None and now - ts < OFFER_DEDUP_TTL_S


def mint(
    route: tuple[int, int, str],
    artifacts: list[Artifact],
    *,
    max_buttons: int = _MAX_BUTTONS_PER_CARD,
) -> MintedCard | None:
    """Register the fresh (non-offer-deduped) artifacts and return the card.

    Filters out files offered on this route within ``OFFER_DEDUP_TTL_S`` (a
    mid-turn repeat is cheap). Mints ``dlf:`` tokens + buttons for the first
    ``max_buttons`` fresh files ONLY (those are marked offered); the rest are
    reported as an overflow count reachable via ``/file``. Returns ``None`` when
    nothing fresh remains (no card is posted).
    """
    owner_id, thread_id, window_id = route
    now = time.monotonic()
    _prune_offer_dedup(now)
    fresh = [a for a in artifacts if not _recently_offered(route, a.resolved_path, now)]
    if not fresh:
        return None
    head = fresh[:max_buttons]
    rows: list[tuple[str, str]] = []
    for art in head:
        _offer_dedup[(route, art.resolved_path)] = now
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        _rows[token] = ArtifactRow(
            owner_id=owner_id,
            thread_id=thread_id,
            window_id=window_id,
            resolved_path=art.resolved_path,
            display_name=art.display_name,
            pinned_roots=art.allowed_roots,
            created=now,
        )
        rows.append(
            (
                clip_label(art.display_name),
                checked_callback_data(f"{CB_DOWNLOAD_FILE}{window_id}:{token}"),
            )
        )
    return MintedCard(rows=rows, overflow=len(fresh) - len(head), minted=list(head))


def lookup(token: str) -> ArtifactRow | None:
    """Return the registry row for ``token`` (None post-restart / TTL / cleared)."""
    row = _rows.get(token)
    if row is None:
        return None
    if time.monotonic() - row.created >= _TOKEN_TTL_S:
        del _rows[token]
        return None
    return row


def begin_send(token: str) -> bool:
    """Single-FLIGHT gate: ``live → in_flight``. False when in flight / unknown."""
    row = _rows.get(token)
    if row is None or row.state != STATE_LIVE:
        return False
    row.state = STATE_IN_FLIGHT
    return True


def finish_send(token: str, ok: bool) -> None:
    """Resolve an in-flight send back to ``live`` (single-FLIGHT, NOT single-use).

    ``ok`` is accepted for API parity with the pick-card model; a download is
    not a terminal action, so a re-tap re-uploads (benign, serialized) — hence
    both outcomes return the row to ``live``.
    """
    del ok  # both outcomes → live (single-FLIGHT, not single-use)
    row = _rows.get(token)
    if row is not None:
        row.state = STATE_LIVE


def invalidate_window(window_id: str) -> None:
    """Drop every artifact card + offer-dedup entry minted for ``window_id``."""
    stale = [t for t, row in _rows.items() if row.window_id == window_id]
    for t in stale:
        del _rows[t]
    dead = [k for k in _offer_dedup if k[0][2] == window_id]
    for k in dead:
        del _offer_dedup[k]


def invalidate_topic(owner_id: int, thread_id: int) -> None:
    """Drop every artifact card + offer-dedup entry for ``(owner_id, thread_id)``.

    Topic-keyed (mirrors ``late_answer.invalidate_topic``): ``clear_topic_state``
    reaches queue-less routes too, so the sweep is on the stored (owner, thread).
    """
    stale = [
        t
        for t, row in _rows.items()
        if row.owner_id == owner_id and row.thread_id == thread_id
    ]
    for t in stale:
        del _rows[t]
    dead = [k for k in _offer_dedup if k[0][0] == owner_id and k[0][1] == thread_id]
    for k in dead:
        del _offer_dedup[k]


def reset_for_tests() -> None:
    """Test-only: drop all registry + offer-dedup state (R3 reset-seam contract)."""
    _rows.clear()
    _offer_dedup.clear()
