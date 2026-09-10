"""Local HTTP API and static file serving.

Binds to loopback only. There is no auth because there is no remote surface:
anything that can reach this port can already run code as you.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from device import LocationSession
from motion import MotionOptions, build_plan
from route import Route, RoutePlayer, build_gpx, parse_gpx
from routing import RoutingError, geocode, plan_route

log = logging.getLogger("locspoof.server")

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY_BYTES = 8 * 1024 * 1024  # GPX files can be large; cap it anyway


def _combined_status(session: LocationSession, player: RoutePlayer) -> dict[str, Any]:
    return {"device": session.snapshot(), "route": player.snapshot()}


def _coord(value: Any, lo: float, hi: float, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason=f"{name} must be a number")
    if not (lo <= out <= hi) or out != out:  # NaN fails the range test too
        raise web.HTTPBadRequest(reason=f"{name} out of range")
    return out


def build_app(session: LocationSession, player: RoutePlayer) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)

    # The road detail that feeds the motion model (per-segment speeds, junction
    # and turn positions) comes back from /api/route/plan. Rather than make the
    # browser echo all of it back on start, the last plan is kept here and
    # matched by geometry. A manual or imported route simply has no entry.
    last_plan: dict[str, Any] = {}

    def detail_for(points: list[tuple[float, float]]) -> dict[str, Any]:
        """Road detail for these points, if they are the ones last planned."""
        cached = last_plan.get("points")
        if not cached or len(cached) != len(points):
            return {}
        if cached[0] != points[0] or cached[-1] != points[-1]:
            return {}
        return last_plan

    async def index(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html")

    async def status(_request: web.Request) -> web.StreamResponse:
        return web.json_response(_combined_status(session, player))

    async def events(request: web.Request) -> web.StreamResponse:
        """Server-sent events carrying the full status on every change."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

        def wake() -> None:
            # Coalescing queue: a pending notification already covers this one.
            if queue.empty():
                queue.put_nowait(None)

        session.add_listener(wake)
        player_change = wake
        player.add_listener(player_change)
        try:
            await response.write(
                b"data: " + json.dumps(_combined_status(session, player)).encode() + b"\n\n"
            )
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")  # keep proxies and tabs alive
                    continue
                payload = json.dumps(_combined_status(session, player)).encode()
                await response.write(b"data: " + payload + b"\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            session.remove_listener(wake)
            player.remove_listener(player_change)
        return response

    async def set_location(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        lat = _coord(body.get("lat"), -90, 90, "lat")
        lon = _coord(body.get("lon"), -180, 180, "lon")
        # A manual pin overrides any route in progress.
        await player.stop()
        result = await session.set_point(lat, lon)
        return web.json_response(result)

    async def clear_location(_request: web.Request) -> web.StreamResponse:
        await player.stop()
        return web.json_response(await session.clear())

    async def set_jitter(request: web.Request) -> web.StreamResponse:
        """Toggle the wander applied to a stationary pin."""
        body = await request.json()
        session.pin_jitter_enabled = bool(body.get("enabled", True))
        return web.json_response({"ok": True, "enabled": session.pin_jitter_enabled})

    async def route_start(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        raw = body.get("points") or []
        if len(raw) < 2:
            raise web.HTTPBadRequest(reason="a route needs at least two points")
        points = [
            (_coord(p[0], -90, 90, "lat"), _coord(p[1], -180, 180, "lon"))
            for p in raw
        ]
        speed = float(body.get("speed_mph") or 3.0)
        if not (0 < speed <= 600):
            raise web.HTTPBadRequest(reason="speed_mph out of range")

        route = Route(points)
        plan = None
        if bool(body.get("realistic", True)):
            detail = detail_for(points)
            profile = str(body.get("profile") or detail.get("profile") or "drive")
            options = MotionOptions(
                profile=profile if profile in ("drive", "bike", "walk") else "drive",
                # An explicit mph acts as a ceiling; leaving it off lets the
                # road's own speeds drive entirely.
                speed_mph=None if body.get("use_road_speeds") and not body.get("speed_mph") else speed,
                use_road_speeds=bool(body.get("use_road_speeds", True)) and bool(detail),
                stops_enabled=bool(body.get("stops", True)),
                seed=body.get("seed"),
            )
            # Junctions and turns arrive as coordinates; the profile works in
            # distance along the route, so project them onto that axis.
            junctions = [route.distance_of_nearest(tuple(p)) for p in detail.get("junctions", [])]
            turns = [route.distance_of_nearest(tuple(p)) for p in detail.get("turns", [])]
            plan = build_plan(
                route,
                options,
                segment_speeds=detail.get("segment_speeds"),
                junctions_m=junctions,
                turns_m=turns,
            )

        await player.start(
            points,
            speed_mph=speed,
            loop=bool(body.get("loop")),
            pingpong=bool(body.get("pingpong")),
            plan=plan,
            route=route,
            jitter=bool(body.get("jitter", True)),
            jitter_seed=body.get("seed"),
        )
        snapshot = player.snapshot()
        if plan is not None:
            snapshot["estimated_s"] = round(plan.estimated_duration_s(), 1)
            snapshot["dwell_s"] = round(plan.dwell_total_s(), 1)
        return web.json_response({"ok": True, **snapshot})

    async def route_plan(request: web.Request) -> web.StreamResponse:
        """Snap waypoints to real roads and return the full geometry."""
        body = await request.json()
        raw = body.get("points") or []
        if len(raw) < 2:
            raise web.HTTPBadRequest(reason="need a start and an end")
        waypoints = [
            (_coord(p[0], -90, 90, "lat"), _coord(p[1], -180, 180, "lon"))
            for p in raw
        ]
        profile = str(body.get("profile") or "drive")
        if profile not in ("drive", "bike", "walk"):
            raise web.HTTPBadRequest(reason="profile must be drive, bike or walk")
        try:
            result = await plan_route(waypoints, profile=profile)
        except RoutingError as exc:
            # 502: we are fine, the upstream engines are not.
            raise web.HTTPBadGateway(reason=f"routing failed: {exc}")

        # Keep the road detail for the motion model, but do not ship the bulky
        # per-segment arrays to the browser; it has no use for them.
        last_plan.clear()
        last_plan.update(result)
        last_plan["profile"] = profile
        return web.json_response({
            "ok": True,
            "points": result["points"],
            "distance_m": result["distance_m"],
            "duration_s": result["duration_s"],
            "engine": result["engine"],
            "junction_count": len(result.get("junctions", [])),
            "turn_count": len(result.get("turns", [])),
        })

    async def geocode_search(request: web.Request) -> web.StreamResponse:
        query = request.query.get("q", "")
        try:
            results = await geocode(query)
        except RoutingError as exc:
            raise web.HTTPBadGateway(reason=str(exc))
        return web.json_response({"ok": True, "results": results})

    async def route_stop(_request: web.Request) -> web.StreamResponse:
        await player.stop()
        return web.json_response({"ok": True})

    async def gpx_import(request: web.Request) -> web.StreamResponse:
        data = await request.text()
        try:
            points = parse_gpx(data)
        except Exception as exc:
            raise web.HTTPBadRequest(reason=f"could not parse GPX: {exc}")
        if len(points) < 2:
            raise web.HTTPBadRequest(reason="GPX contained fewer than two points")
        return web.json_response({"ok": True, "points": points})

    async def gpx_export(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        points = [(float(p[0]), float(p[1])) for p in (body.get("points") or [])]
        if len(points) < 2:
            raise web.HTTPBadRequest(reason="nothing to export")
        return web.Response(
            body=build_gpx(points).encode("utf-8"),
            headers={
                "Content-Type": "application/gpx+xml",
                "Content-Disposition": 'attachment; filename="route.gpx"',
            },
        )

    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/status", status),
            web.get("/api/events", events),
            web.post("/api/location", set_location),
            web.post("/api/clear", clear_location),
            web.post("/api/jitter", set_jitter),
            web.post("/api/route/start", route_start),
            web.post("/api/route/stop", route_stop),
            web.post("/api/route/plan", route_plan),
            web.get("/api/geocode", geocode_search),
            web.post("/api/gpx/import", gpx_import),
            web.post("/api/gpx/export", gpx_export),
            web.static("/static", WEB_DIR),
        ]
    )
    return app
