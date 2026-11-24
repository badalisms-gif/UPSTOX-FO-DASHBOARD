
import numpy as np
import pandas as pd
import requests
import streamlit as st

from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"

STRATEGIES = [
    "ORB 15 Minutes",
    "ORB 30 Minutes",
    "Breakout + Retest",
    "ORB + Market Structure",
    "Retest + Market Structure"
]


# ==================================================
# 1. HISTORICAL DATA
# ==================================================

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_history_v7(token, instrument_key, days):

    end = datetime.now(IST).date() - timedelta(days=1)
    start = end - timedelta(days=days + 30)
    cursor = start
    records = []

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    key = quote(instrument_key, safe="")

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

        response.raise_for_status()

        payload = response.json()

        if payload.get("status") != "success":
            raise RuntimeError(str(payload))

        candles = payload.get(
            "data", {}
        ).get("candles", [])

        records.extend(candles)

        cursor = chunk_end + timedelta(days=1)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(
        [x[:7] for x in records],
        columns=[
            "Time", "Open", "High", "Low",
            "Close", "Volume", "OI"
        ]
    )

    df["Time"] = pd.to_datetime(
        df["Time"],
        utc=True
    ).dt.tz_convert(IST)

    for c in [
        "Open", "High", "Low",
        "Close", "Volume", "OI"
    ]:
        df[c] = pd.to_numeric(
            df[c], errors="coerce"
        )

    df = (
        df.drop_duplicates("Time")
        .set_index("Time")
        .sort_index()
        .between_time("09:15", "15:29")
    )

    df = df.dropna(
        subset=["Open", "High", "Low", "Close"]
    )

    df = df[
        (df["High"] >= df[["Open", "Close"]].max(axis=1))
        & (df["Low"] <= df[["Open", "Close"]].min(axis=1))
        & (df["Low"] > 0)
    ]

    return df


# ==================================================
# 2. DATA QUALITY
# ==================================================

def validate_sessions(df):

    good = []
    rejected = []

    for day, part in df.groupby(df.index.date):

        expected = pd.date_range(
            start=pd.Timestamp(
                f"{day} 09:15", tz=IST
            ),
            periods=375,
            freq="1min"
        )

        if part.index.equals(expected):
            good.append(part)

        else:
            rejected.append({
                "Date": str(day),
                "Candles": len(part),
                "Status": "Incomplete session"
            })

    clean = (
        pd.concat(good).sort_index()
        if good else pd.DataFrame()
    )

    return clean, pd.DataFrame(rejected)


# ==================================================
# 3. CANDLE GENERATION
# ==================================================

def make_bars(df, minutes):

    rule = f"{minutes}min"

    result = df.resample(
        rule,
        origin="start_day",
        offset="15min",
        label="right",
        closed="left"
    ).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
        "OI": "last"
    })

    count = df["Close"].resample(
        rule,
        origin="start_day",
        offset="15min",
        label="right",
        closed="left"
    ).count()

    result = result[count == minutes]

    return result.dropna(
        subset=["Open", "High", "Low", "Close"]
    )


# ==================================================
# 4. TECHNICAL INDICATORS
# ==================================================

def rma(series, length):

    return series.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length
    ).mean()


def indicators_v7(df):

    d = df.copy()

    close = d["Close"]
    high = d["High"]
    low = d["Low"]

    d["EMA9"] = close.ewm(
        span=9, adjust=False
    ).mean()

    d["EMA21"] = close.ewm(
        span=21, adjust=False
    ).mean()

    d["EMA50"] = close.ewm(
        span=50, adjust=False
    ).mean()

    delta = close.diff()

    gain = rma(delta.clip(lower=0), 9)
    loss = rma(-delta.clip(upper=0), 9)

    rs = gain / loss.replace(0, np.nan)

    d["RSI"] = 100 - 100 / (1 + rs)

    d.loc[
        (loss == 0) & (gain > 0), "RSI"
    ] = 100

    d.loc[
        (loss == 0) & (gain == 0), "RSI"
    ] = 50

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    d["ATR"] = rma(tr, 14)

    up = high.diff()
    down = -low.diff()

    plus_dm = pd.Series(
        np.where(
            (up > down) & (up > 0),
            up, 0
        ),
        index=d.index
    )

    minus_dm = pd.Series(
        np.where(
            (down > up) & (down > 0),
            down, 0
        ),
        index=d.index
    )

    atr = d["ATR"].replace(0, np.nan)

    d["DI_PLUS"] = 100 * rma(
        plus_dm, 14
    ) / atr

    d["DI_MINUS"] = 100 * rma(
        minus_dm, 14
    ) / atr

    di_sum = (
        d["DI_PLUS"] + d["DI_MINUS"]
    ).replace(0, np.nan)

    dx = (
        100
        * (d["DI_PLUS"] - d["DI_MINUS"]).abs()
        / di_sum
    )

    d["ADX"] = rma(dx, 14)

    candle_range = (
        high - low
    ).replace(0, np.nan)

    d["BODY_ATR"] = (
        close - d["Open"]
    ).abs() / atr

    d["CLOSE_POSITION"] = (
        close - low
    ) / candle_range

    d["UPPER_WICK"] = (
        high - d[["Open", "Close"]].max(axis=1)
    ) / candle_range

    d["LOWER_WICK"] = (
        d[["Open", "Close"]].min(axis=1) - low
    ) / candle_range

    d["EXTENSION"] = (
        close - d["EMA9"]
    ).abs() / atr

    d["EMA_SLOPE"] = (
        d["EMA21"] - d["EMA21"].shift(3)
    ) / atr

    d["EMA_SEPARATION"] = (
        d["EMA9"] - d["EMA21"]
    ).abs() / atr

    d["RVOL"] = d["Volume"] / (
        d["Volume"].shift(1)
        .rolling(20).mean()
        .replace(0, np.nan)
    )

    daily = d.groupby(d.index.date)

    d["PREV_HIGH"] = daily["High"].transform(
        lambda x: x.shift(1).rolling(5).max()
    )

    d["PREV_LOW"] = daily["Low"].transform(
        lambda x: x.shift(1).rolling(5).min()
    )

    d["SWING_HIGH"] = daily["High"].transform(
        lambda x: x.rolling(5).max()
    )

    d["SWING_LOW"] = daily["Low"].transform(
        lambda x: x.rolling(5).min()
    )

    return d


