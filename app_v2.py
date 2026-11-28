
import os
import gzip
import json

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import streamlit as st

from institutional_upload import render as render_institutional
from news_engine import assess_news, scan_news_market
from backtest_engine import render_backtesting
from backtest_engine_v7 import render_backtesting_v7


st.set_page_config(
    page_title="NSE F&O PRO — ALL IN ONE V7",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"

INDEXES = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service"
}

MASTER = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)

NEWS_SOURCES = [
    "Google News",
    "NSE Announcements",
    "BSE Announcements",
    "NSE Bulk Deals",
    "NSE Block Deals"
]


# =====================================
# BASIC FUNCTIONS
# =====================================

def now():
    return datetime.now(IST)


def n(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except (ValueError, TypeError):
        return np.nan


def api(path, token, params=None):
    r = requests.get(
        API + path,
        params=params,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json"
        },
        timeout=15
    )

    if not r.ok:
        raise RuntimeError(
            f"Upstox HTTP {r.status_code}"
        )

    p = r.json()

    if p.get("status") != "success":
        raise RuntimeError("Upstox API error")

    return p.get("data")


@st.cache_data(ttl=86400, show_spinner=False)
def master():
    r = requests.get(MASTER, timeout=30)
    r.raise_for_status()
    return json.loads(gzip.decompress(r.content))


@st.cache_data(ttl=3600, show_spinner=False)
def instruments():
    m = master()

    underlying = {
        x.get("underlying_key")
        for x in m
        if x.get("segment") == "NSE_FO"
        and x.get("instrument_type") in ("CE", "PE")
    }

    return {
        x["instrument_key"]: x["trading_symbol"]
        for x in m
        if x.get("segment") == "NSE_EQ"
        and x.get("instrument_key") in underlying
    }


@st.cache_data(ttl=86400, show_spinner=False)
def lot_sizes():
    return {
        x.get("instrument_key"): n(x.get("lot_size"))
        for x in master()
        if x.get("segment") == "NSE_FO"
    }


def instrument_mapping():
    mapping = {
        symbol: key
        for key, symbol in instruments().items()
    }
    mapping.update(INDEXES)
    return mapping


# =====================================
# MARKET STATUS
# =====================================

@st.cache_data(ttl=30, show_spinner=False)
def exchange(token):
    try:
        d = api("/v2/market/status/NSE", token)
        return str(d.get("status", "UNKNOWN")).upper()
    except Exception:
        return "UNKNOWN"


def open_market(token):
    t = now()

    return bool(
        token
        and t.weekday() < 5
        and time(9, 15) <= t.time() < time(15, 30)
        and exchange(token) == "NORMAL_OPEN"
    )


def entry_window(token):
    return (
        open_market(token)
        and time(9, 30) <= now().time() <= time(14, 45)
    )


def stamp(x):
    try:
        if isinstance(x, (int, float)) or str(x).isdigit():
            value = float(x)
            unit = "ms" if abs(value) >= 1e11 else "s"

            return pd.to_datetime(
                value, unit=unit, utc=True
            ).tz_convert(IST)

        return pd.to_datetime(
            x, utc=True
        ).tz_convert(IST)

    except Exception:
        return pd.NaT


def recent(value, seconds):
    t = stamp(value)

    return bool(
        pd.notna(t)
        and -5 <= (
            now() - t.to_pydatetime()
        ).total_seconds() <= seconds
    )


def fresh(q, seconds=180):
    t = stamp(
        q.get("last_trade_time")
        or q.get("timestamp")
    )

    return bool(
        pd.notna(t)
        and t.date() == now().date()
        and -5 <= (
            now() - t.to_pydatetime()
        ).total_seconds() <= seconds
    )


def quotes(token, keys):
    out = {}
    keys = list(dict.fromkeys(keys))

    for i in range(0, len(keys), 40):
        data = api(
            "/v2/market-quote/quotes",
            token,
            {
                "instrument_key":
                ",".join(keys[i:i + 40])
            }
        )

        for q in (data or {}).values():
            key = q.get("instrument_token")

            if key:
                out[key] = q

    return out


# =====================================
# STEP 1: TRADING IDEAS GENERATOR
# =====================================

def ranking(token):
    m = instruments()
    rows = []
    errors = []
    received = set()

    for i in range(0, len(m), 40):
        try:
            data = quotes(
                token, list(m)[i:i + 40]
            )
        except Exception as e:
            errors.append(str(e))
            continue

        for k, q in data.items():
            if k not in m:
                continue

            received.add(k)

            p = n(q.get("last_price"))
            prev = p - n(q.get("net_change"))

            o = q.get("ohlc") or {}

            op, hi, lo = [
                n(o.get(a))
                for a in ("open", "high", "low")
            ]

            if (
                not np.isfinite(
                    [p, prev, op, hi, lo]
                ).all()
                or min(p, prev, op) <= 0
                or hi <= lo
            ):
                continue

            ch = 100 * (p / prev - 1)

            pos = np.clip(
                100 * (p - lo) / (hi - lo),
                0, 100
            )

            side = "NEUTRAL"

            if ch > .35 and p > op and pos >= 60:
                side = "CE"

            elif ch < -.35 and p < op and pos <= 40:
                side = "PE"

            score = 0

            if side != "NEUTRAL":
                score = (
                    min(abs(ch) / 2.5, 1) * 60
                    + (
                        pos if side == "CE"
                        else 100 - pos
                    ) * .4
                )

            rows.append({
                "Symbol": m[k],
                "Key": k,
                "Side": side,
                "LTP": p,
                "Change %": round(ch, 2),
                "Momentum Score": round(score, 1),
                "Fresh": fresh(q),
                "Volume": n(q.get("volume"))
            })

    frame = pd.DataFrame(rows)

    if not frame.empty:
        frame = frame.sort_values(
            "Momentum Score",
            ascending=False
        )

    return frame, len(m), len(received), errors


# =====================================
# HISTORICAL AND LIVE CANDLES
# =====================================

def parse(d):
    rows = []

    for c in (d or {}).get("candles", []):
        try:
            rows.append([
                pd.to_datetime(
                    c[0], utc=True
                ).tz_convert(IST),
                *map(float, c[1:6])
            ])
        except Exception:
            continue

    frame = pd.DataFrame(
        rows,
        columns=[
            "ts", "open", "high",
            "low", "close", "volume"
        ]
    )

    if frame.empty:
        return frame

    return (
        frame.drop_duplicates("ts")
        .set_index("ts")
        .sort_index()
    )


@st.cache_data(ttl=1800, show_spinner=False)
def history(token, key, day, days=15):
    end = day - timedelta(days=1)
    start = day - timedelta(days=days)

    return parse(
        api(
            f"/v3/historical-candle/"
            f"{quote(key, safe='')}/"
            f"minutes/1/{end}/{start}",
            token
        )
    )


def raw(token, key):
    h = history(token, key, now().date())

    c = parse(
        api(
            f"/v3/historical-candle/intraday/"
            f"{quote(key, safe='')}/minutes/1",
            token
        )
    )

    frames = [
        f for f in (h, c)
        if not f.empty
    ]

    if not frames:
        raise RuntimeError("No candles")

    d = pd.concat(frames).sort_index()
    d = d[~d.index.duplicated(keep="last")]

    return d.between_time("09:15", "15:29")


def bars(d, m, cut):
    r = d.resample(
        f"{m}min",
        origin="start_day",
        offset="15min"
    )

    b = r.agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    return b[
        (r.close.count() == m)
        & (
            b.index + pd.Timedelta(minutes=m)
            <= cut
        )
    ].dropna()


# =====================================
# STEP 2: TECHNICAL INDICATORS
# =====================================

