
# market_data_engine.py
# NSE F&O PRO V8.2
# PRIMARY LIVE FEED + CROSS VERIFICATION
# AUTOMATIC RETRY + DATA QUALITY
# Research only - No automatic orders

import json
import math
import gzip
import re
import time
import requests
import pandas as pd
import streamlit as st

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
VERSION = "8.2"

NSE_CM_URL = "https://mcp.nseindia.in/cmmkt/mcp"
NSE_BHAVCOPY_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"
UPSTOX_URL = "https://api.upstox.com"

MASTER_URL = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)


# ==========================================
# BASIC UTILITIES
# ==========================================

def now_ist():
    return datetime.now(IST)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_time(value):
    try:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, str):
            value = value.strip()

            if not value:
                return None

            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                value = float(value) if "." in value else int(value)

        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None

            magnitude = abs(value)

            if magnitude >= 1e17:
                unit = "ns"
            elif magnitude >= 1e14:
                unit = "us"
            elif magnitude >= 1e11:
                unit = "ms"
            else:
                unit = "s"

            ts = pd.to_datetime(
                value,
                unit=unit,
                utc=True,
                errors="coerce"
            )
        else:
            ts = pd.Timestamp(value)

            if pd.isna(ts):
                return None

            if ts.tzinfo is None:
                ts = ts.tz_localize(IST)

        if pd.isna(ts):
            return None

        return ts.tz_convert(IST)

    except Exception:
        return None


def data_age(value, reference=None):
    ts = parse_time(value)

    if ts is None:
        return None

    reference = reference or now_ist()
    return float((reference - ts).total_seconds())


def is_fresh(value, max_seconds=180, reference=None):
    reference = reference or now_ist()
    ts = parse_time(value)

    if ts is None:
        return False

    age = data_age(value, reference)

    return (
        ts.date() == reference.date()
        and age is not None
        and -5 <= age <= max_seconds
    )


def format_time(value):
    ts = parse_time(value)
    return ts.isoformat() if ts is not None else None


def rounded(value, digits=3):
    return round(value, digits) if value is not None else None


def timestamp_self_test():
    expected = pd.Timestamp(
        "2026-11-25 14:51:04.449",
        tz=IST
    )

    cases = [
        1795598464449,
        "1795598464449",
        "1795598464.449",
        1795598464449000,
        1795598464449000000,
        "2026-11-25T09:21:04.449Z",
        "2026-11-25 14:51:04.449"
    ]

    results = []

    for value in cases:
        parsed = parse_time(value)

        passed = (
            parsed is not None
            and abs(
                (parsed - expected).total_seconds()
            ) < 0.001
        )

        results.append({
            "Input": str(value),
            "Converted IST": format_time(value),
            "Status": "PASS" if passed else "FAIL"
        })

    for value in [None, "", "invalid", True]:
        results.append({
            "Input": str(value),
            "Converted IST": format_time(value),
            "Status": (
                "PASS"
                if parse_time(value) is None
                else "FAIL"
            )
        })

    return pd.DataFrame(results)


# ==========================================
# NSE MCP CLIENT
# ==========================================

class NSEMCPClient:

    def __init__(self, url):
        self.url = url
        self.session = requests.Session()
        self.request_id = 0
        self.initialized = False

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }

    def close(self):
        self.session.close()

    def request(
        self,
        method,
        params=None,
        notification=False
    ):
        self.request_id += 1

        payload = {
            "jsonrpc": "2.0",
            "method": method
        }

        if not notification:
            payload["id"] = self.request_id

        if params is not None:
            payload["params"] = params

        with self.session.post(
            self.url,
            json=payload,
            headers=self.headers,
            timeout=(10, 40)
        ) as response:

            response.raise_for_status()

            session_id = response.headers.get(
                "Mcp-Session-Id"
            )

            if session_id:
                self.headers["Mcp-Session-Id"] = session_id

            if notification:
                return {}

            content_type = response.headers.get(
                "Content-Type", ""
            )

            if "text/event-stream" in content_type:
                result = None
                event_data = []

                for line in (
                    response.text + "\n\n"
                ).splitlines():

                    if line.startswith("data:"):
                        event_data.append(line[5:].strip())

                    elif not line and event_data:
                        try:
                            item = json.loads(
                                "\n".join(event_data)
                            )

                            if item.get("id") == self.request_id:
                                result = item

                        except (ValueError, TypeError):
                            pass

                        event_data = []

                if result is None:
                    raise RuntimeError(
                        "No matching MCP response"
                    )

            else:
                result = response.json()

        if "error" in result:
            raise RuntimeError(str(result["error"]))

        return result.get("result", {})

    def initialize(self):
        if self.initialized:
            return

        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "NSE-FO-PRO-V8",
                    "version": VERSION
                }
            }
        )

        self.headers["MCP-Protocol-Version"] = result.get(
            "protocolVersion",
            "2025-03-26"
        )

        self.request(
            "notifications/initialized",
            notification=True
        )

        self.initialized = True

    def list_tools(self):
        self.initialize()

        tools = []
        params = {}
        seen = set()

        while True:
            result = self.request("tools/list", params)
            tools.extend(result.get("tools", []))

            cursor = result.get("nextCursor")

            if not cursor:
                break

            if cursor in seen:
                raise RuntimeError("Repeated MCP cursor")

            seen.add(cursor)
            params = {"cursor": cursor}

        return tools

    def call(self, name, arguments=None):
        self.initialize()

        result = self.request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments or {}
            }
        )

        if result.get("isError"):
            raise RuntimeError(str(result))

        data = result.get("structuredContent")

        if data is None:
            for item in result.get("content", []):
                if item.get("type") != "text":
                    continue

                try:
                    data = json.loads(item.get("text", ""))
                    break
                except (ValueError, TypeError):
                    continue

        if data is None:
            raise RuntimeError("No valid JSON data from NSE")

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))

        return data


# ==========================================
# NSE MARKET DATA ENGINE
# ==========================================

