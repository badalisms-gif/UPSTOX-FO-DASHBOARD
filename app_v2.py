
import os
import gzip
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="NSE F&O PRO V2",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"

MASTER = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)

INDEXES = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service"
}


def num(x, default=np.nan):
    try:
        x = float(x)
        return x if np.isfinite(x) else default
    except (ValueError, TypeError):
        return default


def api(path, token, params=None):
    response = requests.get(
        API + path,
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

    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError("Upstox API error")

    return payload.get("data")


@st.cache_data(ttl=86400)
def master():
    r = requests.get(MASTER, timeout=40)
    r.raise_for_status()
    return json.loads(gzip.decompress(r.content))


@st.cache_data(ttl=86400)
def universe():
    items = master()

    underlying = {
        x.get("underlying_key")
        for x in items
        if x.get("segment") == "NSE_FO"
        and x.get("instrument_type") in ("CE", "PE")
    }

    rows = []

    for x in items:
        if (
            x.get("segment") == "NSE_EQ"
            and x.get("instrument_key") in underlying
        ):
            rows.append({
                "symbol": x.get("trading_symbol"),
                "key": x.get("instrument_key")
            })

    return pd.DataFrame(rows).drop_duplicates("key")


def market_open(now):
    return (
        now.weekday() < 5
        and time(9, 15) <= now.time() < time(15, 30)
    )


def entry_window(now):
    return (
        market_open(now)
        and time(9, 30) <= now.time() <= time(14, 45)
    )


def scan_market(token):
    stocks = universe()

    if stocks.empty:
        raise RuntimeError("F&O universe unavailable")

    mapping = dict(zip(stocks.key, stocks.symbol))
    keys = list(mapping)
    rows = []
    errors = []

    for start in range(0, len(keys), 40):
        batch = keys[start:start + 40]

        try:
            data = api(
                "/v2/market-quote/quotes",
                token,
                {"instrument_key": ",".join(batch)}
            )

            for q in (data or {}).values():
                key = q.get("instrument_token")
                ltp = num(q.get("last_price"))
                net = num(q.get("net_change"))
                ohlc = q.get("ohlc") or {}

                if not isinstance(ohlc, dict):
                    continue

                op = num(ohlc.get("open"))
                hi = num(ohlc.get("high"))
                lo = num(ohlc.get("low"))
                prev = ltp - net

                if key not in mapping:
                    continue

                if not all(
                    np.isfinite(v)
                    for v in [ltp, prev, op, hi, lo]
                ):
                    continue

                if min(prev, op, ltp) <= 0 or hi <= lo:
                    continue

                change = 100 * (ltp / prev - 1)
                opening = 100 * (ltp / op - 1)
                position = np.clip(
                    100 * (ltp - lo) / (hi - lo),
                    0, 100
                )

                side = "NEUTRAL"

                if (
                    change > 0.35
                    and opening > 0
                    and position >= 60
                ):
                    side = "CE"

                elif (
                    change < -0.35
                    and opening < 0
                    and position <= 40
                ):
                    side = "PE"

                strength = min(abs(change) / 2.5, 1) * 60

                location = (
                    position if side == "CE"
                    else 100 - position
                )

                score = (
                    strength + location * 0.4
                    if side != "NEUTRAL" else 0
                )

                rows.append({
                    "Symbol": mapping[key],
                    "Key": key,
                    "Side": side,
                    "LTP": ltp,
                    "Change %": round(change, 2),
                    "Open Change %": round(opening, 2),
                    "Volume": num(q.get("volume"), 0),
                    "Momentum Score": round(score, 1)
                })

        except Exception as e:
            errors.append(str(e))

    frame = pd.DataFrame(rows)

    if frame.empty:
        raise RuntimeError("No usable market quotes")

    return (
        frame.sort_values(
            "Momentum Score", ascending=False
        ),
        len(stocks),
        errors
    )


def parse_candles(data):
    rows = []

    for c in (data or {}).get("candles", []):
        try:
            stamp = pd.to_datetime(
                c[0], utc=True
            ).tz_convert(IST)

            rows.append([
                stamp,
                *[float(v) for v in c[1:6]]
            ])

        except (ValueError, TypeError, IndexError):
            continue

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(
            rows,
            columns=[
                "timestamp", "open", "high",
                "low", "close", "volume"
            ]
        )
        .drop_duplicates("timestamp")
        .set_index("timestamp")
        .sort_index()
    )


