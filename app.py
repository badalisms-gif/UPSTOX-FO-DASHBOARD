
import os
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="NIFTY F&O | Chain + Market Trend",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com"
SESSION = requests.Session()

INDEX_KEYS = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "FIN NIFTY": "NSE_INDEX|Nifty Fin Service"
}


def number(value, default=0.0):
    try:
        result = float(value)
        return result if np.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def api_get(path, token, params=None):
    response = SESSION.get(
        BASE + path,
        params=params,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json"
        },
        timeout=25
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Upstox HTTP {response.status_code}: "
            f"{response.text[:250]}"
        )

    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError(str(payload.get("errors", "API error")))

    return payload.get("data") or {}


def get_expiry(token, key, requested):
    requested = requested.strip()

    if requested != "current_week":
        return requested

    data = api_get(
        "/v2/option/contract",
        token,
        {"instrument_key": key}
    )

    today = datetime.now(IST).date()

    dates = sorted({
        item.get("expiry")
        for item in data
        if item.get("expiry")
        and date.fromisoformat(item["expiry"]) >= today
    })

    if not dates:
        raise RuntimeError("No upcoming option expiry found.")

    return dates[0]


def get_chain(token, key, expiry):
    data = api_get(
        "/v2/option/chain",
        token,
        {
            "instrument_key": key,
            "expiry_date": expiry
        }
    )

    records = []

    for item in data:
        strike = number(item.get("strike_price"))
        spot = number(item.get("underlying_spot_price"))

        for side, field in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            option = item.get(field) or {}
            market = option.get("market_data") or {}
            greeks = option.get("option_greeks") or {}

            bid = number(market.get("bid_price"))
            ask = number(market.get("ask_price"))
            ltp = number(market.get("ltp"))
            oi = number(market.get("oi"))
            previous_oi = number(market.get("prev_oi"))

            spread = (
                (ask - bid) / ((ask + bid) / 2) * 100
                if ask >= bid > 0 else None
            )

            records.append({
                "strike": strike,
                "side": side,
                "spot": spot,
                "ltp": ltp,
                "oi": oi,
                "prev_oi": previous_oi,
                "oi_change": oi - previous_oi,
                "volume": number(market.get("volume")),
                "bid": bid,
                "ask": ask,
                "spread_pct": spread,
                "iv": number(greeks.get("iv"), None),
                "delta": number(greeks.get("delta"), None),
                "gamma": number(greeks.get("gamma"), None),
                "theta": number(greeks.get("theta"), None),
                "vega": number(greeks.get("vega"), None),
                "instrument": option.get("instrument_key", "")
            })

    return pd.DataFrame(records)


def score_options(df, side, max_spread, max_iv, min_volume,
                  previous, now):
    if df.empty:
        return pd.DataFrame()

    spot = number(df["spot"].iloc[0])

    strikes = sorted(df["strike"].unique())
    differences = np.diff(strikes)
    differences = differences[differences > 0]
    step = float(differences.min()) if len(differences) else 50

    candidates = df[
        (df.side == side) &
        ((df.strike - spot).abs() <= step * 3)
    ].copy()

    opposite = "PE" if side == "CE" else "CE"
    results = []

    for _, row in candidates.iterrows():
        reasons = []
        failures = []

        wall = df[
            (df.side == opposite) &
            ((df.strike - spot).abs() <= 2 * step)
        ]

        if side == "CE":
            wall = wall[wall.strike <= spot]
        else:
            wall = wall[wall.strike >= spot]

        tests = [
            (
                wall.oi_change.sum() > 0,
                "Opposite-side OI buildup"
            ),
            (
                row.oi_change > 0,
                "Selected contract OI increased"
            ),
            (
                row.volume >= min_volume and row.volume > 0,
                "Volume sufficient"
            ),
            (
                row.spread_pct is not None
                and 0 <= row.spread_pct <= max_spread,
                "Bid/ask spread acceptable"
            ),
            (
                row.delta is not None
                and 0.35 <= abs(row.delta) <= 0.75,
                "Delta acceptable"
            ),
            (
                row.iv is not None
                and 0 < row.iv <= max_iv,
                "IV acceptable"
            )
        ]

        for passed, label in tests:
            (reasons if passed else failures).append(label)

        old = previous.get(row.instrument)

        fresh = (
            old is not None
            and old.get("time") is not None
            and 0 < (now - old["time"]).total_seconds() <= 180
        )

        if fresh:
            if row.ltp > old["ltp"]:
                reasons.append("Premium rising since previous snapshot")
            else:
                failures.append("Premium not rising")
        else:
            failures.append("Recent comparison snapshot unavailable")

        score = len(reasons)

        entry = row.ask if row.ask > 0 else row.ltp
        risk = max(entry * 0.12, 1.0)

        results.append({
            **row.to_dict(),
            "score": score,
            "reasons": reasons,
            "failures": failures,
            "entry": entry,
            "risk": risk,
            "fresh": fresh
        })

    if not results:
        return pd.DataFrame()

    result = pd.DataFrame(results)
    result["atm_distance"] = (result.strike - spot).abs()

    return result.sort_values(
        ["score", "atm_distance"],
        ascending=[False, True]
    ).head(3)


