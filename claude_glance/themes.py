"""claude-glance themes: palettes substituted into style.css.

Each theme defines: bg (card), fg (text), border, and the three level
colors used by the meters/status dot (cool → warm → hot).
Add your own by dropping an entry in THEMES; the name goes in config.ini.
"""

from __future__ import annotations

from dataclasses import dataclass


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
        bg="#002b36", fg="#93a1a1", border="#268bd2",
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
        bg="#fdf6e3", fg="#586e75", border="#93a1a1",
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


def get(name: str) -> Theme:
    return THEMES.get(name.strip().lower(), THEMES[DEFAULT])


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
