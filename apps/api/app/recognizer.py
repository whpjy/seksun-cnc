from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from math import sqrt

from .models import Bounds, CylindricalFeature, GeometryAnalysis, PrismaticFeature, Vec3


AXIS_PARALLEL_TOLERANCE = 0.9995
RADIUS_TOLERANCE_MM = 0.03
LINE_TOLERANCE_MM = 0.08
INTERVAL_TOLERANCE_MM = 0.2
PLANAR_AXIS_TOLERANCE = 0.98


def _dot(left: Vec3, right: Vec3) -> float:
    return left.x * right.x + left.y * right.y + left.z * right.z


def _subtract(left: Vec3, right: Vec3) -> Vec3:
    return Vec3(x=left.x - right.x, y=left.y - right.y, z=left.z - right.z)


def _scale(value: Vec3, factor: float) -> Vec3:
    return Vec3(x=value.x * factor, y=value.y * factor, z=value.z * factor)


def _length(value: Vec3) -> float:
    return sqrt(_dot(value, value))


def _normalize(value: Vec3) -> Vec3:
    magnitude = _length(value)
    if magnitude < 1e-9:
        return Vec3(x=0, y=0, z=1)
    normalized = _scale(value, 1 / magnitude)
    components = [normalized.x, normalized.y, normalized.z]
    dominant = max(range(3), key=lambda index: abs(components[index]))
    if components[dominant] < 0:
        normalized = _scale(normalized, -1)
    return normalized


def _line_distance(left: CylindricalFeature, right: CylindricalFeature) -> float:
    axis = _normalize(left.axis)
    delta = _subtract(right.center, left.center)
    perpendicular = _subtract(delta, _scale(axis, _dot(delta, axis)))
    return _length(perpendicular)


def _axis_interval(feature: CylindricalFeature, axis: Vec3) -> tuple[float, float]:
    middle = _dot(feature.center, axis)
    half = max(feature.length, 0) / 2
    return middle - half, middle + half


def _bounds_projection(bounds: Bounds, axis: Vec3) -> tuple[float, float]:
    coordinates = [
        _dot(Vec3(x=x, y=y, z=z), axis)
        for x, y, z in product(
            (bounds.minimum.x, bounds.maximum.x),
            (bounds.minimum.y, bounds.maximum.y),
            (bounds.minimum.z, bounds.maximum.z),
        )
    ]
    return min(coordinates), max(coordinates)


def _plane_positions(analysis: GeometryAnalysis, axis: Vec3) -> list[tuple[float, Vec3]]:
    positions: list[tuple[float, Vec3]] = []
    for plane in analysis.planar_features:
        normal = _normalize(plane.normal)
        if abs(_dot(normal, axis)) >= AXIS_PARALLEL_TOLERANCE:
            positions.append((_dot(plane.center, axis), normal))
    return positions


@dataclass
class _CylinderGroup:
    kind: str
    radius: float
    axis: Vec3
    members: list[CylindricalFeature] = field(default_factory=list)


def _same_cylinder(group: _CylinderGroup, feature: CylindricalFeature) -> bool:
    axis = _normalize(feature.axis)
    if group.kind != feature.kind:
        return False
    if abs(_dot(group.axis, axis)) < AXIS_PARALLEL_TOLERANCE:
        return False
    if abs(group.radius - feature.radius) > RADIUS_TOLERANCE_MM:
        return False
    return _line_distance(group.members[0], feature) <= max(
        LINE_TOLERANCE_MM,
        feature.radius * 0.01,
    )


def _split_disconnected(group: _CylinderGroup) -> list[list[CylindricalFeature]]:
    ordered = sorted(group.members, key=lambda item: _axis_interval(item, group.axis)[0])
    result: list[list[CylindricalFeature]] = []
    current: list[CylindricalFeature] = []
    current_end = float("-inf")
    for feature in ordered:
        start, end = _axis_interval(feature, group.axis)
        if current and start > current_end + INTERVAL_TOLERANCE_MM:
            result.append(current)
            current = []
        current.append(feature)
        current_end = max(current_end, end)
    if current:
        result.append(current)
    return result


