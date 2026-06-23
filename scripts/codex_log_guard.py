#!/usr/bin/env python3
"""Inspect and safely filter TRACE/DEBUG rows in Codex logs_2.sqlite."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


TRIGGER_NAME = "codex_drop_trace_debug_logs"
EXPECTED_COLUMNS = {
    "id",
    "ts",
    "ts_nanos",
    "level",
    "target",
    "feedback_log_body",
    "estimated_bytes",
}
TRIGGER_SQL = f"""CREATE TRIGGER {TRIGGER_NAME}
BEFORE INSERT ON logs
FOR EACH ROW
WHEN upper(NEW.level) IN ('TRACE', 'DEBUG')
BEGIN
    SELECT RAISE(IGNORE);
END"""


class GuardError(RuntimeError):
    pass


def db_uri(path: Path, mode: str = "ro") -> str:
    resolved = path.expanduser().resolve().as_posix()
    return f"file:{quote(resolved, safe='/:')}?mode={mode}"


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_uri(path, "ro"), uri=True, timeout=5)


def connect_rw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=30, isolation_level=None)


def normalize_sql(sql: str | None) -> str:
    return " ".join((sql or "").lower().replace(";", "").split())


def file_state(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mtime_local": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        }
    except FileNotFoundError:
        return None


def require_database(path: Path) -> None:
    if not path.is_file():
        raise GuardError(f"database not found: {path}")


def schema_state(conn: sqlite3.Connection) -> dict[str, Any]:
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    columns = {
        row[1]: row[2] for row in conn.execute('PRAGMA table_info("logs")').fetchall()
    }
    triggers = [
        {"name": row[0], "table": row[1], "sql": row[2]}
        for row in conn.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master "
            "WHERE type='trigger' ORDER BY name"
        )
    ]
    return {
        "quick_check": quick,
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": conn.execute("PRAGMA schema_version").fetchone()[0],
        "columns": columns,
        "missing_expected_columns": sorted(EXPECTED_COLUMNS - set(columns)),
        "triggers": triggers,
    }


def log_snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    count, max_id = conn.execute(
        "SELECT count(*),coalesce(max(id),0) FROM logs"
    ).fetchone()
    seq_row = conn.execute(
        "SELECT coalesce(seq,0) FROM sqlite_sequence WHERE name='logs'"
    ).fetchone()
    return {"count": count, "max_id": max_id, "sequence": seq_row[0] if seq_row else 0}


def level_counts(conn: sqlite3.Connection, cutoff: int | None = None) -> list[dict[str, Any]]:
    if cutoff is None:
        rows = conn.execute(
            "SELECT level,count(*),coalesce(sum(estimated_bytes),0) "
            "FROM logs GROUP BY level ORDER BY count(*) DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT level,count(*),coalesce(sum(estimated_bytes),0) "
            "FROM logs WHERE ts>=? GROUP BY level ORDER BY count(*) DESC",
            (cutoff,),
        ).fetchall()
    return [
        {"level": level, "rows": count, "estimated_bytes": size}
        for level, count, size in rows
    ]


def inspect(path: Path, sample_seconds: float) -> dict[str, Any]:
    require_database(path)
    conn = connect_ro(path)
    try:
        schema = schema_state(conn)
        if not schema["columns"]:
            raise GuardError("table 'logs' was not found")
        before = log_snapshot(conn)
        all_levels = level_counts(conn)
        recent_levels = level_counts(conn, int(time.time()) - 3600)
    finally:
        conn.close()

    files_before = {
        suffix or "main": file_state(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-shm")
    }
    started = time.time()
    if sample_seconds > 0:
        time.sleep(sample_seconds)

    conn = connect_ro(path)
    try:
        after = log_snapshot(conn)
        new_levels = [
            {"level": level, "rows": count, "estimated_bytes": size}
            for level, count, size in conn.execute(
                "SELECT level,count(*),coalesce(sum(estimated_bytes),0) "
                "FROM logs WHERE id>? GROUP BY level ORDER BY count(*) DESC",
                (before["max_id"],),
            )
        ]
    finally:
        conn.close()

    elapsed = max(time.time() - started, 1e-9)
    files_after = {
        suffix or "main": file_state(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-shm")
    }
    file_deltas: dict[str, Any] = {}
    for key in files_before:
        old, new = files_before[key], files_after[key]
        file_deltas[key] = {
            "before": old,
            "after": new,
            "size_delta": (
                new["size_bytes"] - old["size_bytes"] if old and new else None
            ),
            "mtime_changed": (
                new["mtime_ns"] != old["mtime_ns"] if old and new else None
            ),
        }

    td_rows = sum(
        row["rows"] for row in new_levels if row["level"].upper() in {"TRACE", "DEBUG"}
    )
    return {
        "database": str(path),
        "schema": schema,
        "all_levels": all_levels,
        "last_hour_levels": recent_levels,
        "sample": {
            "seconds": round(elapsed, 3),
            "before": before,
            "after": after,
            "visible_row_delta": after["count"] - before["count"],
            "max_id_delta": after["max_id"] - before["max_id"],
            "sequence_delta": after["sequence"] - before["sequence"],
            "new_levels": new_levels,
            "trace_debug_rows": td_rows,
            "trace_debug_rows_per_second": round(td_rows / elapsed, 3),
            "files": file_deltas,
        },
    }


def validate_for_change(conn: sqlite3.Connection) -> dict[str, Any]:
    state = schema_state(conn)
    if state["quick_check"] != "ok":
        raise GuardError(f"PRAGMA quick_check failed: {state['quick_check']}")
    if state["missing_expected_columns"]:
        raise GuardError(
            "unsupported logs schema; missing columns: "
            + ", ".join(state["missing_expected_columns"])
        )
    return state


def online_backup(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = backup_dir / f"logs_2_before_log_guard_{stamp}.sqlite"
    suffix = 1
    while destination.exists():
        destination = backup_dir / f"logs_2_before_log_guard_{stamp}_{suffix}.sqlite"
        suffix += 1

    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=1024, sleep=0.01)
        dst.commit()
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise GuardError(f"backup quick_check failed: {check}")
    except Exception:
        dst.close()
        src.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        dst.close()
        src.close()
    return destination


def trigger_row(conn: sqlite3.Connection) -> tuple[str, str, str] | None:
    return conn.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master "
        "WHERE type='trigger' AND name=?",
        (TRIGGER_NAME,),
    ).fetchone()


def install(path: Path, backup_dir: Path, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise GuardError("installation requires explicit --yes")
    require_database(path)
    conn = connect_rw(path)
    try:
        state = validate_for_change(conn)
        existing = trigger_row(conn)
        if existing:
            if normalize_sql(existing[2]) == normalize_sql(TRIGGER_SQL):
                return {"status": "already_installed", "trigger": TRIGGER_NAME}
            raise GuardError("same trigger name exists with unexpected SQL")
        conflicts = [
            row["name"] for row in state["triggers"] if row["table"].lower() == "logs"
        ]
        if conflicts:
            raise GuardError(
                "refusing to combine with existing logs triggers: " + ", ".join(conflicts)
            )
    finally:
        conn.close()

    backup = online_backup(path, backup_dir)
    conn = connect_rw(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        validate_for_change(conn)
        if trigger_row(conn):
            raise GuardError("trigger appeared concurrently; no change made")
        conn.execute(TRIGGER_SQL)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    result = transactional_self_test(path)
    return {
        "status": "installed",
        "trigger": TRIGGER_NAME,
        "backup": str(backup),
        "self_test": result,
    }


def uninstall(path: Path, backup_dir: Path, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise GuardError("uninstallation requires explicit --yes")
    require_database(path)
    conn = connect_rw(path)
    try:
        validate_for_change(conn)
        existing = trigger_row(conn)
        if not existing:
            return {"status": "not_installed", "trigger": TRIGGER_NAME}
        if normalize_sql(existing[2]) != normalize_sql(TRIGGER_SQL):
            raise GuardError("refusing to remove a trigger with unexpected SQL")
    finally:
        conn.close()

    backup = online_backup(path, backup_dir)
    conn = connect_rw(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = trigger_row(conn)
        if not existing or normalize_sql(existing[2]) != normalize_sql(TRIGGER_SQL):
            raise GuardError("trigger changed concurrently; no change made")
        conn.execute(f'DROP TRIGGER "{TRIGGER_NAME}"')
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return {"status": "uninstalled", "trigger": TRIGGER_NAME, "backup": str(backup)}


def transactional_self_test(path: Path) -> dict[str, Any]:
    require_database(path)
    conn = connect_rw(path)
    marker = f"codex_log_guard_selftest_{uuid.uuid4().hex}"
    try:
        validate_for_change(conn)
        existing = trigger_row(conn)
        if not existing or normalize_sql(existing[2]) != normalize_sql(TRIGGER_SQL):
            raise GuardError("expected trigger is not installed exactly")
        conn.execute("BEGIN IMMEDIATE")
        results: dict[str, int] = {}
        for level in ("TRACE", "DEBUG", "INFO", "WARN", "ERROR"):
            conn.execute(
                "INSERT INTO logs("
                "ts,ts_nanos,level,target,feedback_log_body,estimated_bytes"
                ") VALUES(?,?,?,?,?,?)",
                (
                    int(time.time()),
                    0,
                    level,
                    marker,
                    "temporary self-test; transaction will be rolled back",
                    64,
                ),
            )
            results[level] = conn.execute("SELECT changes()").fetchone()[0]
        visible = dict(
            conn.execute(
                "SELECT level,count(*) FROM logs WHERE target=? GROUP BY level",
                (marker,),
            ).fetchall()
        )
        conn.execute("ROLLBACK")
        leftovers = conn.execute(
            "SELECT count(*) FROM logs WHERE target=?", (marker,)
        ).fetchone()[0]
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    passed = (
        results == {"TRACE": 0, "DEBUG": 0, "INFO": 1, "WARN": 1, "ERROR": 1}
        and visible == {"ERROR": 1, "INFO": 1, "WARN": 1}
        and leftovers == 0
        and check == "ok"
    )
    if not passed:
        raise GuardError(
            f"self-test failed: results={results}, visible={visible}, "
            f"leftovers={leftovers}, quick_check={check}"
        )
    return {
        "passed": True,
        "insert_changes": results,
        "visible_inside_transaction": visible,
        "leftovers_after_rollback": leftovers,
        "quick_check": check,
    }


def make_plan(report: dict[str, Any]) -> dict[str, Any]:
    schema = report["schema"]
    sample = report["sample"]
    installed = any(
        row["name"] == TRIGGER_NAME for row in schema["triggers"]
    )
    blockers = []
    if schema["quick_check"] != "ok":
        blockers.append("database quick_check is not ok")
    if schema["missing_expected_columns"]:
        blockers.append("logs schema is unsupported")
    other = [
        row["name"]
        for row in schema["triggers"]
        if row["table"].lower() == "logs" and row["name"] != TRIGGER_NAME
    ]
    if other:
        blockers.append("other triggers target logs: " + ", ".join(other))
    rate = sample["trace_debug_rows_per_second"]
    if installed:
        recommendation = "keep trigger and run self-test; uninstall only if diagnostics are needed"
    elif blockers:
        recommendation = "do not install; resolve blockers or use an official logging control"
    elif rate > 1:
        recommendation = "install after explicit approval; high-rate TRACE/DEBUG writes observed"
    elif rate > 0:
        recommendation = "consider installation after a longer representative sample"
    else:
        recommendation = "do not install from this sample alone; no TRACE/DEBUG activity observed"
    return {
        "recommendation": recommendation,
        "blockers": blockers,
        "trigger_installed": installed,
        "observed_trace_debug_rows_per_second": rate,
        "effects": [
            "drops only TRACE and DEBUG",
            "retains INFO, WARN, and ERROR",
            "does not shrink existing files",
            "retained levels and WAL maintenance can still write",
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "action", choices=("inspect", "plan", "install", "uninstall", "self-test")
    )
    result.add_argument(
        "--db", type=Path, default=Path.home() / ".codex" / "logs_2.sqlite"
    )
    result.add_argument("--sample-seconds", type=float, default=0)
    result.add_argument(
        "--backup-dir", type=Path, default=Path.home() / ".codex" / "backups"
    )
    result.add_argument("--yes", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "inspect":
            output = inspect(args.db, args.sample_seconds)
        elif args.action == "plan":
            report = inspect(args.db, args.sample_seconds)
            output = {"inspection": report, "plan": make_plan(report)}
        elif args.action == "install":
            output = install(args.db, args.backup_dir, args.yes)
            if args.sample_seconds > 0:
                output["live_sample"] = inspect(args.db, args.sample_seconds)["sample"]
        elif args.action == "uninstall":
            output = uninstall(args.db, args.backup_dir, args.yes)
        else:
            output = {"transactional": transactional_self_test(args.db)}
            if args.sample_seconds > 0:
                output["live_sample"] = inspect(args.db, args.sample_seconds)["sample"]
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (GuardError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
