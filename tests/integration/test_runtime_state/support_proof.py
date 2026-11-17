"""Exact namespace metadata/content snapshot for no-mutation proof assertions."""

import hashlib
from pathlib import Path


def proof_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_nlink,
            path.lstat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else None,
        )
        for path in root.rglob("*")
    }
