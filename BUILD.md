# Building osu!collector-gui

The app is a single Python file (`osu_collector_gui.py`) plus an HTML/CSS/JS
frontend in `web/`. The GUI renders through **pywebview**, which uses each
platform's native webview — so there's no Qt/Electron runtime to bundle and
the binaries stay small.

| OS | Webview used | Extra runtime for the user |
|----|--------------|----------------------------|
| Windows | Edge **WebView2** | none (ships with Win10/11) |
| macOS | system **WebKit** | none |
| Linux | **WebKit2GTK** | `webkit2gtk` + GObject introspection must be installed |

The two things every build must get right:

1. **Bundle the `web/` folder** (`--add-data`). The app finds it via
   `sys._MEIPASS` when frozen. The separator is `;` on Windows and `:` on
   macOS/Linux.
2. **Use the right icon format**: `.ico` (Windows), `.icns` (macOS). Both are
   pre-generated in `packaging/`.

---

## Easiest path: GitHub Actions (all three at once)

`.github/workflows/build.yml` builds Windows, macOS, and Linux on every push
to `main`, every PR, and on demand:

1. Push your branch / open a PR, **or** go to the **Actions** tab → *Build
   (Windows · macOS · Linux)* → **Run workflow**.
2. When it finishes, download the artifacts from the run:
   - `osu-collector-gui-windows-setup` → `osu-collector-gui-Setup.exe` (Inno installer)
   - `osu-collector-gui-macos-dmg` → `osu-collector-gui.dmg`
   - `osu-collector-gui-linux-appimage` → `osu-collector-gui-x86_64.AppImage`
3. Tagging a release (`git tag v1.0.0 && git push --tags`) additionally
   publishes all three to a GitHub **Release** — which is also what the app's
   built-in update checker reads (see *Auto-update* below).

## Auto-update

On launch the app calls the GitHub Releases API, compares the latest tag to
its own `APP_VERSION`, and — if a newer version exists — shows an **"⬆ Update
to vX"** pill in the top bar. Clicking it downloads the right installer for
the current OS (Setup.exe / .dmg / .AppImage) and launches it; the Windows
installer is built with `CloseApplications=yes` so it can replace the running
exe. Releases must therefore be **tagged `vX.Y.Z` matching `APP_VERSION`** and
carry the three installer assets (the workflow attaches them automatically).

---

## Building manually

Each binary must be built **on its own OS** (PyInstaller does not
cross-compile). Common first steps:

```sh
git clone https://github.com/R3dWolfie/R3DCollector-GUI.git
cd R3DCollector-GUI
python3 -m venv .venv
# activate it (see per-OS lines below), then:
pip install -r requirements.txt
pip install pyinstaller
```

### Windows

```bat
.venv\Scripts\activate
pyinstaller --noconfirm --windowed --onefile ^
    --name osu-collector-gui ^
    --icon packaging\icon.ico ^
    --add-data "web;web" ^
    osu_collector_gui.py
```

Output: `dist\osu-collector-gui.exe`. Double-click to run. (Unsigned, so
SmartScreen warns on first launch — that's expected for an unsigned build.)

### macOS

```sh
source .venv/bin/activate
pip install pyobjc-framework-WebKit pyobjc-framework-Cocoa
pyinstaller --noconfirm --windowed \
    --name osu-collector-gui \
    --icon packaging/icon.icns \
    --add-data "web:web" \
    --osx-bundle-identifier com.r3d.osucollectorgui \
    osu_collector_gui.py
```

Output: `dist/osu-collector-gui.app`. First launch: right-click → **Open**
(Gatekeeper blocks unsigned apps on double-click). To share it, zip the
bundle: `cd dist && zip -r osu-collector-gui-macos.zip osu-collector-gui.app`.

### Linux

Install the webview backend's system libraries first (Debian/Ubuntu shown):

```sh
sudo apt install -y libgirepository1.0-dev gir1.2-webkit2-4.1 \
                    gir1.2-gtk-3.0 python3-gi python3-gi-cairo
source .venv/bin/activate
pip install "pywebview[gtk]"
pyinstaller --noconfirm --windowed --onefile \
    --name osu-collector-gui \
    --add-data "web:web" \
    osu_collector_gui.py
```

Output: `dist/osu-collector-gui`. Run it with `./dist/osu-collector-gui`.

> **Linux note:** the binary does **not** bundle GTK/WebKit2 — those are huge
> system libraries. The target machine must have `webkit2gtk` installed
> (most desktops do; otherwise `sudo apt install gir1.2-webkit2-4.1`). If you
> want a fully self-contained artifact, package an **AppImage** or just run
> from source (`python osu_collector_gui.py`).

---

## Running from source (no build)

The simplest option on any OS — see the README's *Install (from source)*.

```sh
pip install -r requirements.txt   # Linux: also pip install "pywebview[gtk]"
python osu_collector_gui.py
```

---

## Troubleshooting

- **Blank window / "Frontend assets not found"** — the `web/` folder wasn't
  bundled. Re-check the `--add-data` flag and its separator (`;` vs `:`).
- **macOS: "app is damaged / can't be opened"** — Gatekeeper on an unsigned
  app. `xattr -dr com.apple.quarantine dist/osu-collector-gui.app`, or
  right-click → Open.
- **Linux: `ModuleNotFoundError: gi` or no window** — the GTK backend system
  packages above aren't installed, or you skipped `pip install
  "pywebview[gtk]"`.
- **Code-signing** — all builds are unsigned by default. For wider
  distribution, sign with `signtool` (Windows) or `codesign`/notarization
  (macOS).
