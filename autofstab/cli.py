"""Interactive, menu-driven fstab editor."""

import argparse
import os
from typing import List

from .backup import backup_fstab
from .devices import get_mountpoint, identifier_for, list_block_devices
from .model import Entry, RawLine, Record, format_entry_line, parse_fstab, render_fstab
from .validate import dry_run_verify, validate_entries

MENU = """
AutoFSTAB -- interactive fstab editor
  1) List entries
  2) Add entry
  3) Edit entry
  4) Remove entry
  5) Validate current entries
  6) Save (backup + validate + dry-run + write)
  7) Reload from disk (discard changes)
  8) Quit
"""


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value if value else default


def prompt_int(text: str, default: int) -> int:
    while True:
        raw = prompt(text, default=str(default))
        try:
            return int(raw)
        except ValueError:
            print("Please enter a whole number.")


def confirm(text: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{text} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _entry_positions(records: List[Record]) -> List[int]:
    return [i for i, r in enumerate(records) if isinstance(r, Entry)]


def list_entries(records: List[Record]) -> None:
    positions = _entry_positions(records)
    if not positions:
        print("No fstab entries yet.")
        return
    print(f"{'#':<3} {'Device':<38} {'Mount point':<16} {'Type':<8} {'Options':<22} {'Dump':<5} {'Pass'}")
    for display_idx, pos in enumerate(positions, start=1):
        e = records[pos]
        print(
            f"{display_idx:<3} {e.device:<38} {e.mountpoint:<16} {e.fstype:<8} "
            f"{e.options:<22} {e.dump:<5} {e.passno}"
        )


def add_entry(records: List[Record]) -> bool:
    print("\nAdd fstab entry")
    print("  1) Pick from detected block devices")
    print("  2) Enter manually (device path, network share, tmpfs, swap, etc.)")
    mode = prompt("Choice", default="1")

    device = None
    suggested_mount = ""
    suggested_fstype = ""

    if mode == "1":
        try:
            devices = list_block_devices()
        except RuntimeError as e:
            print(f"Could not list devices: {e}")
            return False
        if not devices:
            print("No devices with a filesystem were detected.")
            return False

        for i, d in enumerate(devices, start=1):
            mp = get_mountpoint(d) or "-"
            print(
                f"  {i}) /dev/{d.get('name')}  size={d.get('size')}  fstype={d.get('fstype')}  "
                f"uuid={d.get('uuid') or '-'}  label={d.get('label') or '-'}  mounted_at={mp}"
            )
        idx = prompt_int("Select device number", default=1)
        if not (1 <= idx <= len(devices)):
            print("Invalid selection, cancelled.")
            return False
        node = devices[idx - 1]

        print("  1) UUID (default, recommended)")
        print("  2) LABEL")
        print("  3) raw device path (/dev/...)")
        pref = prompt("Identify device by", default="1")
        if pref == "2":
            device = identifier_for(node, prefer="LABEL")
        elif pref == "3":
            device = f"/dev/{node.get('name')}"
        else:
            device = identifier_for(node, prefer="UUID")
        if device is None:
            device = f"/dev/{node.get('name')}"
            print(f"No UUID/LABEL available; falling back to {device}")

        suggested_fstype = node.get("fstype") or ""
        suggested_mount = get_mountpoint(node) or ""
    else:
        device = prompt("Device / source (e.g. UUID=..., /dev/sdb1, //server/share, tmpfs)")
        if not device:
            print("Cancelled.")
            return False

    mountpoint = prompt("Mount point", default=suggested_mount)
    fstype = prompt("Filesystem type", default=suggested_fstype or "auto")
    print("Tip: add 'nofail' for removable/external drives so a missing disk doesn't block boot.")
    options = prompt("Mount options", default="defaults")
    default_pass = 1 if mountpoint == "/" else (0 if mountpoint in ("none", "swap") else 2)
    dump = prompt_int("Dump (0 or 1)", default=0)
    passno = prompt_int("Pass (fsck order, 0/1/2)", default=default_pass)

    entry = Entry(device=device, mountpoint=mountpoint, fstype=fstype, options=options or "defaults",
                  dump=dump, passno=passno)
    print("\nNew entry:")
    print(format_entry_line(entry))
    if confirm("Add this entry?", default=True):
        records.append(entry)
        print("Added (not yet saved).")
        return True
    print("Cancelled.")
    return False


def edit_entry(records: List[Record]) -> bool:
    positions = _entry_positions(records)
    if not positions:
        print("No entries to edit.")
        return False
    list_entries(records)
    idx = prompt_int("Entry number to edit", default=1)
    if not (1 <= idx <= len(positions)):
        print("Invalid selection.")
        return False
    pos = positions[idx - 1]
    entry = records[pos]

    print("Press Enter to keep the current value.")
    device = prompt("Device / source", default=entry.device)
    mountpoint = prompt("Mount point", default=entry.mountpoint)
    fstype = prompt("Filesystem type", default=entry.fstype)
    options = prompt("Mount options", default=entry.options)
    dump = prompt_int("Dump", default=entry.dump)
    passno = prompt_int("Pass", default=entry.passno)

    updated = Entry(device, mountpoint, fstype, options, dump, passno)
    print("\nUpdated entry:")
    print(format_entry_line(updated))
    if confirm("Save this change?", default=True):
        records[pos] = updated
        return True
    print("Cancelled.")
    return False


def remove_entry(records: List[Record]) -> bool:
    positions = _entry_positions(records)
    if not positions:
        print("No entries to remove.")
        return False
    list_entries(records)
    idx = prompt_int("Entry number to remove", default=1)
    if not (1 <= idx <= len(positions)):
        print("Invalid selection.")
        return False
    pos = positions[idx - 1]
    print(format_entry_line(records[pos]))
    if confirm("Remove this entry?", default=False):
        del records[pos]
        print("Removed (not yet saved).")
        return True
    print("Cancelled.")
    return False


def run_validation(records: List[Record]) -> None:
    entries = [r for r in records if isinstance(r, Entry)]
    errors, warnings = validate_entries(entries)
    if not errors and not warnings:
        print("No issues found.")
        return
    for e in errors:
        print(f"ERROR: {e}")
    for w in warnings:
        print(f"WARNING: {w}")


def save(records: List[Record], path: str) -> bool:
    entries = [r for r in records if isinstance(r, Entry)]
    errors, warnings = validate_entries(entries)
    if errors:
        print("Cannot save -- validation errors found:")
        for e in errors:
            print(f"  ERROR: {e}")
        return False
    if warnings:
        print("Validation warnings:")
        for w in warnings:
            print(f"  WARNING: {w}")
        if not confirm("Continue despite warnings?", default=False):
            return False

    content = render_fstab(records)

    ok, output, tool_available = dry_run_verify(content)
    if tool_available:
        print("\nfindmnt --verify output:")
        print(output or "(no output -- looks clean)")
        if ok is False:
            if not confirm("Dry-run verification reported problems. Save anyway?", default=False):
                return False
    else:
        print(output)

    if os.path.exists(path):
        backup_path = backup_fstab(path)
        if backup_path:
            print(f"Backed up existing file to {backup_path}")

    try:
        with open(path, "w") as f:
            f.write(content)
    except PermissionError:
        print(f"Permission denied writing to {path}. Re-run with sudo.")
        return False

    print(f"Saved {path}.")
    print("Run 'sudo mount -a' to apply new mounts, or reboot to test fully.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive fstab editor with backup, validation, and dry-run checks.")
    parser.add_argument("-f", "--file", default="/etc/fstab", help="Path to the fstab file to edit (default: /etc/fstab)")
    args = parser.parse_args()

    path = args.file
    if os.path.exists(path):
        records = parse_fstab(path)
    else:
        print(f"'{path}' does not exist yet -- starting with an empty fstab.")
        records = []

    if path == "/etc/fstab" and os.geteuid() != 0:
        print("Note: you are not root. You can browse/edit freely, but saving to /etc/fstab will need sudo.")

    dirty = False

    while True:
        print(MENU)
        choice = input("> ").strip().lower()

        if choice == "1":
            list_entries(records)
        elif choice == "2":
            dirty = add_entry(records) or dirty
        elif choice == "3":
            dirty = edit_entry(records) or dirty
        elif choice == "4":
            dirty = remove_entry(records) or dirty
        elif choice == "5":
            run_validation(records)
        elif choice == "6":
            if save(records, path):
                dirty = False
        elif choice == "7":
            if dirty and not confirm("Discard unsaved changes?", default=False):
                continue
            records = parse_fstab(path) if os.path.exists(path) else []
            dirty = False
            print("Reloaded from disk.")
        elif choice in ("8", "q", "quit", "exit"):
            if dirty and not confirm("You have unsaved changes. Quit anyway?", default=False):
                continue
            break
        else:
            print("Unrecognized option.")


if __name__ == "__main__":
    main()
