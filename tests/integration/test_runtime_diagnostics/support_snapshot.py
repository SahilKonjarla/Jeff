"""Canonical no-mutation snapshot, excluding credential contents exactly as before."""

import hashlib
import os
import stat

from jeff.application.runtime import RuntimeLayout


def snapshot(layout: RuntimeLayout) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path): (
            path.lstat().st_ino,
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_mtime_ns,
            path.lstat().st_nlink,
            os.readlink(path)
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and path != layout.credential
            else None,
        )
        for root in (layout.data_dir, layout.runtime_dir)
        for path in (root, *root.rglob("*"))
    }
