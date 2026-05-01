"""Tests for the topic-targeted send/edit/delete classifier in message_sender.

The classifier converts Telegram error strings/types into ``TopicSendOutcome``
so that callers (status, content, activity, attention, interactive) can
distinguish a genuinely dead topic from a transient/format failure and route
to repair or emergency DM accordingly.
"""

from __future__ import annotations

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter

from ccbot.handlers.message_sender import TopicSendOutcome, _classify_bad_request


@pytest.mark.parametrize(
    "message",
    [
        "Message thread not found",
        "message thread not found",
        "Bad Request: message thread not found",
        "Topic_id_invalid",
        "TOPIC_ID_INVALID",
        "Bad Request: TOPIC_ID_INVALID",
        "Topic not found",
    ],
)
def test_classify_topic_not_found(message: str) -> None:
    assert (
        _classify_bad_request(BadRequest(message)) is TopicSendOutcome.TOPIC_NOT_FOUND
    )


@pytest.mark.parametrize(
    "message",
    [
        "Topic_closed",
        "TOPIC_CLOSED",
        "Bad Request: topic is closed",
    ],
)
def test_classify_topic_closed(message: str) -> None:
    assert _classify_bad_request(BadRequest(message)) is TopicSendOutcome.TOPIC_CLOSED


def test_classify_forbidden() -> None:
    assert (
        _classify_bad_request(Forbidden("Forbidden: bot was kicked"))
        is TopicSendOutcome.FORBIDDEN
    )


def test_classify_rate_limited() -> None:
    assert _classify_bad_request(RetryAfter(5)) is TopicSendOutcome.RATE_LIMITED


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: chat not found",
        "Bad Request: message to edit not found",
        "Bad Request: can't parse entities",
        "completely unknown error string",
    ],
)
def test_classify_other_bad_request(message: str) -> None:
    assert _classify_bad_request(BadRequest(message)) is TopicSendOutcome.OTHER


def test_classify_message_not_modified() -> None:
    # ``message is not modified`` is a benign no-op edit response — pinned to
    # its own outcome so attention.notify_waiting can short-circuit instead of
    # falling through to a fresh, audible card.
    assert (
        _classify_bad_request(BadRequest("Bad Request: message is not modified"))
        is TopicSendOutcome.MESSAGE_NOT_MODIFIED
    )


def test_classify_random_exception() -> None:
    assert (
        _classify_bad_request(RuntimeError("not a telegram error"))
        is TopicSendOutcome.OTHER
    )


def test_classify_outcome_values_are_stable() -> None:
    # Logged into launchd.err.log; downstream tooling parses these.
    assert TopicSendOutcome.OK.value == "OK"
    assert TopicSendOutcome.TOPIC_NOT_FOUND.value == "TOPIC_NOT_FOUND"
    assert TopicSendOutcome.TOPIC_CLOSED.value == "TOPIC_CLOSED"
    assert TopicSendOutcome.FORBIDDEN.value == "FORBIDDEN"
    assert TopicSendOutcome.RATE_LIMITED.value == "RATE_LIMITED"
    assert TopicSendOutcome.OTHER.value == "OTHER"
