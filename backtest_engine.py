
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
    "V5 Original",
    "Breakout + Retest",
    "Trend Pullback",
    "Market Structure",
    "Opening Range Breakout",
    "Hybrid"
]


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
            cursor + timedelta(days=20), end
        )

        url = (
            f"{API}/v3/historical-candle/"
            f"{key}/minutes/1/"
            f"{chunk_end}/{cursor}"
        )

        response = requests.get(
            url, headers=headers, timeout=30
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

        candles.extend(
            payload.get("data", {}).get("candles", [])
        )

        cursor = chunk_end + timedelta(days=1)

    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        candles,
        columns=[
            "Time", "Open", "High", "Low",
            "Close", "Volume", "OI"
        ]
    )

    df["Time"] = pd.to_datetime(
        df["Time"], utc=True, errors="coerce"
    ).dt.tz_convert(IST)

    for col in [
        "Open", "High", "Low",
        "Close", "Volume", "OI"
    ]:
        df[col] = pd.to_numeric(
            df[col], errors="coerce"
        )

    df = df.dropna(
        subset=["Time", "Open", "High", "Low", "Close"]
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

    return df.between_time("09:15", "15:29")


# ==========================================
# CANDLE ENGINE
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

    bars = bars[grouped["Close"].count() == minutes]

    return bars.dropna(
        subset=["Open", "High", "Low", "Close"]
    )


# ==========================================
# TECHNICAL INDICATORS
# ==========================================

def indicators(df):

    df = df.copy()

    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    for period in (9, 21, 50):

        df[f"EMA{period}"] = close.ewm(
            span=period, adjust=False
        ).mean()

    change = close.diff()

    gain = change.clip(lower=0).ewm(
        alpha=1/9, adjust=False
    ).mean()

    loss = (-change.clip(upper=0)).ewm(
        alpha=1/9, adjust=False
    ).mean()

    rs = gain / loss.replace(0, np.nan)

    df["RSI"] = 100 - 100 / (1 + rs)

    df.loc[
        (loss == 0) & (gain > 0), "RSI"
    ] = 100

    df.loc[
        (loss == 0) & (gain == 0), "RSI"
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
            (up > down) & (up > 0), up, 0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (down > up) & (down > 0), down, 0
        ),
        index=df.index
    )

    atr = df["ATR"].replace(0, np.nan)

    df["DI_PLUS"] = (
        100 * plus_dm.ewm(
            alpha=1/14, adjust=False
        ).mean() / atr
    )

    df["DI_MINUS"] = (
        100 * minus_dm.ewm(
            alpha=1/14, adjust=False
        ).mean() / atr
    )

    di_sum = (
        df["DI_PLUS"] + df["DI_MINUS"]
    ).replace(0, np.nan)

    dx = (
        100
        * (df["DI_PLUS"] - df["DI_MINUS"]).abs()
        / di_sum
    )

    df["ADX"] = dx.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    df["RVOL"] = (
        df["Volume"]
        / df["Volume"].shift(1)
        .rolling(20).mean()
        .replace(0, np.nan)
    )

    typical = (high + low + close) / 3
    dates = df.index.date

    cumulative_pv = (
        typical * df["Volume"]
    ).groupby(dates).cumsum()

    cumulative_volume = (
        df["Volume"].groupby(dates)
        .cumsum().replace(0, np.nan)
    )

    df["VWAP"] = cumulative_pv / cumulative_volume

    df["PreviousHigh"] = high.shift(1).rolling(5).max()
    df["PreviousLow"] = low.shift(1).rolling(5).min()

    # V6 additional indicators

    df["EMA21Slope"] = (
        df["EMA21"] - df["EMA21"].shift(3)
    )

    df["Extension"] = (
        (close - df["EMA9"]).abs() / atr
    )

    df["SwingHigh"] = high.shift(1).rolling(10).max()
    df["SwingLow"] = low.shift(1).rolling(10).min()

    df["RecentLow"] = low.rolling(3).min()
    df["RecentHigh"] = high.rolling(3).max()

    df["DI_GAP"] = (
        df["DI_PLUS"] - df["DI_MINUS"]
    ).abs()

    df["ADXChange"] = df["ADX"].diff(2)

    return df


# ==========================================
# HIGHER TIMEFRAME CONFIRMATION
# ==========================================

def prepare_data(raw, minutes):

    base = indicators(make_candles(raw, minutes))
    htf15 = indicators(make_candles(raw, 15))
    htf60 = indicators(make_candles(raw, 60))

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
            base.index, method="ffill"
        )

        base[f"{label}_Close"] = aligned["Close"]
        base[f"{label}_EMA"] = aligned["EMA21"]

        base[f"{label}_Ready"] = (
            aligned["Ready"]
            .fillna(False)
            .astype(bool)
        )

    return base


