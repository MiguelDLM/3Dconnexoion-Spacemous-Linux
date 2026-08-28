# 3dxdisp-pro — 3Dconnexion SpacePilot Pro LCD on Linux

![CI](https://github.com/MiguelDLM/3dxdisp-pro/actions/workflows/ci.yml/badge.svg)
![release](https://img.shields.io/github/v/release/MiguelDLM/3dxdisp-pro?include_prereleases)
![device](https://img.shields.io/badge/device-046d%3Ac629-blue)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

Bring the LCD screen of the **3Dconnexion SpacePilot Pro** back to life on Linux.

The open-source [spacenavd](https://github.com/FreeSpacenav/spacenavd) driver handles
the 6DOF axes and buttons perfectly, but it does not support the LCD, which stays
frozen on the boot logo. This project documents the (previously undocumented for this
device) USB protocol and provides:

- **`spplcd.py`** — library + CLI to push images, text, or a test pattern to the LCD
- **`spnav_lcd_daemon.py`** — a small daemon that shows your spacenavd button
  assignments and a clock on the screen, similar to what 3DxWare does on Windows
- **`99-spacepilot-pro-lcd.rules`** — udev rule for non-root access
- **`spacepilot-lcd.service`** — systemd user unit for the daemon

## Relationship with FreeSpacenav (spacenavd, libspnav, spnavcfg)

**spacenavd is required** — it is the driver that makes the 6DOF sensor and the
31 buttons work at all; this project does not replace it, it completes it. The
device exposes two independent USB interfaces, and each side of the stack owns
one:

```
                      SpacePilot Pro (USB 046d:c629)
                     ┌──────────────┬───────────────┐
                     │ interface 1  │  interface 0  │
                     │ HID: 6DOF +  │  LCD + bezel  │
                     │ 31 buttons   │  keys         │
                     └──────┬───────┴───────┬───────┘
                            │               │
                   ┌────────▼──────┐ ┌──────▼──────────────┐
                   │   spacenavd   │ │  THIS PROJECT       │
                   │  (FreeSpace-  │ │  spnav_lcd_daemon   │
                   │   nav driver) │ │  (pyusb, direct)    │
                   └────────┬──────┘ └──▲───────┬──────────┘
                            │ socket    │       │ uinput virtual
              /var/run/spnav.sock       │       │ keyboard (profiles)
                  ┌─────────┼───────────┘       │
                  │         │ button/motion     ▼
          ┌───────▼──────┐  │ events     ┌──────────────┐
          │ Blender, ... │◄─┘            │ any focused  │
          │ (libspnav)   │               │ application  │
          └──────────────┘               └──────────────┘
```

- **spacenavd** owns the HID interface: axes and buttons. Blender and other
  NDOF-aware apps consume them through **libspnav** / the spacenavd socket.
- **This project** owns the LCD interface (spacenavd ignores it): screen
  drawing and the bezel keys around it. No conflict is possible — different
  USB interfaces.
- For the **6DOF input-test page and the button profiles**, our daemon
  connects to spacenavd's socket *as one more client* (like Blender does), so
  it sees the same button/motion events without stealing them from anyone.
- **Button profiles vs spacenavd's `kbmap`**: spacenavd has a built-in
  keyboard mapping (`kbmapN`), but it is X11-only, global, and single-profile.
  Our profiles inject through a **uinput virtual keyboard** instead (works on
  Wayland) and can be switched per context from the LCD. Both can coexist;
  in practice you want spacenavd's config for motion tuning (`bnact`,
  sensitivity, dead zones) and this project's profiles for shortcuts.
- **spnavcfg** (the FreeSpacenav GUI) configures spacenavd itself (axes,
  sensitivity, `bnactN`); our settings app configures the LCD and profiles.
  They edit different files and don't interfere.

## How it works (the interesting part)

The SpacePilot Pro was built while 3Dconnexion was a Logitech subsidiary, and its
screen is **the same 320×240 color LCD module used by the Logitech G19 keyboard**,
speaking the same protocol that was reverse-engineered years ago in
[libg19](https://github.com/jgeboski/libg19).

The device (USB `046d:c629`) exposes two interfaces:

| Interface | Class | Purpose |
|---|---|---|
| 0 | Vendor-specific (0xFF) | **The LCD.** Bulk OUT endpoint `0x02` (512-byte packets). No kernel driver binds to it. |
| 1 | HID | The 6DOF sensor and buttons (what spacenavd uses, via `/dev/input`). |

Because the LCD has its own interface, it can be claimed with libusb/pyusb **without
touching spacenavd or motion input at all**.

### Frame format

A full screen update is **one bulk transfer of 154,112 bytes** to endpoint `0x02`:

```
512-byte header  +  320*240*2 bytes of RGB565 little-endian pixels
```

The header (taken from libg19) is 15 magic bytes followed by incrementing filler:

```
10 0F 00 58 02 00 00 00 00 00 00 3F 01 EF 00 | 0F 10 11 12 ... FF 00 01 ...
```

### Panel orientation (determined empirically on real hardware)

The physical panel is a **240×320 portrait module mounted sideways, with a mirrored
horizontal scan**. A landscape 320×240 image must be transformed with
`FLIP_LEFT_RIGHT` then `ROTATE_90` (PIL transposes) before being written row-major
into the framebuffer. Colors are plain RGB565 little-endian, no byte swap.

If you skip this you get diagonal color stripes and mirrored text — that is how the
orientation was discovered.

### Extras

- **Brightness** is controlled with a vendor control transfer
  (`bmRequestType=0x41, bRequest=0x0A, wValue=0-100`, per libg19) — exposed as
  `spplcd.py --brightness N`.
- **The keys around the screen** (menu/navigation, G19 heritage) do *not* go
  through the HID interface, so spacenavd never sees them. They report on the
  LCD interface via interrupt endpoint `0x81` as a 2-byte little-endian bitmask
  (press sets the bit, release sends `0x0000`) — captured on real hardware and
  matching the Logitech G19 display-key codes exactly:

  | Key | Bit | Key | Bit |
  |---|---|---|---|
  | Settings | `0x0001` | Right | `0x0010` |
  | Back | `0x0002` | Left | `0x0020` |
  | Menu | `0x0004` | Down | `0x0040` |
  | OK | `0x0008` | Up | `0x0080` |
  | Light | `0x0200` | | |

  The daemon gives them functions (see below); `bezel_keys.py` is the raw
  capture tool used to discover them (stop the daemon first, both claim
  interface 0).
- Note for SpacePilot *original* (`046d:c625`) owners: that model has a different
  monochrome 240×64 screen driven by HID feature reports; use
  [jtsiomb/3dxdisp](https://github.com/jtsiomb/3dxdisp) instead. This project is
  specifically for the **Pro**.

## Installation

```bash
git clone https://github.com/MiguelDLM/3dxdisp-pro.git
cd 3dxdisp-pro

# Dependencies (in a venv, or use your distro's python3-usb / python3-pil)
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Non-root USB access (make sure your user is in the 'plugdev' group)
sudo cp 99-spacepilot-pro-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --action=change --attr-match=idVendor=046d --attr-match=idProduct=c629
# (or simply unplug and replug the device)
```

## Usage

```bash
venv/bin/python spplcd.py                   # color bars test pattern
venv/bin/python spplcd.py picture.png       # any image, rescaled to 320x240
venv/bin/python spplcd.py --text "hello"    # centered text
venv/bin/python spplcd.py --brightness 60   # backlight brightness, 0-100
```

### Button-mapping daemon

`spnav_lcd_daemon.py` turns the screen into a configurable applet panel in the
spirit of 3DxWare's LCD applets on Windows. Available applets:

- **Button mappings**: the `bnactN` / `kbmapN` / `bnmapN` assignments from your
  spacenavd config (`~/.spnavrc` or `/etc/spnavrc`), with physical key names.
- **Clock**: digital or **analog** (hands), 12/24h, seconds, date, custom font,
  colors and size, any time zone, and **dual clock** (two time zones at once).
- **Calendar**: month view with today highlighted.
- **System monitor**: live CPU / RAM / GPU / VRAM / network usage bars with
  temperatures (AMD GPUs via amdgpu sysfs, NVIDIA via `nvidia-smi`).
- **Active profile**: shows the current button profile with the function-key
  pad drawn like the physical layout (keys 1-4 at the corners, 5 in the
  middle) so you can see at a glance what each key does.
- **6DOF input test**: live translation/rotation axis bars and a 31-button grid
  that lights up as you press — verify the device works at a glance (reads
  spacenavd's socket directly, alongside your other apps).
- **AI usage**: how much of your Claude and Antigravity quota is left, with
  the time to each reset — see below.
- **Desktop notifications**: the same notifications GNOME/KDE show can be
  mirrored on the LCD as overlay popups (via `dbus-monitor`, optional).

#### AI usage applet

A quota gauge for the AI CLIs you already have signed in — no key to paste and
nothing extra to install (`aiusage.py` uses only the standard library):

- **Claude** — the real numbers, straight from the endpoint the `/usage`
  command of Claude Code itself calls
  (`GET https://api.anthropic.com/api/oauth/usage`), signed with the OAuth
  token Claude Code keeps in `~/.claude/.credentials.json`. It reports the
  **5-hour** and **7-day** window utilisation (plus the per-model weekly
  windows on plans that have them, and extra-usage credits when enabled) with
  the exact reset timestamps. That endpoint rate-limits aggressively, so it is
  polled at most once every 180 seconds and the answer is cached.
- **Claude, offline fallback** — when the endpoint is unreachable (not signed
  in, expired token, HTTP 429) the applet counts tokens in the session
  transcripts under `~/.claude/projects/**/*.jsonl`, grouped into the same
  5-hour blocks [ccusage](https://github.com/ryoppippi/ccusage) uses. Set
  *Claude 5h token budget* to turn that count into a percentage bar; leave it
  at 0 to just read the raw totals. *Claude data source* forces `api` or
  `local` instead of the `auto` default.
- **Antigravity** — the `agy` CLI already knows its own quotas, and its
  `/usage` slash command answers with JSON in print mode instead of opening
  the TUI panel: `agy --print /usage --output-format json`. That gives the
  **5-hour and weekly** window of every model group — *Gemini models* (`Gem`)
  and *Claude and GPT models* (`3P`) — with the exact reset times, refreshed
  from Google's backend. The call runs no model turn (`usage.total_tokens`
  comes back 0), so watching your quota never spends it. Its log is redirected
  to a single throwaway file (`$TMPDIR/spacepilot-lcd-agy.log`, truncated
  before each call) instead of the per-run log the CLI writes by default. Set
  *Antigravity CLI command* if the binary is not called `agy` or is not on the
  daemon's `PATH` (`~/.local/bin` is searched too, which systemd user units
  usually miss).

Every row says where its number came from (`pro` / `live` / `local est.`), so
an estimate is never mistaken for the real quota. Polling happens on a
background thread, so a slow network never stalls the screen. Adding another
provider is a `poll(cfg)` method plus one entry in `aiusage.PROVIDERS`.

### Settings application

`lcd_settings.py` is a native Qt window (PySide6) to configure everything: which
pages to show and their order (add/remove/reorder), every applet option (fonts,
colors with a color picker, time zones, clock styles...), brightness, OSD and
notifications — with a **pixel-exact live preview** rendered by the daemon's own
applet code. Saving applies instantly: the daemon hot-reloads
`~/.config/spacepilot-lcd/config.json` without restarting.

```bash
venv/bin/pip install PySide6        # only needed for the settings app
venv/bin/python lcd_settings.py
```

A desktop launcher template is included (`spacepilot-lcd-settings.desktop`): edit
the path inside and copy it to `~/.local/share/applications/`. To ship the app as
a single binary, PyInstaller works: `pyinstaller --onefile lcd_settings.py`.

### Button profiles (SpaceMouse Enterprise style)

Profiles map SpaceMouse buttons to keyboard shortcuts per use context — e.g. a
"Blender Edit" profile where function key 1 sends `Tab`, 2 sends `G` (move),
3 sends `R` (rotate); or a "LibreOffice Calc" profile with undo/redo/sheet
navigation. Injection happens through a **uinput virtual keyboard**
(python-evdev), so it works on Wayland and X11 alike.

- The **`default` profile always exists** and means *no injection at all* —
  the device behaves natively (spacenavd/Blender NDOF untouched). You can
  return to it at any time.
- **Switching**: add the **Profiles page** to the LCD; on that page the bezel
  Up/Down keys move through the profile list and **OK activates** (the page
  shows each profile's button table, Enterprise style; the active one is
  marked ●). The chosen profile persists across daemon restarts.
- **Editing**: the settings app has a **Profiles tab** — create, duplicate
  (e.g. "Blender Sculpt" from "Blender Edit"), rename, delete, and fill a
  31-row table of label + keys per button. Invalid combos are flagged red.
- **Keys syntax**: `ctrl+shift+z`, `tab`, `g`, `f12`, `ctrl+pagedown`,
  `kp_plus`... Letters, digits, F-keys, navigation and modifiers are
  layout-independent; for arithmetic symbols prefer the `kp_*` numpad names.
- Example profiles in [`examples/profiles.json`](examples/profiles.json)
  (Blender Edit / Blender Sculpt / LibreOffice Calc).

**Permissions**: writing to `/dev/uinput` is required. On many modern systems
logind already grants the seated user an ACL (check with `getfacl
/dev/uinput`); otherwise install the included rule:
`sudo cp 99-spacepilot-uinput.rules /etc/udev/rules.d/` (and make sure your
user is in the `input` group).

**Dual-function keys**: the SpacePilot Pro has 5 physical function keys but 10
function-key button codes (12-21). Verified on real hardware (raw hidraw
captures): **the device only ever emits codes 12-16** — one per physical key;
the "second functions" (6-10) were implemented in software by 3DxWare. This
project does the same: while a profile is active, the **Menu button toggles
the function bank** — in bank 2 the physical keys 1-5 trigger the bindings of
buttons 17-21. The Active-profile page shows both functions per key and
highlights the live bank, and an OSD confirms each toggle. (While a profile is
active, Menu is reserved for this; with `default` it stays native.)

**Blender tip**: Blender consumes many SpaceMouse buttons natively (views,
Fit, modifiers). The function keys (buttons 12-21) are unbound by default,
which makes them ideal for profile bindings without double-handling.

### Bezel keys

**Screen saver**: optionally, after a configurable idle time (no bezel keys,
buttons or motion) the screen can stay as-is, switch to a chosen page (e.g. the
clock) or show a custom image; any interaction restores the previous page.

Every bezel key press gives on-screen feedback (OSD):

| Key | Function |
|---|---|
| Left / Right | previous / next page |
| Up / Down | backlight brightness (on the Profiles page: select profile) |
| Light | backlight on / off |
| Menu | page menu — Up/Down to select, OK to confirm, Back to cancel |
| OK | confirm in menu; refresh page otherwise |
| Back | close menu/help; otherwise jump to first page |
| Settings | help overlay with this key reference |

The daemon survives device unplug/replug.

### Applet ideas (contributions welcome)

Media now-playing (MPRIS), weather, pomodoro timer, per-core CPU, disk usage,
photo slideshow, e-mail/RSS counters, more AI providers (Codex keeps
`tokens_used` per thread in `~/.codex/state_5.sqlite`)... the applet API is a single pure function
returning a 320×240 PIL image (see `applets.py`).

> While the daemon runs it holds the LCD USB interface claimed — stop it
> (`systemctl --user stop spacepilot-lcd`) before using `spplcd.py` manually.

```bash
venv/bin/python spnav_lcd_daemon.py         # run in foreground to try it
```

To run it permanently, install the systemd user unit (edit the paths inside first):

```bash
mkdir -p ~/.config/systemd/user
cp spacepilot-lcd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now spacepilot-lcd
```

## Using it with Blender

Blender maps all 31 SpacePilot Pro buttons **natively** through its GHOST NDOF
layer (same numbering as spacenavd): Menu opens the NDOF popup, Fit frames the
selection, Top/Front/Right switch views, Esc/Alt/Shift/Ctrl act as those keys,
and so on. Customize them in *Preferences > Keymap* (search for "NDOF").

What Blender does *not* handle is daemon-side motion tuning. The example config
[`examples/spnavrc-blender`](examples/spnavrc-blender) binds the keys whose
physical labels match spacenavd's built-in actions — works on Wayland too:

| Key | Action |
|---|---|
| `+` / `-` | global sensitivity up / down |
| `Dominant` | restrict motion to the strongest axis |
| `Rotation` | rotation-only mode |
| `Pan Zoom` | pan/zoom-only mode |

```bash
sudo cp examples/spnavrc-blender /etc/spnavrc   # spacenavd is a system service
sudo systemctl restart spacenavd
```

The LCD daemon picks the file up automatically and shows the bindings on the
screen. The commented `kbmap` examples in the same file inject keyboard keys,
but only on X11 sessions — they do nothing on Wayland.

## Compatibility notes

- Tested on Linux with spacenavd 1.3.1 running — the daemon and spacenavd coexist
  with no interference, since they use different USB interfaces.
- Other G19-protocol devices are *not* auto-detected (the VID/PID is fixed to the
  SpacePilot Pro); adapting it to the actual G19 only requires changing the IDs.
- The SpaceMouse Enterprise LCD uses a different mechanism — see
  [spacenavd PR #134](https://github.com/FreeSpacenav/spacenavd/pull/134).

## Credits

- [libg19](https://github.com/jgeboski/libg19) by James Geboski — the G19 protocol
  (header constant and transfer format) this project builds on.
- [spacenavd / FreeSpacenav](https://github.com/FreeSpacenav/spacenavd) by John
  Tsiombikas — the driver that makes these devices usable on Linux in the first place.
- Forum archaeology: the hint that the SpacePilot Pro screen is a Logitech GamePanel
  module comes from old 3Dconnexion forum threads and
  [Jeremy Paquette's blog](https://jeremypaquette.com/blog/?post=4).

## License

GPL-3.0 — see [LICENSE](LICENSE). The protocol header data originates from libg19
(GPL-3.0).
