"""klepsydra themes: palettes substituted into style.css.

Each theme defines: bg (card), fg (text), border, and the three level
colors used by the meters/status dot (cool → warm → hot).
Add your own by dropping an entry in THEMES; the name goes in config.ini.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Theme:
    bg: str
    fg: str
    border: str
    cool: str
    warm: str
    hot: str
    dark: bool = True


THEMES: dict[str, Theme] = {
    # the original, near-black neutral
    "midnight": Theme(
        bg="#16161d", fg="#e8e6e3", border="#ffffff",
        cool="#7fb069", warm="#e6b450", hot="#e05252"),

    "nord": Theme(
        bg="#2e3440", fg="#eceff4", border="#88c0d0",
        cool="#a3be8c", warm="#ebcb8b", hot="#bf616a"),

    "dracula": Theme(
        bg="#282a36", fg="#f8f8f2", border="#bd93f9",
        cool="#50fa7b", warm="#f1fa8c", hot="#ff5555"),

    "gruvbox": Theme(
        bg="#282828", fg="#ebdbb2", border="#d5c4a1",
        cool="#b8bb26", warm="#fabd2f", hot="#fb4934"),

    "catppuccin": Theme(  # mocha
        bg="#1e1e2e", fg="#cdd6f4", border="#cba6f7",
        cool="#a6e3a1", warm="#f9e2af", hot="#f38ba8"),

    "tokyo-night": Theme(
        bg="#1a1b26", fg="#c0caf5", border="#7aa2f7",
        cool="#9ece6a", warm="#e0af68", hot="#f7768e"),

    "solarized-dark": Theme(
        bg="#002b36", fg="#eee8d5", border="#268bd2",  # base2: base1 reads too dim
        cool="#859900", warm="#b58900", hot="#dc322f"),

    "rose-pine": Theme(
        bg="#191724", fg="#e0def4", border="#c4a7e7",
        cool="#9ccfd8", warm="#f6c177", hot="#eb6f92"),

    "everforest": Theme(
        bg="#2d353b", fg="#d3c6aa", border="#a7c080",
        cool="#a7c080", warm="#dbbc7f", hot="#e67e80"),

    # green-on-black CRT look
    "terminal": Theme(
        bg="#000000", fg="#33ff66", border="#33ff66",
        cool="#33ff66", warm="#ffcc33", hot="#ff3355"),

    # light themes
    "paper": Theme(
        bg="#fbfbf9", fg="#2b2b2b", border="#000000",
        cool="#4c8c4a", warm="#b8860b", hot="#c0392b", dark=False),

    "solarized-light": Theme(
        bg="#fdf6e3", fg="#002b36", border="#93a1a1",  # base03: base01 reads too dim
        cool="#859900", warm="#b58900", hot="#dc322f", dark=False),

    # ---- Keraunos (github.com/corvardt/Keraunos) ---------------------------
    # Its map is an instrument readout with two media: a phosphor tube, and ink
    # on a chart recorder's roll. Tokens map over as void->bg, text->fg,
    # line->border, and the meters climb the palette's own rungs, which keeps
    # `strike` (reserved there for lightning) as the colour of a full meter.

    # the tube itself: light emitted on black, white kept for the strike
    "tube": Theme(
        bg="#0a0a0b", fg="#c8c8cc", border="#26262b",
        cool="#666670", warm="#c8c8cc", hot="#ffffff"),

    # P1, the oscilloscope green. Phosphors are the tube's neutrals multiplied
    # by a ratio normalised on its own luminance, so the hue changes and the
    # weight does not.
    "phosphor-green": Theme(
        bg="#060b08", fg="#7de490", border="#182b1e",
        cool="#40744f", warm="#7de490", hot="#a0ffb4"),

    # P3, the terminal that came after
    "phosphor-amber": Theme(
        bg="#0d0a05", fg="#ffbf5c", border="#332413",
        cool="#886233", warm="#ffbf5c", hot="#fff473"),

    "phosphor-ice": Theme(
        bg="#080a0e", fg="#9ed0fc", border="#1e2735",
        cool="#516a8a", warm="#9ed0fc", hot="#caffff"),

    # Crimson, by WildLeoKnight. https://lospec.com/palette-list/crimson
    "crimson": Theme(
        bg="#1b0326", fg="#eff9d6", border="#7a1c4b",
        cool="#7a1c4b", warm="#ba5044", hot="#eff9d6"),

    # Blood Demon RX, by Chicknhawk.
    # https://lospec.com/palette-list/blood-demon-rx
    "demon": Theme(
        bg="#171f37", fg="#ff7b8a", border="#5d2c44",
        cool="#81334a", warm="#ed4960", hot="#ff7b8a"),

    # Oil 6, by GrafxKid. https://lospec.com/palette-list/oil-6
    "oil": Theme(
        bg="#272744", fg="#fbf5ef", border="#494d7e",
        cool="#8b6d9c", warm="#c69fa5", hot="#f2d3ab"),

    # the other medium: ink on cool neutral stock, never cream. Black takes
    # over the strike's role, so here a full meter darkens instead of glowing.
    "chart": Theme(
        bg="#dedee0", fg="#2a2a2e", border="#c2c2c6",
        cool="#76767f", warm="#626268", hot="#000000", dark=False),
}

DEFAULT = "midnight"
ORDER = list(THEMES)


def _lum(hex_color: str) -> float:
    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    h = hex_color.lstrip("#")
    r, g, b = (channel(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours, 1 to 21."""
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def mix(a: str, b: str, k: float) -> str:
    """`a` moved a share `k` of the way to `b`."""
    ha, hb = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(
        f"{round(int(ha[i:i + 2], 16) + (int(hb[i:i + 2], 16) - int(ha[i:i + 2], 16)) * k):02x}"
        for i in (0, 2, 4))


