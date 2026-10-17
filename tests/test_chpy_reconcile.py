from __future__ import annotations

import sqlite3

import pytest

from income_fund_lab.chpy_reconcile import (
    OptionContract,
    build_history,
    parse_option_ticker,
    reconcile_positions,
    signed_trade_delta,
)


def test_trade_signs_use_position_direction_not_source_quantity_sign():
    assert signed_trade_delta("SS", -12) == -12
    assert signed_trade_delta("S", -7) == -7
    assert signed_trade_delta("B", 5) == 5
    assert signed_trade_delta("BC", 3) == 3
    with pytest.raises(ValueError):
        signed_trade_delta("X", 1)


def test_opening_and_closing_positions_reconcile():
    contract = OptionContract("INTC", "2026-10-23", "call", 102.0)
    reconciled, discrepancies, total = reconcile_positions(
        starting={},
        trade_delta={contract: signed_trade_delta("SS", -6188)},
        ending={contract: -6188},
    )
    assert reconciled == 1
    assert discrepancies == []
    assert total == 0

    reconciled, discrepancies, total = reconcile_positions(
        starting={contract: -6096},
        trade_delta={contract: signed_trade_delta("BC", 6096)},
        ending={},
    )
    assert reconciled == 1
    assert discrepancies == []
    assert total == 0


def test_exact_contract_identity_keeps_expiration_and_strike_separate():
    old_contract = OptionContract("INTC", "2026-10-16", "call", 116.0)
    new_contract = OptionContract("INTC", "2026-10-23", "call", 102.0)
    reconciled, discrepancies, total = reconcile_positions(
        starting={old_contract: -6096},
        trade_delta={old_contract: 6096, new_contract: -6188},
        ending={new_contract: -6188},
    )
    assert reconciled == 2
    assert discrepancies == []
    assert total == 0


def test_missing_contract_in_complete_snapshot_is_zero_position():
    contract = OptionContract("AMD", "2026-10-16", "call", 625.0)
    reconciled, discrepancies, total = reconcile_positions(
        starting={contract: -100},
        trade_delta={contract: 100},
        ending={},
    )
    assert reconciled == 1
    assert discrepancies == []
    assert total == 0


def test_option_ticker_parses_exact_identity():
    assert parse_option_ticker("INTC  261023C00102000") == OptionContract("INTC", "2026-10-23", "call", 102.0)
    assert parse_option_ticker("AMD   261016P00625000") == OptionContract("AMD", "2026-10-16", "put", 625.0)
    assert parse_option_ticker("INTC") is None


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_sources (
            id INTEGER PRIMARY KEY, feed_type TEXT NOT NULL, source_url TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL, reported_source_date TEXT, record_count INTEGER NOT NULL,
            status TEXT NOT NULL, sha256 TEXT NOT NULL, archive_path TEXT, metadata_path TEXT
        );
        CREATE TABLE holdings_snapshots (
            id INTEGER PRIMARY KEY, raw_source_id INTEGER NOT NULL, source_as_of_date TEXT NOT NULL,
            version INTEGER NOT NULL, is_current INTEGER NOT NULL, superseded_at_utc TEXT
        );
        CREATE TABLE chpy_holdings_daily (
            row_hash TEXT PRIMARY KEY, source_as_of_date TEXT NOT NULL, fund TEXT NOT NULL, ticker TEXT,
            cusip TEXT, security_name TEXT NOT NULL, asset_type TEXT, quantity REAL, market_value REAL,
            portfolio_weight REAL, option_underlying TEXT, option_type TEXT, option_strike REAL,
            option_expiration TEXT, fetched_at_utc TEXT NOT NULL, source_url TEXT NOT NULL,
            raw_source_id INTEGER, snapshot_id INTEGER, source_row_number INTEGER
        );
        CREATE TABLE chpy_intraday_trades (
            row_hash TEXT PRIMARY KEY, trade_date TEXT NOT NULL, fund TEXT NOT NULL, ticker TEXT NOT NULL,
            side TEXT, raw_type TEXT, quantity INTEGER, price REAL, fetched_at_utc TEXT NOT NULL,
            source_url TEXT NOT NULL, raw_source_id INTEGER, source_row_number INTEGER
        );
        """
    )
    return conn


def add_snapshot(conn, snapshot_id, source_id, source_date, positions):
    conn.execute(
        "INSERT INTO raw_sources VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, "holdings", "holdings", f"{source_date}T05:00:00Z", source_date, len(positions), "validated", f"h{source_id}", None, None),
    )
    conn.execute(
        "INSERT INTO holdings_snapshots VALUES (?,?,?,?,?,NULL)",
        (snapshot_id, source_id, source_date, 1, 1),
    )
    for row_number, (contract, quantity, parsed_fields) in enumerate(positions, 1):
        option_underlying = contract.underlying if parsed_fields else None
        option_type = contract.option_type if parsed_fields else None
        option_strike = contract.strike if parsed_fields else None
        option_expiration = contract.expiration if parsed_fields else None
        ticker = f"{contract.underlying}  {contract.expiration[2:4]}{contract.expiration[5:7]}{contract.expiration[8:10]}{'C' if contract.option_type == 'call' else 'P'}{int(contract.strike * 1000):08d}"
        conn.execute(
            """INSERT INTO chpy_holdings_daily
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"h{snapshot_id}-{row_number}", source_date, "CHPY", ticker, ticker, "option",
                "option", quantity, 0, 0, option_underlying, option_type, option_strike,
                option_expiration, f"{source_date}T05:00:00Z", "holdings", source_id, snapshot_id, row_number,
            ),
        )


