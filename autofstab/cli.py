"""Interactive, menu-driven fstab editor."""

import argparse
import getpass
import os
import shutil
import sys
from typing import List, Optional

from . import network
from .backup import backup_fstab
from .devices import get_mountpoint, identifier_for, list_block_devices
from .model import (
    Entry, RawLine, Record, automount_mountpoints, format_entry_line,
    mountpoints_to_create, parse_fstab, render_fstab,
)
from .privileged import write_with_sudo
from .validate import dry_run_verify, validate_entries

MENU = """
AutoFSTAB -- interactive fstab editor
  1) List entries
  2) Add entry
  3) Add network share (SMB/NFS)
  4) Edit entry
  5) Remove entry
  6) Validate current entries
  7) Save (backup + validate + dry-run + write)
  8) Reload from disk (discard changes)
  9) Quit
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


def add_entry(records: List[Record], pending_credentials: List[dict]) -> bool:
    print("\nAdd fstab entry")
    print("  1) Pick from detected block devices")
    print("  2) Enter manually (device path, tmpfs, swap, etc.)")
    print("  3) Network share (SMB/CIFS or NFS)")
    mode = prompt("Choice", default="1")

    if mode == "3":
        return add_network_share(records, pending_credentials)

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


def _pick_from_list(items, title, prompt_text):
    """Show a numbered list and return the chosen item, or None.

    Used for both discovered servers and browsed shares -- the CLI's
    equivalent of the GUI's pick-a-row dialogs.
    """
    if not items:
        return None
    print(f"\n{title}")
    for i, label in enumerate(items, start=1):
        print(f"  {i}) {label}")
    print("  0) Type it in myself")
    choice = prompt_int(prompt_text, default=0)
    if 1 <= choice <= len(items):
        return items[choice - 1]
    return None


def _discover_server() -> str:
    """Offer a scan of the local network, returning a chosen address or ""."""
    subnets = network.local_subnets()
    if not subnets:
        return ""
    if not confirm(f"Scan the local network ({', '.join(subnets)}) for servers?", default=False):
        return ""
    print("Scanning... (a few seconds)")
    found = network.discover_servers()
    if not found:
        print("No SMB or NFS servers answered. You can still type an address in.")
        return ""
    labels = [
        f"{srv.name or srv.host}  ({srv.host})  offers: {', '.join(srv.services)}"
        for srv in found
    ]
    picked = _pick_from_list(labels, "Servers found:", "Select server number")
    return found[labels.index(picked)].host if picked else ""


def add_network_share(records, pending_credentials) -> bool:
    """Add an SMB/CIFS or NFS share.

    Same core as the GUI's Advanced tab -- network.build_entry does the
    real work -- so both front ends produce byte-identical fstab lines and
    the same noauto/x-systemd.automount behaviour.
    """
    print("\nAdd a network share")
    print("  1) SMB / CIFS (Windows share, most NAS boxes)")
    print("  2) NFS (Unix/Linux export)")
    kind = network.NFS if prompt("Choice", default="1") == "2" else network.SMB

    if network.helper_missing(kind):
        print("\n" + network.install_hint(kind))
        print("The entry can still be added now -- it just won't mount until that's installed.")

    server = _discover_server() or prompt("Server (hostname or IP)")
    if not server:
        print("Cancelled.")
        return False

    username = password = ""
    if kind == network.SMB:
        username = prompt("Username (leave blank for a guest share)")
        if username:
            # getpass, so the password never appears on screen. It is kept
            # out of argv too -- network.list_smb_shares passes it through
            # the environment instead.
            password = getpass.getpass("Password (not shown, not written to fstab): ")

    share = ""
    if kind == network.NFS:
        exports = network.list_nfs_exports(server)
        if exports:
            share = _pick_from_list(exports, f"Exports on {server}:", "Select export number") or ""
        elif not shutil.which("showmount"):
            print("`showmount` isn't installed, so exports can't be listed. Type the path in.")
    else:
        shares = network.list_smb_shares(server, username, password)
        if shares:
            share = _pick_from_list(shares, f"Shares on {server}:", "Select share number") or ""
        elif not shutil.which("smbclient"):
            print("`smbclient` isn't installed, so shares can't be listed. Type the name in.")
        else:
            print("No shares could be listed (many servers refuse anonymous browsing).")

    if not share:
        share = prompt("Export path (e.g. /volume1/media)" if kind == network.NFS
                       else "Share name (e.g. media)")
    if not share:
        print("Cancelled.")
        return False

    # default_mountpoint only reads .kind/.server/.share; the mountpoint it
    # is being asked to suggest is naturally still blank at this point.
    draft = network.NetworkShare(kind=kind, server=server, share=share, mountpoint="")
    mountpoint = prompt("Mount point", default=network.default_mountpoint(draft))

    share_spec = network.NetworkShare(
        kind=kind, server=server, share=share, mountpoint=mountpoint,
        username=username or None, password=password or None,
    )

    nfs_version = network.probe_nfs_version(server) if kind == network.NFS else None
    entry, credentials_path = network.build_entry(share_spec, nfs_version)

    print("\nNew entry:")
    print(format_entry_line(entry))
    print("Mounts on first access rather than at boot, so a server that's off")
    print("or unreachable can never hold up your startup.")
    if not confirm("Add this entry?", default=True):
        print("Cancelled.")
        return False

    records.append(entry)
    if credentials_path:
        pending_credentials.append({
            "path": credentials_path,
            "content": network.credentials_content(username or "", password or ""),
        })
        print(f"The password will be written to {credentials_path} (root-only) when you save,")
        print("never into fstab itself.")
    print("Added (not yet saved).")
    return True


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


def _writable(path: str) -> bool:
    """Whether this process could write `path` without escalating."""
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    return os.access(directory, os.W_OK)


def _escalate_save(records: List[Record], path: str, content: str,
                   credentials: Optional[List[dict]] = None) -> bool:
    """Escalate just the write, never the whole session.

    Same bargain the GUI strikes: browse and edit unprivileged, and ask for
    a password only at the moment something has to touch the file. Whatever
    the user answers, their edits are still in `records` afterwards -- a
    refusal costs them the save, not the work.
    """
    entries = [r for r in records if isinstance(r, Entry)]
    result = write_with_sudo(
        path, content,
        credentials=credentials,
        ensure_dirs=mountpoints_to_create(entries),
        start_automounts=automount_mountpoints(entries),
    )
    if not result.ok:
        if result.error:
            print(result.error)
        print("Not saved. Your changes are still here -- pick 7 again when you're ready.")
        return False

    if result.backup_path:
        print(f"Backed up existing file to {result.backup_path}")
    print(f"Saved {path} (as root).")
    print("Run 'sudo mount -a' to apply new mounts, or reboot to test fully.")
    return True


def save(records: List[Record], path: str,
         credentials: Optional[List[dict]] = None) -> bool:
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

    # Credential files have to be root-owned and 0600, which an unprivileged
    # write can't produce -- so those go straight to the escalated path
    # rather than failing halfway through and leaving a half-written share.
    # Work out whether root is needed BEFORE backing anything up. Backing up
    # first would leave a stray .bak behind every time the user declines the
    # password prompt, and the escalated path makes its own backup as root,
    # so doing it here too would just produce two of them.
    if credentials:
        print(f"\nSaving needs root: {path}, plus {len(credentials)} credentials file(s) "
              "that must be root-owned and unreadable by anyone else.")
    elif not _writable(path):
        print(f"\nWriting {path} needs root.")

    if credentials or not _writable(path):
        if not confirm("Continue with sudo?", default=True):
            print("Not saved. Your changes are still here -- pick 7 again when you're ready.")
            return False
        return _escalate_save(records, path, content, credentials)

    if os.path.exists(path):
        backup_path = backup_fstab(path)
        if backup_path:
            print(f"Backed up existing file to {backup_path}")

    try:
        with open(path, "w") as f:
            f.write(content)
    except PermissionError:
        # Backstop: os.access can disagree with the kernel under ACLs or an
        # NFS root-squash, so the write itself still gets to have the say.
        print(f"\nWriting {path} needs root after all.")
        if not confirm("Retry with sudo?", default=True):
            print("Not saved. Your changes are still here -- pick 7 again when you're ready.")
            return False
        return _escalate_save(records, path, content, credentials)

    print(f"Saved {path}.")
    print("Run 'sudo mount -a' to apply new mounts, or reboot to test fully.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive fstab editor with backup, validation, and dry-run checks.")
    parser.add_argument("-f", "--file", default="/etc/fstab", help="Path to the fstab file to edit (default: /etc/fstab)")
    args = parser.parse_args()

    path = args.file
    # Distinguish "not there" from "there but unreadable". Treating the
    # second as an empty file would be silent data loss: the save step can
    # escalate, so it would cheerfully replace a file whose contents we were
    # never allowed to see, with whatever the empty session produced.
    try:
        records = parse_fstab(path)
    except FileNotFoundError:
        print(f"'{path}' does not exist yet -- starting with an empty fstab.")
        records = []
    except PermissionError:
        print(f"Can't read {path} -- permission denied.")
        print("Refusing to go further: saving later could replace a file whose")
        print("contents were never visible here. Re-run with sudo to edit it.")
        return 1

    if path == "/etc/fstab" and os.geteuid() != 0:
        print("Note: you are not root. Browse and edit freely -- only the save itself")
        print("needs a password, and it'll ask for one then. No need to restart with sudo.")

    dirty = False
    pending_credentials: List[dict] = []

    while True:
        print(MENU)
        choice = input("> ").strip().lower()

        if choice == "1":
            list_entries(records)
        elif choice == "2":
            dirty = add_entry(records, pending_credentials) or dirty
        elif choice == "3":
            dirty = add_network_share(records, pending_credentials) or dirty
        elif choice == "4":
            dirty = edit_entry(records) or dirty
        elif choice == "5":
            dirty = remove_entry(records) or dirty
        elif choice == "6":
            run_validation(records)
        elif choice == "7":
            if save(records, path, pending_credentials):
                dirty = False
                # Written now, so they mustn't be written again on a later save.
                pending_credentials.clear()
        elif choice == "8":
            if dirty and not confirm("Discard unsaved changes?", default=False):
                continue
            records = parse_fstab(path) if os.path.exists(path) else []
            dirty = False
            # Staged secrets belonged to the discarded edits.
            pending_credentials.clear()
            print("Reloaded from disk.")
        elif choice in ("9", "q", "quit", "exit"):
            if dirty and not confirm("You have unsaved changes. Quit anyway?", default=False):
                continue
            break
        else:
            print("Unrecognized option.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
