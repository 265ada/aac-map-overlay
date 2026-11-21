"""ArcheAge Chronicles map overlay.

A small always-on-top window that shows The Hidden Gaming Lair's official
map embed (https://www.th.gl/developers) with our own controls:
map and marker pickers, opacity, click-through lock and global hotkeys.

Hotkeys (work while the game has focus):
  Ctrl+Alt+M      show / hide the overlay
  Alt+L           lock / unlock (locked = clicks go through to the game); change it in ⚙ settings
  Ctrl+Alt+Up     more opaque
  Ctrl+Alt+Down   more transparent
  Ctrl+Alt+H      hide / show the top bars
  Ctrl+Alt+P      next activity profile (Manual, Gathering, Exploring)
  Ctrl+Alt+D      show / hide the solo DPS meter (beta)

Only one copy runs at a time: launching it again while it is open does nothing.

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
from dps_meter import DpsMeter
from version import __version__

HERE = pathlib.Path(__file__).parent
RES = pathlib.Path(getattr(sys, "_MEIPASS", HERE))  # bundled files when packaged as an .exe
CFG_DIR = pathlib.Path(os.environ.get("APPDATA", HERE)) / "AACMapOverlay"
CFG = CFG_DIR / "settings.json"
TITLE = "AAC Map Overlay"
DPS_TITLE = "AAC DPS Meter"
PORT = 47853  # fixed, so the map's saved Peer Link settings survive restarts
DEFAULTS = {"map": "Auroria", "preset": "collect", "types": [], "live": False, "liveSeen": False, "adblock": True, "autoZoom": True, "autoDiscover": True, "discRadius": 8, "autoUpdate": True, "barsHidden": False, "locked": False, "profile": "manual", "target": "", "sound": True,
            "autoHide": True, "lowPower": True, "round": False, "showProgress": False, "manualFilters": None, "opacity": 0.9,
            "x": 40, "y": 120, "w": 460, "h": 560,
            "lockKey": {"mods": 1, "vk": 0x4C, "label": "Alt+L"},
            "dps": False, "dpsX": 520, "dpsY": 120, "dpsW": 300, "dpsH": 232}

user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
LWA_ALPHA, LWA_COLORKEY = 0x2, 0x1
ROUND_KEY = 0x00030201  # COLORREF of #010203: the page paints this around the round map; Windows makes it see-through
GAME_EXE = "archeagechronicles.exe"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312
WM_APP_LOCKKEY = 0x8001  # tells the hotkey thread to swap the lock key
DEFAULT_LOCK_KEY = {"mods": MOD_ALT, "vk": 0x4C, "label": "Alt+L"}
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


user32.MonitorFromWindow.restype = wt.HMONITOR
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
VK = {"M": 0x4D, "L": 0x4C, "H": 0x48, "P": 0x50, "D": 0x44, "UP": 0x26, "DOWN": 0x28}


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
        self.auto_hidden = False
        self.force_show = False
        self.dps_window = None
        self.hotkey_tid = 0
        self.pending_lock_key = None
        self.meter = DpsMeter(self.dps_report, CFG_DIR / "dps-samples")

    # ---- native window helpers -------------------------------------------
    def hwnd(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return user32.FindWindowW(None, TITLE)

    def dps_hwnd(self):
        try:
            return int(self.dps_window.native.Handle.ToInt64())
        except Exception:
            return user32.FindWindowW(None, DPS_TITLE)

    def apply_style(self):
        self.apply_dps_style()
        h = self.hwnd()
        if not h:
            return
        ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self.locked:
            ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            ex &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, ex)
        alpha = int(255 * max(0.2, min(1.0, self.s["opacity"])))
        if self.s.get("round"):
            user32.SetLayeredWindowAttributes(h, ROUND_KEY, alpha, LWA_ALPHA | LWA_COLORKEY)
        else:
            user32.SetLayeredWindowAttributes(h, 0, alpha, LWA_ALPHA)

    def apply_dps_style(self):
        """The meter window follows the overlay's lock: locked = clicks go through to the game."""
        h = self.dps_window and self.dps_hwnd()
        if not h:
            return
        ex = user32.GetWindowLongPtrW(h, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self.locked:
            ex |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            ex &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, ex)
        user32.SetLayeredWindowAttributes(h, 0, int(255 * max(0.5, min(1.0, self.s["opacity"]))), LWA_ALPHA)

    # ---- solo DPS meter (separate window) ------------------------------------
    def dps_report(self, state):
        if self.dps_window and self.s.get("dps"):
            self.dps_window.evaluate_js(f"window.render && render({json.dumps(state)})")

    def toggle_dps(self, on=None):
        on = (not self.s.get("dps")) if on is None else bool(on)
        self.s["dps"] = on
        self.save()
        self.meter.set_enabled(on)
        if not self.dps_window:
            return on
        if on and not self.auto_hidden:
            self.dps_window.show()
            self.apply_dps_style()
            self.dps_window.evaluate_js(f"window.onNative && onNative({json.dumps({'locked': self.locked})})")
        elif not on:
            self.dps_window.hide()
        if self.window:
            self.window.evaluate_js(f"window.onDps && onDps({json.dumps(on)})")
        return on

    def on_dps_moved(self, x, y):
        self.s["dpsX"], self.s["dpsY"] = x, y
        self.save()

    def on_dps_resized(self, w, h):
        self.s["dpsW"], self.s["dpsH"] = w, h
        self.save()

    def save(self):
        try:
            CFG_DIR.mkdir(parents=True, exist_ok=True)
            CFG.write_text(json.dumps(self.s, indent=1), encoding="utf-8")
        except OSError:
            pass

    def tell_ui(self):
        if self.window:
            self.window.evaluate_js(f"window.onNative && onNative({json.dumps({'locked': self.locked, 'opacity': self.s['opacity']})})")
        if self.dps_window and self.s.get("dps"):
            self.dps_window.evaluate_js(f"window.onNative && onNative({json.dumps({'locked': self.locked})})")

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
        if self.auto_hidden and self.visible:
            # Ctrl+Alt+M while auto-hidden: show anyway (map without the game) until the game is back in front
            self.force_show = True
            self.auto_hidden = False
            self.window.show()
            self.apply_style()
            return
        self.visible = not self.visible
        self.auto_hidden = False
        self.force_show = False
        (self.window.show if self.visible else self.window.hide)()
        if self.visible:
            self.apply_style()

    # ---- only show while the game is the active window ---------------------
    def foreground(self):
        """(pid, exe name) of the program in front."""
        h = user32.GetForegroundWindow()
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        k32 = ctypes.windll.kernel32
        hp = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not hp:
            return pid.value, ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wt.DWORD(len(buf))
            ok = k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size))
            return pid.value, (os.path.basename(buf.value).lower() if ok else "")
        finally:
            k32.CloseHandle(hp)

    def autohide_loop(self):
        """Show the overlay only while the game is in front (or you're using the overlay itself).
        With the game closed or another program in front, it stays hidden. Ctrl+Alt+M shows it anyway."""
        me = os.getpid()
        while True:
            time.sleep(0.3)
            try:
                if not self.s.get("autoHide") or not self.visible:
                    if self.auto_hidden and self.visible:
                        self.auto_hidden = False
                        self.window.show()
                        self.apply_style()
                    continue
                pid, exe = self.foreground()
                game_front = exe == GAME_EXE
                if game_front:
                    self.force_show = False  # back to normal once the game is in front again
                ours = pid == me or exe in ("aacmapoverlay.exe", "msedgewebview2.exe")
                want = game_front or ours or self.force_show
                if not want and not self.auto_hidden:
                    self.auto_hidden = True
                    self.window.hide()
                    if self.dps_window and self.s.get("dps"):
                        self.dps_window.hide()
                elif want and self.auto_hidden:
                    self.auto_hidden = False
                    self.window.show()
                    if self.dps_window and self.s.get("dps"):
                        self.dps_window.show()
                    self.apply_style()
            except Exception as e:
                print("auto-hide error:", e)

    def toggle_bars(self):
        self.s["barsHidden"] = not self.s.get("barsHidden")
        self.save()
        if self.window:
            self.window.evaluate_js(f"window.setBars && setBars({json.dumps(self.s['barsHidden'])})")

    # ---- global hotkeys ---------------------------------------------------
    def lock_key(self):
        k = self.s.get("lockKey") or DEFAULT_LOCK_KEY
        try:
            return {"mods": int(k["mods"]) & 0x7, "vk": int(k["vk"]), "label": str(k.get("label") or "")[:24]}
        except (KeyError, TypeError, ValueError):
            return dict(DEFAULT_LOCK_KEY)

    def set_lock_key(self, key):
        """Called from the settings panel: ask the hotkey thread to swap the lock key."""
        try:
            new = {"mods": int(key["mods"]) & 0x7, "vk": int(key["vk"]), "label": str(key["label"])[:24]}
        except (KeyError, TypeError, ValueError):
            return "That key can't be used"
        ctrl_alt = MOD_CONTROL | MOD_ALT
        if new["mods"] == ctrl_alt and new["vk"] in {VK[k] for k in ("M", "H", "P", "D", "UP", "DOWN")}:
            return f"{new['label']} is already one of the overlay's own keys"
        is_fkey = 0x70 <= new["vk"] <= 0x87
        if not new["mods"] and not is_fkey:
            return "Use Ctrl, Alt or Shift with that key (F1-F24 work alone)"
        if not self.hotkey_tid:
            return "Hotkeys aren't ready yet, try again in a moment"
        self.pending_lock_key = new
        user32.PostThreadMessageW(self.hotkey_tid, WM_APP_LOCKKEY, 0, 0)
        return ""

    def _swap_lock_key(self):
        """Runs on the hotkey thread (hotkeys belong to the thread that registered them)."""
        new, self.pending_lock_key = self.pending_lock_key, None
        if not new:
            return
        old = self.lock_key()
        user32.UnregisterHotKey(None, 2)
        if user32.RegisterHotKey(None, 2, new["mods"] | MOD_NOREPEAT, new["vk"]):
            self.s["lockKey"] = new
            self.save()
            result = {"ok": True, **new}
        else:  # another program owns that combo: keep the old one
            user32.RegisterHotKey(None, 2, old["mods"] | MOD_NOREPEAT, old["vk"])
            result = {"ok": False, **old, "error": f"{new['label']} is taken by another program"}
        if self.window:
            self.window.evaluate_js(f"window.onLockKey && onLockKey({json.dumps(result)})")
        if self.dps_window:
            self.dps_window.evaluate_js(f"window.onLockKey && onLockKey({json.dumps(result)})")

    def hotkeys(self):
        self.hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        ctrl_alt = MOD_CONTROL | MOD_ALT
        lk = self.lock_key()
        keys = {1: ("M", self.toggle_visible), 2: (lk["vk"], lambda: self.set_lock(not self.locked), lk["mods"]),
                3: ("UP", lambda: self.set_opacity(self.s["opacity"] + 0.1)),
                4: ("DOWN", lambda: self.set_opacity(self.s["opacity"] - 0.1)),
                5: ("H", self.toggle_bars),
                6: ("P", lambda: self.window.evaluate_js("window.cycleProfile && cycleProfile()")),
                7: ("D", self.toggle_dps)}
        for i, (k, _, *mods) in keys.items():
            mod = mods[0] if mods else ctrl_alt
            if not user32.RegisterHotKey(None, i, mod | MOD_NOREPEAT, VK[k] if isinstance(k, str) else k):
                print(f"Could not register hotkey {k} (mods {mod}): another app is using it.")
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_APP_LOCKKEY:
                try:
                    self._swap_lock_key()
                except Exception as e:
                    print("lock key error:", e)
                continue
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
        threading.Thread(target=self.autohide_loop, daemon=True).start()
        if self.s.get("dps"):
            self.meter.set_enabled(True)

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
        self._ov.s.update({k: v for k, v in patch.items() if k in ("map", "preset", "types", "live", "liveSeen", "autoZoom", "autoDiscover", "discRadius", "autoUpdate", "barsHidden", "peerCode", "peerMe",
                                                                            "profile", "target", "sound", "autoHide", "lowPower", "round",
                                                                            "showProgress", "manualFilters", "manualTarget", "manualRadius")})
        self._ov.save()

    def set_opacity(self, v):
        self._ov.set_opacity(v)

    def lock(self):
        self._ov.set_lock(True)

    def toggle_max(self):
        return self._ov.toggle_max()

    def set_round(self, on):
        self._ov.s["round"] = bool(on)
        self._ov.save()
        self._ov.apply_style()

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

    def toggle_dps(self):
        return self._ov.toggle_dps()

    def set_lock_key(self, key):
        return self._ov.set_lock_key(key)

    def reset_lock_key(self):
        return self._ov.set_lock_key(DEFAULT_LOCK_KEY)


