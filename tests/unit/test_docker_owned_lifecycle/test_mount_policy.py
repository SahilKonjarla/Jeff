"""Exact mount policy and bounded configuration/state diagnostics without raw values."""

import copy
import time
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    AuthorityResult,
    DockerBackendFault,
    DockerPolicyFacts,
    DockerTargetDeclaration,
)
from jeff.infrastructure.runtime_execution import DockerJobController

from .support_controller import setup_controller
from .support_ports import Backend, Journal, Sink


def test_mount_listing_and_exact_tmpfs_declaration_agree_without_broadening_mounts() -> None:
    # Characterize the captured boolean shape, not a retained raw inspect:
    # project bind listed; scratch declared in exact HostConfig.Tmpfs.
    # Also retain the originally supported explicit scratch representation.
    for listed in (False, True):
        controller, backend, journal, claim, declaration = setup_controller()
        original = backend.inspect

        def inspect(
            endpoint: str,
            container_id: str,
            *,
            deadline: float,
            read: Callable[..., dict[str, object]] = original,
            scratch_listed: bool = listed,
        ) -> dict[str, object]:
            raw = read(endpoint, container_id, deadline=deadline)
            if not scratch_listed:
                entries = raw["Mounts"]
                assert isinstance(entries, list)
                raw["Mounts"] = [entry for entry in entries if entry["Destination"] == "/project"]
            return raw

        backend.inspect = inspect  # type: ignore[method-assign]
        handle = controller.create(
            claim, declaration, deadline=time.monotonic() + 2, cleanup_deadline=time.monotonic() + 4
        )
        outcome = controller.run(
            handle, Sink(), deadline=time.monotonic() + 2, cleanup_deadline=time.monotonic() + 4
        )
        assert outcome.exit_code == 7 and outcome.cleanup_verified and not outcome.outcome_unknown
        assert backend.calls == ["create", "attach", "remove"]
        assert [event.phase for event in journal.events] == [
            "create_intent",
            "created",
            "start_intent",
            "exited",
            "cleaned",
        ]

    def changed(
        case: str,
    ) -> tuple[
        DockerJobController, Backend, Journal, AuthorityResult, DockerTargetDeclaration, list[bool]
    ]:
        controller, backend, journal, claim, declaration = setup_controller()
        original = backend.inspect
        active = [False]

        def inspect(endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
            raw = original(endpoint, container_id, deadline=deadline)
            entries = raw["Mounts"]
            host = raw["HostConfig"]
            assert isinstance(entries, list) and isinstance(host, dict)
            if not case.startswith("explicit_"):
                entries = entries[:1]
                raw["Mounts"] = entries
            if not active[0]:
                return raw
            project = entries[0]
            tmpfs = host["Tmpfs"]
            assert isinstance(project, dict) and isinstance(tmpfs, dict)
            if case in ("missing_tmpfs", "explicit_missing_tmpfs"):
                host.pop("Tmpfs")
            elif case in ("changed_tmpfs", "explicit_changed_tmpfs"):
                tmpfs["/scratch"] = "rw,exec,mode=0777"
            elif case == "extra_tmpfs":
                tmpfs["/other"] = "rw"
            elif case == "foreign_source":
                project["Source"] = "/synthetic/unreviewed"
            elif case == "writable_project":
                project["RW"] = True
            elif case == "integer_project_rw":
                project["RW"] = 0
            elif case == "propagation":
                project["Propagation"] = "rshared"
            elif case == "project_type":
                project["Type"] = "volume"
            elif case == "duplicate_project":
                entries.append(copy.deepcopy(project))
            elif case == "foreign_destination":
                entries.append({"Destination": "/other", "Type": "tmpfs", "RW": True})
            elif case == "malformed_destination":
                project["Destination"] = []
            elif case == "empty_mounts":
                raw["Mounts"] = []
            elif case == "extra_mounts":
                entries.extend([copy.deepcopy(project), copy.deepcopy(project)])
            elif case == "explicit_type":
                entries[1]["Type"] = "bind"
            elif case == "explicit_rw":
                entries[1]["RW"] = False
            elif case == "explicit_integer_rw":
                entries[1]["RW"] = 1
            elif case == "explicit_missing_scratch":
                entries[1]["Destination"] = "/other"
            elif case == "explicit_empty":
                entries[1] = {}
            else:
                raise AssertionError("unknown synthetic case")
            return raw

        backend.inspect = inspect  # type: ignore[method-assign]
        return controller, backend, journal, claim, declaration, active

    # Selected adversarial groups, not a cross-product matrix. Scratch declaration
    # remains compulsory even when its Mounts entry is absent or present.
    for case in (
        "missing_tmpfs",
        "changed_tmpfs",
        "extra_tmpfs",
        "foreign_source",
        "writable_project",
        "integer_project_rw",
        "propagation",
        "project_type",
        "duplicate_project",
        "foreign_destination",
        "malformed_destination",
        "empty_mounts",
        "extra_mounts",
        "explicit_type",
        "explicit_rw",
        "explicit_integer_rw",
        "explicit_missing_scratch",
        "explicit_missing_tmpfs",
        "explicit_changed_tmpfs",
        "explicit_empty",
    ):
        controller, backend, journal, claim, declaration, active = changed(case)
        active[0] = True
        with pytest.raises(DockerBackendFault) as caught:
            controller.create(
                claim,
                declaration,
                deadline=time.monotonic() + 2,
                cleanup_deadline=time.monotonic() + 4,
            )
        assert caught.value.code == "docker_policy_mismatch"
        assert backend.calls == ["create", "remove"] and journal.events[-1].phase == "cleaned"

    for case in ("changed_tmpfs", "foreign_source", "explicit_rw"):
        controller, backend, journal, claim, declaration, active = changed(case)
        handle = controller.create(
            claim, declaration, deadline=time.monotonic() + 2, cleanup_deadline=time.monotonic() + 4
        )
        active[0] = True
        outcome = controller.run(
            handle, Sink(), deadline=time.monotonic() + 2, cleanup_deadline=time.monotonic() + 4
        )
        assert outcome.outcome_unknown and outcome.cleanup_verified and outcome.exit_code is None
        assert backend.calls == ["create", "remove"]
        assert "start_intent" not in [event.phase for event in journal.events]


def test_policy_diagnostics_identify_configuration_checks_without_raw_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kind, expected in (
        ("user", "Config.User"),
        ("environment", "Config.Env.values"),
        ("tmpfs", "HostConfig.Tmpfs"),
        ("missing_tmpfs", "HostConfig.Tmpfs"),
        ("mount_count", "Mounts.count"),
        ("host_shape", "HostConfig"),
        ("mount_shape", "Mounts.entry"),
    ):
        controller, backend, journal, claim, declaration = setup_controller()
        original = backend.inspect
        changed = [False]

        def inspect(
            endpoint: str,
            container_id: str,
            *,
            deadline: float,
            mode: str = kind,
            read: Callable[..., dict[str, object]] = original,
            once: list[bool] = changed,
        ) -> dict[str, object]:
            raw = read(endpoint, container_id, deadline=deadline)
            if not once[0]:
                once[0] = True
                config = raw["Config"]
                host = raw["HostConfig"]
                assert isinstance(config, dict) and isinstance(host, dict)
                if mode == "user":
                    config["User"] = "SYNTHETIC_PRIVATE_VALUE"
                if mode == "environment":
                    config["Env"] = ["SYNTHETIC_PRIVATE_KEY=SYNTHETIC_PRIVATE_VALUE"]
                if mode == "tmpfs":
                    host["Tmpfs"] = {"SYNTHETIC_PRIVATE_KEY": "SYNTHETIC_PRIVATE_VALUE"}
                if mode == "missing_tmpfs":
                    host.pop("Tmpfs")
                if mode == "mount_count":
                    raw["Mounts"] = []
                if mode == "host_shape":
                    raw["HostConfig"] = None
                if mode == "mount_shape":
                    raw["Mounts"] = ["SYNTHETIC_PRIVATE_VALUE", {}]
            return raw

        with monkeypatch.context() as patch:
            patch.setattr(backend, "inspect", inspect)
            with pytest.raises(DockerBackendFault) as caught:
                controller.create(
                    claim,
                    declaration,
                    deadline=time.monotonic() + 5,
                    cleanup_deadline=time.monotonic() + 10,
                )
        assert caught.value.code == "docker_policy_mismatch" and caught.value.policy is not None
        facts = caught.value.policy
        assert facts.check_id == expected and facts.matches is False
        assert facts.present == (kind != "missing_tmpfs")
        assert facts.type_ok == (kind not in {"host_shape", "mount_shape", "missing_tmpfs"})
        assert "SYNTHETIC_PRIVATE" not in facts.model_dump_json() + str(caught.value)
        assert len(facts.model_dump_json().encode()) < 256
        assert backend.calls == ["create", "remove"] and journal.events[-1].phase == "cleaned"


def test_policy_diagnostics_cover_initial_state_and_reject_untrusted_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kind, expected in (
        ("status", "State.Status"),
        ("running", "State.Running"),
        ("mapping", "State"),
    ):
        controller, backend, journal, claim, declaration = setup_controller()
        original = backend.inspect
        changed = [False]

        def inspect(
            endpoint: str,
            container_id: str,
            *,
            deadline: float,
            mode: str = kind,
            read: Callable[..., dict[str, object]] = original,
            once: list[bool] = changed,
        ) -> dict[str, object]:
            raw = read(endpoint, container_id, deadline=deadline)
            if not once[0]:
                once[0] = True
                if mode == "mapping":
                    raw["State"] = None
                else:
                    state = raw["State"]
                    assert isinstance(state, dict)
                    state["Status" if mode == "status" else "Running"] = (
                        "SYNTHETIC_PRIVATE_VALUE" if mode == "status" else 0
                    )
            return raw

        with monkeypatch.context() as patch:
            patch.setattr(backend, "inspect", inspect)
            with pytest.raises(DockerBackendFault) as caught:
                controller.create(
                    claim,
                    declaration,
                    deadline=time.monotonic() + 5,
                    cleanup_deadline=time.monotonic() + 10,
                )
        assert caught.value.policy is not None and caught.value.policy.check_id == expected
        assert caught.value.policy.type_ok == (kind == "status")
        assert backend.calls == ["create", "remove"] and journal.events[-1].phase == "cleaned"
    facts = DockerPolicyFacts(check_id="State.Status", present=True, type_ok=True, matches=False)
    for bad in (
        {"check_id": "SYNTHETIC_PRIVATE_VALUE"},
        {"actual_value": "SYNTHETIC_PRIVATE_VALUE"},
        {"present": 1},
        {"type_ok": "false"},
        {"matches": True},
    ):
        with pytest.raises(ValidationError) as caught_model:
            DockerPolicyFacts.model_validate({**facts.model_dump(), **bad})
        assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught_model.value)
    with pytest.raises(ValueError):
        DockerBackendFault("docker_unavailable", policy=facts)