# ==========================================
# ORIGINAL V5 SIGNAL RULES
# ==========================================

def check_signal(
    row,
    opening_high,
    opening_low,
    is_index=False
):

    required = [
        "Open", "High", "Low", "Close",
        "ATR", "EMA9", "EMA21", "RSI",
        "ADX", "DI_PLUS", "DI_MINUS",
        "HTF15_Close", "HTF15_EMA",
        "HTF60_Close", "HTF60_EMA",
        "PreviousHigh", "PreviousLow"
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
    candle_range = row["High"] - row["Low"]

    if atr <= 0 or candle_range <= 0:
        return None

    body = abs(row["Close"] - row["Open"])

    close_position = (
        (row["Close"] - row["Low"]) / candle_range
    )

    if row["ADX"] < 25:
        return None

    if body < 0.35 * atr:
        return None

    if abs(row["Close"] - row["EMA9"]) > 2 * atr:
        return None

    if not is_index:

        if not np.isfinite([
            row["RVOL"], row["VWAP"]
        ]).all():
            return None

        if row["RVOL"] < 1.3:
            return None

    bull = all([
        row["Close"] > row["Open"],
        row["Close"] > row["EMA9"],
        row["EMA9"] > row["EMA21"],
        row["RSI"] >= 55,
        row["ADX"] >= 25,
        row["DI_PLUS"] > row["DI_MINUS"],
        row["HTF15_Close"] > row["HTF15_EMA"],
        row["HTF60_Close"] > row["HTF60_EMA"],
        row["Close"] > opening_high + 0.05 * atr,
        row["Close"] > row["PreviousHigh"] + 0.05 * atr,
        close_position >= 0.70,
        is_index or row["Close"] > row["VWAP"]
    ])

    bear = all([
        row["Close"] < row["Open"],
        row["Close"] < row["EMA9"],
        row["EMA9"] < row["EMA21"],
        row["RSI"] <= 45,
        row["ADX"] >= 25,
        row["DI_MINUS"] > row["DI_PLUS"],
        row["HTF15_Close"] < row["HTF15_EMA"],
        row["HTF60_Close"] < row["HTF60_EMA"],
        row["Close"] < opening_low - 0.05 * atr,
        row["Close"] < row["PreviousLow"] - 0.05 * atr,
        close_position <= 0.30,
        is_index or row["Close"] < row["VWAP"]
    ])

    if bull:
        return "CE"

    if bear:
        return "PE"

    return None


# ==========================================
# V6 MULTI-STRATEGY SIGNAL ENGINE
# ==========================================

def strategy_signal(
    day,
    i,
    opening_high,
    opening_low,
    strategy,
    is_index=False
):

    row = day.iloc[i]

    if strategy == "V5 Original":
        return check_signal(
            row, opening_high, opening_low, is_index
        )

    if i < 3:
        return None

    prev = day.iloc[i - 1]

    required = [
        "ATR", "RSI", "ADX", "EMA9",
        "EMA21", "EMA50", "EMA21Slope",
        "DI_PLUS", "DI_MINUS", "DI_GAP",
        "HTF15_Close", "HTF15_EMA",
        "Extension", "SwingHigh", "SwingLow"
    ]

    if not np.isfinite(
        [row[k] for k in required]
    ).all():
        return None

    if not row["HTF15_Ready"]:
        return None

    atr = row["ATR"]

    if atr <= 0:
        return None

    candle_range = row["High"] - row["Low"]

    if candle_range <= 0:
        return None

    body = abs(row["Close"] - row["Open"])

    close_pos = (
        (row["Close"] - row["Low"]) / candle_range
    )

    # Market regime filter

    if row["ADX"] < 20:
        return None

    if row["DI_GAP"] < 4:
        return None

    # Late entry protection

    if row["Extension"] > 1.5:
        return None

    if body < 0.20 * atr:
        return None

    if not is_index:

        if not np.isfinite([
            row["RVOL"], row["VWAP"]
        ]).all():
            return None

        if row["RVOL"] < 1.1:
            return None

    bull = all([
        row["Close"] > row["Open"],
        row["Close"] > row["EMA9"],
        row["EMA9"] > row["EMA21"],
        row["EMA21Slope"] > 0,
        52 <= row["RSI"] <= 75,
        row["DI_PLUS"] > row["DI_MINUS"],
        row["HTF15_Close"] > row["HTF15_EMA"],
        close_pos >= 0.60,
        is_index or row["Close"] > row["VWAP"]
    ])

    bear = all([
        row["Close"] < row["Open"],
        row["Close"] < row["EMA9"],
        row["EMA9"] < row["EMA21"],
        row["EMA21Slope"] < 0,
        25 <= row["RSI"] <= 48,
        row["DI_MINUS"] > row["DI_PLUS"],
        row["HTF15_Close"] < row["HTF15_EMA"],
        close_pos <= 0.40,
        is_index or row["Close"] < row["VWAP"]
    ])

    if not bull and not bear:
        return None

    # --------------------------------------
    # 1. BREAKOUT + RETEST
    # --------------------------------------

    bull_retest = False
    bear_retest = False

    for j in range(max(1, i - 4), i):

        breakout = day.iloc[j]

        level_high = max(
            opening_high,
            breakout["PreviousHigh"]
        )

        level_low = min(
            opening_low,
            breakout["PreviousLow"]
        )

        after_break = day.iloc[j + 1:i + 1]

        if (
            breakout["Close"]
            > level_high + 0.05 * breakout["ATR"]
        ):

            bull_retest |= bool(
                row["Low"] <= level_high + 0.25 * atr
                and row["Low"] >= level_high - 0.50 * atr
                and row["Close"] > level_high
                and row["Close"] > prev["Close"]
                and after_break["Close"].min()
                >= level_high - 0.25 * atr
            )

        if (
            breakout["Close"]
            < level_low - 0.05 * breakout["ATR"]
        ):

            bear_retest |= bool(
                row["High"] >= level_low - 0.25 * atr
                and row["High"] <= level_low + 0.50 * atr
                and row["Close"] < level_low
                and row["Close"] < prev["Close"]
                and after_break["Close"].max()
                <= level_low + 0.25 * atr
            )

    # --------------------------------------
    # 2. TREND PULLBACK
    # --------------------------------------

    bull_pullback = all([
        row["EMA21"] > row["EMA50"],
        prev["Low"] <= prev["EMA9"] + 0.15 * atr,
        prev["Low"] >= prev["EMA21"] - 0.35 * atr,
        row["Close"] > prev["High"]
    ])

    bear_pullback = all([
        row["EMA21"] < row["EMA50"],
        prev["High"] >= prev["EMA9"] - 0.15 * atr,
        prev["High"] <= prev["EMA21"] + 0.35 * atr,
        row["Close"] < prev["Low"]
    ])

    # --------------------------------------
    # 3. MARKET STRUCTURE BREAKOUT
    # Rolling prior range, not future pivots
    # --------------------------------------

    bull_structure = all([
        row["Close"] > row["SwingHigh"] + 0.05 * atr,
        prev["Close"] <= row["SwingHigh"],
        row["ADXChange"] >= 0
    ])

    bear_structure = all([
        row["Close"] < row["SwingLow"] - 0.05 * atr,
        prev["Close"] >= row["SwingLow"],
        row["ADXChange"] >= 0
    ])

    # --------------------------------------
    # 4. OPENING RANGE BREAKOUT
    # --------------------------------------

    orb_window = (
        day.index[i].strftime("%H:%M") <= "11:30"
    )

    bull_orb = all([
        orb_window,
        prev["Close"] <= opening_high,
        row["Close"] > opening_high + 0.10 * atr
    ])

    bear_orb = all([
        orb_window,
        prev["Close"] >= opening_low,
        row["Close"] < opening_low - 0.10 * atr
    ])

    # --------------------------------------
    # 5. HYBRID STRATEGY
    # --------------------------------------

    bull_hybrid = all([
        row["ADX"] >= 25,
        row["EMA21"] > row["EMA50"],
        row["HTF60_Ready"],
        row["HTF60_Close"] > row["HTF60_EMA"],
        row["DI_GAP"] >= 6,
        bull_retest or bull_pullback or bull_structure
    ])

    bear_hybrid = all([
        row["ADX"] >= 25,
        row["EMA21"] < row["EMA50"],
        row["HTF60_Ready"],
        row["HTF60_Close"] < row["HTF60_EMA"],
        row["DI_GAP"] >= 6,
        bear_retest or bear_pullback or bear_structure
    ])

    rules = {
        "Breakout + Retest": (
            bull_retest, bear_retest
        ),
        "Trend Pullback": (
            bull_pullback, bear_pullback
        ),
        "Market Structure": (
            bull_structure, bear_structure
        ),
        "Opening Range Breakout": (
            bull_orb, bear_orb
        ),
        "Hybrid": (
            bull_hybrid, bear_hybrid
        )
    }

    long_rule, short_rule = rules[strategy]

    if bull and long_rule:
        return "CE"

    if bear and short_rule:
        return "PE"

    return None


# ==========================================
# SESSION VALIDATION
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
    direction_filter="BOTH",
    strategy="V5 Original",
    test_end=None,
    stop_mode="ATR",
    trailing=False,
    cooldown=0,
    prepared=None
):

    if raw.empty:
        return pd.DataFrame()

    data = (
        prepared if prepared is not None
        else prepare_data(raw, minutes)
    )

    if len(data) < 60:
        return pd.DataFrame()

    trades = []
    equity = float(capital)
    slip = slippage_pct / 100

    baseline = strategy == "V5 Original"

    # Preserve original V5 exit rules
    if baseline:
        stop_mode = "ATR"
        trailing = False
        cooldown = 0

    for date in sorted(set(data.index.date)):

        if test_start is not None and date < test_start:
            continue

        if test_end is not None and date > test_end:
            continue

        if equity <= 0:
            break

        day = data[data.index.date == date]
        raw_day = raw[raw.index.date == date]

        if not complete_session(raw_day):
            continue

        opening = raw_day.between_time("09:15", "09:29")

        if len(opening) != 15:
            continue

        opening_high = opening["High"].max()
        opening_low = opening["Low"].min()

        used_risk = 0.0
        trade_count = 0
        i = 0

        while i < len(day) - 1:

            if equity <= 0 or trade_count >= max_trades:
                break

            remaining = daily_risk - used_risk

            if remaining <= brokerage:
                break

            signal_time = day.index[i]
            row = day.iloc[i]

            if signal_time.strftime("%H:%M") < "09:30":
                i += 1
                continue

            if signal_time.strftime("%H:%M") > "14:45":
                break

            side = strategy_signal(
                day,
                i,
                opening_high,
                opening_low,
                strategy,
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
                day.index[entry_index] - signal_time
                != pd.Timedelta(minutes=minutes)
            ):
                i += 1
                continue

            entry_time = signal_time
            entry_raw = day.iloc[entry_index]["Open"]

            direction = 1 if side == "CE" else -1

            entry = entry_raw * (1 + direction * slip)

            atr = row["ATR"]

            if not np.isfinite(atr) or atr <= 0:
                i += 1
                continue

            stop_distance = 1.5 * atr

            if not baseline:

                # Reject large next-candle gaps
                if abs(entry_raw - row["Close"]) > 0.5 * atr:
                    i += 1
                    continue

                if stop_mode == "Swing + ATR":

                    if side == "CE":
                        structural_stop = (
                            row["RecentLow"] - 0.15 * atr
                        )

                        structural_risk = (
                            entry - structural_stop
                        )

                    else:
                        structural_stop = (
                            row["RecentHigh"] + 0.15 * atr
                        )

                        structural_risk = (
                            structural_stop - entry
                        )

                    stop_distance = max(
                        stop_distance,
                        structural_risk
                    )

                    if stop_distance > 3 * atr:
                        i += 1
                        continue

            if stop_distance <= 0 or entry <= 0:
                i += 1
                continue

            stop = entry - direction * stop_distance

            initial_stop = stop

            target1 = entry + direction * stop_distance

            target2 = (
                entry + direction * stop_distance * rr
            )

            if stop <= 0 or target2 <= 0:
                i += 1
                continue

            risk_budget = min(
                remaining, risk_per_trade, equity
            )

            unit_risk = stop_distance + stop * slip

            quantity = int(min(
                max(0, risk_budget - brokerage)
                / unit_risk,
                equity / entry
            ))

            if quantity < 1:
                i += 1
                continue

            planned_risk = (
                quantity * unit_risk + brokerage
            )

            exit_price = None
            exit_time = None
            reason = None

            t1_hit = False
            best_price = entry
            mfe = 0.0
            mae = 0.0

            future = raw_day[
                raw_day.index >= entry_time
            ].between_time("09:30", "15:19")

            for ts, candle in future.iterrows():

                op = candle["Open"]
                high = candle["High"]
                low = candle["Low"]

                # Existing stop is checked before
                # any trailing adjustment.

                if side == "CE":

                    if op <= stop:
                        exit_price = op
                        reason = "STOP LOSS"

                    elif op >= target2:
                        exit_price = target2
                        reason = "TARGET"

                    elif low <= stop:
                        exit_price = stop
                        reason = "STOP LOSS"

                    elif high >= target2:
                        exit_price = target2
                        reason = "TARGET"

                else:

                    if op >= stop:
                        exit_price = op
                        reason = "STOP LOSS"

                    elif op <= target2:
                        exit_price = target2
                        reason = "TARGET"

                    elif high >= stop:
                        exit_price = stop
                        reason = "STOP LOSS"

                    elif low <= target2:
                        exit_price = target2
                        reason = "TARGET"

                if exit_price is not None:

                    if reason == "TARGET":
                        t1_hit = True

                    if (
                        reason == "STOP LOSS"
                        and stop != initial_stop
                    ):
                        reason = "TRAILING SL"

                    exit_time = ts + pd.Timedelta(minutes=1)
                    break

                # Excursions on fully survived minutes
                if side == "CE":

                    mfe = max(mfe, high - entry)
                    mae = max(mae, entry - low)

                    if high >= target1:
                        t1_hit = True

                    best_price = max(best_price, high)

                    if trailing and t1_hit:

                        stop = max(
                            stop,
                            entry,
                            best_price - 1.5 * atr
                        )

                else:

                    mfe = max(mfe, entry - low)
                    mae = max(mae, high - entry)

                    if low <= target1:
                        t1_hit = True

                    best_price = min(best_price, low)

                    if trailing and t1_hit:

                        stop = min(
                            stop,
                            entry,
                            best_price + 1.5 * atr
                        )

                # Updated trailing stop becomes
                # effective from the next minute.

                if ts.strftime("%H:%M") >= "15:19":

                    exit_price = candle["Close"]

                    exit_time = (
                        ts + pd.Timedelta(minutes=1)
                    )

                    reason = "SESSION EXIT"
                    break

            if exit_price is None:
                i += 1
                continue

            exit_price *= 1 - direction * slip

            points = (exit_price - entry) * direction

            gross = points * quantity
            net = gross - brokerage

            equity += net

            used_risk += max(planned_risk, -net)

            trade_count += 1

            trades.append({
                "Strategy": strategy,
                "Date": str(date),
                "Direction": side,
                "Entry Time": str(entry_time),
                "Exit Candle End": str(exit_time),
                "Holding Minutes": (
                    exit_time - entry_time
                ).total_seconds() / 60,
                "Entry": round(entry, 2),
                "Stop Loss": round(initial_stop, 2),
                "Target 1": round(target1, 2),
                "Target 2": round(target2, 2),
                "Target": round(target2, 2),
                "Final Trailing SL": round(stop, 2),
                "Target 1 Touched": t1_hit,
                "Exit": round(exit_price, 2),
                "Spot Units": quantity,
                "Points": round(points, 2),
                "Planned Risk": round(planned_risk, 2),
                "Gross P&L": round(gross, 2),
                "Estimated Costs": brokerage,
                "Net P&L": round(net, 2),
                "Exit Reason": reason,
                "Equity": round(equity, 2),
                "RSI": round(row["RSI"], 2),
                "ADX": round(row["ADX"], 2),
                "ATR": round(atr, 2),
                "RVOL": round(row["RVOL"], 2),
                "EMA9": round(row["EMA9"], 2),
                "EMA21": round(row["EMA21"], 2),
                "EMA50": round(row["EMA50"], 2),
                "VWAP": round(row["VWAP"], 2),
                "DI Gap": round(row["DI_GAP"], 2),
                "ATR Extension": round(row["Extension"], 2),
                "Opening High": round(opening_high, 2),
                "Opening Low": round(opening_low, 2),
                "MFE Survived Bars": round(mfe, 2),
                "MAE Survived Bars": round(mae, 2)
            })

            i = max(
                i + 1,
                int(
                    day.index.searchsorted(
                        exit_time, side="right"
                    )
                ) + cooldown
            )

    return pd.DataFrame(trades)


