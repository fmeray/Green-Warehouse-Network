"""Geocoding and customer-to-site distance calculations."""
from functools import lru_cache
import csv
from hashlib import sha256
import json
from math import radians, sin, cos, asin, sqrt, isfinite
import os
from pathlib import Path
import tempfile
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:  # Keep the core runnable before optional dependencies install.
    def load_dotenv(*args, **kwargs):
        return False

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
if not os.getenv("OpenRoute_API_KEY", "").strip():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

_nominatim_lock = Lock()
_last_nominatim_request = 0.0
_cache_dir = ROOT / "data" / "cache"
_last_distance_status = {"source": "unknown"}


def _cached_customer_ids():
    """IDs present in the static matrix input; scenario additions are local."""
    try:
        with (ROOT / "data" / "customers.csv").open(encoding="utf-8", newline="") as handle:
            return {row["id"] for row in csv.DictReader(handle)}
    except (OSError, KeyError):
        return set()


class ExternalAPIError(RuntimeError):
    """Raised when an external geocoding or routing service fails."""


def haversine(a_lat, a_lon, b_lat, b_lon):
    r=6371.0; p1,p2=radians(a_lat),radians(b_lat); dp=radians(b_lat-a_lat); dl=radians(b_lon-a_lon)
    h=sin(dp/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return 2*r*asin(sqrt(h))


def _request_json(request, service):
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error") or json.loads(body).get("message") or body
        except (TypeError, json.JSONDecodeError):
            message = body
        raise ExternalAPIError(f"{service} request failed with HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise ExternalAPIError(f"{service} request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ExternalAPIError(f"{service} returned invalid JSON") from exc


def geocode_address(address):
    """Resolve a user-entered address through Nominatim."""
    query = address.strip()
    if not query:
        raise ValueError("Enter an address to geocode.")
    endpoint = os.getenv(
        "Nominatim_API", "https://nominatim.openstreetmap.org/search"
    ).strip()
    user_agent = os.getenv(
        "NOMINATIM_USER_AGENT", "GreenWarehouseNetwork/1.0"
    ).strip()
    return _geocode_cached(query, endpoint, user_agent)


@lru_cache(maxsize=256)
def _geocode_cached(query, endpoint, user_agent):
    global _last_nominatim_request

    with _nominatim_lock:
        wait = 1.0 - (time.monotonic() - _last_nominatim_request)
        if wait > 0:
            time.sleep(wait)
        _last_nominatim_request = time.monotonic()

    parts = urlsplit(endpoint)
    params = dict(parse_qsl(parts.query))
    params.update({"q": query, "format": "jsonv2", "limit": "1", "countrycodes": "de"})
    url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
    )
    request = Request(url, headers={"User-Agent": user_agent})
    results = _request_json(request, "Nominatim")
    if not isinstance(results, list) or not results:
        raise ExternalAPIError(f"Nominatim found no result for {query!r}.")
    try:
        result = results[0]
        lat, lon = float(result["lat"]), float(result["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordinates out of range")
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalAPIError("Nominatim returned an invalid location.") from exc
    return {
        "lat": lat,
        "lon": lon,
        "display_name": result.get("display_name", query),
    }


def _distance_cache_key(customers, sites, endpoint):
    locations = {
        "customers": [(c["id"], c["lat"], c["lon"]) for c in customers],
        "sites": [(s["id"], s["lat"], s["lon"]) for s in sites],
        "endpoint": endpoint,
    }
    return sha256(json.dumps(locations, sort_keys=True).encode()).hexdigest()


def _validate_matrix(matrix, customers, sites):
    if not isinstance(matrix, list) or len(matrix) != len(customers):
        raise ExternalAPIError("OpenRouteService returned an invalid distance matrix.")
    try:
        validated = []
        for row in matrix:
            if not isinstance(row, list) or len(row) != len(sites):
                raise ValueError("unexpected row size")
            values = [float(value) for value in row]
            if any(not isfinite(value) or value < 0 for value in values):
                raise ValueError("invalid distance")
            validated.append(values)
    except (TypeError, ValueError) as exc:
        raise ExternalAPIError("OpenRouteService returned invalid distances.") from exc
    return validated


def _openroute_distance_matrix(customers, sites):
    global _last_distance_status
    endpoint = os.getenv(
        "OPENROUTE_MATRIX_URL",
        "https://api.openrouteservice.org/v2/matrix/driving-hgv",
    ).strip()
    cache_key = _distance_cache_key(customers, sites, endpoint)
    cache_path = _cache_dir / f"distance-{cache_key}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalAPIError("Could not read the cached route matrix.") from exc
        matrix = _validate_matrix(cached, customers, sites)
        _last_distance_status = {"source": "openrouteservice_cache", "cache": str(cache_path)}
    else:
        raise ExternalAPIError(
            "No cached OpenRouteService distance matrix is available; "
            "the solver does not call OpenRouteService at runtime."
        )

    return {
        customer["id"]: {
            site["id"]: matrix[i][j]
            for j, site in enumerate(sites)
        }
        for i, customer in enumerate(customers)
    }


def distance_matrix(customers, sites):
    global _last_distance_status
    static_ids = _cached_customer_ids()
    routed = [customer for customer in customers if customer["id"] in static_ids]
    estimated = [customer for customer in customers if customer["id"] not in static_ids]
    try:
        matrix = _openroute_distance_matrix(routed, sites) if routed else {}
        for customer in estimated:
            matrix[customer["id"]] = {
                site["id"]: 1.3 * haversine(customer["lat"], customer["lon"], site["lat"], site["lon"])
                for site in sites
            }
        if estimated:
            _last_distance_status["estimated_customer_ids"] = [customer["id"] for customer in estimated]
        return matrix
    except ExternalAPIError as exc:
        # Distances are prepared data. A missing cache is estimated locally;
        # the solver never reaches out to OpenRouteService during a run.
        _last_distance_status = {
            "source": "haversine_fallback",
            "error_type": "tool_failure",
            "code": "distance_cache_unavailable",
            "details": [f"{exc} Haversine distance × 1.3 was used instead."],
            "suggested_fix": {"fallback": "haversine distance × 1.3"},
        }
        return {
            customer["id"]: {
                site["id"]: 1.3 * haversine(customer["lat"], customer["lon"], site["lat"], site["lon"])
                for site in sites
            }
            for customer in customers
        }


def distance_status():
    """Return provenance for the most recent distance calculation."""
    return dict(_last_distance_status)
