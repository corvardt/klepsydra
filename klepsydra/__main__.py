import os

# Wayland forbids a client from placing its own window, so on Wayland the card
# cannot come back where you left it. Under X11 (XWayland included) it can, via
# libX11; see _x11_position/_x11_move in widget.py.
# A priority list, not a hard "x11": GDK tries each in turn, so a session with
# no X server at all still starts, just without remembering its position.
# setdefault, so GDK_BACKEND=wayland on the command line still wins.
os.environ.setdefault("GDK_BACKEND", "x11,wayland")

from .widget import main  # noqa: E402

raise SystemExit(main())
