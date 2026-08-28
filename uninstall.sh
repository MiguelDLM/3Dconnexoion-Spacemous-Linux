#!/usr/bin/env bash
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
#
# Undoes install.sh.
#
#   ./uninstall.sh            remove the service, menu entry, icon, binaries
#   ./uninstall.sh --purge    also delete ~/.config/spacepilot-lcd (your
#                             pages, button profiles and settings)
#
# The venv and this directory are left alone: deleting the checkout is up to
# you. The udev rule is removed only if you confirm the sudo prompt.

set -euo pipefail

APP_ID="3dxdisp-pro"
SERVICE="spacepilot-lcd.service"
UDEV_RULE="99-spacepilot-pro-lcd.rules"

BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/spacepilot-lcd"

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge)   PURGE=1 ;;
        -h|--help) sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                   exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }

if systemctl --user list-unit-files "$SERVICE" >/dev/null 2>&1; then
    say "Stopping the daemon"
    systemctl --user disable --now "$SERVICE" >/dev/null 2>&1 || true
fi
rm -f "$UNIT_DIR/$SERVICE"
systemctl --user daemon-reload 2>/dev/null || true

say "Removing the menu entry, icon and binaries"
rm -f "$DESKTOP_DIR/$APP_ID.desktop" \
      "$DESKTOP_DIR/spacepilot-lcd-settings.desktop" \
      "$ICON_DIR/$APP_ID.svg" \
      "$BIN_DIR/spacepilot-lcd-settings" \
      "$BIN_DIR/spacepilot-lcd-daemon"

command -v update-desktop-database >/dev/null \
    && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null \
    && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" \
       >/dev/null 2>&1 || true
for kbuild in kbuildsycoca6 kbuildsycoca5; do
    command -v "$kbuild" >/dev/null && "$kbuild" >/dev/null 2>&1 && break
done || true

if [ -f "/etc/udev/rules.d/$UDEV_RULE" ]; then
    say "Removing the udev rule (needs sudo; Ctrl-C to keep it)"
    sudo rm -f "/etc/udev/rules.d/$UDEV_RULE" \
        && sudo udevadm control --reload \
        || warn "left /etc/udev/rules.d/$UDEV_RULE in place"
fi

if [ "$PURGE" -eq 1 ]; then
    say "Deleting $CONFIG_DIR"
    rm -rf "$CONFIG_DIR"
else
    say "Kept your settings in $CONFIG_DIR (use --purge to delete them)"
fi

say "Done."
