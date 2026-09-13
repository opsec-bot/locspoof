# locspoof

Local iPhone location simulator for Windows. A map you click, a route you draw,
and a supervisor that keeps the tunnel alive. Built directly on pymobiledevice3.

## Requirements

- **Apple Mobile Device Support** (installed with iTunes or the Apple Devices app
  from the Microsoft Store). This provides usbmuxd, the USB transport. Without it
  nothing can talk to the phone.
- **Python 3.13** with `pymobiledevice3`, `aiohttp`, `gpxpy`. All already present
  on this machine.
- **iOS 17 or newer**, with **Developer Mode** on:
  Settings > Privacy & Security > Developer Mode, then restart.
- iPhone connected by **USB**, unlocked, and trusting this computer.

Verified end to end on **iOS 26.5.2** with pymobiledevice3 11.12.1 on Windows 10.
The in-process `UserspaceRsdTunnel` works on that version, so no admin rights and
no separate `tunneld` process are needed. If a future iOS build breaks that path,
pymobiledevice3's `tunneld.api.get_tunneld_devices` is the fallback: run
`pymobiledevice3 lockdown start-tunnel` alongside and attach to the existing RSD
instead.

## Run

```
python app.py
```

It opens `http://127.0.0.1:8765/`. Press Ctrl+C to stop, which clears the
simulated location and hands the phone back to its real GPS.

Flags: `--host ADDR`, `--port N`, `--no-browser`, `-v` (debug logging,
including pymobiledevice3 internals).

## Using it

**Search** — type an address, city or landmark to jump the map there.
Backed by Nominatim.

**Pin mode** — click anywhere and the phone reports that spot. Or paste
`33.381861, -96.259611` into the coordinates box.

**Route mode, follow roads** — click a start, then a destination, and the route
is planned along real streets automatically. Two clicks is the whole workflow;
it plans as soon as the second one lands. Add more clicks for waypoints to go
through. Pick Drive, Bike or Walk and it re-plans on that network.

**Route mode, straight lines** — click to drop points joined by direct lines.
Use this for open water, off-road, or when the routing servers are down.

**Speed** is in mph, with presets from walking to highway. `loop` restarts each
lap; `back and forth` reverses at the end instead.

**Realistic motion** (on by default) makes the phone accelerate from rest, brake
for corners, obey each road's own speed limit, and wait at junctions. With it
off you get the old behaviour: a constant glide down the line. **Road speeds**
uses OSRM's per-segment speed for each street; the mph box then acts as a
ceiling rather than a fixed speed, so a 45 mph setting still crawls through a
car park.

**GPX** — import a `.gpx` to load it as a route, or export what you have drawn.

**Stop spoofing** restores the real GPS without quitting.

### Basemaps

Six keyless layers via the control in the top right, and your choice is
remembered: Satellite + labels (default), Satellite, Streets, Topographic,
Dark, and OpenStreetMap. Satellite and Streets go to zoom 19; the Dark canvas
stops at 16, so switch to Satellite when placing a pin on a specific building.

## Many phones on one VM

`app.py` drives one phone. To let several people share a single always-on box,
`hub.py` runs one `app.py` per phone and puts them all behind one URL:

```
python hub.py --host 100.82.227.93
```

Everyone opens that same link. Nobody picks a port, and nobody gets a password.

### How it decides whose phone you get

Every request is attributed with `tailscale whois`, which resolves the source
address back to the tailnet user whose WireGuard key the packets actually
arrived under. That is not a header, so it cannot be forged by the person
sending the request. The hub then proxies to *that person's* worker:

```
browser (tailnet) --> hub :8765 --> whois --> worker 127.0.0.1:88xx --> phone
```

So `/` is your map, and your brother loading the identical URL gets his. `/hub/`
is always the setup page, and the map carries a small link back to it.

Workers bind loopback, never the tailnet address. That is the reason the hub
proxies rather than redirecting: `server.py` has no auth, so a worker on a
tailnet port would be reachable by anyone on the tailnet, whereas on 127.0.0.1
the only route in is through the hub, which checks ownership every time.

Two people with their own tailnet accounts are fully separated: neither can see
or reach the other's phone at all. Sharing one login is one user as far as
whois is concerned, so that isolation is gone, and the hub instead breaks the
tie on the machine the request came from -- browse from your own phone and you
get your own phone's map. That works, but if you want the isolation, invite
your brother as a real user from the Users tab rather than signing his phone in
to your account.

