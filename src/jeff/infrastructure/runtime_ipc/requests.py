"""Strict discriminated native RPC request kinds."""

from typing import Annotated

from pydantic import Field, RootModel

from jeff.application.dispatch import (
    EvidenceRequest,
    ExecutionCancelRequest,
    ExecutionRequest,
    FreshExecutionRequest,
    ProjectRequest,
    ReceiptRequest,
    SubscriptionRequest,
)
from jeff.application.runtime import ControlRequest


class RpcRequest(
    RootModel[
        Annotated[
            ControlRequest
            | ProjectRequest
            | ReceiptRequest
            | EvidenceRequest
            | ExecutionRequest
            | FreshExecutionRequest
            | ExecutionCancelRequest
            | SubscriptionRequest,
            Field(discriminator="kind"),
        ]
    ]
):
    pass
