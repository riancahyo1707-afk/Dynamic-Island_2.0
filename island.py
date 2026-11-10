#!/usr/bin/env python3
"""
Dynamic Island for KDE / Linux  -  GTK4 + layer-shell edition (native Wayland)

Same features as the PySide6 edition, but it runs as a real Wayland layer-shell
surface, so drag & drop in and out of the island works on KDE Wayland.

  - Spring-physics animation (the pill stretches and bounces like iOS)
  - Music (MPRIS via playerctl), notifications (D-Bus), volume, brightness,
    battery, Caps/Num Lock, VPN / Wi-Fi alerts, timer, Pomodoro, CTF countdown
  - Pages: Home | Shelf | System | Focus | Launch | Settings
  - File shelf: drop up to 5 files/folders, drag them out again, or copy them
  - Themes (black, glass, clear, light, midnight, hacker, sunset, album + yours)
  - Modes: Automatic / Standby / Popup / Closed
  - Tray icon (StatusNotifierItem): left click = next mode, middle = next theme,
    right click = open Settings

Control from the terminal (the running island answers):
    island timer 5m | toast "hi" | toggle | page system | pomodoro [stop|skip]
    island ctf "HTB Event" "2026-11-20 19:00" | remind "Class" "08:00" daily
    island mode auto|standby|popup|closed|cycle | theme glass | style opacity 0.5
    island shelf add FILE... | shelf paste | shelf copy | shelf clear
    island forget "name" | reload | cancel | quit
Config: ~/.config/dynamic-island/config.json
"""
import colorsys
import glob
import json
import math
import os
import re
import shlex
import shutil
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta


def _ensure_layer_shell_preload():
    """gtk4-layer-shell has to be loaded before libwayland-client: re-exec once with LD_PRELOAD."""
    if (len(sys.argv) > 1 or os.environ.get("ISLAND_NO_PRELOAD")
            or "gtk4-layer-shell" in os.environ.get("LD_PRELOAD", "")):
        return                                  # client command, or already done
    cands = []
    if os.environ.get("ISLAND_LS_LIB"):
        cands.append(os.environ["ISLAND_LS_LIB"])
    for pattern in ("/usr/lib/*/libgtk4-layer-shell.so*", "/usr/lib64/libgtk4-layer-shell.so*",
                    "/usr/lib/libgtk4-layer-shell.so*", "/usr/local/lib/**/libgtk4-layer-shell.so*",
                    os.path.expanduser("~/.local/share/dynamic-island/gls/lib/libgtk4-layer-shell.so*")):
        cands += sorted(glob.glob(pattern, recursive=True))
    for lib in cands:
        if os.path.exists(lib):
            env = dict(os.environ)
            env["LD_PRELOAD"] = (lib + ":" + env.get("LD_PRELOAD", "")).rstrip(":")
            env["ISLAND_NO_PRELOAD"] = "1"
            os.execve(sys.executable, [sys.executable] + sys.argv, env)


_ensure_layer_shell_preload()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import (Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango,  # noqa: E402
                           PangoCairo)
import cairo  # noqa: E402

LS = None
if len(sys.argv) == 1:                          # only the real island needs layer-shell
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as LS
    except (ValueError, ImportError):
        LS = None

# ----------------------------------------------------------------- settings --
APP_ID = "dev.brian.DynamicIsland"
CANVAS_W, CANVAS_H = 470, 210          # transparent surface; the pill is drawn inside
TOP_MARGIN = 6
DEBUG = bool(os.environ.get("ISLAND_DEBUG"))

SIZES = {                               # mode -> (width, height)
    "expanded": (400, 180),
    "notif": (380, 80),
    "battery": (250, 44),
    "volume": (270, 44),
    "brightness": (270, 44),
    "toast": (300, 44),
    "track": (340, 58),
    "hidden": (40, 0),
}
COMPACT_IDLE = (126, 34)
COMPACT_ACTIVE = (190, 34)


def _c(r, g, b, a=1.0):
    return (r / 255.0, g / 255.0, b / 255.0, a)


WHITE = _c(255, 255, 255)
GREEN = _c(48, 209, 88)
RED = _c(255, 69, 58)
ORANGE = _c(255, 159, 10)
BLUE = _c(10, 132, 255)

PAGES = ["home", "shelf", "system", "focus", "launch", "settings"]
MAX_SHELF = 5
POMO = {"focus": 25 * 60, "break": 5 * 60, "long": 15 * 60}
MODES = [("auto", "Automatic"), ("standby", "Standby"), ("popup", "Popup"), ("closed", "Closed")]
MODE_LABELS = {"auto": "Auto", "standby": "Standby", "popup": "Popup", "closed": "Closed"}
MODE_COLORS = {"auto": BLUE, "standby": GREEN, "popup": ORANGE, "closed": RED}

PLAYER_FORMAT = "\t".join([
    "{{status}}", "{{artist}}", "{{title}}", "{{album}}",
    "{{position}}", "{{mpris:length}}", "{{mpris:artUrl}}",
])
BATTERY = next(iter(sorted(glob.glob("/sys/class/power_supply/BAT*"))), None)

CONFIG_DIR = os.path.expanduser("~/.config/dynamic-island")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    "events": [],       # {"name", "when": "YYYY-MM-DD HH:MM", "kind": "ctf|remind", "repeat": "none|daily|weekly"}
    "launchers": [      # up to 8 buttons on the Launch page
        {"name": "Terminal", "cmd": "konsole", "icon": "utilities-terminal"},
        {"name": "Files", "cmd": "dolphin", "icon": "system-file-manager"},
        {"name": "Browser", "cmd": "firefox-esr", "icon": "firefox-esr"},
        {"name": "Settings", "cmd": "systemsettings", "icon": "preferences-system"},
    ],
    "theme": "black",   # black | glass | clear | light | midnight | hacker | sunset | album | your own
    "themes": {},       # your own: {"mine": {"bg": "#202020", "opacity": 0.5, "text": "#ffffff"}}
    "style": {},        # tweaks on top of the theme (set with: island style KEY VALUE)
    "shelf_expire_minutes": 0,   # 0 = keep shelf items until removed or the island quits
    "mode": "standby",  # auto | standby | popup | closed
    "quiet_hours": [],  # e.g. ["23:00", "07:00"] -> Automatic mode switches to Closed
}

THEME_BASE = {
    "bg": "#000000", "bg2": "", "opacity": 1.0,
    "text": "#ffffff", "accent": "", "art_accent": True,
    "border": "#ffffff", "border_opacity": 0.08,
    "corner": 34, "font": "", "tint": 0.0,
}
THEMES = {
    "black": {"label": "Black"},
    "glass": {"label": "Dark glass", "bg": "#0c0c12", "opacity": 0.60, "border_opacity": 0.18},
    "clear": {"label": "Clear", "bg": "#000000", "opacity": 0.22, "border_opacity": 0.35},
    "light": {"label": "Light", "bg": "#f5f5f7", "opacity": 0.92, "text": "#1c1c1e",
              "border": "#000000", "border_opacity": 0.12},
    "midnight": {"label": "Midnight", "bg": "#0a1030", "bg2": "#1b0f3a", "opacity": 0.96,
                 "accent": "#7aa2ff", "art_accent": False, "border": "#7aa2ff", "border_opacity": 0.25},
    "hacker": {"label": "Hacker", "bg": "#010d03", "opacity": 0.94, "text": "#33ff77",
               "accent": "#33ff77", "art_accent": False, "border": "#33ff77",
               "border_opacity": 0.35, "font": "Monospace"},
    "sunset": {"label": "Sunset", "bg": "#3a1030", "bg2": "#7a2a1a", "opacity": 0.95,
               "text": "#fff1e6", "accent": "#ffb36b", "art_accent": False,
               "border": "#ffb36b", "border_opacity": 0.25},
    "album": {"label": "Album tint", "bg": "#101012", "opacity": 0.96, "tint": 0.40},
}
STYLE_NUMERIC = {"opacity", "border_opacity", "corner", "tint"}


# ------------------------------------------------------------------ helpers --
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, default in list(cfg.items()):
                if isinstance(data.get(key), type(default)):
                    cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def parse_color(s, default=None):
    c = Gdk.RGBA()
    if s and c.parse(str(s)):
        return (c.red, c.green, c.blue, c.alpha)
    return default


def with_alpha(c, a):
    return (c[0], c[1], c[2], a)


def mix(a, b, k):
    return tuple(a[i] * (1 - k) + b[i] * k for i in range(4))


def resolve_theme(cfg):
    name = cfg.get("theme", "black")
    t = dict(THEME_BASE)
    user = cfg.get("themes", {})
    if name in THEMES:
        t.update(THEMES[name])
    elif isinstance(user, dict) and isinstance(user.get(name), dict):
        t.update(user[name])
    else:
        name = "black"
    style = cfg.get("style", {})
    if isinstance(style, dict):
        t.update({k: v for k, v in style.items() if k in THEME_BASE})
    return name, t


def parse_duration(s):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", s or "")
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def parse_when(s):
    """'2026-11-20 19:00' | '19:00' (next time it happens) | '+30m' (from now)."""
    s = (s or "").strip()
    if s.startswith("+"):
        d = parse_duration(s[1:])
        return datetime.now() + timedelta(seconds=d) if d else None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        t = datetime.strptime(s, "%H:%M").time()
    except ValueError:
        return None
    dt = datetime.combine(datetime.now().date(), t)
    return dt if dt > datetime.now() else dt + timedelta(days=1)


def next_occurrence(ev, now):
    try:
        base = datetime.strptime(str(ev.get("when", "")), "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None
    step = {"daily": 1, "weekly": 7}.get(ev.get("repeat", "none"))
    if step:
        late = now - timedelta(seconds=120)
        if base < late:
            base += timedelta(days=((late - base).days // step) * step)
            while base < late:
                base += timedelta(days=step)
    return base


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_countdown(sec):
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


def fmt_rate(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 / 1024:.1f} MB/s"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def hit(r, x, y):
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


class Spring:
    """Damped spring. Lower damping = more bounce."""

    def __init__(self, value, stiffness=330.0, damping=19.0):
        self.x = self.target = float(value)
        self.v = 0.0
        self.k = stiffness
        self.d = damping

    def step(self, dt):
        n = int(dt / 0.008) + 1
        h = dt / n
        for _ in range(n):
            self.v += (self.k * (self.target - self.x) - self.d * self.v) * h
            self.x += self.v * h

    def settled(self):
        return abs(self.target - self.x) < 0.3 and abs(self.v) < 3.0

    def snap(self):
        self.x = self.target
        self.v = 0.0


# ---- processes (never block the UI)
def run_async(argv, callback=None):
    """Run a command; callback(stdout_text) when done (empty text on any failure)."""
    try:
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE
                                              | Gio.SubprocessFlags.STDERR_SILENCE)
        launcher.setenv("LC_ALL", "C", True)
        proc = launcher.spawnv(argv)
    except GLib.Error:
        if callback:
            callback("")
        return

    def done(p, res):
        try:
            _ok, out, _err = p.communicate_utf8_finish(res)
        except GLib.Error:
            out = ""
        if callback:
            callback(out or "")

    proc.communicate_utf8_async(None, None, done)


def stream_lines(argv, on_line):
    """Run a long-lived command and call on_line(text) for every output line."""
    try:
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE
                                              | Gio.SubprocessFlags.STDERR_SILENCE)
        launcher.setenv("LC_ALL", "C", True)
        proc = launcher.spawnv(argv)
    except GLib.Error:
        return None
    stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

    def got(s, res):
        try:
            line, _length = s.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return                              # process ended
        on_line(line)
        s.read_line_async(GLib.PRIORITY_DEFAULT, None, got)

    stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got)
    return proc, stream


