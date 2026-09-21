#!/usr/bin/env python3
"""Move the current probes.db run into results/archive.db, then clear the live db."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE_DEFAULT = ROOT / "results" / "probes.db"
ARCHIVE_DEFAULT = ROOT / "results" / "archive.db"


def sidecar(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


def checkpoint(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def table_exists(conn: sqlite3.Connection, schema: str, name: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def init_archive(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS probes (
          rowid INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(probes)")}
    if "host_uptime_s" not in cols:
        conn.execute("ALTER TABLE probes ADD COLUMN host_uptime_s REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS probes_run_id_ts ON probes(run_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS probes_id_ts ON probes(id, ts)")
    conn.commit()


def remove_live(path: Path) -> None:
    path.unlink(missing_ok=True)
    sidecar(path, "-wal").unlink(missing_ok=True)
    sidecar(path, "-shm").unlink(missing_ok=True)


def archive_run(live: Path, archive: Path) -> tuple[str, int]:
    if not live.is_file():
        return "", 0

    checkpoint(live)
    archive.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    conn = sqlite3.connect(str(archive))
    try:
        init_archive(conn)
        conn.execute("ATTACH DATABASE ? AS live", (str(live.resolve()),))
        if not table_exists(conn, "live", "probes"):
            conn.execute("DETACH DATABASE live")
            conn.close()
            remove_live(live)
            return run_id, 0
        live_cols = {row[1] for row in conn.execute("PRAGMA live.table_info(probes)")}
        host_sel = "host_uptime_s" if "host_uptime_s" in live_cols else "NULL"
        conn.execute(
            f"""
            INSERT INTO probes (run_id, ts, id, ok, http_status, latency_ms, selected, error, host_uptime_s)
            SELECT ?, ts, id, ok, http_status, latency_ms, selected, error, {host_sel}
            FROM live.probes
            """,
            (run_id,),
        )
        copied = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE live")
    finally:
        conn.close()

    remove_live(live)
    return run_id, int(copied)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", default=str(LIVE_DEFAULT), help="current run sqlite")
    parser.add_argument("--archive", default=str(ARCHIVE_DEFAULT), help="historical sqlite")
    args = parser.parse_args()
    live = Path(args.live)
    archive = Path(args.archive)
    if not live.is_file():
        print("archive_probes: no live db, skip", file=sys.stderr)
        return
    run_id, copied = archive_run(live, archive)
    print(
        f"archive_probes: moved {copied} rows to {archive} (run_id={run_id})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
