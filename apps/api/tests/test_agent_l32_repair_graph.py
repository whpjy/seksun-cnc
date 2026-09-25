from __future__ import annotations

from pathlib import Path

from app.agent.config import load_agent_settings
from app.agent.l32_repair_graph import run_l32_repair_graph


def _settings(tmp_path: Path):
    return load_agent_settings({
        "CNC_AGENT_MODE": "active",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / "repair.sqlite"),
    })


def test_backside_undercut_is_not_blindly_auto_repaired(tmp_path: Path) -> None:
    result = run_l32_repair_graph(
        job_id="undercut-case",
        blocker="turning tool is not reachable: 轮廓存在标准纵向车刀无法从当前方向到达的倒扣",
        failed_operations=["OP55-BACK", "OP58-BACK"],
        profile_review_state="review",
        coverage_status="incomplete",
        settings=_settings(tmp_path),
    )

    assert result["diagnosis"]["defect"] == "profile_undercut"
    assert result["status"] == "waiting"
    assert result["decision"] == "human_review"
    assert all(not item["auto_applicable"] for item in result["candidates"])
    assert all(item["validation_status"] == "rejected_by_safety_gate" for item in result["candidates"])
    assert result["summary"]["production_ready"] is False


def test_safe_tool_hand_candidate_can_enter_recompile(tmp_path: Path) -> None:
    seen: list[str] = []

    def validate(candidate: dict[str, object]) -> dict[str, object]:
        seen.append(str(candidate["id"]))
        return {"status": "passed", "whole_program_status": "passed"}

    result = run_l32_repair_graph(
        job_id="hand-case",
        blocker="轴向进给方向与外圆车刀左右手不匹配",
        failed_operations=["OP20"],
        profile_review_state="accepted",
        coverage_status="complete",
        settings=_settings(tmp_path),
        validate_candidate=validate,
    )

    assert seen == ["replace_turning_tool_hand"]
    assert result["status"] == "repair_ready"
    assert result["decision"] == "retry"
    assert result["summary"]["next_action"] == "recompile_and_simulate"