def indicators(d):
    d = d.copy()

    for p in (9, 21, 50):
        d[f"ema{p}"] = d.close.ewm(
            span=p, adjust=False
        ).mean()

    z = d.close.diff()

    g = z.clip(lower=0).ewm(
        alpha=1/9, adjust=False
    ).mean()

    l = (-z.clip(upper=0)).ewm(
        alpha=1/9, adjust=False
    ).mean()

    d["rsi"] = 100 - 100 / (
        1 + g / l.replace(0, np.nan)
    )

    d.loc[(l == 0) & (g > 0), "rsi"] = 100
    d.loc[(l == 0) & (g == 0), "rsi"] = 50

    tr = pd.concat([
        d.high - d.low,
        (d.high - d.close.shift()).abs(),
        (d.low - d.close.shift()).abs()
    ], axis=1).max(axis=1)

    d["atr"] = tr.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    up = d.high.diff()
    dn = -d.low.diff()

    plus = np.where(
        (up > dn) & (up > 0), up, 0
    )

    minus = np.where(
        (dn > up) & (dn > 0), dn, 0
    )

    for name, arr in [
        ("plus", plus),
        ("minus", minus)
    ]:
        d[name] = (
            100 * pd.Series(
                arr, index=d.index
            ).ewm(
                alpha=1/14,
                adjust=False
            ).mean()
            / d.atr.replace(0, np.nan)
        )

    dx = (
        100 * (d.plus - d.minus).abs()
        / (d.plus + d.minus).replace(0, np.nan)
    )

    d["adx"] = dx.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    d["rvol"] = (
        d.volume
        / d.volume.shift().rolling(20)
        .mean().replace(0, np.nan)
    )

    return d


# =====================================
# STEP 6: FAKE BREAKOUT PROTECTION
# =====================================

def technical(token, key):
    cut = now()
    d = raw(token, key)

    b, b15, b60 = [
        indicators(bars(d, m, cut))
        for m in (5, 15, 60)
    ]

    if min(len(b), len(b15), len(b60)) < 21:
        raise RuntimeError("Insufficient history")

    a = b.iloc[-1]
    h = b15.iloc[-1]
    j = b60.iloc[-1]

    if (
        a.name.date() != cut.date()
        or cut - a.name.to_pydatetime()
        > timedelta(minutes=10)
    ):
        raise RuntimeError("Stale candle")

    today = b[b.index.date == cut.date()]
    opening = today.iloc[:3]

    expected = [
        time(9, 15),
        time(9, 20),
        time(9, 25)
    ]

    if (
        len(opening) < 3
        or list(opening.index.time) != expected
    ):
        raise RuntimeError("Opening range missing")

    side = "NEUTRAL"

    if a.close > a.ema9 > a.ema21:
        side = "CE"

    elif a.close < a.ema9 < a.ema21:
        side = "PE"

    s = 1 if side == "CE" else -1

    if s == 1:
        level = max(
            opening.high.max(),
            b.high.iloc[-6:-1].max()
        )
    else:
        level = min(
            opening.low.min(),
            b.low.iloc[-6:-1].min()
        )

    body = abs(a.close - a.open)
    rng = a.high - a.low

    closepos = (
        (a.close - a.low) / rng
        if rng > 0 else .5
    )

    vwap = (
        (
            today.volume
            * (today.high + today.low + today.close)
            / 3
        ).sum() / today.volume.sum()
        if today.volume.sum() > 0
        else np.nan
    )

    idx = key.startswith("NSE_INDEX|")

    volok = (
        bool(
            a.rvol >= 1.3
            and s * (a.close - vwap) > 0
        )
        if not idx else None
    )

    checks = {
        "Trend": side != "NEUTRAL",

        "Higher TF": (
            s * (h.close - h.ema21) > 0
            and s * (j.close - j.ema21) > 0
        ),

        "Momentum": (
            a.rsi >= 55 and a.plus > a.minus
            if s == 1
            else a.rsi <= 45 and a.minus > a.plus
        ),

        "ADX": a.adx >= 25,

        "Breakout": (
            s * (a.close - level) > .05 * a.atr
        ),

        "Candle quality": (
            body >= .35 * a.atr
            and s * (a.close - a.open) > 0
            and (
                closepos >= .7
                if s == 1 else closepos <= .3
            )
        ),

        "Not extended": (
            abs(a.close - a.ema9) <= 2 * a.atr
        ),

        "Spot volume/VWAP": volok
    }

    metrics = {
        "RSI": round(a.rsi, 2),
        "ADX": round(a.adx, 2),
        "RVOL": round(a.rvol, 2),
        "VWAP": vwap,
        "ATR": a.atr,
        "EMA 9": a.ema9,
        "EMA 21": a.ema21,
        "EMA 50": a.ema50,
        "DI Plus": a.plus,
        "DI Minus": a.minus,
        "Spot Close": a.close,
        "Opening High": opening.high.max(),
        "Opening Low": opening.low.min(),
        "Breakout Level": level,
        "Technical Candle": str(a.name)
    }

    return side, checks, metrics


# =====================================
# STEP 4: OPTION CHAIN AND GREEKS
# =====================================

@st.cache_data(ttl=900, show_spinner=False)
def expiry(token, key, day):
    d = api(
        "/v2/option/contract",
        token,
        {"instrument_key": key}
    ) or []

    dates = sorted({
        x["expiry"]
        for x in d
        if x.get("expiry", "") > day.isoformat()
    })

    return dates[0] if dates else None


def chain(token, key, exp):
    d = api(
        "/v2/option/chain",
        token,
        {
            "instrument_key": key,
            "expiry_date": exp
        }
    ) or []

    rows = []

    for x in d:
        for side, field in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            o = x.get(field) or {}
            m = o.get("market_data") or {}
            g = o.get("option_greeks") or {}

            bid = n(m.get("bid_price"))
            ask = n(m.get("ask_price"))

            rows.append({
                "Key": o.get("instrument_key"),
                "Side": side,
                "Strike": n(x.get("strike_price")),
                "Spot": n(x.get("underlying_spot_price")),
                "LTP": n(m.get("ltp")),
                "Bid": bid,
                "Ask": ask,

                "Spread %": (
                    200 * (ask - bid) / (ask + bid)
                    if ask >= bid > 0
                    else np.nan
                ),

                "OI": n(m.get("oi")),
                "Previous OI": n(m.get("prev_oi")),
                "Volume": n(m.get("volume")),
                "Delta": n(g.get("delta")),
                "Theta": n(g.get("theta")),
                "Gamma": n(g.get("gamma")),
                "Vega": n(g.get("vega")),
                "IV": n(g.get("iv"))
            })

    if not rows:
        return pd.DataFrame()

    c = pd.DataFrame(rows).dropna(
        subset=["Key"]
    )

    c["Daily OI Change"] = (
        c.OI - c["Previous OI"]
    )

    return c


def summary(c):
    ce = c[c.Side == "CE"]
    pe = c[c.Side == "PE"]

    co = ce.OI.sum(min_count=1)
    po = pe.OI.sum(min_count=1)

    pcr = po / co if co > 0 else np.nan

    pain = {}

    for k in c.Strike.dropna().unique():
        pain[k] = (
            (
                (k - ce.Strike).clip(lower=0)
                * ce.OI
            ).sum()
            + (
                (pe.Strike - k).clip(lower=0)
                * pe.OI
            ).sum()
        )

    max_pain = (
        min(pain, key=pain.get)
        if pain and co > 0 and po > 0
        else np.nan
    )

    return pcr, max_pain


# =====================================
# SESSION STORAGE
# =====================================

