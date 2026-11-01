"""Network control for the web UI: NetworkManager through ``nmcli``, with a safe way back.

A change can cut off the browser that asked for it, so ``NetworkControl`` applies it a moment
after replying, then waits for the user to confirm from the new address. If nobody confirms
in time, or the change fails, the previous settings come back. This runs beside the render
loop, not in it: the network is not generator state, so it does not go through
``Engine.submit``.
"""

from __future__ import annotations

import copy
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core import network as net

KINDS = {"wired": "wired", "client": "Wi-Fi", "hotspot": "hotspot"}
_PROFILE = {"wired": net.WIRED, "client": net.WIFI, "hotspot": net.HOTSPOT}
_PERMISSION_HINT = ("the generator's user may not change network settings; run "
                    "deploy/install-pi.sh on the Pi to allow it")


class NetworkError(Exception):
    """NetworkManager refused or failed a change."""


class NetworkBusy(Exception):
    """Another change is still being applied or waiting for confirmation."""


def parse(kind: str, data: dict[str, Any]):
    if kind == "wired":
        return net.IPv4.from_dict(data)
    if kind == "client":
        return net.WifiClient.from_dict(data)
    if kind == "hotspot":
        return net.Hotspot.from_dict(data)
    raise ValueError(f"unknown network kind {kind!r}; use wired, client or hotspot")


@dataclass
class Snapshot:
    kind: str
    profiles: dict[str, dict[str, str] | None]  # managed properties, None if absent
    active: dict[str, str | None]  # device -> active connection UUID


# ---- NetworkManager ---------------------------------------------------------------------


def run_nmcli(args: list[str]) -> str:
    env = {**os.environ, "LC_ALL": "C.UTF-8"}  # English messages, stable field values
    try:
        p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=60,
                           env=env)
    except FileNotFoundError:
        raise NetworkError("nmcli not found: NetworkManager is not installed") from None
    except subprocess.TimeoutExpired:
        raise NetworkError("nmcli did not answer within a minute") from None
    if p.returncode:
        msg = (p.stderr or p.stdout).strip().removeprefix("Error: ")
        raise NetworkError(msg or f"nmcli failed with exit code {p.returncode}")
    return p.stdout


def _has_dnsmasq() -> bool:
    return shutil.which("dnsmasq", path="/usr/sbin:/usr/bin:/sbin:/bin") is not None


