from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


def _as_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_quantity(value: Any) -> float | None:
    if value is None:
        return None
    text = _flatten_text(value).replace(",", "").replace("$", "")
    if not text:
        return None
    if text.lower() in {"nan", "n/a", "na", "null", "none", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_weight(value: Any) -> float | None:
    text = _flatten_text(value)
    if not text:
        return None
    text = text.replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_option_descriptor(security_name: str) -> dict[str, Any]:
    text = _flatten_text(security_name)
    if not text:
        return {"option_underlying": None, "option_type": None, "option_strike": None, "option_expiration": None}

    match = re.search(r"(?i)([A-Z0-9.-]{2,10})\s+US\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+([A-Z])\s*(\d+(?:\.\d+)?)\b", text)
    if match:
        underlying, expiration, option_type, strike = match.groups()
        return {
            "option_underlying": underlying.upper(),
            "option_type": "call" if option_type.upper() == "C" else "put",
            "option_strike": float(strike),
            "option_expiration": _as_iso_date(expiration),
        }

    match = re.search(r"(?i)([A-Z0-9.-]{2,10})\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d+(?:\.\d+)?)\s*([CP])\b", text)
    if match:
        underlying, expiration, strike, option_type = match.groups()
        return {
            "option_underlying": underlying.upper(),
            "option_type": "call" if option_type.upper() == "C" else "put",
            "option_strike": float(strike),
            "option_expiration": _as_iso_date(expiration),
        }

    match = re.search(r"(?i)\b([A-Z]{2,8})\b.*?(\d{1,2}/\d{1,2}/\d{2,4}).*?(\d+(?:\.\d+)?)\s*([CP])\b", text)
    if match:
        underlying, expiration, strike, option_type = match.groups()
        return {
            "option_underlying": underlying.upper(),
            "option_type": "call" if option_type.upper() == "C" else "put",
            "option_strike": float(strike),
            "option_expiration": _as_iso_date(expiration),
        }

    return {"option_underlying": None, "option_type": None, "option_strike": None, "option_expiration": None}


def validate_response(status_code: int, payload: bytes | str, url: str) -> str:
    if status_code != 200:
        raise ValueError(f"HTTP {status_code} for {url}")
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
    if not text.strip():
        raise ValueError(f"Empty response for {url}")
    lowered = text.lower()
    blockers = [
        "maintenance",
        "site unavailable",
        "access denied",
        "forbidden",
        "not found",
        "error page",
        "unexpected error",
        "please try again later",
        "cloudflare",
    ]
    if any(token in lowered for token in blockers):
        raise ValueError(f"Error page or unavailable source for {url}")
    return text


def parse_usoy_html(html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#table-full-holdings")
    if table is None:
        raise ValueError("USOY holdings table not found in HTML")

    rows = table.select("tbody tr")
    if not rows:
        raise ValueError("USOY holdings table is empty")

    as_of_match = re.search(r"As\s+of\s+(\d{1,2}/\d{1,2}/\d{4})", html, flags=re.IGNORECASE)
    as_of = _as_iso_date(as_of_match.group(1)) if as_of_match else None

    records: list[dict[str, Any]] = []
    for source_row_number, row in enumerate(rows, start=2):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        ticker = _flatten_text(cells[0].get_text())
        security_name = _flatten_text(cells[1].get_text())
        cusip = _flatten_text(cells[2].get_text())
        weight = _parse_weight(cells[3].get_text())
        shares = _parse_quantity(cells[4].get_text())
        quantity = int(shares) if shares is not None and float(shares).is_integer() else shares
        option = _parse_option_descriptor(security_name)
        record = {
            "source_row_number": source_row_number,
            "fund": "USOY",
            "source_as_of_date": as_of,
            "security_name": security_name,
            "ticker": ticker,
            "CUSIP": cusip or ticker,
            "asset_type": "option" if option["option_underlying"] else "security",
            "quantity": quantity,
            "market_value": None,
            "portfolio_weight": weight,
            "option_underlying": option["option_underlying"],
            "option_type": option["option_type"],
            "option_strike": option["option_strike"],
            "option_expiration": option["option_expiration"],
            "raw_security_description": security_name,
            "source_url": source_url,
        }
        records.append(record)

    if not records:
        raise ValueError("No USOY holdings records parsed")

    return {"fund": "USOY", "source_as_of_date": as_of, "records": records, "source_url": source_url}


def parse_chpy_holdings_csv(csv_text: str) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CHPY holdings CSV has no header")
    required = {"Date", "Account", "StockTicker", "CUSIP", "SecurityName", "Shares", "Price", "MarketValue", "Weightings", "NetAssets"}
    missing = sorted(required - set(reader.fieldnames))
    if missing:
        raise ValueError(f"Missing CHPY CSV columns: {missing}")

    rows = list(reader)
    if not rows:
        raise ValueError("CHPY holdings CSV is empty")

    source_as_of = None
    records: list[dict[str, Any]] = []
    for source_row_number, row in enumerate(rows, start=2):
        if not row or not any((value or "").strip() for value in row.values()):
            continue
        date = row.get("Date")
        row_source_date = _as_iso_date(date)
        if source_as_of is None:
            source_as_of = row_source_date
        security_name = _flatten_text(row.get("SecurityName"))
        ticker = _flatten_text(row.get("StockTicker"))
        cusip = _flatten_text(row.get("CUSIP"))
        shares = _parse_quantity(row.get("Shares"))
        if shares is None:
            raise ValueError(f"Missing Shares value in CHPY row: {row}")
        weight = _parse_weight(row.get("Weightings"))
        market_value = _parse_quantity(row.get("MarketValue"))
        option = _parse_option_descriptor(security_name)
        asset_type = "option" if option["option_underlying"] else "security"
        record = {
            "source_row_number": source_row_number,
            "fund": _flatten_text(row.get("Account")) or "CHPY",
            "source_as_of_date": row_source_date,
            "security_name": security_name,
            "ticker": ticker,
            "CUSIP": cusip,
            "asset_type": asset_type,
            "quantity": int(shares) if shares.is_integer() else shares,
            "market_value": market_value,
            "portfolio_weight": weight,
            "option_underlying": option["option_underlying"],
            "option_type": option["option_type"],
            "option_strike": option["option_strike"],
            "option_expiration": option["option_expiration"],
            "raw_security_description": security_name,
        }
        records.append(record)

    if not records:
        raise ValueError("No CHPY holdings records parsed")

    return {"fund": "CHPY", "source_as_of_date": source_as_of, "records": records}


def _normalize_trade_side(value: Any) -> tuple[str | None, str | None]:
    text = _flatten_text(value).upper()
    if not text:
        return None, None

    aliases = {
        "B": "buy",
        "BUY": "buy",
        "BC": "buy",
        "BUY TO CLOSE": "buy",
        "S": "sell",
        "SELL": "sell",
        "SS": "sell",
        "SELL SHORT": "sell",
        "SHORT SALE": "sell",
        "SHORT": "sell",
    }
    normalized = aliases.get(text)
    if normalized is not None:
        return normalized, text
    lookup = text.replace("_", " ").strip()
    if lookup in {"BUY", "BUY TO CLOSE", "LONG"}:
        return "buy", text
    if lookup in {"SELL", "SELL SHORT", "SHORT SALE", "SHORT", "SELL TO OPEN"}:
        return "sell", text
    return lookup.lower() if lookup else None, text


def parse_chpy_trade_csv(csv_text: str) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CHPY trade CSV has no header")
    rows = list(reader)
    if not rows:
        raise ValueError("CHPY trade CSV is empty")

    fund = None
    records: list[dict[str, Any]] = []
    for source_row_number, row in enumerate(rows, start=2):
        if not row or not any((value or "").strip() for value in row.values()):
            continue
        trade_date = row.get("trade_date") or row.get("Date") or row.get("TradeDate")
        security = row.get("security") or row.get("Security") or row.get("Ticker") or row.get("StockTicker")
        raw_side_value = row.get("side") or row.get("Side") or row.get("Type") or row.get("type")
        normalized_side, raw_side = _normalize_trade_side(raw_side_value)
        if normalized_side is None:
            normalized_side = (row.get("side") or row.get("Side") or "").strip().lower()
        qty = _parse_quantity(row.get("quantity") or row.get("Quantity") or row.get("Qty/Par Value") or row.get("qty") or row.get("Qty"))
        price = _parse_quantity(row.get("price") or row.get("Price") or row.get("Exec Price") or row.get("exec_price"))
        if qty is None:
            raise ValueError(f"Missing quantity in CHPY trade: {row}")
        if normalized_side in {"sell", "short", "short sale", "sell short"} and qty > 0:
            qty *= -1
        row_fund = (row.get("fund") or row.get("Fund") or row.get("Account") or fund or "CHPY").strip().upper()
        if fund is None:
            fund = row_fund
        records.append({
            "source_row_number": source_row_number,
            "trade_date": _as_iso_date(trade_date),
            "fund": row_fund,
            "security": _flatten_text(security),
            "side": normalized_side or None,
            "raw_type": raw_side,
            "quantity": int(qty) if float(qty).is_integer() else qty,
            "price": price,
            "raw": row,
        })

    return {"fund": fund or "CHPY", "records": records}


def fetch_url(url: str, timeout: int = 30, retries: int = 3) -> requests.Response:
    last_error: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            response = requests.get(url, timeout=timeout, headers={"User-Agent": "income-fund-lab/0.1"}, allow_redirects=True)
            if response.status_code == 200:
                return response
            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = requests.HTTPError(f"Transient HTTP {response.status_code} for {url}")
                continue
            raise requests.HTTPError(f"HTTP {response.status_code} for {url}")
        except requests.RequestException as exc:  # pragma: no cover - defensive branch
            last_error = exc
    if last_error is not None:
        raise RuntimeError(f"Unable to fetch {url}: {last_error}")
    raise RuntimeError(f"Unable to fetch {url}")


def _hash_payload(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def archive_snapshot(base_dir: str | Path, fund: str, source_type: str, payload: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
    path = Path(base_dir)
    fund_key = fund.upper()
    source_date = metadata.get("source_as_of_date") or metadata.get("retrieval_timestamp_utc", "unknown")[:10]
    sha = metadata.get("sha256") or _hash_payload(payload)
    raw_root = path / "raw" / fund_key.lower() / str(source_date)
    candidate = raw_root / sha[:12]
    if candidate.exists() and (candidate / "source.bin").exists():
        return {
            "status": "unchanged",
            "archive_path": str(candidate / "source.bin"),
            "sha256": sha,
            "fund": fund_key,
            "source_type": source_type,
        }
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "source.bin").write_bytes(payload)
    meta = {**metadata, "sha256": sha, "fund": fund_key, "source_type": source_type}
    (candidate / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "status": "new",
        "archive_path": str(candidate / "source.bin"),
        "sha256": sha,
        "fund": fund_key,
        "source_type": source_type,
    }


def write_normalized_snapshot(base_dir: str | Path, fund: str, source_type: str, records: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    path = Path(base_dir)
    fund_key = fund.upper()
    source_date = metadata.get("source_as_of_date") or metadata.get("retrieval_timestamp_utc", "unknown")[:10]
    sha = metadata.get("sha256")
    normalized_root = path / "normalized" / fund_key.lower() / str(source_date)
    # Deterministic ordering helps repeatable output.
    ordered_records = [json.loads(json.dumps(item, sort_keys=True, default=str)) for item in records]
    if sha is None:
        sha = hashlib.sha256(json.dumps(ordered_records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    target = normalized_root / f"{sha[:12]}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"fund": fund_key, "source_type": source_type, "metadata": metadata, "records": ordered_records}, sort_keys=True, indent=2)
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    if existing == payload:
        return {"status": "unchanged", "archive_path": str(target), "sha256": sha}
    target.write_text(payload, encoding="utf-8")
    return {"status": "new", "archive_path": str(target), "sha256": sha}


__all__ = [
    "validate_response",
    "parse_usoy_html",
    "parse_chpy_holdings_csv",
    "parse_chpy_trade_csv",
    "fetch_url",
    "archive_snapshot",
    "write_normalized_snapshot",
]
