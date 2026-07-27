"""The AI-defaults context cap must govern EVERY local backend, not just Ollama.

llama.cpp fixes its window at launch (`-c`) and it is routinely far smaller
than the model card advertises — a 128K-card model served with `-c 8192` really
has 8192. Two independent gaps let prompts get truncated server-side:

1. The trimmer's context clamp was gated on "is this Ollama?", so llama.cpp was
   budgeted against the model card and the trimmer believed it had endless room.
2. The launcher read its own llamacpp_ctx (default 8192) instead of the shared
   AI-defaults cap, so llama-server came up smaller than configured.
"""
import inspect

import pytest

from src import agent_loop, llm_core, llamacpp_launcher


_LEGACY_DEFAULT = 8192


def _resolve_ctx(shared, override):
    """Mirror of the launcher's resolution, driven by the real source."""
    src = inspect.getsource(llamacpp_launcher.start_if_configured)
    assert "ollama_num_ctx" in src, "launcher no longer consults the shared cap"
    ctx = shared
    if override > 0 and override != _LEGACY_DEFAULT:
        ctx = override
    return ctx if ctx > 0 else _LEGACY_DEFAULT


def test_shared_cap_drives_llamacpp_launch():
    assert _resolve_ctx(12288, 0) == 12288


def test_stale_legacy_override_does_not_win():
    """8192 was llamacpp_ctx's former DEFAULT, and the save path materializes
    defaults — a persisted 8192 means "never touched", not "chose 8192". If it
    counted as explicit it would keep overriding the shared cap on every
    existing install, which is the bug being fixed."""
    assert _resolve_ctx(12288, _LEGACY_DEFAULT) == 12288


def test_deliberate_override_is_honoured():
    assert _resolve_ctx(12288, 32768) == 32768


def test_falls_back_when_nothing_is_configured():
    assert _resolve_ctx(0, 0) == _LEGACY_DEFAULT


# ── the served-window probe ──────────────────────────────────────────────────

def test_served_ctx_probe_reads_props(monkeypatch):
    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"default_generation_settings": {"n_ctx": 8192}}

    monkeypatch.setattr(llm_core, "_OLLAMA_CAPS_CACHE", {})
    monkeypatch.setattr(llm_core.httpx, "get", lambda *a, **k: _R())
    assert llm_core.llamacpp_served_ctx("http://localhost:8080/v1") == 8192


def test_served_ctx_probe_returns_zero_for_non_llamacpp(monkeypatch):
    """Must degrade to 0, never raise — it runs against any self-hosted URL."""
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(llm_core, "_OLLAMA_CAPS_CACHE", {})
    monkeypatch.setattr(llm_core.httpx, "get", boom)
    assert llm_core.llamacpp_served_ctx("http://localhost:11434/v1") == 0


def test_served_ctx_probe_rejects_garbage_urls():
    assert llm_core.llamacpp_served_ctx("") == 0
    assert llm_core.llamacpp_served_ctx("not-a-url") == 0


# ── the trimmer clamp ────────────────────────────────────────────────────────

def _clamp_source():
    src = inspect.getsource(agent_loop)
    start = src.index('_ctx_cap = int(get_setting("ollama_num_ctx"')
    # Generous trailing window: this slice has to reach past the probe calls
    # to the min() itself, and comments in between have pushed it out before.
    return src[start - 1600:start + 1400]


def test_clamp_is_not_gated_on_ollama_alone():
    """Assert the branch CONDITION, not just that the name appears somewhere —
    deleting `or _is_self_hosted` from the `if` leaves the assignment above it
    intact, so a name-presence check would still pass while the clamp is once
    again Ollama-only."""
    body = _clamp_source()
    assert "if _is_ollama_backend or _is_self_hosted:" in body, (
        "the context clamp is Ollama-only again; llama.cpp will be budgeted "
        "against the model card instead of its real -c window"
    )
    assert "llamacpp_served_ctx" in body