# ==========================================
# PERFORMANCE REPORT
# ==========================================

def performance(trades, capital):

    if trades.empty:

        return {
            "Total Trades": 0,
            "CE Signals": 0,
            "PE Signals": 0,
            "Winning Trades": 0,
            "Losing Trades": 0,
            "Win Rate": None,
            "Net P&L": 0.0,
            "Profit Factor": None,
            "Maximum Drawdown": 0.0,
            "Drawdown %": 0.0,
            "Return %": 0.0,
            "Final Equity": capital
        }

    pnl = trades["Net P&L"]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    equity = pd.Series(
        [capital] + trades["Equity"].tolist()
    )

    peak = equity.cummax()
    drawdown = peak - equity

    drawdown_pct = (
        drawdown / peak.replace(0, np.nan)
    ) * 100

    ce = trades[trades["Direction"] == "CE"]
    pe = trades[trades["Direction"] == "PE"]

    return {
        "Total Trades": len(trades),
        "CE Signals": len(ce),
        "PE Signals": len(pe),
        "Winning Trades": len(wins),
        "Losing Trades": len(losses),
        "Breakeven Trades": int((pnl == 0).sum()),
        "Win Rate": round(100 * len(wins) / len(trades), 2),
        "Loss Rate": round(100 * len(losses) / len(trades), 2),
        "Target Hits": int(
            (trades["Exit Reason"] == "TARGET").sum()
        ),
        "SL Hits": int(
            (trades["Exit Reason"] == "STOP LOSS").sum()
        ),
        "Trailing SL Hits": int(
            (trades["Exit Reason"] == "TRAILING SL").sum()
        ),
        "Session Exits": int(
            (trades["Exit Reason"] == "SESSION EXIT").sum()
        ),
        "Target 1 Touched": int(
            trades["Target 1 Touched"].sum()
        ),
        "Gross P&L": round(trades["Gross P&L"].sum(), 2),
        "Estimated Costs": round(
            trades["Estimated Costs"].sum(), 2
        ),
        "Net P&L": round(pnl.sum(), 2),
        "CE Net P&L": round(ce["Net P&L"].sum(), 2),
        "PE Net P&L": round(pe["Net P&L"].sum(), 2),
        "Average Trade": round(pnl.mean(), 2),
        "Average Win": round(wins.mean(), 2) if len(wins) else 0,
        "Average Loss": round(losses.mean(), 2) if len(losses) else 0,
        "Best Trade": round(pnl.max(), 2),
        "Worst Trade": round(pnl.min(), 2),
        "Profit Factor": (
            round(gross_profit / gross_loss, 2)
            if gross_loss > 0 else None
        ),
        "Maximum Drawdown": round(drawdown.max(), 2),
        "Drawdown %": round(drawdown_pct.max(), 2),
        "Return %": round(100 * pnl.sum() / capital, 2),
        "Final Equity": round(capital + pnl.sum(), 2)
    }


