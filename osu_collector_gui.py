#!/usr/bin/env python3
"""
osu-collector-gui — cross-platform GUI for downloading osu!collector
collections, with progress bars and optional auto-import to osu!lazer.

Talks directly to https://osucollector.com/api/ and downloads .osz files
from a public osu! mirror (catboy.best by default). No interactive
prompting, no PTY driving — just HTTP.

Runs on Linux, Windows, and macOS. Single file. Bundle for Windows with:
    pyinstaller --noconfirm --windowed --onefile osu_collector_gui.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, as_completed, wait as futures_wait,
    FIRST_COMPLETED, TimeoutError as FuturesTimeout)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "osu-collector-gui"
APP_VERSION = "1.5.13"
APP_AUTHOR = "Red"


def _default_lazer_realm_path() -> Path:
    """Best-effort default for the user's client.realm location."""
    home = Path.home()
    if sys.platform == "win32":
        # osu!lazer on Windows stores data in %APPDATA%\osu
        return home / "AppData/Roaming/osu/client.realm"
    if sys.platform == "darwin":
        return home / "Library/Application Support/osu/client.realm"
    # Linux + everything else
    return home / ".local/share/osu/client.realm"


def _child_env() -> dict:
    """Environment for launching EXTERNAL programs (osu!lazer, Collection
    Manager CLI, flatpak) from a frozen build.

    PyInstaller points LD_LIBRARY_PATH at its own bundled libraries; a child
    GUI app that inherits it loads the WRONG system libraries and crashes on
    startup — silently, since we send its output to /dev/null. That's why
    launching osu!lazer to import worked from a clean terminal but not from
    the app. PyInstaller stashes the original value in <VAR>_ORIG; restore it
    (or drop the var entirely if there was none)."""
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = env.pop(var + "_ORIG", None)
        if orig is not None:
            env[var] = orig
        elif getattr(sys, "frozen", False):
            env.pop(var, None)
    return env


USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/R3dWolfie/Osu-Collector-GUI)"

OSU_COLLECTOR_API = "https://osucollector.com/api"
# Mirror download endpoints as URL templates — "{id}" is the beatmapset id.
# Templates (not a fixed "/d/<id>") let mirrors use different path schemes,
# e.g. Nekoha's /api4/download/. All verified to stream raw .osz bytes.
DEFAULT_MIRROR = "https://catboy.best/d/{id}"
FALLBACK_MIRRORS = [
    "https://api.nerinyan.moe/d/{id}",
    "https://osu.direct/d/{id}",                  # NOT api.osu.direct (no DNS)
    "https://mirror.nekoha.moe/api4/download/{id}",
    "https://beatconnect.io/b/{id}",
    # CN-hosted (redirects to a high port); listed last so the least-busy
    # tie-break treats it as lowest-priority bonus capacity — it only soaks up
    # downloads when the global mirrors are all at their adaptive cap.
    "https://dl.sayobot.cn/beatmaps/download/full/{id}",
]

# No-video download endpoints per mirror. Skipping the background video cuts a
# video set's size by ~70% (verified). Mirrors without a known no-video URL fall
# back to their full template, so a map never fails — it just keeps its video
# from those mirrors.
NO_VIDEO_TEMPLATES = {
    "https://catboy.best/d/{id}": "https://catboy.best/d/{id}n",
    "https://api.nerinyan.moe/d/{id}": "https://api.nerinyan.moe/d/{id}?noVideo=true",
    "https://osu.direct/d/{id}": "https://osu.direct/d/{id}?noVideo=true",
    "https://mirror.nekoha.moe/api4/download/{id}": "https://mirror.nekoha.moe/api4/download/{id}?novideo=1",
    "https://beatconnect.io/b/{id}": "https://beatconnect.io/b/{id}?novideo=1",
    "https://dl.sayobot.cn/beatmaps/download/full/{id}": "https://dl.sayobot.cn/beatmaps/download/novideo/{id}",
}

# After a mirror's connect fails, blacklist it for this many seconds so
# other parallel download slots don't waste their connect-timeout on it.
MIRROR_DEAD_TTL_S = 60

# Network limits — be polite to the mirrors
DOWNLOAD_PARALLEL = 16  # default worker threads (Balanced). Gentler than the
                        # old 48 so an average PC doesn't get hammered while
                        # downloading; the "Max speed" tuning preset bumps it
                        # back up. Download executor is hard-clamped to 32
                        # workers regardless; per-mirror adaptive caps below
                        # are the real governor on concurrency.
DOWNLOAD_TIMEOUT_S = 120
DOWNLOAD_CONNECT_TIMEOUT_S = 10   # fail fast if a mirror is rate-limiting our IP
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 2

# Adaptive per-mirror concurrency (AIMD — like TCP congestion control).
# Each mirror starts at *_START simultaneous downloads and probes upward to
# *_MAX while it stays healthy; a 429/403 halves it (down to *_MIN) and the
# mirror cools down briefly. This self-tunes to the fastest rate each mirror
# tolerates — "max speed without rate-limiting" — with no fixed guess.
PER_MIRROR_START = 2         # start gentle so the opening burst across all
                             # parallel slots doesn't trip a 429 immediately
PER_MIRROR_MIN = 1
PER_MIRROR_MAX = 12
PER_MIRROR_PROBE_EVERY = 4   # consecutive successes per +1 to the cap

# Per-mirror hard ceiling on concurrency, matched by domain substring and
# applied on TOP of the adaptive cap. Sayobot is a slow CN CDN that redirects
# to a high port (~200 KB/s from outside China); capping it at 1 means a slow
# stall there can tie up at most one worker instead of soaking several. Matches
# both its full and no-video templates.
PER_MIRROR_HARD_CAP: tuple[tuple[str, int], ...] = (
    ("sayobot", 1),
)


def _mirror_hard_cap(url: str) -> int:
    """The hard concurrency ceiling for a mirror URL (PER_MIRROR_MAX if none)."""
    for needle, cap in PER_MIRROR_HARD_CAP:
        if needle in url:
            return cap
    return PER_MIRROR_MAX
RATE_LIMIT_COOLDOWN_S = 8.0  # default pause for a 429 with no Retry-After
RATE_LIMIT_COOLDOWN_MAX = 30.0   # never sideline a mirror longer than this,
                                 # so one big Retry-After can't stall a set
DOWNLOAD_OVERALL_DEADLINE_S = 90   # give up on one set after this long and
                                   # move on, rather than blocking a worker

CONFIG_DIR = Path.home() / (
    ".config" if sys.platform != "win32" else "AppData/Roaming"
) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "settings.json"

CACHE_DIR = Path.home() / (
    ".cache" if sys.platform != "win32" else "AppData/Local"
) / APP_NAME
CM_CLI_CACHE_DIR = CACHE_DIR / "cm-cli"

CM_CLI_RELEASE_URL = (
    "https://github.com/Piotrekol/CollectionManager/releases/latest/"
    "download/CollectionManager-CLI.zip"
)

# This app's own GitHub repo — used by the built-in update checker, which
# compares APP_VERSION against the latest published Release.
GITHUB_REPO = "R3dWolfie/Osu-Collector-GUI"
GITHUB_LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

# ---------------------------------------------------------------------------
# osu!collector API client
# ---------------------------------------------------------------------------

@dataclass
class BeatmapInfo:
    beatmap_id: int
    set_id: int
    md5: str
    artist: str = "Unknown"
    title: str = "Unknown"
    diff_name: str = "Unknown"
    mode: int = 0          # 0=osu, 1=taiko, 2=fruits, 3=mania
    star_rating: float = 0.0


@dataclass
class CollectionInfo:
    id: int
    name: str
    uploader: str
    beatmap_count: int
    beatmapset_ids: list[int] = field(default_factory=list)
    beatmaps: list[BeatmapInfo] = field(default_factory=list)


_MODE_TO_INT = {"osu": 0, "taiko": 1, "fruits": 2, "catch": 2, "mania": 3}


class OsuCollectorClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # Transient statuses worth retrying — osu!collector sits behind Cloudflare
    # and intermittently 520-524s under load; plus the usual 429/502/503/504.
    _RETRYABLE = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

    def _get(self, url: str, tries: int = 4) -> requests.Response:
        """GET with exponential backoff on transient osu!collector / Cloudflare
        errors so one server hiccup doesn't abort the whole run."""
        delay: float = 1.0
        last: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                r = self.session.get(url, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
            else:
                if r.status_code not in self._RETRYABLE:
                    return r
                last = requests.HTTPError(
                    f"{r.status_code} Server Error for url: {url}", response=r)
            if attempt < tries:
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
        # Retries exhausted: hand back the last response so the caller's own
        # status handling (404 check / raise_for_status) still applies; only a
        # pure network failure (no response) is raised here.
        if isinstance(last, requests.HTTPError) and last.response is not None:
            return last.response
        raise last if last else RuntimeError(f"GET failed: {url}")

    def fetch_collection(self, collection_id: int,
                         with_beatmap_details: bool = False,
                         progress: Callable[[int], None] | None = None,
                         should_cancel: Callable[[], bool] | None = None
                         ) -> CollectionInfo:
        """Fetch collection metadata + flat list of beatmapset IDs.

        If with_beatmap_details is True, also fetches per-beatmap details
        (artist, title, diff name, mode, star rating, md5) needed for
        .osdb generation. Costs extra paginated API calls. `progress`, if
        given, is called with the running beatmap-detail count after each page
        so the UI can show "fetching…" feedback on large collections.
        """
        url = f"{OSU_COLLECTOR_API}/collections/{collection_id}"
        r = self._get(url)
        if r.status_code == 404:
            raise ValueError(f"Collection {collection_id} not found")
        r.raise_for_status()
        data = r.json()

        # Extract unique beatmapset IDs (collection metadata gives them
        # under "beatmapsets" — each item has its own .id).
        set_ids: list[int] = []
        seen: set[int] = set()
        for bs in data.get("beatmapsets", []):
            sid = bs.get("id")
            if isinstance(sid, int) and sid not in seen:
                seen.add(sid)
                set_ids.append(sid)

        info = CollectionInfo(
            id=int(data["id"]),
            name=str(data.get("name") or f"Collection {collection_id}"),
            uploader=str((data.get("uploader") or {}).get("username") or "?"),
            beatmap_count=int(data.get("beatmapCount") or len(set_ids)),
            beatmapset_ids=set_ids,
        )

        if with_beatmap_details:
            info.beatmaps = self._fetch_beatmaps_paged(
                collection_id, progress, should_cancel)
        return info

    def _fetch_beatmaps_paged(self, collection_id: int,
                              progress: Callable[[int], None] | None = None,
                              should_cancel: Callable[[], bool] | None = None
                              ) -> list[BeatmapInfo]:
        """Page through /beatmapsv2 to get details for every beatmap."""
        out: list[BeatmapInfo] = []
        cursor = "0"
        for _ in range(500):  # safety bound — most collections fit in <50 pages
            if should_cancel and should_cancel():
                break   # a big collection can be mid-fetch when Cancel is hit
            url = (f"{OSU_COLLECTOR_API}/collections/{collection_id}/beatmapsv2"
                   f"?perPage=100&cursor={cursor}")
            r = self._get(url)
            r.raise_for_status()
            data = r.json()
            for b in data.get("beatmaps", []) or []:
                bs = b.get("beatmapset") or {}
                out.append(BeatmapInfo(
                    beatmap_id=int(b.get("id") or 0),
                    set_id=int(b.get("beatmapset_id") or bs.get("id") or 0),
                    md5=str(b.get("checksum") or ""),
                    artist=str(bs.get("artist") or "Unknown"),
                    title=str(bs.get("title") or "Unknown"),
                    diff_name=str(b.get("version") or "Unknown"),
                    mode=_MODE_TO_INT.get(str(b.get("mode") or "osu").lower(), 0),
                    star_rating=float(b.get("difficulty_rating") or 0.0),
                ))
            if progress:
                try:
                    progress(len(out))
                except Exception:
                    pass
            if not data.get("hasMore"):
                break
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                break
        return out


# ---------------------------------------------------------------------------
# Beatmap mirror downloader
# ---------------------------------------------------------------------------

def _parse_retry_after(value: str | None, cap: float = 600.0) -> float | None:
    """Parse an HTTP Retry-After header into a delay in seconds.

    Accepts either a delta-seconds integer ("120") or an HTTP-date. Returns
    None when absent/unparseable so the caller can fall back to its default.
    Capped so a misbehaving mirror can't blacklist itself for hours.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), cap)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), cap) if delta > 0 else None
    except (TypeError, ValueError):
        return None


class BeatmapMirror:
    """Downloads a single .osz from a mirror with retries + fallbacks.

    Shares a process-wide dead-mirror blacklist across instances so that
    when one parallel download slot detects a mirror is blocking/timing
    out at TCP-connect time, the other 9 slots skip that mirror for the
    next MIRROR_DEAD_TTL_S seconds and go straight to a working one.
    Without this, every slot independently rediscovers the same dead
    mirror, wasting ~10s connect-timeout each.
    """

    # Class-level shared state, all guarded by _state_lock:
    #   _dead_until: {url -> monotonic time the blacklist expires}
    #   _active:     {url -> current concurrent-download count}
    #   _limit:      {url -> current adaptive concurrency cap (AIMD)}
    #   _success:    {url -> consecutive successes since the last cap bump}
    #   _rr_index:   monotonically increasing round-robin cursor; each
    #                download starts at the next mirror so load spreads
    #                evenly (1->2->3->4->1...) even at low parallelism,
    #                instead of every download hammering the primary.
    # The lock is held ONLY during pick + increment/decrement. HTTP
    # I/O never runs under the lock.
    _dead_until: dict[str, float] = {}
    _active: dict[str, int] = {}
    _limit: dict[str, int] = {}
    _success: dict[str, int] = {}
    _rr_index: int = 0
    _state_lock = __import__("threading").Lock()

    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS,
                 extra: Iterable[str] = (),
                 pool_maxsize: int = 64,
                 no_video: bool = False) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # Size the connection pool to the max parallelism so high worker
        # counts get real concurrency. The stock Session pools only 10
        # connections per host, so >10 parallel downloads to one mirror
        # would queue (or churn connections) — a hidden serialization.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=16, pool_maxsize=pool_maxsize,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # User-supplied custom mirrors go first so they're preferred, then
        # the built-ins. De-duplicated, order preserved.
        seen: set[str] = set()
        ordered = [*extra, primary, *fallbacks]
        if no_video:
            # Swap each built-in for its no-video endpoint (~70% smaller for
            # video maps); unknown/custom templates pass through unchanged.
            ordered = [NO_VIDEO_TEMPLATES.get(u, u) for u in ordered]
        self.urls = [
            u for u in ordered
            if u and not (u in seen or seen.add(u))
        ]

    @staticmethod
    def normalize_template(raw: str) -> str | None:
        """Turn a user-entered mirror URL into a download template.

        Accepts a full template containing "{id}", or a base URL to which
        "/{id}" is appended. Returns None for empty/non-http input.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        if not (raw.startswith("http://") or raw.startswith("https://")):
            return None
        if "{id}" in raw:
            return raw
        return raw.rstrip("/") + "/{id}"

    @classmethod
    def _next_start(cls) -> int:
        """Return the next round-robin offset into the mirror list."""
        with cls._state_lock:
            i = cls._rr_index
            cls._rr_index += 1
            return i

    @classmethod
    def _is_dead(cls, url: str) -> bool:
        with cls._state_lock:
            until = cls._dead_until.get(url, 0.0)
            if until > time.monotonic():
                return True
            if until:
                cls._dead_until.pop(url, None)
            return False

    @classmethod
    def _mark_dead(cls, url: str, seconds: float = MIRROR_DEAD_TTL_S) -> None:
        """Blacklist a mirror for `seconds` (process-wide, so every parallel
        slot skips it). Never shortens an existing, longer blacklist."""
        with cls._state_lock:
            until = time.monotonic() + max(1.0, seconds)
            if until > cls._dead_until.get(url, 0.0):
                cls._dead_until[url] = until

    @classmethod
    def reset_state(cls) -> None:
        """Clear dead-cache + active counts + adaptive caps. Tests + reset."""
        with cls._state_lock:
            cls._dead_until.clear()
            cls._active.clear()
            cls._limit.clear()
            cls._success.clear()
            cls._rr_index = 0

    @classmethod
    def on_rate_limited(cls, url: str, retry_after: float | None) -> float:
        """A mirror returned 429/403: multiplicatively halve its concurrency
        cap (down to the floor) and cool it down briefly. Returns the
        cooldown applied (seconds)."""
        cooldown = retry_after if retry_after else RATE_LIMIT_COOLDOWN_S
        cooldown = min(cooldown, RATE_LIMIT_COOLDOWN_MAX)
        with cls._state_lock:
            cur = cls._limit.get(url, PER_MIRROR_START)
            cls._limit[url] = max(PER_MIRROR_MIN, cur // 2)
            cls._success[url] = 0
            until = time.monotonic() + max(1.0, cooldown)
            if until > cls._dead_until.get(url, 0.0):
                cls._dead_until[url] = until
        return cooldown

    @classmethod
    def on_success(cls, url: str) -> None:
        """A clean download: additively probe the cap upward (one step per
        PER_MIRROR_PROBE_EVERY consecutive successes), up to the ceiling."""
        with cls._state_lock:
            ceiling = _mirror_hard_cap(url)
            cur = cls._limit.get(url, PER_MIRROR_START)
            if cur >= ceiling:
                cls._success[url] = 0
                return
            n = cls._success.get(url, 0) + 1
            if n >= PER_MIRROR_PROBE_EVERY:
                cls._limit[url] = min(ceiling, cur + 1)
                cls._success[url] = 0
            else:
                cls._success[url] = n

    @classmethod
    def _acquire_least_busy(cls, candidates: list[str],
                           excluding: set[str],
                           respect_caps: bool = False) -> str | None:
        """Atomically pick the least-busy alive mirror among `candidates`
        not in `excluding`, and increment its active count.

        Tie-break by index in `candidates` so the declared primary
        (typically catboy) wins on cold start and on equal counts.

        With respect_caps=False (the default), falls back to allowing dead
        mirrors when every alive candidate is excluded — better to attempt
        and surface the failure than to refuse to try at all. Returns None
        only when every candidate is in `excluding`.

        With respect_caps=True (the real download path), a mirror is only
        eligible while its active count is below its adaptive cap, and dead
        mirrors are NOT used as a fallback — None then means "nothing free
        right now, wait and retry" (cooling down or at capacity), which the
        caller distinguishes from "exhausted" via its own exclude set.
        """
        with cls._state_lock:
            now = time.monotonic()
            def _alive(u: str) -> bool:
                until = cls._dead_until.get(u, 0.0)
                if until <= now:
                    if until:
                        cls._dead_until.pop(u, None)
                    return True
                return False

            available = [u for u in candidates
                         if u not in excluding and _alive(u)]
            if respect_caps:
                # Only mirrors under their current adaptive cap are eligible;
                # no dead-mirror fallback (we'd rather wait for a cooldown).
                available = [
                    u for u in available
                    if cls._active.get(u, 0) < min(
                        cls._limit.get(u, PER_MIRROR_START), _mirror_hard_cap(u)
                    )
                ]
            elif not available:
                # Every alive candidate excluded — allow dead mirrors so
                # the caller still gets to try.
                available = [u for u in candidates if u not in excluding]
            if not available:
                return None

            chosen = min(
                available,
                key=lambda u: (cls._active.get(u, 0), candidates.index(u)),
            )
            cls._active[chosen] = cls._active.get(chosen, 0) + 1
            return chosen

    @classmethod
    def _release(cls, url: str) -> None:
        """Decrement active count for `url`. Pop the entry if count
        reaches 0. Must pair with a successful _acquire_least_busy call."""
        with cls._state_lock:
            n = cls._active.get(url, 0)
            if n <= 1:
                cls._active.pop(url, None)
            else:
                cls._active[url] = n - 1

    def download(self, beatmapset_id: int, dest_dir: Path,
                 should_cancel: Callable[[], bool] | None = None) -> Path | None:
        """Download .osz to dest_dir; return final path or None on failure.

        Mirror selection combines round-robin, adaptive per-mirror
        concurrency, and a shared dead-list:

        - Each download starts at the next mirror in rotation (1, 2, 3, 4,
          1, …) so no single mirror takes all the load.
        - A mirror is only used while its in-flight count is below its
          adaptive cap, which probes upward on success and halves on a 429
          — self-tuning to each mirror's tolerated speed.
        - A 429/403 cools the mirror down briefly (without giving up on the
          set); a 404 means this mirror doesn't have the set (try another);
          a connection/timeout failure blacklists the mirror process-wide.

        When every eligible mirror is momentarily at capacity or cooling
        down, the call waits and retries (up to an overall deadline) rather
        than failing — that's the back-pressure that keeps us at max speed
        without tripping rate limits.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Already downloaded on a previous run? Skip WITHOUT hitting a mirror —
        # saves re-requesting (and re-tripping rate limits) for maps already on
        # disk. .osz files are named "{set_id}.osz" or "{set_id} Artist - Title
        # .osz", so match the id followed by "." or " " (avoids matching a
        # longer id with the same prefix).
        sid = str(beatmapset_id)
        for p in dest_dir.glob(f"{sid}*.osz"):
            rest = p.name[len(sid):]
            if (rest == ".osz" or rest.startswith(" ")) and p.stat().st_size > 0:
                return p

        last_error: Exception | None = None
        # Mirrors permanently out for THIS set: a 404 (not hosted here) or a
        # hard connection failure. Distinct from the transient at-cap /
        # cooling-down state, which we wait out instead of excluding.
        exhausted: set[str] = set()
        n_mirrors = len(self.urls)

        start = self._next_start() % n_mirrors
        ordered = self.urls[start:] + self.urls[:start]

        deadline = time.monotonic() + DOWNLOAD_OVERALL_DEADLINE_S

        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                return None
            if len(exhausted) >= n_mirrors:
                break   # every mirror 404'd or hard-failed — genuinely gone

            base_url = self._acquire_least_busy(
                ordered, excluding=exhausted, respect_caps=True,
            )
            if base_url is None:
                # All non-exhausted mirrors are at capacity or cooling down.
                # Wait for a slot/cooldown rather than failing.
                time.sleep(0.2)
                continue

            try:
                url = base_url.format(id=beatmapset_id)
                with self.session.get(url, stream=True,
                                      timeout=(DOWNLOAD_CONNECT_TIMEOUT_S,
                                               DOWNLOAD_TIMEOUT_S),
                                      allow_redirects=True) as r:
                    if r.status_code == 404:
                        # Coverage differs per mirror — a 404 here does NOT
                        # mean the set is gone everywhere. Drop this mirror
                        # for this set and try the others.
                        exhausted.add(base_url)
                        continue
                    if r.status_code in (429, 403):
                        # Rate-limited: halve this mirror's cap and cool it
                        # down (honouring Retry-After). Keep the set in play
                        # — retry after the cooldown, possibly elsewhere.
                        cooldown = self.on_rate_limited(
                            base_url, _parse_retry_after(r.headers.get("Retry-After"))
                        )
                        last_error = requests.HTTPError(
                            f"{r.status_code} from {base_url}; cap→"
                            f"{self._limit.get(base_url)}, cooldown {int(cooldown)}s"
                        )
                        continue
                    r.raise_for_status()

                    filename = self._filename_from_response(r, beatmapset_id)
                    dest = dest_dir / filename
                    # Already have a complete copy on disk — skip the body
                    # download (saves bandwidth on re-runs and sidesteps the
                    # Windows rename-onto-existing-file failure below).
                    if dest.exists() and dest.stat().st_size > 0:
                        self.on_success(base_url)
                        return dest
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    head = b""
                    written = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if should_cancel and should_cancel():
                                tmp.unlink(missing_ok=True)
                                return None
                            if chunk:
                                if not head:
                                    head = chunk[:4]
                                written += len(chunk)
                                f.write(chunk)

                    # Validate it's actually a .osz (ZIP) and complete. Some
                    # mirrors answer 200 with a Cloudflare/rate-limit HTML or
                    # JSON page instead of the file; saved as .osz that makes
                    # osu!lazer report "Beatmap import failed". A short read vs
                    # Content-Length means a truncated download. Reject either
                    # and try another mirror.
                    expected = r.headers.get("Content-Length")
                    truncated = (expected is not None
                                 and expected.isdigit()
                                 and written < int(expected))
                    if head[:2] != b"PK" or written < 1024 or truncated:
                        tmp.unlink(missing_ok=True)
                        # A mirror serving garbage for a set it claims (200)
                        # is misbehaving — cool it down so other slots skip
                        # it briefly too, and try the next mirror for this set.
                        self._mark_dead(base_url, HTTP_BACKOFF_S)
                        exhausted.add(base_url)
                        why = ("truncated" if truncated
                               else "non-.osz response")
                        last_error = requests.HTTPError(
                            f"{base_url} returned a {why} for {beatmapset_id}"
                        )
                        continue

                    # os.replace overwrites atomically on every platform;
                    # plain rename() raises FileExistsError (WinError 183)
                    # on Windows when dest already exists.
                    tmp.replace(dest)
                    self.on_success(base_url)
                    return dest
            except (requests.ConnectionError, requests.Timeout) as e:
                # TCP-level failure: blacklist this mirror process-wide and
                # drop it for this set so we don't wait on it.
                self._mark_dead(base_url)
                exhausted.add(base_url)
                last_error = e
                continue
            except requests.RequestException as e:
                # Other transient HTTP error (5xx, etc.): brief cooldown on
                # this mirror, then let the loop pick another.
                self._mark_dead(base_url, HTTP_BACKOFF_S)
                last_error = e
                continue
            finally:
                self._release(base_url)

        # Exhausted every mirror, or hit the deadline.
        if last_error:
            raise last_error
        return None

    @staticmethod
    def _filename_from_response(r: requests.Response, set_id: int) -> str:
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-\d\'\')?"?([^";]+)"?', cd)
        if m:
            name = m.group(1).strip()
            # Some mirrors URL-encode it
            try:
                from urllib.parse import unquote
                name = unquote(name)
            except Exception:
                pass
            if name.lower().endswith(".osz"):
                return _safe_filename(name)
        return f"{set_id}.osz"


def _safe_filename(name: str) -> str:
    # Strip path separators and other unsafe chars (Windows-friendly).
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip().strip(".") or "beatmap.osz"


# ---------------------------------------------------------------------------
# .osdb file writer  (Collection Manager / osu!collector compatible)
# ---------------------------------------------------------------------------
#
# Format reference: roogue/osu-collector-dl/src/core/OsdbGenerator.ts and
# Piotrekol/CollectionManager OsdbCollectionHandler.cs.
#
# We write "o!dm6" (uncompressed) — this is the format osu-collector-dl
# itself emits, and it loads in Collection Manager and osu!lazer.
#
# Layout (.NET BinaryWriter conventions, little-endian):
#     string  "o!dm6"
#     double  save date in OADate format
#     string  editor (collection uploader name)
#     int32   number of collections (always 1 — one .osdb per collection)
#     string  collection name
#     int32   beatmap count
#     for each beatmap:
#         int32   beatmap id
#         int32   beatmap set id
#         string  artist
#         string  title
#         string  difficulty version
#         string  md5 hash
#         string  user comment ("")
#         byte    mode (0..3)
#         double  star rating
#     int32   number of "hash-only" beatmaps (always 0 here)
#     string  footer "By Piotrekol"
#
# Strings use the .NET BinaryWriter format: a 7-bit-encoded length prefix
# followed by UTF-8 bytes.

class OsdbWriter:
    @staticmethod
    def _write_7bit_int(buf: io.BytesIO, value: int) -> None:
        while value >= 0x80:
            buf.write(bytes([(value & 0x7F) | 0x80]))
            value >>= 7
        buf.write(bytes([value & 0x7F]))

    @classmethod
    def _write_string(cls, buf: io.BytesIO, s: str) -> None:
        data = s.encode("utf-8")
        cls._write_7bit_int(buf, len(data))
        buf.write(data)

    @staticmethod
    def _to_oadate(dt: datetime) -> float:
        # OADate epoch is 1899-12-30. Days since that point as a double.
        epoch = datetime(1899, 12, 30, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - epoch
        return delta.total_seconds() / 86400.0

    @classmethod
    def write(cls, dest_path: Path, info: CollectionInfo,
              prefer_md5_map: dict[int, str] | None = None) -> None:
        if not info.beatmaps:
            raise ValueError(
                "OsdbWriter requires per-beatmap details — call "
                "fetch_collection(..., with_beatmap_details=True) first."
            )
        cls.write_many(dest_path, [info], prefer_md5_map=prefer_md5_map)

    @classmethod
    def write_many(cls, dest_path: Path,
                   collections: list[CollectionInfo],
                   editor: str | None = None,
                   prefer_md5_map: dict[int, str] | None = None) -> None:
        """Write one .osdb file containing one or more collections.

        Writes o!dm8 format: an uncompressed "o!dm8" header followed by
        a gzip-wrapped body. The body re-states the version string and
        contains per-collection metadata + per-beatmap entries. CM CLI
        only writes o!dm8 these days, so the v8 reader path is the
        well-tested one — emitting v6 (uncompressed) hits an older code
        path in CM that throws EndOfStreamException on perfectly valid
        files.

        Per-collection layout (v8):
            string  name
            int32   OnlineId (-1 if none)
            int32   beatmap_count
            for each beatmap:
                int32  MapId
                int32  MapSetId
                string Artist
                string Title
                string DiffName
                string Md5
                string UserComment
                byte   PlayMode
                double StarRating
            int32   hash_only_count
            for each hash_only: string md5
        Then a single trailing footer string "By Piotrekol".
        """
        body = io.BytesIO()
        cls._write_string(body, "o!dm8")
        body.write(struct.pack("<d", cls._to_oadate(datetime.now(timezone.utc))))
        cls._write_string(body, editor or (collections[0].uploader if collections else "Unknown"))
        body.write(struct.pack("<i", len(collections)))

        for info in collections:
            cls._write_string(body, info.name or "Unknown")
            body.write(struct.pack("<i", info.id if info.id else -1))
            body.write(struct.pack("<i", len(info.beatmaps)))
            for bm in info.beatmaps:
                body.write(struct.pack("<i", bm.beatmap_id))
                body.write(struct.pack("<i", bm.set_id))
                cls._write_string(body, bm.artist or "Unknown")
                cls._write_string(body, bm.title or "Unknown")
                cls._write_string(body, bm.diff_name or "Unknown")
                md5 = bm.md5 or ""
                if prefer_md5_map and bm.beatmap_id in prefer_md5_map:
                    md5 = prefer_md5_map[bm.beatmap_id]
                cls._write_string(body, md5)
                cls._write_string(body, "")  # user comment
                body.write(bytes([max(0, min(3, bm.mode))]))
                body.write(struct.pack("<d", float(bm.star_rating)))
            body.write(struct.pack("<i", 0))   # no hash-only beatmaps

        cls._write_string(body, "By Piotrekol")

        # Wrap the body in a gzip stream. CM CLI uses SharpCompress's
        # GZipArchive which produces a standard gzip envelope; Python's
        # stdlib gzip is wire-compatible.
        import gzip as _gz
        compressed = _gz.compress(body.getvalue(), compresslevel=6)

        # Final file: uncompressed "o!dm8" header + gzip stream.
        out = io.BytesIO()
        cls._write_string(out, "o!dm8")
        out.write(compressed)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(out.getvalue())


class OsdbReader:
    """Inverse of OsdbWriter — parses an o!dm6 .osdb file back into
    CollectionInfo dataclasses. Used to round-trip existing lazer
    collections through CM CLI for non-destructive merging.
    """

    @staticmethod
    def _read_7bit_int(buf: io.BytesIO) -> int:
        result = 0
        shift = 0
        for _ in range(5):  # max 5 bytes for a 32-bit int
            b = buf.read(1)
            if not b:
                raise EOFError("unexpected end of .osdb")
            byte = b[0]
            result |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                return result
            shift += 7
        raise ValueError("malformed 7-bit int in .osdb")

    @classmethod
    def _read_string(cls, buf: io.BytesIO) -> str:
        length = cls._read_7bit_int(buf)
        return buf.read(length).decode("utf-8", errors="replace")

    @staticmethod
    def _read_int32(buf: io.BytesIO) -> int:
        return struct.unpack("<i", buf.read(4))[0]

    @staticmethod
    def _read_double(buf: io.BytesIO) -> float:
        return struct.unpack("<d", buf.read(8))[0]

    @staticmethod
    def _read_byte(buf: io.BytesIO) -> int:
        return buf.read(1)[0]

    _SUPPORTED_VERSIONS = {
        "o!dm3": 3, "o!dm4": 4, "o!dm5": 5, "o!dm6": 6,
        "o!dm7": 7, "o!dm8": 8,
    }

    @classmethod
    def read(cls, src_path: Path) -> list[CollectionInfo]:
        raw = src_path.read_bytes()
        buf = io.BytesIO(raw)

        magic = cls._read_string(buf)
        if magic not in cls._SUPPORTED_VERSIONS:
            raise ValueError(
                f"Unsupported .osdb version: {magic!r}. Supported: "
                f"{', '.join(sorted(cls._SUPPORTED_VERSIONS))}"
            )
        version = cls._SUPPORTED_VERSIONS[magic]

        # v7+ wraps the body in a gzip stream produced by SharpCompress's
        # GZipArchive. Python's gzip module reads it fine — the SharpCompress
        # filename header is part of the standard gzip envelope.
        if version >= 7:
            import gzip
            try:
                body = gzip.decompress(raw[buf.tell():])
            except OSError as e:
                raise ValueError(f"gzip decompress failed: {e}") from e
            buf = io.BytesIO(body)
            inner = cls._read_string(buf)
            if inner not in cls._SUPPORTED_VERSIONS:
                raise ValueError(
                    f"Inner version mismatch: expected o!dm*, got {inner!r}"
                )

        cls._read_double(buf)            # save date — ignored
        editor = cls._read_string(buf)
        num_collections = cls._read_int32(buf)

        result: list[CollectionInfo] = []
        for _ in range(num_collections):
            name = cls._read_string(buf)
            online_id = -1
            if version >= 7:
                online_id = cls._read_int32(buf)   # new field in v7+
            n_beatmaps = cls._read_int32(buf)
            beatmaps: list[BeatmapInfo] = []
            for _ in range(n_beatmaps):
                bm_id = cls._read_int32(buf)
                set_id = cls._read_int32(buf) if version >= 2 else 0
                artist = cls._read_string(buf)
                title = cls._read_string(buf)
                diff = cls._read_string(buf)
                md5 = cls._read_string(buf)
                cls._read_string(buf)        # user comment — ignored
                mode = cls._read_byte(buf)
                star = cls._read_double(buf)
                beatmaps.append(BeatmapInfo(
                    beatmap_id=bm_id, set_id=set_id, md5=md5,
                    artist=artist, title=title, diff_name=diff,
                    mode=mode, star_rating=star,
                ))

            # Hash-only beatmaps (md5s without metadata). v3+ wrote this
            # int32 marker; older versions ended the collection here.
            if version >= 3:
                n_hash_only = cls._read_int32(buf)
                for _ in range(n_hash_only):
                    h = cls._read_string(buf)
                    if h:
                        beatmaps.append(BeatmapInfo(
                            beatmap_id=0, set_id=0, md5=h,
                        ))

            result.append(CollectionInfo(
                id=online_id if online_id > 0 else 0,
                name=name,
                uploader=editor,
                beatmap_count=len(beatmaps),
                beatmapset_ids=sorted({b.set_id for b in beatmaps if b.set_id}),
                beatmaps=beatmaps,
            ))

        return result


def merge_collection_lists(
    *lists: list[CollectionInfo],
    on_name_collision: str = "merge",
) -> list[CollectionInfo]:
    """Merge several lists of CollectionInfo into one.

    on_name_collision:
        "merge"  — combine beatmaps from same-named collections (deduped by md5)
        "skip"   — skip the new collection if a name already exists (keep old)
        "rename" — append a numeric suffix to the new collection's name
    """
    by_name: dict[str, CollectionInfo] = {}
    order: list[str] = []

    for lst in lists:
        for c in lst:
            key = c.name.strip()
            if key not in by_name:
                by_name[key] = CollectionInfo(
                    id=c.id, name=c.name, uploader=c.uploader,
                    beatmap_count=len(c.beatmaps),
                    beatmapset_ids=list(c.beatmapset_ids),
                    beatmaps=list(c.beatmaps),
                )
                order.append(key)
                continue

            if on_name_collision == "merge":
                existing = by_name[key]
                seen = {b.md5 for b in existing.beatmaps if b.md5}
                for b in c.beatmaps:
                    if b.md5 and b.md5 not in seen:
                        existing.beatmaps.append(b)
                        seen.add(b.md5)
                existing.beatmap_count = len(existing.beatmaps)
            elif on_name_collision == "skip":
                continue
            elif on_name_collision == "rename":
                n = 2
                new_key = f"{key} ({n})"
                while new_key in by_name:
                    n += 1
                    new_key = f"{key} ({n})"
                renamed = CollectionInfo(
                    id=c.id, name=new_key, uploader=c.uploader,
                    beatmap_count=len(c.beatmaps),
                    beatmapset_ids=list(c.beatmapset_ids),
                    beatmaps=list(c.beatmaps),
                )
                by_name[new_key] = renamed
                order.append(new_key)

    return [by_name[k] for k in order]


# ---------------------------------------------------------------------------
# CollectionManager CLI runner
# ---------------------------------------------------------------------------
#
# We invoke CM CLI to round-trip existing lazer collections through .osdb,
# merge in our new collections in Python, and re-import. CM does NOT have a
# native merge command — its `convert` overwrites — so we do the merging
# ourselves and use CM purely as a Realm <-> .osdb codec.

@dataclass
class ProbeResult:
    """Result of asking CM CLI which beatmaps lazer has imported.

    `resolved` maps beatmap_id → BeatmapInfo with lazer's current md5 and
    metadata. `resolved_hashes` is the set of md5 hashes lazer recognized —
    this catches maps imported with OnlineID=-1 (common for mirror imports
    lazer couldn't verify online), which `resolved` (keyed by online id)
    would miss. A beatmap is "already imported" if its id is in `resolved`
    OR its md5 is in `resolved_hashes`.
    """
    resolved: dict[int, BeatmapInfo] = field(default_factory=dict)
    resolved_hashes: set = field(default_factory=set)


@dataclass
class CmCliConfig:
    command: list[str]          # full argv prefix to invoke CM CLI
    osu_location: str | None    # passed via -l (auto-detect if None)


class CmCliRunner:
    def __init__(self, cfg: CmCliConfig) -> None:
        self.cfg = cfg

    def export_realm_to_osdb(self, realm_path: Path, dest_osdb: Path) -> None:
        """Read existing lazer collections out to a temp .osdb.

        Always passes -s (SkipOsuLocation). When the input file is itself
        a client.realm, also passing -l would make CM open the same realm
        twice (once via LoadOsuDatabase, once via CollectionLoader),
        which Realm.NET treats as a conflicting open and throws an
        unhandled CLR exception (0xe0434352) under wine.
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(realm_path),
                "-o", str(dest_osdb),
                "-s"]
        self._run(argv)

    def import_osdb_to_realm(self, src_osdb: Path, realm_path: Path) -> None:
        """Overwrite client.realm with the collections from src_osdb.

        Same -s rationale as export. The input is a .osdb so there's no
        double-realm-open risk, but we keep it consistent and avoid
        loading the realm via -l (which would race with our own -o write).
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(src_osdb),
                "-o", str(realm_path),
                "-s"]
        self._run(argv)

    def convert_osdb_to_db(self, src_osdb: Path, dest_db: Path) -> None:
        """Convert an .osdb to osu! stable's native collection.db format.

        Same `cm convert` command as the others — output format is inferred
        from the .db extension on -o. The .db output is accepted by
        osu!collector.com, osu! stable, and osu!lazer.
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(src_osdb),
                "-o", str(dest_db),
                "-s"]
        self._run(argv)

    def probe_imported_beatmaps(self, realm_path: Path,
                                beatmap_ids: list[int],
                                hashes: list[str] | None = None) -> ProbeResult:
        """Ask CM CLI which beatmaps lazer's BeatmapInfo DB already has.

        Prefers matching by **md5 hash** (`create -h <file> -l <realm>`) over
        beatmap id (`-b`), because lazer imports from mirror .osz files often
        land with OnlineID=-1 (it couldn't verify them online) — their id won't
        match the collection's, but their md5 is stored correctly. CM loads
        lazer's DB (via -l) and enriches each entry it recognizes with full
        metadata; unrecognized ones come back as `Unknown` / id 0. So an entry
        counts as "imported" if it has a real online id OR real metadata.

        The query file + probe.osdb live in the realm's parent .oc-gui-tmp/ dir
        — the same wine-sandbox-safe convention the merge step uses.

        Fail-open: any error returns an empty ProbeResult so the caller falls
        through to downloading everything.
        """
        hashes = [h for h in (hashes or []) if h]
        beatmap_ids = [b for b in (beatmap_ids or []) if b]
        if not hashes and not beatmap_ids:
            return ProbeResult()

        tmp_dir = realm_path.parent / ".oc-gui-tmp"
        tmp_dir.mkdir(exist_ok=True)
        query_file = tmp_dir / "probe-query.txt"
        probe_osdb = tmp_dir / "probe.osdb"

        # Snapshot the realm into a probe-only subdir so CM CLI doesn't
        # contend with a running osu!lazer for client.realm's lock. CM
        # CLI -l expects a directory containing a file literally named
        # "client.realm", so the snapshot must keep that filename. Realm
        # is MVCC — a file-level copy is a consistent point-in-time view.
        probe_realm_dir = tmp_dir / "probe-realm"
        probe_realm_dir.mkdir(exist_ok=True)
        probe_realm = probe_realm_dir / "client.realm"

        try:
            try:
                shutil.copy2(realm_path, probe_realm)
            except OSError:
                # Snapshot failed (disk full, perms, etc.) — fail open
                # so the caller proceeds to a full download.
                return ProbeResult()

            # CM CLI rejects -b and -h in the same call, and the two catch
            # DIFFERENT maps: hash matches maps lazer imported with OnlineID=-1
            # (mirror imports it couldn't verify), id matches verified maps
            # whose on-disk file differs from the collection's reference md5.
            # So run both against the one snapshot and union the results. Both
            # flags accept a file path (dodges the command-line length limit).
            resolved: dict[int, BeatmapInfo] = {}
            resolved_hashes: set[str] = set()

            def _run_query(flag: str, values: list) -> None:
                try:
                    probe_osdb.unlink(missing_ok=True)
                except OSError:
                    pass
                query_file.write_text("\n".join(str(v) for v in values))
                self._run([*self.cfg.command, "create",
                           flag, str(query_file),
                           "-o", str(probe_osdb),
                           "-l", str(probe_realm_dir)])
                if not probe_osdb.exists() or probe_osdb.stat().st_size == 0:
                    return
                for c in OsdbReader.read(probe_osdb):
                    for bm in c.beatmaps:
                        artist = (getattr(bm, "artist", "") or "").strip().lower()
                        # Recognized = real online id OR real metadata (the
                        # latter catches OnlineID=-1 maps matched by hash).
                        if not (bm.beatmap_id > 0 or artist not in ("", "unknown")):
                            continue
                        if bm.beatmap_id > 0:
                            resolved[bm.beatmap_id] = bm
                        if getattr(bm, "md5", ""):
                            resolved_hashes.add(bm.md5)

            if hashes:
                _run_query("-h", hashes)
            if beatmap_ids:
                _run_query("-b", beatmap_ids)
            return ProbeResult(resolved=resolved, resolved_hashes=resolved_hashes)
        except Exception:
            return ProbeResult()
        finally:
            for p in (probe_osdb, probe_realm):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                probe_realm_dir.rmdir()
            except OSError:
                pass


    DEBUG_LOG = Path("/tmp/oc-cm-cli-debug.log") if sys.platform != "win32" \
        else Path(os.environ.get("TEMP", ".")) / "oc-cm-cli-debug.log"

    @classmethod
    def _run(cls, argv: list[str]) -> None:
        # Always dump the full invocation + output to a debug log so we
        # can actually see what CM CLI did, even when the GUI's error
        # dialog truncates a multi-thousand-line wine register dump.
        with cls.DEBUG_LOG.open("a", encoding="utf-8") as dlog:
            dlog.write("\n" + "=" * 70 + "\n")
            dlog.write(f"timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            dlog.write(f"argv: {shlex.join(argv)}\n")
            dlog.flush()

            run_kwargs: dict = {}
            if sys.platform == "win32":
                # CM CLI is a console app; in our --windowed build that would
                # pop a console window for every invocation. CREATE_NO_WINDOW
                # keeps it hidden.
                run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=600,
                # Use a proper /dev/null for stdin so .NET doesn't
                # block trying to read from a tty it doesn't have.
                stdin=subprocess.DEVNULL,
                **run_kwargs,
            )
            dlog.write(f"exit: {proc.returncode}\n")
            dlog.write(f"--- stdout ---\n{proc.stdout}\n")
            dlog.write(f"--- stderr ---\n{proc.stderr}\n")
            dlog.flush()

        if proc.returncode != 0:
            # Trim wine's gigantic register dump out of the user-facing
            # error — keep the human-readable preamble + a pointer to
            # the debug log.
            stderr = proc.stderr or ""
            for marker in ("Unhandled exception:", "Register dump:", "Backtrace:"):
                idx = stderr.find(marker)
                if idx > 0:
                    stderr = stderr[:idx].rstrip()
                    break
            raise RuntimeError(
                f"CM CLI failed (exit {proc.returncode}):\n"
                f"  cmd: {shlex.join(argv)}\n"
                f"  stdout: {proc.stdout.strip()}\n"
                f"  stderr: {stderr.strip()}\n"
                f"\nFull invocation logged to {cls.DEBUG_LOG}"
            )

    @staticmethod
    def autodetect() -> CmCliConfig | None:
        """Best-effort auto-detection of CM CLI on Linux/Windows.

        Search order:
            1. Standard install locations (Squirrel installer, Program Files,
               wine flatpak prefix)
            2. The cache dir populated by CmCliInstaller (auto-downloaded
               from CM's GitHub releases)
        """
        if sys.platform == "win32":
            home = Path.home()
            # Highest priority: the copy the installer bundles next to our own
            # .exe ({app}\cm-cli\). Lets the app work out of the box with no
            # separate Collection Manager download.
            bundled = (Path(sys.executable).resolve().parent
                       / "cm-cli" / "CollectionManager.App.Cli.exe")
            candidates = [
                bundled,
                home / "AppData/Local/Programs/Collection Manager/CollectionManager.App.Cli.exe",
                Path("C:/Program Files/Collection Manager/CollectionManager.App.Cli.exe"),
                CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe",
            ]
            for p in candidates:
                if p.exists():
                    return CmCliConfig(command=[str(p)], osu_location=None)
            return None

        # Linux: try the wine flatpak install first (the most common
        # setup), then the auto-downloaded cache running through wine.
        wine_exe = (
            Path.home()
            / ".var/app/org.winehq.Wine/data/wine/drive_c/users"
            / os.environ.get("USER", "red")
            / "AppData/Local/Programs/Collection Manager/CollectionManager.App.Cli.exe"
        )
        if wine_exe.exists():
            win_path = (
                "C:\\users\\"
                + os.environ.get("USER", "red")
                + "\\AppData\\Local\\Programs\\Collection Manager"
                + "\\CollectionManager.App.Cli.exe"
            )
            return CmCliConfig(
                command=["flatpak", "run", "org.winehq.Wine", win_path],
                osu_location=None,
            )

        # Auto-downloaded copy in our cache dir, run via wine flatpak
        # (wine needs filesystem permission for the cache dir — we grant
        # it once during install).
        cached = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        if cached.exists() and shutil.which("flatpak"):
            # Wine flatpak prefers Z: drive paths for unix files.
            wine_path = "Z:" + str(cached).replace("/", "\\")
            return CmCliConfig(
                command=["flatpak", "run", "org.winehq.Wine", wine_path],
                osu_location=None,
            )

        # Last-ditch: native build on a system that has one.
        for p in (Path("/usr/local/bin/CollectionManager.App.Cli"),
                  Path("/usr/bin/CollectionManager.App.Cli")):
            if p.exists():
                return CmCliConfig(command=[str(p)], osu_location=None)
        return None


# ---------------------------------------------------------------------------
# Auto-installer for Collection Manager CLI
# ---------------------------------------------------------------------------

class CmCliInstaller:
    """Downloads CM CLI from its GitHub releases into our cache dir.

    The release zip is small (~3.6 MB) and self-contained — a single
    .exe + a native realm-wrappers.dll. Works natively on Windows and
    via wine on Linux.
    """

    @staticmethod
    def installed_exe() -> Path | None:
        exe = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        return exe if exe.exists() else None

    @staticmethod
    def install(log_func=print) -> Path:
        """Download + extract latest CM CLI release. Returns the .exe path."""
        import urllib.request
        import zipfile

        CM_CLI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log_func(f"Downloading {CM_CLI_RELEASE_URL}...")

        req = urllib.request.Request(
            CM_CLI_RELEASE_URL, headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        log_func(f"Downloaded {len(data) // 1024} KiB, extracting...")

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(CM_CLI_CACHE_DIR)

        exe = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        if not exe.exists():
            raise RuntimeError(
                "CM CLI zip extracted but expected exe not found at "
                f"{exe}. The release archive layout may have changed."
            )

        # On Linux, grant the wine flatpak permission to read the cache
        # directory so it can actually find the exe. This is a no-op if
        # the override is already set, and harmless if wine isn't installed.
        if sys.platform != "win32" and shutil.which("flatpak"):
            try:
                subprocess.run(
                    ["flatpak", "override", "--user",
                     f"--filesystem={CM_CLI_CACHE_DIR}",
                     "org.winehq.Wine"],
                    check=False, capture_output=True, timeout=10,
                )
                log_func(f"Granted wine flatpak access to {CM_CLI_CACHE_DIR}")
            except (OSError, subprocess.SubprocessError):
                pass

        log_func(f"Installed CM CLI: {exe}")
        return exe

    @classmethod
    def provision(cls, log_func=print) -> Path:
        """One-click setup of everything Collection Manager needs.

        Windows: just download the CLI. Linux: install the WineHQ flatpak,
        drop the .NET 9 runtime into its prefix, and grant it access to the
        osu! data + CM cache — the same steps as scripts/setup-linux.sh, but
        driven from the app so there's no terminal to open.
        """
        if not sys.platform.startswith("linux"):
            return cls.install(log_func)

        import json as _json
        import urllib.request
        import zipfile

        if not shutil.which("flatpak"):
            raise RuntimeError(
                "flatpak isn't installed. Install it with your package "
                "manager first (Arch: sudo pacman -S flatpak · Debian/Ubuntu: "
                "sudo apt install flatpak), then click Install again.")

        app_id = "org.winehq.Wine"
        home = Path.home()
        prefix = home / ".var/app" / app_id / "data/wine"
        osu_data = _default_lazer_realm_path().parent

        def run(cmd, timeout=None, check=True):
            log_func("$ " + " ".join(str(c) for c in cmd))
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            if check and p.returncode != 0:
                raise RuntimeError(
                    f"'{' '.join(cmd[:3])} …' failed ({p.returncode}): "
                    f"{(p.stderr or p.stdout).strip()[:400]}")
            return p

        remotes = subprocess.run(["flatpak", "remotes"],
                                 capture_output=True, text=True)
        if "flathub" not in (remotes.stdout or ""):
            run(["flatpak", "remote-add", "--if-not-exists", "--user",
                 "flathub", "https://flathub.org/repo/flathub.flatpakrepo"])

        log_func("Installing the WineHQ flatpak (this can take a few minutes)…")
        branch = "stable-25.08"
        ls = subprocess.run(
            ["flatpak", "remote-ls", "flathub",
             "--columns=application,branch"], capture_output=True, text=True)
        stables = sorted(
            parts[1] for parts in (ln.split() for ln in (ls.stdout or "").splitlines())
            if len(parts) >= 2 and parts[0] == app_id and parts[1].startswith("stable-"))
        if stables:
            branch = stables[-1]
        log_func(f"  wine branch: {branch}")
        run(["flatpak", "install", "-y", "--user", "--noninteractive",
             "flathub", f"{app_id}//{branch}"], timeout=2400)

        log_func("Initialising the wine prefix…")
        run(["flatpak", "run", app_id, "wineboot", "-u"],
            timeout=300, check=False)

        log_func("Installing the .NET 9 runtime into the wine prefix…")
        dotnet_dir = prefix / "drive_c/Program Files/dotnet"
        dotnet_dir.mkdir(parents=True, exist_ok=True)
        meta_url = ("https://builds.dotnet.microsoft.com/dotnet/"
                    "release-metadata/9.0/releases.json")
        with urllib.request.urlopen(meta_url, timeout=60) as r:
            rel = _json.load(r)["releases"][0]
        for section in ("runtime", "windowsdesktop"):
            url = next((f["url"] for f in rel[section]["files"]
                        if f.get("rid") == "win-x64"
                        and f["name"].endswith(".zip")), None)
            if not url:
                raise RuntimeError(f".NET {section} win-x64 zip not in metadata")
            log_func(f"  + {section}: {url.rsplit('/', 1)[-1]}")
            with urllib.request.urlopen(url, timeout=300) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(dotnet_dir)

        log_func("Granting wine access to your osu! data…")
        run(["flatpak", "override", "--user",
             f"--filesystem={osu_data}", app_id], check=False)

        log_func("Downloading the Collection Manager CLI…")
        exe = cls.install(log_func)
        log_func("✓ Collection Manager is ready.")
        return exe


# ---------------------------------------------------------------------------
# osu!lazer auto-importer (cross-platform)
# ---------------------------------------------------------------------------

class OsuLazerImporter:
    """Detect and feed files to a running osu!lazer instance."""

    def __init__(self, binary_override: str | Path | None = None) -> None:
        if binary_override:
            p = Path(binary_override).expanduser()
            self.binary: Path | None = p if p.exists() else None
        else:
            self.binary = self._locate_binary()

    @staticmethod
    def _locate_binary() -> Path | None:
        """Find an osu!lazer executable on disk (best-effort)."""
        candidates: list[Path] = []
        home = Path.home()

        if sys.platform.startswith("linux"):
            # AppImage in common locations
            for d in [home / "Applications", home / "Downloads",
                      home / "bin", home / ".local/bin"]:
                candidates.extend(sorted(d.glob("osu*.AppImage")))
            fixed = [
                Path("/var/lib/flatpak/exports/bin/sh.ppy.osu"),
                home / ".local/share/flatpak/exports/bin/sh.ppy.osu",
                Path("/usr/bin/osu-lazer"),
                Path("/usr/local/bin/osu-lazer"),
                Path("/opt/osu-lazer-bin/osu-lazer"),  # AUR osu-lazer-bin
            ]
            # Anything named osu-lazer / osu! on PATH (covers custom installs).
            for name in ("osu-lazer", "osu!"):
                w = shutil.which(name)
                if w:
                    fixed.append(Path(w))
            for p in fixed:
                if p.exists() and p not in candidates:
                    candidates.append(p)
        elif sys.platform == "win32":
            base = home / "AppData/Local/osulazer"
            # Current osu!lazer ships with Velopack, which keeps the live
            # exe in a "current" subfolder (swapped atomically on update).
            # This is THE common case on any recent install — check first.
            p = base / "current" / "osu!.exe"
            if p.exists():
                candidates.append(p)
            # Legacy Squirrel.Windows installs dropped it under an
            # "app-X.Y.Z" subfolder instead.
            for ver in sorted(base.glob("app-*"), reverse=True):
                p = ver / "osu!.exe"
                if p.exists():
                    candidates.append(p)
            # Direct top-level fallback (rare).
            for p in [
                base / "osu!.exe",
                home / "AppData/Local/Programs/osulazer/osu!.exe",
                Path("C:/Program Files/osulazer/osu!.exe"),
                Path("C:/Program Files (x86)/osulazer/osu!.exe"),
            ]:
                if p.exists():
                    candidates.append(p)
        elif sys.platform == "darwin":
            for p in [
                Path("/Applications/osu!.app/Contents/MacOS/osu!"),
                home / "Applications/osu!.app/Contents/MacOS/osu!",
            ]:
                if p.exists():
                    candidates.append(p)

        return candidates[0] if candidates else None

    def is_running(self) -> bool:
        try:
            import psutil
        except ImportError:
            return False
        for p in psutil.process_iter(attrs=["name"]):
            try:
                name = (p.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "osu!" in name or "osu.exe" in name or name.startswith("osu_"):
                return True
        return False

    def import_file(self, osz_path: Path) -> bool:
        """Hand a single file to osu!lazer (kept for callers that want one)."""
        return self.import_files([osz_path]) > 0

    def import_files(self, paths: list[Path]) -> int:
        """Hand many files to osu!lazer in as few launches as possible.

        osu!lazer imports every file passed to one launch as a single batch
        ("importing 1 of N…"), so this drags-and-drops the whole set at once
        instead of spawning a process per map — which on a Linux AppImage is
        a FUSE-mount + engine cold-start each time (and rival instances fight
        over client.realm). Chunked to stay under the OS command-line length
        limit (small on Windows). Returns how many files were dispatched.
        """
        if not self.binary or not self.binary.exists():
            return 0
        files = [str(p) for p in paths if p]
        if not files:
            return 0
        # Command-line byte budget: ~30k on Windows, ~1.5 MB on Linux/macOS
        # (well under ARG_MAX). Every realistic collection fits in ONE launch
        # — which is the whole point: firing several osu processes at once
        # makes them cold-start and race to forward, so nothing imports. When
        # a split IS unavoidable (enormous collection), WAIT for each launch
        # to exit before the next so they never overlap.
        budget = 28_000 if sys.platform == "win32" else 1_500_000
        dispatched = 0
        i = 0
        while i < len(files):
            j, size = i, 0
            while j < len(files) and (j == i or size + len(files[j]) + 1 < budget):
                size += len(files[j]) + 1
                j += 1
            batch = files[i:j]
            proc = self._launch_with_files(batch)
            if proc is None:
                break
            dispatched += len(batch)
            i = j
            if i < len(files):
                # Another chunk follows — let this launch finish forwarding
                # (or, if it became the primary instance, cap the wait).
                try:
                    proc.wait(timeout=90)
                except Exception:
                    pass
        return dispatched

    def _launch_with_files(self, files: list[str]):
        """Launch osu!lazer with these files. Returns the Popen (so the
        caller can wait for it) or None if it couldn't start."""
        try:
            kwargs: dict = dict(stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                env=_child_env())
            if sys.platform == "win32":
                # DETACHED_PROCESS so the call doesn't block or pop a console.
                kwargs["creationflags"] = 0x00000008
                argv = [str(self.binary), *files]
            else:
                kwargs["start_new_session"] = True
                if self.binary.name == "sh.ppy.osu":
                    # Flatpak lazer can't see the download folder from inside
                    # its sandbox, so plain-path imports silently fail.
                    # flatpak file-forwarding (@@ … @@) mounts the files in.
                    argv = ["flatpak", "run", "--file-forwarding",
                            "sh.ppy.osu", "@@", *files, "@@"]
                else:
                    argv = [str(self.binary), *files]
            return subprocess.Popen(argv, **kwargs)
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Download worker (background thread)
# ---------------------------------------------------------------------------

@dataclass
class DownloadJob:
    collection_ids: list[int]
    output_dir: Path
    download_beatmaps: bool = True
    generate_osdb: bool = False
    auto_import: bool = True
    osu_binary: str | None = None       # manual override; "" / None = auto
    import_parallel: int = 1            # 1..8 — concurrent import calls
    import_delay_ms: int = 0            # min delay between import calls
    mirror_url: str = DEFAULT_MIRROR
    extra_mirrors: list[str] = field(default_factory=list)  # user templates
    skip_video: bool = True             # prefer no-video .osz where supported
    # Lazer collection merging via CM CLI
    add_to_lazer_collections: bool = False
    cm_cli_command: list[str] | None = None  # full argv prefix
    lazer_realm_path: str | None = None      # path to client.realm
    target_collection_name: str | None = None  # if set, all maps go here
    restart_lazer_after: bool = False
    # Post-import housekeeping
    cleanup_after_import: bool = False       # delete <id> - <name>/ folders
    # Dedup
    skip_already_imported: bool = True        # probe lazer + skip its sets
    # Tuning
    download_parallel: int = DOWNLOAD_PARALLEL  # worker threads; per-mirror
                                                # adaptive caps govern the rest


class Downloader:
    """Performs the actual downloads on a background thread."""

    # ---- event emit helpers (these replace the old pyqtSignals) ----
    def _log(self, line: str) -> None:
        self._emit("log", {"line": line})

    def _error(self, message: str) -> None:
        self._emit("error", {"message": message})

    def _beatmap_progress(self, current: int, total: int) -> None:
        self._emit("beatmap_progress", {"current": current, "total": total})

    def _collection_started(self, idx: int, total: int, name: str,
                            n_sets: int) -> None:
        self._emit("collection_started",
                   {"idx": idx, "total": total, "name": name, "n_sets": n_sets})

    def _collection_finished(self, idx: int, ok: int, total: int) -> None:
        self._emit("collection_finished",
                   {"idx": idx, "ok": ok, "total": total})

    def _prep(self, detail: str, title: str | None = None) -> None:
        """Surface pre-download progress (fetching the beatmap list, probing the
        library) so the 'Starting…' phase isn't an opaque wait on big collections."""
        self._emit("prep", {"detail": detail, "title": title})

    def _awaiting_import(self, n: int) -> None:
        self._emit("awaiting_import_confirmation", {"n": n})

    def _batch_finished(self, ok: int, total: int) -> None:
        self._emit("batch_finished", {"ok": ok, "total": total})

    def __init__(self, job: DownloadJob, emit) -> None:
        self._emit = emit
        self.job = job
        self._cancelled = False
        self.api = OsuCollectorClient()
        self.mirror = BeatmapMirror(primary=job.mirror_url,
                                    extra=job.extra_mirrors,
                                    no_video=job.skip_video)
        # Always construct the importer so we know the lazer binary path
        # for the post-merge restart, even if auto_import is off. The
        # _maybe_import path checks job.auto_import before actually
        # invoking it, so this is purely about knowing the binary.
        self.importer = OsuLazerImporter(binary_override=job.osu_binary)
        import threading as _t
        # Downloaded files are collected here and handed to osu!lazer in one
        # batch when the run finishes (see _flush_imports) — lazer treats a
        # multi-file launch as a single "importing 1 of N" batch.
        self._imported_paths: list[Path] = []
        self._import_calls_issued = 0
        # Event used to pause the worker before the destructive merge so
        # the user can confirm osu!lazer has finished its async import
        # queue. Set by confirm_merge_continue() from the GUI thread.
        self._continue_merge_event = _t.Event()
        # Set by _lazer_kill_if_running so the relaunch step can use the
        # exact same binary the user had running, even if our standard
        # search paths wouldn't find it.
        self._discovered_lazer_exe: Path | None = None
        # .osdb files we wrote during this run, so the merge step picks
        # up only the new ones — not stale .osdb files left over from
        # previous batches in the same output directory.
        self._generated_osdb_files: list[Path] = []

    def cancel(self) -> None:
        self._cancelled = True
        # Unblock any thread waiting on the merge confirmation gate so
        # it can notice the cancel and exit cleanly.
        self._continue_merge_event.set()

    def _probe_enabled_for_job(self) -> bool:
        """All gates that must be true for the probe step to run.

        Note: this is NOT tied to merging into a collection. Skipping maps you
        already have in osu!lazer is useful for any download (incl. a plain one
        or a re-run), so it only needs the toggle + a readable realm + CM CLI.
        """
        return bool(
            self.job.skip_already_imported
            and self.job.cm_cli_command
            and self.job.lazer_realm_path
            and Path(self.job.lazer_realm_path).expanduser().exists()
        )

    def _should_generate_osdb(self) -> bool:
        """We must write .osdb files when the user asked to export them OR
        whenever we're merging into a lazer collection — _merge_into_lazer()
        consumes the .osdb generated this run, so skipping generation here
        makes the merge silently no-op (the chosen collection never appears)."""
        return bool(self.job.generate_osdb or self.job.add_to_lazer_collections)

    def _should_fetch_details(self) -> bool:
        """Per-beatmap details (id + md5) are needed by .osdb generation,
        the lazer collection merge, and the dedup probe."""
        return bool(
            self.job.generate_osdb
            or self.job.add_to_lazer_collections
            or self._probe_enabled_for_job()
        )

    def confirm_merge_continue(self) -> None:
        """Called from the GUI thread when the user clicks OK on the
        'did osu!lazer finish importing?' dialog. Releases the worker
        from its wait at the start of _merge_into_lazer."""
        self._continue_merge_event.set()

    # ---- helpers ----------------------------------------------------------

    def _maybe_import(self, path: Path) -> None:
        """Queue a downloaded file for the end-of-run batch import."""
        if not self.job.auto_import or not self.importer.binary:
            return
        self._imported_paths.append(path)

    def _flush_imports(self) -> None:
        """Hand every queued file to osu!lazer at once (chunked). osu!lazer
        imports a multi-file launch as one batch, so this avoids the
        per-map process storm the old code caused on Linux."""
        if not self.job.auto_import or not self.importer.binary:
            return
        if not self._imported_paths:
            return
        n = self.importer.import_files(self._imported_paths)
        self._import_calls_issued = n
        if n:
            self._log(f"[lazer] handed {n} map(s) to osu!lazer to import")

    def _download_one(self, set_id: int, col_dir: Path) -> tuple[int, Path | None, str | None]:
        try:
            path = self.mirror.download(set_id, col_dir,
                                        should_cancel=lambda: self._cancelled)
            return set_id, path, None
        except Exception as e:
            return set_id, None, str(e)

    # ---- main loop --------------------------------------------------------

    def run(self) -> None:
        ok_collections = 0
        total = len(self.job.collection_ids)

        # Make a missing lazer binary LOUD. Without this the auto-import
        # path no-ops silently — maps download fine but never reach the
        # game, with no clue why.
        if self.job.auto_import and not self.importer.binary:
            self._log(
                "[lazer] WARNING: auto-import is ON but no osu!lazer "
                "executable was found. Downloaded maps will NOT be imported "
                "into the game. Set the 'osu!lazer binary' path in the "
                "Advanced section (e.g. Windows: "
                r"%LOCALAPPDATA%\osulazer\current\osu!.exe)."
            )

        for idx, cid in enumerate(self.job.collection_ids, 1):
            if self._cancelled:
                self._log("[cancelled]")
                break

            # The probe and .osdb generation both need per-beatmap details
            # (beatmap_id + md5). Force the detail fetch when ANY feature
            # that consumes them is on — incl. merging into a lazer
            # collection, which reads the .osdb files we generate this run.
            need_details = self._should_fetch_details()
            self._prep(
                "Fetching the collection's beatmap list…",
                title=f"Preparing collection {idx}/{total}…",
            )
            try:
                info = self.api.fetch_collection(
                    cid, with_beatmap_details=need_details,
                    progress=(lambda n: self._prep(
                        f"Fetching beatmap details… {n:,} so far"))
                    if need_details else None,
                    should_cancel=lambda: self._cancelled,
                )
            except Exception as e:
                self._error(f"Collection {cid}: {e}")
                continue

            if self._cancelled:
                self._log("[cancelled]")
                break

            self._log(
                f"\n=== Collection {idx}/{total}: {info.name} "
                f"by {info.uploader} ({len(info.beatmapset_ids)} sets) ==="
            )
            self._collection_started(idx, total, info.name, len(info.beatmapset_ids))

            safe_name = _safe_filename(info.name)
            col_dir = self.job.output_dir / f"{info.id} - {safe_name}"
            col_dir.mkdir(parents=True, exist_ok=True)

            # --- probe lazer for which sets it already has ---
            skipped_set_ids: set[int] = set()
            probe_md5_map: dict[int, str] = {}
            if self._probe_enabled_for_job() and info.beatmaps and not self._cancelled:
                try:
                    self._prep(
                        f"Checking your osu!lazer library "
                        f"({len(info.beatmaps):,} maps)…"
                    )
                    self._log(
                        f"  [probe] checking lazer for {len(info.beatmaps)} maps "
                        "(by hash)..."
                    )
                    cm = CmCliRunner(CmCliConfig(
                        command=list(self.job.cm_cli_command),
                        osu_location=None,
                    ))
                    realm = Path(self.job.lazer_realm_path).expanduser()
                    probe = cm.probe_imported_beatmaps(
                        realm,
                        [b.beatmap_id for b in info.beatmaps if b.beatmap_id],
                        hashes=[b.md5 for b in info.beatmaps if b.md5],
                    )
                    probe_md5_map = {bid: bm.md5 for bid, bm in probe.resolved.items() if bm.md5}
                    # A map is already imported if lazer knows its md5 OR its id.
                    have = lambda b: (b.md5 and b.md5 in probe.resolved_hashes) \
                        or (b.beatmap_id and b.beatmap_id in probe.resolved)
                    matched = sum(1 for b in info.beatmaps if have(b))
                    skipped_set_ids = {b.set_id for b in info.beatmaps
                                       if have(b) and b.set_id}
                    self._log(
                        f"  [probe] lazer has {matched}/{len(info.beatmaps)} maps; "
                        f"skipping {len(skipped_set_ids)}/{len(info.beatmapset_ids)} sets"
                    )
                except Exception as e:
                    # Fail-open: probe failures cost bandwidth, not data.
                    self._log(f"  [probe] failed: {e} — proceeding without dedup")

            ok = 0
            failed = 0
            absent = 0   # 404 on every mirror — genuinely not hosted anywhere
            skipped = 0

            # --- generate .osdb (independent of beatmap downloads) ---
            if self._should_generate_osdb():
                try:
                    osdb_path = col_dir / f"{safe_name}.osdb"
                    OsdbWriter.write(osdb_path, info,
                                     prefer_md5_map=probe_md5_map or None)
                    self._generated_osdb_files.append(osdb_path)
                    self._log(f"  [.osdb] {osdb_path.name}")
                except Exception as e:
                    self._log(f"  [.osdb error] {e}")

            # --- download beatmaps in parallel ---
            if self.job.download_beatmaps and info.beatmapset_ids:
                set_ids = info.beatmapset_ids
                workers = max(1, min(32, self.job.download_parallel))
                failed_ids: list[int] = []   # transient failures, retried below
                done = 0
                # Tick the bar for every set already skipped so progress
                # accurately reflects total work.
                if skipped_set_ids:
                    done = len(skipped_set_ids & set(set_ids))
                    skipped = done
                    self._beatmap_progress(done, len(set_ids))
                    self._log(f"  [skip] {skipped} set(s) already imported in lazer")
                ex = ThreadPoolExecutor(max_workers=workers)
                try:
                    futures = {
                        ex.submit(self._download_one, sid, col_dir): sid
                        for sid in set_ids if sid not in skipped_set_ids
                    }
                    # Poll with a short timeout instead of blocking in
                    # as_completed(): otherwise a Cancel click isn't noticed
                    # until the next download happens to finish, which during
                    # a rate-limit stall can be many seconds — the "cancel
                    # button doesn't work" symptom.
                    pending = set(futures)
                    while pending and not self._cancelled:
                        finished, pending = futures_wait(
                            pending, timeout=0.3, return_when=FIRST_COMPLETED)
                        for fut in finished:
                            if self._cancelled:
                                break
                            done += 1
                            self._beatmap_progress(done, len(set_ids))
                            sid, path, err = fut.result()
                            if err:
                                # Transient — all mirrors errored (rate-limit/
                                # 5xx) within the deadline. Recoverable, so
                                # queue it for the retry pass. DON'T log a red
                                # error per set: on a big collection that's a
                                # wall of scary red for what are really "will
                                # retry" hiccups. Summarised below.
                                failed += 1
                                failed_ids.append(sid)
                                continue
                            if path is None:
                                absent += 1   # 404 everywhere; summarised below
                                continue
                            ok += 1
                            self._log(f"  [{done}/{len(set_ids)}] {path.name}")
                            self._maybe_import(path)
                finally:
                    # On cancel, don't block on in-flight downloads — cancel the
                    # queued futures and return now; each running download sees
                    # should_cancel() and bails at its next chunk.
                    ex.shutdown(wait=not self._cancelled, cancel_futures=True)

                if absent:
                    self._log(f"  [skip] {absent} set(s) not hosted on any mirror")

                # --- one time-boxed retry for rate-limited failures ---
                # Reset the mirror cooldowns from this pass and re-attempt the
                # failed sets, but cap the whole thing at ~20s: whatever hasn't
                # come through by then is skipped (no long waiting). Re-running
                # the collection later grabs the stragglers — and already-
                # downloaded files are skipped without even hitting a mirror.
                if failed_ids and not self._cancelled:
                    retrying = failed_ids
                    failed_ids = []
                    self._log(
                        f"  [retry] re-attempting {len(retrying)} rate-limited "
                        "set(s) for ~20s, then skipping the rest"
                    )
                    self._prep(
                        f"Retrying {len(retrying)} rate-limited map(s) (~20s)…",
                        title=f"Retrying {len(retrying)} rate-limited map(s)…",
                    )
                    BeatmapMirror.reset_state()   # clear this pass's cooldowns
                    recovered = 0
                    rex = ThreadPoolExecutor(max_workers=workers)
                    rfut = [rex.submit(self._download_one, sid, col_dir)
                            for sid in retrying]
                    try:
                        for fut in as_completed(rfut, timeout=20):
                            if self._cancelled:
                                break
                            _sid, path, err = fut.result()
                            if path is not None and not err:
                                recovered += 1
                                ok += 1
                                failed -= 1
                                self._log(f"  [retry-ok] {path.name}")
                                self._maybe_import(path)
                    except FuturesTimeout:
                        pass   # 20s budget elapsed — skip the rest
                    finally:
                        rex.shutdown(wait=False, cancel_futures=True)
                    skipped_now = len(retrying) - recovered
                    if skipped_now > 0:
                        self._log(
                            f"  [skip] {skipped_now} set(s) still rate-limited — "
                            "skipped; re-run the collection later to grab them"
                        )
            else:
                # No beatmap download requested. Still emit progress so the
                # bar finishes.
                self._beatmap_progress(len(info.beatmapset_ids),
                                           max(len(info.beatmapset_ids), 1))

            self._collection_finished(idx, ok, len(info.beatmapset_ids))
            self._log(
                f"=== {info.name}: {ok} ok, {failed} failed, "
                f"{absent} not on mirrors, "
                f"{skipped} skipped (already imported) ==="
            )
            if ok > 0 or skipped > 0 or self.job.generate_osdb:
                ok_collections += 1

        # Batch-import everything we downloaded into osu!lazer in one go —
        # lazer handles a multi-file launch as a single "importing 1 of N"
        # batch instead of a per-map cold-start storm.
        if not self._cancelled:
            self._flush_imports()

        # --- merge into lazer collections via CM CLI ---
        if self.job.add_to_lazer_collections and not self._cancelled:
            try:
                self._merge_into_lazer()
            except Exception as e:
                self._error(f"lazer collection merge failed: {e}")
        elif (self.job.auto_import and self._import_calls_issued > 0
              and self.job.cleanup_after_import and not self._cancelled):
            # No merge, but cleanup is on — confirm imports finished before
            # deleting the source files lazer might still be reading. With no
            # merge AND no cleanup there's nothing to gate, so don't prompt.
            self._continue_merge_event.clear()
            self._awaiting_import(self._import_calls_issued)
            self._continue_merge_event.wait(timeout=3600)

        # --- cleanup per-collection folders ---
        if self.job.cleanup_after_import and not self._cancelled:
            try:
                self._cleanup_collection_folders()
            except Exception as e:
                self._log(f"[cleanup] failed: {e}")

        self._batch_finished(ok_collections, total)

    # ---- post-import cleanup ---------------------------------------------

    def _cleanup_collection_folders(self) -> None:
        """Delete the per-collection download folders, keeping everything
        else (especially anything that looks like a Realm file).

        Pattern matched: top-level dirs in output_dir whose name starts
        with '<digits> - '. That's the convention osu-collector-dl uses
        and what our worker creates. We refuse to delete:
            - the /db subdirectory
            - any file with .realm in its name (incl. backups)
            - anything not a directory
            - paths outside output_dir
        """
        out_dir = Path(self.job.output_dir).resolve()
        if not out_dir.exists() or not out_dir.is_dir():
            return

        deleted = 0
        kept = 0
        for entry in out_dir.iterdir():
            try:
                # Hard safety: only descend into things directly inside
                # out_dir, never follow symlinks pointing elsewhere.
                if entry.is_symlink():
                    kept += 1
                    continue
                if not entry.is_dir():
                    kept += 1
                    continue
                if entry.name == "db":
                    kept += 1
                    continue
                if entry.name.startswith("."):
                    # Hidden temp dirs we manage ourselves (.cm_tmp, etc.)
                    kept += 1
                    continue
                # Match the "<id> - <name>" pattern.
                if not re.match(r"^\d+\s*[-–]\s*", entry.name):
                    kept += 1
                    continue
                # Belt-and-braces: refuse if anything inside has .realm
                # in its name.
                contains_realm = False
                for sub in entry.rglob("*.realm*"):
                    contains_realm = True
                    break
                if contains_realm:
                    self._log(
                        f"[cleanup] SKIP {entry.name}: contains a .realm file"
                    )
                    kept += 1
                    continue

                shutil.rmtree(entry)
                deleted += 1
            except OSError as e:
                self._log(f"[cleanup] couldn't remove {entry.name}: {e}")
                kept += 1

        self._log(
            f"[cleanup] removed {deleted} collection folder(s), kept {kept} other entr(ies)"
        )

    # ---- lazer collection merge ------------------------------------------

    def _merge_into_lazer(self) -> None:
        # Short-circuit if nothing was generated this run (e.g. all
        # collections failed to fetch, or every set was skipped). Without
        # this guard, the expensive snapshot+export through wine runs even
        # when there's nothing to merge — looks like the GUI hangs.
        if not self._generated_osdb_files:
            self._log("[lazer] no new collections generated this run — skipping merge")
            return

        if not self.job.cm_cli_command:
            raise RuntimeError(
                "Collection Manager CLI not configured. Set its path in "
                "the GUI's 'Lazer collections' section."
            )

        # If we issued any auto-import calls, lazer is doing async work
        # in the background — extracting .osz files, hashing beatmaps,
        # writing to client.realm. We must wait until that's done, or
        # the merge will kill lazer mid-import and lose data. Lazer
        # doesn't expose a "queue empty" signal, so we ask the user.
        if self._import_calls_issued > 0:
            self._log(
                f"\n[lazer] {self._import_calls_issued} auto-import call(s) "
                "were issued. Waiting for user confirmation that osu!lazer "
                "has finished importing them before touching client.realm..."
            )
            self._continue_merge_event.clear()
            self._awaiting_import(self._import_calls_issued)
            # Long but bounded wait — 1h cap so a forgotten dialog
            # doesn't leak the worker thread forever.
            self._continue_merge_event.wait(timeout=3600)
            if self._cancelled:
                self._log("[lazer] cancelled while waiting for import "
                              "confirmation")
                return
            self._log("[lazer] user confirmed; proceeding with merge")
        if not self.job.lazer_realm_path:
            raise RuntimeError("lazer client.realm path not configured.")
        realm_path = Path(self.job.lazer_realm_path).expanduser()
        if not realm_path.exists():
            raise FileNotFoundError(f"client.realm not found at {realm_path}")

        cm = CmCliRunner(CmCliConfig(
            command=list(self.job.cm_cli_command),
            osu_location=None,
        ))

        # Read the existing collections via a SNAPSHOT copy, so lazer
        # can stay open for now. Killing lazer is deferred to the
        # write-back phase. (Realm is MVCC — a file-level copy gives
        # us a consistent point-in-time snapshot of committed state.)
        was_running = False  # filled in later, before the write-back

        # Same wine-sandbox constraint as _fetch_existing_collections —
        # CM CLI can only write to paths the wine flatpak can see, which
        # in practice means the realm's own parent dir.
        self._log("\n[lazer] snapshotting realm and exporting existing collections...")
        tmp_dir = realm_path.parent / ".oc-gui-tmp"
        tmp_dir.mkdir(exist_ok=True)
        snapshot_realm = tmp_dir / "snapshot.realm"
        existing_osdb = tmp_dir / "existing.osdb"

        try:
            shutil.copy2(realm_path, snapshot_realm)
        except OSError as e:
            raise RuntimeError(
                f"Couldn't snapshot client.realm to {snapshot_realm}: {e}"
            ) from e

        # FAIL-CLOSED: if we can't read the existing collections, ABORT.
        # Treating "couldn't read" as "you have no collections" would
        # cause CM CLI's destructive Write to nuke the realm — exactly
        # what happened in v0.4.0. Refuse to proceed instead.
        try:
            cm.export_realm_to_osdb(snapshot_realm, existing_osdb)
        except Exception as e:
            raise RuntimeError(
                "CM CLI failed to export your existing lazer collections "
                "from client.realm. Aborting before any destructive write — "
                "your collections are untouched.\n\nUnderlying error: "
                f"{e}"
            ) from e

        if not existing_osdb.exists() or existing_osdb.stat().st_size == 0:
            raise RuntimeError(
                "CM CLI exported a 0-byte or missing file when reading "
                "your existing lazer collections. Aborting before any "
                "destructive write — your collections are untouched.\n\n"
                f"Expected file: {existing_osdb}"
            )

        try:
            existing = OsdbReader.read(existing_osdb)
        except Exception as e:
            raise RuntimeError(
                "Couldn't parse the .osdb that CM CLI exported from your "
                "realm. Aborting before any destructive write — your "
                "collections are untouched.\n\nParser error: "
                f"{e}\nFile: {existing_osdb} ({existing_osdb.stat().st_size} bytes)"
            ) from e

        self._log(f"[lazer] {len(existing)} existing collection(s) loaded")

        # Belt-and-braces sanity check: if the realm is non-trivial in size
        # but we got back zero collections, something is wrong — refuse to
        # write rather than risk wiping a collection database that just
        # happened to parse weirdly.
        realm_size = realm_path.stat().st_size
        if not existing and realm_size > 1024 * 1024:
            raise RuntimeError(
                f"client.realm is {realm_size // 1024 // 1024} MB but the "
                "export contained zero collections. This usually means the "
                "export tool is incompatible with your lazer/realm version. "
                "Aborting before any destructive write — your collections "
                "are untouched."
            )

        # Collect ONLY .osdb files we generated this run. Previously we
        # rglob'd output_dir which picked up stale .osdb files from
        # earlier batches and silently merged them, producing way more
        # collections than the user actually downloaded.
        new_collections: list[CollectionInfo] = []
        for f in self._generated_osdb_files:
            if not f.exists():
                self._log(f"[lazer] WARN: generated {f.name} is missing")
                continue
            try:
                new_collections.extend(OsdbReader.read(f))
            except Exception as e:
                self._log(f"[lazer] skip unreadable {f.name}: {e}")

        if not new_collections:
            self._log("[lazer] no new collections to merge — skipping")
            return

        # If the user picked a single target collection name, rewrite ALL
        # the new collections to use that name. The merge step will then
        # combine them (and any same-named existing one) into a single
        # collection.
        if self.job.target_collection_name:
            target = self.job.target_collection_name
            self._log(
                f"[lazer] funneling all new maps into collection {target!r}"
            )
            for c in new_collections:
                c.name = target

        merged = merge_collection_lists(
            existing, new_collections,
            on_name_collision="merge",
        )
        self._log(
            f"[lazer] merged result: {len(merged)} collection(s) "
            f"(existing {len(existing)} + new {len(new_collections)} → {len(merged)})"
        )

        merged_osdb = tmp_dir / "merged.osdb"
        OsdbWriter.write_many(merged_osdb, merged, editor="osu-collector-gui")

        # Backup the realm before overwriting — paranoia is justified here.
        backup = realm_path.with_suffix(
            realm_path.suffix + f".bak-{int(time.time())}"
        )
        try:
            shutil.copy2(realm_path, backup)
            self._log(f"[lazer] backed up realm to {backup.name}")
        except OSError as e:
            self._log(f"[lazer] WARNING: couldn't back up realm: {e}")

        # Lazer must NOT be running while CM rewrites client.realm. We
        # already killed it at the start of this method; this second
        # check catches any auto-restart that may have happened in the
        # meantime (e.g. some launchers respawn it).
        if self._lazer_kill_if_running():
            was_running = True
        try:
            self._log("[lazer] writing merged collections back to realm...")
            cm.import_osdb_to_realm(merged_osdb, realm_path)
            self._log("[lazer] done.")
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except OSError:
                pass

        if self.job.restart_lazer_after or was_running:
            self._lazer_relaunch()

    def _lazer_kill_if_running(self) -> bool:
        try:
            import psutil
        except ImportError:
            return False

        targets: list = []
        for p in psutil.process_iter(attrs=["name", "exe", "cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if ("osu!" in name or "osu.exe" in name
                    or name.startswith("osu_")
                    or "osu!.exe" in exe):
                targets.append(p)

        if not targets:
            return False

        # Snapshot the exe path of the process we're about to kill so we
        # can relaunch the SAME binary later — no guessing needed.
        # AppImages are special: psutil.exe returns /tmp/.mount_<hash>/...
        # which is unmounted the moment the AppImage exits. We need the
        # original .AppImage path. Order of preference:
        #   1. $APPIMAGE env var (set by every AppImage runtime)
        #   2. cmdline[0] if it's a .AppImage file
        #   3. /proc/PID/exe (works for non-AppImage installs)
        for p in targets:
            try:
                discovered: Path | None = None

                # 1. APPIMAGE env var — the canonical path
                try:
                    env = p.environ()
                    appimage = env.get("APPIMAGE")
                    if appimage and Path(appimage).exists():
                        discovered = Path(appimage)
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        FileNotFoundError, OSError):
                    pass

                # 2. cmdline[0] — what the user actually invoked
                if discovered is None:
                    cmdline = p.info.get("cmdline") or []
                    if cmdline:
                        c0 = Path(cmdline[0]).expanduser()
                        if c0.exists() and (
                            c0.suffix.lower() == ".appimage"
                            or "osu" in c0.name.lower()
                        ):
                            discovered = c0

                # 3. exe symlink — only if it points somewhere persistent
                if discovered is None:
                    exe = p.info.get("exe")
                    if exe and Path(exe).exists() and "/.mount_" not in exe:
                        discovered = Path(exe)

                if discovered is not None:
                    self._discovered_lazer_exe = discovered
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        self._log(
            f"[lazer] terminating {len(targets)} osu!lazer process(es) "
            "and waiting for them to exit cleanly..."
        )
        # SIGTERM first — lazer's signal handler flushes the Realm and
        # closes its file handles. Skipping the wait or going straight
        # to SIGKILL leaves the realm in a half-flushed state that
        # Realm.NET refuses to re-open under wine, crashing CM CLI with
        # 0xe0434352.
        for p in targets:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Wait up to 15 s for graceful shutdown.
        gone, alive = psutil.wait_procs(targets, timeout=15)
        for p in alive:
            self._log(f"[lazer] PID {p.pid} ignored SIGTERM, sending SIGKILL")
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=5)

        # Extra grace period so the kernel releases the file handles
        # and any Realm .lock entries are visible-as-stale to the next
        # opener.
        time.sleep(2)

        # Clean up obviously-stale Realm lock + management files. Lazer
        # leaves these behind when it exits cleanly too, but a hard
        # SIGKILL after a hung shutdown can produce a corrupt .lock that
        # Realm.NET refuses. Removing them is safe — Realm will recreate
        # on next open.
        try:
            realm_path = Path(self.job.lazer_realm_path).expanduser()
            for stale in [
                realm_path.parent / "client.realm.lock",
                realm_path.parent / "client.realm.note",
            ]:
                if stale.exists():
                    try:
                        stale.unlink()
                        self._log(f"[lazer] removed stale {stale.name}")
                    except OSError:
                        pass
        except (TypeError, OSError):
            pass

        self._log("[lazer] all instances stopped")
        return True

    def _lazer_relaunch(self) -> None:
        # Prefer the exe path we snapshotted from the running process —
        # that's guaranteed to be the user's actual binary, no
        # heuristics needed.
        binary = self._discovered_lazer_exe or self.importer.binary
        if not binary:
            self._log(
                "[lazer] no binary path discovered or configured — "
                "skipping relaunch. Set 'osu!lazer binary' in the Tuning "
                "section if you want auto-restart to work."
            )
            return
        self._log(f"[lazer] relaunching {binary}")
        # Save the actual binary into the importer for any subsequent
        # operations in this run.
        self.importer.binary = binary
        try:
            kwargs: dict = dict(stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                env=_child_env())
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([str(self.importer.binary)], **kwargs)
            self._log("[lazer] relaunched osu!lazer")
        except OSError as e:
            self._log(f"[lazer] relaunch failed: {e}")


# ---------------------------------------------------------------------------
# UI helpers (pure functions, unit-testable without Qt)
# ---------------------------------------------------------------------------

def should_enable_start(collection_ids_text: str) -> bool:
    """The Start button is enabled iff at least one non-whitespace character
    appears in the collection-IDs field. The actual ID parsing happens at
    submit time via _parse_ids."""
    return bool(collection_ids_text.strip())


def target_combo_default_label() -> str:
    """The default sentinel item in the 'Add to' picker. Picking this
    preserves v0.6.x's behavior: one lazer collection per osu!collector
    collection, named after the collection."""
    return "(one collection per osu!collector collection)"


def target_combo_no_merge_label() -> str:
    """The sentinel item that disables lazer collection merge entirely.
    Files still download (and may still auto-import into lazer) but no
    realm modification happens."""
    return "Don't merge"


# ---------------------------------------------------------------------------
# Web UI bridge (pywebview) + entry point
# ---------------------------------------------------------------------------
#
# The Qt UI was retired in v1.0.0 in favour of an HTML/CSS/JS frontend (the
# "Cherry" design system) rendered in a native pywebview window. The proven
# download engine above is unchanged; this layer just wires the frontend to
# it. Frontend → Python calls land on JsApi's public methods (exposed by
# pywebview as window.pywebview.api.*). Python → frontend events are pushed
# via window.evaluate_js("window.ocOnEvent({...})").

def _web_dir() -> Path:
    """Locate the bundled frontend, working both from source and from a
    PyInstaller one-file build (which unpacks data under sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "web"
    return Path(__file__).resolve().parent / "web"


WEB_DIR = _web_dir()

# Module-level picker sentinels (these used to live on MainWindow).
DEFAULT_TARGET = target_combo_default_label()
NEW_TARGET = "+ Create new collection..."


def _load_settings() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def _normalize_cm(value) -> list[str]:
    """Coerce a CM CLI command (str from the UI, or list from disk) to argv."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    s = str(value).strip()
    if not s:
        return []
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def _parse_ids(text: str) -> list[int]:
    """Extract osu!collector collection IDs from free-form text.

    Accepts bare numeric IDs and full /collections/<id> URLs, separated by
    newlines, commas, or whitespace. Order-preserving and de-duplicated so a
    user pasting the same link twice doesn't download it twice.
    """
    ids: list[int] = []
    for token in re.split(r"[\s,]+", text or ""):
        token = token.strip()
        if not token:
            continue
        m = re.search(r"/collections/(\d+)", token)
        if m:
            ids.append(int(m.group(1)))
        elif token.isdigit():
            ids.append(int(token))
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _autodetect_paths() -> dict:
    """Best-effort detection of the three integration paths so the user
    never has to configure anything for the common case."""
    realm = _default_lazer_realm_path()
    realm_ok = False
    try:
        realm_ok = realm.exists()
    except OSError:
        realm_ok = False
    osu_bin = OsuLazerImporter._locate_binary()
    cm = CmCliRunner.autodetect()
    return {
        "realm_path": str(realm) if realm_ok else "",
        "realm_detected": realm_ok,
        "osu_binary": str(osu_bin) if osu_bin else "",
        "osu_detected": bool(osu_bin),
        "cm_cli_command": shlex.join(cm.command) if cm else "",
        "cm_detected": cm is not None,
    }


def _fetch_existing_collections(
    cm_cli_command: list[str], realm_path: Path,
) -> list[CollectionInfo]:
    """Run CM CLI to export client.realm to a temp .osdb and parse it.

    Snapshots the live realm first (Realm is MVCC, so a file copy is a
    consistent point-in-time view) so osu!lazer can stay open, and keeps the
    temp files next to the realm so the wine sandbox can write them.
    """
    snapshot = realm_path.parent / f".oc-gui-snapshot-{os.getpid()}.realm"
    out = realm_path.parent / f".oc-gui-export-{os.getpid()}.osdb"
    try:
        shutil.copy2(realm_path, snapshot)
        cm = CmCliRunner(CmCliConfig(command=list(cm_cli_command), osu_location=None))
        cm.export_realm_to_osdb(snapshot, out)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(
                "Collection Manager CLI exited without producing an output "
                f"file. The wine sandbox may be unable to write to {out.parent}. "
                "Grant it with:\n"
                f"  flatpak override --user --filesystem={out.parent} org.winehq.Wine"
            )
        return OsdbReader.read(out)
    finally:
        for p in (snapshot, out):
            try:
                p.unlink()
            except OSError:
                pass


def _consolidate_osdb(out_dir: Path, emit) -> None:
    """Move any loose .osdb files under out_dir into a single db/ subfolder."""
    db_dir = out_dir / "db"
    db_dir.mkdir(exist_ok=True)
    moved = 0
    for f in out_dir.rglob("*.osdb"):
        try:
            if db_dir in f.parents:
                continue
            dest = db_dir / f.name
            if dest.exists():
                dest = db_dir / f"{f.parent.name} - {f.name}"
            shutil.move(str(f), str(dest))
            moved += 1
        except OSError:
            continue
    if moved:
        emit("log", {"line": f"[moved {moved} .osdb file(s) into {db_dir}]"})


def _open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _version_tuple(s: str) -> tuple[int, ...]:
    """Parse a dotted version like '1.2.0' (leading v/V stripped) into a
    comparable tuple of ints; non-numeric parts are ignored."""
    parts: list[int] = []
    for chunk in str(s).lstrip("vV").split("."):
        num = re.match(r"\d+", chunk.strip())
        if not num:
            break
        parts.append(int(num.group()))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    lt, ct = _version_tuple(latest), _version_tuple(current)
    return bool(lt) and lt > ct


def _pick_release_asset(assets: list, platform: str) -> str:
    """Choose the right downloadable asset for this OS from a GitHub release."""
    def find(suffixes: tuple[str, ...]) -> str:
        for a in assets:
            name = str(a.get("name") or "").lower()
            if name.endswith(suffixes):
                return a.get("browser_download_url") or ""
        return ""
    if platform == "win32":
        return find(("setup.exe", ".exe"))
    if platform == "darwin":
        return find((".dmg",))
    # Linux ships a .tar.gz now; keep .appimage as a fallback for older
    # releases so the picker is format-agnostic.
    return find((".tar.gz", ".tgz", ".appimage"))


def _launch_updater(path: Path) -> None:
    """Run a freshly-downloaded installer/update artifact."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # runs Setup.exe
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # mounts the .dmg
    elif str(path).endswith((".tar.gz", ".tgz")):
        # Portable tarball: extract beside the current install, swap it in,
        # relaunch, and exit (see below). Does not return on success.
        _apply_linux_tarball_update(path)
    else:
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        subprocess.Popen([str(path)], start_new_session=True)  # AppImage


def _apply_linux_tarball_update(tar_path: Path) -> None:
    """Update a portable-tarball install in place, then hand off to the new
    build. Extracts the new tarball onto the same filesystem as the install,
    renames the old install aside (safe: the running process keeps its open
    inodes/mmaps), moves the new one in, relaunches it detached, and exits.
    Rolls back the rename on failure so a botched update can't brick the app.
    Only meaningful for a frozen (bundled) build."""
    import tarfile
    import tempfile
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "Running from source — pull the new version with git instead.")
    exe = Path(sys.executable).resolve()
    install_dir = exe.parent            # …/osu-collector-gui/
    parent = install_dir.parent
    # Stage on the SAME filesystem as the install so the final move is atomic.
    staging = Path(tempfile.mkdtemp(prefix=".ocg-upd-", dir=str(parent)))
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(staging)
        new_dir = staging / install_dir.name
        if not (new_dir / exe.name).exists():
            raise RuntimeError("update archive has an unexpected layout")
        backup = parent / f"{install_dir.name}.bak-{int(time.time())}"
        os.rename(install_dir, backup)      # frees the install path
        try:
            os.rename(new_dir, install_dir)
        except OSError:
            os.rename(backup, install_dir)  # roll back
            raise
        new_exe = install_dir / exe.name
        try:
            os.chmod(new_exe, 0o755)
        except OSError:
            pass
        subprocess.Popen([str(new_exe)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    os._exit(0)   # the freshly-launched instance takes over


def _cleanup_update_backups() -> None:
    """Remove leftover *.bak-* / .ocg-upd-* dirs from a previous tarball
    update (the old install can't delete itself while running, so the next
    launch cleans up). Best-effort, Linux frozen builds only."""
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return
    try:
        install_dir = Path(sys.executable).resolve().parent
        parent = install_dir.parent
        for p in parent.glob(f"{install_dir.name}.bak-*"):
            shutil.rmtree(p, ignore_errors=True)
        for p in parent.glob(".ocg-upd-*"):
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


class JsApi:
    """The object pywebview exposes to JavaScript as window.pywebview.api.

    Every public method is callable from the frontend and returns a
    JSON-serialisable value. Long-running work (downloads, CM CLI export) runs
    on background threads; progress is pushed back to the page via events.
    """

    def __init__(self) -> None:
        self._window = None
        self._settings = _load_settings()
        self._downloader: Downloader | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_window(self, window) -> None:
        self._window = window

    # ----- state / settings ------------------------------------------------

    def _detected_state(self) -> dict:
        """Detection-panel state that honors manual path overrides, not just
        auto-detection. A user who sets client.realm / the lazer binary by
        hand should see it as 'found' once it's on disk — otherwise the panel
        keeps showing 'not found' even though the run will happily use it."""
        s = self._settings
        det = _autodetect_paths()

        def _override(path_key: str, detected_key: str, raw) -> None:
            raw = (raw or "").strip()
            if not raw:
                return
            try:
                exists = Path(raw).expanduser().exists()
            except OSError:
                exists = False
            if exists:
                det[path_key] = str(Path(raw).expanduser())
                det[detected_key] = True

        _override("realm_path", "realm_detected", s.get("lazer_realm_path"))
        _override("osu_binary", "osu_detected", s.get("osu_binary"))

        cm_cmd = _normalize_cm(s.get("cm_cli_command")) if s.get("cm_cli_command") else None
        if cm_cmd:
            # A single-token command is a path we can existence-check; a
            # multi-token one (e.g. `flatpak run …`) we trust as configured.
            ok = True
            if len(cm_cmd) == 1:
                try:
                    ok = Path(cm_cmd[0]).expanduser().exists()
                except OSError:
                    ok = False
            if ok:
                det["cm_cli_command"] = shlex.join(cm_cmd)
                det["cm_detected"] = True
        return det

    def get_state(self) -> dict:
        """Everything the frontend needs to render its initial state."""
        s = self._settings
        auto = _autodetect_paths()
        cm_value = s.get("cm_cli_command")
        cm_str = shlex.join(cm_value) if isinstance(cm_value, list) and cm_value \
            else (cm_value if isinstance(cm_value, str) else "") or auto["cm_cli_command"]
        return {
            "version": APP_VERSION,
            "author": APP_AUTHOR,
            "name": APP_NAME,
            "platform": sys.platform,
            "theme": s.get("theme", "dark"),
            "output_dir": s.get("last_output_dir")
                or str(Path.home() / "osu-collections"),
            "target": s.get("target_collection", DEFAULT_TARGET),
            "new_collection_name": s.get("new_collection_name", ""),
            "settings": {
                "auto_import": bool(s.get("auto_import", True)),
                "skip_already_imported": bool(s.get("skip_already_imported", True)),
                "skip_video": bool(s.get("skip_video", True)),
                "restart_lazer_after": bool(s.get("restart_lazer_after", True)),
                "generate_osdb": bool(s.get("generate_osdb", False)),
                "consolidate_osdb": bool(s.get("consolidate_osdb", False)),
                "cleanup_after_import": bool(s.get("cleanup_after_import", False)),
                "download_parallel": int(s.get("download_parallel", DOWNLOAD_PARALLEL)),
                "import_parallel": int(s.get("import_parallel", 1)),
                "import_delay_ms": int(s.get("import_delay_ms", 0)),
                "osu_binary": s.get("osu_binary") or auto["osu_binary"],
                "lazer_realm_path": s.get("lazer_realm_path") or auto["realm_path"],
                "cm_cli_command": cm_str,
                "custom_mirrors": s.get("custom_mirrors", ""),
            },
            "detected": self._detected_state(),
            "labels": {
                "default_target": DEFAULT_TARGET,
                "new_target": NEW_TARGET,
                "no_merge": target_combo_no_merge_label(),
            },
        }

    def _merge_settings(self, settings: dict) -> None:
        if not settings:
            return
        S = self._settings
        for k in ("auto_import", "skip_already_imported", "restart_lazer_after",
                  "generate_osdb", "consolidate_osdb", "cleanup_after_import",
                  "skip_video"):
            if k in settings:
                S[k] = bool(settings[k])
        for k in ("download_parallel", "import_parallel", "import_delay_ms"):
            if k in settings:
                try:
                    S[k] = int(settings[k])
                except (TypeError, ValueError):
                    pass
        if "osu_binary" in settings:
            S["osu_binary"] = (settings["osu_binary"] or "").strip()
        if "lazer_realm_path" in settings:
            S["lazer_realm_path"] = (settings["lazer_realm_path"] or "").strip()
        if "cm_cli_command" in settings:
            S["cm_cli_command"] = _normalize_cm(settings["cm_cli_command"])
        if "custom_mirrors" in settings:
            S["custom_mirrors"] = settings["custom_mirrors"] or ""
        if "theme" in settings:
            S["theme"] = settings["theme"]

    def save_settings(self, payload: dict) -> dict:
        """Persist settings from the Settings panel; returns fresh state."""
        payload = payload or {}
        if "output_dir" in payload:
            self._settings["last_output_dir"] = payload["output_dir"]
        if "target" in payload:
            self._settings["target_collection"] = payload["target"]
        if "new_collection_name" in payload:
            self._settings["new_collection_name"] = payload["new_collection_name"]
        self._merge_settings(payload.get("settings", {}))
        try:
            _save_settings(self._settings)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "state": self.get_state()}

    def save_theme(self, theme: str) -> bool:
        self._settings["theme"] = "light" if theme == "light" else "dark"
        try:
            _save_settings(self._settings)
        except OSError:
            return False
        return True

    # ----- pickers ---------------------------------------------------------

    def choose_folder(self, current: str = "") -> str:
        if not self._window:
            return ""
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=current or str(Path.home()),
            )
        except Exception:
            return ""
        if res:
            return res[0] if isinstance(res, (list, tuple)) else str(res)
        return ""

    def choose_file(self, current: str = "") -> str:
        if not self._window:
            return ""
        try:
            import webview
            directory = ""
            if current:
                p = Path(current).expanduser()
                directory = str(p.parent if p.parent.exists() else Path.home())
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, directory=directory or str(Path.home()),
                allow_multiple=False,
            )
        except Exception:
            return ""
        if res:
            return res[0] if isinstance(res, (list, tuple)) else str(res)
        return ""

    def open_folder(self, path: str = "") -> bool:
        target = Path(path or self._settings.get("last_output_dir")
                      or (Path.home() / "osu-collections")).expanduser()
        if target.exists():
            _open_in_file_manager(target)
            return True
        return False

    # ----- collection preview + lazer scan ---------------------------------

    def preview(self, text: str) -> dict:
        """Lightweight metadata fetch for pasted IDs so the frontend can show
        cherry poster cards. Metadata only — no beatmap detail pages."""
        ids = _parse_ids(text)
        client = OsuCollectorClient()
        collections: list[dict] = []
        for cid in ids[:24]:
            try:
                info = client.fetch_collection(cid)
                cover = ""
                if info.beatmapset_ids:
                    cover = (f"https://assets.ppy.sh/beatmaps/"
                             f"{info.beatmapset_ids[0]}/covers/cover.jpg")
                collections.append({
                    "id": info.id,
                    "name": info.name,
                    "uploader": info.uploader,
                    "count": len(info.beatmapset_ids),
                    "cover": cover,
                })
            except Exception as e:
                collections.append({"id": cid, "error": str(e)})
        return {"ids": ids, "collections": collections}

    def scan_collections(self) -> dict:
        """Auto-scan the osu! folder: export existing lazer collections via CM
        CLI so the user can pick one to merge into — no button required."""
        auto = _autodetect_paths()
        realm = (self._settings.get("lazer_realm_path") or auto["realm_path"]).strip()
        cm_value = self._settings.get("cm_cli_command")
        cmd = _normalize_cm(cm_value) or _normalize_cm(auto["cm_cli_command"])
        if not realm or not Path(realm).expanduser().exists():
            return {"ok": False, "reason": "no_realm"}
        if not cmd and (sys.platform == "win32"
                        or shutil.which("flatpak") or shutil.which("wine")):
            # Auto-provision the CM CLI (download it) so existing collections
            # list on open — not only after a merge run. On Linux it still needs
            # a wine to run through; if there's none, detection stays empty.
            try:
                if not CmCliInstaller.installed_exe():
                    CmCliInstaller.install(log_func=lambda s: None)
                cmd = _normalize_cm(_autodetect_paths()["cm_cli_command"])
            except Exception:
                pass
        if not cmd:
            return {"ok": False, "reason": "no_cm"}
        try:
            cols = _fetch_existing_collections(cmd, Path(realm).expanduser())
        except Exception as e:
            return {"ok": False, "reason": "error", "error": str(e)}
        return {
            "ok": True,
            "collections": [
                {"name": c.name, "count": len(c.beatmaps)} for c in cols
            ],
        }

    # ----- export ----------------------------------------------------------

    def choose_save_path(self, default_name: str = "collection.db") -> str:
        """Native 'Save as…' dialog for the export destination."""
        if not self._window:
            return ""
        try:
            import webview
            res = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=default_name or "collection.db",
            )
        except Exception:
            return ""
        if res:
            return res[0] if isinstance(res, (list, tuple)) else str(res)
        return ""

    def export_to_file(self, payload: dict) -> dict:
        """Export an existing osu!lazer collection (or all of them) to a
        `.db` (osu! stable / collector format) or `.osdb` file.

        The CM CLI runs in the wine sandbox and can only write where it's been
        granted (the realm folder), so we produce the file next to the realm
        and then move it to the user's chosen destination.
        """
        payload = payload or {}
        name = (payload.get("collection") or "").strip()      # "" = all
        dest_s = (payload.get("dest") or "").strip()
        if not dest_s:
            return {"ok": False, "error": "No destination chosen."}
        dest = Path(dest_s).expanduser()
        fmt = ".osdb" if dest.suffix.lower() == ".osdb" else ".db"

        auto = _autodetect_paths()
        realm_s = (self._settings.get("lazer_realm_path")
                   or auto["realm_path"] or "").strip()
        if not realm_s or not Path(realm_s).expanduser().exists():
            return {"ok": False, "error": "osu!lazer client.realm not found."}
        realm = Path(realm_s).expanduser()
        cmd = (_normalize_cm(self._settings.get("cm_cli_command"))
               or _normalize_cm(auto["cm_cli_command"]))
        if not cmd:
            try:
                if not CmCliInstaller.installed_exe():
                    CmCliInstaller.install(log_func=lambda s: None)
                cmd = _normalize_cm(_autodetect_paths()["cm_cli_command"])
            except Exception:
                pass
        if not cmd:
            return {"ok": False, "error": "Collection Manager CLI not available "
                    "(on Linux, run scripts/setup-linux.sh once)."}

        cm = CmCliRunner(CmCliConfig(command=list(cmd), osu_location=None))
        pid = os.getpid()
        tmp: list[Path] = []

        def near(suffix: str) -> Path:
            p = realm.parent / f".oc-gui-export-{pid}{suffix}"
            tmp.append(p)
            return p

        try:
            snapshot = near(".realm")
            shutil.copy2(realm, snapshot)
            full_osdb = near(".osdb")
            cm.export_realm_to_osdb(snapshot, full_osdb)

            if name:
                one = next((c for c in OsdbReader.read(full_osdb)
                            if c.name == name), None)
                if one is None:
                    return {"ok": False, "error": f"Collection {name!r} not found."}
                src_osdb = near(".one.osdb")
                OsdbWriter.write(src_osdb, one)
            else:
                src_osdb = full_osdb

            if fmt == ".osdb":
                produced = src_osdb
            else:
                produced = near(".out.db")
                cm.convert_osdb_to_db(src_osdb, produced)

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), str(dest))
            return {"ok": True, "path": str(dest)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            for p in tmp:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    # ----- the main action -------------------------------------------------

    def start(self, payload: dict) -> dict:
        payload = payload or {}
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "A download is already running."}

            ids = _parse_ids(payload.get("ids_text", ""))
            if not ids:
                return {"ok": False,
                        "error": "No valid collection IDs or links found."}

            out_dir = Path(payload.get("output_dir")
                           or (Path.home() / "osu-collections")).expanduser()
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": f"Can't create output folder: {e}"}

            # Persist the run's choices so the next launch remembers them.
            self._settings["last_output_dir"] = str(out_dir)
            target = payload.get("target", DEFAULT_TARGET)
            self._settings["target_collection"] = target
            new_name = (payload.get("new_collection_name") or "").strip()
            self._settings["new_collection_name"] = new_name
            self._merge_settings(payload.get("settings", {}))
            try:
                _save_settings(self._settings)
            except OSError:
                pass

            auto = _autodetect_paths()
            S = self._settings
            cm_cmd = _normalize_cm(S.get("cm_cli_command")) \
                or _normalize_cm(auto["cm_cli_command"]) or None
            realm = (S.get("lazer_realm_path") or auto["realm_path"]).strip() or None
            osu_bin = (S.get("osu_binary") or auto["osu_binary"]).strip() or None

            no_merge = target_combo_no_merge_label()
            realm_ok = bool(realm and Path(realm).expanduser().exists())
            merge_wanted = target != no_merge
            want_skip = bool(S.get("skip_already_imported", True))
            # Auto-download the CM CLI if we have a realm but no CLI yet and
            # we'd use it — either to merge, OR to probe lazer for the
            # "skip already-imported" dedup (which also needs the CLI + realm).
            # On Windows this is the whole fix; on Linux it still needs the
            # wine flatpak, so a failure here just falls through to the warning.
            if (merge_wanted or want_skip) and realm_ok and not cm_cmd:
                try:
                    CmCliInstaller.install(log_func=lambda s: None)
                    cm_cmd = _normalize_cm(
                        _autodetect_paths()["cm_cli_command"]) or None
                    if cm_cmd:
                        self._settings["cm_cli_command"] = cm_cmd
                        try:
                            _save_settings(self._settings)
                        except OSError:
                            pass
                except Exception:
                    cm_cmd = None
            # Merging into lazer's realm needs BOTH Collection Manager CLI and
            # a real client.realm. If either is missing we still download +
            # auto-import (which don't need CM CLI), and warn instead of
            # erroring — that's the behaviour that "just works".
            add_to_lazer = merge_wanted and bool(cm_cmd) and realm_ok
            merge_warning = None
            if merge_wanted and not add_to_lazer:
                missing = []
                if not cm_cmd:
                    missing.append("Collection Manager CLI")
                if not realm_ok:
                    missing.append("client.realm")
                merge_warning = (
                    "Maps will download and import, but collections won't be "
                    "merged into osu!lazer — couldn't find "
                    + " and ".join(missing)
                    + ". Set the path(s) in Settings → Paths."
                )
            target_name: str | None = None
            if target == NEW_TARGET:
                if not new_name:
                    return {"ok": False,
                            "error": "Enter a name for the new collection."}
                target_name = new_name
            elif target not in (DEFAULT_TARGET, no_merge):
                target_name = target  # an existing lazer collection name

            # The merge step reads back the per-collection .osdb files we wrote
            # this run, so generation MUST be on whenever we merge. The user's
            # "Generate .osdb" toggle only controls whether they're kept as
            # standalone export artifacts afterwards.
            generate_osdb = bool(S.get("generate_osdb", False)) or add_to_lazer

            extra_mirrors: list[str] = []
            for line in str(S.get("custom_mirrors", "")).splitlines():
                tmpl = BeatmapMirror.normalize_template(line)
                if tmpl:
                    extra_mirrors.append(tmpl)

            job = DownloadJob(
                collection_ids=ids,
                output_dir=out_dir,
                download_beatmaps=True,
                extra_mirrors=extra_mirrors,
                generate_osdb=generate_osdb,
                auto_import=bool(S.get("auto_import", True)),
                osu_binary=osu_bin,
                import_parallel=int(S.get("import_parallel", 1)),
                import_delay_ms=int(S.get("import_delay_ms", 0)),
                add_to_lazer_collections=add_to_lazer,
                cm_cli_command=cm_cmd,
                lazer_realm_path=realm,
                target_collection_name=target_name,
                restart_lazer_after=bool(S.get("restart_lazer_after", True)),
                cleanup_after_import=bool(S.get("cleanup_after_import", False)),
                skip_already_imported=bool(S.get("skip_already_imported", True)),
                download_parallel=int(S.get("download_parallel", DOWNLOAD_PARALLEL)),
                skip_video=bool(S.get("skip_video", True)),
            )

            consolidate = bool(S.get("consolidate_osdb", False))
            self._downloader = Downloader(job, self._emit_event)

            def _run() -> None:
                try:
                    self._downloader.run()
                except Exception as e:
                    self._emit_event("error", {"message": str(e)})
                    self._emit_event("batch_finished",
                                     {"ok": 0, "total": len(ids)})
                finally:
                    if consolidate:
                        try:
                            _consolidate_osdb(out_dir, self._emit_event)
                        except Exception:
                            pass

            self._thread = threading.Thread(target=_run, daemon=True,
                                            name="oc-download")
            self._thread.start()
            return {"ok": True, "count": len(ids), "output_dir": str(out_dir),
                    "warning": merge_warning}

    # ----- updates ---------------------------------------------------------

    def check_update(self) -> dict:
        """Query GitHub Releases; report whether a newer version is published."""
        try:
            import urllib.request
            req = urllib.request.Request(
                GITHUB_LATEST_RELEASE_API,
                headers={"User-Agent": USER_AGENT,
                         "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            return {"update": False}
        tag = str(data.get("tag_name") or "")
        if not _is_newer(tag, APP_VERSION):
            return {"update": False, "latest": tag.lstrip("vV")}
        return {
            "update": True,
            "latest": tag.lstrip("vV"),
            "url": data.get("html_url") or GITHUB_RELEASES_PAGE,
            "download_url": _pick_release_asset(data.get("assets") or [],
                                                sys.platform),
            "notes": (data.get("body") or "")[:600],
        }

    def apply_update(self, download_url: str = "") -> dict:
        """Download the platform installer and launch it; if no direct asset
        is available, open the releases page in the browser instead."""
        # Running from source (not a frozen/packaged build): downloading and
        # launching a release artifact is the wrong update vector — it won't
        # touch the git checkout, and on Linux it would fetch the AppImage
        # (which is broken on Arch/rolling distros) and crash. Point the user
        # at the releases page and tell them to update their source instead.
        if not getattr(sys, "frozen", False):
            try:
                import webbrowser
                webbrowser.open(GITHUB_RELEASES_PAGE)
            except Exception:
                pass
            return {"ok": True, "opened": "page",
                    "message": "You're running from source — `git pull` to update."}
        if not download_url:
            try:
                import webbrowser
                webbrowser.open(GITHUB_RELEASES_PAGE)
            except Exception:
                pass
            return {"ok": True, "opened": "page"}
        try:
            import urllib.request
            import tempfile
            name = download_url.split("/")[-1] or "osu-collector-gui-update"
            dest = Path(tempfile.gettempdir()) / name
            req = urllib.request.Request(
                download_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            _launch_updater(dest)
            return {"ok": True, "path": str(dest)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def cancel(self) -> bool:
        if self._downloader:
            self._downloader.cancel()
        return True

    def install_collection_manager(self) -> dict:
        """One-click Collection Manager setup. Runs in the background and
        streams progress via 'cm_setup' events; returns immediately."""
        import threading
        if sys.platform.startswith("linux") and not shutil.which("flatpak"):
            return {"ok": False, "error":
                    "flatpak isn't installed. Install it with your package "
                    "manager (e.g. sudo pacman -S flatpak) then try again."}

        def work():
            def log(line):
                self._emit_event("cm_setup", {"line": str(line)})
            try:
                CmCliInstaller.provision(log_func=log)
                self._emit_event("cm_setup", {"done": True, "ok": True})
            except Exception as e:
                self._emit_event("cm_setup",
                                 {"done": True, "ok": False, "error": str(e)})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def confirm_merge(self, proceed: bool) -> bool:
        if not self._downloader:
            return False
        if proceed:
            self._downloader.confirm_merge_continue()
        else:
            self._downloader.cancel()
        return True

    # ----- python → frontend ----------------------------------------------

    def _emit_event(self, name: str, payload: dict) -> None:
        if not self._window:
            return
        try:
            msg = json.dumps({"event": name, "data": payload})
        except (TypeError, ValueError):
            msg = json.dumps({"event": name, "data": {}})
        try:
            self._window.evaluate_js(f"window.ocOnEvent({msg})")
        except Exception:
            pass


CRASH_LOG = Path(
    os.environ.get("TEMP", "/tmp") if sys.platform == "win32" else "/tmp"
) / "oc-crash.log"


def _write_crash(text: str) -> Path | None:
    """Append a traceback to the crash log so a windowed build's startup
    failure isn't silent. Returns the log path (or None if it couldn't write)."""
    try:
        with CRASH_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now(timezone.utc).isoformat()} "
                    f"v{APP_VERSION} {sys.platform} =====\n")
            f.write(text)
        return CRASH_LOG
    except OSError:
        return None


def _report_crash(exc: BaseException) -> None:
    """Log + surface an unhandled exception. On a windowed Windows build there's
    no console, so also pop a MessageBox pointing at the log."""
    import traceback
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path = _write_crash(text)
    try:
        sys.stderr.write(text)
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                f"{type(exc).__name__}: {exc}\n\n"
                f"Full details written to:\n{log_path or '(could not write log)'}",
                f"osu-collector-gui v{APP_VERSION} hit an error", 0x10)
    except Exception:
        pass


def _install_crash_handler() -> None:
    """Route uncaught exceptions (main + worker threads) through _report_crash."""
    sys.excepthook = lambda et, ev, tb: _report_crash(
        ev if isinstance(ev, BaseException) else et(ev))
    try:
        threading.excepthook = lambda a: _report_crash(a.exc_value)  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    _install_crash_handler()
    _cleanup_update_backups()   # clear leftovers from a prior tarball update
    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "pywebview is required to run the GUI.\n"
            "Install it with:  pip install -r requirements.txt\n"
        )
        return 1

    index = WEB_DIR / "index.html"
    if not index.exists():
        sys.stderr.write(f"Frontend assets not found at {index}\n")
        return 1

    api = JsApi()
    state = api._settings
    bg = "#fafafa" if state.get("theme") == "light" else "#0d0a0b"
    window = webview.create_window(
        f"{APP_NAME} v{APP_VERSION} by {APP_AUTHOR}",
        url=str(index),
        js_api=api,
        width=1200,
        height=840,
        min_size=(940, 640),
        background_color=bg,
    )
    api.set_window(window)

    # Closing the window must actually exit the process. The download/import
    # executor threads are non-daemon, so a normal return would make Python hang
    # at exit waiting to join them — the app lingers as a background process.
    # Cancel any running job and hard-exit when the window closes.
    def _shutdown(*_args):
        try:
            api.cancel()
        except Exception:
            pass
        os._exit(0)

    try:
        window.events.closed += _shutdown
    except Exception:
        pass

    # Linux: prefer the self-contained Qt WebEngine backend. The GTK/WebKit
    # backend crashes on rolling distros (Arch) when PyInstaller's bundled
    # glib collides with the system's newer libsecret. Qt bundles its own
    # engine, so it works regardless of the host's GTK stack. Override with
    # OCG_GUI=gtk to force the old backend when running from source.
    gui = os.environ.get("OCG_GUI") or None
    if gui is None and sys.platform.startswith("linux"):
        if getattr(sys, "frozen", False):
            # The bundled Qt engine's sandbox helper isn't SUID in a portable
            # tarball, so the sandbox can't start. We only ever load our own
            # local, trusted frontend, so disabling it is safe.
            os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        try:
            import qtpy  # noqa: F401 — probe for the Qt backend
            gui = "qt"
        except ImportError:
            gui = None  # let pywebview auto-select (GTK) when Qt isn't present

    webview.start(gui=gui)
    _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