DEFAULTS = {
    "ranking": pd.DataFrame(),
    "results": [],
    "diagnostics": [],
    "snapshots": {},
    "chains": {},
    "chain_times": {},
    "signals": {},
    "journal": [],
    "last_scan": None,
    "coverage": "—",
    "fresh_coverage": 0,
    "cursor": 0,
    "news_cursor": 0,
    "news": {},
    "news_market": {},
    "news_total": 0,
    "news_last_scan": None,
    "scan_count": 0,
    "deep_scanned": set(),
    "session_date": now().date()
}

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

S = st.session_state


def reset_day():
    if S.session_date == now().date():
        return

    for v in S.signals.values():
        if v["Status"] == "ACTIVE":
            v["Status"] = (
                "SESSION ENDED / UNRESOLVED"
            )

    for k, v in DEFAULTS.items():
        if k not in ("signals", "journal"):
            S[k] = v

    S.session_date = now().date()


reset_day()


# =====================================
# DASHBOARD SETTINGS
# =====================================

st.title("NSE F&O PRO — ALL IN ONE V7")

st.caption(
    "Momentum + News Scanner | "
    "Technical Analysis | Option Intelligence | "
    "V6 Historical Backtesting | V7 Advanced Research"
)

with st.sidebar:
    st.header("Upstox Connection")

    token = st.text_input(
        "Upstox Access Token",
        value=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        type="password"
    ).strip()

    st.header("Signal Verification Mode")

    mode = st.radio(
        "Select Trading Research Mode",
        [
            "Technical + News",
            "Strict Verified"
        ],
        index=0
    )

    if mode == "Strict Verified":
        st.info(
            "Google News and at least one "
            "official exchange announcement "
            "feed are required."
        )
    else:
        st.warning(
            "Official exchange news may be "
            "unavailable. Such signals are "
            "marked PARTIALLY VERIFIED."
        )

    st.header("Risk Management")

    capital = st.number_input(
        "Capital ₹",
        min_value=1000,
        value=100000,
        step=10000
    )

    daily = st.number_input(
        "Daily risk ₹",
        min_value=100,
        value=5000,
        step=500
    )

    used = st.number_input(
        "Risk already used ₹",
        min_value=0,
        value=0,
        step=100
    )

    per = st.number_input(
        "Risk per trade ₹",
        min_value=100,
        value=1500,
        step=100
    )

    rr = st.slider(
        "Reward / risk",
        2.0, 4.0, 2.0, .25
    )

    st.header("Option Filters")

    minvol = st.number_input(
        "Minimum option volume",
        min_value=100,
        value=10000,
        step=1000
    )

    spread = st.slider(
        "Maximum spread %",
        .5, 5.0, 2.0, .5
    )

    iv = st.slider(
        "Maximum IV %",
        10, 100, 45
    )

    count = st.slider(
        "Deep scan per side per cycle",
        1, 20, 5
    )

    st.header("Advanced News Engine")

    news_batch = st.slider(
        "News stocks per cycle",
        1, 20, 5
    )

    news_priority = st.slider(
        "News priority stocks per cycle",
        1, 10, 3
    )

    news_auto = st.toggle(
        "Automatic market news scanning",
        value=True
    )

    auto = st.toggle(
        "Auto scan (120 seconds)",
        value=True
    )

    st.header("Navigation")

    page = st.radio(
        "Select Section",
        [
            "Home",
            "Market Scanner",
            "Option Chain & Greeks",
            "Deep Analysis",
            "News & Events",
            "Signal History",
            "Backtesting",
            "Trading Journal",
            "Institutional Intelligence"
        ]
    )

    st.caption(
        "Research only • No automatic orders "
        "• Expiry day excluded"
    )


def budget():
    return min(
        per,
        max(0, daily - used)
    )


# =====================================
# STEP 3: ADVANCED NEWS INTELLIGENCE
# =====================================

def automatic_news(symbol, side):
    try:
        result = dict(
            assess_news(symbol, side)
        )

    except Exception as e:
        result = {
            "status": "NEWS UNVERIFIED",
            "block": False,
            "points": 0,
            "items": [],
            "sources": {},
            "errors": {"Engine": str(e)},
            "complete": False,
            "checked": now().isoformat()
        }

    result["symbol"] = symbol
    result["direction"] = side

    S.news[symbol + "|" + side] = result

    return result


def verification(result):
    sources = result.get("sources", {})

    google = bool(
        sources.get("Google News", False)
    )

    nse = bool(
        sources.get("NSE Announcements", False)
    )

    bse = bool(
        sources.get("BSE Announcements", False)
    )

    official = nse or bse

    if not google:
        return "NEWS UNVERIFIED"

    if result.get("symbol") in INDEXES:
        return "PARTIALLY VERIFIED"

    if official:
        return "FULLY VERIFIED"

    return "PARTIALLY VERIFIED"


def news_feed_ok(result):
    status = verification(result)

    if status == "NEWS UNVERIFIED":
        return False

    if mode == "Strict Verified":
        return status == "FULLY VERIFIED"

    return True


def new_batch_size():
    return int(news_batch)


def news_cycle():
    symbols = sorted(
        set(instruments().values())
    )

    S.news_total = len(symbols)

    if not symbols:
        return

    result = scan_news_market(
        symbols,
        start=S.news_cursor,
        batch_size=new_batch_size()
    )

    for row in result.get("results", []):
        row = dict(row)

        sources = row.get("Sources") or {}

        if not isinstance(sources, dict):
            sources = {}

        row["Sources"] = sources

        row["Verification"] = verification({
            "symbol": row.get("Symbol"),
            "sources": sources
        })

        if row.get("News Direction") == "NEUTRAL":

            if row["Verification"] == "NEWS UNVERIFIED":
                row["News Direction"] = "UNVERIFIED"

            elif not (
                sources.get("NSE Announcements")
                or sources.get("BSE Announcements")
            ):
                row["News Direction"] = (
                    "NO DIRECTION / PARTIAL DATA"
                )

        if row.get("Symbol"):
            S.news_market[row["Symbol"]] = row

    S.news_cursor = result.get("next_cursor", 0)
    S.news_last_scan = now()


def news_frame():
    if not S.news_market:
        return pd.DataFrame()

    return pd.DataFrame(
        list(S.news_market.values())
    )


def news_candidates(r):
    candidates = []

    if r.empty:
        return candidates

    lookup = {
        row["Symbol"]: row
        for row in r.to_dict("records")
    }

    for symbol, item in S.news_market.items():
        direction = item.get("News Direction", "")

        if direction not in ("CE WATCH", "PE WATCH"):
            continue

        if not recent(item.get("Checked"), 1800):
            continue

        if not news_feed_ok({
            "symbol": symbol,
            "sources": item.get("Sources", {})
        }):
            continue

        row = lookup.get(symbol)

        if not row or not row.get("Fresh"):
            continue

        side = direction.split()[0]

        points = n(
            item.get(side + " Points", 0)
        )

        candidates.append({
            **row,
            "Side": side,
            "Origin": "NEWS",
            "News Priority": (
                points if np.isfinite(points) else 0
            )
        })

    candidates.sort(
        key=lambda x: (
            x["News Priority"],
            x["Momentum Score"]
        ),
        reverse=True
    )

    return candidates[:news_priority]


