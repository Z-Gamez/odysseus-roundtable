"""Port selection for the desktop launchers (AirPlay squatter regression).

macOS AirPlay Receiver listens on port 7000 and answers 403 to everything.
The old "any HTTP response = server up" probes mistook it for a running
Odysseus (white screen on fresh boot), and even an identity-aware probe
alone would just fail to bind 7000. _choose_server_port must walk past a
squatted port to the first one that is ours or genuinely free.
"""
from standalone_app import _choose_server_port


def _preds(ours=(), free=()):
    return (lambda p: p in ours), (lambda p: p in free)


def test_warm_odysseus_on_wanted_port_is_reused():
    is_ours, is_free = _preds(ours={7000})
    assert _choose_server_port(7000, is_ours, is_free) == (7000, False)


def test_airplay_squats_7000_falls_to_7001():
    # 7000: not ours, not bindable (AirPlay holds it) -> spawn on 7001.
    is_ours, is_free = _preds(ours=set(), free={7001, 7002})
    assert _choose_server_port(7000, is_ours, is_free) == (7001, True)


def test_warm_fallback_from_previous_launch_is_found():
    # Relaunch after an AirPlay fallback: 7000 squatted, our warm server
    # already lives on 7001 -> reuse it, no second spawn.
    is_ours, is_free = _preds(ours={7001}, free={7002})
    assert _choose_server_port(7000, is_ours, is_free) == (7001, False)


def test_free_wanted_port_spawns_there():
    is_ours, is_free = _preds(ours=set(), free={7000})
    assert _choose_server_port(7000, is_ours, is_free) == (7000, True)


def test_everything_blocked_still_returns_wanted():
    is_ours, is_free = _preds()
    assert _choose_server_port(7000, is_ours, is_free) == (7000, True)
