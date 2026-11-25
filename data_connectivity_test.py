import streamlit as st
import pandas as pd
import requests
import gzip
import json
import time
import io
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

st.set_page_config(
    page_title="F&O DATA CONNECTIVITY V2",
    page_icon="📡",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
NSE = "https://www.nseindia.com"
ARCHIVE = "https://nsearchives.nseindia.com"
UP = "https://api.upstox.com"

HEADERS = {
    "User-Agent": "FNO-Data-Research/2.0",
    "Accept": "application/json, text/html, */*"
}

st.title("F&O DATA CONNECTIVITY TEST V2")
st.caption(
    "NSE DIRECT | OFFICIAL REPORTS | MCP | "
    "UPSTOX NEWS | DATA VALIDATION"
)

st.info(
    "Read-only research. No automatic orders. "
    "HTTP success does not prove data accuracy."
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

max_news_pages = st.sidebar.number_input(
    "Maximum News Pages Per Batch",
    min_value=1,
    max_value=10,
    value=3
)

results = []
payloads = {}
news_records = []
quote_records = []
report_frames = {}

def now():
    return datetime.now(IST)

def parse_stamp(value):
    if value is None or value == "":
        return None

    try:
        if isinstance(value, (int, float)) or (
            isinstance(value, str)
            and value.isdigit()
        ):
            v = float(value)
            unit = "ms" if abs(v) > 1e11 else "s"

            dt = pd.to_datetime(
                v, unit=unit, utc=True
            )

        else:
            dt = pd.to_datetime(
                value,
                dayfirst=True
            )

        if pd.isna(dt):
            return None

        if dt.tzinfo is None:
            dt = dt.tz_localize(IST)
        else:
            dt = dt.tz_convert(IST)

        return dt

    except Exception:
        return None

def age_seconds(value, checked=None):
    dt = parse_stamp(value)

    if dt is None:
        return None

    checked = checked or now()

    return round(
        (checked - dt.to_pydatetime())
        .total_seconds(),
        2
    )

def freshness(value):
    dt = parse_stamp(value)

    if dt is None:
        return "TIMESTAMP UNAVAILABLE"

    age = age_seconds(value)

    return (
        f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"| Age {age} sec"
    )

def http_reason(code):
    meanings = {
        400: "Invalid request or parameters",
        401: "Authentication required or expired",
        403: "Access refused; authorized source needed",
        404: "Endpoint or dated report not found",
        429: "Rate limit; reduce request frequency",
        500: "Source server error",
        502: "Gateway error",
        503: "Service temporarily unavailable",
        504: "Gateway timeout"
    }

    return meanings.get(
        code,
        f"HTTP {code}"
    )

def add_result(
    source,
    name,
    state,
    code="-",
    ms=0,
    count=0,
    stamp=None,
    note=""
):
    results.append({
        "Source": source,
        "Data": name,
        "Status": state,
        "HTTP": code,
        "Response ms": ms,
        "Records": count,
        "Freshness": freshness(stamp),
        "Remarks": note,
        "Checked IST": now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    })

def request_url(
    url,
    params=None,
    headers=None,
    method="GET",
    body=None,
    timeout=15
):
    start = time.perf_counter()

    try:
        response = None

        for attempt in range(2):

            response = requests.request(
                method,
                url,
                params=params,
                headers=headers or HEADERS,
                json=body,
                timeout=timeout,
                allow_redirects=False
            )

            if response.status_code not in (
                429, 500, 502, 503, 504
            ):
                break

            if attempt == 0:
                wait = response.headers.get(
                    "Retry-After", "2"
                )

                try:
                    wait = float(wait)
                except Exception:
                    wait = 2

                if wait > 10:
                    break

                time.sleep(max(1, wait))

        ms = round(
            (time.perf_counter() - start)
            * 1000
        )

        return response, ms, ""

    except requests.RequestException as e:

        return (
            None,
            round(
                (time.perf_counter() - start)
                * 1000
            ),
            type(e).__name__
        )

def failed(source, name, response, ms, error):
    code = (
        response.status_code
        if response is not None
        else "-"
    )

    note = (
        http_reason(code)
        if response is not None
        else error
    )

    add_result(
        source,
        name,
        "FAILED",
        code,
        ms,
        note=note
    )

def get_json(
    source,
    name,
    url,
    params=None,
    headers=None
):
    r, ms, err = request_url(
        url,
        params=params,
        headers=headers
    )

    if r is None or r.status_code != 200:
        failed(source, name, r, ms, err)
        return None, ms

    try:
        return r.json(), ms

    except Exception:
        add_result(
            source,
            name,
            "INVALID RESPONSE",
            200,
            ms,
            note="Response is not valid JSON"
        )

        return None, ms

def records_from(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        rows = data.get("data", [])

        if isinstance(rows, list):
            return rows

    return []

def latest_stamp(rows, fields):
    stamps = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        for field in fields:

            dt = parse_stamp(row.get(field))

            if dt is not None:
                stamps.append(dt)
                break

    return max(stamps) if stamps else None

def test_nse_list(
    name,
    endpoint,
    params=None
):
    data, ms = get_json(
        "NSE",
        name,
        NSE + endpoint,
        params=params
    )

    if data is None:
        return []

    rows = [
        x for x in records_from(data)
        if isinstance(x, dict)
    ]

    stamp = latest_stamp(
        rows,
        [
            "an_dt",
            "sort_date",
            "broadcastDateTime",
            "timestamp",
            "date"
        ]
    )

    add_result(
        "NSE",
        name,
        "DATA RECEIVED" if rows
        else "EMPTY / UNVERIFIED",
        200,
        ms,
        len(rows),
        stamp,
        "Completeness not established"
    )

    payloads[name] = rows[:20]

    return rows

def test_page(source, name, url):
    r, ms, err = request_url(url)

    if r is None or r.status_code != 200:
        failed(source, name, r, ms, err)
        return

    add_result(
        source,
        name,
        "PAGE REACHABLE",
        200,
        ms,
        note="Page only; records not extracted"
    )

def test_nse_quote():
    name = "NSE Equity Quote"

    data, ms = get_json(
        "NSE",
        name,
        NSE + "/api/quote-equity",
        params={"symbol": symbol}
    )

    if not isinstance(data, dict):
        return None, None

    price = data.get(
        "priceInfo", {}
    ).get("lastPrice")

    stamp = data.get(
        "metadata", {}
    ).get("lastUpdateTime")

    add_result(
        "NSE",
        name,
        "DATA RECEIVED"
        if price is not None
        else "INVALID DATA",
        200,
        ms,
        int(price is not None),
        stamp,
        f"LTP: {price}"
    )

    return price, stamp

def test_nse_chain():
    name = "NSE NIFTY Option Chain"

    data, ms = get_json(
        "NSE",
        name,
        NSE + "/api/option-chain-indices",
        params={"symbol": "NIFTY"}
    )

    if not isinstance(data, dict):
        return

    records = data.get("records", {})
    rows = records.get("data", [])

    add_result(
        "NSE",
        name,
        "DATA RECEIVED" if rows
        else "INVALID DATA",
        200,
        ms,
        len(rows),
        records.get("timestamp"),
        "Chain completeness not established"
    )

def read_report(content):

    if content[:2] == b"PK":

        with zipfile.ZipFile(
            io.BytesIO(content)
        ) as z:

            members = [
                x for x in z.infolist()
                if x.filename.lower().endswith(".csv")
            ]

            if not members:
                raise ValueError("No CSV in archive")

            if members[0].file_size > 100_000_000:
                raise ValueError("Archive too large")

            with z.open(members[0]) as file:
                return pd.read_csv(
                    file,
                    low_memory=False
                )

    return pd.read_csv(
        io.BytesIO(content),
        low_memory=False
    )

def validate_report(df, report_type):

    columns = {
        str(c).strip().upper()
        for c in df.columns
    }

    if df.empty:
        return False

    if report_type == "DELIVERY":

        return (
            bool(
                columns.intersection({
                    "SYMBOL", "TCKRSYMB"
                })
            )
            and bool(
                columns.intersection({
                    "DELIV_QTY",
                    "DELIV_PER",
                    "DELIVERY QTY"
                })
            )
        )

    if report_type == "BULK_BLOCK":

        return (
            bool(
                columns.intersection({
                    "SYMBOL", "TCKRSYMB"
                })
            )
            and any(
                "CLIENT" in c
                or "QUANTITY" in c
                for c in columns
            )
        )

    if report_type == "EQUITY":

        return (
            "TCKRSYMB" in columns
            and "CLSPRIC" in columns
            and "TRADDT" in columns
        )

    if report_type == "FO":

        return (
            "TCKRSYMB" in columns
            and "OPNINTRST" in columns
            and "TRADDT" in columns
        )

    return False

def test_archive(name, url, report_type):

    r, ms, err = request_url(
        url,
        timeout=30
    )

    if r is None or r.status_code != 200:
        failed(
            "NSE ARCHIVE",
            name,
            r,
            ms,
            err
        )
        return

    try:
        df = read_report(r.content)

        if not validate_report(
            df, report_type
        ):
            raise ValueError(
                "Unexpected report schema"
            )

        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

        date_col = next(
            (
                c for c in df.columns
                if c.upper() == "TRADDT"
            ),
            None
        )

        date_note = "Report date unverified"

        if date_col:

            dates = pd.to_datetime(
                df[date_col],
                errors="coerce"
            ).dt.date.dropna()

            if (
                len(dates) == len(df)
                and dates.eq(report_date).all()
            ):
                date_note = "Report date matches"

            else:
                date_note = "DATE MISMATCH OR MISSING"

        report_frames[name] = df

        add_result(
            "NSE ARCHIVE",
            name,
            "REPORT RECEIVED",
            200,
            ms,
            len(df),
            note=(
                f"{date_note} | "
                f"Columns: {len(df.columns)}"
            )
        )

    except Exception as e:

        add_result(
            "NSE ARCHIVE",
            name,
            "INVALID REPORT",
            200,
            ms,
            note=type(e).__name__
        )

def mcp_decode(response):

    try:
        return response.json()

    except Exception:

        for line in response.text.splitlines():

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

def test_mcp(name, url):

    headers = {
        "Content-Type": "application/json",
        "Accept": (
            "application/json, text/event-stream"
        )
    }

    initial = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {
                "name": "fno-research",
                "version": "2.0"
            }
        }
    }

    r, ms, err = request_url(
        url,
        headers=headers,
        method="POST",
        body=initial
    )

    if r is None or r.status_code != 200:
        failed("NSE MCP", name, r, ms, err)
        return

    data = mcp_decode(r)

    if "result" not in data:

        add_result(
            "NSE MCP",
            name,
            "HANDSHAKE FAILED",
            200,
            ms
        )
        return

    session_id = r.headers.get(
        "Mcp-Session-Id"
    )

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    version = data["result"].get(
        "protocolVersion",
        "2025-03-26"
    )

    headers["MCP-Protocol-Version"] = version

    add_result(
        "NSE MCP",
        name + " Initialize",
        "HANDSHAKE OK",
        200,
        ms
    )

    init_r, init_ms, init_err = request_url(
        url,
        headers=headers,
        method="POST",
        body={
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
    )

    if (
        init_r is None
        or init_r.status_code not in (200, 202, 204)
    ):
        failed(
            "NSE MCP",
            name + " Notification",
            init_r,
            init_ms,
            init_err
        )
        return

    r, ms, err = request_url(
        url,
        headers=headers,
        method="POST",
        body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
    )

    if r is None or r.status_code != 200:
        failed(
            "NSE MCP",
            name + " Tools",
            r,
            ms,
            err
        )
        return

    data = mcp_decode(r)

    tools = data.get(
        "result", {}
    ).get("tools", [])

    payloads[name + " Tools"] = tools

    add_result(
        "NSE MCP",
        name + " Tools",
        "TOOLS DISCOVERED"
        if tools
        else "TOOLS UNAVAILABLE",
        200,
        ms,
        len(tools),
        note=(
            "Market queries not executed; "
            "tool schema inspection required"
        )
    )

def load_master():

    url = (
        "https://assets.upstox.com/"
        "market-quote/instruments/"
        "exchange/NSE.json.gz"
    )

    r, ms, err = request_url(
        url,
        timeout=30
    )

    if r is None or r.status_code != 200:

        failed(
            "UPSTOX",
            "Instrument Master",
            r,
            ms,
            err
        )

        return {}, []

    try:
        instruments = json.loads(
            gzip.decompress(r.content)
        )

        options = [
            x for x in instruments
            if x.get("segment") == "NSE_FO"
            and x.get("instrument_type")
            in ["CE", "PE"]
        ]

        underlyings = {
            x.get("underlying_key")
            for x in options
            if x.get("underlying_key")
        }

        equities = {
            x["instrument_key"]:
            x.get("trading_symbol")
            for x in instruments
            if x.get("segment") == "NSE_EQ"
            and x.get("instrument_key")
            in underlyings
        }

        add_result(
            "UPSTOX",
            "F&O Instrument Master",
            "DATA RECEIVED",
            200,
            ms,
            len(equities),
            note=f"Option contracts: {len(options)}"
        )

        return equities, options

    except Exception as e:

        add_result(
            "UPSTOX",
            "Instrument Master",
            "INVALID DATA",
            200,
            ms,
            note=type(e).__name__
        )

        return {}, []

def quote_batch(keys, auth, name):

    data, ms = get_json(
        "UPSTOX",
        name,
        UP + "/v2/market-quote/quotes",
        params={
            "instrument_key": ",".join(keys)
        },
        headers=auth
    )

    if data is None:
        return {}

    rows = data.get("data", {})

    if (
        data.get("status") != "success"
        or not isinstance(rows, dict)
    ):
        add_result(
            "UPSTOX",
            name,
            "INVALID DATA",
            200,
            ms
        )
        return {}

    result = {}

    for row in rows.values():

        key = row.get("instrument_token")

        if key not in keys:
            continue

        stamp = row.get("last_trade_time")

        age = age_seconds(stamp)

        result[key] = row

        quote_records.append({
            "Instrument": key,
            "LTP": row.get("last_price"),
            "Last Trade": str(parse_stamp(stamp)),
            "Last Trade Age Seconds": age,
            "Within 5 Seconds": (
                age is not None
                and 0 <= age <= 5
            ),
            "Checked IST": str(now())
        })

    add_result(
        "UPSTOX",
        name,
        "DATA RECEIVED" if result
        else "EMPTY",
        200,
        ms,
        len(result),
        note=f"Received {len(result)}/{len(keys)}"
    )

    return result

def test_option_chain(key, auth):

    data, ms = get_json(
        "UPSTOX",
        "Option Contracts",
        UP + "/v2/option/contract",
        params={"instrument_key": key},
        headers=auth
    )

    if data is None:
        return

    rows = records_from(data)

    add_result(
        "UPSTOX",
        "Option Contracts",
        "DATA RECEIVED" if rows
        else "EMPTY",
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

    data, ms = get_json(
        "UPSTOX",
        "Option Chain",
        UP + "/v2/option/chain",
        params={
            "instrument_key": key,
            "expiry_date": expiries[0]
        },
        headers=auth
    )

    if data is None:
        return

    rows = records_from(data)
    valid_greeks = 0
    valid_oi = 0
    total_legs = 0

    for row in rows:

        for side in [
            "call_options", "put_options"
        ]:

            leg = row.get(side, {})

            if not leg:
                continue

            total_legs += 1

            market = leg.get(
                "market_data", {}
            )

            greeks = leg.get(
                "option_greeks", {}
            )

            if market.get("oi") is not None:
                valid_oi += 1

            if all(
                greeks.get(k) is not None
                for k in [
                    "delta", "theta",
                    "gamma", "vega", "iv"
                ]
            ):
                valid_greeks += 1

    add_result(
        "UPSTOX",
        "Option Chain and Greeks",
        "DATA RECEIVED" if rows
        else "EMPTY",
        200,
        ms,
        len(rows),
        note=(
            f"Expiry {expiries[0]} | "
            f"OI {valid_oi}/{total_legs} | "
            f"Greeks {valid_greeks}/{total_legs} | "
            "Freshness and numeric accuracy unverified"
        )
    )

    payloads["Upstox Option Chain"] = rows[:3]

def test_news(equities, keys, auth):

    successful_keys = set()
    partial_batches = 0
    complete_batches = 0
    total_batches = 0

    for i in range(0, len(keys), 30):

        batch = keys[i:i + 30]
        total_batches += 1
        batch_complete = False

        for page in range(
            1, int(max_news_pages) + 1
        ):

            name = (
                f"News Batch {total_batches}"
                f" Page {page}"
            )

            data, ms = get_json(
                "UPSTOX NEWS",
                name,
                UP + "/v2/news",
                params={
                    "category": "instrument_keys",
                    "instrument_keys": ",".join(batch),
                    "page_number": page,
                    "page_size": 100
                },
                headers=auth
            )

            if data is None:
                break

            mapping = data.get("data", {})

            if (
                data.get("status") != "success"
                or not isinstance(mapping, dict)
            ):

                add_result(
                    "UPSTOX NEWS",
                    name,
                    "INVALID RESPONSE",
                    200,
                    ms
                )
                break

            successful_keys.update(batch)

            count = 0
            stamps = []

            for key, articles in mapping.items():

                if (
                    key not in batch
                    or not isinstance(articles, list)
                ):
                    continue

                for article in articles:

                    stamp = article.get(
                        "published_time"
                    )

                    dt = parse_stamp(stamp)

                    if dt is not None:
                        stamps.append(dt)

                    count += 1

                    news_records.append({
                        "Symbol": equities.get(key, key),
                        "Instrument": key,
                        "Heading": article.get("heading"),
                        "Summary": article.get("summary"),
                        "Published IST": (
                            str(dt) if dt is not None
                            else "UNVERIFIED"
                        ),
                        "Age Seconds": age_seconds(stamp),
                        "URL": article.get("article_link")
                    })

            meta = data.get(
                "metadata", {}
            ).get("page", {})

            pages = meta.get("total_pages")

            add_result(
                "UPSTOX NEWS",
                name,
                "DATA RECEIVED" if count
                else "NO ARTICLES RETURNED",
                200,
                ms,
                count,
                max(stamps) if stamps else None,
                f"Reported total pages: {pages}"
            )

            if pages is not None:

                try:
                    if page >= int(pages):
                        batch_complete = True
                        break

                except Exception:
                    break

            else:
                break

            time.sleep(0.3)

        if batch_complete:
            complete_batches += 1
        else:
            partial_batches += 1

        time.sleep(0.3)

    add_result(
        "UPSTOX NEWS",
        "News API Coverage",
        "COMPLETE REQUESTS"
        if complete_batches == total_batches
        else "PARTIAL / UNVERIFIED",
        count=len(news_records),
        note=(
            f"Successful requests for "
            f"{len(successful_keys)}/{len(keys)} keys | "
            f"Completed batches {complete_batches} | "
            f"Partial batches {partial_batches} | "
            "Does not establish complete news coverage"
        )
    )

def compare_prices(
    nse_price,
    nse_stamp,
    up_quote
):

    if nse_price is None or not up_quote:

        add_result(
            "CROSS CHECK",
            symbol + " Price Comparison",
            "UNAVAILABLE",
            note="Both sources required"
        )

        return

    up_price = up_quote.get("last_price")
    up_stamp = up_quote.get("last_trade_time")

    a = parse_stamp(nse_stamp)
    b = parse_stamp(up_stamp)

    if a is None or b is None:

        add_result(
            "CROSS CHECK",
            symbol + " Price Comparison",
            "TIMESTAMP UNVERIFIED"
        )
        return

    try:

        nse_price = float(nse_price)
        up_price = float(up_price)

        if nse_price <= 0 or up_price <= 0:
            raise ValueError("Invalid price")

        time_gap = abs(
            (a - b).total_seconds()
        )

        diff = abs(
            nse_price - up_price
        )

        pct = diff / nse_price * 100

        add_result(
            "CROSS CHECK",
            symbol + " Price Comparison",
            "COMPARABLE SAMPLE"
            if time_gap <= 2
            else "NOT SYNCHRONIZED",
            note=(
                f"NSE {nse_price} | "
                f"Upstox {up_price} | "
                f"Difference {pct:.4f}% | "
                f"Timestamp gap {time_gap:.2f}s"
            )
        )

    except Exception:

        add_result(
            "CROSS CHECK",
            symbol + " Price Comparison",
            "INVALID DATA"
        )

st.subheader("Data Source Testing")

st.write(
    "Official NSE files, NSE announcements, "
    "MCP tools, Upstox quotes, news and Greeks."
)

if st.button(
    "RUN COMPLETE V2 DATA TEST",
    type="primary"
):

    progress = st.progress(0)
    status = st.empty()

    status.write("Testing NSE direct sources...")

    test_page(
        "NSE",
        "Official Website",
        NSE
    )

    announcements = test_nse_list(
        "Corporate Announcements",
        "/api/corporate-announcements",
        {"index": "equities"}
    )

    nse_price, nse_stamp = test_nse_quote()

    test_nse_chain()

    preopen = test_nse_list(
        "Pre Open Data",
        "/api/market-data-pre-open",
        {"key": "FO"}
    )

    test_nse_list(
        "Direct Bulk Deals",
        "/api/bulk-deals"
    )

    test_nse_list(
        "Direct Block Deals",
        "/api/block-deals"
    )

    progress.progress(20)

    status.write("Testing official NSE reports...")

    test_page(
        "NSE",
        "Official Reports",
        NSE + "/all-reports"
    )

    test_page(
        "NSE",
        "Bulk Block Deals Report Page",
        NSE + "/report-detail/"
        "display-bulk-and-block-deals"
    )

    test_page(
        "BSE",
        "Announcements Page",
        "https://www.bseindia.com/"
        "corporates/ann.html"
    )

    d = report_date.strftime("%Y%m%d")
    d2 = report_date.strftime("%d%m%Y")

    test_archive(
        "Security Deliverable Data",
        ARCHIVE + "/products/content/"
        f"sec_bhavdata_full_{d2}.csv",
        "DELIVERY"
    )

    test_archive(
        "CM UDiFF Bhavcopy",
        ARCHIVE + "/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d}_"
        "F_0000.csv.zip",
        "EQUITY"
    )

    test_archive(
        "FO UDiFF Bhavcopy",
        ARCHIVE + "/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{d}_"
        "F_0000.csv.zip",
        "FO"
    )

    test_archive(
        "Bulk Deals Archive Candidate",
        ARCHIVE + "/content/equities/"
        f"bulk_{d2}.csv",
        "BULK_BLOCK"
    )

    test_archive(
        "Block Deals Archive Candidate",
        ARCHIVE + "/content/equities/"
        f"block_{d2}.csv",
        "BULK_BLOCK"
    )

    progress.progress(40)

    status.write("Testing NSE MCP tools...")

    test_mcp(
        "Bhavcopy MCP",
        "https://mcp.nseindia.in/bhavcopy/cm/mcp"
    )

    test_mcp(
        "CM Market MCP",
        "https://mcp.nseindia.in/cmmkt/mcp"
    )

    progress.progress(55)

    status.write("Loading instrument master...")

    equities, options = load_master()

    selected_key = next(
        (
            key for key, name in equities.items()
            if name == symbol
        ),
        None
    )

    if token:

        auth = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        status.write("Testing Upstox live quotes...")

        quote_batch(
            ["NSE_INDEX|Nifty 50"],
            auth,
            "NIFTY Quote"
        )

        selected_quote = {}

        if selected_key:

            selected_quote = quote_batch(
                [selected_key],
                auth,
                "Selected Stock Quote"
            ).get(selected_key, {})

            compare_prices(
                nse_price,
                nse_stamp,
                selected_quote
            )

            test_option_chain(
                selected_key,
                auth
            )

        else:

            add_result(
                "UPSTOX",
                "Selected Stock",
                "NOT FOUND",
                note=symbol
            )

        if full_quotes:

            keys = list(equities)
            received = {}

            before = len(quote_records)
            scan_start = time.perf_counter()

            for i in range(0, len(keys), 40):

                received.update(
                    quote_batch(
                        keys[i:i + 40],
                        auth,
                        f"Quote Batch {i // 40 + 1}"
                    )
                )

                time.sleep(0.3)

            sample = quote_records[before:]

            fresh = sum(
                x["Within 5 Seconds"]
                for x in sample
            )

            coverage = (
                len(received) / len(keys) * 100
                if keys else 0
            )

            add_result(
                "UPSTOX",
                "Full F&O Quote Coverage",
                "COMPLETE"
                if keys and len(received) == len(keys)
                else "PARTIAL",
                ms=round(
                    (time.perf_counter() - scan_start)
                    * 1000
                ),
                count=len(received),
                note=(
                    f"{len(received)}/{len(keys)} | "
                    f"Coverage {coverage:.2f}% | "
                    f"Last trade within 5 sec: {fresh}"
                )
            )

        progress.progress(75)

        status.write("Testing Upstox News API...")

        if full_news:
            news_keys = list(equities)
        else:
            news_keys = (
                [selected_key]
                if selected_key else []
            )

        if news_keys:

            test_news(
                equities,
                news_keys,
                auth
            )

    else:

        add_result(
            "UPSTOX",
            "Authenticated APIs",
            "SKIPPED",
            note="Access token required"
        )

    news_df = pd.DataFrame(news_records)

    if not news_df.empty:

        news_df = news_df.drop_duplicates(
            subset=[
                "Instrument",
                "Heading",
                "Published IST"
            ]
        )

    st.session_state["v2_results"] = (
        pd.DataFrame(results)
    )

    st.session_state["v2_payloads"] = payloads

    st.session_state["v2_news"] = news_df

    st.session_state["v2_quotes"] = (
        pd.DataFrame(quote_records)
    )

    st.session_state["v2_reports"] = report_frames

    progress.progress(100)

    status.success(
        "V2 Data Connectivity Test Completed"
    )

if "v2_results" in st.session_state:

    df = st.session_state["v2_results"]

    st.divider()
    st.header("DATA HEALTH REPORT")

    a, b, c = st.columns(3)

    a.metric("Total Tests", len(df))

    b.metric(
        "Data / Report Responses",
        int(
            df["Status"].isin([
                "DATA RECEIVED",
                "REPORT RECEIVED",
                "TOOLS DISCOVERED",
                "COMPLETE",
                "COMPLETE REQUESTS"
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
        "DOWNLOAD V2 TEST REPORT",
        df.to_csv(index=False),
        "data_connectivity_v2.csv",
        "text/csv"
    )

    st.subheader("HTTP ERROR DIAGNOSTICS")

    errors = df[
        df["HTTP"].astype(str).isin([
            "400", "401", "403",
            "404", "429", "500",
            "502", "503", "504"
        ])
    ]

    if errors.empty:
        st.success("No listed HTTP errors")
    else:
        st.dataframe(
            errors,
            use_container_width=True
        )

    st.header("NSE CORPORATE ANNOUNCEMENTS")

    announcements = st.session_state[
        "v2_payloads"
    ].get("Corporate Announcements", [])

    if announcements:

        st.dataframe(
            pd.DataFrame(announcements),
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD NSE ANNOUNCEMENT SAMPLE",
            pd.DataFrame(
                announcements
            ).to_csv(index=False),
            "nse_announcement_sample.csv",
            "text/csv"
        )

    else:
        st.warning("Announcements unavailable")

    st.header("UPSTOX STOCK NEWS")

    news_df = st.session_state["v2_news"]

    if not news_df.empty:

        st.metric(
            "Unique Stock-News Records",
            len(news_df)
        )

        st.dataframe(
            news_df,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD STOCK NEWS",
            news_df.to_csv(index=False),
            "upstox_stock_news.csv",
            "text/csv"
        )

    else:
        st.info("No validated news records")

    st.header("QUOTE FRESHNESS")

    qdf = st.session_state["v2_quotes"]

    if not qdf.empty:

        st.dataframe(
            qdf,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD QUOTE FRESHNESS",
            qdf.to_csv(index=False),
            "quote_freshness_v2.csv",
            "text/csv"
        )

    st.header("NSE OFFICIAL REPORTS")

    for name, frame in st.session_state[
        "v2_reports"
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

    st.header("NSE MCP AND RESPONSE INSPECTION")

    for name, data in st.session_state[
        "v2_payloads"
    ].items():

        with st.expander(name):
            st.json(data)

    st.warning(
        "Successful requests do not prove full "
        "coverage or trading accuracy. Archive "
        "paths are test candidates. Last trade "
        "age is not network latency. MCP discovery "
        "does not establish live market access."
    )

st.divider()

st.caption(
    "DATA CONNECTIVITY V2 | "
    "READ ONLY | NO ORDERS"
)