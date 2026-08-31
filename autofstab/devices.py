"""Block device discovery via lsblk, for picking devices interactively."""

import json
import os
import subprocess
from typing import Dict, List, Optional


def list_block_devices() -> List[Dict]:
    """Return lsblk nodes that carry a filesystem (skip bare disks/partitions with none)."""
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-O"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("lsblk not found — install util-linux to use device detection.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"lsblk failed: {e.stderr.strip()}")

    data = json.loads(result.stdout)
    devices: List[Dict] = []

    def walk(nodes, parent_tran=None, parent_disk=None):
        for node in nodes:
            # lsblk only reports "tran" (nvme/sata/usb/...) on the whole disk,
            # not on its partitions, so inherit it down to children here.
            node["tran"] = node.get("tran") or parent_tran
            # Track which physical disk each partition belongs to (a disk's
            # own parent_disk is itself) -- lets callers tell that e.g. a
            # /boot partition and the root partition share the same drive
            # even though they're different UUIDs.
            node["parent_disk"] = parent_disk or node.get("name")
            devices.append(node)
            if node.get("children"):
                walk(node["children"], node["tran"], node["parent_disk"])

    walk(data.get("blockdevices", []))
    return [d for d in devices if d.get("fstype")]


TRANSPORT_LABELS = {
    "nvme": "NVMe",
    "sata": "SATA",
    "usb": "USB",
    "mmc": "MMC/SD",
    "sas": "SAS",
    "ata": "ATA",
    "virtio": "Virtio",
    "iscsi": "iSCSI",
}


def describe_transport(tran: Optional[str]) -> str:
    if not tran:
        return "Unknown"
    return TRANSPORT_LABELS.get(tran, tran.upper())


def resolve_device_index() -> Dict[str, Dict]:
    """Map fstab-style source strings (UUID=..., LABEL=..., /dev/name) to the
    matching lsblk node, for currently connected devices. Sources that can't
    be resolved (disconnected drives, network shares, tmpfs, swap, etc.)
    simply won't appear in the result."""
    index: Dict[str, Dict] = {}
    try:
        nodes = list_block_devices()
    except RuntimeError:
        return index

    for node in nodes:
        name = node.get("name")
        if name:
            index[f"/dev/{name}"] = node
        uuid = node.get("uuid")
        if uuid:
            index[f"UUID={uuid}"] = node
        lbl = node.get("label")
        if lbl:
            index[f"LABEL={lbl}"] = node

    return index


def display_name(node: Optional[Dict], fallback: str) -> str:
    """A short name for a device the way a file browser would show it:
    its volume label if it has one, otherwise size + filesystem, otherwise
    the given fallback (typically the raw fstab source string)."""
    if not node:
        return fallback
    label = node.get("label")
    if label:
        return label
    bits = [b for b in (node.get("size"), node.get("fstype")) if b]
    return " ".join(bits) if bits else fallback


def pending_mounts(entries) -> List:
    """Entries that fstab says should be mounted but currently aren't.

    Skips the ones mounting doesn't apply to: swap (activated with
    swapon, not mount), pseudo-targets like "none", and plain `noauto`
    entries, which opt out of being mounted. Entries combining `noauto`
    with `x-systemd.automount` are included -- those are meant to be
    mounted, just on demand.
    """
    pending = []
    for entry in entries:
        if entry.mountpoint in ("none", "swap", ""):
            continue
        if (entry.fstype or "").lower() == "swap":
            continue
        options = [o.strip() for o in (entry.options or "").split(",")]
        # noauto means "don't mount at boot", not "never mount". An
        # x-systemd.automount entry is meant to be mounted, just lazily --
        # and mounting it explicitly is how a failure (a wrong share name,
        # bad credentials) actually surfaces instead of showing an empty
        # folder. Genuine noauto entries are still left alone.
        if "noauto" in options and "x-systemd.automount" not in options:
            continue
        if not entry.mountpoint.startswith("/"):
            continue
        if os.path.ismount(entry.mountpoint):
            continue
        pending.append(entry)
    return pending