On Linux the tailscale local API socket is root-owned, so the hub needs one
grant before `whois` will answer:

```
sudo tailscale set --operator=$USER
```

Without it the hub fails closed and serves nobody. Requests arriving on
loopback skip whois entirely and see every phone, which is what makes the hub
usable on a laptop that is not on a tailnet at all.

`deploy/locspoof-hub.service` is a systemd unit for the VM.

### Onboarding a phone without a cable

The documented way to create a RemotePairing record is
`pymobiledevice3 lockdown remotepairing --pair` over USB, and a VM has no USB.
pymobiledevice3 has a second path that the hub uses instead:
`RemotePairingManualPairingService` opens a plain TCP connection to the phone
and runs the same SRP handshake. On an iPhone no PIN is involved, because
`_request_pair_consent` raises a Trust / Don't Trust dialog naming the host and
the SRP password is the fixed `"000000"` (the PIN branch is tvOS only). Tap
Trust and the record is written on the VM. Nothing is plugged in and nothing is
uploaded.

Two details make this work in practice.

`RemotePairingTunnelService.remote_identifier` returns the constructor argument
rather than what the handshake reported, and `pair_record_path` is built from
it, so constructing one with an empty identifier saves the record as
`remote_.plist` where no later lookup will ever find it.
`pairing._IdentifiedPairingService` prefers the handshake value so first contact
with an unknown phone still saves correctly.

And the port has to be found by scanning. remoted takes whatever ephemeral port
Darwin gives it, so it changes when the phone reboots, and the phone announces
it over Bonjour, which is multicast and cannot cross a tailnet. `pairing.find_service_port`
tries the likely ports, then sweeps 49152-65535. Ports that merely accept TCP
are rejected, because Tailscale's own peerapi listens on the phone too and
accepting a connection proves nothing; each candidate has to complete a
RemotePairing handshake to count. The result is cached per device, so the sweep
is a one-off rather than a startup cost.

### What is measured, and what is not

The hub itself is verified end to end: identity, worker spawn, the proxy for
HTML, JSON and SSE, and stop.

Network pairing is **not** verified, and the evidence so far is against it. A
full sweep of 49152-65535 on an iPhone 15 Pro (iOS 26.5.2) over Tailscale, with
the phone on cellular, found exactly two listeners: Tailscale's own peerapi
(61600) and one port that accepted TCP but never answered the handshake. So on
cellular the phone does not appear to serve RemotePairing on the tunnel
interface, which would block not just pairing but the whole VM approach for a
phone away from home. Whether it is served over Wi-Fi is the open question;
`find_service_port` is how to answer it.

If it turns out not to be, the fallback is a one-time cable pairing on a machine
that has one. The record is three keys in a plist and names nothing about the
host that made it, so it works unchanged on the VM. The setup page takes the
upload, or copy it by hand:

```
~/.pymobiledevice3/remote_<UDID>.plist
```

That fallback still needs a reachable RemotePairing port to build a tunnel, so
it solves onboarding, not reachability.

## How it works

Four moving parts:

| File | Responsibility |
|---|---|
| `device.py` | Owns the tunnel and the LocationSimulation channel |
| `route.py` | Polyline geometry, GPX, playback clock |
| `routing.py` | Road routing and place search, with engine fallback |
| `server.py` | HTTP API and SSE status stream, loopback unless `--host` |
| `web/index.html` | Leaflet UI |
| `hub.py` | Runs one `app.py` per phone and routes people to their own |
| `tailnet.py` | Tailscale as the device directory and the login |
| `pairing.py` | Pairing a phone with a machine that has no USB port |
| `web/hub.html` | The setup page behind `/hub/` |

The chain to the phone is:

```
usbmux (Apple driver)
  -> UserspaceRsdTunnel        in-process, no admin needed
    -> DvtProvider             DTX connection over the tunnel
      -> LocationSimulation    .set(lat, lon) / .clear()
```

`LocationSimulation.set()` invokes the
`simulateLocationWithLatitude:longitude:` selector on
`com.apple.instruments.server.services.LocationSimulation`. That single call is
the entire spoofing mechanism; everything else is plumbing to reach it.

### Why one process

pymobiledevice3's userspace tunnel runs on PyTCP, whose network stack is a
process-global singleton. Only one tunnel can exist per process, and opening a
second raises. So a single supervisor task owns the tunnel and every caller goes
through `LocationSession`.

### Road routing