def parse_candles(data):
    rows = data.get("candles", []) if isinstance(data, dict) else []

    if not rows:
        return pd.DataFrame()

    records = []

    for candle in rows:
        try:
            stamp = pd.to_datetime(candle[0], utc=True).tz_convert(IST)
            records.append({
                "timestamp": stamp,
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]) if len(candle) > 5 else 0
            })
        except (ValueError, TypeError, IndexError):
            continue

    if not records:
        return pd.DataFrame()

    frame = pd.DataFrame(records)

    return (
        frame.drop_duplicates("timestamp")
        .sort_values("timestamp")
        .set_index("timestamp")
    )


def get_intraday_candles(token, key):
    data = api_get(
        f"/v2/historical-candle/intraday/{key}/1minute",
        token
    )
    return parse_candles(data)


def get_history_candles(token, key, today):
    # Upstox V3 historical endpoint supports 1-minute candles.
    from_date = (today - timedelta(days=12)).isoformat()
    to_date = (today - timedelta(days=1)).isoformat()

    data = api_get(
        f"/v3/historical-candle/{key}/minutes/1/"
        f"{to_date}/{from_date}",
        token
    )

    return parse_candles(data)


def five_minute_candles(frame, now):
    if frame.empty:
        return pd.DataFrame()

    frame = frame.between_time("09:15", "15:29").copy()

    # Resample each trading day from the 09:15 session anchor.
    result = frame.resample(
        "5min",
        origin="start_day",
        offset="15min",
        label="left",
        closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    result = result.dropna(subset=["open", "close"])

    # Exclude incomplete current 5-minute candle.
    result = result[
        result.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)
    ]

    return result


def calculate_rsi(close, length=9):
    change = close.diff()
    gain = change.clip(lower=0)
    loss = (-change).clip(lower=0)

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)

    return rsi


def calculate_adx(frame, length=14):
    high = frame.high
    low = frame.low
    close = frame.close

    up = high.diff()
    down = -low.diff()

    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0),
        index=frame.index
    )

    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0),
        index=frame.index
    )

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(
        alpha=1 / length, adjust=False,
        min_periods=length
    ).mean()

    plus_di = 100 * (
        plus_dm.ewm(
            alpha=1 / length, adjust=False,
            min_periods=length
        ).mean() / atr.replace(0, np.nan)
    )

    minus_di = 100 * (
        minus_dm.ewm(
            alpha=1 / length, adjust=False,
            min_periods=length
        ).mean() / atr.replace(0, np.nan)
    )

    dx = (
        100 * (plus_di - minus_di).abs() /
        (plus_di + minus_di).replace(0, np.nan)
    )

    adx = dx.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()

    return plus_di, minus_di, adx


