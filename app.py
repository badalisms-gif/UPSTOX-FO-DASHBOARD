
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
    page_title="NSE Options | Scanner & Trend",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"
MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

INDEXES = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "FIN NIFTY": "NSE_INDEX|Nifty Fin Service"
}


def n(value, default=np.nan):
    try:
        result = float(value)
        return result if np.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def request_data(path, token, params=None):
    response = requests.get(
        API + path,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        },
        timeout=25
    )

    if not response.ok:
        raise RuntimeError(
            f"Upstox HTTP {response.status_code}: "
            f"{response.text[:220]}"
        )

    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError(
            str(payload.get("errors") or
                payload.get("message") or "API error")
        )

    return payload.get("data") or {}


@st.cache_data(ttl=86400)
def load_master():
    response = requests.get(MASTER, timeout=45)
    response.raise_for_status()
    return json.loads(gzip.decompress(response.content))


@st.cache_data(ttl=86400)
def instruments():
    rows = []

    for item in load_master():
        if item.get("segment") != "NSE_EQ":
            continue

        key = item.get("instrument_key")
        symbol = (
            item.get("trading_symbol") or
            item.get("tradingsymbol")
        )

        if key and symbol:
            rows.append({
                "symbol": str(symbol),
                "name": str(item.get("name") or symbol),
                "key": key
            })

    if not rows:
        raise RuntimeError("NSE equity instruments unavailable")

    return (
        pd.DataFrame(rows)
        .drop_duplicates("key")
        .sort_values("symbol")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=86400)
def fo_universe():
    items = load_master()
    equities = {}

    for item in items:
        if item.get("segment") == "NSE_EQ":
            key = item.get("instrument_key")
            symbol = str(
                item.get("trading_symbol") or
                item.get("tradingsymbol") or ""
            ).upper()

            if key and symbol:
                equities[key] = {
                    "symbol": symbol,
                    "key": key
                }

    underlying_keys = set()
    underlying_names = set()

    for item in items:
        if item.get("segment") != "NSE_FO":
            continue

        option_type = str(
            item.get("instrument_type") or ""
        ).upper()

        if option_type not in ("CE", "PE"):
            continue

        underlying_key = item.get("underlying_key")

        if underlying_key:
            underlying_keys.add(underlying_key)

        underlying_name = str(
            item.get("name") or
            item.get("underlying_symbol") or ""
        ).upper()

        if underlying_name:
            underlying_names.add(underlying_name)

    rows = [
        value
        for key, value in equities.items()
        if key in underlying_keys
        or value["symbol"] in underlying_names
    ]

    if not rows:
        raise RuntimeError(
            "F&O stock mapping unavailable in instrument master"
        )

    return (
        pd.DataFrame(rows)
        .drop_duplicates("key")
        .sort_values("symbol")
        .reset_index(drop=True)
    )


