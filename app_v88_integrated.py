
import ast
import sys
import inspect
from pathlib import Path
from datetime import time

import streamlit as st


# ==========================================
# NSE F&O PRO V8.8.1
# EXTENDED MARKET SESSION
# MANUAL POSITION SIZING
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REVISION = "8.8.1"

PREVIOUS_APP = BASE_DIR / "app_v87_integrated.py"


# ==========================================
# MARKET SESSION CONFIGURATION
# ==========================================

MARKET_START = time(9, 15)
ENTRY_START = time(9, 30)
ENTRY_END = time(15, 15)
EOD_TIME = time(15, 25)
MARKET_END = time(15, 30)


# ==========================================
# LOAD EXISTING V8.7 ENGINE
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

    imports = []
    functions = []
    constants = []

    for node in tree.body:

        if isinstance(
            node,
            (ast.Import, ast.ImportFrom)
        ):
            imports.append(node)

        elif isinstance(
            node,
            ast.FunctionDef
        ):
            functions.append(node)

        elif isinstance(node, ast.Assign):

            try:
                ast.literal_eval(node.value)
                constants.append(node)

            except (ValueError, TypeError):
                pass

    module = ast.Module(
        body=imports + constants + functions,
        type_ignores=[]
    )

    ast.fix_missing_locations(module)

    namespace = {
        "__file__": str(PREVIOUS_APP),
        "__name__": "v87_loader",
        "BASE_DIR": BASE_DIR,
        "REVISION": "8.7",
        "inspect": inspect
    }

    exec(
        compile(
            module,
            str(PREVIOUS_APP),
            "exec"
        ),
        namespace
    )

    namespace["BASE_DIR"] = BASE_DIR

    required = [
        "load_v86",
        "integrate_v87"
    ]

    for name in required:

        if not callable(namespace.get(name)):

            raise RuntimeError(
                f"Missing V8.7 function: {name}"
            )

    return namespace


# ==========================================
# MARKET TIMING TRANSFORMATION
# ==========================================

class SessionTiming(ast.NodeTransformer):

    def __init__(self):

        self.entry_changes = 0
        self.eod_changes = 0
        self.snapshot_changes = 0

    def visit_FunctionDef(self, node):

        # New entries: 09:30 to 15:15

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
                replacement,
                node
            )

        # Existing signal tracking: 15:25

        if node.name == "track":

            for item in ast.walk(node):

                if not isinstance(
                    item, ast.Call
                ):
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

        # Option snapshots:
        # Full normal market session

        if node.name == "v87_snapshot_batch":

            for item in ast.walk(node):

                if not isinstance(
                    item, ast.Call
                ):
                    continue

                if not isinstance(
                    item.func, ast.Name
                ):
                    continue

                if item.func.id == "entry_window":

                    item.func.id = "open_market"

                    self.snapshot_changes += 1

        return self.generic_visit(node)


# ==========================================
# UPDATE INTEGRATED DASHBOARD
# ==========================================

def update_source(source):

    tree = ast.parse(source)

    transformer = SessionTiming()

    tree = transformer.visit(tree)

    ast.fix_missing_locations(tree)

    if transformer.entry_changes != 1:

        raise RuntimeError(
            "Entry window verification failed"
        )

    if transformer.eod_changes != 1:

        raise RuntimeError(
            "EOD timing verification failed"
        )

    if transformer.snapshot_changes < 1:

        raise RuntimeError(
            "Snapshot timing verification failed"
        )

    source = ast.unparse(tree)

    source = source.replace(
        "V8.7 Strategy Engine",
        "V8.8.1 Strategy Engine"
    )

    source = source.replace(
        "Balanced V8.7",
        "Balanced V8.8.1"
    )

    timing_panel = """

with st.sidebar:

    st.divider()

    st.header("V8.8.1 Market Timing")

    st.success(
        "Extended market session enabled"
    )

    st.write("Market Open: 09:15 AM")

    st.write("Entry Start: 09:30 AM")

    st.write("Entry Cutoff: 03:15 PM")

    st.write("EOD Observation: 03:25 PM")

    st.write("Market Close: 03:30 PM")

    st.write(
        "Option Snapshots: 09:15 AM - 03:30 PM"
    )

    st.success(
        "Manual Position Sizing"
    )

    st.caption(
        "Existing V8.7 signal qualification "
        "and manual risk settings retained."
    )

    st.caption(
        "Entry, Target, SL, Greeks, OI, "
        "Volume and Premium Momentum retained."
    )

    st.warning(
        "Research signals only. "
        "No automatic orders."
    )

"""

    source += timing_panel

    compile(
        source,
        "app_v881_runtime.py",
        "exec"
    )

    return source


# ==========================================
# BUILD COMPLETE V8.8.1 ENGINE
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

    # Load previous V8.6 integration

    previous = namespace["load_v86"]()

    if not isinstance(previous, dict):

        raise RuntimeError(
            "V8.6 loader returned invalid data"
        )

    # Corrected V8.7 integration call

    integrated_source = namespace[
        "integrate_v87"
    ](
        base_source,
        previous
    )

    if not isinstance(
        integrated_source, str
    ):

        raise RuntimeError(
            "V8.7 integration returned "
            "invalid source"
        )

    # Restore helper functions from
    # previous integration layers

    helper_names = (
        "load_local_module",
        "locate_module",
        "available_files",
        "load_v84",
        "inspect"
    )

    for name in helper_names:

        if name in previous:

            namespace[name] = previous[name]

    if callable(previous.get("load_previous")):

        older = previous["load_previous"]()

        if isinstance(older, dict):

            for name in helper_names:

                if (
                    name not in namespace
                    and name in older
                ):

                    namespace[name] = older[name]

    namespace["inspect"] = inspect

    # Apply extended market timing

    final_source = update_source(
        integrated_source
    )

    namespace["REVISION"] = REVISION

    namespace["__name__"] = "__main__"

    namespace["__file__"] = str(
        BASE_DIR / "app_v88_integrated.py"
    )

    # Run complete integrated dashboard

    exec(
        compile(
            final_source,
            "app_v881_runtime.py",
            "exec"
        ),
        namespace
    )


# ==========================================
# APPLICATION START
# ==========================================

try:

    main()

except Exception as e:

    st.error(
        "V8.8.1 integration could not start."
    )

    st.error(
        type(e).__name__
    )

    if isinstance(
        e,
        (TypeError, NameError, FileNotFoundError)
    ):

        st.code(str(e)[:500])

    elif isinstance(e, RuntimeError):

        message = str(e)

        safe_messages = (
            "verification failed",
            "not found",
            "Missing V8.7 function",
            "invalid data",
            "invalid source"
        )

        if any(
            x in message
            for x in safe_messages
        ):

            st.code(message[:500])

    st.info(
        "Existing V8.7 files remain unchanged."
    )

    st.stop()