def _merged_feature(
    members: list[CylindricalFeature],
    axis: Vec3,
    analysis: GeometryAnalysis,
    index: int,
) -> CylindricalFeature:
    starts_and_ends = [_axis_interval(item, axis) for item in members]
    start = min(item[0] for item in starts_and_ends)
    end = max(item[1] for item in starts_and_ends)
    length = max(0, end - start)
    reference = members[0]
    reference_position = _dot(reference.center, axis)
    center = _subtract(reference.center, _scale(axis, reference_position - (start + end) / 2))
    diameter = reference.radius * 2
    angular_span_degrees = min(360.0, sum(item.angular_span_degrees for item in members))
    reasons: list[str] = []

    plane_positions = _plane_positions(analysis, axis)
    end_tolerance = max(0.25, min(1.0, diameter * 0.08))
    start_planes = [normal for position, normal in plane_positions if abs(position - start) <= end_tolerance]
    end_planes = [normal for position, normal in plane_positions if abs(position - end) <= end_tolerance]
    if start_planes and end_planes:
        end_type = "through"
        access_direction = axis
        reasons.append("圆柱两端均与法向平行平面相交，判定为通孔候选")
    elif start_planes or end_planes:
        end_type = "blind"
        access_direction = _scale(axis, -1 if start_planes else 1)
        reasons.append("仅检测到一个开放端，判定为盲孔候选")
    else:
        end_type = "unknown"
        access_direction = axis
        reasons.append("未可靠匹配孔口平面，需要人工确认孔端类型")

    ratio = length / max(diameter, 0.01)
    confidence = 0.86
    review_state = "accepted"
    if end_type == "unknown":
        confidence -= 0.16
        review_state = "review"
    if ratio < 0.12 or length < 0.2:
        confidence = min(confidence, 0.32)
        review_state = "excluded"
        reasons.append("圆柱长度相对直径过短，更可能是倒角、圆角或建模碎面")
    elif ratio < 0.25:
        confidence -= 0.18
        review_state = "review"
        reasons.append("短圆柱特征可能是浅沉孔或倒角，需要复核")
    if len(members) > 1:
        confidence = min(0.96, confidence + 0.04)
        reasons.append(f"已合并 {len(members)} 个同轴同径圆柱面")
    if reference.kind == "hole" and angular_span_degrees < 355:
        confidence = min(confidence, 0.08)
        review_state = "excluded"
        reasons.append(
            f"圆周仅覆盖 {angular_span_degrees:.1f}°，属于开口圆弧、槽端或圆角，不按孔自动规划"
        )

    return CylindricalFeature(
        id=f"HF-{index}",
        kind=reference.kind,
        radius=reference.radius,
        diameter=diameter,
        length=length,
        center=center,
        axis=axis,
        angular_span_degrees=angular_span_degrees,
        source_face_ids=[face_id for item in members for face_id in (item.source_face_ids or [item.id])],
        segment_count=len(members),
        end_type=end_type,
        access_direction=access_direction,
        confidence=max(0, min(confidence, 1)),
        review_state=review_state,
        review_reasons=reasons,
    )


def _dominant_axis(value: Vec3) -> tuple[int, float]:
    components = [value.x, value.y, value.z]
    index = max(range(3), key=lambda item: abs(components[item]))
    return index, components[index]


def _component(value: Vec3, index: int) -> float:
    return (value.x, value.y, value.z)[index]


