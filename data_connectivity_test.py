import streamlit as st
import requests
import pandas as pd
import gzip
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

st.set_page_config(
    page_title="F&O Data Connectivity Test",
    page_icon="📡",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")

NSE = "https://www.nseindia.com"
UPSTOX = "https://api.upstox.com"

HEADERS = {
    "User-Agent": "FNO-Data-Research/1.0",
    "Accept": "application/json, text/html, */*"
}

st.title("F&O DATA CONNECTIVITY TEST V1")

st.caption(
    "NSE | BSE | UPSTOX | MCP | DATA HEALTH"
)

st.info(
    "Research and connectivity testing only. "
    "No automatic orders."
)

token = st.sidebar.text_input(
    "Upstox Access Token",
    type="password"
).strip()

symbol = st.sidebar.text_input(
    "Test Stock",
    value="DIXON"
).strip().upper()

results = []
payloads = {}

def now():
    return datetime.now(IST)

def add_result(
    source,
    name,
    status,
    http,
    latency,
    records,
    freshness,
    remarks
):
    results.append({
        "Source": source,
        "Data": name,
        "Status": status,
        "HTTP": http,
        "Response ms": latency,
        "Records": records,
        "Freshness": freshness,
        "Remarks": remarks,
        "Checked IST": now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    })

def timestamp_info(value):
    if value is None or value == "":
        return "TIMESTAMP UNAVAILABLE"

    try:
        if isinstance(value, (int, float)) or (
            isinstance(value, str)
            and value.isdigit()
        ):
            v = float(value)
            unit = "ms" if v > 1e11 else "s"
            dt = pd.to_datetime(
                v, unit=unit, utc=True
            ).tz_convert(IST)
        else:
            dt = pd.to_datetime(
                value, dayfirst=True
            )

            if dt.tzinfo is None:
                dt = dt.tz_localize(IST)
            else:
                dt = dt.tz_convert(IST)

        age = (
            now() - dt.to_pydatetime()
        ).total_seconds()

        return (
            f"{dt.strftime('%d-%m-%Y %H:%M:%S')} "
            f"| Age {round(age, 1)} sec"
        )

    except Exception:
        return str(value)

def test_url(
    source,
    name,
    url,
    params=None,
    headers=None,
    expected="json",
    validator=None
):
    start = time.perf_counter()

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers or HEADERS,
            timeout=15
        )

        latency = round(
            (time.perf_counter() - start) * 1000
        )

        if response.status_code != 200:
            add_result(
                source, name,
                "FAILED",
                response.status_code,
                latency, 0,
                "NOT VERIFIED",
                f"HTTP {response.status_code}"
            )
            return None

        if expected == "html":
            add_result(
                source, name,
                "PAGE REACHABLE",
                200, latency, 0,
                "NOT VERIFIED",
                "Page access only; data not validated"
            )
            return response.text

        data = response.json()

        if validator:
            valid, count, stamp, note = validator(data)
        else:
            valid = bool(data)
            count = len(data) if hasattr(
                data, "__len__"
            ) else 0
            stamp = None
            note = "JSON response received"

        add_result(
            source,
            name,
            "DATA RECEIVED" if valid else "EMPTY / INVALID",
            200,
            latency,
            count,
            timestamp_info(stamp),
            note
        )

        payloads[name] = data
        return data

    except Exception as e:
        latency = round(
            (time.perf_counter() - start) * 1000
        )

        add_result(
            source, name,
            "FAILED",
            "-",
            latency, 0,
            "NOT VERIFIED",
            type(e).__name__
        )

        return None

def validate_nse_list(data):
    rows = data if isinstance(data, list) else []

    if not rows and isinstance(data, dict):
        rows = data.get("data", [])

    if not isinstance(rows, list):
        rows = []

    stamp = None

    if rows and isinstance(rows[0], dict):
        row = rows[0]

        for key in [
            "an_dt",
            "sort_date",
            "broadcastDateTime",
            "date",
            "timestamp"
        ]:
            if row.get(key):
                stamp = row[key]
                break

    return (
        bool(rows),
        len(rows),
        stamp,
        "Record count checked; completeness unverified"
    )

