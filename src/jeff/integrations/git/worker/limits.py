"""Private inherited-channel protocol budgets and finite filter-name policy."""

from __future__ import annotations

_REQUEST_LIMIT = 2 * 1024 * 1024


_FRAME_LIMIT = 96 * 1024


_FILTER = r"^filter\..*\.(clean|process|required)$"
