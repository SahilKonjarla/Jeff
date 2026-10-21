# Interfaces — tier 3

**Status:** M01 project commands and two read-only Git questions implemented; desktop, voice, MCP server, Pi UI, and runtime commands remain unimplemented.

**Ownership:** `cli.py` presents application results. `create_app(service: ProjectService, questions: QuestionService) -> typer.Typer` accepts injected use cases; clients never construct concrete adapters or invoke Git/storage directly.

**Dependencies:** application API/domain and client presentation libraries (currently Typer). No concrete storage, execution, integration, provider SDK, or bootstrap imports.

## Current commands

After [installation](../../../README.md), activate the venv and run inside the intended Git repository:

```bash
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

Init registers the project and preserves existing identity/settings. Config exposes identity only, never arbitrary settings. Status reports project registration only, not daemon, branch, jobs, or process health. Without `--json`, commands render human-readable identity or a safe error.

**Flow:** entry point → bootstrap wiring → CLI captures current directory → application service → typed response → rendering. Both `jeff` and `python -m jeff` use this path. Help creates no project files or adapter I/O.

Success exits 0. Recognized project-command failures exit 1: JSON envelopes go to stdout; human-readable errors go to stderr. No arguments/invalid CLI syntax/unknown commands exit 2 using Typer usage errors, not the application JSON envelope. `start` and `run` are unavailable. Unsupported `ask` input is an application `unsupported_intent` error, not an invented answer.

`ask` uses deterministic matching, no models. Branch answers distinguish named/unborn/detached HEAD. Scoped changes show index/worktree codes (`.` unchanged, `?` untracked), rename origin, and conflicts. Both identify project/root and observation time. Protected paths/submodule contents are explicitly excluded (`complete: false`); no visible changes never means a fully clean repository. Branch/file metadata uses reversible JSON string-body encoding; terminal roots and all JSON are ASCII-escaped to prevent control/bidi injection. Content/diffs are never rendered. See [application contracts](../application/README.md) and [installed question tests](../../../tests/acceptance/test_installed_questions.py).

**Data/security:** output is the application's public-field allowlist; settings and raw adapter exceptions are not printed. No `.env` loading, model, audio, daemon, approvals, or events exist yet. Registration is deliberate local CLI behavior, not an authorization bypass for future arbitrary commands.

**Owning milestones:** M01 CLI; M06/M07 desktop/voice; M11 MCP; M15 general protocol/Pi.

**Validation:** [unit CLI tests](../../../tests/unit/test_cli.py), [installed checks](../../../tests/acceptance/README.md), and [architecture tests](../../../tests/architecture/README.md). Run `.venv/bin/python -m pytest tests/unit tests/acceptance tests/architecture -q` from the source root. Future clients must use application contracts and preserve exact targeting, error, cancellation, and approval semantics. See [package map](../README.md).
