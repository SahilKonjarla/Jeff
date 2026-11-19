"""JFG2 compile/link-only checks. Never execute a Linux object or candidate ELF."""

import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from test_guard_object_build import compile_object, evaluate, sections

from jeff.application.execution import GuardV2Terminal, encode_guard_v2_frame

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/jeff/infrastructure/runtime_execution/guard_v2_linux.c"
LINKER = Path("/opt/homebrew/opt/lld/bin/ld.lld")
LINKER_SHA = "030cb3e4e93f36334617651e5ce97ef7ff6417986228786069b4fd63e69aa771"


@pytest.fixture(scope="module", params=[("aarch64", 183, 1), ("x86_64", 62, 2)])
def compiled_v2(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[str, Path, dict[str, bytes]]:
    arch, machine, abi = request.param
    assert SOURCE.is_file(), "JFG2 helper source missing"
    arena = tmp_path_factory.mktemp("object-v2-" + arch)
    output = arena / "guard-v2.o"
    compile_object(SOURCE, output, arch)
    actual, blobs = sections(output.read_bytes())
    assert actual == machine
    assert blobs[".jeff.guard"] == b"JGO2" + bytes([77, 8, 2, abi, 5, 21, 53, 61, 69, 1, 0, 0])
    assert blobs[".text"] and ".interp" not in blobs
    return arch, output, blobs


def test_jfg2_object_is_native_static_code_with_unchanged_child_filter(
    compiled_v2: tuple[str, Path, dict[str, bytes]],
) -> None:
    arch, _, blobs = compiled_v2
    expected = {
        "aarch64": "cf66160cb6e8b0c792af7ebe6bbb9d41ea5732af1f6ef01af203d6e576373849",
        "x86_64": "8065cc245d98b80cee958a3add26b51335a7ec7d1804b052a8bc254e2977d973",
    }
    policy = blobs[".jeff.guard.policy"]
    assert hashlib.sha256(policy).hexdigest() == expected[arch]
    audit = 0xC00000B7 if arch == "aarch64" else 0xC000003E
    for number in [122, 140, 117, 270, 271] if arch == "aarch64" else [203, 141, 101, 310, 311]:
        assert evaluate(policy, number, audit) == 0x50001
    assert evaluate(policy, 0x40000000, audit) == 0x80000000


def test_actual_core_pipe_clock_eof_predicates_compile_without_execution(
    compiled_v2: tuple[str, Path, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    arch, _, _ = compiled_v2
    source = tmp_path / "predicates.c"
    header = SOURCE.with_name("guard_v2_core.h")
    source.write_text(f'''#include "{header}"
_Static_assert(G2_FRAME == 4173, "one bounded frame");
_Static_assert(G2_CONTROL_IDLE(-11), "only EAGAIN allows dispatch");
_Static_assert(!G2_CONTROL_IDLE(-4), "EINTR is not healthy control");
_Static_assert(!G2_CONTROL_IDLE(0), "EOF rejects dispatch");
_Static_assert(!G2_CONTROL_IDLE(1), "queued abort or other input rejects dispatch");
_Static_assert(!G2_CONTROL_IDLE(-5), "read errors reject dispatch");
_Static_assert(G2_PIPE_IDS_OK(1,2,3), "three independent pipe endpoints");
_Static_assert(!G2_PIPE_IDS_OK(1,2,2), "stdout cannot alias guard status");
_Static_assert(!G2_PIPE_IDS_OK(1,1,3), "control cannot alias stdout");
_Static_assert(!G2_PIPE_IDS_OK(0,2,3), "no missing pipe identity");
_Static_assert(G2_CLOCK_DECISION(9,10,20)==1, "clock fault, not deadline");
_Static_assert(G2_CLOCK_DECISION(20,10,20)==2, "deadline wins boundary");
_Static_assert(G2_CLOCK_DECISION(19,10,20)==0, "within budget");
_Static_assert(!G2_FINAL_STDERR_EOF(1,0), "transient EOF can reopen");
_Static_assert(G2_FINAL_STDERR_EOF(1,1), "no child can retain/reopen writer");
_Static_assert(G2_WAIT_ALL==0x40000001U, "include non-SIGCHLD clone descendants");
_Static_assert(G2_TERM_FLAGS(1,0)==1 && G2_TERM_FLAGS(1,1)==3, "stderr facts");
''')
    output = tmp_path / "predicates.o"
    compile_object(source, output, arch)
    machine, _ = sections(output.read_bytes())
    assert machine == (183 if arch == "aarch64" else 62)


def inspect_linked_v2(data: bytes, machine: int, abi: int, policy: bytes) -> dict[str, object]:
    """ELF metadata only. Never load or execute candidate code."""
    assert 64 <= len(data) <= 16777216
    assert data[:7] == b"\x7fELF\x02\x01\x01"
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    assert header[1:4] == (2, machine, 1) and header[4] > 0
    assert header[8:10] == (64, 56) and 0 < header[10] <= 32
    assert 64 <= header[5] and header[5] + header[9] * header[10] <= len(data)
    programs = [
        struct.unpack_from("<IIQQQQQQ", data, header[5] + i * header[9]) for i in range(header[10])
    ]
    loads, stacks = [], []
    for kind, flags, offset, address, _, filesz, memsz, alignment in programs:
        assert kind not in (2, 3) and offset + filesz <= len(data)
        if kind == 1:
            assert filesz <= memsz and address + memsz <= 2**64
            assert flags & 3 != 3
            assert alignment in (0, 1) or alignment & (alignment - 1) == 0
            assert alignment <= 1 or address % alignment == offset % alignment
            loads.append((flags, address, filesz))
        if kind == 0x6474E551:
            assert flags & 1 == 0
            stacks.append(kind)
    assert loads and len(stacks) == 1
    assert any(
        flags & 1 and address <= header[4] < address + filesz for flags, address, filesz in loads
    )
    assert header[11] == 64 and 0 < header[12] <= 256 and header[13] < header[12]
    assert 64 <= header[6] and header[6] + header[11] * header[12] <= len(data)
    table = [
        struct.unpack_from("<IIQQQQIIQQ", data, header[6] + i * header[11])
        for i in range(header[12])
    ]

    def payload(section: tuple[int, ...]) -> bytes:
        assert section[1] != 8 and section[4] + section[5] <= len(data)
        return data[section[4] : section[4] + section[5]]

    section_names = payload(table[header[13]])
    blobs, starts = {}, []
    for section in table:
        assert section[0] < len(section_names) and section_names.find(b"\0", section[0]) >= 0
        name = section_names[section[0] : section_names.index(b"\0", section[0])].decode("ascii")
        assert name not in blobs
        if section[1] != 8:
            blobs[name] = payload(section)
        assert section[1] not in (6, 11)
        if section[1] != 2:
            continue
        assert section[9] == 24 and section[5] % 24 == 0 and section[6] < len(table)
        strings = table[section[6]]
        assert strings[4] + strings[5] <= len(data)
        names = data[strings[4] : strings[4] + strings[5]]
        assert section[4] + section[5] <= len(data)
        for name, info, _, index, value, _ in struct.iter_unpack(
            "<IBBHQQ",
            data[section[4] : section[4] + section[5]],
        ):
            assert name < len(names) and names.find(b"\0", name) >= 0
            symbol = names[name : names.index(b"\0", name)]
            assert index != 0 or not symbol
            if symbol == b"_start":
                assert info & 15 == 2 and 0 < index < len(table)
                starts.append(value)
    assert starts == [header[4]]
    assert blobs[".text"] and ".interp" not in blobs
    assert ".dynamic" not in blobs and ".dynsym" not in blobs
    assert blobs[".jeff.guard"] == b"JGO2" + bytes([77, 8, 2, abi, 5, 21, 53, 61, 69, 1, 0, 0])
    assert blobs[".jeff.guard.policy"] == policy
    return dict(
        machine=machine,
        entry=header[4],
        elf_type="ET_EXEC",
        bytes=len(data),
        interpreter=False,
        dynamic=False,
        executable_stack=False,
        writable_executable_load=False,
        undefined_symbols=False,
        entry_matches_start=True,
        emitted_filter_sha256=hashlib.sha256(policy).hexdigest(),
    )


def test_link_only_native_v2_candidate_and_negative_metadata_checks(
    compiled_v2: tuple[str, Path, dict[str, bytes]],
) -> None:
    arch, obj, blobs = compiled_v2
    assert hashlib.sha256(LINKER.resolve().read_bytes()).hexdigest() == LINKER_SHA
    machine, abi, emulation = (183, 1, "aarch64elf") if arch == "aarch64" else (62, 2, "elf_x86_64")
    output = obj.with_suffix(".elf")
    assert not output.exists()
    command = [
        str(LINKER),
        "-m",
        emulation,
        "--static",
        "--no-dynamic-linker",
        "--no-undefined",
        "--fatal-warnings",
        "--entry=_start",
        "-z",
        "noexecstack",
        "-z",
        "relro",
        "-z",
        "now",
        "-o",
        str(output),
        str(obj),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        umask=0o177,
        env={"PATH": "/usr/bin:/bin", "HOME": str(obj.parent), "TMPDIR": str(obj.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    data, policy = output.read_bytes(), blobs[".jeff.guard.policy"]
    facts = inspect_linked_v2(data, machine, abi, policy)
    mutations = []
    for offset, replacement in ((4, b"\x01"), (16, b"\x01\0"), (24, bytes(8))):
        changed = bytearray(data)
        changed[offset : offset + len(replacement)] = replacement
        mutations.append(bytes(changed))
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    for i in range(header[10]):
        offset = header[5] + i * header[9]
        kind, flags = struct.unpack_from("<II", data, offset)
        if kind == 1:
            changed = bytearray(data)
            struct.pack_into("<I", changed, offset + 4, flags | 3)
            mutations.append(bytes(changed))
            break
    assert len(mutations) == 4
    for mutated in mutations:
        with pytest.raises(AssertionError):
            inspect_linked_v2(mutated, machine, abi, policy)
    assert hashlib.sha256(LINKER.resolve().read_bytes()).hexdigest() == LINKER_SHA
    obj.with_suffix(".json").write_text(
        json.dumps(
            dict(
                command=command,
                metadata=facts,
                object_sha256=hashlib.sha256(obj.read_bytes()).hexdigest(),
                elf_sha256=hashlib.sha256(data).hexdigest(),
                mode="0600",
                helper_executed=False,
                linker_sha256=LINKER_SHA,
                negative_metadata_checks=4,
            ),
            indent=2,
        )
        + "\n"
    )


def test_control_gate_is_wired_immediately_before_clone() -> None:
    """Structural regression only; not evidence of syscall/kernel ordering at runtime."""
    text = SOURCE.read_text()
    assert re.search(
        r"g2_control_before_clone\(&c\);\s*long child=G_CALL\(clone,",
        text,
    )
    gate = text.split("static void g2_control_before_clone(")[1].split("\n}", 1)[0]
    assert "G_CALL(read,0,&extra,1,0,0,0)" in gate
    assert "if (!G2_CONTROL_IDLE(control))" in gate
    assert "g2_finish(c,4,4,c->life.last,125)" in gate


def test_optimizer_folds_actual_c_wire_functions_to_python_golden_vectors(
    compiled_v2: tuple[str, Path, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    """Compile-time constant folding only, NOT running C or kernel behavior."""
    arch, _, _ = compiled_v2
    source, output = tmp_path / "vectors.c", tmp_path / "vectors.ll"
    nonce = UUID(int=1)
    functions = []
    for name, cause, detail, flags, fields in (
        ("exit124", 1, 124, 1, {"cause": "workload_exit", "exit_code": 124}),
        ("timeout", 3, 0, 1, {"cause": "deadline_expired"}),
        ("clock_fault", 4, 1, 1, {"cause": "guard_fault", "fault": "clock_regression"}),
    ):
        frame = GuardV2Terminal.model_validate(
            dict(
                nonce=nonce,
                manifest_digest="11" * 32,
                sequence=1,
                observed_ns=1100,
                stderr_eof=True,
                stderr_dropped=False,
            )
            | fields
        )
        expected = encode_guard_v2_frame(frame)
        checks = " & ".join(f"(out[{i}]=={value})" for i, value in enumerate(expected))
        functions.append(f"""int vector_{name}(void) {{
static const struct g_guard g={{.nonce={{[15]=1}},
.digest={{{",".join(["17"] * 32)}}}}};
unsigned char out[81];
g2_terminal(&g,out,1,1100,{cause},{detail},{flags});
return {checks};
}}""")
    source.write_text(f'#include "{SOURCE.with_name("guard_v2_core.h")}"\n' + "\n".join(functions))
    # Existing approved LLVM toolchain; Apple Clang 12 does not fully fold these
    # array stores. Helper objects still use the original compile_object flags.
    command = [
        "/opt/homebrew/opt/llvm/bin/clang",
        "--target=" + arch + "-linux-gnu",
        "-std=c11",
        "-O2",
        "-ffreestanding",
        "-nostdinc",
        "-fno-builtin",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-S",
        "-emit-llvm",
        str(source),
        "-o",
        str(output),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    ir = output.read_text()
    for name in ("exit124", "timeout", "clock_fault"):
        body = re.search(
            r"define[^@]*@vector_" + name + r"\([^)]*\)[^{]*\{([^}]+)\}", ir, flags=re.DOTALL
        )
        assert body is not None
        assert "ret i32 1" in body.group(1), "compiler did not fold the full wire vector"
