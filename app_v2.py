
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

from news_engine import assess_news

st.set_page_config(
    page_title="NSE F&O PRO — ALL IN ONE",
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


@st.cache_data(ttl=86400)
def master():
    r = requests.get(
        "https://assets.upstox.com/"
        "market-quote/instruments/exchange/NSE.json.gz",
        timeout=30
    )

    r.raise_for_status()

    return json.loads(
        gzip.decompress(r.content)
    )


@st.cache_data(ttl=3600)
def instruments():

    m = master()

    u = {
        x.get("underlying_key")
        for x in m
        if x.get("segment") == "NSE_FO"
        and x.get("instrument_type") in ("CE", "PE")
    }

    return {
        x["instrument_key"]: x["trading_symbol"]
        for x in m
        if x.get("segment") == "NSE_EQ"
        and x.get("instrument_key") in u
    }


@st.cache_data(ttl=30, show_spinner=False)
def exchange(token):

    try:
        d = api(
            "/v2/market/status/NSE",
            token
        )

        return str(
            d.get("status", "UNKNOWN")
        ).upper()

    except Exception:
        return "UNKNOWN"


def open_market(token):

    t = now()

    return (
        t.weekday() < 5
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

            return pd.to_datetime(
                float(x),
                unit="ms",
                utc=True
            ).tz_convert(IST)

        return pd.to_datetime(
            x, utc=True
        ).tz_convert(IST)

    except Exception:
        return pd.NaT


def fresh(q, seconds=180):

    t = stamp(
        q.get("last_trade_time")
        or q.get("timestamp")
    )

    return (
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

        d = api(
            "/v2/market-quote/quotes",
            token,
            {
                "instrument_key":
                ",".join(keys[i:i + 40])
            }
        )

        for q in (d or {}).values():

            out[q.get("instrument_token")] = q

    return out


# =====================================
# STEP 1: TRADING IDEAS GENERATOR
# =====================================

def ranking(token):

    m = instruments()

    rows = []
    errors = []

    for i in range(0, len(m), 40):

        try:

            data = quotes(
                token,
                list(m)[i:i + 40]
            )

        except Exception as e:

            errors.append(str(e))
            continue

        for k, q in data.items():

            if k not in m:
                continue

            p = n(q.get("last_price"))
            prev = p - n(q.get("net_change"))

            o = q.get("ohlc") or {}

            op, hi, lo = [
                n(o.get(a))
                for a in ("open", "high", "low")
            ]

            if (
                not all(np.isfinite(
                    [p, prev, op, hi, lo]
                ))
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

    return frame, len(m), errors


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

    return pd.DataFrame(
        rows,
        columns=[
            "ts", "open", "high",
            "low", "close", "volume"
        ]
    ).set_index("ts").sort_index()


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

    h = history(
        token,
        key,
        now().date()
    )

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

    d = d[
        ~d.index.duplicated(keep="last")
    ]

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
# STEP 2: AUTOMATED TECHNICAL ANALYST
# =====================================

def indicators(d):

    d = d.copy()

    for p in (9, 21, 50):

        d[f"ema{p}"] = d.close.ewm(
            span=p,
            adjust=False
        ).mean()

    z = d.close.diff()

    g = z.clip(lower=0).ewm(
        alpha=1/9,
        adjust=False
    ).mean()

    l = (-z.clip(upper=0)).ewm(
        alpha=1/9,
        adjust=False
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
        (up > dn) & (up > 0),
        up, 0
    )

    minus = np.where(
        (dn > up) & (dn > 0),
        dn, 0
    )

    for name, arr in [
        ("plus", plus),
        ("minus", minus)
    ]:

        d[name] = (
            100 * pd.Series(
                arr,
                index=d.index
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

    if (
        len(opening) < 3
        or opening.index[0].time() != time(9, 15)
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
        "Opening High": opening.high.max(),
        "Opening Low": opening.low.min()
    }

    return side, checks, metrics


# =====================================
# STEP 4: ADVANCED OPTION CHAIN
# =====================================

def expiry(token, key):

    d = api(
        "/v2/option/contract",
        token,
        {"instrument_key": key}
    ) or []

    dates = sorted({

        x["expiry"]

        for x in d

        if x.get("expiry", "")
        > now().date().isoformat()
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

                **{
                    k.title(): n(g.get(k))
                    for k in (
                        "delta", "theta",
                        "gamma", "vega", "iv"
                    )
                }
            })

    return (
        pd.DataFrame(rows).dropna(subset=["Key"])
        if rows else pd.DataFrame()
    )


def summary(c):

    ce = c[c.Side == "CE"]
    pe = c[c.Side == "PE"]

    co = ce.OI.sum()
    po = pe.OI.sum()

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


@st.cache_data(ttl=86400)
def lot_sizes():

    return {

        x.get("instrument_key"):
        n(x.get("lot_size"))

        for x in master()

        if x.get("segment") == "NSE_FO"
    }


# =====================================
# SESSION STORAGE
# =====================================

for k, v in {

    "ranking": pd.DataFrame(),
    "results": [],
    "diagnostics": [],
    "snapshots": {},
    "chains": {},
    "signals": {},
    "journal": [],
    "last_scan": None,
    "coverage": "—",
    "cursor": 0,
    "news": {},
    "scan_count": 0,
    "deep_scanned": set(),
    "session_date": now().date()

}.items():

    if k not in st.session_state:

        st.session_state[k] = v

S = st.session_state

if S.session_date != now().date():

    S.snapshots = {}
    S.chains = {}
    S.results = []
    S.news = {}
    S.deep_scanned = set()
    S.cursor = 0
    S.session_date = now().date()


# =====================================
# DASHBOARD SETTINGS
# =====================================

st.title("NSE F&O PRO — ALL IN ONE")

st.caption(
    "Automatic News Intelligence | "
    "Advanced Option Chain | "
    "Research Signals"
)

with st.sidebar:

    st.header("Upstox Connection")

    token = st.text_input(

        "Upstox Access Token",

        value=os.getenv(
            "UPSTOX_ACCESS_TOKEN", ""
        ),

        type="password"
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
            "Trading Journal"
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
# STEP 3: AUTOMATIC NEWS INTELLIGENCE
# =====================================

def automatic_news(symbol, side):

    try:

        news = assess_news(
            symbol,
            side
        )

    except Exception:

        news = {

            "status": "NEWS UNVERIFIED",
            "block": False,
            "points": 0,
            "items": [],
            "checked": now().isoformat()
        }

    news = dict(news)

    news["direction"] = side

    S.news[symbol] = news

    return news


# =====================================
# STEP 5: MASTER SCORING SYSTEM
# =====================================

def option_setup(
    o, t, checks, metrics,
    c, exp, symbol, old
):

    q = quotes(
        token, [o.Key]
    ).get(o.Key, {})

    if not fresh(q):

        raise RuntimeError(
            "Option quote stale or timestamp missing"
        )

    b = indicators(
        bars(
            raw(token, o.Key),
            5,
            now()
        )
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

    entry = float(o.Ask)
    atr = n(a.atr)

    if not np.isfinite(atr) or atr <= 0:

        raise RuntimeError("ATR missing")

    prev = old.get(o.Key)

    elapsed = (
        (now() - prev["time"]).total_seconds()
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
            now() - oprev["time"]
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

    lot = n(
        lot_sizes().get(o.Key)
    )

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

    oiok = bool(oi > 0)

    volok = bool(
        vol > 0
        and a.rvol >= 1.1
    )

    groups = {

        "Spot direction": all(
            bool(v)
            for v in checks.values()
            if v is not None
        ),

        "Premium momentum": premium,

        "Live OI": oiok,

        "Fresh volume": volok,

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

    weights = [
        25, 15, 15, 15, 5, 10, 15
    ]

    base_score = sum(

        w

        for w, v in zip(
            weights, groups.values()
        )

        if v
    )

    safe = (

        abs(entry - a.close) <= .5 * atr

        and lots > 0

        and entry_window(token)

        and all(groups.values())
    )

    # AUTOMATIC NEWS INTEGRATION

    news = automatic_news(
        symbol,
        o.Side
    )

    event = news["status"]

    news_points = min(
        10,
        max(0, n(news["points"]))
    )

    score = round(

        base_score * .90

        + news_points,

        1
    )

    safe = (

        safe

        and not news["block"]

        and score >= 90

        and entry_window(token)
    )

    return {

        "Symbol": symbol,
        "Key": o.Key,
        "Side": o.Side,
        "Strike": o.Strike,
        "Expiry": exp,

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

        "Current Premium": o.LTP,

        "Delta": o.Delta,

        "Theta": o.Theta,

        "Gamma": o.Gamma,

        "Vega": o.Vega,

        "IV": o.IV,

        "OI": o.OI,

        "OI Change": oi,

        "Fresh Volume": vol,

        "Total Volume": o.Volume,

        "Premium Change": prem,

        "Opposite OI Change": oppchange,

        "PCR": pcr,

        "Max Pain": pain,

        "Spread %": o["Spread %"],

        "News Review": event,

        "News Checked": news["checked"],

        "Time": now().isoformat(),

        "Last Quote Check": now().isoformat(),

        "Checks": {
            **checks,
            **groups,
            "No adverse news detected":
            not news["block"]
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

    if not active or not open_market(token):

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

        elif p >= v["Target"]:

            v["Status"] = "TARGET OBSERVED"

            v["Exit Observation"] = p

        elif now().time() >= time(15, 20):

            v["Status"] = "EOD OBSERVED"

            v["Exit Observation"] = p


# =====================================
# COMPLETE MARKET SCANNER
# =====================================

def scan():

    S.results = []
    S.diagnostics = []
    S.last_scan = None

    r, total, errors = ranking(token)

    S.ranking = r

    S.coverage = f"{len(r)}/{total}"

    S.diagnostics = errors

    if (

        r.empty

        or not entry_window(token)

        or budget() <= 0
    ):

        S.last_scan = now()

        return

    selected = []

    for side in ("CE", "PE"):

        z = r[
            (r.Side == side)
            & r.Fresh
        ]

        start = (
            S.cursor * count
        ) % max(1, len(z))

        selected.extend(

            pd.concat([
                z.iloc[start:],
                z.iloc[:start]
            ]).head(count).to_dict("records")
        )

    S.cursor += 1

    selected.extend({

        "Symbol": k,

        "Key": v,

        "Side": "AUTO"

    } for k, v in INDEXES.items())

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

            exp = expiry(token, key)

            if not exp:

                raise RuntimeError(
                    "No eligible expiry"
                )

            c = chain(token, key, exp)

            if c.empty:

                raise RuntimeError(
                    "Empty option chain"
                )

            old = S.snapshots.get(key, {})

            S.chains[sym] = c

            snapshot_time = now()

            S.snapshots[key] = {

                x.Key: {

                    "time": snapshot_time,

                    "OI": x.OI,

                    "Volume": x.Volume,

                    "LTP": x.LTP
                }

                for _, x in c.iterrows()
            }

            if not all(

                bool(v)

                for v in checks.values()

                if v is not None
            ):

                failed = [

                    k for k, v in checks.items()

                    if v is not None and not v
                ]

                raise RuntimeError(

                    "Failed: " + ", ".join(failed)
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

            z["Distance"] = (

                z.Delta.abs() - .55

            ).abs()

            if z.empty:

                raise RuntimeError(
                    "No eligible option"
                )

            options = z.sort_values(

                [
                    "Spread %",
                    "Distance",
                    "Volume"
                ],

                ascending=[
                    True, True, False
                ]

            ).head(3)

            for _, o in options.iterrows():

                try:

                    v = option_setup(

                        o,

                        side,

                        checks,

                        metrics,

                        c,

                        exp,

                        sym,

                        old
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
# MAIN DASHBOARD
# QUALIFIED CALLS ALWAYS VISIBLE
# =====================================

@st.fragment(
    run_every="120s" if auto else None
)
def dashboard():

    status = (
        exchange(token)
        if token else "UNKNOWN"
    )

    opened = bool(
        token and open_market(token)
    )

    a, b, c = st.columns(3)

    a.metric(

        "Market",

        "OPEN" if opened

        else "CLOSED / UNVERIFIED"
    )

    b.metric(

        "Quote Coverage",

        S.coverage
    )

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
        "News Reviewed",
        len(S.news)
    )

    f.metric(
        "Scan Cycles",
        S.scan_count
    )

    st.caption(

        f"{now():%d-%m-%Y %H:%M:%S} IST"

        f" | Exchange: {status}"
    )

    clicked = st.button(

        "SCAN COMPLETE F&O MARKET",

        type="primary"
    )

    if token and (

        clicked or (auto and opened)
    ):

        try:

            track()

            with st.spinner(
                "Scanning F&O market..."
            ):

                scan()

        except Exception as e:

            st.error(str(e))

    if not token:

        st.info(
            "Enter Upstox Access Token"
        )

    st.header(
        "🎯 Final Qualified Calls"
    )

    active = [

        v for v in S.signals.values()

        if v["Status"] == "ACTIVE"

        and v["Time"][:10]
        == now().date().isoformat()
    ]

    active = sorted(

        active,

        key=lambda v: v["Score"],

        reverse=True
    )

    if not active:

        st.info(

            "NO TRADE / WAIT — "

            "No fully confirmed setup"
        )

    if active and not opened:

        st.warning(

            "Market closed or unverified. "

            "Displayed signals are historical "

            "observations, not fresh entries."
        )

    for v in active:

        with st.container(border=True):

            st.subheader(

                f"{v['Symbol']} "

                f"{v['Strike']:g} "

                f"{v['Side']} | {v['Expiry']}"
            )

            st.caption(

                f"Research signal | {v['Time']}"

                f" | Score {v['Score']}/100"
            )

            cols = st.columns(3)

            for i, k in enumerate([

                "Entry",

                "Current Premium",

                "SL",

                "Target",

                "Target 2",

                "Lots"
            ]):

                cols[i % 3].metric(

                    k, v[k]
                )

            st.progress(

                min(v["Score"] / 100, 1.0)
            )

            if v["Current Premium"] > v["Entry"]:

                st.caption(
                    "Premium above original entry. "
                    "Do not assume entry is still available."
                )

            if v["Planned Risk"] > budget():

                st.warning(
                    "Original setup exceeds current "
                    "remaining risk budget."
                )

            st.subheader(
                "Complete Trade Details"
            )

            details = {

                k: val

                for k, val in v.items()

                if k not in (

                    "Checks", "Key"
                )
            }

            st.dataframe(

                pd.DataFrame(

                    list(details.items()),

                    columns=[
                        "Parameter",
                        "Value"
                    ]

                ).astype(str),

                hide_index=True,

                use_container_width=True
            )

            st.subheader(
                "Signal Confirmation"
            )

            for name, passed in v["Checks"].items():

                if passed is None:

                    st.write(
                        f"➖ {name}: Not applicable"
                    )

                elif bool(passed):

                    st.write(
                        f"✅ {name}"
                    )

                else:

                    st.write(
                        f"❌ {name}"
                    )

            st.subheader(
                "News Intelligence"
            )

            news = S.news.get(
                v["Symbol"], {}
            )

            if isinstance(news, dict):

                st.write(

                    "News Status:",

                    news.get(
                        "status",
                        "NEWS UNVERIFIED"
                    )
                )

                st.write(

                    "News Points:",

                    news.get("points", 0)
                )

                for item in news.get(
                    "items", []
                ):

                    st.write(
                        item["title"]
                    )

                    st.caption(

                        f"{item['source']} | "

                        f"{item['time']}"
                    )

                    if item.get(
                        "url", ""
                    ).startswith("https://"):

                        st.markdown(

                            f"[Read News]({item['url']})"
                        )

            st.warning(

                "Research signal, not an executed trade. "

                "Recheck live bid/ask before entry. "

                "Calls are alternatives, not simultaneous "

                "positions."
            )

    if S.last_scan:

        st.caption(

            f"Last completed scan: "

            f"{S.last_scan:%H:%M:%S} IST"
        )

        if (

            now() - S.last_scan

        ).total_seconds() > 180:

            st.warning(

                "Scan snapshot is stale. "

                "Do not use as a fresh entry."
            )

    if not opened:

        st.warning(

            "No new calls while exchange is closed "

            "or market status cannot be verified."
        )

    st.divider()

    # =================================
    # MARKET WATCHLIST
    # =================================

    if page in (

        "Home",

        "Market Scanner"
    ):

        r = S.ranking

        if not opened:

            st.info(

                "Watchlist may contain previous-session data."
            )

        if not r.empty:

            a, b = st.columns(2)

            for col, side in [

                (a, "CE"),

                (b, "PE")
            ]:

                col.subheader(

                    "TOP 10 " + side
                )

                col.dataframe(

                    r[r.Side == side]

                    .head(10)

                    .drop(columns="Key"),

                    hide_index=True,

                    use_container_width=True
                )

            if page == "Market Scanner":

                st.header(

                    "Full F&O Market Ranking"
                )

                st.dataframe(

                    r,

                    hide_index=True,

                    use_container_width=True
                )

    # =================================
    # DEEP TECHNICAL ANALYSIS
    # =================================

    if page == "Deep Analysis":

        st.header(

            "Deep Technical Analysis"
        )

        st.dataframe(

            pd.DataFrame([

                {

                    k: x

                    for k, x in v.items()

                    if k != "Checks"
                }

                for v in S.results

            ]),

            use_container_width=True
        )

        st.write(S.diagnostics)

    # =================================
    # OPTION CHAIN AND GREEKS
    # =================================

    if page == "Option Chain & Greeks":

        st.header(

            "Advanced Option Chain"
        )

        if not S.chains:

            st.info(
                "Option chain snapshots will appear "
                "after live scanning."
            )

        for sym, c in S.chains.items():

            with st.expander(sym):

                pcr, pain = summary(c)

                a, b = st.columns(2)

                a.metric(

                    "PCR",

                    round(pcr, 2)
                )

                b.metric(

                    "Max Pain",

                    pain
                )

                st.dataframe(

                    c,

                    hide_index=True,

                    use_container_width=True
                )

    # =================================
    # SIGNAL HISTORY
    # =================================

    if page == "Signal History":

        st.header(

            "Signal History & Tracking"
        )

        d = pd.DataFrame([

            {

                k: x

                for k, x in v.items()

                if k != "Checks"
            }

            for v in S.signals.values()
        ])

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

            st.info(
                "No signals recorded yet."
            )

    with st.expander(

        "Rejected Setups / Diagnostics"
    ):

        st.write(

            S.diagnostics
        )

    st.caption(

        "Scores are rule-based, not win probabilities. "

        "Deep scan rotates candidates. "

        "Full quote coverage is not full technical coverage."
    )


dashboard()


# =====================================
# AUTOMATIC NEWS AND EVENTS PAGE
# =====================================

if page == "News & Events":

    st.header(

        "Automatic News Intelligence"
    )

    st.caption(

        "Google News RSS | "

        "48-hour headlines | "

        "5-minute cache"
    )

    st.success(

        "Automatic news engine connected. "

        "Manual CLEAR is no longer required."
    )

    st.info(

        "Aligned headlines can earn 10 points. "

        "Adverse or mixed headlines can block a setup. "

        "Unverified news earns zero points."
    )

    st.warning(

        "Headline classification is keyword-based. "

        "It does not verify every corporate event "

        "or guarantee the direction of a stock."
    )

    st.markdown(

        "[NSE Corporate Announcements]"

        "(https://www.nseindia.com/"

        "companies-listing/"

        "corporate-filings-announcements)"
    )

    if token:

        try:

            syms = sorted(

                set(instruments().values())

                | set(INDEXES)
            )

            sym = st.selectbox(

                "Select Stock",

                syms
            )

            direction = st.selectbox(

                "Assess Direction",

                ["CE", "PE"]
            )

            if st.button(

                "FETCH LATEST NEWS",

                type="primary"
            ):

                with st.spinner(

                    "Fetching recent news..."
                ):

                    automatic_news(

                        sym,

                        direction
                    )

        except Exception as e:

            st.error(str(e))

    st.metric(

        "Stocks Reviewed This Session",

        len(S.news)
    )

    if not S.news:

        st.info(

            "Automatic news results will appear "

            "when candidates reach option setup analysis. "

            "You can also fetch news manually for testing."
        )

    for symbol, result in list(S.news.items()):

        if not isinstance(result, dict):

            continue

        with st.expander(

            f"{symbol} | {result['status']}",

            expanded=True
        ):

            st.write(

                "Direction:",

                result.get("direction", "—")
            )

            st.write(

                "News Status:",

                result["status"]
            )

            st.write(

                "News Points:",

                result["points"]
            )

            st.write(

                "Checked:",

                result["checked"]
            )

            if result["block"]:

                st.error(

                    "Adverse or mixed headline detected."
                )

            if not result["items"]:

                st.info(

                    "No matching recent headlines "

                    "or news feed unavailable."
                )

            for item in result["items"]:

                st.write(

                    item["title"]
                )

                st.caption(

                    f"{item['source']} | "

                    f"{item['time']}"
                )

                if item.get(

                    "url", ""

                ).startswith("https://"):

                    st.markdown(

                        f"[Read Full News]({item['url']})"
                    )


# =====================================
# STEP 7: BACKTESTING
# =====================================

if page == "Backtesting":

    st.header(

        "Strategy Backtesting"
    )

    st.info(

        "Full historical options backtesting requires "

        "timestamped option chain, OI, Greeks, bid/ask "

        "and news data."
    )

    st.warning(

        "Historical options backtesting is not yet "

        "implemented. Current snapshots cannot "

        "reconstruct historical option-chain conditions."
    )

    st.subheader(

        "Forward Signal Observations"
    )

    observations = [

        v for v in S.signals.values()

        if v["Status"] in (

            "TARGET OBSERVED",

            "SL OBSERVED",

            "EOD OBSERVED"
        )
    ]

    if observations:

        df = pd.DataFrame(observations)

        targets = sum(

            v["Status"] == "TARGET OBSERVED"

            for v in observations
        )

        stops = sum(

            v["Status"] == "SL OBSERVED"

            for v in observations
        )

        a, b, c = st.columns(3)

        a.metric(

            "Completed Observations",

            len(observations)
        )

        b.metric(

            "Targets Observed",

            targets
        )

        c.metric(

            "SL Observed",

            stops
        )

        st.dataframe(

            df.drop(

                columns=["Checks", "Key"],

                errors="ignore"
            ),

            use_container_width=True
        )

        st.caption(

            "These are sampled price observations, "

            "not historical backtest results or executed trades. "

            "Intraperiod target/SL touches may be missed."
        )

    else:

        st.info(

            "No completed forward observations yet."
        )


# =====================================
# TRADING JOURNAL
# =====================================

if page == "Trading Journal":

    st.header(

        "Trading Journal"
    )

    with st.form("journal"):

        sym = st.text_input(

            "Stock / Option Contract"
        )

        side = st.selectbox(

            "Side",

            ["CE", "PE"]
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

        notes = st.text_area(

            "Trade Notes"
        )

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

        d = pd.DataFrame(

            S.journal
        )

        p = d["Net P&L"]

        a, b, c = st.columns(3)

        a.metric(

            "Net P&L",

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

                p[p > 0].sum() / loss,

                2
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


st.divider()

st.caption(

    "NSE F&O PRO — ALL IN ONE | "

    "Automatic News Intelligence | "

    "Session-only storage | "

    "Auto scan requires an active Streamlit session | "

    "No automatic real orders | "

    "Export records before closing."
)
