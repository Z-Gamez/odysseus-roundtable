"""The llama.cpp context override needs the same handling as its Ollama sibling.

llamacpp_ctx existed in DEFAULT_SETTINGS but was never exposed in the UI, so
the only way to set it was editing settings.json by hand. Exposing it is only
half the job: the settings POST clamps ollama_num_ctx and did not clamp this
one, so a string would reach the launcher's int() and an absurd value would
make llama-server fail to allocate its KV cache — surfacing as "llama.cpp
won't start" with nothing pointing at the cause.
"""
import inspect
import re


def test_the_control_exists_in_the_ui():
    html = open("static/index.html", encoding="utf-8").read()
    assert 'id="set-llamacppCtx"' in html


def test_the_control_is_wired_to_the_setting():
    js = open("static/js/settings.js", encoding="utf-8").read()
    assert "set-llamacppCtx" in js, "control is never looked up"
    assert "llamacpp_ctx" in js, "control never reads or writes the setting"
    # It must both populate on load and save on change, or it silently resets.
    assert re.search(r"lctx\.value\s*=\s*s\.llamacpp_ctx", js), "never populated"
    assert re.search(r"save\(\{\s*llamacpp_ctx", js), "never saved"


def test_the_setting_is_clamped_like_its_sibling():
    from routes import auth_routes
    src = inspect.getsource(auth_routes)
    idx = src.index("_INT_RANGES = {")
    window = src[idx:idx + 900]
    assert "llamacpp_ctx" in window, (
        "llamacpp_ctx is not clamped; a string reaches int() in the launcher "
        "and an absurd value stops llama-server allocating its KV cache"
    )


def test_zero_still_means_follow_the_shared_setting():
    """0 is the sentinel the launcher already honours — exposing the control
    must not change that contract."""
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["llamacpp_ctx"] == 0
    from src import llamacpp_launcher
    src = inspect.getsource(llamacpp_launcher.start_if_configured)
    assert "ollama_num_ctx" in src, (
        "the launcher must still fall back to the shared cap when the override "
        "is 0"
    )
