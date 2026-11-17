"""Typed dispatcher storage and client ports; no arbitrary callbacks or fallback."""

from pathlib import Path
from typing import Protocol

from jeff.application.projects import Failure
from jeff.application.storage import ProjectRegistry, RequestLedger

from .replies import DispatchReply, EvidenceReply, ReceiptReply
from .requests import EvidenceQuery, ProjectCall, ReceiptQuery
from .status import ProjectStatusSuccess


class DispatchStore(RequestLedger, ProjectRegistry, Protocol):
    """Combine typed ledger/registry ports without granting arbitrary SQL callbacks."""

    pass


class EvidenceClient(Protocol):
    """Inspect bounded request metadata, never retrieve a cached raw answer."""

    def inspect_evidence(self, query: EvidenceQuery) -> EvidenceReply: ...


class ProjectClient(EvidenceClient, Protocol):
    """Separate daemon actions from local read-only status; no in-process fallback."""

    def execute(self, call: ProjectCall) -> DispatchReply: ...
    def inspect_receipt(self, query: ReceiptQuery) -> ReceiptReply: ...
    def status(self, directory: Path) -> ProjectStatusSuccess | Failure: ...
