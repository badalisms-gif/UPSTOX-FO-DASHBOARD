
"""V8.6 - NSE F&O Research and Session Audit."""

import ast
import sys
import json
from pathlib import Path
from collections import Counter

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REVISION = "8.6"


# =====================================
# LOAD EXISTING V8.5.3 FUNCTIONS
# =====================================

def load_previous():

    candidates = [
        BASE_DIR / "app_v85_integrated.py",
        BASE_DIR / "app_v853_integrated.py"
    ]

    path = next(
        (p for p in candidates if p.is_file()),
        None
    )

    if path is None:
        raise FileNotFoundError(
            "V8.5.3 integration file not found"
        )

    tree = ast.parse(
        path.read_text(encoding="utf-8-sig"),
        filename=str(path)
    )

    nodes = [
        node for node in tree.body
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef
            )
        )
    ]

    module = ast.fix_missing_locations(
        ast.Module(
            body=nodes,
            type_ignores=[]
        )
    )

    namespace = {
        "__file__": str(path),
        "BASE_DIR": BASE_DIR,
        "REVISION": REVISION
    }

    exec(
        compile(
            module,
            str(path),
            "exec"
        ),
        namespace
    )

    required = [
        "integrate_v85",
        "load_local_module",
        "locate_module",
        "available_files"
    ]

    for name in required:
        if not callable(namespace.get(name)):
            raise RuntimeError(
                f"Missing V8.5.3 function: {name}"
            )

    return namespace


# =====================================
# V8.6 SESSION AUDIT FUNCTIONS
# =====================================

