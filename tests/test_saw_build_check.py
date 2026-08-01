"""Tests for the deterministic build/structure gate (_build_check).

This gate is what stops a weak QAS from false-passing obviously-broken work
(e.g. a Flutter project created by `flutter create` whose lib/main.dart never
got written). We test the cheap structural checks that don't need a toolchain,
plus the toolchain paths with subprocess stubbed.
"""
import subprocess

from src.saw import orchestrator as orch


def test_flutter_missing_main_dart_fails(tmp_path):
    # A Flutter project (pubspec.yaml) with no lib/main.dart must FAIL the gate.
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    ok, label, detail = orch._build_check(str(tmp_path))
    assert ok is False
    assert "main.dart" in detail


def test_flutter_trivial_main_dart_fails(tmp_path):
    # main.dart present but empty / no runApp() — still incomplete (fails before
    # ever reaching `flutter analyze`, so no toolchain is required).
    (tmp_path / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "main.dart").write_text("// todo\n", encoding="utf-8")
    ok, label, detail = orch._build_check(str(tmp_path))
    assert ok is False


def test_flutter_project_in_subfolder_is_found(tmp_path):
    # The model often runs `flutter create my_app`, which scaffolds into a
    # <workspace>/my_app/ subfolder. The gate must still find and validate it
    # there (previously this skipped the gate entirely -> QAS could false-pass).
    proj = tmp_path / "my_app"
    (proj / "lib").mkdir(parents=True)
    (proj / "pubspec.yaml").write_text("name: my_app\n", encoding="utf-8")
    # Missing entry point in the subfolder must still FAIL, and the detail should
    # point at the subfolder so the Dev knows where the real project root is.
    ok, label, detail = orch._build_check(str(tmp_path))
    assert ok is False
    assert "my_app/lib/main.dart" in detail


def test_python_compiles(tmp_path):
    (tmp_path / "add.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    res = orch._build_check(str(tmp_path))
    assert res is not None
    ok, label, detail = res
    assert ok is True
    assert label == "py_compile"


def test_python_syntax_error_fails(tmp_path):
    (tmp_path / "bad.py").write_text("def add(a, b)\n    return a + b\n", encoding="utf-8")
    ok, label, detail = orch._build_check(str(tmp_path))
    assert ok is False


def test_unknown_project_returns_none(tmp_path):
    (tmp_path / "readme.txt").write_text("hi\n", encoding="utf-8")
    assert orch._build_check(str(tmp_path)) is None


# ── dependency resolution failures ─────────────────────────────────────────
#
# A wrong flame/SDK pin makes `flutter analyze` exit 1 with "version solving
# failed" and NOT ONE line that starts with "error". The gate only scanned for
# those lines, so a project that could not resolve its packages — let alone
# build or launch — was reported as a clean pass and the run completed. Real
# output, reproduced against Flutter with `flame: ^0.10.0` on Dart 3.12.

_VERSION_SOLVING_OUTPUT = """Resolving dependencies...
The current Dart SDK version is 3.12.2.

Because flappyphone depends on flame >=0.2.0 <1.0.0-rc10 which doesn't support null safety, version solving failed.

You can try the following suggestion to make the pubspec resolve:
* Try upgrading your constraint on flame: flutter pub add flame:^1.38.0
Failed to update packages.
"""


def _flutter_project(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "main.dart").write_text(
        "import 'package:flutter/material.dart';\n"
        "void main() => runApp(const MaterialApp(home: Scaffold()));\n"
        "// padding to clear the minimum length check ------------------\n",
        encoding="utf-8")


class _Res:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_version_solving_failure_is_not_a_pass(tmp_path, monkeypatch):
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Res(1, _VERSION_SOLVING_OUTPUT))
    ok, label, detail = orch._build_check(str(tmp_path))
    assert ok is False, "a project that cannot resolve dependencies must not pass"
    assert "exited 1" in detail


def test_failure_detail_carries_flutters_own_remedy(tmp_path, monkeypatch):
    """flutter names the fix; handing it to the developer is the whole point of
    feeding the gate's detail back as feedback."""
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Res(1, _VERSION_SOLVING_OUTPUT))
    _, _, detail = orch._build_check(str(tmp_path))
    assert "flutter pub add flame:^1.38.0" in detail
    assert "Suggested fix:" in detail


def test_clean_analyze_still_passes(tmp_path, monkeypatch):
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Res(0, "No issues found!\n"))
    ok, label, _ = orch._build_check(str(tmp_path))
    assert ok is True
    assert label == "flutter analyze"


def test_error_lines_still_fail(tmp_path, monkeypatch):
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(
        1, "error - Undefined name 'foo' - lib/main.dart:3:5\n"))
    ok, _, detail = orch._build_check(str(tmp_path))
    assert ok is False
    assert "Undefined name" in detail


def test_missing_toolchain_gives_no_verdict_rather_than_a_pass(tmp_path, monkeypatch):
    """Returning True here meant any machine without Flutter passed every
    Flutter project automatically — the exact false-completion this gate is for."""
    def boom(*a, **k):
        raise FileNotFoundError("flutter not on PATH")
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run", boom)
    assert orch._build_check(str(tmp_path)) is None


def test_analyze_timeout_gives_no_verdict(tmp_path, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("flutter analyze", 360)
    _flutter_project(tmp_path)
    monkeypatch.setattr(subprocess, "run", slow)
    assert orch._build_check(str(tmp_path)) is None
