"""Dispatch evidence reads preserve scope and fixed failures without native I/O."""

import time
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from jeff.application.dispatch import (
    DispatchStore,
    EvidenceQuery,
    EvidenceReply,
    ProjectCall,
    ProjectDispatcher,
    ReceiptQuery,
    ReceiptReply,
    StreamQuery,
    StreamReply,
)
from jeff.application.history import StreamPage
from jeff.application.projects import ErrorData, Failure, ProjectData, ProjectService, Success
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.application.storage import StorageFault

ROOT = Path("/synthetic/project")
PROJECT_ID = UUID(int=1)


def _dispatcher() -> tuple[ProjectDispatcher, Mock, Mock]:
    projects = create_autospec(ProjectService, instance=True)
    projects.execute.return_value = Success(
        data=ProjectData(
            project_id=str(PROJECT_ID), root=str(ROOT), config_path=str(ROOT / "jeff.local.yaml")
        )
    )
    store = create_autospec(DispatchStore, instance=True)
    dispatcher = ProjectDispatcher(
        projects,
        create_autospec(QuestionService, instance=True),
        store,
        RuntimeIdentityData(instance_id=UUID(int=2), pid=1),
    )
    return dispatcher, projects, store


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("trace", "trace"),
        ("debug", "debug"),
        ("history", "history"),
        ("replay", "replay"),
        ("stream", "stream_page"),
        ("receipt", "inspect"),
    ],
)
@pytest.mark.parametrize("code", ["receipt_scope_mismatch", "storage_failed"])
def test_evidence_operations_keep_distinct_storage_failure_mapping(
    operation: Literal["trace", "debug", "history", "replay", "stream", "receipt"],
    method: str,
    code: Literal["receipt_scope_mismatch", "storage_failed"],
) -> None:
    dispatcher, _, store = _dispatcher()
    getattr(store, method).side_effect = StorageFault(code)
    reply: StreamReply | ReceiptReply | EvidenceReply
    deadline = time.monotonic() + 10
    if operation == "stream":
        reply = dispatcher.read_stream(StreamQuery(directory=ROOT), deadline)
    elif operation == "receipt":
        reply = dispatcher.inspect_receipt(
            ReceiptQuery(directory=ROOT, request_id=UUID(int=3)), deadline
        )
    else:
        reply = dispatcher.inspect_evidence(
            EvidenceQuery(
                directory=ROOT,
                operation=operation,
                request_id=UUID(int=3) if operation == "trace" else None,
            ),
            deadline,
        )
    assert reply.error is not None
    expected = (
        "receipt_unavailable"
        if operation == "receipt" or (operation != "stream" and code == "receipt_scope_mismatch")
        else code
    )
    assert reply.error.error.code == expected
    store.index.assert_not_called()
    store.claim.assert_not_called()
    store.finish.assert_not_called()


@pytest.mark.parametrize("operation", ["trace", "debug", "receipt"])
def test_exact_uninitialized_evidence_keeps_nullable_init_scope(
    operation: Literal["trace", "debug", "receipt"],
) -> None:
    dispatcher, projects, store = _dispatcher()
    projects.execute.return_value = Failure(
        error=ErrorData(code="project_not_initialized", message="Synthetic missing registration")
    )
    projects.resolve_root.return_value = ROOT
    store.trace.return_value = (None, None)
    store.inspect.return_value = None
    query: ReceiptQuery | EvidenceQuery
    reply: ReceiptReply | EvidenceReply
    deadline = time.monotonic() + 10
    if operation == "receipt":
        query = ReceiptQuery(directory=ROOT, request_id=UUID(int=3))
        reply = dispatcher.inspect_receipt(query, deadline)
        scope = store.inspect.call_args.args[0]
    else:
        query = EvidenceQuery(directory=ROOT, operation=operation, request_id=UUID(int=3))
        reply = dispatcher.inspect_evidence(query, deadline)
        scope = store.trace.call_args.args[0]
    assert reply.error is None and reply.request_id == query.query_id
    assert scope.canonical_root == ROOT and scope.project_id is None
    assert scope.allow_unbound_init is True
    projects.resolve_root.assert_called_once_with(ROOT, deadline=deadline)
    store.index.assert_not_called()
    store.claim.assert_not_called()
    store.finish.assert_not_called()


def test_stream_read_preserves_scope_cursor_and_committed_page() -> None:
    dispatcher, _, store = _dispatcher()
    page = StreamPage(
        project_id=PROJECT_ID,
        canonical_root=ROOT,
        database_epoch=UUID(int=4),
        coverage_started_at_ms=1,
        high_water=7,
        retention_generation=0,
        start_after=3,
        scanned_through=7,
        events=(),
        has_more=False,
    )
    store.stream_page.return_value = page
    deadline = time.monotonic() + 10
    reply = dispatcher.read_stream(StreamQuery(directory=ROOT, cursor="opaque", limit=7), deadline)
    assert reply.page == page and reply.error is None
    read = store.stream_page.call_args.args[0]
    assert (read.project_id, read.canonical_root, read.cursor, read.limit) == (
        PROJECT_ID,
        ROOT,
        "opaque",
        7,
    )
    assert store.stream_page.call_args.kwargs == {"deadline": deadline}


def test_unsupported_intent_never_claims_or_queries_project() -> None:
    dispatcher, projects, store = _dispatcher()
    reply = dispatcher.execute(
        ProjectCall(directory=ROOT, operation="ask", question="Unsupported synthetic question"),
        time.monotonic() + 10,
    )
    assert reply.accepted is False and not reply.result.ok
    assert reply.result.error.code == "unsupported_intent"
    projects.execute.assert_not_called()
    store.claim.assert_not_called()
