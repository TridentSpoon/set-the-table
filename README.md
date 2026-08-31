# Set the Table

*An Auto-Mount FSTAB Assistant.*

A small editor for `/etc/fstab` — a GTK4/libadwaita GUI plus a menu-driven
CLI, sharing the same safety rails: backups before every write, structural
validation, and a non-destructive dry-run check via `findmnt --verify`
before anything touches disk. (The Python package underneath is still
called `autofstab` — that's just the internal/technical name and doesn't
need to match the app's name.)

## Requirements

- Python 3.8+
- `util-linux` (provides `lsblk` for device picking and `findmnt` for
  dry-run verification). Usually already installed on any Linux system.
- For the GUI: GTK4 and libadwaita with their Python bindings (PyGObject).
  - Arch/CachyOS: `sudo pacman -S python-gobject gtk4 libadwaita`
  - Debian/Ubuntu: `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`
  - Fedora: `sudo dnf install python3-gobject gtk4 libadwaita`
- `pkexec` (part of polkit, installed by default on virtually every desktop
  distro) — lets the GUI ask for your password graphically only when you
  click Save, instead of needing to launch the whole app as root.

## Installing as a desktop app (double-click to launch)

```bash
./install.sh
```

Run that from your clone of this repo. It copies the app to
`~/.local/share/set-the-table`, adds a launcher to
`~/.local/bin/set-the-table`, and installs a `.desktop` entry so **Set the
Table** shows up in your application menu like any other installed app —
no terminal needed after this one-time step. It checks for the GTK4/
libadwaita dependencies above first and tells you exactly what to install
if they're missing. Re-running `./install.sh` safely reinstalls over a
previous copy, which is how you upgrade:

```bash
cd /path/to/set-the-table   # your clone, not ~/.local/share/set-the-table
git pull
./install.sh
```

The installed copy under `~/.local/share` is a plain file copy, not a git
checkout, so `git pull` won't work from there.

If it doesn't show up in your app menu right away, log out and back in,
or test it directly with `gtk-launch io.github.autofstab.SetTheTable`.

## Usage

### GUI (recommended)

Try it against a scratch file first — no root needed:

```bash
python3 autofstab_gui.py --file /tmp/test-fstab
```

Run it normally against the real file — no `sudo` needed to launch it:

```bash
python3 autofstab_gui.py
```

You can browse and edit freely; the only privileged step is the actual
write. If Save hits a permissions error, it automatically retries via
`pkexec`, which pops up your desktop's normal graphical password prompt —
no terminal required. That call runs on a background thread so the window
stays fully responsive while the prompt is up, however long you take at it
(a "Waiting for authentication…" toast shows in the meantime); the same
applies to unlocking system-critical entries, below.

The window has:

- **+** opens the **device picker** straight away and adds the drive you
  pick immediately, with best-practice settings already filled in (see
  below) — no form to fill out first. The new entry shows up in a "Pending
  changes" section where you can hit the pencil to adjust anything before
  saving. For non-block-device sources (network shares, tmpfs, etc.) use
  the menu's **Add entry manually…** instead, which opens the full form.
  The picker defaults to showing only drives that are *currently mounted*
  (e.g. one you just plugged in) — flip its switch off to see every
  detected drive, mounted or not. If there's nothing left to suggest under
  that filter (e.g. you've already added everything you had plugged in),
  it opens with the switch off automatically instead of showing an empty
  list. Devices are always identified by UUID
  (falling back to LABEL, then raw path, only if no UUID exists) — that's
  the right call in essentially every case, so it's not a choice you have
  to make. Drives that aren't already in your fstab list and aren't your
  system/swap drive split into two sections when browsing every detected
  drive (mounted-only switch off): **"Add your Windows drives"** for NTFS
  drives specifically (with a tooltip explaining that NTFS is Windows'
  default filesystem) — shown whether or not they're currently mounted,
  since an internal Windows drive often isn't auto-mounted the way
  removable media is — and **Suggested** for everything else that
  qualifies, which does need to be currently mounted (the "I already have
  this plugged in, make it permanent" case). While the mounted-only switch
  is *on*, everything shown is already mounted, so the split isn't doing
  much work — Windows drives fold into Suggested instead, one flat list of
  everything currently plugged in. USB drives get their own
  **"USB drives"** section too, but always last, with a tooltip explaining
  that removable USB media is normally handled automatically and doesn't
  need a permanent entry — still there if you want one (e.g. a USB dock),
  just clearly the least-recommended choice; a USB stick formatted as NTFS
  lands here, not in Windows drives. Anything left over — not already in
  your list, not USB, not Windows — shows under **Other detected devices**
  (dimmed), and anything a current entry already points at is collected
  under **Already used** (also dimmed), since there's nothing left to do
  with those. Full section order: Windows drives, Suggested, Other
  detected devices, Already used, USB drives.
- **pencil / trash** buttons edit or remove the selected entry.
- **Save** (outermost, the primary action) — validates, shows any
  warnings, runs the `findmnt --verify` dry-run, backs up the existing
  file, then writes. Each step can block or ask for confirmation if
  something looks wrong.
- **↻ (reload)** right next to Save — re-reads the file from disk, then
  offers to mount anything the file says should be mounted but isn't,
  creating any missing mount point folders first. That's the whole
  "did my save actually work?" loop in one button. It mounts by mount
  point, so the options come from the saved file — it can't mount
  anything the file doesn't already describe — and it asks before doing
  it, since mounting needs your password. Entries marked `noauto`, swap,
  and anything already mounted are left alone. No need
  to restart the app to check that a save actually took effect; click
  Save, then click reload, and you're looking at exactly what's on disk.
- **Validate** — runs the same structural checks Save does, without
  saving.
- The menu (☰) also has **Add entry manually…**, **Reload from disk**
  (same as the ↻ button), **Open other file…** (handy for pointing the
  app at a test file), and **About Set the Table**.

**Network drives (NAS, Windows shares):** the **+** dialog's
**Advanced** tab handles SMB/CIFS and NFS. Entries are written with
`noauto,x-systemd.automount,_netdev`, which matters more than it looks:
a plain network entry makes systemd wait on the mount at boot and then
fail `remote-fs.target`, which can drop the machine to an emergency
shell if the NAS happens to be switched off — and even `nofail` only
downgrades that to a startup stall. With these options the share is
mounted the first time you open it instead, so an unreachable server
costs nothing at boot.

SMB passwords are never written into fstab, which is mode 644 and
readable by every user on the machine. They go into a root-owned
`chmod 600` file under `/etc/samba/credentials/`, referenced from the
entry via `credentials=`. Leave the username and password blank for a
guest share. SMB entries also get `uid`/`gid` so the share belongs to
you rather than root; NFS doesn't, since the server decides ownership
there.

The dialog can find things for you rather than making you type them.
The **search** button next to Server scans your local subnet for hosts
answering on SMB (445) or NFS (2049) and lists them by NetBIOS name —
this is the only method that reliably finds a NAS, since mDNS only sees
servers that advertise, NetBIOS broadcast needs SMB1-era discovery that
modern NAS boxes disable, and the ARP table only knows hosts you've
already talked to. It's an explicit button press, never automatic. The
**folder** button next to Share asks the chosen server what it offers:
NFS exports need no credentials, while most SMB servers won't list
shares without a username and password, so fill those in first. NFS
entries also get `nfsvers=` pinned when the server can't do v4, which
saves a failed 4.2 → 4.1 → 4.0 negotiation on every mount.

Saving also creates any missing mount point folders and runs
`systemctl daemon-reload`. Both matter: systemd only reads fstab through
`systemd-fstab-generator`, which runs at boot and on daemon-reload, so
without it a new `x-systemd.automount` entry sits in the file doing
nothing until you reboot — and an automount unit won't start without its
directory anyway.

**Where the share shows up:** it's mounted at the path you chose (e.g.
`/mnt/Home_File_Backup`), so it appears there as an ordinary folder. It
will *not* show up on its own in Dolphin's Places, Devices, Removable
Devices, or Remote — Devices comes from Solid/udisks2, which lists block
devices, and Remote lists KIO bookmarks (`smb://`, `nfs://`), which are a
separate mechanism from fstab entirely. Navigate to the mount point once
and drag it into Places to pin it.

Mounting needs a helper package that isn't always installed —
`cifs-utils` for SMB, `nfs-utils` (`nfs-common` on Debian/Ubuntu) for
NFS. Without it, mount fails with a bare "unknown filesystem type", so
the dialog checks up front and names the package for your distro.

**Restoring a backup:** every save writes a timestamped backup beside the
file. **☰ → Restore from backup…** lists them newest first, with how many
entries each held and which one matches the file as it stands. Picking one
shows a diff of exactly what restoring would change before you commit to
it. Restoring goes through the same path as a normal save, so the current
file is backed up first — stepping back is itself undoable — and systemd
is reloaded afterwards.

**Checking for updates:** the About dialog has a **Check for Updates**
button, and the app also checks once a couple of seconds after launch. It
only speaks up when there's genuinely something newer: being up to date,
offline, or rate-limited are all silent, since a popup for "nothing to do"
is just noise. It never downloads or installs anything by itself — the
button opens the release page. Launch `--no-update-check` to stop it
contacting GitHub at startup.

**Developer options:** `--dev` adds *Open other file…* to the menu, which
retargets the whole app — Save included — at a different fstab. Handy for
testing against a scratch file, confusing enough in normal use that it's
hidden by default.

**Smart defaults on quick-add:** NTFS/exFAT/FAT drives get
`uid=<you>,gid=<you>,umask=022,nofail` (so they're usable without being root
or typing a password every boot — the default otherwise is root-only
access); native Linux filesystems get `defaults,nofail`; swap gets
`none`/`sw`. `uid`/`gid` resolve to the desktop user even when the app was
launched via `sudo`/`pkexec`. These are just a starting point — edit the
entry afterwards if you need something else.

Picking a device already used by another entry (e.g. one btrfs partition
split into several subvolumes) doesn't do anything special — it's a normal,
supported pattern — but the picker notes how many entries already use it.
Entries that share a physical device are also shown grouped together in
the list, with the 2nd+ one indented under a "↳ same drive" label instead
of repeating the drive name.

**System-critical entries are locked by default** (🔒 in the header): `/boot`,
`/boot/efi`/`/efi`, and anything else on the same physical disk as your
current root filesystem — covers the whole "system drive" as a unit, not
just the exact root partition, since breaking any of it can stop your
machine from booting. Locked rows show a small lock icon and can't be
edited or removed until you click the header's lock button, which requires
a real `pkexec` authentication (not just a click-through warning) — the
same graphical password prompt Save uses. Re-locking doesn't need
authentication. This is a UX safety net against accidental clicks, not a
security boundary — Save's own `pkexec` step is what actually gates writing
to disk.

### CLI

Same safety rails, text-menu driven:

```bash
python3 autofstab.py --file /tmp/test-fstab
# or, for the real file:
sudo python3 autofstab.py
```

Menu options: list, add, edit, remove, validate, save (backup + validate +
dry-run + write), reload from disk, quit.

### Shared behavior

All changes are held in memory until you explicitly save, and every save is
backed up first to `<path>.bak.<timestamp>`. Neither front end runs
`mount -a` for you — after saving, you apply new mounts yourself
(`sudo mount -a`) so that step stays a deliberate, separate action.

## Project layout

```
autofstab/
  model.py        parsing/rendering of fstab records (shared)
  devices.py      lsblk-based device discovery + smart mount defaults (shared)
  validate.py     structural checks + findmnt dry-run (shared)
  backup.py       timestamped backups (shared)
  privileged.py   pkexec-based write fallback for the GUI
  cli.py          text-menu front end
  gui.py          GTK4/libadwaita front end
autofstab.py       CLI entry point
autofstab_gui.py   GUI entry point
```

## Running tests

```bash
python3 -m unittest discover -s tests -p "test_fstab.py" -v
```

That covers parsing, rendering, and validation — pure logic, no display
needed. There's also a functional GUI test that builds the real widgets
and fires the real signal handlers:

```bash
python3 tests/test_gui.py
```

It needs a display, so it's kept out of the `unittest discover` run above.
Worth running after any GUI change: it has caught several bugs that
reading the code did not, including a nonexistent `Gtk.ScrolledWindow`
method, a button referenced via `self` but never stored there, and
`Adw.Application` silently defaulting to single-instance.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.
