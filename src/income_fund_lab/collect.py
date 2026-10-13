from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from income_fund_lab.collectors import (
    archive_snapshot,
    fetch_url,
    parse_chpy_holdings_csv,
    parse_chpy_trade_csv,
    parse_usoy_html,
    validate_response,
    write_normalized_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _collect_source(fund: str, source_type: str, source_url: str, resolved_url: str, parse_fn, *, raw_dir: Path | None = None) -> dict:
    response = fetch_url(resolved_url)
    payload = response.content
    validate_response(response.status_code, payload, resolved_url)
    text = payload.decode("utf-8", errors="replace")
    params = inspect.signature(parse_fn).parameters
    if "source_url" in params:
        parsed = parse_fn(text, source_url)
    else:
        parsed = parse_fn(text)
    records = parsed.get("records", [])
    source_date = parsed.get("source_as_of_date") or "unknown"
    metadata = {
        "fund": fund,
        "source_type": source_type,
        "source_url": source_url,
        "resolved_url": resolved_url,
        "source_as_of_date": source_date,
        "retrieval_timestamp_utc": _utc_now(),
        "sha256": _sha256(payload),
        "records_count": len(records),
    }
    archive = archive_snapshot(raw_dir or DATA_DIR, fund, source_type, payload, metadata)
    normalized = write_normalized_snapshot(DATA_DIR if raw_dir is None else raw_dir, fund, source_type, records, metadata)
    return {
        "fund": fund,
        "source_type": source_type,
        "reporting_date": source_date,
        "records_count": len(records),
        "archive_path": archive["archive_path"],
        "normalized_path": normalized["archive_path"],
        "status": archive["status"] if archive["status"] != "unchanged" else "unchanged",
    }


def collect_all_sources(base_dir: str | Path = DATA_DIR) -> list[dict]:
    jobs = [
        (
            "USOY",
            "holdings",
            "https://www.defianceetfs.com/usoy-full-holdings/",
            "https://www.defianceetfs.com/usoy-full-holdings/",
            parse_usoy_html,
        ),
        (
            "CHPY",
            "holdings",
            "https://yieldmaxetfs.com/our-etfs/chpy/",
            "https://yieldmaxetfs.com/wp-content/uploads/funds/CHPY/TidalFG_Holdings_CHPY.csv",
            parse_chpy_holdings_csv,
        ),
        (
            "CHPY",
            "intraday_trades",
            "https://yieldmaxetfs.com/our-etfs/chpy/",
            "https://yieldmaxetfs.com/ym/intraday-file",
            parse_chpy_trade_csv,
        ),
    ]
    results: list[dict] = []
    for fund, source_type, source_url, resolved_url, parser in jobs:
        try:
            result = _collect_source(fund, source_type, source_url, resolved_url, parser, raw_dir=Path(base_dir))
            result["status"] = result["status"] if result["status"] in {"new", "unchanged"} else "failed"
            results.append(result)
        except Exception as exc:  # pragma: no cover - runtime guard for collection
            results.append({
                "fund": fund,
                "source_type": source_type,
                "reporting_date": "unknown",
                "records_count": 0,
                "archive_path": "",
                "normalized_path": "",
                "status": "failed",
                "error": str(exc),
            })
    return results


def main() -> int:
    results = collect_all_sources(DATA_DIR)
    failed = [item for item in results if item.get("status") == "failed"]
    for item in results:
        report_date = item.get("reporting_date") or "unknown"
        print(f"{item.get('fund')} {item.get('source_type')}: {report_date} | records={item.get('records_count', 0)} | archive={item.get('archive_path') or '-'} | status={item.get('status')}")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
