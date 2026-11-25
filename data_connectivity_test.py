import streamlit as st
import pandas as pd
import requests
import gzip
import json
import time
import io
import zipfile
import re
import math
import uuid

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin

st.set_page_config(
    page_title="F&O DATA CONNECTIVITY V3",
    page_icon="📡",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")

NSE = "https://www.nseindia.com"
ARC = "https://nsearchives.nseindia.com"
UP = "https://api.upstox.com"

HEADERS = {
    "User-Agent": "FNO-Data-Research/3.0",
    "Accept": "application/json, text/html, */*"
}

ALLOWED_DOMAINS = (
    "nseindia.com",
    "nseindia.in",
    "bseindia.com",
    "upstox.com"
)

st.title("F&O DATA CONNECTIVITY TEST V3")

st.caption(
    "NSE DIRECT | MCP | UPSTOX | WEBSOCKET | "
    "NEWS INTELLIGENCE | CROSS VERIFICATION"
)

st.info(
    "Read-only research. No automatic orders. "
    "Source validation is not prediction accuracy."
)

token = st.sidebar.text_input(
    "Upstox Access Token",
    type="password"
).strip()

symbol = st.sidebar.text_input(
    "Test Stock",
    value="DIXON"
).strip().upper()

report_date = st.sidebar.date_input(
    "NSE Report Date",
    value=datetime.now(IST).date()
    - timedelta(days=1)
)

full_quotes = st.sidebar.checkbox(
    "Test All F&O Stock Quotes",
    value=True
)

full_news = st.sidebar.checkbox(
    "Test All F&O Stock News",
    value=True
)

news_pages = st.sidebar.number_input(
    "Maximum News Pages Per Batch",
    min_value=1,
    max_value=10,
    value=3
)

enable_ws = st.sidebar.checkbox(
    "Test Live WebSocket",
    value=True
)

ws_seconds = st.sidebar.slider(
    "WebSocket Sample Seconds",
    5, 30, 15
)

results = []
payloads = {}
quote_rows = []
news_rows = []
reports = {}
ws_rows = []

def now():
    return datetime.now(IST)

def safe_url(url):

    try:
        p = urlparse(url)
        host = p.hostname or ""

        return (
            p.scheme == "https"
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and any(
                host == d
                or host.endswith("." + d)
                for d in ALLOWED_DOMAINS
            )
        )

    except Exception:
        return False

def display_url(url):

    p = urlparse(url)

    return f"{p.scheme}://{p.hostname}{p.path}"

def stamp(value):

    if value is None or value == "":
        return None

    try:

        if isinstance(value, (int, float)) or (
            isinstance(value, str)
            and value.isdigit()
        ):

            v = float(value)

            dt = pd.to_datetime(
                v,
                unit="ms" if abs(v) > 1e11 else "s",
                utc=True
            )

        else:

            dt = pd.to_datetime(value)

        if pd.isna(dt):
            return None

        if dt.tzinfo is None:
            dt = dt.tz_localize(IST)
        else:
            dt = dt.tz_convert(IST)

        return dt

    except Exception:
        return None

def age(value, checked=None):

    dt = stamp(value)

    if dt is None:
        return None

    checked = checked or now()

    return round(
        (checked - dt.to_pydatetime())
        .total_seconds(),
        3
    )

def number(value):

    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except Exception:
        return None

def add(
    source,
    name,
    status,
    http="-",
    ms=0,
    records=0,
    timestamp=None,
    note="",
    final_url=""
):

    dt = stamp(timestamp)

    results.append({
        "Source": source,
        "Data": name,
        "Status": status,
        "HTTP": http,
        "Response ms": ms,
        "Records": records,
        "Timestamp IST": str(dt) if dt is not None else "",
        "Age Seconds": age(timestamp),
        "Remarks": note,
        "Final URL": final_url,
        "Checked IST": str(now())
    })

def http_reason(code):

    return {
        301: "Permanent redirect",
        302: "Temporary redirect",
        400: "Invalid parameters",
        401: "Authentication required",
        403: "Access refused",
        404: "Endpoint or report not found",
        429: "Rate limit",
        500: "Server error",
        502: "Gateway error",
        503: "Unavailable",
        504: "Gateway timeout"
    }.get(code, f"HTTP {code}")

def request(
    url,
    params=None,
    headers=None,
    method="GET",
    body=None,
    timeout=15
):

    started = time.perf_counter()

    h = dict(headers or HEADERS)

    current = url
    redirects = []
    retries = 0

    try:

        with requests.Session() as session:

            while True:

                if not safe_url(current):
                    raise ValueError(
                        "Unapproved destination"
                    )

                r = session.request(
                    method,
                    current,
                    params=params,
                    headers=h,
                    json=body,
                    timeout=timeout,
                    allow_redirects=False
                )

                if r.status_code in (
                    301, 302, 303, 307, 308
                ):

                    location = r.headers.get(
                        "Location"
                    )

                    if (
                        not location
                        or len(redirects) >= 4
                        or method != "GET"
                    ):
                        break

                    target = urljoin(
                        r.url,
                        location
                    )

                    if not safe_url(target):
                        break

                    if (
                        urlparse(current).netloc
                        != urlparse(target).netloc
                    ):

                        h = {
                            k: v for k, v in h.items()
                            if k.lower() not in (
                                "authorization",
                                "cookie",
                                "mcp-session-id"
                            )
                        }

                    redirects.append(
                        f"{r.status_code}: "
                        f"{display_url(target)}"
                    )

                    current = target
                    params = None
                    continue

                if (
                    method == "GET"
                    and r.status_code in (
                        429, 500, 502, 503, 504
                    )
                    and retries < 1
                ):

                    try:
                        wait = float(
                            r.headers.get(
                                "Retry-After", "2"
                            )
                        )
                    except Exception:
                        break

                    if wait <= 10:
                        retries += 1
                        time.sleep(max(1, wait))
                        continue

                break

        return (
            r,
            round(
                (time.perf_counter() - started)
                * 1000
            ),
            " | ".join(redirects)
        )

    except Exception as e:

        return (
            None,
            round(
                (time.perf_counter() - started)
                * 1000
            ),
            type(e).__name__
        )

def fetch(
    source,
    name,
    url,
    params=None,
    headers=None,
    expected="json"
):

    r, ms, detail = request(
        url,
        params=params,
        headers=headers
    )

    if r is None:

        add(
            source,
            name,
            "FAILED",
            ms=ms,
            note=detail
        )

        return None, ms

    if r.status_code != 200:

        add(
            source,
            name,
            "FAILED",
            r.status_code,
            ms,
            note=(
                http_reason(r.status_code)
                + " | " + detail
            ),
            final_url=display_url(r.url)
        )

        return None, ms

    if expected == "html":

        add(
            source,
            name,
            "PAGE REACHABLE",
            200,
            ms,
            note=detail,
            final_url=display_url(r.url)
        )

        return r.text, ms

    try:

        data = r.json()

        return data, ms

    except Exception:

        add(
            source,
            name,
            "INVALID JSON",
            200,
            ms,
            note=detail,
            final_url=display_url(r.url)
        )

        return None, ms

def rows_from(data):

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        rows = data.get("data", [])

        if isinstance(rows, list):
            return rows

    return []

def latest(rows, fields):

    values = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        for field in fields:

            dt = stamp(row.get(field))

            if dt is not None:
                values.append(dt)
                break

    return max(values) if values else None

def nse_list(name, endpoint, params=None):

    data, ms = fetch(
        "NSE",
        name,
        NSE + endpoint,
        params=params
    )

    if data is None:
        return []

    rows = [
        x for x in rows_from(data)
        if isinstance(x, dict)
    ]

    dt = latest(
        rows,
        [
            "an_dt",
            "sort_date",
            "broadcastDateTime",
            "timestamp",
            "date"
        ]
    )

    add(
        "NSE",
        name,
        "DATA RECEIVED" if rows
        else "EMPTY / UNVERIFIED",
        200,
        ms,
        len(rows),
        dt,
        "Completeness unverified"
    )

    payloads[name] = rows

    return rows

def nse_quote():

    data, ms = fetch(
        "NSE",
        "Direct Equity Quote",
        NSE + "/api/quote-equity",
        {"symbol": symbol}
    )

    if not isinstance(data, dict):
        return None, None

    price = number(
        data.get(
            "priceInfo", {}
        ).get("lastPrice")
    )

    dt = data.get(
        "metadata", {}
    ).get("lastUpdateTime")

    add(
        "NSE",
        "Direct Equity Quote",
        "DATA RECEIVED"
        if price is not None and price > 0
        else "INVALID DATA",
        200,
        ms,
        int(price is not None),
        dt,
        f"LTP {price}"
    )

    return price, dt

def nse_chain():

    data, ms = fetch(
        "NSE",
        "Direct NIFTY Option Chain",
        NSE + "/api/option-chain-indices",
        {"symbol": "NIFTY"}
    )

    if not isinstance(data, dict):
        return

    record = data.get("records", {})
    rows = record.get("data", [])

    add(
        "NSE",
        "Direct NIFTY Option Chain",
        "DATA RECEIVED"
        if rows else "INVALID DATA",
        200,
        ms,
        len(rows),
        record.get("timestamp")
    )

def archive(name, url, required):

    r, ms, detail = request(
        url,
        timeout=25
    )

    if r is None or r.status_code != 200:

        code = (
            r.status_code
            if r is not None else "-"
        )

        add(
            "NSE ARCHIVE",
            name,
            "FAILED",
            code,
            ms,
            note=(
                http_reason(code)
                if r is not None else detail
            ),
            final_url=display_url(r.url)
            if r is not None else ""
        )

        return

    try:

        if r.content[:2] == b"PK":

            with zipfile.ZipFile(
                io.BytesIO(r.content)
            ) as z:

                members = [
                    m for m in z.infolist()
                    if m.filename.lower()
                    .endswith(".csv")
                ]

                if not members:
                    raise ValueError("No CSV")

                if members[0].file_size > 100_000_000:
                    raise ValueError("Large archive")

                with z.open(members[0]) as f:

                    df = pd.read_csv(
                        f,
                        low_memory=False
                    )

        else:

            df = pd.read_csv(
                io.BytesIO(r.content),
                low_memory=False
            )

        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

        columns = {
            c.upper() for c in df.columns
        }

        if not set(required).issubset(columns):
            raise ValueError("Schema mismatch")

        date_note = "Report date unverified"

        date_column = next(
            (
                c for c in df.columns
                if c.upper() == "TRADDT"
            ),
            None
        )

        if date_column:

            dates = pd.to_datetime(
                df[date_column],
                errors="coerce"
            ).dt.date

            if (
                len(df) > 0
                and dates.eq(report_date).all()
            ):
                date_note = "Report date matches"
            else:
                date_note = "DATE MISMATCH"

        reports[name] = df

        add(
            "NSE ARCHIVE",
            name,
            "REPORT RECEIVED"
            if len(df) else "EMPTY REPORT",
            200,
            ms,
            len(df),
            note=date_note + " | " + detail,
            final_url=display_url(r.url)
        )

    except Exception as e:

        add(
            "NSE ARCHIVE",
            name,
            "INVALID REPORT",
            200,
            ms,
            note=str(e),
            final_url=display_url(r.url)
        )

def mcp_decode(r):

    try:

        value = r.json()

        return value if isinstance(
            value, dict
        ) else {}

    except Exception:

        for line in r.text.splitlines():

            if line.startswith("data:"):

                try:
                    value = json.loads(
                        line[5:].strip()
                    )

                    if isinstance(value, dict):
                        return value

                except Exception:
                    pass

    return {}

def mcp_connect(name, url):

    h = {
        "Content-Type": "application/json",
        "Accept": (
            "application/json, text/event-stream"
        )
    }

    r, ms, detail = request(
        url,
        method="POST",
        headers=h,
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "fno-research",
                    "version": "3.0"
                }
            }
        }
    )

    if r is None or r.status_code != 200:

        add(
            "NSE MCP",
            name,
            "FAILED",
            r.status_code if r is not None else "-",
            ms,
            note=detail
        )

        return None, []

    data = mcp_decode(r)
    result = data.get("result")

    if not isinstance(result, dict):

        add(
            "NSE MCP",
            name,
            "INVALID HANDSHAKE",
            200,
            ms
        )

        return None, []

    sid = r.headers.get("Mcp-Session-Id")

    if sid:
        h["Mcp-Session-Id"] = sid

    h["MCP-Protocol-Version"] = result.get(
        "protocolVersion",
        "2025-03-26"
    )

    add(
        "NSE MCP",
        name + " Initialize",
        "HANDSHAKE OK",
        200,
        ms
    )

    r, ms, detail = request(
        url,
        method="POST",
        headers=h,
        body={
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
    )

    if (
        r is None
        or r.status_code not in (200, 202, 204)
    ):

        add(
            "NSE MCP",
            name + " Notification",
            "FAILED"
        )

        return None, []

    all_tools = []
    cursor = None
    complete = False

    for page in range(5):

        params = (
            {"cursor": cursor}
            if cursor else {}
        )

        r, ms, detail = request(
            url,
            method="POST",
            headers=h,
            body={
                "jsonrpc": "2.0",
                "id": page + 2,
                "method": "tools/list",
                "params": params
            }
        )

        if r is None or r.status_code != 200:
            break

        result = mcp_decode(r).get(
            "result", {}
        )

        all_tools.extend(
            result.get("tools", [])
        )

        cursor = result.get("nextCursor")

        if not cursor:
            complete = True
            break

    payloads[name + " Tools"] = all_tools

    add(
        "NSE MCP",
        name + " Tools",
        "TOOLS DISCOVERED"
        if all_tools else "FAILED",
        records=len(all_tools),
        note=(
            "Discovery complete"
            if complete else "Partial discovery"
        )
    )

    return h, all_tools

def load_master():

    url = (
        "https://assets.upstox.com/"
        "market-quote/instruments/"
        "exchange/NSE.json.gz"
    )

    r, ms, detail = request(
        url,
        timeout=30
    )

    if r is None or r.status_code != 200:

        add(
            "UPSTOX",
            "Instrument Master",
            "FAILED",
            r.status_code if r is not None else "-",
            ms
        )

        return {}

    try:

        raw = (
            gzip.decompress(r.content)
            if r.content[:2] == b"\x1f\x8b"
            else r.content
        )

        instruments = json.loads(raw)

        option_rows = [
            x for x in instruments
            if x.get("segment") == "NSE_FO"
            and x.get("instrument_type")
            in ("CE", "PE")
        ]

        underlying = {
            x.get("underlying_key")
            for x in option_rows
        }

        equities = {
            x["instrument_key"]: {
                "symbol": x.get("trading_symbol"),
                "name": x.get("name", "")
            }
            for x in instruments
            if x.get("segment") == "NSE_EQ"
            and x.get("instrument_key")
            in underlying
        }

        add(
            "UPSTOX",
            "F&O Instrument Master",
            "DATA RECEIVED",
            200,
            ms,
            len(equities),
            note=(
                f"Option contracts "
                f"{len(option_rows)}"
            )
        )

        return equities

    except Exception:

        add(
            "UPSTOX",
            "Instrument Master",
            "INVALID DATA"
        )

        return {}

def get_quotes(keys, auth, name):

    data, ms = fetch(
        "UPSTOX",
        name,
        UP + "/v2/market-quote/quotes",
        {
            "instrument_key": ",".join(keys)
        },
        auth
    )

    if not isinstance(data, dict):
        return {}

    rows = data.get("data", {})

    if (
        data.get("status") != "success"
        or not isinstance(rows, dict)
    ):

        add(
            "UPSTOX",
            name,
            "INVALID DATA",
            200,
            ms
        )

        return {}

    found = {}

    checked = now()

    for row in rows.values():

        if not isinstance(row, dict):
            continue

        key = row.get("instrument_token")
        price = number(row.get("last_price"))

        if (
            key not in keys
            or price is None
            or price <= 0
        ):
            continue

        found[key] = row

        last_trade = row.get("last_trade_time")

        a = age(
            last_trade,
            checked
        )

        quote_rows.append({
            "Instrument": key,
            "LTP": price,
            "Last Trade IST": str(
                stamp(last_trade)
            ),
            "Last Trade Age Seconds": a,
            "Within 5 Seconds": (
                a is not None
                and 0 <= a <= 5
            ),
            "Response ms": ms,
            "Checked IST": str(checked)
        })

    add(
        "UPSTOX",
        name,
        "DATA RECEIVED" if found
        else "EMPTY",
        200,
        ms,
        len(found),
        note=f"{len(found)}/{len(keys)}"
    )

    return found

def option_chain(key, auth):

    data, ms = fetch(
        "UPSTOX",
        "Option Contracts",
        UP + "/v2/option/contract",
        {"instrument_key": key},
        auth
    )

    if not isinstance(data, dict):
        return

    rows = rows_from(data)

    add(
        "UPSTOX",
        "Option Contracts",
        "DATA RECEIVED" if rows else "EMPTY",
        200,
        ms,
        len(rows)
    )

    expiries = sorted({
        x.get("expiry")
        for x in rows
        if x.get("expiry")
        and x["expiry"] >= str(now().date())
    })

    if not expiries:
        return

    data, ms = fetch(
        "UPSTOX",
        "Option Chain",
        UP + "/v2/option/chain",
        {
            "instrument_key": key,
            "expiry_date": expiries[0]
        },
        auth
    )

    if not isinstance(data, dict):
        return

    rows = rows_from(data)

    legs = 0
    valid = 0
    oi_valid = 0
    quote_valid = 0

    for row in rows:

        for side in (
            "call_options",
            "put_options"
        ):

            leg = row.get(side) or {}

            if not leg:
                continue

            legs += 1

            market = leg.get("market_data") or {}
            g = leg.get("option_greeks") or {}

            delta = number(g.get("delta"))
            iv = number(g.get("iv"))
            gamma = number(g.get("gamma"))
            vega = number(g.get("vega"))
            theta = number(g.get("theta"))

            if (
                delta is not None
                and abs(delta) <= 1
                and (
                    delta >= 0
                    if side == "call_options"
                    else delta <= 0
                )
                and iv is not None
                and iv > 0
                and gamma is not None
                and gamma >= 0
                and vega is not None
                and vega >= 0
                and theta is not None
            ):

                valid += 1

            oi = number(market.get("oi"))

            if oi is not None and oi >= 0:
                oi_valid += 1

            bid = number(
                market.get("bid_price")
            )

            ask = number(
                market.get("ask_price")
            )

            if (
                bid is not None
                and ask is not None
                and 0 < bid <= ask
            ):
                quote_valid += 1

    payloads["Upstox Option Chain"] = rows

    add(
        "UPSTOX",
        "Option Chain Quality",
        "DATA RECEIVED" if rows else "EMPTY",
        200,
        ms,
        len(rows),
        note=(
            f"Expiry {expiries[0]} | "
            f"Numeric Greeks {valid}/{legs} | "
            f"OI {oi_valid}/{legs} | "
            f"Bid Ask {quote_valid}/{legs} | "
            "Feed timestamp unavailable"
        )
    )

def cross_verify(nse_price, nse_time, quote):

    if (
        nse_price is None
        or nse_price <= 0
        or not quote
    ):

        add(
            "CROSS CHECK",
            "NSE vs Upstox Price",
            "UNAVAILABLE",
            note="Both sources required"
        )

        return

    a = stamp(nse_time)

    b = stamp(
        quote.get("last_trade_time")
    )

    p = number(quote.get("last_price"))

    if (
        a is None
        or b is None
        or p is None
        or p <= 0
    ):

        add(
            "CROSS CHECK",
            "NSE vs Upstox Price",
            "UNVERIFIED"
        )

        return

    gap = abs((a - b).total_seconds())

    difference = (
        abs(nse_price - p)
        / nse_price * 100
    )

    add(
        "CROSS CHECK",
        "NSE vs Upstox Price",
        "COMPARABLE SAMPLE"
        if gap <= 2
        else "NOT SYNCHRONIZED",
        note=(
            f"NSE {nse_price} | "
            f"Upstox {p} | "
            f"Difference {difference:.4f}% | "
            f"Time gap {gap:.2f}s"
        )
    )

def normalize(text):

    text = str(text or "").lower()

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    ).strip()