# ==========================================
# STRATEGY COMPARISON
# ==========================================

def compare_strategies(
    raw,
    strategies,
    start,
    end,
    settings
):

    prepared = prepare_data(
        raw, settings["minutes"]
    )

    sessions = []

    for date in sorted(set(raw.index.date)):

        if start <= date <= end:

            raw_day = raw[raw.index.date == date]

            if complete_session(raw_day):
                sessions.append(date)

    split = int(len(sessions) * 0.70)

    holdout_available = (
        split >= 1 and split < len(sessions)
    )

    summary = []
    validation = []
    all_trades = {}

    for strategy in strategies:

        trades = run_backtest(
            raw=raw,
            strategy=strategy,
            test_start=start,
            test_end=end,
            prepared=prepared,
            **settings
        )

        all_trades[strategy] = trades

        metrics = performance(
            trades, settings["capital"]
        )

        summary.append({
            "Strategy": strategy,
            **metrics
        })

        if holdout_available:

            periods = [
                (
                    "Development 70%",
                    sessions[0],
                    sessions[split - 1]
                ),
                (
                    "Holdout 30%",
                    sessions[split],
                    sessions[-1]
                )
            ]

            for label, p_start, p_end in periods:

                period_trades = run_backtest(
                    raw=raw,
                    strategy=strategy,
                    test_start=p_start,
                    test_end=p_end,
                    prepared=prepared,
                    **settings
                )

                pm = performance(
                    period_trades,
                    settings["capital"]
                )

                validation.append({
                    "Strategy": strategy,
                    "Period": label,
                    "Start": str(p_start),
                    "End": str(p_end),
                    "Total Trades": pm["Total Trades"],
                    "Win Rate": pm["Win Rate"],
                    "Net P&L": pm["Net P&L"],
                    "Profit Factor": pm["Profit Factor"],
                    "Drawdown %": pm["Drawdown %"]
                })

    return (
        pd.DataFrame(summary),
        pd.DataFrame(validation),
        all_trades
    )


