"""Wand Maestro server entrypoint.

One asyncio process runs two listeners that share a handler:
  :8080  plain  http + ws   (section pages on any phone, ESP32 wand)
  :8443  TLS    https + wss  (wand-sim page — DeviceMotion needs a secure context)

Each listener serves the web/ directory as static files (via process_request)
and upgrades WS_PATH to a WebSocket. A single Hub, SessionState, engine, and
Scheduler are shared across both ports.

Run:  python server/main.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import ssl
import uuid

import websockets
from websockets.asyncio.server import ServerConnection, serve

import protocol as P
from clocksync import server_time_ms
from config import (
    CERT_DIR,
    DEFAULT_SESSION,
    HTTP_PORT,
    HTTPS_PORT,
    PROTOCOL_VERSION,
    SECTION_GRACE_S,
    WS_PATH,
)
from engine.conductor import Conductor
from hub import ClientConn, Hub, send_json
from recording.recorder import GestureRecorder
from scheduler import Scheduler
from session import Section, SessionState, WandSlot
from static_files import build_static_response
from wandio import WandRouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-6s %(levelname)-7s %(message)s")
log = logging.getLogger("main")


def _ip_score(ip: str) -> int:
    """Rank an IPv4 by how likely it is the real Wi-Fi/LAN address a phone on the
    same network can reach. Home Wi-Fi (192.168) beats corporate (10), both beat
    the Docker/WSL bridges (172.17/18) and Tailscale/CGNAT (100.64/10)."""
    p = ip.split(".")
    if len(p) != 4 or not all(x.isdigit() for x in p):
        return -100
    a, b = int(p[0]), int(p[1])
    if ip.startswith("169.254."):     # link-local (no DHCP lease) — dead
        return -60
    if ip.startswith("127."):
        return -55
    if a == 172 and b in (17, 18):    # Docker / WSL bridge — never LAN-reachable
        return -50
    if a == 192 and b == 168:
        return 100
    if a == 10:
        return 80
    if a == 172 and 16 <= b <= 31:
        return 60
    if a == 100 and 64 <= b <= 127:   # CGNAT/hotspot/Tailscale — often the real Wi-Fi
        return 40                     # ...so prefer it over Docker, below real LANs
    return 20                         # a public/other IP: usable


def detect_lan_ip() -> str:
    """Local Wi-Fi/LAN IP for the QR. Override with WM_LAN_IP if auto-detection
    picks the wrong interface (multi-homed machines are ambiguous)."""
    override = os.environ.get("WM_LAN_IP")
    if override:
        return override
    candidates: list[str] = []
    try:
        candidates += socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        candidates.append(s.getsockname()[0])
    except Exception:
        pass
    finally:
        s.close()
    candidates = list(dict.fromkeys(candidates))  # dedupe, keep order
    return max(candidates, key=_ip_score) if candidates else "127.0.0.1"


class App:
    def __init__(self) -> None:
        self.hub = Hub()
        self.session = SessionState(name=DEFAULT_SESSION)
        self.engine = Conductor()
        self.recorder = GestureRecorder(DEFAULT_SESSION)
        self.wand = WandRouter(self.engine, recorder=self.recorder)
        self.scheduler = Scheduler(self.engine, self.hub)
        self.lan_ip = detect_lan_ip()

    # --- static + WS routing ---
    def process_request(self, connection: ServerConnection, request):
        # Returning None lets the WebSocket handshake proceed; a Response serves HTTP.
        if request.path.split("?", 1)[0].rstrip("/") in (WS_PATH, WS_PATH.rstrip("/")):
            return None
        if request.path == "/" or not request.path.startswith(WS_PATH):
            return build_static_response(request.path)
        return None

    # --- connection lifecycle ---
    async def handler(self, ws: ServerConnection) -> None:
        conn: ClientConn | None = None
        try:
            raw = await ws.recv()
            hello = json.loads(raw)
            if hello.get("t") != P.HELLO:
                await send_json(ws, {"t": P.ERR, "code": "expected_hello", "msg": "first frame must be hello"})
                return
            if hello.get("v") != PROTOCOL_VERSION:
                await send_json(ws, {"t": P.ERR, "code": "bad_version",
                                     "msg": f"server speaks v{PROTOCOL_VERSION}"})
                return
            role = hello.get("role")
            if role not in P.ROLES:
                await send_json(ws, {"t": P.ERR, "code": "bad_role", "msg": f"unknown role {role!r}"})
                return

            conn = await self._on_hello(ws, hello, role)
            await self._message_loop(conn)
        except (websockets.ConnectionClosed, json.JSONDecodeError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("handler error")
        finally:
            if conn is not None:
                await self._on_disconnect(conn)

    async def _on_hello(self, ws: ServerConnection, hello: dict, role: str) -> ClientConn:
        client_id = hello.get("client_id") or uuid.uuid4().hex
        # A reconnect while the previous socket is still lingering (phone woke up
        # before the server's ping timeout noticed the old one died): close the
        # zombie now. Its disconnect handler no-ops thanks to the identity guard.
        stale = self.hub.get(client_id)
        if stale is not None and stale.ws is not ws:
            log.info("closing stale socket for %s (reconnect)", client_id[:8])
            asyncio.create_task(self._close_quietly(stale.ws))
        conn = ClientConn(client_id=client_id, role=role, ws=ws, name=hello.get("name", ""))
        self.hub.register(conn)

        config: dict = {"session": self.session.name}

        if role in ("section",):
            section = self._bind_section(conn)
            conn.section_id = section.section_id
            config.update(section_id=section.section_id, instrument=section.instrument)
        elif role in P.WAND_ROLES:
            variant = P.WAND_VARIANT[role]
            self.session.wand = WandSlot(connected=True, variant=variant)
            log.info("wand connected (variant=%s)", variant)
        elif role in ("stage", "admin"):
            # Phone wand is parked for now, so the console QR means "join as an
            # instrument" — over plain http (no secure context / cert warning
            # needed for audio). `wand_url` is the key the console QR reads.
            # (To re-enable the phone wand later, point this at
            # https://.../wandsim/ on :8443 — that page still exists.)
            http_base = f"http://{self.lan_ip}:{HTTP_PORT}"
            config["wand_url"] = f"{http_base}/section/?s={self.session.name}"
            config["join_url"] = f"{http_base}/section/?s={self.session.name}"
            config["cv_url"] = f"{http_base}/cvwand/"
            config["lan_ip"] = self.lan_ip

        await send_json(ws, {
            "t": P.WELCOME,
            "v": PROTOCOL_VERSION,
            "client_id": client_id,
            "role": role,
            "server_time": server_time_ms(),
            "config": config,
        })
        # Keep the engine's routing in lockstep with the roster from the very
        # first hello (not just from SECTION_READY) — same single path as every
        # other roster mutation.
        await self._sections_changed()
        return conn

    def _bind_section(self, conn: ClientConn) -> Section:
        """Reuse an existing (possibly disconnected) section for this client_id,
        else create a fresh one. Either way the phone ends up on an instrument
        the current song actually contains (transport running or not)."""
        insts = self.engine.part_instruments()
        for s in self.session.sections.values():
            if s.client_id == conn.client_id:
                s.connected = True
                if insts and s.instrument not in insts:   # song changed while away
                    s.instrument = self.session.deal_instrument(insts)
                    log.info("section %s re-dealt to %s on rejoin", s.section_id, s.instrument)
                log.info("section %s rejoined as %s", s.section_id, conn.client_id[:8])
                return s
        sid = self.session.new_section_id()
        section = Section(section_id=sid, client_id=conn.client_id,
                          instrument=self.session.deal_instrument(insts))
        self.session.sections[sid] = section
        log.info("section %s created for %s (instrument=%s)", sid, conn.client_id[:8], section.instrument)
        return section

    async def _message_loop(self, conn: ClientConn) -> None:
        async for raw in conn.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._dispatch(conn, msg)

    async def _dispatch(self, conn: ClientConn, msg: dict) -> None:
        t = msg.get("t")

        # Clock ping: answer INLINE and immediately, before anything else, so
        # server-side processing jitter stays out of the sync path.
        if t == P.CLOCK_PING:
            await send_json(conn.ws, {"t": P.CLOCK_PONG, "id": msg.get("id"),
                                      "t0": msg.get("t0"), "ts": server_time_ms()})
            return

        if t == P.CLOCK_REPORT:
            conn.theta = msg.get("theta")
            conn.rtt = msg.get("rtt")
            await self._broadcast_roster()
            return

        if t == P.SECTION_READY:
            if conn.section_id and conn.section_id in self.session.sections:
                self.session.sections[conn.section_id].ready = True
                await self._sections_changed()
            return

        if t == P.SECTION_LEAVE:
            # Explicit goodbye (the phone's Leave button): free the slot right away,
            # no grace period.
            await self._remove_section(conn.section_id, "left")
            return

        if t == P.ADMIN_CMD:
            await self._admin(msg.get("cmd"), msg.get("args") or {})
            return

        # Wand input -> router (buffers frames per grab, hands the engine a
        # complete gesture window on release).
        if t == P.WAND_IMU:
            self.wand.on_imu(msg.get("frames", []))
            return
        if t == P.WAND_POSE:
            self.wand.on_pose(msg.get("frames", []))
            return
        if t == P.WAND_GRAB:
            self.wand.on_grab(msg.get("state", ""), server_time_ms())
            return
        if t == P.WAND_FEEDBACK:
            self.engine.on_feedback(int(msg.get("value", 0)))
            return
        if t == P.STAGE_ASSIGN:
            await self._assign_instrument(msg.get("section_id"), msg.get("instrument"))
            return
        if t == P.SONG_LOAD:
            await self._load_song(conn, msg.get("name", "uploaded"), msg.get("data", ""))
            return
        if t == P.SONG_EDIT:
            await self._apply_edit(conn, msg.get("song") or {})
            return
        if t == P.STAGE_PLACE:
            await self._place_section(msg.get("section_id"), msg.get("px"), msg.get("py"))
            return
        if t == P.WAND_RECAL:
            return  # yaw recal wired in P5

        log.debug("unhandled message type %r", t)

    async def _admin(self, cmd: str, args: dict) -> None:
        log.info("admin cmd=%s args=%s", cmd, args)
        if cmd in ("start", "clicktest"):
            self.session.playing = True
            # Anchor beat 0 one second out so the first beat has clean lead time.
            self.engine.on_transport(cmd, server_time_ms() + 1000.0)
            self.scheduler.start()
        elif cmd == "stop":
            self.session.playing = False
            self.engine.on_transport("stop", None)
        elif cmd == "allnotesoff":
            self.engine.on_transport("allnotesoff", None)
        elif cmd == "tempo":
            self.engine.set_tempo(float(args.get("bpm", 100)))
        elif cmd == "force":
            self.engine.set_forced(args.get("candidate"))
        elif cmd == "aim":
            sid = args.get("section_id")
            self.engine.on_aim(sid if sid and sid != "all" else None)
        elif cmd == "volume":
            sec = self.session.sections.get(args.get("section_id"))
            if sec:
                sec.volume = max(0.0, min(1.0, float(args.get("volume", 1.0))))
                self.engine.on_sections_changed(self.session.engine_sections())
        elif cmd == "mute":
            sec = self.session.sections.get(args.get("section_id"))
            if sec:
                sec.muted = bool(args.get("muted"))
                self.engine.on_sections_changed(self.session.engine_sections())
        elif cmd == "record":
            if args.get("action") == "start":
                self.recorder.start(args.get("label", ""))
            else:
                self.recorder.stop()
        await self._broadcast_roster()

    async def _sync_instruments_to_song(self) -> None:
        """After ANY song change (MIDI drop or live edit): re-align phones to the
        new parts (minimal moves — see session.reconcile_instruments), tell each
        changed phone, and refresh routing. Never consults the transport state:
        matching works the same whether the song has started or not."""
        for section in self.session.reconcile_instruments(self.engine.part_instruments()):
            log.info("section %s re-dealt to %s (song change)", section.section_id, section.instrument)
            c = self.hub.get(section.client_id)
            if c:
                await send_json(c.ws, {"t": P.SECTION_CONFIG, "section_id": section.section_id,
                                       "instrument": section.instrument})
        await self._sections_changed()

    async def _load_song(self, conn: ClientConn, name: str, b64: str) -> None:
        from engine.midi_load import load_midi_bytes
        try:
            data = base64.b64decode(b64)
            song, tracks = load_midi_bytes(data, name)
            self.engine.load_song(song, tracks)
            log.info("song loaded: %s (%d bars, %d parts)", song.name, len(song.bars), len(tracks))
            await self._sync_instruments_to_song()
        except Exception as e:  # noqa: BLE001 - report parse failures to the uploader
            log.warning("midi load failed for %r: %s", name, e)
            await send_json(conn.ws, {"t": P.ERR, "code": "bad_midi", "msg": str(e)})

    async def _apply_edit(self, conn: ClientConn, song_json: dict) -> None:
        """Editor pushed hand-edited notes: rebuild the Song and swap it in WITHOUT
        restarting playback (reanchor=False) so an edit lands on the next bar."""
        from engine.midi_load import build_song_from_grid
        try:
            parts = song_json.get("parts") or []
            bpm = float(song_json.get("bpm") or self.engine.bpm)
            name = song_json.get("name") or "edited"
            song, tracks = build_song_from_grid(parts, bpm, name)
            self.engine.update_song(song, tracks, reanchor=False)
            log.info("song edited: %s (%d bars, %d parts)", song.name, len(song.bars), len(tracks))
            await self._sync_instruments_to_song()
        except Exception as e:  # noqa: BLE001 - report bad edits back to the editor
            log.warning("song edit failed: %s", e)
            await send_json(conn.ws, {"t": P.ERR, "code": "bad_edit", "msg": str(e)})

    async def _place_section(self, section_id: str, px, py) -> None:
        """User dragged a phone onto the seating map. Store its spot + azimuth so
        the wand's yaw can later point at it, and refresh the stage."""
        if section_id is None or px is None or py is None:
            return
        sec = self.session.place_section(section_id, px, py)
        if sec:
            log.info("placed %s at (%.2f, %.2f) -> azimuth %.1f", section_id, sec.px, sec.py, sec.azimuth_deg)
            await self._sections_changed()

    async def _assign_instrument(self, section_id: str, instrument: str) -> None:
        section = self.session.sections.get(section_id)
        if not section or not instrument:
            return
        section.instrument = instrument
        conn = self.hub.get(section.client_id)
        if conn:
            await send_json(conn.ws, {"t": P.SECTION_CONFIG, "section_id": section_id,
                                      "instrument": instrument})
        await self._sections_changed()

    async def _remove_section(self, section_id: str | None, why: str) -> None:
        """Delete a section from the roster (explicit leave, or grace expired).
        Deliberately NO instrument rebalance here: swapping a surviving phone's
        timbre mid-performance is audible. The engine's index fallback keeps the
        orphaned part sounding, and the next joiner is dealt the least-covered
        part — coverage self-heals without disturbing anyone."""
        if not section_id or section_id not in self.session.sections:
            return
        sec = self.session.sections.pop(section_id)
        log.info("section %s removed (%s, was %s)", section_id, why, sec.instrument)
        await self._sections_changed()

    async def _reap_later(self, section_id: str) -> None:
        """After the grace period, drop the section if its phone never came back."""
        await asyncio.sleep(SECTION_GRACE_S)
        sec = self.session.sections.get(section_id)
        if sec and not sec.connected:
            await self._remove_section(section_id, f"grace {SECTION_GRACE_S:.0f}s expired")

    @staticmethod
    async def _close_quietly(ws: ServerConnection) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def _on_disconnect(self, conn: ClientConn) -> None:
        # Identity guard: if this client_id already reconnected on a NEWER socket,
        # this is just the zombie dying — touching the roster/section state here
        # would silence the live connection (the "one phone stops playing" bug).
        if self.hub.get(conn.client_id) is not conn:
            log.info("stale socket for %s closed; newer connection lives on", conn.client_id[:8])
            return
        self.hub.unregister(conn.client_id, conn)
        if conn.role == "section" and conn.section_id in self.session.sections:
            # Keep the slot (instrument/placement) for a grace period so a
            # screen-off phone rebinds as itself; reap it if it never returns.
            self.session.sections[conn.section_id].connected = False
            self.session.sections[conn.section_id].ready = False
            self.engine.on_sections_changed(self.session.engine_sections())
            asyncio.create_task(self._reap_later(conn.section_id))
        elif conn.role in P.WAND_ROLES:
            self.session.wand = WandSlot()
        await self._broadcast_roster()

    async def _sections_changed(self) -> None:
        """The one way roster mutations become visible: push the new section
        list into the engine's routing, then broadcast the roster."""
        self.engine.on_sections_changed(self.session.engine_sections())
        await self._broadcast_roster()

    async def _broadcast_roster(self) -> None:
        payload = {"t": P.ROSTER, **self.session.roster_payload()}
        # Enrich each section entry with its connected client's clock estimate,
        # so the stage can show per-section offset and compute the spread.
        for entry in payload["sections"]:
            section = self.session.sections.get(entry["id"])
            conn = self.hub.get(section.client_id) if section else None
            entry["theta"] = conn.theta if conn else None
            entry["rtt"] = conn.rtt if conn else None
        # Engine state for the editor (tempo, manual override, current candidate).
        payload["engine"] = self.engine.status()
        payload["record"] = self.recorder.status()
        await self.hub.broadcast(payload, roles=("stage", "admin"))


