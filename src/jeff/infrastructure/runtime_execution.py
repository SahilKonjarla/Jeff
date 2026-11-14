"""Internal owned-container state machine; no public routing or implicit port defaults.

Real journal/input/output adapters are required before production composition. A
serialized model/label/name alone cannot adopt a resource into this controller.
"""

import math
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityResult,
    DockerBackendFault,
    DockerJobOutcome,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchJournal,
    DockerLifecycleBackend,
    DockerOutputSink,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    DockerTargetVerifier,
    PreparedInputVerifier,
)


def ownership_labels(intent: DockerLaunchIntent) -> dict[str, str]:
    return {
        "jeff.job": str(intent.record.job_id),
        "jeff.claim": str(intent.record.claim_id),
        "jeff.attempt": str(intent.attempt_id),
        "jeff.nonce": intent.nonce,
    }


def scratch_options(intent: DockerLaunchIntent) -> str:
    return (
        f"rw,noexec,nosuid,nodev,size={intent.record.manifest.profile.scratch_bytes},"
        "nr_inodes=4096,uid=65532,gid=65532,mode=0700"
    )


def create_arguments(intent: DockerLaunchIntent) -> tuple[str, ...]:
    """Fixed policy argv, not a pass-through Docker flag/shell surface."""
    p = intent.record.manifest.profile
    result = [
        "create",
        "--pull=never",
        "--name",
        "jeff-" + str(intent.attempt_id),
        "--read-only",
        "--network",
        "none",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--user",
        "65532:65532",
        "--workdir",
        "/project",
        "--entrypoint",
        p.executable,
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--log-driver",
        "none",
        "--restart",
        "no",
        "--no-healthcheck",
        "--stop-signal",
        "SIGTERM",
        "--memory",
        str(p.memory_bytes),
        "--memory-swap",
        str(p.memory_bytes),
        "--cpus",
        f"{p.cpu_millis / 1000:.3f}",
        "--pids-limit",
        str(p.pids),
        "--shm-size",
        "4194304",
        "--tmpfs",
        "/scratch:" + scratch_options(intent),
        "--mount",
        f"type=bind,source={intent.inputs.path},target=/project,readonly,bind-propagation=rprivate",
    ]
    for key, value in ownership_labels(intent).items():
        result.extend(("--label", f"{key}={value}"))
    for key, value in intent.target.effective_environment:
        result.extend(("--env", f"{key}={value}"))
    result.extend((p.image_id, *intent.record.manifest.argv[1:]))
    return tuple(result)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise DockerBackendFault("docker_policy_mismatch")
    return cast(dict[str, object], value)


@dataclass
class _Session:
    handle: DockerOwnedContainer
    start_attempted: bool = False
    removed: bool = False
    stop_attempted: bool = False
    kill_attempted: bool = False
    remove_attempted: bool = False


