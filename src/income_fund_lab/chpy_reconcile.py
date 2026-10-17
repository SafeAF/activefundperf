"""Read-only CHPY option position reconciliation."""
from __future__ import annotations
import argparse
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Iterable

DB = Path("data/chpy_intraday.sqlite3")
OPTION_TICKER = re.compile(r"^([A-Z.]+)\s+(\d{6})([CP])(\d{8})$")
TRADE_DECREASE_CODES = {"SS", "S"}
TRADE_INCREASE_CODES = {"B", "BC"}

MARKET_HOLIDAYS = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
}


@dataclass(frozen=True, order=True)
class OptionContract:
    underlying: str
    expiration: str
    option_type: str
    strike: float

    def label(self) -> str:
        return f"{self.underlying} {self.expiration} {self.option_type.upper()} {self.strike:g}"


@dataclass(frozen=True)
class ReconciliationRow:
    contract: OptionContract
    starting_qty: float
    trade_delta: float
    expected_qty: float
    actual_qty: float
    difference: float


@dataclass(frozen=True)
class ReconciliationResult:
    starting_snapshot_id: int
    starting_source_id: int
    starting_date: str
    trade_source_id: int | None
    trade_date: str
    ending_snapshot_id: int
    ending_source_id: int
    ending_date: str
    starting_contracts: int
    trade_executions: int
    ending_contracts: int
    reconciled_contracts: int
    discrepancy_rows: list[ReconciliationRow]
    total_abs_discrepancy: float
    starting_short_contracts: float
    starting_long_contracts: float
    ending_short_contracts: float
    ending_long_contracts: float
    trade_uncertainties: list[str]
    stock_changes: list[tuple[str, float, float, float]]
    alignment_scores: list[tuple[str, str, str, int, float]]

    @property
    def status(self) -> str:
        if self.trade_source_id is None or self.trade_uncertainties:
            return "UNAVAILABLE"
        if not self.discrepancy_rows:
            return "PASS"
        if discrepancies_have_expiration_support(self.discrepancy_rows, self.starting_date, self.ending_date):
            return "REVIEW"
        return "FAIL"


def latest_trade_sources_sql() -> str:
    return """
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


def parse_option_ticker(ticker: str) -> OptionContract | None:
    match = OPTION_TICKER.match(" ".join(str(ticker or "").split()))
    if not match:
        return None
    raw_date = match.group(2)
    expiration = f"20{raw_date[:2]}-{raw_date[2:4]}-{raw_date[4:6]}"
    option_type = "call" if match.group(3) == "C" else "put"
    strike = int(match.group(4)) / 1000
    return OptionContract(match.group(1), expiration, option_type, float(strike))


def signed_trade_delta(raw_type: str | None, quantity: float | int | None) -> float:
    code = str(raw_type or "").strip().upper()
    absolute_quantity = abs(float(quantity or 0))
    if code in TRADE_DECREASE_CODES:
        return -absolute_quantity
    if code in TRADE_INCREASE_CODES:
        return absolute_quantity
    raise ValueError(f"unsupported CHPY trade code: {raw_type!r}")


def reconcile_positions(
    starting: dict[OptionContract, float],
    trade_delta: dict[OptionContract, float],
    ending: dict[OptionContract, float],
) -> tuple[int, list[ReconciliationRow], float]:
    reconciled = 0
    discrepancies: list[ReconciliationRow] = []
    total_abs_discrepancy = 0.0
    for contract in sorted(set(starting) | set(trade_delta) | set(ending)):
        starting_qty = starting.get(contract, 0.0)
        delta = trade_delta.get(contract, 0.0)
        expected = starting_qty + delta
        actual = ending.get(contract, 0.0)
        difference = actual - expected
        if abs(difference) <= 1e-9:
            reconciled += 1
        else:
            discrepancies.append(ReconciliationRow(contract, starting_qty, delta, expected, actual, difference))
            total_abs_discrepancy += abs(difference)
    return reconciled, discrepancies, total_abs_discrepancy


def current_validated_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id, s.raw_source_id, s.source_as_of_date
        FROM holdings_snapshots s
        JOIN raw_sources r ON r.id=s.raw_source_id
        WHERE s.is_current=1
          AND r.feed_type='holdings'
          AND r.status='validated'
        ORDER BY s.source_as_of_date
        """
    ).fetchall()


def authoritative_trade_sources(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["reported_source_date"]: row["id"]
        for row in conn.execute(latest_trade_sources_sql()).fetchall()
    }


