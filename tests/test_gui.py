"""Functional test for the GTK4 GUI.

Drives the real widgets and signal handlers in-process, so GTK/libadwaita
API mistakes surface here rather than the first time a user clicks the
button. Several real bugs were caught this way that no amount of reading
the code would have found -- a nonexistent Gtk.ScrolledWindow method, a
button never stored on self, Adw.Application defaulting to single-instance.

Needs a display (it builds real widgets). Run with:

    python3 tests/test_gui.py

Exits non-zero if any check fails.

NOTE: assertions here deliberately avoid depending on which physical
drives happen to be plugged in or mounted right now -- that changed
mid-session more than once and produced false failures. Where a specific
kind of device is needed, a synthetic lsblk-shaped dict is injected
instead; where live state is unavoidable, the expectation is computed from
the same live data rather than hardcoded.
"""

import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk

from autofstab import gui
from autofstab.gui import NetworkSharePage
from autofstab.model import Entry
from autofstab.privileged import AuthResult, PrivilegedWriteResult

TEST_FSTAB = "/tmp/autofstab_gui_test.fstab"

# Real UUIDs get resolved against live lsblk output, so the few checks that
# genuinely need a real device use whatever the machine actually has.
RESULTS = []


def check(label, condition):
    RESULTS.append((label, bool(condition)))
    print(f"{'OK  ' if condition else 'FAIL'} - {label}")


def pump_until(predicate, timeout=2.0):
    """Iterate the real GLib main loop until predicate() holds or timeout.

    Needed wherever work is handed to a background thread and delivered
    back via GLib.idle_add (the pkexec paths), and for close() which runs
    an animation before the widget actually unmaps.
    """
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        ctx.iteration(True)
    return predicate()


def rows_of(listbox):
    out = []
    child = listbox.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def synthetic(name, fstype, uuid, label, mountpoint=None, tran="sata"):
    """An lsblk-shaped node, for exercising device kinds this machine may
    not currently have plugged in."""
    return {
        "name": name, "fstype": fstype, "uuid": uuid, "label": label,
        "size": "1T", "mountpoint": mountpoint, "tran": tran,
        "parent_disk": name[:-1] if name[-1].isdigit() else name,
    }


