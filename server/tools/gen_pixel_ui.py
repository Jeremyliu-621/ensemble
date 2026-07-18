"""Cozy pixel-art UI asset set for the Maestro Console (Retro Diffusion API).

Aesthetic: the warm "cozy room" console mock — cream paper UI, top-down wooden
room as the playground, chunky pixel icons per instrument, a wizard mascot.
Distinct from the opulent stagepix set (SEED 909): this one is soft, warm,
daylight, SNES-cozy.

Cohesion strategy (same as gen_assets.py): one MODEL, one SUFFIX, one SEED.
Token comes from $RD_TOKEN or server/tools/rd_token.txt (gitignored).

Usage:
  python server/tools/gen_pixel_ui.py credits
  python server/tools/gen_pixel_ui.py cost [names...]
  python server/tools/gen_pixel_ui.py list
  python server/tools/gen_pixel_ui.py gen NAME [NAME...]
  python server/tools/gen_pixel_ui.py all

PNGs land in web/assets/pixel/ (tracked; the token never ships).
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

API = "https://api.retrodiffusion.ai/v1"
SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
OUT_DIR = REPO_DIR / "web" / "assets" / "pixel"
TOKEN_FILE = SERVER_DIR / "tools" / "rd_token.txt"

MODEL = "rd_plus__default"
SEED = 777
SUFFIX = ("cozy warm pixel art, 16-bit SNES style, warm cream and soft brown palette, "
          "gentle daylight, charming, clean readable shapes, soft shading")

ICON_SUFFIX = "video game inventory item icon, single isolated object, centered, plain background"

# name -> (prompt, width, height, remove_bg, style_override|None)
ASSETS: dict[str, tuple] = {
    # ---- the playground floor: a cozy top-down room. Two variants, pick one. ----
    # (rd_plus__environment forces perspective — stay on default and ask for
    #  orthographic top-down; keep the RUG plain so cards/hub read on top of it.
    #  NOTE: rd_plus__default 400s ("inference_failed") at 512x384 just like it
    #  did at 448x256 — big canvases fail; pixel art upscales losslessly via
    #  image-rendering: pixelated, so generate small and let CSS scale.)
    "room_bg": ("orthographic top-down view of a cozy warm living room interior seen "
                "directly from above, light wooden plank floor, one large plain beige "
                "rug covering the center, a green sofa along the bottom edge, a wooden "
                "bookshelf and leafy potted plants along the walls, small round side "
                "tables, video game interior room map, flat top-down, no people",
                384, 288, False, None),
    "room_bg_b": ("orthographic top-down view of a warm cozy music room seen directly "
                  "from above, light wooden plank floor, a large plain round beige rug "
                  "in the center, a brown couch, leafy potted plants in the corners, a "
                  "wooden cabinet, warm afternoon light, video game interior room map, "
                  "flat top-down, no people",
                  256, 192, False, None),

    # ---- hub + mascots ----
    "hub_laptop": ("an open silver laptop computer sitting on a small wooden desk, front "
                   "view, the screen glowing with colorful music equalizer bars, "
                   "single isolated object, centered, plain background",
                   128, 112, True, None),
    "wizard": ("a tiny cute friendly wizard with a purple robe, a purple pointed hat and "
               "a white beard, holding up a small glowing magic wand, standing, full "
               "body, single isolated character, plain background",
               96, 112, True, None),
    # (first try came out as a thin vertical stick — force a diagonal composition
    #  and a big glowing tip so it reads at 56px in the wand panel)
    "wand_wood": ("a magic wand made of twisted dark brown wood pointing diagonally toward "
                  "the top right corner, a large bright glowing green orb at its tip "
                  "surrounded by green sparkles and glow, dynamic diagonal composition, "
                  "single isolated object, plain background",
                  112, 144, True, None),
    "logo_wand": ("a small golden magic wand with a bright yellow five-point star tip and "
                  "tiny sparkles, video game icon, single isolated object, plain background",
                  64, 64, True, None),

    # ---- instrument icons (every name the engine can emit) ----
    "icon_drums":    ("a red and white snare drum with two crossed wooden drumsticks, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_piano":    ("a small black and white piano keyboard, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_bass":     ("a brown electric bass guitar, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_violin":   ("a glossy brown violin with a bow, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_cello":    ("a large brown cello standing upright, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_viola":    ("a dark amber viola with a bow, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_flute":    ("a silver flute held diagonally, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_clarinet": ("a black clarinet with silver keys standing upright, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_trumpet":  ("a shiny golden trumpet, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_harp":     ("a small golden harp, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_bell":     ("a golden hand bell with a wooden handle, " + ICON_SUFFIX, 64, 64, True, None),
    "icon_synth":    ("a small electronic synthesizer keyboard with colorful knobs, " + ICON_SUFFIX, 64, 64, True, None),
}


def token() -> str:
    tok = os.environ.get("RD_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    sys.exit(f"No token. Set $RD_TOKEN or write it to {TOKEN_FILE}")


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers={"X-RD-Token": token(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:400]}") from None


def _get(path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"X-RD-Token": token()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _payload(name: str, check_cost: bool = False) -> dict:
    prompt, w, h, rembg, style = ASSETS[name]
    p = {
        "prompt": f"{prompt}, {SUFFIX}",
        "prompt_style": style or MODEL,
        "width": w,
        "height": h,
        "num_images": 1,
        "seed": SEED,
        "remove_bg": rembg,
    }
    if check_cost:
        p["check_cost"] = True
    return p


def cmd_credits() -> None:
    print(json.dumps(_get("/inferences/credits"), indent=2))


def cmd_cost(names: list[str]) -> None:
    names = names or list(ASSETS)
    total = 0.0
    for n in names:
        try:
            r = _post("/inferences", _payload(n, check_cost=True))
            c = r.get("cost", r.get("balance_cost", 0.0))
            total += c
            print(f"  {n:14s} ${c:.4f}  ({ASSETS[n][1]}x{ASSETS[n][2]})")
        except Exception as e:  # noqa: BLE001
            print(f"  {n:14s} cost check failed: {e}")
    print(f"  {'TOTAL':14s} ${total:.4f}  ({len(names)} assets)")


def cmd_gen(names: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    for n in names:
        if n not in ASSETS:
            print(f"  ! unknown asset {n!r} (see `list`)")
            continue
        try:
            r = _post("/inferences", _payload(n))
            imgs = r.get("base64_images") or []
            if not imgs:
                print(f"  ! {n}: no image in response: {r}")
                continue
            out = OUT_DIR / f"{n}.png"
            out.write_bytes(base64.b64decode(imgs[0]))
            spent += r.get("balance_cost", 0.0)
            print(f"  ✓ {n:14s} -> {out.relative_to(REPO_DIR)}  "
                  f"(${r.get('balance_cost', 0):.4f}, bal ${r.get('remaining_balance', 0):.2f})")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {n}: {type(e).__name__}: {e}")
    print(f"  spent ${spent:.4f} this run")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    rest = args[1:]
    if cmd == "credits":
        cmd_credits()
    elif cmd == "cost":
        cmd_cost(rest)
    elif cmd == "gen":
        if not rest:
            sys.exit("gen needs at least one asset name (see `list`)")
        cmd_gen(rest)
    elif cmd == "all":
        cmd_gen(list(ASSETS))
    elif cmd == "list":
        for n, (p, w, h, rb, st) in ASSETS.items():
            print(f"  {n:14s} {w}x{h}  {'transparent' if rb else 'opaque'}")
    else:
        sys.exit(f"unknown command {cmd!r}. Try: credits | cost | list | gen NAME | all")


if __name__ == "__main__":
    main()
