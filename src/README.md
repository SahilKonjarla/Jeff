# Source layout

**Status:** M01 registration and read-only branch/change questions implemented. Daemon/runtime behavior remains unimplemented.

`jeff/` contains the Python package; see its [tier map](jeff/README.md). Packaging and tooling live in [pyproject.toml](../pyproject.toml).

**Owning milestone:** M01. No milestone has been accepted. Demo generation and local fixture checks now exist outside the runtime package; technical probes, CI, and platform acceptance remain pending.

**Implemented flow:** the installed `jeff` script or `python -m jeff` calls bootstrap, which injects discovery/storage into the application and CLI. Help does no project I/O; explicit project commands bind a canonical Git root, initialize or inspect declared configuration, and render public-safe typed identity results. `ask` adds deterministic question routing through a Git-read port, with protected-path filtering, escaped metadata, bounded Git subprocesses, and timestamped results. No providers, daemon, or arbitrary user-command execution is started; internal Git reads use narrow validated argv.

**Dependencies/security:** only entry points import bootstrap; inner tiers cannot import outer implementations. Domain remains standard-library-only. The [architecture tests](../tests/architecture/README.md) enforce static import directions, not runtime isolation.

**Validation and extension:** use the exact install/check commands in the [root README](../README.md). Add real domain/application/adapter files only with working behavior and update their READMEs. The detailed design/plan remain under ignored local `docs/` and may be absent in a fresh clone.
