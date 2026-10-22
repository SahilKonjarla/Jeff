# Development scripts

**Status:** M01 demo-project generation implemented. These helpers are not part of the installed Jeff runtime; production imports of `scripts`/`tests` are rejected by architecture checks.

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

All templates live in `create_demo_projects.py`; no template package/dependency is added. The sandbox contains a loopback-first HTTP service, deterministic pytest mismatch, and narrow-mount Compose service. The second project is minimal. Generated READMEs explain exact behavior and deliberate single-file restoration using the recorded baseline; broad cleanup/reset is not a setup procedure.

Service/test/Compose details belong in [examples](../examples/README.md). No raw credentials or environment files are required. `.gitignore` already excludes both default generated roots, so generation does not modify the parent's ignore rules or index/history.

**Owning milestones:** M01 creates/validates fixtures; M04 extends test orchestration; M05 owns Jeff process/Docker integration. Fixture files alone do not implement later milestones.

## Validation

```bash
.venv/bin/python -m pytest tests/unit/test_demo_safety.py tests/integration/test_demo_generation.py tests/integration/test_demo_http.py tests/acceptance/test_demo_workflow.py -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
```

Checks cover refusal/preservation, independent/deterministic Git baselines, parent Git immutability, real loopback health/404/collision behavior with owned-process teardown, the **expected** 1-pass/1-fail nested pytest run, YAML structure, and installed Jeff commands against both demos. Local Compose syntax validation passed without pulls/services and with isolated empty Docker configuration. No Docker runtime, Linux or fresh-OS acceptance is claimed. Clean local wheel/venv and manual CLI checks are recorded separately; no one-command CI runner, Docker validation environment or self-hosted runner is implemented by this generator. Test temp directories must satisfy the [outside-Git boundary](../tests/README.md), not merely an ignore rule. See [root setup](../README.md) and the local progress tracker for actual evidence. Extend only with bounded helpers that do not bypass application policy.
