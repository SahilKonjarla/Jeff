"""Required guarded proposal→claim→existing controller→durable result seam, synthetic Docker."""

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Never, cast
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner

from jeff.application.execution import (
    DockerBackendFault,
    DockerExecutionResult,
    DockerOutputSink,
    DockerOwnedContainer,
    ExecutionManifest,
    ExecutionStopRecord,
    ExecutionStopSignal,
    FreshExecutionCall,
    FreshExecutionReply,
    FreshExecutionView,
    GuardedExecutionManifest,
    GuardV2Terminal,
)
from jeff.infrastructure.runtime_execution.arguments import create_arguments
from jeff.infrastructure.runtime_execution.guard_artifacts import InstalledGuardVerifier
from jeff.infrastructure.runtime_output import PytestOutputCollector
from jeff.interfaces.cli.execution_commands import register
from jeff.interfaces.cli.project_commands import register as register_project_commands
from jeff.interfaces.cli.review_commands import register as register_review_commands

from .support_composition import approved, composed, pending, show
from .support_guarded import policy


def test_settings4_reaches_existing_cold_bootstrap_without_starting_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import GuardedDockerBackendConfiguration
    from jeff.bootstrap.runtime import create_configured_execution

    def no_child(*args: object, **kwargs: object) -> None:
        raise AssertionError("Cold guarded bootstrap must not launch any child")

    monkeypatch.setattr("subprocess.Popen", no_child)
    with composed(tmp_path, configured=True, guarded=True) as (coordinator, store, _, docker):
        assert isinstance(store.execution_backend, GuardedDockerBackendConfiguration)
        result = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert isinstance(result, type(coordinator))
        assert result.require_guard and result.guards is not None
        assert not docker.calls and not docker.queries


