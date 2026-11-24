
import gzip
import json
import re
import requests
import streamlit as st
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

NSE = "https://www.nseindia.com"

MASTER = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": NSE + "/"
}


def now():
    return datetime.now(IST)


# =====================================
# COMPANY NAME DATABASE
# =====================================

@st.cache_data(ttl=86400, show_spinner=False)
def company_names():

    r = requests.get(MASTER, timeout=25)
    r.raise_for_status()

    data = json.loads(
        gzip.decompress(r.content)
    )

    return {
        x["trading_symbol"]: x.get("name")
        for x in data
        if x.get("segment") == "NSE_EQ"
        and x.get("trading_symbol")
        and x.get("name")
    }


def company_name(symbol):

    indexes = {
        "NIFTY": "Nifty 50",
        "BANKNIFTY": "Nifty Bank",
        "FINNIFTY": "Nifty Financial Services"
    }

    if symbol in indexes:
        return indexes[symbol]

    try:
        return company_names().get(
            symbol, symbol
        )
    except Exception:
        return symbol


# =====================================
# NEWS CLASSIFICATION
# =====================================

HIGH_RISK = [
    r"\bfraud\b",
    r"\baccounting irregularit",
    r"\binsolvency\b",
    r"\bdefaulted\b",
    r"\btrading suspension\b",
    r"\bforensic audit\b",
    r"\bsebi investigation\b",
    r"\bregulatory investigation\b",
    r"\bshow.cause notice\b"
]

POSITIVE = [
    r"\bwins? (a |new |major )?order\b",
    r"\bsecures? (a |new |major )?contract\b",
    r"\bprofit rises?\b",
    r"\bprofit jumps?\b",
    r"\bprofit surges?\b",
    r"\bbeats? estimates\b",
    r"\braises? guidance\b",
    r"\bcredit rating upgrade\b",
    r"\bturns profitable\b",
    r"\breceives? regulatory approval\b"
]

NEGATIVE = [
    r"\bprofit falls?\b",
    r"\bprofit declines?\b",
    r"\bprofit drops?\b",
    r"\bnet loss widens?\b",
    r"\bmisses? estimates\b",
    r"\bcuts? guidance\b",
    r"\bcredit rating downgrade\b",
    r"\border cancellation\b",
    r"\bcontract terminated\b",
    r"\bregulatory penalty\b"
]

EVENTS = {

    "QUARTERLY RESULTS": [
        "quarterly result",
        "financial result",
        "net profit",
        "net loss",
        "earnings"
    ],

    "ORDER / CONTRACT": [
        "order",
        "contract",
        "letter of award",
        "letter of intent"
    ],

    "REGULATORY": [
        "sebi",
        "penalty",
        "investigation",
        "regulatory",
        "show cause"
    ],

    "MANAGEMENT": [
        "resignation",
        "appointment",
        "managing director",
        "chief executive",
        "management change"
    ],

    "MERGER / ACQUISITION": [
        "merger",
        "acquisition",
        "demerger",
        "takeover"
    ],

    "CORPORATE ACTION": [
        "dividend",
        "bonus issue",
        "stock split",
        "buyback",
        "rights issue"
    ],

    "INVESTOR MEETING": [
        "investor meeting",
        "analyst meeting",
        "institutional investor"
    ],

    "BULK / BLOCK DEAL": [
        "bulk deal",
        "block deal"
    ],

    "FUND RAISING": [
        "fund raising",
        "fundraising",
        "qualified institutional placement"
    ]
}


def classify(text):

    text = str(text).lower()

    if any(
        re.search(p, text)
        for p in HIGH_RISK
    ):
        return "HIGH RISK"

    pos = any(
        re.search(p, text)
        for p in POSITIVE
    )

    neg = any(
        re.search(p, text)
        for p in NEGATIVE
    )

    if pos and neg:
        return "MIXED"

    if pos:
        return "POSITIVE"

    if neg:
        return "NEGATIVE"

    return "REVIEW"


def event_type(text):

    text = str(text).lower()

    for category, words in EVENTS.items():

        if any(w in text for w in words):
            return category

    return "GENERAL NEWS"


# =====================================
# GOOGLE NEWS RSS
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def google_news(symbol, name):

    cutoff = now() - timedelta(hours=48)

    query = (
        f'"{name}" stock India when:2d'
    )

    try:

        r = requests.get(
            "https://news.google.com/rss/search",
            params={
                "q": query,
                "hl": "en-IN",
                "gl": "IN",
                "ceid": "IN:en"
            },
            timeout=12
        )

        r.raise_for_status()

        root = ET.fromstring(r.content)

        items = []

        for item in root.findall(".//item"):

            try:

                title = item.findtext(
                    "title", ""
                )

                published = parsedate_to_datetime(
                    item.findtext("pubDate", "")
                ).astimezone(IST)

                if not cutoff <= published <= now():
                    continue

                source = item.findtext(
                    "source", "Google News"
                )

                items.append({

                    "title": title,

                    "source": source,

                    "time": published.isoformat(),

                    "url": item.findtext(
                        "link", ""
                    ),

                    "impact": classify(title),

                    "event": event_type(title),

                    "official": False
                })

            except Exception:
                continue

        return {
            "available": True,
            "items": items[:20],
            "error": ""
        }

    except Exception as e:

        return {
            "available": False,
            "items": [],
            "error": str(e)
        }


