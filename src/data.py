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
    return {
        row["mode"]: float(row["value"])
        for row in _read_csv("emission_factors.csv")
    }


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
        if not -90 <= customer["lat"] <= 90 or not -180 <= customer["lon"] <= 180:
            errors.append(f"invalid coordinates for customer {customer['id']}")
        if customer["demand"] <= 0:
            errors.append(f"non-positive demand for customer {customer['id']}")
    for site in ss:
        if not -90 <= site["lat"] <= 90 or not -180 <= site["lon"] <= 180:
            errors.append(f"invalid coordinates for site {site['id']}")
        if site["capacity"] <= 0:
            errors.append(f"non-positive capacity for site {site['id']}")
    return {
        "valid": not errors,
        "errors": errors,
        "customers": len(cs),
        "sites": len(ss),
        "total_demand": sum(x["demand"] for x in cs),
    }