def candles(token, key, now):
    encoded = quote(key, safe="")
    end = (now.date() - timedelta(days=1)).isoformat()
    start = (now.date() - timedelta(days=15)).isoformat()

    historical = api(
        f"/v3/historical-candle/{encoded}/"
        f"minutes/1/{end}/{start}",
        token
    )

    current = api(
        f"/v3/historical-candle/intraday/"
        f"{encoded}/minutes/1",
        token
    )

    frames = [
        parse_candles(historical),
        parse_candles(current)
    ]

    frames = [f for f in frames if not f.empty]

    if not frames:
        raise RuntimeError("Candle data unavailable")

    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]

    return df.between_time("09:15", "15:29")


def make_bars(df, minutes, now):
    rule = f"{minutes}min"

    counts = df.close.resample(
        rule, origin="start_day", offset="15min"
    ).count()

    bars = df.resample(
        rule, origin="start_day", offset="15min"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    bars = bars[
        (counts == minutes)
        & (
            bars.index + pd.Timedelta(minutes=minutes)
            <= pd.Timestamp(now)
        )
    ]

    return bars.dropna(subset=["close"]).copy()


def indicators(df):
    df = df.copy()

    for period in [9, 21, 50]:
        df[f"ema{period}"] = df.close.ewm(
            span=period, adjust=False
        ).mean()

    change = df.close.diff()

    gain = change.clip(lower=0).ewm(
        alpha=1/9, adjust=False
    ).mean()

    loss = (-change.clip(upper=0)).ewm(
        alpha=1/9, adjust=False
    ).mean()

    df["rsi"] = 100 - 100 / (
        1 + gain / loss.replace(0, np.nan)
    )

    df.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    df.loc[(loss == 0) & (gain == 0), "rsi"] = 50

    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1/14, adjust=False,
        min_periods=14
    ).mean()

    up = df.high.diff()
    down = -df.low.diff()

    plus = pd.Series(
        np.where((up > down) & (up > 0), up, 0),
        index=df.index
    )

    minus = pd.Series(
        np.where((down > up) & (down > 0), down, 0),
        index=df.index
    )

    df["plus_di"] = (
        100 * plus.ewm(
            alpha=1/14, adjust=False
        ).mean() / df.atr.replace(0, np.nan)
    )

    df["minus_di"] = (
        100 * minus.ewm(
            alpha=1/14, adjust=False
        ).mean() / df.atr.replace(0, np.nan)
    )

    dx = (
        100 * (df.plus_di - df.minus_di).abs()
        / (df.plus_di + df.minus_di).replace(0, np.nan)
    )

    df["adx"] = dx.ewm(
        alpha=1/14, adjust=False,
        min_periods=14
    ).mean()

    df["rvol"] = (
        df.volume /
        df.volume.shift(1).rolling(20).mean()
        .replace(0, np.nan)
    )

    return df


