"""Nothing may write inside the frozen .app bundle.

macOS TCC keys permissions to an app's signing identity. Odysseus ships
ad-hoc-signed, so there is no stable Team ID and TCC falls back to the cdhash —
which changes every build. A write into the bundle breaks the signature seal
("a sealed resource is missing or invalid"), and from then on every rebuild
reads as a DIFFERENT app: the Contacts and Automation/Messages grants the user
already gave are silently dropped and re-prompted.

Two writers did this, both by deriving a path from code location rather than
from the data dir:
  * src/saw/store.py  -> Contents/Frameworks/data/saw.db
  * src/builtin_mcp.py -> Contents/Frameworks/data/local/playwright-mcp-cache

Bundles are read-only on macOS regardless — this also fails for any user
without write access to /Applications.
"""
import inspect


def test_saw_db_uses_the_shared_data_dir():
    from src.saw import store
    src = inspect.getsource(store._data_dir)
    assert "DATA_DIR" in src, (
        "saw.db path must come from src.constants.DATA_DIR, which resolves to "
        "~/.odysseus/data when frozen"
    )
    # Strip the docstring: it explains the old __file__-based bug, and matching
    # on prose would fail the moment someone documents what went wrong.
    body = src.split('"""')[-1]
    assert "__file__" not in body, (
        "deriving the path from __file__ lands inside Contents/Frameworks in a "
        "frozen .app and breaks the code signature"
    )


def test_saw_db_lands_in_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    import importlib
    import src.constants as constants
    importlib.reload(constants)
    from src.saw import store
    importlib.reload(store)
    assert str(tmp_path) in store._db_path()


def test_browser_cache_is_not_written_into_the_bundle():
    from src import builtin_mcp
    src = inspect.getsource(builtin_mcp)
    idx = src.index("playwright-mcp-cache")
    window = src[max(0, idx - 500):idx + 100]
    assert "DATA_DIR" in window, (
        "the Playwright cache is hundreds of MB written at runtime; under "
        "get_app_root() that lands inside the .app bundle"
    )
    assert "base_dir" not in window.split("cache_home")[-1], (
        "cache_home must not be derived from the app root"
    )


def test_no_module_derives_a_writable_data_dir_from_code_location():
    """Catch the next one before it ships.

    Reading bundled resources by code location is fine — writing is not. This
    pins the two known writers; extend it if another appears.
    """
    from src.saw import store
    from src import builtin_mcp
    for mod in (store, builtin_mcp):
        text = inspect.getsource(mod)
        for marker in ('os.path.join(base_dir, "data"',
                       'root / "data"'):
            assert marker not in text, (
                f"{mod.__name__} builds a data path from its own location; in a "
                f"frozen .app that writes inside the signed bundle"
            )
