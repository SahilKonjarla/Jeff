"""Private SQLite registry with one connection-owning thread and bounded declared work.

Python SQLite opens a pathname, not our checked descriptor. Private ancestry plus
before/after identity checks narrow races; this is not a hostile same-user/ACL proof.
"""

import json
import math
import os
import queue
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    DockerBackendConfiguration,
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchScope,
    DockerLaunchSnapshot,
    DockerOutputFacts,
    DockerOutputSnapshot,
    DockerOwnedContainer,
    ExecutionCancelCall,
    ExecutionData,
    ExecutionInspection,
    ExecutionInspectionScope,
    ExecutionManifest,
    ExecutionProfile,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    ExecutionStopTerminal,
    ExecutionStopView,
    ProposalRequestScope,
    ReviewFault,
    ReviewOperation,
    ReviewRecord,
)
from jeff.application.history import (
    ACTIVITY_EVENT_LIMIT,
    INT64_MAX,
    ActivityEvent,
    ActivityMeta,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryCursor,
    HistoryItem,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    RetentionPosition,
    StreamCursor,
    StreamPage,
    StreamRead,
    activity_code,
)
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    DATABASE_SCHEMA,
    RETIREMENT_BATCH_LIMIT,
    RETIREMENT_INTERVAL_SECONDS,
    STORAGE_PENDING_LIMIT,
    ConfiguredRuntimeSettings,
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    RetentionProgress,
    RetirementBatch,
    StorageFault,
    StorageIssueCode,
    StorageReadiness,
)
from jeff.domain.execution import (
    LIVE_REVIEW_LIMIT,
    REVIEW_RECORD_LIMIT,
    REVIEW_TTL_SECONDS,
    AuthorityState,
    ReviewState,
    canonical_bytes,
    fingerprint,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import (
    RECOVERY_DATABASE_BYTES,
    RECOVERY_JOURNAL_BYTES,
    DiagnosticLease,
    Ownership,
)

_LEDGER = "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
_PROJECTS = (
    "CREATE TABLE projects (project_id TEXT NOT NULL PRIMARY KEY, "
    "canonical_root TEXT NOT NULL UNIQUE)"
)
_RECEIPTS = (
    "CREATE TABLE request_receipts (request_id TEXT NOT NULL PRIMARY KEY, "
    "owner_instance_id TEXT NOT NULL, operation TEXT NOT NULL "
    "CHECK(operation IN ('init','ask.branch','ask.changes')), "
    "canonical_root TEXT NOT NULL, project_id TEXT, "
    "state TEXT NOT NULL CHECK(state IN ('accepted','running','succeeded','failed','unknown')), "
    "accepted_at_ms INTEGER NOT NULL CHECK(accepted_at_ms>=0), finished_at_ms INTEGER, "
    "outcome_code TEXT CHECK(outcome_code IN "
    "('ok','operation_failed','timeout','interrupted','partial','target_mismatch')), "
    "CHECK(project_id IS NOT NULL OR operation='init'), "
    "CHECK((state IN ('accepted','running') AND finished_at_ms IS NULL "
    "AND outcome_code IS NULL) OR (state IN ('succeeded','failed','unknown') "
    "AND finished_at_ms IS NOT NULL AND outcome_code IS NOT NULL)))"
)
_RECEIPT_INDEX = "CREATE INDEX receipts_unfinished ON request_receipts(state,owner_instance_id)"
_COLUMNS = (
    "request_id,owner_instance_id,operation,canonical_root,project_id,"
    "state,accepted_at_ms,finished_at_ms,outcome_code"
)
_ACTIVITY_META = (
    "CREATE TABLE activity_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "epoch TEXT NOT NULL, "
    "coverage_started_at_ms INTEGER NOT NULL CHECK(coverage_started_at_ms>=0), "
    "high_water INTEGER NOT NULL CHECK(high_water>=0), "
    "generation INTEGER NOT NULL CHECK(generation>=0))"
)
_FLOOR_COLUMN = "replay_floor INTEGER NOT NULL DEFAULT 0 CHECK(replay_floor>=0)"
_ACTIVITY_META_V4 = _ACTIVITY_META[:-1] + ", " + _FLOOR_COLUMN + ")"
_TOMBSTONES = "CREATE TABLE request_tombstones (request_id TEXT NOT NULL PRIMARY KEY)"
_EXPIRABLE_INDEX = (
    "CREATE INDEX receipts_expirable ON request_receipts(finished_at_ms,request_id) "
    "WHERE state IN ('succeeded','failed')"
)
_ACTIVITY_REQUESTS = (
    "CREATE TABLE activity_requests ("
    "request_id TEXT PRIMARY KEY REFERENCES request_receipts(request_id), "
    "canonical_root TEXT NOT NULL, project_id TEXT, accepted_sequence INTEGER NOT NULL UNIQUE)"
)
_ACTIVITY_EVENTS = (
    "CREATE TABLE activity_events (event_id INTEGER PRIMARY KEY, "
    "request_id TEXT NOT NULL REFERENCES request_receipts(request_id), "
    "owner_instance_id TEXT NOT NULL, "
    "canonical_root TEXT NOT NULL, project_id TEXT, scope_project_id TEXT, kind TEXT NOT NULL, "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), code TEXT, git_category TEXT, "
    "elapsed_ms INTEGER, output_bytes INTEGER, git_started INTEGER CHECK(git_started IN (0,1)), "
    "registration_created INTEGER CHECK(registration_created IN (0,1)))"
)
_ACTIVITY_INDEXES = {
    "activity_trace": "CREATE INDEX activity_trace ON activity_events(request_id,event_id)",
    "activity_replay": (
        "CREATE INDEX activity_replay ON activity_events(scope_project_id,canonical_root,event_id)"
    ),
    "activity_history": (
        "CREATE INDEX activity_history "
        "ON activity_requests(project_id,canonical_root,accepted_sequence)"
    ),
}
_EVENT_COLUMNS = (
    "event_id,request_id,owner_instance_id,canonical_root,project_id,kind,recorded_at_ms,"
    "code,git_category,elapsed_ms,output_bytes,git_started,registration_created"
)
_REVIEWS = (
    "CREATE TABLE execution_reviews (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "proposal_id TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL UNIQUE, "
    "job_id TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, owner_instance_id TEXT NOT NULL, "
    "manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB))<=32768), "
    "manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64), "
    "configuration_digest TEXT NOT NULL CHECK(length(configuration_digest)=64), "
    "state TEXT NOT NULL CHECK(state IN ('pending','approved','denied','expired','invalidated')), "
    "created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), "
    "expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=created_at_ms))"
)
_REVIEW_OPS = (
    "CREATE TABLE execution_operations (request_id TEXT PRIMARY KEY, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, operation TEXT NOT NULL "
    "CHECK(operation IN ('propose','approve','deny')), payload_digest TEXT NOT NULL "
    "CHECK(length(payload_digest)=64), proposal_id TEXT NOT NULL, "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "outcome_state TEXT NOT NULL CHECK(outcome_state IN ('pending','approved','denied')))"
)
_REVIEW_COLUMNS = (
    "sequence,proposal_id,approval_id,job_id,project_id,canonical_root,owner_instance_id,"
    "manifest_json,manifest_digest,configuration_digest,state,created_at_ms,expires_at_ms"
)
_AUTHORITIES = (
    "CREATE TABLE execution_authorities (proposal_id TEXT PRIMARY KEY, "
    "approval_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL UNIQUE, "
    "project_id TEXT NOT NULL, canonical_root TEXT NOT NULL, owner_instance_id TEXT NOT NULL, "
    "manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB))<=32768), "
    "manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64), "
    "state TEXT NOT NULL CHECK(state IN "
    "('pending','approved','denied','expired','invalidated','consumed')), "
    "created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0), "
    "expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms>=created_at_ms), "
    "claim_id TEXT UNIQUE, claim_state TEXT "
    "CHECK(claim_state IN ('claimed','unknown','finished')), "
    "claimed_at_ms INTEGER, CHECK((state='consumed' AND claim_id IS NOT NULL "
    "AND claim_state IS NOT NULL AND claimed_at_ms IS NOT NULL "
    "AND claimed_at_ms>=created_at_ms) OR "
    "(state!='consumed' AND claim_id IS NULL AND claim_state IS NULL AND claimed_at_ms IS NULL)))"
)
_AUTHORITY_OPS = (
    "CREATE TABLE execution_authority_operations (request_id TEXT PRIMARY KEY, "
    "payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), "
    "proposal_id TEXT NOT NULL REFERENCES execution_authorities(proposal_id))"
)
_AUTHORITY_COLUMNS = (
    "proposal_id,approval_id,job_id,project_id,canonical_root,owner_instance_id,manifest_json,"
    "manifest_digest,state,created_at_ms,expires_at_ms,claim_id,claim_state,claimed_at_ms"
)
_LAUNCHES = (
    "CREATE TABLE execution_launches (attempt_id TEXT PRIMARY KEY, "
    "claim_id TEXT NOT NULL UNIQUE REFERENCES execution_authorities(claim_id), "
    "intent_json TEXT NOT NULL CHECK(length(CAST(intent_json AS BLOB))<=131072), "
    "intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64), "
    "container_id TEXT, created_at TEXT, observed_container_id TEXT, exit_code INTEGER, "
    "CHECK((container_id IS NULL)=(created_at IS NULL)), "
    "CHECK(container_id IS NULL OR length(container_id)=64), "
    "CHECK(created_at IS NULL OR length(created_at)<=64), "
    "CHECK(observed_container_id IS NULL OR length(observed_container_id)=64), "
    "CHECK(exit_code IS NULL OR exit_code BETWEEN 0 AND 255))"
)
_LAUNCH_EVENTS = (
    "CREATE TABLE execution_launch_events (attempt_id TEXT NOT NULL "
    "REFERENCES execution_launches(attempt_id), phase TEXT NOT NULL "
    "CHECK(phase IN ('create_intent','created','start_intent','exited','cleaned','unknown')), "
    "sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 6), "
    "event_digest TEXT NOT NULL CHECK(length(event_digest)=64), "
    "PRIMARY KEY(attempt_id,phase), UNIQUE(attempt_id,sequence))"
)
_RESULTS = (
    "CREATE TABLE execution_results (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_launches(attempt_id), "
    "disposition TEXT NOT NULL CHECK(disposition IN ('completed','quarantined')), "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "metadata_json TEXT NOT NULL CHECK(length(CAST(metadata_json AS BLOB))<=8192), "
    "completion_digest TEXT NOT NULL CHECK(length(completion_digest)=64), "
    "stdout TEXT, stderr TEXT, retirement_reason TEXT "
    "CHECK(retirement_reason IN ('age','capacity')), "
    "CHECK((stdout IS NULL)=(stderr IS NULL)), "
    "CHECK((stdout IS NULL)=(retirement_reason IS NOT NULL)), "
    "CHECK(stdout IS NULL OR length(CAST(stdout AS BLOB))+length(CAST(stderr AS BLOB))<=1048576))"
)
_RESULT_PAYLOADS = (
    "CREATE INDEX execution_result_payloads ON execution_results(sequence) WHERE stdout IS NOT NULL"
)
_RESULT_QUARANTINE = (
    "CREATE UNIQUE INDEX execution_result_quarantine ON execution_results(disposition) "
    "WHERE disposition='quarantined' AND stdout IS NOT NULL"
)
_STOP_PAYLOADS = (
    "CREATE TABLE execution_stop_payloads ("
    "proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES execution_lifecycles(proposal_id), "
    "disposition TEXT NOT NULL CHECK(disposition IN ('known','quarantined')), "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), "
    "clock_confirmed INTEGER NOT NULL CHECK(clock_confirmed IN (0,1)), "
    "output_facts_json TEXT NOT NULL CHECK(length(CAST(output_facts_json AS BLOB))<=8192), "
    "output_digest TEXT NOT NULL CHECK(length(output_digest)=64), "
    "stdout TEXT, stderr TEXT, retirement_reason TEXT, "
    "CHECK((stdout IS NULL AND stderr IS NULL AND retirement_reason IS NOT NULL "
    "AND retirement_reason IN ('age','capacity')) OR "
    "(stdout IS NOT NULL AND stderr IS NOT NULL AND retirement_reason IS NULL "
    "AND length(CAST(stdout AS BLOB))+length(CAST(stderr AS BLOB))<=1048576)))"
)
_LIFECYCLES = (
    "CREATE TABLE execution_lifecycles (proposal_id TEXT PRIMARY KEY "
    "REFERENCES execution_authorities(proposal_id), record_json TEXT NOT NULL "
    "CHECK(length(CAST(record_json AS BLOB))<=32768), record_digest TEXT NOT NULL "
    "CHECK(length(record_digest)=64))"
)
_SIDECARS = ("state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm")
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink


def _fault(error: Exception, fallback: StorageIssueCode = "storage_failed") -> StorageFault:
    if isinstance(error, StorageFault):
        return error
    if isinstance(error, sqlite3.Error) and getattr(error, "sqlite_errorcode", 0) & 255 in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    ):
        return StorageFault("storage_busy")
    return StorageFault(fallback)


