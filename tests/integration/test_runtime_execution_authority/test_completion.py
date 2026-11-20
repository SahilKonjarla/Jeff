"""Atomic sanitized completion, binding/unknown refusals and immutable lost-ACK facts."""

import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import DockerBackendFault, DockerCompletion, ReviewFault
from jeff.application.storage import StorageFault

from .support_authority import authority_store
from .support_launch import completion, fact, launch_intent, launch_scope


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