ALIASES = {
    "LT": ["larsen and toubro", "l&t"],
    "DIXON": ["dixon technologies", "dixon"],
    "INFY": ["infosys"],
    "TCS": ["tata consultancy services", "tcs"],
    "RELIANCE": ["reliance industries"],
    "SBIN": ["state bank of india", "sbi"],
    "HDFCBANK": ["hdfc bank"],
    "ICICIBANK": ["icici bank"],
    "POLICYBZR": ["pb fintech", "policybazaar"],
    "APLAPOLLO": ["apl apollo"],
    "CIPLA": ["cipla"],
    "MARUTI": ["maruti suzuki"],
    "HYUNDAI": ["hyundai motor india"],
    "TATASTEEL": ["tata steel"],
    "SUNPHARMA": ["sun pharmaceutical"],
    "ZYDUSLIFE": ["zydus lifesciences"]
}

def company_relevance(
    name,
    sym,
    heading,
    summary
):

    aliases = list(
        ALIASES.get(sym, [])
    )

    company = normalize(name)

    company = re.sub(
        r"\b(limited|ltd|private|pvt)\b",
        "",
        company
    ).strip()

    if len(company) >= 5:
        aliases.append(company)

    if len(sym) >= 4:
        aliases.append(sym)

    title = normalize(heading)
    body = normalize(summary)

    for alias in aliases:

        alias = normalize(alias)

        if len(alias) < 3:
            continue

        pattern = (
            r"\b"
            + re.escape(alias)
            + r"\b"
        )

        if re.search(pattern, title):

            return "TITLE MATCH", alias

    for alias in aliases:

        alias = normalize(alias)

        if len(alias) < 3:
            continue

        if re.search(
            r"\b" + re.escape(alias) + r"\b",
            body
        ):

            return "BODY MATCH", alias

    return "UNCONFIRMED MAPPING", ""

