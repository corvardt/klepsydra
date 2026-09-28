"""klepsydra: a small, always-on-display Claude usage widget for GNOME.

Local-first: reads Claude Code's JSONL logs. With --limits it additionally
fetches official subscription utilization from api.anthropic.com (opt-in).

Run:  python3 -m klepsydra            # local-only, zero network
      python3 -m klepsydra --limits   # + official limit percentages

Interaction:
  drag          move the card
  left-click    expand/collapse the detail panel
  middle-click  cycle theme
  Ctrl+click    mini mode (the 5h window as an hourglass)
  click droplet a bounce, without expanding the card
  right-click   window menu (always on top, ...)
  Ctrl+scroll   zoom
  Shift+scroll  cycle theme

All changes persist to ~/.config/klepsydra/config.ini.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import math
import random
import re
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk, Pango  # noqa: E402

try:  # absent on a build with no X11 backend; placement is then unavailable
    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11  # noqa: E402
except (ValueError, ImportError):  # pragma: no cover
    GdkX11 = None

from . import APP_ID, __version__  # noqa: E402
from . import collector as col  # noqa: E402
from . import limits as lim  # noqa: E402
from . import themes  # noqa: E402
from .config import Config  # noqa: E402

LIMITS_STALE_S = 600
LIMITS_BACKOFF_S = 300      # after a 429, stop asking for this long
LIMITS_BACKOFF_MAX_S = 1800  # ceiling for the doubling backoff
SCALE_STEP = 0.05
SCALE_MIN, SCALE_MAX = 0.6, 2.5
WATCH_DEBOUNCE_MS = 150   # coalesce the burst of writes Claude Code makes per turn
CONTEXT_MINUTES = 10      # a session idle this long stops drawing a context bar
EMPTY = "—"               # a DetailRow set to this hides itself instead
CREDIT_URL = "https://corvardt.com"
FIVE_HOURS = timedelta(hours=col.SESSION_HOURS)
WEEK = timedelta(days=7)
ALERT_LEVELS = (90, 70)   # percent; each notifies once per window


def _countdown(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = dt - datetime.now(timezone.utc)
    s = int(delta.total_seconds())
    if s <= 0:
        return "resetting…"
    if s < 60:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _claim(gesture) -> None:
    """Stop the event here, so Gtk.WindowHandle's titlebar action never runs."""
    gesture.set_state(Gtk.EventSequenceState.CLAIMED)


_x11_cache: tuple | None = None


