# Jeff

A local developer assistant for macOS and Linux: one text and hold-to-talk voice control surface for your projects, coding sessions, and development tools.

> **Status: planning / pre-implementation.** This repository currently contains the product design, implementation plan, and project documentation. There is no installable Jeff package, working CLI, daemon, desktop app, or application test suite yet. All implementation milestones are not started.

## What Jeff is being built to do

- Answer project questions and supervise multiple projects through a shared local daemon.
- Run commands, pytest, development processes, and Docker workflows with explicit approvals.
- Launch and coordinate Pi coding sessions and specialized agents, with human-approved handoffs.
- Provide a PySide6 desktop console, macOS menu bar/Linux tray, and hold-to-talk voice interaction.
- Inspect Git, GitHub/CI, repository symbols, and explicitly registered SSH hosts.
- Connect to approved MCP servers over native stdio, Docker stdio, and authenticated remote HTTP.
- Retain redacted structured activity, recover jobs and watchers safely, and deliver local notifications.

These are planned requirements, not claims of available functionality. See the [design](docs/jeff-design.md) for the full scope and the [progress tracker](docs/jeff-progress.md) for implementation evidence.

## Local first, human supervised

Deterministic tools answer factual questions. Pi handles coding tasks; local models handle Jeff's reasoning. Local speech is the default, with cloud reasoning and speech fallback explicitly opt-in. Local-AI mode blocks cloud AI; fully offline mode also blocks remote integrations.

The microphone is off except during deliberate hold-to-talk capture. Raw audio is not retained, and voice transcripts stay in the active conversation. Commands require explicit, single-use authorization bound to their exact action and target. Models propose actions; they cannot approve them. Destructive production actions remain denied.

These are design requirements to implement and verify, not protections already provided by this repository.

## Architecture

The planned implementation is one Python package with a local asyncio daemon, SQLite persistence, authenticated Unix-domain socket IPC, and shared application contracts for every client.

| Layer | Responsibility |
| --- | --- |
| Domain | Pure rules, identities, events, and state transitions; standard library only |
| Application | Use cases, policy decisions, orchestration, and adapter ports |
| Infrastructure / integrations | Persistence, IPC, execution, and external tool/provider adapters |
| Interfaces | CLI, desktop, voice interaction, and MCP server clients of the application API |
| Bootstrap | Dependency wiring and lifecycle composition |

Inner layers do not import outer layers. Clients do not bypass policy to execute commands or access storage directly. Python 3.12+, Typer, Pydantic, PySide6, pytest, Ruff, and mypy are planned defaults; dependency versions and platform behavior still need validation.

## Getting started

There is nothing to install or run yet. The `docs/` directory is kept locally and excluded from Git, so documentation links below are available only in a checkout with those local files; they will not resolve in a fresh clone. Start with:

1. [Documentation index](docs/README.md) — where to find each source of truth.
2. [Product design](docs/jeff-design.md) — required behavior and boundaries.
3. [Implementation plan](docs/jeff-implementation-plan.md) — M01–M17 and acceptance scenarios.
4. [Progress tracker](docs/jeff-progress.md) — actual status, evidence, and next step.

The next implementation slice is **M01: install Jeff and ask a real project question**. Commands shown in the implementation plan describe the future CLI contract; they are not currently runnable Jeff instructions. Installation and development commands will be added here when packaging and tests exist and have been verified.

## Configuration and secrets

The design reserves ignored `jeff.local.yaml` for private project configuration and `.jeff/` for project runtime files. Credentials must use supported credential-store or explicitly selected environment-variable references, not committed values.

`.env` and `.env.*` are ignored, **except `.env.example`**, which is an empty, committed starting point for a future placeholder-only configuration template. No environment-variable contract exists yet; environment loading is not implemented. Never put real credentials in an example file.

Local documentation (`docs/`), agent-toolkit files and metadata, and virtual environments are ignored. Ignore rules do not remove files or submodules already tracked by Git.

## Scope boundaries

Voice, desktop, local reasoning, multi-project supervision, and every required MCP transport are part of the complete product, not optional future implementation work. Individual features may be configurable once working.

IDE-specific plugins, wake-word/toggle listening, vector/semantic history search, and remote notification delivery services are outside the current scope. Registered SSH administration is in scope, subject to exact per-command approval and host/privilege boundaries.

## Working on Jeff

Implement small, runnable milestones in plan order. Define acceptance scenarios before coding, update component READMEs with behavior, and record only checks actually run. Required real integrations and macOS/Linux verification cannot be replaced by mocked success. Major implementation stages require human review.

Broader context lives in [PROJECT_CONTEXT.md](docs/context/PROJECT_CONTEXT.md); canonical vocabulary lives in [DOMAIN_MODEL.md](docs/context/DOMAIN_MODEL.md). No distribution route or project license has been established yet.