EVENTS = {
    "ORDER / CONTRACT": [
        "wins order",
        "bagged order",
        "new order",
        "contract awarded",
        "order win"
    ],
    "REGULATORY": [
        "sebi",
        "rbi",
        "usfda",
        "regulatory",
        "approval",
        "penalty"
    ],
    "EARNINGS": [
        "quarterly results",
        "net profit",
        "revenue",
        "earnings",
        "margin"
    ],
    "DEBT": [
        "debt",
        "default",
        "insolvency",
        "credit rating"
    ],
    "CORPORATE ACTION": [
        "buyback",
        "dividend",
        "stock split",
        "merger",
        "acquisition"
    ],
    "PRICE MOVEMENT": [
        "shares rise",
        "shares fall",
        "stock jumps",
        "stock falls",
        "52 week high",
        "52 week low"
    ]
}

def event_type(heading, summary):

    content = normalize(
        str(heading or "")
        + " "
        + str(summary or "")
    )

    matched = []

    for event, terms in EVENTS.items():

        if any(
            normalize(term) in content
            for term in terms
        ):

            matched.append(event)

    return (
        " | ".join(matched)
        if matched else "UNCLASSIFIED"
    )

def news_test(equities, keys, auth):

    successful = 0
    completed = 0
    attempted = 0

    for start in range(0, len(keys), 30):

        batch = keys[start:start + 30]
        attempted += 1

        batch_ok = False

        for page in range(
            1, int(news_pages) + 1
        ):

            name = (
                f"Batch {attempted} "
                f"Page {page}"
            )

            data, ms = fetch(
                "UPSTOX NEWS",
                name,
                UP + "/v2/news",
                {
                    "category": "instrument_keys",
                    "instrument_keys": ",".join(batch),
                    "page_number": page,
                    "page_size": 100
                },
                auth
            )

            if not isinstance(data, dict):
                break

            mapping = data.get("data", {})

            if (
                data.get("status") != "success"
                or not isinstance(mapping, dict)
            ):

                add(
                    "UPSTOX NEWS",
                    name,
                    "INVALID RESPONSE"
                )

                break

            batch_ok = True

            count = 0
            dates = []

            for key, articles in mapping.items():

                if (
                    key not in batch
                    or not isinstance(articles, list)
                ):
                    continue

                instrument = equities.get(key, {})

                sym = instrument.get(
                    "symbol", key
                )

                company = instrument.get(
                    "name", ""
                )

                for article in articles:

                    if not isinstance(article, dict):
                        continue

                    heading = article.get(
                        "heading", ""
                    )

                    summary = article.get(
                        "summary", ""
                    )

                    published = article.get(
                        "published_time"
                    )

                    dt = stamp(published)

                    if dt is not None:
                        dates.append(dt)

                    relevance, matched = (
                        company_relevance(
                            company,
                            sym,
                            heading,
                            summary
                        )
                    )

                    news_rows.append({
                        "Symbol": sym,
                        "Instrument": key,
                        "Heading": heading,
                        "Summary": summary,
                        "Published IST": str(dt),
                        "Age Seconds": age(published),
                        "URL": article.get(
                            "article_link", ""
                        ),
                        "Company Relevance": relevance,
                        "Matched Alias": matched,
                        "Event Type": event_type(
                            heading,
                            summary
                        ),
                        "Directional Impact": (
                            "REVIEW REQUIRED"
                        )
                    })

                    count += 1

            metadata = data.get(
                "metadata", {}
            )

            pages = metadata.get(
                "page", {}
            ).get("total_pages")

            add(
                "UPSTOX NEWS",
                name,
                "DATA RECEIVED"
                if count else "NO ARTICLES",
                200,
                ms,
                count,
                max(dates) if dates else None,
                f"Reported pages {pages}"
            )

            if pages is None:
                break

            try:

                if page >= int(pages):
                    completed += 1
                    break

            except Exception:
                break

            time.sleep(0.3)

        if batch_ok:
            successful += len(batch)

        time.sleep(0.3)

    add(
        "UPSTOX NEWS",
        "News Coverage",
        "COMPLETE REQUESTS"
        if completed == attempted
        else "PARTIAL / UNVERIFIED",
        records=len(news_rows),
        note=(
            f"Successful request keys "
            f"{successful}/{len(keys)} | "
            f"Completed batches "
            f"{completed}/{attempted}"
        )
    )

