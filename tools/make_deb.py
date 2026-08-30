#!/usr/bin/env python3
"""Build klepsydra_<version>_all.deb without needing dpkg installed.

A .deb is an `ar` archive containing debian-binary, control.tar.gz and
data.tar.gz, all built here with stdlib only, byte-compatible with dpkg.

Usage: python3 tools/make_deb.py [output_dir]
"""

from __future__ import annotations

import gzip
import hashlib
import io
import sys
import tarfile
import time
from pathlib import Path

PKG = "klepsydra"
ROOT = Path(__file__).resolve().parents[1]
MAINTAINER = "corvardt <corvardt@protonmail.com>"
HOMEPAGE = "https://github.com/corvardt/klepsydra"

sys.path.insert(0, str(ROOT))
from klepsydra import __version__ as VERSION  # noqa: E402

CONTROL = f"""\
Package: {PKG}
Version: {VERSION}
Architecture: all
Section: utils
Priority: optional
Maintainer: {MAINTAINER}
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-4.0
Installed-Size: {{size}}
Homepage: {HOMEPAGE}
Description: subtle always-on desktop widget showing Claude usage
 Reads Claude Code's local JSONL logs to display token usage, cost,
 and the rolling 5-hour rate-limit window on a small translucent
 GTK4 card. Makes zero network connections unless official limit
 fetching is explicitly enabled in ~/.config/klepsydra/config.ini.
 .
 Unofficial. Not affiliated with or endorsed by Anthropic. "Claude"
 and "Claude Code" are Anthropic's trademarks, used here only to say
 what this reads.
"""

LAUNCHER = """\
#!/bin/sh
export PYTHONPATH="/usr/share/klepsydra${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m klepsydra "$@"
"""

DESKTOP = """\
[Desktop Entry]
Type=Application
Name=Klepsydra
Comment=Claude usage widget (local logs; network only if enabled in config)
Exec=klepsydra
Icon=io.github.corvardt.Klepsydra
StartupWMClass=io.github.corvardt.Klepsydra
Categories=Utility;Monitor;
Keywords=claude;usage;tokens;
"""

AUTOSTART = DESKTOP + "X-GNOME-Autostart-enabled=true\n"

COPYRIGHT = f"""\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: klepsydra
Source: {HOMEPAGE}

Files: *
Copyright: 2026 corvardt
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 SOFTWARE.
 .
 The full licence text also ships as /usr/share/doc/klepsydra/LICENSE.
"""


def build() -> Path:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time())

    # ---- data.tar.gz -------------------------------------------------------
    files: list[tuple[str, bytes, int]] = []  # (path, content, mode)
    pkgdir = ROOT / "klepsydra"
    for src in sorted(pkgdir.iterdir()):
        if src.suffix in (".py", ".css") and src.is_file():
            files.append((f"./usr/share/klepsydra/klepsydra/{src.name}",
                          src.read_bytes(), 0o644))
    files.append(("./usr/bin/klepsydra", LAUNCHER.encode(), 0o755))
    # named after APP_ID, which is what the desktop entry and the GTK window
    # both point at, so the shell matches window to launcher
    files.append(("./usr/share/icons/hicolor/scalable/apps/"
                  "io.github.corvardt.Klepsydra.svg",
                  (ROOT / "glyph.svg").read_bytes(), 0o644))
    files.append(("./usr/share/applications/klepsydra.desktop",
                  DESKTOP.encode(), 0o644))
    files.append(("./etc/xdg/autostart/klepsydra.desktop",
                  AUTOSTART.encode(), 0o644))
    files.append((f"./usr/share/doc/{PKG}/README.md",
                  (ROOT / "README.md").read_bytes(), 0o644))
    files.append((f"./usr/share/doc/{PKG}/copyright", COPYRIGHT.encode(), 0o644))
    files.append((f"./usr/share/doc/{PKG}/LICENSE",
                  (ROOT / "LICENSE").read_bytes(), 0o644))

    changelog = (f"{PKG} ({VERSION}) unstable; urgency=low\n\n"
                 f"  * Release {VERSION}.\n\n"
                 f" -- {MAINTAINER}  "
                 f"{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(now))}\n")
    files.append((f"./usr/share/doc/{PKG}/changelog.Debian.gz",
                  gzip.compress(changelog.encode(), mtime=0), 0o644))

    data_buf = io.BytesIO()
    with tarfile.open(fileobj=data_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        seen_dirs: set[str] = set()
        for path, content, mode in files:
            # add parent dirs
            parts = path.lstrip("./").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "./" + "/".join(parts[:i])
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    ti = tarfile.TarInfo(d + "/")
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.mtime = now
                    ti.uname = ti.gname = "root"
                    tar.addfile(ti)
            ti = tarfile.TarInfo(path)
            ti.size = len(content)
            ti.mode = mode
            ti.mtime = now
            ti.uname = ti.gname = "root"
            tar.addfile(ti, io.BytesIO(content))
    data_tar = data_buf.getvalue()

    # ---- control.tar.gz ----------------------------------------------------
    installed_kb = max(sum(len(c) for _, c, _ in files) // 1024, 1)
    control = CONTROL.format(size=installed_kb)
    md5sums = "".join(
        f"{hashlib.md5(content).hexdigest()}  {path.lstrip('./')}\n"
        for path, content, _ in files)
    conffiles = "/etc/xdg/autostart/klepsydra.desktop\n"

    ctrl_buf = io.BytesIO()
    with tarfile.open(fileobj=ctrl_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, content in (("./control", control),
                              ("./md5sums", md5sums),
                              ("./conffiles", conffiles)):
            ti = tarfile.TarInfo(name)
            data = content.encode()
            ti.size = len(data)
            ti.mode = 0o644
            ti.mtime = now
            ti.uname = ti.gname = "root"
            tar.addfile(ti, io.BytesIO(data))
    control_tar = ctrl_buf.getvalue()

    # ---- ar archive ---------------------------------------------------------
    def ar_member(name: str, content: bytes) -> bytes:
        header = (f"{name:<16}{now:<12}0     0     100644  "
                  f"{len(content):<10}`\n").encode("ascii")
        assert len(header) == 60
        pad = b"\n" if len(content) % 2 else b""
        return header + content + pad

    deb = out_dir / f"{PKG}_{VERSION}_all.deb"
    with deb.open("wb") as f:
        f.write(b"!<arch>\n")
        f.write(ar_member("debian-binary", b"2.0\n"))
        f.write(ar_member("control.tar.gz", control_tar))
        f.write(ar_member("data.tar.gz", data_tar))
    print(f"built {deb} ({deb.stat().st_size} bytes)")
    return deb


if __name__ == "__main__":
    build()