# =====================================
# NSE OFFICIAL DATA
# =====================================

def nse_request(path, params=None):

    with requests.Session() as session:

        session.headers.update(HEADERS)

        home = session.get(
            NSE,
            timeout=10
        )

        home.raise_for_status()

        r = session.get(
            NSE + path,
            params=params,
            timeout=15
        )

        r.raise_for_status()

        return r.json()


def extract_rows(data):

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for key in (
            "data",
            "bulkDeals",
            "blockDeals",
            "bulk_deals",
            "block_deals"
        ):

            if isinstance(data.get(key), list):
                return data[key]

    raise ValueError(
        "Unrecognized NSE response format"
    )


# =====================================
# CORPORATE ANNOUNCEMENTS
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def nse_announcements():

    try:

        data = nse_request(

            "/api/corporate-announcements",

            {
                "index": "equities",

                "from_date": (
                    now() - timedelta(days=2)
                ).strftime("%d-%m-%Y"),

                "to_date": now().strftime(
                    "%d-%m-%Y"
                )
            }
        )

        return {
            "available": True,
            "rows": extract_rows(data),
            "error": ""
        }

    except Exception as e:

        return {
            "available": False,
            "rows": [],
            "error": str(e)
        }


def parse_nse_time(value):

    value = str(value or "").strip()

    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S"
    ):

        try:

            return datetime.strptime(
                value, fmt
            ).replace(tzinfo=IST)

        except ValueError:
            continue

    return None


def official_news(symbol):

    data = nse_announcements()

    items = []

    cutoff = now() - timedelta(hours=48)

    for row in data["rows"]:

        if str(
            row.get("symbol", "")
        ).upper() != symbol.upper():
            continue

        published = parse_nse_time(

            row.get("an_dt")

            or row.get("sort_date")

            or row.get("broadcastDateTime")
        )

        if published is None:
            continue

        if not cutoff <= published <= now():
            continue

        title = str(

            row.get("desc")

            or row.get("subject")

            or "Corporate announcement"
        )

        details = str(
            row.get("attchmntText", "")
        )

        text = title + " " + details

        items.append({

            "title": title + (
                " — " + details[:300]
                if details else ""
            ),

            "source": "NSE Corporate Announcements",

            "time": published.isoformat(),

            "url": (
                row.get("attchmntFile")
                or NSE
            ),

            "impact": classify(text),

            "event": event_type(text),

            "official": True
        })

    return {
        "available": data["available"],
        "items": items,
        "error": data["error"]
    }


# =====================================
# BULK AND BLOCK DEALS
# =====================================

@st.cache_data(ttl=600, show_spinner=False)
def deals_feed(kind):

    endpoint = (
        "/api/historical/bulk-deals"
        if kind == "BULK"
        else "/api/historical/block-deals"
    )

    try:

        data = nse_request(

            endpoint,

            {
                "from": (
                    now() - timedelta(days=7)
                ).strftime("%d-%m-%Y"),

                "to": now().strftime(
                    "%d-%m-%Y"
                )
            }
        )

        return {
            "available": True,
            "rows": extract_rows(data),
            "error": ""
        }

    except Exception as e:

        return {
            "available": False,
            "rows": [],
            "error": str(e)
        }


def pick(row, *keys):

    for key in keys:

        if row.get(key) is not None:
            return row[key]

    return ""


def deal_news(symbol, kind):

    data = deals_feed(kind)

    items = []

    for row in data["rows"]:

        sym = pick(
            row,
            "symbol",
            "BD_SYMBOL",
            "BD_DT_SYMBOL"
        )

        if str(sym).upper() != symbol.upper():
            continue

        client = pick(
            row,
            "clientName",
            "BD_CLIENT_NAME"
        )

        side = pick(
            row,
            "buySell",
            "BD_BUY_SELL",
            "BD_BUY_SELL_TYPE"
        )

        qty = pick(
            row,
            "quantity",
            "BD_QTY_TRD"
        )

        price = pick(
            row,
            "tradePrice",
            "BD_TP_WATP"
        )

        date = pick(
            row,
            "date",
            "BD_DT_DATE",
            "BD_DATE"
        )

        if not date:
            continue

        items.append({

            "title": (
                f"{kind} DEAL | {symbol} | "
                f"{client} | {side} | "
                f"Quantity: {qty} | Price: {price}"
            ),

            "source": "NSE Deal Data",

            "time": str(date),

            "url": (
                NSE + "/report-detail/"
                "display-bulk-and-block-deals"
            ),

            "impact": "REVIEW",

            "event": kind + " DEAL",

            "official": True
        })

    return {
        "available": data["available"],
        "items": items[:15],
        "error": data["error"]
    }


