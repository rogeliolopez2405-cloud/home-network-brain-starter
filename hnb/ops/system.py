"""Facts about the host the hub runs on — only what can actually be read,
never a guessed "all good". Used by the setup wizard's Network step and
/api/system."""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path


def _lan_ipv4() -> str | None:
    """The source address the OS would use for off-host traffic. A UDP
    `connect` only consults the routing table; no packet is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                      # TEST-NET-1: never routed anywhere real
        addr = s.getsockname()[0]
        return addr if not ipaddress.ip_address(addr).is_loopback else None
    except OSError:
        return None
    finally:
        s.close()


def _ipv6_state() -> dict[str, bool | None]:
    """IPv6 matters for Matter. Read the kernel's view where available."""
    disabled = Path("/proc/sys/net/ipv6/conf/all/disable_ipv6")
    inet6 = Path("/proc/net/if_inet6")
    if not disabled.exists():
        return {"enabled": None, "link_local": None}      # not Linux: unknown, not "no"
    enabled = disabled.read_text().strip() == "0"
    link_local = False
    if inet6.exists():
        for line in inet6.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[0].startswith("fe80") and parts[5] != "lo":
                link_local = True
                break
    return {"enabled": enabled, "link_local": link_local}


def network_facts(port: int) -> dict[str, object]:
    ip = _lan_ipv4()
    host = socket.gethostname()
    return {
        "hostname": host,
        "lan_address": ip,
        "urls": [u for u in (f"http://{ip}:{port}" if ip else None,) if u],
        "ipv6": _ipv6_state(),
    }
