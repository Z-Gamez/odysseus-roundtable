"""SAW orchestrator — the round-table state machine.

Pipeline:
    BSA -> Architect (design gate) -> [ Developer -> QAS -> Security ]xN -> Tech Writer

Gates (SAW stop-the-line / independence):
  - stop-the-line: BSA must produce acceptance criteria or the run halts.
  - design gate:   Architect must APPROVE the spec; REVISE loops back to BSA
                   (bounded), and a persistently-unapproved design halts the line.
  - QAS gate:      independent, write-disabled reviewer must PASS; FAIL loops to Dev.
  - security gate: independent (separate from QAS) reviewer must APPROVE; BLOCK
                   loops to Dev. QAS and Security can never be the same run.
  - Tech Writer:   documents the shipped change (no gate; cheap/local model).

Each role = one `stream_agent_loop` call. `run_pipeline()` is an async generator of
tagged SSE event strings, streamed to the UI via `src.agent_runs` (start/subscribe).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import AsyncGenerator, Dict, Optional, Set, Tuple

from src.saw import roles, store
from src.saw.roles import PIPELINE, RoleSpec

logger = logging.getLogger(__name__)

_DONE = "data: [DONE]\n\n"
MAX_DEV_QAS_ITERATIONS = 3   # Developer <-> QAS/Security retry budget (more = better for weaker local models)
MAX_ARCH_REVISIONS = 1       # Architect -> BSA spec-revision budget


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _role_override(role_key: str, owner: str):
    """Per-role model override from settings['saw_role_models'][role_key]
    ({endpoint_id, model}); returns (url, model, headers) or None if unset."""
    try:
        from src.settings import get_setting
        rc = (get_setting("saw_role_models", {}) or {}).get(role_key) or {}
        ep_id = (rc.get("endpoint_id") or "").strip()
        if not ep_id:
            return None
        from src.endpoint_resolver import resolve_endpoint_by_id
        return resolve_endpoint_by_id(ep_id, (rc.get("model") or "").strip() or None, owner=owner)
    except Exception as e:
        logger.debug("[saw] per-role override failed for %s: %s", role_key, e)
        return None


def _resolve(role: RoleSpec, owner: str) -> Optional[Tuple[str, str, dict]]:
    """Resolve a role's (endpoint_url, model, headers). Order of precedence:
       1. explicit per-role override (settings 'saw_role_models'), else
       2. the role's tier purpose (saw_heavy -> Claude / saw_cheap -> local)."""
    override = _role_override(role.key, owner)
    if override:
        return override
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
        return {
            "bash", "python", "web_search", "web_fetch", "read_file", "write_file",
            "edit_file", "ls", "glob", "grep", "get_workspace", "ask_user",
        }


def _write_spec(workspace: str, spec_text: str) -> None:
    """Persist BSA's spec to SPEC.md so downstream roles reliably have it on disk."""
    try:
        os.makedirs(workspace, exist_ok=True)
        with open(os.path.join(workspace, "SPEC.md"), "w", encoding="utf-8") as f:
            f.write(spec_text)
    except Exception as e:
        logger.warning("[saw] could not persist SPEC.md: %s", e)


def _read_spec(workspace: str) -> str:
    """Read the existing SPEC.md (the prior round's spec) so a follow-up run can build on
    what already exists instead of starting over."""
    try:
        with open(os.path.join(workspace, "SPEC.md"), encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _extract_spec(text: str) -> str:
    """Pull the clean spec (User Story / Acceptance Criteria / Implementation Notes) out of
    the BSA's full output, dropping the tool-call fences and narration the model emits — so
    SPEC.md is a clean source of truth instead of the model's raw transcript."""
    if not text:
        return text or ""
    m = re.search(r"(?im)^#{1,4}\s*(user story|acceptance criteria|spec\b)", text)
    spec = text[m.start():] if m else text
    spec = re.sub(r"```.*?```", "", spec, flags=re.S)   # strip fenced tool calls / code dumps
    spec = re.sub(r"\n{3,}", "\n\n", spec).strip()
    return spec


def _build_spec_md(bsa_text: str, title: str, acceptance: str) -> str:
    """Clean spec for SPEC.md. If the user supplied acceptance criteria, pin them at the top
    verbatim and mark them authoritative so the BSA can't silently replace them."""
    spec = _extract_spec(bsa_text)
    if acceptance and acceptance.strip():
        spec = (f"# {title}\n\n"
                "## Acceptance Criteria (from the ticket — AUTHORITATIVE, do not change)\n"
                f"{acceptance.strip()}\n\n---\n\n{spec}")
    elif not spec.lstrip().startswith("#"):
        spec = f"# {title}\n\n{spec}"
    return spec


def _list_workspace(workspace: str, limit: int = 80) -> str:
    """A real listing of files on disk, handed to QAS so it knows exactly what exists rather
    than trusting the Developer's narration."""
    try:
        skip = {".git", "__pycache__", "node_modules", "build", ".dart_tool", ".gradle", ".idea"}
        out = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), workspace).replace("\\", "/"))
                if len(out) >= limit:
                    return "\n".join(sorted(out)) + "\n… (truncated)"
        return "\n".join(sorted(out)) if out else "(no files yet)"
    except Exception:
        return "(could not list workspace)"


