# market_data_engine.py
# NSE F&O PRO V8.1
# NSE MCP + UPSTOX DATA VALIDATION ENGINE
# Research only - No automatic orders

import json
import math
import gzip
import re
import requests
import pandas as pd
import streamlit as st

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

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
        if math.isfinite(value):
            return value
    except (ValueError, TypeError, OverflowError):
        pass

    return None


def parse_time(value):
    try:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, str):
            value = value.strip()

            if not value:
                return None

            if re.fullmatch(
                r"[+-]?\d+(?:\.\d+)?", value
            ):
                value = (
                    float(value)
                    if "." in value
                    else int(value)
                )

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

    return float(
        (reference - ts).total_seconds()
    )


def is_fresh(
    value,
    max_seconds=180,
    reference=None
):

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

    if ts is None:
        return None

    return ts.isoformat()


def timestamp_self_test():

    expected = pd.Timestamp(
        "2026-11-25 14:51:04.449",
        tz=IST
    )

    cases = [
        1795598464449,
        "1795598464449",
        "1795598464.449",
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

    for value in [None, "", "invalid"]:

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

        self.headers = {
            "Content-Type": "application/json",
            "Accept": (
                "application/json, text/event-stream"
            )
        }

        self.initialized = False

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

        response = self.session.post(
            self.url,
            json=payload,
            headers=self.headers,
            timeout=(10, 40)
        )

        response.raise_for_status()

        session_id = response.headers.get(
            "Mcp-Session-Id"
        )

        if session_id:
            self.headers[
                "Mcp-Session-Id"
            ] = session_id

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

                    event_data.append(
                        line[5:].strip()
                    )

                elif not line and event_data:

                    try:

                        item = json.loads(
                            "\n".join(event_data)
                        )

                        if item.get("id") == (
                            self.request_id
                        ):
                            result = item

                    except Exception:
                        pass

                    event_data = []

            if result is None:
                raise RuntimeError(
                    "No matching MCP response"
                )

        else:
            result = response.json()

        if "error" in result:
            raise RuntimeError(
                str(result["error"])
            )

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
                    "version": "8.1"
                }
            }
        )

        version = result.get(
            "protocolVersion",
            "2025-03-26"
        )

        self.headers[
            "MCP-Protocol-Version"
        ] = version

        self.request(
            "notifications/initialized",
            notification=True
        )

        self.initialized = True

    def list_tools(self):

        self.initialize()

        tools = []
        params = {}

        while True:

            result = self.request(
                "tools/list",
                params
            )

            tools.extend(
                result.get("tools", [])
            )

            cursor = result.get("nextCursor")

            if not cursor:
                break

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

        structured = result.get(
            "structuredContent"
        )

        if structured is not None:
            data = structured

        else:

            data = None

            for item in result.get(
                "content", []
            ):

                if item.get("type") != "text":
                    continue

                text = item.get("text", "")

                try:
                    data = json.loads(text)
                    break

                except Exception:
                    continue

        if data is None:
            raise RuntimeError(
                "No valid JSON data from NSE"
            )

        if isinstance(data, dict):

            if data.get("error"):
                raise RuntimeError(
                    str(data["error"])
                )

        return data


# ==========================================
# NSE MARKET DATA ENGINE
# ==========================================

class NSEMarketData:

    def __init__(self):

        self.cm = NSEMCPClient(NSE_CM_URL)

        self.bhavcopy = NSEMCPClient(
            NSE_BHAVCOPY_URL
        )

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

        return self.cm.call(
            "cm_get_data_status", {}
        )

    def equity_status(self):

        return self.cm.call(
            "cm_get_allstocks_status", {}
        )

    def stock_quote(self, symbol):

        return self.cm.call(
            "cm_get_stock_quote",
            {"symbol": symbol.upper()}
        )

    def equity_stocks(
        self,
        prefix="",
        limit=500
    ):

        return self.cm.call(
            "cm_get_equity_stocks",
            {
                "limit": min(
                    max(int(limit), 1), 500
                ),
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
                "limit": min(
                    max(int(limit), 1), 50
                )
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

    def historical_movers(
        self,
        date,
        direction="gain"
    ):

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

    def corporate_actions(
        self, symbol, start, end
    ):

        return self.bhavcopy.call(
            "get_corporate_actions",
            {
                "symbol": symbol.upper(),
                "fromDate": str(start),
                "toDate": str(end)
            }
        )

    def stock_history(
        self,
        symbol,
        months=3,
        end_date=None
    ):

        end = (
            pd.Timestamp(end_date).date()
            if end_date
            else now_ist().date()
        )

        months = min(
            max(int(months), 1), 3
        )

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
                "Raw bhavcopy prices. "
                "Review corporate actions before "
                "calculating returns."
            )
        }

    def volume_analysis(
        self,
        symbol,
        days=30
    ):

        return self.bhavcopy.call(
            "get_volume_analysis",
            {
                "symbol": symbol.upper(),
                "days": min(
                    max(int(days), 5), 252
                )
            }
        )


