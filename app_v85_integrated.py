
"""V8.5 Dashboard: V8.4 + NSE Intelligence."""

from pathlib import Path
import ast
import streamlit as st


def load_v84():

    path = Path(__file__).with_name(
        "app_v84_integrated.py"
    )

    source = path.read_text(
        encoding="utf-8"
    )

    tree = ast.parse(source)

    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "integrate"
    ]

    if len(functions) != 1:
        raise ValueError(
            "V8.4 integration function not found"
        )

    module = ast.Module(
        body=functions,
        type_ignores=[]
    )

    namespace = {}

    exec(
        compile(
            module,
            str(path),
            "exec"
        ),
        namespace
    )

    return namespace["integrate"]


def integrate_v85(source):

    integrate_v84 = load_v84()

    source = integrate_v84(source)

    source = source.replace(
        "NSE F&O PRO — ALL IN ONE V8.4",
        "NSE F&O PRO — ALL IN ONE V8.5"
    )

    source = source.replace(
        "V8.4 Live Data Engine",
        "V8.5 Live Data Engine"
    )

    extra = '''

# =====================================
# V8.5 NSE MARKET INTELLIGENCE
# =====================================

from nse_intelligence_engine import (
    render_intelligence
)

with st.sidebar:

    st.divider()

    st.header("V8.5 Research Modules")

    show_nse_intelligence = st.checkbox(
        "Show NSE Market Intelligence",
        value=False,
        key="v85_intelligence"
    )

if show_nse_intelligence:

    st.divider()

    try:

        intelligence_universe = (
            get_fo_universe()
        )

        render_intelligence(
            token=token,
            universe=intelligence_universe
        )

    except Exception as exc:

        message = str(exc)

        if token:
            message = message.replace(
                token,
                "[REDACTED]"
            )

        st.error(
            "NSE Intelligence: " + message
        )

    st.divider()

st.caption(
    "V8.5 | Live NSE Verification | "
    "Market Intelligence | Research Only"
)

'''

    return source + "\n\n" + extra


# =====================================
# SAFE DASHBOARD LOADER
# =====================================

try:

    original = Path(__file__).with_name(
        "app_v2.py"
    )

    source = original.read_text(
        encoding="utf-8"
    )

    integrated_source = integrate_v85(
        source
    )

    code = compile(
        integrated_source,
        str(original),
        "exec"
    )

except Exception as exc:

    st.error(
        "V8.5 integration stopped safely: "
        + str(exc)
    )

    st.stop()

else:

    exec(
        code,
        globals(),
        globals()
    )
