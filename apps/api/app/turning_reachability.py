from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cam.providers.turning import TurningContext

from .models import Operation
from .rotational_features import (
    RotationalProfile,
    suppress_external_grooves,
    suppress_rectangular_internal_grooves,
)


class TurningReachabilityCheck(BaseModel):
    id: str
    status: Literal["passed", "warning", "failed"]
    message: str
    measured_value: float | str | None = None
    limit_value: float | str | None = None


class TurningReachabilityResult(BaseModel):
    schema_version: str = "1.0.0"
    status: Literal["passed", "warning", "failed"]
    operation_id: str
    profile_id: str
    checks: list[TurningReachabilityCheck]
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _check(
    identifier: str, status: str, message: str,
    measured: float | str | None = None, limit: float | str | None = None,
) -> TurningReachabilityCheck:
    return TurningReachabilityCheck(
        id=identifier, status=status, message=message,
        measured_value=measured, limit_value=limit,
    )


def _material_side_nodes(profile: RotationalProfile) -> list[tuple[float, float]]:
    grouped: list[tuple[float, list[float]]] = []
    for point in sorted(profile.points, key=lambda item: (item.z, item.radius)):
        if grouped and abs(grouped[-1][0] - point.z) <= 1e-9:
            grouped[-1][1].append(point.radius)
        else:
            grouped.append((point.z, [point.radius]))
    return [
        (z_value, max(radii) if profile.side == "outer" else min(radii))
        for z_value, radii in grouped
    ]


