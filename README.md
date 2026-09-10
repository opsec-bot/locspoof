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

## Run

```
cd C:\Users\tav08\locspoof
python app.py
```

It opens `http://127.0.0.1:8765/`. Press Ctrl+C to stop, which clears the
simulated location and hands the phone back to its real GPS.

Flags: `--port N`, `--no-browser`, `-v` (debug logging, including
pymobiledevice3 internals).

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

**GPX** — import a `.gpx` to load it as a route, or export what you have drawn.

**Stop spoofing** restores the real GPS without quitting.

### Basemaps

Six keyless layers via the control in the top right, and your choice is
remembered: Satellite + labels (default), Satellite, Streets, Topographic,
Dark, and OpenStreetMap. Satellite and Streets go to zoom 19; the Dark canvas
stops at 16, so switch to Satellite when placing a pin on a specific building.

## How it works

Four moving parts:

| File | Responsibility |
|---|---|
| `device.py` | Owns the tunnel and the LocationSimulation channel |
| `route.py` | Polyline geometry, GPX, playback clock |
| `routing.py` | Road routing and place search, with engine fallback |
| `server.py` | HTTP API and SSE status stream, loopback only |
| `web/index.html` | Leaflet UI |

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

- The spoof lasts only while this program is running and the phone is attached.
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
