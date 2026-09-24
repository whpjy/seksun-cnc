from __future__ import annotations

from pathlib import Path

from app.agent.config import load_agent_settings
from app.agent.orchestrator import OrchestratorTools, run_manufacturing_orchestrator
from app.agent.workspace import build_agent_workspace
from app.agent.world_model import (
    ManufacturingWorldModel,
    OpenQuestion,
    apply_execution_trace,
    create_manufacturing_world_model,
)


def sample_world() -> ManufacturingWorldModel:
    return create_manufacturing_world_model(
        job_id="world-test",
        analysis={
            "schema_version": "1.0.0",
            "topology": {"faces": 8},
            "measurements": {"volume": 100.0},
            "cylindrical_features": [{
                "id": "HF-1", "kind": "hole", "confidence": 0.92,
                "review_state": "accepted", "review_reasons": [],
            }],
        },
        plan={
            "process_kind": "subtractive",
            "title": "测试工艺",
            "coverage": {"targets": []},
            "setups": [{
                "id": "SETUP-1", "name": "首装", "fixture": "三爪卡盘",
                "work_axis": {"x": 0, "y": 0, "z": 1},
                "operations": [{
                    "id": "OP10", "name": "钻孔", "sequence": 10,
                    "feature_ids": ["HF-1"], "enabled": True,
                }],
            }],
        },
        material="6061-T6",
        machine="测试机床",
        filename="part.step",
        model_url="/model.stl",
    )


def settings(tmp_path: Path, name: str):
    return load_agent_settings({
        "CNC_AGENT_MODE": "shadow",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / f"{name}.sqlite"),
        "CNC_AGENT_MAX_LOCAL_RETRIES": "3",
    })


def test_world_model_preserves_geometry_plan_and_evidence() -> None:
    world = sample_world()

    assert world.geometry_facts["topology"]["faces"] == 8
    assert world.feature_hypotheses[0].confirmation == "geometry_confirmed"
    assert world.planning["L3_rolling_window"]["operation_ids"] == ["OP10"]
    assert world.current_operation_id == "OP10"
    assert world.next_action == "compile"


def test_orchestrator_runs_one_operation_through_real_tool_contracts(tmp_path: Path) -> None:
    events: list[str] = []

    result = run_manufacturing_orchestrator(
        world=sample_world(),
        settings=settings(tmp_path, "happy"),
        tools=OrchestratorTools(
            execute=lambda _world, _context: {
                "status": "completed", "summary": "真实刀路与材料去除仿真完成",
            },
            review=lambda _world, _context: {
                "verdict": "passed", "summary": "AI 审核与硬约束均通过",
            },
        ),
        progress_callback=lambda stage, _message, **_details: events.append(stage),
    )

    world = ManufacturingWorldModel.model_validate(result["world"])
    assert result["status"] == "completed"
    assert world.operations[0].status == "committed"
    assert world.lifecycle == "completed"
    assert [item["action"] for item in result["trace"]] == [
        "decide", "compile", "decide", "execute", "decide", "review",
        "decide", "commit", "decide", "complete",
    ]
    assert "orchestrator_execute" in events
    assert "orchestrator_review" in events


def test_orchestrator_can_perceive_on_demand_before_planning(tmp_path: Path) -> None:
    world = sample_world()
    world.open_questions.append(OpenQuestion(
        id="Q-1",
        question="孔底是否可达？",
        reason="需要确认刀具方向",
        priority="high",
        blocking=True,
    ))

    result = run_manufacturing_orchestrator(
        world=world,
        settings=settings(tmp_path, "perception"),
        tools=OrchestratorTools(
            perceive=lambda _world, _context: {
                "resolved": True,
                "answer": "B-Rep 与剖切视图确认从 +Z 可达",
                "summary": "完成按需几何感知",
            },
            execute=lambda _world, _context: {"status": "completed"},
            review=lambda _world, _context: {"verdict": "passed"},
        ),
    )

    resolved = ManufacturingWorldModel.model_validate(result["world"])
    assert resolved.open_questions[0].status == "resolved"
    assert result["status"] == "completed"
    assert result["trace"][1]["action"] == "perceive"


def test_orchestrator_repairs_failed_review_at_smallest_scope(tmp_path: Path) -> None:
    def review(world: ManufacturingWorldModel, _context: dict) -> dict:
        operation = next(item for item in world.operations if item.id == "OP10")
        if operation.attempts == 1:
            return {"verdict": "repair", "repair_scope": "operation", "summary": "残料超差"}
        return {"verdict": "passed", "summary": "修正后通过"}

    result = run_manufacturing_orchestrator(
        world=sample_world(),
        settings=settings(tmp_path, "repair"),
        tools=OrchestratorTools(
            execute=lambda _world, _context: {"status": "completed"},
            review=review,
            repair=lambda _world, _context: {
                "applied": True,
                "operation": {"parameters": {"radial_stock_to_leave_mm": 0.1}},
                "summary": "仅修正当前工序余量",
            },
        ),
    )

    world = ManufacturingWorldModel.model_validate(result["world"])
    assert result["status"] == "completed"
    assert world.operations[0].attempts == 2
    assert world.operations[0].payload["parameters"]["radial_stock_to_leave_mm"] == 0.1
    assert any(item["action"] == "repair" for item in result["trace"])


def test_execution_trace_updates_material_state_and_next_action() -> None:
    updated = apply_execution_trace(sample_world(), {
        "status": "passed",
        "summary": {
            "verification_status": "passed",
            "collision_status": "passed",
            "remediation_status": "passed",
            "global_defect_count": 0,
        },
        "records": [{
            "operation_id": "OP10",
            "status": "passed",
            "evidence": {
                "toolpath_generated": True,
                "simulation_status": "completed",
                "remaining_volume_mm3": 82.5,
            },
            "defects": [],
            "collisions": [],
        }],
    })

    assert updated.operations[0].status == "committed"
    assert updated.material_states[-1].remaining_volume_mm3 == 82.5
    assert updated.lifecycle == "completed"
    assert updated.next_action == "complete"
    assert updated.stop_conditions["all_operations_committed"] is True


def test_world_model_holds_local_pass_when_global_verification_fails() -> None:
    updated = apply_execution_trace(sample_world(), {
        "status": "blocked",
        "summary": {
            "verification_status": "failed",
            "collision_status": "passed",
            "remediation_status": "passed",
            "global_defect_count": 0,
        },
        "records": [{
            "operation_id": "OP10",
            "status": "passed",
            "evidence": {
                "toolpath_generated": True,
                "simulation_status": "completed",
                "remaining_volume_mm3": 82.5,
            },
        }],
    })

    assert updated.operations[0].status == "verified"
    assert updated.lifecycle == "waiting_human"
    assert updated.next_action == "human_review"
    assert len(updated.material_states) == 1
    assert updated.stop_conditions["all_operations_committed"] is False


def test_workspace_exposes_world_summary_and_artifact(tmp_path: Path) -> None:
    world = sample_world()
    (tmp_path / "agent-world-model.json").write_text(
        world.model_dump_json(indent=2), encoding="utf-8",
    )

    workspace = build_agent_workspace(
        job_id=world.job_id,
        job_status="completed",
        directory=tmp_path,
        events=[],
    )

    assert workspace["orchestration"]["current_operation_id"] == "OP10"
    assert workspace["orchestration"]["next_action"] == "compile"
    assert "agent-world-model" in {item["id"] for item in workspace["artifacts"]}
