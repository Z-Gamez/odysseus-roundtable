"""XML tool calls that name the tool in the TAG must execute, not land as prose.

Real failure, Round Table run rt_769b9c966817: the Developer looped five times
against QA and never advanced. Its output carried 24 tool calls in this shape:

    <tool_call>
    <function=read_file>
    <parameter=path>
    SPEC.md
    </parameter>
    </function>
    </tool_call>

Everything upstream of the parser was working. The agent-debug log shows
`_is_api_model=True tools_sent=11` on every round, so tools WERE attached, and
llama-server returns proper native `tool_calls` when it recognises what the
model produced. It simply produced a shape the chat template does not
declare, so llama.cpp passed it through as content — and Odysseus's XML
patterns all expect `<invoke name="tool">`, where the name is an ATTRIBUTE.

This is matched on syntax alone. Qwen3-Coder and xLAM-style fine-tunes are
where it was first seen, but nothing in the parser inspects the model, so any
model emitting this shape is covered.
Here the names ride in the tag itself, so every pattern missed and
parse_tool_blocks returned zero blocks.

Nothing executed, the QA gate never saw a change, and the Developer tried
again. Recovering these is what breaks the loop.
"""
import pytest

import src.agent_tools  # noqa: F401  — enter via agent_tools; tool_parsing is circular
from src.tool_parsing import parse_tool_blocks


QWEN_CALL = """<tool_call>
<function=read_file>
<parameter=path>
SPEC.md
</parameter>
</function>
</tool_call>"""


def test_the_exact_shape_from_the_failed_run():
    blocks = parse_tool_blocks(QWEN_CALL)
    assert len(blocks) == 1, (
        "the Qwen/xLAM <function=name> dialect parsed to nothing; the model "
        "narrates tool calls while the run makes no progress"
    )
    assert blocks[0].tool_type == "read_file"
    assert "SPEC.md" in blocks[0].content


def test_multiple_parameters_are_all_captured():
    text = """<tool_call>
<function=write_file>
<parameter=path>
index.html
</parameter>
<parameter=content>
<h1>hi</h1>
</parameter>
</function>
</tool_call>"""
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "write_file"
    assert "index.html" in blocks[0].content


def test_capitalised_function_name_still_resolves():
    """Models capitalise tool names; the name map is case-sensitive."""
    text = "<tool_call>\n<function=Read_File>\n<parameter=path>\na.txt\n</parameter>\n</function>\n</tool_call>"
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1 and blocks[0].tool_type == "read_file"


def test_prose_around_the_call_does_not_break_it():
    text = ("I'll start by reading the spec.\n\n" + QWEN_CALL +
            "\nThat should tell me what to build.")
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1 and blocks[0].tool_type == "read_file"


def test_unknown_tool_name_is_dropped_not_executed():
    """The dialect must not become a way to invoke something off the tool map."""
    text = ("<tool_call>\n<function=definitely_not_a_tool>\n"
            "<parameter=x>\n1\n</parameter>\n</function>\n</tool_call>")
    assert parse_tool_blocks(text) == []


def test_unclosed_function_tag_is_ignored():
    """Truncated streams must not yield a half-parsed call."""
    text = "<tool_call>\n<function=read_file>\n<parameter=path>\nSPEC.md\n"
    assert parse_tool_blocks(text) == []


def test_opener_flood_terminates():
    """The delimiters are forward-only; an opener flood must not go quadratic."""
    import time
    text = "<function=read_file>" * 4000          # openers, never closed
    start = time.time()
    parse_tool_blocks(text)
    assert time.time() - start < 5, "parser degraded on unclosed-opener input"


def test_invoke_dialect_still_works():
    """The pre-existing <invoke name="..."> path must not regress."""
    text = ('<tool_call><invoke name="read_file">'
            '<parameter name="path">SPEC.md</parameter></invoke></tool_call>')
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 1 and blocks[0].tool_type == "read_file"


def test_parsing_is_model_agnostic():
    """No model or endpoint is consulted — the shape alone decides.

    This shape shows up in Qwen3-Coder and xLAM-style fine-tunes, but gating on
    a model name would mean the next model to emit it silently loops again, the
    exact failure this file exists to prevent.
    """
    import inspect
    from src.tool_parsing import _parse_xml_func_eq

    for fn in (parse_tool_blocks, _parse_xml_func_eq):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"model", "model_name", "endpoint", "endpoint_url",
                              "provider", "is_api_model"}), (
            f"{fn.__name__} takes a model/endpoint argument; this dialect must "
            f"be recognised on syntax alone, for every model"
        )


def test_every_tool_in_the_map_is_reachable_through_this_syntax():
    """Not just the handful seen in one failing run."""
    for tool, param, value in [
        ("read_file", "path", "a.md"),
        ("bash", "command", "ls -la"),
        ("web_search", "query", "physics ragdoll"),
        ("grep", "pattern", "TODO"),
        ("write_file", "path", "out.txt"),
    ]:
        text = (f"<tool_call><function={tool}><parameter={param}>{value}"
                f"</parameter></function></tool_call>")
        blocks = parse_tool_blocks(text)
        assert len(blocks) == 1 and blocks[0].tool_type == tool, (
            f"{tool} not reachable through the tag-name XML syntax"
        )
