"""Declared build tools stay explicit, checksum-bound and separate from runtime guards."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_guard_toolchain as tools


def declaration(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    binaries = {}
    for role in ("clang", "lld"):
        path = tmp_path / role
        path.write_bytes(role.encode())
        path.chmod(0o700)
        binaries[role] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    data: dict[str, object] = {
        "schema": 1,
        "release": tools.RELEASE,
        "bottles": tools.BOTTLES,
        "tools": binaries,
    }
    manifest = tmp_path / "toolchain.json"
    manifest.write_text(json.dumps(data))
    return manifest, data


def test_explicit_toolchain_does_not_require_reference_mac_paths(tmp_path: Path) -> None:
    manifest, _ = declaration(tmp_path)
    actual = tools.load_toolchain(manifest)
    assert tools.verify_tool(actual, "lld") == tmp_path / "lld"
    assert tools.verify_tool(actual, "clang") == tmp_path / "clang"
    (tmp_path / "lld").write_bytes(b"changed after declaration")
    with pytest.raises(ValueError, match="digest"):
        tools.verify_tool(actual, "lld")


def test_verified_linker_keeps_driver_alias_for_argv0(tmp_path: Path) -> None:
    binary = tmp_path / "lld"
    binary.write_bytes(b"multicall driver")
    binary.chmod(0o700)
    alias = tmp_path / "ld.lld"
    alias.symlink_to(binary)
    declared = {"lld": tools.Tool(alias, tools.digest(binary))}
    assert tools.verify_tool(declared, "lld") == alias


@pytest.mark.parametrize("field", ["release", "bottles", "tools", "extra"])
def test_invalid_declared_provenance_fails_without_fallback(tmp_path: Path, field: str) -> None:
    manifest, data = declaration(tmp_path)
    data[field] = "unapproved"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        tools.load_toolchain(manifest)


def test_absent_explicit_manifest_is_not_reference_fallback(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tools.load_toolchain(tmp_path / "missing.json")


def test_reference_mac_keeps_original_linker_identity() -> None:
    reference = tools.load_toolchain(None)
    assert reference["lld"].path == Path("/opt/homebrew/opt/lld/bin/ld.lld")
    assert (
        reference["lld"].sha256
        == "030cb3e4e93f36334617651e5ce97ef7ff6417986228786069b4fd63e69aa771"
    )


def test_formula_version_revision_and_bottle_are_all_pinned() -> None:
    formulae = {
        "formulae": [
            dict(
                name=name,
                versions=dict(stable=tools.RELEASE),
                revision=0,
                bottle=dict(stable=dict(files={tools.BOTTLE_TAG: dict(sha256=digest)})),
            )
            for name, digest in tools.BOTTLES.items()
        ]
    }
    tools.validate_formulae(formulae)
    formulae["formulae"][0]["revision"] = 1
    with pytest.raises(ValueError) as error:
        tools.validate_formulae(formulae)
    assert "llvm" in str(error.value)
    assert "expected 23.1.6 revision 0" in str(error.value)
    assert "observed '23.1.6' revision 1" in str(error.value)


@pytest.mark.parametrize("tampered", [False, True])
def test_provision_pours_checked_bottles_before_declaring_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered: bool,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "macOS")
    monkeypatch.setenv("RUNNER_ARCH", "ARM64")
    archives = {}
    for name, executable in (("llvm", "clang"), ("lld", "ld.lld")):
        archive = tmp_path / (name + ".tar.gz")
        archive.write_bytes(name.encode())
        archives[name] = archive
        binary = tmp_path / name / "bin" / executable
        binary.parent.mkdir(parents=True)
        binary.write_bytes(executable.encode())
        binary.chmod(0o700)
    pins = {name: tools.digest(path) for name, path in archives.items()}
    monkeypatch.setattr(tools, "BOTTLES", pins)
    formulae = {
        "formulae": [
            dict(
                name=name,
                versions=dict(stable=tools.RELEASE),
                revision=0,
                installed=[{}] if name == "llvm" else [],
                bottle=dict(stable=dict(files={tools.BOTTLE_TAG: dict(sha256=sha)})),
            )
            for name, sha in pins.items()
        ]
    }
    if tampered:
        archives["llvm"].write_bytes(b"corrupted archive")
    calls = []

    def run(args: list[str], *, seconds: int = 180) -> str:
        calls.append(args)
        if args[1] == "info":
            return json.dumps(formulae)
        if args[1] == "--cache":
            return str(archives[args[-1]])
        if args[1] == "--prefix":
            return str(tmp_path / args[-1])
        if args[-1] == "--version":
            return "clang LLD " + tools.RELEASE
        assert args[1] in {"fetch", "install", "reinstall"}
        return ""

    monkeypatch.setattr(tools, "run", run)
    output = tmp_path / "declared.json"
    if tampered:
        with pytest.raises(ValueError, match="bottle"):
            tools.provision(output)
        assert not output.exists()
        assert not any(c[1] in {"install", "reinstall"} for c in calls)
    else:
        tools.provision(output)
        assert tools.verify_tool(tools.load_toolchain(output), "clang").name == "clang"
        assert output.stat().st_mode & 0o777 == 0o600
        assert [c[1:] for c in calls if c[1] in {"install", "reinstall"}] == [
            ["reinstall", "--force-bottle", "llvm"],
            ["install", "--force-bottle", "lld"],
        ]


def test_command_environment_is_child_only(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "HOMEBREW_NO_AUTO_UPDATE"
    monkeypatch.delenv(key, raising=False)
    observed = {}

    def command(*args: object, **kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", command)
    assert tools.run([tools.BREW, "info"]) == "ok"
    assert key not in os.environ
    assert isinstance(observed["env"], dict) and observed["env"][key] == "1"


def test_bottle_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bottle.tar.gz"
    path.write_bytes(b"not the pinned upstream bottle")
    with pytest.raises(ValueError, match="bottle"):
        tools.verify_bottle(path, tools.BOTTLES["llvm"])
