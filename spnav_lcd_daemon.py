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
"""Applet daemon for the SpacePilot Pro LCD, in the spirit of 3DxWare's
LCD applets on Windows.

The pages shown, their order and every visual option are configured with the
companion settings app (lcd_settings.py) and stored in
~/.config/spacepilot-lcd/config.json; the daemon hot-reloads the file when it
changes. Available applets: button mappings, digital/analog/dual clocks,
calendar, system monitor, 6DOF input test, AI subscription usage. Desktop
notifications can be mirrored on the screen as overlays, and new releases on
GitHub are announced the same way (see updates.py; installing stays manual).

Bezel keys (they report on the LCD USB interface, endpoint 0x81 — invisible
to spacenavd; every press gives on-screen feedback):
  Left / Right  previous / next page      Up / Down  backlight brightness
  Light         backlight on/off          Menu       page menu
  OK            confirm / refresh         Back       close / first page
  Settings      help overlay

Note: while the daemon runs it holds the LCD interface claimed; stop it
(systemctl --user stop spacepilot-lcd) before using spplcd.py manually.
"""

import signal
import sys
import time
from datetime import datetime

import usb.core

import aiusage
import applets
import lcdconfig
import updates
from notifications import NotificationListener
from spnav_client import SpnavClient
from spplcd import SpacePilotLCD
from sysstats import SystemStats

try:
    from keyinjector import KeyInjector
except ImportError:
    KeyInjector = None

KEY_ENDPOINT = 0x81
KEY_SETTINGS, KEY_BACK, KEY_MENU, KEY_OK = 0x01, 0x02, 0x04, 0x08
KEY_RIGHT, KEY_LEFT, KEY_DOWN, KEY_UP = 0x10, 0x20, 0x40, 0x80
KEY_LIGHT = 0x200

HELP_SECONDS = 6