def scan_quotes(token, universe):
    rows = []
    failures = []
    keys = universe["key"].tolist()

    for start in range(0, len(keys), 40):
        batch = keys[start:start + 40]

        try:
            data = request_data(
                "/v2/market-quote/quotes",
                token,
                {"instrument_key": ",".join(batch)}
            )

            for item in data.values():
                if not isinstance(item, dict):
                    continue

                key = (
                    item.get("instrument_token") or
                    item.get("instrument_key")
                )

                if not key:
                    continue

                ltp = n(item.get("last_price"))

                ohlc = item.get("ohlc") or {}

                if isinstance(ohlc, list):
                    ohlc = next(
                        (
                            value for value in ohlc
                            if value.get("interval")
                            in ("1d", "day", "D")
                        ),
                        {}
                    )

                day_open = n(ohlc.get("open"))
                high = n(ohlc.get("high"))
                low = n(ohlc.get("low"))
                volume = n(item.get("volume"), 0)

                net_change = n(item.get("net_change"))
                previous_close = n(
                    item.get("previous_close") or
                    item.get("prev_close")
                )

                # Previous close must not be assumed
                # from the current daily OHLC close.

                if np.isfinite(net_change) and np.isfinite(ltp):
                    previous_close = ltp - net_change
                    close_source = "net_change"

                elif np.isfinite(previous_close):
                    close_source = "previous_close"

                else:
                    previous_close = np.nan
                    close_source = "unavailable"

                if not np.isfinite(ltp) or ltp <= 0:
                    continue

                if (
                    np.isfinite(previous_close)
                    and previous_close > 0
                ):
                    change_pct = (
                        (ltp / previous_close - 1) * 100
                    )
                else:
                    change_pct = np.nan

                if np.isfinite(day_open) and day_open > 0:
                    from_open = (
                        (ltp / day_open - 1) * 100
                    )
                else:
                    from_open = np.nan

                if (
                    np.isfinite(high)
                    and np.isfinite(low)
                    and high > low
                ):
                    range_position = (
                        (ltp - low) / (high - low) * 100
                    )
                else:
                    range_position = np.nan

                rows.append({
                    "key": key,
                    "LTP": ltp,
                    "Prev Close": previous_close,
                    "Change %": change_pct,
                    "From Open %": from_open,
                    "Range Position %": range_position,
                    "Volume": volume,
                    "Close Source": close_source
                })

        except Exception as exc:
            failures.append(str(exc)[:160])

    if not rows:
        raise RuntimeError(
            "No usable quotes returned. " +
            "; ".join(failures[:2])
        )

    quotes = pd.DataFrame(rows).drop_duplicates("key")

    frame = universe.merge(
        quotes,
        on="key",
        how="inner"
    )

    if frame.empty:
        raise RuntimeError(
            "F&O stock keys and quote keys do not match"
        )

    valid = (
        frame["Change %"].notna()
        & frame["From Open %"].notna()
        & frame["Range Position %"].notna()
    )

    bullish = (
        valid
        & (frame["Change %"] > 0.35)
        & (frame["From Open %"] > 0)
        & (frame["Range Position %"] >= 60)
    )

    bearish = (
        valid
        & (frame["Change %"] < -0.35)
        & (frame["From Open %"] < 0)
        & (frame["Range Position %"] <= 40)
    )

    frame["Direction"] = np.select(
        [bullish, bearish, ~valid],
        ["CE WATCH", "PE WATCH", "DATA UNAVAILABLE"],
        default="NEUTRAL"
    )

    strength = (
        np.minimum(
            frame["Change %"].abs().fillna(0) / 2.5,
            1
        ) * 60
    )

    position = np.where(
        bullish,
        frame["Range Position %"].fillna(0),
        np.where(
            bearish,
            100 - frame["Range Position %"].fillna(100),
            0
        )
    )

    score = (
        strength + np.clip(position, 0, 100) * 0.4
    ).clip(0, 100).round().astype(int)

    frame["Score / 100"] = np.where(
        bullish | bearish,
        score,
        0
    )

    frame["Priority"] = np.select(
        [
            bullish | bearish,
            frame["Direction"] == "NEUTRAL"
        ],
        [0, 1],
        default=2
    )

    frame = frame.sort_values(
        ["Priority", "Score / 100", "symbol"],
        ascending=[True, False, True]
    ).reset_index(drop=True)

    frame.insert(
        0,
        "Rank",
        np.arange(1, len(frame) + 1)
    )

    frame.loc[
        frame["Priority"] != 0,
        "Rank"
    ] = 0

    return frame.drop(columns=["Priority"]), failures


def contracts(token, key):
    data = request_data(
        "/v2/option/contract",
        token,
        {"instrument_key": key}
    )

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected option contract response"
        )

    today = datetime.now(IST).date()
    expiries = set()

    for item in data:
        expiry = item.get("expiry")

        if expiry:
            try:
                expiry_date = datetime.strptime(
                    expiry, "%Y-%m-%d"
                ).date()

                if expiry_date >= today:
                    expiries.add(expiry)

            except ValueError:
                continue

    return sorted(expiries)