# ==========================================
# STREAMLIT DASHBOARD
# ==========================================

def render_backtesting(token, universe):

    st.header("V6 MULTI-STRATEGY BACKTESTING")

    st.warning(
        "LEVEL 1: Technical spot-price simulation. "
        "CE/PE are directional proxies, not actual "
        "option premium trades. Historical option "
        "Greeks, OI, bid/ask and news are not tested."
    )

    if not universe:
        st.error("Instrument list unavailable.")
        return

    symbols = sorted(universe.keys())

    symbol = st.selectbox(
        "Select Instrument",
        symbols,
        index=symbols.index("NIFTY")
        if "NIFTY" in symbols else 0,
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

    direction_filter = st.selectbox(
        "Trade Direction",
        ["BOTH", "CE", "PE"],
        key="bt_direction"
    )

    mode = st.selectbox(
        "Backtesting Mode",
        ["Compare All Strategies"] + STRATEGIES,
        key="bt_v6_mode"
    )

    st.subheader("Risk Management")

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
        1.0, 4.0, 2.0, 0.25,
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
        1, 10, 3,
        key="bt_max_trades"
    )

    st.subheader("V6 Exit Configuration")

    stop_mode = st.selectbox(
        "Stop Loss Method",
        ["ATR", "Swing + ATR"],
        key="bt_v6_stop"
    )

    trailing = st.checkbox(
        "Enable Trailing Stop Loss",
        value=False,
        key="bt_v6_trailing"
    )

    cooldown = st.slider(
        "Extra Cooldown Candles",
        0, 5, 0,
        key="bt_v6_cooldown"
    )

    st.caption(
        "V5 Original always retains its original "
        "ATR stop, fixed target and re-entry rules. "
        "Target 1 is an observation milestone, "
        "not a partial exit. Target 2 closes "
        "the full simulated position."
    )

    settings = {
        "minutes": minutes,
        "capital": capital,
        "daily_risk": daily_risk,
        "risk_per_trade": risk_per_trade,
        "rr": rr,
        "brokerage": brokerage,
        "slippage_pct": slippage,
        "max_trades": max_trades,
        "is_index": universe[symbol].startswith("NSE_INDEX"),
        "direction_filter": direction_filter,
        "stop_mode": stop_mode,
        "trailing": trailing,
        "cooldown": cooldown
    }

    signature = (
        symbol, universe[symbol], days, mode,
        tuple(settings.items())
    )

    if st.button(
        "RUN HISTORICAL BACKTEST",
        type="primary",
        key="bt_run"
    ):

        st.session_state.pop("bt_v6_result", None)

        if not token:
            st.error("Upstox token required.")
            return

        try:

            with st.spinner(
                "Downloading candles and comparing strategies..."
            ):

                end = (
                    datetime.now(IST).date()
                    - timedelta(days=1)
                )

                start = end - timedelta(days=days - 1)

                raw = fetch_history(
                    token, universe[symbol], days
                )

                if raw.empty:
                    st.warning("Historical data unavailable.")
                    return

                test_raw = raw[
                    (raw.index.date >= start)
                    & (raw.index.date <= end)
                ]

                if test_raw.empty:
                    st.warning("No data in requested period.")
                    return

                dates = sorted(set(test_raw.index.date))

                complete_days = sum(
                    complete_session(
                        test_raw[test_raw.index.date == d]
                    )
                    for d in dates
                )

                strategies = (
                    STRATEGIES
                    if mode == "Compare All Strategies"
                    else [mode]
                )

                summary, validation, trades = (
                    compare_strategies(
                        raw,
                        strategies,
                        start,
                        end,
                        settings
                    )
                )

                st.session_state["bt_v6_result"] = {
                    "signature": signature,
                    "summary": summary,
                    "validation": validation,
                    "trades": trades,
                    "candles": len(test_raw),
                    "sessions": len(dates),
                    "complete": complete_days,
                    "incomplete": len(dates) - complete_days,
                    "start": str(test_raw.index.min()),
                    "end": str(test_raw.index.max()),
                    "capital": capital
                }

        except Exception as exc:
            st.error(f"Backtest failed: {exc}")

    result = st.session_state.get("bt_v6_result")

    if result is None:
        return

    if result["signature"] != signature:
        st.info("Settings changed. Run backtest again.")
        return

    st.divider()

    st.subheader("Historical Data Coverage")

    a, b = st.columns(2)

    a.metric("Historical Candles", result["candles"])
    b.metric("Sessions Received", result["sessions"])

    c, d = st.columns(2)

    c.metric("Complete Sessions", result["complete"])
    d.metric("Incomplete Sessions Skipped", result["incomplete"])

    st.caption(
        f"Data: {result['start']} to {result['end']}"
    )

    st.caption(
        "15 extra calendar days are requested for "
        "indicator warm-up. Completely missing "
        "sessions cannot be identified without "
        "an exchange calendar."
    )

    summary = result["summary"].copy()

    st.subheader("STRATEGY COMPARISON")

    display_columns = [
        "Strategy",
        "Total Trades",
        "Winning Trades",
        "Losing Trades",
        "Win Rate",
        "Net P&L",
        "Profit Factor",
        "Maximum Drawdown",
        "Return %"
    ]

    st.dataframe(
        summary[display_columns].sort_values(
            "Net P&L", ascending=False
        ),
        hide_index=True,
        use_container_width=True
    )

    st.bar_chart(
        summary.set_index("Strategy")["Net P&L"]
    )

    st.caption(
        "Ranking is descriptive, not a live trading "
        "recommendation. No-trade strategies are "
        "not evidence of profitability."
    )

    st.subheader("CHRONOLOGICAL VALIDATION")

    validation = result["validation"]

    if validation.empty:

        st.warning(
            "Insufficient complete sessions "
            "for chronological validation."
        )

    else:

        st.dataframe(
            validation,
            hide_index=True,
            use_container_width=True
        )

        st.info(
            "First 70% of complete sessions: development. "
            "Last 30%: holdout. Each period starts "
            "with the same capital. Do not tune rules "
            "using holdout results. Previously reviewed "
            "dates are not genuinely unseen data."
        )

    st.subheader("EQUITY COMPARISON BY DATE")

    curves = {}

    for name, trades in result["trades"].items():

        if not trades.empty:

            curves[name] = (
                trades.groupby("Date")["Net P&L"]
                .sum()
            )

    if curves:

        equity_curves = (
            pd.DataFrame(curves)
            .sort_index()
            .fillna(0)
            .cumsum()
            + result["capital"]
        )

        st.line_chart(equity_curves)

    st.download_button(
        "DOWNLOAD STRATEGY COMPARISON",
        summary.to_csv(index=False),
        file_name=f"{symbol}_v6_comparison.csv",
        mime="text/csv",
        key="bt_v6_comparison_csv"
    )

    if not validation.empty:

        st.download_button(
            "DOWNLOAD VALIDATION REPORT",
            validation.to_csv(index=False),
            file_name=f"{symbol}_v6_validation.csv",
            mime="text/csv",
            key="bt_v6_validation_csv"
        )

    st.divider()

    st.subheader("INDIVIDUAL STRATEGY ANALYSIS")

    selected = st.selectbox(
        "Select Strategy Details",
        list(result["trades"].keys()),
        key="bt_v6_detail"
    )

    trades = result["trades"][selected]

    metrics = performance(
        trades, result["capital"]
    )

    if trades.empty:

        st.info(
            "No qualifying simulated trades "
            "for this strategy."
        )

    else:

        a, b = st.columns(2)

        a.metric("Total Trades", metrics["Total Trades"])
        b.metric("Win Rate", f"{metrics['Win Rate']}%")

        c, d = st.columns(2)

        c.metric(
            "Net P&L",
            f"₹{metrics['Net P&L']:,.2f}"
        )

        d.metric(
            "Maximum Drawdown",
            f"₹{metrics['Maximum Drawdown']:,.2f}"
        )

        e, f = st.columns(2)

        e.metric("Target Hits", metrics["Target Hits"])
        f.metric("SL Hits", metrics["SL Hits"])

        g, h = st.columns(2)

        g.metric(
            "Profit Factor",
            metrics["Profit Factor"]
            if metrics["Profit Factor"] is not None
            else "N/A"
        )

        h.metric(
            "Return",
            f"{metrics['Return %']}%"
        )

        st.subheader("Complete Performance Report")

        report = pd.DataFrame(
            list(metrics.items()),
            columns=["Metric", "Result"]
        )

        st.dataframe(
            report.astype(str),
            hide_index=True,
            use_container_width=True
        )

        st.subheader("Daily Simulated P&L")

        st.bar_chart(
            trades.groupby("Date")["Net P&L"].sum()
        )

        st.subheader("Drawdown Analysis")

        equity = pd.Series(
            [result["capital"]]
            + trades["Equity"].tolist()
        )

        st.line_chart(
            pd.DataFrame({
                "Drawdown": equity - equity.cummax()
            })
        )

        st.subheader("CE vs PE Performance")

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

        st.subheader("Historical Trade History")

        st.dataframe(
            trades,
            hide_index=True,
            use_container_width=True
        )

        st.download_button(
            "DOWNLOAD BACKTEST CSV",
            trades.to_csv(index=False),
            file_name=f"{symbol}_{selected}_v6.csv",
            mime="text/csv",
            key="bt_download_trades"
        )

        st.download_button(
            "DOWNLOAD PERFORMANCE REPORT",
            report.to_csv(index=False),
            file_name=f"{symbol}_{selected}_report.csv",
            mime="text/csv",
            key="bt_download_report"
        )

    nonempty = [
        t for t in result["trades"].values()
        if not t.empty
    ]

    if nonempty:

        combined = pd.concat(
            nonempty, ignore_index=True
        )

        st.download_button(
            "DOWNLOAD ALL STRATEGY TRADES",
            combined.to_csv(index=False),
            file_name=f"{symbol}_all_v6_trades.csv",
            mime="text/csv",
            key="bt_v6_all_trades"
        )

    st.caption(
        "Research simulation only. Completed candles "
        "and next-candle entries are used. Stop loss "
        "takes priority when stop and target are "
        "touched in the same minute. Trailing stop "
        "updates apply to the next minute. Drawdown "
        "uses closed trades. Excursion metrics exclude "
        "the exit minute. Costs are estimates. "
        "Price gaps may exceed planned risk. "
        "No real orders are executed."
    )
