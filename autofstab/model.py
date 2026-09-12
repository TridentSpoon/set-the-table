"""Parsing and rendering of /etc/fstab files.

The file is modeled as an ordered list of records so that comments and
blank lines round-trip unchanged. Only whitespace-separated lines with at
least 4 fields (device, mountpoint, fstype, options[, dump[, pass]]) are
treated as real entries; everything else is preserved verbatim.
"""

import os
from dataclasses import dataclass
from typing import List, Union


@dataclass
class RawLine:
    """A comment, blank, or unparseable line, kept verbatim."""

    text: str


@dataclass
class Entry:
    device: str
    mountpoint: str
    fstype: str
    options: str
    dump: int = 0
    passno: int = 0
    existing: bool = False  # True if loaded from the on-disk fstab, not added this session


Record = Union[RawLine, Entry]


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def parse_fstab(path: str) -> List[Record]:
    records: List[Record] = []
    with open(path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                records.append(RawLine(line))
                continue

            fields = stripped.split()
            if len(fields) < 4:
                records.append(RawLine(line))
                continue

            device, mountpoint, fstype, options = fields[0], fields[1], fields[2], fields[3]
            dump = _safe_int(fields[4]) if len(fields) > 4 else 0
            passno = _safe_int(fields[5]) if len(fields) > 5 else 0
            records.append(Entry(device, mountpoint, fstype, options, dump, passno, existing=True))
    return records


def render_fstab(records: List[Record]) -> str:
    entries = [r for r in records if isinstance(r, Entry)]
    widths = {
        "device": max([len(e.device) for e in entries] + [0]),
        "mountpoint": max([len(e.mountpoint) for e in entries] + [0]),
        "fstype": max([len(e.fstype) for e in entries] + [0]),
        "options": max([len(e.options) for e in entries] + [0]),
    }

    lines = []
    for r in records:
        if isinstance(r, RawLine):
            lines.append(r.text)
        else:
            lines.append(
                f"{r.device:<{widths['device']}}  "
                f"{r.mountpoint:<{widths['mountpoint']}}  "
                f"{r.fstype:<{widths['fstype']}}  "
                f"{r.options:<{widths['options']}}  "
                f"{r.dump}  {r.passno}"
            )

    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def format_entry_line(entry: Entry) -> str:
    return f"{entry.device}  {entry.mountpoint}  {entry.fstype}  {entry.options}  {entry.dump}  {entry.passno}"


def mountpoints_to_create(entries: List[Entry]) -> List[str]:
    """Mount points in `entries` that don't exist on disk yet.

    Worth creating at save time rather than leaving to the mount step: a
    systemd automount unit won't start without its directory, and `noauto`
    entries (every network share) are skipped by Refresh, so nothing else
    would ever create them.
    """
    wanted: List[str] = []
    for entry in entries:
        mountpoint = entry.mountpoint
        if not mountpoint.startswith("/") or mountpoint in ("none", "swap"):
            continue
        if (entry.fstype or "").lower() == "swap":
            continue
        if not os.path.isdir(mountpoint) and mountpoint not in wanted:
            wanted.append(mountpoint)
    return wanted


def automount_mountpoints(entries: List[Entry]) -> List[str]:
    """Mount points whose entry uses x-systemd.automount.

    These need their unit started before the folder is actually watched;
    generating the unit isn't enough on its own.
    """
    return [
        e.mountpoint for e in entries
        if e.mountpoint.startswith("/")
        and any(o.strip() == "x-systemd.automount" for o in (e.options or "").split(","))
    ]


def read_records(path: str):
    """(records, error) for `path`, where error is None on success.

    A file that exists but can't be read must never come back as an empty
    list. Saving would then replace contents nobody was ever allowed to
    see -- and since both front ends can escalate to root, "couldn't read
    it" is no protection at all. Absence is the genuinely different case:
    an fstab that isn't there yet really is empty.
    """
    try:
        return parse_fstab(path), None
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeDecodeError) as exc:
        return None, str(exc)
