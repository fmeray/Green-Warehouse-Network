"""Load deterministic Germany sample data from CSV files."""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def _read_csv(filename):
    with (DATA_DIR / filename).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def customers():
    return [
        {
            **row,
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "demand": float(row["demand"]),
        }
        for row in _read_csv("customers.csv")
    ]


def sites():
    return [
        {
            **row,
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "capacity": float(row["capacity"]),
            "fixed_cost": float(row["fixed_cost"]),
            "fixed_co2": float(row["fixed_co2"]),
        }
        for row in _read_csv("candidate_sites.csv")
    ]


def emission_factors():
    """Return factors in g CO2e per tonne-km.

    The current data contract calls the value ``g_co2e_per_tkm``.  ``value``
    is still accepted so older project data can be used without a migration.
    """
    factors = {}
    for row in _read_csv("emission_factors.csv"):
        value = row.get("g_co2e_per_tkm", row.get("value"))
        if value in (None, ""):
            continue
        factors[row["mode"].strip().lower()] = float(value)
    return factors


def emission_factor_records():
    """Return the full factor rows, including Climatiq provenance metadata."""
    return _read_csv("emission_factors.csv")


def validate_data():
    cs, ss = customers(), sites()
    errors = []
    if len({x["id"] for x in cs}) != len(cs):
        errors.append("duplicate customer IDs")
    if len({x["id"] for x in ss}) != len(ss):
        errors.append("duplicate site IDs")
    if sum(s["capacity"] for s in ss) < 2 * sum(c["demand"] for c in cs):
        errors.append("total capacity below 2x demand")
    for customer in cs:
        if not 47.2 <= customer["lat"] <= 55.1 or not 5.8 <= customer["lon"] <= 15.1:
            errors.append(f"customer {customer['id']} is outside the Germany bounds")
        if customer["demand"] <= 0:
            errors.append(f"non-positive demand for customer {customer['id']}")
    for site in ss:
        if not 47.2 <= site["lat"] <= 55.1 or not 5.8 <= site["lon"] <= 15.1:
            errors.append(f"site {site['id']} is outside the Germany bounds")
        if site["capacity"] <= 0:
            errors.append(f"non-positive capacity for site {site['id']}")
    return {
        "valid": not errors,
        "errors": errors,
        "customers": len(cs),
        "sites": len(ss),
        "total_demand": sum(x["demand"] for x in cs),
    }
