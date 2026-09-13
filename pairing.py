"""Pairing a phone with a machine that has no USB port.

`wireless.py` reaches a phone that already holds a RemotePairing record. This
module is about getting that record in the first place, which is the one step a
cloud VM cannot do the normal way: the documented flow is
`pymobiledevice3 lockdown remotepairing --pair` over a cable, and a droplet has
no cable.

There are two ways out, and the hub offers both because only one of them is
certain to work.

**Pair over the network.** pymobiledevice3 already implements it:
`RemotePairingManualPairingService` opens a plain TCP connection to the phone's
RemotePairing port and runs the same SRP handshake. For an iPhone no PIN is
involved -- `_request_pair_consent` makes the phone raise a Trust / Don't Trust
dialog naming the host, and the SRP password is the fixed "000000" (the PIN
branch is tvOS only). Tap Trust and the record is written locally. Nothing is
plugged in and nothing is uploaded.

That path needs the phone to be serving RemotePairing on an interface the VM can
reach, which is the part that is not guaranteed. Measured against an iPhone 15
Pro on iOS 26.5.2 over Tailscale while the phone was on cellular, a full sweep
of the Darwin ephemeral range (49152-65535) found only two listeners: Tailscale's
own peerapi, and one port that accepted TCP but never answered the handshake. So
on cellular the service appears not to be bound to the tunnel interface. On the
home Wi-Fi it may well be; `find_service_port` is how you check, since Bonjour
is multicast and cannot answer across a tailnet.

**Import a record made elsewhere.** The fallback. The record is three keys in a
plist and is not tied to the host that made it, so pairing once on a machine
with a cable and handing the file to the hub works permanently. `import_record`
validates and installs it.

A note on the UDID, which is easy to get wrong: `RemotePairingTunnelService`
overrides `remote_identifier` to return the constructor argument rather than the
value the handshake reports, and `pair_record_path` is built from it. Construct
one with an empty identifier and the record saves as `remote_.plist`, which no
later lookup will ever find. `_IdentifiedPairingService` below prefers the
handshake value so a first contact with an unknown phone still saves correctly.
"""
from __future__ import annotations

import asyncio
import logging
import plistlib
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from pymobiledevice3.exceptions import RemotePairingCompletedError
from pymobiledevice3.remote.tunnel_service import RemotePairingManualPairingService

log = logging.getLogger("locspoof.pairing")

# Darwin hands listeners ports from this range, and remoted takes whatever it is
# given, so the port changes when the phone reboots. There is no way to ask for
# it remotely: the phone announces it over Bonjour, which stops at the router.
EPHEMERAL_LO = 49152
EPHEMERAL_HI = 65535

# Tried before the full sweep. 49152 is the bottom of the range and so the most
# common value in practice; the rest are ports observed in the wild.
LIKELY_PORTS = (49152, 49153, 49154, 50000, 58783, 60000, 62078)

# Aggressive scanning over a WireGuard tunnel drops packets and turns real
# refusals into timeouts, which reads as a false negative. These values were
# picked to stay under that: measured clean over Tailscale to a phone on LTE.
SCAN_CONCURRENCY = 100
SCAN_TIMEOUT = 2.0
CONNECT_TIMEOUT = 10.0
HANDSHAKE_TIMEOUT = 25.0
# Long, because a human has to notice the dialog and tap it.
CONSENT_TIMEOUT = 120.0

PairProgress = Callable[[str], None]


class ProbeResult(NamedTuple):
    """What a phone tells us about itself before any pairing happens."""

    udid: str
    model: str
    name: str
    paired: bool  # an existing record on this machine already validates


class PairingUnreachable(RuntimeError):
    """Nothing on the far end spoke RemotePairing."""


class _IdentifiedPairingService(RemotePairingManualPairingService):
    """Manual pairing that knows its own UDID after the handshake.

    The base class answers `remote_identifier` from the constructor argument,
    which is fine once you know the UDID and wrong on first contact, when the
    only source of it is the handshake the connection just completed. Preferring
    the handshake keeps `pair_record_path` correct in both cases.
    """

    @property
    def remote_identifier(self) -> str:
        if self._remote_identifier:
            return self._remote_identifier
        info = getattr(self, "handshake_info", None) or {}
        identifier = (info.get("peerDeviceInfo") or {}).get("identifier")
        if not identifier:
            raise ValueError("no identifier: handshake has not completed")
        return identifier


