
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="NIFTY F&O | Single Decision V2",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com/v2"
INDEX_KEY = "NSE_INDEX|Nifty 50"


# ==========================================================
# GENERAL HELPERS
# ==========================================================

def num(value, default=np.nan):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def api_get(path, token, params=None):
    response = requests.get(
        BASE + path,
        params=params or {},
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json"
        },
        timeout=25
    )

    if response.status_code != 200:
        try:
            body = response.json()
            errors = body.get("errors", [])
            message = "; ".join(
                str(e.get("message", e))
                for e in errors
            )
        except (ValueError, TypeError):
            message = response.text[:300]

        raise RuntimeError(
            f"Upstox HTTP {response.status_code}: {message}"
        )

    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError(
            str(payload.get("errors") or "Upstox API error")
        )

    return payload.get("data") or []


# ==========================================================
# OPTION CHAIN
# ==========================================================

def get_option_chain(token, expiry):
    raw = api_get(
        "/option/chain",
        token,
        {
            "instrument_key": INDEX_KEY,
            "expiry_date": expiry
        }
    )

    records = []

    for item in raw:
        strike = num(item.get("strike_price"))
        spot = num(item.get("underlying_spot_price"))

        for side, field in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            option = item.get(field) or {}
            market = option.get("market_data") or {}
            greeks = option.get("option_greeks") or {}

            bid = num(market.get("bid_price"))
            ask = num(market.get("ask_price"))

            spread = np.nan

            if (
                np.isfinite(bid)
                and np.isfinite(ask)
                and bid > 0
                and ask >= bid
            ):
                spread = (
                    (ask - bid) /
                    ((ask + bid) / 2)
                ) * 100

            oi = num(market.get("oi"), 0)
            prev_oi = num(market.get("prev_oi"), 0)

            records.append({
                "strike": strike,
                "side": side,
                "spot": spot,
                "ltp": num(market.get("ltp")),
                "oi": oi,
                "prev_oi": prev_oi,
                "oi_change_close": oi - prev_oi,
                "volume": num(market.get("volume"), 0),
                "bid": bid,
                "ask": ask,
                "spread_pct": spread,
                "iv": num(greeks.get("iv")),
                "delta": num(greeks.get("delta")),
                "gamma": num(greeks.get("gamma")),
                "theta": num(greeks.get("theta")),
                "vega": num(greeks.get("vega")),
                "instrument": option.get("instrument_key", "")
            })

    return pd.DataFrame(records)


# ==========================================================
# 1-MINUTE DATA -> 5-MINUTE CANDLES
# FIX FOR UPSTOX UDAPI1076
# ==========================================================

def get_5minute_candles(token):
    encoded_key = requests.utils.quote(
        INDEX_KEY,
        safe=""
    )

    # This endpoint accepts 1minute or 30minute,
    # not 5minute.
    raw = api_get(
        "/historical-candle/intraday/"
        + encoded_key
        + "/1minute",
        token
    )

    rows = raw.get("candles", []) if isinstance(raw, dict) else []

    if not rows:
        raise RuntimeError(
            "Upstox returned no intraday 1-minute candles."
        )

    frame = pd.DataFrame(rows)

    if frame.shape[1] < 6:
        raise RuntimeError(
            "Unexpected Upstox candle format."
        )

    frame = frame.iloc[:, :6].copy()

    frame.columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]

    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"],
        utc=True
    ).dt.tz_convert(IST)

    for col in [
        "open", "high", "low", "close", "volume"
    ]:
        frame[col] = pd.to_numeric(
            frame[col],
            errors="coerce"
        )

    frame = frame.dropna(
        subset=[
            "timestamp",
            "open",
            "high",
            "low",
            "close"
        ]
    )

    frame = frame.sort_values("timestamp")
    frame = frame.drop_duplicates("timestamp")
    frame = frame.set_index("timestamp")

    # Keep only regular NSE cash-session minutes.
    frame = frame.between_time(
        "09:15",
        "15:29"
    )

    # 09:15, 09:20, 09:25 ... aligned bars.
    candles = frame.resample(
        "5min",
        label="left",
        closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    # Count actual one-minute records in each bar.
    counts = frame["close"].resample(
        "5min",
        label="left",
        closed="left"
    ).count()

    candles["minute_count"] = counts

    # Avoid incomplete 5-minute candles.
    candles = candles[
        candles["minute_count"] == 5
    ]

    candles = candles.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close"
        ]
    )

    candles = candles.reset_index()

    if candles.empty:
        raise RuntimeError(
            "No complete 5-minute candles could be built."
        )

    return candles


