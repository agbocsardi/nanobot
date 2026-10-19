"""Linux isolation for codemode; never falls back to native execution."""

from __future__ import annotations

import ctypes
import errno
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _export_seccomp(fd: int) -> None:
    """Deny process creation and sensitive kernel APIs, including alternate ABIs."""
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise RuntimeError("Codemode requires libseccomp (libseccomp2 on Debian/Ubuntu)") from exc
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_export_bpf.restype = ctypes.c_int
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; other architectures default to kill.
    if not ctx:
        raise RuntimeError("Could not allocate codemode seccomp filter")
    try:
        for name in (
            "clone", "clone3", "fork", "vfork", "unshare", "setns", "ptrace",
            "process_vm_readv", "process_vm_writev", "pidfd_getfd", "bpf",
            "perf_event_open", "userfaultfd", "io_uring_setup", "keyctl",
            "socket", "connect",
        ):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0):
                raise RuntimeError(f"Could not restrict syscall {name}")
        if lib.seccomp_export_bpf(ctx, fd):
            raise RuntimeError("Could not export codemode seccomp filter")
    finally:
        lib.seccomp_release(ctx)


@contextmanager
def sandbox_command() -> Iterator[tuple[list[str], int]]:
    """Yield argv and the sole extra inherited FD for a fresh, offline worker.

    The writable tmpfs is size-limited. The root and runtime mounts are read-only;
    neither the workspace nor the parent's environment is exposed. The seccomp
    filter prevents multiplying per-process resource limits via fork/threads.
    """
    if sys.platform != "linux":
        raise RuntimeError("Python codemode requires Linux with Bubblewrap; no unsafe fallback")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Python codemode requires Bubblewrap: install the bubblewrap package")
    python = Path(sys.executable).resolve()
    prefix = Path(sys.base_prefix).resolve()
    worker = Path(__file__).with_name("_codemode_worker.py")
    with tempfile.TemporaryFile() as seccomp:
        _export_seccomp(seccomp.fileno())
        seccomp.seek(0)
        args = [
            bwrap, "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-net",
            "--unshare-uts", "--new-session", "--die-with-parent",
            "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
        ]
        for root in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(root).exists():
                args += ["--ro-bind", root, root]
        if not prefix.is_relative_to("/usr") and prefix not in (Path("/"), Path("/bin")):
            args += ["--ro-bind", str(prefix), str(prefix)]
        args += [
            "--ro-bind", str(worker), "/worker.py",
            "--proc", "/proc", "--remount-ro", "/proc", "--dir", "/dev",
            "--dev-bind", "/dev/null", "/dev/null",
            "--dev-bind", "/dev/urandom", "/dev/urandom",
            "--size", str(16 * 1024 * 1024), "--tmpfs", "/tmp",
            "--chdir", "/tmp", "--remount-ro", "/",
            "--seccomp", str(seccomp.fileno()),
            "--", str(python), "-I", "-S", "-B", "/worker.py",
        ]
        yield args, seccomp.fileno()
