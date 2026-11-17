"""Fresh internal authority through real SQLite; no Docker or input-verification claims."""

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    DockerBackendConfiguration,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerCompletion,
    DockerJobOutcome,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchScope,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionCall,
    ExecutionInspectionScope,
    ExecutionManifest,
    ExecutionProfile,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    ExecutionStopTerminal,
    PreparedInputBinding,
    PreparedInputs,
    ReviewFault,
    ReviewOperation,
    VerifiedDockerTarget,
)
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore, _RecoveryPermit
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_settings3_backend_cache_requires_ready_and_stopped_doctor_stays_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.infrastructure import docker_cli

    def no_backend(*args: object, **kwargs: object) -> None:
        raise AssertionError("Settings loading must not construct a Docker adapter")

    monkeypatch.setattr(docker_cli.DockerTargetInspector, "__init__", no_backend)
    monkeypatch.setattr(docker_cli.DockerLifecycleCLI, "__init__", no_backend)
    for kind in ("configured", "disabled", "failed_database", "legacy"):
        root = tmp_path / kind[0]  # Keep synthetic native socket paths below the existing bound.
        layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
        owner = RuntimeStateStore(layout).acquire(create=True)
        assert owner is not None
        profile = ExecutionProfile(
            project_id=uuid4(),
            canonical_root=root / "project",
            name="python",
            engine_id="synthetic-no-query",
            endpoint="unix:///synthetic/nonexistent.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="synthetic",
            executable="/usr/local/bin/python",
        )
        backend = DockerBackendConfiguration(
            binary=DockerCLIIdentity(path=Path("/synthetic/missing-cli"), sha256="a" * 64, size=1),
            config=Path("/synthetic/missing-config"),
            declarations=(DockerTargetDeclaration(profile=profile),),
        )
        value = {
            "schema_version": 3,
            "history": {"retention_days": 30},
            "execution": {"profiles": [profile.model_dump(mode="json")]},
            "backend": None if kind == "disabled" else backend.model_dump(mode="json"),
        }
        if kind == "legacy":
            value.pop("backend")
            value["schema_version"] = 2
        payload = json.dumps(value).encode()
        with owner:
            owner.store.credential(owner)
            load_or_create_settings(owner)
            layout.settings.write_bytes(payload)
            if kind == "failed_database":
                layout.database.write_bytes(b"SYNTHETIC_NON_DATABASE")
                layout.database.chmod(0o600)
            storage = SQLiteStorage(owner, instance_id=uuid4())
            assert storage.execution_backend is None
            storage.start()
            try:
                state = storage.wait_ready(5)
                if kind == "failed_database":
                    assert state.state == "failed" and storage.execution_backend is None
                else:
                    assert state.state == "ready"
                    expected = backend if kind == "configured" else None
                    assert storage.execution_backend == expected
                    # No hot reload or backend construction: paths are nonexistent.
                    if kind != "legacy":
                        layout.settings.write_bytes(json.dumps({**value, "backend": None}).encode())
                    assert storage.execution_backend == expected
                    layout.settings.write_bytes(payload)
                storage.request_close()
                assert storage.execution_backend is None
            finally:
                storage.close(timeout=5)
            assert storage.closed and storage.execution_backend is None
            assert layout.settings.read_bytes() == payload
        if kind != "failed_database":
            before = layout.settings.stat()

            def selected(current: RuntimeLayout = layout) -> RuntimeLayout:
                return current

            result = RuntimeDiagnosticsAdapter(selected).inspect()
            assert (
                result.ok
                and result.data.summary == "stopped"
                and result.data.settings_schema_version == (2 if kind == "legacy" else 3)
            )
            assert "synthetic/missing" not in result.model_dump_json()
            after = layout.settings.stat()
            assert (before.st_ino, before.st_mtime_ns, before.st_size) == (
                after.st_ino,
                after.st_mtime_ns,
                after.st_size,
            )
            assert layout.settings.read_bytes() == payload


@contextmanager
def authority_store(
    root: Path,
    clock: dict[str, int] | None = None,
    *,
    retention_clock: Callable[[], int] | None = None,
    retention_monotonic: Callable[[], float] | None = None,
) -> Iterator[tuple[SQLiteStorage, Project, ExecutionManifest, UUID]]:
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project, generation = Project(uuid4(), root / "project"), uuid4()
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-no-engine-query",
        endpoint="unix:///synthetic/nonexistent.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    manifest = ExecutionManifest(
        profile=profile, argv=(profile.executable, "-V"), inputs=PreparedInputs()
    )
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(
            owner,
            instance_id=generation,
            retention_clock=retention_clock,
            retention_monotonic=retention_monotonic,
            review_clock=(lambda: clock["wall"]) if clock is not None else None,
            review_monotonic=(lambda: float(clock["mono"])) if clock is not None else None,
        )
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            yield storage, project, manifest, generation
        finally:
            storage.close(timeout=5)


def command(project: Project, generation: UUID, **values: object) -> AuthorityOperation:
    return AuthorityOperation.model_validate(
        {
            "project_id": project.project_id,
            "canonical_root": project.root,
            "owner_instance_id": generation,
            **values,
        }
    )


