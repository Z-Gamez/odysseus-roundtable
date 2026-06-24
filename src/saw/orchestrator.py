"""SAW orchestrator — the round-table state machine (Phase 1 slice).

Pipeline:  BSA  ->  [ Developer  ->  QAS ]xN  (loop until QAS PASS or max iters)

Each role is one `stream_agent_loop` call (role prompt + scoped tools + per-role
model via Odysseus endpoint "purposes"). Between roles we enforce SAW's gates:
  - stop-the-line: BSA must produce acceptance criteria or the run halts.
  - QAS gate: an independent, write-disabled reviewer must emit a PASS verdict;
    FAIL loops back to the Developer with the feedback attached.

`run_pipeline()` is an async generator of SSE event strings. It is meant to be
handed to `src.agent_runs.start(run_id, gen)`; the route streams it to the UI via
`src.agent_runs.subscribe(run_id)` (replay + live + heartbeats for free).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncGenerator, Dict, Optional, Set, Tuple

from src.saw import roles, store
from src.saw.roles import PIPELINE, RoleSpec

logger = logging.getLogger(__name__)

_DONE = "data: [DONE]\n\n"
MAX_DEV_QAS_ITERATIONS = 2


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve(role: RoleSpec, owner: str) -> Optional[Tuple[str, str, dict]]:
    """Resolve a role's (endpoint_url, model, headers) from its Odysseus endpoint
    purpose ("default" = Claude/heavy, "utility" = Ollama/cheap). None if unset."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint(role.endpoint_purpose, owner=owner)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[saw] endpoint resolve failed for %s: %s", role.key, e)
        return None
    if not url or not model:
        return None
    return url, model, headers or {}


def _tool_universe() -> Set[str]:
    try:
        from src.agent_tools import TOOL_TAGS
        return set(TOOL_TAGS)
    except Exception:
        # Minimal fallback so role scoping still does something useful.
        return {
            "bash", "python", "web_search", "web_fetch", "read_file", "write_file",
            "edit_file", "ls", "glob", "grep", "get_workspace", "ask_user",
        }


# ---------------------------------------------------------------------------
# Gate parsers
# ---------------------------------------------------------------------------

_AC_HEADING = re.compile(r"acceptance\s+criteria", re.I)
_AC_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.M)
_VERDICT = re.compile(r"QAS\s+VERDICT:\s*(PASS|FAIL)", re.I)


def _has_acceptance_criteria(text: str) -> bool:
    return bool(text and _AC_HEADING.search(text) and _AC_ITEM.search(text))


