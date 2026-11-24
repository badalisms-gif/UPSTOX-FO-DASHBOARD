
import numpy as np
import pandas as pd
import requests
import streamlit as st

from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"


# ==========================================
# HISTORICAL DATA ENGINE
# ==========================================

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history(token, instrument_key, days):

    end = datetime.now(IST).date() - timedelta(days=1)

    test_start = end - timedelta(days=days - 1)

    start = test_start - timedelta(days=15)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    key = quote(instrument_key, safe="")

    candles = []
    cursor = start

    while cursor <= end:

        chunk_end = min(
            cursor + timedelta(days=20),
            end
        )

        url = (
            f"{API}/v3/historical-candle/"
            f"{key}/minutes/1/"
            f"{chunk_end}/{cursor}"
        )

        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        if not response.ok:

            raise RuntimeError(
                f"Upstox HTTP {response.status_code}"
            )

        payload = response.json()

        if payload.get("status") != "success":

            raise RuntimeError(
                "Historical API request failed"
            )

        batch = payload.get(
            "data", {}
        ).get("candles", [])

        candles.extend(batch)

        cursor = chunk_end + timedelta(days=1)

    if not candles:

        return pd.DataFrame()

    df = pd.DataFrame(
        candles,
        columns=[
            "Time",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "OI"
        ]
    )

    df["Time"] = pd.to_datetime(
        df["Time"],
        utc=True,
        errors="coerce"
    ).dt.tz_convert(IST)

    for col in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "OI"
    ]:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna(
        subset=[
            "Time",
            "Open",
            "High",
            "Low",
            "Close"
        ]
    )

    df = df[
        (df["Open"] > 0)
        & (df["Close"] > 0)
        & (df["Low"] > 0)
        & (df["High"] >= df["Low"])
        & (df["High"] >= df["Open"])
        & (df["High"] >= df["Close"])
        & (df["Low"] <= df["Open"])
        & (df["Low"] <= df["Close"])
    ]

    df = (
        df.drop_duplicates("Time")
        .sort_values("Time")
        .set_index("Time")
    )

    return df.between_time(
        "09:15",
        "15:29"
    )


# ==========================================
# CANDLE RESAMPLING
# ==========================================

def make_candles(df, minutes):

    if df.empty:

        return df.copy()

    grouped = df.resample(
        f"{minutes}min",
        origin="start_day",
        offset="15min",
        label="right",
        closed="left"
    )

    bars = grouped.agg({

        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
        "OI": "last"
    })

    count = grouped["Close"].count()

    bars = bars[count == minutes]

    return bars.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close"
        ]
    )


# ==========================================
# TECHNICAL INDICATORS
# MATCHED TO V4 LIVE SCANNER
# ==========================================

def indicators(df):

    df = df.copy()

    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    for period in (9, 21, 50):

        df[f"EMA{period}"] = close.ewm(
            span=period,
            adjust=False
        ).mean()

    change = close.diff()

    gain = change.clip(
        lower=0
    ).ewm(
        alpha=1/9,
        adjust=False
    ).mean()

    loss = (
        -change.clip(upper=0)
    ).ewm(
        alpha=1/9,
        adjust=False
    ).mean()

    rs = gain / loss.replace(
        0,
        np.nan
    )

    df["RSI"] = 100 - 100 / (1 + rs)

    df.loc[
        (loss == 0) & (gain > 0),
        "RSI"
    ] = 100

    df.loc[
        (loss == 0) & (gain == 0),
        "RSI"
    ] = 50

    tr = pd.concat([

        high - low,

        (high - close.shift()).abs(),

        (low - close.shift()).abs()

    ], axis=1).max(axis=1)

    df["ATR"] = tr.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    up = high.diff()
    down = -low.diff()

    plus_dm = pd.Series(

        np.where(
            (up > down) & (up > 0),
            up,
            0
        ),

        index=df.index
    )

    minus_dm = pd.Series(

        np.where(
            (down > up) & (down > 0),
            down,
            0
        ),

        index=df.index
    )

    atr = df["ATR"].replace(
        0,
        np.nan
    )

    df["DI_PLUS"] = (

        100 * plus_dm.ewm(
            alpha=1/14,
            adjust=False
        ).mean() / atr
    )

    df["DI_MINUS"] = (

        100 * minus_dm.ewm(
            alpha=1/14,
            adjust=False
        ).mean() / atr
    )

    di_sum = (

        df["DI_PLUS"]
        + df["DI_MINUS"]

    ).replace(0, np.nan)

    dx = (

        100
        * (
            df["DI_PLUS"]
            - df["DI_MINUS"]
        ).abs()
        / di_sum
    )

    df["ADX"] = dx.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    df["RVOL"] = (

        df["Volume"]

        / df["Volume"]
        .shift(1)
        .rolling(20)
        .mean()
        .replace(0, np.nan)
    )

    typical = (
        high + low + close
    ) / 3

    dates = df.index.date

    cumulative_pv = (

        typical * df["Volume"]

    ).groupby(dates).cumsum()

    cumulative_volume = (

        df["Volume"]
        .groupby(dates)
        .cumsum()
        .replace(0, np.nan)
    )

    df["VWAP"] = (

        cumulative_pv
        / cumulative_volume
    )

    df["PreviousHigh"] = (

        high.shift(1)
        .rolling(5)
        .max()
    )

    df["PreviousLow"] = (

        low.shift(1)
        .rolling(5)
        .min()
    )

    return df


