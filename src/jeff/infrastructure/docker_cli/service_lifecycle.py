"""Fixed single-fixture service operations, composed only by an authorized worker."""

import hashlib
import re
from collections.abc import Callable
from typing import cast
from uuid import UUID

from jeff.application.compose_lifecycle import (
    ResourceIdentity,
    ServiceAction,
    ServiceAttempt,
    ServiceResources,
    ServiceRestart,
    ServiceStart,
    ServiceStop,
    ServiceTarget,
)
from jeff.application.execution import DockerBackendFault
from jeff.domain.execution import fingerprint

from .metadata import _json
from .service_health import _no_bindings
from .transport import _DockerCLI

_JOB = "org.jeff.service.start_job"
_REVIEW = "org.jeff.service.start_manifest"
_PYTHON = "/usr/local/bin/python"
_USER = "65534:65534"
Query = Callable[..., bytes]
_CONTAINER = (
    '[{{json .Id}},{{json .Created}},{{json .Image}},{{json .State.Status}},'
    '{{json (index .Config.Labels "org.jeff.service.start_job")}},'
    '{{json (index .Config.Labels "org.jeff.service.start_manifest")}},'
    '{{json .HostConfig.PublishAllPorts}},{{json .HostConfig.PortBindings}},'
    '{{json .NetworkSettings.Ports}},{{json .HostConfig.NetworkMode}},'
    '[{{range .NetworkSettings.Networks}}{{json .NetworkID}},{{end}}null],'
    '{{json .HostConfig.Memory}},{{json .HostConfig.NanoCpus}},{{json .HostConfig.PidsLimit}},'
    '{{json .Config.User}},{{json .HostConfig.Privileged}},{{json .HostConfig.ReadonlyRootfs}},'
    '{{json .HostConfig.CapDrop}},{{json .HostConfig.SecurityOpt}},{{json .Mounts}},'
    '{{json .HostConfig.MemorySwap}},{{json .HostConfig.RestartPolicy.Name}},'
    '{{json .Path}},{{json .Args}}]'
)
_NETWORK = (
    '[{{json .Id}},{{json .Created}},{{json .Internal}},{{json .Driver}},'
    '{{json (index .Labels "org.jeff.service.start_job")}},'
    '{{json (index .Labels "org.jeff.service.start_manifest")}},'
    '[{{range $id, $entry := .Containers}}{{json $id}},{{end}}null]]'
)


def _id(data: bytes) -> str:
    if re.fullmatch(rb"[0-9a-f]{64}\n?", data) is None:
        raise DockerBackendFault("docker_mutation_unconfirmed")
    return data.strip().decode("ascii")


def _row(data: bytes, length: int) -> list[object]:
    try:
        value = _json(data)
        if isinstance(value, list) and len(value) == length:
            return value
    except (ValueError, RecursionError):
        pass
    raise DockerBackendFault("docker_ownership_unconfirmed")


def _resource(fields: list[object], expected: str) -> ResourceIdentity:
    if fields[0] != expected:
        raise DockerBackendFault("docker_ownership_unconfirmed")
    try:
        return ResourceIdentity.model_validate({"id": expected, "created_at": fields[1]})
    except ValueError:
        raise DockerBackendFault("docker_ownership_unconfirmed") from None


def _name(target: ServiceTarget) -> str:
    return f"jeff-{target.project_id.hex}-{target.service}"


