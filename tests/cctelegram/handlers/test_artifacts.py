"""Unit tests for handlers/artifacts.py (the 📎 artifact delivery lane leaf).

RED-first (plan §J): the extraction shapes + the validated-fd open contract are
pinned before implementation. Also covers the resolve/validate rejections
(traversal, symlink-escape, oversize, empty-cwd fail-closed), the registry
single-FLIGHT state machine + offer-dedup + token TTL, and the ≤64-byte
callback cap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram.handlers import artifacts
from cctelegram.handlers.callback_data import CB_DOWNLOAD_FILE


@pytest.fixture(autouse=True)
def _reset() -> None:
    artifacts.reset_for_tests()
    yield
    artifacts.reset_for_tests()


# ── Extraction shapes (§A.2) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("See report.md for details", ["report.md"]),
        ("Wrote the chart to `chart.png` now", ["chart.png"]),
        ("Saved (export.pdf) in the folder", ["export.pdf"]),
        ("[the report](docs/report.md) is ready", ["docs/report.md"]),
        ("Output written to output.csv.", ["output.csv"]),
        ("Your notes are in ~/notes/todo.txt", ["~/notes/todo.txt"]),
        ("Open ./build/index.html to preview", ["./build/index.html"]),
        ("Placed at temp/report.md today", ["temp/report.md"]),
        ("Tailing /var/log/app.log now", ["/var/log/app.log"]),
        ("A deep one: a/b/c/deck.pptx.", ["a/b/c/deck.pptx"]),
        # Legit trailing-punctuation pins — MUST survive the fold-item-1
        # right-boundary tightening (sentence-final period is in the shapes
        # above; comma / colon / quotes pinned here).
        ("Check report.md, then continue", ["report.md"]),
        ("Next file: report.md: all done", ["report.md"]),
        ('Open "report.md" now', ["report.md"]),
    ],
)
def test_extract_shapes(text: str, expected: list[str]) -> None:
    assert artifacts.extract_artifact_candidates(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "backup at foo.pdf.bak here",
        "old copy report.md.old kept",
        "inside foo.pdf/assets today",
        "versioned report.pdf-v2 saved",
        "see foo.md#anchor now",
        "get foo.md?download=1 there",
    ],
)
def test_extract_rejects_continued_tokens(text: str) -> None:
    """[fold item 1 — hermes P1 / codex P2-1, converged] a matched extension
    must genuinely END the token: prose mentioning `secrets.json.bak` must
    never mint a tappable card claiming `secrets.json` — the card would lie
    about what was mentioned. Reject when the next characters CONTINUE the
    token: any word char, `/`, `~`, or `.`/`-`/`?`/`#` immediately followed
    by a word char."""
    assert artifacts.extract_artifact_candidates(text) == []


def test_extract_bare_domain_documented_behavior() -> None:
    """[fold item 1] a BARE domain (`example.com/report.pdf`, no scheme) is
    string-indistinguishable from a bare-relative path, so it IS extracted —
    but always as the WHOLE token (never a truncated `report.pdf`); the
    resolve-against-cwd step then fail-closes on nonexistence, so no card is
    ever offered for it. Scheme-ful URLs (`https://…`) never match at all —
    the left boundary rejects every mid-token start after `://`."""
    assert artifacts.extract_artifact_candidates("see example.com/report.pdf now") == [
        "example.com/report.pdf"
    ]
    assert (
        artifacts.extract_artifact_candidates("at https://example.com/report.pdf now")
        == []
    )


def test_extract_rejects_non_allowlisted_and_mdx_guard() -> None:
    # Source-code extensions are never offered; .mdx must not match .md.
    assert artifacts.extract_artifact_candidates("run script.py then main.ts") == []
    assert artifacts.extract_artifact_candidates("edit component.mdx here") == []
    assert artifacts.extract_artifact_candidates("no paths here at all") == []


def test_extract_dedups_preserving_order() -> None:
    text = "a.png and a.png then b.pdf and a.png"
    assert artifacts.extract_artifact_candidates(text) == ["a.png", "b.pdf"]


def test_extract_multiple_mixed_shapes() -> None:
    text = "Files: `reports/q1.csv`, /tmp/out.json, and ~/deck.pptx done."
    assert artifacts.extract_artifact_candidates(text) == [
        "reports/q1.csv",
        "/tmp/out.json",
        "~/deck.pptx",
    ]


# ── Validation (§A.3) ─────────────────────────────────────────────────────


def _write(p: Path, data: bytes = b"hello") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_resolve_absolute_inside_root(tmp_path: Path) -> None:
    f = _write(tmp_path / "sub" / "report.md")
    got = artifacts.resolve_artifacts([str(f)], str(tmp_path), [], 1024)
    assert len(got) == 1
    assert got[0].resolved_path == str(f.resolve())
    assert got[0].display_name == "sub/report.md"
    assert got[0].size == 5
    assert got[0].allowed_roots == (str(tmp_path.resolve()),)


def test_resolve_bare_relative_joined_to_cwd(tmp_path: Path) -> None:
    _write(tmp_path / "temp" / "data.csv")
    got = artifacts.resolve_artifacts(["temp/data.csv"], str(tmp_path), [], 1024)
    assert len(got) == 1
    assert got[0].display_name == "temp/data.csv"


def test_resolve_extra_root(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    f = _write(root_b / "note.txt")
    got = artifacts.resolve_artifacts([str(f)], str(root_a), [str(root_b)], 1024)
    assert len(got) == 1 and got[0].display_name == "note.txt"


def test_reject_nonexistent(tmp_path: Path) -> None:
    got = artifacts.resolve_artifacts(["missing.pdf"], str(tmp_path), [], 1024)
    assert got == []


def test_reject_directory_not_file(tmp_path: Path) -> None:
    (tmp_path / "adir.md").mkdir()
    got = artifacts.resolve_artifacts(["adir.md"], str(tmp_path), [], 1024)
    assert got == []


def test_reject_oversize(tmp_path: Path) -> None:
    _write(tmp_path / "big.zip", b"x" * 5000)
    got = artifacts.resolve_artifacts(["big.zip"], str(tmp_path), [], 1024)
    assert got == []


def test_reject_empty_cwd_fail_closed(tmp_path: Path) -> None:
    _write(tmp_path / "x.md")
    # empty cwd AND no extra roots ⇒ no roots ⇒ nothing offerable.
    assert artifacts.resolve_artifacts(["x.md"], "", [], 1024) == []


def test_reject_traversal_escaping_cwd(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path / "secret.json")
    got = artifacts.resolve_artifacts(["../secret.json"], str(root), [], 1024)
    assert got == []


def test_reject_absolute_outside_roots(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside" / "x.pdf")
    root = tmp_path / "root"
    root.mkdir()
    got = artifacts.resolve_artifacts([str(outside)], str(root), [], 1024)
    assert got == []


def test_reject_in_root_symlink_resolving_outside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = _write(tmp_path / "outside" / "secret.log")
    link = root / "link.log"
    link.symlink_to(target)
    # resolve() follows the symlink → outside root → rejected.
    got = artifacts.resolve_artifacts(["link.log"], str(root), [], 1024)
    assert got == []


def test_resolve_single_reasons(tmp_path: Path) -> None:
    _write(tmp_path / "ok.md")
    art, reason = artifacts.resolve_single("ok.md", str(tmp_path), [], 1024)
    assert art is not None and reason is None
    _art, reason = artifacts.resolve_single("nope.md", str(tmp_path), [], 1024)
    assert _art is None and reason and "not found" in reason.lower()
    _art, reason = artifacts.resolve_single("../up.md", str(tmp_path / "r"), [], 1024)
    assert _art is None and reason is not None
    _write(tmp_path / "huge.pdf", b"z" * 4096)
    _art, reason = artifacts.resolve_single("huge.pdf", str(tmp_path), [], 1024)
    assert _art is None and reason and "large" in reason.lower()
    _art, reason = artifacts.resolve_single("x.md", "", [], 1024)
    assert _art is None and reason is not None


# ── Worktree-aware fallback root (harness worktree sessions) ──────────────
#
# A Claude session whose cwd is a HARNESS WORKTREE
# (``<main_repo>/.claude/worktrees/<name>``) writes its handoff to the MAIN
# repo (``<main_repo>/temp/sessions/x.md`` — the convention) and mentions it as
# the relative path ``temp/sessions/x.md``. The join against the worktree cwd
# misses, so the resolve now falls back to the worktree's main-repo root.


def _worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Return (main_root, worktree_cwd) with the harness ``.claude/worktrees``
    shape. Both are real dirs so ``resolve()`` canonicalizes them."""
    main_root = tmp_path / "myrepo"
    worktree = main_root / ".claude" / "worktrees" / "agent-x"
    worktree.mkdir(parents=True, exist_ok=True)
    return main_root, worktree


