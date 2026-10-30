"""Real traffic lights and stop signs along a route, from OpenStreetMap.

The lookup lives in sitescrape-catalog as the `openstreetmap.org/traffic-controls`
endpoint, so the Overpass query is written and tested once and other tools can
call it too. locspoof talks to a sitescrape REST server:

- `SITESCRAPE_URL` set: use that server (for example one left running with
  `npx sitescrape serve` in a sitescrape-catalog clone).
- otherwise: start `npx sitescrape serve` in `SITESCRAPE_CATALOG_DIR`
  (default ~/Documents/GitHub/sitescrape-catalog) on first use and stop it on exit.

Lights and signs are cached on disk by map tile (TILE_DEG, about 1 km), so only
tiles never seen before go to Overpass. A route through a familiar area is
instant and works with Overpass down.

Every failure returns None, and the motion model falls back to guessing stops
from junctions, so a missing sitescrape never stops a route from playing.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import aiohttp

from route import Point

log = logging.getLogger("locspoof.controls")

ENDPOINT = "openstreetmap.org/traffic-controls"
DEFAULT_CATALOG_DIR = Path.home() / "Documents" / "GitHub" / "sitescrape-catalog"
# Overpass is a shared free service and can be slow; a route plan waits this long
# at most before falling back to guessed stops.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=90)
# Tile edge in degrees. About 1.1 km north-south; an 8 km city drive touches
# around twenty of them.
TILE_DEG = 0.01
CACHE_DIR = Path(os.environ.get("LOCSPOOF_CACHE_DIR", Path.home() / ".locspoof")) / "osm-controls"
# Lights and signs rarely move. Refetch a tile after this long anyway.
CACHE_TTL_S = 30 * 24 * 3600
# A sign within this distance of the route counts as on it. OSRM routes follow
# the same OSM ways, so anything wider starts catching side streets.
NEAR_M = 8.0
# Tiles per sitescrape call, well under the endpoint's limit of 400.
BATCH = 150

_server: Optional[subprocess.Popen] = None
_server_url: Optional[str] = None
_server_lock = asyncio.Lock()


Tile = tuple[int, int]


def tile_of(lat: float, lon: float) -> Tile:
    return math.floor(lat / TILE_DEG), math.floor(lon / TILE_DEG)


def tile_box(tile: Tile) -> list[float]:
    s, w = tile[0] * TILE_DEG, tile[1] * TILE_DEG
    return [round(s, 6), round(w, 6), round(s + TILE_DEG, 6), round(w + TILE_DEG, 6)]


def tiles_for(points: Sequence[Point]) -> set[Tile]:
    """Every tile the route passes through, sampling each segment finely
    enough that a diagonal cannot skip the corner of one."""
    tiles: set[Tile] = set()
    step = TILE_DEG / 4
    for (a_lat, a_lon), (b_lat, b_lon) in zip(points, points[1:]):
        n = max(1, int(max(abs(b_lat - a_lat), abs(b_lon - a_lon)) / step) + 1)
        for i in range(n + 1):
            f = i / n
            tiles.add(tile_of(a_lat + (b_lat - a_lat) * f, a_lon + (b_lon - a_lon) * f))
    if len(points) == 1:
        tiles.add(tile_of(*points[0]))
    return tiles


def _cache_path(tile: Tile) -> Path:
    return CACHE_DIR / f"{tile[0]}_{tile[1]}.json"


def load_tile(tile: Tile) -> Optional[list[dict[str, Any]]]:
    try:
        data = json.loads(_cache_path(tile).read_text(encoding="utf-8"))
        if time.time() - data["fetched_at"] > CACHE_TTL_S:
            return None
        return data["controls"]
    except (OSError, ValueError, KeyError):
        return None


def save_tile(tile: Tile, controls: list[dict[str, Any]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(tile).write_text(
            json.dumps({"fetched_at": time.time(), "controls": controls}), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("could not cache tile %s: %s", tile, exc)


def near_route(points: Sequence[Point], controls: Sequence[dict[str, Any]], max_m: float = NEAR_M) -> list[dict[str, Any]]:
    """Controls within max_m of the route line.

    Segments are bucketed by tile so each control is only checked against the
    few segments around it.
    """
    buckets: dict[Tile, list[int]] = {}
    for i, ((a_lat, a_lon), (b_lat, b_lon)) in enumerate(zip(points, points[1:])):
        lo, hi = tile_of(min(a_lat, b_lat), min(a_lon, b_lon)), tile_of(max(a_lat, b_lat), max(a_lon, b_lon))
        for ty in range(lo[0], hi[0] + 1):
            for tx in range(lo[1], hi[1] + 1):
                buckets.setdefault((ty, tx), []).append(i)

    kept = []
    for c in controls:
        lat, lon = c["lat"], c["lon"]
        # Metres per degree here; flat-earth is fine over a few metres.
        my, mx = 111_320.0, 111_320.0 * math.cos(math.radians(lat))
        ty, tx = tile_of(lat, lon)
        best = math.inf
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for i in buckets.get((ty + dy, tx + dx), ()):
                    (a_lat, a_lon), (b_lat, b_lon) = points[i], points[i + 1]
                    ax, ay = (a_lon - lon) * mx, (a_lat - lat) * my
                    bx, by = (b_lon - lon) * mx, (b_lat - lat) * my
                    vx, vy = bx - ax, by - ay
                    length2 = vx * vx + vy * vy
                    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * vx + ay * vy) / length2))
                    best = min(best, math.hypot(ax + vx * t, ay + vy * t))
        if best <= max_m:
            kept.append(c)
    return kept


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_server() -> None:
    global _server
    if _server and _server.poll() is None:
        _server.terminate()
    _server = None


async def _server_base(session: aiohttp.ClientSession) -> Optional[str]:
    """URL of a sitescrape server, starting one if needed."""
    global _server, _server_url
    configured = os.environ.get("SITESCRAPE_URL")
    if configured:
        return configured.rstrip("/")

    async with _server_lock:
        if _server_url and _server and _server.poll() is None:
            return _server_url

        catalog = Path(os.environ.get("SITESCRAPE_CATALOG_DIR", DEFAULT_CATALOG_DIR))
        npx = shutil.which("npx")
        if not npx or not (catalog / "node_modules" / "sitescrape").exists():
            log.info("sitescrape not found at %s; stops will be guessed from junctions", catalog)
            return None

        port = _free_port()
        _server = subprocess.Popen(
            [npx, "sitescrape", "serve", "--port", str(port)],
            cwd=catalog,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atexit.register(_stop_server)
        url = f"http://127.0.0.1:{port}"
        # npx plus tsx takes a few seconds to come up.
        for _ in range(60):
            if _server.poll() is not None:
                log.warning("sitescrape serve exited with %s", _server.returncode)
                return None
            try:
                async with session.get(url + "/", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        _server_url = url
                        log.info("started sitescrape on %s", url)
                        return url
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.5)
        log.warning("sitescrape serve did not come up")
        _stop_server()
        return None


async def _fetch_tiles(session: aiohttp.ClientSession, base: str, tiles: list[Tile]) -> bool:
    """Fetch tiles from sitescrape and cache each one. False if any batch failed."""
    headers = {}
    token = os.environ.get("SITESCRAPE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for start in range(0, len(tiles), BATCH):
        batch = tiles[start:start + BATCH]
        body = {"boxes": [tile_box(t) for t in batch]}
        async with session.post(f"{base}/{ENDPOINT}", json=body, headers=headers) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                log.warning("traffic controls failed: %s", (payload.get("error") or {}).get("message"))
                return False
        found: dict[Tile, list[dict[str, Any]]] = {t: [] for t in batch}
        for c in payload["data"]["controls"]:
            found.setdefault(tile_of(c["lat"], c["lon"]), []).append(c)
        for tile in batch:
            # An empty tile is cached too: no lights there is an answer.
            save_tile(tile, found[tile])
    return True


async def fetch_controls(points: Sequence[Point]) -> Optional[list[dict[str, Any]]]:
    """Traffic controls on the route, or None if they could not be had.

    Only tiles missing from the cache are fetched. If any of those fail, the
    result would undercount stops, so it is None and stops are guessed.
    """
    if len(points) < 2:
        return None
    tiles = sorted(tiles_for(points))
    missing = [t for t in tiles if load_tile(t) is None]
    if missing:
        log.info("traffic controls: %d of %d tiles not cached, fetching", len(missing), len(tiles))
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                base = await _server_base(session)
                if not base or not await _fetch_tiles(session, base, missing):
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as exc:
            log.warning("traffic controls failed: %s", exc or type(exc).__name__)
            return None

    controls: list[dict[str, Any]] = []
    for tile in tiles:
        cached = load_tile(tile)
        if cached is None:
            return None
        controls.extend(cached)
    return near_route(points, controls)