class DpsApi:
    """Functions the DPS meter window can call."""

    def __init__(self, ov):
        self._ov = ov

    def ready(self):
        ov = self._ov
        ov.apply_dps_style()
        ov.dps_window.evaluate_js(f"window.onNative && onNative({json.dumps({'locked': ov.locked})})")
        ov.dps_report(ov.meter.state())

    def reset(self):
        self._ov.meter.reset()

    def record(self):
        return self._ov.meter.record_sample(30)

    def hide(self):
        self._ov.toggle_dps(False)


_instance_lock = None


def claim_single_instance(wait_ms=6000):
    """True if this is the only running overlay. A second launch waits briefly (an update
    relaunch can start before the old copy has finished closing) and then gives up."""
    global _instance_lock
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _instance_lock = k32.CreateMutexW(None, True, r"Local\AACMapOverlay.SingleInstance")
    if not _instance_lock:
        return True  # cannot tell; do not block startup
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    return k32.WaitForSingleObject(_instance_lock, wait_ms) in (0, 0x80)  # acquired / previous owner died


def main():
    if not claim_single_instance():
        return  # another overlay is already running
    ov = Overlay()
    s = ov.s
    ov.window = webview.create_window(
        TITLE, str(RES / "ui.html"), js_api=Api(ov), x=s["x"], y=s["y"], width=s["w"], height=s["h"],
        frameless=True, easy_drag=False, on_top=True, resizable=True, background_color="#14171c")
    ov.window.events.shown += ov.on_shown
    ov.window.events.moved += ov.on_moved
    ov.window.events.resized += ov.on_resized
    ov.dps_window = webview.create_window(
        DPS_TITLE, str(RES / "dps.html"), js_api=DpsApi(ov), x=s["dpsX"], y=s["dpsY"], width=s["dpsW"], height=s["dpsH"],
        frameless=True, easy_drag=False, on_top=True, resizable=True, hidden=not s.get("dps"), background_color="#121519",
        min_size=(230, 150))
    ov.dps_window.events.moved += ov.on_dps_moved
    ov.dps_window.events.resized += ov.on_dps_resized
    ov.dps_window.events.shown += ov.apply_dps_style
    webview.start(http_server=True, http_port=PORT, private_mode=False, storage_path=str(CFG_DIR / "webview"))


if __name__ == "__main__":
    main()