def websocket_test(keys, auth):

    try:

        import websocket

        from upstox_client.feeder.proto \
            import MarketDataFeedV3_pb2 as pb

    except ImportError:

        add(
            "WEBSOCKET",
            "Live Stream",
            "DEPENDENCY MISSING",
            note=(
                "Install websocket-client "
                "and current upstox-python-sdk"
            )
        )

        return

    data, ms = fetch(
        "UPSTOX",
        "WebSocket Authorization",
        UP + "/v3/feed/market-data-feed/authorize",
        headers=auth
    )

    if not isinstance(data, dict):
        return

    uri = data.get(
        "data", {}
    ).get(
        "authorized_redirect_uri"
    )

    if not uri:

        add(
            "WEBSOCKET",
            "Authorization",
            "INVALID RESPONSE"
        )

        return

    parsed = urlparse(uri)

    host = parsed.hostname or ""

    if not (
        parsed.scheme == "wss"
        and (
            host == "upstox.com"
            or host.endswith(".upstox.com")
        )
        and not parsed.username
        and not parsed.password
    ):

        add(
            "WEBSOCKET",
            "Authorization",
            "UNAPPROVED DESTINATION"
        )

        return

    ws = None

    packets = 0
    live_packets = 0
    received = set()
    live_keys = set()

    try:

        started = time.perf_counter()

        ws = websocket.create_connection(
            uri,
            timeout=5
        )

        connected_ms = round(
            (
                time.perf_counter()
                - started
            ) * 1000
        )

        ws.send_binary(
            json.dumps({
                "guid": str(uuid.uuid4()),
                "method": "sub",
                "data": {
                    "mode": "ltpc",
                    "instrumentKeys": keys
                }
            }).encode("utf-8")
        )

        end = (
            time.monotonic()
            + ws_seconds
        )

        while time.monotonic() < end:

            ws.settimeout(
                min(
                    2,
                    max(
                        0.1,
                        end - time.monotonic()
                    )
                )
            )

            try:
                raw = ws.recv()

            except websocket.WebSocketTimeoutException:
                continue

            checked = now()

            if not isinstance(
                raw, (bytes, bytearray)
            ):
                continue

            response = pb.FeedResponse()
            response.ParseFromString(raw)

            packets += 1

            packet_type = int(response.type)

            if packet_type == 1:
                live_packets += 1

            current_ts = int(
                response.currentTs
            )

            for key, feed in response.feeds.items():

                if key not in keys:
                    continue

                if not feed.HasField("ltpc"):
                    continue

                ltpc = feed.ltpc

                if ltpc.ltp <= 0:
                    continue

                received.add(key)

                if packet_type == 1:
                    live_keys.add(key)

                ws_rows.append({
                    "Instrument": key,
                    "Packet Type": packet_type,
                    "LTP": ltpc.ltp,
                    "Last Trade IST": str(
                        stamp(ltpc.ltt)
                    ),
                    "Last Trade Age Seconds": age(
                        ltpc.ltt,
                        checked
                    ),
                    "Feed Timestamp IST": str(
                        stamp(current_ts)
                        if current_ts else None
                    ),
                    "Feed Age Seconds": age(
                        current_ts,
                        checked
                    ) if current_ts else None,
                    "Received IST": str(checked)
                })

        add(
            "WEBSOCKET",
            "Live Stream Sample",
            "LIVE MESSAGES RECEIVED"
            if live_keys else "NO LIVE UPDATES OBSERVED",
            ms=connected_ms,
            records=len(ws_rows),
            note=(
                f"Packets {packets} | "
                f"Live packets {live_packets} | "
                f"Keys received "
                f"{len(received)}/{len(keys)} | "
                f"Live keys {len(live_keys)} | "
                f"Sample {ws_seconds}s"
            )
        )

    except Exception as e:

        add(
            "WEBSOCKET",
            "Live Stream Sample",
            "FAILED / PARTIAL",
            records=len(ws_rows),
            note=type(e).__name__
        )

    finally:

        if ws is not None:

            try:
                ws.close(timeout=1)
            except Exception:
                pass

