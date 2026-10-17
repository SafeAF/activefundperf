# income-fund-lab

## Purpose

This project collects and archives the publicly disclosed holdings and trade data for the actively managed income ETFs USOY and CHPY. It is intentionally narrow: it does not produce analytics, dashboards, trading logic, or performance estimates. It focuses on reliable archival of original publisher files, traceable normalized records, and repeatable automation.

## Future functionality
With daily holdings snapshots plus intraday trades, we can calculate:
- Options coverage: How much of each stock position is covered by short calls?
- Net option exposure: What is the marked liability of outstanding spreads?
- Roll profitability: How much did they originally collect, and how much did they pay to close each spread?
- Premium efficiency: How much premium are they generating relative to the capital allocated?
- NAV attribution: How much of the day's portfolio movement comes from stocks versus options?

Are the managers collecting enough premium to make up for the upside they surrender—and are they actually keeping more of that premium than they lose managing their spreads?

## Running:
Report
   PYENV_VERSION=3.12.13 PYTHONPATH=src python -m income_fund_lab.chpy_report

   PYENV_VERSION=3.12.13 PYTHONPATH=src python -m income_fund_lab.chpy_nav --history

Cronjob
   */15 * * * * cd ~/source/activefundperf && /home/sam/.pyenv/versions/3.12.13/bin/python -m income_fund_lab.chpy_intraday_sqlite --db /home/sam/source/activefundperf/data/chpy_intraday.sqlite3 >> /tmp/chpy_intraday.log 2>&1


## Next things to add
 - Reconcile the $968,237 NAV difference. Make it an explicit residual instead of silently treating it as zero.

 - Track option lots through expiration. Associate executions by underlying, expiration, type, and strike, then account for settlements and assignments.

 - Normalize share counts for creations/redemptions. The uniform 1% increase across stock holdings strongly suggests fund-level flows. Don't count those increases as stock-selection decisions.

 - Measure actual strategy profitability by expiration cohort. Then compare that with distributions and NAV total return.

 - Reconstructing a complete option cycle—opening transactions, adjustments, settlements, and final P&L

 - daily call-coverage-by-underlying metric.

## Goals 
 - Is CHPY making money on its options? Track each option position from opening to closing, expiration, exercise, or assignment, including adjustments along the way.

 - Is CHPY actually outperforming SMH? Compare their total returns, including reinvested distributions—not just their raw NAV changes.


## Notes

If NVDA, AMD, MRVL, and AVGO consistently remain uncovered while UMC, INTC, and other names are overwritten, that would provide much stronger evidence of deliberate upside preservation.

## Architecture

The application uses a small set of collectors that:

- fetch the original publisher file,
- validate the HTTP response and content shape,
- reject obvious error pages and malformed payloads,
- extract any publisher-reported as-of date,
- record retrieval timestamp in UTC,
- archive the original source bytes without modification,
- create deterministic normalized JSON for downstream analysis.

The storage layout is date-partitioned under `data/`:

- `data/raw/usoy/`
- `data/raw/chpy/`
- `data/normalized/usoy/`
- `data/normalized/chpy/`

Each archived source is keyed by the source date and a content SHA-256. Repeated runs do not overwrite historical files and do not duplicate identical content.

## Official sources

- USOY holdings: https://www.defianceetfs.com/usoy-full-holdings/
- CHPY holdings: https://yieldmaxetfs.com/wp-content/uploads/funds/CHPY/TidalFG_Holdings_CHPY.csv
- CHPY intraday trades: https://yieldmaxetfs.com/ym/intraday-file

## Source limitations and verified observations

At the time of implementation, the live sources behaved as follows:

- USOY full holdings renders as HTML and the page includes the full holdings table. The collector uses the published page as the source and preserves the original HTML.
- CHPY holdings publishes an official CSV on the YieldMax site. The collector downloads the CSV directly and archives the original file.
- CHPY intraday transactions and daily holdings are collected into the same SQLite database. Each feed is attempted independently and every attempt is recorded.

The GitHub Actions work is designed to archive successful sources even when one source fails, while still reporting a failed overall run if a required source fails.

## Installation on Debian Linux

