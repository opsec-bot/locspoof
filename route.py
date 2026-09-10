"""Route geometry, GPX import/export, and the playback loop.

A route is just a list of (lat, lon) vertices. Playback walks the polyline at a
constant ground speed and pushes a new coordinate every tick, which is what the
phone sees as movement.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence

import gpxpy
import gpxpy.gpx

if TYPE_CHECKING:
    # motion imports Route from here, so this stays type-only to avoid a cycle.
    from motion import MotionPlan

log = logging.getLogger("locspoof.route")

EARTH_RADIUS_M = 6_371_000.0
DEFAULT_TICK_HZ = 1.0
MPH_TO_MPS = 0.44704
METERS_PER_MILE = 1609.344

Point = tuple[float, float]
Setter = Callable[[float, float], Awaitable[Any]]


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """Linear interpolation between two points.

    Good enough at the scale a person walks or drives between GPX vertices;
    great-circle interpolation only starts to matter over hundreds of km.
    """
    return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)


class Route:
    """A polyline with cumulative distances, so we can seek by metres travelled."""

    def __init__(self, points: Sequence[Point]) -> None:
        if len(points) < 2:
            raise ValueError("a route needs at least two points")
        self.points: list[Point] = [(float(p[0]), float(p[1])) for p in points]
        self.cumulative: list[float] = [0.0]
        for i in range(1, len(self.points)):
            seg = haversine_m(self.points[i - 1], self.points[i])
            self.cumulative.append(self.cumulative[-1] + seg)

    @property
    def length_m(self) -> float:
        return self.cumulative[-1]

    def distance_of_nearest(self, target: Point) -> float:
        """Distance along the route of the vertex closest to `target`.

        Used to place junctions and turn manoeuvres, which OSRM reports as bare
        coordinates, onto the one-dimensional distance axis the motion profile
        works in. Vertex resolution is enough: OSRM puts these points on the
        geometry it returned, so the nearest vertex is the right one.
        """
        best_index, best_distance = 0, float("inf")
        for i, point in enumerate(self.points):
            d = haversine_m(point, target)
            if d < best_distance:
                best_index, best_distance = i, d
        return self.cumulative[best_index]

    def at_distance(self, metres: float) -> Point:
        """Position at `metres` along the polyline, clamped to the endpoints."""
        if metres <= 0:
            return self.points[0]
        if metres >= self.length_m:
            return self.points[-1]
        # Small routes make a linear scan cheaper than the bisect setup cost.
        lo, hi = 0, len(self.cumulative) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cumulative[mid] <= metres:
                lo = mid
            else:
                hi = mid
        span = self.cumulative[hi] - self.cumulative[lo]
        fraction = 0.0 if span == 0 else (metres - self.cumulative[lo]) / span
        return interpolate(self.points[lo], self.points[hi], fraction)


def parse_gpx(data: str) -> list[Point]:
    """Pull every track/route point out of a GPX document, in order."""
    gpx = gpxpy.parse(data)
    points: list[Point] = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend((p.latitude, p.longitude) for p in segment.points)
    for route in gpx.routes:
        points.extend((p.latitude, p.longitude) for p in route.points)
    if not points:
        points.extend((w.latitude, w.longitude) for w in gpx.waypoints)
    return points


def build_gpx(points: Sequence[Point], name: str = "locspoof route") -> str:
    """Serialise a polyline as a single-segment GPX track."""
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack(name=name)
    segment = gpxpy.gpx.GPXTrackSegment()
    segment.points.extend(
        gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon) for lat, lon in points
    )
    track.segments.append(segment)
    gpx.tracks.append(track)
    return gpx.to_xml()


class RoutePlayer:
    """Walks a Route at a fixed speed, pushing coordinates through `setter`."""

    def __init__(self, setter: Setter) -> None:
        self._setter = setter
        self._listeners: list[Callable[[], None]] = []
        self._task: Optional[asyncio.Task[None]] = None

        self.route: Optional[Route] = None
        self.speed_mps: float = 1.4          # used only when there is no plan
        self.plan: Optional["MotionPlan"] = None
        self.current_speed_mps: float = 0.0  # what the phone is doing right now
        self.loop: bool = False
        self.pingpong: bool = False
        self.distance_m: float = 0.0
        self.running: bool = False
        self.finished: bool = False
        self.waiting: bool = False           # true while sitting at a stop
        self.stops_remaining: int = 0

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(cb)

    def _on_change(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.debug("route listener raised", exc_info=True)

    def snapshot(self) -> dict[str, Any]:
        length_m = self.route.length_m if self.route else 0.0
        return {
            "running": self.running,
            "finished": self.finished,
            "distance_m": round(self.distance_m, 1),
            "length_m": round(length_m, 1),
            "distance_mi": round(self.distance_m / METERS_PER_MILE, 3),
            "length_mi": round(length_m / METERS_PER_MILE, 3),
            # speed_mph is the setting; current_mph is what it is doing now,
            # which differs the whole time once a motion plan is driving.
            "speed_mph": round(self.speed_mps / MPH_TO_MPS, 1),
            "current_mph": round(self.current_speed_mps / MPH_TO_MPS, 1),
            "waiting": self.waiting,
            "realistic": self.plan is not None,
            "stops_total": len(self.plan.stops) if self.plan else 0,
            "stops_remaining": self.stops_remaining,
            "loop": self.loop,
            "pingpong": self.pingpong,
            "points": self.route.points if self.route else [],
            "stop_points": (
                [self.route.at_distance(s.distance_m) for s in self.plan.stops]
                if self.plan and self.route else []
            ),
        }

    async def start(
        self,
        points: Sequence[Point],
        speed_mph: float,
        loop: bool = False,
        pingpong: bool = False,
        tick_hz: float = DEFAULT_TICK_HZ,
        plan: Optional["MotionPlan"] = None,
        route: Optional[Route] = None,
    ) -> None:
        await self.stop()
        # The caller may already have built the Route to construct the plan
        # against; reuse it so distances line up exactly.
        self.route = route if route is not None else Route(points)
        self.speed_mps = max(0.05, speed_mph * MPH_TO_MPS)
        self.plan = plan
        self.current_speed_mps = 0.0
        self.waiting = False
        self.stops_remaining = len(plan.stops) if plan else 0
        self.loop = loop
        self.pingpong = pingpong
        self.distance_m = 0.0
        self.finished = False
        self.running = True
        self._task = asyncio.create_task(self._run(max(0.2, tick_hz)), name="locspoof-route")
        self._on_change()

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._on_change()

    def _arm_stops(self) -> list[Any]:
        """Fresh copy of the stop list, in order. Re-armed on every forward lap."""
        if not self.plan:
            return []
        return sorted(self.plan.stops, key=lambda s: s.distance_m)

    async def _run(self, tick_hz: float) -> None:
        assert self.route is not None
        interval = 1.0 / tick_hz
        direction = 1  # +1 forward along the polyline, -1 back toward the start
        last = time.monotonic()
        pending = self._arm_stops()
        dwell_until = 0.0
        try:
            while True:
                # Integrate against the wall clock rather than assuming each tick
                # lasted exactly `interval`. A slow push or a busy loop would
                # otherwise make the phone travel slower than the requested
                # speed, and the drift compounds over a long route.
                now = time.monotonic()
                elapsed = now - last
                last = now

                # Waiting at a stop. Position is frozen, so anything reading the
                # phone sees a genuine standstill, not a slow crawl.
                if now < dwell_until:
                    self.waiting = True
                    self.current_speed_mps = 0.0
                    await self._push(self.route.at_distance(self.distance_m))
                    self._on_change()
                    await asyncio.sleep(interval)
                    continue
                self.waiting = False

                # With a plan, speed comes from the profile and changes every
                # tick. Without one, it is the flat speed the caller asked for.
                speed = self.plan.speed_at(self.distance_m) if self.plan else self.speed_mps
                self.current_speed_mps = speed
                self.distance_m += speed * elapsed * direction

                # Reached the next stop. Land on it exactly rather than
                # overshooting, then start the dwell timer.
                if direction == 1 and pending and self.distance_m >= pending[0].distance_m:
                    stop = pending.pop(0)
                    self.distance_m = stop.distance_m
                    dwell_until = now + stop.dwell_s
                    self.stops_remaining = len(pending)
                    self.waiting = True
                    self.current_speed_mps = 0.0
                    log.debug("stopping %.1fs at %.0fm (%s)", stop.dwell_s, stop.distance_m, stop.reason)
                    await self._push(self.route.at_distance(self.distance_m))
                    self._on_change()
                    await asyncio.sleep(interval)
                    continue

                if self.distance_m >= self.route.length_m:
                    # Reached the far end.
                    if self.pingpong:
                        self.distance_m = self.route.length_m
                        direction = -1
                    elif self.loop:
                        self.distance_m = 0.0  # teleports back to the start
                        pending = self._arm_stops()
                        self.stops_remaining = len(pending)
                    else:
                        # Land exactly on the last point rather than past it,
                        # push once more, and stop.
                        self.distance_m = self.route.length_m
                        await self._push(self.route.at_distance(self.distance_m))
                        self.running = False
                        self.finished = True
                        self._on_change()
                        return
                elif self.distance_m <= 0 and direction == -1:
                    # Back at the start on a return leg. Only reachable in
                    # pingpong mode, but the non-pingpong branch terminates
                    # rather than looping forever if it ever is.
                    self.distance_m = 0.0
                    if self.pingpong:
                        direction = 1
                        # Stops only fire going forward, so a new forward leg
                        # gets a fresh set.
                        pending = self._arm_stops()
                        self.stops_remaining = len(pending)
                    else:
                        self.running = False
                        self.finished = True
                        self._on_change()
                        return

                await self._push(self.route.at_distance(self.distance_m))
                self._on_change()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("route playback stopped: %s", exc)
            self.running = False
            self._on_change()

    async def _push(self, point: Point) -> None:
        # A failed push means the channel dropped. The session records the
        # coordinate anyway and re-applies it on reconnect, so playback keeps
        # its own clock running rather than stalling.
        with contextlib.suppress(Exception):
            await self._setter(point[0], point[1])