def _x11():
    """(libX11, display) once, or None where the card cannot place itself.

    GTK4 dropped window positioning and Wayland forbids a client from placing
    its own window at all, so remembering where the card sits is only possible
    under X11 (XWayland counts). libX11 ships with every X-capable desktop, so
    this adds no package dependency; anywhere else it returns None and the
    widget simply lets the desktop choose, as it did before."""
    global _x11_cache
    if _x11_cache is None:
        _x11_cache = (False,)
        try:
            name = ctypes.util.find_library("X11")
            lib = ctypes.CDLL(name) if name else None
        except OSError:
            lib = None
        if lib is not None:
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            dpy = lib.XOpenDisplay(None)
            if dpy:
                lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_int, ctypes.c_int]
                lib.XDefaultRootWindow.restype = ctypes.c_ulong
                lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
                lib.XTranslateCoordinates.argtypes = [
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
                _x11_cache = (lib, dpy)
    return _x11_cache if len(_x11_cache) == 2 else None


def _level_class(pct: float) -> str:
    if pct >= 90:
        return "hot"
    if pct >= 70:
        return "warm"
    return "cool"


def render_css(scale: float, opacity: float, theme_name: str) -> str:
    """Render style.css: substitute the theme palette + opacity, then
    multiply every px value by the zoom factor."""
    t = themes.get(themes.resolve(theme_name))
    css = Path(__file__).with_name("style.css").read_text()
    for token, value in (("@BG@", t.bg), ("@FG@", t.fg), ("@BORDER@", t.border),
                         ("@COOL@", t.cool), ("@WARM@", t.warm), ("@HOT@", t.hot),
                         ("@OPACITY@", f"{opacity:.2f}")):
        css = css.replace(token, value)
    return re.sub(r"(\d+(?:\.\d+)?)px",
                  lambda m: f"{float(m.group(1)) * scale:.1f}px", css)


def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Bar(Gtk.DrawingArea):
    """Slim meter: a fill in the level colour, split into shaded segments
    when shares are given (models, biggest first, darkest), plus a tick at
    the elapsed share of the window."""

    SHADES = (1.0, 0.6, 0.38, 0.22)

    def __init__(self) -> None:
        super().__init__(hexpand=True)
        self.add_css_class("meter-bar")  # height comes from CSS, so it scales
        self.pct: float | None = None
        self.pace: float | None = None
        self.shares: list[float] = []
        self.set_draw_func(self._draw)

    def set(self, pct: float | None, pace: float | None = None,
            shares: list[float] | None = None) -> None:
        self.pct, self.pace, self.shares = pct, pace, shares or []
        self.queue_draw()

    def _draw(self, _area, cr, w, h) -> None:
        root = self.get_root()
        t, s = root.theme, root.cfg.scale
        fg = _rgb(t.fg)
        _rounded(cr, 0, 0, w, h, min(h / 2, 2 * s))
        cr.clip()
        cr.set_source_rgba(*fg, 0.12)
        cr.paint()

        if self.pct is not None:
            fill = w * min(max(self.pct, 0.0), 100.0) / 100
            rgb = _rgb(getattr(t, _level_class(self.pct)))
            x = 0.0
            shares = self.shares or [1.0]
            for i, f in enumerate(shares):
                seg = fill * f
                last = i == len(shares) - 1
                cr.set_source_rgba(*rgb, self.SHADES[min(i, 3)])
                cr.rectangle(x, 0, seg if last else max(seg - s, 0), h)
                cr.fill()
                x += seg

        if self.pace is not None:
            cr.set_source_rgba(*fg, 0.75)
            cr.rectangle(min(max(self.pace, 0.0), 1.0) * (w - s), 0, max(1.0, s), h)
            cr.fill()


class MeterRow(Gtk.Box):
    """label ..... value  +  slim progress bar underneath."""

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.label = Gtk.Label(label=label, xalign=0.0)
        self.label.add_css_class("meter-label")
        self.value = Gtk.Label(label="—", xalign=1.0, hexpand=True)
        self.value.add_css_class("meter-value")
        top.append(self.label)
        top.append(self.value)
        self.bar = Bar()
        self.append(top)
        self.append(self.bar)

    def set(self, pct: float | None, text: str, pace: float | None = None,
            shares: list[float] | None = None) -> None:
        """`pace`: elapsed share of the window; ahead of it means burning fast."""
        self.value.set_label(text)
        self.bar.set(pct, pace, shares)


class DetailRow(Gtk.Box):
    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=0.0)
        lbl.add_css_class("detail-label")
        self.value = Gtk.Label(label="—", xalign=1.0, hexpand=True)
        self.value.add_css_class("detail-value")
        # A long project or branch name would otherwise widen the whole card.
        # Middle, not end: the trailing dollar figure is the point of the row.
        self.value.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.value.set_max_width_chars(34)
        self.append(lbl)
        self.append(self.value)

    def set(self, text: str) -> None:
        """An empty row is hidden rather than shown as '—'. On a light day most
        of the panel is unpopulated, and a wall of dashes reads as broken."""
        self.value.set_label(text)
        self.set_visible(bool(text) and text != EMPTY)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Hourglass(Gtk.DrawingArea):
    """The 5h window as a 10x10 grid, one cell per percent. Used cells vanish
    in reading order; the next one shakes as it gets close and pops when it
    goes. A spent window shows a padlock across the empty grid."""

    CELL, GAP = 6, 2        # px before scale
    SHAKE_FROM = 0.7        # fraction of the current percent where shaking starts
    POP_S = 0.45
    MAX_POPS = 5            # a burst of several percent pops only the last few
    LOCK = ("..........", "...####...", "..#....#..", "..#....#..", ".########.",
            ".###..###.", ".###..###.", ".########.", ".########.", "..........")

    def __init__(self) -> None:
        super().__init__()
        self.theme = themes.get(themes.DEFAULT)
        self.scale = 1.0
        self.pct: float | None = None
        self.gone: int | None = None     # None until the first update
        self.bits: list[tuple] = []      # (x, y, vx, vy, t0, rgb)
        self._ticking = False
        self.set_draw_func(self._draw)

    def configure(self, theme: themes.Theme, scale: float) -> None:
        self.theme, self.scale = theme, scale
        cs, gap = self.CELL * scale, self.GAP * scale
        step = cs + gap
        # margin of one cell all round so pops are not clipped
        self.set_content_width(int(10 * step - gap + 2 * cs))
        self.set_content_height(int(10 * step - gap + 2 * cs))
        self.queue_draw()

    @staticmethod
    def _motion() -> bool:
        return bool(Gtk.Settings.get_default().get_property("gtk-enable-animations"))

    def update(self, pct: float | None) -> None:
        gone = min(int(pct), 100) if pct is not None else 0
        if self.gone is not None and gone > self.gone and self._motion():
            for i in range(max(self.gone, gone - self.MAX_POPS), gone):
                if i < 100:
                    self._pop(i)
        self.gone, self.pct = gone, pct
        self._animate()
        self.queue_draw()

    def _shake(self) -> float:
        """0..1: how hard the next cell shakes."""
        if self.pct is None or self.gone is None or self.gone >= 100 or not self._motion():
            return 0.0
        frac = self.pct - int(self.pct)
        return max(0.0, (frac - self.SHAKE_FROM) / (1 - self.SHAKE_FROM))

    def _animate(self) -> None:
        if (self._shake() or self.bits) and not self._ticking:
            self._ticking = True
            self.add_tick_callback(self._tick)

    def _tick(self, *_args) -> bool:
        now = time.monotonic()
        self.bits = [b for b in self.bits if now - b[4] < self.POP_S]
        self.queue_draw()
        if self._shake() or self.bits:
            return True
        self._ticking = False
        return False

    def _cell(self, i: int) -> tuple[float, float]:
        step = (self.CELL + self.GAP) * self.scale
        pad = self.CELL * self.scale
        return pad + (i % 10) * step, pad + (i // 10) * step

    def _pop(self, i: int) -> None:
        x, y = self._cell(i)
        half = self.CELL * self.scale / 2
        rgb = _rgb(getattr(self.theme, _level_class(i)))
        t0 = time.monotonic()
        for k in range(8):
            a = k * math.pi / 4 + random.uniform(-0.3, 0.3)
            v = random.uniform(20, 45) * self.scale
            self.bits.append((x + half, y + half, math.cos(a) * v,
                              math.sin(a) * v, t0, rgb))

    def _draw(self, _area, cr, _w, _h) -> None:
        t, s = self.theme, self.scale
        cs, gap = self.CELL * s, self.GAP * s
        step = cs + gap
        fg = _rgb(t.fg)
        gone = self.gone or 0
        level = _rgb(getattr(t, _level_class(self.pct or 0.0)))
        now = time.monotonic()
        shake = self._shake()

        for i in range(100):
            x, y = self._cell(i)
            if gone >= 100 and self.LOCK[i // 10][i % 10] == "#":
                cr.set_source_rgba(*_rgb(t.hot), 1.0)
            elif i < gone:
                cr.set_source_rgba(*fg, 0.07)
            else:
                cr.set_source_rgba(*level, 1.0 if self.pct is not None else 0.25)
                if i == gone and shake:
                    x += shake * 1.5 * s * math.sin(now * 47)
                    y += shake * 1.5 * s * math.sin(now * 61 + 1)
            cr.rectangle(x, y, cs, cs)
            cr.fill()

        for x, y, vx, vy, t0, rgb in self.bits:
            dt = now - t0
            p = dt / self.POP_S
            size = cs * 0.45 * (1 - p)
            cr.set_source_rgba(*rgb, 1 - p)
            cr.rectangle(x + vx * dt - size / 2, y + vy * dt + 30 * s * dt * dt - size / 2,
                         size, size)
            cr.fill()


class Heatmap(Gtk.DrawingArea):
    """Last 7 days by hour: one row per day, today at the bottom, each cell
    shaded by its share of the busiest hour. Hover a cell for its figure."""

    CELL, GAP = 7, 2
    DAYS = 7

    def __init__(self) -> None:
        super().__init__(halign=Gtk.Align.START)
        self.grid = [[0.0] * 24 for _ in range(self.DAYS)]
        self.now = datetime.now().astimezone()
        self.scale = 1.0
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def configure(self, scale: float) -> None:
        self.scale = scale
        step = (self.CELL + self.GAP) * scale
        self.set_content_width(int(24 * step - self.GAP * scale))
        self.set_content_height(int(self.DAYS * step - self.GAP * scale))
        self.queue_draw()

    def update(self, grid: list[list[float]], now: datetime) -> None:
        self.grid, self.now = grid, now.astimezone()
        self.queue_draw()

    def _draw(self, _area, cr, _w, _h) -> None:
        t = self.get_root().theme
        s = self.scale
        cs, step = self.CELL * s, (self.CELL + self.GAP) * s
        fg, rgb = _rgb(t.fg), _rgb(t.cool)
        peak = max(map(max, self.grid)) or 1.0
        for d, row in enumerate(self.grid):
            for h, v in enumerate(row):
                if d == self.DAYS - 1 and h > self.now.hour:
                    continue  # hours that have not happened yet
                if v > 0:  # sqrt so a light hour still shows beside a heavy one
                    cr.set_source_rgba(*rgb, 0.2 + 0.8 * math.sqrt(v / peak))
                else:
                    cr.set_source_rgba(*fg, 0.06)
                cr.rectangle(h * step, d * step, cs, cs)
                cr.fill()

    def _tooltip(self, _w, x, y, _keyboard, tooltip) -> bool:
        step = (self.CELL + self.GAP) * self.scale
        h, d = int(x // step), int(y // step)
        if not (0 <= h < 24 and 0 <= d < self.DAYS):
            return False
        day = self.now - timedelta(days=self.DAYS - 1 - d)
        tooltip.set_text(f"{day:%a} {h:02d}:00  ·  ${self.grid[d][h]:.2f}")
        return True


class Droplet(Gtk.DrawingArea):
    """A water drop at a laptop (side on, its face lit by the screen) while a
    Claude Code turn runs, and bored in a rocking chair when none does.
    Switching, it flings the laptop away and a chair puffs into being under
    it; back to work, the chair vanishes and a laptop drops from the sky.

    At the laptop it acts out what the logs say is under way: a hand on its
    chin and a thought bubble while the model thinks, typing while it writes,
    a slam of Enter on each file edit, watching a `>_` while a shell runs,
    eyes scanning while files are read, shading its eyes under a globe for
    the web, drumming its fingers beside a ghostly helper for a subagent,
    hands on hips and a "?" when Claude waits on you, arms crossed under a
    cloud on an API error. An interruption knocks it back into a shrug; a
    queued message makes it glance up. A long spell of work brings the odd
    break (a head scratch, a knuckle crack, a stretch). It types frantically past 70% and
    sweats past 90%, dozes after a quiet spell, and freezes in ice at 100%.
    Its body takes the 5h level colour. Click it for a bounce; its eyes
    follow the pointer over the card; it jumps when a limit refusal is
    logged."""

    SHAPE = ("...#...", "..###..", ".#####.", "#######",
             "#######", "#######", ".#####.", "..###..")
    EYE_ROW, EYES = 4, (2, 4)
    SEAT = 5                # the rocking chair's seat height, in sprite pixels
    # the laptop, side on, in sprite pixels: deck length and thickness, lid
    # length and thickness, and how far the lid leans back
    DECK_L, DECK_T, LID_L, LID_T, LEAN = 8.5, 1.2, 5.5, 1.1, 0.28
    PX = 2.0                # sprite pixel, px before scale
    HOP = 4                 # hop height, in sprite pixels
    AIR = 0.75              # share of a hop spent off the ground
    SWAP_S = 1.2            # length of the laptop <-> chair switch
    ICE = ("#9ed0fc", "#3a88c2")  # on dark, on light themes
    SLEEP_S = 5 * 60        # no Claude Code activity for this long: doze
    ZED = ("###", ".#.", "###")
    KEY_S = 0.35            # life of a key spark
    BREAKS = {"scratch": 1.6, "crack": 1.2, "stretch": 1.4}  # seconds each
    SLAM_S = 0.35           # Enter, slammed on a file edit
    WAIT_S = 6.0            # thinking or writing this long: take a break now and then
    MOMENT_S = 1.2          # length of a one-off reaction (a shrug, a glance)
    # what its hands do in each live state (see Collector.live)
    ACTS = {"thinking": "think", "writing": "type", "editing": "type",
            "shell": "rest", "reading": "rest", "web": "shade",
            "subagent": "drum", "asking": "hips", "error": "cross"}
    QUESTION = (".##", "..#", ".#.", "...", ".#.")
    PROMPT = ("#..", ".#.", "#..")  # the ">" of a shell prompt
    FRAME_MS = 60           # ponytail: fixed ~16 fps timer; a frame clock if it ever looks choppy

    def __init__(self) -> None:
        super().__init__(hexpand=True)
        self.scale = 1.0
        self.pct: float | None = None
        self.state = "idle"           # from Collector.live
        self.state_since = 0.0
        self.moment: tuple[str, float] | None = None  # a one-off reaction
        self.frozen = False
        self.idle_s = 0.0
        self.mode: str | None = None  # "work" or "rest"; set on the first frame
        self.changed = float("-inf")  # when the last switch began
        self.phase = 0.0
        self.happy_until = 0.0
        self.poked_until = 0.0
        self.startled_until = 0.0
        self.look_at: float | None = None  # pointer x in this widget, if on the card
        self.keys: list[tuple[float, float, float]] = []  # (x, vx, t0)
        self.pause: tuple[str, float] | None = None  # a typing break: (kind, start)
        self.next_pause = 0.0
        self.slam_at = float("-inf")
        self.last = 0.0
        self._timer = 0
        self.set_draw_func(self._draw)
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())
        poke = Gtk.GestureClick(button=1)
        poke.connect("pressed", self._on_poke)
        self.add_controller(poke)

    def configure(self, scale: float) -> None:
        self.scale = scale
        p = self.PX * scale
        self.set_content_width(int(16 * p))
        self.set_content_height(int(p * (len(self.SHAPE) + self.SEAT + self.HOP) + 2 * scale))
        self.queue_draw()

    def _on_poke(self, gesture, *_args) -> None:
        """Claimed, so a poke neither drags the card nor expands it."""
        _claim(gesture)
        self.poked_until = time.monotonic() + 0.8

    def startle(self) -> None:
        self.startled_until = time.monotonic() + 1.2

    def react(self, kind: str) -> None:
        """A one-off from the logs: "interrupted", "queued", or "edit"."""
        now = time.monotonic()
        if kind == "edit":
            self.slam_at = now
        else:
            self.moment = (kind, now)
            if kind == "queued":
                self.poked_until = now + 0.5

    def update(self, pct: float | None, state: str, idle_s: float) -> None:
        frozen = pct is not None and pct >= 100
        now = time.monotonic()
        if self.frozen and not frozen:
            self.happy_until = now + 1.6
        if state != self.state:
            if state == "error":
                self.startle()
            self.state, self.state_since = state, now
        self.pct, self.frozen, self.idle_s = pct, frozen, idle_s
        if not self._timer:  # no animation: just be in the right place
            self.mode, self.changed = ("rest" if state == "idle" else "work"), float("-inf")
            self.queue_draw()

    def _start(self) -> None:
        if Hourglass._motion() and not self._timer:
            self.last = time.monotonic()
            self._timer = GLib.timeout_add(self.FRAME_MS, self._step)

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _gait(self, now: float) -> tuple[float, float] | None:
        """(seconds per hop, hop height factor) for a hop in place, or None."""
        if self.frozen or not self._timer:
            return None
        if now < self.startled_until:
            return 0.3, 1.5
        if now < self.poked_until:
            return 0.25, 1.4
        if now < self.happy_until:
            return 0.4, 1.8
        return None

    def _typing(self, now: float) -> bool:
        return (self.mode == "work" and not self.frozen
                and now - self.changed >= self.SWAP_S)

    def _step(self) -> bool:
        now = time.monotonic()
        dt, self.last = now - self.last, now
        p = self.PX * self.scale
        target = "rest" if self.state == "idle" else "work"
        if self.mode is None:
            self.mode = target
        elif target != self.mode and not self.frozen:
            self.mode, self.changed = target, now
        if self._gait(now):
            self.phase = (self.phase + dt / self._gait(now)[0]) % 1.0
        else:
            self.phase = 0.0
        pct = self.pct or 0.0
        if not self._typing(now) or self.state not in ("thinking", "writing"):
            self.pause = None
        elif self.pause:
            if now - self.pause[1] > self.BREAKS[self.pause[0]]:
                self.pause = None
                self.next_pause = now + random.uniform(8, 14)
        elif now - self.state_since > self.WAIT_S and now >= self.next_pause and pct < 90:
            self.pause = (random.choice(list(self.BREAKS)), now)
        if self._typing(now) and self.state in ("writing", "editing"):
            if random.random() < (12 if pct >= 70 else 6) * dt:  # sparks a second
                x = self._left(self.get_width())
                self.keys.append((x - 10 * p + random.uniform(3, 8) * p,
                                  random.uniform(-4, 4) * p, now))
        self.keys = [k for k in self.keys if now - k[2] < self.KEY_S]
        self.queue_draw()
        return True

    def _left(self, w: float) -> float:
        """The droplet's left edge, with the laptop to its left: the pair sits
        in the middle of the strip."""
        return w / 2 + 1.5 * self.PX * self.scale

    def _chair(self, cr, ox: float, ground: float, p: float, rgb) -> None:
        """A rocking chair side on, after a spindle-back silhouette: a back
        leaning away with an arched top rail and spindles, an arm with its
        own spindles, splayed legs, and long rockers turned up at both ends.
        Units are sprite pixels from the seat's back corner, up from the
        ground."""
        def at(u: float, v: float) -> tuple[float, float]:
            return ox + u * p, ground - v * p

        def line(width: float, *pts: tuple[float, float]) -> None:
            cr.set_line_width(width * p)
            cr.move_to(*at(*pts[0]))
            for pt in pts[1:]:
                cr.line_to(*at(*pt))
            cr.stroke()

        cr.save()
        cr.set_source_rgba(*rgb, 1.0)
        cr.set_line_cap(1)   # cairo.LINE_CAP_ROUND
        cr.set_line_join(1)  # cairo.LINE_JOIN_ROUND
        cr.set_line_width(0.8 * p)
        cr.move_to(*at(-2.5, 1.6))  # rockers
        cr.curve_to(*at(-1, 0), *at(8, -0.3), *at(11, 1.4))
        cr.stroke()
        line(0.7, (0.3, 0.6), (1.2, 5))    # back leg
        line(0.7, (8.6, 0.7), (7.8, 5))    # front leg
        line(1.0, (0.6, 5), (8.6, 5))      # seat
        line(0.7, (0.8, 5), (-1.8, 13))    # back posts, leaning away
        line(0.7, (2.2, 5), (-0.2, 13.2))
        for k in (1 / 3, 2 / 3):           # back spindles
            line(0.45, (0.8 + 1.4 * k, 5), (-1.8 + 1.6 * k, 13 + 0.2 * k))
        cr.set_line_width(0.9 * p)         # arched top rail
        cr.move_to(*at(-2.4, 12.8))
        cr.curve_to(*at(-1.6, 14), *at(0.2, 14), *at(0.7, 13.2))
        cr.stroke()
        line(0.7, (1.1, 8.6), (9.3, 8.4))  # arm, and its front post
        line(0.7, (8.4, 8.4), (8.1, 5))
        for u in (4.9, 6.5):               # arm spindles
            line(0.45, (u, 5), (u, 8.5))
        cr.restore()

    def _laptop(self, cr, left: float, bottom: float, p: float, fg, bg,
                glow, lit: float, alpha: float = 1.0, angle: float = 0.0) -> None:
        """The laptop side on: a deck on the ground and a lid leaning back
        from the hinge, its screen face lit by `lit`. `angle` spins the whole
        thing about its middle, for the fling."""
        cx, cy = left + self.DECK_L * p / 2, bottom - self.LID_L * p / 2
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(angle)
        cr.translate(-cx, -cy)
        cr.set_source_rgba(*(b + (f - b) * 0.4 for f, b in zip(fg, bg)), alpha)
        _rounded(cr, left, bottom - self.DECK_T * p, self.DECK_L * p, self.DECK_T * p, 0.5 * p)
        cr.fill()
        cr.translate(left + 0.8 * p, bottom - self.DECK_T * p)  # the hinge
        cr.rotate(-self.LEAN)
        cr.set_source_rgba(*(b + (f - b) * 0.5 for f, b in zip(fg, bg)), alpha)
        _rounded(cr, 0, -self.LID_L * p, self.LID_T * p, self.LID_L * p, 0.45 * p)
        cr.fill()
        if lit:  # the screen, facing the typist
            cr.set_source_rgba(*glow, lit * alpha)
            cr.rectangle(self.LID_T * p, -self.LID_L * p + 0.6 * p, 0.5 * p,
                         self.LID_L * p - 1.2 * p)
            cr.fill()
        cr.restore()

    def _draw(self, _area, cr, w, h) -> None:
        t, s = self.get_root().theme, self.scale
        p = self.PX * s
        now = time.monotonic()
        ground = h - s
        body = _rgb(self.ICE[0 if t.dark else 1] if self.frozen
                    else getattr(t, _level_class(self.pct or 0.0)))
        eye, fg = _rgb(t.bg), _rgb(t.fg)
        glow = _rgb(self.ICE[0 if t.dark else 1])  # screen light, pale blue
        x = self._left(w)
        mode = self.mode or ("rest" if self.state == "idle" else "work")
        tt = now - self.changed  # time into the current switch
        settled = tt >= self.SWAP_S or not self._timer
        typing = self._typing(now) and self._timer
        frantic = (self.pct or 0.0) >= 70
        seat = self.SEAT * p
        lap = x - 10 * p  # the laptop's left end

        # where the droplet sits, what props are out, and when it lands
        base, chair_k, lid_dy, landed = ground, 0.0, None, None
        puff_at = None
        if mode == "rest":
            if settled:
                base, chair_k = ground - seat, 1.0
            else:
                v = min(max((tt - 0.5) / 0.45, 0.0), 1.0)  # hop up into the chair
                base = ground - seat * v - 5 * p * math.sin(math.pi * v)
                chair_k = min(max((tt - 0.7) / 0.12, 0.0), 1.0)
                puff_at, landed = 0.7, tt - 0.95
                if tt < 0.6:  # the laptop, flung away spinning
                    u = tt / 0.6
                    self._laptop(cr, lap - 30 * p * u,
                                 ground - 14 * p * math.sin(math.pi * u * 0.8),
                                 p, fg, eye, glow, 0.0, 1 - u, -3 * math.pi * u)
        else:
            if settled:
                lid_dy = 0.0
            else:
                v = min(tt / 0.45, 1.0)  # hop down out of the chair
                base = ground - seat * (1 - v) - 4 * p * math.sin(math.pi * v)
                chair_k = 1.0 if tt < 0.2 else 0.0
                puff_at, landed = 0.2, tt - 0.45
                if tt >= 0.5:  # a laptop drops from the sky, and bounces once
                    u = min((tt - 0.5) / 0.35, 1.0)
                    b = tt - 0.85
                    lid_dy = (-16 * p * (1 - u) ** 2 if u < 1
                              else -1.2 * p * math.sin(math.pi * b / 0.25) if b < 0.25 else 0.0)

        asleep_now = self.idle_s > self.SLEEP_S
        rock = 0.0  # the chair and whoever is in it tilt about the rocker
        if mode == "rest" and settled and self._timer and not self.frozen:
            rock = (0.025 * math.sin(now * 0.9) if asleep_now
                    else 0.06 * math.sin(now * 1.6))
        cr.save()
        if rock:
            cr.translate(x + 3.5 * p, ground)
            cr.rotate(rock)
            cr.translate(-(x + 3.5 * p), -ground)

        if chair_k:  # pops in from its feet up
            cx = x + 3.5 * p
            cr.save()
            cr.translate(cx, ground)
            cr.scale(chair_k, chair_k)
            cr.translate(-cx, -ground)
            self._chair(cr, x - 1.2 * p, ground, p,
                        tuple(b + (f - b) * 0.4 for f, b in zip(fg, eye)))
            cr.restore()

        gait = self._gait(now)
        lift, sx, sy = 0.0, 1.0, 1.0
        if gait:
            if self.phase < self.AIR:
                lift = self.HOP * p * gait[1] * math.sin(math.pi * self.phase / self.AIR)
                sx, sy = 0.94, 1.08
            else:  # squash on landing
                q = math.sin(math.pi * (self.phase - self.AIR) / (1 - self.AIR))
                sx, sy = 1 + 0.2 * q, 1 - 0.25 * q
        asleep = (mode == "rest" and settled and not gait and not self.frozen
                  and self._timer and asleep_now)
        bored = (mode == "rest" and settled and not gait and not self.frozen
                 and self._timer and not asleep)
        pause = self.pause[0] if typing and self.pause else None
        slamming = typing and self.slam_at <= now < self.slam_at + self.SLAM_S
        act = pause or ("slam" if slamming else self.ACTS.get(self.state, "type"))
        mom = (self.moment[0] if self.moment and self._timer
               and now - self.moment[1] < self.MOMENT_S else None)
        into = (now - self.pause[1]) / self.BREAKS[pause] if pause else 0.0
        speed = 22 if frantic else 14
        if landed is not None and 0 <= landed < 0.15:  # squash as a switch lands
            q = math.sin(math.pi * landed / 0.15)
            sx, sy = 1 + 0.2 * q, 1 - 0.25 * q
        elif typing and not gait:
            if pause == "stretch":
                q = math.sin(math.pi * min(into, 1.0))
                sx, sy = 1 - 0.08 * q, 1 + 0.12 * q
            elif act == "type":  # a small bob with each keystroke
                q = abs(math.sin(now * speed))
                sx, sy = 1 + 0.02 * q, 1 - 0.03 * q
            elif act == "hips":  # tapping a foot
                q = max(0.0, math.sin(now * 9))
                sx, sy = 1 + 0.02 * q, 1 - 0.04 * q
        elif asleep:  # slumped in the chair
            b = 0.03 * math.sin(now * 1.4)
            sx, sy = 1.06 - b, 0.88 + b
        elif self._timer and settled and not gait and not self.frozen:  # breathing
            b = 0.04 * math.sin(now * 2.2)
            sx, sy = 1 - b, 1 + b
        sweating = typing and (self.pct or 0.0) >= 90
        pw, ph = p * sx, p * sy
        x0 = x + (7 * p - 7 * pw) / 2
        if sweating:
            x0 += 0.5 * p * math.sin(now * 40)
        if typing and self.state == "shell":  # leaning in to watch it run
            x0 -= 0.6 * p
        if mom == "interrupted":  # knocked back a step
            x0 += 1.5 * p * (1 - (now - self.moment[1]) / self.MOMENT_S)
        y0 = base - lift - len(self.SHAPE) * ph
        if self.frozen:
            y0 -= p  # room for the ice below the body
            cr.set_source_rgba(*fg, 0.16)
            _rounded(cr, x0 - p, y0 - p, 9 * p, 10 * p, p)
            cr.fill_preserve()
            cr.set_source_rgba(*fg, 0.45)
            cr.set_line_width(max(1.0, s * 0.8))
            cr.stroke()

        happy = now < max(self.poked_until, self.happy_until)
        tossing = mode == "rest" and not settled and tt < 0.6
        if self.look_at is not None and settled and not asleep and not self.frozen:
            d = self.look_at - (x0 + 3.5 * pw)
            look = 0 if abs(d) < 2 * p else (1 if d > 0 else -1)
        else:
            # at the screen, after the flung laptop, or, bored, off to one side
            look = -1 if tossing or typing else (1 if bored and now % 12 < 7 else 0)
            if typing and not pause:
                look = {"reading": -1 if now * 2.5 % 2 < 1 else 0,
                        "web": (-1, 0, 1)[int(now * 1.5) % 3],
                        "subagent": 1, "asking": 0, "error": 1,
                        "thinking": 0}.get(self.state, look)
            if mom == "queued":
                look = 0
        # a lid low for shut or sleepy eyes, high for a happy squint
        closed = (self.frozen or asleep or pause == "stretch"
                  or (self._timer and now % 4.0 < 0.15))
        lit = 0.3 + 0.12 * math.sin(now * 9) if typing else 0.0
        for r, row in enumerate(self.SHAPE):
            for c, ch in enumerate(row):
                if ch != "#":
                    continue
                cr.set_source_rgba(*body, 1.0)
                cr.rectangle(x0 + c * pw, y0 + r * ph, pw + 0.3, ph + 0.3)
                cr.fill()
                if lit and r <= self.EYE_ROW and c <= 3:  # screen light, on the near side
                    cr.set_source_rgba(*glow, lit)
                    cr.rectangle(x0 + c * pw, y0 + r * ph, pw + 0.3, ph + 0.3)
                    cr.fill()
                if r == self.EYE_ROW and c - look in self.EYES:
                    cr.set_source_rgba(*eye, 1.0)
                    if happy:
                        cr.rectangle(x0 + c * pw, y0 + r * ph, pw, ph * 0.4)
                    elif bored and not closed:  # heavy lids
                        cr.rectangle(x0 + c * pw, y0 + r * ph + ph * 0.45, pw, ph * 0.55)
                    else:
                        top = y0 + r * ph + (ph * 0.6 if closed else 0)
                        cr.rectangle(x0 + c * pw, top, pw, ph * (0.4 if closed else 1))
                    cr.fill()

        cr.restore()  # end of the rock

        if lid_dy is not None:
            flicker = 20 if self.state == "reading" else 9
            self._laptop(cr, lap, ground + lid_dy, p, fg, eye, glow,
                         0.6 + 0.25 * math.sin(now * flicker) if typing else 0.25)
        if typing and not pause:
            self._props(cr, act, body, fg, glow, lap, ground, x0, y0, pw, p, now)

        deck = ground - self.DECK_T * p  # the keyboard's top
        if typing and not gait:
            self._hands(cr, act, into, speed, body, fg, lap, deck, x0, y0, pw, p, now)
        elif tossing and tt < 0.3:  # hands thrown up after the fling, or a shrug
            self._hands(cr, "shrug" if mom == "interrupted" else "stretch", 0.5,
                        speed, body, fg, lap, deck, x0, y0, pw, p, now)

        for kx, vx, t0 in self.keys:  # sparks flying off the keyboard
            q = (now - t0) / self.KEY_S
            cr.set_source_rgba(*fg, 0.8 * (1 - q))
            cr.rectangle(kx + vx * q, deck - 5 * p * q + 3 * p * q * q,
                         p * 0.7, p * 0.7)
            cr.fill()

        if puff_at is not None and 0 <= tt - puff_at < 0.35:  # poof
            q = (tt - puff_at) / 0.35
            cx, cy = x + 3.5 * p, ground - 2 * p
            size = 1.4 * p * (1 - q)
            cr.set_source_rgba(*fg, 0.6 * (1 - q))
            for k in range(8):
                a = k * math.pi / 4
                r = (2 + 5 * q) * p
                cr.rectangle(cx + math.cos(a) * r - size / 2,
                             cy + math.sin(a) * r * 0.6 - size / 2, size, size)
            cr.fill()

        if asleep:  # two z's drifting up, out of step
            for k in (0.0, 0.5):
                q = (now / 2.5 + k) % 1.0
                zx, zy = x0 + 7 * pw + q * 2 * p, y0 + 2 * p - q * 3 * p
                cr.set_source_rgba(*fg, 0.7 * (1 - q))
                for r, row in enumerate(self.ZED):
                    for c, ch in enumerate(row):
                        if ch == "#":
                            cr.rectangle(zx + c * p, zy + r * p, p, p)
                cr.fill()

        if now < self.startled_until:  # "!" beside the head
            cr.set_source_rgba(*_rgb(t.hot), 1.0)
            ex = x0 + 7 * pw + p
            cr.rectangle(ex, y0, p, 3 * p)
            cr.rectangle(ex, y0 + 4 * p, p, p)
            cr.fill()

        if sweating:  # a bead flicking off the brow
            q = (now * 1.5) % 1.0
            cr.set_source_rgba(*fg, 0.6 * (1 - q))
            cr.rectangle(x0 + 7 * pw + p * (0.5 + 2 * q), y0 + 2 * ph + 2 * p * q, p, p)
            cr.fill()

    def _props(self, cr, act, body, fg, glow, lap, ground, x0, y0, pw, p, now) -> None:
        """What floats around it for the state at hand: a thought bubble, a
        shell prompt over the screen, a globe, a ghostly helper, a "?", a
        cloud."""
        def glyph(rows, gx, gy, size):
            for r, row in enumerate(rows):
                for c, ch in enumerate(row):
                    if ch == "#":
                        cr.rectangle(gx + c * size, gy + r * size, size, size)
            cr.fill()

        if act == "think":  # three dots rising, one after another
            cr.set_source_rgba(*fg, 0.6)
            for k, (dx, dy, r) in enumerate(((0.3, -1.2, 0.4), (-0.9, -2.6, 0.55),
                                             (-2.4, -4.0, 0.7))):
                if now % 1.6 > k * 0.4:
                    cr.arc(x0 + dx * p, y0 + dy * p, r * p, 0, 2 * math.pi)
                    cr.fill()
        elif self.state == "shell":  # ">_" over the screen, the cursor blinking
            cr.set_source_rgba(*glow, 0.9)
            gx, gy = lap + 1.2 * p, ground - 10 * p
            glyph(self.PROMPT, gx, gy, 0.7 * p)
            if now % 1.0 < 0.5:
                cr.rectangle(gx + 2.8 * p, gy + 1.4 * p, 1.4 * p, 0.7 * p)
                cr.fill()
        elif act == "shade":  # a globe, turning
            gx, gy, r = x0 + 8.5 * pw, y0 + 0.5 * p, 1.6 * p
            cr.set_source_rgba(*glow, 0.9)
            cr.set_line_width(0.4 * p)
            cr.arc(gx, gy, r, 0, 2 * math.pi)
            cr.stroke()
            cr.save()
            cr.translate(gx, gy)
            cr.scale(max(abs(math.cos(now * 2)), 0.05), 1.0)
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
            cr.move_to(gx - r, gy)
            cr.line_to(gx + r, gy)
            cr.stroke()
        elif act == "drum":  # a small, ghostly helper, typing away
            g = 0.55 * p
            hx = x0 + 8.5 * pw
            hy = ground - len(self.SHAPE) * g - g * abs(math.sin(now * 14)) * 0.4
            cr.set_source_rgba(*body, 0.45)
            glyph(self.SHAPE, hx, hy, g)
        elif act == "hips":  # a "?" over its head
            cr.set_source_rgba(*fg, 0.85)
            glyph(self.QUESTION, x0 + 7 * pw + 0.5 * p, y0 - 2.5 * p, p)
        elif act == "cross":  # a small dark cloud
            cr.set_source_rgba(*fg, 0.35)
            for dx, dy, r in ((2.4, -2.2, 1.1), (3.8, -2.8, 1.4), (5.2, -2.2, 1.1)):
                cr.arc(x0 + dx * p, y0 + dy * p, r * p, 0, 2 * math.pi)
                cr.fill()

    def _hands(self, cr, act, into, speed, body, fg, lap, deck,
               x0, y0, pw, p, now) -> None:
        """Two big floating cartoon hands, no arms, in a lighter tint of the
        body; the far one is drawn first and dimmer. `act` is "type" (pounding
        the keys in turn), "slam" (Enter, hard), "rest" (still on the keys),
        "drum" (drumming fingers), "think" (one at the chin), "shade" (one
        shading the eyes), "hips", "cross", "shrug", or a break: a hand to the
        head, both together for a knuckle crack, or up for a stretch."""
        pause = act
        hs = 2 * p
        keys = deck - hs  # resting on the keyboard
        if pause == "scratch":
            hands = [(x0 + 1.2 * pw, y0 - 1.4 * p + 0.5 * p * math.sin(now * 28)),
                     (lap + 6.2 * p, keys)]
        elif pause == "crack":
            j = 0.4 * p * math.sin(now * 30)
            bx, by = x0 - 3.2 * p, y0 + 3.5 * p
            hands = [(bx + 1.6 * p, by - j), (bx, by + j)]
            if (now - self.pause[1]) % 0.35 < 0.08:  # the crack
                cr.set_source_rgba(*fg, 0.8)
                cr.rectangle(bx - 0.9 * p, by - 1.0 * p, 0.7 * p, 0.7 * p)
                cr.rectangle(bx + hs + 1.8 * p, by - 1.0 * p, 0.7 * p, 0.7 * p)
                cr.fill()
        elif pause == "stretch":
            up = 2 * p * math.sin(math.pi * min(into, 1.0))
            hands = [(x0 + 6.2 * pw, y0 - hs - up), (x0 - 0.8 * pw, y0 - hs - up)]
        elif act == "slam":  # the near hand winds up and comes down on Enter
            q = (now - self.slam_at) / self.SLAM_S
            up = 3 * p * (1 - q / 0.6) if q < 0.6 else 0.0
            hands = [(lap + 3.8 * p, keys), (lap + 6.2 * p, keys - up)]
            if 0.6 <= q < 0.85:  # the thump
                cr.set_source_rgba(*fg, 0.8)
                for dx in (-1.2, 2.4):
                    cr.rectangle(lap + (6.2 + dx) * p, keys - 0.6 * p, 0.8 * p, 0.8 * p)
                cr.fill()
        elif act == "rest":
            hands = [(lap + 3.8 * p, keys), (lap + 6.2 * p, keys)]
        elif act == "think":  # the near hand under the chin
            hands = [(lap + 3.8 * p, keys), (x0 - 1.0 * p, y0 + 5.0 * p)]
        elif act == "shade":  # the near hand shading the eyes
            hands = [(lap + 3.8 * p, keys), (x0 - 1.2 * p, y0 + 2.2 * p)]
        elif act == "hips":
            hands = [(x0 + 6.4 * pw, y0 + 4.5 * p), (x0 - 1.6 * p, y0 + 4.5 * p)]
        elif act == "cross":  # arms folded
            hands = [(x0 + 3.4 * p, y0 + 4.4 * p), (x0 + 1.2 * p, y0 + 4.8 * p)]
        elif act == "shrug":  # palms up at the shoulders
            hands = [(x0 + 7.2 * pw, y0 + 1.8 * p), (x0 - 2.2 * p, y0 + 1.8 * p)]
        elif act == "drum":  # drumming its fingers on the deck
            hands = [(lap + 3.8 * p, keys - 0.3 * p * max(0.0, math.sin(now * 5))),
                     (lap + 6.2 * p, keys - 0.3 * p * max(0.0, math.sin(now * 5 + 1.5)))]
        else:
            hands = [(lap + 3.8 * p, keys - p * max(0.0, math.sin(now * speed))),
                     (lap + 6.2 * p, keys - p * max(0.0, -math.sin(now * speed)))]
        tint = tuple(v + (1 - v) * 0.35 for v in body)
        for k, (hx, hy) in enumerate(hands):
            cr.set_source_rgba(*tint, 0.8 if k == 0 else 1.0)
            _rounded(cr, hx, hy, hs, hs, 0.55 * p)
            cr.fill()


class KlepsydraWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, use_limits: bool) -> None:
        super().__init__(application=app, title="Klepsydra")
        self.cfg = Config.load()
        use_limits = use_limits or self.cfg.limits_enabled
        self.use_limits = use_limits
        self.collector = col.Collector()
        self.limits: lim.Limits | None = None
        self._limits_inflight = False
        self._limits_backoff = 0.0
        self._backoff_s = 0.0
        self._limits_error: str | None = None
        # USD per 100% of each window, learned from the last good poll
        self._cap5 = 0.0
        self._cap7 = 0.0
        self._alerts: dict[str, tuple[datetime | None, int]] = {}
        self._css_provider: Gtk.CssProvider | None = None
        self._flash_text: str | None = None
        self._flash_src = 0
        self._watches: dict[Path, Gio.FileMonitor] = {}
        self._watch_pending = False
        self._win_xid: int | None = None
        self._last_pos: tuple[int, int] | None = None
        # the surface only exists once mapped, and the WM places the window
        # right after, so the restore has to wait for both
        self.connect("map", lambda *_: GLib.idle_add(self._restore_position))

        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("klepsydra")
        self.hourglass = Hourglass()
        self.heatmap = Heatmap()
        self.drop = Droplet()
        self.mini_drop = Droplet()
        self._state = "idle"
        self._idle_s = float("inf")
        self._refusals: int | None = None  # unknown until the first render
        self._reacted: int | None = None   # collector moments already acted on
        self._apply_style(save=False)

        handle = Gtk.WindowHandle()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        self.stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.stack.add_named(card, "full")
        handle.set_child(self.stack)
        self.set_child(handle)

        # header ------------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="klepsydra", xalign=0.0)
        title.add_css_class("title")
        # nothing else on the card hints that left-click opens the detail panel
        self.chevron = Gtk.Label(label="▴" if self.cfg.expanded else "▾")
        self.chevron.add_css_class("chevron")
        header.append(title)
        header.append(self.drop)
        header.append(self.chevron)
        card.append(header)

        # meters -------------------------------------------------------------
        self.m5h = MeterRow("5h window")
        self.mweek = MeterRow("week")
        self.m5h.set_visible(self.cfg.show_five_hour)
        self.mweek.set_visible(self.cfg.show_week)
        card.append(self.m5h)
        card.append(self.mweek)

        # today --------------------------------------------------------------
        self.today_lbl = Gtk.Label(xalign=0.0)
        self.today_lbl.add_css_class("today")
        self.today_lbl.set_visible(self.cfg.show_today)
        card.append(self.today_lbl)

        self.models_lbl = Gtk.Label(xalign=0.0, wrap=True)
        self.models_lbl.add_css_class("models")
        self.models_lbl.set_visible(self.cfg.show_models)
        card.append(self.models_lbl)

        # detail panel (left-click toggles) -----------------------------------
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.detail.add_css_class("detail")
        # one context bar per live session. How many there are is not known
        # ahead of time, so the rows are pooled and reused across renders.
        self.ctx_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ctx_head = Gtk.Label(label="context", xalign=0.0)
        self.ctx_head.add_css_class("detail-label")
        self.ctx_box.append(self.ctx_head)
        self.detail.append(self.ctx_box)
        self._ctx_rows: list[MeterRow] = []
        self.d_eta = DetailRow("limit eta")
        self.d_month = DetailRow("month")
        self.d_cache = DetailRow("cache hits")
        self.d_thinking = DetailRow("thinking")
        self.d_background = DetailRow("background")
        self.d_projects = DetailRow("top projects")
        self.d_branches = DetailRow("top branches")
        self.d_web = DetailRow("web searches")
        self.d_extra = DetailRow("extra credits")
        self._detail_rows = (self.d_eta, self.d_month, self.d_cache,
                             self.d_thinking, self.d_background,
                             self.d_projects, self.d_branches, self.d_web,
                             self.d_extra)
        for row in self._detail_rows:
            self.detail.append(row)
        self.week_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        week_lbl = Gtk.Label(label="last 7 days", xalign=0.0)
        week_lbl.add_css_class("detail-label")
        self.week_box.append(week_lbl)
        self.week_box.append(self.heatmap)
        self.detail.append(self.week_box)
        self.d_extra.set_visible(False)
        self.detail.set_visible(self.cfg.expanded)
        card.append(self.detail)

        self.foot_lbl = Gtk.Label(xalign=0.0)
        self.foot_lbl.add_css_class("foot")
        self.foot_lbl.set_visible(self.cfg.show_footer)
        card.append(self.foot_lbl)

        # a byline at the foot of the expanded card, linking to the author
        self.credit = Gtk.Label(label="built by corvardt", xalign=0.0)
        self.credit.add_css_class("credit")
        self.credit.set_cursor_from_name("pointer")
        self.credit.set_tooltip_text(CREDIT_URL)
        self.credit.set_visible(self.cfg.expanded)
        credit_click = Gtk.GestureClick(button=1)
        credit_click.connect("pressed", self._on_credit)
        self.credit.add_controller(credit_click)
        card.append(self.credit)

        # mini mode: the hourglass and the 5h figures, nothing else
        mini = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mini.add_css_class("card")
        mini.add_css_class("mini")
        self.mini_pct = Gtk.Label()
        self.mini_pct.add_css_class("meter-value")
        self.mini_sub = Gtk.Label()
        self.mini_sub.add_css_class("meter-label")
        mini.append(self.mini_drop)
        mini.append(self.hourglass)
        mini.append(self.mini_pct)
        mini.append(self.mini_sub)
        self.stack.add_named(mini, "mini")
        self.stack.set_visible_child_name("mini" if self.cfg.mini else "full")

        # interactions ---------------------------------------------------------
        # Middle-click goes on the *window* in the capture phase, not on the
        # card. Gtk.WindowHandle gives the card titlebar behaviour, and a
        # titlebar's middle-click lowers the window, which fires before a
        # bubble-phase controller on a descendant. Capturing at the window beats
        # the handle to the event, and the handler claims the sequence so the
        # card does not sink behind everything else on its way to a new theme.
        middle = Gtk.GestureClick(button=2)
        middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        middle.connect("released", self._on_middle_click)
        self.add_controller(middle)

        # left stays on the card and in the bubble phase: the handle needs the
        # button-1 press to drag the window
        # the droplet watches the pointer anywhere over the card
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_pointer)
        motion.connect("motion", self._on_pointer)
        motion.connect("leave", lambda _c: self._on_pointer(None, None, None))
        self.add_controller(motion)

        for box in (card, mini):
            left = Gtk.GestureClick(button=1)
            left.connect("released", self._on_left_click)
            box.add_controller(left)

        for box in (card, mini):
            scroll = Gtk.EventControllerScroll(
                flags=Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", self._on_scroll)
            box.add_controller(scroll)

        self._tick()
        # Poll is only a safety net (NFS, inotify exhaustion); the watches below
        # are what make new usage show up immediately.
        GLib.timeout_add_seconds(self.cfg.refresh_logs, self._tick)
        # Cheap, disk-free redraw so countdowns and the burn rate stay live.
        GLib.timeout_add_seconds(1, self._clock)
        if use_limits:
            # a restart reuses a cache younger than one poll interval, so
            # relaunching the widget repeatedly costs no extra requests
            self._poll_limits(max_age=LIMITS_STALE_S)
            GLib.timeout_add_seconds(self.cfg.refresh_limits, self._poll_limits)

    # -- log watching ----------------------------------------------------------
    def _sync_watches(self) -> None:
        """Watch every project log directory, so a write lands on screen in
        milliseconds instead of waiting for the next poll."""
        for root in col.log_roots():
            for path in [root] + [p for p in root.iterdir() if p.is_dir()]:
                if path in self._watches:
                    continue
                try:
                    mon = Gio.File.new_for_path(str(path)).monitor_directory(
                        Gio.FileMonitorFlags.NONE, None)
                except GLib.Error:
                    continue  # inotify limit reached; the poll still covers us
                mon.connect("changed", self._on_log_changed)
                self._watches[path] = mon

    def _on_log_changed(self, *_args) -> None:
        """Claude Code writes several lines per turn; coalesce them."""
        if self._watch_pending:
            return
        self._watch_pending = True

        def fire() -> bool:
            self._watch_pending = False
            self._tick()
            return False

        GLib.timeout_add(WATCH_DEBOUNCE_MS, fire)

    # -- interactions --------------------------------------------------------
    # -- placement (X11 only; see _x11) --------------------------------------
    def _xid(self) -> int | None:
        """Cached X window id, or None when not running on X11."""
        if self._win_xid is None and GdkX11 is not None:
            surface = self.get_surface()
            if isinstance(surface, GdkX11.X11Surface):
                with warnings.catch_warnings():  # get_xid is deprecated but is
                    warnings.simplefilter("ignore")  # still the only way to it
                    self._win_xid = surface.get_xid()
        return self._win_xid

    def _position(self) -> tuple[int, int] | None:
        x11, xid = _x11(), self._xid()
        if not x11 or xid is None:
            return None
        lib, dpy = x11
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        if not lib.XTranslateCoordinates(dpy, xid, lib.XDefaultRootWindow(dpy),
                                         0, 0, ctypes.byref(x), ctypes.byref(y),
                                         ctypes.byref(child)):
            return None
        return x.value, y.value

    def _restore_position(self) -> None:
        """Put the card back where it was left. The card is undecorated, so
        there is no frame offset to correct for and the move is exact."""
        x11, xid = _x11(), self._xid()
        # exactly (-1, -1) means unplaced, rather than "any negative": a monitor
        # to the left of the primary one gives real windows negative x
        if not x11 or xid is None or (self.cfg.x, self.cfg.y) == (-1, -1):
            return
        lib, dpy = x11
        lib.XMoveWindow(dpy, xid, self.cfg.x, self.cfg.y)
        lib.XFlush(dpy)

    def _remember_position(self) -> None:
        """Persist the position once a drag has settled.

        Called from the 1s clock. Writing on every tick would rewrite the INI
        continuously, and writing mid-drag would store a spot the card only
        passed through, so it saves only when two consecutive reads agree and
        that resting place differs from what is already on disk."""
        pos = self._position()
        if pos is None:
            return
        settled = pos == self._last_pos
        self._last_pos = pos
        if settled and pos != (self.cfg.x, self.cfg.y):
            self.cfg.x, self.cfg.y = pos
            self.cfg.save()

    def _on_pointer(self, _controller, x, y) -> None:
        for drop in (self.drop, self.mini_drop):
            ok, pt = (self.compute_point(drop, Graphene.Point().init(x, y))
                      if x is not None else (False, None))
            drop.look_at = pt.x if ok else None

    def _on_credit(self, gesture, *_args) -> None:
        """Claimed, so the click opens the site without collapsing the card."""
        _claim(gesture)
        Gio.AppInfo.launch_default_for_uri(CREDIT_URL, None)

    def _on_left_click(self, gesture, n_press, x, y) -> None:
        if gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.cfg.mini = not self.cfg.mini
            self.stack.set_visible_child_name("mini" if self.cfg.mini else "full")
            self._apply_style(save=True)
            return
        if self.cfg.mini:
            return
        self.cfg.expanded = not self.cfg.expanded
        self.detail.set_visible(self.cfg.expanded)
        self.credit.set_visible(self.cfg.expanded)
        self.chevron.set_label("▴" if self.cfg.expanded else "▾")
        self.cfg.save()
        self._render()

    def _on_middle_click(self, gesture, n_press, x, y) -> None:
        """Cycle to the next theme and remember it."""
        _claim(gesture)
        self.cfg.theme = themes.next_theme(themes.resolve(self.cfg.theme))
        self._apply_style(save=True)
        self._flash(self.cfg.theme)

    def _flash(self, text: str) -> None:
        """Briefly show a label (theme name) in the footer slot."""
        self._flash_text = text
        self.foot_lbl.set_label(text)
        self.foot_lbl.add_css_class("toast")

        def clear() -> bool:
            self._flash_src = 0
            self._flash_text = None
            self.foot_lbl.remove_css_class("toast")
            self._render()
            return False

        if self._flash_src:
            GLib.source_remove(self._flash_src)
        self._flash_src = GLib.timeout_add_seconds(2, clear)

    def _on_scroll(self, controller, dx, dy) -> bool:
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.SHIFT_MASK:  # Shift+scroll cycles themes
            self.cfg.theme = themes.next_theme(themes.resolve(self.cfg.theme),
                                               1 if dy > 0 else -1)
            self._apply_style(save=True)
            self._flash(self.cfg.theme)
            return True
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        step = -SCALE_STEP if dy > 0 else SCALE_STEP
        new = min(max(self.cfg.scale + step, SCALE_MIN), SCALE_MAX)
        if abs(new - self.cfg.scale) < 1e-9:
            return True
        self.cfg.scale = new
        self._apply_style(save=True)
        return True

    def _apply_style(self, save: bool) -> None:
        display = Gdk.Display.get_default()
        if self._css_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._css_provider)
        provider = Gtk.CssProvider()
        css = render_css(self.cfg.scale, self.cfg.opacity, self.cfg.theme)
        try:
            provider.load_from_string(css)          # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(css.encode())   # older PyGObject
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._css_provider = provider
        self.theme = themes.get(themes.resolve(self.cfg.theme))
        self.hourglass.configure(self.theme, self.cfg.scale)
        self.heatmap.configure(self.cfg.scale)
        for drop in (self.drop, self.mini_drop):
            drop.configure(self.cfg.scale)
        if hasattr(self, "m5h"):  # the first call runs before the rows exist
            for row in (self.m5h, self.mweek, *self._ctx_rows):
                row.bar.queue_draw()
        # mini mode shrinks to its content; the card keeps its configured width
        self.set_default_size(-1 if self.cfg.mini
                              else int(self.cfg.width * self.cfg.scale), -1)
        if save:
            self.cfg.save()

    # -- data ------------------------------------------------------------------
    def _poll_limits(self, max_age: float = 0.0) -> bool:
        if self._limits_inflight or time.monotonic() < self._limits_backoff:
            return True
        self._limits_inflight = True

        def work() -> None:
            result = lim.fetch_limits(max_age=max_age)
            GLib.idle_add(self._limits_done, result)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _limits_done(self, result: lim.Limits) -> bool:
        self._limits_inflight = False
        self._limits_error = result.error
        if result.error:
            if result.error == "rate limited":
                # Back off further on every refusal. The endpoint is shared
                # with Claude Code itself, so a busy session can keep the
                # account refused for minutes; knocking every poll only adds
                # to the traffic that caused it.
                self._backoff_s = min(max(self._backoff_s * 2, LIMITS_BACKOFF_S),
                                      LIMITS_BACKOFF_MAX_S)
                self._limits_backoff = time.monotonic() + self._backoff_s
            if self.limits and not self.limits.error:
                self._render()  # keep the last good numbers, aged by _render
                return False
        else:
            self._backoff_s = 0.0
            at = datetime.fromtimestamp(result.fetched_at, timezone.utc)
            self._cap5 = self._window_capacity(result.five_hour, FIVE_HOURS, at) or self._cap5
            self._cap7 = self._window_capacity(result.seven_day, WEEK, at) or self._cap7
        self.limits = result
        self._render()
        return False

    def _window_capacity(self, b: lim.Bucket | None, span: timedelta,
                         at: datetime) -> float:
        """USD that 100% of an official window is worth, from what was spent
        in it by the poll. Below 5% the ratio is mostly noise."""
        if not b or not b.resets_at or b.utilization < 5:
            return 0.0
        spent = self.collector.cost_between(b.resets_at - span, at)
        return spent / b.utilization * 100

    def _official_pct(self, b: lim.Bucket, cap: float,
                      now: datetime) -> tuple[float, bool]:
        """The polled utilization plus local spend since the poll, so the bar
        keeps moving between polls. Returns (pct, estimated)."""
        at = datetime.fromtimestamp(self.limits.fetched_at, timezone.utc)
        if not cap or not b.resets_at or now >= b.resets_at:
            return b.utilization, False
        drift = self.collector.cost_between(at, now + timedelta(seconds=1)) / cap * 100
        return b.utilization + drift, drift > 0

    def _alert(self, name: str, pct: float | None, until: datetime | None,
               now: datetime) -> None:
        """Notify once per window as `name` crosses each ALERT_LEVELS step,
        and when a window that had alerted resets."""
        prev_until, prev = self._alerts.get(name, (None, 0))
        if prev_until and now >= prev_until:
            if prev and self.cfg.notifications:
                self._notify(name, f"{name} limit reset", "")
            prev_until, prev = None, 0
        level = next((lv for lv in ALERT_LEVELS if pct is not None and pct >= lv), 0)
        if level > prev and self.cfg.notifications:
            self._notify(name, f"{name} limit at {pct:.0f}%",
                         f"resets in {_countdown(until)}" if until else "")
        self._alerts[name] = (until or prev_until, max(level, prev))

    def _notify(self, name: str, title: str, body: str) -> None:
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        self.get_application().send_notification(f"klepsydra-{name}", n)

    def _tick(self) -> bool:
        try:
            self.collector.refresh()
        except Exception:  # never let a parse hiccup kill the widget
            pass
        self._summarize(datetime.now(timezone.utc))
        self._sync_watches()
        self._render()
        return True

    def _summarize(self, now: datetime) -> None:
        """Full-history aggregates, recomputed per rescan rather than per
        1s clock tick."""
        c = self.collector
        self._week_sum = c.summarize(
            [e for e in c.entries if (now - e.ts).total_seconds() < 7 * 86400])
        self._today = c.today(now)
        self._today_sum = c.summarize(self._today)
        self._month_sum = c.summarize(c.month(now))
        self._capacity = c.capacity_estimate(now=now)
        self._week_grid = c.week_grid(now)  # USD; 0 until a block finishes

    def _clock(self) -> bool:
        """Re-render from the data already in memory. No disk, no parsing ,
        just enough to keep the countdowns and the burn rate ticking between
        the (much rarer) log rescans."""
        self._render()
        self._remember_position()
        return True

    # -- render ------------------------------------------------------------------
    def _render_contexts(self, rows: list[tuple[str, int, int]]) -> None:
        for i, (label, tokens, limit) in enumerate(rows):
            if i >= len(self._ctx_rows):
                row = MeterRow("")
                # session titles are sentences; the card is narrow
                row.label.set_ellipsize(Pango.EllipsizeMode.END)
                row.label.set_max_width_chars(26)
                row.label.add_css_class("ctx-label")
                row.value.add_css_class("ctx-value")
                self._ctx_rows.append(row)
                self.ctx_box.append(row)
            row = self._ctx_rows[i]
            row.label.set_label(label)
            pct = tokens / limit * 100
            row.set(pct, f"{pct:.0f}%")
            # the row shows a percentage only; the rest lives in the tooltip
            row.set_tooltip_text(
                f"{label}\n{tokens:,} / {limit:,} tokens · {pct:.0f}%")
            row.set_visible(True)
        for row in self._ctx_rows[len(rows):]:
            row.set_visible(False)
        self.ctx_head.set_visible(bool(rows))

    def _render_no_logs(self) -> None:
        """No Claude Code logs anywhere. Without this the card just reads
        'idle' / 'no active session', which is indistinguishable from a broken
        install, so say what we looked for instead."""
        self._state = "idle"
        self._idle_s = float("inf")
        self.m5h.set(None, "no logs found")
        self._render_mini(None, "no logs", "")
        self.mweek.set(None, "—")
        self.today_lbl.set_label("waiting for Claude Code usage")
        self.models_lbl.set_label("looked in ~/.claude/projects")
        self.foot_lbl.set_label("run Claude Code once to populate")
        if self.cfg.expanded:
            self._render_contexts([])
            for row in self._detail_rows:
                row.set_visible(False)
            self.week_box.set_visible(False)

    def _render_mini(self, pct: float | None, text: str, sub: str) -> None:
        self.hourglass.update(pct)
        for drop in (self.drop, self.mini_drop):
            drop.update(pct, self._state, self._idle_s)
        self.mini_pct.set_label(text)
        self.mini_sub.set_label(sub)
        self.mini_sub.set_visible(bool(sub))

    def _render(self) -> None:
        now = datetime.now(timezone.utc)
        c = self.collector
        if not col.log_roots():
            self._render_no_logs()
            return
        block = c.active_block(now)
        tok_rate, _ = c.recent_rate(now=now)
        self._idle_s = (now.timestamp() - c.last_activity
                        if c.last_activity else float("inf"))
        self._state = c.live(now)
        # one-offs logged since the last render; stale ones (a history
        # replayed at startup) are skipped
        if self._reacted is not None:
            for kind, at in c.moments[self._reacted:]:
                if now.timestamp() - at < 30:
                    for drop in (self.drop, self.mini_drop):
                        drop.react(kind)
        self._reacted = len(c.moments)
        # a refusal logged since the last render: the droplet jumps
        if self._refusals is not None and len(c.rejections) > self._refusals:
            for drop in (self.drop, self.mini_drop):
                drop.startle()
        self._refusals = len(c.rejections)
        official = self.limits if (self.limits and not self.limits.error) else None
        stale = bool(self.limits and
                     now.timestamp() - self.limits.fetched_at > LIMITS_STALE_S)
        capacity = self._cap5 or self._capacity

        # 5h meter
        if official and official.five_hour:
            b = official.five_hour
            pct, est = self._official_pct(b, self._cap5, now)
            cd = _countdown(b.resets_at)
            txt = f"{'~' if est else ''}{pct:.0f}%"
            pace = 1 - (b.resets_at - now) / FIVE_HOURS if b.resets_at else None
            self.m5h.set(pct, f"{txt}  ·  {cd}" if cd else txt, pace)
            self._render_mini(pct, txt, cd)
            self._alert("5h", pct, b.resets_at, now)
        elif block:
            # No official numbers: measure the block against your own busiest
            # finished block. Cost, not tokens: the real allowance weights
            # models very differently (see Collector.capacity_estimate).
            pct = (block.cost / capacity * 100) if capacity else None
            reset = _countdown(block.end)
            txt = (f"~{pct:.0f}%  ·  {reset}" if pct is not None
                   else f"${block.cost:.2f}  ·  {reset}")
            self.m5h.set(pct, txt, (now - block.start) / FIVE_HOURS)
            self._render_mini(pct, f"~{pct:.0f}%" if pct is not None
                              else f"${block.cost:.2f}", reset)
            self._alert("5h", pct, block.end, now)
        else:
            self.m5h.set(None, "idle")
            self._render_mini(None, "idle", "")
            self._alert("5h", None, None, now)

        # weekly meter, split by model: biggest spender first
        ws = self._week_sum
        mix = [(n, d["cost"] / ws["cost"]) for n, d in ws["by_model"].items()
               if ws["cost"] and d["cost"] / ws["cost"] >= 0.005]
        shares = [f for _, f in mix]
        self.mweek.set_tooltip_text(
            "  ·  ".join(f"{n} {f * 100:.0f}%" for n, f in mix) or None)
        if official and official.seven_day:
            b = official.seven_day
            pct, est = self._official_pct(b, self._cap7, now)
            extra = ""
            if official.seven_day_opus and official.seven_day_opus.utilization >= 50:
                extra = f"  ·  opus {official.seven_day_opus.utilization:.0f}%"
            cd = _countdown(b.resets_at)
            txt = f"{'~' if est else ''}{pct:.0f}%{extra}"
            pace = 1 - (b.resets_at - now) / WEEK if b.resets_at else None
            self.mweek.set(pct, f"{txt}  ·  {cd}" if cd else txt, pace, shares)
            self._alert("week", pct, b.resets_at, now)
        else:
            self.mweek.set(None, f"{col.fmt_tokens(ws['tokens'])} tok · ${ws['cost']:.0f}")

        # today
        today = self._today
        s = self._today_sum
        self.today_lbl.set_label(
            f"today   {col.fmt_tokens(s['tokens'])} tok · ${s['cost']:.2f}")

        top = list(s["by_model"].items())[:3]
        self.models_lbl.set_label(
            "   ".join(f"{name} ${d['cost']:.2f}" for name, d in top) or " ")

        # detail panel
        if self.cfg.expanded:
            self._render_contexts(c.contexts(CONTEXT_MINUTES, now))
            ms = self._month_sum
            self.d_cache.set(f"{s['cache_r'] / s['prompt'] * 100:.0f}% of input"
                             if s["prompt"] else EMPTY)
            self.d_thinking.set(f"{s['thinking'] / s['out'] * 100:.0f}% of output"
                                if s["thinking"] and s["out"] else EMPTY)
            self.d_month.set(f"{col.fmt_tokens(ms['tokens'])} tok · ${ms['cost']:.2f}")
            eta = c.eta_to_full(block, capacity, now) if (block and capacity) else None
            self.d_eta.set(f"{eta.astimezone():%H:%M} · in {_countdown(eta)}"
                           if eta else EMPTY)
            bg = s["bg_cost"]
            self.d_background.set(
                f"${bg:.2f} · {bg / s['cost'] * 100:.0f}% of today"
                if bg and s["cost"] else EMPTY)
            searches, fetches = s["web_search"], s["web_fetch"]
            if searches or fetches:
                bits = [f"{searches} · ${searches * col.WEB_SEARCH_USD:.2f}"] if searches else []
                if fetches:
                    bits.append(f"{fetches} fetch")
                self.d_web.set("  ".join(bits))
            else:
                self.d_web.set(EMPTY)
            self.heatmap.update(self._week_grid, now)
            self.week_box.set_visible(any(map(any, self._week_grid)))
            self.d_projects.set(
                "  ".join(f"{n} ${v:.2f}" for n, v in c.top_by(today, "project"))
                or EMPTY)
            self.d_branches.set(
                "  ".join(f"{n} ${v:.2f}" for n, v in c.top_by(today, "branch"))
                or EMPTY)
            if official and official.extra:
                self.d_extra.set_visible(True)
                self.d_extra.set(f"${official.extra.used_credits:.2f} / "
                                 f"${official.extra.monthly_limit:.0f}")
            else:
                self.d_extra.set_visible(False)

        # footer: burn rate / status. The 10-minute rate, not the block average:
        # the average smears idle gaps and lags a heavy run that just started.
        if tok_rate:
            foot = f"burn {col.fmt_tokens(tok_rate)} tok/min"
        elif block:
            foot = "idle"
        else:
            foot = "no active session"
        if self._limits_error:
            foot += f"  ·  ⚠ {self._limits_error}"
        elif stale:
            foot += "  ·  ⚠ limits stale"
        if self._flash_text is None:  # don't clobber a theme-name toast
            self.foot_lbl.set_label(foot)