def test_worktree_fallback_relative_hits_main_root(tmp_path: Path) -> None:
    """RED-first: a relative candidate that exists ONLY in the main repo root
    (not under the worktree cwd) resolves via the derived main root, is pinned
    to the main root, and the display name is root-relative to it."""
    main_root, worktree = _worktree(tmp_path)
    f = _write(main_root / "temp" / "sessions" / "x.md", b"handoff")
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(worktree), [], 1024)
    assert len(got) == 1
    assert got[0].resolved_path == str(f.resolve())
    assert got[0].display_name == "temp/sessions/x.md"
    assert got[0].allowed_roots == (str(main_root.resolve()),)


def test_worktree_fallback_cwd_copy_wins(tmp_path: Path) -> None:
    """RED-first (order): the same-named file existing in BOTH the worktree cwd
    and the main root resolves to the WORKTREE's own copy (cwd hit wins)."""
    main_root, worktree = _worktree(tmp_path)
    _write(main_root / "temp" / "sessions" / "x.md", b"MAIN")
    cwd_copy = _write(worktree / "temp" / "sessions" / "x.md", b"WORKTREE")
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(worktree), [], 1024)
    assert len(got) == 1
    assert got[0].resolved_path == str(cwd_copy.resolve())
    # Pinned to the worktree cwd (the primary root), not the main root.
    assert got[0].allowed_roots == (str(worktree.resolve()),)


