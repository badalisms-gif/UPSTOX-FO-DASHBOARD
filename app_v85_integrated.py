
"""V8.5 Dashboard - Safe NSE Intelligence Integration."""

from pathlib import Path
import ast
import sys
import importlib
import importlib.util
import streamlit as st


# =====================================
# REPOSITORY PATH FIX
# =====================================

BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

importlib.invalidate_caches()


# =====================================
# SAFE MODULE LOADER
# =====================================

def load_local_module(name):

    path = BASE_DIR / (name + ".py")

    if not path.is_file():

        available = sorted(
            p.name
            for p in BASE_DIR.glob("*.py")
        )

        raise FileNotFoundError(
            f"Missing deployed file: {path.name}. "
            f"Available Python files: {available}"
        )

    importlib.invalidate_caches()

    existing = sys.modules.get(name)

    if existing is not None:

        existing_path = getattr(
            existing, "__file__", None
        )

        if (
            existing_path
            and Path(existing_path).resolve() == path
        ):
            return existing

    spec = importlib.util.spec_from_file_location(
        name,
        str(path)
    )

    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot load {path.name}"
        )

    module = importlib.util.module_from_spec(
        spec
    )

    sys.modules[name] = module

    try:

        spec.loader.exec_module(module)

    except Exception:

        sys.modules.pop(name, None)
        raise

    return module


# =====================================
# LOAD EXISTING V8.4 INTEGRATION
# =====================================

def load_v84():

    path = BASE_DIR / "app_v84_integrated.py"

    if not path.is_file():
        raise FileNotFoundError(
            "app_v84_integrated.py not found"
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

    module = ast.fix_missing_locations(
        module
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


# =====================================
# V8.5 INTELLIGENCE INTEGRATION
# =====================================

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

with st.sidebar:

    st.divider()

    st.header("V8.5 Research Modules")

    show_nse_intelligence = st.checkbox(
        "Show NSE Market Intelligence",
        value=False,
        key="v85_intelligence"
    )

    intelligence_path = (
        BASE_DIR / "nse_intelligence_engine.py"
    )

    if intelligence_path.is_file():

        st.success(
            "NSE Intelligence file detected"
        )

    else:

        st.error(
            "NSE Intelligence file missing "
            "from deployed repository"
        )


if show_nse_intelligence:

    st.divider()

    st.header("NSE Market Intelligence")

    try:

        intelligence_module = load_local_module(
            "nse_intelligence_engine"
        )

        render_function = getattr(
            intelligence_module,
            "render_intelligence",
            None
        )

        if not callable(render_function):

            raise AttributeError(
                "render_intelligence function "
                "not found in "
                "nse_intelligence_engine.py"
            )

        st.success(
            "NSE Intelligence Engine loaded"
        )

        intelligence_universe = (
            get_fo_universe()
        )

        render_function(
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
            "NSE Intelligence Error: "
            + type(exc).__name__
            + ": "
            + message
        )

        st.warning(
            "The existing live market dashboard "
            "remains available. "
            "Intelligence data is not verified."
        )

        with st.expander(
            "Module Loading Diagnostics"
        ):

            st.write(
                "Intelligence file present:",
                intelligence_path.is_file()
            )

            st.write(
                "Repository folder:",
                str(BASE_DIR)
            )

            st.write(
                "Python files detected:"
            )

            st.write(
                sorted(
                    p.name
                    for p in BASE_DIR.glob("*.py")
                )
            )

            if isinstance(
                exc,
                ModuleNotFoundError
            ):

                st.write(
                    "Missing module:",
                    getattr(exc, "name", "Unknown")
                )

    st.divider()


st.caption(
    "V8.5 | Live NSE Verification | "
    "Market Intelligence | Research Only | "
    "No Automatic Orders"
)

'''

    return source + "\n\n" + extra


# =====================================
# SAFE DASHBOARD LOADER
# =====================================

try:

    original = BASE_DIR / "app_v2.py"

    if not original.is_file():

        raise FileNotFoundError(
            "Original app_v2.py not found"
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
        + type(exc).__name__
        + ": "
        + str(exc)
    )

    st.stop()

else:

    exec(
        code,
        globals(),
        globals()
    )
