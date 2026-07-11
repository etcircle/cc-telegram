"""Bounded voice-to-text transcription via OpenAI's audio API.

Transcribes with the gpt-4o-transcribe model through httpx, applying a
duration-scaled end-to-end deadline and one pre-send-safe retry.

Key function: transcribe_voice(ogg_data, duration_s=...) -> str
"""

import asyncio
import logging
import re

import httpx

from .config import config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

_MIN_ATTEMPT_BUDGET_S = 120.0
_MAX_ATTEMPT_BUDGET_S = 600.0
_MAX_ADVISORY_DURATION_S = 3600
_DEFAULT_RETRY_DELAY_S = 1.5
_MAX_RETRY_AFTER_S = 10
_INTEGER_RETRY_AFTER_RE = re.compile(r"[0-9]+")


def _get_client() -> httpx.AsyncClient:
    """Return a lazily-initialized httpx client singleton."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


def transcription_budget_s(duration_s: object) -> float:
    """Return the clamped per-attempt budget for an advisory duration hint."""
    if isinstance(duration_s, bool):
        return _MIN_ATTEMPT_BUDGET_S
    if not isinstance(duration_s, int):
        return _MIN_ATTEMPT_BUDGET_S
    if duration_s <= 0 or duration_s > _MAX_ADVISORY_DURATION_S:
        return _MIN_ATTEMPT_BUDGET_S
    return min(
        _MAX_ATTEMPT_BUDGET_S,
        max(_MIN_ATTEMPT_BUDGET_S, 2.0 * duration_s),
    )


def _retry_delay(error: Exception) -> float | None:
    """Return the safe retry delay, or None when retry is not permitted."""
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return _DEFAULT_RETRY_DELAY_S
    if not isinstance(error, httpx.HTTPStatusError):
        return None
    if error.response.status_code != 429:
        return None

    value = error.response.headers.get("Retry-After")
    if value is None:
        return _DEFAULT_RETRY_DELAY_S
    if _INTEGER_RETRY_AFTER_RE.fullmatch(value) is None:
        return None
    delay = int(value)
    if delay > _MAX_RETRY_AFTER_S:
        return None
    return float(delay)


async def transcribe_voice(ogg_data: bytes, *, duration_s: int | None = None) -> str:
    """Transcribe OGG voice data to text via OpenAI API.

    Raises:
        TimeoutError: When an attempt exceeds its end-to-end budget.
        httpx.HTTPStatusError: On API errors after any eligible retry.
        httpx.TransportError: On transport errors after any eligible retry.
        ValueError: If the API returns an empty transcription.
    """
    url = f"{config.openai_base_url.rstrip('/')}/audio/transcriptions"
    client = _get_client()
    budget_s = transcription_budget_s(duration_s)

    async def attempt() -> str:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            files={"file": ("voice.ogg", ogg_data, "audio/ogg")},
            data={"model": "gpt-4o-transcribe"},
            timeout=budget_s,
        )
        response.raise_for_status()

        text = response.json().get("text", "").strip()
        if not text:
            raise ValueError("Empty transcription returned by API")
        return text

    try:
        return await asyncio.wait_for(attempt(), timeout=budget_s)
    except Exception as first_error:
        delay = _retry_delay(first_error)
        if delay is None:
            raise

    await asyncio.sleep(delay)
    return await asyncio.wait_for(attempt(), timeout=budget_s)


async def close_client() -> None:
    """Close the httpx client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
