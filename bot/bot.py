#!/usr/bin/env python3
"""Compare bench Mihomo instances. Telegram Bot API goes through prod proxy only."""

from __future__ import annotations

import asyncio
import html
import math
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app_version import app_build_identity
from downtime import downtime_ticks

PROBE_INTERVAL_SEC = 1
CURL_MAX_TIME_SEC = 8
PROBE_WAIT_SEC = CURL_MAX_TIME_SEC + 2
TELEGRAM_POLL_TIMEOUT = 50
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
SUCCESS_HTTP = 404
TELEGRAM_TEXT_LIMIT = 4000
# Typical phone bubble wraps past this, so summary lines stay inside it.
PHONE_LINE = 32

PERIODS: dict[str, tuple[timedelta | None, str]] = {
    "5m": (timedelta(minutes=5), "последние 5 минут"),
    "30m": (timedelta(minutes=30), "последние 30 минут"),
    "1h": (timedelta(hours=1), "последний час"),
    "6h": (timedelta(hours=6), "последние 6 часов"),
    "12h": (timedelta(hours=12), "последние 12 часов"),
    "24h": (timedelta(hours=24), "последние сутки"),
    "7d": (timedelta(days=7), "последнюю неделю"),
    "30d": (timedelta(days=30), "последний месяц"),
    "all": (None, "всё время"),
}
PERIOD_BUTTONS = (
    (("5m", "5 мин"), ("30m", "30 мин"), ("1h", "1 ч")),
    (("6h", "6 ч"), ("12h", "12 ч"), ("24h", "сутки")),
    (("7d", "неделя"), ("30d", "месяц"), ("all", "всё время")),
)
BUCKETS = (
    ("0–100 мс", 0, 100),
    ("100–500 мс", 100, 500),
    ("500–1000 мс", 500, 1000),
    ("> 1000 мс", 1000, None),
)
VIEWS = {"s", "n"}
COMMANDS = {
    "/start",
    "/menu",
    "/status",
    "/summary",
    "/stats",
}

_started_monotonic: float | None = None
_ignored_chats: set[str] = set()


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"{name} is empty or unset")
    return value


def parse_ids() -> list[str]:
    raw = os.environ.get("BENCH_INSTANCE_IDS") or os.environ.get("BENCH_IDS") or ""
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not ids:
        die("BENCH_INSTANCE_IDS is empty")
    return ids


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def note_ignored_chat(chat_id: object, allowed_chat_id: str) -> None:
    key = str(chat_id)
    if key in _ignored_chats:
        return
    _ignored_chats.add(key)
    print(
        f"ignored chat_id={key} (ALLOWED_CHAT_ID={allowed_chat_id})",
        file=sys.stderr,
        flush=True,
    )


def mark_bot_started() -> None:
    global _started_monotonic
    _started_monotonic = time.monotonic()


def bot_uptime_seconds() -> float | None:
    if _started_monotonic is None:
        return None
    return max(0.0, time.monotonic() - _started_monotonic)


def host_uptime_seconds() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    idx = (p / 100.0) * (len(xs) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (idx - lo)


def seconds_human(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and not days:
        parts.append(f"{secs} с")
    if not parts:
        return "0 с"
    return " ".join(parts)


def wrap_plain(text: str, width: int = PHONE_LINE) -> list[str]:
    words = text.split()
    if not words:
        return [text] if text else []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}"
    lines.append(current)
    return lines


def flow_lines(prefix: str, items: list[str], width: int = PHONE_LINE, sep: str = ", ") -> list[str]:
    if not items:
        return []
    lines: list[str] = []
    current = prefix
    for item in items:
        joiner = "" if current in {prefix, ""} else sep
        if current and len(current) + len(joiner) + len(item) > width:
            ended = current.rstrip()
            if joiner == ", ":
                ended += ","
            lines.append(ended)
            current = item
        else:
            current = f"{current}{joiner}{item}"
    if current:
        lines.append(current.rstrip())
    return lines


def kv_html(rows: list[tuple[str, str]]) -> str:
    """Label/value lines that stay readable when a phone wraps the bubble."""
    lines: list[str] = []
    for label, value in rows:
        one = f"{label}: {value}"
        if len(one) <= PHONE_LINE:
            lines.append(f"<b>{html.escape(label)}</b>: {html.escape(value)}")
            continue
        lines.append(f"<b>{html.escape(label)}</b>")
        lines.extend(html.escape(part) for part in wrap_plain(value))
    return "\n".join(lines)


def split_pipe(text: str) -> list[str]:
    parts = [part.strip() for part in text.split("|")]
    return [part for part in parts if part] or ["—"]


def selected_label(text: str, on: bool) -> str:
    return f"[{text}]" if on else text


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value))} мс"


