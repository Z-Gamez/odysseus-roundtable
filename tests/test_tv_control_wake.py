"""Standby detection and Wake-on-LAN for the TV control tool.

A TV in standby keeps its network card alive but stops running the SmartCast
API, so a TCP connection succeeds and the TLS handshake then hangs. Reported
raw, that surfaces as "SSL handshake timeout", which reads like a broken
address rather than a sleeping TV. These tests pin the two behaviours that fix
it: the stall is classified as standby, and power-on sends a magic packet
before giving up.

Addresses here are deliberately generic — the real TV's IP and MAC stay out of
the repo.
"""

import socket

import pytest

from src.agent_tools import tv_control as T

MAC = "aa:bb:cc:dd:ee:ff"
IP = "10.0.0.5"


@pytest.fixture
def creds(monkeypatch):
    """Point the tool at a fake TV, with the fields the caller chooses."""

    def _set(**over):
        data = {"ip": IP, "port": 7345, "token": "t0ken"}
        data.update(over)
        monkeypatch.setattr(T, "_load_creds", lambda: data)
        return data

    return _set


@pytest.fixture
def sent(monkeypatch):
    """Capture UDP datagrams instead of putting them on the wire."""
    packets = []

    class FakeSock:
        def __init__(self, *a, **k):
            pass

        def setsockopt(self, *a):
            pass

        def sendto(self, data, addr):
            packets.append((data, addr))

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", FakeSock)
    return packets


# ── standby classification ────────────────────────────────────────────────


@pytest.fixture
def layers(monkeypatch):
    """Drive the TCP/TLS probes that _classify_failure measures."""

    def _set(tcp, tls):
        monkeypatch.setattr(T, "_tcp_ok", lambda *a: tcp)
        monkeypatch.setattr(T, "_tls_ok", lambda *a: tls)

    return _set


def test_timeout_with_tcp_but_no_tls_is_standby(layers):
    """The standby signature: the card answers, the API never speaks TLS."""
    layers(tcp=True, tls=False)
    out = T._classify_failure(IP, 7345, TimeoutError("timed out"))
    assert out.get("_standby") is True
    assert out.get("_ip") == IP


def test_timeout_with_nothing_listening_is_unreachable(layers):
    """No TCP either — a stale IP, not a sleeping TV. A wake cannot help."""
    layers(tcp=False, tls=False)
    out = T._classify_failure(IP, 7345, TimeoutError("timed out"))
    assert out.get("_unreachable") is True
    assert not out.get("_standby")


def test_timeout_on_a_responsive_tv_is_transient_not_standby(layers):
    """The reported bug: a powered-on TV must never be called asleep.

    A timeout used to be classified as standby purely from the exception type,
    so any transient blip on a TV that was plainly switched on produced "the TV
    is in standby" — and then a pointless wake.
    """
    layers(tcp=True, tls=True)
    out = T._classify_failure(IP, 7345, TimeoutError("timed out"))
    assert out.get("_transient") is True
    assert not out.get("_standby")


def test_diagnose_maps_each_layer_combination(layers):
    layers(tcp=True, tls=True)
    assert T.diagnose(IP, 7345) == "awake"
    layers(tcp=True, tls=False)
    assert T.diagnose(IP, 7345) == "standby"
    layers(tcp=False, tls=False)
    assert T.diagnose(IP, 7345) == "unreachable"


def test_tcp_probe_outlasts_a_slow_standby_connect():
    """A standby connect took 2.4s. If the probe gives up first, standby is
    misread as unreachable and the wake that would fix it never fires."""
    assert T._PROBE_TIMEOUT >= 5.0


def test_standby_error_message_points_at_the_fix():
    """The message has to name the action that recovers, not just the symptom."""
    res = T._result({"_standby": True, "_ip": IP, "_error": "timed out"},
                    "read info")
    assert res["exit_code"] == 1
    low = res["error"].lower()
    assert "standby" in low
    assert "wake-on-lan" in low


def test_unreachable_message_says_a_wake_will_not_help():
    res = T._result({"_unreachable": True, "_ip": IP, "_error": "timed out"},
                    "read info")
    low = res["error"].lower()
    assert "not standby" in low
    assert "tv_token.json" in low


def test_transient_message_does_not_claim_standby():
    """Whatever else it says, it must not tell the user the TV is asleep."""
    res = T._result({"_transient": True, "_ip": IP, "_error": "timed out"},
                    "read info")
    low = res["error"].lower()
    assert "powered on" in low
    assert "not standby" in low


# ── wake ──────────────────────────────────────────────────────────────────


