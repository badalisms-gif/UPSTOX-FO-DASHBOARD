
import gzip
import json
import re
import requests
import streamlit as st
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

IST = ZoneInfo("Asia/Kolkata")

NSE = "https://www.nseindia.com"
BSE = "https://api.bseindia.com"

MASTER = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9"
}


def now():
    return datetime.now(IST)


def session():
    s = requests.Session()
    s.headers.update(HEADERS)

    retry = Retry(
        total=1,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=False
    )

    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def error_text(e):
    if isinstance(e, requests.exceptions.HTTPError):
        if e.response is not None:
            return f"HTTP {e.response.status_code}"

    return f"{type(e).__name__}: {str(e)[:200]}"


def success(items):
    return {
        "available": True,
        "items": items,
        "error": ""
    }


def failure(e):
    return {
        "available": False,
        "items": [],
        "error": error_text(e)
    }


# =====================================
# COMPANY DATABASE
# =====================================

@st.cache_data(ttl=86400, show_spinner=False)
def company_names():

    r = requests.get(MASTER, timeout=25)
    r.raise_for_status()

    data = json.loads(gzip.decompress(r.content))

    return {
        x["trading_symbol"]: x["name"]
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
        return company_names().get(symbol, symbol)
    except Exception:
        return symbol


def clean_name(name):

    return re.sub(
        r"\b(limited|ltd|corporation|corp)\b\.?",
        "",
        str(name),
        flags=re.I
    ).strip(" .")


def normalize(text):

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(text).lower()
    ).strip()


def matches_company(text, symbol, name):

    text = " " + normalize(text) + " "

    aliases = [
        normalize(clean_name(name)),
        normalize(symbol)
    ]

    return any(
        len(a) >= 3 and f" {a} " in text
        for a in aliases
    )


# =====================================
# NEWS CLASSIFICATION V4
# =====================================

HIGH_RISK = [
    r"\bfraud\b",
    r"accounting irregularit",
    r"\binsolvency\b",
    r"\bdefaulted\b",
    r"trading suspension",
    r"forensic audit",
    r"sebi investigation",
    r"regulatory investigation",
    r"show.cause notice"
]

POSITIVE = [
    r"\bwins?\b.{0,35}\border\b",
    r"\bsecures?\b.{0,35}\bcontract\b",
    r"profit (rises?|jumps?|surges?|increases?)",
    r"beats? estimates",
    r"raises? guidance",
    r"credit rating upgrade",
    r"turns profitable",
    r"receives? regulatory approval",
    r"target (raised|increased|hiked)",
    r"target price (raised|increased|hiked)",
    r"raises? (its )?(price )?target",
    r"upgraded? to buy",
    r"upgraded? to outperform",
    r"strong earnings growth"
]

NEGATIVE = [
    r"profit (falls?|declines?|drops?|decreases?)",
    r"net loss widens?",
    r"misses? estimates",
    r"cuts? guidance",
    r"credit rating downgrade",
    r"order cancellation",
    r"contract terminated",
    r"regulatory penalty",
    r"target (cut|lowered|reduced|slashed)",
    r"target price (cut|lowered|reduced|slashed)",
    r"cuts? (its )?(price )?target",
    r"downgraded? to sell",
    r"downgraded? to underperform",
    r"weak earnings",
    r"profit warning"
]

EVENTS = {
    "BROKERAGE": [
        "price target", "target price",
        "target cut", "target raised",
        "brokerage", "outperform",
        "underperform", "downgrade", "upgrade"
    ],
    "QUARTERLY RESULTS": [
        "quarterly result", "financial result",
        "net profit", "net loss", "earnings"
    ],
    "ORDER / CONTRACT": [
        "order", "contract",
        "letter of award", "letter of intent"
    ],
    "REGULATORY": [
        "sebi", "penalty", "investigation",
        "regulatory", "show cause"
    ],
    "MANAGEMENT": [
        "resignation", "appointment",
        "managing director", "chief executive"
    ],
    "MERGER / ACQUISITION": [
        "merger", "acquisition",
        "demerger", "takeover"
    ],
    "CORPORATE ACTION": [
        "dividend", "bonus issue",
        "stock split", "buyback", "rights issue"
    ],
    "INVESTOR MEETING": [
        "investor meeting", "analyst meeting",
        "institutional investor"
    ],
    "BULK / BLOCK DEAL": [
        "bulk deal", "block deal"
    ],
    "FUND RAISING": [
        "fund raising", "fundraising",
        "qualified institutional placement"
    ]
}


