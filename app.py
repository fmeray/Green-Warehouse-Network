"""Streamlit dashboard for the Green Warehouse Network."""
from pathlib import Path
from uuid import uuid4

try:
    import plotly.express as px
    import streamlit as st
except ImportError:
    px = None
    st = None

from agent.graph import OpenAIConfigurationError, OpenAIRequestError, generate_report, get_graph
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from src.data import customers as load_customers, sites as load_sites
from src.map import plot_customers_only, plot_network_solutions
from src.schema import scenario_from_dict

ASSET_DIR = Path(__file__).with_name("logo")
LOGO_PATH = ASSET_DIR / "logo_icon.svg"
USER_ICON_PATH = ASSET_DIR / "user_icon.svg"


def _network_svg(result, customer_rows, site_rows):
    points = [(float(row["lat"]), float(row["lon"])) for row in customer_rows + site_rows]
    min_lat, max_lat = min(point[0] for point in points), max(point[0] for point in points)
    min_lon, max_lon = min(point[1] for point in points), max(point[1] for point in points)

    def project(lat, lon):
        x = 40 + 960 * (lon - min_lon) / max(max_lon - min_lon, 1e-9)
        y = 560 - 500 * (lat - min_lat) / max(max_lat - min_lat, 1e-9)
        return round(x, 1), round(y, 1)

    sites_by_id = {row["id"]: row for row in site_rows}
    customer_by_id = {row["id"]: row for row in customer_rows}
    lines = []
    for customer_id, assignment in result.get("assignments", {}).items():
        customer = customer_by_id.get(customer_id)
        if not customer:
            continue
        cx, cy = project(customer["lat"], customer["lon"])
        for site_id, dash in ((assignment.get("primary"), ""), (assignment.get("backup"), "6 6")):
            site = sites_by_id.get(site_id)
            if site:
                sx, sy = project(site["lat"], site["lon"])
                lines.append(f"<line x1='{cx}' y1='{cy}' x2='{sx}' y2='{sy}' stroke='#2d6a4f' stroke-width='1.5' stroke-dasharray='{dash}' opacity='.55'/>")
    nodes = []
    for customer in customer_rows:
        x, y = project(customer["lat"], customer["lon"])
        nodes.append(f"<circle cx='{x}' cy='{y}' r='4' fill='#277da1'/>")
    for site_id in result.get("open_sites", []):
        site = sites_by_id.get(site_id)
        if site:
            x, y = project(site["lat"], site["lon"])
            nodes.append(f"<rect x='{x - 6}' y='{y - 6}' width='12' height='12' fill='#f4a261'/>")
    return "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1040 600' role='img' aria-label='Network map'><rect width='1040' height='600' fill='#f1f8f4'/>" + "".join(lines + nodes) + "</svg>"


def _report_html(report_fragment, map_svg=""):
    """Wrap the LLM-generated report fragment in a downloadable HTML document."""
    sections = [
        "<!doctype html><html><head><meta charset='utf-8'><title>Green Warehouse Network report</title>",
        "<style>body{font-family:Arial,sans-serif;color:#1b4332;max-width:1100px;margin:2rem auto}"
        "table{border-collapse:collapse}td,th{border:1px solid #b7d7c4;padding:.4rem;text-align:left}"
        "pre{background:#f1f8f4;padding:1rem;overflow:auto}</style></head><body>",
        "<h1>Green Warehouse Network</h1>",
        report_fragment or "<p>No report was generated.</p>",
        "<p>Distance attribution: © openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors.</p>",
    ]
    if map_svg:
        sections.append(f"<h2>Network map</h2>{map_svg}")
    sections.append("</body></html>")
    return "".join(sections)


def explain_error(warning):
    """Render a structured tool warning without making the dashboard noisy."""
    if isinstance(warning, dict):
        message = warning.get("message")
        if message:
            return str(message)
        details = warning.get("details")
        if isinstance(details, list):
            return "\n".join(str(item) for item in details)
        if details:
            return str(details)
        return str(warning)
    return str(warning)


def _scenario_change_label(saved):
    """Return the compact scenario change shown in the scenarios table."""
    scenario = saved.get("scenario") or {}
    close_sites = scenario.get("close_sites", [])
    if close_sites:
        label = "Close site" if len(close_sites) == 1 else "Close sites"
        return f"{label}: {', '.join(close_sites)}"
    force_open_sites = scenario.get("force_open_sites", [])
    if force_open_sites:
        label = "Open site" if len(force_open_sites) == 1 else "Open sites"
        return f"{label}: {', '.join(force_open_sites)}"
    if scenario.get("emission_mode") == "electric":
        return "Use electric trucks"
    changed = []
    for field, default in (
        ("demand_scale", 1.0),
        ("capacity_scale", 1.0),
        ("failure_probability", 0.05),
        ("k", None),
    ):
        if scenario.get(field) != default:
            changed.append(f"{field}: {scenario.get(field)}")
    if changed:
        return "; ".join(changed)
    return "Default scenario"


