"""One VM, several phones, one URL.

`app.py` holds exactly one tunnel, because PyTCP's stack is a process-global
singleton and `UserspaceRsdTunnel.aopen` guards against a second. That is not a
limit worth fighting: it just means one phone per process. So the hub owns no
tunnel of its own and instead runs one `app.py` per phone as a child process.

The part that makes it usable by more than one person is that nobody has to know
which port belongs to them. Every request is attributed to a tailnet user with
`tailscale whois`, which reads the identity off the WireGuard peer key the
packets arrived under and so cannot be forged by a header. The hub then proxies
that request to *that person's* worker. Two brothers open the same link and each
one drives his own phone. Neither can reach the other's, and there is no
password anywhere.

Workers bind loopback, never the tailnet address. That is the point of proxying
rather than redirecting: `server.py` has no auth, so a worker on a tailnet port
would be reachable by anyone on the tailnet. On 127.0.0.1 the only way in is
through the hub, which checks ownership on every request.

    browser (tailnet) ---> hub :8765 ---> whois ---> worker 127.0.0.1:88xx ---> phone

Run it the same way as `app.py`:

    python hub.py --host 100.x.y.z

`/` is the map for whoever is asking, or the setup page if they have no phone
running yet. `/hub/` is always the setup page.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import logging
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web

import pairing
import tailnet
from app import check_host, is_local, url_for

log = logging.getLogger("locspoof.hub")

HERE = Path(__file__).parent
WEB_DIR = HERE / "web"
DEFAULT_PORT = 8765

# Workers live here. Loopback only: they have no auth of their own.
WORKER_HOST = "127.0.0.1"
WORKER_PORT_BASE = 8800

# How long to wait for a freshly spawned worker to start answering before
# reporting it as broken.
WORKER_READY_TIMEOUT = 20.0

# Hop-by-hop headers must not be forwarded; Content-Length is recomputed because
# the body may be re-chunked, and Host must reflect the upstream.
SKIP_REQUEST_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade"}
SKIP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}

# Injected into the proxied map page so the setup page stays one tap away on a
# phone, where typing a URL is the worst part of any workflow.
HUB_LINK = (
    '<a href="/hub/" style="position:fixed;right:12px;bottom:12px;z-index:99999;'
    "background:rgba(22,27,34,.96);border:1px solid #30363d;border-radius:8px;"
    "padding:6px 10px;color:#8b949e;font:11px system-ui,sans-serif;"
    'text-decoration:none">phones</a>'
)


# --------------------------------------------------------------------- storage


class DeviceStore:
    """Remembers each phone's RemotePairing port between runs.

    The port is ephemeral on the phone and changes when it reboots, and finding
    it costs a full port sweep because Bonjour cannot cross a tailnet. Caching
    it turns that five-minute discovery into a one-off.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self._data = {}
        except Exception:
            log.warning("could not read %s, starting empty", self.path, exc_info=True)
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1))
        tmp.replace(self.path)

    def get(self, udid: str) -> dict[str, Any]:
        return self._data.get(udid, {})

    def put(self, udid: str, **fields: Any) -> None:
        self._data.setdefault(udid, {}).update(fields)
        self.save()

    def port_for(self, udid: str) -> Optional[int]:
        port = self.get(udid).get("port")
        return int(port) if port else None


# --------------------------------------------------------------------- workers


