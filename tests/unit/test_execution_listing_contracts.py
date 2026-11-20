"""Pagination is a bounded read scope, never permission or a private record projection."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import FreshExecutionCall, FreshExecutionPage, FreshExecutionReply


@pytest.mark.parametrize(
    "fields",
    [
        {"limit": 0},
        {"limit": 33},
        {"limit": True},
        {"limit": "2"},
        {"after": "bad"},
        {"after": "-1:" + str(UUID(int=1))},
        {"after": "01:" + str(UUID(int=1))},
        {"after": "9223372036854775808:" + str(UUID(int=1))},
        {"after": "1:" + str(UUID(int=1)).replace("-", "")},
        {"approval_id": uuid4()},
        {"profile_name": "python"},
        {"argv": ("python",)},
        {"paths": ("source.py",)},
        {"reviewed_manifest_digest": "a" * 64},
    ],
)
def test_list_calls_reject_invalid_boundaries_and_action_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FreshExecutionCall.model_validate(
            dict(request_id=uuid4(), directory="/project", operation="jobs.list", **fields)
        )


@pytest.mark.parametrize("operation", ["propose", "show", "logs", "approve", "deny", "start"])
def test_action_calls_never_accept_pagination(operation: str) -> None:
    with pytest.raises(ValidationError):
        FreshExecutionCall.model_validate(
            dict(
                request_id=uuid4(),
                directory="/project",
                operation=operation,
                after=f"0:{uuid4()}",
                limit=2,
            )
        )


def test_list_replies_require_pages_and_pages_bind_root_and_boundary() -> None:
    fields = dict(request_id=uuid4(), identity=uuid4(), operation="jobs.list", ok=True)
    with pytest.raises(ValidationError):
        FreshExecutionReply.model_validate(fields)
    page = FreshExecutionPage(project_id=uuid4(), canonical_root=Path("/project"))
    reply = FreshExecutionReply.model_validate(dict(fields, page=page))
    assert FreshExecutionReply.model_validate_json(reply.model_dump_json()) == reply
    with pytest.raises(ValidationError):
        FreshExecutionPage.model_validate(dict(page.model_dump(), next_after=f"0:{uuid4()}"))
    with pytest.raises(ValidationError):
        FreshExecutionPage.model_validate(dict(page.model_dump(), owner_instance_id=uuid4()))
    with pytest.raises(ValidationError):
        FreshExecutionPage(project_id=uuid4(), canonical_root=Path("relative"))