def test_worktree_fallback_missing_in_both_skipped(tmp_path: Path) -> None:
    """RED-first: a relative candidate in NEITHER root → fail-closed skip."""
    _main_root, worktree = _worktree(tmp_path)
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(worktree), [], 1024)
    assert got == []


def test_non_worktree_cwd_no_fallback(tmp_path: Path) -> None:
    """RED-first: a NON-harness-worktree cwd + a missing relative candidate is
    skipped — the fallback is never invented for a plain cwd."""
    # A regular repo directory that happens to have a file in its parent.
    plain = tmp_path / "plainrepo"
    plain.mkdir()
    _write(tmp_path / "temp" / "sessions" / "x.md", b"parent")
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(plain), [], 1024)
    assert got == []


def test_worktree_fallback_traversal_rejected_under_both(tmp_path: Path) -> None:
    """RED-first: a ``../``-escaping candidate must reject under BOTH the cwd
    and the derived main root (never serve a file outside either)."""
    main_root, worktree = _worktree(tmp_path)
    # A real file two levels above the main root; a traversal candidate aimed at
    # it must not resolve under the worktree cwd NOR under the main root.
    _write(tmp_path / "secret.json", b"secret")
    got = artifacts.resolve_artifacts(
        ["../../../../secret.json"], str(worktree), [], 1024
    )
    assert got == []


def test_worktree_fallback_symlink_in_main_root_rejected(tmp_path: Path) -> None:
    """RED-first: a symlink UNDER the main root pointing outside is rejected —
    resolve() follows it, then containment against the main root fails."""
    main_root, worktree = _worktree(tmp_path)
    target = _write(tmp_path / "outside" / "secret.log", b"secret")
    (main_root / "temp").mkdir(parents=True, exist_ok=True)
    link = main_root / "temp" / "link.log"
    link.symlink_to(target)
    got = artifacts.resolve_artifacts(["temp/link.log"], str(worktree), [], 1024)
    assert got == []


