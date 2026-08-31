"""Building fstab entries for network shares (SMB/CIFS and NFS).

The defaults here are chosen so that a NAS which happens to be switched
off costs nothing at boot. A plain network entry makes systemd wait on
the mount and then fail remote-fs.target, which can drop the machine to
an emergency shell -- the classic "my NAS was off and now my laptop
won't boot". Even `nofail` only downgrades that to a timeout stall.

`noauto,x-systemd.automount` avoids the problem entirely: systemd mounts
the share on first access rather than at boot, so an unreachable server
is simply a folder that isn't populated yet.
"""

import os
import shutil
import subprocess
from typing import List, NamedTuple, Optional

from .devices import _invoking_user_ids

SMB = "cifs"
NFS = "nfs"

# Mounted on first access instead of at boot, unmounted again after a
# minute idle, and ordered after the network is actually up.
BASE_NETWORK_OPTIONS = ("noauto", "x-systemd.automount", "x-systemd.idle-timeout=60", "_netdev")

# Where a credentials file goes. Root-owned, chmod 600 -- fstab itself is
# world-readable (mode 644), so a password must never be written into it.
CREDENTIALS_DIR = "/etc/samba/credentials"

# The package that provides each mount helper, per distro family. Without
# it, mount fails with a bare "unknown filesystem type".
HELPER_PACKAGES = {
    SMB: {"binary": "mount.cifs", "arch": "cifs-utils", "debian": "cifs-utils", "fedora": "cifs-utils"},
    NFS: {"binary": "mount.nfs", "arch": "nfs-utils", "debian": "nfs-common", "fedora": "nfs-utils"},
}


class NetworkShare(NamedTuple):
    kind: str                      # SMB or NFS
    server: str                    # hostname or IP
    share: str                     # SMB share name, or NFS export path
    mountpoint: str
    username: Optional[str] = None  # SMB only
    password: Optional[str] = None  # SMB only; never written to fstab


def helper_missing(kind: str) -> Optional[dict]:
    """The helper package info if the mount helper isn't installed, else None."""
    info = HELPER_PACKAGES.get(kind)
    if not info or shutil.which(info["binary"]):
        return None
    return info


def install_hint(kind: str) -> str:
    info = HELPER_PACKAGES[kind]
    return (
        f"`{info['binary']}` is missing, so this share can't be mounted until it's installed:\n"
        f"    Arch/CachyOS:   sudo pacman -S {info['arch']}\n"
        f"    Debian/Ubuntu:  sudo apt install {info['debian']}\n"
        f"    Fedora:         sudo dnf install {info['fedora']}"
    )


def sanitize_name(text: str) -> str:
    """A filesystem-safe token for building default paths from a share name."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in text.strip())
    return cleaned.strip("_.") or "share"


def credentials_path_for(share_name: str) -> str:
    return os.path.join(CREDENTIALS_DIR, sanitize_name(share_name))


def default_mountpoint(share: NetworkShare) -> str:
    # NFS exports are paths (/volume1/media), so use the last segment.
    leaf = share.share.rstrip("/").split("/")[-1] or share.server
    return f"/mnt/{sanitize_name(leaf)}"


def source_for(share: NetworkShare) -> str:
    if share.kind == SMB:
        return f"//{share.server.strip('/')}/{share.share.strip('/')}"
    export = share.share if share.share.startswith("/") else f"/{share.share}"
    return f"{share.server}:{export}"


def options_for(share: NetworkShare, credentials_path: Optional[str] = None,
                nfs_version: Optional[int] = None) -> str:
    options: List[str] = list(BASE_NETWORK_OPTIONS)

    # Only pin when the server can't do v4; a v4-capable server is
    # better left to negotiate so it can use the newest it supports.
    if share.kind == NFS and nfs_version and nfs_version < 4:
        options.append(f"nfsvers={nfs_version}")

    if share.kind == SMB:
        # SMB has no Linux ownership of its own, so without uid/gid the
        # share mounts owned by root and is read-only to the desktop user.
        uid, gid = _invoking_user_ids()
        if credentials_path:
            options.append(f"credentials={credentials_path}")
        else:
            options.append("guest")
        options += [f"uid={uid}", f"gid={gid}", "file_mode=0664", "dir_mode=0775"]

    return ",".join(options)


def credentials_content(username: str, password: str, domain: str = "") -> str:
    """The body of a CIFS credentials file, as mount.cifs expects it."""
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    return "\n".join(lines) + "\n"


def probe_nfs_version(server: str, timeout: float = 4.0) -> Optional[int]:
    """The highest NFS version `server` registers with rpcbind, or None.

    Worth asking, because mount(8) with no nfsvers= tries 4.2 and
    negotiates downward until something answers. Against a v3-only
    server that walk costs a round of failed attempts on every automount
    -- which the user feels directly, since the mount is triggered by
    opening the folder. Pinning the version skips it.

    Returns None when the answer is unclear (rpcinfo missing, server
    unreachable, or a v4-only server, which needs no rpcbind entry at
    all). Callers should then leave nfsvers= off and let mount
    negotiate, which is the safe default.
    """
    if not shutil.which("rpcinfo"):
        return None
    try:
        result = subprocess.run(
            ["rpcinfo", "-p", server], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    versions = []
    for line in result.stdout.splitlines():
        parts = line.split()
        # "100003  3  tcp  2049  nfs" -- program 100003 is NFS itself.
        if len(parts) >= 5 and parts[0] == "100003" and parts[-1] == "nfs":
            try:
                versions.append(int(parts[1]))
            except ValueError:
                continue
    return max(versions) if versions else None


def build_entry(share: NetworkShare, nfs_version: Optional[int] = None):
    """Turn a NetworkShare into (Entry, credentials_path_or_None).

    The returned Entry is safe to write to fstab: any password lives in
    the separate credentials file, never in the entry itself.
    """
    from .model import Entry

    credentials_path = None
    if share.kind == SMB and share.password:
        credentials_path = credentials_path_for(share.mountpoint.rstrip("/").split("/")[-1] or share.share)

    entry = Entry(
        device=source_for(share),
        mountpoint=share.mountpoint,
        fstype=share.kind,
        options=options_for(share, credentials_path, nfs_version),
        dump=0,
        passno=0,  # never fsck a network share
    )
    return entry, credentials_path
