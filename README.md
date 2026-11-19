# AAC Map Overlay

An always-on-top map window for **ArcheAge Chronicles**. It follows your character live,
zooms itself to the pins around you, and marks the things you've visited.

![AAC Map Overlay icon](assets/icon.png)

The map, markers and live position come from [The Hidden Gaming Lair (TH.GL)](https://archeagechronicles.th.gl/).
This overlay puts their map in a small, see-through window with its own controls and extras.

## Install

1. Download **AACMapOverlay-Setup.exe** from the [latest release](https://github.com/265ada/aac-map-overlay/releases/latest).
2. Run it. It installs just for you (no admin prompt) and adds a Start menu shortcut.
   - Windows may show **"Windows protected your PC"** because the app isn't code-signed.
     Click **More info → Run anyway**.
3. Run the game in **borderless windowed** mode so the overlay can sit on top of it.

Prefer no installer? Download **AACMapOverlay-portable.zip**, unzip it anywhere and run `AACMapOverlay.exe`.

Needs Windows 10 or 11 (64-bit). The Edge WebView2 runtime it uses is already part of Windows.

## Updates

The overlay checks GitHub for a new release every time it starts and every 5 minutes while it runs.

- **At start:** a new version downloads and installs by itself, then the overlay reopens.
- **While running:** a banner offers **Update** or **Later**, so it never restarts in the middle of play.

Your settings are kept. To turn automatic installs off, set `"autoUpdate": false` in
`%APPDATA%\AACMapOverlay\settings.json`; you'll still get the banner.

## Live position (follow your character)

The overlay gets your position from TH.GL's own desktop app through their **Peer Link**. It never reads
the game itself.

1. Install and start the [TH.GL app](https://www.th.gl/apps/archeage-chronicles) with the game running.
2. In the TH.GL app's map, open **Peer Link**, click **Generate**, then **Join Peer Mesh**, and copy the code.
3. In the overlay, turn on **📡 Live**, open **Peer Link** on the map, paste the code and click **Join Peer Mesh**.
4. Under **Apps**, click your game to make it **Me**. The map now follows you.

Both sides remember the code, so you only do this once.

## Features

The overlay looks like a plain map: its bars and footer, and TH.GL's map buttons, only appear while you hover over it and it's unlocked. Locking closes any open panels (markers, settings, progress, TH.GL's Filters); unlocking opens them again.


| | |
|---|---|
| 📡 Live | TH.GL's full map with your live position (Peer Link, above) |
| 🔍 Smart zoom | Zooms in hard on tight clusters of pins you still have to discover and out in open areas, only while your character is moving. Standing still or a maximized overlay never changes the zoom, so you can look around and plan a route. Scrolling or dragging the map yourself pauses it for 10 seconds |
| ✓ Auto-discover | Stand within range of a pin (8 m by default; pick 5–30 m with the **✓ 8 m** menu in the top bar) for 2 seconds and it's marked auto-discovered, the same way TH.GL's app marks it. The footer shows the nearest unmarked pin and a countdown. Pins that move (monsters), boss spawns and quest givers are skipped. Only pins your filters show are checked, so turn on Ores, Plants and Trees to have gathering nodes marked |
| 🎯 Pointer | Pick a target (nearest collectible, boss, dungeon, Nui, resource, ore, plants, trees, or one type like Coilstones). A dashed guide line runs from you to it on the map, with an arrow at the edge when it's off-screen. The next nearest ones beyond the map's edge get small compass arrows with their distance along the border, so you can see which way the others are. An arrow and distance at the bottom of the map point to the nearest unmarked one, for example "Raeven's Coilstone · 140 m NE", even while the overlay is locked |
| 📊 Progress | Collected / total for every collectible and resource type the map shows, plus this session: time played, distance walked and what you marked |
| 🧭 Profiles | **Gathering** (resources on, 12 m range, closer zoom, pointer to the nearest resource) and **Exploring** (collectibles on, 8 m range, wider zoom, pointer to the nearest collectible). **Manual** puts your own filters, pointer and range back. **Ctrl+Alt+P** cycles them |
| ⭕ Round map | A round minimap that turns with your facing; the corners are see-through |
| ⚙ Settings | Sound when an unmarked pin comes in range; show the overlay only while the game is running and in front (Ctrl+Alt+M shows it anyway, for example to look at the map without the game); low-power drawing while you stand still (all on by default) |
| 🛡 Ad blocking | Brave's ad-block engine with EasyList and EasyPrivacy, refreshed weekly. The footer counts what was blocked. Turn it off with `"adblock": false` in settings.json |
| Map and Markers menus | Auroria or any of the 8 dungeon maps, plus marker presets (normal mode) |
| ☰ | Pick marker types one by one (normal mode) |
| Slider | Opacity, 20–100% |
| ⛶ / 🗗 | Maximize to the monitor the overlay is on, then back to its previous size and place |
| 🔒 | Lock: clicks pass through to the game |

### Hotkeys (work while the game has focus)

| | |
|---|---|
| **Ctrl+Alt+M** | Show / hide the overlay. While the game isn't in front, it shows the overlay anyway until the game is back |
| **Alt+L** | Lock / unlock. Locking hides every bar (map only, clicks go to the game); unlocking brings the bars back. The overlay reopens locked or unlocked, the way you left it |
| **Ctrl+Alt+H** | Hide / show the top bars. While they're hidden, drag the thin strip at the top edge to move the window |
| **Ctrl+Alt+P** | Next activity profile (Manual → Gathering → Exploring) |
| **Ctrl+Alt+↑ / ↓** | More / less opaque |

TH.GL's Filters panel starts closed each time the overlay opens. Drag the title to move the window and the corner grip to resize it. Settings (map, markers, opacity,
position, size and the toggles above) are saved in `%APPDATA%\AACMapOverlay\settings.json`.

## Is it safe with the game's anti-cheat?

The overlay is an ordinary window on top of the game. It doesn't read game memory, inject anything or
send input to the game. Live position comes from TH.GL's app, so the same considerations as using
that app apply.

## Build it yourself

Needs Python 3.12 and, for the installer, [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```bash
python -m pip install -r requirements.txt pyinstaller
```

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

The app lands in `dist\AACMapOverlay\` and the installer in `dist\AACMapOverlay-Setup.exe`.
To run from source instead, double-click `run.bat`.

### Releasing a new version

1. Change `__version__` in `version.py` (for example `"1.0.1"`) and commit.
2. Tag and push:

```bash
git tag v1.0.1
```

```bash
git push origin main v1.0.1
```

GitHub Actions builds the installer and portable zip and publishes the release. Installed overlays
pick it up on their next start.

## Files

| | |
|---|---|
| `overlay.py` | The window, hotkeys, settings and update checks |
| `ui.html` | The overlay's controls around the map |
| `page_tools.js` | Runs inside TH.GL's map: smart zoom, auto-discover, header hiding |
| `adblock_hook.py` | Ad and tracker blocking for the overlay's browser |
| `updater.py`, `version.py` | Update checks against GitHub Releases |
| `installer.iss`, `build.ps1`, `.github/workflows/release.yml` | Packaging and releases |

## Credits

Map, markers and Peer Link by [The Hidden Gaming Lair](https://www.th.gl/), whose link stays visible under
the map as their embed terms ask. If the map helps you, consider
[supporting TH.GL](https://www.th.gl/support-me). ArcheAge Chronicles and its names, images and data
belong to their owners. This project isn't affiliated with TH.GL or the game's makers.

Released under the [MIT License](LICENSE).
