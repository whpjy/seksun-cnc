from __future__ import annotations

from pathlib import Path

from app.agent.config import load_agent_settings
from app.agent.execution_graph import run_operation_execution_subgraph


def run_execution(
    tmp_path: Path,
    *,
    job_id: str,
    operations: list[dict],
    generated_operations: list[str],
    collisions: list[dict] | None = None,
    defects: list[dict] | None = None,
    actions: list[dict] | None = None,
    can_auto_replan: bool = False,
    events: list[tuple[str, str, dict]] | None = None,
):
    settings = load_agent_settings({
        "CNC_AGENT_MODE": "shadow",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / f"{job_id}.sqlite"),
        "CNC_AGENT_MAX_LOCAL_RETRIES": "3",
    })
    preview_segments = [
        {
            "operation_id": operation_id,
            "motion": motion,
            "start": [0, 0, 0],
            "end": [1, 0, 0],
        }
        for operation_id in generated_operations
        for motion in ("rapid", "cut")
    ]

    def report(stage: str, message: str, **details) -> None:
        if events is not None:
            events.append((stage, message, details))

    return run_operation_execution_subgraph(
        job_id=job_id,
        operations=operations,
        cam_result={
            "generated_operations": generated_operations,
            "preview_segments": preview_segments,
        },
        simulation={
            "status": "completed",
            "metrics": {"removed_volume_mm3": 18.5, "remaining_volume_mm3": 81.5},
            "operation_snapshots": [
                {
                    "operation_id": operation_id,
                    "status": "completed",
                    "sequence": index,
                    "cut_segment_count": 1,
                    "removed_volume_mm3": 10.0 * index,
                    "removed_volume_delta_mm3": 10.0,
                    "remaining_volume_mm3": 100.0 - 10.0 * index,
                }
                for index, operation_id in enumerate(generated_operations, 1)
            ],
        },
        verification={"status": "passed"},
        collision={"status": "failed" if collisions else "passed", "collisions": collisions or []},
        remediation={
            "status": "action_required" if defects else "passed",
            "defects": defects or [],
            "actions": actions or [],
            "can_auto_replan": can_auto_replan,
            "iteration": 1,
            "max_iterations": 3,
        },
        settings=settings,
        progress_callback=report,
    )


def sample_operations() -> list[dict]:
    return [
        {"id": "OP10", "name": "粗铣平面", "setup_id": "SETUP-1", "feature_ids": ["PF-1"]},
        {"id": "OP20", "name": "钻孔", "setup_id": "SETUP-1", "feature_ids": ["HF-1"]},
    ]


def test_execution_graph_verifies_each_operation_from_real_evidence(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict]] = []
    result = run_execution(
        tmp_path,
        job_id="job-passed",
        operations=sample_operations(),
        generated_operations=["OP10", "OP20"],
        events=events,
    )

    assert result["status"] == "passed"
    assert result["summary"]["next_action"] == "release"
    assert result["summary"]["counts"] == {
        "passed": 2, "action_required": 0, "blocked": 0,
    }
    assert result["records"][0]["evidence"]["segment_count"] == 2
    assert result["records"][0]["evidence"]["cut_segment_count"] == 1
    assert result["records"][0]["evidence"]["removed_volume_delta_mm3"] == 10.0
    assert result["records"][0]["evidence"]["simulation_scope"] == "cumulative_after_operation"
    assert result["records"][0]["tool_calls"][1] == {
        "tool": "simulation.remove_material", "status": "completed",
    }
    assert result["records"][1]["viewer"]["operation_id"] == "OP20"
    assert [stage for stage, _, _ in events].count("verify_operation") == 2
    assert events[-1][0] == "summarize_execution"


def test_execution_graph_blocks_missing_toolpath_or_collision(tmp_path: Path) -> None:
    result = run_execution(
        tmp_path,
        job_id="job-blocked",
        operations=sample_operations(),
        generated_operations=["OP10"],
        collisions=[{
            "operation_id": "OP10",
            "kind": "holder_stock",
            "severity": "critical",
        }],
    )

    assert result["status"] == "blocked"
    assert result["summary"]["next_action"] == "manual_review"
    assert result["summary"]["counts"]["blocked"] == 1
    assert result["summary"]["skipped_operation_ids"] == ["OP20"]
    assert result["records"][0]["collisions"][0]["kind"] == "holder_stock"


def test_execution_graph_stops_when_operation_has_no_real_simulation_state(tmp_path: Path) -> None:
    result = run_execution(
        tmp_path,
        job_id="job-missing-toolpath",
        operations=sample_operations(),
        generated_operations=[],
    )

    assert result["status"] == "blocked"
    assert result["records"][0]["evidence"]["toolpath_generated"] is False
    assert result["records"][0]["evidence"]["simulation_status"] == "unavailable"
    assert result["summary"]["skipped_operation_ids"] == ["OP20"]


def test_execution_graph_routes_recoverable_defect_to_local_remediation(tmp_path: Path) -> None:
    defect = {
        "id": "DEF-1",
        "severity": "warning",
        "operation_ids": ["OP20"],
        "message": "孔壁余量超出阈值",
    }
    action = {
        "operation_id": "OP20",
        "action": "adjust_allowance",
        "parameter": "radial_stock_to_leave_mm",
    }
    result = run_execution(
        tmp_path,
        job_id="job-remediation",
        operations=sample_operations(),
        generated_operations=["OP10", "OP20"],
        defects=[defect],
        actions=[action],
        can_auto_replan=True,
    )

    assert result["status"] == "action_required"
    assert result["summary"]["next_action"] == "local_remediation"
    assert result["summary"]["counts"]["action_required"] == 1
    assert result["records"][1]["recommended_actions"] == [action]


def test_execution_graph_does_not_release_unassigned_global_defect(tmp_path: Path) -> None:
    result = run_execution(
        tmp_path,
        job_id="job-global-defect",
        operations=sample_operations(),
        generated_operations=["OP10", "OP20"],
        defects=[{
            "id": "DEF-GLOBAL",
            "severity": "high",
            "operation_ids": [],
            "message": "整体曲面精加工策略不完整",
        }],
    )

    assert result["summary"]["counts"]["passed"] == 2
    assert result["status"] == "action_required"
    assert result["summary"]["next_action"] == "engineering_review"
    assert result["summary"]["global_defect_ids"] == ["DEF-GLOBAL"]
