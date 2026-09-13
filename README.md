# locspoof

Click a spot on a map and your iPhone reports that it is there. Draw a route and
the phone drives it, slowing for corners and waiting at lights. The cable is
needed once, and after that the phone can be in another state. Built on
pymobiledevice3.

## What you need

- An iPhone on iOS 17 or newer with Developer Mode on:
  Settings > Privacy & Security > Developer Mode, then restart.
- Windows with Apple Mobile Device Support, which arrives with iTunes or with
  the Apple Devices app from the Microsoft Store. It is the USB driver. Without
  it nothing can reach the phone.
- Python 3.13.

## Setup

1. Install the packages:

   ```
   pip install -r requirements.txt
   ```

2. Plug the phone in, unlock it, tap Trust.

3. Start it:

   ```
   python app.py
   ```

A browser opens on `http://127.0.0.1:8765/`. Click the map. The phone moves.

Ctrl+C stops it and hands the phone back to its real GPS.

Flags: `--host ADDR`, `--port N`, `--no-browser`, `-v` for debug logging.

Tested end to end on iOS 26.5.2 with pymobiledevice3 11.12.1 on Windows 10. The
in-process `UserspaceRsdTunnel` works there, so nothing needs admin rights and
no separate `tunneld` has to run. Should a later iOS break that path,
`tunneld.api.get_tunneld_devices` is the way back: run
`pymobiledevice3 lockdown start-tunnel` alongside and attach to the RSD it
opens.

## Using it

The map opens in Pin mode. Click anywhere and the phone reports that spot, click
again and it moves. Paste `33.381861, -96.259611` into the coordinates box to
land somewhere exact. The search box takes an address, a city or a landmark and
jumps the map there, through Nominatim.

Switch to Route mode and your first two clicks become a start and a destination.
It plans along real streets the moment the second one lands. More clicks add
waypoints to pass through, and Drive, Bike and Walk each re-plan on their own
network. For open water, for a field, or for an afternoon when the routing
servers are down, straight-line mode joins your clicks with direct lines.

Speed is in mph, with presets from walking to highway. Loop restarts each lap
and back and forth reverses at the end. Realistic motion, on by default, pulls
away from rest, brakes for corners, keeps to each road's speed limit and waits
at junctions; switch it off and you get a constant glide down the line. Road
speeds takes OSRM's figure for each street and turns the mph box into a ceiling,
so 45 still crawls through a car park.

Import a `.gpx` to load a route, or export the one you drew. Stop spoofing gives
back the real GPS without quitting.

Six basemaps sit in the control at the top right, and it remembers the one you
picked. Satellite and Streets reach zoom 19. The dark canvas stops at 16, so
switch to Satellite when the pin has to land on a particular building.

## Cutting the cable

One pairing step, with the phone plugged in:

```
python -m pymobiledevice3 lockdown remotepairing --pair
```

No Trust dialog appears, because the handshake rides the lockdown connection the
phone already trusts. Check it took with
`python -m pymobiledevice3 remote browse`, which should list the phone under
`wifi` once you pull the cable.

From then on locspoof finds the phone itself. USB wins whenever the cable is in,
since it comes up faster and no network can disturb it, and Wi-Fi carries it the
rest of the time. The status line names the link that is live.

## Leaving the house

Bonjour is multicast. It dies at the first router and only ever finds a phone on
the same network. Hand it an address instead and the phone is reachable wherever
it routes:

```
python app.py --device-address <phone-ip>
```

Put the phone and the computer on Tailscale and that address is the phone's
tailnet IP. It is also quicker, since it skips the discovery wait: the same phone went
from address to open channel in 0.7s, against 3.9s through Bonjour.

To work the map from the phone as well, serve it on the computer's tailnet
address:

```
python app.py --device-address <phone-ip> --host <computer-ip>
```

Open `http://<computer-ip>:8765/` in Safari on the phone. Both halves now cross
the tailnet, commands out to the phone and the map back from the computer, and
that computer can sit at home with nothing plugged into it.

The two addresses do different jobs. `--device-address` is the phone.
`--host` is the computer the phone dials. Tailscale draws every address it hands
out from `100.64.0.0/10`, so they all look alike; `tailscale ip -4` prints the
computer's and the Tailscale app prints the phone's. Swap them and the bind
fails, and the error lists the addresses the machine actually holds.

Nothing here asks for a password. Loopback was the only thing keeping it to
you, and `--host` trades that for whichever network you name, so name a private
one. `0.0.0.0` and `::` are
refused outright.

Under 640px the panel folds into a sheet that drops away with one tap, because
on a phone the map is the thing you touch.

None of this gets you a phone working alone. The computer still has to run this
program and still has to be reachable. Cutting it out altogether means an
app on the phone driving its own tunnel, which needs macOS to build and is
another project.

## How it works

| File | Does |
|---|---|
| `device.py` | Owns the tunnel and the LocationSimulation channel |
| `route.py` | Polyline geometry, GPX, playback clock |
| `routing.py` | Road routing and place search, with engine fallback |
| `motion.py` | Turns a route into a speed profile |
| `jitter.py` | Correlated drift on the reported fix |
| `wireless.py` | Reaches the phone over the network instead of the cable |
| `server.py` | HTTP API and the status stream |
| `web/index.html` | Leaflet UI |