def validate_nse_quote(data):
    info = data.get("priceInfo", {})
    metadata = data.get("metadata", {})

    price = info.get("lastPrice")
    stamp = metadata.get("lastUpdateTime")

    return (
        price is not None,
        1 if price is not None else 0,
        stamp,
        f"Last price: {price}"
    )

def validate_nse_chain(data):
    records = data.get("records", {})
    rows = records.get("data", [])

    return (
        bool(rows),
        len(rows),
        records.get("timestamp"),
        "Option chain rows checked"
    )

def validate_upstox_quote(data):
    rows = data.get("data", {})

    if not isinstance(rows, dict):
        rows = {}

    first = next(iter(rows.values()), {})

    stamp = (
        first.get("last_trade_time")
        or first.get("timestamp")
    )

    return (
        data.get("status") == "success"
        and bool(rows),
        len(rows),
        stamp,
        f"LTP: {first.get('last_price')}"
    )

def validate_upstox_list(data):
    rows = data.get("data", [])

    return (
        data.get("status") == "success"
        and bool(rows),
        len(rows),
        None,
        "Response received; freshness not established"
    )

def test_mcp(name, url):
    start = time.perf_counter()

    try:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": (
                    "application/json, "
                    "text/event-stream"
                )
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "fno-data-test",
                        "version": "1.0"
                    }
                }
            },
            timeout=15
        )

        latency = round(
            (time.perf_counter() - start) * 1000
        )

        text = response.text
        parsed = None

        try:
            parsed = response.json()
        except Exception:
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        parsed = json.loads(
                            line[5:].strip()
                        )
                        break
                    except Exception:
                        pass

        valid = (
            response.status_code == 200
            and isinstance(parsed, dict)
            and "result" in parsed
        )

        add_result(
            "NSE MCP",
            name,
            "HANDSHAKE OK" if valid else "FAILED",
            response.status_code,
            latency,
            0,
            "NOT VERIFIED",
            "Handshake only; market tools not tested"
        )

    except Exception as e:
        add_result(
            "NSE MCP", name,
            "FAILED", "-",
            round(
                (time.perf_counter() - start) * 1000
            ),
            0,
            "NOT VERIFIED",
            type(e).__name__
        )

def load_master():
    start = time.perf_counter()

    url = (
        "https://assets.upstox.com/"
        "market-quote/instruments/exchange/"
        "NSE.json.gz"
    )

    try:
        response = requests.get(
            url, timeout=30
        )
        response.raise_for_status()

        raw = gzip.decompress(response.content)
        instruments = json.loads(raw)

        options = [
            x for x in instruments
            if x.get("segment") == "NSE_FO"
            and x.get("instrument_type")
            in ["CE", "PE"]
        ]

        underlying_keys = {
            x.get("underlying_key")
            for x in options
            if x.get("underlying_key")
        }

        equities = [
            x for x in instruments
            if x.get("segment") == "NSE_EQ"
            and x.get("instrument_key")
            in underlying_keys
        ]

        add_result(
            "UPSTOX",
            "F&O Instrument Master",
            "DATA RECEIVED",
            200,
            round(
                (time.perf_counter() - start) * 1000
            ),
            len(equities),
            "MASTER FILE",
            f"Option contracts: {len(options)}"
        )

        return equities, options

    except Exception as e:
        add_result(
            "UPSTOX",
            "F&O Instrument Master",
            "FAILED",
            "-",
            round(
                (time.perf_counter() - start) * 1000
            ),
            0,
            "NOT VERIFIED",
            type(e).__name__
        )

        return [], []

