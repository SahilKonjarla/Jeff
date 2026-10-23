"""Finite subscription values and cursor kinds; no sockets or implicit stream lifetime."""

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import StreamFrame, SubscriptionQuery, SubscriptionRequest
from jeff.application.history import StreamCursor, StreamPage
from jeff.application.runtime import RuntimeIdentityData
from jeff.infrastructure.runtime_ipc import RpcRequest


def test_stream_cursor_is_canonical_strict_and_distinct_from_history() -> None:
    cursor = StreamCursor(
        project_id=uuid4(),
        canonical_root=Path("/p"),
        database_epoch=uuid4(),
        retention_generation=0,
        after=0,
    )
    assert StreamCursor.decode(cursor.encode()) == cursor
    raw = cursor.model_dump_json()
    for payload in (
        raw.replace("activity.v1", "history.v1"),
        raw[:-1] + ',"after":0}',
        raw.replace('"after":0', '"after":true'),
        raw.replace('"after":0', '"after":NaN'),
        raw.replace('"after":0', '"after":-1'),
    ):
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        with pytest.raises(ValueError):
            StreamCursor.decode(token)
    for token in ("", "!", "x" * 8193, cursor.encode() + "="):
        with pytest.raises(ValueError):
            StreamCursor.decode(token)


def test_subscription_envelope_is_finite_and_has_no_external_drain_override() -> None:
    query = SubscriptionQuery(directory=Path("/p"))
    request = SubscriptionRequest(expected_instance_id=uuid4(), query=query)
    assert isinstance(
        RpcRequest.model_validate_json(request.model_dump_json()).root, SubscriptionRequest
    )
    raw = json.loads(request.model_dump_json())
    raw["allow_during_drain"] = True
    with pytest.raises(ValidationError):
        RpcRequest.model_validate(raw)
    with pytest.raises(ValidationError):
        StreamFrame(
            kind="event",
            request_id=query.query_id,
            identity=RuntimeIdentityData(instance_id=uuid4(), pid=1),
            cursor="opaque",
        )


def test_exhausted_stream_page_must_advance_through_committed_interval() -> None:
    data = dict(
        project_id=uuid4(),
        canonical_root=Path("/p"),
        database_epoch=uuid4(),
        retention_generation=0,
        high_water=9,
        coverage_started_at_ms=1,
        start_after=2,
        scanned_through=9,
        events=(),
        has_more=False,
    )
    assert StreamPage.model_validate(data).scanned_through == 9
    for changed in ({"scanned_through": 2}, {"has_more": True}, {"start_after": 10}):
        with pytest.raises(ValidationError):
            StreamPage.model_validate({**data, **changed})
