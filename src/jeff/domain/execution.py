"""Pure public-action bounds and review states; these never grant launch authority."""

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

MANIFEST_BYTES = 32768
ARGV_BYTES = 8192
ARGV_COUNT = 64
REVIEW_TTL_SECONDS = 600
LIVE_REVIEW_LIMIT = 16
REVIEW_RECORD_LIMIT = 5000
REVIEW_PAGE_LIMIT = 32


class ReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


def public_text(value: str, maximum: int = 4096) -> str:
    """Conservative channel checks, not universal secret-content detection.

    Operators must supply non-secret declarations. Unknown secret values cannot be
    identified reliably; no credential/reference resolution exists in this phase.
    """
    if len(value.encode("utf-8")) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Public action text exceeds its bounds.")
    if re.search(
        r"(?i)(?:--?(?:password|passwd|token|api[-_]?key|secret|credential)(?:[=\s]|$)"
        r"|(?:password|token|api[-_]?key|secret|credential)\s*="
        r"|authorization\s*:|bearer\s+|-----BEGIN .*PRIVATE KEY"
        r"|[a-z][a-z0-9+.-]*://[^/\s]*@)",
        value,
    ):
        raise ValueError("Sensitive argument channels are not supported.")
    return value


def canonical_bytes(value: Any) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    if len(encoded) > MANIFEST_BYTES:
        raise ValueError("Action manifest exceeds its encoded bound.")
    return encoded


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