def launch_intent(
    store: SQLiteStorage, project: Project, manifest: ExecutionManifest, generation: UUID
) -> DockerLaunchIntent:
    record = store.authority(
        command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    ).record
    store.authority(
        command(
            project,
            generation,
            operation="approve",
            approval_id=record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    )
    claimed = store.authority(
        command(
            project,
            generation,
            operation="claim",
            approval_id=record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    ).record
    profile = manifest.profile
    target = VerifiedDockerTarget(
        declaration_digest="a" * 64,
        profile_digest=profile.fingerprint,
        binary=DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="b" * 64, size=1),
        endpoint=profile.endpoint,
        engine_id=profile.engine_id,
        image_id=profile.image_id,
        platform=profile.platform,
        client_version="synthetic",
        server_version="synthetic",
        image_configuration_digest="c" * 64,
        effective_environment=(),
    )
    return DockerLaunchIntent(
        attempt_id=uuid4(),
        record=claimed,
        target=target,
        inputs=PreparedInputBinding(
            snapshot_id=manifest.inputs.snapshot_id,
            inventory_digest=manifest.inputs.digest,
            path=project.root.parent / "prepared",
        ),
        nonce="d" * 32,
    )


def launch_scope(intent: DockerLaunchIntent) -> DockerLaunchScope:
    assert intent.record.claim_id is not None
    return DockerLaunchScope(
        project_id=intent.record.project_id,
        canonical_root=intent.record.canonical_root,
        claim_id=intent.record.claim_id,
    )


def fact(intent: DockerLaunchIntent, phase: str) -> DockerLaunchEvent:
    return DockerLaunchEvent.model_validate(
        {
            "intent": intent,
            "phase": phase,
            **(
                {"container_id": "e" * 64, "created_at": "2026-01-01T00:00:00.000000000Z"}
                if phase not in {"create_intent", "unknown"}
                else {}
            ),
            **({"exit_code": 7} if phase == "exited" else {}),
        }
    )


def completion(
    intent: DockerLaunchIntent,
    text: bytes = b"public output\n",
    *,
    unknown: bool = False,
    exit_code: int = 7,
) -> DockerCompletion:
    capture = GuardedOutputCollector(intent.record.manifest.profile.output_bytes)
    for offset in range(0, len(text), 16384):
        capture.accept("stdout", text[offset : offset + 16384])
    capture.finish("stdout")
    capture.finish("stderr")
    return DockerCompletion(
        owned=DockerOwnedContainer(
            intent=intent, container_id="e" * 64, created_at="2026-01-01T00:00:00.000000000Z"
        ),
        outcome=DockerJobOutcome(
            exit_code=exit_code, cleanup_verified=True, outcome_unknown=unknown
        ),
        output=capture.seal(),
    )


def stopped_sample(
    store: SQLiteStorage,
    project: Project,
    manifest: ExecutionManifest,
    generation: UUID,
    *,
    retain: bool = True,
) -> tuple[ExecutionStopRecord, ExecutionStopCapture]:
    from jeff.domain.execution import fingerprint

    intent = launch_intent(store, project, manifest, generation)
    for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
        store.record(fact(intent, phase), deadline=time.monotonic() + 5)
    scope = ExecutionInspectionScope(
        project_id=project.project_id,
        canonical_root=project.root,
        owner_instance_id=generation,
        approval_id=intent.record.approval_id,
    )
    request = ExecutionStopRequest(
        **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
    )
    record = store.request_stop(request, deadline=time.monotonic() + 5)
    capture = GuardedOutputCollector(manifest.profile.output_bytes)
    capture.accept("stdout", b"safe prefix\n")
    digest_helper = completion(intent).model_copy(update={"output": capture.seal(abort=True)})
    owned = digest_helper.owned
    observation = ExecutionStopObservation(
        scope=scope,
        request_id=record.request_id,
        manifest_digest=manifest.fingerprint,
        owned=owned,
        terminal=ExecutionStopTerminal(
            outcome="cancelled",
            evidence="owned",
            cleanup_verified=True,
            exit_code=7,
            attempt_id=intent.attempt_id,
            ownership_digest=fingerprint(
                {
                    "intent": intent.fingerprint,
                    "container": owned.container_id,
                    "birth": owned.created_at,
                }
            ),
            output_facts=digest_helper.output_facts,
            output_digest=digest_helper.output_digest,
        ),
    )
    sample = ExecutionStopCapture(observation=observation, output=digest_helper.output)
    final = (
        store.finish_stop_with_capture(observation, sample.output, deadline=time.monotonic() + 5)
        if retain
        else (store.finish_stop(observation, deadline=time.monotonic() + 5))
    )
    return final, sample


def test_stop_and_legacy_payloads_share_sixteen_known_slots_without_backfilling_metadata(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        first = None
        for index in range(17):
            if index % 2:
                stopped_sample(store, project, manifest, generation)
            else:
                intent = launch_intent(store, project, manifest, generation)
                for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                    store.record(fact(intent, phase), deadline=time.monotonic() + 5)
                store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
                if first is None:
                    first = intent
        assert first is not None
        oldest = store.execution_result(launch_scope(first), deadline=time.monotonic() + 5)
        assert oldest.payload_state == "retired" and oldest.retirement_reason == "capacity"
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql:
            assert sql.execute(
                "SELECT (SELECT count(*) FROM execution_results WHERE stdout IS NOT NULL) + "
                "(SELECT count(*) FROM execution_stop_payloads WHERE stdout IS NOT NULL)"
            ).fetchone() == (16,)
        metadata_only, sample = stopped_sample(store, project, manifest, generation, retain=False)
        inspection = store.inspect_execution(metadata_only.scope, deadline=time.monotonic() + 5)
        assert inspection.stop is not None and inspection.stop.payload_state == "missing"
        with pytest.raises(ReviewFault):
            store.finish_stop_with_capture(
                sample.observation, sample.output, deadline=time.monotonic() + 5
            )
        assert (
            store.inspect_stop(metadata_only.scope, deadline=time.monotonic() + 5) == metadata_only
        )


@pytest.mark.parametrize("kind", ("age", "clock", "tamper"))
def test_stop_archive_age_clock_and_tampering_never_expose_unavailable_text(
    tmp_path: Path, kind: str
) -> None:
    clock = {"wall": 100000000, "mono": 1000.0}
    with authority_store(
        tmp_path,
        retention_clock=lambda: int(clock["wall"]),
        retention_monotonic=lambda: clock["mono"],
    ) as (store, project, manifest, generation):
        record, sample = stopped_sample(store, project, manifest, generation)
        original = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
        assert original.stop_output == sample.output and original.stop is not None
        if kind == "age":
            clock["wall"] += 31 * 86400000
            clock["mono"] += 31 * 86400
        elif kind == "clock":
            clock["wall"] -= 1
        else:
            store.close(timeout=5)
            with closing(sqlite3.connect(store.owner.store.layout.database)) as sql, sql:
                sql.execute("UPDATE execution_stop_payloads SET stdout='evil prefix' || char(10)")
            reopened = SQLiteStorage(store.owner, instance_id=generation)
            reopened.start()
            try:
                assert reopened.wait_ready(5).state == "ready"
                with pytest.raises(ReviewFault) as altered:
                    reopened.inspect_execution(record.scope, deadline=time.monotonic() + 5)
                assert altered.value.code == "storage_schema_unsupported"
            finally:
                reopened.close(timeout=5)
            return
        inspected = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
        assert inspected.stop is not None
        assert inspected.stop.payload_state == ("expired" if kind == "age" else "clock_unconfirmed")
        assert (
            inspected.stop_output is None
            and inspected.stop.output_facts == original.stop.output_facts
        )
        if kind == "age":
            store.retire(deadline=time.monotonic() + 5)
            retired = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
            assert retired.stop is not None and retired.stop.payload_state == "retired"
            assert retired.stop.retirement_reason == "age" and retired.stop_output is None


def test_completion_atomically_records_sanitized_output_and_releases_admission(
    tmp_path: Path,
) -> None:
    for index, (code, text, limit) in enumerate(
        (
            (7, b"public output\n", 1048576),
            (0, b"password=SYNTHETIC_VALUE\n", 1048576),
            (7, b"safe\n" * 100, 64),
        )
    ):
        with authority_store(tmp_path / str(index)) as (store, project, manifest, generation):
            manifest = manifest.model_copy(
                update={"profile": manifest.profile.model_copy(update={"output_bytes": limit})}
            )
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                event = fact(intent, phase)
                if phase == "exited":
                    event = event.model_copy(update={"exit_code": code})
                store.record(event, deadline=time.monotonic() + 5)
            result = store.complete_execution(
                completion(intent, text, exit_code=code), deadline=time.monotonic() + 5
            )
            assert result.disposition == "completed" and result.payload_state == "retained"
            assert result.output is not None and result.outcome.exit_code == code
            if index == 0:
                assert result.output.stdout == "public output\n"
            if index == 1:
                assert (
                    result.output.guard_reason == "sensitive_channel"
                    and "SYNTHETIC_VALUE" not in result.output.stdout
                )
            if index == 2:
                assert result.output.truncated and result.output.retained_bytes <= 64
            assert (
                store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5).claim_state
                == "finished"
            )
            assert (
                store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                == result
            )
            assert (
                launch_intent(store, project, manifest, generation).record.claim_id
                != intent.record.claim_id
            )


def test_completion_refuses_binding_missing_facts_and_unknown_cannot_finish(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        request = completion(intent)
        with pytest.raises(DockerBackendFault):
            store.complete_execution(request, deadline=time.monotonic() + 5)
        for phase in ("exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        for bad in (
            request.model_copy(
                update={
                    "owned": request.owned.model_copy(
                        update={
                            "intent": intent.model_copy(
                                update={
                                    "record": intent.record.model_copy(
                                        update={"owner_instance_id": uuid4()}
                                    )
                                }
                            )
                        }
                    )
                }
            ),
            request.model_copy(
                update={
                    "owned": request.owned.model_copy(
                        update={"created_at": "2026-01-02T00:00:00.000000000Z"}
                    )
                }
            ),
            request.model_copy(
                update={
                    "owned": request.owned.model_copy(
                        update={"intent": intent.model_copy(update={"nonce": "f" * 32})}
                    )
                }
            ),
            request.model_copy(
                update={"output": request.output.model_copy(update={"retention_limit": 1024})}
            ),
            request.model_copy(
                update={
                    "output": request.output.model_copy(
                        update={"stdout": "\x1b", "retained_bytes": 1}
                    )
                }
            ),
        ):
            with pytest.raises(DockerBackendFault):
                store.complete_execution(bad, deadline=time.monotonic() + 5)
        assert (
            store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5).claim_state
            == "claimed"
        )
        store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            store.complete_execution(request, deadline=time.monotonic() + 5)
        quarantined = store.complete_execution(
            completion(intent, unknown=True), deadline=time.monotonic() + 5
        )
        assert quarantined.disposition == "quarantined" and quarantined.output is not None
        assert (
            store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5).claim_state
            == "unknown"
        )
        with pytest.raises(ReviewFault):
            launch_intent(store, project, manifest, generation)


