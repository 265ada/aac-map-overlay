"""Ad blocking for the overlay's built-in browser (Edge WebView2).

Uses Brave's ad-block engine (the `adblock` package, Python bindings for
adblock-rust) with the EasyList and EasyPrivacy filter lists. Requests that
match are answered with an empty 403 before they leave the PC. A small
script also hides the empty ad boxes TH.GL leaves behind.

Filter lists are downloaded once, cached in %APPDATA%\\AACMapOverlay\\filters
and refreshed weekly. Until they are ready (or if the download fails) a short
built-in list covers TH.GL's ad network.
"""
import json
import os
import pathlib
import threading
import time
import urllib.request

try:
    import adblock
except ImportError:  # run.bat installs it; keep the overlay working without it
    adblock = None

LISTS = {
    "easylist.txt": "https://easylist.to/easylist/easylist.txt",
    "easyprivacy.txt": "https://easylist.to/easylist/easyprivacy.txt",
}
MAX_AGE = 7 * 24 * 3600
# TH.GL's ad stack (seen on its map pages), so blocking works on first start too
BUILTIN = "\n".join("||%s^$third-party" % h for h in (
    "nitropay.com", "doubleclick.net", "googlesyndication.com", "amazon-adsystem.com", "p7cloud.net",
    "confiant-integrations.net", "scalibur.io", "id5-sync.com", "hadronid.net", "privacymanager.io",
    "btloader.com", "cpx.to", "33across.com", "crwdcntrl.net", "fastclick.net", "ad.gt", "aniview.com",
    "avplayer.com", "connect.facebook.net", "adnxs.com", "rubiconproject.com", "pubmatic.com",
    "openx.net", "criteo.com", "casalemedia.com", "teads.tv", "taboola.com", "outbrain.com"))
DEBUG = bool(os.environ.get("AAO_DEBUG"))
NEVER_BLOCK = ("http://127.0.0.1", "http://localhost")
DEFAULT_SOURCE = "https://archeagechronicles.th.gl/"

# Hides TH.GL's leftover ad frames ("Get AD Free" boxes) and Brave/EasyList
# element rules for the current site. Runs in every frame, before page scripts.
HIDE_JS = r"""
(() => {
  const css = (__CSS__)[location.hostname] || '';
  const thgl = /(^|\.)th\.gl$/.test(location.hostname);
  if (!css && !thgl) return;
  const add = () => {
    if (css && document.head && !document.getElementById('aao-hide')) {
      const s = document.createElement('style'); s.id = 'aao-hide'; s.textContent = css; document.head.appendChild(s);
    }
    if (!thgl || !document.body) return;
    // "Ad Blocker Detected" notice: keep it invisible and press its Close button
    // as soon as the countdown lets it (closing it the normal way keeps the page usable)
    const nag = [...document.querySelectorAll('[role=dialog],[role=alertdialog]')]
      .find(d => /Ad Blocker Detected/i.test(d.textContent));
    document.documentElement.classList.toggle('aao-nag', !!nag);
    if (nag && !document.getElementById('aao-nag-css')) {
      const s = document.createElement('style'); s.id = 'aao-nag-css';
      s.textContent = 'html.aao-nag [role=dialog],html.aao-nag [role=alertdialog],html.aao-nag [data-state=open].fixed.inset-0{opacity:0!important}';
      document.head.appendChild(s);
    }
    if (nag && !nag.dataset.aao) {
      nag.dataset.aao = '1';
      const tick = setInterval(() => {
        const b = [...nag.querySelectorAll('button')].find(b => /^\s*Close/i.test(b.textContent));
        if (!nag.isConnected) { clearInterval(tick); add(); return; }
        if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') b.click();
      }, 250);
    }
    for (const el of document.querySelectorAll('div,span,a')) {
      if (el.childElementCount < 3 && /^\s*(Get AD​?\s*Free|Go ad-free)\s*$/i.test(el.textContent)) {
        const box = el.closest('.fixed, .bg-card') || el;
        if (box !== document.body && box.style.display !== 'none') box.style.setProperty('display', 'none', 'important');
      }
    }
  };
  let t = 0;
  new MutationObserver(() => { if (!t) t = setTimeout(() => { t = 0; add(); }, 150); })
    .observe(document, {childList: true, subtree: true});
  document.addEventListener('DOMContentLoaded', add);
})();
"""

