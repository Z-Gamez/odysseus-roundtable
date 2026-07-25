"""Native tool-calling default for curated Ollama models (fresh-install fix).

Native function calling is opt-in per endpoint (supports_tools toggle)
because most local models mishandle native schemas on Ollama (#1567). On a
fresh install the toggle is unset, and Ornith — which only speaks native
tool calls — wrote its commands into the Round Table transcript as plain
text (observed on macOS). Curated models now default to native when the
endpoint flag is None, and the SAW file-I/O hint must stay consistent.
"""
from src.llm_core import ollama_native_tools_model
from src.saw.orchestrator import _augment_for_local


def test_curated_model_matching():
    assert ollama_native_tools_model("ornith:9b-12k")
    assert ollama_native_tools_model("ornith:35B-12k")
    assert ollama_native_tools_model("hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q6_K")
    assert not ollama_native_tools_model("qwen3:14b-q4_K_M")
    assert not ollama_native_tools_model("gemma4:latest")
    assert not ollama_native_tools_model("")
    assert not ollama_native_tools_model(None)


def _msgs():
    return [{"role": "system", "content": "You are the developer."},
            {"role": "user", "content": "build it"}]


# 127.0.0.1 (not localhost) so the dev machine's real endpoint row — which
# has supports_tools=True for localhost:11434 — can't satisfy the DB lookup
# and mask the unset-flag path under test.
URL = "http://127.0.0.1:11434/v1"


def test_hint_skipped_for_curated_native_model():
    out = _augment_for_local(_msgs(), URL, has_python=True, model="ornith:9b-12k")
    assert out[0]["content"] == "You are the developer."   # no fenced-mode hint


def test_hint_still_added_for_fenced_models():
    out = _augment_for_local(_msgs(), URL, has_python=True, model="qwen3:14b-q4_K_M")
    assert "file" in out[0]["content"].lower()
    assert out[0]["content"] != "You are the developer."
