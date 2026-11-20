"""Git facade, private worker schema/argv and hook compatibility without child launches."""

import inspect
import json
import runpy
import subprocess
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints

import pytest

from jeff.application.history import GitObservation
from jeff.domain.projects import ProjectError
from jeff.integrations import git
from jeff.integrations.git import worker

INTERFACE_DIGEST = "239861d216ec481b33d2bd1477ebc570cf3e5d9170f4718e28ca317b48eadaff"
SCHEMA_DIGEST = "e5d85d6ceaa73be110c15941c8472aa4bfb925a6e30581e13fbd56d91306513a"
POLICY_DIGEST = "18372d77acf7def9780cc2fad9bcfd345839f975ee960281ec1941feeea70661"
PORCELAIN = b"# branch.head main\0# branch.oid (initial)\0? sample.txt\0"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical(value: str) -> str:
    for module, names in (
        (git, ("GitProjectDiscovery", "GitRepositoryRead", "_GitParentGone")),
        (worker, ("OwnedGitRunner", "_Request")),
    ):
        for name in names:
            cls = getattr(module, name)
            value = value.replace(
                f"{cls.__module__}.{cls.__qualname__}", f"{module.__name__}.{name}"
            )
    return value


def _interfaces() -> dict[str, object]:
    result: dict[str, object] = {}
    for module, names in (
        (
            git,
            (
                "GitProjectDiscovery",
                "GitRepositoryRead",
                "_parent_live",
                "_run_git",
                "_run_git_process",
                "_malformed",
                "_path",
                "_parse_status",
            ),
        ),
        (
            worker,
            (
                "OwnedGitRunner",
                "_Request",
                "_pairs",
                "_json",
                "_remaining",
                "_wait",
                "_receive",
                "_send",
                "_arguments",
                "_request",
                "_validate_descriptors",
                "_main",
                "main",
            ),
        ),
    ):
        for name in names:
            value = getattr(module, name)
            members = vars(value).items() if inspect.isclass(value) else [("call", value)]
            for method, function in members:
                if isinstance(function, property):
                    function = function.fget
                if not inspect.isfunction(function):
                    continue
                result[f"{module.__name__}.{name}.{method}"] = {
                    "signature": _canonical(str(inspect.signature(function))),
                    "hints": {
                        key: _canonical(str(hint)) for key, hint in get_type_hints(function).items()
                    },
                    "doc": inspect.getdoc(function),
                }
    return result


def _schema() -> dict[str, object]:
    return worker._Request.model_json_schema()


def _policy() -> dict[str, object]:
    result: dict[str, object] = {}
    for operation in (
        "discovery.root",
        "discovery.private_tracking",
        "filters.names",
        "status.branch",
        "status.changes",
    ):
        drivers = ["alpha", "z_2"] if operation == "status.branch" else []
        arguments = worker._arguments(operation, drivers)
        request = worker._request(Path("/synthetic/repo"), arguments, 1.0)
        result[operation] = {"arguments": arguments, "json": request.model_dump_json()}
    result["limits"] = [
        git.GIT_TIMEOUT,
        git.GIT_OUTPUT_LIMIT,
        git.MAX_CHANGES,
        worker._REQUEST_LIMIT,
        worker._FRAME_LIMIT,
        worker._FILTER,
    ]
    return result


def test_original_interfaces_and_resolved_annotations_are_preserved() -> None:
    assert _digest(_interfaces()) == INTERFACE_DIGEST


def test_private_request_schema_is_preserved() -> None:
    assert _digest(_schema()) == SCHEMA_DIGEST


def test_exact_argv_policy_serialization_and_limits_are_preserved() -> None:
    assert _digest(_policy()) == POLICY_DIGEST


def test_root_runner_hook_remains_live_for_discovery_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        command = args[2]
        body = (
            str(tmp_path).encode() + b"\n"
            if command == "rev-parse"
            else PORCELAIN
            if command == "status"
            else b""
        )
        return subprocess.CompletedProcess([], 0, body, b"")

    monkeypatch.setattr(git, "_run_git", fake)
    assert git.GitProjectDiscovery().find_root(tmp_path) == tmp_path.resolve()
    assert git.GitRepositoryRead().read_branch(tmp_path).unborn
    assert git.GitRepositoryRead().read_changes(tmp_path)[0].path == "sample.txt"
    assert [call[2] for call in calls] == [
        "rev-parse",
        "ls-files",
        "config",
        "status",
        "config",
        "status",
    ]


def test_default_runner_hook_and_observation_still_share_measured_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[GitObservation] = []

    def fake(
        *args: object, measured: list[int], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        measured[:] = [4, 1]
        return subprocess.CompletedProcess([], 0, b"data", b"")

    class Observer:
        def record(self, observation: GitObservation) -> None:
            seen.append(observation)

    monkeypatch.setattr(git, "_run_git_process", fake)
    assert (
        git._run_git(
            "/usr/bin/git",
            Path("/synthetic"),
            "status",
            observer=Observer(),
            category="status.branch",
        ).stdout
        == b"data"
    )
    assert len(seen) == 1
    observation = seen[0]
    assert observation.process_started and observation.output_bytes == 4


@pytest.mark.parametrize(
    "body",
    [b"invalid", b"# branch.head main\0", b"# branch.head main\0# branch.oid (initial)\0? ../x\0"],
)
def test_malformed_porcelain_remains_a_whole_answer_failure(body: bytes) -> None:
    with pytest.raises(ProjectError) as error:
        git._parse_status(body)
    assert error.value.code == "git_invalid_output"


def test_parser_limit_is_resolved_from_root_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "MAX_CHANGES", 0)
    with pytest.raises(ProjectError) as error:
        git._parse_status(PORCELAIN)
    assert error.value.code == "git_output_limit"


def test_worker_main_retains_reassignable_core_and_safe_exit2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_main", lambda: 1)
    assert worker.main() == 1

    def fail() -> int:
        raise OSError("SYNTHETIC_CLOSE_ERROR")

    monkeypatch.setattr(worker, "_main", fail)
    assert worker.main() == 2


def test_worker_package_entrypoint_dispatches_main_without_starting_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "main", lambda: 77)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("jeff.integrations.git.worker", run_name="__main__")
    assert exit_info.value.code == 77


def test_package_size_limit(code_line_count: Callable[[str], int]) -> None:
    for directory in (Path(git.__file__).parent, Path(worker.__file__).parent):
        for path in directory.glob("*.py"):
            assert code_line_count(path.read_text()) <= 400, path.name
