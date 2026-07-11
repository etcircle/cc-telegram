"""Unit tests for bounded, retry-safe OpenAI voice transcription."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cctelegram import transcribe


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure each test starts with a fresh client."""
    transcribe._client = None
    yield
    transcribe._client = None


@pytest.fixture
def mock_config():
    """Patch config with test values."""
    with patch.object(transcribe, "config") as cfg:
        cfg.openai_api_key = "sk-test-key"
        cfg.openai_base_url = "https://api.openai.com/v1"
        yield cfg


def _mock_response(
    *,
    json_data: dict,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers,
        request=request,
    )


@pytest.mark.parametrize(
    ("duration_s", "expected"),
    [
        (None, 120.0),
        (0, 120.0),
        (-1, 120.0),
        ("x", 120.0),
        (True, 120.0),
        (False, 120.0),
        (7200, 120.0),
        (360, 600.0),
        (45, 120.0),
        (200, 400.0),
    ],
)
def test_transcription_budget_exact_clamps(duration_s: object, expected: float):
    assert transcribe.transcription_budget_s(duration_s) == expected


class TestTranscribeVoice:
    @pytest.mark.asyncio
    async def test_success_and_request_timeout(self, mock_config):
        resp = _mock_response(json_data={"text": "Hello world"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data", duration_s=200)

        assert result == "Hello world"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "Bearer sk-test-key" in str(call_kwargs)
        assert call_kwargs.kwargs["timeout"] == 400.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {}])
    async def test_empty_transcription_raises(self, mock_config, body):
        resp = _mock_response(json_data=body)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(ValueError, match="Empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_custom_base_url_and_trailing_slash(self, mock_config):
        mock_config.openai_base_url = "https://proxy.example.com/v1/"
        resp = _mock_response(json_data={"text": "Transcribed"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Transcribed"
        assert (
            mock_post.call_args.args[0]
            == "https://proxy.example.com/v1/audio/transcriptions"
        )

    @pytest.mark.asyncio
    async def test_wait_for_is_real_end_to_end_bound(self, mock_config, monkeypatch):
        stalled = asyncio.Event()

        async def stalled_post(*args, **kwargs):
            await stalled.wait()

        client = MagicMock()
        client.post = stalled_post
        monkeypatch.setattr(transcribe, "_get_client", lambda: client)
        monkeypatch.setattr(transcribe, "transcription_budget_s", lambda _value: 0.01)

        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await transcribe.transcribe_voice(b"fake-ogg-data", duration_s=360)
        assert time.monotonic() - started < 0.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first_failure", "expected_delay"),
        [
            (
                httpx.ConnectError(
                    "connect", request=httpx.Request("POST", "https://x")
                ),
                1.5,
            ),
            (
                httpx.ConnectTimeout(
                    "connect", request=httpx.Request("POST", "https://x")
                ),
                1.5,
            ),
            (None, 1.5),
            ("0", 0.0),
            ("3", 3.0),
            ("10", 10.0),
        ],
    )
    async def test_retry_matrix_and_exact_awaited_delay(
        self, mock_config, monkeypatch, first_failure, expected_delay
    ):
        success = _mock_response(json_data={"text": "second attempt"})
        if isinstance(first_failure, Exception):
            first = first_failure
        else:
            headers = {} if first_failure is None else {"Retry-After": first_failure}
            first = _mock_response(
                json_data={"error": "rate"}, status_code=429, headers=headers
            )

        post = AsyncMock(side_effect=[first, success])
        client = MagicMock(post=post)
        sleep = AsyncMock()
        monkeypatch.setattr(transcribe, "_get_client", lambda: client)
        monkeypatch.setattr(transcribe.asyncio, "sleep", sleep)

        assert await transcribe.transcribe_voice(b"ogg") == "second attempt"
        assert post.await_count == 2
        sleep.assert_awaited_once_with(expected_delay)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            "11",
            "30",
            "-1",
            "3.0",
            "soon",
            "Wed, 21 Oct 2015 07:28:00 GMT",
            httpx.ReadTimeout("read", request=httpx.Request("POST", "https://x")),
            httpx.WriteTimeout("write", request=httpx.Request("POST", "https://x")),
            httpx.RemoteProtocolError(
                "lost", request=httpx.Request("POST", "https://x")
            ),
            503,
            401,
            "empty",
        ],
    )
    async def test_does_not_retry_ambiguous_or_ineligible_failures(
        self, mock_config, monkeypatch, failure
    ):
        if failure == "empty":
            first = _mock_response(json_data={"text": ""})
            expected = ValueError
        elif isinstance(failure, int):
            first = _mock_response(json_data={"error": "no"}, status_code=failure)
            expected = httpx.HTTPStatusError
        elif isinstance(failure, str):
            first = _mock_response(
                json_data={"error": "rate"},
                status_code=429,
                headers={"Retry-After": failure},
            )
            expected = httpx.HTTPStatusError
        else:
            first = failure
            expected = type(failure)

        post = AsyncMock(
            side_effect=[first, _mock_response(json_data={"text": "bad retry"})]
        )
        client = MagicMock(post=post)
        sleep = AsyncMock()
        monkeypatch.setattr(transcribe, "_get_client", lambda: client)
        monkeypatch.setattr(transcribe.asyncio, "sleep", sleep)

        with pytest.raises(expected):
            await transcribe.transcribe_voice(b"ogg")
        assert post.await_count == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_retryable_failure_propagates(self, mock_config, monkeypatch):
        request = httpx.Request("POST", "https://x")
        post = AsyncMock(
            side_effect=[
                httpx.ConnectError("first", request=request),
                httpx.ConnectError("second", request=request),
            ]
        )
        monkeypatch.setattr(transcribe, "_get_client", lambda: MagicMock(post=post))
        monkeypatch.setattr(transcribe.asyncio, "sleep", AsyncMock())

        with pytest.raises(httpx.ConnectError, match="second"):
            await transcribe.transcribe_voice(b"ogg")
        assert post.await_count == 2


class TestCloseClient:
    @pytest.mark.asyncio
    async def test_close_client_when_open(self):
        transcribe._client = httpx.AsyncClient()
        assert transcribe._client is not None
        await transcribe.close_client()
        assert transcribe._client is None

    @pytest.mark.asyncio
    async def test_close_client_when_none(self):
        assert transcribe._client is None
        await transcribe.close_client()
        assert transcribe._client is None
