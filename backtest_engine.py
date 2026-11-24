
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

def fetch_history(token, instrument_key, days):

    end = datetime.now(IST).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)

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
            url,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()
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
            "Time", "Open", "High", "Low",
            "Close", "Volume", "OI"
        ]
    )

    df["Time"] = pd.to_datetime(
        df["Time"], utc=True
    ).dt.tz_convert(IST)

    for col in [
        "Open", "High", "Low",
        "Close", "Volume", "OI"
    ]:
        df[col] = pd.to_numeric(
            df[col], errors="coerce"
        )

    df = df.dropna(
        subset=["Open", "High", "Low", "Close"]
    )

    df = (
        df.drop_duplicates("Time")
        .sort_values("Time")
        .set_index("Time")
    )

    return df.between_time("09:15", "15:29")


# ==========================================
# CANDLE RESAMPLING
# ==========================================

def make_candles(df, minutes):

    if df.empty:
        return df.copy()

    rule = f"{minutes}min"

    grouped = df.resample(
        rule,
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

    df["EMA9"] = close.ewm(
        span=9, adjust=False
    ).mean()

    df["EMA21"] = close.ewm(
        span=21, adjust=False
    ).mean()

    df["EMA50"] = close.ewm(
        span=50, adjust=False
    ).mean()

    change = close.diff()

    gain = change.clip(lower=0).ewm(
        alpha=1/9, adjust=False,
        min_periods=9
    ).mean()

    loss = (-change.clip(upper=0)).ewm(
        alpha=1/9, adjust=False,
        min_periods=9
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
        100 *
        (df["DI_PLUS"] - df["DI_MINUS"]).abs()
        / di_sum
    )

    df["ADX"] = dx.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    df["RVOL"] = (
        df["Volume"] /
        df["Volume"].shift(1)
        .rolling(20).mean()
        .replace(0, np.nan)
    )

    typical = (high + low + close) / 3

    dates = df.index.date

    cumulative_pv = (
        typical * df["Volume"]
    ).groupby(dates).cumsum()

    cumulative_volume = (
        df["Volume"]
        .groupby(dates).cumsum()
        .replace(0, np.nan)
    )

    df["VWAP"] = (
        cumulative_pv / cumulative_volume
    )

    df["PreviousHigh"] = (
        high.shift(1).rolling(5).max()
    )

    df["PreviousLow"] = (
        low.shift(1).rolling(5).min()
    )

    return df


# ==========================================
# HIGHER TIMEFRAME CONFIRMATION
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

    for label, higher in [
        ("HTF15", htf15),
        ("HTF60", htf60)
    ]:

        aligned = higher[
            ["Close", "EMA21"]
        ].reindex(
            base.index,
            method="ffill"
        )

        base[f"{label}_Close"] = aligned["Close"]
        base[f"{label}_EMA"] = aligned["EMA21"]

    return base


# ==========================================
# SIGNAL & FAKE BREAKOUT FILTER
# ==========================================

def check_signal(
    row,
    opening_high,
    opening_low,
    is_index=False
):

    atr = row["ATR"]

    if not np.isfinite(atr) or atr <= 0:
        return None

    candle_range = row["High"] - row["Low"]

    if candle_range <= 0:
        return None

    body = abs(row["Close"] - row["Open"])

    close_position = (
        (row["Close"] - row["Low"])
        / candle_range
    )

    if row["ADX"] < 25:
        return None

    if body < 0.35 * atr:
        return None

    if abs(
        row["Close"] - row["EMA9"]
    ) > 2 * atr:
        return None

    if not is_index:

        if not np.isfinite(row["RVOL"]):
            return None

        if row["RVOL"] < 1.3:
            return None

    bull = all([
        row["Close"] > row["Open"],
        row["EMA9"] > row["EMA21"],
        row["EMA21"] > row["EMA50"],
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
        row["EMA9"] < row["EMA21"],
        row["EMA21"] < row["EMA50"],
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
    is_index=False
):

    if raw.empty:
        return pd.DataFrame()

    data = prepare_data(raw, minutes)

    if len(data) < 60:
        return pd.DataFrame()

    trades = []
    equity = float(capital)

    dates = sorted(set(data.index.date))

    for date in dates:

        day = data[data.index.date == date]

        raw_day = raw[
            raw.index.date == date
        ]

        opening = raw_day.between_time(
            "09:15", "09:29"
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

            side = check_signal(
                row,
                opening_high,
                opening_low,
                is_index
            )

            if side is None:
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

            slip = slippage_pct / 100

            direction = 1 if side == "CE" else -1

            entry = entry_raw * (
                1 + direction * slip
            )

            stop_distance = 1.5 * row["ATR"]

            if stop_distance <= 0:
                i += 1
                continue

            stop = entry - direction * stop_distance

            target = (
                entry +
                direction * stop_distance * rr
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
                stop_distance + stop * slip
            )

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
            j = entry_index

            for j in range(
                entry_index, len(day)
            ):

                candle = day.iloc[j]
                ts = day.index[j]

                op = candle["Open"]
                high = candle["High"]
                low = candle["Low"]

                if side == "CE":

                    stop_hit = low <= stop
                    target_hit = high >= target

                    if stop_hit:
                        exit_price = min(stop, op)
                        reason = "STOP LOSS"

                    elif target_hit:
                        exit_price = target
                        reason = "TARGET"

                else:

                    stop_hit = high >= stop
                    target_hit = low <= target

                    if stop_hit:
                        exit_price = max(stop, op)
                        reason = "STOP LOSS"

                    elif target_hit:
                        exit_price = target
                        reason = "TARGET"

                if exit_price is not None:
                    exit_time = ts
                    break

                if (
                    ts.strftime("%H:%M") >= "15:15"
                    or j == len(day) - 1
                ):

                    exit_price = candle["Close"]
                    exit_time = ts
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
                planned_risk, -net
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
                "Planned Risk": round(planned_risk, 2),
                "Gross P&L": round(gross, 2),
                "Estimated Costs": brokerage,
                "Net P&L": round(net, 2),
                "Exit Reason": reason,
                "Equity": round(equity, 2),
                "RSI": round(row["RSI"], 2),
                "ADX": round(row["ADX"], 2)
            })

            i = j + 1

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
        [capital] +
        trades["Equity"].tolist()
    )

    peak = equity.cummax()
    drawdown = peak - equity

    drawdown_pct = (
        drawdown / peak.replace(0, np.nan)
    ) * 100

    return {
        "Total Trades": len(trades),
        "CE Signals": int(
            (trades["Direction"] == "CE").sum()
        ),
        "PE Signals": int(
            (trades["Direction"] == "PE").sum()
        ),
        "Winning Trades": len(wins),
        "Losing Trades": len(losses),
        "Win Rate": round(
            100 * len(wins) / len(trades), 2
        ),
        "Target Hits": int(
            (trades["Exit Reason"] == "TARGET").sum()
        ),
        "SL Hits": int(
            (trades["Exit Reason"] == "STOP LOSS").sum()
        ),
        "Net P&L": round(pnl.sum(), 2),
        "Profit Factor": round(
            gross_profit / gross_loss, 2
        ) if gross_loss > 0 else None,
        "Maximum Drawdown": round(
            drawdown.max(), 2
        ),
        "Drawdown %": round(
            drawdown_pct.max(), 2
        ),
        "Final Equity": round(
            capital + pnl.sum(), 2
        )
    }


