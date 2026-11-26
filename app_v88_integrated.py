import ast
import sys
from pathlib import Path
from datetime import time

import streamlit as st


# ==========================================
# NSE F&O PRO V8.8
# EXTENDED MARKET SESSION
# MANUAL POSITION SIZING
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REVISION = "8.8"

PREVIOUS_APP = BASE_DIR / "app_v87_integrated.py"

ENTRY_START = time(9, 30)
ENTRY_END = time(15, 15)
EOD_TIME = time(15, 25)
MARKET_END = time(15, 30)


# ==========================================
# TIMING TRANSFORMATION
# ==========================================

class SessionTiming(ast.NodeTransformer):

    def __init__(self):
        self.entry_changes = 0
        self.eod_changes = 0

    def visit_FunctionDef(self, node):

        if node.name == "entry_window":

            replacement = ast.parse(
                """
def entry_window(token):
    return (
        open_market(token)
        and time(9, 30) <= now().time()
        < time(15, 15)
    )
"""
            ).body[0]

            self.entry_changes += 1

            return ast.copy_location(
                replacement, node
            )

        if node.name == "track":

            for item in ast.walk(node):

                if not isinstance(item, ast.Call):
                    continue

                if not isinstance(
                    item.func, ast.Name
                ):
                    continue

                if item.func.id != "time":
                    continue

                if len(item.args) != 2:
                    continue

                if not all(
                    isinstance(a, ast.Constant)
                    for a in item.args
                ):
                    continue

                values = [
                    a.value for a in item.args
                ]

                if values == [15, 20]:

                    item.args[1] = ast.Constant(
                        value=25
                    )

                    self.eod_changes += 1

        return self.generic_visit(node)


# ==========================================
# UPDATE INTEGRATED SOURCE
# ==========================================

def update_source(source):

    tree = ast.parse(source)

    transformer = SessionTiming()

    tree = transformer.visit(tree)

    ast.fix_missing_locations(tree)

    if transformer.entry_changes != 1:
        raise RuntimeError(
            "Entry window could not be "
            "verified safely."
        )

    if transformer.eod_changes != 1:
        raise RuntimeError(
            "EOD timing could not be "
            "verified safely."
        )

    source = ast.unparse(tree)

    source = source.replace(
        "V8.7 Strategy Engine",
        "V8.8 Strategy Engine"
    )

    source = source.replace(
        "Balanced V8.7",
        "Balanced V8.8"
    )

    # Preserve existing V8.7 manual sizing.
    # No new capital or risk rejection.

    timing_panel = """

with st.sidebar:

    st.divider()

    st.header("V8.8 Market Timing")

    st.success(
        "Extended session enabled"
    )

    st.write("Market Open: 09:15 AM")
    st.write("Entry Start: 09:30 AM")
    st.write("Entry Cutoff: 03:15 PM")
    st.write("EOD Observation: 03:25 PM")
    st.write("Market Close: 03:30 PM")

    st.info(
        "Manual position sizing. "
        "Existing V8.7 strategy and "
        "risk settings retained."
    )

    st.caption(
        "Research signals only. "
        "No automatic orders."
    )
"""

    source += timing_panel

    compile(
        source,
        "app_v88_runtime.py",
        "exec"
    )

    return source


# ==========================================
# LOAD EXISTING V8.7 INTEGRATION
# ==========================================

def load_v87():

    if not PREVIOUS_APP.exists():

        raise FileNotFoundError(
            "app_v87_integrated.py not found"
        )

    source = PREVIOUS_APP.read_text(
        encoding="utf-8-sig"
    )

    tree = ast.parse(source)

    imports = [
        node for node in tree.body
        if isinstance(
            node,
            (ast.Import, ast.ImportFrom)
        )
    ]

    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
    ]

    available = {
        node.name for node in functions
    }

    if "integrate_v87" not in available:

        raise RuntimeError(
            "Expected integrate_v87 function "
            "not found in V8.7."
        )

    module = ast.Module(
        body=imports + functions,
        type_ignores=[]
    )

    ast.fix_missing_locations(module)

    namespace = {
        "__file__": str(PREVIOUS_APP),
        "__name__": "v87_loader",
        "BASE_DIR": BASE_DIR,
        "REVISION": "8.7"
    }

    # Preserve top-level literal constants
    # required by the previous integration.

    for node in tree.body:

        if not isinstance(node, ast.Assign):
            continue

        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue

        for target in node.targets:

            if isinstance(target, ast.Name):

                namespace[target.id] = value

    exec(
        compile(
            module,
            str(PREVIOUS_APP),
            "exec"
        ),
        namespace
    )

    return namespace


# ==========================================
# BUILD AND RUN DASHBOARD
# ==========================================

def main():

    base_file = BASE_DIR / "app_v2.py"

    if not base_file.exists():

        raise FileNotFoundError(
            "app_v2.py not found"
        )

    namespace = load_v87()

    base_source = base_file.read_text(
        encoding="utf-8-sig"
    )

    integrated_source = namespace[
        "integrate_v87"
    ](base_source)

    if not isinstance(
        integrated_source, str
    ):

        raise RuntimeError(
            "V8.7 did not return valid source"
        )

    final_source = update_source(
        integrated_source
    )

    namespace["REVISION"] = REVISION

    namespace["__name__"] = "__main__"

    namespace["__file__"] = str(
        BASE_DIR / "app_v88_integrated.py"
    )

    exec(
        compile(
            final_source,
            "app_v88_runtime.py",
            "exec"
        ),
        namespace
    )


# ==========================================
# START APPLICATION
# ==========================================

try:

    main()

except Exception as e:

    st.error(
        "V8.8 integration could not start."
    )

    st.error(
        type(e).__name__
    )

    st.info(
        "Existing V8.7 files are unchanged. "
        "Check compatibility of the "
        "V8.7 integration function."
    )

    st.stop()