def test_wake_sends_a_valid_magic_packet(creds, sent):
    creds(mac=MAC)
    res = T.wake()
    assert res["exit_code"] == 0
    assert sent, "no packet was sent"

    data, _ = sent[0]
    # 6 sync bytes then the MAC 16 times.
    assert len(data) == 102
    assert data[:6] == b"\xff" * 6
    assert data[6:12] == bytes.fromhex(MAC.replace(":", ""))
    assert data[6:] == bytes.fromhex(MAC.replace(":", "")) * 16


def test_wake_targets_the_subnet_broadcast(creds, sent):
    """A directed broadcast is what reaches the TV; 255.255.255.255 is backup."""
    creds(mac=MAC)
    T.wake()
    targets = {addr for _, addr in sent}
    assert ("10.0.0.255", 9) in targets
    assert ("10.0.0.255", 7) in targets


def test_wake_accepts_dash_separated_mac(creds, sent):
    """Windows `arp -a` prints dashes; the parser must not care."""
    creds(mac=MAC.replace(":", "-").upper())
    assert T.wake()["exit_code"] == 0
    assert sent[0][0][6:12] == bytes.fromhex(MAC.replace(":", ""))


def test_wake_without_a_mac_explains_how_to_supply_one(monkeypatch, creds, sent):
    creds()
    monkeypatch.setattr(T, "_tv_mac", lambda: "")
    res = T.wake()
    assert res["exit_code"] == 1
    assert "mac" in res["error"].lower()
    assert not sent


def test_wake_rejects_a_malformed_mac(creds, sent):
    creds(mac="aa:bb:cc")
    res = T.wake()
    assert res["exit_code"] == 1
    assert not sent


def test_explicit_mac_beats_the_arp_lookup(monkeypatch, creds):
    """A configured MAC must not depend on a fresh ARP entry."""
    creds(mac=MAC)

    def fail(*a, **k):
        raise AssertionError("ARP should not be consulted when mac is set")

    monkeypatch.setattr("subprocess.run", fail)
    assert T._tv_mac() == MAC


# ── ARP lookup ────────────────────────────────────────────────────────────
#
# `arp -a <ip>` is Windows syntax. On macOS and the BSDs, -a and a host
# argument are mutually exclusive, so that invocation yielded nothing and the
# tool reported the MAC as missing — on the machine that actually runs it.


def _fake_arp(monkeypatch, stdout):
    import subprocess

    class R:
        pass

    def run(args, **k):
        r = R()
        # Only the bare table dump returns anything, mimicking a platform where
        # the host-argument forms are rejected.
        r.stdout = stdout if args[:2] == ["arp", "-a"] else ""
        return r

    monkeypatch.setattr(subprocess, "run", run)


def test_parses_macos_arp_output(monkeypatch):
    _fake_arp(monkeypatch,
              "? (10.0.0.1) at 11:22:33:44:55:66 on en0 ifscope [ethernet]\n"
              f"? ({IP}) at {MAC} on en0 ifscope [ethernet]\n")
    assert T._arp_mac(IP) == MAC


def test_parses_windows_arp_output(monkeypatch):
    _fake_arp(monkeypatch,
              "Interface: 10.0.0.2 --- 0x5\n"
              "  Internet Address      Physical Address      Type\n"
              "  10.0.0.1              11-22-33-44-55-66     dynamic\n"
              f"  {IP}              {MAC.replace(':', '-')}     dynamic\n")
    assert T._arp_mac(IP).lower() == MAC.replace(":", "-")


def test_parses_ip_neigh_output(monkeypatch):
    import subprocess

    class R:
        pass

    def run(args, **k):
        r = R()
        r.stdout = (f"{IP} dev eth0 lladdr {MAC} REACHABLE\n"
                    if args[0] == "ip" else "")
        return r

    monkeypatch.setattr(subprocess, "run", run)
    assert T._arp_mac(IP) == MAC


def test_arp_does_not_return_a_neighbours_mac(monkeypatch):
    """The MAC must come off the line matching this IP, not any line."""
    _fake_arp(monkeypatch,
              f"? (10.0.0.1) at 11:22:33:44:55:66 on en0\n"
              f"? (10.0.0.99) at 99:99:99:99:99:99 on en0\n")
    assert T._arp_mac(IP) == ""


def test_arp_ip_match_is_not_a_prefix_match(monkeypatch):
    """10.0.0.5 must not match the 10.0.0.55 line."""
    _fake_arp(monkeypatch, f"? (10.0.0.55) at 99:99:99:99:99:99 on en0\n")
    assert T._arp_mac("10.0.0.5") == ""


