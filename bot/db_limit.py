"""Keep probe sqlite files under a disk cap without compressing them.

The live database stays <= 500 MiB and the archive of older rows stays <= 2 GiB.
Rows are deleted; the files are not gzipped and not VACUUMed on the hot path.
SQLite reuses free pages, so once a file reaches its high-water mark it stops
growing. A file that is already over the cap is rewritten once, still uncompressed,
because SQLite does not return free pages to the disk otherwise.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LIVE_MAX_BYTES = 500 * 1024 * 1024
ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
# Bound each hot-path delete so a probe append does not stall the bot for long.
SHED_BATCH_ROWS = 20_000

_LIVE_COLUMNS = (
    "ts, id, ok, http_status, latency_ms, selected, error, host_uptime_s"
)


def data_file_bytes(path: Path) -> int:
    """Bytes of the sqlite file plus its WAL and shared-memory sidecars."""
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def live_soft(limit: int) -> int:
    """Main-file size where the live db starts dropping the oldest rows."""
    margin = min(32 * 1024 * 1024, max(4096, limit // 10))
    return max(1, limit - margin)


def live_target(limit: int) -> int:
    """Used bytes to keep after a shed, leaving a free-page pool inside the file."""
    margin = min(96 * 1024 * 1024, max(8192, limit // 4))
    soft = live_soft(limit)
    target = max(1, limit - margin)
    if target >= soft:
        target = max(1, soft - max(4096, soft // 5))
    return target


def archive_fill(limit: int) -> int:
    """How much archived row data to keep so the file does not grow past `limit`."""
    reserve = min(32 * 1024 * 1024, max(8192, limit // 20))
    return max(1, limit - reserve)


def _pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL").fetchall()
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def _table_names(conn: sqlite3.Connection, schema: str) -> set[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def table_exists(conn: sqlite3.Connection, schema: str, name: str) -> bool:
    return name in _table_names(conn, schema)


def _column_names(conn: sqlite3.Connection, schema: str, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_host_uptime(conn: sqlite3.Connection, schema: str) -> None:
    if not table_exists(conn, schema, "probes"):
        return
    if "host_uptime_s" in _column_names(conn, schema, "probes"):
        return
    qual = "probes" if schema == "main" else f"{schema}.probes"
    conn.execute(f"ALTER TABLE {qual} ADD COLUMN host_uptime_s REAL")


def _create_probes(conn: sqlite3.Connection, schema: str, *, with_run_id: bool) -> None:
    qual = "probes" if schema == "main" else f"{schema}.probes"
    run_col = "run_id TEXT NOT NULL," if with_run_id else ""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {qual} (
          rowid INTEGER PRIMARY KEY AUTOINCREMENT,
          {run_col}
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
    _ensure_host_uptime(conn, schema)


def _create_index(conn: sqlite3.Connection, schema: str, name: str, columns: str) -> None:
    # Schema qualifies the index. Qualifying the table ("arch.probes") is a syntax error.
    index = name if schema == "main" else f"{schema}.{name}"
    conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON probes({columns})")


def init_live(conn: sqlite3.Connection) -> None:
    _pragmas(conn)
    _create_probes(conn, "main", with_run_id=False)
    _create_index(conn, "main", "probes_id_ts", "id, ts")
    conn.commit()


def init_archive(conn: sqlite3.Connection) -> None:
    _pragmas(conn)
    _create_probes(conn, "main", with_run_id=True)
    _create_index(conn, "main", "probes_run_id_ts", "run_id, ts")
    _create_index(conn, "main", "probes_id_ts", "id, ts")
    conn.commit()


def _init_attached_archive(conn: sqlite3.Connection, schema: str) -> None:
    conn.execute(f"PRAGMA {schema}.journal_mode=WAL").fetchall()
    conn.execute(f"PRAGMA {schema}.synchronous=NORMAL")
    _create_probes(conn, schema, with_run_id=True)
    _create_index(conn, schema, "probes_run_id_ts", "run_id, ts")
    _create_index(conn, schema, "probes_id_ts", "id, ts")
    conn.commit()


def _page_stats(conn: sqlite3.Connection, schema: str = "main") -> tuple[int, int, int]:
    prefix = "" if schema == "main" else f"{schema}."
    page_size = int(conn.execute(f"PRAGMA {prefix}page_size").fetchone()[0])
    page_count = int(conn.execute(f"PRAGMA {prefix}page_count").fetchone()[0])
    freelist = int(conn.execute(f"PRAGMA {prefix}freelist_count").fetchone()[0])
    if page_size <= 0:
        page_size = 4096
    return page_size, page_count, freelist


def used_bytes(conn: sqlite3.Connection, schema: str = "main") -> int:
    page_size, page_count, freelist = _page_stats(conn, schema)
    return max(0, page_count - freelist) * page_size


def _main_file_bytes(conn: sqlite3.Connection, schema: str = "main") -> int:
    page_size, page_count, _freelist = _page_stats(conn, schema)
    return page_count * page_size


def _free_bytes(conn: sqlite3.Connection, schema: str = "main") -> int:
    page_size, _page_count, freelist = _page_stats(conn, schema)
    return freelist * page_size


def _checkpoint(conn: sqlite3.Connection, schema: str = "main") -> None:
    prefix = "" if schema == "main" else f"{schema}."
    conn.execute(f"PRAGMA {prefix}wal_checkpoint(TRUNCATE)").fetchall()


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _rows_for_excess(count: int, used: int, excess: int) -> int:
    if count <= 0 or used <= 0 or excess <= 0:
        return 0
    average = max(used / count, 1.0)
    return min(count, max(1, int(excess / average) + 1))


def _trim_to_used(
    conn: sqlite3.Connection,
    target: int,
    *,
    table: str = "probes",
    schema: str = "main",
) -> int:
    deleted = 0
    for _ in range(16):
        used = used_bytes(conn, schema)
        if used <= target:
            break
        count = _count_rows(conn, table)
        if count <= 0:
            break
        batch = _rows_for_excess(count, used, used - target)
        if batch <= 0:
            break
        before = count
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {table} ORDER BY rowid LIMIT ?)",
            (batch,),
        )
        removed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        deleted += removed
        if removed <= 0 or _count_rows(conn, table) >= before:
            break
    return deleted


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def pack_database(path: Path, *, archive: bool) -> None:
    """Rewrite `path` so free pages are not kept. Rows are copied uncompressed."""
    if not path.is_file():
        return
    before = data_file_bytes(path)
    tmp = Path(str(path) + ".repack")
    _remove_sidecars(tmp)
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(str(tmp), timeout=30.0)
    try:
        if archive:
            init_archive(dst)
            columns = f"run_id, {_LIVE_COLUMNS}"
        else:
            init_live(dst)
            columns = _LIVE_COLUMNS
        dst.execute("ATTACH DATABASE ? AS srcdb", (str(path.resolve()),))
        try:
            if table_exists(dst, "srcdb", "probes"):
                dst.execute(
                    f"INSERT INTO probes ({columns}) "
                    f"SELECT {columns} FROM srcdb.probes ORDER BY rowid"
                )
            dst.commit()
        finally:
            try:
                dst.execute("DETACH DATABASE srcdb")
            except sqlite3.Error:
                pass
        _checkpoint(dst)
    except Exception:
        dst.close()
        _remove_sidecars(tmp)
        tmp.unlink(missing_ok=True)
        raise
    dst.close()
    _remove_sidecars(tmp)
    _remove_sidecars(path)
    tmp.replace(path)
    after = data_file_bytes(path)
    print(
        f"db_limit: rewrote {path.name} {before} -> {after} bytes, no compression",
        file=sys.stderr,
    )


def _host_expr(conn: sqlite3.Connection, schema: str) -> str:
    if "host_uptime_s" in _column_names(conn, schema, "probes"):
        return "host_uptime_s"
    return "NULL"


def _spill_oldest(
    conn: sqlite3.Connection,
    archive_path: Path,
    drop_n: int,
    archive_max: int,
) -> None:
    """Move the newest part of the oldest `drop_n` live rows into the archive."""
    if drop_n <= 0:
        return
    count = _count_rows(conn, "probes")
    drop_n = min(drop_n, count)
    if drop_n <= 0:
        return
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    used = used_bytes(conn)
    average = max(used / count, 1.0) if count else 1.0
    fill = archive_fill(archive_max)
    drop_bytes = int(drop_n * average)
    keep_n = drop_n if drop_bytes <= fill else min(drop_n, max(1, int(fill / average)))
    old_keep = _old_keep(fill, int(keep_n * average))
    skip = drop_n - keep_n
    run_id = datetime.now(timezone.utc).strftime("spill-%Y%m%dT%H%M%S.%fZ")

    conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path.resolve()),))
    archived = 0
    try:
        _init_attached_archive(conn, "arch")
        dropped_arch = _trim_to_used(conn, old_keep, table="arch.probes", schema="arch")
        if keep_n > 0:
            cursor = conn.execute(
                f"""
                INSERT INTO arch.probes (run_id, {_LIVE_COLUMNS})
                SELECT ?, {_LIVE_COLUMNS}
                FROM main.probes
                ORDER BY rowid
                LIMIT ? OFFSET ?
                """,
                (run_id, keep_n, skip),
            )
            archived = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else keep_n
        conn.execute(
            """
            DELETE FROM main.probes
            WHERE rowid IN (SELECT rowid FROM main.probes ORDER BY rowid LIMIT ?)
            """,
            (drop_n,),
        )
        conn.commit()
        _checkpoint(conn, "arch")
        _checkpoint(conn)
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            conn.execute("DETACH DATABASE arch")
        except sqlite3.Error:
            pass

    print(
        f"db_limit: moved {archived} oldest live rows into {archive_path.name} "
        f"and dropped {drop_n} from live"
        + (f"; archive dropped {dropped_arch} older rows" if dropped_arch else ""),
        file=sys.stderr,
    )
    if data_file_bytes(archive_path) > archive_max:
        cap_archive_file(archive_path, archive_max)


def _drop_count(conn: sqlite3.Connection, target: int) -> int:
    file_bytes = _main_file_bytes(conn)
    free = _free_bytes(conn)
    used = max(0, file_bytes - free)
    if used <= target:
        return 0
    count = _count_rows(conn, "probes")
    return _rows_for_excess(count, used, used - target)


def _measure(conn: sqlite3.Connection, path: Path) -> tuple[int, int, int, int]:
    file_bytes = _main_file_bytes(conn)
    free = _free_bytes(conn)
    disk = data_file_bytes(path)
    return file_bytes, free, disk, max(0, file_bytes - free)


def _old_keep(fill: int, incoming_bytes: int) -> int:
    """How many already-archived bytes to retain beside `incoming_bytes` of new rows."""
    slack = min(64 * 1024, max(4096, fill // 32))
    return max(0, fill - min(fill, max(0, incoming_bytes) + slack))


def enforce_live_cap(
    conn: sqlite3.Connection,
    path: Path,
    archive_path: Path,
    *,
    shedding: bool,
    max_rows: int | None = None,
    live_max: int = LIVE_MAX_BYTES,
    archive_max: int = ARCHIVE_MAX_BYTES,
) -> tuple[bool, bool]:
    """Shed oldest live rows until the file can stay within `live_max`.

    Returns `(still_shedding, needs_pack)`. On `needs_pack` the caller closes
    `conn`, calls `pack_database`, and opens the file again. The connection is
    left open here.

    Shedding starts only once the main file has reached its high-water mark and
    the free-page pool is nearly empty, then deletes down to `live_target`.
    That keeps a gap of reusable pages so ordinary inserts do not allocate and
    do not repack the file.
    """
    soft = live_soft(live_max)
    target = live_target(live_max)
    reserve = max(1, soft - target)
    file_bytes, free, disk, used = _measure(conn, path)

    wal = Path(str(path) + "-wal")
    try:
        wal_bytes = wal.stat().st_size
    except OSError:
        wal_bytes = 0
    if disk > live_max or wal_bytes > 1024 * 1024:
        _checkpoint(conn)
        file_bytes, free, disk, used = _measure(conn, path)

    at_ceiling = file_bytes >= soft or disk > live_max
    if not shedding and not at_ceiling:
        return False, False

    pool_low = free < max(4096, reserve // 4)
    started = False
    if used > target and (shedding or (at_ceiling and pool_low)):
        drop_n = _drop_count(conn, target)
        if max_rows is not None:
            drop_n = min(drop_n, max_rows)
        if drop_n > 0:
            _spill_oldest(conn, archive_path, drop_n, archive_max)
            started = True
        file_bytes, free, disk, used = _measure(conn, path)

    if started and used > target and (file_bytes >= soft or disk > live_max):
        return True, False
    try:
        main_disk = path.stat().st_size
    except OSError:
        main_disk = 0
    if main_disk > live_max and used <= live_max:
        return False, True
    return False, False


def _insert_src_newest(
    conn: sqlite3.Connection,
    run_id: str,
    archive_max: int,
) -> int:
    count = _count_rows(conn, "src.probes")
    if count <= 0:
        return 0
    src_used = used_bytes(conn, "src")
    average = max(src_used / count, 1.0)
    fill = archive_fill(archive_max)
    fit_n = count if src_used <= fill else min(count, max(1, int(fill / average)))
    old_keep = _old_keep(fill, int(fit_n * average))
    _trim_to_used(conn, old_keep, table="probes", schema="main")
    offset = count - fit_n
    host = _host_expr(conn, "src")
    cursor = conn.execute(
        f"""
        INSERT INTO main.probes (run_id, {_LIVE_COLUMNS})
        SELECT ?, ts, id, ok, http_status, latency_ms, selected, error, {host}
        FROM src.probes
        ORDER BY rowid
        LIMIT ? OFFSET ?
        """,
        (run_id, fit_n, offset),
    )
    if cursor.rowcount and cursor.rowcount > 0:
        return int(cursor.rowcount)
    return fit_n


def transfer_live_into_archive(
    live_path: Path,
    archive_path: Path,
    run_id: str,
    archive_max: int = ARCHIVE_MAX_BYTES,
) -> int:
    """Append a live probes db to the archive, then enforce the archive disk cap."""
    if not live_path.is_file():
        return 0
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(archive_path), timeout=30.0)
    copied = 0
    try:
        init_archive(conn)
        conn.execute("ATTACH DATABASE ? AS src", (str(live_path.resolve()),))
        try:
            if table_exists(conn, "src", "probes"):
                copied = _insert_src_newest(conn, run_id, archive_max)
                conn.commit()
                _checkpoint(conn)
        finally:
            try:
                conn.execute("DETACH DATABASE src")
            except sqlite3.Error:
                pass
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    cap_archive_file(archive_path, archive_max)
    return copied


def cap_archive_file(path: Path, archive_max: int = ARCHIVE_MAX_BYTES) -> None:
    """Drop the oldest archived rows and, if the file is already too big, rewrite it once."""
    if not path.is_file():
        return
    for _ in range(2):
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            init_archive(conn)
            deleted = _trim_to_used(conn, archive_max, table="probes", schema="main")
            conn.commit()
            _checkpoint(conn)
            used = used_bytes(conn)
        finally:
            conn.close()
        if deleted:
            print(
                f"db_limit: dropped {deleted} oldest rows from {path.name}",
                file=sys.stderr,
            )
        disk = data_file_bytes(path)
        if disk <= archive_max or used > archive_max:
            return
        pack_database(path, archive=True)