def assess_turning_reachability(
    operation: Operation,
    profile: RotationalProfile,
    context: TurningContext,
    *,
    assembly_clearance_mm: float = 0.2,
) -> TurningReachabilityResult:
    if assembly_clearance_mm < 0:
        raise ValueError("assembly clearance cannot be negative")
    checks: list[TurningReachabilityCheck] = []
    groove_side = str(operation.parameters.get("groove_side", "external"))
    expected_kinds = {
        "turn_cutoff": {"cutoff"},
        "turn_grooving": {"internal_grooving"} if groove_side == "internal" else {"grooving"},
        "axial_drilling": {"drill"},
        "axial_tapping": {"tap"},
    }.get(operation.type, {"turning_od" if profile.side == "outer" else "turning_id"})
    kind_matches = operation.tool.kind in expected_kinds
    expected_kind = " / ".join(sorted(expected_kinds))
    checks.append(_check(
        "tool_side_compatibility", "passed" if kind_matches else "failed",
        "刀具类型与回转轮廓加工侧匹配" if kind_matches else f"{profile.side} 轮廓要求 {expected_kind} 刀具",
        operation.tool.kind, expected_kind,
    ))

    is_axial_tool = operation.type in {"axial_drilling", "axial_tapping"}
    orientation_complete = is_axial_tool or (
        operation.tool.orientation_code is not None and operation.tool.hand is not None
    )
    checks.append(_check(
        "tool_orientation_metadata", "passed" if orientation_complete else "failed",
        "轴向刀具按主轴中心线校验" if is_axial_tool else "刀尖方向与左右手信息已建档" if orientation_complete else "缺少刀尖方向或左右手信息，无法确认刀片有效象限",
        "axial_alignment" if is_axial_tool else str(operation.tool.orientation_code or "missing"),
        "spindle_centerline" if is_axial_tool else str(operation.tool.hand or "missing"),
    ))
    if operation.type in {"turn_od_roughing", "turn_od_finishing"}:
        expected_hand = "right" if context.cut_direction == "negative_z" else "left"
        direction_hand_ok = operation.tool.hand in {expected_hand, "neutral"}
        checks.append(_check(
            "cut_direction_tool_hand",
            "passed" if direction_hand_ok else "failed",
            "刀具左右手与轴向进给方向匹配"
            if direction_hand_ok else "轴向进给方向与外圆车刀左右手不匹配",
            str(operation.tool.hand or "missing"), expected_hand,
        ))
    nose_radius = float(operation.tool.nose_radius_mm or 0)
    checks.append(_check(
        "nose_radius", "passed" if nose_radius > 0 or is_axial_tool else "warning",
        "轴向钻削不使用车刀刀尖圆弧补偿" if is_axial_tool else "刀尖圆弧半径已用于精车补偿" if nose_radius > 0 else "未配置刀尖圆弧半径，将退化为中心线近似",
        "not_applicable" if is_axial_tool else nose_radius,
        "not_applicable" if is_axial_tool else "> 0",
    ))

    contour_operations = {
        "turn_od_roughing", "turn_od_finishing", "turn_id_roughing", "turn_id_finishing",
    }
    reachability_profile = profile
    if operation.type in contour_operations:
        if profile.side == "inner":
            reachability_profile = suppress_rectangular_internal_grooves(profile)
        else:
            reachability_profile = suppress_external_grooves(profile)
    nodes = _material_side_nodes(reachability_profile)
    ordered = sorted(nodes, reverse=context.cut_direction == "negative_z")
    undercuts: list[tuple[tuple[float, float], tuple[float, float]]] = []
    if operation.type in contour_operations:
        entered_reduced_diameter = False
        for left, right in zip(ordered, ordered[1:]):
            if right[1] < left[1] - 1e-6:
                entered_reduced_diameter = True
            elif entered_reduced_diameter and right[1] > left[1] + 1e-6:
                undercuts.append((left, right))
    checks.append(_check(
        "profile_undercut", "failed" if undercuts else "passed",
        "轮廓存在标准纵向车刀无法从当前方向到达的倒扣" if undercuts else "轮廓沿设定进给方向无隐藏倒扣",
        len(undercuts), 0,
    ))

    maximum_radius = max(point.radius for point in profile.points)
    stock_ok = maximum_radius <= context.stock_radius_mm + 1e-9
    checks.append(_check(
        "stock_envelope", "passed" if stock_ok else "failed",
        "目标轮廓位于棒料包络内" if stock_ok else "目标轮廓半径超出棒料半径",
        maximum_radius, context.stock_radius_mm,
    ))

    if profile.side == "inner" and operation.type in {"turn_id_roughing", "turn_id_finishing"}:
        holder_radius = operation.tool.holder_diameter_mm / 2
        required_bore = holder_radius + assembly_clearance_mm
        entry_ok = context.initial_bore_radius_mm >= required_bore - 1e-9
        checks.append(_check(
            "boring_bar_entry", "passed" if entry_ok else "failed",
            "初始孔可供镗杆安全进入" if entry_ok else "初始孔小于镗杆半径与装配间隙之和",
            context.initial_bore_radius_mm, required_bore,
        ))
        minimum_profile_radius = min(point.radius for point in profile.points)
        shank_ok = minimum_profile_radius >= required_bore - 1e-9
        checks.append(_check(
            "boring_bar_radial_clearance", "passed" if shank_ok else "failed",
            "内轮廓为镗杆保留了径向空间" if shank_ok else "目标内孔无法容纳当前镗杆包络",
            minimum_profile_radius, required_bore,
        ))
        profile_depth = max(point.z for point in profile.points) - min(point.z for point in profile.points)
        usable_stickout = max(operation.tool.stickout_mm - assembly_clearance_mm, 0)
        reach_ok = profile_depth <= usable_stickout + 1e-9
        checks.append(_check(
            "boring_bar_axial_reach", "passed" if reach_ok else "failed",
            "镗杆伸出长度覆盖内轮廓深度" if reach_ok else "内轮廓深度超过镗杆可用伸出长度",
            profile_depth, usable_stickout,
        ))

    if profile.side == "inner" and operation.type == "turn_grooving" and groove_side == "internal":
        holder_radius = operation.tool.holder_diameter_mm / 2
        required_bore = holder_radius + assembly_clearance_mm
        minimum_profile_radius = min(point.radius for point in profile.points)
        entry_ok = minimum_profile_radius >= required_bore - 1e-9
        checks.append(_check(
            "internal_groove_tool_entry", "passed" if entry_ok else "failed",
            "基孔可供内槽刀杆安全进入" if entry_ok else "基孔小于内槽刀杆半径与装配间隙之和",
            minimum_profile_radius, required_bore,
        ))
        required_reach = float(operation.parameters.get("groove_reach_depth_mm", 0))
        usable_stickout = max(operation.tool.stickout_mm - assembly_clearance_mm, 0)
        reach_ok = required_reach <= usable_stickout + 1e-9
        checks.append(_check(
            "internal_groove_axial_reach", "passed" if reach_ok else "failed",
            "内槽刀伸出覆盖槽位深度" if reach_ok else "内槽位置超过刀杆可用伸出长度",
            required_reach, usable_stickout,
        ))

    if operation.type == "axial_drilling":
        minimum_profile_diameter = min(point.radius for point in profile.points) * 2
        diameter_ok = operation.tool.diameter_mm <= minimum_profile_diameter + 1e-9
        checks.append(_check(
            "drill_profile_clearance", "passed" if diameter_ok else "failed",
            "钻头直径位于最小内孔包络内" if diameter_ok else "钻头直径超过最小内孔直径",
            operation.tool.diameter_mm, minimum_profile_diameter,
        ))
        programmed_depth = float(operation.parameters.get("depth_mm", 0))
        stickout_ok = operation.tool.stickout_mm >= programmed_depth + assembly_clearance_mm - 1e-9
        checks.append(_check(
            "drill_axial_reach", "passed" if stickout_ok else "failed",
            "钻头伸出覆盖编程深度与轴向间隙" if stickout_ok else "钻头伸出不足以覆盖编程深度与轴向间隙",
            operation.tool.stickout_mm, programmed_depth + assembly_clearance_mm,
        ))
        flute_ok = operation.tool.flute_length_mm >= programmed_depth - 1e-9
        checks.append(_check(
            "drill_flute_length", "passed" if flute_ok else "failed",
            "钻头有效刃长覆盖编程深度" if flute_ok else "钻头有效刃长不足",
            operation.tool.flute_length_mm, programmed_depth,
        ))

    blocking = [item.message for item in checks if item.status == "failed"]
    warnings = [item.message for item in checks if item.status == "warning"]
    status = "failed" if blocking else "warning" if warnings else "passed"
    return TurningReachabilityResult(
        status=status,
        operation_id=operation.id,
        profile_id=profile.id,
        checks=checks,
        blocking_reasons=blocking,
        warnings=warnings,
    )
