
"""V8.5.3 Dashboard - NSE Intelligence Source Loader."""

from pathlib import Path
import ast
import sys
import inspect
import importlib
import importlib.util
from importlib.machinery import SourceFileLoader
import streamlit as st


# =====================================
# REPOSITORY CONFIGURATION
# =====================================

BASE_DIR = Path(__file__).resolve().parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

importlib.invalidate_caches()

REVISION = "8.5.3"


# =====================================
# LOCAL FILE DISCOVERY
# =====================================

def available_files():

    return sorted(
        p.name
        for p in BASE_DIR.iterdir()
        if p.is_file()
    )


def locate_module(name):

    expected = BASE_DIR / (name + ".py")

    if expected.is_file():
        return expected.resolve()

    matches = [
        p for p in BASE_DIR.iterdir()
        if p.is_file()
        and p.name.strip().lower()
        == (name + ".py").lower()
    ]

    if len(matches) == 1:
        return matches[0].resolve()

    raise FileNotFoundError(
        f"Missing file: {name}.py. "
        f"Available files: {available_files()}"
    )


# =====================================
# DIRECT PYTHON SOURCE LOADER
# =====================================

def load_local_module(name):

    path = locate_module(name)

    importlib.invalidate_caches()

    existing = sys.modules.get(name)

    if existing is not None:

        existing_path = getattr(
            existing,
            "__file__",
            None
        )

        existing_mtime = getattr(
            existing,
            "_source_mtime_ns",
            None
        )

        if (
            existing_path
            and Path(existing_path).resolve() == path
            and existing_mtime
            == path.stat().st_mtime_ns
        ):
            return existing

    loader = SourceFileLoader(
        name,
        str(path)
    )

    spec = importlib.util.spec_from_loader(
        name,
        loader,
        origin=str(path)
    )

    if spec is None:
        raise ImportError(
            f"Cannot create module spec: {path}"
        )

    module = importlib.util.module_from_spec(
        spec
    )

    module.__file__ = str(path)

    previous = sys.modules.get(name)

    sys.modules[name] = module

    try:

        source = path.read_bytes()

        if not source.strip():
            raise ValueError(
                f"Empty Python file: {path.name}"
            )

        compiled = compile(
            source,
            str(path),
            "exec"
        )

        exec(
            compiled,
            module.__dict__,
            module.__dict__
        )

        module._source_mtime_ns = (
            path.stat().st_mtime_ns
        )

    except Exception:

        if previous is not None:
            sys.modules[name] = previous
        else:
            sys.modules.pop(name, None)

        raise

    return module


# =====================================
# LOAD V8.4 INTEGRATION FUNCTION
# =====================================

def load_v84():

    path = BASE_DIR / "app_v84_integrated.py"

    if not path.is_file():
        raise FileNotFoundError(
            "app_v84_integrated.py not found"
        )

    source = path.read_text(
        encoding="utf-8-sig"
    )

    tree = ast.parse(
        source,
        filename=str(path)
    )

    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "integrate"
    ]

    if len(functions) != 1:
        raise ValueError(
            "V8.4 integrate function not found"
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
# V8.5.3 INTEGRATION
# =====================================

def integrate_v85(source):

    integrate_v84 = load_v84()

    source = integrate_v84(source)

    source = source.replace(
        "NSE F&O PRO — ALL IN ONE V8.4",
        "NSE F&O PRO — ALL IN ONE V8.5.3"
    )

    source = source.replace(
        "V8.4 Live Data Engine",
        "V8.5.3 Live Data Engine"
    )

    extra = '''

# =====================================
# NSE INTELLIGENCE RESEARCH MODULE
# =====================================

intelligence_path = None
intelligence_file_error = ""

try:

    intelligence_path = locate_module(
        "nse_intelligence_engine"
    )

except Exception as exc:

    intelligence_file_error = str(exc)


with st.sidebar:

    st.divider()

    st.header("V8.5.3 Research Modules")

    st.caption(
        "Loader revision: " + REVISION
    )

    show_nse_intelligence = st.checkbox(
        "Show NSE Market Intelligence",
        value=False,
        key="v853_intelligence"
    )

    if intelligence_path is not None:

        st.success(
            "NSE Intelligence file detected"
        )

        st.caption(
            "File size: "
            + str(
                intelligence_path.stat().st_size
            )
            + " bytes"
        )

    else:

        st.error(
            "NSE Intelligence file missing"
        )

    with st.expander(
        "Deployment Diagnostics"
    ):

        st.write(
            "Loader revision:",
            REVISION
        )

        st.write(
            "Python version:",
            sys.version.split()[0]
        )

        st.write(
            "Repository folder:",
            str(BASE_DIR)
        )

        st.write(
            "Intelligence file:",
            str(intelligence_path)
        )

        st.write(
            "Available files:",
            available_files()
        )

        if intelligence_file_error:

            st.write(
                intelligence_file_error
            )


# =====================================
# RENDER INTELLIGENCE MODULE
# =====================================

if show_nse_intelligence:

    st.divider()

    st.header(
        "NSE Market Intelligence"
    )

    try:

        intelligence_module = (
            load_local_module(
                "nse_intelligence_engine"
            )
        )

        st.success(
            "NSE Intelligence Engine loaded"
        )

        render_function = getattr(
            intelligence_module,
            "render_intelligence",
            None
        )

        if not callable(render_function):

            functions_found = [
                name
                for name, obj in vars(
                    intelligence_module
                ).items()
                if inspect.isfunction(obj)
                and obj.__module__
                == intelligence_module.__name__
            ]

            raise AttributeError(
                "render_intelligence function "
                "not found. Available functions: "
                + str(functions_found)
            )

        st.caption(
            "Render function verified"
        )

        if not token:

            st.warning(
                "Enter Upstox Access Token "
                "to run intelligence research."
            )

        else:

            intelligence_universe = (
                get_fo_universe()
            )

            if not intelligence_universe:

                raise ValueError(
                    "F&O universe is empty"
                )

            signature = inspect.signature(
                render_function
            )

            kwargs = {
                "token": token,
                "universe": intelligence_universe
            }

            signature.bind(**kwargs)

            render_function(**kwargs)

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
            "Existing dashboard remains available. "
            "Intelligence data is not verified."
        )

        with st.expander(
            "Module Loading Diagnostics",
            expanded=True
        ):

            st.write(
                "Loader revision:",
                REVISION
            )

            st.write(
                "Error type:",
                type(exc).__name__
            )

            st.write(
                "Intelligence file:",
                str(intelligence_path)
            )

            st.write(
                "Available files:",
                available_files()
            )

            if isinstance(
                exc,
                ModuleNotFoundError
            ):

                st.write(
                    "Missing dependency:",
                    getattr(
                        exc,
                        "name",
                        "Unknown"
                    )
                )

            if isinstance(
                exc,
                SyntaxError
            ):

                st.write(
                    "Syntax error line:",
                    exc.lineno
                )

                st.write(
                    "Syntax error file:",
                    exc.filename
                )

    st.divider()


st.caption(
    "V8.5.3 | Live NSE Verification | "
    "Market Intelligence | Research Only | "
    "No Automatic Orders"
)

'''

    return source + "\n\n" + extra


# =====================================
# MAIN DASHBOARD LOADER
# =====================================

try:

    original = BASE_DIR / "app_v2.py"

    if not original.is_file():

        raise FileNotFoundError(
            "Original app_v2.py not found"
        )

    source = original.read_text(
        encoding="utf-8-sig"
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
        "V8.5.3 integration stopped safely: "
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
