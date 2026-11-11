import itertools
import unittest
from unittest.mock import patch

from src.data import customers, emission_factors, sites
from src.model import solve
from src.schema import Scenario, scenario_from_dict
from src.tools import run_scenario


class SolverPathTests(unittest.TestCase):
    def test_five_customer_three_site_case_matches_independent_bruteforce(self):
        customer_rows = customers()[:5]
        site_rows = sites()[:3]
        scenario = Scenario(k=3, max_backup_km=2000)

        from src.geo import distance_matrix
        distances = distance_matrix(customer_rows, site_rows)
        factor = emission_factors()[scenario.emission_mode]
        by_id = {site["id"]: site for site in site_rows}
        feasible_values = []
        for choices in itertools.product(
            [(primary, backup) for primary in by_id for backup in by_id if primary != backup],
            repeat=len(customer_rows),
        ):
            loads = {site["id"]: 0.0 for site in site_rows}
            for customer, (primary, _) in zip(customer_rows, choices):
                loads[primary] += customer["demand"]
            if any(loads[site_id] > by_id[site_id]["capacity"] + 1e-9 for site_id in loads):
                continue
            feasible = True
            for failed in by_id:
                for target in by_id:
                    if target == failed:
                        continue
                    load = sum(
                        customer["demand"]
                        for customer, (primary, backup) in zip(customer_rows, choices)
                        if primary == target or (primary == failed and backup == target)
                    )
                    if load > by_id[target]["capacity"] + 1e-9:
                        feasible = False
                        break
                if not feasible:
                    break
            if not feasible:
                continue
            transport_cost = sum(
                customer["demand"] * scenario.transport_cost_eur_per_tkm
                * (distances[customer["id"]][primary] + scenario.failure_probability * distances[customer["id"]][backup])
                for customer, (primary, backup) in zip(customer_rows, choices)
            )
            transport_co2 = sum(
                customer["demand"] * factor / 1_000_000
                * (distances[customer["id"]][primary] + scenario.failure_probability * distances[customer["id"]][backup])
                for customer, (primary, backup) in zip(customer_rows, choices)
            )
            feasible_values.append((
                sum(site["fixed_co2"] for site in site_rows) + transport_co2,
                sum(site["fixed_cost"] for site in site_rows) + transport_cost,
            ))

        self.assertTrue(feasible_values)
        best_co2 = min(feasible_values, key=lambda value: (value[0], value[1]))
        best_cost = min(feasible_values, key=lambda value: (value[1], value[0]))
        for objective, expected in (("co2", best_co2), ("cost", best_cost)):
            result, reason = solve(customer_rows, site_rows, Scenario(objective=objective, k=3, max_backup_km=2000))
            self.assertEqual(reason, {})
            self.assertEqual(result["solver_path"], "cbc")
            self.assertAlmostEqual(result["total_co2_t"], expected[0], places=3)
            self.assertAlmostEqual(result["total_cost_eur"], expected[1], places=2)

    def test_cbc_is_the_only_solver_and_missing_cbc_is_clear(self):
        with patch("src.model._cbc_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "CBC is not available"):
                solve(customers()[:2], sites()[:3], Scenario(k=3, max_backup_km=2000))

    def test_final_schema_has_opt_in_analysis_and_no_frontier_fields(self):
        scenario = scenario_from_dict({})
        self.assertFalse(scenario.k_sweep)
        self.assertFalse(scenario.compare_objectives)
        self.assertEqual(scenario.max_backup_km, 500)
        self.assertNotIn("tradeoff_points", scenario.model_dump())
        self.assertNotIn("backup_must_cover_full_demand", scenario.model_dump())
        with self.assertRaises(Exception):
            scenario_from_dict({"tradeoff_points": 3})

    def test_default_scenario_has_no_optional_analysis_flags(self):
        scenario = scenario_from_dict({})
        self.assertFalse(scenario.k_sweep)
        self.assertFalse(scenario.compare_objectives)

    def test_compare_objectives_returns_the_spec_formula(self):
        result = run_scenario({"compare_objectives": True})
        self.assertEqual(result["status"], "optimal")
        comparison = result["objective_comparison"]
        if "cost_of_avoiding_co2_eur_per_t" in result:
            co2 = comparison["co2_network"]
            cost = comparison["cost_network"]
            expected = (co2["total_cost_eur"] - cost["total_cost_eur"]) / (cost["total_co2_t"] - co2["total_co2_t"])
            self.assertAlmostEqual(result["cost_of_avoiding_co2_eur_per_t"], round(expected, 2))
        else:
            self.assertIn("cost_of_avoiding_co2_solver_gap", result)


if __name__ == "__main__":
    unittest.main()
