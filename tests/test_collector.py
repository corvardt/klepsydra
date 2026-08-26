"""Synthetic-fixture tests for the collector. Run: python3 tests/test_collector.py"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent))
from klepsydra import collector as col  # noqa: E402


def entry_line(ts, model, mid, rid, inp=100, out=50, cw5=1000, cw1h=0, cr=5000,
               sid="s1", kind=None, branch=None):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "requestId": rid,
        "sessionId": sid, "uuid": "u-" + mid,
        "sessionKind": kind, "gitBranch": branch,
        "message": {
            "id": mid, "model": model, "role": "assistant",
            "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_input_tokens": cw5 + cw1h,
                "cache_read_input_tokens": cr,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw5,
                    "ephemeral_1h_input_tokens": cw1h,
                },
            },
        },
    })


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    now = datetime(2026, 8, 17, 0, 30, tzinfo=timezone.utc)
    tmp = tempfile.mkdtemp()
    root = Path(tmp) / ".claude" / "projects" / "-home-user-proj"
    root.mkdir(parents=True)
    f = root / "session1.jsonl"

    lines = []
    # Block 1: two entries at 09:37 and 10:10 (should floor to 09:00, end 14:00)
    t1 = now.replace(hour=9, minute=37) - timedelta(days=1)
    t2 = now.replace(hour=10, minute=10) - timedelta(days=1)
    lines.append(entry_line(iso(t1), "claude-opus-4-5-20251101", "msg_1", "req_1"))
    lines.append(entry_line(iso(t2), "claude-sonnet-4-5-20250929", "msg_2", "req_2"))
    # duplicate streamed line of msg_2 (same message.id + requestId) -> must dedupe
    lines.append(entry_line(iso(t2), "claude-sonnet-4-5-20250929", "msg_2", "req_2"))
    # entry with no requestId, duplicated -> dedupe on message.id alone
    obj = json.loads(entry_line(iso(t2), "claude-haiku-4-5-20251001", "msg_3", None))
    del obj["requestId"]
    lines.append(json.dumps(obj))
    lines.append(json.dumps(obj))
    # Block 2 after >5h gap: today 00:05 (active if now=00:30)
    t3 = now.replace(hour=0, minute=5)
    lines.append(entry_line(iso(t3), "claude-opus-4-5-20251101", "msg_4", "req_4",
                            inp=200, out=100, cw5=0, cw1h=2000, cr=0))
    # non-assistant noise
    lines.append(json.dumps({"type": "user", "timestamp": iso(t3), "message": {"role": "user"}}))
    lines.append(json.dumps({"type": "summary", "summary": "compaction"}))

    f.write_text("\n".join(lines) + "\n")

    os.environ["CLAUDE_CONFIG_DIR"] = str(Path(tmp) / ".claude")
    # neutralize real home dirs
    os.environ["HOME"] = tmp

    c = col.Collector()
    added = c.refresh()
    assert added == 4, f"expected 4 unique entries, got {added}"

    blocks = c.blocks()
    assert len(blocks) == 2, f"expected 2 blocks, got {len(blocks)}"
    b1, b2 = blocks
    assert b1.start.hour == 9 and b1.start.minute == 0, b1.start
    assert (b1.end - b1.start) == timedelta(hours=5)
    assert len(b1.entries) == 3
    assert b2.start.hour == 0 and len(b2.entries) == 1

    active = c.active_block(now)
    assert active is not None and active.start == b2.start, "block 2 should be active at 00:30"

    # cost check: msg_1 opus-4-5: (100*5 + 1000*6.25 + 5000*0.5 + 50*25)/1e6
    e1 = c.entries[0]
    expected = (100 * 5 + 1000 * 6.25 + 5000 * 0.5 + 50 * 25) / 1e6
    assert abs(e1.cost - expected) < 1e-9, (e1.cost, expected)

    # msg_4 has 1h cache write: (200*5 + 2000*10 + 100*25)/1e6
    e4 = c.entries[-1]
    expected4 = (200 * 5 + 2000 * 10 + 100 * 25) / 1e6
    assert abs(e4.cost - expected4) < 1e-9, (e4.cost, expected4)

    # today = entries since local midnight; with UTC-ish env, msg_4 qualifies
    today = c.today(now)
    assert any(e.ts == t3 for e in today)

    s = c.summarize(c.entries)
    assert s["cost"] > 0 and s["tokens"] > 0
    assert "opus 4.5" in s["by_model"], s["by_model"].keys()

    # incremental append: add one more line, refresh must pick up exactly 1
    with f.open("a") as fh:
        fh.write(entry_line(iso(t3 + timedelta(minutes=3)),
                            "claude-opus-4-5-20251101", "msg_5", "req_5") + "\n")
    assert c.refresh() == 1

    # recent-rate sanity: msg_4 lands 25 min before `now`, inside the window
    tok_rate, cost_rate = c.recent_rate(minutes=60, now=now)
    assert tok_rate > 0 and cost_rate > 0

    # partial trailing line must not crash or be consumed
    with f.open("a") as fh:
        fh.write('{"type": "assistant", "message": {"id": "msg_6"')
    assert c.refresh() == 0

    # unknown model falls back gracefully
    assert col.price_for("claude-future-9") == col._FALLBACK_PRICE
    assert col.price_for("claude-opus-4-5-20251101") == col.PRICING["claude-opus-4-5"]
    assert col.price_for("claude-opus-4-1-20250805") == col.PRICING["claude-opus-4-1"]

    assert col.short_model("claude-opus-4-5-20251101") == "opus 4.5"
    assert col.fmt_tokens(12_400) == "12.4k"
    assert col.fmt_tokens(4_100_000) == "4.1M"

    # --- v2 features -------------------------------------------------------
    # project attribution from dir name '-home-user-proj' -> 'proj'
    assert all(e.project == "proj" for e in c.entries), \
        {e.project for e in c.entries}

    # synthetic pseudo-models are skipped at ingest
    with f.open("a") as fh:
        fh.write("\n" + entry_line(iso(t3 + timedelta(minutes=4)),
                                   "<synthetic>", "msg_syn", "req_syn") + "\n")
    assert c.refresh() == 0, "synthetic entries must be ignored"

    # second project dir
    root2 = root.parent / "-home-user-webapp"
    root2.mkdir()
    (root2 / "s2.jsonl").write_text(
        entry_line(iso(t3 + timedelta(minutes=5)),
                   "claude-sonnet-4-5-20250929", "msg_7", "req_7", sid="s2") + "\n")
    assert c.refresh() == 1
    today = c.today(now)
    tops = c.top_by(today, "project")
    assert len(tops) == 2 and tops[0][0] in ("proj", "webapp"), tops

    # month includes everything from this month
    assert len(c.month(now)) == len(c.entries)

    # --- v3: background split and branch attribution ------------------------
    (root / "s3.jsonl").write_text("\n".join([
        entry_line(iso(t3 + timedelta(minutes=6)), "claude-opus-4-5-20251101",
                   "msg_8", "req_8", sid="s3", kind="bg", branch="main"),
        entry_line(iso(t3 + timedelta(minutes=7)), "claude-opus-4-5-20251101",
                   "msg_9", "req_9", sid="s3", branch="feature-x"),
        # detached checkout: 'HEAD' names no branch and must not become a row
        entry_line(iso(t3 + timedelta(minutes=8)), "claude-opus-4-5-20251101",
                   "msg_10", "req_10", sid="s3", kind="bg", branch="HEAD"),
    ]) + "\n")
    assert c.refresh() == 3
    today = c.today(now)

    bgs = [e for e in today if e.bg]
    assert len(bgs) == 2, len(bgs)          # msg_8 and msg_10; absent kind = interactive
    assert all(not e.bg for e in today if e.session in ("s1", "s2"))

    branches = dict(c.top_by(today, "branch", n=9))
    assert set(branches) == {"main", "feature-x"}, branches
    assert "HEAD" not in branches

    sb = c.summarize(today)
    assert 0 < sb["bg_cost"] < sb["cost"], (sb["bg_cost"], sb["cost"])
    # bg_cost counts only bg entries, so the two of them and nothing else
    assert abs(sb["bg_cost"] - sum(e.cost for e in bgs)) < 1e-12

    # project_name edge cases
    from pathlib import Path as P
    assert col.project_name(P("/r/-home-user-my-app/x.jsonl"), P("/r")) == "app"
    assert col.project_name(P("/r/plain/x.jsonl"), P("/r")) == "plain"

    print("ALL COLLECTOR TESTS PASSED")


if __name__ == "__main__":
    main()
