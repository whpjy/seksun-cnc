from pathlib import Path

from app.agent.config import AgentSettings
from app.agent.l32_repair_graph import run_l32_repair_graph


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(checkpoint_path=tmp_path / "repair.sqlite")


def test_missing_op50_with_nonrotational_region_proposes_safe_alternatives(tmp_path: Path) -> None:
    result = run_l32_repair_graph(
        job_id="abc",
        blocker="formal L32 process plan is missing operations: OP50",
        failed_operations=[],
        profile_review_state="accepted",
        coverage_status="incomplete",
        manufacturing_context={
            "nonrotational_turning_limit_z_mm": -1.05,
            "machine_capabilities": ["back_turning", "back_live_tool_milling"],
        },
        settings=_settings(tmp_path),
    )

    candidates = {item["id"]: item for item in result["candidates"]}
    assert result["diagnosis"]["defect"] == "missing_back_face_process"
    assert candidates["restore_op50_back_turning"]["auto_applicable"] is False
    assert candidates["restore_op50_back_turning"]["validation_status"] == "requires_human_confirmation"
    assert candidates["back_live_tool_face_finish"]["capability_available"] is True
    assert result["summary"]["next_action"] == "select_safe_back_face_process"


def test_missing_op50_can_restore_only_without_protected_region(tmp_path: Path) -> None:
    result = run_l32_repair_graph(
        job_id="def",
        blocker="formal L32 process plan is missing operations: OP50",
        failed_operations=[],
        profile_review_state="accepted",
        coverage_status="complete",
        manufacturing_context={
            "nonrotational_turning_limit_z_mm": None,
            "machine_capabilities": ["back_turning"],
        },
        settings=_settings(tmp_path),
        validate_candidate=lambda candidate: {
            "status": "passed" if candidate["id"] == "restore_op50_back_turning" else "failed",
        },
    )

    assert result["summary"]["decision"] == "retry"
    assert result["summary"]["selected_candidate"]["id"] == "restore_op50_back_turning"
    assert result["summary"]["next_action"] == "recompile_and_simulate"


def test_semantic_back_face_gap_uses_same_general_strategy(tmp_path: Path) -> None:
    result = run_l32_repair_graph(
        job_id="semantic-gap",
        blocker="formal L32 process plan requires exactly one back-face finishing operation; found 0",
        failed_operations=[],
        profile_review_state="accepted",
        coverage_status="incomplete",
        manufacturing_context={
            "nonrotational_turning_limit_z_mm": -1.05,
            "machine_capabilities": ["back_live_tool_milling"],
        },
        settings=_settings(tmp_path),
    )

    candidates = {item["id"]: item for item in result["candidates"]}
    candidate = candidates["back_live_tool_face_finish"]
    assert result["diagnosis"]["defect"] == "missing_back_face_process"
    assert candidate["strategy_id"] == "back_live_tool_face_v1"
    assert candidate["intent"]["kind"] == "face_finish"
    assert "continuous_stock" in candidate["required_evidence"]
