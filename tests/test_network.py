import pytest

from lagman.core import network as net
from lagman.web.network import (
    NetworkBusy,
    NetworkControl,
    NetworkError,
    NMBackend,
    SimulatedNetwork,
)

# ---- settings ---------------------------------------------------------------------------


def test_ipv4_dhcp_and_static():
    assert net.IPv4.from_dict({"method": "auto"}) == net.IPv4()
    ip = net.IPv4.from_dict({"method": "manual", "address": " 192.168.1.50 ",
                             "gateway": "192.168.1.1", "dns": "1.1.1.1, 8.8.8.8"})
    assert ip.address == "192.168.1.50/24"  # a bare address is a /24
    assert ip.dns == ("1.1.1.1", "8.8.8.8")
    assert ip.to_dict() == {"method": "manual", "address": "192.168.1.50/24",
                            "gateway": "192.168.1.1", "dns": ["1.1.1.1", "8.8.8.8"]}
    assert net.IPv4.from_dict({"method": "manual", "address": "10.0.0.5/8"}).gateway == ""


@pytest.mark.parametrize("bad, match", [
    ({"method": "static"}, "method"),
    ({"method": "manual"}, "address"),
    ({"method": "manual", "address": "192.168.1.300"}, "address"),
    ({"method": "manual", "address": "192.168.1.0/24"}, "network"),
    ({"method": "manual", "address": "192.168.1.255/24"}, "broadcast"),
    ({"method": "manual", "address": "fe80::1/64"}, "IPv4"),
    ({"method": "manual", "address": "192.168.1.5/24", "gateway": "10.0.0.1"}, "gateway"),
    ({"method": "manual", "address": "192.168.1.5/24", "dns": "one.one"}, "DNS"),
])
def test_ipv4_rejects_bad_settings(bad, match):
    with pytest.raises(ValueError, match=match):
        net.IPv4.from_dict(bad)


def test_wifi_client_settings():
    c = net.WifiClient.from_dict({"ssid": "Studio", "password": "correct horse"})
    assert c.ssid == "Studio" and c.password == "correct horse" and c.ipv4 == net.IPv4()
    # A blank password keeps the saved one.
    assert net.WifiClient.from_dict({"ssid": "Studio", "password": ""}).password is None
    with pytest.raises(ValueError, match="SSID"):
        net.WifiClient.from_dict({"ssid": ""})
    with pytest.raises(ValueError, match="SSID"):
        net.WifiClient.from_dict({"ssid": "x" * 33})
    with pytest.raises(ValueError, match="8 to 63"):
        net.WifiClient.from_dict({"ssid": "Studio", "password": "short"})


def test_hotspot_settings():
    h = net.Hotspot.from_dict({"ssid": "lag-man", "password": "latency-test"})
    assert (h.band, h.address) == ("2.4", "192.168.4.1/24")
    assert net.Hotspot.from_dict({"ssid": "a", "password": "", "band": "5"}).password is None
    with pytest.raises(ValueError, match="band"):
        net.Hotspot.from_dict({"ssid": "a", "password": "latency-test", "band": "6"})
    with pytest.raises(ValueError, match="private"):
        net.Hotspot.from_dict({"ssid": "a", "password": "latency-test", "address": "8.8.8.1"})


# ---- NetworkManager properties ----------------------------------------------------------


def _pairs(props):
    assert len(props) % 2 == 0
    return dict(zip(props[::2], props[1::2], strict=True))


def test_dhcp_clears_static_addresses():
    p = _pairs(net.wired_props(net.IPv4()))
    assert p["ipv4.method"] == "auto" and p["ipv4.addresses"] == "" and p["ipv4.dns"] == ""
    assert p["connection.autoconnect"] == "yes"
    p = _pairs(net.wired_props(net.IPv4("manual", "192.168.1.50/24", "192.168.1.1",
                                        ("1.1.1.1",))))
    assert (p["ipv4.method"], p["ipv4.addresses"], p["ipv4.gateway"], p["ipv4.dns"]) == (
        "manual", "192.168.1.50/24", "192.168.1.1", "1.1.1.1")
    assert set(p) <= set(net.MANAGED_KEYS[net.WIRED])