def calculate_trend(history, intraday, now):
    if intraday.empty:
        return None, "Today's intraday candles unavailable."

    today = now.date()

    current = intraday[intraday.index.date == today]

    if current.empty:
        return None, "No current-session candles."

    if now.time() < time(9, 30):
        return {
            "direction": "OPENING / WAIT",
            "confidence": "LOW",
            "reason": "Wait for 09:15–09:30 opening range."
        }, None

    if history.empty:
        return None, "Previous-session history unavailable."

    combined = pd.concat([history, current])
    combined = combined[
        ~combined.index.duplicated(keep="last")
    ].sort_index()

    bars = five_minute_candles(combined, now)

    if len(bars) < 40:
        return None, "Insufficient historical 5-minute candles."

    bars["ema9"] = bars.close.ewm(
        span=9, adjust=False
    ).mean()

    bars["ema20"] = bars.close.ewm(
        span=20, adjust=False
    ).mean()

    bars["rsi9"] = calculate_rsi(bars.close, 9)

    plus_di, minus_di, adx = calculate_adx(bars, 14)

    bars["plus_di"] = plus_di
    bars["minus_di"] = minus_di
    bars["adx"] = adx

    today_bars = bars[bars.index.date == today]

    if len(today_bars) < 3:
        return {
            "direction": "OPENING / WAIT",
            "confidence": "LOW",
            "reason": "Three completed 5-minute candles required."
        }, None

    latest = today_bars.iloc[-1]

    # Today's VWAP from 1-minute index data, when valid volume exists.
    # Index volume may be unavailable/zero, so never invent VWAP.
    today_minutes = current[
        current.index + pd.Timedelta(minutes=1)
        <= pd.Timestamp(now)
    ]

    vol = today_minutes.volume.fillna(0)
    valid_volume = vol.sum() > 0

    vwap = None

    if valid_volume:
        typical = (
            today_minutes.high +
            today_minutes.low +
            today_minutes.close
        ) / 3

        vwap = float((typical * vol).sum() / vol.sum())

    opening = today_bars.iloc[:3]
    opening_high = float(opening.high.max())
    opening_low = float(opening.low.min())

    close = float(latest.close)
    ema9 = float(latest.ema9)
    ema20 = float(latest.ema20)
    rsi = number(latest.rsi9, None)
    adx_value = number(latest.adx, None)
    plus = number(latest.plus_di, None)
    minus = number(latest.minus_di, None)

    bullish = (
        close > ema9 and
        ema9 > ema20 and
        rsi is not None and rsi > 52
    )

    bearish = (
        close < ema9 and
        ema9 < ema20 and
        rsi is not None and rsi < 48
    )

    breakout_up = close > opening_high
    breakout_down = close < opening_low

    if bullish and (breakout_up or close > opening_low):
        direction = "BULLISH"
    elif bearish and (breakout_down or close < opening_high):
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    if (
        adx_value is not None and
        adx_value >= 25 and
        (
            (direction == "BULLISH" and plus > minus)
            or
            (direction == "BEARISH" and minus > plus)
        )
    ):
        confidence = "STRONG"
    elif direction != "NEUTRAL":
        confidence = "EARLY / MODERATE"
    else:
        confidence = "LOW / MIXED"

    if len(today_bars) <= 4:
        confidence = "EARLY / LOW"

    return {
        "direction": direction,
        "confidence": confidence,
        "close": close,
        "ema9": ema9,
        "ema20": ema20,
        "rsi": rsi,
        "adx": adx_value,
        "plus_di": plus,
        "minus_di": minus,
        "vwap": vwap,
        "opening_high": opening_high,
        "opening_low": opening_low,
        "last_bar": str(today_bars.index[-1])
    }, None


st.title("NIFTY F&O | Option Chain + Market Trend")
st.caption(
    "Two independent systems | Trend from 09:30 AM | "
    "Research only | No automatic orders"
)

with st.sidebar:
    st.header("Connection")

    token = st.text_input(
        "Upstox Access Token",
        value=os.environ.get("UPSTOX_ACCESS_TOKEN", ""),
        type="password"
    )

    underlying = st.selectbox(
        "Underlying",
        list(INDEX_KEYS.keys())
    )

    instrument_key = INDEX_KEYS[underlying]

    expiry_input = st.text_input(
        "Expiry",
        value="current_week",
        help="current_week or YYYY-MM-DD"
    )

    st.header("Option Chain Filters")

    max_spread = st.slider(
        "Maximum spread %",
        0.5, 10.0, 3.0, 0.5
    )

    max_iv = st.slider(
        "Maximum IV %",
        10, 100, 40
    )

    min_volume = st.number_input(
        "Minimum volume",
        min_value=0,
        value=10000,
        step=1000
    )

    min_score = st.slider(
        "Minimum BUY score",
        4, 7, 6
    )

    rr = st.slider(
        "Illustrative reward/risk",
        1.0, 3.0, 1.75, 0.25
    )

    auto = st.toggle(
        "Auto-refresh every 30 seconds",
        value=False
    )


