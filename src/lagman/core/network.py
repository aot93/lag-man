"""Network settings for the generator's web UI (SPEC.md, "Web control front end").

The Pi's network is run by NetworkManager. The generator keeps its own three connection
profiles and never edits any other: ``lagman-wired`` for the Ethernet port, ``lagman-wifi``
to join a Wi-Fi network and ``lagman-hotspot`` for a network the Pi makes itself. This module
validates the settings the UI sends and turns them into ``nmcli`` properties; running nmcli
lives in ``web.network``.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

WIRED, WIFI, HOTSPOT = "lagman-wired", "lagman-wifi", "lagman-hotspot"
HOTSPOT_ADDRESS = "192.168.4.1/24"
BANDS = {"2.4": "bg", "5": "a"}  # UI band -> 802-11-wireless.band
PRIORITY = "100"  # above the profiles the OS image makes, so ours win at boot

_IPV4_KEYS = ("ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns")
_AUTO = ("connection.autoconnect", "connection.autoconnect-priority")
_SECURITY = ("802-11-wireless-security.key-mgmt", "802-11-wireless-security.psk")

# Every property the generator sets on each profile: what a snapshot saves and a revert
# restores. Secrets (the psk) are read only for that, never sent to the UI.
MANAGED_KEYS = {
    WIRED: (*_AUTO, *_IPV4_KEYS),
    WIFI: (*_AUTO, "802-11-wireless.ssid", "802-11-wireless.mode", *_SECURITY, *_IPV4_KEYS),
    HOTSPOT: (*_AUTO, "802-11-wireless.ssid", "802-11-wireless.mode", "802-11-wireless.band",
              *_SECURITY, "802-11-wireless-security.proto",
              "802-11-wireless-security.pairwise", "802-11-wireless-security.group",
              *_IPV4_KEYS, "ipv6.method"),
}


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value.strip()


def _interface(text: str, what: str) -> ipaddress.IPv4Interface:
    try:
        iface = ipaddress.ip_interface(text if "/" in text else text + "/24")
    except ValueError:
        raise ValueError(f"{what} must be an IPv4 address like 192.168.1.50/24; "
                         f"got {text!r}") from None
    if not isinstance(iface, ipaddress.IPv4Interface):
        raise ValueError(f"{what} must be an IPv4 address; got {text!r}")
    net = iface.network
    if net.prefixlen <= 30:
        if iface.ip == net.network_address:
            raise ValueError(f"{iface} is the network's own address; use a host address")
        if iface.ip == net.broadcast_address:
            raise ValueError(f"{iface} is the network's broadcast address")
    return iface


def _ipv4_addr(text: str, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise ValueError(f"{what} must be an IPv4 address like 192.168.1.1; "
                         f"got {text!r}") from None


@dataclass(frozen=True)
class IPv4:
    method: str = "auto"  # auto (DHCP) or manual (static)
    address: str = ""  # with prefix, 192.168.1.50/24
    gateway: str = ""
    dns: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IPv4:
        method = _text(data, "method") or "auto"
        if method not in ("auto", "manual"):
            raise ValueError("addressing method must be auto (DHCP) or manual (static)")
        if method == "auto":
            return cls()
        address = _text(data, "address")
        if not address:
            raise ValueError("a static address is needed, like 192.168.1.50/24")
        iface = _interface(address, "address")
        gateway = _text(data, "gateway")
        if gateway:
            gateway = _ipv4_addr(gateway, "gateway")
            if ipaddress.IPv4Address(gateway) not in iface.network:
                raise ValueError(f"gateway {gateway} is not in {iface.network}")
        dns = data.get("dns") or []
        if isinstance(dns, str):
            dns = [d for d in re.split(r"[\s,]+", dns) if d]
        if not isinstance(dns, list):
            raise ValueError("DNS must be a list of addresses")
        try:
            servers = tuple(_ipv4_addr(str(d).strip(), "DNS server") for d in dns)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return cls("manual", str(iface), gateway, servers)

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "address": self.address, "gateway": self.gateway,
                "dns": list(self.dns)}


def _ssid(data: dict[str, Any]) -> str:
    ssid = _text(data, "ssid")
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("SSID must be 1 to 32 bytes")
    return ssid


def _password(data: dict[str, Any]) -> str | None:
    """A WPA passphrase, or None (blank) to keep the saved one."""
    value = data.get("password") or ""
    if not isinstance(value, str):
        raise ValueError("password must be text")
    if not value:
        return None
    if not 8 <= len(value) <= 63 or not value.isascii() or not value.isprintable():
        raise ValueError("Wi-Fi password must be 8 to 63 plain characters (WPA2)")
    return value


@dataclass(frozen=True)
class WifiClient:
    ssid: str
    password: str | None
    ipv4: IPv4 = field(default_factory=IPv4)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WifiClient:
        return cls(_ssid(data), _password(data), IPv4.from_dict(data.get("ipv4") or {}))


@dataclass(frozen=True)
class Hotspot:
    ssid: str
    password: str | None
    band: str = "2.4"
    address: str = HOTSPOT_ADDRESS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hotspot:
        band = _text(data, "band") or "2.4"
        if band not in BANDS:
            raise ValueError("band must be 2.4 or 5 (GHz)")
        iface = _interface(_text(data, "address") or HOTSPOT_ADDRESS, "hotspot address")
        if not iface.ip.is_private:
            raise ValueError(f"hotspot address must be a private address, like "
                             f"{HOTSPOT_ADDRESS}")
        return cls(_ssid(data), _password(data), band, str(iface))


# ---- nmcli properties -------------------------------------------------------------------


def ipv4_props(ip: IPv4) -> list[str]:
    # Setting every key each time clears the static values when switching to DHCP.
    return ["ipv4.method", ip.method, "ipv4.addresses", ip.address,
            "ipv4.gateway", ip.gateway, "ipv4.dns", ",".join(ip.dns)]


def _autoconnect() -> list[str]:
    return ["connection.autoconnect", "yes", "connection.autoconnect-priority", PRIORITY]


def _security(password: str | None) -> list[str]:
    props = ["802-11-wireless-security.key-mgmt", "wpa-psk"]
    if password is not None:
        props += ["802-11-wireless-security.psk", password]
    return props


def wired_props(ip: IPv4) -> list[str]:
    return [*_autoconnect(), *ipv4_props(ip)]


def wifi_props(c: WifiClient) -> list[str]:
    return [*_autoconnect(), "802-11-wireless.ssid", c.ssid,
            "802-11-wireless.mode", "infrastructure", *_security(c.password),
            *ipv4_props(c.ipv4)]


def hotspot_props(h: Hotspot) -> list[str]:
    # "shared" runs a DHCP server (dnsmasq) for the clients on the hotspot's subnet.
    # TODO(verify): WPA2-only (rsn, ccmp) is what the Pi 5's Wi-Fi needs as an access point.
    return [*_autoconnect(), "802-11-wireless.ssid", h.ssid, "802-11-wireless.mode", "ap",
            "802-11-wireless.band", BANDS[h.band], *_security(h.password),
            "802-11-wireless-security.proto", "rsn",
            "802-11-wireless-security.pairwise", "ccmp",
            "802-11-wireless-security.group", "ccmp",
            "ipv4.method", "shared", "ipv4.addresses", h.address, "ipv4.gateway", "",
            "ipv4.dns", "", "ipv6.method", "disabled"]


# ---- nmcli output -----------------------------------------------------------------------


def split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output, where ':' and '\\' in values are escaped."""
    out, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def parse_rows(text: str, width: int) -> list[list[str]]:
    """Rows of ``nmcli -t`` output, each padded or cut to ``width`` fields."""
    return [(split_terse(line) + [""] * width)[:width] for line in text.splitlines() if line]