class NMBackend:
    """Reads and changes the network through nmcli. TODO(verify) on the Pi 5: the hotspot,
    switching between it and a Wi-Fi network, and reverting each."""

    def __init__(self, run: Callable[[list[str]], str] = run_nmcli,
                 has_dnsmasq: Callable[[], bool] = _has_dnsmasq):
        self._run = run
        self._has_dnsmasq = has_dnsmasq

    def _nm(self, args: list[str]) -> str:
        try:
            return self._run(args)
        except NetworkError as e:
            text = str(e).lower()
            if "privileges" in text or "not authorized" in text:
                raise NetworkError(f"{e}: {_PERMISSION_HINT}") from None
            raise

    def _devices(self) -> dict[str, dict[str, str]]:
        """The first Ethernet and Wi-Fi device, by kind."""
        out: dict[str, dict[str, str]] = {}
        rows = net.parse_rows(self._nm(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]),
                              4)
        for dev, typ, state, conn in rows:
            kind = {"ethernet": "ethernet", "wifi": "wifi"}.get(typ)
            if kind and kind not in out:
                out[kind] = {"device": dev, "state": state, "connection": conn}
        return out

    def _connections(self) -> list[list[str]]:
        return net.parse_rows(self._nm(["-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection",
                                        "show"]), 4)

    def _profile(self, name: str, keys: tuple[str, ...], secrets: bool = False) -> dict:
        flags = ["-s", "-t"] if secrets else ["-t"]
        return net.parse_fields(self._nm([*flags, "-f", ",".join(keys), "connection", "show",
                                          name]))

    def info(self) -> dict[str, Any]:
        devices = self._devices()
        names = {r[0] for r in self._connections()}
        out_devices = {}
        for kind, d in devices.items():
            f = net.parse_fields(self._nm(["-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                                           "device", "show", d["device"]]))
            entry = {**d,
                     "addresses": [v for k, v in f.items() if k.startswith("IP4.ADDRESS")],
                     "gateway": f.get("IP4.GATEWAY", ""),
                     "dns": [v for k, v in f.items() if k.startswith("IP4.DNS")]}
            if kind == "wifi":
                entry["ssid"], entry["mode"] = "", None
                if d["connection"]:
                    w = self._profile(d["connection"],
                                      ("802-11-wireless.ssid", "802-11-wireless.mode"))
                    entry["ssid"] = w.get("802-11-wireless.ssid", "")
                    entry["mode"] = {"ap": "hotspot", "infrastructure": "client"}.get(
                        w.get("802-11-wireless.mode", ""))
            out_devices[kind] = entry
        public = {k: tuple(x for x in v if not x.endswith(".psk"))
                  for k, v in net.MANAGED_KEYS.items()}
        profiles: dict[str, Any] = {"wired": None, "client": None, "hotspot": None}
        if net.WIRED in names:
            fields = self._profile(net.WIRED, public[net.WIRED])
            profiles["wired"] = net.ipv4_from_nm(fields).to_dict()
        if net.WIFI in names:
            profiles["client"] = net.client_from_nm(self._profile(net.WIFI, public[net.WIFI]))
        if net.HOTSPOT in names:
            profiles["hotspot"] = net.hotspot_from_nm(
                self._profile(net.HOTSPOT, public[net.HOTSPOT]))
        return {"devices": out_devices, "profiles": profiles}

    def scan(self) -> list[dict[str, Any]]:
        rows = net.parse_rows(self._nm(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                                        "list", "--rescan", "auto"]), 3)
        best: dict[str, dict[str, Any]] = {}
        for ssid, signal, security in rows:
            if ssid and (ssid not in best or int(signal or 0) > best[ssid]["signal"]):
                best[ssid] = {"ssid": ssid, "signal": int(signal or 0), "security": security}
        return sorted(best.values(), key=lambda n: -n["signal"])

    def _names(self, kind: str) -> tuple[str, ...]:
        return (net.WIRED,) if kind == "wired" else (net.WIFI, net.HOTSPOT)

    def _device(self, kind: str) -> dict[str, str]:
        devices = self._devices()
        dev = devices.get("ethernet" if kind == "wired" else "wifi")
        if not dev:
            raise NetworkError("this computer has no wired network port" if kind == "wired"
                               else "this computer has no Wi-Fi")
        return dev

    def snapshot(self, kind: str) -> Snapshot:
        names = {r[0] for r in self._connections()}
        profiles = {n: (self._profile(n, net.MANAGED_KEYS[n], secrets=True)
                        if n in names else None) for n in self._names(kind)}
        dev = self._device(kind)["device"]
        active = {dev: next((r[1] for r in self._connections() if r[3] == dev), None)}
        return Snapshot(kind, profiles, active)

    def apply(self, kind: str, settings) -> None:
        if kind == "hotspot" and not self._has_dnsmasq():
            # NetworkManager runs dnsmasq to hand out addresses on a shared connection.
            raise NetworkError("the hotspot needs dnsmasq to give out addresses; install "
                               "it with 'sudo apt install dnsmasq-base'")
        dev = self._device(kind)
        names = {r[0] for r in self._connections()}
        name = _PROFILE[kind]
        if kind == "wired":
            props, typ = net.wired_props(settings), "ethernet"
        elif kind == "client":
            props, typ = net.wifi_props(settings), "wifi"
        else:
            props, typ = net.hotspot_props(settings), "wifi"
        if name in names:
            self._nm(["connection", "modify", name, *props])
        else:
            self._nm(["connection", "add", "type", typ, "con-name", name,
                      "ifname", dev["device"], *props])
        # Only one Wi-Fi profile of ours connects at boot: the one chosen last.
        other = {"client": net.HOTSPOT, "hotspot": net.WIFI}.get(kind)
        if other in names:
            self._nm(["connection", "modify", other, "connection.autoconnect", "no"])
        if kind == "wired" and dev["state"] == "unavailable":
            return  # no cable: the profile is used when one is plugged in
        self._nm(["-w", "30", "connection", "up", name])

    def restore(self, snap: Snapshot) -> None:
        names = {r[0] for r in self._connections()}
        for name, fields in snap.profiles.items():
            if fields is None:
                if name in names:
                    self._nm(["connection", "delete", name])
            elif fields:
                args = [x for k, v in fields.items() for x in (k, v)]
                self._nm(["connection", "modify", name, *args])
        errors = []
        for dev, uuid in snap.active.items():
            try:
                if uuid:
                    self._nm(["-w", "30", "connection", "up", "uuid", uuid])
                else:
                    self._nm(["device", "disconnect", dev])
            except NetworkError as e:
                errors.append(f"{dev}: {e}")
        if errors:
            raise NetworkError("; ".join(errors))


class SimulatedNetwork:
    """In-memory network for headless runs and tests: an Ethernet port on DHCP and Wi-Fi
    joined to "Studio"."""

    def __init__(self):
        self.fail_next: str | None = None
        self.state: dict[str, Any] = {
            "devices": {
                "ethernet": {"device": "eth0", "state": "connected",
                             "connection": "Wired connection 1",
                             "addresses": ["192.168.1.50/24"], "gateway": "192.168.1.1",
                             "dns": ["192.168.1.1"]},
                "wifi": {"device": "wlan0", "state": "connected", "connection": "preconfigured",
                         "addresses": ["192.168.1.51/24"], "gateway": "192.168.1.1",
                         "dns": ["192.168.1.1"], "ssid": "Studio", "mode": "client"},
            },
            "profiles": {"wired": net.IPv4().to_dict(), "client": None, "hotspot": None},
        }

    def info(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def scan(self) -> list[dict[str, Any]]:
        return [{"ssid": "Studio", "signal": 82, "security": "WPA2"},
                {"ssid": "Studio 5G", "signal": 64, "security": "WPA2 WPA3"},
                {"ssid": "Guest", "signal": 31, "security": ""}]

    def snapshot(self, kind: str) -> Snapshot:
        return Snapshot(kind, {"state": copy.deepcopy(self.state)}, {})

    def apply(self, kind: str, settings) -> None:
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise NetworkError(msg)
        devices, profiles = self.state["devices"], self.state["profiles"]
        if kind == "wired":
            profiles["wired"] = settings.to_dict()
            eth = devices["ethernet"]
            eth["connection"] = net.WIRED
            if settings.method == "manual":
                eth.update(addresses=[settings.address], gateway=settings.gateway,
                           dns=list(settings.dns))
        elif kind == "client":
            profiles["client"] = {"ssid": settings.ssid, "ipv4": settings.ipv4.to_dict()}
            devices["wifi"].update(connection=net.WIFI, ssid=settings.ssid, mode="client")
        else:
            profiles["hotspot"] = {"ssid": settings.ssid, "band": settings.band,
                                   "address": settings.address}
            devices["wifi"].update(connection=net.HOTSPOT, ssid=settings.ssid, mode="hotspot",
                                   addresses=[settings.address], gateway="", dns=[])

    def restore(self, snap: Snapshot) -> None:
        self.state = copy.deepcopy(snap.profiles["state"])


# ---- confirm or revert ------------------------------------------------------------------


def _log(message: str) -> None:
    print(f"network: {message}", file=sys.stderr)  # the service's journal


def _timer(delay: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


class NetworkControl:
    def __init__(self, backend=None, note: str = "", confirm_s: float = 120.0,
                 delay_s: float = 1.0, schedule: Callable = _timer,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = _log):
        self.backend = backend
        self._log = log
        self.note = note
        self.confirm_s = confirm_s
        self.delay_s = delay_s
        self._schedule = schedule
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._last_error: str | None = None

    def _need_backend(self):
        if self.backend is None:
            raise NetworkError(self.note or "network control is off")
        return self.backend

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": self.backend is not None, "note": self.note,
                               "hostname": socket.gethostname(), "devices": {},
                               "profiles": {}}
        if self.backend is not None:
            try:
                out.update(self.backend.info())
            except NetworkError as e:
                out["error"] = str(e)
        with self._lock:
            out["last_error"] = self._last_error
            p = self._pending
            out["pending"] = None if p is None else {
                "kind": p["kind"], "label": KINDS[p["kind"]], "state": p["state"],
                "remaining_s": (max(0, math.ceil(p["deadline"] - self._clock()))
                                if p["state"] == "waiting" else None)}
        return out

    def scan(self) -> list[dict[str, Any]]:
        return self._need_backend().scan()

    def apply(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        settings = parse(kind, data)
        backend = self._need_backend()
        saved = (backend.info().get("profiles") or {}).get(kind)
        if getattr(settings, "password", "") is None and (
                saved is None or saved.get("ssid") != settings.ssid):
            raise ValueError("enter the Wi-Fi password")
        with self._lock:
            if self._pending is not None:
                raise NetworkBusy("keep or revert the last network change first")
            snap = backend.snapshot(kind)
            token = object()
            self._pending = {"kind": kind, "state": "applying", "snap": snap,
                             "token": token, "deadline": 0.0}
            self._last_error = None
        self._log(f"applying {KINDS[kind]} settings")
        self._schedule(self.delay_s, lambda: self._apply(token, kind, settings))
        return self.info()

    def _apply(self, token, kind: str, settings) -> None:
        try:
            self.backend.apply(kind, settings)
        except NetworkError as e:
            self._log(f"{KINDS[kind]} settings failed: {e}")
            self._finish(token, f"Could not apply the {KINDS[kind]} settings: {e}.")
            return
        self._log(f"{KINDS[kind]} settings applied; waiting for confirmation")
        with self._lock:
            if self._pending is None or self._pending["token"] is not token:
                return
            self._pending.update(state="waiting", deadline=self._clock() + self.confirm_s)
        self._schedule(self.confirm_s, lambda: self._expire(token))

    def _expire(self, token) -> None:
        with self._lock:
            p = self._pending
            if p is None or p["token"] is not token or p["state"] != "waiting":
                return
            p["state"] = "reverting"
        self._finish(token, f"The {KINDS[p['kind']]} change was not confirmed within "
                            f"{round(self.confirm_s)} s.")

    def _finish(self, token, reason: str | None) -> None:
        """Put the previous settings back and clear the pending change."""
        with self._lock:
            p = self._pending
            if p is None or p["token"] is not token:
                return
        try:
            self.backend.restore(p["snap"])
            outcome, failed = "The previous settings were restored.", False
        except NetworkError as e:
            outcome, failed = f"Restoring the previous settings failed: {e}.", True
        self._log(f"{reason or 'Reverted by the user.'} {outcome}")
        with self._lock:
            self._pending = None
            self._last_error = (f"{reason} {outcome}" if reason
                                else outcome if failed else None)

    def confirm(self) -> dict[str, Any]:
        with self._lock:
            if self._pending is None or self._pending["state"] != "waiting":
                raise NetworkBusy("no network change is waiting for confirmation")
            self._pending = None
        self._log("change kept")
        return self.info()

    def revert(self) -> dict[str, Any]:
        with self._lock:
            p = self._pending
            if p is None or p["state"] != "waiting":
                raise NetworkBusy("no network change is waiting for confirmation")
            p["state"] = "reverting"
            token = p["token"]
        self._schedule(self.delay_s, lambda: self._finish(token, None))
        return self.info()