def current_root_identity() -> Dict[str, Optional[str]]:
    """Identify whatever block device is currently mounted at /, resolved
    through bind mounts and subvolumes (e.g. a btrfs @ subvolume) down to
    the underlying device -- lsblk's own mountpoint field can't do this,
    since it only sees the device's default/first mount, not every
    subvolume mounted from it. Returns {"uuid": ..., "device": ...}, with
    values None if they couldn't be determined."""
    try:
        result = subprocess.run(
            ["findmnt", "-no", "UUID,SOURCE", "/"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"uuid": None, "device": None}

    parts = result.stdout.strip().split(None, 1)
    uuid = parts[0] if parts and parts[0] else None
    device = parts[1].split("[")[0] if len(parts) > 1 else None
    return {"uuid": uuid, "device": device or None}


def get_mountpoint(node: Dict) -> Optional[str]:
    mp = node.get("mountpoint")
    if mp:
        return mp
    for mp in node.get("mountpoints") or []:
        if mp:
            return mp
    return None


def identifier_for(node: Dict, prefer: str = "UUID") -> Optional[str]:
    """Build a UUID=/LABEL= fstab source string for a device, falling back sensibly."""
    uuid = node.get("uuid")
    label = node.get("label")
    name = node.get("name")

    if prefer == "LABEL" and label:
        return f"LABEL={label}"
    if uuid:
        return f"UUID={uuid}"
    if label:
        return f"LABEL={label}"
    return f"/dev/{name}" if name else None


# Filesystems with no native Linux permission model -- the kernel driver
# maps everything to a single uid/gid/mode given at mount time instead.
FOREIGN_FSTYPES = {"ntfs", "ntfs3", "exfat", "vfat", "fat", "fat32", "msdos"}


def _invoking_user_ids() -> "tuple[str, str]":
    """The desktop user's (uid, gid), even when this process was started via
    sudo/pkexec as root -- otherwise a Windows drive added while root'd
    would default to being owned by root, right back to needing a password
    to use it."""
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        return uid, gid
    return str(os.getuid()), str(os.getgid())


def _sanitize_path_segment(text: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in text.strip())
    return cleaned.strip("_") or "drive"


def suggest_mount_settings(node: Dict) -> Dict:
    """Best-practice fstab defaults for a freshly picked device, based on its
    filesystem type -- a starting point, not a hard rule; the user can
    still edit every field afterwards.

    NTFS/exFAT/FAT have no Linux permission model of their own, so without
    uid=/gid=/umask= they usually end up readable only by root -- exactly
    the "type your password every time" experience this is meant to avoid.
    """
    fstype = (node.get("fstype") or "").lower()

    if fstype == "swap":
        return {"mountpoint": "none", "fstype": fstype, "options": "sw", "dump": 0, "passno": 0}

    if fstype in FOREIGN_FSTYPES:
        uid, gid = _invoking_user_ids()
        options = f"defaults,uid={uid},gid={gid},umask=022,nofail"
        passno = 0  # no fsck.<fstype> helper for these on Linux
    else:
        options = "defaults,nofail"
        passno = 2

    # A device's *current* mountpoint is often a session-scoped udisks/gvfs
    # automount (/run/media/$USER/Label, or /media/... on some distros) --
    # not a real fstab candidate. /run is tmpfs and rebuilt empty every
    # boot, and that path only exists once a desktop session for that
    # specific user has started, which is later than fstab mounts happen.
    # Only reuse the live mountpoint if it looks like a real, persistent
    # location an admin set up on purpose.
    live_mountpoint = get_mountpoint(node) or ""
    is_session_mount = live_mountpoint.startswith(("/run/", "/media/"))
    mountpoint = live_mountpoint if (live_mountpoint and not is_session_mount) else ""
    if not mountpoint:
        segment = _sanitize_path_segment(node.get("label") or node.get("name") or "drive")
        mountpoint = f"/mnt/{segment}"

    return {
        "mountpoint": mountpoint,
        "fstype": fstype or (node.get("fstype") or "auto"),
        "options": options,
        "dump": 0,
        "passno": passno,
    }