def _parse_verdict(text: str) -> Optional[str]:
    m = _VERDICT.search(text or "")
    if not m:
        return None
    return m.group(1).lower()  # "pass" | "fail"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _bsa_messages(role: RoleSpec, title: str, description: str, acceptance: str,
                  workspace: str) -> list:
    ac_block = (
        f"Provided acceptance criteria:\n{acceptance}"
        if acceptance.strip()
        else "No acceptance criteria were provided — you MUST define them."
    )
    user = (
        f"# Ticket\nTitle: {title}\n\nDescription:\n{description or '(none)'}\n\n"
        f"{ac_block}\n\nWorkspace: {workspace}"
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _dev_messages(role: RoleSpec, title: str, spec_text: str,
                  qas_feedback: Optional[str], workspace: str) -> list:
    user = (
        f"# Workspace (build here — use absolute paths under this dir)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec (from BSA)\n{spec_text}\n\n"
        "SPEC.md has been saved in the workspace. Read it, then IMPLEMENT the code with "
        "write_file/edit_file. Do NOT finish until the required files actually exist on disk."
    )
    if qas_feedback:
        user += (
            "\n\n# Previous QAS review (FAILED — you must address every point)\n"
            f"{qas_feedback}"
        )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _qas_messages(role: RoleSpec, title: str, spec_text: str, dev_text: str,
                  workspace: str) -> list:
    user = (
        f"# Workspace (the code is here — use absolute paths under this dir)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec\n{spec_text}\n\n"
        f"# Developer's report\n{dev_text}\n\n"
        "Independently validate the work against the acceptance criteria in SPEC.md. "
        "List the files, run the verification steps, then end with the verdict line."
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Per-role runner
# ---------------------------------------------------------------------------

def _translate(chunk: str, role_key: str, acc: list) -> list:
    """Turn one raw stream_agent_loop SSE chunk into our tagged UI events,
    accumulating assistant text (minus reasoning) into `acc`."""
    out: list = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if "delta" in data:
            if not data.get("thinking"):
                acc.append(data["delta"])
                out.append(_sse({"type": "delta", "role": role_key, "text": data["delta"]}))
            continue
        t = data.get("type")
        if t == "tool_start":
            out.append(_sse({"type": "tool", "role": role_key, "phase": "start",
                             "tool": data.get("tool")}))
        elif t == "tool_output":
            out.append(_sse({"type": "tool", "role": role_key, "phase": "output",
                             "tool": data.get("tool")}))
    return out


async def _run_role(role: RoleSpec, url: str, model: str, headers: dict, messages: list,
                    workspace: str, owner: str, run_id: str, universe: Set[str],
                    iteration: int, result: dict) -> AsyncGenerator[str, None]:
    """Run one role via the agent loop; yield tagged events; store text in result."""
    from src.agent_loop import stream_agent_loop

    disabled = role.disabled_against(universe)
    sid = f"saw:{run_id}:{role.key}:{iteration}"
    acc: list = []
    try:
        async for chunk in stream_agent_loop(
            url,
            model,
            messages,
            headers=headers,
            temperature=role.temperature,
            max_rounds=role.max_rounds,
            session_id=sid,
            disabled_tools=disabled or None,
            relevant_tools=set(role.allowed_tools) or None,
            owner=owner,
            workspace=workspace,
        ):
            for ev in _translate(chunk, role.key, acc):
                yield ev
    except asyncio.CancelledError:
        result["text"] = "".join(acc).strip()
        raise
    except Exception as e:  # surface role failure but don't kill the pipeline frame
        logger.error("[saw] role %s failed: %s", role.key, e, exc_info=True)
        yield _sse({"type": "delta", "role": role.key, "text": f"\n[role error: {e}]\n"})
    result["text"] = "".join(acc).strip()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(run_id: str, title: str, description: str, acceptance: str,
                       workspace: str, owner: str) -> AsyncGenerator[str, None]:
    store.create_run(run_id, title, description, acceptance, workspace, owner)
    yield _sse({"type": "run_start", "run_id": run_id, "title": title,
                "pipeline": [r.key for r in PIPELINE], "workspace": workspace})
    idx = 0
    try:
        universe = _tool_universe()

        # ---- Role 1: BSA ----
        bsa = roles.BSA
        ep = _resolve(bsa, owner)
        if ep is None:
            async for ev in _halt(run_id, "provider",
                                  "No 'default' chat model configured. Set one in "
                                  "Odysseus Settings → Models."):
                yield ev
            return
        url, model, headers = ep
        yield _sse({"type": "role_start", "role": bsa.key, "title": bsa.title,
                    "model": model, "purpose": bsa.endpoint_purpose, "iteration": 1})
        res: Dict[str, str] = {"text": ""}
        async for ev in _run_role(bsa, url, model, headers,
                                  _bsa_messages(bsa, title, description, acceptance, workspace),
                                  workspace, owner, run_id, universe, 1, res):
            yield ev
        bsa_text = res["text"]
        store.add_step(run_id, idx, bsa.key, 1, model, bsa.endpoint_purpose, bsa_text)
        idx += 1
        yield _sse({"type": "role_done", "role": bsa.key, "iteration": 1, "chars": len(bsa_text)})

        # GATE: stop-the-line (no AC/DoD, no work)
        if not _has_acceptance_criteria(bsa_text):
            yield _sse({"type": "gate", "gate": "stop-the-line", "status": "halt",
                        "detail": "BSA produced no acceptance criteria — stopping the line."})
            yield _sse({"type": "run_done", "status": "halted", "detail": "no acceptance criteria"})
            store.set_run_status(run_id, "halted")
            yield _DONE
            return
        yield _sse({"type": "gate", "gate": "stop-the-line", "status": "pass",
                    "detail": "Acceptance criteria present."})

        # Persist BSA's spec to SPEC.md so the Developer reliably has it as a file,
        # regardless of whether BSA chose to call write_file itself.
        try:
            import os
            os.makedirs(workspace, exist_ok=True)
            with open(os.path.join(workspace, "SPEC.md"), "w", encoding="utf-8") as _f:
                _f.write(bsa_text)
        except Exception as _e:
            logger.warning("[saw] could not persist SPEC.md: %s", _e)

        # ---- Roles 2-3: Developer <-> QAS loop ----
        passed = False
        qas_feedback: Optional[str] = None
        for iteration in range(1, MAX_DEV_QAS_ITERATIONS + 1):
            # Developer
            dev = roles.DEVELOPER
            dep = _resolve(dev, owner)
            if dep is None:
                async for ev in _halt(run_id, "provider", "Developer endpoint unavailable."):
                    yield ev
                return
            durl, dmodel, dheaders = dep
            yield _sse({"type": "role_start", "role": dev.key, "title": dev.title,
                        "model": dmodel, "purpose": dev.endpoint_purpose, "iteration": iteration})
            dres: Dict[str, str] = {"text": ""}
            async for ev in _run_role(dev, durl, dmodel, dheaders,
                                      _dev_messages(dev, title, bsa_text, qas_feedback, workspace),
                                      workspace, owner, run_id, universe, iteration, dres):
                yield ev
            dev_text = dres["text"]
            store.add_step(run_id, idx, dev.key, iteration, dmodel, dev.endpoint_purpose, dev_text)
            idx += 1
            yield _sse({"type": "role_done", "role": dev.key, "iteration": iteration,
                        "chars": len(dev_text)})

            # QAS (independent, write-disabled)
            qas = roles.QAS
            qep = _resolve(qas, owner)
            if qep is None:
                async for ev in _halt(run_id, "provider", "QAS endpoint unavailable."):
                    yield ev
                return
            qurl, qmodel, qheaders = qep
            yield _sse({"type": "role_start", "role": qas.key, "title": qas.title,
                        "model": qmodel, "purpose": qas.endpoint_purpose, "iteration": iteration})
            qres: Dict[str, str] = {"text": ""}
            async for ev in _run_role(qas, qurl, qmodel, qheaders,
                                      _qas_messages(qas, title, bsa_text, dev_text, workspace),
                                      workspace, owner, run_id, universe, iteration, qres):
                yield ev
            qas_text = qres["text"]
            verdict = _parse_verdict(qas_text)
            store.add_step(run_id, idx, qas.key, iteration, qmodel, qas.endpoint_purpose,
                           qas_text, gate="qas", verdict=verdict or "unknown")
            idx += 1
            yield _sse({"type": "role_done", "role": qas.key, "iteration": iteration,
                        "chars": len(qas_text)})

            if verdict == "pass":
                yield _sse({"type": "gate", "gate": "qas", "status": "pass",
                            "detail": "QAS approved — acceptance criteria met."})
                passed = True
                break
            detail = ("QAS rejected the work." if verdict == "fail"
                      else "QAS verdict unclear — treated as FAIL.")
            yield _sse({"type": "gate", "gate": "qas", "status": "fail",
                        "detail": detail, "iteration": iteration})
            qas_feedback = qas_text

        status = "passed" if passed else "failed"
        yield _sse({"type": "run_done", "status": status,
                    "detail": ("Shipped." if passed
                               else f"Failed after {MAX_DEV_QAS_ITERATIONS} QAS iterations.")})
        store.set_run_status(run_id, status)
        yield _DONE

    except asyncio.CancelledError:
        store.set_run_status(run_id, "stopped")
        raise
    except Exception as e:
        logger.error("[saw] pipeline %s crashed: %s", run_id, e, exc_info=True)
        yield _sse({"type": "run_done", "status": "error", "detail": str(e)})
        store.set_run_status(run_id, "error")
        yield _DONE


async def _halt(run_id: str, gate: str, detail: str) -> AsyncGenerator[str, None]:
    yield _sse({"type": "gate", "gate": gate, "status": "halt", "detail": detail})
    yield _sse({"type": "run_done", "status": "halted", "detail": detail})
    store.set_run_status(run_id, "halted")
    yield _DONE
