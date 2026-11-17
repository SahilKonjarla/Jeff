"""Private SQLite registry with one connection-owning thread and bounded declared work.

Python SQLite opens a pathname, not our checked descriptor. Private ancestry plus
before/after identity checks narrow races; this is not a hostile same-user/ACL proof.
"""

from .catalog import _ACTIVITY_EVENTS as _ACTIVITY_EVENTS
from .catalog import _ACTIVITY_INDEXES as _ACTIVITY_INDEXES
from .catalog import _ACTIVITY_META as _ACTIVITY_META
from .catalog import _ACTIVITY_META_V4 as _ACTIVITY_META_V4
from .catalog import _ACTIVITY_REQUESTS as _ACTIVITY_REQUESTS
from .catalog import _AUTHORITIES as _AUTHORITIES
from .catalog import _AUTHORITY_COLUMNS as _AUTHORITY_COLUMNS
from .catalog import _AUTHORITY_OPS as _AUTHORITY_OPS
from .catalog import _COLUMNS as _COLUMNS
from .catalog import _EVENT_COLUMNS as _EVENT_COLUMNS
from .catalog import _EXPIRABLE_INDEX as _EXPIRABLE_INDEX
from .catalog import _FLOOR_COLUMN as _FLOOR_COLUMN
from .catalog import _LAUNCH_EVENTS as _LAUNCH_EVENTS
from .catalog import _LAUNCHES as _LAUNCHES
from .catalog import _LEDGER as _LEDGER
from .catalog import _LIFECYCLES as _LIFECYCLES
from .catalog import _PROJECTS as _PROJECTS
from .catalog import _RECEIPT_INDEX as _RECEIPT_INDEX
from .catalog import _RECEIPTS as _RECEIPTS
from .catalog import _RESULT_PAYLOADS as _RESULT_PAYLOADS
from .catalog import _RESULT_QUARANTINE as _RESULT_QUARANTINE
from .catalog import _RESULTS as _RESULTS
from .catalog import _REVIEW_COLUMNS as _REVIEW_COLUMNS
from .catalog import _REVIEW_OPS as _REVIEW_OPS
from .catalog import _REVIEWS as _REVIEWS
from .catalog import _STOP_PAYLOADS as _STOP_PAYLOADS
from .catalog import _TOMBSTONES as _TOMBSTONES
from .files import _FLAGS as _FLAGS
from .files import _SIDECARS as _SIDECARS
from .files import _signature as _signature
from .security import _fault as _fault
from .security import _guard as _guard
from .security import _schema as _schema
from .stopped import inspect_stopped_storage
from .store import SQLiteStorage
from .work import _Work as _Work

__all__ = ["SQLiteStorage", "inspect_stopped_storage"]