def option_positions(conn: sqlite3.Connection, snapshot_id: int) -> dict[OptionContract, float]:
    positions: dict[OptionContract, float] = defaultdict(float)
    rows = conn.execute(
        """
        SELECT ticker, option_underlying, option_expiration, option_type, option_strike, quantity
        FROM chpy_holdings_daily
        WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ).fetchall()
    for row in rows:
        if row["option_type"] is not None:
            contract = OptionContract(row["option_underlying"], row["option_expiration"], row["option_type"], float(row["option_strike"]))
        else:
            contract = parse_option_ticker(row["ticker"])
        if contract is not None:
            positions[contract] += float(row["quantity"] or 0)
    return dict(positions)


def trade_deltas(conn: sqlite3.Connection, trade_date: str, source_id: int | None) -> tuple[dict[OptionContract, float], int, list[str]]:
    if source_id is None:
        return {}, 0, [f"no authoritative validated intraday source for {trade_date}"]
    deltas: dict[OptionContract, float] = defaultdict(float)
    uncertainties: list[str] = []
    rows = conn.execute(
        """
        SELECT ticker, raw_type, quantity
        FROM chpy_intraday_trades
        WHERE trade_date=? AND raw_source_id=?
        ORDER BY source_row_number
        """,
        (trade_date, source_id),
    ).fetchall()
    for row in rows:
        contract = parse_option_ticker(row["ticker"])
        if contract is None:
            uncertainties.append(f"unparsed option ticker: {row['ticker']}")
            continue
        try:
            deltas[contract] += signed_trade_delta(row["raw_type"], row["quantity"])
        except ValueError as exc:
            uncertainties.append(str(exc))
    return dict(deltas), len(rows), uncertainties


def stock_quantity_changes(conn: sqlite3.Connection, starting_snapshot_id: int, ending_snapshot_id: int) -> list[tuple[str, float, float, float]]:
    start = {
        ticker: quantity
        for ticker, quantity in conn.execute(
            """
            SELECT ticker, quantity
            FROM chpy_holdings_daily
            WHERE snapshot_id=? AND option_type IS NULL AND ticker NOT IN ('Cash&Other', 'FGXXX')
            """,
            (starting_snapshot_id,),
        ).fetchall()
        if parse_option_ticker(ticker) is None
    }
    end = {
        ticker: quantity
        for ticker, quantity in conn.execute(
            """
            SELECT ticker, quantity
            FROM chpy_holdings_daily
            WHERE snapshot_id=? AND option_type IS NULL AND ticker NOT IN ('Cash&Other', 'FGXXX')
            """,
            (ending_snapshot_id,),
        ).fetchall()
        if parse_option_ticker(ticker) is None
    }
    changes: list[tuple[str, float, float, float]] = []
    for ticker in sorted(set(start) | set(end)):
        before = float(start.get(ticker) or 0)
        after = float(end.get(ticker) or 0)
        difference = after - before
        if abs(difference) > 1e-9:
            changes.append((ticker, before, after, difference))
    return changes


def contract_counts(positions: dict[OptionContract, float]) -> tuple[float, float]:
    short_contracts = sum(-quantity for quantity in positions.values() if quantity < 0)
    long_contracts = sum(quantity for quantity in positions.values() if quantity > 0)
    return short_contracts, long_contracts


def next_trading_day(value: str) -> str:
    current = date.fromisoformat(value) + timedelta(days=1)
    while current.weekday() >= 5 or current.isoformat() in MARKET_HOLIDAYS:
        current += timedelta(days=1)
    return current.isoformat()


def is_next_trading_day(start: str, end: str) -> bool:
    return next_trading_day(start) == end


def score_alignments(conn: sqlite3.Connection, snapshots: list[sqlite3.Row], trade_sources: dict[str, int]) -> list[tuple[str, str, str, int, float]]:
    scores: list[tuple[str, str, str, int, float]] = []
    for start, end in zip(snapshots, snapshots[1:]):
        starting = option_positions(conn, start["id"])
        ending = option_positions(conn, end["id"])
        for trade_date, source_id in sorted(trade_sources.items()):
            delta, _, uncertainties = trade_deltas(conn, trade_date, source_id)
            _, discrepancies, total = reconcile_positions(starting, delta, ending)
            scores.append((start["source_as_of_date"], trade_date, end["source_as_of_date"], len(discrepancies) + len(uncertainties), total))
    return sorted(scores, key=lambda item: (item[3], item[4], item[0], item[1], item[2]))

def discrepancies_have_expiration_support(rows: list[ReconciliationRow], starting_date: str, ending_date: str) -> bool:
    return bool(rows) and all(row.contract.expiration in {starting_date, ending_date} for row in rows)


def build_transition_reconciliation(
    conn: sqlite3.Connection,
    start: sqlite3.Row,
    end: sqlite3.Row,
    trade_sources: dict[str, int],
    alignment_scores: list[tuple[str, str, str, int, float]] | None = None,
) -> ReconciliationResult:
    trade_date = start["source_as_of_date"]
    starting = option_positions(conn, start["id"])
    ending = option_positions(conn, end["id"])
    trade_source_id = trade_sources.get(trade_date)
    deltas, trade_executions, uncertainties = trade_deltas(conn, trade_date, trade_source_id)
    if not is_next_trading_day(start["source_as_of_date"], end["source_as_of_date"]):
        uncertainties = [*uncertainties, f"next available holdings date is not the next trading day ({next_trading_day(start['source_as_of_date'])})"]
    reconciled, discrepancies, total_abs_discrepancy = reconcile_positions(starting, deltas, ending)
    starting_short, starting_long = contract_counts(starting)
    ending_short, ending_long = contract_counts(ending)
    return ReconciliationResult(
        starting_snapshot_id=start["id"],
        starting_source_id=start["raw_source_id"],
        starting_date=start["source_as_of_date"],
        trade_source_id=trade_source_id,
        trade_date=trade_date,
        ending_snapshot_id=end["id"],
        ending_source_id=end["raw_source_id"],
        ending_date=end["source_as_of_date"],
        starting_contracts=len(starting),
        trade_executions=trade_executions,
        ending_contracts=len(ending),
        reconciled_contracts=reconciled,
        discrepancy_rows=discrepancies,
        total_abs_discrepancy=total_abs_discrepancy,
        starting_short_contracts=starting_short,
        starting_long_contracts=starting_long,
        ending_short_contracts=ending_short,
        ending_long_contracts=ending_long,
        trade_uncertainties=uncertainties,
        stock_changes=stock_quantity_changes(conn, start["id"], end["id"]),
        alignment_scores=alignment_scores or [],
    )


def build_history(conn: sqlite3.Connection) -> list[ReconciliationResult]:
    snapshots = current_validated_snapshots(conn)
    if len(snapshots) < 2:
        return []
    trade_sources = authoritative_trade_sources(conn)
    alignment_scores = score_alignments(conn, snapshots, trade_sources)
    return [
        build_transition_reconciliation(conn, start, end, trade_sources, alignment_scores)
        for start, end in zip(snapshots, snapshots[1:])
    ]



def choose_transition(snapshots: list[sqlite3.Row]) -> tuple[sqlite3.Row, str, sqlite3.Row]:
    for start, end in zip(snapshots, snapshots[1:]):
        if start["source_as_of_date"] == "2026-10-14" and end["source_as_of_date"] == "2026-10-15":
            return start, "2026-10-14", end
    if len(snapshots) < 2:
        raise SystemExit("Need at least two current validated holdings snapshots.")
    start, end = snapshots[0], snapshots[1]
    return start, start["source_as_of_date"], end


def build_reconciliation(conn: sqlite3.Connection) -> ReconciliationResult:
    snapshots = current_validated_snapshots(conn)
    if len(snapshots) < 2:
        raise SystemExit("Need at least two current validated holdings snapshots.")
    trade_sources = authoritative_trade_sources(conn)
    start, _, end = choose_transition(snapshots)
    return build_transition_reconciliation(conn, start, end, trade_sources, score_alignments(conn, snapshots, trade_sources))


def format_quantity(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.3f}"


def print_rows(rows: Iterable[ReconciliationRow]) -> None:
    print("Underlying | Type | Strike | Expiration | Starting qty | Trade delta | Expected qty | Actual qty | Difference")
    printed = False
    for row in rows:
        printed = True
        print(
            f"{row.contract.underlying} | {row.contract.option_type} | {row.contract.strike:g} | {row.contract.expiration} | "
            f"{format_quantity(row.starting_qty)} | {format_quantity(row.trade_delta)} | "
            f"{format_quantity(row.expected_qty)} | {format_quantity(row.actual_qty)} | {format_quantity(row.difference)}"
        )
    if not printed:
        print("(none)")


def print_report(result: ReconciliationResult) -> None:
    print("CHPY POSITION RECONCILIATION")
    print()
    print(f"Starting holdings: {result.starting_date}")
    print(f"Trades applied:    {result.trade_date}")
    print(f"Ending holdings:   {result.ending_date}")
    print()
    print(f"Starting option contracts: {result.starting_contracts}")
    print(f"Trade executions:          {result.trade_executions}")
    print(f"Ending option contracts:   {result.ending_contracts}")
    print()
    print(f"Exact contracts reconciled: {result.reconciled_contracts}")
    print(f"Contracts with discrepancies: {len(result.discrepancy_rows)}")
    print(f"Total absolute discrepancy: {format_quantity(result.total_abs_discrepancy)} contracts")
    print()
    print("SOURCE IDS")
    print(f"Starting holdings snapshot/source: {result.starting_snapshot_id}/{result.starting_source_id}")
    print(f"Trade source:                      {result.trade_source_id if result.trade_source_id is not None else 'unavailable'}")
    print(f"Ending holdings snapshot/source:   {result.ending_snapshot_id}/{result.ending_source_id}")
    print(f"Consecutive trading days:          {'yes' if is_next_trading_day(result.starting_date, result.ending_date) else 'no'}")
    print()
    print("OPTION TOTALS")
    print(f"Starting short/long contracts: {format_quantity(result.starting_short_contracts)} / {format_quantity(result.starting_long_contracts)}")
    print(f"Ending short/long contracts:   {format_quantity(result.ending_short_contracts)} / {format_quantity(result.ending_long_contracts)}")
    print()
    print("RECONCILIATION STATUS")
    print(result.status)
    print()
    print("DISCREPANCIES")
    print_rows(result.discrepancy_rows)
    print()
    print("STOCK-SHARE CHANGES")
    if result.stock_changes:
        for ticker, before, after, difference in result.stock_changes:
            print(f"{ticker}: {format_quantity(before)} -> {format_quantity(after)} ({format_quantity(difference)})")
    else:
        print("(none)")
    print()
    print("MISSING DATA OR UNCERTAIN TIMING")
    if result.trade_uncertainties:
        for item in result.trade_uncertainties:
            print(f"- {item}")
    else:
        print("- none in selected reconciliation; all selected trade tickers parsed and source feeds were available")
    print()
    print("ALTERNATIVE ALIGNMENTS")
    for start_date, trade_date, end_date, discrepancy_count, total_abs in result.alignment_scores[:6]:
        print(
            f"{start_date} + trades {trade_date} -> {end_date}: "
            f"{discrepancy_count} discrepant contracts, {format_quantity(total_abs)} total absolute discrepancy"
        )
    print()
    print("CONCLUSION")
    if result.status == "PASS" and result.trade_date == result.starting_date:
        print(
            "Trades dated with the starting holdings date reconcile to the next holdings snapshot; "
            "the holdings source date appears to represent positions before that day's intraday option transactions are applied."
        )
    elif result.status == "PASS":
        print("The selected trade date fully reconciles the holdings transition shown above.")
    else:
        print("The selected transition does not fully reconcile; discrepancies above are left unadjusted.")

def print_history(results: list[ReconciliationResult], details: bool = False) -> None:
    print("CHPY HISTORICAL POSITION AUDIT")
    print()
    print("Start       End         Trades  Contracts  Mismatches  Abs Diff  Status")
    for result in results:
        contracts = result.reconciled_contracts + len(result.discrepancy_rows)
        print(
            f"{result.starting_date}  {result.ending_date}  "
            f"{result.trade_executions:>6}  {contracts:>9}  {len(result.discrepancy_rows):>10}  "
            f"{format_quantity(result.total_abs_discrepancy):>8}  {result.status}"
        )
    counts = Counter(result.status for result in results)
    print()
    print(f"TRANSITIONS: {len(results)}")
    for status in ("PASS", "FAIL", "UNAVAILABLE", "REVIEW"):
        print(f"{status}: {counts[status]}")
    if details:
        print()
        print("DETAILS")
        for result in results:
            print()
            print(f"{result.starting_date} + trades {result.trade_date} -> {result.ending_date}: {result.status}")
            print(
                f"Sources: holdings {result.starting_snapshot_id}/{result.starting_source_id}; "
                f"trades {result.trade_source_id if result.trade_source_id is not None else 'unavailable'}; "
                f"ending {result.ending_snapshot_id}/{result.ending_source_id}"
            )
            print(
                f"Option short/long: {format_quantity(result.starting_short_contracts)} / {format_quantity(result.starting_long_contracts)} "
                f"-> {format_quantity(result.ending_short_contracts)} / {format_quantity(result.ending_long_contracts)}"
            )
            if result.trade_uncertainties:
                print("Issues:")
                for issue in result.trade_uncertainties:
                    print(f"- {issue}")
            print("Stock-share changes:")
            if result.stock_changes:
                for ticker, before, after, difference in result.stock_changes:
                    print(f"{ticker}: {format_quantity(before)} -> {format_quantity(after)} ({format_quantity(difference)})")
            else:
                print("(none)")
            if result.status in {"FAIL", "REVIEW", "UNAVAILABLE"} or result.discrepancy_rows:
                print("Discrepancies:")
                print_rows(result.discrepancy_rows)
                expiring = [row for row in result.discrepancy_rows if row.contract.expiration in {result.starting_date, result.ending_date}]
                if expiring:
                    print(f"Expiration-supported discrepancies: {len(expiring)} of {len(result.discrepancy_rows)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile CHPY option positions against validated intraday trades.")
    parser.add_argument("--history", action="store_true", help="audit every available consecutive holdings transition")
    parser.add_argument("--details", action="store_true", help="with --history, print discrepancy details for failed/review transitions")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.history:
            print_history(build_history(conn), details=args.details)
        else:
            print_report(build_reconciliation(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