# ==========================================================
# INDICATORS
# ==========================================================

def rma(series, length):
    return series.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()


def calculate_indicators(df):
    x = df.copy()

    x["ema9"] = x["close"].ewm(
        span=9,
        adjust=False,
        min_periods=9
    ).mean()

    x["ema20"] = x["close"].ewm(
        span=20,
        adjust=False,
        min_periods=20
    ).mean()

    previous_close = x["close"].shift(1)

    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - previous_close).abs(),
        (x["low"] - previous_close).abs()
    ], axis=1).max(axis=1)

    x["atr"] = rma(tr, 14)

    up = x["high"].diff()
    down = -x["low"].diff()

    plus_dm = pd.Series(
        np.where(
            (up > down) & (up > 0),
            up,
            0.0
        ),
        index=x.index
    )

    minus_dm = pd.Series(
        np.where(
            (down > up) & (down > 0),
            down,
            0.0
        ),
        index=x.index
    )

    smoothed_tr = rma(tr, 14).replace(
        0,
        np.nan
    )

    x["plus_di"] = (
        100 * rma(plus_dm, 14) / smoothed_tr
    )

    x["minus_di"] = (
        100 * rma(minus_dm, 14) / smoothed_tr
    )

    dx = (
        100 *
        (x["plus_di"] - x["minus_di"]).abs() /
        (x["plus_di"] + x["minus_di"]).replace(
            0,
            np.nan
        )
    )

    x["adx"] = rma(dx, 14)

    # NIFTY index may report zero volume.
    # True VWAP cannot be calculated from zero volume.
    x["vwap"] = np.nan

    for _, group in x.groupby(
        x["timestamp"].dt.date
    ):
        volume = group["volume"].clip(lower=0)

        if volume.sum() > 0:
            typical = (
                group["high"] +
                group["low"] +
                group["close"]
            ) / 3

            cumulative_volume = (
                volume.cumsum().replace(0, np.nan)
            )

            vwap = (
                (typical * volume).cumsum() /
                cumulative_volume
            )

            x.loc[group.index, "vwap"] = vwap

    return x


# ==========================================================
# MARKET DIRECTION
# ==========================================================

def find_direction(df, adx_min):
    if len(df) < 35:
        return (
            "NONE",
            "Not enough complete 5-minute candles.",
            None
        )

    current = df.iloc[-1]
    previous = df.iloc[-2]

    required = [
        "ema9",
        "ema20",
        "adx",
        "atr",
        "plus_di",
        "minus_di"
    ]

    if any(
        not np.isfinite(current[key])
        for key in required
    ):
        return (
            "NONE",
            "Indicators are not ready.",
            current
        )

    if current["adx"] < adx_min:
        return (
            "NONE",
            "ADX is below minimum threshold.",
            current
        )

    valid_vwap = np.isfinite(
        current["vwap"]
    )

    bullish_vwap = (
        not valid_vwap or
        current["close"] > current["vwap"]
    )

    bearish_vwap = (
        not valid_vwap or
        current["close"] < current["vwap"]
    )

    bullish = (
        current["close"] > current["ema9"]
        and current["ema9"] > current["ema20"]
        and current["plus_di"] > current["minus_di"]
        and current["ema9"] > previous["ema9"]
        and bullish_vwap
    )

    bearish = (
        current["close"] < current["ema9"]
        and current["ema9"] < current["ema20"]
        and current["minus_di"] > current["plus_di"]
        and current["ema9"] < previous["ema9"]
        and bearish_vwap
    )

    if bullish:
        return (
            "CE",
            "Bullish EMA + ADX/DI alignment.",
            current
        )

    if bearish:
        return (
            "PE",
            "Bearish EMA + ADX/DI alignment.",
            current
        )

    return (
        "NONE",
        "Trend conditions disagree.",
        current
    )


# ==========================================================
# OPTION RANKING
# ==========================================================

