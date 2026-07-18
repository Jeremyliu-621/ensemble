"""Numeric IP validation and RFC-compliant URL authority formatting."""
from __future__ import annotations

import ipaddress
import sys


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_numeric_ip(value: str) -> IPAddress:
    """Parse a bare numeric IPv4 or IPv6 address with a concise error."""
    if "%" in value:
        raise ValueError("must be a bare numeric IPv4 or IPv6 address without an interface scope")
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("must be a bare numeric IPv4 or IPv6 address") from exc


def validate_probe_server_address(value: str) -> str:
    """Return a canonical address that a separate UNO Q can reach.

    Loopback and special-purpose destination classes cannot identify the laptop
    from the board. Link-local IPv6 also needs a receiver-local interface scope,
    so the probe deliberately requires a global or unique-local address instead.
    """
    address = parse_numeric_ip(value)
    if address.is_loopback:
        raise ValueError("cannot be loopback; the UNO Q cannot reach the laptop through it")
    if address.is_unspecified:
        raise ValueError("cannot be unspecified; choose an address assigned to the laptop")
    if address.is_multicast:
        raise ValueError("cannot be multicast; choose a unicast address assigned to the laptop")
    if address.is_link_local:
        raise ValueError(
            "cannot be an unscoped link-local address; use a global or unique-local address"
        )
    return address.compressed


def format_url_host(value: str) -> str:
    """Format a numeric address as the host portion of a URL authority.

    Non-address values are returned unchanged so existing hostname-based server
    overrides remain backward-compatible.
    """
    try:
        address = parse_numeric_ip(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    return address.compressed


def websocket_url(address: str, port: int, path: str = "/ws") -> str:
    """Build a canonical ws URL from a numeric IPv4 or IPv6 literal."""
    parsed = parse_numeric_ip(address)
    if not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    if not path.startswith("/"):
        raise ValueError("WebSocket path must start with /")
    return f"ws://{format_url_host(parsed.compressed)}:{port}{path}"


def address_score(value: str) -> int:
    """Rank addresses for LAN auto-detection and startup diagnostics."""
    try:
        address = parse_numeric_ip(value)
    except ValueError:
        return -100

    if address.is_link_local:
        return -60
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return -55
    if isinstance(address, ipaddress.IPv6Address):
        # Both unique-local and global IPv6 are plausible peer-reachable LAN
        # addresses. Auto-detection currently supplies IPv4 candidates only,
        # but an IPv6 WM_LAN_IP override must not trigger a false VPN warning.
        return 60 if address.is_private else 20

    first, second = address.packed[:2]
    if first == 172 and second in (17, 18):
        return -50
    if first == 192 and second == 168:
        return 100
    if first == 10:
        return 80
    if first == 172 and 16 <= second <= 31:
        return 60
    if first == 100 and 64 <= second <= 127:
        return 40
    return 20


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: network_address.py ADDRESS", file=sys.stderr)
        return 2
    try:
        print(validate_probe_server_address(argv[0]))
    except ValueError as exc:
        print(f"--server-ip {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