# ==================================================
# 5. MARKET REGIME
# ==================================================

def prepare_v7(raw, timeframe):

    d = indicators_v7(
        make_bars(raw, timeframe)
    )

    htf = indicators_v7(
        make_bars(raw, 15)
    )

    d["HTF_CLOSE"] = htf["Close"].reindex(
        d.index, method="ffill"
    )

    d["HTF_EMA21"] = htf["EMA21"].reindex(
        d.index, method="ffill"
    )

    d["HTF_ADX"] = htf["ADX"].reindex(
        d.index, method="ffill"
    )

    bull = (
        (d["ADX"] >= 22)
        & (d["EMA9"] > d["EMA21"])
        & (d["EMA_SLOPE"] > 0.10)
        & (d["EMA_SEPARATION"] >= 0.15)
        & (d["HTF_CLOSE"] > d["HTF_EMA21"])
    )

    bear = (
        (d["ADX"] >= 22)
        & (d["EMA9"] < d["EMA21"])
        & (d["EMA_SLOPE"] < -0.10)
        & (d["EMA_SEPARATION"] >= 0.15)
        & (d["HTF_CLOSE"] < d["HTF_EMA21"])
    )

    d["REGIME"] = np.select(
        [bull, bear],
        ["BULLISH TREND", "BEARISH TREND"],
        default="SIDEWAYS"
    )

    return d


# ==================================================
# 6. FAKE BREAKOUT PROTECTION
# ==================================================

def quality_checks(
    row,
    side,
    is_index,
    adx_min,
    regime_filter,
    strict_filter
):

    required = [
        "ATR", "RSI", "ADX",
        "DI_PLUS", "DI_MINUS",
        "BODY_ATR", "EXTENSION",
        "CLOSE_POSITION",
        "UPPER_WICK", "LOWER_WICK"
    ]

    if not all(
        np.isfinite(row[x])
        for x in required
    ):
        return False

    if row["ATR"] <= 0:
        return False

    if row["ADX"] < adx_min:
        return False

    if row["BODY_ATR"] < 0.25:
        return False

    if row["EXTENSION"] > 2.0:
        return False

    if strict_filter:

        if row["BODY_ATR"] < 0.40:
            return False

        if row["EXTENSION"] > 1.50:
            return False

        if abs(
            row["DI_PLUS"] - row["DI_MINUS"]
        ) < 5:
            return False

    if not is_index:

        if not np.isfinite(row["RVOL"]):
            return False

        if row["RVOL"] < 1.10:
            return False

    if side == "CE":

        checks = [
            row["Close"] > row["Open"],
            row["Close"] > row["EMA9"],
            row["EMA9"] > row["EMA21"],
            row["HTF_CLOSE"] > row["HTF_EMA21"],
            row["DI_PLUS"] > row["DI_MINUS"],
            52 <= row["RSI"] <= 78,
            row["CLOSE_POSITION"] >= 0.65,
            row["UPPER_WICK"] <= 0.30
        ]

        if regime_filter:
            checks.append(
                row["REGIME"] == "BULLISH TREND"
            )

    else:

        checks = [
            row["Close"] < row["Open"],
            row["Close"] < row["EMA9"],
            row["EMA9"] < row["EMA21"],
            row["HTF_CLOSE"] < row["HTF_EMA21"],
            row["DI_MINUS"] > row["DI_PLUS"],
            22 <= row["RSI"] <= 48,
            row["CLOSE_POSITION"] <= 0.35,
            row["LOWER_WICK"] <= 0.30
        ]

        if regime_filter:
            checks.append(
                row["REGIME"] == "BEARISH TREND"
            )

    return all(checks)