class NSEMarketData:

    def __init__(self):
        self.cm = NSEMCPClient(NSE_CM_URL)
        self.bhavcopy = NSEMCPClient(NSE_BHAVCOPY_URL)

    def close(self):
        self.cm.close()
        self.bhavcopy.close()

    def connectivity(self):
        result = {}

        for name, client in [
            ("CM Market", self.cm),
            ("Bhavcopy", self.bhavcopy)
        ]:
            try:
                tools = client.list_tools()

                result[name] = {
                    "Status": "CONNECTED",
                    "Tools": len(tools)
                }

            except Exception as e:
                result[name] = {
                    "Status": "FAILED",
                    "Error": str(e)
                }

        return result

    def market_status(self):
        return self.cm.call("cm_get_data_status", {})

    def equity_status(self):
        return self.cm.call("cm_get_allstocks_status", {})

    def stock_quote(self, symbol):
        return self.cm.call(
            "cm_get_stock_quote",
            {"symbol": symbol.upper()}
        )

    def equity_stocks(self, prefix="", limit=500):
        return self.cm.call(
            "cm_get_equity_stocks",
            {
                "limit": min(max(int(limit), 1), 500),
                "symbolFilter": prefix.upper()
            }
        )

    def index_quote(self, name):
        return self.cm.call(
            "cm_get_index_quote",
            {"indexName": name}
        )

    def live_indices(self, group="all"):
        return self.cm.call(
            "cm_get_live_indices",
            {
                "group": group,
                "nameFilter": ""
            }
        )

    def sector_strength(self):
        return self.live_indices("sectoral")

    def india_vix(self):
        return self.index_quote("INDIA VIX")

    def market_movers(self, limit=50):
        return self.cm.call(
            "nse_get_market_movers",
            {
                "indexName": "",
                "limit": min(max(int(limit), 1), 50)
            }
        )

    def market_breadth(self, date):
        return self.bhavcopy.call(
            "get_market_breadth",
            {"date": str(date)}
        )

    def market_mood(self, date):
        return self.bhavcopy.call(
            "get_market_mood",
            {"date": str(date)}
        )

    def historical_movers(self, date, direction="gain"):
        return self.bhavcopy.call(
            "get_top_movers",
            {
                "date": str(date),
                "n": 50,
                "direction": direction
            }
        )

    def historical_turnover(self, date):
        return self.bhavcopy.call(
            "get_top_by_volume",
            {
                "date": str(date),
                "n": 50,
                "sortBy": "value"
            }
        )

    def corporate_actions(self, symbol, start, end):
        return self.bhavcopy.call(
            "get_corporate_actions",
            {
                "symbol": symbol.upper(),
                "fromDate": str(start),
                "toDate": str(end)
            }
        )

    def stock_history(self, symbol, months=3, end_date=None):
        end = (
            pd.Timestamp(end_date).date()
            if end_date
            else now_ist().date()
        )

        months = min(max(int(months), 1), 3)

        start = (
            pd.Timestamp(end)
            - pd.DateOffset(months=months)
        ).date()

        actions = self.corporate_actions(
            symbol, start, end
        )

        history = self.bhavcopy.call(
            "get_stock_history",
            {
                "symbol": symbol.upper(),
                "months": months,
                "endDate": str(end)
            }
        )

        return {
            "History": history,
            "Corporate Actions": actions,
            "Adjusted Prices": False,
            "Warning": (
                "Raw bhavcopy prices. Review corporate "
                "actions before calculating returns."
            )
        }

    def volume_analysis(self, symbol, days=30):
        return self.bhavcopy.call(
            "get_volume_analysis",
            {
                "symbol": symbol.upper(),
                "days": min(max(int(days), 5), 252)
            }
        )


# ==========================================
# UPSTOX PRIMARY LIVE DATA ENGINE
# ==========================================

@st.cache_data(ttl=21600, show_spinner=False)
def get_fo_universe():
    response = requests.get(MASTER_URL, timeout=40)
    response.raise_for_status()

    instruments = json.loads(
        gzip.decompress(response.content)
    )

    underlying_keys = {
        item.get("underlying_key")
        for item in instruments
        if item.get("segment") == "NSE_FO"
        and item.get("instrument_type") in ("CE", "PE")
        and item.get("underlying_key")
    }

    universe = {}

    for item in instruments:
        if (
            item.get("segment") == "NSE_EQ"
            and item.get("instrument_key") in underlying_keys
        ):
            symbol = item.get("trading_symbol")

            if symbol:
                universe[symbol] = item["instrument_key"]

    if not universe:
        raise RuntimeError("F&O universe is empty")

    return universe


def upstox_api(token, path, params=None):
    if not token:
        raise RuntimeError("Upstox access token required")

    response = requests.get(
        UPSTOX_URL + path,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        },
        timeout=20
    )

    if not response.ok:
        raise RuntimeError(
            f"Upstox HTTP {response.status_code}"
        )

    result = response.json()

    if result.get("status") != "success":
        raise RuntimeError("Upstox API request failed")

    return result.get("data", {})


def upstox_quotes(token, keys):
    keys = list(dict.fromkeys(keys))
    quotes = {}

    for start in range(0, len(keys), 40):
        batch = keys[start:start + 40]

        data = upstox_api(
            token,
            "/v2/market-quote/quotes",
            {"instrument_key": ",".join(batch)}
        )

        for item in data.values():
            key = item.get("instrument_token")

            if key:
                quotes[key] = item

    return quotes


def upstox_market_status(token):
    return upstox_api(
        token,
        "/v2/market/status/NSE"
    )


# ==========================================
# NSE DATA VALIDATION
# ==========================================

