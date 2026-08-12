"""Which backend an image request goes to.

The optional `model` argument is the trap. A small model fills it in with a
plausible guess — "sdxl", "stable-diffusion", "flux" — none of which is a
configured endpoint, and the original routing only treated the exact strings
"comfyui"/"local" as local. So every guess fell through to the endpoint
resolver and came back "No enabled endpoints found" while a working local
ComfyUI sat idle.

Rule: only a name that clearly identifies a HOSTED provider skips the local
backend. Anything else means "whatever renders this".
"""
import sys
import asyncio
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_servers"))


@pytest.fixture
def routed(monkeypatch):
    """Run the tool with the local backend and the resolver both stubbed, and
    report which one it chose."""
    import mcp_servers.image_gen_server as srv
    from src import comfyui_launcher, comfyui_backend

    chosen = {}

    async def fake_ensure(*a, **k):
        return True

    async def fake_generate(prompt, **kw):
        chosen["backend"] = "local"
        return b"\x89PNG"

    def fake_resolve(spec, **kw):
        chosen["backend"] = "hosted"
        raise ValueError("No enabled endpoints found")

    monkeypatch.setattr(comfyui_launcher, "ensure_running", fake_ensure)
    monkeypatch.setattr(comfyui_backend, "generate", fake_generate)
    monkeypatch.setattr(srv, "_save_and_report", lambda *a, **k: "ok")
    monkeypatch.setattr("src.ai_interaction._resolve_model", fake_resolve)

    def run(args):
        chosen.clear()
        asyncio.run(srv.call_tool("generate_image", args))
        return chosen.get("backend")

    return run


@pytest.mark.parametrize("model", [
    None,                    # nothing specified
    "",                      # explicitly empty
    "comfyui", "local",      # named directly
    "sdxl", "SDXL",          # the guesses that used to fail
    "stable-diffusion",
    "flux", "flux-schnell",
    "sd_xl_base_1.0.safetensors",
    "some-model-nobody-configured",
])
def test_anything_not_clearly_hosted_renders_locally(routed, model):
    args = {"prompt": "a cat"}
    if model is not None:
        args["model"] = model
    assert routed(args) == "local", f"model={model!r} should have rendered locally"


@pytest.mark.parametrize("model", [
    "dall-e-3", "DALL-E-3", "gpt-image-1", "gpt-image-1.5",
])
def test_an_explicit_hosted_model_is_honoured(routed, model):
    """Asking for a paid provider by name must not be silently answered by the
    local one — the user may want that specific model."""
    assert routed({"prompt": "a cat", "model": model}) == "hosted"


def test_schema_tells_the_model_not_to_guess():
    """The parameter exists for the rare explicit case; advertising it as a
    free choice is what produced the bad values in the first place."""
    import src.agent_tools  # noqa: F401
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    fn = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
              if s.get("function", {}).get("name") == "generate_image")
    desc = fn["parameters"]["properties"]["model"]["description"]
    assert "OMIT" in desc.upper()
    assert "never guess" in desc.lower()