# ==================================================
# 7. OPENING RANGE
# ==================================================

def opening_range(raw_day, minutes):

    start = raw_day.index[0]

    end = start + pd.Timedelta(
        minutes=minutes
    )

    part = raw_day[
        raw_day.index < end
    ]

    if len(part) != minutes:
        return None

    return {
        "High": float(part["High"].max()),
        "Low": float(part["Low"].min()),
        "Ready": end
    }


# ==================================================
# 8. STRATEGY SIGNAL GENERATOR
# ==================================================

def strategy_signals(
    day_bars,
    raw_day,
    strategy,
    config,
    is_index
):

    signals = []

    range_minutes = (
        30 if strategy == "ORB 30 Minutes"
        else 15
    )

    opening = opening_range(
        raw_day, range_minutes
    )

    if opening is None:
        return signals

    retest_mode = "Retest" in strategy
    structure_mode = "Market Structure" in strategy

    pending = {"CE": None, "PE": None}

    for i in range(1, len(day_bars)):

        row = day_bars.iloc[i]
        prev = day_bars.iloc[i - 1]
        timestamp = day_bars.index[i]

        if timestamp <= opening["Ready"]:
            continue

        if timestamp.strftime("%H:%M") > "14:30":
            break

        atr = row["ATR"]

        if not np.isfinite(atr) or atr <= 0:
            continue

        buffer = 0.05 * atr

        upper = opening["High"]
        lower = opening["Low"]

        if structure_mode:

            if not np.isfinite(row["PREV_HIGH"]):
                continue

            if not np.isfinite(row["PREV_LOW"]):
                continue

            upper = max(
                upper, row["PREV_HIGH"]
            )

            lower = min(
                lower, row["PREV_LOW"]
            )

        for side in ["CE", "PE"]:

            if config["direction"] not in [
                "BOTH", side
            ]:
                continue

            level = upper if side == "CE" else lower

            if side == "CE":

                breakout = (
                    prev["Close"] <= level + buffer
                    and row["Close"] > level + buffer
                )

            else:

                breakout = (
                    prev["Close"] >= level - buffer
                    and row["Close"] < level - buffer
                )

            quality = quality_checks(
                row,
                side,
                is_index,
                config["adx_min"],
                config["regime_filter"],
                config["strict_filter"]
            )

            if not retest_mode:

                if breakout and quality:
                    signals.append(
                        (timestamp, side, row, level)
                    )

                continue

            p = pending[side]

            if p is not None:

                age = i - p["Index"]
                saved_level = p["Level"]

                if side == "CE":

                    invalid = (
                        row["Close"]
                        < saved_level - 0.20 * atr
                    )

                    retest = (
                        row["Low"]
                        <= saved_level + 0.25 * atr
                        and row["Close"]
                        > saved_level + buffer
                    )

                else:

                    invalid = (
                        row["Close"]
                        > saved_level + 0.20 * atr
                    )

                    retest = (
                        row["High"]
                        >= saved_level - 0.25 * atr
                        and row["Close"]
                        < saved_level - buffer
                    )

                if invalid or age > 5:

                    pending[side] = None

                elif age >= 1 and retest and quality:

                    signals.append(
                        (
                            timestamp,
                            side,
                            row,
                            saved_level
                        )
                    )

                    pending[side] = None

                    continue

            if breakout and quality:

                pending[side] = {
                    "Index": i,
                    "Level": level
                }

    return signals


# ==================================================
# 9. STOP LOSS CALCULATION
# ==================================================

def calculate_stop(entry, side, row, config):

    atr = float(row["ATR"])

    distance = config["atr_mult"] * atr

    if config["stop_method"] == "Swing + ATR":

        if side == "CE":

            swing = row["SWING_LOW"]

            if np.isfinite(swing):
                distance = max(
                    distance,
                    entry - swing + 0.10 * atr
                )

        else:

            swing = row["SWING_HIGH"]

            if np.isfinite(swing):
                distance = max(
                    distance,
                    swing - entry + 0.10 * atr
                )

    if side == "CE":
        return entry - distance

    return entry + distance


# ==================================================
# 10. MINUTE LEVEL TRADE SIMULATION
# ==================================================

