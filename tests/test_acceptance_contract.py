"""Acceptance-driven assignment and quality-regression contract tests."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = (ROOT / "skills" / "use-deepseek-router" / "SKILL.md").read_text(encoding="utf-8")
REASONING = (ROOT / "runtime" / "reasoning.py").read_text(encoding="utf-8")


def test_skill_requires_acceptance_sections_and_parent_gate():
    for marker in (
        "ACCEPTANCE CRITERIA",
        "VERIFICATION OWNER",
        "STOPPING CONDITION",
        "Parent acceptance gate",
        "UNVERIFIED",
        "at most one",
        "MATERIAL GAPS",
        "DO NOT CHANGE",
        "deepseek_pro + REACT",
        "completed",
        "BLOCKED",
        "interrupted",
        "cancelled",
        "failed",
        "partial workspace edits",
        "NO duplicate",
        "NO overwrite",
        "NO takeover",
        "objective material engineering gap",
    ):
        assert marker in SKILL


def test_parent_gate_only_accepts_successful_completed_child_results():
    assert "Only a successful `completed` result enters the normal Acceptance Gate" in SKILL
    for status in ("BLOCKED", "interrupted", "cancelled", "failed"):
        assert f"`{status}`" in SKILL
    assert "must not be treated as successful completion" in SKILL


def test_materiality_gate_excludes_subjective_polish_and_bounds_follow_up():
    for marker in (
        "correctness",
        "required user-visible behavior",
        "integration",
        "robustness",
        "regression risk",
        "security / invariant",
        "concrete maintainability risk",
        "subjective polish",
        "automatic follow-up maximum = 1",
    ):
        assert marker in SKILL


def test_complex_pro_dispatch_must_carry_quality_closure():
    for marker in (
        "parent handles trivial deterministic edits",
        "Ordinary Pro + REACT handles clear, non-trivial implementation",
        "Complex Pro + REACT handles cross-module implementation",
        "assignment **must**\n  include `QUALITY CLOSURE`",
        "Every Complex Pro + REACT assignment must",
        "do not omit it after deciding the task is complex",
    ):
        assert marker in SKILL


def test_acceptance_contract_does_not_add_policy_or_transport_schema():
    assert "QUALITY_REFINEMENT" not in SKILL
    assert "AcceptanceProfile" not in SKILL
    assert "refinement_count" not in SKILL
    assert "acceptance_profile" not in SKILL
    assert "ACCEPTANCE CRITERIA" not in REASONING


def test_skill_dispatches_0149_through_manager_delegate():
    for marker in (
        "Codex 0.149 or newer",
        "direct_codex",
        "delegate --agent-type <role>",
        "Desktop-bundled Codex runtime",
        "Codex 0.148 or older",
        'spawn_agent(agent_type="deepseek_flash|deepseek_pro", fork_turns="none")',
    ):
        assert marker in SKILL


def test_complex_pro_gets_a_longer_bounded_direct_codex_timeout():
    for marker in (
        "ordinary assignments default to 900 seconds",
        "Complex Pro + REACT assignments with the required `QUALITY CLOSURE` default to 1800 seconds",
        "--delegate-timeout <seconds>",
        "positive explicit override",
        "timeout_seconds",
    ):
        assert marker in SKILL


def test_quality_regression_cases_keep_parent_visual_ownership():
    cases = json.loads((ROOT / "eval" / "native-quality-tasks.json").read_text(encoding="utf-8"))
    assert {case["task_id"] for case in cases} == {
        "visual-black-hole",
        "responsive-ui",
        "interactive-canvas",
    }
    for case in cases:
        assert case["verification_owner"] == "SHARED"
        assert len(case["acceptance_criteria"]) >= 4
    black_hole = next(case for case in cases if case["task_id"] == "visual-black-hole")
    criteria = " ".join(black_hole["acceptance_criteria"])
    for marker in ("shadow", "lensing", "Doppler", "disk"):
        assert marker.lower() in criteria.lower()


def test_escalated_pro_owns_technical_delivery_and_acceptance():
    for marker in (
        "Escalated Pro ownership",
        "make Pro the technical owner of the",
        "Do not ask Pro merely to suggest changes",
        "mechanical facts it can establish",
        "send it back to Pro",
        "rather than guessed",
        "does not\nsubstitute its own complex reasoning",
    ):
        assert marker in SKILL
