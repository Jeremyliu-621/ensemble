# Why the physical wand wasn't connecting

Debugging log for getting `firmware/uno_q/wand/` onto the frontend for the
first time. Three unrelated problems stacked on top of each other; each one
made the symptom look like the *next* problem until it was isolated.

## 1. The sketch didn't compile

`firmware/uno_q/wand/sketch/sketch.ino` registered a downlink handler:

```cpp
void onCmd(const String& payload) { ... }
...
Bridge.provide("cmd", onCmd);
```

`Bridge.provide` wraps the callback in `std::function<void(Args...)>` and the
RPC dispatcher default-constructs a `std::tuple<Args...>` to decode into
(`Arduino_RPClite/src/wrapper.h`). A reference-typed `Args` (`const String&`)
can't be default-constructed, so the build failed with a wall of template
errors the moment this handler was added — `stream_probe` never hit this
because it only ever *sends* (`Bridge.notify`), it has no MCU-side downlink.

**Fix:** take the payload by value — `void onCmd(String payload)`.

## 2. The laptop wasn't reachable at all

Once the sketch compiled and the app started, the board logged:

```
wand.link WARNING link down (ConnectionRefusedError); reconnecting
wand.link INFO falling back to gateway: ws://172.19.0.1:8080/ws
```

The laptop was tethered to an iPhone over **USB** Personal Hotspot, which
macOS gives an isolated point-to-point address (`192.0.0.2/255.255.255.255`
on `en0`). The board was joined to the *same* iPhone's **WiFi** hotspot
(`172.20.10.x`). iOS does not bridge the USB-tethered path and the WiFi
hotspot path together — they're separate NAT contexts. No IP we could have
given the board would have worked; the laptop simply wasn't on the board's
network.

**Fix:** connect the laptop's WiFi radio directly to the same network the
board is on (join the hotspot's WiFi, or in this case switch to a normal
router network — `192.168.18.6`). Confirm with:

```
ipconfig getifaddr en0
```

A `192.0.0.2`-style address is the tell — that's USB tethering, not WiFi.

## 3. Auto-discovery and gateway fallback both resolve to the wrong thing

Even on the same WiFi, the board kept falling back to `172.19.0.1` — the
Docker bridge gateway for the container the app runs in
(`phoneharmonic-wand_default`, created fresh on every deploy), not the LAN's
real gateway. `wand_link.py`'s "default gateway" heuristic runs *inside* that
container's network namespace, so it can only ever see Docker's internal
network, never the board's actual WiFi uplink. The UDP discovery beacon
likely has the same problem in the other direction — broadcast packets from
the laptop probably can't reach into the container either.

Manually setting `WAND_LAPTOP_IP=<ip>` on the `ssh ... arduino-app-cli app
start ...` command line looked like the fix (it's the documented override,
and the README explicitly describes setting it before running), but it did
nothing: `arduino-app-cli app start` launches the app in that Docker
container, and headless SSH invocations don't forward the deploying shell's
env into it. (App Lab GUI runs may behave differently — untested.)

`firmware/uno_q/stream_probe/` had already solved the identical problem: it
never relies on env vars at all, it writes a generated `probe_config.json`
into the app's `python/` directory *before* deploying, and reads it at
startup instead.

**Fix:** ported the same pattern to the production wand app —
`firmware/uno_q/wand/python/config.py` now also accepts a `wand_config.json`
(`{"laptop_ip": "..."}`) dropped next to it at deploy time, checked after
`WAND_LAPTOP_IP` but before falling back to auto-discovery. The deploy script
generates this file and scps it alongside the app on every deploy.

## Net result

```
main   INFO    wand connected (variant=hw)
wand   INFO    gesture window: imu, 285 frames, 5091ms
engine INFO    stroke SHAKE -> {...} (intensity target 0.96)
```

## Making this work over a phone hotspot specifically

Problem 2's fix generalizes to a hotspot fine — just join the hotspot over
WiFi (not USB) so the laptop and board share the phone's subnet, then
regenerate `wand_config.json` with whatever IP the laptop gets on that
network (it won't be `192.168.18.6` — get the real one with
`ipconfig getifaddr en0`).

One thing to watch for that's specific to phone hotspots and not present on
a router network: `stream_probe/README.md` notes that iPhone hotspots can
hand out IPv6-only or dual-stack addressing while the board's container
networking ends up IPv4-only, and `stream_probe` carries a
`board_tcp_relay.py` specifically to bridge that mismatch. The production
`wand/` app doesn't have that relay yet — if a hotspot run fails even after
the WiFi-not-USB and `wand_config.json` fixes above, porting that relay is
the next thing to try.
