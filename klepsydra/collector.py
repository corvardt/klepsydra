"""klepsydra collector: reads Claude Code's local JSONL logs.

100% local. No network. Reads only:
  ~/.claude/projects/**/*.jsonl
  ~/.config/claude/projects/**/*.jsonl
  $CLAUDE_CONFIG_DIR/projects/**/*.jsonl   (if set)

Every function here is small on purpose: audit it in one sitting.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION_HOURS = 5  # Anthropic's rolling rate-limit window

# ---------------------------------------------------------------------------
# Pricing (USD per million tokens): input, cache_write_5m, cache_write_1h,
# cache_read, output.  Matched by longest prefix on message.model.
# Source: docs.claude.com pricing page, Aug 2026.
# ---------------------------------------------------------------------------
PRICING: dict[str, tuple[float, float, float, float, float]] = {
    "claude-opus-4-1":   (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-4-2":   (15.0, 18.75, 30.0, 1.50, 75.0),  # safety net
    "claude-opus-4-5":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8":   (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4":     (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-5":     (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-sonnet-4-5": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4":   (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-5":   (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-haiku-4-5":  (1.0, 1.25, 2.0, 0.10, 5.0),
    "claude-haiku-4":    (1.0, 1.25, 2.0, 0.10, 5.0),
    "claude-3-5-haiku":  (0.80, 1.00, 1.60, 0.08, 4.0),
    "claude-haiku-3-5":  (0.80, 1.00, 1.60, 0.08, 4.0),
    "claude-fable-5":    (10.0, 12.50, 20.0, 1.00, 50.0),
    "claude-mythos-5":   (10.0, 12.50, 20.0, 1.00, 50.0),
}
_FALLBACK_PRICE = (3.0, 3.75, 6.0, 0.30, 15.0)  # sonnet-class, for unknown ids

WEB_SEARCH_USD = 10.0 / 1000  # server-side web search, billed per request


def price_for(model: str) -> tuple[float, float, float, float, float]:
    best = ""
    for prefix in PRICING:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return PRICING[best] if best else _FALLBACK_PRICE


def short_model(model: str) -> str:
    """claude-opus-4-5-20251101 -> opus 4.5"""
    parts = model.replace("claude-", "").split("-")
    name = parts[0] if parts else model
    version = ".".join(p for p in parts[1:] if p.isdigit() and len(p) < 4)
    return f"{name} {version}".strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    ts: datetime            # UTC
    model: str
    inp: int
    out: int
    cache_w5: int           # 5-minute-TTL cache writes
    cache_w1h: int          # 1-hour-TTL cache writes
    cache_r: int
    project: str = ""       # friendly name of the project (from cwd when present)
    session: str = ""       # sessionId
    sidechain: bool = False  # True for subagent (Task tool) turns
    thinking: int = 0       # extended-thinking tokens, a subset of `out`
    web_search: int = 0     # server-side web searches (billed per request)
    web_fetch: int = 0      # server-side web fetches (not billed per request)

    @property
    def total_tokens(self) -> int:
        return self.inp + self.out + self.cache_w5 + self.cache_w1h + self.cache_r

    @property
    def cost(self) -> float:
        pi, pw5, pw1h, pr, po = price_for(self.model)
        return ((self.inp * pi + self.cache_w5 * pw5 + self.cache_w1h * pw1h
                 + self.cache_r * pr + self.out * po) / 1_000_000
                + self.web_search * WEB_SEARCH_USD)


@dataclass
class Block:
    start: datetime
    end: datetime
    entries: list[Entry] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(e.total_tokens for e in self.entries)

    @property
    def cost(self) -> float:
        return sum(e.cost for e in self.entries)

    @property
    def models(self) -> list[str]:
        """Distinct short model names used in this block, busiest first."""
        by: dict[str, float] = {}
        for e in self.entries:
            by[short_model(e.model)] = by.get(short_model(e.model), 0.0) + e.cost
        return [k for k, _ in sorted(by.items(), key=lambda kv: -kv[1])]


# ---------------------------------------------------------------------------
# Log discovery + incremental parsing
# ---------------------------------------------------------------------------
def log_roots() -> list[Path]:
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = [Path(env)] if env else []
    candidates += [Path.home() / ".claude", Path.home() / ".config" / "claude"]
    seen: set[Path] = set()
    for c in candidates:
        p = c / "projects"
        if not p.is_dir():
            continue
        real = p.resolve()  # ~/.config/claude is often a symlink to ~/.claude
        if real in seen:
            continue
        seen.add(real)
        roots.append(p)
    return roots


def project_name(path: Path, root: Path) -> str:
    """Fallback name for logs with no `cwd` field. Claude Code encodes the
    project path as a dir name like '-home-user-my-app'; '-' encodes '/', so
    the true name is ambiguous and the last token is only an approximation.
    Lines that carry `cwd` are named exactly; see `_ingest_line`."""
    try:
        encoded = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return ""
    token = encoded.rstrip("-").rsplit("-", 1)[-1]
    return token or encoded


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


class Collector:
    """Incrementally tails all Claude Code JSONL logs.

    Keeps per-file byte offsets so refreshes only read appended lines.
    A file whose size shrank (rotation/rewrite) is re-read from zero.
    """

    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}
        self._seen: set[str] = set()
        self._blocks: list[Block] | None = None
        self.entries: list[Entry] = []

    def refresh(self) -> int:
        """Scan for new lines. Returns number of new entries ingested."""
        added = 0
        for root in log_roots():
            for path in root.rglob("*.jsonl"):
                try:
                    added += self._ingest(path, project_name(path, root))
                except OSError:
                    continue
        if added:
            self.entries.sort(key=lambda e: e.ts)
            self._blocks = None  # invalidate the cached block layout
        return added

    def _ingest(self, path: Path, project: str = "") -> int:
        size = path.stat().st_size
        offset = self._offsets.get(path, 0)
        if size < offset:
            offset = 0
        if size == offset:
            return 0
        added = 0
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
            # only consume complete lines; leave a trailing partial for next pass
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                return 0
            self._offsets[path] = offset + last_nl + 1
            for raw in data[: last_nl + 1].splitlines():
                if self._ingest_line(raw, project):
                    added += 1
        return added

    def _ingest_line(self, raw: bytes, project: str = "") -> bool:
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            return False
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if obj.get("type") != "assistant":
            return False
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            return False
        # dedup: message.id + requestId, falling back to message.id alone
        mid = msg.get("id")
        if mid is None:
            return False
        rid = obj.get("requestId")
        key = f"{mid}:{rid}" if rid is not None else str(mid)
        if key in self._seen:
            return False
        ts = _parse_ts(obj.get("timestamp", ""))
        if ts is None:
            return False
        self._seen.add(key)
        cc = usage.get("cache_creation") or {}
        w5 = cc.get("ephemeral_5m_input_tokens")
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if w5 is None:  # older logs: only the aggregate field exists
            w5 = usage.get("cache_creation_input_tokens", 0) or 0
            w1h = 0
        model = msg.get("model", "unknown")
        if model.startswith("<"):  # "<synthetic>" internal zero-cost entries
            return False
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd.strip("/"):
            project = cwd.rstrip("/").rsplit("/", 1)[-1]
        details = usage.get("output_tokens_details") or {}
        tools = usage.get("server_tool_use") or {}
        self.entries.append(Entry(
            ts=ts,
            model=model,
            project=project,
            session=str(obj.get("sessionId", "")),
            sidechain=bool(obj.get("isSidechain")),
            inp=usage.get("input_tokens", 0) or 0,
            out=usage.get("output_tokens", 0) or 0,
            cache_w5=int(w5),
            cache_w1h=int(w1h),
            cache_r=usage.get("cache_read_input_tokens", 0) or 0,
            thinking=details.get("thinking_tokens", 0) or 0,
            web_search=tools.get("web_search_requests", 0) or 0,
            web_fetch=tools.get("web_fetch_requests", 0) or 0,
        ))
        return True

    # -- aggregations -------------------------------------------------------
    def blocks(self) -> list[Block]:
        """ccusage-style 5h session blocks: start floored to the hour (UTC),
        new block when outside the window or after a >=5h gap.

        Cached; `refresh` drops the cache when it ingests anything, so the
        1-second UI clock can call this without re-walking every entry."""
        if self._blocks is not None:
            return self._blocks
        blocks: list[Block] = []
        cur: Block | None = None
        prev_ts: datetime | None = None
        for e in self.entries:
            if (cur is None or e.ts >= cur.end
                    or (prev_ts and e.ts - prev_ts >= timedelta(hours=SESSION_HOURS))):
                start = e.ts.replace(minute=0, second=0, microsecond=0)
                cur = Block(start=start, end=start + timedelta(hours=SESSION_HOURS))
                blocks.append(cur)
            cur.entries.append(e)
            prev_ts = e.ts
        self._blocks = blocks
        return blocks

    def active_block(self, now: datetime | None = None) -> Block | None:
        now = now or datetime.now(timezone.utc)
        blocks = self.blocks()
        if blocks and blocks[-1].start <= now < blocks[-1].end:
            return blocks[-1]
        return None

    def today(self, now: datetime | None = None) -> list[Entry]:
        now = (now or datetime.now(timezone.utc)).astimezone()  # local day
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc)
        return [e for e in self.entries if e.ts >= start_utc]

    def summarize(self, entries: list[Entry]) -> dict:
        by_model: dict[str, dict] = {}
        for e in entries:
            m = by_model.setdefault(short_model(e.model),
                                    {"tokens": 0, "cost": 0.0, "requests": 0})
            m["tokens"] += e.total_tokens
            m["cost"] += e.cost
            m["requests"] += 1
        return {
            "tokens": sum(e.total_tokens for e in entries),
            "cost": sum(e.cost for e in entries),
            "requests": len(entries),
            "output": sum(e.out for e in entries),
            "thinking": sum(e.thinking for e in entries),
            "web_search": sum(e.web_search for e in entries),
            "web_fetch": sum(e.web_fetch for e in entries),
            "agent_tokens": sum(e.total_tokens for e in entries if e.sidechain),
            "by_model": dict(sorted(by_model.items(),
                                    key=lambda kv: -kv[1]["cost"])),
        }

    def burn_rate(self, block: Block, now: datetime | None = None) -> float:
        """Tokens per minute averaged across the whole block so far."""
        if not block.entries:
            return 0.0
        now = now or datetime.now(timezone.utc)
        first = block.entries[0].ts
        minutes = max((min(now, block.end) - first).total_seconds() / 60, 1.0)
        return block.tokens / minutes

    def recent_rate(self, minutes: float = 10.0,
                    now: datetime | None = None) -> tuple[float, float]:
        """(tokens/min, usd/min) over the last `minutes`.

        The block average above smears idle gaps across the whole window; this
        is what you are burning *right now*, which is what a live widget should
        show and what the reset-time projection should extrapolate from."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=minutes)
        recent = []
        for e in reversed(self.entries):  # entries are ts-sorted; stop early
            if e.ts < cutoff:
                break
            recent.append(e)
        if not recent:
            return 0.0, 0.0
        return (sum(e.total_tokens for e in recent) / minutes,
                sum(e.cost for e in recent) / minutes)

    def month(self, now: datetime | None = None) -> list[Entry]:
        now = (now or datetime.now(timezone.utc)).astimezone()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc)
        return [e for e in self.entries if e.ts >= start_utc]

    @staticmethod
    def cache_hit_rate(entries: list[Entry]) -> float | None:
        """cache reads as a share of all prompt-side tokens (0..100)."""
        reads = sum(e.cache_r for e in entries)
        prompt = sum(e.inp + e.cache_w5 + e.cache_w1h + e.cache_r for e in entries)
        if prompt == 0:
            return None
        return reads / prompt * 100

    def projection(self, block: Block, now: datetime | None = None) -> tuple[int, float]:
        """(projected_tokens, projected_cost) for the block by its reset time,
        extrapolating the *recent* rate rather than the block average."""
        now = now or datetime.now(timezone.utc)
        if not block.entries:
            return 0, 0.0
        remaining_min = max((block.end - now).total_seconds() / 60, 0.0)
        tok_rate, cost_rate = self.recent_rate(now=now)
        return (int(block.tokens + tok_rate * remaining_min),
                block.cost + cost_rate * remaining_min)

    def eta_to_full(self, block: Block, capacity: float,
                    now: datetime | None = None) -> datetime | None:
        """When the block would reach `capacity` USD at the recent rate.

        None if idle, already over, or the reset lands first."""
        now = now or datetime.now(timezone.utc)
        _, cost_rate = self.recent_rate(now=now)
        remaining = capacity - block.cost
        if cost_rate <= 0 or remaining <= 0:
            return None
        hit = now + timedelta(minutes=remaining / cost_rate)
        return hit if hit < block.end else None

    @staticmethod
    def sessions_count(entries: list[Entry]) -> int:
        return len({e.session for e in entries if e.session})

    @staticmethod
    def top_projects(entries: list[Entry], n: int = 2) -> list[tuple[str, float]]:
        costs: dict[str, float] = {}
        for e in entries:
            if e.project:
                costs[e.project] = costs.get(e.project, 0.0) + e.cost
        return sorted(costs.items(), key=lambda kv: -kv[1])[:n]

    def last_activity(self) -> datetime | None:
        return self.entries[-1].ts if self.entries else None

    def capacity_estimate(self, days: int = 30,
                          now: datetime | None = None) -> float:
        """Your busiest finished 5h block, in USD, over the last `days`.

        Used as the local stand-in for the real 5h allowance when official
        limits aren't available. Cost rather than raw tokens because the real
        allowance weights models very differently (an opus token costs far more
        of it than a haiku one), and a ceiling rather than a median because a
        median makes every ordinary block read as ~100%."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        done = [b.cost for b in self.blocks()
                if b.end <= now and b.start >= cutoff and b.cost > 0]
        return max(done) if done else 0.0

    def hourly(self, hours: int = 12, now: datetime | None = None) -> list[float]:
        """Cost per hour for the last `hours`, oldest first, sparkline data."""
        now = now or datetime.now(timezone.utc)
        top = now.replace(minute=0, second=0, microsecond=0)
        buckets = [0.0] * hours
        start = top - timedelta(hours=hours - 1)
        for e in reversed(self.entries):
            if e.ts < start:
                break
            idx = int((e.ts - start).total_seconds() // 3600)
            if 0 <= idx < hours:
                buckets[idx] += e.cost
        return buckets


_SPARK = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Unicode bar chart; blank for an empty hour so idle gaps stay visible."""
    peak = max(values, default=0.0)
    if peak <= 0:
        return _SPARK[0] * len(values)
    out = []
    for v in values:
        if v <= 0:
            out.append(_SPARK[0])
        else:  # 1..8, so any activity at all is drawn
            out.append(_SPARK[max(1, round(v / peak * 8))])
    return "".join(out)


def fmt_tokens(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))
