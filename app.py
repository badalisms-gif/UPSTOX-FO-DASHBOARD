
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Upstox F&O Directional Dashboard",
    page_icon="📊",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com/v2"

KEYS = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "FIN NIFTY": "NSE_INDEX|Nifty Fin Service"
}


def number(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def api_get(path, token, params):
    response = requests.get(
        BASE + path,
        params=params,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json"
        },
        timeout=20
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Upstox HTTP {response.status_code}: "
            + response.text[:250]
        )

    payload = response.json()

    if payload.get("status") != "success":
        raise RuntimeError(str(
            payload.get("errors")
            or payload.get("message")
            or "Unknown API error"
        ))

    return payload.get("data") or []


def unpack_chain(chain):
    records = []

    for item in chain:
        strike = number(item.get("strike_price"))
        spot = number(item.get("underlying_spot_price"))

        for side, key in [
            ("CE", "call_options"),
            ("PE", "put_options")
        ]:
            option = item.get(key) or {}
            market = option.get("market_data") or {}
            greeks = option.get("option_greeks") or {}

            bid = number(market.get("bid_price"))
            ask = number(market.get("ask_price"))
            oi = number(market.get("oi"))
            prev_oi = number(market.get("prev_oi"))

            spread = None

            if ask >= bid > 0:
                spread = (
                    (ask - bid) / ((ask + bid) / 2)
                ) * 100

            records.append({
                "strike": strike,
                "side": side,
                "spot": spot,
                "ltp": number(market.get("ltp")),
                "oi": oi,
                "prev_oi": prev_oi,
                "oi_change_day": oi - prev_oi,
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


def market_is_open(now):
    return (
        now.weekday() < 5
        and time(9, 15) <= now.time() <= time(15, 30)
    )


def analyze_side(
    df, side, previous, spot_change,
    max_spread, max_iv, min_volume, rr
):
    if df.empty:
        return []

    spot = number(df["spot"].iloc[0])

    strikes = sorted(df["strike"].unique())

    gaps = [
        b - a
        for a, b in zip(strikes[:-1], strikes[1:])
        if b > a
    ]

    step = min(gaps) if gaps else 50

    candidates = df[
        (df["side"] == side)
        & (abs(df["strike"] - spot) <= step * 3)
        & (df["ltp"] > 0)
        & (df["ask"] > 0)
    ].copy()

    opposite = "PE" if side == "CE" else "CE"

    opposite_df = df[df["side"] == opposite]

    if side == "CE":
        wall = opposite_df[
            (opposite_df["strike"] <= spot)
            & (opposite_df["strike"] >= spot - step * 2)
        ]
    else:
        wall = opposite_df[
            (opposite_df["strike"] >= spot)
            & (opposite_df["strike"] <= spot + step * 2)
        ]

    wall_change = wall["oi_change_day"].sum()

    results = []

    for _, row in candidates.iterrows():

        reasons = []
        failures = []

        previous_row = previous.get(row["instrument"])

        premium_change = None
        live_oi_change = None

        if previous_row is not None:
            premium_change = (
                row["ltp"] - previous_row["ltp"]
            )
            live_oi_change = (
                row["oi"] - previous_row["oi"]
            )

        # Seven screening checks.
        checks = [
            (
                wall_change > 0,
                "Supporting opposite-side OI buildup"
            ),
            (
                row["oi_change_day"] > 0,
                "Selected contract OI above previous close"
            ),
            (
                row["volume"] >= min_volume,
                "Volume threshold passed"
            ),
            (
                row["spread_pct"] is not None
                and 0 <= row["spread_pct"] <= max_spread,
                "Bid/ask spread acceptable"
            ),
            (
                row["delta"] is not None
                and 0.35 <= abs(row["delta"]) <= 0.75,
                "Delta in preferred range"
            ),
            (
                row["iv"] is not None
                and 0 < row["iv"] <= max_iv,
                "IV within limit"
            ),
            (
                premium_change is not None
                and premium_change > 0,
                "Premium rising since previous snapshot"
            )
        ]

        for passed, description in checks:
            if passed:
                reasons.append(description)
            else:
                failures.append(description)

        score = len(reasons)

        entry = row["ask"]

        risk = max(entry * 0.12, 1.0)

        sl = max(0.05, entry - risk)
        target = entry + risk * rr

        direction_ok = (
            spot_change is not None
            and (
                (side == "CE" and spot_change > 0)
                or
                (side == "PE" and spot_change < 0)
            )
        )

        # Require actual intraday OI increase
        # as an additional hard confirmation.
        oi_live_ok = (
            live_oi_change is not None
            and live_oi_change > 0
        )

        results.append({
            "strike": row["strike"],
            "side": side,
            "ltp": row["ltp"],
            "score": score,
            "delta": row["delta"],
            "iv": row["iv"],
            "spread": row["spread_pct"],
            "oi": row["oi"],
            "oi_change_day": row["oi_change_day"],
            "premium_change": premium_change,
            "live_oi_change": live_oi_change,
            "spot_change": spot_change,
            "direction_ok": direction_ok,
            "oi_live_ok": oi_live_ok,
            "entry": entry,
            "sl": sl,
            "target": target,
            "reasons": reasons,
            "failures": failures,
            "distance": abs(row["strike"] - spot)
        })

    results.sort(
        key=lambda x: (
            x["score"],
            -x["distance"]
        ),
        reverse=True
    )

    return results[:3]


def fmt_change(value):
    if value is None:
        return "N/A"
    return f"{value:+,.2f}"


def display_candidates(title, items):
    st.subheader(title)

    if not items:
        st.warning("No eligible contracts")
        return

    rows = []

    for item in items:
        rows.append({
            "Strike": f'{item["strike"]:g} {item["side"]}',
            "Score": f'{item["score"]}/7',
            "LTP": round(item["ltp"], 2),
            "Delta": item["delta"],
            "IV": item["iv"],
            "Spread %": item["spread"],
            "Entry*": round(item["entry"], 2),
            "SL*": round(item["sl"], 2),
            "Target*": round(item["target"], 2)
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True
    )

    best = items[0]

    st.write(
        f'**Best candidate:** '
        f'{best["strike"]:g} {best["side"]} '
        f'| Score {best["score"]}/7'
    )

    st.write(
        f'Spot change: {fmt_change(best["spot_change"])} '
        f'| Premium change: '
        f'{fmt_change(best["premium_change"])} '
        f'| Live OI change: '
        f'{fmt_change(best["live_oi_change"])}'
    )

    with st.expander("Why this score?"):
        for reason in best["reasons"]:
            st.success(reason)

        for failure in best["failures"]:
            st.warning(failure)


st.title("Upstox F&O | Directional Signal Dashboard")

st.caption(
    "One final direction | CE / PE / NO TRADE | "
    "Research only | No automatic orders"
)

with st.sidebar:

    st.header("Connection")

    token = st.text_input(
        "Upstox Algo Trading Access Token",
        value=os.environ.get("UPSTOX_ACCESS_TOKEN", ""),
        type="password"
    )

    underlying = st.selectbox(
        "Underlying",
        list(KEYS.keys())
    )

    instrument_key = KEYS[underlying]

    expiry = st.text_input(
        "Expiry (YYYY-MM-DD)",
        value="2026-11-23"
    )

    st.header("Signal filters")

    max_spread = st.slider(
        "Maximum bid/ask spread %",
        0.5, 10.0, 3.0, 0.5
    )

    max_iv = st.slider(
        "Maximum IV (%)",
        10, 100, 40
    )

    min_volume = st.number_input(
        "Minimum traded volume",
        min_value=0,
        value=10000,
        step=1000
    )

    min_score = st.slider(
        "Minimum BUY score",
        4, 7, 6
    )

    rr = st.slider(
        "Reward / Risk",
        1.0, 3.0, 1.75, 0.25
    )

    auto = st.toggle(
        "Auto-refresh every 30 seconds",
        value=True
    )

    st.caption(
        "Access token is used for your API requests. "
        "Do not share it publicly."
    )


if "previous" not in st.session_state:
    st.session_state.previous = {}

if "previous_spot" not in st.session_state:
    st.session_state.previous_spot = None

if "previous_time" not in st.session_state:
    st.session_state.previous_time = None

if "last_identity" not in st.session_state:
    st.session_state.last_identity = None

if "force_refresh" not in st.session_state:
    st.session_state.force_refresh = False


@st.fragment(run_every="30s" if auto else None)
def dashboard():

    if st.button("Fetch / Refresh Now", type="primary"):
        st.session_state.force_refresh = True

    if not token:
        st.info("Enter Upstox Access Token in sidebar.")
        return

    identity = instrument_key + ":" + expiry.strip()

    if identity != st.session_state.last_identity:
        st.session_state.previous = {}
        st.session_state.previous_spot = None
        st.session_state.previous_time = None
        st.session_state.last_identity = identity

    if not auto and not st.session_state.force_refresh:
        st.info("Click Fetch / Refresh Now.")
        return

    st.session_state.force_refresh = False

    try:
        raw = api_get(
            "/option/chain",
            token,
            {
                "instrument_key": instrument_key,
                "expiry_date": expiry.strip()
            }
        )

        df = unpack_chain(raw)

    except Exception as exc:
        st.error(f"API fetch failed: {exc}")
        return

    if df.empty:
        st.warning(
            "No option-chain data returned. "
            "Check expiry and contract availability."
        )
        return

    now = datetime.now(IST)
    open_hours = market_is_open(now)

    spot = number(df["spot"].iloc[0])

    ce_oi = df[df["side"] == "CE"]["oi"].sum()
    pe_oi = df[df["side"] == "PE"]["oi"].sum()

    pcr = pe_oi / ce_oi if ce_oi > 0 else 0

    st.caption(
        f'Last fetch: {now:%d %b %Y %I:%M:%S %p} IST'
    )

    if not open_hours:
        st.warning(
            "Outside normal trading hours. "
            "BUY disabled; quotes may be stale."
        )

    # Only compare recent snapshots.
    previous = st.session_state.previous
    previous_spot = st.session_state.previous_spot
    previous_time = st.session_state.previous_time

    fresh_comparison = False

    if (
        open_hours
        and previous_time is not None
        and previous_spot is not None
    ):
        elapsed = (now - previous_time).total_seconds()

        fresh_comparison = 25 <= elapsed <= 90

    if fresh_comparison:
        spot_change = spot - previous_spot
    else:
        spot_change = None
        previous = {}

    a, b, c, d = st.columns(4)

    a.metric("NIFTY / Index Spot", f"{spot:,.2f}")
    b.metric("Put/Call OI Ratio", f"{pcr:.2f}")
    c.metric("CE Total OI", f"{ce_oi:,.0f}")
    d.metric("PE Total OI", f"{pe_oi:,.0f}")

    ce_items = analyze_side(
        df, "CE", previous, spot_change,
        max_spread, max_iv, min_volume, rr
    )

    pe_items = analyze_side(
        df, "PE", previous, spot_change,
        max_spread, max_iv, min_volume, rr
    )

    # Use the best eligible candidate from each side,
    # not just the highest-ranked row.
    def eligible(items):
        return [
            item for item in items
            if item["score"] >= min_score
            and item["direction_ok"]
            and item["oi_live_ok"]
            and item["premium_change"] is not None
            and item["premium_change"] > 0
        ]

    ce_eligible = eligible(ce_items)
    pe_eligible = eligible(pe_items)

    ce_best = ce_eligible[0] if ce_eligible else None
    pe_best = pe_eligible[0] if pe_eligible else None

    st.divider()
    st.subheader("FINAL TRADING DECISION")

    final_signal = "NO TRADE"
    selected = None

    if not open_hours:
        reason = "Market is closed."

    elif not fresh_comparison:
        reason = (
            "Waiting for two recent market-hours "
            "snapshots approximately 30 seconds apart."
        )

    elif spot_change == 0:
        reason = "No observed spot movement."

    elif ce_best and pe_best:
        reason = "Both sides qualified; conflicting signals."

    elif ce_best:
        final_signal = "CALL BUY CANDIDATE"
        selected = ce_best
        reason = "Spot and CE premium rising with OI confirmation."

    elif pe_best:
        final_signal = "PUT BUY CANDIDATE"
        selected = pe_best
        reason = "Spot falling and PE premium rising with OI confirmation."

    else:
        reason = "Directional confirmation insufficient."

    if selected:
        st.success(final_signal)

        st.metric(
            "Selected Contract",
            f'{selected["strike"]:g} {selected["side"]}'
        )

        st.metric(
            "Confirmation Score",
            f'{selected["score"]}/7'
        )

        x, y, z = st.columns(3)

        x.metric(
            "Indicative Entry",
            f'₹{selected["entry"]:.2f}'
        )

        y.metric(
            "Illustrative SL",
            f'₹{selected["sl"]:.2f}'
        )

        z.metric(
            "Illustrative Target",
            f'₹{selected["target"]:.2f}'
        )

    else:
        st.info("NO TRADE")

    st.write("**Reason:**", reason)

    st.caption(
        "These are candidate signals, not validated "
        "trade recommendations. Entry/SL/Target are "
        "illustrative and not based on backtesting."
    )

    st.divider()
    st.subheader("Top 3 CE / PE Candidates")

    left, right = st.columns(2)

    with left:
        display_candidates("CALL (CE)", ce_items)

    with right:
        display_candidates("PUT (PE)", pe_items)

    st.caption(
        "Entry is indicative ask. SL uses 12% "
        "premium risk and target uses selected R:R."
    )

    st.divider()
    st.subheader("Full Option Chain + Greeks")

    columns = [
        "strike", "side", "ltp", "oi", "prev_oi",
        "oi_change_day", "volume", "bid", "ask",
        "spread_pct", "iv", "delta",
        "gamma", "theta", "vega"
    ]

    view = df[columns].sort_values(
        ["strike", "side"]
    )

    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        height=450
    )

    st.download_button(
        "Download Option Chain CSV",
        view.to_csv(index=False),
        file_name="upstox_option_chain.csv",
        mime="text/csv"
    )

    st.warning(
        "Research-only heuristic. OI increase alone "
        "does not prove buying or writing. "
        "This dashboard does not check exchange holidays "
        "or confirm the trend using 5-minute candles."
    )

    # Save current snapshot for the next refresh.
    if open_hours:
        st.session_state.previous = {
            row["instrument"]: {
                "ltp": row["ltp"],
                "oi": row["oi"]
            }
            for _, row in df.iterrows()
            if row["instrument"] and row["ltp"] > 0
        }

        st.session_state.previous_spot = spot
        st.session_state.previous_time = now

    else:
        st.session_state.previous = {}
        st.session_state.previous_spot = None
        st.session_state.previous_time = None


dashboard()

st.caption(
    "Source: Upstox Option Chain API v2. "
    "Independent research dashboard. "
    "No automatic broker orders."
)
