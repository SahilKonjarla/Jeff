# Contract tests

**Status:** no separate reusable contract suite yet. Existing project/Git-read ports are exercised through unit, integration, and installed checks; do not run this empty directory alone expecting test evidence.

**Purpose:** verify concrete adapters satisfy the typed ports declared by application code. Define shared expectations for results, errors, cancellation, timeout, and ownership semantics as those ports become real.

**Dependencies:** application-owned contracts, domain values, and adapters under test. Adapters remain independent of clients and sibling integration implementations.

**Planned example:** a Git adapter returns an observed branch through the declared read port and reports unsupported/unavailable state honestly. The port and adapter now exist in [application questions](../../src/jeff/application/questions.py) and [Git integration](../../src/jeff/integrations/git.py); their current expectations are checked in [integration tests](../integration/test_git_questions.py).

**Owning milestones:** M01 first real adapter contracts; later integration milestones add required expectations.

Use safe fixtures and separately authorized external prerequisites. Do not bake provider credentials or environment-specific success into the contract. See the existing suites for actual evidence; this directory contributes no checks yet. See [test map](../README.md).
