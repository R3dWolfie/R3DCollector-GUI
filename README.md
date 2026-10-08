# Osu-Collector-GUI

[![Build Windows .exe](https://github.com/R3dWolfie/R3DCollector-GUI/actions/workflows/build-windows.yml/badge.svg)](https://github.com/R3dWolfie/R3DCollector-GUI/actions/workflows/build-windows.yml)

A cross-platform GUI for downloading [osu!collector](https://osucollector.com/) collections, with live progress and optional auto-import to **osu!lazer**. Built in the **R3D "Cherry"** design system — a warm near-black look with industrial-condensed display type and monospace technical labelling.

> **Windows users**: pre-built `.exe` is on every [GitHub Actions](https://github.com/R3dWolfie/R3DCollector-GUI/actions) run as a build artifact, and tagged releases get a published binary on the [Releases](https://github.com/R3dWolfie/R3DCollector-GUI/releases) page.

The interface is an HTML/CSS/JS frontend rendered in a native [pywebview](https://pywebview.flowlib.org/) window, sitting on top of a pure-Python download engine. It talks to osu!collector's HTTP API directly and downloads `.osz` files from public osu! mirrors. Runs on **Linux**, **Windows**, and **macOS**.

## The flow (it's meant to be effortless)

1. **Paste** an osu!collector link or ID (as many as you like).
2. **Pick** the lazer collection to import into — existing ones are auto-scanned, or create a new one.
3. **Hit Download.**

Everything else — your osu!lazer binary, `client.realm`, the Collection Manager CLI, the output folder, sensible parallelism — auto-detects. Power-user knobs all live behind the **Settings** tab.

## Features

- Paste any number of collection links/IDs at once; live **poster previews** of each collection
- Auto-scans your osu! folder for existing lazer collections to merge into
- Persistent **download dock** with live progress; per-collection / per-beatmap counts
- Auto-import each downloaded `.osz` into a running **osu!lazer** instance
- Dark theme by default with a working **light toggle** (persisted)
- Live activity log, toasts, and a satisfying finish
- Cancel button (stops cleanly between beatmaps)
- Optional `.osdb` generation and consolidation into a single `db/` subfolder
- **Native installer** for **Windows** (`Setup.exe`, bundles the Collection Manager CLI + auto-installs WebView2/.NET if missing) and a **macOS** `.dmg`, with a **built-in update checker** that one-click installs new releases. On **Linux**, [run from source](#run-from-source-any-os) — the reliable path across distros

## Download & install

Most people want the prebuilt release — no Python, no terminal.

**→ Grab the latest from the [Releases page](https://github.com/R3dWolfie/R3DCollector-GUI/releases/latest).**

| Platform | Download | Setup |
|---|---|---|
| **Windows 10 / 11** | `Setup.exe` | Run it. The installer bundles the Collection Manager CLI and auto-installs WebView2 + the .NET 9 runtime if they're missing. Nothing else to do. |
| **macOS** | `.dmg` | Drag to Applications. First launch: **right-click → Open** (the app is unsigned). Collection *merging* needs one extra step — see [Collection merging off Windows](#collection-merging-off-windows-linux--macos). |
| **Linux** — Debian / Ubuntu / Mint / Fedora | `.AppImage` | `chmod +x ...AppImage` and run it. For *merging*, see [below](#collection-merging-off-windows-linux--macos). |
| **Linux** — Arch / other rolling or bleeding-edge | *run from source* | The AppImage's bundled WebKit breaks on newer/rolling distros — [run from source](#run-from-source-any-os) instead. It's three commands. |

### What works everywhere vs. what needs setup

The app has two halves, and it's worth knowing which is which:

- **Core — download maps + auto-import them into osu!lazer's library.** Pure Python on top of your system's browser engine. Works on **every** platform with **zero** extra setup.
- **Collection organizing — putting maps into a *named* collection, merging into existing ones, the import-dropdown list, and the Export tab.** Handled by the [Collection Manager CLI](https://github.com/Piotrekol/CollectionManager), which is a **Windows-only .NET 9** tool (its Realm reader is a Windows-native library — there is no Linux/macOS build). On **Windows** it's bundled and fully automatic. On **Linux/macOS** it runs through the **WineHQ flatpak**, which is a one-time setup.

So: downloading collections into your lazer library *just works* anywhere. Organizing them into named collections needs Windows, or wine elsewhere.

## Run from source (any OS)

```sh
git clone https://github.com/R3dWolfie/R3DCollector-GUI.git
cd R3DCollector-GUI
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python osu_collector_gui.py
```

**Linux** renders into the host's **WebKitGTK**, which is tightly tied to the system
GObject stack, so it needs the system bindings and a venv that can see them:

```sh
# 1. System packages — WebKitGTK + GTK3 + PyGObject:
#   Arch:          sudo pacman -S webkit2gtk-4.1 gtk3 python-gobject
#   Debian/Ubuntu: sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi python3-gi-cairo
#   Fedora:        sudo dnf install webkit2gtk4.1 gtk3 python3-gobject

# 2. Venv that can see the system GObject bindings, then run:
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
python osu_collector_gui.py
```

Windows uses the built-in **Edge WebView2** runtime and macOS uses system **WebKit**, so the prebuilt installers there need no extra backend setup.

## Collection merging off Windows (Linux / macOS)

Downloads and auto-import work with no extra steps. To also **list existing
lazer collections and merge into them**, install the wine-hosted Collection
Manager CLI once. On **Linux** a script does the whole thing — WineHQ flatpak +
.NET 9 runtime + filesystem permissions for your osu! data:

```sh
scripts/setup-linux.sh            # or: scripts/setup-linux.sh /path/to/osu/data
```

Then relaunch the app and your collections appear in the import dropdown. On
**Windows** the installer already handles all of this; nothing to run.

## Troubleshooting

- **My whole PC lags while downloading.** High parallelism plus live auto-import
  hammers disk/CPU. In **Settings → Tuning**, click the **Gentle** speed preset
  (or lower *Parallel downloads*), and/or turn off **Auto-import** so osu!lazer
  isn't importing at the same time. The lag is only during a run.
- **Linux: the AppImage won't start** (WebKit / "failed to spawn" errors,
  common on Arch and other rolling distros). [Run from source](#run-from-source-any-os) —
  it uses your system's own WebKit and is reliable across distros.
- **Linux: "Application Crash · wine-preloader" / collections stopped listing
  after a system update.** A freedesktop flatpak-runtime update can break the
  WineHQ flatpak (its `services.exe` crashes in the new libc). Roll the runtime
  back and pin it:
  ```sh
  # find the previous commit:
  flatpak remote-info --user --log flathub org.freedesktop.Platform/x86_64/25.08
  # roll back to it, then mask so updates can't re-break it:
  flatpak update --user --commit=<PREVIOUS_COMMIT> org.freedesktop.Platform/x86_64/25.08
  flatpak mask  --user org.freedesktop.Platform//25.08
  # later, to allow updates again: flatpak mask --user --remove org.freedesktop.Platform//25.08
  ```

## Build standalone binaries (Windows · macOS · Linux)

See [BUILD.md](BUILD.md). The included GitHub Actions workflow builds all
three at once on every push/PR; manual per-OS PyInstaller commands are
documented there too.

## Linux: add to your application menu

Drop the included `osu-collector-gui.desktop` into `~/.local/share/applications/` and update the `Exec=` line to point at your install.

## Auto-import to osu!lazer

If `osu!lazer` is running when downloads start, each `.osz` is sent to it as soon as it lands. The importer auto-detects the lazer binary in standard locations:

- **Linux**: `~/Applications/osu*.AppImage` or the Flatpak shim `sh.ppy.osu`
- **Windows**: `%LOCALAPPDATA%\osulazer\osu!.exe`
- **macOS**: `/Applications/osu!.app`

## Acknowledgements

- [osu!collector](https://osucollector.com/) for the API and the collections themselves
- [catboy.best](https://catboy.best/), [nerinyan.moe](https://api.nerinyan.moe/), [osu.direct](https://osu.direct/) for the beatmap mirrors
- Inspired by [roogue/osu-collector-dl](https://github.com/roogue/osu-collector-dl)

## License

MIT — see [LICENSE](LICENSE).
