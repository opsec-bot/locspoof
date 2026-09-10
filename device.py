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

import wireless
from jitter import GpsJitter
from pymobiledevice3 import usbmux
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

log = logging.getLogger("locspoof.device")

DEVICE_POLL_SECONDS = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 30.0

# How often a stationary pin is re-pushed with fresh wander. Roughly the rate a
# real handset produces fixes, and slow enough to be negligible traffic.
HOLD_INTERVAL_SECONDS = 1.0

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
        # "usb" or "wifi". USB is preferred whenever the cable is present: it is
        # faster to establish and cannot be disrupted by the network.
        self.transport: Optional[str] = None

        # The coordinate we want the phone to report. Survives reconnects and is
        # re-applied automatically, so a dropped cable resumes the same spoof.
        self.desired: Optional[tuple[float, float]] = None
        self.applied_at: Optional[float] = None

        # A pinned location that reports byte-identical coordinates forever is
        # the loudest tell there is, so the hold loop keeps it wandering the way
        # a real stationary receiver does. Suppressed while a route drives the
        # position, since the player applies its own wander.
        # Wi-Fi fallback. Works only for a device already holding a RemotePairing
        # record, created once over the cable; see wireless.py.
        self.wireless_enabled: bool = True
        self.prefer_serial: Optional[str] = None
        # Explicit (host, port) to reach the phone at, skipping Bonjour. Bonjour
        # is multicast and stops at the first router; an address works anywhere
        # the phone is routable, including over a VPN.
        self.wireless_address: Optional[tuple[str, int]] = None
        self._usbmux_error: Optional[str] = None

        self.pin_jitter_enabled: bool = True
        # Returns True when the hold loop may drive the position. Wired to the
        # route player, so a route finishing on its own hands control back
        # without anyone having to remember to clear a flag.
        self.hold_gate: Optional[Callable[[], bool]] = None
        self._pin_jitter = GpsJitter()
        self._hold_task: Optional[asyncio.Task[None]] = None

        self._loc: Optional[LocationSimulation] = None
        self._apply_lock = asyncio.Lock()
        self._channel_failed = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._listeners: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="locspoof-supervisor")
        self._hold_task = asyncio.create_task(self._hold_pin(), name="locspoof-hold")

    async def aclose(self) -> None:
        self._stop.set()
        self._channel_failed.set()
        for task in (self._hold_task, self._task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
            "transport": self.transport,
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

    async def _hold_pin(self) -> None:
        """Keep a stationary pin alive by re-pushing it with fresh wander.

        Only runs when nothing else is driving the position. The wander is
        applied to a copy: `self.desired` stays the true point, so the drift
        never accumulates into it.
        """
        while not self._stop.is_set():
            await self._sleep(HOLD_INTERVAL_SECONDS)
            if not self.pin_jitter_enabled:
                continue
            if self.hold_gate is not None and not self.hold_gate():
                continue
            if self.state != "ready" or self.desired is None:
                continue
            lat, lon = self._pin_jitter.apply(self.desired, HOLD_INTERVAL_SECONDS, 0.0)
            async with self._apply_lock:
                loc = self._loc
                if loc is None:
                    continue
                try:
                    await loc.set(lat, lon)
                except Exception as exc:
                    self._fail(f"hold failed: {exc}")

    def _fail(self, detail: str) -> None:
        """Mark the current channel dead so the supervisor rebuilds it."""
        log.warning("channel failure: %s", detail)
        self._loc = None
        self._set_state("error", detail)
        self._channel_failed.set()

    async def _find_usb(self) -> Optional[str]:
        """Serial of the first phone on the cable, if any."""
        try:
            devices = await usbmux.list_devices()
        except Exception as exc:
            # usbmuxd ships with Apple Mobile Device Support. Without it there is
            # no USB transport at all, which is the most common Windows failure.
            # Not fatal any more: a device already paired for RemotePairing can
            # still be reached over Wi-Fi.
            self._usbmux_error = str(exc)
            return None
        self._usbmux_error = None
        usb = [d for d in devices if d.connection_type == "USB"]
        return usb[0].serial if usb else None

    async def _find_target(self) -> Optional[tuple[str, str, Any]]:
        """Locate the phone as (transport, serial, wifi_service).

        USB wins whenever the cable is present: it establishes faster and cannot
        be knocked over by the network. Wi-Fi is the fallback, and only finds
        devices that already hold a RemotePairing record from a one-time
        `pymobiledevice3 lockdown remotepairing --pair` over the cable.
        """
        serial = await self._find_usb()
        if serial is not None:
            return ("usb", serial, None)

        if not self.wireless_enabled:
            return None

        # An explicit address beats discovery: it is faster, and it is the only
        # option once the phone is off this LAN.
        if self.wireless_address is not None:
            host, port = self.wireless_address
            service = await wireless.connect_direct(host, port, identifier=self.prefer_serial)
            if service is not None:
                return ("wifi", getattr(service, "remote_identifier", "") or "", service)
            return None

        services = await wireless.discover(udid=self.prefer_serial)
        if not services:
            return None
        service = services[0]
        # Anything beyond the first is a duplicate device we will not use.
        for extra in services[1:]:
            with contextlib.suppress(Exception):
                await extra.close()
        return ("wifi", getattr(service, "remote_identifier", "") or "", service)

    async def _supervise(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            target = await self._find_target()
            if target is None:
                if self._usbmux_error and not self.wireless_enabled:
                    self._set_state(
                        "no_usbmux",
                        f"cannot reach Apple Mobile Device Service ({self._usbmux_error}). "
                        "Install Apple Devices or iTunes.",
                    )
                else:
                    self._set_state(
                        "no_device",
                        "connect your iPhone by USB, or join it to this network once paired",
                    )
                self.serial = None
                self.device_name = None
                self.transport = None
                await self._sleep(DEVICE_POLL_SECONDS)
                continue

            transport, serial, service = target
            self.serial = serial
            self.transport = transport
            self._set_state(
                "connecting",
                "bringing up the tunnel" if transport == "usb" else "bringing up the Wi-Fi tunnel",
            )
            try:
                await self._run_session(serial, transport, service)
                backoff = BACKOFF_START  # clean exit means the phone went away
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state("error", str(exc))
                log.warning("session ended: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _run_session(
        self, serial: str, transport: str = "usb", service: Any = None
    ) -> None:
        """One full tunnel lifetime. Returns when the phone or channel goes away."""
        self._channel_failed.clear()
        tunnel = UserspaceRsdTunnel(serial=serial, autopair=True)
        if transport == "wifi":
            # The override only has to be in place while the tunnel opens; the
            # provider lands on the tunnel's exit stack and outlives it.
            async with wireless.provider_override(service):
                rsd = await tunnel.aopen()
        else:
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
                    await self._hold(serial, transport)
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

    async def _hold(self, serial: str, transport: str = "usb") -> None:
        """Stay in 'ready' until the phone goes away or a DTX call fails.

        On Wi-Fi there is no usbmux presence to poll, and polling it would see an
        empty list and immediately conclude the phone had vanished. The tunnel's
        own transport watcher already tears the session down when the connection
        dies, which surfaces here as a channel failure, so waiting on that alone
        is both sufficient and correct.
        """
        if transport == "wifi":
            await self._channel_failed.wait()
            return

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