def technical(token, key, now):
    raw = candles(token, key, now)

    b5 = indicators(make_bars(raw, 5, now))
    b15 = indicators(make_bars(raw, 15, now))
    b60 = indicators(make_bars(raw, 60, now))

    if len(b5) < 50 or len(b15) < 25 or len(b60) < 21:
        raise RuntimeError("Insufficient candle history")

    a = b5.iloc[-1]
    b = b15.iloc[-1]
    h = b60.iloc[-1]

    if now - a.name.to_pydatetime() > timedelta(minutes=10):
        raise RuntimeError("Stale 5-minute candle")

    today = b5[b5.index.date == now.date()]

    if len(today) < 3:
        raise RuntimeError("Opening range incomplete")

    opening = today.iloc[:3]

    if opening.index[0].time() != time(9, 15):
        raise RuntimeError("Opening candles missing")

    or_high = opening.high.max()
    or_low = opening.low.min()

    typical = (today.high + today.low + today.close) / 3

    vwap = (
        (typical * today.volume).sum() / today.volume.sum()
        if today.volume.sum() > 0 else np.nan
    )

    bullish = a.close > a.ema9 > a.ema21
    bearish = a.close < a.ema9 < a.ema21

    side = (
        "CE" if bullish
        else "PE" if bearish
        else "NEUTRAL"
    )

    if side == "CE":
        higher = b.close > b.ema21 and h.close > h.ema21
        momentum = a.rsi >= 55 and a.plus_di > a.minus_di
        breakout = (
            a.close > or_high
            and a.close > b5.high.iloc[-6:-1].max()
        )
        vwap_ok = np.isfinite(vwap) and a.close > vwap

    elif side == "PE":
        higher = b.close < b.ema21 and h.close < h.ema21
        momentum = a.rsi <= 45 and a.minus_di > a.plus_di
        breakout = (
            a.close < or_low
            and a.close < b5.low.iloc[-6:-1].min()
        )
        vwap_ok = np.isfinite(vwap) and a.close < vwap

    else:
        higher = momentum = breakout = vwap_ok = False

    extension = (
        abs(a.close - a.ema9) / a.atr
        if np.isfinite(a.atr) and a.atr > 0
        else np.inf
    )

    volume_ok = np.isfinite(a.rvol) and a.rvol >= 1.3

    checks = {
        "Trend": side != "NEUTRAL",
        "Higher TF": higher,
        "Momentum": momentum,
        "ADX": a.adx >= 25,
        "VWAP": vwap_ok,
        "Breakout": breakout,
        "Volume": volume_ok,
        "Not Extended": extension <= 2
    }

    weights = [10, 15, 10, 10, 10, 15, 5, 5]

    score = sum(
        w for w, passed in zip(weights, checks.values())
        if passed
    )

    return {
        "Side": side,
        "Technical Score": score,
        "RSI": round(a.rsi, 1),
        "ADX": round(a.adx, 1),
        "ATR": a.atr,
        "RVOL": round(a.rvol, 2),
        "VWAP": vwap,
        "Opening High": or_high,
        "Opening Low": or_low,
        "Bar": str(a.name),
        "Checks": checks
    }


def get_expiry(token, key, now):
    data = api(
        "/v2/option/contract",
        token,
        {"instrument_key": key}
    )

    dates = sorted({
        x["expiry"]
        for x in (data or [])
        if x.get("expiry")
        and x["expiry"] > now.date().isoformat()
    })

    return dates[0] if dates else None


def chain_data(token, key, expiry):
    data = api(
        "/v2/option/chain",
        token,
        {
            "instrument_key": key,
            "expiry_date": expiry
        }
    )

    rows = []

    for item in data or []:
        for side, field in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            option = item.get(field) or {}
            m = option.get("market_data") or {}
            g = option.get("option_greeks") or {}

            bid = num(m.get("bid_price"))
            ask = num(m.get("ask_price"))

            spread = (
                200 * (ask - bid) / (ask + bid)
                if ask >= bid > 0 else np.nan
            )

            rows.append({
                "Strike": num(item.get("strike_price")),
                "Spot": num(item.get("underlying_spot_price")),
                "Side": side,
                "LTP": num(m.get("ltp")),
                "Bid": bid,
                "Ask": ask,
                "Spread %": spread,
                "OI": num(m.get("oi")),
                "Previous OI": num(m.get("prev_oi")),
                "Volume": num(m.get("volume")),
                "Delta": num(g.get("delta")),
                "Theta": num(g.get("theta")),
                "Gamma": num(g.get("gamma")),
                "Vega": num(g.get("vega")),
                "IV": num(g.get("iv")),
                "Key": option.get("instrument_key")
            })

    df = pd.DataFrame(rows)

    if not df.empty:
        df["OI Change"] = df["OI"] - df["Previous OI"]

    return df


