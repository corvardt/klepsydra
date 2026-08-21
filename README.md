# claude-glance

A small, subtle, always-on-display desktop widget for Debian/GNOME that shows
your Claude usage in real time. Built from scratch to be **fully auditable in
one sitting**, ~600 lines of plain Python, no third-party dependencies beyond
Debian's own GTK4 bindings.

<p align="center">
  <img src="base.png" alt="The collapsed claude-glance card, showing the 5-hour window at 71%, the weekly bar, today's tokens and cost, and the current burn rate." width="420">
</p>

## What it shows

- **5h window**, your position in Anthropic's rolling 5-hour rate-limit
  window, with countdown to reset. Local mode estimates it against your own
  median session; `--limits` mode shows the official percentage.
- **week**, 7-day usage (official weekly limit % with `--limits`, plus a
  separate note when your Opus weekly quota passes 50%).
- **today**, tokens, computed cost, and request count since local midnight.
- **model split**, today's cost per model.
- **burn rate**, tokens/minute in the active session, so you can see a heavy
  agent run eating your window in real time.

**Left-click the card** to expand a detail panel with more:

- **rate (10m)** and **block projection**, where the current 5h block lands at
  the current burn rate, plus an **eta** for when you'd hit the limit
- week + month totals (tokens & cost)
- **cache hit rate**, cache reads as % of prompt tokens (how much caching saves you)
- **thinking**, extended-thinking tokens as a share of output
- sessions today, last-activity time, and a 12-hour usage sparkline
- **top projects today**, cost split by the project directory the usage came from
- subagent and web-search usage, and extra-usage credits (`--limits` mode)

Rows with nothing to report hide themselves, so the panel stays as short as
your day was.

<p align="center">
  <img src="details.png" alt="The expanded claude-glance card, adding rate, block projection, limit eta, week and month totals, cache hit rate, thinking tokens, sessions, last activity, top projects, and a 12-hour sparkline." width="420">
</p>

## Customization

Everything lives in `~/.config/claude-glance/config.ini` (auto-created,
commented). Theme, scale/zoom, base width, opacity, refresh intervals, opt-in
network mode, and per-section visibility (hide the week bar, the model split,
the footer…).

| Gesture | Action |
|---|---|
| drag | move the card |
| left-click | expand/collapse the detail panel |
| middle-click | cycle theme |
| Ctrl+scroll | zoom in/out (5% steps) |
| Shift+scroll | cycle theme forward/back |
| right-click | menu, toggle details, next theme, reset zoom, quit |

All of it persists back to the config file.

### Themes

20 built-in palettes: `midnight` (default), `nord`, `dracula`, `gruvbox`,
`catppuccin`, `tokyo-night`, `solarized-dark`, `rose-pine`, `everforest`,
`terminal`, plus the light `paper` and `solarized-light`. Set `theme = auto`
to follow GNOME's light/dark preference.

