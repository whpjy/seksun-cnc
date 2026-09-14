from __future__ import annotations

from collections import defaultdict

from .models import (
    CoverageTarget, GeometryAnalysis, ManufacturingCoverage, ProcessPlan, Vec3,
)
from .operation_library import OPERATION_DEFINITIONS


_MATURITY = {item.id: item.maturity for item in OPERATION_DEFINITIONS}


def _axis_key(axis: Vec3 | None) -> tuple[int, int, int]:
    if axis is None:
        return (0, 0, 0)
    values = (axis.x, axis.y, axis.z)
    index = max(range(3), key=lambda item: abs(values[item]))
    result = [0, 0, 0]
    result[index] = 1 if values[index] >= 0 else -1
    return tuple(result)


def evaluate_plan_coverage(
    analysis: GeometryAnalysis,
    plan: ProcessPlan,
) -> ManufacturingCoverage:
    """Measure semantic manufacturing coverage before expensive CAM generation.

    This is deliberately conservative: an unresolved loop or unvalidated
    operation keeps the plan out of production even when every generated
    operation has a toolpath.
    """
    operations = [
        operation
        for setup in plan.setups
        for operation in setup.operations
        if operation.enabled
    ]
    by_feature: dict[str, list] = defaultdict(list)
    for operation in operations:
        for feature_id in operation.feature_ids:
            by_feature[feature_id].append(operation)

    targets: list[CoverageTarget] = []

    def add_feature_target(feature, kind: str, required: set[str], label: str) -> None:
        matching = [
            operation for operation in by_feature.get(feature.id, [])
            if operation.type in required
        ]
        found_types = {operation.type for operation in matching}
        if feature.review_state == "review":
            state = "review"
        elif required <= found_types:
            state = "covered"
        else:
            state = "uncovered"
        targets.append(CoverageTarget(
            id=f"TARGET-{feature.id}", kind=kind, label=label, state=state,
            required_operation_types=sorted(required),
            covered_by=[operation.id for operation in matching],
            source_feature_ids=[feature.id],
        ))

    for feature in analysis.cylindrical_features:
        if feature.kind == "hole" and feature.review_state != "excluded":
            add_feature_target(
                feature, "hole", {"drilling"} if feature.diameter <= 12 else {"helical_boring"},
                f"Ø{feature.diameter:g} {feature.end_type} hole",
            )

    for feature in analysis.prismatic_features:
        if feature.review_state == "excluded":
            continue
        required = (
            {"pocket_roughing", "pocket_finishing"}
            if feature.kind == "pocket"
            else {"slot_roughing", "slot_finishing"}
        )
        add_feature_target(feature, feature.kind, required, f"{feature.kind} {feature.length:g}×{feature.width:g}")

    for feature in analysis.internal_profile_features:
        if feature.review_state == "excluded":
            continue
        if feature.machining_kind == "engraving":
            required = {"engraving"}
        elif feature.machining_kind == "blind_pocket":
            required = {"pocket_roughing", "pocket_finishing"}
        else:
            required = {"internal_profile_roughing", "internal_profile_finishing"}
        add_feature_target(
            feature,
            "internal_profile",
            required,
            f"{feature.machining_kind} {feature.length:g}×{feature.width:g}",
        )

    surface_ids = sorted({
        feature_id
        for operation in operations
        for feature_id in operation.feature_ids
        if feature_id.startswith("SURFACE-SET-")
    })
    surface_required = {"surface_roughing", "surface_3d", "waterline"}
    for feature_id in surface_ids:
        matching = [
            operation for operation in by_feature[feature_id]
            if operation.type in surface_required
        ]
        state = "covered" if surface_required <= {item.type for item in matching} else "uncovered"
        targets.append(CoverageTarget(
            id=f"TARGET-{feature_id}", kind="surface", label=feature_id,
            state=state, required_operation_types=sorted(surface_required),
            covered_by=[item.id for item in matching], source_feature_ids=[feature_id],
        ))

    bounds = analysis.measurements.get("bounding_box")
    profile_plane = None
    profile_axis = (0, 0, 1)
    if hasattr(bounds, "size"):
        sizes = (bounds.size.x, bounds.size.y, bounds.size.z)
        thin_axis = min(range(3), key=lambda index: sizes[index])
        profile_planes = [
            plane for plane in analysis.planar_features
            if abs((plane.normal.x, plane.normal.y, plane.normal.z)[thin_axis]) >= 0.98
        ]
        profile_plane = max(profile_planes, key=lambda item: item.area, default=None)
        if profile_plane:
            profile_axis = _axis_key(profile_plane.normal)

    if plan.stock.get("type") == "sheet" and profile_plane:
        profile_operations = [
            operation for operation in operations
            if operation.type in {"profile_roughing", "profile_finishing"}
        ]
        profile_types = {operation.type for operation in profile_operations}
        required = {"profile_roughing", "profile_finishing"}
        targets.append(CoverageTarget(
            id="TARGET-OUTER-PROFILE", kind="outer_profile", label="Outer profile",
            state="covered" if required <= profile_types else "uncovered",
            required_operation_types=sorted(required),
            covered_by=[operation.id for operation in profile_operations],
            source_feature_ids=[profile_plane.id],
        ))

        internal_loops = max(profile_plane.wire_count - 1, 0)
        recognized_holes = sum(
            feature.kind == "hole"
            and feature.review_state != "excluded"
            and _axis_key(feature.access_direction or feature.axis) == profile_axis
            for feature in analysis.cylindrical_features
        )
        recognized_prismatic = sum(
            feature.review_state != "excluded"
            and _axis_key(feature.access_direction) == profile_axis
            for feature in analysis.prismatic_features
        )
        recognized_internal_profiles = sum(
            feature.review_state != "excluded"
            and _axis_key(feature.access_direction) == profile_axis
            for feature in analysis.internal_profile_features
        )
        unresolved_loops = max(
            internal_loops - recognized_holes - recognized_prismatic
            - recognized_internal_profiles,
            0,
        )
        for index in range(unresolved_loops):
            targets.append(CoverageTarget(
                id=f"TARGET-UNRESOLVED-INTERNAL-{index + 1}",
                kind="internal_profile", label="Unclassified internal profile",
                state="unresolved", source_feature_ids=[profile_plane.id],
            ))

    issues: list[str] = []
    source_solids = int(analysis.topology.get("source_solids", 1))
    selection_confirmed = bool(analysis.topology.get("selection_confirmed", 0))
    if source_solids > 1 and not selection_confirmed:
        issues.append(f"输入包含 {source_solids} 个实体，目标实体尚未由用户确认")
    review_count = sum(target.state == "review" for target in targets)
    unresolved_count = sum(target.state == "unresolved" for target in targets)
    uncovered_count = sum(target.state == "uncovered" for target in targets)
    if review_count:
        issues.append(f"{review_count} 个制造目标仍需人工确认")
    if unresolved_count:
        issues.append(f"{unresolved_count} 个内部轮廓尚未分类")
    if uncovered_count:
        issues.append(f"{uncovered_count} 个制造目标没有完整工序覆盖")

    capability_gaps: list[str] = []
    for operation in operations:
        maturity = _MATURITY.get(operation.definition_id or operation.type, "unknown")
        if maturity not in {"validated", "production"}:
            capability_gaps.append(f"{operation.id} {operation.type} maturity={maturity}")
        if not operation.tool.catalog_match:
            capability_gaps.append(f"{operation.id} 使用临时刀具 {operation.tool.name}")
    capability_gaps = list(dict.fromkeys(capability_gaps))

    covered_count = sum(target.state == "covered" for target in targets)
    score = covered_count / len(targets) if targets else 0.0
    incomplete = uncovered_count > 0 or unresolved_count > 0 or not targets
    needs_review = review_count > 0 or bool(issues) or bool(capability_gaps)
    status = "incomplete" if incomplete else "review" if needs_review else "complete"
    production_ready = status == "complete" and not capability_gaps and plan.automation_status == "ready"
    return ManufacturingCoverage(
        status=status, score=round(score, 4), target_count=len(targets),
        covered_count=covered_count, unresolved_count=unresolved_count,
        review_count=review_count, production_ready=production_ready,
        targets=targets, issues=issues, capability_gaps=capability_gaps,
    )