if "previous" not in st.session_state:
    st.session_state.previous = {}

if "identity" not in st.session_state:
    st.session_state.identity = ""

if "refresh_requested" not in st.session_state:
    st.session_state.refresh_requested = False


@st.fragment(run_every="30s" if auto else None)
def dashboard():
    if st.button("Fetch / Refresh Now", type="primary"):
        st.session_state.refresh_requested = True

    if not token:
        st.info("Enter your Upstox token in the sidebar.")
        return

    if not auto and not st.session_state.refresh_requested:
        st.info("Click Fetch / Refresh Now.")
        return

    st.session_state.refresh_requested = False

    now = datetime.now(IST)

    market_hours = (
        now.weekday() < 5 and
        time(9, 15) <= now.time() <= time(15, 30)
    )

    # This checks weekday and time, not the NSE holiday calendar.
    st.caption(
        f"Last fetch attempt: {now:%d %b %Y %I:%M:%S %p} IST"
    )

    if not market_hours:
        st.warning(
            "Outside regular market hours. Historical/stale "
            "quotes may appear. Live BUY disabled."
        )

    identity = f"{instrument_key}:{expiry_input}"

    if identity != st.session_state.identity:
        st.session_state.previous = {}
        st.session_state.identity = identity

    st.header("1. OPTION CHAIN + GREEKS (INDEPENDENT)")

    chain = pd.DataFrame()
    expiry = None

    try:
        expiry = get_expiry(
            token, instrument_key, expiry_input
        )

        chain = get_chain(
            token, instrument_key, expiry
        )

    except Exception as exc:
        st.error(f"Option Chain unavailable: {exc}")

    ce_best = None
    pe_best = None

    if not chain.empty:
        spot = float(chain.spot.iloc[0])

        ce_total = chain.loc[chain.side == "CE", "oi"].sum()
        pe_total = chain.loc[chain.side == "PE", "oi"].sum()
        pcr = pe_total / ce_total if ce_total > 0 else None

        a, b, c, d = st.columns(4)

        a.metric("Spot", f"{spot:,.2f}")
        b.metric(
            "PCR",
            f"{pcr:.2f}" if pcr is not None else "N/A"
        )
        c.metric("CE total OI", f"{ce_total:,.0f}")
        d.metric("PE total OI", f"{pe_total:,.0f}")

        st.caption(f"Selected expiry: {expiry}")

        previous = st.session_state.previous

        ce = score_options(
            chain, "CE", max_spread, max_iv,
            min_volume, previous, now
        )

        pe = score_options(
            chain, "PE", max_spread, max_iv,
            min_volume, previous, now
        )

        left, right = st.columns(2)

        for col, side, candidates in [
            (left, "CALL (CE)", ce),
            (right, "PUT (PE)", pe)
        ]:
            with col:
                st.subheader(f"Top 3 {side} candidates")

                if candidates.empty:
                    st.warning("No eligible contracts.")
                    continue

                view = candidates.copy()

                view["Entry*"] = view["entry"].round(2)
                view["SL*"] = (
                    view["entry"] - view["risk"]
                ).clip(lower=0.05).round(2)

                view["Target*"] = (
                    view["entry"] + view["risk"] * rr
                ).round(2)

                view["Score"] = (
                    view["score"].astype(str) + "/7"
                )

                st.dataframe(
                    view[[
                        "strike", "Score", "ltp",
                        "delta", "iv", "spread_pct",
                        "Entry*", "SL*", "Target*"
                    ]],
                    hide_index=True,
                    use_container_width=True
                )

                best = candidates.iloc[0]

                if side == "CALL (CE)":
                    ce_best = best
                else:
                    pe_best = best

                status = (
                    "BUY CANDIDATE"
                    if market_hours
                    and best["score"] >= min_score
                    and best["fresh"]
                    else "WAIT"
                )

                st.write(
                    f"**Best candidate:** "
                    f"{best['strike']:g} {best['side']} | "
                    f"Score {best['score']}/7"
                )

                st.write(f"**Chain status:** {status}")

                with st.expander("Why this score?"):
                    for reason in best["reasons"]:
                        st.success(reason)
                    for reason in best["failures"]:
                        st.warning(reason)

        with st.expander("Full option chain and Greeks"):
            st.dataframe(
                chain,
                hide_index=True,
                use_container_width=True
            )

        st.session_state.previous = {
            row.instrument: {
                "ltp": row.ltp,
                "time": now
            }
            for _, row in chain.iterrows()
            if row.instrument and row.ltp > 0
        }

    else:
        st.warning("No option-chain data available.")

    st.caption(
        "*Entry uses ask/LTP. SL uses illustrative 12% "
        "premium risk; target uses selected R:R. "
        "These are not validated trade levels."
    )

    st.divider()
    st.header("2. MARKET TREND (INDEPENDENT)")

    trend = None
    trend_error = None

    if not market_hours:
        st.info(
            "TREND: UNAVAILABLE — market outside regular hours."
        )

    else:
        try:
            history = get_history_candles(
                token, instrument_key, now.date()
            )

            intraday = get_intraday_candles(
                token, instrument_key
            )

            trend, trend_error = calculate_trend(
                history, intraday, now
            )

        except Exception as exc:
            trend_error = str(exc)

        if trend_error:
            st.warning(
                "TREND: UNAVAILABLE — " + trend_error
            )

        elif trend:
            direction = trend["direction"]
            confidence = trend["confidence"]

            if direction == "BULLISH":
                st.success(f"TREND: BULLISH | {confidence}")
            elif direction == "BEARISH":
                st.error(f"TREND: BEARISH | {confidence}")
            else:
                st.info(f"TREND: {direction} | {confidence}")

            if "close" in trend:
                a, b, c, d = st.columns(4)

                a.metric(
                    "NIFTY / Index",
                    f"{trend['close']:.2f}"
                )
                b.metric(
                    "EMA 9",
                    f"{trend['ema9']:.2f}"
                )
                c.metric(
                    "EMA 20",
                    f"{trend['ema20']:.2f}"
                )
                d.metric(
                    "RSI 9",
                    f"{trend['rsi']:.1f}"
                    if trend["rsi"] is not None else "N/A"
                )

                a, b, c = st.columns(3)

                a.metric(
                    "ADX",
                    f"{trend['adx']:.1f}"
                    if trend["adx"] is not None else "N/A"
                )

                b.metric(
                    "Opening High",
                    f"{trend['opening_high']:.2f}"
                )

                c.metric(
                    "Opening Low",
                    f"{trend['opening_low']:.2f}"
                )

                st.write(
                    "**VWAP:** " +
                    (
                        f"{trend['vwap']:.2f}"
                        if trend["vwap"] is not None
                        else "Unavailable from index volume"
                    )
                )

                st.caption(
                    f"Last completed 5-minute bar: "
                    f"{trend['last_bar']}"
                )

    st.divider()
    st.header("3. SIGNAL AGREEMENT (INFORMATION ONLY)")

    if ce_best is None or pe_best is None:
        st.info("Option-chain comparison unavailable.")

    elif trend is None or "close" not in trend:
        st.info(
            "Trend unavailable. Option Chain candidates "
            "remain independent and unblocked."
        )

    else:
        ce_score = int(ce_best["score"])
        pe_score = int(pe_best["score"])

        if ce_score == pe_score:
            chain_bias = "NO DIRECTIONAL EDGE"
        elif ce_score > pe_score:
            chain_bias = "CE LEADING"
        else:
            chain_bias = "PE LEADING"

        direction = trend["direction"]

        st.write(f"**Option Chain:** {chain_bias}")
        st.write(f"**Market Trend:** {direction}")

        if chain_bias == "NO DIRECTIONAL EDGE":
            st.info(
                "Chain scores are tied. No directional "
                "preference from this scoring system."
            )

        elif (
            chain_bias == "CE LEADING"
            and direction == "BULLISH"
        ) or (
            chain_bias == "PE LEADING"
            and direction == "BEARISH"
        ):
            st.success("ALIGNED — both systems agree.")

        elif direction == "NEUTRAL":
            st.info("MIXED — market trend is neutral.")

        else:
            st.warning(
                "CONFLICT — Option Chain and market "
                "trend do not agree."
            )

    st.warning(
        "Research-only screening. Scores are heuristic, "
        "not a validated profitable strategy. "
        "OI change versus previous close does not "
        "prove fresh writing or buying. "
        "A single snapshot is insufficient for trading."
    )


dashboard()