def _max_iterations() -> int:
    """Configurable Dev<->QAS retry budget (setting 'saw_max_iterations'; default
    MAX_DEV_QAS_ITERATIONS, clamped 1..8)."""
    try:
        from src.settings import get_setting
        return max(1, min(int(get_setting("saw_max_iterations", MAX_DEV_QAS_ITERATIONS)), 8))
    except Exception:
        return MAX_DEV_QAS_ITERATIONS


def _build_check(workspace: str):
    """Cheap, deterministic completeness/compile gate so a weak QAS can't false-pass
    obviously-broken work. Returns (ok, label, detail) or None when not recognised."""
    import glob, subprocess, sys as _sys
    try:
        # Flutter: the app entry point must actually exist and be real
        if os.path.exists(os.path.join(workspace, "pubspec.yaml")):
            main_dart = os.path.join(workspace, "lib", "main.dart")
            if not os.path.exists(main_dart):
                return False, "structure", "Required file lib/main.dart is MISSING — the app has no entry point."
            txt = open(main_dart, encoding="utf-8", errors="replace").read()
            if "runApp" not in txt or len(txt.strip()) < 80:
                return False, "structure", "lib/main.dart is empty or has no runApp() — the entry point is incomplete."
            # Deeper: run the Dart analyzer to catch real compile errors, not just a
            # missing entry point. Only ERROR-severity lines fail the gate (warnings/info
            # are reported but don't block).
            try:
                r = subprocess.run("flutter analyze", cwd=workspace, shell=True,
                                   capture_output=True, text=True, timeout=360)
                errs = [ln.strip() for ln in (r.stdout + r.stderr).splitlines()
                        if re.match(r"(?i)^error\b", ln.strip())]
                if errs:
                    return False, "flutter analyze", "Analyzer errors — fix these:\n" + "\n".join(errs[:30])
                return True, "flutter analyze", "lib/main.dart present; flutter analyze found no errors."
            except subprocess.TimeoutExpired:
                return True, "structure", "lib/main.dart present (flutter analyze timed out — skipped)."
            except Exception:
                return True, "structure", "lib/main.dart present (flutter analyze unavailable)."
        # Python: every file must compile
        pys = [p for p in glob.glob(os.path.join(workspace, "**", "*.py"), recursive=True)
               if "__pycache__" not in p][:60]
        if pys:
            r = subprocess.run([_sys.executable, "-m", "py_compile", *pys], cwd=workspace,
                               capture_output=True, text=True, timeout=120)
            return (r.returncode == 0), "py_compile", ((r.stdout + r.stderr).strip() or "All Python files compile.")[:1500]
    except Exception as e:
        logger.warning("[saw] build check error: %s", e)
    return None


_LOCAL_FILE_HINT = (
    "\n\n# IMPORTANT — how to create/edit files on this model\n"
    "The `python` and `bash` tools ARE enabled and available to you. NEVER claim a tool is "
    "blocked or disabled — just call it and do the work.\n"
    "ALWAYS write files with the `python` tool, using a triple-quoted string so newlines and "
    "indentation are preserved exactly, e.g.:\n"
    "with open(r'<absolute path>', 'w', encoding='utf-8') as f:\n"
    "    f.write('''<exact file contents>''')\n"
    "Do NOT use write_file/edit_file tools, and do NOT use echo/cat/heredoc in bash to write "
    "files — they corrupt newlines and indentation. Use the python tool for ALL file creation "
    "and edits, then run the file (or `python -m py_compile <file>`) to verify it before finishing."
)


