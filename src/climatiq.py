"""Small Climatiq integration used to refresh the cached factor file.

Climatiq's factor catalogue changes independently of the planner, so factors
are fetched only when explicitly requested (or when CLIMATIQ_REFRESH=1). The
existing CSV values remain the safe fallback if the catalogue or plan does not
expose a freight factor.
"""
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .data import DATA_DIR, emission_factor_records

try:
    from dotenv import load_dotenv
    load_dotenv(DATA_DIR.parent / ".env")
except ImportError:
    # The project can still read the three simple KEY=value entries when the
    # optional python-dotenv package has not been installed yet.
    env_path = DATA_DIR.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _request_json(url, api_key, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Climatiq HTTP {exc.code}: {detail}") from exc
    except (URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Climatiq request failed: check the API URL and network") from exc


def _catalogue_candidate(mode, api_key):
    """Find a public road-freight factor through Climatiq Search."""
    query = (
        "truck diesel GLEC Europe"
        if mode == "diesel"
        else "truck electric UBA Germany"
    )
    endpoint = os.getenv(
        "CLIMATIQ_SEARCH_URL", "https://api.climatiq.io/data/v1/search"
    )
    params = {
        "query": query,
        "data_version": os.getenv("CLIMATIQ_DATA_VERSION", "^37"),
        "results_per_page": "50",
    }
    payload = _request_json(endpoint + "?" + urlencode(params), api_key)
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not results:
        raise RuntimeError(f"Climatiq Search returned no {mode} road factors")

    def score(item):
        text = json.dumps(item).lower()
        value = 0
        if item.get("unit") == "kg/tonne-km": value += 10
        if mode == "diesel" and item.get("region") == "EU_S_AMERICA": value += 8
        if mode == "electric" and item.get("region") == "DE": value += 8
        if item.get("source") in {"GLEC", "UBA"}: value += 5
        if "heavy" in text or "hgv" in text or "truck" in text: value += 2
        return value

    return max(results, key=score)


def _catalogue(mode):
    api_key = os.getenv("Climate_API_KEY", os.getenv("CLIMATIQ_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("Climate_API_KEY is not configured")
    candidate = _catalogue_candidate(mode, api_key)
    endpoint = os.getenv(
        "CLIMATIQ_ESTIMATE_URL", "https://api.climatiq.io/data/v1/estimate"
    )
    estimate = _request_json(
        endpoint,
        api_key,
        method="POST",
        payload={
            "emission_factor": {"id": candidate["id"]},
            "parameters": {
                "weight": 1,
                "weight_unit": "t",
                "distance": 1,
                "distance_unit": "km",
            },
        },
    )
    co2e_kg = float(estimate.get("co2e") or 0)
    if co2e_kg <= 0:
        raise RuntimeError("Climatiq Estimate returned no positive CO2e value")
    # One tonne over one kilometre gives kg CO2e per tonne-km; the CSV uses g.
    source = "Climatiq Estimate: " + str(
        estimate.get("emission_factor", {}).get("name", candidate.get("name", candidate["id"]))
    )
    return co2e_kg * 1000, source


def search_emission_factors(mode):
    """Search Climatiq's catalogue for diagnostics.

    Search is useful for inspecting factor metadata. The refresh job selects a
    public result and uses the Estimate endpoint, because free-plan Search
    responses can expose ``factor: null`` even when Estimate is enabled.
    """
    api_key = os.getenv("Climate_API_KEY", os.getenv("CLIMATIQ_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("Climate_API_KEY is not configured")
    endpoint = os.getenv(
        "CLIMATIQ_SEARCH_URL", "https://api.climatiq.io/data/v1/search"
    )
    params = {
        "query": f"road freight {mode}",
        "data_version": os.getenv("CLIMATIQ_DATA_VERSION", "^37"),
        "results_per_page": "20",
    }
    return _request_json(endpoint + "?" + urlencode(params), api_key)


def refresh_emission_factors():
    """Refresh diesel/electric rows and persist successful Climatiq values.

    Returns structured status so the dashboard can show whether fallback data
    was retained; it never makes the planner unusable because of an API error.
    """
    rows = emission_factor_records()
    statuses = {}
    for row in rows:
        mode = row["mode"].strip().lower()
        try:
            value, source = _catalogue(mode)
        except RuntimeError as exc:
            statuses[mode] = {"updated": False, "reason": str(exc)}
            continue
        row["g_co2e_per_tkm"] = str(value)
        row["source"] = "Climatiq: " + source
        row["fetched_at"] = datetime.now(timezone.utc).isoformat()
        statuses[mode] = {"updated": True, "source": source, "value": value}
    path = DATA_DIR / "emission_factors.csv"
    if any(x.get("updated") for x in statuses.values()):
        fields = ["mode", "g_co2e_per_tkm", "source", "region", "fetched_at"]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=DATA_DIR, delete=False) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in fields} for row in rows)
            temporary = Path(handle.name)
        temporary.replace(path)
    return statuses