def option_chain(token, key, expiry):
    data = request_data(
        "/v2/option/chain",
        token,
        {
            "instrument_key": key,
            "expiry_date": expiry
        }
    )

    rows = []

    for item in data:
        for side, field in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            option = item.get(field) or {}
            market = option.get("market_data") or {}
            greeks = option.get("option_greeks") or {}

            bid = n(market.get("bid_price"))
            ask = n(market.get("ask_price"))

            spread = np.nan

            if (
                np.isfinite(bid)
                and np.isfinite(ask)
                and ask >= bid > 0
            ):
                spread = (
                    100 * (ask - bid) / ((ask + bid) / 2)
                )

            oi = n(market.get("oi"), 0)
            previous_oi = n(market.get("prev_oi"), 0)

            rows.append({
                "strike": n(item.get("strike_price")),
                "side": side,
                "spot": n(item.get("underlying_spot_price")),
                "ltp": n(market.get("ltp")),
                "oi": oi,
                "prev_oi": previous_oi,
                "oi_change": oi - previous_oi,
                "volume": n(market.get("volume"), 0),
                "bid": bid,
                "ask": ask,
                "spread_pct": spread,
                "iv": n(greeks.get("iv")),
                "delta": n(greeks.get("delta")),
                "gamma": n(greeks.get("gamma")),
                "theta": n(greeks.get("theta")),
                "vega": n(greeks.get("vega")),
                "instrument": option.get("instrument_key") or ""
            })

    return pd.DataFrame(rows)


def candidate_list(
    df, side, old, old_time, now,
    min_vol, max_spread, max_iv, rr
):
    spot = (
        n(df.spot.dropna().iloc[0])
        if df.spot.notna().any()
        else np.nan
    )

    strikes = sorted(df.strike.dropna().unique())

    gaps = [
        b - a
        for a, b in zip(strikes, strikes[1:])
        if b > a
    ]

    step = min(gaps) if gaps else 50

    subset = df[
        (df.side == side)
        & ((df.strike - spot).abs() <= 3 * step)
        & (df.ltp > 0)
    ]

    opposite = df[
        df.side == ("PE" if side == "CE" else "CE")
    ]

    if side == "CE":
        wall = opposite[
            (opposite.strike <= spot)
            & (opposite.strike >= spot - 2 * step)
        ]
    else:
        wall = opposite[
            (opposite.strike >= spot)
            & (opposite.strike <= spot + 2 * step)
        ]

    fresh = (
        old_time is not None
        and 15 <= (now - old_time).total_seconds() <= 180
    )

    results = []

    for _, row in subset.iterrows():
        previous = old.get(row.instrument) if fresh else None

        premium_up = (
            previous is not None
            and row.ltp > previous
        )

        checks = {
            "Opposite OI buildup":
                wall.oi_change.sum() > 0,

            "Selected contract OI increased":
                row.oi_change > 0,

            "Volume sufficient":
                row.volume >= min_vol and row.volume > 0,

            "Spread acceptable":
                np.isfinite(row.spread_pct)
                and 0 <= row.spread_pct <= max_spread,

            "Delta acceptable":
                np.isfinite(row.delta)
                and 0.35 <= abs(row.delta) <= 0.75,

            "IV acceptable":
                np.isfinite(row.iv)
                and 0 < row.iv <= max_iv,

            "Premium rising":
                premium_up
        }

        entry = (
            row.ask
            if np.isfinite(row.ask) and row.ask > 0
            else row.ltp
        )

        risk = max(entry * 0.12, 1.0)

        results.append({
            "strike": row.strike,
            "side": side,
            "score": sum(checks.values()),
            "ltp": row.ltp,
            "delta": row.delta,
            "iv": row.iv,
            "spread": row.spread_pct,
            "oi": row.oi,
            "volume": row.volume,
            "premium_up": premium_up,
            "fresh": fresh,
            "entry": entry,
            "sl": max(0.05, entry - risk),
            "target": entry + risk * rr,
            "checks": checks,
            "distance": abs(row.strike - spot)
        })

    return sorted(
        results,
        key=lambda x: (-x["score"], x["distance"])
    )[:3]


