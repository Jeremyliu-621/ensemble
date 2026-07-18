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
import math
import os
import socket
import ssl
import time
import uuid

import websockets
from websockets.asyncio.server import ServerConnection, serve

import arranger
import protocol as P
from announcer import Announcer
from clocksync import server_time_ms
from config import (
    CERT_DIR,
    DEFAULT_SESSION,
    DISCONNECT_GRACE_S,
    HTTP_PORT,
    HTTPS_PORT,
    PROTOCOL_VERSION,
    SECTION_GRACE_S,
    SESSION_FILE,
    SONG_CACHE,
    WS_PATH,
)
from engine.candidates import GENERATORS
from engine.conductor import Conductor
from hub import ClientConn, Hub, send_json
from imu_telemetry import ImuTelemetry
from network_address import address_score, format_url_host
from recording.recorder import GestureRecorder
from scheduler import Scheduler
from session import Section, SessionState, WandSlot
from showlog import ShowLog
from static_files import build_static_response
from wandio import WandAimer, WandRouter

PAD_CANDIDATES = list(GENERATORS)   # MPR121 pads 0-5 force these; pad up = auto

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-6s %(levelname)-7s %(message)s")
log = logging.getLogger("main")


def _ip_score(ip: str) -> int:
    """Rank an address by how likely it is reachable by LAN peers."""
    return address_score(ip)


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
        self._load_session()
        self.engine = Conductor()
        self._restore_song()
        self.recorder = GestureRecorder(DEFAULT_SESSION)
        self.wand = WandRouter(self.engine, recorder=self.recorder)
        self.scheduler = Scheduler(self.engine, self.hub)
        self.aimer = WandAimer()
        self.showlog = ShowLog(DEFAULT_SESSION)
        self.announcer = Announcer(self._announce_line)
        self.imu_telemetry = ImuTelemetry()
        self.lan_ip = detect_lan_ip()
        self._wand_client: str | None = None    # who owns the wand slot
        self._last_roster_ms = 0.0              # throttle for health-ping-triggered rosters
        self._last_aim: str | None = None
        self._last_state_ms = 0.0               # wand.state broadcast throttle
        self._wand_cmd_seq = 0                  # wand.cmd downlink counter
        self._tension = 0.0
        self._last_tension_ms = 0.0
        self._last_expr = (0, 1.0)              # deterministic-mode (semis, gain) throttle
        self._last_expr_ms = 0.0
        self._vibe_task: asyncio.Task | None = None

    # --- session persistence + roster hygiene ---
    def _load_session(self) -> None:
        try:
            if SESSION_FILE.exists():
                self.session.restore(json.loads(SESSION_FILE.read_text(encoding="utf-8")))
                log.info("restored %d section slots from %s", len(self.session.sections), SESSION_FILE)
        except (OSError, ValueError, KeyError) as e:
            log.warning("session restore failed (%s) — starting fresh", e)

    def _save_session(self) -> None:
        try:
            SESSION_FILE.write_text(json.dumps(self.session.to_dict(), indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("session save failed: %s", e)

    # --- song persistence: a restart must never silently revert the show to
    # the built-in loop (which also mutes the camera's "what you did" flashes,
    # since only real songs engage the arrangement devices). ---
    def _save_song_cache(self, mid_bytes: bytes | None = None, name: str = "",
                         grid: dict | None = None) -> None:
        try:
            SONG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            if mid_bytes is not None:
                SONG_CACHE.with_suffix(".mid").write_bytes(mid_bytes)
                SONG_CACHE.with_suffix(".meta.json").write_text(
                    json.dumps({"name": name}), encoding="utf-8")
                SONG_CACHE.with_suffix(".grid.json").unlink(missing_ok=True)
            elif grid is not None:
                SONG_CACHE.with_suffix(".grid.json").write_text(
                    json.dumps(grid), encoding="utf-8")
        except OSError as e:
            log.warning("song cache save failed: %s", e)

    def _restore_song(self) -> None:
        """On boot, reload whichever landed last: the dropped MIDI or an edit."""
        try:
            mid, grid = SONG_CACHE.with_suffix(".mid"), SONG_CACHE.with_suffix(".grid.json")
            pick = max((p for p in (mid, grid) if p.exists()),
                       key=lambda p: p.stat().st_mtime, default=None)
            if pick is None:
                return
            if pick == grid:
                from engine.midi_load import build_song_from_grid
                g = json.loads(grid.read_text(encoding="utf-8"))
                song, tracks = build_song_from_grid(g.get("parts") or [],
                                                    float(g.get("bpm") or 100),
                                                    g.get("name") or "edited")
            else:
                from engine.midi_load import load_midi_bytes
                meta = SONG_CACHE.with_suffix(".meta.json")
                nm = (json.loads(meta.read_text(encoding="utf-8")).get("name", "restored.mid")
                      if meta.exists() else "restored.mid")
                song, tracks = load_midi_bytes(mid.read_bytes(), nm)
            self.engine.load_song(song, tracks)
            log.info("restored last song '%s' (%d bars, %d parts)",
                     song.name, len(song.bars), len(tracks))
        except Exception as e:  # noqa: BLE001 - a bad cache must never block boot
            log.warning("song restore failed (%s) — starting with the built-in loop", e)

    async def prune_loop(self) -> None:
        """Drop section slots that have been disconnected past the grace period,
        so private-mode phones minting fresh ids don't clutter the roster forever."""
        while True:
            await asyncio.sleep(30.0)
            now = time.time()
            stale = [sid for sid, s in self.session.sections.items()
                     if not s.connected and s.dropped_at and now - s.dropped_at > DISCONNECT_GRACE_S]
            if not stale:
                continue
            for sid in stale:
                del self.session.sections[sid]
            log.info("pruned %d stale section slot(s): %s", len(stale), stale)
            self._save_session()
            self.engine.on_sections_changed(self.session.engine_sections())
            await self._broadcast_roster()

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
        stale = self.hub.get(client_id)
        if stale is not None and stale.ws is not ws:
            if role in ("stage", "admin"):
                # Two dashboard tabs sharing a persisted id (a duplicated console
                # tab) must NOT evict each other: eviction -> the loser reconnects
                # -> it evicts the winner -> a 1 Hz mutual reconnect storm where
                # each tab's socket lives ~1s and both starve of broadcasts (the
                # "piano roll empty while audio plays" bug). Views hold no
                # server-side state, so just give the newcomer its own identity
                # and let every tab live.
                client_id = uuid.uuid4().hex
                log.info("stage id collision -> new identity %s", client_id[:8])
            else:
                # Sections/wand own a singular slot: a reconnect while the old
                # socket lingers (phone woke before the ping timeout) means the
                # old socket is a zombie — close it. Its disconnect handler
                # no-ops thanks to the identity guard.
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
            # The physical wand outranks the camera: the console's hub camera
            # auto-starts on every page load and would otherwise steal the slot
            # from the board mid-rehearsal (its input then dies SILENTLY). A
            # camera hello while a hardware wand owns the slot leaves it alone.
            holder = self.hub.get(self._wand_client) if self._wand_client else None
            if (role != "wand" and holder is not None and holder.role == "wand"
                    and self._wand_client != client_id):
                log.info("hardware wand keeps the slot; %s (%s) is a bystander",
                         client_id[:8], role)
            else:
                self.session.wand = WandSlot(connected=True, variant=variant)
                self._wand_client = client_id
                self.imu_telemetry.reset()       # never mix diagnostics across wand owners
                self._last_state_ms = 0.0
                self.wand.reset()               # a fresh wand must not inherit a stale grab
                self.showlog.record("wand.connect", variant=variant)
                self.announcer.poke("wand.connect", f"The conductor's wand just came alive ({variant}).")
                log.info("wand connected (variant=%s)", variant)
        elif role in ("stage", "admin"):
            # Phone wand is parked for now, so the console QR means "join as an
            # instrument" — over plain http (no secure context / cert warning
            # needed for audio). `wand_url` is the key the console QR reads.
            # (To re-enable the phone wand later, point this at
            # https://.../wandsim/ on :8443 — that page still exists.)
            http_base = f"http://{format_url_host(self.lan_ip)}:{HTTP_PORT}"
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
        if role in P.WAND_ROLES:
            await self._notify_wand()           # sync the board to current state on connect
        return conn

    def _bind_section(self, conn: ClientConn) -> Section:
        """Reuse an existing (possibly disconnected) section for this client_id,
        else create a fresh one. Either way the phone ends up on an instrument
        the current song actually contains (transport running or not)."""
        insts = self.engine.part_instruments()
        for s in self.session.sections.values():
            if s.client_id == conn.client_id:
                s.connected = True
                s.dropped_at = None
                if insts and s.instrument not in insts:   # song changed while away
                    s.instrument = self.session.deal_instrument(insts)
                    log.info("section %s re-dealt to %s on rejoin", s.section_id, s.instrument)
                log.info("section %s rejoined as %s", s.section_id, conn.client_id[:8])
                return s
        sid = self.session.new_section_id()
        section = Section(section_id=sid, client_id=conn.client_id,
                          instrument=self.session.deal_instrument(insts))
        self.session.sections[sid] = section
        self._save_session()
        log.info("section %s created for %s (instrument=%s)", sid, conn.client_id[:8], section.instrument)
        return section

    async def _message_loop(self, conn: ClientConn) -> None:
        async for raw in conn.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await self._dispatch(conn, msg)
            except websockets.ConnectionClosed:
                raise
            except Exception:  # noqa: BLE001 - one malformed frame must not drop the client
                log.exception("dispatch failed for %r from %s", msg.get("t"), conn.client_id[:8])

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
            # Health pings arrive every 2s per device; don't fan out a full
            # roster (with piano-roll tracks) for each one on venue wifi.
            if server_time_ms() - self._last_roster_ms > 1000.0:
                await self._broadcast_roster()
            return

        if t == P.CV_STATE:
            # Chloe's standalone CV app joins as admin; the console's hub camera
            # (the same recognizer vocabulary) joins as wand-cv. Both are the
            # webcam recognizer — accept both, nobody else.
            if conn.role != "admin" and conn.role not in P.WAND_ROLES:
                await send_json(conn.ws, {"t": P.ERR, "code": "forbidden",
                                          "msg": "admin/wand role required for cv.state"})
                return
            gesture = msg.get("gesture")
            mode = msg.get("mode")
            confidence = msg.get("confidence", 0)
            valid_confidence = (
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and math.isfinite(confidence)
                and 0 <= confidence <= 1
            )
            if (gesture not in (None, *P.CV_GESTURES)
                    or mode not in P.CV_MODES
                    or not valid_confidence):
                await send_json(conn.ws, {"t": P.ERR, "code": "bad_cv_state",
                                          "msg": "invalid CV gesture, mode, or confidence"})
                return

            signature = (gesture, mode)
            previous = conn.extra.get("cv_state_signature")
            conn.extra["cv_state_signature"] = signature
            conn.extra["cv_state"] = {
                "gesture": gesture,
                "mode": mode,
                "confidence": float(confidence),
            }
            if signature != previous:
                log.info("cv state client=%s gesture=%s mode=%s confidence=%.0f%%",
                         conn.client_id[:8], gesture or "NONE", mode, float(confidence) * 100)
            return

        if t == P.SECTION_READY:
            if conn.section_id and conn.section_id in self.session.sections:
                section = self.session.sections[conn.section_id]
                first_ready = not section.ready
                section.ready = True
                if first_ready:
                    self.showlog.record("section.join", section=conn.section_id,
                                        instrument=section.instrument)
                    self.announcer.poke("section.join",
                                        f"Phone section {conn.section_id} joined as {section.instrument} — "
                                        f"{len(self.session.engine_sections())} sections live.")
                await self._sections_changed()
            return

        if t == P.SECTION_LEAVE:
            # Explicit goodbye (the phone's Leave button): free the slot right away,
            # no grace period.
            await self._remove_section(conn.section_id, "left")
            return

        # The performer's other hand: CV-palm / wand hardware may drive transport
        # and SELECT-mode aiming (only these verbs — a rogue wand can pause the
        # show or solo a phone, never load songs or change volumes).
        if (t == P.ADMIN_CMD and conn.role in P.WAND_ROLES
                and msg.get("cmd") in ("start", "stop", "rewind", "forward", "aim")):
            await self._admin(msg.get("cmd"), msg.get("args") or {})
            return

        # Show control: only the stage/editor may drive the show — the join QR
        # is public, so audience phones must not be able to stop or hijack it.
        if (t in (P.ADMIN_CMD, P.SONG_LOAD, P.SONG_HUM, P.SONG_FILE,
                  P.STAGE_ASSIGN, P.STAGE_PLACE, P.STAGE_RECORD)
                and conn.role not in ("stage", "admin")):
            await send_json(conn.ws, {"t": P.ERR, "code": "forbidden", "msg": "controller role required"})
            return

        if t == P.ADMIN_CMD:
            await self._admin(msg.get("cmd"), msg.get("args") or {})
            return

        # Single wand: only the slot owner's input counts. Two cameras can be
        # open at once (the hub iframe + a popped-out tab) — the newest hello
        # owns the slot and the other stream is ignored, instead of the two
        # evicting each other in a reconnect loop. If the owner dropped, the
        # next wand message adopts its sender so the slot self-heals.
        if (conn.role in P.WAND_ROLES
                and t in (P.WAND_IMU, P.WAND_POSE, P.WAND_GRAB, P.WAND_MODE,
                          P.WAND_FEEDBACK, P.WAND_GESTURE, P.WAND_RECAL)):
            if self._wand_client is None:
                self._wand_client = conn.client_id
                self.session.wand = WandSlot(connected=True, variant=P.WAND_VARIANT[conn.role])
                self.wand.reset()
                log.info("wand slot adopted by %s", conn.client_id[:8])
                await self._broadcast_roster()   # the UI must see the wand come alive
            elif conn.client_id != self._wand_client:
                return

        # Wand input -> router (buffers frames per grab, hands the engine a
        # complete gesture window on release). IMU frames also feed aiming.
        if t == P.WAND_IMU:
            frames = self.imu_telemetry.ingest(
                msg.get("seq"), msg.get("frames"), server_time_ms(),
            )
            self.wand.on_imu(frames)
            await self._update_aim(frames)
            return
        if t == P.WAND_POSE:
            self.wand.on_pose(msg.get("frames", []))
            return
        if t == P.WAND_GRAB:
            if self.session.wand.mode != "det":  # det mode: continuous control, no gesture windows
                self.wand.on_grab(msg.get("state", ""), server_time_ms())
            return
        if t == P.WAND_MODE:                    # physical toggle: ai composes / det controls
            mode = "det" if msg.get("mode") == "det" else "ai"
            param = msg.get("param")
            changed = mode != self.session.wand.mode
            if param in ("pitch", "volume", "filter") and param != self.session.wand.det_param:
                self.session.wand.det_param = param
                changed = True
            if changed:
                self.session.wand.mode = mode
                self.wand.reset()               # a mid-grab toggle must not strand a window
                # Any mode/param change releases the previous warp everywhere, so a
                # parameter never sticks after the wand stops controlling it.
                await self.hub.broadcast({"t": P.FX_EXPR, "section": P.SECTION_ALL,
                                          "semis": 0, "gain": 1.0}, roles=("section", "stage"))
                await self.hub.broadcast({"t": P.FX_TENSION, "value": 0.0},
                                         roles=("section", "stage"))
                self.showlog.record("wand.mode", mode=mode, param=self.session.wand.det_param)
                log.info("wand mode -> %s (%s)", mode, self.session.wand.det_param)
                await self._broadcast_roster()
                await self._notify_wand()        # mode change reaches the board
            return
        if t == P.WAND_FEEDBACK:
            self.engine.on_feedback(int(msg.get("value", 0)))
            return
        if t == P.WAND_GESTURE:                 # on-wand TinyML: a pre-classified motion
            self.engine.on_classified(str(msg.get("label", "")),
                                      float(msg.get("strength", 1.0)), server_time_ms())
            return
        if t == P.WAND_TOUCH:                   # MPR121 pads: 0-5 force a candidate
            await self._wand_touch(int(msg.get("pad", -1)), msg.get("state", ""))
            return
        if t == P.WAND_RANGE:                   # ToF distance -> proximity tension
            await self._wand_range(float(msg.get("mm", -1.0)))
            return
        if t == P.WAND_RECAL:
            self.aimer.recal()
            return
        if t == P.STAGE_ASSIGN:
            await self._assign_instrument(msg.get("section_id"), msg.get("instrument"))
            return
        if t == P.STAGE_PLACE:
            await self._place_section(msg.get("section_id"), msg.get("px"), msg.get("py"))
            return
        if t == P.STAGE_RECORD:                 # finished room recording -> ledger
            self.showlog.record("recording", sha256=str(msg.get("sha256", ""))[:64],
                                bytes=int(msg.get("bytes", 0)), dur_s=round(float(msg.get("dur_s", 0)), 1))
            self.showlog.write_manifest()       # refresh so the mint includes the audio hash
            log.info("recording logged: %s (%d bytes)", str(msg.get("sha256", ""))[:16], msg.get("bytes", 0))
            return
        if t == P.SONG_LOAD:
            await self._load_song(conn, msg.get("name", "uploaded"), msg.get("data", ""))
            return
        if t == P.SONG_EDIT:
            await self._apply_edit(conn, msg.get("song") or {})
            return
        if t == P.SONG_HUM:                     # a hummed melody becomes the song
            await self._load_hum(conn, msg.get("frames", []))
            return
        if t == P.SONG_FILE:                    # one-click load from the songs/ folder
            await self._load_song_file(conn, str(msg.get("name", "")))
            return

        log.debug("unhandled message type %r", t)

    async def _admin(self, cmd: str, args: dict) -> None:
        log.info("admin cmd=%s args=%s", cmd, args)
        if cmd in ("start", "clicktest"):
            self.session.playing = True
            # Anchor beat 0 one second out so the first beat has clean lead time.
            self.engine.on_transport(cmd, server_time_ms() + 1000.0)
            self.scheduler.start()
            st = self.engine.status()
            n = len(self.session.engine_sections())
            self.showlog.record("show.start", sections=n, song=st["song"], bpm=st["bpm"])
            self.announcer.poke("show.start",
                                f"The show just started: song '{st['song']}' at {st['bpm']} BPM "
                                f"with {n} phone sections.")
            if self._vibe_task is None or self._vibe_task.done():
                self._vibe_task = asyncio.create_task(self._vibe_loop())
        elif cmd == "stop":
            self.session.playing = False
            self.engine.on_transport("stop", None)
            if self._vibe_task is not None:
                self._vibe_task.cancel()
                self._vibe_task = None
            self.showlog.record("show.stop")
            self.showlog.write_manifest()
            m = self.showlog.manifest()
            self.announcer.poke("show.stop",
                                f"The set just ended after {m['events']} logged moments. "
                                f"Its fingerprint hash is {m['head_hash'][:12]}. Send the crowd off.")
        elif cmd in ("rewind", "forward"):
            self.engine.on_transport(cmd, None)
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
        await self._notify_wand()               # pause/play reaches the board's LED

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
            # Parse off the event loop: a big file must not stall the scheduler
            # tick or clock pongs mid-performance.
            song, tracks = await asyncio.to_thread(load_midi_bytes, data, name)
            self.engine.load_song(song, tracks)
            self._save_song_cache(mid_bytes=data, name=name)
            log.info("song loaded: %s (%d bars, %d parts)", song.name, len(song.bars), len(tracks))
            self.showlog.record("song.load", name=song.name, bars=len(song.bars), parts=len(tracks))
            self.announcer.poke("song.load",
                                f"New song dropped: '{song.name}', {len(song.bars)} bars, "
                                f"{len(tracks)} parts, {song.bpm:.0f} BPM.")
            # Re-deal phones onto the new song's parts (minimal moves, doubling).
            await self._sync_instruments_to_song()
            # The LLM arranger (async, best-effort) regroups parts by musical role.
            ready_ids = [s.section_id for s in self.session.sections.values() if s.connected]
            asyncio.create_task(self._arrange(tracks, ready_ids))
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
            self._save_song_cache(grid={"name": name, "bpm": bpm, "parts": parts})
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
            self._save_session()
            await self._sections_changed()

    async def _arrange(self, tracks: list[dict], section_ids: list[str]) -> None:
        mapping = await arranger.arrange(tracks, section_ids)
        if not mapping:
            return
        self.engine.set_part_assignment(mapping)
        for sid, idxs in mapping.items():        # re-seat each phone on its first part
            section = self.session.sections.get(sid)
            if section and idxs and idxs[0] < len(tracks):
                section.instrument = tracks[idxs[0]]["instrument"]
                c = self.hub.get(section.client_id)
                if c:
                    await send_json(c.ws, {"t": P.SECTION_CONFIG, "section_id": sid,
                                           "instrument": section.instrument})
        self.showlog.record("arrangement", mapping=mapping)
        self.announcer.poke("song.load", "The AI arranger just seated the orchestra: "
                            + ", ".join(f"{sid} takes {len(ix)} part(s)"
                                        for sid, ix in mapping.items()))
        await self._broadcast_roster()

    # --- wand feature surface (aiming, pads, proximity) ---
    def _placements(self) -> dict[str, float]:
        """Azimuth per ready section. Unplaced orchestras get an automatic
        spread across -60..60 by join order, so aiming works out of the box."""
        ready = [s for s in self.session.sections.values() if s.connected and s.ready]
        if not ready:
            return {}
        if any(s.azimuth_deg for s in ready):
            return {s.section_id: s.azimuth_deg for s in ready}
        n = len(ready)
        return {s.section_id: (0.0 if n == 1 else -60.0 + 120.0 * i / (n - 1))
                for i, s in enumerate(ready)}

    _DEG = (0, 2, 4, 5, 7, 9, 11)   # major-scale semitone offsets (expression quantizer)

    async def _expression(self, frames: list, aim: str | None) -> None:
        """Deterministic mode: pure COORDINATE control, no motion involved. The
        wand's tilt (gravity direction — an absolute coordinate, drift-free)
        maps to the selected parameter: pitch = scale-locked degrees (quantized,
        can never sound wrong), volume = a gain sweep, filter = the room-wide
        tension filter. Streamed to the aimed phone; every other phone reads
        the section field and resets to neutral."""
        row = frames[-1] if frames else None
        if not row or len(row) < 3:
            return
        try:
            tilt = max(-1.0, min(1.0, float(row[2]) / 9.8))   # ay = the lift axis
        except (TypeError, ValueError):
            return
        now = server_time_ms()
        param = self.session.wand.det_param
        if param == "filter":                    # raise = open/bright, lower = washed out
            value = round(1.0 - (tilt + 1) / 2, 3)
            if ("filter", value) == self._last_expr or now - self._last_expr_ms < 100.0:
                return
            self._last_expr, self._last_expr_ms = ("filter", value), now
            await self.hub.broadcast({"t": P.FX_TENSION, "value": value},
                                     roles=("section", "stage"))
            return
        if param == "volume":
            semis, gain = 0, round(0.3 + 0.9 * (tilt + 1) / 2, 3)
        else:                                    # pitch
            oct_, step = divmod(round(tilt * 7), 7)
            semis, gain = oct_ * 12 + self._DEG[step], 1.0
        if (param, semis, gain) == self._last_expr or now - self._last_expr_ms < 100.0:
            return
        self._last_expr, self._last_expr_ms = (param, semis, gain), now
        await self.hub.broadcast({"t": P.FX_EXPR, "section": aim or P.SECTION_ALL,
                                  "semis": semis, "gain": gain}, roles=("section", "stage"))

    async def _notify_wand(self) -> None:
        """Reflect show state (pause/play, ai/det mode, selected phone) back to
        the hardware wand so the board can drive its LED/haptics. No-op unless a
        wand owns the slot; hub.send_to guards a dead socket."""
        if self._wand_client is None:
            return
        self._wand_cmd_seq += 1
        await self.hub.send_to(self._wand_client, {
            "t": P.WAND_CMD,
            "playing": self.session.playing,
            "mode": self.session.wand.mode,
            "aim": self._last_aim,
            "seq": self._wand_cmd_seq,
        })

    async def _update_aim(self, frames: list) -> None:
        self.aimer.on_frames(frames)
        aim = self.aimer.resolve(self._placements())
        if self.session.wand.mode == "det":
            await self._expression(frames, aim)
        now = server_time_ms()
        if aim != self._last_aim:
            self._last_aim = aim
            self.engine.on_aim(aim)
            if aim:
                self.showlog.record("wand.aim", section=aim)
            await self._notify_wand()            # selected-phone change reaches the board
        elif now - self._last_state_ms < 150.0:
            return
        self._last_state_ms = now
        await self.hub.broadcast({"t": P.WAND_STATE, "grabbed": self.wand.grabbing,
                                  "aim_section": aim, "yaw_deg": round(self.aimer.yaw, 1),
                                  "imu": self.imu_telemetry.snapshot()},
                                 roles=("stage", "admin"))

    async def _wand_touch(self, pad: int, state: str) -> None:
        if state == "down" and 0 <= pad < len(PAD_CANDIDATES):
            self.engine.set_forced(PAD_CANDIDATES[pad])
            self.showlog.record("wand.touch", pad=pad, forced=PAD_CANDIDATES[pad])
        elif state == "up":
            self.engine.set_forced(None)
        else:
            return  # pads >= len(PAD_CANDIDATES) are reserved for hardware-side modes
        await self._broadcast_roster()

    async def _wand_range(self, mm: float) -> None:
        if mm < 0:
            return
        # 600mm+ away = open; closing to 100mm sweeps the tension to full.
        tension = max(0.0, min(1.0, (600.0 - mm) / 500.0))
        now = server_time_ms()
        if abs(tension - self._tension) < 0.03 or now - self._last_tension_ms < 100.0:
            return
        if round(tension * 4) != round(self._tension * 4):   # ledger at quarter steps only
            self.showlog.record("wand.tension", value=round(tension, 2))
        self._tension = tension
        self._last_tension_ms = now
        await self.hub.broadcast({"t": P.FX_TENSION, "value": round(tension, 3)},
                                 roles=("section", "stage"))

    async def _announce_line(self, text: str, audio_b64: str | None = None) -> None:
        payload = {"t": P.ANNOUNCE, "text": text}
        if audio_b64:
            payload["audio_b64"] = audio_b64
            payload["mime"] = "audio/mpeg"
        await self.hub.broadcast(payload, roles=("stage", "admin"))

    async def _vibe_loop(self) -> None:
        """While the show runs, periodically hand the commentator a vibe digest."""
        while True:
            await asyncio.sleep(90.0)
            if not self.session.playing:
                continue
            st = self.engine.status()
            g = st.get("gesture") or {}
            self.announcer.poke("vibe",
                                f"Mid-set vibe check: '{st['song']}' at {st['bpm']} BPM, the "
                                f"{st.get('decision_source')} brain last chose {st.get('last_choice')}, "
                                f"gesture energy {g.get('energy', 0):.2f}.")

    async def _load_song_file(self, conn: ClientConn, name: str) -> None:
        import pathlib
        from config import REPO_DIR
        path = REPO_DIR / "songs" / pathlib.Path(name).name   # basename only: no traversal
        if path.suffix != ".mid" or not path.exists():
            await send_json(conn.ws, {"t": P.ERR, "code": "bad_song",
                                      "msg": f"no such song: {name}"})
            return
        await self._load_song(conn, path.name, base64.b64encode(path.read_bytes()).decode())

    async def _load_hum(self, conn: ClientConn, frames: list) -> None:
        from engine.hum import song_from_pitches
        song = song_from_pitches(frames, self.engine.bpm)
        if song is None:
            await send_json(conn.ws, {"t": P.ERR, "code": "bad_hum",
                                      "msg": "couldn't hear a melody — hum louder and longer"})
            return
        self.engine.load_song(song, [])
        self.showlog.record("song.hum", bars=len(song.bars), key=song.key_root)
        self.announcer.poke("song.load",
                            "Someone just HUMMED the next melody into the mic and the whole "
                            "orchestra picked it up. React to that.")
        await self._broadcast_roster()
        log.info("hummed song loaded: %d bars, key=%d", len(song.bars), song.key_root)

    async def _assign_instrument(self, section_id: str, instrument: str) -> None:
        section = self.session.sections.get(section_id)
        if not section or not instrument:
            return
        section.instrument = instrument
        self._save_session()
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
            section = self.session.sections[conn.section_id]
            was_ready = section.ready
            section.connected = False
            section.ready = False
            section.dropped_at = time.time()
            self.engine.on_sections_changed(self.session.engine_sections())
            if was_ready:
                self.showlog.record("section.drop", section=conn.section_id)
                self.announcer.poke("section.drop",
                                    f"Section {conn.section_id} dropped off — the orchestra covers.")
            asyncio.create_task(self._reap_later(conn.section_id))
        elif conn.role in P.WAND_ROLES and self._wand_client == conn.client_id:
            self.session.wand = WandSlot()
            self._wand_client = None
            self.wand.reset()                   # never leave a grab open across a drop
        await self._broadcast_roster()

    async def _sections_changed(self) -> None:
        """The one way roster mutations become visible: push the new section
        list into the engine's routing, then broadcast the roster."""
        self.engine.on_sections_changed(self.session.engine_sections())
        await self._broadcast_roster()

    async def _broadcast_roster(self) -> None:
        self._last_roster_ms = server_time_ms()
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
        log.warning("    Wi-Fi as the phones, or start with:  WM_LAN_IP=<reachable-address> python server/main.py")
    log.info("open on this laptop:  http://localhost:%d/", HTTP_PORT)
    url_host = format_url_host(app.lan_ip)
    log.info("stage/admin:  http://%s:%d/stage/?admin=1", url_host, HTTP_PORT)
    log.info("section join: http://%s:%d/section/?s=%s", url_host, HTTP_PORT, DEFAULT_SESSION)
    asyncio.create_task(app.prune_loop())

    # host=None binds all interfaces (IPv4 + IPv6), so both localhost (::1 on
    # Windows) and LAN addresses from either family reach the server.
    # max_size: default 1MiB closes the socket on a big base64 MIDI upload (1009).
    max_frame = 16 * 2**20
    async with serve(app.handler, None, HTTP_PORT, process_request=app.process_request,
                     max_size=max_frame):
        log.info("HTTP/ws  listening on :%d", HTTP_PORT)
        if ssl_ctx is not None:
            async with serve(app.handler, None, HTTPS_PORT,
                             process_request=app.process_request, ssl=ssl_ctx,
                             max_size=max_frame):
                log.info("HTTPS/wss listening on :%d  (wand-sim: https://%s:%d/wandsim/)",
                         HTTPS_PORT, url_host, HTTPS_PORT)
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
