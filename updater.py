"""Update checks against GitHub Releases.

Each release on https://github.com/265ada/aac-map-overlay/releases carries
AACMapOverlay-Setup.exe. The overlay asks GitHub for the latest release, and
when it is newer than the running version it downloads that installer and runs
it silently. The installer closes the overlay, replaces the files and starts
the new version again. Settings live in %APPDATA% and are kept.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

from version import __version__

REPO = "265ada/aac-map-overlay"
ASSET = "AACMapOverlay-Setup.exe"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UA = {"User-Agent": "AACMapOverlay-updater", "Accept": "application/vnd.github+json"}


def parse(v):
    """'v1.2.3' -> (1, 2, 3); anything odd sorts as oldest."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(v).strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def can_self_update():
    """Only the installed .exe can replace itself; source checkouts just get a link."""
    return bool(getattr(sys, "frozen", False))


def check(timeout=10):
    """Return {'version', 'url', 'page', 'notes'} when a newer release exists, else None."""
    req = urllib.request.Request(API, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    if rel.get("draft") or rel.get("prerelease"):
        return None
    latest = rel.get("tag_name", "")
    if parse(latest) <= parse(__version__):
        return None
    asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
    return {"version": latest.lstrip("v"), "url": asset and asset["browser_download_url"],
            "page": rel.get("html_url") or RELEASES_PAGE, "notes": (rel.get("body") or "")[:600]}


def download_and_run(info):
    """Download the new installer and start it silently. The caller should quit right after."""
    if not info.get("url"):
        raise RuntimeError("release has no installer attached")
    dest = pathlib.Path(tempfile.gettempdir()) / f"AACMapOverlay-Setup-{info['version']}.exe"
    req = urllib.request.Request(info["url"], headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    if dest.stat().st_size < 1_000_000:
        raise RuntimeError("download looks incomplete")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlive us
    subprocess.Popen([str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                      f"/LOG={os.path.join(tempfile.gettempdir(), 'AACMapOverlay-update.log')}"],
                     creationflags=flags, close_fds=True)
