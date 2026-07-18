"""Mint the finished set as a Solana compressed NFT via Crossmint.

Reads the newest show manifest (the hash-chained ledger head commits to the
entire performance — see server/showlog.py) and mints a cNFT whose metadata
carries that fingerprint plus set stats. Uses Crossmint's staging environment
(Solana devnet) by default; --production only with a funded account.
Helius's mint API is deprecated — do not switch to it.

Setup: create a collection in the Crossmint staging console, get an API key
with the nfts.create scope, then:
  export CROSSMINT_API_KEY=...
  python server/tools/mint_cnft.py --collection <collection-id> \\
      --recipient email:you@example.com:solana

Without CROSSMINT_API_KEY set this dry-runs: it prints the exact request it
would send. Minted NFTs take ~10-30s to appear in wallets/explorers.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_SHOWS = REPO / "server" / "data" / "shows"


def latest_manifest(shows_dir: pathlib.Path) -> pathlib.Path | None:
    files = sorted(shows_dir.glob("*.manifest.json"))
    return files[-1] if files else None


def build_payload(manifest: dict, recipient: str, image: str) -> dict:
    started = manifest.get("started")
    attrs = [
        {"trait_type": "head_hash", "value": manifest["head_hash"]},
        {"trait_type": "events", "value": str(manifest["events"])},
        {"trait_type": "session", "value": manifest["session"]},
    ]
    for kind, count in sorted((manifest.get("kinds") or {}).items()):
        attrs.append({"trait_type": kind, "value": str(count)})
    return {
        "recipient": recipient,
        "compressed": True,
        "metadata": {
            "name": "Wand Maestro — a conducted set",
            "description": ("A live, wand-conducted phone-orchestra performance. The head hash "
                            f"{manifest['head_hash'][:16]}… commits to the full hash-chained event "
                            "ledger of this unrepeatable set."),
            "image": image,
            "attributes": attrs,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--collection", default="<collection-id>")
    ap.add_argument("--recipient", default="email:demo@example.com:solana")
    ap.add_argument("--manifest", default=None, help="path to a .manifest.json (default: newest)")
    ap.add_argument("--image", default="https://placehold.co/600x600/1a0f0a/e7c583/png?text=Wand+Maestro",
                    help="artwork URL for the NFT (swap in your own)")
    ap.add_argument("--shows-dir", default=str(DEFAULT_SHOWS))
    ap.add_argument("--production", action="store_true", help="mainnet via www.crossmint.com ($)")
    args = ap.parse_args()

    path = pathlib.Path(args.manifest) if args.manifest else latest_manifest(pathlib.Path(args.shows_dir))
    if path is None or not path.exists():
        print(f"no show manifest found (looked in {args.shows_dir}) — stop a show first "
              "(admin stop writes one)")
        return 1
    manifest = json.loads(path.read_text(encoding="utf-8"))
    payload = build_payload(manifest, args.recipient, args.image)

    host = "www.crossmint.com" if args.production else "staging.crossmint.com"
    url = f"https://{host}/api/2022-06-09/collections/{args.collection}/nfts"
    key = os.environ.get("CROSSMINT_API_KEY", "")

    print(f"manifest: {path}")
    print(f"endpoint: POST {url}")
    if not key:
        print("\nCROSSMINT_API_KEY not set — dry run. Payload:")
        print(json.dumps(payload, indent=2))
        return 0

    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    print(json.dumps(out, indent=2))
    print("\nmint submitted — allow ~10-30s before it shows in a wallet/explorer (devnet"
          if not args.production else "\nmint submitted (mainnet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
