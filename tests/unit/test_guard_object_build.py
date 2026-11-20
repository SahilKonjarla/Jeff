"""Compile-only Linux object/embedded-policy checks; never link or execute the helper."""

import hashlib
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/jeff/infrastructure/runtime_execution/guard_linux.c"


def sections(data: bytes) -> tuple[int, dict[str, bytes]]:
    assert data[:7] == b"\x7fELF\x02\x01\x01"
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    assert header[1] == 1 and header[4] == 0 and header[10] == 0  # ET_REL, no entry/phdrs.
    offset, stride, count, names_index = header[6], header[11], header[12], header[13]
    assert stride == 64 and 0 < count < 256 and names_index < count
    records = [struct.unpack_from("<IIQQQQIIQQ", data, offset + i * stride) for i in range(count)]
    names_record = records[names_index]
    names = data[names_record[4] : names_record[4] + names_record[5]]
    result = {}
    for record in records:
        name = names[record[0] :].split(b"\0", 1)[0].decode("ascii")
        if record[1] != 8:  # SHT_NOBITS has no file payload.
            assert record[4] + record[5] <= len(data)
            result[name] = data[record[4] : record[4] + record[5]]
    for record in records:
        if record[1] == 2:  # Reject an implicit libc/compiler-runtime dependency.
            assert record[9] == 24 and record[5] % 24 == 0 and record[6] < count
            strings = records[record[6]]
            symbol_names = data[strings[4] : strings[4] + strings[5]]
            table = data[record[4] : record[4] + record[5]]
            for name, _, _, section, _, _ in struct.iter_unpack("<IBBHQQ", table):
                assert section != 0 or name == 0, symbol_names[name:].split(b"\0", 1)[0]
    return header[2], result


def compile_object(source: Path, output: Path, arch: str) -> None:
    command = [
        "/usr/bin/clang",
        "--target=" + arch + "-linux-gnu",
        "-std=c11",
        "-O2",
        "-ffreestanding",
        "-nostdinc",
        "-fno-builtin",
        "-fno-stack-protector",
        "-fno-pic",
        "-fno-pie",
        "-fno-asynchronous-unwind-tables",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-c",
        str(source),
        "-o",
        str(output),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "HOME": str(output.parent), "TMPDIR": str(output.parent)},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module", params=[("aarch64", 183, 1), ("x86_64", 62, 2)])
