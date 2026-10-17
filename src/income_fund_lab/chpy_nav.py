"""Read-only CHPY NAV, shares-outstanding, and holdings reconciliation history."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from income_fund_lab.chpy_reconcile import parse_option_ticker


DEFAULT_DB = Path("data/chpy_intraday.sqlite3")
CASH_TICKERS = {"CASH&OTHER", "FGXXX"}
REQUIRED_COLUMNS = {
    "Date",
    "Account",
    "StockTicker",
    "SecurityName",
    "Shares",
    "MarketValue",
    "NetAssets",
    "SharesOutstanding",
}


class SourceDataError(ValueError):
    """A selected archived source cannot support authoritative NAV reporting."""


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: int
    raw_source_id: int
    source_as_of_date: str
    version: int
    is_current: bool
    fetched_at_utc: str
    record_count: int
    sha256: str
    archive_path: Path


@dataclass(frozen=True)
class HoldingValue:
    ticker: str
    quantity: Decimal
    market_value: Decimal
    category: str


@dataclass(frozen=True)
class NavSnapshot:
    source: SourceSnapshot
    net_assets: Decimal
    shares_outstanding: Decimal
    nav_per_share: Decimal
    equity_value: Decimal
    option_value: Decimal
    cash_other_value: Decimal
    residual: Decimal
    residual_pct: Decimal
    holdings: tuple[HoldingValue, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DailyChange:
    start: NavSnapshot
    end: NavSnapshot
    net_asset_change: Decimal
    nav_change_pct: Decimal
    shares_change: Decimal
    shares_change_pct: Decimal
    approximate_flow_value: Decimal

    @property
    def direction(self) -> str:
        if self.shares_change > 0:
            return "net share creations"
        if self.shares_change < 0:
            return "net share redemptions"
        return "unchanged shares outstanding"


def parse_number(value: str | None, field: str, row_number: int) -> Decimal:
    text = str(value or "").strip()
    if not text:
        raise SourceDataError(f"row {row_number}: missing {field}")
    negative = text.startswith("(") and text.endswith(")")
    normalized = text.strip("()").replace(",", "").replace("$", "").strip()
    try:
        parsed = Decimal(normalized)
    except InvalidOperation as exc:
        raise SourceDataError(f"row {row_number}: malformed {field}: {text!r}") from exc
    if negative:
        parsed = -parsed
    if not parsed.is_finite():
        raise SourceDataError(f"row {row_number}: non-finite {field}: {text!r}")
    return parsed


def parse_source_date(value: str | None, row_number: int) -> str:
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise SourceDataError(f"row {row_number}: malformed Date: {text!r}")


def holding_category(ticker: str) -> str:
    normalized = " ".join(ticker.split())
    if normalized.upper() in CASH_TICKERS:
        return "cash_other"
    if parse_option_ticker(normalized) is not None:
        return "option"
    return "equity"


def select_validated_snapshots(conn: sqlite3.Connection) -> list[SourceSnapshot]:
    """Select one validated version per source date, preferring the current version."""
    rows = conn.execute(
        """
        SELECT s.id AS snapshot_id, s.raw_source_id, s.source_as_of_date,
               s.version, s.is_current, r.fetched_at_utc, r.record_count,
               r.sha256, r.archive_path
        FROM holdings_snapshots s
        JOIN raw_sources r ON r.id=s.raw_source_id
        WHERE r.feed_type='holdings' AND r.status='validated'
        ORDER BY s.source_as_of_date, s.is_current DESC, s.version DESC, s.id DESC
        """
    ).fetchall()
    selected: dict[str, SourceSnapshot] = {}
    for row in rows:
        source_date = row["source_as_of_date"]
        if source_date in selected:
            continue
        selected[source_date] = SourceSnapshot(
            snapshot_id=row["snapshot_id"],
            raw_source_id=row["raw_source_id"],
            source_as_of_date=source_date,
            version=row["version"],
            is_current=bool(row["is_current"]),
            fetched_at_utc=row["fetched_at_utc"],
            record_count=row["record_count"],
            sha256=row["sha256"],
            archive_path=Path(row["archive_path"]),
        )
    return [selected[key] for key in sorted(selected)]


def parse_archived_snapshot(source: SourceSnapshot) -> NavSnapshot:
    try:
        payload = source.archive_path.read_bytes()
    except OSError as exc:
        raise SourceDataError(f"cannot read archive {source.archive_path}: {exc}") from exc
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != source.sha256:
        raise SourceDataError(
            f"archive SHA-256 mismatch for raw source {source.raw_source_id}: "
            f"expected {source.sha256}, got {actual_hash}"
        )

    try:
        text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceDataError(f"archive is not valid UTF-8: {source.archive_path}") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise SourceDataError("holdings CSV has no header")
    missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
    if missing:
        raise SourceDataError(f"missing CSV columns: {', '.join(missing)}")

    dates: set[str] = set()
    net_assets_values: set[Decimal] = set()
    shares_values: set[Decimal] = set()
    holdings: list[HoldingValue] = []
    for row_number, row in enumerate(reader, start=2):
        if not row or not any(str(value or "").strip() for value in row.values()):
            continue
        account = str(row.get("Account") or "").strip().upper()
        if account != "CHPY":
            raise SourceDataError(f"row {row_number}: expected Account CHPY, got {account!r}")
        dates.add(parse_source_date(row.get("Date"), row_number))
        net_assets_values.add(parse_number(row.get("NetAssets"), "NetAssets", row_number))
        shares_values.add(parse_number(row.get("SharesOutstanding"), "SharesOutstanding", row_number))
        ticker = str(row.get("StockTicker") or "").strip()
        holdings.append(
            HoldingValue(
                ticker=ticker,
                quantity=parse_number(row.get("Shares"), "Shares", row_number),
                market_value=parse_number(row.get("MarketValue"), "MarketValue", row_number),
                category=holding_category(ticker),
            )
        )

    if not holdings:
        raise SourceDataError("holdings CSV has no data rows")
    if len(dates) != 1:
        raise SourceDataError(f"inconsistent source dates within CSV: {sorted(dates)}")
    if len(net_assets_values) != 1:
        raise SourceDataError("inconsistent repeated NetAssets values within CSV")
    if len(shares_values) != 1:
        raise SourceDataError("inconsistent repeated SharesOutstanding values within CSV")
    csv_date = next(iter(dates))
    if csv_date != source.source_as_of_date:
        raise SourceDataError(
            f"CSV date {csv_date} does not match snapshot date {source.source_as_of_date}"
        )
    net_assets = next(iter(net_assets_values))
    shares_outstanding = next(iter(shares_values))
    if net_assets <= 0:
        raise SourceDataError(f"NetAssets must be positive, got {net_assets}")
    if shares_outstanding <= 0:
        raise SourceDataError(f"SharesOutstanding must be positive, got {shares_outstanding}")

    equity = sum((row.market_value for row in holdings if row.category == "equity"), Decimal(0))
    options = sum((row.market_value for row in holdings if row.category == "option"), Decimal(0))
    cash_other = sum((row.market_value for row in holdings if row.category == "cash_other"), Decimal(0))
    residual = net_assets - equity - options - cash_other
    warnings: list[str] = []
    if len(holdings) != source.record_count:
        warnings.append(
            f"raw source {source.raw_source_id} metadata says {source.record_count} records; "
            f"archive contains {len(holdings)}"
        )
    try:
        fetched_date = datetime.fromisoformat(source.fetched_at_utc.replace("Z", "+00:00")).date()
        if date.fromisoformat(source.source_as_of_date) > fetched_date:
            warnings.append(
                f"publisher source date {source.source_as_of_date} is after fetch date {fetched_date}"
            )
    except ValueError:
        warnings.append(f"malformed fetch timestamp: {source.fetched_at_utc!r}")

    return NavSnapshot(
        source=source,
        net_assets=net_assets,
        shares_outstanding=shares_outstanding,
        nav_per_share=net_assets / shares_outstanding,
        equity_value=equity,
        option_value=options,
        cash_other_value=cash_other,
        residual=residual,
        residual_pct=residual / net_assets * 100,
        holdings=tuple(holdings),
        warnings=tuple(warnings),
    )


def calculate_change(start: NavSnapshot, end: NavSnapshot) -> DailyChange:
    shares_change = end.shares_outstanding - start.shares_outstanding
    return DailyChange(
        start=start,
        end=end,
        net_asset_change=end.net_assets - start.net_assets,
        nav_change_pct=(end.nav_per_share / start.nav_per_share - 1) * 100,
        shares_change=shares_change,
        shares_change_pct=shares_change / start.shares_outstanding * 100,
        approximate_flow_value=shares_change * end.nav_per_share,
    )


def build_history(conn: sqlite3.Connection) -> tuple[list[NavSnapshot], list[str]]:
    history: list[NavSnapshot] = []
    warnings: list[str] = []
    for source in select_validated_snapshots(conn):
        try:
            history.append(parse_archived_snapshot(source))
        except SourceDataError as exc:
            warnings.append(
                f"snapshot {source.snapshot_id} / raw source {source.raw_source_id} "
                f"({source.source_as_of_date}) skipped: {exc}"
            )
    return history, warnings


def money(value: Decimal, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


def number(value: Decimal, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:,.0f}"


def percent(value: Decimal, signed: bool = False, places: int = 2) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:,.{places}f}%"


def option_contract_counts(snapshot: NavSnapshot) -> tuple[Decimal, Decimal]:
    options = (row for row in snapshot.holdings if row.category == "option")
    short = Decimal(0)
    long = Decimal(0)
    for row in options:
        if row.quantity < 0:
            short += -row.quantity
        elif row.quantity > 0:
            long += row.quantity
    return short, long


def equity_change_summary(start: NavSnapshot, end: NavSnapshot) -> tuple[int, Decimal | None]:
    old = {row.ticker: row.quantity for row in start.holdings if row.category == "equity"}
    new = {row.ticker: row.quantity for row in end.holdings if row.category == "equity"}
    changes = [
        (new[ticker] / old[ticker] - 1) * 100
        for ticker in sorted(old.keys() & new.keys())
        if old[ticker] != 0
    ]
    if not changes:
        return 0, None
    changes.sort()
    middle = len(changes) // 2
    median = changes[middle] if len(changes) % 2 else (changes[middle - 1] + changes[middle]) / 2
    return len(changes), median


def october_assessment(history: list[NavSnapshot]) -> list[str]:
    by_date = {row.source.source_as_of_date: row for row in history}
    start = by_date.get("2026-10-15")
    end = by_date.get("2026-10-16")
    if not start or not end:
        return ["Oct 15 -> Oct 16 assessment unavailable: one or both validated snapshots are missing."]
    change = calculate_change(start, end)
    common_count, median_equity_pct = equity_change_summary(start, end)
    start_short, start_long = option_contract_counts(start)
    end_short, end_long = option_contract_counts(end)
    option_pct = (
        (end_short / start_short - 1) * 100 if start_short else Decimal(0)
    )
    equity_text = (
        f"median quantity change across {common_count} common equities was "
        f"{percent(median_equity_pct, signed=True, places=3)}"
        if median_equity_pct is not None
        else "no common equities were available for comparison"
    )
    consistent = (
        change.shares_change > 0
        and median_equity_pct is not None
        and abs(median_equity_pct - change.shares_change_pct) <= Decimal("0.10")
        and abs(option_pct - change.shares_change_pct) <= Decimal("0.10")
    )
    conclusion = (
        "The reported shares, common-equity quantities, and option contract counts are consistent "
        "with proportionate fund expansion and net ETF share creations."
        if consistent
        else "The available measures do not all support a clear proportionate-expansion conclusion."
    )
    return [
        f"Reported shares outstanding: {number(start.shares_outstanding)} -> "
        f"{number(end.shares_outstanding)} ({percent(change.shares_change_pct, signed=True, places=3)}).",
        f"Equities: {equity_text}.",
        f"Option contracts: {number(start_short)} short / {number(start_long)} long -> "
        f"{number(end_short)} short / {number(end_long)} long "
        f"({percent(option_pct, signed=True, places=3)} for short contracts).",
        conclusion,
    ]


def render_history(history: list[NavSnapshot], warnings: list[str]) -> str:
    lines = ["CHPY NAV AND FUND FLOW HISTORY", ""]
    header = (
        f"{'Date':<10} | {'Net Assets':>17} | {'Shares Outstanding':>18} | "
        f"{'NAV/Share':>10} | {'NAV Change':>10} | {'Shares Change':>18} | {'Holdings Residual':>23}"
    )
    lines.extend([header, "-" * len(header)])
    previous: NavSnapshot | None = None
    for snapshot in history:
        change = calculate_change(previous, snapshot) if previous else None
        nav_change = percent(change.nav_change_pct, signed=True) if change else "--"
        shares_change = (
            f"{number(change.shares_change, signed=True)} ({percent(change.shares_change_pct, signed=True)})"
            if change
            else "--"
        )
        residual = f"{money(snapshot.residual)} ({percent(snapshot.residual_pct)})"
        lines.append(
            f"{snapshot.source.source_as_of_date:<10} | {money(snapshot.net_assets):>17} | "
            f"{number(snapshot.shares_outstanding):>18} | ${snapshot.nav_per_share:>9.4f} | "
            f"{nav_change:>10} | {shares_change:>18} | {residual:>23}"
        )
        previous = snapshot

    lines.extend(["", "SOURCE PROVENANCE AND HOLDINGS RECONCILIATION"])
    for snapshot in history:
        source = snapshot.source
        summed = snapshot.equity_value + snapshot.option_value + snapshot.cash_other_value
        lines.extend(
            [
                f"{source.source_as_of_date}: snapshot_id={source.snapshot_id}, "
                f"raw_source_id={source.raw_source_id}, version={source.version}, "
                f"fetched={source.fetched_at_utc}",
                f"  Equity {money(snapshot.equity_value)}; signed options {money(snapshot.option_value)}; "
                f"cash/other {money(snapshot.cash_other_value)}; sum {money(summed)}; "
                f"published net assets {money(snapshot.net_assets)}; "
                f"residual {money(snapshot.residual)} ({percent(snapshot.residual_pct)}).",
            ]
        )
        for warning in snapshot.warnings:
            lines.append(f"  WARNING: {warning}")

    lines.extend(["", "CREATIONS / REDEMPTIONS"])
    for start, end in zip(history, history[1:]):
        change = calculate_change(start, end)
        lines.extend(
            [
                f"Start date: {start.source.source_as_of_date}",
                f"End date: {end.source.source_as_of_date}",
                f"Change in net assets: {money(change.net_asset_change, signed=True)}",
                f"Change in shares: {number(change.shares_change, signed=True)}",
                f"Percentage change: {percent(change.shares_change_pct, signed=True, places=3)}",
                f"Approximate net creation/redemption value (share change x end-date NAV/share): "
                f"{money(change.approximate_flow_value, signed=True)}",
                f"Assessment: {change.direction}.",
                "",
            ]
        )

    lines.append("OCTOBER 15 -> OCTOBER 16 ASSESSMENT")
    lines.extend(october_assessment(history))
    all_warnings = [*warnings]
    if all_warnings:
        lines.extend(["", "WARNINGS"])
        lines.extend(f"- {warning}" for warning in all_warnings)
    lines.extend(
        [
            "",
            "LIMITATIONS",
            "NAV change is unadjusted and is not labeled total return; verified distribution data is not included.",
            "The flow value is an end-date NAV approximation, not exact investor cash flow.",
            "Holdings residuals are reported as published and may reflect rounding, other assets/liabilities, or reporting conventions.",
        ]
    )
    return "\n".join(lines)


def open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="display all validated snapshot dates")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    if not args.history:
        parser.error("--history is required")
    with open_read_only(args.db) as conn:
        history, warnings = build_history(conn)
    if not history:
        detail = "\n".join(f"WARNING: {warning}" for warning in warnings)
        message = "No usable validated CHPY holdings snapshots found."
        raise SystemExit(f"{message}\n{detail}" if detail else message)
    print(render_history(history, warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
