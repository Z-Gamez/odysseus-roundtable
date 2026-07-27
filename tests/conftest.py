"""Shared test configuration - ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing core.database below runs init_db() at import time, and its default
# (sqlite:///./data/app.db) can't be opened in a clean worktree because SQLite
# won't create the missing ./data parent dir - pytest then dies during
# collection, before any test module loads. Default to an in-memory DB for the
# test session so collection is deterministic and writes no repo-local
# artifacts. An explicit DATABASE_URL (a real test/CI database) is preserved.
# This only unblocks collection/import-time init; it does not provide a shared
# file-backed DB across processes - tests needing that must set DATABASE_URL.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Pre-import real heavy modules BEFORE any test file's module-level stubs can
# replace them with MagicMock. Some test files (e.g. test_llm_core_sanitize_*)
# stub sqlalchemy/core.database at module scope with `if mod not in sys.modules`,
# which fires during collection. If the real module hasn't been imported yet,
# the stub wins and contaminates every subsequent test that needs the real ORM.
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm  # noqa: F401
    import core.database  # noqa: F401
    import src.database
except ImportError:
    pass  # not installed - the stubs below will handle it

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db

# Pre-import core.models before test_agent_loop.py's module-level stubs
# run (it replaces sys.modules['core.models'] with a MagicMock during
# collection, which breaks session import in subsequent tests).
import core.models  # noqa: E402

def pytest_configure(config):
    """Register the dynamic taxonomy ``sub_*`` markers before collection.

    The stable ``area_*`` markers are declared in ``pyproject.toml``. The
    per-file ``sub_*`` markers are derived from the test filenames here so that
    unknown-mark warnings still surface genuine typos outside the taxonomy. This
    only registers marker names; it imports no production module.
    """
    import pathlib
    from tests._taxonomy import discover_markers

    tests_dir = pathlib.Path(__file__).parent
    paths = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
    for marker_name in discover_markers(paths):
        if marker_name.startswith("sub_"):
            config.addinivalue_line("markers", f"{marker_name}: taxonomy sub-area marker")


def pytest_collection_modifyitems(config, items):
    """Tag each collected test with its taxonomy ``area_*`` and ``sub_*`` markers.

    Collection-time only: this adds markers and nothing else. It does not skip,
    reorder, or deselect tests, mutate fixtures or the environment, or import any
    production module. See ``tests/_taxonomy.py`` for the classification rules.
    """
    import pytest
    from tests._taxonomy import markers_for_path

    for item in items:
        path = getattr(item, "path", None) or item.fspath
        for marker_name in markers_for_path(path):
            item.add_marker(getattr(pytest.mark, marker_name))


try:  # once, at collection time — never during a test's setup
    import src.settings as _settings
except Exception:  # settings unavailable in this environment; skip the patch
    _settings = None


@pytest.fixture(autouse=True)
def _neutral_chat_bar_toggles():
    """Keep the developer's UI toggles out of the test run.

    `fast_mode` is a chat-bar switch stored in user settings, and stream_llm
    reads it directly: when on it appends /no_think AND turns reasoning off via
    reasoning_effort / chat_template_kwargs. That means a developer who happens
    to have Fast Mode enabled sees different request payloads than one who
    doesn't, and payload-shape assertions fail for reasons unrelated to the
    change under test. (Caught exactly that way — two suppression tests started
    failing purely because the toggle was on.)

    Default it off so payload tests are deterministic. Tests that care about
    Fast Mode set it explicitly and override this.

    Deliberately does NOT request pytest's `monkeypatch`. Requesting it here
    changes when pytest instantiates it, which reorders teardown for every test
    in the run: tests/test_upload_limits_centralized.py has an autouse fixture
    that reloads a module on teardown, and with monkeypatch finalising later
    that reload re-read an intentionally-invalid env var and raised. Save and
    restore the attribute by hand so this fixture stays invisible to ordering.
    """
    if _settings is None:
        yield
        return

    _real = _settings.get_setting

    def _patched(key, default=None):
        if key == "fast_mode":
            return False
        return _real(key, default)

    _settings.get_setting = _patched
    try:
        yield
    finally:
        _settings.get_setting = _real


# ---------------------------------------------------------------------------
# Platform-conditional skips
# ---------------------------------------------------------------------------
# These tests assert POSIX-only behaviour or need a binary/kernel feature that
# does not exist on this platform. They were failing rather than skipping, which
# buried any REAL regression in a wall of 39 expected failures.
#
# Every entry names why. Kept in one table rather than scattered across 15 files
# so the platform gap is visible and can be shrunk deliberately — an entry that
# stops being true should be deleted, not left to rot.
#
# NOTE: skipping is for tests that cannot be meaningful here. It is not a way to
# silence a genuine failure — anything whose cause was not identified is left
# failing on purpose.

import sys as _sys
import shutil as _shutil

_IS_WIN = _sys.platform == "win32"
_NO_TMUX = _shutil.which("tmux") is None
_NO_NPX = _shutil.which("npx") is None

# test id fragment -> (condition, reason)
_PLATFORM_SKIPS = {
    # _is_sensitive_path splits on os.sep; these call it directly with POSIX
    # separators, which on Windows yields one path component and no match.
    # Production is unaffected: _resolve_tool_path realpaths first, and real
    # Windows paths in both slash styles are correctly blocked.
    "test_tool_path_confinement.py::test_sensitive_ssh_dir":
        (_IS_WIN, "POSIX separators passed straight to _is_sensitive_path"),
    "test_tool_path_confinement.py::test_sensitive_gnupg_dir":
        (_IS_WIN, "POSIX separators passed straight to _is_sensitive_path"),
    "test_tool_path_confinement.py::test_sensitive_shell_rc":
        (_IS_WIN, "POSIX separators passed straight to _is_sensitive_path"),
    "test_tool_path_confinement.py::test_sensitive_key_filenames":
        (_IS_WIN, "POSIX separators passed straight to _is_sensitive_path"),
    # The allowlist carries /tmp and $TMPDIR, both POSIX-only. Windows %TEMP%
    # is deliberately NOT on it — widening the roots is a security decision,
    # not a test fix.
    "test_tool_path_confinement.py::test_allows_tmp":
        (_IS_WIN, "%TEMP% is intentionally not on the Windows allowlist"),

    # socket.AF_UNIX does not exist on Windows.
    "test_cookbook_docker_access.py::test_container_opt_in_with_unix_socket_is_allowed":
        (_IS_WIN, "no socket.AF_UNIX on Windows"),
    "test_cookbook_docker_access.py::test_local_container_serve_allows_generated_docker_exec_when_enabled":
        (_IS_WIN, "no socket.AF_UNIX on Windows"),
    "test_shell_routes.py::TestHostDockerAccess":
        (_IS_WIN, "no socket.AF_UNIX on Windows"),

    # Apple Silicon / Metal detection on a CUDA machine.
    "test_shell_routes.py::TestAppleSiliconDetection":
        (not _sys.platform.startswith("darwin"), "macOS-only hardware detection"),
    "test_hwfit_macos.py::test_detect_system_propagates_unified_memory":
        (not _sys.platform.startswith("darwin"), "macOS unified memory"),
    "test_hwfit_cpu_arch_detection.py::test_detect_system_reports_cpu_arch_for_gpu_backends":
        (not _sys.platform.startswith("darwin"), "asserts Metal on a CUDA host"),
    "test_hwfit_cpu_arch_detection.py::test_detect_system_keeps_32_bit_arm_on_conservative_cpu_backend":
        (not _sys.platform.startswith("darwin"), "asserts 32-bit ARM backend"),

    # Missing binaries.
    "test_shell_routes.py::TestPackageProbeStatus::test_local_user_install_bin_is_added_to_path":
        (_NO_TMUX, "tmux is not installed"),
    "test_builtin_mcp_npx_cache.py::test_npx_cache_check_detects_scoped_package_in_npx_cache":
        (_NO_NPX, "npx is not installed"),

    # Windows normalises drive-letter case and quotes shell arguments
    # differently; both are assertions about POSIX string forms.
    # Named individually, never by file: a file-level fragment also skips the
    # passing tests around them, which quietly shrinks coverage instead of
    # isolating the platform gap.
    "test_rename_user_owner_sync.py::test_rename_updates_upload_metadata_owner":
        (_IS_WIN, "Windows path-case normalisation"),
    "test_rename_user_owner_sync.py::test_rename_updates_skill_md_owner":
        (_IS_WIN, "Windows path-case normalisation"),
    "test_rename_user_owner_sync.py::test_rename_leaves_other_skill_owners_untouched":
        (_IS_WIN, "Windows path-case normalisation"),
    "test_rename_user_owner_sync.py::test_rename_skill_md_owner_case_insensitive":
        (_IS_WIN, "Windows path-case normalisation"),
    "test_run_focus.py::test_dry_run_prints_command_and_does_not_execute":
        (_IS_WIN, "Windows shell argument quoting"),
    "test_run_focus.py::test_dry_run_last_failed_prints_safe_flags":
        (_IS_WIN, "Windows shell argument quoting"),
    "test_run_focus.py::test_fast_durations_dry_run_prints_command":
        (_IS_WIN, "Windows shell argument quoting"),
}


def pytest_collection_modifyitems(config, items):
    import pytest as _pytest
    for item in items:
        for frag, (cond, reason) in _PLATFORM_SKIPS.items():
            if cond and frag in item.nodeid.replace("\\", "/"):
                item.add_marker(_pytest.mark.skip(reason=f"platform: {reason}"))
                break