def simulate_trade(
    raw_day,
    timestamp,
    side,
    row,
    level,
    equity,
    risk_budget,
    config
):

    future = raw_day[
        raw_day.index >= timestamp
    ]

    future = future.between_time(
        "09:30", "15:14"
    )

    if future.empty:
        return None

    if future.index[0] != timestamp:
        return None

    sign = 1 if side == "CE" else -1

    slip = config["slippage"] / 100

    market_entry = float(
        future.iloc[0]["Open"]
    )

    entry = market_entry * (
        1 + sign * slip
    )

    if abs(
        entry - row["Close"]
    ) > 0.75 * row["ATR"]:
        return None

    stop = calculate_stop(
        entry, side, row, config
    )

    risk_points = abs(entry - stop)

    if risk_points <= 0 or stop <= 0:
        return None

    if risk_points > 3.5 * row["ATR"]:
        return None

    cost = config["cost"]

    stop_with_slip = stop * (
        1 - sign * slip
    )

    risk_per_unit = abs(
        entry - stop_with_slip
    )

    qty_risk = int(
        max(0, risk_budget - cost)
        / risk_per_unit
    )

    qty_capital = int(
        max(0, equity - cost) / entry
    )

    qty = min(qty_risk, qty_capital)

    if qty < 1:
        return None

    target1 = entry + sign * risk_points

    target2 = (
        entry
        + sign * risk_points * config["rr"]
    )

    if target2 <= 0:
        return None

    current_stop = stop
    target1_hit = False

    mfe = 0.0
    mae = 0.0

    exit_price = None
    exit_time = None
    exit_reason = None

    for t, candle in future.iterrows():

        o = float(candle["Open"])
        h = float(candle["High"])
        l = float(candle["Low"])
        c = float(candle["Close"])

        if side == "CE":

            stop_hit = l <= current_stop
            target_hit = h >= target2

            stop_fill = min(
                o, current_stop
            )

        else:

            stop_hit = h >= current_stop
            target_hit = l <= target2

            stop_fill = max(
                o, current_stop
            )

        # Conservative: stop gets priority
        # if stop and target touch same minute.

        if stop_hit:

            exit_price = stop_fill

            exit_reason = (
                "TRAILING STOP"
                if abs(current_stop - stop) > 0.0001
                else "STOP LOSS"
            )

        elif target_hit:

            exit_price = target2
            exit_reason = "TARGET 2"
            target1_hit = True

        if exit_price is not None:

            # Do not use post-exit candle extremes.
            observed = sign * (
                exit_price - entry
            )

            mfe = max(mfe, observed)
            mae = max(mae, -observed)

            exit_time = t + pd.Timedelta(
                minutes=1
            )

            break

        if side == "CE":

            mfe = max(mfe, h - entry)
            mae = max(mae, entry - l)

            if h >= target1:
                target1_hit = True

        else:

            mfe = max(mfe, entry - l)
            mae = max(mae, h - entry)

            if l <= target1:
                target1_hit = True

        # Trail becomes effective next minute.
        # No same-candle look-ahead.

        if config["trailing"] and target1_hit:

            breakeven = (
                entry + cost / qty
                if side == "CE"
                else entry - cost / qty
            )

            breakeven /= (
                1 - sign * slip
            )

            trail_distance = (
                config["trail_atr"] * row["ATR"]
            )

            if side == "CE":

                proposed = max(
                    breakeven,
                    c - trail_distance
                )

                current_stop = max(
                    current_stop,
                    proposed
                )

            else:

                proposed = min(
                    breakeven,
                    c + trail_distance
                )

                current_stop = min(
                    current_stop,
                    proposed
                )

    if exit_price is None:

        exit_price = float(
            future.iloc[-1]["Close"]
        )

        exit_time = (
            future.index[-1]
            + pd.Timedelta(minutes=1)
        )

        exit_reason = "SESSION EXIT"

    exit_price *= (
        1 - sign * slip
    )

    points = sign * (
        exit_price - entry
    )

    gross = points * qty
    net = gross - cost

    planned_risk = (
        risk_per_unit * qty + cost
    )

    return {
        "Date": str(timestamp.date()),
        "Direction": side,
        "Entry Time": timestamp,
        "Exit Time": exit_time,
        "Entry": round(entry, 2),
        "Stop Loss": round(stop, 2),
        "Target 1": round(target1, 2),
        "Target 2": round(target2, 2),
        "Final Trailing SL": round(current_stop, 2),
        "Target 1 Touched": target1_hit,
        "Exit": round(exit_price, 2),
        "Spot Units": qty,
        "Points": round(points, 2),
        "Planned Risk": round(planned_risk, 2),
        "Gross P&L": round(gross, 2),
        "Costs": round(cost, 2),
        "Net P&L": round(net, 2),
        "Exit Reason": exit_reason,
        "Holding Minutes": int(
            (exit_time - timestamp)
            .total_seconds() / 60
        ),
        "MFE Points": round(mfe, 2),
        "MAE Points": round(mae, 2),
        "MFE R": round(mfe / risk_points, 2),
        "MAE R": round(mae / risk_points, 2),
        "RSI": round(row["RSI"], 2),
        "ADX": round(row["ADX"], 2),
        "ATR": round(row["ATR"], 2),
        "DI Plus": round(row["DI_PLUS"], 2),
        "DI Minus": round(row["DI_MINUS"], 2),
        "Body ATR": round(row["BODY_ATR"], 2),
        "Extension ATR": round(row["EXTENSION"], 2),
        "Market Regime": row["REGIME"],
        "Breakout Level": round(level, 2)
    }


