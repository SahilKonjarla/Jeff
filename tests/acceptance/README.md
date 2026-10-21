# Acceptance tests

**Status:** installed M01 help, project-command, and branch/change-question checks implemented. M01 itself is not accepted.

- [test_demo_workflow.py](test_demo_workflow.py) invokes the real generator CLI from a foreign working directory, then installed Jeff initialization/config/status and both questions for each generated repository. It checks distinct/stable IDs, initial `main`/baseline/note metadata, preserved registration on rerun, help without writes, and refusal without echoing private fixture data.
- [test_cli_install.py](test_cli_install.py) checks installed console/module help and package metadata from temporary directories, with no project I/O.
- [test_project_commands.py](test_project_commands.py) runs real installed module commands against disposable Git repositories: init/config/status/repeat, stable identity from subdirectories, actual `git check-ignore`, errors outside/unregistered repositories, and safe malformed-config messages. Inherited Git targeting/config injection is deliberately supplied and must not redirect the adapter.

- [test_installed_questions.py](test_installed_questions.py) runs both installed console and module `ask` entry points from fixture subdirectories. It checks identity/time, unborn branch, scoped changes, escaped filenames in JSON/text, unsupported/unregistered errors, and unchanged config/HEAD with no index creation.

Project-command successes exit 0; recognized failures exit 1 with typed JSON or stderr text. Unsupported CLI syntax/commands exit 2. Config/status inspect identity only, not arbitrary settings or runtime state.

**Dependencies:** pytest and an editable installation in the test Python environment, plus system Git. [Fixture initialization](../conftest.py) is confined to temporary repositories; no user repository index/history is changed. Test output uses synthetic private values, never real credentials or user `.env` files. Integration protected-path fixtures use synthetic data only.

From the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/acceptance -q
```

**Owning milestone:** M01; later milestones add actual installed behavior and real integration evidence. Linux, other Python versions, non-editable artifacts, and fresh-machine setup remain unverified. Branch/change questions have scoped local checks, not complete M01 acceptance. No daemon, desktop, audio, providers, or remote integration acceptance is claimed. Missing hardware/credentials do not reduce required scope. See [test map](../README.md).