def main():
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    cfg = lcdconfig.load()
    cfg_mtime = lcdconfig.mtime()
    stats = SystemStats()
    spnav = None
    ai = None               # aiusage.AIUsage, only while an AI page exists
    updater = None          # updates.UpdateChecker, while the watch is on
    announced = None        # release tag already announced this run
    notif_listener = None
    notif = None            # (app, summary, body, expiry)

    # --- button profiles ----------------------------------------------------
    active_profile = lcdconfig.load_state().get(
        "active_profile", lcdconfig.DEFAULT_PROFILE)
    profile_sel = 0
    injector = None
    injector_error = None
    # The 5 physical function keys emit a single code each (buttons 12-16);
    # the hardware has no second-function codes (verified via hidraw). The
    # Menu button toggles a software "bank": in bank 2, keys 1-5 trigger
    # the bindings of buttons 17-21 ("6"-"10"), like 3DxWare does.
    fn_bank = 1

    def valid_profiles():
        nonlocal active_profile
        names = lcdconfig.profile_names(cfg)
        if active_profile not in names:
            active_profile = lcdconfig.DEFAULT_PROFILE
        return names

    def get_injector():
        nonlocal injector, injector_error
        if injector is None and injector_error is None:
            if KeyInjector is None:
                injector_error = "python-evdev not installed"
            else:
                try:
                    injector = KeyInjector()
                except Exception as err:
                    injector_error = str(err)
                    print(f"Key injection unavailable: {err}",
                          file=sys.stderr)
        return injector

    def on_spacemouse_button(bnum, pressed):
        """Runs in the spnav client thread: inject the active binding."""
        nonlocal fn_bank, dirty
        if not pressed or active_profile == lcdconfig.DEFAULT_PROFILE:
            return
        if bnum == 0:                      # Menu: toggle function bank
            fn_bank = 2 if fn_bank == 1 else 1
            set_osd("Function keys 6-10" if fn_bank == 2
                    else "Function keys 1-5")
            return
        if fn_bank == 2 and 12 <= bnum <= 16:
            bnum += 5                      # keys 1-5 act as 6-10
        profile = lcdconfig.get_profile(cfg, active_profile)
        binding = (profile or {}).get("bindings", {}).get(str(bnum))
        if not binding or not binding.get("keys"):
            return
        inj = get_injector()
        if inj is None:
            return
        try:
            inj.press_combo(binding["keys"])
        except Exception as err:
            print(f"Injection failed for button {bnum}: {err}",
                  file=sys.stderr)

    lcd = None
    page = 0
    brightness = int(cfg["brightness"])
    light_on = True
    pressed_mask = 0
    menu_open = False
    menu_sel = 0
    help_until = 0.0
    osd = None              # (text, fraction or None, expiry)
    dirty = True
    last_render = 0.0
    last_minute = None
    last_bezel = time.time()
    saver_on = False        # screensaver currently showing
    saver_image = False     # saver is in image mode (vs page mode)
    saver_prev = 0          # page to restore on wake

    def last_activity():
        spnav_last = spnav.state.last_event if spnav else 0.0
        return max(last_bezel, spnav_last)

    def saver_wake():
        nonlocal saver_on, saver_image, page, dirty
        if saver_on:
            saver_on = saver_image = False
            page = min(saver_prev, len(pages()) - 1)
            dirty = True
            print("screensaver off", file=sys.stderr)

    def pages():
        return cfg["pages"] or [lcdconfig.applet_with_defaults(
            {"type": "clock"})]

    def need_spnav():
        return (any(p["type"] == "input" for p in pages())
                or bool(cfg.get("profiles")))

    def ai_page():
        for p in pages():
            if p["type"] == "ai_usage":
                return p
        return None

    def apply_config():
        nonlocal spnav, notif_listener, ai, updater, page, dirty, brightness
        if need_spnav() and spnav is None:
            spnav = SpnavClient(on_button=on_spacemouse_button)
        ai_cfg = ai_page()
        if ai_cfg is not None:
            if ai is None:
                ai = aiusage.AIUsage(ai_cfg)
            else:
                ai.configure(ai_cfg)
        upd_cfg = cfg.get("updates") or {}
        if upd_cfg.get("enabled"):
            if updater is None:
                updater = updates.UpdateChecker(upd_cfg)
            else:
                updater.configure(upd_cfg)
        if cfg["notifications"]["enabled"] and notif_listener is None:
            notif_listener = NotificationListener()
        valid_profiles()
        page = min(page, len(pages()) - 1)
        dirty = True

    apply_config()

    def set_osd(text, fraction=None):
        nonlocal osd, dirty
        osd = (text, fraction, time.time() + float(cfg["osd_seconds"]))
        dirty = True

    def handle_key(bit):
        nonlocal page, brightness, light_on, menu_open, menu_sel
        nonlocal help_until, dirty, profile_sel, active_profile, last_bezel
        nonlocal fn_bank
        last_bezel = time.time()
        if saver_on:
            # First key press only wakes the screen saver.
            saver_wake()
            return
        titles = [p["title"] or p["type"] for p in pages()]
        # Contextual keys on the Profiles page: Up/Down select, OK activate.
        on_profiles = (pages()[page]["type"] == "profiles"
                       and not menu_open and not help_until)
        if on_profiles and bit in (KEY_UP, KEY_DOWN, KEY_OK):
            names = valid_profiles()
            if bit == KEY_UP:
                profile_sel = (profile_sel - 1) % len(names)
            elif bit == KEY_DOWN:
                profile_sel = (profile_sel + 1) % len(names)
            else:
                active_profile = names[profile_sel]
                fn_bank = 1
                lcdconfig.save_state({"active_profile": active_profile})
                set_osd(f"Profile: {active_profile}")
            dirty = True
            return
        if bit == KEY_LIGHT:
            light_on = not light_on
            lcd.set_brightness(brightness if light_on else 0)
            if light_on:
                set_osd("Backlight on")
            return
        if bit == KEY_UP and not menu_open:
            brightness = min(100, brightness + 10)
            light_on = True
            lcd.set_brightness(brightness)
            set_osd("Brightness", brightness / 100)
        elif bit == KEY_DOWN and not menu_open:
            brightness = max(5, brightness - 10)
            light_on = True
            lcd.set_brightness(brightness)
            set_osd("Brightness", brightness / 100)
        elif bit == KEY_LEFT:
            page = (page - 1) % len(titles)
            menu_open = False
            set_osd(titles[page])
        elif bit == KEY_RIGHT:
            page = (page + 1) % len(titles)
            menu_open = False
            set_osd(titles[page])
        elif bit == KEY_MENU:
            menu_open = not menu_open
            menu_sel = page
            help_until = 0
            dirty = True
        elif bit == KEY_UP and menu_open:
            menu_sel = (menu_sel - 1) % len(titles)
            dirty = True
        elif bit == KEY_DOWN and menu_open:
            menu_sel = (menu_sel + 1) % len(titles)
            dirty = True
        elif bit == KEY_OK:
            if menu_open:
                page, menu_open = menu_sel, False
                set_osd(titles[page])
            else:
                set_osd("Refreshed")
        elif bit == KEY_BACK:
            if menu_open or help_until:
                menu_open, help_until = False, 0
            else:
                page = 0
                set_osd(titles[0])
            dirty = True
        elif bit == KEY_SETTINGS:
            help_until = time.time() + HELP_SECONDS
            menu_open = False
            dirty = True

    while running:
        if lcd is None:
            try:
                lcd = SpacePilotLCD()
                lcd.set_brightness(brightness if light_on else 0)
                dirty = True
            except (IOError, usb.core.USBError) as err:
                print(f"LCD unavailable, retrying: {err}", file=sys.stderr)
                time.sleep(10)
                continue
        try:
            now = time.time()

            # Hot-reload the configuration when the settings app saves it.
            if lcdconfig.mtime() != cfg_mtime:
                cfg = lcdconfig.load()
                cfg_mtime = lcdconfig.mtime()
                brightness = int(cfg["brightness"])
                if light_on:
                    lcd.set_brightness(brightness)
                apply_config()
                set_osd("Settings applied")

            minute = datetime.now().strftime("%H:%M")
            if minute != last_minute:
                last_minute = minute
                dirty = True

            # Screen saver: trigger after idle, wake on any activity.
            sv = cfg.get("screensaver", {})
            if saver_on and spnav and \
                    spnav.state.last_event > last_bezel and \
                    now - spnav.state.last_event < 2:
                saver_wake()
            elif (not saver_on and sv.get("enabled")
                  and sv.get("behavior") in ("page", "image")
                  and now - last_activity() > float(sv["minutes"]) * 60):
                saver_prev = page
                saver_on = True
                if sv["behavior"] == "page":
                    page = max(0, min(int(sv["page_index"]),
                                      len(pages()) - 1))
                else:
                    saver_image = True
                menu_open = False
                dirty = True
                print(f"screensaver on after "
                      f"{now - last_activity():.0f}s idle", file=sys.stderr)

            current = pages()[page]
            if now - last_render >= applets.refresh_interval(current):
                dirty = True

            if updater is not None and notif is None:
                release = updater.snapshot()
                tag = release.get("tag")
                # The cache remembers announcements across restarts;
                # `announced` covers the window before the poll thread
                # picks the cache back up.
                if (release.get("available") and tag and tag != announced
                        and tag != release.get("notified_tag")):
                    announced = tag
                    updates.mark_notified(tag)
                    upd_cfg = cfg.get("updates") or {}
                    summary = f"Update available: {tag}"
                    body = "Install it from the settings app"
                    if upd_cfg.get("notify_lcd", True):
                        notif = ("3dxdisp-pro", summary, body,
                                 now + max(8.0, float(
                                     cfg["notifications"]["seconds"])))
                        dirty = True
                    if upd_cfg.get("notify_desktop", True):
                        updates.notify_desktop(
                            summary,
                            f"3dxdisp-pro {release.get('current')} is "
                            f"installed. {body}.")
                    print(f"update available: {tag}", file=sys.stderr)

            if notif_listener is not None:
                try:
                    app, summary, body = notif_listener.queue.get_nowait()
                    notif = (app, summary, body,
                             now + float(cfg["notifications"]["seconds"]))
                    dirty = True
                except Exception:
                    pass
            if notif and now >= notif[3]:
                notif = None
                dirty = True
            if osd and now >= osd[2]:
                osd = None
                dirty = True
            if help_until and now >= help_until:
                help_until = 0
                dirty = True

            if dirty:
                names = valid_profiles()
                sel = min(profile_sel, len(names) - 1)
                sel_profile = lcdconfig.get_profile(cfg, names[sel])
                act_profile = lcdconfig.get_profile(cfg, active_profile)
                ctx = {"stats": stats,
                       "spnav": spnav.state if spnav else None,
                       "ai": ai.snapshot() if ai else None,
                       "profiles_ui": {
                           "names": names,
                           "active": active_profile,
                           "sel": sel,
                           "bindings": (sel_profile or {}).get("bindings",
                                                               {}),
                           "active_bindings": (act_profile or {}).get(
                               "bindings", {}),
                           "fn_bank": fn_bank}}
                if saver_image:
                    img = applets.render_saver_image(sv.get("image", ""))
                else:
                    img = applets.RENDERERS[current["type"]](current, ctx)
                if menu_open:
                    titles = [p["title"] or p["type"] for p in pages()]
                    img = applets.draw_menu(img, titles, menu_sel)
                elif help_until:
                    img = applets.draw_help(img)
                if notif:
                    img = applets.draw_notification(img, *notif[:3])
                if osd:
                    img = applets.draw_osd(img, osd[0], osd[1])
                lcd.send_image(img)
                last_render = now
                dirty = False

            # Poll the bezel keys; the timeout paces the loop, faster when
            # the current page wants frequent refreshes.
            timeout = min(500, int(applets.refresh_interval(current) * 1000))
            try:
                data = lcd.dev.read(KEY_ENDPOINT, 8, timeout=max(50, timeout))
            except usb.core.USBTimeoutError:
                continue
            for i in range(0, len(data) - 1, 2):
                mask = data[i] | (data[i + 1] << 8)
                new_bits = mask & ~pressed_mask
                pressed_mask = mask
                for shift in range(16):
                    bit = 1 << shift
                    if new_bits & bit:
                        handle_key(bit)
        except usb.core.USBError as err:
            print(f"USB error, reconnecting: {err}", file=sys.stderr)
            try:
                lcd.close()
            except Exception:
                pass
            lcd = None
            time.sleep(5)

    if spnav:
        spnav.stop()
    if ai:
        ai.stop()
    if updater:
        updater.stop()
    if notif_listener:
        notif_listener.stop()
    if lcd is not None:
        lcd.close()


if __name__ == "__main__":
    main()
