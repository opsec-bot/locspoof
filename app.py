"""locspoof: local iPhone location simulator.

Runs one process holding one userspace RSD tunnel, a small HTTP API, and a
Leaflet map UI. Start it, open the page, click the map.

    python app.py [--port 8765] [--no-browser] [-v]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import threading
import webbrowser

from aiohttp import web

from device import LocationSession
from route import RoutePlayer
from server import build_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local iPhone location simulator")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port to serve on")
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

    url = f"http://{HOST}:{args.port}/"

    async def on_startup(_app: web.Application) -> None:
        session.start()
        print(f"\n  locspoof running at {url}")
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

    web.run_app(app, host=HOST, port=args.port, print=None, handle_signals=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
