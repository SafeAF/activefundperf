from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from income_fund_lab.chpy_nav import HoldingValue, NavSnapshot, SourceSnapshot
from income_fund_lab.chpy_total_return import (
    DistributionHistory,
    DistributionRecord,
    ValuationObservation,
    archive_distribution_page,
    calculate_interval,
    calculate_intervals,
    infer_valuation_observations,
    load_distribution_history,
    parse_distribution_page,
)


def page(amount="0.5140", ex_date="10/14/2026", *, nav="62.34", nav_date="10/15/2026"):
    return f"""
    <html><div class="fund-info-table"><div class="fund-label">NAV:</div><div class="fund-value">${nav}</div></div>
    <p>As of {nav_date}</p>
    <table class="distributions-table"><thead><tr>
    <th>DISTRIBUTION PER SHARE</th><th>DECLARED DATE</th><th>EX DATE</th>
    <th>RECORD DATE</th><th>PAYABLE DATE</th><th>ROC</th>
    </tr></thead><tbody><tr>
    <td>${amount}</td><td>10/13/2026</td><td>{ex_date}</td><td>10/14/2026</td><td>10/15/2026</td><td>37.84%</td>
    </tr></tbody></table></html>
    """.encode()


def distribution_history(*records, through=date(2026, 10, 17)):
    return DistributionHistory(
        records=tuple(records),
        source_count=1,
        coverage_start=date(2026, 4, 17),
        coverage_through=through,
        revision_notes=(),
    )


def distribution(amount="0.5140", ex_date=date(2026, 10, 14)):
    return DistributionRecord(
        amount=Decimal(amount),
        declared_date=date(2026, 10, 13),
        ex_date=ex_date,
        record_date=ex_date,
        payable_date=date(2026, 10, 15),
        roc="37.84%",
        source_url="https://yieldmaxetfs.com/our-etfs/chpy/",
        acquired_at_utc=datetime(2026, 10, 17, tzinfo=timezone.utc),
        source_sha256="a" * 64,
    )


def nav_snapshot(reported, fetched, nav, *, source_id=1):
    shares = Decimal("100")
    source = SourceSnapshot(
        snapshot_id=source_id,
        raw_source_id=source_id,
        source_as_of_date=reported,
        version=1,
        is_current=True,
        fetched_at_utc=fetched,
        record_count=1,
        sha256=str(source_id) * 64,
        archive_path=Path(f"/{source_id}.csv"),
    )
    return NavSnapshot(
        source=source,
        net_assets=Decimal(nav) * shares,
        shares_outstanding=shares,
        nav_per_share=Decimal(nav),
        equity_value=Decimal(nav) * shares,
        option_value=Decimal(0),
        cash_other_value=Decimal(0),
        residual=Decimal(0),
        residual_pct=Decimal(0),
        holdings=(HoldingValue("AMD", shares, Decimal(nav) * shares, "equity"),),
        warnings=(),
    )


def observation(day, nav):
    snapshot = nav_snapshot(day.isoformat(), f"{day.isoformat()}T22:00:00Z", nav)
    return ValuationObservation(day, Decimal(nav), snapshot, "verified", "test")


def test_nav_return_and_ex_date_distribution_adjustment():
    start = observation(date(2026, 10, 13), "100")
    end = observation(date(2026, 10, 14), "95")

    interval = calculate_interval(start, end, distribution_history(distribution("2")))

    assert interval.nav_return == Decimal("-0.05")
    assert interval.total_return == Decimal("-0.03")
    assert interval.distribution_status == "verified"


def test_compounding_uses_products_not_sum():
    rows = [
        observation(date(2026, 10, 14), "100"),
        observation(date(2026, 10, 15), "110"),
        observation(date(2026, 10, 16), "99"),
    ]
    intervals = calculate_intervals(rows, distribution_history())
    factor = Decimal(1)
    for interval in intervals:
        factor *= Decimal(1) + interval.total_return

    assert [row.total_return for row in intervals] == [Decimal("0.1"), Decimal("-0.1")]
    assert factor - 1 == Decimal("-0.01")


def test_missing_distribution_is_not_silently_zero_but_covered_absence_is():
    start = observation(date(2026, 10, 15), "100")
    covered = observation(date(2026, 10, 16), "101")
    future = observation(date(2026, 10, 19), "102")
    history = distribution_history()

    confirmed = calculate_interval(start, covered, history)
    missing = calculate_interval(covered, future, history)

    assert confirmed.distribution == 0
    assert confirmed.distribution_status == "confirmed-zero"
    assert confirmed.total_return == Decimal("0.01")
    assert missing.distribution is None
    assert missing.total_return is None


def test_weekend_future_date_and_premarket_sources_map_to_previous_weekday():
    snapshots = [
        nav_snapshot("2026-10-16", "2026-10-16T05:30:06Z", "62.3396"),
        nav_snapshot("2026-10-19", "2026-10-17T05:30:04Z", "59.9723", source_id=2),
    ]

    rows = infer_valuation_observations(snapshots, (page(),))

    assert [row.valuation_date for row in rows] == [date(2026, 10, 15), date(2026, 10, 16)]
    assert "official fund page" in rows[0].timing_basis


def test_after_close_same_day_source_remains_unresolved():
    snapshot = nav_snapshot("2026-10-14", "2026-10-14T23:41:27Z", "65.7645")

    row = infer_valuation_observations([snapshot])[0]

    assert row.valuation_date is None
    assert row.timing_status == "unresolved"


def test_duplicate_import_is_idempotent_and_revision_is_preserved(tmp_path):
    first = page("0.5140")
    revised = page("0.5150")
    acquired1 = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    acquired2 = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    _, _, created = archive_distribution_page(first, raw_root=tmp_path, acquired_at_utc=acquired1)
    _, _, duplicate_created = archive_distribution_page(first, raw_root=tmp_path, acquired_at_utc=acquired1)
    archive_distribution_page(revised, raw_root=tmp_path, acquired_at_utc=acquired2)
    history = load_distribution_history(tmp_path)

    assert created is True
    assert duplicate_created is False
    assert history.records[0].amount == Decimal("0.5150")
    assert history.records[0].version == 2
    assert history.records[0].revised is True
    assert len(history.revision_notes) == 1


def test_missing_nav_observation_blocks_interval():
    start = observation(date(2026, 10, 14), "100")
    end = observation(date(2026, 10, 16), "102")

    interval = calculate_interval(start, end, distribution_history())

    assert interval.total_return is None
    assert "missing intervening NAV observation" in interval.warnings


def test_explicit_split_is_supported_and_unknown_adjustment_is_blocked():
    start = observation(date(2026, 10, 14), "100")
    end = observation(date(2026, 10, 15), "51")
    history = distribution_history()

    split = calculate_interval(start, end, history, split_factor=Decimal(2))
    unsupported = calculate_interval(start, end, history, unknown_adjustment="rights offering")

    assert split.nav_return == Decimal("0.02")
    assert split.total_return == Decimal("0.02")
    assert unsupported.total_return is None


def test_distribution_parser_preserves_source_provenance():
    payload = page()
    acquired = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    digest = hashlib.sha256(payload).hexdigest()

    record = parse_distribution_page(
        payload,
        source_url="https://yieldmaxetfs.com/our-etfs/chpy/",
        acquired_at_utc=acquired,
    )[0]

    assert record.source_sha256 == digest
    assert record.source_url == "https://yieldmaxetfs.com/our-etfs/chpy/"
    assert record.acquired_at_utc == acquired
