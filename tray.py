"""Notification-area (system tray) icon with a right-click menu.

Works even when the overlay is locked, hidden, off-screen or its hotkey is taken by
another program: Unlock, Bring back to main screen and Settings are always one click away.
"""
import sys
import threading


def _bg(fn):
    """Run menu actions off the UI thread (they call back into the window)."""
    return lambda *_: threading.Thread(target=fn, daemon=True).start()


class Tray:
    def __init__(self, ov, version, icon_path):
        self.ov = ov
        self.version = version
        self.icon_path = icon_path
        self.icon = None

    def create(self):
        """Call on the WinForms UI thread."""
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Drawing import Icon
        from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem, ToolStripSeparator

        ov = self.ov
        n = NotifyIcon()
        try:
            n.Icon = Icon.ExtractAssociatedIcon(sys.executable) if getattr(sys, "frozen", False) else Icon(self.icon_path)
        except Exception:
            n.Icon = Icon(self.icon_path)
        n.Text = "AAC Map Overlay"
        menu = ContextMenuStrip()

        def item(text, fn):
            it = ToolStripMenuItem(text)
            it.Click += _bg(fn)
            menu.Items.Add(it)
            return it

        head = ToolStripMenuItem(f"AAC Map Overlay {self.version}")
        head.Enabled = False
        menu.Items.Add(head)
        show = item("Show overlay", lambda: ov.toggle_visible() if not (ov.visible and not ov.auto_hidden) else ov.hide_overlay())
        lock = item("Unlock", lambda: ov.set_lock(not ov.locked))
        item("Bring back to main screen && unlock", lambda: ov.rescue(move=True))
        item("Settings…", lambda: ov.rescue(move=False, settings=True))
        menu.Items.Add(ToolStripSeparator())
        dps = item("DPS meter (beta)", ov.toggle_dps)
        autohide = item("Only show while the game is in front", ov.toggle_autohide)
        menu.Items.Add(ToolStripSeparator())
        item("Check for updates", ov.check_updates_now)
        item("Quit", ov.quit)

        def refresh(*_):
            shown = ov.visible and not ov.auto_hidden
            show.Text = "Hide overlay" if shown else "Show overlay"
            key = ov.lock_key()["label"]
            lock.Text = f"Unlock  ({key})" if ov.locked else f"Lock  ({key})"
            dps.Checked = bool(ov.s.get("dps"))
            autohide.Checked = bool(ov.s.get("autoHide"))

        menu.Opening += refresh
        n.ContextMenuStrip = menu
        n.DoubleClick += _bg(lambda: ov.rescue(move=False))
        n.Visible = True
        self.icon = n
        return True

    def balloon(self, title, text):
        if self.icon:
            from System.Windows.Forms import ToolTipIcon
            self.icon.ShowBalloonTip(4000, title, text, ToolTipIcon.Info)

    def remove(self):
        if self.icon:
            self.icon.Visible = False
            self.icon.Dispose()
            self.icon = None
