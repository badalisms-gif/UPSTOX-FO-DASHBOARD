
import json
import time
import threading
from datetime import datetime

import pandas as pd
import streamlit as st
import upstox_client

from market_data_engine import (
    IST,
    NSEMarketData,
    get_fo_universe,
    parse_time,
    number,
    is_fresh,
    validate_nse_stock,
    upstox_market_status,
    render_market_data_engine
)

st.set_page_config(
    page_title="NSE F&O PRO V8.3",
    page_icon="📊",
    layout="wide"
)

VERSION = "8.3"


def now():
    return datetime.now(IST)


class LiveMarketEngine:

    def __init__(self, token, universe):
        self.token = token
        self.universe = universe
        self.keys = list(universe.values())

        self.cache = {}
        self.nse_cache = {}
        self.errors = []

        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        self.connected = False
        self.market_open = False
        self.streamer = None
        self.worker = None
        self.last_message = None

    def error(self, message):
        with self.lock:
            self.errors.append({
                "Time": now().isoformat(),
                "Error": str(message).replace(
                    self.token, "[REDACTED]"
                )
            })
            self.errors = self.errors[-50:]

    def on_open(self, *args):
        with self.lock:
            self.connected = True
            self.cache.clear()
            self.last_message = None

    def on_close(self, *args):
        with self.lock:
            self.connected = False
            self.cache.clear()

    def on_error(self, error, *args):
        self.error(error)

    def on_message(self, message):

        if self.stop_event.is_set():
            return

        try:
            if isinstance(message, str):
                message = json.loads(message)

            if not isinstance(message, dict):
                return

            feeds = message.get("feeds", {})
            feed_time = message.get("currentTs")

            if not feeds:
                return

            received = now()

            with self.lock:

                self.last_message = received

                for key, feed in feeds.items():

                    full = feed.get("fullFeed", {})
                    market = full.get("marketFF", {})

                    ltpc = (
                        market.get("ltpc")
                        or feed.get("ltpc")
                        or {}
                    )

                    if not ltpc:
                        continue

                    price = number(ltpc.get("ltp"))
                    previous = number(ltpc.get("cp"))
                    trade_time = ltpc.get("ltt")

                    if (
                        price is None
                        or price <= 0
                        or previous is None
                        or previous <= 0
                    ):
                        continue

                    old = self.cache.get(key, {})

                    old_time = parse_time(
                        old.get("Trade Time")
                    )

                    new_time = parse_time(trade_time)

                    if new_time is None:
                        continue

                    if (
                        old_time is not None
                        and new_time < old_time
                    ):
                        continue

                    daily = {}

                    candles = (
                        market.get("marketOHLC", {})
                        .get("ohlc", [])
                    )

                    for candle in candles:
                        if candle.get("interval") == "1d":
                            daily = candle
                            break

                    op = number(daily.get("open"))
                    high = number(daily.get("high"))
                    low = number(daily.get("low"))

                    position = None

                    if (
                        high is not None
                        and low is not None
                        and high > low
                        and low <= price <= high
                    ):
                        position = (
                            100 * (price - low)
                            / (high - low)
                        )

                    self.cache[key] = {
                        "LTP": price,
                        "Previous Close": previous,
                        "Change %": (
                            100 * (price / previous - 1)
                        ),
                        "Open": op,
                        "High": high,
                        "Low": low,
                        "Volume": number(
                            market.get("vtt")
                        ),
                        "Range Position": position,
                        "Trade Time": trade_time,
                        "Feed Time": feed_time,
                        "Received IST": received.isoformat()
                    }

        except Exception as e:
            self.error(e)

    def connect(self):

        configuration = upstox_client.Configuration()
        configuration.access_token = self.token

        api_client = upstox_client.ApiClient(
            configuration
        )

        self.streamer = (
            upstox_client.MarketDataStreamerV3(
                api_client,
                self.keys,
                "full"
            )
        )

        self.streamer.on("open", self.on_open)
        self.streamer.on("message", self.on_message)
        self.streamer.on("close", self.on_close)
        self.streamer.on("error", self.on_error)

        self.streamer.auto_reconnect(
            True, 5, 10
        )

        self.streamer.connect()

    def verify_symbol(self, client, symbol):

        key = self.universe[symbol]

        try:
            response = client.stock_quote(symbol)
            stock = response.get("stock", {})

            if stock.get("symbol") != symbol:
                raise RuntimeError("NSE symbol mismatch")

            reference = now()

            result = validate_nse_stock(
                stock, 180, reference
            )

            with self.lock:
                quote = dict(self.cache.get(key, {}))

            reasons = []

            if not result["NSE Valid"]:
                reasons.append(result["Errors"])

            nse_price = number(result.get("NSE LTP"))
            live_price = number(quote.get("LTP"))

            nse_time = parse_time(
                result.get("NSE Timestamp")
            )

            trade_time = parse_time(
                quote.get("Trade Time")
            )

            if not is_fresh(
                quote.get("Trade Time"), 15, reference
            ):
                reasons.append("STALE LIVE TRADE")

            if not is_fresh(
                quote.get("Feed Time"), 15, reference
            ):
                reasons.append("STALE FEED")

            gap = None

            if nse_time is None or trade_time is None:
                reasons.append("TIMESTAMP UNAVAILABLE")
            else:
                gap = abs(
                    (nse_time - trade_time).total_seconds()
                )

                if gap > 90:
                    reasons.append("SOURCE TIME GAP")

            difference = None

            if (
                nse_price is None
                or nse_price <= 0
                or live_price is None
                or live_price <= 0
            ):
                reasons.append("PRICE UNAVAILABLE")
            else:
                difference = (
                    100 * abs(nse_price - live_price)
                    / nse_price
                )

                if difference > 0.35:
                    reasons.append("PRICE MISMATCH")

            nse_previous = number(
                result.get("Previous Close")
            )

            live_previous = number(
                quote.get("Previous Close")
            )

            if (
                nse_previous is None
                or nse_previous <= 0
                or live_previous is None
                or live_previous <= 0
            ):
                reasons.append("PREVIOUS CLOSE UNAVAILABLE")

            elif (
                100 * abs(nse_previous - live_previous)
                / nse_previous > 0.10
            ):
                reasons.append("PREVIOUS CLOSE MISMATCH")

            with self.lock:

                self.nse_cache[symbol] = {
                    "Verified": not reasons,
                    "Reasons": " | ".join(reasons),
                    "NSE Price": nse_price,
                    "NSE Timestamp": result.get(
                        "NSE Timestamp"
                    ),
                    "Verified Live Price": live_price,
                    "Verified Trade Time": quote.get(
                        "Trade Time"
                    ),
                    "Verified IST": reference.isoformat(),
                    "Price Difference %": difference,
                    "Source Gap Seconds": gap
                }

        except Exception as e:

            with self.lock:
                self.nse_cache[symbol] = {
                    "Verified": False,
                    "Reasons": "NSE REQUEST FAILED"
                }

            self.error(e)

    def verification_loop(self):

        client = NSEMarketData()
        symbols = sorted(self.universe)

        try:
            while not self.stop_event.is_set():

                try:
                    status = upstox_market_status(
                        self.token
                    )

                    self.market_open = (
                        str(status.get("status", "")).upper()
                        == "NORMAL_OPEN"
                    )

                except Exception as e:
                    self.market_open = False
                    self.error(e)

                if not self.market_open:
                    self.stop_event.wait(30)
                    continue

                for symbol in symbols:

                    if self.stop_event.is_set():
                        break

                    if not self.connected:
                        break

                    self.verify_symbol(client, symbol)

                    if self.stop_event.wait(0.25):
                        break

                # Retry failed symbols after each full pass.

                with self.lock:
                    failed = [
                        s for s in symbols
                        if not self.nse_cache.get(
                            s, {}
                        ).get("Verified", False)
                    ]

                if self.stop_event.wait(10):
                    break

                for symbol in failed:

                    if (
                        self.stop_event.is_set()
                        or not self.connected
                    ):
                        break

                    self.verify_symbol(client, symbol)

                    if self.stop_event.wait(0.25):
                        break

                self.stop_event.wait(5)

        finally:
            client.close()

    def start(self):

        self.connect()

        self.worker = threading.Thread(
            target=self.verification_loop,
            daemon=True
        )

        self.worker.start()

    def stop(self):

        self.stop_event.set()

        with self.lock:
            self.connected = False
            self.market_open = False
            self.cache.clear()

        if self.streamer is not None:
            try:
                self.streamer.disconnect()
            except Exception as e:
                self.error(e)

    def snapshot(self):

        reference = now()

        with self.lock:

            live = {
                k: dict(v)
                for k, v in self.cache.items()
            }

            verified = {
                k: dict(v)
                for k, v in self.nse_cache.items()
            }

            connected = self.connected
            market_open = self.market_open

        rows = []

        for symbol, key in self.universe.items():

            q = live.get(key, {})
            v = verified.get(symbol, {})

            price = number(q.get("LTP"))
            change = number(q.get("Change %"))
            position = number(q.get("Range Position"))
            op = number(q.get("Open"))

            fresh = (
                connected
                and market_open
                and is_fresh(
                    q.get("Trade Time"), 15, reference
                )
                and is_fresh(
                    q.get("Feed Time"), 15, reference
                )
                and is_fresh(
                    q.get("Received IST"), 15, reference
                )
            )

            independent_valid = (
                v.get("Verified", False)
                and is_fresh(
                    v.get("Verified IST"), 90, reference
                )
                and is_fresh(
                    v.get("NSE Timestamp"), 180, reference
                )
            )

            nse_price = number(v.get("NSE Price"))

            current_difference = None
            current_gap = None

            if (
                price is not None
                and nse_price is not None
                and nse_price > 0
            ):
                current_difference = (
                    100 * abs(price - nse_price)
                    / nse_price
                )

            nt = parse_time(v.get("NSE Timestamp"))
            ut = parse_time(q.get("Trade Time"))

            if nt is not None and ut is not None:
                current_gap = abs(
                    (nt - ut).total_seconds()
                )

            eligible = (
                fresh
                and independent_valid
                and current_difference is not None
                and current_difference <= 0.35
                and current_gap is not None
                and current_gap <= 90
            )

            direction = "NEUTRAL"
            score = 0.0

            if eligible and all(
                x is not None
                for x in [
                    price, change, position, op
                ]
            ):

                if (
                    change >= 0.35
                    and position >= 60
                    and price > op
                ):
                    direction = "CE WATCH"

                elif (
                    change <= -0.35
                    and position <= 40
                    and price < op
                ):
                    direction = "PE WATCH"

                if direction != "NEUTRAL":

                    strength = min(
                        abs(change) / 2.5, 1
                    )

                    directional_position = (
                        position
                        if direction == "CE WATCH"
                        else 100 - position
                    )

                    score = round(
                        strength * 60
                        + directional_position * 0.40,
                        2
                    )

            rows.append({
                "Symbol": symbol,
                "LTP": price,
                "Previous Close": q.get(
                    "Previous Close"
                ),
                "Change %": change,
                "Volume": q.get("Volume"),
                "Live Fresh": fresh,
                "NSE Verification Valid": independent_valid,
                "Currently Cross Verified": eligible,
                "Direction": direction,
                "Momentum Score": score,
                "Price Difference %": current_difference,
                "Source Gap Seconds": current_gap,
                "Trade Time": q.get("Trade Time"),
                "Feed Time": q.get("Feed Time"),
                "NSE Verified IST": v.get("Verified IST"),
                "Last Verification Reasons": v.get(
                    "Reasons", "PENDING"
                )
            })

        return pd.DataFrame(rows)


