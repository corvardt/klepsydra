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
from functools import cached_property
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION_HOURS = 5  # Anthropic's rolling rate-limit window
CONTEXT_WINDOW = 200_000        # tokens; the default for older models
CONTEXT_WINDOW_1M = 1_000_000   # 1M-window models, and the [1m] beta suffix
# Model ids whose window is 1M without the [1m] suffix. Longest-prefix match.
CONTEXT_1M_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                       "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                       "claude-sonnet-4-6")

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
    "claude-opus-5-5":   (4.0, 5.00, 8.0, 0.20, 20.0),
    "claude-sonnet-4-5": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4":   (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-5":   (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-haiku-4-5":  (1.0, 1.25, 2.0, 0.10, 5.0),
    "claude-haiku-4":    (1.0, 1.25, 2.0, 0.10, 5.0),
    "claude-3-5-haiku":  (0.80, 1.00, 1.60, 0.08, 4.0),
    "claude-haiku-3-5":  (0.80, 1.00, 1.60, 0.08, 4.0),
    "claude-fable-5":    (10.0, 12.50, 20.0, 1.00, 50.0),
    "claude-fable-5-1":  (10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-mythos-5":   (10.0, 12.50, 20.0, 1.00, 50.0),
}
_FALLBACK_PRICE = (3.0, 3.75, 6.0, 0.30, 15.0)  # sonnet-class, for unknown ids

WEB_SEARCH_USD = 10.0 / 1000  # server-side web search, billed per request

# Live state, from the newest log lines. A pending tool call means its tool
# is running; the rest fall back to "writing" (the model's own work).
TOOL_STATES = {
    "Bash": "shell", "BashOutput": "shell", "KillShell": "shell",
    "Read": "reading", "Grep": "reading", "Glob": "reading", "LS": "reading",
    "NotebookRead": "reading",
    "Edit": "editing", "Write": "editing", "MultiEdit": "editing",
    "NotebookEdit": "editing",
    "WebSearch": "web", "WebFetch": "web",
    "Agent": "subagent", "Task": "subagent",
    "AskUserQuestion": "asking", "ExitPlanMode": "asking",
}
# ponytail: permission prompts are not logged, so a normally instant tool
# still pending after PERMISSION_S is taken to be waiting for approval
QUICK_TOOLS = ("reading", "editing")
PERMISSION_S = 3.0
# with several sessions mid-turn, the one ranked first here wins
LIVE_ORDER = ("asking", "error", "shell", "subagent", "web", "editing",
              "reading", "writing", "thinking")
LIVE_GUARD_S = 600.0  # a turn silent this long (a crash, a kill) is over
# Nothing is logged mid-stream, so the gap before a message's first line is
# thinking and writing alike. Thinking is the smaller part (a median 15% of
# output tokens, and 4 gaps in 10 hold no thinking block at all), so only the
# first moments of a gap count as thinking; the rest is writing. Gaps run a
# median 3.5s, and this cap leaves thinking about a quarter of that time.
THINK_S = 1.5
# ponytail: one multiplier, Opus 5 / 5.5 fast-mode rates; per-model table if
# another model's fast premium differs
FAST_MULTIPLIER = 2.0


def price_for(model: str) -> tuple[float, float, float, float, float]:
    best = ""
    for prefix in PRICING:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return PRICING[best] if best else _FALLBACK_PRICE


def short_model(model: str) -> str:
    """claude-opus-4-5-20251101 -> opus 4.5"""
    parts = model.replace("claude-", "").split("-")
    name = next((p for p in parts if not p.isdigit()), model)
    version = ".".join(p for p in parts if p.isdigit() and len(p) < 4)
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
    branch: str = ""        # git branch the turn ran on (from gitBranch)
    session: str = ""       # sessionId
    bg: bool = False        # True for background jobs; absent sessionKind = interactive
    web_search: int = 0     # server-side web searches (billed per request)
    web_fetch: int = 0      # server-side web fetches (not billed per request)
    sidechain: bool = False  # subagent turn: shares the sessionId, own context
    fast: bool = False       # fast mode, billed at FAST_MULTIPLIER
    thinking: int = 0        # share of `out` spent thinking

    @property
    def total_tokens(self) -> int:
        return self.inp + self.out + self.cache_w5 + self.cache_w1h + self.cache_r

    @property
    def context_tokens(self) -> int:
        """Prompt size this turn was billed for, which is what the session's
        context window holds. Output is excluded: it only enters the window on
        the *next* turn, which then reports it as input or cache."""
        return self.inp + self.cache_w5 + self.cache_w1h + self.cache_r

    @property
    def context_limit(self) -> int:
        if "[1m]" in self.model or self.model.startswith(CONTEXT_1M_PREFIXES):
            return CONTEXT_WINDOW_1M
        return CONTEXT_WINDOW

    @cached_property
    def cost(self) -> float:
        pi, pw5, pw1h, pr, po = price_for(self.model)
        tokens = (self.inp * pi + self.cache_w5 * pw5 + self.cache_w1h * pw1h
                  + self.cache_r * pr + self.out * po) / 1_000_000
        if self.fast:
            tokens *= FAST_MULTIPLIER
        return tokens + self.web_search * WEB_SEARCH_USD


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
        self.titles: dict[str, str] = {}  # sessionId -> Claude Code's ai-title
        # (rateLimitType, resets_at) -> first time Claude Code was refused
        self.rejections: dict[tuple[str, datetime], datetime] = {}
        # sessionId -> (state, epoch seconds) of its main thread, and its tool
        # calls still waiting on a result: tool_use id -> (state, epoch seconds)
        self._phase: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, dict[str, tuple[str, float]]] = {}
        # one-off events: ("interrupted" | "queued" | "error" | "edit", epoch seconds)
        self.moments: list[tuple[str, float]] = []
        self.last_activity = 0.0

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
        if b'"ai-title"' in raw:  # session title, rewritten as the topic shifts
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
            sid, title = obj.get("sessionId"), obj.get("aiTitle")
            if obj.get("type") == "ai-title" and sid and title:
                self.titles[str(sid)] = str(title)
            return False
        if b'"turn_duration"' in raw or b'"user"' in raw or b'"queue-operation"' in raw:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
            if obj.get("type") in ("user", "system", "queue-operation"):
                self._note_event(obj)
                return False
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            return False
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if obj.get("type") != "assistant":
            return False
        self._note_event(obj)  # every block line, before the dedup below
        quota = obj.get("quotaLimits")
        if isinstance(quota, dict) and quota.get("status") == "rejected":
            self._note_rejection(quota, obj.get("timestamp", ""))
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
        # 'HEAD' is what a detached checkout reports; it names no branch, so it
        # would otherwise pool every worktree's spend under one meaningless row.
        branch = obj.get("gitBranch") or ""
        if branch == "HEAD":
            branch = ""
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd.strip("/"):
            project = cwd.rstrip("/").rsplit("/", 1)[-1]
        tools = usage.get("server_tool_use") or {}
        details = usage.get("output_tokens_details") or {}
        self.entries.append(Entry(
            ts=ts,
            model=model,
            project=project,
            branch=branch,
            session=str(obj.get("sessionId", "")),
            bg=obj.get("sessionKind") == "bg",
            inp=usage.get("input_tokens", 0) or 0,
            out=usage.get("output_tokens", 0) or 0,
            cache_w5=int(w5),
            cache_w1h=int(w1h),
            cache_r=usage.get("cache_read_input_tokens", 0) or 0,
            web_search=tools.get("web_search_requests", 0) or 0,
            web_fetch=tools.get("web_fetch_requests", 0) or 0,
            sidechain=bool(obj.get("isSidechain")),
            fast=usage.get("speed") == "fast",
            thinking=details.get("thinking_tokens", 0) or 0,
        ))
        return True

    def _note_event(self, obj: dict) -> None:
        """Advance a session's live state by one log line. Each content block
        is its own line, written when it completes, so the newest line says
        what is under way: after a prompt or a tool result the model is
        thinking; after a thinking or text block it is writing; a tool call
        runs until its result comes back. turn_duration or an interruption
        ends the turn. Local commands (/model and the like) never start one."""
        ts = _parse_ts(obj.get("timestamp", ""))
        sid = str(obj.get("sessionId", ""))
        if ts is None or not sid or obj.get("isMeta"):
            return
        at = ts.timestamp()
        self.last_activity = max(self.last_activity, at)
        kind = obj.get("type")
        if obj.get("isSidechain"):  # a subagent: activity, not the main thread's state
            return
        pending = self._pending.setdefault(sid, {})
        if kind == "queue-operation":
            if obj.get("operation") in (None, "enqueue"):
                self.moments.append(("queued", at))
            return
        if kind == "system":
            if obj.get("subtype") == "turn_duration":
                self._set(sid, "idle", at)
                pending.clear()
            return
        content = (obj.get("message") or {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            if obj.get("isApiErrorMessage"):
                self._set(sid, "error", at)
                self.moments.append(("error", at))
            for b in blocks:
                if b.get("type") == "tool_use":
                    state = TOOL_STATES.get(str(b.get("name")), "writing")
                    pending[str(b.get("id"))] = (state, at)
                    if state == "editing":  # over too fast to be seen as a state
                        self.moments.append(("edit", at))
                elif b.get("type") in ("thinking", "redacted_thinking", "text"):
                    self._set(sid, "writing", at)
            return
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            for b in results:
                pending.pop(str(b.get("tool_use_id")), None)
            self._set(sid, "thinking", at)
            return
        first = content if isinstance(content, str) else next(
            (b.get("text", "") for b in blocks if b.get("type") == "text"), "")
        if first.startswith("[Request interrupted"):
            self._set(sid, "idle", at)
            pending.clear()
            self.moments.append(("interrupted", at))
        elif not first.startswith("<"):
            self._set(sid, "thinking", at)
            pending.clear()

    def _set(self, sid: str, state: str, at: float) -> None:
        if at >= self._phase.get(sid, ("", 0.0))[1]:
            self._phase[sid] = (state, at)

    def live(self, now: datetime | None = None) -> str:
        """What Claude Code is doing right now: the state of whichever
        mid-turn session ranks first in LIVE_ORDER, or "idle"."""
        t = (now or datetime.now(timezone.utc)).timestamp()
        best = len(LIVE_ORDER)
        for sid in set(self._phase) | set(self._pending):
            state, at = self._phase.get(sid, ("idle", 0.0))
            pending = self._pending.get(sid)
            if pending:
                state, at = max(pending.values(), key=lambda v: v[1])
                if state in QUICK_TOOLS and t - at > PERMISSION_S:
                    state = "asking"
            elif state == "thinking" and t - at > THINK_S:
                state = "writing"
            if state != "idle" and t - at < LIVE_GUARD_S:
                best = min(best, LIVE_ORDER.index(state))
        return LIVE_ORDER[best] if best < len(LIVE_ORDER) else "idle"

    def _note_rejection(self, quota: dict, raw_ts: str) -> None:
        """A limit refusal: what was spent in that window by then is ~100%."""
        ts = _parse_ts(raw_ts)
        kind, resets = quota.get("rateLimitType"), quota.get("resetsAt")
        if ts is None or not kind or not isinstance(resets, (int, float)):
            return
        key = (str(kind), datetime.fromtimestamp(resets, timezone.utc))
        self.rejections[key] = min(ts, self.rejections.get(key, ts))

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

    def contexts(self, minutes: float = 30.0,
                 now: datetime | None = None) -> list[tuple[str, int, int]]:
        """(label, tokens, limit) for every session that spoke in the last
        `minutes`, fullest first.

        Context is a level, not a running total: every turn re-sends the whole
        conversation, so the newest turn's prompt *is* what the window holds,
        and it falls back on its own after a /compact. Subagent turns are
        skipped; they share the parent's sessionId but have their own, much
        smaller, context."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=minutes)
        last: dict[str, Entry] = {}
        for e in reversed(self.entries):  # ts-sorted; newest wins, stop early
            if e.ts < cutoff:
                break
            if e.session and not e.sidechain:
                last.setdefault(e.session, e)
        counts: dict[str, int] = {}
        for e in last.values():
            counts[e.project] = counts.get(e.project, 0) + 1
        rows = []
        for sid, e in last.items():
            # two terminals in one repo would otherwise draw two identical bars
            label = self.titles.get(sid) or e.project or sid[:6]
            if sid not in self.titles and e.project and counts[e.project] > 1:
                label = f"{label} {sid[:4]}"
            rows.append((label, e.context_tokens, e.context_limit))
        return sorted(rows, key=lambda r: -r[1] / r[2])

    def cost_between(self, start: datetime, end: datetime) -> float:
        total = 0.0
        for e in reversed(self.entries):  # ts-sorted; stop early
            if e.ts < start:
                break
            if e.ts < end:
                total += e.cost
        return total

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
            "prompt": sum(e.context_tokens for e in entries),
            "cache_r": sum(e.cache_r for e in entries),
            "out": sum(e.out for e in entries),
            "thinking": sum(e.thinking for e in entries),
            "cost": sum(e.cost for e in entries),
            "bg_cost": sum(e.cost for e in entries if e.bg),
            "web_search": sum(e.web_search for e in entries),
            "web_fetch": sum(e.web_fetch for e in entries),
            "by_model": dict(sorted(by_model.items(),
                                    key=lambda kv: -kv[1]["cost"])),
        }

    def recent_rate(self, minutes: float = 10.0,
                    now: datetime | None = None) -> tuple[float, float]:
        """(tokens/min, usd/min) over the last `minutes`.

        A block average would smear idle gaps across the whole window; this is
        what you are burning *right now*, which is what the footer should show
        and what `eta_to_full` should extrapolate from."""
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
    def top_by(entries: list[Entry], attr: str, n: int = 2) -> list[tuple[str, float]]:
        """Costliest values of `attr` (project, branch), dearest first."""
        costs: dict[str, float] = {}
        for e in entries:
            v = getattr(e, attr)
            if v:
                costs[v] = costs.get(v, 0.0) + e.cost
        return sorted(costs.items(), key=lambda kv: -kv[1])[:n]

    def capacity_estimate(self, days: int = 30,
                          now: datetime | None = None) -> float:
        """Your busiest finished 5h block, in USD, over the last `days`.

        Used as the local stand-in for the real 5h allowance when official
        limits aren't available. Cost rather than raw tokens because the real
        allowance weights models very differently (an opus token costs far more
        of it than a haiku one), and a ceiling rather than a median because a
        median makes every ordinary block read as ~100%.

        A logged 5h refusal beats both: the spend in that window up to the
        refusal is a measured 100%, so the latest one in range wins."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        hits = [(ts, resets) for (kind, resets), ts in self.rejections.items()
                if kind == "five_hour" and ts >= cutoff]
        if hits:
            ts, resets = max(hits)
            spent = self.cost_between(resets - timedelta(hours=SESSION_HOURS), ts)
            if spent > 0:
                return spent
        done = [b.cost for b in self.blocks()
                if b.end <= now and b.start >= cutoff and b.cost > 0]
        return max(done) if done else 0.0

    def week_grid(self, now: datetime | None = None) -> list[list[float]]:
        """Cost per local hour for the last 7 days: 7 rows of 24, today last."""
        local = (now or datetime.now(timezone.utc)).astimezone()
        start = (local - timedelta(days=6)).replace(hour=0, minute=0,
                                                    second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc)
        grid = [[0.0] * 24 for _ in range(7)]
        for e in reversed(self.entries):
            if e.ts < start_utc:
                break
            lt = e.ts.astimezone()
            day = (lt.date() - start.date()).days
            if 0 <= day < 7:
                grid[day][lt.hour] += e.cost
        return grid


def fmt_tokens(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))
