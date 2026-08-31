"""GTK4/libadwaita GUI for editing /etc/fstab.

Reuses the same model/devices/validate/backup modules as the CLI, so both
front ends share identical parsing, validation, backup, and dry-run logic.
"""

import argparse
import os
import sys
import threading
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from .backup import backup_fstab
from .devices import (
    current_root_identity,
    describe_transport,
    display_name,
    get_mountpoint,
    identifier_for,
    list_block_devices,
    pending_mounts,
    resolve_device_index,
    suggest_mount_settings,
)
from .model import Entry, format_entry_line, parse_fstab, render_fstab
from .privileged import authenticate_via_pkexec, mount_with_pkexec, write_with_pkexec
from . import network, updates
from .validate import dry_run_verify, validate_entries


def _existing_use_count(node, entries) -> int:
    """How many current fstab entries already reference this device (by
    UUID, LABEL, or raw path). Not a problem by itself -- e.g. one btrfs
    partition commonly has a separate entry per subvolume -- just useful
    to know before adding another one."""
    candidates = set()
    if node.get("uuid"):
        candidates.add(f"UUID={node['uuid']}")
    if node.get("label"):
        candidates.add(f"LABEL={node['label']}")
    if node.get("name"):
        candidates.add(f"/dev/{node['name']}")
    return sum(1 for e in entries if e.device in candidates)


# Prefixes that name one specific block device, so two entries sharing the
# string really are the same drive. Sources like "tmpfs" or "none" are not
# in here: several tmpfs mounts are independent filesystems, not one device
# seen twice, so grouping them would be actively misleading.
DEVICE_IDENTIFIER_PREFIXES = ("UUID=", "PARTUUID=", "LABEL=", "PARTLABEL=", "/dev/")


def _device_group_key(node, device, position):
    """Group entries pointing at the same physical device (e.g. several
    btrfs subvolumes on one partition) so they can be shown linked.

    Prefers the resolved lsblk node, which catches the case where two
    entries name the same drive different ways (UUID= in one, LABEL= in
    the other). Falls back to the raw source string when the device isn't
    currently attached -- a disconnected drive's subvolumes should still
    read as one drive, not several unrelated entries. Anything that names
    no particular device gets a unique key so it never falsely groups."""
    if node:
        return ("dev", node.get("uuid") or node.get("name"))
    if device.startswith(DEVICE_IDENTIFIER_PREFIXES):
        return ("source", device)
    return ("solo", position)


def _resolve_root_node(index, root_info):
    """The lsblk node for whatever is currently mounted at /, so its
    parent_disk can be compared against other devices."""
    if root_info.get("uuid"):
        node = index.get(f"UUID={root_info['uuid']}")
        if node:
            return node
    if root_info.get("device"):
        return index.get(root_info["device"])
    return None


def _is_system_drive(node, root_node) -> bool:
    """True if `node` lives on the same physical disk as the currently
    mounted root filesystem -- not just the exact root partition, but also
    /boot, an EFI partition, or any other partition sharing that drive."""
    if not node or not root_node:
        return False
    return bool(node.get("parent_disk")) and node.get("parent_disk") == root_node.get("parent_disk")


CRITICAL_MOUNTPOINTS = {"/boot", "/boot/efi", "/efi"}

# NTFS is Windows' default filesystem -- exFAT/FAT are cross-platform and
# common on USB drives regardless of OS, so they don't get singled out the
# same way.
WINDOWS_FSTYPES = {"ntfs", "ntfs3"}


def _is_critical_entry(entry, node, root_node) -> bool:
    """Entries locked by default: anything on the physical system/boot disk,
    plus well-known boot mountpoints even if the device can't currently be
    resolved (e.g. temporarily disconnected)."""
    if entry.mountpoint in CRITICAL_MOUNTPOINTS:
        return True
    return _is_system_drive(node, root_node)


class _DeviceBuckets(NamedTuple):
    """How DevicePickerDialog._classify() sorts detected devices into the
    dialog's sections, plus the shared lookups its rows need."""

    windows: list
    suggested: list
    other: list
    already_used: list
    usb: list
    root_node: dict
    existing_entries: list


def _is_descendant(widget, ancestor) -> bool:
    """True if `widget` is `ancestor` or nested somewhere inside it."""
    node = widget
    while node is not None:
        if node == ancestor:
            return True
        node = node.get_parent()
    return False


def _run_in_thread(work_fn, callback):
    """Run `work_fn()` on a background thread and deliver its result to
    `callback(result)` on the GTK main thread via GLib.idle_add.

    Needed for anything that shells out to `pkexec`: it blocks until the
    user finishes (or cancels) the polkit password prompt, which can take
    as long as they like. Calling it directly from a signal handler would
    freeze the whole GTK main loop -- no redraws, no input handling, no
    resizing -- for that entire time, which is what made the window look
    like it was falling apart while waiting.
    """

    def worker():
        result = work_fn()
        GLib.idle_add(callback, result)

    threading.Thread(target=worker, daemon=True).start()


def _report_widget(text: str, max_height: int = 220) -> Gtk.Widget:
    label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
    label.add_css_class("monospace")
    label.add_css_class("caption")
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_child(label)
    scrolled.set_max_content_height(max_height)
    scrolled.set_propagate_natural_height(True)
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    return scrolled