def validate_nse_stock(
    stock,
    max_age=180,
    reference=None
):
    reference = reference or now_ist()
    stock = stock or {}

    errors = []
    warnings = []

    symbol = stock.get("symbol", "")
    ltp = number(stock.get("lastTradedPrice"))
    previous = number(stock.get("preClosePrice"))
    op = number(stock.get("openPrice"))
    high = number(stock.get("highPrice"))
    low = number(stock.get("lowPrice"))
    volume = number(stock.get("volume"))
    timestamp = stock.get("latestTimestamp")

    if not symbol:
        errors.append("MISSING SYMBOL")

    if stock.get("series") != "EQ":
        errors.append("NON EQ SERIES")

    prices = [ltp, previous, op, high, low]

    if any(p is None or p <= 0 for p in prices):
        errors.append("INVALID OHLC PRICE")
    else:
        if high < low:
            errors.append("INVALID RANGE")

        if not low <= ltp <= high:
            errors.append("LTP OUTSIDE DAY RANGE")

        if not low <= op <= high:
            errors.append("OPEN OUTSIDE DAY RANGE")

    if volume is None or volume <= 0:
        errors.append("INVALID VOLUME")

    age = data_age(timestamp, reference)

    if age is None:
        errors.append("INVALID NSE TIMESTAMP")
    elif age < -5:
        errors.append("NSE TIMESTAMP IN FUTURE")
    elif not is_fresh(timestamp, max_age, reference):
        errors.append("STALE NSE DATA")

    change = None
    change_pct = None

    if ltp is not None and previous is not None and previous > 0:
        change = ltp - previous
        change_pct = change / previous * 100

        reported = number(stock.get("change"))
        reported_pct = number(stock.get("perChange"))

        if reported is not None and abs(change - reported) > 0.11:
            warnings.append("PRICE CHANGE RECALCULATED")

        if (
            reported_pct is not None
            and abs(change_pct - reported_pct) > 0.10
        ):
            warnings.append("PERCENT CHANGE RECALCULATED")

    position = None

    if (
        high is not None
        and low is not None
        and ltp is not None
        and high > low
    ):
        position = (ltp - low) / (high - low) * 100

    return {
        "Symbol": symbol,
        "NSE LTP": ltp,
        "Previous Close": previous,
        "Open": op,
        "High": high,
        "Low": low,
        "Volume": volume,
        "Change": change,
        "Change %": change_pct,
        "Day Range Position": position,
        "NSE Timestamp": timestamp,
        "NSE Timestamp IST": format_time(timestamp),
        "NSE Age Seconds": rounded(age),
        "NSE Valid": not errors,
        "Errors": " | ".join(errors),
        "Warnings": " | ".join(warnings)
    }


# ==========================================
# V8.2 CROSS VERIFICATION
# ==========================================

def cross_verify(
    nse_stock,
    upstox_stock,
    tolerance_pct=0.35,
    max_age=180,
    max_time_gap=90,
    reference=None,
    upstox_max_age=15
):
    reference = reference or now_ist()

    result = validate_nse_stock(
        nse_stock, max_age, reference
    )

    upstox_stock = upstox_stock or {}

    reasons = []

    if not result["NSE Valid"]:
        reasons.extend(result["Errors"].split(" | "))

    upstox_ltp = number(upstox_stock.get("last_price"))
    nse_ltp = result["NSE LTP"]

    trade_timestamp = upstox_stock.get("last_trade_time")
    quote_timestamp = upstox_stock.get("timestamp")

    nse_time = parse_time(result["NSE Timestamp"])
    trade_time = parse_time(trade_timestamp)
    quote_time = parse_time(quote_timestamp)

    trade_age = data_age(trade_timestamp, reference)
    quote_age = data_age(quote_timestamp, reference)

    if upstox_ltp is None or upstox_ltp <= 0:
        reasons.append("UPSTOX PRICE UNAVAILABLE")

    if trade_time is None:
        reasons.append("LAST TRADE TIME UNAVAILABLE")
    elif not is_fresh(
        trade_timestamp, upstox_max_age, reference
    ):
        reasons.append("STALE OR INVALID UPSTOX TRADE")

    if quote_time is None:
        reasons.append("UPSTOX QUOTE TIMESTAMP UNAVAILABLE")
    elif not is_fresh(
        quote_timestamp, upstox_max_age, reference
    ):
        reasons.append("STALE OR INVALID UPSTOX QUOTE")

    if (
        trade_time is not None
        and quote_time is not None
        and trade_time > quote_time
    ):
        reasons.append("INVALID UPSTOX TIMESTAMP ORDER")

    time_gap = None

    if nse_time is not None and trade_time is not None:
        time_gap = abs(
            (nse_time - trade_time).total_seconds()
        )

        if time_gap > max_time_gap:
            reasons.append("SOURCE TIMESTAMP MISMATCH")
    else:
        reasons.append("TIMESTAMP UNVERIFIED")

    difference = None

    if (
        nse_ltp is not None
        and nse_ltp > 0
        and upstox_ltp is not None
        and upstox_ltp > 0
    ):
        difference = abs(nse_ltp - upstox_ltp) / nse_ltp * 100

        if difference > tolerance_pct:
            reasons.append("SOURCE PRICE MISMATCH")
    else:
        reasons.append("PRICE COMPARISON UNAVAILABLE")

    # Upstox primary price and OHLC data

    ohlc = upstox_stock.get("ohlc") or {}

    primary_open = number(ohlc.get("open"))
    primary_high = number(ohlc.get("high"))
    primary_low = number(ohlc.get("low"))
    primary_previous = number(ohlc.get("close"))
    primary_volume = number(upstox_stock.get("volume"))

    primary_change = None
    primary_position = None

    if (
        primary_previous is not None
        and primary_previous > 0
        and upstox_ltp is not None
    ):
        primary_change = (
            upstox_ltp / primary_previous - 1
        ) * 100

    if (
        primary_high is not None
        and primary_low is not None
        and primary_high > primary_low
        and upstox_ltp is not None
        and primary_low <= upstox_ltp <= primary_high
    ):
        primary_position = (
            (upstox_ltp - primary_low)
            / (primary_high - primary_low)
        ) * 100

    primary_prices = [
        upstox_ltp,
        primary_open,
        primary_high,
        primary_low,
        primary_previous
    ]

    primary_valid = all(
        p is not None and p > 0
        for p in primary_prices
    )

    if primary_valid:
        primary_valid = (
            primary_low <= upstox_ltp <= primary_high
            and primary_low <= primary_open <= primary_high
            and primary_volume is not None
            and primary_volume > 0
        )

    reasons = list(dict.fromkeys(reasons))

    result.update({
        "Primary Source": "UPSTOX",
        "Independent Source": "NSE MCP",

        "Upstox LTP": upstox_ltp,
        "Upstox Open": primary_open,
        "Upstox High": primary_high,
        "Upstox Low": primary_low,
        "Upstox Previous Close": primary_previous,
        "Upstox Volume": primary_volume,
        "Upstox Change %": primary_change,
        "Upstox Day Range Position": primary_position,
        "Upstox OHLC Valid": primary_valid,

        "Upstox Timestamp": trade_timestamp,
        "Upstox Timestamp Source": "last_trade_time",
        "Upstox Timestamp IST": format_time(trade_timestamp),
        "Upstox Quote Timestamp": quote_timestamp,
        "Upstox Quote Timestamp IST": format_time(quote_timestamp),

        "Upstox Age Seconds": rounded(trade_age),
        "Upstox Quote Age Seconds": rounded(quote_age),

        "Source Time Gap Seconds": rounded(time_gap),
        "Price Difference %": rounded(difference, 6),

        "Maximum Allowed Age Seconds": max_age,
        "Maximum Allowed Upstox Age Seconds": upstox_max_age,
        "Maximum Allowed Gap Seconds": max_time_gap,
        "Maximum Allowed Price Difference %": tolerance_pct,

        "Verification Time IST": reference.isoformat(),

        "Cross Verified": not reasons,
        "Verification Status": (
            "VERIFIED" if not reasons else "BLOCKED"
        ),
        "Verification Reasons": " | ".join(reasons)
    })

    return result