def main() -> int:
    parser = argparse.ArgumentParser(prog="klepsydra")
    parser.add_argument("--limits", action="store_true",
                        help="opt-in: fetch official subscription limits "
                             "from api.anthropic.com using Claude Code's "
                             "stored OAuth token (read-only)")
    parser.add_argument("--theme", metavar="NAME",
                        help="theme for this run (saved to config); "
                             "use --list-themes to see them all")
    parser.add_argument("--list-themes", action="store_true",
                        help="print available theme names and exit")
    parser.add_argument("--version", action="version",
                        version=f"klepsydra {__version__}")
    args, _ = parser.parse_known_args()

    if args.list_themes:
        for name in themes.ORDER:
            t = themes.THEMES[name]
            print(f"{name:<16} {'dark' if t.dark else 'light':<5}  {t.bg}")
        print("auto             follows the desktop light/dark setting")
        return 0

    # Under `python3 -m klepsydra` the program name defaults to "__main__.py",
    # which becomes the Wayland app_id / X11 WM_CLASS: the shell then cannot
    # match the window to <APP_ID>.desktop and shows a second, generic icon.
    GLib.set_prgname(APP_ID)
    app = Gtk.Application(application_id=APP_ID)
    # glyph.svg, installed as hicolor/scalable/apps/<APP_ID>.svg. The card is
    # undecorated so this never shows on the window itself, only where the
    # shell represents it: alt-tab, the dock, the overview.
    Gtk.Window.set_default_icon_name(APP_ID)

    def on_activate(a: Gtk.Application) -> None:
        win = KlepsydraWindow(a, use_limits=args.limits)
        if args.theme:
            win.cfg.theme = args.theme.strip().lower()
            win._apply_style(save=True)
        win.present()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
