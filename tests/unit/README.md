# Unit tests

**Status:** M01 CLI, domain identity, and application use-case tests implemented.

- [test_demo_safety.py](test_demo_safety.py): generator preflight/refusal, symlinks/traversal/markers, no writes or Git on preserved repos, missing Git, partial-state refusal, and prevention of parent-repository Git fallback.
- [test_cli.py](test_cli.py): help/no-argument behavior, unavailable commands, and help without adapter calls or files.
- [test_questions.py](test_questions.py): deterministic routing, unsupported input without Git access/echo, registered targeting/time, protected origin/destination filtering, and safe terminal metadata.
- [test_projects.py](test_projects.py): absolute roots, stable repeat initialization, inspection without implicit registration, and mismatched roots from a controlled port.

**Dependencies:** pytest, Typer test runner, Pydantic result contracts, domain/application/interfaces. Controlled discovery/storage doubles exercise the same public interface as adapters without invoking Git or storage. Real filesystem/process checks belong under integration/acceptance.

From the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/unit -q
```

**Owning milestone:** M01, not yet accepted. No daemon, approvals, jobs, or provider behavior is implemented/tested; read-only Git-question rules are covered. Fixtures contain only synthetic data, no credentials or environment files. Extend through real public contracts rather than test-only execution paths. See [test map](../README.md).