def _is_local_endpoint(url: str) -> bool:
    u = (url or "").lower()
    return "11434" in u or "ollama" in u or "127.0.0.1" in u or "localhost" in u


def _augment_for_local(messages: list, url: str, has_python: bool) -> list:
    """Local models reliably drive `python`/`bash` tool calls but malform Odysseus's
    write_file/edit_file format (→ infinite loops). For local endpoints, tell roles that
    HAVE the python tool to do file I/O through it. No-op for cloud (Claude) endpoints and
    for roles WITHOUT python (e.g. BSA/Architect/Tech Writer) — ordering them to 'use the
    python tool' when they don't have it just makes them loop and dump commands into files."""
    if not has_python or not _is_local_endpoint(url):
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + _LOCAL_FILE_HINT
            return out
    return out


def _is_git_repo(ws: str) -> bool:
    try:
        import subprocess
        r = subprocess.run(["git", "-C", ws, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "true" in r.stdout
    except Exception:
        return False


def _commit_run_output(ws: str, run_id: str, message: str, mode: str) -> dict:
    """Commit the run's output and return a small summary dict.

    dry_run (local): commit on the branch the user is ALREADY on, so the generated files
    stay put in the workspace folder. No throwaway saw/<run_id> branch that the working
    tree then leaves — that is what made finished work look 'deleted'.
    github: commit on a fresh saw/<run_id> branch so it can be pushed and opened as a PR."""
    import subprocess

    def g(*args):
        return subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True, timeout=30)

    g("config", "user.email", "saw@local")
    g("config", "user.name", "SAW Release Engineer")
    if mode == "github":
        branch = f"saw/{run_id}"
        g("checkout", "-b", branch)
    else:
        cur = g("rev-parse", "--abbrev-ref", "HEAD")
        branch = (cur.stdout or "").strip() or "main"
    g("add", "-A")
    commit = g("commit", "-m", message or "chore: SAW round-table change")
    committed = commit.returncode == 0
    stat = g("show", "--stat", "--oneline", "HEAD") if committed else g("diff", "--cached", "--stat")
    return {
        "branch": branch,
        "committed": committed,
        "stat": (stat.stdout or "").strip()[:1800],
        "note": (commit.stdout + commit.stderr).strip()[:300],
    }


def _gh_available() -> bool:
    import shutil
    return shutil.which("gh") is not None


def _has_github_remote(ws: str) -> bool:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", ws, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "github.com" in (r.stdout or "")
    except Exception:
        return False


def _push_and_open_pr(ws: str, branch: str, title: str, body: str) -> dict:
    """Push the branch and open a GitHub PR via gh. Returns {ok, url|note}."""
    import subprocess
    if not _gh_available():
        return {"ok": False, "note": "gh CLI not installed"}
    if not _has_github_remote(ws):
        return {"ok": False, "note": "workspace has no GitHub 'origin' remote"}

    def run(args):
        return subprocess.run(args, cwd=ws, capture_output=True, text=True, timeout=120)

    push = run(["git", "push", "-u", "origin", branch])
    if push.returncode != 0:
        return {"ok": False, "note": "git push failed: " + (push.stderr or "")[:200]}
    pr = run(["gh", "pr", "create", "--title", title, "--body", body, "--head", branch])
    if pr.returncode != 0:
        return {"ok": False, "note": "gh pr create failed: " + (pr.stderr or "")[:200]}
    url = ""
    for line in (pr.stdout or "").splitlines():
        if line.strip().startswith("http"):
            url = line.strip()
    return {"ok": True, "url": url}


def merge_run(workspace: str, run_id: str, mode: Optional[str] = None) -> dict:
    """HITL approve & merge the run's saw/<run_id> branch. GitHub mode uses
    `gh pr merge`; otherwise merges locally into main/master (or establishes main)."""
    import subprocess
    branch = f"saw/{run_id}"
    if mode is None:
        try:
            from src.settings import get_setting
            mode = get_setting("saw_rte_mode", "dry_run")
        except Exception:
            mode = "dry_run"

    def g(*args):
        return subprocess.run(["git", "-C", workspace, *args], capture_output=True, text=True, timeout=120)

    # Local runs now commit straight to the working branch, so there's no saw/<run_id>
    # branch to merge. If it's absent, the change has already landed — nothing to do.
    if g("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        return {"ok": True, "detail": "This run was committed directly to your working branch — nothing to merge."}
    orig = (g("rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip() or branch

    if mode == "github" and _gh_available() and _has_github_remote(workspace):
        m = subprocess.run(["gh", "pr", "merge", branch, "--merge", "--delete-branch"],
                           cwd=workspace, capture_output=True, text=True, timeout=120)
        if m.returncode == 0:
            return {"ok": True, "detail": f"Merged the GitHub PR for {branch}."}
        return {"ok": False, "detail": "gh pr merge failed: " + (m.stderr or "")[:300]}

    main_ok = g("rev-parse", "--verify", "--quiet", "refs/heads/main").returncode == 0
    master_ok = g("rev-parse", "--verify", "--quiet", "refs/heads/master").returncode == 0
    base = "main" if main_ok else ("master" if master_ok else None)
    if base is None:
        g("branch", "-f", "main", branch); g("checkout", "main")
        return {"ok": True, "detail": f"Established 'main' at the approved change ({branch})."}
    if base == branch:
        return {"ok": True, "detail": f"{branch} is already the base branch."}
    g("checkout", base)
    mg = g("merge", "--no-ff", branch, "-m", f"Merge {branch} (SAW HITL approved)")
    if mg.returncode == 0:
        return {"ok": True, "detail": f"Merged {branch} into {base} locally."}
    g("merge", "--abort")
    g("checkout", orig)   # never strand the user on a branch missing their files
    return {"ok": False, "detail": f"Merge into {base} hit conflicts; aborted — left you on {orig}."}


# ---------------------------------------------------------------------------
# Gate parsers
# ---------------------------------------------------------------------------

_AC_HEADING = re.compile(r"acceptance\s+criteria", re.I)
_AC_ITEM = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.M)
_QAS_VERDICT = re.compile(r"QAS\s+VERDICT:\s*(PASS|FAIL)", re.I)
_ARCH_VERDICT = re.compile(r"ARCH\s+VERDICT:\s*(APPROVE|REVISE)", re.I)
_SEC_VERDICT = re.compile(r"SECURITY\s+VERDICT:\s*(APPROVE|BLOCK)", re.I)


def _has_acceptance_criteria(text: str) -> bool:
    return bool(text and _AC_HEADING.search(text) and _AC_ITEM.search(text))


def _parse(rx: re.Pattern, text: str) -> Optional[str]:
    m = rx.search(text or "")
    return m.group(1).lower() if m else None


def _parse_verdict(text: str) -> Optional[str]:     # qas: pass|fail
    return _parse(_QAS_VERDICT, text)


def _parse_arch(text: str) -> Optional[str]:        # approve|revise
    return _parse(_ARCH_VERDICT, text)


def _parse_security(text: str) -> Optional[str]:    # approve|block
    return _parse(_SEC_VERDICT, text)


def _rte_section(text: str, name: str) -> str:
    m = re.search(r"##\s*" + name + r"\s*\n(.+?)(?=\n##\s|\Z)", text or "", re.S | re.I)
    return m.group(1).strip() if m else ""


def _first_content_line(section: str) -> str:
    """First non-blank, non-code-fence line of a section (models often wrap the
    commit message / title in ``` fences)."""
    for ln in (section or "").splitlines():
        s = ln.strip()
        if s and not s.startswith("```"):
            return s
    return ""


def _parse_rte(text: str, fallback_title: str) -> Tuple[str, str, str]:
    """Extract (commit_message, pr_title, pr_body) from RTE output, with fallbacks."""
    commit = _first_content_line(_rte_section(text, "Commit Message")) or f"feat: {fallback_title}"
    title = _first_content_line(_rte_section(text, "PR Title")) or fallback_title
    body = _rte_section(text, "PR Body") or (text or "").strip()
    return commit, title, body


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _bsa_messages(role: RoleSpec, title: str, description: str, acceptance: str,
                  workspace: str, arch_feedback: Optional[str] = None, prior_spec: str = "") -> list:
    ac_block = (
        ("ACCEPTANCE CRITERIA PROVIDED BY THE USER — these are REQUIRED and AUTHORITATIVE. "
         "Reproduce them EXACTLY under your '## Acceptance Criteria' heading; you may ADD "
         "clarifying criteria but must NOT change, drop, or reword any of these:\n" + acceptance)
        if acceptance.strip()
        else "No acceptance criteria were provided — you MUST define them."
    )
    user = (
        f"# Workspace\n{workspace}\n\n"
        f"# Ticket\nTitle: {title}\n\nDescription:\n{description or '(none)'}\n\n{ac_block}"
    )
    if prior_spec.strip():
        user = (
            "# FOLLOW-UP — change request on existing, working software\n"
            "A previous round already shipped a working implementation; ALL of its files are still in "
            "the workspace and its spec is shown below. Do NOT start over. Explore the existing files "
            "(ls/glob/grep/read_file), then re-emit the FULL spec UPDATED to fold in only the user's "
            "requested change while preserving everything that already works. In the ticket below, the "
            "Description is the user's requested change — not a brand-new project.\n\n"
            "## Existing SPEC.md (what already exists)\n" + prior_spec.strip() + "\n\n---\n\n" + user
        )
    if arch_feedback:
        user += ("\n\n# System Architect requested revisions (address every point, "
                 "then re-emit the full spec)\n" + arch_feedback)
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _arch_messages(role: RoleSpec, title: str, spec_text: str, workspace: str) -> list:
    user = (
        f"# Workspace (the code/spec live here)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec to review (also saved as SPEC.md)\n{spec_text}\n\n"
        "Review the design BEFORE implementation. End with the verdict line."
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _dev_messages(role: RoleSpec, title: str, spec_text: str,
                  review_feedback: Optional[str], workspace: str,
                  arch_notes: Optional[str] = None, continuation: bool = False) -> list:
    user = (
        f"# Workspace (build here — use absolute paths under this dir)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec (from BSA)\n{spec_text}\n\n"
        "SPEC.md has been saved in the workspace. Read it, then IMPLEMENT the code with "
        "write_file/edit_file. Do NOT finish until the required files actually exist on disk."
    )
    if continuation:
        user += ("\n\n# This is a CHANGE on existing, working code\n"
                 "The workspace ALREADY contains a complete working implementation from a prior round. "
                 "Read the existing files FIRST, then MODIFY them in place to satisfy the updated spec. "
                 "Apply only what the change requires; do NOT rewrite or delete working code the change "
                 "doesn't touch.")
    if arch_notes and arch_notes.strip():
        user += "\n\n# System Architect's design review (apply this guidance)\n" + arch_notes
    if review_feedback:
        user += ("\n\n# Previous review FAILED — you must address every point\n"
                 + review_feedback)
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _qas_messages(role: RoleSpec, title: str, spec_text: str, dev_text: str,
                  workspace: str, file_list: str = "", build_note: str = "") -> list:
    build_block = (f"# Automated build/structure check (authoritative)\n{build_note}\n\n" if build_note else "")
    user = (
        f"# Workspace (the code is here — use absolute paths under this dir)\n{workspace}\n\n"
        f"# Files actually on disk right now\n{file_list or '(unknown)'}\n\n"
        f"{build_block}"
        f"# Ticket\n{title}\n\n# Spec\n{spec_text}\n\n# Developer's report (context only)\n{dev_text}\n\n"
        "The files listed above DO exist — READ them yourself; never claim files are missing "
        "without checking, and do not just trust the report. Read each changed file, run the "
        "verification steps from SPEC.md, validate against the acceptance criteria, then end "
        "with the verdict line."
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _security_messages(role: RoleSpec, title: str, spec_text: str, dev_text: str,
                       workspace: str) -> list:
    user = (
        f"# Workspace (the code is here)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec\n{spec_text}\n\n# Developer's report\n{dev_text}\n\n"
        "QAS has already passed this. Now do an INDEPENDENT security review of the "
        "changed files. End with the verdict line."
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _techwriter_messages(role: RoleSpec, title: str, spec_text: str, dev_text: str,
                         workspace: str) -> list:
    user = (
        f"# Workspace\n{workspace}\n\n# Ticket\n{title}\n\n# Spec\n{spec_text}\n\n"
        f"# What was built\n{dev_text}\n\n"
        "The change shipped (QA + security passed). Add concise docs for it. Do not change code."
    )
    return [{"role": "system", "content": role.system_prompt},
            {"role": "user", "content": user}]


def _rte_messages(role: RoleSpec, title: str, spec_text: str, dev_text: str,
                  workspace: str) -> list:
    user = (
        f"# Workspace (your shell runs here)\n{workspace}\n\n"
        f"# Ticket\n{title}\n\n# Spec\n{spec_text}\n\n# What was built\n{dev_text}\n\n"
        "The change passed design, QA, and security. Inspect it (git status / read files) "
        "and produce the commit message + PR text per your output contract."
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

    messages = _augment_for_local(messages, url, "python" in role.allowed_tools)
    disabled = role.disabled_against(universe)
    sid = f"saw:{run_id}:{role.key}:{iteration}"
    acc: list = []
    try:
        async for chunk in stream_agent_loop(
            url, model, messages,
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
    except Exception as e:
        logger.error("[saw] role %s failed: %s", role.key, e, exc_info=True)
        yield _sse({"type": "delta", "role": role.key, "text": f"\n[role error: {e}]\n"})
    result["text"] = "".join(acc).strip()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(run_id: str, title: str, description: str, acceptance: str,
                       workspace: str, owner: str, parent_run_id: str = "") -> AsyncGenerator[str, None]:
    store.create_run(run_id, title, description, acceptance, workspace, owner)
    # Follow-up run: the prior round's SPEC.md and files are still in the workspace and
    # serve as the team's memory of what already exists, so we build on it instead of anew.
    prior_spec = _read_spec(workspace) if parent_run_id else ""
    yield _sse({"type": "run_start", "run_id": run_id, "title": title, "parent_run_id": parent_run_id,
                "pipeline": [r.key for r in PIPELINE], "workspace": workspace})
    state = {"idx": 0}
    universe = _tool_universe()

    try:
        # ---------- Role 1: BSA (stop-the-line is the one hard early gate) ----------
        bsa = roles.BSA
        ep = _resolve(bsa, owner)
        if ep is None:
            async for ev in _halt(run_id, "provider",
                                  "No saw_heavy chat model configured in Odysseus Settings."):
                yield ev
            return
        url, model, headers = ep
        yield _sse({"type": "role_start", "role": bsa.key, "title": bsa.title,
                    "model": model, "purpose": bsa.endpoint_purpose, "iteration": 1})
        res: Dict[str, str] = {"text": ""}
        async for ev in _run_role(bsa, url, model, headers,
                                  _bsa_messages(bsa, title, description, acceptance, workspace,
                                                prior_spec=prior_spec),
                                  workspace, owner, run_id, universe, 1, res):
            yield ev
        bsa_text = res["text"]
        store.add_step(run_id, state["idx"], bsa.key, 1, model, bsa.endpoint_purpose, bsa_text)
        state["idx"] += 1
        yield _sse({"type": "role_done", "role": bsa.key, "iteration": 1, "chars": len(bsa_text)})

        if not (acceptance.strip() or _has_acceptance_criteria(bsa_text)):
            async for ev in _halt(run_id, "stop-the-line",
                                  "BSA produced no acceptance criteria — stopping the line."):
                yield ev
            return
        yield _sse({"type": "gate", "gate": "stop-the-line", "status": "pass",
                    "detail": ("Using the acceptance criteria you provided." if acceptance.strip()
                               else "Acceptance criteria present.")})
        # Clean, single source of truth: strip tool-call noise; pin the user's AC if provided.
        # Downstream roles read this cleaned spec, not the BSA's raw transcript.
        bsa_text = _build_spec_md(bsa_text, title, acceptance)
        _write_spec(workspace, bsa_text)

        # ---------- Role 2: System Architect (ADVISORY — never halts or loops) ----------
        # Its review is recorded and passed to the Developer as design guidance. A weak
        # model that flubs its verdict must NOT be able to stop the line.
        arch_text = ""
        arch = roles.SYSTEM_ARCHITECT
        aep = _resolve(arch, owner)
        if aep is not None:
            aurl, amodel, aheaders = aep
            yield _sse({"type": "role_start", "role": arch.key, "title": arch.title,
                        "model": amodel, "purpose": arch.endpoint_purpose, "iteration": 1})
            ares: Dict[str, str] = {"text": ""}
            async for ev in _run_role(arch, aurl, amodel, aheaders,
                                      _arch_messages(arch, title, bsa_text, workspace),
                                      workspace, owner, run_id, universe, 1, ares):
                yield ev
            arch_text = ares["text"]
            averdict = _parse_arch(arch_text)
            store.add_step(run_id, state["idx"], arch.key, 1, amodel, arch.endpoint_purpose,
                           arch_text, gate="design", verdict=averdict or "advisory")
            state["idx"] += 1
            yield _sse({"type": "role_done", "role": arch.key, "iteration": 1, "chars": len(arch_text)})
            if averdict == "revise":
                yield _sse({"type": "gate", "gate": "design", "status": "fail",
                            "detail": "Architect flagged design concerns — passed to the Developer (advisory)."})
            else:
                yield _sse({"type": "gate", "gate": "design", "status": "pass",
                            "detail": "Architect review complete."})

        # ---------- Implementation loop: Developer -> build gate -> QAS -> Security ----------
        shipped = False
        feedback: Optional[str] = None
        dev_text = ""
        max_iter = _max_iterations()
        for iteration in range(1, max_iter + 1):
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
                                      _dev_messages(dev, title, bsa_text, feedback, workspace, arch_text,
                                                    continuation=bool(parent_run_id)),
                                      workspace, owner, run_id, universe, iteration, dres):
                yield ev
            dev_text = dres["text"]
            store.add_step(run_id, state["idx"], dev.key, iteration, dmodel, dev.endpoint_purpose, dev_text)
            state["idx"] += 1
            yield _sse({"type": "role_done", "role": dev.key, "iteration": iteration, "chars": len(dev_text)})

            # Deterministic build/structure gate — a weak QAS can't false-pass broken work.
            build_note = ""
            bchk = await asyncio.to_thread(_build_check, workspace)
            if bchk is not None:
                bok, blabel, bdetail = bchk
                build_note = "[%s] %s\n%s" % (blabel, "PASSED" if bok else "FAILED", bdetail)
                first = bdetail.splitlines()[0] if bdetail else ""
                yield _sse({"type": "gate", "gate": "build", "status": "pass" if bok else "fail",
                            "detail": ("%s: passed" % blabel) if bok else ("%s FAILED — %s" % (blabel, first))})
                if bok is False:
                    feedback = ("The automated build/structure check FAILED — fix this before "
                                "anything else:\n" + bdetail)
                    continue

            # QAS gate
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
                                      _qas_messages(qas, title, bsa_text, dev_text, workspace, _list_workspace(workspace), build_note),
                                      workspace, owner, run_id, universe, iteration, qres):
                yield ev
            qas_text = qres["text"]
            qverdict = _parse_verdict(qas_text)
            store.add_step(run_id, state["idx"], qas.key, iteration, qmodel, qas.endpoint_purpose,
                           qas_text, gate="qas", verdict=qverdict or "unknown")
            state["idx"] += 1
            yield _sse({"type": "role_done", "role": qas.key, "iteration": iteration, "chars": len(qas_text)})

            if qverdict != "pass":
                yield _sse({"type": "gate", "gate": "qas", "status": "fail",
                            "detail": ("QAS rejected the work." if qverdict == "fail"
                                       else "QAS verdict unclear — treated as FAIL."),
                            "iteration": iteration})
                feedback = qas_text
                continue
            yield _sse({"type": "gate", "gate": "qas", "status": "pass",
                        "detail": "QAS approved — acceptance criteria met."})

            # Security gate (independent of QAS)
            sec = roles.SECURITY
            sep = _resolve(sec, owner)
            if sep is None:
                async for ev in _halt(run_id, "provider", "Security endpoint unavailable."):
                    yield ev
                return
            surl, smodel, sheaders = sep
            yield _sse({"type": "role_start", "role": sec.key, "title": sec.title,
                        "model": smodel, "purpose": sec.endpoint_purpose, "iteration": iteration})
            sres: Dict[str, str] = {"text": ""}
            async for ev in _run_role(sec, surl, smodel, sheaders,
                                      _security_messages(sec, title, bsa_text, dev_text, workspace),
                                      workspace, owner, run_id, universe, iteration, sres):
                yield ev
            sec_text = sres["text"]
            sverdict = _parse_security(sec_text)
            store.add_step(run_id, state["idx"], sec.key, iteration, smodel, sec.endpoint_purpose,
                           sec_text, gate="security", verdict=sverdict or "unknown")
            state["idx"] += 1
            yield _sse({"type": "role_done", "role": sec.key, "iteration": iteration, "chars": len(sec_text)})

            if sverdict == "approve":
                yield _sse({"type": "gate", "gate": "security", "status": "pass",
                            "detail": "Security approved — no blocking issues."})
                shipped = True
                break
            yield _sse({"type": "gate", "gate": "security", "status": "fail",
                        "detail": ("Security blocked the change." if sverdict == "block"
                                   else "Security verdict unclear — treated as BLOCK."),
                        "iteration": iteration})
            feedback = sec_text

        # ---------- Tech Writer (docs, cheap/local model, no gate) ----------
        if shipped:
            tw = roles.TECH_WRITER
            tep = _resolve(tw, owner)
            if tep is not None:
                turl, tmodel, theaders = tep
                yield _sse({"type": "role_start", "role": tw.key, "title": tw.title,
                            "model": tmodel, "purpose": tw.endpoint_purpose, "iteration": 1})
                tres: Dict[str, str] = {"text": ""}
                async for ev in _run_role(tw, turl, tmodel, theaders,
                                          _techwriter_messages(tw, title, bsa_text, dev_text, workspace),
                                          workspace, owner, run_id, universe, 1, tres):
                    yield ev
                store.add_step(run_id, state["idx"], tw.key, 1, tmodel, tw.endpoint_purpose, tres["text"])
                state["idx"] += 1
                yield _sse({"type": "role_done", "role": tw.key, "iteration": 1, "chars": len(tres["text"])})

        # ---------- RTE: package the shipped change as a PR (dry-run) ----------
        if shipped:
            rte = roles.RTE
            rep = _resolve(rte, owner)
            if rep is not None and _is_git_repo(workspace):
                rurl, rmodel, rheaders = rep
                yield _sse({"type": "role_start", "role": rte.key, "title": rte.title,
                            "model": rmodel, "purpose": rte.endpoint_purpose, "iteration": 1})
                rres: Dict[str, str] = {"text": ""}
                async for ev in _run_role(rte, rurl, rmodel, rheaders,
                                          _rte_messages(rte, title, bsa_text, dev_text, workspace),
                                          workspace, owner, run_id, universe, 1, rres):
                    yield ev
                store.add_step(run_id, state["idx"], rte.key, 1, rmodel, rte.endpoint_purpose, rres["text"])
                state["idx"] += 1
                yield _sse({"type": "role_done", "role": rte.key, "iteration": 1, "chars": len(rres["text"])})
                commit_msg, pr_title, pr_body = _parse_rte(rres["text"], title)
                try:
                    from src.settings import get_setting
                    rte_mode = get_setting("saw_rte_mode", "dry_run")
                except Exception:
                    rte_mode = "dry_run"
                git = _commit_run_output(workspace, run_id, commit_msg, rte_mode)
                pr_url = ""; mode_out = "dry_run"
                if rte_mode == "github" and git["committed"]:
                    gh = _push_and_open_pr(workspace, git["branch"], pr_title, pr_body)
                    if gh.get("ok"):
                        mode_out = "github"; pr_url = gh.get("url", "")
                    else:
                        pr_body += f"\n\n_GitHub PR not opened ({gh.get('note')}); kept as a local branch._"
                yield _sse({"type": "pr", "role": rte.key, "mode": mode_out, "run_id": run_id,
                            "branch": git["branch"], "committed": git["committed"], "url": pr_url,
                            "title": pr_title, "body": pr_body, "commit": commit_msg,
                            "stat": git["stat"], "note": git["note"]})
            elif rep is not None:
                yield _sse({"type": "gate", "gate": "rte", "status": "halt",
                            "detail": "Workspace is not a git repo — skipping PR shepherding."})

        status = "passed" if shipped else "failed"
        yield _sse({"type": "run_done", "status": status,
                    "detail": ("Shipped." if shipped
                               else f"Failed after {max_iter} implementation iterations.")})
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
