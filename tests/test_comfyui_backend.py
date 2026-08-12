"""The local ComfyUI image backend.

Exists so image generation works without a paid provider: every
OpenAI-compatible image endpoint costs money, so the tool's only answer to
"draw me a picture" used to be a recommendation to go buy one.

ComfyUI's API is a workflow graph submitted to /prompt and collected from
/history, so the parts worth pinning are the graph being wired correctly and
the failure paths saying something the user can act on.
"""
import asyncio

import pytest

from src import comfyui_backend as comfy


# ── workflow construction ──────────────────────────────────────────────────


def test_workflow_is_wired_end_to_end():
    w = comfy.build_workflow("a fox", "model.safetensors")
    # Checkpoint feeds the sampler, both prompts, and the VAE decode.
    assert w["3"]["inputs"]["model"] == ["4", 0]
    assert w["6"]["inputs"]["clip"] == ["4", 1]
    assert w["8"]["inputs"]["vae"] == ["4", 2]
    # Sampler -> decode -> save.
    assert w["8"]["inputs"]["samples"] == ["3", 0]
    assert w["9"]["inputs"]["images"] == ["8", 0]
    assert w["4"]["inputs"]["ckpt_name"] == "model.safetensors"
    assert w["6"]["inputs"]["text"] == "a fox"


def test_positive_and_negative_prompts_are_separate_nodes():
    """Node 6 is positive, node 7 negative — swapping them inverts the image."""
    w = comfy.build_workflow("a fox", "m.ckpt", negative="blurry")
    assert w["3"]["inputs"]["positive"] == ["6", 0]
    assert w["3"]["inputs"]["negative"] == ["7", 0]
    assert w["6"]["inputs"]["text"] == "a fox"
    assert w["7"]["inputs"]["text"] == "blurry"


def test_seed_varies_so_repeat_prompts_differ():
    seeds = {comfy.build_workflow("x", "m")["3"]["inputs"]["seed"] for _ in range(5)}
    assert len(seeds) > 1, "a fixed seed would return the same image every time"


@pytest.mark.parametrize("given,expected", [
    ("1024x1024", (1024, 1024)),
    ("1024x768", (1024, 768)),
    ("1000x999", (1000, 992)),   # snapped down to multiples of 8
    ("huge", (1024, 1024)),      # unparseable -> safe default
    ("99999x99999", (2048, 2048)),
    ("16x16", (256, 256)),
])
def test_sizes_are_made_safe(given, expected):
    """ComfyUI's latent node needs multiples of 8; a stray value crashes the
    graph mid-run instead of being rejected."""
    assert comfy._parse_size(given) == expected


# ── talking to the server ──────────────────────────────────────────────────


class _Resp:
    def __init__(self, code=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = code, payload, content
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Stands in for httpx.AsyncClient with a scripted conversation."""

    def __init__(self, *, post=None, gets=None):
        self._post, self._gets = post, dict(gets or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return self._post

    async def get(self, url, **kw):
        for frag, resp in self._gets.items():
            if frag in url:
                return resp() if callable(resp) else resp
        return _Resp(404, {})


def _patch_client(monkeypatch, client):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)


def test_generate_returns_png_bytes(monkeypatch):
    history = {"pid": {"status": {"completed": True},
                       "outputs": {"9": {"images": [
                           {"filename": "o_001.png", "subfolder": "", "type": "output"}]}}}}
    client = _Client(
        post=_Resp(200, {"prompt_id": "pid"}),
        gets={"/history/": _Resp(200, history),
              "/view": _Resp(200, content=b"\x89PNG-bytes")},
    )
    _patch_client(monkeypatch, client)
    monkeypatch.setattr(comfy, "POLL_INTERVAL", 0)
    out = asyncio.run(comfy.generate("a fox", checkpoint="m.safetensors"))
    assert out == b"\x89PNG-bytes"


def test_rejected_workflow_surfaces_comfyuis_reason(monkeypatch):
    """Graph validation failures are the actionable ones (missing node, unknown
    checkpoint), so the message must carry them through."""
    _patch_client(monkeypatch, _Client(post=_Resp(400, {"error": "unknown node LoadFoo"})))
    monkeypatch.setattr(comfy, "POLL_INTERVAL", 0)
    with pytest.raises(RuntimeError, match="rejected the workflow"):
        asyncio.run(comfy.generate("x", checkpoint="m"))


def test_render_error_is_reported(monkeypatch):
    history = {"pid": {"status": {"status_str": "error", "completed": False,
                                  "messages": [["execution_error", {"ex": "OOM"}]]},
                       "outputs": {}}}
    _patch_client(monkeypatch, _Client(
        post=_Resp(200, {"prompt_id": "pid"}),
        gets={"/history/": _Resp(200, history)}))
    monkeypatch.setattr(comfy, "POLL_INTERVAL", 0)
    with pytest.raises(RuntimeError, match="failed to render"):
        asyncio.run(comfy.generate("x", checkpoint="m"))


def test_timeout_explains_the_cold_start(monkeypatch):
    _patch_client(monkeypatch, _Client(
        post=_Resp(200, {"prompt_id": "pid"}),
        gets={"/history/": _Resp(200, {})}))    # never completes
    monkeypatch.setattr(comfy, "POLL_INTERVAL", 0)
    monkeypatch.setattr(comfy, "POLL_TIMEOUT", 0.05)
    with pytest.raises(RuntimeError, match="did not finish"):
        asyncio.run(comfy.generate("x", checkpoint="m"))


def test_no_checkpoint_installed_says_where_to_put_one(monkeypatch):
    """Running-but-empty is a different problem from not running, and the user
    needs to be told which one they have."""
    async def none(*a, **k):
        return []
    monkeypatch.setattr(comfy, "list_checkpoints", none)
    with pytest.raises(RuntimeError, match="models/checkpoints"):
        asyncio.run(comfy.generate("x"))


def test_is_available_is_false_when_nothing_listens(monkeypatch):
    import httpx

    class Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: Boom())
    assert asyncio.run(comfy.is_available()) is False


def test_url_is_overridable_for_a_remote_box(monkeypatch):
    monkeypatch.setenv("COMFYUI_URL", "http://10.0.0.9:8188/")
    assert comfy.comfy_url() == "http://10.0.0.9:8188"
