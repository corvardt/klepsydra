"""claude-glance: a small, always-on-display Claude usage widget for GNOME.

Local-first: reads Claude Code's JSONL logs. With --limits it additionally
fetches official subscription utilization from api.anthropic.com (opt-in).

Run:  python3 -m claude_glance            # local-only, zero network
      python3 -m claude_glance --limits   # + official limit percentages

Interaction:
  drag          move the card
  left-click    expand/collapse the detail panel
  middle-click  cycle theme
  Ctrl+scroll   zoom
  Shift+scroll  cycle theme
  right-click   menu (details, theme, reset zoom, quit)

All changes persist to ~/.config/claude-glance/config.ini.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from . import collector as col  # noqa: E402
from . import limits as lim  # noqa: E402
from . import themes  # noqa: E402
from .config import Config  # noqa: E402

LIMITS_STALE_S = 600
SCALE_STEP = 0.05
SCALE_MIN, SCALE_MAX = 0.6, 2.5
WATCH_DEBOUNCE_MS = 150   # coalesce the burst of writes Claude Code makes per turn
SPARK_HOURS = 12
EMPTY = "—"               # a DetailRow set to this hides itself instead


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


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m ago"
    return f"{s // 86400}d ago"


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
        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("meter-bar")
        self.append(top)
        self.append(self.bar)

    def set(self, pct: float | None, text: str) -> None:
        self.value.set_label(text)
        for c in ("cool", "warm", "hot"):
            self.bar.remove_css_class(c)
        if pct is None:
            self.bar.set_fraction(0.0)
        else:
            self.bar.set_fraction(min(max(pct, 0.0), 100.0) / 100.0)
            self.bar.add_css_class(_level_class(pct))


class DetailRow(Gtk.Box):
    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=0.0)
        lbl.add_css_class("detail-label")
        self.value = Gtk.Label(label="—", xalign=1.0, hexpand=True)
        self.value.add_css_class("detail-value")
        self.append(lbl)
        self.append(self.value)

    def set(self, text: str) -> None:
        """An empty row is hidden rather than shown as '—'. On a light day most
        of the panel is unpopulated, and a wall of dashes reads as broken."""
        self.value.set_label(text)
        self.set_visible(bool(text) and text != EMPTY)


class GlanceWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, use_limits: bool) -> None:
        super().__init__(application=app, title="Claude Glance")
        self.cfg = Config.load()
        use_limits = use_limits or self.cfg.limits_enabled
        self.use_limits = use_limits
        self.collector = col.Collector()
        self.limits: lim.Limits | None = None
        self._limits_inflight = False
        self._css_provider: Gtk.CssProvider | None = None
        self._flash_text: str | None = None
        self._watches: dict[Path, Gio.FileMonitor] = {}
        self._watch_pending = False

        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("glance")
        self._apply_style(save=False)

        handle = Gtk.WindowHandle()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        handle.set_child(card)
        self.set_child(handle)

        # header ------------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("dot")
        title = Gtk.Label(label="claude", xalign=0.0, hexpand=True)
        title.add_css_class("title")
        self.mode_lbl = Gtk.Label(label="live" if use_limits else "local")
        self.mode_lbl.add_css_class("mode")
        # nothing else on the card hints that left-click opens the detail panel
        self.chevron = Gtk.Label(label="▴" if self.cfg.expanded else "▾")
        self.chevron.add_css_class("chevron")
        header.append(self.dot)
        header.append(title)
        header.append(self.mode_lbl)
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
        self.d_rate = DetailRow("rate (10m)")
        self.d_proj = DetailRow("block proj.")
        self.d_eta = DetailRow("limit eta")
        self.d_week = DetailRow("week (local)")
        self.d_month = DetailRow("month")
        self.d_cache = DetailRow("cache hits")
        self.d_thinking = DetailRow("thinking")
        self.d_agents = DetailRow("subagents")
        self.d_web = DetailRow("web searches")
        self.d_sessions = DetailRow("sessions today")
        self.d_last = DetailRow("last activity")
        self.d_projects = DetailRow("top projects")
        self.d_extra = DetailRow("extra credits")
        # a plain unlabelled sparkline read as an orphan; as a row it lands on
        # the same label/value grid as everything else in the panel
        self.d_spark = DetailRow(f"last {SPARK_HOURS}h")
        self.d_spark.value.add_css_class("spark")
        self._detail_rows = (self.d_rate, self.d_proj, self.d_eta, self.d_week,
                             self.d_month, self.d_cache, self.d_thinking,
                             self.d_agents, self.d_web, self.d_sessions,
                             self.d_last, self.d_projects, self.d_extra,
                             self.d_spark)
        for row in self._detail_rows:
            self.detail.append(row)
        self.d_extra.set_visible(False)
        self.detail.set_visible(self.cfg.expanded)
        card.append(self.detail)

        self.foot_lbl = Gtk.Label(xalign=0.0)
        self.foot_lbl.add_css_class("foot")
        self.foot_lbl.set_visible(self.cfg.show_footer)
        card.append(self.foot_lbl)

        # interactions ---------------------------------------------------------
        self._build_menu(card, app)
        right = Gtk.GestureClick(button=3)
        right.connect("released", self._on_right_click)
        card.add_controller(right)

        left = Gtk.GestureClick(button=1)
        left.connect("released", self._on_left_click)
        card.add_controller(left)

        middle = Gtk.GestureClick(button=2)
        middle.connect("released", self._on_middle_click)
        card.add_controller(middle)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        card.add_controller(scroll)

        self._tick()
        self._sync_watches()
        # Poll is only a safety net (NFS, inotify exhaustion); the watches below
        # are what make new usage show up immediately.
        GLib.timeout_add_seconds(self.cfg.refresh_logs, self._tick)
        # Cheap, disk-free redraw so countdowns and "last activity" stay live.
        GLib.timeout_add_seconds(1, self._clock)
        if use_limits:
            self._poll_limits()
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

    # -- menu ------------------------------------------------------------------
    def _build_menu(self, card: Gtk.Widget, app: Gtk.Application) -> None:
        """Right-click menu. Quit used to fire on right-click directly, which
        made it far too easy to lose the widget by accident, and nothing on the
        card told you how to get it back."""
        for name, cb in (
            ("toggle-details", lambda *_: self._on_left_click(None, 1, 0, 0)),
            ("next-theme", lambda *_: self._on_middle_click(None, 1, 0, 0)),
            ("reset-zoom", lambda *_: self._reset_zoom()),
            ("quit", lambda *_: app.quit()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        menu = Gio.Menu()
        menu.append("Toggle details", "win.toggle-details")
        menu.append("Next theme", "win.next-theme")
        menu.append("Reset zoom", "win.reset-zoom")
        hints = Gio.Menu()
        hints.append("Drag to move · left-click for details", "win.noop")
        hints.append("Ctrl+scroll zoom · Shift+scroll theme", "win.noop")
        menu.append_section(None, hints)
        quit_section = Gio.Menu()
        quit_section.append("Quit", "win.quit")
        menu.append_section(None, quit_section)

        # a disabled action makes the hint row render as an inert label
        noop = Gio.SimpleAction.new("noop", None)
        noop.set_enabled(False)
        self.add_action(noop)

        self.menu = Gtk.PopoverMenu.new_from_model(menu)
        self.menu.set_parent(card)
        self.menu.set_has_arrow(False)
        self.menu.add_css_class("glance-menu")
        # GTK warns if a popover outlives its parent
        self.connect("destroy", lambda *_: self.menu.unparent())

    def _on_right_click(self, gesture, n_press, x, y) -> None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.menu.set_pointing_to(rect)
        self.menu.popup()

    def _reset_zoom(self) -> None:
        self.cfg.scale = 1.0
        self._apply_style(save=True)

    # -- interactions --------------------------------------------------------
    def _on_left_click(self, gesture, n_press, x, y) -> None:
        self.cfg.expanded = not self.cfg.expanded
        self.detail.set_visible(self.cfg.expanded)
        self.chevron.set_label("▴" if self.cfg.expanded else "▾")
        self.cfg.save()
        self._render()

    def _on_middle_click(self, gesture, n_press, x, y) -> None:
        """Cycle to the next theme and remember it."""
        self.cfg.theme = themes.next_theme(themes.resolve(self.cfg.theme))
        self._apply_style(save=True)
        self._flash(self.cfg.theme)

    def _flash(self, text: str) -> None:
        """Briefly show a label (theme name) in the footer slot."""
        self._flash_text = text
        self.foot_lbl.set_label(text)
        self.foot_lbl.add_css_class("toast")

        def clear() -> bool:
            self._flash_text = None
            self.foot_lbl.remove_css_class("toast")
            self._render()
            return False

        GLib.timeout_add_seconds(2, clear)

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
        self.set_default_size(int(self.cfg.width * self.cfg.scale), -1)
        if save:
            self.cfg.save()

    # -- data ------------------------------------------------------------------
    def _poll_limits(self) -> bool:
        if self._limits_inflight:
            return True
        self._limits_inflight = True

        def work() -> None:
            result = lim.fetch_limits()
            GLib.idle_add(self._limits_done, result)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _limits_done(self, result: lim.Limits) -> bool:
        self.limits = result
        self._limits_inflight = False
        self._render()
        return False

    def _tick(self) -> bool:
        try:
            self.collector.refresh()
        except Exception:  # never let a parse hiccup kill the widget
            pass
        self._render()
        return True

    def _clock(self) -> bool:
        """Re-render from the data already in memory. No disk, no parsing ,
        just enough to keep the countdowns and 'last activity' ticking between
        the (much rarer) log rescans."""
        self._render()
        return True

    # -- render ------------------------------------------------------------------
    def _render_no_logs(self) -> None:
        """No Claude Code logs anywhere. Without this the card just reads
        'idle' / 'no active session', which is indistinguishable from a broken
        install, so say what we looked for instead."""
        self.m5h.set(None, "no logs found")
        self.mweek.set(None, "—")
        self.today_lbl.set_label("waiting for Claude Code usage")
        self.models_lbl.set_label("looked in ~/.claude/projects")
        self.foot_lbl.set_label("run Claude Code once to populate")
        for cls in ("cool", "warm", "hot"):
            self.dot.remove_css_class(cls)
        self.dot.add_css_class("off")
        if self.cfg.expanded:
            for row in self._detail_rows:
                row.set_visible(False)

    def _render(self) -> None:
        now = datetime.now(timezone.utc)
        c = self.collector
        if not col.log_roots():
            self._render_no_logs()
            return
        block = c.active_block(now)
        official = self.limits if (self.limits and not self.limits.error) else None
        stale = bool(self.limits and
                     now.timestamp() - self.limits.fetched_at > LIMITS_STALE_S)
        capacity = c.capacity_estimate(now=now)  # USD; 0 until a block finishes

        # 5h meter
        if official and official.five_hour:
            b = official.five_hour
            cd = _countdown(b.resets_at)
            self.m5h.set(b.utilization,
                         f"{b.utilization:.0f}%  ·  {cd}" if cd else f"{b.utilization:.0f}%")
        elif block:
            # No official numbers: measure the block against your own busiest
            # finished block. Cost, not tokens: the real allowance weights
            # models very differently (see Collector.capacity_estimate).
            pct = (block.cost / capacity * 100) if capacity else None
            reset = _countdown(block.end)
            txt = (f"~{pct:.0f}%  ·  {reset}" if pct is not None
                   else f"${block.cost:.2f}  ·  {reset}")
            self.m5h.set(pct, txt)
        else:
            self.m5h.set(None, "idle")

        # weekly meter
        week = [e for e in c.entries if (now - e.ts).total_seconds() < 7 * 86400]
        ws = c.summarize(week)
        if official and official.seven_day:
            b = official.seven_day
            extra = ""
            if official.seven_day_opus and official.seven_day_opus.utilization >= 50:
                extra = f"  ·  opus {official.seven_day_opus.utilization:.0f}%"
            cd = _countdown(b.resets_at)
            txt = f"{b.utilization:.0f}%{extra}"
            self.mweek.set(b.utilization, f"{txt}  ·  {cd}" if cd else txt)
        else:
            self.mweek.set(None, f"{col.fmt_tokens(ws['tokens'])} tok · ${ws['cost']:.0f}")

        # today
        today = c.today(now)
        s = c.summarize(today)
        self.today_lbl.set_label(
            f"today   {col.fmt_tokens(s['tokens'])} tok · "
            f"${s['cost']:.2f} · {s['requests']} req")

        top = list(s["by_model"].items())[:3]
        self.models_lbl.set_label(
            "   ".join(f"{name} ${d['cost']:.2f}" for name, d in top) or " ")

        # detail panel
        if self.cfg.expanded:
            tok_rate, cost_rate = c.recent_rate(now=now)
            self.d_rate.set(f"{col.fmt_tokens(tok_rate)} tok/min · ${cost_rate:.2f}/min"
                            if tok_rate else "idle")
            self.d_week.set(f"{col.fmt_tokens(ws['tokens'])} tok · ${ws['cost']:.2f}")
            ms = c.summarize(c.month(now))
            self.d_month.set(f"{col.fmt_tokens(ms['tokens'])} tok · ${ms['cost']:.2f}")
            hit = c.cache_hit_rate(today)
            if hit is None:
                self.d_cache.set(EMPTY)
            else:  # one decimal near the edges so 99.7% doesn't read as 100%
                fmt = f"{hit:.1f}" if (hit > 99 or hit < 1) and hit not in (0, 100) else f"{hit:.0f}"
                self.d_cache.set(f"{fmt}% of prompt tokens")
            if block:
                ptok, pcost = c.projection(block, now)
                self.d_proj.set(f"→ {col.fmt_tokens(ptok)} tok · ${pcost:.2f} by reset")
            else:
                self.d_proj.set(EMPTY)
            eta = c.eta_to_full(block, capacity, now) if (block and capacity) else None
            self.d_eta.set(f"{eta.astimezone():%H:%M} · in {_countdown(eta)}"
                           if eta else "—")
            think = s["thinking"]
            self.d_thinking.set(
                f"{col.fmt_tokens(think)} tok · {think / s['output'] * 100:.0f}% of output"
                if think and s["output"] else "—")
            agent = s["agent_tokens"]
            self.d_agents.set(
                f"{col.fmt_tokens(agent)} tok · {agent / s['tokens'] * 100:.0f}% of today"
                if agent and s["tokens"] else "—")
            searches, fetches = s["web_search"], s["web_fetch"]
            if searches or fetches:
                bits = [f"{searches} · ${searches * col.WEB_SEARCH_USD:.2f}"] if searches else []
                if fetches:
                    bits.append(f"{fetches} fetch")
                self.d_web.set("  ".join(bits))
            else:
                self.d_web.set(EMPTY)
            spark = col.sparkline(c.hourly(SPARK_HOURS, now))
            self.d_spark.set(spark if spark.strip() else EMPTY)
            self.d_sessions.set(str(c.sessions_count(today)) if today else "0")
            self.d_last.set(_ago(c.last_activity()))
            projects = c.top_projects(today)
            self.d_projects.set(
                "  ".join(f"{n} ${v:.2f}" for n, v in projects) or "—")
            if official and official.extra:
                self.d_extra.set_visible(True)
                self.d_extra.set(f"${official.extra.used_credits:.2f} / "
                                 f"${official.extra.monthly_limit:.0f}")
            else:
                self.d_extra.set_visible(False)

        # footer: burn rate / status
        if block and block.entries:
            rate = c.burn_rate(block, now)
            foot = f"burn {col.fmt_tokens(rate)} tok/min"
        else:
            foot = "no active session"
        if self.limits and self.limits.error:
            foot += f"  ·  ⚠ {self.limits.error}"
        elif stale:
            foot += "  ·  ⚠ limits stale"
        if self._flash_text is None:  # don't clobber a theme-name toast
            self.foot_lbl.set_label(foot)

        # status dot
        for cls in ("cool", "warm", "hot", "off"):
            self.dot.remove_css_class(cls)
        if official and official.five_hour:
            self.dot.add_css_class(_level_class(official.five_hour.utilization))
        elif block:
            self.dot.add_css_class("cool")
        else:
            self.dot.add_css_class("off")


def main() -> int:
    parser = argparse.ArgumentParser(prog="claude-glance")
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
                        version=f"claude-glance {__version__}")
    args, _ = parser.parse_known_args()

    if args.list_themes:
        for name in themes.ORDER:
            t = themes.THEMES[name]
            print(f"{name:<16} {'dark' if t.dark else 'light'}  {t.bg}")
        print("auto             follows the desktop light/dark setting")
        return 0

    app = Gtk.Application(application_id=APP_ID)

    def on_activate(a: Gtk.Application) -> None:
        win = GlanceWindow(a, use_limits=args.limits)
        if args.theme:
            win.cfg.theme = args.theme.strip().lower()
            win._apply_style(save=True)
        win.present()

    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