def classify(text):

    text = str(text).lower()

    if any(re.search(p, text) for p in HIGH_RISK):
        return "HIGH RISK"

    pos = any(re.search(p, text) for p in POSITIVE)
    neg = any(re.search(p, text) for p in NEGATIVE)

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


def make_item(title, source, published, url,
              official=False, details=""):

    text = title + " " + details

    return {
        "title": title,
        "source": source,
        "time": published,
        "url": url,
        "impact": classify(text),
        "event": event_type(text),
        "official": official
    }


# =====================================
# GOOGLE NEWS
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def google_news(symbol, name):

    cutoff = now() - timedelta(hours=48)

    short = clean_name(name)

    queries = [
        f'"{short}" India when:2d',
        f'"{symbol}" NSE stock when:2d'
    ]

    items = []
    errors = []
    available = False

    for query in dict.fromkeys(queries):

        try:

            with session() as s:

                r = s.get(
                    "https://news.google.com/rss/search",
                    params={
                        "q": query,
                        "hl": "en-IN",
                        "gl": "IN",
                        "ceid": "IN:en"
                    },
                    timeout=10
                )

                r.raise_for_status()

            root = ET.fromstring(r.content)

            if root.find("channel") is None:
                raise ValueError("Invalid RSS response")

            available = True

            for item in root.findall(".//item"):

                try:

                    title = item.findtext("title", "")

                    if not matches_company(
                        title, symbol, name
                    ):
                        continue

                    published = parsedate_to_datetime(
                        item.findtext("pubDate", "")
                    ).astimezone(IST)

                    if not cutoff <= published <= now():
                        continue

                    items.append(make_item(
                        title,
                        item.findtext("source", "Google News"),
                        published.isoformat(),
                        item.findtext("link", "")
                    ))

                except Exception:
                    continue

            if items:
                break

        except Exception as e:
            errors.append(error_text(e))

    return {
        "available": available,
        "items": items[:30],
        "error": "; ".join(errors)
    }


# =====================================
# NSE OFFICIAL CONNECTION
# =====================================

def nse_request(path, params=None):

    with session() as s:

        s.headers.update({
            "Referer": NSE + "/"
        })

        home = s.get(NSE, timeout=8)
        home.raise_for_status()

        r = s.get(
            NSE + path,
            params=params,
            timeout=12
        )

        r.raise_for_status()

        try:
            return r.json()
        except ValueError:
            raise ValueError(
                "NSE returned non-JSON response"
            )


def extract_rows(data):

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for key in (
            "data", "Table", "bulkDeals",
            "blockDeals", "bulk_deals",
            "block_deals"
        ):

            if isinstance(data.get(key), list):
                return data[key]

    raise ValueError("Unrecognized response format")


def parse_time(value):

    value = str(value or "").strip()

    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%Y-%m-%d"
    ):

        try:
            return datetime.strptime(
                value, fmt
            ).replace(tzinfo=IST)
        except ValueError:
            continue

    try:

        d = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)

        return d.astimezone(IST)

    except Exception:
        return None


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
                "to_date": now().strftime("%d-%m-%Y")
            }
        )

        return success(extract_rows(data))

    except Exception as e:
        return failure(e)