def show_news(result):
    status = verification(result)

    st.write(
        "Direction:",
        result.get("direction", "—")
    )

    st.write(
        "News status:",
        result.get("status", "UNVERIFIED")
    )

    st.write(
        "News points:",
        result.get("points", 0)
    )

    if status == "FULLY VERIFIED":
        st.success(
            "FULLY VERIFIED — required feeds available"
        )

    elif status == "PARTIALLY VERIFIED":
        st.warning(
            "PARTIALLY VERIFIED — official "
            "announcement coverage unavailable"
        )

    else:
        st.error("NEWS UNVERIFIED")

    st.caption(
        "Checked: "
        + str(result.get("checked", "—"))
    )

    sources = result.get("sources", {})
    errors = result.get("errors", {})

    for source in NEWS_SOURCES:
        available = sources.get(source)

        if available is True:
            st.write(
                "✅ " + source + " — Available"
            )

        elif available is False:
            st.write(
                "⚠️ " + source + " — Unavailable"
            )

        else:
            st.write(
                "➖ " + source + " — Not verified"
            )

    if result.get("block"):
        st.error(
            "Adverse or mixed news detected."
        )

    if not result.get("complete", False):
        st.warning(
            "News source coverage is incomplete."
        )

    if not result.get("items"):
        st.info(
            "No matching headlines returned. "
            "This does not establish absence of news."
        )

    for item in result.get("items", []):
        st.write(item.get("title", ""))

        st.caption(
            f"{item.get('source', '')} | "
            f"{item.get('time', '')} | "
            f"{item.get('event', '')} | "
            f"{item.get('impact', '')}"
        )

        url = str(item.get("url", ""))

        if url.startswith("https://"):
            st.link_button("Read source", url)

    failures = {
        k: v
        for k, v in errors.items()
        if v
    }

    if failures:
        with st.expander("News source diagnostics"):
            st.write(failures)


# =====================================
# STEP 5: MASTER SCORING SYSTEM
# =====================================

def option_setup(
    o, checks, metrics,
    c, exp, symbol, old, news, origin,
    snapshot_time
):
    q = quotes(
        token, [o.Key]
    ).get(o.Key, {})

    if not fresh(q):
        raise RuntimeError("Option quote stale")

    b = indicators(
        bars(raw(token, o.Key), 5, now())
    )

    if len(b) < 30:
        raise RuntimeError(
            "Insufficient option history"
        )

    a = b.iloc[-1]

    if (
        a.name.date() != now().date()
        or now() - a.name.to_pydatetime()
        > timedelta(minutes=10)
    ):
        raise RuntimeError("Option candles stale")

    q = quotes(
        token, [o.Key]
    ).get(o.Key, {})

    if not fresh(q):
        raise RuntimeError("Final quote stale")

    depth = q.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    bid = n(
        buys[0].get("price")
        if buys else None
    )

    entry = n(
        sells[0].get("price")
        if sells else None
    )

    if not (
        np.isfinite([bid, entry]).all()
        and entry >= bid > 0
    ):
        raise RuntimeError(
            "Live bid/ask unavailable"
        )

    live_spread = (
        200 * (entry - bid) / (entry + bid)
    )

    if live_spread > spread:
        raise RuntimeError("Live spread too wide")

    atr = n(a.atr)

    if not np.isfinite(atr) or atr <= 0:
        raise RuntimeError("ATR missing")

    prev = old.get(o.Key)

    elapsed = (
        (snapshot_time - prev["time"]).total_seconds()
        if prev else 0
    )

    comparable = (
        prev is not None
        and 20 <= elapsed <= 600
        and prev["time"].date() == now().date()
    )

    oi = (
        o.OI - prev["OI"]
        if comparable else np.nan
    )

    vol = (
        o.Volume - prev["Volume"]
        if comparable else np.nan
    )

    prem = (
        o.LTP - prev["LTP"]
        if comparable else np.nan
    )

    opposite = c[
        (c.Side != o.Side)
        & (c.Strike == o.Strike)
    ]

    opp = (
        opposite.iloc[0]
        if len(opposite) else None
    )

    oprev = (
        old.get(opp.Key)
        if opp is not None else None
    )

    oppchange = (
        opp.OI - oprev["OI"]
        if oprev
        and 20 <= (
            snapshot_time - oprev["time"]
        ).total_seconds() <= 600
        and oprev["time"].date() == now().date()
        else np.nan
    )

    pcr, pain = summary(c)

    risk = max(
        1.5 * atr,
        entry - b.low.iloc[-3:].min()
    )

    sl = entry - risk

    lot = n(lot_sizes().get(o.Key))

    if (
        not np.isfinite(lot)
        or lot <= 0
        or sl <= 0
        or risk / entry > .25
    ):
        raise RuntimeError("Risk or lot invalid")

    lots = max(
        0,
        int(
            min(
                budget() / (risk * lot),
                capital / (entry * lot)
            )
        )
    )

    premium = bool(
        a.close > a.ema9 > a.ema21
        and a.rsi >= 52
        and a.close > b.close.iloc[-2]
        and prem > 0
    )

    greek = bool(
        np.isfinite([
            o.Delta,
            o.Theta,
            o.Gamma,
            o.Vega,
            o.IV
        ]).all()
        and abs(o.Theta) / entry <= .15
        and o.Gamma >= 0
        and o.Vega >= 0
    )

    groups = {
        "Spot direction": all(
            bool(v)
            for v in checks.values()
            if v is not None
        ),

        "Premium momentum": premium,

        "Live OI increase": bool(oi > 0),

        "Fresh volume": bool(
            vol > 0 and a.rvol >= 1.1
        ),

        "Opposite OI observed": bool(
            np.isfinite(oppchange)
        ),

        "Delta": (
            .45 <= abs(o.Delta) <= .70
        ),

        "IV / Greeks": (
            greek and 0 < o.IV <= iv
        )
    }

    weights = [25, 15, 15, 15, 5, 10, 15]

    base_score = sum(
        w for w, v in zip(
            weights, groups.values()
        )
        if v
    )

    points = n(news.get("points", 0))

    news_points = (
        min(10, max(0, points))
        if np.isfinite(points) else 0
    )

    score = round(
        base_score * .90 + news_points, 1
    )

    verified = verification(news)

    safety = {
        "Live spread": live_spread <= spread,

        "Entry not extended": (
            abs(entry - a.close) <= .5 * atr
        ),

        "Affordable position": lots > 0,

        "Entry window": entry_window(token),

        "News feed requirement": news_feed_ok(news),

        "News assessment recent": recent(
            news.get("checked"), 300
        ),

        "Chain snapshot recent": recent(
            snapshot_time.isoformat(), 180
        ),

        "Spot candle recent": recent(
            metrics.get("Technical Candle"), 600
        ),

        "No adverse headline detected":
            not news.get("block", False)
    }

    safe = (
        all(groups.values())
        and all(safety.values())
        and score >= 90
    )

    return {
        "Symbol": symbol,
        "Key": o.Key,
        "Side": o.Side,
        "Strike": o.Strike,
        "Expiry": exp,
        "Origin": origin,
        "Verification": verified,
        "Research Mode": mode,
        "Score": score,

        "Technical & Option Score":
            round(base_score * .90, 1),

        "News Points": news_points,

        "Status": (
            "QUALIFIED" if safe else "WAIT"
        ),

        "Entry": round(entry, 2),
        "SL": round(sl, 2),

        "Target": round(
            entry + rr * risk, 2
        ),

        "Target 2": round(
            entry + (rr + 1) * risk, 2
        ),

        "Reward / Risk": rr,
        "Lots": lots,
        "Lot Size": int(lot),

        "Planned Risk": round(
            lots * lot * risk, 2
        ),

        "Capital": round(
            lots * lot * entry, 2
        ),

        "Current Premium": n(q.get("last_price")),
        "Live Bid": bid,
        "Live Ask": entry,
        "Delta": o.Delta,
        "Theta": o.Theta,
        "Gamma": o.Gamma,
        "Vega": o.Vega,
        "IV": o.IV,
        "OI": o.OI,
        "OI Change": oi,
        "Daily OI Change": o["Daily OI Change"],
        "Fresh Volume": vol,
        "Total Volume": o.Volume,
        "Premium Change": prem,
        "Opposite OI Change": oppchange,
        "PCR": pcr,
        "Max Pain": pain,
        "Spread %": round(live_spread, 2),
        "Premium RSI": a.rsi,
        "Premium ATR": atr,
        "Premium RVOL": a.rvol,
        "News Review": news.get("status"),
        "News Checked": news.get("checked"),
        "Chain Checked": snapshot_time.isoformat(),
        "Time": now().isoformat(),
        "Last Quote Check": now().isoformat(),
        "News Snapshot": news,

        "Checks": {
            **checks,
            **groups,
            **safety
        },

        **metrics
    }