`routing.py` calls public OSRM instances. OSRM answers with Contraction
Hierarchies, a preprocessed Dijkstra variant, which is why a 19-mile query
returns in well under a second. Running A* here instead would mean holding the
OSM road graph locally, gigabytes per region, for no gain.

Each profile has a list of engines tried in order, because the public demo
servers go down often:

| Profile | First choice | Fallback |
|---|---|---|
| Drive | router.project-osrm.org | routing.openstreetmap.de/routed-car |
| Bike | routing.openstreetmap.de/routed-bike | router.project-osrm.org |
| Walk | routing.openstreetmap.de/routed-foot | router.project-osrm.org |

If every engine fails the UI says so and suggests straight-line mode, rather
than leaving you with a dead button. Planning is proxied through the local
server, not the browser, so there is one place for the fallback logic.

### Motion model

Constant speed is the loudest tell a simulated location has. Nothing real holds
11.176 m/s for twenty minutes, takes a right angle without slowing, or crosses
forty junctions without ever waiting.

`motion.py` turns a route into a velocity profile, built once in four passes:

1. A speed ceiling per sample, from OSRM's per-segment speeds.
2. Corner braking, from the turn angle at each vertex.
3. Stops, chosen at real junctions and turn manoeuvres.
4. A forward pass limiting acceleration and a backward pass limiting
   deceleration.

Pass 4 is what sells it. Sweeping `v² = u² + 2as` forwards bounds how fast speed
can rise, sweeping it backwards bounds how late braking can start, and the
pointwise minimum is the fastest profile physically reachable under the ceiling.
The phone eases away from a stop and brakes into it instead of teleporting
between speeds.

Two details that matter:

- **A stop's speed floor is not zero.** The player advances by `v * dt`, so a
  true zero would mean never arriving at the point it is braking for. The
  vehicle creeps in at 0.6 m/s and the standstill is a dwell timer, during
  which the position does not change at all.
- **Routes start and end at rest**, for the same reason and by the same
  mechanism. A phone that appears already doing 45 mph is not a journey.

Junctions come from OSRM's `intersections`, filtered to nodes with three or more
bearings; two bearings is just the road bending. Turns come from `maneuver.type`,
excluding `new name` and `continue`, which are the road changing name under you
rather than you turning.

Seed the plan to get the same stops every run; leave it off for fresh ones.

### Untethered over Wi-Fi

The cable is only needed once. After a single pairing step the phone can be
driven over the network with nothing plugged in.

One time, with the phone connected by USB:

```
python -m pymobiledevice3 lockdown remotepairing --pair
```

That handshake runs over the already-trusted lockdownd transport, so it is
promptless: no Trust dialog appears. It writes the RemotePairing pair record
that Wi-Fi discovery depends on. Confirm it took with
`python -m pymobiledevice3 remote browse`, which should list the phone under
`wifi` once the cable is out.

From then on `locspoof` finds the phone by itself. USB is preferred whenever the
cable is present, because it establishes faster and cannot be disturbed by the
network; Wi-Fi is used otherwise. The status line says which link is live.

**How it works.** iOS 17.4+ exposes CoreDeviceProxy over lockdown, so
pymobiledevice3's no-root helper always bootstraps over USB and only falls back
to RemotePairing over Bonjour for older devices. That is a policy in the helper,
not a limit of the device: a modern iPhone advertises `_remotepairing._tcp` on
the LAN and serves the same tunnel over Wi-Fi.

`UserspaceRsdTunnel._aopen_locked` is entirely transport-agnostic apart from one
call to the module-level `_create_no_root_tunnel_provider`, which hard-codes the
USB bootstrap. `wireless.py` swaps that single function for the duration of
`aopen()`, which reuses the library's whole lifecycle: the process-global
single-tunnel guard, the PyTCP tun, the dial plane, the RSD handshake and the
AsyncExitStack teardown. Reimplementing `_aopen_locked` would have to reach into
those same private globals to stay correct, and would rot faster.

Discovery deduplicates by identifier, since Bonjour answers on every interface
and the same phone comes back over both IPv4 and IPv6.

Two things to know. Bonjour only returns devices that already hold a pair record
locally, so discovery finding nothing usually means the pairing step has not been
done rather than that the phone is unreachable. And a Wi-Fi session cannot poll
usbmux for presence the way a USB one does, so it relies on the tunnel's own
transport watcher to notice the link dying.

Measured on an iPhone 15 Pro on iOS 26.5.2: discovery about 3 s, tunnel up in
0.6 s, DVT channel open 0.2 s later.