AUDIT_SOURCE = '''

def v86_state():

    day = now().date().isoformat()

    if S.get("v86_day") != day:

        S.v86_day = day
        S.v86_audit = []
        S.v86_options = []
        S.v86_screened = set()
        S.v86_selected = set()
        S.v86_technical = set()
        S.v86_chain = set()
        S.v86_qualified = set()
        S.v86_cycles = 0
        S.v86_errors = []
        S.v86_nse_context = {}
        S.v86_sequence = 0


def v86_redact(value):

    message = str(value)

    if token:
        message = message.replace(
            token, "[REDACTED]"
        )

    return message


def v86_log(
    stage,
    symbol="MARKET",
    side="",
    status="INFO",
    reason="",
    **extra
):

    v86_state()

    S.v86_sequence += 1

    record = {
        "Sequence": S.v86_sequence,
        "Time": now().isoformat(),
        "Cycle": S.v86_cycles,
        "Symbol": symbol,
        "Side": side,
        "Stage": stage,
        "Status": status,
        "Reason": v86_redact(reason),
        **extra
    }

    S.v86_audit.append(record)


def v86_failed(checks):

    return [
        name
        for name, passed in checks.items()
        if passed is not None
        and not bool(passed)
    ]


def v86_grade(record):

    if record.get("Status") == "QUALIFIED":

        if (
            record.get("Score", 0) >= 95
            and record.get("Verification")
            == "FULLY VERIFIED"
        ):
            return "A+ QUALIFIED"

        return "A QUALIFIED"

    score = record.get("Score", 0)

    if score >= 70:
        return "B WATCH ONLY"

    return "REJECTED"


def v86_option_record(record):

    v86_state()

    failed = v86_failed(
        record.get("Checks", {})
    )

    grade = v86_grade(record)

    row = {
        "Time": now().isoformat(),
        "Cycle": S.v86_cycles,
        "Symbol": record.get("Symbol"),
        "Side": record.get("Side"),
        "Strike": record.get("Strike"),
        "Expiry": record.get("Expiry"),
        "Grade": grade,
        "Score": record.get("Score"),
        "Status": record.get("Status"),
        "Verification": record.get("Verification"),
        "Failed Checks": " | ".join(failed),
        "Entry": record.get("Entry"),
        "SL": record.get("SL"),
        "Target": record.get("Target"),
        "Lots": record.get("Lots"),
        "Planned Risk": record.get("Planned Risk"),
        "Capital": record.get("Capital"),
        "Delta": record.get("Delta"),
        "Theta": record.get("Theta"),
        "Gamma": record.get("Gamma"),
        "Vega": record.get("Vega"),
        "IV": record.get("IV"),
        "OI Change": record.get("OI Change"),
        "Fresh Volume": record.get("Fresh Volume"),
        "Premium Change": record.get("Premium Change"),
        "Spread %": record.get("Spread %"),
        "RSI": record.get("RSI"),
        "ADX": record.get("ADX"),
        "RVOL": record.get("RVOL")
    }

    S.v86_options.append(row)

    if record.get("Status") == "QUALIFIED":
        S.v86_qualified.add(
            record.get("Symbol")
        )

    v86_log(
        "OPTION SCORE",
        record.get("Symbol", ""),
        record.get("Side", ""),
        grade,
        " | ".join(failed),
        Score=record.get("Score"),
        Strike=record.get("Strike")
    )

    return grade


# =====================================
# NSE MARKET CONTEXT
# =====================================

def v86_fetch_nse_context():

    v86_state()

    from market_data_engine import NSEMarketData

    client = NSEMarketData()

    result = {
        "Checked": now().isoformat(),
        "Data": {},
        "Errors": {}
    }

    tasks = [
        ("Market Status", "market_status", ()),
        ("Equity Status", "equity_status", ()),
        ("Sector Strength", "sector_strength", ()),
        ("India VIX", "india_vix", ()),
        ("Market Movers", "market_movers", ())
    ]

    for label, method, args in tasks:

        try:

            function = getattr(client, method)

            data = function(*args)

            if data is None:
                raise RuntimeError(
                    "Empty response"
                )

            result["Data"][label] = data

        except Exception as exc:

            result["Errors"][label] = (
                v86_redact(exc)
            )

    S.v86_nse_context = result

    v86_log(
        "NSE INTELLIGENCE",
        status=(
            "PARTIAL"
            if result["Errors"]
            else "RECEIVED"
        ),
        reason=str(
            result["Errors"]
        )
    )


# =====================================
# SESSION AUDIT DISPLAY
# =====================================

def v86_render_audit():

    v86_state()

    st.header("V8.6 Market Research Audit")

    a, b, c, d = st.columns(4)

    a.metric(
        "Stocks Screened",
        len(S.v86_screened)
    )

    b.metric(
        "Selected for Deep Scan",
        len(S.v86_selected)
    )

    c.metric(
        "Technical Analysis Completed",
        len(S.v86_technical)
    )

    d.metric(
        "Technical Filters Passed",
        len(S.v86_chain)
    )

    a, b, c = st.columns(3)

    a.metric(
        "Option Evaluations",
        len(S.v86_options)
    )

    b.metric(
        "Qualified Symbols",
        len(S.v86_qualified)
    )

    c.metric(
        "Audit Scan Cycles",
        S.v86_cycles
    )

    st.caption(
        "Unique stock counts are cumulative "
        "for the current dashboard session. "
        "Option evaluations may include repeats."
    )

    if S.v86_options:

        df = pd.DataFrame(
            S.v86_options
        )

        st.subheader(
            "Opportunity Classification"
        )

        counts = (
            df["Grade"]
            .value_counts()
            .rename_axis("Grade")
            .reset_index(name="Evaluations")
        )

        st.dataframe(
            counts,
            hide_index=True,
            use_container_width=True
        )

        latest = (
            df.sort_values(
                "Time",
                ascending=False
            )
            .drop_duplicates(
                [
                    "Symbol",
                    "Side",
                    "Strike",
                    "Expiry"
                ]
            )
        )

        watch = latest[
            latest.Grade == "B WATCH ONLY"
        ].copy()

        if not watch.empty:

            watch = watch[
                watch.Time.apply(
                    lambda x: recent(x, 180)
                )
            ]

        if not watch.empty:

            st.subheader(
                "B Grade - Recent Watchlist"
            )

            st.warning(
                "Research watchlist only. "
                "These setups failed one or more "
                "qualification conditions. "
                "Do not treat them as entries."
            )

            cols = [
                "Symbol",
                "Side",
                "Strike",
                "Expiry",
                "Score",
                "Failed Checks",
                "Time"
            ]

            st.dataframe(
                watch[cols].sort_values(
                    "Score",
                    ascending=False
                ).head(20),
                hide_index=True,
                use_container_width=True
            )

        st.download_button(
            "Download All Option Evaluations",
            df.to_csv(index=False),
            "v86_option_evaluations.csv",
            key="v86_option_csv"
        )

    else:

        st.info(
            "No option evaluations recorded yet."
        )

    if S.v86_audit:

        audit = pd.DataFrame(
            S.v86_audit
        )

        failures = audit[
            audit.Status.isin([
                "REJECTED",
                "ERROR",
                "B WATCH ONLY"
            ])
        ]

        reasons = Counter()

        for value in failures.Reason:

            for reason in str(value).split(" | "):

                reason = reason.strip()

                if reason:
                    reasons[reason] += 1

        if reasons:

            st.subheader(
                "Most Frequent Rejection Reasons"
            )

            report = pd.DataFrame([
                {
                    "Reason": reason,
                    "Occurrences": count
                }
                for reason, count
                in reasons.most_common(30)
            ])

            st.dataframe(
                report,
                hide_index=True,
                use_container_width=True
            )

        with st.expander(
            "Complete Session Audit"
        ):

            st.dataframe(
                audit.tail(500),
                hide_index=True,
                use_container_width=True
            )

        st.download_button(
            "Download Full Session Audit",
            audit.to_csv(index=False),
            "v86_session_audit.csv",
            key="v86_audit_csv"
        )

    st.caption(
        "Session memory only. Download reports "
        "before rebooting or closing the app."
    )


def v86_render_nse_context():

    v86_state()

    st.header("NSE MCP Market Context")

    if st.button(
        "FETCH NSE MARKET CONTEXT",
        key="v86_nse_context_button"
    ):

        try:

            with st.spinner(
                "Fetching NSE MCP data..."
            ):
                v86_fetch_nse_context()

        except Exception as exc:

            st.error(
                v86_redact(exc)
            )

    result = S.v86_nse_context

    if not result:

        st.info(
            "Fetch NSE context to inspect "
            "market and sector information."
        )

        return

    st.caption(
        "Received: "
        + result.get("Checked", "")
    )

    for name, data in result.get(
        "Data", {}
    ).items():

        with st.expander(name):

            st.write(data)

    for name, error in result.get(
        "Errors", {}
    ).items():

        st.warning(
            name + ": " + error
        )

    st.download_button(
        "Download NSE Market Context",
        json.dumps(
            result,
            default=str,
            indent=2
        ),
        "v86_nse_context.json",
        key="v86_nse_json"
    )

    st.caption(
        "NSE context is research information. "
        "It does not automatically change "
        "signal scores or qualification rules."
    )

'''


