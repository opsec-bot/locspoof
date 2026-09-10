"""Wi-Fi transport: reach the phone over the network instead of the cable.

iOS 17.4+ exposes CoreDeviceProxy over lockdown, so pymobiledevice3's no-root
helper always bootstraps the tunnel over USB and only falls back to RemotePairing
over Bonjour for older devices. That is a policy in the helper, not a limit of
the device: a modern iPhone advertises `_remotepairing._tcp` on the LAN and will
happily serve the same tunnel over Wi-Fi.

Getting there needs one thing first. Bonjour discovery only returns devices that
already hold a **RemotePairing pair record** on this machine, because
`get_remote_pairing_tunnel_services` filters its results through
`iter_remote_paired_identifiers()`. That record is created once, over the cable,
and the handshake is promptless because it runs on the already-trusted lockdownd
transport:

    python -m pymobiledevice3 lockdown remotepairing --pair

After that the cable is optional. `python -m pymobiledevice3 remote browse` will
list the phone under `wifi` with an address and port.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator, Optional

from pymobiledevice3.remote import tunnel_service, userspace_tunnel

log = logging.getLogger("locspoof.wireless")

DISCOVERY_TIMEOUT = 3.0

# The port iOS advertises its RemotePairing tunnel service on. Stable in
# practice, but `pymobiledevice3 remote browse` prints the real one.
DEFAULT_REMOTEPAIRING_PORT = 49152


async def discover(udid: Optional[str] = None, timeout: float = DISCOVERY_TIMEOUT) -> list[Any]:
    """Find RemotePairing tunnel services for this device on the local network.

    The returned services are already connected, so anything not used must be
    closed. Bonjour answers on every interface it can, which means the same
    phone comes back several times over IPv4 and IPv6; results are deduplicated
    by identifier so callers see one entry per device.
    """
    try:
        services = await tunnel_service.get_remote_pairing_tunnel_services(
            bonjour_timeout=timeout, udid=udid
        )
    except Exception as exc:
        log.debug("wireless discovery failed: %s", exc)
        return []

    unique: list[Any] = []
    seen: set[str] = set()
    for service in services:
        identifier = getattr(service, "remote_identifier", None) or ""
        if identifier in seen:
            # A duplicate route to a phone we already have. Close it rather than
            # leaking the connection Bonjour discovery already opened.
            with contextlib.suppress(Exception):
                await service.close()
            continue
        seen.add(identifier)
        unique.append(service)

    if unique:
        log.info(
            "found %d device(s) over Wi-Fi: %s",
            len(unique),
            ", ".join(f"{getattr(s, 'hostname', '?')}:{getattr(s, 'port', '?')}" for s in unique),
        )
    return unique


def paired_identifiers() -> list[str]:
    """UDIDs that already hold a RemotePairing record on this machine."""
    try:
        from pymobiledevice3 import pair_records

        return list(pair_records.iter_remote_paired_identifiers())
    except Exception as exc:
        log.debug("could not read pair records: %s", exc)
        return []


async def connect_direct(
    hostname: str, port: int = DEFAULT_REMOTEPAIRING_PORT, identifier: Optional[str] = None
) -> Optional[Any]:
    """Connect to a known address instead of discovering one.

    Bonjour is multicast, so it dies at the first router: the phone has to be on
    the same LAN segment. Given an address it can be reached anywhere it is
    routable, including across a VPN such as Tailscale or WireGuard, which makes
    the phone controllable from outside the house without any of this changing.

    The identifier still has to match a local pair record, since that record
    holds the keys the handshake needs. With exactly one paired device it is
    inferred, so nobody has to type a UDID.
    """
    if identifier is None:
        known = paired_identifiers()
        if len(known) != 1:
            log.warning(
                "cannot infer which device to reach: %d pair records found. Pass an identifier.",
                len(known),
            )
            return None
        identifier = known[0]

    try:
        service = await tunnel_service.create_core_device_tunnel_service_using_remotepairing(
            identifier, hostname, port
        )
    except Exception as exc:
        log.debug("direct connect to %s:%s failed: %s", hostname, port, exc)
        return None
    log.info("connected directly to %s:%s", hostname, port)
    return service


@contextlib.asynccontextmanager
async def provider_override(service: Any) -> AsyncIterator[None]:
    """Make UserspaceRsdTunnel use an already-discovered Wi-Fi service.

    `UserspaceRsdTunnel._aopen_locked` is generic apart from a single call to the
    module-level `_create_no_root_tunnel_provider`, which hard-codes the USB
    bootstrap. Everything after it (`start_tcp_tunnel`, the PyTCP tun, the dial
    plane, the RSD handshake) is transport-agnostic and works unchanged for a
    RemotePairing service.

    Swapping that one function for the duration of `aopen()` reuses the library's
    whole lifecycle, including the process-global single-tunnel guard and the
    AsyncExitStack teardown. Reimplementing `_aopen_locked` here instead would
    have to reach into those same private globals to stay correct, which is the
    more fragile of the two options.

    The override is only needed while the tunnel opens; once the provider is on
    the exit stack the original is restored.
    """
    original = userspace_tunnel._create_no_root_tunnel_provider

    async def _wireless_provider(
        serial: Optional[str], autopair: bool, remotepairing_fallback: bool = True
    ) -> tuple[Any, None]:
        # No lockdown object to keep alive: the Wi-Fi service is its own transport.
        return service, None

    userspace_tunnel._create_no_root_tunnel_provider = _wireless_provider
    try:
        yield
    finally:
        userspace_tunnel._create_no_root_tunnel_provider = original