def test_coverage(equities, auth):
    keys = [
        x["instrument_key"]
        for x in equities
        if x.get("instrument_key")
    ]

    received = {}
    failures = 0
    start = time.perf_counter()

    for i in range(0, len(keys), 40):
        batch = keys[i:i + 40]

        try:
            response = requests.get(
                UPSTOX + "/v2/market-quote/quotes",
                headers=auth,
                params={
                    "instrument_key": ",".join(batch)
                },
                timeout=15
            )

            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                failures += 1
                continue

            for row in data.get("data", {}).values():
                key = row.get("instrument_token")

                if key in keys:
                    received[key] = row

        except Exception:
            failures += 1

        time.sleep(0.3)

    coverage = (
        len(received) / len(keys) * 100
        if keys else 0
    )

    fresh = 0

    for row in received.values():
        stamp = (
            row.get("last_trade_time")
            or row.get("timestamp")
        )

        try:
            if str(stamp).isdigit():
                value = float(stamp)
                unit = "ms" if value > 1e11 else "s"

                dt = pd.to_datetime(
                    value, unit=unit, utc=True
                ).tz_convert(IST)
            else:
                dt = pd.to_datetime(
                    stamp, utc=True
                ).tz_convert(IST)

            age = (
                now() - dt.to_pydatetime()
            ).total_seconds()

            if 0 <= age <= 5:
                fresh += 1

        except Exception:
            pass

    add_result(
        "UPSTOX",
        "Full F&O Equity Quote Coverage",
        "COMPLETE" if coverage == 100
        else "PARTIAL",
        "-",
        round(
            (time.perf_counter() - start) * 1000
        ),
        len(received),
        f"{fresh} quotes within 5 seconds",
        (
            f"{len(received)}/{len(keys)} "
            f"| Coverage {coverage:.2f}% "
            f"| Failed batches {failures}"
        )
    )

    return len(keys), len(received), fresh

full_coverage = st.sidebar.checkbox(
    "Test All F&O Stock Quotes",
    value=True
)

st.subheader("Data Sources To Test")

st.write(
    "NSE announcements, equity quotes, "
    "option chain, pre-open data, deals, "
    "reports, BSE, NSE MCP and Upstox."
)