# ==========================================
# STREAMLIT DASHBOARD
# ==========================================

st.title("NSE F&O PRO V8.3 — REAL-TIME ENGINE")

st.caption(
    "Upstox WebSocket V3 | NSE Independent Verification"
)

page = st.sidebar.radio(
    "Engine Mode",
    ["V8.3 Real-Time Scanner", "V8.2 Audit Engine"]
)

if page == "V8.2 Audit Engine":

    render_market_data_engine()
    st.stop()

token = st.sidebar.text_input(
    "Upstox Access Token",
    type="password"
).strip()

try:
    universe = get_fo_universe()
except Exception as e:
    st.error(str(e))
    st.stop()

st.metric("Total F&O Universe", len(universe))

a, b = st.columns(2)

if a.button("START LIVE ENGINE", type="primary"):

    if not token:
        st.error("Enter Upstox Access Token")

    else:
        old = st.session_state.pop(
            "v83_engine", None
        )

        if old is not None:
            old.stop()

        engine = LiveMarketEngine(
            token, universe
        )

        try:
            engine.start()
            st.session_state["v83_engine"] = engine

        except Exception as e:
            engine.stop()
            st.error(
                str(e).replace(token, "[REDACTED]")
            )

if b.button("STOP LIVE ENGINE"):

    engine = st.session_state.pop(
        "v83_engine", None
    )

    if engine is not None:
        engine.stop()

    st.success("Live engine stopped")