def test_bare_worktrees_container_cwd_no_fallback(tmp_path: Path) -> None:
    """RED-first (codex P1): a cwd that IS the bare ``.claude/worktrees``
    CONTAINER (no ``<name>`` segment after it — e.g. ``~/.claude/worktrees``)
    must NOT derive a fallback root: deriving its parent would open the entire
    home directory. A candidate that exists under the would-be derived parent
    is skipped, fail-closed."""
    main_root = tmp_path / "myrepo"
    container = main_root / ".claude" / "worktrees"
    container.mkdir(parents=True)
    _write(main_root / "temp" / "sessions" / "x.md", b"handoff")
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(container), [], 1024)
    assert got == []


def test_worktree_fallback_nested_subdir_cwd_derives_main(tmp_path: Path) -> None:
    """A cwd DEEPER inside a worktree (``<main>/.claude/worktrees/<x>/subdir``)
    still derives ``<main>`` correctly (the prefix before the segment pair)."""
    main_root, worktree = _worktree(tmp_path)
    subdir = worktree / "src" / "pkg"
    subdir.mkdir(parents=True)
    f = _write(main_root / "temp" / "sessions" / "x.md", b"handoff")
    got = artifacts.resolve_artifacts(["temp/sessions/x.md"], str(subdir), [], 1024)
    assert len(got) == 1
    assert got[0].resolved_path == str(f.resolve())
    assert got[0].allowed_roots == (str(main_root.resolve()),)


def test_cwd_copy_oversize_owns_name_no_fallback(tmp_path: Path) -> None:
    """[hermes P3a] an EXISTING-but-oversize cwd copy OWNS the name: no
    main-root fallback even though a VALID same-named copy sits there —
    substituting a different file for the one the prose referred to would lie."""
    main_root, worktree = _worktree(tmp_path)
    _write(main_root / "temp" / "x.md", b"ok")  # valid main-root copy
    _write(worktree / "temp" / "x.md", b"z" * 5000)  # oversize cwd copy
    got = artifacts.resolve_artifacts(["temp/x.md"], str(worktree), [], 1024)
    assert got == []


def test_cwd_copy_directory_owns_name_no_fallback(tmp_path: Path) -> None:
    """[hermes P3b] a DIRECTORY at the name under cwd owns it — no fallback."""
    main_root, worktree = _worktree(tmp_path)
    _write(main_root / "temp" / "x.md", b"ok")  # valid main-root copy
    (worktree / "temp" / "x.md").mkdir(parents=True)  # directory in cwd
    got = artifacts.resolve_artifacts(["temp/x.md"], str(worktree), [], 1024)
    assert got == []


def test_cwd_escaping_symlink_owns_name_no_fallback(tmp_path: Path) -> None:
    """RED-first [hermes P3c]: a cwd symlink at the name ESCAPING the roots
    owns the name too — NO main-root fallback even though a valid same-named
    copy sits there. Rationale (the narrowed `_FALLBACK_REASONS`): an existing
    cwd entry at the name that is rejected (oversize / non-regular / escaping
    symlink) must never be silently SUBSTITUTED with a different main-root
    file — the tap would deliver a file other than the one the session's
    prose referred to. The fallback fires ONLY on genuine file-not-found."""
    main_root, worktree = _worktree(tmp_path)
    _write(main_root / "temp" / "x.md", b"ok")  # valid main-root copy
    outside = _write(tmp_path / "outside" / "secret.md", b"secret")
    (worktree / "temp").mkdir(parents=True, exist_ok=True)
    (worktree / "temp" / "x.md").symlink_to(outside)  # escaping cwd symlink
    got = artifacts.resolve_artifacts(["temp/x.md"], str(worktree), [], 1024)
    assert got == []


def test_worktree_fallback_resolve_single_surfaces_hit(tmp_path: Path) -> None:
    """``/file`` (resolve_single) shares the pipeline → the same fallback."""
    main_root, worktree = _worktree(tmp_path)
    f = _write(main_root / "temp" / "sessions" / "x.md", b"handoff")
    art, reason = artifacts.resolve_single(
        "temp/sessions/x.md", str(worktree), [], 1024
    )
    assert reason is None
    assert art is not None
    assert art.resolved_path == str(f.resolve())
    assert art.display_name == "temp/sessions/x.md"
    assert art.allowed_roots == (str(main_root.resolve()),)


