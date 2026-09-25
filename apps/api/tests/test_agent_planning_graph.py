from __future__ import annotations

from pathlib import Path

from app.agent.checkpoint import open_sqlite_checkpointer
from app.agent.config import load_agent_settings
from app.agent.planning_graph import build_process_planning_subgraph
from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.qwen import QwenPlanningError


def sample_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.8.0",
        "source_file": "agent-fixture.step",
        "topology": {"solids": 1, "faces": 8, "edges": 18},
        "measurements": {
            "surface_area": 12000.0,
            "volume": 50000.0,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 100, "y": 60, "z": 20},
                "size": {"x": 100, "y": 60, "z": 20},
            },
        },
        "planar_features": [{
            "id": "PF-1", "area": 6000,
            "center": {"x": 50, "y": 30, "z": 20},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [{
            "id": "CF-1", "kind": "hole", "radius": 5, "diameter": 10,
            "length": 20, "center": {"x": 20, "y": 20, "z": 10},
            "axis": {"x": 0, "y": 0, "z": 1},
        }],
        "solid_candidates": [{
            "index": 1,
            "volume": 50000,
            "surface_area": 12000,
            "center": {"x": 50, "y": 30, "z": 10},
            "bounds": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 100, "y": 60, "z": 20},
                "size": {"x": 100, "y": 60, "z": 20},
            },
            "selected": True,
        }],
    })


def guidance(_analysis, plan, **_kwargs):
    operation_recommendations = [
        {
            "action": "keep",
            "setup_id": setup.id,
            "operation_id": operation.id,
            "operation_type": operation.type,
            "feature_ids": operation.feature_ids,
            "priority": operation.sequence,
            "reason": "AI 已确认该工序属于当前制造路线",
        }
        for setup in plan.setups
        for operation in setup.operations
    ]
    return {
        "schema_version": "1.0.0",
        "provider": "test",
        "model": "fake-qwen",
        "created_at": "2026-09-24T00:00:00Z",
        "review": {
            "manufacturing_intent": "验证智能体候选方案",
            "recommended_process_kind": "subtractive",
            "part_family": "test-part",
            "confidence": 0.92,
            "summary": "保持确定性基线并完成验证",
            "requires_engineer_review": False,
            "risks": [],
            "operation_recommendations": operation_recommendations,
        },
    }


def invoke_graph(tmp_path: Path, mode: str, reviewer=guidance):
    analysis = sample_analysis()
    baseline = build_process_plan(analysis, "6061-T6", "VMC-850")
    settings = load_agent_settings({
        "CNC_AGENT_MODE": mode,
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / f"{mode}.sqlite"),
    })
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_process_planning_subgraph(settings, checkpointer, reviewer=reviewer)
        return graph.invoke({
            "job_id": f"job-{mode}",
            "mode": mode,
            "material": "6061-T6",
            "machine": "VMC-850",
            "analysis": analysis.model_dump(mode="json"),
            "baseline_plan": baseline.model_dump(mode="json"),
            "status": "reviewing",
        }, {"configurable": {"thread_id": f"job-{mode}:process-planning"}})


def test_shadow_graph_builds_candidate_without_replacing_production(tmp_path: Path) -> None:
    result = invoke_graph(tmp_path, "shadow")

    assert result["status"] == "eligible"
    assert result["candidate_plan"]["ai_planning"]["agent_mode"] == "shadow"
    assert result["evaluation"]["eligible_for_promotion"] is True
    assert result["evaluation"]["production_result_changed"] is False
    assert result["evaluation"]["primary_plan_selected"] is False
    assert result["evaluation"]["recommendation_summary"]["by_action"]["keep"] > 1
    assert all(
        operation["source"] == "recommendation"
        for setup in result["candidate_plan"]["setups"]
        for operation in setup["operations"]
    )


def test_active_graph_promotes_only_eligible_candidate(tmp_path: Path) -> None:
    result = invoke_graph(tmp_path, "active")

    assert result["status"] == "eligible"
    assert result["evaluation"]["production_result_changed"] is True
    assert result["evaluation"]["eligible_for_promotion"] is True
    assert result["evaluation"]["primary_plan_selected"] is True
    audit = result["evaluation"]["operation_audit"]
    assert audit["operation_count"] > 0
    assert audit["counts"]["blocked"] == 0
    assert {item["operation_id"] for item in audit["records"]} == {
        operation["id"]
        for setup in result["candidate_plan"]["setups"]
        for operation in setup["operations"]
    }


