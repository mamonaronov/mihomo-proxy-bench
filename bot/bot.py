#!/usr/bin/env python3
"""Compare bench Mihomo instances. Telegram Bot API goes through prod proxy only."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

PROBE_INTERVAL_SEC = 15
CURL_MAX_TIME_SEC = 8
SUMMARY_WINDOW = timedelta(hours=1)
TELEGRAM_POLL_TIMEOUT = 50
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
SUCCESS_HTTP = 404


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aware_min() -> datetime:
    return datetime.min.replace(tzinfo=timezone.utc)


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


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}ms"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    async def read_all(self) -> list[dict[str, Any]]:
        async with self._lock:
            if not self.path.is_file():
                return []
            text = self.path.read_text(encoding="utf-8")
        records: list[dict[str, Any]] = []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records


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


async def probe_one(
    api_client: httpx.AsyncClient,
    instance_id: str,
    secret: str,
) -> dict[str, Any]:
    socks_task = asyncio.create_task(probe_socks(instance_id))
    selected_task = asyncio.create_task(selected_node(api_client, instance_id, secret))
    socks, selected = await asyncio.gather(socks_task, selected_task)
    return {
        "ts": now_iso(),
        "id": instance_id,
        "ok": bool(socks["ok"]),
        "http_status": socks["http_status"],
        "latency_ms": socks["latency_ms"],
        "selected": selected,
        "error": socks["error"],
    }


async def probe_loop(
    store: Store,
    ids: list[str],
    secret: str,
    api_client: httpx.AsyncClient,
) -> None:
    while True:
        results = await asyncio.gather(
            *(probe_one(api_client, instance_id, secret) for instance_id in ids),
            return_exceptions=True,
        )
        for instance_id, result in zip(ids, results):
            if isinstance(result, Exception):
                record = {
                    "ts": now_iso(),
                    "id": instance_id,
                    "ok": False,
                    "http_status": None,
                    "latency_ms": None,
                    "selected": None,
                    "error": str(result),
                }
            else:
                record = result
            await store.append(record)
        await asyncio.sleep(PROBE_INTERVAL_SEC)


def last_by_id(records: list[dict[str, Any]], ids: list[str]) -> dict[str, dict[str, Any] | None]:
    latest: dict[str, dict[str, Any] | None] = {instance_id: None for instance_id in ids}
    for record in records:
        instance_id = record.get("id")
        if instance_id not in latest:
            continue
        prev = latest[instance_id]
        if prev is None:
            latest[instance_id] = record
            continue
        prev_ts = parse_ts(str(prev.get("ts") or "")) or aware_min()
        cur_ts = parse_ts(str(record.get("ts") or "")) or aware_min()
        if cur_ts >= prev_ts:
            latest[instance_id] = record
    return latest


def format_status(records: list[dict[str, Any]], ids: list[str]) -> str:
    latest = last_by_id(records, ids)
    lines = ["last probe per config"]
    for instance_id in ids:
        record = latest[instance_id]
        if record is None:
            lines.append(f"{instance_id}: no probes yet")
            continue
        flag = "ok" if record.get("ok") else "fail"
        http_status = record.get("http_status")
        http_s = str(http_status) if http_status is not None else "—"
        node = record.get("selected") or "—"
        line = (
            f"{instance_id}: {flag}  {fmt_ms(record.get('latency_ms') if isinstance(record.get('latency_ms'), (int, float)) else None)}  "
            f"http {http_s}  node {node}"
        )
        if record.get("error"):
            line += f"  ({record['error']})"
        lines.append(line)
    return "\n".join(lines)


def summarize_id(records: list[dict[str, Any]], instance_id: str, cutoff: datetime) -> dict[str, Any]:
    window = []
    for row in records:
        if row.get("id") != instance_id:
            continue
        ts = parse_ts(str(row.get("ts") or ""))
        if ts is None or ts < cutoff:
            continue
        window.append(row)
    n = len(window)
    oks = [row for row in window if row.get("ok")]
    timeouts = sum(
        1
        for row in window
        if str(row.get("error") or "") == "timeout"
        or str(row.get("error") or "").startswith("timeout")
    )
    latencies = [
        float(row["latency_ms"])
        for row in oks
        if isinstance(row.get("latency_ms"), (int, float))
    ]
    success = (len(oks) / n) if n else None
    return {
        "id": instance_id,
        "n": n,
        "success": success,
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "timeouts": timeouts,
    }


def pick_winner(stats: list[dict[str, Any]]) -> tuple[str | None, str]:
    ranked = [item for item in stats if item["n"] > 0 and item["success"] is not None]
    if not ranked:
        return None, "none"

    def key(item: dict[str, Any]) -> tuple[float, float]:
        success = float(item["success"])
        p95 = item["p95"]
        p95_rank = float(p95) if p95 is not None else float("inf")
        return (-success, p95_rank)

    ranked.sort(key=key)
    winner = ranked[0]
    if len(ranked) > 1:
        second = ranked[1]
        if winner["success"] == second["success"] and winner["p95"] == second["p95"]:
            return None, "tie"
        if winner["success"] == second["success"]:
            return str(winner["id"]), "same success, lower p95"
    return str(winner["id"]), "higher success"


def format_summary(records: list[dict[str, Any]], ids: list[str]) -> str:
    cutoff = datetime.now(timezone.utc) - SUMMARY_WINDOW
    stats = [summarize_id(records, instance_id, cutoff) for instance_id in ids]
    lines = ["window 1h"]
    for item in stats:
        if item["n"] == 0:
            lines.append(f"{item['id']}: no probes in window")
            continue
        lines.append(
            f"{item['id']}: {fmt_pct(item['success'])}  n={item['n']}  "
            f"p50={fmt_ms(item['p50'])}  p95={fmt_ms(item['p95'])}  "
            f"timeouts={item['timeouts']}"
        )
    winner, reason = pick_winner(stats)
    if winner:
        lines.append(f"winner: {winner}  ({reason})")
    else:
        lines.append(f"winner: {reason}")
    return "\n".join(lines)


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
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description") or f"telegram {method} failed")
        return body

    async def send(self, chat_id: int | str, text: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})


def command_name(text: str) -> str:
    first = text.split()[0] if text.split() else ""
    return first.split("@", 1)[0].lower()


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
                    "allowed_updates": ["message"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"getUpdates failed: {exc}", file=sys.stderr)
            await asyncio.sleep(3)
            continue
        for update in body.get("result") or []:
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None or str(chat_id) != allowed_chat_id:
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            cmd = command_name(text)
            if cmd not in {"/status", "/summary"}:
                continue
            try:
                records = await store.read_all()
                if cmd == "/status":
                    await tg.send(chat_id, format_status(records, ids))
                else:
                    await tg.send(chat_id, format_summary(records, ids))
            except Exception as exc:  # noqa: BLE001
                print(f"command {cmd} failed: {exc}", file=sys.stderr)
                try:
                    await tg.send(chat_id, f"error: {exc}")
                except Exception as send_exc:  # noqa: BLE001
                    print(f"reply failed: {send_exc}", file=sys.stderr)


async def amain() -> None:
    if not shutil.which("curl"):
        die("curl not found (needed for SOCKS probes)")
    token = require_env("TELEGRAM_BOT_TOKEN")
    allowed = require_env("ALLOWED_CHAT_ID")
    secret = require_env("MIHOMO_API_SECRET")
    ids = parse_ids()
    proxy = os.environ.get("TELEGRAM_PROXY_URL", "socks5h://proxy:11808").strip()
    results_path = Path(os.environ.get("RESULTS_PATH", "/app/results/probes.jsonl"))

    store = Store(results_path)
    tg = Telegram(token, proxy)
    api_client = httpx.AsyncClient(timeout=CURL_MAX_TIME_SEC, trust_env=False)
    print(f"bench-bot starting ids={ids} Bot API via {proxy}", flush=True)
    try:
        await asyncio.gather(
            probe_loop(store, ids, secret, api_client),
            poll_loop(tg, store, ids, allowed),
        )
    finally:
        await tg.aclose()
        await api_client.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
