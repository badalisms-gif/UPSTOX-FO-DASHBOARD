
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Upstox F&O Dashboard V3", page_icon="📊", layout="wide")
IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com/v2"


def num(value, default=None):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def api_get(path, token, params):
    response = requests.get(
        BASE + path,
        params=params,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Upstox HTTP {response.status_code}: {response.text[:250]}"
        )
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(
            str(payload.get("errors") or payload.get("message") or "API error")
        )
    return payload.get("data") or []


def unpack(chain):
    rows = []
    for item in chain:
        strike = num(item.get("strike_price"), 0)
        spot = num(item.get("underlying_spot_price"), 0)

        for side, key in [
            ("CE", "call_options"),
            ("PE", "put_options"),
        ]:
            option = item.get(key) or {}
            md = option.get("market_data") or {}
            gr = option.get("option_greeks") or {}

            bid = num(md.get("bid_price"), 0)
            ask = num(md.get("ask_price"), 0)
            oi = num(md.get("oi"), 0)
            prev_oi = num(md.get("prev_oi"), 0)

            rows.append({
                "strike": strike,
                "spot": spot,
                "side": side,
                "instrument": option.get("instrument_key") or "",
                "ltp": num(md.get("ltp"), 0),
                "oi": oi,
                "prev_oi": prev_oi,
                "oi_change_close": oi - prev_oi,
                "volume": num(md.get("volume"), 0),
                "bid": bid,
                "ask": ask,
                "spread_pct": (
                    100 * (ask - bid) / ((ask + bid) / 2)
                    if ask >= bid > 0 else None
                ),
                "iv": num(gr.get("iv")),
                "delta": num(gr.get("delta")),
                "gamma": num(gr.get("gamma")),
                "theta": num(gr.get("theta")),
                "vega": num(gr.get("vega")),
            })

    return pd.DataFrame(rows)


def market_open(now):
    return (
        now.weekday() < 5
        and time(9, 15) <= now.time() <= time(15, 30)
    )


def analyze_side(
    df, side, previous, max_spread, max_iv,
    min_vol, min_score, steps, spot_threshold,
    rr, in_hours, comparable
):
    spot = float(df.spot.iloc[0])
    strikes = sorted(df.strike.unique())
    step = min(
        (b - a for a, b in zip(strikes, strikes[1:]) if b > a),
        default=50,
    )

    subset = df[
        (df.side == side)
        & (abs(df.strike - spot) <= steps * step)
    ]

    prior_spot = previous.get("spot") if comparable else None
    prior_rows = previous.get("rows", {}) if comparable else {}
    spot_move = spot - prior_spot if prior_spot is not None else None

    opposite = "PE" if side == "CE" else "CE"
    wall = df[df.side == opposite]

    if side == "CE":
        wall = wall[wall.strike <= spot]
    else:
        wall = wall[wall.strike >= spot]

    wall = wall[abs(wall.strike - spot) <= 2 * step]
    wall_change = float(wall.oi_change_close.sum()) if not wall.empty else 0

    results = []

    for _, r in subset.iterrows():
        old = prior_rows.get(r.instrument) if r.instrument else None
        paired = old is not None and spot_move is not None

        premium_move = r.ltp - old["ltp"] if paired else None
        oi_move = r.oi - old["oi"] if paired else None
        vol_move = r.volume - old["volume"] if paired else None

        checks = [
            (
                "Spot direction",
                spot_move is not None and (
                    spot_move >= spot_threshold
                    if side == "CE"
                    else spot_move <= -spot_threshold
                ),
            ),
            (
                "Premium rising",
                premium_move is not None and premium_move > 0,
            ),
            (
                "OI rising",
                oi_move is not None and oi_move > 0,
            ),
            (
                "New traded volume",
                vol_move is not None and vol_move > 0,
            ),
            (
                "Opposite-side OI buildup vs previous close",
                wall_change > 0,
            ),
            (
                "Delta 0.35 to 0.75",
                r.delta is not None
                and 0.35 <= abs(r.delta) <= 0.75,
            ),
            (
                "IV below ceiling",
                r.iv is not None and 0 < r.iv <= max_iv,
            ),
        ]

        score = sum(bool(passed) for _, passed in checks)

        liquid = (
            r.ltp > 0
            and r.volume >= min_vol
            and r.spread_pct is not None
            and 0 <= r.spread_pct <= max_spread
            and r.ask > 0
            and r.bid > 0
        )

        ready = bool(
            in_hours
            and paired
            and liquid
            and all(ok for _, ok in checks[:4])
            and score >= min_score
        )

        entry = r.ask if r.ask > 0 else None
        risk = max(entry * 0.12, 1.0) if entry is not None else None

        results.append({
            "row": r,
            "score": score,
            "checks": checks,
            "ready": ready,
            "liquid": liquid,
            "spot_move": spot_move,
            "premium_move": premium_move,
            "oi_move": oi_move,
            "vol_move": vol_move,
            "entry": entry,
            "sl": max(0.05, entry - risk) if entry is not None else None,
            "target": entry + risk * rr if entry is not None else None,
            "distance": abs(r.strike - spot),
        })

    return sorted(
        results,
        key=lambda x: (
            x["ready"],
            x["score"],
            -x["distance"],
        ),
        reverse=True,
    )