@st.fragment(run_every="3s")
def live_dashboard():

    engine = st.session_state.get("v83_engine")

    if engine is None:
        st.info("Start the live engine")
        return

    frame = engine.snapshot()

    total = len(frame)

    fresh = int(
        frame["Live Fresh"].sum()
    )

    verified = int(
        frame["Currently Cross Verified"].sum()
    )

    if engine.connected:
        st.success("UPSTOX WEBSOCKET CONNECTED")
    else:
        st.warning("WEBSOCKET DISCONNECTED")

    st.caption(
        "Market Status: "
        + (
            "NORMAL OPEN"
            if engine.market_open
            else "NOT CONFIRMED OPEN"
        )
    )

    a, b, c = st.columns(3)

    a.metric(
        "Live Feed Received",
        int(frame["LTP"].notna().sum())
    )

    b.metric(
        "Currently Fresh",
        fresh
    )

    c.metric(
        "Currently Cross Verified",
        verified
    )

    a, b = st.columns(2)

    a.metric(
        "Fresh Coverage",
        f"{100 * fresh / total:.2f}%"
    )

    b.metric(
        "Cross Verified Coverage",
        f"{100 * verified / total:.2f}%"
    )

    ranked = frame.sort_values(
        "Momentum Score",
        ascending=False
    )

    ce = ranked[
        ranked["Direction"] == "CE WATCH"
    ]

    pe = ranked[
        ranked["Direction"] == "PE WATCH"
    ]

    st.subheader("Final Qualified Calls")

    st.info(
        "No final option entry calls from this module. "
        "Technical, option premium, Greeks, news and "
        "risk qualification are required."
    )

    columns = [
        "Symbol",
        "LTP",
        "Previous Close",
        "Change %",
        "Volume",
        "Momentum Score",
        "Price Difference %",
        "Source Gap Seconds"
    ]

    st.subheader("Live Verified CE Watchlist")

    st.dataframe(
        ce[columns].head(10),
        use_container_width=True
    )

    st.subheader("Live Verified PE Watchlist")

    st.dataframe(
        pe[columns].head(10),
        use_container_width=True
    )

    with st.expander("Complete Live Market Data"):

        st.dataframe(
            frame,
            use_container_width=True
        )

    with st.expander("Engine Errors"):

        with engine.lock:
            errors = list(engine.errors)

        st.dataframe(
            pd.DataFrame(errors),
            use_container_width=True
        )

    st.download_button(
        "DOWNLOAD LIVE VALIDATION CSV",
        frame.to_csv(index=False).encode("utf-8-sig"),
        "v83_live_validation.csv",
        "text/csv"
    )

    st.caption(
        "Updated IST: " + now().strftime(
            "%d-%m-%Y %H:%M:%S"
        )
    )


live_dashboard()

st.warning(
    "Research only. No automatic orders. "
    "Keep the Streamlit session active. "
    "Stop the engine before closing or changing tokens."
)
