"""Tests for the deterministic build/structure gate (_build_check).

This gate is what stops a weak QAS from false-passing obviously-broken work
(e.g. a Flutter project created by `flutter create` whose lib/main.dart never
got written). We test the cheap structural checks that don't need a toolchain.
"""
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
