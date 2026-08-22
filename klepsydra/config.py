"""klepsydra config: tiny INI file at ~/.config/klepsydra/config.ini.

Auto-created with defaults on first run. Only this file is ever written.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "klepsydra"
CONFIG_PATH = CONFIG_DIR / "config.ini"

DEFAULT_INI = """\
; klepsydra configuration: edit freely, applied on next start.
; scale / opacity can also be changed live with Ctrl+scroll on the widget.

[widget]
; theme: midnight, nord, dracula, gruvbox, catppuccin, tokyo-night,
;        solarized-dark, rose-pine, everforest, terminal, paper,
;        solarized-light, or 'auto' to follow the desktop light/dark setting.
;        Keraunos media: tube, phosphor-green, phosphor-amber, phosphor-ice,
;        crimson, demon, oil, and the light chart.
;        Run `klepsydra --list-themes` to see them all.
; Middle-click the card to cycle themes live.
theme = midnight
; zoom factor for the whole card (0.6 - 2.5). Ctrl+scroll adjusts & saves this.
scale = 1.0
; base card width in px (before scale)
width = 260
; card background opacity (0.0 - 1.0)
opacity = 0.82
; start with the detail panel expanded (left-click toggles it)
expanded = false

[network]
; OPT-IN: fetch official subscription limit percentages from
; api.anthropic.com using the OAuth token Claude Code stores locally.
; false = the widget makes zero network connections.
official_limits = false

[refresh]
; seconds between local log rescans. This is only a safety net: the widget
; watches the log directories and redraws as soon as Claude Code writes, so
; new usage normally appears within a fraction of a second.
logs = 5
; seconds between official limit polls (only if official_limits = true)
limits = 60

[sections]
; hide any part of the card you don't care about
five_hour = true
week = true
today = true
models = true
footer = true
"""


@dataclass
class Config:
    theme: str = "midnight"
    scale: float = 1.0
    width: int = 260
    opacity: float = 0.82
    expanded: bool = False
    limits_enabled: bool = False
    refresh_logs: int = 5
    refresh_limits: int = 60
    show_five_hour: bool = True
    show_week: bool = True
    show_today: bool = True
    show_models: bool = True
    show_footer: bool = True

    @staticmethod
    def load() -> "Config":
        cfg = Config()
        if not CONFIG_PATH.exists():
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_text(DEFAULT_INI)
            except OSError:
                pass
            return cfg
        p = configparser.ConfigParser()
        try:
            p.read(CONFIG_PATH)
        except configparser.Error:
            return cfg
        g = p["widget"] if p.has_section("widget") else {}
        cfg.theme = str(g.get("theme", cfg.theme)).strip().lower()
        cfg.scale = _clamp(_get_float(g, "scale", cfg.scale), 0.6, 2.5)
        cfg.width = int(_clamp(_get_float(g, "width", cfg.width), 160, 800))
        cfg.opacity = _clamp(_get_float(g, "opacity", cfg.opacity), 0.0, 1.0)
        cfg.expanded = _get_bool(g, "expanded", cfg.expanded)
        n = p["network"] if p.has_section("network") else {}
        cfg.limits_enabled = _get_bool(n, "official_limits", False)
        r = p["refresh"] if p.has_section("refresh") else {}
        cfg.refresh_logs = max(int(_get_float(r, "logs", cfg.refresh_logs)), 1)
        cfg.refresh_limits = max(int(_get_float(r, "limits", cfg.refresh_limits)), 30)
        s = p["sections"] if p.has_section("sections") else {}
        cfg.show_five_hour = _get_bool(s, "five_hour", True)
        cfg.show_week = _get_bool(s, "week", True)
        cfg.show_today = _get_bool(s, "today", True)
        cfg.show_models = _get_bool(s, "models", True)
        cfg.show_footer = _get_bool(s, "footer", True)
        return cfg

    def save(self) -> None:
        """Rewrite only the `key = value` lines, leaving every comment and blank
        line exactly where the user (or the template) put it. configparser can't
        do this: it drops comments on write, which would erase the annotated
        template the first time you cycle a theme."""
        values = {
            "widget": {
                "theme": self.theme,
                "scale": f"{self.scale:.2f}",
                "width": str(self.width),
                "opacity": f"{self.opacity:.2f}",
                "expanded": str(self.expanded).lower(),
            },
            "network": {"official_limits": str(self.limits_enabled).lower()},
            "refresh": {
                "logs": str(self.refresh_logs),
                "limits": str(self.refresh_limits),
            },
            "sections": {
                "five_hour": str(self.show_five_hour).lower(),
                "week": str(self.show_week).lower(),
                "today": str(self.show_today).lower(),
                "models": str(self.show_models).lower(),
                "footer": str(self.show_footer).lower(),
            },
        }
        try:
            text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else DEFAULT_INI
        except OSError:
            text = DEFAULT_INI
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(_patch_ini(text, values))
        except OSError:
            pass


_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_]+)(\s*=\s*)(.*)$")


def _patch_ini(text: str, values: dict[str, dict[str, str]]) -> str:
    """Substitute values into an existing INI in place, appending any key or
    section the file is missing."""
    lines = text.splitlines()
    pending = {sec: dict(kv) for sec, kv in values.items()}
    section: str | None = None
    # index just past each section's last key line, where new keys get inserted,
    # so they land before any trailing comments rather than after them
    section_end: dict[str, int] = {}

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            section_end[section] = i + 1
            continue
        if section is None or not stripped or stripped[0] in ";#":
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key = m.group(2).lower()
        if key in pending.get(section, {}):
            lines[i] = m.group(1) + m.group(2) + m.group(3) + pending[section].pop(key)
        section_end[section] = i + 1

    tail: list[str] = []
    inserts: list[tuple[int, list[str]]] = []
    for sec, kv in pending.items():
        if not kv:
            continue
        new = [f"{k} = {v}" for k, v in kv.items()]
        if sec in section_end:
            inserts.append((section_end[sec], new))
        else:
            tail += ["", f"[{sec}]"] + new
    for idx, new in sorted(inserts, reverse=True):  # bottom-up keeps indices valid
        lines[idx:idx] = new
    lines += tail
    return "\n".join(lines) + "\n"


def _get_float(section, key: str, default: float) -> float:
    try:
        return float(section.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_bool(section, key: str, default: bool) -> bool:
    raw = str(section.get(key, default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)
