"""Explicit lifetime/state owner with direct, same-receiver method bindings.

No mixins, extra actors or replacement lock/queue owners. Leaf methods still
dispatch through this instance, including existing subclass and instance hooks.
"""

import queue
import threading
import time
from collections.abc import Callable
from uuid import UUID

from jeff.application.execution import DockerBackendConfiguration, ExecutionProfile
from jeff.application.history import ActivityMeta, RetentionPosition
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
from jeff.application.storage import (
    STORAGE_PENDING_LIMIT,
    RetentionProgress,
    StorageFault,
    StorageReadiness,
)
from jeff.infrastructure.runtime_state import Ownership

from . import authority as _authority
from . import (
    completion,
    dispatch,
    events,
    files,
    inspection,
    launches,
    opening,
    payload_retention,
    receipts,
    requests,
    results,
    retention,
    reviews,
    runner,
    stop_work,
    stops,
    submission,
    transactions,
    watches,
)
from . import history as _history_methods
from .work import _Work


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

    index = requests.index

    lookup = requests.lookup

    root_claimed = requests.root_claimed

    claim = requests.claim

    finish = requests.finish

    inspect = requests.inspect

    observe = requests.observe

    trace = requests.trace

    replay = requests.replay

    debug = requests.debug

    history = requests.history

    stream_page = requests.stream_page

    retire = requests.retire

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

    review = requests.review

    authority = requests.authority

    submit_cancel = requests.submit_cancel

    request_stop = requests.request_stop

    finish_stop = requests.finish_stop

    finish_stop_with_capture = requests.finish_stop_with_capture

    inspect_stop = requests.inspect_stop

    watch = requests.watch
    inspect_execution = requests.inspect_execution

    list_executions = requests.list_executions

    lookup_proposal = requests.lookup_proposal

    record = requests.record

    launch_status = requests.launch_status

    complete_execution = requests.complete_execution

    execution_result = requests.execution_result

    _submit = submission._submit

    _enqueue = submission._enqueue

    _begin_write = transactions._begin_write

    _commit_write = transactions._commit_write

    _rollback_write = transactions._rollback_write

    _authorize_recovery = files._authorize_recovery

    _sidecars = files._sidecars

    _check = files._check

    _open = opening._open

    _execute = dispatch._execute

    _execution_result = results._execution_result

    _retire_execution_payloads = staticmethod(payload_retention._retire_execution_payloads)

    _retained_payloads = staticmethod(payload_retention._retained_payloads)

    _insert_stop_payload = results._insert_stop_payload

    _stop_payload = results._stop_payload

    _complete_execution = completion._complete_execution

    _launch_status = launches._launch_status

    _launch_record = launches._launch_record

    _authority_record = staticmethod(_authority._authority_record)

    _authority_payload_digest = staticmethod(_authority._authority_payload_digest)

    _watch = watches._watch
    _inspect_execution = inspection._inspect_execution

    _list_executions = inspection._list_executions

    _lookup_proposal = inspection._lookup_proposal

    _stop_target = stops._stop_target

    _read_stop = staticmethod(stops._read_stop)

    _write_stop = staticmethod(stops._write_stop)

    _result_stop_terminal = staticmethod(stops._result_stop_terminal)

    _request_stop = stop_work._request_stop

    _finish_stop = stop_work._finish_stop

    _authority_work = _authority._authority_work

    _review_time = reviews._review_time

    _review_record = staticmethod(reviews._review_record)

    _review_work = reviews._review_work

    _receipt = staticmethod(events._receipt)

    _activity_meta = staticmethod(events._activity_meta)

    _activity_event = staticmethod(events._activity_event)

    _append_event = events._append_event

    _scoped_receipt = staticmethod(receipts._scoped_receipt)

    _stream_page = _history_methods._stream_page

    _history = _history_methods._history

    _activity_work = _history_methods._activity_work

    _retire = retention._retire

    _sample_retention_wall = retention._sample_retention_wall

    _retention_now = retention._retention_now

    _retention_end = retention._retention_end

    _maintain_due = retention._maintain_due

    _reconcile = receipts._reconcile

    _receipt_work = receipts._receipt_work

    _release = runner._release

    _failed = runner._failed

    _run = runner._run