# =====================================
# LIVE SIGNAL TRACKING
# =====================================

def track():
    active = [
        v for v in S.signals.values()
        if v["Status"] == "ACTIVE"
        and v["Time"][:10]
        == now().date().isoformat()
    ]

    if not active:
        return

    if not open_market(token):
        if now().time() >= time(15, 30):
            for v in active:
                v["Status"] = (
                    "SESSION ENDED / UNRESOLVED"
                )
        return

    data = quotes(
        token,
        [v["Key"] for v in active]
    )

    for v in active:
        q = data.get(v["Key"], {})
        p = n(q.get("last_price"))

        if not fresh(q) or not np.isfinite(p):
            continue

        v["Current Premium"] = p
        v["Last Quote Check"] = now().isoformat()

        v["Observed P&L"] = round(
            (p - v["Entry"])
            * v["Lots"]
            * v["Lot Size"],
            2
        )

        if p <= v["SL"]:
            v["Status"] = "SL OBSERVED"
            v["Exit Observation"] = p
            v["Exit Time"] = now().isoformat()

        elif p >= v["Target"]:
            v["Status"] = "TARGET OBSERVED"
            v["Exit Observation"] = p
            v["Exit Time"] = now().isoformat()

        elif now().time() >= time(15, 20):
            v["Status"] = "EOD OBSERVED"
            v["Exit Observation"] = p
            v["Exit Time"] = now().isoformat()


# =====================================
# COMBINED MARKET SCANNER
# =====================================

def scan():
    S.results = []
    S.diagnostics = []

    r, total, received, errors = ranking(token)

    S.ranking = r
    S.coverage = f"{received}/{total}"

    S.fresh_coverage = (
        int(r.Fresh.sum())
        if not r.empty else 0
    )

    S.diagnostics = list(errors)

    if (
        r.empty
        or not entry_window(token)
        or budget() <= 0
    ):
        S.last_scan = now()
        return

    selected = news_candidates(r)

    for side in ("CE", "PE"):
        z = r[
            (r.Side == side)
            & r.Fresh
        ]

        if z.empty:
            continue

        top = z.head(1)
        pool = z.iloc[1:]

        start = (
            S.cursor * max(1, count - 1)
        ) % max(1, len(pool))

        rotated = pd.concat([
            pool.iloc[start:],
            pool.iloc[:start]
        ])

        chosen = pd.concat([
            top,
            rotated.head(max(0, count - 1))
        ])

        for row in chosen.to_dict("records"):
            selected.append({
                **row,
                "Origin": "MOMENTUM"
            })

    S.cursor += 1

    selected.extend({
        "Symbol": k,
        "Key": v,
        "Side": "AUTO",
        "Origin": "INDEX"
    } for k, v in INDEXES.items())

    unique = {}

    for row in selected:
        key = row["Key"]

        if key not in unique:
            unique[key] = row

        else:
            if unique[key]["Side"] != row["Side"]:
                unique[key]["Side"] = "AUTO"

            unique[key]["Origin"] = (
                "NEWS + MOMENTUM"
            )

    selected = list(unique.values())
    progress = st.progress(0)

    for i, row in enumerate(selected):
        sym = row["Symbol"]
        key = row["Key"]

        if not entry_window(token):
            break

        try:
            side, checks, metrics = technical(
                token, key
            )

            S.deep_scanned.add(sym)

            if (
                side == "NEUTRAL"
                or row["Side"] not in ("AUTO", side)
            ):
                raise RuntimeError("Trend conflict")

            failed = [
                k for k, v in checks.items()
                if v is not None and not bool(v)
            ]

            exp = expiry(
                token, key, now().date()
            )

            if not exp:
                raise RuntimeError("No eligible expiry")

            c = chain(token, key, exp)

            if c.empty:
                raise RuntimeError("Empty option chain")

            snapshot_time = now()
            snapshot_key = key + "|" + exp

            old = S.snapshots.get(
                snapshot_key, {}
            )

            S.chains[sym] = c
            S.chain_times[sym] = (
                snapshot_time.isoformat()
            )

            S.snapshots[snapshot_key] = {
                x.Key: {
                    "time": snapshot_time,
                    "OI": x.OI,
                    "Volume": x.Volume,
                    "LTP": x.LTP
                }
                for _, x in c.iterrows()
            }

            if failed:
                raise RuntimeError(
                    "Failed: " + ", ".join(failed)
                )

            news = automatic_news(sym, side)

            if news.get("block"):
                raise RuntimeError(
                    "Blocked by adverse news"
                )

            if not news_feed_ok(news):
                raise RuntimeError(
                    "Required news feeds unavailable"
                )

            z = c[
                (c.Side == side)
                & c.Delta.abs().between(.45, .70)
                & (c.Volume >= minvol)
                & (c["Spread %"] <= spread)
                & (c.IV > 0)
                & (c.IV <= iv)
                & (c.OI > 0)
                & (c.Ask > 0)
                & (c.LTP > 0)
            ].copy()

            z = z[
                (z.Delta > 0)
                if side == "CE"
                else (z.Delta < 0)
            ]

            if z.empty:
                raise RuntimeError("No eligible option")

            z["Distance"] = (
                z.Delta.abs() - .55
            ).abs()

            options = z.sort_values(
                ["Spread %", "Distance", "Volume"],
                ascending=[True, True, False]
            ).head(3)

            for _, o in options.iterrows():
                try:
                    v = option_setup(
                        o,
                        checks,
                        metrics,
                        c,
                        exp,
                        sym,
                        old,
                        news,
                        row["Origin"],
                        snapshot_time
                    )

                    S.results.append(v)

                    if v["Status"] == "QUALIFIED":
                        ident = (
                            now().date().isoformat()
                            + "|" + sym
                        )

                        if ident not in S.signals:
                            S.signals[ident] = {
                                **v,
                                "Status": "ACTIVE",
                                "Observed P&L": 0
                            }

                        break

                    failed = [
                        k for k, passed
                        in v["Checks"].items()
                        if passed is not None
                        and not bool(passed)
                    ]

                    S.diagnostics.append(
                        f"{sym} {o.Strike:g} "
                        f"{side}: "
                        + ", ".join(failed)
                    )

                except Exception as e:
                    S.diagnostics.append(
                        f"{sym} option: {e}"
                    )

        except Exception as e:
            S.diagnostics.append(
                f"{sym}: {e}"
            )

        progress.progress(
            (i + 1) / len(selected)
        )

    progress.empty()

    S.last_scan = now()
    S.scan_count += 1


# =====================================
# DISPLAY HELPERS
# =====================================

def flat_records(records):
    return pd.DataFrame([
        {
            k: x
            for k, x in v.items()
            if k not in (
                "Checks",
                "News Snapshot",
                "Key"
            )
        }
        for v in records
    ])


