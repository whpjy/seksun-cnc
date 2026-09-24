from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value or {})


class EvidenceReference(BaseModel):
    id: str
    kind: Literal[
        "geometry", "model", "view", "toolpath", "simulation", "collision",
        "conformance", "rule", "human", "ai_review",
    ]
    source: str
    summary: str
    operation_id: str | None = None
    created_at: str = Field(default_factory=utc_now)


class FeatureHypothesis(BaseModel):
    id: str
    kind: str
    confidence: float = Field(default=0.5, ge=0, le=1)
    confirmation: Literal["hypothesis", "observed", "geometry_confirmed", "process_confirmed"] = "hypothesis"
    evidence_ids: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    id: str
    question: str
    reason: str
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    blocking: bool = False
    status: Literal["open", "resolved", "deferred"] = "open"
    answer: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class WorldOperation(BaseModel):
    id: str
    setup_id: str
    name: str
    sequence: int
    feature_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "proposed", "compiled", "executed", "verified", "committed",
        "action_required", "blocked",
    ] = "proposed"
    attempts: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    review: dict[str, Any] | None = None


class MaterialState(BaseModel):
    sequence: int = Field(ge=0)
    kind: Literal["initial_stock", "after_operation", "target"]
    operation_id: str | None = None
    status: Literal["current", "historical", "candidate"] = "current"
    remaining_volume_mm3: float | None = None
    artifact: str | None = None
    created_at: str = Field(default_factory=utc_now)


