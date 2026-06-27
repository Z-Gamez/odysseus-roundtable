"""Unit tests for the SAW Round Table gate parsers and spec cleaning.

These cover the load-bearing pure functions the orchestrator uses to drive its
gates (verdict parsing, acceptance-criteria detection, spec extraction, RTE
section parsing) so a refactor can't silently break the pipeline's control flow.
"""
from src.saw import orchestrator as orch


def test_qas_verdict_pass_fail():
    assert orch._parse_verdict("blah\nQAS VERDICT: PASS") == "pass"
    assert orch._parse_verdict("QAS VERDICT: FAIL - missing tests") == "fail"
    assert orch._parse_verdict("no verdict here") is None


def test_arch_and_security_verdicts():
    assert orch._parse_arch("ARCH VERDICT: APPROVE") == "approve"
    assert orch._parse_arch("ARCH VERDICT: REVISE - do x") == "revise"
    assert orch._parse_security("SECURITY VERDICT: BLOCK - sqli") == "block"
    assert orch._parse_security("SECURITY VERDICT: APPROVE") == "approve"


def test_has_acceptance_criteria():
    good = "## Acceptance Criteria\n- [ ] does a thing\n- [x] done"
    assert orch._has_acceptance_criteria(good) is True
    assert orch._has_acceptance_criteria("## Acceptance Criteria\n(no checklist)") is False
    assert orch._has_acceptance_criteria("nothing here") is False


def test_extract_spec_strips_fences_and_narration():
    raw = ("Sure! Let me write the spec.\n```\nget_workspace()\n```\n"
           "## User Story\nAs a user...\n\n## Acceptance Criteria\n- [ ] x")
    spec = orch._extract_spec(raw)
    assert spec.startswith("## User Story")
    assert "get_workspace" not in spec


def test_build_spec_md_pins_user_acceptance():
    spec = orch._build_spec_md("## User Story\nAs a user...", "My App", "- [ ] must do X")
    assert "AUTHORITATIVE" in spec
    assert "- [ ] must do X" in spec
    assert spec.lstrip().startswith("# My App")


def test_parse_rte_sections():
    text = ("## Commit Message\nfeat(x): do x\n\n## PR Title\nAdd x\n\n"
            "## PR Body\n### Summary\nit does x")
    commit, title, body = orch._parse_rte(text, "fallback")
    assert commit == "feat(x): do x"
    assert title == "Add x"
    assert "it does x" in body


def test_parse_rte_falls_back_when_missing():
    commit, title, body = orch._parse_rte("(model said nothing useful)", "My Ticket")
    assert commit == "feat: My Ticket"
    assert title == "My Ticket"


def test_first_content_line_skips_code_fences():
    assert orch._first_content_line("```\nfeat: real subject\n```") == "feat: real subject"