# ==========================================
# HIGHER TIMEFRAME CONFIRMATION
# NO FUTURE CANDLE DATA
# ==========================================

def prepare_data(raw, minutes):

    base = indicators(
        make_candles(raw, minutes)
    )

    htf15 = indicators(
        make_candles(raw, 15)
    )

    htf60 = indicators(
        make_candles(raw, 60)
    )

    if base.empty:

        return base

    for label, higher in [

        ("HTF15", htf15),

        ("HTF60", htf60)

    ]:

        higher = higher.copy()

        higher["Ready"] = (
            np.arange(len(higher)) >= 20
        )

        aligned = higher[
            ["Close", "EMA21", "Ready"]
        ].reindex(
            base.index,
            method="ffill"
        )

        base[f"{label}_Close"] = (
            aligned["Close"]
        )

        base[f"{label}_EMA"] = (
            aligned["EMA21"]
        )

        base[f"{label}_Ready"] = (
            aligned["Ready"]
            .fillna(False)
            .astype(bool)
        )

    return base


# ==========================================
# SIGNAL AND FAKE BREAKOUT PROTECTION
# ==========================================

def check_signal(
    row,
    opening_high,
    opening_low,
    is_index=False
):

    required = [

        "Open",
        "High",
        "Low",
        "Close",
        "ATR",
        "EMA9",
        "EMA21",
        "RSI",
        "ADX",
        "DI_PLUS",
        "DI_MINUS",
        "HTF15_Close",
        "HTF15_EMA",
        "HTF60_Close",
        "HTF60_EMA",
        "PreviousHigh",
        "PreviousLow"
    ]

    if not np.isfinite(
        [row[k] for k in required]
    ).all():

        return None

    if not (
        row["HTF15_Ready"]
        and row["HTF60_Ready"]
    ):

        return None

    atr = row["ATR"]

    if atr <= 0:

        return None

    candle_range = (
        row["High"] - row["Low"]
    )

    if candle_range <= 0:

        return None

    body = abs(
        row["Close"] - row["Open"]
    )

    close_position = (

        (
            row["Close"]
            - row["Low"]
        )

        / candle_range
    )

    if row["ADX"] < 25:

        return None

    if body < 0.35 * atr:

        return None

    if abs(

        row["Close"]
        - row["EMA9"]

    ) > 2 * atr:

        return None

    if not is_index:

        if not np.isfinite([
            row["RVOL"],
            row["VWAP"]
        ]).all():

            return None

        if row["RVOL"] < 1.3:

            return None

    # BULLISH SETUP — V4 RULES

    bull = all([

        row["Close"] > row["Open"],

        row["Close"] > row["EMA9"],

        row["EMA9"] > row["EMA21"],

        row["RSI"] >= 55,

        row["ADX"] >= 25,

        row["DI_PLUS"] > row["DI_MINUS"],

        row["HTF15_Close"]
        > row["HTF15_EMA"],

        row["HTF60_Close"]
        > row["HTF60_EMA"],

        row["Close"]
        > opening_high + 0.05 * atr,

        row["Close"]
        > row["PreviousHigh"] + 0.05 * atr,

        close_position >= 0.70,

        is_index
        or row["Close"] > row["VWAP"]
    ])

    # BEARISH SETUP — V4 RULES

    bear = all([

        row["Close"] < row["Open"],

        row["Close"] < row["EMA9"],

        row["EMA9"] < row["EMA21"],

        row["RSI"] <= 45,

        row["ADX"] >= 25,

        row["DI_MINUS"] > row["DI_PLUS"],

        row["HTF15_Close"]
        < row["HTF15_EMA"],

        row["HTF60_Close"]
        < row["HTF60_EMA"],

        row["Close"]
        < opening_low - 0.05 * atr,

        row["Close"]
        < row["PreviousLow"] - 0.05 * atr,

        close_position <= 0.30,

        is_index
        or row["Close"] < row["VWAP"]
    ])

    if bull:

        return "CE"

    if bear:

        return "PE"

    return None


