"""The six structured tool functions in the final specification."""
from copy import deepcopy
from hashlib import sha256
import json

from .data import customers as base_customers, emission_factors, sites
from .geo import distance_matrix, distance_status, haversine
from .model import solve
from .schema import (
    SCENARIO_RANGE_LABELS,
    SCENARIO_RANGES,
    Scenario,
    scenario_from_dict,
)

SAVED_SCENARIOS = {}
SCENARIO_CACHE = {}


def _error(error_type, code, field=None, value=None, allowed=None,
           details=None, suggested_fix=None, **extra):
    return {
        "ok": False, "error_type": error_type, "code": code,
        "field": field, "value": value, "allowed": allowed,
        "details": details or [], "suggested_fix": suggested_fix or {},
        **extra,
    }


def _success(**values):
    return {"ok": True, **values}


def _dump(scenario):
    return scenario.model_dump() if hasattr(scenario, "model_dump") else scenario.__dict__.copy()


def _schema_error(exc):
    raw = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc)}]
    first = raw[0] if raw else {}
    location = first.get("loc")
    field = location[0] if isinstance(location, (list, tuple)) and location else location
    if field is None:
        message = str(exc)
        known = (*SCENARIO_RANGES, "objective", "emission_mode", "k")
        field = next((name for name in known if name in message), None)
    allowed = SCENARIO_RANGE_LABELS.get(field)
    if field == "k":
        allowed = "2 to number of sites"
    return _error(
        "validation", "out_of_range" if allowed else "invalid_scenario",
        field=field, value=first.get("input"), allowed=allowed, details=raw,
    )


def _materialize(scenario):
    customers = [
        dict(customer) for customer in base_customers()
        if customer["id"] not in scenario.remove_customers
    ]
    customers.extend(_dump(customer) for customer in scenario.add_customers)
    for customer in customers:
        customer["demand"] *= scenario.demand_scale
    return customers, sites()


def validate_scenario(params=None):
    try:
        scenario = (
            scenario_from_dict(params) if isinstance(params, dict)
            else (params or Scenario())
        )
    except Exception as exc:
        error = _schema_error(exc)
        return {**error, "valid": False, "errors": error["details"], "warnings": []}

    errors = []
    warnings = []
    field_errors = []
    candidate_sites = sites()
    site_ids = {site["id"] for site in candidate_sites}
    customers, _ = _materialize(scenario)
    for name, (low, high) in SCENARIO_RANGES.items():
        value = getattr(scenario, name)
        if not low <= value <= high:
            message = f"{name} must be between {low} and {high}"
            field_errors.append({
                "field": name,
                "value": value,
                "allowed": SCENARIO_RANGE_LABELS[name],
                "message": message,
            })
            errors.append(message)
    if scenario.objective not in {"co2", "cost"}:
        errors.append("objective must be co2 or cost")
    if scenario.emission_mode not in emission_factors():
        field_errors.append({"field": "emission_mode", "value": scenario.emission_mode, "allowed": "diesel or electric", "message": "unknown emission mode"})
        errors.append("emission_mode must be diesel or electric")

    names = {site["id"]: site["name"] for site in candidate_sites}
    for site_id in scenario.close_sites + scenario.force_open_sites:
        if site_id not in site_ids:
            field = "close_sites" if site_id in scenario.close_sites else "force_open_sites"
            item = {"field": field, "value": site_id, "allowed": list(names), "message": f"unknown site: {site_id}"}
            field_errors.append(item)
            errors.append(item["message"])
    overlap = set(scenario.close_sites) & set(scenario.force_open_sites)
    if overlap:
        errors.append("close_sites and force_open_sites overlap")
        field_errors.append({"field": "close_sites", "value": sorted(overlap), "allowed": "must not overlap force_open_sites", "message": errors[-1]})
    available_count = len(site_ids - set(scenario.close_sites))
    if scenario.k is not None and not 2 <= scenario.k <= available_count:
        message = "k must be between 2 and available site count"
        field_errors.append({"field": "k", "value": scenario.k, "allowed": f"2 to {available_count}", "message": message})
        errors.append(message)

    known_customer_ids = {customer["id"] for customer in base_customers()}
    for customer_id in scenario.remove_customers:
        if customer_id not in known_customer_ids:
            message = f"unknown customer: {customer_id}"
            field_errors.append({"field": "remove_customers", "value": customer_id, "allowed": "customer IDs in customers.csv", "message": message})
            errors.append(message)
    if len({customer["id"] for customer in customers}) != len(customers):
        errors.append("duplicate customer ID in additions")
        field_errors.append({"field": "add_customers", "value": "duplicate ID", "allowed": "unique customer IDs", "message": errors[-1]})
    for customer in customers:
        if not 47.2 <= customer["lat"] <= 55.1 or not 5.8 <= customer["lon"] <= 15.1:
            message = f"customer {customer['id']} is outside the Germany bounds"
            field_errors.append({"field": "add_customers", "value": customer["id"], "allowed": "coordinates inside Germany", "message": message})
            errors.append(message)

    total_capacity = sum(
        site["capacity"] * scenario.capacity_scale
        for site in candidate_sites if site["id"] not in scenario.close_sites
    )
    total_demand = sum(customer["demand"] for customer in customers)
    if total_capacity < 2 * total_demand:
        warnings.append("available capacity is below 2x demand; failure cover may be infeasible")
    distances = distance_matrix(customers, candidate_sites)
    for customer in customers:
        reachable = [
            site for site in candidate_sites
            if site["id"] not in scenario.close_sites
            and distances[customer["id"]][site["id"]] <= scenario.max_backup_km
        ]
        if len(reachable) < 2:
            warnings.append(f"{customer['id']} has fewer than two reachable sites")
    provenance = distance_status()
    tool_warnings = [provenance] if provenance.get("error_type") == "tool_failure" else []
    result = {
        "valid": not errors, "errors": errors, "warnings": warnings,
        "field_errors": field_errors, "tool_warnings": tool_warnings,
        "distance_status": provenance,
    }
    if errors:
        first = field_errors[0] if field_errors else {}
        code = (
            "unknown_site" if isinstance(first.get("allowed"), list)
            else "out_of_range" if first.get("allowed") else "invalid_scenario"
        )
        return _error(
            "validation", code, field=first.get("field"), value=first.get("value"),
            allowed=first.get("allowed"), details=field_errors or errors,
            **result,
        )
    return _success(**result)


