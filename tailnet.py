"""Tailscale as the directory and the login.

The hub needs two facts it cannot get from anywhere else: which phones exist,
and who is asking. Tailscale already knows both, so nothing here invents a
device list or a password.

`tailscale status --json` names every machine in the tailnet with its OS, so the
iPhones fall out of a filter. `tailscale whois` maps the source address of an
inbound HTTP request back to the tailnet user that owns that machine, which is
an identity the requester cannot forge: it comes from the WireGuard peer key the
packets actually arrived under, not from a header. That is the whole auth story
for `hub.py`, and it is stronger than a shared password would be.

Both are read-only CLI calls. On Linux the local API socket is root-owned by
default, so a hub running as a normal user needs one grant first:

    sudo tailscale set --operator=$USER

Without it `whois` fails closed and the hub serves nobody.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, NamedTuple, Optional

log = logging.getLogger("locspoof.tailnet")

CLI_TIMEOUT = 10.0

# Windows ships the CLI outside PATH, so look there before giving up. Listed
# first on every platform because `shutil.which` is the cheap fallback.
WINDOWS_PATHS = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
)
UNIX_PATHS = (
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    # Docker and the static tarball both land here.
    "/usr/sbin/tailscale",
)


class TailscaleError(RuntimeError):
    """The CLI is missing, not running, or refused the request."""


class Peer(NamedTuple):
    """One machine in the tailnet."""

    name: str  # short name, e.g. "iphone-15-pro"
    ip: str  # tailnet IPv4, e.g. "100.113.121.104"
    os: str  # "iOS", "windows", "linux", ...
    user_id: int  # owning tailnet user
    online: bool

    @property
    def is_iphone(self) -> bool:
        # iPadOS reports "iOS" too, and both speak the same RemotePairing
        # protocol, so there is no reason to separate them.
        return self.os.lower() in ("ios", "ipados")


class Identity(NamedTuple):
    """Who is behind an inbound request."""

    login: str  # "opsec-bot@github"
    display: str  # "Credit"
    user_id: int
    machine: str  # the machine they connected from

    @property
    def is_local(self) -> bool:
        """True for the loopback pseudo-identity used when off-tailnet."""
        return self.user_id == LOCAL_USER_ID


# Reserved id for requests arriving on loopback. Anything reaching 127.0.0.1 is
# already running as the hub's own user, so it gets full access without asking
# Tailscale. That is what makes the hub usable on a laptop with no tailnet, and
# it cannot be spoofed from outside because loopback is not routable.
LOCAL_USER_ID = -1
LOCAL_IDENTITY = Identity(login="local", display="local", user_id=LOCAL_USER_ID, machine="loopback")


def find_cli() -> Optional[str]:
    """Absolute path to the tailscale binary, or None."""
    override = os.environ.get("TAILSCALE_CLI")
    if override and os.path.isfile(override):
        return override
    for candidate in WINDOWS_PATHS + UNIX_PATHS:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("tailscale")


async def _run(*args: str) -> str:
    """Run the CLI and return stdout, raising TailscaleError on any failure."""
    cli = find_cli()
    if cli is None:
        raise TailscaleError(
            "tailscale CLI not found. Install Tailscale, or set TAILSCALE_CLI to its path."
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise TailscaleError(f"could not run {cli}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise TailscaleError(f"tailscale {' '.join(args)} timed out")

    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise TailscaleError(f"tailscale {' '.join(args)}: {detail}")
    return stdout.decode(errors="replace")


async def _status() -> dict[str, Any]:
    raw = await _run("status", "--json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"could not parse status output: {exc}") from exc


def _short_name(node: dict[str, Any]) -> str:
    """Display name for a node.

    `HostName` is whatever the device calls itself, and iOS reports the useless
    "localhost" for every iPhone. The first label of the MagicDNS name is the
    name shown in the admin console, so prefer that and fall back only if it is
    missing.

    `status` calls the field `DNSName` and `whois` calls it `Name`, so accept
    either rather than returning "unknown" for half the callers.
    """
    dns = (node.get("DNSName") or node.get("Name") or "").strip(".")
    if dns:
        return dns.split(".", 1)[0]
    return node.get("HostName") or "unknown"


def _ipv4(node: dict[str, Any]) -> Optional[str]:
    for addr in node.get("TailscaleIPs") or []:
        if ":" not in addr:
            return addr
    return None


async def peers(include_self: bool = False) -> list[Peer]:
    """Every machine in the tailnet, self excluded by default."""
    status = await _status()
    nodes = list((status.get("Peer") or {}).values())
    if include_self and status.get("Self"):
        nodes.append(status["Self"])

    out: list[Peer] = []
    for node in nodes:
        ip = _ipv4(node)
        if ip is None:
            # IPv6-only nodes are not reachable by the addresses locspoof
            # passes around, so there is nothing useful to show.
            continue
        out.append(
            Peer(
                name=_short_name(node),
                ip=ip,
                os=node.get("OS") or "",
                user_id=int(node.get("UserID") or 0),
                online=bool(node.get("Online")),
            )
        )
    out.sort(key=lambda p: p.name)
    return out


async def iphones(user_id: Optional[int] = None) -> list[Peer]:
    """iOS devices in the tailnet, optionally only those owned by one user."""
    found = [p for p in await peers() if p.is_iphone]
    if user_id is not None and user_id != LOCAL_USER_ID:
        found = [p for p in found if p.user_id == user_id]
    return found


async def whois(address: str) -> Optional[Identity]:
    """Identity behind a `host:port` peer address, or None if not in the tailnet.

    Loopback short-circuits to `LOCAL_IDENTITY` without touching the CLI, so the
    hub works on a machine that is not on a tailnet at all.
    """
    host, _, port = address.rpartition(":")
    if not host:
        host, port = address, "0"
    host = host.strip("[]")
    if host in ("127.0.0.1", "::1", "localhost"):
        return LOCAL_IDENTITY

    # The docs are explicit that --json must precede the address argument.
    try:
        raw = await _run("whois", "--json", f"{host}:{port or 0}")
    except TailscaleError as exc:
        log.debug("whois %s failed: %s", host, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    profile = data.get("UserProfile") or {}
    node = data.get("Node") or {}
    login = profile.get("LoginName")
    if not login:
        # A tagged node has no user profile. Tagged devices are servers, not
        # people, so there is nobody to show a phone list to.
        return None
    return Identity(
        login=login,
        display=profile.get("DisplayName") or login,
        user_id=int(profile.get("ID") or 0),
        machine=_short_name(node),
    )


async def self_ipv4() -> Optional[str]:
    """This machine's tailnet IPv4, for choosing a bind address."""
    status = await _status()
    return _ipv4(status.get("Self") or {})
