"""Scoped evidence and committed stream reads using the same dispatcher instance."""

from typing import TYPE_CHECKING

from jeff.application.history import (
    ActivityTrace,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    StreamRead,
)
from jeff.application.projects import Failure
from jeff.application.storage import Receipt, ReceiptScope, StorageFault, storage_failure

from .failures import dispatch_failure
from .replies import EvidenceReply, ReceiptReply
from .requests import EvidenceQuery, ReceiptQuery
from .subscriptions import StreamQuery, StreamReply

if TYPE_CHECKING:
    from .dispatcher import ProjectDispatcher


def read_stream(
    self: "ProjectDispatcher", query: StreamQuery, absolute_deadline: float
) -> StreamReply:
    """Rebind the current project before reading a committed, cursor-checked page."""
    bound = self._bind(query, absolute_deadline)
    if isinstance(bound, Failure):
        return StreamReply(error=bound)
    try:
        page = self.store.stream_page(
            StreamRead(
                project_id=bound.project_id,
                canonical_root=bound.root,
                cursor=query.cursor,
                limit=query.limit,
            ),
            deadline=absolute_deadline,
        )
        return StreamReply(page=page)
    except StorageFault as error:
        return StreamReply(error=storage_failure(error.code))


def inspect_evidence(
    self: "ProjectDispatcher", query: EvidenceQuery, absolute_deadline: float
) -> EvidenceReply:
    """Read scoped evidence without registration repair or invented nonexecution proof."""

    def failed(error: Failure) -> EvidenceReply:
        return EvidenceReply(
            request_id=query.query_id,
            identity=self.identity,
            operation=query.operation,
            error=error,
        )

    bound = self._bind(query, absolute_deadline)
    if isinstance(bound, Failure):
        if (
            query.operation not in ("trace", "debug")
            or query.request_id is None
            or bound.error.code != "project_not_initialized"
        ):
            return failed(bound)
        root_result = self.projects.resolve_root(query.directory, deadline=absolute_deadline)
        if isinstance(root_result, Failure):
            return failed(root_result)
        if query.expected_project_id is not None or (
            query.expected_root is not None and query.expected_root != root_result
        ):
            return failed(dispatch_failure("project_target_mismatch"))
        root, project_id = root_result, None
    else:
        root, project_id = bound.root, bound.project_id
    receipt: Receipt | None
    trace: ActivityTrace | None
    try:
        if query.operation == "history":
            assert project_id is not None
            history = self.store.history(
                HistoryRead(
                    project_id=project_id,
                    canonical_root=root,
                    limit=query.limit,
                    cursor=query.cursor,
                ),
                deadline=absolute_deadline,
            )
            return EvidenceReply(
                request_id=query.query_id,
                identity=self.identity,
                operation=query.operation,
                history=history,
            )
        if query.operation == "debug" and query.request_id is None:
            assert project_id is not None
            receipt, trace = self.store.debug(
                HistoryScope(project_id=project_id, canonical_root=root),
                deadline=absolute_deadline,
            )
            return EvidenceReply(
                request_id=query.query_id,
                identity=self.identity,
                operation=query.operation,
                receipt=receipt,
                trace=trace,
            )
        if query.operation in ("trace", "debug"):
            assert query.request_id is not None
            receipt, trace = self.store.trace(
                ReceiptScope(
                    request_id=query.request_id,
                    canonical_root=root,
                    project_id=project_id,
                    allow_unbound_init=True,
                ),
                deadline=absolute_deadline,
            )
            return EvidenceReply(
                request_id=query.query_id,
                identity=self.identity,
                operation=query.operation,
                receipt=receipt,
                trace=trace,
            )
        assert project_id is not None
        page = self.store.replay(
            ReplayScope(
                project_id=project_id,
                canonical_root=root,
                after=query.after,
                limit=query.limit,
                expected_epoch=query.expected_epoch,
            ),
            deadline=absolute_deadline,
        )
        return EvidenceReply(
            request_id=query.query_id,
            identity=self.identity,
            operation=query.operation,
            page=page,
        )
    except StorageFault as error:
        return failed(
            dispatch_failure("receipt_unavailable")
            if error.code == "receipt_scope_mismatch"
            else storage_failure(error.code)
        )


def inspect_receipt(
    self: "ProjectDispatcher", query: ReceiptQuery, absolute_deadline: float
) -> ReceiptReply:
    bound = self._bind(query, absolute_deadline)
    if isinstance(bound, Failure):
        if bound.error.code != "project_not_initialized":
            return ReceiptReply(request_id=query.query_id, identity=self.identity, error=bound)
        root_result = self.projects.resolve_root(query.directory, deadline=absolute_deadline)
        if isinstance(root_result, Failure):
            return ReceiptReply(
                request_id=query.query_id, identity=self.identity, error=root_result
            )
        if query.expected_project_id is not None or (
            query.expected_root is not None and query.expected_root != root_result
        ):
            return ReceiptReply(
                request_id=query.query_id,
                identity=self.identity,
                error=dispatch_failure("project_target_mismatch"),
            )
        root, project_id = root_result, None
    else:
        root, project_id = bound.root, bound.project_id
    try:
        receipt = self.store.inspect(
            ReceiptScope(
                request_id=query.request_id,
                canonical_root=root,
                project_id=project_id,
                allow_unbound_init=True,
            ),
            deadline=absolute_deadline,
        )
        return ReceiptReply(request_id=query.query_id, identity=self.identity, receipt=receipt)
    except StorageFault:
        return ReceiptReply(
            request_id=query.query_id,
            identity=self.identity,
            error=dispatch_failure("receipt_unavailable"),
        )