def test_arp_survives_a_missing_arp_binary(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("no arp here")

    monkeypatch.setattr(subprocess, "run", boom)
    assert T._arp_mac(IP) == ""


# ── persistence ───────────────────────────────────────────────────────────


def test_discovered_mac_is_written_back(monkeypatch, tmp_path):
    """Once the ARP entry expires there is no way left to learn the address —
    which is exactly when a wake is needed, so it has to be persisted."""
    path = tmp_path / "tv_token.json"
    path.write_text('{"ip": "%s", "token": "secret"}' % IP, encoding="utf-8")
    monkeypatch.setattr(T, "_token_path", lambda: str(path))
    monkeypatch.setattr(T, "_arp_mac", lambda ip: MAC)

    assert T._tv_mac() == MAC

    import json as _json
    saved = _json.loads(path.read_text(encoding="utf-8"))
    assert saved["mac"] == MAC
    assert saved["token"] == "secret", "writing the MAC must not lose the token"
    assert saved["ip"] == IP


# ── power-on through standby ──────────────────────────────────────────────


def test_power_on_wakes_then_retries(monkeypatch, creds):
    """The keypress cannot land while the API is down, so wake comes first."""
    creds(mac=MAC)
    calls = {"press": 0, "wake": 0}

    def fake_request(*a, **k):
        calls["press"] += 1
        # Asleep for the first attempt, awake once the packet has landed.
        if calls["press"] == 1:
            return {"_standby": True, "_error": "timed out"}
        return {"STATUS": {"RESULT": "SUCCESS"}}

    monkeypatch.setattr(T, "_request", fake_request)
    monkeypatch.setattr(T, "wake", lambda: calls.__setitem__("wake", 1) or
                        {"output": "sent", "exit_code": 0})
    monkeypatch.setattr(T, "_WAKE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("time.sleep", lambda s: None)

    res = T.power("on")
    assert calls["wake"] == 1, "power on did not attempt a wake"
    assert calls["press"] >= 2, "power on did not retry after waking"
    assert res["exit_code"] == 0


def test_power_off_does_not_wake(monkeypatch, creds):
    """Waking a TV in order to turn it off would be absurd."""
    creds(mac=MAC)
    woke = []

    monkeypatch.setattr(T, "_request",
                        lambda *a, **k: {"_standby": True, "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: woke.append(1) or {"exit_code": 0})
    monkeypatch.setattr("time.sleep", lambda s: None)

    res = T.power("off")
    assert not woke
    assert res["exit_code"] == 1


def test_power_on_reports_honestly_when_wake_is_unconfirmed(monkeypatch, creds):
    """If the API never comes up we say so rather than claiming success."""
    creds(mac=MAC)
    monkeypatch.setattr(T, "_request",
                        lambda *a, **k: {"_standby": True, "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: {"output": "sent", "exit_code": 0})
    monkeypatch.setattr(T, "_WAKE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(T, "_WAKE_ATTEMPTS", 2)
    monkeypatch.setattr("time.sleep", lambda s: None)

    res = T.power("on")
    low = res["output"].lower()
    assert "did not come up" in low or "still be turning on" in low


def test_power_on_surfaces_a_wake_failure(monkeypatch, creds):
    creds()
    monkeypatch.setattr(T, "_request",
                        lambda *a, **k: {"_standby": True, "_error": "timed out"})
    monkeypatch.setattr(T, "wake",
                        lambda: {"error": "mac unknown", "exit_code": 1})
    res = T.power("on")
    assert res["exit_code"] == 1
    assert "mac unknown" in res["error"]


# ── plumbing ──────────────────────────────────────────────────────────────


def _tv_schema():
    import src.agent_tools  # noqa: F401  — settle the import cycle first
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    return next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                if s.get("function", {}).get("name") == "tv_control")


# The dispatcher and the schema live in different files (agent_tools/tv_control.py
# and tool_schemas.py). Both `current_app` and `wake` were implemented and
# dispatched while the enum still said otherwise, so the model could not call
# them — including the one action documented as the way to correct a wrong app
# id. An explicit list here means adding an action without advertising it fails.
EXPECTED_ACTIONS = {"info", "power", "volume", "input", "launch",
                    "current_app", "wake"}
EXPECTED_APPS = {"youtube", "netflix", "prime", "hulu", "disney", "max", "hbo"}


def test_schema_advertises_every_action():
    assert set(_tv_schema()["parameters"]["properties"]["action"]["enum"]) == \
        EXPECTED_ACTIONS


def test_schema_advertises_every_app_in_the_table():
    """A launchable app the schema omits is unreachable, table entry or not."""
    assert set(_tv_schema()["parameters"]["properties"]["app"]["enum"]) == \
        EXPECTED_APPS
    assert set(T._DEFAULT_APPS) == EXPECTED_APPS


def test_every_advertised_action_is_dispatched():
    """Advertising an action the dispatcher rejects is the mirror-image bug."""
    import inspect

    src = inspect.getsource(T)
    for action in EXPECTED_ACTIONS:
        assert f'"{action}"' in src, f"action {action!r} is never dispatched"
