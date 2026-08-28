#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Subscription/quota metrics for the AI-usage applet.

Two providers today, both read straight from what the CLIs already leave on
disk — nothing is installed and no key is ever asked for:

* **Claude** — the authoritative numbers come from the same endpoint the
  ``/usage`` command of Claude Code uses (``GET /api/oauth/usage``), signed
  with the OAuth token Claude Code keeps in ``~/.claude/.credentials.json``.
  It answers with the 5-hour and 7-day window utilisation (0-100) plus the
  reset timestamps. That endpoint rate-limits hard, so it is polled at most
  once every ``API_MIN_INTERVAL`` seconds. When it is unreachable (no
  credentials, expired token, 429) we fall back to counting tokens in the
  session transcripts under ``~/.claude/projects/**/*.jsonl``, grouped into
  5-hour blocks the way ccusage does.
* **Antigravity** — the ``agy`` CLI already knows its own quotas, and its
  ``/usage`` slash command answers with JSON in print mode instead of opening
  the TUI panel. That gives the 5-hour and weekly window of every model group
  (Gemini models, and Claude/GPT models) with the exact reset times, straight
  from Google's backend. The call runs no model turn, so it costs no tokens.

Everything runs on a background thread and the applet only ever reads the
last snapshot, so rendering never blocks on the network or on disk.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CLAUDE_DIR = os.path.expanduser("~/.claude")
CLAUDE_CREDENTIALS = os.path.join(CLAUDE_DIR, ".credentials.json")
CLAUDE_PROJECTS = os.path.join(CLAUDE_DIR, "projects")
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
# Claude's usage windows: rolling 5 hours and 7 days.
BLOCK_HOURS = 5

# The endpoint answers 429 to anything more eager than this.
API_MIN_INTERVAL = 180
# How far back the transcript tailer keeps records (covers a 5-hour block
# and the day's total with room to spare).
RETENTION_HOURS = 30


def _utc_now():
    return datetime.now(timezone.utc)


def _day_start():
    """Local midnight, as an aware UTC datetime."""
    local = datetime.now().astimezone()
    return local.replace(hour=0, minute=0, second=0,
                         microsecond=0).astimezone(timezone.utc)


