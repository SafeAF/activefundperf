"""Small read-only terminal report for the latest collected CHPY data."""
# cd /home/sam/source/activefundperf && python3 src/income_fund_lab/chpy_report.py
from __future__ import annotations

import re
import sqlite3
import textwrap
from collections import defaultdict
from pathlib import Path


DB = Path("data/chpy_intraday.sqlite3")
OPTION = re.compile(r"^([A-Z.]+)\s+\d{6}([CP])\d{8}$")


def money(value: float) -> str:
    return f"${value:,.2f}"


def wrapped(label: str, items: list[str]) -> None:
    print(textwrap.fill("; ".join(items), width=110, initial_indent=label, subsequent_indent="  "))


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    snapshot = conn.execute(
        "SELECT id,source_as_of_date FROM holdings_snapshots "
        "ORDER BY source_as_of_date DESC,version DESC LIMIT 1"
    ).fetchone()
    if not snapshot:
        raise SystemExit("No holdings snapshots found.")
    holdings = conn.execute(
        "SELECT * FROM chpy_holdings_daily WHERE snapshot_id=? ORDER BY source_row_number",
        (snapshot["id"],),
    ).fetchall()

    options = [r for r in holdings if r["option_type"]]
    securities = [r for r in holdings if not r["option_type"]]
    cash = [r for r in securities if r["ticker"] in {"Cash&Other", "FGXXX"}]
    stocks = [r for r in securities if r not in cash]

    print("CHPY DAILY ANALYSIS")
    print("\nPORTFOLIO")
    print(f"Holdings source date: {snapshot['source_as_of_date']}")
    print(f"Stock market value: {money(sum(r['market_value'] or 0 for r in stocks))}")
    print(f"Signed options market value: {money(sum(r['market_value'] or 0 for r in options))}")
    print(f"Cash/other value: {money(sum(r['market_value'] or 0 for r in cash))}")
    print("Top stock holdings:")
    for r in sorted(stocks, key=lambda x: x["portfolio_weight"] or 0, reverse=True)[:10]:
        print(f"  {r['ticker']:<5} {r['portfolio_weight']:>5.2f}%  {money(r['market_value'])}")

    short = [r for r in options if (r["quantity"] or 0) < 0]
    long = [r for r in options if (r["quantity"] or 0) > 0]
    print("\nOPTIONS EXPOSURE")
    print(f"Contracts: {sum(abs(r['quantity']) for r in short):,.0f} short; "
          f"{sum(r['quantity'] for r in long):,.0f} long")
    by_underlying: dict[str, float] = defaultdict(float)
    shares = {r["ticker"]: r["quantity"] for r in stocks}
    for r in short:
        by_underlying[r["option_underlying"]] += abs(r["quantity"])
    coverage_items = []
    for ticker, contracts in sorted(by_underlying.items(), key=lambda x: x[1], reverse=True):
        coverage = ""
        if ticker in shares:
            coverage = f", {shares[ticker] / (contracts * 100):.1%} covered"
        coverage_items.append(f"{ticker} {contracts:,.0f}{coverage}")
    wrapped("Short calls (contracts, approximate stock coverage at 100 shares/contract): ", coverage_items)
    expirations: dict[str, float] = defaultdict(float)
    for r in options:
        expirations[r["option_expiration"]] += abs(r["quantity"])
    print("Expirations: " + "; ".join(f"{d}: {n:,.0f}" for d, n in sorted(expirations.items())))

    verticals = []
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in options:
        if r["option_type"] == "call":
            groups[(r["option_underlying"], r["option_expiration"])].append(r)
    for (ticker, expiry), rows in sorted(groups.items()):
        shorts = sorted((r for r in rows if r["quantity"] < 0), key=lambda r: r["option_strike"])
        longs = sorted((r for r in rows if r["quantity"] > 0), key=lambda r: r["option_strike"])
        for s in shorts:
            match = next((r for r in longs if r["quantity"] == -s["quantity"]), None)
            if match:
                verticals.append((ticker, expiry, -s["quantity"], s["option_strike"], match["option_strike"]))
                longs.remove(match)
    if verticals:
        wrapped("Obvious call verticals: ", [
            f"{ticker} {expiry} {count:,.0f}x {short_strike:g}/{long_strike:g}"
            for ticker, expiry, count, short_strike, long_strike in verticals
        ])

    latest_trade_sources = """
        SELECT r.id, r.reported_source_date
        FROM raw_sources r
        WHERE r.feed_type='intraday_trades'
          AND r.status='validated'
          AND NOT EXISTS (
              SELECT 1
              FROM raw_sources newer
              WHERE newer.feed_type=r.feed_type
                AND newer.status=r.status
                AND newer.reported_source_date=r.reported_source_date
                AND (newer.fetched_at_utc > r.fetched_at_utc OR (newer.fetched_at_utc = r.fetched_at_utc AND newer.id > r.id))
          )
    """
    trade_date = conn.execute(f"SELECT max(reported_source_date) FROM ({latest_trade_sources})").fetchone()[0]
    trades = conn.execute(
        f"SELECT t.* FROM chpy_intraday_trades t JOIN ({latest_trade_sources}) s ON s.id=t.raw_source_id "
        "WHERE t.trade_date=? ORDER BY t.source_row_number", (trade_date,)
    ).fetchall() if trade_date else []
    totals = defaultdict(float)
    opening = defaultdict(float)
    uncertain = 0
    for r in trades:
        match = OPTION.match(r["ticker"])
        if not match:
            uncertain += 1
            continue
        cash_flow = abs(r["quantity"] or 0) * (r["price"] or 0) * 100
        totals[r["raw_type"]] += cash_flow
        if r["raw_type"] in {"SS", "B"}:
            opening[match.group(1)] += cash_flow if r["raw_type"] == "SS" else -cash_flow
    print("\nTODAY'S TRADES")
    print(f"Trade source date: {trade_date or 'none'}; {len(trades)} executions")
    print(f"Opening premium received (SS): {money(totals['SS'])}")
    print(f"Opening premium paid (B):      {money(totals['B'])}")
    print(f"Closing cash received (S):    {money(totals['S'])}")
    print(f"Closing cash paid (BC):       {money(totals['BC'])}")
    net = totals["SS"] + totals["S"] - totals["B"] - totals["BC"]
    print(f"Net reported option-trade cash flow: {money(net)}")
    print("Top opening net credits:")
    for ticker, value in sorted(opening.items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  {ticker:<5} {money(value)}")
    if uncertain:
        print(f"WARNING: omitted {uncertain} trade(s) whose standard 100-share multiplier was uncertain.")
    print("Note: reported amounts are trade cash flows, not realized profits.")

    prior = conn.execute(
        "SELECT id,source_as_of_date FROM holdings_snapshots WHERE source_as_of_date<? "
        "ORDER BY source_as_of_date DESC,version DESC LIMIT 1", (snapshot["source_as_of_date"],)
    ).fetchone()
    print("\nCHANGES")
    if not prior:
        print("Insufficient history: no previous holdings snapshot.")
    else:
        old = dict(conn.execute(
            "SELECT ticker,quantity FROM chpy_holdings_daily WHERE snapshot_id=? AND option_type IS NULL",
            (prior["id"],),
        ))
        changes = []
        for r in stocks:
            before = old.get(r["ticker"])
            if before not in (None, 0):
                changes.append(((r["quantity"] - before) / abs(before) * 100, r["ticker"], before, r["quantity"]))
        print(f"Compared with {prior['source_as_of_date']} (snapshots may not align with trades):")
        for pct, ticker, before, after in sorted(changes, key=lambda x: abs(x[0]), reverse=True)[:5]:
            print(f"  {ticker:<5} {before:,.0f} -> {after:,.0f} ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