1. Install Python 3.12 and pip.
2. In the repository root, install the project and dev dependencies:

   python3.12 -m pip install -e '.[dev]'

3. Run the collectors or test suite using the 3.12 interpreter.

## Running the collector manually

From the repository root:

python -m income_fund_lab.collect

The CHPY SQLite collector used by cron is:

python -m income_fund_lab.chpy_intraday_sqlite

Its default database is `data/chpy_intraday.sqlite3`, which is also the default used by `income_fund_lab.chpy_daily_summary`. To inspect collection provenance without fetching:

python -m income_fund_lab.chpy_intraday_sqlite --audit

The CLI prints a concise summary including the fund, source type, source date, number of records, archive path, and new/unchanged/failed status.

## Running tests

pytest -q

The suite is offline and fixture-based. It does not depend on live websites.

## GitHub Actions setup

The repository includes `.github/workflows/collect.yml` with:

- scheduled runs at 18:37 and 22:17 America/New_York, Monday-Friday,
- manual execution via `workflow_dispatch`,
- Python 3.12 setup,
- dependency installation,
- collection execution,
- validation and archive commit logic,
- a final failure state if a required source fails,
- `contents: write` permissions,
- concurrency protection.

This workflow is intended to run automatically, but scheduled GitHub Actions can be delayed or missed. It is not a guaranteed archival service.

## Required repository permissions

The workflow uses:

- `permissions: contents: write`

The GitHub Actions user must be allowed to push to the default branch.

## Data layout and record formats

### Raw archive

Each raw source snapshot is stored under a date-partitioned path with the original bytes preserved:

- `data/raw/usoy/YYYY-MM-DD/<sha256-prefix>/source.bin`
- `data/raw/chpy/YYYY-MM-DD/<sha256-prefix>/source.bin`

A matching `meta.json` file stores:

- fund,
- source type,
- source URL,
- resolved download URL,
- publisher as-of date when available,
- retrieval timestamp in UTC,
- original SHA-256,
- records count,
- any other source metadata that is needed for traceability.

### Normalized records

Normalized output is written under `data/normalized/` as JSON payloads keyed by the same source date and content hash. The holdings schema is intentionally simple and covers fields such as:

- fund
- source_as_of_date
- security_name
- ticker
- CUSIP
- asset_type
- quantity
- market_value
- portfolio_weight
- option_underlying
- option_type
- option_strike
- option_expiration
- raw_security_description

Trade records keep the original published fields and normalize only the fields that are actually supplied by the source.

## What is collected versus what is inferred

The collectors record the publisher's original source data and preserve it without modification. Normalized fields are derived where the publisher explicitly provides sufficient information.

The project does not infer:

- undisclosed option deliverables,
- transaction prices from holdings changes,
- publisher reporting dates when none are available,
- unavailable trade data where the source is inaccessible.

If a value cannot be verified from the published source, it remains unknown and the raw source identifier is preserved.

## CHPY SQLite provenance and revisions

Original CHPY response bytes are stored beneath `data/raw/chpy/` by feed and SHA-256. The raw file and its first-fetch metadata file are content-addressed, created without overwrite, and made read-only. SQLite records every collection run and per-feed result, including validation errors.

Transaction rows retain source ordering and the publisher's original trade code. Row identity combines the raw-file hash and source row number, so genuinely repeated executions are preserved while an identical downloaded file is imported only once.

Distinct holdings files for one reported date are stored as versioned snapshots. Only the newest validated version is current; summaries read the current version instead of aggregating revisions together.

## What is collected now

This slice implements the first data-collection vertical, covering:

- USOY holdings
- CHPY holdings
- CHPY intraday trades and daily holdings in SQLite with immutable source provenance

It does not implement performance analysis, trade P&L, distribution analysis, or any web frontend.

## Verification notes

The local development environment successfully verified:

- offline fixture tests pass,
- live USOY holdings download succeeds,
- live CHPY holdings download succeeds,
- CHPY SQLite migrations preserve pre-existing rows and add provenance columns and tables.

The scheduled GitHub Action is configured to continue archiving successful sources even if one required source fails, while still failing the job once the collection step reports a source error.
