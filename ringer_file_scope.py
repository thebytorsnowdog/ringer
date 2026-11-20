"""Descriptor-relative reads and publication for worker files."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path

MAX_OUTPUT_BYTES = 20 * 1024 * 1024


def absolute_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError(f"expected an absolute path without parent traversal: {path}")
    parts = path.parts[1:]
    if sys.platform == "darwin" and parts[:1] in {("tmp",), ("var",), ("etc",)}:
        # macOS ships these three system aliases. Do not resolve worker links.
        return ("private", *parts)
    return parts


def relative_parts(path: Path) -> tuple[str, ...]:
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise OSError(f"expected a path inside the task directory: {path}")
    return path.parts


def directory_flags() -> int:
    if os.open not in os.supports_dir_fd or not all(
        hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        raise OSError("safe worker file access requires descriptor-relative no-follow opens")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextlib.contextmanager
def descend(root_fd: int, parts: tuple[str, ...], *, create: bool = False):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_directory(path: Path, *, create: bool = False):
    fd = os.open("/", directory_flags())
    try:
        with descend(fd, absolute_parts(path), create=create) as directory:
            yield directory
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_input(path: Path):
    """Pin a declared read-only mount without traversing links."""
    absolute_parts(path)
    with open_directory(path.parent) as parent:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path.name, flags, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                raise OSError(f"expected a regular input file or directory: {path}")
            yield fd
        finally:
            os.close(fd)


def append_log(path: Path, data: bytes) -> None:
    with open_directory(path.absolute().parent, create=True) as parent:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path.name, flags, 0o600, dir_fd=parent)
        with os.fdopen(fd, "ab") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(f"expected a regular log file with a single link: {path}")
            stream.write(data)


@contextlib.contextmanager
def open_regular(root_fd: int, parts: tuple[str, ...], *, nonempty: bool = True):
    if not parts:
        raise OSError("expected a file")
    with descend(root_fd, parts[:-1]) as parent:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if not stat.S_ISREG(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
            raise OSError("expected a regular file")
        fd = os.open(parts[-1], flags, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("expected a regular file with a single link")
            if nonempty and not info.st_size:
                raise OSError("expected a non-empty regular file")
            yield stream, info


@contextlib.contextmanager
def open_expected(task_fd: int, value: str, external: frozenset[Path]):
    path = Path(value).expanduser()
    if path.is_absolute():
        if path not in external:
            raise OSError(f"external file is not declared: {path}")
        with open_directory(path.parent) as parent, open_regular(parent, (path.name,)) as opened:
            yield opened
    else:
        with open_regular(task_fd, relative_parts(path)) as opened:
            yield opened


def external_outputs(expected: tuple[str, ...]) -> frozenset[Path]:
    """Absolute entries in the coordinator's expect_files are exact grants."""
    return frozenset(path for value in expected if (path := Path(value).expanduser()).is_absolute())


@contextlib.contextmanager
def atomic_output(parent: int, name: str, *, mode: int = 0o600):
    temp_name = ".ringer-output-" + secrets.token_hex(16)
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            yield stream
        os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=parent)


def copy_output(source, info: os.stat_result, parent: int, name: str, *, mode: int = 0o600):
    """Hash the exact bytes copied from the validated descriptor."""
    if info.st_size > MAX_OUTPUT_BYTES:
        raise OSError(f"larger than 20 MB ({info.st_size:,} bytes)")
    digest = hashlib.sha256()
    size = 0
    with atomic_output(parent, name, mode=mode) as target:
        while chunk := source.read(128 * 1024):
            size += len(chunk)
            if size > info.st_size:
                raise OSError("source grew during copy")
            target.write(chunk)
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if size != info.st_size or after.st_nlink != 1 or (
            after.st_mtime_ns, after.st_ctime_ns
        ) != (info.st_mtime_ns, info.st_ctime_ns):
            raise OSError("source changed during copy")
    return size, digest.hexdigest()