# ---- cairo helpers
def rrect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def rgba(cr, c, a=1.0):
    cr.set_source_rgba(c[0], c[1], c[2], c[3] * a)


def fill_rr(cr, x, y, w, h, r, c):
    rrect(cr, x, y, w, h, r)
    rgba(cr, c)
    cr.fill()


def stroke_rr(cr, x, y, w, h, r, c, width=1.0, dash=None):
    rrect(cr, x, y, w, h, r)
    rgba(cr, c)
    cr.set_line_width(width)
    if dash:
        cr.set_dash(dash)
    cr.stroke()
    cr.set_dash([])


def pixbuf_surface(pb):
    """GdkPixbuf -> cairo ImageSurface (ARGB32, premultiplied)."""
    w, h, n = pb.get_width(), pb.get_height(), pb.get_n_channels()
    has_a, stride, px = pb.get_has_alpha(), pb.get_rowstride(), pb.get_pixels()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    sstride, data = surf.get_stride(), surf.get_data()
    for y in range(h):
        row = y * stride
        out = y * sstride
        for x in range(w):
            i = row + x * n
            a = px[i + 3] if has_a else 255
            data[out + x * 4:out + x * 4 + 4] = bytes(
                (px[i + 2] * a // 255, px[i + 1] * a // 255, px[i] * a // 255, a))
    surf.mark_dirty()
    return surf


def load_pixbuf_bytes(data):
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None
    return loader.get_pixbuf()


# ------------------------------------------------------------------- island --
class Island(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app)
        self.app = app
        self.cfg = load_config()
        if not os.path.exists(CONFIG_PATH):
            self.persist()

        # ---- the window: a transparent layer-shell surface at the top-center
        self.set_title("Dynamic Island")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(CANVAS_W, CANVAS_H)
        self.add_css_class("island")
        self.layer = False
        if LS is not None and LS.is_supported():
            LS.init_for_window(self)
            LS.set_namespace(self, "dynamic-island")
            LS.set_layer(self, LS.Layer.OVERLAY)
            LS.set_anchor(self, LS.Edge.TOP, True)          # only TOP -> centered horizontally
            LS.set_margin(self, LS.Edge.TOP, TOP_MARGIN)
            LS.set_exclusive_zone(self, -1)                 # ignore panels, sit at the very top
            LS.set_keyboard_mode(self, LS.KeyboardMode.NONE)
            self.layer = True
        else:
            print("[island] layer-shell not available: running as a normal window", file=sys.stderr)
        css = Gtk.CssProvider()
        text = "window.island, window.island.background { background: transparent; box-shadow: none; border: none; }"
        try:
            css.load_from_string(text)
        except AttributeError:
            css.load_from_data(text.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.area = Gtk.DrawingArea()
        self.area.set_content_width(CANVAS_W)
        self.area.set_content_height(CANVAS_H)
        self.area.set_draw_func(self.on_draw)
        self.set_child(self.area)
        try:
            fam = Gtk.Settings.get_default().get_property("gtk-font-name").rsplit(" ", 1)[0]
        except Exception:
            fam = "Sans"
        self.system_font = fam or "Sans"
        self.font_family = self.system_font

        # ---- visual state
        self.mode = self.want = "compact"
        self.payload = None
        self.alpha = 1.0
        self.fading_out = False
        self.fade_delay = 0.0
        self.sw = Spring(COMPACT_IDLE[0])
        self.sh = Spring(COMPACT_IDLE[1])
        self.expanded = False
        self.hover = False
        self.hover_pos = None
        self.transient = None
        self.hits = []                  # clickable areas, rebuilt on every draw
        self._last = time.monotonic()
        self._last_mask = None
        self._frame_id = None
        self._frame_ms = 16
        self._collapse_id = None
        self._row_pending = None
        self._streams = []
        self.last_scroll = 0.0
        self.peek_until = 0.0           # explicit commands may show the island even in Popup/Closed

        # ---- data state
        self.music = None
        self.first_poll_done = False
        self._music_busy = False
        self.art = None                 # cairo surface 160x160
        self.art_url = ""
        self.accent = (0.92, 0.92, 0.92, 1.0)
        self.volume = None
        self.bat = None
        self.low_warned = set()
        self.timer_end = None
        self.timer_total = 1.0
        self.timer_label = ""
        self.timer_color = ORANGE
        self.pomo_active = False
        self.pomo_phase = "focus"
        self.pomo_done = 0
        self.fired = set()
        self.page = 0
        self.icon_cache = {}
        self.file_icons = {}
        self.cpu_prev = None
        self.cpu_hist = deque([0.0] * 60, maxlen=60)
        self.ram = 0.0
        self.net_prev = None
        self.net_rate = (0.0, 0.0)
        thermal = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
        self.temp_path = thermal[0] if thermal else None
        self.temp = None
        self.led_paths = {
            "Caps Lock": glob.glob("/sys/class/leds/*::capslock/brightness"),
            "Num Lock": glob.glob("/sys/class/leds/*::numlock/brightness"),
        }
        self.leds = {}
        self.xset_fails = 0 if shutil.which("xset") else 99
        self._misc_n = 0
        backlights = sorted(glob.glob("/sys/class/backlight/*"))
        self.backlight = backlights[0] if backlights else None
        self.bright = None
        self.net_state = None
        self.vpn = set()
        self.shelf = []                 # temporary shelf: memory only, gone when the island quits
        self.shelf_msg = ("", 0.0)
        self.dragging = False
        self._dbuf_lines = None
        self.theme_bg2 = None
        self.apply_theme()
        self.vis_mode = self.cfg.get("mode") if self.cfg.get("mode") in dict(MODES) else "standby"
        if self.effective_mode() != "standby":      # start hidden, no flash
            self.mode = self.want = "hidden"
            self.sw.x = self.sw.target = 40.0
            self.sh.x = self.sh.target = 0.0

        self.setup_input()
        for ms, fn in ((1000, self.poll_music), (3000, self.poll_battery), (1000, self.sample_system),
                       (300, self.poll_misc), (2000, self.poll_network), (15000, self.check_events)):
            GLib.timeout_add(ms, lambda f=fn: (f(), True)[1])
        self.start_pactl()
        self.start_dbus()
        self.poll_music()
        self.poll_battery()
        self.sample_system()
        self.poll_misc()
        self.poll_network()
        self.check_events()
        self.wake()

    # ------------------------------------------------------------- plumbing --
    def wake(self):
        """Start the animation loop (it stops by itself when nothing moves)."""
        if self._frame_id is None:
            self._last = time.monotonic()
            self._frame_ms = 16
            self._frame_id = GLib.timeout_add(16, self._frame_cb)

    def _frame_cb(self):
        ms = self.tick()
        if not ms:
            self._frame_id = None
            return False
        if ms != self._frame_ms:
            self._frame_ms = ms
            self._frame_id = GLib.timeout_add(ms, self._frame_cb)
            return False
        return True

    def start_collapse(self, ms=2500):
        self.stop_collapse()
        self._collapse_id = GLib.timeout_add(ms, self._collapse_cb)

    def stop_collapse(self):
        if self._collapse_id is not None:
            GLib.source_remove(self._collapse_id)
            self._collapse_id = None

    def _collapse_cb(self):
        self._collapse_id = None
        if self.expanded and not self.hover and not self.dragging:
            self.expanded = False
            self.peek_until = 0.0
            self.wake()
        return False

    def persist(self):
        try:
            save_config(self.cfg)
        except OSError:
            pass

    def cleanup(self):
        for proc, _stream in self._streams:
            try:
                proc.force_exit()
            except Exception:
                pass

    # ------------------------------------------------------------- commands --
    def command(self, args, cwd=None):
        if not args:
            return
        cmd, args = args[0], list(args[1:])
        if cmd == "timer":
            secs = parse_duration(args[0] if args else "")
            if secs:
                self.start_timer(secs)
        elif cmd == "toast":
            self.show_toast(" ".join(args) or "Hello")
        elif cmd == "cancel":
            self.cancel_timer()
        elif cmd == "toggle":
            self.toggle_expanded()
        elif cmd == "page":
            self.open_page(args[0] if args else "home")
        elif cmd in ("pomodoro", "pomo"):
            sub = args[0] if args else ""
            if sub == "stop" or (not sub and self.pomo_active):
                self.pomo_stop()
            elif sub == "skip" and self.pomo_active:
                self.pomo_next()
            elif not self.pomo_active:
                self.pomo_start()
        elif cmd in ("ctf", "remind") and len(args) >= 2:
            repeat = "none"
            if args[-1] in ("daily", "weekly"):
                repeat = args.pop()
            self.add_event(cmd, args[0], " ".join(args[1:]), repeat)
        elif cmd == "forget" and args:
            self.forget_event(" ".join(args))
        elif cmd == "reload":
            self.cfg = load_config()
            self.apply_theme()
            self.show_toast("Config reloaded", 2.0)
        elif cmd == "theme":
            self.set_theme(args[0] if args else "next")
        elif cmd == "style":
            self.set_style(args)
        elif cmd == "shelf":
            sub = args[0] if args else ""
            if sub == "add":
                paths = [p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p) for p in args[1:]]
                self.add_files(paths, announce=True)
            elif sub == "paste":
                self.paste_files(announce=True)
            elif sub == "clear":
                self.shelf_clear()
                self.show_toast("Shelf cleared", 2.0)
            elif sub == "copy":
                self.shelf_copy(self.shelf)
                self.show_toast(self.shelf_msg[0], 2.5)
            else:
                self.open_page("shelf")
        elif cmd == "mode":
            self.set_mode(args[0] if args else "cycle")
        elif cmd == "debug-click" and len(args) == 2:       # for testing: click at canvas x y
            self.click(float(args[0]), float(args[1]), 1)
        elif cmd == "debug-drop" and args:                   # for testing: pretend files were dropped
            self.dragging = True
            self.begin_drag_over()
            self.add_files(args)
            self.dragging = False
        elif cmd == "quit":
            self.app.quit()

    # --------------------------------------------------------------- events --
    def post(self, kind, seconds, **data):
        """Show a temporary activity (notification, volume, ...)."""
        is_new = (self.transient is None or self.transient["kind"] != kind
                  or kind in ("notif", "toast", "track"))
        self.transient = dict(data, kind=kind, until=time.monotonic() + seconds)
        if is_new:                      # little bounce so it feels alive
            self.sh.v += 70
            self.sw.v += 140
        self.wake()

    def show_toast(self, text, seconds=3.0, color=None, force=False):
        self.post("toast", seconds, text=text, color=color, force=force)

    def start_timer(self, seconds):
        self.pomo_active = False
        self.timer_label, self.timer_color = "", ORANGE
        self.timer_total = float(seconds)
        self.timer_end = time.time() + seconds
        self.show_toast(f"Timer set · {fmt_time(seconds)}", 2.0)

    def cancel_timer(self):
        self.timer_end = None
        self.pomo_active = False
        self.show_toast("Timer cancelled", 2.0)

    # ---- pomodoro
    def begin_phase(self):
        secs = POMO[self.pomo_phase]
        self.timer_total = float(secs)
        self.timer_end = time.time() + secs
        self.timer_label = {"focus": "Focus", "break": "Break", "long": "Long break"}[self.pomo_phase]
        self.timer_color = ORANGE if self.pomo_phase == "focus" else GREEN

    def pomo_start(self):
        self.pomo_active = True
        self.pomo_phase = "focus"
        self.pomo_done = 0
        self.begin_phase()
        self.show_toast("Focus · 25:00", 2.5, ORANGE)

    def pomo_next(self):
        if self.pomo_phase == "focus":
            self.pomo_done += 1
            self.pomo_phase = "long" if self.pomo_done % 4 == 0 else "break"
        else:
            self.pomo_phase = "focus"
        self.begin_phase()
        self.wake()

    def pomo_stop(self):
        self.pomo_active = False
        self.timer_end = None
        self.timer_label = ""
        self.show_toast("Pomodoro stopped", 2.0)

    # ---- visibility mode (auto / standby / popup / closed)
    def in_quiet_hours(self):
        q = self.cfg.get("quiet_hours")
        if not (isinstance(q, list) and len(q) == 2):
            return False
        try:
            a = datetime.strptime(str(q[0]), "%H:%M").time()
            b = datetime.strptime(str(q[1]), "%H:%M").time()
        except ValueError:
            return False
        now = datetime.now().time()
        return (a <= now < b) if a <= b else (now >= a or now < b)

    def effective_mode(self):
        """What the island actually does right now: standby, popup or closed."""
        m = self.vis_mode
        if m == "auto":
            if self.in_quiet_hours():
                m = "closed"
            else:
                playing = self.music is not None and self.music["status"] == "Playing"
                busy = bool(self.timer_end) or playing or self.expanded
                m = "standby" if busy else "popup"
        if m == "closed" and time.monotonic() < self.peek_until:
            m = "popup"                 # you asked for it explicitly (tray / terminal)
        return m

    def set_mode(self, key):
        keys = [k for k, _ in MODES]
        if key == "cycle":
            key = keys[(keys.index(self.vis_mode) + 1) % len(keys)]
        if key not in keys:
            return
        self.vis_mode = key
        self.cfg["mode"] = key
        self.persist()
        if key == "closed":
            self.expanded = False
            self.peek_until = 0.0
        self.transient = None
        self.show_toast(f"Mode · {dict(MODES)[key]}", 1.8, MODE_COLORS[key], force=True)
        if getattr(self.app, "tray", None):
            self.app.tray.refresh()
        self.wake()

    def toggle_expanded(self):
        if self.expanded:
            self.expanded = False
            self.peek_until = 0.0
            self.transient = None
        else:
            self.expanded = True
            self.transient = None
            self.peek_until = time.monotonic() + 8.0     # explicit open: show even in Closed mode
            self.poll_music()
            if not self.hover:
                self.start_collapse(6000)
        self.wake()

    def open_page(self, name):
        if name not in PAGES:
            return
        self.expanded = True
        self.transient = None
        self.peek_until = time.monotonic() + 8.0
        self.set_page(PAGES.index(name))
        if not self.hover:
            self.start_collapse(6000)
        self.wake()

    def set_page(self, i):
        if i == self.page:
            return
        self.page = i
        self.alpha = 0.0                # quick fade-in of the new page
        self.fade_delay = 0.0
        self.wake()

    # ---- theme
    def apply_theme(self):
        name, t = resolve_theme(self.cfg)
        self.theme_name = name

        def color(key, default):
            return parse_color(t.get(key) or "", None) or parse_color(default)

        def num(key, default, lo, hi):
            try:
                v = float(t.get(key, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        text = color("text", "#ffffff")
        self.c_text = text
        self.c_sub, self.c_sub2, self.c_soft = (with_alpha(text, .55), with_alpha(text, .75),
                                                with_alpha(text, .67))
        self.c_dim, self.c_btn = with_alpha(text, .24), with_alpha(text, .10)
        border = color("border", "#ffffff")
        self.c_border = with_alpha(border, num("border_opacity", 0.08, 0.0, 1.0))
        alpha = num("opacity", 1.0, 0.05, 1.0)
        bg = color("bg", "#000000")
        self.theme_bg = with_alpha(bg, alpha)
        bg2 = parse_color(t.get("bg2") or "", None)
        self.theme_bg2 = with_alpha(bg2, alpha) if bg2 else None
        self.theme_tint = num("tint", 0.0, 0.0, 1.0)
        self.theme_accent = parse_color(t.get("accent") or "", None) or text
        self.theme_art_accent = bool(t.get("art_accent", True))
        self.corner = num("corner", 34, 4, 60)
        self.font_family = str(t.get("font") or "") or self.system_font
        self.bg_cur = self.theme_bg
        self.restyle_art()
        self.wake()

    def theme_names(self):
        user = self.cfg.get("themes", {})
        extra = [n for n in user if n not in THEMES] if isinstance(user, dict) else []
        return list(THEMES) + extra

    def theme_label(self, name=None):
        name = name or self.theme_name
        return THEMES.get(name, {}).get("label", name)

    def set_theme(self, name, toast=True):
        names = self.theme_names()
        if name in ("next", "cycle"):
            i = names.index(self.theme_name) if self.theme_name in names else -1
            name = names[(i + 1) % len(names)]
        if name not in names:
            self.show_toast(f"Unknown theme · {name}", 2.5, RED)
            return
        self.cfg["theme"] = name
        self.cfg["style"] = {}              # tweaks belong to one theme
        self.persist()
        self.apply_theme()
        if toast:
            self.show_toast(f"Theme · {self.theme_label()}", 1.8)

    def set_style(self, args):
        if args and args[0] == "reset":
            self.cfg["style"] = {}
            self.persist()
            self.apply_theme()
            self.show_toast("Style reset", 2.0)
            return
        if len(args) < 2 or args[0] not in THEME_BASE:
            self.show_toast("style: " + " | ".join(THEME_BASE), 4.0, RED)
            return
        key, val = args[0], " ".join(args[1:])
        if key in STYLE_NUMERIC:
            try:
                val = float(val)
            except ValueError:
                self.show_toast(f"{key} needs a number", 2.5, RED)
                return
        elif key == "art_accent":
            val = val.lower() in ("1", "true", "yes", "on")
        elif key in ("bg", "bg2", "text", "accent", "border") and val and not parse_color(val):
            self.show_toast(f"Bad color · {val}", 2.5, RED)
            return
        self.cfg["style"][key] = val
        self.persist()
        self.apply_theme()
        self.show_toast(f"{key} = {val}", 2.0)

    # ---- CTF events + reminders
    def add_event(self, kind, name, when_s, repeat="none"):
        dt = parse_when(when_s)
        if not dt or not name:
            self.show_toast("Bad date · use YYYY-MM-DD HH:MM", 4.0, RED)
            return
        self.cfg["events"].append({"name": name, "when": dt.strftime("%Y-%m-%d %H:%M"),
                                   "kind": kind, "repeat": repeat})
        self.persist()
        self.show_toast(f"{'CTF' if kind == 'ctf' else 'Reminder'} added · {name}", 3.0, GREEN)

    def forget_event(self, name):
        before = len(self.cfg["events"])
        self.cfg["events"] = [e for e in self.cfg["events"]
                              if not (isinstance(e, dict) and str(e.get("name", "")).lower() == name.lower())]
        self.persist()
        removed = before - len(self.cfg["events"])
        self.show_toast(f"Removed {removed} event(s)" if removed else "No event with that name", 2.5)

    def upcoming(self, kind):
        now = datetime.now()
        best = None
        for ev in self.cfg.get("events", []):
            if not isinstance(ev, dict) or ev.get("kind") != kind:
                continue
            occ = next_occurrence(ev, now)
            if occ and occ >= now and (best is None or occ < best[0]):
                best = (occ, ev)
        return best

    def check_events(self):
        now = datetime.now()
        for ev in self.cfg.get("events", []):
            if not isinstance(ev, dict):
                continue
            occ = next_occurrence(ev, now)
            if not occ:
                continue
            offsets = (1440, 60, 10, 0) if ev.get("kind") == "ctf" else (10, 0)
            for off in offsets:
                at = occ - timedelta(minutes=off)
                key = (ev.get("name"), occ.isoformat(), off)
                if at <= now < at + timedelta(seconds=120) and key not in self.fired:
                    self.fired.add(key)
                    self.fire_event(ev, off)
        self.prune_shelf()
        self.wake()                     # re-evaluate quiet hours / automatic mode

    def fire_event(self, ev, off):
        label = "CTF" if ev.get("kind") == "ctf" else "Reminder"
        if off == 0:
            body = "Starting now"
        elif off >= 1440:
            body = "Starts in 1 day"
        elif off >= 60:
            body = f"Starts in {off // 60} hour"
        else:
            body = f"Starts in {off} min"
        self.post("notif", 8.0, app=label, title=str(ev.get("name", "")), body=body, icon=None)

    # ---- file shelf (temporary: kept in memory only)
    def shelf_note(self, text, secs=3.0):
        self.shelf_msg = (text, time.monotonic() + secs)
        self.wake()

    def make_item(self, pth):
        isdir = os.path.isdir(pth)
        try:
            info = f"{len(os.listdir(pth))} items" if isdir else human_size(os.path.getsize(pth))
        except OSError:
            info = "Folder" if isdir else ""
        return {"path": pth, "name": os.path.basename(pth.rstrip("/")) or pth,
                "info": info, "added": time.time()}

    def add_files(self, paths, announce=False):
        added = full = dup = 0
        for pth in paths:
            pth = os.path.abspath(os.path.expanduser(pth))
            if not os.path.exists(pth):
                continue
            if any(i["path"] == pth for i in self.shelf):
                dup += 1
            elif len(self.shelf) >= MAX_SHELF:
                full += 1
            else:
                self.shelf.append(self.make_item(pth))
                added += 1
        if added:
            msg = f"Added {added}" + (f" · {full} skipped (shelf full)" if full else "")
        elif full:
            msg = f"Shelf full ({MAX_SHELF}) · remove one first"
        elif dup:
            msg = "Already on the shelf"
        else:
            msg = "Nothing to add"
        self.shelf_note(msg)
        if announce:
            self.show_toast(msg, 2.5, GREEN if added else RED)
        return added

    def prune_shelf(self):
        try:
            exp = float(self.cfg.get("shelf_expire_minutes", 0) or 0)
        except (TypeError, ValueError):
            exp = 0
        now = time.time()
        keep = [i for i in self.shelf
                if os.path.exists(i["path"]) and not (exp and now - i["added"] > exp * 60)]
        if len(keep) != len(self.shelf):
            self.shelf = keep
            self.wake()

    def shelf_clear(self):
        self.shelf = []
        self.shelf_note("Shelf cleared")

    def shelf_copy(self, items):
        items = [i for i in items if os.path.exists(i["path"])]
        if not items:
            self.shelf_note("Nothing to copy")
            return
        files = [Gio.File.new_for_path(i["path"]) for i in items]
        uris = [f.get_uri() for f in files]
        label = items[0]["name"] if len(items) == 1 else f"{len(items)} items"
        if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            try:                        # wl-copy works without keyboard focus (we never take focus)
                p = Gio.Subprocess.new(["wl-copy", "--type", "text/uri-list"],
                                       Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
                p.get_stdin_pipe().write_all(("\r\n".join(uris) + "\r\n").encode(), None)
                p.get_stdin_pipe().close(None)
                self.shelf_note(f"Copied {label} · Ctrl+V to paste")
                return
            except GLib.Error:
                pass
        fl = Gdk.FileList.new_from_list(files)
        val = GObject.Value()
        val.init(Gdk.FileList)
        val.set_boxed(fl)
        providers = [
            Gdk.ContentProvider.new_for_value(val),
            Gdk.ContentProvider.new_for_bytes("application/x-kde-cutselection", GLib.Bytes.new(b"0")),
            Gdk.ContentProvider.new_for_bytes("x-special/gnome-copied-files",
                                              GLib.Bytes.new(("copy\n" + "\n".join(uris)).encode())),
            Gdk.ContentProvider.new_for_value("\n".join(i["path"] for i in items)),
        ]
        self.get_clipboard().set_content(Gdk.ContentProvider.new_union(providers))
        self.shelf_note(f"Copied {label} · Ctrl+V to paste")

    def paste_files(self, announce=False):
        """Add files that were copied (Ctrl+C) in Dolphin / any file manager."""
        def finish(paths):
            if paths:
                self.add_files(paths, announce=announce)
            else:
                self.shelf_note("Clipboard has no files · copy some first")
                if announce:
                    self.show_toast("Clipboard has no files", 2.5, RED)

        def parse(text):
            paths = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("file://"):
                    line = Gio.File.new_for_uri(line).get_path() or ""
                if line.startswith("/") and os.path.exists(line):
                    paths.append(line)
            return paths

        def gtk_fallback():
            cb = self.get_clipboard()

            def got(c, res):
                try:
                    fl = c.read_value_finish(res)
                    finish([f.get_path() for f in fl.get_files() if f.get_path()])
                except GLib.Error:
                    c.read_text_async(None, lambda c2, r2: finish(parse(self._safe_text(c2, r2))))

            cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, got)

        if shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY"):
            def got_uris(out):
                paths = parse(out)
                if paths:
                    finish(paths)
                else:
                    run_async(["wl-paste", "--no-newline"], lambda o: finish(parse(o)) if parse(o) else gtk_fallback())

            run_async(["wl-paste", "--no-newline", "--type", "text/uri-list"], got_uris)
        else:
            gtk_fallback()

    @staticmethod
    def _safe_text(cb, res):
        try:
            return cb.read_text_finish(res) or ""
        except GLib.Error:
            return ""

    def launch(self, item):
        cmd = str(item.get("cmd", ""))
        if not cmd:
            return
        try:
            Gio.AppInfo.create_from_commandline(cmd, None, Gio.AppInfoCreateFlags.NONE).launch([], None)
        except GLib.Error as e:
            self.show_toast(f"Could not launch {item.get('name', cmd)}", 3.0, RED)
            print("[island] launch failed:", e.message, file=sys.stderr)
            return
        self.expanded = False
        self.show_toast(f"Launching {item.get('name', cmd)}", 1.6)

    # ---------------------------------------------- system stats (CPU/RAM/net) --
    def sample_system(self):
        try:
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:9]]
            idle, total = v[3] + v[4], sum(v)
            if self.cpu_prev and total > self.cpu_prev[0]:
                busy = 1 - (idle - self.cpu_prev[1]) / (total - self.cpu_prev[0])
                self.cpu_hist.append(max(0.0, min(100.0, busy * 100)))
            self.cpu_prev = (total, idle)
        except (OSError, ValueError, IndexError):
            pass
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, val = line.partition(":")
                    if k in ("MemTotal", "MemAvailable"):
                        info[k] = int(val.split()[0])
                    if len(info) == 2:
                        break
            self.ram = 100 * (1 - info["MemAvailable"] / info["MemTotal"])
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        try:
            rx = tx = 0
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    name, _, data = line.partition(":")
                    if name.strip() == "lo":
                        continue
                    cols = data.split()
                    rx += int(cols[0])
                    tx += int(cols[8])
            now = time.monotonic()
            if self.net_prev:
                dt = max(0.001, now - self.net_prev[2])
                self.net_rate = (max(0.0, (rx - self.net_prev[0]) / dt),
                                 max(0.0, (tx - self.net_prev[1]) / dt))
            self.net_prev = (rx, tx, now)
        except (OSError, ValueError, IndexError):
            pass
        if self.temp_path:
            t = read_int(self.temp_path)
            self.temp = t / 1000 if t else None

    # ---------------------------------------------- Caps/Num lock + brightness --
    def poll_misc(self):
        self._misc_n += 1
        if any(self.led_paths.values()):
            for name, paths in self.led_paths.items():
                if paths:
                    self.set_lock(name, any((read_int(p) or 0) > 0 for p in paths))
        elif self.xset_fails < 5 and self._misc_n % 3 == 0 and not os.environ.get("WAYLAND_DISPLAY"):
            self.poll_xset()
        if self.backlight:
            b = read_int(self.backlight + "/brightness")
            mx = read_int(self.backlight + "/max_brightness")
            if b is not None and mx:
                pct = round(100 * b / mx)
                if self.bright is not None and pct != self.bright:
                    self.post("brightness", 1.6, vol=pct, mute=False)
                self.bright = pct

    def set_lock(self, name, on):
        old = self.leds.get(name)
        self.leds[name] = on
        if old is not None and old != on:
            self.show_toast(f"{name} · {'ON' if on else 'off'}", 1.6, ORANGE if on else self.c_sub)

    def poll_xset(self):
        def got(out):
            caps = re.search(r"Caps Lock:\s+(on|off)", out)
            num = re.search(r"Num Lock:\s+(on|off)", out)
            if not caps:
                self.xset_fails += 1
                return
            self.set_lock("Caps Lock", caps.group(1) == "on")
            if num:
                self.set_lock("Num Lock", num.group(1) == "on")

        run_async(["xset", "q"], got)

    # ------------------------------------------------ VPN / Wi-Fi / network --
    def net_snapshot(self):
        base = "/sys/class/net"
        try:
            names = os.listdir(base)
        except OSError:
            return None
        vpn, wifi, eth = set(), False, False
        for n in names:
            if re.match(r"(tun|tap|wg|ppp|tailscale)", n):
                vpn.add(n)
                continue
            if n == "lo" or n.startswith(("docker", "br-", "veth", "virbr", "vmnet")):
                continue
            try:
                with open(f"{base}/{n}/operstate") as f:
                    state = f.read().strip()
            except OSError:
                continue
            if state != "up":
                continue
            if os.path.isdir(f"{base}/{n}/wireless"):
                wifi = True
            else:
                eth = True
        return vpn, wifi, eth

    def poll_network(self):
        snap = self.net_snapshot()
        if snap is None:
            return
        vpn, wifi, eth = snap
        if self.net_state is not None:
            pvpn, pwifi, peth = self.net_state
            for n in sorted(vpn - pvpn):
                self.announce_vpn(n)
            for n in sorted(pvpn - vpn):
                self.show_toast(f"VPN disconnected · {n}", 3.5, RED)
            online, was = wifi or eth, pwifi or peth
            if online and not was:
                if wifi:
                    self.announce_wifi()
                else:
                    self.show_toast("Ethernet connected", 3.0, GREEN)
            elif was and not online:
                self.show_toast("Network disconnected", 3.5, RED)
        self.net_state = snap
        if vpn != self.vpn:
            self.vpn = vpn
            self.wake()

    def announce_vpn(self, name):
        def got(out):
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
            self.show_toast(f"VPN connected · {m.group(1) if m else name}", 4.0, GREEN)

        run_async(["ip", "-4", "-o", "addr", "show", "dev", name], got)

    def announce_wifi(self):
        def got(out):
            ssid = ""
            for line in out.splitlines():
                if line.startswith("yes:"):
                    ssid = line[4:].replace("\\:", ":")
                    break
            self.show_toast(f"Wi-Fi connected · {ssid}" if ssid else "Wi-Fi connected", 3.5, GREEN)

        run_async(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], got)

    # ---------------------------------------------------------------- music --
    def poll_music(self):
        if self._music_busy:
            return
        self._music_busy = True
        run_async(["playerctl", "metadata", "--format", PLAYER_FORMAT], self.on_music)

    def on_music(self, out):
        self._music_busy = False
        parts = out.rstrip("\r\n").split("\t")
        if len(parts) < 7:
            if self.music is not None:
                self.music = None
                self.wake()
            self.first_poll_done = True
            return

        def num(s):
            try:
                return float(s) / 1e6
            except ValueError:
                return 0.0

        status, artist, title, album, pos, length, art_url = parts[:7]
        new = dict(status=status, artist=artist, title=title or "Unknown", album=album,
                   pos=num(pos), length=num(length), t=time.monotonic())
        old = self.music
        self.music = new
        self.load_art(art_url)
        changed = old is None or (old["title"], old["artist"]) != (new["title"], new["artist"])
        if changed and status == "Playing" and not self.expanded and self.first_poll_done:
            self.post("track", 3.5)
        self.first_poll_done = True
        self.wake()

    def music_position(self):
        m = self.music
        if not m:
            return 0.0
        pos = m["pos"]
        if m["status"] == "Playing":
            pos += time.monotonic() - m["t"]
        return min(pos, m["length"]) if m["length"] else pos

    def player(self, *args):
        run_async(["playerctl", *args])
        GLib.timeout_add(250, lambda: (self.poll_music(), False)[1])

    def load_art(self, url):
        if url == self.art_url:
            return
        self.art_url = url
        self.set_art(None)
        if url.startswith("file://"):
            try:
                self.set_art(GdkPixbuf.Pixbuf.new_from_file(Gio.File.new_for_uri(url).get_path()))
            except GLib.Error:
                pass
        elif url.startswith("http"):
            def work():
                try:
                    with urllib.request.urlopen(url, timeout=6) as r:
                        data = r.read(8_000_000)
                except Exception:
                    return
                GLib.idle_add(self.art_downloaded, url, data)

            threading.Thread(target=work, daemon=True).start()

    def art_downloaded(self, url, data):
        if url == self.art_url:
            pb = load_pixbuf_bytes(data)
            if pb is not None:
                self.set_art(pb)
        return False

    def set_art(self, pb):
        """Keep a 160x160 square cairo surface of the cover (and derive the accent color)."""
        self.art_pb = None
        self.art = None
        if pb is not None:
            w, h = pb.get_width(), pb.get_height()
            if w > 0 and h > 0:
                k = max(160 / w, 160 / h)
                sw, sh = max(160, round(w * k)), max(160, round(h * k))
                s = pb.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
                s = s.new_subpixbuf((sw - 160) // 2, (sh - 160) // 2, 160, 160)
                self.art_pb = s
                self.art = pixbuf_surface(s)
        self.restyle_art()

    def restyle_art(self):
        self.accent = self.theme_accent
        pb = getattr(self, "art_pb", None)
        if pb is not None and self.theme_art_accent:
            px = pb.scale_simple(1, 1, GdkPixbuf.InterpType.BILINEAR).get_pixels()
            h, light, sat = colorsys.rgb_to_hls(px[0] / 255, px[1] / 255, px[2] / 255)
            if sat >= 0.15:
                r, g, b = colorsys.hls_to_rgb(h, min(0.72, max(light, 0.58)), min(1.0, max(sat, 0.55)))
                self.accent = (r, g, b, 1.0)
        self.wake()

    # --------------------------------------------------------------- volume --
    def start_pactl(self):
        self._vol_id = None

        def on_line(line):
            if re.search(r"'change' on sink #", line):
                if self._vol_id:
                    GLib.source_remove(self._vol_id)
                self._vol_id = GLib.timeout_add(40, self._vol_fire)

        s = stream_lines(["pactl", "subscribe"], on_line)
        if s:
            self._streams.append(s)
        self.query_volume(silent=True)

    def _vol_fire(self):
        self._vol_id = None
        self.query_volume()
        return False

    def query_volume(self, silent=False):
        def got_volume(out):
            m = re.search(r"(\d+)%", out)
            if not m:
                return
            vol = int(m.group(1))

            def got_mute(out2):
                muted = "yes" in out2.lower()
                new = (vol, muted)
                if new != self.volume:
                    first = self.volume is None
                    self.volume = new
                    if not silent and not first:
                        self.post("volume", 1.8, vol=vol, mute=muted)

            run_async(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], got_mute)

        run_async(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], got_volume)

    # -------------------------------------------------------------- battery --
    def poll_battery(self):
        if not BATTERY:
            return
        try:
            with open(BATTERY + "/capacity") as f:
                pct = int(f.read().strip())
            with open(BATTERY + "/status") as f:
                status = f.read().strip()
        except (OSError, ValueError):
            return
        plugged = status in ("Charging", "Full", "Not charging")
        if self.bat is not None:
            was_plugged = self.bat[2]
            if plugged and not was_plugged:
                label = "Fully charged" if status == "Full" else "Charging"
                self.post("battery", 4.0, pct=pct, charging=True, label=label)
            elif was_plugged and not plugged:
                self.post("battery", 3.0, pct=pct, charging=False, label="On battery")
        if not plugged:
            for level in (20, 10):
                if pct <= level and level not in self.low_warned:
                    self.low_warned.add(level)
                    self.post("battery", 5.0, pct=pct, charging=False, label="Low battery")
                    break
        else:
            self.low_warned.clear()
        self.bat = (pct, status, plugged)

    # -------------------------------------------------------- notifications --
    def start_dbus(self):
        s = stream_lines(["dbus-monitor", "--session",
                          "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"],
                         self.on_dbus_line)
        if s:
            self._streams.append(s)

    def on_dbus_line(self, ln):
        if ln.startswith("method call") and "member=Notify" in ln:
            self._dbuf_lines = []
        elif self._dbuf_lines is not None:
            s = ln.strip()
            if s.startswith(("array [", "method call", "method return", "signal")):
                self.handle_notify(self._dbuf_lines)
                self._dbuf_lines = None
            else:
                self._dbuf_lines.append(ln)

    def handle_notify(self, lines):
        import html
        items = []
        for ln in lines:
            s = ln.strip()
            if s.startswith('string "'):
                items.append(s[len('string "'):])
            elif s.startswith(("uint32", "int32")):
                continue
            elif items:                 # continuation of a multi-line string
                items[-1] += "\n" + ln.rstrip()
        strings = [v[:-1] if v.endswith('"') else v for v in items]
        if len(strings) < 3:
            return
        app, icon, summary = strings[0], strings[1], strings[2]
        body = strings[3] if len(strings) > 3 else ""
        body = html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\n", " ").strip()
        summary = html.unescape(re.sub(r"<[^>]+>", "", summary)).strip()
        if not summary and not body:
            return
        pic = self.icon_surface(icon) or self.icon_surface(app.lower())
        self.post("notif", 5.5, app=app, title=summary or app, body=body, icon=pic)

    # ---------------------------------------------------------------- icons --
    def icon_surface(self, name, size=64):
        """Icon by theme name or file path -> cairo surface (None if not found)."""
        if not name:
            return None
        key = (name, size)
        if key in self.icon_cache:
            return self.icon_cache[key]
        surf = None
        try:
            path = None
            if name.startswith("file://"):
                path = Gio.File.new_for_uri(name).get_path()
            elif name.startswith("/"):
                path = name
            else:
                theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
                if theme.has_icon(name):
                    paintable = theme.lookup_icon(name, None, size, 1, Gtk.TextDirection.NONE, 0)
                    f = paintable.get_file()
                    path = f.get_path() if f else None
            if path and os.path.exists(path):
                surf = pixbuf_surface(GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size))
        except Exception:
            surf = None
        self.icon_cache[key] = surf
        return surf

    def file_icon(self, path):
        if path not in self.file_icons:
            surf = None
            try:
                info = Gio.File.new_for_path(path).query_info("standard::icon", 0, None)
                icon = info.get_icon()
                theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
                paintable = theme.lookup_by_gicon(icon, 32, 1, Gtk.TextDirection.NONE, 0)
                f = paintable.get_file()
                if f and f.get_path():
                    surf = pixbuf_surface(GdkPixbuf.Pixbuf.new_from_file_at_size(f.get_path(), 32, 32))
            except Exception:
                surf = None
            self.file_icons[path] = surf
        return self.file_icons[path]

    # ---------------------------------------------------------- animation --
    def desired(self):
        t = self.transient
        em = self.effective_mode()
        if t:
            if time.monotonic() >= t["until"]:
                self.transient = None
            elif em != "closed" or t.get("force"):
                return t["kind"], t
        if em == "closed":
            return "hidden", None
        if self.expanded:
            return "expanded", None
        return ("compact" if em == "standby" else "hidden"), None

    def compact_active(self):
        return bool(self.timer_end) or (
            self.music is not None and self.music["status"] in ("Playing", "Paused"))

    def size_for(self, mode):
        if mode == "compact":
            w, h = COMPACT_ACTIVE if self.compact_active() else COMPACT_IDLE
            return (w + 8 if self.hover else w), h
        return SIZES[mode]

    def pill_rect(self):
        w, h = max(0.0, self.sw.x), max(0.0, self.sh.x)
        return ((CANVAS_W - w) / 2, 0.0, w, h)

    def tick(self):
        """One animation step. Returns the delay (ms) until the next one, or 0 when everything is still."""
        now = time.monotonic()
        dt = min(now - self._last, 0.04)
        self._last = now

        if self.timer_end and time.time() >= self.timer_end:
            self.timer_end = None
            if self.pomo_active:
                was_focus = self.pomo_phase == "focus"
                self.pomo_next()
                msg = "Focus done · take a break" if was_focus else "Break over · back to focus"
                self.show_toast(msg, 6.0, self.timer_color)
            else:
                self.show_toast("Timer finished", 6.0)

        want, payload = self.desired()
        self.want = want
        if want != "hidden" and not self.get_visible():
            self._last_mask = None
            self.present()              # pop out of hiding
        if want == self.mode and not self.fading_out:
            self.payload = payload
        elif not self.fading_out:
            self.fading_out = True

        # fade old content out -> resize -> fade new content in
        if self.fading_out:
            self.alpha = max(0.0, self.alpha - dt / 0.09)
            if self.alpha <= 0.0:
                self.mode = self.want
                self.payload = payload
                self.fading_out = False
                self.fade_delay = 0.10
        elif self.fade_delay > 0:
            self.fade_delay -= dt
        elif self.alpha < 1.0:
            self.alpha = min(1.0, self.alpha + dt / 0.18)

        tw, th = self.size_for(self.mode)
        self.sw.target, self.sh.target = tw, th
        for sp in (self.sw, self.sh):
            sp.step(dt)
            if sp.settled():
                sp.snap()

        # smoothly follow the background color (themes, album tint)
        tgt = self.theme_bg
        if self.theme_tint > 0 and self.art and self.mode != "hidden":
            tgt = mix(tgt, with_alpha(self.accent, tgt[3]), self.theme_tint)
        f = 1 - math.exp(-dt * 6)
        diff = max(abs(tgt[i] - self.bg_cur[i]) for i in range(4))
        self.bg_cur = tgt if diff < 0.01 else mix(self.bg_cur, tgt, f)
        bg_busy = self.bg_cur != tgt

        # only the pill itself receives clicks / drops; the rest of the surface is click-through
        surf = self.get_surface()
        if surf is not None:
            px, py, pw, ph = self.pill_rect()
            region = (id(surf), int(px) - 1, int(py), int(pw) + 2, max(1, int(ph) + 1))
            if region != self._last_mask:
                self._last_mask = region
                try:
                    surf.set_input_region(cairo.Region(cairo.RectangleInt(*region[1:])))
                except Exception:
                    pass
        self.area.queue_draw()

        if (self.mode == "hidden" and self.want == "hidden" and not self.fading_out
                and self.sw.settled() and self.sh.settled() and self.get_visible()):
            self.set_visible(False)     # fully tucked away: no surface, no clicks, no CPU

        animating = (not self.sw.settled() or not self.sh.settled() or self.fading_out
                     or self.fade_delay > 0 or self.alpha < 1.0 or self.want != self.mode or bg_busy)
        playing = self.music is not None and self.music["status"] == "Playing"
        steady = bool(self.transient) or bool(self.timer_end) or self.mode == "expanded" or (
            playing and self.mode in ("compact", "track"))
        if animating:
            return 16
        return 40 if steady else 0

    # -------------------------------------------------------------- input --
    def setup_input(self):
        click = Gtk.GestureClick()
        click.set_button(0)                         # any mouse button
        click.connect("pressed", lambda g, n, x, y: self.click(x, y, g.get_current_button()))
        click.connect("released", self.on_released)
        self.area.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self.on_enter)
        motion.connect("leave", self.on_leave)
        motion.connect("motion", self.on_motion)
        self.area.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.area.add_controller(scroll)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)       # files dropped INTO the island
        drop.connect("enter", self.on_drop_enter)
        drop.connect("leave", self.on_drop_leave)
        drop.connect("drop", self.on_drop)
        self.area.add_controller(drop)

        drag = Gtk.DragSource()                                           # files dragged OUT of the shelf
        drag.set_actions(Gdk.DragAction.COPY)
        drag.connect("prepare", self.on_drag_prepare)
        drag.connect("drag-end", lambda *a: self.wake())
        self.area.add_controller(drag)

    def click(self, x, y, button):
        if DEBUG:
            print("[island] click", button, round(x), round(y), self.mode, flush=True)
        if button == 3:
            self.open_page("settings")
            return
        if button == 2:
            self.player("play-pause")
            return
        if self.transient and self.mode == self.transient["kind"]:
            self.transient = None       # dismiss
            self.wake()
            return
        if self.mode == "expanded":
            for h in reversed(self.hits):
                if hit(h["r"], x, y):
                    if h.get("row") is not None:        # shelf row: click = copy, drag = drag out
                        self._row_pending = h["row"]
                    else:
                        h["fn"](x, y)
                    self.wake()
                    return
            self.toggle_expanded()                      # blank area closes it
            return
        self.toggle_expanded()

    def on_released(self, gesture, n, x, y):
        i, self._row_pending = self._row_pending, None
        if i is not None and i < len(self.shelf):
            self.shelf_copy([self.shelf[i]])

    def on_enter(self, ctrl, x, y):
        self.hover = True
        self.hover_pos = (x, y)
        self.stop_collapse()
        self.wake()

    def on_leave(self, ctrl):
        self.hover = False
        self.hover_pos = None
        if self.expanded:
            self.start_collapse(2500)
        self.wake()

    def on_motion(self, ctrl, x, y):
        self.hover_pos = (x, y)

    def on_scroll(self, ctrl, dx, dy):
        now = time.monotonic()
        if self.mode != "expanded" or not dy or now - self.last_scroll < 0.25:
            return False
        self.last_scroll = now
        self.set_page((self.page + (1 if dy > 0 else -1)) % len(PAGES))
        return True

    # drag & drop INTO the island: it opens the shelf so you have a big target
    def begin_drag_over(self):
        self.stop_collapse()
        self.transient = None
        self.expanded = True
        self.peek_until = time.monotonic() + 8.0
        self.set_page(PAGES.index("shelf"))
        self.wake()

    def on_drop_enter(self, target, x, y):
        if DEBUG:
            print("[island] drag enter", flush=True)
        if self.effective_mode() == "closed" and time.monotonic() >= self.peek_until:
            return 0
        self.dragging = True
        self.begin_drag_over()
        return Gdk.DragAction.COPY

    def on_drop_leave(self, target):
        self.dragging = False
        if self.expanded:
            self.start_collapse(2500)
        self.wake()

    def on_drop(self, target, value, x, y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if DEBUG:
            print("[island] drop:", paths, flush=True)
        self.dragging = False
        self.add_files(paths)
        self.start_collapse(6000)
        self.wake()
        return True

    def on_drag_prepare(self, source, x, y):
        idx = None
        for h in self.hits:
            if h.get("row") is not None and hit(h["r"], x, y):
                idx = h["row"]
        if idx is None or idx >= len(self.shelf):
            return None                                 # not on a shelf row: no drag
        self._row_pending = None
        everything = bool(source.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        items = [i for i in (self.shelf if everything else [self.shelf[idx]]) if os.path.exists(i["path"])]
        if not items:
            return None
        fl = Gdk.FileList.new_from_list([Gio.File.new_for_path(i["path"]) for i in items])
        val = GObject.Value()
        val.init(Gdk.FileList)
        val.set_boxed(fl)
        return Gdk.ContentProvider.new_for_value(val)

    # ------------------------------------------------------------ drawing --
    def text(self, cr, rect, s, size=11, bold=False, color=None, align="l"):
        x, y, w, h = rect
        if not s or w <= 0:
            return
        layout = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription()
        fd.set_family(self.font_family)
        fd.set_size(int(size * Pango.SCALE))
        fd.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(fd)
        layout.set_text(str(s), -1)
        layout.set_width(int(w * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_single_paragraph_mode(True)
        layout.set_alignment({"l": Pango.Alignment.LEFT, "c": Pango.Alignment.CENTER,
                              "r": Pango.Alignment.RIGHT}[align])
        _tw, th = layout.get_pixel_size()
        rgba(cr, color or self.c_text)
        cr.move_to(x, y + (h - th) / 2)
        PangoCairo.show_layout(cr, layout)

    def add_hit(self, rect, fn=None, row=None):
        self.hits.append({"r": rect, "fn": fn, "row": row})

    def chip(self, cr, rect, label, active=False, color=None, fn=None, size=8.5):
        x, y, w, h = rect
        color = color or self.c_text
        if active:
            fill_rr(cr, x, y, w, h, h / 2, with_alpha(color, 0.22))
            stroke_rr(cr, x, y, w, h, h / 2, with_alpha(color, 0.75))
            self.text(cr, rect, label, size, True, color, "c")
        else:
            stroke_rr(cr, x, y, w, h, h / 2, self.c_dim)
            self.text(cr, rect, label, size, True, self.c_sub, "c")
        if fn:
            self.add_hit(rect, fn)

    def on_draw(self, area, cr, width, height):
        self.hits = []
        x, y, w, h = self.pill_rect()
        if h < 1:
            return
        rad = min(h / 2, self.corner)
        rrect(cr, x, y, w, h, rad)
        if self.theme_bg2 is not None:
            grad = cairo.LinearGradient(0, y, 0, y + h)
            grad.add_color_stop_rgba(0, *self.bg_cur)
            grad.add_color_stop_rgba(1, *self.theme_bg2)
            cr.set_source(grad)
        else:
            rgba(cr, self.bg_cur)
        cr.fill_preserve()
        rgba(cr, self.c_border)
        cr.set_line_width(1)
        cr.stroke_preserve()
        cr.clip()
        draw = getattr(self, "draw_" + self.mode, None)
        if draw:
            cr.push_group()
            draw(cr, (x, y, w, h))
            cr.pop_group_to_source()
            cr.paint_with_alpha(self.alpha)

    def draw_art(self, cr, rect, radius):
        x, y, w, h = rect
        cr.save()
        rrect(cr, x, y, w, h, radius)
        cr.clip()
        if self.art:
            cr.translate(x, y)
            cr.scale(w / 160.0, h / 160.0)
            cr.set_source_surface(self.art, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_BEST)
            cr.paint()
            cr.restore()
        else:
            rgba(cr, _c(48, 48, 54))
            cr.paint()
            cr.restore()
            self.text(cr, rect, "♪", h * 0.45, False, self.c_sub, "c")

    def draw_eq(self, cr, right, cy, playing):
        t = time.monotonic()
        for i in range(4):
            bh = 4 + (11 * (0.5 + 0.5 * math.sin(t * (6 + i * 1.7) + i * 1.3)) if playing else 0)
            fill_rr(cr, right - 20 + i * 5, cy - bh / 2, 3, bh, 1.5, self.accent)

    def draw_compact(self, cr, r):
        x, y, w, h = r
        cy = y + h / 2
        music = self.music is not None and self.music["status"] in ("Playing", "Paused")
        remaining = max(0.0, self.timer_end - time.time()) if self.timer_end else 0

        if music:
            self.draw_art(cr, (x + 12, cy - 10, 20, 20), 6)
        elif self.timer_end:
            cx, rad = x + 22, 8
            cr.set_line_width(2.5)
            rgba(cr, self.c_dim)
            cr.arc(cx, cy, rad, 0, 2 * math.pi)
            cr.stroke()
            rgba(cr, self.timer_color)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.arc(cx, cy, rad, -math.pi / 2, -math.pi / 2 + (remaining / self.timer_total) * 2 * math.pi)
            cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_BUTT)

        if self.timer_end:
            self.text(cr, (x + w - 64, y, 52, h), fmt_time(remaining + 0.99), 10.5, True,
                      self.timer_color, "r")
        elif music:
            self.draw_eq(cr, x + w - 14, cy, self.music["status"] == "Playing")
        elif self.vpn:                  # small "VPN up" badge while idle
            rgba(cr, GREEN)
            cr.arc(x + w / 2 - 17, cy, 3, 0, 2 * math.pi)
            cr.fill()
            self.text(cr, (x + w / 2 - 10, y, 40, h), "VPN", 9, True, self.c_sub)
        if self.shelf and not music and not self.timer_end:     # files waiting on the shelf
            rgba(cr, self.c_dim)
            cr.arc(x + w - 22, cy, 8, 0, 2 * math.pi)
            cr.fill()
            self.text(cr, (x + w - 30, cy - 8, 16, 16), str(len(self.shelf)), 8, True, self.c_text, "c")

    def draw_track(self, cr, r):
        x, y, w, h = r
        cy = y + h / 2
        m = self.music or {}
        self.draw_art(cr, (x + 10, cy - 19, 38, 38), 9)
        self.text(cr, (x + 58, cy - 21, w - 110, 22), m.get("title", ""), 11.5, True)
        self.text(cr, (x + 58, cy + 1, w - 110, 18), m.get("artist", ""), 10, False, self.c_sub)
        self.draw_eq(cr, x + w - 18, cy, m.get("status") == "Playing")

    def draw_notif(self, cr, r):
        x, y, w, h = r
        t = self.payload or {}
        cy = y + h / 2
        icon = (x + 16, cy - 22, 44, 44)
        cr.save()
        rrect(cr, *icon, 11)
        cr.clip()
        pic = t.get("icon")
        if pic is not None:
            rgba(cr, _c(36, 36, 40))
            cr.paint()
            cr.translate(icon[0], icon[1])
            cr.scale(44.0 / pic.get_width(), 44.0 / pic.get_height())
            cr.set_source_surface(pic, 0, 0)
            cr.paint()
            cr.restore()
        else:
            rgba(cr, BLUE)
            cr.paint()
            cr.restore()
            self.text(cr, icon, "🔔", 15, False, WHITE, "c")
        tx = icon[0] + 44 + 12
        tw = x + w - 16 - tx
        self.text(cr, (tx, y + 10, tw, 14), t.get("app", ""), 8.5, False, self.c_sub)
        self.text(cr, (tx, y + 24, tw, 20), t.get("title", ""), 11.5, True)
        if t.get("body"):
            self.text(cr, (tx, y + 45, tw, 20), t["body"], 10, False, self.c_sub2)

    def draw_battery_icon(self, cr, x, cy, pct, color, bolt):
        stroke_rr(cr, x, cy - 7, 28, 14, 4, self.c_soft, 1.5)
        fill_rr(cr, x + 29.5, cy - 3, 2.5, 6, 1, self.c_soft)
        fill_rr(cr, x + 2.5, cy - 4.5, max(2.0, 23 * pct / 100), 9, 2, color)
        if bolt:
            c = x + 14
            cr.move_to(c + 1.5, cy - 6)
            for dx, dy in ((-4, 1), (-0.5, 1), (-1.5, 6), (4, -1.5), (0.5, -1.5)):
                cr.line_to(c + dx, cy + dy)
            cr.close_path()
            cr.set_source_rgba(0, 0, 0, 0.82)
            cr.fill()

    def draw_battery(self, cr, r):
        x, y, w, h = r
        t = self.payload or {}
        cy = y + h / 2
        pct = t.get("pct", 0)
        low = t.get("label") == "Low battery"
        color = GREEN if t.get("charging") else (RED if low else self.c_text)
        self.draw_battery_icon(cr, x + 18, cy, pct, color, t.get("charging"))
        self.text(cr, (x + 62, y, w - 120, h), t.get("label", ""), 11, True)
        self.text(cr, (x + w - 62, y, 46, h), f"{pct}%", 11, True, color, "r")

    def draw_speaker(self, cr, x, cy, level, muted, color):
        cr.move_to(x, cy - 3)
        for px, py in ((x + 4, cy - 3), (x + 9, cy - 7), (x + 9, cy + 7), (x + 4, cy + 3), (x, cy + 3)):
            cr.line_to(px, py)
        cr.close_path()
        rgba(cr, color)
        cr.fill()
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if muted:
            cr.move_to(x + 13, cy - 4)
            cr.line_to(x + 19, cy + 4)
            cr.move_to(x + 19, cy - 4)
            cr.line_to(x + 13, cy + 4)
            cr.stroke()
        else:
            for i in range(1 if level < 34 else 2 if level < 67 else 3):
                rad = 4 + i * 4
                cr.new_sub_path()
                cr.arc(x + 9, cy, rad, -math.pi / 4, math.pi / 4)
                cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_BUTT)

    def draw_sun(self, cr, x, cy, color):
        cx = x + 9
        rgba(cr, color)
        cr.arc(cx, cy, 4, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1.5)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for i in range(8):
            a = i * math.pi / 4
            cr.move_to(cx + 6.5 * math.cos(a), cy + 6.5 * math.sin(a))
            cr.line_to(cx + 9 * math.cos(a), cy + 9 * math.sin(a))
        cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_BUTT)

    def draw_level(self, cr, r, kind):
        x, y, w, h = r
        t = self.payload or {}
        cy = y + h / 2
        vol, muted = t.get("vol", 0), t.get("mute", False)
        color = self.c_sub if muted else self.c_text
        if kind == "sun":
            self.draw_sun(cr, x + 18, cy, color)
        else:
            self.draw_speaker(cr, x + 18, cy, vol, muted, color)
        track = (x + 52, cy - 3, w - 52 - 62, 6)
        fill_rr(cr, *track, 3, self.c_dim)
        if not muted and vol > 0:
            fill_rr(cr, track[0], track[1], track[2] * min(vol, 100) / 100, 6, 3,
                    self.c_text if vol <= 100 else ORANGE)
        self.text(cr, (x + w - 58, y, 44, h), "Muted" if muted else f"{vol}%", 10.5, True, color, "r")

    def draw_volume(self, cr, r):
        self.draw_level(cr, r, "speaker")

    def draw_brightness(self, cr, r):
        self.draw_level(cr, r, "sun")

    def draw_toast(self, cr, r):
        x, y, w, h = r
        t = self.payload or {}
        self.text(cr, (x + 18, y, w - 36, h), t.get("text", ""), 11, True, t.get("color") or self.c_text, "c")

    def draw_icon(self, cr, kind, rect, color=None):
        x, y, w, h = rect
        rgba(cr, color or self.c_text)
        if kind == "play":
            cr.move_to(x + w * 0.28, y + h * 0.15)
            cr.line_to(x + w * 0.28, y + h * 0.85)
            cr.line_to(x + w * 0.82, y + h * 0.5)
            cr.close_path()
        elif kind == "pause":
            rrect(cr, x + w * 0.22, y + h * 0.15, w * 0.2, h * 0.7, 2)
            rrect(cr, x + w * 0.58, y + h * 0.15, w * 0.2, h * 0.7, 2)
        else:                           # prev / next: two triangles
            d = 1 if kind == "next" else -1
            cx = x + w / 2
            for off in (-0.2, 0.2):
                a = cx + d * w * off
                cr.move_to(a - d * w * 0.2, y + h * 0.25)
                cr.line_to(a - d * w * 0.2, y + h * 0.75)
                cr.line_to(a + d * w * 0.2, y + h * 0.5)
                cr.close_path()
        cr.fill()

    # ---- expanded view: pages + dots + chips
    def draw_expanded(self, cr, r):
        x, y, w, h = r
        getattr(self, "page_" + PAGES[self.page])(cr, r)
        n = len(PAGES)
        x0 = x + w / 2 - n * 9
        for i in range(n):
            rect = (x0 + i * 18, y + h - 20, 18, 16)
            rgba(cr, self.c_text if i == self.page else self.c_dim)
            cr.arc(rect[0] + 9, rect[1] + 8, 2.5, 0, 2 * math.pi)
            cr.fill()
            self.add_hit(rect, lambda px, py, i=i: self.set_page(i))
        mc = (x + w - 86, y + h - 22, 70, 18)           # mode chip (bottom-right)
        color = MODE_COLORS[self.vis_mode]
        stroke_rr(cr, *mc, 9, with_alpha(color, 0.43))
        self.text(cr, mc, MODE_LABELS[self.vis_mode], 8.5, True, color, "c")
        self.add_hit((mc[0] - 4, mc[1] - 4, mc[2] + 8, mc[3] + 8), lambda px, py: self.set_mode("cycle"))
        tc = (x + 16, y + h - 22, 96, 18)               # theme chip (bottom-left)
        stroke_rr(cr, *tc, 9, self.c_dim)
        self.text(cr, tc, self.theme_label(), 8.5, True, self.c_sub, "c")
        self.add_hit((tc[0] - 4, tc[1] - 4, tc[2] + 8, tc[3] + 8), lambda px, py: self.set_theme("next", toast=False))

    def page_home(self, cr, r):
        x, y, w, h = r
        m = self.music
        if not m:
            now = datetime.now()
            self.text(cr, (x, y + 14, w, 64), now.strftime("%H:%M"), 40, False, self.c_text, "c")
            self.text(cr, (x, y + 82, w, 22), now.strftime("%A, %d %B"), 11, False, self.c_sub, "c")
            self.text(cr, (x, y + 114, w, 20), "Nothing playing", 9.5, False, self.c_dim, "c")
            return
        cx = x + w / 2
        art = (x + 20, y + 16, 64, 64)
        self.draw_art(cr, art, 12)
        tx = art[0] + 64 + 14
        tw = x + w - 20 - tx
        self.text(cr, (tx, y + 24, tw, 22), m["title"], 13, True)
        self.text(cr, (tx, y + 48, tw, 18), m["artist"] or m["album"], 10.5, False, self.c_sub)
        if m["status"] == "Playing":
            self.draw_eq(cr, x + w - 22, y + 52, True)

        pos = self.music_position()
        bar = (x + 20, y + 94, w - 40, 5)
        fill_rr(cr, *bar, 2.5, self.c_dim)
        if m["length"]:
            fill_rr(cr, bar[0], bar[1], bar[2] * min(1, pos / m["length"]), bar[3], 2.5, self.accent)
        self.text(cr, (bar[0], bar[1] + bar[3] + 3, 60, 14), fmt_time(pos), 8.5, False, self.c_sub)
        if m["length"]:
            self.text(cr, (bar[0] + bar[2] - 60, bar[1] + bar[3] + 3, 60, 14), fmt_time(m["length"]),
                      8.5, False, self.c_sub, "r")

        def seek(px, py):
            if not self.music["length"]:
                return
            secs = min(1.0, max(0.0, (px - bar[0]) / bar[2])) * self.music["length"]
            self.music["pos"], self.music["t"] = secs, time.monotonic()
            self.player("position", f"{secs:.1f}")

        def toggle(px, py):
            self.music["status"] = "Paused" if self.music["status"] == "Playing" else "Playing"
            self.music["pos"], self.music["t"] = self.music_position(), time.monotonic()
            self.player("play-pause")

        prev, play, nxt = (cx - 86, y + 118, 32, 32), (cx - 20, y + 116, 40, 36), (cx + 54, y + 118, 32, 32)
        self.draw_icon(cr, "prev", prev)
        self.draw_icon(cr, "pause" if m["status"] == "Playing" else "play", play)
        self.draw_icon(cr, "next", nxt)
        self.add_hit((bar[0], bar[1] - 10, bar[2], bar[3] + 20), seek)
        self.add_hit(prev, lambda px, py: self.player("previous"))
        self.add_hit(play, toggle)
        self.add_hit(nxt, lambda px, py: self.player("next"))

    def page_shelf(self, cr, r):
        x, y, w, h = r
        copy_r = (x + w - 22 - 52 - 6 - 66, y + 10, 66, 18)
        clear_r = (x + w - 22 - 52, y + 10, 52, 18)
        paste_r = (copy_r[0] - 6 - 48, y + 10, 48, 18)
        msg, until = self.shelf_msg
        if msg and time.monotonic() < until:
            self.text(cr, (x + 22, y + 10, paste_r[0] - x - 28, 18), msg, 9, True, self.accent)
        else:
            self.text(cr, (x + 22, y + 10, 160, 18), f"SHELF  {len(self.shelf)}/{MAX_SHELF}", 8.5, True, self.c_sub)
        for rect, label, fn, enabled in (
                (paste_r, "Paste", lambda px, py: self.paste_files(), True),
                (copy_r, "Copy all", lambda px, py: self.shelf_copy(self.shelf), bool(self.shelf)),
                (clear_r, "Clear", lambda px, py: self.shelf_clear(), bool(self.shelf))):
            fill_rr(cr, *rect, 9, self.c_btn)
            self.text(cr, rect, label, 8.5, True, self.c_text if enabled else self.c_dim, "c")
            self.add_hit(rect, fn)

        area = (x + 16, y + 32, w - 32, 118)
        if self.dragging:
            stroke_rr(cr, *area, 12, self.accent, 1.6, [5, 4])
        elif not self.shelf:
            stroke_rr(cr, *area, 12, self.c_dim, 1.2, [5, 4])
        if not self.shelf:
            self.text(cr, (area[0], area[1] + 28, area[2], 24),
                      "Release to add" if self.dragging else "Drop files or folders here",
                      12, True, self.accent if self.dragging else self.c_sub, "c")
            self.text(cr, (area[0], area[1] + 56, area[2], 18),
                      f"Up to {MAX_SHELF} · or copy files, then press Paste", 8.5, False, self.c_dim, "c")
            return
        for i, it in enumerate(self.shelf):
            rect = (x + 16, y + 34 + i * 23, w - 32, 21)
            xr = (rect[0] + rect[2] - 26, rect[1], 24, rect[3])
            if self.hover_pos is not None and hit(rect, *self.hover_pos):
                fill_rr(cr, *rect, 8, self.c_btn)
            ic = self.file_icon(it["path"])
            if ic is not None:
                cr.save()
                cr.translate(rect[0] + 6, rect[1] + 2.5)
                cr.scale(16.0 / ic.get_width(), 16.0 / ic.get_height())
                cr.set_source_surface(ic, 0, 0)
                cr.paint()
                cr.restore()
            else:                       # simple built-in glyph when the icon theme has nothing
                gx, gy = rect[0] + 6, rect[1] + 2.5
                rgba(cr, ORANGE if os.path.isdir(it["path"]) else self.c_sub2)
                rrect(cr, gx + 1, gy, 14, 16, 3)
                cr.fill()
            self.text(cr, (rect[0] + 30, rect[1], rect[2] - 30 - 90, rect[3]), it["name"], 10)
            self.text(cr, (rect[0] + rect[2] - 90, rect[1], 62, rect[3]), it["info"], 8.5, False,
                      self.c_sub, "r")
            self.text(cr, xr, "×", 12, True, self.c_sub, "c")
            self.add_hit(rect, row=i)                                   # click = copy, drag = drag out
            self.add_hit(xr, lambda px, py, i=i: (self.shelf.pop(i) if i < len(self.shelf) else None))

    def kv(self, cr, x, y, w, label, value, color=None):
        self.text(cr, (x, y, w, 20), label, 9.5, False, self.c_sub)
        self.text(cr, (x, y, w, 20), value, 10.5, True, color, "r")

    def page_system(self, cr, r):
        x0, y, w, h = r
        x = x0 + 22
        cpu = self.cpu_hist[-1]
        col = GREEN if cpu < 60 else ORANGE if cpu < 85 else RED
        self.text(cr, (x, y + 16, 80, 16), "CPU", 9, False, self.c_sub)
        self.text(cr, (x, y + 30, 150, 40), f"{cpu:.0f}%", 24, True, col)
        gx, gy, gw, gh = x, y + 82, 170, 52
        rgba(cr, self.c_dim)
        cr.set_line_width(1)
        cr.move_to(gx, gy + gh)
        cr.line_to(gx + gw, gy + gh)
        cr.stroke()
        hist = list(self.cpu_hist)
        for i, v in enumerate(hist):
            px = gx + gw * i / (len(hist) - 1)
            py = gy + gh - gh * min(v, 100) / 100
            (cr.move_to if i == 0 else cr.line_to)(px, py)
        rgba(cr, col)
        cr.set_line_width(1.8)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()

        cx = x0 + 222
        cw = x0 + w - 22 - cx
        ram_col = ORANGE if self.ram > 85 else self.c_text
        self.kv(cr, cx, y + 14, cw, "RAM", f"{self.ram:.0f}%", ram_col)
        fill_rr(cr, cx, y + 38, cw, 6, 3, self.c_dim)
        fill_rr(cr, cx, y + 38, cw * min(self.ram, 100) / 100, 6, 3, ram_col)
        self.kv(cr, cx, y + 58, cw, "↓ Down", fmt_rate(self.net_rate[0]))
        self.kv(cr, cx, y + 82, cw, "↑ Up", fmt_rate(self.net_rate[1]))
        if self.temp is not None:
            self.kv(cr, cx, y + 106, cw, "Temp", f"{self.temp:.0f}°C",
                    RED if self.temp > 85 else ORANGE if self.temp > 70 else None)
        elif self.vpn:
            self.kv(cr, cx, y + 106, cw, "VPN", ", ".join(sorted(self.vpn)), GREEN)

    def page_focus(self, cr, r):
        x, y, w, h = r
        remaining = max(0.0, self.timer_end - time.time()) if self.timer_end else 0
        ring = (x + 26, y + 24, 70, 70)
        ccx, ccy = ring[0] + 35, ring[1] + 35
        cr.set_line_width(5)
        rgba(cr, self.c_dim)
        cr.arc(ccx, ccy, 33, 0, 2 * math.pi)
        cr.stroke()
        if self.timer_end:
            rgba(cr, self.timer_color)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.arc(ccx, ccy, 33, -math.pi / 2, -math.pi / 2 + (remaining / self.timer_total) * 2 * math.pi)
            cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_BUTT)
        self.text(cr, ring, fmt_time(remaining + 0.99) if self.timer_end else fmt_time(POMO["focus"]),
                  12.5, True, self.c_text if self.timer_end else self.c_sub, "c")
        label = self.timer_label or ("Timer" if self.timer_end else "Ready")
        sub = f"Session {self.pomo_done + 1}" if self.pomo_active else "Pomodoro 25 / 5"
        self.text(cr, (x + 108, y + 40, 96, 20), label, 12.5, True,
                  self.timer_color if self.timer_end else self.c_text)
        self.text(cr, (x + 108, y + 62, 96, 18), sub, 9, False, self.c_sub)

        btn, skip = (x + 22, y + 118, 92, 28), (x + 122, y + 118, 56, 28)
        fill_rr(cr, *btn, 14, self.c_btn)
        self.text(cr, btn, "Stop" if self.pomo_active else "Start", 10.5, True,
                  RED if self.pomo_active else GREEN, "c")
        self.add_hit(btn, lambda px, py: self.pomo_stop() if self.pomo_active else self.pomo_start())
        if self.pomo_active:
            fill_rr(cr, *skip, 14, self.c_btn)
            self.text(cr, skip, "Skip", 10.5, True, self.c_text, "c")
            self.add_hit(skip, lambda px, py: self.pomo_next())

        rgba(cr, self.c_dim)
        cr.set_line_width(1)
        cr.move_to(x + 206, y + 22)
        cr.line_to(x + 206, y + 150)
        cr.stroke()
        cx, cw = x + 222, w - 222 - 22
        self.text(cr, (cx, y + 16, cw, 14), "NEXT CTF", 8, True, self.c_sub)
        nxt = self.upcoming("ctf")
        if nxt:
            occ, ev = nxt
            self.text(cr, (cx, y + 30, cw, 20), str(ev.get("name", "")), 11.5, True)
            self.text(cr, (cx, y + 50, cw, 30), fmt_countdown((occ - datetime.now()).total_seconds()),
                      19, True, self.accent if self.music else GREEN)
            self.text(cr, (cx, y + 82, cw, 16), occ.strftime("%a %d %b · %H:%M"), 9, False, self.c_sub)
        else:
            self.text(cr, (cx, y + 34, cw, 20), "No CTF set", 11.5, True, self.c_sub)
            self.text(cr, (cx, y + 56, cw, 28), "island ctf NAME WHEN", 8.5, False, self.c_dim)
        self.text(cr, (cx, y + 108, cw, 14), "NEXT REMINDER", 8, True, self.c_sub)
        rem = self.upcoming("remind")
        if rem:
            occ, ev = rem
            when = occ.strftime("%H:%M") if occ.date() == datetime.now().date() else occ.strftime("%a %H:%M")
            self.text(cr, (cx, y + 123, cw - 50, 18), str(ev.get("name", "")), 10.5, True)
            self.text(cr, (cx + cw - 50, y + 123, 50, 18), when, 9.5, False, self.c_sub, "r")
        else:
            self.text(cr, (cx, y + 123, cw, 18), "None", 10, False, self.c_dim)

    def page_launch(self, cr, r):
        x, y, w, h = r
        items = [i for i in self.cfg.get("launchers", []) if isinstance(i, dict) and i.get("cmd")][:8]
        if not items:
            self.text(cr, (x, y + 60, w, 24), "No launchers in config.json", 10, False, self.c_sub, "c")
            return
        cw = (w - 40) / 4
        for i, item in enumerate(items):
            row, col = divmod(i, 4)
            rect = (x + 20 + col * cw, y + 18 + row * 70, cw, 64)
            ic = (rect[0] + rect[2] / 2 - 15, rect[1] + 4, 30, 30)
            pic = self.icon_surface(item.get("icon", ""))
            if pic is not None:
                cr.save()
                cr.translate(ic[0], ic[1])
                cr.scale(30.0 / pic.get_width(), 30.0 / pic.get_height())
                cr.set_source_surface(pic, 0, 0)
                cr.paint()
                cr.restore()
            else:
                hue = (sum(map(ord, str(item.get("name", "?")))) * 7 % 360) / 360.0
                rgba(cr, (*colorsys.hls_to_rgb(hue, 0.35, 0.6), 1.0))
                cr.arc(ic[0] + 15, ic[1] + 15, 15, 0, 2 * math.pi)
                cr.fill()
                self.text(cr, ic, (str(item.get("name", "?"))[:1] or "?").upper(), 12, True, WHITE, "c")
            self.text(cr, (rect[0], rect[1] + 38, rect[2], 16), str(item.get("name", "")), 9, False, self.c_sub, "c")
            self.add_hit(rect, lambda px, py, item=item: self.launch(item))

    def page_settings(self, cr, r):
        x, y, w, h = r
        lx, cx0 = x + 20, x + 76

        def caption(yy, s):
            self.text(cr, (lx, yy, 56, 20), s, 8, True, self.c_sub)

        caption(y + 12, "MODE")
        for i, (key, label) in enumerate(MODES):
            self.chip(cr, (cx0 + i * 74, y + 12, 70, 20), label, key == self.vis_mode, MODE_COLORS[key],
                      lambda px, py, key=key: self.set_mode(key))
        caption(y + 42, "THEME")
        names = self.theme_names()[:15]
        for i, name in enumerate(names):
            row, col = divmod(i, 4)
            self.chip(cr, (cx0 + col * 74, y + 42 + row * 24, 70, 20), self.theme_label(name),
                      name == self.theme_name, None,
                      lambda px, py, name=name: self.set_theme(name, toast=False))
        yy = y + 42 + ((len(names) + 3) // 4) * 24 + 4
        caption(yy, "TIMER")
        for i, (label, secs) in enumerate((("5 min", 300), ("15 min", 900), ("25 min", 1500))):
            self.chip(cr, (cx0 + i * 74, yy, 70, 20), label, False, None,
                      lambda px, py, secs=secs: self.start_timer(secs))
        self.chip(cr, (cx0 + 3 * 74, yy, 70, 20), "Stop pomo" if self.pomo_active else "Pomodoro",
                  self.pomo_active, ORANGE,
                  lambda px, py: self.pomo_stop() if self.pomo_active else self.pomo_start())
        caption(yy + 26, "OTHER")
        self.chip(cr, (cx0, yy + 26, 70, 20), "Clear shelf", False, None, lambda px, py: self.shelf_clear())
        self.chip(cr, (cx0 + 74, yy + 26, 70, 20), "Quit island", False, RED, lambda px, py: self.app.quit())


# ---------------------------------------------------------- tray (KDE / SNI) --
SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""


def tray_pixmap(color):
    """32x32 pill icon as ARGB32 bytes (network order), as the StatusNotifierItem spec wants."""
    size = 32
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    rrect(cr, 3, 9, 26, 14, 7)
    cr.set_source_rgba(0.08, 0.08, 0.08, 1)
    cr.fill_preserve()
    cr.set_source_rgba(0.93, 0.93, 0.93, 1)
    cr.set_line_width(2)
    cr.stroke()
    cr.set_source_rgba(*color)
    cr.arc(22, 16, 3, 0, 2 * math.pi)
    cr.fill()
    raw = bytes(surf.get_data())
    out = bytearray()
    for i in range(0, len(raw), 4):             # cairo: premultiplied BGRA -> spec: straight ARGB
        b, g, r, a = raw[i:i + 4]
        if a:
            r, g, b = min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
        out += bytes((a, r, g, b))
    return size, size, bytes(out)


class Tray:
    """Minimal StatusNotifierItem so KDE shows a tray icon (needed to bring the island back)."""
    PATH = "/StatusNotifierItem"
    IFACE = "org.kde.StatusNotifierItem"

    def __init__(self, island):
        self.island = island
        self.conn = None
        self.node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.owner = Gio.bus_own_name(Gio.BusType.SESSION, self.name, Gio.BusNameOwnerFlags.NONE,
                                      self.on_bus, self.on_name, None)

    def on_bus(self, conn, name):
        self.conn = conn
        conn.register_object(self.PATH, self.node.interfaces[0], self.on_call, self.on_get, None)

    def on_name(self, conn, name):
        conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                  "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  lambda *a: None)

    def on_call(self, conn, sender, path, iface, method, params, invocation):
        isl = self.island
        if method == "Activate":
            isl.set_mode("cycle")               # left click: next mode
        elif method == "SecondaryActivate":
            isl.set_theme("next")               # middle click: next theme
        elif method == "ContextMenu":
            isl.open_page("settings")           # right click: open Settings on the island
        invocation.return_value(None)

    def on_get(self, conn, sender, path, iface, prop):
        isl = self.island
        mode = isl.vis_mode
        simple = {"Category": ("s", "ApplicationStatus"), "Id": ("s", "dynamic-island"),
                  "Title": ("s", "Dynamic Island"), "Status": ("s", "Active"), "WindowId": ("i", 0),
                  "IconName": ("s", ""), "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
                  "ItemIsMenu": ("b", False), "Menu": ("o", "/NO_DBUSMENU")}
        if prop in simple:
            return GLib.Variant(*simple[prop])
        if prop == "IconPixmap":
            return GLib.Variant("a(iiay)", [tray_pixmap(MODE_COLORS[mode])])
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], "Dynamic Island",
                                                 f"Mode: {dict(MODES)[mode]} · Theme: {isl.theme_label()}\n"
                                                 "Left click: next mode · Middle: next theme · Right: settings"))
        return None

    def refresh(self):
        if self.conn:
            for sig in ("NewIcon", "NewToolTip"):
                self.conn.emit_signal(None, self.PATH, self.IFACE, sig, None)


# --------------------------------------------------------------- application --
class IslandApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.island = None
        self.tray = None

    def do_command_line(self, cmdline):
        args = list(cmdline.get_arguments())[1:]
        if self.island is None:
            if args:                            # a command, but no island is running
                cmdline.print_literal("Island is not running.\n")
                return 1
            self.island = Island(self)
            self.tray = Tray(self.island)
            if self.island.mode != "hidden":
                self.island.present()
            self.hold()                         # keep running even with no visible window
            return 0
        if args:
            self.island.command(args, cmdline.get_cwd())
        else:
            cmdline.print_literal("Island is already running.\n")
        return 0

    def do_shutdown(self):
        if self.island:
            self.island.cleanup()
        Gtk.Application.do_shutdown(self)


def main():
    sys.exit(IslandApp().run(sys.argv))


if __name__ == "__main__":
    main()
