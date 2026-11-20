"""Real disposable-Git dispatcher integration; requires its existing approved Git fixture."""

import os
import time
from pathlib import Path
from uuid import uuid4

from jeff.application.dispatch import ProjectCall, ProjectDispatcher
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import QuestionService, QuestionSuccess
from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


def test_dispatcher_refuses_retired_uuid_as_known_nonacceptance_without_poisoning_storage(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    clock = [0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner, instance_id=identity.instance_id, retention_clock=lambda: clock[0]
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects,
                QuestionService(projects, GitRepositoryRead()),
                store,
                identity,
            )
            call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            first = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(first.result, QuestionSuccess)
            assert first.receipt is not None and first.receipt.finished_at_ms is not None
            clock[0] = first.receipt.finished_at_ms + 30 * 86400000 + 1
            assert store.retire(deadline=time.monotonic() + 5).retired_count == 1
            again = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(again.result, Failure)
            assert again.result.error.code == "request_retired"
            assert again.accepted is False and again.receipt is None
            assert store.snapshot().state == "ready"
            configured = dispatch.execute(
                ProjectCall(operation="config", directory=git_repo), time.monotonic() + 10
            )
            assert configured.result.ok
        finally:
            store.close()
