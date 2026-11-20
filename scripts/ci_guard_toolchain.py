"""Provision checksum-pinned hosted-CI build tools, never Linux runtime guards.

Homebrew validates/pours the pinned bottles (including normal relocation). The
trusted setup step declares resulting executable identities; tests recheck those
bytes before/after use. This is not signed attestation or production authority.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

RELEASE = "23.1.6"
BOTTLE_TAG = "arm64_sequoia"
BOTTLES = {
    "llvm": "8a7215262526c9d76c252136eeb4b972ffd93fa6aa1820a5cd16d21cf1362ad8",
    "lld": "13124f426568b2b1d209f446faea63b2425dc1f1a2540b2b9853706b03fc8a8c",
}
BREW = "/opt/homebrew/bin/brew"


@dataclass(frozen=True)
class Tool:
    path: Path
    sha256: str


REFERENCE = {
    "lld": Tool(
        Path("/opt/homebrew/opt/lld/bin/ld.lld"),
        "030cb3e4e93f36334617651e5ce97ef7ff6417986228786069b4fd63e69aa771",
    ),
    "clang": Tool(
        Path("/opt/homebrew/opt/llvm/bin/clang"),
        "9d0a3f7f8ddaf3f6d8a633f7d06b2edb515e78c1929ac9edaf95f9930d8aec86",
    ),
}


def need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def obj(value: object) -> dict[str, object]:
    need(isinstance(value, dict) and all(isinstance(k, str) for k in value), "Invalid object")
    return cast(dict[str, object], value)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_tool(tools: dict[str, Tool], role: str) -> Path:
    tool = tools[role]
    need(tool.path.is_absolute(), "Build tool path must be absolute")
    path = tool.path.resolve(strict=True)
    facts = path.stat()
    need(stat.S_ISREG(facts.st_mode) and bool(facts.st_mode & 0o111), "Build tool not executable")
    need(digest(path) == tool.sha256, "Build tool digest changed")
    # LLD selects ELF/Mach-O mode from argv[0]; hash the target, invoke the alias.
    return tool.path


def load_toolchain(manifest: Path | None) -> dict[str, Tool]:
    if manifest is None:
        return dict(REFERENCE)
    need(manifest.is_absolute() and manifest.stat().st_size <= 65536, "Invalid toolchain path/size")
    data = obj(json.loads(manifest.read_bytes()))
    need(set(data) == {"schema", "release", "bottles", "tools"}, "Invalid toolchain fields")
    need(type(data["schema"]) is int and data["schema"] == 1, "Invalid toolchain schema")
    need(data["release"] == RELEASE and data["bottles"] == BOTTLES, "Unapproved tool provenance")
    entries = obj(data["tools"])
    need(set(entries) == {"lld", "clang"}, "Invalid build tools")
    result = {}
    for role, entry in entries.items():
        item = obj(entry)
        need(set(item) == {"path", "sha256"}, "Invalid tool identity fields")
        path, sha = item["path"], item["sha256"]
        need(isinstance(path, str) and isinstance(sha, str), "Invalid tool identity")
        path, sha = cast(str, path), cast(str, sha)
        need(
            Path(path).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", sha) is not None,
            "Invalid tool path/digest",
        )
        result[role] = Tool(Path(path), sha)
    return result


def validate_formulae(value: object) -> None:
    entries = obj(value)["formulae"]
    need(isinstance(entries, list), "Invalid formula list")
    found = {}
    for entry in cast(list[object], entries):
        formula = obj(entry)
        name = formula["name"]
        need(isinstance(name, str) and name not in found, "Invalid formula name")
        found[cast(str, name)] = formula
    need(set(found) == set(BOTTLES), "Unexpected formulae")
    for name, expected in BOTTLES.items():
        formula = found[name]
        stable, revision = obj(formula["versions"])["stable"], formula["revision"]
        need(
            stable == RELEASE and revision == 0,
            f"Unapproved {name} release/revision: expected {RELEASE} revision 0; "
            f"observed {stable!r} revision {revision!r}",
        )
        files = obj(obj(obj(formula["bottle"])["stable"])["files"])
        need(obj(files[BOTTLE_TAG])["sha256"] == expected, "Unapproved bottle checksum")


def verify_bottle(path: Path, expected: str) -> None:
    need(path.is_file() and digest(path) == expected, "Pinned bottle checksum mismatch")


def run(args: list[str], *, seconds: int = 180, refresh_metadata: bool = False) -> str:
    child_env = os.environ | {
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_INSTALL_CLEANUP": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
    }
    child_env.pop("HOMEBREW_FORCE_API_AUTO_UPDATE", None)
    if refresh_metadata:
        # Runner images can carry an older API catalog. Refresh metadata once,
        # then validate exact pins; never enable general Homebrew auto-upgrades.
        child_env["HOMEBREW_FORCE_API_AUTO_UPDATE"] = "1"
        child_env["HOMEBREW_API_AUTO_UPDATE_SECS"] = "0"
        child_env.pop("HOMEBREW_API_UPDATED", None)
    result = subprocess.run(args, capture_output=True, text=True, timeout=seconds, env=child_env)
    need(result.returncode == 0, "Build tool command failed: " + result.stderr[-1000:])
    return result.stdout.strip()


def provision(output: Path) -> None:
    need(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_OS") == "macOS"
        and os.environ.get("RUNNER_ARCH") == "ARM64",
        "Provision only on hosted macOS ARM64 CI",
    )
    need(
        output.is_absolute() and output.parent.is_dir() and not output.exists(),
        "Fresh output required",
    )
    info = [BREW, "info", "--json=v2", "llvm", "lld"]
    validate_formulae(json.loads(run(info, refresh_metadata=True)))
    run([BREW, "fetch", "--force-bottle", "--bottle-tag=" + BOTTLE_TAG, "llvm", "lld"])
    for name, sha in BOTTLES.items():
        cache = Path(run([BREW, "--cache", "--bottle-tag=" + BOTTLE_TAG, name]))
        verify_bottle(cache, sha)
    state = obj(json.loads(run(info)))
    validate_formulae(state)
    entries = cast(list[object], state["formulae"])
    installed = {cast(str, obj(f)["name"]): obj(f)["installed"] for f in entries}
    for name in BOTTLES:
        need(isinstance(installed[name], list), "Invalid installed formula state")
        # A cached runner installation must not bypass the pinned bottle pour.
        action = "reinstall" if installed[name] else "install"
        run([BREW, action, "--force-bottle", name])
    validate_formulae(json.loads(run(info)))
    declared = {}
    for role, formula, executable, label in (
        ("lld", "lld", "ld.lld", "LLD"),
        ("clang", "llvm", "clang", "clang"),
    ):
        path = Path(run([BREW, "--prefix", formula])) / "bin" / executable
        before = digest(path)
        version = run([str(path), "--version"], seconds=20)
        need(
            label in version and re.search(r"\b23\.1\.6\b", version) is not None,
            "Installed tool version mismatch",
        )
        need(digest(path) == before, "Tool changed during setup")
        declared[role] = {"path": str(path), "sha256": before}
    data = dict(schema=1, release=RELEASE, bottles=BOTTLES, tools=declared)
    with output.open("x", encoding="utf-8") as stream:
        output.chmod(0o600)
        stream.write(json.dumps(data, sort_keys=True, indent=2) + "\n")
    for role in declared:
        verify_tool(load_toolchain(output), role)
    print("Pinned LLVM/LLD build toolchain declared; no guard was executed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    provision(parser.parse_args().output)


if __name__ == "__main__":
    main()