def show_news_market():
    st.header("News-Based Opportunities")

    total = S.news_total
    reviewed = len(S.news_market)

    st.metric(
        "News Stocks Attempted",
        f"{reviewed}/{total}" if total else "0"
    )

    if total:
        st.progress(
            min(reviewed / total, 1.0)
        )

    d = news_frame()

    if d.empty:
        st.info(
            "Run a news scan to populate this section."
        )
        return

    valid = d[
        d["Checked"].apply(
            lambda x: recent(x, 1800)
        )
    ] if "Checked" in d else d.iloc[:0]

    st.caption(
        "Priority watchlist uses assessments "
        "from the last 30 minutes."
    )

    left, right = st.columns(2)

    for col, side in [
        (left, "CE"),
        (right, "PE")
    ]:
        col.subheader(
            "NEWS " + side + " WATCH"
        )

        z = valid[
            valid["News Direction"]
            == side + " WATCH"
        ]

        if not z.empty:
            cols = [
                "Symbol",
                "Company",
                side + " Points",
                "Verification",
                "Headlines",
                "Checked"
            ]

            col.dataframe(
                z[
                    [k for k in cols if k in z]
                ].sort_values(
                    side + " Points",
                    ascending=False
                ),
                hide_index=True,
                use_container_width=True
            )

        else:
            col.info(
                "No recent aligned news candidate."
            )

    st.subheader("News Source Coverage")

    source_rows = []

    for source in NEWS_SOURCES:
        available = 0
        unavailable = 0
        unknown = 0

        for item in S.news_market.values():
            status = item.get(
                "Sources", {}
            ).get(source)

            if status is True:
                available += 1

            elif status is False:
                unavailable += 1

            else:
                unknown += 1

        source_rows.append({
            "Source": source,
            "Available checks": available,
            "Unavailable checks": unavailable,
            "Unknown": unknown
        })

    st.dataframe(
        pd.DataFrame(source_rows),
        hide_index=True,
        use_container_width=True
    )

    official_available = any(
        item.get("Sources", {}).get(
            "NSE Announcements"
        )
        or item.get("Sources", {}).get(
            "BSE Announcements"
        )
        for item in S.news_market.values()
        if recent(item.get("Checked"), 1800)
    )

    if not official_available:
        st.warning(
            "No recent successful official "
            "announcement feed checks. "
            "Technical + News Mode permits "
            "partially verified research only."
        )

    if S.news_last_scan:
        st.caption(
            "Last news batch: "
            + S.news_last_scan.strftime(
                "%H:%M:%S IST"
            )
        )


# =====================================
# FINAL QUALIFIED CALLS
# ALWAYS VISIBLE
# =====================================

def show_calls(opened):
    st.header("🎯 Final Qualified Calls")

    active = [
        v for v in S.signals.values()
        if v["Status"] == "ACTIVE"
        and v["Time"][:10]
        == now().date().isoformat()
    ]

    active.sort(
        key=lambda v: v["Score"],
        reverse=True
    )

    if not active:
        st.info(
            "NO TRADE / WAIT — "
            "No fully confirmed technical setup"
        )
        return

    for v in active:
        with st.container(border=True):
            st.subheader(
                f"{v['Symbol']} "
                f"{v['Strike']:g} "
                f"{v['Side']} | {v['Expiry']}"
            )

            verified = v.get(
                "Verification",
                "NEWS UNVERIFIED"
            )

            if verified == "FULLY VERIFIED":
                st.success(
                    "FULLY VERIFIED — required "
                    "news feeds available at signal time"
                )

            else:
                st.warning(
                    "PARTIALLY VERIFIED — official "
                    "news confirmation incomplete"
                )

            st.caption(
                f"Origin: {v.get('Origin', '—')} | "
                f"Signal time: {v['Time']} | "
                f"Score: {v['Score']}/100"
            )

            fresh_signal = (
                opened
                and entry_window(token)
                and recent(v["Time"], 180)
                and recent(v["Last Quote Check"], 180)
                and news_feed_ok(
                    v.get("News Snapshot", {})
                )
                and recent(v.get("News Checked"), 300)
                and v["Planned Risk"] <= budget()
                and v["Capital"] <= capital
                and v["SL"] < v["Current Premium"] <= v["Entry"]
            )

            if fresh_signal:
                st.success(
                    "RECENT QUALIFIED RESEARCH SIGNAL"
                )

            else:
                st.warning(
                    "TRACKING ONLY — "
                    "Not a fresh entry recommendation"
                )

            cols = st.columns(3)

            for i, k in enumerate([
                "Entry",
                "Current Premium",
                "SL",
                "Target",
                "Target 2",
                "Lots",
                "Planned Risk",
                "Capital",
                "Observed P&L"
            ]):
                cols[i % 3].metric(
                    k, v.get(k, "—")
                )

            st.progress(
                min(v["Score"] / 100, 1.0)
            )

            if v["Current Premium"] > v["Entry"]:
                st.warning(
                    "Premium above original entry. "
                    "Do not chase."
                )

            st.subheader("Complete Trade Details")

            details = {
                k: val
                for k, val in v.items()
                if k not in (
                    "Checks",
                    "Key",
                    "News Snapshot"
                )
            }

            st.dataframe(
                pd.DataFrame(
                    list(details.items()),
                    columns=["Parameter", "Value"]
                ).astype(str),
                hide_index=True,
                use_container_width=True
            )

            st.subheader(
                "Technical & Option Confirmation"
            )

            for name, passed in v["Checks"].items():
                if passed is None:
                    st.write(
                        f"➖ {name}: Not applicable"
                    )

                elif bool(passed):
                    st.write(f"✅ {name}")

                else:
                    st.write(f"❌ {name}")

            st.subheader(
                "News Intelligence at Signal Time"
            )

            show_news(
                v.get("News Snapshot", {})
            )

            st.warning(
                "Research observation only. "
                "Recheck executable bid/ask. "
                "Signals are alternatives, "
                "not simultaneous positions. "
                "Actual loss may exceed planned risk."
            )


# =====================================
# FORWARD TESTING REPORT
# =====================================

def show_forward_observations():
    st.header("Forward Signal Observations")

    observations = [
        v for v in S.signals.values()
        if v["Status"] in (
            "TARGET OBSERVED",
            "SL OBSERVED",
            "EOD OBSERVED"
        )
    ]

    if observations:
        df = flat_records(observations)

        targets = sum(
            v["Status"] == "TARGET OBSERVED"
            for v in observations
        )

        stops = sum(
            v["Status"] == "SL OBSERVED"
            for v in observations
        )

        eod = sum(
            v["Status"] == "EOD OBSERVED"
            for v in observations
        )

        a, b, c = st.columns(3)

        a.metric(
            "Completed Observations",
            len(observations)
        )

        b.metric("Targets Observed", targets)
        c.metric("SL Observed", stops)

        d, e = st.columns(2)

        d.metric("EOD Observations", eod)

        e.metric(
            "Target Observation Rate",
            f"{100 * targets / len(observations):.1f}%"
        )

        st.dataframe(
            df,
            use_container_width=True
        )

        st.download_button(
            "Download Forward Observations",
            df.to_csv(index=False),
            "forward_observations.csv",
            key="forward_download"
        )

        st.caption(
            "Sampled prices, not executed trades. "
            "Intraperiod SL or target touches "
            "may be missed."
        )

    else:
        st.info(
            "No completed forward observations yet."
        )


# =====================================
# MAIN DASHBOARD
# =====================================