def _baseline(objective="co2", scenario=None):
    scenario = scenario or scenario_from_dict({"objective": objective})
    customers = base_customers()
    candidate_sites = sites()
    total_demand = sum(customer["demand"] for customer in customers)
    lat = sum(customer["lat"] * customer["demand"] for customer in customers) / total_demand
    lon = sum(customer["lon"] * customer["demand"] for customer in customers) / total_demand
    factor = emission_factors()[scenario.emission_mode] * scenario.emission_factor_scale
    rate = scenario.transport_cost_eur_per_tkm
    carbon_price = scenario.carbon_price_eur_per_t
    cog_km = sum(
        haversine(customer["lat"], customer["lon"], lat, lon) * customer["demand"]
        for customer in customers
    ) / total_demand
    cog_co2 = sum(
        haversine(customer["lat"], customer["lon"], lat, lon) * customer["demand"] * factor / 1_000_000
        for customer in customers
    )
    center = {
        "resilient": False, "objective": objective,
        "site": {"name": "Center of gravity", "lat": round(lat, 5), "lon": round(lon, 5)},
        "total_cost_eur": round(cog_km * total_demand * rate, 2),
        "total_cost_with_carbon_eur": round(cog_km * total_demand * rate + carbon_price * cog_co2, 2),
        "total_co2_t": round(cog_co2, 3), "average_distance_km": round(cog_km, 2),
        "note": "Greenfield comparison only; it has no backup and no warehouse capacity.",
    }
    matrix = distance_matrix(customers, candidate_sites)
    candidates = []
    for site in candidate_sites:
        transport_cost = sum(matrix[c["id"]][site["id"]] * c["demand"] * rate for c in customers)
        transport_co2 = sum(matrix[c["id"]][site["id"]] * c["demand"] * factor / 1_000_000 for c in customers)
        warehouse_co2 = site["fixed_co2"] * scenario.warehouse_co2_scale
        total_co2 = warehouse_co2 + transport_co2
        total_cost = site["fixed_cost"] + transport_cost
        candidates.append({
            "resilient": False, "objective": objective, "k": 1,
            "site": {key: site[key] for key in ("id", "name", "lat", "lon")},
            "open_sites": [site["id"]],
            "assignments": {c["id"]: {"primary": site["id"], "backup": None} for c in customers},
            "capacity_t": site["capacity"], "demand_t": total_demand,
            "capacity_sufficient": site["capacity"] >= total_demand,
            "total_cost_eur": round(total_cost, 2),
            "total_cost_with_carbon_eur": round(total_cost + carbon_price * total_co2, 2),
            "total_co2_t": round(total_co2, 3), "transport_co2_t": round(transport_co2, 3),
            "warehouse_co2_t": round(warehouse_co2, 3),
            "average_primary_km": round(sum(matrix[c["id"]][site["id"]] for c in customers) / len(customers), 2),
            "note": "Comparison baseline; it cannot provide a backup.",
        })
    best = min(
        candidates,
        key=lambda item: (
            item["total_co2_t"] if objective == "co2" else item["total_cost_eur"],
            item["total_cost_eur"] if objective == "co2" else item["total_co2_t"],
            item["open_sites"],
        ),
    )
    return {"center_of_gravity": center, "single_warehouse": best, **best}