def chain_summary(df):
    ce = df[df.Side == "CE"].dropna(subset=["OI"])
    pe = df[df.Side == "PE"].dropna(subset=["OI"])

    ce_oi = ce.OI.sum()
    pe_oi = pe.OI.sum()

    pcr = pe_oi / ce_oi if ce_oi > 0 else np.nan

    pain = {}

    for strike in df.Strike.dropna().unique():
        call_loss = (
            (strike - ce.Strike).clip(lower=0) * ce.OI
        ).sum()

        put_loss = (
            (pe.Strike - strike).clip(lower=0) * pe.OI
        ).sum()

        pain[strike] = call_loss + put_loss

    max_pain = (
        min(pain, key=pain.get)
        if pain and ce_oi > 0 and pe_oi > 0
        else np.nan
    )

    return pcr, max_pain


@st.cache_data(ttl=86400)
def lot_sizes():
    return {
        x.get("instrument_key"): num(x.get("lot_size"))
        for x in master()
        if x.get("segment") == "NSE_FO"
    }


def select_option(df, side, min_volume, max_spread, max_iv):
    if df.empty:
        return pd.DataFrame()

    delta_ok = (
        df.Delta.between(0.45, 0.70)
        if side == "CE"
        else df.Delta.between(-0.70, -0.45)
    )

    candidates = df[
        (df.Side == side)
        & delta_ok
        & (df.Volume >= min_volume)
        & (df["Spread %"] <= max_spread)
        & (df.IV > 0)
        & (df.IV <= max_iv)
        & (df.OI > 0)
        & (df.Ask > 0)
        & (df.LTP > 0)
        & df.Key.notna()
    ].copy()

    if candidates.empty:
        return candidates

    candidates["Delta Distance"] = (
        candidates.Delta.abs() - 0.55
    ).abs()

    return candidates.sort_values(
        ["Spread %", "Delta Distance", "Volume"],
        ascending=[True, True, False]
    ).head(3)


def option_setup(
    token, option, tech, now,
    capital, risk_budget, rr
):
    raw = candles(token, option.Key, now)
    bars = indicators(make_bars(raw, 5, now))

    if len(bars) < 30:
        raise RuntimeError("Option history insufficient")

    a = bars.iloc[-1]

    if now - a.name.to_pydatetime() > timedelta(minutes=10):
        raise RuntimeError("Option candle stale")

    entry = float(option.Ask)
    atr = num(a.atr)

    if not np.isfinite(atr) or atr <= 0:
        raise RuntimeError("Option ATR unavailable")

    premium_up = (
        a.close > a.ema9 > a.ema21
        and a.rsi >= 52
        and a.close > bars.close.iloc[-2]
    )

    not_chasing = (
        entry <= a.close + 0.5 * atr
        and entry >= a.close - 0.5 * atr
    )

    risk = max(
        1.5 * atr,
        entry - bars.low.iloc[-3:].min()
    )

    sl = entry - risk

    if sl <= 0 or risk / entry > 0.25:
        raise RuntimeError("Option stop risk excessive")

    target = entry + rr * risk

    lot = num(lot_sizes().get(option.Key))

    if not np.isfinite(lot) or lot <= 0:
        raise RuntimeError("Lot size unavailable")

    lots = max(
        0,
        int(min(
            risk_budget / (risk * lot),
            capital / (entry * lot)
        ))
    )

    score = (
        tech["Technical Score"]
        + 10
        + (10 if premium_up else 0)
    )

    mandatory = all(tech["Checks"].values())

    qualified = (
        mandatory
        and premium_up
        and not_chasing
        and lots > 0
        and entry_window(now)
        and score >= 90
    )

    return {
        "Strike": option.Strike,
        "Side": option.Side,
        "Score": score,
        "Status": "QUALIFIED" if qualified else "WAIT",
        "Entry": round(entry, 2),
        "SL": round(sl, 2),
        "Target": round(target, 2),
        "R:R": rr,
        "Lots": lots,
        "Lot Size": int(lot),
        "Capital": round(lots * lot * entry, 2),
        "Planned Risk": round(lots * lot * risk, 2),
        "Delta": option.Delta,
        "Theta": option.Theta,
        "Gamma": option.Gamma,
        "Vega": option.Vega,
        "IV": option.IV,
        "OI Change": option["OI Change"],
        "Spread %": round(option["Spread %"], 2),
        "Premium Momentum": premium_up
    }


