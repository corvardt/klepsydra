"""Limit-fetch tests: the on-disk cache, and the widget's 429 handling.

The endpoint is shared with Claude Code's own polling and is rate limited per
account, so both halves exist to keep the widget from adding to a 429 storm.
The widget half needs GTK and is skipped where it is unavailable.
"""

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from klepsydra import limits as lim  # noqa: E402

PAYLOAD = {"five_hour": {"utilization": 42.0, "resets_at": "2026-08-27T04:00:00Z"}}


def _no_network(*_a, **_kw):
    raise AssertionError("network hit")


def test_fresh_cache_skips_the_request():
    lim._write_cache(PAYLOAD, time.time())
    real, urllib.request.urlopen = urllib.request.urlopen, _no_network
    try:
        r = lim.fetch_limits(max_age=60)
    finally:
        urllib.request.urlopen = real
    assert r.five_hour.utilization == 42.0
    assert r.five_hour.resets_at.hour == 4


def test_stale_cache_is_ignored():
    lim._write_cache(PAYLOAD, time.time() - 120)
    hits = []
    real, urllib.request.urlopen = urllib.request.urlopen, lambda *a, **k: hits.append(1)
    try:
        try:
            lim.fetch_limits(max_age=60)
        except Exception:
            pass
    finally:
        urllib.request.urlopen = real
    assert hits, "a stale cache must not suppress the request"


def test_unreadable_cache_is_not_fatal():
    lim.CACHE_PATH.write_text("{not json")
    assert lim._read_cache(60) is None


def test_backoff_doubles_and_keeps_the_last_good_numbers():
    try:
        from klepsydra.widget import (KlepsydraWindow as K, LIMITS_BACKOFF_S,
                                      LIMITS_BACKOFF_MAX_S)
    except (ImportError, ValueError):  # no GTK on this machine
        print("skip  backoff (GTK unavailable)")
        return
    from types import SimpleNamespace

    done = K._limits_done
    good = lim.Limits(lim.Bucket(50.0, None), None, None, None, time.time())

    def err(msg):
        return lim.Limits(None, None, None, None, time.time(), error=msg)

    w = SimpleNamespace(limits=None, _limits_inflight=True, _limits_backoff=0.0,
                        _backoff_s=0.0, _limits_error=None, _render=lambda: None)
    done(w, good)
    assert w.limits is good and w._limits_error is None

    done(w, err("rate limited"))
    assert w.limits is good, "a refusal must not wipe the numbers on screen"
    assert w._limits_error == "rate limited"
    assert w._backoff_s == LIMITS_BACKOFF_S

    done(w, err("rate limited"))
    assert w._backoff_s == 2 * LIMITS_BACKOFF_S
    for _ in range(10):
        done(w, err("rate limited"))
    assert w._backoff_s == LIMITS_BACKOFF_MAX_S

    done(w, good)
    assert w._backoff_s == 0.0 and w._limits_error is None

    # with no good data yet, the error is what the card shows
    w2 = SimpleNamespace(limits=None, _limits_inflight=True, _limits_backoff=0.0,
                         _backoff_s=0.0, _limits_error=None, _render=lambda: None)
    done(w2, err("network: timed out"))
    assert w2.limits.error == "network: timed out"

    # a poll during the backoff window must not start a request
    w2._limits_inflight = False
    w2._limits_backoff = time.monotonic() + 60
    K._poll_limits(w2)
    assert w2._limits_inflight is False


with tempfile.TemporaryDirectory() as tmp:
    lim.CACHE_PATH = Path(tmp) / "usage.json"
    for fn in (test_fresh_cache_skips_the_request, test_stale_cache_is_ignored,
               test_unreadable_cache_is_not_fatal,
               test_backoff_doubles_and_keeps_the_last_good_numbers):
        fn()
        print(f"ok  {fn.__name__}")
print("ALL LIMITS TESTS PASSED")