def test_guarded_approval_runs_once_and_preserves_original_workload(tmp_path: Path) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert isinstance(record.manifest, GuardedExecutionManifest)
        assert record.manifest_digest != record.manifest.original.fingerprint
        view = FreshExecutionView.from_record(record)
        assert view.authority_kind == "guarded_execution_v1"
        assert FreshExecutionView.model_validate_json(view.model_dump_json()) == view
        result = coordinator.run(
            project.root, record.approval_id, record.manifest_digest, deadline=time.monotonic() + 5
        )
        assert result.disposition == "completed" and result.outcome.exit_code == 0
        assert result.output is not None and result.output.stdout == "public output\n"
        assert docker.calls.count("create") == docker.calls.count("attach_guarded") == 1
        assert "attach" not in docker.calls
        args = create_arguments(docker.intents[0])
        assert "--interactive" in args and "--init=false" in args and "--jfg2" in args
        assert record.manifest_digest in args
        assert result.owned.intent.record.manifest == record.manifest
        assert show(store, project, record.approval_id).manifest == record.manifest
        with pytest.raises(DockerBackendFault):
            coordinator.run(
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
        assert docker.calls.count("create") == 1


@pytest.mark.parametrize(("failing", "version"), [(False, 2), (True, 1), (True, 2)])
def test_pytest_cli_uses_existing_approval_and_reads_report_by_job_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: bool, version: int
) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, _, project, docker):
        coordinator.pytest_capture = lambda limit, token, paths: PytestOutputCollector(
            limit, run_token=token, approved_paths=paths
        )

        class Client:
            def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
                return coordinator.execute(call, deadline=time.monotonic() + 5)

        monkeypatch.chdir(project.root)
        app = typer.Typer()
        ask = register(app, Client, None)

        def no_git_client() -> Never:
            raise AssertionError("Test summaries must not dispatch to Git.")

        register_project_commands(app, no_git_client, ask)
        cli = CliRunner()
        proposal = cli.invoke(
            app, ["tests", "run", "--profile", "python", "--input", "source.py", "--json"]
        )
        assert proposal.exit_code == 0, (proposal.output, proposal.exception)
        reply = FreshExecutionReply.model_validate_json(proposal.stdout)
        assert reply.view is not None
        view = reply.view
        assert "create" not in docker.calls
        not_run = cli.invoke(app, ["tests", "retry-failed", str(view.job_id), "--json"])
        assert not_run.exit_code == 1 and "execution_retry_unavailable" in not_run.stdout
        coordinator.decide(
            project.root,
            view.approval_id,
            view.manifest_digest,
            approve=True,
            deadline=time.monotonic() + 5,
        )
        docker.exit_code = int(failing)
        docker.output = json.dumps(
            {
                "version": version,
                "run_token": view.manifest.argv[-1],
                "passed": 2 - int(failing),
                "failed": int(failing),
                "exit_status": docker.exit_code,
                "failures": [
                    {
                        "node_id": "source.py::test_case",
                        "path": "source.py",
                        "line": 3,
                        "excerpt": "assert False",
                    }
                ]
                if failing
                else [],
            }
        ).encode()
        coordinator.run(
            project.root, view.approval_id, view.manifest_digest, deadline=time.monotonic() + 5
        )
        results = cli.invoke(app, ["tests", "results", str(view.job_id), "--json"])
        assert results.exit_code == 0, (results.output, results.exception)
        outcome = FreshExecutionReply.model_validate_json(results.stdout)
        assert outcome.pytest_result is not None and outcome.pytest_result.report is not None
        report = outcome.pytest_result.report
        assert (report.passed, report.failed, report.exit_status) == (
            2 - int(failing),
            int(failing),
            int(failing),
        )
        if failing:
            assert report.failures[0].node_id == "source.py::test_case"
            assert (report.failures[0].line, report.failures[0].excerpt) == (3, "assert False")
        assert outcome.output is None
        wrong_id = cli.invoke(app, ["tests", "results", str(view.approval_id), "--json"])
        assert wrong_id.exit_code == 1
        logs = cli.invoke(app, ["execution", "logs", str(view.approval_id), "--json"])
        assert logs.exit_code == 0
        archive = FreshExecutionReply.model_validate_json(logs.stdout)
        assert archive.pytest_result is None
        assert archive.output is not None and archive.output.stdout == ""
        for kind, expected in (("pytest", 1), ("command", 0)):
            listed = cli.invoke(
                app, ["execution", "jobs", "list", "--state", "finished", "--kind", kind, "--json"]
            )
            assert listed.exit_code == 0, (listed.output, listed.exception)
            page = FreshExecutionReply.model_validate_json(listed.stdout).page
            assert page is not None and len(page.items) == expected
        assert docker.calls.count("remove") == 1
        summary = cli.invoke(app, ["ask", "What failed?"])
        assert summary.exit_code == 0 and f"Passed: {report.passed}" in summary.stdout
        retry = cli.invoke(app, ["tests", "retry-failed", str(view.job_id), "--json"])
        if not failing or version == 1:
            assert retry.exit_code == 1 and "execution_retry_unavailable" in retry.stdout
            return
        assert retry.exit_code == 0, (retry.stdout, retry.exception)
        proposed = FreshExecutionReply.model_validate_json(retry.stdout).view
        assert proposed is not None and proposed.state.value == "pending"
        assert (proposed.approval_id, proposed.job_id) != (view.approval_id, view.job_id)
        assert proposed.manifest.argv[5:] == ("source.py::test_case",)
        assert proposed.manifest.profile == view.manifest.profile
        assert proposed.manifest.inputs.files == view.manifest.inputs.files
        latest = cli.invoke(app, ["ask", "what failed?"])
        assert latest.exit_code == 0 and str(proposed.job_id) in latest.stdout
        assert "Pytest report unavailable (not_claimed)." in latest.stdout
        with pytest.raises(DockerBackendFault):
            coordinator.run(
                project.root,
                proposed.approval_id,
                proposed.manifest_digest,
                deadline=time.monotonic() + 5,
            )
        assert docker.calls.count("create") == 1
        (project.root / "source.py").write_text("source changed\n")
        changed = cli.invoke(app, ["tests", "retry-failed", str(view.job_id), "--json"])
        assert changed.exit_code == 1 and "execution_conflict" in changed.stdout
        assert docker.calls.count("create") == 1


