from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from income_fund_lab.chpy_nav import (
    SourceDataError,
    build_history,
    calculate_change,
    parse_archived_snapshot,
    select_validated_snapshots,
)


HEADER = "Date,Account,StockTicker,CUSIP,SecurityName,Shares,Price,MarketValue,Weightings,NetAssets,SharesOutstanding,CreationUnits\n"


def csv_bytes(
    source_date="10/15/2026",
    net_assets="1000",
    shares="100",
    second_net_assets=None,
    second_shares=None,
):
    second_net_assets = net_assets if second_net_assets is None else second_net_assets
    second_shares = shares if second_shares is None else second_shares
    return (
        HEADER
        + f"{source_date},CHPY,AMD,1,AMD Inc,10,80,800,80%,{net_assets},{shares},4\n"
        + f"{source_date},CHPY,AMD   261023C00100000,OPT,AMD US 10/23/26 C100,-1,5,-5,-0.5%,{second_net_assets},{second_shares},4\n"
        + f"{source_date},CHPY,Cash&Other,CASH,Cash & Other,205,1,205,20.5%,{net_assets},{shares},4\n"
    ).encode()


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_sources (
            id INTEGER PRIMARY KEY, feed_type TEXT, fetched_at_utc TEXT,
            record_count INTEGER, status TEXT, sha256 TEXT, archive_path TEXT
        );
        CREATE TABLE holdings_snapshots (
            id INTEGER PRIMARY KEY, raw_source_id INTEGER, source_as_of_date TEXT,
            version INTEGER, is_current INTEGER
        );
        """
    )
    return conn


def add_source(conn, tmp_path, *, source_id=1, snapshot_id=1, iso_date="2026-10-15", version=1, current=1, status="validated", payload=None):
    payload = payload or csv_bytes()
    path = tmp_path / f"{source_id}.csv"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    conn.execute(
        "INSERT INTO raw_sources VALUES (?,?,?,?,?,?,?)",
        (source_id, "holdings", f"{iso_date}T20:00:00Z", 3, status, digest, str(path)),
    )
    conn.execute(
        "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?)",
        (snapshot_id, source_id, iso_date, version, current),
    )
    return select_validated_snapshots(conn)[-1]


def test_nav_computation_and_holdings_value_reconciliation(tmp_path):
    conn = memory_db()
    source = add_source(conn, tmp_path)

    snapshot = parse_archived_snapshot(source)

    assert snapshot.nav_per_share == 10
    assert snapshot.equity_value == 800
    assert snapshot.option_value == -5
    assert snapshot.cash_other_value == 205
    assert snapshot.residual == 0
    assert snapshot.residual_pct == 0


def test_repeated_fund_metadata_is_validated_not_summed(tmp_path):
    conn = memory_db()
    source = add_source(conn, tmp_path)

    snapshot = parse_archived_snapshot(source)

    assert snapshot.net_assets == 1000
    assert snapshot.shares_outstanding == 100


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (csv_bytes(net_assets="", second_net_assets=""), "missing NetAssets"),
        (csv_bytes(shares="bad", second_shares="bad"), "malformed SharesOutstanding"),
        (csv_bytes(second_net_assets="1001"), "inconsistent repeated NetAssets"),
        (csv_bytes(second_shares="101"), "inconsistent repeated SharesOutstanding"),
    ],
)
def test_missing_malformed_or_inconsistent_fund_metadata(tmp_path, payload, message):
    conn = memory_db()
    source = add_source(conn, tmp_path, payload=payload)

    with pytest.raises(SourceDataError, match=message):
        parse_archived_snapshot(source)


def test_zero_shares_outstanding_is_rejected(tmp_path):
    conn = memory_db()
    source = add_source(conn, tmp_path, payload=csv_bytes(shares="0"))

    with pytest.raises(SourceDataError, match="SharesOutstanding must be positive"):
        parse_archived_snapshot(source)


def test_creation_and_redemption_calculations(tmp_path):
    conn = memory_db()
    first = parse_archived_snapshot(add_source(conn, tmp_path, payload=csv_bytes()))
    second_source = add_source(
        conn,
        tmp_path,
        source_id=2,
        snapshot_id=2,
        iso_date="2026-10-16",
        payload=csv_bytes(source_date="10/16/2026", net_assets="1210", shares="110"),
    )
    second = parse_archived_snapshot(second_source)

    creation = calculate_change(first, second)
    redemption = calculate_change(second, first)

    assert creation.nav_change_pct == 10
    assert creation.net_asset_change == 210
    assert creation.shares_change == 10
    assert creation.shares_change_pct == 10
    assert creation.approximate_flow_value == 110
    assert creation.direction == "net share creations"
    assert redemption.direction == "net share redemptions"
    assert calculate_change(first, first).direction == "unchanged shares outstanding"


def test_snapshot_selection_prefers_current_then_highest_validated_version(tmp_path):
    conn = memory_db()
    add_source(conn, tmp_path, source_id=1, snapshot_id=1, version=1, current=0)
    add_source(conn, tmp_path, source_id=2, snapshot_id=2, version=2, current=1)
    add_source(conn, tmp_path, source_id=3, snapshot_id=3, version=3, current=0)
    add_source(conn, tmp_path, source_id=4, snapshot_id=4, version=4, current=1, status="invalid")

    selected = select_validated_snapshots(conn)

    assert [(row.snapshot_id, row.raw_source_id) for row in selected] == [(2, 2)]


def test_build_history_warns_and_skips_unusable_archive(tmp_path):
    conn = memory_db()
    add_source(conn, tmp_path, payload=csv_bytes(shares="0"))

    history, warnings = build_history(conn)

    assert history == []
    assert "SharesOutstanding must be positive" in warnings[0]
