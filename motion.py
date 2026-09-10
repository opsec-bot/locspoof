"""Speed profiles and stops, so playback does not look like a metronome.

A constant-speed walk down a polyline is the loudest tell a simulated location
has. Nothing real holds 11.176 m/s for twenty minutes, takes a 90-degree corner
without slowing, or crosses forty intersections without ever waiting.

This module turns a route into a velocity profile: speed as a function of
distance travelled, honouring road speed limits, corner geometry, and
acceleration limits, with timed stops inserted at junctions.

The profile is built once, up front, in four passes:

1. A speed ceiling per sample, from OSRM's per-segment speeds when available.
2. Corner braking, from the turn angle at each vertex.
3. Stops, chosen at real junctions and turns, which pin the ceiling to zero.
4. A forward pass limiting acceleration and a backward pass limiting
   deceleration, which is the standard way to make a velocity profile
   physically reachable in both directions.

Pass 4 is what makes the result feel real: the phone eases away from a stop and
brakes into it, instead of teleporting between speeds.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional, Sequence

from route import Route

Point = tuple[float, float]

MPH_TO_MPS = 0.44704

# Sampling resolution along the route. Two metres is fine detail for a corner
# without making a cross-city route enormous; the cap keeps memory bounded on
# very long routes by coarsening instead of growing without limit.
SAMPLE_STEP_M = 2.0
MAX_SAMPLES = 40_000

# Speed floor at a stop. It is deliberately not zero: the player advances by
# v * dt, so a true zero would mean never reaching the point it is braking for.
# The vehicle creeps in at this speed and the actual standstill is the dwell
# timer, during which position does not change at all.
STOP_FLOOR_MPS = 0.6


@dataclass(frozen=True)
class ProfileParams:
    """Per-travel-mode physics and behaviour."""

    accel: float           # m/s^2 pulling away
    decel: float           # m/s^2 braking, normally harder than accelerating
    default_speed: float   # m/s when no road data is available
    max_speed: float       # m/s ceiling for the mode
    corner_speeds: tuple[tuple[float, float], ...]  # (degrees, m/s cap)
    junction_stop_chance: float
    turn_stop_chance: float
    stop_dwell: tuple[float, float]  # seconds, uniform range


# Cars brake harder than they accelerate and lose a lot of speed in a corner.
# Pedestrians barely change pace to turn, and rarely stop dead at a junction.
PARAMS: dict[str, ProfileParams] = {
    "drive": ProfileParams(
        accel=1.4, decel=2.4, default_speed=13.4, max_speed=40.0,
        corner_speeds=((20, 1e9), (45, 11.0), (80, 7.0), (120, 4.5), (180, 3.0)),
        junction_stop_chance=0.30, turn_stop_chance=0.55, stop_dwell=(4.0, 22.0),
    ),
    "bike": ProfileParams(
        accel=0.9, decel=1.8, default_speed=5.0, max_speed=12.0,
        corner_speeds=((25, 1e9), (60, 4.5), (100, 3.0), (180, 2.0)),
        junction_stop_chance=0.22, turn_stop_chance=0.35, stop_dwell=(3.0, 15.0),
    ),
    "walk": ProfileParams(
        accel=0.6, decel=0.9, default_speed=1.35, max_speed=2.6,
        corner_speeds=((60, 1e9), (120, 1.1), (180, 0.9)),
        junction_stop_chance=0.12, turn_stop_chance=0.15, stop_dwell=(2.0, 12.0),
    ),
}


@dataclass
class Stop:
    """A pause at a fixed distance along the route."""

    distance_m: float
    dwell_s: float
    reason: str


@dataclass
class MotionOptions:
    """What the caller wants; everything here has a sane default."""

    profile: str = "drive"
    # None means "use the road's own speeds". A number pins the whole route to
    # that speed as a ceiling, which is what the mph box does.
    speed_mph: Optional[float] = None
    use_road_speeds: bool = True
    stops_enabled: bool = True
    seed: Optional[int] = None


@dataclass
class MotionPlan:
    """A velocity profile plus the stops along it."""

    step_m: float
    speeds: list[float]                     # m/s, indexed by sample
    stops: list[Stop] = field(default_factory=list)
    length_m: float = 0.0

    def speed_at(self, distance_m: float) -> float:
        """Speed at a distance along the route, linearly interpolated."""
        if not self.speeds:
            return 0.0
        if distance_m <= 0:
            return self.speeds[0]
        idx = distance_m / self.step_m
        low = int(idx)
        if low >= len(self.speeds) - 1:
            return self.speeds[-1]
        frac = idx - low
        return self.speeds[low] * (1 - frac) + self.speeds[low + 1] * frac

    def estimated_duration_s(self) -> float:
        """Time to traverse the whole profile, including dwell at stops.

        Integrates dt = ds / v over the samples. Speeds are clamped away from
        zero so a stop, which is zero by construction, cannot make this diverge;
        the real waiting time is the dwell total, added separately.
        """
        total = self.dwell_total_s()
        for v in self.speeds[:-1]:
            total += self.step_m / max(v, 0.35)
        return total

    def dwell_total_s(self) -> float:
        return sum(s.dwell_s for s in self.stops)


def _bearing(a: Point, b: Point) -> float:
    """Initial bearing from a to b, in degrees."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def turn_angle(prev: Point, here: Point, nxt: Point) -> float:
    """How sharply the path turns at `here`, in degrees. 0 is dead straight."""
    delta = abs(_bearing(here, nxt) - _bearing(prev, here))
    if delta > 180.0:
        delta = 360.0 - delta
    return delta


