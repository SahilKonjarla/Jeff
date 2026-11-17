"""Default read-only Git budgets and the existing injected runner type."""

import subprocess
from collections.abc import Callable

GIT_TIMEOUT = 5.0


GIT_OUTPUT_LIMIT = 1024 * 1024


MAX_CHANGES = 10_000


ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]
