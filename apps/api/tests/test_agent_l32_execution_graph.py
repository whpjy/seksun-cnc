from __future__ import annotations

from pathlib import Path

from app.agent.config import load_agent_settings
from app.agent.l32_execution_graph import run_l32_operation_execution_graph


def _run(tmp_path: Path, *, removed: list[float] | None = None, review_callback=None):
    removed = removed or [12.0, 5.0]
    stages = [
        {"operation_id": "OP10", "operation_name": "端面", "channel_id": "main", "phase": "front_turning", "command_count": 2, "verification_status": "passed"},
        {"operation_id": "OP20", "operation_name": "外圆粗车", "channel_id": "main", "phase": "front_turning", "command_count": 2, "verification_status": "passed"},
    ]
    commands = []
    for index, operation_id in enumerate(("OP10", "OP20"), 1):
        commands.extend([
            {"sequence": index * 2 - 1, "type": "select_tool", "operation_id": operation_id},
            {"sequence": index * 2, "type": "feed_move", "operation_id": operation_id},
        ])
    snapshots = []
    volume = 100.0
    for operation_id, delta in zip(("OP10", "OP20"), removed):
        snapshots.append({
            "operation_id": operation_id, "source_frame": "main",
            "before_samples": [{"z": 0}, {"z": 1}],
            "after_samples": [{"z": 0}, {"z": 1}],
            "metrics": {
                "initial_volume_mm3": volume,
                "remaining_volume_mm3": volume - delta,
                "removed_volume_mm3": delta,
                "removal_percent": delta / volume * 100,
            },
        })
        volume -= delta
    settings = load_agent_settings({
        "CNC_AGENT_MODE": "shadow",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / "l32.sqlite"),
    })
    return run_l32_operation_execution_graph(
        job_id="l32-test", stages=stages,
        operations={item: {"id": item, "name": item} for item in ("OP10", "OP20")},
        toolpath={"channels": [{"id": "main", "commands": commands}]},
        continuous_simulation={"status": "passed", "stage_snapshots": snapshots, "checks": []},
        settings=settings, review_callback=review_callback,
    )


def test_l32_execution_reviews_and_preserves_each_material_state(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result["status"] == "passed"
    assert [item["operation_id"] for item in result["records"]] == ["OP10", "OP20"]
    assert result["records"][0]["evidence"]["removed_volume_delta_mm3"] == 12.0
    assert result["summary"]["production_ready"] is False
    assert result["summary"]["machine_collision_status"] == "not_verified"
    assert result["summary"]["next_action"] == "machine_level_validation"


def test_l32_execution_stops_on_zero_measured_removal(tmp_path: Path) -> None:
    result = _run(tmp_path, removed=[0.0, 5.0])

    assert result["status"] == "action_required"
    assert result["records"][0]["blocking_reasons"] == ["zero_measured_removal"]
    assert result["summary"]["skipped_operation_ids"] == ["OP20"]


def test_l32_execution_stops_when_ai_requests_repair(tmp_path: Path) -> None:
    reviewed = []

    def review(record):
        reviewed.append(record["operation_id"])
        return {"status": "completed", "review": {
            "verdict": "repair", "confidence": 0.9, "missing_evidence": [],
        }}

    result = _run(tmp_path, review_callback=review)

    assert reviewed == ["OP10"]
    assert result["status"] == "action_required"
    assert result["summary"]["skipped_operation_ids"] == ["OP20"]
