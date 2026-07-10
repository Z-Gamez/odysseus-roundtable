"""Regression tests for the Windows Git Bash command rewrites.

MSYS treats a drive path behind any prefix ("./C:/x", "$PWD/C:/x", a
workspace-joined "C:/ws/C:/x", or a quoted backslash path) as literal
components, so mkdir -p materializes a junk tree of reserved-char "C:"
dirs inside the workspace. Observed twice in Round Table runs (vamp,
ragdoll rt_163dedc3ba84) before rewrite_for_win_bash grew the re-anchor
pass. These tests pin the rewrite behavior without needing bash itself.
"""
import pytest

from src.agent_tools.subprocess_tools import rewrite_for_win_bash


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # --- the incident shapes: buried drive paths get re-anchored ---
        (
            "mkdir -p ./C:/Odysseus/saw-sandbox/ragdoll/assets",
            "mkdir -p C:/Odysseus/saw-sandbox/ragdoll/assets",
        ),
        (
            "mkdir -p $PWD/C:/Odysseus/x",
            "mkdir -p C:/Odysseus/x",
        ),
        (   # workspace joined onto itself (what nested <ws>/C:/.../<ws>)
            "mkdir -p C:/Odysseus/saw-sandbox/ragdoll/C:/Odysseus/saw-sandbox/ragdoll/js",
            "mkdir -p C:/Odysseus/saw-sandbox/ragdoll/js",
        ),
        ("mkdir -p /C:/Odysseus/x", "mkdir -p C:/Odysseus/x"),
        ("mkdir -p ../C:/Odysseus/x", "mkdir -p C:/Odysseus/x"),
        ("mkdir -p ~/C:/Odysseus/x", "mkdir -p C:/Odysseus/x"),
        (   # stacked prefixes collapse to a fixpoint
            'mkdir -p "C:/ws/C:/ws/C:/real/x"',
            'mkdir -p "C:/real/x"',
        ),
        (   # backslash flavor of the buried path (both passes chain)
            "mkdir -p ./C:\\Odysseus\\saw-sandbox\\ragdoll\\css",
            "mkdir -p C:/Odysseus/saw-sandbox/ragdoll/css",
        ),
        # --- plain backslash drive paths become forward-slash ---
        (
            'mkdir -p "C:\\Odysseus\\saw-sandbox\\x"',
            'mkdir -p "C:/Odysseus/saw-sandbox/x"',
        ),
        # --- cmd-style null redirect ---
        ("ls missing 2>nul", "ls missing 2>/dev/null"),
    ],
)
def test_rewrites(cmd, expected):
    assert rewrite_for_win_bash(cmd) == expected


@pytest.mark.parametrize(
    "cmd",
    [
        # sed expressions keep their slashes
        "sed s/C:/D:/ file.txt",
        'sed -e "s/C:/D:/g" f',
        'sed -e "s|C:/old|C:/new|" f',
        # URLs untouched
        "curl https://example.com/a/b",
        "git clone https://github.com/u/r.git C:/Odysseus/dest",
        # MSYS-style and normal paths untouched
        "ls /c/Odysseus/saw-sandbox",
        "mkdir -p C:/Odysseus/normal/path",
        "cd C:/Odysseus/saw-sandbox/ragdoll && mkdir -p js css assets",
        # env vars with colons
        "echo PATH=$PATH:/usr/bin",
        # code strings with clean drive paths
        "python -c \"open('C:/ws/f.txt')\"",
        "awk \"{print $1}\" C:/ws/f.txt",
    ],
)
def test_leaves_valid_commands_alone(cmd):
    assert rewrite_for_win_bash(cmd) == cmd