def test_wifi_props_keep_the_password_unless_given():
    p = _pairs(net.wifi_props(net.WifiClient("Studio", None, net.IPv4())))
    assert p["802-11-wireless.ssid"] == "Studio" and p["802-11-wireless.mode"] == "infrastructure"
    assert "802-11-wireless-security.psk" not in p
    p = _pairs(net.wifi_props(net.WifiClient("Studio", "correct horse", net.IPv4())))
    assert p["802-11-wireless-security.psk"] == "correct horse"
    assert set(p) <= set(net.MANAGED_KEYS[net.WIFI])


def test_hotspot_props_share_a_fixed_address():
    p = _pairs(net.hotspot_props(net.Hotspot("lag-man", "latency-test", "5", "192.168.4.1/24")))
    assert p["802-11-wireless.mode"] == "ap" and p["802-11-wireless.band"] == "a"
    assert p["ipv4.method"] == "shared" and p["ipv4.addresses"] == "192.168.4.1/24"
    assert p["802-11-wireless-security.key-mgmt"] == "wpa-psk"
    assert set(p) <= set(net.MANAGED_KEYS[net.HOTSPOT])


def test_terse_output_parsing():
    assert net.split_terse(r"Studio\: 5G:72:WPA2") == ["Studio: 5G", "72", "WPA2"]
    assert net.split_terse(r"a\\b:") == ["a\\b", ""]
    fields = net.parse_fields("ipv4.method:manual\nIP4.ADDRESS[1]:192.168.1.5/24\n"
                              r"IP6.ADDRESS[1]:fe80\:\:1/64" "\n")
    assert fields == {"ipv4.method": "manual", "IP4.ADDRESS[1]": "192.168.1.5/24",
                      "IP6.ADDRESS[1]": "fe80::1/64"}


def test_settings_read_back_from_a_profile():
    fields = {"ipv4.method": "manual", "ipv4.addresses": "192.168.1.5/24",
              "ipv4.gateway": "192.168.1.1", "ipv4.dns": "1.1.1.1,8.8.8.8",
              "802-11-wireless.ssid": "Studio", "802-11-wireless.band": "a"}
    assert net.ipv4_from_nm(fields).dns == ("1.1.1.1", "8.8.8.8")
    assert net.ipv4_from_nm({"ipv4.method": "auto", "ipv4.addresses": ""}) == net.IPv4()
    assert net.hotspot_from_nm(fields)["band"] == "5"


# ---- confirm or revert ------------------------------------------------------------------


class FakeTimers:
    def __init__(self):
        self.due = []

    def __call__(self, delay, fn):
        self.due.append((delay, fn))

    def run(self):
        due, self.due = self.due, []
        for _, fn in due:
            fn()


@pytest.fixture
def ctl():
    timers = FakeTimers()
    now = [0.0]
    control = NetworkControl(SimulatedNetwork(), confirm_s=120, schedule=timers,
                             clock=lambda: now[0])
    return control, timers, now


def test_change_waits_for_confirmation(ctl):
    control, timers, now = ctl
    out = control.apply("wired", {"method": "manual", "address": "192.168.1.60/24"})
    assert out["pending"]["state"] == "applying"
    # Applied after the response is sent, so the reply is not lost with the old address.
    assert control.info()["profiles"]["wired"]["method"] == "auto"
    timers.run()
    info = control.info()
    assert info["profiles"]["wired"]["address"] == "192.168.1.60/24"
    assert info["pending"]["state"] == "waiting" and info["pending"]["remaining_s"] == 120
    now[0] = 30
    assert control.info()["pending"]["remaining_s"] == 90
    with pytest.raises(NetworkBusy):
        control.apply("wired", {"method": "auto"})
    assert control.confirm()["pending"] is None
    timers.run()  # the expiry timer finds nothing to revert
    assert control.info()["profiles"]["wired"]["address"] == "192.168.1.60/24"