def fmt_pct(count: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{(count / total * 100):.1f}%".replace(".", ",")


def is_timeout(error: str | None) -> bool:
    text = str(error or "")
    return text == "timeout" or text.startswith("timeout")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS probes (
              rowid INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              id TEXT NOT NULL,
              ok INTEGER NOT NULL,
              http_status INTEGER,
              latency_ms REAL,
              selected TEXT,
              error TEXT,
              host_uptime_s REAL
            )
            """
        )
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(probes)")}
        if "host_uptime_s" not in cols:
            self._conn.execute("ALTER TABLE probes ADD COLUMN host_uptime_s REAL")
        self._conn.execute("CREATE INDEX IF NOT EXISTS probes_id_ts ON probes(id, ts)")
        self._conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ts": row["ts"],
            "id": row["id"],
            "ok": bool(row["ok"]),
            "http_status": row["http_status"],
            "latency_ms": row["latency_ms"],
            "selected": row["selected"],
            "error": row["error"],
            "host_uptime_s": row["host_uptime_s"],
        }

    async def aclose(self) -> None:
        async with self._lock:
            self._conn.close()

    async def append(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO probes (ts, id, ok, http_status, latency_ms, selected, error, host_uptime_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("ts"),
                    record.get("id"),
                    1 if record.get("ok") else 0,
                    record.get("http_status"),
                    record.get("latency_ms"),
                    record.get("selected"),
                    record.get("error"),
                    record.get("host_uptime_s"),
                ),
            )
            self._conn.commit()

    async def fetch_since(
        self,
        ids: list[str],
        cutoff: datetime | None,
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT ts, id, ok, http_status, latency_ms, selected, error, host_uptime_s "
            f"FROM probes WHERE id IN ({placeholders})"
        )
        params: list[Any] = list(ids)
        if cutoff is not None:
            sql += " AND ts >= ?"
            params.append(cutoff.astimezone(timezone.utc).isoformat(timespec="seconds"))
        sql += " ORDER BY ts ASC, rowid ASC"
        async with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]


async def probe_socks(instance_id: str) -> dict[str, Any]:
    proxy = f"socks5h://proxy-{instance_id}:11808"
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_total}",
        "--max-time",
        str(CURL_MAX_TIME_SEC),
        "-x",
        proxy,
        "https://api.telegram.org/bot",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    http_status: int | None = None
    latency_ms: float | None = None
    if stdout:
        parts = stdout.split()
        if parts:
            try:
                code = int(parts[0])
                http_status = None if code == 0 else code
            except ValueError:
                http_status = None
        if len(parts) >= 2:
            try:
                latency_ms = round(float(parts[1]) * 1000.0, 1)
            except ValueError:
                latency_ms = None

    error: str | None = None
    if proc.returncode == 28:
        error = "timeout"
    elif proc.returncode != 0:
        error = (stderr or f"curl exit {proc.returncode}")[:240]
    elif http_status != SUCCESS_HTTP:
        error = f"http {http_status}" if http_status is not None else (stderr or "no http status")

    return {
        "ok": proc.returncode == 0 and http_status == SUCCESS_HTTP,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "error": error,
    }


async def selected_node(client: httpx.AsyncClient, instance_id: str, secret: str) -> str | None:
    headers = {"Authorization": f"Bearer {secret}"}
    current = "AUTO"
    seen: set[str] = set()
    for _ in range(8):
        if current in seen:
            break
        seen.add(current)
        url = f"http://proxy-{instance_id}:19090/proxies/{quote(current, safe='')}"
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return None if current == "AUTO" else current
        if response.status_code != 200:
            return None if current == "AUTO" else current
        try:
            payload = response.json()
        except ValueError:
            return None if current == "AUTO" else current
        now = payload.get("now")
        proxy_type = str(payload.get("type") or "")
        if now and proxy_type in GROUP_TYPES and str(now) != current:
            current = str(now)
            continue
        if now:
            return str(now)
        return current
    return current


def probe_fail_record(
    instance_id: str,
    ts: str,
    error: str,
    host_uptime_s: float | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "id": instance_id,
        "ok": False,
        "http_status": None,
        "latency_ms": None,
        "selected": None,
        "error": error[:240],
        "host_uptime_s": host_uptime_s,
    }


async def probe_one(
    api_client: httpx.AsyncClient,
    instance_id: str,
    secret: str,
    ts: str,
) -> dict[str, Any]:
    socks, selected = await asyncio.gather(
        probe_socks(instance_id),
        selected_node(api_client, instance_id, secret),
    )
    return {
        "ts": ts,
        "id": instance_id,
        "ok": bool(socks["ok"]),
        "http_status": socks["http_status"],
        "latency_ms": socks["latency_ms"],
        "selected": selected,
        "error": socks["error"],
    }


async def record_probe(
    store: Store,
    api_client: httpx.AsyncClient,
    instance_id: str,
    secret: str,
) -> None:
    ts = now_iso()
    host_up = host_uptime_seconds()
    try:
        record = await asyncio.wait_for(
            probe_one(api_client, instance_id, secret, ts),
            timeout=PROBE_WAIT_SEC,
        )
        record["host_uptime_s"] = host_up
    except asyncio.TimeoutError:
        record = probe_fail_record(instance_id, ts, "timeout", host_up)
    except Exception as exc:  # noqa: BLE001
        record = probe_fail_record(instance_id, ts, str(exc), host_up)
    await store.append(record)


def _discard_probe_task(inflight: set[asyncio.Task[None]], task: asyncio.Task[None]) -> None:
    inflight.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"probe task failed: {exc}", file=sys.stderr)


async def probe_instance_loop(
    store: Store,
    instance_id: str,
    secret: str,
    api_client: httpx.AsyncClient,
    inflight: set[asyncio.Task[None]],
) -> None:
    while True:
        task = asyncio.create_task(record_probe(store, api_client, instance_id, secret))
        inflight.add(task)
        task.add_done_callback(lambda done: _discard_probe_task(inflight, done))
        await asyncio.sleep(PROBE_INTERVAL_SEC)


async def probe_loop(
    store: Store,
    ids: list[str],
    secret: str,
    api_client: httpx.AsyncClient,
) -> None:
    inflight: set[asyncio.Task[None]] = set()
    await asyncio.gather(
        *(
            probe_instance_loop(store, instance_id, secret, api_client, inflight)
            for instance_id in ids
        )
    )


def window_cutoff(period: str, now: datetime | None = None) -> datetime | None:
    end = now or now_utc()
    delta, _title = PERIODS.get(period, PERIODS["1h"])
    if delta is None:
        return None
    return end - delta


def in_window(record: dict[str, Any], cutoff: datetime | None) -> datetime | None:
    ts = parse_ts(str(record.get("ts") or ""))
    if ts is None:
        return None
    if cutoff is not None and ts < cutoff:
        return None
    return ts


def summarize_id(
    records: list[dict[str, Any]],
    instance_id: str,
    cutoff: datetime | None,
) -> dict[str, Any]:
    window: list[dict[str, Any]] = []
    for row in records:
        if row.get("id") != instance_id:
            continue
        if in_window(row, cutoff) is None:
            continue
        window.append(row)
    window.sort(key=lambda row: parse_ts(str(row.get("ts") or "")) or datetime.min.replace(tzinfo=timezone.utc))
    n = len(window)
    oks = [row for row in window if row.get("ok")]
    timeouts = sum(1 for row in window if is_timeout(row.get("error")))
    fails = n - len(oks)
    latencies = [
        float(row["latency_ms"])
        for row in oks
        if isinstance(row.get("latency_ms"), (int, float))
    ]
    buckets = {label: 0 for label, _lo, _hi in BUCKETS}
    buckets["timeout"] = timeouts
    buckets["fail"] = max(0, fails - timeouts)
    for row in oks:
        ms = row.get("latency_ms")
        if not isinstance(ms, (int, float)):
            continue
        for label, lo, hi in BUCKETS:
            if ms >= lo and (hi is None or ms < hi):
                buckets[label] += 1
                break
    switches = 0
    prev_node: str | None = None
    seen_nodes: set[str] = set()
    fail_streak = 0
    longest_fail = 0
    for row in window:
        node = str(row.get("selected") or "").strip()
        if node:
            seen_nodes.add(node)
            if prev_node is not None and node != prev_node:
                switches += 1
            prev_node = node
        if row.get("ok"):
            fail_streak = 0
        else:
            fail_streak += 1
            if fail_streak > longest_fail:
                longest_fail = fail_streak
    expected = expected_ticks(cutoff, records, instance_id)
    window_start = cutoff
    if window_start is None and window:
        window_start = parse_ts(str(window[0].get("ts") or ""))
    heartbeats: list[tuple[datetime, float | None]] = []
    for row in window:
        ts = parse_ts(str(row.get("ts") or ""))
        if ts is None:
            continue
        raw_up = row.get("host_uptime_s")
        host_up = float(raw_up) if isinstance(raw_up, (int, float)) else None
        heartbeats.append((ts, host_up))
    service_down, server_off = downtime_ticks(
        heartbeats,
        window_start=window_start,
        window_end=now_utc(),
        interval_seconds=PROBE_INTERVAL_SEC,
        now_host_uptime_s=host_uptime_seconds(),
    )
    buckets["service_down"] = service_down
    buckets["server_off"] = server_off
    return {
        "id": instance_id,
        "n": n,
        "ok": len(oks),
        "fail": fails,
        "timeouts": timeouts,
        "success": (len(oks) / n) if n else None,
        "timeout_rate": (timeouts / n) if n else None,
        "avg": (sum(latencies) / len(latencies)) if latencies else None,
        "min": min(latencies) if latencies else None,
        "max": max(latencies) if latencies else None,
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "stdev": stdev(latencies),
        "node_switches": switches,
        "unique_nodes": len(seen_nodes),
        "fail_streak": longest_fail,
        "expected": expected,
        "coverage": (n / expected) if expected else None,
        "buckets": buckets,
    }


def top_nodes(
    records: list[dict[str, Any]],
    instance_id: str,
    cutoff: datetime | None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    lats: dict[str, list[float]] = defaultdict(list)
    for row in records:
        if row.get("id") != instance_id:
            continue
        if in_window(row, cutoff) is None:
            continue
        node = str(row.get("selected") or "").strip()
        if not node:
            continue
        counts[node] += 1
        if row.get("ok") and isinstance(row.get("latency_ms"), (int, float)):
            lats[node].append(float(row["latency_ms"]))
    result: list[dict[str, Any]] = []
    for name, samples in counts.most_common(limit):
        values = lats.get(name) or []
        result.append(
            {
                "name": name,
                "samples": samples,
                "avg": (sum(values) / len(values)) if values else None,
                "p95": percentile(values, 95),
            }
        )
    return result


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return math.sqrt(var)


def expected_ticks(cutoff: datetime | None, records: list[dict[str, Any]], instance_id: str) -> int:
    now = now_utc()
    if cutoff is None:
        stamps = [
            ts
            for row in records
            if row.get("id") == instance_id
            for ts in [parse_ts(str(row.get("ts") or ""))]
            if ts is not None
        ]
        if not stamps:
            return 0
        start = min(stamps)
    else:
        start = cutoff
    span = max(0.0, (now - start).total_seconds())
    return int(span // PROBE_INTERVAL_SEC)


def _fmt_success(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%".replace(".", ",")


def _fmt_sigma(value: float | None) -> str:
    if value is None:
        return "—"
    return f"σ {int(round(value))} мс"


def _norm_metric(value: Any, key: str) -> Any:
    if isinstance(value, float) and key in {"p50", "p95", "p99", "avg", "stdev"}:
        return round(value, 1)
    if isinstance(value, float) and key in {"success", "timeout_rate"}:
        return round(value, 4)
    return value


def _pick_dimension(
    stats: list[dict[str, Any]],
    key: str,
    *,
    higher: bool,
    fmt,
    weight: int,
) -> dict[str, Any]:
    scored = [(item, item.get(key)) for item in stats if item.get(key) is not None]
    missing = [item for item in stats if item.get(key) is None]
    if not scored:
        return {
            "winner": None,
            "loser": None,
            "tied": False,
            "tied_ids": [],
            "order": [(str(item["id"]), "—") for item in missing],
            "points": {},
        }

    def sort_key(pair: tuple[dict[str, Any], Any]) -> tuple[float, str]:
        number = _norm_metric(pair[1], key)
        primary = float(number) if isinstance(number, (int, float)) else 0.0
        if higher:
            primary = -primary
        return (primary, str(pair[0]["id"]))

    ranked = sorted(scored, key=sort_key)

    def unique_edge(index: int) -> str | None:
        if len(ranked) < 2:
            return None
        edge = _norm_metric(ranked[index][1], key)
        tied_edge = [item for item, value in ranked if _norm_metric(value, key) == edge]
        if len(tied_edge) != 1:
            return None
        return str(ranked[index][0]["id"])

    loser = unique_edge(-1)
    best_item, best = ranked[0]
    best_n = _norm_metric(best, key)
    tied = [item for item, value in ranked if _norm_metric(value, key) == best_n]
    tied_ids = [str(item["id"]) for item in tied] if len(tied) > 1 else []
    winner = None if tied_ids else str(best_item["id"])

    n = len(ranked)
    points: dict[str, float] = {}
    index = 0
    while index < n:
        edge = _norm_metric(ranked[index][1], key)
        end = index + 1
        while end < n and _norm_metric(ranked[end][1], key) == edge:
            end += 1
        share = sum((n - 1 - pos) * weight for pos in range(index, end)) / (end - index)
        for pos in range(index, end):
            points[str(ranked[pos][0]["id"])] = share
        index = end

    order = [(str(item["id"]), fmt(item)) for item, _value in ranked]
    order.extend((str(item["id"]), "—") for item in missing)
    return {
        "winner": winner,
        "loser": loser,
        "tied": bool(tied_ids),
        "tied_ids": tied_ids,
        "order": order,
        "points": points,
    }


def _rank_instances(usable: list[dict[str, Any]], totals: dict[str, float]) -> list[str]:
    """Higher success first. Inside the same displayed percent, weighted ranks break the tie."""

    def key(item: dict[str, Any]) -> tuple[float, float, float, str]:
        success = item.get("success")
        if isinstance(success, (int, float)):
            shown = round(float(success) * 1000)
            exact = float(success)
        else:
            shown = -1
            exact = -1.0
        return (-shown, -totals.get(str(item["id"]), 0.0), -exact, str(item["id"]))

    return [str(item["id"]) for item in sorted(usable, key=key)]


def _lead_tie(ranked: list[str], by_id: dict[str, dict[str, Any]], totals: dict[str, float]) -> list[str]:
    if not ranked:
        return []
    lead = ranked[0]
    tied = [lead]
    lead_item = by_id[lead]
    for other in ranked[1:]:
        item = by_id[other]
        same_score = abs(totals.get(lead, 0.0) - totals.get(other, 0.0)) < 1e-9
        both = isinstance(lead_item.get("success"), (int, float)) and isinstance(item.get("success"), (int, float))
        same_success = both and abs(float(lead_item["success"]) - float(item["success"])) < 1e-12
        if same_score and same_success:
            tied.append(other)
            continue
        break
    return tied


def compare_stats(stats: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [item for item in stats if item.get("n")]
    if len(usable) < 2:
        return None
    specs = [
        ("Надёжность", "success", True, lambda s: _fmt_success(s.get("success")), 5),
        ("Таймауты", "timeout_rate", False, lambda s: _fmt_success(s.get("timeout_rate")), 3),
        ("Скорость p50", "p50", False, lambda s: fmt_ms(s.get("p50")), 2),
        ("Хвост p95", "p95", False, lambda s: fmt_ms(s.get("p95")), 3),
        ("Хвост p99", "p99", False, lambda s: fmt_ms(s.get("p99")), 2),
        ("Разброс", "stdev", False, lambda s: _fmt_sigma(s.get("stdev")), 1),
        ("Смена ноды", "node_switches", False, lambda s: str(int(s.get("node_switches") or 0)), 1),
        ("Длинный простой", "fail_streak", False, lambda s: f"{int(s.get('fail_streak') or 0)} проб", 2),
    ]
    dimensions: list[tuple[str, dict[str, Any]]] = []
    totals = {str(item["id"]): 0.0 for item in usable}
    for name, key, higher, fmt, weight in specs:
        result = _pick_dimension(usable, key, higher=higher, fmt=fmt, weight=weight)
        for instance_id, points in result["points"].items():
            totals[instance_id] += points
        dimensions.append((name, result))
    ranked = _rank_instances(usable, totals)
    by_id = {str(item["id"]): item for item in usable}
    missing = [str(item["id"]) for item in stats if not item.get("n")]
    return {
        "ranked": ranked,
        "missing": missing,
        "lead_tie": _lead_tie(ranked, by_id, totals),
        "dimensions": dimensions,
        "scores": totals,
    }


def _loss_groups(instance_id: str, dimensions: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for name, result in dimensions:
        winner = result.get("winner")
        if winner and winner != instance_id:
            key = str(winner)
        elif result.get("tied") and instance_id not in (result.get("tied_ids") or []):
            key = ", ".join(result.get("tied_ids") or [])
        else:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(name)
    return [(key, groups[key]) for key in order]


def _prefixed_items(prefix: str, items: list[str]) -> list[str]:
    if not items:
        return wrap_plain(prefix) if prefix else []
    if len(prefix) + len(items[0]) <= PHONE_LINE:
        return flow_lines(prefix, items)
    return [*wrap_plain(prefix.rstrip()), *flow_lines("", items)]


def _advice_lines(instance_id: str, dimensions: list[tuple[str, dict[str, Any]]]) -> list[str]:
    wins = [name for name, result in dimensions if result.get("winner") == instance_id]
    worse = [name for name, result in dimensions if result.get("loser") == instance_id]
    lines: list[str] = []
    if wins:
        lines.extend(_prefixed_items("лучше по: ", wins))
    for winner, names in _loss_groups(instance_id, dimensions):
        # Who tied for the lead is listed once above the ranking.
        if "," in winner:
            continue
        lines.extend(_prefixed_items(f"уступает {winner}: ", names))
    if worse:
        lines.extend(_prefixed_items("хуже всех по: ", worse))
    if not lines:
        lines.append("без явного перевеса")
    return [html.escape(line) for line in lines]


def _advice_note(instance_id: str, dimensions: list[tuple[str, dict[str, Any]]]) -> str | None:
    wins = {name for name, result in dimensions if result.get("winner") == instance_id}
    if "Надёжность" in wins:
        return "Для Telegram-ботов надёжность важнее сырой скорости."
    speed = {"Скорость p50", "Хвост p95", "Хвост p99"}
    if not wins & speed:
        return None
    for name, result in dimensions:
        if name == "Надёжность" and result.get("winner") not in {None, instance_id}:
            return "Быстрее по задержкам, но чаще теряет Bot API."
    return None


def _stat_lines(item: dict[str, Any]) -> list[str]:
    n = int(item["n"])
    success = fmt_pct(int(item["ok"]), n) if n else "—"
    latency = f"p50 {fmt_ms(item.get('p50'))} · p95 {fmt_ms(item.get('p95'))}"
    lines = [f"{success} · таймауты {_fmt_success(item.get('timeout_rate'))}"]
    if len(latency) <= PHONE_LINE:
        lines.append(latency)
    else:
        lines.append(f"p50 {fmt_ms(item.get('p50'))}")
        lines.append(f"p95 {fmt_ms(item.get('p95'))}")
    lines.append(f"простой {int(item.get('fail_streak') or 0)}")
    return lines


def _recommendation_card(
    index: int,
    item: dict[str, Any],
    dimensions: list[tuple[str, dict[str, Any]]],
) -> str:
    instance_id = str(item["id"])
    lines = [f"<b>{index}. {html.escape(instance_id)}</b>"]
    lines.extend(html.escape(line) for line in _stat_lines(item))
    lines.extend(_advice_lines(instance_id, dimensions))
    note = _advice_note(instance_id, dimensions)
    if note:
        lines.extend(html.escape(part) for part in wrap_plain(note))
    return "\n".join(lines)


def _metric_block(name: str, result: dict[str, Any]) -> str:
    lines = [f"<b>{html.escape(name)}</b>"]
    if result.get("tied"):
        lines.append("ничья")
    for instance_id, value in result["order"]:
        lines.append(f"{html.escape(str(instance_id))} · {html.escape(str(value))}")
    return "\n".join(lines)


def verdict_parts(stats: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Ranking lines, then one paragraph per metric (dropped first if the message is long)."""
    verdict = compare_stats(stats)
    if verdict is None:
        return [], []
    dimensions: list[tuple[str, dict[str, Any]]] = verdict["dimensions"]
    lines = ["<b>Рекомендации</b>"]
    lead = verdict["lead_tie"]
    if len(lead) > 1:
        lines.append("")
        lines.extend(html.escape(part) for part in flow_lines("Вровень: ", lead))
    for name, result in dimensions:
        if result.get("tied"):
            lines.append("")
            lines.extend(
                html.escape(part) for part in flow_lines(f"ничья {name}: ", result.get("tied_ids") or [])
            )
    by_id = {str(item["id"]): item for item in stats}
    index = 1
    for instance_id in verdict["ranked"]:
        lines.append("")
        lines.append(_recommendation_card(index, by_id[instance_id], dimensions))
        index += 1
    for instance_id in verdict["missing"]:
        lines.append("")
        lines.append(f"<b>{index}. {html.escape(instance_id)}</b>\nнет проб за этот период")
        index += 1
    metrics = [_metric_block(name, result) for name, result in dimensions]
    return lines, metrics


def _append_fitting(lines: list[str], blocks: list[str], limit: int) -> None:
    """Add whole blocks until one no longer fits. Later blocks are not tried."""
    for block in blocks:
        candidate = "\n".join([*lines, "", block])
        if len(candidate) > limit:
            return
        lines.extend(["", block])


def cb_data(period: str, view: str, instance_id: str | None = None) -> str:
    if view == "i" and instance_id:
        return f"st:{period}:i:{instance_id}"
    return f"st:{period}:{view}"


def parse_stats_cb(data: str | None) -> tuple[str, str, str | None]:
    period, view, instance_id = "1h", "s", None
    if not data or not data.startswith("st:"):
        return period, view, instance_id
    parts = data.split(":")
    if len(parts) >= 2 and parts[1] in PERIODS:
        period = parts[1]
    if len(parts) >= 3 and parts[2] == "i" and len(parts) >= 4:
        return period, "i", parts[3]
    if len(parts) >= 3 and parts[2] in VIEWS:
        view = parts[2]
    return period, view, instance_id


def stats_keyboard(period: str, view: str, ids: list[str], instance_id: str | None) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for group in PERIOD_BUTTONS:
        rows.append(
            [
                {"text": selected_label(label, key == period), "callback_data": cb_data(key, view, instance_id)}
                for key, label in group
            ]
        )
    rows.append(
        [
            {"text": selected_label("Сводка", view == "s"), "callback_data": cb_data(period, "s")},
            {"text": selected_label("Ноды", view == "n"), "callback_data": cb_data(period, "n")},
        ]
    )
    id_row: list[dict[str, str]] = []
    for item in ids:
        id_row.append(
            {
                "text": selected_label(item, view == "i" and instance_id == item),
                "callback_data": cb_data(period, "i", item),
            }
        )
        if len(id_row) == 3:
            rows.append(id_row)
            id_row = []
    if id_row:
        rows.append(id_row)
    rows.append([{"text": "↻ Обновить", "callback_data": cb_data(period, view, instance_id)}])
    return {"inline_keyboard": rows}


def stats_table(item: dict[str, Any], expected: int) -> list[str]:
    n = int(item["n"])
    fail = int(item["fail"])
    samples = f"{n} из {expected}" if expected else str(n)
    success = fmt_pct(int(item["ok"]), n) if n else "—"
    rows = [
        ("Успех", success),
        ("Замеров", f"{samples} · ошибок {fail} ({fmt_pct(fail, n)})"),
        ("Средняя", fmt_ms(item["avg"])),
        ("Минимум", fmt_ms(item["min"])),
        ("Максимум", fmt_ms(item["max"])),
        ("p50", fmt_ms(item["p50"])),
        ("p95", fmt_ms(item["p95"])),
        ("p99", fmt_ms(item["p99"])),
        ("Разброс", _fmt_sigma(item.get("stdev"))),
        ("Таймауты", str(item["timeouts"])),
        ("Смена ноды", str(int(item.get("node_switches") or 0))),
        ("Длинный простой", f"{int(item.get('fail_streak') or 0)} проб"),
    ]
    return [kv_html(rows)]


def compact_card(item: dict[str, Any]) -> str:
    instance_id = html.escape(str(item["id"]))
    n = int(item["n"])
    if n == 0:
        return f"<b>{instance_id}</b>\nнет проб"
    success = html.escape(fmt_pct(int(item["ok"]), n))
    return "\n".join(
        [
            f"<b>{instance_id}</b> · {success}",
            f"p50 {html.escape(fmt_ms(item['p50']))} · таймауты {int(item['timeouts'])}",
        ]
    )


def _strip_tags(text: str) -> str:
    out: list[str] = []
    skip = False
    for ch in text:
        if ch == "<":
            skip = True
            continue
        if ch == ">":
            skip = False
            continue
        if not skip:
            out.append(ch)
    return "".join(out)


def _tags_balanced(text: str) -> bool:
    for tag in ("b", "pre", "code"):
        if text.count(f"<{tag}>") != text.count(f"</{tag}>"):
            return False
    return True


def fit_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Keep a valid HTML message. Never cut through a tag."""
    if len(text) <= limit and _tags_balanced(text):
        return text
    parts = text.split("\n\n")
    while len(parts) > 1 and len("\n\n".join(parts)) > limit:
        parts.pop()
    fitted = "\n\n".join(parts)
    if len(fitted) <= limit and _tags_balanced(fitted):
        return fitted
    plain = html.unescape(_strip_tags(text))
    if len(plain) > limit:
        plain = plain[: limit - 1] + "…"
    return html.escape(plain)


def bucket_block(item: dict[str, Any], expected: int) -> list[str]:
    buckets: dict[str, int] = item["buckets"]
    rows = [(label, int(buckets.get(label, 0))) for label, _lo, _hi in BUCKETS]
    rows.append(("Таймаут", int(buckets.get("timeout", 0))))
    rows.append(("Другой fail", int(buckets.get("fail", 0))))
    rows.append(("Сервис не запущен", int(buckets.get("service_down", 0))))
    rows.append(("Сервер выключен", int(buckets.get("server_off", 0))))
    observed = sum(count for _, count in rows)
    total = max(expected, observed) if expected else observed
    table = [
        (label, f"{seconds_human(count * PROBE_INTERVAL_SEC)} ({fmt_pct(count, total)})")
        for label, count in rows
    ]
    intro = f"Время в диапазонах (тик {PROBE_INTERVAL_SEC} с, должно быть {expected or total} зам.):"
    return [*wrap_plain(intro), kv_html(table)]


def node_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["Нет выбранных нод."]
    lines: list[str] = []
    for i, row in enumerate(rows):
        if i:
            lines.append("")
        parts = split_pipe(str(row["name"]))
        wrapped = wrap_plain(parts[0])
        lines.append("• " + html.escape(wrapped[0]))
        lines.extend(html.escape(line) for line in wrapped[1:])
        for part in parts[1:]:
            lines.extend(html.escape(line) for line in wrap_plain(part))
        meta = f"{int(row['samples'])} раз · avg {fmt_ms(row['avg'])}"
        tail = f"p95 {fmt_ms(row['p95'])}"
        if len(meta) + 3 + len(tail) <= PHONE_LINE:
            lines.append(f"{meta} · {tail}")
        else:
            lines.append(meta)
            lines.append(tail)
    return lines


def _labeled_line(label: str, value: str) -> list[str]:
    if len(f"{label} · {value}") <= PHONE_LINE:
        return [f"<b>{html.escape(label)}</b> · {html.escape(value)}"]
    return [f"<b>{html.escape(label)}</b>", html.escape(value)]


def header_block(_ids: list[str]) -> str:
    commit, title = app_build_identity()
    lines: list[str] = []
    lines.extend(_labeled_line("Аптайм бота", seconds_human(bot_uptime_seconds())))
    lines.extend(_labeled_line("Аптайм сервера", seconds_human(host_uptime_seconds())))
    lines.append("<b>Коммит</b>")
    lines.extend(html.escape(part) for part in wrap_plain(title))
    lines.append(f"<code>{html.escape(commit)}</code>")
    lines.extend(_labeled_line("Интервал проб", f"{PROBE_INTERVAL_SEC} с"))
    return "\n".join(lines)


def render_stats(
    records: list[dict[str, Any]],
    ids: list[str],
    period: str,
    view: str,
    instance_id: str | None,
) -> str:
    if period not in PERIODS:
        period = "1h"
    _delta, title = PERIODS[period]
    cutoff = window_cutoff(period)
    focus = [instance_id] if view == "i" and instance_id in ids else ids
    stats = [summarize_id(records, item, cutoff) for item in focus]

    lines = ["🖴 <b>Сравнение схем Mihomo</b>", "", header_block(ids), "", f"<b>За {title}</b>"]
    showed_verdict = False
    metric_blocks: list[str] = []
    if len(focus) > 1:
        ranking_lines, metric_blocks = verdict_parts(stats)
        if ranking_lines:
            showed_verdict = True
            lines.extend(ranking_lines)

    optional: list[str] = []
    if view == "n":
        for item in focus:
            body = "\n".join(node_lines(top_nodes(records, item, cutoff)))
            optional.append(f"<b>Топ нод · {html.escape(item)}</b>\n{body}")
    elif len(stats) > 1 and not showed_verdict:
        optional.extend(compact_card(item) for item in stats)
    if metric_blocks:
        optional.append("<b>Что лучше</b>\n" + metric_blocks[0])
        optional.extend(metric_blocks[1:])
    if len(stats) != 1 or view == "n":
        _append_fitting(lines, optional, TELEGRAM_TEXT_LIMIT)
        if len(stats) > 1:
            _append_fitting(lines, ["Полные цифры — кнопка схемы."], TELEGRAM_TEXT_LIMIT)
    else:
        for item in stats:
            expected = int(item.get("expected") or 0)
            lines.append("")
            lines.append(f"<b>{html.escape(str(item['id']))}</b>")
            if item["n"] == 0:
                lines.append("Нет проб в этом окне.")
                continue
            lines.extend(stats_table(item, expected))
            lines.append("")
            lines.extend(bucket_block(item, expected))

    return fit_telegram_text("\n".join(lines))


class Telegram:
    def __init__(self, token: str, proxy: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(TELEGRAM_POLL_TIMEOUT + 15, connect=20),
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}/{method}"
        response = await self.client.post(url, json=payload)
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            response.raise_for_status()
            raise RuntimeError(f"telegram {method} returned non-JSON") from exc
        if body.get("ok"):
            return body
        desc = str(body.get("description") or f"telegram {method} failed")
        if "not modified" in desc.lower():
            return body
        raise RuntimeError(desc)

    async def send(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("sendMessage", payload)

    async def edit(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        await self.call("answerCallbackQuery", payload)

    async def delete_webhook(self) -> None:
        # A leftover webhook makes getUpdates return nothing, so /start is silent.
        await self.call("deleteWebhook", {})

    async def set_commands(self) -> None:
        await self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Открыть сравнение схем"},
                    {"command": "status", "description": "Сводка за период"},
                    {"command": "summary", "description": "Статистика за выбранный период"},
                ]
            },
        )


def command_name(text: str) -> str:
    first = text.split()[0] if text.split() else ""
    return first.split("@", 1)[0].lower()


async def show_panel(
    tg: Telegram,
    store: Store,
    ids: list[str],
    chat_id: int | str,
    period: str,
    view: str,
    instance_id: str | None,
    *,
    message_id: int | None = None,
) -> None:
    if instance_id is not None and instance_id not in ids:
        instance_id = None
        view = "s"
    if period not in PERIODS:
        period = "1h"
    records = await store.fetch_since(ids, window_cutoff(period))
    text = render_stats(records, ids, period, view, instance_id)
    markup = stats_keyboard(period, view, ids, instance_id)
    if message_id is not None:
        try:
            await tg.edit(chat_id, message_id, text, markup)
            return
        except RuntimeError as exc:
            print(f"editMessageText failed: {exc}", file=sys.stderr)
    try:
        await tg.send(chat_id, text, markup)
    except RuntimeError as exc:
        print(f"sendMessage failed: {exc}", file=sys.stderr)
        await tg.send(chat_id, "Панель не отправилась. Открой одну схему кнопкой.", None)


async def poll_loop(
    tg: Telegram,
    store: Store,
    ids: list[str],
    allowed_chat_id: str,
) -> None:
    offset = 0
    while True:
        try:
            body = await tg.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": TELEGRAM_POLL_TIMEOUT,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"getUpdates failed: {exc}", file=sys.stderr)
            await asyncio.sleep(3)
            continue
        for update in body.get("result") or []:
            offset = max(offset, int(update["update_id"]) + 1)
            try:
                await dispatch_update(tg, store, ids, allowed_chat_id, update)
            except Exception as exc:  # noqa: BLE001
                print(f"update failed: {exc}", file=sys.stderr)


async def dispatch_update(
    tg: Telegram,
    store: Store,
    ids: list[str],
    allowed_chat_id: str,
    update: dict[str, Any],
) -> None:
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        callback_id = str(callback.get("id") or "")
        if chat_id is None or str(chat_id) != allowed_chat_id:
            note_ignored_chat(chat_id, allowed_chat_id)
            if callback_id:
                await tg.answer_callback(callback_id)
            return
        data = str(callback.get("data") or "")
        period, view, instance_id = parse_stats_cb(data)
        await tg.answer_callback(callback_id)
        message_id = message.get("message_id")
        await show_panel(
            tg,
            store,
            ids,
            chat_id,
            period,
            view,
            instance_id,
            message_id=int(message_id) if message_id is not None else None,
        )
        return

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or str(chat_id) != allowed_chat_id:
        note_ignored_chat(chat_id, allowed_chat_id)
        return
    text = str(message.get("text") or "").strip()
    if not text:
        return
    cmd = command_name(text)
    if cmd not in COMMANDS:
        return
    print(f"cmd {cmd} chat={chat_id}", flush=True)
    period = "1h"
    view = "s"
    if cmd == "/status":
        view = "s"
    await show_panel(tg, store, ids, chat_id, period, view, None)


async def amain() -> None:
    if not shutil.which("curl"):
        die("curl not found (needed for SOCKS probes)")
    mark_bot_started()
    token = require_env("TELEGRAM_BOT_TOKEN")
    allowed = require_env("ALLOWED_CHAT_ID")
    secret = require_env("MIHOMO_API_SECRET")
    ids = parse_ids()
    proxy = os.environ.get("TELEGRAM_PROXY_URL", "socks5h://proxy:11808").strip()
    results_path = Path(os.environ.get("RESULTS_PATH", "/app/results/probes.db"))

    store = Store(results_path)
    tg = Telegram(token, proxy)
    api_client = httpx.AsyncClient(timeout=CURL_MAX_TIME_SEC, trust_env=False)
    print(f"bench-bot starting ids={ids} Bot API via {proxy} db={results_path}", flush=True)
    try:
        await tg.delete_webhook()
    except Exception as exc:  # noqa: BLE001
        print(f"deleteWebhook failed: {exc}", file=sys.stderr)
    try:
        await tg.set_commands()
    except Exception as exc:  # noqa: BLE001
        print(f"setMyCommands failed: {exc}", file=sys.stderr)
    try:
        await asyncio.gather(
            probe_loop(store, ids, secret, api_client),
            poll_loop(tg, store, ids, allowed),
        )
    finally:
        await tg.aclose()
        await api_client.aclose()
        await store.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
