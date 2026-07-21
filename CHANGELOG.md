# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed

- **Auto-import now works when osu!lazer is already running.** The importer
  launched `osu! <files>` blindly. When the game was **closed** that launch
  became the primary instance and imported the whole batch in-process (fine).
  When the game was **already open**, the same launch was a *secondary*
  instance that forwarded each file over osu!'s single-lane import IPC channel,
  which has a hard ~3s/file timeout serviced one message at a time — so any
  real batch overran it and maps were silently dropped (`IPCTimeoutException`).
  Now the importer detects a running instance and, when osu! is open, forwards
  **one file per launch, serially with up to 3 retries** (letting the game
  drain its queue between files) instead of one doomed batched forward. The
  fast batched cold-launch is kept for when osu! is closed. Reported on NixOS
  (Nix `osu-lazer-bin`), but the bug hit any platform with the game open during
  import.

### Fixed

- **Skip-already-imported now unions hash AND id matching.** v1.5.7 switched to
  hash-only, which actually matched *fewer* maps — hash and id catch different
  sets (hash → OnlineID=-1 mirror imports; id → verified maps whose on-disk
  file differs from the collection's reference). The probe now runs both
  against one realm snapshot and skips a set if its **md5 OR its online id** is
  in lazer. Catches the maps both single-method probes missed.

## [1.5.7] — 2026-06-23

### Fixed

- **Skip-already-imported now matches by MD5 hash, not just online ID.** Maps
  lazer imported from mirror `.osz` files often land with `OnlineID = -1` (it
  couldn't verify them online), so the id-based probe missed them and they got
  re-downloaded even though they were already in your library. The probe now
  asks the CM CLI by **beatmap hash** (`create -h`), which lazer stores
  reliably — so already-imported maps are correctly skipped regardless of their
  online id. Verified against a real realm; falls back to id matching only when
  a collection provides no checksums.

## [1.5.6] — 2026-06-23

### Fixed

- **"Skip already-imported maps" now works on plain downloads, not just merges.**
  The probe that checks osu!lazer for maps you already have was gated behind
  "merge into a collection" — so a plain download or Import-All re-downloaded
  everything, even already-imported maps. The probe now runs for any download
  when the toggle is on and a readable `client.realm` + CM CLI are available
  (and the CM CLI is auto-fetched for skip-only runs too). Watch for the
  `[probe] lazer has X/Y maps; skipping Z sets` log line — if it's missing,
  your realm isn't detected (set it in Settings → Paths).

## [1.5.5] — 2026-06-23

### Fixed

- **Retry no longer looks stuck / double-runs.** A leftover summary line made
  the retry log a second `[retry]` ("spaced rounds") that read like a second
  pass, and the retry itself wasn't time-bounded so it could drag for minutes
  on a heavily rate-limited collection. Now it's a single, **time-boxed ~20s**
  retry (no long cooldown): re-attempt the failed sets, and whatever hasn't
  come through in 20s is skipped.
- **Already-downloaded maps are skipped without a mirror request.** `download()`
  now checks the output folder for an existing `.osz` (`{id}.osz` / `{id} …osz`)
  *before* contacting any mirror, so re-running a collection doesn't re-download
  or re-trip rate limits on maps you already have.

## [1.5.4] — 2026-06-23

### Changed

- **Retry pass is now a single 20s round, then skip.** The escalating 3-round
  retry (20s → 40s → 60s cooldowns) made the end of a big collection wait 2+
  minutes. Now: one 20s cooldown + one retry, then any still-rate-limited sets
  are **skipped** (logged calmly, not as errors) — re-run the collection later
  to grab them. Faster finish; no long wait.

## [1.5.3] — 2026-06-23

### Changed

- **Quieter download log — no more wall of red.** Sets that fail a pass on
  mirror rate-limits are retried (since 1.5.1), so they're no longer logged as
  a red `[error]` each; instead the run shows one calm "[retry] N set(s) hit
  rate limits — retrying" summary and a "[skip] N not hosted on any mirror"
  summary. A red `[error]` now means a *final* failure that survived all retry
  rounds. Recovered-on-retry maps log a green `[retry-ok]`.

## [1.5.2] — 2026-06-23

### Fixed

- **"Failed to load Python DLL" crash on launch (Windows).** The Windows build
  was `--onefile`, which extracts Python to `%TEMP%\_MEI…` on every launch and
  loads `python312.dll` from there — fragile, and it fails outright when
  antivirus quarantines a temp DLL or a CRT dependency isn't found. Switched to
  `--onedir`: the installer lays the files down permanently in the app folder,
  so Python loads from `{app}\_internal` with no temp extraction. Also launches
  faster and is friendlier to antivirus.

## [1.5.1] — 2026-06-23

### Fixed

- **Maps that fail to download are now retried instead of lost.** On a big
  collection, mirrors rate-limit by IP over time (403/429) and occasionally
  503, so a chunk of sets can fail their first attempt when *every* mirror is
  cooling down at once — at any concurrency, even Gentle. Those were just
  logged and abandoned. Now failed sets are retried in up to 3 spaced-out
  rounds (20s/40s/60s cooldowns, mirror state reset each round) so the per-IP
  windows clear and the maps come through. Sets genuinely absent from every
  mirror (404) aren't retried.

## [1.5.0] — 2026-06-23

### Added

- **Live progress during "Starting…".** Before the first download, a big
  collection spends time (1) paging the full beatmap list from osu!collector
  and (2) probing osu!lazer for what you already have. The dock now shows what
  it's doing — "Fetching beatmap details… 8,200 so far", "Checking your
  osu!lazer library…" — instead of an opaque "Starting…".
- **Crash handler.** Unhandled exceptions (incl. on a windowed Windows build
  with no console) are written to `oc-crash.log` in the temp dir and shown in a
  dialog, so a startup failure is captured instead of vanishing.

## [1.4.3] — 2026-06-23

### Fixed

- **"Update" no longer downloads/launches a release artifact when running from
  source.** On a `git`/source checkout the in-app updater used to fetch the
  platform installer (on Linux, the AppImage that's broken on Arch) and run it
  — the wrong update vector, and a crash. It now opens the Releases page and
  tells you to `git pull` instead. Packaged builds are unaffected.

## [1.4.2] — 2026-06-23

### Fixed

- **Manually-set client.realm (and lazer binary) now show as "detected."** The
  detection panel was built purely from auto-detection and ignored manual path
  overrides, so a realm you set by hand kept reporting "not found" even though
  it was saved and the run used it fine. The panel now honors a manual path
  once it exists on disk, and **Browse…** auto-saves so the indicator updates
  immediately (no separate "Save settings" click needed).

## [1.4.1] — 2026-06-23

### Changed

- **Sayobot mirror hard-capped to 1 concurrent download.** It's a slow CN CDN
  (~200 KB/s from outside China, redirects to a high port) and tended to stall
  workers and time out on big maps — looking "dead" in the logs. It stays as a
  lowest-priority backstop but can now tie up at most one worker. Other mirrors
  are unaffected (adaptive cap up to 12).

## [1.4.0] — 2026-06-22

### Changed

- **Gentler default download concurrency.** Parallel downloads now defaults to
  **16** (was 48) so an average PC doesn't get bogged down — high parallelism
  plus live auto-import can saturate disk/CPU on slower machines during a run.

### Added

- **Speed presets in Settings → Tuning.** One-click **Gentle** (6 workers, low
  PC impact), **Balanced** (16, default), and **Max speed** (32 — the real cap;
  the download executor is clamped to 32 regardless) chips set the tuning fields
  and save immediately. The chip matching your current values is highlighted.

## [1.3.1] — 2026-06-22

### Changed

- **Monday AimSlop quick preset refreshed.** The collection (#21994) grew to
  15,631 maps; the preset card now shows the current count (was 8,606).

## [1.3.0] — 2026-06-22

### Added

- **Export tab.** Save an osu!lazer collection — one, or all of them — to a
  `.db` (osu! stable / osu!collector) or `.osdb` (Collection Manager) file via
  a native Save dialog.

## [1.2.0] — 2026-06-22

### Fixed

- **Collection list/merge now gets the right runtime.** The Collection Manager
  CLI is a .NET 9 app; the Windows installer now installs the **.NET 9 Desktop
  Runtime** (it was installing .NET Framework 4.8 — the wrong runtime, so merges
  failed on clean Windows machines).

### Added

- **`scripts/setup-linux.sh`** — one-time Linux setup for the collection feature:
  installs the WineHQ flatpak + the .NET 9 runtime into its wine prefix and grants
  sandbox access to your osu! data. Downloads + auto-import work without it.
- Existing collections now **auto-load on open** (the CM CLI auto-downloads
  during the scan, not only on a merge run).

## [1.1.4] — 2026-06-22

### Fixed

- **App no longer lingers as a background process after the window is closed.**
  Non-daemon download/import worker threads kept Python alive at exit; closing
  the window now cancels any running job and exits the process immediately.

## [1.1.3] — 2026-06-22

### Fixed

- **No "has osu!lazer finished importing?" prompt when there's nothing to gate.**
  It now appears only for an actual collection merge, or when cleanup-after-import
  needs the source files released — not after a plain download + import with no
  collection chosen.

## [1.1.2] — 2026-06-22

### Fixed

- **Installer no longer hangs on "Closing applications."** The updater used the
  Restart Manager's graceful close, which waits forever on a WebView2 window;
  it now force-closes the app being replaced (`CloseApplications=force`).
- **Light mode fixed.** Preset cards used hardcoded white text (invisible on
  light-mode's white cards); switched to theme-aware colours that read in both
  themes.
- **Preset map count** is now larger and higher-contrast (was a dim grey).

## [1.1.1] — 2026-06-22

### Fixed

- **Cancel is instant.** It no longer waits for in-flight downloads to finish —
  running downloads bail mid-stream and the worker pool returns immediately.
- **Resilient to osu!collector hiccups.** API calls now retry transient
  Cloudflare 5xx / 520–524 errors with backoff instead of aborting the whole run.
- **AppImage menu icon** installs into the icon theme path, so desktop menus
  show the R3D logo instead of a generic document icon.

### Changed

- **Bigger UI** — a global 1.25× scale makes text and buttons readable on
  high-DPI displays; preset collection name + map count are now white and larger.

## [1.1.0] — 2026-06-22

### Added

- **Weekday presets** on the Download view — Monday AimSlop · Tuesday Streams ·
  Thursday Finger Control Hell · Friday Techy — plus a one-click
  **"Import All 4 · Red's Recommended"** button that grabs all four into their
  own osu!lazer collections.
- **Skip-videos mode (on by default)** — downloads the no-video version of each
  map (~70% smaller for video maps); toggle it off in Settings to keep videos.
- **Bundled, R3D-themed Windows installer** — ships the Collection Manager CLI
  and installs the Edge WebView2 / .NET Framework runtimes only if missing.

### Changed

- Faster downloads: a 6th mirror (sayobot), higher per-mirror concurrency cap
  (8 → 12), more worker threads (24 → 48, ceiling 64) and a larger connection
  pool.
- Poster kicker recoloured for legibility over cover art.

## [1.0.2] — 2026-06-22

### Fixed

- **Auto-update loop.** The v1.0.1 installers were built from a commit that
  still reported `APP_VERSION = "1.0.0"`, so the in-app updater saw the v1.0.1
  release as newer than itself and re-prompted to update forever. Re-released
  with the version string corrected and the tag cut from the bumped commit.

## [1.0.1] — 2026-06-22

### Fixed

- **Collection Manager CLI console windows flashing on Windows.** Each
  probe/export/import call briefly popped a black console window in the
  windowed build; suppressed with `CREATE_NO_WINDOW`.

### Changed

- Hardened the lazer-collection merge so `.osdb` generation is guaranteed by
  the download engine itself whenever a merge is requested — not only at the
  UI layer — with regression tests so it can't silently regress again.

## [1.0.0] — 2026-06-22

The complete redesign. The Qt UI is gone; the app now renders an HTML/CSS/JS
frontend in a native [pywebview](https://pywebview.flowlib.org/) window, built
in the **R3D "Cherry"** design system. The proven download engine is unchanged
under the hood — it was simply decoupled from Qt so it can drive the new UI.

### Changed

- **Brand-new interface (R3D "Cherry").** Warm near-black theme, Big Shoulders
  display type, JetBrains Mono technical labelling, cherry-red accents and
  glow. Dark by default with a working, persisted **light toggle** (applied
  before first paint, no flash).
- **Effortless three-step flow.** Paste a link/ID → pick a collection → hit
  Download. The main screen is just those three things; every other option
  moved to the **Settings** tab.
- **Auto-everything.** The osu! folder is auto-scanned for existing lazer
  collections (no Refresh button), and the osu!lazer binary, `client.realm`,
  Collection Manager CLI, and output folder all auto-detect on launch.

### Added

- **Native installers for all three platforms**, built in CI and attached to
  tagged releases: Windows `Setup.exe` (Inno Setup — Start-Menu shortcut +
  uninstaller), macOS `.dmg`, and a Linux **AppImage**.
- **Built-in update checker.** On launch the app compares its version against
  the latest GitHub Release and shows an "⬆ Update to vX" pill; clicking it
  downloads the right installer for the OS and launches it.
- **Live poster previews.** Pasted collections render as cherry-duotone gig
  posters — cover art, collection name in huge condensed caps, `// N maps`.
- **Persistent download dock** with aggregate progress, current-file ticker,
  live counts, and a pulse while active.
- **Toasts** for scan/complete/error and a confetti finish on success.
- Activity log with colour-coded lines; "Open output folder" shortcut.

### Fixed

- **Merging into an osu!lazer collection silently did nothing** unless the
  "Generate .osdb" toggle was on. The merge step reads back the per-collection
  `.osdb` files, so generation is now forced whenever a merge is requested.
- Merge now runs only when **both** Collection Manager CLI and `client.realm`
  are present; otherwise the app still downloads + auto-imports and shows a
  clear warning instead of failing. Collection Manager CLI is auto-downloaded
  when a merge is wanted but it isn't installed (zero-setup merge on Windows).
- Linux CI build (PyGObject/pycairo needed cairo + pkg-config headers).

### Removed

- **PyQt6 dependency** and the entire Qt UI layer (`MainWindow`,
  `DownloadWorker` Qt signals, the QSS theme). Replaced by `pywebview` +
  `web/` assets and a Qt-free `Downloader` orchestrator.

## [0.9.1] — 2026-06-06

The "fix the failed imports + add mirrors" release.

### Fixed

- **osu!lazer "Beatmap import failed" spam.** Mirrors behind Cloudflare can
  answer a download with HTTP 200 but an HTML/JSON rate-limit page instead
  of the file; that got saved as a `.osz` and lazer then failed to import
  it. Downloads are now validated — the file must start with the ZIP magic
  (`PK`) and, when the server sends Content-Length, be complete — otherwise
  the mirror is skipped and another is tried. No more garbage handed to
  lazer.
- **Wrong osu.direct host.** The mirror was `api.osu.direct`, which doesn't
  resolve ("Failed to resolve 'api.osu.direct'"); the working host is
  `osu.direct/d/{id}`. Fixed — that mirror is usable again.
- **"Hard stuck after start", then crawling.** A single download could block
  a worker for up to 5 minutes when a mirror sent a long `Retry-After`. The
  per-set deadline is now 90 s (was 300), a rate-limit cooldown is capped at
  30 s no matter what the server asks, and each mirror starts at a gentler
  concurrency of 2 so the opening burst doesn't trip a 429 immediately.

### Added

- **Nekoha mirror** (`mirror.nekoha.moe`) — verified fast — joins the
  built-in pool (now catboy, nerinyan, osu.direct, nekoha, beatconnect).
- **Custom mirror URLs.** Advanced → Mirrors takes one URL template per
  line (`https://host/path/{id}`, or a base URL that gets `/{id}` appended);
  yours are tried before the built-ins. Mirrors are now URL templates, so
  endpoints with different path schemes work.

  (Sayobot was evaluated but only 302-redirects to a China-only CDN and
  didn't deliver bytes internationally, so it's left out of the defaults —
  add it as a custom mirror if it works for you.)

### Tests

- `tests/test_osz_validation.py`, `tests/test_mirror_templates.py` — non-osz
  / truncated rejection, template normalization, custom-mirror ordering, and
  the corrected default mirror set.

## [0.9.0] — 2026-06-06

The "max speed without rate-limiting" release. Downloads now self-tune to
exactly as fast as each mirror tolerates.

### Added

- **Adaptive per-mirror concurrency (AIMD, like TCP congestion control).**
  Each mirror has its own concurrency cap that probes upward while it stays
  healthy and halves the moment it returns a 429/403, then cools down
  briefly before recovering. There's no fixed parallel-download guess to
  tune — the app finds the maximum safe rate per mirror on its own and
  keeps the overall throughput at that edge.
- When every eligible mirror is momentarily at capacity or cooling down, a
  download now **waits for a slot rather than failing**, so high worker
  counts apply back-pressure instead of dropping maps.

### Changed

- Default worker threads raised 10 → 24; the per-mirror caps are the real
  governor now, so it's safe to leave the slider high. The "Parallel
  downloads" tooltip explains this.
- A 429 no longer fully blacklists a mirror for a minute — it just throttles
  it down and keeps using it at the lower rate, which sustains far more
  throughput when a mirror is merely busy rather than down.

  Benchmarked against mirrors with hard concurrency limits (2–6): 400/400
  sets downloaded, only ~4% of requests hit a 429 (each backed off cleanly),
  and every mirror's cap settled at or just below its true limit — the
  strict one self-throttling to a single connection.

### Tests

- `tests/test_adaptive_concurrency.py` — cap halving on 429, floor/ceiling
  clamping, slow upward probing, and cap-gated mirror selection.

## [0.8.1] — 2026-06-06

The "stop getting rate-limited" release — mirror handling reworked so load
spreads evenly and a throttled mirror is backed off instead of hammered.

### Added

- **Round-robin mirror rotation.** Each download now starts at the next
  mirror in sequence (catboy → nerinyan → osu.direct → beatconnect → …) so
  no single mirror takes all the requests — even at low parallelism. Within
  the rotated order it still picks the least-busy alive mirror and skips any
  that are blacklisted.

### Fixed

- **Rate-limited mirrors are now respected, not retried.** A 429 (or 403)
  used to fall into the generic retry path and hit the *same* throttled
  mirror up to 3× with backoff — deepening the rate-limit. It now blacklists
  that mirror process-wide (honouring the `Retry-After` header, capped at
  10 min) so every parallel slot backs off it, and falls straight through to
  the other mirrors.
- **High parallel-download counts were silently throttled.** The shared
  `requests.Session` pooled only 10 connections per host, so >10 parallel
  downloads to one mirror queued instead of running concurrently. The pool is
  now sized to the parallelism cap (32). Verified: 48 mock downloads run at a
  measured peak concurrency equal to the worker count (1→1, 8→8, 32→32),
  scaling wall-time linearly.

### Tests

- `tests/test_rate_limit.py`, `tests/test_round_robin.py` — 429 blacklist +
  fallthrough, `Retry-After` parsing, round-robin rotation across mirrors.

## [0.8.0] — 2026-06-06

The "UI overhaul" release — a from-scratch rebuild of the window layout and
theme, plus a download-coverage fix that recovers maps the old code wrongly
gave up on.

### Changed

- **Rebuilt the GUI.** Card-based layout with a clear header, a single
  download card (IDs, output, target collection, parallelism), a prominent
  cherry-red gradient primary button, and consistent button styling
  throughout. Replaces the cramped single-column form with tiny 9px labels.
- **Fonts are now point-sized**, so text stays a consistent physical size
  from 1080p through 4K instead of rendering tiny on high-DPI displays — the
  root of the old "doesn't scale" problem. A single base app font drives
  everything.
- **The window resizes cleanly.** The activity log lives in a stretchable
  card that absorbs extra vertical space (no more dead air), and the whole
  page sits in a scroll area so opening Advanced on a short window scrolls
  instead of squishing widgets on top of each other. The old
  grow-the-window-on-expand hack is gone.
- **Advanced settings** is now a tidy collapsible card grouped into Paths /
  Behaviour / Tuning, with clearer labels.

### Fixed

- **~half a collection showing as "not on mirror".** `BeatmapMirror.download`
  treated a 404 from the *first* mirror it tried as "this map doesn't exist"
  and gave up — but mirrors have different coverage, so a 404 on catboy.best
  doesn't mean the set is gone. It now falls through to the other mirrors and
  only reports "not on mirror" once every mirror has been tried. (Maps still
  missing after the fix usually mean a mirror is unreachable on your network —
  e.g. a DNS failure resolving one of the mirror hosts.)

### Tests

- `tests/test_mainwindow_smoke.py` — constructs the real MainWindow headlessly,
  asserts every worker/handler/settings-wired widget still exists, and checks
  the advanced toggle, target picker, and start-button gating.

## [0.7.2] — 2026-06-05

### Fixed

- **Windows: `[WinError 183] Cannot create a file when that file already
  exists` on download.** `BeatmapMirror.download` finished each `.osz` by
  renaming the `.part` temp onto the final name with `Path.rename`, which on
  Windows refuses to overwrite an existing file — so any beatmapset already on
  disk from a prior run failed. Now skips the body download entirely when a
  complete `.osz` is already present, and uses `os.replace` (atomic overwrite on
  all platforms) for the temp→final move otherwise.

### Tests

- `tests/test_download_rename.py` — fresh download writes the file with no
  leftover `.part`; a pre-existing complete `.osz` is returned without
  re-streaming or a rename conflict.

## [0.7.1] — 2026-06-05

### Fixed

- **Maps not importing into osu!lazer on Windows.** Current osu!lazer ships
  with the Velopack updater, which keeps the live executable in
  `%LOCALAPPDATA%\osulazer\current\osu!.exe`. `OsuLazerImporter._locate_binary`
  only knew the legacy Squirrel (`app-X.Y.Z\`) and top-level layouts, so on any
  recent install it found nothing, `self.importer.binary` was `None`, and the
  auto-import path no-opped **silently** — beatmaps downloaded fine but never
  reached the game. Now checks the Velopack `current\` folder first (preferred
  over any stale `app-*` folder), keeping the legacy paths as fallbacks.
- **Silent auto-import failure is now loud.** When auto-import is enabled but no
  osu!lazer executable can be found, the run log prints an explicit warning with
  the expected path instead of quietly importing nothing.

### Tests

- `tests/test_locate_lazer.py` — covers Velopack `current\`, legacy Squirrel
  `app-*\`, the current-over-legacy preference, and the not-installed case.

## [0.7.0] — 2026-05-12

The "actually looks good now" release. Replaces the dense scrolling QFormLayout stack from v0.5.0 with a single-page progressive-disclosure layout themed in Cherry red on a dark base. Functional behavior (download, probe, merge, mirrors) is unchanged — this is purely structure + styling.

### Changed

- **Layout** — single-page progressive disclosure. Main view shows only the essentials (collection IDs, output, add-to picker, two parallelism spinboxes, Start, status, progress, log). Everything else (paths, behavior toggles, import delay, realm-recovery) lives behind a collapsible "Advanced" expander that's closed by default.
- **Theme** — module-level QSS applied to QApplication. Cherry red accent (#e3344f → #ffa15f gradient on Start button and progress bar) on a #1e1e26 surface. Custom-styled QSpinBox arrows, QCheckBox indicators, scrollbars, and dropdowns. Title bar reads "osu-collector-gui by Red".
- **Default window size** 900×950 → 520×680 (480×500 min). The QScrollArea wrap from v0.5.0 is gone — the layout fits.
- **Default picker** is "(one collection per osu!collector collection)" — preserves v0.6.x merge-by-default. "Don't merge" is now an explicit option in the picker.
- **Start button** disabled until at least one non-whitespace character appears in the Collection IDs field. Replaced with a neutral-styled Cancel button during a run.
- **Log box** always visible (~110px, monospace 11px) with idle placeholder "Ready. Paste a collection ID above and click Start to begin."

### Removed

- **"Download beatmaps" toggle** — always on. Disabling it disabled the core feature, so it was dead UI.
- **"Add downloaded maps to osu!lazer collections" master toggle** — subsumed by the "Don't merge" option in the Add-to picker.
- **Per-beatmap progress bar** — redundant with the per-line log output.
- **`showEvent` / `_recompute_scroll_layout` machinery** from v0.6.1 — no scroll area means no scroll-recomputation needed.

### Other

- New `advanced_expanded` settings key persists whether the Advanced section was open at last close. New users default to collapsed.
- New `tests/test_main_window.py` with 8 unit tests for the pure-function UI helpers (`should_enable_start`, target-combo sentinel labels).

## [0.6.2] — 2026-05-12

### Fixed

- **Downloads now spread load across mirrors intelligently.** Previously every parallel download slot started by trying catboy.best, so if catboy was rate-limiting the user's IP all 10 slots would each pay a full TCP-connect timeout (~10s × 3 retries) before falling back. Now `BeatmapMirror` picks the least-busy alive mirror for each new download — when catboy is healthy and fast its active count drops to 0 immediately so it stays preferred; when catboy stalls or its connections pile up, load shifts to nerinyan / osu.direct / beatconnect automatically. No magic numbers, no UI controls, no user-visible behavior changes when mirrors are working normally.

## [0.6.1] — 2026-05-12

### Fixed

- **"Have to resize the window to see content" on first open.** The QScrollArea wrapping the form would underestimate its content height during Qt's initial layout pass, leaving some sections invisible until the user manually resized the window. Now `MainWindow.showEvent` schedules a deferred `updateGeometry()` + `adjustSize()` on the scroll area's inner widget via `QTimer.singleShot(0, …)`, which runs after the layout has settled. Especially load-bearing on Windows where the configure-notify timing differs from Linux.
- **Windows DPI scaling at 125% / 150%.** Qt 6's default `Round` rounding policy was snapping non-integer scaling factors to the nearest integer, producing 1-pixel-off widget heights that compounded across nested forms. Now uses `PassThrough` so Qt honors the OS's exact DPI factor. Set before `QApplication` is constructed, as required.
- **Root scroll-content vertical size policy** changed from `Preferred` to `Minimum` so its `sizeHint()` reflects actual minimum content height (matters for some compositor configurations).

## [0.6.0] — 2026-05-12

The "stop redownloading maps I already have" release. Huge osu!collector collections (e.g. 17391 with ~11k maps) now skip downloading any beatmapset where lazer already has at least one diff imported — but still compose the full collection in lazer.

### Added

- **Skip beatmapsets already imported in osu!lazer.** New checkbox under "Lazer collections". Before downloads start, Collection Manager CLI probes lazer's BeatmapInfo DB (`cm.exe create -b <bids> -l <realm-parent>`) to learn which beatmap_ids it has. Sets with at least one imported diff are skipped; their md5s still land in the resulting lazer collection so it composes correctly. Bonus: the .osdb is written using lazer's current md5 for resolved maps so collection entries aren't "ghost" rows when the mapper has updated diffs.
- **Configurable parallel download count.** New "Parallel downloads" spinbox (1..32, default 4 to preserve previous behavior). Above ~8, requests round-robin across the three configured mirrors (catboy.best / nerinyan.moe / osu.direct) so a single mirror doesn't take all the load.
- **Mirror round-robin** in `BeatmapMirror.download()` — each `set_id` picks a different primary URL via `set_id % len(urls)`, with the other mirrors retained as fallbacks. Disabled by passing `round_robin=False` to the constructor.

### Other

- New `tests/` directory with pytest unit tests for the writer/reader prefer-md5 path, mirror URL rotation, and CM CLI probe (mocked subprocess). `requirements-dev.txt` pins pytest.

## [0.5.0] — 2026-04-09

The "actually merges into osu!lazer" release. Going from "downloads beatmaps" to "downloads beatmaps, generates `.osdb`, **and** writes them into your live `client.realm` non-destructively, then relaunches lazer for you" took most of a day of debugging .NET unhandled exceptions under wine, and it finally works end-to-end.

### Added

- **Merge downloaded collections directly into osu!lazer.** New "Add downloaded maps to osu!lazer collections" section: pick an existing collection from a refreshable dropdown, create a new one inline, or let each osu!collector collection become its own lazer collection. Uses Collection Manager CLI under the hood as a Realm codec, with all the merging done in Python so existing collections are preserved.
- **`.osdb` file generation** in the o!dm8 (gzip) format that Collection Manager itself emits. Works as a generic export even if you don't use the merge feature.
- **Auto-detect of `client.realm`** based on platform — `%APPDATA%\osu` on Windows, `~/.local/share/osu` on Linux, `~/Library/Application Support/osu` on macOS.
- **Auto-download Collection Manager CLI** on first use if it's not already installed (~3.6 MB from CM's GitHub releases). Cached to `~/.cache/osu-collector-gui/cm-cli/` (Linux) or `%LOCALAPPDATA%\osu-collector-gui\cm-cli\` (Windows). On Linux + wine flatpak, the necessary `flatpak override` runs automatically.
- **"Recover realm from backup…" button** that lists every `client.realm.bak-<timestamp>` snapshot with date and size. Restoring also takes a fresh `before-recover-<timestamp>` copy of the current state, so the action is two-way reversible.
- **Per-collection target picker** with collision policy (merge, skip, rename) — "Refresh" populates it from your live realm via CM CLI.
- **"Continue merge?" confirmation prompt** that pauses the worker after auto-imports finish, so you can verify in lazer that all the .osz files are imported before the destructive realm rewrite kicks in.
- **Auto-cleanup** of `<id> - <name>/` download folders after import is confirmed done. The `db/` subfolder, anything containing `.realm`, hidden temp dirs, and non-matching folders are all left untouched.
- **Persistent debug log** at `/tmp/oc-cm-cli-debug.log` (Linux/macOS) or `%TEMP%\oc-cm-cli-debug.log` (Windows) capturing every CM CLI invocation's full stdout + stderr.
- **Settings persisted on window close**, not just on Start click — adjustments stop disappearing if you close without running a download.
- **Title bar** now reads `osu-collector-gui v0.5.0 by Red`.

### Fixed

- **AppImage relaunch failure**. The kill step recorded `psutil.exe` which on a running AppImage is `/tmp/.mount_<hash>/usr/bin/osu!` — a path that gets unmounted the moment the AppImage exits. Relaunch then failed with `ENOENT`. Now reads `$APPIMAGE` env var → `cmdline[0]` → exe symlink (skipping `/tmp/.mount_*` paths) so the relaunch always uses the persistent `.AppImage` file.
- **Stale `.osdb` files getting merged into the realm**. Previously the merge step did `output_dir.rglob("*.osdb")` and silently scooped up `.osdb` files left over from previous batches — a user downloading 1 collection ended up with 13 unrelated collections in lazer. Now tracks every `.osdb` written this run and merges only those.
- **`OsdbWriter` produced files CM CLI couldn't read.** Previous v6 (uncompressed) format triggered `System.IO.EndOfStreamException` deep inside CM's reader because the v6 path is not actively maintained — CM only writes v8 internally. Now writes o!dm8 (gzip-compressed, with `OnlineId` field) which CM reads through its well-tested code path.
- **Destructive merge wiping the realm on a failed export.** A previous version had `except: existing = []` which let an empty/unreadable export silently turn into "you have no existing collections", and the subsequent CM write deleted everything. The merge is now fail-closed: any export problem aborts the run with a clear error and the realm is untouched. A multi-MB realm that exports zero collections is also rejected as a sanity check.
- **`client.realm` never properly released after kill.** `psutil.terminate()` returns instantly but lazer takes a moment to flush its Realm state. Now `wait_procs(timeout=15)` blocks until the processes actually exit, escalating to SIGKILL after 15s, with an additional 2s grace period for kernel handle release. Stale `client.realm.lock` and `client.realm.note` files are also cleaned up.
- **Refresh button hung waiting for `osu!lazer` to be closed.** Now snapshots `client.realm` to a sibling `.snapshot.realm` and reads the COPY — lazer can stay open while you browse collections. Realm is MVCC so the file copy is a consistent point-in-time snapshot.
- **Wine sandbox + temp paths.** CM CLI invoked through the wine flatpak couldn't write to `/tmp` (sandbox can only see directories explicitly granted to it). All temp output is now placed next to `client.realm`, which is always wine-accessible because lazer runs from there.
- **CM CLI command field broke on paths with spaces.** `raw.split()` mangled `Collection Manager` and `shlex.split` ate backslashes from unquoted Windows-style paths. Settings now persist `cm_cli_command` as a JSON list — no string-parsing round trip — and the QLineEdit only quotes for display.
- **CM CLI Realm.NET double-open crash (`0xe0434352`)**. Passing both `-i` and `-l` made CM open `client.realm` twice. Now always passes `-s` (SkipOsuLocation) — the input file is enough.
- **`shlex.join` for the displayed command** so paths with spaces survive the round trip into and out of settings.
- **`xdg-document-portal` FUSE wedge bricking flatpak launches.** Documented; partial workaround via the existing `fix-portals` helper script.
- **Realm path field defaulted to Linux path on Windows.** Now picks the right location per platform.

### Other

- Added `CHANGELOG.md` (this file).
- Title bar version, app version, settings file version, and release tag are all in sync.

## [0.2.0] — 2026-04-09

- Initial public release.
- Single-file Python rewrite using PyQt6 + requests, replacing the original bash + kdialog + Python PTY-driver stack that drove `osu-collector-dl` interactively.
- Talks to osu!collector's HTTP API directly and downloads `.osz` files from `catboy.best` with `nerinyan.moe` and `osu.direct` as fallbacks.
- Per-collection and per-beatmap progress bars in a native Qt window.
- Auto-import each downloaded `.osz` into a running `osu!lazer` instance (Linux AppImage / Flatpak / Windows Squirrel install / macOS app bundle).
- Settings persistence at `~/.config/osu-collector-gui/settings.json`.
- GitHub Actions workflow that builds the Windows `.exe` with PyInstaller and attaches it to releases tagged `v*`.

## [0.1.0] — 2026-04-09

- First commit. Smoke test of the architecture: PyQt6 + requests + threading + a single download path. Dropped almost immediately in favour of 0.2.0.