def _recognize_prismatic_features(analysis: GeometryAnalysis) -> list[PrismaticFeature]:
    part_bounds = analysis.measurements["bounding_box"]
    assert isinstance(part_bounds, Bounds)
    part_min = (part_bounds.minimum.x, part_bounds.minimum.y, part_bounds.minimum.z)
    part_max = (part_bounds.maximum.x, part_bounds.maximum.y, part_bounds.maximum.z)
    result: list[PrismaticFeature] = []

    axis_planes: dict[tuple[int, int], list] = {}
    for plane in analysis.planar_features:
        axis_index, signed_component = _dominant_axis(plane.normal)
        if abs(signed_component) < PLANAR_AXIS_TOLERANCE:
            continue
        axis_planes.setdefault((axis_index, 1 if signed_component > 0 else -1), []).append(plane)

    for (axis_index, sign), planes in axis_planes.items():
        outward_position = max(sign * _component(plane.center, axis_index) for plane in planes)
        transverse = [index for index in range(3) if index != axis_index]
        for plane in planes:
            if plane.bounds is None:
                continue
            depth = outward_position - sign * _component(plane.center, axis_index)
            if depth < 0.3:
                continue
            sizes = [_component(plane.bounds.size, index) for index in transverse]
            if min(sizes) < 1.0:
                continue
            projected_area = sizes[0] * sizes[1]
            rectangularity = plane.area / max(projected_area, 1e-6)
            if not 0.88 <= rectangularity <= 1.05:
                continue
            has_adjacency_evidence = plane.adjacent_edge_count > 0
            if has_adjacency_evidence and (
                plane.rising_edge_count < 2
                or plane.rising_edge_count <= plane.falling_edge_count
            ):
                continue

            touches: list[tuple[int, str]] = []
            tolerance = max(0.15, min(sizes) * 0.01)
            for index in transverse:
                if abs(_component(plane.bounds.minimum, index) - part_min[index]) <= tolerance:
                    touches.append((index, "min"))
                if abs(_component(plane.bounds.maximum, index) - part_max[index]) <= tolerance:
                    touches.append((index, "max"))
            open_axes = {
                index for index in transverse
                if (index, "min") in touches and (index, "max") in touches
            }
            if not touches:
                kind = "pocket"
                confidence = 0.84
                reason = "检测到低于同向外表面的封闭矩形底面，判定为型腔候选"
            elif len(touches) == 2 and len(open_axes) == 1:
                kind = "slot"
                confidence = 0.8
                reason = "矩形底面沿一个方向贯通零件边界，判定为贯通槽候选"
            else:
                continue

            length = max(sizes)
            width = min(sizes)
            state = "accepted"
            reasons = [reason, f"底面矩形度 {rectangularity:.2f}，深度 {depth:.2f} mm"]
            if has_adjacency_evidence:
                confidence = min(0.94, confidence + 0.06)
                reasons.append(
                    f"邻接拓扑检测到 {plane.rising_edge_count} 条向上侧壁边、"
                    f"{plane.falling_edge_count} 条向下侧壁边，符合凹特征底面"
                )
            if depth > width * 2.5:
                confidence -= 0.18
                state = "review"
                reasons.append("深宽比较大，需要复核刀具可达性与分层切削参数")
            if plane.wire_count > 1:
                confidence -= 0.12
                state = "review"
                reasons.append("底面包含多个边界环，可能存在岛屿或复合型腔")

            result.append(
                PrismaticFeature(
                    id=f"MF-{len(result) + 1}",
                    kind=kind,
                    source_face_id=plane.id,
                    center=plane.center,
                    bounds=plane.bounds,
                    access_direction=plane.normal,
                    length=length,
                    width=width,
                    depth=depth,
                    open_sides=len(touches),
                    confidence=max(0, min(confidence, 1)),
                    review_state=state,
                    review_reasons=reasons,
                )
            )
    return result


def normalize_manufacturing_features(analysis: GeometryAnalysis) -> GeometryAnalysis:
    groups: list[_CylinderGroup] = []
    for feature in analysis.cylindrical_features:
        feature.source_face_ids = feature.source_face_ids or [feature.id]
        matching = next((group for group in groups if _same_cylinder(group, feature)), None)
        if matching is None:
            matching = _CylinderGroup(
                kind=feature.kind,
                radius=feature.radius,
                axis=_normalize(feature.axis),
            )
            groups.append(matching)
        matching.members.append(feature)

    merged: list[CylindricalFeature] = []
    next_index = 1
    for group in groups:
        for connected_members in _split_disconnected(group):
            merged.append(_merged_feature(connected_members, group.axis, analysis, next_index))
            next_index += 1

    analysis.schema_version = "0.4.0"
    analysis.cylindrical_features = merged
    analysis.prismatic_features = _recognize_prismatic_features(analysis)
    return analysis