def test_operation_audit_blocks_planned_cam_capability(tmp_path: Path) -> None:
    def planned_capability_guidance(analysis, plan, **kwargs):
        payload = guidance(analysis, plan, **kwargs)
        payload["review"]["operation_recommendations"].append({
            "action": "add",
            "setup_id": plan.setups[0].id,
            "operation_id": "AI-UNSUPPORTED",
            "operation_type": "adaptive_clearing",
            "feature_ids": [analysis.planar_features[0].id],
            "priority": 999,
            "reason": "测试尚未落地的 CAM 能力必须被阻断",
            "tool_id": "EM-6",
            "parameters": {"depth_mm": 1.0},
        })
        return payload

    result = invoke_graph(tmp_path, "active", reviewer=planned_capability_guidance)

    assert result["status"] == "blocked"
    assert result["evaluation"]["production_result_changed"] is False
    assert result["evaluation"]["primary_plan_selected"] is False
    assert result["evaluation"]["operation_audit"]["status"] == "blocked"
    assert "AI-UNSUPPORTED" in result["evaluation"]["operation_audit"]["blocked_operation_ids"]


def test_operation_audit_feedback_replans_and_removes_blocked_operation(tmp_path: Path) -> None:
    calls = 0

    def repairing_guidance(analysis, plan, **kwargs):
        nonlocal calls
        calls += 1
        payload = guidance(analysis, plan, **kwargs)
        if calls == 1:
            payload["review"]["operation_recommendations"].append({
                "action": "add",
                "setup_id": plan.setups[0].id,
                "operation_id": "AI-REPAIR-ME",
                "operation_type": "adaptive_clearing",
                "feature_ids": [analysis.planar_features[0].id],
                "priority": 999,
                "reason": "首次规划故意选择未落地能力",
                "tool_id": "EM-6",
                "parameters": {"depth_mm": 1.0},
            })
        else:
            recommendation = next(
                item for item in payload["review"]["operation_recommendations"]
                if item["operation_id"] == "AI-REPAIR-ME"
            )
            recommendation["action"] = "remove"
            recommendation["reason"] = "逐工序能力校验失败，删除不可执行工序"
        return payload

    result = invoke_graph(tmp_path, "active", reviewer=repairing_guidance)

    assert calls == 2
    assert result["status"] == "eligible"
    assert result["operation_audit"]["replan_count"] == 1
    assert len(result["operation_audit"]["history"]) == 2
    assert result["operation_audit"]["history"][0]["status"] == "blocked"
    assert result["operation_audit"]["status"] != "blocked"
    assert all(
        operation["id"] != "AI-REPAIR-ME"
        for setup in result["candidate_plan"]["setups"]
        for operation in setup["operations"]
    )


def test_agent_compiler_applies_remove_and_add_decisions(tmp_path: Path) -> None:
    def changing_guidance(analysis, plan, **kwargs):
        payload = guidance(analysis, plan, **kwargs)
        recommendations = payload["review"]["operation_recommendations"]
        removed = recommendations[0]
        removed["action"] = "remove"
        recommendations.append({
            "action": "add",
            "setup_id": "SETUP-AI-MILL",
            "operation_id": "AI-OP99",
            "operation_type": "edge_chamfer",
            "feature_ids": [analysis.planar_features[0].id],
            "priority": 99,
            "reason": "AI 根据锐边风险补充去毛刺",
            "tool_id": "CM-6-90",
            "parameters": {"chamfer_width_mm": 0.2},
        })
        return payload

    result = invoke_graph(tmp_path, "active", reviewer=changing_guidance)
    operation_ids = [
        operation["id"]
        for setup in result["candidate_plan"]["setups"]
        for operation in setup["operations"]
    ]

    assert "AI-OP99" in operation_ids
    assert any(
        setup["id"] == "SETUP-AI-MILL" and setup["machine_id"] == "vmc-850"
        for setup in result["candidate_plan"]["setups"]
    )
    assert result["candidate_plan"]["ai_planning"]["planner"] == "langgraph_ai_primary"
    assert result["evaluation"]["eligible_for_promotion"] is False
    assert result["evaluation"]["primary_plan_selected"] is False


def test_graph_falls_back_when_ai_review_fails(tmp_path: Path) -> None:
    def failing_reviewer(*_args, **_kwargs):
        raise QwenPlanningError("provider unavailable")

    result = invoke_graph(tmp_path, "shadow", reviewer=failing_reviewer)

    assert result["status"] == "fallback"
    assert result["evaluation"]["eligible_for_promotion"] is False
    assert result["evaluation"]["primary_plan_selected"] is False
    assert "provider unavailable" in result["evaluation"]["blocking_reasons"][0]
