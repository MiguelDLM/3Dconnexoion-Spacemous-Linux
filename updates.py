#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Release checking and in-place updating for 3dxdisp-pro.

Two halves that can be used independently:

* :class:`UpdateChecker` asks the GitHub releases API, at most once every
  ``check_hours``, whether a newer tag than ``lcdconfig.VERSION`` exists. The
  answer is cached on disk, so restarting the daemon does not re-query and the
  60-per-hour anonymous rate limit is never a concern. It runs on a background
  thread and the callers only ever read the last snapshot.
* :func:`install` applies the update to a git checkout: fast-forward to the
  release tag, refresh the dependencies, restart the systemd user unit. It
  refuses to touch a working tree with local changes and never merges, so it
  can only ever move the branch forward to exactly what the release points at.

Both are also reachable from the command line::

    python3 updates.py            # report what is available
    python3 updates.py --install  # apply it
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import lcdconfig

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(lcdconfig.CONFIG_DIR, "update-cache.json")
SERVICE = "spacepilot-lcd.service"
# GitHub rejects API calls without one.
USER_AGENT = f"3dxdisp-pro/{lcdconfig.VERSION}"
HTTP_TIMEOUT = 10
GIT_TIMEOUT = 120
PIP_TIMEOUT = 300


def repo_slug():
    """"owner/name" parsed out of lcdconfig.REPO_URL."""
    return lcdconfig.REPO_URL.rstrip("/").split("github.com/", 1)[-1]


def parse_version(text):
    """"v1.2.3" -> (1, 2, 3). Unparseable pieces stop the parse."""
    parts = []
    for chunk in str(text or "").lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate, current):
    """True when `candidate` is a strictly higher version than `current`."""
    new, old = parse_version(candidate), parse_version(current)
    if not new:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------