# ==========================================
# UPSTOX DATA ENGINE
# ==========================================

@st.cache_data(ttl=21600, show_spinner=False)
def get_fo_universe():

    response = requests.get(
        MASTER_URL,
        timeout=40
    )

    response.raise_for_status()

    instruments = json.loads(
        gzip.decompress(response.content)
    )

    underlying_keys = {

        item.get("underlying_key")

        for item in instruments

        if item.get("segment") == "NSE_FO"

        and item.get("instrument_type")
        in ("CE", "PE")

        and item.get("underlying_key")
    }

    universe = {}

    for item in instruments:

        if (
            item.get("segment") == "NSE_EQ"

            and item.get("instrument_key")
            in underlying_keys
        ):

            symbol = item.get(
                "trading_symbol"
            )

            if symbol:

                universe[symbol] = item[
                    "instrument_key"
                ]

    if not universe:

        raise RuntimeError(
            "F&O universe is empty"
        )

    return universe


def upstox_api(token, path, params=None):

    if not token:

        raise RuntimeError(
            "Upstox access token required"
        )

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

        raise RuntimeError(
            "Upstox API request failed"
        )

    return result.get("data", {})


def upstox_quotes(token, keys):

    keys = list(dict.fromkeys(keys))

    quotes = {}

    for start in range(0, len(keys), 40):

        batch = keys[start:start + 40]

        data = upstox_api(
            token,
            "/v2/market-quote/quotes",
            {
                "instrument_key": ",".join(batch)
            }
        )

        for item in data.values():

            key = item.get(
                "instrument_token"
            )

            if key:
                quotes[key] = item

    return quotes


def upstox_market_status(token):

    return upstox_api(
        token,
        "/v2/market/status/NSE"
    )


# ==========================================
# NSE DATA QUALITY VALIDATION
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

    ltp = number(
        stock.get("lastTradedPrice")
    )

    previous = number(
        stock.get("preClosePrice")
    )

    op = number(
        stock.get("openPrice")
    )

    high = number(
        stock.get("highPrice")
    )

    low = number(
        stock.get("lowPrice")
    )

    volume = number(
        stock.get("volume")
    )

    timestamp = stock.get(
        "latestTimestamp"
    )

    if not symbol:
        errors.append("MISSING SYMBOL")

    if stock.get("series") != "EQ":
        errors.append("NON EQ SERIES")

    prices = [
        ltp, previous, op, high, low
    ]

    if any(
        p is None or p <= 0
        for p in prices
    ):

        errors.append("INVALID OHLC PRICE")

    else:

        if high < low:
            errors.append("INVALID RANGE")

        if not low <= ltp <= high:
            errors.append(
                "LTP OUTSIDE DAY RANGE"
            )

        if not low <= op <= high:
            errors.append(
                "OPEN OUTSIDE DAY RANGE"
            )

    if volume is None or volume <= 0:

        errors.append("INVALID VOLUME")

    nse_time = parse_time(timestamp)

    age = data_age(
        timestamp,
        reference
    )

    if nse_time is None:

        errors.append(
            "INVALID NSE TIMESTAMP"
        )

    elif age < -5:

        errors.append(
            "NSE TIMESTAMP IN FUTURE"
        )

    elif not is_fresh(
        timestamp,
        max_age,
        reference
    ):

        errors.append("STALE NSE DATA")

    change = None
    change_pct = None

    if (
        ltp is not None
        and previous is not None
        and previous > 0
    ):

        change = ltp - previous

        change_pct = (
            change / previous
        ) * 100

        reported = number(
            stock.get("change")
        )

        reported_pct = number(
            stock.get("perChange")
        )

        if (
            reported is not None
            and abs(change - reported) > 0.11
        ):

            warnings.append(
                "PRICE CHANGE RECALCULATED"
            )

        if (
            reported_pct is not None
            and abs(
                change_pct - reported_pct
            ) > 0.10
        ):

            warnings.append(
                "PERCENT CHANGE RECALCULATED"
            )

    position = None

    if (
        high is not None
        and low is not None
        and ltp is not None
        and high > low
    ):

        position = (
            (ltp - low) / (high - low)
        ) * 100

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

        "NSE Timestamp IST": format_time(
            timestamp
        ),

        "NSE Age Seconds": (
            round(age, 3)
            if age is not None
            else None
        ),

        "NSE Valid": not errors,

        "Errors": " | ".join(errors),

        "Warnings": " | ".join(warnings)
    }


