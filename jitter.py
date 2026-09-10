"""GPS wander, so a reported position is never perfectly still or perfectly exact.

Two things give a simulated location away even when the coordinates are
plausible. A stationary phone that reports byte-identical coordinates for
thirty seconds, and a moving one that sits exactly on the road centreline.
Real receivers do neither: the fix wanders by a few metres, and it wanders
*smoothly*, because consecutive fixes share most of their error sources.

That last point is why this is not `random.gauss()` per tick. Independent noise
looks like television static and is trivially distinguishable from a real trace.
This uses an Ornstein-Uhlenbeck process, which is the standard model for
correlated, mean-reverting drift:

    dx = -(x / tau) dt + sigma * sqrt(2 dt / tau) dW

It wanders like a real fix, and `tau` sets how long it takes to forget where it
was. Being mean-reverting, it never walks off to infinity the way a plain random
walk would, so the position stays honest over a long session.

Accuracy also depends on motion. A receiver averaging over a moving baseline
reports a tighter fix than one sitting still fighting multipath off buildings,
so the wander shrinks as speed rises.
"""
from __future__ import annotations

import math
import random
from typing import Optional

Point = tuple[float, float]

# Metres per degree of latitude. Longitude is this scaled by cos(latitude),
# which matters: at 33 degrees north a degree of longitude is ~16% shorter.
M_PER_DEG_LAT = 111_320.0

# Defaults chosen to sit in the range a phone actually reports. Consumer GNSS
# with assistance lands around 3-5 m of horizontal error in the open, worse
# stationary among buildings, better moving with a clear sky.
STILL_SIGMA_M = 3.0
MOVING_SIGMA_M = 1.5
# Speed at which wander has fully tightened to MOVING_SIGMA_M. Roughly walking
# pace: once you are clearly moving, the fix steadies.
TIGHTEN_SPEED_MPS = 1.5
# Correlation time. Around ten seconds looks right: long enough that successive
# fixes are visibly related, short enough that a minute-long stop still wanders.
TAU_S = 9.0


class GpsJitter:
    """Correlated positional error, in metres east and north of the true point."""

    def __init__(
        self,
        still_sigma_m: float = STILL_SIGMA_M,
        moving_sigma_m: float = MOVING_SIGMA_M,
        tau_s: float = TAU_S,
        seed: Optional[int] = None,
    ) -> None:
        self.still_sigma_m = still_sigma_m
        self.moving_sigma_m = moving_sigma_m
        self.tau_s = max(0.5, tau_s)
        self._rng = random.Random(seed)
        # Start already displaced, drawn from the process's own equilibrium
        # spread. Starting at exactly zero would make the very first fix
        # perfectly accurate, which no real receiver ever is.
        self.east_m = self._rng.gauss(0.0, still_sigma_m)
        self.north_m = self._rng.gauss(0.0, still_sigma_m)

    def sigma_for_speed(self, speed_mps: float) -> float:
        """Wander amplitude at a given ground speed."""
        if speed_mps <= 0.0:
            return self.still_sigma_m
        blend = min(1.0, speed_mps / TIGHTEN_SPEED_MPS)
        return self.still_sigma_m + (self.moving_sigma_m - self.still_sigma_m) * blend

    def advance(self, dt_s: float, speed_mps: float = 0.0) -> tuple[float, float]:
        """Step the process forward and return the new (east, north) offset."""
        if dt_s <= 0.0:
            return self.east_m, self.north_m
        # Cap the step so a long pause between ticks cannot produce one huge
        # jump; beyond about tau the process has forgotten its history anyway.
        dt = min(dt_s, self.tau_s)
        sigma = self.sigma_for_speed(speed_mps)
        decay = dt / self.tau_s
        kick = sigma * math.sqrt(2.0 * decay)
        self.east_m += -self.east_m * decay + kick * self._rng.gauss(0.0, 1.0)
        self.north_m += -self.north_m * decay + kick * self._rng.gauss(0.0, 1.0)
        return self.east_m, self.north_m

    def apply(self, point: Point, dt_s: float, speed_mps: float = 0.0) -> Point:
        """Advance the wander and offset `point` by it."""
        east, north = self.advance(dt_s, speed_mps)
        lat, lon = point
        # Guard the pole case, where cos(lat) collapses and longitude explodes.
        cos_lat = max(0.01, math.cos(math.radians(lat)))
        return (
            lat + north / M_PER_DEG_LAT,
            lon + east / (M_PER_DEG_LAT * cos_lat),
        )

    def reset(self) -> None:
        self.east_m = 0.0
        self.north_m = 0.0

    @property
    def offset_m(self) -> float:
        """Current distance from the true position, in metres."""
        return math.hypot(self.east_m, self.north_m)