def test_job_logs_follow_reads_live_and_final_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.application.execution.log_tail import LiveLogTail
    from jeff.application.execution.ports import DockerOutputCapture
    from jeff.infrastructure.runtime_output import GuardedOutputCollector

    from .support_guarded import GuardedDocker

    with composed(tmp_path, guarded=True) as (coordinator, _, project, docker):

        def attach_tail(capture: DockerOutputCapture, tail: LiveLogTail) -> None:
            assert isinstance(capture, GuardedOutputCollector)
            capture.follow_into(tail)

        coordinator.attach_log_tail = attach_tail
        first, release = threading.Event(), threading.Event()
        assert isinstance(docker, GuardedDocker)
        original = docker.attach_guarded

        def attach(
            handle: DockerOwnedContainer,
            sink: DockerOutputSink,
            stop: ExecutionStopSignal | None,
            observe_running: Callable[[], None] | None,
            *,
            deadline: float,
        ) -> tuple[int, GuardV2Terminal]:
            sink.accept("stdout", b"before release\n")
            first.set()
            assert release.wait(5)
            return original(handle, sink, stop, observe_running, deadline=deadline)

        monkeypatch.setattr(docker, "attach_guarded", attach)
        record = approved(coordinator, project)

        class Client:
            def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
                return coordinator.execute(call, deadline=time.monotonic() + 5)

        monkeypatch.chdir(project.root)
        app = typer.Typer()
        register(app, Client, None, jobs=register_review_commands(app, None))
        cli = CliRunner()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(
                coordinator.start,
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=uuid4(),
                admission_deadline=time.monotonic() + 5,
                on_admitted=lambda _: None,
            )
            try:
                assert first.wait(5)
                preview = cli.invoke(app, ["jobs", "logs", str(record.job_id)])
                assert preview.exit_code == 0 and preview.stdout == "before release\n"
                assert not running.done()
            finally:
                release.set()
            assert isinstance(running.result(timeout=5), DockerExecutionResult)
        done = cli.invoke(app, ["jobs", "logs", str(record.job_id), "--follow"])
        assert done.exit_code == 0 and done.stdout == "before release\npublic output\n"
        assert docker.calls.count("remove") == 1


def test_workload124_is_not_timeout(tmp_path: Path) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, _, project, docker):
        docker.exit_code = 124
        record = approved(coordinator, project)
        result = coordinator.run(
            project.root, record.approval_id, record.manifest_digest, deadline=time.monotonic() + 5
        )
        assert result.disposition == "completed"
        assert result.outcome.exit_code == 124 and result.outcome.timed_out is False


def test_valid_guard_deadline_uses_existing_durable_timeout_path(tmp_path: Path) -> None:
    with composed(tmp_path, guarded=True, cancellable=True) as (coordinator, _, project, docker):
        # Disable only the fake's interactive cancellation wait for this typed deadline case.
        docker.cancel_mode = False
        docker.failure = "deadline"
        record = approved(coordinator, project)
        result = coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=uuid4(),
            admission_deadline=time.monotonic() + 5,
            on_admitted=lambda _: None,
        )
        assert isinstance(result, ExecutionStopRecord)
        assert result.terminal is not None and result.terminal.outcome == "timed_out"
        assert result.terminal.cleanup_verified and result.terminal.exit_code == 124
        assert "stop" not in docker.calls and "kill" not in docker.calls


@pytest.mark.parametrize("failure", ["protocol", "clock_fault"])
def test_uncertain_guard_never_stops_removes_retries_or_calls_it_timeout(
    tmp_path: Path,
    failure: str,
) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, store, project, docker):
        docker.failure = failure
        record = approved(coordinator, project)
        result = coordinator.run(
            project.root, record.approval_id, record.manifest_digest, deadline=time.monotonic() + 5
        )
        assert isinstance(result, DockerExecutionResult) and result.disposition == "quarantined"
        assert result.outcome.outcome_unknown and not result.outcome.timed_out
        assert not result.outcome.cleanup_verified
        assert not {"stop", "kill", "remove", "release_guarded"}.intersection(docker.calls)
        assert show(store, project, record.approval_id).claim_state == "unknown"