def _corner_cap(angle: float, params: ProfileParams) -> float:
    """Speed ceiling for a corner of the given angle."""
    for threshold, cap in params.corner_speeds:
        if angle <= threshold:
            return cap
    return params.corner_speeds[-1][1]


def build_plan(
    route: Route,
    options: MotionOptions,
    segment_speeds: Optional[Sequence[Optional[float]]] = None,
    junctions_m: Optional[Sequence[float]] = None,
    turns_m: Optional[Sequence[float]] = None,
) -> MotionPlan:
    """Turn a route into a velocity profile.

    :param route: the polyline to traverse.
    :param options: mode, speed preference, whether to insert stops.
    :param segment_speeds: OSRM per-segment speeds in m/s, one per geometry
        segment. Missing or None entries fall back to the mode default.
    :param junctions_m: distances along the route of real road junctions.
    :param turns_m: distances along the route of turn manoeuvres.
    """
    params = PARAMS.get(options.profile) or PARAMS["drive"]
    rng = random.Random(options.seed)

    length = route.length_m
    step = SAMPLE_STEP_M
    if length / step > MAX_SAMPLES:
        step = length / MAX_SAMPLES
    count = max(2, int(length / step) + 1)

    # ---- pass 1: ceiling from road speeds -------------------------------
    user_cap = options.speed_mph * MPH_TO_MPS if options.speed_mph else None
    ceiling = [0.0] * count
    for i in range(count):
        s = i * step
        base = params.default_speed
        if options.use_road_speeds and segment_speeds:
            road = _speed_for_distance(route, segment_speeds, s)
            if road:
                base = road
        if user_cap is not None:
            # The mph box is a ceiling, not an override: a 65 mph setting still
            # crawls through a parking lot, it just never exceeds 65.
            base = min(base, user_cap) if options.use_road_speeds and segment_speeds else user_cap
        ceiling[i] = min(base, params.max_speed)

    # ---- pass 2: corner braking ------------------------------------------
    for idx in range(1, len(route.points) - 1):
        angle = turn_angle(route.points[idx - 1], route.points[idx], route.points[idx + 1])
        cap = _corner_cap(angle, params)
        if cap >= 1e8:
            continue
        at = route.cumulative[idx]
        # Apply the cap over a short window around the vertex, not a single
        # sample, so the corner has width and the smoothing passes have
        # something to brake into.
        for i in _window(at, 6.0, step, count):
            ceiling[i] = min(ceiling[i], cap)

    # ---- pass 3: stops ----------------------------------------------------
    stops: list[Stop] = []
    if options.stops_enabled:
        candidates: list[tuple[float, str, float]] = []
        for d in junctions_m or ():
            candidates.append((d, "junction", params.junction_stop_chance))
        for d in turns_m or ():
            candidates.append((d, "turn", params.turn_stop_chance))
        candidates.sort()

        last_stop = -1e9
        for distance, reason, chance in candidates:
            # Never stop twice within 40 m, or right at either end of the route.
            if distance - last_stop < 40.0:
                continue
            if distance < 15.0 or distance > length - 15.0:
                continue
            if rng.random() > chance:
                continue
            dwell = rng.uniform(*params.stop_dwell)
            stops.append(Stop(distance_m=distance, dwell_s=dwell, reason=reason))
            last_stop = distance
            for i in _window(distance, 1.5, step, count):
                ceiling[i] = min(ceiling[i], STOP_FLOOR_MPS)

    # A journey starts from a standstill and ends at one. Without this the
    # phone appears already travelling at full speed at the first coordinate,
    # which nothing real does. Same floor as a stop, and for the same reason:
    # an exact zero here would mean never pulling away.
    ceiling[0] = min(ceiling[0], STOP_FLOOR_MPS)
    ceiling[-1] = min(ceiling[-1], STOP_FLOOR_MPS)

    # ---- pass 4: make it physically reachable -----------------------------
    speeds = _limit_acceleration(ceiling, step, params.accel, params.decel)

    return MotionPlan(step_m=step, speeds=speeds, stops=stops, length_m=length)


def _window(centre_m: float, half_width_m: float, step: float, count: int) -> range:
    lo = max(0, int((centre_m - half_width_m) / step))
    hi = min(count - 1, int((centre_m + half_width_m) / step))
    return range(lo, hi + 1)


def _speed_for_distance(
    route: Route, segment_speeds: Sequence[Optional[float]], distance_m: float
) -> Optional[float]:
    """OSRM speed for the geometry segment containing `distance_m`."""
    cumulative = route.cumulative
    lo, hi = 0, len(cumulative) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cumulative[mid] <= distance_m:
            lo = mid
        else:
            hi = mid
    if lo < len(segment_speeds):
        value = segment_speeds[lo]
        if value and value > 0.2:
            return float(value)
    return None


def _limit_acceleration(
    ceiling: Sequence[float], step: float, accel: float, decel: float
) -> list[float]:
    """Clamp a speed ceiling to what can actually be accelerated into and out of.

    Two sweeps over v^2 = u^2 + 2*a*s. Forward limits how fast speed may rise,
    backward limits how late braking may start. The result is the pointwise
    minimum, which is the fastest physically reachable profile under the
    ceiling.
    """
    n = len(ceiling)
    out = list(ceiling)

    # Forward: you cannot be going faster than you could have accelerated to.
    for i in range(1, n):
        reachable = math.sqrt(max(0.0, out[i - 1] ** 2 + 2 * accel * step))
        out[i] = min(out[i], reachable)

    # Backward: you cannot be going faster than you can still brake from.
    for i in range(n - 2, -1, -1):
        stoppable = math.sqrt(max(0.0, out[i + 1] ** 2 + 2 * decel * step))
        out[i] = min(out[i], stoppable)

    return out
