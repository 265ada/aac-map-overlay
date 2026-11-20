"""Solo DPS meter: reads the floating damage numbers off the game's screen.

How it works
  * Several times a second it copies the middle of the game window (screen pixels only,
    the game itself is never read or touched).
  * It keeps only bright white / yellow pixels, where the damage numbers are drawn,
    and reads them with Windows' built-in OCR.
  * Words that are only digits count as hits. Names and other text are ignored.
  * A number floats upward for about a second, so the same number seen again a little
    higher up in the next frames is the same hit and is counted once.
  * A fight starts with the first hit and ends after FIGHT_GAP seconds without one.

This is an estimate: overlapping numbers, very fast ticks of the same value and numbers
from other players near your target can be missed or counted. Only damage your screen
shows is counted, which is why it is a solo meter.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import re
import threading
import time

FIGHT_GAP = 8.0          # seconds without a hit that end a fight
FPS = 8                  # screen reads per second while fighting (fewer while idle)
IDLE_FPS = 3
TRACK_TTL = 0.7          # seconds a number is remembered while it floats
MAX_HIT = 2_000_000      # ignore anything larger (OCR glued numbers together)
# part of the game window that is read: the middle, where your target is
ROI = (0.28, 0.12, 0.72, 0.66)  # left, top, right, bottom as fractions of the window

user32 = ctypes.windll.user32
DIGITS = re.compile(r"^\d{1,3}(?:[,.]\d{3})+$|^\d{1,7}$")


def game_window(exe="archeagechronicles.exe"):
    """Handle of the game's visible main window, or None."""
    k32 = ctypes.windll.kernel32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if not user32.IsWindowVisible(h) or user32.GetWindow(h, 4):  # skip owned windows
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        hp = k32.OpenProcess(0x1000, False, pid.value)
        if hp:
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = wt.DWORD(len(buf))
                if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)) and buf.value.lower().endswith("\\" + exe):
                    found.append(h)
                    return False
            finally:
                k32.CloseHandle(hp)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def client_rect(h):
    """Screen rectangle of the window's client area (left, top, width, height)."""
    r = wt.RECT()
    if not user32.GetClientRect(h, ctypes.byref(r)):
        return None
    p = wt.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(p))
    return p.x, p.y, r.right, r.bottom


def text_mask(rgb):
    """'How white is each pixel' as dark-on-light grayscale, which OCR reads best.

    White text has all three channels high; fire, skin, grass and sky always have one low
    channel, so the darkest channel separates them. Kept soft (not black/white) so thin
    anti-aliased strokes survive; OCR does its own thresholding. Also returns how many
    clearly white pixels there are, to skip OCR on frames with nothing to read."""
    import numpy as np
    lo = rgb.min(axis=2)
    # big / critical hits are yellow-orange: red and green both high (fire has much less green)
    r, g = rgb[..., 0].astype(np.uint16), rgb[..., 1].astype(np.uint16)
    yellow = np.where((r > 170) & (g * 100 > r * 72), np.minimum(r, g), 0).astype(np.uint8)
    text = np.maximum(lo, yellow)
    return (255 - text).astype(np.uint8), int((text > 170).sum())


class Ocr:
    """Windows' built-in OCR (Windows.Media.Ocr) on grayscale numpy images."""

    def __init__(self):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        self.engine = OcrEngine.try_create_from_language(Language("en-US")) or OcrEngine.try_create_from_user_profile_languages()
        if not self.engine:
            raise RuntimeError("Windows OCR is not available (install the English language pack)")
        self.loop = asyncio.new_event_loop()

    def words(self, gray):
        """[(text, x, y, w, h)] for each word found."""
        import numpy as np
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import Buffer
        h, w = gray.shape
        bgra = np.empty((h, w, 4), np.uint8)
        bgra[..., 0] = bgra[..., 1] = bgra[..., 2] = gray
        bgra[..., 3] = 255
        data = bgra.tobytes()
        buf = Buffer(len(data))
        buf.length = len(data)
        memoryview(buf)[:] = data
        bmp = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, w, h)
        res = self.loop.run_until_complete(self.engine.recognize_async(bmp))
        out = []
        for line in res.lines:
            words = list(line.words)
            # a line with letters is a name or label ("33 DiRe" = level + name), never damage
            if any(re.search(r"[^\d,.+\-\s]", w.text) for w in words):
                continue
            for word in words:
                r = word.bounding_rect
                out.append((word.text, r.x, r.y, r.width, r.height))
        return out


