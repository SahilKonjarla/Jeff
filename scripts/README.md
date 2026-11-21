# Development scripts

**Status:** M01 demo-project generation implemented. These helpers are not part of the installed Jeff runtime; production imports of `scripts`/`tests` are rejected by architecture checks.

## M04 pytest runner image

`pytest_job_runner.py` is copied into the declared Linux image at
`/opt/jeff/pytest_job_runner.py`, not installed into the native Jeff wheel.
The accepted ARM64 image used `python:3.12.10-slim` resolved to
`sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db`,
with pytest 9.1.1, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0 and Pygments
2.21.0 installed during image preparation. The image sets `TMPDIR=/scratch` so
pytest capture can use M03’s writable scratch mount. The fixture itself needs
no additional packages.

Declare the built image by its actual immutable image ID in the guarded profile;
rebuilding can produce a different ID. Image preparation is separate from job
approval: Jeff won’t pull, install dependencies or fall back to the host at run
time. See the [pytest CLI workflow](../README.md#pytest-jobs-m04).

## Hosted CI guard build prerequisites

`scripts/ci_guard_toolchain.py` provisions build tools only on the declared hosted macOS ARM64 runner. It checks out immutable Homebrew/core commit `2c03794c90e4b4506ac0381072064840e5b2ccf6`, validates that commit and both tool formula SHA256s, and temporarily activates that core snapshot for tool/dependency resolution. Any original core tap is preserved and restored even on ordinary setup failure; no arbitrary formula-path allowance is enabled. Homebrew API lookup and automatic updates are disabled in child commands, so a moving or cached catalog cannot select a different LLVM release. LLVM/LLD23.1.6 revision0 and pinned `arm64_sequoia` bottle metadata/downloaded bytes are still checked before pour; existing installations are repoured. This source checkout and normal dependency setup occur only on the temporary hosted runner, not in the Jeff checkout or local Homebrew installation.

The setup step writes a fresh mode0600 schema2 manifest binding the immutable core commit and explicitly supplies `JEFF_GUARD_TEST_TOOLCHAIN` to the build tests. The manifest declares post-pour tool identities (Homebrew relocation can change bytes); tests hash those binaries before/after use. Provenance relies on the pinned bottle/manager setup, not independent signed attestation. Without an explicit manifest, only the original reference-Mac linker/compiler byte pins are accepted; missing/invalid explicit configuration fails, never silently falls back or skips.

LLD is a multicall driver: retain the `ld.lld` invocation alias even while hashing its resolved target. The tests compile Linux objects, link private non-executable candidates and inspect metadata/negative mutations; they never execute Linux helper code. Production guard resources/filter pins remain unchanged. Only an explicitly reviewed core/formula/bottle pin update can change the selected distribution; the mutable upstream default branch is never checked out. The script's hosted gate is an operational safeguard, not a security boundary against a caller forging environment values.

## Hosted CI historical compatibility fixtures

`scripts/ci_compatibility_fixtures.py` builds three noneditable historical installations only on the hosted macOS ARM64 runner, under a fresh private directory in `RUNNER_TEMP`. Source commits are v3 `8f1129023631aa8a0ac4041c70bfba2d33ef0297` (wire3/DB2), B `930d8a1419a502fc87841ff3f1bb33292628c229` and C `fd3a53aa985845d806653ed3599e98212c35df90` (both wire5/DB3). These are **new source-pinned rebuilds, not recovered original wheels or substitutes for historical acceptance evidence**.

The setup uses recorded hash-pinned setuptools/wheel/packaging build inputs and the current dependency-lock version constraints. It checks each checkout commit, compares all packaged Python modules with source, hashes the new wheel before/after installation, and verifies installed module hashes plus wire/DB identities from its isolated interpreter. A fresh `manifest.json` records the new identities; only after all three pass does the script publish `JEFF_TEST_V3_PYTHON`, `JEFF_TEST_B_PYTHON`, and `JEFF_TEST_C_PYTHON` through `GITHUB_ENV`. Failure never substitutes the current interpreter or skips a test. Setup imports package constants but starts no daemon, workload, or database migration. The existing ten-minute job limit and test selection remain unchanged. No local historical installation is needed for the mocked setup unit tests. Other pre-existing optional historical-fixture skips are unchanged, not newly established compatibility evidence.

## Current guard delivery boundary

M03 now packages two pinned JFG2 guard binaries under `src/jeff/infrastructure/runtime_execution/`, verified by the installation-owned adapter. No package compile/download/compiler-discovery hook exists. Current executable protocol is JFG2; JFG1 drafts below are historical and never gain authority. Builds, replacement binaries and runtime probes still need separately approved provenance/finite scope. See [current controller/artifact contract](../src/jeff/infrastructure/runtime_execution/README.md).

## Standalone Linux guard — build/artifact convention only

The following compile/link narrative is historical; its future-tense integration statements describe that checkpoint, not the current packaged JFG2 path.

M03's approved direction uses an independently pinned standalone Linux helper rather than assuming every job image contains Python or another trusted interpreter. The initial [application source contract](../src/jeff/application/README.md#standalone-guard--source-contract-only) now has a subsequent C-source/compile-only implementation in `src/jeff/infrastructure/runtime_execution/{guard_linux.c,guard_core.h,guard_abi.h}`. Existing local Clang compiles native Linux ARM64/AMD64 objects in fresh private test arenas using `-c -std=c11 -O2 -ffreestanding -nostdinc -fno-builtin -fno-stack-protector -fno-pic -fno-pie -fno-asynchronous-unwind-tables -Wall -Wextra -Werror`. These are **ET_REL objects, not executable or deployable guard artifacts**. That compile-only slice added no Linux build script/toolchain/linker, linked helper binary, package-data artifact or automatic setup/build hook; the subsequent separately approved tool/link checkpoint is recorded below.

`tests/unit/test_guard_object_build.py` performs the approved compile-only checks for both explicit targets (`aarch64-linux-gnu`, `x86_64-linux-gnu`) with `/usr/bin/clang` and no dependency download. It inspects object/ABI and emitted-filter data without running the helper or installing a filter. The control-FD gate adds one constant-expression C probe compilation per ABI, for4 compiler invocations in the final148-case selection; probes use startup's actual mode predicate and are not linked/executed. Compilation is not Linux/runtime conformance. At that earlier compile-only checkpoint the ELF linker was not found on PATH and no toolchain was installed. The later approved link-only checkpoint below supersedes that tooling status.

For a separately approved future link/build/distribution slice:

- Target names are `jeff-job-guard-linux-arm64` and `jeff-job-guard-linux-amd64`, selected explicitly by the reviewed platform; no host-macOS executable or architecture fallback.
- Artifact declarations bind exact SHA256, positive byte size (at most16MiB), platform, JFG1 protocol and workload-filter policy SHA256. These are declarations, not evidence of reproducibility, Linux/kernel compatibility or isolation.
- The reviewed guest executable location is `/jeff-runtime/jeff-job-guard`; eventual delivery must be immutable/read-only and separate from untrusted project/scratch content. This does not authorize a new mount or change current Docker policy.
- Require explicit compiler/source provenance, per-ABI build/test receipts, artifact hashes and separately reviewed wheel package-data inclusion. Builds/pulls/downloads, dependencies and runtime probes need their own approved finite scope. Existing verified artifacts must not be silently overwritten.
- Package installation must not compile/download a helper, query/start Docker or silently accept an unverified binary. No permissive fallback from an unavailable guard to raw execution may be introduced when guarded activation is later integrated.

## Guard link-only tool preparation checkpoint

User-approved Homebrew installation added LLD23.1.6, LLVM23.1.6 and z3 5.4.1, and upgraded transitive lz4 1.10.0→1.11.1 and xz 5.8.2→5.8.4; zstd 1.5.7_1 was already installed. `HOMEBREW_NO_AUTO_UPDATE=1`, `HOMEBREW_NO_INSTALL_CLEANUP=1` and `HOMEBREW_NO_ANALYTICS=1` were set, with private cache/log/temp locations and bottle-only installation. Required dependency upgrades still occurred; old lz4/xz versions remain present. These are shared `/opt/homebrew` changes, not project Python dependency changes or permission for further installs/uninstalls.

A private `.agent-work` driver invoked `/opt/homebrew/opt/lld/bin/ld.lld` exactly once per pinned object, using `-m aarch64elf`/`-m elf_x86_64`, `--static --no-dynamic-linker --no-undefined --fatal-warnings --entry=_start -z noexecstack -z relro -z now`. Output ELF metadata/hashes were checked without executing candidates, and outputs were set to0600. See the [artifact boundary](../src/jeff/infrastructure/README.md#standalone-guard--link-only-elf-candidates). This is not a published build script, reproducible-build certification, kernel validation or a normal-installation upgrade. No global PATH/profile edit, package-data hook, source build, Docker operation or repository Git action was performed. Further kernel probes require their own explicit scope/capacity.

## Generator interface

From the Jeff source root, with Python 3.12+ and system Git (supports initial-branch/SHA-1 object-format selection):

```bash
.venv/bin/python scripts/create_demo_projects.py
```

The default is anchored to the script's source checkout, not the caller's current directory. It creates two independent Git repositories under [examples](../examples/README.md). For a separate fresh location whose parent already exists:

```bash
.venv/bin/python scripts/create_demo_projects.py --output-dir /absolute/path/to/new-demos
```

Replace the example path deliberately; `..`, symlinks, special paths, unrelated existing targets, and invalid ownership markers are refused. Alternate destinations are not automatically added to a parent repository's ignore rules; choose a separate location or manage those ignores explicitly. `--help` lists the interface without generating anything. Exit 0 prints `Created`/`Preserved` paths; refusal/failure exits 1 with a safe message.

The importable development interface is `generate(output_dir: Path = DEFAULT_OUTPUT) -> tuple[DemoResult, ...]`. Results carry project, path, and created status; `DemoError` contains public-safe diagnostics. `scripts/__init__.py` supports tests only; the installed package still includes only `src/jeff`.

## Creation and preservation contract

1. Preflight both targets before creating either. Existing generated targets require a real local `.git` directory and bounded, no-follow regular `.jeff-demo.json` marker with matching generator/version/project/baseline hash.
2. For absent targets only, exclusively create template files, initialize `main`, stage the explicit baseline file list, and commit using synthetic identity and fixed dates. No remotes or copied project files. System/global Git config, templates, hooks, signing, user attributes, transports, and ambient Git targeting are disabled.
3. Record a private ignored recognition receipt and leave `demo-notes.txt` intentionally untracked. Baseline hashes reproduce for identical templates across locations.
4. Repeated runs perform no Git operations on existing recognized repositories and preserve all files, edits, branches, commits, and Jeff IDs/settings. There is no reset, deletion, repair, upgrade, force, service startup, Docker execution, or automatic Jeff registration.

Git add/commit requires the child's real `.git`; it cannot fall back to the parent repository if initialization is interrupted. Marker recognition is not authentication or integrity verification. Checks reduce unsafe path handling but are not a complete adversarial same-user filesystem-race sandbox. Failures may leave partial directories; they are never automatically removed or adopted on retry. Inspect manually or choose a new output location.

## Fixture contents and restoration

Template strings live in [`_demo_templates.py`](_demo_templates.py) and remain re-exported by `create_demo_projects.py`; no template package/dependency is added. The sandbox contains a loopback-first HTTP service, deterministic pytest mismatch, and narrow-mount Compose service. The second project is minimal. Generated READMEs explain exact behavior and deliberate single-file restoration using the recorded baseline; broad cleanup/reset is not a setup procedure.

Service/test/Compose details belong in [examples](../examples/README.md). No raw credentials or environment files are required. `.gitignore` already excludes both default generated roots, so generation does not modify the parent's ignore rules or index/history.

**Owning milestones:** M01 creates/validates fixtures; M04 extends test orchestration; M05 owns Jeff process/Docker integration. Fixture files alone do not implement later milestones.

## Validation

```bash
.venv/bin/python -m pytest tests/unit/test_demo_safety.py tests/integration/test_demo_generation.py tests/integration/test_demo_http.py tests/acceptance/test_demo_workflow.py -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
```

Checks cover refusal/preservation, independent/deterministic Git baselines, parent Git immutability, real loopback health/404/collision behavior with owned-process teardown, the **expected** 1-pass/1-fail nested pytest run, YAML structure, and installed Jeff commands against both demos. Local Compose syntax validation passed without pulls/services and with isolated empty Docker configuration. No Docker runtime or fresh-OS acceptance is claimed. Jeff targets macOS only. Clean local wheel/venv and manual CLI checks are recorded separately; no one-command CI runner, Docker validation environment or self-hosted runner is implemented by this generator. Test temp directories must satisfy the [outside-Git boundary](../tests/README.md), not merely an ignore rule. See [root setup](../README.md) and the local progress tracker for actual evidence. Extend only with bounded helpers that do not bypass application policy.