# ==========================================
# NSE + UPSTOX CROSS VERIFICATION
# ==========================================

def cross_verify(
    nse_stock,
    upstox_stock,
    tolerance_pct=0.35,
    max_age=180,
    max_time_gap=90,
    reference=None
):

    reference = reference or now_ist()

    result = validate_nse_stock(
        nse_stock,
        max_age,
        reference
    )

    upstox_stock = upstox_stock or {}

    upstox_ltp = number(
        upstox_stock.get("last_price")
    )

    nse_ltp = result["NSE LTP"]

    trade_timestamp = upstox_stock.get(
        "last_trade_time"
    )

    quote_timestamp = upstox_stock.get(
        "timestamp"
    )

    timestamp = trade_timestamp

    timestamp_source = "last_trade_time"

    if (
        trade_timestamp is None
        or trade_timestamp == ""
    ):

        timestamp = quote_timestamp

        timestamp_source = "timestamp"

    reasons = []

    if not result["NSE Valid"]:

        reasons.append(
            result["Errors"]
        )

    if upstox_ltp is None or upstox_ltp <= 0:

        reasons.append(
            "UPSTOX PRICE UNAVAILABLE"
        )

    nse_time = parse_time(
        result["NSE Timestamp"]
    )

    upstox_time = parse_time(
        timestamp
    )

    upstox_age = data_age(
        timestamp,
        reference
    )

    if upstox_time is None:

        reasons.append(
            "INVALID UPSTOX TIMESTAMP"
        )

    elif upstox_age < -5:

        reasons.append(
            "UPSTOX TIMESTAMP IN FUTURE"
        )

    elif not is_fresh(
        timestamp,
        max_age,
        reference
    ):

        reasons.append(
            "STALE UPSTOX DATA"
        )

    # Quote timestamp alone does not prove
    # that the last traded price is fresh.

    if timestamp_source != "last_trade_time":

        reasons.append(
            "LAST TRADE TIME UNAVAILABLE"
        )

    time_gap = None

    if (
        nse_time is not None
        and upstox_time is not None
    ):

        time_gap = abs(
            (
                nse_time - upstox_time
            ).total_seconds()
        )

        if time_gap > max_time_gap:

            reasons.append(
                "SOURCE TIMESTAMP MISMATCH"
            )

    else:

        reasons.append(
            "TIMESTAMP UNVERIFIED"
        )

    difference = None

    if (
        nse_ltp is not None
        and nse_ltp > 0
        and upstox_ltp is not None
        and upstox_ltp > 0
    ):

        difference = (

            abs(nse_ltp - upstox_ltp)

            / nse_ltp

        ) * 100

        if difference > tolerance_pct:

            reasons.append(
                "SOURCE PRICE MISMATCH"
            )

    else:

        reasons.append(
            "PRICE COMPARISON UNAVAILABLE"
        )

    reasons = list(
        dict.fromkeys(reasons)
    )

    result.update({

        "Upstox LTP": upstox_ltp,

        "Upstox Timestamp": timestamp,

        "Upstox Timestamp Source":
            timestamp_source,

        "Upstox Timestamp IST":
            format_time(timestamp),

        "Upstox Quote Timestamp IST":
            format_time(quote_timestamp),

        "Upstox Age Seconds": (
            round(upstox_age, 3)
            if upstox_age is not None
            else None
        ),

        "Source Time Gap Seconds": (
            round(time_gap, 3)
            if time_gap is not None
            else None
        ),

        "Price Difference %": (
            round(difference, 6)
            if difference is not None
            else None
        ),

        "Maximum Allowed Age Seconds":
            max_age,

        "Maximum Allowed Gap Seconds":
            max_time_gap,

        "Maximum Allowed Price Difference %":
            tolerance_pct,

        "Verification Time IST":
            reference.isoformat(),

        "Cross Verified": not reasons,

        "Verification Status": (

            "VERIFIED"

            if not reasons

            else "BLOCKED"
        ),

        "Verification Reasons": (
            " | ".join(reasons)
        )
    })

    return result