class DockerFixtureService:
    """One reviewed source payload, no mutable project mount or Compose execution.

    Bootstrap must supply trusted bounded fixture bytes and a pinned transport.
    The calling worker owns admission and journaling; this adapter is not wired by default.
    """

    def __init__(
        self, cli: _DockerCLI, target: ServiceTarget, source: bytes, *,
        configured_start: ServiceStart | None = None,
    ) -> None:
        if target.project != "sandbox-project" or target.service != "api" or not 0 < len(source) <= 8192:
            raise DockerBackendFault("docker_unsafe_configuration")
        try:
            self._source = source.decode("utf-8", "strict")
        except UnicodeError:
            raise DockerBackendFault("docker_unsafe_configuration") from None
        if "\0" in self._source:
            raise DockerBackendFault("docker_unsafe_configuration")
        self._cli, self._target = cli, target
        self._configured_start = configured_start
        self.configuration_digest = fingerprint(
            {"fixture": "m05_api_v1", "source_sha256": hashlib.sha256(source).hexdigest(),
             "docker_cli": cli.identity.model_dump(mode="json")}
        )

        if configured_start is not None and (
            configured_start.target != target
            or configured_start.configuration_digest != self.configuration_digest
        ):
            raise DockerBackendFault("docker_unsafe_configuration")

    def _query(self, manifest: ServiceAction, deadline: float) -> Query:
        if manifest.target != self._target:
            raise DockerBackendFault("docker_target_mismatch")
        endpoint = manifest.target.endpoint
        socket_id = self._cli._socket(endpoint)

        def query(*args: str) -> bytes:
            if self._cli._socket(endpoint) != socket_id:
                raise DockerBackendFault("docker_target_mismatch")
            data = self._cli._control(args, endpoint=endpoint, deadline=deadline)
            if self._cli._socket(endpoint) != socket_id:
                raise DockerBackendFault("docker_target_mismatch")
            return data

        if _json(query("info", "--format", "{{json .ID}}")) != manifest.target.engine_id:
            raise DockerBackendFault("docker_target_mismatch")
        return query

    def _verify_start(self, start: ServiceStart, query: Query, *, check_names: bool) -> None:
        if start.configuration_digest != self.configuration_digest or (
            self._configured_start is not None and start != self._configured_start
        ):
            raise DockerBackendFault("docker_target_mismatch")
        image = _row(query(
            "image", "inspect", "--format",
            '[{{json .Id}},{{json .Os}},{{json .Architecture}},'
            '{{json (index .Config "Volumes")}}]',
            "--", start.image_id,
        ), 4)
        if image[:3] != [start.image_id, *start.platform.split("/")] or image[3] not in (None, {}):
            raise DockerBackendFault("docker_target_mismatch")
        if check_names:
            name = _name(start.target)
            if any(query(*args).strip() for args in (
                ("network", "ls", "--quiet", "--filter", f"name=^{name}$"),
                ("container", "ls", "--all", "--quiet", "--filter", f"name=^/{name}$"),
                ("container", "ls", "--all", "--quiet", "--filter",
                 f"label=com.docker.compose.project={start.target.project}"),
            )):
                raise DockerBackendFault("docker_ownership_unconfirmed")

    def verify(self, manifest: ServiceAction, *, deadline: float) -> str:
        query = self._query(manifest, deadline)
        if isinstance(manifest, ServiceStart):
            self._verify_start(manifest, query, check_names=True)
        else:
            self._verify_resources(query, manifest, manifest.resources, "running")
        return manifest.digest

    @staticmethod
    def _action(attempt: ServiceAttempt, kind: type[ServiceStart] | type[ServiceStop] | type[ServiceRestart]) -> ServiceAction:
        if not isinstance(attempt.manifest, kind) or not attempt.matches_review():
            raise DockerBackendFault("docker_authority_required")
        return attempt.manifest

    def create(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        start = cast(ServiceStart, self._action(attempt, ServiceStart))
        query = self._query(start, deadline)
        self._verify_start(start, query, check_names=True)
        name = _name(start.target)
        labels = ("--label", f"{_JOB}={attempt.job_id}", "--label", f"{_REVIEW}={start.digest}")
        nid = _id(query("network", "create", "--driver", "bridge", "--internal", *labels, name))
        network = self._network(query, nid, attempt.job_id, start.digest, (None,))
        whole, fractional = divmod(start.nano_cpus, 1_000_000_000)
        cid = _id(query(
            "container", "create", "--pull=never", "--name", name, *labels,
            "--label", f"com.docker.compose.project={start.target.project}",
            "--label", f"com.docker.compose.service={start.target.service}",
            "--label", f"com.docker.compose.project.working_dir={start.target.root}",
            "--label", f"com.docker.compose.project.config_files={start.target.root / 'compose.yaml'}",
            "--label", "com.docker.compose.oneoff=False",
            "--network", nid, "--user", _USER, "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true", "--restart=no", "--no-healthcheck",
            "--memory", str(start.memory_bytes), "--memory-swap", str(start.memory_bytes),
            "--cpus", f"{whole}.{fractional:09d}",
            "--pids-limit", str(start.pids_limit), "--entrypoint", _PYTHON, "--", start.image_id,
            *self._argv(),
        ))
        observed = _row(query("container", "inspect", "--format", _CONTAINER, "--", cid), 24)
        resources = ServiceResources(container=_resource(observed, cid), network=network)
        self._verify_resources(query, start, resources, "created", job=attempt.job_id, row=observed)
        return resources

    def _argv(self) -> tuple[str, ...]:
        return ("-I", "-B", "-c", self._source, "--host", "0.0.0.0", "--port", "8000")

    def start(
        self, attempt: ServiceAttempt, resources: ServiceResources, *, deadline: float
    ) -> ServiceResources:
        start = cast(ServiceStart, self._action(attempt, ServiceStart))
        query = self._query(start, deadline)
        self._verify_start(start, query, check_names=False)
        self._verify_resources(query, start, resources, "created", job=attempt.job_id)
        query("container", "start", "--", resources.container.id)
        self._verify_resources(query, start, resources, "running", job=attempt.job_id)
        return resources

    def restart(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        action = cast(ServiceRestart, self._action(attempt, ServiceRestart))
        query = self._query(action, deadline)
        self._verify_resources(query, action, action.resources, "running")
        query("container", "restart", "--time", "3", "--", action.resources.container.id)
        self._verify_resources(query, action, action.resources, "running")
        return action.resources

    def stop(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
        stop = cast(ServiceStop, self._action(attempt, ServiceStop))
        query = self._query(stop, deadline)
        self._verify_resources(query, stop, stop.resources, "running")
        query("container", "stop", "--time", "3", "--", stop.resources.container.id)
        self._verify_resources(query, stop, stop.resources, "exited")
        return stop.resources

    @staticmethod
    def _network(
        query: Query, nid: str, job: UUID, digest: str, members: tuple[str | None, ...]
    ) -> ResourceIdentity:
        row = _row(query("network", "inspect", "--format", _NETWORK, "--", nid), 7)
        if row[2] is not True or row[3:] != ["bridge", str(job), digest, list(members)]:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return _resource(row, nid)

    def _verify_resources(
        self, query: Query, action: ServiceAction, resources: ServiceResources, state: str,
        *, job: UUID | None = None, row: list[object] | None = None,
    ) -> None:
        cid, nid = resources.container.id, resources.network.id
        if not all(re.fullmatch(r"[a-f0-9]{64}", value) for value in (cid, nid)):
            raise DockerBackendFault("docker_ownership_unconfirmed")
        if isinstance(action, (ServiceStop, ServiceRestart)):
            job, digest = action.start_job_id, action.start_manifest_digest
        else:
            digest = action.digest
        if job is None:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        if row is None:
            row = _row(query("container", "inspect", "--format", _CONTAINER, "--", cid), 24)
        membership = row[10]
        allowed_membership = (
            ([nid, None], [None, None], ["", None], [None]) if state != "running" else ([nid, None],)
        )
        if (
            _resource(row, cid) != resources.container
            or row[3:6] != [state, str(job), digest]
            or row[6] is not False or row[7] not in (None, {}) or not _no_bindings(row[8])
            or row[9] != nid or membership not in allowed_membership
            or row[14] != _USER or row[15] is not False or row[16] is not True
            or row[17] != ["ALL"] or row[18] != ["no-new-privileges=true"]
            or row[19] not in (None, []) or row[21] not in ("", "no")
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")
        policy = action.original if isinstance(action, ServiceRestart) else action
        if isinstance(policy, ServiceStart) and (
            policy.configuration_digest != self.configuration_digest
            or row[2] != policy.image_id
            or row[11:14] != [policy.memory_bytes, policy.nano_cpus, policy.pids_limit]
            or row[20] != policy.memory_bytes
            or row[22:] != [_PYTHON, list(self._argv())]
        ):
            raise DockerBackendFault("docker_policy_mismatch")
        members = (cid, None) if state == "running" else (None,)
        if self._network(query, nid, job, digest, members) != resources.network:
            raise DockerBackendFault("docker_ownership_unconfirmed")