def rank_options(
    df,
    side,
    spot,
    min_volume,
    max_spread,
    max_iv,
    previous_prices
):
    strikes = sorted(
        df["strike"].dropna().unique()
    )

    steps = [
        b - a
        for a, b in zip(
            strikes,
            strikes[1:]
        )
        if b > a
    ]

    step = min(steps) if steps else 50

    candidates = df[
        (df["side"] == side)
        & (
            (df["strike"] - spot).abs()
            <= 3 * step
        )
    ].copy()

    results = []

    for _, r in candidates.iterrows():

        if not (
            np.isfinite(r["ltp"])
            and r["ltp"] > 0
        ):
            continue

        previous = previous_prices.get(
            r["instrument"]
        )

        premium_rising = (
            previous is not None
            and r["ltp"] > previous
        )

        checks = {
            "Delta": (
                np.isfinite(r["delta"])
                and 0.35 <= abs(r["delta"]) <= 0.75
            ),
            "Volume": (
                r["volume"] >= min_volume
            ),
            "Spread": (
                np.isfinite(r["spread_pct"])
                and 0 <= r["spread_pct"] <= max_spread
            ),
            "IV": (
                np.isfinite(r["iv"])
                and 0 < r["iv"] <= max_iv
            ),
            "OI": (
                r["oi"] > 0
            ),
            "Premium momentum": premium_rising
        }

        score = sum(
            bool(value)
            for value in checks.values()
        )

        results.append({
            "row": r,
            "score": score,
            "distance": abs(r["strike"] - spot),
            "checks": checks
        })

    results.sort(
        key=lambda item: (
            -item["score"],
            item["distance"]
        )
    )

    return results


# ==========================================================
# SIDEBAR
# ==========================================================

st.title(
    "NIFTY F&O | Single Final Decision V2"
)

st.caption(
    "5-minute trend + Upstox option chain | "
    "Research only | No automatic orders"
)

with st.sidebar:

    st.header("Connection")

    token = st.text_input(
        "Upstox Algo Trading Access Token",
        value=os.getenv(
            "UPSTOX_ACCESS_TOKEN",
            ""
        ),
        type="password"
    )

    expiry = st.text_input(
        "Expiry YYYY-MM-DD",
        value="2026-11-23"
    )

    st.header("Signal Filters")

    adx_min = st.slider(
        "Minimum ADX",
        15,
        40,
        25
    )

    max_spread = st.slider(
        "Maximum bid/ask spread %",
        0.5,
        10.0,
        3.0,
        0.5
    )

    max_iv = st.slider(
        "Maximum IV %",
        10,
        100,
        40
    )

    min_volume = st.number_input(
        "Minimum traded volume",
        min_value=0,
        value=10000,
        step=1000
    )

    min_score = st.slider(
        "Minimum option score / 6",
        3,
        6,
        5
    )

    reward_risk = st.slider(
        "Illustrative reward/risk",
        1.0,
        3.0,
        1.75,
        0.25
    )

    auto = st.toggle(
        "Auto-refresh every 30 seconds",
        value=False
    )

    st.caption(
        "Keep your API token private."
    )


# ==========================================================
# SESSION STATE
# ==========================================================

if "previous_prices" not in st.session_state:
    st.session_state.previous_prices = {}

if "previous_time" not in st.session_state:
    st.session_state.previous_time = None

if "previous_identity" not in st.session_state:
    st.session_state.previous_identity = ""


# ==========================================================
# MAIN DASHBOARD
# ==========================================================