The chain to the phone:

```
usbmux (Apple driver)
  -> UserspaceRsdTunnel        in-process, no admin needed
    -> DvtProvider             DTX connection over the tunnel
      -> LocationSimulation    .set(lat, lon) / .clear()
```

`LocationSimulation.set()` calls `simulateLocationWithLatitude:longitude:` on
`com.apple.instruments.server.services.LocationSimulation`. That one call does
all of the spoofing. Everything else is plumbing to reach it.

### One process

pymobiledevice3's userspace tunnel runs on PyTCP, whose network stack is a
process-global singleton. One tunnel per process, and asking for a second one
raises. So a single supervisor task owns the tunnel and every caller goes
through `LocationSession`.

### Roads

`routing.py` calls public OSRM instances. OSRM answers with contraction
hierarchies, a preprocessed Dijkstra, which is why a 19-mile query comes back in
well under a second. Running A* here would mean keeping the OSM road graph on
disk, gigabytes for one region, and it would not answer any faster.

Each profile works down a list of engines, because the public demo servers go
down often:

| Profile | First choice | Fallback |
|---|---|---|
| Drive | router.project-osrm.org | routing.openstreetmap.de/routed-car |
| Bike | routing.openstreetmap.de/routed-bike | router.project-osrm.org |
| Walk | routing.openstreetmap.de/routed-foot | router.project-osrm.org |

If all of them fail the UI says so and points at straight-line mode. A button that
silently does nothing would be worse. Planning runs through the local server,
not the browser, so the fallback logic lives in one place.

### Motion

Constant speed is the loudest tell a fix is simulated. Nothing real holds
11.176 m/s for twenty minutes, takes a right angle without slowing, or crosses
forty junctions and waits at none.

`motion.py` builds a speed profile in four passes: a ceiling per sample from
OSRM's segment speeds, braking set by the turn angle at each vertex, stops
placed at real junctions and turns, then a forward pass bounding acceleration
and a backward pass bounding braking.

The fourth pass is the one that matters. Sweeping `v² = u² + 2as` forward bounds
how fast speed can climb, sweeping it backward bounds how late braking can
start, and the smaller of the two at each point is the quickest profile the
physics allows under the ceiling. The phone eases away from a stop and leans
into it. Nothing jumps between speeds.

A stop's floor is not zero. The player moves by `v * dt`, so a true zero would
never arrive at the point it is braking for. The car creeps in at 0.6 m/s and
the standstill is a timer, during which the position does not change at all.
Routes start and end at rest for the same reason. A phone already doing 45 mph
when you press start has not come from anywhere.

Junctions come from OSRM's `intersections`, kept only where three or more
bearings meet, since two is the road bending. Turns come from `maneuver.type`,
minus `new name` and `continue`, which are the road changing name under you.

Seed the plan and the stops fall in the same places every run. Leave it off for
fresh ones.

### Drift

Two things give away a simulated fix even when the coordinates make sense: a
parked phone reporting the same numbers to the byte for thirty seconds, and a
moving one sitting dead on the road centreline. Real receivers do neither.

`jitter.py` is an Ornstein-Uhlenbeck process, the usual model for drift that
wanders and pulls back:

    dx = -(x / tau) dt + sigma * sqrt(2 dt / tau) dW

Fresh noise each tick would look like television static and come apart from a
real trace at a glance. This drifts smoothly, with a measured lag-1 correlation
near 0.9, and because it reverts it never walks off. Worst excursion in 100,000
steps was 15 m.

Spread is about 3 m parked and 1.5 m moving, since a receiver averaging over a
moving baseline reports a tighter fix than one sitting among buildings. The
first offset comes from the settled spread. Start it at zero and the phone lands
perfectly on its opening fix, which is a tell of its own.

Drift is applied to a copy of the true position, so progress along a route stays
exact while the reported fix wanders. A parked pin gets the same treatment,
pushed again once a second with new drift. That loop is gated on the route
player itself, so a route that ends on its own hands control back with nothing
to reset.

### When it drops

The supervisor is a state machine:

```
no_device -> connecting -> ready -> (failure) -> no_device
```

The coordinate you set is kept as what you asked for, separate from what is
currently applied. Unplug the phone mid-session and the API keeps taking
coordinates without complaint; when the phone returns the supervisor re-applies
the last one. Route playback keeps its clock running through a drop, so the
position still matches the time that has passed.

Failures back off from 2s to a 30s ceiling, and USB presence is polled every 2s.
A Wi-Fi session cannot poll usbmux, so it leans on the tunnel's own transport
watcher to notice the link die.

## Limits

- The spoof lasts while this program runs and the phone stays reachable. Quit,
  or reboot the phone, and the real GPS returns.
- Developer Mode stays visible in Settings while it is on.
- Bonjour only returns phones that already hold a pair record on this machine,
  so finding nothing usually means the pairing step has not been done.
- Apps can still catch it: a jump too fast to be travel, an altitude that never
  changes, GPS that disagrees with the Wi-Fi networks in range or with the
  motion sensors. Apple's `isSimulatedBySoftware` flag stays off on this path.

## Licence

Uses pymobiledevice3, which is GPL-3.0-or-later. Distribute this and those terms
apply.