class ManufacturingWorldModel(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    revision: int = 1
    updated_at: str = Field(default_factory=utc_now)
    lifecycle: Literal[
        "initialized", "planning", "awaiting_execution", "validating",
        "repairing", "waiting_human", "completed", "blocked",
    ] = "initialized"
    current_objective: str
    next_action: Literal[
        "perceive", "plan", "compile", "execute", "review", "commit",
        "repair", "human_review", "complete",
    ] = "plan"
    current_operation_id: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    geometry_facts: dict[str, Any] = Field(default_factory=dict)
    feature_hypotheses: list[FeatureHypothesis] = Field(default_factory=list)
    planning: dict[str, Any] = Field(default_factory=dict)
    operations: list[WorldOperation] = Field(default_factory=list)
    material_states: list[MaterialState] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    stop_conditions: dict[str, bool] = Field(default_factory=dict)


def _feature_hypotheses(analysis: dict[str, Any]) -> list[FeatureHypothesis]:
    result: list[FeatureHypothesis] = []
    collections = (
        "cylindrical_features", "prismatic_features", "planar_machining_features",
        "internal_profile_features",
    )
    for collection in collections:
        for feature in analysis.get(collection, []) or []:
            if not isinstance(feature, dict) or not feature.get("id"):
                continue
            review_state = str(feature.get("review_state", "review"))
            result.append(FeatureHypothesis(
                id=str(feature["id"]),
                kind=str(feature.get("kind", collection.removesuffix("_features"))),
                confidence=float(feature.get("confidence", 0.5) or 0.5),
                confirmation=(
                    "geometry_confirmed" if review_state == "accepted" else "hypothesis"
                ),
                evidence_ids=["geometry-analysis"],
                review_reasons=[str(item) for item in feature.get("review_reasons", [])],
            ))
    return result


def create_manufacturing_world_model(
    *,
    job_id: str,
    analysis: Any,
    plan: Any,
    material: str,
    machine: str,
    filename: str,
    model_url: str | None,
    require_initial_perception: bool = False,
) -> ManufacturingWorldModel:
    analysis_data = _payload(analysis)
    plan_data = _payload(plan)
    operations: list[WorldOperation] = []
    work_packages: list[dict[str, Any]] = []
    for setup in plan_data.get("setups", []) or []:
        setup_id = str(setup.get("id", ""))
        operation_ids: list[str] = []
        for operation in setup.get("operations", []) or []:
            if not operation.get("enabled", True):
                continue
            operation_id = str(operation.get("id", ""))
            operation_ids.append(operation_id)
            operations.append(WorldOperation(
                id=operation_id,
                setup_id=setup_id,
                name=str(operation.get("name", operation_id)),
                sequence=int(operation.get("sequence", len(operations) + 1)),
                feature_ids=[str(item) for item in operation.get("feature_ids", [])],
                payload=dict(operation),
            ))
        work_packages.append({
            "setup_id": setup_id,
            "name": setup.get("name"),
            "fixture": setup.get("fixture"),
            "work_axis": setup.get("work_axis"),
            "operation_ids": operation_ids,
            "status": "candidate",
        })
    operations.sort(key=lambda item: item.sequence)
    current = operations[0] if operations else None
    unresolved = []
    coverage = plan_data.get("coverage") or {}
    for target in coverage.get("targets", []) or []:
        if target.get("state") in {"uncovered", "unresolved", "review"}:
            unresolved.append(OpenQuestion(
                id=f"coverage:{target.get('id')}",
                question=f"如何可靠加工并验证 {target.get('label') or target.get('id')}？",
                reason="工艺覆盖尚未被确定性验证",
                priority="high" if target.get("state") == "uncovered" else "medium",
                blocking=target.get("state") in {"uncovered", "unresolved"},
            ))
    if require_initial_perception and current and model_url:
        unresolved.insert(0, OpenQuestion(
            id="initial-model-understanding",
            question=f"几何和装夹证据是否足以支持首道工序 {current.id} {current.name}？",
            reason="首道工序前需要建立有图像证据的制造认知基线",
            priority="high",
            blocking=True,
        ))
    return ManufacturingWorldModel(
        job_id=job_id,
        lifecycle="awaiting_execution" if operations else "waiting_human",
        current_objective=(
            f"编译、执行并验证 {current.id} {current.name}" if current
            else "补充信息并形成首个可执行工序"
        ),
        next_action="perceive" if any(item.blocking for item in unresolved) else "compile" if current else "human_review",
        current_operation_id=current.id if current else None,
        source={"filename": filename, "model_url": model_url, "analysis_schema": analysis_data.get("schema_version")},
        resources={"material": material, "machine": machine},
        geometry_facts={
            "topology": analysis_data.get("topology", {}),
            "measurements": analysis_data.get("measurements", {}),
            "solid_candidates": analysis_data.get("solid_candidates", []),
        },
        feature_hypotheses=_feature_hypotheses(analysis_data),
        planning={
            "L1_strategy": {
                "status": "candidate",
                "process_kind": plan_data.get("process_kind"),
                "title": plan_data.get("title"),
                "route": plan_data.get("manufacturing_route"),
            },
            "L2_work_packages": work_packages,
            "L3_rolling_window": {
                "status": "candidate",
                "operation_ids": [item.id for item in operations[:3]],
                "window_size": min(3, len(operations)),
            },
        },
        operations=operations,
        material_states=[MaterialState(
            sequence=0,
            kind="initial_stock",
            artifact="plan.json#stock",
        )],
        open_questions=unresolved,
        evidence=[
            EvidenceReference(id="geometry-analysis", kind="geometry", source="analysis.json", summary="确定性 STEP/B-Rep 几何分析"),
            EvidenceReference(id="source-model", kind="model", source=model_url or filename, summary="原始三维模型"),
        ],
        decisions=[{
            "at": utc_now(),
            "kind": "initialize_world",
            "summary": "已建立制造世界模型，并选择首个滚动工序窗口",
            "operation_id": current.id if current else None,
        }],
        stop_conditions={
            "all_operations_committed": False,
            "real_toolpaths_available": False,
            "simulation_passed": False,
            "no_unresolved_high_risk": not any(item.blocking for item in unresolved),
            "human_decisions_resolved": True,
        },
    )


def load_world_model(path: Path) -> ManufacturingWorldModel | None:
    if not path.is_file():
        return None
    return ManufacturingWorldModel.model_validate_json(path.read_text(encoding="utf-8"))


def apply_execution_trace(
    world: ManufacturingWorldModel,
    execution: dict[str, Any],
) -> ManufacturingWorldModel:
    updated = world.model_copy(deep=True)
    summary = dict(execution.get("summary") or {})
    global_gate_passed = (
        str(execution.get("status")) == "passed"
        and str(summary.get("verification_status", "unknown")) == "passed"
        and str(summary.get("collision_status", "unknown")) == "passed"
        and str(summary.get("remediation_status", "unknown")) in {"passed", "clear"}
        and int(summary.get("global_defect_count", 0) or 0) == 0
    )
    records = {
        str(item.get("operation_id")): item
        for item in execution.get("records", [])
        if isinstance(item, dict) and item.get("operation_id")
    }
    for operation in updated.operations:
        record = records.get(operation.id)
        if not record:
            continue
        evidence = dict(record.get("evidence") or {})
        evidence_id = f"execution:{operation.id}:attempt:{operation.attempts + 1}"
        updated.evidence.append(EvidenceReference(
            id=evidence_id,
            kind="simulation",
            source="agent-execution.json",
            summary=f"{operation.id} 刀路、材料去除与安全校验结果：{record.get('status')}",
            operation_id=operation.id,
        ))
        operation.evidence_ids.append(evidence_id)
        operation.attempts += 1
        operation.review = {
            "verdict": record.get("status"),
            "evidence": evidence,
            "defects": record.get("defects", []),
            "collisions": record.get("collisions", []),
            "ai_review": record.get("ai_review"),
        }
        if record.get("status") == "passed":
            operation.status = "committed" if global_gate_passed else "verified"
        if operation.status == "committed":
            for state in updated.material_states:
                state.status = "historical"
            updated.material_states.append(MaterialState(
                sequence=len(updated.material_states),
                kind="after_operation",
                operation_id=operation.id,
                remaining_volume_mm3=evidence.get("remaining_volume_mm3"),
                artifact=f"simulation.json#operation={operation.id}",
            ))
        elif record.get("status") == "action_required":
            operation.status = "action_required"
        elif record.get("status") != "passed":
            operation.status = "blocked"
        updated.decisions.append({
            "at": utc_now(),
            "kind": "operation_gate",
            "operation_id": operation.id,
            "decision": operation.status,
            "evidence_ids": [evidence_id],
        })

    remaining = next((item for item in updated.operations if item.status != "committed"), None)
    summary_status = str(execution.get("status", "blocked"))
    if summary_status == "passed" and not global_gate_passed:
        summary_status = "blocked"
    if remaining is None and updated.operations:
        updated.lifecycle = "completed"
        updated.next_action = "complete"
        updated.current_operation_id = None
        updated.current_objective = "所有已规划工序均通过真实执行证据校验"
    elif summary_status == "action_required":
        updated.lifecycle = "repairing"
        updated.next_action = "repair"
        updated.current_operation_id = remaining.id if remaining else None
        updated.current_objective = f"修正并重新验证 {remaining.id if remaining else '当前工序'}"
    elif summary_status == "blocked":
        updated.lifecycle = "waiting_human"
        updated.next_action = "human_review"
        updated.current_operation_id = remaining.id if remaining else None
        updated.current_objective = f"处理 {remaining.id if remaining else '当前工序'} 的阻断风险"
    else:
        updated.lifecycle = "awaiting_execution"
        updated.next_action = "compile"
        updated.current_operation_id = remaining.id if remaining else None
        updated.current_objective = f"继续验证 {remaining.id if remaining else '下一工序'}"
    updated.stop_conditions.update({
        "all_operations_committed": bool(updated.operations) and remaining is None,
        "real_toolpaths_available": bool(updated.operations) and all(
            item.review and item.review.get("evidence", {}).get("toolpath_generated")
            for item in updated.operations
        ),
        "simulation_passed": bool(updated.operations) and all(
            item.review and item.review.get("evidence", {}).get("simulation_status") == "completed"
            for item in updated.operations
        ),
    })
    updated.revision += 1
    updated.updated_at = utc_now()
    return updated
