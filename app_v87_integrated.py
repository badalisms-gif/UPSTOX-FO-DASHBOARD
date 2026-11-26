
"""V8.7 Balanced Multi-Strategy F&O Research."""

import ast
import sys
import json

from pathlib import Path
from collections import Counter

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REVISION = "8.7"


# =====================================
# LOAD EXISTING V8.6
# =====================================

def load_v86():

    path = BASE_DIR / "app_v86_integrated.py"

    if not path.is_file():
        raise FileNotFoundError(path.name)

    tree = ast.parse(
        path.read_text(encoding="utf-8-sig")
    )

    nodes = []

    for node in tree.body:

        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef
            )
        ):
            nodes.append(node)

        elif isinstance(node, ast.Assign):

            names = [
                t.id for t in node.targets
                if isinstance(t, ast.Name)
            ]

            if "AUDIT_SOURCE" in names:
                nodes.append(node)

    module = ast.fix_missing_locations(
        ast.Module(
            body=nodes,
            type_ignores=[]
        )
    )

    ns = {
        "__file__": str(path),
        "BASE_DIR": BASE_DIR,
        "REVISION": REVISION
    }

    exec(
        compile(module, str(path), "exec"),
        ns
    )

    return ns


# =====================================
# V8.7 ENGINE
# =====================================

