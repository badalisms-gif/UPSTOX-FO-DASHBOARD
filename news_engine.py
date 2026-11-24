
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")


# =====================================
# AUTOMATIC NEWS FETCHING
# =====================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(symbol):

    if symbol in (
        "NIFTY",
        "BANKNIFTY",
        "FINNIFTY"
    ):
        query = (
            f"{symbol} India market when:2d"
        )

    else:
        query = (
            f'"{symbol}" NSE stock when:2d'
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
            timeout=8
        )

        r.raise_for_status()

        root = ET.fromstring(r.content)

        if root.find("channel") is None:
            raise ValueError("Invalid RSS")

        items = []

        for item in root.findall("./channel/item"):

            title = item.findtext(
                "title", ""
            )

            if not re.search(
                r"(?<!\w)"
                + re.escape(symbol)
                + r"(?!\w)",
                title,
                re.I
            ):
                continue

            try:

                date = parsedate_to_datetime(
                    item.findtext("pubDate", "")
                ).astimezone(IST)

            except Exception:
                continue

            age = datetime.now(IST) - date

            if not (
                timedelta(0)
                <= age
                <= timedelta(hours=48)
            ):
                continue

            items.append({
                "title": title,

                "source": item.findtext(
                    "source", "Unknown"
                ),

                "time": date.isoformat(),

                "url": item.findtext(
                    "link", ""
                )
            })

        return {
            "available": True,
            "items": items[:15]
        }

    except Exception:

        return {
            "available": False,
            "items": []
        }


# =====================================
# AUTOMATIC NEWS IMPACT ANALYSIS
# =====================================

def assess_news(symbol, side):

    data = fetch_news(symbol)

    flags = []

    for item in data["items"]:

        t = item["title"].lower()

        # HIGH RISK NEWS

        if re.search(
            r"\b(fraud|insolvency|default|"
            r"sebi ban|trading suspension|"
            r"regulatory probe|raid)\b",
            t
        ):

            flag = "HIGH RISK"

        # NEGATIVE NEWS

        elif re.search(
            r"\b(order cancellation|"
            r"profit falls|profit drops|"
            r"downgrade|weak results|"
            r"net loss)\b",
            t
        ):

            flag = "NEGATIVE"

        # POSITIVE NEWS

        elif re.search(
            r"\b(wins order|bags order|"
            r"profit rises|profit jumps|"
            r"upgrade|record profit)\b",
            t
        ):

            flag = "POSITIVE"

        else:

            flag = "REVIEW"

        flags.append(flag)

    # CONFLICTING NEWS PROTECTION

    conflict = (
        "HIGH RISK" in flags
        or (
            "POSITIVE" in flags
            and "NEGATIVE" in flags
        )
    )

    # OPPOSITE DIRECTION NEWS

    opposite = (
        "NEGATIVE" in flags
        if side == "CE"
        else "POSITIVE" in flags
    )

    # NEWS ALIGNED WITH TRADE

    aligned = (
        "POSITIVE" in flags
        if side == "CE"
        else "NEGATIVE" in flags
    )

    # FINAL NEWS STATUS

    if conflict:

        status = "HIGH RISK / MIXED"

    elif opposite:

        status = "OPPOSITE NEWS"

    elif aligned:

        status = "ALIGNED HEADLINE"

    elif not data["available"] or not flags:

        status = "NEWS UNVERIFIED"

    else:

        status = "HEADLINES UNCLASSIFIED"

    # NEWS SCORING

    points = (
        10
        if aligned
        and not conflict
        and not opposite
        else 0
    )

    return {

        "status": status,

        "block": bool(
            conflict or opposite
        ),

        "points": points,

        "items": data["items"],

        "checked": datetime.now(
            IST
        ).isoformat()
    }