# ==========================================
# SESSION DATA VALIDATION
# ==========================================

def complete_session(raw_day):

    if len(raw_day) != 375:

        return False

    expected = pd.date_range(

        start=raw_day.index[0].normalize()
        + pd.Timedelta(hours=9, minutes=15),

        periods=375,

        freq="min"
    )

    return raw_day.index.equals(expected)


# ==========================================
# HISTORICAL TRADE SIMULATION
# ==========================================

def run_backtest(

    raw,

    minutes=5,

    capital=100000,

    daily_risk=5000,

    risk_per_trade=1500,

    rr=2.0,

    brokerage=40,

    slippage_pct=0.05,

    max_trades=3,

    is_index=False,

    test_start=None,

    direction_filter="BOTH"

):

    if raw.empty:

        return pd.DataFrame()

    data = prepare_data(
        raw,
        minutes
    )

    if len(data) < 60:

        return pd.DataFrame()

    trades = []

    equity = float(capital)

    dates = sorted(
        set(data.index.date)
    )

    slip = slippage_pct / 100

    for date in dates:

        if (
            test_start is not None
            and date < test_start
        ):

            continue

        if equity <= 0:

            break

        day = data[
            data.index.date == date
        ]

        raw_day = raw[
            raw.index.date == date
        ]

        if not complete_session(raw_day):

            continue

        opening = raw_day.between_time(
            "09:15",
            "09:29"
        )

        if len(opening) != 15:

            continue

        opening_high = opening["High"].max()

        opening_low = opening["Low"].min()

        used_risk = 0.0

        trade_count = 0

        i = 0

        while i < len(day) - 1:

            if equity <= 0:

                break

            if trade_count >= max_trades:

                break

            remaining = (
                daily_risk - used_risk
            )

            if remaining <= brokerage:

                break

            signal_time = day.index[i]

            row = day.iloc[i]

            if (
                signal_time.strftime("%H:%M")
                < "09:30"
            ):

                i += 1

                continue

            if (
                signal_time.strftime("%H:%M")
                > "14:45"
            ):

                break

            side = check_signal(

                row,

                opening_high,

                opening_low,

                is_index
            )

            if side is None:

                i += 1

                continue

            if (
                direction_filter != "BOTH"
                and side != direction_filter
            ):

                i += 1

                continue

            entry_index = i + 1

            if (

                day.index[entry_index]
                - signal_time

                != pd.Timedelta(
                    minutes=minutes
                )
            ):

                i += 1

                continue

            entry_time = signal_time

            entry_raw = day.iloc[
                entry_index
            ]["Open"]

            direction = (

                1 if side == "CE"

                else -1
            )

            entry = entry_raw * (
                1 + direction * slip
            )

            stop_distance = (
                1.5 * row["ATR"]
            )

            if (
                stop_distance <= 0
                or entry <= 0
            ):

                i += 1

                continue

            stop = (

                entry

                - direction * stop_distance
            )

            target = (

                entry

                + direction
                * stop_distance
                * rr
            )

            if stop <= 0 or target <= 0:

                i += 1

                continue

            risk_budget = min(

                remaining,

                risk_per_trade,

                equity
            )

            unit_risk = (

                stop_distance

                + stop * slip
            )

            quantity = int(min(

                max(
                    0,
                    risk_budget - brokerage
                ) / unit_risk,

                equity / entry
            ))

            if quantity < 1:

                i += 1

                continue

            planned_risk = (

                quantity * unit_risk

                + brokerage
            )

            exit_price = None

            exit_time = None

            reason = None

            # Use one-minute candles for
            # more precise SL and target checks.

            future = raw_day[
                raw_day.index >= entry_time
            ].between_time(
                "09:30",
                "15:19"
            )

            for ts, candle in future.iterrows():

                op = candle["Open"]

                high = candle["High"]

                low = candle["Low"]

                if side == "CE":

                    stop_hit = low <= stop

                    target_hit = high >= target

                    if op <= stop:

                        exit_price = op

                        reason = "STOP LOSS"

                    elif op >= target:

                        exit_price = target

                        reason = "TARGET"

                    elif stop_hit:

                        exit_price = stop

                        reason = "STOP LOSS"

                    elif target_hit:

                        exit_price = target

                        reason = "TARGET"

                else:

                    stop_hit = high >= stop

                    target_hit = low <= target

                    if op >= stop:

                        exit_price = op

                        reason = "STOP LOSS"

                    elif op <= target:

                        exit_price = target

                        reason = "TARGET"

                    elif stop_hit:

                        exit_price = stop

                        reason = "STOP LOSS"

                    elif target_hit:

                        exit_price = target

                        reason = "TARGET"

                if exit_price is not None:

                    exit_time = (
                        ts + pd.Timedelta(minutes=1)
                    )

                    break

                if (
                    ts.strftime("%H:%M")
                    >= "15:19"
                ):

                    exit_price = candle["Close"]

                    exit_time = (
                        ts + pd.Timedelta(minutes=1)
                    )

                    reason = "SESSION EXIT"

                    break

            if exit_price is None:

                i += 1

                continue

            exit_price *= (

                1 - direction * slip
            )

            points = (

                exit_price - entry

            ) * direction

            gross = points * quantity

            net = gross - brokerage

            equity += net

            used_risk += max(

                planned_risk,

                -net
            )

            trade_count += 1

            trades.append({

                "Date": str(date),

                "Direction": side,

                "Entry Time": str(entry_time),

                "Exit Candle End": str(exit_time),

                "Entry": round(entry, 2),

                "Stop Loss": round(stop, 2),

                "Target": round(target, 2),

                "Exit": round(exit_price, 2),

                "Spot Units": quantity,

                "Points": round(points, 2),

                "Planned Risk": round(
                    planned_risk,
                    2
                ),

                "Gross P&L": round(gross, 2),

                "Estimated Costs": brokerage,

                "Net P&L": round(net, 2),

                "Exit Reason": reason,

                "Equity": round(equity, 2),

                "RSI": round(row["RSI"], 2),

                "ADX": round(row["ADX"], 2),

                "ATR": round(row["ATR"], 2),

                "RVOL": round(row["RVOL"], 2),

                "EMA9": round(row["EMA9"], 2),

                "EMA21": round(row["EMA21"], 2),

                "EMA50": round(row["EMA50"], 2),

                "VWAP": round(row["VWAP"], 2),

                "Opening High": round(
                    opening_high,
                    2
                ),

                "Opening Low": round(
                    opening_low,
                    2
                )
            })

            # No overlapping positions.
            # Resume after the exit candle.

            i = max(

                i + 1,

                int(
                    day.index.searchsorted(
                        exit_time,
                        side="right"
                    )
                )
            )

    return pd.DataFrame(trades)


