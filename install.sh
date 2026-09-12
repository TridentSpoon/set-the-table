#!/usr/bin/env bash
# Installs "Set the Table" as a normal double-click-able desktop app.
#
# Run this once:
#   ./install.sh
#
# Afterwards, find "Set the Table" in your application menu/launcher.
# Re-running this script safely reinstalls over a previous copy.

set -euo pipefail

APP_NAME="Set the Table"
APP_ID="io.github.autofstab.SetTheTable"

# Set by what gui.py actually uses: Adw.Dialog/AlertDialog/AboutDialog are
# libadwaita 1.5, Gtk.FileDialog/UriLauncher are GTK 4.10.
GTK_MIN="4.10"
ADW_MIN="1.5"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/set-the-table"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "Checking dependencies..."

if ! command -v python3 >/dev/null; then
    echo "python3 is not installed -- install it first, then re-run this script." >&2
    exit 1
fi

# Prints "<gtk> <adw>" (e.g. "4.14 1.5"), or nothing if the bindings are
# missing. Both numbers are needed: gi.require_version('Adw', '1') names the
# ABI series, not the release, so it succeeds just as happily on libadwaita
# 1.2 -- which would install cleanly here and then fail at runtime with a
# bare AttributeError.
GTK_ADW_VERSIONS="$(python3 -c "
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
print(f'{Gtk.get_major_version()}.{Gtk.get_minor_version()} {Adw.get_major_version()}.{Adw.get_minor_version()}')
" 2>/dev/null || true)"

if [ -z "$GTK_ADW_VERSIONS" ]; then
    echo "Missing GTK4/libadwaita Python bindings (PyGObject). Install them first:" >&2
    echo "  Arch/CachyOS:   sudo pacman -S python-gobject gtk4 libadwaita" >&2
    echo "  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
    echo "  Fedora:         sudo dnf install python3-gobject gtk4 libadwaita" >&2
    exit 1
fi

GTK_HAVE="${GTK_ADW_VERSIONS% *}"
ADW_HAVE="${GTK_ADW_VERSIONS#* }"

# True when $1 is at least $2. sort -V sorts 4.9 before 4.10; string and
# float comparisons both get that backwards.
version_ge() {
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]
}

if ! version_ge "$GTK_HAVE" "$GTK_MIN" || ! version_ge "$ADW_HAVE" "$ADW_MIN"; then
    echo "GTK/libadwaita are too old for $APP_NAME:" >&2
    echo "  GTK         need $GTK_MIN+, have $GTK_HAVE" >&2
    echo "  libadwaita  need $ADW_MIN+, have $ADW_HAVE" >&2
    echo "" >&2
    echo "These ship with your desktop rather than as add-on packages, so" >&2
    echo "installing more won't raise them. libadwaita $ADW_MIN is GNOME 46" >&2
    echo "(spring 2024) -- this needs a distro release from then or later:" >&2
    echo "Ubuntu 24.04 LTS, Debian 13, Fedora 40, or current Arch all qualify." >&2
    exit 1
fi

if ! command -v lsblk >/dev/null || ! command -v findmnt >/dev/null; then
    echo "Warning: lsblk/findmnt (util-linux) not found -- device picking and the" >&2
    echo "save dry-run won't work. util-linux ships with virtually every Linux" >&2
    echo "install already, so this would be unusual." >&2
fi

echo "Installing $APP_NAME to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r "$SRC_DIR/autofstab" "$INSTALL_DIR/"
cp "$SRC_DIR/autofstab_gui.py" "$SRC_DIR/autofstab.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/autofstab_gui.py" "$INSTALL_DIR/autofstab.py"

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/set-the-table" <<LAUNCHER
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/autofstab_gui.py" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/set-the-table"

mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_NAME
GenericName=Auto-Mount FSTAB Assistant
Comment=Auto-Mount FSTAB Assistant -- add drives to /etc/fstab without the terminal
Exec=$BIN_DIR/set-the-table
Icon=drive-multidisk
Terminal=false
Categories=System;Utility;
StartupWMClass=$APP_ID
DESKTOP

if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo ""
echo "Done. '$APP_NAME' should now show up in your application menu."
echo "If it doesn't appear right away, log out and back in, or try:"
echo "  gtk-launch $APP_ID"
echo ""
echo "It launches with no special privileges -- it only asks for your"
echo "password (via the desktop's own graphical prompt) at the moment you"
echo "actually click Save."