# ── open_validated_artifact (§A.4) ────────────────────────────────────────


def test_open_validated_happy_path(tmp_path: Path) -> None:
    f = _write(tmp_path / "doc.pdf", b"PDFDATA")
    res = artifacts.open_validated_artifact(
        str(f.resolve()), (str(tmp_path.resolve()),), 1024
    )
    assert res.file is not None and res.reason is None
    try:
        assert res.size == 7
        assert res.file.read() == b"PDFDATA"
    finally:
        res.file.close()


def test_open_rejects_outside_pinned_roots(tmp_path: Path) -> None:
    f = _write(tmp_path / "doc.pdf")
    res = artifacts.open_validated_artifact(
        str(f.resolve()), (str((tmp_path / "elsewhere").resolve()),), 1024
    )
    assert res.file is None and res.reason is not None


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable on this platform"
)
def test_open_refuses_final_component_symlink(tmp_path: Path) -> None:
    target = _write(tmp_path / "real.md", b"data")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    # A final-component symlink (swapped in after validation) → O_NOFOLLOW fails.
    res = artifacts.open_validated_artifact(str(link), (str(tmp_path.resolve()),), 1024)
    assert res.file is None and res.reason is not None


def test_open_enforces_size_on_fd(tmp_path: Path) -> None:
    f = _write(tmp_path / "big.zip", b"y" * 4096)
    res = artifacts.open_validated_artifact(
        str(f.resolve()), (str(tmp_path.resolve()),), 1024
    )
    assert res.file is None and res.reason is not None