def _peer_info(service: Any) -> dict[str, Any]:
    info = getattr(service, "handshake_info", None) or {}
    return info.get("peerDeviceInfo") or {}


async def _close(service: Any) -> None:
    try:
        await service.close()
    except Exception:
        log.debug("close failed", exc_info=True)


async def probe(
    host: str, port: int, udid: str = "", connect_timeout: float = CONNECT_TIMEOUT
) -> ProbeResult:
    """Ask a phone who it is, without triggering any prompt.

    `connect(autopair=False)` performs the pair-verify attempt and the handshake
    and then stops, so this is safe to call against a phone that has never been
    paired: it shows the user nothing.
    """
    # Reachability is checked separately from the handshake so the two failures
    # can be told apart. RemotePairingManualPairingService.connect wraps its own
    # TCP connect in wait_for, so without this an unreachable port and a port
    # that simply is not remoted both arrive as the same TimeoutError.
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=connect_timeout
        )
        writer.close()
    except asyncio.TimeoutError as exc:
        raise PairingUnreachable(
            f"nothing answered at {host}:{port} (connection timed out)"
        ) from exc
    except (ConnectionRefusedError, OSError) as exc:
        raise PairingUnreachable(f"nothing is listening on {host}:{port}: {exc}") from exc

    service = _IdentifiedPairingService(udid, host, port)
    try:
        try:
            await asyncio.wait_for(service.connect(autopair=False), timeout=HANDSHAKE_TIMEOUT)
            paired = True
        except RemotePairingCompletedError:
            # Cannot happen with autopair=False, but treat it as success anyway.
            paired = True
        except asyncio.TimeoutError as exc:
            raise PairingUnreachable(
                f"{host}:{port} accepted a connection but never answered the "
                "RemotePairing handshake, so it is not the pairing service"
            ) from exc
        except (ConnectionRefusedError, OSError) as exc:
            raise PairingUnreachable(f"{host}:{port} is not reachable: {exc}") from exc
        except Exception:
            # Reached the service and it declined to validate: unpaired, which
            # is exactly the state pairing is for. The handshake still ran, so
            # the peer information below is populated.
            paired = False

        # peerDeviceInfo is not always present. Measured against an iPhone 15 Pro
        # on iOS 26.5.2, a successful handshake carried only the wire protocol
        # versions and deviceOptions, and no peer block at all -- which is
        # exactly why the library overrides `remote_identifier` to return the
        # caller's UDID rather than reading it back. So treat it as a bonus: if
        # the phone volunteers an identifier, use it; otherwise the UDID the
        # caller already supplied stands.
        peer = _peer_info(service)
        identifier = peer.get("identifier") or udid
        if not identifier:
            raise PairingUnreachable(
                f"{host}:{port} completed a handshake but did not report which "
                "device it is, so the UDID has to be supplied by the caller"
            )
        return ProbeResult(
            udid=identifier,
            model=peer.get("model") or "",
            name=peer.get("name") or "",
            paired=paired,
        )
    finally:
        await _close(service)


async def pair(host: str, port: int, udid: str = "", progress: Optional[PairProgress] = None) -> str:
    """Pair over the network. Returns the UDID once the record is written.

    Blocks while the phone shows its Trust dialog. On success pymobiledevice3
    saves the record and raises `RemotePairingCompletedError`, because the phone
    closes the connection as soon as pairing completes; that exception is the
    success signal, not a failure.
    """

    def say(message: str) -> None:
        log.info("%s", message)
        if progress is not None:
            progress(message)

    # Always probe first, even when the caller supplied a UDID. It costs one
    # round trip and it is the difference between "the phone is not reachable"
    # and "you did not tap Trust": without it, the TCP connect timeout inside
    # RemotePairingManualPairingService.connect surfaces as the consent timeout
    # below and blames the user for a network problem.
    say("asking the phone to identify itself")
    found = await probe(host, port, udid)
    if not udid:
        udid = found.udid
    elif found.udid != udid:
        raise PairingUnreachable(
            f"{host}:{port} is {found.udid}, not the {udid} that was expected"
        )
    if found.paired:
        say("this phone is already paired with this machine")
        return udid

    service = _IdentifiedPairingService(udid, host, port)
    say("waiting for you to tap Trust on the phone")
    try:
        await asyncio.wait_for(service.connect(autopair=True), timeout=CONSENT_TIMEOUT)
    except RemotePairingCompletedError:
        say("paired")
    except asyncio.TimeoutError as exc:
        raise PairingUnreachable(
            "the phone never answered the Trust prompt. Unlock it, keep it awake, "
            "and try again."
        ) from exc
    finally:
        await _close(service)

    if not record_path(udid).exists():
        raise PairingUnreachable(
            "pairing reported success but no record was written; the phone may "
            "have rejected the request"
        )
    return udid