def get_baseline(objective="co2"):
    try:
        scenario = scenario_from_dict({"objective": objective})
        result, reason = solve(*_materialize(scenario), scenario)
    except Exception as exc:
        return _error("tool_failure", "baseline_failed", details=[str(exc)], status="tool_failure")
    reference = _baseline(objective, scenario)
    if result is None:
        status = "time_limit" if reason.get("reason_code") == "time_limit" else "infeasible"
        return _error(
            "solver_failure" if status == "time_limit" else "infeasible",
            reason.get("reason_code", "capacity"), details=[reason.get("details", "Baseline failed.")],
            status=status, solver_path="cbc", solver_status=reason.get("solver_status"),
            solver_relative_gap=reason.get("solver_relative_gap"),
            single_warehouse=reference["single_warehouse"], center_of_gravity=reference["center_of_gravity"],
        )
    return _success(
        status=result["status"], solver_path="cbc", network=result,
        single_warehouse=reference["single_warehouse"], center_of_gravity=reference["center_of_gravity"],
    )


def list_sites():
    return _success(sites=[{**site, "status": "candidate"} for site in sites()])


def _infeasible_response(reason, scenario, customers, validation):
    code = reason.get("reason_code", "capacity")
    suggestion = {}
    if code == "backup_distance":
        suggestion = {"field": "max_backup_km", "value": min(2000.0, scenario.max_backup_km + 100.0)}
    elif code == "too_few_sites":
        suggestion = {"field": "close_sites", "value": []}
    elif code == "capacity":
        suggestion = {"field": "capacity_scale", "value": 2.0}
        if scenario.k is not None:
            suggestion = {"field": "k", "value": min(5, len(sites()))}
    return _error(
        "solver_failure" if code == "time_limit" else "infeasible", code,
        value=scenario.k if code == "capacity" else scenario.max_backup_km if code == "backup_distance" else scenario.close_sites,
        details=[reason.get("details", "No feasible solution.")], suggested_fix=suggestion,
        status="time_limit" if code == "time_limit" else "infeasible",
        reason_code=code, reason=reason.get("details"), solver_path="cbc",
        solver_status=reason.get("solver_status"), solver_relative_gap=reason.get("solver_relative_gap"),
        customers=[customers[0]["id"]] if code == "backup_distance" and customers else [],
        sites=scenario.close_sites if code == "too_few_sites" else [], validation=validation,
    )


def _compare_objectives(customers, candidate_sites, scenario, main_result):
    other_objective = "cost" if scenario.objective == "co2" else "co2"
    other = scenario_from_dict({**_dump(scenario), "objective": other_objective, "compare_objectives": False, "k_sweep": False})
    other_result, other_reason = solve(customers, candidate_sites, other)
    if main_result.get("status") != "optimal" or not other_result or other_result.get("status") != "optimal":
        gaps = [
            value for value in (
                main_result.get("solver_relative_gap"),
                (other_result or {}).get("solver_relative_gap"),
                other_reason.get("solver_relative_gap"),
            ) if value is not None
        ]
        return {
            "co2_network": main_result if scenario.objective == "co2" else other_result,
            "cost_network": main_result if scenario.objective == "cost" else other_result,
            "solver_gap": max(gaps) if gaps else None,
        }
    co2_result = main_result if scenario.objective == "co2" else other_result
    cost_result = main_result if scenario.objective == "cost" else other_result
    avoided = cost_result["total_co2_t"] - co2_result["total_co2_t"]
    extra_cost = co2_result["total_cost_eur"] - cost_result["total_cost_eur"]
    value = extra_cost / avoided if avoided else None
    return {
        "co2_network": {"total_cost_eur": co2_result["total_cost_eur"], "total_co2_t": co2_result["total_co2_t"]},
        "cost_network": {"total_cost_eur": cost_result["total_cost_eur"], "total_co2_t": cost_result["total_co2_t"]},
        "cost_of_avoiding_co2_eur_per_t": round(value, 2) if value is not None else None,
        "solver_gap": None,
    }


