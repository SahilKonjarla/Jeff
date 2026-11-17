"""Controller metadata seam only; fake responses never establish actual isolation."""

import copy
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    DockerBackendFault,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionProfile,
)
from jeff.infrastructure.docker_cli import DockerTargetInspector


class Metadata:
    def __init__(self) -> None:
        self.identity = DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="a" * 64, size=100)
        self.queries: list[str] = []
        self.engine = {
            "ID": "synthetic-engine",
            "OSType": "linux",
            "Architecture": "aarch64",
            "OperatingSystem": "Docker Desktop",
            "ServerVersion": "29.9.3",
            "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
            "MemoryLimit": True,
            "PidsLimit": True,
            "CPUCfsPeriod": True,
            "CPUCfsQuota": True,
        }
        self.image = {
            "ID": "sha256:" + "1" * 64,
            "OS": "linux",
            "Architecture": "arm64",
            "Variant": "",
            "Config": {
                "Env": ["LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin"],
                "User": "",
                "Entrypoint": None,
                "Cmd": ["python3"],
                "WorkingDir": "",
                "Volumes": None,
                "Healthcheck": None,
                "OnBuild": None,
            },
        }

    def check(self) -> None:
        pass

    def read(self, query: str, *, endpoint: str, image_id: str | None, deadline: float) -> bytes:
        self.queries.append(query)
        assert endpoint == "unix:///synthetic/docker.sock" and deadline > time.monotonic()
        assert image_id == ("sha256:" + "1" * 64 if query == "image.inspect" else None)
        value: object = (
            "29.9.3"
            if query == "client.version"
            else self.image
            if query == "image.inspect"
            else self.engine
        )
        return json.dumps(value).encode()


def test_stop_during_verification_prevents_remaining_metadata_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import (
        AuthorityRecord,
        ExecutionManifest,
        ExecutionStopSignal,
        PreparedInputs,
    )
    from jeff.domain.execution import AuthorityState

    declared = declaration()
    manifest = ExecutionManifest(
        profile=declared.profile, argv=(declared.profile.executable, "-V"), inputs=PreparedInputs()
    )
    record = AuthorityRecord(
        proposal_id=uuid4(),
        approval_id=uuid4(),
        job_id=uuid4(),
        project_id=declared.profile.project_id,
        canonical_root=declared.profile.canonical_root,
        owner_instance_id=uuid4(),
        manifest=manifest,
        manifest_digest=manifest.fingerprint,
        state=AuthorityState.APPROVED,
        created_at_ms=100,
        expires_at_ms=600100,
    )
    stop = ExecutionStopSignal(record, job_deadline=time.monotonic() + 30)
    reader = Metadata()
    read = reader.read

    def interrupted(query: str, *, endpoint: str, image_id: str | None, deadline: float) -> bytes:
        result = read(query, endpoint=endpoint, image_id=image_id, deadline=deadline)
        stop.request(uuid4(), "cancel")
        return result

    monkeypatch.setattr(reader, "read", interrupted)
    with pytest.raises(DockerBackendFault) as cancelled:
        DockerTargetInspector(_reader=reader).verify_target_cancellable(
            declared, stop, deadline=time.monotonic() + 30
        )
    assert cancelled.value.code == "docker_cancelled"
    assert reader.queries == ["client.version"]


def declaration() -> DockerTargetDeclaration:
    profile = ExecutionProfile(
        name="python",
        project_id=uuid4(),
        canonical_root=Path("/synthetic/project"),
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
        public_environment=(("LANG", "C"),),
    )
    return DockerTargetDeclaration(
        profile=profile,
        image_environment=(("LANG", "C.UTF-8"), ("PATH", "/usr/local/bin:/usr/bin:/bin")),
        image_command=("python3",),
    )


def test_declared_target_is_verified_without_granting_isolation_or_launch_authority() -> None:
    reader = Metadata()
    declared = declaration()
    result = DockerTargetInspector(_reader=reader).verify_target(
        declared, deadline=time.monotonic() + 10
    )
    assert reader.queries == ["client.version", "engine.info", "image.inspect", "engine.info"]
    assert result.engine_id == declared.profile.engine_id
    assert result.image_id == declared.profile.image_id and result.platform == "linux/arm64"
    assert result.declaration_digest == declared.fingerprint
    assert result.effective_environment == (("LANG", "C"), ("PATH", "/usr/local/bin:/usr/bin:/bin"))
    assert result.binary.sha256 == "a" * 64 and len(result.fingerprint) == 64
    assert result.isolation_verified is False and result.inputs_verified is False
    assert result.launch_authority_issued is False