# ==========================================
# COMPLETE F&O COVERAGE SCANNER
# ==========================================

def scan_fo_market(
    token,
    symbols=None,
    tolerance_pct=0.35,
    max_age=180,
    max_time_gap=90
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

        raise RuntimeError(
            "No valid F&O symbols selected"
        )

    client = NSEMarketData()

    rows = []

    progress = st.progress(0)

    try:

        # Smaller batches reduce the time
        # gap between NSE and Upstox quotes.

        for start in range(
            0, len(selected), 10
        ):

            batch = selected[
                start:start + 10
            ]

            nse_records = {}

            for symbol in batch:

                try:

                    response = (
                        client.stock_quote(symbol)
                    )

                    stock = response.get(
                        "stock", {}
                    )

                    if stock.get("symbol") != symbol:

                        raise RuntimeError(
                            "NSE symbol mismatch"
                        )

                    nse_records[symbol] = stock

                except Exception as e:

                    rows.append({

                        "Symbol": symbol,

                        "Cross Verified": False,

                        "Verification Status":
                            "NSE UNAVAILABLE",

                        "Verification Reasons":
                            str(e)
                    })

            try:

                quotes = upstox_quotes(

                    token,

                    [
                        universe[s]
                        for s in nse_records
                    ]
                )

                quote_error = ""

            except Exception as e:

                quotes = {}

                quote_error = str(e)

            reference = now_ist()

            for symbol, stock in (
                nse_records.items()
            ):

                result = cross_verify(

                    stock,

                    quotes.get(
                        universe[symbol], {}
                    ),

                    tolerance_pct,

                    max_age,

                    max_time_gap,

                    reference
                )

                if quote_error:

                    result[
                        "Verification Reasons"
                    ] += " | " + quote_error

                    result["Cross Verified"] = False

                    result[
                        "Verification Status"
                    ] = "BLOCKED"

                result["Instrument Key"] = (
                    universe[symbol]
                )

                rows.append(result)

            progress.progress(

                min(
                    (start + len(batch))
                    / len(selected),
                    1.0
                )
            )

    finally:

        client.close()

        progress.empty()

    frame = pd.DataFrame(rows)

    verified = int(

        frame["Cross Verified"]

        .fillna(False)

        .sum()
    )

    coverage = {

        "Total F&O Universe": len(universe),

        "Selected": len(selected),

        "Attempted": len(frame),

        "Verified": verified,

        "Blocked": len(frame) - verified,

        "Attempted Coverage %": round(

            100 * len(frame) / len(universe),

            2
        ),

        "Verified Coverage %": round(

            100 * verified / len(universe),

            2
        ),

        "Selected Verification Rate %": round(

            100 * verified / len(selected),

            2
        ),

        "Scan Time": str(now_ist())
    }

    return frame, coverage


# ==========================================
# PRELIMINARY MOMENTUM RANKING
# NOT A FINAL TRADING SIGNAL
# ==========================================

def rank_verified_stocks(frame):

    if frame.empty:
        return frame.copy()

    data = frame[

        frame["Cross Verified"] == True

    ].copy()

    if data.empty:
        return data

    reference = now_ist()

    # Previously verified records must
    # still be fresh when ranked.

    fresh_mask = data.apply(

        lambda row: (

            is_fresh(

                row.get("NSE Timestamp"),

                row.get(
                    "Maximum Allowed Age Seconds",
                    180
                ),

                reference
            )

            and is_fresh(

                row.get("Upstox Timestamp"),

                row.get(
                    "Maximum Allowed Age Seconds",
                    180
                ),

                reference
            )
        ),

        axis=1
    )

    data = data[fresh_mask].copy()

    if data.empty:
        return data

    def classify(row):

        change = row["Change %"]

        position = row[
            "Day Range Position"
        ]

        ltp = row["NSE LTP"]

        op = row["Open"]

        if (
            pd.isna(change)
            or pd.isna(position)
        ):

            return "NEUTRAL"

        if (
            change >= 0.35
            and position >= 60
            and ltp > op
        ):

            return "CE WATCH"

        if (
            change <= -0.35
            and position <= 40
            and ltp < op
        ):

            return "PE WATCH"

        return "NEUTRAL"

    data["Direction"] = data.apply(
        classify,
        axis=1
    )

    def score(row):

        if row["Direction"] == "NEUTRAL":
            return 0.0

        strength = min(

            abs(row["Change %"]) / 2.5,

            1.0
        )

        position = row[
            "Day Range Position"
        ]

        if row["Direction"] == "PE WATCH":

            position = 100 - position

        return round(

            strength * 60

            + position * 0.40,

            2
        )

    data["Momentum Score"] = data.apply(
        score,
        axis=1
    )

    return data.sort_values(

        "Momentum Score",

        ascending=False
    )


# ==========================================
# STREAMLIT INDEPENDENT TEST DASHBOARD
# ==========================================

def render_market_data_engine():

    st.title(
        "NSE F&O PRO V8.1 — DATA ENGINE"
    )

    st.caption(
        "NSE MCP + Upstox | "
        "Timestamp Fixed | "
        "Data Quality | Cross Verification"
    )

    if st.session_state.get(
        "v8_engine_version"
    ) != "8.1":

        for key in [
            "v8_single",
            "v8_scan",
            "v8_coverage"
        ]:

            st.session_state.pop(
                key, None
            )

        st.session_state[
            "v8_engine_version"
        ] = "8.1"

    token = st.sidebar.text_input(

        "Upstox Access Token",

        type="password"
    ).strip()

    tolerance = st.sidebar.slider(

        "Maximum Price Difference %",

        min_value=0.10,

        max_value=1.00,

        value=0.35,

        step=0.05
    )

    max_age = st.sidebar.slider(

        "Maximum Data Age Seconds",

        min_value=60,

        max_value=300,

        value=180,

        step=30
    )

    max_time_gap = st.sidebar.slider(

        "Maximum Source Time Gap Seconds",

        min_value=30,

        max_value=180,

        value=90,

        step=15
    )

    test_date = st.sidebar.date_input(

        "Historical Test Date",

        value=datetime(
            2026, 11, 23
        ).date(),

        max_value=now_ist().date()
    )

    st.caption(
        "Server Time IST: "
        + now_ist().strftime(
            "%d-%m-%Y %H:%M:%S"
        )
    )

    if st.button(
        "TEST TIMESTAMP CONVERSION"
    ):

        st.session_state[
            "v81_timestamp_test"
        ] = timestamp_self_test()

    if "v81_timestamp_test" in st.session_state:

        tests = st.session_state[
            "v81_timestamp_test"
        ]

        if tests["Status"].eq("PASS").all():

            st.success(
                "ALL TIMESTAMP TESTS PASSED"
            )

        else:

            st.error(
                "TIMESTAMP TEST FAILED"
            )

        st.dataframe(
            tests,
            use_container_width=True
        )

    client = NSEMarketData()

    try:

        if st.button(
            "TEST NSE MCP CONNECTIVITY"
        ):

            with st.spinner(
                "Checking NSE servers..."
            ):

                st.session_state[
                    "v8_connections"
                ] = client.connectivity()

        if "v8_connections" in st.session_state:

            st.json(

                st.session_state[
                    "v8_connections"
                ]
            )

        st.divider()

        if st.button(
            "CHECK LIVE MARKET DATA"
        ):

            tasks = {

                "CM Data Status":
                    client.market_status,

                "Equity Data Status":
                    client.equity_status,

                "NIFTY 50":
                    lambda: client.index_quote(
                        "NIFTY 50"
                    ),

                "NIFTY BANK":
                    lambda: client.index_quote(
                        "NIFTY BANK"
                    ),

                "India VIX":
                    client.india_vix,

                "Sector Strength":
                    client.sector_strength,

                "Market Movers":
                    client.market_movers
            }

            results = {}

            for name, function in tasks.items():

                try:

                    results[name] = function()

                except Exception as e:

                    results[name] = {
                        "Error": str(e)
                    }

            st.session_state[
                "v8_live"
            ] = results

        for name, data in (

            st.session_state.get(
                "v8_live", {}
            ).items()
        ):

            with st.expander(name):

                st.json(data)

        st.divider()

        if st.button(
            "CHECK HISTORICAL MARKET DATA"
        ):

            tasks = {

                "Market Breadth":
                    lambda: client.market_breadth(
                        test_date
                    ),

                "Market Mood":
                    lambda: client.market_mood(
                        test_date
                    ),

                "Top Gainers":
                    lambda: client.historical_movers(
                        test_date, "gain"
                    ),

                "Top Losers":
                    lambda: client.historical_movers(
                        test_date, "loss"
                    ),

                "Highest Turnover":
                    lambda: client.historical_turnover(
                        test_date
                    )
            }

            results = {}

            for name, function in tasks.items():

                try:

                    results[name] = function()

                except Exception as e:

                    results[name] = {
                        "Error": str(e)
                    }

            st.session_state[
                "v8_historical"
            ] = results

        for name, data in (

            st.session_state.get(
                "v8_historical", {}
            ).items()
        ):

            with st.expander(name):

                st.json(data)

        st.divider()

        st.subheader(
            "NSE + Upstox Cross Verification"
        )

        universe = {}

        try:

            universe = get_fo_universe()

            st.metric(
                "Total F&O Stocks",
                len(universe)
            )

        except Exception as e:

            st.error(str(e))

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

            if st.button(
                "TEST SINGLE STOCK"
            ):

                st.session_state.pop(
                    "v8_single", None
                )

                if not token:

                    st.error(
                        "Enter Upstox access token"
                    )

                else:

                    try:

                        stock = client.stock_quote(
                            symbol
                        ).get("stock", {})

                        if stock.get(
                            "symbol"
                        ) != symbol:

                            raise RuntimeError(
                                "NSE symbol mismatch"
                            )

                        quotes = upstox_quotes(

                            token,

                            [universe[symbol]]
                        )

                        result = cross_verify(

                            stock,

                            quotes.get(
                                universe[symbol], {}
                            ),

                            tolerance,

                            max_age,

                            max_time_gap
                        )

                        st.session_state[
                            "v8_single"
                        ] = result

                    except Exception as e:

                        st.error(str(e))

            if "v8_single" in st.session_state:

                result = st.session_state[
                    "v8_single"
                ]

                st.caption(
                    "Saved test result: "
                    + str(result.get("Symbol"))
                    + " | "
                    + str(
                        result.get(
                            "Verification Time IST"
                        )
                    )
                )

                still_fresh = (

                    is_fresh(

                        result.get(
                            "NSE Timestamp"
                        ),

                        max_age
                    )

                    and is_fresh(

                        result.get(
                            "Upstox Timestamp"
                        ),

                        max_age
                    )
                )

                same_settings = (

                    result.get(
                        "Maximum Allowed Age Seconds"
                    ) == max_age

                    and result.get(
                        "Maximum Allowed Gap Seconds"
                    ) == max_time_gap

                    and result.get(
                        "Maximum Allowed Price Difference %"
                    ) == tolerance
                )

                if result["Symbol"] != symbol:

                    st.warning(
                        "Saved result belongs to "
                        "another stock. Test again."
                    )

                elif not same_settings:

                    st.warning(
                        "Settings changed. "
                        "Test again."
                    )

                elif not result["Cross Verified"]:

                    st.warning(
                        "DATA VERIFICATION BLOCKED"
                    )

                elif not still_fresh:

                    st.warning(
                        "PREVIOUSLY VERIFIED — "
                        "DATA NOW STALE"
                    )

                else:

                    st.success(
                        "NSE + UPSTOX VERIFIED"
                    )

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

                st.session_state.pop(
                    "v8_scan", None
                )

                st.session_state.pop(
                    "v8_coverage", None
                )

                if not token:

                    st.error(
                        "Enter Upstox access token"
                    )

                else:

                    try:

                        if scan_mode.startswith(
                            "Quick"
                        ):

                            selected = options[:10]

                        elif scan_mode.startswith(
                            "Extended"
                        ):

                            selected = options[:30]

                        else:

                            selected = None

                        with st.spinner(
                            "Verifying NSE and Upstox..."
                        ):

                            frame, coverage = (
                                scan_fo_market(

                                    token,

                                    selected,

                                    tolerance,

                                    max_age,

                                    max_time_gap
                                )
                            )

                        st.session_state[
                            "v8_scan"
                        ] = frame

                        st.session_state[
                            "v8_coverage"
                        ] = coverage

                    except Exception as e:

                        st.error(str(e))

        if "v8_scan" in st.session_state:

            frame = st.session_state[
                "v8_scan"
            ]

            coverage = st.session_state[
                "v8_coverage"
            ]

            st.caption(
                "Saved scan results. "
                "Verification counts refer to "
                "the original scan time."
            )

            a, b, c = st.columns(3)

            a.metric(
                "Attempted",
                coverage["Attempted"]
            )

            b.metric(
                "Verified at Scan",
                coverage["Verified"]
            )

            c.metric(
                "Blocked",
                coverage["Blocked"]
            )

            st.json(coverage)

            st.subheader(
                "Complete Data Validation"
            )

            st.dataframe(

                frame,

                use_container_width=True
            )

            ranking_frame = frame.copy()

            required_columns = [

                "Maximum Allowed Age Seconds",

                "Maximum Allowed Gap Seconds",

                "Maximum Allowed Price Difference %"
            ]

            if all(
                col in ranking_frame.columns
                for col in required_columns
            ):

                ranking_frame = ranking_frame[

                    (
                        ranking_frame[
                            required_columns[0]
                        ] == max_age
                    )

                    & (
                        ranking_frame[
                            required_columns[1]
                        ] == max_time_gap
                    )

                    & (
                        ranking_frame[
                            required_columns[2]
                        ] == tolerance
                    )
                ]

            else:

                ranking_frame = (
                    ranking_frame.iloc[0:0]
                )

            ranked = rank_verified_stocks(
                ranking_frame
            )

            st.metric(
                "Currently Fresh Verified Stocks",
                len(ranked)
            )

            if not ranked.empty:

                st.subheader(
                    "Verified CE Watchlist"
                )

                st.dataframe(

                    ranked[

                        ranked["Direction"]
                        == "CE WATCH"

                    ].head(10),

                    use_container_width=True
                )

                st.subheader(
                    "Verified PE Watchlist"
                )

                st.dataframe(

                    ranked[

                        ranked["Direction"]
                        == "PE WATCH"

                    ].head(10),

                    use_container_width=True
                )

            else:

                st.warning(
                    "No fresh verified candidates "
                    "under current settings."
                )

            st.download_button(

                "DOWNLOAD VALIDATION CSV",

                frame.to_csv(
                    index=False
                ).encode("utf-8"),

                "v81_market_validation.csv",

                "text/csv"
            )

        report = {

            "Engine Version": "8.1",

            "Report Time IST":
                now_ist().isoformat(),

            "Connectivity":
                st.session_state.get(
                    "v8_connections", {}
                ),

            "Live Market":
                st.session_state.get(
                    "v8_live", {}
                ),

            "Historical Market":
                st.session_state.get(
                    "v8_historical", {}
                ),

            "Single Stock Verification":
                st.session_state.get(
                    "v8_single", {}
                ),

            "Coverage":
                st.session_state.get(
                    "v8_coverage", {}
                )
        }

        st.download_button(

            "DOWNLOAD V8 DATA REPORT",

            json.dumps(
                report,
                indent=2,
                default=str
            ),

            "v81_data_engine_report.json",

            "application/json"
        )

    finally:

        client.close()

    st.divider()

    st.warning(

        "Research only. Verified prices and "
        "momentum rankings are not final "
        "option entry signals. "
        "Verification is a snapshot, not "
        "continuous monitoring. "
        "No automatic orders are placed."
    )


# ==========================================
# STANDALONE EXECUTION
# ==========================================

if __name__ == "__main__":

    st.set_page_config(

        page_title="NSE F&O PRO V8.1",

        page_icon="📊",

        layout="wide"
    )

    render_market_data_engine()