# ==========================================
# CONTROLLED PARALLEL NSE FETCHING
# ==========================================

def fetch_nse_group(symbols):
    client = NSEMarketData()

    records = {}
    errors = {}

    try:
        for symbol in symbols:
            try:
                response = client.stock_quote(symbol)
                stock = response.get("stock", {})

                if stock.get("symbol") != symbol:
                    raise RuntimeError("NSE symbol mismatch")

                records[symbol] = stock

            except Exception as e:
                errors[symbol] = str(e)

                if "429" in str(e):
                    time.sleep(2)

            # Small delay to avoid uncontrolled requests.
            time.sleep(0.10)

    finally:
        client.close()

    return records, errors


def fetch_nse_parallel(symbols, workers=2):
    workers = max(1, min(int(workers), 3, len(symbols)))

    groups = [
        symbols[i::workers]
        for i in range(workers)
    ]

    records = {}
    errors = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(fetch_nse_group, group)
            for group in groups
            if group
        ]

        for future in futures:
            group_records, group_errors = future.result()
            records.update(group_records)
            errors.update(group_errors)

    return records, errors


# ==========================================
# BATCH VERIFICATION
# ==========================================

def verify_batch(
    token,
    symbols,
    universe,
    tolerance_pct,
    max_age,
    max_time_gap,
    upstox_max_age,
    workers,
    attempt
):
    started = now_ist()

    # Independent NSE data is fetched first.
    # Upstox is fetched immediately afterward
    # to keep the primary quote as fresh as possible.

    nse_records, nse_errors = fetch_nse_parallel(
        symbols, workers
    )

    quote_error = ""

    try:
        quotes = upstox_quotes(
            token,
            [universe[s] for s in symbols]
        )
    except Exception as e:
        quotes = {}
        quote_error = str(e)

    reference = now_ist()
    rows = []

    for symbol in symbols:
        result = cross_verify(
            nse_records.get(symbol, {}),
            quotes.get(universe[symbol], {}),
            tolerance_pct,
            max_age,
            max_time_gap,
            reference,
            upstox_max_age
        )

        result["Symbol"] = symbol
        result["Instrument Key"] = universe[symbol]
        result["Attempt Number"] = attempt
        result["Batch Started IST"] = started.isoformat()
        result["Batch Duration Seconds"] = rounded(
            (reference - started).total_seconds()
        )

        result["NSE Request Error"] = nse_errors.get(
            symbol, ""
        )

        result["Upstox Request Error"] = quote_error

        extra = []

        if symbol in nse_errors:
            extra.append("NSE REQUEST FAILED")

        if quote_error:
            extra.append("UPSTOX REQUEST FAILED")

        if extra:
            existing = result["Verification Reasons"]

            result["Verification Reasons"] = " | ".join(
                ([existing] if existing else []) + extra
            )

            result["Cross Verified"] = False
            result["Verification Status"] = "BLOCKED"

        rows.append(result)

    return rows


# ==========================================
# CURRENT FRESHNESS CHECK
# ==========================================

def row_is_currently_fresh(row, reference=None):
    reference = reference or now_ist()

    if row.get("Cross Verified") != True:
        return False

    return (
        is_fresh(
            row.get("NSE Timestamp"),
            row.get("Maximum Allowed Age Seconds", 180),
            reference
        )
        and is_fresh(
            row.get("Upstox Timestamp"),
            row.get("Maximum Allowed Upstox Age Seconds", 15),
            reference
        )
        and is_fresh(
            row.get("Upstox Quote Timestamp"),
            row.get("Maximum Allowed Upstox Age Seconds", 15),
            reference
        )
    )


# ==========================================
# V8.2 COMPLETE MARKET SCANNER
# ==========================================