st.title("NSE F&O PRO V2")

st.caption(
    "Market Scanner | Technical Engine | "
    "Option Intelligence | Research Only"
)

with st.sidebar:
    st.header("Upstox Connection")

    token = st.text_input(
        "Access Token",
        value=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        type="password"
    )

    st.header("Risk Management")

    capital = st.number_input(
        "Available Capital ₹",
        min_value=1000,
        value=100000,
        step=10000
    )

    daily_risk = st.number_input(
        "Daily Risk Budget ₹",
        min_value=100,
        value=5000,
        step=500
    )

    used_risk = st.number_input(
        "Risk Already Used Today ₹",
        min_value=0,
        value=0,
        step=500
    )

    risk_trade = st.number_input(
        "Maximum Risk Per Trade ₹",
        min_value=100,
        value=1500,
        step=100
    )

    rr = st.slider(
        "Reward / Risk",
        2.0, 4.0, 2.0, 0.25
    )

    min_volume = st.number_input(
        "Minimum Option Volume",
        min_value=100,
        value=10000,
        step=1000
    )

    max_spread = st.slider(
        "Maximum Spread %",
        0.5, 5.0, 2.0, 0.5
    )

    max_iv = st.slider(
        "Maximum IV %",
        10, 100, 45
    )

    deep_count = st.slider(
        "Deep Scan Per Side",
        1, 10, 3
    )

    auto = st.toggle(
        "Auto Scan Every 120 Seconds",
        value=False
    )

    st.caption(
        "Expiry-day contracts excluded. "
        "No automatic orders."
    )

if "journal" not in st.session_state:
    st.session_state.journal = []

if "results" not in st.session_state:
    st.session_state.results = pd.DataFrame()

if "ranking" not in st.session_state:
    st.session_state.ranking = pd.DataFrame()

if "diagnostics" not in st.session_state:
    st.session_state.diagnostics = []

if "last_scan" not in st.session_state:
    st.session_state.last_scan = None


