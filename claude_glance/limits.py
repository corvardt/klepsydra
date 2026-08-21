"""claude-glance limits: OPT-IN fetch of official subscription utilization.

Only runs when launched with --limits. Makes exactly one kind of request:

    GET https://api.anthropic.com/api/oauth/usage

using the OAuth access token Claude Code already stores in
~/.claude/.credentials.json. Nothing else is contacted, nothing is sent
beyond that one Authorization header, and this module never writes anywhere.

We deliberately do NOT refresh the token (that would rotate credentials
Claude Code owns). If the token is expired we just report it and the widget
falls back to local estimates until you use Claude Code again.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ALLOWED_HOST = "api.anthropic.com"


@dataclass
class Bucket:
    utilization: float          # 0..100
    resets_at: datetime | None


@dataclass
class ExtraUsage:
    monthly_limit: float
    used_credits: float


@dataclass
class Limits:
    five_hour: Bucket | None
    seven_day: Bucket | None
    seven_day_opus: Bucket | None
    seven_day_sonnet: Bucket | None
    fetched_at: float
    error: str | None = None
    extra: ExtraUsage | None = None


def _credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _read_token() -> tuple[str | None, str | None]:
    """Returns (token, error). Read-only, never modifies the file."""
    path = _credentials_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, "no credentials file (API-key mode?)"
    except (json.JSONDecodeError, OSError) as e:
        return None, f"credentials unreadable: {e.__class__.__name__}"
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None, "no OAuth token (subscription login required)"
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)):
        exp_s = expires / 1000 if expires >= 1e11 else expires  # ms vs s
        if exp_s < time.time():
            return None, "token expired; open Claude Code to refresh"
    return token, None


def _parse_bucket(obj) -> Bucket | None:
    if not isinstance(obj, dict):
        return None
    util = obj.get("utilization")
    if util is None:
        return None
    resets = None
    raw = obj.get("resets_at")
    if isinstance(raw, str):
        try:
            resets = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return Bucket(utilization=float(util), resets_at=resets)


def fetch_limits(timeout: float = 10.0) -> Limits:
    token, err = _read_token()
    if err:
        return Limits(None, None, None, None, time.time(), error=err)

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-glance/1.0 (local desktop widget)",
    })
    # belt-and-braces: refuse to talk to anything but the allowed host
    if urllib.parse.urlparse(USAGE_URL).hostname != ALLOWED_HOST:  # pragma: no cover
        return Limits(None, None, None, None, time.time(), error="host not allowed")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = {401: "unauthorized (token invalid/expired)",
               403: "forbidden (not a subscription account?)",
               429: "rate limited"}.get(e.code, f"HTTP {e.code}")
        return Limits(None, None, None, None, time.time(), error=msg)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Limits(None, None, None, None, time.time(),
                      error=f"network: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
        return Limits(None, None, None, None, time.time(), error="bad response")

    extra = None
    eu = payload.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled"):
        try:
            extra = ExtraUsage(monthly_limit=float(eu.get("monthly_limit") or 0),
                               used_credits=float(eu.get("used_credits") or 0))
        except (TypeError, ValueError):
            extra = None

    return Limits(
        five_hour=_parse_bucket(payload.get("five_hour")),
        seven_day=_parse_bucket(payload.get("seven_day")),
        seven_day_opus=_parse_bucket(payload.get("seven_day_opus")),
        seven_day_sonnet=_parse_bucket(payload.get("seven_day_sonnet")),
        fetched_at=time.time(),
        extra=extra,
    )