def display_change(value):
    return f"{value:+,.2f}" if value is not None else "N/A"


st.title("Upstox F&O | Option Chain + Greeks Dashboard V3")
st.caption(
    "Top 3 CE and PE candidates | Research-only | No automatic orders"
)

with st.sidebar:
    st.header("Connection")

    token = st.text_input(
        "Upstox Algo Trading Access Token",
        value=os.environ.get("UPSTOX_ACCESS_TOKEN", ""),
        type="password",
    )

    underlying = st.selectbox(
        "Underlying",
        ["NIFTY 50", "BANK NIFTY", "FIN NIFTY"],
    )

    keys = {
        "NIFTY 50": "NSE_INDEX|Nifty 50",
        "BANK NIFTY": "NSE_INDEX|Nifty Bank",
        "FIN NIFTY": "NSE_INDEX|Nifty Fin Service",
    }

    instrument_key = keys[underlying]

    expiry = st.text_input(
        "Expiry (YYYY-MM-DD)",
        "2026-11-23",
    ).strip()

    st.header("Signal filters")

    max_spread = st.slider(
        "Maximum bid/ask spread %",
        0.5, 10.0, 3.0, 0.5,
    )

    max_iv = st.slider(
        "Maximum IV (%)",
        10, 100, 40,
    )

    min_vol = st.number_input(
        "Minimum cumulative traded volume",
        min_value=0,
        value=10000,
        step=1000,
    )

    min_score = st.slider(
        "Minimum BUY score (out of 7)",
        4, 7, 6,
    )

    steps = st.slider(
        "Strikes to scan on each side of ATM",
        1, 10, 3,
    )

    spot_threshold = st.number_input(
        "Minimum spot movement between snapshots (points)",
        min_value=0.0,
        value=2.0,
        step=0.5,
    )

    rr = st.slider(
        "Illustrative reward/risk",
        1.0, 3.0, 1.5, 0.25,
    )

    auto = st.toggle(
        "Auto-refresh every 30 seconds",
        value=False,
    )

    st.caption(
        "Keep this page open for refreshes. Respect API limits."
    )


if "previous" not in st.session_state:
    st.session_state.previous = None

if "identity" not in st.session_state:
    st.session_state.identity = ""


