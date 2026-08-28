#!/usr/bin/env bash
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
#
# One-command installer for 3dxdisp-pro. Works both from a git checkout and
# from an unpacked release tarball; in the tarball it prefers the bundled
# standalone binaries, so nothing but this script is needed.
#
#   ./install.sh              install everything
#   ./install.sh --no-udev    skip the one step that needs sudo
#   ./install.sh --no-service do not enable the background daemon
#   ./install.sh --source     ignore bundled binaries, use a Python venv
#
# Everything lands under $HOME except the udev rule, which has to be in
# /etc/udev/rules.d to give your user access to the LCD interface.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="3dxdisp-pro"
SERVICE="spacepilot-lcd.service"
UDEV_RULE="99-spacepilot-pro-lcd.rules"

BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

WANT_UDEV=1
WANT_SERVICE=1
FORCE_SOURCE=0
for arg in "$@"; do
    case "$arg" in
        --no-udev)    WANT_UDEV=0 ;;
        --no-service) WANT_SERVICE=0 ;;
        --source)     FORCE_SOURCE=1 ;;
        -h|--help)    sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                      exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Decide how to run the two programs
# --------------------------------------------------------------------------
BUNDLED_GUI="$SRC/spacepilot-lcd-settings"
BUNDLED_DAEMON="$SRC/spacepilot-lcd-daemon"

if [ "$FORCE_SOURCE" -eq 0 ] && [ -x "$BUNDLED_GUI" ] \
   && [ -x "$BUNDLED_DAEMON" ]; then
    MODE=binary
else
    MODE=source
fi

if [ "$MODE" = binary ]; then
    say "Installing the bundled standalone binaries (no Python needed)"
    mkdir -p "$BIN_DIR"
    install -m 755 "$BUNDLED_GUI" "$BIN_DIR/spacepilot-lcd-settings"
    install -m 755 "$BUNDLED_DAEMON" "$BIN_DIR/spacepilot-lcd-daemon"
    GUI_EXEC="$BIN_DIR/spacepilot-lcd-settings"
    DAEMON_EXEC="$BIN_DIR/spacepilot-lcd-daemon"
    WORKDIR="$HOME"
else
    command -v python3 >/dev/null || die "python3 is not installed."
    python3 - <<'PY' || die "Python 3.8 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
    if [ ! -x "$SRC/venv/bin/python" ]; then
        say "Creating the virtual environment in $SRC/venv"
        python3 -m venv "$SRC/venv" \
            || die "Could not create a venv. On Debian/Ubuntu install python3-venv."
    fi
    say "Installing Python dependencies"
    # -m pip, never venv/bin/pip: a moved checkout leaves that script with a
    # stale shebang, while the interpreter itself keeps working.
    "$SRC/venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$SRC/venv/bin/python" -m pip install --quiet -r "$SRC/requirements.txt" \
        || die "Dependency installation failed."
    GUI_EXEC="$SRC/venv/bin/python $SRC/lcd_settings.py"
    DAEMON_EXEC="$SRC/venv/bin/python $SRC/spnav_lcd_daemon.py"
    WORKDIR="$SRC"
fi

# --------------------------------------------------------------------------
# USB access (the only step that needs root)
# --------------------------------------------------------------------------
# Compare the rules that actually matter, not the comments: a reworded
# header must not cost the user a pointless sudo prompt on every re-run.
effective_rules() { grep -vE '^[[:space:]]*(#|$)' "$1" 2>/dev/null || true; }

if [ "$WANT_UDEV" -eq 1 ]; then
    if [ -f "/etc/udev/rules.d/$UDEV_RULE" ] \
       && [ "$(effective_rules "$SRC/$UDEV_RULE")" \
            = "$(effective_rules "/etc/udev/rules.d/$UDEV_RULE")" ]; then
        say "udev rule already installed"
    elif [ -f "$SRC/$UDEV_RULE" ]; then
        say "Installing the udev rule (needs sudo, for LCD access as your user)"
        if sudo install -m 644 "$SRC/$UDEV_RULE" "/etc/udev/rules.d/$UDEV_RULE"
        then
            sudo udevadm control --reload || true
            sudo udevadm trigger --action=change \
                --attr-match=idVendor=046d --attr-match=idProduct=c629 || true
        else
            warn "Could not install the udev rule; the LCD will need root"
            warn "until you run: sudo install -m 644 $SRC/$UDEV_RULE /etc/udev/rules.d/"
        fi
    fi
    if ! id -nG | tr ' ' '\n' | grep -qx plugdev; then
        warn "Your user is not in the 'plugdev' group, which the rule grants"
        warn "access through. Fix with: sudo usermod -aG plugdev $USER"
        warn "(then log out and back in)."
    fi