def _load_cache():
    try:
        with open(CACHE_FILE) as fp:
            cache = json.load(fp)
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        os.makedirs(lcdconfig.CONFIG_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as fp:
            json.dump(cache, fp, indent=2)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def fetch_latest():
    """The newest published release, or an {"error": ...} dict."""
    url = f"https://api.github.com/repos/{repo_slug()}/releases/latest"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        # 404 simply means the project has no published release yet.
        if err.code == 404:
            return {"tag": "", "checked": time.time()}
        return {"error": f"HTTP {err.code}"}
    except Exception as err:
        return {"error": str(err)[:64]}
    return {
        "tag": payload.get("tag_name") or "",
        "name": payload.get("name") or "",
        "url": payload.get("html_url") or f"{lcdconfig.REPO_URL}/releases",
        "notes": (payload.get("body") or "").strip(),
        "published": payload.get("published_at") or "",
        "checked": time.time(),
    }


def status(force=False, check_hours=24):
    """Cached release status.

    Returns a dict with at least `current`, `available` and `tag`; `error`
    when the last attempt failed, and the cached release fields otherwise.
    """
    cache = _load_cache()
    age = time.time() - float(cache.get("checked") or 0)
    if force or not cache.get("checked") or age >= max(1, check_hours) * 3600:
        fresh = fetch_latest()
        if "error" in fresh:
            # Keep the last good answer; only the error is refreshed, and
            # the timestamp is left alone so the next tick retries.
            cache = dict(cache, error=fresh["error"])
        else:
            cache = dict(fresh, notified_tag=cache.get("notified_tag"))
        _save_cache(cache)
    result = dict(cache)
    result["current"] = f"v{lcdconfig.VERSION}"
    result["available"] = is_newer(cache.get("tag"), lcdconfig.VERSION)
    return result


def mark_notified(tag):
    """Remember that `tag` was already announced, so it is announced once."""
    cache = _load_cache()
    cache["notified_tag"] = tag
    cache["notified_at"] = time.time()
    _save_cache(cache)


def notify_desktop(summary, body):
    """Fire-and-forget desktop notification; silently a no-op without
    notify-send, which is all the daemon can reasonably assume."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=3dxdisp-pro", summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class UpdateChecker:
    """Background release poll; read .snapshot(), never blocks the caller."""

    def __init__(self, cfg=None):
        self._cfg = dict(cfg or {})
        self._snapshot = {}
        self._force = False
        self._wake = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def configure(self, cfg):
        if dict(cfg) != self._cfg:
            self._cfg = dict(cfg)
            self._wake.set()

    def snapshot(self):
        return self._snapshot

    def check_now(self):
        self._force = True
        self._wake.set()

    def stop(self):
        self._running = False
        self._wake.set()

    def _run(self):
        while self._running:
            cfg = self._cfg
            force, self._force = self._force, False
            try:
                self._snapshot = status(
                    force=force,
                    check_hours=int(cfg.get("check_hours") or 24))
            except Exception as err:          # never kill the thread
                self._snapshot = {"error": str(err)[:64],
                                  "current": f"v{lcdconfig.VERSION}",
                                  "available": False}
            # status() decides for itself whether the cache is stale, so the
            # loop only has to tick often enough to notice a config change.
            self._wake.wait(900)
            self._wake.clear()


# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------

def _run(argv, cwd=INSTALL_DIR, timeout=GIT_TIMEOUT):
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)
    output = (done.stdout + done.stderr).strip()
    return done.returncode, output


def _git(*args):
    return _run(["git"] + list(args))


def venv_python():
    """The interpreter of the project venv, or the running one."""
    candidate = os.path.join(INSTALL_DIR, "venv", "bin", "python")
    return candidate if os.access(candidate, os.X_OK) else sys.executable


def install_method():
    """How (and whether) this copy can update itself in place.

    Returns (method, detail) where method is "git" for a checkout we can
    fast-forward, or None with a human-readable reason otherwise.
    """
    if getattr(sys, "frozen", False):
        return None, ("this is a standalone binary; download the new one "
                      "from the release page")
    if shutil.which("git") is None:
        return None, "git is not installed"
    code, _ = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        return None, f"{INSTALL_DIR} is not a git checkout"
    code, remotes = _git("remote", "-v")
    slug = repo_slug()
    if code != 0 or slug.lower() not in remotes.lower():
        return None, f"no git remote points at {slug}"
    code, dirty = _git("status", "--porcelain", "--untracked-files=no")
    if code == 0 and dirty:
        return None, ("the working tree has local changes; commit or stash "
                      "them first")
    return "git", INSTALL_DIR


def service_active():
    if shutil.which("systemctl") is None:
        return False
    code, out = _run(["systemctl", "--user", "is-active", SERVICE], cwd=None,
                     timeout=15)
    return out.strip() == "active"


def plan(tag):
    """The exact commands install() would run, for the confirmation dialog."""
    steps = ["git fetch --tags origin",
             f"git merge --ff-only {tag or 'origin'}",
             f"{os.path.relpath(venv_python(), INSTALL_DIR)}"
             " -m pip install -r requirements.txt"]
    if service_active():
        steps.append(f"systemctl --user restart {SERVICE}")
    return steps


def install(tag=None, log=None):
    """Fast-forward the checkout to `tag` and restart the daemon.

    Returns (ok, lines). Every step is reported through `log` as it runs, so
    a caller can stream it into a dialog.
    """
    lines = []

    def say(text):
        lines.append(text)
        if log is not None:
            log(text)

    method, detail = install_method()
    if method is None:
        say(f"Cannot update in place: {detail}")
        return False, lines

    code, out = _git("fetch", "--tags", "--quiet", "origin")
    if code != 0:
        say(f"git fetch failed: {out}")
        return False, lines
    say("Fetched origin.")

    target = tag or "origin/HEAD"
    code, out = _git("rev-parse", "--verify", "--quiet",
                     target + "^{commit}")
    if code != 0:
        say(f"Release {target} not found in the repository.")
        return False, lines

    # --ff-only guarantees this only ever moves the branch forward onto the
    # released commit: no merge, no rewrite, nothing to resolve by hand.
    code, out = _git("merge", "--ff-only", target)
    if code != 0:
        say(f"Cannot fast-forward to {target}: {out}")
        say("Your checkout has diverged from the release; update manually.")
        return False, lines
    say(f"Updated the checkout to {target}.")

    python = venv_python()
    code, out = _run([python, "-m", "pip", "install", "--quiet", "-r",
                      "requirements.txt"], timeout=PIP_TIMEOUT)
    say("Dependencies up to date." if code == 0
        else f"Dependency install reported a problem:\n{out}")

    if service_active():
        code, out = _run(["systemctl", "--user", "restart", SERVICE],
                         cwd=None, timeout=60)
        say("Daemon restarted." if code == 0
            else f"Could not restart the daemon: {out}")
    else:
        say(f"{SERVICE} is not running; nothing to restart.")

    _save_cache(dict(_load_cache(), checked=0))   # re-check on next tick
    say("Update finished. Restart the settings app to load the new code.")
    return True, lines


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main(argv):
    info = status(force=True)
    if info.get("error"):
        print(f"Could not reach GitHub: {info['error']}", file=sys.stderr)
        return 2
    current, tag = info["current"], info.get("tag") or ""
    if not tag:
        print("No releases published yet.")
        return 0
    if not info["available"]:
        print(f"Up to date ({current}).")
        return 0
    print(f"Update available: {tag} (installed {current})")
    print(info.get("url") or "")
    if "--install" not in argv:
        method, detail = install_method()
        print("Run with --install to apply it." if method
              else f"In-place update unavailable: {detail}")
        return 0
    ok, _ = install(tag, log=print)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