def parse_candles(payload):
    raw = (
        payload.get("candles", [])
        if isinstance(payload, dict)
        else []
    )

    records = []

    for candle in raw:
        try:
            stamp = pd.to_datetime(
                candle[0], utc=True
            ).tz_convert(IST)

            values = [float(v) for v in candle[1:6]]

            records.append([stamp, *values])

        except (IndexError, ValueError, TypeError):
            continue

    if not records:
        return pd.DataFrame()

    return (
        pd.DataFrame(
            records,
            columns=[
                "timestamp", "open", "high",
                "low", "close", "volume"
            ]
        )
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .set_index("timestamp")
    )


def candle_data(token, key, now):
    encoded = quote(key, safe="")

    today = now.date()
    from_date = (today - timedelta(days=12)).isoformat()
    to_date = (today - timedelta(days=1)).isoformat()

    history_data = request_data(
        f"/v3/historical-candle/{encoded}/minutes/1/"
        f"{to_date}/{from_date}",
        token
    )

    intraday_data = request_data(
        f"/v2/historical-candle/intraday/{encoded}/1minute",
        token
    )

    return (
        parse_candles(history_data),
        parse_candles(intraday_data)
    )


def trend_calc(history, current, now):
    if current.empty:
        return None, "Current intraday candles unavailable"

    today = now.date()

    current = current[
        current.index.date == today
    ]

    if current.empty:
        return None, "No candles from today"

    if now.time() < time(9, 30):
        return {
            "direction": "OPENING / WAIT",
            "confidence": "LOW"
        }, None

    if history.empty:
        return None, "Previous-session history unavailable"

    merged = pd.concat([
        history, current
    ]).sort_index()

    merged = merged[
        ~merged.index.duplicated(keep="last")
    ].between_time("09:15", "15:29")

    counts = merged.close.resample("5min").count()

    bars = merged.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    bars = bars[
        (counts == 5)
        & (
            bars.index + pd.Timedelta(minutes=5)
            <= pd.Timestamp(now)
        )
    ].dropna(subset=["close"])

    if len(bars) < 40:
        return None, "Insufficient completed 5-minute candles"

    bars["ema9"] = bars.close.ewm(
        span=9, adjust=False
    ).mean()

    bars["ema20"] = bars.close.ewm(
        span=20, adjust=False
    ).mean()

    change = bars.close.diff()

    gain = change.clip(lower=0).ewm(
        alpha=1/9,
        adjust=False,
        min_periods=9
    ).mean()

    loss = (-change.clip(upper=0)).ewm(
        alpha=1/9,
        adjust=False,
        min_periods=9
    ).mean()

    bars["rsi"] = (
        100 - 100 / (1 + gain / loss.replace(0, np.nan))
    )

    bars.loc[
        (loss == 0) & (gain > 0), "rsi"
    ] = 100

    bars.loc[
        (gain == 0) & (loss > 0), "rsi"
    ] = 0

    up = bars.high.diff()
    down = -bars.low.diff()

    plus_dm = pd.Series(
        np.where(
            (up > down) & (up > 0), up, 0
        ),
        index=bars.index
    )

    minus_dm = pd.Series(
        np.where(
            (down > up) & (down > 0), down, 0
        ),
        index=bars.index
    )

    true_range = pd.concat([
        bars.high - bars.low,
        (bars.high - bars.close.shift()).abs(),
        (bars.low - bars.close.shift()).abs()
    ], axis=1).max(axis=1)

    atr = true_range.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    plus_di = (
        100 * plus_dm.ewm(
            alpha=1/14,
            adjust=False,
            min_periods=14
        ).mean() / atr.replace(0, np.nan)
    )

    minus_di = (
        100 * minus_dm.ewm(
            alpha=1/14,
            adjust=False,
            min_periods=14
        ).mean() / atr.replace(0, np.nan)
    )

    dx = (
        100 * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, np.nan)
    )

    bars["adx"] = dx.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    bars["plus_di"] = plus_di
    bars["minus_di"] = minus_di

    today_bars = bars[
        bars.index.date == today
    ]

    if len(today_bars) < 3:
        return {
            "direction": "OPENING / WAIT",
            "confidence": "LOW"
        }, None

    last = today_bars.iloc[-1]

    if (
        now - last.name.to_pydatetime()
        > timedelta(minutes=12)
    ):
        return None, "Latest completed candle is stale"

    opening = today_bars.iloc[:3]

    opening_high = opening.high.max()
    opening_low = opening.low.min()

    bullish = (
        last.close > last.ema9 > last.ema20
        and last.rsi > 52
    )

    bearish = (
        last.close < last.ema9 < last.ema20
        and last.rsi < 48
    )

    if bullish:
        direction = "BULLISH"
    elif bearish:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    strong = (
        np.isfinite(last.adx)
        and last.adx >= 25
        and (
            (bullish and last.plus_di > last.minus_di)
            or
            (bearish and last.minus_di > last.plus_di)
        )
    )

    if len(today_bars) <= 4:
        confidence = "EARLY / LOW"
    elif strong:
        confidence = "STRONG"
    elif direction != "NEUTRAL":
        confidence = "MODERATE"
    else:
        confidence = "MIXED"

    valid_minutes = current[
        current.index + pd.Timedelta(minutes=1)
        <= pd.Timestamp(now)
    ]

    volume = valid_minutes.volume.clip(lower=0)

    if volume.sum() > 0:
        typical = (
            valid_minutes.high
            + valid_minutes.low
            + valid_minutes.close
        ) / 3

        vwap = (typical * volume).sum() / volume.sum()
    else:
        vwap = np.nan

    return {
        "direction": direction,
        "confidence": confidence,
        "close": last.close,
        "ema9": last.ema9,
        "ema20": last.ema20,
        "rsi": last.rsi,
        "adx": last.adx,
        "vwap": vwap,
        "opening_high": opening_high,
        "opening_low": opening_low,
        "bar": str(last.name)
    }, None