@st.fragment(
    run_every="30s" if auto else None
)
def dashboard():

    clicked = st.button(
        "Fetch / Refresh Now",
        type="primary"
    )

    if not token:
        st.info(
            "Enter your Upstox token in the sidebar."
        )
        return

    if not auto and not clicked:
        st.info(
            "Click Fetch / Refresh Now."
        )
        return

    now = datetime.now(IST)

    market_window = (
        now.weekday() < 5
        and time(9, 20) <= now.time() <= time(15, 25)
    )

    if not market_window:
        st.warning(
            "Outside regular market window. "
            "NO TRADE. Quotes may be stale."
        )

    try:
        expiry_date = datetime.strptime(
            expiry.strip(),
            "%Y-%m-%d"
        ).date()

        if expiry_date < now.date():
            st.error(
                "Expiry date is in the past."
            )
            return

    except ValueError:
        st.error(
            "Use expiry format YYYY-MM-DD."
        )
        return

    identity = (
        INDEX_KEY + ":" + expiry.strip()
    )

    if identity != st.session_state.previous_identity:
        st.session_state.previous_prices = {}
        st.session_state.previous_time = None
        st.session_state.previous_identity = identity

    try:
        chain = get_option_chain(
            token,
            expiry.strip()
        )

        if chain.empty:
            st.warning(
                "No option chain data returned."
            )
            return

        candles = get_5minute_candles(token)
        indicators = calculate_indicators(candles)

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError
    ) as exc:
        st.error(
            f"Data fetch/processing failed: {exc}"
        )
        return

    # ------------------------------------------------------
    # INDEX AND OI SUMMARY
    # ------------------------------------------------------

    valid_spots = chain["spot"].dropna()

    spot = (
        float(valid_spots.iloc[0])
        if not valid_spots.empty
        else np.nan
    )

    ce_oi = chain.loc[
        chain["side"] == "CE",
        "oi"
    ].sum()

    pe_oi = chain.loc[
        chain["side"] == "PE",
        "oi"
    ].sum()

    pcr = (
        pe_oi / ce_oi
        if ce_oi > 0
        else np.nan
    )

    a, b, c = st.columns(3)

    a.metric(
        "NIFTY Spot",
        f"{spot:,.2f}"
        if np.isfinite(spot)
        else "N/A"
    )

    b.metric(
        "Put/Call OI Ratio",
        f"{pcr:.2f}"
        if np.isfinite(pcr)
        else "N/A"
    )

    c.metric(
        "5-minute candles",
        len(indicators)
    )

    st.caption(
        f"Last API fetch: "
        f"{now:%d %b %Y %I:%M:%S %p} IST"
    )

    # ------------------------------------------------------
    # COMPLETED 5-MINUTE BARS
    # ------------------------------------------------------

    completed = indicators[
        indicators["timestamp"]
        + pd.Timedelta(minutes=5)
        <= pd.Timestamp(now)
    ].copy()

    if completed.empty:
        st.error(
            "No completed 5-minute candle available."
        )
        return

    last_bar_time = completed[
        "timestamp"
    ].iloc[-1]

    fresh_candle = (
        now - last_bar_time.to_pydatetime()
        <= timedelta(minutes=12)
    )

    # Check that the last bar belongs to today.
    same_day = (
        last_bar_time.date() == now.date()
    )

    fresh_candle = (
        fresh_candle and same_day
    )

    side, trend_reason, latest = find_direction(
        completed,
        adx_min
    )

    st.subheader(
        "NIFTY 5-Minute Trend"
    )

    st.write(
        f"**Direction:** {side}"
    )

    st.write(
        f"**Reason:** {trend_reason}"
    )

    if latest is not None:
        a, b, c, d = st.columns(4)

        a.metric(
            "EMA 9",
            f"{latest['ema9']:.2f}"
        )

        b.metric(
            "EMA 20",
            f"{latest['ema20']:.2f}"
        )

        c.metric(
            "ADX",
            f"{latest['adx']:.1f}"
        )

        d.metric(
            "ATR",
            f"{latest['atr']:.2f}"
        )

        if np.isfinite(latest["vwap"]):
            st.write(
                f"VWAP: {latest['vwap']:.2f}"
            )
        else:
            st.caption(
                "Index VWAP unavailable: "
                "zero or missing index volume."
            )

    st.caption(
        "Last completed bar starts: "
        + last_bar_time.strftime(
            "%d %b %Y %H:%M IST"
        )
    )

    # ------------------------------------------------------
    # SNAPSHOT COMPARISON
    # ------------------------------------------------------

    previous_time = st.session_state.previous_time

    comparison_valid = (
        market_window
        and previous_time is not None
        and timedelta(seconds=20)
        <= now - previous_time
        <= timedelta(minutes=3)
    )

    previous_prices = (
        st.session_state.previous_prices
        if comparison_valid
        else {}
    )

    rankings = []

    if (
        side in ("CE", "PE")
        and np.isfinite(spot)
    ):
        rankings = rank_options(
            chain,
            side,
            spot,
            min_volume,
            max_spread,
            max_iv,
            previous_prices
        )

    # ------------------------------------------------------
    # SINGLE FINAL DECISION
    # ------------------------------------------------------

    st.divider()

    st.subheader(
        "FINAL TRADING DECISION"
    )

    decision = "NO TRADE"
    reasons = []

    if not market_window:
        reasons.append(
            "Market is outside regular trading hours."
        )

    if not fresh_candle:
        reasons.append(
            "5-minute market data is stale."
        )

    if side == "NONE":
        reasons.append(
            "No confirmed NIFTY direction."
        )

    if not comparison_valid:
        reasons.append(
            "Waiting for two recent market-hours "
            "snapshots, 20-180 seconds apart."
        )

    if not rankings:
        reasons.append(
            "No directional option candidates."
        )

    if rankings:

        best = rankings[0]
        row = best["row"]
        score = best["score"]
        checks = best["checks"]

        if score < min_score:
            reasons.append(
                f"Option score {score}/6 "
                f"is below {min_score}/6."
            )

        if not checks["Premium momentum"]:
            reasons.append(
                "Premium momentum is not confirmed."
            )

        # Mandatory liquidity checks.
        mandatory = (
            checks["Delta"]
            and checks["Volume"]
            and checks["Spread"]
            and checks["IV"]
            and checks["Premium momentum"]
        )

        if not mandatory:
            reasons.append(
                "One or more mandatory option "
                "quality checks failed."
            )

        if (
            market_window
            and fresh_candle
            and comparison_valid
            and score >= min_score
            and mandatory
        ):
            decision = (
                f"{side} BUY CANDIDATE"
            )

    if decision == "NO TRADE":

        st.info(
            "NO TRADE"
        )

        for reason in reasons:
            st.write(
                "• " + reason
            )

    else:

        st.success(
            decision
        )

        entry = (
            row["ask"]
            if np.isfinite(row["ask"])
            and row["ask"] > 0
            else row["ltp"]
        )

        # Illustrative option-premium levels only.
        risk = entry * 0.12

        sl = max(
            0.05,
            entry - risk
        )

        target = (
            entry + risk * reward_risk
        )

        st.write(
            f"**Selected Strike:** "
            f"{row['strike']:g} {side}"
        )

        st.write(
            f"**Option Score:** {score}/6"
        )

        st.write(
            f"**Indicative Entry:** ₹{entry:.2f}"
        )

        st.write(
            f"**Illustrative SL:** ₹{sl:.2f}"
        )

        st.write(
            f"**Illustrative Target:** "
            f"₹{target:.2f}"
        )

        st.warning(
            "These entry, SL and target levels "
            "are illustrative, not backtested "
            "or validated execution levels."
        )

    # ------------------------------------------------------
    # TOP THREE CANDIDATES
    # ------------------------------------------------------

    st.divider()

    st.subheader(
        "Top 3 Candidates"
    )

    if rankings:

        display = []

        for item in rankings[:3]:

            r = item["row"]

            display.append({
                "Strike": (
                    f"{r['strike']:g} "
                    f"{r['side']}"
                ),
                "Score": (
                    f"{item['score']}/6"
                ),
                "LTP": r["ltp"],
                "Delta": r["delta"],
                "IV": r["iv"],
                "Spread %": r["spread_pct"],
                "Volume": r["volume"],
                "OI": r["oi"],
                "OI vs prev close": (
                    r["oi_change_close"]
                ),
                "Premium rising": (
                    item["checks"]["Premium momentum"]
                )
            })

        st.dataframe(
            pd.DataFrame(display),
            hide_index=True,
            use_container_width=True
        )

    else:

        st.write(
            "No candidates while trend is unconfirmed."
        )

    # ------------------------------------------------------
    # COMPLETE OPTION CHAIN
    # ------------------------------------------------------

    with st.expander(
        "Full Option Chain and Greeks"
    ):

        st.dataframe(
            chain.drop(
                columns=["instrument"]
            ),
            hide_index=True,
            use_container_width=True
        )

        st.download_button(
            "Download Option Chain CSV",
            data=chain.to_csv(
                index=False
            ),
            file_name="nifty_option_chain.csv",
            mime="text/csv"
        )

    st.warning(
        "Research-only screening. "
        "Not a validated profitable strategy. "
        "OI change vs previous close does not "
        "prove buying or writing. "
        "The regular-hours check does not "
        "independently verify NSE holidays. "
        "No automatic orders are placed."
    )

    # ------------------------------------------------------
    # SAVE SNAPSHOT FOR NEXT REFRESH
    # ------------------------------------------------------

    if market_window and fresh_candle:

        st.session_state.previous_prices = {
            r["instrument"]: r["ltp"]
            for _, r in chain.iterrows()
            if (
                r["instrument"]
                and np.isfinite(r["ltp"])
                and r["ltp"] > 0
            )
        }

        st.session_state.previous_time = now


dashboard()