if st.button(
    "RUN COMPLETE DATA CONNECTIVITY TEST",
    type="primary"
):
    progress = st.progress(0)
    status = st.empty()

    status.write("Testing NSE website...")

    test_url(
        "NSE",
        "Official Website",
        NSE,
        expected="html"
    )

    progress.progress(10)

    status.write("Testing NSE announcements...")

    test_url(
        "NSE",
        "Corporate Announcements",
        NSE + "/api/corporate-announcements",
        params={"index": "equities"},
        validator=validate_nse_list
    )

    test_url(
        "NSE",
        "Equity Quote",
        NSE + "/api/quote-equity",
        params={"symbol": symbol},
        validator=validate_nse_quote
    )

    progress.progress(25)

    status.write("Testing NSE derivatives...")

    test_url(
        "NSE",
        "Option Chain NIFTY",
        NSE + "/api/option-chain-indices",
        params={"symbol": "NIFTY"},
        validator=validate_nse_chain
    )

    test_url(
        "NSE",
        "Pre Open Data",
        NSE + "/api/market-data-pre-open",
        params={"key": "FO"},
        validator=validate_nse_list
    )

    test_url(
        "NSE",
        "Bulk Deals",
        NSE + "/api/bulk-deals",
        validator=validate_nse_list
    )

    test_url(
        "NSE",
        "Block Deals",
        NSE + "/api/block-deals",
        validator=validate_nse_list
    )

    progress.progress(40)

    status.write("Testing official report pages...")

    test_url(
        "NSE",
        "Daily Reports",
        NSE + "/all-reports",
        expected="html"
    )

    test_url(
        "BSE",
        "Corporate Announcements Page",
        "https://www.bseindia.com/corporates/ann.html",
        expected="html"
    )

    progress.progress(50)

    status.write("Testing NSE MCP servers...")

    test_mcp(
        "Bhavcopy MCP",
        "https://mcp.nseindia.in/bhavcopy/cm/mcp"
    )

    test_mcp(
        "CM Market MCP",
        "https://mcp.nseindia.in/cmmkt/mcp"
    )

    progress.progress(65)

    status.write("Loading F&O instrument master...")

    equities, options = load_master()

    if token:
        auth = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        status.write("Testing Upstox market data...")

        test_url(
            "UPSTOX",
            "NIFTY Live Quote",
            UPSTOX + "/v2/market-quote/quotes",
            params={
                "instrument_key":
                "NSE_INDEX|Nifty 50"
            },
            headers=auth,
            validator=validate_upstox_quote
        )

        selected = next(
            (
                x for x in equities
                if x.get("trading_symbol") == symbol
            ),
            None
        )

        if selected:
            key = selected["instrument_key"]

            test_url(
                "UPSTOX",
                "Selected Stock Quote",
                UPSTOX + "/v2/market-quote/quotes",
                params={"instrument_key": key},
                headers=auth,
                validator=validate_upstox_quote
            )

            contracts = test_url(
                "UPSTOX",
                "Option Contracts",
                UPSTOX + "/v2/option/contract",
                params={"instrument_key": key},
                headers=auth,
                validator=validate_upstox_list
            )

            if contracts:
                expiries = sorted({
                    x.get("expiry")
                    for x in contracts.get("data", [])
                    if x.get("expiry")
                    and x["expiry"] >= str(now().date())
                })

                if expiries:
                    test_url(
                        "UPSTOX",
                        "Live Option Chain and Greeks",
                        UPSTOX + "/v2/option/chain",
                        params={
                            "instrument_key": key,
                            "expiry_date": expiries[0]
                        },
                        headers=auth,
                        validator=validate_upstox_list
                    )

        progress.progress(85)

        if full_coverage and equities:
            status.write(
                "Testing all F&O equity quotes..."
            )

            total, received, fresh = test_coverage(
                equities, auth
            )

            st.session_state["coverage"] = {
                "Total": total,
                "Received": received,
                "Fresh": fresh
            }

    else:
        add_result(
            "UPSTOX",
            "Authenticated Market Data",
            "SKIPPED",
            "-", 0, 0,
            "NOT VERIFIED",
            "Access token required"
        )

    st.session_state["test_results"] = pd.DataFrame(
        results
    )

    st.session_state["test_payloads"] = payloads

    progress.progress(100)
    status.success("Connectivity test completed.")

if "test_results" in st.session_state:

    df = st.session_state["test_results"]

    st.divider()
    st.header("DATA CONNECTIVITY RESULTS")

    c1, c2, c3 = st.columns(3)

    c1.metric("Total Tests", len(df))

    c2.metric(
        "Data Responses",
        int(
            df["Status"].isin([
                "DATA RECEIVED",
                "COMPLETE"
            ]).sum()
        )
    )

    c3.metric(
        "Failed Tests",
        int((df["Status"] == "FAILED").sum())
    )

    if "coverage" in st.session_state:
        coverage = st.session_state["coverage"]

        a, b, c = st.columns(3)

        a.metric(
            "F&O Stocks",
            coverage["Total"]
        )

        b.metric(
            "Quotes Received",
            coverage["Received"]
        )

        c.metric(
            "Fresh Quotes",
            coverage["Fresh"]
        )

    st.dataframe(
        df,
        use_container_width=True
    )

    st.download_button(
        "DOWNLOAD COMPLETE TEST REPORT",
        df.to_csv(index=False).encode("utf-8"),
        "data_connectivity_report.csv",
        "text/csv"
    )

    st.subheader("Data Response Inspection")

    for name, data in st.session_state.get(
        "test_payloads", {}
    ).items():

        with st.expander(name):

            if isinstance(data, list):
                st.json(data[:3])
            elif isinstance(data, dict):
                st.json({
                    k: (
                        v[:3]
                        if isinstance(v, list)
                        else v
                    )
                    for k, v in data.items()
                })

    st.warning(
        "HTTP success does not establish accuracy. "
        "Page access and MCP handshake do not prove "
        "market data availability. Closed-market "
        "quotes may legitimately be old."
    )

st.divider()

st.caption(
    "DATA RESEARCH V1 | READ ONLY | "
    "NO AUTOMATIC ORDERS"
)