# =====================================
# COMBINED NEWS INTELLIGENCE
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(symbol):

    name = company_name(symbol)

    google = google_news(symbol, name)

    official = official_news(symbol)

    bulk = deal_news(symbol, "BULK")

    block = deal_news(symbol, "BLOCK")

    sources = {

        "Google News": google["available"],

        "NSE Announcements":
        official["available"],

        "NSE Bulk Deals":
        bulk["available"],

        "NSE Block Deals":
        block["available"]
    }

    errors = {

        "Google News": google["error"],

        "NSE Announcements":
        official["error"],

        "NSE Bulk Deals":
        bulk["error"],

        "NSE Block Deals":
        block["error"]
    }

    combined = (

        official["items"]

        + google["items"]

        + bulk["items"]

        + block["items"]
    )

    unique = {}

    for item in combined:

        key = re.sub(
            r"\W+", "",
            item["title"].lower()
        )

        if key not in unique:
            unique[key] = item

    items = list(unique.values())

    return {

        "available": any(sources.values()),

        "complete": all(sources.values()),

        "company": name,

        "sources": sources,

        "errors": errors,

        "items": items
    }


# =====================================
# FINAL NEWS ASSESSMENT
# =====================================

def assess_news(symbol, side):

    side = str(side).upper()

    data = fetch_news(symbol)

    items = data["items"]

    flags = {

        item["impact"]

        for item in items

        if item["event"] not in (
            "BULK DEAL",
            "BLOCK DEAL"
        )
    }

    conflict = (

        "HIGH RISK" in flags

        or "MIXED" in flags

        or (
            "POSITIVE" in flags
            and "NEGATIVE" in flags
        )
    )

    opposite = (

        "NEGATIVE" in flags

        if side == "CE"

        else "POSITIVE" in flags
    )

    aligned = (

        "POSITIVE" in flags

        if side == "CE"

        else "NEGATIVE" in flags
    )

    if conflict:

        status = "HIGH RISK / MIXED"

    elif opposite:

        status = "OPPOSITE NEWS"

    elif aligned:

        status = "ALIGNED HEADLINE"

    elif not data["available"]:

        status = "NEWS FEED UNAVAILABLE"

    elif not items and not data["complete"]:

        status = "NEWS UNVERIFIED"

    elif not items:

        status = "NO RECENT MATCHING NEWS"

    else:

        status = "HEADLINES UNCLASSIFIED"

    points = 0

    if aligned and not conflict and not opposite:

        official_aligned = any(

            item["official"]

            and item["impact"] == (

                "POSITIVE"
                if side == "CE"
                else "NEGATIVE"
            )

            for item in items
        )

        points = 10 if official_aligned else 5

    return {

        "symbol": symbol,

        "company": data["company"],

        "direction": side,

        "status": status,

        "block": bool(
            conflict or opposite
        ),

        "points": points,

        "items": items,

        "sources": data["sources"],

        "errors": data["errors"],

        "complete": data["complete"],

        "checked": now().isoformat()
    }


# =====================================
# NEWS OPPORTUNITY SCANNER
# =====================================

def scan_news_market(
    symbols,
    start=0,
    batch_size=10
):

    symbols = list(
        dict.fromkeys(symbols)
    )

    if not symbols:

        return {
            "results": [],
            "next_cursor": 0,
            "total": 0
        }

    start = start % len(symbols)

    rotated = (
        symbols[start:]
        + symbols[:start]
    )

    batch = rotated[:batch_size]

    results = []

    for symbol in batch:

        try:

            ce = assess_news(symbol, "CE")

            pe = assess_news(symbol, "PE")

            direction = "NEUTRAL"

            if ce["block"] and pe["block"]:

                direction = "HIGH RISK"

            elif (
                ce["points"] > 0
                and not ce["block"]
            ):

                direction = "CE WATCH"

            elif (
                pe["points"] > 0
                and not pe["block"]
            ):

                direction = "PE WATCH"

            results.append({

                "Symbol": symbol,

                "Company": ce["company"],

                "News Direction": direction,

                "CE Points": ce["points"],

                "PE Points": pe["points"],

                "CE Status": ce["status"],

                "PE Status": pe["status"],

                "Headlines": len(ce["items"]),

                "Sources": ce["sources"],

                "Checked": ce["checked"]
            })

        except Exception as e:

            results.append({

                "Symbol": symbol,

                "News Direction": "UNVERIFIED",

                "Error": str(e)
            })

    return {

        "results": results,

        "next_cursor": (
            start + len(batch)
        ) % len(symbols),

        "total": len(symbols)
    }