# ==========================================
# PERFORMANCE REPORT
# ==========================================

def performance(trades, capital):

    if trades.empty:

        return {}

    pnl = trades["Net P&L"]

    wins = pnl[pnl > 0]

    losses = pnl[pnl < 0]

    gross_profit = wins.sum()

    gross_loss = abs(losses.sum())

    equity = pd.Series(

        [capital]

        + trades["Equity"].tolist()
    )

    peak = equity.cummax()

    drawdown = peak - equity

    drawdown_pct = (

        drawdown

        / peak.replace(0, np.nan)

    ) * 100

    ce = trades[
        trades["Direction"] == "CE"
    ]

    pe = trades[
        trades["Direction"] == "PE"
    ]

    return {

        "Total Trades": len(trades),

        "CE Signals": len(ce),

        "PE Signals": len(pe),

        "Winning Trades": len(wins),

        "Losing Trades": len(losses),

        "Breakeven Trades": int(
            (pnl == 0).sum()
        ),

        "Win Rate": round(

            100 * len(wins) / len(trades),

            2
        ),

        "Loss Rate": round(

            100 * len(losses) / len(trades),

            2
        ),

        "Target Hits": int(

            (
                trades["Exit Reason"]
                == "TARGET"
            ).sum()
        ),

        "SL Hits": int(

            (
                trades["Exit Reason"]
                == "STOP LOSS"
            ).sum()
        ),

        "Session Exits": int(

            (
                trades["Exit Reason"]
                == "SESSION EXIT"
            ).sum()
        ),

        "Gross P&L": round(

            trades["Gross P&L"].sum(),

            2
        ),

        "Estimated Costs": round(

            trades["Estimated Costs"].sum(),

            2
        ),

        "Net P&L": round(pnl.sum(), 2),

        "CE Net P&L": round(

            ce["Net P&L"].sum(),

            2
        ),

        "PE Net P&L": round(

            pe["Net P&L"].sum(),

            2
        ),

        "Average Trade": round(

            pnl.mean(),

            2
        ),

        "Average Win": round(

            wins.mean(),

            2

        ) if len(wins) else 0,

        "Average Loss": round(

            losses.mean(),

            2

        ) if len(losses) else 0,

        "Best Trade": round(

            pnl.max(),

            2
        ),

        "Worst Trade": round(

            pnl.min(),

            2
        ),

        "Profit Factor": round(

            gross_profit / gross_loss,

            2

        ) if gross_loss > 0 else None,

        "Maximum Drawdown": round(

            drawdown.max(),

            2
        ),

        "Drawdown %": round(

            drawdown_pct.max(),

            2
        ),

        "Return %": round(

            100 * pnl.sum() / capital,

            2
        ),

        "Final Equity": round(

            capital + pnl.sum(),

            2
        )
    }


