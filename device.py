"""Owns the one userspace RSD tunnel and the LocationSimulation channel.

PyTCP's network stack is a process-global singleton inside pymobiledevice3, so
exactly one userspace tunnel may exist per process. That constraint drives the
whole design: a single supervisor task owns the tunnel, and every caller goes
through this object rather than opening its own.

The supervisor is a state machine that re-establishes itself whenever the phone
disappears, the tunnel dies, or a DTX call fails:

    no_device -> connecting -> ready -> (failure) -> no_device
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Optional

from pymobiledevice3 import usbmux
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

log = logging.getLogger("locspoof.device")

DEVICE_POLL_SECONDS = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 30.0

# iOS 17+ is required for the RSD / userspace-tunnel path used here.
MIN_IOS_MAJOR = 17


class LocationSession:
    """Keeps a live LocationSimulation channel to the attached iPhone.

    Public methods never raise when the phone is absent. They record the desired
    state and return; the supervisor applies it as soon as a channel is up. That
    way the UI stays responsive while a cable is unplugged mid-session.
    """

    def __init__(self) -> None:
        self.state: str = "starting"
        self.detail: str = ""
        self.device_name: Optional[str] = None
        self.serial: Optional[str] = None
        self.ios_version: Optional[str] = None

        # The coordinate we want the phone to report. Survives reconnects and is
        # re-applied automatically, so a dropped cable resumes the same spoof.
        self.desired: Optional[tuple[float, float]] = None
        self.applied_at: Optional[float] = None

        self._loc: Optional[LocationSimulation] = None
        self._apply_lock = asyncio.Lock()
        self._channel_failed = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._listeners: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="locspoof-supervisor")

    async def aclose(self) -> None:
        self._stop.set()
        self._channel_failed.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ------------------------------------------------------------- observation

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.debug("status listener raised", exc_info=True)

    def _set_state(self, state: str, detail: str = "") -> None:
        if (state, detail) != (self.state, self.detail):
            log.info("state: %s%s", state, f" ({detail})" if detail else "")
        self.state = state
        self.detail = detail
        self._notify()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "device_name": self.device_name,
            "serial": self.serial,
            "ios_version": self.ios_version,
            "desired": list(self.desired) if self.desired else None,
            "applied_at": self.applied_at,
        }

    # ------------------------------------------------------------ public verbs

    async def set_point(self, lat: float, lon: float) -> dict[str, Any]:
        """Record and apply a coordinate. Safe to call at any time."""
        self.desired = (lat, lon)
        ok, err = await self._apply()
        self._notify()
        return {"ok": ok, "error": err, "applied": ok}

    async def clear(self) -> dict[str, Any]:
        """Stop simulating so the real GPS comes back."""
        self.desired = None
        self.applied_at = None
        async with self._apply_lock:
            loc = self._loc
            if loc is None:
                self._notify()
                return {"ok": True, "applied": False, "error": None}
            try:
                await loc.clear()
            except Exception as exc:
                self._fail(f"clear failed: {exc}")
                return {"ok": False, "applied": False, "error": str(exc)}
        self._notify()
        return {"ok": True, "applied": True, "error": None}

    # --------------------------------------------------------------- internals

    async def _apply(self) -> tuple[bool, Optional[str]]:
        """Push self.desired to the device if a channel is currently open."""
        async with self._apply_lock:
            loc = self._loc
            if loc is None:
                return False, None
            if self.desired is None:
                return False, "no coordinate set"
            lat, lon = self.desired
            try:
                await loc.set(lat, lon)
            except Exception as exc:
                self._fail(f"set failed: {exc}")
                return False, str(exc)
            self.applied_at = time.time()
            return True, None

    def _fail(self, detail: str) -> None:
        """Mark the current channel dead so the supervisor rebuilds it."""
        log.warning("channel failure: %s", detail)
        self._loc = None
        self._set_state("error", detail)
        self._channel_failed.set()

    async def _find_device(self) -> Optional[Any]:
        try:
            devices = await usbmux.list_devices()
        except Exception as exc:
            # usbmuxd ships with Apple Mobile Device Support. Without it there is
            # no USB transport at all, which is the most common Windows failure
            # and deserves its own message rather than a generic error.
            self._set_state(
                "no_usbmux",
                f"cannot reach Apple Mobile Device Service ({exc}). Install Apple Devices or iTunes.",
            )
            return None
        usb = [d for d in devices if d.connection_type == "USB"]
        return usb[0] if usb else None

    async def _supervise(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            device = await self._find_device()
            if device is None:
                if self.state != "no_usbmux":
                    self._set_state("no_device", "plug in your iPhone over USB and unlock it")
                self.serial = None
                self.device_name = None
                await self._sleep(DEVICE_POLL_SECONDS)
                continue

            self.serial = device.serial
            self._set_state("connecting", "bringing up the tunnel")
            try:
                await self._run_session(device.serial)
                backoff = BACKOFF_START  # clean exit means the phone went away
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state("error", str(exc))
                log.warning("session ended: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _run_session(self, serial: str) -> None:
        """One full tunnel lifetime. Returns when the phone or channel goes away."""
        self._channel_failed.clear()
        tunnel = UserspaceRsdTunnel(serial=serial, autopair=True)
        rsd = await tunnel.aopen()
        try:
            self.ios_version = getattr(rsd, "product_version", None)
            self.device_name = getattr(rsd, "name", None) or serial
            if not self._ios_supported(self.ios_version):
                self._set_state("unsupported", f"iOS {self.ios_version} is below {MIN_IOS_MAJOR}.0")
                await self._sleep(30)
                return

            async with DvtProvider(rsd) as dvt:
                async with LocationSimulation(dvt) as loc:
                    self._loc = loc
                    self._set_state("ready", "")
                    # Re-apply whatever was active before the drop.
                    if self.desired is not None:
                        await self._apply()
                    await self._hold(serial)
        finally:
            self._loc = None
            with contextlib.suppress(Exception):
                await tunnel.aclose()

    @staticmethod
    def _ios_supported(version: Optional[str]) -> bool:
        if not version:
            return True  # unknown, let the attempt proceed rather than block it
        try:
            return int(str(version).split(".")[0]) >= MIN_IOS_MAJOR
        except (ValueError, IndexError):
            return True

    async def _hold(self, serial: str) -> None:
        """Stay in 'ready' until the phone unplugs or a DTX call fails."""
        while not self._stop.is_set():
            failed = asyncio.create_task(self._channel_failed.wait())
            timer = asyncio.create_task(asyncio.sleep(DEVICE_POLL_SECONDS))
            done, pending = await asyncio.wait({failed, timer}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if failed in done:
                return
            devices: list[Any] = []
            with contextlib.suppress(Exception):
                devices = await usbmux.list_devices()
            if not any(d.serial == serial and d.connection_type == "USB" for d in devices):
                self._set_state("no_device", "iPhone disconnected")
                return

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
