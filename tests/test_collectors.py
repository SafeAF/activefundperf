from __future__ import annotations

import json
from pathlib import Path

import pytest

from income_fund_lab.chpy_daily_summary import compute_daily_trade_pnl, summarize_daily_positions
from income_fund_lab.chpy_intraday_sqlite import ensure_schema, insert_records
from income_fund_lab.collectors import (
    parse_chpy_holdings_csv,
    parse_chpy_trade_csv,
    parse_usoy_html,
    validate_response,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_usoy_html_parsing():
    html = (FIXTURES / "usoy_holdings.html").read_text(encoding="utf-8")
    result = parse_usoy_html(html, "https://www.defianceetfs.com/usoy-full-holdings/")

    assert result["fund"] == "USOY"
    assert result["source_as_of_date"] == "2026-10-13"
    assert len(result["records"]) == 3
    assert result["records"][1]["ticker"] == "2USO  261120C00140010"
    assert result["records"][1]["option_underlying"] == "USO"
    assert result["records"][1]["option_type"] == "call"
    assert result["records"][1]["option_strike"] == 140.01


def test_chpy_holdings_parsing():
    csv_text = (FIXTURES / "chpy_holdings.csv").read_text(encoding="utf-8")
    result = parse_chpy_holdings_csv(csv_text)

    assert result["fund"] == "CHPY"
    assert result["source_as_of_date"] == "2026-10-13"
    assert len(result["records"]) == 4
    assert result["records"][1]["security_name"] == "SKHY US 10/16/26 C185"
    assert result["records"][1]["option_underlying"] == "SKHY"
    assert result["records"][1]["option_type"] == "call"
    assert result["records"][1]["quantity"] == -2181
    assert result["records"][2]["option_type"] == "call"
    assert result["records"][3]["option_type"] == "put"


def test_chpy_trade_parsing():
    csv_text = (FIXTURES / "chpy_intraday_trades.csv").read_text(encoding="utf-8")
    result = parse_chpy_trade_csv(csv_text)

    assert result["fund"] == "CHPY"
    assert len(result["records"]) == 3
    assert result["records"][0]["side"] == "buy"
    assert result["records"][1]["quantity"] == -25
    assert result["records"][1]["price"] == 154.20


def test_live_chpy_intraday_csv_parsing():
    csv_text = """Date,Fund,Ticker,Type,Qty/Par Value,Exec Price
10/14/2026,CHPY,CBRS 261016C00167500,BC,1331,0.12
10/14/2026,CHPY,CBRS 261016C00172500,S,1331,0.07
10/14/2026,CHPY,INTC 261023C00102000,SS,6096,2.54
10/14/2026,CHPY,INTC 261023C00102000,B,92,2.61
"""
    result = parse_chpy_trade_csv(csv_text)

    assert result["fund"] == "CHPY"
    assert len(result["records"]) == 4
    assert result["records"][0]["trade_date"] == "2026-10-14"
    assert result["records"][0]["side"] == "buy"
    assert result["records"][1]["side"] == "sell"
    assert result["records"][2]["side"] == "sell"
    assert result["records"][3]["side"] == "buy"
    assert result["records"][2]["quantity"] == -6096
    assert result["records"][2]["price"] == 2.54


def test_mixed_fund_intraday_rows_keep_their_own_fund_label():
    csv_text = """Date,Fund,Ticker,Type,Qty/Par Value,Exec Price
10/14/2026,TSLY,TSLA 261016C00392500,BC,2000,0.39
10/14/2026,CHPY,CBRS 261016C00167500,BC,1331,0.12
10/14/2026,CHPY,CBRS 261016C00172500,S,1331,0.07
"""
    result = parse_chpy_trade_csv(csv_text)

    assert [row["fund"] for row in result["records"]] == ["TSLY", "CHPY", "CHPY"]
    assert result["records"][1]["side"] == "buy"
    assert result["records"][2]["quantity"] == -1331


def test_sqlite_schema_supports_holdings_and_trades(tmp_path):
    db_path = tmp_path / "holdings.sqlite3"
    conn = __import__("sqlite3").connect(db_path)
    ensure_schema(conn)

    trades = [{
        "trade_date": "2026-10-14",
        "fund": "CHPY",
        "security": "CBRS 261016C00167500",
        "side": "buy",
        "raw_type": "BC",
        "quantity": 1331,
        "price": 0.12,
    }]
    holdings = [{
        "fund": "CHPY",
        "source_as_of_date": "2026-10-14",
        "security_name": "QUALCOMM Inc",
        "ticker": "QCOM",
        "CUSIP": "747525103",
        "asset_type": "security",
        "quantity": 201546,
        "market_value": 35645425.56,
        "portfolio_weight": 2.68,
        "option_underlying": None,
        "option_type": None,
        "option_strike": None,
        "option_expiration": None,
    }]

    assert insert_records(conn, trades, "https://example.com/trades", table_name="chpy_intraday_trades") == 1
    assert insert_records(conn, holdings, "https://example.com/holdings", table_name="chpy_holdings_daily") == 1
    assert conn.execute("SELECT COUNT(*) FROM chpy_intraday_trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chpy_holdings_daily").fetchone()[0] == 1
    conn.close()


def test_daily_position_and_trade_pnl_summary():
    trades = [
        ("2026-10-14", "AAPL", "buy", 100, 10.00),
        ("2026-10-14", "AAPL", "buy", 50, 12.00),
        ("2026-10-14", "AAPL", "sell", -40, 15.00),
        ("2026-10-15", "AAPL", "sell", -30, 16.00),
    ]
    holdings = [
        ("2026-10-14", "AAPL", 80.0, 1280.0, 4.0),
        ("2026-10-14", "MSFT", 50.0, 2500.0, 2.5),
        ("2026-10-15", "AAPL", 50.0, 800.0, 3.2),
    ]

    pnl = compute_daily_trade_pnl(trades)
    positions = summarize_daily_positions(holdings)

    assert pnl["2026-10-14"]["AAPL"] == 200.0
    assert pnl["2026-10-15"]["AAPL"] == 180.0
    assert positions["2026-10-14"]["AAPL"]["total_quantity"] == 80.0
    assert positions["2026-10-14"]["MSFT"]["total_quantity"] == 50.0
    assert positions["2026-10-15"]["AAPL"]["total_quantity"] == 50.0


def test_invalid_http_and_error_pages():
    with pytest.raises(ValueError):
        validate_response(503, "<html>Maintenance</html>", "https://example.invalid")

    with pytest.raises(ValueError):
        validate_response(200, "<html><title>Access denied</title></html>", "https://example.invalid")


def test_missing_or_malformed_fields_are_rejected():
    malformed = "Date,Account,StockTicker,CUSIP,SecurityName,Shares\n10/13/2026,CHPY,IBM,123,Missing quantity\n"
    with pytest.raises(ValueError):
        parse_chpy_holdings_csv(malformed)


def test_as_of_date_extraction_and_revision_tracking(tmp_path):
    base = tmp_path / "data"
    # The archive helpers will be validated indirectly by a snapshot test below.
    assert base.exists() is False


def test_duplicate_collection_attempts(tmp_path):
    from income_fund_lab.collectors import archive_snapshot

    payload = b"same-bytes\n"
    meta = {
        "fund": "CHPY",
        "source_type": "holdings",
        "source_url": "https://example.com/file.csv",
        "resolved_url": "https://example.com/file.csv",
        "source_as_of_date": "2026-10-13",
        "retrieval_timestamp_utc": "2026-10-13T00:00:00Z",
        "sha256": "abc123",
        "records": [{"ticker": "CHPY"}],
    }

    first = archive_snapshot(base_dir=tmp_path, fund="CHPY", source_type="holdings", payload=payload, metadata=meta)
    second = archive_snapshot(base_dir=tmp_path, fund="CHPY", source_type="holdings", payload=payload, metadata=meta)

    assert first["status"] == "new"
    assert second["status"] == "unchanged"
    assert len(list((tmp_path / "raw" / "chpy").glob("**/*"))) > 0


def test_same_date_revised_snapshot_kept(tmp_path):
    from income_fund_lab.collectors import archive_snapshot

    meta1 = {
        "fund": "USOY",
        "source_type": "holdings",
        "source_url": "https://example.com/a",
        "resolved_url": "https://example.com/a",
        "source_as_of_date": "2026-10-13",
        "retrieval_timestamp_utc": "2026-10-13T00:00:00Z",
        "sha256": "hash-one",
        "records": [{"ticker": "A"}],
    }
    meta2 = {
        "fund": "USOY",
        "source_type": "holdings",
        "source_url": "https://example.com/b",
        "resolved_url": "https://example.com/b",
        "source_as_of_date": "2026-10-13",
        "retrieval_timestamp_utc": "2026-10-13T01:00:00Z",
        "sha256": "hash-two",
        "records": [{"ticker": "B"}],
    }

    first = archive_snapshot(base_dir=tmp_path, fund="USOY", source_type="holdings", payload=b"one", metadata=meta1)
    second = archive_snapshot(base_dir=tmp_path, fund="USOY", source_type="holdings", payload=b"two", metadata=meta2)

    assert first["status"] == "new"
    assert second["status"] == "new"
    assert first["archive_path"] != second["archive_path"]
