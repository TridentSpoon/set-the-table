"""Timestamped backups of the fstab file before it gets overwritten."""

import datetime
import os
import shutil
from typing import Optional


def backup_fstab(path: str) -> Optional[str]:
    """Copy the existing file to a timestamped sibling. Returns the backup path, or
    None if there was nothing to back up (e.g. path doesn't exist yet)."""
    if not os.path.exists(path):
        return None

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.bak.{timestamp}"
    shutil.copy2(path, backup_path)
    return backup_path
