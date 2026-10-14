from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

from income_fund_lab.chpy_intraday_sqlite import DEFAULT_DB, ensure_schema


def compute_daily_trade_pnl(trade_rows: list[tuple[str, str, str, float, float]]) -> dict[str, dict[str, float]]:
    lots_by_ticker: dict[str, list[dict[str, float]]] = defaultdict(list)
    pnl_by_day: dict[str, dict[str, float]] = defaultdict(dict)
    cashflow_by_day_ticker: dict[tuple[str, str], float] = defaultdict(float)

    for trade_date, ticker, side, quantity, price in trade_rows:
        qty = float(quantity)
        price_value = float(price)
        normalized_side = str(side or "").strip().lower()
        if normalized_side in {"buy", "b", "bc", "buy to close"}:
            lots_by_ticker[ticker].append({"trade_date": trade_date, "quantity": abs(qty), "price": price_value})
            cashflow_by_day_ticker[(trade_date, ticker)] -= abs(qty) * price_value
            continue
        if normalized_side in {"sell", "s", "ss", "sell short", "short sale", "short"}:
            sell_quantity = abs(qty)
            cashflow_by_day_ticker[(trade_date, ticker)] += sell_quantity * price_value
            while sell_quantity > 0 and lots_by_ticker[ticker]:
                lot = lots_by_ticker[ticker][0]
                matched_quantity = min(sell_quantity, lot["quantity"])
                pnl_by_day[trade_date].setdefault(ticker, 0.0)
                pnl_by_day[trade_date][ticker] += matched_quantity * (price_value - lot["price"])
                lot["quantity"] -= matched_quantity
                sell_quantity -= matched_quantity
                if lot["quantity"] <= 0:
                    lots_by_ticker[ticker].pop(0)

    for (trade_date, ticker), net_cashflow in cashflow_by_day_ticker.items():
        pnl_by_day[trade_date].setdefault(ticker, net_cashflow)

    return pnl_by_day


def summarize_daily_positions(holding_rows: list[tuple[str, str, float, float, float]]) -> dict[str, dict[str, dict[str, float]]]:
    daily_positions: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for as_of_date, ticker, total_quantity, market_value, portfolio_weight in holding_rows:
        date_key = str(as_of_date)
        daily_positions[date_key].setdefault(
            str(ticker),
            {"total_quantity": 0.0, "market_value": 0.0, "portfolio_weight": 0.0},
        )
        daily_positions[date_key][str(ticker)]["total_quantity"] += float(total_quantity)
        daily_positions[date_key][str(ticker)]["market_value"] += float(market_value)
        daily_positions[date_key][str(ticker)]["portfolio_weight"] += float(portfolio_weight)
    return daily_positions


def fetch_trade_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, float, float]]:
    rows = conn.execute(
        "SELECT trade_date, ticker, side, quantity, price FROM chpy_intraday_trades ORDER BY trade_date, ticker, rowid"
    ).fetchall()
    return [(trade_date, ticker, side, float(quantity), float(price)) for trade_date, ticker, side, quantity, price in rows]


def fetch_holdings_rows(conn: sqlite3.Connection) -> list[tuple[str, str, float, float, float]]:
    rows = conn.execute(
        """SELECT h.source_as_of_date, h.ticker, h.quantity, h.market_value, h.portfolio_weight
           FROM chpy_holdings_daily h
           JOIN holdings_snapshots s ON s.id = h.snapshot_id
           WHERE s.is_current = 1
           ORDER BY h.source_as_of_date, h.source_row_number"""
    ).fetchall()
    return [(source_as_of_date, ticker, float(quantity), float(market_value), float(portfolio_weight)) for source_as_of_date, ticker, quantity, market_value, portfolio_weight in rows]


def ensure_summary_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chpy_daily_position_summary (
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            total_quantity REAL,
            market_value REAL,
            portfolio_weight REAL,
            PRIMARY KEY (as_of_date, ticker)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chpy_daily_trade_pnl (
            trade_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            realized_pnl REAL,
            PRIMARY KEY (trade_date, ticker)
        )
        """
    )
    conn.commit()


def write_summary_tables(conn: sqlite3.Connection) -> dict[str, int]:
    ensure_summary_tables(conn)
    trade_rows = fetch_trade_rows(conn)
    holding_rows = fetch_holdings_rows(conn)

    pnl_map = compute_daily_trade_pnl(trade_rows)
    position_map = summarize_daily_positions(holding_rows)

    trade_inserts = 0
    for trade_date, ticker_map in pnl_map.items():
        for ticker, pnl in ticker_map.items():
            conn.execute(
                "INSERT INTO chpy_daily_trade_pnl (trade_date, ticker, realized_pnl) VALUES (?, ?, ?) ON CONFLICT(trade_date, ticker) DO UPDATE SET realized_pnl = excluded.realized_pnl",
                (trade_date, ticker, float(pnl)),
            )
            trade_inserts += 1

    position_inserts = 0
    for as_of_date, ticker_map in position_map.items():
        for ticker, payload in ticker_map.items():
            conn.execute(
                "INSERT INTO chpy_daily_position_summary (as_of_date, ticker, total_quantity, market_value, portfolio_weight) VALUES (?, ?, ?, ?, ?) ON CONFLICT(as_of_date, ticker) DO UPDATE SET total_quantity = excluded.total_quantity, market_value = excluded.market_value, portfolio_weight = excluded.portfolio_weight",
                (as_of_date, ticker, float(payload["total_quantity"]), float(payload["market_value"]), float(payload["portfolio_weight"])),
            )
            position_inserts += 1

    conn.commit()
    return {"trade_rows": trade_inserts, "position_rows": position_inserts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize CHPY daily holdings and trade P&L from the SQLite tables.")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path containing the CHPY import tables")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        summary = write_summary_tables(conn)
        print(f"Wrote {summary['position_rows']} daily position rows and {summary['trade_rows']} daily P&L rows to {db_path}")
        print("Sample trade P&L:")
        for row in conn.execute("SELECT trade_date, ticker, realized_pnl FROM chpy_daily_trade_pnl ORDER BY trade_date, ticker LIMIT 5").fetchall():
            print(row)
        print("Sample positions:")
        for row in conn.execute("SELECT as_of_date, ticker, total_quantity, market_value, portfolio_weight FROM chpy_daily_position_summary ORDER BY as_of_date, ticker LIMIT 5").fetchall():
            print(row)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