def official_news(symbol):

    data = nse_announcements()
    items = []

    cutoff = now() - timedelta(hours=48)

    for row in data["items"]:

        if str(row.get("symbol", "")).upper() != symbol.upper():
            continue

        published = parse_time(
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

        details = str(row.get("attchmntText") or "")

        items.append(make_item(
            title + (" — " + details[:300] if details else ""),
            "NSE Corporate Announcements",
            published.isoformat(),
            row.get("attchmntFile") or NSE,
            True,
            details
        ))

    return {
        "available": data["available"],
        "items": items,
        "error": data["error"]
    }


# =====================================
# BSE ALTERNATIVE ANNOUNCEMENTS
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def bse_announcements():

    rows = []
    complete = False

    try:

        with session() as s:

            s.headers.update({
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com"
            })

            for page in range(1, 11):

                r = s.get(
                    BSE + "/BseIndiaAPI/api/AnnSubCategoryGetData/w",
                    params={
                        "pageno": page,
                        "strCat": "-1",
                        "strPrevDate": (
                            now() - timedelta(days=2)
                        ).strftime("%Y%m%d"),
                        "strScrip": "",
                        "strSearch": "P",
                        "strToDate": now().strftime("%Y%m%d"),
                        "strType": "C",
                        "subcategory": "-1"
                    },
                    timeout=12
                )

                r.raise_for_status()

                data = r.json()
                batch = extract_rows(data)

                if not batch:
                    complete = True
                    break

                rows.extend(batch)

                table1 = data.get("Table1", [])

                if table1:

                    total = table1[0].get("ROWCNT")

                    if total is not None:
                        if len(rows) >= int(total):
                            complete = True
                            break

        return {
            "available": complete,
            "items": rows,
            "error": (
                "" if complete
                else "BSE pagination incomplete"
            )
        }

    except Exception as e:

        return {
            "available": False,
            "items": rows,
            "error": error_text(e)
        }


def bse_news(symbol, name):

    data = bse_announcements()
    items = []

    cutoff = now() - timedelta(hours=48)

    for row in data["items"]:

        company = str(row.get("SLONGNAME", ""))

        if not matches_company(company, symbol, name):
            continue

        published = parse_time(
            row.get("NEWS_DT")
            or row.get("DT_TM")
            or row.get("DissemDT")
        )

        if published is None:
            continue

        if not cutoff <= published <= now():
            continue

        title = str(
            row.get("NEWSSUB")
            or row.get("HEADLINE")
            or "BSE Corporate Announcement"
        )

        attachment = str(row.get("ATTACHMENTNAME") or "")

        url = (
            "https://www.bseindia.com/xml-data/"
            "corpfiling/AttachLive/" + attachment
            if attachment
            else "https://www.bseindia.com/"
        )

        items.append(make_item(
            title,
            "BSE Corporate Announcements",
            published.isoformat(),
            url,
            True
        ))

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
                "to": now().strftime("%d-%m-%Y")
            }
        )

        return success(extract_rows(data))

    except Exception as e:
        return failure(e)


def pick(row, *keys):

    for key in keys:
        if row.get(key) is not None:
            return row[key]

    return ""


