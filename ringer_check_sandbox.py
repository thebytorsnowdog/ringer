"""OS confinement for commands that can execute worker-authored code."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

from ringer_file_scope import absolute_parts


class SandboxUnavailable(OSError):
    """The verifier could not establish its execution boundary."""


def _linux_network_filter(fd: int) -> None:
    # Deny socket creation even where an enclosing container forbids setting up
    # a separate network namespace. Anonymous socketpairs remain available for
    # local IPC. io_uring can otherwise create sockets without socket(2).
    try:
        lib = ctypes.CDLL("libseccomp.so.2")
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_rule_add.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint
        ]
        lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        rules = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
        if not rules:
            raise SandboxUnavailable("cannot allocate seccomp filter")
        try:
            for name in (b"socket", b"socketcall", b"io_uring_setup"):
                number = lib.seccomp_syscall_resolve_name(name)
                if number == -1 or lib.seccomp_rule_add(
                    rules, 0x00050000 | errno.EPERM, number, 0  # SCMP_ACT_ERRNO
                ) != 0:
                    raise SandboxUnavailable(f"cannot block {name.decode()} with seccomp")
            if lib.seccomp_export_bpf(rules, fd) != 0:
                raise SandboxUnavailable("cannot export seccomp filter")
            os.lseek(fd, 0, os.SEEK_SET)
        finally:
            lib.seccomp_release(rules)
    except (AttributeError, OSError) as exc:
        raise SandboxUnavailable(f"Linux verification requires a working libseccomp: {exc}") from exc


def _linux_command(command, cwd, scratch, read_mounts, write_mounts, stack):
    bwrap = next((path for path in ("/usr/bin/bwrap", "/bin/bwrap") if os.access(path, os.X_OK)), None)
    if bwrap is None:
        raise SandboxUnavailable("Linux verification requires bubblewrap (bwrap)")
    rules = stack.enter_context(tempfile.TemporaryFile())
    _linux_network_filter(rules.fileno())
    # No host root, home, /run, or system device tree in the mount namespace.
    argv = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--cap-drop", "ALL", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc",
            "--dev", "/dev", "--tmpfs", "/tmp"]
    for name in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(name):
            argv.extend(("--symlink", os.readlink(name), name))
        elif os.path.isdir(name):
            argv.extend(("--ro-bind", name, name))
    for name in ("/etc/ld.so.cache", "/etc/alternatives"):
        argv.extend(("--ro-bind-try", name, name))

    fds = [rules.fileno()]
    writable = dict(write_mounts)
    readable = dict(read_mounts)
    for path in sorted(readable.keys() | writable.keys(), key=lambda item: (len(item.parts), item)):
        if path == Path("/") or path.parts[1:2] in {("proc",), ("dev",)}:
            raise SandboxUnavailable(f"invalid verification input root: {path}")
        if path in writable:
            argv.extend(("--bind", str(writable[path]), str(path)))
        else:
            fd = readable[path]
            argv.extend(("--ro-bind", f"/proc/self/fd/{fd}", str(path)))
            fds.append(fd)
    argv.extend(("--chdir", str(cwd), "--seccomp", str(rules.fileno())))
    for name, value in sandbox_environment(scratch).items():
        argv.extend(("--setenv", name, value))
    return argv + ["--", *command], tuple(fds)


def sandbox_environment(scratch: Path) -> dict[str, str]:
    return {
        "PATH": (str(Path(sys.base_prefix) / "bin") + ":/opt/homebrew/bin:" if sys.platform == "darwin" else "") + "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch),
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _mac_command(command, scratch, read_mounts):
    launcher = "/usr/bin/sandbox-exec"
    if not os.access(launcher, os.X_OK):
        raise SandboxUnavailable("macOS verification requires Seatbelt (sandbox-exec)")
    # Pass paths as parameters so even unusual file names cannot alter rules.
    profile = [
        "(version 1)", "(deny default)", "(deny network*)", "(allow process-exec process-fork)",
        "(allow sysctl-read)", "(allow mach-lookup (global-name \"com.apple.system.logger\"))",
        '(allow file-read* file-map-executable (subpath "/System/Library") (subpath "/usr") (subpath "/bin")'
        ' (subpath "/sbin") (subpath "/Library/Apple/System") (subpath "/opt/homebrew")'
        ' (subpath "/Library/Developer/CommandLineTools")'
        ' (literal "/private/etc/localtime") (literal "/dev/null")'
        ' (literal "/dev/random") (literal "/dev/urandom"))',
        '(allow file-read* file-write* (subpath (param "SCRATCH")))',
        '(allow file-write-data (literal "/dev/null"))',
    ]
    if Path("/System/Library/Sandbox/Profiles/dyld-support.sb").is_file():
        profile.insert(2, '(import "dyld-support.sb")')
    params = ["-D", f"SCRATCH={scratch}"]
    profile.append('(allow file-read* (subpath "/private/var/select") (subpath "/private/var/db/xcode_select_link"))')
    profile.append('(allow file-read* (literal "/dev/stdin") (literal "/dev/fd") (regex #"^/dev/fd/[0-9]+$"))')
    directories = {scratch, Path("/private/var/select")}
    # Use the same interpreter Ringer runs under, including a standalone uv install.
    for part in ("bin", "lib"):
        runtime = Path(sys.base_prefix).resolve() / part
        key = "PYTHON_" + part.upper()
        params.extend(("-D", f"{key}={runtime}"))
        profile.append(f'(allow file-read* file-map-executable (subpath (param "{key}")))')
        directories.add(runtime)
    framework = Path(sys.base_prefix).resolve() / "Python"
    if framework.is_file() and "Python.framework" in framework.parts:
        params.extend(("-D", f"PYTHON_FRAMEWORK={framework}"))
        profile.append('(allow file-read* file-map-executable (literal (param "PYTHON_FRAMEWORK")))')
    for index, (path, fd) in enumerate(read_mounts):
        key = f"INPUT_{index}"
        match = "subpath" if stat.S_ISDIR(os.fstat(fd).st_mode) else "literal"
        profile.append(f'(allow file-read* ({match} (param "{key}")))')
        params.extend(("-D", f"{key}={Path('/', *absolute_parts(path))}"))
        directories.add(path)
        if match == "literal":
            # Python locates explicitly granted modules by listing their parent.
            params.extend(("-D", f"{key}_PARENT={path.parent}"))
            profile.append(f'(allow file-read-data (literal (param "{key}_PARENT")))')
    for index, parent in enumerate(sorted({ancestor for d in directories for ancestor in d.parents})):
        key = f"ANCESTOR_{index}"
        params.extend(("-D", f"{key}={parent}"))
        profile.append(f'(allow file-read-metadata file-read-data (literal (param "{key}")))')
    return [launcher, *params, "-p", "\n".join(profile), *command], ()


@contextlib.contextmanager
def sandbox_command(command, *, cwd: Path, scratch: Path, read_mounts=(), work_mount=None,
                    output_mounts=()):
    """Yield an isolated argv, inherited descriptors, environment and host cwd."""
    if isinstance(command, str):
        command = ["/bin/sh", "-c", command]
    with contextlib.ExitStack() as stack:
        if sys.platform == "linux":
            writable = [(scratch, scratch), *output_mounts]
            if work_mount is not None:
                writable.append((cwd, work_mount))
            argv, fds = _linux_command(command, cwd, scratch, read_mounts, writable, stack)
            yield argv, fds, sandbox_environment(scratch), Path("/")
        elif sys.platform == "darwin":
            argv, fds = _mac_command(command, scratch, read_mounts)
            yield argv, fds, sandbox_environment(scratch), work_mount or cwd
        else:
            raise SandboxUnavailable(f"no verification sandbox for {sys.platform}")
