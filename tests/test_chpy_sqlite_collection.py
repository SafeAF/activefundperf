from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from income_fund_lab.chpy_intraday_sqlite import Feed, audit_report, collect_all, ensure_schema
from income_fund_lab.collectors import parse_chpy_holdings_csv, parse_chpy_trade_csv


FIXTURES = Path(__file__).parent / "fixtures"
TRADES = (FIXTURES / "chpy_intraday_repeated.csv").read_bytes()
HOLDINGS = (FIXTURES / "chpy_holdings.csv").read_bytes()
REVISED_HOLDINGS = (FIXTURES / "chpy_holdings_revised.csv").read_bytes()


class Response:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


def feeds(minimum_holdings: int = 1):
    return (
        Feed("intraday_trades", "https://example.test/trades", 1),
        Feed("holdings", "https://example.test/holdings", minimum_holdings),
    )


def open_db(tmp_path):
    path = tmp_path / "chpy.sqlite3"
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    return path, conn


def test_identical_trade_occurrences_survive_and_rerun_is_idempotent(tmp_path):
    path, conn = open_db(tmp_path)
    payloads = {"https://example.test/trades": TRADES, "https://example.test/holdings": HOLDINGS}
    fetcher = lambda url: Response(payloads[url])

    first = collect_all(conn, path, fetcher, feeds())
    second = collect_all(conn, path, fetcher, feeds())

    assert [row["status"] for row in first] == ["success", "success"]
    assert [row["status"] for row in second] == ["unchanged", "unchanged"]
    rows = conn.execute("SELECT ticker,raw_type,quantity,source_row_number FROM chpy_intraday_trades ORDER BY source_row_number").fetchall()
    assert rows[:2] == [
        ("CBRS 261016C00167500", "BC", 1331, 2),
        ("CBRS 261016C00167500", "BC", 1331, 3),
    ]
    assert len(rows) == 3
    assert conn.execute("SELECT count(*) FROM raw_sources WHERE status='validated'").fetchone()[0] == 2
    conn.close()


def test_raw_archives_are_hashed_and_read_only(tmp_path):
    path, conn = open_db(tmp_path)
    payloads = {"https://example.test/trades": TRADES, "https://example.test/holdings": HOLDINGS}
    collect_all(conn, path, lambda url: Response(payloads[url]), feeds())
    for archive_path, digest in conn.execute("SELECT archive_path,sha256 FROM raw_sources WHERE status='validated'"):
        archived = Path(archive_path)
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == digest
        assert archived.stat().st_mode & 0o222 == 0
    conn.close()


def test_revised_holdings_replace_current_snapshot_without_deleting_history(tmp_path):
    path, conn = open_db(tmp_path)
    current = {"https://example.test/trades": TRADES, "https://example.test/holdings": HOLDINGS}
    collect_all(conn, path, lambda url: Response(current[url]), feeds())
    current["https://example.test/holdings"] = REVISED_HOLDINGS
    collect_all(conn, path, lambda url: Response(current[url]), feeds())

    snapshots = conn.execute("SELECT version,is_current FROM holdings_snapshots ORDER BY version").fetchall()
    assert snapshots == [(1, 0), (2, 1)]
    assert conn.execute("SELECT count(*) FROM chpy_holdings_daily").fetchone()[0] == 8
    quantity = conn.execute("""SELECT h.quantity FROM chpy_holdings_daily h JOIN holdings_snapshots s ON s.id=h.snapshot_id
        WHERE s.is_current=1 AND h.ticker='CBRS' AND h.cusip='15675D103'""").fetchone()[0]
    assert quantity == 132900
    conn.close()


def test_failed_feed_does_not_block_other_feed_and_run_is_incomplete(tmp_path):
    path, conn = open_db(tmp_path)
    def fetcher(url):
        return Response(b"maintenance", 503) if url.endswith("trades") else Response(HOLDINGS)

    results = collect_all(conn, path, fetcher, feeds())

    assert [row["status"] for row in results] == ["failed", "success"]
    assert conn.execute("SELECT status FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "incomplete"
    assert conn.execute("SELECT count(*) FROM chpy_holdings_daily").fetchone()[0] == 4
    assert conn.execute("SELECT validation_errors FROM collection_feed_runs WHERE feed_type='intraday_trades'").fetchone()[0] != "[]"
    conn.close()


def test_partial_holdings_are_archived_but_not_imported(tmp_path):
    path, conn = open_db(tmp_path)
    payloads = {"https://example.test/trades": TRADES, "https://example.test/holdings": HOLDINGS}
    results = collect_all(conn, path, lambda url: Response(payloads[url]), feeds(minimum_holdings=10))

    assert results[1]["status"] == "failed"
    assert "expected at least 10" in results[1]["errors"][0]
    assert conn.execute("SELECT count(*) FROM chpy_holdings_daily").fetchone()[0] == 0
    raw = conn.execute("SELECT status,archive_path FROM raw_sources WHERE feed_type='holdings'").fetchone()
    assert raw[0] == "invalid" and Path(raw[1]).exists()
    conn.close()


def test_legacy_schema_migration_preserves_rows_and_audit_reports(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE chpy_intraday_trades (row_hash TEXT PRIMARY KEY,trade_date TEXT NOT NULL,
        fund TEXT NOT NULL,ticker TEXT NOT NULL,side TEXT,raw_type TEXT,quantity INTEGER,price REAL,
        fetched_at_utc TEXT NOT NULL,source_url TEXT NOT NULL)""")
    conn.execute("INSERT INTO chpy_intraday_trades VALUES ('old','2026-10-13','CHPY','AAPL','buy','B',1,10.0,'2026-10-13T20:00:00Z','legacy')")
    conn.commit()

    ensure_schema(conn)

    assert conn.execute("SELECT count(*) FROM chpy_intraday_trades").fetchone()[0] == 1
    assert conn.execute("SELECT raw_source_id,source_row_number FROM chpy_intraday_trades").fetchone()[0] is not None
    report = audit_report(conn)
    assert "database_integrity: ok" in report
    assert "trade_executions: 1" in report
    conn.close()


def test_matching_legacy_rows_adopt_first_real_archive_without_duplication(tmp_path):
    path, conn = open_db(tmp_path)
    trades = [row for row in parse_chpy_trade_csv(TRADES.decode())["records"] if row["fund"] == "CHPY"]
    holdings = parse_chpy_holdings_csv(HOLDINGS.decode())["records"]
    from income_fund_lab.chpy_intraday_sqlite import insert_records
    insert_records(conn, trades, "https://example.test/trades")
    insert_records(conn, holdings, "https://example.test/holdings", "chpy_holdings_daily")
    ensure_schema(conn)

    payloads = {"https://example.test/trades": TRADES, "https://example.test/holdings": HOLDINGS}
    results = collect_all(conn, path, lambda url: Response(payloads[url]), feeds())

    assert [row["status"] for row in results] == ["unchanged", "unchanged"]
    assert conn.execute("SELECT count(*) FROM chpy_intraday_trades").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM chpy_holdings_daily").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM raw_sources WHERE status='imported_legacy'").fetchone()[0] == 0
    conn.close()