def add_trade_source(conn, source_id, trade_date, fetched_suffix="16:00:00", trades=()):
    conn.execute(
        "INSERT INTO raw_sources VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, "intraday_trades", "trades", f"{trade_date}T{fetched_suffix}Z", trade_date, len(trades), "validated", f"t{source_id}", None, None),
    )
    for row_number, (contract, raw_type, quantity) in enumerate(trades, 1):
        ticker = f"{contract.underlying} {contract.expiration[2:4]}{contract.expiration[5:7]}{contract.expiration[8:10]}{'C' if contract.option_type == 'call' else 'P'}{int(contract.strike * 1000):08d}"
        conn.execute(
            "INSERT INTO chpy_intraday_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{source_id}-{row_number}", trade_date, "CHPY", ticker, "buy" if raw_type in {"B", "BC"} else "sell", raw_type, quantity, 1.0, f"{trade_date}T{fetched_suffix}Z", "trades", source_id, row_number),
        )


def test_history_audits_two_transitions_with_pass_and_fail():
    conn = memory_db()
    amd = OptionContract("AMD", "2026-01-09", "call", 100.0)
    nvda = OptionContract("NVDA", "2026-01-09", "call", 200.0)
    add_snapshot(conn, 1, 101, "2026-01-05", [(amd, -10, True)])
    add_snapshot(conn, 2, 102, "2026-01-06", [(nvda, -5, False)])
    add_snapshot(conn, 3, 103, "2026-01-07", [(nvda, -4, True)])
    add_trade_source(conn, 201, "2026-01-05", trades=[(amd, "BC", 10), (nvda, "SS", -5)])
    add_trade_source(conn, 202, "2026-01-06", trades=[])

    results = build_history(conn)

    assert [result.status for result in results] == ["PASS", "FAIL"]
    assert results[0].trade_executions == 2
    assert len(results[1].discrepancy_rows) == 1


def test_missing_intraday_source_is_unavailable():
    conn = memory_db()
    contract = OptionContract("AMD", "2026-01-09", "call", 100.0)
    add_snapshot(conn, 1, 101, "2026-01-05", [(contract, -10, True)])
    add_snapshot(conn, 2, 102, "2026-01-06", [(contract, -10, True)])

    result = build_history(conn)[0]

    assert result.status == "UNAVAILABLE"
    assert result.trade_source_id is None


def test_superseded_intraday_versions_are_not_double_counted():
    conn = memory_db()
    contract = OptionContract("AMD", "2026-01-09", "call", 100.0)
    add_snapshot(conn, 1, 101, "2026-01-05", [(contract, -10, True)])
    add_snapshot(conn, 2, 102, "2026-01-06", [])
    add_trade_source(conn, 201, "2026-01-05", fetched_suffix="10:00:00", trades=[(contract, "BC", 5)])
    add_trade_source(conn, 202, "2026-01-05", fetched_suffix="16:00:00", trades=[(contract, "BC", 10)])

    result = build_history(conn)[0]

    assert result.status == "PASS"
    assert result.trade_source_id == 202
    assert result.trade_executions == 1


def test_expiration_without_recorded_transaction_is_review_not_balanced():
    conn = memory_db()
    expiring = OptionContract("AMD", "2026-01-16", "call", 100.0)
    unrelated = OptionContract("NVDA", "2026-01-23", "call", 200.0)
    add_snapshot(conn, 1, 101, "2026-01-16", [(expiring, -10, True)])
    add_snapshot(conn, 2, 102, "2026-01-20", [])
    add_trade_source(conn, 201, "2026-01-16", trades=[(unrelated, "SS", -1), (unrelated, "BC", 1)])

    result = build_history(conn)[0]

    assert result.status == "REVIEW"
    assert len(result.discrepancy_rows) == 1
    assert result.total_abs_discrepancy == 10


def test_missing_intermediate_trading_day_snapshot_is_unavailable():
    conn = memory_db()
    contract = OptionContract("AMD", "2026-01-09", "call", 100.0)
    add_snapshot(conn, 1, 101, "2026-01-05", [(contract, -10, True)])
    add_snapshot(conn, 2, 102, "2026-01-07", [])
    add_trade_source(conn, 201, "2026-01-05", trades=[(contract, "BC", 10)])

    result = build_history(conn)[0]

    assert result.status == "UNAVAILABLE"
    assert "not the next trading day" in result.trade_uncertainties[0]