st.title("NSE F&O | Scanner + Options Dashboard")

st.caption(
    "F&O Stock Scanner | Option Chain | Greeks | "
    "Independent Market Trend | Research Only"
)

with st.sidebar:
    st.header("Upstox Connection")

    token = st.text_input(
        "Upstox Access Token",
        value=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        type="password"
    )

    category = st.radio(
        "Instrument Type",
        ["Index Options", "Stock Options"]
    )

    if category == "Index Options":
        name = st.selectbox(
            "Index",
            list(INDEXES)
        )

        key = INDEXES[name]

    else:
        try:
            universe = instruments()

            selected = st.selectbox(
                "Search NSE Stock",
                universe.index.tolist(),
                format_func=lambda i: (
                    f"{universe.loc[i, 'symbol']} - "
                    f"{universe.loc[i, 'name']}"
                )
            )

            name = universe.loc[selected, "symbol"]
            key = universe.loc[selected, "key"]

        except Exception as exc:
            st.error(str(exc))
            st.stop()

    expiry_choice = st.selectbox(
        "Expiry",
        ["Auto: nearest available", "Enter manually"]
    )

    expiry_manual = ""

    if expiry_choice == "Enter manually":
        expiry_manual = st.text_input(
            "Expiry YYYY-MM-DD",
            value="2026-11-23"
        )

    st.header("Option Filters")

    min_vol = st.number_input(
        "Minimum Option Volume",
        min_value=0,
        value=10000,
        step=1000
    )

    max_spread = st.slider(
        "Maximum Spread %",
        0.5, 10.0, 3.0, 0.5
    )

    max_iv = st.slider(
        "Maximum IV %",
        10, 100, 40
    )

    min_score = st.slider(
        "Minimum Score / 7",
        4, 7, 6
    )

    rr = st.slider(
        "Reward / Risk",
        1.0, 3.0, 1.75, 0.25
    )

    auto = st.toggle(
        "Auto-refresh every 30 seconds",
        value=False
    )