### Beyond the LAN

Bonjour is multicast, so it dies at the first router and only ever finds a phone
on the same network segment. Given an address instead, the phone is reachable
anywhere it is routable:

```
python app.py --device-address 100.64.0.3
```

Put both the phone and this machine on a VPN such as Tailscale and that address
is their tailnet IP, which makes the phone controllable from anywhere with
internet, with no cable and no shared network. The identifier is inferred when
only one device is paired; pass `--device-udid` if several are.

This is also simply faster, since it skips the discovery timeout entirely: the
same phone went from address to open channel in 0.7 s against 3.9 s via Bonjour.

`--no-wireless` forces USB only.

### Driving it from the phone

The link above reaches the phone from the computer. Doing anything with it still
meant sitting at the computer, because the map was served on loopback. `--host`
serves it on a chosen address instead:

```
python app.py --device-address 100.64.0.3 --host 100.64.0.2
```

Then open `http://100.64.0.2:8765/` in Safari on the phone. Both legs now run
over the tailnet — the control API out to the phone, the map back from it — so
the computer can be at home with nothing plugged into it.

Below 640px wide the panel becomes a bottom sheet that drops away with one tap,
because on a phone the map *is* the control surface: every pin and every route
point is a tap on it, and a 340px panel over a 390px screen leaves nothing to
tap. Controls grow to thumb size and text inputs go to 16px, under which iOS
Safari zooms the whole page on focus.

**There is no password on any of this.** Loopback was the security boundary, and
`--host` trades it for whichever network you name, so name a private one: a
tailnet, where the ACLs are the access control, not a coffee shop's Wi-Fi.
`0.0.0.0` and `::` are refused outright for that reason — binding every
interface at once is never the narrow choice, and the difference between one
routable address and all of them is the difference between your phone and
anyone's.

What none of this becomes is phone-only. The computer still has to be running
this program and reachable; it just no longer has to be in front of you.
Removing it altogether means an app on the device driving its own tunnel, which
is a different project and needs macOS to build.

### GPS drift

Two things give a simulated fix away even when the coordinates are plausible: a
parked phone reporting byte-identical coordinates for thirty seconds, and a
moving one sitting exactly on the road centreline. Real receivers do neither.

`jitter.py` is an Ornstein-Uhlenbeck process, which is the standard model for
correlated, mean-reverting drift:

    dx = -(x / tau) dt + sigma * sqrt(2 dt / tau) dW

Independent noise per tick would look like television static and be trivially
separable from a real trace; this drifts smoothly instead, with a measured
lag-1 correlation of about 0.9. Being mean-reverting it never walks away, so
the position stays honest over a long session: 15 m was the worst excursion in
100,000 steps.

Spread is about 3 m stationary and 1.5 m moving, since a receiver averaging over
a moving baseline reports a tighter fix than one sitting still among buildings.
The initial offset is drawn from the equilibrium distribution rather than zero,
because a first fix that is perfectly accurate is itself a tell.

Drift is applied to a *copy* of the true position, so progress along a route
stays exact while only the reported fix wanders.

A parked pin gets the same treatment: `device.py` re-pushes it once a second
with fresh drift. That loop is gated on the route player rather than a flag, so
a route finishing on its own hands control back with nothing to reset.

### Reconnect behaviour

The supervisor is a state machine:

```
no_device -> connecting -> ready -> (failure) -> no_device
```

The coordinate you set is stored as *desired* state, separate from what is
currently applied. Unplug the phone mid-session and the API keeps accepting
coordinates without erroring; when the phone comes back, the supervisor
re-applies the last one automatically. Route playback keeps its own clock
running across a drop rather than stalling, so the position stays consistent
with elapsed time.

Failures back off from 2s to a 30s ceiling. Device presence is polled every 2s.

## Limits

- The spoof lasts only while this program is running and the phone is reachable.
  Quit, unplug, or reboot the phone and the real GPS returns.
- Developer Mode stays visible in Settings while enabled.
- Untethered operation is not supported here. That needs an app running on the
  phone itself driving its own on-device tunnel, which is a different project.
- Apps can still infer simulation heuristically: teleport-speed jumps, a fixed
  altitude, or GPS that disagrees with Wi-Fi BSSIDs and motion sensors.
  Apple's `isSimulatedBySoftware` flag is not set by this path.

## Licence

Uses pymobiledevice3, which is **GPL-3.0-or-later**. If you distribute this,
those terms apply.
