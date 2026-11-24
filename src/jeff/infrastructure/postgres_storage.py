"""Isolated PostgreSQL proof for registry, receipt activity and execution authority.

Not wired into the daemon. No live maintenance, Docker execution or SQLite
import. Use disposable state only.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Lock, get_ident
from typing import Protocol
from uuid import UUID, uuid4

from jeff.application.compose_lifecycle import (
    ServiceAttempt,
    ServiceAuthorityOperation,
    ServiceAuthorityResult,
    ServiceEvent,
)
from jeff.application.execution import (
    AuthorityOperation,
    AuthorityResult,
    DockerLaunchEvent,
    DockerLaunchScope,
    DockerLaunchSnapshot,
)
from jeff.application.history import (
    ActivityEvent,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    StreamPage,
    StreamRead,
)
from jeff.application.storage import (
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    RetirementBatch,
    StorageFault,
)
from jeff.domain.projects import Project

from . import postgres_activity as activity
from . import postgres_execution as execution
from . import postgres_service_lifecycle as services
from .postgres_results import RESULT_DDL, PostgresExecutionResults
from .postgres_watches import WATCH_DDL, PostgresWatches


class Cursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...


class Connection(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


_DDL = (
    "CREATE SCHEMA jeff_proof",
    "CREATE TABLE jeff_proof.projects "
    "(project_id uuid PRIMARY KEY, canonical_root text UNIQUE NOT NULL)",
    "CREATE TABLE jeff_proof.receipts (request_id uuid PRIMARY KEY, payload jsonb NOT NULL)",
    *activity.ACTIVITY_DDL,
    *execution.EXECUTION_DDL,
    *RESULT_DDL,
    *WATCH_DDL,
)


class PostgresStorage:
    """Writes use the original session holding the database advisory lock.

    The factory must return non-autocommit connections with a bounded connect
    timeout. A writer session belongs to the thread that acquired it.
    """

    def __init__(
        self,
        connect: Callable[[], Connection],
        database_error: type[Exception],
        owner_instance_id: UUID,
    ) -> None:
        self._connect = connect
        self._database_error = database_error
        self._owner = owner_instance_id
        self._writer: Connection | None = None
        self._writer_thread: int | None = None
        self._writer_mutex = Lock()
        self._launch_claims: set[UUID] = set()
        self._proposal_deadlines: dict[UUID, float] = {}
        self.results = PostgresExecutionResults(
            self._operation, owner_instance_id, self._launch_claims
        )
        self.watches = PostgresWatches(self._operation, owner_instance_id)

    @property
    def instance_id(self) -> UUID:
        return self._owner

    def _operation(self, deadline: float, write: bool) -> AbstractContextManager[Connection]:
        return self._transaction(deadline, write=write)

    @contextmanager
    def writer(self) -> Iterator[None]:
        """Hold one cooperative writer lease; never reconnect within that lease."""
        if not self._writer_mutex.acquire(blocking=False):
            raise StorageFault("storage_busy")
        connection = None
        try:
            connection = self._connect()
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (0x4A45464650475246,)
            ).fetchone()
            if row != (True,):
                raise StorageFault("storage_busy")
            connection.rollback()  # The session lock survives transaction rollback.
            self._writer, self._writer_thread = connection, get_ident()
            yield
        except self._database_error:
            raise StorageFault("storage_failed") from None
        finally:
            self._writer, self._writer_thread = None, None
            self._launch_claims.clear()
            self._proposal_deadlines.clear()
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._writer_mutex.release()

    @contextmanager
    def _transaction(self, deadline: float | None, *, write: bool) -> Iterator[Connection]:
        end = time.monotonic() + 5 if deadline is None else deadline
        if time.monotonic() >= end:
            raise StorageFault("storage_busy")
        connection = None
        committing = False
        try:
            if write:
                if self._writer is None or self._writer_thread != get_ident():
                    raise StorageFault("storage_closing")
                connection = self._writer
            else:
                connection = self._connect()
            remaining = int((end - time.monotonic()) * 1000)
            if remaining <= 0:
                raise StorageFault("storage_busy")
            if not write:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)", (str(min(remaining, 5000)),)
            )
            yield connection
            if write:
                if time.monotonic() >= end:
                    raise StorageFault("storage_busy")
                committing = True
                connection.commit()
                if time.monotonic() >= end:
                    raise StorageFault("storage_outcome_unconfirmed")
        except self._database_error:
            if write and connection is not None:
                self._writer = None
                connection.close()
            raise StorageFault(
                "storage_outcome_unconfirmed" if committing else "storage_failed"
            ) from None
        except Exception:
            if write and connection is not None:
                if committing:
                    self._writer = None
                    connection.close()
                else:
                    try:
                        connection.rollback()
                    except self._database_error:
                        self._writer = None
                        connection.close()
            raise
        finally:
            if not write and connection is not None:
                connection.close()

    def provision(self, *, deadline: float) -> None:
        """Create the proof tables explicitly; an existing schema is never reset."""
        with self._transaction(deadline, write=True) as connection:
            for statement in _DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO jeff_proof.activity (singleton,database_epoch,coverage_started_at_ms) "
                "VALUES (true,%s,%s)",
                (uuid4(), time.time_ns() // 1000000),
            )

    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
        mono = time.monotonic()
        now = time.time_ns() // 1_000_000
        with self._transaction(deadline, write=operation.operation != "show") as connection:
            record, fresh = execution.decide(
                connection,
                operation,
                self._owner,
                now,
                live_proposals={key for key, end in self._proposal_deadlines.items() if mono < end},
            )
        # Only the original session's successfully acknowledged commit reaches this point.
        if fresh and operation.operation == "propose":
            self._proposal_deadlines[record.approval_id] = mono + execution.TTL_MS / 1000
        if fresh and operation.operation == "claim":
            assert record.claim_id is not None
            self._launch_claims.add(record.claim_id)
            return AuthorityResult(record=record, launch_claim=record.claim_id)
        return AuthorityResult(record=record)

    def provision_service_journal(self, *, deadline: float) -> None:
        """Explicit additive migration; never invoked by startup or discovery."""
        services.provision(self, deadline=deadline)

    def service_authority(
        self, operation: ServiceAuthorityOperation, *, deadline: float
    ) -> ServiceAuthorityResult:
        return services.authority(self, operation, deadline=deadline)

    def record_service(self, attempt: ServiceAttempt, event: ServiceEvent, *, deadline: float) -> None:
        services.record(self, attempt, event, deadline=deadline)

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
        record = event.intent.record
        now = time.time_ns() // 1_000_000
        with self._transaction(deadline, write=True) as connection:
            execution.append_launch(
                connection,
                event,
                self._owner,
                self._launch_claims,
                allow_mutation=(
                    time.monotonic() < self._proposal_deadlines.get(record.approval_id, 0)
                    and record.created_at_ms <= now < record.expires_at_ms
                ),
            )

    def launch_status(self, scope: DockerLaunchScope, *, deadline: float) -> DockerLaunchSnapshot:
        with self._transaction(deadline, write=False) as connection:
            return execution.launch_status(connection, scope)

    def fence_prior_execution(self, prior_owner: UUID, *, deadline: float) -> int:
        """Under the writer lock, quarantine prior-owner claims without reviving them."""
        if prior_owner == self._owner:
            raise StorageFault("receipt_conflict")
        with self._transaction(deadline, write=True) as connection:
            return execution.fence(connection, prior_owner)

    def history(self, scope: HistoryRead, *, deadline: float) -> HistoryPage:
        with self._transaction(deadline, write=False) as connection:
            return activity.history(connection, scope)

    def stream_page(self, scope: StreamRead, *, deadline: float) -> StreamPage:
        with self._transaction(deadline, write=False) as connection:
            return activity.stream(connection, scope)

    def observe(self, record: ActivityRecord, *, deadline: float) -> ActivityEvent:
        with self._transaction(deadline, write=True) as connection:
            activity.meta(connection, locked=True)
            receipt = activity.receipt_for(connection, record.request_id, locked=True)
            if (
                receipt is None
                or receipt.state != "running"
                or receipt.owner_instance_id != self._owner
                or record.owner_instance_id != self._owner
            ):
                raise StorageFault("receipt_conflict")
            return activity.append_event(
                connection,
                receipt,
                kind="git.finished" if record.git else "registration.observed",
                code=record.git.code if record.git else None,
                at=time.time_ns() // 1000000,
                observation=record,
            )

    def trace(
        self, scope: ReceiptScope, *, deadline: float
    ) -> tuple[Receipt | None, ActivityTrace | None]:
        with self._transaction(deadline, write=False) as connection:
            receipt = activity.scoped_receipt(connection, scope)
            return (
                (None, None) if receipt is None else (receipt, activity.trace(connection, receipt))
            )

    def debug(self, scope: HistoryScope, *, deadline: float) -> tuple[Receipt, ActivityTrace]:
        with self._transaction(deadline, write=False) as connection:
            request_id = activity.latest_request(connection, scope)
            receipt = activity.scoped_receipt(
                connection,
                ReceiptScope(
                    request_id=request_id,
                    project_id=scope.project_id,
                    canonical_root=scope.canonical_root,
                ),
            )
            if receipt is None:
                raise StorageFault("storage_unrecognized")
            return receipt, activity.trace(connection, receipt)

    def replay(self, scope: ReplayScope, *, deadline: float) -> ActivityPage:
        with self._transaction(deadline, write=False) as connection:
            return activity.replay(connection, scope)

    def retire_before(self, cutoff: int, *, deadline: float) -> RetirementBatch:
        """Explicit proof operation; the live scheduler and clock guards are not ported."""
        if type(cutoff) is not int or not 0 <= cutoff <= time.time_ns() // 1000000:
            raise StorageFault("storage_busy")
        with self._transaction(deadline, write=True) as connection:
            return activity.retire(connection, cutoff)

    def reconcile_prior_owner(self, prior_owner: UUID, *, deadline: float) -> int:
        """Proof helper: caller must already have fenced the named prior writer."""
        if prior_owner == self._owner:
            raise StorageFault("receipt_conflict")
        with self._transaction(deadline, write=True) as connection:
            return activity.reconcile(connection, prior_owner, time.time_ns() // 1000000)

    @staticmethod
    def _project(connection: Connection, project: Project) -> bool:
        pair = (project.project_id, str(project.root))
        rows = connection.execute(
            "SELECT project_id,canonical_root FROM jeff_proof.projects "
            "WHERE project_id=%s OR canonical_root=%s",
            pair,
        ).fetchall()
        if rows and rows != [pair]:
            raise StorageFault("registry_conflict")
        return bool(rows)

    def index(self, project: Project, *, deadline: float | None = None) -> tuple[Project, bool]:
        with self._transaction(deadline, write=True) as connection:
            inserted = connection.execute(
                "INSERT INTO jeff_proof.projects VALUES (%s,%s) ON CONFLICT DO NOTHING "
                "RETURNING project_id",
                (project.project_id, str(project.root)),
            ).fetchone()
            if inserted is None and not self._project(connection, project):
                raise StorageFault("registry_conflict")
            return project, inserted is not None

    def lookup(self, project: Project, *, deadline: float | None = None) -> Project | None:
        with self._transaction(deadline, write=False) as connection:
            return project if self._project(connection, project) else None

    def root_claimed(self, root: Path, *, deadline: float | None = None) -> bool:
        with self._transaction(deadline, write=False) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM jeff_proof.projects WHERE canonical_root=%s", (str(root),)
                ).fetchone()
                is not None
            )

    def claim(self, claim: ReceiptClaim, *, deadline: float) -> ReceiptClaimResult:
        with self._transaction(deadline, write=True) as connection:
            if claim.owner_instance_id != self._owner:
                raise StorageFault("receipt_conflict")
            activity.meta(connection, locked=True)
            if (
                connection.execute(
                    "SELECT 1 FROM jeff_proof.tombstones WHERE request_id=%s", (claim.request_id,)
                ).fetchone()
                is not None
            ):
                raise StorageFault("request_retired")
            candidate = Receipt(
                **claim.model_dump(), state="accepted", accepted_at_ms=time.time_ns() // 1000000
            )
            inserted = connection.execute(
                "INSERT INTO jeff_proof.receipts VALUES (%s,%s::jsonb) ON CONFLICT DO NOTHING "
                "RETURNING request_id",
                (claim.request_id, candidate.model_dump_json()),
            ).fetchone()
            if inserted is not None:
                activity.append_event(
                    connection, candidate, kind="request.accepted", at=candidate.accepted_at_ms
                )
                return ReceiptClaimResult(created=True, receipt=candidate)
            prior = activity.receipt_for(connection, claim.request_id)
            if prior is None or (
                prior.operation != claim.operation
                or prior.canonical_root != claim.canonical_root
                or (
                    prior.project_id != claim.project_id
                    and not (prior.operation == "init" and prior.project_id is None)
                )
            ):
                raise StorageFault("receipt_conflict")
            return ReceiptClaimResult(created=False, receipt=prior)

    def inspect(self, scope: ReceiptScope, *, deadline: float) -> Receipt | None:
        with self._transaction(deadline, write=False) as connection:
            return activity.scoped_receipt(connection, scope)

    def finish(self, transition: ReceiptTransition, *, deadline: float) -> Receipt:
        with self._transaction(deadline, write=True) as connection:
            activity.meta(connection, locked=True)
            prior = activity.receipt_for(connection, transition.request_id, locked=True)
            if (
                prior is None
                or prior.owner_instance_id != self._owner
                or transition.owner_instance_id != self._owner
                or prior.state not in {"accepted", "running"}
            ):
                raise StorageFault("receipt_conflict")
            associating = (
                prior.operation == "init"
                and prior.project_id is None
                and prior.state == transition.state == "running"
                and transition.project_id is not None
            )
            if not associating and (
                transition.project_id is not None
                or (transition.state == "running" and prior.state != "accepted")
            ):
                raise StorageFault("receipt_conflict")
            receipt = Receipt.model_validate(
                {
                    **prior.model_dump(),
                    "project_id": transition.project_id if associating else prior.project_id,
                    "state": transition.state,
                    "finished_at_ms": None
                    if transition.state == "running"
                    else time.time_ns() // 1000000,
                    "outcome_code": transition.outcome_code,
                }
            )
            connection.execute(
                "UPDATE jeff_proof.receipts SET payload=%s::jsonb WHERE request_id=%s",
                (receipt.model_dump_json(), receipt.request_id),
            )
            activity.append_event(
                connection,
                receipt,
                kind="identity.established" if associating else f"request.{receipt.state}",
                at=time.time_ns() // 1000000,
                code=transition.error_code or receipt.outcome_code,
            )
            return receipt