def hits_in(words, scale=1.0):
    """Damage numbers among OCR words: [(value, cx, cy, height)] in source pixels."""
    hits = []
    for text, x, y, w, h in words:
        t = text.strip().strip("+-").replace(" ", "")
        if not DIGITS.match(t):
            continue
        v = int(re.sub(r"[,.]", "", t))
        if 0 < v <= MAX_HIT:
            hits.append((v, (x + w / 2) / scale, (y + h / 2) / scale, h / scale))
    return hits


class Tracker:
    """Counts each floating number once, when it is seen drifting upward.

    Damage numbers rise; level numbers beside player names and other static digits do not,
    so a number only counts after it has moved up by half its height."""

    def __init__(self):
        self.tracks = []  # [value, x, y, h, last_seen, first_y, counted]

    def feed(self, hits, now):
        self.tracks = [t for t in self.tracks if now - t[4] < TRACK_TTL]
        new = []
        used = set()
        for v, x, y, h in sorted(hits, key=lambda k: -k[2]):  # lowest (newest) first
            best = None
            for i, t in enumerate(self.tracks):
                if i in used or t[0] != v:
                    continue
                size = max(h, t[3], 8)
                # same value, about the same column, and at or above where it was (it floats up)
                if abs(x - t[1]) < 2.5 * size and t[2] - 5 * size < y < t[2] + 1.0 * size:
                    if best is None or abs(y - t[2]) < abs(y - self.tracks[best][2]):
                        best = i
            if best is None:
                self.tracks.append([v, x, y, h, now, y, False])
                used.add(len(self.tracks) - 1)
            else:
                t = self.tracks[best]
                t[1], t[2], t[3], t[4] = x, y, h, now
                used.add(best)
                if not t[6] and t[5] - y >= 0.5 * max(h, 8):
                    t[6] = True
                    new.append(v)
        return new


class Fight:
    def __init__(self, start):
        self.start = start
        self.last = start
        self.total = 0
        self.hits = 0
        self.biggest = 0
        self.timeline = []  # (time, damage)

    def add(self, v, now):
        self.total += v
        self.hits += 1
        self.biggest = max(self.biggest, v)
        self.last = now
        self.timeline.append((now, v))

    def seconds(self, now=None):
        end = self.last if now is None else max(self.last, now)
        return max(1.0, end - self.start)

    def summary(self, now=None, live=False):
        secs = self.seconds(now if live else None)
        recent = [d for t, d in self.timeline if (now or self.last) - t <= 5]
        return {"total": self.total, "dps": round(self.total / secs), "secs": round(secs, 1),
                "hits": self.hits, "biggest": self.biggest,
                "recentDps": round(sum(recent) / 5) if live else None}