def test_completion_rollback_and_lost_ack_leave_only_facts_no_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for mode in ("rollback", "lost_ack"):
        with authority_store(tmp_path / mode) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            request = completion(intent)
            if mode == "rollback":

                def refused_commit(connection: sqlite3.Connection, fd: int) -> None:
                    raise StorageFault("storage_busy")

                with monkeypatch.context() as patch:
                    patch.setattr(store, "_commit_write", refused_commit)
                    with pytest.raises(DockerBackendFault):
                        store.complete_execution(request, deadline=time.monotonic() + 5)
                assert (
                    store.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    ).claim_state
                    == "claimed"
                )
                with pytest.raises(DockerBackendFault):
                    store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
            else:

                def lose_reply(value: DockerCompletion) -> None:
                    store.complete_execution(value, deadline=time.monotonic() + 5)
                    raise DockerBackendFault("docker_journal_unconfirmed")

                with pytest.raises(DockerBackendFault):
                    lose_reply(request)
                recorded = store.execution_result(
                    launch_scope(intent), deadline=time.monotonic() + 5
                )
                assert recorded.disposition == "completed" and recorded.output is not None
                for retry in (request, completion(intent, b"conflicting text\n")):
                    with pytest.raises(DockerBackendFault):
                        store.complete_execution(retry, deadline=time.monotonic() + 5)
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
                assert (
                    store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                    == recorded
                )
                assert (
                    store.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    ).claim_state
                    == "finished"
                )


def test_output_retention_caps_text_then_expires_quarantine_without_releasing_it(
    tmp_path: Path,
) -> None:
    clock = [10000000000]
    with authority_store(tmp_path, retention_clock=lambda: clock[0]) as (
        store,
        project,
        manifest,
        generation,
    ):
        intents = []
        for index in range(18):
            intent = launch_intent(store, project, manifest, generation)
            intents.append(intent)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            text = b"safe\n" * 10000 if index == 1 else b"public\n"
            result = store.complete_execution(
                completion(intent, text, unknown=index == 17), deadline=time.monotonic() + 5
            )
            assert result.disposition == ("quarantined" if index == 17 else "completed")
        first = store.execution_result(launch_scope(intents[0]), deadline=time.monotonic() + 5)
        assert (
            first.payload_state == "retired"
            and first.retirement_reason == "capacity"
            and first.output is None
        )
        retained = store.execution_result(launch_scope(intents[1]), deadline=time.monotonic() + 5)
        assert retained.output is not None and retained.output.retained_bytes == 50000
        clock[0] -= 1
        assert (
            store.execution_result(
                launch_scope(intents[1]), deadline=time.monotonic() + 5
            ).payload_state
            == "clock_unconfirmed"
        )
        with pytest.raises(StorageFault):
            store.retire(deadline=time.monotonic() + 5)
        clock[0] += 1 + 31 * 86400000
        expired = store.execution_result(launch_scope(intents[-1]), deadline=time.monotonic() + 5)
        assert expired.payload_state == "expired" and expired.output is None
        batch = store.retire(deadline=time.monotonic() + 5)
        assert batch.has_more
        states = [
            store.execution_result(launch_scope(item), deadline=time.monotonic() + 5).payload_state
            for item in intents[1:]
        ]
        assert states.count("retired") == 4 and states.count("expired") == 13
        for _ in range(4):
            store.retire(deadline=time.monotonic() + 5)
        for intent in intents[1:]:
            final = store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
            assert (
                final.payload_state == "retired"
                and final.retirement_reason == "age"
                and final.output is None
            )
            assert final.output_facts.retained_bytes > 0 and final.output_digest
        assert (
            store.launch_status(
                launch_scope(intents[-1]), deadline=time.monotonic() + 5
            ).claim_state
            == "unknown"
        )
        with pytest.raises(ReviewFault):
            launch_intent(store, project, manifest, generation)


def test_results_restart_and_signed_schema7_migration_preserve_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for migrate in (False, True):
        with authority_store(tmp_path / str(migrate)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            unknown = None
            if not migrate:
                store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
                unknown = launch_intent(store, project, manifest, generation)
                for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                    store.record(fact(unknown, phase), deadline=time.monotonic() + 5)
                store.complete_execution(
                    completion(unknown, unknown=True), deadline=time.monotonic() + 5
                )
            store.close(timeout=5)
            settings = store.owner.store.layout.settings.read_bytes()
            if migrate:
                with (
                    closing(sqlite3.connect(store.owner.store.layout.database)) as fixture,
                    fixture,
                ):
                    fixture.execute("DROP TABLE execution_stop_payloads")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                    fixture.execute("DROP TABLE execution_lifecycles")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                    fixture.execute("DROP TABLE execution_results")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=8")
                    fixture.execute("PRAGMA user_version=7")
            signed: list[int] = []
            original_arm = store.owner.arm_storage_recovery

            def record_arm(
                database_fd: int,
                journal_fd: int,
                *,
                page_size: int,
                original_pages: int,
                schema_version: int = 4,
                _arm: Callable[..., _RecoveryPermit] = original_arm,
                _signed: list[int] = signed,
            ) -> object:
                permit = _arm(
                    database_fd,
                    journal_fd,
                    page_size=page_size,
                    original_pages=original_pages,
                    schema_version=schema_version,
                )
                actual = permit.schema_version
                assert actual == schema_version
                _signed.append(actual)
                return permit

            with monkeypatch.context() as patch:
                patch.setattr(store.owner, "arm_storage_recovery", record_arm)
                again = SQLiteStorage(store.owner, instance_id=uuid4())
                again.start()
                try:
                    assert again.wait_ready(5).schema_version == 10
                    assert (
                        again.lookup(project) == project
                        and store.owner.store.layout.settings.read_bytes() == settings
                    )
                    observed = again.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    )
                    assert observed.claim_state == ("unknown" if migrate else "finished")
                    if migrate:
                        assert signed[0] == 7
                        with pytest.raises(DockerBackendFault):
                            again.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            )
                    else:
                        assert (
                            again.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            ).output
                            is not None
                        )
                        assert unknown is not None
                        assert (
                            again.execution_result(
                                launch_scope(unknown), deadline=time.monotonic() + 5
                            ).disposition
                            == "quarantined"
                        )
                    with pytest.raises(DockerBackendFault):
                        again.complete_execution(completion(intent), deadline=time.monotonic() + 5)
                finally:
                    again.close(timeout=5)