# ==================================================
# 11. BACKTEST ENGINE
# ==================================================

def run_strategy(
    raw,
    prepared,
    strategy,
    config,
    test_dates,
    is_index
):

    trades = []

    equity = float(config["capital"])

    date_set = set(test_dates)

    for day, raw_day in raw.groupby(
        raw.index.date
    ):

        if day not in date_set:
            continue

        day_bars = prepared[
            prepared.index.date == day
        ]

        if len(day_bars) < 10:
            continue

        signals = strategy_signals(
            day_bars,
            raw_day,
            strategy,
            config,
            is_index
        )

        used_risk = 0.0
        trade_count = 0
        next_entry = raw_day.index[0]

        for timestamp, side, row, level in signals:

            if timestamp < next_entry:
                continue

            if trade_count >= config["max_trades"]:
                break

            available = min(
                config["risk_per_trade"],
                config["daily_risk"] - used_risk
            )

            if available <= config["cost"]:
                break

            trade = simulate_trade(
                raw_day,
                timestamp,
                side,
                row,
                level,
                equity,
                available,
                config
            )

            if trade is None:
                continue

            equity += trade["Net P&L"]

            used_risk += max(
                trade["Planned Risk"],
                -trade["Net P&L"],
                0
            )

            trade_count += 1

            trade["Strategy"] = strategy
            trade["Equity"] = round(equity, 2)

            trades.append(trade)

            next_entry = (
                trade["Exit Time"]
                + pd.Timedelta(
                    minutes=(
                        config["cooldown"]
                        * config["timeframe"]
                    )
                )
            )

            if equity <= 0:
                break

        if equity <= 0:
            break

    return pd.DataFrame(trades)


# ==================================================
# 12. PERFORMANCE ANALYSIS
# ==================================================

def performance_v7(trades, capital):

    if trades.empty:

        return {
            "Trades": 0,
            "Wins": 0,
            "Losses": 0,
            "Win Rate %": 0,
            "Net P&L": 0,
            "Profit Factor": np.nan,
            "Maximum Drawdown": 0,
            "Drawdown %": 0,
            "Return %": 0,
            "Average Trade": 0,
            "Average Win": 0,
            "Average Loss": 0,
            "Target 1 Hits": 0,
            "Target 2 Hits": 0,
            "Trailing Exits": 0,
            "Costs": 0
        }

    pnl = trades["Net P&L"]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    equity = np.r_[
        capital,
        capital + pnl.cumsum().values
    ]

    peak = np.maximum.accumulate(equity)

    dd = peak - equity

    dd_pct = (
        dd / np.maximum(peak, 0.01)
    ) * 100

    if len(losses):

        pf = wins.sum() / abs(
            losses.sum()
        )

    elif len(wins):
        pf = np.inf

    else:
        pf = np.nan

    return {
        "Trades": len(trades),
        "Wins": len(wins),
        "Losses": len(losses),
        "Win Rate %": round(
            len(wins) / len(trades) * 100, 2
        ),
        "Net P&L": round(pnl.sum(), 2),
        "Profit Factor": round(pf, 2),
        "Maximum Drawdown": round(dd.max(), 2),
        "Drawdown %": round(dd_pct.max(), 2),
        "Return %": round(
            pnl.sum() / capital * 100, 2
        ),
        "Average Trade": round(pnl.mean(), 2),
        "Average Win": round(
            wins.mean(), 2
        ) if len(wins) else 0,
        "Average Loss": round(
            losses.mean(), 2
        ) if len(losses) else 0,
        "Target 1 Hits": int(
            trades["Target 1 Touched"].sum()
        ),
        "Target 2 Hits": int(
            (trades["Exit Reason"] == "TARGET 2").sum()
        ),
        "Trailing Exits": int(
            (trades["Exit Reason"] == "TRAILING STOP").sum()
        ),
        "Costs": round(trades["Costs"].sum(), 2)
    }


# ==================================================
# 13. ENTRY TIME ANALYSIS
# ==================================================

def time_analysis(trades):

    if trades.empty:
        return pd.DataFrame()

    d = trades.copy()

    d["Time Bucket"] = pd.to_datetime(
        d["Entry Time"]
    ).dt.strftime("%H:00")

    result = d.groupby(
        ["Strategy", "Time Bucket"]
    ).agg(
        Trades=("Net P&L", "count"),
        Net_PnL=("Net P&L", "sum"),
        Average_PnL=("Net P&L", "mean"),
        Win_Rate=(
            "Net P&L",
            lambda x: (x > 0).mean() * 100
        )
    ).reset_index()

    return result.round(2)


# ==================================================
# 14. MARKET REGIME ANALYSIS
# ==================================================

