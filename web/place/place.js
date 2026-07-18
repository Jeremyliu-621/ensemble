// Seating & Aim — the conductor's control surface for MANUAL PLACEMENT.
//
// A top-down map of the room: you (the conductor / wand) stand at the bottom;
// every phone that joined shows as a puck. Drag a puck to where that phone
// really sits, and the server stores its azimuth — the angle the wand's yaw will
// point along later. Tap a puck to conduct just that instrument (works today,
// no wand needed); the pointing "cone" shows how the wand would resolve a yaw to
// the nearest phone. Volume / mute apply to whichever instrument is aimed.
//
// Connects as `admin`, so it receives the enriched roster and can send
// stage.place + admin.cmd (aim / volume / mute). No audio, no clock here.

import { Conn } from "../shared/ws.js";
import * as P from "../shared/protocol.js";

const params = new URLSearchParams(location.search);
const session = params.get("s") || "lol1";
const el = (id) => document.getElementById(id);

// Sprites we have art for; anything else falls back deterministically by index.
const SPRITES = ["violin", "cello", "flute", "trumpet", "harp", "drums", "piano", "synth"];
const spriteFor = (inst, i) =>
  (inst && inst !== "synth" && SPRITES.includes(inst)) ? inst : SPRITES[i % SPRITES.length];

// Must match server/session.py azimuth_from_xy (conductor sits below the map).
const CONDUCTOR_Y = -0.25;
const MAX_GAP_DEG = 35;             // server's yaw→section tolerance (nearest_to_yaw)
const azFromXY = (px, py) => Math.atan2(px, py - CONDUCTOR_Y) * 180 / Math.PI;

let sections = [];                  // latest connected sections from the roster
let aimed = null;                   // section_id being conducted, or null = all
const seeded = new Set();           // ids we've auto-arranged once (so we don't fight the user)
const nodes = new Map();            // section_id -> puck element
let dragging = null;                // {id, moved} while a pointer drag is active

// --- map <-> screen ---------------------------------------------------------
// px ∈ [-1,1] left→right, py ∈ [0,1] near-the-conductor→back-of-stage.
function mapBox() {
  const r = el("mapwrap").getBoundingClientRect();
  return { r, cx: r.width / 2, marginX: r.width / 2 * 0.84, top: r.height * 0.12, bot: r.height * 0.9 };
}
function toScreen(px, py) {
  const b = mapBox();
  return { x: b.cx + px * b.marginX, y: b.bot - py * (b.bot - b.top) };
}
function fromScreen(sx, sy) {
  const b = mapBox();
  const px = Math.max(-1, Math.min(1, (sx - b.cx) / b.marginX));
  const py = Math.max(0, Math.min(1, (b.bot - sy) / (b.bot - b.top)));
  return { px, py };
}

// --- render -----------------------------------------------------------------
function seedPosition(i, n) {
  // Fan the not-yet-placed phones across a forward arc so they start sensible.
  const frac = n <= 1 ? 0.5 : i / (n - 1);
  const ang = (-58 + 116 * frac) * Math.PI / 180;   // -58°..+58° from straight ahead
  const r = 0.72;
  return { px: r * Math.sin(ang), py: r * Math.cos(ang) };
}

function renderRoster(m) {
  sections = (m.sections || []).filter((s) => s.connected);
  el("status").textContent = sections.length
    ? `${sections.length} phone${sections.length > 1 ? "s" : ""} · session ${session}`
    : `session ${session}`;

  // Auto-arrange any brand-new phone once, and push that default placement to the
  // server so it has an azimuth immediately — the user then drags to refine. Use
  // each phone's stable index in the fan so positions don't shift as others join.
  sections.forEach((s, i) => {
    if (s.placed || seeded.has(s.id)) return;
    const pos = seedPosition(i, sections.length);
    seeded.add(s.id);
    s.px = pos.px; s.py = pos.py;
    conn.send({ t: P.STAGE_PLACE, section_id: s.id, px: pos.px, py: pos.py });
  });

  // Drop pucks for sections that left.
  for (const [id, node] of nodes) {
    if (!sections.some((s) => s.id === id)) { node.remove(); nodes.delete(id); }
  }
  sections.forEach((s, i) => upsertPuck(s, i));

  // If the aimed section vanished, fall back to conducting all.
  if (aimed && !sections.some((s) => s.id === aimed)) setAim(null, { silent: true });
  syncControls();
  drawGuides();
  el("hint").hidden = sections.length > 0;
}

function upsertPuck(s, i) {
  let node = nodes.get(s.id);
  if (!node) {
    node = document.createElement("div");
    node.className = "puck";
    node.innerHTML =
      `<div class="disc"><img alt=""></div><div class="name"></div><div class="az"></div>`;
    el("pucks").appendChild(node);
    nodes.set(s.id, node);
    attachDrag(node, s.id);
  }
  node.querySelector("img").src = `../assets/${spriteFor(s.instrument, i)}.png`;
  node.querySelector(".name").textContent = `${s.instrument} · ${s.id}`;
  node.querySelector(".az").textContent = `${Math.round(azFromXY(s.px, s.py))}°`;
  node.classList.toggle("ghost", !s.placed);
  node.classList.toggle("dropped", !s.ready);
  node.classList.toggle("muted", !!s.muted);
  node.classList.toggle("aimed", s.id === aimed);
  if (dragging?.id !== s.id) {   // don't yank a puck the user is actively holding
    const p = toScreen(s.px, s.py);
    node.style.left = p.x + "px";
    node.style.top = p.y + "px";
  }
}

