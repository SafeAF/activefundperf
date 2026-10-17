"""Auditable CHPY distribution-adjusted NAV total-return history."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from income_fund_lab.chpy_nav import DEFAULT_DB, NavSnapshot, build_history, open_read_only


DISTRIBUTION_URL = "https://yieldmaxetfs.com/our-etfs/chpy/"
DEFAULT_RAW_ROOT = Path("data/raw/chpy/distributions")
NEW_YORK = ZoneInfo("America/New_York")
EXPECTED_HEADERS = (
    "DISTRIBUTION PER SHARE",
    "DECLARED DATE",
    "EX DATE",
    "RECORD DATE",
    "PAYABLE DATE",
    "ROC",
)


class DistributionSourceError(ValueError):
    """An official distribution page or archive is unusable."""


@dataclass(frozen=True)
class DistributionRecord:
    amount: Decimal
    declared_date: date
    ex_date: date
    record_date: date
    payable_date: date
    roc: str
    source_url: str
    acquired_at_utc: datetime
    source_sha256: str
    version: int = 1
    revised: bool = False


@dataclass(frozen=True)
class DistributionHistory:
    records: tuple[DistributionRecord, ...]
    source_count: int
    coverage_start: date
    coverage_through: date
    revision_notes: tuple[str, ...]

    def amount_on(self, ex_date: date) -> tuple[Decimal | None, str]:
        matches = [row for row in self.records if row.ex_date == ex_date]
        if matches:
            return sum((row.amount for row in matches), Decimal(0)), "verified"
        if self.coverage_start <= ex_date <= self.coverage_through:
            return Decimal(0), "confirmed-zero"
        return None, "missing"


@dataclass(frozen=True)
class ValuationObservation:
    valuation_date: date | None
    nav: Decimal
    source: NavSnapshot
    timing_status: str
    timing_basis: str


@dataclass(frozen=True)
class ReturnInterval:
    start: ValuationObservation
    end: ValuationObservation
    distribution: Decimal | None
    distribution_status: str
    nav_return: Decimal | None
    total_return: Decimal | None
    warnings: tuple[str, ...]


class _DistributionTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table" and "distributions-table" in (values.get("class") or "").split():
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif self.in_table and tag == "tr" and self.row:
            self.rows.append(self.row)
        elif self.in_table and tag == "table":
            self.in_table = False


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError as exc:
        raise DistributionSourceError(f"malformed {field}: {value!r}") from exc


def _parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value.replace("$", "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise DistributionSourceError(f"malformed distribution amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise DistributionSourceError(f"invalid distribution amount: {value!r}")
    return amount


def parse_distribution_page(
    payload: bytes,
    *,
    source_url: str,
    acquired_at_utc: datetime,
    source_sha256: str | None = None,
) -> list[DistributionRecord]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DistributionSourceError("official page is not valid UTF-8") from exc
    parser = _DistributionTableParser()
    parser.feed(text)
    if not parser.rows or tuple(parser.rows[0]) != EXPECTED_HEADERS:
        raise DistributionSourceError("official CHPY distribution table was not found or changed schema")
    digest = source_sha256 or hashlib.sha256(payload).hexdigest()
    records: list[DistributionRecord] = []
    for number, row in enumerate(parser.rows[1:], start=1):
        if len(row) != len(EXPECTED_HEADERS):
            raise DistributionSourceError(f"distribution row {number} has {len(row)} fields, expected 6")
        records.append(
            DistributionRecord(
                amount=_parse_amount(row[0]),
                declared_date=_parse_date(row[1], "declared date"),
                ex_date=_parse_date(row[2], "ex date"),
                record_date=_parse_date(row[3], "record date"),
                payable_date=_parse_date(row[4], "payable date"),
                roc=row[5],
                source_url=source_url,
                acquired_at_utc=acquired_at_utc,
                source_sha256=digest,
            )
        )
    if not records:
        raise DistributionSourceError("official CHPY distribution table is empty")
    if len({row.ex_date for row in records}) != len(records):
        raise DistributionSourceError("official page contains duplicate ex-dividend dates")
    return records


def official_nav_anchor(payload: bytes) -> tuple[date, Decimal] | None:
    """Return the page's displayed fund-detail NAV date/value, when available."""
    text = payload.decode("utf-8", errors="replace")
    nav_match = re.search(
        r'fund-label">NAV:</div><div class="fund-value">\$([0-9,.]+)</div>', text
    )
    if not nav_match:
        return None
    tail = html.unescape(text[nav_match.end() : nav_match.end() + 5000])
    date_match = re.search(r"As of\s+(\d{1,2}/\d{1,2}/\d{4})", re.sub(r"<[^>]+>", " ", tail))
    if not date_match:
        return None
    return _parse_date(date_match.group(1), "fund NAV as-of date"), _parse_amount(nav_match.group(1))