def _guard(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA trusted_schema=OFF")
    if connection.execute("PRAGMA trusted_schema").fetchone() != (0,):
        raise StorageFault("storage_failed")
    # Builds without the Python extension-loading API cannot enable it through that
    # API. The SQL authorizer denies load_extension regardless of build configuration.
    if hasattr(connection, "enable_load_extension"):
        connection.enable_load_extension(False)

    def authorize(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        if (
            action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
            or (action == sqlite3.SQLITE_FUNCTION and (second or "").lower() == "load_extension")
            or (action == sqlite3.SQLITE_PRAGMA and (first or "").lower() == "writable_schema")
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    connection.execute(f"PRAGMA busy_timeout={DEFAULT_RUNTIME_LIMITS.sqlite_busy_seconds * 1000}")
    connection.execute("PRAGMA foreign_keys=ON")


def _schema(connection: sqlite3.Connection) -> int:
    if connection.execute("PRAGMA application_id").fetchone() != (DATABASE_APPLICATION_ID,):
        raise StorageFault("storage_unrecognized")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, DATABASE_SCHEMA):
        raise StorageFault("storage_schema_unsupported")
    if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
        raise StorageFault("storage_recovery_required")
    expected = {"schema_migrations": ("table", _LEDGER)}
    if version >= 1:
        expected["projects"] = ("table", _PROJECTS)
    if version >= 2:
        expected["request_receipts"] = ("table", _RECEIPTS)
        expected["receipts_unfinished"] = ("index", _RECEIPT_INDEX)
    if version >= 3:
        expected.update(
            {
                "activity_meta": ("table", _ACTIVITY_META_V4 if version >= 4 else _ACTIVITY_META),
                "activity_requests": ("table", _ACTIVITY_REQUESTS),
                "activity_events": ("table", _ACTIVITY_EVENTS),
            }
        )
        expected.update({name: ("index", sql) for name, sql in _ACTIVITY_INDEXES.items()})
    if version >= 4:
        expected["request_tombstones"] = ("table", _TOMBSTONES)
        expected["receipts_expirable"] = ("index", _EXPIRABLE_INDEX)
    if version >= 5:
        expected["execution_reviews"] = ("table", _REVIEWS)
        expected["execution_operations"] = ("table", _REVIEW_OPS)
    if version >= 6:
        expected["execution_authorities"] = ("table", _AUTHORITIES)
        expected["execution_authority_operations"] = ("table", _AUTHORITY_OPS)
    if version >= 7:
        expected["execution_launches"] = ("table", _LAUNCHES)
        expected["execution_launch_events"] = ("table", _LAUNCH_EVENTS)
    if version >= 8:
        expected["execution_results"] = ("table", _RESULTS)
        expected["execution_result_payloads"] = ("index", _RESULT_PAYLOADS)
        expected["execution_result_quarantine"] = ("index", _RESULT_QUARANTINE)
    if version >= 9:
        expected["execution_lifecycles"] = ("table", _LIFECYCLES)
    if version >= 10:
        expected["execution_stop_payloads"] = ("table", _STOP_PAYLOADS)
    found = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT ?",
        (len(expected) + 1,),
    ).fetchall()

    def normalize(text: object) -> str:
        return " ".join(str(text).upper().split())

    if len(found) != len(expected) or any(
        name not in expected
        or kind != expected[name][0]
        or normalize(sql) != normalize(expected[name][1])
        for kind, name, sql in found
    ):
        raise StorageFault("storage_schema_unsupported")
    ledger = connection.execute(
        "SELECT version,name FROM schema_migrations ORDER BY version LIMIT ?", (version + 1,)
    ).fetchall()
    if ledger != [
        (number, name)
        for number, name in (
            (1, "registry_v1"),
            (2, "request_receipts_v2"),
            (3, "activity_v3"),
            (4, "retention_v4"),
            (5, "execution_review_v5"),
            (6, "execution_authority_v6"),
            (7, "execution_launch_v7"),
            (8, "execution_results_v8"),
            (9, "execution_lifecycle_v9"),
            (10, "execution_stop_payloads_v10"),
        )
        if number <= version
    ]:
        raise StorageFault("storage_schema_unsupported")
    return version


def inspect_stopped_storage(lease: DiagnosticLease, deadline: float) -> int:
    """Only stable sidecar-free rollback storage; no owner thread, writes or migration.

    Reuses actual marker/catalog/ledger policy, not a parallel schema approximation.
    Pathname-open race limits remain the same as the existing storage preflight.
    """

    def no_sidecars(root: int) -> None:
        for name in (*_SIDECARS, "recovery.json"):
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise StorageFault("storage_recovery_required")

    try:
        with lease.data_directory() as root:
            no_sidecars(root)  # Refuse residue before any database body-capable open.
        with lease.data_directory() as root, lease.open_file("database") as fd:
            no_sidecars(root)
            header = os.pread(fd, 100, 0)
            if len(header) != 100 or header[:16] != b"SQLite format 3\0":
                raise StorageFault("storage_unrecognized")
            if header[18:20] != b"\x01\x01":
                raise StorageFault("storage_schema_unsupported")
            lease.check()
            connection = sqlite3.connect(
                lease.layout.database.as_uri() + "?mode=ro&immutable=1",
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            try:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                _guard(connection)
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA temp_store=MEMORY")
                version = _schema(connection)
                lease.check()
            finally:
                connection.close()
            no_sidecars(root)
            lease.check()
            return version
    except sqlite3.Error as error:
        if time.monotonic() >= deadline:
            raise DiagnosticDeadlineExceeded from None
        raise _fault(error, "storage_unrecognized") from None
    except OSError:
        raise StorageFault("storage_failed") from None


@dataclass
class _Work:
    operation: Literal[
        "index",
        "lookup",
        "claim",
        "finish",
        "inspect",
        "root_claimed",
        "observe",
        "trace",
        "replay",
        "debug",
        "history",
        "stream",
        "retire",
        "review",
        "authority",
        "proposal_lookup",
        "execution_inspect",
        "launch_record",
        "launch_status",
        "execution_complete",
        "execution_result",
        "stop_request",
        "stop_inspect",
        "stop_finish",
        "cancel",
    ]
    payload: (
        Project
        | ReceiptClaim
        | ReceiptTransition
        | ReceiptScope
        | Path
        | ActivityRecord
        | ReplayScope
        | HistoryScope
        | ReviewOperation
        | AuthorityOperation
        | ProposalRequestScope
        | ExecutionInspectionScope
        | DockerLaunchEvent
        | DockerLaunchScope
        | DockerCompletion
        | ExecutionStopRequest
        | ExecutionStopObservation
        | ExecutionStopCapture
        | ExecutionCancelCall
        | None
    )
    deadline: float
    result: Future[object]


class SQLiteStorage:
    """Start/close belong to explicit lifetime ownership; registry calls block callers,
    never the control loop. No SQL/connection/cursor is exposed through this interface.
    """

    def __init__(
        self,
        owner: Ownership,
        *,
        instance_id: UUID | None = None,
        commit_hint: Callable[[], None] | None = None,
        retention_clock: Callable[[], int] | None = None,
        retention_monotonic: Callable[[], float] | None = None,
        scheduled_retention: bool = False,
        review_clock: Callable[[], int] | None = None,
        review_monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._review_clock = review_clock or (lambda: time.time_ns() // 1000000)
        self._review_monotonic = review_monotonic or time.monotonic
        self._review_wall_high: int | None = None
        self._review_mono_high: float | None = None
        self._review_clock_bad = False
        self._review_deadlines: dict[str, float] = {}
        self._authority_deadlines: dict[str, float] = {}
        self._execution_profiles: tuple[ExecutionProfile, ...] = ()
        self._execution_backend: DockerBackendConfiguration | None = None
        self._retention_monotonic = retention_monotonic or time.monotonic
        self._retention_progress = RetentionProgress(enabled=scheduled_retention)
        self._retention_position: RetentionPosition | None = None
        self._retention_clock = retention_clock or (lambda: time.time_ns() // 1000000)
        self._retention_days: int | None = None
        self._retention_mono_high: float | None = None
        self._retention_wall_high: int | None = None
        self._retention_clock_deferred = False
        self._commit_hint = commit_hint
        self._activity_dirty = False
        self._notification_failed = threading.Event()
        self.owner = owner
        self.instance_id = instance_id
        self._mutex = threading.Lock()
        self._state = StorageReadiness()
        self._initialized = threading.Event()
        self._closing = threading.Event()
        self._queue: queue.Queue[_Work] = queue.Queue(STORAGE_PENDING_LIMIT)
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._close_fault: StorageFault | None = None

    @property
    def notification_healthy(self) -> bool:
        return not self._notification_failed.is_set()

    def pause_retention(self) -> None:
        """Seal maintenance admission, not active SQL or ordinary action drainage."""
        with self._mutex:
            self._retention_progress = self._retention_progress.model_copy(
                update={
                    "paused": True,
                    "next_attempt_at": None,
                }
            )

    def retention_position(self) -> RetentionPosition | None:
        """Mutex-only committed epoch/generation/floor; no SQL or diagnostic writes."""
        with self._mutex:
            return self._retention_position

    def _publish_retention(self, meta: ActivityMeta) -> None:
        position = RetentionPosition(
            database_epoch=meta.database_epoch,
            retention_generation=meta.retention_generation,
            replay_floor=meta.replay_floor,
        )
        with self._mutex:
            self._retention_position = position

    def retention_snapshot(self) -> RetentionProgress:
        with self._mutex:
            return self._retention_progress

    def snapshot(self) -> StorageReadiness:
        """Return cached readiness without opening SQLite or queuing diagnostic work."""
        with self._mutex:
            return self._state

    def start(self) -> None:
        with self._mutex:
            if self._thread is not None or self._closing.is_set():
                raise StorageFault("storage_closing")
            self._state = StorageReadiness(state="initializing")
            self._thread = threading.Thread(
                target=self._run, name="jeff-storage-owner", daemon=False
            )
            self._thread.start()

    def wait_ready(self, timeout: float) -> StorageReadiness:
        if not self._initialized.wait(timeout):
            raise StorageFault("storage_outcome_unconfirmed")
        return self.snapshot()

    @property
    def closed(self) -> bool:
        return self._thread is None or not self._thread.is_alive()

    def request_close(self) -> None:
        """Seal new work and maintenance; active SQL still owns its connection and slot."""
        with self._mutex:
            self._accepting = False
            self._closing.set()
            self._retention_progress = self._retention_progress.model_copy(
                update={
                    "paused": True,
                    "next_attempt_at": None,
                }
            )
            if self._state.state != "failed":
                self._state = StorageReadiness(state="closing")

    def close(self, timeout: float = DEFAULT_RUNTIME_LIMITS.stop_seconds) -> None:
        """Join the SQL owner; a timed-out or faulty close stays explicitly unconfirmed."""
        self.request_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise StorageFault("storage_outcome_unconfirmed")
        if self._close_fault is not None:
            raise self._close_fault

    def index(self, project: Project, *, deadline: float | None = None) -> tuple[Project, bool]:
        return project, cast(bool, self._submit("index", project, deadline))

    def lookup(self, project: Project, *, deadline: float | None = None) -> Project | None:
        return project if self._submit("lookup", project, deadline) else None

    def root_claimed(self, root: Path, *, deadline: float | None = None) -> bool:
        if not root.is_absolute() or "\0" in str(root):
            raise StorageFault("registry_conflict")
        return cast(bool, self._submit("root_claimed", root, deadline))

    def claim(self, claim: ReceiptClaim, *, deadline: float) -> ReceiptClaimResult:
        return cast(ReceiptClaimResult, self._submit("claim", claim, deadline))

    def finish(self, transition: ReceiptTransition, *, deadline: float) -> Receipt:
        return cast(Receipt, self._submit("finish", transition, deadline))

    def inspect(self, scope: ReceiptScope, *, deadline: float) -> Receipt | None:
        return cast(Receipt | None, self._submit("inspect", scope, deadline))

    def observe(self, record: ActivityRecord, *, deadline: float) -> ActivityEvent:
        return cast(ActivityEvent, self._submit("observe", record, deadline))

    def trace(
        self, scope: ReceiptScope, *, deadline: float
    ) -> tuple[Receipt | None, ActivityTrace | None]:
        return cast(
            tuple[Receipt | None, ActivityTrace | None], self._submit("trace", scope, deadline)
        )

    def replay(self, scope: ReplayScope, *, deadline: float) -> ActivityPage:
        return cast(ActivityPage, self._submit("replay", scope, deadline))

    def debug(self, scope: HistoryScope, *, deadline: float) -> tuple[Receipt, ActivityTrace]:
        return cast(tuple[Receipt, ActivityTrace], self._submit("debug", scope, deadline))

    def history(self, scope: HistoryRead, *, deadline: float) -> HistoryPage:
        return cast(HistoryPage, self._submit("history", scope, deadline))

    def stream_page(self, scope: StreamRead, *, deadline: float) -> StreamPage:
        return cast(StreamPage, self._submit("stream", scope, deadline))

    def retire(self, *, deadline: float) -> RetirementBatch:
        """Installation-internal bounded maintenance; no cutoff/SQL or wire operation."""
        return cast(RetirementBatch, self._submit("retire", None, deadline))

    @property
    def execution_profiles(self) -> tuple[ExecutionProfile, ...]:
        return self._execution_profiles

    @property
    def execution_backend(self) -> DockerBackendConfiguration | None:
        """Startup snapshot only, not verification, activation or revocable authority."""
        with self._mutex:
            if self._state.state != "ready" or not self._accepting or self._closing.is_set():
                return None
            return self._execution_backend

    def review(self, operation: ReviewOperation, *, deadline: float) -> ExecutionData:
        try:
            return cast(ExecutionData, self._submit("review", operation, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
        """Internal verified-worker seam only; deliberately absent from RPC/CLI routing."""
        try:
            return cast(AuthorityResult, self._submit("authority", operation, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]:
        """Nonblocking actor submission; only a completed Future confirms durable intent."""
        call = ExecutionCancelCall.model_validate(call.model_dump(mode="json"))
        result: Future[object] = Future()
        return cast(
            Future[ExecutionStopRecord],
            self._enqueue(
                _Work("cancel", call, min(deadline, time.monotonic() + 30), result), control=True
            ),
        )

    def request_stop(
        self, request: ExecutionStopRequest, *, deadline: float
    ) -> ExecutionStopRecord:
        """Internal durable intent only; never sends a signal or grants a new claim."""
        request = ExecutionStopRequest.model_validate(request.model_dump(mode="json"))
        try:
            return cast(ExecutionStopRecord, self._submit("stop_request", request, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def finish_stop(
        self, observation: ExecutionStopObservation, *, deadline: float
    ) -> ExecutionStopRecord:
        observation = ExecutionStopObservation.model_validate(observation.model_dump(mode="json"))
        try:
            return cast(ExecutionStopRecord, self._submit("stop_finish", observation, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def finish_stop_with_capture(
        self,
        observation: ExecutionStopObservation,
        capture: DockerOutputSnapshot,
        *,
        deadline: float,
    ) -> ExecutionStopRecord:
        request = ExecutionStopCapture.model_validate(
            {
                "observation": observation.model_dump(mode="json"),
                "output": capture.model_dump(mode="json"),
            }
        )
        try:
            return cast(ExecutionStopRecord, self._submit("stop_finish", request, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def inspect_stop(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionStopRecord | None:
        try:
            return cast(ExecutionStopRecord | None, self._submit("stop_inspect", scope, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def inspect_execution(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionInspection:
        try:
            return cast(ExecutionInspection, self._submit("execution_inspect", scope, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def lookup_proposal(
        self, scope: ProposalRequestScope, *, deadline: float
    ) -> AuthorityRecord | None:
        """Scoped DB8 operation facts only, never permission or a new preparation."""
        try:
            return cast(AuthorityRecord | None, self._submit("proposal_lookup", scope, deadline))
        except StorageFault as error:
            raise ReviewFault(error.code) from None

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
        """Private journal seam. Unconfirmed SQL never authorizes a repeated mutation."""
        try:
            self._submit("launch_record", event, deadline)
        except StorageFault:
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def launch_status(self, scope: DockerLaunchScope, *, deadline: float) -> DockerLaunchSnapshot:
        try:
            return cast(DockerLaunchSnapshot, self._submit("launch_status", scope, deadline))
        except StorageFault:
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def complete_execution(
        self, completion: DockerCompletion, *, deadline: float
    ) -> DockerExecutionResult:
        try:
            completion = DockerCompletion.model_validate(completion.model_dump(mode="json"))
        except ValueError:
            raise DockerBackendFault("docker_output_failed") from None
        try:
            return cast(
                DockerExecutionResult, self._submit("execution_complete", completion, deadline)
            )
        except StorageFault:
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def execution_result(
        self, scope: DockerLaunchScope, *, deadline: float
    ) -> DockerExecutionResult:
        try:
            return cast(DockerExecutionResult, self._submit("execution_result", scope, deadline))
        except StorageFault:
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def _submit(
        self,
        operation: Literal[
            "index",
            "lookup",
            "claim",
            "finish",
            "inspect",
            "root_claimed",
            "observe",
            "trace",
            "replay",
            "debug",
            "history",
            "stream",
            "retire",
            "review",
            "authority",
            "proposal_lookup",
            "execution_inspect",
            "launch_record",
            "launch_status",
            "execution_complete",
            "execution_result",
            "stop_request",
            "stop_inspect",
            "stop_finish",
            "cancel",
        ],
        payload: Project
        | ReceiptClaim
        | ReceiptTransition
        | ReceiptScope
        | Path
        | ActivityRecord
        | ReplayScope
        | HistoryScope
        | ReviewOperation
        | AuthorityOperation
        | ProposalRequestScope
        | ExecutionInspectionScope
        | DockerLaunchEvent
        | DockerLaunchScope
        | DockerCompletion
        | ExecutionStopRequest
        | ExecutionStopObservation
        | ExecutionStopCapture
        | ExecutionCancelCall
        | None,
        deadline: float | None,
    ) -> object:
        """Bound admission and caller waiting without abandoning already running SQL."""
        if isinstance(payload, Project) and (
            not isinstance(payload.project_id, UUID)
            or not payload.root.is_absolute()
            or "\0" in str(payload.root)
        ):
            raise StorageFault("registry_conflict")
        deadline = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds,
        )
        if deadline <= time.monotonic():
            raise StorageFault("storage_busy")
        result: Future[object] = Future()
        self._enqueue(
            _Work(operation, payload, deadline, result), control=operation.startswith("stop_")
        )
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            # Only queued work can be cancelled safely; running SQL keeps its slot.
            raise StorageFault(
                "storage_busy" if result.cancel() else "storage_outcome_unconfirmed"
            ) from None

    def _enqueue(self, work: _Work, *, control: bool = False) -> Future[object]:
        if not math.isfinite(work.deadline) or work.deadline <= time.monotonic():
            raise StorageFault("storage_busy")
        with self._mutex:
            if not self._accepting:
                raise StorageFault("storage_closing")
            # One reserved control position WITHIN the existing finite pending bound.
            if not control and self._queue.qsize() >= STORAGE_PENDING_LIMIT - 1:
                raise StorageFault("storage_busy")
            try:
                self._queue.put_nowait(work)
            except queue.Full:
                raise StorageFault("storage_busy") from None
        return work.result

    def _begin_write(
        self,
        connection: sqlite3.Connection,
        root: int,
        fd: int,
        source_schema: int = DATABASE_SCHEMA,
    ) -> None:
        """Arm the actual DELETE journal before business DML, on the same SQL owner."""
        self._check(root, fd)
        if source_schema not in (4, 5, 6, 7, 8, 9, 10) or _schema(connection) != source_schema:
            raise StorageFault("storage_schema_unsupported")
        try:
            self.owner.store.read_credential()  # Existing authority only, never rekey residue.
            self.owner.retire_storage_recovery(fd)
            connection.execute("BEGIN IMMEDIATE")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            connection.execute(f"PRAGMA user_version={source_schema}")  # Real journal first.
            journal = os.open("state.sqlite3-journal", os.O_RDONLY | _FLAGS, dir_fd=root)
            try:
                self.owner.arm_storage_recovery(
                    fd,
                    journal,
                    page_size=page_size,
                    original_pages=pages,
                    schema_version=source_schema,
                )
            finally:
                os.close(journal)
        except (ControlFault, OSError):
            raise StorageFault("storage_recovery_required") from None

    def _commit_write(self, connection: sqlite3.Connection, fd: int) -> None:
        """Commit before proof retirement; a later failure is not evidence of rollback."""
        connection.execute("COMMIT")
        try:
            self.owner.retire_storage_recovery(fd)
        except (ControlFault, OSError):
            raise StorageFault("storage_outcome_unconfirmed") from None

    def _rollback_write(self, connection: sqlite3.Connection, fd: int) -> None:
        """Let SQLite roll back before retiring only the matching recovery permission."""
        connection.execute("ROLLBACK")
        try:
            self.owner.retire_storage_recovery(fd)
        except (ControlFault, OSError):
            raise StorageFault("storage_outcome_unconfirmed") from None

    def _authorize_recovery(self, root: int, database_fd: int) -> int:
        """Read-only eligibility before writable SQLite open; return a pinned journal FD."""
        journal = os.open("state.sqlite3-journal", os.O_RDONLY | _FLAGS, dir_fd=root)
        try:
            before = os.fstat(journal)
            permit = self.owner.verify_storage_recovery(database_fd, journal)
            database = os.fstat(database_fd)
            if not (
                0 < database.st_size <= RECOVERY_DATABASE_BYTES
                and 28 <= before.st_size <= RECOVERY_JOURNAL_BYTES
                and permit.original_pages * permit.page_size <= RECOVERY_DATABASE_BYTES
                and database.st_size % permit.page_size == 0
            ):
                raise StorageFault("storage_recovery_required")
            header = os.pread(journal, 28, 0)
            magic = bytes.fromhex("d9d505f920a163d7")
            if len(header) != 28 or header[:8] != magic:
                raise StorageFault("storage_recovery_required")
            records = int.from_bytes(header[8:12], "big")
            original_pages = int.from_bytes(header[16:20], "big")
            sector = int.from_bytes(header[20:24], "big")
            page_size = int.from_bytes(header[24:28], "big")
            if not (
                records > 0
                and original_pages == permit.original_pages
                and page_size == permit.page_size
                and 512 <= sector <= 65536
                and sector & (sector - 1) == 0
                and before.st_size >= sector + page_size + 8
                and (
                    records == 0xFFFFFFFF or records <= (before.st_size - sector) // (page_size + 8)
                )
            ):
                raise StorageFault("storage_recovery_required")
            # ATTACH is forbidden; never adopt a super-journal relationship/path.
            if os.pread(journal, 8, before.st_size - 8) == magic:
                raise StorageFault("storage_recovery_required")
            after = os.fstat(journal)
            if (_signature(before), before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                _signature(after),
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise StorageFault("storage_recovery_required")
            self.owner.verify_storage_recovery(database_fd, journal)
            return journal
        except BaseException:
            os.close(journal)
            raise

    def _sidecars(self, root: int, *, preexisting: bool) -> None:
        for name in _SIDECARS:
            try:
                info = os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if preexisting:
                raise StorageFault("storage_recovery_required")
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == self.owner.store.layout.uid
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
            ):
                raise StorageFault("storage_unsafe")

    def _check(self, root: int, fd: int) -> None:
        self.owner.check()
        current = os.stat("state.sqlite3", dir_fd=root, follow_symlinks=False)
        before = os.fstat(fd)
        if not (
            stat.S_ISREG(current.st_mode)
            and current.st_uid == self.owner.store.layout.uid
            and stat.S_IMODE(current.st_mode) == 0o600
            and current.st_nlink == 1
            and _signature(current) == _signature(before)
        ):
            raise StorageFault("storage_unsafe")
        self._sidecars(root, preexisting=False)

    def _open(self, root: int) -> tuple[sqlite3.Connection, int]:
        self.owner.check()
        for name in _SIDECARS[1:]:
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise StorageFault("storage_recovery_required")
        try:
            os.stat("state.sqlite3-journal", dir_fd=root, follow_symlinks=False)
            recovering = True
        except FileNotFoundError:
            recovering = False
        try:
            os.stat("recovery.json", dir_fd=root, follow_symlinks=False)
            has_permit = True
        except FileNotFoundError:
            has_permit = False
        fd = -1
        journal_fd = -1
        connection: sqlite3.Connection | None = None
        recovery_schema: int | None = None
        try:
            if recovering or has_permit:
                try:
                    fd = os.open("state.sqlite3", os.O_RDONLY | _FLAGS, dir_fd=root)
                except FileNotFoundError:
                    raise StorageFault("storage_recovery_required") from None
                created = False
            else:
                try:
                    fd = os.open(
                        "state.sqlite3",
                        os.O_RDWR | _FLAGS | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=root,
                    )
                    created = True
                except FileExistsError:
                    fd = os.open("state.sqlite3", os.O_RDONLY | _FLAGS, dir_fd=root)
                    created = False
            self._check(root, fd)
            if recovering:
                try:
                    journal_fd = self._authorize_recovery(root, fd)
                except (ControlFault, OSError):
                    raise StorageFault("storage_recovery_required") from None
            else:
                try:
                    self.owner.retire_storage_recovery(fd)
                except (ControlFault, OSError):
                    raise StorageFault("storage_recovery_required") from None
            base = self.owner.store.layout.database.as_uri()
            if not created and not recovering:
                header = os.pread(fd, 100, 0)
                if len(header) != 100 or header[:16] != b"SQLite format 3\0":
                    raise StorageFault("storage_unrecognized")
                # File-header read/write versions 1/1 are rollback format. Immutable
                # SQLite deliberately bypasses WAL handling and can report DELETE
                # for a clean WAL file, so its PRAGMA is not this format proof.
                if header[18:20] != b"\x01\x01":
                    raise StorageFault("storage_schema_unsupported")
                try:
                    probe = sqlite3.connect(
                        base + "?mode=ro&immutable=1", uri=True, isolation_level=None
                    )
                    try:
                        _guard(probe)
                        _schema(probe)
                    finally:
                        probe.close()
                except sqlite3.Error as error:
                    raise _fault(error, "storage_unrecognized") from None
                self._check(root, fd)
                self._sidecars(root, preexisting=True)
            if recovering:
                recovery_schema = self.owner.verify_storage_recovery(
                    fd, journal_fd
                ).schema_version  # Last authenticated check before writable open.
            connection = sqlite3.connect(
                base + "?mode=rw&cache=private",
                uri=True,
                timeout=DEFAULT_RUNTIME_LIMITS.sqlite_busy_seconds,
                isolation_level=None,
            )
            _guard(connection)
            self._check(root, fd)
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA synchronous").fetchone() != (2,):
                raise StorageFault("storage_failed")
            if created:
                if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise StorageFault("storage_failed")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"PRAGMA application_id={DATABASE_APPLICATION_ID}")
                connection.execute(_LEDGER)
                connection.execute("PRAGMA user_version=0")
                connection.execute("COMMIT")
            version = _schema(connection)
            if recovering:
                if version != recovery_schema:
                    raise StorageFault("storage_schema_unsupported")
                if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                    raise StorageFault("storage_unrecognized")
                self._sidecars(root, preexisting=True)
                self.owner.retire_storage_recovery(fd)
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_PROJECTS)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name) VALUES (?,?)", (1, "registry_v1")
                )
                connection.execute("PRAGMA user_version=1")
                connection.execute("COMMIT")
            if version < 2:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_RECEIPTS)
                connection.execute(_RECEIPT_INDEX)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name) VALUES (?,?)",
                    (2, "request_receipts_v2"),
                )
                connection.execute("PRAGMA user_version=2")
                connection.execute("COMMIT")
            if version < 3:
                connection.execute("BEGIN IMMEDIATE")
                for sql in (
                    _ACTIVITY_META,
                    _ACTIVITY_REQUESTS,
                    _ACTIVITY_EVENTS,
                    *_ACTIVITY_INDEXES.values(),
                ):
                    connection.execute(sql)
                connection.execute(
                    "INSERT INTO activity_meta VALUES (1,?,?,0,0)",
                    (str(uuid4()), time.time_ns() // 1000000),
                )
                connection.execute("INSERT INTO schema_migrations VALUES (3,'activity_v3')")
                connection.execute("PRAGMA user_version=3")
                connection.execute("COMMIT")
            if version < 4:
                connection.execute("BEGIN IMMEDIATE")
                meta = self._activity_meta(connection, legacy=True)
                if meta.retention_generation == INT64_MAX:
                    raise StorageFault("storage_schema_unsupported")
                connection.execute(_TOMBSTONES)
                connection.execute(_EXPIRABLE_INDEX)
                connection.execute("ALTER TABLE activity_meta ADD COLUMN " + _FLOOR_COLUMN)
                connection.execute("UPDATE activity_meta SET generation=generation+1")
                connection.execute("INSERT INTO schema_migrations VALUES (4,'retention_v4')")
                connection.execute("PRAGMA user_version=4")
                self._check(root, fd)
                try:
                    connection.execute("COMMIT")
                    self._check(root, fd)
                except Exception:
                    if not connection.in_transaction:
                        raise StorageFault("storage_outcome_unconfirmed") from None
                    raise
            if version < 5:
                self._begin_write(connection, root, fd, source_schema=4)
                connection.execute(_REVIEWS)
                connection.execute(_REVIEW_OPS)
                connection.execute("INSERT INTO schema_migrations VALUES (5,'execution_review_v5')")
                connection.execute("PRAGMA user_version=5")
                self._commit_write(connection, fd)
            if version < 6:
                self._begin_write(connection, root, fd, source_schema=5)
                connection.execute(_AUTHORITIES)
                connection.execute(_AUTHORITY_OPS)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (6,'execution_authority_v6')"
                )
                connection.execute("PRAGMA user_version=6")
                self._commit_write(connection, fd)
            if version < 7:
                self._begin_write(connection, root, fd, source_schema=6)
                connection.execute(_LAUNCHES)
                connection.execute(_LAUNCH_EVENTS)
                connection.execute("INSERT INTO schema_migrations VALUES (7,'execution_launch_v7')")
                connection.execute("PRAGMA user_version=7")
                self._commit_write(connection, fd)
            if version < 8:
                self._begin_write(connection, root, fd, source_schema=7)
                connection.execute(_RESULTS)
                connection.execute(_RESULT_PAYLOADS)
                connection.execute(_RESULT_QUARANTINE)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (8,'execution_results_v8')"
                )
                connection.execute("PRAGMA user_version=8")
                self._commit_write(connection, fd)
            if version < 9:
                self._begin_write(connection, root, fd, source_schema=8)
                connection.execute(_LIFECYCLES)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (9,'execution_lifecycle_v9')"
                )
                connection.execute("PRAGMA user_version=9")
                self._commit_write(connection, fd)
            if version < 10:
                self._begin_write(connection, root, fd, source_schema=9)
                connection.execute(_STOP_PAYLOADS)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (10,'execution_stop_payloads_v10')"
                )
                connection.execute("PRAGMA user_version=10")
                self._commit_write(connection, fd)
            _schema(connection)
            self._activity_meta(connection)
            if connection.execute(
                "SELECT 1 FROM execution_reviews WHERE state IN ('pending','approved') LIMIT 1"
            ).fetchone():
                self._begin_write(connection, root, fd)
                connection.execute(
                    "UPDATE execution_reviews SET state='invalidated' "
                    "WHERE state IN ('pending','approved')"
                )
                self._commit_write(connection, fd)
            if connection.execute(
                "SELECT 1 FROM execution_authorities WHERE state IN ('pending','approved') "
                "OR claim_state='claimed' LIMIT 1"
            ).fetchone():
                self._begin_write(connection, root, fd)
                connection.execute(
                    "UPDATE execution_authorities SET state='invalidated' "
                    "WHERE state IN ('pending','approved')"
                )
                connection.execute(
                    "UPDATE execution_authorities SET claim_state='unknown' "
                    "WHERE claim_state='claimed'"
                )
                self._commit_write(connection, fd)
            if self.instance_id is not None:
                self._reconcile(connection, self.instance_id, root, fd)
            self._check(root, fd)
            return connection, fd
        except BaseException:
            try:
                if connection is not None and connection.in_transaction:
                    self._rollback_write(connection, fd)
            finally:
                self._release(connection, fd)
            raise
        finally:
            if journal_fd >= 0:
                os.close(journal_fd)

    def _execute(self, connection: sqlite3.Connection, work: _Work, root: int, fd: int) -> object:
        self._check(root, fd)
        if work.operation == "cancel":
            assert isinstance(work.payload, ExecutionCancelCall)
            call = work.payload
            row = connection.execute(
                "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE approval_id=?",
                (str(call.approval_id),),
            ).fetchone()
            if row is None:
                raise ReviewFault("execution_not_found")
            target = self._authority_record(row)
            return self._request_stop(
                connection,
                ExecutionStopRequest(
                    project_id=target.project_id,
                    canonical_root=target.canonical_root,
                    owner_instance_id=cast(UUID, self.instance_id),
                    approval_id=call.approval_id,
                    request_id=call.request_id,
                    manifest_digest=call.reviewed_manifest_digest,
                ),
                root,
                fd,
            )
        if work.operation == "stop_request":
            assert isinstance(work.payload, ExecutionStopRequest)
            return self._request_stop(connection, work.payload, root, fd)
        if work.operation == "stop_finish":
            if isinstance(work.payload, ExecutionStopCapture):
                return self._finish_stop(
                    connection, work.payload.observation, root, fd, capture=work.payload.output
                )
            assert isinstance(work.payload, ExecutionStopObservation)
            return self._finish_stop(connection, work.payload, root, fd)
        if work.operation == "stop_inspect":
            assert isinstance(work.payload, ExecutionInspectionScope)
            record = self._stop_target(connection, work.payload)
            return self._read_stop(connection, record, work.payload)
        if work.operation == "execution_complete":
            assert isinstance(work.payload, DockerCompletion)
            return self._complete_execution(connection, work.payload, root, fd)
        if work.operation == "execution_result":
            assert isinstance(work.payload, DockerLaunchScope)
            return self._execution_result(connection, work.payload)
        if work.operation == "launch_record":
            assert isinstance(work.payload, DockerLaunchEvent)
            self._launch_record(connection, work.payload, root, fd)
            return None
        if work.operation == "launch_status":
            assert isinstance(work.payload, DockerLaunchScope)
            return self._launch_status(connection, work.payload)
        if work.operation == "execution_inspect":
            assert isinstance(work.payload, ExecutionInspectionScope)
            return self._inspect_execution(connection, work.payload, root, fd)
        if work.operation == "proposal_lookup":
            assert isinstance(work.payload, ProposalRequestScope)
            return self._lookup_proposal(connection, work.payload)
        if work.operation == "authority":
            assert isinstance(work.payload, AuthorityOperation)
            return self._authority_work(connection, work.payload, root, fd)
        if work.operation == "review":
            assert isinstance(work.payload, ReviewOperation)
            return self._review_work(connection, work.payload, root, fd)
        if work.operation == "retire":
            return self._retire(connection, root, fd)
        if work.operation == "stream":
            assert isinstance(work.payload, StreamRead)
            return self._stream_page(connection, work.payload, root, fd)
        if work.operation == "history":
            assert isinstance(work.payload, HistoryRead)
            return self._history(connection, work.payload, root, fd)
        if work.operation in ("observe", "trace", "replay", "debug"):
            return self._activity_work(connection, work, root, fd)
        if work.operation == "root_claimed":
            assert isinstance(work.payload, Path)
            return (
                connection.execute(
                    "SELECT 1 FROM projects WHERE canonical_root=?", (str(work.payload),)
                ).fetchone()
                is not None
            )
        if work.operation not in ("index", "lookup"):
            return self._receipt_work(connection, work, root, fd)
        assert isinstance(work.payload, Project)
        project_id, path = str(work.payload.project_id), str(work.payload.root)
        writing = work.operation == "index"
        if writing:
            self._begin_write(connection, root, fd)
        rows = connection.execute(
            "SELECT project_id,canonical_root FROM projects WHERE project_id=? OR canonical_root=?",
            (project_id, path),
        ).fetchall()
        if rows and rows != [(project_id, path)]:
            raise StorageFault("registry_conflict")
        created = not rows
        if writing and created:
            connection.execute(
                "INSERT INTO projects(project_id,canonical_root) VALUES (?,?)", (project_id, path)
            )
        self._check(root, fd)
        try:
            if writing:
                if created:
                    self._commit_write(connection, fd)
                else:
                    self._rollback_write(connection, fd)
            self._check(root, fd)
        except (sqlite3.Error, StorageFault, ControlFault, OSError):
            if writing and not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        return created if writing else bool(rows)

    def _execution_result(
        self, connection: sqlite3.Connection, scope: DockerLaunchScope
    ) -> DockerExecutionResult:
        launch = self._launch_status(connection, scope)
        row = connection.execute(
            "SELECT sequence,disposition,recorded_at_ms,metadata_json,completion_digest,"
            "stdout,stderr,retirement_reason FROM execution_results WHERE attempt_id=?",
            (str(launch.intent.attempt_id),),
        ).fetchone()
        if row is None:
            raise DockerBackendFault("docker_mutation_unconfirmed")
        try:
            metadata = json.loads(row[3])
            expected = "finished" if row[1] == "completed" else "unknown"
            if launch.claim_state != expected or (
                row[1] == "completed"
                and (launch.uncertain or "cleaned" not in launch.phases or launch.exit_code is None)
            ):
                raise ValueError("Inconsistent completion state.")
            output = (
                None
                if row[5] is None
                else DockerOutputSnapshot.model_validate(
                    {**metadata["output_facts"], "stdout": row[5], "stderr": row[6]}
                )
            )
            result = DockerExecutionResult.model_validate(
                {
                    **metadata,
                    "owned": DockerOwnedContainer.model_validate(
                        {
                            "intent": launch.intent,
                            "container_id": launch.container_id,
                            "created_at": launch.created_at,
                        }
                    ),
                    "sequence": row[0],
                    "disposition": row[1],
                    "recorded_at_ms": row[2],
                    "completion_digest": row[4],
                    "payload_state": "retired" if output is None else "retained",
                    "retirement_reason": row[7],
                    "output": output,
                }
            )
            now = self._sample_retention_wall()
            if result.output is not None and (
                now is None
                or result.recorded_at_ms < max(0, now - (self._retention_days or 30) * 86400000)
            ):
                result = result.model_copy(
                    update={
                        "output": None,
                        "payload_state": "clock_unconfirmed" if now is None else "expired",
                    }
                )
            return result
        except (ValueError, TypeError, KeyError):
            raise StorageFault("storage_schema_unsupported") from None

    @staticmethod
    def _retire_execution_payloads(
        connection: sqlite3.Connection, cutoff: int, *, age_limit: int = 4
    ) -> int:
        # Bounded page churn under the existing16MiB recovery-journal ceiling.
        # No claim of physical-crash proof follows from this logical byte budget.
        rows = SQLiteStorage._retained_payloads(connection)
        retired = 0
        expired = sorted(
            (row for row in rows if row[4] and row[3] < cutoff),
            key=lambda row: (row[2] != "quarantined", row[3], row[0], row[1]),
        )[:age_limit]
        removed = {(row[0], row[1]) for row in expired}
        known = sorted(
            (r for r in rows if r[2] == "known" and (r[0], r[1]) not in removed),
            key=lambda r: (r[3], r[0], r[1]),
        )
        for row, reason in [(r, "age") for r in expired] + [(r, "capacity") for r in known[:-16]]:
            table, key = (
                ("execution_results", "sequence")
                if row[0] == "legacy"
                else ("execution_stop_payloads", "proposal_id")
            )
            connection.execute(
                f"UPDATE {table} SET stdout=NULL,stderr=NULL,retirement_reason=? WHERE {key}=?",
                (reason, row[1]),
            )
            retired += 1
        return retired

    @staticmethod
    def _retained_payloads(connection: sqlite3.Connection) -> list[tuple[str, str, str, int, int]]:
        rows = connection.execute(
            "SELECT 'legacy',CAST(sequence AS TEXT),CASE disposition WHEN 'completed' "
            "THEN 'known' ELSE 'quarantined' END,recorded_at_ms,1 FROM execution_results "
            "WHERE stdout IS NOT NULL UNION ALL SELECT 'stop',proposal_id,disposition,"
            "recorded_at_ms,clock_confirmed FROM execution_stop_payloads "
            "WHERE stdout IS NOT NULL LIMIT 19"
        ).fetchall()
        if len(rows) > 18 or sum(r[2] == "quarantined" for r in rows) > 1:
            raise StorageFault("storage_schema_unsupported")
        return cast(list[tuple[str, str, str, int, int]], rows)

    def _insert_stop_payload(
        self,
        connection: sqlite3.Connection,
        authority: AuthorityRecord,
        record: ExecutionStopRecord,
        capture: DockerOutputSnapshot,
    ) -> None:
        terminal = record.terminal
        assert terminal is not None
        if capture.retention_limit != authority.manifest.profile.output_bytes:
            raise ReviewFault("execution_target_mismatch")
        now = self._sample_retention_wall()
        cutoff = max(0, (now or 0) - (self._retention_days or 30) * 86400000)
        self._retire_execution_payloads(connection, cutoff, age_limit=1)
        occupied = any(r[2] == "quarantined" for r in self._retained_payloads(connection))
        retain = terminal.outcome != "unknown" or not occupied
        connection.execute(
            "INSERT INTO execution_stop_payloads VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(authority.proposal_id),
                "quarantined" if terminal.outcome == "unknown" else "known",
                now if now is not None else record.requested_at_ms,
                int(now is not None),
                terminal.output_facts.model_dump_json()
                if terminal.output_facts is not None
                else "{}",
                terminal.output_digest,
                capture.stdout if retain else None,
                capture.stderr if retain else None,
                None if retain else "capacity",
            ),
        )
        self._retire_execution_payloads(connection, cutoff, age_limit=0)

    def _stop_payload(
        self,
        connection: sqlite3.Connection,
        authority: AuthorityRecord,
        record: ExecutionStopRecord,
    ) -> tuple[ExecutionStopView, DockerOutputSnapshot | None]:
        terminal = record.terminal
        fields: dict[str, object] = dict(
            request_id=record.request_id,
            reason=record.reason,
            requested_at_ms=record.requested_at_ms,
            clock_confirmed=record.clock_confirmed,
            payload_state="missing",
        )
        if terminal is not None:
            fields.update(
                outcome=terminal.outcome,
                evidence=terminal.evidence,
                cleanup_verified=terminal.cleanup_verified,
                exit_code=terminal.exit_code,
                output_facts=terminal.output_facts,
            )
        row = connection.execute(
            "SELECT disposition,recorded_at_ms,clock_confirmed,output_facts_json,output_digest,"
            "stdout,stderr,retirement_reason FROM execution_stop_payloads WHERE proposal_id=?",
            (str(authority.proposal_id),),
        ).fetchone()
        if row is None:
            return ExecutionStopView.model_validate(fields), None
        try:
            if (
                terminal is None
                or terminal.output_facts is None
                or (
                    DockerOutputFacts.model_validate_json(row[3]) != terminal.output_facts
                    or row[4] != terminal.output_digest
                    or row[0] != ("quarantined" if terminal.outcome == "unknown" else "known")
                    or terminal.output_facts.retention_limit
                    != authority.manifest.profile.output_bytes
                )
            ):
                raise ValueError("Stop payload binding differs.")
            snapshot = None
            if row[5] is not None:
                snapshot = DockerOutputSnapshot.model_validate(
                    {**terminal.output_facts.model_dump(), "stdout": row[5], "stderr": row[6]}
                )
                if sha256(snapshot.model_dump_json().encode("utf-8")).hexdigest() != row[4]:
                    raise ValueError("Stop payload digest differs.")
            if snapshot is None:
                state, retirement = "retired", row[7]
            else:
                now = self._sample_retention_wall()
                state, retirement = "retained", None
                if now is None or not row[2]:
                    state = "clock_unconfirmed"
                elif row[1] < max(0, now - (self._retention_days or 30) * 86400000):
                    state = "expired"
                if state != "retained":
                    snapshot = None
            fields.update(payload_state=state, retirement_reason=retirement, recorded_at_ms=row[1])
            return ExecutionStopView.model_validate(fields), snapshot
        except (ValueError, TypeError):
            raise StorageFault("storage_schema_unsupported") from None

    def _complete_execution(
        self, connection: sqlite3.Connection, completion: DockerCompletion, root: int, fd: int
    ) -> DockerExecutionResult:
        owned = completion.owned
        intent = owned.intent
        supplied = intent.record
        if self.instance_id is None or supplied.owner_instance_id != self.instance_id:
            raise DockerBackendFault("docker_authority_required")
        scope = DockerLaunchScope.model_validate(
            {
                "project_id": supplied.project_id,
                "canonical_root": supplied.canonical_root,
                "claim_id": supplied.claim_id,
            }
        )
        observed = self._launch_status(connection, scope)
        authority_row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE claim_id=?",
            (str(supplied.claim_id),),
        ).fetchone()
        if authority_row is None:
            raise DockerBackendFault("docker_authority_required")
        current = self._authority_record(authority_row)
        if (
            current.model_dump(exclude={"claim_state"})
            != supplied.model_dump(exclude={"claim_state"})
            or observed.intent != intent
            or observed.container_id != owned.container_id
            or observed.created_at != owned.created_at
            or observed.claim_state not in {"claimed", "unknown"}
            or completion.output.retention_limit != supplied.manifest.profile.output_bytes
            or (
                completion.outcome.exit_code is not None
                and observed.exit_code is not None
                and completion.outcome.exit_code != observed.exit_code
            )
            or connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
                (str(supplied.project_id), str(supplied.canonical_root)),
            ).fetchone()
            is None
            or connection.execute(
                "SELECT 1 FROM execution_results WHERE attempt_id=?", (str(intent.attempt_id),)
            ).fetchone()
            is not None
        ):
            raise DockerBackendFault("docker_authority_required")
        if completion.confirmed and (
            observed.uncertain
            or observed.phases != ("create_intent", "created", "start_intent", "exited", "cleaned")
            or observed.exit_code != completion.outcome.exit_code
        ):
            raise DockerBackendFault("docker_mutation_unconfirmed")
        now = self._sample_retention_wall()
        if now is None:
            raise DockerBackendFault("docker_mutation_unconfirmed")
        cutoff = max(0, now - (self._retention_days or 30) * 86400000)
        if not completion.confirmed and any(
            r[2] == "quarantined" and (not r[4] or r[3] >= cutoff)
            for r in self._retained_payloads(connection)
        ):
            raise DockerBackendFault("docker_busy")
        metadata = json.dumps(
            {
                "outcome": completion.outcome.model_dump(mode="json"),
                "output_facts": completion.output_facts.model_dump(mode="json"),
                "output_digest": completion.output_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(metadata.encode("utf-8")) > 8192:
            raise DockerBackendFault("docker_output_failed")
        self._begin_write(connection, root, fd)
        self._retire_execution_payloads(connection, cutoff, age_limit=1)
        if not completion.confirmed and "unknown" not in observed.phases:
            event = DockerLaunchEvent(
                intent=intent,
                phase="unknown",
                container_id=owned.container_id,
                created_at=owned.created_at,
            )
            digest = fingerprint(
                {"intent": intent.fingerprint, **event.model_dump(mode="json", exclude={"intent"})}
            )
            connection.execute(
                "INSERT INTO execution_launch_events VALUES (?,?,?,?)",
                (str(intent.attempt_id), "unknown", len(observed.phases) + 1, digest),
            )
        connection.execute(
            "INSERT INTO execution_results (attempt_id,disposition,recorded_at_ms,metadata_json,"
            "completion_digest,stdout,stderr) VALUES (?,?,?,?,?,?,?)",
            (
                str(intent.attempt_id),
                "completed" if completion.confirmed else "quarantined",
                now,
                metadata,
                completion.fingerprint,
                completion.output.stdout,
                completion.output.stderr,
            ),
        )
        connection.execute(
            "UPDATE execution_authorities SET claim_state=? WHERE claim_id=?",
            ("finished" if completion.confirmed else "unknown", str(supplied.claim_id)),
        )
        self._retire_execution_payloads(connection, cutoff, age_limit=0)
        stop_scope = ExecutionInspectionScope(
            project_id=supplied.project_id,
            canonical_root=supplied.canonical_root,
            owner_instance_id=supplied.owner_instance_id,
            approval_id=supplied.approval_id,
        )
        stop = self._read_stop(connection, supplied, stop_scope)
        if stop is not None and stop.terminal is None:
            # Preserve the original immutable result; cancellation cannot relabel
            # an ordinary completion that won before termination was established.
            terminal = self._result_stop_terminal(self._execution_result(connection, scope))
            updated = ExecutionStopRecord.model_validate(
                {
                    **stop.model_dump(),
                    "terminal": terminal.model_dump(),
                }
            )
            self._write_stop(connection, supplied, updated)
        self._commit_write(connection, fd)
        return self._execution_result(connection, scope)

    def _launch_status(
        self, connection: sqlite3.Connection, scope: DockerLaunchScope
    ) -> DockerLaunchSnapshot:
        if self.instance_id is None:
            raise DockerBackendFault("docker_authority_required")
        row = connection.execute(
            "SELECT l.intent_json,l.intent_digest,l.container_id,l.created_at,"
            "l.observed_container_id,l.exit_code,a.claim_state FROM execution_launches l "
            "JOIN execution_authorities a ON a.claim_id=l.claim_id "
            "WHERE l.claim_id=? AND a.project_id=? AND a.canonical_root=?",
            (str(scope.claim_id), str(scope.project_id), str(scope.canonical_root)),
        ).fetchone()
        if row is None:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        try:
            intent = DockerLaunchIntent.model_validate_json(row[0])
            if intent.fingerprint != row[1] or intent.record.claim_id != scope.claim_id:
                raise ValueError("Invalid persisted launch binding.")
            events = connection.execute(
                "SELECT phase,sequence FROM execution_launch_events WHERE attempt_id=? "
                "ORDER BY sequence LIMIT 7",
                (str(intent.attempt_id),),
            ).fetchall()
            if not events or any(number != index for index, (_, number) in enumerate(events, 1)):
                raise ValueError("Invalid persisted lifecycle order.")
            phases = tuple(phase for phase, _ in events)
            return DockerLaunchSnapshot.model_validate(
                {
                    "intent": intent,
                    "phases": phases,
                    "container_id": row[2],
                    "created_at": row[3],
                    "observed_container_id": row[4],
                    "exit_code": row[5],
                    "claim_state": row[6],
                    "uncertain": row[6] == "unknown" or "unknown" in phases,
                }
            )
        except ValueError:
            raise StorageFault("storage_schema_unsupported") from None

    def _launch_record(
        self, connection: sqlite3.Connection, event: DockerLaunchEvent, root: int, fd: int
    ) -> None:
        intent = event.intent
        supplied = intent.record
        if self.instance_id is None or supplied.owner_instance_id != self.instance_id:
            raise DockerBackendFault("docker_authority_required")
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE claim_id=?",
            (str(supplied.claim_id),),
        ).fetchone()
        if row is None:
            raise DockerBackendFault("docker_authority_required")
        current = self._authority_record(row)
        if (
            current.state != AuthorityState.CONSUMED
            or current.claim_state not in {"claimed", "unknown"}
            or current.model_dump(exclude={"claim_state"})
            != supplied.model_dump(exclude={"claim_state"})
            or connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
                (str(current.project_id), str(current.canonical_root)),
            ).fetchone()
            is None
        ):
            raise DockerBackendFault("docker_authority_required")
        try:
            binding = intent.fingerprint
            payload = intent.model_dump_json()
            if len(payload.encode("utf-8")) > 131072:
                raise ValueError("Launch metadata exceeds its bound.")
            digest = fingerprint(
                {"intent": binding, **event.model_dump(mode="json", exclude={"intent"})}
            )
        except ValueError:
            raise DockerBackendFault("docker_policy_mismatch") from None
        self._begin_write(connection, root, fd)
        existing = connection.execute(
            "SELECT attempt_id,intent_digest FROM execution_launches WHERE claim_id=?",
            (str(current.claim_id),),
        ).fetchone()
        if existing is not None and existing != (str(intent.attempt_id), binding):
            raise DockerBackendFault("docker_authority_required")
        if event.phase in {"create_intent", "start_intent"}:
            # A durable stop is a launch fence, not merely absence of a launch row.
            # The same SQL owner orders this check against claim/stop transactions.
            if connection.execute(
                "SELECT 1 FROM execution_lifecycles WHERE proposal_id=?",
                (str(current.proposal_id),),
            ).fetchone():
                raise DockerBackendFault("docker_authority_required")
            now, mono, healthy = self._review_time()
            original = self._authority_deadlines.get(str(current.proposal_id))
            if (
                current.claim_state != "claimed"
                or not healthy
                or original is None
                or now >= current.expires_at_ms
                or mono >= original
            ):
                raise DockerBackendFault("docker_authority_required")
        if existing is None:
            if event.phase not in {"create_intent", "unknown"}:
                raise DockerBackendFault("docker_authority_required")
            if connection.execute(
                "SELECT 1 FROM execution_launches WHERE attempt_id=?", (str(intent.attempt_id),)
            ).fetchone():
                raise DockerBackendFault("docker_authority_required")
            connection.execute(
                "INSERT INTO execution_launches (attempt_id,claim_id,intent_json,"
                "intent_digest) VALUES (?,?,?,?)",
                (str(intent.attempt_id), str(current.claim_id), payload, binding),
            )
        elif event.phase == "create_intent":
            raise DockerBackendFault("docker_mutation_unconfirmed")
        prior = connection.execute(
            "SELECT event_digest FROM execution_launch_events WHERE attempt_id=? AND phase=?",
            (str(intent.attempt_id), event.phase),
        ).fetchone()
        if prior is not None:
            if event.phase == "start_intent" or prior[0] != digest:
                raise DockerBackendFault("docker_mutation_unconfirmed")
            self._commit_write(connection, fd)
            return  # Observation deduplication never reopens a mutation gate.
        phases = {
            item[0]
            for item in connection.execute(
                "SELECT phase FROM execution_launch_events WHERE attempt_id=?",
                (str(intent.attempt_id),),
            )
        }
        owned = connection.execute(
            "SELECT container_id,created_at,observed_container_id,exit_code "
            "FROM execution_launches WHERE attempt_id=?",
            (str(intent.attempt_id),),
        ).fetchone()
        assert owned is not None
        if (
            (
                event.phase in {"created", "start_intent"}
                and ("unknown" in phases or "cleaned" in phases or current.claim_state != "claimed")
            )
            or (
                event.phase == "created"
                and ("create_intent" not in phases or "start_intent" in phases)
            )
            or (event.phase == "start_intent" and "created" not in phases)
            or (event.phase == "exited" and ("start_intent" not in phases or "cleaned" in phases))
            or (event.phase == "cleaned" and owned[0] is None)
        ):
            raise DockerBackendFault("docker_authority_required")
        values = (
            event.container_id,
            event.created_at,
            event.observed_container_id,
            event.exit_code,
        )
        if any(
            old is not None and new is not None and old != new
            for old, new in zip(owned, values, strict=True)
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")
        container = event.container_id or owned[0]
        observed = event.observed_container_id or owned[2]
        if container is not None and observed is not None and container != observed:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        connection.execute(
            "UPDATE execution_launches SET container_id=coalesce(container_id,?),"
            "created_at=coalesce(created_at,?),observed_container_id=coalesce(observed_container_id,?),"
            "exit_code=coalesce(exit_code,?) WHERE attempt_id=?",
            (*values, str(intent.attempt_id)),
        )
        connection.execute(
            "INSERT INTO execution_launch_events VALUES (?,?,?,?)",
            (str(intent.attempt_id), event.phase, len(phases) + 1, digest),
        )
        if event.phase == "unknown":
            connection.execute(
                "UPDATE execution_authorities SET claim_state='unknown' WHERE claim_id=?",
                (str(current.claim_id),),
            )
        # Cleanup facts alone cannot release admission before completion/output integration.
        self._commit_write(connection, fd)

    @staticmethod
    def _authority_record(row: tuple[object, ...]) -> AuthorityRecord:
        values = dict(zip(_AUTHORITY_COLUMNS.split(","), row, strict=True))
        values["manifest"] = ExecutionManifest.model_validate_json(str(values.pop("manifest_json")))
        return AuthorityRecord.model_validate(values)

    @staticmethod
    def _authority_payload_digest(operation: AuthorityOperation) -> str:
        project, path = str(operation.project_id), str(operation.canonical_root)
        return fingerprint(
            {
                "operation": operation.operation,
                "request": str(operation.request_id),
                "project": project,
                "root": path,
                "owner": str(operation.owner_instance_id),
                "approval": str(operation.approval_id) if operation.approval_id else None,
                "manifest": operation.manifest.fingerprint if operation.manifest else None,
                "current": operation.current_manifest_digest,
            }
        )

    def _inspect_execution(
        self, connection: sqlite3.Connection, scope: ExecutionInspectionScope, root: int, fd: int
    ) -> ExecutionInspection:
        # One actor turn: completion cannot race an authority read on this SQL owner.
        record = self._authority_work(
            connection,
            AuthorityOperation(
                operation="show",
                project_id=scope.project_id,
                canonical_root=scope.canonical_root,
                owner_instance_id=scope.owner_instance_id,
                approval_id=scope.approval_id,
            ),
            root,
            fd,
        ).record
        if record.owner_instance_id != scope.owner_instance_id:
            raise ReviewFault("execution_target_mismatch")
        stop = self._read_stop(connection, record, scope)
        has_legacy = (
            connection.execute(
                "SELECT 1 FROM execution_results r JOIN execution_launches l "
                "ON l.attempt_id=r.attempt_id "
                "WHERE l.claim_id=?",
                (str(record.claim_id),),
            ).fetchone()
            is not None
        )
        if (
            stop is not None
            and stop.terminal is not None
            and stop.terminal.outcome != "completed"
            and not has_legacy
        ):
            view, output = self._stop_payload(connection, record, stop)
            return ExecutionInspection(
                record=record, completion_state="stop_recorded", stop=view, stop_output=output
            )
        stop_view = ExecutionStopView.from_record(stop) if stop is not None else None
        if record.claim_id is None:
            return ExecutionInspection(
                record=record, completion_state="not_claimed", stop=stop_view
            )
        launch_row = connection.execute(
            "SELECT 1 FROM execution_launches WHERE claim_id=?", (str(record.claim_id),)
        ).fetchone()
        if launch_row is None:
            if record.claim_state == "finished":
                raise StorageFault("storage_schema_unsupported")
            return ExecutionInspection(record=record, completion_state="claim_only", stop=stop_view)
        launch_scope = DockerLaunchScope(
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            claim_id=record.claim_id,
        )
        launch = self._launch_status(connection, launch_scope)
        result_row = connection.execute(
            "SELECT 1 FROM execution_results WHERE attempt_id=?", (str(launch.intent.attempt_id),)
        ).fetchone()
        if result_row is None:
            if record.claim_state == "finished":
                raise StorageFault("storage_schema_unsupported")
            return ExecutionInspection(
                record=record, completion_state="launch_without_result", stop=stop_view
            )
        result = self._execution_result(connection, launch_scope)
        if stop is not None:
            stop_view = ExecutionStopView.from_record(
                stop,
                payload_state=result.payload_state,
                retirement_reason=result.retirement_reason,
                recorded_at_ms=result.recorded_at_ms,
            )
        return ExecutionInspection(
            record=record,
            completion_state="recorded",
            result=result,
            stop=stop_view,
        )

    def _lookup_proposal(
        self, connection: sqlite3.Connection, scope: ProposalRequestScope
    ) -> AuthorityRecord | None:
        if self.instance_id is None or scope.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
                (str(scope.project_id), str(scope.canonical_root)),
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_target_mismatch")
        if connection.execute(
            "SELECT 1 FROM execution_operations WHERE request_id=?", (str(scope.request_id),)
        ).fetchone():
            raise ReviewFault("execution_conflict")
        prior = connection.execute(
            "SELECT payload_digest,proposal_id FROM execution_authority_operations "
            "WHERE request_id=?",
            (str(scope.request_id),),
        ).fetchone()
        if prior is None:
            return None
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
            (prior[1],),
        ).fetchone()
        if row is None:
            raise StorageFault("storage_schema_unsupported")
        record = self._authority_record(row)
        if (record.project_id, record.canonical_root, record.owner_instance_id) != (
            scope.project_id,
            scope.canonical_root,
            scope.owner_instance_id,
        ):
            raise ReviewFault("execution_conflict")
        original = AuthorityOperation(
            operation="propose",
            request_id=scope.request_id,
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            owner_instance_id=scope.owner_instance_id,
            manifest=record.manifest,
            current_manifest_digest=record.manifest_digest,
        )
        if self._authority_payload_digest(original) != prior[0]:
            raise ReviewFault("execution_conflict")
        return record

    def _stop_target(
        self, connection: sqlite3.Connection, scope: ExecutionInspectionScope
    ) -> AuthorityRecord:
        if self.instance_id is None or scope.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities "
            "WHERE approval_id=? AND project_id=? AND canonical_root=?",
            (str(scope.approval_id), str(scope.project_id), str(scope.canonical_root)),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        record = self._authority_record(row)
        if (
            record.owner_instance_id != scope.owner_instance_id
            or connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
                (str(scope.project_id), str(scope.canonical_root)),
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_target_mismatch")
        return record

    @staticmethod
    def _read_stop(
        connection: sqlite3.Connection, authority: AuthorityRecord, scope: ExecutionInspectionScope
    ) -> ExecutionStopRecord | None:
        row = connection.execute(
            "SELECT record_json,record_digest FROM execution_lifecycles WHERE proposal_id=?",
            (str(authority.proposal_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            record = ExecutionStopRecord.model_validate_json(row[0])
            if (
                record.scope != scope
                or record.manifest_digest != authority.manifest_digest
                or record.fingerprint != row[1]
            ):
                raise ValueError("Invalid lifecycle binding.")
            return record
        except ValueError:
            raise StorageFault("storage_schema_unsupported") from None

    @staticmethod
    def _write_stop(
        connection: sqlite3.Connection, authority: AuthorityRecord, record: ExecutionStopRecord
    ) -> None:
        payload = record.model_dump_json()
        if len(payload.encode()) > 32768:
            raise ReviewFault("execution_capacity")
        connection.execute(
            "INSERT INTO execution_lifecycles VALUES (?,?,?) "
            "ON CONFLICT(proposal_id) DO UPDATE SET record_json=excluded.record_json,"
            "record_digest=excluded.record_digest",
            (str(authority.proposal_id), payload, record.fingerprint),
        )

    @staticmethod
    def _result_stop_terminal(result: DockerExecutionResult) -> ExecutionStopTerminal:
        owned = result.owned
        return ExecutionStopTerminal(
            outcome="completed" if result.disposition == "completed" else "unknown",
            evidence="owned",
            cleanup_verified=result.outcome.cleanup_verified,
            exit_code=result.outcome.exit_code,
            attempt_id=owned.intent.attempt_id,
            ownership_digest=fingerprint(
                {
                    "intent": owned.intent.fingerprint,
                    "container": owned.container_id,
                    "birth": owned.created_at,
                }
            ),
            output_facts=result.output_facts,
            output_digest=result.output_digest,
        )

    def _request_stop(
        self, connection: sqlite3.Connection, request: ExecutionStopRequest, root: int, fd: int
    ) -> ExecutionStopRecord:
        scope = ExecutionInspectionScope.model_validate(
            request.model_dump(
                include={
                    "project_id",
                    "canonical_root",
                    "owner_instance_id",
                    "approval_id",
                }
            )
        )
        authority = self._stop_target(connection, scope)
        if request.manifest_digest != authority.manifest_digest:
            raise ReviewFault("execution_target_mismatch")
        self._begin_write(connection, root, fd)
        digest = fingerprint(
            {
                "operation": "stop_v1",
                **request.model_dump(
                    mode="json",
                    exclude={"request_id"},
                ),
            }
        )
        if connection.execute(
            "SELECT 1 FROM execution_operations WHERE request_id=?", (str(request.request_id),)
        ).fetchone():
            raise ReviewFault("execution_conflict")
        prior = connection.execute(
            "SELECT payload_digest,proposal_id FROM execution_authority_operations "
            "WHERE request_id=?",
            (str(request.request_id),),
        ).fetchone()
        if prior is not None and prior != (digest, str(authority.proposal_id)):
            raise ReviewFault("execution_conflict")
        record = self._read_stop(connection, authority, scope)
        if prior is not None and record is None:
            raise StorageFault("storage_schema_unsupported")
        if record is None:
            if authority.state not in {AuthorityState.APPROVED, AuthorityState.CONSUMED}:
                raise ReviewFault("execution_conflict")
            completed: ExecutionStopTerminal | None = None
            if authority.state == AuthorityState.CONSUMED and authority.claim_state != "claimed":
                if authority.claim_state != "finished":
                    raise ReviewFault("execution_outcome_unknown")
                completed = self._result_stop_terminal(
                    self._execution_result(
                        connection,
                        DockerLaunchScope(
                            project_id=authority.project_id,
                            canonical_root=authority.canonical_root,
                            claim_id=cast(UUID, authority.claim_id),
                        ),
                    )
                )
            now, _, healthy = self._review_time()
            stamp = (
                now
                if type(now) is int and 0 <= now <= INT64_MAX - 15000
                else authority.created_at_ms
            )
            record = ExecutionStopRecord(
                scope=scope,
                request_id=request.request_id,
                manifest_digest=authority.manifest_digest,
                reason=request.reason,
                requested_at_ms=max(authority.created_at_ms, stamp),
                clock_confirmed=healthy,
                terminal=ExecutionStopTerminal(
                    outcome="timed_out" if request.reason == "timeout" else "cancelled",
                    evidence="not_started",
                    cleanup_verified=True,
                )
                if authority.state == AuthorityState.APPROVED
                else completed,
            )
            if authority.state == AuthorityState.APPROVED:
                connection.execute(
                    "UPDATE execution_authorities SET state='invalidated' WHERE proposal_id=?",
                    (str(authority.proposal_id),),
                )
            self._write_stop(connection, authority, record)
        if prior is None:
            connection.execute(
                "INSERT INTO execution_authority_operations VALUES (?,?,?)",
                (str(request.request_id), digest, str(authority.proposal_id)),
            )
        self._commit_write(connection, fd)
        return record

    def _finish_stop(
        self,
        connection: sqlite3.Connection,
        observed: ExecutionStopObservation,
        root: int,
        fd: int,
        *,
        capture: DockerOutputSnapshot | None = None,
    ) -> ExecutionStopRecord:
        if capture is not None:
            ExecutionStopCapture(observation=observed, output=capture)
        authority = self._stop_target(connection, observed.scope)
        self._begin_write(connection, root, fd)
        record = self._read_stop(connection, authority, observed.scope)
        if (
            record is None
            or observed.request_id != record.request_id
            or observed.manifest_digest != record.manifest_digest
        ):
            raise ReviewFault("execution_target_mismatch")
        if observed.terminal.outcome == "completed":
            # Only the unchanged ordinary completion port can establish v1
            # completion. A stop observation cannot manufacture its missing row.
            if (
                connection.execute(
                    "SELECT 1 FROM execution_results r JOIN execution_launches l "
                    "ON l.attempt_id=r.attempt_id WHERE l.claim_id=?",
                    (str(authority.claim_id),),
                ).fetchone()
                is None
            ):
                raise ReviewFault("execution_conflict")
            legacy = self._execution_result(
                connection,
                DockerLaunchScope(
                    project_id=authority.project_id,
                    canonical_root=authority.canonical_root,
                    claim_id=cast(UUID, authority.claim_id),
                ),
            )
            if (
                legacy.disposition != "completed"
                or self._result_stop_terminal(legacy) != observed.terminal
            ):
                raise ReviewFault("execution_conflict")
        if record.terminal is not None:
            if record.terminal != observed.terminal:
                raise ReviewFault("execution_conflict")
            if capture is not None:
                view, _ = self._stop_payload(connection, authority, record)
                if view.payload_state == "missing":
                    raise ReviewFault("execution_conflict")
            self._commit_write(connection, fd)
            return record
        terminal = observed.terminal
        if (
            authority.state != AuthorityState.CONSUMED
            or authority.claim_state not in {"claimed", "unknown"}
            or (authority.claim_state == "unknown" and terminal.outcome != "unknown")
        ):
            raise ReviewFault("execution_outcome_unknown")
        if terminal.evidence == "not_started":
            if connection.execute(
                "SELECT 1 FROM execution_launches WHERE claim_id=?", (str(authority.claim_id),)
            ).fetchone():
                raise ReviewFault("execution_outcome_unknown")
            # Positive no-launch evidence is the committed stop fence plus the
            # mandatory journal-before-mutation rule, not a missing row alone.
        elif terminal.evidence == "owned":
            owned = observed.owned
            assert owned is not None
            launch = self._launch_status(
                connection,
                DockerLaunchScope(
                    project_id=authority.project_id,
                    canonical_root=authority.canonical_root,
                    claim_id=cast(UUID, authority.claim_id),
                ),
            )
            if (
                launch.intent != owned.intent
                or launch.container_id != owned.container_id
                or launch.created_at != owned.created_at
                or terminal.exit_code != launch.exit_code
            ):
                raise ReviewFault("execution_target_mismatch")
            if terminal.outcome != "unknown" and (
                launch.uncertain
                or "cleaned" not in launch.phases
                or ("start_intent" in launch.phases and "exited" not in launch.phases)
            ):
                raise ReviewFault("execution_outcome_unknown")
            if terminal.output_facts is not None and (
                terminal.output_facts.retention_limit != authority.manifest.profile.output_bytes
            ):
                raise ReviewFault("execution_target_mismatch")
        elif terminal.outcome != "unknown":
            raise ReviewFault("execution_outcome_unknown")
        try:
            updated = ExecutionStopRecord.model_validate(
                {
                    **record.model_dump(),
                    "terminal": terminal.model_dump(),
                }
            )
        except ValueError:
            raise ReviewFault("execution_conflict") from None
        self._write_stop(connection, authority, updated)
        if capture is not None:
            self._insert_stop_payload(connection, authority, updated, capture)
        connection.execute(
            "UPDATE execution_authorities SET claim_state=? WHERE proposal_id=?",
            (
                "unknown" if terminal.outcome == "unknown" else "finished",
                str(authority.proposal_id),
            ),
        )
        self._commit_write(connection, fd)
        return updated

    def _authority_work(
        self, connection: sqlite3.Connection, operation: AuthorityOperation, root: int, fd: int
    ) -> AuthorityResult:
        if self.instance_id is None or operation.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")
        project, path = str(operation.project_id), str(operation.canonical_root)
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?", (project, path)
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_target_mismatch")
        self._begin_write(connection, root, fd)
        now, mono, healthy = self._review_time()
        live = connection.execute(
            "SELECT proposal_id,expires_at_ms,owner_instance_id FROM execution_authorities "
            "WHERE state IN ('pending','approved') LIMIT ?",
            (LIVE_REVIEW_LIMIT + 1,),
        ).fetchall()
        if len(live) > LIVE_REVIEW_LIMIT:
            raise StorageFault("storage_schema_unsupported")
        for identifier, expires, owner in live:
            state = None
            if (
                not healthy
                or owner != str(self.instance_id)
                or identifier not in self._authority_deadlines
            ):
                state = "invalidated"
            elif now >= expires or mono >= self._authority_deadlines[identifier]:
                state = "expired"
            if state is not None:
                connection.execute(
                    "UPDATE execution_authorities SET state=? WHERE proposal_id=?",
                    (state, identifier),
                )
        mutating = operation.operation != "show"
        digest = self._authority_payload_digest(operation)
        if (
            mutating
            and connection.execute(
                "SELECT 1 FROM execution_operations WHERE request_id=?",
                (str(operation.request_id),),
            ).fetchone()
        ):
            raise ReviewFault("execution_conflict")
        prior = (
            connection.execute(
                "SELECT payload_digest,proposal_id FROM execution_authority_operations "
                "WHERE request_id=?",
                (str(operation.request_id),),
            ).fetchone()
            if mutating
            else None
        )
        if prior:
            if prior[0] != digest:
                raise ReviewFault("execution_conflict")
            row = connection.execute(
                "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
                (prior[1],),
            ).fetchone()
            if row is None:
                raise StorageFault("storage_schema_unsupported")
            result = AuthorityResult(record=self._authority_record(row))
            self._commit_write(connection, fd)
            return result  # Facts only: never reconstruct the first launch permission.
        if not healthy and operation.operation in {"propose", "approve", "claim"}:
            self._commit_write(connection, fd)
            raise ReviewFault("execution_clock")
        claim_id: UUID | None = None
        if operation.operation == "propose":
            manifest = operation.manifest
            if (
                manifest is None
                or manifest.profile.project_id != operation.project_id
                or manifest.profile.canonical_root != operation.canonical_root
                or manifest.fingerprint != operation.current_manifest_digest
            ):
                raise ReviewFault("execution_target_mismatch")
            count = connection.execute("SELECT count(*) FROM execution_authorities").fetchone()[0]
            active = connection.execute(
                "SELECT count(*) FROM execution_authorities WHERE state IN ('pending','approved')"
            ).fetchone()[0]
            if count >= REVIEW_RECORD_LIMIT or active >= LIVE_REVIEW_LIMIT:
                raise ReviewFault("execution_capacity")
            identifier = str(uuid4())
            connection.execute(
                "INSERT INTO execution_authorities (proposal_id,approval_id,job_id,project_id,"
                "canonical_root,owner_instance_id,manifest_json,manifest_digest,state,created_at_ms,"
                "expires_at_ms) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    identifier,
                    str(uuid4()),
                    str(uuid4()),
                    project,
                    path,
                    str(self.instance_id),
                    canonical_bytes(manifest.model_dump(mode="json")).decode("ascii"),
                    manifest.fingerprint,
                    now,
                    now + REVIEW_TTL_SECONDS * 1000,
                ),
            )
        else:
            row = connection.execute(
                "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities "
                "WHERE approval_id=? AND project_id=? AND canonical_root=?",
                (str(operation.approval_id), project, path),
            ).fetchone()
            if row is None:
                raise ReviewFault("execution_not_found")
            record = self._authority_record(row)
            identifier = str(record.proposal_id)
            if operation.operation == "show":
                self._commit_write(connection, fd)
                return AuthorityResult(record=record)
            required = (
                AuthorityState.APPROVED
                if operation.operation == "claim"
                else AuthorityState.PENDING
            )
            if record.state != required:
                self._commit_write(connection, fd)
                raise ReviewFault(
                    "execution_expired"
                    if record.state in {AuthorityState.EXPIRED, AuthorityState.INVALIDATED}
                    else "execution_conflict"
                )
            if operation.operation in {"approve", "claim"} and (
                operation.current_manifest_digest != record.manifest_digest
            ):
                connection.execute(
                    "UPDATE execution_authorities SET state='invalidated' WHERE proposal_id=?",
                    (identifier,),
                )
                self._commit_write(connection, fd)
                raise ReviewFault("execution_target_mismatch")
            if operation.operation == "claim":
                if connection.execute(
                    "SELECT 1 FROM execution_authorities WHERE claim_state='unknown' LIMIT 1"
                ).fetchone():
                    raise ReviewFault("execution_outcome_unknown")
                if connection.execute(
                    "SELECT 1 FROM execution_authorities WHERE claim_state='claimed' LIMIT 1"
                ).fetchone():
                    raise ReviewFault("execution_capacity")
                claim_id = uuid4()
                connection.execute(
                    "UPDATE execution_authorities SET state='consumed',claim_id=?,"
                    "claim_state='claimed',"
                    "claimed_at_ms=? WHERE proposal_id=?",
                    (str(claim_id), now, identifier),
                )
            else:
                state = "approved" if operation.operation == "approve" else "denied"
                connection.execute(
                    "UPDATE execution_authorities SET state=? WHERE proposal_id=?",
                    (state, identifier),
                )
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
            (identifier,),
        ).fetchone()
        assert row is not None
        result = AuthorityResult(record=self._authority_record(row), launch_claim=claim_id)
        connection.execute(
            "INSERT INTO execution_authority_operations VALUES (?,?,?)",
            (str(operation.request_id), digest, identifier),
        )
        self._commit_write(
            connection, fd
        )  # Failure/unknown acknowledgement returns no launch claim.
        if operation.operation == "propose":
            self._authority_deadlines[identifier] = mono + REVIEW_TTL_SECONDS
        elif operation.operation == "deny":
            self._authority_deadlines.pop(identifier, None)
        return result

    def _review_time(self) -> tuple[int, float, bool]:
        wall, mono = self._review_clock(), self._review_monotonic()
        if (
            type(wall) is not int
            or not 0 <= wall <= INT64_MAX - 600000
            or not math.isfinite(mono)
            or mono < 0
            or (self._review_wall_high is not None and wall < self._review_wall_high)
            or (self._review_mono_high is not None and mono < self._review_mono_high)
        ):
            self._review_clock_bad = True
        if self._review_clock_bad:
            return self._review_wall_high or 0, self._review_mono_high or 0.0, False
        self._review_wall_high, self._review_mono_high = wall, mono
        return wall, mono, True

    @staticmethod
    def _review_record(row: tuple[object, ...]) -> ReviewRecord:
        data = dict(zip(_REVIEW_COLUMNS.split(","), row, strict=True))
        manifest = ActionManifest.model_validate_json(str(data["manifest_json"]))
        if (
            str(manifest.profile.project_id) != data["project_id"]
            or str(manifest.canonical_root) != data["canonical_root"]
            or manifest.profile.fingerprint != data["configuration_digest"]
        ):
            raise StorageFault("storage_schema_unsupported")
        state = ReviewState(str(data["state"]))
        reasons = {
            ReviewState.PENDING: "approval_required",
            ReviewState.APPROVED: "execution_unavailable",
            ReviewState.DENIED: "approval_denied",
            ReviewState.EXPIRED: "approval_expired",
            ReviewState.INVALIDATED: "approval_invalidated",
        }
        return ReviewRecord.model_validate(
            {
                "sequence": data["sequence"],
                "proposal_id": data["proposal_id"],
                "approval_id": data["approval_id"],
                "job_id": data["job_id"],
                "manifest": manifest,
                "manifest_digest": data["manifest_digest"],
                "state": state,
                "created_at_ms": data["created_at_ms"],
                "expires_at_ms": data["expires_at_ms"],
                "owner_instance_id": data["owner_instance_id"],
                "job_state": "waiting_for_approval" if state == ReviewState.PENDING else "blocked",
                "reason": reasons[state],
            }
        )

    def _review_work(
        self, connection: sqlite3.Connection, operation: ReviewOperation, root: int, fd: int
    ) -> ExecutionData:
        call = operation.call
        if self.instance_id is None or operation.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")
        project, path = str(operation.project_id), str(operation.canonical_root)
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?", (project, path)
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_target_mismatch")
        self._begin_write(connection, root, fd)
        now, mono, healthy = self._review_time()
        active = connection.execute(
            "SELECT proposal_id,expires_at_ms,owner_instance_id FROM execution_reviews "
            "WHERE state IN ('pending','approved') LIMIT ?",
            (LIVE_REVIEW_LIMIT + 1,),
        ).fetchall()
        if len(active) > LIVE_REVIEW_LIMIT:
            raise StorageFault("storage_schema_unsupported")
        for identifier, expires, owner in active:
            reason = None
            if (
                not healthy
                or owner != str(self.instance_id)
                or identifier not in self._review_deadlines
            ):
                reason = "invalidated"
            elif now >= expires or mono >= self._review_deadlines[identifier]:
                reason = "expired"
            if reason is not None:
                connection.execute(
                    "UPDATE execution_reviews SET state=? WHERE proposal_id=?", (reason, identifier)
                )
        if healthy:
            cutoff = max(0, now - (self._retention_days or 30) * 86400000)
            connection.execute(
                "DELETE FROM execution_reviews WHERE sequence IN "
                "(SELECT sequence FROM execution_reviews "
                "WHERE state IN ('denied','expired','invalidated') AND created_at_ms<? LIMIT 100)",
                (cutoff,),
            )
        mutating = call.operation in {"propose", "approve", "deny"}
        if (
            mutating
            and connection.execute(
                "SELECT 1 FROM execution_authority_operations WHERE request_id=?",
                (str(call.request_id),),
            ).fetchone()
        ):
            raise ReviewFault("execution_conflict")
        payload_digest = fingerprint(
            {
                "call": call.model_dump(mode="json"),
                "project": project,
                "root": path,
                "manifest": operation.manifest.fingerprint if operation.manifest else None,
                "configuration": operation.configuration_fingerprint,
            }
        )
        prior = (
            connection.execute(
                "SELECT project_id,canonical_root,payload_digest,proposal_id "
                "FROM execution_operations WHERE request_id=?",
                (str(call.request_id),),
            ).fetchone()
            if mutating
            else None
        )
        if prior:
            if prior[:3] != (project, path, payload_digest):
                raise ReviewFault("execution_conflict")
            row = connection.execute(
                "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
                (prior[3],),
            ).fetchone()
            if row is None:
                raise ReviewFault("execution_expired")
            self._commit_write(connection, fd)
            return ExecutionData(items=(self._review_record(row),))
        if not healthy and call.operation in {"propose", "approve"}:
            self._commit_write(connection, fd)
            raise ReviewFault("execution_clock")
        if call.operation == "propose":
            manifest = operation.manifest
            if (
                manifest is None
                or manifest.profile.project_id != operation.project_id
                or manifest.canonical_root != operation.canonical_root
            ):
                raise ReviewFault("execution_target_mismatch")
            if manifest.profile.fingerprint != operation.configuration_fingerprint:
                raise ReviewFault("execution_target_mismatch")
            total = connection.execute(
                "SELECT count(*) FROM execution_operations WHERE operation='propose'"
            ).fetchone()[0]
            live = connection.execute(
                "SELECT count(*) FROM execution_reviews WHERE state IN ('pending','approved')"
            ).fetchone()[0]
            if total >= REVIEW_RECORD_LIMIT or live >= LIVE_REVIEW_LIMIT:
                raise ReviewFault("execution_capacity")
            identifier, approval, job = str(uuid4()), str(uuid4()), str(uuid4())
            connection.execute(
                "INSERT INTO execution_reviews (proposal_id,approval_id,job_id,project_id,"
                "canonical_root,owner_instance_id,manifest_json,manifest_digest,"
                "configuration_digest,state,created_at_ms,expires_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    identifier,
                    approval,
                    job,
                    project,
                    path,
                    str(self.instance_id),
                    canonical_bytes(manifest.model_dump(mode="json")).decode("ascii"),
                    manifest.fingerprint,
                    manifest.profile.fingerprint,
                    now,
                    now + REVIEW_TTL_SECONDS * 1000,
                ),
            )
            row = connection.execute(
                "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
                (identifier,),
            ).fetchone()
        elif call.operation in {"approve", "deny", "approval.show", "job.show"}:
            column = "job_id" if call.operation == "job.show" else "approval_id"
            row = connection.execute(
                "SELECT "
                + _REVIEW_COLUMNS
                + " FROM execution_reviews WHERE "
                + column
                + "=? AND project_id=? AND canonical_root=?",
                (str(call.object_id), project, path),
            ).fetchone()
            if row is None:
                raise ReviewFault("execution_not_found")
            current = self._review_record(row)
            identifier = str(current.proposal_id)
            if call.operation in {"approve", "deny"}:
                if current.state != ReviewState.PENDING:
                    self._commit_write(connection, fd)
                    raise ReviewFault(
                        "execution_expired"
                        if current.state in {ReviewState.EXPIRED, ReviewState.INVALIDATED}
                        else "execution_conflict"
                    )
                if (
                    call.operation == "approve"
                    and operation.configuration_fingerprint != current.manifest.profile.fingerprint
                ):
                    connection.execute(
                        "UPDATE execution_reviews SET state='invalidated' WHERE proposal_id=?",
                        (identifier,),
                    )
                    self._commit_write(connection, fd)
                    raise ReviewFault("execution_target_mismatch")
                state = "approved" if call.operation == "approve" else "denied"
                connection.execute(
                    "UPDATE execution_reviews SET state=? WHERE proposal_id=? AND state='pending'",
                    (state, identifier),
                )
                row = connection.execute(
                    "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
                    (identifier,),
                ).fetchone()
        else:
            rows = connection.execute(
                "SELECT "
                + _REVIEW_COLUMNS
                + " FROM execution_reviews WHERE project_id=? AND canonical_root=? "
                "AND sequence>? ORDER BY sequence LIMIT ?",
                (project, path, call.after, call.limit + 1),
            ).fetchall()
            self._commit_write(connection, fd)
            return ExecutionData(
                items=tuple(self._review_record(item) for item in rows[: call.limit]),
                next_after=rows[call.limit - 1][0] if len(rows) > call.limit else None,
            )
        assert row is not None
        result = self._review_record(row)
        if mutating:
            connection.execute(
                "INSERT INTO execution_operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(call.request_id),
                    project,
                    path,
                    call.operation,
                    payload_digest,
                    str(result.proposal_id),
                    now,
                    result.state.value,
                ),
            )
        self._commit_write(connection, fd)
        if call.operation == "propose":
            self._review_deadlines[str(result.proposal_id)] = mono + REVIEW_TTL_SECONDS
        return ExecutionData(items=(result,))

    @staticmethod
    def _receipt(row: tuple[object, ...]) -> Receipt:
        return Receipt.model_validate(dict(zip(_COLUMNS.split(","), row, strict=True)))

    @staticmethod
    def _activity_meta(connection: sqlite3.Connection, *, legacy: bool = False) -> ActivityMeta:
        fields = "epoch,coverage_started_at_ms,high_water,generation"
        row = connection.execute(
            f"SELECT {fields}{'' if legacy else ',replay_floor'} "
            "FROM activity_meta WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise StorageFault("storage_unrecognized")
        return ActivityMeta(
            database_epoch=row[0],
            coverage_started_at_ms=row[1],
            high_water=row[2],
            retention_generation=row[3],
            replay_floor=0 if legacy else row[4],
        )

    @staticmethod
    def _activity_event(row: tuple[object, ...]) -> ActivityEvent:
        (
            sequence,
            request,
            owner,
            root,
            project,
            kind,
            at,
            code,
            category,
            elapsed,
            size,
            started,
            created,
        ) = row
        return ActivityEvent.model_validate(
            {
                "sequence": sequence,
                "request_id": request,
                "owner_instance_id": owner,
                "canonical_root": root,
                "project_id": project,
                "kind": kind,
                "recorded_at_ms": at,
                "code": code,
                "git": None
                if category is None
                else {
                    "category": category,
                    "process_started": bool(started),
                    "code": code,
                    "elapsed_ms": elapsed,
                    "output_bytes": size,
                },
                "registration_created": None if created is None else bool(created),
            }
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        receipt: Receipt,
        kind: str,
        *,
        code: str | None = None,
        record: ActivityRecord | None = None,
        fresh: bool = False,
    ) -> ActivityEvent | None:
        if (
            not fresh
            and connection.execute(
                "SELECT 1 FROM activity_requests WHERE request_id=?", (str(receipt.request_id),)
            ).fetchone()
            is None
        ):
            return None  # Existing receipts are legacy metadata, never invented traces.
        count = int(
            connection.execute(
                "SELECT count(*) FROM activity_events WHERE request_id=?",
                (str(receipt.request_id),),
            ).fetchone()[0]
        )
        terminal = kind in (
            "request.succeeded",
            "request.failed",
            "request.unknown",
            "request.interrupted",
        )
        if count >= (ACTIVITY_EVENT_LIMIT if terminal else ACTIVITY_EVENT_LIMIT - 2):
            raise StorageFault("activity_budget_exceeded")
        meta = self._activity_meta(connection)
        git = record.git if record is not None else None
        event = ActivityEvent.model_validate(
            {
                "sequence": meta.high_water + 1,
                "request_id": receipt.request_id,
                "owner_instance_id": receipt.owner_instance_id,
                "canonical_root": receipt.canonical_root,
                "project_id": receipt.project_id,
                "kind": kind,
                "recorded_at_ms": time.time_ns() // 1000000,
                "code": git.code
                if git is not None
                else activity_code(code)
                if code is not None
                else None,
                "git": git,
                "registration_created": record.registration_created if record is not None else None,
            }
        )
        connection.execute(
            "INSERT INTO activity_events(event_id,request_id,owner_instance_id,canonical_root,"
            "project_id,scope_project_id,kind,recorded_at_ms,code,git_category,elapsed_ms,output_bytes,"
            "git_started,registration_created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.sequence,
                str(event.request_id),
                str(event.owner_instance_id),
                str(event.canonical_root),
                str(event.project_id) if event.project_id else None,
                str(event.project_id) if event.project_id else None,
                event.kind,
                event.recorded_at_ms,
                event.code,
                git.category if git else None,
                git.elapsed_ms if git else None,
                git.output_bytes if git else None,
                int(git.process_started) if git else None,
                int(event.registration_created) if event.registration_created is not None else None,
            ),
        )
        connection.execute(
            "UPDATE activity_meta SET high_water=? WHERE singleton=1", (event.sequence,)
        )
        self._activity_dirty = True
        return event

    @staticmethod
    def _scoped_receipt(connection: sqlite3.Connection, scope: ReceiptScope) -> Receipt | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?", (str(scope.request_id),)
        ).fetchone()
        receipt = SQLiteStorage._receipt(row) if row is not None else None
        if receipt is not None and (
            receipt.canonical_root != scope.canonical_root
            or (
                receipt.project_id != scope.project_id
                and not (
                    scope.allow_unbound_init
                    and receipt.operation == "init"
                    and receipt.project_id is None
                )
            )
        ):
            raise StorageFault("receipt_scope_mismatch")
        return receipt

    def _stream_page(
        self, connection: sqlite3.Connection, scope: StreamRead, root: int, fd: int
    ) -> StreamPage:
        meta = self._activity_meta(connection)
        after = meta.high_water
        events: tuple[ActivityEvent, ...] = ()
        more = False
        if scope.cursor is not None:
            try:
                cursor = StreamCursor.decode(scope.cursor)
            except ValueError:
                raise StorageFault("activity_invalid_cursor") from None
            if (
                cursor.project_id != scope.project_id
                or cursor.canonical_root != scope.canonical_root
                or cursor.database_epoch != meta.database_epoch
                or cursor.retention_generation != meta.retention_generation
                or cursor.after > meta.high_water
                or cursor.after < meta.replay_floor
            ):
                raise StorageFault("activity_invalid_cursor")
            after = cursor.after
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE scope_project_id=? "
                "AND canonical_root=? AND project_id=? AND event_id>? AND event_id<=? "
                "ORDER BY event_id LIMIT ?",
                (
                    str(scope.project_id),
                    str(scope.canonical_root),
                    str(scope.project_id),
                    after,
                    meta.high_water,
                    scope.limit + 1,
                ),
            ).fetchall()
            events = tuple(self._activity_event(row) for row in rows[: scope.limit])
            more = len(rows) > scope.limit
        self._check(root, fd)
        return StreamPage(
            **meta.model_dump(),
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            start_after=after,
            scanned_through=events[-1].sequence if more else meta.high_water,
            events=events,
            has_more=more,
        )

    def _history(
        self, connection: sqlite3.Connection, scope: HistoryRead, root: int, fd: int
    ) -> HistoryPage:
        meta = self._activity_meta(connection)
        ceiling, before = meta.high_water, meta.high_water
        seek = "<=" if scope.cursor is None else "<"
        if scope.cursor is not None:
            try:
                cursor = HistoryCursor.decode(scope.cursor)
            except ValueError:
                raise StorageFault("activity_invalid_cursor") from None
            if (
                cursor.project_id != scope.project_id
                or cursor.canonical_root != scope.canonical_root
                or cursor.database_epoch != meta.database_epoch
                or cursor.retention_generation != meta.retention_generation
                or cursor.snapshot_high_water > meta.high_water
            ):
                raise StorageFault("activity_invalid_cursor")
            ceiling, before = cursor.snapshot_high_water, cursor.before
            if (
                connection.execute(
                    "SELECT 1 FROM activity_requests WHERE project_id=? AND canonical_root=? "
                    "AND accepted_sequence=?",
                    (str(scope.project_id), str(scope.canonical_root), before),
                ).fetchone()
                is None
            ):
                raise StorageFault("activity_invalid_cursor")
        columns = ",".join("r." + name for name in _COLUMNS.split(","))
        rows = connection.execute(
            f"SELECT {columns},a.accepted_sequence FROM activity_requests a "
            "JOIN request_receipts r ON r.request_id=a.request_id "
            "WHERE a.project_id=? AND a.canonical_root=? AND a.accepted_sequence<=? "
            f"AND a.accepted_sequence{seek}? "
            "AND NOT EXISTS (SELECT 1 FROM activity_events e WHERE e.request_id=a.request_id "
            "AND e.kind='identity.established' AND e.event_id>?) "
            "ORDER BY a.accepted_sequence DESC LIMIT ?",
            (
                str(scope.project_id),
                str(scope.canonical_root),
                ceiling,
                before,
                ceiling,
                scope.limit + 1,
            ),
        ).fetchall()
        items = tuple(
            HistoryItem(
                accepted_sequence=row[-1],
                **self._receipt(row[:-1]).model_dump(exclude={"owner_instance_id"}),
            )
            for row in rows[: scope.limit]
        )
        next_cursor = None
        if len(rows) > scope.limit:
            next_cursor = HistoryCursor(
                project_id=scope.project_id,
                canonical_root=scope.canonical_root,
                database_epoch=meta.database_epoch,
                retention_generation=meta.retention_generation,
                snapshot_high_water=ceiling,
                before=items[-1].accepted_sequence,
            ).encode()
        self._check(root, fd)
        return HistoryPage(
            **meta.model_dump(), snapshot_high_water=ceiling, items=items, next_cursor=next_cursor
        )

    def _activity_work(
        self, connection: sqlite3.Connection, work: _Work, root: int, fd: int
    ) -> object:
        payload = work.payload
        if work.operation == "debug":
            assert isinstance(payload, HistoryScope)
            row = connection.execute(
                "SELECT request_id FROM activity_requests WHERE project_id=? "
                "AND canonical_root=? ORDER BY accepted_sequence DESC LIMIT 1",
                (str(payload.project_id), str(payload.canonical_root)),
            ).fetchone()
            if row is None:
                raise StorageFault("no_history")
            payload = ReceiptScope(
                request_id=row[0],
                canonical_root=payload.canonical_root,
                project_id=payload.project_id,
            )
        if work.operation in ("trace", "debug"):
            assert isinstance(payload, ReceiptScope)
            receipt = self._scoped_receipt(connection, payload)
            if receipt is None:
                return None, None
            known = (
                connection.execute(
                    "SELECT 1 FROM activity_requests WHERE request_id=?", (str(payload.request_id),)
                ).fetchone()
                is not None
            )
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events "
                "WHERE request_id=? ORDER BY event_id LIMIT 33",
                (str(payload.request_id),),
            ).fetchall()
            trace = ActivityTrace(
                **self._activity_meta(connection).model_dump(),
                legacy_metadata_only=not known,
                events=tuple(self._activity_event(row) for row in rows),
            )
            self._check(root, fd)
            return receipt, trace
        if work.operation == "replay":
            assert isinstance(payload, ReplayScope)
            meta = self._activity_meta(connection)
            if (
                payload.after < meta.replay_floor
                or payload.after > meta.high_water
                or (
                    payload.expected_epoch is not None
                    and payload.expected_epoch != meta.database_epoch
                )
            ):
                raise StorageFault("activity_invalid_cursor")
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE scope_project_id=? "
                "AND canonical_root=? AND event_id>? AND event_id<=? ORDER BY event_id LIMIT ?",
                (
                    str(payload.project_id),
                    str(payload.canonical_root),
                    payload.after,
                    meta.high_water,
                    payload.limit + 1,
                ),
            ).fetchall()
            events = tuple(self._activity_event(row) for row in rows[: payload.limit])
            self._check(root, fd)
            return ActivityPage(
                **meta.model_dump(),
                events=events,
                has_more=len(rows) > payload.limit,
                next_after=events[-1].sequence if events else payload.after,
            )
        assert isinstance(payload, ActivityRecord)
        self._begin_write(connection, root, fd)
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?",
            (str(payload.request_id),),
        ).fetchone()
        receipt = self._receipt(row) if row is not None else None
        if (
            receipt is None
            or receipt.owner_instance_id != payload.owner_instance_id
            or receipt.state != "running"
            or (self.instance_id is not None and payload.owner_instance_id != self.instance_id)
        ):
            raise StorageFault("receipt_conflict")
        event = self._append_event(
            connection,
            receipt,
            "git.finished" if payload.git is not None else "registration.observed",
            record=payload,
        )
        if event is None:
            raise StorageFault("receipt_conflict")
        self._check(root, fd)
        try:
            self._commit_write(connection, fd)
            self._check(root, fd)
        except Exception:
            if not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        return event

    def _retire(self, connection: sqlite3.Connection, root: int, fd: int) -> RetirementBatch:
        """Atomically tombstone eligible completed UUIDs before deleting their details.

        Unknown/unfinished ownership remains. Publish new cursor-invalidating facts
        only after checked transaction completion; no-op priming is rolled back.
        """
        with self._mutex:
            if self._retention_progress.paused or self._closing.is_set():
                raise StorageFault("storage_closing")
        assert self._retention_days is not None
        now = self._sample_retention_wall()
        self._retention_clock_deferred = now is None
        if now is None:
            raise StorageFault("storage_busy")  # Defer; never clamp to a stale larger cutoff.
        cutoff = max(0, now - self._retention_days * 86400000)
        self._begin_write(connection, root, fd)
        meta = self._activity_meta(connection)
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts "
            "WHERE state IN ('succeeded','failed') AND finished_at_ms<? "
            "ORDER BY finished_at_ms,request_id LIMIT ?",
            (cutoff, RETIREMENT_BATCH_LIMIT + 1),
        ).fetchall()
        selected = rows[:RETIREMENT_BATCH_LIMIT]
        if selected and meta.retention_generation == INT64_MAX:
            raise StorageFault("storage_failed")
        floor = meta.replay_floor
        for row in selected:
            receipt = self._receipt(row)
            request_id = str(receipt.request_id)
            if row[0] != request_id:
                raise StorageFault("storage_unrecognized")
            membership = connection.execute(
                "SELECT canonical_root,project_id,accepted_sequence FROM activity_requests "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
            event_rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE request_id=? "
                "ORDER BY event_id LIMIT ?",
                (request_id, ACTIVITY_EVENT_LIMIT + 1),
            ).fetchall()
            trace = ActivityTrace(
                **meta.model_dump(),
                legacy_metadata_only=membership is None,
                events=tuple(self._activity_event(event) for event in event_rows),
            )
            if membership is not None and membership != (
                str(receipt.canonical_root),
                str(receipt.project_id) if receipt.project_id else None,
                trace.events[0].sequence,
            ):
                raise StorageFault("storage_unrecognized")
            if any(
                event.request_id != receipt.request_id
                or event.owner_instance_id != receipt.owner_instance_id
                or event.canonical_root != receipt.canonical_root
                for event in trace.events
            ):
                raise StorageFault("storage_unrecognized")
            floor = max(floor, trace.events[-1].sequence if trace.events else 0)
            connection.execute("INSERT INTO request_tombstones VALUES (?)", (request_id,))
            connection.execute("DELETE FROM activity_events WHERE request_id=?", (request_id,))
            connection.execute("DELETE FROM activity_requests WHERE request_id=?", (request_id,))
            connection.execute("DELETE FROM request_receipts WHERE request_id=?", (request_id,))
        payload_retired = self._retire_execution_payloads(connection, cutoff)
        if selected:
            connection.execute(
                "UPDATE activity_meta SET generation=generation+1,replay_floor=? WHERE singleton=1",
                (floor,),
            )
        result = RetirementBatch(
            **self._activity_meta(connection).model_dump(),
            retired_count=len(selected),
            has_more=len(rows) > RETIREMENT_BATCH_LIMIT
            or connection.execute(
                "SELECT 1 FROM execution_results WHERE stdout IS NOT NULL "
                "AND recorded_at_ms<? LIMIT 1",
                (cutoff,),
            ).fetchone()
            is not None,
        )
        self._check(root, fd)
        try:
            if selected or payload_retired:
                self._commit_write(connection, fd)
            else:
                self._rollback_write(connection, fd)
            self._check(root, fd)
        except Exception:
            if not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        self._publish_retention(result)  # Checked transaction completion precedes the cache.
        self._activity_dirty = bool(selected)
        return result

    def _sample_retention_wall(self) -> int | None:
        try:
            now = self._retention_clock()
        except Exception:
            return None
        if (
            type(now) is not int
            or not 0 <= now <= INT64_MAX
            or (self._retention_wall_high is not None and now < self._retention_wall_high)
        ):
            return None
        self._retention_wall_high = now
        return now

    def _retention_now(self) -> float | None:
        try:
            value = self._retention_monotonic()
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or not math.isfinite(value + RETIREMENT_INTERVAL_SECONDS)
                or value + RETIREMENT_INTERVAL_SECONDS - value < RETIREMENT_INTERVAL_SECONDS
                or (self._retention_mono_high is not None and value < self._retention_mono_high)
            ):
                return None
            self._retention_mono_high = float(value)
            return float(value)
        except Exception:
            return None

    def _retention_end(
        self, *, batch: RetirementBatch | None = None, fault: StorageFault | None = None
    ) -> None:
        completed = self._retention_now()
        with self._mutex:
            progress = self._retention_progress
            self._retention_progress = progress.model_copy(
                update={
                    "active": False,
                    "next_attempt_at": completed + RETIREMENT_INTERVAL_SECONDS
                    if completed is not None
                    and progress.enabled
                    and not progress.paused
                    and not self._closing.is_set()
                    else None,
                    "last_outcome": "committed"
                    if fault is None
                    else "clock_deferred"
                    if self._retention_clock_deferred
                    else "busy"
                    if fault.code in ("storage_busy", "storage_closing")
                    else "failed",
                    "last_batch": batch if batch is not None else progress.last_batch,
                    "error": fault.code if fault is not None else None,
                }
            )

    def _maintain_due(self, connection: sqlite3.Connection, root: int, fd: int) -> None:
        now = self._retention_now()
        with self._mutex:
            progress = self._retention_progress
            if (
                not progress.enabled
                or progress.paused
                or self._closing.is_set()
                or self._state.state != "ready"
                or now is None
            ):
                return
            if progress.next_attempt_at is None:
                self._retention_progress = progress.model_copy(
                    update={
                        "next_attempt_at": now + RETIREMENT_INTERVAL_SECONDS,
                    }
                )
                return
            if now < progress.next_attempt_at:
                return
            self._retention_progress = progress.model_copy(
                update={
                    "active": True,
                    "attempts": progress.attempts + 1,
                    "next_attempt_at": None,
                }
            )
        self._activity_dirty = False
        self._retention_clock_deferred = False
        try:
            batch = self._retire(connection, root, fd)
            if self._activity_dirty and self._commit_hint is not None:
                try:
                    self._commit_hint()
                except Exception:
                    self._notification_failed.set()
            self._retention_end(batch=batch)
        except Exception as error:
            try:
                if connection.in_transaction:
                    self._rollback_write(connection, fd)
            except sqlite3.Error:
                fault = StorageFault("storage_outcome_unconfirmed")
            else:
                fault = _fault(error)
            if fault.code not in ("storage_busy", "storage_closing"):
                self._failed(fault)
                self._closing.set()
            self._retention_end(fault=fault)

    def _reconcile(
        self, connection: sqlite3.Connection, instance: UUID, root: int, fd: int
    ) -> None:
        """Mark prior-owner unfinished receipts interrupted/unknown, never re-execute them."""
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
        while True:
            if time.monotonic() >= deadline:
                raise StorageFault("storage_busy")
            self._begin_write(connection, root, fd)
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM request_receipts WHERE state IN ('accepted','running') "
                "AND owner_instance_id<>? LIMIT 100",
                (str(instance),),
            ).fetchall()
            if not rows:
                self._rollback_write(connection, fd)  # Undo the primer when there is no work.
                return
            for row in rows:
                receipt = self._receipt(row)
                connection.execute(
                    "UPDATE request_receipts SET state='unknown',finished_at_ms=?,"
                    "outcome_code='interrupted' WHERE request_id=?",
                    (time.time_ns() // 1000000, str(receipt.request_id)),
                )
                self._append_event(connection, receipt, "request.interrupted", code="interrupted")
            self._commit_write(connection, fd)

    def _receipt_work(
        self, connection: sqlite3.Connection, work: _Work, root: int, fd: int
    ) -> object:
        payload = work.payload
        assert isinstance(payload, (ReceiptClaim, ReceiptTransition, ReceiptScope))
        writing = work.operation != "inspect"
        if writing:
            self._begin_write(connection, root, fd)
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?",
            (str(payload.request_id),),
        ).fetchone()
        receipt = self._receipt(row) if row is not None else None
        result: object
        if isinstance(payload, ReceiptClaim):
            if receipt is None and (
                self.instance_id is not None and payload.owner_instance_id != self.instance_id
            ):
                raise StorageFault("receipt_conflict")
            retired = connection.execute(
                "SELECT 1 FROM request_tombstones WHERE request_id=?", (str(payload.request_id),)
            ).fetchone()
            if retired is not None:
                raise StorageFault(
                    "storage_unrecognized" if receipt is not None else "request_retired"
                )
            if receipt is not None:
                if (
                    receipt.operation != payload.operation
                    or receipt.canonical_root != payload.canonical_root
                    or (
                        receipt.project_id != payload.project_id
                        and not (receipt.operation == "init" and receipt.project_id is None)
                    )
                ):
                    raise StorageFault("receipt_conflict")
                result = ReceiptClaimResult(created=False, receipt=receipt)
            else:
                if self.instance_id is not None and payload.owner_instance_id != self.instance_id:
                    raise StorageFault("receipt_conflict")
                receipt = Receipt(
                    **payload.model_dump(),
                    state="accepted",
                    accepted_at_ms=time.time_ns() // 1000000,
                )
                connection.execute(
                    f"INSERT INTO request_receipts({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(receipt.request_id),
                        str(receipt.owner_instance_id),
                        receipt.operation,
                        str(receipt.canonical_root),
                        str(receipt.project_id) if receipt.project_id is not None else None,
                        receipt.state,
                        receipt.accepted_at_ms,
                        None,
                        None,
                    ),
                )
                event = self._append_event(connection, receipt, "request.accepted", fresh=True)
                assert event is not None
                connection.execute(
                    "INSERT INTO activity_requests VALUES (?,?,?,?)",
                    (
                        str(receipt.request_id),
                        str(receipt.canonical_root),
                        str(receipt.project_id) if receipt.project_id else None,
                        event.sequence,
                    ),
                )
                result = ReceiptClaimResult(created=True, receipt=receipt)
        elif isinstance(payload, ReceiptScope):
            if receipt is not None and (
                receipt.canonical_root != payload.canonical_root
                or (
                    receipt.project_id != payload.project_id
                    and not (
                        payload.allow_unbound_init
                        and receipt.operation == "init"
                        and receipt.project_id is None
                    )
                )
            ):
                raise StorageFault("receipt_scope_mismatch")
            result = receipt
        else:
            associating = (
                receipt is not None
                and receipt.operation == "init"
                and receipt.project_id is None
                and receipt.state == "running"
                and payload.state == "running"
                and payload.project_id is not None
            )
            if (
                receipt is None
                or receipt.owner_instance_id != payload.owner_instance_id
                or (self.instance_id is not None and payload.owner_instance_id != self.instance_id)
                or receipt.state not in ("accepted", "running")
                or (payload.state == "running" and receipt.state != "accepted" and not associating)
                or (payload.project_id is not None and not associating)
            ):
                raise StorageFault("receipt_conflict")
            finished = None if payload.state == "running" else time.time_ns() // 1000000
            project_id = payload.project_id if associating else receipt.project_id
            connection.execute(
                "UPDATE request_receipts SET state=?,finished_at_ms=?,outcome_code=?,project_id=? "
                "WHERE request_id=?",
                (
                    payload.state,
                    finished,
                    payload.outcome_code,
                    str(project_id) if project_id is not None else None,
                    str(payload.request_id),
                ),
            )
            result = Receipt.model_validate(
                {
                    **receipt.model_dump(),
                    "state": payload.state,
                    "project_id": project_id,
                    "finished_at_ms": finished,
                    "outcome_code": payload.outcome_code,
                }
            )
        if isinstance(payload, ReceiptTransition):
            assert isinstance(result, Receipt)
            if associating:
                connection.execute(
                    "UPDATE activity_requests SET project_id=? WHERE request_id=?",
                    (str(result.project_id), str(result.request_id)),
                )
                connection.execute(
                    "UPDATE activity_events SET scope_project_id=? WHERE request_id=?",
                    (str(result.project_id), str(result.request_id)),
                )
            kind = "identity.established" if associating else "request." + payload.state
            self._append_event(
                connection, result, kind, code=payload.error_code or payload.outcome_code
            )
        self._check(root, fd)
        if writing:
            try:
                if isinstance(result, ReceiptClaimResult) and not result.created:
                    self._rollback_write(connection, fd)
                else:
                    self._commit_write(connection, fd)
                self._check(root, fd)
            except Exception:
                if not connection.in_transaction:
                    raise StorageFault("storage_outcome_unconfirmed") from None
                raise
        return result

    def _release(self, connection: sqlite3.Connection | None, fd: int) -> None:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            self._close_fault = StorageFault("storage_outcome_unconfirmed")
            self._failed(self._close_fault)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    self._close_fault = StorageFault("storage_outcome_unconfirmed")
                    self._failed(self._close_fault)

    def _failed(self, fault: StorageFault) -> None:
        with self._mutex:
            self._accepting = False
            self._state = StorageReadiness(state="failed", error=fault.code)
            self._retention_position = None
            self._execution_backend = None

    def _run(self) -> None:
        connection = None
        fd = -1
        try:
            settings, source = load_or_create_settings(self.owner)
            self._retention_days = settings.history.retention_days
            self._execution_profiles = settings.execution.profiles
            with self.owner.store.directory(self.owner.store.layout.data_dir, create=False) as root:
                connection, fd = self._open(root)
                self._publish_retention(self._activity_meta(connection))
                # Ready-time baseline; invalid time cannot trigger purge.
                self._sample_retention_wall()
                with self._mutex:
                    if self._closing.is_set():
                        self._state = StorageReadiness(state="closing")
                    else:
                        self._accepting = True
                        ready_at = self._retention_now()
                        if (
                            self._retention_progress.enabled
                            and not self._retention_progress.paused
                            and ready_at is not None
                        ):
                            self._retention_progress = self._retention_progress.model_copy(
                                update={
                                    "next_attempt_at": ready_at + RETIREMENT_INTERVAL_SECONDS,
                                }
                            )
                        self._execution_backend = (
                            settings.backend
                            if isinstance(settings, ConfiguredRuntimeSettings)
                            else None
                        )
                        self._state = StorageReadiness(
                            state="ready",
                            schema_version=DATABASE_SCHEMA,
                            retention_days=settings.history.retention_days,
                            settings_source=source,
                        )
                self._initialized.set()
                try:
                    while not self._closing.is_set() or not self._queue.empty():
                        try:
                            work = self._queue.get(timeout=0.05)
                        except queue.Empty:
                            self._maintain_due(connection, root, fd)
                            continue
                        try:
                            if self.snapshot().state == "failed":
                                if not work.result.done():
                                    work.result.set_exception(StorageFault("storage_closing"))
                                continue
                            if not work.result.set_running_or_notify_cancel():
                                continue
                            if time.monotonic() >= work.deadline:
                                raise StorageFault("storage_busy")
                            self._activity_dirty = False
                            value = self._execute(connection, work, root, fd)
                            if self._activity_dirty and self._commit_hint is not None:
                                try:
                                    self._commit_hint()
                                except Exception:
                                    # COMMIT already happened: delivery failure cannot undo it.
                                    self._notification_failed.set()
                            work.result.set_result(value)
                        except Exception as error:  # thread boundary never leaks raw driver facts
                            try:
                                if connection.in_transaction:
                                    self._rollback_write(connection, fd)
                            except Exception:  # Resolve active work even if proof retirement fails.
                                fault = StorageFault("storage_outcome_unconfirmed")
                                self._failed(fault)
                                self._closing.set()
                            else:
                                if isinstance(error, (ReviewFault, DockerBackendFault)):
                                    if not work.result.done():
                                        work.result.set_exception(error)
                                    continue
                                fault = _fault(error)
                            if fault.code not in (
                                "storage_busy",
                                "storage_closing",
                                "registry_conflict",
                                "receipt_conflict",
                                "request_retired",
                                "receipt_scope_mismatch",
                                "activity_invalid_cursor",
                                "activity_budget_exceeded",
                                "no_history",
                            ):
                                self._failed(fault)
                                self._closing.set()
                            if not work.result.done():
                                work.result.set_exception(fault)
                        finally:
                            self._queue.task_done()
                            self._maintain_due(connection, root, fd)
                finally:
                    closing_connection, closing_fd = connection, fd
                    connection, fd = None, -1
                    self._release(closing_connection, closing_fd)
        except Exception as error:  # one owned thread's safe terminal boundary
            self._failed(self._close_fault or _fault(error))
        finally:
            self._release(connection, fd)
            with self._mutex:
                self._accepting = False
                self._execution_backend = None
            self._initialized.set()
            while True:
                try:
                    abandoned = self._queue.get_nowait()
                except queue.Empty:
                    break
                if not abandoned.result.done():
                    abandoned.result.set_exception(StorageFault("storage_closing"))
                self._queue.task_done()
