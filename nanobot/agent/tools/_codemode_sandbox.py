"""Linux isolation for codemode; never falls back to native execution."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import re
import signal
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_BWRAP_PATH = "/usr/bin/bwrap"


@dataclass(frozen=True)
class BwrapBinary:
    path: Path
    version: str
    apparmor_label: str

    @property
    def description(self) -> str:
        return f"Bubblewrap {self.version} at {self.path} (AppArmor: {self.apparmor_label})"


def _apparmor_userns_restricted() -> bool:
    try:
        return Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect AppArmor user-namespace restrictions: {exc}") from exc


async def _probe_bwrap(path: Path) -> tuple[str, str]:
    """Read the actual exec label before setup, not an inferred profile filename.

    --args blocks on our pipe until the exec transition has happened and its
    AppArmor label can be inspected. Then --version exits without namespaces.
    All pipes/processes are closed/reaped, including cancellation and timeout.
    """
    read_fd, write_fd = os.pipe()
    proc = None
    try:
        async with asyncio.timeout(5):
            proc = await asyncio.create_subprocess_exec(
                str(path), "--args", str(read_fd), pass_fds=(read_fd,),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, limit=4096,
            )
            os.close(read_fd)
            read_fd = -1
            try:
                label = Path(f"/proc/{proc.pid}/attr/apparmor/current").read_text().strip().rstrip("\0")
            except OSError as exc:
                label = f"unavailable ({exc})"
            try:
                os.write(write_fd, b"--version\0")
            except BrokenPipeError:
                # A wrong/broken executable may exit before reading --args;
                # report its stderr/exit status rather than a pipe symptom.
                pass
            os.close(write_fd)
            write_fd = -1
            stdout = await proc.stdout.readline()
            stderr = await proc.stderr.read(4096)
            await proc.wait()
            if proc.returncode:
                raise RuntimeError(
                    f"Cannot validate Bubblewrap at {path}: --version probe exited {proc.returncode}: "
                    f"{stderr.decode(errors='replace').strip() or 'no diagnostic'}"
                )
            return stdout.decode(errors="replace").strip(), label
    except TimeoutError as exc:
        raise RuntimeError(f"Bubblewrap validation timed out after 5s for {path}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot execute Bubblewrap validation for {path}: {exc}") from exc
    finally:
        for fd in (read_fd, write_fd):
            if fd >= 0:
                os.close(fd)
        if proc is not None:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()


async def validate_bwrap(configured_path: str) -> BwrapBinary:
    """Resolve an explicit executable and validate version and active OS policy."""
    if sys.platform != "linux":
        raise RuntimeError("Python codemode requires Linux with Bubblewrap; no unsafe fallback")
    path = Path(configured_path)
    if not path.is_absolute() or "\0" in configured_path:
        raise RuntimeError("tools.codemode.bwrapPath must be an absolute executable path; PATH is not searched")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"Bubblewrap executable {configured_path} is unavailable: {exc}. "
            "Install bubblewrap there or set tools.codemode.bwrapPath; PATH is not searched."
        ) from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"Bubblewrap path {path} is not an executable file")
    version_output, label = await _probe_bwrap(path)
    match = re.fullmatch(r"bubblewrap (\d+)\.(\d+)\.(\d+)(?:[-+][^\s]+)?", version_output)
    if not match:
        raise RuntimeError(f"Unexpected --version from {path}: {version_output!r}; require Bubblewrap >= 0.9.0")
    version = ".".join(match.groups())
    if tuple(map(int, match.groups())) < (0, 9, 0):
        raise RuntimeError(f"Unsupported Bubblewrap {version} at {path}; require >= 0.9.0 for this sandbox")
    binary = BwrapBinary(path, version, label)
    restricted = _apparmor_userns_restricted()
    if restricted and (not label or label.startswith("unavailable")):
        raise RuntimeError(
            f"{binary.description}: cannot validate the active AppArmor profile while "
            "kernel.apparmor_restrict_unprivileged_userns=1. No fallback was attempted."
        )
    if restricted and (label == "unconfined" or label.startswith("unprivileged_userns")):
        raise RuntimeError(
            f"{binary.description}: no user-namespace-authorized AppArmor profile is attached. "
            "kernel.apparmor_restrict_unprivileged_userns=1 strips setup capabilities from unprofiled binaries. "
            "Set tools.codemode.bwrapPath to an already profiled executable, or have an administrator attach/load a userns-allowing "
            f"profile for this exact executable ({path}); a profile for another path does not cover it. "
            "No fallback was attempted."
        )
    return binary


def worker_exit_error(binary: BwrapBinary, returncode: int, stderr: str) -> RuntimeError:
    """Report the selected binary and the failed setup stage, not a blanket AA hint."""
    diagnostic = stderr.strip() or "no diagnostics"
    if "loopback:" in stderr and ("Operation not permitted" in stderr or "Permission denied" in stderr):
        cause = (
            "Private-network loopback setup was denied despite requested CAP_NET_ADMIN. "
            "The selected AppArmor profile or outer container policy denies the required setup capability."
        )
    elif "namespace" in stderr.lower() and ("Operation not permitted" in stderr or "Permission denied" in stderr):
        cause = "Namespace creation/mapping was denied by the active AppArmor profile or outer OS/container policy."
    elif "Unknown option" in stderr or "unknown option" in stderr:
        cause = "This binary does not support a required sandbox option."
    elif returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        cause = f"Worker terminated by signal {name}; this is not evidence of an AppArmor setup failure."
    else:
        cause = "Worker exited without reporting script completion."
    return RuntimeError(
        f"{binary.description}: {cause} Exit {returncode}: {diagnostic}. No unsandboxed fallback was used."
    )


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
def sandbox_command(binary: BwrapBinary) -> Iterator[tuple[list[str], int]]:
    """Yield argv and the sole extra inherited FD for a fresh, offline worker.

    The writable tmpfs is size-limited. The root and runtime mounts are read-only;
    neither the workspace nor the parent's environment is exposed. The seccomp
    filter prevents multiplying per-process resource limits via fork/threads.
    """
    python = Path(sys.executable).resolve()
    prefix = Path(sys.base_prefix).resolve()
    worker = Path(__file__).with_name("_codemode_worker.py")
    with tempfile.TemporaryFile() as seccomp:
        _export_seccomp(seccomp.fileno())
        seccomp.seek(0)
        args = [
            str(binary.path), "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-net",
            "--unshare-uts", "--new-session", "--die-with-parent",
            # Modern bwrap configures lo during setup. SETPCAP lets the trusted
            # worker clear even the bounding set before compiling generated code.
            "--cap-drop", "ALL", "--cap-add", "CAP_NET_ADMIN", "--cap-add", "CAP_SETPCAP",
            "--clearenv",
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