@st.fragment(
    run_every="120s"
    if auto or news_auto
    else None
)
def dashboard():
    reset_day()

    status = (
        exchange(token)
        if token else "UNKNOWN"
    )

    opened = bool(
        token and open_market(token)
    )

    st.caption("Active Mode: " + mode)

    a, b, c = st.columns(3)

    a.metric(
        "Market",
        "OPEN" if opened
        else "CLOSED / UNVERIFIED"
    )

    b.metric("Quote Coverage", S.coverage)

    c.metric(
        "Remaining risk ₹",
        max(0, daily - used)
    )

    d, e, f = st.columns(3)

    d.metric(
        "Deep Scanned Today",
        len(S.deep_scanned)
    )

    e.metric(
        "News Stocks Attempted",
        len(S.news_market)
    )

    f.metric("Scan Cycles", S.scan_count)

    st.caption(
        f"{now():%d-%m-%Y %H:%M:%S} IST"
        f" | Exchange: {status}"
        f" | Fresh stock quotes: {S.fresh_coverage}"
    )

    left, right = st.columns(2)

    clicked = left.button(
        "SCAN COMPLETE F&O MARKET",
        type="primary"
    )

    news_clicked = right.button(
        "SCAN NEXT NEWS BATCH"
    )

    if token and (clicked or auto):
        try:
            track()
        except Exception as e:
            st.warning(
                f"Tracking unavailable: {e}"
            )

    if news_clicked or (
        news_auto
        and (
            clicked
            or S.news_last_scan is None
            or not recent(
                S.news_last_scan.isoformat(), 120
            )
        )
    ):
        try:
            with st.spinner("Scanning news batch..."):
                news_cycle()

        except Exception as e:
            st.warning(
                f"News scan unavailable: {e}"
            )

    if token and (
        clicked or (auto and opened)
    ):
        try:
            with st.spinner("Scanning F&O market..."):
                scan()

        except Exception as e:
            st.error(str(e))

    if not token:
        st.info("Enter Upstox Access Token")

    # Final qualified calls on every page.
    show_calls(opened)

    if S.last_scan:
        st.caption(
            "Last completed scan: "
            + S.last_scan.strftime("%H:%M:%S IST")
        )

        if not recent(
            S.last_scan.isoformat(), 180
        ):
            st.warning(
                "Market scan snapshot is stale."
            )

    if not opened:
        st.warning(
            "No new qualified calls while "
            "exchange is closed or unverified."
        )

    st.divider()

    # =================================
    # HOME AND MARKET SCANNER
    # =================================

    if page in ("Home", "Market Scanner"):
        st.header("Momentum Opportunities")

        r = S.ranking

        if not opened:
            st.info(
                "Watchlist may contain "
                "previous-session data."
            )

        if not r.empty:
            a, b = st.columns(2)

            for col, side in [
                (a, "CE"),
                (b, "PE")
            ]:
                col.subheader("TOP 10 " + side)

                col.dataframe(
                    r[r.Side == side]
                    .head(10)
                    .drop(columns="Key"),
                    hide_index=True,
                    use_container_width=True
                )

            if page == "Market Scanner":
                st.header("Full F&O Market Ranking")

                st.dataframe(
                    r,
                    hide_index=True,
                    use_container_width=True
                )

        show_news_market()

    # =================================
    # DEEP ANALYSIS
    # =================================

    if page == "Deep Analysis":
        st.header("Deep Technical Analysis")

        if S.results:
            st.dataframe(
                flat_records(S.results),
                use_container_width=True
            )

            st.download_button(
                "Download Analysis CSV",
                flat_records(
                    S.results
                ).to_csv(index=False),
                "deep_analysis.csv"
            )

        else:
            st.info(
                "No option setups in latest cycle."
            )

        st.write(S.diagnostics)

    # =================================
    # OPTION CHAIN
    # =================================

    if page == "Option Chain & Greeks":
        st.header("Advanced Option Chain")

        if token:
            try:
                mapping = instrument_mapping()

                symbol = st.selectbox(
                    "Select underlying",
                    sorted(mapping)
                )

                if st.button("FETCH OPTION CHAIN"):
                    key = mapping[symbol]

                    exp = expiry(
                        token, key, now().date()
                    )

                    if not exp:
                        raise RuntimeError(
                            "No eligible expiry"
                        )

                    c = chain(token, key, exp)

                    if c.empty:
                        raise RuntimeError("Empty chain")

                    S.chains[symbol] = c
                    S.chain_times[symbol] = (
                        now().isoformat()
                    )

            except Exception as e:
                st.error(str(e))

        if not S.chains:
            st.info(
                "Option chain snapshots will "
                "appear after scanning."
            )

        for sym, c in S.chains.items():
            with st.expander(sym):
                pcr, pain = summary(c)

                a, b = st.columns(2)

                a.metric("PCR", round(pcr, 2))
                b.metric("Max Pain", pain)

                st.caption(
                    "Snapshot: "
                    + str(S.chain_times.get(sym, "—"))
                )

                st.caption(
                    "PCR and max pain are "
                    "contextual, not entry signals."
                )

                st.dataframe(
                    c,
                    hide_index=True,
                    use_container_width=True
                )

    # =================================
    # NEWS AND EVENTS
    # =================================

    if page == "News & Events":
        show_news_market()

        st.header("Automatic News Intelligence")

        st.caption(
            "Google News | NSE Announcements | "
            "BSE Announcements | Bulk Deals | Block Deals"
        )

        st.info(
            "48-hour headlines and recent deals. "
            "Headline classification is rule-based. "
            "Feed availability does not prove "
            "all material news has been captured."
        )

        try:
            syms = sorted(
                set(instruments().values())
                | set(INDEXES)
            )

            sym = st.selectbox(
                "Select Stock", syms
            )

            direction = st.selectbox(
                "Assess Direction",
                ["CE", "PE"]
            )

            if st.button("FETCH LATEST NEWS"):
                with st.spinner(
                    "Fetching recent news..."
                ):
                    automatic_news(sym, direction)

        except Exception as e:
            st.error(str(e))

        st.markdown(
            "[NSE Corporate Announcements]"
            "(https://www.nseindia.com/"
            "companies-listing/"
            "corporate-filings-announcements)"
        )

        st.markdown(
            "[BSE Corporate Announcements]"
            "(https://www.bseindia.com/corporates/"
            "ann.html)"
        )

        st.markdown(
            "[NSE Bulk and Block Deals]"
            "(https://www.nseindia.com/"
            "report-detail/"
            "display-bulk-and-block-deals)"
        )

        d = news_frame()

        if not d.empty:
            st.subheader("Full Market News Scan")

            display = d.drop(
                columns=["Sources"],
                errors="ignore"
            )

            st.dataframe(
                display,
                hide_index=True,
                use_container_width=True
            )

            st.download_button(
                "Download News Scan",
                display.to_csv(index=False),
                "market_news.csv"
            )

        for ident, result in reversed(
            list(S.news.items())
        ):
            with st.expander(
                f"{ident} | "
                f"{result.get('status', '—')}",
                expanded=False
            ):
                show_news(result)

    # =================================
    # SIGNAL HISTORY
    # =================================

    if page == "Signal History":
        st.header("Signal History & Tracking")

        d = flat_records(S.signals.values())

        if not d.empty:
            st.dataframe(
                d,
                use_container_width=True
            )

            st.download_button(
                "Download Signals CSV",
                d.to_csv(index=False),
                "signals.csv"
            )

        else:
            st.info("No signals recorded yet.")

    # =================================
    # BACKTESTING INTRODUCTION
    # =================================

    if page == "Backtesting":
        st.header("Strategy Testing Centre")

        st.info(
            "V6 Historical Backtesting, "
            "V7 Advanced Research and "
            "Forward Signal Observations "
            "are available below."
        )

        st.caption(
            "Historical research runs separately "
            "from the live market scanner. "
            "V7 does not automatically change "
            "live signal rules."
        )

    with st.expander(
        "Rejected Setups / Diagnostics"
    ):
        st.write(S.diagnostics)

    st.caption(
        "Scores are rule-based, not win probabilities. "
        "Full quote coverage does not mean "
        "full technical or news coverage. "
        "Scan cycles may exceed 120 seconds. "
        "Fully Verified refers to required news "
        "feed availability, not guaranteed accuracy."
    )


