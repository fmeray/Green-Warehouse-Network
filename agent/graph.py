"""LangGraph scenario agent for the Green Warehouse Network."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
from typing import Any, TypedDict

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path):
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from typing_extensions import Annotated

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from src.data import customers as base_customers, sites as candidate_sites
from src.schema import scenario_from_dict
from agent.schema import IntentEnvelope, PartialScenario
from src.tools import get_baseline, run_scenario, validate_scenario

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))


class OpenAIConfigurationError(RuntimeError):
    """The agent cannot start because the OpenAI key is not configured."""


class OpenAIRequestError(RuntimeError):
    """The configured OpenAI service could not complete an agent request."""


class ScenarioToolInput(BaseModel):
    """Arguments exposed to the LLM's scenario execution tool."""

    params: dict[str, Any] = Field(default_factory=dict)


def _run_scenario_tool(params: dict[str, Any]) -> dict[str, Any]:
    """LangChain tool wrapper around the deterministic scenario solver."""
    return run_scenario(params)


RUN_SCENARIO_TOOL = StructuredTool.from_function(
    func=_run_scenario_tool,
    name="run_scenario",
    description=(
        "Solve the confirmed Green Warehouse Network scenario. Pass the full "
        "scenario JSON in params, including the confirmed objective."
    ),
    args_schema=ScenarioToolInput,
)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[Any], add_messages]
    scenario: dict[str, Any]
    execute_scenario: bool
    baseline: dict[str, Any]
    last_result: dict[str, Any]
    saved_scenarios: dict[str, dict[str, Any]]
    pending_question: str | None
    intent: str
    partial_scenario: dict[str, Any]
    request: str
    validation: dict[str, Any]
    previous_result: dict[str, Any] | None
    change_list: dict[str, Any]
    ask_kind: str | None
    ask_action: str | None
    changed_fields: list[str]
    direct_reply: str
    reply: str


def _dump(value: Any, *, exclude_none: bool = False) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=exclude_none)
    if isinstance(value, dict):
        return {key: val for key, val in value.items() if not (exclude_none and val is None)}
    return {}


def _default_scenario() -> dict[str, Any]:
    return scenario_from_dict({}).model_dump()