async def find_service_port(
    host: str,
    progress: Optional[PairProgress] = None,
    lo: int = EPHEMERAL_LO,
    hi: int = EPHEMERAL_HI,
) -> Optional[int]:
    """Find the phone's RemotePairing port by scanning, or None.

    Bonjour is how this is meant to be discovered and it cannot cross a tailnet,
    so there is nothing to do but knock on doors. Likely ports go first so the
    common case returns in a second rather than five minutes; only then does the
    full sweep run.

    Ports that merely accept TCP are rejected. The phone runs other listeners --
    Tailscale's peerapi among them -- and accepting a connection proves nothing,
    so each candidate has to complete a RemotePairing handshake to count.
    """

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    async def speaks_remotepairing(port: int, connect_timeout: float = CONNECT_TIMEOUT) -> bool:
        try:
            await probe(host, port, connect_timeout=connect_timeout)
            return True
        except Exception:
            return False

    say("trying the usual ports")
    for port in LIKELY_PORTS:
        if lo <= port <= hi and await speaks_remotepairing(port, SCAN_TIMEOUT):
            say(f"found the pairing service on port {port}")
            return port

    open_ports: list[int] = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def knock(port: int) -> None:
        async with sem:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=SCAN_TIMEOUT
                )
            except Exception:
                return
            open_ports.append(port)
            writer.close()

    started = time.monotonic()
    ports = [p for p in range(lo, hi + 1) if p not in LIKELY_PORTS]
    # Chunked so progress is reportable and the tunnel is not hammered flat.
    for index in range(0, len(ports), 1000):
        chunk = ports[index : index + 1000]
        await asyncio.gather(*(knock(p) for p in chunk))
        say(
            f"scanned {chunk[0]}-{chunk[-1]} of {hi} "
            f"({len(open_ports)} listening, {time.monotonic() - started:.0f}s)"
        )

    for port in sorted(open_ports):
        say(f"checking port {port}")
        if await speaks_remotepairing(port):
            say(f"found the pairing service on port {port}")
            return port

    say("no RemotePairing service answered on any port")
    return None


# ------------------------------------------------------------------ records


def records_dir() -> Path:
    """Where pymobiledevice3 keeps pair records for the current OS user."""
    from pymobiledevice3.common import get_home_folder

    return Path(get_home_folder())


def record_path(udid: str) -> Path:
    return records_dir() / f"remote_{udid}.plist"


def paired_udids() -> list[str]:
    """UDIDs holding a usable RemotePairing record on this machine."""
    try:
        from pymobiledevice3 import pair_records

        return sorted(pair_records.iter_remote_paired_identifiers())
    except Exception:
        log.debug("could not enumerate pair records", exc_info=True)
        return []


# The three keys pymobiledevice3 writes in `save_pair_record`. Checked on import
# so a truncated or unrelated plist is refused before it shadows a good record.
RECORD_KEYS = ("public_key", "private_key", "remote_unlock_host_key")


def import_record(udid: str, data: bytes) -> Path:
    """Install a pair record produced on another machine.

    The record is a keypair and nothing in it names the host that created it, so
    a record made over USB on a laptop works unchanged on the VM. Refuses to
    overwrite an existing record: two hosts presenting the same identity to one
    phone is a situation worth not creating by accident.
    """
    try:
        parsed = plistlib.loads(data)
    except Exception as exc:
        raise ValueError(f"not a plist: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("pair record should be a plist dictionary")
    missing = [k for k in RECORD_KEYS if k not in parsed]
    if missing:
        raise ValueError(f"pair record is missing {', '.join(missing)}")

    target = record_path(udid)
    if target.exists():
        raise ValueError(f"a pair record for {udid} is already installed")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    log.info("imported pair record for %s", udid)
    return target
