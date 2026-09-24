from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ARTIFACT_CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("agent-world-model.json", "制造世界模型", "json", "orchestrator"),
    ("agent-orchestrator.json", "总协调器执行记录", "json", "orchestrator"),
    ("model.stl", "三维模型", "model", "geometry"),
    ("analysis.json", "几何分析", "json", "feature_recognition"),
    ("rotational-features.json", "回转制造特征", "json", "feature_recognition"),
    ("planning-guidance.json", "AI 规划建议", "json", "process_planning"),
    ("agent-plan.json", "智能体候选方案", "json", "process_planning"),
    ("agent-evaluation.json", "候选方案评测", "json", "validation"),
    ("operation-audit.json", "逐工序能力校验", "json", "validation"),
    ("agent-execution.json", "逐工序执行记录", "json", "operation_execution"),
    ("agent-remediation.json", "验证纠错记录", "json", "validation_remediation"),
    ("plan.json", "工艺方案", "json", "process_planning"),
    ("ai-plan.json", "AI 工艺审查", "json", "process_planning"),
    ("toolpath.json", "刀路数据", "json", "operation_execution"),
    ("simulation.json", "材料仿真", "json", "simulation"),
    ("verification.json", "成品验证", "json", "validation"),
    ("collision.json", "碰撞检查", "json", "validation"),
    ("remediation.json", "纠错报告", "json", "remediation"),
)


def artifact_manifest(job_id: str, directory: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for filename, label, kind, group in ARTIFACT_CATALOG:
        path = directory / filename
        if not path.is_file():
            continue
        artifacts.append({
            "id": filename.removesuffix(".json").removesuffix(".stl"),
            "label": label,
            "filename": filename,
            "kind": kind,
            "group": group,
            "url": f"/api/v1/jobs/{job_id}/files/{filename}",
            "size_bytes": path.stat().st_size,
            "viewer": {"kind": "model", "url": f"/api/v1/jobs/{job_id}/files/{filename}"}
            if kind == "model" else None,
        })
    return artifacts


def build_agent_workspace(
    *,
    job_id: str,
    job_status: str,
    directory: Path,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    world_path = directory / "agent-world-model.json"
    orchestration: dict[str, Any] | None = None
    if world_path.is_file():
        world = json.loads(world_path.read_text(encoding="utf-8"))
        questions = [
            item for item in world.get("open_questions", [])
            if isinstance(item, dict) and item.get("status") == "open"
        ]
        orchestration = {
            "revision": world.get("revision"),
            "lifecycle": world.get("lifecycle"),
            "current_objective": world.get("current_objective"),
            "next_action": world.get("next_action"),
            "current_operation_id": world.get("current_operation_id"),
            "open_question_count": len(questions),
            "blocking_question_count": sum(bool(item.get("blocking")) for item in questions),
            "evidence_count": len(world.get("evidence", [])),
        }
    active = (
        events[-1] if events and job_status in {"completed", "failed"}
        else next(
            (event for event in reversed(events) if event.get("status") == "running"),
            events[-1] if events else None,
        )
    )
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "thread_id": job_id,
        "status": job_status,
        "active_event_id": active.get("event_id") if isinstance(active, dict) else None,
        "orchestration": orchestration,
        "events": events,
        "artifacts": artifact_manifest(job_id, directory),
    }
