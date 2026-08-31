"""Fallback for writing to files that need root (like /etc/fstab) via
pkexec, so a GUI user gets the system's native graphical password prompt
instead of needing a terminal and sudo.

Only the narrow backup+write operation runs as root -- not the whole app --
via a small helper script passed to `pkexec python3 -c ...`. The new
content is fed over stdin rather than passed as an argument, keeping it
out of process listings (`ps`) and away from any argv-length limit.
"""

import json
import shutil
import subprocess
import sys
from typing import List, NamedTuple, Optional, Tuple

_HELPER_SCRIPT = """
import sys, os, shutil, datetime
target = sys.argv[1]
content = sys.stdin.read()
backup_path = ""
if os.path.exists(target):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target + ".bak." + ts
    shutil.copy2(target, backup_path)
tmp_path = target + ".autofstab.tmp"
with open(tmp_path, "w") as f:
    f.write(content)
os.replace(tmp_path, target)
sys.stdout.write(backup_path)
"""

_MOUNT_SCRIPT = """
import sys, os, json, subprocess
targets = json.load(sys.stdin)
created, mounted, failed = [], [], []
for target in targets:
    if not os.path.isdir(target):
        try:
            os.makedirs(target, exist_ok=True)
            created.append(target)
        except OSError as e:
            failed.append([target, "couldn't create the folder: %s" % e])
            continue
    # Mount by mountpoint so mount(8) reads the options straight from
    # fstab -- this only ever mounts what fstab already describes.
    proc = subprocess.run(["mount", target], capture_output=True, text=True, timeout=60)
    if proc.returncode == 0:
        mounted.append(target)
    else:
        # mount(8) puts the real reason on the first line and follows it
        # with boilerplate ("dmesg(1) may have more information..."), so
        # take the first useful line rather than the last.
        lines = [l.strip() for l in (proc.stderr or proc.stdout).splitlines() if l.strip()]
        lines = [l for l in lines if "dmesg(1)" not in l]
        reason = lines[0] if lines else "mount failed"
        for prefix in ("mount: %s: " % target, "mount: ", "%s: " % target):
            if reason.startswith(prefix):
                reason = reason[len(prefix):]
                break
        failed.append([target, reason])
sys.stdout.write(json.dumps({"created": created, "mounted": mounted, "failed": failed}))
"""

_NO_PKEXEC_MESSAGE = (
    "pkexec is not installed, so a graphical privilege prompt isn't available. "
    "Re-run this app with sudo instead."
)


class PrivilegedWriteResult(NamedTuple):
    ok: bool
    backup_path: Optional[str]
    error: Optional[str]
    cancelled: bool


class AuthResult(NamedTuple):
    ok: bool
    error: Optional[str]
    cancelled: bool


def authenticate_via_pkexec() -> AuthResult:
    """Trigger a real polkit authentication prompt with no side effects --
    used to gate unlocking system-critical fstab entries for editing. Runs
    `true` as root purely so pkexec has to authenticate; nothing is written
    or changed."""
    if not shutil.which("pkexec"):
        return AuthResult(False, _NO_PKEXEC_MESSAGE, False)

    try:
        result = subprocess.run(["pkexec", "true"], capture_output=True, text=True)
    except FileNotFoundError:
        return AuthResult(False, _NO_PKEXEC_MESSAGE, False)

    if result.returncode == 126:
        return AuthResult(False, None, True)
    if result.returncode != 0:
        error = result.stderr.strip() or f"pkexec exited with status {result.returncode}"
        return AuthResult(False, error, False)

    return AuthResult(True, None, False)


class MountResult(NamedTuple):
    ok: bool
    created: List[str]
    mounted: List[str]
    failed: List[Tuple[str, str]]
    error: Optional[str]
    cancelled: bool


def mount_with_pkexec(mountpoints: List[str], timeout: float = 120.0) -> MountResult:
    """Create any missing mount point folders, then mount each target.

    Mounts by mount point rather than device, so mount(8) takes the
    options from fstab itself -- nothing here can mount something the
    saved file doesn't already describe.
    """
    if not mountpoints:
        return MountResult(True, [], [], [], None, False)

    if not shutil.which("pkexec"):
        return MountResult(False, [], [], [], _NO_PKEXEC_MESSAGE, False)

    try:
        result = subprocess.run(
            ["pkexec", sys.executable, "-c", _MOUNT_SCRIPT],
            input=json.dumps(mountpoints),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return MountResult(False, [], [], [], _NO_PKEXEC_MESSAGE, False)
    except subprocess.TimeoutExpired:
        return MountResult(
            False, [], [], [],
            "Mounting timed out. A network share or an unresponsive drive can hang this.",
            False,
        )

    if result.returncode == 126:
        return MountResult(False, [], [], [], None, True)
    if result.returncode != 0:
        error = result.stderr.strip() or f"pkexec exited with status {result.returncode}"
        return MountResult(False, [], [], [], error, False)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return MountResult(False, [], [], [], "Couldn't read the result of the mount attempt.", False)

    failed = [(str(t), str(m)) for t, m in payload.get("failed", [])]
    return MountResult(
        not failed,
        payload.get("created", []),
        payload.get("mounted", []),
        failed,
        None,
        False,
    )


def write_with_pkexec(path: str, content: str) -> PrivilegedWriteResult:
    """Back up and write `content` to `path` as root via pkexec."""
    if not shutil.which("pkexec"):
        return PrivilegedWriteResult(False, None, _NO_PKEXEC_MESSAGE, False)

    try:
        result = subprocess.run(
            ["pkexec", sys.executable, "-c", _HELPER_SCRIPT, path],
            input=content,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return PrivilegedWriteResult(False, None, _NO_PKEXEC_MESSAGE, False)

    if result.returncode == 126:
        # Polkit's own convention: the user dismissed/cancelled the auth prompt.
        return PrivilegedWriteResult(False, None, None, True)
    if result.returncode != 0:
        error = result.stderr.strip() or f"pkexec exited with status {result.returncode}"
        return PrivilegedWriteResult(False, None, error, False)

    return PrivilegedWriteResult(True, result.stdout.strip() or None, None, False)