def test_unconfirmed_change_reverts(ctl):
    control, timers, _ = ctl
    control.apply("hotspot", {"ssid": "lag-man", "password": "latency-test"})
    timers.run()
    assert control.info()["devices"]["wifi"]["mode"] == "hotspot"
    timers.run()  # nobody confirmed
    info = control.info()
    assert info["devices"]["wifi"]["mode"] == "client" and info["pending"] is None
    assert "not confirmed" in info["last_error"]


def test_revert_now(ctl):
    control, timers, _ = ctl
    control.apply("client", {"ssid": "Other", "password": "something-long"})
    timers.run()
    assert control.revert()["pending"]["state"] == "reverting"
    timers.run()
    info = control.info()
    assert info["devices"]["wifi"]["ssid"] == "Studio" and info["pending"] is None
    with pytest.raises(NetworkBusy):
        control.confirm()


def test_failed_change_is_rolled_back(ctl):
    control, timers, _ = ctl
    control.backend.fail_next = "Secrets were required, but not provided"
    control.apply("client", {"ssid": "Other", "password": "wrong-password"})
    timers.run()
    info = control.info()
    assert info["pending"] is None and "Secrets" in info["last_error"]
    assert info["devices"]["wifi"]["ssid"] == "Studio"


def test_bad_settings_are_rejected_before_anything_changes(ctl):
    control, timers, _ = ctl
    with pytest.raises(ValueError):
        control.apply("wired", {"method": "manual"})
    with pytest.raises(ValueError, match="kind"):
        control.apply("bluetooth", {})
    assert timers.due == [] and control.info()["pending"] is None


def test_no_backend_means_no_control():
    control = NetworkControl(None, note="Network control is off.")
    info = control.info()
    assert not info["available"] and info["note"] == "Network control is off."
    with pytest.raises(NetworkError):
        control.apply("wired", {"method": "auto"})


# ---- nmcli ------------------------------------------------------------------------------


class FakeNmcli:
    """Answers the nmcli queries NMBackend makes and records every command."""

    def __init__(self, profiles=None):
        self.calls = []
        self.profiles = profiles if profiles is not None else {}

    def __call__(self, args):
        self.calls.append(args)
        if args[:4] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return ("eth0:ethernet:connected:Wired connection 1\n"
                    "wlan0:wifi:connected:preconfigured\n")
        if args[:4] == ["-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection"]:
            rows = [f"{n}:uuid-{n}:x:" for n in self.profiles]
            return "\n".join(["Wired connection 1:uuid-w1:ethernet:eth0",
                              "preconfigured:uuid-pre:wifi:wlan0", *rows]) + "\n"
        if args[:2] == ["-s", "-t"] and args[-1] in self.profiles:
            return self.profiles[args[-1]]
        return ""


def test_backend_creates_a_profile_then_activates_it():
    nm = FakeNmcli()
    NMBackend(nm).apply("wired", net.IPv4("manual", "192.168.1.60/24", "", ()))
    add = next(c for c in nm.calls if c[:2] == ["connection", "add"])
    assert add[2:8] == ["type", "ethernet", "con-name", net.WIRED, "ifname", "eth0"]
    assert ["ipv4.addresses", "192.168.1.60/24"] == add[add.index("ipv4.addresses"):][:2]
    assert nm.calls[-1][:4] == ["-w", "30", "connection", "up"]


