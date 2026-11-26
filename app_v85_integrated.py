
"""V8.5 Dashboard - NSE Intelligence Loader Fix."""

from pathlib import Path
import ast
import os
import sys
import importlib
import importlib.util
import streamlit as st


# =====================================
# REPOSITORY DISCOVERY
# =====================================

BASE_DIR = Path(__file__).resolve().parent

REPO_DIRS = list(dict.fromkeys([
    BASE_DIR,
    Path.cwd().resolve(),
    Path("/mount/src/upstox-fo-dashboard"),
    Path("/mount/src/UPSTOX-FO-DASHBOARD")
]))

for folder in REPO_DIRS:

    if folder.is_dir():

        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))

importlib.invalidate_caches()


# =====================================
# FIND LOCAL PYTHON FILE
# =====================================

def find_local_file(filename):

    for folder in REPO_DIRS:

        path = folder / filename

        if path.is_file():
            return path.resolve()

    for folder in REPO_DIRS:

        if not folder.is_dir():
            continue

        if folder == Path("/"):
            continue

        try:

            for path in folder.iterdir():

                if (
                    path.is_file()
                    and path.name.strip() == filename
                ):
                    return path.resolve()

        except OSError:
            continue

    return None


# =====================================
# DEPLOYMENT DIAGNOSTICS
# =====================================

def deployment_details():

    details = {}

    for folder in REPO_DIRS:

        if not folder.is_dir():
            continue

        try:

            details[str(folder)] = sorted(
                repr(p.name)
                for p in folder.iterdir()
                if p.is_file()
            )

        except OSError as exc:

            details[str(folder)] = str(exc)

    return details


# =====================================
# SAFE MODULE LOADER
# =====================================

def load_local_module(name):

    filename = name + ".py"

    path = find_local_file(filename)

    if path is None:

        raise FileNotFoundError(
            f"{filename} not found in deployed files"
        )

    parent = str(path.parent)

    if parent not in sys.path:
        sys.path.insert(0, parent)

    importlib.invalidate_caches()

    existing = sys.modules.get(name)

    if existing is not None:

        existing_path = getattr(
            existing,
            "__file__",
            None
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
            f"Cannot load {path}"
        )

    module = importlib.util.module_from_spec(spec)

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

    path = find_local_file(
        "app_v84_integrated.py"
    )

    if path is None:

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

    module = ast.fix_missing_locations(module)

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
# NSE MARKET INTELLIGENCE
# =====================================

intelligence_path = find_local_file(
    "nse_intelligence_engine.py"
)

with st.sidebar:

    st.divider()

    st.header("V8.5 Research Modules")

    st.caption("Loader revision: 8.5.2")

    show_nse_intelligence = st.checkbox(
        "Show NSE Market Intelligence",
        value=False,
        key="v85_intelligence"
    )

    if intelligence_path is not None:

        st.success(
            "NSE Intelligence file detected"
        )

    else:

        st.error(
            "NSE Intelligence file not found "
            "in deployed repository"
        )

    with st.expander(
        "Deployment Diagnostics",
        expanded=intelligence_path is None
    ):

        st.write(
            "Repository folder:",
            str(BASE_DIR)
        )

        st.write(
            "Working directory:",
            str(Path.cwd())
        )

        st.write(
            "Intelligence file:",
            str(intelligence_path)
        )

        st.json(
            deployment_details()
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
                "not found"
            )

        st.success(
            "NSE Intelligence Engine loaded"
        )

        if not token:

            st.warning(
                "Enter Upstox access token "
                "for connected research."
            )

        else:

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

        if isinstance(
            exc,
            ModuleNotFoundError
        ):

            st.write(
                "Missing module:",
                getattr(exc, "name", "Unknown")
            )

        st.warning(
            "Existing dashboard remains available. "
            "Intelligence data is not verified."
        )

    st.divider()


st.caption(
    "V8.5.2 | Live NSE Verification | "
    "Market Intelligence | Research Only | "
    "No Automatic Orders"
)

'''

    return source + "\n\n" + extra


# =====================================
# SAFE DASHBOARD LOADER
# =====================================

try:

    original = find_local_file(
        "app_v2.py"
    )

    if original is None:

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

    st.json(
        deployment_details()
    )

    st.stop()

else:

    exec(
        code,
        globals(),
        globals()
    )