def run_scenario(params=None):
    try:
        scenario = scenario_from_dict(params) if isinstance(params, dict) else (params or Scenario())
    except Exception as exc:
        return {**_schema_error(exc), "status": "invalid"}
    validation = validate_scenario(scenario)
    if not validation["valid"]:
        return {**validation, "status": "invalid", "validation": validation}
    scenario_id = sha256(json.dumps(_dump(scenario), sort_keys=True, default=str).encode()).hexdigest()[:12]
    cached = SCENARIO_CACHE.get(scenario_id)
    if cached is not None:
        SAVED_SCENARIOS[scenario_id] = deepcopy(cached)
        return _success(**deepcopy(cached))
    try:
        customers, candidate_sites = _materialize(scenario)
        result, reason = solve(customers, candidate_sites, scenario)
    except RuntimeError as exc:
        return _error("tool_failure", "cbc_unavailable", details=[str(exc)], status="tool_failure")
    except Exception as exc:
        return _error("tool_failure", "solver_failed", details=[str(exc)], status="tool_failure")
    if result is None:
        return _infeasible_response(reason, scenario, customers, validation)

    baseline = _baseline(scenario.objective, scenario)
    result["resilience_price_eur"] = round(result["total_cost_eur"] - baseline["center_of_gravity"]["total_cost_eur"], 2)
    result["resilience_price_co2_t"] = round(result["total_co2_t"] - baseline["center_of_gravity"]["total_co2_t"], 3)
    result["resilience_price_vs_single_warehouse_eur"] = round(result["total_cost_eur"] - baseline["single_warehouse"]["total_cost_eur"], 2)
    result["resilience_price_vs_single_warehouse_co2_t"] = round(result["total_co2_t"] - baseline["single_warehouse"]["total_co2_t"], 3)
    result["scenario"] = _dump(scenario)
    result["validation"] = validation
    result["tool_warnings"] = validation.get("tool_warnings", [])
    result["scenario_id"] = scenario_id

    if scenario.k_sweep:
        result["k_sweep"] = []
        for k in range(2, min(5, len(candidate_sites)) + 1):
            sweep_result, sweep_reason = solve(customers, candidate_sites, scenario, forced_k=k)
            if sweep_result:
                result["k_sweep"].append({
                    "k": k, "status": "optimal", "total_cost_eur": sweep_result["total_cost_eur"],
                    "total_co2_t": sweep_result["total_co2_t"], "open_sites": sweep_result["open_sites"],
                    "assignments": sweep_result["assignments"], "solver_path": "cbc",
                    "solver_status": sweep_result.get("solver_status", sweep_result["status"]),
                    "solver_relative_gap": sweep_result.get("solver_relative_gap"),
                })
            else:
                result["k_sweep"].append({
                    "k": k, "status": "time_limit" if sweep_reason.get("reason_code") == "time_limit" else "infeasible",
                    "reason_code": sweep_reason.get("reason_code"), "reason": sweep_reason.get("details"),
                    "solver_path": "cbc", "solver_status": sweep_reason.get("solver_status"),
                    "solver_relative_gap": sweep_reason.get("solver_relative_gap"),
                })
        result["recommended_k"] = min(
            (item for item in result["k_sweep"] if item.get("solver_status") == "optimal"),
            key=lambda item: item["total_co2_t"] if scenario.objective == "co2" else item["total_cost_eur"],
            default={"k": result["k"]},
        )["k"]

    if scenario.compare_objectives:
        comparison = _compare_objectives(customers, candidate_sites, scenario, result)
        if comparison.get("cost_of_avoiding_co2_eur_per_t") is not None:
            result.update({"cost_of_avoiding_co2_eur_per_t": comparison["cost_of_avoiding_co2_eur_per_t"]})
        else:
            result["cost_of_avoiding_co2_solver_gap"] = comparison.get("solver_gap")
        result["objective_comparison"] = comparison

    SAVED_SCENARIOS[scenario_id] = deepcopy(result)
    failures = [simulate_failure(scenario_id, site_id) for site_id in result["open_sites"]]
    result["failure_summary"] = failures
    result["worst_case_failure"] = max(failures, key=lambda item: item.get("extra_co2_t", float("-inf")), default=None)
    SAVED_SCENARIOS[scenario_id] = deepcopy(result)
    SCENARIO_CACHE[scenario_id] = deepcopy(result)
    return _success(**result)


