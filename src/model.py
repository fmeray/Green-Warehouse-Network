"""The PuLP/CBC resilient facility-location model."""
import time

from .data import emission_factors
from .geo import distance_matrix

try:
    import pulp
except ImportError:  # The public solve function reports a clear CBC error.
    pulp = None


SOLVER_TIME_LIMIT_SECONDS = 20
SOLVER_GAP_REL = 0.01


def _cbc_available():
    """Return whether PuLP can execute its bundled CBC command."""
    if pulp is None or not hasattr(pulp, "PULP_CBC_CMD"):
        return False
    try:
        return "PULP_CBC_CMD" in pulp.listSolvers(onlyAvailable=True)
    except Exception:
        return False


def _metrics(distance, demand, factor, transport_rate):
    return (
        distance * demand * transport_rate,
        distance * demand * factor / 1_000_000,
    )


def _solve_mip(cs, ss, scenario, forced_k=None, time_limit=None,
               gap_rel=None, warm_start=None):
    """Build and solve the binary primary/backup model with CBC."""
    dist = distance_matrix(cs, ss)
    factor = emission_factors()[scenario.emission_mode] * scenario.emission_factor_scale
    available = [site["id"] for site in ss if site["id"] not in scenario.close_sites]
    available_ids = set(available)
    forced = set(scenario.force_open_sites)
    if not forced.issubset(available_ids):
        return None, {
            "reason_code": "too_few_sites",
            "details": "a forced-open site is closed or unknown",
            "solver_path": "cbc",
        }

    by_id = {site["id"]: site for site in ss}
    eligible = {
        customer["id"]: sorted(
            (
                site_id for site_id in available
                if dist[customer["id"]][site_id] <= scenario.max_backup_km
            ),
            key=lambda site_id: (dist[customer["id"]][site_id], site_id),
        )[:5]
        for customer in cs
    }
    for customer in cs:
        if len(eligible[customer["id"]]) < 2:
            return None, {
                "reason_code": "backup_distance",
                "details": f"{customer['id']} has fewer than two eligible sites",
                "solver_path": "cbc",
            }

    requested_k = forced_k if forced_k is not None else scenario.k
    minimum_k = max(2, len(forced))
    if requested_k is not None and not minimum_k <= requested_k <= len(available):
        return None, {
            "reason_code": "too_few_sites",
            "details": "the requested warehouse count is unavailable",
            "solver_path": "cbc",
        }

    problem = pulp.LpProblem("green_warehouse_network", pulp.LpMinimize)
    y = {
        site_id: pulp.LpVariable(f"y_{site_id}", cat="Binary")
        for site_id in available
    }
    x = {
        (customer["id"], site_id): pulp.LpVariable(
            f"x_{customer['id']}_{site_id}", cat="Binary"
        )
        for customer in cs for site_id in eligible[customer["id"]]
    }
    z = {
        (customer["id"], site_id): pulp.LpVariable(
            f"z_{customer['id']}_{site_id}", cat="Binary"
        )
        for customer in cs for site_id in eligible[customer["id"]]
    }
    w = {
        (customer["id"], failed, target): pulp.LpVariable(
            f"w_{customer['id']}_{failed}_{target}", cat="Binary"
        )
        for customer in cs
        for failed in eligible[customer["id"]]
        for target in eligible[customer["id"]]
        if failed != target
    }

    for customer in cs:
        cid = customer["id"]
        choices = eligible[cid]
        problem += pulp.lpSum(x[cid, site_id] for site_id in choices) == 1
        problem += pulp.lpSum(z[cid, site_id] for site_id in choices) == 1
        for site_id in choices:
            problem += x[cid, site_id] + z[cid, site_id] <= 1
            problem += x[cid, site_id] <= y[site_id]
            problem += z[cid, site_id] <= y[site_id]
        for failed in choices:
            for target in choices:
                if failed == target:
                    continue
                link = w[cid, failed, target]
                problem += link <= x[cid, failed]
                problem += link <= z[cid, target]
                problem += link >= x[cid, failed] + z[cid, target] - 1
            problem += pulp.lpSum(
                w[cid, failed, target]
                for target in choices if target != failed
            ) == x[cid, failed]
        for target in choices:
            problem += pulp.lpSum(
                w[cid, failed, target]
                for failed in choices if failed != target
            ) == z[cid, target]

    for site_id in available:
        problem += pulp.lpSum(
            customer["demand"] * x[customer["id"], site_id]
            for customer in cs
            if (customer["id"], site_id) in x
        ) <= by_id[site_id]["capacity"] * scenario.capacity_scale * y[site_id]

    # After a failure at any site, the failed site's primary demand moves to
    # its assigned backup. The exact w links make this capacity constraint
    # apply to the right customer pairs.
    for failed in available:
        for target in available:
            if failed == target:
                continue
            normal = pulp.lpSum(
                customer["demand"] * x[customer["id"], target]
                for customer in cs
                if (customer["id"], target) in x
            )
            diverted = pulp.lpSum(
                customer["demand"] * w[customer["id"], failed, target]
                for customer in cs
                if (customer["id"], failed, target) in w
            )
            problem += normal + diverted <= (
                by_id[target]["capacity"] * scenario.capacity_scale * y[target]
            )

    for site_id in forced:
        problem += y[site_id] == 1
    if requested_k is not None:
        problem += pulp.lpSum(y.values()) == requested_k
    else:
        problem += pulp.lpSum(y.values()) >= minimum_k
        # The dashboard's supported exploration range is k = 2..5. Keeping
        # the unconstrained network in that same range is also what makes the
        # default model fit the stated response-time target.
        problem += pulp.lpSum(y.values()) <= min(5, len(available))

    transport_cost = pulp.lpSum(
        customer["demand"] * scenario.transport_cost_eur_per_tkm * dist[cid][site_id]
        * (x[cid, site_id] + scenario.failure_probability * z[cid, site_id])
        for customer in cs
        for cid in [customer["id"]]
        for site_id in eligible[cid]
    )
    transport_co2 = pulp.lpSum(
        customer["demand"] * factor / 1_000_000 * dist[cid][site_id]
        * (x[cid, site_id] + scenario.failure_probability * z[cid, site_id])
        for customer in cs
        for cid in [customer["id"]]
        for site_id in eligible[cid]
    )
    fixed_cost = pulp.lpSum(
        by_id[site_id]["fixed_cost"] * y[site_id] for site_id in available
    )
    warehouse_co2 = pulp.lpSum(
        by_id[site_id]["fixed_co2"] * scenario.warehouse_co2_scale * y[site_id]
        for site_id in available
    )
    total_cost = fixed_cost + transport_cost
    total_co2 = warehouse_co2 + transport_co2
    tie_break = 1e-9
    problem += (
        total_co2 + tie_break * total_cost
        if scenario.objective == "co2"
        else total_cost + tie_break * total_co2
    )

    solver_options = {"msg": False}
    if time_limit is not None:
        solver_options["timeLimit"] = time_limit
    if gap_rel is not None:
        solver_options["gapRel"] = gap_rel
    if warm_start:
        assignments = warm_start.get("assignments", {})
        open_sites = set(warm_start.get("open_sites", []))
        for site_id, variable in y.items():
            variable.setInitialValue(int(site_id in open_sites))
        for (cid, site_id), variable in x.items():
            variable.setInitialValue(int(assignments.get(cid, {}).get("primary") == site_id))
        for (cid, site_id), variable in z.items():
            variable.setInitialValue(int(assignments.get(cid, {}).get("backup") == site_id))
        for (cid, failed, target), variable in w.items():
            assignment = assignments.get(cid, {})
            variable.setInitialValue(int(
                assignment.get("primary") == failed and assignment.get("backup") == target
            ))
        solver_options["warmStart"] = True

    started = time.perf_counter()
    status_code = problem.solve(pulp.PULP_CBC_CMD(**solver_options))
    elapsed = time.perf_counter() - started
    status_name = pulp.LpStatus.get(status_code, str(status_code))
    time_limited = status_name in {"Not Solved", "Undefined"}
    metadata = {
        "solver_path": "cbc",
        "solver_status": "time_limit" if time_limited else (
            "optimal" if status_name == "Optimal" else status_name.lower().replace(" ", "_")
        ),
        "stopped_at_limit": time_limited,
        "solver_time_seconds": round(elapsed, 3),
        "solver_time_limit_seconds": time_limit,
        "solver_relative_gap": gap_rel,
    }
    if status_name != "Optimal":
        return None, {
            "reason_code": "time_limit" if time_limited else "capacity",
            "details": (
                "CBC stopped before finding an optimal solution."
                if time_limited else
                "No site set can satisfy normal and single-failure capacities."
            ),
            **metadata,
        }

    opened = [site_id for site_id in available if pulp.value(y[site_id]) > 0.5]
    assignments = {}
    loads = {site_id: 0.0 for site_id in opened}
    try:
        for customer in cs:
            cid = customer["id"]
            primary = next(site_id for site_id in eligible[cid] if pulp.value(x[cid, site_id]) > 0.5)
            backup = next(site_id for site_id in eligible[cid] if pulp.value(z[cid, site_id]) > 0.5)
            primary_cost, primary_co2 = _metrics(
                dist[cid][primary], customer["demand"], factor,
                scenario.transport_cost_eur_per_tkm,
            )
            backup_cost, backup_co2 = _metrics(
                dist[cid][backup], customer["demand"], factor,
                scenario.transport_cost_eur_per_tkm,
            )
            assignments[cid] = {
                "primary": primary, "backup": backup,
                "primary_km": round(dist[cid][primary], 3),
                "backup_km": round(dist[cid][backup], 3),
                "primary_transport_cost": primary_cost,
                "backup_transport_cost": backup_cost,
                "primary_co2": primary_co2, "backup_co2": backup_co2,
            }
            loads[primary] += customer["demand"]
    except (StopIteration, TypeError):
        return None, {
            "reason_code": "solver_status",
            "details": "CBC returned an incomplete assignment.",
            **metadata,
        }

    transport_co2_value = sum(
        row["primary_co2"] + scenario.failure_probability * row["backup_co2"]
        for row in assignments.values()
    )
    warehouse_co2_value = sum(by_id[site_id]["fixed_co2"] for site_id in opened) * scenario.warehouse_co2_scale
    transport_cost_value = sum(
        row["primary_transport_cost"] + scenario.failure_probability * row["backup_transport_cost"]
        for row in assignments.values()
    )
    fixed_cost_value = sum(by_id[site_id]["fixed_cost"] for site_id in opened)
    total_co2_value = transport_co2_value + warehouse_co2_value
    total_cost_value = fixed_cost_value + transport_cost_value
    primary_distances = [row["primary_km"] for row in assignments.values()]
    backup_distances = [row["backup_km"] for row in assignments.values()]
    return {
        "status": "optimal", "objective": scenario.objective,
        "k": len(opened), "open_sites": opened, "assignments": assignments,
        "loads": loads,
        "total_cost_eur": round(total_cost_value, 2),
        "total_cost_with_carbon_eur": round(
            total_cost_value + scenario.carbon_price_eur_per_t * total_co2_value, 2
        ),
        "total_co2_t": round(total_co2_value, 3),
        "transport_co2_t": round(transport_co2_value, 3),
        "warehouse_co2_t": round(warehouse_co2_value, 3),
        "average_primary_km": round(sum(primary_distances) / len(primary_distances), 2),
        "average_backup_km": round(sum(backup_distances) / len(backup_distances), 2),
        "base_cost_eur": round(total_cost_value, 2),
        "carbon_cost_eur": round(scenario.carbon_price_eur_per_t * total_co2_value, 2),
        **metadata,
    }, {}


def solve(cs, ss, scenario, forced_k=None, time_limit=SOLVER_TIME_LIMIT_SECONDS,
          gap_rel=SOLVER_GAP_REL, warm_start=None, solver_name="cbc"):
    """Solve with CBC, raising instead of selecting another backend."""
    if solver_name != "cbc":
        raise ValueError("Only the CBC solver is supported by this project.")
    if not _cbc_available():
        raise RuntimeError(
            "CBC is not available. Install pulp==2.9.0 with its bundled CBC solver."
        )
    return _solve_mip(
        cs, ss, scenario, forced_k=forced_k, time_limit=time_limit,
        gap_rel=gap_rel, warm_start=warm_start,
    )