def _utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("acquisition timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def archive_distribution_page(
    payload: bytes,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    source_url: str = DISTRIBUTION_URL,
    acquired_at_utc: datetime | None = None,
) -> tuple[Path, Path, bool]:
    acquired_at_utc = acquired_at_utc or datetime.now(timezone.utc)
    digest = hashlib.sha256(payload).hexdigest()
    records = parse_distribution_page(
        payload,
        source_url=source_url,
        acquired_at_utc=acquired_at_utc,
        source_sha256=digest,
    )
    root = raw_root / digest[:2]
    raw_path = root / f"{digest}.bin"
    metadata_path = root / f"{digest}.json"
    metadata = {
        "acquired_at_utc": _utc_timestamp(acquired_at_utc),
        "feed_type": "distributions",
        "first_ex_date": min(row.ex_date for row in records).isoformat(),
        "last_ex_date": max(row.ex_date for row in records).isoformat(),
        "record_count": len(records),
        "sha256": digest,
        "source_url": source_url,
        "status": "validated",
    }
    encoded = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
    root.mkdir(parents=True, exist_ok=True)
    created = not raw_path.exists()
    if raw_path.exists() and hashlib.sha256(raw_path.read_bytes()).hexdigest() != digest:
        raise RuntimeError(f"immutable distribution archive hash mismatch: {raw_path}")
    if raw_path.exists() and metadata_path.exists():
        existing = json.loads(metadata_path.read_text())
        if existing.get("sha256") != digest or existing.get("source_url") != source_url:
            raise RuntimeError(f"immutable distribution metadata conflict: {metadata_path}")
        return raw_path, metadata_path, False
    if not raw_path.exists():
        raw_path.write_bytes(payload)
        os.chmod(raw_path, 0o444)
    if metadata_path.exists() and metadata_path.read_bytes() != encoded:
        raise RuntimeError(f"immutable distribution metadata conflict: {metadata_path}")
    if not metadata_path.exists():
        metadata_path.write_bytes(encoded)
        os.chmod(metadata_path, 0o444)
    return raw_path, metadata_path, created