def free_port(start: int = WORKER_PORT_BASE) -> int:
    """A loopback port nothing is listening on."""
    for candidate in range(start, start + 200):
        with socket.socket() as probe:
            try:
                probe.bind((WORKER_HOST, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("no free loopback port for a worker")


class Worker:
    """One `app.py` child process, bound to one phone."""

    def __init__(self, udid: str, owner: int, label: str, device_address: Optional[str]) -> None:
        self.udid = udid
        self.owner = owner
        self.label = label
        self.device_address = device_address
        self.port = free_port()
        self.started_at = time.time()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.error: Optional[str] = None
        self._log: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://{WORKER_HOST}:{self.port}"

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        args = [
            sys.executable,
            str(HERE / "app.py"),
            "--host",
            WORKER_HOST,
            "--port",
            str(self.port),
            "--no-browser",
            "--device-udid",
            self.udid,
        ]
        # An address is only needed to skip discovery. Without one, device.py
        # tries USB first and then Bonjour, which is exactly what should happen
        # on a machine sitting next to the phone: a cable or the same Wi-Fi
        # works with no port known at all.
        if self.device_address:
            args += ["--device-address", self.device_address]
        log.info("spawning worker for %s on %s", self.label, self.port)
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(HERE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._drain(), name=f"worker-log-{self.port}")
        await self._await_ready()

    async def _drain(self) -> None:
        """Keep the child's output so a failure has an explanation attached."""
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                self._log.append(text)
                del self._log[:-200]
                log.debug("[%s] %s", self.label, text)

    async def _await_ready(self) -> None:
        """Poll the worker's own status endpoint until it answers."""
        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if not self.alive:
                    raise RuntimeError(
                        f"worker exited immediately: {self.tail() or 'no output'}"
                    )
                try:
                    async with session.get(
                        f"{self.base_url}/api/status", timeout=aiohttp.ClientTimeout(total=2)
                    ) as resp:
                        if resp.status == 200:
                            return
                except Exception:
                    pass
                await asyncio.sleep(0.3)
        raise RuntimeError(f"worker did not start in time: {self.tail() or 'no output'}")

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return

        # Ask the worker to drop the spoof before killing it. app.py's
        # on_cleanup already does this on SIGTERM, but only on POSIX: on Windows
        # `terminate()` is TerminateProcess, which runs no handlers at all and
        # would leave the phone reporting its last fake position for good. One
        # HTTP call makes stopping reliable on both.
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/clear", timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    await resp.read()
        except Exception:
            log.debug("could not clear location before stopping %s", self.label, exc_info=True)

        with contextlib.suppress(ProcessLookupError):
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("worker %s ignored terminate, killing", self.label)
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()

    def tail(self, lines: int = 6) -> str:
        return " | ".join(self._log[-lines:])

    def snapshot(self) -> dict[str, Any]:
        return {
            "udid": self.udid,
            "label": self.label,
            "owner": self.owner,
            "port": self.port,
            "device_address": self.device_address,
            "alive": self.alive,
            "uptime_s": round(time.time() - self.started_at, 1),
            "log": self._log[-6:],
        }


class WorkerManager:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        # Which phone a given machine is looking at, when more than one is up.
        # Keyed by machine rather than user because two people sharing one
        # tailnet login are one user as far as whois is concerned, and they
        # still need separate selections.
        self._selected: dict[str, str] = {}

    def all(self) -> list[Worker]:
        return list(self._workers.values())

    def get(self, udid: str) -> Optional[Worker]:
        return self._workers.get(udid)

    def owned_by(self, identity: tailnet.Identity) -> list[Worker]:
        # The loopback identity is the hub's own operator, so it sees everything;
        # that is the only way to run this on a laptop with no tailnet.
        if identity.is_local:
            return self.all()
        return [w for w in self._workers.values() if w.owner == identity.user_id]

    def current(self, identity: tailnet.Identity) -> Optional[Worker]:
        """The worker this request should be proxied to.

        Ownership narrows the candidates; picking among them is deliberately
        not just "the first one". Separate tailnet accounts make this trivial,
        but a family sharing one login is one user to whois, so the tie is
        broken on the machine the request came from: browse from your own
        phone and you get your own phone's map, with no selection needed.
        """
        mine = [w for w in self.owned_by(identity) if w.alive]
        if not mine:
            return None

        chosen = self._selected.get(identity.machine)
        for worker in mine:
            if worker.udid == chosen:
                return worker
        # A worker labelled with the requesting machine is that machine's own
        # phone driving its own map, which is never the wrong answer.
        for worker in mine:
            if worker.label == identity.machine:
                return worker
        return mine[0]

    def select(self, identity: tailnet.Identity, udid: str) -> None:
        self._selected[identity.machine] = udid

    async def start(self, udid: str, owner: int, label: str, device_address: str) -> Worker:
        existing = self._workers.get(udid)
        if existing is not None:
            if existing.alive:
                return existing
            await existing.stop()
        worker = Worker(udid, owner, label, device_address)
        self._workers[udid] = worker
        try:
            await worker.start()
        except Exception:
            self._workers.pop(udid, None)
            raise
        return worker

    async def stop(self, udid: str) -> bool:
        worker = self._workers.pop(udid, None)
        if worker is None:
            return False
        await worker.stop()
        return True

    async def stop_all(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.all()), return_exceptions=True)
        self._workers.clear()


# ------------------------------------------------------------------------ jobs


class Job:
    """A long operation the browser polls instead of holding a request open.

    Pairing waits on a human tapping a dialog and a port sweep takes minutes.
    Neither belongs in a request that a phone browser will give up on.
    """

    def __init__(self, kind: str, owner: int) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.owner = owner
        self.state = "running"  # running | done | failed
        self.messages: list[str] = []
        self.result: dict[str, Any] = {}
        self.error: Optional[str] = None
        self.started_at = time.time()

    def say(self, message: str) -> None:
        log.info("[job %s] %s", self.id, message)
        self.messages.append(message)
        del self.messages[:-40]

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "messages": self.messages,
            "result": self.result,
            "error": self.error,
            "elapsed_s": round(time.time() - self.started_at, 1),
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = itertools.count()

    def create(self, kind: str, owner: int) -> Job:
        job = Job(kind, owner)
        self._jobs[job.id] = job
        # Keep the table from growing without bound over a long uptime.
        if next(self._counter) % 20 == 19:
            self._prune()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        for job_id, job in list(self._jobs.items()):
            if job.state != "running" and job.started_at < cutoff:
                del self._jobs[job_id]


# ------------------------------------------------------------------------- app


def build_hub_app(store: DeviceStore, workers: WorkerManager) -> web.Application:
    app = web.Application()
    jobs = JobRegistry()

    async def identify(request: web.Request) -> Optional[tailnet.Identity]:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer:
            return None
        address = f"{peer[0]}:{peer[1]}"
        return await tailnet.whois(address)

    async def require_identity(request: web.Request) -> tailnet.Identity:
        identity = await identify(request)
        if identity is None:
            raise web.HTTPForbidden(
                reason="not a tailnet user",
                text=(
                    "This hub identifies people by their Tailscale account and could "
                    "not identify you. Reach it over the tailnet, and on Linux make "
                    "sure the hub may call the local API:\n\n"
                    "    sudo tailscale set --operator=$USER\n"
                ),
            )
        return identity

    def device_address(udid: str, ip: str) -> Optional[str]:
        port = store.port_for(udid)
        return f"{ip}:{port}" if port else None

    # ------------------------------------------------------------- hub pages

    async def hub_page(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "hub.html")

    async def hub_state(request: web.Request) -> web.StreamResponse:
        """Everything the setup page draws: who you are, your phones, your worker."""
        identity = await require_identity(request)
        try:
            phones = await tailnet.iphones(identity.user_id)
            tailnet_error = None
        except tailnet.TailscaleError as exc:
            phones, tailnet_error = [], str(exc)

        known = set(pairing.paired_udids())
        current = workers.current(identity)
        devices = []
        for phone in phones:
            saved = store.get(phone.name) or {}
            udid = saved.get("udid")
            devices.append(
                {
                    "name": phone.name,
                    "ip": phone.ip,
                    "online": phone.online,
                    "udid": udid,
                    "port": store.port_for(udid) if udid else None,
                    "paired": bool(udid and udid in known),
                    "running": bool(udid and (w := workers.get(udid)) and w.alive),
                }
            )
        return web.json_response(
            {
                "identity": {
                    "login": identity.login,
                    "display": identity.display,
                    "machine": identity.machine,
                    "local": identity.is_local,
                },
                "devices": devices,
                "tailnet_error": tailnet_error,
                "paired_udids": sorted(known),
                "workers": [w.snapshot() for w in workers.owned_by(identity)],
                "current": current.udid if current else None,
            }
        )

    async def hub_job(request: web.Request) -> web.StreamResponse:
        job = jobs.get(request.match_info["job_id"])
        if job is None:
            raise web.HTTPNotFound(reason="no such job")
        return web.json_response(job.snapshot())

    async def hub_discover(request: web.Request) -> web.StreamResponse:
        """Find a phone's UDID and RemotePairing port, as a background job."""
        identity = await require_identity(request)
        body = await request.json()
        name = str(body.get("name") or "")
        ip = str(body.get("ip") or "")
        if not ip:
            raise web.HTTPBadRequest(reason="ip is required")

        job = jobs.create("discover", identity.user_id)

        async def run() -> None:
            try:
                port = store.port_for(store.get(name).get("udid") or "")
                if port is not None:
                    job.say(f"trying the remembered port {port}")
                    try:
                        result = await pairing.probe(ip, port)
                        store.put(name, udid=result.udid, port=port, ip=ip)
                        store.put(result.udid, udid=result.udid, port=port, ip=ip, name=name)
                        job.result = {"udid": result.udid, "port": port, "paired": result.paired}
                        job.state = "done"
                        job.say("the phone answered on the remembered port")
                        return
                    except Exception as exc:
                        job.say(f"the remembered port no longer answers: {exc}")

                found = await pairing.find_service_port(ip, progress=job.say)
                if found is None:
                    job.state = "failed"
                    job.error = (
                        "No RemotePairing service answered on any port. The phone is "
                        "not serving it on the interface this VM can reach. Put the "
                        "phone on Wi-Fi and try again, or pair over USB and import "
                        "the record instead."
                    )
                    return
                result = await pairing.probe(ip, found)
                store.put(name, udid=result.udid, port=found, ip=ip)
                store.put(result.udid, udid=result.udid, port=found, ip=ip, name=name)
                job.result = {"udid": result.udid, "port": found, "paired": result.paired}
                job.state = "done"
            except Exception as exc:
                log.exception("discover failed")
                job.state = "failed"
                job.error = str(exc)

        asyncio.create_task(run(), name=f"job-{job.id}")
        return web.json_response({"job": job.id})

    async def hub_pair(request: web.Request) -> web.StreamResponse:
        """Pair over the network. The phone shows a Trust prompt."""
        identity = await require_identity(request)
        body = await request.json()
        udid = str(body.get("udid") or "")
        ip = str(body.get("ip") or "")
        port = body.get("port") or (store.port_for(udid) if udid else None)
        if not (udid and ip and port):
            raise web.HTTPBadRequest(reason="udid, ip and port are required")

        job = jobs.create("pair", identity.user_id)

        async def run() -> None:
            try:
                await pairing.pair(ip, int(port), udid, progress=job.say)
                job.result = {"udid": udid}
                job.state = "done"
            except Exception as exc:
                log.exception("pairing failed")
                job.state = "failed"
                job.error = str(exc)

        asyncio.create_task(run(), name=f"job-{job.id}")
        return web.json_response({"job": job.id})

    async def hub_import(request: web.Request) -> web.StreamResponse:
        """Install a pair record made on a machine that has a cable."""
        await require_identity(request)
        reader = await request.multipart()
        udid = ""
        data = b""
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "udid":
                udid = (await part.text()).strip()
            elif part.name == "record":
                data = await part.read(decode=False)
                # The canonical filename carries the UDID, so accept it as the
                # identifier when the form did not supply one.
                if not udid and part.filename:
                    stem = Path(part.filename).stem
                    if stem.startswith("remote_"):
                        udid = stem[len("remote_") :]
        if not udid or not data:
            raise web.HTTPBadRequest(reason="need a udid and a record file")
        try:
            pairing.import_record(udid, data)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc))
        return web.json_response({"ok": True, "udid": udid})

    async def hub_start(request: web.Request) -> web.StreamResponse:
        """Bring up a worker for one phone and make it this user's current one."""
        identity = await require_identity(request)
        body = await request.json()
        udid = str(body.get("udid") or "")
        ip = str(body.get("ip") or "")
        label = str(body.get("name") or udid[:8])
        if not udid:
            raise web.HTTPBadRequest(reason="udid is required")
        if udid not in set(pairing.paired_udids()):
            raise web.HTTPBadRequest(reason="that phone is not paired with this machine yet")

        # No address is not an error. It means nothing has found this phone over
        # the network yet, so let the worker fall back to USB and Bonjour; on a
        # machine next to the phone that is the path that works anyway.
        ip = ip or str(store.get(udid).get("ip") or "")
        address = device_address(udid, ip) if ip else None
        try:
            worker = await workers.start(udid, identity.user_id, label, address)
        except Exception as exc:
            raise web.HTTPBadGateway(reason=f"could not start the worker: {exc}")
        workers.select(identity, udid)
        return web.json_response({"ok": True, "worker": worker.snapshot()})

    async def hub_stop(request: web.Request) -> web.StreamResponse:
        identity = await require_identity(request)
        body = await request.json()
        udid = str(body.get("udid") or "")
        worker = workers.get(udid)
        if worker is None:
            return web.json_response({"ok": True, "stopped": False})
        if not identity.is_local and worker.owner != identity.user_id:
            raise web.HTTPForbidden(reason="that is not your phone")
        await workers.stop(udid)
        return web.json_response({"ok": True, "stopped": True})

    async def hub_select(request: web.Request) -> web.StreamResponse:
        identity = await require_identity(request)
        body = await request.json()
        udid = str(body.get("udid") or "")
        worker = workers.get(udid)
        if worker is None or (not identity.is_local and worker.owner != identity.user_id):
            raise web.HTTPBadRequest(reason="not one of your running phones")
        workers.select(identity, udid)
        return web.json_response({"ok": True})

    # ----------------------------------------------------------------- proxy

    async def proxy(request: web.Request) -> web.StreamResponse:
        """Forward this request to the requester's own worker.

        Identity decides the destination, so every person opening `/` lands on
        their own phone's map without a per-user URL, and a request can never be
        aimed at someone else's worker by editing a path.
        """
        identity = await require_identity(request)
        worker = workers.current(identity)
        if worker is None:
            # Nothing running for this person: the setup page is the useful
            # answer for a browser, and a clear error for anything else.
            if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                return await hub_page(request)
            raise web.HTTPServiceUnavailable(reason="no phone running for you yet")

        url = worker.base_url + str(request.rel_url)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP_REQUEST_HEADERS}
        session: aiohttp.ClientSession = request.app["session"]
        try:
            upstream = await session.request(
                request.method,
                url,
                headers=headers,
                data=request.content if request.body_exists else None,
                allow_redirects=False,
            )
        except Exception as exc:
            raise web.HTTPBadGateway(
                reason="worker is not answering",
                text=f"{worker.label}: {exc}\n{worker.tail()}",
            )

        async with upstream:
            content_type = upstream.headers.get("Content-Type", "")
            # Content-Type is set explicitly on both branches below, and aiohttp
            # rejects having it in both places, so keep it out of the copy.
            out_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in SKIP_RESPONSE_HEADERS and k.lower() != "content-type"
            }

            # The map page gets a link back to the setup page. Buffering is fine
            # for one HTML document and lets the anchor be inserted; everything
            # else, SSE above all, must stream untouched.
            if "text/html" in content_type:
                body = await upstream.read()
                text = body.decode("utf-8", errors="replace")
                if "</body>" in text:
                    text = text.replace("</body>", HUB_LINK + "</body>", 1)
                return web.Response(
                    status=upstream.status, headers=out_headers, text=text, content_type="text/html"
                )

            response = web.StreamResponse(status=upstream.status, headers=out_headers)
            if content_type:
                response.headers["Content-Type"] = content_type
            if "text/event-stream" in content_type:
                # Server-sent events: no buffering anywhere on the path, or the
                # map stops updating until something else flushes it.
                response.headers["Cache-Control"] = "no-cache"
                response.headers["X-Accel-Buffering"] = "no"
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            except ConnectionResetError:
                # The browser navigated away or the tab slept, which happens on
                # every SSE stream a phone ever opens. Returning the response
                # ends the handler quietly; re-raising would make aiohttp log a
                # traceback for ordinary behaviour.
                log.debug("client closed the stream for %s", worker.label)
            return response

    app.add_routes(
        [
            web.get("/hub/", hub_page),
            web.get("/hub/state", hub_state),
            web.get("/hub/jobs/{job_id}", hub_job),
            web.post("/hub/discover", hub_discover),
            web.post("/hub/pair", hub_pair),
            web.post("/hub/import", hub_import),
            web.post("/hub/start", hub_start),
            web.post("/hub/stop", hub_stop),
            web.post("/hub/select", hub_select),
            # Everything else belongs to whichever worker is the requester's.
            web.route("*", "/{tail:.*}", proxy),
        ]
    )
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="locspoof hub: many phones, one VM")
    parser.add_argument(
        "--host",
        default=WORKER_HOST,
        metavar="ADDR",
        help="address to serve on. Loopback by default; give it this machine's "
        "tailnet address (100.x.y.z) to let other people in.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--state",
        default=str(Path.home() / ".locspoof" / "devices.json"),
        help="where to remember each phone's RemotePairing port",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = check_host(args.host)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("pymobiledevice3").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING
    )

    store = DeviceStore(Path(args.state))
    workers = WorkerManager()
    app = build_hub_app(store, workers)

    async def on_startup(app_: web.Application) -> None:
        # No total timeout: SSE responses are meant to stay open for hours.
        app_["session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        )
        url = url_for(host, args.port)
        print(f"\n  locspoof hub running at {url}")
        if is_local(host):
            print("  loopback only. Pass --host <your 100.x.y.z> to let others in.")
        else:
            print("  everyone on the tailnet opens that one link and gets their own phone.")
        if tailnet.find_cli() is None:
            print("  WARNING: no tailscale CLI found, so nobody can be identified.")
        print("  press Ctrl+C to stop every phone and restore their real GPS\n")

    async def on_cleanup(app_: web.Application) -> None:
        print("\n  stopping every worker...")
        await workers.stop_all()
        await app_["session"].close()
        print("  done. every phone is back on its real GPS.")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=host, port=args.port, print=None, handle_signals=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
