import json
import time
import requests
import streamlit as st
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SERVERS = {
    "CM Market": "https://mcp.nseindia.in/cmmkt/mcp",
    "Bhavcopy": "https://mcp.nseindia.in/bhavcopy/cm/mcp"
}


class NSEClient:

    def __init__(self, url):
        self.url = url
        self.session = requests.Session()
        self.request_id = 0

        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        })

    def parse_response(self, response):

        response.raise_for_status()

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError:
            pass

        events = []

        for line in response.text.splitlines():
            if line.startswith("data:"):
                events.append(line[5:].strip())
            elif not line.strip() and events:
                try:
                    obj = json.loads("\n".join(events))
                    if "result" in obj or "error" in obj:
                        return obj
                except ValueError:
                    pass
                events = []

        if events:
            return json.loads("\n".join(events))

        raise RuntimeError("Invalid MCP response")

    def rpc(self, method, params=None):

        self.request_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {}
        }

        response = self.session.post(
            self.url,
            json=payload,
            timeout=(10, 60)
        )

        response.raise_for_status()

        session_id = response.headers.get(
            "Mcp-Session-Id"
        )

        if session_id:
            self.session.headers[
                "Mcp-Session-Id"
            ] = session_id

        data = self.parse_response(response)

        if "error" in data:
            raise RuntimeError(str(data["error"]))

        if data.get("id") != self.request_id:
            raise RuntimeError("MCP response ID mismatch")

        if "result" not in data:
            raise RuntimeError("Missing MCP result")

        return data["result"]

    def connect(self):

        result = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "fno-research-v8",
                    "version": "1.0"
                }
            }
        )

        self.session.headers[
            "MCP-Protocol-Version"
        ] = result.get(
            "protocolVersion",
            "2025-03-26"
        )

        response = self.session.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            },
            timeout=30
        )

        response.raise_for_status()

        return result

    def list_tools(self):

        tools = []
        cursor = None

        for _ in range(10):

            params = {}

            if cursor:
                params["cursor"] = cursor

            result = self.rpc(
                "tools/list",
                params
            )

            tools.extend(
                result.get("tools", [])
            )

            cursor = result.get("nextCursor")

            if not cursor:
                break

        return tools

    def call_tool(self, name, arguments):

        result = self.rpc(
            "tools/call",
            {
                "name": name,
                "arguments": arguments
            }
        )

        if result.get("isError"):
            raise RuntimeError(
                json.dumps(result, default=str)
            )

        return result

    def close(self):
        self.session.close()


def test_cases(symbol, date):

    return {
        "CM Market": [

            ("cm_get_data_status", {}),

            ("cm_get_allstocks_status", {}),

            ("cm_get_stock_quote", {
                "symbol": symbol
            }),

            ("cm_get_equity_stocks", {
                "limit": 500,
                "symbolFilter": ""
            }),

            ("cm_get_live_indices", {
                "group": "sectoral",
                "nameFilter": ""
            }),

            ("cm_get_index_quote", {
                "indexName": "NIFTY 50"
            }),

            ("cm_get_index_quote", {
                "indexName": "INDIA VIX"
            }),

            ("nse_get_market_movers", {
                "indexName": "",
                "limit": 50
            })
        ],

        "Bhavcopy": [

            ("get_corporate_actions", {
                "symbol": symbol,
                "fromDate": "2025-01-01",
                "toDate": date
            }),

            ("get_stock_history", {
                "symbol": symbol,
                "months": 3,
                "endDate": date
            }),

            ("get_volume_analysis", {
                "symbol": symbol,
                "days": 30
            }),

            ("get_bulk_quote", {
                "symbols": [
                    symbol,
                    "RELIANCE",
                    "INFY",
                    "HDFCBANK",
                    "TCS"
                ]
            }),

            ("get_market_breadth", {
                "date": date
            }),

            ("get_market_mood", {
                "date": date
            }),

            ("get_index_movers", {
                "date": date,
                "period": "1D",
                "n": 10,
                "scope": "equity"
            }),

            ("get_top_by_volume", {
                "date": date,
                "n": 50,
                "sortBy": "value"
            }),

            ("get_top_movers", {
                "date": date,
                "n": 50,
                "direction": "gain"
            }),

            ("get_top_movers", {
                "date": date,
                "n": 50,
                "direction": "loss"
            }),

            ("get_stock_vs_index", {
                "symbol": symbol,
                "indexName": "Nifty 50",
                "months": 1,
                "date": date
            })
        ]
    }


def inspect_result(result):

    parsed = []

    if result.get("structuredContent") is not None:
        parsed.append(result["structuredContent"])

    for item in result.get("content", []):

        if item.get("type") == "text":

            raw = item.get("text", "")

            try:
                parsed.append(json.loads(raw))
            except ValueError:
                if raw.strip():
                    parsed.append(raw)

    errors = []
    warnings = []
    timestamps = []

    def walk(obj):

        if isinstance(obj, dict):

            for key, value in obj.items():

                k = key.lower()

                if k in ("error", "errors") and value:
                    errors.append(str(value))

                if k in ("warning", "warnings") and value:
                    warnings.append(str(value))

                if k in (
                    "date",
                    "latesttimestamp",
                    "nsetimestamp",
                    "timestamp",
                    "lastcrawled",
                    "last_crawled"
                ) and value:
                    timestamps.append(str(value))

                walk(value)

        elif isinstance(obj, list):

            for item in obj:
                walk(item)

    walk(parsed)

    if errors:
        status = "DATA ERROR"
    elif not parsed:
        status = "EMPTY RESPONSE"
    elif warnings:
        status = "REVIEW WARNING"
    else:
        status = "RESPONSE RECEIVED"

    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "timestamps": list(dict.fromkeys(timestamps)),
        "parsed": parsed
    }


