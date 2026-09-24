from __future__ import annotations

from typing import Any


STAGE_METADATA: dict[str, tuple[str, str, str]] = {
    "uploading": ("intake", "receive_model", "模型接入"),
    "geometry_analysis": ("feature_recognition", "extract_geometry", "特征识别"),
    "draft_planning": ("process_planning", "build_deterministic_draft", "工艺规划"),
    "ai_planning": ("process_planning", "qwen_process_review", "AI 工艺研判"),
    "agent_review": ("process_planning", "review_ai", "调用 AI 工艺研判"),
    "agent_synthesis": ("process_planning", "synthesize_candidate", "编译候选工艺方案"),
    "agent_validation": ("process_planning", "validate_candidate", "校验候选方案"),
    "agent_decision": ("process_planning", "decide_promotion", "候选方案晋级判断"),
    "agent_fallback": ("process_planning", "fallback_to_baseline", "回退正式基线"),
    "ai_integration": ("process_planning", "integrate_ai_guidance", "融合规划建议"),
    "process_generation": ("process_planning", "compile_process_plan", "生成工艺方案"),
    "coverage_validation": ("validation", "validate_process_coverage", "确定性校验"),
    "operation_execution": ("operation_execution", "verify_operation", "逐工序执行与验证"),
    "validation_remediation": ("validation_remediation", "attribute_defect", "验证与纠错"),
    "completed": ("delivery", "finalize_plan", "规划完成"),
    "error": ("system", "handle_failure", "任务异常"),
}


def _status(stage: str, details: dict[str, Any]) -> str:
    explicit = details.pop("agent_status", None)
    if explicit in {"running", "completed", "failed", "blocked", "waiting"}:
        return str(explicit)
    if stage == "error":
        return "failed"
    if stage == "completed":
        return "completed"
    return "running"


def build_agent_event(
    *,
    sequence: int,
    stage: str,
    message: str,
    percent: float,
    created_at: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(details)
    subgraph, default_node, default_title = STAGE_METADATA.get(
        stage, ("execution", stage, message),
    )
    phase = str(payload.get("phase") or "")
    node_id = str(payload.pop("agent_node", "") or (phase if stage == "ai_planning" and phase else default_node))
    kind = str(payload.pop("agent_kind", "reasoning" if stage == "ai_planning" else "activity"))
    status = _status(stage, payload)
    title = str(payload.pop("agent_title", default_title))
    artifacts = payload.pop("artifacts", [])
    evidence = payload.pop("evidence", [])
    viewer = payload.pop("viewer", None)
    detail = payload.get("detail")

    metrics = {
        key: value
        for key, value in payload.items()
        if key not in {"phase", "detail", "model_url", "warning"}
        and isinstance(value, (str, int, float, bool))
    }
    if viewer is None and payload.get("model_url"):
        viewer = {"kind": "model", "url": payload["model_url"]}

    return {
        "schema_version": "1.0.0",
        "event_id": f"event-{sequence:04d}",
        "sequence": sequence,
        "stage": stage,
        "subgraph": subgraph,
        "node_id": node_id,
        "title": title,
        "kind": kind,
        "status": status,
        "message": message,
        "summary": str(detail or message),
        "percent": max(0, min(round(percent, 1), 100)),
        "created_at": created_at,
        "evidence": evidence if isinstance(evidence, list) else [],
        "metrics": metrics,
        "artifacts": artifacts if isinstance(artifacts, list) else [],
        "viewer": viewer if isinstance(viewer, dict) else None,
        **payload,
    }