def compiled(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[str, dict[str, bytes]]:
    arch, machine, abi = request.param
    assert SOURCE.is_file(), "Standalone helper source is missing"
    arena = tmp_path_factory.mktemp("object-" + arch)
    output = arena / "guard.o"
    compile_object(SOURCE, output, arch)
    actual, blobs = sections(output.read_bytes())
    assert actual == machine
    assert blobs[".jeff.guard"] == b"JGO1" + bytes([77, 8, 1, abi, 5, 21, 53, 61, 69, 1, 0, 0])
    assert ".interp" not in blobs and blobs[".text"]
    return arch, blobs


def evaluate(policy: bytes, syscall: int, arch: int, argument: int = 0) -> int:
    """Bounded interpreter of this emitted cBPF subset, not kernel execution."""
    words = {0: syscall, 4: arch, 16: argument & 0xFFFFFFFF, 20: argument >> 32}
    instructions = list(struct.iter_unpack("<HBBI", policy))
    accumulator, pc = 0, 0
    for _ in range(len(instructions) + 1):
        assert 0 <= pc < len(instructions)
        code, yes, no, value = instructions[pc]
        pc += 1
        if code == 0x20:
            accumulator = words[value]
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x45:
            pc += yes if accumulator & value else no
        elif code == 0x06:
            return int(value)
        else:
            raise AssertionError("Unexpected cBPF instruction")
    raise AssertionError("Unbounded policy evaluation")


def test_compile_only_object_has_policy(compiled: tuple[str, dict[str, bytes]]) -> None:
    arch, blobs = compiled
    policy = blobs[".jeff.guard.policy"]
    assert len(policy) % 8 == 0 and 0 < len(policy) < 4096
    # Frozen emitted policy bytes, also checked against independent syscall vectors below.
    expected = {
        "aarch64": "cf66160cb6e8b0c792af7ebe6bbb9d41ea5732af1f6ef01af203d6e576373849",
        "x86_64": "8065cc245d98b80cee958a3add26b51335a7ec7d1804b052a8bc254e2977d973",
    }
    assert hashlib.sha256(policy).hexdigest() == expected[arch]


def test_emitted_policy_denies_cross_process_controls(
    compiled: tuple[str, dict[str, bytes]],
) -> None:
    arch, blobs = compiled
    policy = blobs[".jeff.guard.policy"]
    audit = 0xC00000B7 if arch == "aarch64" else 0xC000003E
    denied = (
        [122, 274, 118, 119, 140, 117, 270, 271, 268, 97, 272, 241, 280, 282]
        if arch == "aarch64"
        else [203, 314, 142, 144, 141, 101, 310, 311, 308, 272, 312, 298, 321, 323]
    )
    denied += [438, 434, 424, 440, 448, 425, 426, 427]
    for number in denied:
        assert evaluate(policy, number, audit) == 0x50001
    assert evaluate(policy, 435, audit) == 0x50026  # clone3 -> ENOSYS, not permissive execution.
    assert evaluate(policy, 0, 0) == 0x80000000
    assert evaluate(policy, 0x40000000, audit) == 0x80000000
    assert evaluate(policy, 63 if arch == "aarch64" else 0, audit) == 0x7FFF0000


def test_emitted_policy_limits_clone_and_prlimit_targets(
    compiled: tuple[str, dict[str, bytes]],
) -> None:
    arch, blobs = compiled
    policy = blobs[".jeff.guard.policy"]
    audit = 0xC00000B7 if arch == "aarch64" else 0xC000003E
    clone, prlimit = (220, 261) if arch == "aarch64" else (56, 302)
    assert evaluate(policy, clone, audit, 17) == 0x7FFF0000
    for flags in (0x10000000 | 17, 0x20000000 | 17, 0x100000000 | 17):
        assert evaluate(policy, clone, audit, flags) == 0x50001
    assert evaluate(policy, prlimit, audit, 0) == 0x7FFF0000
    for target in (1, 0x100000000):
        assert evaluate(policy, prlimit, audit, target) == 0x50001


def test_control_and_output_modes_are_disjoint_compile_time(
    compiled: tuple[str, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    # Constant-expression checks use the SAME C predicate as helper startup.
    # No helper/probe is linked or executed, and no real F_GETFL is called.
    arch, _ = compiled
    header = SOURCE.with_name("guard_core.h")
    probe = tmp_path / "fd-mode-checks.c"
    probe.write_text(f'''#include "{header}"
_Static_assert(G_CONTROL_ACCESS == 0 && G_OUTPUT_ACCESS == 1, "Linux access modes");
_Static_assert(G_ACCESS_MASK == 3, "Linux access mask");
_Static_assert(G_IO_ACCESS_OK(0, G_CONTROL_ACCESS), "read-only input");
_Static_assert(G_IO_ACCESS_OK(1, G_OUTPUT_ACCESS), "write-only output");
_Static_assert(G_IO_ACCESS_OK(0x800, G_CONTROL_ACCESS), "nonblocking input");
_Static_assert(G_IO_ACCESS_OK(0xc01, G_OUTPUT_ACCESS), "append/nonblock output");
_Static_assert(!G_IO_ACCESS_OK(-9, G_CONTROL_ACCESS), "failed input F_GETFL");
_Static_assert(!G_IO_ACCESS_OK(-9, G_OUTPUT_ACCESS), "failed output F_GETFL");
_Static_assert(!G_IO_ACCESS_OK(1, G_CONTROL_ACCESS), "write-only input");
_Static_assert(!G_IO_ACCESS_OK(0, G_OUTPUT_ACCESS), "read-only output");
_Static_assert(!G_IO_ACCESS_OK(2, G_CONTROL_ACCESS), "read-write input");
_Static_assert(!G_IO_ACCESS_OK(2, G_OUTPUT_ACCESS), "read-write output");
_Static_assert(!G_IO_ACCESS_OK(3, G_CONTROL_ACCESS), "special input mode");
_Static_assert(!G_IO_ACCESS_OK(3, G_OUTPUT_ACCESS), "special output mode");
_Static_assert(!(G_IO_ACCESS_OK(0x800, G_CONTROL_ACCESS) &&
                 G_IO_ACCESS_OK(0x800, G_OUTPUT_ACCESS)), "same OFD cannot satisfy both");
''')
    output = tmp_path / "fd-mode-checks.o"
    compile_object(probe, output, arch)
    machine, _ = sections(output.read_bytes())
    assert machine == (183 if arch == "aarch64" else 62)
