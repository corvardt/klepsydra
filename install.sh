#!/usr/bin/env bash
# claude-glance installer: Debian/GNOME
# Installs to ~/.local/share/claude-glance, adds a launcher and autostart.
set -euo pipefail

APP_DIR="$HOME/.local/share/claude-glance"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"
CONFIG_DIR="$HOME/.config/claude-glance"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LAUNCHER="$BIN_DIR/claude-glance"
AUTOSTART="$AUTOSTART_DIR/claude-glance.desktop"
STAMP="$APP_DIR/.install"          # records version + the flags used to install

usage() {
  cat <<'USAGE'
claude-glance installer

  ./install.sh [--limits] [--no-autostart]   install (or reinstall)
  ./install.sh --update                      reinstall from this checkout,
                                             reusing the flags you chose before
  ./install.sh --uninstall [--purge]         remove it; --purge also deletes
                                             ~/.config/claude-glance
  ./install.sh --help

  --limits         opt in to official subscription percentages, which lets the
                   widget talk to api.anthropic.com. Default is zero network.
  --no-autostart   install without the GNOME autostart entry.
USAGE
}

version_of() {  # read __version__ out of the package without importing GTK
  sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/claude_glance/__init__.py" 2>/dev/null
}

stop_running() {
  # Anchored, and limited to this user: an unanchored -f pattern also matches
  # any shell whose command line merely mentions claude_glance, including the
  # one running this script.
  if pkill -u "$(id -u)" -f '^python3 -m claude_glance' 2>/dev/null; then
    echo "==> Stopped the running widget"
    sleep 0.3
  fi
}

do_uninstall() {
  local purge="$1"
  stop_running
  echo "==> Removing $APP_DIR"      && rm -rf "$APP_DIR"
  echo "==> Removing $LAUNCHER"     && rm -f  "$LAUNCHER"
  echo "==> Removing $AUTOSTART"    && rm -f  "$AUTOSTART"
  if [[ "$purge" == "yes" ]]; then
    echo "==> Removing $CONFIG_DIR" && rm -rf "$CONFIG_DIR"
  else
    echo "    Keeping your config at $CONFIG_DIR (--purge removes it too)"
  fi
  if dpkg -l claude-glance 2>/dev/null | grep -q '^[ri][ic]'; then
    echo
    echo "    Note: a claude-glance .deb is also on this system. This script"
    echo "    does not touch it. Remove it with: sudo dpkg --purge claude-glance"
  fi
  echo
  echo "Uninstalled."
}

# ---- argument parsing -------------------------------------------------------
MODE="install"
LIMITS_FLAG=""
AUTOSTART_ENABLED="yes"
PURGE="no"

for arg in "$@"; do
  case "$arg" in
    --limits)       LIMITS_FLAG=" --limits" ;;
    --no-autostart) AUTOSTART_ENABLED="no" ;;
    --update)       MODE="update" ;;
    --uninstall)    MODE="uninstall" ;;
    --purge)        PURGE="yes" ;;
    --help|-h)      usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$MODE" == "uninstall" ]]; then
  do_uninstall "$PURGE"
  exit 0
fi

if [[ "$MODE" == "update" ]]; then
  if [[ ! -f "$STAMP" ]]; then
    echo "No existing install found at $APP_DIR." >&2
    echo "Run ./install.sh (optionally with --limits) to install first." >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$STAMP"                       # sets INSTALLED_VERSION / LIMITS / AUTOSTART_ON
  LIMITS_FLAG="${LIMITS:-}"
  AUTOSTART_ENABLED="${AUTOSTART_ON:-yes}"
  echo "==> Updating ${INSTALLED_VERSION:-?} -> $(version_of "$SRC_DIR")"
  [[ -n "$LIMITS_FLAG" ]] && echo "    (keeping --limits from your last install)"
fi

# ---- install ----------------------------------------------------------------
if [[ -n "$LIMITS_FLAG" ]]; then
  echo "==> Installing WITH official limit fetching (talks to api.anthropic.com only)"
else
  echo "==> Installing in local-only mode (zero network). Re-run with --limits to change."
fi

echo "==> Checking dependencies (python3-gi, GTK4)…"
MISSING=()
python3 -c "import gi" 2>/dev/null || MISSING+=("python3-gi")
python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" 2>/dev/null \
  || MISSING+=("gir1.2-gtk-4.0")
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "    Missing packages: ${MISSING[*]}"
  echo "    Run: sudo apt install ${MISSING[*]}"
  exit 1
fi

stop_running

echo "==> Copying files to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
rm -rf "$APP_DIR/claude_glance"        # drop files removed upstream, incl. stale .pyc
cp -r "$SRC_DIR/claude_glance" "$APP_DIR/"
rm -rf "$APP_DIR/claude_glance/__pycache__"

cat > "$STAMP" <<EOF
INSTALLED_VERSION="$(version_of "$SRC_DIR")"
LIMITS="$LIMITS_FLAG"
AUTOSTART_ON="$AUTOSTART_ENABLED"
EOF

echo "==> Creating launcher $LAUNCHER"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR" && exec python3 -m claude_glance$LIMITS_FLAG "\$@"
EOF
chmod +x "$LAUNCHER"

if [[ "$AUTOSTART_ENABLED" == "yes" ]]; then
  echo "==> Creating autostart entry"
  mkdir -p "$AUTOSTART_DIR"
  cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Glance
Comment=Local Claude usage widget
Exec=$LAUNCHER$LIMITS_FLAG
X-GNOME-Autostart-enabled=true
NoDisplay=false
EOF
else
  echo "==> Skipping autostart entry (--no-autostart)"
  rm -f "$AUTOSTART"
fi

echo
echo "Installed claude-glance $(version_of "$SRC_DIR"). Start now with:  claude-glance"
if ! command -v claude-glance >/dev/null 2>&1; then
  echo "  ⚠ $BIN_DIR is not on your PATH. Add it to ~/.profile:"
  echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
echo
echo "Tips:"
echo "  • Drag the card anywhere; right-click it for the menu (quit is in there)."
echo "  • GNOME (Wayland) shows it as a normal window. To keep it on all"
echo "    workspaces: Alt+Space on the focused widget → 'Always on Visible"
echo "    Workspace'. On Xorg you can also: wmctrl -r 'Claude Glance' -b add,sticky,below"
echo "  • Update:    ./install.sh --update"
echo "  • Uninstall: ./install.sh --uninstall"