ENGINE = '''

with st.sidebar:

    st.divider()
    st.header("V8.7 Strategy Engine")

    v87_mode = st.radio(
        "Strategy Mode",
        [
            "Balanced V8.7",
            "Strict Technical"
        ],
        index=0,
        key="v87_mode"
    )

    v87_balanced = (
        v87_mode == "Balanced V8.7"
    )

    v87_score = st.slider(
        "Minimum Master Score",
        80, 95, 80,
        key="v87_score"
    )

    v87_minvol = st.number_input(
        "V8.7 Minimum Option Volume",
        min_value=100,
        value=5000,
        step=1000,
        key="v87_minvol"
    )

    v87_auto_snapshots = st.toggle(
        "Automatic Option Snapshots",
        value=True,
        key="v87_auto_snapshots"
    )

    v87_snapshot_count = st.slider(
        "Snapshot Stocks Per Batch",
        2, 8, 4,
        key="v87_snapshot_count"
    )

    st.info(
        "Manual Position Sizing. "
        "Capital and risk budget do not "
        "reject signals."
    )


if v87_balanced:
    minvol = v87_minvol


def v87_state():

    import hashlib

    identity = (
        now().date().isoformat(),
        hashlib.sha256(
            token.encode()
        ).hexdigest()
    )

    if S.get("v87_identity") != identity:

        S.v87_identity = identity
        S.v87_history = {}
        S.v87_cache = {}
        S.v87_errors = {}
        S.v87_cursor = 0
        S.v87_requests = 0
        S.v87_last = None
        S.v87_technical = []


v87_state()


# =====================================
# NO CAPITAL OR RISK BUDGET REJECTION
# =====================================

def budget():

    return float("inf")


# =====================================
# MULTI STRATEGY TECHNICAL ANALYSIS
# =====================================

v87_old_technical = technical


def technical(token, key):

    if not v87_balanced:

        return v87_old_technical(
            token, key
        )

    cut = now()

    d = raw(token, key)

    b, b15, b60 = [
        indicators(bars(d, m, cut))
        for m in (5, 15, 60)
    ]

    if min(
        len(b), len(b15), len(b60)
    ) < 21:

        raise RuntimeError(
            "Insufficient candle history"
        )

    a = b.iloc[-1]
    p = b.iloc[-2]
    h = b15.iloc[-1]
    j = b60.iloc[-1]

    if (
        a.name.date() != cut.date()
        or cut - a.name.to_pydatetime()
        > timedelta(minutes=10)
    ):

        raise RuntimeError(
            "Stale spot candle"
        )

    today = b[
        b.index.date == cut.date()
    ]

    opening = today.iloc[:3]

    if (
        len(opening) < 3
        or list(opening.index.time) != [
            time(9, 15),
            time(9, 20),
            time(9, 25)
        ]
    ):

        raise RuntimeError(
            "Opening range missing"
        )

    atr = n(a.atr)

    if not np.isfinite(atr) or atr <= 0:

        raise RuntimeError(
            "Invalid spot ATR"
        )

    side = "NEUTRAL"

    if a.close > a.ema9 > a.ema21:

        side = "CE"

    elif a.close < a.ema9 < a.ema21:

        side = "PE"

    s = 1 if side == "CE" else -1

    prior = b.iloc[-6:-1]

    if s == 1:

        level = max(
            opening.high.max(),
            prior.high.max()
        )

        breakout = (
            a.close > level + .05 * atr
        )

        retest_level = max(
            opening.high.max(),
            b.high.iloc[-10:-4].max()
        )

        retest = (
            b.close.iloc[-4:-1].max()
            > retest_level + .05 * atr

            and a.low
            <= retest_level + .35 * atr

            and a.low
            >= retest_level - .50 * atr

            and a.close > retest_level
            and a.close > a.open
        )

        continuation = (
            a.close > p.high
            and a.ema9 > p.ema9
            and p.close > p.ema21
        )

        pullback = (
            min(a.low, p.low)
            <= a.ema9 + .25 * atr

            and min(a.low, p.low)
            >= a.ema21 - .30 * atr

            and a.close > p.close
            and a.close > a.ema9
        )

    else:

        level = min(
            opening.low.min(),
            prior.low.min()
        )

        breakout = (
            a.close < level - .05 * atr
        )

        retest_level = min(
            opening.low.min(),
            b.low.iloc[-10:-4].min()
        )

        retest = (
            b.close.iloc[-4:-1].min()
            < retest_level - .05 * atr

            and a.high
            >= retest_level - .35 * atr

            and a.high
            <= retest_level + .50 * atr

            and a.close < retest_level
            and a.close < a.open
        )

        continuation = (
            a.close < p.low
            and a.ema9 < p.ema9
            and p.close < p.ema21
        )

        pullback = (
            max(a.high, p.high)
            >= a.ema9 - .25 * atr

            and max(a.high, p.high)
            <= a.ema21 + .30 * atr

            and a.close < p.close
            and a.close < a.ema9
        )

    strategies = {

        "Fresh Breakout": breakout,

        "Breakout Retest": retest,

        "Trend Continuation": continuation,

        "EMA Pullback": pullback
    }

    matched = [
        name
        for name, passed
        in strategies.items()
        if bool(passed)
    ]

    rng = a.high - a.low

    closepos = (
        (a.close - a.low) / rng
        if rng > 0 else .5
    )

    body = abs(a.close - a.open)

    volume = today.volume.sum()

    vwap = (
        (
            today.volume
            * (
                today.high
                + today.low
                + today.close
            ) / 3
        ).sum() / volume
        if volume > 0 else np.nan
    )

    higher60 = bool(
        s * (j.close - j.ema21) > 0
    )

    checks = {

        "Trend": side != "NEUTRAL",

        "Higher TF": bool(
            s * (h.close - h.ema21) > 0
        ),

        "Momentum": bool(
            a.rsi >= 52
            and a.plus > a.minus
            if s == 1
            else a.rsi <= 48
            and a.minus > a.plus
        ),

        "ADX": bool(
            a.adx >= 20
        ),

        "Strategy Match": bool(matched),

        "Candle quality": bool(

            body >= .25 * atr

            and s * (
                a.close - a.open
            ) > 0

            and (
                closepos >= .60
                if s == 1
                else closepos <= .40
            )
        ),

        "Not extended": bool(
            abs(a.close - a.ema9)
            <= 2 * atr
        ),

        "Spot volume/VWAP": (
            None
            if key.startswith("NSE_INDEX|")
            else bool(
                a.rvol >= 1.10
                and s * (
                    a.close - vwap
                ) > 0
            )
        )
    }

    metrics = {

        "Strategy": (
            matched[0]
            if matched else "NONE"
        ),

        "Matched Strategies":
            ", ".join(matched),

        "Higher 60m Support": higher60,

        "RSI": round(a.rsi, 2),
        "ADX": round(a.adx, 2),
        "RVOL": round(a.rvol, 2),

        "VWAP": vwap,
        "ATR": atr,

        "EMA 9": a.ema9,
        "EMA 21": a.ema21,
        "EMA 50": a.ema50,

        "DI Plus": a.plus,
        "DI Minus": a.minus,

        "Spot Close": a.close,

        "Opening High":
            opening.high.max(),

        "Opening Low":
            opening.low.min(),

        "Breakout Level": level,

        "Technical Candle":
            str(a.name)
    }

    S.v87_technical.append({

        "Time": now().isoformat(),

        "Key": key,

        "Side": side,

        "Strategy": metrics["Strategy"],

        "RSI": metrics["RSI"],

        "ADX": metrics["ADX"],

        "RVOL": metrics["RVOL"],

        "Failed": " | ".join(
            k for k, v in checks.items()
            if v is not None
            and not bool(v)
        )
    })

    S.v87_technical = (
        S.v87_technical[-10000:]
    )

    return side, checks, metrics


# =====================================
# OPTION SNAPSHOT COLLECTION
# =====================================

v87_old_chain = chain


def v87_collect(token, key, exp):

    v87_state()

    c = v87_old_chain(
        token, key, exp
    )

    if c.empty:

        raise RuntimeError(
            "Empty option chain"
        )

    received = now()

    ident = key + "|" + exp

    records = {

        x.Key: {

            "time": received,

            "OI": x.OI,

            "Volume": x.Volume,

            "LTP": x.LTP
        }

        for _, x in c.iterrows()
    }

    history = S.v87_history.setdefault(
        ident, []
    )

    history.append({

        "time": received,

        "records": records
    })

    history[:] = [

        item for item in history

        if 0 <= (
            received - item["time"]
        ).total_seconds() <= 600

    ][-20:]

    S.v87_cache[ident] = {

        "time": received,

        "chain": c.copy()
    }

    S.v87_requests += 1

    return c


def chain(token, key, exp):

    return v87_collect(
        token, key, exp
    )


def v87_previous(
    snapshot_key,
    snapshot_time,
    fallback
):

    history = S.v87_history.get(
        snapshot_key, []
    )

    valid = [

        item for item in history

        if (
            20 <= (
                snapshot_time - item["time"]
            ).total_seconds() <= 600

            and item["time"].date()
            == snapshot_time.date()
        )
    ]

    if not valid:

        return fallback

    best = min(

        valid,

        key=lambda item: abs(

            (
                snapshot_time - item["time"]
            ).total_seconds() - 90
        )
    )

    return best["records"]


# =====================================
# AUTOMATIC SNAPSHOT BATCH
# =====================================

def v87_snapshot_batch():

    v87_state()

    if (
        not token
        or not v87_auto_snapshots
        or not entry_window(token)
    ):

        return

    r = S.ranking

    if r.empty:

        return

    required = {
        "Symbol", "Key",
        "Side", "Fresh"
    }

    if not required.issubset(
        r.columns
    ):

        return

    candidates = []
    priority = []

    for side in ("CE", "PE"):

        z = r[
            (r.Side == side)
            & r.Fresh
        ].head(8)

        rows = z.to_dict("records")

        if rows:

            priority.append(rows[0])

            candidates.extend(
                rows[1:]
            )

    remaining = max(

        0,

        v87_snapshot_count
        - len(priority)
    )

    if candidates and remaining:

        start = (
            S.v87_cursor
            % len(candidates)
        )

        rotated = (
            candidates[start:]
            + candidates[:start]
        )

        priority.extend(
            rotated[:remaining]
        )

        S.v87_cursor += remaining

    for item in priority:

        sym = item["Symbol"]
        key = item["Key"]

        try:

            exp = expiry(
                token,
                key,
                now().date()
            )

            if not exp:

                continue

            ident = key + "|" + exp

            cached = S.v87_cache.get(
                ident
            )

            if cached and (

                now() - cached["time"]

            ).total_seconds() < 30:

                continue

            v87_collect(
                token, key, exp
            )

            S.v87_errors.pop(
                sym, None
            )

        except Exception as exc:

            S.v87_errors[sym] = (
                v86_redact(exc)
            )

    S.v87_last = now()


# =====================================
# V8.7 SNAPSHOT MONITOR
# =====================================

@st.fragment(
    run_every="45s"
    if v87_auto_snapshots
    else None
)
def v87_monitor():

    v87_state()

    v87_snapshot_batch()

    st.subheader(
        "V8.7 Option Data Monitor"
    )

    ready = 0

    for history in S.v87_history.values():

        if len(history) < 2:

            continue

        latest = history[-1]["time"]

        if (
            now() - latest
        ).total_seconds() > 180:

            continue

        if any(

            20 <= (
                latest - item["time"]
            ).total_seconds() <= 600

            for item in history[:-1]

        ):

            ready += 1

    a, b, c = st.columns(3)

    a.metric(
        "Tracked Chains",
        len(S.v87_history)
    )

    b.metric(
        "Comparison Ready",
        ready
    )

    c.metric(
        "Snapshot Requests",
        S.v87_requests
    )

    if S.v87_last:

        st.caption(
            "Last snapshot batch: "
            + S.v87_last.strftime(
                "%H:%M:%S IST"
            )
        )

    st.caption(
        "Snapshots use receipt timestamps. "
        "Missing OI or volume comparison "
        "is not treated as confirmation."
    )

    if S.v87_errors:

        with st.expander(
            "Snapshot Diagnostics"
        ):

            st.write(
                S.v87_errors
            )

    if S.v87_technical:

        report = pd.DataFrame(
            S.v87_technical
        )

        with st.expander(
            "V8.7 Strategy Diagnostics"
        ):

            st.dataframe(
                report.tail(100),
                hide_index=True,
                use_container_width=True
            )

            st.download_button(

                "Download Strategy Audit",

                report.to_csv(
                    index=False
                ),

                "v87_strategy_audit.csv",

                key="v87_strategy_csv"
            )

'''


