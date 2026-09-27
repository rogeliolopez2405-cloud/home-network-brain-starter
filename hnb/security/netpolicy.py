"""Who may talk to the hub at all.

The hub answers only callers on the home network, loopback, or a private
overlay (Tailscale's shared address space). This is enforced in the app, not
left to Docker's port binding or the host firewall — Docker-published ports
bypass host firewall rules, and the LAN address can change after a reboot.

Forwarded-for headers are never trusted: nothing proxies in front of the hub
in M1, so the socket peer address is the only truthful source.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Broad, standard ranges only — never a specific home's subnet.
DEFAULT_ALLOWED_NETWORKS = (
    "127.0.0.0/8",        # loopback
    "10.0.0.0/8",         # RFC 1918
    "172.16.0.0/12",      # RFC 1918 (also Docker bridges)
    "192.168.0.0/16",     # RFC 1918
    "169.254.0.0/16",     # IPv4 link-local
    "100.64.0.0/10",      # shared address space (Tailscale)
    "::1/128",            # IPv6 loopback
    "fe80::/10",          # IPv6 link-local
    "fc00::/7",           # IPv6 unique local
)


def parse_networks(values: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(v.strip(), strict=True) for v in values if v.strip())


def is_allowed(host: str | None, networks: Iterable[Network]) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks if addr.version == net.version)