dashboard()


# =====================================
# STEP 7: ADVANCED BACKTESTING CENTRE
# V6 + V7 + FORWARD OBSERVATIONS
# OUTSIDE AUTO REFRESH FRAGMENT
# =====================================

if page == "Backtesting":

    research_section = st.radio(
        "Select Backtesting Module",
        [
            "V7 Advanced Research",
            "V6 Historical Backtesting",
            "Forward Signal Observations",
            "Backtesting Coverage"
        ],
        horizontal=True,
        key="backtesting_module_v7"
    )

    st.divider()

    # =================================
    # V7 ADVANCED RESEARCH ENGINE
    # =================================

    if research_section == "V7 Advanced Research":

        st.subheader(
            "Advanced Strategy Research — V7"
        )

        st.caption(
            "Opening Range Breakout | "
            "Breakout + Retest | "
            "Market Structure | "
            "Trend Regime | "
            "Trailing Stop | "
            "Chronological Validation | "
            "Cost Sensitivity"
        )

        if not token:
            st.warning(
                "Enter Upstox Access Token "
                "to run historical research."
            )

        else:
            try:
                universe = instrument_mapping()

                render_backtesting_v7(
                    token,
                    universe
                )

            except Exception as e:
                st.error(
                    f"V7 Advanced Research Error: {e}"
                )

        st.warning(
            "V7 uses historical underlying spot "
            "prices as a strategy research proxy. "
            "Results are not actual option premium "
            "returns or guaranteed future performance."
        )

    # =================================
    # V6 HISTORICAL BACKTESTING
    # =================================

    elif research_section == "V6 Historical Backtesting":

        st.subheader(
            "Historical Strategy Comparison — V6"
        )

        if not token:
            st.warning(
                "Enter Upstox Access Token "
                "to run historical backtesting."
            )

        else:
            try:
                universe = instrument_mapping()

                render_backtesting(
                    token,
                    universe
                )

            except Exception as e:
                st.error(
                    f"Historical Backtesting Error: {e}"
                )

        st.warning(
            "Technical backtesting results must "
            "not be interpreted as actual options "
            "trading returns."
        )

    # =================================
    # FORWARD SIGNAL OBSERVATIONS
    # =================================

    elif research_section == "Forward Signal Observations":

        show_forward_observations()

        st.caption(
            "Forward observations are updated "
            "on full dashboard reruns. "
            "Refresh the page to view the latest "
            "completed observations."
        )

    # =================================
    # BACKTESTING COVERAGE
    # =================================

    elif research_section == "Backtesting Coverage":

        st.subheader("Backtesting Coverage")

        coverage = pd.DataFrame([
            {
                "Module": "Historical spot candles",
                "Status": "Implemented"
            },
            {
                "Module": "EMA / RSI / ADX / ATR",
                "Status": "Implemented"
            },
            {
                "Module": "Higher timeframe confirmation",
                "Status": "Implemented"
            },
            {
                "Module": "Technical breakout filters",
                "Status": "Implemented"
            },
            {
                "Module": "Historical risk simulation",
                "Status": "Implemented"
            },
            {
                "Module": "Multiple strategy comparison",
                "Status": "V6 and V7 engines"
            },
            {
                "Module": "Opening Range Breakout",
                "Status": "V7 engine"
            },
            {
                "Module": "Breakout and retest",
                "Status": "V7 engine"
            },
            {
                "Module": "Market structure confirmation",
                "Status": "V7 engine"
            },
            {
                "Module": "Trend regime filtering",
                "Status": "V7 engine"
            },
            {
                "Module": "Trailing stop simulation",
                "Status": "V7 engine"
            },
            {
                "Module": "Time and regime analysis",
                "Status": "V7 engine"
            },
            {
                "Module": "Chronological validation",
                "Status": "V7 fixed-rule validation"
            },
            {
                "Module": "Slippage sensitivity",
                "Status": "V7 engine"
            },
            {
                "Module": "Actual historical option premium",
                "Status": "Not implemented"
            },
            {
                "Module": "Historical option chain and Greeks",
                "Status": "Not implemented"
            },
            {
                "Module": "Historical news verification",
                "Status": "Not implemented"
            },
            {
                "Module": "Complete live strategy replication",
                "Status": "Not implemented"
            },
            {
                "Module": "Live signal observations",
                "Status": "Session-based tracking"
            }
        ])

        st.dataframe(
            coverage,
            hide_index=True,
            use_container_width=True
        )

        st.warning(
            "Historical research currently tests "
            "underlying price behaviour. "
            "Historical option premium, historical "
            "Greeks, historical OI and historical "
            "news are not fully replicated."
        )

        st.info(
            "V7 strategy results must be validated "
            "on genuinely unseen periods and through "
            "forward observation before changing "
            "live scanner rules."
        )


# =====================================
# TRADING JOURNAL
# =====================================

if page == "Trading Journal":

    st.header("Trading Journal")

    with st.form("journal"):

        sym = st.text_input(
            "Stock / Option Contract"
        )

        side = st.selectbox(
            "Side", ["CE", "PE"]
        )

        en = st.number_input(
            "Entry",
            min_value=0.0
        )

        ex = st.number_input(
            "Exit",
            min_value=0.0
        )

        qty = st.number_input(
            "Quantity",
            min_value=1
        )

        charges = st.number_input(
            "Charges",
            min_value=0.0
        )

        notes = st.text_area("Trade Notes")

        submitted = st.form_submit_button(
            "Save Trade"
        )

        if submitted and sym and en > 0:
            S.journal.append({
                "Time": now().isoformat(),
                "Symbol": sym,
                "Side": side,
                "Entry": en,
                "Exit": ex,
                "Quantity": qty,
                "Charges": charges,

                "Net P&L": round(
                    (ex - en) * qty - charges,
                    2
                ),

                "Notes": notes
            })

    if S.journal:
        d = pd.DataFrame(S.journal)
        p = d["Net P&L"]

        a, b, c = st.columns(3)

        a.metric(
            "Net P&L ₹",
            round(p.sum(), 2)
        )

        b.metric(
            "Win Rate",
            f"{100 * (p > 0).mean():.1f}%"
        )

        loss = -p[p < 0].sum()

        c.metric(
            "Profit Factor",
            round(
                p[p > 0].sum() / loss, 2
            )
            if loss > 0 else "N/A"
        )

        st.dataframe(
            d,
            use_container_width=True
        )

        st.download_button(
            "Download Trading Journal",
            d.to_csv(index=False),
            "journal.csv"
        )


# =====================================
# V7 DASHBOARD FOOTER
# =====================================

if page == "Institutional Intelligence":
    render_institutional(token, instrument_mapping, technical, automatic_news, expiry, chain, now, minvol, spread, iv, option_setup, S.snapshots)

st.divider()

st.caption(
    "NSE F&O PRO — ALL IN ONE V7 | "
    "Dual Verification Mode | "
    "Advanced News Intelligence | "
    "V6 Historical Backtesting | "
    "V7 Advanced Strategy Research | "
    "Forward Signal Tracking | "
    "Session-only storage | "
    "Active Streamlit session required | "
    "No automatic real orders | "
    "Export records before closing."
)