if "identity" not in st.session_state:
    st.session_state.identity = ""

if "prior" not in st.session_state:
    st.session_state.prior = {}

if "prior_time" not in st.session_state:
    st.session_state.prior_time = None


@st.fragment(run_every="30s" if auto else None)
def dashboard():
    clicked = st.button(
        "Fetch / Refresh Now",
        type="primary"
    )

    if not token:
        st.info("Enter your Upstox token.")
        return

    if not auto and not clicked:
        st.info("Click Fetch / Refresh Now.")
        return

    now = datetime.now(IST)

    market_hours = (
        now.weekday() < 5
        and time(9, 15) <= now.time() <= time(15, 30)
    )

    st.caption(
        f"Last fetch: {now:%d %b %Y %I:%M:%S %p} IST"
    )

    if not market_hours:
        st.warning(
            "Market closed. Quotes may be stale. "
            "Live BUY signals disabled."
        )

    # ----------------------------------------
    # SECTION 0: STOCK SCANNER
    # ----------------------------------------

    st.header("0. F&O STOCK OPPORTUNITY SCANNER")

    st.caption(
        "Quote-based momentum ranking. "
        "Not a confirmed 5-minute breakout signal."
    )

    if st.button(
        "Scan all available F&O stocks",
        key="scan_fo"
    ):
        try:
            universe_fo = fo_universe()

            with st.spinner(
                f"Scanning {len(universe_fo)} F&O stocks..."
            ):
                ranking, errors = scan_quotes(
                    token, universe_fo
                )

            st.session_state["scanner_ranking"] = ranking
            st.session_state["scanner_time"] = now
            st.session_state["scanner_errors"] = errors

        except Exception as exc:
            st.error(f"Scanner unavailable: {exc}")

    ranking = st.session_state.get("scanner_ranking")

    if ranking is not None and not ranking.empty:
        scan_time = st.session_state.get("scanner_time")

        st.caption(
            f"Snapshot: {scan_time:%d %b %Y %I:%M:%S %p} IST "
            f"| Stocks: {len(ranking)}"
        )

        if not market_hours:
            st.warning(
                "Market closed: ranking is not a live opportunity."
            )

        if st.session_state.get("scanner_errors"):
            st.warning(
                "Some quote batches failed. "
                "Ranking may be incomplete."
            )

        columns = [
            "Rank",
            "symbol",
            "Direction",
            "Score / 100",
            "LTP",
            "Prev Close",
            "Change %",
            "From Open %",
            "Range Position %",
            "Volume"
        ]

        st.subheader("Top 10 CE / PE Watchlist")

        leaders = ranking[
            ranking.Direction.isin([
                "CE WATCH", "PE WATCH"
            ])
        ].head(10)

        st.dataframe(
            leaders[columns],
            hide_index=True,
            use_container_width=True
        )

        if leaders.empty:
            st.info(
                "No qualified CE/PE candidates. "
                "Do not trade based on volume alone."
            )

        st.subheader("Full F&O Ranking")

        st.caption(
            "Only CE/PE WATCH stocks receive a positive rank. "
            "Rank 0 means no qualified opportunity."
        )

        filter_choice = st.selectbox(
            "Ranking Filter",
            [
                "All",
                "CE WATCH",
                "PE WATCH",
                "NEUTRAL",
                "DATA UNAVAILABLE"
            ],
            key="rank_filter"
        )

        if filter_choice == "All":
            filtered = ranking
        else:
            filtered = ranking[
                ranking.Direction == filter_choice
            ]

        st.dataframe(
            filtered[columns],
            hide_index=True,
            use_container_width=True
        )

        st.download_button(
            "Download Ranking CSV",
            ranking[columns].to_csv(index=False),
            file_name="fo_scanner_ranking.csv",
            mime="text/csv"
        )

    else:
        st.info(
            "Press Scan all available F&O stocks "
            "to generate the ranking."
        )

    st.divider()

    # ----------------------------------------
    # SECTION 1: OPTION CHAIN
    # ----------------------------------------

    st.header("1. OPTION CHAIN + GREEKS")

    chain = pd.DataFrame()
    ce = []
    pe = []

    try:
        expiries = contracts(token, key)

        if not expiries:
            raise RuntimeError(
                "No currently listed option contracts "
                "for this instrument."
            )

        expiry = (
            expiries[0]
            if expiry_choice == "Auto: nearest available"
            else expiry_manual.strip()
        )

        if expiry not in expiries:
            raise RuntimeError(
                f"Expiry {expiry} unavailable. "
                f"Available: {', '.join(expiries[:8])}"
            )

        identity = f"{key}:{expiry}"

        if identity != st.session_state.identity:
            st.session_state.identity = identity
            st.session_state.prior = {}
            st.session_state.prior_time = None

        chain = option_chain(token, key, expiry)

        if chain.empty:
            raise RuntimeError("Option chain returned no rows")

        st.caption(
            f"Instrument: {name} | Expiry: {expiry}"
        )

        spot = (
            n(chain.spot.dropna().iloc[0])
            if chain.spot.notna().any()
            else np.nan
        )

        ce_oi = chain.loc[
            chain.side == "CE", "oi"
        ].sum()

        pe_oi = chain.loc[
            chain.side == "PE", "oi"
        ].sum()

        a, b, c, d = st.columns(4)

        a.metric(
            "Spot",
            f"{spot:,.2f}" if np.isfinite(spot) else "N/A"
        )

        b.metric(
            "PCR",
            f"{pe_oi / ce_oi:.2f}" if ce_oi > 0 else "N/A"
        )

        c.metric("CE OI", f"{ce_oi:,.0f}")
        d.metric("PE OI", f"{pe_oi:,.0f}")

        prior = (
            st.session_state.prior
            if market_hours else {}
        )

        prior_time = (
            st.session_state.prior_time
            if market_hours else None
        )

        ce = candidate_list(
            chain, "CE", prior, prior_time,
            now, min_vol, max_spread, max_iv, rr
        )

        pe = candidate_list(
            chain, "PE", prior, prior_time,
            now, min_vol, max_spread, max_iv, rr
        )

        left, right = st.columns(2)

        for col, label, candidates in [
            (left, "CALL (CE)", ce),
            (right, "PUT (PE)", pe)
        ]:
            with col:
                st.subheader(label + " Top 3")

                if not candidates:
                    st.info("No near-ATM candidate.")
                    continue

                rows = []

                for item in candidates:
                    rows.append({
                        "Strike": (
                            f"{item['strike']:g} {item['side']}"
                        ),
                        "Score": f"{item['score']}/7",
                        "LTP": item["ltp"],
                        "Delta": item["delta"],
                        "IV": item["iv"],
                        "Spread %": item["spread"],
                        "Entry*": round(item["entry"], 2),
                        "SL*": round(item["sl"], 2),
                        "Target*": round(item["target"], 2)
                    })

                st.dataframe(
                    pd.DataFrame(rows),
                    hide_index=True,
                    use_container_width=True
                )

                best = candidates[0]

                status = (
                    "BUY CANDIDATE"
                    if (
                        market_hours
                        and best["fresh"]
                        and best["premium_up"]
                        and best["score"] >= min_score
                    )
                    else "WAIT"
                )

                st.write(
                    f"**Chain Status:** {status} | "
                    f"**Best:** {best['strike']:g} "
                    f"{best['side']} "
                    f"({best['score']}/7)"
                )

                with st.expander("Why this score?"):
                    for check, passed in best["checks"].items():
                        st.write(
                            ("✅ " if passed else "❌ ") + check
                        )

        with st.expander("Full Option Chain + Greeks"):
            view = chain.drop(columns=["instrument"])

            st.dataframe(
                view,
                hide_index=True,
                use_container_width=True
            )

            st.download_button(
                "Download Option Chain CSV",
                view.to_csv(index=False),
                file_name="option_chain.csv",
                mime="text/csv"
            )

        if market_hours:
            st.session_state.prior = {
                row.instrument: row.ltp
                for _, row in chain.iterrows()
                if row.instrument
                and np.isfinite(row.ltp)
                and row.ltp > 0
            }

            st.session_state.prior_time = now

        else:
            st.session_state.prior = {}
            st.session_state.prior_time = None

    except Exception as exc:
        st.error(f"Option Chain unavailable: {exc}")

    st.caption(
        "*Entry, SL and Target are illustrative. "
        "They are not validated trading levels."
    )

    st.divider()

    # ----------------------------------------
    # SECTION 2: MARKET TREND
    # ----------------------------------------

    st.header("2. MARKET / STOCK TREND")

    trend = None

    if not market_hours:
        st.info("TREND: UNAVAILABLE — market closed")

    else:
        try:
            history, current = candle_data(
                token, key, now
            )

            trend, error = trend_calc(
                history, current, now
            )

            if error:
                st.warning(
                    "TREND: UNAVAILABLE — " + error
                )

            elif trend:
                direction = trend["direction"]

                if direction == "BULLISH":
                    st.success(
                        f"TREND: BULLISH | "
                        f"{trend['confidence']}"
                    )

                elif direction == "BEARISH":
                    st.error(
                        f"TREND: BEARISH | "
                        f"{trend['confidence']}"
                    )

                else:
                    st.info(
                        f"TREND: {direction} | "
                        f"{trend['confidence']}"
                    )

                if "close" in trend:
                    a, b, c, d = st.columns(4)

                    a.metric(
                        "Last 5m Close",
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
                    )

                    st.write(
                        f"ADX: {trend['adx']:.1f}"
                    )

                    if np.isfinite(trend["vwap"]):
                        st.write(
                            f"VWAP: {trend['vwap']:.2f}"
                        )
                    else:
                        st.write("VWAP unavailable")

                    st.write(
                        f"Opening High: "
                        f"{trend['opening_high']:.2f}"
                    )

                    st.write(
                        f"Opening Low: "
                        f"{trend['opening_low']:.2f}"
                    )

                    st.caption(
                        f"Last completed candle: "
                        f"{trend['bar']}"
                    )

        except Exception as exc:
            st.warning(
                f"TREND: UNAVAILABLE — {exc}"
            )

    st.divider()

    # ----------------------------------------
    # SECTION 3: SIGNAL AGREEMENT
    # ----------------------------------------

    st.header("3. SIGNAL AGREEMENT")

    if not ce or not pe:
        st.info("Option Chain comparison unavailable.")

    elif not trend or "close" not in trend:
        st.info(
            "Trend unavailable. "
            "CE/PE candidates remain independent."
        )

    else:
        ce_score = ce[0]["score"]
        pe_score = pe[0]["score"]

        if ce_score > pe_score:
            bias = "CE LEADING"

        elif pe_score > ce_score:
            bias = "PE LEADING"

        else:
            bias = "NO DIRECTIONAL EDGE"

        direction = trend["direction"]

        st.write(
            f"Chain: **{bias}** | "
            f"Trend: **{direction}**"
        )

        if bias == "NO DIRECTIONAL EDGE":
            st.info(
                "Equal CE and PE scores. "
                "No directional preference."
            )

        elif (
            (bias == "CE LEADING" and direction == "BULLISH")
            or
            (bias == "PE LEADING" and direction == "BEARISH")
        ):
            st.success(
                "ALIGNED — both systems agree."
            )

        elif direction == "NEUTRAL":
            st.info(
                "MIXED — neutral market trend."
            )

        else:
            st.warning(
                "CONFLICT — Chain and Trend disagree."
            )

    st.warning(
        "Research-only screening. "
        "Scores are heuristic, not backtested. "
        "No automatic orders. "
        "Exchange holidays are not separately verified."
    )


dashboard()
