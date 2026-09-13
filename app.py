"""locspoof: local iPhone location simulator.

Runs one process holding one userspace RSD tunnel, a small HTTP API, and a
Leaflet map UI. Start it, open the page, click the map.

    python app.py [--host ADDR] [--port 8765] [--no-browser] [-v]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import ipaddress
import socket
import logging
import sys
import threading
import webbrowser

from aiohttp import web

from device import LocationSession
from route import RoutePlayer
from server import build_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local iPhone location simulator")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        metavar="ADDR",
        help="address to serve the UI on. Loopback by default. Give it a VPN "
        "address to open the map from the phone itself; the API has no auth, "
        "so whatever can reach it can move your phone.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--device-address",
        metavar="HOST[:PORT]",
        help="reach the phone at this address instead of discovering it. Bonjour "
        "stops at the first router, so this is what you need over a VPN.",
    )
    parser.add_argument(
        "--device-udid",
        help="which paired device to reach. Inferred when only one is paired.",
    )
    parser.add_argument("--no-wireless", action="store_true", help="USB only")
    return parser.parse_args()


def check_host(value: str) -> str:
    """Reject binds that would put the unauthenticated API on every interface.

    `server.py` has no login because loopback meant no remote surface. Serving
    it wider is a deliberate act, and there is a large difference between one
    routable address and all of them: a laptop on cafe Wi-Fi that binds the
    wildcard is handing the room a button that moves its owner's phone. An
    explicit address keeps the exposure to the network you meant.
    """
    bare = value.strip().strip("[]")
    unspecified = bare in ("", "*")  # aiohttp reads both as every interface
    if not unspecified:
        try:
            unspecified = ipaddress.ip_address(bare).is_unspecified  # 0.0.0.0, ::
        except ValueError:
            return bare  # a hostname; leave resolution to the stack
    if unspecified:
        raise SystemExit(
            f"  --host {value} would serve the map on every interface, and there\n"
            "  is no password on it: anything that can reach the port can move\n"
            "  your phone. Pass the one address you want instead, such as this\n"
            "  machine's Tailscale IP (100.x.y.z)."
        )
    return bare


def is_local(host: str) -> bool:
    """True when only this machine can reach the bind address."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host in ("localhost", "localhost.localdomain")


def url_for(host: str, port: int) -> str:
    """Browser URL for a bind address, bracketing IPv6 literals."""
    bare = host.strip("[]")
    try:
        if isinstance(ipaddress.ip_address(bare), ipaddress.IPv6Address):
            return f"http://[{bare}]:{port}/"
    except ValueError:
        pass
    return f"http://{bare}:{port}/"


def local_addresses() -> list[str]:
    """Addresses this machine answers on, to name in a failed-bind message."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return found


def bind_error(host: str, port: int, exc: OSError) -> str:
    """Explain a bind failure, which asyncio reports only as an address list.

    Mixing up the two address flags is the easy mistake: `--host` is this
    computer, `--device-address` is the phone, and both are 100.x on a tailnet,
    so they look interchangeable and are not.
    """
    # asyncio reports a missing address as a bare "could not bind on any
    # address out of [...]" with no errno, so None must not match the set.
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
    if exc.errno is not None and exc.errno in in_use:
        return (
            f"  port {port} is already in use. Pass --port with another number,\n"
            "  or stop the copy of locspoof that already has it."
        )
    lines = [
        f"  cannot serve on {host}: this machine has no such address.",
        "",
        "  --host is THIS computer's address, the one the phone dials.",
        "  --device-address is the phone's. On a tailnet both start 100.,",
        "  so check you have not swapped them.",
    ]
    mine = local_addresses()
    if mine:
        lines += ["", "  addresses this machine answers on:"]
        lines += [f"    {a}" for a in mine]
    lines += ["", "  `tailscale ip -4` prints the tailnet one."]
    return "\n".join(lines)


def parse_address(value: str) -> tuple[str, int]:
    """Split HOST[:PORT], tolerating bracketed IPv6 such as [fd89::1]:49152."""
    from wireless import DEFAULT_REMOTEPAIRING_PORT

    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port else DEFAULT_REMOTEPAIRING_PORT
    # A bare IPv6 literal has several colons; only treat the last as a port
    # separator when there is exactly one.
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, int(port)
    return value, DEFAULT_REMOTEPAIRING_PORT


def main() -> int:
    args = parse_args()
    host = check_host(args.host)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # pymobiledevice3 is chatty at INFO and drowns out our own status lines.
    logging.getLogger("pymobiledevice3").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING
    )

    session = LocationSession()
    session.wireless_enabled = not args.no_wireless
    session.prefer_serial = args.device_udid
    if args.device_address:
        session.wireless_address = parse_address(args.device_address)
    player = RoutePlayer(setter=session.set_point)
    # The pin hold and the route player both drive position, so only one may
    # run at a time. Gating on the player rather than a flag means a route
    # finishing on its own hands control back automatically.
    session.hold_gate = lambda: not player.running
    app = build_app(session, player)

    url = url_for(host, args.port)
    local_only = is_local(host)

    async def on_startup(_app: web.Application) -> None:
        session.start()
        print(f"\n  locspoof running at {url}")
        if not local_only:
            print("  open that on the phone to drive the map from anywhere it can")
            print("  reach this address. Nothing asks for a password, so keep the")
            print("  address on a private network.")
        print("  press Ctrl+C to stop and restore the real GPS\n")
        if not args.no_browser:
            # Opening a browser can block on Windows, so keep it off the loop.
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    async def on_cleanup(_app: web.Application) -> None:
        print("\n  stopping route playback and clearing the simulated location...")
        await player.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.clear(), timeout=5.0)
        await session.aclose()
        print("  done. the phone is back on its real GPS.")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    try:
        web.run_app(app, host=host, port=args.port, print=None, handle_signals=True)
    except OSError as exc:
        raise SystemExit(bind_error(host, args.port, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
