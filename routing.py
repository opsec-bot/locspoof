"""Road-following route planning and place search.

Routing goes to public OSRM instances. OSRM answers with Contraction
Hierarchies, a preprocessed Dijkstra variant, so a cross-city query comes back
in milliseconds. Running the search locally would mean holding the OSM road
graph on disk, which is gigabytes per region for no benefit here.

Every endpoint below is keyless. Each profile lists more than one instance and
they are tried in order, because the public demo servers go down regularly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Sequence

import aiohttp

log = logging.getLogger("locspoof.routing")

# Nominatim's usage policy requires a User-Agent that identifies the client.
USER_AGENT = "locspoof/1.0 (local iPhone location simulator)"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# OSRM's own demo server only carries the car profile. The FOSSGIS instances
# carry car, bike and foot separately, so they come first for walk and bike.
PROFILE_ENDPOINTS: dict[str, list[str]] = {
    "drive": [
        "https://router.project-osrm.org/route/v1/driving/",
        "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
    ],
    "bike": [
        "https://routing.openstreetmap.de/routed-bike/route/v1/driving/",
        "https://router.project-osrm.org/route/v1/driving/",
    ],
    "walk": [
        "https://routing.openstreetmap.de/routed-foot/route/v1/driving/",
        "https://router.project-osrm.org/route/v1/driving/",
    ],
}

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"

Point = tuple[float, float]


class RoutingError(Exception):
    """No engine could answer the request."""


def _coord_list(points: Sequence[Point]) -> str:
    """OSRM wants lon,lat pairs joined by semicolons."""
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)


async def plan_route(points: Sequence[Point], profile: str = "drive") -> dict[str, Any]:
    """Snap `points` to the road network and return the full geometry.

    :param points: two or more (lat, lon) waypoints, in visiting order.
    :param profile: one of drive, bike, walk.
    :returns: dict with points (lat, lon list), distance_m, duration_s, engine.
    """
    if len(points) < 2:
        raise RoutingError("need at least two points to plan a route")
    endpoints = PROFILE_ENDPOINTS.get(profile) or PROFILE_ENDPOINTS["drive"]
    coords = _coord_list(points)
    params = {"overview": "full", "geometries": "geojson", "continue_straight": "false"}

    errors: list[str] = []
    async with aiohttp.ClientSession(
        timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as http:
        for base in endpoints:
            url = base + coords
            try:
                async with http.get(url, params=params) as response:
                    if response.status != 200:
                        errors.append(f"{base} -> HTTP {response.status}")
                        continue
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                errors.append(f"{base} -> {type(exc).__name__}: {exc}")
                continue

            if payload.get("code") != "Ok" or not payload.get("routes"):
                errors.append(f"{base} -> {payload.get('code')}: {payload.get('message', '')}")
                continue

            route = payload["routes"][0]
            # GeoJSON is [lon, lat]; the rest of this program uses (lat, lon).
            coordinates = route.get("geometry", {}).get("coordinates") or []
            latlon = [(float(c[1]), float(c[0])) for c in coordinates]
            if len(latlon) < 2:
                errors.append(f"{base} -> route had no geometry")
                continue

            log.info(
                "planned %s route: %d points, %.0f m via %s",
                profile, len(latlon), route.get("distance", 0.0), base
            )
            return {
                "points": latlon,
                "distance_m": float(route.get("distance") or 0.0),
                "duration_s": float(route.get("duration") or 0.0),
                "engine": base,
            }

    raise RoutingError("; ".join(errors) or "no routing engine responded")


async def geocode(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Look up a place name or address through Nominatim."""
    query = query.strip()
    if not query:
        return []
    params = {"q": query, "format": "jsonv2", "limit": str(max(1, min(limit, 20)))}
    async with aiohttp.ClientSession(
        timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as http:
        async with http.get(NOMINATIM_SEARCH, params=params) as response:
            if response.status != 200:
                raise RoutingError(f"search failed: HTTP {response.status}")
            rows = await response.json(content_type=None)
    return [
        {
            "name": row.get("display_name", ""),
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "kind": row.get("type", ""),
        }
        for row in rows
        if row.get("lat") and row.get("lon")
    ]