def simulate_failure(scenario_id, site_id):
    result = SAVED_SCENARIOS.get(scenario_id)
    if not result:
        return _error("validation", "scenario_not_found", field="scenario_id", value=scenario_id, details=["The saved scenario does not exist."], status="not_found")
    if site_id not in result["open_sites"]:
        return _error("validation", "site_not_open", field="site_id", value=site_id, allowed=result["open_sites"], details=["The requested site is not open in this scenario."], status="invalid")
    scenario = scenario_from_dict(result["scenario"])
    customers, _ = _materialize(scenario)
    moved = []
    loads = {site_id_: 0.0 for site_id_ in result["open_sites"] if site_id_ != site_id}
    for customer in customers:
        assignment = result["assignments"][customer["id"]]
        target = assignment["backup"] if assignment["primary"] == site_id else assignment["primary"]
        loads[target] += customer["demand"]
        if assignment["primary"] == site_id:
            moved.append({"customer_id": customer["id"], "to_site": assignment["backup"], "demand": customer["demand"]})
    by_id = {site["id"]: site for site in sites()}
    overloads = []
    site_loads = []
    for open_site, load in loads.items():
        capacity = by_id[open_site]["capacity"] * scenario.capacity_scale
        if load > capacity + 1e-9:
            overloads.append({"site": open_site, "load": round(load, 2), "capacity": round(capacity, 2)})
        site_loads.append({"site": open_site, "load_t": round(load, 2), "capacity_t": round(capacity, 2), "utilization_pct": round(100 * load / capacity, 1)})
    moved_rows = [customer for customer in customers if result["assignments"][customer["id"]]["primary"] == site_id]
    extra_km = sum(result["assignments"][customer["id"]]["backup_km"] - result["assignments"][customer["id"]]["primary_km"] for customer in moved_rows)
    extra_tonne_km = sum((result["assignments"][customer["id"]]["backup_km"] - result["assignments"][customer["id"]]["primary_km"]) * customer["demand"] for customer in moved_rows)
    factor = emission_factors()[scenario.emission_mode] * scenario.emission_factor_scale
    return _success(
        status="ok", scenario_id=scenario_id, failed_site=site_id, solver_path="cbc",
        extra_km=round(extra_km, 2), extra_tonne_km=round(extra_tonne_km, 2),
        extra_co2_t=round(extra_tonne_km * factor / 1_000_000, 3),
        customers_moved=moved, capacity_overloads=overloads, remaining_loads=loads,
        site_loads=site_loads,
    )


def compare_scenarios(ids):
    if not isinstance(ids, list):
        return _error("validation", "invalid_scenario_ids", field="ids", value=ids, allowed="a list of saved scenario IDs", details=["ids must be a list."], status="invalid")
    rows = []
    for scenario_id in ids:
        if scenario_id not in SAVED_SCENARIOS:
            return _error("validation", "scenario_not_found", field="scenario_id", value=scenario_id, details=["The saved scenario does not exist."], status="not_found")
        result = SAVED_SCENARIOS[scenario_id]
        rows.append({"scenario_id": scenario_id, "total_cost_eur": result["total_cost_eur"], "total_co2_t": result["total_co2_t"], "k": result["k"], "open_sites": result["open_sites"], "solver_path": "cbc"})
    if not rows:
        return _success(status="ok", scenarios=[])
    reference = rows[0]
    for row in rows:
        row["delta_cost_eur"] = round(row["total_cost_eur"] - reference["total_cost_eur"], 2)
        row["delta_co2_t"] = round(row["total_co2_t"] - reference["total_co2_t"], 3)
    return _success(status="ok", scenarios=rows, reference=reference["scenario_id"])