class DockerJobController:
    def __init__(
        self,
        verifier: DockerTargetVerifier,
        backend: DockerLifecycleBackend,
        inputs: PreparedInputVerifier,
        journal: DockerLaunchJournal,
        *,
        owner_instance_id: UUID,
    ) -> None:
        self.verifier, self.backend, self.inputs, self.journal = verifier, backend, inputs, journal
        self.owner_instance_id = owner_instance_id
        self._mutex = threading.Lock()
        self._used_claims: set[UUID] = set()
        self._sessions: dict[str, _Session] = {}
        self._declarations: dict[UUID, DockerTargetDeclaration] = {}
        self._quarantined = False
        self._journal_lost = False

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self._mutex.acquire(blocking=False):
            raise DockerBackendFault("docker_busy")
        try:
            yield
        finally:
            self._mutex.release()

    @staticmethod
    def _deadline(deadline: float) -> None:
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")

    @classmethod
    def _budgets(cls, deadline: float, cleanup_deadline: float) -> None:
        cls._deadline(deadline)
        if not math.isfinite(cleanup_deadline) or not deadline <= cleanup_deadline <= deadline + 10:
            raise DockerBackendFault("docker_unsafe_configuration")

    def _record(
        self,
        intent: DockerLaunchIntent,
        phase: str,
        *,
        deadline: float,
        handle: DockerOwnedContainer | None = None,
        exit_code: int | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        event = DockerLaunchEvent.model_validate(
            {
                "intent": intent,
                "phase": phase,
                "container_id": handle.container_id if handle else None,
                "created_at": handle.created_at if handle else None,
                "exit_code": exit_code,
                "observed_container_id": observed_container_id,
            }
        )
        try:
            self._deadline(deadline)
            self.journal.record(event, deadline=deadline)
            self._deadline(deadline)
        except Exception:
            self._quarantined = self._journal_lost = True
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def _unknown(
        self,
        intent: DockerLaunchIntent,
        deadline: float,
        handle: DockerOwnedContainer | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        self._quarantined = True
        try:
            self._record(
                intent,
                "unknown",
                deadline=deadline,
                handle=handle,
                observed_container_id=observed_container_id if handle is None else None,
            )
        except DockerBackendFault:
            pass  # Keep memory quarantine even when uncertainty cannot be durably recorded.

    def _engine(self, intent: DockerLaunchIntent, deadline: float) -> None:
        self._deadline(deadline)
        if self.backend.identity != intent.target.binary:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        if (
            self.backend.engine_id(intent.target.endpoint, deadline=deadline)
            != intent.target.engine_id
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")

    def _read(
        self,
        intent: DockerLaunchIntent,
        container_id: str,
        deadline: float,
        created_at: str | None = None,
    ) -> dict[str, object]:
        self._engine(intent, deadline)
        raw = self.backend.inspect(intent.target.endpoint, container_id, deadline=deadline)
        self._engine(intent, deadline)
        labels = _mapping(_mapping(raw.get("Config")).get("Labels"))
        if (
            raw.get("Id") != container_id
            or any(labels.get(k) != v for k, v in ownership_labels(intent).items())
            or not isinstance(raw.get("Created"), str)
            or (created_at is not None and raw.get("Created") != created_at)
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return raw

    @staticmethod
    def _safe_config(handle: DockerOwnedContainer, raw: dict[str, object]) -> None:
        intent, manifest = handle.intent, handle.intent.record.manifest
        p = manifest.profile
        config, host = _mapping(raw.get("Config")), _mapping(raw.get("HostConfig"))
        expected = {
            "User": "65532:65532",
            "WorkingDir": "/project",
            "Entrypoint": [p.executable],
            "Cmd": list(manifest.argv[1:]),
            "Labels": ownership_labels(intent),
            "StopSignal": "SIGTERM",
        }
        if any(config.get(k) != v for k, v in expected.items()):
            raise DockerBackendFault("docker_policy_mismatch")
        if any(config.get(k) is not False for k in ("OpenStdin", "AttachStdin", "Tty")):
            raise DockerBackendFault("docker_policy_mismatch")
        if config.get("Healthcheck") not in (None, {"Test": ["NONE"]}):
            raise DockerBackendFault("docker_policy_mismatch")
        environment = config.get("Env")
        if not isinstance(environment, list) or not all(isinstance(v, str) for v in environment):
            raise DockerBackendFault("docker_policy_mismatch")
        wanted_env = sorted(f"{k}={v}" for k, v in intent.target.effective_environment)
        if sorted(environment) != wanted_env:
            raise DockerBackendFault("docker_policy_mismatch")
        if (
            raw.get("Image") != p.image_id
            or raw.get("Path") != p.executable
            or raw.get("Args") != list(manifest.argv[1:])
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        fixed = {
            "NetworkMode": "none",
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Memory": p.memory_bytes,
            "MemorySwap": p.memory_bytes,
            "NanoCpus": p.cpu_millis * 1000000,
            "PidsLimit": p.pids,
            "ShmSize": 4194304,
            "CapDrop": ["ALL"],
            "Tmpfs": {"/scratch": scratch_options(intent)},
        }
        if any(
            host.get(k) != v or (type(v) is int and type(host.get(k)) is not int)
            for k, v in fixed.items()
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        if host.get("ReadonlyRootfs") is not True or any(
            host.get(k) is not False for k in ("Privileged", "AutoRemove", "PublishAllPorts")
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        if any(
            host.get(k) not in (None, [], {})
            for k in (
                "CapAdd",
                "Devices",
                "DeviceRequests",
                "DeviceCgroupRules",
                "PortBindings",
                "Links",
                "VolumesFrom",
                "ExtraHosts",
                "Binds",
            )
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        if (
            host.get("PidMode") not in ("", "private")
            or host.get("UTSMode") not in ("", "private")
            or host.get("Runtime") not in ("", "runc")
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        if host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges=true"]):
            raise DockerBackendFault("docker_policy_mismatch")
        if (
            _mapping(host.get("RestartPolicy")).get("Name") != "no"
            or _mapping(host.get("LogConfig")).get("Type") != "none"
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        mounts = raw.get("Mounts")
        if not isinstance(mounts, list) or len(mounts) != 2:
            raise DockerBackendFault("docker_policy_mismatch")
        by_target = {_mapping(m).get("Destination"): _mapping(m) for m in mounts}
        project, scratch = by_target.get("/project", {}), by_target.get("/scratch", {})
        if (
            project.get("Type") != "bind"
            or project.get("Source") != str(intent.inputs.path)
            or project.get("RW") is not False
            or project.get("Propagation") != "rprivate"
            or scratch.get("Type") != "tmpfs"
            or scratch.get("RW") is not True
        ):
            raise DockerBackendFault("docker_policy_mismatch")

    def _session(self, handle: DockerOwnedContainer) -> _Session:
        session = self._sessions.get(handle.container_id)
        if session is None or session.handle is not handle:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return session

    def inspect_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> dict[str, object]:
        with self._operation():
            if self._session(handle).removed:
                raise DockerBackendFault("docker_ownership_unconfirmed")
            return self._read(handle.intent, handle.container_id, deadline, handle.created_at)

    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerOwnedContainer:
        with self._operation():
            self._budgets(deadline, cleanup_deadline)
            if self._quarantined or any(not s.removed for s in self._sessions.values()):
                raise DockerBackendFault("docker_busy")
            record = claim.record
            if (
                claim.launch_claim is None
                or claim.launch_claim != record.claim_id
                or record.claim_state != "claimed"
                or record.owner_instance_id != self.owner_instance_id
                or record.manifest.profile != declaration.profile
                or claim.launch_claim in self._used_claims
                or len(self._used_claims) >= 5000
            ):
                raise DockerBackendFault("docker_authority_required")
            self._used_claims.add(claim.launch_claim)
            intent: DockerLaunchIntent | None = None
            handle: DockerOwnedContainer | None = None
            observed_container_id: str | None = None
            try:
                target = self.verifier.verify_target(declaration, deadline=deadline)
                if target.declaration_digest != declaration.fingerprint:
                    raise DockerBackendFault("docker_target_mismatch")
                binding = self.inputs.verify(record.manifest, deadline=deadline)
                intent = DockerLaunchIntent(
                    attempt_id=uuid4(),
                    record=record,
                    target=target,
                    inputs=binding,
                    nonce=secrets.token_hex(16),
                )
                self._declarations[intent.attempt_id] = declaration
                self.inputs.check(binding, record.manifest, deadline=deadline)
                self._engine(intent, deadline)
                self._record(intent, "create_intent", deadline=deadline)
                container_id = self.backend.create(intent, deadline=deadline)
                if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                    raise DockerBackendFault("docker_mutation_unconfirmed")
                observed_container_id = container_id
                if container_id in self._sessions:
                    raise DockerBackendFault("docker_mutation_unconfirmed")
                raw = self._read(intent, container_id, deadline)
                handle = DockerOwnedContainer(
                    intent=intent, container_id=container_id, created_at=cast(str, raw["Created"])
                )
                self._sessions[container_id] = _Session(handle)
                self._record(intent, "created", deadline=deadline, handle=handle)
                self._safe_config(handle, raw)
                state = _mapping(raw.get("State"))
                if state.get("Status") != "created" or state.get("Running") is not False:
                    raise DockerBackendFault("docker_policy_mismatch")
                return handle
            except Exception as error:
                self._quarantined = True
                if intent is not None:
                    if (
                        isinstance(error, DockerBackendFault)
                        and error.observed_container_id is not None
                    ):
                        observed_container_id = error.observed_container_id
                    self._unknown(intent, cleanup_deadline, handle, observed_container_id)
                if handle is not None:
                    self._cleanup(self._sessions[handle.container_id], cleanup_deadline)
                if isinstance(error, DockerBackendFault):
                    raise
                raise DockerBackendFault("docker_mutation_unconfirmed") from None

    def _cleanup(self, session: _Session, deadline: float) -> bool:
        handle, intent = session.handle, session.handle.intent
        if session.removed:
            return True  # Confirmed prior removal is a fact, never a repeated mutation.
        try:
            if session.remove_attempted:
                self._engine(intent, deadline)
                if not self.backend.absent(
                    intent.target.endpoint, handle.container_id, deadline=deadline
                ):
                    raise DockerBackendFault("docker_cleanup_unconfirmed")
                self._engine(intent, deadline)
                session.removed = True
                self._record(intent, "cleaned", deadline=deadline, handle=handle)
                return True
            raw = self._read(intent, handle.container_id, deadline, handle.created_at)
            state = _mapping(raw.get("State"))
            if state.get("Running") is True:
                if not session.stop_attempted:
                    session.stop_attempted = True
                    try:
                        self.backend.stop(
                            intent.target.endpoint, handle.container_id, deadline=deadline
                        )
                    except Exception:
                        self._unknown(intent, deadline, handle)
                raw = self._read(intent, handle.container_id, deadline, handle.created_at)
                if _mapping(raw.get("State")).get("Running") is True:
                    if not session.kill_attempted:
                        session.kill_attempted = True
                        try:
                            self.backend.kill(
                                intent.target.endpoint, handle.container_id, deadline=deadline
                            )
                        except Exception:
                            self._unknown(intent, deadline, handle)
                    raw = self._read(intent, handle.container_id, deadline, handle.created_at)
            if _mapping(raw.get("State")).get("Running") is not False:
                raise DockerBackendFault("docker_cleanup_unconfirmed")
            self._engine(intent, deadline)
            session.remove_attempted = True
            self.backend.remove(intent.target.endpoint, handle.container_id, deadline=deadline)
            if not self.backend.absent(
                intent.target.endpoint, handle.container_id, deadline=deadline
            ):
                raise DockerBackendFault("docker_cleanup_unconfirmed")
            self._engine(intent, deadline)
            session.removed = True
            try:
                self._record(intent, "cleaned", deadline=deadline, handle=handle)
            except DockerBackendFault:
                pass  # Physical cleanup known; durable acknowledgement still unknown/quarantined.
            return True
        except Exception:
            self._unknown(intent, deadline, handle)
            return False

    def terminate_remove_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> bool:
        with self._operation():
            return self._cleanup(self._session(handle), deadline)

    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerJobOutcome:
        with self._operation():
            deadline = min(
                deadline, time.monotonic() + handle.intent.record.manifest.profile.timeout_seconds
            )
            cleanup_deadline = min(cleanup_deadline, deadline + 10)
            self._budgets(deadline, cleanup_deadline)
            session = self._session(handle)
            if session.start_attempted or session.removed or self._quarantined:
                raise DockerBackendFault("docker_authority_required")
            session.start_attempted = (
                True  # Before journal/transport: uncertainty never reopens start.
            )
            exit_code: int | None = None
            timed_out = unknown = False
            try:
                if (
                    type(sink.retention_limit) is not int
                    or sink.retention_limit != handle.intent.record.manifest.profile.output_bytes
                ):
                    raise DockerBackendFault("docker_unsafe_configuration")
                target = self.verifier.verify_target(
                    DockerTargetDeclaration.model_validate(self._declaration(handle)),
                    deadline=deadline,
                )
                if target.fingerprint != handle.intent.target.fingerprint:
                    raise DockerBackendFault("docker_target_mismatch")
                self.inputs.check(
                    handle.intent.inputs, handle.intent.record.manifest, deadline=deadline
                )
                raw = self._read(handle.intent, handle.container_id, deadline, handle.created_at)
                self._safe_config(handle, raw)
                before_start = _mapping(raw.get("State"))
                if (
                    before_start.get("Status") != "created"
                    or before_start.get("Running") is not False
                ):
                    raise DockerBackendFault("docker_policy_mismatch")
                self._record(handle.intent, "start_intent", deadline=deadline, handle=handle)
                transport_status = self.backend.attach(
                    handle.intent.target.endpoint, handle.container_id, sink, deadline=deadline
                )
                raw = self._read(handle.intent, handle.container_id, deadline, handle.created_at)
                state = _mapping(raw.get("State"))
                code = state.get("ExitCode")
                if (
                    state.get("Status") != "exited"
                    or state.get("Running") is not False
                    or type(code) is not int
                    or not 0 <= code <= 255
                ):
                    raise DockerBackendFault("docker_mutation_unconfirmed")
                exit_code = code
                self._record(
                    handle.intent, "exited", deadline=deadline, handle=handle, exit_code=code
                )
                if type(transport_status) is not int or transport_status not in (0, code):
                    unknown = (
                        True  # Guest exit observed, but attached output/transport is unconfirmed.
                    )
                    self._unknown(handle.intent, cleanup_deadline, handle)
            except Exception as error:
                unknown = True
                timed_out = isinstance(error, DockerBackendFault) and error.code == "docker_timeout"
                self._unknown(handle.intent, cleanup_deadline, handle)
            cleaned = self._cleanup(session, cleanup_deadline)
            return DockerJobOutcome(
                exit_code=exit_code,
                timed_out=timed_out,
                outcome_unknown=unknown or self._journal_lost or not cleaned,
                cleanup_verified=cleaned,
            )

    def _declaration(self, handle: DockerOwnedContainer) -> dict[str, object]:
        return self._declarations[handle.intent.attempt_id].model_dump(mode="python")