def test_clamp_takes_the_tightest_window():
    """Setting, and what the server actually serves, are both floors."""
    body = _clamp_source()
    assert "min(ctx_for_budget" in body


# --- router mode: one host, several windows ---------------------------------

def test_served_ctx_is_cached_per_model_not_per_host(monkeypatch):
    """A router host serves several models with DIFFERENT windows.

    Keyed on host alone, the first model's window is handed back for the
    second after a swap. Budgeting a 12K model at 64K makes the trimmer pack
    a prompt llama.cpp then truncates server-side — silently cutting exactly
    the code the caller is trying to protect.
    """
    llm_core._OLLAMA_CAPS_CACHE.clear()
    windows = {"Qwen3.6:35B": 65536, "Ornith:9B": 12288}
    current = {"loaded": "Qwen3.6:35B"}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"default_generation_settings":
                    {"n_ctx": windows[current["loaded"]]}}

    monkeypatch.setattr(llm_core.httpx, "get", lambda *a, **k: _R())

    url = "http://localhost:8080/v1"
    assert llm_core.llamacpp_served_ctx(url, "Qwen3.6:35B") == 65536

    # Router swaps the resident model; /props now reports the smaller window.
    current["loaded"] = "Ornith:9B"
    assert llm_core.llamacpp_served_ctx(url, "Ornith:9B") == 12288, (
        "the previous model's context window was reused after a router swap"
    )
    # And the first model's answer is still cached under its own key.
    current["loaded"] = "Qwen3.6:35B"
    assert llm_core.llamacpp_served_ctx(url, "Qwen3.6:35B") == 65536
    llm_core._OLLAMA_CAPS_CACHE.clear()


def test_router_reports_each_models_window_before_it_is_loaded(monkeypatch):
    """The trimmer budgets BEFORE the request that swaps the model in.

    /props only ever describes the resident model, and reports n_ctx 0 when
    the router is idle. Asking it about a not-yet-loaded model gives the wrong
    window or none, so a 12K model gets budgeted at the 64K default and
    llama.cpp truncates the prompt server-side. Router mode publishes each
    model's own launch argv under /v1/models status.args instead.
    """
    llm_core._OLLAMA_CAPS_CACHE.clear()

    def _fake_get(url, *a, **k):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                if url.endswith("/v1/models"):
                    return {"data": [
                        {"id": "Ornith:9B", "status": {"args": [
                            "llama-server", "--alias", "Ornith:9B",
                            "--ctx-size", "12288", "--n-gpu-layers", "99"]}},
                        {"id": "Qwen3.6:35B", "status": {"args": [
                            "llama-server", "--alias", "Qwen3.6:35B",
                            "--ctx-size", "65536", "--cpu-moe"]}},
                    ]}
                # Router is idle: no model resident, so no window to report.
                return {"default_generation_settings": {"n_ctx": 0}}
        return _R()

    monkeypatch.setattr(llm_core.httpx, "get", _fake_get)
    url = "http://localhost:8080/v1"

    assert llm_core.llamacpp_served_ctx(url, "Ornith:9B") == 12288, (
        "budgeted a 12K model against something else; llama.cpp would cut the "
        "prompt server-side"
    )
    assert llm_core.llamacpp_served_ctx(url, "Qwen3.6:35B") == 65536
    llm_core._OLLAMA_CAPS_CACHE.clear()


def test_single_model_server_still_uses_props(monkeypatch):
    """Non-router servers have no status.args — must fall back, not report 0."""
    llm_core._OLLAMA_CAPS_CACHE.clear()

    def _fake_get(url, *a, **k):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                if url.endswith("/v1/models"):
                    return {"data": [{"id": "solo", "object": "model"}]}
                return {"default_generation_settings": {"n_ctx": 8192}}
        return _R()

    monkeypatch.setattr(llm_core.httpx, "get", _fake_get)
    assert llm_core.llamacpp_served_ctx("http://localhost:8080/v1", "solo") == 8192
    llm_core._OLLAMA_CAPS_CACHE.clear()