@pytest.mark.parametrize("unknown_stop", [False, True])
def test_guarded_active_cancel_keeps_cleanup_with_original_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_stop: bool,
) -> None:
    from jeff.application.execution import ExecutionCancelCall, ExecutionCancelReply

    with composed(tmp_path, guarded=True, cancellable=True) as (coordinator, _, project, docker):
        record = approved(coordinator, project)
        docker.entered = threading.Event()
        if unknown_stop:

            def lost_stop(endpoint: str, container_id: str, *, deadline: float) -> None:
                docker.calls.append("stop")
                raise DockerBackendFault("docker_mutation_unconfirmed")

            monkeypatch.setattr(docker, "stop", lost_stop)
        with ThreadPoolExecutor(max_workers=1) as worker:
            work = worker.submit(
                coordinator.start,
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=uuid4(),
                admission_deadline=time.monotonic() + 5,
                on_admitted=lambda _: None,
            )
            assert docker.entered.wait(2)

            class Client:
                def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
                    return coordinator.execute(call, deadline=time.monotonic() + 5)

                def cancel(self, call: ExecutionCancelCall) -> ExecutionCancelReply:
                    from jeff.application.execution import ExecutionStopView

                    recorded = coordinator.submit_cancel(
                        call, deadline=time.monotonic() + 2
                    ).result(timeout=3)
                    return ExecutionCancelReply(
                        request_id=call.request_id,
                        approval_id=call.approval_id,
                        reviewed_manifest_digest=call.reviewed_manifest_digest,
                        ok=True,
                        identity=coordinator.instance_id,
                        stop=ExecutionStopView.from_record(recorded),
                    )

            monkeypatch.chdir(project.root)
            app = typer.Typer()
            register(app, Client, Client, jobs=register_review_commands(app, None))
            args = ["jobs", "cancel", str(record.job_id), "--digest"]
            bad = CliRunner().invoke(app, [*args, "0" * 64])
            assert bad.exit_code == 1 and not work.done()
            response = CliRunner().invoke(app, [*args, record.manifest_digest, "--json"])
            assert response.exit_code == 0, (response.output, response.exception)
            result = work.result(timeout=5)
        assert isinstance(result, ExecutionStopRecord) and result.terminal is not None
        if unknown_stop:
            assert result.terminal.outcome == "unknown" and not result.terminal.cleanup_verified
            assert not {"kill", "remove", "release_guarded"}.intersection(docker.calls)
        else:
            assert result.terminal.outcome == "cancelled" and result.terminal.cleanup_verified
            assert docker.calls.index("stop") < docker.calls.index("remove")
            assert docker.calls.index("remove") < docker.calls.index("release_guarded")


def test_backend_without_guard_transport_refuses_before_create(tmp_path: Path) -> None:
    from jeff.infrastructure.runtime_execution import DockerJobController

    from .support_docker import Docker

    with composed(tmp_path, guarded=True) as (coordinator, _, project, _):
        record = approved(coordinator, project)
        unsupported = Docker()
        cast(DockerJobController, coordinator.controller).backend = unsupported
        with pytest.raises(DockerBackendFault):
            coordinator.run(
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
        assert unsupported.calls == []


def test_guard_policy_change_invalidates_approval_before_claim(tmp_path: Path) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        coordinator.guards = InstalledGuardVerifier(
            policy().model_copy(update={"deadline_margin_ns": 200000000})
        )
        with pytest.raises(DockerBackendFault):
            coordinator.decide(
                project.root,
                record.approval_id,
                record.manifest_digest,
                approve=True,
                deadline=time.monotonic() + 5,
            )
        assert show(store, project, record.approval_id).claim_id is None
        assert docker.calls == []


def test_old_facts_stay_readable_but_are_never_upgraded_to_guarded_launch(tmp_path: Path) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        record = pending(coordinator, project)
        assert isinstance(record.manifest, ExecutionManifest)
        original = record.model_dump_json()
        coordinator.guards, coordinator.require_guard = InstalledGuardVerifier(policy()), True
        with pytest.raises(DockerBackendFault):
            coordinator.decide(
                project.root,
                record.approval_id,
                record.manifest_digest,
                approve=True,
                deadline=time.monotonic() + 5,
            )
        assert show(store, project, record.approval_id).model_dump_json() == original
        assert docker.calls == []
