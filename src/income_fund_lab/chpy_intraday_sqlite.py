from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from income_fund_lab.collectors import fetch_url, parse_chpy_holdings_csv, parse_chpy_trade_csv, validate_response

DEFAULT_DB = "data/chpy_intraday.sqlite3"
TRADE_URL = "https://yieldmaxetfs.com/ym/intraday-file"
HOLDINGS_URL = "https://yieldmaxetfs.com/wp-content/uploads/funds/CHPY/TidalFG_Holdings_CHPY.csv"


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    minimum_records: int


FEEDS = (Feed("intraday_trades", TRADE_URL, 1), Feed("holdings", HOLDINGS_URL, 10))


class SourceValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _row_hash(raw_sha256: str, source_row_number: int) -> str:
    return hashlib.sha256(f"{raw_sha256}:{source_row_number}".encode()).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    if definition.split()[0] not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create provenance tables and migrate legacy rows without dropping data."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
        id INTEGER PRIMARY KEY, started_at_utc TEXT NOT NULL, completed_at_utc TEXT,
        status TEXT NOT NULL CHECK (status IN ('running','success','incomplete')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS raw_sources (
        id INTEGER PRIMARY KEY, feed_type TEXT NOT NULL, source_url TEXT NOT NULL,
        fetched_at_utc TEXT NOT NULL, reported_source_date TEXT, record_count INTEGER NOT NULL,
        status TEXT NOT NULL, sha256 TEXT NOT NULL, archive_path TEXT, metadata_path TEXT,
        UNIQUE(feed_type, sha256))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS collection_feed_runs (
        id INTEGER PRIMARY KEY, collection_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        feed_type TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at_utc TEXT NOT NULL,
        reported_source_date TEXT, record_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
        raw_source_id INTEGER REFERENCES raw_sources(id), raw_sha256 TEXT,
        validation_errors TEXT NOT NULL DEFAULT '[]')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holdings_snapshots (
        id INTEGER PRIMARY KEY, raw_source_id INTEGER NOT NULL UNIQUE REFERENCES raw_sources(id),
        source_as_of_date TEXT NOT NULL, version INTEGER NOT NULL,
        is_current INTEGER NOT NULL CHECK(is_current IN (0,1)), superseded_at_utc TEXT,
        UNIQUE(source_as_of_date, version))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chpy_intraday_trades (
        row_hash TEXT PRIMARY KEY, trade_date TEXT NOT NULL, fund TEXT NOT NULL, ticker TEXT NOT NULL,
        side TEXT, raw_type TEXT, quantity INTEGER, price REAL, fetched_at_utc TEXT NOT NULL,
        source_url TEXT NOT NULL, raw_source_id INTEGER, source_row_number INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chpy_holdings_daily (
        row_hash TEXT PRIMARY KEY, source_as_of_date TEXT NOT NULL, fund TEXT NOT NULL, ticker TEXT,
        cusip TEXT, security_name TEXT NOT NULL, asset_type TEXT, quantity REAL, market_value REAL,
        portfolio_weight REAL, option_underlying TEXT, option_type TEXT, option_strike REAL,
        option_expiration TEXT, fetched_at_utc TEXT NOT NULL, source_url TEXT NOT NULL,
        raw_source_id INTEGER, snapshot_id INTEGER, source_row_number INTEGER)""")
    for definition in ("raw_source_id INTEGER", "source_row_number INTEGER"):
        _add_column(conn, "chpy_intraday_trades", definition)
    for definition in ("raw_source_id INTEGER", "snapshot_id INTEGER", "source_row_number INTEGER"):
        _add_column(conn, "chpy_holdings_daily", definition)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chpy_intraday_trades_date ON chpy_intraday_trades(trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chpy_intraday_raw_source ON chpy_intraday_trades(raw_source_id,source_row_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chpy_holdings_daily_date ON chpy_holdings_daily(source_as_of_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chpy_holdings_snapshot ON chpy_holdings_daily(snapshot_id,source_row_number)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_current_holdings_date ON holdings_snapshots(source_as_of_date) WHERE is_current=1")
    _migrate_legacy_rows(conn)
    conn.commit()


def _legacy_raw_source(conn: sqlite3.Connection, feed: str, url: str, fetched: str, date: str, count: int) -> int:
    digest = "legacy-" + hashlib.sha256(f"{feed}|{url}|{fetched}|{date}".encode()).hexdigest()
    conn.execute("""INSERT OR IGNORE INTO raw_sources
        (feed_type,source_url,fetched_at_utc,reported_source_date,record_count,status,sha256)
        VALUES (?,?,?,?,?,'imported_legacy',?)""", (feed, url, fetched, date, count, digest))
    return conn.execute("SELECT id FROM raw_sources WHERE feed_type=? AND sha256=?", (feed, digest)).fetchone()[0]


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    groups = conn.execute("""SELECT source_url,fetched_at_utc,trade_date,count(*) FROM chpy_intraday_trades
        WHERE raw_source_id IS NULL GROUP BY source_url,fetched_at_utc,trade_date""").fetchall()
    for url, fetched, date, count in groups:
        raw_id = _legacy_raw_source(conn, "intraday_trades", url, fetched, date, count)
        rows = conn.execute("""SELECT rowid FROM chpy_intraday_trades WHERE raw_source_id IS NULL
            AND source_url=? AND fetched_at_utc=? AND trade_date=? ORDER BY rowid""", (url, fetched, date)).fetchall()
        for number, (rowid,) in enumerate(rows, 1):
            conn.execute("UPDATE chpy_intraday_trades SET raw_source_id=?,source_row_number=? WHERE rowid=?", (raw_id, number, rowid))
    groups = conn.execute("""SELECT source_url,fetched_at_utc,source_as_of_date,count(*) FROM chpy_holdings_daily
        WHERE snapshot_id IS NULL GROUP BY source_url,fetched_at_utc,source_as_of_date ORDER BY source_as_of_date,fetched_at_utc""").fetchall()
    for url, fetched, date, count in groups:
        raw_id = _legacy_raw_source(conn, "holdings", url, fetched, date, count)
        found = conn.execute("SELECT id FROM holdings_snapshots WHERE raw_source_id=?", (raw_id,)).fetchone()
        if found:
            snapshot_id = found[0]
        else:
            version = conn.execute("SELECT coalesce(max(version),0)+1 FROM holdings_snapshots WHERE source_as_of_date=?", (date,)).fetchone()[0]
            conn.execute("UPDATE holdings_snapshots SET is_current=0,superseded_at_utc=? WHERE source_as_of_date=? AND is_current=1", (fetched, date))
            snapshot_id = conn.execute("INSERT INTO holdings_snapshots(raw_source_id,source_as_of_date,version,is_current) VALUES (?,?,?,1)", (raw_id, date, version)).lastrowid
        rows = conn.execute("""SELECT rowid FROM chpy_holdings_daily WHERE snapshot_id IS NULL
            AND source_url=? AND fetched_at_utc=? AND source_as_of_date=? ORDER BY rowid""", (url, fetched, date)).fetchall()
        for number, (rowid,) in enumerate(rows, 1):
            conn.execute("UPDATE chpy_holdings_daily SET raw_source_id=?,snapshot_id=?,source_row_number=? WHERE rowid=?", (raw_id, snapshot_id, number, rowid))


def validate_records(feed_type: str, records: list[dict], minimum_records: int) -> str:
    errors: list[str] = []
    if len(records) < minimum_records:
        errors.append(f"expected at least {minimum_records} CHPY records, got {len(records)}")
    date_key = "trade_date" if feed_type == "intraday_trades" else "source_as_of_date"
    dates = {row.get(date_key) for row in records}
    if None in dates or "" in dates:
        errors.append(f"one or more records have no valid {date_key}")
    valid_dates = {value for value in dates if value}
    if len(valid_dates) != 1:
        errors.append(f"records contain inconsistent source dates: {sorted(valid_dates)}")
    for index, row in enumerate(records, 1):
        if str(row.get("fund") or "").upper() != "CHPY":
            errors.append(f"record {index} is not CHPY")
        security = row.get("security") if feed_type == "intraday_trades" else row.get("security_name")
        if not security:
            errors.append(f"record {index} has no security")
        if row.get("quantity") is None:
            errors.append(f"record {index} has no quantity")
        if feed_type == "intraday_trades" and (row.get("price") is None or not row.get("raw_type")):
            errors.append(f"trade record {index} lacks price or original trade code")
    if errors:
        raise SourceValidationError(errors)
    return next(iter(valid_dates))


def _archive_raw(db_path: Path, feed: str, payload: bytes, metadata: dict) -> tuple[Path, Path]:
    digest = metadata["sha256"]
    root = db_path.parent / "raw" / "chpy" / feed / digest[:2]
    raw_path, meta_path = root / f"{digest}.bin", root / f"{digest}.json"
    root.mkdir(parents=True, exist_ok=True)
    if raw_path.exists():
        if _sha256(raw_path.read_bytes()) != digest:
            raise RuntimeError(f"immutable raw archive hash mismatch: {raw_path}")
    else:
        with raw_path.open("xb") as handle:
            handle.write(payload)
        os.chmod(raw_path, 0o444)
    encoded = json.dumps(metadata, indent=2, sort_keys=True).encode()
    if meta_path.exists():
        if meta_path.read_bytes() != encoded:
            raise RuntimeError(f"immutable raw metadata conflict: {meta_path}")
    else:
        with meta_path.open("xb") as handle:
            handle.write(encoded)
        os.chmod(meta_path, 0o444)
    return raw_path, meta_path


def _insert_raw_source(conn: sqlite3.Connection, feed: Feed, fetched: str, date: str | None, records: list[dict], digest: str, status: str, paths: tuple[Path, Path]) -> tuple[int, bool]:
    cursor = conn.execute("""INSERT OR IGNORE INTO raw_sources
        (feed_type,source_url,fetched_at_utc,reported_source_date,record_count,status,sha256,archive_path,metadata_path)
        VALUES (?,?,?,?,?,?,?,?,?)""", (feed.name, feed.url, fetched, date, len(records), status, digest, str(paths[0]), str(paths[1])))
    raw_id = conn.execute("SELECT id FROM raw_sources WHERE feed_type=? AND sha256=?", (feed.name, digest)).fetchone()[0]
    return raw_id, cursor.rowcount == 1


def _matching_legacy_source(conn: sqlite3.Connection, feed: Feed, date: str, records: list[dict]) -> int | None:
    candidates = conn.execute(
        "SELECT id FROM raw_sources WHERE feed_type=? AND status='imported_legacy' AND reported_source_date=? AND record_count=?",
        (feed.name, date, len(records)),
    ).fetchall()
    for (raw_id,) in candidates:
        if feed.name == "intraday_trades":
            stored = conn.execute("""SELECT trade_date,fund,ticker,side,raw_type,quantity,price
                FROM chpy_intraday_trades WHERE raw_source_id=? ORDER BY source_row_number""", (raw_id,)).fetchall()
            parsed = [(row["trade_date"], row["fund"], row["security"], row["side"], row["raw_type"], row["quantity"], row["price"]) for row in records]
        else:
            stored = conn.execute("""SELECT source_as_of_date,fund,ticker,cusip,security_name,quantity,market_value,portfolio_weight
                FROM chpy_holdings_daily WHERE raw_source_id=? ORDER BY source_row_number""", (raw_id,)).fetchall()
            parsed = [(row["source_as_of_date"], row["fund"], row["ticker"], row["CUSIP"], row["security_name"], row["quantity"], row["market_value"], row["portfolio_weight"]) for row in records]
        if stored == parsed:
            return raw_id
    return None


def insert_records(conn: sqlite3.Connection, records: list[dict], source_url: str, table_name: str = "chpy_intraday_trades", *, raw_source_id: int | None = None, raw_sha256: str | None = None, snapshot_id: int | None = None, fetched_at: str | None = None) -> int:
    """Insert each source-row occurrence; identical values at different rows remain distinct."""
    fetched_at = fetched_at or _utc_now()
    digest = raw_sha256 or hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    inserted = 0
    for fallback, row in enumerate(records, 1):
        number = int(row.get("source_row_number") or fallback)
        row_hash = _row_hash(digest, number)
        if table_name == "chpy_intraday_trades":
            cursor = conn.execute("""INSERT OR IGNORE INTO chpy_intraday_trades
                (row_hash,trade_date,fund,ticker,side,raw_type,quantity,price,fetched_at_utc,source_url,raw_source_id,source_row_number)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (row_hash, row["trade_date"], str(row.get("fund") or "CHPY").upper(), str(row.get("security") or "").strip(), row.get("side"), row.get("raw_type"), row.get("quantity"), row.get("price"), fetched_at, source_url, raw_source_id, number))
        elif table_name == "chpy_holdings_daily":
            cursor = conn.execute("""INSERT OR IGNORE INTO chpy_holdings_daily
                (row_hash,source_as_of_date,fund,ticker,cusip,security_name,asset_type,quantity,market_value,portfolio_weight,
                 option_underlying,option_type,option_strike,option_expiration,fetched_at_utc,source_url,raw_source_id,snapshot_id,source_row_number)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (row_hash, row["source_as_of_date"], str(row.get("fund") or "CHPY").upper(), row.get("ticker"), row.get("CUSIP"), row.get("security_name"), row.get("asset_type"), row.get("quantity"), row.get("market_value"), row.get("portfolio_weight"), row.get("option_underlying"), row.get("option_type"), row.get("option_strike"), row.get("option_expiration"), fetched_at, source_url, raw_source_id, snapshot_id, number))
        else:
            raise ValueError(f"unsupported table: {table_name}")
        inserted += cursor.rowcount == 1
    conn.commit()
    return inserted


def _insert_snapshot(conn: sqlite3.Connection, raw_id: int, date: str, fetched: str) -> int:
    found = conn.execute("SELECT id FROM holdings_snapshots WHERE raw_source_id=?", (raw_id,)).fetchone()
    if found:
        return found[0]
    version = conn.execute("SELECT coalesce(max(version),0)+1 FROM holdings_snapshots WHERE source_as_of_date=?", (date,)).fetchone()[0]
    conn.execute("UPDATE holdings_snapshots SET is_current=0,superseded_at_utc=? WHERE source_as_of_date=? AND is_current=1", (fetched, date))
    return conn.execute("INSERT INTO holdings_snapshots(raw_source_id,source_as_of_date,version,is_current) VALUES (?,?,?,1)", (raw_id, date, version)).lastrowid


def _collect_feed(conn: sqlite3.Connection, run_id: int, db_path: Path, feed: Feed, fetcher: Callable) -> dict:
    fetched, payload, digest = _utc_now(), None, None
    raw_id, records, date, errors, inserted, status = None, [], None, [], 0, "failed"
    try:
        response = fetcher(feed.url)
        payload = response.content
        validate_response(response.status_code, payload, feed.url)
        digest = _sha256(payload)
        text = payload.decode("utf-8-sig", errors="strict")
        parsed = parse_chpy_trade_csv(text) if feed.name == "intraday_trades" else parse_chpy_holdings_csv(text)
        records = [row for row in parsed.get("records", []) if str(row.get("fund") or "").upper() == "CHPY"]
        date = validate_records(feed.name, records, feed.minimum_records)
        legacy_id = _matching_legacy_source(conn, feed, date, records)
        if legacy_id is not None:
            metadata = {"feed_type": feed.name, "source_url": feed.url, "fetched_at_utc": fetched,
                        "reported_source_date": date, "record_count": len(records), "status": "validated",
                        "sha256": digest, "validation_errors": []}
            paths = _archive_raw(db_path, feed.name, payload, metadata)
            conn.execute("""UPDATE raw_sources SET source_url=?,fetched_at_utc=?,status='validated',sha256=?,archive_path=?,metadata_path=?
                WHERE id=?""", (feed.url, fetched, digest, str(paths[0]), str(paths[1]), legacy_id))
            row_table = "chpy_intraday_trades" if feed.name == "intraday_trades" else "chpy_holdings_daily"
            conn.execute(f"UPDATE {row_table} SET source_row_number=-source_row_number WHERE raw_source_id=?", (legacy_id,))
            for row in records:
                conn.execute(f"UPDATE {row_table} SET source_row_number=? WHERE raw_source_id=? AND source_row_number=?",
                             (row["source_row_number"], legacy_id, -(row["source_row_number"] - 1)))
            conn.commit()
        existing = conn.execute(
            "SELECT id,status FROM raw_sources WHERE feed_type=? AND sha256=?", (feed.name, digest)
        ).fetchone()
        if existing:
            raw_id = existing[0]
            if existing[1] != "validated":
                raise SourceValidationError([f"source bytes were previously rejected with status {existing[1]}"])
            status = "unchanged"
            is_new = False
        else:
            is_new = True
        metadata = {"feed_type": feed.name, "source_url": feed.url, "fetched_at_utc": fetched,
                    "reported_source_date": date, "record_count": len(records), "status": "validated",
                    "sha256": digest, "validation_errors": []}
        if is_new:
            paths = _archive_raw(db_path, feed.name, payload, metadata)
            raw_id, is_new = _insert_raw_source(conn, feed, fetched, date, records, digest, "validated", paths)
        if is_new:
            if feed.name == "intraday_trades":
                inserted = insert_records(conn, records, feed.url, raw_source_id=raw_id, raw_sha256=digest, fetched_at=fetched)
            else:
                snapshot_id = _insert_snapshot(conn, raw_id, date, fetched)
                inserted = insert_records(conn, records, feed.url, "chpy_holdings_daily", raw_source_id=raw_id, raw_sha256=digest, snapshot_id=snapshot_id, fetched_at=fetched)
        status = "success" if is_new else "unchanged"
    except Exception as exc:
        errors = exc.errors if isinstance(exc, SourceValidationError) else [str(exc)]
        if payload is not None and digest is not None and raw_id is None:
            existing = conn.execute(
                "SELECT id FROM raw_sources WHERE feed_type=? AND sha256=?", (feed.name, digest)
            ).fetchone()
            if existing:
                raw_id = existing[0]
        if payload is not None and raw_id is None:
            digest = digest or _sha256(payload)
            metadata = {"feed_type": feed.name, "source_url": feed.url, "fetched_at_utc": fetched,
                        "reported_source_date": date, "record_count": len(records), "status": "invalid",
                        "sha256": digest, "validation_errors": errors}
            paths = _archive_raw(db_path, feed.name, payload, metadata)
            raw_id, _ = _insert_raw_source(conn, feed, fetched, date, records, digest, "invalid", paths)
    conn.execute("""INSERT INTO collection_feed_runs
        (collection_run_id,feed_type,source_url,fetched_at_utc,reported_source_date,record_count,status,raw_source_id,raw_sha256,validation_errors)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (run_id, feed.name, feed.url, fetched, date, len(records), status, raw_id, digest, json.dumps(errors)))
    conn.commit()
    return {"feed": feed.name, "status": status, "source_date": date, "records": len(records), "inserted": inserted, "errors": errors, "sha256": digest}


def collect_all(conn: sqlite3.Connection, db_path: Path, fetcher: Callable = fetch_url, feeds: tuple[Feed, ...] = FEEDS) -> list[dict]:
    run_id = conn.execute("INSERT INTO collection_runs(started_at_utc,status) VALUES (?,'running')", (_utc_now(),)).lastrowid
    results = [_collect_feed(conn, run_id, db_path, feed, fetcher) for feed in feeds]
    status = "success" if all(row["status"] in {"success", "unchanged"} for row in results) else "incomplete"
    conn.execute("UPDATE collection_runs SET completed_at_utc=?,status=? WHERE id=?", (_utc_now(), status, run_id))
    conn.commit()
    return results


def audit_report(conn: sqlite3.Connection, limit: int = 10) -> str:
    lines = ["CHPY collection audit", f"database_integrity: {conn.execute('PRAGMA integrity_check').fetchone()[0]}"]
    lines.append(f"trade_executions: {conn.execute('SELECT count(*) FROM chpy_intraday_trades').fetchone()[0]}")
    current = conn.execute("SELECT count(*) FROM chpy_holdings_daily h JOIN holdings_snapshots s ON s.id=h.snapshot_id WHERE s.is_current=1").fetchone()[0]
    lines.append(f"current_holding_rows: {current}")
    lines.append(f"revised_holding_snapshots: {conn.execute('SELECT count(*) FROM holdings_snapshots WHERE version>1').fetchone()[0]}")
    lines.append("recent_feeds:")
    rows = conn.execute("""SELECT r.id,f.feed_type,f.status,f.fetched_at_utc,f.reported_source_date,
        f.record_count,f.raw_sha256,f.validation_errors FROM collection_feed_runs f
        JOIN collection_runs r ON r.id=f.collection_run_id ORDER BY f.id DESC LIMIT ?""", (limit,)).fetchall()
    for run, feed, status, fetched, date, count, digest, errors in rows:
        lines.append(f"  run={run} feed={feed} status={status} fetched={fetched} source_date={date or '-'} records={count} sha256={digest or '-'} errors={errors}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect and audit YieldMax CHPY transactions and holdings in SQLite.")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--audit", action="store_true", help="print collection audit without fetching")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        if args.audit:
            print(audit_report(conn))
            return 0
        results = collect_all(conn, db_path)
        for item in results:
            print(f"{item['feed']}: status={item['status']} source_date={item['source_date'] or '-'} records={item['records']} inserted={item['inserted']} sha256={item['sha256'] or '-'}")
            for error in item["errors"]:
                print(f"  ERROR: {error}")
        return 0 if all(item["status"] in {"success", "unchanged"} for item in results) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
