#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Settings application for the SpacePilot Pro LCD daemon (Qt / PySide6).

Configure which pages the LCD shows and in which order, every visual option
(clock styles, fonts, colors, time zones, dual clocks, calendar, system
monitor rows, 6DOF test), plus brightness, OSD and notification mirroring.
The live preview on the right is rendered with the daemon's own applet code,
so it is pixel-exact. Saving writes ~/.config/spacepilot-lcd/config.json and
the running daemon applies it immediately — no restart needed.
"""

import copy
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget)

import applets
import lcdconfig
import aiusage
from keyinjector import validate_combo
from spnav_client import State
from spplcd import WIDTH, HEIGHT
from sysstats import SystemStats

try:
    from zoneinfo import available_timezones
    TIMEZONES = [""] + sorted(available_timezones())
except Exception:
    TIMEZONES = ["", "UTC", "Europe/Madrid", "America/Mexico_City",
                 "America/New_York", "Asia/Tokyo"]

APPLET_LABELS = {
    "mappings": "Button mappings",
    "clock": "Clock",
    "calendar": "Calendar",
    "system": "System monitor",
    "input": "6DOF input test",
    "profiles": "Profiles (selector)",
    "active_profile": "Active profile (pad view)",
    "ai_usage": "AI usage (Claude / Antigravity)",
}

FIELD_LABELS = {
    "title": "Title",
    "style": "Style",
    "use_24h": "24-hour format",
    "show_seconds": "Show seconds",
    "show_date": "Show date",
    "font": "Font",
    "size": "Digit size",
    "color": "Color",
    "background": "Background",
    "highlight": "Highlight color",
    "timezone": "Time zone (empty = local)",
    "label": "Label",
    "second_timezone": "2nd time zone (dual clock)",
    "second_label": "2nd label",
    "week_starts_monday": "Week starts on Monday",
    "show_cpu": "Show CPU",
    "show_ram": "Show RAM",
    "show_gpu": "Show GPU",
    "show_vram": "Show VRAM",
    "show_net": "Show network rate",
    "refresh_seconds": "Refresh (seconds)",
    "axis_range": "Axis range (deflection)",
    "show_claude": "Show Claude",
    "claude_source": "Claude data source",
    "claude_block_tokens": "Claude 5h token budget (0 = raw count)",
    "show_antigravity": "Show Antigravity",
    "antigravity_command": "Antigravity CLI command",
}

# Option keys that are a fixed choice rather than free text.
ENUM_FIELDS = {
    "style": ["digital", "analog"],
    "claude_source": ["auto", "api", "local"],
}

# Numeric option keys whose sensible range is not the 1-1000 default.
SPIN_RANGES = {
    "refresh_seconds": (1, 3600),
    "claude_block_tokens": (0, 2000000000),
}


class ColorButton(QPushButton):
    def __init__(self, value, on_change):
        super().__init__()
        self._on_change = on_change
        self.set_value(value)
        self.clicked.connect(self._pick)

    def set_value(self, value):
        self._value = value
        self.setStyleSheet(
            f"background-color: {value}; border: 1px solid #888;"
            " min-height: 22px;")
        self.setText(value)

    def _pick(self):
        color = QColorDialog.getColor(QColor(self._value), self)
        if color.isValid():
            self.set_value(color.name())
            self._on_change(color.name())


def demo_spnav_state():
    state = State()
    state.connected = True
    state.axes = [120, -80, 230, -160, 60, -30]
    state.buttons = {1, 5, 29}
    return state


class SettingsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"3dxdisp-pro Settings  v{lcdconfig.VERSION}")
        self.cfg = lcdconfig.load()
        self.stats = SystemStats()
        self.stats.cpu_percent()  # prime the delta
        self.demo_state = demo_spnav_state()
        # Started on demand by the preview of an AI-usage page.
        self.ai = None

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        root = QWidget()
        tabs.addTab(root, "Pages")
        tabs.addTab(self._build_profiles_tab(), "Profiles")
        tabs.addTab(self._build_about_tab(), "About")
        layout = QHBoxLayout(root)

        # ----- left column: page list + ordering ---------------------------
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Pages (display order)</b>"))
        self.page_list = QListWidget()
        self.page_list.currentRowChanged.connect(self._page_selected)
        left.addWidget(self.page_list, 1)

        row = QHBoxLayout()
        add_btn = QPushButton("Add")
        menu = QMenu(add_btn)
        for ptype, label in APPLET_LABELS.items():
            menu.addAction(label, lambda t=ptype: self._add_page(t))
        add_btn.setMenu(menu)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_page)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(lambda: self._move_page(-1))
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(lambda: self._move_page(1))
        for b in (add_btn, remove_btn, up_btn, down_btn):
            row.addWidget(b)
        left.addLayout(row)

        # global settings
        glob = QGroupBox("General")
        gform = QFormLayout(glob)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(5, 100)
        self.brightness.setValue(int(self.cfg["brightness"]))
        self.brightness.valueChanged.connect(
            lambda v: self.cfg.__setitem__("brightness", v))
        gform.addRow("Brightness", self.brightness)
        self.osd_secs = QDoubleSpinBox()
        self.osd_secs.setRange(0.5, 10)
        self.osd_secs.setSingleStep(0.5)
        self.osd_secs.setValue(float(self.cfg["osd_seconds"]))
        self.osd_secs.valueChanged.connect(
            lambda v: self.cfg.__setitem__("osd_seconds", v))
        gform.addRow("OSD duration (s)", self.osd_secs)
        self.notif_enable = QCheckBox("Mirror desktop notifications")
        self.notif_enable.setChecked(self.cfg["notifications"]["enabled"])
        self.notif_enable.toggled.connect(
            lambda v: self.cfg["notifications"].__setitem__("enabled", v))
        gform.addRow(self.notif_enable)
        self.notif_secs = QSpinBox()
        self.notif_secs.setRange(2, 30)
        self.notif_secs.setValue(int(self.cfg["notifications"]["seconds"]))
        self.notif_secs.valueChanged.connect(
            lambda v: self.cfg["notifications"].__setitem__("seconds", v))
        gform.addRow("Notification time (s)", self.notif_secs)
        left.addWidget(glob)

        # screen saver
        saver = QGroupBox("Screen saver")
        sform = QFormLayout(saver)
        sv = self.cfg["screensaver"]
        sv_enable = QCheckBox("Enable after idle time")
        sv_enable.setChecked(bool(sv["enabled"]))
        sv_enable.toggled.connect(lambda v: sv.__setitem__("enabled", v))
        sform.addRow(sv_enable)
        sv_min = QSpinBox()
        sv_min.setRange(1, 120)
        sv_min.setValue(int(sv["minutes"]))
        sv_min.valueChanged.connect(lambda v: sv.__setitem__("minutes", v))
        sform.addRow("Idle minutes", sv_min)
        self.sv_behavior = QComboBox()
        self.sv_behavior.addItem("Keep current page", "keep")
        self.sv_behavior.addItem("Switch to a page", "page")
        self.sv_behavior.addItem("Show an image", "image")
        self.sv_behavior.setCurrentIndex(
            {"keep": 0, "page": 1, "image": 2}.get(sv["behavior"], 1))
        self.sv_behavior.currentIndexChanged.connect(
            lambda i: sv.__setitem__("behavior",
                                     self.sv_behavior.itemData(i)))
        sform.addRow("Behavior", self.sv_behavior)
        self.sv_page = QComboBox()
        self._reload_saver_pages()
        self.sv_page.currentIndexChanged.connect(
            lambda i: sv.__setitem__("page_index", max(0, i)))
        sform.addRow("Saver page", self.sv_page)
        img_row = QHBoxLayout()
        self.sv_image = QLineEdit(sv["image"])
        self.sv_image.textChanged.connect(
            lambda t: sv.__setitem__("image", t))
        browse = QPushButton("...")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._pick_saver_image)
        img_row.addWidget(self.sv_image)
        img_row.addWidget(browse)
        sform.addRow("Image file", img_row)
        left.addWidget(saver)

        layout.addLayout(left, 1)

        # ----- middle column: options for the selected page ----------------
        mid = QVBoxLayout()
        mid.addWidget(QLabel("<b>Page options</b>"))
        self.options_area = QScrollArea()
        self.options_area.setWidgetResizable(True)
        mid.addWidget(self.options_area, 1)
        layout.addLayout(mid, 1)

        # ----- right column: live preview + actions ------------------------
        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Live preview</b>"))
        self.preview = QLabel()
        self.preview.setFixedSize(WIDTH * 2, HEIGHT * 2)
        self.preview.setStyleSheet("border: 2px solid #444;")
        right.addWidget(self.preview)
        right.addStretch(1)
        apply_btn = QPushButton("Apply (daemon reloads instantly)")
        apply_btn.clicked.connect(self._apply)
        right.addWidget(apply_btn)
        defaults_btn = QPushButton("Restore defaults")
        defaults_btn.clicked.connect(self._restore_defaults)
        right.addWidget(defaults_btn)
        layout.addLayout(right, 0)

        self._reload_page_list()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_preview)
        self.timer.start(1000)
        self._update_preview()

    # ----- screen saver helpers ----------------------------------------------

    def _reload_saver_pages(self):
        self.sv_page.blockSignals(True)
        self.sv_page.clear()
        for p in self.cfg["pages"]:
            self.sv_page.addItem(p.get("title") or p["type"])
        idx = int(self.cfg["screensaver"].get("page_index", 0))
        if 0 <= idx < self.sv_page.count():
            self.sv_page.setCurrentIndex(idx)
        self.sv_page.blockSignals(False)

    def _pick_saver_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Screen saver image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.sv_image.setText(path)

    # ----- profiles tab ------------------------------------------------------

    # ----- about tab ---------------------------------------------------------

    def _build_about_tab(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.addStretch(1)
        title = QLabel("<h1>3dxdisp-pro</h1>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        info = QLabel(
            f"<p align='center'>Version <b>v{lcdconfig.VERSION}</b></p>"
            "<p align='center'>LCD, bezel keys and button profiles for the "
            "3Dconnexion SpacePilot Pro on Linux.</p>"
            f"<p align='center'><a href='https://github.com/MiguelDLM'>"
            "github.com/MiguelDLM</a> &nbsp;·&nbsp; "
            f"<a href='{lcdconfig.REPO_URL}'>project repository</a></p>"
            "<p align='center'>License: GPL-3.0</p>")
        info.setOpenExternalLinks(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        row.addStretch(1)
        lic_btn = QPushButton("View license")
        lic_btn.clicked.connect(self._show_license)
        row.addWidget(lic_btn)
        upd_btn = QPushButton("Check for updates")
        upd_btn.clicked.connect(self._check_updates)
        row.addWidget(upd_btn)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(2)
        return host

    def _show_license(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "LICENSE")
        try:
            with open(path) as fp:
                text = fp.read()
        except OSError:
            text = ("LICENSE file not found. This program is distributed "
                    "under the GNU GPL v3: https://www.gnu.org/licenses/"
                    "gpl-3.0.html")
        from PySide6.QtWidgets import QDialog, QPlainTextEdit
        dlg = QDialog(self)
        dlg.setWindowTitle("GNU General Public License v3")
        dlg.resize(620, 480)
        box = QVBoxLayout(dlg)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        box.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        box.addWidget(close)
        dlg.exec()

    def _check_updates(self):
        import json
        from urllib.request import urlopen, Request
        api = (lcdconfig.REPO_URL.replace("github.com",
                                          "api.github.com/repos")
               + "/releases/latest")
        try:
            req = Request(api, headers={"Accept":
                                        "application/vnd.github+json"})
            with urlopen(req, timeout=6) as resp:
                latest = json.load(resp).get("tag_name", "")
        except Exception as err:
            QMessageBox.warning(self, "Check for updates",
                                f"Could not reach GitHub:\n{err}")
            return
        current = f"v{lcdconfig.VERSION}"
        if not latest:
            QMessageBox.information(self, "Check for updates",
                                    "No releases published yet.")
        elif latest == current:
            QMessageBox.information(
                self, "Check for updates",
                f"You are up to date ({current}).")
        else:
            QMessageBox.information(
                self, "Check for updates",
                f"New version available: {latest} (you have {current}).\n"
                f"Download it at:\n{lcdconfig.REPO_URL}/releases/latest")

    def _build_profiles_tab(self):
        host = QWidget()
        layout = QHBoxLayout(host)

        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Button profiles</b>"))
        hint = QLabel(
            "'default' always exists: no key injection (native behavior).\n"
            "Activate profiles from the LCD's Profiles page (Up/Down + OK).")
        hint.setWordWrap(True)
        left.addWidget(hint)
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._profile_selected)
        left.addWidget(self.profile_list, 1)
        row = QHBoxLayout()
        for text, slot in (("New", self._profile_new),
                           ("Duplicate", self._profile_duplicate),
                           ("Rename", self._profile_rename),
                           ("Delete", self._profile_delete)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        left.addLayout(row)
        layout.addLayout(left, 0)

        right = QVBoxLayout()
        right.addWidget(QLabel(
            "<b>Bindings</b> — keys syntax: <code>ctrl+shift+z</code>, "
            "<code>tab</code>, <code>g</code>, <code>f12</code>, "
            "<code>kp_plus</code>... Empty row = button does nothing.<br>"
            "The 5 physical function keys send buttons 12-16; rows 17-21 "
            "(\"6\"-\"10\") are their <b>second function</b>, reached by "
            "toggling the bank with the <b>Menu</b> button (reserved while "
            "a profile is active)."))
        self.bindings = QTableWidget(31, 3)
        self.bindings.setHorizontalHeaderLabels(["Button", "Label", "Keys"])
        self.bindings.verticalHeader().setVisible(False)
        self.bindings.setColumnWidth(0, 150)
        self.bindings.setColumnWidth(1, 200)
        self.bindings.setColumnWidth(2, 170)
        for b in range(31):
            name = applets.BUTTON_NAMES.get(b, str(b))
            if b == 0:
                name = "Menu (bank toggle)"
            elif 12 <= b <= 16:
                name = f"{name}  (function key)"
            elif 17 <= b <= 21:
                name = f"{name}  (= key {b - 16} +Menu)"
            item = QTableWidgetItem(f"{name}  #{b}")
            item.setFlags(Qt.ItemIsEnabled)
            self.bindings.setItem(b, 0, item)
            label_item = QTableWidgetItem("")
            keys_item = QTableWidgetItem("")
            if b == 0:
                # Menu is consumed by the bank toggle while a profile is
                # active, so bindings on it would never fire.
                label_item.setFlags(Qt.ItemIsEnabled)
                keys_item.setFlags(Qt.ItemIsEnabled)
            self.bindings.setItem(b, 1, label_item)
            self.bindings.setItem(b, 2, keys_item)
        self.bindings.cellChanged.connect(self._binding_changed)
        right.addWidget(self.bindings, 1)
        layout.addLayout(right, 1)

        self._reload_profile_list()
        return host

    def _reload_profile_list(self, select=0):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for p in self.cfg["profiles"]:
            self.profile_list.addItem(p["name"])
        self.profile_list.blockSignals(False)
        if self.cfg["profiles"]:
            select = max(0, min(select, len(self.cfg["profiles"]) - 1))
            self.profile_list.setCurrentRow(select)
            self._load_bindings(self.cfg["profiles"][select])
        else:
            self._load_bindings(None)

    def _current_profile(self):
        row = self.profile_list.currentRow()
        if 0 <= row < len(self.cfg["profiles"]):
            return self.cfg["profiles"][row]
        return None

    def _profile_selected(self, _row):
        self._load_bindings(self._current_profile())

    def _load_bindings(self, profile):
        self.bindings.blockSignals(True)
        bindings = (profile or {}).get("bindings", {})
        for b in range(31):
            binding = bindings.get(str(b), {})
            self.bindings.item(b, 1).setText(binding.get("label", ""))
            self.bindings.item(b, 2).setText(binding.get("keys", ""))
            self.bindings.item(b, 2).setForeground(QColor("black"))
        self.bindings.setEnabled(profile is not None)
        self.bindings.blockSignals(False)

    def _binding_changed(self, row, col):
        profile = self._current_profile()
        if profile is None or col == 0:
            return
        label = self.bindings.item(row, 1).text().strip()
        keys = self.bindings.item(row, 2).text().strip()
        if keys and validate_combo(keys):
            self.bindings.item(row, 2).setForeground(QColor("red"))
        else:
            self.bindings.item(row, 2).setForeground(QColor("black"))
        bindings = profile.setdefault("bindings", {})
        if label or keys:
            bindings[str(row)] = {"label": label, "keys": keys}
        else:
            bindings.pop(str(row), None)

    def _ask_name(self, title, default=""):
        name, ok = QInputDialog.getText(self, title, "Profile name:",
                                        text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if name in lcdconfig.profile_names(self.cfg):
            QMessageBox.warning(self, title,
                                f"A profile named '{name}' already exists.")
            return None
        return name

    def _profile_new(self):
        name = self._ask_name("New profile")
        if name:
            self.cfg["profiles"].append({"name": name, "bindings": {}})
            self._reload_profile_list(len(self.cfg["profiles"]) - 1)

    def _profile_duplicate(self):
        profile = self._current_profile()
        if profile is None:
            return
        name = self._ask_name("Duplicate profile", profile["name"] + " copy")
        if name:
            clone = copy.deepcopy(profile)
            clone["name"] = name
            self.cfg["profiles"].append(clone)
            self._reload_profile_list(len(self.cfg["profiles"]) - 1)

    def _profile_rename(self):
        profile = self._current_profile()
        if profile is None:
            return
        name = self._ask_name("Rename profile", profile["name"])
        if name:
            profile["name"] = name
            self._reload_profile_list(self.profile_list.currentRow())

    def _profile_delete(self):
        row = self.profile_list.currentRow()
        if 0 <= row < len(self.cfg["profiles"]):
            name = self.cfg["profiles"][row]["name"]
            if QMessageBox.question(
                    self, "Delete profile",
                    f"Delete profile '{name}'?") == QMessageBox.Yes:
                del self.cfg["profiles"][row]
                self._reload_profile_list(row)

    # ----- page list management --------------------------------------------

    def _reload_page_list(self, select=0):
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for p in self.cfg["pages"]:
            label = APPLET_LABELS.get(p["type"], p["type"])
            title = p.get("title") or ""
            item = QListWidgetItem(
                f"{label}" + (f"  -  {title}" if title != label else ""))
            self.page_list.addItem(item)
        self.page_list.blockSignals(False)
        if hasattr(self, "sv_page"):
            self._reload_saver_pages()
        if self.cfg["pages"]:
            select = max(0, min(select, len(self.cfg["pages"]) - 1))
            self.page_list.setCurrentRow(select)
        else:
            self._build_options(None)

    def _current_page(self):
        row = self.page_list.currentRow()
        if 0 <= row < len(self.cfg["pages"]):
            return self.cfg["pages"][row]
        return None

    def _page_selected(self, _row):
        self._build_options(self._current_page())
        self._update_preview()

    def _add_page(self, ptype):
        self.cfg["pages"].append(
            lcdconfig.applet_with_defaults({"type": ptype}))
        self._reload_page_list(len(self.cfg["pages"]) - 1)

    def _remove_page(self):
        row = self.page_list.currentRow()
        if 0 <= row < len(self.cfg["pages"]):
            del self.cfg["pages"][row]
            self._reload_page_list(row)

    def _move_page(self, delta):
        row = self.page_list.currentRow()
        new = row + delta
        if 0 <= row < len(self.cfg["pages"]) and 0 <= new < len(
                self.cfg["pages"]):
            pages = self.cfg["pages"]
            pages[row], pages[new] = pages[new], pages[row]
            self._reload_page_list(new)

    # ----- option form ------------------------------------------------------

    def _build_options(self, page):
        form_host = QWidget()
        form = QFormLayout(form_host)
        if page is None:
            form.addRow(QLabel("Add a page to configure it."))
        else:
            for key, default in lcdconfig.APPLET_DEFAULTS[
                    page["type"]].items():
                page.setdefault(key, default)
                form.addRow(FIELD_LABELS.get(key, key),
                            self._editor(page, key, page[key]))
        self.options_area.setWidget(form_host)

    def _editor(self, page, key, value):
        def setter(v):
            page[key] = v
            if key == "title":
                self._reload_page_list(self.page_list.currentRow())
            self._update_preview()

        if key in ("color", "background", "highlight"):
            return ColorButton(value, setter)
        if key == "font":
            combo = QComboBox()
            fonts = list(lcdconfig.available_fonts())
            combo.addItems(fonts)
            if value in fonts:
                combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if key in ("timezone", "second_timezone"):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(TIMEZONES)
            combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if key in ENUM_FIELDS:
            combo = QComboBox()
            combo.addItems(ENUM_FIELDS[key])
            combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if isinstance(value, bool):
            box = QCheckBox()
            box.setChecked(value)
            box.toggled.connect(setter)
            return box
        if isinstance(value, int):
            spin = QSpinBox()
            spin.setRange(*SPIN_RANGES.get(key, (1, 1000)))
            spin.setValue(value)
            spin.valueChanged.connect(setter)
            return spin
        edit = QLineEdit(str(value))
        edit.textChanged.connect(setter)
        return edit

    # ----- preview / apply ---------------------------------------------------

    def _update_preview(self):
        page = self._current_page()
        if page is None:
            self.preview.clear()
            return
        if page["type"] == "ai_usage":
            if self.ai is None:
                self.ai = aiusage.AIUsage(page)
            else:
                self.ai.configure(page)
        names = lcdconfig.profile_names(self.cfg)
        first = self.cfg["profiles"][0] if self.cfg["profiles"] else None
        ctx = {"stats": self.stats, "spnav": self.demo_state,
               "ai": self.ai.snapshot() if self.ai else None,
               "profiles_ui": {
                   "names": names,
                   "active": self.cfg["profiles"][0]["name"] if first
                   else "default",
                   "sel": 1 if first else 0,
                   "bindings": (first or {}).get("bindings", {}),
                   "active_bindings": (first or {}).get("bindings", {})}}
        try:
            img = applets.RENDERERS[page["type"]](page, ctx)
        except Exception as err:
            self.preview.setText(f"Preview error:\n{err}")
            return
        data = img.tobytes("raw", "RGB")
        qimg = QImage(data, img.width, img.height, img.width * 3,
                      QImage.Format_RGB888)
        self.preview.setPixmap(QPixmap.fromImage(qimg).scaled(
            WIDTH * 2, HEIGHT * 2, Qt.KeepAspectRatio))

    def closeEvent(self, event):
        if self.ai is not None:
            self.ai.stop()
        super().closeEvent(event)

    def _apply(self):
        lcdconfig.save(self.cfg)
        self.statusBar().showMessage(
            "Saved - the daemon applies it within a second.", 4000)

    def _restore_defaults(self):
        if QMessageBox.question(
                self, "Restore defaults",
                "Discard the current configuration and restore defaults?") \
                == QMessageBox.Yes:
            self.cfg = copy.deepcopy(lcdconfig.DEFAULT_CONFIG)
            self.cfg["pages"] = [lcdconfig.applet_with_defaults(p)
                                 for p in self.cfg["pages"]]
            self._reload_page_list()


def main():
    app = QApplication(sys.argv)
    win = SettingsWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