def parse_fields(text: str) -> dict[str, str]:
    """``KEY:value`` lines from ``nmcli -t ... show``."""
    out = {}
    for line in text.splitlines():
        if not line:
            continue
        key, _, rest = line.partition(":")
        out[key] = ":".join(split_terse(rest))
    return out


def ipv4_from_nm(fields: dict[str, str]) -> IPv4:
    if fields.get("ipv4.method") != "manual":
        return IPv4()
    dns = tuple(d for d in re.split(r"[\s,]+", fields.get("ipv4.dns", "")) if d)
    address = fields.get("ipv4.addresses", "").split(",")[0].strip()
    return IPv4("manual", address, fields.get("ipv4.gateway", ""), dns)


def client_from_nm(fields: dict[str, str]) -> dict[str, Any]:
    return {"ssid": fields.get("802-11-wireless.ssid", ""),
            "ipv4": ipv4_from_nm(fields).to_dict()}


def hotspot_from_nm(fields: dict[str, str]) -> dict[str, Any]:
    band = {v: k for k, v in BANDS.items()}.get(fields.get("802-11-wireless.band", ""), "2.4")
    address = fields.get("ipv4.addresses", "").split(",")[0].strip() or HOTSPOT_ADDRESS
    return {"ssid": fields.get("802-11-wireless.ssid", ""), "band": band, "address": address}