def test_changed_target_unsupported_features_and_implicit_configuration_refuse() -> None:
    reader = Metadata()
    reader.engine["ID"] = "other-engine"
    with pytest.raises(DockerBackendFault) as changed:
        DockerTargetInspector(_reader=reader).verify_target(
            declaration(), deadline=time.monotonic() + 10
        )
    assert changed.value.code == "docker_target_mismatch"
    assert reader.queries == ["client.version", "engine.info"]
    unsupported = Metadata()
    unsupported.engine["MemoryLimit"] = False
    with pytest.raises(DockerBackendFault) as missing:
        DockerTargetInspector(_reader=unsupported).verify_target(
            declaration(), deadline=time.monotonic() + 10
        )
    assert missing.value.code == "docker_unsupported"
    implicit = Metadata()
    config = copy.deepcopy(implicit.image["Config"])
    assert isinstance(config, dict)
    config["Volumes"] = {"/implicit": {}}
    implicit.image["Config"] = config
    with pytest.raises(DockerBackendFault) as volume:
        DockerTargetInspector(_reader=implicit).verify_target(
            declaration(), deadline=time.monotonic() + 10
        )
    assert volume.value.code == "docker_unsupported"
    wrong_environment = declaration().model_copy(update={"image_environment": ()})
    with pytest.raises(DockerBackendFault) as environment:
        DockerTargetInspector(_reader=Metadata()).verify_target(
            wrong_environment, deadline=time.monotonic() + 10
        )
    assert environment.value.code == "docker_target_mismatch"


def test_ambiguous_metadata_never_leaks_raw_payload_and_sensitive_declarations_refuse() -> None:
    class BrokenMetadata(Metadata):
        payload = b""

        def read(
            self, query: str, *, endpoint: str, image_id: str | None, deadline: float
        ) -> bytes:
            return self.payload

    for payload in (
        b'"29.9.3"' * 40000,
        b'{"x":1,"x":2}',
        b"NaN",
        b'"\\xff"',
        b'{"raw":"SYNTHETIC_PRIVATE"}',
    ):
        reader = BrokenMetadata()
        reader.payload = payload
        with pytest.raises(DockerBackendFault) as invalid:
            DockerTargetInspector(_reader=reader).verify_target(
                declaration(), deadline=time.monotonic() + 10
            )
        assert "SYNTHETIC_PRIVATE" not in str(invalid.value)
        assert invalid.value.code in {
            "docker_protocol_error",
            "docker_output_limit",
            "docker_unsupported",
        }
    with pytest.raises(ValidationError):
        DockerTargetDeclaration(
            profile=declaration().profile, image_environment=(("API_TOKEN", "synthetic"),)
        )
    with pytest.raises(ValidationError):
        DockerTargetDeclaration(
            profile=declaration().profile, image_environment=(("LANG", "C"), ("LANG", "C"))
        )


def test_expired_verification_refuses_before_any_metadata_command() -> None:
    reader = Metadata()
    with pytest.raises(DockerBackendFault) as expired:
        DockerTargetInspector(_reader=reader).verify_target(
            declaration(), deadline=time.monotonic() - 1
        )
    assert expired.value.code == "docker_timeout" and reader.queries == []


def test_private_cli_configuration_refuses_shared_parents_and_credential_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Configuration checks must not start a child.")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    binary = tmp_path / "docker"
    binary.write_bytes(b"synthetic executable, never invoked")
    binary.chmod(0o700)
    config = tmp_path / "cli"
    config.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    configuration = config / "config.json"
    configuration.write_bytes(b"{}")
    configuration.chmod(0o600)
    inspector = DockerTargetInspector(binary, config, home=home)
    with pytest.raises(DockerBackendFault) as expired:
        inspector.verify_target(declaration(), deadline=time.monotonic() - 1)
    assert expired.value.code == "docker_timeout"
    configuration.write_bytes(b'{"credsStore":"synthetic-not-a-real-helper"}')
    with pytest.raises(DockerBackendFault) as credentials:
        DockerTargetInspector(binary, config, home=home)
    assert credentials.value.code == "docker_unsafe_configuration"
    configuration.write_bytes(b"{}")
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    moved = shared / "docker"
    moved.write_bytes(binary.read_bytes())
    moved.chmod(0o700)
    with pytest.raises(DockerBackendFault) as ancestry:
        DockerTargetInspector(moved, config, home=home)
    assert ancestry.value.code == "docker_unsafe_configuration"


