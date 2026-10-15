#!/usr/bin/env python3
"""Move the current probes.db run into results/archive.db, then clear the live db.

The archive stays uncompressed and at most 2 GiB on disk; older rows are deleted.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bot"))

from db_limit import cap_archive_file, transfer_live_into_archive  # noqa: E402

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


def readonly_snapshot(path: Path) -> Path:
    """Copy a db the current user can read but not write (typically root:644)."""
    snap = sidecar(path, ".archive-snap")
    snap.unlink(missing_ok=True)
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    dst = sqlite3.connect(str(snap))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        src.close()
        dst.close()
    return snap


def remove_live(path: Path) -> None:
    for target in (path, sidecar(path, "-wal"), sidecar(path, "-shm")):
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"archive_probes: rows were copied, but could not delete {target}: {exc}"
            ) from exc


def archive_run(live: Path, archive: Path) -> tuple[str, int]:
    if not live.is_file():
        return "", 0

    source = live
    snap: Path | None = None
    try:
        checkpoint(live)
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).lower():
            raise
        print(
            "archive_probes: live db is not writable, copying read-only",
            file=sys.stderr,
        )
        source = readonly_snapshot(live)
        snap = source
    archive.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    try:
        copied = transfer_live_into_archive(source, archive, run_id)
    finally:
        if snap is not None:
            snap.unlink(missing_ok=True)

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
        if archive.is_file():
            cap_archive_file(archive)
        print("archive_probes: no live db, skip", file=sys.stderr)
        return
    run_id, copied = archive_run(live, archive)
    print(
        f"archive_probes: moved {copied} rows to {archive} (run_id={run_id})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
