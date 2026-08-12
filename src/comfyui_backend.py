"""Talk to a local ComfyUI instance and get a PNG back.

Why this exists: the image tool speaks OpenAI's /v1/images/generations, whose
only providers are paid (gpt-image, dall-e). ComfyUI is free, runs on the local
GPU, and is the de-facto local image backend — but its API is a workflow graph
submitted to /prompt and collected asynchronously from /history, not a single
request/response. This module hides that difference so the tool can offer a
local option without the caller knowing which backend answered.

Deliberately dependency-free beyond httpx (already required) and stdlib: this
is imported by an MCP server that runs as its own process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8188"
# A ComfyUI render is seconds on a modern GPU but minutes on a cold start, when
# the checkpoint is first paged into VRAM.
POLL_TIMEOUT = 300.0
POLL_INTERVAL = 1.0


def comfy_url() -> str:
    """Where ComfyUI is expected. Env override so a remote box can serve it."""
    return (os.environ.get("COMFYUI_URL") or DEFAULT_URL).rstrip("/")


async def is_available(url: Optional[str] = None, timeout: float = 2.5) -> bool:
    """True when a ComfyUI is actually answering — used to decide whether to
    offer local generation before promising the user anything."""
    import httpx
    base = (url or comfy_url()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(base + "/system_stats")
            return r.status_code == 200
    except Exception:
        return False


async def list_checkpoints(url: Optional[str] = None) -> list[str]:
    """Checkpoints ComfyUI can load. Empty means it is running but has no model,
    which is a different problem from it not running at all."""
    import httpx
    base = (url or comfy_url()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(base + "/object_info/CheckpointLoaderSimple")
            r.raise_for_status()
            info = r.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            return list(info)
    except Exception as e:
        logger.warning("[comfyui] could not list checkpoints: %s", e)
        return []


def _parse_size(size: str) -> Tuple[int, int]:
    try:
        w, h = size.lower().split("x", 1)
        w, h = int(w), int(h)
    except Exception:
        return 1024, 1024
    # ComfyUI's latent nodes require multiples of 8; a stray value crashes the
    # graph mid-run rather than being rejected up front.
    w = max(256, min(2048, (w // 8) * 8))
    h = max(256, min(2048, (h // 8) * 8))
    return w, h


def build_workflow(prompt: str, checkpoint: str, size: str = "1024x1024",
                   steps: int = 25, cfg: float = 7.0,
                   negative: str = "") -> dict:
    """A minimal txt2img graph in ComfyUI's API format.

    Node ids are arbitrary strings; the links are what matter. Kept as plain
    data rather than a saved .json so there is no file to lose track of, and so
    the size/steps actually asked for are the ones that run.
    """
    w, h = _parse_size(size)
    return {
        "3": {"class_type": "KSampler",
              "inputs": {"seed": random.randint(0, 2 ** 63 - 1),
                         "steps": steps, "cfg": cfg,
                         "sampler_name": "euler", "scheduler": "normal",
                         "denoise": 1.0,
                         "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "odysseus", "images": ["8", 0]}},
    }


async def generate(prompt: str, *, checkpoint: str = "", size: str = "1024x1024",
                   steps: int = 25, negative: str = "",
                   url: Optional[str] = None) -> bytes:
    """Render `prompt` and return PNG bytes. Raises RuntimeError with something
    the user can act on."""
    import httpx
    base = (url or comfy_url()).rstrip("/")

    if not checkpoint:
        cks = await list_checkpoints(base)
        if not cks:
            raise RuntimeError(
                "ComfyUI is running but has no checkpoint installed. Put a "
                "model .safetensors in ComfyUI/models/checkpoints and restart it.")
        checkpoint = cks[0]

    workflow = build_workflow(prompt, checkpoint, size=size, steps=steps,
                              negative=negative)
    client_id = os.urandom(8).hex()

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0,
                                                       write=30.0, pool=10.0)) as c:
        r = await c.post(base + "/prompt",
                         json={"prompt": workflow, "client_id": client_id})
        if r.status_code != 200:
            # ComfyUI reports graph validation failures here, and they are the
            # actionable ones (missing node, unknown checkpoint).
            raise RuntimeError(f"ComfyUI rejected the workflow ({r.status_code}): {r.text[:400]}")
        prompt_id = r.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI accepted the job but returned no prompt_id")

        # Poll rather than open a websocket: one less moving part, and the job
        # is seconds long.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + POLL_TIMEOUT
        entry = None
        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            h = await c.get(f"{base}/history/{prompt_id}")
            if h.status_code != 200:
                continue
            hist = h.json()
            if prompt_id in hist:
                entry = hist[prompt_id]
                break
        if entry is None:
            raise RuntimeError(
                f"ComfyUI did not finish within {int(POLL_TIMEOUT)}s. A first run "
                f"loads the checkpoint into VRAM and can be slow; try again.")

        status = (entry.get("status") or {})
        if status.get("status_str") == "error" or not status.get("completed", True):
            msgs = json.dumps(status.get("messages", []))[:400]
            raise RuntimeError(f"ComfyUI failed to render: {msgs}")

        images = []
        for node_out in (entry.get("outputs") or {}).values():
            images.extend(node_out.get("images") or [])
        if not images:
            raise RuntimeError("ComfyUI finished but produced no image")

        img = images[0]
        v = await c.get(base + "/view", params={
            "filename": img.get("filename", ""),
            "subfolder": img.get("subfolder", ""),
            "type": img.get("type", "output"),
        })
        v.raise_for_status()
        return v.content
