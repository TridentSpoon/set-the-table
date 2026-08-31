"""Timestamped backups of the fstab file before it gets overwritten."""

import datetime
import difflib
import os
import shutil
from typing import List, NamedTuple, Optional


def backup_fstab(path: str) -> Optional[str]:
    """Copy the existing file to a timestamped sibling. Returns the backup path, or
    None if there was nothing to back up (e.g. path doesn't exist yet)."""
    if not os.path.exists(path):
        return None

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.bak.{timestamp}"
    shutil.copy2(path, backup_path)
    return backup_path


class BackupInfo(NamedTuple):
    path: str
    when: datetime.datetime
    entry_count: int
    is_current: bool          # identical to the file as it stands now


def _count_entries(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and len(stripped.split()) >= 4:
            count += 1
    return count


def list_backups(path: str) -> List[BackupInfo]:
    """Timestamped backups of `path`, newest first.

    Backups are named `<path>.bak.YYYYmmdd-HHMMSS` by backup_fstab(), so
    the timestamp is read back off the name rather than from mtime, which
    a copy or restore would disturb.
    """
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".bak."

    try:
        current = open(path).read()
    except OSError:
        current = None

    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found

    for name in names:
        if not name.startswith(prefix):
            continue
        stamp = name[len(prefix):]
        try:
            when = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        full = os.path.join(directory, name)
        try:
            content = open(full).read()
        except OSError:
            continue
        found.append(BackupInfo(full, when, _count_entries(content), content == current))

    return sorted(found, key=lambda b: b.when, reverse=True)


def diff_against(backup_path: str, current_path: str) -> str:
    """A readable diff showing what restoring `backup_path` would change."""
    try:
        backup_lines = open(backup_path).read().splitlines()
    except OSError as e:
        return f"Couldn't read the backup: {e}"
    try:
        current_lines = open(current_path).read().splitlines()
    except OSError:
        current_lines = []

    diff = list(difflib.unified_diff(
        current_lines, backup_lines,
        fromfile="now", tofile="after restoring", lineterm="",
    ))
    if not diff:
        return "This backup is identical to the current file — restoring it would change nothing."
    return "\n".join(diff)