def scan_fo_market(
    token,
    symbols=None,
    tolerance_pct=0.35,
    max_age=180,
    max_time_gap=90,
    upstox_max_age=15,
    workers=2,
    batch_size=10,
    max_retries=2,
    retry_delay=20
):
    universe = get_fo_universe()

    selected = (
        sorted(universe)
        if symbols is None
        else sorted({
            s.upper()
            for s in symbols
            if s.upper() in universe
        })
    )

    if not selected:
        raise RuntimeError("No valid F&O symbols selected")

    started = now_ist()

    batch_size = max(1, min(int(batch_size), 20))
    max_retries = max(0, min(int(max_retries), 3))

    final_records = {}
    initial_records = {}
    attempt_log = []
    retry_summary = []

    progress = st.progress(0)
    status = st.empty()

    def run_pass(targets, attempt):
        total = len(targets)

        for start in range(0, total, batch_size):
            batch = targets[start:start + batch_size]

            status.info(
                f"Pass {attempt} | "
                f"Scanning {start + 1}-{start + len(batch)} "
                f"of {total} stocks"
            )

            rows = verify_batch(
                token,
                batch,
                universe,
                tolerance_pct,
                max_age,
                max_time_gap,
                upstox_max_age,
                workers,
                attempt
            )

            for row in rows:
                symbol = row["Symbol"]

                if attempt == 1:
                    initial_records[symbol] = row.copy()

                final_records[symbol] = row.copy()
                attempt_log.append(row.copy())

            progress.progress(
                min((start + len(batch)) / total, 1.0)
            )

    try:
        run_pass(selected, 1)

        initial_verified = sum(
            r["Cross Verified"]
            for r in initial_records.values()
        )

        initial_blocked = len(selected) - initial_verified

        for retry in range(1, max_retries + 1):
            blocked = [
                symbol
                for symbol, row in final_records.items()
                if not row["Cross Verified"]
            ]

            if not blocked:
                break

            # Authentication errors require a new token.
            # Do not repeatedly retry an invalid token.

            eligible = [
                symbol for symbol in blocked
                if not any(
                    code in final_records[symbol].get(
                        "Upstox Request Error", ""
                    )
                    for code in ["HTTP 401", "HTTP 403"]
                )
            ]

            if not eligible:
                break

            status.warning(
                f"Retry {retry}: waiting {retry_delay} seconds "
                f"before refreshing {len(eligible)} blocked stocks."
            )

            time.sleep(max(0, retry_delay))
            progress.progress(0)

            run_pass(eligible, retry + 1)

            recovered = sum(
                final_records[s]["Cross Verified"]
                for s in eligible
            )

            retry_summary.append({
                "Retry": retry,
                "Attempted": len(eligible),
                "Recovered": recovered,
                "Still Blocked": len(eligible) - recovered
            })

    finally:
        progress.empty()
        status.empty()

    reference = now_ist()
    rows = []

    for symbol in selected:
        row = final_records[symbol].copy()
        first = initial_records[symbol]

        row["Initial Verification Status"] = first[
            "Verification Status"
        ]

        row["Initial Verification Reasons"] = first[
            "Verification Reasons"
        ]

        row["Initial NSE Timestamp IST"] = first.get(
            "NSE Timestamp IST"
        )

        row["Recovered After Retry"] = (
            not first["Cross Verified"]
            and row["Cross Verified"]
        )

        row["Currently Fresh At Scan End"] = (
            row_is_currently_fresh(row, reference)
        )

        rows.append(row)

    frame = pd.DataFrame(rows)

    verified = int(frame["Cross Verified"].sum())
    recovered = int(frame["Recovered After Retry"].sum())

    fresh = int(
        frame["Currently Fresh At Scan End"].sum()
    )

    reason_counts = Counter()

    for row in rows:
        if not row["Cross Verified"]:
            reason_counts.update(
                reason.strip()
                for reason in row["Verification Reasons"].split("|")
                if reason.strip()
            )

    coverage = {
        "Engine Version": VERSION,
        "Total F&O Universe": len(universe),
        "Selected": len(selected),
        "Attempted": len(frame),

        "Initial Verified": initial_verified,
        "Initial Blocked": initial_blocked,

        "Initial Verification Rate %": round(
            100 * initial_verified / len(selected), 2
        ),

        "Recovered After Retry": recovered,
        "Verified": verified,
        "Blocked": len(frame) - verified,

        "Currently Fresh At Scan End": fresh,

        "Attempted Coverage %": round(
            100 * len(frame) / len(universe), 2
        ),

        "Verified Coverage %": round(
            100 * verified / len(universe), 2
        ),

        "Selected Verification Rate %": round(
            100 * verified / len(selected), 2
        ),

        "Total Verification Attempts": len(attempt_log),
        "Retry Summary": retry_summary,
        "Blocked Reason Counts": dict(reason_counts),

        "Scan Started IST": started.isoformat(),
        "Scan Time": reference.isoformat(),

        "Scan Duration Seconds": rounded(
            (reference - started).total_seconds()
        ),

        "Settings": {
            "Price Difference %": tolerance_pct,
            "NSE Maximum Age": max_age,
            "Upstox Maximum Age": upstox_max_age,
            "Maximum Source Gap": max_time_gap,
            "Parallel Workers": workers,
            "Batch Size": batch_size,
            "Maximum Retries": max_retries,
            "Retry Delay Seconds": retry_delay
        }
    }

    st.session_state["v82_attempt_log"] = pd.DataFrame(
        attempt_log
    )

    return frame, coverage


# ==========================================
# PRIMARY UPSTOX MOMENTUM RANKING
# ==========================================

def rank_verified_stocks(frame):
    if frame.empty:
        return frame.copy()

    reference = now_ist()

    data = frame[
        frame.apply(
            lambda row: row_is_currently_fresh(
                row, reference
            ),
            axis=1
        )
    ].copy()

    if data.empty:
        return data

    data = data[
        data["Upstox OHLC Valid"] == True
    ].copy()

    if data.empty:
        return data

    def classify(row):
        change = number(row["Upstox Change %"])
        position = number(row["Upstox Day Range Position"])
        ltp = number(row["Upstox LTP"])
        op = number(row["Upstox Open"])

        if any(
            value is None
            for value in [change, position, ltp, op]
        ):
            return "NEUTRAL"

        if change >= 0.35 and position >= 60 and ltp > op:
            return "CE WATCH"

        if change <= -0.35 and position <= 40 and ltp < op:
            return "PE WATCH"

        return "NEUTRAL"

    data["Direction"] = data.apply(classify, axis=1)

    def score(row):
        if row["Direction"] == "NEUTRAL":
            return 0.0

        strength = min(
            abs(row["Upstox Change %"]) / 2.5,
            1.0
        )

        position = row["Upstox Day Range Position"]

        if row["Direction"] == "PE WATCH":
            position = 100 - position

        return round(
            strength * 60 + position * 0.40,
            2
        )

    data["Momentum Score"] = data.apply(score, axis=1)

    return data.sort_values(
        "Momentum Score",
        ascending=False
    )


# ==========================================
# BLOCKED REASON ANALYSIS
# ==========================================

def blocked_reason_analysis(frame):
    counts = Counter()

    for _, row in frame.iterrows():
        if row.get("Cross Verified") == True:
            continue

        reasons = str(
            row.get("Verification Reasons", "")
        )

        counts.update({
            reason.strip()
            for reason in reasons.split("|")
            if reason.strip()
        })

    return pd.DataFrame([
        {
            "Blocking Reason": reason,
            "Affected Stocks": count
        }
        for reason, count in counts.most_common()
    ])


