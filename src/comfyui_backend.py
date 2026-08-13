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
# A warm render is seconds. The FIRST one after a cold start also loads the
# checkpoint, and that is where the real cost is — measured on a 16GB M2 with
# a 6.46GB SDXL checkpoint:
#
#   ComfyUI alone, checkpoint warm : ready 6.2s  + gen  25.2s =  31.4s
#   with a llama.cpp model resident: ready 4.1s  + gen 145.2s = 149.3s
#
# Six times slower under memory contention, so any budget tuned to the
# uncontended case fails every time a local LLM is loaded. The first run
# therefore gets its own, much larger allowance.
POLL_TIMEOUT = 300.0
POLL_INTERVAL = 1.0
# Read timeout for the polling client. This used to be 60s and was the real
# failure: while ComfyUI loads a large checkpoint under memory pressure its
# HTTP server stops answering, so the /history poll itself timed out and the
# exception escaped the loop — the generation died at ~60s regardless of
# POLL_TIMEOUT. Polls are now both patient and non-fatal.
POLL_READ_TIMEOUT = 120.0
# Set once a generation completes, so subsequent calls use the shorter budget.
_checkpoint_warm = False


def is_warm() -> bool:
    return _checkpoint_warm


def _total_ram_bytes() -> int:
    """Physical RAM, without taking a psutil dependency into an MCP process."""
    try:
        if os.name == "nt":
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return int(ms.ullTotalPhys)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        return 0


# Below this, a big checkpoint and a resident LLM cannot both fit. A 16GB M2
# measured 145s for a load that takes 25s uncontended — and with a large model
# also resident it does not complete at all: the machine pages rather than
# loads, so no timeout is generous enough to rescue it. Freeing the LLM first
# is the fix; it reloads by itself on the next chat message.
_LOW_RAM_CEILING = 24 * 1024 ** 3


def _should_free_llm() -> bool:
    setting = None
    try:
        from src.settings import get_setting
        setting = get_setting("comfyui_free_llm_memory", "auto")
    except Exception:
        pass
    if setting in (True, "always", "yes"):
        return True
    if setting in (False, "never", "no"):
        return False
    total = _total_ram_bytes()
    return bool(total and total < _LOW_RAM_CEILING)


def free_memory_for_image() -> bool:
    """Unload the local LLM so the checkpoint has room. True if we freed one.

    Only for the cold path: once the checkpoint is resident this costs nothing
    to skip. llama.cpp is restarted automatically by its own supervisor the
    next time a chat request arrives, so the user loses nothing but the reload.
    """
    if not _should_free_llm():
        return False
    try:
        from src import llamacpp_supervisor as llama
        if not llama.is_running():
            return False
        freed = llama.server_rss_bytes() or 0
        if llama._stop_child():
            logger.info("[comfyui] unloaded the local LLM (%.1fGB) so the "
                        "checkpoint can load; it restarts on the next chat",
                        freed / 1024 ** 3 if freed else 0.0)
            return True
    except Exception as e:
        logger.debug("[comfyui] could not free LLM memory: %s", e)
    return False


def _first_run_timeout() -> float:
    """Budget for a run that may still have to load the checkpoint."""
    try:
        from src.settings import get_setting
        v = get_setting("comfyui_first_run_timeout_seconds", 300)
        return float(v) if v else 300.0
    except Exception:
        return 300.0


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

    global _checkpoint_warm
    # Cold load on a memory-tight machine: make room BEFORE asking ComfyUI to
    # read 6.46GB off disk. Without this a 16GB box simply pages forever and no
    # timeout is long enough, which is what "the model never loads" looked like.
    if not _checkpoint_warm:
        free_memory_for_image()
    budget = POLL_TIMEOUT if _checkpoint_warm else _first_run_timeout()
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0,
                                                       read=POLL_READ_TIMEOUT,
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
        started = loop.time()
        deadline = started + budget
        entry = None
        last_note = 0.0
        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                h = await c.get(f"{base}/history/{prompt_id}")
            except Exception as e:
                # NOT fatal. While a large checkpoint is paging in, ComfyUI's
                # HTTP server stops answering; letting that escape is what
                # killed every first generation at the read timeout. The job
                # is still queued and running, so keep waiting.
                logger.debug("[comfyui] poll hiccup (still waiting): %s", e)
                continue
            if h.status_code != 200:
                continue
            hist = h.json()
            if prompt_id in hist:
                entry = hist[prompt_id]
                break
            # Silence for two minutes looks like a hang, so say what is going on.
            waited = loop.time() - started
            if waited - last_note >= 15.0:
                last_note = waited
                logger.info("[comfyui] still working (%ds elapsed%s)", int(waited),
                            "" if _checkpoint_warm else " — first run, loading the checkpoint")
        if entry is None:
            waited = int(loop.time() - started)
            if not _checkpoint_warm:
                raise RuntimeError(
                    f"timed out during first checkpoint load after {waited}s. The "
                    f"model is {'' if waited < budget else 'still '}being read into "
                    f"memory — with a local LLM also resident this measured ~145s on "
                    f"a 16GB machine. ComfyUI has been left running, so trying again "
                    f"now starts warm and should be quick. Raise "
                    f"comfyui_first_run_timeout_seconds if it keeps happening.")
            raise RuntimeError(
                f"ComfyUI did not finish within {waited}s even though the checkpoint "
                f"was already loaded — something is wrong with the render itself.")

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
        # The expensive part is done: the checkpoint is resident, so the next
        # call gets the short budget rather than the first-run one.
        _checkpoint_warm = True
        return v.content