def regime_analysis(trades):

    if trades.empty:
        return pd.DataFrame()

    return trades.groupby(
        ["Strategy", "Market Regime", "Direction"]
    ).agg(
        Trades=("Net P&L", "count"),
        Net_PnL=("Net P&L", "sum"),
        Win_Rate=(
            "Net P&L",
            lambda x: (x > 0).mean() * 100
        )
    ).reset_index().round(2)


# ==================================================
# 15. CHRONOLOGICAL VALIDATION
# ==================================================

def walk_forward(
    raw,
    prepared,
    strategies,
    config,
    dates,
    is_index
):

    results = []

    if len(dates) < 25:
        return pd.DataFrame()

    # Expanding chronological evaluation.
    # Fixed rules; no future-data optimization.

    start = int(len(dates) * 0.60)

    blocks = np.array_split(
        dates[start:], 3
    )

    for fold, block in enumerate(blocks, 1):

        if len(block) == 0:
            continue

        for strategy in strategies:

            trades = run_strategy(
                raw,
                prepared,
                strategy,
                config,
                list(block),
                is_index
            )

            metrics = performance_v7(
                trades,
                config["capital"]
            )

            metrics.update({
                "Strategy": strategy,
                "Fold": fold,
                "Start": str(block[0]),
                "End": str(block[-1])
            })

            results.append(metrics)

    return pd.DataFrame(results)


# ==================================================
# 16. COST SENSITIVITY
# ==================================================

def cost_sensitivity(
    raw,
    prepared,
    strategies,
    config,
    dates,
    is_index
):

    results = []

    for slippage in [
        0.00, 0.02, 0.05, 0.10
    ]:

        settings = config.copy()

        settings["slippage"] = slippage

        for strategy in strategies:

            trades = run_strategy(
                raw,
                prepared,
                strategy,
                settings,
                dates,
                is_index
            )

            metrics = performance_v7(
                trades,
                config["capital"]
            )

            results.append({
                "Strategy": strategy,
                "Slippage Per Side %": slippage,
                "Trades": metrics["Trades"],
                "Win Rate %": metrics["Win Rate %"],
                "Net P&L": metrics["Net P&L"],
                "Profit Factor": metrics["Profit Factor"]
            })

    return pd.DataFrame(results)


# ==================================================
# 17. CSV DOWNLOAD
# ==================================================

def download_table(df, label, filename, key):

    if df is not None and not df.empty:

        st.download_button(
            label=label,
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=filename,
            mime="text/csv",
            key=key
        )


# ==================================================
# 18. STREAMLIT DASHBOARD
# ==================================================