def build_ssl_context() -> ssl.SSLContext | None:
    """Load mkcert-issued cert/key from certs/ if present, else None (no HTTPS)."""
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    if not (cert.exists() and key.exists()):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


async def main() -> None:
    app = App()
    ssl_ctx = build_ssl_context()

    log.info("LAN IP detected: %s", app.lan_ip)
    if _ip_score(app.lan_ip) <= 0:
        log.warning("  ^ that looks like a virtual/VPN interface (Docker/WSL/Tailscale), which")
        log.warning("    phones on your Wi-Fi CANNOT reach. Connect this machine to the same")
        log.warning("    Wi-Fi as the phones, or start with:  WM_LAN_IP=192.168.x.x python server/main.py")
    log.info("open on this laptop:  http://localhost:%d/", HTTP_PORT)

    # host=None binds all interfaces (IPv4 + IPv6), so both localhost (::1 on
    # Windows) and the LAN IPv4 reach the server.
    async with serve(app.handler, None, HTTP_PORT, process_request=app.process_request):
        log.info("HTTP/ws  listening on :%d", HTTP_PORT)
        if ssl_ctx is not None:
            async with serve(app.handler, None, HTTPS_PORT,
                             process_request=app.process_request, ssl=ssl_ctx):
                log.info("HTTPS/wss listening on :%d  (wand-sim: https://%s:%d/wandsim/)",
                         HTTPS_PORT, app.lan_ip, HTTPS_PORT)
                await asyncio.Future()
        else:
            log.warning("no certs in %s -> HTTPS/:%d disabled. "
                        "Run mkcert (see README) to enable the wand-sim page.", CERT_DIR, HTTPS_PORT)
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