# ==========================================
# OFFLINE VALIDATION SELF TEST
# ==========================================

def verification_self_test():
    reference = pd.Timestamp(
        "2026-11-26 10:00:00",
        tz=IST
    )

    stock = {
        "symbol": "TEST",
        "series": "EQ",
        "lastTradedPrice": 100,
        "preClosePrice": 99,
        "openPrice": 99,
        "highPrice": 101,
        "lowPrice": 98,
        "volume": 10000,
        "latestTimestamp": (
            reference - timedelta(seconds=20)
        ).isoformat()
    }

    quote = {
        "last_price": 100.05,
        "last_trade_time": (
            reference - timedelta(seconds=2)
        ).isoformat(),
        "timestamp": (
            reference - timedelta(seconds=1)
        ).isoformat(),
        "ohlc": {
            "open": 99,
            "high": 101,
            "low": 98,
            "close": 99
        },
        "volume": 10100
    }

    cases = []

    cases.append(("Valid Quote", stock, quote, True))

    stale_nse = dict(stock)
    stale_nse["latestTimestamp"] = (
        reference - timedelta(seconds=200)
    ).isoformat()

    cases.append(
        ("Stale NSE", stale_nse, quote, False)
    )

    gap_nse = dict(stock)
    gap_nse["latestTimestamp"] = (
        reference - timedelta(seconds=120)
    ).isoformat()

    cases.append(
        ("Source Gap", gap_nse, quote, False)
    )

    stale_trade = dict(quote)
    stale_trade["last_trade_time"] = (
        reference - timedelta(seconds=30)
    ).isoformat()

    cases.append(
        ("Stale Upstox Trade", stock, stale_trade, False)
    )

    stale_quote = dict(quote)
    stale_quote["timestamp"] = (
        reference - timedelta(seconds=30)
    ).isoformat()

    cases.append(
        ("Stale Upstox Quote", stock, stale_quote, False)
    )

    mismatch = dict(quote)
    mismatch["last_price"] = 101

    cases.append(
        ("Price Mismatch", stock, mismatch, False)
    )

    missing_trade = dict(quote)
    missing_trade.pop("last_trade_time")

    cases.append(
        ("Missing Trade Time", stock, missing_trade, False)
    )

    results = []

    for name, nse, upstox, expected in cases:
        result = cross_verify(
            nse,
            upstox,
            reference=reference
        )

        actual = result["Cross Verified"]

        results.append({
            "Test": name,
            "Expected Verified": expected,
            "Actual Verified": actual,
            "Status": "PASS" if actual == expected else "FAIL",
            "Reasons": result["Verification Reasons"]
        })

    return pd.DataFrame(results)


# ==========================================
# STREAMLIT DASHBOARD
# ==========================================

