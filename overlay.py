"""ArcheAge Chronicles map overlay.

A small always-on-top window that shows The Hidden Gaming Lair's official
map embed (https://www.th.gl/developers) with our own controls:
map and marker pickers, opacity, click-through lock and global hotkeys.

Hotkeys (work while the game has focus):
  Ctrl+Alt+M      show / hide the overlay
  Alt+L           lock / unlock (locked = clicks go through to the game)
  Ctrl+Alt+Up     more opaque
  Ctrl+Alt+Down   more transparent
  Ctrl+Alt+H      hide / show the top bars

The overlay never reads or touches the game: it is only a window on top.
Live position comes from TH.GL's own app through Peer Link.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import pathlib
import sys
import threading
import time
import webbrowser

import webview

import updater
from adblock_hook import AdBlocker
from version import __version__

HERE = pathlib.Path(__file__).parent
RES = pathlib.Path(getattr(sys, "_MEIPASS", HERE))  # bundled files when packaged as an .exe
CFG_DIR = pathlib.Path(os.environ.get("APPDATA", HERE)) / "AACMapOverlay"
CFG = CFG_DIR / "settings.json"
TITLE = "AAC Map Overlay"
PORT = 47853  # fixed, so the map's saved Peer Link settings survive restarts
DEFAULTS = {"map": "Auroria", "preset": "collect", "types": [], "live": False, "liveSeen": False, "adblock": True, "autoZoom": True, "autoDiscover": True, "discRadius": 8, "autoUpdate": True, "barsHidden": False, "locked": False, "opacity": 0.9,
            "x": 40, "y": 120, "w": 460, "h": 560}

user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
LWA_ALPHA = 0x2
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY = 0x0312
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


user32.MonitorFromWindow.restype = wt.HMONITOR
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
VK = {"M": 0x4D, "L": 0x4C, "H": 0x48, "UP": 0x26, "DOWN": 0x28}


def load_settings():
    try:
        return {**DEFAULTS, **json.loads(CFG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


class Overlay:
    def __init__(self):
        self.s = load_settings()
        self.locked = bool(self.s.get("locked"))  # come back locked if it was locked
        self.visible = True
        self.window = None
        self.adblock = AdBlocker(CFG_DIR) if self.s.get("adblock", True) else None
        self.update = None  # newer release info, once found
        self.max_from = None  # window rect (screen pixels) before maximizing

    # ---- native window helpers -------------------------------------------
    def hwnd(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return user32.FindWindowW(None, TITLE)

    def apply_style(self):
        h = self.hwnd()
        if not h:
            return
        ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self.locked:
            ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            ex &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, ex)
        user32.SetLayeredWindowAttributes(h, 0, int(255 * max(0.2, min(1.0, self.s["opacity"]))), LWA_ALPHA)

    def save(self):
        try:
            CFG_DIR.mkdir(parents=True, exist_ok=True)
            CFG.write_text(json.dumps(self.s, indent=1), encoding="utf-8")
        except OSError:
            pass

    def tell_ui(self):
        if self.window:
            self.window.evaluate_js(f"window.onNative && onNative({json.dumps({'locked': self.locked, 'opacity': self.s['opacity']})})")

    # ---- actions shared by hotkeys and the UI ----------------------------
    def set_lock(self, on):
        self.locked = bool(on)
        self.s["locked"] = self.locked
        self.save()
        self.apply_style()
        self.tell_ui()

    def set_opacity(self, v):
        self.s["opacity"] = round(max(0.2, min(1.0, float(v))), 2)
        self.apply_style()
        self.save()
        self.tell_ui()

    def toggle_visible(self):
        self.visible = not self.visible
        (self.window.show if self.visible else self.window.hide)()
        if self.visible:
            self.apply_style()

    def toggle_bars(self):
        self.s["barsHidden"] = not self.s.get("barsHidden")
        self.save()
        if self.window:
            self.window.evaluate_js(f"window.setBars && setBars({json.dumps(self.s['barsHidden'])})")

    # ---- global hotkeys ---------------------------------------------------
    def hotkeys(self):
        ctrl_alt = MOD_CONTROL | MOD_ALT
        keys = {1: ("M", self.toggle_visible), 2: ("L", lambda: self.set_lock(not self.locked), MOD_ALT),
                3: ("UP", lambda: self.set_opacity(self.s["opacity"] + 0.1)),
                4: ("DOWN", lambda: self.set_opacity(self.s["opacity"] - 0.1)),
                5: ("H", self.toggle_bars)}
        for i, (k, _, *mods) in keys.items():
            mod = mods[0] if mods else ctrl_alt
            if not user32.RegisterHotKey(None, i, mod | MOD_NOREPEAT, VK[k]):
                print(f"Could not register hotkey {k} (mods {mod}): another app is using it.")
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and msg.wParam in keys:
                try:
                    keys[msg.wParam][1]()
                except Exception as e:  # keep the hotkey loop alive
                    print("hotkey error:", e)

    # ---- window events ----------------------------------------------------
    # ---- maximize / restore ---------------------------------------------
    def toggle_max(self):
        """Fill the monitor the overlay is on (minus the taskbar), or go back to the old size."""
        h = self.hwnd()
        if not h:
            return False
        if self.max_from:
            l, t, r, b = self.max_from
            self.max_from = None
            user32.SetWindowPos(h, None, l, t, r - l, b - t, SWP_NOZORDER | SWP_NOACTIVATE)
            return False
        cur = RECT()
        user32.GetWindowRect(h, ctypes.byref(cur))
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi))
        self.max_from = (cur.left, cur.top, cur.right, cur.bottom)  # set before moving: no saves while maximized
        w = mi.rcWork
        user32.SetWindowPos(h, None, w.left, w.top, w.right - w.left, w.bottom - w.top, SWP_NOZORDER | SWP_NOACTIVATE)
        return True

    def on_moved(self, x, y):
        if self.max_from:
            return  # keep the normal size and place for next start
        self.s["x"], self.s["y"] = x, y
        self.save()

    def on_resized(self, w, h):
        if self.max_from:
            return
        self.s["w"], self.s["h"] = w, h
        self.save()

    def on_shown(self):
        self.apply_style()
        threading.Thread(target=self.hotkeys, daemon=True).start()
        threading.Thread(target=self.hook_webview, daemon=True).start()
        threading.Thread(target=self.update_loop, daemon=True).start()

    # ---- updates (GitHub Releases) -----------------------------------------
    def update_loop(self):
        """Check at start and every 5 minutes. At start, install right away if allowed."""
        time.sleep(4)
        first = True
        shown = ""
        while True:
            try:
                info = updater.check()
            except Exception as e:
                info = None
                print("update check failed:", e)
            if info:
                self.update = info
                if first and self.s.get("autoUpdate", True) and updater.can_self_update() and info.get("url"):
                    self.install_update()
                    return
                if info["version"] != shown:  # show each new version once; "Later" isn't re-nagged
                    shown = info["version"]
                    self.tell_update("available")
            first = False
            time.sleep(5 * 60)  # 12 checks an hour, well under GitHub's 60/hour limit

    def tell_update(self, state, err=""):
        if self.window and self.update:
            self.window.evaluate_js(f"window.onUpdate && onUpdate({json.dumps({**self.update, 'state': state, 'error': err, 'canSelf': updater.can_self_update()})})")

    def install_update(self):
        if not self.update:
            return
        if not updater.can_self_update() or not self.update.get("url"):
            webbrowser.open(self.update["page"])
            return
        self.tell_update("downloading")
        try:
            updater.download_and_run(self.update)
        except Exception as e:
            self.tell_update("failed", str(e))
            return
        time.sleep(1)
        self.window.destroy()  # the installer replaces the files and starts the new version

    # ---- WebView2 hooks: map tools script + ad blocking ---------------------
    def on_ui(self, fn):
        from System import Func, Object
        return self.window.native.Invoke(Func[Object](fn))

    def hook_webview(self):
        import time
        tools = (RES / "page_tools.js").read_text(encoding="utf-8")

        def attach():
            core = self.window.native.webview.CoreWebView2
            if core is None:
                return False
            core.AddScriptToExecuteOnDocumentCreatedAsync(tools)  # smart zoom, header hiding
            if self.adblock:
                self.adblock.attach(core)
            return True
        for _ in range(150):  # WebView2 finishes starting shortly after the window shows
            try:
                if self.on_ui(attach):
                    break
            except Exception as e:
                print("webview hook error:", e)
            time.sleep(0.1)
        else:
            return
        if self.adblock:
            self.adblock.load_lists(on_ready=lambda: self.on_ui(self.adblock.install_script))
        # reload the map once so the first page load gets the scripts and filtering
        self.window.evaluate_js("window.load && load()")


class Api:
    """Functions the page can call as window.pywebview.api.<name>()."""

    def __init__(self, ov):
        self._ov = ov

    def get_settings(self):
        return {**self._ov.s, "locked": self._ov.locked, "version": __version__}

    def update_now(self):
        threading.Thread(target=self._ov.install_update, daemon=True).start()

    def save(self, patch):
        self._ov.s.update({k: v for k, v in patch.items() if k in ("map", "preset", "types", "live", "liveSeen", "autoZoom", "autoDiscover", "discRadius", "autoUpdate", "barsHidden", "peerCode", "peerMe")})
        self._ov.save()

    def set_opacity(self, v):
        self._ov.set_opacity(v)

    def lock(self):
        self._ov.set_lock(True)

    def toggle_max(self):
        return self._ov.toggle_max()

    def resize(self, w, h):
        self._ov.window.resize(max(300, int(w)), max(260, int(h)))

    def blocked(self):
        return self._ov.adblock.blocked if self._ov.adblock else -1

    def open_url(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)

    def hide(self):
        self._ov.toggle_visible()

    def quit(self):
        self._ov.window.destroy()


def main():
    ov = Overlay()
    s = ov.s
    ov.window = webview.create_window(
        TITLE, str(RES / "ui.html"), js_api=Api(ov), x=s["x"], y=s["y"], width=s["w"], height=s["h"],
        frameless=True, easy_drag=False, on_top=True, resizable=True, background_color="#14171c")
    ov.window.events.shown += ov.on_shown
    ov.window.events.moved += ov.on_moved
    ov.window.events.resized += ov.on_resized
    webview.start(http_server=True, http_port=PORT, private_mode=False, storage_path=str(CFG_DIR / "webview"))


if __name__ == "__main__":
    main()