class DpsMeter:
    """Runs the reader in a thread and reports to a callback once a second (and on each hit)."""

    def __init__(self, report, sample_dir=None):
        self.report = report      # fn(dict) -> None
        self.sample_dir = sample_dir  # where "Record sample" saves frames
        self.record_until = 0.0
        self.recorded = 0
        self.record_path = ""
        self.enabled = False
        self.fight = None
        self.history = []         # last few finished fights (summaries)
        self.error = ""
        self.waiting = ""         # why nothing is being read right now
        self._thread = None
        self._lock = threading.Lock()

    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled and not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._send()

    def record_sample(self, seconds=30):
        """Save what the reader sees (4 frames a second) so the reader can be tuned on real fights."""
        if not self.sample_dir:
            return ""
        import pathlib
        folder = pathlib.Path(self.sample_dir) / time.strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        self.record_path, self.recorded = str(folder), 0
        self.record_until = time.time() + seconds
        if not self.enabled:
            self.set_enabled(True)
        self._send()
        return self.record_path

    def reset(self):
        with self._lock:
            self.fight = None
            self.history = []
        self._send()

    def state(self):
        now = time.time()
        with self._lock:
            f = self.fight
            cur = f.summary(now, live=True) if f else None
            spark = []
            if f:  # damage per second over the last 30 s
                for s in range(30):
                    t0 = now - 30 + s
                    spark.append(sum(d for t, d in f.timeline if t0 <= t < t0 + 1))
            return {"enabled": self.enabled, "fight": cur, "history": list(self.history), "spark": spark,
                    "error": self.error, "waiting": self.waiting, "inFight": bool(f and now - f.last < FIGHT_GAP),
                    "recording": max(0, round(self.record_until - now)), "recorded": self.recorded, "recordPath": self.record_path}

    def _send(self):
        try:
            self.report(self.state())
        except Exception as e:
            print("dps report error:", e)

    def _end_fight(self):
        if self.fight and self.fight.hits:
            self.history.insert(0, self.fight.summary())
            del self.history[5:]
        self.fight = None

    def _run(self):
        try:
            import mss
            import mss.tools
            import numpy as np
            ocr = Ocr()
        except Exception as e:  # missing packages / no OCR language
            self.error = f"Screen reading unavailable: {e}"
            self._send()
            return
        tracker = Tracker()
        last_report = 0.0
        last_rec = 0.0
        with mss.MSS() as screen:
            while True:
                now = time.time()
                busy = bool(self.fight and now - self.fight.last < FIGHT_GAP)
                time.sleep(1 / (FPS if busy else IDLE_FPS))
                if not self.enabled:
                    time.sleep(0.5)
                    continue
                new = []
                try:
                    h = game_window()
                    if not h or user32.IsIconic(h):
                        self.waiting = "Game not running"
                        continue
                    if user32.GetForegroundWindow() != h:
                        self.waiting = "Game not in front"
                        continue  # something else is on top: the screen would not show the game
                    self.waiting = ""
                    rect = client_rect(h)
                    if not rect or rect[2] < 200:
                        continue
                    x, y, w, hgt = rect
                    box = {"left": x + int(w * ROI[0]), "top": y + int(hgt * ROI[1]),
                           "width": int(w * (ROI[2] - ROI[0])), "height": int(hgt * (ROI[3] - ROI[1]))}
                    shot = screen.grab(box)
                    if time.time() < self.record_until and time.time() - last_rec >= 0.25:
                        last_rec = time.time()
                        self.recorded += 1
                        mss.tools.to_png(shot.rgb, shot.size, output=f"{self.record_path}/{self.recorded:04d}.png")
                    rgb = np.frombuffer(shot.rgb, np.uint8).reshape(shot.height, shot.width, 3)
                    gray, lit = text_mask(rgb)
                    now = time.time()
                    if lit > 30:  # nothing white on screen: skip the OCR
                        scale = 2 if hgt < 1800 else 1  # OCR wants text about 30 px tall
                        if scale == 2:
                            gray = np.repeat(np.repeat(gray, 2, axis=0), 2, axis=1)
                        new = tracker.feed(hits_in(ocr.words(gray), scale), now)
                    with self._lock:
                        for v in new:
                            if not self.fight:
                                self.fight = Fight(now)
                            self.fight.add(v, now)
                    self.error = ""
                except Exception as e:
                    self.error = f"Reader error: {e}"
                    time.sleep(1)
                finally:  # runs after `continue` too, so the window always gets a fresh state
                    now = time.time()
                    with self._lock:
                        if self.fight and now - self.fight.last >= FIGHT_GAP:
                            self._end_fight()
                    if new or now - last_report >= 1:
                        last_report = now
                        self._send()
