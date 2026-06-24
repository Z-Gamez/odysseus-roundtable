"""SAW — SAFe Agentic Workflow harness for Odysseus.

A thin multi-agent orchestration layer that runs a round-table of role-scoped
agents (BSA -> Developer -> QAS ...) on top of Odysseus's existing single-agent
loop (`src.agent_loop.stream_agent_loop`), enforcing stop-the-line quality gates
and evidence-based delivery.

Design notes:
  - Each SAFe role is ONE call to `stream_agent_loop` with a role-specific system
    prompt, tool allow-list, and model (hybrid: Claude for heavy roles via the
    "default" endpoint, local Ollama for cheap roles via the "utility" endpoint).
  - The orchestrator is an async generator of tagged SSE events; it is streamed to
    the UI via the existing `src.agent_runs` pub-sub (start/subscribe/stop).
  - Role prompts here are ADAPTED from bybren-llc/safe-agentic-workflow (MIT) and
    tuned for Odysseus's tool/workspace environment. SAW is the spec; this is the
    port.

Status: Phase 1 vertical slice (BSA -> Developer -> QAS).
"""

__all__ = ["roles", "orchestrator", "store"]

SAW_VERSION = "0.1.0"