st.subheader("Complete Source Validation")

if st.button(
    "RUN COMPLETE V3 DATA TEST",
    type="primary"
):

    status = st.empty()
    progress = st.progress(0)

    status.write("Testing NSE direct sources...")

    fetch(
        "NSE",
        "Official Website",
        NSE,
        expected="html"
    )

    nse_list(
        "Corporate Announcements",
        "/api/corporate-announcements",
        {"index": "equities"}
    )

    nse_price, nse_time = nse_quote()

    nse_chain()

    nse_list(
        "Pre Open Data",
        "/api/market-data-pre-open",
        {"key": "FO"}
    )

    nse_list(
        "Direct Bulk Deals",
        "/api/bulk-deals"
    )

    nse_list(
        "Direct Block Deals",
        "/api/block-deals"
    )

    progress.progress(20)

    status.write("Testing official NSE archives...")

    fetch(
        "NSE",
        "Official Reports",
        NSE + "/all-reports",
        expected="html"
    )

    fetch(
        "NSE",
        "Bulk Block Deals Page",
        NSE + "/report-detail/"
        "display-bulk-and-block-deals",
        expected="html"
    )

    fetch(
        "BSE",
        "Announcements Page",
        "https://www.bseindia.com/"
        "corporates/ann.html",
        expected="html"
    )

    d = report_date.strftime("%Y%m%d")
    d2 = report_date.strftime("%d%m%Y")

    archive(
        "NSE Delivery",
        ARC + "/products/content/"
        f"sec_bhavdata_full_{d2}.csv",
        ["SYMBOL", "DELIV_QTY"]
    )

    archive(
        "NSE Equity Bhavcopy",
        ARC + "/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d}_"
        "F_0000.csv.zip",
        ["TCKRSYMB", "TRADDT", "CLSPRIC"]
    )

    archive(
        "NSE FO Bhavcopy",
        ARC + "/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{d}_"
        "F_0000.csv.zip",
        ["TCKRSYMB", "TRADDT", "OPNINTRST"]
    )

    archive(
        "Bulk Deals Candidate",
        ARC + "/content/equities/"
        f"bulk_{d2}.csv",
        ["SYMBOL", "CLIENT NAME"]
    )

    archive(
        "Block Deals Candidate",
        ARC + "/content/equities/"
        f"block_{d2}.csv",
        ["SYMBOL", "CLIENT NAME"]
    )

    progress.progress(40)

    status.write("Discovering NSE MCP tools...")

    servers = {
        "Bhavcopy MCP": (
            "https://mcp.nseindia.in/"
            "bhavcopy/cm/mcp"
        ),
        "CM Market MCP": (
            "https://mcp.nseindia.in/"
            "cmmkt/mcp"
        )
    }

    for name, url in servers.items():

        mcp_connect(name, url)

    progress.progress(55)

    status.write("Loading F&O instruments...")

    equities = load_master()

    selected = next(
        (
            key for key, item in equities.items()
            if item["symbol"] == symbol
        ),
        None
    )

    if token:

        auth = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        status.write("Testing market quotes...")

        get_quotes(
            ["NSE_INDEX|Nifty 50"],
            auth,
            "NIFTY Quote"
        )

        if selected:

            quote = get_quotes(
                [selected],
                auth,
                "Selected Stock Quote"
            ).get(selected)

            cross_verify(
                nse_price,
                nse_time,
                quote
            )

            option_chain(
                selected,
                auth
            )

        if full_quotes:

            keys = list(equities)
            received = {}
            started = time.perf_counter()

            before = len(quote_rows)

            for i in range(0, len(keys), 40):

                received.update(
                    get_quotes(
                        keys[i:i + 40],
                        auth,
                        f"Quote Batch {i // 40 + 1}"
                    )
                )

                time.sleep(0.3)

            sample = quote_rows[before:]

            fresh = sum(
                x["Within 5 Seconds"]
                for x in sample
            )

            add(
                "UPSTOX",
                "Full F&O Coverage",
                "COMPLETE"
                if keys and len(received) == len(keys)
                else "PARTIAL",
                ms=round(
                    (
                        time.perf_counter()
                        - started
                    ) * 1000
                ),
                records=len(received),
                note=(
                    f"{len(received)}/{len(keys)} | "
                    f"Fresh within 5s {fresh}"
                )
            )

        progress.progress(70)

        status.write("Testing stock news...")

        keys = (
            list(equities)
            if full_news
            else ([selected] if selected else [])
        )

        if keys:

            news_test(
                equities,
                keys,
                auth
            )

        progress.progress(85)

        if enable_ws:

            status.write(
                "Testing live WebSocket..."
            )

            keys = (
                list(equities)
                if full_quotes
                else ([selected] if selected else [])
            )

            keys = list(dict.fromkeys(
                ["NSE_INDEX|Nifty 50"] + keys
            ))

            websocket_test(
                keys,
                auth
            )

    else:

        add(
            "UPSTOX",
            "Authenticated Tests",
            "SKIPPED",
            note="Access token required"
        )

    ndf = pd.DataFrame(news_rows)

    if not ndf.empty:

        ndf["Article ID"] = ndf.apply(
            lambda x: (
                str(x["URL"]).split("?")[0]
                if str(x["URL"]).startswith("https://")
                else normalize(x["Heading"])
                + "|" + str(x["Published IST"])
            ),
            axis=1
        )

        ndf = ndf.drop_duplicates(
            subset=[
                "Instrument",
                "Article ID"
            ]
        )

        ndf = ndf.sort_values(
            "Age Seconds",
            ascending=True
        )

    st.session_state["v3_results"] = (
        pd.DataFrame(results)
    )

    st.session_state["v3_news"] = ndf

    st.session_state["v3_quotes"] = (
        pd.DataFrame(quote_rows)
    )

    st.session_state["v3_ws"] = (
        pd.DataFrame(ws_rows)
    )

    st.session_state["v3_payloads"] = payloads

    st.session_state["v3_reports"] = reports

    progress.progress(100)

    status.success(
        "V3 Data Connectivity Test Completed"
    )