def render_market_data_engine():

    st.title("NSE F&O PRO V8.2 — DATA ENGINE")

    st.caption(
        "Upstox Primary | NSE Independent Verification | "
        "Parallel Fetch | Automatic Retry | Quality Audit"
    )

    if st.session_state.get("v8_engine_version") != VERSION:
        for key in [
            "v8_single",
            "v8_scan",
            "v8_coverage",
            "v82_attempt_log",
            "v82_self_tests"
        ]:
            st.session_state.pop(key, None)

        st.session_state["v8_engine_version"] = VERSION

    token = st.sidebar.text_input(
        "Upstox Access Token",
        type="password"
    ).strip()

    st.sidebar.subheader("Data Quality Settings")

    tolerance = st.sidebar.slider(
        "Maximum Price Difference %",
        0.10, 1.00, 0.35, 0.05
    )

    max_age = st.sidebar.slider(
        "Maximum NSE Data Age Seconds",
        60, 300, 180, 30
    )

    max_time_gap = st.sidebar.slider(
        "Maximum Source Time Gap Seconds",
        30, 180, 90, 15
    )

    upstox_max_age = st.sidebar.slider(
        "Maximum Upstox Age Seconds",
        5, 60, 15, 5
    )

    st.sidebar.subheader("Scanner Performance")

    workers = st.sidebar.selectbox(
        "Parallel NSE Workers",
        [1, 2, 3],
        index=1
    )

    batch_size = st.sidebar.selectbox(
        "Stocks Per Batch",
        [5, 10, 15, 20],
        index=1
    )

    max_retries = st.sidebar.selectbox(
        "Automatic Retry Passes",
        [0, 1, 2, 3],
        index=2
    )

    retry_delay = st.sidebar.slider(
        "Retry Delay Seconds",
        10, 60, 20, 10
    )

    test_date = st.sidebar.date_input(
        "Historical Test Date",
        value=now_ist().date() - timedelta(days=1),
        max_value=now_ist().date()
    )

    st.caption(
        "Server Time IST: "
        + now_ist().strftime("%d-%m-%Y %H:%M:%S")
    )

    if st.button("RUN ALL VALIDATION SELF TESTS"):
        st.session_state["v82_self_tests"] = {
            "Timestamp": timestamp_self_test(),
            "Verification": verification_self_test()
        }

    if "v82_self_tests" in st.session_state:
        tests = st.session_state["v82_self_tests"]

        passed = all(
            table["Status"].eq("PASS").all()
            for table in tests.values()
        )

        if passed:
            st.success("ALL OFFLINE VALIDATION TESTS PASSED")
        else:
            st.error("VALIDATION SELF TEST FAILED")

        for name, table in tests.items():
            with st.expander(name + " Tests"):
                st.dataframe(
                    table,
                    use_container_width=True
                )

    client = NSEMarketData()

    try:
        if st.button("TEST NSE MCP CONNECTIVITY"):
            with st.spinner("Checking NSE servers..."):
                st.session_state["v8_connections"] = (
                    client.connectivity()
                )

        if "v8_connections" in st.session_state:
            st.json(st.session_state["v8_connections"])

        if st.button("TEST UPSTOX CONNECTIVITY"):
            try:
                result = upstox_market_status(token)
                st.session_state["v82_upstox_status"] = result
                st.success("UPSTOX API CONNECTED")
            except Exception as e:
                st.session_state.pop("v82_upstox_status", None)
                st.error(str(e))

        if "v82_upstox_status" in st.session_state:
            st.json(st.session_state["v82_upstox_status"])

        st.divider()

        if st.button("CHECK LIVE MARKET DATA"):
            tasks = {
                "CM Data Status": client.market_status,
                "Equity Data Status": client.equity_status,
                "NIFTY 50": lambda: client.index_quote("NIFTY 50"),
                "NIFTY BANK": lambda: client.index_quote("NIFTY BANK"),
                "India VIX": client.india_vix,
                "Sector Strength": client.sector_strength,
                "Market Movers": client.market_movers
            }

            results = {}

            for name, function in tasks.items():
                try:
                    results[name] = function()
                except Exception as e:
                    results[name] = {"Error": str(e)}

            st.session_state["v8_live"] = results

        for name, data in st.session_state.get(
            "v8_live", {}
        ).items():
            with st.expander(name):
                st.json(data)

        st.divider()

        if st.button("CHECK HISTORICAL MARKET DATA"):
            tasks = {
                "Market Breadth": lambda: client.market_breadth(test_date),
                "Market Mood": lambda: client.market_mood(test_date),
                "Top Gainers": lambda: client.historical_movers(
                    test_date, "gain"
                ),
                "Top Losers": lambda: client.historical_movers(
                    test_date, "loss"
                ),
                "Highest Turnover": lambda: client.historical_turnover(
                    test_date
                )
            }

            results = {}

            for name, function in tasks.items():
                try:
                    results[name] = function()
                except Exception as e:
                    results[name] = {"Error": str(e)}

            st.session_state["v8_historical"] = results
            st.session_state["v82_historical_date"] = str(test_date)

        if "v82_historical_date" in st.session_state:
            st.caption(
                "Saved Historical Date: "
                + st.session_state["v82_historical_date"]
            )

        for name, data in st.session_state.get(
            "v8_historical", {}
        ).items():
            with st.expander(name):
                st.json(data)

        st.divider()

        st.subheader("NSE + Upstox Cross Verification")

        try:
            universe = get_fo_universe()
        except Exception as e:
            universe = {}
            st.error(str(e))

        st.metric("Total F&O Stocks", len(universe))

        if universe:
            options = sorted(universe)

            default = (
                "DIXON"
                if "DIXON" in options
                else options[0]
            )

            symbol = st.selectbox(
                "Select F&O Stock",
                options,
                index=options.index(default)
            )

            if st.button("TEST SINGLE STOCK"):
                st.session_state.pop("v8_single", None)

                if not token:
                    st.error("Enter Upstox access token")
                else:
                    try:
                        rows = verify_batch(
                            token,
                            [symbol],
                            universe,
                            tolerance,
                            max_age,
                            max_time_gap,
                            upstox_max_age,
                            1,
                            1
                        )

                        st.session_state["v8_single"] = rows[0]

                    except Exception as e:
                        st.error(str(e))

            if "v8_single" in st.session_state:
                result = st.session_state["v8_single"]

                same_settings = (
                    result.get("Maximum Allowed Age Seconds") == max_age
                    and result.get(
                        "Maximum Allowed Upstox Age Seconds"
                    ) == upstox_max_age
                    and result.get(
                        "Maximum Allowed Gap Seconds"
                    ) == max_time_gap
                    and result.get(
                        "Maximum Allowed Price Difference %"
                    ) == tolerance
                )

                st.caption(
                    "Saved test result: "
                    + str(result.get("Symbol"))
                    + " | "
                    + str(result.get("Verification Time IST"))
                )

                if result["Symbol"] != symbol:
                    st.warning("Saved result belongs to another stock")

                elif not same_settings:
                    st.warning("Settings changed. Test again.")

                elif not result["Cross Verified"]:
                    st.error("DATA VERIFICATION BLOCKED")

                elif not row_is_currently_fresh(result):
                    st.warning("PREVIOUSLY VERIFIED — DATA NOW STALE")

                else:
                    st.success("NSE + UPSTOX VERIFIED")

                st.json(result)

            st.divider()

            scan_mode = st.radio(
                "F&O Scan Mode",
                [
                    "Quick Test - 10 Stocks",
                    "Extended Test - 30 Stocks",
                    "Complete F&O Market"
                ]
            )

            if st.button(
                "RUN F&O DATA SCANNER",
                type="primary"
            ):
                if not token:
                    st.error("Enter Upstox access token")

                else:
                    try:
                        market = upstox_market_status(token)

                        if (
                            str(market.get("status", "")).upper()
                            != "NORMAL_OPEN"
                        ):
                            st.warning(
                                "Normal trading session not confirmed. "
                                "Live scan blocked."
                            )
                        else:
                            if scan_mode.startswith("Quick"):
                                selected = options[:10]

                            elif scan_mode.startswith("Extended"):
                                selected = options[:30]

                            else:
                                selected = None

                            with st.spinner(
                                "Running V8.2 market validation..."
                            ):
                                frame, coverage = scan_fo_market(
                                    token,
                                    selected,
                                    tolerance,
                                    max_age,
                                    max_time_gap,
                                    upstox_max_age,
                                    workers,
                                    batch_size,
                                    max_retries,
                                    retry_delay
                                )

                            st.session_state["v8_scan"] = frame
                            st.session_state["v8_coverage"] = coverage

                    except Exception as e:
                        st.error(str(e))

        # ==================================
        # SCAN RESULTS
        # ==================================

        if "v8_scan" in st.session_state:
            frame = st.session_state["v8_scan"]
            coverage = st.session_state["v8_coverage"]

            st.header("V8.2 Verification Summary")

            st.caption(
                "Saved scan: " + str(coverage.get("Scan Time"))
            )

            a, b, c = st.columns(3)

            a.metric(
                "Attempted",
                coverage["Attempted"]
            )

            b.metric(
                "Initial Verified",
                coverage["Initial Verified"]
            )

            c.metric(
                "Initial Blocked",
                coverage["Initial Blocked"]
            )

            a, b, c = st.columns(3)

            a.metric(
                "Recovered After Retry",
                coverage["Recovered After Retry"]
            )

            b.metric(
                "Final Verified",
                coverage["Verified"]
            )

            c.metric(
                "Final Blocked",
                coverage["Blocked"]
            )

            a, b, c = st.columns(3)

            a.metric(
                "Initial Verification Rate",
                f'{coverage["Initial Verification Rate %"]:.2f}%'
            )

            b.metric(
                "Final Verification Rate",
                f'{coverage["Selected Verification Rate %"]:.2f}%'
            )

            c.metric(
                "Scan Duration",
                f'{coverage["Scan Duration Seconds"]:.1f} sec'
            )

            st.progress(
                coverage["Selected Verification Rate %"] / 100
            )

            with st.expander("Complete Coverage Report"):
                st.json(coverage)

            if coverage["Retry Summary"]:
                st.subheader("Automatic Retry Performance")

                st.dataframe(
                    pd.DataFrame(coverage["Retry Summary"]),
                    use_container_width=True
                )

            st.subheader("Blocked Stocks — Root Cause")

            reasons = blocked_reason_analysis(frame)

            if reasons.empty:
                st.success("No blocked stocks in final scan results")
            else:
                st.dataframe(
                    reasons,
                    use_container_width=True
                )

                st.caption(
                    "A stock can have more than one blocking reason."
                )

                st.bar_chart(
                    reasons.set_index("Blocking Reason")[
                        "Affected Stocks"
                    ]
                )

            blocked_frame = frame[
                frame["Cross Verified"] != True
            ]

            if not blocked_frame.empty:
                with st.expander("All Blocked Stocks"):
                    st.dataframe(
                        blocked_frame,
                        use_container_width=True
                    )

            recovered_frame = frame[
                frame["Recovered After Retry"] == True
            ]

            if not recovered_frame.empty:
                with st.expander("Stocks Recovered After Retry"):
                    st.dataframe(
                        recovered_frame,
                        use_container_width=True
                    )

            st.subheader("Complete Data Validation")

            st.dataframe(
                frame,
                use_container_width=True
            )

            # ==================================
            # FRESHNESS AND MOMENTUM
            # ==================================

            ranking_frame = frame[
                (
                    frame["Maximum Allowed Age Seconds"]
                    == max_age
                )
                & (
                    frame["Maximum Allowed Upstox Age Seconds"]
                    == upstox_max_age
                )
                & (
                    frame["Maximum Allowed Gap Seconds"]
                    == max_time_gap
                )
                & (
                    frame["Maximum Allowed Price Difference %"]
                    == tolerance
                )
            ].copy()

            if not ranking_frame.empty:
                reference = now_ist()

                fresh_frame = ranking_frame[
                    ranking_frame.apply(
                        lambda row: row_is_currently_fresh(
                            row, reference
                        ),
                        axis=1
                    )
                ].copy()

            else:
                fresh_frame = ranking_frame.copy()

            ranked = rank_verified_stocks(ranking_frame)

            st.metric(
                "Currently Fresh Verified Stocks",
                len(fresh_frame)
            )

            st.metric(
                "Fresh Stocks Eligible For Momentum Ranking",
                len(ranked)
            )

            st.caption(
                "Verification is historical until freshness "
                "is checked again. Full scan results are not "
                "a simultaneous live market snapshot."
            )

            if not ranked.empty:
                columns = [
                    "Symbol",
                    "Direction",
                    "Upstox LTP",
                    "Upstox Change %",
                    "Upstox Volume",
                    "Momentum Score",
                    "Price Difference %",
                    "Source Time Gap Seconds",
                    "Verification Time IST"
                ]

                st.subheader("Verified CE Watchlist")

                st.dataframe(
                    ranked[
                        ranked["Direction"] == "CE WATCH"
                    ][columns].head(10),
                    use_container_width=True
                )

                st.subheader("Verified PE Watchlist")

                st.dataframe(
                    ranked[
                        ranked["Direction"] == "PE WATCH"
                    ][columns].head(10),
                    use_container_width=True
                )

            else:
                st.warning(
                    "No currently fresh eligible momentum candidates."
                )

            st.divider()

            st.subheader("Download Complete Audit Reports")

            st.download_button(
                "DOWNLOAD VALIDATION CSV",
                frame.to_csv(index=False).encode("utf-8-sig"),
                "v82_market_validation.csv",
                "text/csv"
            )

            if "v82_attempt_log" in st.session_state:
                attempt_log = st.session_state["v82_attempt_log"]

                st.download_button(
                    "DOWNLOAD ALL RETRY ATTEMPTS CSV",
                    attempt_log.to_csv(
                        index=False
                    ).encode("utf-8-sig"),
                    "v82_all_attempts.csv",
                    "text/csv"
                )

            if not reasons.empty:
                st.download_button(
                    "DOWNLOAD BLOCKED REASONS CSV",
                    reasons.to_csv(
                        index=False
                    ).encode("utf-8-sig"),
                    "v82_blocked_reasons.csv",
                    "text/csv"
                )

        # ==================================
        # JSON AUDIT REPORT
        # ==================================

        report = {
            "Engine Version": VERSION,
            "Report Time IST": now_ist().isoformat(),

            "Connectivity": st.session_state.get(
                "v8_connections", {}
            ),

            "Upstox Connectivity": st.session_state.get(
                "v82_upstox_status", {}
            ),

            "Live Market": st.session_state.get(
                "v8_live", {}
            ),

            "Historical Market": st.session_state.get(
                "v8_historical", {}
            ),

            "Single Stock Verification": st.session_state.get(
                "v8_single", {}
            ),

            "Coverage": st.session_state.get(
                "v8_coverage", {}
            )
        }

        st.download_button(
            "DOWNLOAD V8.2 DATA REPORT",
            json.dumps(
                report,
                indent=2,
                default=str
            ),
            "v82_data_engine_report.json",
            "application/json"
        )

    finally:
        client.close()

    st.divider()

    st.warning(
        "Research only. No automatic orders. "
        "Verified prices and momentum rankings are not "
        "final option entry signals. "
        "Export reports before closing the session."
    )


# ==========================================
# STANDALONE EXECUTION
# ==========================================

if __name__ == "__main__":
    st.set_page_config(
        page_title="NSE F&O PRO V8.2",
        page_icon="📊",
        layout="wide"
    )

    render_market_data_engine()
