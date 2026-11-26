
"""Integrated V7 + V8.4 Dashboard."""

from pathlib import Path
import streamlit as st


def integrate(source):

    def replace(old, new):
        nonlocal source

        if source.count(old) != 1:
            raise ValueError(
                "Incompatible V7 source: " + old[:100]
            )

        source = source.replace(old, new, 1)

    if (
        "from backtest_engine_v7 import render_backtesting_v7"
        not in source
    ):
        raise ValueError("V7 app_v2.py required")

    source = source.replace(
        "NSE F&O PRO — ALL IN ONE V7",
        "NSE F&O PRO — ALL IN ONE V8.4"
    )

    replace(
        "from news_engine import assess_news, scan_news_market",
        '''from news_engine import assess_news, scan_news_market
from market_data_engine_v84 import Engine as LiveEngine
from market_data_engine import get_fo_universe
import time as monotonic_clock'''
    )

    # =====================================
    # LIVE WEBSOCKET MARKET RANKING
    # =====================================

    start = source.index("def ranking(token):")

    end = source.index(
        "# =====================================\n"
        "# HISTORICAL AND LIVE CANDLES",
        start
    )

    source = source[:start] + '''def live_snapshot():
    e = S.get("integrated_live")

    if (
        e is None
        or e.token != token
        or e.halt.is_set()
    ):
        return pd.DataFrame()

    return e.snapshot()


def live_gate(symbol, side):
    d = live_snapshot()

    if d.empty or symbol in INDEXES:
        return False

    z = d[d.Symbol == symbol]

    if len(z) != 1:
        return False

    a = z.iloc[0]

    return bool(
        a["Live Fresh"]
        and a["Currently Cross Verified"]
        and a["Raw Direction"] == side
    )


def ranking(token):
    d = live_snapshot()
    e = S.get("integrated_live")

    total = (
        len(e.universe)
        if e else len(instruments())
    )

    if d.empty:
        return (
            pd.DataFrame(),
            total,
            0,
            ["Live engine not ready"]
        )

    received = int(d.LTP.notna().sum())

    d = d.copy()

    d["Key"] = d.Symbol.map(e.universe)
    d["Side"] = d["Raw Direction"]

    d["Fresh"] = (
        d["Live Fresh"]
        & d["Currently Cross Verified"]
    )

    d.loc[~d.Fresh, "Side"] = "NEUTRAL"

    return (
        d.sort_values(
            "Momentum Score",
            ascending=False
        ),
        total,
        received,
        list(e.errors)
    )


''' + source[end:]

    # =====================================
    # LIVE ENGINE SESSION MANAGEMENT
    # =====================================

    replace(
        "def budget():",
        '''old_engine = S.get("integrated_live")

if (
    old_engine is not None
    and old_engine.token != token
):
    old_engine.stop()

    S.pop("integrated_live", None)

    S.ranking = pd.DataFrame()
    S.results = []
    S.snapshots = {}
    S.chains = {}
    S.chain_times = {}

    for signal in S.signals.values():
        if signal["Status"] == "ACTIVE":
            signal["Status"] = (
                "TOKEN CHANGED / UNRESOLVED"
            )


with st.sidebar:

    st.header("V8.4 Live Data Engine")

    if st.button(
        "START LIVE ENGINE",
        key="integrated_start"
    ):

        old = S.pop("integrated_live", None)

        if old:
            old.stop()

        engine = None

        try:
            if not token:
                raise ValueError("Enter Upstox token")

            universe = get_fo_universe()

            if not universe:
                raise ValueError("Empty F&O universe")

            engine = LiveEngine(
                token,
                universe
            )

            engine.start()

            S.integrated_live = engine

        except Exception as exc:

            if engine:
                engine.stop()

            st.error(
                str(exc).replace(
                    token,
                    "[REDACTED]"
                ) if token else str(exc)
            )

    if st.button(
        "STOP LIVE ENGINE",
        key="integrated_stop"
    ):

        old = S.pop("integrated_live", None)

        if old:
            old.stop()

        S.ranking = pd.DataFrame()

    st.caption(
        "Keep session active. "
        "Index entries blocked until "
        "independently verified."
    )


def budget():'''
    )

    # =====================================
    # MANDATORY LIVE VERIFICATION
    # =====================================

    replace(
        '    safe = (\n'
        '        all(groups.values())',

        '''    safety["Live NSE cross verification"] = live_gate(
        symbol, o.Side
    )

    safety["Final option quote fresh"] = fresh(q, 15)

    safe = (
        all(groups.values())'''
    )

    # =====================================
    # INDEX ENTRY SAFETY
    # =====================================

    replace(
        '''    selected.extend({
        "Symbol": k,
        "Key": v,
        "Side": "AUTO",
        "Origin": "INDEX"
    } for k, v in INDEXES.items())''',

        '''    # Index entry candidates excluded:
    # Independent index verification required.'''
    )

    # =====================================
    # PRE-ANALYSIS VERIFICATION
    # =====================================

    replace(
        '''        try:
            side, checks, metrics = technical(''',

        '''        try:

            if not live_gate(sym, row["Side"]):
                raise RuntimeError(
                    "Live NSE verification expired "
                    "or direction changed"
                )

            side, checks, metrics = technical('''
    )

    # =====================================
    # FINAL SIGNAL VERIFICATION
    # =====================================

    replace(
        '''                    if v["Status"] == "QUALIFIED":''',

        '''                    if (
                        v["Status"] == "QUALIFIED"
                        and not live_gate(sym, side)
                    ):
                        v["Status"] = "WAIT"

                        v["Checks"][
                            "Live NSE cross verification"
                        ] = False

                    if v["Status"] == "QUALIFIED":'''
    )

    # =====================================
    # FRESH ENTRY VALIDATION
    # =====================================

    replace(
        '''                and recent(v["Last Quote Check"], 180)''',

        '''                and recent(
                    v["Last Quote Check"], 15
                )
                and live_gate(
                    v["Symbol"], v["Side"]
                )'''
    )

    replace(
        '''        if not fresh(q) or not np.isfinite(p):''',

        '''        if not fresh(q, 15) or not np.isfinite(p):'''
    )

    # =====================================
    # DASHBOARD AUTO REFRESH
    # =====================================

    replace(
        '''    run_every="120s"
    if auto or news_auto
    else None''',

        '''    run_every="10s"'''
    )

    # =====================================
    # LIVE MARKET DATA COVERAGE
    # =====================================

    replace(
        '''    st.caption("Active Mode: " + mode)''',

        '''    live = live_snapshot()

    engine = S.get("integrated_live")

    st.caption("Active Mode: " + mode)

    if not live.empty:

        p = live[live.Priority]

        a, b, c, d = st.columns(4)

        a.metric(
            "Live feed",
            int(live.LTP.notna().sum())
        )

        b.metric(
            "Fresh",
            int(live["Live Fresh"].sum())
        )

        c.metric(
            "Cross verified",
            int(
                live["Currently Cross Verified"].sum()
            )
        )

        d.metric(
            "Priority verified",
            f'{int(p["Currently Cross Verified"].sum())}/{len(p)}'
        )

        st.caption(
            "WebSocket: " + (
                "CONNECTED"
                if engine.connected
                else "DISCONNECTED"
            )
        )

        S.ranking, total, received, _ = ranking(token)

        S.coverage = f"{received}/{total}"

        S.fresh_coverage = int(
            live["Live Fresh"].sum()
        )

        with st.expander(
            "Live verification audit"
        ):

            st.dataframe(
                live,
                hide_index=True
            )

            st.write(
                list(engine.errors)
            )

            st.download_button(
                "Download live validation CSV",
                live.to_csv(index=False),
                "integrated_live_validation.csv"
            )

    else:

        S.ranking = pd.DataFrame()
        S.coverage = "—"
        S.fresh_coverage = 0

        st.warning(
            "Live engine stopped or awaiting data; "
            "no fresh entries."
        )'''
    )

    # =====================================
    # CONTROL TECHNICAL SCAN FREQUENCY
    # =====================================

    replace(
        '''    if token and (
        clicked or (auto and opened)
    ):''',

        '''    if token and opened and not live.empty and (
        clicked or (
            auto and (
                S.last_scan is None
                or not recent(
                    S.last_scan.isoformat(),
                    120
                )
            )
        )
    ):'''
    )

    # =====================================
    # VERIFIED CE AND PE WATCHLISTS
    # =====================================

    replace(
        '''                    r[r.Side == side]
                    .head(10)''',

        '''                    r[
                        (r.Side == side)
                        & r.Fresh
                    ]
                    .head(10)'''
    )

    return source


# =====================================
# LOAD ORIGINAL V7 DASHBOARD
# =====================================

try:

    original = Path(__file__).with_name(
        "app_v2.py"
    )

    source = original.read_text(
        encoding="utf-8"
    )

    integrated_source = integrate(source)

    code = compile(
        integrated_source,
        str(original),
        "exec"
    )

except Exception as exc:

    st.error(
        f"Integration stopped safely: {exc}"
    )

    st.stop()

else:

    exec(
        code,
        globals(),
        globals()
    )
