"""Geocoding and customer-to-site distance calculations."""
from functools import lru_cache
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

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

_nominatim_lock = Lock()
_last_nominatim_request = 0.0
_cache_dir = ROOT / "data" / "cache"


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
        raise ExternalAPIError(f"{service} request failed with HTTP {exc.code}") from exc
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
    params.update({"q": query, "format": "jsonv2", "limit": "1"})
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
    endpoint = os.getenv(
        "OPENROUTE_MATRIX_URL",
        "https://api.openrouteservice.org/v2/matrix/driving-car",
    ).strip()
    cache_key = _distance_cache_key(customers, sites, endpoint)
    cache_path = _cache_dir / f"distance-{cache_key}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalAPIError("Could not read the cached route matrix.") from exc
        matrix = _validate_matrix(cached, customers, sites)
    else:
        locations = [
            [c["lon"], c["lat"]] for c in customers
        ] + [[s["lon"], s["lat"]] for s in sites]
        payload = {
            "locations": locations,
            "sources": [str(i) for i in range(len(customers))],
            "destinations": [
                str(i) for i in range(len(customers), len(locations))
            ],
            "metrics": ["distance"],
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": os.environ["OpenRoute_API_KEY"].strip(),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response = _request_json(request, "OpenRouteService")
        if not isinstance(response, dict) or "distances" not in response:
            raise ExternalAPIError(
                "OpenRouteService response did not contain a distance matrix."
            )
        # The Matrix API returns distances in meters unless units are requested.
        matrix = [
            [distance / 1000 for distance in row]
            for row in _validate_matrix(
                response["distances"], customers, sites
            )
        ]
        _cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=_cache_dir, delete=False
            ) as cache_file:
                json.dump(matrix, cache_file)
                temporary_path = Path(cache_file.name)
            temporary_path.replace(cache_path)
        except OSError as exc:
            raise ExternalAPIError("Could not cache the route matrix.") from exc

    return {
        customer["id"]: {
            site["id"]: matrix[i][j]
            for j, site in enumerate(sites)
        }
        for i, customer in enumerate(customers)
    }


def distance_matrix(customers, sites):
    api_key = os.getenv("OpenRoute_API_KEY", "").strip()
    if api_key:
        return _openroute_distance_matrix(customers, sites)
    return {c['id']:{s['id']:haversine(c['lat'],c['lon'],s['lat'],s['lon']) for s in sites} for c in customers}
