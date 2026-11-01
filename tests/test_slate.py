from lagman.core.slate import network_lines


def _info(wifi=None, eth=None, hostname="lagman"):
    devices = {}
    if eth is not None:
        devices["ethernet"] = {"device": "eth0", "addresses": eth}
    if wifi is not None:
        devices["wifi"] = {"device": "wlan0", **wifi}
    return {"hostname": hostname, "devices": devices}


def test_network_lines_give_the_control_page_on_each_port():
    info = _info(eth=["192.168.1.50/24"],
                 wifi={"addresses": ["192.168.10.101/24"], "ssid": "Batwave", "mode": "client"})
    assert network_lines(info, 8080) == (
        "WIRED     http://192.168.1.50:8080/",
        "WI-FI     http://192.168.10.101:8080/  Batwave",
        "HOST      http://lagman.local:8080/",
    )


def test_network_lines_name_the_hotspot():
    info = _info(eth=[], wifi={"addresses": ["192.168.4.1/24"], "ssid": "lag-man",
                               "mode": "hotspot"})
    assert network_lines(info, 8080)[0] == "HOTSPOT   http://192.168.4.1:8080/  lag-man"


def test_network_lines_when_not_connected():
    assert network_lines(_info(eth=[], hostname=""), 8080) == ("NETWORK   not connected",)