Eight more come from [Keraunos](https://github.com/corvardt/Keraunos), whose
map is drawn as an instrument readout rather than a UI. It has two media, and
both are here: `tube`, a phosphor CRT where light is emitted on black, and
`chart`, ink laid on a chart recorder's cool grey roll. `phosphor-green`
(P1, the oscilloscope), `phosphor-amber` (P3) and `phosphor-ice` tint the tube
by multiplying its neutrals against a ratio normalised on its own luminance,
so the hue changes and the weight does not. `crimson`, `demon` and `oil` are
borrowed schemes ([WildLeoKnight](https://lospec.com/palette-list/crimson),
[Chicknhawk](https://lospec.com/palette-list/blood-demon-rx) and
[GrafxKid](https://lospec.com/palette-list/oil-6) respectively).

These eight are ramps rather than traffic lights: with no red to escalate into,
a filling meter climbs the palette's own rungs toward the colour Keraunos
reserves for a lightning strike. On `chart` that runs the other way, and a full
meter goes black.

```bash
claude-glance --list-themes      # print them all
claude-glance --theme nord       # use one (and save it)
```

Adding your own is six hex values in `themes.py`: `bg`, `fg`, `border`, and
the `cool`/`warm`/`hot` colors the meters shift through as you approach a limit.

## Trust model (read this: it's why this exists)

**Default mode makes zero network connections.** It only *reads* these paths:

| Path | Why |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code's own local usage logs |
| `~/.config/claude/projects/**/*.jsonl` | newer XDG location of the same |
| `$CLAUDE_CONFIG_DIR/projects/**` | if you've overridden the config dir |

It never writes outside its own install directory, never executes anything,
never phones home. Verify yourself:

```bash
grep -rn "urllib\|socket\|http\|requests" claude_glance/   # network only in limits.py
strace -f -e trace=network claude-glance                    # empty in default mode
```

**Opt-in `--limits` mode** additionally makes one request type:
`GET https://api.anthropic.com/api/oauth/usage`, the same endpoint Claude
Code's own `/usage` command calls, authenticated with the OAuth token Claude
Code already stores in `~/.claude/.credentials.json` (read-only; the token
never goes anywhere except Anthropic). The hostname is hard-pinned in
`limits.py`. We deliberately **never refresh/rotate** that token; if it
expires, the widget says so and falls back to local estimates until you next
use Claude Code.

## Install (Debian 12/13, GNOME)

```bash
sudo apt install python3-gi gir1.2-gtk-4.0   # usually already present on GNOME
git clone https://github.com/corvardt/claude-glance && cd claude-glance
./install.sh              # local-only mode
./install.sh --limits     # with official subscription percentages
claude-glance
```

The installer copies files to `~/.local/share/claude-glance`, adds a
`claude-glance` launcher and a GNOME autostart entry. Nothing needs root
except the apt packages. Pass `--no-autostart` if you'd rather launch it
yourself.

### Update / uninstall

```bash
git pull && ./install.sh --update    # keeps the flags you installed with
./install.sh --uninstall             # keeps ~/.config/claude-glance
./install.sh --uninstall --purge     # config too
```

### Or install the .deb

Each release ships a `.deb` built by `tools/make_deb.py` (stdlib only, no
`dpkg-dev` needed to produce it). It installs system-wide, so `apt` handles
updates and removal exactly:

```bash
sudo apt install ./claude-glance_*.deb
sudo apt purge claude-glance          # removes everything, config included
```

Pick one or the other; the two methods install to different prefixes. They
share an autostart filename, so if you do end up with both, the `install.sh`
copy shadows the packaged one rather than starting a second widget.

## Always-on-display on GNOME

- **Move it:** drag the card anywhere.
- **Quit:** right-click the card → *Quit*.
- **Keep on all workspaces (Wayland):** focus the widget, press
  `Alt+Space` → *Always on Visible Workspace*. GNOME remembers this per
  application, so it persists across restarts.
- **Xorg alternative:** `wmctrl -r 'Claude Glance' -b add,sticky,below`
  pins it to the desktop beneath other windows.

## Accuracy notes

- Cost is computed from tokens × Anthropic's published per-MTok prices
  (embedded table in `collector.py`, including split 5m/1h cache-write rates).
  Update the table there if prices change.
- The 5-hour block reconstruction mirrors ccusage's algorithm (window starts
  floored to the hour, new block after ≥5h gap) which matches Anthropic's
  observed reset behavior. Local logs only see *Claude Code* usage; chats on
  claude.ai count against the same limit but aren't in the logs, which is what
  `--limits` mode is for.
- Duplicate streamed log lines are deduplicated by `message.id:requestId`.

## Files

```
claude_glance/collector.py   log discovery, parsing, dedup, pricing, 5h blocks
claude_glance/limits.py      opt-in official limits (single pinned endpoint)
claude_glance/widget.py      GTK4 UI (zoom, detail panel)
claude_glance/config.py      ~/.config/claude-glance/config.ini handling
claude_glance/themes.py      colour palettes
claude_glance/style.css      the look (theme tokens + px values scale with zoom)
install.sh                   user-level install / update / uninstall
tools/make_deb.py            builds the .deb (stdlib only, no dpkg needed)
tests/                       stdlib test runners, no pytest required
```

(The config file is the only thing the widget ever writes, besides its own
install dir.)

Right-click → *Quit* closes it. `./install.sh --uninstall` removes it.

## Contributing

```bash
python3 tests/test_collector.py   # pricing, dedup, 5h blocks, stdlib only
python3 tests/test_config.py      # config round-trip
python3 tools/make_deb.py dist/   # package build
```

CI runs both suites on Python 3.9 and 3.13, builds the `.deb`, and parses every
theme's CSS under GTK4. No third-party dependencies, please keep it that way;
"auditable in one sitting" is the point of the project.
