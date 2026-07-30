"""Resilience of the TV control tool against a stalling SmartCast service.

A SmartCast TV intermittently stops answering HTTP while still completing TCP
handshakes. Observed on a powered-on set: ping up but jittery (6-913ms), TCP
connect to :7345 fine, every HTTPS request hung past 30s — and yet a small
PUT /key_command/ went through in 0.173s in that same window. Minutes later,
10/10 reads returned 200 in ~0.11s.

Two things follow, and both are pinned here:

  * A timeout is not a verdict. It is a stall that usually clears, so the tool
    retries rather than failing outright.
  * TCP-up/TLS-down does NOT mean standby. A stalled service is externally
    identical to a sleeping one, so calling it standby sent a wake to a TV that
    was already on. Only a landed key command is evidence either way.

Addresses here are generic — the real TV's IP and MAC stay out of the repo.
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


@pytest.fixture
def layers(monkeypatch):
    """Drive the TCP/TLS probes that the classifier measures."""

    def _set(tcp, tls):
        monkeypatch.setattr(T, "_tcp_ok", lambda *a: tcp)
        monkeypatch.setattr(T, "_tls_ok", lambda *a: tls)

    return _set


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


# ── classifying a failure ─────────────────────────────────────────────────


def test_tcp_up_but_no_answer_is_stalled_not_standby(layers):
    """The reported bug. A stalled service and a sleeping TV are identical at
    the TLS layer, so this must NOT be called standby — doing so is what kept
    sending a wake to a TV that was plainly switched on."""
    layers(tcp=True, tls=False)
    out = T._classify_failure(IP, 7345, TimeoutError("timed out"), attempts=3)
    assert out.get("_stalled") is True
    assert not out.get("_standby")
    assert out.get("_attempts") == 3
    assert out.get("_ip") == IP


def test_nothing_listening_is_unreachable(layers):
    """No TCP either — a stale address. A wake cannot help."""
    layers(tcp=False, tls=False)
    out = T._classify_failure(IP, 7345, TimeoutError("timed out"))
    assert out.get("_unreachable") is True
    assert not out.get("_stalled")


def test_a_landed_nudge_proves_the_service_is_alive(layers):
    """The only evidence that separates a stall from standby."""
    layers(tcp=True, tls=False)
    alive = T._classify_failure(IP, 7345, TimeoutError(), nudged=True,
                                nudge_ok=True)
    dead = T._classify_failure(IP, 7345, TimeoutError(), nudged=True,
                               nudge_ok=False)
    assert alive.get("_service_alive") is True
    assert not dead.get("_service_alive")


def test_diagnose_never_reports_standby(layers):
    """The middle state is "not_responding" precisely because the handshake
    cannot tell a stalled service from a sleeping one."""
    layers(tcp=True, tls=True)
    assert T.diagnose(IP, 7345) == "awake"
    layers(tcp=True, tls=False)
    assert T.diagnose(IP, 7345) == "not_responding"
    layers(tcp=False, tls=False)
    assert T.diagnose(IP, 7345) == "unreachable"


def test_tcp_probe_outlasts_a_slow_standby_connect():
    """A standby connect took 2.4s. If the probe gives up first, a reachable TV
    is misreported as gone."""
    assert T._PROBE_TIMEOUT >= 5.0


def test_per_attempt_timeout_is_short():
    """13s of dead air per call was the complaint; retries need a small budget
    each so the worst case stays reasonable."""
    assert T._TIMEOUT <= 5.0
    assert len(T._RETRY_DELAYS) >= 2


# ── the three distinct faults get three distinct messages ─────────────────


def test_stalled_message_with_live_service_forbids_a_wake():
    res = T._result({"_stalled": True, "_ip": IP, "_attempts": 3,
                     "_service_alive": True, "_error": "timed out"}, "launch")
    assert res["exit_code"] == 1
    low = res["error"].lower()
    assert "not standby" in low
    assert "do not send a wake" in low
    assert "3 attempts" in low


def test_stalled_message_without_evidence_stays_agnostic():
    """With nothing landed we genuinely do not know, so assert neither."""
    res = T._result({"_stalled": True, "_ip": IP, "_attempts": 3,
                     "_service_alive": False, "_error": "timed out"}, "launch")
    low = res["error"].lower()
    assert "not responding" in low
    assert "stalled or the tv is asleep" in low


def test_unreachable_message_says_a_wake_will_not_help():
    res = T._result({"_unreachable": True, "_ip": IP, "_error": "timed out"},
                    "read info")
    low = res["error"].lower()
    assert "not standby" in low
    assert "tv_token.json" in low


# ── retry and nudge orchestration ─────────────────────────────────────────


def test_a_stall_that_clears_succeeds_instead_of_failing(monkeypatch, no_sleep):
    """The whole point: a single timeout must not fail the tool."""
    calls = []

    def attempt(ip, port, path, method, body, token, timeout):
        calls.append(path)
        if len(calls) == 1:
            return "fail", TimeoutError("timed out")
        return "ok", {"STATUS": {"RESULT": "SUCCESS"}}

    monkeypatch.setattr(T, "_attempt", attempt)
    # TCP is up during a stall — that is the whole point of the signature.
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    out = T._request(IP, 7345, "/app/launch", method="PUT", body={})
    assert out == {"STATUS": {"RESULT": "SUCCESS"}}
    assert len(calls) == 2


def test_retries_are_bounded(monkeypatch, no_sleep):
    calls = []

    def attempt(*a, **k):
        calls.append(1)
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    out = T._request(IP, 7345, "/app/current")
    expected = 1 + len(T._RETRY_DELAYS)
    # The nudge rides on the same hook, so allow for one extra call.
    assert expected <= len(calls) <= expected + 1
    assert out.get("_stalled") is True
    assert out["_attempts"] == expected


def test_a_dead_address_is_not_retried(monkeypatch, no_sleep):
    """Retrying an address with no TCP at all only burns the whole budget to
    reach the same answer — which made this path slower than the single long
    timeout it replaced."""
    calls = []

    def attempt(*a, **k):
        calls.append(1)
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: False)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    out = T._request(IP, 7345, "/app/current")
    assert len(calls) == 1, "a dead address must not be retried"
    assert out.get("_unreachable") is True
    assert out["_attempts"] == 1


def test_an_http_error_is_not_retried(monkeypatch, no_sleep):
    """A real HTTP answer is not a stall; hammering it changes nothing."""
    calls = []

    def attempt(*a, **k):
        calls.append(1)
        return "http", {"_http_error": 403, "_detail": "denied"}

    monkeypatch.setattr(T, "_attempt", attempt)
    out = T._request(IP, 7345, "/app/launch", method="PUT", body={})
    assert len(calls) == 1
    assert out["_http_error"] == 403


def test_a_nudge_is_sent_before_the_last_attempt(monkeypatch, no_sleep):
    seen = []

    def attempt(ip, port, path, method, body, token, timeout):
        seen.append((path, body))
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    T._request(IP, 7345, "/app/current")
    nudges = [b for p, b in seen if p == "/key_command/"]
    assert nudges, "no nudge was attempted"
    assert nudges[0] == T._NUDGE_BODY


def test_nudge_can_be_suppressed(monkeypatch, no_sleep):
    seen = []

    def attempt(ip, port, path, method, body, token, timeout):
        seen.append(path)
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    T._request(IP, 7345, "/app/current", allow_nudge=False)
    assert "/key_command/" not in seen


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
    assert T.wake()["exit_code"] == 1
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
              "? (10.0.0.1) at 11:22:33:44:55:66 on en0\n"
              "? (10.0.0.99) at 99:99:99:99:99:99 on en0\n")
    assert T._arp_mac(IP) == ""


def test_arp_ip_match_is_not_a_prefix_match(monkeypatch):
    """10.0.0.5 must not match the 10.0.0.55 line."""
    _fake_arp(monkeypatch, "? (10.0.0.55) at 99:99:99:99:99:99 on en0\n")
    assert T._arp_mac("10.0.0.5") == ""


def test_arp_survives_a_missing_arp_binary(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("no arp here")

    monkeypatch.setattr(subprocess, "run", boom)
    assert T._arp_mac(IP) == ""


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


# ── power ─────────────────────────────────────────────────────────────────


def test_power_on_reports_success_when_the_nudge_landed(monkeypatch, creds):
    """The nudge IS a power-on keypress, so the work is already done and
    reporting a failure would be wrong."""
    creds(mac=MAC)
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_nudge_ok": True, "_service_alive": True,
        "_attempts": 3, "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: pytest.fail("must not wake"))
    res = T.power("on")
    assert res["exit_code"] == 0
    assert "stalled" in res["output"].lower()


def test_power_on_only_wakes_when_nothing_landed(monkeypatch, creds, no_sleep):
    creds(mac=MAC)
    woke = []
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_nudge_ok": False,
        "_service_alive": False, "_attempts": 3, "_error": "timed out"})
    monkeypatch.setattr(T, "wake",
                        lambda: woke.append(1) or {"output": "sent",
                                                   "exit_code": 0})
    monkeypatch.setattr(T, "power_state", lambda *a: None)
    monkeypatch.setattr(T, "_BOOT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(T, "_BOOT_POLL_SECONDS", 0)
    T.power("on")
    assert woke, "a wake is still the right last resort when nothing lands"


def test_power_off_is_never_nudged(monkeypatch, no_sleep, creds):
    """The nudge is a power-on keypress; sending it while turning the TV off
    would switch it straight back on."""
    creds(mac=MAC)
    seen = []

    def attempt(ip, port, path, method, body, token, timeout):
        seen.append(body)
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda *a, **k: 0)
    monkeypatch.setattr(T, "wake", lambda: pytest.fail("must not wake on off"))
    T.power("off")
    assert T._NUDGE_BODY not in seen


def test_power_off_does_not_wake(monkeypatch, creds, no_sleep):
    """Waking a TV in order to turn it off would be absurd."""
    creds(mac=MAC)
    woke = []
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_service_alive": False, "_attempts": 3,
        "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: woke.append(1) or {"exit_code": 0})
    res = T.power("off")
    assert not woke
    assert res["exit_code"] == 1


def test_power_on_reports_honestly_when_wake_is_unconfirmed(monkeypatch, creds,
                                                           no_sleep):
    """If the API never comes up we say so rather than claiming success."""
    creds(mac=MAC)
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_service_alive": False, "_attempts": 3,
        "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: {"output": "sent", "exit_code": 0})
    monkeypatch.setattr(T, "power_state", lambda *a: None)
    monkeypatch.setattr(T, "_BOOT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(T, "_BOOT_POLL_SECONDS", 0)
    res = T.power("on")
    low = res["output"].lower()
    assert "did not respond" in low
    assert "stalled" in low or "starting" in low


def test_power_on_surfaces_a_wake_failure(monkeypatch, creds, no_sleep):
    creds()
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_service_alive": False, "_attempts": 3,
        "_error": "timed out"})
    monkeypatch.setattr(T, "wake",
                        lambda: {"error": "mac unknown", "exit_code": 1})
    res = T.power("on")
    assert res["exit_code"] == 1
    assert "mac unknown" in res["error"]


# ── coming back from a cold boot ──────────────────────────────────────────
#
# The case that "failed no matter how long". A cold SmartCast boot takes far
# longer than a wake from standby, and the old confirm loop judged success on an
# AUTHENTICATED keypress — which cannot land until the service is fully up. So
# it reported failure on a TV that was already coming on, indefinitely.


def test_power_state_reads_without_a_token(monkeypatch):
    """Verified to answer HTTP 200 with no AUTH header, which is what makes it
    usable while a token is missing, stale, or the service is still booting."""
    seen = {}

    def attempt(ip, port, path, method, body, token, timeout):
        seen["token"] = token
        seen["path"] = path
        return "ok", {"ITEMS": [{"VALUE": 1}]}

    monkeypatch.setattr(T, "_attempt", attempt)
    assert T.power_state(IP, 7345) == 1
    assert seen["token"] == "", "the power read must not depend on pairing"
    assert seen["path"] == "/state/device/power_mode"


def test_power_state_is_none_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(T, "_attempt",
                        lambda *a, **k: ("fail", TimeoutError()))
    assert T.power_state(IP, 7345) is None


def test_boot_is_confirmed_by_the_power_read_not_a_keypress(monkeypatch, creds,
                                                            no_sleep):
    """The keypress may keep failing while the service starts; the tokenless
    read is what tells us the TV is actually on."""
    creds(mac=MAC)
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_service_alive": False, "_attempts": 3,
        "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: {"output": "sent", "exit_code": 0})

    states = [None, None, 1]
    monkeypatch.setattr(T, "power_state", lambda *a: states.pop(0))
    monkeypatch.setattr(T, "_BOOT_POLL_SECONDS", 0)

    res = T.power("on")
    assert res["exit_code"] == 0
    assert "the tv is on" in res["output"].lower()


def test_boot_wait_is_long_enough_for_a_cold_start():
    """~24s was the old budget and it expired mid-boot."""
    assert T._BOOT_WAIT_SECONDS >= 60


def test_boot_timeout_points_at_the_diagnose_action(monkeypatch, creds,
                                                    no_sleep):
    creds(mac=MAC)
    monkeypatch.setattr(T, "_request", lambda *a, **k: {
        "_stalled": True, "_ip": IP, "_service_alive": False, "_attempts": 3,
        "_error": "timed out"})
    monkeypatch.setattr(T, "wake", lambda: {"output": "sent", "exit_code": 0})
    monkeypatch.setattr(T, "power_state", lambda *a: None)
    monkeypatch.setattr(T, "_BOOT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(T, "_BOOT_POLL_SECONDS", 0)
    res = T.power("on")
    assert "diagnose" in res["output"].lower()


# ── the API moving ports ──────────────────────────────────────────────────


def test_find_api_port_skips_the_named_port(monkeypatch):
    tried = []

    def read(ip, port, path, timeout=3.0):
        tried.append(port)
        return {"ITEMS": [{"VALUE": 1}]} if port == 9000 else None

    monkeypatch.setattr(T, "_unauth_read", read)
    assert T._find_api_port(IP, skip=7345) == 9000
    assert 7345 not in tried


def test_a_moved_port_is_retried_and_remembered(monkeypatch, no_sleep):
    """Only device_info ever probed both ports; commands pinned the stored one,
    so an API that came up elsewhere failed every command indefinitely."""
    saved = {}
    monkeypatch.setattr(T, "_remember", lambda **kw: saved.update(kw))
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_find_api_port", lambda ip, skip=0: 9000)

    def attempt(ip, port, path, method, body, token, timeout):
        if port == 9000:
            return "ok", {"STATUS": {"RESULT": "SUCCESS"}}
        return "fail", TimeoutError("timed out")

    monkeypatch.setattr(T, "_attempt", attempt)
    out = T._request(IP, 7345, "/app/current")
    assert out == {"STATUS": {"RESULT": "SUCCESS"}}
    assert saved.get("port") == 9000, "the working port must be persisted"


def test_port_self_heal_does_not_recurse(monkeypatch, no_sleep):
    """If neither port works, the retry must not loop forever."""
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    monkeypatch.setattr(T, "_remember", lambda **kw: None)
    monkeypatch.setattr(T, "_find_api_port", lambda ip, skip=0: 9000)
    monkeypatch.setattr(T, "_attempt",
                        lambda *a, **k: ("fail", TimeoutError("timed out")))
    out = T._request(IP, 7345, "/app/current")
    assert out.get("_stalled") is True


def test_remember_preserves_the_token(monkeypatch, tmp_path):
    path = tmp_path / "tv_token.json"
    path.write_text('{"ip": "%s", "token": "secret", "port": 7345}' % IP,
                    encoding="utf-8")
    monkeypatch.setattr(T, "_token_path", lambda: str(path))
    T._remember(port=9000)

    import json as _json
    saved = _json.loads(path.read_text(encoding="utf-8"))
    assert saved["port"] == 9000
    assert saved["token"] == "secret"


# ── diagnose ──────────────────────────────────────────────────────────────


def test_diagnose_flags_a_stored_port_that_is_wrong(monkeypatch, creds):
    creds(port=7345)
    monkeypatch.setattr(T, "_tcp_ok", lambda ip, port: True)
    monkeypatch.setattr(T, "_tls_ok", lambda ip, port: port == 9000)
    monkeypatch.setattr(T, "_unauth_read",
                        lambda ip, port, path, timeout=3.0:
                        {"ITEMS": [{"VALUE": {"MODEL_NAME": "V-TEST"}}]}
                        if port == 9000 else None)
    monkeypatch.setattr(T, "power_state", lambda ip, port: 1)

    import json as _json
    rep = _json.loads(T.diagnostics()["output"])
    assert "9000" in rep["verdict"]
    assert "stored" in rep["verdict"].lower()


def test_diagnose_distinguishes_dead_from_stalled(monkeypatch, creds):
    creds()
    monkeypatch.setattr(T, "_tls_ok", lambda *a: False)
    monkeypatch.setattr(T, "_unauth_read", lambda *a, **k: None)

    monkeypatch.setattr(T, "_tcp_ok", lambda *a: True)
    import json as _json
    stalled = _json.loads(T.diagnostics()["output"])["verdict"]
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: False)
    dead = _json.loads(T.diagnostics()["output"])["verdict"]

    assert "stalled" in stalled or "booting" in stalled
    assert "nothing answers" in dead


def test_diagnose_never_leaks_the_token(monkeypatch, creds):
    """It reports whether a token exists, never its value."""
    creds(token="super-secret-token")
    monkeypatch.setattr(T, "_tcp_ok", lambda *a: False)
    monkeypatch.setattr(T, "_tls_ok", lambda *a: False)
    monkeypatch.setattr(T, "_unauth_read", lambda *a, **k: None)
    out = T.diagnostics()["output"]
    assert "super-secret-token" not in out
    assert '"has_token": true' in out


# ── launch readback ───────────────────────────────────────────────────────


def test_namespace_2_and_4_are_equivalent():
    """The TV normalises NAME_SPACE 2 to 4 on readback, so treating that as a
    mismatch would report failure on a launch that actually worked."""
    assert T._ns_equal(2, 4)
    assert T._ns_equal(4, 2)
    assert T._ns_equal(3, 3)
    assert not T._ns_equal(3, 5)


def test_launch_rejects_a_mismatched_readback(monkeypatch, creds, no_sleep):
    """/app/launch returns STATUS SUCCESS even when nothing launches, so the
    only honest check is reading back what is actually running."""
    creds()
    calls = []

    def fake_request(ip, port, path, **k):
        calls.append(path)
        if path == "/app/current":
            # Something else entirely is on screen.
            return {"ITEM": {"VALUE": {"APP_ID": "99", "NAME_SPACE": 3}}}
        return {"STATUS": {"RESULT": "SUCCESS"}}

    monkeypatch.setattr(T, "_request", fake_request)
    res = T.launch_app("netflix")
    assert res["exit_code"] == 1
    assert "99" in res["error"]
    assert "tv_apps.json" in res["error"]


def test_launch_accepts_a_matching_readback(monkeypatch, creds, no_sleep):
    creds()
    cfg = T._app_table()["netflix"]

    def fake_request(ip, port, path, **k):
        if path == "/app/current":
            return {"ITEM": {"VALUE": {"APP_ID": cfg["APP_ID"],
                                       "NAME_SPACE": cfg["NAME_SPACE"]}}}
        return {"STATUS": {"RESULT": "SUCCESS"}}

    monkeypatch.setattr(T, "_request", fake_request)
    res = T.launch_app("netflix")
    assert res["exit_code"] == 0


# ── schema / dispatcher parity ────────────────────────────────────────────


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
                    "current_app", "wake", "diagnose"}
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
