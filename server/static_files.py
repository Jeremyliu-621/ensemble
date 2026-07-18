"""Serve the web/ directory over the same port as the WebSocket endpoint.

websockets' `process_request` hook is called for every incoming connection
before the handshake. If it returns a Response, the connection is answered as
plain HTTP (a static file); if it returns None, the WebSocket handshake
proceeds. We route WS_PATH to the socket and everything else to a file.
"""
from __future__ import annotations

import mimetypes
import urllib.parse

from websockets.datastructures import Headers
from websockets.http11 import Response

from config import WEB_DIR

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("image/svg+xml", ".svg")

# Map a bare directory request to its index.
DIRECTORY_INDEX = "index.html"


def _text_response(status: int, reason: str, body: str) -> Response:
    return Response(
        status,
        reason,
        Headers({"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body.encode()))}),
        body.encode(),
    )


def _redirect(location: str) -> Response:
    """302 to the canonical URL. Serving a directory's index INLINE at a
    slash-less path would break every relative asset on the page (the browser
    resolves ./x.js against the wrong base), so redirect like a real server."""
    return Response(302, "Found", Headers({"Location": location, "Content-Length": "0"}), b"")


def build_static_response(raw_path: str) -> Response:
    """Resolve a URL path to a file under WEB_DIR and return an HTTP Response.

    Guards against directory traversal by resolving and confirming the target
    stays inside WEB_DIR.
    """
    url_path = urllib.parse.urlparse(raw_path).path
    rel = urllib.parse.unquote(url_path).lstrip("/")
    if rel == "":
        return _redirect("/console/")  # bare host -> the console (the app IS the landing)

    target = (WEB_DIR / rel).resolve()

    # Directory -> its index.html. A directory URL without a trailing slash
    # must redirect first, or the page's relative assets (./app.js) resolve to
    # the parent and 404 — a page full of dead buttons.
    if target.is_dir():
        if not url_path.endswith("/"):
            return _redirect(url_path + "/")
        target = (target / DIRECTORY_INDEX).resolve()

    # Traversal guard: target must live under WEB_DIR.
    try:
        target.relative_to(WEB_DIR)
    except ValueError:
        return _text_response(403, "Forbidden", "403 Forbidden")

    if not target.is_file():
        return _text_response(404, "Not Found", f"404 Not Found: {url_path}")

    ctype, _ = mimetypes.guess_type(str(target))
    if ctype is None:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"

    body = target.read_bytes()
    headers = Headers({
        "Content-Type": ctype,
        "Content-Length": str(len(body)),
        # Dev convenience: never cache, so edited JS/HTML shows up on reload.
        "Cache-Control": "no-cache, no-store, must-revalidate",
    })
    return Response(200, "OK", headers, body)