def render_backtesting_v7(token, universe):

    st.header("V7 ADVANCED RESEARCH ENGINE")

    st.warning(
        "LEVEL 1: Spot-price simulation only. "
        "CE/PE represent directional trades, "
        "not actual option premium trades. "
        "No real orders are placed."
    )

    if not token:

        st.error("Upstox access token required.")
        return

    if not universe:

        st.error("Instrument universe unavailable.")
        return

    with st.form("v7_research_form"):

        symbols = sorted(universe.keys())

        default_symbol = (
            symbols.index("NIFTY")
            if "NIFTY" in symbols else 0
        )

        symbol = st.selectbox(
            "Select Instrument",
            symbols,
            index=default_symbol
        )

        days = st.selectbox(
            "Historical Calendar Days",
            [30, 60, 90, 120, 180, 365],
            index=2
        )

        timeframe = st.selectbox(
            "Timeframe",
            [5, 15]
        )

        direction = st.selectbox(
            "Trade Direction",
            ["BOTH", "CE", "PE"]
        )

        mode = st.selectbox(
            "Backtesting Mode",
            ["Compare All Strategies"] + STRATEGIES
        )

        st.subheader("Market Quality Filters")

        regime_filter = st.checkbox(
            "Enable Market Regime Filter",
            value=True
        )

        strict_filter = st.checkbox(
            "Enable Strict Fake Breakout Protection",
            value=True
        )

        adx_min = st.slider(
            "Minimum ADX",
            18, 35, 22
        )

        st.subheader("Risk Management")

        capital = st.number_input(
            "Backtest Capital",
            min_value=10000,
            value=100000,
            step=10000
        )

        daily_risk = st.number_input(
            "Daily Risk Budget",
            min_value=100,
            value=5000,
            step=500
        )

        risk_per_trade = st.number_input(
            "Risk Per Trade",
            min_value=100,
            value=1500,
            step=100
        )

        rr = st.slider(
            "Reward / Risk",
            1.0, 4.0, 2.0, 0.25
        )

        cost = st.number_input(
            "Round Trip Costs",
            min_value=0.0,
            value=40.0
        )

        slippage = st.number_input(
            "Slippage Per Side %",
            min_value=0.0,
            max_value=1.0,
            value=0.05,
            step=0.01,
            format="%.2f"
        )

        max_trades = st.slider(
            "Maximum Trades Per Day",
            1, 5, 3
        )

        st.subheader("Exit Configuration")

        stop_method = st.selectbox(
            "Stop Loss Method",
            ["ATR", "Swing + ATR"]
        )

        atr_mult = st.slider(
            "ATR Stop Multiplier",
            1.0, 3.0, 1.5, 0.25
        )

        trailing = st.checkbox(
            "Enable Trailing Stop Loss",
            value=True
        )

        trail_atr = st.slider(
            "Trailing ATR Multiplier",
            0.5, 3.0, 1.5, 0.25
        )

        cooldown = st.slider(
            "Cooldown Candles",
            0, 5, 2
        )

        st.subheader("Advanced Research")

        run_validation = st.checkbox(
            "Enable Walk-Forward Validation",
            value=True
        )

        run_cost_test = st.checkbox(
            "Enable Cost Sensitivity Analysis",
            value=True
        )

        run = st.form_submit_button(
            "RUN V7 ADVANCED BACKTEST"
        )

    if run:

        config = {
            "capital": capital,
            "daily_risk": daily_risk,
            "risk_per_trade": risk_per_trade,
            "rr": rr,
            "cost": cost,
            "slippage": slippage,
            "max_trades": max_trades,
            "stop_method": stop_method,
            "atr_mult": atr_mult,
            "trailing": trailing,
            "trail_atr": trail_atr,
            "cooldown": cooldown,
            "timeframe": timeframe,
            "direction": direction,
            "adx_min": adx_min,
            "regime_filter": regime_filter,
            "strict_filter": strict_filter
        }

        strategies = (
            STRATEGIES
            if mode == "Compare All Strategies"
            else [mode]
        )

        try:

            with st.spinner(
                "Downloading and validating historical data..."
            ):

                raw = fetch_history_v7(
                    token,
                    universe[symbol],
                    days
                )

                if raw.empty:

                    st.error("No historical data received.")
                    return

                raw, rejected = validate_sessions(raw)

                if raw.empty:

                    st.error("No complete sessions available.")
                    return

                prepared = prepare_v7(
                    raw, timeframe
                )

                cutoff = (
                    datetime.now(IST).date()
                    - timedelta(days=days)
                )

                dates = sorted(
                    x for x in set(raw.index.date)
                    if x >= cutoff
                )

                if not dates:

                    st.error("No sessions in selected period.")
                    return

                is_index = universe[symbol].startswith(
                    "NSE_INDEX"
                )

            comparison = []
            trade_frames = []

            progress = st.progress(0)

            for i, strategy in enumerate(strategies):

                trades = run_strategy(
                    raw,
                    prepared,
                    strategy,
                    config,
                    dates,
                    is_index
                )

                metrics = performance_v7(
                    trades, capital
                )

                metrics["Strategy"] = strategy

                comparison.append(metrics)

                if not trades.empty:
                    trade_frames.append(trades)

                progress.progress(
                    (i + 1) / len(strategies)
                )

            progress.empty()

            comparison = pd.DataFrame(
                comparison
            ).sort_values(
                "Net P&L",
                ascending=False
            )

            all_trades = (
                pd.concat(
                    trade_frames,
                    ignore_index=True
                )
                if trade_frames
                else pd.DataFrame()
            )

            validation = pd.DataFrame()
            sensitivity = pd.DataFrame()

            if run_validation:

                with st.spinner(
                    "Running chronological validation..."
                ):

                    validation = walk_forward(
                        raw,
                        prepared,
                        strategies,
                        config,
                        dates,
                        is_index
                    )

            if run_cost_test:

                with st.spinner(
                    "Testing slippage sensitivity..."
                ):

                    sensitivity = cost_sensitivity(
                        raw,
                        prepared,
                        strategies,
                        config,
                        dates,
                        is_index
                    )

            st.session_state["v7_result"] = {
                "symbol": symbol,
                "comparison": comparison,
                "trades": all_trades,
                "validation": validation,
                "sensitivity": sensitivity,
                "rejected": rejected,
                "sessions": len(dates),
                "candles": len(raw),
                "start": str(dates[0]),
                "end": str(dates[-1]),
                "config": config
            }

        except Exception as e:

            st.error(f"V7 Backtest Error: {e}")
            return

    result = st.session_state.get(
        "v7_result"
    )

    if result is None:
        return

    comparison = result["comparison"]
    trades = result["trades"]
    validation = result["validation"]
    sensitivity = result["sensitivity"]

    st.divider()

    st.subheader("Data Coverage")

    st.write(
        f"Instrument: {result['symbol']}"
    )

    st.write(
        f"Test Period: {result['start']} "
        f"to {result['end']}"
    )

    st.write(
        f"Complete Test Sessions: {result['sessions']}"
    )

    st.write(
        f"Minute Candles Including Warmup: "
        f"{result['candles']}"
    )

    st.caption(
        "Entirely missing trading sessions are not "
        "detected without an exchange calendar."
    )

    if not result["rejected"].empty:

        with st.expander("Rejected Sessions"):

            st.dataframe(
                result["rejected"],
                width="stretch"
            )

    st.subheader("Strategy Comparison")

    main_cols = [
        "Strategy",
        "Trades",
        "Win Rate %",
        "Net P&L",
        "Profit Factor",
        "Maximum Drawdown",
        "Return %"
    ]

    st.dataframe(
        comparison[main_cols],
        width="stretch",
        hide_index=True
    )

    st.bar_chart(
        comparison.set_index("Strategy")["Net P&L"]
    )

    best = comparison.iloc[0]

    if best["Trades"] == 0:

        st.warning(
            "No trades in highest-ranked configuration."
        )

    elif best["Net P&L"] <= 0:

        st.error(
            "NO PROFITABLE LEADING STRATEGY. "
            "RESEARCH ONLY."
        )

    else:

        st.info(
            f"Best research candidate: "
            f"{best['Strategy']}. "
            "Independent testing required."
        )

    download_table(
        comparison,
        "DOWNLOAD V7 COMPARISON CSV",
        "v7_comparison.csv",
        "v7_download_comparison"
    )

    if not validation.empty:

        st.subheader("Walk-Forward Validation")

        st.caption(
            "Fixed-rule chronological folds. "
            "Each fold starts with the same capital. "
            "Previously researched dates are not "
            "genuinely unseen data."
        )

        st.dataframe(
            validation,
            width="stretch",
            hide_index=True
        )

        download_table(
            validation,
            "DOWNLOAD V7 VALIDATION CSV",
            "v7_validation.csv",
            "v7_download_validation"
        )

    if not sensitivity.empty:

        st.subheader("Cost Sensitivity Analysis")

        st.dataframe(
            sensitivity,
            width="stretch",
            hide_index=True
        )

        download_table(
            sensitivity,
            "DOWNLOAD COST SENSITIVITY CSV",
            "v7_cost_sensitivity.csv",
            "v7_download_cost"
        )

    if not trades.empty:

        st.subheader("Entry Timing Analysis")

        timing = time_analysis(trades)

        st.dataframe(
            timing,
            width="stretch",
            hide_index=True
        )

        download_table(
            timing,
            "DOWNLOAD TIMING ANALYSIS CSV",
            "v7_timing.csv",
            "v7_download_timing"
        )

        st.subheader("Market Regime Analysis")

        regimes = regime_analysis(trades)

        st.dataframe(
            regimes,
            width="stretch",
            hide_index=True
        )

        download_table(
            regimes,
            "DOWNLOAD MARKET REGIME CSV",
            "v7_regimes.csv",
            "v7_download_regime"
        )

        st.subheader("Detailed Trade History")

        selected = st.selectbox(
            "View Strategy Trades",
            ["ALL"] + sorted(
                trades["Strategy"].unique()
            ),
            key="v7_trade_filter"
        )

        view = (
            trades
            if selected == "ALL"
            else trades[
                trades["Strategy"] == selected
            ]
        )

        st.dataframe(
            view,
            width="stretch",
            hide_index=True
        )

        download_table(
            trades,
            "DOWNLOAD ALL V7 TRADES CSV",
            "v7_all_trades.csv",
            "v7_download_trades"
        )

        st.subheader("Equity Curves")

        curves = {}

        for strategy, part in trades.groupby(
            "Strategy"
        ):

            daily_pnl = part.groupby(
                "Date"
            )["Net P&L"].sum()

            curves[strategy] = daily_pnl

        curve = pd.DataFrame(curves).sort_index()

        curve = (
            curve.fillna(0).cumsum()
            + result["config"]["capital"]
        )

        st.line_chart(curve)

    else:

        st.warning(
            "No qualified trades found. "
            "Do not interpret zero trades as "
            "successful performance."
        )

    st.divider()

    st.subheader("V7 Backtesting Coverage")

    coverage = pd.DataFrame([
        ["Historical spot candles", "Implemented"],
        ["ORB 15 / 30 Minutes", "Implemented"],
        ["Breakout + Retest", "Implemented"],
        ["Market Structure Filter", "Implemented"],
        ["Market Regime Detection", "Implemented"],
        ["Fake Breakout Protection", "Implemented"],
        ["Minute Level Exit Simulation", "Implemented"],
        ["ATR / Swing Stop Loss", "Implemented"],
        ["Trailing Stop Loss", "Implemented"],
        ["Entry Timing Analysis", "Implemented"],
        ["Cost Sensitivity", "Implemented"],
        ["Chronological Validation", "Implemented"],
        ["Historical Option Premium", "Pending"],
        ["Historical Greeks / OI", "Pending"],
        ["Historical News Backtesting", "Pending"],
        ["Real Order Execution", "Disabled"]
    ], columns=["Module", "Status"])

    st.dataframe(
        coverage,
        width="stretch",
        hide_index=True
    )

    st.caption(
        "Research and paper simulation only. "
        "Historical results do not guarantee "
        "future profitability."
    )
