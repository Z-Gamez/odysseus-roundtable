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


def test_handshake_timeout_is_reported_as_standby(monkeypatch, creds):
    """A stalled handshake must be distinguishable from a bad address."""
    creds()

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(T, "urlopen", boom, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", boom)

    out = T._request(IP, 7345, "/state/device/deviceinfo")
    assert out.get("_standby") is True


def test_connection_refused_is_not_standby(monkeypatch, creds):
    """A refused port means nothing is listening at all — not a sleeping TV."""
    creds()

    def boom(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = T._request(IP, 9000, "/state/device/deviceinfo")
    assert not out.get("_standby")
    assert out.get("_error")


def test_standby_error_message_points_at_the_fix():
    """The message has to name the action that recovers, not just the symptom."""
    res = T._result({"_standby": True, "_error": "timed out"}, "read info")
    assert res["exit_code"] == 1
    low = res["error"].lower()
    assert "standby" in low
    assert "wake-on-lan" in low
    assert "power" in low


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
