"""Bounded, workspace-local cache for prepared vision images."""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_FILES = 128

def _cache_dir(workspace: Path) -> Path:
    base = workspace.expanduser().resolve()
    root = base / "tmp" / "context-images"
    # Refuse a pre-existing symlink: cache writes must not escape the workspace.
    if root.exists() and root.is_symlink():
        raise OSError("image cache directory is a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root:
        raise OSError("image cache directory escapes workspace")
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root

def _cleanup(directory: Path) -> None:
    files = [p for p in directory.iterdir() if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    files.sort(key=lambda p: p.stat().st_mtime_ns)
    while len(files) > MAX_CACHE_FILES or total > MAX_CACHE_BYTES:
        victim = files.pop(0)
        try:
            total -= victim.stat().st_size
            victim.unlink()
        except OSError:
            pass

def cache_prepared_image(raw: bytes, mime: str, workspace: Path, *, suffix: str = ".img") -> Path:
    """Atomically cache bounded prepared bytes inside *workspace*."""
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError("image exceeds cache limit")
    directory = _cache_dir(workspace)
    for _ in range(10):
        path = directory / f"{time.time_ns()}-{secrets.token_hex(8)}{suffix}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            _cleanup(directory)
            return path
        except FileExistsError:
            continue
    raise OSError("could not allocate unique image cache path")

def path_within_workspace(path: str | Path, workspace: Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(workspace.expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False
