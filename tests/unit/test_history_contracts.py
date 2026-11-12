"""Strict finite history cursor/diagnostic wire contracts; no I/O."""

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import DebugQuery, EvidenceQuery, EvidenceRequest, HistoryQuery
from jeff.application.history import HistoryCursor


def test_history_cursor_roundtrip_and_rejects_ambiguous_payloads() -> None:
    cursor = HistoryCursor(
        project_id=uuid4(),
        canonical_root=Path("/p"),
        database_epoch=uuid4(),
        retention_generation=0,
        snapshot_high_water=12,
        before=8,
    )
    assert HistoryCursor.decode(cursor.encode()) == cursor
    for token in ("", "!", "x" * 8193, cursor.encode() + "=", "W10"):
        with pytest.raises(ValueError, match="Invalid history cursor"):
            HistoryCursor.decode(token)
    raw = cursor.model_dump_json()
    duplicate = raw[:-1] + ',"before":8}'
    for payload in (
        duplicate,
        raw.replace('"before":8', '"before":true'),
        raw.replace('"before":8', '"before":NaN'),
        raw.replace('"before":8', '"before":13'),
    ):
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        with pytest.raises(ValueError):
            HistoryCursor.decode(token)


def test_finite_queries_do_not_relax_event_replay_or_mix_cursor_kinds() -> None:
    for kwargs in (
        {"operation": "replay", "limit": 65},
        {"operation": "trace", "request_id": uuid4(), "cursor": "opaque"},
        {"operation": "debug", "after": 1},
        {"operation": "history", "request_id": uuid4()},
        {"operation": "history", "expected_epoch": uuid4()},
    ):
        with pytest.raises(ValidationError):
            EvidenceQuery.model_validate({"directory": "/p", **kwargs})
    assert HistoryQuery(directory=Path("/p")).limit == 50
    assert DebugQuery(directory=Path("/p")).request_id is None
    request = EvidenceRequest(
        query=HistoryQuery(directory=Path("/p")), expected_instance_id=uuid4()
    )
    assert json.loads(request.model_dump_json())["revision"] == 6
    assert EvidenceRequest.model_validate_json(request.model_dump_json()).query.limit == 50
    raw = json.loads(request.model_dump_json())
    del raw["query"]["limit"]
    assert EvidenceRequest.model_validate(raw).query.limit == 50