def _validation_report_widget(errors, warnings, max_height: int = 220) -> Gtk.Widget:
    """Like _report_widget, but colors each line by severity instead of
    rendering one flat block of text: red for errors, amber for warnings
    that need attention, and a muted/dim tone for warnings that are just
    expected/informational (e.g. a new entry's mount point not existing
    yet) so they read as distinct from something worth worrying about."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for e in errors:
        label = Gtk.Label(label=f"ERROR: {e}", xalign=0, wrap=True, selectable=True)
        label.add_css_class("error")
        label.add_css_class("caption")
        box.append(label)
    for w in warnings:
        label = Gtk.Label(label=f"WARNING: {w}", xalign=0, wrap=True, selectable=True)
        label.add_css_class("dim-label" if "expected for a new entry" in w else "warning")
        label.add_css_class("caption")
        box.append(label)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_child(box)
    scrolled.set_max_content_height(max_height)
    scrolled.set_propagate_natural_height(True)
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    return scrolled


class EntryRow(Gtk.ListBoxRow):
    def __init__(self, entry: Entry, position: int, node, dimmed: bool, is_system_drive: bool,
                 is_grouped_child: bool = False, is_critical: bool = False, critical_unlocked: bool = False):
        super().__init__()
        self.position = position
        self.entry = entry
        self.transport = describe_transport(node.get("tran")) if node else "Unknown"
        self.is_grouped_child = is_grouped_child
        self.is_critical = is_critical
        drive_name = display_name(node, fallback=entry.device)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_top(4 if is_grouped_child else 8)
        row.set_margin_bottom(4 if is_grouped_child else 8)
        row.set_margin_start(32 if is_grouped_child else 12)
        row.set_margin_end(12)

        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, valign=Gtk.Align.START)
        left_box.set_size_request(124 if is_grouped_child else 140, -1)

        if is_grouped_child:
            drive_label = Gtk.Label(label="↳ same drive", xalign=0)
            drive_label.add_css_class("dim-label")
            drive_label.add_css_class("caption")
        else:
            drive_label = Gtk.Label(label=drive_name, xalign=0)
            drive_label.add_css_class("heading")
            if dimmed:
                drive_label.add_css_class("dim-label")
        left_box.append(drive_label)

        if is_system_drive and not is_grouped_child:
            system_icon = Gtk.Image.new_from_icon_name("drive-harddisk-system-symbolic")
            system_icon.set_tooltip_text("This is part of your system/boot drive")
            system_icon.add_css_class("accent")
            left_box.append(system_icon)

        if is_critical:
            if critical_unlocked:
                lock_icon = Gtk.Image.new_from_icon_name("changes-allow-symbolic")
                lock_icon.set_tooltip_text("Unlocked for this session")
            else:
                lock_icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
                lock_icon.set_tooltip_text(
                    "Locked — part of your boot/system drive. Click the lock button in the "
                    "header to unlock before editing."
                )
                lock_icon.add_css_class("warning")
            left_box.append(lock_icon)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)

        title = Gtk.Label(label=f"{entry.mountpoint}  ·  {entry.fstype}  ·  {self.transport}", xalign=0)
        title.add_css_class("heading")
        if dimmed:
            title.add_css_class("dim-label")

        subtitle = Gtk.Label(
            label=f"{entry.device}    options={entry.options}  dump={entry.dump}  pass={entry.passno}",
            xalign=0,
        )
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")

        details.append(title)
        details.append(subtitle)

        row.append(left_box)
        row.append(details)
        self.set_child(row)


class NetworkShareDialog(Adw.Dialog):
    """Add a NAS / network share.

    Kept separate from the block-device form because the useful questions
    are different (server and share name, not a UUID) and because the
    options that make a network mount safe -- mount-on-access rather than
    at boot -- aren't something the user should have to know to type.
    """

    def __init__(self, parent_window: Gtk.Window, on_save):
        super().__init__()
        self.parent_window = parent_window
        self.on_save = on_save
        self.set_title("Add a network drive")
        self.set_content_width(480)
        self.set_follows_content_size(True)
        self.set_presentation_mode(Adw.DialogPresentationMode.FLOATING)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: self.close())
        header.pack_start(cancel)

        add = Gtk.Button(label="Add")
        add.add_css_class("suggested-action")
        add.connect("clicked", self._on_add_clicked)
        header.pack_end(add)
        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()

        server_group = Adw.PreferencesGroup(title="Where is it?")
        self.kind_row = Adw.ComboRow(title="Type")
        self.kind_row.set_model(Gtk.StringList.new([
            "Windows / NAS share (SMB)",
            "NFS export",
        ]))
        self.kind_row.set_selected(0)
        self.kind_row.connect("notify::selected", lambda *_: self._sync_kind())
        server_group.add(self.kind_row)

        self.server_row = Adw.EntryRow(title="Server")
        self.server_row.set_tooltip_text("Hostname or IP address, e.g. nas.local or 192.168.1.50")
        self.scan_button = Gtk.Button(
            icon_name="system-search-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Look for file servers on your network",
        )
        self.scan_button.connect("clicked", self._on_scan_clicked)
        self.server_row.add_suffix(self.scan_button)
        server_group.add(self.server_row)

        self.share_row = Adw.EntryRow(title="Share name")
        self.share_row.connect("changed", lambda *_: self._suggest_mountpoint())
        self.browse_button = Gtk.Button(
            icon_name="folder-remote-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="List what this server is sharing",
        )
        self.browse_button.connect("clicked", self._on_browse_clicked)
        self.share_row.add_suffix(self.browse_button)
        server_group.add(self.share_row)
        page.add(server_group)

        mount_group = Adw.PreferencesGroup(title="Where should it appear?")
        self.mount_row = Adw.EntryRow(title="Mount point")
        mount_group.add(self.mount_row)
        page.add(mount_group)

        self.auth_group = Adw.PreferencesGroup(
            title="Sign in",
            description="Leave both blank for a guest/public share. The password is saved to a "
                        "root-only file (chmod 600) and referenced from fstab — it is never "
                        "written into fstab itself, which is readable by everyone.",
        )
        self.username_row = Adw.EntryRow(title="Username")
        self.auth_group.add(self.username_row)
        self.password_row = Adw.PasswordEntryRow(title="Password")
        self.auth_group.add(self.password_row)
        page.add(self.auth_group)

        self.note_group = Adw.PreferencesGroup()
        self.note_label = Gtk.Label(xalign=0, wrap=True)
        self.note_label.add_css_class("caption")
        self.note_group.add(self.note_label)
        page.add(self.note_group)

        toolbar_view.set_content(page)
        self.set_child(toolbar_view)
        self._sync_kind()

    def _current_kind(self):
        return network.SMB if self.kind_row.get_selected() == 0 else network.NFS

    def _sync_kind(self):
        kind = self._current_kind()
        is_smb = kind == network.SMB
        self.auth_group.set_visible(is_smb)
        self.share_row.set_title("Share name" if is_smb else "Export path")
        self.share_row.set_tooltip_text(
            "The share as advertised by the NAS, e.g. media"
            if is_smb
            else "The exported path on the server, e.g. /volume1/media"
        )

        notes = [
            "This share is mounted the first time you open it, not during startup — "
            "so if the server is switched off, your machine still boots normally."
        ]
        missing = network.helper_missing(kind)
        if missing:
            notes.append(network.install_hint(kind))
        self.note_label.set_text("\n\n".join(notes))
        self.note_label.remove_css_class("warning")
        if missing:
            self.note_label.add_css_class("warning")
        self._suggest_mountpoint()

    def _suggest_mountpoint(self):
        # Only fill in a suggestion while the user hasn't typed their own.
        if getattr(self, "_mount_edited", False):
            return
        share_text = self.share_row.get_text().strip()
        if not share_text:
            return
        probe = network.NetworkShare(
            self._current_kind(), self.server_row.get_text().strip() or "server", share_text, ""
        )
        self.mount_row.set_text(network.default_mountpoint(probe))

    def _choose_from(self, heading, description, items, on_choose):
        """A small list dialog for picking a discovered server or share."""
        dialog = Adw.Dialog()
        dialog.set_title(heading)
        dialog.set_content_width(420)
        dialog.set_follows_content_size(True)
        dialog.set_presentation_mode(Adw.DialogPresentationMode.FLOATING)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description=description)

        for title, subtitle, value in items:
            row = Adw.ActionRow(title=title, subtitle=subtitle or "")
            row.set_activatable(True)

            def picked(_row, chosen=value):
                dialog.close()
                on_choose(chosen)

            row.connect("activated", picked)
            group.add(row)

        page.add(group)
        toolbar.set_content(page)
        dialog.set_child(toolbar)
        dialog.present(self)

    def _busy(self, button, busy, label):
        button.set_sensitive(not busy)
        self.note_label.set_text(label) if busy else self._sync_kind()

    def _on_scan_clicked(self, button):
        # A connect-scan of the local subnet; off the main loop so the
        # dialog stays responsive while it runs.
        self._busy(self.scan_button, True, "Looking for file servers on your network…")
        _run_in_thread(network.discover_servers, self._on_scan_done)

    def _on_scan_done(self, servers):
        self._busy(self.scan_button, False, "")
        if not servers:
            self._busy(self.scan_button, True,
                       "No file servers answered on this network. If the NAS is on, "
                       "you can still type its address in by hand.")
            self.scan_button.set_sensitive(True)
            return

        items = []
        for server in servers:
            offers = " and ".join("SMB" if s == network.SMB else "NFS" for s in server.services)
            items.append((server.name or server.host, f"{server.host} — offers {offers}", server))
        self._choose_from("Servers found", "Pick one to fill in its address.", items, self._apply_server)

    def _apply_server(self, server):
        self.server_row.set_text(server.host)
        # Follow what the server actually offers, rather than leaving a
        # type selected that it can't do.
        if network.NFS in server.services and network.SMB not in server.services:
            self.kind_row.set_selected(1)
        elif network.SMB in server.services and network.NFS not in server.services:
            self.kind_row.set_selected(0)

    def _on_browse_clicked(self, button):
        server = self.server_row.get_text().strip()
        if not server:
            self.server_row.add_css_class("error")
            return
        self.server_row.remove_css_class("error")

        kind = self._current_kind()
        username = self.username_row.get_text().strip()
        password = self.password_row.get_text()
        self._busy(self.browse_button, True, f"Asking {server} what it shares…")

        def work():
            if kind == network.NFS:
                return network.list_nfs_exports(server)
            return network.list_smb_shares(server, username, password)

        _run_in_thread(work, self._on_browse_done)

    def _on_browse_done(self, shares):
        self._busy(self.browse_button, False, "")
        self.browse_button.set_sensitive(True)
        if not shares:
            hint = (
                "No exports came back. The server may not be sharing over NFS."
                if self._current_kind() == network.NFS
                else "No shares came back. Most servers won't list their shares "
                     "without a username and password — fill those in and try again."
            )
            self._busy(self.browse_button, True, hint)
            self.browse_button.set_sensitive(True)
            return
        items = [(s, None, s) for s in shares]
        self._choose_from("Shares available", "Pick one to fill it in.", items, self._apply_share)

    def _apply_share(self, share_name):
        self._mount_edited = False
        self.share_row.set_text(share_name)

    def _on_add_clicked(self, button):
        server = self.server_row.get_text().strip()
        share = self.share_row.get_text().strip()
        mountpoint = self.mount_row.get_text().strip()

        ok = True
        for row, value in ((self.server_row, server), (self.share_row, share), (self.mount_row, mountpoint)):
            row.remove_css_class("error") if value else row.add_css_class("error")
            ok = ok and bool(value)
        if not ok:
            return

        kind = self._current_kind()
        share_obj = network.NetworkShare(
            kind=kind,
            server=server,
            share=share,
            mountpoint=mountpoint,
            username=self.username_row.get_text().strip() or None if kind == network.SMB else None,
            password=self.password_row.get_text() or None if kind == network.SMB else None,
        )
        self.on_save(share_obj)
        self.close()


class AboutDialog(Adw.Dialog):
    """About window with a working "Check for Updates" button.

    Hand-built rather than using Adw.AboutDialog, which offers no way to
    attach a button that runs code -- only static links -- and the point
    here is to actually perform the check and report the result inline.
    """

    def __init__(self):
        super().__init__()
        self.set_title("About Set the Table")
        self.set_content_width(420)
        self.set_follows_content_size(True)
        self.set_presentation_mode(Adw.DialogPresentationMode.FLOATING)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()

        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading.set_margin_top(12)
        heading.set_margin_bottom(6)

        icon = Gtk.Image.new_from_icon_name("drive-multidisk")
        icon.set_pixel_size(64)
        heading.append(icon)

        name = Gtk.Label(label="Set the Table")
        name.add_css_class("title-1")
        heading.append(name)

        tagline = Gtk.Label(label="An Auto-Mount FSTAB Assistant")
        tagline.add_css_class("dim-label")
        heading.append(tagline)

        version = Gtk.Label(label=f"Version {__version__}")
        version.add_css_class("dim-label")
        version.add_css_class("caption")
        heading.append(version)

        heading_group = Adw.PreferencesGroup()
        heading_group.add(heading)
        page.add(heading_group)

        update_group = Adw.PreferencesGroup()
        self.update_row = Adw.ActionRow(
            title="Check for Updates",
            subtitle="Asks GitHub for the latest release. Nothing is sent until you click.",
        )
        self.update_spinner = Adw.Spinner()
        self.update_spinner.set_visible(False)
        self.update_row.add_suffix(self.update_spinner)

        self.update_button = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        self.update_button.connect("clicked", self._on_check_clicked)
        self.update_row.add_suffix(self.update_button)
        update_group.add(self.update_row)

        self.download_row = Adw.ActionRow(title="Open the download page")
        self.download_row.set_activatable(True)
        self.download_row.add_suffix(Gtk.Image.new_from_icon_name("external-link-symbolic"))
        self.download_row.set_visible(False)
        self.download_row.connect("activated", self._on_download_clicked)
        update_group.add(self.download_row)
        page.add(update_group)

        info_group = Adw.PreferencesGroup()
        info_group.set_description(
            "Add drives to /etc/fstab so they mount automatically at startup — "
            "with a backup, validation, and a dry-run before anything is written."
        )
        page.add(info_group)

        toolbar_view.set_content(page)
        self.set_child(toolbar_view)

        self._download_url = updates.RELEASES_URL

    def _on_check_clicked(self, button):
        # Network I/O blocks; keep it off the main loop so the dialog stays
        # responsive (same reasoning as the pkexec calls).
        self.update_button.set_sensitive(False)
        self.update_spinner.set_visible(True)
        self.update_row.set_subtitle("Checking…")
        self.download_row.set_visible(False)
        _run_in_thread(lambda: updates.check_for_update(__version__), self._on_check_done)

    def _on_check_done(self, result):
        self.update_button.set_sensitive(True)
        self.update_spinner.set_visible(False)
        self.update_row.set_subtitle(result.message)

        for css_class in ("success", "warning", "error"):
            self.update_row.remove_css_class(css_class)

        if result.status == updates.UPDATE_AVAILABLE:
            self.update_row.add_css_class("success")
            self._download_url = result.url
            self.download_row.set_subtitle(result.url)
            self.download_row.set_visible(True)
        elif result.status == updates.ERROR:
            self.update_row.add_css_class("warning")

    def _on_download_clicked(self, row):
        launcher = Gtk.UriLauncher.new(self._download_url)
        launcher.launch(self.get_root(), None, None)


class DevicePickerDialog(Adw.Dialog):
    """Lets the user pick a detected block device to add. Always identifies
    the device by UUID (falling back to LABEL, then raw path, only when a
    UUID isn't available) -- that's the right choice essentially always, so
    it isn't exposed as a user-facing decision."""

    def __init__(self, parent_window: Gtk.Window, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.parent_window = parent_window
        self._windows_rows = []
        self._suggested_rows = []
        self._other_rows = []
        self._already_used_rows = []
        self._usb_rows = []
        self.set_title("Choose a device")
        self.set_content_width(560)
        self.set_content_height(480)
        # Force floating-dialog presentation (dimmed backdrop, click-outside
        # to dismiss) rather than letting it fall back to a bottom sheet on
        # a smaller window, where that click-outside behavior can differ.
        self.set_presentation_mode(Adw.DialogPresentationMode.FLOATING)

        # Explicit Escape-to-close: added at the CAPTURE phase so it fires
        # before any child row/switch could otherwise consume the key.
        escape_controller = Gtk.EventControllerKey()
        escape_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        escape_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(escape_controller)

        # Explicit click-outside-to-close: Adw.Dialog's floating presentation
        # is allocated the whole window (backdrop included) and doesn't
        # dismiss on a backdrop click by default -- that area picks as an
        # internal WindowHandle, not something wired to close the dialog.
        # Detect it ourselves: if the click didn't land inside our own
        # content, treat it as a request to dismiss.
        click_controller = Gtk.GestureClick()
        click_controller.connect("pressed", self._on_dialog_clicked)
        self.add_controller(click_controller)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()

        filter_group = Adw.PreferencesGroup()
        self.mounted_only_switch = Adw.SwitchRow(
            title="Only show currently mounted drives",
            subtitle="Turn off to see every detected drive, mounted or not",
        )
        self.mounted_only_switch.set_active(True)
        self.mounted_only_switch.connect("notify::active", lambda *_: self._populate())
        filter_group.add(self.mounted_only_switch)
        page.add(filter_group)

        self.windows_group = Adw.PreferencesGroup(
            title="Add your Windows drives",
            description="NTFS is the filesystem Windows uses for its drives by default.",
        )
        self.windows_group.set_tooltip_text("NTFS is the filesystem Windows uses for its drives by default.")
        page.add(self.windows_group)

        self.suggested_group = Adw.PreferencesGroup(title="Suggested")
        page.add(self.suggested_group)

        self.other_group = Adw.PreferencesGroup(title="Detected devices")
        page.add(self.other_group)

        # Devices a current fstab entry already points at -- nothing left
        # to do with them, so they sit below the actionable sections.
        self.already_used_group = Adw.PreferencesGroup(
            title="Already used",
            description="These drives are already covered by an entry in your list.",
        )
        page.add(self.already_used_group)

        # Always last: USB is removable/plug-and-play by nature, so a
        # permanent fstab entry is usually not what you want -- still
        # available, just clearly the least-recommended choice.
        self.usb_group = Adw.PreferencesGroup(
            title="USB drives",
            description="Removable USB drives are normally handled automatically when plugged in "
                        "and don't need a permanent fstab entry.",
        )
        self.usb_group.set_tooltip_text(
            "Removable USB drives are normally handled automatically when plugged in "
            "and don't need a permanent fstab entry."
        )
        page.add(self.usb_group)

        toolbar_view.set_content(page)
        self.set_child(toolbar_view)

        try:
            self._devices = list_block_devices()
            self._error = None
        except RuntimeError as e:
            self._devices = []
            self._error = str(e)

        # If limiting to mounted drives wouldn't leave anything worth
        # adding (e.g. you've already added everything you had plugged
        # in), default to showing everything instead of an empty/all-dimmed
        # list you'd have to know to turn the switch off to escape.
        if not self._error:
            mounted_candidates = [d for d in self._devices if get_mountpoint(d)]
            buckets = self._classify(mounted_candidates)
            # Only the actionable sections count toward "worth it" -- USB is
            # deliberately the least-recommended category, and already-used
            # devices have nothing left to do, so neither should be the
            # reason to default to the narrower mounted-only view.
            if not buckets.windows and not buckets.suggested:
                self.mounted_only_switch.set_active(False)

        self._populate()

    def _classify(self, devices) -> "_DeviceBuckets":
        """Sort devices into the picker's sections.

        `already_used` is anything a current fstab entry already points at
        -- you've handled it, so it's informational rather than actionable.
        `windows` (NTFS) is called out on its own because an internal
        Windows drive often isn't auto-mounted the way removable media is,
        so it wouldn't otherwise surface. `usb` is the mirror image:
        deliberately deprioritized into its own always-last section, since
        removable media is normally handled automatically and rarely needs
        a permanent fstab entry -- still offered if you really want one.
        `suggested` is everything else that's actually mounted (the "I
        already have this plugged in, make it permanent" case). Whatever
        is left -- not used, not USB, not Windows -- lands in `other`.

        While "Only show currently mounted drives" is on, everything in
        view is already mounted, so splitting Windows out into its own
        section adds a division that isn't doing much work -- Windows
        drives fold into `suggested` instead. With the filter off (browsing
        every detected drive, mounted or not), the split still earns its
        keep, since Windows drives may be the only ones worth calling out
        in a longer, noisier list."""
        root_info = current_root_identity()
        quick_index = {}
        for d in self._devices:
            if d.get("uuid"):
                quick_index[f"UUID={d['uuid']}"] = d
            if d.get("name"):
                quick_index[f"/dev/{d['name']}"] = d
        root_node = _resolve_root_node(quick_index, root_info)
        existing_entries = self.parent_window._entries() if self.parent_window else []
        split_windows = not self.mounted_only_switch.get_active()

        windows, suggested, other, already_used, usb = [], [], [], [], []
        for node in devices:
            is_system = _is_system_drive(node, root_node)
            is_swap = (node.get("fstype") or "").lower() == "swap"
            is_used = _existing_use_count(node, existing_entries) > 0
            is_mounted = bool(get_mountpoint(node))
            is_usb = (node.get("tran") or "").lower() == "usb"
            is_windows = (node.get("fstype") or "").lower() in WINDOWS_FSTYPES

            if is_used:
                already_used.append(node)
            elif is_usb:
                usb.append(node)
            elif is_system or is_swap:
                # Not in fstab yet, but still not something to suggest
                # adding -- e.g. a zram swap device the kernel manages.
                other.append(node)
            elif is_windows and split_windows:
                windows.append(node)
            elif is_mounted:
                suggested.append(node)
            else:
                other.append(node)
        return _DeviceBuckets(windows, suggested, other, already_used, usb, root_node, existing_entries)

    def _build_row(self, node, root_node, existing_entries, dim):
        name = node.get("name")
        fstype = node.get("fstype") or "?"
        size = node.get("size") or "?"
        uuid = node.get("uuid") or "—"
        label = node.get("label") or "—"
        mp = get_mountpoint(node) or "—"
        transport = describe_transport(node.get("tran"))
        drive_name = display_name(node, fallback=f"/dev/{name}")
        title = f"{drive_name}  ({transport})"
        if _is_system_drive(node, root_node):
            title += "  ·  System drive"
        if (node.get("fstype") or "").lower() == "swap":
            title += "  ·  Swap"

        subtitle = f"/dev/{name} · {fstype} · {size} · UUID={uuid} · LABEL={label} · mounted at {mp}"
        used_count = _existing_use_count(node, existing_entries)
        if used_count:
            noun = "entry" if used_count == 1 else "entries"
            subtitle = f"Already used by {used_count} {noun} in your list · {subtitle}"

        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(True)
        row.connect("activated", self._on_row_activated, node)
        if dim:
            row.add_css_class("dim-label")
        return row

    def _populate(self):
        for row in self._windows_rows:
            self.windows_group.remove(row)
        for row in self._suggested_rows:
            self.suggested_group.remove(row)
        for row in self._other_rows:
            self.other_group.remove(row)
        for row in self._already_used_rows:
            self.already_used_group.remove(row)
        for row in self._usb_rows:
            self.usb_group.remove(row)
        self._windows_rows = []
        self._suggested_rows = []
        self._other_rows = []
        self._already_used_rows = []
        self._usb_rows = []
        self.other_group.set_description("")

        if self._error:
            self.windows_group.set_visible(False)
            self.suggested_group.set_visible(False)
            self.already_used_group.set_visible(False)
            self.usb_group.set_visible(False)
            self.other_group.set_visible(True)
            self.other_group.set_title("Detected devices")
            self.other_group.set_description(self._error)
            return

        mounted_only = self.mounted_only_switch.get_active()
        devices = [d for d in self._devices if not mounted_only or get_mountpoint(d)]

        if not devices:
            self.windows_group.set_visible(False)
            self.suggested_group.set_visible(False)
            self.already_used_group.set_visible(False)
            self.usb_group.set_visible(False)
            self.other_group.set_visible(True)
            self.other_group.set_title("Detected devices")
            self.other_group.set_description(
                "No currently mounted drives detected — turn off the switch above to see everything."
                if mounted_only
                else "No block devices with a filesystem were detected."
            )
            return

        b = self._classify(devices)

        self.windows_group.set_visible(bool(b.windows))
        for node in b.windows:
            row = self._build_row(node, b.root_node, b.existing_entries, dim=False)
            self.windows_group.add(row)
            self._windows_rows.append(row)

        self.suggested_group.set_visible(bool(b.suggested))
        for node in b.suggested:
            row = self._build_row(node, b.root_node, b.existing_entries, dim=False)
            self.suggested_group.add(row)
            self._suggested_rows.append(row)

        has_other_sections = bool(b.windows or b.suggested or b.already_used or b.usb)
        self.other_group.set_title("Other detected devices" if has_other_sections else "Detected devices")
        self.other_group.set_visible(bool(b.other))
        for node in b.other:
            row = self._build_row(node, b.root_node, b.existing_entries, dim=True)
            self.other_group.add(row)
            self._other_rows.append(row)

        self.already_used_group.set_visible(bool(b.already_used))
        for node in b.already_used:
            row = self._build_row(node, b.root_node, b.existing_entries, dim=True)
            self.already_used_group.add(row)
            self._already_used_rows.append(row)

        self.usb_group.set_visible(bool(b.usb))
        for node in b.usb:
            row = self._build_row(node, b.root_node, b.existing_entries, dim=False)
            self.usb_group.add(row)
            self._usb_rows.append(row)

    def _on_row_activated(self, row, node):
        device = identifier_for(node, prefer="UUID")
        if device is None:
            device = f"/dev/{node.get('name')}"
        self.on_pick(device, node)
        self.close()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_dialog_clicked(self, gesture, n_press, x, y):
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        if not _is_descendant(picked, self.get_child()):
            self.close()


class EntryFormDialog(Adw.Dialog):
    """Add/edit form for a single fstab entry."""

    def __init__(self, parent_window: Gtk.Window, entry, on_save):
        super().__init__()
        self.parent_window = parent_window
        self.on_save = on_save
        self.set_title("Edit entry" if entry else "Add entry")
        self.set_content_width(460)
        self.set_follows_content_size(True)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda b: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(save_btn)

        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()

        source_group = Adw.PreferencesGroup(title="Source")
        pick_row = Adw.ActionRow(title="Pick a detected device")
        pick_btn = Gtk.Button(icon_name="view-list-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Browse detected devices")
        pick_btn.connect("clicked", self._on_pick_device)
        pick_row.add_suffix(pick_btn)
        source_group.add(pick_row)

        self.device_row = Adw.EntryRow(title="Device / source")
        source_group.add(self.device_row)
        page.add(source_group)

        mount_group = Adw.PreferencesGroup(title="Mount")
        self.mount_row = Adw.EntryRow(title="Mount point")
        browse_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Choose folder")
        browse_btn.connect("clicked", self._on_browse_folder)
        self.mount_row.add_suffix(browse_btn)
        mount_group.add(self.mount_row)

        self.fstype_row = Adw.EntryRow(title="Filesystem type")
        mount_group.add(self.fstype_row)

        self.options_row = Adw.EntryRow(title="Mount options")
        mount_group.add(self.options_row)
        page.add(mount_group)

        adv_group = Adw.PreferencesGroup(title="Advanced")
        self.dump_row = Adw.SpinRow.new_with_range(0, 1, 1)
        self.dump_row.set_title("Dump")
        adv_group.add(self.dump_row)

        self.pass_row = Adw.SpinRow.new_with_range(0, 2, 1)
        self.pass_row.set_title("Pass (fsck order)")
        adv_group.add(self.pass_row)
        page.add(adv_group)

        toolbar_view.set_content(page)
        self.set_child(toolbar_view)

        if entry:
            self.device_row.set_text(entry.device)
            self.mount_row.set_text(entry.mountpoint)
            self.fstype_row.set_text(entry.fstype)
            self.options_row.set_text(entry.options)
            self.dump_row.set_value(entry.dump)
            self.pass_row.set_value(entry.passno)
        else:
            self.options_row.set_text("defaults")
            self.pass_row.set_value(2)

    def _on_pick_device(self, button):
        picker = DevicePickerDialog(self.parent_window, on_pick=self._apply_picked_device)
        picker.present(self)

    def _apply_picked_device(self, device, node):
        self.device_row.set_text(device)
        mountpoint = get_mountpoint(node) or ""
        if mountpoint and not self.mount_row.get_text():
            self.mount_row.set_text(mountpoint)
        fstype = node.get("fstype") or ""
        if fstype and not self.fstype_row.get_text():
            self.fstype_row.set_text(fstype)

    def _on_browse_folder(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose mount point")

        def _on_chosen(source, result):
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if path:
                self.mount_row.set_text(path)

        dialog.select_folder(self.parent_window, None, _on_chosen)

    def _on_save_clicked(self, button):
        device = self.device_row.get_text().strip()
        mountpoint = self.mount_row.get_text().strip()
        fstype = self.fstype_row.get_text().strip()
        options = self.options_row.get_text().strip() or "defaults"
        dump = int(self.dump_row.get_value())
        passno = int(self.pass_row.get_value())

        all_filled = True
        for row, value in ((self.device_row, device), (self.mount_row, mountpoint), (self.fstype_row, fstype)):
            if value:
                row.remove_css_class("error")
            else:
                row.add_css_class("error")
                all_filled = False
        if not all_filled:
            return

        self.on_save(Entry(device, mountpoint, fstype, options, dump, passno))
        self.close()


class AutoFstabWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, path: str):
        super().__init__(application=app, title="Set the Table", default_width=820, default_height=580)
        self.path = path
        self.dirty = False
        self._force_close = False
        self.critical_unlocked = False
        self._auth_toast = None
        # Credential files to write, root-owned and 0600, next time we
        # save -- batched so adding a share costs one password prompt,
        # not one per share plus another for the fstab write.
        self._pending_credentials = []
        self.records = parse_fstab(path) if os.path.exists(path) else []

        self._build_ui()
        self._refresh_list()
        self.connect("close-request", self._on_close_request)

        if path == "/etc/fstab" and os.geteuid() != 0:
            # Deferred until the window is actually mapped to the screen --
            # adding a toast (which slides in with an animation) before the
            # widget hierarchy has a real size allocation, which is still
            # the case at the end of __init__ here, produces exactly the
            # kind of layout glitch reported (content sliding off to the
            # left). "map" only fires once real geometry exists.
            self.connect("map", self._show_startup_toast)

    def _show_startup_toast(self, window):
        self.disconnect_by_func(self._show_startup_toast)
        self.toast_overlay.add_toast(
            Adw.Toast.new("You'll be asked for your password when you Save.")
        )

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.title_widget = Adw.WindowTitle(title="Set the Table", subtitle=self.path)
        header.set_title_widget(self.title_widget)

        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a drive (recommended settings applied automatically)")
        add_btn.connect("clicked", self._on_add_clicked)
        header.pack_start(add_btn)

        self.edit_btn = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit selected entry")
        self.edit_btn.set_sensitive(False)
        self.edit_btn.connect("clicked", self._on_edit_clicked)
        header.pack_start(self.edit_btn)

        self.remove_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove selected entry")
        self.remove_btn.set_sensitive(False)
        self.remove_btn.connect("clicked", self._on_remove_clicked)
        header.pack_start(self.remove_btn)

        self.lock_btn = Gtk.Button(
            icon_name="changes-prevent-symbolic",
            tooltip_text="System-critical entries (boot, system drive) are locked — click to unlock",
        )
        self.lock_btn.connect("clicked", self._on_lock_toggle_clicked)
        header.pack_start(self.lock_btn)

        # pack_end stacks inward from the window edge, so packing Save
        # first puts it outermost (the conventional spot for the primary
        # action), with Reload right next to it -- the natural next click
        # to confirm a save actually took effect, without restarting the
        # app -- then Validate closer to the title.
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(self.save_btn)

        self.reload_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Refresh / Reconnect — re-read the file, then mount anything in it that isn't mounted",
        )
        self.reload_btn.connect("clicked", lambda b: self._on_reload(None, None))
        header.pack_end(self.reload_btn)

        validate_btn = Gtk.Button(label="Validate")
        validate_btn.connect("clicked", self._on_validate_clicked)
        header.pack_end(validate_btn)

        menu = Gio.Menu()
        menu.append("Add entry manually…", "win.add_manual")
        menu.append("Add a network drive…", "win.add_network")
        menu.append("Refresh / Reconnect", "win.reload")
        menu.append("Open other file…", "win.open")
        menu.append("About Set the Table", "win.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu")
        header.pack_end(menu_btn)

        self._add_action("add_manual", self._on_add_manual_clicked)
        self._add_action("add_network", self._on_add_network_clicked)
        self._add_action("reload", self._on_reload)
        self._add_action("open", self._on_open)
        self._add_action("about", self._on_about)

        toolbar_view.add_top_bar(header)

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_bar.set_margin_top(6)
        filter_bar.set_margin_bottom(6)
        filter_bar.set_margin_start(12)
        filter_bar.set_margin_end(12)
        filter_bar.append(Gtk.Label(label="Filter by drive:"))
        self.drive_filter_dropdown = Gtk.DropDown()
        self.drive_filter_dropdown.set_model(Gtk.StringList.new(["All drives"]))
        self.drive_filter_dropdown.set_selected(0)
        self.drive_filter_dropdown.connect("notify::selected", self._on_drive_filter_changed)
        filter_bar.append(self.drive_filter_dropdown)
        toolbar_view.add_top_bar(filter_bar)

        self.pending_listbox = Gtk.ListBox()
        self.pending_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.pending_listbox.add_css_class("boxed-list")
        self.pending_listbox.connect("row-selected", self._on_pending_row_selected)
        self.pending_listbox.set_filter_func(self._filter_by_drive)

        pending_scrolled = Gtk.ScrolledWindow()
        pending_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pending_scrolled.set_vexpand(True)
        pending_scrolled.set_child(self.pending_listbox)

        pending_label = Gtk.Label(label="Pending changes", xalign=0)
        pending_label.add_css_class("heading")

        self.pending_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pending_section.set_vexpand(True)
        self.pending_section.append(pending_label)
        self.pending_section.append(pending_scrolled)

        self.existing_listbox = Gtk.ListBox()
        self.existing_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.existing_listbox.add_css_class("boxed-list")
        self.existing_listbox.connect("row-selected", self._on_existing_row_selected)
        self.existing_listbox.set_filter_func(self._filter_by_drive)
        self.existing_listbox.set_placeholder(
            Adw.StatusPage(
                title="No fstab entries",
                description="Use the + button to add one.",
                icon_name="drive-harddisk-symbolic",
            )
        )

        existing_scrolled = Gtk.ScrolledWindow()
        existing_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        existing_scrolled.set_vexpand(True)
        existing_scrolled.set_child(self.existing_listbox)

        existing_label = Gtk.Label(label="Already in /etc/fstab", xalign=0)
        existing_label.add_css_class("heading")
        existing_label.add_css_class("dim-label")

        existing_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        existing_section.set_vexpand(True)
        existing_section.append(existing_label)
        existing_section.append(existing_scrolled)

        # No shared outer scroll -- each section scrolls its own list
        # independently and expands/shrinks with the window, splitting the
        # available height between them (Gtk.Box distributes space equally
        # among vexpand children; a hidden section gets excluded entirely,
        # so "Already in /etc/fstab" alone takes the full height while
        # "Pending changes" has nothing in it).
        lists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        lists_box.set_vexpand(True)
        lists_box.append(self.pending_section)
        lists_box.append(existing_section)

        clamp = Adw.Clamp(maximum_size=700)
        clamp.set_vexpand(True)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(18)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        clamp.set_child(lists_box)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(clamp)

        toolbar_view.set_content(self.toast_overlay)
        self.set_content(toolbar_view)

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    # -- data helpers -------------------------------------------------------

    def _entries(self):
        return [r for r in self.records if isinstance(r, Entry)]

    def _entries_with_positions(self):
        return [(i, r) for i, r in enumerate(self.records) if isinstance(r, Entry)]

    @staticmethod
    def _clear_listbox(listbox):
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            listbox.remove(child)
            child = next_child

    def _append_grouped_rows(self, listbox, items):
        """items: list of (pos, entry, node, is_system, is_critical). Renders
        entries that share a physical device consecutively, with the 2nd+
        one indented and marked as linked, while preserving each group's
        first-appearance order among the other entries."""
        def key_of(item):
            pos, entry, node = item[0], item[1], item[2]
            return _device_group_key(node, entry.device, pos)

        first_seen = {}
        for i, item in enumerate(items):
            first_seen.setdefault(key_of(item), i)
        ordered = sorted(enumerate(items), key=lambda pair: (first_seen[key_of(pair[1])], pair[0]))

        rendered_keys = set()
        for _, item in ordered:
            pos, entry, node, is_system, is_critical = item
            key = key_of(item)
            is_child = key in rendered_keys
            rendered_keys.add(key)
            row = EntryRow(
                entry, pos, node, dimmed=entry.existing, is_system_drive=is_system,
                is_grouped_child=is_child, is_critical=is_critical, critical_unlocked=self.critical_unlocked,
            )
            self._row_transports.append(row.transport)
            listbox.append(row)

    def _refresh_list(self):
        self._clear_listbox(self.pending_listbox)
        self._clear_listbox(self.existing_listbox)

        index = resolve_device_index()
        root_info = current_root_identity()
        root_node = _resolve_root_node(index, root_info)
        self._row_transports = []
        pending_items = []
        existing_items = []
        for pos, entry in self._entries_with_positions():
            node = index.get(entry.device)
            is_system = _is_system_drive(node, root_node)
            is_critical = _is_critical_entry(entry, node, root_node)
            item = (pos, entry, node, is_system, is_critical)
            (pending_items if not entry.existing else existing_items).append(item)

        self._append_grouped_rows(self.pending_listbox, pending_items)
        self._append_grouped_rows(self.existing_listbox, existing_items)

        self.pending_section.set_visible(len(pending_items) > 0)
        self._update_edit_remove_sensitivity()
        self._update_drive_filter_options()

    # -- drive (transport) filter ---------------------------------------------

    def _selected_drive_filter(self):
        model = self.drive_filter_dropdown.get_model()
        idx = self.drive_filter_dropdown.get_selected()
        if model is None or idx == Gtk.INVALID_LIST_POSITION:
            return "All drives"
        item = model.get_item(idx)
        return item.get_string() if item else "All drives"

    def _update_drive_filter_options(self):
        transports = sorted(set(self._row_transports))
        current = self._selected_drive_filter()
        options = ["All drives"] + transports
        self.drive_filter_dropdown.set_model(Gtk.StringList.new(options))
        self.drive_filter_dropdown.set_selected(options.index(current) if current in options else 0)

    def _filter_by_drive(self, row):
        selected = self._selected_drive_filter()
        return selected == "All drives" or row.transport == selected

    def _on_drive_filter_changed(self, dropdown, pspec):
        self.pending_listbox.invalidate_filter()
        self.existing_listbox.invalidate_filter()

    # -- dialogs --------------------------------------------------------------

    def _confirm(self, heading, body, ok_label, on_result, destructive=False, extra_text=None, extra_widget=None):
        dialog = Adw.AlertDialog.new(heading, body)
        if extra_widget is not None:
            dialog.set_extra_child(extra_widget)
        elif extra_text:
            dialog.set_extra_child(_report_widget(extra_text))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", ok_label)
        dialog.set_response_appearance(
            "ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(source, result):
            response = source.choose_finish(result)
            on_result(response == "ok")

        dialog.choose(self, None, _on_response)

    def _info(self, heading, body, extra_text=None, extra_widget=None):
        dialog = Adw.AlertDialog.new(heading, body)
        if extra_widget is not None:
            dialog.set_extra_child(extra_widget)
        elif extra_text:
            dialog.set_extra_child(_report_widget(extra_text))
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    # -- actions ------------------------------------------------------------

    def _on_pending_row_selected(self, listbox, row):
        if row is not None:
            self.existing_listbox.unselect_all()
        self._update_edit_remove_sensitivity()

    def _on_existing_row_selected(self, listbox, row):
        if row is not None:
            self.pending_listbox.unselect_all()
        self._update_edit_remove_sensitivity()

    def _get_selected_row(self):
        return self.pending_listbox.get_selected_row() or self.existing_listbox.get_selected_row()

    def _update_edit_remove_sensitivity(self):
        row = self._get_selected_row()
        locked = row is not None and row.is_critical and not self.critical_unlocked
        enabled = row is not None and not locked
        self.edit_btn.set_sensitive(enabled)
        self.remove_btn.set_sensitive(enabled)
        lock_tip = "Locked — click the lock button in the header to unlock system-critical entries"
        self.edit_btn.set_tooltip_text(lock_tip if locked else "Edit selected entry")
        self.remove_btn.set_tooltip_text(lock_tip if locked else "Remove selected entry")

    def _on_lock_toggle_clicked(self, button):
        if self.critical_unlocked:
            self.critical_unlocked = False
            self._update_lock_button()
            self._refresh_list()
            self.toast_overlay.add_toast(Adw.Toast.new("System-critical entries re-locked"))
            return

        # authenticate_via_pkexec blocks until the user finishes the
        # password prompt, so this runs off the main thread to keep the
        # window responsive while it's waiting -- see _run_in_thread.
        self.lock_btn.set_sensitive(False)
        toast = Adw.Toast.new("Waiting for authentication…")
        toast.set_timeout(0)
        self._auth_toast = toast
        self.toast_overlay.add_toast(toast)
        _run_in_thread(authenticate_via_pkexec, self._on_unlock_auth_done)

    def _on_unlock_auth_done(self, result):
        self.lock_btn.set_sensitive(True)
        if self._auth_toast is not None:
            self._auth_toast.dismiss()
            self._auth_toast = None

        if result.cancelled:
            return
        if not result.ok:
            self._info("Could not unlock", "Authentication failed or is unavailable.", extra_text=result.error)
            return

        self.critical_unlocked = True
        self._update_lock_button()
        self._refresh_list()
        self.toast_overlay.add_toast(Adw.Toast.new("System-critical entries unlocked for this session"))

    def _update_lock_button(self):
        if self.critical_unlocked:
            self.lock_btn.set_icon_name("changes-allow-symbolic")
            self.lock_btn.set_tooltip_text("System-critical entries are unlocked — click to re-lock")
        else:
            self.lock_btn.set_icon_name("changes-prevent-symbolic")
            self.lock_btn.set_tooltip_text("System-critical entries (boot, system drive) are locked — click to unlock")

    def _on_add_clicked(self, button):
        DevicePickerDialog(self, on_pick=self._quick_add_device).present(self)

    def _on_add_manual_clicked(self, action, param):
        EntryFormDialog(self, entry=None, on_save=self._add_entry_confirmed).present(self)

    def _on_add_network_clicked(self, action, param):
        NetworkShareDialog(self, on_save=self._add_network_share).present(self)

    def _add_network_share(self, share):
        if share.kind == network.NFS:
            # Ask the server which NFS versions it speaks so the entry can
            # pin one. rpcinfo goes over the network, so it runs off the
            # main loop like every other blocking call here.
            toast = Adw.Toast.new(f"Checking {share.server}…")
            toast.set_timeout(0)
            self._probe_toast = toast
            self.toast_overlay.add_toast(toast)
            _run_in_thread(
                lambda: network.probe_nfs_version(share.server),
                lambda version: self._finish_network_share(share, version),
            )
            return
        self._finish_network_share(share, None)

    def _finish_network_share(self, share, nfs_version):
        if getattr(self, "_probe_toast", None) is not None:
            self._probe_toast.dismiss()
            self._probe_toast = None

        entry, credentials_path = network.build_entry(share, nfs_version)
        if credentials_path:
            self._pending_credentials.append({
                "path": credentials_path,
                "content": network.credentials_content(share.username or "", share.password or ""),
            })
        self._add_entry_confirmed(entry)

        note = f"Added {entry.mountpoint} — mounts when you first open it"
        if network.helper_missing(share.kind):
            note = f"Added {entry.mountpoint} — but the mount helper isn't installed yet"
        self.toast_overlay.add_toast(Adw.Toast.new(note))

    def _quick_add_device(self, device, node):
        settings = suggest_mount_settings(node)
        entry = Entry(
            device=device,
            mountpoint=settings["mountpoint"],
            fstype=settings["fstype"],
            options=settings["options"],
            dump=settings["dump"],
            passno=settings["passno"],
        )
        self._add_entry_confirmed(entry)
        self.toast_overlay.add_toast(
            Adw.Toast.new(f"Added {entry.mountpoint} with recommended settings — click Edit to change them")
        )

    def _add_entry_confirmed(self, entry):
        self.records.append(entry)
        self.dirty = True
        self._refresh_list()

    def _on_edit_clicked(self, button):
        row = self._get_selected_row()
        if row is None:
            return
        pos = row.position
        EntryFormDialog(
            self, entry=self.records[pos], on_save=lambda e: self._edit_entry_confirmed(pos, e)
        ).present(self)

    def _edit_entry_confirmed(self, pos, entry):
        self.records[pos] = entry
        self.dirty = True
        self._refresh_list()

    def _on_remove_clicked(self, button):
        row = self._get_selected_row()
        if row is None:
            return
        pos = row.position
        entry = self.records[pos]
        self._confirm(
            heading="Remove entry?",
            body=format_entry_line(entry),
            ok_label="Remove",
            destructive=True,
            on_result=lambda ok: self._remove_confirmed(pos) if ok else None,
        )

    def _remove_confirmed(self, pos):
        del self.records[pos]
        self.dirty = True
        self._refresh_list()

    def _on_validate_clicked(self, button):
        errors, warnings = validate_entries(self._entries())
        if not errors and not warnings:
            self._info("Validation passed", "No issues found.")
            return
        self._info(
            "Validation results",
            f"{len(errors)} error(s), {len(warnings)} warning(s).",
            extra_widget=_validation_report_widget(errors, warnings),
        )

    def _on_save_clicked(self, button):
        errors, warnings = validate_entries(self._entries())
        if errors:
            self._info("Cannot save", "Fix these issues first:", extra_widget=_validation_report_widget(errors, []))
            return
        if warnings:
            self._confirm(
                heading="Validation warnings",
                body="Continue saving despite these warnings?",
                ok_label="Continue",
                extra_widget=_validation_report_widget([], warnings),
                on_result=lambda ok: self._save_dryrun() if ok else None,
            )
        else:
            self._save_dryrun()

    def _save_dryrun(self):
        content = render_fstab(self.records)
        ok, output, tool_available = dry_run_verify(content)
        if tool_available and ok is False:
            self._confirm(
                heading="Dry-run verification failed",
                body="findmnt --verify reported problems with this fstab. Save anyway?",
                ok_label="Save anyway",
                destructive=True,
                extra_text=output,
                on_result=lambda ok: self._write(content) if ok else None,
            )
        else:
            self._write(content)

    def _write(self, content):
        backup_path = None
        try:
            if self._pending_credentials:
                # Credential files must be root-owned and 0600, which an
                # unprivileged write can't produce -- go straight to pkexec.
                raise PermissionError("credential files require root")
            if os.path.exists(self.path):
                backup_path = backup_fstab(self.path)
            with open(self.path, "w") as f:
                f.write(content)
        except PermissionError:
            # pkexec blocks until the user finishes the password prompt, so
            # this runs off the main thread to keep the window responsive
            # while it's waiting -- see _run_in_thread.
            self.save_btn.set_sensitive(False)
            toast = Adw.Toast.new("Waiting for authentication…")
            toast.set_timeout(0)
            self._auth_toast = toast
            self.toast_overlay.add_toast(toast)
            _run_in_thread(
                lambda: write_with_pkexec(self.path, content, self._pending_credentials),
                self._on_privileged_write_done,
            )
            return

        self._finish_save(backup_path)

    def _on_privileged_write_done(self, result):
        self.save_btn.set_sensitive(True)
        if self._auth_toast is not None:
            self._auth_toast.dismiss()
            self._auth_toast = None

        if result.cancelled:
            return
        if not result.ok:
            self._info(
                "Permission denied",
                f"Could not write to {self.path}.",
                extra_text=result.error or "Try re-running this app with sudo instead.",
            )
            return
        self._finish_save(result.backup_path)

    def _finish_save(self, backup_path):
        self._pending_credentials = []
        for r in self._entries():
            r.existing = True
        self.dirty = False
        self._refresh_list()

        toast_text = f"Saved {self.path}"
        if backup_path:
            toast_text += f" (backup: {os.path.basename(backup_path)})"
        toast = Adw.Toast.new(toast_text)
        toast.set_timeout(5)
        self.toast_overlay.add_toast(toast)

    def _on_reload(self, action, param):
        def do_reload():
            self.records = parse_fstab(self.path) if os.path.exists(self.path) else []
            self.dirty = False
            self._refresh_list()
            self._offer_to_mount_pending()

        if self.dirty:
            self._confirm(
                heading="Discard unsaved changes?",
                body="Reloading will replace your in-memory changes with the file on disk.",
                ok_label="Discard",
                destructive=True,
                on_result=lambda ok: do_reload() if ok else None,
            )
        else:
            do_reload()

    def _offer_to_mount_pending(self):
        """After reloading, mount anything the file says should be mounted
        but isn't -- the natural "did my save actually work?" follow-up.

        Only offered, never automatic: mounting is a privileged change to
        the running system, so it asks first rather than surprising anyone
        with a password prompt they didn't expect from a refresh.
        """
        pending = pending_mounts(self._entries())
        if not pending:
            self.toast_overlay.add_toast(
                Adw.Toast.new("Reloaded — everything in this file is already mounted")
            )
            return

        missing_dirs = [e.mountpoint for e in pending if not os.path.isdir(e.mountpoint)]
        lines = [f"{e.mountpoint}  ({e.fstype})" for e in pending]
        if missing_dirs:
            noun = "folder" if len(missing_dirs) == 1 else "folders"
            lines.append("")
            lines.append(f"The mount point {noun} below will be created first:")
            lines += [f"    {d}" for d in missing_dirs]

        count = len(pending)
        self._confirm(
            heading=f"Mount {count} {'entry' if count == 1 else 'entries'} now?",
            body="These are in the file but aren't mounted. Mounting uses the options "
                 "saved in the file, so nothing outside it is touched.",
            ok_label="Mount",
            extra_text="\n".join(lines),
            on_result=lambda ok: self._start_mount([e.mountpoint for e in pending]) if ok else None,
        )

    def _start_mount(self, mountpoints):
        # pkexec blocks on the password prompt, and mounting itself can be
        # slow, so this runs off the main loop -- same as save and unlock.
        self.reload_btn.set_sensitive(False)
        toast = Adw.Toast.new("Waiting for authentication…")
        toast.set_timeout(0)
        self._auth_toast = toast
        self.toast_overlay.add_toast(toast)
        _run_in_thread(lambda: mount_with_pkexec(mountpoints), self._on_mount_done)

    def _on_mount_done(self, result):
        self.reload_btn.set_sensitive(True)
        if self._auth_toast is not None:
            self._auth_toast.dismiss()
            self._auth_toast = None

        # Mount state changed, so re-render: rows pick up their new live
        # mount points, and the picker's "already used" logic follows.
        self._refresh_list()

        if result.cancelled:
            return

        if result.error:
            self._info("Couldn't mount", "The mount step didn't run.", extra_text=result.error)
            return

        if result.failed:
            detail = [f"{target}: {message}" for target, message in result.failed]
            if result.mounted:
                detail = [f"Mounted: {', '.join(result.mounted)}", ""] + detail
            self._info(
                "Some entries didn't mount",
                f"{len(result.mounted)} mounted, {len(result.failed)} failed.",
                extra_text="\n".join(detail),
            )
            return

        summary = f"Mounted {len(result.mounted)} " + ("entry" if len(result.mounted) == 1 else "entries")
        if result.created:
            noun = "folder" if len(result.created) == 1 else "folders"
            summary += f" (created {len(result.created)} mount point {noun})"
        self.toast_overlay.add_toast(Adw.Toast.new(summary))

    def _on_open(self, action, param):
        dialog = Gtk.FileDialog()
        dialog.set_title("Open fstab file")

        def _on_chosen(source, result):
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path()
            if not path:
                return
            self.path = path
            self.records = parse_fstab(path) if os.path.exists(path) else []
            self.dirty = False
            self._refresh_list()
            self.title_widget.set_subtitle(path)

        dialog.open(self, None, _on_chosen)

    def _on_about(self, action, param):
        AboutDialog().present(self)

    def _on_close_request(self, window):
        if self.dirty and not self._force_close:
            self._confirm(
                heading="Unsaved changes",
                body="You have unsaved changes. Quit anyway?",
                ok_label="Quit",
                destructive=True,
                on_result=self._on_quit_confirmed,
            )
            return True
        return False

    def _on_quit_confirmed(self, ok):
        if ok:
            self._force_close = True
            self.close()


class AutoFstabApp(Adw.Application):
    def __init__(self, path: str):
        # NON_UNIQUE: without this, launching the app a second time (e.g. with
        # a different --file) just re-presents whatever instance is already
        # running instead of opening the file you asked for.
        super().__init__(
            application_id="io.github.autofstab.SetTheTable",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.path = path
        self.window = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        if not self.window:
            self.window = AutoFstabWindow(self, self.path)
        self.window.present()


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive GUI fstab editor with backup, validation, and dry-run checks.")
    parser.add_argument("-f", "--file", default="/etc/fstab", help="Path to the fstab file to edit (default: /etc/fstab)")
    args = parser.parse_args()

    app = AutoFstabApp(args.file)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