@st.fragment(run_every="120s" if auto else None)
def dashboard():
    now = datetime.now(IST)

    remaining = max(0, daily_risk - used_risk)

    budget = min(risk_trade, remaining)

    a, b, c = st.columns(3)

    a.metric("Market Window", (
        "OPEN" if market_open(now) else "CLOSED"
    ))

    b.metric("Remaining Risk ₹", remaining)

    c.metric("Entry Window", (
        "ACTIVE" if entry_window(now) else "WAIT"
    ))

    st.caption(
        f"IST: {now:%d-%m-%Y %H:%M:%S}"
    )

    if not token:
        st.info("Enter Upstox Access Token")
        return

    clicked = st.button(
        "SCAN COMPLETE F&O MARKET",
        type="primary"
    )

    if clicked or (auto and market_open(now)):
        st.session_state.results = pd.DataFrame()
        st.session_state.ranking = pd.DataFrame()
        st.session_state.diagnostics = []
        st.session_state.last_scan = None

        try:
            with st.spinner("Scanning F&O market..."):
                ranking, total, errors = scan_market(token)

            st.session_state.ranking = ranking
            st.session_state.last_scan = now

            st.session_state.coverage = (
                f"{len(ranking)} / {total} stocks"
            )

            diagnostics = [
                f"Quote batch error: {e}"
                for e in errors
            ]

            results = []

            if entry_window(now) and budget > 0:
                selected = pd.concat([
                    ranking[ranking.Side == "CE"]
                    .head(deep_count),
                    ranking[ranking.Side == "PE"]
                    .head(deep_count)
                ])

                index_rows = pd.DataFrame([
                    {
                        "Symbol": name,
                        "Key": key,
                        "Side": "AUTO"
                    }
                    for name, key in INDEXES.items()
                ])

                selected = pd.concat([
                    selected, index_rows
                ], ignore_index=True)

                progress = st.progress(0)

                for i, row in selected.iterrows():
                    symbol = row.Symbol

                    try:
                        current_time = datetime.now(IST)

                        tech = technical(
                            token, row.Key, current_time
                        )

                        side = tech["Side"]

                        if side == "NEUTRAL":
                            raise RuntimeError("Neutral trend")

                        if row.Side not in ("AUTO", side):
                            raise RuntimeError("Trend conflict")

                        if not all(tech["Checks"].values()):
                            failed = [
                                k for k, v
                                in tech["Checks"].items()
                                if not v
                            ]

                            raise RuntimeError(
                                "Failed: " + ", ".join(failed)
                            )

                        expiry = get_expiry(
                            token, row.Key, current_time
                        )

                        if not expiry:
                            raise RuntimeError("No eligible expiry")

                        chain = chain_data(
                            token, row.Key, expiry
                        )

                        if chain.empty:
                            raise RuntimeError("Empty option chain")

                        pcr, pain = chain_summary(chain)

                        options = select_option(
                            chain, side, min_volume,
                            max_spread, max_iv
                        )

                        if options.empty:
                            raise RuntimeError(
                                "No liquid eligible option"
                            )

                        for _, option in options.iterrows():
                            try:
                                setup = option_setup(
                                    token,
                                    option,
                                    tech,
                                    datetime.now(IST),
                                    capital,
                                    budget,
                                    rr
                                )

                                setup.update({
                                    "Symbol": symbol,
                                    "Expiry": expiry,
                                    "RSI": tech["RSI"],
                                    "ADX": tech["ADX"],
                                    "RVOL": tech["RVOL"],
                                    "PCR": pcr,
                                    "Max Pain": pain,
                                    "Time": datetime.now(IST)
                                    .isoformat()
                                })

                                results.append(setup)

                                if setup["Status"] == "QUALIFIED":
                                    break

                            except Exception as e:
                                diagnostics.append(
                                    f"{symbol} option: {e}"
                                )

                    except Exception as e:
                        diagnostics.append(
                            f"{symbol}: {e}"
                        )

                    progress.progress(
                        min((i + 1) / len(selected), 1.0)
                    )

                progress.empty()

            st.session_state.results = pd.DataFrame(results)
            st.session_state.diagnostics = diagnostics

        except Exception as e:
            st.error(f"Scanner error: {e}")

    ranking = st.session_state.ranking
    results = st.session_state.results

    if st.session_state.last_scan:
        age = (
            datetime.now(IST)
            - st.session_state.last_scan
        ).total_seconds()

        st.caption(
            f"Last scan: "
            f"{st.session_state.last_scan:%H:%M:%S} IST"
            f" | Coverage: "
            f"{st.session_state.get('coverage', 'N/A')}"
        )

        if age > 180:
            st.warning("Snapshot stale. Refresh before use.")

    else:
        age = np.inf

    st.header("1. Final Qualified Opportunities")

    if (
        results.empty
        or age > 180
        or not entry_window(datetime.now(IST))
        or budget <= 0
    ):
        st.info("NO TRADE / WAIT")

    else:
        qualified = results[
            (results.Status == "QUALIFIED")
            & (results["Planned Risk"] <= budget)
            & (results.Capital <= capital)
        ].sort_values(
            ["Score", "Spread %"],
            ascending=[False, True]
        ).drop_duplicates("Symbol").head(5)

        if qualified.empty:
            st.info("NO TRADE — filters not satisfied")

        else:
            st.dataframe(
                qualified[[
                    "Symbol", "Expiry", "Strike", "Side",
                    "Score", "Entry", "SL", "Target",
                    "Lots", "Planned Risk"
                ]],
                hide_index=True,
                use_container_width=True
            )

            st.warning(
                "Alternative research setups, not simultaneous "
                "positions. Combined risk must stay within "
                "the daily budget."
            )

    st.header("2. Top CE / PE Watchlist")

    left, right = st.columns(2)

    if not ranking.empty:
        columns = [
            "Symbol", "LTP", "Change %",
            "Momentum Score"
        ]

        with left:
            st.subheader("TOP 10 CE")
            st.dataframe(
                ranking[ranking.Side == "CE"]
                .head(10)[columns],
                hide_index=True,
                use_container_width=True
            )

        with right:
            st.subheader("TOP 10 PE")
            st.dataframe(
                ranking[ranking.Side == "PE"]
                .head(10)[columns],
                hide_index=True,
                use_container_width=True
            )

    st.header("3. Deep Analysis Results")

    if not results.empty:
        st.dataframe(
            results,
            hide_index=True,
            use_container_width=True
        )

        st.download_button(
            "Download Analysis CSV",
            results.to_csv(index=False),
            file_name="fo_pro_analysis.csv",
            mime="text/csv"
        )

    with st.expander("Rejected Setups / Diagnostics"):
        for message in st.session_state.diagnostics:
            st.write(message)

    with st.expander("Full Market Ranking"):
        st.dataframe(
            ranking,
            hide_index=True,
            use_container_width=True
        )

    st.warning(
        "Research only. Scores are rule-based, "
        "not validated win probabilities. "
        "Exchange holidays are not verified. "
        "Brokerage, taxes and slippage excluded."
    )


