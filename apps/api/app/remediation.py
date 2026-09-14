from __future__ import annotations

from typing import Any

from .models import GeometryAnalysis, Operation, ProcessPlan


def _operation_index(plan: ProcessPlan) -> tuple[dict[str, Operation], dict[str, str]]:
    operations: dict[str, Operation] = {}
    setups: dict[str, str] = {}
    for setup in plan.setups:
        for operation in setup.operations:
            operations[operation.id] = operation
            setups[operation.id] = setup.id
    return operations, setups


def _feature_ids(operation_ids: list[str], operations: dict[str, Operation]) -> list[str]:
    return sorted({
        feature_id
        for operation_id in operation_ids
        if operation_id in operations
        for feature_id in operations[operation_id].feature_ids
    })


def build_remediation_report(
    analysis: GeometryAnalysis,
    plan: ProcessPlan,
    cam_result: dict[str, Any],
    verification: dict[str, Any],
    collision: dict[str, Any],
    *,
    iteration: int = 0,
    max_iterations: int = 3,
) -> dict[str, Any]:
    """Translate CAM failures into traceable manufacturing defects and safe next actions.

    This report is deterministic.  It deliberately distinguishes actions that can be
    applied by a future automatic replan loop from actions that require an engineer.
    """

    operations, setup_by_operation = _operation_index(plan)
    defects: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    action_keys: set[tuple[object, ...]] = set()

    def add_action(
        kind: str,
        label: str,
        reason: str,
        *,
        operation_id: str | None = None,
        operation_type: str | None = None,
        setup_id: str | None = None,
        feature_ids: list[str] | None = None,
        parameters: dict[str, float | int | str | bool] | None = None,
        auto_applicable: bool = False,
    ) -> str:
        key = (kind, operation_id, operation_type, setup_id, tuple(sorted(feature_ids or [])))
        if key in action_keys:
            return next(item["id"] for item in actions if item["_key"] == key)
        action_keys.add(key)
        action_id = f"ACT-{len(actions) + 1:03d}"
        actions.append({
            "id": action_id,
            "_key": key,
            "kind": kind,
            "label": label,
            "reason": reason,
            "setup_id": setup_id,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "feature_ids": feature_ids or [],
            "parameters": parameters or {},
            "auto_applicable": auto_applicable,
        })
        return action_id

    def add_defect(
        kind: str,
        severity: str,
        title: str,
        message: str,
        *,
        source: str,
        operation_ids: list[str] | None = None,
        feature_ids: list[str] | None = None,
        setup_ids: list[str] | None = None,
        evidence: list[str] | None = None,
        metrics: dict[str, float | int | str] | None = None,
        action_ids: list[str] | None = None,
    ) -> None:
        defects.append({
            "id": f"DEF-{len(defects) + 1:03d}",
            "kind": kind,
            "severity": severity,
            "status": "open",
            "source": source,
            "title": title,
            "message": message,
            "setup_ids": sorted(set(setup_ids or [])),
            "operation_ids": sorted(set(operation_ids or [])),
            "feature_ids": sorted(set(feature_ids or [])),
            "evidence": evidence or [],
            "metrics": metrics or {},
            "action_ids": action_ids or [],
        })

    enabled_ids = {operation.id for operation in operations.values() if operation.enabled}
    generated_ids = {str(item) for item in cam_result.get("generated_operations", [])}
    for operation_id in sorted(enabled_ids - generated_ids):
        operation = operations[operation_id]
        action_id = add_action(
            "review_operation",
            f"修复 {operation.name} 的刀路生成",
            "该工序没有产生有效切削轨迹，需要检查几何引用、刀具可达性和 CAM 适配器。",
            operation_id=operation_id,
            operation_type=operation.type,
            setup_id=setup_by_operation.get(operation_id),
            feature_ids=operation.feature_ids,
        )
        add_defect(
            "missing_toolpath", "high", "工序未生成有效刀路",
            f"{operation_id} {operation.name} 已在方案中启用，但 CAM 没有生成有效刀路。",
            source="preflight", operation_ids=[operation_id], feature_ids=operation.feature_ids,
            setup_ids=[setup_by_operation[operation_id]], action_ids=[action_id],
        )

    checks = {str(item.get("id")): item for item in verification.get("checks", []) if isinstance(item, dict)}
    through_check = checks.get("through_hole_depth")
    if through_check and through_check.get("status") == "failed":
        affected = [operation for operation in operations.values() if operation.enabled and operation.type == "drilling"]
        action_ids = []
        for operation in affected:
            required_depth = (
                float(operation.parameters.get("feature_depth_mm", operation.parameters.get("depth_mm", 0)))
                + float(operation.parameters.get("drill_tip_length_mm", 0))
                + float(operation.parameters.get("breakthrough_mm", 0))
            )
            if float(operation.parameters.get("depth_mm", 0)) + 1e-6 >= required_depth:
                continue
            action_ids.append(add_action(
                "modify_operation", f"补足 {operation.id} 钻削深度", "通孔需要包含钻尖长度和穿透余量。",
                operation_id=operation.id, operation_type=operation.type,
                setup_id=setup_by_operation.get(operation.id), feature_ids=operation.feature_ids,
                parameters={"depth_mm": round(required_depth, 3)}, auto_applicable=True,
            ))
        affected_ids = [operation.id for operation in affected]
        add_defect(
            "insufficient_depth", "high", "通孔加工深度不足", str(through_check.get("message", "")),
            source="preflight", operation_ids=affected_ids,
            feature_ids=_feature_ids(affected_ids, operations),
            setup_ids=[setup_by_operation[item] for item in affected_ids], action_ids=action_ids,
        )

    surface_check = checks.get("surface_strategy_coverage")
    if surface_check and surface_check.get("status") == "failed":
        surface_operations: dict[str, set[str]] = {}
        for operation in operations.values():
            for feature_id in operation.feature_ids:
                if feature_id.startswith("SURFACE-SET-"):
                    surface_operations.setdefault(feature_id, set()).add(operation.type)
        action_ids = []
        for feature_id, available in surface_operations.items():
            for operation_type in sorted({"surface_roughing", "surface_3d", "waterline"} - available):
                action_ids.append(add_action(
                    "add_operation", f"增加 {operation_type}", f"{feature_id} 的三维曲面策略不完整。",
                    operation_type=operation_type, feature_ids=[feature_id],
                ))
        add_defect(
            "missing_finishing_strategy", "high", "三维曲面加工策略不完整",
            str(surface_check.get("message", "")), source="preflight",
            feature_ids=sorted(surface_operations), action_ids=action_ids,
        )

    metrics = verification.get("metrics", {}) if isinstance(verification.get("metrics"), dict) else {}
    missing_volume = float(metrics.get("missing_target_volume_mm3", 0) or 0)
    excess_volume = float(metrics.get("excess_stock_volume_mm3", 0) or 0)
    spatial_regions = metrics.get("defect_regions", []) if isinstance(metrics.get("defect_regions"), list) else []
    overcut_regions = [item for item in spatial_regions if isinstance(item, dict) and item.get("kind") == "overcut"]
    excess_regions = [item for item in spatial_regions if isinstance(item, dict) and item.get("kind") == "excess_stock"]

    def attributed_operation_ids(regions: list[dict[str, Any]]) -> list[str]:
        return sorted({
            str(attribution["operation_id"])
            for region in regions
            for attribution in region.get("attribution", [])
            if isinstance(attribution, dict) and attribution.get("operation_id")
        })

    if missing_volume > 0:
        operation_ids = attributed_operation_ids(overcut_regions)
        action_id = add_action(
            "engineer_review", "复核过切区域和加工基准",
            "材料已经被错误移除，自动增加工序无法恢复，需要检查装夹坐标、刀补和策略边界。",
        )
        add_defect(
            "overcut", "critical", "检测到目标材料缺失",
            "累计余料与 STEP 目标比较后发现过切，禁止自动放行。", source="spatial_conformance",
            operation_ids=operation_ids,
            feature_ids=_feature_ids(operation_ids, operations),
            setup_ids=[setup_by_operation[item] for item in operation_ids if item in setup_by_operation],
            evidence=[str(item.get("id")) for item in overcut_regions],
            metrics={"volume_mm3": round(missing_volume, 3), "region_count": len(overcut_regions)},
            action_ids=[action_id],
        )
    if excess_volume > 0:
        uncovered_features = sorted({
            feature_id
            for target in (plan.coverage.targets if plan.coverage else [])
            if target.state != "covered"
            for feature_id in target.source_feature_ids
        })
        action_id = add_action(
            "add_rest_machining", "增加残料加工或清根工序",
            "需要结合残料空间分布选择小直径刀具，并重新进行空间校验。",
            feature_ids=uncovered_features,
        )
        add_defect(
            "excess_stock", "high", "检测到未加工残料",
            "累计余料仍多于 STEP 目标，需要补充残料加工、清根或精加工。", source="spatial_conformance",
            operation_ids=attributed_operation_ids(excess_regions),
            feature_ids=uncovered_features,
            evidence=[str(item.get("id")) for item in excess_regions],
            metrics={"volume_mm3": round(excess_volume, 3), "region_count": len(excess_regions)},
            action_ids=[action_id],
        )

    for collision_item in collision.get("collisions", []):
        if not isinstance(collision_item, dict):
            continue
        operation_id = str(collision_item.get("operation_id", ""))
        kind = str(collision_item.get("kind", "collision"))
        operation = operations.get(operation_id)
        action_kind = "change_tool" if kind.startswith("holder_") else "review_fixture"
        action_label = "更换长颈刀具或减小刀柄包络" if kind.startswith("holder_") else "调整夹具位置或刀路避让"
        action_id = add_action(
            action_kind, action_label, f"{kind} 碰撞必须在重新生成刀路前消除。",
            operation_id=operation_id or None, operation_type=operation.type if operation else None,
            setup_id=setup_by_operation.get(operation_id), feature_ids=operation.feature_ids if operation else [],
        )
        add_defect(
            kind, "critical", "检测到刀具系统碰撞",
            f"{operation_id or '未知工序'} 与 {collision_item.get('target_id', '未知对象')} 发生 {kind}。",
            source="collision", operation_ids=[operation_id] if operation_id else [],
            feature_ids=operation.feature_ids if operation else [],
            setup_ids=[setup_by_operation[operation_id]] if operation_id in setup_by_operation else [],
            evidence=[str(collision_item.get("position", {}))], action_ids=[action_id],
        )

    low_rapids = [item for item in collision.get("low_rapids", []) if isinstance(item, dict)]
    if low_rapids:
        operation_ids = sorted({str(item.get("operation_id", "")) for item in low_rapids if item.get("operation_id")})
        required_clearance = max(
            float(item.get("required_z", 0)) - float(item.get("minimum_z", 0))
            for item in low_rapids
        ) + (plan.safety.clearance_mm if plan.safety else 3) + 1
        action_id = add_action(
            "adjust_safety", "提高安全平面并重新生成快移",
            "横向快移低于当前装夹安全平面。",
            parameters={"clearance_mm": round(required_clearance, 3)}, auto_applicable=True,
        )
        add_defect(
            "low_rapid", "critical", "横向快移低于安全平面",
            f"检测到 {len(low_rapids)} 条危险快移。", source="collision",
            operation_ids=operation_ids, feature_ids=_feature_ids(operation_ids, operations),
            setup_ids=[setup_by_operation[item] for item in operation_ids if item in setup_by_operation],
            evidence=[str(item) for item in low_rapids[:10]], action_ids=[action_id],
        )

    for action in actions:
        action.pop("_key", None)
    action_by_id = {item["id"]: item for item in actions}
    critical_count = sum(item["severity"] == "critical" for item in defects)
    blocking_critical_count = sum(
        item["severity"] == "critical"
        and (
            not item["action_ids"]
            or not all(action_by_id[action_id]["auto_applicable"] for action_id in item["action_ids"])
        )
        for item in defects
    )
    auto_action_count = sum(bool(item["auto_applicable"]) for item in actions)
    status = "blocked" if blocking_critical_count else "action_required" if defects else "clear"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "can_auto_replan": bool(auto_action_count and iteration < max_iterations and not blocking_critical_count),
        "summary": {
            "defect_count": len(defects),
            "critical_count": critical_count,
            "blocking_critical_count": blocking_critical_count,
            "action_count": len(actions),
            "auto_action_count": auto_action_count,
        },
        "defects": defects,
        "actions": actions,
    }


