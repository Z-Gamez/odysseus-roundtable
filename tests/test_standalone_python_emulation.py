"""standalone_app python-interpreter emulation (frozen sys.executable).

In the packaged app sys.executable is Odysseus itself. The agent python
tool spawns `sys.executable -I -c <code>` and (formerly) the Round Table
build gate spawned `-m py_compile` — without emulation those fell through
the argv dispatch to the WINDOW role, opening a new GUI instance per call
(observed on the macOS build). These tests drive standalone_app.py from
source with the exact argv shapes the app produces.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(REPO, "standalone_app.py")


def _run(*args, timeout=60):
    return subprocess.run([sys.executable, ENTRY, *args],
                          capture_output=True, text=True,
                          cwd=REPO, timeout=timeout)


def test_dash_c_executes_code():
    r = _run("-c", "print('emulation-ok')")
    assert r.returncode == 0
    assert "emulation-ok" in r.stdout


def test_agent_python_tool_shape():
    # The exact shape PythonTool spawns: -I then -c.
    r = _run("-I", "-c", "import sys; print(len(sys.argv)); print('tool-ok')")
    assert r.returncode == 0
    assert "tool-ok" in r.stdout


def test_dash_c_nonzero_exit_on_exception():
    r = _run("-c", "raise SystemExit(3)")
    assert r.returncode == 3


def test_dash_m_py_compile_good_and_bad(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("def ok():\n    return 1\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")

    r = _run("-m", "py_compile", str(good))
    assert r.returncode == 0, r.stderr

    r = _run("-m", "py_compile", str(bad))
    assert r.returncode != 0
    assert "bad.py" in (r.stderr + r.stdout)


def test_unsupported_dash_args_error_instead_of_window():
    r = _run("-X", "whatever")
    assert r.returncode == 2
    assert "unsupported interpreter arguments" in r.stderr


def test_orchestrator_gate_compiles_in_process(tmp_path):
    # The build gate must not spawn sys.executable at all anymore.
    from src.saw.orchestrator import _build_check
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ok, kind, msg = _build_check(str(tmp_path))
    assert ok is True and kind == "py_compile"

    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    ok, kind, msg = _build_check(str(tmp_path))
    assert ok is False and kind == "py_compile"
    assert "bad.py" in msg
