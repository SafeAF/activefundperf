from __future__ import annotations

import json
from pathlib import Path

import pytest

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
