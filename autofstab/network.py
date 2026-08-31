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
import socket
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


# --- discovery ---------------------------------------------------------
#
# Finding a NAS reliably needs more than one method. mDNS only sees
# servers that advertise; NetBIOS needs SMB1-era broadcast, which modern
# NAS boxes often disable; and the ARP neighbour table only lists hosts
# this machine has already talked to, so a freshly powered-on NAS is
# invisible to it. A short connect-scan of the local subnet is the only
# approach that reliably finds one, so it's offered as an explicit,
# user-triggered action rather than something the app does on its own.

SERVICE_PORTS = {445: SMB, 2049: NFS}


class DiscoveredServer(NamedTuple):
    host: str
    services: List[str]   # SMB and/or NFS
    name: Optional[str] = None


def local_subnets() -> List[str]:
    """Local IPv4 /24-or-smaller networks, as 'a.b.c.' prefixes.

    Anything wider than a /24 is skipped: scanning it would mean tens of
    thousands of connections, which is neither quick nor neighbourly.
    """
    prefixes = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return prefixes

    for line in out.splitlines():
        for token in line.split():
            if "/" not in token:
                continue
            address, _, bits = token.partition("/")
            if address.count(".") != 3 or not bits.isdigit():
                continue
            if int(bits) < 24:
                continue
            prefixes.append(address.rsplit(".", 1)[0] + ".")
            break
    return sorted(set(prefixes))


def _port_open(host: str, port: int, timeout: float) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def netbios_name(host: str, timeout: float = 3.0) -> Optional[str]:
    """The NetBIOS name of a host, for showing something friendlier than an IP."""
    if not shutil.which("nmblookup"):
        return None
    try:
        out = subprocess.run(
            ["nmblookup", "-A", host], capture_output=True, text=True, timeout=timeout
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        # "GRINGOTTS  <20> - B <ACTIVE>" -- <20> is the file-server service.
        if len(parts) >= 2 and parts[1] == "<20>" and not parts[0].startswith("."):
            return parts[0]
    return None


def discover_servers(timeout: float = 0.6, workers: int = 128) -> List[DiscoveredServer]:
    """Find hosts on the local network answering on an SMB or NFS port."""
    from concurrent.futures import ThreadPoolExecutor

    targets = []
    for prefix in local_subnets():
        for last in range(1, 255):
            for port in SERVICE_PORTS:
                targets.append((f"{prefix}{last}", port))
    if not targets:
        return []

    found = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda t: (t, _port_open(t[0], t[1], timeout)), targets)
        for (host, port), is_open in results:
            if is_open:
                found.setdefault(host, set()).add(SERVICE_PORTS[port])

    servers = []
    for host in sorted(found, key=lambda h: [int(p) for p in h.split(".")]):
        servers.append(DiscoveredServer(host, sorted(found[host]), netbios_name(host)))
    return servers


def list_nfs_exports(host: str, timeout: float = 8.0) -> List[str]:
    """Export paths the NFS server offers. Needs no authentication."""
    if not shutil.which("showmount"):
        return []
    try:
        result = subprocess.run(
            ["showmount", "-e", "--no-headers", host],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    exports = []
    for line in result.stdout.splitlines():
        path = line.split()[0] if line.split() else ""
        if path.startswith("/"):
            exports.append(path)
    return exports


def list_smb_shares(host: str, username: str = "", password: str = "",
                    timeout: float = 15.0) -> List[str]:
    """Share names the SMB server offers.

    Many servers refuse anonymous enumeration, in which case credentials
    are required and an empty list comes back. Administrative shares
    (IPC$, print$, and other $-suffixed ones) are filtered out -- they
    aren't things anyone wants in fstab.
    """
    if not shutil.which("smbclient"):
        return []

    command = ["smbclient", "-L", f"//{host}", "-g"]
    env = dict(os.environ)
    if username:
        command += ["-U", username]
        # Passed via the environment rather than argv, so it never shows
        # up in `ps` output.
        env["PASSWD"] = password or ""
    else:
        command.append("-N")

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return []

    shares = []
    for line in result.stdout.splitlines():
        # -g gives machine-readable "Disk|name|comment" records.
        parts = line.split("|")
        if len(parts) >= 2 and parts[0].strip().lower() == "disk":
            name = parts[1].strip()
            if name and not name.endswith("$"):
                shares.append(name)
    return shares