if "v3_results" in st.session_state:

    df = st.session_state["v3_results"]

    st.divider()

    st.header("V3 DATA HEALTH REPORT")

    a, b, c = st.columns(3)

    a.metric("Total Tests", len(df))

    b.metric(
        "Data Responses",
        int(
            df["Status"].isin([
                "DATA RECEIVED",
                "REPORT RECEIVED",
                "TOOLS DISCOVERED",
                "COMPLETE",
                "COMPLETE REQUESTS",
                "LIVE MESSAGES RECEIVED"
            ]).sum()
        )
    )

    c.metric(
        "Failed Requests",
        int((df["Status"] == "FAILED").sum())
    )

    st.dataframe(
        df,
        use_container_width=True
    )

    st.download_button(
        "DOWNLOAD V3 TEST REPORT",
        df.to_csv(index=False),
        "data_connectivity_v3.csv",
        "text/csv"
    )

    st.header("HTTP ERROR DIAGNOSTICS")

    errors = df[
        df["HTTP"].astype(str).isin([
            "301", "302", "400", "401",
            "403", "404", "429", "500",
            "502", "503", "504"
        ])
    ]

    st.dataframe(
        errors,
        use_container_width=True
    )

    st.header("NSE ANNOUNCEMENTS")

    announcements = st.session_state[
        "v3_payloads"
    ].get(
        "Corporate Announcements", []
    )

    if announcements:

        adf = pd.DataFrame(
            announcements
        )

        st.dataframe(
            adf,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD NSE ANNOUNCEMENTS",
            adf.to_csv(index=False),
            "nse_announcements_v3.csv",
            "text/csv"
        )

    else:

        st.warning(
            "NSE announcements unavailable"
        )

    st.header("COMPANY NEWS INTELLIGENCE")

    ndf = st.session_state["v3_news"]

    if not ndf.empty:

        a, b, c = st.columns(3)

        a.metric("Stock News", len(ndf))

        b.metric(
            "Unique Articles",
            ndf["Article ID"].nunique()
        )

        c.metric(
            "Stocks With News",
            ndf["Symbol"].nunique()
        )

        view = st.radio(
            "News Filter",
            [
                "All News",
                "Title Match",
                "Body Match",
                "Unconfirmed Mapping"
            ],
            horizontal=True
        )

        filtered = ndf.copy()

        if view != "All News":

            filtered = filtered[
                filtered["Company Relevance"]
                == view.upper()
            ]

        st.dataframe(
            filtered,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD V3 NEWS INTELLIGENCE",
            ndf.to_csv(index=False),
            "news_intelligence_v3.csv",
            "text/csv"
        )

        st.warning(
            "Company name matching is heuristic. "
            "It does not establish directional impact."
        )

    else:

        st.info("No validated news records")

    st.header("QUOTE FRESHNESS")

    qdf = st.session_state["v3_quotes"]

    if not qdf.empty:

        st.dataframe(
            qdf,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD V3 QUOTE FRESHNESS",
            qdf.to_csv(index=False),
            "quote_freshness_v3.csv",
            "text/csv"
        )

    st.header("LIVE WEBSOCKET QUALITY")

    wdf = st.session_state["v3_ws"]

    if not wdf.empty:

        a, b = st.columns(2)

        a.metric(
            "Instruments Received",
            wdf["Instrument"].nunique()
        )

        b.metric(
            "Instrument Update Records",
            len(wdf)
        )

        ages = pd.to_numeric(
            wdf["Feed Age Seconds"],
            errors="coerce"
        ).dropna()

        if not ages.empty:

            st.metric(
                "Median Feed Timestamp Age",
                f"{ages.median():.3f} sec"
            )

        st.dataframe(
            wdf.tail(200),
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD WEBSOCKET SAMPLE",
            wdf.to_csv(index=False),
            "websocket_sample_v3.csv",
            "text/csv"
        )

    else:

        st.info(
            "No decoded WebSocket data available"
        )

    st.header("OFFICIAL NSE REPORTS")

    for name, frame in st.session_state[
        "v3_reports"
    ].items():

        with st.expander(name):

            st.write("Records:", len(frame))

            st.dataframe(
                frame.head(50),
                use_container_width=True
            )

            st.download_button(
                "DOWNLOAD " + name,
                frame.to_csv(index=False),
                name.replace(" ", "_") + ".csv",
                "text/csv",
                key="report_" + name
            )

    st.header("MCP TOOL DISCOVERY")

    for name, data in st.session_state[
        "v3_payloads"
    ].items():

        if not name.endswith(" Tools"):
            continue

        with st.expander(name):

            st.json(data)

            st.download_button(
                "DOWNLOAD TOOL SCHEMAS",
                json.dumps(
                    data,
                    indent=2,
                    default=str
                ),
                name.replace(" ", "_") + ".json",
                "application/json",
                key="mcp_" + name
            )

    st.warning(
        "Feed timestamp age is not pure network "
        "latency. Market closure and clock differences "
        "can affect freshness. Bulk/block archive "
        "paths remain candidates. MCP queries require "
        "inspection of discovered tool schemas."
    )

st.divider()

st.caption(
    "DATA CONNECTIVITY V3 | READ ONLY | "
    "NO AUTOMATIC ORDERS"
)