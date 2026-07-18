"""Generate the Wand Maestro pixel-art UI asset set via the Retro Diffusion API.

Cohesion strategy (this is what makes the set look designed, not random):
  * one MODEL + STYLE for all sprites,
  * one shared prompt SUFFIX,
  * one SEED base (per-asset offset kept stable),
  * remove_bg on every sprite so they composite onto the stage,
  * an optional shared PALETTE image (see `palette` command) locked across assets.

The API token is a SECRET. It's read from $RD_TOKEN or server/tools/rd_token.txt
(both gitignored) — never hardcode it and never commit it.

Usage (run from repo root or server/):
  python server/tools/gen_assets.py credits            # free: show remaining balance
  python server/tools/gen_assets.py cost [names...]    # free dry-run: price a batch
  python server/tools/gen_assets.py list               # show the asset manifest
  python server/tools/gen_assets.py gen NAME [NAME...]  # generate specific assets
  python server/tools/gen_assets.py all                # generate the whole set

Generated PNGs land in web/assets/ (tracked — clients serve them; the token never ships).
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# Windows consoles default to cp1252; force UTF-8 so status glyphs don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

API = "https://api.retrodiffusion.ai/v1"
SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
OUT_DIR = REPO_DIR / "web" / "assets"
TOKEN_FILE = SERVER_DIR / "tools" / "rd_token.txt"

# --- shared aesthetic knobs -------------------------------------------------
# Wand Maestro, GRAND & EXTRAVAGANT: a lavish 2D theater — gilded proscenium,
# deep red velvet curtains that part, dramatic spotlights, a glowing chandelier,
# sparkles, an ornate marquee. Rich detail, warm magical glow. The front-facing
# musicians (kept from the flat pass) perform in a straight row; the stage around
# them is opulent.
MODEL = "rd_plus__default"
SEED = 909
SUFFIX = ("ornate luxurious highly detailed pixel art, dramatic theatrical stage lighting, "
          "gold filigree, deep red velvet, warm magical glow, elegant, beautiful, 16-bit")

# name -> (prompt, width, height, remove_bg, style_override|None, tile)
ASSETS: dict[str, tuple] = {
    # ---- The grand stage (two backdrop variants to pick the best) ----
    "backdrop": ("a breathtaking opulent empty grand theater stage interior seen head on, a "
                 "gilded ornate proscenium, a deep sapphire-blue back wall with a painted golden "
                 "starry mural, warm dramatic spotlights, a polished parquet wood stage floor, "
                 "majestic and luxurious, no people", 448, 256, False, "rd_plus__environment", False),
    "backdrop_b": ("an opulent grand concert hall stage interior seen head on, ornate gold and "
                   "crimson baroque decor, glowing warm chandeliers, rich marble columns, deep red "
                   "and gold, polished stage floor, majestic, cinematic lighting, no people",
                   448, 256, False, "rd_plus__environment", False),

    # ---- Intro curtains: the LEFT half drape (mirrored in CSS for the right) + top valance ----
    "curtain_half": ("an enormous luxurious deep red velvet stage curtain drape covering the LEFT "
                     "half, heavy rich vertical folds, gold braid trim and tassels, a single "
                     "isolated curtain panel, plain background", 256, 256, True, None, False),
    "valance": ("a grand ornate deep red velvet theater curtain valance swag pelmet across the top, "
                "scalloped, heavy gold fringe and tassels, a single isolated object, plain background",
                256, 144, True, None, False),
    "curtain_closed": ("a deep red velvet theater stage curtain fully closed across the whole stage, "
                       "hanging straight down in heavy rich vertical folds from top to bottom, an "
                       "ornate scalloped gold valance across the top and gold fringe along the bottom, "
                       "filling the entire wide frame", 448, 256, False, "rd_plus__environment", False),

    # ---- Lights, sparkle & signage (the extravagance) ----
    "chandelier": ("a magnificent ornate golden crystal chandelier glowing with warm candlelight, "
                   "hanging, sparkling, a single isolated object, plain black background",
                   128, 160, True, None, False),
    "spotlight": ("a soft translucent cone of warm golden stage spotlight beam shining downward, a "
                  "glowing light ray, a single isolated beam on a plain black background",
                  128, 224, True, None, False),
    "sparkle": ("a glowing magical golden sparkle star with light rays, a single isolated object, "
                "plain black background", 64, 64, True, None, False),
    "marquee": ("an ornate golden art-deco theater marquee sign framed with round glowing "
                "lightbulbs, a blank empty dark center panel, a single isolated object, plain "
                "background", 224, 128, True, None, False),

    # ---- Props ----
    "podium": ("an ornate gilded conductor's podium with a music stand and deep red velvet, a "
               "single isolated object, plain background", 128, 128, True, None, False),
    "wand2": ("a magnificent ornate magic conductor's wand with a glowing five-point golden star "
              "tip trailing sparkles, a single isolated object, plain background",
              96, 160, True, None, False),

    # Elegant, formally-dressed musicians — front-facing, full body, placed in a
    # row on the lavish stage (the ornate SUFFIX makes them match the scenery).
    "violin":  ("an elegant orchestra violinist in a black formal tailcoat playing a violin, "
                "standing facing forward, full body head to toe, symmetrical, plain background", 128, 176, True, None, False),
    "cello":   ("an elegant orchestra cellist in a flowing formal gown playing a cello, "
                "standing facing forward, full body head to toe, symmetrical, plain background", 128, 176, True, None, False),
    "flute":   ("an elegant orchestra flutist in formal concert attire playing a flute, "
                "standing facing forward, full body head to toe, symmetrical, plain background", 128, 176, True, None, False),
    "trumpet": ("an elegant orchestra trumpeter in a formal tailcoat playing a trumpet, "
                "standing facing forward, full body head to toe, symmetrical, plain background", 128, 176, True, None, False),
    "drums":   ("an elegant percussionist in formal concert attire at a drum, standing facing "
                "forward, full body, symmetrical, plain background", 128, 176, True, None, False),
    "piano":   ("an elegant pianist in formal concert attire standing at a grand piano keyboard, "
                "facing forward, full body, symmetrical, plain background", 128, 176, True, None, False),
    "harp":    ("an elegant harpist in a flowing formal gown playing a golden harp, standing facing "
                "forward, full body head to toe, symmetrical, plain background", 128, 176, True, None, False),
    "synth":   ("an elegant musician in formal concert attire playing an ornate keyboard, standing "
                "facing forward, full body, symmetrical, plain background", 128, 176, True, None, False),

    # Note VFX (glowing, ornate — matches the sparkle & wand).
    "note": ("a glowing golden ornate musical note with soft sparkles, a single isolated object, "
             "plain black background", 64, 64, True, None, False),
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
    prompt, w, h, rembg, style, tile = ASSETS[name]
    p = {
        "prompt": f"{prompt}, {SUFFIX}",
        "prompt_style": style or MODEL,
        "width": w,
        "height": h,
        "num_images": 1,
        "seed": SEED,
        "remove_bg": rembg,
    }
    if tile:
        p["tile_x"] = True
    if check_cost:
        p["check_cost"] = True
    return p


def cmd_credits() -> None:
    try:
        info = _get("/inferences/credits")
        print(json.dumps(info, indent=2))
    except Exception as e:  # noqa: BLE001
        print(f"credits check failed: {type(e).__name__}: {e}")
        raise


def cmd_cost(names: list[str]) -> None:
    names = names or list(ASSETS)
    total = 0.0
    for n in names:
        try:
            r = _post("/inferences", _payload(n, check_cost=True))
            c = r.get("cost", r.get("balance_cost", 0.0))
            total += c
            print(f"  {n:12s} ${c:.4f}  ({ASSETS[n][1]}x{ASSETS[n][2]}, {ASSETS[n][4] or MODEL})")
        except Exception as e:  # noqa: BLE001
            print(f"  {n:12s} cost check failed: {e}")
    print(f"  {'TOTAL':12s} ${total:.4f}  ({len(names)} assets)")


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
            print(f"  ✓ {n:12s} -> {out.relative_to(REPO_DIR)}  "
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
        for n, (p, w, h, rb, st, tl) in ASSETS.items():
            print(f"  {n:12s} {w}x{h}  {'transparent' if rb else 'opaque'}  {st or MODEL}")
    else:
        sys.exit(f"unknown command {cmd!r}. Try: credits | cost | list | gen NAME | all")


if __name__ == "__main__":
    main()
