"""Characterize Docker CLI query grammar, metadata and constructor/transport interfaces."""

import importlib
import inspect
import json
import subprocess
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Never, get_args, get_type_hints

import pytest
from pydantic import BaseModel

from jeff.domain.execution import fingerprint
from jeff.infrastructure import docker_cli as native

SCHEMA_DIGEST = "a0c99fd5ad37288c81757e6f9b369e72be6da1d8df43b450589a05bc4cb53084"
GRAMMAR_DIGEST = "b079bbb887c9d128045b8d1e719e8399e8da6feecef49f60c10edd938a05b478"
METADATA_DIGEST = "c8f1b385aaecc546b025ec4231f5cdf39765e0afadbab2263cefbd2bb61c5b5e"
SIGNATURE_DIGEST = "64e0630eb223e5705479e9da40829f6306c81cef3d8d28ecb4787c9e6f9801d9"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schemas() -> dict[str, object]:
    return {
        name: value.model_json_schema()
        for name, value in vars(native).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__.startswith("jeff.infrastructure.docker_cli")
    }


def _grammar() -> dict[str, object]:
    return {
        "queries": list(get_args(native.Query)),
        "limit": native._LIMIT,
        "engine": native._ENGINE_FORMAT,
        "image": native._IMAGE_FORMAT,
    }


def _metadata() -> dict[str, str]:
    engine = native._Engine.model_validate(
        native._json(
            json.dumps(
                {
                    "ID": "synthetic-engine",
                    "OSType": "linux",
                    "Architecture": "aarch64",
                    "OperatingSystem": "Docker Desktop",
                    "ServerVersion": "29.0.0",
                    "SecurityOptions": ["name=seccomp,profile=builtin"],
                    "MemoryLimit": True,
                    "PidsLimit": True,
                    "CPUCfsPeriod": True,
                    "CPUCfsQuota": True,
                }
            ).encode()
        )
    )
    image = native._Image.model_validate(
        native._json(
            json.dumps(
                {
                    "ID": "sha256:" + "1" * 64,
                    "OS": "linux",
                    "Architecture": "arm64",
                    "Variant": "v8",
                    "Config": {"Env": ["LANG=C.UTF-8"], "Cmd": ["python3"]},
                }
            ).encode()
        )
    )
    return {
        "engine": engine.model_dump_json(),
        "image": image.model_dump_json(),
        "image_configuration_digest": fingerprint(image.Config.model_dump()),
        "failure": native._failure_facts(
            1, 0, 30, b"can't evaluate field Config "
        ).model_dump_json(),
    }


def _signatures() -> dict[str, str]:
    result = {}
    aliases = ("_Reader", "_DockerCLI", "DockerLifecycleCLI", "DockerTargetInspector")
    for name in aliases:
        cls = getattr(native, name)
        for method_name, value in vars(cls).items():
            if isinstance(value, property):
                value = value.fget
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if not inspect.isfunction(value):
                continue
            signature = str(inspect.signature(value))
            for alias in aliases:
                annotation = getattr(native, alias)
                signature = signature.replace(
                    f"{annotation.__module__}.{annotation.__qualname__}",
                    f"jeff.infrastructure.docker_cli.{alias}",
                )
            result[f"{name}.{method_name}"] = signature
            get_type_hints(value)
    return result


def test_frozen_metadata_schemas_preserve_original_contracts() -> None:
    assert _digest(_schemas()) == SCHEMA_DIGEST


def test_metadata_query_templates_and_limits_stay_identical() -> None:
    assert _digest(_grammar()) == GRAMMAR_DIGEST


def test_metadata_serialization_fingerprints_and_failure_facts_stay_identical() -> None:
    assert _digest(_metadata()) == METADATA_DIGEST


def test_only_guard_attachment_and_owned_capture_state_extend_interfaces() -> None:
    signatures = _signatures()
    key = "_DockerCLI._capture"
    before, separator, extension = signatures[key].partition(", log_sink: ")
    assert separator and extension == (
        "collections.abc.Callable[[typing.Literal['stdout', 'stderr'], bytes], None]"
        " | None = None) -> bytes"
    )
    signatures[key] = before + ") -> bytes"
    assert _digest(signatures) == "0176a0f55bc745851150fc93f38251e5151836f180d817bf8de80d6d944eda62"
    additions = {
        "DockerLifecycleCLI.__init__",
        "DockerLifecycleCLI.attach_guarded",
        "DockerLifecycleCLI.release_guarded",
    }
    assert additions <= signatures.keys()
    assert _digest({k: v for k, v in signatures.items() if k not in additions}) == SIGNATURE_DIGEST


def test_namespace_subprocess_hook_reaches_both_captured_process_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("No real Docker command is approved")

    assert native.subprocess is subprocess
    monkeypatch.setattr(native.subprocess, "Popen", forbidden)
    for operation in (native._DockerCLI._capture, native.DockerLifecycleCLI._attach):
        module = importlib.import_module(operation.__module__)
        assert module.subprocess is native.subprocess and module.subprocess.Popen is forbidden


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name