# WebView2 resource contexts -> adblock request types
TYPES = {"Document": "sub_frame", "Stylesheet": "stylesheet", "Image": "image", "Media": "media",
         "Font": "font", "Script": "script", "XmlHttpRequest": "xmlhttprequest", "Fetch": "xmlhttprequest",
         "TextTrack": "media", "EventSource": "other", "Websocket": "websocket", "Manifest": "other",
         "SignedExchange": "other", "Ping": "ping", "CspViolationReport": "csp_report"}


class AdBlocker:
    def __init__(self, cache_dir):
        self.dir = pathlib.Path(cache_dir) / "filters"
        self.engine = self._build(BUILTIN)
        self.css = {}
        self.blocked = 0
        self.seen = 0
        self.core = None
        self._script_id = None

    # ---- filter lists ----------------------------------------------------
    @staticmethod
    def _build(text):
        if not adblock:
            return None
        fs = adblock.FilterSet()
        fs.add_filter_list(text, format="standard")
        return adblock.Engine(fs)

    def _fetch_lists(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        texts = []
        for name, url in LISTS.items():
            f = self.dir / name
            if not f.exists() or time.time() - f.stat().st_mtime > MAX_AGE:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "AACMapOverlay"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        f.write_bytes(r.read())
                except OSError as e:
                    print("filter list download failed:", name, e)
            if f.exists():
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
        return texts

    def load_lists(self, on_ready=None):
        """Download/refresh the lists and swap in the full engine (background thread)."""
        def run():
            if not adblock:
                return
            texts = self._fetch_lists()
            if not texts:
                return
            eng = self._build("\n".join([BUILTIN, *texts]))
            css = {}
            for host in ("archeagechronicles.th.gl", "th.gl"):
                try:
                    sel = eng.url_cosmetic_resources(f"https://{host}/").hide_selectors
                    if sel:
                        css[host] = ",".join(sel) + "{display:none!important}"
                except Exception:
                    pass
            self.engine, self.css = eng, css
            if on_ready:
                on_ready()
        threading.Thread(target=run, daemon=True).start()

    # ---- matching ----------------------------------------------------------
    def should_block(self, url, source, rtype):
        if not self.engine or url.startswith(NEVER_BLOCK) or not url.startswith("http"):
            return False
        if rtype == "sub_frame" and (".th.gl/" in url or url.startswith("https://th.gl/")):
            return False  # never block the map page itself
        try:
            return self.engine.check_network_urls(url, source or DEFAULT_SOURCE, rtype).matched
        except Exception:
            return False

    # ---- WebView2 wiring (call on the UI thread) ---------------------------
    def attach(self, core):
        """core: the CoreWebView2 object of the overlay window."""
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
        self.core = core
        try:  # include requests made inside iframes (the TH.GL map is one)
            from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceRequestSourceKinds as Kinds
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All, Kinds.All)
        except Exception:
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All)
        core.WebResourceRequested += self._on_request
        self.install_script()

    def install_script(self):
        core = self.core
        if core is None:
            return
        if self._script_id:
            try:
                core.RemoveScriptToExecuteOnDocumentCreated(self._script_id)
            except Exception:
                pass
        js = HIDE_JS.replace("__CSS__", json.dumps(self.css))
        task = core.AddScriptToExecuteOnDocumentCreatedAsync(js)

        def done(t):
            try:
                self._script_id = t.Result
            except Exception:
                pass
        try:
            from System import Action
            from System.Threading.Tasks import Task
            task.ContinueWith(Action[Task[str]](done))
        except Exception:
            pass

    def _on_request(self, sender, args):
        try:
            req = args.Request
            url = str(req.Uri)
            rtype = TYPES.get(str(args.ResourceContext), "other")
            src = None
            try:
                if req.Headers.Contains("Referer"):
                    src = str(req.Headers.GetHeader("Referer"))
            except Exception:
                pass
            self.seen += 1
            block = self.should_block(url, src, rtype)
            if DEBUG:
                print("BLOCK" if block else "allow", rtype, url[:110], flush=True)
            if block:
                args.Response = sender.Environment.CreateWebResourceResponse(None, 403, "Blocked", "")
                self.blocked += 1
        except Exception as e:
            print("adblock hook error:", e)
