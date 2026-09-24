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
    })


def guidance(*_args, **_kwargs):
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
            "operation_recommendations": [{"action": "keep"}],
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
    assert result["evaluation"]["recommendation_summary"]["by_action"]["keep"] == 1


def test_active_graph_promotes_only_eligible_candidate(tmp_path: Path) -> None:
    result = invoke_graph(tmp_path, "active")

    assert result["status"] == "eligible"
    assert result["evaluation"]["production_result_changed"] is True


def test_graph_falls_back_when_ai_review_fails(tmp_path: Path) -> None:
    def failing_reviewer(*_args, **_kwargs):
        raise QwenPlanningError("provider unavailable")

    result = invoke_graph(tmp_path, "shadow", reviewer=failing_reviewer)

    assert result["status"] == "fallback"
    assert result["evaluation"]["eligible_for_promotion"] is False
    assert "provider unavailable" in result["evaluation"]["blocking_reasons"][0]
