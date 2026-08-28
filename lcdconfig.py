#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Configuration for the SpacePilot Pro LCD daemon and settings app.

The config lives in ~/.config/spacepilot-lcd/config.json. The daemon watches
the file's mtime and hot-reloads it, so the settings app only needs to write
the file — no daemon restart required.
"""

import copy
import glob
import json
import os

VERSION = "1.0"
REPO_URL = "https://github.com/MiguelDLM/3dxdisp-pro"

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "spacepilot-lcd")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# Runtime state written by the daemon (e.g. the profile activated from the
# bezel keys), kept apart so it never clashes with the settings app.
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

# Per-applet option defaults. The settings app builds its forms from these,
# so adding a key here makes it configurable everywhere.
APPLET_DEFAULTS = {
    "mappings": {
        "title": "Button mappings",
    },
    "clock": {
        "title": "Clock",
        "style": "digital",          # digital | analog
        "use_24h": True,
        "show_seconds": False,
        "show_date": True,
        "font": "DejaVuSans-Bold",
        "size": 72,
        "color": "#00ffff",
        "background": "#0a0a1e",
        "timezone": "",              # empty = local time
        "label": "",
        "second_timezone": "",       # non-empty = dual clock
        "second_label": "",
    },
    "calendar": {
        "title": "Calendar",
        "week_starts_monday": True,
        "color": "#ffffff",
        "highlight": "#00a0ff",
    },
    "system": {
        "title": "System monitor",
        "show_cpu": True,
        "show_ram": True,
        "show_gpu": True,
        "show_vram": True,
        "show_net": False,
        "refresh_seconds": 2,
    },
    "input": {
        "title": "6DOF input test",
        "axis_range": 350,
    },
    "profiles": {
        "title": "Profiles",
    },
    "active_profile": {
        "title": "Active profile",
    },
    # AI subscription quotas. Both providers report their real 5h/weekly
    # utilisation: Claude through the endpoint its /usage command calls,
    # Antigravity through `agy --print /usage --output-format json`.
    "ai_usage": {
        "title": "AI usage",
        "show_claude": True,
        "claude_source": "auto",     # auto | api | local
        "claude_block_tokens": 0,    # 5h budget for the local fallback
        "show_antigravity": True,
        "antigravity_command": "agy",
        "refresh_seconds": 180,      # the Claude endpoint rate-limits hard
        "background": "#0a0a1e",
    },
}

DEFAULT_CONFIG = {
    "brightness": 90,
    "osd_seconds": 1.8,
    "notifications": {"enabled": True, "seconds": 6},
    # Screen saver: after `minutes` without using the device (bezel keys,
    # SpaceMouse buttons or motion), apply `behavior`:
    #   "keep"  - stay on the current page (no change)
    #   "page"  - switch to the page at `page_index` (e.g. a clock)
    #   "image" - show the picture at `image` full-screen
    # Any interaction restores the previous page.
    "screensaver": {"enabled": False, "minutes": 10, "behavior": "page",
                    "page_index": 0, "image": ""},
    "pages": [
        {"type": "mappings"},
        {"type": "clock"},
        {"type": "system"},
    ],
    # Button profiles: when a profile other than "default" is active, its
    # bindings inject keyboard shortcuts (virtual keyboard) on SpaceMouse
    # button presses. "default" always exists and means: no injection.
    # profiles: [{"name": ..., "bindings": {"12": {"label": ..., "keys":
    # "ctrl+z"}, ...}}]
    "profiles": [],
}

DEFAULT_PROFILE = "default"


def profile_names(cfg):
    """All selectable profile names; 'default' is always first."""
    return [DEFAULT_PROFILE] + [p["name"] for p in cfg.get("profiles", [])]


def get_profile(cfg, name):
    for p in cfg.get("profiles", []):
        if p["name"] == name:
            return p
    return None


def load_state():
    try:
        with open(STATE_FILE) as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fp:
        json.dump(state, fp)
    os.replace(tmp, STATE_FILE)


def applet_with_defaults(page):
    """Return a page dict with every option filled in from the defaults."""
    merged = copy.deepcopy(APPLET_DEFAULTS.get(page.get("type"), {}))
    merged.update(page)
    return merged


def load():
    try:
        with open(CONFIG_FILE) as fp:
            cfg = json.load(fp)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged.update(cfg)
    merged["pages"] = [applet_with_defaults(p) for p in merged.get("pages", [])
                       if p.get("type") in APPLET_DEFAULTS]
    return merged


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as fp:
        json.dump(cfg, fp, indent=2)
    os.replace(tmp, CONFIG_FILE)


def mtime():
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return None


def available_fonts():
    """Map of font name -> .ttf path for the fonts we can rely on."""
    fonts = {}
    for pattern in ("/usr/share/fonts/truetype/dejavu/*.ttf",
                    "/usr/share/fonts/TTF/DejaVu*.ttf",
                    "/usr/share/fonts/truetype/liberation/*.ttf",
                    "/usr/share/fonts/liberation/*.ttf"):
        for path in glob.glob(pattern):
            name = os.path.splitext(os.path.basename(path))[0]
            if "Oblique" in name or "Italic" in name or "Math" in name:
                continue
            fonts[name] = path
    return dict(sorted(fonts.items()))


def font_path(name):
    return available_fonts().get(name) or available_fonts().get(
        "DejaVuSans-Bold")