def test_open_fdopen_failure_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[fold item 4 — hermes P3-1] a raise from ``os.fdopen`` must not leak the
    validated fd — the helper closes it and returns the error shape."""
    f = _write(tmp_path / "doc.pdf", b"x")
    opened_fds: list[int] = []
    real_open = os.open

    def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened_fds.append(fd)
        return fd

    def fdopen_raises(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryError("fdopen boom")

    monkeypatch.setattr(artifacts.os, "open", spy_open)
    monkeypatch.setattr(artifacts.os, "fdopen", fdopen_raises)
    res = artifacts.open_validated_artifact(
        str(f.resolve()), (str(tmp_path.resolve()),), 1024
    )
    assert res.file is None and res.reason is not None
    assert opened_fds, "the spy must have observed the validated open"
    # The fd must be CLOSED — fstat on a closed fd raises.
    with pytest.raises(OSError):
        os.fstat(opened_fds[-1])


# ── Registry state machine + offer-dedup + TTL (§A.5) ─────────────────────


def _art(path: str = "/repo/a.png", name: str = "a.png") -> artifacts.Artifact:
    return artifacts.Artifact(
        resolved_path=path, display_name=name, size=10, allowed_roots=("/repo",)
    )


def test_mint_and_lookup_roundtrip() -> None:
    route = (12345, 42, "@7")
    card = artifacts.mint(route, [_art()])
    assert card is not None and len(card.rows) == 1
    label, cb = card.rows[0]
    assert label == "a.png" and cb.startswith(f"{CB_DOWNLOAD_FILE}@7:")
    token = cb.split(":")[-1]
    row = artifacts.lookup(token)
    assert row is not None
    assert (row.owner_id, row.thread_id, row.window_id) == (12345, 42, "@7")
    assert row.resolved_path == "/repo/a.png"
    assert row.pinned_roots == ("/repo",)
    assert row.state == "live"


def test_mint_offer_dedup_suppresses_repeat() -> None:
    route = (1, 2, "@3")
    assert artifacts.mint(route, [_art()]) is not None
    # Same file on the same route within the TTL → nothing fresh → None.
    assert artifacts.mint(route, [_art()]) is None


def test_mint_button_cap_and_overflow() -> None:
    route = (1, 2, "@3")
    many = [_art(f"/repo/f{i}.png", f"f{i}.png") for i in range(8)]
    card = artifacts.mint(route, many)
    assert card is not None
    assert len(card.rows) == 6
    assert card.overflow == 2


def test_begin_finish_send_single_flight_not_single_use() -> None:
    route = (1, 2, "@3")
    card = artifacts.mint(route, [_art()])
    assert card is not None
    token = card.rows[0][1].split(":")[-1]
    assert artifacts.begin_send(token) is True
    # Concurrent second tap blocked while in flight.
    assert artifacts.begin_send(token) is False
    # A completed upload returns to live (a re-tap re-uploads — single-FLIGHT).
    artifacts.finish_send(token, True)
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]
    assert artifacts.begin_send(token) is True
    artifacts.finish_send(token, False)
    assert artifacts.begin_send(token) is True


def test_token_ttl_expires_lookup() -> None:
    route = (1, 2, "@3")
    card = artifacts.mint(route, [_art()])
    token = card.rows[0][1].split(":")[-1]  # type: ignore[union-attr]
    row = artifacts._rows[token]
    row.created = time.monotonic() - artifacts._TOKEN_TTL_S - 1
    assert artifacts.lookup(token) is None


def test_offer_dedup_ttl_allows_reoffer_after_expiry() -> None:
    route = (1, 2, "@3")
    assert artifacts.mint(route, [_art()]) is not None
    # Age the dedup entry past its TTL → the file is offerable again.
    for k in list(artifacts._offer_dedup):
        artifacts._offer_dedup[k] = time.monotonic() - artifacts.OFFER_DEDUP_TTL_S - 1
    assert artifacts.mint(route, [_art()]) is not None


def test_invalidate_window_scoped() -> None:
    card_a = artifacts.mint((1, 2, "@7"), [_art()])
    card_b = artifacts.mint((1, 2, "@8"), [_art("/repo/b.png", "b.png")])
    tok_a = card_a.rows[0][1].split(":")[-1]  # type: ignore[union-attr]
    tok_b = card_b.rows[0][1].split(":")[-1]  # type: ignore[union-attr]
    artifacts.invalidate_window("@7")
    assert artifacts.lookup(tok_a) is None
    assert artifacts.lookup(tok_b) is not None


def test_invalidate_topic_scoped() -> None:
    card_a = artifacts.mint((1, 2, "@7"), [_art()])
    card_b = artifacts.mint((1, 3, "@7"), [_art("/repo/b.png", "b.png")])
    tok_a = card_a.rows[0][1].split(":")[-1]  # type: ignore[union-attr]
    tok_b = card_b.rows[0][1].split(":")[-1]  # type: ignore[union-attr]
    artifacts.invalidate_topic(1, 2)
    assert artifacts.lookup(tok_a) is None
    assert artifacts.lookup(tok_b) is not None


def test_callback_data_under_64_bytes() -> None:
    card = artifacts.mint((1, 2, "@123456"), [_art()])
    assert card is not None
    for _label, cb in card.rows:
        assert len(cb.encode("utf-8")) <= 64


def test_mint_clips_long_button_labels_keeps_tail() -> None:
    """[fold item 2 — codex P2-2 / hermes P2-1] button labels are clipped to
    ≤64 chars, prefix-ellipsized (the FILENAME tail is the discriminating
    part). The card body is pathless (owner decision), so the name lives ONLY
    on the button label."""
    long_name = "very/deeply/nested/" * 4 + "final-quarterly-report-2026.pdf"
    assert len(long_name) > 64
    card = artifacts.mint((1, 2, "@3"), [_art("/repo/" + long_name, long_name)])
    assert card is not None
    label, _cb = card.rows[0]
    assert len(label) <= 64
    assert label.startswith("…")
    assert label.endswith("final-quarterly-report-2026.pdf")
    # A short label passes through untouched.
    card2 = artifacts.mint((1, 2, "@4"), [_art("/repo/a.png", "a.png")])
    assert card2 is not None and card2.rows[0][0] == "a.png"


def test_card_text_shape() -> None:
    # Pathless body (owner decision): a single static line, NO paths — a plain-
    # text path gets TLD-auto-linkified into a dead link, and the prose above
    # the card names the file(s). Overflow points back at that message, keeping
    # the count.
    assert artifacts.card_text() == "📎 Tap to download:"
    text = artifacts.card_text(overflow=3)
    assert text.splitlines() == [
        "📎 Tap to download:",
        "…and 3 more — send /file <path> using a path from the message above.",
    ]