fi

# --------------------------------------------------------------------------
# Application icon and menu entry
# --------------------------------------------------------------------------
say "Installing the menu entry and icon"
mkdir -p "$ICON_DIR" "$DESKTOP_DIR"
install -m 644 "$SRC/icons/$APP_ID.svg" "$ICON_DIR/$APP_ID.svg"

# Categories matter: an entry with only Settings;HardwareSettings; is filed
# under System in Plasma's menu, where nobody goes looking for it. Utility
# also puts it in the ordinary Utilities menu, and Keywords make it findable
# by searching for the hardware rather than the project name.
cat > "$DESKTOP_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=3dxdisp-pro Settings
GenericName=SpacePilot Pro LCD
Comment=Configure the 3Dconnexion SpacePilot Pro LCD pages, applets and button profiles
Exec=$GUI_EXEC
Icon=$APP_ID
Terminal=false
Categories=Utility;Settings;HardwareSettings;
Keywords=SpacePilot;SpaceMouse;3Dconnexion;3dxdisp;LCD;spacenavd;3D mouse;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
chmod 644 "$DESKTOP_DIR/$APP_ID.desktop"

# The pre-1.1 entry had the old name and the burying categories.
rm -f "$DESKTOP_DIR/spacepilot-lcd-settings.desktop"

if command -v desktop-file-validate >/dev/null; then
    # Hints are advisory (one is expected: two main categories, on purpose
    # so the entry shows up in Utilities as well as System). Only surface
    # real errors.
    desktop-file-validate "$DESKTOP_DIR/$APP_ID.desktop" 2>&1 \
        | grep -v ': hint:' >&2 || true
fi
command -v update-desktop-database >/dev/null \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null \
    && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" \
       >/dev/null 2>&1 || true
# Plasma only notices new entries once its service cache is rebuilt.
for kbuild in kbuildsycoca6 kbuildsycoca5; do
    command -v "$kbuild" >/dev/null && "$kbuild" >/dev/null 2>&1 && break
done || true

# --------------------------------------------------------------------------
# Background daemon
# --------------------------------------------------------------------------
if [ "$WANT_SERVICE" -eq 1 ]; then
    say "Installing the daemon as a systemd user service"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$SERVICE" <<EOF
[Unit]
Description=3Dconnexion SpacePilot Pro LCD status daemon (3dxdisp-pro)

[Service]
Type=simple
WorkingDirectory=$WORKDIR
ExecStart=$DAEMON_EXEC
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE" >/dev/null
    # `enable --now` will not restart a unit that is already running, so an
    # upgrade would keep serving the old code until the next reboot.
    systemctl --user restart "$SERVICE"
    sleep 2
    if [ "$(systemctl --user is-active "$SERVICE")" = active ]; then
        say "Daemon running."
    else
        warn "The daemon did not stay up. Look at:"
        warn "  systemctl --user status $SERVICE"
    fi
fi

# --------------------------------------------------------------------------
say "Done."
echo
echo "  Settings app : look for \"3dxdisp-pro Settings\" in your application"
echo "                 menu, or run: $GUI_EXEC"
echo "  Daemon       : systemctl --user status $SERVICE"
echo "  Uninstall    : $SRC/uninstall.sh"
if [ "$MODE" = source ]; then
    echo
    echo "  Installed from source in $SRC — keep this directory where it is,"
    echo "  the service and menu entry point at it."
fi
if ! lsusb 2>/dev/null | grep -q 046d:c629; then
    echo
    warn "No SpacePilot Pro (046d:c629) is plugged in right now."
fi