def apply_automatic_remediation(plan: ProcessPlan, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply only actions explicitly marked safe by the deterministic analyzer."""

    if not report.get("can_auto_replan"):
        return []
    operations, _ = _operation_index(plan)
    applied: list[dict[str, Any]] = []
    for action in report.get("actions", []):
        if not isinstance(action, dict) or action.get("auto_applicable") is not True:
            continue
        parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
        if action.get("kind") == "modify_operation":
            operation = operations.get(str(action.get("operation_id", "")))
            if not operation:
                continue
            operation.parameters.update(parameters)
            operation.status = "proposed"
            operation.generation_state = "dirty"
        elif action.get("kind") == "adjust_safety" and plan.safety:
            clearance = parameters.get("clearance_mm")
            if not isinstance(clearance, (int, float)):
                continue
            plan.safety.clearance_mm = max(plan.safety.clearance_mm, float(clearance))
            for operation in operations.values():
                if not operation.enabled:
                    continue
                operation.status = "proposed"
                operation.generation_state = "dirty"
        else:
            continue
        applied.append({
            "action_id": str(action.get("id", "")),
            "kind": str(action.get("kind", "")),
            "label": str(action.get("label", "")),
            "operation_id": action.get("operation_id"),
            "parameters": parameters,
        })
    return applied