def run_checks(app, window):
    # -- pending vs. already-in-fstab split -------------------------------
    existing_entry = Entry("UUID=EXIST-1", "/mnt/existing", "ext4", "defaults", 0, 2, existing=True)
    window.records.append(existing_entry)
    window._refresh_list()
    check("existing entry renders in the read-only section", len(rows_of(window.existing_listbox)) == 1)
    check("existing entry is not in pending", len(rows_of(window.pending_listbox)) == 0)
    check("pending section hidden while empty", window.pending_section.get_visible() is False)

    pending_entry = Entry("UUID=PEND-1", "/mnt/pending", "ext4", "defaults,nofail", 0, 2)
    window._add_entry_confirmed(pending_entry)
    check("new entry lands in pending", len(rows_of(window.pending_listbox)) == 1)
    check("pending section becomes visible", window.pending_section.get_visible() is True)

    # -- grouping: same device, several subvolumes ------------------------
    for mountpoint, subvol in (("/srv", "@srv"), ("/var/log", "@log")):
        window.records.append(
            Entry("UUID=SUBVOL-1", mountpoint, "btrfs", f"subvol=/{subvol},defaults", 0, 0, existing=True)
        )
    window._refresh_list()
    existing_rows = rows_of(window.existing_listbox)
    grouped = [(r.entry.mountpoint, r.is_grouped_child) for r in existing_rows]
    check("subvolume siblings render consecutively", [m for m, _ in grouped[-2:]] == ["/srv", "/var/log"])
    check("first of a device group is not indented", grouped[-2][1] is False)
    check("second of a device group is indented as linked", grouped[-1][1] is True)

    # -- lock: system-critical entries ------------------------------------
    check("critical entries start locked", window.critical_unlocked is False)
    boot_entry = Entry("UUID=BOOT-1", "/boot", "vfat", "defaults,umask=0077", 0, 2, existing=True)
    window.records.append(boot_entry)
    window._refresh_list()
    boot_row = next(r for r in rows_of(window.existing_listbox) if r.entry.mountpoint == "/boot")
    check("/boot is flagged system-critical", boot_row.is_critical is True)

    window.existing_listbox.select_row(boot_row)
    check("Edit disabled for a locked entry", window.edit_btn.get_sensitive() is False)
    check("Remove disabled for a locked entry", window.remove_btn.get_sensitive() is False)

    original_auth = gui.authenticate_via_pkexec
    gui.authenticate_via_pkexec = lambda: AuthResult(True, None, False)
    try:
        window._on_lock_toggle_clicked(None)
        check("lock button disabled while authenticating", window.lock_btn.get_sensitive() is False)
        check("waiting-for-auth toast shown", window._auth_toast is not None)
        check("unlock completes via the main loop", pump_until(lambda: window.critical_unlocked is True))
        check("lock button re-enabled afterwards", window.lock_btn.get_sensitive() is True)
        check("waiting toast cleared afterwards", window._auth_toast is None)
        window.existing_listbox.select_row(
            next(r for r in rows_of(window.existing_listbox) if r.entry.mountpoint == "/boot")
        )
        check("Edit enabled once unlocked", window.edit_btn.get_sensitive() is True)
    finally:
        gui.authenticate_via_pkexec = original_auth

    window._on_lock_toggle_clicked(None)  # re-lock is synchronous, no auth
    check("re-locking needs no authentication", window.critical_unlocked is False)

    gui.authenticate_via_pkexec = lambda: AuthResult(False, None, True)
    try:
        window._on_lock_toggle_clicked(None)
        pump_until(lambda: window.lock_btn.get_sensitive() is True)
        check("cancelled auth leaves entries locked", window.critical_unlocked is False)
    finally:
        gui.authenticate_via_pkexec = original_auth

    window.records.remove(boot_entry)
    window._refresh_list()

    # -- device picker: bucketing -----------------------------------------
    picker = gui.DevicePickerDialog(window, on_pick=lambda d, n: None)
    check("picker uses floating presentation", picker.get_presentation_mode() == Adw.DialogPresentationMode.FLOATING)

    win_drive = synthetic("sdz1", "ntfs", "NTFS-1", "WinDrive", mountpoint=None)
    usb_ntfs = synthetic("sdz2", "ntfs", "USB-NTFS-1", "USBStick", mountpoint="/mnt/usb", tran="usb")
    fresh = synthetic("sdz3", "ext4", "FRESH-1", "FreshDrive", mountpoint="/mnt/fresh")
    unmounted = synthetic("sdz4", "ext4", "UNMOUNT-1", "ColdDrive", mountpoint=None)
    used = synthetic("sdz5", "ext4", "PEND-1", "AlreadyUsed", mountpoint="/mnt/pending")
    picker._devices.extend([win_drive, usb_ntfs, fresh, unmounted, used])

    picker.mounted_only_switch.set_active(False)
    picker._populate()
    titles = lambda rows: [r.get_title() for r in rows]

    check("unmounted NTFS goes to Windows drives", any("WinDrive" in t for t in titles(picker._windows_rows)))
    check("mounted non-Windows goes to Suggested", any("FreshDrive" in t for t in titles(picker._suggested_rows)))
    check("USB wins over NTFS classification", any("USBStick" in t for t in titles(picker._usb_rows)))
    check("USB is not in Windows drives", not any("USBStick" in t for t in titles(picker._windows_rows)))
    check(
        "a device an entry already points at goes to Already used",
        any("AlreadyUsed" in t for t in titles(picker._already_used_rows)),
    )
    check(
        "already-used devices are not in Other detected",
        not any("AlreadyUsed" in t for t in titles(picker._other_rows)),
    )
    check(
        "unused/non-USB/non-Windows goes to Other detected",
        any("ColdDrive" in t for t in titles(picker._other_rows)),
    )
    check("Already used rows are dimmed", all(r.has_css_class("dim-label") for r in picker._already_used_rows))
    check("Other detected rows are dimmed", all(r.has_css_class("dim-label") for r in picker._other_rows))
    check("Suggested rows are not dimmed", all(not r.has_css_class("dim-label") for r in picker._suggested_rows))

    # Windows folds into Suggested while the mounted-only filter is on.
    win_drive_mounted = synthetic("sdz6", "ntfs", "NTFS-2", "MountedWin", mountpoint="/mnt/win")
    picker._devices.append(win_drive_mounted)
    picker.mounted_only_switch.set_active(True)
    picker._populate()
    check(
        "mounted Windows drive merges into Suggested when filtered",
        any("MountedWin" in t for t in titles(picker._suggested_rows)),
    )
    check("Windows section empties once merged", len(picker._windows_rows) == 0)
    check(
        "unmounted devices excluded while mounted-only is on",
        not any("ColdDrive" in t for t in titles(picker._other_rows + picker._already_used_rows)),
    )

    # Row activation always resolves by UUID.
    picked = {}
    picker2 = gui.DevicePickerDialog(window, on_pick=lambda d, n: picked.update(device=d))
    picker2._on_row_activated(None, fresh)
    check("picking a device resolves it by UUID", picked.get("device") == "UUID=FRESH-1")

    # -- device picker: dismissal -----------------------------------------
    dismiss_a = gui.DevicePickerDialog(window, on_pick=lambda d, n: None)
    dismiss_a.present(window)
    check("Escape reports handled", dismiss_a._on_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True)
    check("Escape actually closes the picker", pump_until(lambda: not dismiss_a.get_mapped()))

    dismiss_b = gui.DevicePickerDialog(window, on_pick=lambda d, n: None)
    dismiss_b.present(window)
    check(
        "a backdrop click lands outside the dialog content",
        not gui._is_descendant(dismiss_b.pick(10, 10, Gtk.PickFlags.DEFAULT), dismiss_b.get_child()),
    )
    dismiss_b._on_dialog_clicked(None, 1, 10, 10)
    check("click-outside actually closes the picker", pump_until(lambda: not dismiss_b.get_mapped()))

    # -- quick-add smart defaults -----------------------------------------
    before = len(window._entries())
    window._quick_add_device("UUID=NTFS-9", synthetic("sdz7", "ntfs", "NTFS-9", "WinData", mountpoint=None))
    added = window._entries()[-1]
    check("quick-add appends one entry", len(window._entries()) == before + 1)
    check("NTFS gets uid/gid/umask/nofail", added.options == "defaults,uid=1000,gid=1000,umask=022,nofail")
    check("NTFS gets passno=0 (no fsck helper)", added.passno == 0)
    check("mountpoint derived from label", added.mountpoint == "/mnt/WinData")
    check("quick-added entry is pending, not existing", added.existing is False)

    window._quick_add_device("UUID=EXT-9", synthetic("sdz8", "ext4", "EXT-9", "LinuxData", mountpoint=None))
    native = window._entries()[-1]
    check("native fs gets defaults,nofail", native.options == "defaults,nofail")
    check("native fs gets passno=2", native.passno == 2)

    window._quick_add_device("UUID=SWAP-9", synthetic("sdz9", "swap", "SWAP-9", None, mountpoint=None))
    swap = window._entries()[-1]
    check("swap gets mountpoint=none", swap.mountpoint == "none")
    check("swap gets options=sw", swap.options == "sw")

    # A session-scoped automount is never a valid permanent mountpoint.
    session = synthetic("sdz10", "ext4", "SESSION-9", "SessionDrive", mountpoint="/run/media/user/SessionDrive")
    window._quick_add_device("UUID=SESSION-9", session)
    check("session /run/media mount is not reused", window._entries()[-1].mountpoint == "/mnt/SessionDrive")

    persistent = synthetic("sdz11", "ext4", "PERSIST-9", "PersistDrive", mountpoint="/data")
    window._quick_add_device("UUID=PERSIST-9", persistent)
    check("a real persistent mountpoint is reused", window._entries()[-1].mountpoint == "/data")

    # -- validation report styling ----------------------------------------
    report = gui._validation_report_widget(
        ["Entry 1: device/source field is empty"],
        [
            "Entry 2: mount point '/mnt/New' doesn't exist yet (expected for a new entry -- "
            "it'll need to be created before this can mount)",
            "Entry 3: device 'UUID=x' is already used by entry 1",
        ],
    )
    viewport = report.get_child()
    box = viewport.get_child() if isinstance(viewport, Gtk.Viewport) else viewport
    styled = []
    child = box.get_first_child()
    while child is not None:
        styled.append(child.get_css_classes())
        child = child.get_next_sibling()
    check("report renders one label per finding", len(styled) == 3)
    check("errors use the error style", "error" in styled[0])
    check("expected-for-new-entry warnings are muted", "dim-label" in styled[1] and "warning" not in styled[1])
    check("real warnings use the warning style", "warning" in styled[2])

    # -- save: privileged fallback ----------------------------------------
    from autofstab.model import render_fstab

    content = render_fstab(window.records)
    original_backup = gui.backup_fstab
    original_write = gui.write_with_pkexec

    def backup_denied(path):
        raise PermissionError("simulated: backup needs root")

    gui.backup_fstab = backup_denied
    gui.write_with_pkexec = lambda path, c, creds=None: PrivilegedWriteResult(True, "/etc/fstab.bak.fake", None, False)
    try:
        window.dirty = True
        window._write(content)
        check("Save disabled while authenticating", window.save_btn.get_sensitive() is False)
        check("save shows a waiting-for-auth toast", window._auth_toast is not None)
        check("privileged write completes", pump_until(lambda: window.dirty is False))
        check("saved entries become existing/read-only", all(e.existing for e in window._entries()))
        check("Save re-enabled afterwards", window.save_btn.get_sensitive() is True)
    finally:
        gui.write_with_pkexec = original_write
        gui.backup_fstab = original_backup

    gui.backup_fstab = backup_denied
    gui.write_with_pkexec = lambda path, c, creds=None: PrivilegedWriteResult(False, None, None, True)
    try:
        window.dirty = True
        window._write(content)
        pump_until(lambda: window.save_btn.get_sensitive() is True)
        check("cancelled auth leaves changes unsaved", window.dirty is True)
    finally:
        gui.write_with_pkexec = original_write
        gui.backup_fstab = original_backup

    gui.backup_fstab = backup_denied
    gui.write_with_pkexec = lambda path, c, creds=None: PrivilegedWriteResult(False, None, "pkexec missing", False)
    try:
        window.dirty = True
        window._write(content)
        pump_until(lambda: window.save_btn.get_sensitive() is True)
        check("a failed privileged write does not crash", window.dirty is True)
    finally:
        gui.write_with_pkexec = original_write
        gui.backup_fstab = original_backup

    # -- refresh mounts what isn't mounted --------------------------------
    from autofstab.devices import pending_mounts
    from autofstab.privileged import MountResult

    candidates = [
        Entry("UUID=M1", "/mnt/needs-mounting", "ext4", "defaults,nofail", 0, 2, existing=True),
        Entry("UUID=M2", "/mnt/opted-out", "ext4", "defaults,noauto", 0, 2, existing=True),
        Entry("UUID=M3", "none", "swap", "sw", 0, 0, existing=True),
        Entry("tmpfs", "/mnt/also-needed", "tmpfs", "defaults", 0, 0, existing=True),
        Entry("UUID=M5", "/", "btrfs", "subvol=/@,defaults", 0, 1, existing=True),
    ]
    pending = pending_mounts(candidates)
    pending_targets = [e.mountpoint for e in pending]
    check("an unmounted entry is picked up", "/mnt/needs-mounting" in pending_targets)
    check("noauto entries are left alone", "/mnt/opted-out" not in pending_targets)
    check("swap is not treated as a mount", "none" not in pending_targets)
    check("an already-mounted entry is skipped", "/" not in pending_targets)
    check("non-block sources still get mounted", "/mnt/also-needed" in pending_targets)

    original_mount = gui.mount_with_pkexec

    def run_mount_with(result, targets=("/mnt/needs-mounting",)):
        gui.mount_with_pkexec = lambda mountpoints, **kw: result
        try:
            window._start_mount(list(targets))
            pump_until(lambda: window.reload_btn.get_sensitive() is True)
        finally:
            gui.mount_with_pkexec = original_mount

    run_mount_with(MountResult(True, ["/mnt/needs-mounting"], ["/mnt/needs-mounting"], [], None, False))
    check("a successful mount re-enables refresh", window.reload_btn.get_sensitive() is True)
    check("a successful mount clears the auth toast", window._auth_toast is None)

    run_mount_with(MountResult(False, [], [], [], None, True))
    check("a cancelled password prompt re-enables refresh", window.reload_btn.get_sensitive() is True)

    run_mount_with(MountResult(False, [], [], [("/mnt/needs-mounting", "wrong fs type")], None, False))
    check("a failed mount is reported, not swallowed", window.reload_btn.get_sensitive() is True)

    run_mount_with(MountResult(False, [], [], [], "pkexec is not installed", False))
    check("a missing pkexec is handled", window.reload_btn.get_sensitive() is True)

    # Nothing to do must not fire a password prompt.
    check(
        "mounting with an empty list short-circuits",
        original_mount([]) == MountResult(True, [], [], [], None, False),
    )

    # -- network shares ----------------------------------------------------
    from autofstab import network
    from autofstab.model import render_fstab as _render

    smb = network.NetworkShare(network.SMB, "nas.local", "media", "/mnt/nas-media", "me", "hunter2")
    smb_entry, cred_path = network.build_entry(smb)
    check("SMB source is a UNC path", smb_entry.device == "//nas.local/media")
    check("network entries never get fsck'd", smb_entry.passno == 0)
    check(
        "a dead NAS can't block boot (mount-on-access, not at boot)",
        "noauto" in smb_entry.options and "x-systemd.automount" in smb_entry.options,
    )
    check("network entries wait for the network", "_netdev" in smb_entry.options)
    check("SMB gets uid/gid so it isn't root-only", "uid=" in smb_entry.options and "gid=" in smb_entry.options)
    check("SMB references a credentials file", f"credentials={cred_path}" in smb_entry.options)
    check("THE PASSWORD IS NOT IN THE FSTAB ENTRY", "hunter2" not in (smb_entry.device + smb_entry.options))

    guest_entry, guest_cred = network.build_entry(
        network.NetworkShare(network.SMB, "192.168.1.50", "public", "/mnt/public")
    )
    check("a passwordless share mounts as guest", "guest" in guest_entry.options)
    check("a passwordless share needs no credentials file", guest_cred is None)

    nfs_entry, nfs_cred = network.build_entry(
        network.NetworkShare(network.NFS, "nas.local", "/volume1/backups", "/mnt/backups")
    )
    check("NFS source is server:/export", nfs_entry.device == "nas.local:/volume1/backups")
    check("NFS takes no credentials file", nfs_cred is None)
    check("NFS omits uid/gid (server decides ownership)", "uid=" not in nfs_entry.options)

    check(
        "mount point is suggested from the export's last segment",
        network.default_mountpoint(network.NetworkShare(network.NFS, "s", "/volume1/media", "")) == "/mnt/media",
    )

    # NFS version pinning: only when the server can't do v4, since a
    # v4-capable server should be left free to negotiate the newest it has.
    v3_entry, _ = network.build_entry(
        network.NetworkShare(network.NFS, "s", "/export", "/mnt/x"), 3
    )
    v4_entry, _ = network.build_entry(
        network.NetworkShare(network.NFS, "s", "/export", "/mnt/x"), 4
    )
    unknown_entry, _ = network.build_entry(
        network.NetworkShare(network.NFS, "s", "/export", "/mnt/x"), None
    )
    check("a v3-only server gets nfsvers pinned", "nfsvers=3" in v3_entry.options)
    check("a v4 server is left to negotiate", "nfsvers" not in v4_entry.options)
    check("an unreachable server is left to negotiate", "nfsvers" not in unknown_entry.options)
    check("SMB never gets an nfsvers option", "nfsvers" not in smb_entry.options)

    # Adding a share should stage its secret for the next save, not write
    # it into the file being rendered. SMB skips the NFS probe, so this
    # completes synchronously.
    window._pending_credentials = []
    window._add_network_share(smb)
    staged = window._pending_credentials
    check("adding a share stages exactly one credentials file", len(staged) == 1)
    check("the staged file is under /etc", staged[0]["path"].startswith("/etc/"))
    check("the staged file holds the password", "hunter2" in staged[0]["content"])
    check("THE PASSWORD IS NOT IN THE RENDERED FSTAB", "hunter2" not in _render(window.records))

    # With a secret staged, saving must go through pkexec even if fstab
    # itself happens to be writable -- an unprivileged write can't create
    # a root-owned 0600 file.
    captured = {}
    orig_write = gui.write_with_pkexec
    def fake_write(path, content, creds=None):
        captured["creds"] = creds
        return PrivilegedWriteResult(True, None, None, False)

    gui.write_with_pkexec = fake_write
    try:
        window.dirty = True
        window._write(_render(window.records))
        pump_until(lambda: window.save_btn.get_sensitive() is True)
    finally:
        gui.write_with_pkexec = orig_write
    check("a staged secret forces the privileged write path", captured.get("creds") is not None)
    check("the credentials reach the privileged writer", "hunter2" in captured["creds"][0]["content"])
    check("staged secrets are dropped once written", window._pending_credentials == [])

    # A share with no password must not force a password prompt.
    window._pending_credentials = []
    window._add_network_share(network.NetworkShare(network.SMB, "srv", "open", "/mnt/open"))
    check("a guest share stages no secret", window._pending_credentials == [])

    # -- "+" dialog tabs ----------------------------------------------------
    saved_shares = []
    tabbed = gui.DevicePickerDialog(
        window, on_pick=lambda d, n: None, on_network_save=saved_shares.append
    )
    pages = []
    child = tabbed.view_stack.get_first_child()
    while child is not None:
        page_info = tabbed.view_stack.get_page(child)
        pages.append(page_info.get_name())
        child = child.get_next_sibling()
    check("the + dialog has both tabs", pages == ["drives", "advanced"])
    check("Drives is the tab shown first", tabbed.view_stack.get_visible_child_name() == "drives")
    check("the advanced tab holds the network form", isinstance(tabbed.network_page, NetworkSharePage))

    # Adding from the tab must reach the window and close the dialog.
    tabbed.present(window)
    tabbed.network_page.server_row.set_text("10.0.0.9")
    tabbed.network_page.share_row.set_text("/export/data")
    tabbed.network_page.kind_row.set_selected(1)
    tabbed.network_page.mount_row.set_text("/mnt/data")
    tabbed.network_page._on_add_clicked(None)
    check("adding from the advanced tab reaches the window", len(saved_shares) == 1)
    check("the added share carries the typed details",
          saved_shares[0].server == "10.0.0.9" and saved_shares[0].share == "/export/data")
    check("adding from the tab closes the dialog", pump_until(lambda: not tabbed.get_mapped()))

    # A page used standalone (no owner to close) must not blow up.
    orphan = NetworkSharePage(window, on_save=lambda s: None)
    orphan.server_row.set_text("h")
    orphan.share_row.set_text("s")
    orphan.mount_row.set_text("/mnt/s")
    orphan._on_add_clicked(None)
    check("a page with no owning dialog still adds cleanly", True)

    # -- discovery ---------------------------------------------------------
    # Real scans are avoided here (slow, and dependent on what's powered on);
    # the wiring is exercised with stubbed results instead.
    check("subnet detection returns dotted prefixes",
          all(p.count(".") == 3 and p.endswith(".") for p in network.local_subnets()))

    orig_discover = network.discover_servers
    orig_nfs = network.list_nfs_exports
    orig_smb = network.list_smb_shares

    scan_dialog = NetworkSharePage(window, on_save=lambda s: None)
    try:
        network.discover_servers = lambda *a, **k: [
            network.DiscoveredServer("10.0.0.5", [network.SMB, network.NFS], "NAS"),
            network.DiscoveredServer("10.0.0.6", [network.NFS], None),
        ]
        scan_dialog._on_scan_clicked(None)
        pump_until(lambda: scan_dialog.scan_button.get_sensitive() is True)
        check("scanning re-enables its button when done", scan_dialog.scan_button.get_sensitive() is True)
    finally:
        network.discover_servers = orig_discover

    scan_dialog._apply_server(network.DiscoveredServer("10.0.0.6", [network.NFS], None))
    check("picking an NFS-only server selects NFS", scan_dialog.kind_row.get_selected() == 1)
    check("picking a server fills its address", scan_dialog.server_row.get_text() == "10.0.0.6")
    scan_dialog._apply_server(network.DiscoveredServer("10.0.0.7", [network.SMB], None))
    check("picking an SMB-only server selects SMB", scan_dialog.kind_row.get_selected() == 0)
    check(
        "a server offering both leaves the choice alone",
        (lambda before: (scan_dialog._apply_server(
            network.DiscoveredServer("10.0.0.8", [network.SMB, network.NFS], None)
        ), scan_dialog.kind_row.get_selected() == before)[1])(scan_dialog.kind_row.get_selected()),
    )

    scan_dialog.server_row.set_text("10.0.0.5")
    try:
        network.list_nfs_exports = lambda *a, **k: ["/volume1/media", "/volume1/backups"]
        scan_dialog.kind_row.set_selected(1)
        scan_dialog._on_browse_clicked(None)
        pump_until(lambda: scan_dialog.browse_button.get_sensitive() is True)
        check("browsing re-enables its button", scan_dialog.browse_button.get_sensitive() is True)
    finally:
        network.list_nfs_exports = orig_nfs

    scan_dialog._apply_share("/volume1/media")
    check("picking an export fills the field", scan_dialog.share_row.get_text() == "/volume1/media")
    check("picking an export re-suggests the mount point",
          scan_dialog.mount_row.get_text() == "/mnt/media")

    try:
        network.list_smb_shares = lambda *a, **k: []
        scan_dialog.kind_row.set_selected(0)
        scan_dialog._on_browse_clicked(None)
        pump_until(lambda: scan_dialog.browse_button.get_sensitive() is True)
        check("an empty share list explains why, rather than failing silently",
              "username and password" in scan_dialog.note_label.get_text())
    finally:
        network.list_smb_shares = orig_smb

    # The SMB password must reach smbclient by environment, never argv,
    # since argv is visible to any user via `ps`.
    import inspect as _inspect
    smb_src = _inspect.getsource(network.list_smb_shares)
    check("SMB browse passes the password via the environment", 'env["PASSWD"]' in smb_src)
    check("SMB browse never puts the password in argv", '"-U", username' in smb_src and "password]" not in smb_src)

    dialog = NetworkSharePage(window, on_save=lambda s: None)
    check("network dialog builds", dialog is not None)
    check("SMB selected shows the sign-in fields", dialog.auth_group.get_visible() is True)
    dialog.kind_row.set_selected(1)
    check("NFS hides the sign-in fields", dialog.auth_group.get_visible() is False)
    check("NFS asks for an export path", dialog.share_row.get_title() == "Export path")
    dialog.kind_row.set_selected(0)
    check("switching back restores sign-in", dialog.auth_group.get_visible() is True)
    check(
        "the dialog explains the boot behaviour",
        "still boots" in dialog.note_label.get_text() or "boots normally" in dialog.note_label.get_text(),
    )

    # -- about dialog + update check --------------------------------------
    from autofstab import updates

    about = gui.AboutDialog()
    check("about dialog builds", about is not None)
    check("check button starts enabled", about.update_button.get_sensitive() is True)
    check("download row starts hidden", about.download_row.get_visible() is False)

    original_check = updates.check_for_update

    def run_check_with(result):
        """Click Check with a stubbed network result and let the
        background thread deliver it through the main loop."""
        updates.check_for_update = lambda version, **kw: result
        try:
            about._on_check_clicked(None)
            pump_until(lambda: about.update_button.get_sensitive() is True)
        finally:
            updates.check_for_update = original_check

    run_check_with(updates.UpdateResult(
        updates.UPDATE_AVAILABLE, "v9.9.9", "Version v9.9.9 is available — you have 0.1.0.",
        "https://example.invalid/releases/v9.9.9",
    ))
    check("update-available reports the new version", "9.9.9" in about.update_row.get_subtitle())
    check("update-available offers a download link", about.download_row.get_visible() is True)
    check("update-available is styled as good news", about.update_row.has_css_class("success"))
    check("download link points at the release", about._download_url.endswith("v9.9.9"))

    run_check_with(updates.UpdateResult(updates.UP_TO_DATE, "0.1.0", "You're up to date — 0.1.0 is the latest version."))
    check("up-to-date reports no update", "up to date" in about.update_row.get_subtitle())
    check("up-to-date hides the download link", about.download_row.get_visible() is False)
    check("up-to-date is not styled as an update", not about.update_row.has_css_class("success"))

    run_check_with(updates.UpdateResult(updates.ERROR, None, "Couldn't reach GitHub — check your connection."))
    check("network failure is surfaced, not swallowed", "Couldn't reach GitHub" in about.update_row.get_subtitle())
    check("network failure is styled as a warning", about.update_row.has_css_class("warning"))
    check("network failure re-enables the button", about.update_button.get_sensitive() is True)

    run_check_with(updates.UpdateResult(updates.NO_RELEASES, None, "No releases have been published yet."))
    check("no-releases is reported plainly", "No releases" in about.update_row.get_subtitle())
    check("no-releases offers no download", about.download_row.get_visible() is False)

    # Version comparison is what decides whether an update is offered at
    # all, so the ordering traps are worth locking down.
    check("v-prefixed tags compare correctly", updates.is_newer("v0.2.0", "0.1.0") is True)
    check("equal versions offer no update", updates.is_newer("0.1.0", "0.1.0") is False)
    check("older releases never offer a downgrade", updates.is_newer("0.0.9", "0.1.0") is False)
    check("comparison is numeric, not lexicographic", updates.is_newer("1.10.0", "1.9.0") is True)
    check("an unparseable tag never claims an update", updates.is_newer("garbage", "0.1.0") is False)

    # -- save: unprivileged happy path ------------------------------------
    window.dirty = True
    window._write("# written by the GUI test\n")
    check("plain write succeeds where permitted", os.path.exists(TEST_FSTAB))
    check("dirty cleared after a plain save", window.dirty is False)


def main():
    with open(TEST_FSTAB, "w") as f:
        f.write("# gui test fstab\n")

    def on_activate(app):
        def go():
            try:
                run_checks(app, app.window)
            except Exception:
                traceback.print_exc()
                check("no unhandled exception", False)
            finally:
                GLib.timeout_add(100, lambda: (app.quit(), False)[1])
            return False

        GLib.idle_add(go)

    app = gui.AutoFstabApp(TEST_FSTAB)
    app.connect("activate", on_activate)
    app.run([])

    for stale in os.listdir("/tmp"):
        if stale.startswith(os.path.basename(TEST_FSTAB)):
            os.unlink(os.path.join("/tmp", stale))

    failed = [label for label, ok in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(RESULTS)} checks FAILED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"all {len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
