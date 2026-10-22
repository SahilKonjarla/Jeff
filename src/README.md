# Source layout

**Status:** M01 registration and read-only branch/change questions implemented. Daemon/runtime behavior remains unimplemented.

`jeff/` contains the Python package; see its [tier map](jeff/README.md). Packaging and tooling live in [pyproject.toml](../pyproject.toml).

**Owning milestone:** M01. Mac CLI/demo behavior has automated and user-supplied manual evidence; formal milestone acceptance/M02 handoff is not automatic. Demo generation and bounded technical probes live outside the runtime package. Native local validation is recorded; hosted CI is not running and Linux verification is deferred.

**Implemented flow:** the installed `jeff` script or `python -m jeff` calls bootstrap, which injects discovery/storage into the application and CLI. Help does no project I/O; explicit project commands bind a canonical Git root, initialize or inspect declared configuration, and render public-safe typed identity results. `ask` adds deterministic question routing through a Git-read port, with protected-path filtering, escaped metadata, bounded Git subprocesses, and timestamped results. No providers, daemon, or arbitrary user-command execution is started; internal Git reads use narrow validated argv.

**Dependencies/security:** only entry points import bootstrap; inner tiers cannot import outer implementations. Domain remains standard-library-only. The [architecture tests](../tests/architecture/README.md) enforce static import directions, not runtime isolation.

**Validation and extension:** use the exact install/check commands and evidence distinctions in the [root README](../README.md). Pytest temporary working directories must be genuinely outside Git repositories, not just ignored inside a checkout. Shipped CLI behavior is separate from the experimental audio/model/global-hotkey probes. Add real domain/application/adapter files only with working behavior and update their READMEs. The detailed design/plan remain under ignored local `docs/` and may be absent in a fresh clone.
