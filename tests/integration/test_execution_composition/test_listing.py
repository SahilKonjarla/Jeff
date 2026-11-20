"""Fresh listings discover executable authority, never promote legacy reviews."""

import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    AuthorityOperation,
    ExecutionCall,
    ExecutionListScope,
    ExecutionService,
    ExecutionSuccess,
    FreshExecutionCall,
    FreshExecutionPage,
    FreshExecutionView,
    ReviewFault,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_state import Ownership
from jeff.infrastructure.runtime_storage import SQLiteStorage

from . import support_composition as fixtures
from .support_composition import approved, composed, pending, show
from .test_historical_inspection import restarted_coordinator


def test_fresh_listing_discovers_pending_authority_without_execution(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        call = FreshExecutionCall.model_validate(
            dict(
                request_id=uuid4(),
                directory=project.root,
                operation="approvals.list",
            )
        )
        reply = coordinator.execute(call, time.monotonic() + 5)
        assert reply.ok and reply.page is not None
        assert [item.approval_id for item in reply.page.items] == [record.approval_id]
        assert reply.page.next_after is None and not docker.calls


def page(
    coordinator: ExecutionCoordinator, project: Project, after: str | None = None, limit: int = 2
) -> FreshExecutionPage:
    reply = coordinator.execute(
        FreshExecutionCall(
            request_id=uuid4(),
            directory=project.root,
            operation="jobs.list",
            after=after,
            limit=limit,
        ),
        time.monotonic() + 5,
    )
    assert reply.ok and reply.page is not None
    return reply.page


def test_fresh_listing_does_not_promote_legacy_review(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        legacy = ExecutionService(
            coordinator.projects, store, coordinator.profiles, coordinator.instance_id
        )
        reply = legacy.execute(
            ExecutionCall(
                request_id=uuid4(),
                directory=project.root,
                operation="propose",
                profile_name="python",
                argv=("/usr/local/bin/python", "-V"),
            ),
            time.monotonic() + 5,
        )
        assert isinstance(reply.result, ExecutionSuccess)
        record = reply.result.data.items[0]
        assert not record.execution_available and not record.launch_authority_issued
        assert not page(coordinator, project).items
        fresh = pending(coordinator, project)
        assert [r.approval_id for r in page(coordinator, project).items] == [fresh.approval_id]
        assert not docker.calls


def test_pages_cover_fresh_states_without_duplicates_or_new_claims(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        records = [pending(coordinator, project) for _ in range(5)]
        coordinator.decide(
            project.root,
            records[0].approval_id,
            records[0].manifest_digest,
            approve=False,
            deadline=time.monotonic() + 5,
        )
        coordinator.decide(
            project.root,
            records[1].approval_id,
            records[1].manifest_digest,
            approve=True,
            deadline=time.monotonic() + 5,
        )
        coordinator.decide(
            project.root,
            records[2].approval_id,
            records[2].manifest_digest,
            approve=True,
            deadline=time.monotonic() + 5,
        )
        store.authority(
            AuthorityOperation(
                operation="claim",
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=coordinator.instance_id,
                approval_id=records[2].approval_id,
                current_manifest_digest=records[2].manifest_digest,
            ),
            deadline=time.monotonic() + 5,
        )
        before = [show(store, project, record.approval_id) for record in records]
        listed: list[FreshExecutionView] = []
        cursor = None
        for _ in range(3):
            result = page(coordinator, project, cursor)
            listed.extend(result.items)
            cursor = result.next_after
        assert cursor is None and len(listed) == 5
        assert {r.approval_id for r in listed} == {r.approval_id for r in records}
        assert [(r.created_at_ms, str(r.proposal_id)) for r in listed] == sorted(
            [(r.created_at_ms, str(r.proposal_id)) for r in records], reverse=True
        )
        assert {r.state.value for r in listed} == {"pending", "approved", "denied", "consumed"}
        assert any(r.claim_state == "claimed" for r in listed)
        assert [show(store, project, record.approval_id) for record in records] == before
        assert not docker.calls
        payload = result.model_dump_json()
        assert all(
            '"' + name + '"' not in payload
            for name in (
                "owner_instance_id",
                "claim_id",
                "launch_claim",
                "container_id",
                "nonce",
                "stdout",
            )
        )


def test_listing_rejects_foreign_cursor_and_missing_scope(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        for _ in range(3):
            pending(coordinator, project)
        first = page(coordinator, project)
        assert first.next_after is not None
        other = Project(uuid4(), tmp_path / "other")
        other.root.mkdir(mode=0o700)
        FileProjectStore().initialize(other)
        store.index(other)
        assert not page(coordinator, other).items
        reply = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(),
                directory=other.root,
                operation="jobs.list",
                after=first.next_after,
            ),
            time.monotonic() + 5,
        )
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "execution_conflict" and not docker.calls
        with pytest.raises(ReviewFault) as refused:
            store.list_executions(
                ExecutionListScope(
                    project_id=project.project_id,
                    canonical_root=other.root,
                    owner_instance_id=coordinator.instance_id,
                ),
                deadline=time.monotonic() + 5,
            )
        assert refused.value.code == "execution_target_mismatch"
        with pytest.raises(ReviewFault) as foreign_reader:
            store.list_executions(
                ExecutionListScope(
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=uuid4(),
                ),
                deadline=time.monotonic() + 5,
            )
        assert foreign_reader.value.code == "execution_target_mismatch"


@pytest.mark.parametrize(
    "wall_delta,mono_delta,state",
    [
        (601000, 0, "expired"),
        (0, 601, "expired"),
        (-1, 0, "invalidated"),
    ],
)
def test_listing_projects_expiry_without_writing_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wall_delta: int,
    mono_delta: int,
    state: str,
) -> None:
    wall, mono = [1000000000], [100.0]

    def clocked(owner: Ownership, *, instance_id: UUID) -> SQLiteStorage:
        return SQLiteStorage(
            owner,
            instance_id=instance_id,
            review_clock=lambda: wall[0],
            review_monotonic=lambda: mono[0],
        )

    monkeypatch.setattr(fixtures, "SQLiteStorage", clocked)
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        database = store.owner.store.layout.data_dir / "state.sqlite3"
        before = database.stat().st_mtime_ns
        wall[0] += wall_delta
        mono[0] += mono_delta
        result = page(coordinator, project)
        assert result.items[0].state.value == state
        assert result.items[0].expires_at_ms == record.expires_at_ms
        assert database.stat().st_mtime_ns == before and not docker.calls


def test_replacement_reader_lists_completed_and_unknown_history(tmp_path: Path) -> None:
    with composed(tmp_path) as (original, previous, project, docker):
        completed = approved(original, project)
        original.run(
            project.root,
            completed.approval_id,
            completed.manifest_digest,
            deadline=time.monotonic() + 5,
        )
        claimed = approved(original, project)
        previous.authority(
            AuthorityOperation(
                operation="claim",
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=original.instance_id,
                approval_id=claimed.approval_id,
                current_manifest_digest=claimed.manifest_digest,
            ),
            deadline=time.monotonic() + 5,
        )
        with restarted_coordinator(original, previous, docker) as (current, store):
            current.listing = store
            result = page(current, project)
            assert {r.claim_state for r in result.items} == {"finished", "unknown"}
            assert {r.approval_id for r in result.items} == {
                completed.approval_id,
                claimed.approval_id,
            }
            assert docker.calls == ["create", "attach", "remove"]