def ink(t: Theme, alpha: float, target: float | None = None) -> float:
    """The alpha for fg over bg that reaches the contrast a role needs: 7
    for values (drawn at 0.7 and up), 4.5 for labels (0.4 and up), 3 for
    hints (0.25 and up), or `target`. Never lower than `alpha`, so the
    hierarchy between roles stays; below 0.25 it is decoration, left as is."""
    if target is None:
        target = 7.0 if alpha >= 0.7 else 4.5 if alpha >= 0.4 else 3.0 if alpha >= 0.25 else 0.0
    while alpha < 1.0 and contrast(mix(t.bg, t.fg, alpha), t.bg) < target:
        alpha = min(1.0, alpha + 0.02)
    return alpha


def _legible(color: str, t: Theme, target: float = 3.0) -> str:
    """A level colour moved toward fg until it stands 3:1 off the card."""
    k = 0.0
    out = color
    while contrast(out, t.bg) < target and k < 1.0:
        k = min(1.0, k + 0.05)
        out = mix(color, t.fg, k)
    return out


def _pee_or_mud(hex_color: str) -> bool:
    """Yellow, orange or brown: a coloured water drop in those reads as
    something else entirely."""
    h = hex_color.lstrip("#")
    hue, sat, val = colorsys.rgb_to_hsv(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    return 20 / 360 <= hue <= 70 / 360 and sat >= 0.25 and val >= 0.15


def droplet(t: Theme, level: str) -> str:
    """The mascot's body colour for a level (cool, warm, hot). It follows
    the theme, except that it is never yellow or brown: a warm drop turns
    pink (the theme's hot, turned toward pink) where hot allows, and
    anything else in those hues becomes the grey of the same lightness."""
    color = getattr(t, level)
    if not _pee_or_mud(color):
        return color
    if level == "warm" and not _pee_or_mud(t.hot):  # hot, turned toward pink
        h = t.hot.lstrip("#")
        _, sat, val = colorsys.rgb_to_hsv(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
        pink = colorsys.hsv_to_rgb(340 / 360, sat * 0.8, val)
        return _legible("#" + "".join(f"{round(v * 255):02x}" for v in pink), t)
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    grey = round(0.299 * r + 0.587 * g + 0.114 * b)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def get(name: str) -> Theme:
    """The theme, with any level colour too faint to read on its card
    lifted until it does."""
    t = THEMES.get(name.strip().lower(), THEMES[DEFAULT])
    return replace(t, cool=_legible(t.cool, t), warm=_legible(t.warm, t),
                   hot=_legible(t.hot, t))


def next_theme(name: str, step: int = 1) -> str:
    try:
        i = ORDER.index(name.strip().lower())
    except ValueError:
        i = 0
    return ORDER[(i + step) % len(ORDER)]


def resolve(name: str) -> str:
    """'auto' follows the desktop's dark/light preference."""
    name = (name or DEFAULT).strip().lower()
    if name != "auto":
        return name if name in THEMES else DEFAULT
    try:
        from gi.repository import Gtk
        dark = Gtk.Settings.get_default().get_property(
            "gtk-application-prefer-dark-theme")
    except Exception:
        dark = True
    return DEFAULT if dark else "paper"
