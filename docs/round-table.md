# Round Table — a SAFe multi-agent team inside Odysseus

The **Round Table** turns a single ticket into a small "team" of AI agents that
collaborate to ship working software, the way a SAFe (Scaled Agile) team would —
each role reviews the previous one's work behind real quality gates.

Open it from the **⊹ Round Table** button in the left rail (or the sidebar item).

## The team & pipeline

A run flows through seven roles, in order:

| Role | Does | Can execute? |
|------|------|:---:|
| **BSA** (Business Systems Analyst) | Turns the ticket into a testable spec (`SPEC.md`) | no — analysis only |
| **System Architect** | Advisory design review of the spec before code | no |
| **Developer** | Implements the spec in the workspace | yes (build/run) |
| **QAS** (Quality Assurance) | Independent reviewer — runs the verification, decides PASS/FAIL | yes (tests) |
| **Security Engineer** | Independent security review of the change | yes (checks) |
| **Technical Writer** | Documents what shipped | no — docs only |
| **RTE** (Release Engineer) | Packages the change as a commit + PR text | no (git inspect) |

Each role only gets the tools its job needs (least privilege): the non-implementing
roles cannot run shell/Python, so a writer can never launch your app and a reviewer
can never edit the code it's grading.

### Gates

- **Stop-the-line** — the run halts if the BSA can't produce acceptance criteria.
- **Design review** (advisory) — the Architect's notes are passed to the Developer.
- **Build/structure check** (deterministic) — after the Developer, the orchestrator
  itself verifies the result (e.g. a Flutter app must have a real `lib/main.dart`;
  Python must compile; `flutter analyze` must report no errors). A weak reviewer
  can't rubber-stamp broken work — failures loop back to the Developer.
- **QAS gate** — PASS/FAIL on the acceptance criteria; FAIL loops back to the Developer.
- **Security gate** — APPROVE/BLOCK.

## Configuring it

Open **⚙ Models** (top of the panel):

- **Per-role model** — route each role to any enabled endpoint/model. Leave a role on
  *Default* to use its tier: `saw_heavy` (your strong/API model) or `saw_cheap` (local).
  A common, reliable setup is **Developer on a strong model, the rest local**.
- **Max attempts** — the Dev↔QA retry budget (1–10) before a run fails.
- **Release mode** — `dry_run` (commit locally) or `github` (open a real PR).

Local Ollama models work too; file I/O for local models is routed through the `python`
tool because they tend to malform the structured write-file format.

## Iterating

- **History** (🕘) — every past run; click to view its transcript, or **↻ Reuse** to
  load its ticket back into the form.
- **Continue the discussion** — after a successful run, a box appears to request
  changes. The team keeps everything it built (the workspace + `SPEC.md` are its
  memory) and re-runs the pipeline, applying only your change.

## Where your code lands

Runs commit to the **branch you're on** so the generated files stay in your workspace
folder. In `github` mode the run uses a `saw/<run_id>` branch and the **Approve & Merge**
button merges the PR. Set the workspace per-ticket; the field remembers your last value.

## Attribution & license

This **Round Table** feature is part of a community fork of
[**Odysseus**](https://github.com/pewdiepie-archdaemon/odysseus), a self-hosted AI
workspace, which is licensed under **AGPL-3.0**. This fork remains **AGPL-3.0** — see
[`LICENSE`](../LICENSE).

The role prompts are adapted from the
[**SAFe Agentic Workflow**](https://github.com/bybren-llc/safe-agentic-workflow)
project (MIT). All credit to the upstream Odysseus and SAW authors; the Round Table
integration is the fork's addition.
