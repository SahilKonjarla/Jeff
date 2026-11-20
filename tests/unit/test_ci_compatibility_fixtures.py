"""Pure/mocked CI setup checks: no Git fetch, historical install, or native launch."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import ci_compatibility_fixtures as setup


def test_only_recorded_historical_sources_and_identities_are_selected() -> None:
    assert [(f.variable, f.commit, f.wire, f.database) for f in setup.FIXTURES] == [
        ("JEFF_TEST_V3_PYTHON", "8f1129023631aa8a0ac4041c70bfba2d33ef0297", 3, 2),
        ("JEFF_TEST_B_PYTHON", "930d8a1419a502fc87841ff3f1bb33292628c229", 5, 3),
        ("JEFF_TEST_C_PYTHON", "fd3a53aa985845d806653ed3599e98212c35df90", 5, 3),
    ]
    assert "setuptools==82.0.1 --hash=sha256:" in setup.BUILD_REQUIREMENTS
    assert "wheel==0.48.0 --hash=sha256:" in setup.BUILD_REQUIREMENTS
    assert "packaging==26.3 --hash=sha256:" in setup.BUILD_REQUIREMENTS


def test_constraints_are_pinned_normalized_and_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("# runtime\nPyYAML==6.0.3\n")
    (tmp_path / "requirements-dev.lock").write_text(
        "-r requirements.txt\nPyYAML==6.0.3\nPydantic==2.14.1\n"
    )
    assert setup.constraints(tmp_path) == "pydantic==2.14.1\npyyaml==6.0.3\n"


@pytest.mark.parametrize("requirement", ["pydantic>=2", "-e .", "PyYAML==0", "a==1; x"])
def test_unpinned_or_conflicting_constraints_fail(tmp_path: Path, requirement: str) -> None:
    (tmp_path / "requirements.txt").write_text("PyYAML==6.0.3\n")
    (tmp_path / "requirements-dev.lock").write_text(requirement)
    with pytest.raises(ValueError):
        setup.constraints(tmp_path)


def test_empty_constraints_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("# empty\n")
    (tmp_path / "requirements-dev.lock").write_text("-r requirements.txt\n")
    with pytest.raises(ValueError, match="empty"):
        setup.constraints(tmp_path)


@pytest.mark.parametrize("mutation", [None, "changed", "missing", "extra", "duplicate"])
def test_wheel_requires_exact_source_module_bytes(tmp_path: Path, mutation: str | None) -> None:
    package = tmp_path / "src" / "jeff"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"# pinned source\n")
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if mutation != "missing":
            archive.writestr(
                "jeff/__init__.py", b"changed" if mutation == "changed" else b"# pinned source\n"
            )
        if mutation == "extra":
            archive.writestr("jeff/extra.py", b"extra")
        if mutation == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("jeff/__init__.py", b"# pinned source\n")
    if mutation is None:
        assert setup.wheel_modules(tmp_path, wheel) == {
            "jeff/__init__.py": hashlib.sha256(b"# pinned source\n").hexdigest()
        }
    else:
        with pytest.raises(ValueError):
            setup.wheel_modules(tmp_path, wheel)


@pytest.mark.parametrize("mutation", [None, "wire", "database", "modules"])
def test_installed_probe_identity_is_strict(mutation: str | None) -> None:
    fixture = setup.FIXTURES[0]
    modules = {"jeff/__init__.py": "a" * 64}
    identity: dict[str, object] = {
        "wire": fixture.wire,
        "database": fixture.database,
        "modules": modules,
    }
    if mutation is not None:
        identity[mutation] = 14 if mutation != "modules" else {}
        with pytest.raises(ValueError):
            setup.verify_identity(json.dumps(identity), fixture, modules)
    else:
        setup.verify_identity(json.dumps(identity), fixture, modules)


def test_rebuild_checks_commit_build_flags_wheel_and_installed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = setup.FIXTURES[0]
    commands: list[list[str]] = []
    module = b"# historical fixture, never executed\n"

    def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 90) -> str:
        commands.append(command)
        if "checkout" in command:
            package = cwd / "src" / "jeff"
            package.mkdir(parents=True)
            (package / "__init__.py").write_bytes(module)
        if "rev-parse" in command:
            return fixture.commit
        if "wheel" in command:
            dist = Path(command[command.index("--wheel-dir") + 1])
            with zipfile.ZipFile(dist / "jeff-0.1.0-py3-none-any.whl", "w") as archive:
                archive.writestr("jeff/__init__.py", module)
        if setup.PROBE in command:
            return json.dumps(
                {
                    "wire": 3,
                    "database": 2,
                    "modules": {"jeff/__init__.py": hashlib.sha256(module).hexdigest()},
                }
            )
        return ""

    monkeypatch.setattr(setup, "run", run)
    result = setup.rebuild(fixture, tmp_path, {}, tmp_path / "pins", tmp_path / "build")
    assert result["source_commit"] == fixture.commit and result["wire"] == 3
    assert result["wheel_sha256"] == setup.digest(Path(str(result["wheel"])))
    fetch = next(c for c in commands if "fetch" in c)
    assert fetch[-2:] == [setup.REPOSITORY, fixture.commit]
    assert "--depth=1" in fetch and "--no-recurse-submodules" in fetch
    build = next(c for c in commands if "wheel" in c)
    assert "--no-build-isolation" in build and "--no-deps" in build
    installs = [c for c in commands if "install" in c]
    assert len(installs) == 2 and all("--only-binary=:all:" in c for c in installs)
    assert "--require-hashes" in installs[0] and "--constraint" in installs[1]
    assert all("--isolated" in c for c in installs)


def test_wrong_checkout_is_rejected_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 90) -> str:
        commands.append(command)
        return "wrong" if "rev-parse" in command else ""

    monkeypatch.setattr(setup, "run", run)
    with pytest.raises(ValueError, match="approved commit"):
        setup.rebuild(setup.FIXTURES[0], tmp_path, {}, tmp_path / "pins", tmp_path / "build")
    assert not any("venv" in command for command in commands)


@pytest.fixture
def hosted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "macOS",
        "RUNNER_ARCH": "ARM64",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(tmp_path / "github-env"),
    }.items():
        monkeypatch.setenv(key, value)
    return tmp_path / "compatibility"


@pytest.mark.parametrize("bad", ["GITHUB_ACTIONS", "RUNNER_OS", "RUNNER_ARCH"])
def test_nonhosted_configuration_cannot_provision(
    hosted: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    monkeypatch.setenv(bad, "unapproved")
    with pytest.raises(ValueError, match="hosted macOS ARM64 only"):
        setup.provision(hosted)
    assert not hosted.exists()


@pytest.mark.parametrize("fail", [False, True])
def test_all_fixtures_must_pass_before_any_environment_is_published(
    hosted: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    def rebuild(
        fixture: setup.Fixture,
        root: Path,
        env: dict[str, str],
        dependency_constraints: Path,
        build_requirements: Path,
    ) -> dict[str, object]:
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert dependency_constraints.is_file() and build_requirements.is_file()
        if fail and fixture == setup.FIXTURES[-1]:
            raise ValueError("synthetic final fixture failure")
        return {"variable": fixture.variable, "python": str(root / fixture.variable / "python")}

    monkeypatch.setattr(setup, "rebuild", rebuild)
    envfile = hosted.parent / "github-env"
    if fail:
        with pytest.raises(ValueError, match="synthetic"):
            setup.provision(hosted)
        assert not envfile.exists() and not (hosted / "manifest.json").exists()
    else:
        setup.provision(hosted)
        manifest = json.loads((hosted / "manifest.json").read_text())
        assert manifest["kind"] == "source_pinned_rebuilds_not_original_wheels"
        assert len(manifest["fixtures"]) == 3
        assert envfile.read_text().splitlines() == [
            f"{f.variable}={hosted / f.variable / 'python'}" for f in setup.FIXTURES
        ]


def test_existing_setup_is_never_adopted(hosted: Path) -> None:
    hosted.mkdir()
    with pytest.raises(ValueError, match="fresh direct child"):
        setup.provision(hosted)


def test_symlink_setup_is_never_adopted(hosted: Path) -> None:
    hosted.symlink_to(hosted.parent / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        setup.provision(hosted)
    assert not (hosted.parent / "missing").exists()


def test_non_runner_temp_setup_is_rejected(hosted: Path) -> None:
    with pytest.raises(ValueError, match="fresh direct child"):
        setup.provision(hosted / "nested")
    assert not hosted.exists()


def test_command_errors_do_not_dump_captured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["synthetic"], 1, stdout="PRIVATE", stderr="PRIVATE")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError) as error:
        setup.run(["synthetic"], tmp_path, {})
    assert "PRIVATE" not in str(error.value)