@st.fragment(run_every="30s" if auto else None)
def dashboard():
    manual = st.button(
        "Fetch / Refresh Now",
        type="primary",
    )

    if not token:
        st.info("Enter your Upstox Access Token in the sidebar.")
        return

    try:
        datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        st.error("Enter a listed expiry in YYYY-MM-DD format.")
        return

    identity = f"{instrument_key}|{expiry}"

    if identity != st.session_state.identity:
        st.session_state.identity = identity
        st.session_state.previous = None

    if not (manual or auto):
        st.info("Click Fetch / Refresh Now or enable auto-refresh.")
        return

    try:
        df = unpack(
            api_get(
                "/option/chain",
                token,
                {
                    "instrument_key": instrument_key,
                    "expiry_date": expiry,
                },
            )
        )
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        st.error(f"Upstox API error: {exc}")
        return

    if df.empty:
        st.warning(
            "No option-chain data. Check expiry and contract availability."
        )
        return

    now = datetime.now(IST)
    in_hours = market_open(now)
    spot = float(df.spot.iloc[0])

    ce_oi = df[df.side == "CE"].oi.sum()
    pe_oi = df[df.side == "PE"].oi.sum()
    pcr = pe_oi / ce_oi if ce_oi else None

    previous = st.session_state.previous

    comparable = bool(
        previous
        and previous.get("time")
        and in_hours
        and previous["time"].date() == now.date()
        and 0 < (now - previous["time"]).total_seconds() <= 120
    )

    st.caption(
        f"Last fetch: {now:%d %b %Y %I:%M:%S %p} IST"
    )

    if not in_hours:
        st.warning(
            "Outside normal trading hours. BUY disabled; quotes may be stale."
        )

    if not comparable:
        st.info(
            "Waiting for two recent market-hours snapshots to compare changes."
        )

    a, b, c, d = st.columns(4)

    a.metric("Underlying spot", f"{spot:,.2f}")
    b.metric(
        "Put/Call OI ratio",
        f"{pcr:.2f}" if pcr is not None else "N/A",
    )
    c.metric("CE OI (all strikes)", f"{ce_oi:,.0f}")
    d.metric("PE OI (all strikes)", f"{pe_oi:,.0f}")

    ce = analyze_side(
        df, "CE", previous, max_spread, max_iv,
        min_vol, min_score, steps, spot_threshold,
        rr, in_hours, comparable,
    )

    pe = analyze_side(
        df, "PE", previous, max_spread, max_iv,
        min_vol, min_score, steps, spot_threshold,
        rr, in_hours, comparable,
    )

    conflict = bool(
        ce and pe and ce[0]["ready"] and pe[0]["ready"]
    )

    if conflict:
        st.warning(
            "Conflicting CE and PE confirmations: BUY blocked for both sides."
        )

    st.subheader("Top 3 CE / PE candidates")

    left, right = st.columns(2)

    for col, side, candidates in [
        (left, "CALL (CE)", ce),
        (right, "PUT (PE)", pe),
    ]:
        with col:
            st.markdown(f"### {side}")

            if not candidates:
                st.warning(
                    "No candidates in selected strike range."
                )
                continue

            rows = []

            for rank, item in enumerate(candidates[:3], 1):
                r = item["row"]

                status = (
                    "BUY CANDIDATE"
                    if item["ready"] and not conflict
                    else "WAIT"
                )

                rows.append({
                    "Rank": rank,
                    "Strike": f"{r.strike:g} {r.side}",
                    "Status": status,
                    "Score": f"{item['score']}/7",
                    "LTP": round(r.ltp, 2),
                    "OI change (refresh)": item["oi_move"],
                    "Premium change": item["premium_move"],
                    "Delta": r.delta,
                    "IV %": r.iv,
                    "Spread %": (
                        round(r.spread_pct, 2)
                        if r.spread_pct is not None
                        else None
                    ),
                    "Entry*": (
                        round(item["entry"], 2)
                        if item["entry"] is not None
                        else None
                    ),
                    "SL*": (
                        round(item["sl"], 2)
                        if item["sl"] is not None
                        else None
                    ),
                    "Target*": (
                        round(item["target"], 2)
                        if item["target"] is not None
                        else None
                    ),
                })

            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )

            best = candidates[0]
            r = best["row"]

            status = (
                "BUY CANDIDATE"
                if best["ready"] and not conflict
                else "WAIT"
            )

            st.write(
                f"**Best candidate:** {r.strike:g} {r.side} | "
                f"Score {best['score']}/7 | {status}"
            )

            st.write(
                "Spot change: "
                + display_change(best["spot_move"])
                + " | Premium change: "
                + display_change(best["premium_move"])
                + " | OI change: "
                + display_change(best["oi_move"])
            )

            with st.expander("Why this score?"):
                for label, ok in best["checks"]:
                    st.write(
                        ("✅ " if ok else "❌ ") + label
                    )

                st.write(
                    "Liquidity gate: "
                    + ("PASS" if best["liquid"] else "FAIL")
                )

    st.caption(
        "* Entry is indicative ask; SL uses 12% premium risk "
        "and target uses selected reward/risk. "
        "These values are illustrative even for WAIT rows."
    )

    st.warning(
        "Snapshot-based heuristic only; not a backtested "
        "trading system. OI increases cannot distinguish "
        "buying from writing on their own. Previous-close "
        "OI buildup is not live directional confirmation."
    )

    st.subheader("Full option chain and Greeks")

    cols = [
        "strike", "side", "ltp", "oi", "prev_oi",
        "oi_change_close", "volume", "bid", "ask",
        "spread_pct", "iv", "delta", "gamma",
        "theta", "vega",
    ]

    view = df[cols].sort_values(["strike", "side"])

    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        height=430,
    )

    st.download_button(
        "Download option chain CSV",
        view.to_csv(index=False),
        file_name="upstox_option_chain.csv",
        mime="text/csv",
    )

    st.session_state.previous = {
        "time": now,
        "spot": spot,
        "rows": {
            r.instrument: {
                "ltp": r.ltp,
                "oi": r.oi,
                "volume": r.volume,
            }
            for _, r in df.iterrows()
            if r.instrument and r.ltp > 0
        },
    }


dashboard()

st.caption(
    "Source: Upstox Option Chain API v2 | "
    "No automatic order placement."
)