def _content(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _last_user_message(state: AgentState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) or getattr(message, "type", None) == "human":
            return _content(message)
    return state.get("request", "")


def _chat_model() -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIConfigurationError(
            "OPENAI_API_KEY is missing. Add it to .env before using the chat agent."
        )
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    return ChatOpenAI(model=model, api_key=api_key, temperature=0)


def _structured_intent(model: Any, user_text: str, current: dict[str, Any]) -> IntentEnvelope:
    site_catalog = [
        {"id": site["id"], "name": site["name"]}
        for site in candidate_sites()
    ]
    prompt = (
        "Extract the user's intent and only the scenario fields explicitly mentioned. "
        "Never invent values. Vehicle dispatch routes, driver routing, inventory, and stock "
        "requests are out_of_scope. Customer-to-warehouse assignments, network routes, "
        "warehouse closures, and questions about a previous assignment result are in scope. "
        "A follow-up may change one field while retaining the current scenario. "
        "Any hypothetical containing a concrete scenario change, including 'what happens if' "
        "or 'what if', is intent scenario, not intent question. If the user only asks for "
        "information about an existing result, use question. If the user only confirms an "
        "objective or asks to run the default scenario, return an empty scenario object and "
        "never invent a warehouse closure or other change.\n\n"
        "Few-shot examples. Return only the fields explicitly requested:\n"
        "User: What happens if we change trucks to electric? -> "
        '{"intent":"scenario","scenario":{"emission_mode":"electric"}}\n'
        "User: Switch the fleet to e-trucks. -> "
        '{"intent":"scenario","scenario":{"emission_mode":"electric"}}\n'
        "User: What if demand grows by 30 percent? -> "
        '{"intent":"scenario","scenario":{"demand_scale":1.3}}\n'
        "User: Increase customer volume 30%. -> "
        '{"intent":"scenario","scenario":{"demand_scale":1.3}}\n'
        "User: What happens if Hamburg warehouse closes? -> "
        '{"intent":"scenario","scenario":{"close_sites":["W01"]}}\n'
        "User: Keep backups within 200 km. -> "
        '{"intent":"scenario","scenario":{"max_backup_km":200}}\n'
        "User: Minimize annual spend. -> "
        '{"intent":"scenario","scenario":{"objective":"cost"}}\n'
        "User: Why did the assignments change? -> "
        '{"intent":"question","scenario":{},"question":"Why did the assignments change?"}\n\n'
        "Candidate warehouse catalog: resolve city or warehouse names to their IDs. "
        "For example, Hamburg logistics hub means W01. Return IDs, not names, in "
        "close_sites and force_open_sites.\n"
        f"{json.dumps(site_catalog, sort_keys=True)}\n\n"
        f"Current scenario JSON:\n{json.dumps(current, sort_keys=True, default=str)}\n\n"
        f"User message:\n{user_text}"
    )
    try:
        structured = model.with_structured_output(IntentEnvelope)
        response = structured.invoke([
            SystemMessage(content="You classify Green Warehouse Network requests."),
            HumanMessage(content=prompt),
        ])
        envelope = response if isinstance(response, IntentEnvelope) else IntentEnvelope.model_validate(response)
    except Exception as exc:
        raise OpenAIRequestError(f"OpenAI intent parsing failed: {exc}") from exc

    # Keep the LLM responsible for understanding the request, while making
    # the generic city/name-to-ID conversion deterministic before validation.
    by_alias = {}
    for site in site_catalog:
        site_id = site["id"]
        name = site["name"].lower()
        city = name.split()[0]
        by_alias[site_id.lower()] = site_id
        by_alias[name] = site_id
        by_alias[city] = site_id

    scenario_data = envelope.scenario.model_dump()
    # A structured response with scenario fields is authoritative even if a
    # model also labels the natural-language request as a question.
    if envelope.intent == "question" and any(value is not None for value in scenario_data.values()):
        envelope = IntentEnvelope.model_validate({
            **envelope.model_dump(),
            "intent": "scenario",
            "question": None,
        })
    for field in ("close_sites", "force_open_sites"):
        values = scenario_data.get(field)
        if values is not None:
            normalized = []
            for value in values:
                text = " ".join(str(value).lower().replace("-", " ").split())
                resolved = by_alias.get(text)
                if resolved is None:
                    resolved = next(
                        (
                            site_id
                            for alias, site_id in by_alias.items()
                            if len(alias) > 2 and text.startswith(alias + " ")
                        ),
                        value,
                    )
                normalized.append(resolved)
            scenario_data[field] = normalized
    return IntentEnvelope.model_validate({**envelope.model_dump(), "scenario": scenario_data})


def _merge_scenario(
    current: dict[str, Any],
    partial: dict[str, Any],
    *,
    cumulative: bool = True,
) -> dict[str, Any]:
    merged = {**_default_scenario(), **current}
    for key, value in partial.items():
        if value is not None:
            merged[key] = value
    # Scenario chat is incremental. Opening a site later in the same session
    # reverses an earlier closure, and closing it later reverses an earlier
    # force-open request; the two constraint lists must never overlap.
    requested_close = partial.get("close_sites")
    requested_open = partial.get("force_open_sites")
    if cumulative:
        close_sites = list(dict.fromkeys((current.get("close_sites") or []) + (requested_close or [])))
        force_open_sites = list(dict.fromkeys((current.get("force_open_sites") or []) + (requested_open or [])))
    else:
        close_sites = list(requested_close) if requested_close is not None else list(current.get("close_sites") or [])
        force_open_sites = list(requested_open) if requested_open is not None else list(current.get("force_open_sites") or [])
    if requested_open is not None:
        close_sites = [site for site in close_sites if site not in set(requested_open)]
    if requested_close is not None:
        force_open_sites = [site for site in force_open_sites if site not in set(requested_close)]
    merged["close_sites"] = close_sites
    merged["force_open_sites"] = force_open_sites
    if cumulative and "remove_customers" in partial and partial["remove_customers"] is not None:
        merged["remove_customers"] = list(dict.fromkeys((current.get("remove_customers") or []) + partial["remove_customers"]))
    if cumulative and partial.get("add_customers") is not None:
        existing = {item.get("id") for item in current.get("add_customers", []) if isinstance(item, dict)}
        merged["add_customers"] = list(current.get("add_customers", [])) + [
            item for item in partial["add_customers"]
            if not isinstance(item, dict) or item.get("id") not in existing
        ]
    return merged


SCENARIO_FIELD_LABELS = {
    "objective": "optimization objective",
    "k_sweep": "warehouse-count curve",
    "compare_objectives": "objective comparison",
    "close_sites": "warehouse closures",
    "force_open_sites": "required open warehouses",
    "k": "number of warehouses",
    "capacity_scale": "warehouse capacity",
    "demand_scale": "customer demand",
    "remove_customers": "customer removals",
    "add_customers": "new customers",
    "emission_mode": "truck emission mode",
    "emission_factor_scale": "vehicle emissions factor",
    "warehouse_co2_scale": "warehouse CO2",
    "carbon_price_eur_per_t": "carbon price",
    "transport_cost_eur_per_tkm": "transport cost",
    "max_backup_km": "backup distance",
    "failure_probability": "failure probability",
}


def _changed_fields(current: dict[str, Any], partial: dict[str, Any]) -> list[str]:
    """Compare explicit LLM values with the current scenario."""
    changed = []
    additive_lists = {"close_sites", "force_open_sites", "remove_customers"}
    for field, requested in partial.items():
        if requested is None:
            continue
        current_value = current.get(field)
        if field in additive_lists:
            if any(item not in (current_value or []) for item in requested):
                changed.append(field)
        elif field == "add_customers":
            current_ids = {
                item.get("id") for item in (current_value or []) if isinstance(item, dict)
            }
            if any(
                not isinstance(item, dict) or item.get("id") not in current_ids
                for item in requested
            ):
                changed.append(field)
        elif current_value != requested:
            changed.append(field)
    return changed


def _no_change_reply(partial: dict[str, Any], current: dict[str, Any], changed: list[str]) -> str:
    if not partial:
        fields = ", ".join(SCENARIO_FIELD_LABELS.values())
        return f"I didn't find anything to change. I can change {fields}."
    if not changed and len(partial) == 1:
        field = next(iter(partial))
        value = current.get(field)
        if field == "emission_mode":
            value_text = "electric trucks" if value == "electric" else "diesel trucks"
        elif field == "max_backup_km":
            value_text = f"the backup distance at {value:g} km"
        elif field.endswith("_scale"):
            value_text = f"the {SCENARIO_FIELD_LABELS[field]} at {value:g}x"
        else:
            value_text = str(value)
        return f"Already {value_text}, nothing changes."
    return "Already at the requested values, nothing changes."


def _number_check(reply: str, tool_json: dict[str, Any]) -> bool:
    # LLMs commonly add thousands separators to valid tool numbers. Compare
    # numeric text after removing those separators so formatting does not force
    # the response into the robotic fallback summary.
    serialized = json.dumps(tool_json, ensure_ascii=False, sort_keys=True, default=str)
    serialized = re.sub(r"(?<=\d),(?=\d)", "", serialized)
    reply = re.sub(r"(?<=\d),(?=\d)", "", reply)
    numbers = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", reply)
    money_values = []

    def collect_money(value: Any, path: str = ""):
        if isinstance(value, dict):
            for key, item in value.items():
                collect_money(item, f"{path}.{key}".lower())
        elif isinstance(value, (int, float)) and any(
            token in path for token in ("cost", "eur", "price")
        ):
            money_values.append(float(value))

    collect_money(tool_json)
    for number in numbers:
        if number in serialized:
            continue
        try:
            rounded_money = any(abs(float(number) - value) < 0.5 for value in money_values)
        except ValueError:
            rounded_money = False
        if not rounded_money:
            return False
    return True


def _format_eur(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _normalize_markdown_tables(text: str) -> str:
    """Remove blank lines inside Markdown tables before Streamlit renders them."""
    lines = text.strip().splitlines()
    normalized = []
    for index, line in enumerate(lines):
        if not line.strip():
            previous = next((item for item in reversed(normalized) if item.strip()), "")
            following = next((item for item in lines[index + 1:] if item.strip()), "")
            if previous.lstrip().startswith("|") and following.lstrip().startswith("|"):
                continue
        normalized.append(line)
    return "\n".join(normalized).strip()


def _template(result: dict[str, Any], change_list: dict[str, Any]) -> str:
    if result.get("error_type") == "out_of_scope":
        return str((result.get("details") or ["That request is outside this tool's scope."])[0])
    if result.get("status") == "infeasible":
        reason = (result.get("details") or ["the requested constraints have no feasible solution"])[0]
        fix = result.get("suggested_fix") or {}
        return f"The scenario is infeasible because {reason} A possible fix is {fix}."
    if result.get("status") in {"optimal", "time_limit"}:
        impact = change_list.get("impact_vs_previous", {})
        delta_cost = (impact.get("cost_change_eur") or {}).get("absolute")
        delta_co2 = (impact.get("co2_change_t") or {}).get("absolute")
        sites_closed = [
            f"{item.get('id')} ({item.get('name')})"
            for item in change_list.get("sites_closed_details", [])
        ] or change_list.get("sites_closed", [])
        sites_opened = [
            f"{item.get('id')} ({item.get('name')})"
            for item in change_list.get("sites_opened_details", [])
        ] or change_list.get("sites_opened", [])
        affected = change_list.get("customers_with_assignment_changes", [])
        parameter_changes = [
            item for item in change_list.get("parameters_changed", [])
            if item.get("field") not in {"objective", "close_sites", "force_open_sites"}
            and item.get("old") != item.get("new")
        ]
        lines = [
            f"I ran the {result.get('objective', 'CO2')} plan with {result.get('k')} warehouses. "
            f"The new plan costs €{_format_eur(result.get('total_cost_eur'))} per year and emits {result.get('total_co2_t')} t of CO₂.",
        ]
        if parameter_changes:
            details = "; ".join(
                f"{item.get('field')} changed from "
                f"{_format_eur(item.get('old')) if item.get('field') in {'carbon_price_eur_per_t', 'transport_cost_eur_per_tkm'} else item.get('old')} "
                f"to {_format_eur(item.get('new')) if item.get('field') in {'carbon_price_eur_per_t', 'transport_cost_eur_per_tkm'} else item.get('new')}"
                for item in parameter_changes
            )
            lines.insert(0, f"I applied the requested change: {details}.")
        if sites_closed or sites_opened:
            lines.append(
                f"The warehouse change closes {', '.join(sites_closed) or 'no sites'} "
                f"and opens {', '.join(sites_opened) or 'no additional sites'}."
            )
        for request in change_list.get("requested_site_status", []):
            if request.get("before") == "not open" and request.get("after") == "not open":
                lines.append(
                    f"{request.get('name', request.get('site_id'))} was already not open "
                    "in the previous plan, so no customer reassignment was needed for it."
                )
        if affected:
            lines.extend([
                "Affected customer assignments:",
                "| Customer | Before primary / backup | After primary / backup | CO₂ change (t) | Cost change (€) |",
                "| --- | --- | --- | ---: | ---: |",
            ])
            for item in affected:
                lines.append(
                    f"| {item.get('customer_name', item.get('customer_id'))} "
                    f"| {item.get('old_primary')} / {item.get('old_backup')} "
                    f"| {item.get('new_primary')} / {item.get('new_backup')} "
                    f"| {item.get('co2_change_t', {}).get('absolute', '—')} "
                    f"| {_format_eur(item.get('cost_change_eur', {}).get('absolute', '—'))} |"
                )
        if delta_cost is not None or delta_co2 is not None:
            if (delta_cost is None or abs(float(delta_cost)) < 0.005) and (delta_co2 is None or abs(float(delta_co2)) < 0.0005):
                lines.append("No change since the previous run.")
            else:
                lines.append(
                    f"Compared with the previous plan, the change is "
                    f"{_format_eur(delta_cost) if delta_cost is not None else '—'} € and "
                    f"{delta_co2 if delta_co2 is not None else '—'} t CO₂."
                )
        return _normalize_markdown_tables("\n\n".join(lines))
    return str((result.get("details") or ["The request could not be completed."])[0])


def _explain(
    model: Any,
    result: dict[str, Any],
    change_list: dict[str, Any],
    request: str = "",
) -> str:
    tool_json = {"request": request, **result, "change_list": change_list}
    prompt = (
        "Answer the user's scenario request as a warm, clear operations consultant, "
        "not as a status bot. Start with a natural takeaway that refers to what the "
        "user asked. Use only the tool JSON below. When a warehouse is closed or "
        "assignments change, explain which site was closed, which site or sites now "
        "carry the work, and which customers were affected. Include a Markdown table "
        "with customer, before primary/backup assignment, after primary/backup assignment, CO2 change, and cost "
        "change when those facts exist. Explain the overall cost and CO2 change and "
        "any capacity or feasibility consequence. If a requested site was already "
        "not open, say clearly that the request produced no assignment change and "
        "do not attribute unrelated changes to it. Use impact_vs_previous and the "
        "supplied before/after deltas; do not calculate or invent numbers. Round all "
        "money amounts to whole euros, keep CO2 values faithful to the tool output, "
        "and say 'No change since the previous run.' when both previous-run deltas are zero. "
        "If the user changed a planning parameter such as emission_mode, name that "
        "change explicitly near the beginning instead of saying only that the plan ran. "
        "Thousands separators are allowed, but values must remain faithful to the JSON. "
        "Avoid generic wording such as 'Solved the scenario' when a more human explanation "
        "is possible.\n\n"
        + json.dumps(tool_json, ensure_ascii=False, sort_keys=True, default=str)
    )
    try:
        response = model.invoke([
            SystemMessage(content="You explain Green Warehouse Network tool results."),
            HumanMessage(content=prompt),
        ])
    except Exception as exc:
        raise OpenAIRequestError(f"OpenAI response generation failed: {exc}") from exc
    reply = _normalize_markdown_tables(_content(response))
    if len(reply.split()) <= 240 and _number_check(reply, tool_json):
        return reply
    try:
        response = model.invoke([
            SystemMessage(content="Regenerate the answer. Use only the supplied tool JSON."),
            HumanMessage(content=prompt + "\nThe previous answer failed the number check."),
        ])
    except Exception as exc:
        raise OpenAIRequestError(f"OpenAI response regeneration failed: {exc}") from exc
    reply = _normalize_markdown_tables(_content(response))
    if len(reply.split()) <= 240 and _number_check(reply, tool_json):
        return reply
    return _template(result, change_list)


def _metric_snapshot(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    return {
        key: result[key]
        for key in ("total_cost_eur", "total_co2_t", "k", "open_sites")
        if key in result
    }


def _metric_deltas(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    deltas = {}
    for key in ("total_cost_eur", "total_co2_t", "k"):
        if key not in old or key not in new:
            continue
        delta = new[key] - old[key]
        percent = (100 * delta / old[key]) if old[key] else None
        deltas[key] = {"absolute": round(delta, 3), "percent": round(percent, 2) if percent is not None else None}
    return deltas


def _change_list(state: AgentState) -> dict[str, Any]:
    result = state.get("last_result", {})
    previous = state.get("previous_result") or {}
    baseline = state.get("baseline") or {}
    baseline_network = baseline.get("network", baseline)
    old_scenario = previous.get("scenario") or (_default_scenario() if baseline else {})
    new_scenario = state.get("scenario", {})
    parameters = [
        {"field": key, "old": old_scenario.get(key), "new": value}
        for key, value in new_scenario.items()
        if old_scenario and old_scenario.get(key) != value
    ]
    old_sites = set(previous.get("open_sites", []))
    new_sites = set(result.get("open_sites", []))
    site_names = {site["id"]: site.get("name", site["id"]) for site in candidate_sites()}
    previous_close_sites = set((previous.get("scenario") or {}).get("close_sites", []))
    requested_site_status = [
        {
            "site_id": site_id,
            "name": site_names.get(site_id, site_id),
            "action": "close",
            "before": "open" if site_id in old_sites else "not open",
            "after": "open" if site_id in new_sites else "not open",
        }
        for site_id in new_scenario.get("close_sites", [])
        if site_id not in previous_close_sites
    ]
    assignments_changed = []
    customer_names = {
        customer["id"]: customer.get("name", customer["id"])
        for customer in base_customers()
    }
    for customer in new_scenario.get("add_customers", []):
        if isinstance(customer, dict):
            customer_names[customer["id"]] = customer.get("name", customer["id"])
    for customer_id, assignment in result.get("assignments", {}).items():
        old = previous.get("assignments", {}).get(customer_id, {})
        if old and (old.get("primary") != assignment.get("primary") or old.get("backup") != assignment.get("backup")):
            old_probability = old_scenario.get("failure_probability", 0.05)
            new_probability = new_scenario.get("failure_probability", 0.05)
            old_cost = old.get("primary_transport_cost", 0) + old_probability * old.get("backup_transport_cost", 0)
            new_cost = assignment.get("primary_transport_cost", 0) + new_probability * assignment.get("backup_transport_cost", 0)
            old_co2 = old.get("primary_co2", 0) + old_probability * old.get("backup_co2", 0)
            new_co2 = assignment.get("primary_co2", 0) + new_probability * assignment.get("backup_co2", 0)
            assignments_changed.append({
                "customer_id": customer_id,
                "customer_name": customer_names.get(customer_id, customer_id),
                "old_primary": old.get("primary"),
                "new_primary": assignment.get("primary"),
                "old_backup": old.get("backup"),
                "new_backup": assignment.get("backup"),
                "old_primary_km": old.get("primary_km"),
                "new_primary_km": assignment.get("primary_km"),
                "old_backup_km": old.get("backup_km"),
                "new_backup_km": assignment.get("backup_km"),
                "primary_distance_change_km": round(
                    assignment.get("primary_km", 0) - old.get("primary_km", 0), 3
                ),
                "backup_distance_change_km": round(
                    assignment.get("backup_km", 0) - old.get("backup_km", 0), 3
                ),
                "cost_change_eur": {"absolute": round(new_cost - old_cost, 2)},
                "co2_change_t": {"absolute": round(new_co2 - old_co2, 3)},
            })
    previous_metrics = _metric_snapshot(previous)
    new_metrics = _metric_snapshot(result)
    deltas = _metric_deltas(previous_metrics, new_metrics)
    return {
        "parameters_changed": parameters,
        "against_previous": {"old": previous_metrics, "new": new_metrics, "deltas": deltas},
        "impact_vs_previous": {
            "cost_change_eur": deltas.get("total_cost_eur"),
            "co2_change_t": deltas.get("total_co2_t"),
        },
        "against_baseline": {"baseline": _metric_snapshot(baseline_network), "new": new_metrics, "deltas": _metric_deltas(_metric_snapshot(baseline_network), new_metrics)},
        "sites_opened": sorted(new_sites - old_sites),
        "sites_closed": sorted(old_sites - new_sites),
        "sites_opened_details": [
            {"id": site_id, "name": site_names.get(site_id, site_id)}
            for site_id in sorted(new_sites - old_sites)
        ],
        "sites_closed_details": [
            {"id": site_id, "name": site_names.get(site_id, site_id)}
            for site_id in sorted(old_sites - new_sites)
        ],
        "requested_site_status": requested_site_status,
        "customers_affected_count": len(assignments_changed),
        "customers_with_assignment_changes": assignments_changed,
    }


def generate_report(
    session_events: list[dict[str, Any]],
    objective: str,
    chat_model: Any = None,
) -> str:
    """Ask the LLM to write a report from the tools run in this session."""
    model = chat_model or _chat_model()
    report_data = {
        "confirmed_objective": objective,
        "session_events": session_events,
    }
    prompt = (
        "Create a professional Green Warehouse Network report from the supplied "
        "session record. Return only an HTML fragment suitable for placing inside "
        "a report body: headings, paragraphs, lists, and tables are allowed. "
        "Cover the confirmed objective, every scenario request, every solver/tool "
        "result, changes between scenarios, CO2 and cost impacts, feasibility or "
        "warnings, and a concise conclusion. Use only facts present in the JSON; "
        "do not invent, calculate, or silently omit a failed or infeasible run. "
        "If a value is unavailable, say so. Do not include markdown fences, CSS, "
        "scripts, or a full html/head/body document.\n\n"
        + json.dumps(report_data, ensure_ascii=False, sort_keys=True, default=str)
    )
    try:
        response = model.invoke([
            SystemMessage(content="You are the report writer for Green Warehouse Network."),
            HumanMessage(content=prompt),
        ])
    except Exception as exc:
        raise OpenAIRequestError(f"OpenAI report generation failed: {exc}") from exc
    report = _content(response).strip()
    if report.startswith("```"):
        report = re.sub(r"^```(?:html)?\s*|\s*```$", "", report, flags=re.IGNORECASE).strip()
    return report


def _call_scenario_tool(model: Any, scenario: dict[str, Any]):
    """Let the chat model choose and launch the scenario tool.

    Production requests must use the model's tool-calling interface. There is
    no parser or solver fallback when the model cannot make that call.
    """
    if not hasattr(model, "bind_tools"):
        raise OpenAIRequestError("The configured OpenAI model does not support required tool calling.")

    prompt = (
        "You are the orchestrator for the Green Warehouse Network. The user "
        "has confirmed the objective. You must call the run_scenario tool with "
        "the exact scenario JSON below. Do not solve the optimization yourself "
        "and do not answer without making the tool call.\n\n"
        f"Scenario JSON:\n{json.dumps(scenario, sort_keys=True, default=str)}"
    )
    try:
        try:
            bound = model.bind_tools([RUN_SCENARIO_TOOL], tool_choice="required")
        except TypeError:
            bound = model.bind_tools([RUN_SCENARIO_TOOL])
        response = bound.invoke([
            SystemMessage(content="Use the available tool as the scenario orchestrator."),
            HumanMessage(content=prompt),
        ])
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            raise RuntimeError("The LLM did not launch the run_scenario tool.")
        call = calls[0]
        arguments = call.get("args") or {}
        params = arguments.get("params", arguments)
        if isinstance(params, str):
            params = json.loads(params)
    except Exception as exc:
        raise OpenAIRequestError(f"OpenAI scenario orchestration failed: {exc}") from exc
    result = RUN_SCENARIO_TOOL.invoke({"params": params})
    tool_message = ToolMessage(
        content=json.dumps(result, ensure_ascii=False, default=str),
        tool_call_id=call.get("id", "run_scenario"),
        name="run_scenario",
    )
    return result, [response, tool_message]


def build_graph(chat_model: Any = None, checkpointer: Any = None):
    """Build an injectable graph; tests can pass a fake chat model."""
    model = chat_model or _chat_model()

    def parse_intent(state: AgentState):
        current = state.get("scenario") or _default_scenario()
        if state.get("execute_scenario"):
            # The objective-confirmation button is a control action, not a
            # user scenario. Do not ask the LLM to interpret it or let it
            # invent changes such as closing a warehouse.
            return {
                "intent": "scenario",
                "partial_scenario": {},
                "scenario": current,
                "request": _last_user_message(state),
                "last_result": state.get("last_result"),
                "pending_question": None,
                "ask_kind": None,
                "execute_scenario": False,
            }
        envelope = _structured_intent(model, _last_user_message(state), current)
        partial = _dump(envelope.scenario, exclude_none=True)
        scenario = _default_scenario() if envelope.intent == "reset" else _merge_scenario(current, partial)
        changed_fields = _changed_fields(current, partial)
        result = state.get("last_result")
        if envelope.intent == "out_of_scope":
            result = {"ok": False, "error_type": "out_of_scope", "code": "out_of_scope", "status": "out_of_scope", "details": ["Routing and inventory planning are outside this tool's scope."]}
        direct_reply = None
        if envelope.intent == "scenario" and not changed_fields:
            direct_reply = _no_change_reply(partial, current, changed_fields)
        return {
            "intent": envelope.intent,
            "partial_scenario": partial,
            "scenario": scenario,
            "request": _last_user_message(state),
            "last_result": result,
            "changed_fields": changed_fields,
            "direct_reply": direct_reply,
            "pending_question": envelope.question if envelope.intent == "question" else None,
            "ask_kind": "clarification" if envelope.intent == "question" else None,
        }

    def validate(state: AgentState):
        try:
            scenario = scenario_from_dict(state.get("scenario") or _default_scenario())
        except Exception as exc:
            scenario = None
            raw_errors = exc.errors() if hasattr(exc, "errors") else []
            field_errors = []
            for item in raw_errors:
                location = item.get("loc") or []
                field = ".".join(str(part) for part in location) or "scenario"
                field_errors.append({
                    "field": field,
                    "message": f"{field}: {item.get('msg', str(exc))}",
                })
            validation = {
                "valid": False,
                "details": field_errors or [{"message": str(exc)}],
                "field_errors": field_errors,
                "warnings": [],
            }
        else:
            validation = validate_scenario(scenario)
        # Do not solve or load a baseline while the user is still describing
        # the scenario. The confirmed objective is executed by run_model.
        baseline = state.get("baseline") or {}
        objective = scenario.objective if scenario is not None else "co2"
        if not validation.get("valid"):
            details = validation.get("details") or validation.get("errors") or ["The scenario is invalid."]
            first = details[0] if isinstance(details[0], dict) else {"message": details[0]}
            question = first.get("message", str(first)) + " Which valid value should I use?"
            return {"validation": validation, "baseline": baseline, "ask_kind": "validation", "pending_question": question}
        return {"validation": validation, "baseline": baseline, "pending_question": None, "ask_kind": None}

    def run_model(state: AgentState):
        previous = state.get("last_result")
        scenario = state.get("scenario") or _default_scenario()
        previous_scenario = (previous or {}).get("scenario") or {}
        newly_closed = set(scenario.get("close_sites", [])) - set(previous_scenario.get("close_sites", []))
        other_changes = {
            key for key, value in scenario.items()
            if key != "close_sites" and value != previous_scenario.get(key)
        }
        if (
            previous
            and previous.get("status") in {"optimal", "time_limit"}
            and newly_closed
            and not other_changes
            and all(site_id not in set(previous.get("open_sites", [])) for site_id in newly_closed)
        ):
            # The requested closure cannot affect this solution. Keeping the
            # previous result avoids solver tie-breaking from inventing
            # unrelated warehouse or customer changes.
            result = deepcopy(previous)
            result["scenario"] = scenario
            result["no_op"] = True
            result["no_op_reason"] = "The requested warehouse was already not open in the previous solution."
            return {
                "previous_result": previous,
                "last_result": result,
                "pending_question": None,
                "messages": [],
            }
        try:
            result, orchestration_messages = _call_scenario_tool(
                model, scenario
            )
        except OpenAIRequestError as exc:
            result = {
                "ok": False,
                "error_type": "openai_failure",
                "code": "openai_request_failed",
                "status": "tool_failure",
                "details": [str(exc)],
            }
            orchestration_messages = []
        except Exception as exc:
            result = {
                "ok": False,
                "error_type": "tool_failure",
                "code": "solver_failed",
                "status": "tool_failure",
                "details": [str(exc)],
            }
            orchestration_messages = []
        return {
            "previous_result": previous,
            "last_result": result,
            "pending_question": None,
            "messages": orchestration_messages,
        }

    def feasibility_check(state: AgentState):
        result = state.get("last_result", {})
        if result.get("status") == "infeasible" or result.get("error_type") == "infeasible":
            reason = (result.get("details") or ["the requested constraints have no feasible solution"])[0]
            fix = result.get("suggested_fix") or {}
            question = f"The scenario is infeasible because {reason} One possible fix is {fix}. Apply this fix and rerun?"
            return {"ask_kind": "infeasible", "pending_question": question}
        return {"ask_kind": None, "pending_question": None}

    def compare(state: AgentState):
        changes = _change_list(state)
        result = {**state.get("last_result", {}), "change_list": changes}
        return {"change_list": changes, "last_result": result}

    def explain(state: AgentState):
        result = state.get("last_result", {})
        if result.get("error_type") == "openai_failure":
            reply = (result.get("details") or ["The OpenAI request failed."])[0]
            return {"reply": reply, "messages": [AIMessage(content=reply)]}
        try:
            reply = _explain(
                model,
                result,
                state.get("change_list", {}),
                state.get("request", ""),
            )
        except OpenAIConfigurationError:
            raise
        return {"reply": reply, "messages": [AIMessage(content=reply)]}

    def direct_reply(state: AgentState):
        reply = state.get("direct_reply") or "The request was completed."
        return {"reply": reply, "messages": [AIMessage(content=reply)]}

    def save(state: AgentState):
        saved = dict(state.get("saved_scenarios", {}))
        result = state.get("last_result") or {}
        scenario_id = result.get("scenario_id")
        if scenario_id:
            saved[scenario_id] = result
        return {"saved_scenarios": saved, "pending_question": None}

    def ask_user(state: AgentState):
        answer = interrupt(state.get("pending_question") or "Please clarify the scenario.")
        answer = str(answer).strip()
        kind = state.get("ask_kind")
        answer_message = {"messages": [HumanMessage(content=answer)]}
        if kind == "infeasible":
            if answer.lower() in {"yes", "y", "ok", "okay", "apply it"}:
                fix = state.get("last_result", {}).get("suggested_fix") or {}
                scenario = dict(state.get("scenario") or _default_scenario())
                if fix.get("field"):
                    scenario[fix["field"]] = fix.get("value")
                return {**answer_message, "scenario": scenario, "ask_action": "run_model", "pending_question": None}
            return {**answer_message, "ask_action": "explain", "pending_question": None}
        envelope = _structured_intent(model, answer, state.get("scenario") or _default_scenario())
        partial = _dump(envelope.scenario, exclude_none=True)
        if not partial:
            return {**answer_message, "ask_action": "ask_user", "pending_question": "Please provide one specific valid value.", "ask_kind": "validation"}
        current = dict(state.get("scenario") or _default_scenario())
        if kind == "validation":
            # A correction can also be a new scenario request. Remove fields
            # that caused the previous validation error when the new answer
            # supplies a different field, instead of carrying the invalid
            # value into the next attempt.
            invalid_fields = {
                item.get("field")
                for item in (state.get("validation") or {}).get("field_errors", [])
                if item.get("field")
            }
            defaults = _default_scenario()
            for field in invalid_fields - set(partial):
                if field in defaults:
                    current[field] = defaults[field]
        return {
            **answer_message,
            "scenario": _merge_scenario(current, partial, cumulative=False),
            "ask_action": "validate",
            "pending_question": None,
        }

    def route_after_parse(state: AgentState):
        intent = state.get("intent")
        if state.get("direct_reply"):
            return "direct_reply"
        if intent == "out_of_scope":
            return "explain"
        if intent == "question":
            return "explain" if state.get("last_result") else "ask_user"
        if intent == "report" and state.get("last_result"):
            return "explain"
        return "validate" if intent in {"scenario", "reset", "report"} else "explain"

    def route_after_validate(state: AgentState):
        return "run_model" if state.get("validation", {}).get("valid") else "ask_user"

    def route_after_feasibility(state: AgentState):
        return "ask_user" if state.get("ask_kind") == "infeasible" else "compare"

    def route_after_ask(state: AgentState):
        return state.get("ask_action", "explain")

    builder = StateGraph(AgentState)
    builder.add_node("parse_intent", parse_intent)
    builder.add_node("validate", validate)
    builder.add_node("run_model", run_model)
    builder.add_node("feasibility_check", feasibility_check)
    builder.add_node("compare", compare)
    builder.add_node("explain", explain)
    builder.add_node("direct_reply", direct_reply)
    builder.add_node("save", save)
    builder.add_node("ask_user", ask_user)
    builder.add_edge(START, "parse_intent")
    builder.add_conditional_edges("parse_intent", route_after_parse, {"validate": "validate", "ask_user": "ask_user", "explain": "explain", "direct_reply": "direct_reply"})
    builder.add_conditional_edges("validate", route_after_validate, {"run_model": "run_model", "ask_user": "ask_user"})
    builder.add_edge("run_model", "feasibility_check")
    builder.add_conditional_edges("feasibility_check", route_after_feasibility, {"ask_user": "ask_user", "compare": "compare"})
    builder.add_edge("compare", "explain")
    builder.add_edge("explain", "save")
    builder.add_edge("direct_reply", "save")
    builder.add_conditional_edges("ask_user", route_after_ask, {"validate": "validate", "run_model": "run_model", "ask_user": "ask_user", "explain": "explain"})
    builder.add_edge("save", END)
    return builder.compile(checkpointer=checkpointer or MemorySaver())


_DEFAULT_GRAPH = None


def get_graph():
    global _DEFAULT_GRAPH
    if _DEFAULT_GRAPH is None:
        _DEFAULT_GRAPH = build_graph()
    return _DEFAULT_GRAPH


def ask(text: str, chat_model: Any = None, graph: Any = None, thread_id: str = "ask"):
    """Convenience wrapper for tests and non-Streamlit callers."""
    try:
        graph = graph or build_graph(chat_model=chat_model)
    except OpenAIConfigurationError as exc:
        return {"ok": False, "status": "tool_failure", "error_type": "configuration", "message": str(exc)}
    config = {"configurable": {"thread_id": thread_id}}
    try:
        result = graph.invoke({"messages": [HumanMessage(content=text)], "scenario": _default_scenario()}, config)
    except OpenAIRequestError as exc:
        return {"ok": False, "status": "tool_failure", "error_type": "openai_failure", "message": str(exc)}
    if result.get("__interrupt__"):
        interruption = result["__interrupt__"][0]
        question = getattr(interruption, "value", str(interruption))
        return {"ok": False, "status": "needs_clarification", "message": str(question), "graph_state": result}
    return {**result.get("last_result", {}), "message": result.get("reply", ""), "graph_state": result}


__all__ = [
    "AgentState", "IntentEnvelope", "OpenAIConfigurationError", "OpenAIRequestError", "PartialScenario",
    "RUN_SCENARIO_TOOL", "ScenarioToolInput", "build_graph", "generate_report",
    "get_graph", "ask", "Command",
]
