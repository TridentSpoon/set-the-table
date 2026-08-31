"""Static validation and a non-destructive dry-run check for candidate fstabs."""

import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from .model import Entry


def _has_subvol(options: str) -> bool:
    return any(opt.strip().startswith("subvol=") for opt in options.split(","))


def validate_entries(entries: List[Entry]) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors should block a save; warnings are informational."""
    errors: List[str] = []
    warnings: List[str] = []
    seen_mountpoints: dict = {}
    seen_devices: dict = {}  # device -> (first entry index, its options)

    for i, e in enumerate(entries, start=1):
        if not e.device.strip():
            errors.append(f"Entry {i}: device/source field is empty")
        if not e.mountpoint.strip():
            errors.append(f"Entry {i}: mount point is empty")
        if not e.fstype.strip():
            errors.append(f"Entry {i}: filesystem type is empty")

        if e.mountpoint not in ("none", "swap"):
            if e.mountpoint in seen_mountpoints:
                errors.append(
                    f"Entry {i}: duplicate mount point '{e.mountpoint}' "
                    f"(also entry {seen_mountpoints[e.mountpoint]})"
                )
            else:
                seen_mountpoints[e.mountpoint] = i

            if e.mountpoint and not e.mountpoint.startswith("/"):
                warnings.append(f"Entry {i}: mount point '{e.mountpoint}' is not an absolute path")
            elif e.mountpoint and not os.path.isdir(e.mountpoint):
                if e.existing:
                    # Already in the saved fstab, yet the directory is
                    # missing right now -- worth a more pointed nudge, e.g.
                    # a removable drive's old mount folder got deleted.
                    warnings.append(f"Entry {i}: mount point '{e.mountpoint}' does not exist on disk")
                else:
                    # Normal for a new, unsaved entry -- the folder just
                    # hasn't been created yet, not a sign anything is wrong.
                    warnings.append(
                        f"Entry {i}: mount point '{e.mountpoint}' doesn't exist yet "
                        "(expected for a new entry -- it'll need to be created before this can mount)"
                    )

        if e.device in seen_devices:
            first_index, first_options = seen_devices[e.device]
            # Not a real issue when either side is a btrfs subvolume mount
            # (subvol=...) -- one partition split into several subvolumes,
            # each with its own fstab entry, is the standard, safe way to
            # set that up, not an accidental duplicate.
            if not (_has_subvol(e.options) or _has_subvol(first_options)):
                warnings.append(
                    f"Entry {i}: device '{e.device}' is already used by entry {first_index}"
                )
        else:
            seen_devices[e.device] = (i, e.options)

        if e.device.startswith("/dev/sd") or e.device.startswith("/dev/hd") or e.device.startswith("/dev/vd"):
            warnings.append(
                f"Entry {i}: uses a raw device path ({e.device}); device letters can change "
                "across reboots — consider UUID= or LABEL= instead"
            )

    return errors, warnings


def dry_run_verify(candidate_text: str) -> Tuple[Optional[bool], str, bool]:
    """Verify a candidate fstab without touching the live system.

    Writes the candidate to a temp file and points findmnt's libmount at it via the
    LIBMOUNT_FSTAB environment variable, so nothing is actually mounted.

    Returns (ok, output, tool_available). ok is None when findmnt isn't installed.
    """
    if not shutil.which("findmnt"):
        return None, "findmnt not found — skipping automated dry-run verification.", False

    with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as tmp:
        tmp.write(candidate_text)
        tmp_path = tmp.name

    try:
        env = {**os.environ, "LIBMOUNT_FSTAB": tmp_path}
        result = subprocess.run(
            ["findmnt", "--verify", "--verbose"],
            capture_output=True,
            text=True,
            env=env,
        )
        ok = result.returncode == 0
        output = (result.stdout + result.stderr).strip()
        return ok, output, True
    finally:
        os.unlink(tmp_path)
