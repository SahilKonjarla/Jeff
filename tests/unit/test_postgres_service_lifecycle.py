"""Exercise the service writer against transactional in-memory SQL doubles."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from jeff.application.compose_lifecycle import (
    ServiceAction,
    ServiceAuthorityOperation,
    ServiceAuthorityRecord,
    ServiceEvent,
)
from jeff.application.execution import ReviewFault
from jeff.infrastructure.postgres_execution import find_authority
from jeff.infrastructure.postgres_service_lifecycle import _attempt
from jeff.infrastructure.postgres_storage import PostgresStorage

from test_compose_lifecycle import _resources, _start, _stop


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[object, ...] | None:
        return next(iter(self.rows), None)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _Database:
    def __init__(self) -> None:
        self.authorities: dict[object, tuple[object, object]] = {}
        self.operations: dict[object, tuple[object, ...]] = {}
        self.attempts: dict[object, tuple[object, ...]] = {}
        self.events: dict[object, list[tuple[object, ...]]] = {}
        self.fail_terminal = False
        self.schema_ready = True

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _Rows:
        result: tuple[object, ...] | None = None
        if sql.startswith("SELECT to_regclass("):
            result = (self.schema_ready,)
        elif sql.startswith("SELECT 1 FROM jeff_proof.projects"):
            result = (1,)
        elif sql.startswith("SELECT digest, approval_id"):
            result = self.operations.get(params[0])
        elif sql.startswith("SELECT payload FROM jeff_proof.authorities WHERE "):
            if "WHERE claim_id=" in sql:
                result = next(((raw,) for claim, raw in self.authorities.values() if claim == params[0]), None)
            elif params[0] in self.authorities:
                result = (self.authorities[params[0]][1],)
        elif sql.startswith("SELECT 1 FROM jeff_proof.authorities"):
            result = (1,) if any(
                claim["claim_state"] in ("claimed", "unknown")
                for _, claim in self.authorities.values() if isinstance(claim, dict)
            ) else None
        elif sql.startswith("INSERT INTO jeff_proof.authorities"):
            self.authorities[params[0]] = (None, json.loads(str(params[1])))
        elif sql.startswith("UPDATE jeff_proof.authorities"):
            raw = json.loads(str(params[1]))
            if self.fail_terminal and raw["claim_state"] == "finished":
                raise RuntimeError("synthetic commit-path failure")
            self.authorities[params[2]] = (params[0], raw)
        elif sql.startswith("INSERT INTO jeff_proof.authority_ops"):
            self.operations[params[0]] = params[1:]
        elif sql.startswith("SELECT attempt,phase"):
            if prior := self.attempts.get(params[0]):
                result = (prior[2], prior[3])
        elif sql.startswith("SELECT 1 FROM jeff_proof.service_attempts"):
            result = (1,) if any(row[1] == params[0] for row in self.attempts.values()) else None
        elif sql.startswith("INSERT INTO jeff_proof.service_attempts"):
            self.attempts[params[0]] = (*params[1:3], json.loads(str(params[3])), params[4])
        elif sql.startswith("UPDATE jeff_proof.service_attempts"):
            self.attempts[params[1]] = (*self.attempts[params[1]][:-1], params[0])
        elif sql.startswith("SELECT ordinal,event"):
            return _Rows(self.events.get(params[0], []))
        elif sql.startswith("INSERT INTO jeff_proof.service_events"):
            self.events.setdefault(params[0], []).append((params[1], json.loads(str(params[2]))))
        else:
            raise AssertionError(sql)
        return _Rows([] if result is None else [result])

    @contextmanager
    def transaction(self, deadline: float, *, write: bool) -> Iterator["_Database"]:
        assert deadline >= time.monotonic()
        saved = deepcopy((self.authorities, self.operations, self.attempts, self.events))
        try:
            yield self
            if not write:
                assert saved == (self.authorities, self.operations, self.attempts, self.events)
        except BaseException:
            self.authorities, self.operations, self.attempts, self.events = saved
            raise


@pytest.mark.parametrize("terminal", ["running", "unknown"])
def test_service_claim_and_terminal_facts_share_writer_transaction(
    terminal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _Database()
    owner = uuid4()

    def no_connection() -> _Database:
        raise AssertionError("No PostgreSQL connection is approved")

    storage = PostgresStorage(no_connection, RuntimeError, owner)
    monkeypatch.setattr(storage, "_transaction", db.transaction)
    deadline = time.monotonic() + 10

    def claim(action: ServiceAction) -> ServiceAuthorityRecord:
        op = ServiceAuthorityOperation(
            operation="propose", request_id=uuid4(), project_id=action.target.project_id,
            canonical_root=action.target.root, owner_instance_id=owner, manifest=action,
            current_manifest_digest=action.digest,
        )
        proposed = storage.service_authority(op, deadline=deadline)
        with pytest.raises(ReviewFault, match="execution_not_found"):
            find_authority(db, proposed.record.approval_id)
        approve = ServiceAuthorityOperation.model_validate(
            op.model_dump() | dict(operation="approve", request_id=uuid4(), manifest=None,
                                  approval_id=proposed.record.approval_id)
        )
        storage.service_authority(approve, deadline=deadline)
        call = approve.model_copy(update=dict(operation="claim", request_id=uuid4()))
        fresh = storage.service_authority(call, deadline=deadline)
        replay = storage.service_authority(call, deadline=deadline)
        assert isinstance(fresh.launch_claim, UUID)
        assert replay.record == fresh.record and replay.launch_claim is None
        return fresh.record

    record = claim(_start())
    start = _attempt(record)
    with pytest.raises(ReviewFault):
        storage.record_service(
            start.model_copy(update={"claim_id": uuid4()}), ServiceEvent(phase="intent"),
            deadline=deadline,
        )
    storage.record_service(start, ServiceEvent(phase="intent"), deadline=deadline)
    with pytest.raises(ReviewFault):
        storage.record_service(start, ServiceEvent(phase="intent"), deadline=deadline)
    resources = _resources()
    storage.record_service(
        start, ServiceEvent(phase="created", resources=resources), deadline=deadline
    )
    result = ServiceEvent.model_validate(
        {"phase": terminal, "resources": resources if terminal == "running" else None}
    )
    if terminal == "running":
        db.fail_terminal = True
        with pytest.raises(RuntimeError, match="synthetic"):
            storage.record_service(start, result, deadline=deadline)
        assert len(db.events[start.job_id]) == 2 and db.attempts[start.job_id][-1] == "created"
        db.fail_terminal = False
    storage.record_service(start, result, deadline=deadline)
    assert db.attempts[start.job_id][-1] == terminal and len(db.events[start.job_id]) == 3
    with pytest.raises(ReviewFault):
        storage.record_service(start, result, deadline=deadline)
    if terminal == "unknown":
        with pytest.raises(ReviewFault, match="execution_outcome_unknown"):
            claim(_stop(start, resources))
    else:
        stop = _attempt(claim(_stop(start, resources)))
        storage.record_service(stop, ServiceEvent(phase="intent"), deadline=deadline)
        storage.record_service(
            stop, ServiceEvent(phase="stopped", resources=resources), deadline=deadline
        )
        with pytest.raises(ReviewFault, match="execution_conflict"):
            claim(_stop(start, resources))
