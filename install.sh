#!/usr/bin/env bash
# Installs the GTK4 / Wayland edition of Dynamic Island as a background service for your user.
# Usage:  bash install.sh        (run it from the folder that contains island.py)
set -e

APP="$HOME/.local/share/dynamic-island"
UNIT_DIR="$HOME/.config/systemd/user"
HERE="$(cd "$(dirname "$0")" && pwd)"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
HAVE_SYSTEMD=0
command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1 && HAVE_SYSTEMD=1

if [ "${XDG_SESSION_TYPE:-wayland}" != "wayland" ]; then
    echo "NOTE: you are not in a Wayland session (XDG_SESSION_TYPE=$XDG_SESSION_TYPE)."
    echo "      This edition needs Wayland to sit at the top of the screen. Log in with the"
    echo "      'Plasma (Wayland)' session, or keep using the PySide6 edition on X11."
fi

echo "==> Stopping the old island (if running)"
[ "$HAVE_SYSTEMD" = 1 ] && systemctl --user stop dynamic-island 2>/dev/null || true

echo "==> Installing system packages"
$SUDO apt update || true
$SUDO apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-gdkpixbuf-2.0 \
    gir1.2-pango-1.0 playerctl pulseaudio-utils network-manager iproute2 wl-clipboard \
    dbus-bin librsvg2-common adwaita-icon-theme || echo "   (some packages failed - continuing)"

has_ls() {
    GI_TYPELIB_PATH="${1:-}" python3 -c "import gi; gi.require_version('Gtk4LayerShell','1.0')" 2>/dev/null
}

GLS_TYPELIB=""
if ! has_ls ""; then
    echo "==> Installing gtk4-layer-shell from apt"
    $SUDO apt install -y gir1.2-gtk4layershell-1.0 libgtk4-layer-shell0 2>/dev/null || true
fi
if ! has_ls ""; then
    echo "==> gtk4-layer-shell is not packaged here: building it from source (about a minute)"
    $SUDO apt install -y git meson ninja-build gcc pkg-config libgtk-4-dev libwayland-dev \
        wayland-protocols gobject-introspection || true
    $SUDO apt install -y libgirepository1.0-dev 2>/dev/null || $SUDO apt install -y libgirepository-2.0-dev || true
    TMP="$(mktemp -d)"
    git clone --depth 1 --branch v1.0.4 https://github.com/wmww/gtk4-layer-shell "$TMP/gls"
    (cd "$TMP/gls" \
        && meson setup build -Dexamples=false -Ddocs=false -Dtests=false -Dvapi=false \
               --prefix="$APP/gls" --libdir=lib \
        && ninja -C build install)
    rm -rf "$TMP"
    GLS_TYPELIB="$APP/gls/lib/girepository-1.0"
fi
if has_ls "$GLS_TYPELIB"; then
    echo "   gtk4-layer-shell: OK"
else
    echo "   WARNING: gtk4-layer-shell is still missing. The island will run as a normal window"
    echo "            (not at the top of the screen). See README.txt > Troubleshooting."
fi

echo "==> Installing the program"
mkdir -p "$APP" "$HOME/.local/bin"
cp "$HERE/island.py" "$APP/island.py"
cat > "$HOME/.local/bin/island" << EOF
#!/usr/bin/env bash
# talks to the running island:  island timer 5m | island toast "hi" | island quit ...
exec $(command -v python3) "$APP/island.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/island"

echo "==> Adding 'Add to Island Shelf' to the Dolphin right-click menu"
SM="$HOME/.local/share/kio/servicemenus"
mkdir -p "$SM"
cat > "$SM/island-shelf.desktop" << EOF
[Desktop Entry]
Type=Service
MimeType=all/all;all/allfiles;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
Actions=addToIslandShelf;
X-KDE-Priority=TopLevel

[Desktop Action addToIslandShelf]
Name=Add to Island Shelf
Icon=bookmark-new
Exec=$HOME/.local/bin/island shelf add %F
EOF
chmod +x "$SM/island-shelf.desktop"

echo "==> Creating the systemd user service"
mkdir -p "$UNIT_DIR"
EXTRA_ENV=""
if [ -n "$GLS_TYPELIB" ]; then
    EXTRA_ENV="Environment=GI_TYPELIB_PATH=$GLS_TYPELIB
Environment=LD_LIBRARY_PATH=$APP/gls/lib
Environment=ISLAND_LS_LIB=$APP/gls/lib/libgtk4-layer-shell.so"
fi
cat > "$UNIT_DIR/dynamic-island.service" << EOF
[Unit]
Description=Dynamic Island (GTK4 / Wayland)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Environment=GDK_BACKEND=wayland
Environment=GTK_A11Y=none
$EXTRA_ENV
ExecStart=$(command -v python3) $APP/island.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

if [ "$HAVE_SYSTEMD" = 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now dynamic-island.service
else
    echo "   (systemd user session not available here: service file written, not started)"
fi

echo
echo "Done. The island should appear at the top of your screen."
echo "  status : systemctl --user status dynamic-island"
echo "  logs   : journalctl --user -u dynamic-island -f"
echo "  stop   : systemctl --user stop dynamic-island"
echo "  remove : systemctl --user disable --now dynamic-island"
echo "  control: island timer 5m | toast \"Hello\" | page shelf | pomodoro | ctf NAME WHEN | quit"
echo "  config : ~/.config/dynamic-island/config.json   (run: island reload)"
echo "  (if 'island' is not found, log out and in once so ~/.local/bin is in your PATH)"
echo "  The old PySide6 version is replaced; you can delete $APP/venv to free space."
