"""Mutable finite actor work envelope; no connection or execution owner."""

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jeff.application.execution import (
    AuthorityOperation,
    DockerCompletion,
    DockerLaunchEvent,
    DockerLaunchScope,
    ExecutionCancelCall,
    ExecutionInspectionScope,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRequest,
    ProposalRequestScope,
    ReviewOperation,
)
from jeff.application.history import ActivityRecord, HistoryScope, ReplayScope
from jeff.application.storage import ReceiptClaim, ReceiptScope, ReceiptTransition
from jeff.domain.projects import Project


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
