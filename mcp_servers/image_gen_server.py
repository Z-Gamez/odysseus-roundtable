"""
image_gen_server.py

MCP server exposing image generation via OpenAI-compatible APIs.
"""

import asyncio
import base64
import sys
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import GENERATED_IMAGES_DIR

server = Server("image_gen")


def _save_and_report(png: bytes, prompt: str, model_id: str,
                     size: str, quality: str) -> str:
    """Persist a rendered image, record it in the gallery, and describe it.

    Shared by both backends so a locally-rendered image is stored, linked and
    reported exactly like a paid one — the caller should not be able to tell
    which produced it.
    """
    from src.settings import get_setting

    img_dir = Path(GENERATED_IMAGES_DIR)
    img_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}.png"
    (img_dir / filename).write_bytes(png)

    _pub_base = (get_setting("app_public_url", "") or "").rstrip("/")
    image_url = f"{_pub_base}/api/generated-image/{filename}"

    try:
        from src.database import SessionLocal, GalleryImage
        db = SessionLocal()
        db.add(GalleryImage(id=str(uuid.uuid4()), filename=filename, prompt=prompt,
                            model=model_id, size=size, quality=quality))
        db.commit()
        db.close()
    except Exception:
        pass

    # "Direct link:" rather than an "image_url:" label — small models copied the
    # label token ("image_url") into the link href, producing a broken link.
    return (f"Generated image for: {prompt[:100]}\n"
            f"Direct link: {image_url}\n"
            f"model: {model_id}\nsize: {size}")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image using an image-capable model (e.g. gpt-image-1)",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Model name (auto-detects if omitted)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "generate_image":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    prompt = arguments.get("prompt", "")
    model_spec = arguments.get("model", "")
    size = arguments.get("size", "1024x1024")
    quality = arguments.get("quality", "medium")

    if not prompt:
        return [TextContent(type="text", text="Error: Image prompt is required")]

    try:
        import httpx
        from src.settings import load_settings, get_setting
        from src.ai_interaction import _resolve_model

        if not get_setting("image_gen_enabled", True):
            return [TextContent(type="text", text="Error: Image generation is disabled by the administrator.")]

        _settings = load_settings()

        if not model_spec:
            model_spec = _settings.get("image_model", "")
        if quality == "medium" and _settings.get("image_quality"):
            quality = _settings["image_quality"]

        from src import comfyui_backend as comfy

        # Local first. Every OpenAI-compatible image provider costs money, so
        # without this the tool's only answer to "draw me a picture" was to
        # recommend a paid service. A ComfyUI on the box renders for free on the
        # GPU, so it is preferred whenever it is actually answering — and when
        # it is not, we fall through to the configured/paid path unchanged.
        if not model_spec or model_spec.lower() in ("comfyui", "local"):
            if await comfy.is_available():
                try:
                    png = await comfy.generate(
                        prompt, size=size,
                        negative=_settings.get("image_negative_prompt", ""))
                except Exception as e:
                    return [TextContent(type="text", text=f"Error: {e}")]
                return [TextContent(type="text",
                                    text=_save_and_report(png, prompt, "comfyui", size, quality))]

        # Auto-detect best available image model
        if not model_spec:
            for candidate in ("gpt-image-1.5", "gpt-image-1", "dall-e-3"):
                try:
                    await asyncio.to_thread(_resolve_model, candidate)
                    model_spec = candidate
                    break
                except ValueError:
                    continue
            if not model_spec:
                return [TextContent(type="text", text=(
                    "Error: No image model is available. Nothing is configured in Admin, "
                    f"and no local ComfyUI is answering on {comfy.comfy_url()}. Start "
                    "ComfyUI to generate images locally and for free, or configure an "
                    "image model in Admin."))]

        try:
            url, model_id, headers = await asyncio.to_thread(_resolve_model, model_spec, model_type="image")
        except ValueError:
            _lower_model_spec = model_spec.lower()
            if not any(_name in _lower_model_spec for _name in ("gpt-image", "dall-e")):
                raise
            url, model_id, headers = await asyncio.to_thread(_resolve_model, model_spec)

        is_gpt_image = "gpt-image" in model_id.lower()
        base_url = url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
        images_url = base_url + "/images/generations"

        valid_gpt_sizes = {"1024x1024", "1024x1536", "1536x1024", "auto"}
        valid_dalle3_sizes = {"1024x1024", "1024x1792", "1792x1024"}
        if is_gpt_image and size not in valid_gpt_sizes:
            size = "1024x1024"
        elif not is_gpt_image and size not in valid_dalle3_sizes:
            size = "1024x1024"

        payload = {"model": model_id, "prompt": prompt, "n": 1, "size": size}
        if is_gpt_image:
            payload["quality"] = quality if quality in ("low", "medium", "high", "auto") else "medium"

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)) as client:
            resp = await client.post(images_url, json=payload, headers=headers)

            if resp.status_code != 200:
                error_text = resp.text[:500]
                try:
                    err_json = resp.json()
                    error_text = err_json.get("error", {}).get("message", error_text) if isinstance(err_json.get("error"), dict) else str(err_json.get("error", error_text))
                except Exception:
                    pass
                return [TextContent(type="text", text=f"Error: Image generation failed ({resp.status_code}): {error_text}")]

            data = resp.json()
            images = data.get("data", [])
            if not images:
                return [TextContent(type="text", text="Error: No images returned from API")]

            img = images[0]
            if img.get("b64_json"):
                return [TextContent(type="text", text=_save_and_report(
                    base64.b64decode(img["b64_json"]), prompt, model_id, size,
                    payload.get("quality", "medium")))]
            if img.get("url"):
                # A hosted provider returned a link rather than bytes; pass it
                # through as-is (nothing local to store).
                return [TextContent(type="text", text=(
                    f"Generated image for: {prompt[:100]}\n"
                    f"Direct link: {img['url']}\n"
                    f"model: {model_id}\nsize: {size}"))]
            return [TextContent(type="text", text="Error: Unexpected image API response format")]

    except httpx.TimeoutException:
        return [TextContent(type="text", text="Error: Image generation timed out (300s)")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
