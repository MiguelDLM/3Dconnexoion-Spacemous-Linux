#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Applet renderers for the SpacePilot Pro LCD.

Every applet is a pure function taking its page config (a dict filled in by
lcdconfig.applet_with_defaults) plus whatever live context it needs, and
returning a 320x240 PIL image. The settings app reuses these for its live
preview, so what you see there is pixel-exact.
"""

import calendar
import math
import os
import platform
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9
    ZoneInfo = None

from PIL import Image, ImageDraw, ImageFont

from lcdconfig import font_path
from spplcd import WIDTH, HEIGHT

ACTION_LABELS = {
    "sensitivity-up": "Sensitivity +",
    "sensitivity-down": "Sensitivity -",
    "sensitivity-reset": "Sensitivity reset",
    "disable-rotation": "Pan/zoom only",
    "disable-translation": "Rotation only",
    "dominant-axis": "Dominant axis",
    "none": "(none)",
}

# Physical key names of the SpacePilot Pro, by spacenavd button number.
# Source: Blender's GHOST NDOF map for device 046d:c629 (31 buttons).
BUTTON_NAMES = {
    0: "Menu", 1: "Fit", 2: "Top", 3: "Left", 4: "Right", 5: "Front",
    6: "Bottom", 7: "Back", 8: "RollCW", 9: "RollCC", 10: "ISO1", 11: "ISO2",
    **{12 + n: str(n + 1) for n in range(10)},
    22: "Esc", 23: "Alt", 24: "Shift", 25: "Ctrl", 26: "Rot", 27: "Pan",
    28: "Dom", 29: "+", 30: "-",
}

CONFIG_PATHS = [os.path.expanduser("~/.spnavrc"), "/etc/spnavrc"]

_font_cache = {}


def font(size, name="DejaVuSans-Bold"):
    key = (name, size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(font_path(name), size)
        except OSError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def page_base(title, background="#0a0a1e"):
    img = Image.new("RGB", (WIDTH, HEIGHT), background)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, 30], fill=(30, 30, 70))
    d.text((8, 15), title, fill=(255, 255, 255), anchor="lm", font=font(20))
    d.text((WIDTH - 8, 15), datetime.now().strftime("%H:%M"),
           fill=(0, 255, 255), anchor="rm", font=font(20))
    return img, d


def _bar(d, x, y, w, h, fraction, color):
    fraction = max(0.0, min(1.0, fraction))
    d.rectangle([x, y, x + w, y + h], outline=(90, 90, 120))
    if fraction > 0:
        d.rectangle([x + 1, y + 1, x + 1 + int((w - 2) * fraction), y + h - 1],
                    fill=color)


def _usage_color(fraction):
    if fraction < 0.6:
        return (0, 200, 80)
    if fraction < 0.85:
        return (240, 200, 0)
    return (230, 40, 40)


# --------------------------------------------------------------------------
# Button mappings
# --------------------------------------------------------------------------

def find_spnavrc():
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            return path
    return None


def parse_mappings(path):
    mappings = {}
    with open(path) as fp:
        for line in fp:
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            for prefix, fmt in (("bnact", lambda v: ACTION_LABELS.get(v, v)),
                                ("kbmap", lambda v: f"Key: {v}"),
                                ("bnmap", lambda v: f"-> button {v}")):
                if key.startswith(prefix) and key[len(prefix):].isdigit():
                    mappings[int(key[len(prefix):])] = fmt(value)
    return sorted(mappings.items())


def render_mappings(cfg):
    img, d = page_base("SpacePilot Pro")
    entry_font, small_font = font(15), font(12)
    config_path = find_spnavrc()
    mappings = parse_mappings(config_path) if config_path else []
    if not mappings:
        d.text((WIDTH // 2, HEIGHT // 2), "No button mappings in spnavrc",
               fill=(255, 255, 0), anchor="mm", font=entry_font)
        d.text((WIDTH // 2, HEIGHT // 2 + 24), "(spacenavd defaults active)",
               fill=(160, 160, 160), anchor="mm", font=small_font)
    else:
        per_col, top, line_h = 9, 40, 21
        for n, (button, label) in enumerate(mappings[:per_col * 2]):
            col, row = divmod(n, per_col)
            x, y = 8 + col * (WIDTH // 2), top + row * line_h
            name = BUTTON_NAMES.get(button, f"B{button}")
            d.text((x, y), f"[{name}]", fill=(0, 255, 0), font=entry_font)
            d.text((x + 56, y), label[:15], fill=(255, 255, 255),
                   font=entry_font)
        if len(mappings) > per_col * 2:
            d.text((WIDTH - 8, HEIGHT - 14),
                   f"+{len(mappings) - per_col * 2} more",
                   fill=(160, 160, 160), anchor="rm", font=small_font)
    source = os.path.basename(config_path) if config_path else "no spnavrc"
    d.text((8, HEIGHT - 14), source, fill=(100, 100, 140), anchor="lm",
           font=small_font)
    return img


# --------------------------------------------------------------------------
# Clocks
# --------------------------------------------------------------------------

def _now(tz_name):
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now()


def _time_text(now, cfg):
    fmt = "%H:%M" if cfg["use_24h"] else "%I:%M"
    if cfg["show_seconds"]:
        fmt += ":%S"
    text = now.strftime(fmt)
    if not cfg["use_24h"]:
        text = text.lstrip("0") + now.strftime(" %p")
    return text


def render_clock(cfg):
    # Clean full-screen clock: no page header (it would repeat the time).
    if cfg["style"] == "analog":
        return render_analog_clock(cfg)
    dual = bool(cfg["second_timezone"])
    img = Image.new("RGB", (WIDTH, HEIGHT), cfg["background"])
    d = ImageDraw.Draw(img)
    now = _now(cfg["timezone"])
    size = max(20, min(96, int(cfg["size"])))
    if dual:
        size = min(size, 54)
        now2 = _now(cfg["second_timezone"])
        rows = [(cfg["label"] or cfg["timezone"] or "Local", now),
                (cfg["second_label"] or cfg["second_timezone"], now2)]
        for n, (label, t) in enumerate(rows):
            y = 62 + n * 96
            d.text((WIDTH // 2, y), _time_text(t, cfg), fill=cfg["color"],
                   anchor="mm", font=font(size, cfg["font"]))
            d.text((WIDTH // 2, y + 40), label[:32], fill=(180, 180, 180),
                   anchor="mm", font=font(14, cfg["font"]))
    else:
        d.text((WIDTH // 2, 100), _time_text(now, cfg), fill=cfg["color"],
               anchor="mm", font=font(size, cfg["font"]))
        if cfg["label"]:
            d.text((WIDTH // 2, 40), cfg["label"][:32], fill=(180, 180, 180),
                   anchor="mm", font=font(15, cfg["font"]))
        if cfg["show_date"]:
            d.text((WIDTH // 2, 180), now.strftime("%A %d %B %Y"),
                   fill=(255, 255, 255), anchor="mm",
                   font=font(17, cfg["font"]))
    return img


def render_analog_clock(cfg):
    img = Image.new("RGB", (WIDTH, HEIGHT), cfg["background"])
    d = ImageDraw.Draw(img)
    now = _now(cfg["timezone"])
    cx, cy, r = WIDTH // 2, HEIGHT // 2, 105
    color = cfg["color"]
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=3)
    for h in range(12):
        a = math.radians(h * 30)
        r0 = r - (12 if h % 3 == 0 else 7)
        d.line([cx + r0 * math.sin(a), cy - r0 * math.cos(a),
                cx + (r - 3) * math.sin(a), cy - (r - 3) * math.cos(a)],
               fill=color, width=3 if h % 3 == 0 else 1)

    def hand(angle, length, width, col):
        a = math.radians(angle)
        d.line([cx, cy, cx + length * math.sin(a), cy - length * math.cos(a)],
               fill=col, width=width)

    hand((now.hour % 12 + now.minute / 60) * 30, r * 0.52, 5, color)
    hand(now.minute * 6 + now.second / 10, r * 0.78, 3, color)
    if cfg["show_seconds"]:
        hand(now.second * 6, r * 0.88, 1, "#ff4040")
    d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=color)
    if cfg["show_date"]:
        d.text((cx, HEIGHT - 14), now.strftime("%a %d %b"),
               fill=(180, 180, 180), anchor="mm", font=font(14, cfg["font"]))
    if cfg["label"] or cfg["timezone"]:
        d.text((cx, 12), (cfg["label"] or cfg["timezone"])[:32],
               fill=(180, 180, 180), anchor="mm", font=font(13, cfg["font"]))
    return img


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def render_calendar(cfg):
    now = datetime.now()
    img, d = page_base(now.strftime("%B %Y"))
    cal = calendar.Calendar(0 if cfg["week_starts_monday"] else 6)
    days = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    if not cfg["week_starts_monday"]:
        days = days[-1:] + days[:-1]
    col_w, top, row_h = WIDTH // 7, 42, 28
    head_font, day_font = font(13), font(15)
    for n, day in enumerate(days):
        d.text((n * col_w + col_w // 2, top), day, fill=(140, 140, 200),
               anchor="mm", font=head_font)
    for row, week in enumerate(cal.monthdayscalendar(now.year, now.month)):
        for col, day in enumerate(week):
            if day == 0:
                continue
            x = col * col_w + col_w // 2
            y = top + 22 + row * row_h
            if day == now.day:
                d.ellipse([x - 13, y - 12, x + 13, y + 12],
                          fill=cfg["highlight"])
            d.text((x, y), str(day), fill=cfg["color"], anchor="mm",
                   font=day_font)
    return img


# --------------------------------------------------------------------------
# System monitor
# --------------------------------------------------------------------------

def render_system(cfg, stats):
    img, d = page_base(cfg["title"] if cfg["title"] != "System monitor"
                       else (platform.node() or "System"))
    f, small = font(16), font(13)

    def row(y, label, fraction, text, temp):
        d.text((10, y), label, fill=(255, 255, 255), font=f)
        _bar(d, 60, y + 1, 150, 14, fraction, _usage_color(fraction))
        d.text((218, y), text, fill=(200, 200, 200), font=small)
        if temp is not None:
            d.text((WIDTH - 8, y), f"{temp:.0f}C", fill=(0, 255, 255),
                   anchor="ra", font=small)

    y = 48
    if cfg["show_cpu"]:
        cpu = stats.cpu_percent()
        row(y, "CPU", cpu / 100, f"{cpu:3.0f}%", stats.cpu_temp())
        y += 38
    if cfg["show_ram"]:
        used, total = stats.memory()
        row(y, "RAM", used / total if total else 0,
            f"{used:.1f}/{total:.0f}G", None)
        y += 38
    gpu = stats.gpu() if (cfg["show_gpu"] or cfg["show_vram"]) else None
    if cfg["show_gpu"]:
        if gpu is not None:
            row(y, "GPU", gpu[0] / 100, f"{gpu[0]:3.0f}%", gpu[3])
        else:
            d.text((10, y), "GPU: n/a", fill=(120, 120, 120), font=f)
        y += 38
    if cfg["show_vram"] and gpu is not None:
        row(y, "VRAM", gpu[1] / gpu[2] if gpu[2] else 0,
            f"{gpu[1]:.1f}/{gpu[2]:.0f}G", None)
        y += 38
    if cfg["show_net"]:
        rx, tx = stats.net_rate()
        d.text((10, y), "NET", fill=(255, 255, 255), font=f)
        d.text((60, y), f"v {rx / 1048576:.2f} M/s   ^ {tx / 1048576:.2f} M/s",
               fill=(200, 200, 200), font=small)
        y += 38
    load1, load5, load15 = os.getloadavg()
    d.text((10, HEIGHT - 18), f"load {load1:.2f} {load5:.2f} {load15:.2f}",
           fill=(100, 100, 140), font=small)
    return img


# --------------------------------------------------------------------------
# 6DOF input test
# --------------------------------------------------------------------------

AXIS_NAMES = ["TX", "TY", "TZ", "RX", "RY", "RZ"]


def render_input(cfg, state):
    """state: spnav_client.State (axes, pressed buttons, connected)."""
    img, d = page_base(cfg["title"] or "6DOF input test")
    small = font(12)
    if state is None or not state.connected:
        d.text((WIDTH // 2, HEIGHT // 2), "spacenavd not reachable",
               fill=(255, 80, 80), anchor="mm", font=font(16))
        d.text((WIDTH // 2, HEIGHT // 2 + 24), "/var/run/spnav.sock",
               fill=(150, 150, 150), anchor="mm", font=small)
        return img
    rng = max(50, int(cfg["axis_range"]))
    bar_w, x0 = 220, 60
    for n, name in enumerate(AXIS_NAMES):
        y = 42 + n * 22
        value = state.axes[n]
        d.text((10, y + 7), name, fill=(255, 255, 255), anchor="lm",
               font=small)
        d.rectangle([x0, y, x0 + bar_w, y + 14], outline=(90, 90, 120))
        mid = x0 + bar_w // 2
        d.line([mid, y, mid, y + 14], fill=(90, 90, 120))
        frac = max(-1.0, min(1.0, value / rng))
        if frac:
            x1 = mid + int(frac * (bar_w // 2 - 1))
            d.rectangle([min(mid, x1), y + 2, max(mid, x1), y + 12],
                        fill=(0, 200, 255) if n < 3 else (255, 160, 0))
        d.text((WIDTH - 8, y + 7), str(value), fill=(200, 200, 200),
               anchor="rm", font=small)
    # Button grid: physical buttons only (codes 17-21, the "6"-"10" second
    # functions, never fire in hardware — verified via hidraw).
    top = 180
    d.text((10, top - 4), "Buttons:", fill=(160, 160, 160), font=small)
    physical = [b for b in range(31) if not 17 <= b <= 21]
    for n, b in enumerate(physical):
        col, row = n % 13, n // 13
        x = 10 + col * 23
        y = top + 12 + row * 22
        lit = b in state.buttons
        d.rectangle([x, y, x + 20, y + 18],
                    fill=(0, 220, 100) if lit else (35, 35, 60),
                    outline=(90, 90, 120))
        name = BUTTON_NAMES.get(b, str(b))[:3]
        d.text((x + 10, y + 9), name, fill=(0, 0, 0) if lit
               else (150, 150, 170), anchor="mm", font=font(9))
    return img


# --------------------------------------------------------------------------
# Button profiles
# --------------------------------------------------------------------------

def render_profiles(cfg, ui):
    """ui: {"names": [...], "active": name, "sel": index, "bindings":
    {button: {label, keys}} of the selected profile}."""
    img, d = page_base(cfg["title"] or "Profiles")
    small, entry = font(12), font(14)
    names = ui["names"]
    sel = ui["sel"]
    # Left column: selectable profile list.
    for n, name in enumerate(names[:8]):
        y = 40 + n * 22
        if n == sel:
            d.rectangle([4, y - 2, 118, y + 16], fill=(60, 60, 140))
        marker = "● " if name == ui["active"] else "  "
        d.text((8, y), (marker + name)[:14], fill=(0, 255, 120)
               if name == ui["active"] else (255, 255, 255), font=entry)
    d.line([124, 36, 124, HEIGHT - 28], fill=(90, 90, 120))
    # Right column: bindings of the selected profile.
    bindings = ui["bindings"]
    if not bindings:
        d.text((222, 110), "default:", fill=(160, 160, 160), anchor="mm",
               font=entry)
        d.text((222, 132), "no key injection", fill=(160, 160, 160),
               anchor="mm", font=small)
    else:
        items = sorted(bindings.items(), key=lambda kv: int(kv[0]))
        for n, (button, b) in enumerate(items[:8]):
            y = 40 + n * 22
            name = BUTTON_NAMES.get(int(button), f"B{button}")
            d.text((132, y), f"[{name}]", fill=(0, 255, 0), font=small)
            label = b.get("label") or b.get("keys", "")
            d.text((176, y), label[:13], fill=(255, 255, 255), font=small)
            d.text((WIDTH - 6, y), b.get("keys", "")[:10],
                   fill=(140, 140, 200), anchor="ra", font=small)
        if len(items) > 8:
            d.text((WIDTH - 8, HEIGHT - 28), f"+{len(items) - 8} more",
                   fill=(160, 160, 160), anchor="rm", font=small)
    d.text((8, HEIGHT - 14), "Up/Down select - OK activate",
           fill=(100, 100, 140), anchor="lm", font=small)
    return img


def render_active_profile(cfg, ui):
    """Shows the active profile with the function-key pad drawn like the
    physical layout: keys 1-4 at the corners of a square, key 5 in the
    middle. Each physical key has two functions (bank 1 = buttons 12-16,
    bank 2 = buttons 17-21); the Menu button toggles the bank and the
    active one is highlighted."""
    img, d = page_base(cfg["title"] or "Active profile")
    name = ui["active"]
    bindings = ui["active_bindings"]
    bank = ui.get("fn_bank", 1)
    d.text((10, 44), name, fill=(0, 255, 120), anchor="lm", font=font(18))

    if not bindings:
        d.text((WIDTH // 2, 130), "No key injection",
               fill=(180, 180, 180), anchor="mm", font=font(16))
        d.text((WIDTH // 2, 154), "(native SpaceMouse behavior)",
               fill=(120, 120, 140), anchor="mm", font=font(12))
        return img

    d.text((WIDTH - 8, 44), f"bank {'6-10' if bank == 2 else '1-5'}"
           " (Menu)", fill=(255, 190, 0) if bank == 2 else (140, 140, 200),
           anchor="rm", font=font(12))

    # Physical pad: key 1 top-left, 2 top-right, 3 bottom-left,
    # 4 bottom-right, 5 center. Buttons: bank1 = 12+i, bank2 = 17+i.
    boxes = [
        (0, 14, 58, 118, 102),      # key 1: top-left
        (1, 202, 58, 306, 102),     # key 2: top-right
        (4, 108, 104, 212, 148),    # key 5: center
        (2, 14, 150, 118, 194),     # key 3: bottom-left
        (3, 202, 150, 306, 194),    # key 4: bottom-right
    ]
    tiny = font(10)

    def line(b):
        if not b or not (b.get("label") or b.get("keys")):
            return "-"
        return (b.get("label") or b.get("keys", ""))[:12]

    for i, x0, y0, x1, y1 in boxes:
        b1 = bindings.get(str(12 + i))
        b2 = bindings.get(str(17 + i))
        used = (b1 or b2)
        d.rounded_rectangle([x0, y0, x1, y1], radius=6,
                            fill=(45, 45, 90) if used else (25, 25, 45),
                            outline=(120, 120, 200) if used
                            else (60, 60, 90), width=2)
        d.text((x0 + 8, y0 + 6), str(i + 1), fill=(0, 255, 0) if used
               else (100, 100, 120), font=font(13))
        # Both functions, the active bank in white, the other dimmed.
        c1 = (255, 255, 255) if bank == 1 else (110, 110, 140)
        c2 = (255, 190, 0) if bank == 2 else (110, 110, 140)
        d.text((x0 + 26, y0 + 6), line(b1), fill=c1, font=tiny)
        d.text((x0 + 26, y0 + 24), line(b2), fill=c2, font=tiny)
    # Bindings outside the pad (anything not 12-21).
    extras = [(int(k), v) for k, v in bindings.items()
              if not 12 <= int(k) <= 21 and (v.get("label") or
                                             v.get("keys"))]
    if extras:
        extras.sort()
        parts = [f"[{BUTTON_NAMES.get(b, b)}] {v.get('label') or v['keys']}"
                 for b, v in extras[:4]]
        d.text((8, HEIGHT - 28), "  ".join(parts)[:52],
               fill=(160, 160, 160), font=tiny)
    d.text((8, HEIGHT - 13), "Menu: switch bank - Profiles page: switch",
           fill=(100, 100, 140), anchor="lm", font=tiny)
    return img


_saver_cache = {}


def render_saver_image(path):
    """Full-screen image for the screen saver, letterboxed on black."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, None)
    if key not in _saver_cache:
        _saver_cache.clear()
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        try:
            src = Image.open(path).convert("RGB")
            scale = min(WIDTH / src.width, HEIGHT / src.height)
            size = (max(1, int(src.width * scale)),
                    max(1, int(src.height * scale)))
            src = src.resize(size)
            canvas.paste(src, ((WIDTH - size[0]) // 2,
                               (HEIGHT - size[1]) // 2))
        except Exception:
            d = ImageDraw.Draw(canvas)
            d.text((WIDTH // 2, HEIGHT // 2), "screensaver image not found",
                   fill=(120, 120, 120), anchor="mm", font=font(13))
        _saver_cache[key] = canvas
    return _saver_cache[key]


# --------------------------------------------------------------------------
# AI usage (subscription quotas)
# --------------------------------------------------------------------------

# Draw order and accent colour of the providers aiusage.py can report on.
PROVIDER_ORDER = ["claude", "antigravity"]
PROVIDER_COLORS = {"claude": (230, 140, 90), "antigravity": (140, 150, 255)}


def _countdown(target):
    """Time left until `target` as "4d 3h" / "2h14m" / "9m", or None."""
    if target is None:
        return None
    left = (target - datetime.now(timezone.utc)).total_seconds()
    if left <= 0:
        return "now"
    if left >= 86400:
        return f"{int(left // 86400)}d {int(left % 86400 // 3600)}h"
    if left >= 3600:
        return f"{int(left // 3600)}h{int(left % 3600 // 60):02d}m"
    return f"{max(1, int(left // 60))}m"


def _draw_provider(d, y0, height, key, status):
    accent = PROVIDER_COLORS.get(key, (200, 200, 200))
    d.text((8, y0), status.get("name") or key.title(), fill=accent,
           font=font(16))
    # Right of the name: where the numbers came from, so an estimate is
    # never mistaken for the real quota.
    tag = ("local est." if status.get("source") == "local"
           else (status.get("plan") or "live"))
    if status.get("note"):
        tag = f"{tag} - {status['note']}"
    d.text((WIDTH - 8, y0 + 4), tag[:30], fill=(130, 130, 160), anchor="rm",
           font=font(11))

    if status.get("error"):
        d.text((16, y0 + 26), status["error"][:36], fill=(255, 120, 120),
               font=font(13))
        return
    metrics = status.get("metrics") or []
    if not metrics:
        d.text((16, y0 + 26), "no data", fill=(140, 140, 140), font=font(13))
        return
    row_h = min(24, max(15, (height - 22) // len(metrics)))
    small, tiny = font(13), font(11)
    for n, metric in enumerate(metrics):
        y = y0 + 22 + n * row_h + row_h // 2 - 1
        # Antigravity labels carry a model-group prefix ("Gem 5h"), so they
        # get their own narrower font to stay clear of the bar column.
        d.text((8, y), metric["label"][:7], fill=(255, 255, 255),
               anchor="lm", font=font(12))
        percent = metric.get("percent")
        if percent is None:
            # No quota configured for this metric: the raw count is all we
            # can honestly show, so it takes the bar's place.
            d.text((66, y), metric.get("detail", "")[:21],
                   fill=(190, 190, 190), anchor="lm", font=small)
        else:
            fraction = percent / 100
            _bar(d, 66, y - 6, 100, 12, fraction, _usage_color(fraction))
            d.text((172, y), f"{percent:.0f}%", fill=(255, 255, 255),
                   anchor="lm", font=small)
            if metric.get("detail"):
                d.text((216, y), metric["detail"][:8], fill=(150, 150, 190),
                       anchor="lm", font=tiny)
        left = _countdown(metric.get("resets_at"))
        if left:
            d.text((WIDTH - 8, y), left, fill=(0, 255, 255), anchor="rm",
                   font=tiny)


def render_ai_usage(cfg, snapshot):
    """snapshot: aiusage.AIUsage.snapshot() - {provider: status, updated}."""
    img, d = page_base(cfg["title"] or "AI usage", cfg["background"])
    shown = [k for k in PROVIDER_ORDER if cfg.get(f"show_{k}")]
    if not shown:
        d.text((WIDTH // 2, HEIGHT // 2), "No provider enabled",
               fill=(180, 180, 180), anchor="mm", font=font(16))
        return img
    if not snapshot:
        d.text((WIDTH // 2, HEIGHT // 2), "Reading usage...",
               fill=(180, 180, 180), anchor="mm", font=font(16))
        return img
    top, footer = 36, 20
    block_h = (HEIGHT - top - footer) // len(shown)
    for n, key in enumerate(shown):
        if n:
            d.line([8, top + n * block_h - 6, WIDTH - 8,
                    top + n * block_h - 6], fill=(60, 60, 90))
        _draw_provider(d, top + n * block_h, block_h, key,
                       snapshot.get(key) or {})
    age = time.time() - snapshot.get("updated", 0)
    d.text((8, HEIGHT - 9), f"updated {int(age)}s ago" if age < 90
           else f"updated {int(age // 60)}m ago", fill=(100, 100, 140),
           anchor="lm", font=font(11))
    return img


# --------------------------------------------------------------------------
# Overlays (menu, help, OSD, notifications)
# --------------------------------------------------------------------------

def draw_menu(img, titles, selection):
    d = ImageDraw.Draw(img)
    f = font(16)
    line_h = 26
    box_w = 230
    box_h = line_h * len(titles) + 42
    x0, y0 = (WIDTH - box_w) // 2, max(8, (HEIGHT - box_h) // 2)
    d.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(20, 20, 50),
                outline=(120, 120, 200), width=2)
    d.text((x0 + box_w // 2, y0 + 15), "Menu", fill=(255, 255, 255),
           anchor="mm", font=font(17))
    for n, name in enumerate(titles):
        y = y0 + 32 + n * line_h
        if n == selection:
            d.rectangle([x0 + 6, y - 1, x0 + box_w - 6, y + line_h - 7],
                        fill=(60, 60, 140))
        d.text((x0 + 16, y + line_h // 2 - 4), name[:22],
               fill=(255, 255, 255), anchor="lm", font=f)
    return img


def draw_help(img):
    d = ImageDraw.Draw(img)
    f = font(14)
    d.rectangle([10, 36, WIDTH - 10, HEIGHT - 10], fill=(20, 20, 50),
                outline=(120, 120, 200), width=2)
    lines = [
        ("Left/Right", "previous / next page"),
        ("Up/Down", "backlight brightness"),
        ("Light", "backlight on/off"),
        ("Menu", "page menu (OK confirm)"),
        ("OK", "refresh page"),
        ("Back", "close / first page"),
        ("Settings", "this help"),
    ]
    for n, (key, desc) in enumerate(lines):
        y = 46 + n * 22
        d.text((22, y), key, fill=(0, 255, 0), font=f)
        d.text((120, y), desc, fill=(255, 255, 255), font=f)
    return img


def draw_osd(img, text, fraction=None):
    d = ImageDraw.Draw(img)
    d.rectangle([20, HEIGHT - 48, WIDTH - 20, HEIGHT - 14], fill=(20, 20, 50),
                outline=(120, 120, 200))
    if fraction is None:
        d.text((WIDTH // 2, HEIGHT - 31), text[:34], fill=(255, 255, 255),
               anchor="mm", font=font(15))
    else:
        d.text((30, HEIGHT - 31), text, fill=(255, 255, 255), anchor="lm",
               font=font(15))
        _bar(d, 150, HEIGHT - 38, 150, 14, fraction, (0, 200, 255))
    return img


def draw_notification(img, app, summary, body):
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, WIDTH - 8, 92], fill=(40, 30, 10),
                outline=(255, 190, 0), width=2)
    d.text((16, 20), (app or "Notification")[:30], fill=(255, 190, 0),
           font=font(13))
    d.text((16, 38), (summary or "")[:34], fill=(255, 255, 255), font=font(15))
    if body:
        body = body.replace("\n", " ")
        d.text((16, 58), body[:40], fill=(210, 210, 210), font=font(12))
        if len(body) > 40:
            d.text((16, 73), body[40:80], fill=(210, 210, 210), font=font(12))
    return img


RENDERERS = {
    "mappings": lambda cfg, ctx: render_mappings(cfg),
    "clock": lambda cfg, ctx: render_clock(cfg),
    "calendar": lambda cfg, ctx: render_calendar(cfg),
    "system": lambda cfg, ctx: render_system(cfg, ctx["stats"]),
    "input": lambda cfg, ctx: render_input(cfg, ctx.get("spnav")),
    "profiles": lambda cfg, ctx: render_profiles(cfg, ctx["profiles_ui"]),
    "active_profile": lambda cfg, ctx: render_active_profile(
        cfg, ctx["profiles_ui"]),
    "ai_usage": lambda cfg, ctx: render_ai_usage(cfg, ctx.get("ai") or {}),
}


def refresh_interval(page_cfg):
    """How often a page wants to be re-rendered, in seconds."""
    t = page_cfg["type"]
    if t == "input":
        return 0.15
    if t == "system":
        return max(1, int(page_cfg["refresh_seconds"]))
    if t == "ai_usage":
        # The poller works in the background; re-drawing every 15s only
        # keeps the reset countdowns honest.
        return 15
    if t == "clock" and page_cfg["show_seconds"]:
        return 1
    return 60