def test_journal_preserves_normal_facts_without_premature_admission_release(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        observed = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert observed.phases == ("create_intent", "created", "start_intent", "exited", "cleaned")
        assert observed.container_id == "e" * 64 and observed.exit_code == 7
        assert not observed.uncertain and observed.claim_state == "claimed"
        # These are persisted observations, not a live handle or a completion/output receipt.
        assert not hasattr(observed, "launch_claim")


def test_fresh_approval_is_consumed_once_and_claim_capability_is_never_replayed(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposal = command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        )
        pending = store.authority(proposal, deadline=time.monotonic() + 5)
        assert pending.record.state == "pending" and pending.launch_claim is None
        assert store.authority(proposal, deadline=time.monotonic() + 5) == pending
        decision = command(
            project,
            generation,
            operation="approve",
            approval_id=pending.record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        )
        approved = store.authority(decision, deadline=time.monotonic() + 5)
        assert approved.record.state == "approved" and approved.launch_claim is None
        claim = command(
            project,
            generation,
            operation="claim",
            approval_id=pending.record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        )
        first = store.authority(claim, deadline=time.monotonic() + 5)
        assert first.record.state == "consumed" and first.launch_claim == first.record.claim_id
        assert first.launch_claim is not None
        repeated = store.authority(claim, deadline=time.monotonic() + 5)
        assert repeated.record == first.record and repeated.launch_claim is None
        shown = store.authority(
            command(project, generation, operation="show", approval_id=pending.record.approval_id),
            deadline=time.monotonic() + 5,
        )
        assert shown.record == first.record and shown.launch_claim is None


def test_legacy_reviews_and_request_ids_cannot_become_authority_and_concurrent_claim_is_unique(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        legacy_manifest = ActionManifest(profile=manifest.profile, argv=manifest.argv)
        legacy_call = ExecutionCall(directory=project.root, operation="propose", argv=manifest.argv)
        legacy = ReviewOperation(
            call=legacy_call,
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            manifest=legacy_manifest,
            configuration_fingerprint=manifest.profile.fingerprint,
        )
        old = store.review(legacy, deadline=time.monotonic() + 5).items[0]
        with pytest.raises(ReviewFault) as collision:
            store.authority(
                command(
                    project,
                    generation,
                    operation="propose",
                    request_id=legacy_call.request_id,
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert collision.value.code == "execution_conflict"
        with pytest.raises(ReviewFault) as old_approval:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=old.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert old_approval.value.code == "execution_not_found"
        proposal = command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        )
        fresh = store.authority(proposal, deadline=time.monotonic() + 5).record
        with pytest.raises(ReviewFault) as reverse:
            store.review(
                legacy.model_copy(
                    update={
                        "call": legacy_call.model_copy(update={"request_id": proposal.request_id})
                    }
                ),
                deadline=time.monotonic() + 5,
            )
        assert reverse.value.code == "execution_conflict"
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=fresh.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        other = Project(uuid4(), tmp_path / "other")
        store.index(other)
        with pytest.raises(ReviewFault) as scope:
            store.authority(
                command(
                    other,
                    generation,
                    operation="claim",
                    approval_id=fresh.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert scope.value.code == "execution_not_found"

        def claim_once() -> UUID | str | None:
            try:
                return store.authority(
                    command(
                        project,
                        generation,
                        operation="claim",
                        approval_id=fresh.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                ).launch_claim
            except ReviewFault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as callers:
            outcomes = list(callers.map(lambda _: claim_once(), range(8)))
        assert sum(isinstance(item, UUID) for item in outcomes) == 1
        assert outcomes.count("execution_conflict") == 7
        old_now = store.review(
            ReviewOperation(
                call=ExecutionCall(
                    directory=project.root, operation="approval.show", object_id=old.approval_id
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        assert old_now.launch_authority_issued is False and old_now.execution_available is False


def test_expiry_changed_binding_and_clock_rollback_never_grant_claim(tmp_path: Path) -> None:
    clock = {"wall": 1000000, "mono": 1000}
    with authority_store(tmp_path, clock) as (store, project, manifest, generation):
        first = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        clock["wall"] += 600000
        clock["mono"] += 600
        with pytest.raises(ReviewFault) as expired:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=first.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert expired.value.code == "execution_expired"
        changed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        with pytest.raises(ReviewFault) as mismatch:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=changed.approval_id,
                    current_manifest_digest="f" * 64,
                ),
                deadline=time.monotonic() + 5,
            )
        assert mismatch.value.code == "execution_target_mismatch"
        last = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=last.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        clock["wall"] -= 1
        with pytest.raises(ReviewFault) as backwards:
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=last.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert backwards.value.code == "execution_clock"
        shown = store.authority(
            command(project, generation, operation="show", approval_id=last.approval_id),
            deadline=time.monotonic() + 5,
        )
        assert shown.record.state == "invalidated" and shown.launch_claim is None


def test_restart_invalidates_unused_authority_and_quarantines_unknown_claim(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        first = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        second = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        for record in (first, second):
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        store.authority(
            command(
                project,
                generation,
                operation="claim",
                approval_id=first.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        with pytest.raises(ReviewFault) as occupied:
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=second.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert occupied.value.code == "execution_capacity"
        store.close(timeout=5)
        new_generation = uuid4()
        again = SQLiteStorage(store.owner, instance_id=new_generation)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            old = again.authority(
                command(project, new_generation, operation="show", approval_id=first.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert old.record.state == "consumed" and old.record.claim_state == "unknown"
            assert old.launch_claim is None
            unused = again.authority(
                command(project, new_generation, operation="show", approval_id=second.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert unused.record.state == "invalidated" and unused.launch_claim is None
            fresh = again.authority(
                command(
                    project,
                    new_generation,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            ).record
            again.authority(
                command(
                    project,
                    new_generation,
                    operation="approve",
                    approval_id=fresh.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            with pytest.raises(ReviewFault) as quarantined:
                again.authority(
                    command(
                        project,
                        new_generation,
                        operation="claim",
                        approval_id=fresh.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert quarantined.value.code == "execution_outcome_unknown"
        finally:
            again.close(timeout=5)


def test_clean_schema5_migration_preserves_registration_settings_and_legacy_review(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        layout = store.owner.store.layout
        legacy_manifest = ActionManifest(profile=manifest.profile, argv=manifest.argv)
        legacy = store.review(
            ReviewOperation(
                call=ExecutionCall(directory=project.root, operation="propose", argv=manifest.argv),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                manifest=legacy_manifest,
                configuration_fingerprint=manifest.profile.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        store.close(timeout=5)
        with closing(sqlite3.connect(layout.database)) as fixture, fixture:
            fixture.execute("DROP TABLE execution_stop_payloads")
            fixture.execute("DELETE FROM schema_migrations WHERE version=10")
            fixture.execute("DROP TABLE execution_lifecycles")
            fixture.execute("DELETE FROM schema_migrations WHERE version=9")
            fixture.execute("DROP TABLE execution_results")
            fixture.execute("DELETE FROM schema_migrations WHERE version=8")
            fixture.execute("DROP TABLE execution_launch_events")
            fixture.execute("DROP TABLE execution_launches")
            fixture.execute("DELETE FROM schema_migrations WHERE version=7")
            fixture.execute("DROP TABLE execution_authority_operations")
            fixture.execute("DROP TABLE execution_authorities")
            fixture.execute("DELETE FROM schema_migrations WHERE version=6")
            fixture.execute("PRAGMA user_version=5")
        settings_before = layout.settings.read_bytes()
        new_generation = uuid4()
        migrated = SQLiteStorage(store.owner, instance_id=new_generation)
        migrated.start()
        try:
            assert migrated.wait_ready(5).schema_version == 10
            assert migrated.lookup(project) == project
            assert layout.settings.read_bytes() == settings_before
            observed = migrated.review(
                ReviewOperation(
                    call=ExecutionCall(
                        directory=project.root,
                        operation="approval.show",
                        object_id=legacy.approval_id,
                    ),
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=new_generation,
                ),
                deadline=time.monotonic() + 5,
            ).items[0]
            assert observed.state == "invalidated" and observed.launch_authority_issued is False
            with pytest.raises(ReviewFault) as still_not_authority:
                migrated.authority(
                    command(
                        project,
                        new_generation,
                        operation="approve",
                        approval_id=legacy.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert still_not_authority.value.code == "execution_not_found"
        finally:
            migrated.close(timeout=5)


def test_journal_competing_attempts_and_changed_bindings_cannot_reserve_twice(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)

        def reserve(index: int) -> UUID | str:
            candidate = intent.model_copy(update={"attempt_id": uuid4(), "nonce": f"{index:032x}"})
            try:
                store.record(fact(candidate, "create_intent"), deadline=time.monotonic() + 5)
                return candidate.attempt_id
            except DockerBackendFault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as callers:
            outcomes = list(callers.map(reserve, range(8)))
        winners = [value for value in outcomes if isinstance(value, UUID)]
        assert len(winners) == 1
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.intent.attempt_id == winners[0] and snapshot.phases == ("create_intent",)
        for changed in (
            snapshot.intent.model_copy(update={"nonce": "f" * 32}),
            snapshot.intent.model_copy(
                update={
                    "inputs": snapshot.intent.inputs.model_copy(
                        update={"path": tmp_path / "other-input"}
                    )
                }
            ),
        ):
            with pytest.raises(DockerBackendFault):
                store.record(fact(changed, "created"), deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            store.launch_status(
                launch_scope(intent).model_copy(update={"project_id": uuid4()}),
                deadline=time.monotonic() + 5,
            )


def test_journal_original_expiry_and_clock_health_gate_start_but_not_cleanup(
    tmp_path: Path,
) -> None:
    for index, mode in enumerate(("wall", "mono", "rollback")):
        clock = {"wall": 1000000, "mono": 1000}
        with authority_store(tmp_path / str(index), clock) as (
            store,
            project,
            manifest,
            generation,
        ):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            if mode == "wall":
                clock["wall"] += 600001
            elif mode == "mono":
                clock["mono"] += 601
            else:
                clock["wall"] -= 1
            with pytest.raises(DockerBackendFault):
                store.record(fact(intent, "start_intent"), deadline=time.monotonic() + 5)
            store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
            store.record(fact(intent, "cleaned"), deadline=time.monotonic() + 5)
            snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
            assert snapshot.uncertain and snapshot.claim_state == "unknown"
            assert "start_intent" not in snapshot.phases and "cleaned" in snapshot.phases


def test_journal_partial_identity_and_owned_unknown_cleanup_remain_distinct(tmp_path: Path) -> None:
    with authority_store(tmp_path / "p") as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        partial = fact(intent, "unknown").model_copy(update={"observed_container_id": "e" * 64})
        store.record(partial, deadline=time.monotonic() + 5)
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.observed_container_id == "e" * 64 and snapshot.container_id is None
        with pytest.raises(DockerBackendFault):
            store.record(fact(intent, "cleaned"), deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            store.record(
                partial.model_copy(update={"observed_container_id": "f" * 64}),
                deadline=time.monotonic() + 5,
            )
        assert store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5) == snapshot
    with authority_store(tmp_path / "o") as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "unknown", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.container_id == "e" * 64 and snapshot.exit_code == 7 and snapshot.uncertain
        for changes in (
            {"container_id": "f" * 64},
            {"created_at": "2026-02-01T00:00:00.000000000Z"},
        ):
            with pytest.raises(DockerBackendFault):
                store.record(
                    fact(intent, "cleaned").model_copy(update=changes),
                    deadline=time.monotonic() + 5,
                )
        assert store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5) == snapshot


def test_journal_postcommit_reply_loss_never_replays_mutation_or_releases_admission(
    tmp_path: Path,
) -> None:
    for index, lost in enumerate(("create_intent", "start_intent", "cleaned")):
        with authority_store(tmp_path / str(index)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            phases = ("create_intent", "created", "start_intent", "exited", "cleaned")
            for phase in phases[: phases.index(lost)]:
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)

            class LostReply:
                def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
                    store.record(
                        event, deadline=deadline
                    )  # Real commit, then synthetic transport loss.
                    raise DockerBackendFault("docker_journal_unconfirmed")

            with pytest.raises(DockerBackendFault):
                LostReply().record(fact(intent, lost), deadline=time.monotonic() + 5)
            if lost in {"create_intent", "start_intent"}:
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, lost), deadline=time.monotonic() + 5)
            snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
            assert lost in snapshot.phases and snapshot.claim_state == "claimed"
            with pytest.raises(ReviewFault) as occupied:
                launch_intent(store, project, manifest, generation)
            assert occupied.value.code == "execution_capacity"
            store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
            assert store.launch_status(
                launch_scope(intent), deadline=time.monotonic() + 5
            ).uncertain


def test_journal_restart_and_clean_schema6_migration_never_restore_permission(
    tmp_path: Path,
) -> None:
    for downgrade in (False, True):
        with authority_store(tmp_path / str(downgrade)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            if not downgrade:
                for phase in ("create_intent", "created", "start_intent"):
                    store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            store.close(timeout=5)
            settings = store.owner.store.layout.settings.read_bytes()
            if downgrade:
                with (
                    closing(sqlite3.connect(store.owner.store.layout.database)) as fixture,
                    fixture,
                ):
                    fixture.execute("DROP TABLE execution_stop_payloads")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                    fixture.execute("DROP TABLE execution_lifecycles")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                    fixture.execute("DROP TABLE execution_results")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=8")
                    fixture.execute("DROP TABLE execution_launch_events")
                    fixture.execute("DROP TABLE execution_launches")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=7")
                    fixture.execute("PRAGMA user_version=6")
            new_generation = uuid4()
            again = SQLiteStorage(store.owner, instance_id=new_generation)
            again.start()
            try:
                assert again.wait_ready(5).schema_version == 10
                assert (
                    again.lookup(project) == project
                    and store.owner.store.layout.settings.read_bytes() == settings
                )
                old = again.authority(
                    command(
                        project,
                        new_generation,
                        operation="show",
                        approval_id=intent.record.approval_id,
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert old.record.claim_state == "unknown" and old.launch_claim is None
                with pytest.raises(DockerBackendFault):
                    again.record(fact(intent, "start_intent"), deadline=time.monotonic() + 5)
                if not downgrade:
                    snapshot = again.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    )
                    assert snapshot.phases == ("create_intent", "created", "start_intent")
                    assert snapshot.uncertain and snapshot.container_id == "e" * 64
            finally:
                again.close(timeout=5)


def test_sql_control_reservation_stays_inside_sixteen_pending_positions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from jeff.application.execution import ExecutionCancelCall

    original = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    armed = [False]

    class Paused(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if armed[0] and sql.startswith("SELECT project_id,canonical_root FROM projects"):
                armed[0] = False
                entered.set()
                assert release.wait(3)
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return cast(sqlite3.Connection, original(*args, **{**kwargs, "factory": Paused}))

    monkeypatch.setattr(sqlite3, "connect", connect)
    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=proposed.approval_id,
            reviewed_manifest_digest=manifest.fingerprint,
        )
        with ThreadPoolExecutor(max_workers=1) as observer:
            armed[0] = True
            lookup = observer.submit(store.lookup, project, deadline=time.monotonic() + 5)
            try:
                assert entered.wait(1)
                # The public nonblocking control port provides deterministic admission
                # while the one existing SQL actor is paused, without private queue reads.
                queued = [
                    store.submit_cancel(call, deadline=time.monotonic() + 5) for _ in range(15)
                ]
                with pytest.raises(StorageFault) as ordinary:
                    store.lookup(project, deadline=time.monotonic() + 5)
                assert ordinary.value.code == "storage_busy"
                queued.append(store.submit_cancel(call, deadline=time.monotonic() + 5))
                with pytest.raises(StorageFault) as full:
                    store.submit_cancel(call, deadline=time.monotonic() + 5)
                assert full.value.code == "storage_busy"
            finally:
                release.set()
            assert lookup.result(timeout=2) == project
            first = queued[0].result(timeout=2)
            assert all(f.result(timeout=2) == first for f in queued)
            assert first.terminal is not None and first.terminal.evidence == "not_started"


def test_stop_revokes_approved_authority_atomically_and_replays_first_facts(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        scope = ExecutionInspectionScope(
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            approval_id=proposed.approval_id,
        )
        request = ExecutionStopRequest(
            **scope.model_dump(),
            request_id=uuid4(),
            manifest_digest=manifest.fingerprint,
        )
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) is None
        with pytest.raises(ReviewFault):  # Pending reviews still use deny.
            store.request_stop(request, deadline=time.monotonic() + 5)
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        first = store.request_stop(request, deadline=time.monotonic() + 5)
        assert first.terminal is not None and first.terminal.outcome == "cancelled"
        assert first.terminal.evidence == "not_started" and first.terminal.cleanup_verified
        assert store.request_stop(request, deadline=time.monotonic() + 5) == first
        duplicate = request.model_copy(update={"request_id": uuid4()})
        assert store.request_stop(duplicate, deadline=time.monotonic() + 5) == first
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) == first
        with pytest.raises(ReviewFault):
            store.request_stop(
                request.model_copy(update={"reason": "shutdown"}), deadline=time.monotonic() + 5
            )
        with pytest.raises(ReviewFault):
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=proposed.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        shown = store.authority(
            command(
                project,
                generation,
                operation="show",
                approval_id=proposed.approval_id,
            ),
            deadline=time.monotonic() + 5,
        )
        assert shown.record.state.value == "invalidated" and shown.launch_claim is None
        assert shown.record.claim_id is None


def test_stop_prelaunch_fence_requires_no_inflight_create_and_never_adopts(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )

    for inflight in (False, True):
        with authority_store(tmp_path / str(inflight)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            if inflight:
                store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            first = store.request_stop(request, deadline=time.monotonic() + 5)
            assert first.terminal is None
            if not inflight:
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)
            observation = ExecutionStopObservation(
                scope=scope,
                request_id=first.request_id,
                manifest_digest=manifest.fingerprint,
                terminal=ExecutionStopTerminal(
                    outcome="cancelled",
                    evidence="not_started",
                    cleanup_verified=True,
                ),
            )
            if inflight:
                with pytest.raises(ReviewFault):
                    store.finish_stop(observation, deadline=time.monotonic() + 5)
                pending = store.inspect_stop(scope, deadline=time.monotonic() + 5)
                assert pending is not None and pending.terminal is None
            else:
                final = store.finish_stop(observation, deadline=time.monotonic() + 5)
                assert final.terminal == observation.terminal
                assert store.finish_stop(observation, deadline=time.monotonic() + 5) == final
                shown = store.authority(
                    command(
                        project,
                        generation,
                        operation="show",
                        approval_id=scope.approval_id,
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record.claim_state == "finished" and shown.launch_claim is None
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, "create_intent"), deadline=time.monotonic() + 5)


def test_stop_natural_completion_wins_without_rewriting_legacy_result(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    for request_before in (True, False):
        with authority_store(tmp_path / str(request_before)) as (
            store,
            project,
            manifest,
            generation,
        ):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            for phase in ("create_intent", "created", "start_intent"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            if request_before:
                assert store.request_stop(request, deadline=time.monotonic() + 5).terminal is None
            for phase in ("exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            observed = completion(intent)
            original = observed.fingerprint
            result = store.complete_execution(observed, deadline=time.monotonic() + 5)
            assert result.disposition == "completed" and result.completion_digest == original
            if not request_before:
                store.request_stop(request, deadline=time.monotonic() + 5)
            stopped = store.inspect_stop(scope, deadline=time.monotonic() + 5)
            assert stopped is not None and stopped.terminal is not None
            assert stopped.terminal.outcome == "completed"
            assert stopped.terminal.exit_code == 7
            assert store.request_stop(request, deadline=time.monotonic() + 5) == stopped
            assert (
                store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                == result
            )


def test_stop_known_owned_exit_keeps_partial_logs_distinct_and_unknown_quarantined(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import (
        DockerOutputFacts,
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )
    from jeff.domain.execution import fingerprint

    for uncertain in (False, True):
        with authority_store(tmp_path / str(uncertain)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=intent.record.approval_id,
            )
            for phase in ("create_intent", "created", "start_intent"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            request = ExecutionStopRequest(
                **scope.model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
                reason="timeout",
            )
            first = store.request_stop(request, deadline=time.monotonic() + 5)
            for phase in ("exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            owned = completion(intent).owned
            capture = GuardedOutputCollector(manifest.profile.output_bytes)
            capture.accept("stdout", b"partial")
            snapshot = capture.seal(abort=True)
            facts = DockerOutputFacts.model_validate(
                snapshot.model_dump(exclude={"stdout", "stderr"})
            )
            terminal = ExecutionStopTerminal(
                outcome="timed_out",
                evidence="owned",
                cleanup_verified=True,
                exit_code=7,
                attempt_id=intent.attempt_id,
                ownership_digest=fingerprint(
                    {
                        "intent": intent.fingerprint,
                        "container": owned.container_id,
                        "birth": owned.created_at,
                    }
                ),
                output_facts=facts,
                output_digest="a" * 64,
            )
            observation = ExecutionStopObservation(
                scope=scope,
                request_id=first.request_id,
                manifest_digest=manifest.fingerprint,
                terminal=terminal,
                owned=owned,
            )
            if not uncertain:
                forged_completed = observation.model_copy(
                    update={
                        "terminal": terminal.model_copy(update={"outcome": "completed"}),
                    }
                )
                with pytest.raises(ReviewFault):
                    store.finish_stop(forged_completed, deadline=time.monotonic() + 5)
            if uncertain:
                store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
                with pytest.raises(ReviewFault):
                    store.finish_stop(observation, deadline=time.monotonic() + 5)
                observation = observation.model_copy(
                    update={"terminal": terminal.model_copy(update={"outcome": "unknown"})}
                )
            final = store.finish_stop(observation, deadline=time.monotonic() + 5)
            assert final.terminal is not None and final.terminal.output_facts is not None
            assert (
                final.terminal.output_facts.aborted and not final.terminal.output_facts.stdout_eof
            )
            assert final.terminal.outcome == ("unknown" if uncertain else "timed_out")
            shown = store.authority(
                command(project, generation, operation="show", approval_id=scope.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert shown.record.claim_state == ("unknown" if uncertain else "finished")
            # Wire12 projects separate terminal facts, never a fabricated v1 result
            # or retroactively attached text for this metadata-only DB9 record.
            inspected = store.inspect_execution(scope, deadline=time.monotonic() + 5)
            assert inspected.completion_state == "stop_recorded" and inspected.result is None
            assert inspected.stop is not None and inspected.stop.outcome == final.terminal.outcome
            assert inspected.stop.payload_state == "missing" and inspected.stop_output is None
            assert store.snapshot().state == "ready"


def test_schema8_to9_signs_source_before_ddl_preserves_v1_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect
    for fail_ddl in (False, True):
        with authority_store(tmp_path / str(fail_ddl)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            original = store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
            store.close(timeout=5)
            layout = store.owner.store.layout
            with closing(original_connect(layout.database)) as fixture, fixture:
                fixture.execute("DROP TABLE execution_stop_payloads")
                fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                fixture.execute("DROP TABLE execution_lifecycles")
                fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                fixture.execute("PRAGMA user_version=8")
            before = layout.database.read_bytes()
            settings = layout.settings.read_bytes()
            signed: list[int] = []
            ddl: list[str] = []
            arm = store.owner.arm_storage_recovery

            def signed_arm(
                *args: Any,
                _signed: list[int] = signed,
                _arm: Callable[..., _RecoveryPermit] = arm,
                **kwargs: Any,
            ) -> _RecoveryPermit:
                _signed.append(int(kwargs["schema_version"]))
                return _arm(*args, **kwargs)

            class Migration(sqlite3.Connection):
                def execute(
                    self,
                    sql: str,
                    parameters: Any = (),
                    /,
                    *,
                    _signed: list[int] = signed,
                    _ddl: list[str] = ddl,
                    _fail: bool = fail_ddl,
                ) -> sqlite3.Cursor:
                    if sql.startswith("CREATE TABLE execution_lifecycles"):
                        assert _signed and _signed[-1] == 8
                        _ddl.append(sql)
                        if _fail:
                            raise sqlite3.OperationalError("synthetic DB9 DDL refusal")
                    return super().execute(sql, parameters)

            def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                return cast(
                    sqlite3.Connection, original_connect(*args, **{**kwargs, "factory": Migration})
                )

            with monkeypatch.context() as patch:
                patch.setattr(store.owner, "arm_storage_recovery", signed_arm)
                patch.setattr(sqlite3, "connect", connect)
                migrated = SQLiteStorage(store.owner, instance_id=uuid4())
                migrated.start()
                try:
                    state = migrated.wait_ready(5)
                    assert len(ddl) == 1 and signed[0] == 8
                    if fail_ddl:
                        assert state.state == "failed"
                    else:
                        assert state.schema_version == 10
                        assert (
                            migrated.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            )
                            == original
                        )
                        assert migrated.lookup(project) == project
                finally:
                    migrated.close(timeout=5)
            assert layout.settings.read_bytes() == settings
            if fail_ddl:
                assert layout.database.read_bytes() == before


@pytest.mark.parametrize("failure", ("none", "ddl", "permit"))
def test_db10_migration_signs_exact_source9_before_ddl_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original_connect = sqlite3.connect
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
        store.close(timeout=5)
        layout = store.owner.store.layout
        with closing(original_connect(layout.database)) as sql, sql:
            sql.execute("DROP TABLE execution_stop_payloads")
            sql.execute("DELETE FROM schema_migrations WHERE version=10")
            sql.execute("PRAGMA user_version=9")
        before = layout.database.read_bytes()
        settings = layout.settings.read_bytes()
        signed: list[int] = []
        ddl: list[str] = []
        arm = store.owner.arm_storage_recovery

        def signed_arm(*args: Any, **kwargs: Any) -> _RecoveryPermit:
            signed.append(int(kwargs["schema_version"]))
            if failure == "permit":
                raise StorageFault("storage_recovery_required")
            return arm(*args, **kwargs)

        class Migration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("CREATE TABLE execution_stop_payloads"):
                    assert signed == [9]
                    ddl.append(sql)
                    if failure == "ddl":
                        raise sqlite3.OperationalError("synthetic DB10 DDL refusal")
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection, original_connect(*args, **{**kwargs, "factory": Migration})
            )

        with monkeypatch.context() as patch:
            patch.setattr(store.owner, "arm_storage_recovery", signed_arm)
            patch.setattr(sqlite3, "connect", connect)
            migrated = SQLiteStorage(store.owner, instance_id=uuid4())
            migrated.start()
            try:
                state = migrated.wait_ready(5)
                assert signed and signed[0] == 9
                if failure != "none":
                    assert state.state == "failed"
                else:
                    assert state.schema_version == 10
                    assert (
                        migrated.execution_result(
                            launch_scope(intent), deadline=time.monotonic() + 5
                        )
                        == original
                    )
                    assert migrated.lookup(project) == project
                assert len(ddl) == (0 if failure == "permit" else 1)
            finally:
                migrated.close(timeout=5)
        assert layout.settings.read_bytes() == settings
        if failure != "none":
            assert layout.database.read_bytes() == before


def test_stop_claim_races_scope_and_namespace_never_reissue_permission(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )

    for index in range(4):
        with authority_store(tmp_path / str(index)) as (store, project, manifest, generation):
            proposal = command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            )
            record = store.authority(proposal, deadline=time.monotonic() + 5).record
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=record.approval_id,
            )
            request = ExecutionStopRequest(
                **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
            )
            for changes in (
                {"owner_instance_id": uuid4()},
                {"canonical_root": project.root / "wrong"},
                {"manifest_digest": "b" * 64},
                {"request_id": proposal.request_id},
            ):
                with pytest.raises(ReviewFault):
                    store.request_stop(
                        request.model_copy(update=changes), deadline=time.monotonic() + 5
                    )
            claim = command(
                project,
                generation,
                operation="claim",
                approval_id=record.approval_id,
                current_manifest_digest=manifest.fingerprint,
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                stop_future = pool.submit(
                    store.request_stop, request, deadline=time.monotonic() + 5
                )
                claim_future = pool.submit(store.authority, claim, deadline=time.monotonic() + 5)
                stopped = stop_future.result(timeout=5)
                try:
                    claimed = claim_future.result(timeout=5)
                except ReviewFault:
                    assert stopped.terminal is not None
                else:
                    assert claimed.launch_claim is not None and stopped.terminal is None
                    replay = store.authority(claim, deadline=time.monotonic() + 5)
                    assert replay.launch_claim is None
                    store.finish_stop(
                        ExecutionStopObservation(
                            scope=scope,
                            request_id=stopped.request_id,
                            manifest_digest=manifest.fingerprint,
                            terminal=ExecutionStopTerminal(
                                outcome="cancelled", evidence="not_started", cleanup_verified=True
                            ),
                        ),
                        deadline=time.monotonic() + 5,
                    )
            assert (
                store.request_stop(request, deadline=time.monotonic() + 5).request_id
                == stopped.request_id
            )
            final = store.inspect_stop(scope, deadline=time.monotonic() + 5)
            assert final is not None and final.terminal is not None
            assert final.terminal.outcome == "cancelled"


def test_stop_rollback_lost_commit_ack_and_restart_never_restore_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import ExecutionStopRequest

    for mode in ("rollback", "lost_ack"):
        with authority_store(tmp_path / mode) as (store, project, manifest, generation):
            record = store.authority(
                command(
                    project,
                    generation,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            ).record
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            request = ExecutionStopRequest(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                approval_id=record.approval_id,
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            )
            commit = store._commit_write

            def failed_commit(
                connection: sqlite3.Connection,
                fd: int,
                *,
                _mode: str = mode,
                _commit: Callable[[sqlite3.Connection, int], None] = commit,
            ) -> None:
                if _mode == "lost_ack":
                    _commit(connection, fd)
                    raise StorageFault("storage_outcome_unconfirmed")
                raise StorageFault("storage_busy")

            with monkeypatch.context() as patch:
                patch.setattr(store, "_commit_write", failed_commit)
                with pytest.raises(ReviewFault):
                    store.request_stop(request, deadline=time.monotonic() + 5)
            if mode == "rollback":
                from jeff.application.execution import ExecutionInspectionScope

                scope = ExecutionInspectionScope.model_validate(
                    request.model_dump(exclude={"request_id", "manifest_digest", "reason"})
                )
                assert store.inspect_stop(scope, deadline=time.monotonic() + 5) is None
                shown = store.authority(
                    command(project, generation, operation="show", approval_id=record.approval_id),
                    deadline=time.monotonic() + 5,
                )
                assert shown.record.state.value == "approved" and shown.launch_claim is None
            else:
                store.close(timeout=5)
                new_generation = uuid4()
                reopened = SQLiteStorage(store.owner, instance_id=new_generation)
                reopened.start()
                try:
                    assert reopened.wait_ready(5).state == "ready"
                    shown = reopened.authority(
                        command(
                            project,
                            new_generation,
                            operation="show",
                            approval_id=record.approval_id,
                        ),
                        deadline=time.monotonic() + 5,
                    )
                    assert shown.record.state.value == "invalidated" and shown.launch_claim is None
                    with pytest.raises(ReviewFault):
                        reopened.request_stop(
                            request.model_copy(update={"owner_instance_id": new_generation}),
                            deadline=time.monotonic() + 5,
                        )
                    with pytest.raises(ReviewFault):
                        reopened.authority(
                            command(
                                project,
                                new_generation,
                                operation="claim",
                                approval_id=record.approval_id,
                                current_manifest_digest=manifest.fingerprint,
                            ),
                            deadline=time.monotonic() + 5,
                        )
                finally:
                    reopened.close(timeout=5)


def test_stop_replay_keeps_first_utc_and_budgets_without_restarting_a_timer(tmp_path: Path) -> None:
    from jeff.application.execution import ExecutionInspectionScope, ExecutionStopRequest

    clock = {"wall": 100_000, "mono": 100}
    with authority_store(tmp_path, clock) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        scope = ExecutionInspectionScope(
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            approval_id=intent.record.approval_id,
        )
        request = ExecutionStopRequest(
            **scope.model_dump(),
            request_id=uuid4(),
            manifest_digest=manifest.fingerprint,
        )
        first = store.request_stop(request, deadline=time.monotonic() + 5)
        assert first.requested_at_ms == 100_000 and first.clock_confirmed
        clock.update(wall=120_000, mono=120)
        assert store.request_stop(request, deadline=time.monotonic() + 5) == first
        assert (
            store.request_stop(
                request.model_copy(
                    update={
                        "request_id": uuid4(),
                        "reason": "shutdown",
                    }
                ),
                deadline=time.monotonic() + 5,
            )
            == first
        )
        assert (first.grace_seconds, first.cleanup_seconds, first.completion_seconds) == (5, 10, 5)
        assert "monotonic" not in first.model_dump_json()


def test_stop_guarded_capture_is_atomic_and_inspectable(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )
    from jeff.domain.execution import fingerprint

    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        owned = DockerOwnedContainer(
            intent=intent, container_id="e" * 64, created_at="2026-01-01T00:00:00Z"
        )
        # Use the exact birth observation from the journal helper, not an invented handle.
        owned = owned.model_copy(update={"created_at": fact(intent, "created").created_at})
        scope = ExecutionInspectionScope(
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            approval_id=intent.record.approval_id,
        )
        first = store.request_stop(
            ExecutionStopRequest(
                **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
            ),
            deadline=time.monotonic() + 5,
        )
        capture = GuardedOutputCollector(manifest.profile.output_bytes)
        capture.accept("stdout", b"safe prefix\n")
        snapshot = capture.seal(abort=True)
        digest_helper = DockerCompletion(
            owned=owned,
            outcome=DockerJobOutcome(
                exit_code=7, timed_out=False, outcome_unknown=False, cleanup_verified=True
            ),
            output=snapshot,
        )
        observation = ExecutionStopObservation(
            scope=scope,
            request_id=first.request_id,
            manifest_digest=manifest.fingerprint,
            owned=owned,
            terminal=ExecutionStopTerminal(
                outcome="cancelled",
                evidence="owned",
                cleanup_verified=True,
                exit_code=7,
                attempt_id=intent.attempt_id,
                ownership_digest=fingerprint(
                    {
                        "intent": intent.fingerprint,
                        "container": owned.container_id,
                        "birth": owned.created_at,
                    }
                ),
                output_facts=digest_helper.output_facts,
                output_digest=digest_helper.output_digest,
            ),
        )
        bad = snapshot.model_copy(update={"stdout": "fake prefix\n"})
        with pytest.raises((ReviewFault, ValueError)):
            store.finish_stop_with_capture(observation, bad, deadline=time.monotonic() + 5)
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) == first
        final = store.finish_stop_with_capture(observation, snapshot, deadline=time.monotonic() + 5)
        assert final.terminal == observation.terminal
        assert (
            store.finish_stop_with_capture(observation, snapshot, deadline=time.monotonic() + 5)
            == final
        )
        inspected = store.inspect_execution(scope, deadline=time.monotonic() + 5)
        assert inspected.completion_state == "stop_recorded" and inspected.result is None
        assert inspected.stop is not None and inspected.stop.outcome == "cancelled"
        assert (
            inspected.stop.payload_state == "retained" and inspected.stop.output_facts is not None
        )
        assert inspected.stop.output_facts.aborted
        assert inspected.stop_output == snapshot
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql:
            assert sql.execute("SELECT count(*) FROM execution_results").fetchone() == (0,)
            assert sql.execute("SELECT count(*) FROM execution_stop_payloads").fetchone() == (1,)


def test_cancel_submission_resolves_indexed_scope_without_cwd_and_excludes_uuid_reuse(
    tmp_path: Path,
) -> None:
    from concurrent.futures import Future

    from jeff.application.execution import ExecutionCancelCall

    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=proposed.approval_id,
            reviewed_manifest_digest=manifest.fingerprint,
        )
        future = store.submit_cancel(call, deadline=time.monotonic() + 5)
        assert isinstance(future, Future)
        first = future.result(timeout=5)
        assert (
            first.scope.project_id == project.project_id
            and first.scope.canonical_root == project.root
        )
        assert first.terminal is not None and first.terminal.evidence == "not_started"
        assert store.submit_cancel(call, deadline=time.monotonic() + 5).result(timeout=5) == first
        with pytest.raises(ReviewFault):
            store.submit_cancel(
                call.model_copy(update={"reviewed_manifest_digest": "0" * 64}),
                deadline=time.monotonic() + 5,
            ).result(timeout=5)
        with pytest.raises(ReviewFault):
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    request_id=call.request_id,
                    approval_id=proposed.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