def _session_scenarios():
    """Return completed scenarios from this Streamlit/LangGraph session."""
    agent_state = st.session_state.get("agent_state", {})
    saved = agent_state.get("saved_scenarios") or {}
    if saved:
        return saved
    fallback = {}
    for event in st.session_state.get("session_events", []):
        result = event.get("result") or {}
        scenario_id = result.get("scenario_id")
        if scenario_id and result.get("status") in {"optimal", "time_limit"}:
            fallback[scenario_id] = result
    return fallback


def main():
    if st is None:
        raise SystemExit("Install requirements.txt to run the dashboard.")
    st.set_page_config(page_title="Green Warehouse Network", layout="wide")
    st.image(str(LOGO_PATH), width=64)
    st.title("Green Warehouse Network")
    st.caption("Resilient facility-location planning with primary and backup coverage")

    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid4())
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    if "session_events" not in st.session_state:
        st.session_state.session_events = []
    if "objective_selection" not in st.session_state:
        st.session_state.objective_selection = "co2"
    if "confirmed_objective" not in st.session_state:
        st.session_state.confirmed_objective = None

    def reset_session():
        st.session_state.thread_id = str(uuid4())
        st.session_state.chat_history = []
        st.session_state.agent_state = {}
        st.session_state.awaiting_agent_reply = False
        st.session_state.session_events = []
        st.session_state.pop("result", None)
        st.session_state.pop("report_body", None)

    def invoke_agent(prompt, objective, show_user=True, execute_scenario=False):
        st.session_state.pop("report_body", None)
        if show_user:
            st.session_state.chat_history.append(("user", prompt))
        st.session_state.request_text = prompt
        try:
            graph = st.session_state.get("agent_graph") or get_graph()
            st.session_state.agent_graph = graph
            config = {"configurable": {"thread_id": st.session_state.thread_id}}
            current = dict(
                st.session_state.get("agent_state", {}).get("scenario")
                or scenario_from_dict({"objective": objective}).model_dump()
            )
            current["objective"] = objective
            if st.session_state.get("awaiting_agent_reply"):
                response = graph.invoke(Command(resume=prompt), config)
            else:
                response = graph.invoke(
                    {
                        "messages": [HumanMessage(content=prompt)],
                        "scenario": current,
                        "execute_scenario": execute_scenario,
                    },
                    config,
                )
            st.session_state.agent_state = response
            scenario_objective = (response.get("scenario") or {}).get("objective")
            if scenario_objective in {"co2", "cost"}:
                st.session_state.confirmed_objective = scenario_objective
            interruption = response.get("__interrupt__")
            if interruption:
                event = interruption[0]
                reply = str(getattr(event, "value", event))
                st.session_state.awaiting_agent_reply = True
            else:
                reply = response.get("reply", "The request was completed.")
                st.session_state.awaiting_agent_reply = False
            st.session_state.chat_history.append(("assistant", reply))
            st.session_state.session_events.append({
                "request": prompt,
                "objective": objective,
                "intent": response.get("intent"),
                "scenario": response.get("scenario"),
                "result": response.get("last_result"),
                "change_list": response.get("change_list"),
                "assistant_answer": reply,
                "interrupted": bool(interruption),
            })
        except OpenAIConfigurationError as exc:
            st.session_state.chat_history.append(("assistant", str(exc)))
            st.session_state.session_events.append({
                "request": prompt, "objective": objective, "error": str(exc),
            })
        except OpenAIRequestError as exc:
            st.session_state.chat_history.append(("assistant", str(exc)))
            st.session_state.session_events.append({
                "request": prompt, "objective": objective, "error": str(exc),
            })
        except Exception as exc:
            st.session_state.chat_history.append(("assistant", f"The agent could not complete the request: {exc}"))
            st.session_state.session_events.append({
                "request": prompt, "objective": objective, "error": str(exc),
            })

    with st.sidebar:
        st.subheader("Planning objective")
        objective_selection = st.selectbox(
            "Optimize for",
            ["co2", "cost"],
            key="objective_selection",
            format_func=lambda value: "CO₂" if value == "co2" else "Cost",
        )
        if st.button("Confirm objective", type="primary"):
            reset_session()
            st.session_state.confirmed_objective = objective_selection
            invoke_agent(
                "Run the default scenario using the confirmed objective. Launch the scenario tool now.",
                objective_selection,
                show_user=False,
                execute_scenario=True,
            )
            st.rerun()
        if st.button("Start over"):
            reset_session()
            st.session_state.confirmed_objective = None
            st.rerun()

        if st.session_state.confirmed_objective and objective_selection == st.session_state.confirmed_objective:
            st.caption(
                "Confirmed: "
                + ("CO₂" if st.session_state.confirmed_objective == "co2" else "Cost")
            )
        elif st.session_state.confirmed_objective:
            st.caption("Objective changed. Confirm the new choice to run the agent.")
        else:
            st.caption("Confirm an objective before the scenario agent can run a tool.")

    confirmed_objective = st.session_state.get("confirmed_objective")
    # Selecting a different objective invalidates the currently displayed
    # result until the user explicitly confirms the new choice.
    objective = (
        confirmed_objective
        if confirmed_objective == st.session_state.get("objective_selection")
        else None
    )

    for role, message in st.session_state.chat_history:
        avatar = str(LOGO_PATH) if role == "assistant" else str(USER_ICON_PATH)
        with st.chat_message(role, avatar=avatar):
            st.markdown(message)

    prompt = st.chat_input(
        "Describe a scenario for the confirmed objective",
        disabled=objective is None,
    )
    if prompt:
        invoke_agent(prompt, objective)
        st.rerun()

    result = (
        st.session_state.get("agent_state", {}).get("last_result")
        or st.session_state.get("result")
        or {}
    ) if objective else {}
    has_solution = result.get("status") in {"optimal", "time_limit"} and bool(result.get("assignments"))
    if result.get("tool_warnings"):
        for warning in result["tool_warnings"]:
            st.info(explain_error(warning))

    cols = st.columns(6)
    cols[0].metric("Annual cost", f"€{result['total_cost_eur']:,.0f}" if has_solution else "—")
    cols[1].metric("CO₂", f"{result['total_co2_t']:,.1f} t" if has_solution else "—")
    cols[2].metric("Open warehouses", result["k"] if has_solution else "—")
    cols[3].metric("Primary distance", f"{result['average_primary_km']:.1f} km" if has_solution else "—")
    cols[4].metric("Backup distance", f"{result['average_backup_km']:.1f} km" if has_solution else "—")
    cols[5].metric(
        "Carbon price",
        f"€{result.get('scenario', {}).get('carbon_price_eur_per_t', 80):,.0f}/t" if has_solution else "—",
    )
    if has_solution:
        st.caption(f"CBC status: {result.get('solver_status', result.get('status'))}; relative gap: {result.get('solver_relative_gap', 'n/a')}")
    if has_solution and "cost_of_avoiding_co2_eur_per_t" in result:
        st.info(f"Cost of avoiding CO₂: €{result['cost_of_avoiding_co2_eur_per_t']:,.2f}/t against the carbon price.")
    elif has_solution and "cost_of_avoiding_co2_solver_gap" in result:
        st.info(f"Cost of avoiding CO₂ is unavailable; solver gap: {result['cost_of_avoiding_co2_solver_gap']}.")
    customer_rows = load_customers()
    scenario_data = result.get("scenario", {}) if has_solution else {}
    removed = set(scenario_data.get("remove_customers", []))
    customer_rows = [customer for customer in customer_rows if customer["id"] not in removed]
    for customer in customer_rows:
        customer["demand"] *= float(scenario_data.get("demand_scale", 1.0))
    customer_rows.extend(scenario_data.get("add_customers", []))
    map_object = (
        plot_network_solutions(customer_rows, load_sites(), result)
        if has_solution
        else plot_customers_only(customer_rows)
    )
    map_html = map_object.get_root().render()
    if objective and st.session_state.get("session_events"):
        if st.button("Generate report with LLM"):
            try:
                st.session_state.report_body = generate_report(
                    st.session_state.session_events,
                    objective,
                )
            except OpenAIConfigurationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"The report could not be generated: {exc}")
        report_body = st.session_state.get("report_body")
        if report_body:
            st.subheader("LLM-generated report")
            st.markdown(report_body, unsafe_allow_html=True)
            report = _report_html(
                report_body,
                _network_svg(result, customer_rows, load_sites()) if has_solution else "",
            )
            st.download_button(
                "Download report",
                report,
                file_name="green-warehouse-network-report.html",
                mime="text/html",
            )

    views = ["Map"]
    if has_solution and result.get("k_sweep"):
        views.append("k curve")
    if has_solution:
        views.append("Scenarios")
    tabs = st.tabs(views)
    for name, tab in zip(views, tabs):
        with tab:
            if name == "Map":
                st.components.v1.html(map_html, height=720, scrolling=False)
                if has_solution:
                    st.dataframe([{"customer_id": cid, **assignment} for cid, assignment in result["assignments"].items()], hide_index=True, use_container_width=True)
            elif name == "k curve":
                sweep = [item for item in result["k_sweep"] if item.get("solver_status") == "optimal"]
                if sweep and px is not None:
                    st.plotly_chart(px.line(sweep, x="k", y=["total_cost_eur", "total_co2_t"], markers=True), use_container_width=True)
                st.dataframe(sweep, hide_index=True, use_container_width=True)
            elif name == "Scenarios":
                saved_scenarios = _session_scenarios()
                rows = [
                    {
                        "scenario_id": scenario_id,
                        "objective": saved.get("objective"),
                        "what_changed": _scenario_change_label(saved),
                        "k": saved.get("k"),
                        "total_cost_eur": saved.get("total_cost_eur"),
                        "total_co2_t": saved.get("total_co2_t"),
                    }
                    for scenario_id, saved in saved_scenarios.items()
                ]
                if rows:
                    st.dataframe(rows, hide_index=True, use_container_width=True)
                else:
                    st.info("No completed scenarios in this session yet.")

    st.caption("Distance attribution: © openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors.")


if __name__ == "__main__":
    main()