# =====================================
# SOURCE INTEGRATION
# =====================================

def integrate_v87(source, previous):

    old = previous["load_previous"]()

    source = previous["integrate_v86"](
        source, old
    )

    def replace(before, after):

        nonlocal source

        matches = source.count(before)

        if matches != 1:

            raise RuntimeError(

                "V8.7 incompatible source: "

                + before[:120]

                + f" | matches={matches}"
            )

        source = source.replace(
            before, after, 1
        )

    # =================================
    # OPTION DELTA RANGE
    # =================================

    replace(

        "c.Delta.abs().between(.45, .70)",

        """c.Delta.abs().between(
                    .40 if v87_balanced else .45,
                    .75 if v87_balanced else .70
                )"""
    )

    replace(

        ".45 <= abs(o.Delta) <= .70",

        """(
                .40 <= abs(o.Delta) <= .75
                if v87_balanced
                else .45 <= abs(o.Delta) <= .70
            )"""
    )

    # Keep audit delta limits aligned.

    replace(

        ".between(.45, .70),",

        """.between(
                            .40 if v87_balanced else .45,
                            .75 if v87_balanced else .70
                        ),"""
    )

    # =================================
    # REMOVE CAPITAL / LOT REJECTION
    # =================================

    replace(

        """    lots = max(
        0,
        int(
            min(
                budget() / (risk * lot),
                capital / (entry * lot)
            )
        )
    )""",

        """    # One reference lot for research.
    # Actual position size is manual.
    lots = 1"""
    )

    replace(

        '"Affordable position": lots > 0,',

        '"Valid contract lot size": lot > 0,'
    )

    # Remove the old 25% risk-width cap.
    # A valid positive SL is still required.

    replace(

        "        or risk / entry > .25\n",

        ""
    )

    replace(

        '        "Lots": lots,',

        '''        "Lots": lots,

        "Position Sizing": "MANUAL",

        "Risk Per Share": round(risk, 2),

        "Risk Per Lot": round(
            risk * lot, 2
        ),

        "Capital Per Lot": round(
            entry * lot, 2
        ),'''
    )

    # =================================
    # REMOVE RISK BUDGET SCAN BLOCK
    # =================================

    replace(

        "        or budget() <= 0\n",

        ""
    )

    replace(

        '                and v["Planned Risk"] <= budget()\n',

        ""
    )

    replace(

        '                and v["Capital"] <= capital\n',

        ""
    )

    # =================================
    # IMPROVED COMPARATIVE SNAPSHOTS
    # =================================

    replace(

        """            old = S.snapshots.get(
                snapshot_key, {}
            )""",

        """            old = v87_previous(
                snapshot_key,
                snapshot_time,
                S.snapshots.get(
                    snapshot_key, {}
                )
            )"""
    )

    # =================================
    # PREMIUM MOMENTUM
    # =================================

    replace(

        """        and a.rsi >= 52
        and a.close > b.close.iloc[-2]
        and prem > 0""",

        """        and a.rsi >= (
            50 if v87_balanced else 52
        )
        and a.close > b.close.iloc[-2]
        and (
            prem > 0
            if np.isfinite(prem)
            else False
        )"""
    )

    # =================================
    # FRESH VOLUME
    # =================================

    replace(

        "vol > 0 and a.rvol >= 1.1",

        """vol > 0
            and a.rvol >= (
                1.0 if v87_balanced else 1.1
            )"""
    )

    # =================================
    # ENTRY EXTENSION
    # =================================

    replace(

        "abs(entry - a.close) <= .5 * atr",

        """abs(entry - a.close) <= (
                .75 if v87_balanced else .5
            ) * atr"""
    )

    # =================================
    # BALANCED MASTER SCORING
    # =================================

    replace(

        """    safe = (
        all(groups.values())
        and all(safety.values())
        and score >= 90
    )""",

        """    if v87_balanced:

        higher_support = bool(
            metrics.get(
                "Higher 60m Support",
                False
            )
        )

        balanced_groups = {

            "Spot direction":
                groups["Spot direction"],

            "Premium momentum":
                groups["Premium momentum"],

            "Fresh volume":
                groups["Fresh volume"],

            "Live OI increase":
                groups["Live OI increase"],

            "Opposite OI observed":
                groups["Opposite OI observed"],

            "Delta":
                groups["Delta"],

            "IV / Greeks":
                groups["IV / Greeks"],

            "Higher 60m Support":
                higher_support
        }

        balanced_weights = {

            "Spot direction": 25,

            "Premium momentum": 20,

            "Fresh volume": 15,

            "Live OI increase": 5,

            "Opposite OI observed": 5,

            "Delta": 10,

            "IV / Greeks": 5,

            "Higher 60m Support": 5
        }

        technical_points = sum(

            balanced_weights[k]

            for k, passed
            in balanced_groups.items()

            if bool(passed)
        )

        score = round(

            technical_points
            + news_points,

            1
        )

        mandatory = [

            "Spot direction",

            "Premium momentum",

            "Fresh volume",

            "Delta",

            "IV / Greeks"
        ]

        safety[
            "Comparable option data"
        ] = bool(

            np.isfinite(
                [oi, vol, prem]
            ).all()

            and vol >= 0
        )

        safe = (

            all(
                bool(
                    balanced_groups[k]
                )

                for k in mandatory
            )

            and all(safety.values())

            and score >= v87_score
        )

        groups = balanced_groups

        base_score = (
            technical_points / .90
        )

    else:

        safe = (

            all(groups.values())

            and all(safety.values())

            and score >= 90
        )"""
    )

    # =================================
    # MANUAL POSITION DISPLAY
    # =================================

    replace(

        '''                "Lots",
                "Planned Risk",
                "Capital",''',

        '''                "Lot Size",
                "Risk Per Lot",
                "Capital Per Lot",'''
    )

    replace(

        '"Remaining risk ₹",\n        max(0, daily - used)',

        '"Position Sizing",\n        "MANUAL"'
    )

    # =================================
    # INSTALL ENGINE BEFORE DASHBOARD
    # =================================

    replace(

        "\ndashboard()\n",

        "\n" + ENGINE + "\n"
        + "\ndashboard()\n"
        + "\nv87_monitor()\n"
    )

    # Keep old audit navigation intact.
    # Update visible version information.

    source = source.replace(

        "V8.6 Market Research Audit",

        "V8.7 Market Research Audit"
    )

    source = source.replace(

        "v86_option_evaluations.csv",

        "v87_option_evaluations.csv"
    )

    source = source.replace(

        "v86_session_audit.csv",

        "v87_session_audit.csv"
    )

    source += '''

st.divider()

st.caption(
    "V8.7 Balanced Multi-Strategy Research | "
    "Manual Position Sizing | "
    "One-Lot Reference P&L | "
    "No Capital or Risk Budget Rejection | "
    "No Automatic Orders"
)
'''

    return source, old


# =====================================
# MAIN APPLICATION
# =====================================

try:

    previous = load_v86()

    original = BASE_DIR / "app_v2.py"

    if not original.is_file():

        raise FileNotFoundError(
            original.name
        )

    source = original.read_text(
        encoding="utf-8-sig"
    )

    integrated_source, old = (
        integrate_v87(
            source,
            previous
        )
    )

    globals().update({

        name: old[name]

        for name in (

            "load_local_module",

            "locate_module",

            "available_files",

            "load_v84",

            "inspect"
        )

        if name in old
    })

    compiled = compile(

        integrated_source,

        str(original),

        "exec"
    )

except Exception as exc:

    st.error(

        "V8.7 stopped safely: "

        + type(exc).__name__

        + ": "

        + str(exc)
    )

    st.info(

        "Existing V8.6 files "

        "have not been modified."
    )

    st.stop()

else:

    exec(

        compiled,

        globals(),

        globals()
    )