# ==========================================
# STREAMLIT BACKTESTING DASHBOARD
# ==========================================

def render_backtesting(token, universe):

    st.header(
        "Historical Backtesting V5"
    )

    st.warning(

        "LEVEL 1: Technical spot-price simulation. "

        "CE and PE represent bullish and bearish "

        "directions only. These are NOT actual "

        "option premium profits. Historical "

        "news, option OI, Greeks, bid/ask and "

        "full master scoring are not tested."
    )

    if not universe:

        st.error(
            "Instrument list unavailable."
        )

        return

    symbols = sorted(
        universe.keys()
    )

    default_index = (

        symbols.index("NIFTY")

        if "NIFTY" in symbols

        else 0
    )

    symbol = st.selectbox(

        "Select Instrument",

        symbols,

        index=default_index,

        key="bt_symbol"
    )

    days = st.selectbox(

        "Historical Calendar Days",

        [7, 15, 30, 60, 90],

        index=2,

        key="bt_days"
    )

    minutes = st.selectbox(

        "Timeframe",

        [5, 15],

        key="bt_minutes"
    )

    if minutes == 15:

        st.info(

            "15-minute testing is an alternative "

            "strategy variant. V4 live scanner "

            "uses 5-minute entries."
        )

    direction_filter = st.selectbox(

        "Trade Direction",

        ["BOTH", "CE", "PE"],

        key="bt_direction"
    )

    st.subheader(
        "Backtesting Risk Management"
    )

    capital = st.number_input(

        "Backtest Capital",

        min_value=1000,

        value=100000,

        step=10000,

        key="bt_capital"
    )

    daily_risk = st.number_input(

        "Daily Risk Budget",

        min_value=100,

        value=5000,

        step=500,

        key="bt_daily_risk"
    )

    risk_per_trade = st.number_input(

        "Risk Per Trade",

        min_value=100,

        value=1500,

        step=100,

        key="bt_trade_risk"
    )

    rr = st.slider(

        "Reward / Risk",

        1.0,

        4.0,

        2.0,

        0.25,

        key="bt_rr"
    )

    brokerage = st.number_input(

        "Estimated Round Trip Costs",

        min_value=0.0,

        value=40.0,

        key="bt_cost"
    )

    slippage = st.number_input(

        "Slippage Per Side %",

        min_value=0.0,

        max_value=5.0,

        value=0.05,

        step=0.01,

        key="bt_slippage"
    )

    max_trades = st.slider(

        "Maximum Trades Per Day",

        1,

        10,

        3,

        key="bt_max_trades"
    )

    settings = (

        symbol,

        days,

        minutes,

        direction_filter,

        capital,

        daily_risk,

        risk_per_trade,

        rr,

        brokerage,

        slippage,

        max_trades
    )

    if st.button(

        "RUN HISTORICAL BACKTEST",

        type="primary",

        key="bt_run"
    ):

        st.session_state.pop(

            "bt_result",

            None
        )

        if not token:

            st.error(
                "Upstox token required."
            )

            return

        try:

            with st.spinner(

                "Downloading historical candles "

                "and testing strategy..."
            ):

                instrument_key = universe[symbol]

                end = (

                    datetime.now(IST).date()

                    - timedelta(days=1)
                )

                test_start = (

                    end

                    - timedelta(days=days - 1)
                )

                raw = fetch_history(

                    token,

                    instrument_key,

                    days
                )

                if raw.empty:

                    st.warning(

                        "Historical data unavailable."
                    )

                    return

                test_raw = raw[

                    raw.index.date >= test_start
                ]

                if test_raw.empty:

                    st.warning(

                        "No data in requested period."
                    )

                    return

                session_dates = sorted(

                    set(test_raw.index.date)
                )

                complete_days = sum(

                    complete_session(

                        test_raw[
                            test_raw.index.date == d
                        ]
                    )

                    for d in session_dates
                )

                incomplete_days = (

                    len(session_dates)

                    - complete_days
                )

                trades = run_backtest(

                    raw=raw,

                    minutes=minutes,

                    capital=capital,

                    daily_risk=daily_risk,

                    risk_per_trade=risk_per_trade,

                    rr=rr,

                    brokerage=brokerage,

                    slippage_pct=slippage,

                    max_trades=max_trades,

                    is_index=instrument_key.startswith(

                        "NSE_INDEX"
                    ),

                    test_start=test_start,

                    direction_filter=direction_filter
                )

                st.session_state["bt_result"] = {

                    "symbol": symbol,

                    "days": days,

                    "minutes": minutes,

                    "direction": direction_filter,

                    "capital": capital,

                    "settings": settings,

                    "candles": len(test_raw),

                    "sessions": len(session_dates),

                    "complete_days": complete_days,

                    "incomplete_days": incomplete_days,

                    "start": str(
                        test_raw.index.min()
                    ),

                    "end": str(
                        test_raw.index.max()
                    ),

                    "trades": trades,

                    "metrics": performance(

                        trades,

                        capital
                    )
                }

        except Exception as exc:

            st.error(

                f"Backtest failed: {exc}"
            )

    result = st.session_state.get(

        "bt_result"
    )

    if result is None:

        return

    if result["settings"] != settings:

        st.info(

            "Settings changed. Run the backtest "

            "again to update results."
        )

        return

    st.divider()

    st.subheader(

        "Historical Test Results"
    )

    st.caption(

        f"{result['symbol']} | "

        f"{result['days']} calendar days | "

        f"{result['minutes']} minute timeframe | "

        f"{result['direction']}"
    )

    a, b = st.columns(2)

    a.metric(

        "Historical Candles",

        result["candles"]
    )

    b.metric(

        "Sessions Received",

        result["sessions"]
    )

    c, d = st.columns(2)

    c.metric(

        "Complete Sessions",

        result["complete_days"]
    )

    d.metric(

        "Incomplete Sessions Skipped",

        result["incomplete_days"]
    )

    st.caption(

        f"Data: {result['start']} "

        f"to {result['end']}"
    )

    st.caption(

        "An additional 15 calendar days are "

        "requested for indicator warm-up. "

        "Only the selected test period is scored. "

        "Entirely missing sessions cannot be "

        "identified without an exchange calendar."
    )

    trades = result["trades"]

    metrics = result["metrics"]

    if trades.empty:

        st.info(

            "No qualifying simulated trades "

            "in the available complete sessions. "

            "This may also reflect insufficient "

            "indicator history or capital."
        )

        return

    col1, col2 = st.columns(2)

    col1.metric(

        "Total Trades",

        metrics["Total Trades"]
    )

    col2.metric(

        "Win Rate",

        f"{metrics['Win Rate']}%"
    )

    col3, col4 = st.columns(2)

    col3.metric(

        "Simulated Net P&L",

        f"₹{metrics['Net P&L']:,.2f}"
    )

    col4.metric(

        "Maximum Drawdown",

        f"₹{metrics['Maximum Drawdown']:,.2f}"
    )

    col5, col6 = st.columns(2)

    col5.metric(

        "Target Hits",

        metrics["Target Hits"]
    )

    col6.metric(

        "Stop Loss Hits",

        metrics["SL Hits"]
    )

    col7, col8 = st.columns(2)

    col7.metric(

        "CE Signals",

        metrics["CE Signals"]
    )

    col8.metric(

        "PE Signals",

        metrics["PE Signals"]
    )

    col9, col10 = st.columns(2)

    pf = metrics["Profit Factor"]

    col9.metric(

        "Profit Factor",

        pf if pf is not None else "N/A"
    )

    col10.metric(

        "Return",

        f"{metrics['Return %']}%"
    )

    st.subheader(

        "Complete Performance Report"
    )

    st.dataframe(

        pd.DataFrame(

            list(metrics.items()),

            columns=[
                "Metric",
                "Result"
            ]

        ).astype(str),

        hide_index=True,

        use_container_width=True
    )

    st.subheader(

        "Simulated Equity Curve"
    )

    equity = pd.DataFrame({

        "Equity": [

            result["capital"]

        ] + trades["Equity"].tolist()
    })

    st.line_chart(equity)

    st.subheader(

        "Daily Simulated P&L"
    )

    daily_pnl = trades.groupby(

        "Date"

    )["Net P&L"].sum()

    st.bar_chart(daily_pnl)

    st.subheader(

        "Drawdown Analysis"
    )

    equity_series = equity["Equity"]

    drawdown = (

        equity_series

        - equity_series.cummax()
    )

    st.line_chart(

        pd.DataFrame({

            "Drawdown": drawdown
        })
    )

    st.subheader(

        "CE vs PE Performance"
    )

    direction_report = trades.groupby(

        "Direction"

    ).agg(

        Trades=("Net P&L", "size"),

        Net_PnL=("Net P&L", "sum"),

        Average_PnL=("Net P&L", "mean"),

        Win_Rate=(

            "Net P&L",

            lambda x: 100 * (x > 0).mean()
        )
    ).round(2)

    st.dataframe(

        direction_report,

        use_container_width=True
    )

    st.subheader(

        "Historical Trade History"
    )

    st.dataframe(

        trades,

        use_container_width=True,

        hide_index=True
    )

    st.download_button(

        "DOWNLOAD BACKTEST CSV",

        trades.to_csv(index=False),

        file_name=(

            f"{result['symbol']}_"

            "technical_backtest_v5.csv"
        ),

        mime="text/csv",

        key="bt_download_trades"
    )

    report = pd.DataFrame(

        list(metrics.items()),

        columns=[
            "Metric",
            "Result"
        ]
    )

    st.download_button(

        "DOWNLOAD PERFORMANCE REPORT",

        report.to_csv(index=False),

        file_name=(

            f"{result['symbol']}_"

            "performance_v5.csv"
        ),

        mime="text/csv",

        key="bt_download_report"
    )

    st.caption(

        "Completed candles and next-candle "

        "entries are used. Stop and target "

        "checks use one-minute OHLC data. "

        "When both are touched within the "

        "same minute and order is unknown, "

        "stop takes priority. "

        "Costs are estimates. Drawdown uses "

        "closed trades only. Capital and "

        "quantity represent hypothetical spot "

        "exposure, not option lots. "

        "Price gaps can exceed planned risk. "

        "Past performance does not guarantee "

        "future results."
    )