def deal_news(symbol, kind):

    data = deals_feed(kind)
    items = []

    for row in data["items"]:

        sym = pick(
            row, "symbol",
            "BD_SYMBOL", "BD_DT_SYMBOL"
        )

        if str(sym).upper() != symbol.upper():
            continue

        client = pick(
            row, "clientName", "BD_CLIENT_NAME"
        )

        side = pick(
            row, "buySell",
            "BD_BUY_SELL", "BD_BUY_SELL_TYPE"
        )

        qty = pick(
            row, "quantity", "BD_QTY_TRD"
        )

        price = pick(
            row, "tradePrice", "BD_TP_WATP"
        )

        date = pick(
            row, "date", "BD_DT_DATE", "BD_DATE"
        )

        published = parse_time(date)

        if published is None:
            continue

        if not (
            now().date() - timedelta(days=7)
            <= published.date()
            <= now().date()
        ):
            continue

        items.append({
            "title": (
                f"{kind} DEAL | {symbol} | "
                f"{client} | {side} | "
                f"Quantity: {qty} | Price: {price}"
            ),
            "source": "NSE Deal Data",
            "time": published.isoformat(),
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
        "items": items[:20],
        "error": data["error"]
    }


# =====================================
# COMBINED NEWS INTELLIGENCE
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(symbol):

    name = company_name(symbol)

    feeds = {
        "Google News": google_news(symbol, name),
        "NSE Announcements": official_news(symbol),
        "BSE Announcements": bse_news(symbol, name),
        "NSE Bulk Deals": deal_news(symbol, "BULK"),
        "NSE Block Deals": deal_news(symbol, "BLOCK")
    }

    sources = {
        k: v["available"]
        for k, v in feeds.items()
    }

    errors = {
        k: v["error"]
        for k, v in feeds.items()
    }

    unique = {}

    for source in (
        "NSE Announcements",
        "BSE Announcements",
        "Google News",
        "NSE Bulk Deals",
        "NSE Block Deals"
    ):

        for item in feeds[source]["items"]:

            key = normalize(item["title"])

            if key not in unique:
                unique[key] = item

    items = sorted(
        unique.values(),
        key=lambda x: x["time"],
        reverse=True
    )

    return {
        "available": any(sources.values()),
        "complete": all(sources.values()),
        "official_available": (
            sources["NSE Announcements"]
            or sources["BSE Announcements"]
        ),
        "company": name,
        "sources": sources,
        "errors": errors,
        "items": items,
        "checked": now().isoformat()
    }


# =====================================
# FINAL NEWS ASSESSMENT
# =====================================

def assess_news(symbol, side):

    side = str(side).upper()

    if side not in ("CE", "PE"):
        raise ValueError("Invalid option direction")

    data = fetch_news(symbol)
    items = data["items"]

    directional = [
        x for x in items
        if x["event"] not in (
            "BULK DEAL", "BLOCK DEAL"
        )
    ]

    flags = {
        x["impact"] for x in directional
    }

    conflict = (
        "HIGH RISK" in flags
        or "MIXED" in flags
        or (
            "POSITIVE" in flags
            and "NEGATIVE" in flags
        )
    )

    expected = (
        "POSITIVE" if side == "CE"
        else "NEGATIVE"
    )

    opposite_flag = (
        "NEGATIVE" if side == "CE"
        else "POSITIVE"
    )

    opposite = opposite_flag in flags
    aligned = expected in flags

    points = 0

    if conflict:
        status = "HIGH RISK / MIXED"

    elif opposite:
        status = "OPPOSITE NEWS"

    elif aligned:

        status = "ALIGNED HEADLINE"

        official_aligned = any(
            x["official"]
            and x["impact"] == expected
            for x in directional
        )

        brokerage_only = all(
            x["event"] == "BROKERAGE"
            for x in directional
            if x["impact"] == expected
        )

        if official_aligned:
            points = 10

        elif brokerage_only:
            points = 3

        else:
            points = 5

    elif not data["available"]:
        status = "NEWS FEED UNAVAILABLE"

    elif not items and not data["complete"]:
        status = "NEWS UNVERIFIED"

    elif not items:
        status = "NO RECENT MATCHING NEWS"

    else:
        status = "HEADLINES UNCLASSIFIED"

    return {
        "symbol": symbol,
        "company": data["company"],
        "direction": side,
        "status": status,
        "block": bool(conflict or opposite),
        "points": points,
        "items": items,
        "sources": data["sources"],
        "errors": data["errors"],
        "complete": data["complete"],
        "official_available": data["official_available"],
        "checked": data["checked"]
    }


# =====================================
# FULL MARKET NEWS SCANNER
# =====================================

def scan_news_market(
    symbols,
    start=0,
    batch_size=10
):

    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        return {
            "results": [],
            "next_cursor": 0,
            "total": 0
        }

    start = start % len(symbols)

    rotated = symbols[start:] + symbols[:start]

    batch = rotated[:max(1, batch_size)]

    results = []

    for symbol in batch:

        try:

            ce = assess_news(symbol, "CE")
            pe = assess_news(symbol, "PE")

            direction = "NEUTRAL"

            if ce["block"] and pe["block"]:
                direction = "HIGH RISK"

            elif ce["points"] > 0 and not ce["block"]:
                direction = "CE WATCH"

            elif pe["points"] > 0 and not pe["block"]:
                direction = "PE WATCH"

            elif not ce["complete"]:
                direction = "UNVERIFIED"

            reasons = [
                x["title"]
                for x in ce["items"]
                if x["impact"] != "REVIEW"
            ]

            results.append({
                "Symbol": symbol,
                "Company": ce["company"],
                "News Direction": direction,
                "CE Points": ce["points"],
                "PE Points": pe["points"],
                "CE Status": ce["status"],
                "PE Status": pe["status"],
                "Headlines": len(ce["items"]),
                "News Reason": " | ".join(reasons[:3]),
                "Official Coverage": ce["official_available"],
                "Sources": ce["sources"],
                "Errors": ce["errors"],
                "Checked": ce["checked"]
            })

        except Exception as e:

            results.append({
                "Symbol": symbol,
                "News Direction": "UNVERIFIED",
                "Error": error_text(e),
                "Checked": now().isoformat()
            })

    return {
        "results": results,
        "next_cursor": (
            start + len(batch)
        ) % len(symbols),
        "total": len(symbols)
    }