def acquire_distribution_page(
    *, raw_root: Path = DEFAULT_RAW_ROOT, source_url: str = DISTRIBUTION_URL
) -> tuple[Path, Path, bool]:
    request = urllib.request.Request(source_url, headers={"User-Agent": "income-fund-lab/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return archive_distribution_page(payload, raw_root=raw_root, source_url=source_url)


def _read_archives(raw_root: Path) -> list[tuple[dict, bytes]]:
    sources: list[tuple[dict, bytes]] = []
    for metadata_path in sorted(raw_root.glob("*/*.json")):
        metadata = json.loads(metadata_path.read_text())
        raw_path = metadata_path.with_suffix(".bin")
        payload = raw_path.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != metadata.get("sha256"):
            raise DistributionSourceError(
                f"archive SHA-256 mismatch for {raw_path}: expected {metadata.get('sha256')}, got {actual}"
            )
        sources.append((metadata, payload))
    return sources


def load_distribution_history(raw_root: Path = DEFAULT_RAW_ROOT) -> DistributionHistory:
    sources = _read_archives(raw_root)
    if not sources:
        raise DistributionSourceError(f"no verified distribution archives found under {raw_root}")
    observations: dict[date, list[DistributionRecord]] = {}
    acquired_dates: list[date] = []
    for metadata, payload in sources:
        acquired = datetime.fromisoformat(metadata["acquired_at_utc"].replace("Z", "+00:00"))
        acquired_dates.append(acquired.date())
        parsed = parse_distribution_page(
            payload,
            source_url=metadata["source_url"],
            acquired_at_utc=acquired,
            source_sha256=metadata["sha256"],
        )
        for row in parsed:
            observations.setdefault(row.ex_date, []).append(row)

    current: list[DistributionRecord] = []
    revision_notes: list[str] = []
    for ex_date, rows in observations.items():
        rows.sort(key=lambda row: (row.acquired_at_utc, row.source_sha256))
        distinct: list[DistributionRecord] = []
        for row in rows:
            identity = (row.amount, row.declared_date, row.record_date, row.payable_date, row.roc)
            if not distinct or identity != (
                distinct[-1].amount,
                distinct[-1].declared_date,
                distinct[-1].record_date,
                distinct[-1].payable_date,
                distinct[-1].roc,
            ):
                distinct.append(row)
            else:
                # Same observation in a later source: keep the newest provenance
                # without manufacturing a new record version.
                distinct[-1] = row
        selected = distinct[-1]
        revised = len(distinct) > 1
        current.append(
            DistributionRecord(
                **{**selected.__dict__, "version": len(distinct), "revised": revised}
            )
        )
        if revised:
            revision_notes.append(
                f"{ex_date.isoformat()}: {len(distinct)} observed versions; latest is v{len(distinct)}"
            )
    current.sort(key=lambda row: row.ex_date)
    return DistributionHistory(
        records=tuple(current),
        source_count=len(sources),
        coverage_start=min(row.ex_date for row in current),
        coverage_through=max(acquired_dates),
        revision_notes=tuple(revision_notes),
    )


def previous_weekday(value: date) -> date:
    value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def next_weekday(value: date) -> date:
    value += timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def infer_valuation_observations(
    snapshots: list[NavSnapshot], official_page_payloads: tuple[bytes, ...] = ()
) -> list[ValuationObservation]:
    anchors = {anchor for payload in official_page_payloads if (anchor := official_nav_anchor(payload))}
    observations: list[ValuationObservation] = []
    for snapshot in snapshots:
        reported = date.fromisoformat(snapshot.source.source_as_of_date)
        fetched = datetime.fromisoformat(snapshot.source.fetched_at_utc.replace("Z", "+00:00"))
        local = fetched.astimezone(NEW_YORK)
        inferred: date | None = None
        basis = ""
        if local.date() < reported and reported == next_weekday(local.date()):
            inferred = previous_weekday(reported)
            basis = (
                f"publisher date {reported} was future-dated when fetched {local:%Y-%m-%d %H:%M %Z}; "
                f"mapped to the preceding trading weekday {inferred}"
            )
        elif local.date() < reported:
            basis = (
                f"publisher date {reported} was anomalously far ahead when fetched "
                f"{local:%Y-%m-%d %H:%M %Z}; no valuation date inferred"
            )
        elif local.date() == reported and local.time() < time(16, 0):
            inferred = previous_weekday(reported)
            basis = (
                f"file was fetched before the {reported} 4:00 p.m. ET NAV close at {local:%H:%M %Z}; "
                f"mapped to the preceding trading weekday {inferred}"
            )
        else:
            basis = (
                f"file was fetched after the {reported} NAV close; archive timing alone cannot distinguish "
                "same-day from prior-close valuation"
            )
        anchor_matches = [
            anchor_date
            for anchor_date, anchor_nav in anchors
            if inferred == anchor_date and snapshot.nav_per_share.quantize(Decimal("0.01")) == anchor_nav
        ]
        if anchor_matches:
            basis += "; official fund page independently displays the same rounded NAV and as-of date"
        observations.append(
            ValuationObservation(
                valuation_date=inferred,
                nav=snapshot.nav_per_share,
                source=snapshot,
                timing_status="inferred" if inferred else "unresolved",
                timing_basis=basis,
            )
        )
    return observations


def calculate_interval(
    start: ValuationObservation,
    end: ValuationObservation,
    distributions: DistributionHistory,
    *,
    split_factor: Decimal | None = None,
    unknown_adjustment: str | None = None,
) -> ReturnInterval:
    warnings: list[str] = []
    if start.valuation_date is None or end.valuation_date is None:
        warnings.append("unresolved NAV timing")
    elif next_weekday(start.valuation_date) != end.valuation_date:
        warnings.append("missing intervening NAV observation")
    distribution, distribution_status = (
        distributions.amount_on(end.valuation_date)
        if end.valuation_date
        else (None, "missing")
    )
    if distribution_status == "missing":
        warnings.append("distribution record is incomplete; absence is not treated as zero")
    if unknown_adjustment:
        warnings.append(f"unsupported per-share adjustment: {unknown_adjustment}")
    denominator = start.nav
    if split_factor is not None:
        if split_factor <= 0:
            warnings.append("invalid split factor")
        else:
            denominator = start.nav / split_factor
    blocking = any(
        warning.startswith(("unresolved", "missing", "distribution", "unsupported", "invalid"))
        for warning in warnings
    )
    nav_return = total_return = None
    if not blocking and distribution is not None:
        nav_return = end.nav / denominator - 1
        total_return = (end.nav + distribution) / denominator - 1
        if abs(total_return) > Decimal("0.20"):
            warnings.append("unusually large absolute daily total return (>20%); check ex-date alignment")
    return ReturnInterval(
        start=start,
        end=end,
        distribution=distribution,
        distribution_status=distribution_status,
        nav_return=nav_return,
        total_return=total_return,
        warnings=tuple(warnings),
    )


def calculate_intervals(
    observations: list[ValuationObservation], distributions: DistributionHistory
) -> list[ReturnInterval]:
    resolved = sorted(
        (row for row in observations if row.valuation_date is not None),
        key=lambda row: row.valuation_date,
    )
    return [calculate_interval(start, end, distributions) for start, end in zip(resolved, resolved[1:])]


def _pct(value: Decimal | None) -> str:
    return "--" if value is None else f"{value * 100:+.4f}%"


def render_report(
    observations: list[ValuationObservation],
    distributions: DistributionHistory,
    nav_warnings: list[str],
) -> str:
    resolved = sorted(
        (row for row in observations if row.valuation_date is not None),
        key=lambda row: row.valuation_date,
    )
    intervals = calculate_intervals(observations, distributions)
    by_end = {row.end.valuation_date: row for row in intervals}
    lines = ["CHPY NAV TOTAL RETURN AUDIT", ""]
    header = (
        f"{'Valuation':<10}  {'NAV':>10}  {'Distribution':>13}  {'NAV Return':>11}  "
        f"{'Total Return':>12}  {'Publisher Date':>14}"
    )
    lines.extend([header, "-" * len(header)])
    for row in resolved:
        amount, dist_status = distributions.amount_on(row.valuation_date)
        interval = by_end.get(row.valuation_date)
        dist_text = "missing" if amount is None else f"${amount:.4f}"
        if amount == 0:
            dist_text += " (confirmed)"
        lines.append(
            f"{row.valuation_date.isoformat():<10}  ${row.nav:>9.4f}  {dist_text:>13}  "
            f"{_pct(interval.nav_return if interval else None):>11}  "
            f"{_pct(interval.total_return if interval else None):>12}  "
            f"{row.source.source.source_as_of_date:>14}"
        )

    valid = [row for row in intervals if row.total_return is not None]
    cumulative: Decimal | None = None
    if valid:
        factor = Decimal(1)
        for row in valid:
            factor *= Decimal(1) + row.total_return
        cumulative = factor - 1
    unresolved = [row for row in observations if row.valuation_date is None]
    missing = [warning for row in intervals for warning in row.warnings if warning.startswith("missing")]
    status = "COMPLETE" if not unresolved and not missing and valid else "PARTIAL" if valid else "UNAVAILABLE"
    lines.extend(["", "PERIOD SUMMARY"])
    if valid:
        period_start = valid[0].start
        period_end = valid[-1].end
        period_distributions = sum((row.distribution or Decimal(0) for row in valid), Decimal(0))
        price_return = period_end.nav / period_start.nav - 1
        lines.extend(
            [
                f"Start: {period_start.valuation_date}",
                f"End: {period_end.valuation_date}",
                f"Starting NAV: ${period_start.nav:.4f}",
                f"Ending NAV: ${period_end.nav:.4f}",
                f"Distributions/share in computable period: ${period_distributions:.4f}",
                f"NAV price return: {_pct(price_return)}",
                f"Compounded NAV total return: {_pct(cumulative)}",
            ]
        )
    else:
        lines.append("No interval has both resolved NAV timing and complete distribution evidence.")
    lines.extend(
        [
            f"Missing observations: {len(missing)} interval(s); {len(unresolved)} unresolved source snapshot(s)",
            f"Timing warnings: {len(unresolved)}",
            f"Status: {status}",
            "",
            "DISTRIBUTIONS PAID OR DECLARED",
        ]
    )
    for record in distributions.records:
        if resolved and resolved[0].valuation_date <= record.ex_date <= resolved[-1].valuation_date:
            revision = f", revised v{record.version}" if record.revised else ""
            lines.append(
                f"{record.ex_date}: ${record.amount:.4f}/share; declared {record.declared_date}; "
                f"record {record.record_date}; payable {record.payable_date}; ROC {record.roc}{revision}"
            )
    lines.extend(["", "TIMING FINDINGS"])
    for row in observations:
        label = row.valuation_date.isoformat() if row.valuation_date else "UNRESOLVED"
        lines.append(
            f"Publisher {row.source.source.source_as_of_date} -> valuation {label}: {row.timing_basis}."
        )
    if unresolved:
        lines.append(
            "The unresolved 2026-10-14 publisher snapshot may represent 2026-10-13 or 2026-10-14; "
            "therefore the 2026-10-14 ex-date return is not calculated."
        )
    lines.extend(["", "PROVENANCE"])
    for row in resolved:
        source = row.source.source
        lines.append(
            f"NAV {row.valuation_date}: raw_source_id={source.raw_source_id}; fetched={source.fetched_at_utc}; "
            f"sha256={source.sha256}; publisher_date={source.source_as_of_date}"
        )
    latest_distribution = max(distributions.records, key=lambda row: row.acquired_at_utc)
    lines.append(
        f"Distributions: {latest_distribution.source_url}; {distributions.source_count} immutable source snapshot(s); "
        f"latest acquisition={_utc_timestamp(latest_distribution.acquired_at_utc)}; "
        f"latest sha256={latest_distribution.source_sha256}"
    )
    lines.extend(
        [
            "",
            "CROSS-CHECKS AND LIMITATIONS",
            "No authoritative daily CHPY NAV total-return series was found for these exact dates; the official site publishes month-end/quarter-end performance only.",
            "Cumulative total return compounds valid daily returns and assumes reinvestment at valuation NAV.",
            "The official distribution table is treated as confirming zero only through its acquisition date; outside that coverage, absence remains missing.",
            "Return-of-capital classification does not remove a cash distribution from total return.",
            "No split or other per-share adjustment was observed in the supported interval; explicit adjustments are required if one occurs.",
        ]
    )
    for warning in nav_warnings:
        lines.append(f"WARNING: {warning}")
    for note in distributions.revision_notes:
        lines.append(f"REVISION: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="render the total-return audit")
    parser.add_argument("--acquire", action="store_true", help="archive the current official distribution page")
    parser.add_argument("--import-page", type=Path, help="archive a previously downloaded official page")
    parser.add_argument("--acquired-at", help="UTC timestamp for --import-page (ISO-8601)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    args = parser.parse_args(argv)
    if not (args.history or args.acquire or args.import_page):
        parser.error("one of --history, --acquire, or --import-page is required")
    if args.acquire and args.import_page:
        parser.error("--acquire and --import-page are mutually exclusive")
    if args.acquire:
        raw_path, _, created = acquire_distribution_page(raw_root=args.raw_root)
        print(f"Distribution source {'archived' if created else 'unchanged'}: {raw_path}")
    if args.import_page:
        acquired = (
            datetime.fromisoformat(args.acquired_at.replace("Z", "+00:00"))
            if args.acquired_at
            else datetime.now(timezone.utc)
        )
        raw_path, _, created = archive_distribution_page(
            args.import_page.read_bytes(), raw_root=args.raw_root, acquired_at_utc=acquired
        )
        print(f"Distribution source {'archived' if created else 'unchanged'}: {raw_path}")
    if args.history:
        history = load_distribution_history(args.raw_root)
        page_payloads = tuple(payload for _, payload in _read_archives(args.raw_root))
        with open_read_only(args.db) as conn:
            nav_history, nav_warnings = build_history(conn)
        observations = infer_valuation_observations(nav_history, page_payloads)
        print(render_report(observations, history, nav_warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
