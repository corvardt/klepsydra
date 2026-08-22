"""Tests for config save/load round-tripping. Run: python3 tests/test_config.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent))
from klepsydra import config as cfgmod  # noqa: E402
from klepsydra.config import Config, DEFAULT_INI, _patch_ini  # noqa: E402


def use_tmp(tmp: Path) -> None:
    cfgmod.CONFIG_DIR = tmp
    cfgmod.CONFIG_PATH = tmp / "config.ini"


def test_comments_survive_save():
    with tempfile.TemporaryDirectory() as d:
        use_tmp(Path(d))
        c = Config.load()                       # writes the annotated template
        before = cfgmod.CONFIG_PATH.read_text()
        assert before == DEFAULT_INI, "first load should lay down the template"

        c.theme = "nord"
        c.scale = 1.25
        c.save()
        after = cfgmod.CONFIG_PATH.read_text()

        kept = [ln for ln in after.splitlines() if ln.strip().startswith(";")]
        orig = [ln for ln in DEFAULT_INI.splitlines() if ln.strip().startswith(";")]
        assert kept == orig, f"comments lost: {len(orig)} -> {len(kept)}"
        assert "theme = nord" in after
        assert "scale = 1.25" in after

        again = Config.load()
        assert again.theme == "nord" and abs(again.scale - 1.25) < 1e-9
        # a second save must be a no-op, not a slow drift of the file
        again.save()
        assert cfgmod.CONFIG_PATH.read_text() == after, "save is not idempotent"


def test_user_edits_and_layout_preserved():
    with tempfile.TemporaryDirectory() as d:
        use_tmp(Path(d))
        cfgmod.CONFIG_PATH.write_text(
            "; my notes\n\n[widget]\n; keep me\ntheme  =  dracula   \n"
            "\n; trailing note\n\n[network]\nofficial_limits = true\n")
        c = Config.load()
        assert c.theme == "dracula" and c.limits_enabled is True
        c.theme = "gruvbox"
        c.save()
        out = cfgmod.CONFIG_PATH.read_text()
        assert "; my notes" in out and "; keep me" in out and "; trailing note" in out
        assert "theme  =  gruvbox" in out, "surrounding whitespace should be kept"
        # keys the file never had get appended into their section, not dropped
        assert "opacity = " in out and "[refresh]" in out
        assert Config.load().theme == "gruvbox"


def test_missing_section_appended():
    out = _patch_ini("[widget]\ntheme = nord\n", {"sections": {"week": "false"}})
    assert "[sections]" in out and "week = false" in out
    assert "theme = nord" in out


def test_commented_key_is_not_matched():
    """A key that only appears commented out must be appended live, not edited
    inside the comment."""
    out = _patch_ini("[widget]\n; theme = nord\n", {"widget": {"theme": "paper"}})
    assert "; theme = nord" in out, "the comment itself must be untouched"
    assert "theme = paper" in out


for fn in (test_comments_survive_save, test_user_edits_and_layout_preserved,
           test_missing_section_appended, test_commented_key_is_not_matched):
    fn()
    print(f"ok  {fn.__name__}")
print("ALL CONFIG TESTS PASSED")