# =====================================
# V8.6 SOURCE INTEGRATION
# =====================================

def integrate_v86(source, previous):

    source = previous[
        "integrate_v85"
    ](source)

    source = source.replace(
        "8.5.3",
        "8.6"
    )

    def replace(old, new):

        nonlocal source

        matches = source.count(old)

        if matches != 1:

            raise ValueError(
                "V8.6 incompatible source: "
                + old[:100]
                + f" | matches={matches}"
            )

        source = source.replace(
            old, new, 1
        )

    # Add session audit functions.

    replace(
        "def scan():",
        AUDIT_SOURCE + '''

def scan():

    v86_state()

    S.v86_cycles += 1

    v86_log(
        "SCAN START",
        status="STARTED"
    )
'''
    )

    # Record full market screening.

    replace(
        '''    S.ranking = r
    S.coverage = f"{received}/{total}"''',

        '''    S.ranking = r
    S.coverage = f"{received}/{total}"

    if not r.empty:

        observed = r[
            r.LTP.notna()
        ]

        S.v86_screened.update(
            observed.Symbol.tolist()
        )

        v86_log(
            "MARKET SCREEN",
            status="COMPLETED",
            reason="Live market ranking",
            Universe=total,
            Received=received,
            Verified=int(r.Fresh.sum())
        )'''
    )

    # Record early scan exits.

    replace(
        '''        S.last_scan = now()
        return

    selected = news_candidates(r)''',

        '''        reason = (
            "Empty market ranking"
            if r.empty
            else (
                "Entry window closed"
                if not entry_window(token)
                else "Risk budget unavailable"
            )
        )

        v86_log(
            "SCAN BLOCKED",
            status="REJECTED",
            reason=reason
        )

        S.last_scan = now()
        return

    selected = news_candidates(r)'''
    )

    # Avoid conflicting news direction
    # overriding verified market direction.

    replace(
        '''        side = direction.split()[0]

        points = n(''',

        '''        side = direction.split()[0]

        if row.get("Side") != side:
            continue

        points = n('''
    )

    # Record selected stocks.

    replace(
        '''    selected = list(unique.values())
    progress = st.progress(0)''',

        '''    selected = list(unique.values())

    for item in selected:

        S.v86_selected.add(
            item["Symbol"]
        )

        v86_log(
            "CANDIDATE SELECTED",
            item["Symbol"],
            item["Side"],
            "SELECTED",
            item.get("Origin", "")
        )

    if not selected:

        v86_log(
            "CANDIDATE SELECTION",
            status="REJECTED",
            reason="No verified directional candidates"
        )

    progress = st.progress(0)'''
    )

    # Record technical analysis.

    replace(
        '''            S.deep_scanned.add(sym)''',

        '''            S.deep_scanned.add(sym)

            S.v86_technical.add(sym)

            v86_log(
                "TECHNICAL ANALYSIS",
                sym,
                side,
                (
                    "REJECTED"
                    if v86_failed(checks)
                    else "PASSED"
                ),
                " | ".join(
                    v86_failed(checks)
                ),
                RSI=metrics.get("RSI"),
                ADX=metrics.get("ADX"),
                RVOL=metrics.get("RVOL")
            )'''
    )

    # Preserve existing option snapshots.
    # Record technical pass separately.

    replace(
        '''            news = automatic_news(sym, side)''',

        '''            S.v86_chain.add(sym)

            v86_log(
                "TECHNICAL FILTERS",
                sym,
                side,
                "PASSED"
            )

            news = automatic_news(sym, side)'''
    )

    # Record option chain filter failures.

    replace(
        '''            if z.empty:
                raise RuntimeError("No eligible option")''',

        '''            if z.empty:

                side_chain = c[
                    c.Side == side
                ]

                filters = {
                    "Delta outside range":
                        ~side_chain.Delta.abs()
                        .between(.45, .70),

                    "Option volume too low":
                        ~(side_chain.Volume >= minvol),

                    "Spread too wide":
                        ~(side_chain["Spread %"] <= spread),

                    "IV outside range":
                        ~(
                            (side_chain.IV > 0)
                            & (side_chain.IV <= iv)
                        ),

                    "OI unavailable":
                        ~(side_chain.OI > 0),

                    "Invalid ask or premium":
                        ~(
                            (side_chain.Ask > 0)
                            & (side_chain.LTP > 0)
                        )
                }

                for name, mask in filters.items():

                    failures = int(mask.sum())

                    if failures:

                        v86_log(
                            "OPTION CHAIN FILTER",
                            sym,
                            side,
                            "REJECTED",
                            name,
                            FailedContracts=failures
                        )

                raise RuntimeError(
                    "No eligible option"
                )'''
    )

    # Add classification only after the
    # original final live verification.

    replace(
        '''                    if v["Status"] == "QUALIFIED":
                        ident = (''',

        '''                    v["Grade"] = (
                        v86_option_record(v)
                    )

                    if v["Status"] == "QUALIFIED":
                        ident = ('''
    )

    # Preserve option-level errors.

    replace(
        '''                except Exception as e:
                    S.diagnostics.append(
                        f"{sym} option: {e}"
                    )''',

        '''                except Exception as e:

                    v86_log(
                        "OPTION EVALUATION",
                        sym,
                        side,
                        "ERROR",
                        str(e),
                        Strike=float(o.Strike)
                    )

                    S.diagnostics.append(
                        f"{sym} option: {v86_redact(e)}"
                    )'''
    )

    # Preserve stock-level rejections.

    replace(
        '''        except Exception as e:
            S.diagnostics.append(
                f"{sym}: {e}"
            )''',

        '''        except Exception as e:

            v86_log(
                "STOCK SCAN",
                sym,
                row.get("Side", ""),
                "REJECTED",
                str(e)
            )

            S.diagnostics.append(
                f"{sym}: {v86_redact(e)}"
            )'''
    )

    # Record completed scan.

    replace(
        '''    S.last_scan = now()
    S.scan_count += 1''',

        '''    S.last_scan = now()
    S.scan_count += 1

    v86_log(
        "SCAN END",
        status="COMPLETED",
        Selected=len(selected),
        OptionResults=len(S.results)
    )'''
    )

    # Add audit navigation.

    replace(
        '''            "Trading Journal"
        ]
    )''',

        '''            "Trading Journal",
            "V8.6 Research Audit",
            "NSE MCP Context"
        ]
    )'''
    )

    # Add audit to dashboard fragment.

    replace(
        '''    with st.expander(
        "Rejected Setups / Diagnostics"
    ):''',

        '''    if page == "V8.6 Research Audit":

        v86_render_audit()

    with st.expander(
        "Rejected Setups / Diagnostics"
    ):'''
    )

    # NSE requests run outside the
    # automatic refresh fragment.

    source += '''

if page == "NSE MCP Context":

    v86_render_nse_context()

st.caption(
    "V8.6 | Session Audit | A+/A/B Classification "
    "| Existing Safety Rules Preserved "
    "| No Automatic Orders"
)
'''

    return source


# =====================================
# MAIN APPLICATION
# =====================================

try:

    previous = load_previous()

    globals().update({
        name: previous[name]
        for name in (
            "load_local_module",
            "locate_module",
            "available_files",
            "load_v84",
            "inspect"
        )
        if name in previous
    })

    original = BASE_DIR / "app_v2.py"

    if not original.is_file():

        raise FileNotFoundError(
            "app_v2.py not found"
        )

    source = original.read_text(
        encoding="utf-8-sig"
    )

    integrated_source = integrate_v86(
        source,
        previous
    )

    compiled = compile(
        integrated_source,
        str(original),
        "exec"
    )

except Exception as exc:

    st.error(
        "V8.6 stopped safely: "
        + type(exc).__name__
        + ": "
        + str(exc)
    )

    st.info(
        "Existing V8.5.3 files "
        "have not been modified."
    )

    st.stop()

else:

    exec(
        compiled,
        globals(),
        globals()
    )