def _parse_iso(value):
    """Parse an ISO-8601 stamp, tolerating 'Z' and fractional seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Incremental .jsonl tailing
# --------------------------------------------------------------------------

class _JsonlTail:
    """Follows a set of append-only .jsonl files.

    Only the bytes appended since the previous scan are read, and records
    older than the retention window are dropped, so polling every minute
    costs nothing even with a long history on disk.
    """

    def __init__(self, extract):
        # extract(dict) -> (key, timestamp, payload) or None
        self._extract = extract
        self._offsets = {}       # path -> bytes consumed
        self._records = {}       # key -> (timestamp, payload)

    def scan(self, pattern, cutoff):
        for path in glob.glob(pattern, recursive=True):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = self._offsets.get(path, 0)
            if size < offset:    # rotated or rewritten: start over
                offset = 0
            if size == offset:
                continue
            try:
                with open(path, "rb") as fp:
                    fp.seek(offset)
                    data = fp.read()
            except OSError:
                continue
            # A trailing partial line is left for the next scan.
            tail = data.rfind(b"\n")
            if tail < 0:
                continue
            self._offsets[path] = offset + tail + 1
            for line in data[:tail].splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                try:
                    got = self._extract(record)
                except Exception:
                    got = None
                if got is not None:
                    key, stamp, payload = got
                    self._records[key] = (stamp, payload)
        self._records = {k: v for k, v in self._records.items()
                         if v[0] >= cutoff}
        return sorted(self._records.values(), key=lambda r: r[0])


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

def _claude_extract(record):
    """(key, timestamp, tokens) for one assistant turn of a transcript."""
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    stamp = _parse_iso(record.get("timestamp"))
    if stamp is None:
        return None
    tokens = sum(int(usage.get(field) or 0) for field in
                 ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"))
    # Streaming writes the same turn several times; the message id plus the
    # request id identifies it exactly once (this is what ccusage keys on).
    key = f"{message.get('id')}:{record.get('requestId')}"
    return key, stamp, tokens


def _current_block(records, hours=BLOCK_HOURS):
    """ccusage-style billing block: it opens on the hour of the first turn
    after a gap of `hours`, and runs for `hours`. Returns (start, tokens,
    turns) for the block covering now, or None when nothing is active."""
    start = None
    previous = None
    tokens = turns = 0
    for stamp, value in records:
        if (start is None or stamp - start >= timedelta(hours=hours)
                or stamp - previous >= timedelta(hours=hours)):
            start = stamp.replace(minute=0, second=0, microsecond=0)
            tokens = turns = 0
        tokens += value
        turns += 1
        previous = stamp
    if start is None or _utc_now() - start >= timedelta(hours=hours):
        return None
    return start, tokens, turns


class ClaudeProvider:
    name = "Claude"

    def __init__(self):
        self._tail = _JsonlTail(_claude_extract)
        self._api = None            # last good API payload
        self._api_time = 0.0
        self._api_error = None
        self._agent = None

    # ----- OAuth usage endpoint -------------------------------------------
    def _user_agent(self):
        """Without a claude-code User-Agent the endpoint uses a much
        stingier rate-limit bucket, so report the installed version."""
        if self._agent is None:
            version = "2.0.0"
            try:
                out = subprocess.run(["claude", "--version"],
                                     capture_output=True, text=True,
                                     timeout=10).stdout
                found = re.search(r"\d+\.\d+(\.\d+)?", out)
                if found:
                    version = found.group(0)
            except Exception:
                pass
            self._agent = f"claude-code/{version}"
        return self._agent

    @staticmethod
    def _token():
        with open(CLAUDE_CREDENTIALS) as fp:
            oauth = json.load(fp).get("claudeAiOauth") or {}
        return oauth.get("accessToken"), oauth.get("subscriptionType")

    def _poll_api(self):
        """Refresh the cached API payload, honouring the minimum interval."""
        if time.time() - self._api_time < API_MIN_INTERVAL:
            return
        self._api_time = time.time()
        try:
            token, plan = self._token()
        except (OSError, ValueError):
            self._api_error = "no credentials"
            return
        if not token:
            self._api_error = "not signed in"
            return
        request = urllib.request.Request(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_BETA,
            "Content-Type": "application/json",
            "User-Agent": self._user_agent(),
        })
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as err:
            self._api_error = {401: "token expired", 403: "token rejected",
                               429: "rate limited"}.get(err.code,
                                                        f"HTTP {err.code}")
            return
        except Exception as err:
            self._api_error = str(err)[:32]
            return
        if not isinstance(payload.get("five_hour"), dict):
            self._api_error = "unexpected reply"
            return
        payload["_plan"] = plan
        self._api, self._api_error = payload, None

    @staticmethod
    def _api_metrics(payload):
        metrics = []
        for key, label in (("five_hour", "5h"), ("seven_day", "7d"),
                           ("seven_day_opus", "Opus"),
                           ("seven_day_sonnet", "Sonn")):
            window = payload.get(key)
            if not isinstance(window, dict):
                continue
            utilization = window.get("utilization")
            if utilization is None:
                continue
            metrics.append({"label": label,
                            "percent": float(utilization),
                            "resets_at": _parse_iso(window.get("resets_at")),
                            "detail": ""})
        extra = payload.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled") \
                and extra.get("utilization") is not None:
            metrics.append({"label": "Xtra",
                            "percent": float(extra["utilization"]),
                            "resets_at": None,
                            "detail": _credits(extra)})
        return metrics

    # ----- local transcripts ----------------------------------------------
    def _local_metrics(self, block_budget):
        cutoff = _utc_now() - timedelta(hours=RETENTION_HOURS)
        records = self._tail.scan(
            os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), cutoff)
        block = _current_block(records)
        if block is None:
            return [{"label": "5h", "percent": None, "resets_at": None,
                     "detail": "no activity"}]
        start, tokens, turns = block
        day = sum(value for stamp, value in records if stamp >= _day_start())
        return [{"label": "5h",
                 "percent": (100.0 * tokens / block_budget if block_budget
                             else None),
                 "resets_at": start + timedelta(hours=BLOCK_HOURS),
                 "detail": (f"{_short(tokens)}" if block_budget else
                            f"{_short(tokens)} tok, {turns} turns")},
                # No 7-day figure without the API, but the day's total is
                # cheap and gives the block some context.
                {"label": "day", "percent": None, "resets_at": None,
                 "detail": f"{_short(day)} tok today"}]

    def poll(self, cfg):
        source = cfg.get("claude_source", "auto")
        if source != "local":
            self._poll_api()
            if self._api is not None:
                return {"name": self.name, "source": "api",
                        "plan": self._api.get("_plan"),
                        "note": self._api_error,
                        "metrics": self._api_metrics(self._api)}
            if source == "api":
                return {"name": self.name, "source": "api",
                        "error": self._api_error or "no data"}
        budget = int(cfg.get("claude_block_tokens") or 0)
        try:
            metrics = self._local_metrics(budget)
        except OSError as err:
            return {"name": self.name, "source": "local", "error": str(err)}
        return {"name": self.name, "source": "local",
                "note": self._api_error if source == "auto" else None,
                "metrics": metrics}


def _credits(extra):
    used, limit = extra.get("used_credits"), extra.get("monthly_limit")
    if used is None or limit is None:
        return ""
    return f"${used:.0f}/${limit:.0f}"


# --------------------------------------------------------------------------
# Antigravity
# --------------------------------------------------------------------------

# The `agy` CLI ships the same quota panel its /usage slash command draws, and
# in print mode it answers with structured JSON instead of opening the TUI.
# The call refreshes the numbers from Google's backend but runs no model turn
# (`usage.total_tokens` comes back 0), so it costs nothing but a round trip.
AGY_ARGS = ["--print", "/usage", "--output-format", "json"]
AGY_TIMEOUT = 30
# Left to itself the CLI drops a fresh log file per run, which at one poll
# every few minutes piles up fast. Point it at a single throwaway file and
# truncate that before each call, so only the last run is kept.
AGY_LOG = os.path.join(tempfile.gettempdir(), "spacepilot-lcd-agy.log")
# Long bucket names have to fit a 320px row, so windows get short labels.
WINDOW_LABELS = {"5h": "5h", "weekly": "7d", "daily": "24h"}


def _agy_label(bucket, group):
    """Short row label like "Gem 5h" / "3P 7d" for one quota bucket."""
    ident = str(bucket.get("id") or "")
    prefix = ident.split("-")[0] if "-" in ident else ""
    if not prefix:
        prefix = (group.get("name") or "").split()[0]
    window = str(bucket.get("window") or "")
    return f"{prefix[:3].title()} {WINDOW_LABELS.get(window, window)}".strip()


class AntigravityProvider:
    name = "Antigravity"

    def __init__(self):
        self._binary = None

    def _resolve(self, command):
        """Find the CLI, including when the daemon runs with systemd's
        minimal PATH and `agy` only lives in ~/.local/bin."""
        if self._binary and os.path.basename(self._binary) == command:
            return self._binary
        found = shutil.which(command)
        if found is None:
            candidate = os.path.expanduser(f"~/.local/bin/{command}")
            found = candidate if os.access(candidate, os.X_OK) else None
        self._binary = found
        return found

    def poll(self, cfg):
        command = cfg.get("antigravity_command") or "agy"
        binary = self._resolve(command)
        if binary is None:
            return {"name": self.name, "error": f"{command} not found"}
        try:
            open(AGY_LOG, "w").close()
        except OSError:
            pass
        try:
            done = subprocess.run(
                [binary] + AGY_ARGS + ["--log-file", AGY_LOG],
                capture_output=True, text=True, timeout=AGY_TIMEOUT,
                # Run from the home directory so the CLI never adopts
                # whatever the daemon's working directory happens to be.
                cwd=os.path.expanduser("~"))
        except subprocess.TimeoutExpired:
            return {"name": self.name, "error": "timed out"}
        except OSError as err:
            return {"name": self.name, "error": str(err)[:32]}
        try:
            payload = json.loads(done.stdout)
        except ValueError:
            reason = (done.stderr or done.stdout or "no output").strip()
            return {"name": self.name, "error": reason.splitlines()[0][:32]}
        if payload.get("status") != "SUCCESS":
            return {"name": self.name,
                    "error": str(payload.get("status") or "failed")[:32]}
        groups = ((payload.get("command") or {}).get("data") or {}).get(
            "groups") or []
        metrics = []
        for group in groups:
            for bucket in group.get("buckets") or []:
                remaining = bucket.get("remaining_fraction")
                if remaining is None:
                    continue
                metrics.append({
                    "label": _agy_label(bucket, group),
                    # The CLI reports what is left; the bars show what is
                    # spent, like every other row on the page.
                    "percent": 100.0 * (1.0 - float(remaining)),
                    "resets_at": _parse_iso(bucket.get("reset_time")),
                    "detail": ""})
        if not metrics:
            return {"name": self.name, "error": "no quota groups"}
        return {"name": self.name, "source": "api", "metrics": metrics}


def _short(tokens):
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.0f}k"
    return str(tokens)


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------

PROVIDERS = {"claude": ClaudeProvider,
             "antigravity": AntigravityProvider}


class AIUsage:
    """Background poller; the applet reads .snapshot() and never blocks.

    `configure()` takes the applet's page config (which providers to show,
    the quotas to measure against, the poll interval) and is safe to call
    whenever the daemon hot-reloads its configuration.
    """

    def __init__(self, cfg=None):
        self._cfg = dict(cfg or {})
        self._providers = {}
        self._snapshot = {}
        self._wake = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def configure(self, cfg):
        if dict(cfg) != self._cfg:
            self._cfg = dict(cfg)
            self._wake.set()

    def snapshot(self):
        """{provider key: status dict}, plus "updated" (epoch) once polled."""
        return self._snapshot

    def stop(self):
        self._running = False
        self._wake.set()

    def _interval(self):
        return max(10, int(self._cfg.get("refresh_seconds") or 180))

    def _run(self):
        while self._running:
            cfg = self._cfg
            result = {}
            for key, factory in PROVIDERS.items():
                if not cfg.get(f"show_{key}", True):
                    continue
                provider = self._providers.get(key)
                if provider is None:
                    provider = self._providers[key] = factory()
                try:
                    result[key] = provider.poll(cfg)
                except Exception as err:      # never kill the poll thread
                    result[key] = {"name": factory.name,
                                   "error": str(err)[:32]}
            result["updated"] = time.time()
            self._snapshot = result
            self._wake.wait(self._interval())
            self._wake.clear()


if __name__ == "__main__":       # quick check: python3 aiusage.py
    usage = AIUsage({"show_claude": True, "show_antigravity": True})
    while not usage.snapshot():
        time.sleep(0.2)
    print(json.dumps(usage.snapshot(), indent=2, default=str))
    usage.stop()