// The pointing beam + tolerance cone from the conductor to the aimed phone.
function drawGuides() {
  const svg = el("guides");
  const b = mapBox();
  const cond = toScreen(0, CONDUCTOR_Y);            // conductor's map anchor (below the floor)
  let out = "";
  // Faint forward arc so the seating fan reads as a stage, not a grid.
  const a0 = toScreen(-0.72 * Math.sin(1.05), 0.72 * Math.cos(1.05));
  const a1 = toScreen(0, 0.75);
  const a2 = toScreen(0.72 * Math.sin(1.05), 0.72 * Math.cos(1.05));
  out += `<path d="M${a0.x} ${a0.y} Q${a1.x} ${a1.y - 30} ${a2.x} ${a2.y}" `
       + `fill="none" stroke="rgba(231,197,131,.14)" stroke-width="1.5"/>`;
  const s = aimed && sections.find((x) => x.id === aimed);
  if (s) {
    const az = azFromXY(s.px, s.py) * Math.PI / 180;
    const gap = MAX_GAP_DEG * Math.PI / 180;
    const reach = b.r.height * 1.15;
    const pt = (a) => ({ x: cond.x + reach * Math.sin(a), y: cond.y - reach * Math.cos(a) });
    const l = pt(az - gap), r = pt(az + gap), tgt = toScreen(s.px, s.py);
    out += `<path d="M${cond.x} ${cond.y} L${l.x} ${l.y} A${reach} ${reach} 0 0 1 ${r.x} ${r.y} Z" `
         + `fill="rgba(231,197,131,.08)"/>`;
    out += `<line x1="${cond.x}" y1="${cond.y}" x2="${tgt.x}" y2="${tgt.y}" `
         + `stroke="#ffe3a3" stroke-width="2" stroke-dasharray="6 5"/>`;
  }
  svg.innerHTML = out;
}

// --- drag + tap -------------------------------------------------------------
function attachDrag(node, id) {
  node.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    node.setPointerCapture(e.pointerId);
    dragging = { id, moved: 0, px0: e.clientX, py0: e.clientY };
  });
  node.addEventListener("pointermove", (e) => {
    if (!dragging || dragging.id !== id) return;
    dragging.moved += Math.abs(e.clientX - dragging.px0) + Math.abs(e.clientY - dragging.py0);
    dragging.px0 = e.clientX; dragging.py0 = e.clientY;
    const p = fromScreen(e.clientX - mapBox().r.left, e.clientY - mapBox().r.top);
    node.style.left = toScreen(p.px, p.py).x + "px";
    node.style.top = toScreen(p.px, p.py).y + "px";
    const s = sections.find((x) => x.id === id);
    if (s) { s.px = p.px; s.py = p.py; s.placed = true; node.classList.remove("ghost"); }
    node.querySelector(".az").textContent = `${Math.round(azFromXY(p.px, p.py))}°`;
    drawGuides();
  });
  node.addEventListener("pointerup", (e) => {
    if (!dragging || dragging.id !== id) return;
    const wasTap = dragging.moved < 6;
    const s = sections.find((x) => x.id === id);
    dragging = null;
    if (wasTap) {
      setAim(aimed === id ? null : id);          // tap toggles conducting this one
    } else if (s) {
      conn.send({ t: P.STAGE_PLACE, section_id: id, px: s.px, py: s.py });  // commit placement
    }
  });
}

// --- aim + mixer ------------------------------------------------------------
function setAim(id, { silent = false } = {}) {
  aimed = id;
  if (!silent) conn.send({ t: P.ADMIN_CMD, cmd: "aim", args: { section_id: id || "all" } });
  for (const [sid, node] of nodes) node.classList.toggle("aimed", sid === aimed);
  syncControls();
  drawGuides();
}

function syncControls() {
  const s = aimed && sections.find((x) => x.id === aimed);
  el("controls").classList.toggle("hidden", !s);
  if (s) {
    el("aimlabel").innerHTML = `Conducting: <b>${s.instrument}</b> <span style="color:var(--gold-dim)">(${s.id}, ${Math.round(azFromXY(s.px, s.py))}°)</span>`;
    el("vol").value = Math.round((s.volume ?? 1) * 100);
    el("mute").textContent = s.muted ? "Unmute" : "Mute";
    el("mute").classList.toggle("on", !!s.muted);
  } else {
    el("aimlabel").innerHTML = `Conducting: <b>the whole orchestra</b>`;
  }
}

el("vol").addEventListener("input", (e) => {
  if (!aimed) return;
  const v = (+e.target.value) / 100;
  const s = sections.find((x) => x.id === aimed); if (s) s.volume = v;
  conn.send({ t: P.ADMIN_CMD, cmd: "volume", args: { section_id: aimed, volume: v } });
});
el("mute").addEventListener("click", () => {
  const s = aimed && sections.find((x) => x.id === aimed); if (!s) return;
  s.muted = !s.muted;
  conn.send({ t: P.ADMIN_CMD, cmd: "mute", args: { section_id: aimed, muted: s.muted } });
  syncControls();
});
el("all").addEventListener("click", () => setAim(null));
window.addEventListener("resize", () => { sections.forEach((s, i) => upsertPuck(s, i)); drawGuides(); });

// --- wire up ----------------------------------------------------------------
const conn = new Conn({ role: "admin", session, key: "place" });
conn.on(P.ROSTER, renderRoster);
conn.onOpen(() => { el("status").classList.add("ok"); });
conn.onClose(() => { el("status").classList.remove("ok"); el("status").textContent = "reconnecting…"; });
conn.connect();