# ==========================================
# STREAMLIT BACKTESTING DASHBOARD
# ==========================================

def render_backtesting(token, universe):

    st.header("Historical Backtesting")

    st.warning(
        "LEVEL 1: Technical spot-price simulation. "
        "CE/PE labels represent bullish/bearish "
        "directions only. Results are NOT actual "
        "option premium profits. Historical news, "
        "Greeks and option OI are not tested."
    )

    if not universe:
        st.error("Instrument list unavailable.")
        return

    symbol = st.selectbox(
        "Select Instrument",
        sorted(universe.keys()),
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

    if st.button(
        "RUN HISTORICAL BACKTEST",
        type="primary",
        key="bt_run"
    ):

        st.session_state.pop(
            "bt_result", None
        )

        if not token:
            st.error("Upstox token required.")
            return

        try:

            with st.spinner(
                "Downloading historical candles "
                "and testing strategy..."
            ):

                instrument_key = universe[symbol]

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
                    )
                )

                st.session_state["bt_result"] = {
                    "symbol": symbol,
                    "days": days,
                    "minutes": minutes,
                    "capital": capital,
                    "candles": len(raw),
                    "sessions": len(set(raw.index.date)),
                    "start": str(raw.index.min()),
                    "end": str(raw.index.max()),
                    "trades": trades,
                    "metrics": performance(
                        trades, capital
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

    st.subheader("Historical Test Results")

    st.caption(
        f"{result['symbol']} | "
        f"{result['days']} calendar days | "
        f"{result['minutes']} minute timeframe"
    )

    st.write(
        "Historical candles:", result["candles"]
    )

    st.write(
        "Sessions received:", result["sessions"]
    )

    st.caption(
        f"Data: {result['start']} "
        f"to {result['end']}"
    )

    trades = result["trades"]
    metrics = result["metrics"]

    if trades.empty:

        st.info(
            "No qualifying trades in the "
            "available historical data."
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

    st.dataframe(
        pd.DataFrame(
            list(metrics.items()),
            columns=["Metric", "Result"]
        ),
        hide_index=True,
        use_container_width=True
    )

    st.subheader("Simulated Equity Curve")

    equity = pd.DataFrame({
        "Equity": [result["capital"]] +
        trades["Equity"].tolist()
    })

    st.line_chart(equity)

    st.subheader("Historical Trade History")

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
            "technical_backtest.csv"
        ),
        mime="text/csv"
    )

    st.caption(
        "Signals use completed candles and "
        "next-candle entries. If stop and target "
        "are touched in the same candle, stop "
        "takes priority. Costs are estimates. "
        "Drawdown is based on closed trades. "
        "Price gaps can exceed planned risk. "
        "Past performance does not guarantee "
        "future results."
    )
