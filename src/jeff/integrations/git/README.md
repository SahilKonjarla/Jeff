# Read-only Git adapter

Discovery/repository modules bind canonical registered roots and implement fixed branch/change metadata queries; parsing, limits and process helpers enforce bounded safe rendering and native subprocess policy. Concrete I/O depends inward on application/domain contracts.

No raw content/diff, provider, write/index mutation or arbitrary argv. Protected paths/submodules are excluded; filter-backed change reads refuse rather than running conversion drivers. A filtered visible-clean answer is not complete repository-clean proof. Hostile-same-UID isolation and atomic repository snapshots are not claimed.

[Worker](worker/README.md) owns actual Git-child lifetime; [integration checks](../../../../tests/integration/test_git_lifetime/README.md) distinguish protocol fakes from captured process behavior. Docker jobs belong to infrastructure/application ports, not this Git adapter. See [integration tier](../README.md).