def test_backend_hotspot_stops_the_client_profile_autoconnecting():
    nm = FakeNmcli({net.WIFI: "connection.autoconnect:yes\n"})
    NMBackend(nm, has_dnsmasq=lambda: True).apply("hotspot", net.Hotspot(
        "lag-man", "latency-test", "2.4",
                                               "192.168.4.1/24"))
    assert ["connection", "modify", net.WIFI, "connection.autoconnect", "no"] in nm.calls
    assert nm.calls[-1][-1] == net.HOTSPOT


def test_backend_restores_a_snapshot():
    nm = FakeNmcli({net.WIRED: "ipv4.method:auto\nipv4.addresses:\n"})
    backend = NMBackend(nm)
    snap = backend.snapshot("wired")
    assert snap.active == {"eth0": "uuid-w1"}  # only the port being changed
    nm.calls.clear()
    backend.restore(snap)
    assert ["connection", "modify", net.WIRED, "ipv4.method", "auto",
            "ipv4.addresses", ""] in nm.calls
    assert nm.calls[-1] == ["-w", "30", "connection", "up", "uuid", "uuid-w1"]


def test_backend_deletes_a_profile_it_created():
    nm = FakeNmcli()
    backend = NMBackend(nm)
    snap = backend.snapshot("hotspot")
    assert snap.profiles == {net.WIFI: None, net.HOTSPOT: None}
    nm.profiles[net.HOTSPOT] = "ipv4.method:shared\n"
    nm.calls.clear()
    backend.restore(snap)
    assert ["connection", "delete", net.HOTSPOT] in nm.calls
    assert ["connection", "delete", net.WIFI] not in nm.calls
    assert ["-w", "30", "connection", "up", "uuid", "uuid-pre"] in nm.calls


def test_backend_reports_missing_permission():
    def denied(args):
        raise NetworkError("Insufficient privileges")

    with pytest.raises(NetworkError, match="install-pi.sh"):
        NMBackend(denied).apply("wired", net.IPv4())


def test_hotspot_needs_dnsmasq():
    nm = FakeNmcli()
    backend = NMBackend(nm, has_dnsmasq=lambda: False)
    with pytest.raises(NetworkError, match="dnsmasq-base"):
        backend.apply("hotspot", net.Hotspot("lag-man", "latency-test"))
    assert not any(c[:2] == ["connection", "add"] for c in nm.calls)


def test_changes_and_failures_are_logged():
    timers, lines = FakeTimers(), []
    control = NetworkControl(SimulatedNetwork(), schedule=timers, log=lines.append)
    control.backend.fail_next = "No suitable device found"
    control.apply("hotspot", {"ssid": "lag-man", "password": "latency-test"})
    timers.run()
    assert any("hotspot" in line for line in lines)
    assert any("No suitable device found" in line for line in lines)


class FakeEngine:
    def __init__(self):
        self.sent = []

    def submit(self, name, timeout=2.0, **args):
        self.sent.append((name, args))


def test_network_slate_sends_lines_only_when_they_change():
    from lagman.web.network import NetworkSlate

    control = NetworkControl(SimulatedNetwork(), schedule=FakeTimers(), log=lambda m: None)
    eng = FakeEngine()
    slate = NetworkSlate(eng, control, 8080)
    slate.poll_once()
    slate.poll_once()
    assert len(eng.sent) == 1
    name, args = eng.sent[0]
    assert name == "set_network" and args["lines"][0] == "WIRED     http://192.168.1.50:8080/"
    control.backend.state["devices"]["wifi"].update(mode="hotspot", ssid="lag-man",
                                                    addresses=["192.168.4.1/24"])
    slate.poll_once()
    assert "HOTSPOT   http://192.168.4.1:8080/  lag-man" in eng.sent[-1][1]["lines"]


def test_network_slate_without_network_control_shows_the_main_address():
    from lagman.web.network import NetworkSlate

    eng = FakeEngine()
    NetworkSlate(eng, NetworkControl(None), 8080, address=lambda: "10.1.2.3").poll_once()
    assert eng.sent[0][1]["lines"][0] == "ADDRESS   http://10.1.2.3:8080/"
