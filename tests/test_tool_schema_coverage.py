"""Every tool the agent can dispatch must also be advertised to the model.

A tool with a handler but no entry in FUNCTION_TOOL_SCHEMAS is invisible: a
native tool-calling model is only offered what the schema list contains, so the
capability exists, is documented in the RAG index, and can never be invoked.
This has now happened three times — tv_control, then current_app/wake, then
generate_image, which had a handler, an MCP mapping, an argument parser and an
index description but no schema, so asking the assistant for a picture did
nothing at all. A per-tool test each time only catches the tool it was written
for; this checks the whole set.
"""
import src.agent_tools as agent_tools  # noqa: F401  — settles the import cycle
import src.tool_index as tool_index
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _schema_names():
    return {s.get("function", {}).get("name") for s in FUNCTION_TOOL_SCHEMAS}


# Described in the retrieval index but deliberately not offered as a native
# function schema. Keep this list SHORT and say why for each entry — an
# unexplained exemption is how the next invisible tool gets waved through.
KNOWN_UNADVERTISED = {
    # Dispatched through the fenced-block path only; it carries its own
    # ```manage_research``` description in agent_loop for text-mode models.
    "manage_research",
}


def test_every_handler_has_a_schema():
    handlers = set(getattr(agent_tools, "TOOL_HANDLERS", {}) or {})
    assert handlers, "no handlers discovered — the check would pass vacuously"
    missing = sorted(h for h in handlers if h not in _schema_names())
    assert not missing, (
        "these tools can be executed but are never offered to the model: "
        + ", ".join(missing))


def test_indexed_tools_are_advertised():
    """The RAG index is what tells the model a capability exists; if retrieval
    surfaces a tool the schema list omits, the model is told to use something
    it cannot call."""
    described = set(tool_index.BUILTIN_TOOL_DESCRIPTIONS)
    missing = sorted(described - _schema_names() - KNOWN_UNADVERTISED)
    assert not missing, (
        "described in the tool index but not advertised as a schema: "
        + ", ".join(missing))


def test_exemptions_are_still_real():
    """Stop the exemption list rotting into a place tools hide forever."""
    described = set(tool_index.BUILTIN_TOOL_DESCRIPTIONS)
    stale = sorted(n for n in KNOWN_UNADVERTISED
                   if n in _schema_names() or n not in described)
    assert not stale, (
        "exemptions no longer needed (now advertised, or no longer described): "
        + ", ".join(stale))


def test_image_generation_is_callable():
    """The user-visible symptom of the bug this file exists for: asking for an
    image did nothing, because the model was never offered the tool."""
    schema = next((s["function"] for s in FUNCTION_TOOL_SCHEMAS
                   if s.get("function", {}).get("name") == "generate_image"), None)
    assert schema is not None, "generate_image is not advertised to the model"
    params = schema["parameters"]
    assert "prompt" in params["properties"]
    assert params.get("required") == ["prompt"]
    # Matches mcp_servers/image_gen_server.py's inputSchema.
    for optional in ("model", "size", "quality"):
        assert optional in params["properties"], f"{optional} missing from the schema"


def test_schemas_are_well_formed():
    for s in FUNCTION_TOOL_SCHEMAS:
        fn = s.get("function") or {}
        name = fn.get("name")
        assert name, f"schema without a name: {s}"
        assert fn.get("description"), f"{name} has no description"
        params = fn.get("parameters") or {}
        assert params.get("type") == "object", f"{name} parameters are not an object"
        props = params.get("properties")
        assert isinstance(props, dict), f"{name} has no properties object"
        for req in params.get("required", []):
            assert req in props, f"{name} requires '{req}' which it does not define"