def run_validation(symbol, date):

    rows = []
    raw_results = {}
    cases = test_cases(symbol, date)

    total = sum(len(x) for x in cases.values())
    completed = 0

    progress = st.progress(0)
    message = st.empty()

    for server_name, url in SERVERS.items():

        client = NSEClient(url)

        try:

            client.connect()

            tools = client.list_tools()

            available = {
                tool["name"] for tool in tools
            }

            raw_results[server_name] = {
                "tools": tools,
                "responses": {}
            }

            for tool_name, arguments in cases[server_name]:

                message.info(
                    f"Testing {server_name}: {tool_name}"
                )

                started = time.perf_counter()

                row = {
                    "Server": server_name,
                    "Tool": tool_name,
                    "Arguments": json.dumps(arguments),
                    "Status": "",
                    "Response ms": 0,
                    "Response bytes": 0,
                    "Data timestamps": "",
                    "Warnings": "",
                    "Error": ""
                }

                try:

                    if tool_name not in available:
                        raise RuntimeError(
                            "Tool not available"
                        )

                    result = client.call_tool(
                        tool_name,
                        arguments
                    )

                    checked = inspect_result(result)

                    key = tool_name + "|" + json.dumps(
                        arguments,
                        sort_keys=True
                    )

                    raw_results[server_name][
                        "responses"
                    ][key] = result

                    row["Status"] = checked["status"]

                    row["Response bytes"] = len(
                        json.dumps(result).encode()
                    )

                    row["Data timestamps"] = " | ".join(
                        checked["timestamps"][:10]
                    )

                    row["Warnings"] = " | ".join(
                        checked["warnings"]
                    )

                    row["Error"] = " | ".join(
                        checked["errors"]
                    )

                except Exception as exc:

                    row["Status"] = "FAILED"
                    row["Error"] = str(exc)

                row["Response ms"] = round(
                    (time.perf_counter() - started) * 1000
                )

                row["Checked IST"] = datetime.now(
                    IST
                ).isoformat()

                rows.append(row)

                completed += 1
                progress.progress(completed / total)

                time.sleep(0.3)

        except Exception as exc:

            for tool_name, arguments in cases[server_name]:

                rows.append({
                    "Server": server_name,
                    "Tool": tool_name,
                    "Arguments": json.dumps(arguments),
                    "Status": "CONNECTION FAILED",
                    "Error": str(exc)
                })

                completed += 1
                progress.progress(completed / total)

        finally:
            client.close()

    message.empty()

    return pd.DataFrame(rows), raw_results


def render_mcp_validation():

    st.title("NSE MCP VALIDATION V8")

    st.caption(
        "Official NSE Data | Actual Tool Calls | Read Only"
    )

    st.info(
        "Tests actual MCP responses, not just "
        "HTTP connectivity. No automatic orders."
    )

    symbol = st.sidebar.text_input(
        "Test Stock",
        "DIXON"
    ).strip().upper()

    date = st.sidebar.date_input(
        "Research Date",
        datetime.now(IST).date()
    )

    if st.button(
        "RUN COMPLETE NSE MCP VALIDATION",
        type="primary"
    ):

        if not symbol:
            st.error("Enter a valid NSE symbol.")
            return

        report, raw = run_validation(
            symbol,
            date.isoformat()
        )

        st.session_state["mcp_report"] = report
        st.session_state["mcp_raw"] = raw

    if "mcp_report" not in st.session_state:
        return

    report = st.session_state["mcp_report"]
    raw = st.session_state["mcp_raw"]

    received = int(
        (report["Status"] == "RESPONSE RECEIVED").sum()
    )

    a, b, c = st.columns(3)

    a.metric("Tests", len(report))
    b.metric("Responses Received", received)
    c.metric("Require Review", len(report) - received)

    st.warning(
        "Response received does not mean data is "
        "fresh, complete or suitable for trading."
    )

    st.dataframe(
        report,
        use_container_width=True
    )

    st.download_button(
        "DOWNLOAD MCP VALIDATION REPORT",
        report.to_csv(index=False),
        file_name="nse_mcp_validation_v8.csv",
        mime="text/csv"
    )

    st.download_button(
        "DOWNLOAD COMPLETE NSE MCP DATA",
        json.dumps(
            raw,
            indent=2,
            ensure_ascii=False,
            default=str
        ),
        file_name="nse_mcp_actual_data_v8.json",
        mime="application/json"
    )

    for server, data in raw.items():

        with st.expander(server + " Raw Responses"):

            for name, response in data.get(
                "responses", {}
            ).items():

                st.write(name)
                st.json(response)


if __name__ == "__main__":

    st.set_page_config(
        page_title="NSE MCP VALIDATION V8",
        page_icon="📊",
        layout="wide"
    )

    render_mcp_validation()