def test_cli_failure_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real capture pipes/fake child: sanitized diagnostics through the public verifier."""
    binary = tmp_path / "docker"
    binary.write_bytes(b"synthetic, never executed")
    binary.chmod(0o700)
    config = tmp_path / "cli"
    config.mkdir(mode=0o700)
    (config / "config.json").write_bytes(b"{}")
    (config / "config.json").chmod(0o600)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    socket_path = tmp_path / "s"
    endpoint = "unix://" + str(socket_path)
    declared = declaration()
    declared = declared.model_copy(
        update={"profile": declared.profile.model_copy(update={"endpoint": endpoint})}
    )
    mode = "template_failure"
    marker = b"SYNTHETIC_PRIVATE_NOT_FOR_REPORTS"
    diagnostic = b"template: can't evaluate field Variant in type image.InspectResponse " + marker

    class Child:
        def __init__(self, output: bytes, error: bytes, code: int) -> None:
            self.returncode = code
            read, write = os.pipe()
            os.write(write, output)
            os.close(write)
            self.stdout = os.fdopen(read, "rb")
            read, write = os.pipe()
            os.write(write, error)
            os.close(write)
            self.stderr = os.fdopen(read, "rb")

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float) -> int:
            return self.returncode

        def terminate(self) -> NoReturn:
            raise AssertionError("No actual/live child exists in this test.")

        def kill(self) -> NoReturn:
            raise AssertionError("No actual/live child exists in this test.")

    def spawn(argv: list[str], **kwargs: Any) -> Child:
        assert kwargs["env"]["HOME"] == str(home)
        assert kwargs["env"]["DOCKER_CONFIG"] == str(config)
        assert "DOCKER_CONTEXT" not in kwargs["env"] and kwargs["close_fds"] is True
        assert argv[1:5] == ["--config", str(config), "--host", endpoint]
        if argv[5] == "version":
            return Child(b'"29.9.3"', b"", 0)
        if argv[5] == "info":
            (home / "Library").mkdir(exist_ok=True)
            assert sorted(p.name for p in config.iterdir()) == ["config.json"]
            return Child(json.dumps(Metadata().engine).encode(), b"", 0)
        assert argv[5:7] == ["image", "inspect"]
        if mode == "template_failure":
            return Child(b"", diagnostic, 1)
        if mode == "supported_raw_config":
            if any(".Config.User" in value for value in argv):
                return Child(b"", diagnostic.replace(b"Variant", b"User"), 1)
            image = Metadata().image
            raw_config = copy.deepcopy(image["Config"])
            assert isinstance(raw_config, dict)
            raw_config.pop("User")  # Omitted OCI defaults have their specified empty meaning.
            raw_config.pop("WorkingDir")
            raw_config.update(
                Labels={}, ExposedPorts={}, ArgsEscaped=False, StopSignal="", Shell=[]
            )
            image["Config"] = raw_config
            return Child(json.dumps(image).encode(), b"", 0)
        return Child(b"{}", marker, 0)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setenv("DOCKER_CONTEXT", "synthetic-untrusted-context")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        inspector = DockerTargetInspector(binary, config, home=home)
        with pytest.raises(DockerBackendFault) as failed:
            inspector.verify_target(declared, deadline=time.monotonic() + 10)
        assert failed.value.code == "docker_command_failed"
        facts = failed.value.diagnostics
        assert facts is not None
        assert facts.returncode == 1 and facts.stdout_bytes == 0
        assert facts.stderr_bytes == len(diagnostic)
        assert facts.category == "template_field_unavailable" and facts.field_name == "Variant"
        assert marker.decode() not in facts.model_dump_json() + str(failed.value)
        mode = "stderr_on_success"
        with pytest.raises(DockerBackendFault) as warned:
            inspector.verify_target(declared, deadline=time.monotonic() + 10)
        warning = warned.value.diagnostics
        assert warning is not None and warning.returncode == 0
        assert warning.category == "stderr_on_success" and warning.field_name is None
        assert warning.stdout_bytes == 2 and warning.stderr_bytes == len(marker)
        assert marker.decode() not in warning.model_dump_json() + str(warned.value)
        mode = "supported_raw_config"
        verified = inspector.verify_target(declared, deadline=time.monotonic() + 10)
        assert verified.engine_id == declared.profile.engine_id
        assert verified.image_id == declared.profile.image_id
        assert verified.launch_authority_issued is False and verified.isolation_verified is False
        assert (home / "Library").is_dir()
        assert sorted(p.name for p in config.iterdir()) == ["config.json"]
        with pytest.raises(DockerBackendFault) as reused_home:
            DockerTargetInspector(binary, config, home=home)
        assert reused_home.value.code == "docker_unsafe_configuration"
        (config / "unexpected").mkdir()
        with pytest.raises(DockerBackendFault) as changed_config:
            inspector.verify_target(declared, deadline=time.monotonic() + 10)
        assert changed_config.value.code == "docker_unsafe_configuration"
        with pytest.raises(DockerBackendFault) as shared_home:
            DockerTargetInspector(binary, config, home=config)
        assert shared_home.value.code == "docker_unsafe_configuration"
        unsafe = Metadata()
        unsafe_config = copy.deepcopy(unsafe.image["Config"])
        assert isinstance(unsafe_config, dict)
        unsafe_config["Labels"] = {"unexpected": "synthetic"}
        unsafe.image["Config"] = unsafe_config
        with pytest.raises(DockerBackendFault):
            DockerTargetInspector(_reader=unsafe).verify_target(
                declaration(), deadline=time.monotonic() + 10
            )
