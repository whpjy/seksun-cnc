from __future__ import annotations

from typing import Any

from ..catalogs import resolve_machine
from ..models import GeometryAnalysis, Operation, Setup
from ..operation_library import get_operation_definition, validate_parameters


def _geometry_feature_ids(analysis: GeometryAnalysis) -> set[str]:
    return {
        feature.id
        for collection in (
            analysis.planar_features,
            analysis.cylindrical_features,
            analysis.prismatic_features,
            analysis.planar_machining_features,
            analysis.internal_profile_features,
        )
        for feature in collection
    } | {
        section.source_feature_id for section in analysis.rotational_sections
    } | {
        f"SOLID-{candidate.index}" for candidate in analysis.solid_candidates
    }


def audit_operation_contract(
    analysis: GeometryAnalysis,
    setup: Setup,
    operation: Operation,
    machine: str,
) -> dict[str, Any]:
    """Validate one planned operation against deterministic execution contracts.

    This is deliberately cheaper than generating a toolpath. It is the first
    gate in the planning loop and makes every AI-selected operation traceable to
    geometry, a registered CAM capability, a catalog tool and valid parameters.
    """

    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def check(identifier: str, passed: bool, message: str, *, blocking: bool = True) -> None:
        status = "passed" if passed else "failed" if blocking else "warning"
        checks.append({"id": identifier, "status": status, "message": message})
        if not passed:
            (blockers if blocking else warnings).append(message)

    try:
        definition = get_operation_definition(operation.type)
    except ValueError as error:
        check("operation_capability", False, str(error))
        return {
            "operation_id": operation.id,
            "operation_name": operation.name,
            "setup_id": setup.id,
            "status": "blocked",
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
        }

    check(
        "operation_capability",
        definition.maturity != "planned",
        f"{definition.name} 能力成熟度为 {definition.maturity}",
    )
    if definition.maturity == "experimental":
        warnings.append(f"{operation.id} 使用实验性能力 {definition.id}，必须经过真实刀路与仿真验证")

    feature_count = len(operation.feature_ids)
    check(
        "feature_selection",
        feature_count >= definition.geometry.minimum_selection
        and (definition.geometry.maximum_selection is None or feature_count <= definition.geometry.maximum_selection),
        f"已绑定 {feature_count} 个特征，能力要求至少 {definition.geometry.minimum_selection} 个",
    )
    recognized = _geometry_feature_ids(analysis)
    derived_features = sorted(set(operation.feature_ids) - recognized)
    if derived_features:
        warnings.append(
            f"{operation.id} 使用规划器派生特征：{'、'.join(derived_features)}；刀路阶段必须解析为真实几何"
        )
    checks.append({
        "id": "geometry_binding",
        "status": "warning" if derived_features else "passed",
        "message": "全部特征来自几何分析" if not derived_features else "包含规划器派生特征",
        "derived_feature_ids": derived_features,
    })

    check(
        "tool_compatibility",
        operation.tool.kind in definition.tool.accepts,
        f"刀具 {operation.tool.id}（{operation.tool.kind}）"
        + ("与工序能力匹配" if operation.tool.kind in definition.tool.accepts else "与工序能力不匹配"),
    )
    check(
        "tool_catalog",
        operation.tool.catalog_match,
        f"刀具 {operation.tool.id} 未绑定真实目录参数",
        blocking=False,
    )

    try:
        validate_parameters(definition, operation.parameters)
        checks.append({"id": "parameters", "status": "passed", "message": "工序参数符合能力约束"})
    except ValueError as error:
        check("parameters", False, str(error))

    machine_profile = resolve_machine(setup.machine_id or setup.machine_name or machine)
    check(
        "machine_tool_envelope",
        operation.tool.diameter_mm <= machine_profile.max_tool_diameter_mm,
        f"刀具直径 {operation.tool.diameter_mm:g} mm，机床上限 {machine_profile.max_tool_diameter_mm:g} mm",
    )
    requested_rpm = operation.parameters.get("spindle_rpm", operation.parameters.get("maximum_spindle_rpm"))
    if isinstance(requested_rpm, (int, float)):
        check(
            "spindle_limit",
            float(requested_rpm) <= min(machine_profile.max_spindle_rpm, operation.tool.max_rpm),
            f"请求转速 {float(requested_rpm):g} rpm，机床/刀具上限 {min(machine_profile.max_spindle_rpm, operation.tool.max_rpm):g} rpm",
        )

    axis_norm = (setup.work_axis.x ** 2 + setup.work_axis.y ** 2 + setup.work_axis.z ** 2) ** 0.5
    check("setup_frame", axis_norm > 0.99, f"装夹 {setup.id} 的工作轴已定义")

    status = "blocked" if blockers else "warning" if warnings else "passed"
    return {
        "operation_id": operation.id,
        "operation_name": operation.name,
        "setup_id": setup.id,
        "operation_type": operation.type,
        "engine_provider": definition.engine.provider,
        "engine_operation": definition.engine.operation,
        "maturity": definition.maturity,
        "status": status,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "feature_ids": operation.feature_ids,
        "tool_id": operation.tool.id,
    }


def summarize_operation_audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        status: sum(record.get("status") == status for record in records)
        for status in ("passed", "warning", "blocked")
    }
    status = "blocked" if counts["blocked"] else "warning" if counts["warning"] else "passed"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "operation_count": len(records),
        "counts": counts,
        "blocked_operation_ids": [
            str(record.get("operation_id")) for record in records if record.get("status") == "blocked"
        ],
        "warning_operation_ids": [
            str(record.get("operation_id")) for record in records if record.get("status") == "warning"
        ],
        "records": records,
    }