dashboard()

st.divider()

st.header("4. Trading Journal")

with st.form("trade_journal"):
    symbol = st.text_input("Stock / Option Contract")

    side = st.selectbox("Trade Side", ["CE", "PE"])

    entry = st.number_input(
        "Actual Entry",
        min_value=0.0,
        step=0.05
    )

    exit_price = st.number_input(
        "Actual Exit",
        min_value=0.0,
        step=0.05
    )

    quantity = st.number_input(
        "Actual Quantity",
        min_value=1,
        value=1
    )

    charges = st.number_input(
        "Total Charges ₹",
        min_value=0.0
    )

    notes = st.text_area("Trade Notes")

    submitted = st.form_submit_button("Save Trade")

    if submitted and symbol and entry > 0:
        pnl = (exit_price - entry) * quantity - charges

        st.session_state.journal.append({
            "Time": datetime.now(IST).isoformat(),
            "Symbol": symbol,
            "Side": side,
            "Entry": entry,
            "Exit": exit_price,
            "Quantity": quantity,
            "Charges": charges,
            "Net P&L": round(pnl, 2),
            "Notes": notes
        })

if st.session_state.journal:
    journal = pd.DataFrame(st.session_state.journal)

    pnl = journal["Net P&L"]

    wins = pnl[pnl > 0].sum()
    losses = abs(pnl[pnl < 0].sum())

    a, b, c = st.columns(3)

    a.metric("Net P&L ₹", round(pnl.sum(), 2))

    b.metric(
        "Win Rate",
        f"{100 * (pnl > 0).mean():.1f}%"
    )

    c.metric(
        "Profit Factor",
        round(wins / losses, 2)
        if losses > 0 else "N/A"
    )

    st.dataframe(
        journal,
        hide_index=True,
        use_container_width=True
    )

    st.download_button(
        "Download Trading Journal",
        journal.to_csv(index=False),
        file_name="trading_journal.csv",
        mime="text/csv"
    )

st.caption(
    "Journal is session-based. Download CSV before closing. "
    "Historical options backtesting is not implemented."
)
