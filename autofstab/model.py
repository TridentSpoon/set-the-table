"""Parsing and rendering of /etc/fstab files.

The file is modeled as an ordered list of records so that comments and
blank lines round-trip unchanged. Only whitespace-separated lines with at
least 4 fields (device, mountpoint, fstype, options[, dump[, pass]]) are
treated as real entries; everything else is preserved verbatim.
"""

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
