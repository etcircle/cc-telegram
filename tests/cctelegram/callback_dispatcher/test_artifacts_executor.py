"""dlf: executor unit tests — the artifact-download auth + upload contract.

RED-first (plan §J): the guard order (expired modal / owner / stale window /
single-FLIGHT contention), the validated-fd upload (send_document gets the OPEN
file object), the send-time revalidation failure + RetryAfter + generic-failure
branches, the re-tap-re-uploads single-FLIGHT semantics, and the PINNED-roots
revalidation (independent of any recomputed cwd) are pinned before implementation.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from cctelegram.callback_dispatcher import artifacts as artifacts_exec
from cctelegram.handlers import artifacts
from cctelegram.handlers.callback_data import CB_DOWNLOAD_FILE


@pytest.fixture(autouse=True)
def _reset() -> None:
    artifacts.reset_for_tests()
    yield
    artifacts.reset_for_tests()


class _FakeQuery:
    def __init__(self, data: str, *, chat_id: int = -100, thread_id: int = 10) -> None:
        self.data = data
        self.message = SimpleNamespace(
            chat_id=chat_id, message_thread_id=thread_id, text=None
        )
        self.answer = AsyncMock()


class _FakeBot:
    def __init__(
        self,
        doc_exc: BaseException | None = None,
        msg_exc: BaseException | None = None,
    ) -> None:
        self.documents: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.doc_exc = doc_exc
        self.msg_exc = msg_exc

    async def send_document(
        self, *, chat_id: int, document: Any, filename: str, **kw: Any
    ) -> Any:
        if self.doc_exc is not None:
            raise self.doc_exc
        self.documents.append(
            {
                "chat_id": chat_id,
                "filename": filename,
                "read": document.read(),
                **kw,
            }
        )
        return SimpleNamespace(message_id=1)

    async def send_message(self, *, chat_id: int, text: str, **kw: Any) -> Any:
        if self.msg_exc is not None:
            raise self.msg_exc
        self.messages.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=2)


def _authorized(query: _FakeQuery, user_id: int, thread_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        command=SimpleNamespace(data=query.data),
        ctx=SimpleNamespace(
            query=query,
            user=SimpleNamespace(id=user_id),
            user_id=user_id,
            thread_id=thread_id,
        ),
    )


def _adapters(
    bot: Any, *, window_id: str = "@5", live_window: str | None = "@5"
) -> SimpleNamespace:
    return SimpleNamespace(
        session_manager=SimpleNamespace(
            resolve_window_for_thread=lambda _u, _t: live_window
        ),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(
                return_value=(
                    SimpleNamespace(window_id=window_id) if window_id else None
                )
            )
        ),
        bot=bot,
        config=SimpleNamespace(artifact_max_bytes=45 * 1024 * 1024),
    )


def _seed_row(
    tmp_path: Any,
    *,
    owner_id: int = 1,
    thread_id: int = 10,
    window_id: str = "@5",
    roots: tuple[str, ...] | None = None,
    data: bytes = b"DATA",
    name: str = "doc.pdf",
) -> tuple[str, Any]:
    f = tmp_path / name
    f.write_bytes(data)
    resolved = str(f.resolve())
    pinned = roots if roots is not None else (str(tmp_path.resolve()),)
    token = "seedtoken0001"
    artifacts._rows[token] = artifacts.ArtifactRow(
        owner_id=owner_id,
        thread_id=thread_id,
        window_id=window_id,
        resolved_path=resolved,
        display_name=name,
        pinned_roots=pinned,
        created=time.monotonic(),
    )
    return token, f


def _answers(query: _FakeQuery) -> list[str]:
    return [c.args[0] for c in query.answer.await_args_list if c.args]


@pytest.mark.asyncio
async def test_expired_modal_when_no_row() -> None:
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:nosuchtoken")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert any("expired" in a.lower() for a in _answers(query))
    assert bot.documents == []


@pytest.mark.asyncio
async def test_owner_mismatch_rejected(tmp_path: Any) -> None:
    from cctelegram.callback_dispatcher import WRONG_USER_PICK_TEXT

    token, _f = _seed_row(tmp_path, owner_id=1)
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 999, 10), _adapters(bot)
    )
    assert WRONG_USER_PICK_TEXT in _answers(query)
    assert bot.documents == []
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_payload_window_parity_rejected(tmp_path: Any) -> None:
    from cctelegram.callback_dispatcher import STALE_CALLBACK_TEXT

    token, _f = _seed_row(tmp_path, window_id="@5")
    # Callback payload window @9 ≠ registry window @5.
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@9:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot, window_id="@9")
    )
    assert STALE_CALLBACK_TEXT in _answers(query)
    assert bot.documents == []


@pytest.mark.asyncio
async def test_lease_stale_window_rejected(tmp_path: Any) -> None:
    from cctelegram.callback_dispatcher import STALE_CALLBACK_TEXT

    token, _f = _seed_row(tmp_path, window_id="@5")
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    # The topic no longer resolves to @5 (rebound) → lease rejects.
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot, live_window="@9")
    )
    assert STALE_CALLBACK_TEXT in _answers(query)
    assert bot.documents == []


@pytest.mark.asyncio
async def test_single_flight_contention(tmp_path: Any) -> None:
    token, _f = _seed_row(tmp_path)
    artifacts.begin_send(token)  # a concurrent tap already holds the flight
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert any("uploading" in a.lower() for a in _answers(query))
    assert bot.documents == []


@pytest.mark.asyncio
async def test_happy_path_uploads_open_file_object(tmp_path: Any) -> None:
    token, _f = _seed_row(tmp_path, data=b"REPORTBYTES", name="report.pdf")
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert len(bot.documents) == 1
    doc = bot.documents[0]
    assert doc["read"] == b"REPORTBYTES"
    assert doc["filename"] == "report.pdf"
    # ANSWER-FIRST: the callback was acked with an "Uploading" notice.
    assert any("uploading" in a.lower() for a in _answers(query))
    # Single-FLIGHT (not single-use): the row is live again.
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_time_revalidation_failure_pinned_roots(tmp_path: Any) -> None:
    # Roots PINNED at mint do NOT cover the file → open refuses → notice.
    token, _f = _seed_row(tmp_path, roots=(str((tmp_path / "elsewhere").resolve()),))
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert bot.documents == []
    assert any("upload failed" in m["text"].lower() for m in bot.messages)
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_pinned_roots_survive_recomputed_cwd(tmp_path: Any) -> None:
    # The executor never recomputes cwd — it revalidates against row.pinned_roots
    # ONLY. A row whose pinned roots cover the file uploads regardless of any
    # (absent) live cwd (codex r2 P2-1).
    token, _f = _seed_row(tmp_path, data=b"PINNED")
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot()
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert len(bot.documents) == 1 and bot.documents[0]["read"] == b"PINNED"


@pytest.mark.asyncio
async def test_retry_after_resets_row_and_notifies(tmp_path: Any) -> None:
    token, _f = _seed_row(tmp_path)
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot(doc_exc=RetryAfter(3))
    # RetryAfter is caught in the executor (send_document re-raises it) →
    # graceful notice, row back to live — never propagates.
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert bot.documents == []
    assert any("rate" in m["text"].lower() for m in bot.messages)
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_retry_after_notice_itself_rate_limited_never_raises(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """[fold item 3 — codex P3-1] the in-topic rate-limit notice can itself
    raise RetryAfter (safe_send re-raises it); the executor swallows + logs it
    best-effort — this lane never depends on PTB's error handler."""
    import logging

    token, _f = _seed_row(tmp_path)
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot(doc_exc=RetryAfter(3), msg_exc=RetryAfter(3))
    with caplog.at_level(
        logging.WARNING, logger="cctelegram.callback_dispatcher.artifacts"
    ):
        await artifacts_exec.execute_download_file_callback(
            _authorized(query, 1, 10), _adapters(bot)
        )
    assert bot.documents == [] and bot.messages == []
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]
    assert any("rate-limit notice" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_generic_send_failure_notifies(tmp_path: Any) -> None:
    token, _f = _seed_row(tmp_path)
    query = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    bot = _FakeBot(doc_exc=RuntimeError("telegram boom"))
    await artifacts_exec.execute_download_file_callback(
        _authorized(query, 1, 10), _adapters(bot)
    )
    assert bot.documents == []
    assert any("upload failed" in m["text"].lower() for m in bot.messages)
    assert artifacts.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_retap_after_content_change_reuploads(tmp_path: Any) -> None:
    token, f = _seed_row(tmp_path, data=b"V1")
    bot = _FakeBot()
    q1 = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    await artifacts_exec.execute_download_file_callback(
        _authorized(q1, 1, 10), _adapters(bot)
    )
    assert bot.documents[-1]["read"] == b"V1"
    # File content changes; a re-tap re-uploads the CURRENT bytes.
    f.write_bytes(b"V2-UPDATED")
    q2 = _FakeQuery(f"{CB_DOWNLOAD_FILE}@5:{token}")
    await artifacts_exec.execute_download_file_callback(
        _authorized(q2, 1, 10), _adapters(bot)
    )
    assert len(bot.documents) == 2
    assert bot.documents[-1]["read"] == b"V2-UPDATED"
