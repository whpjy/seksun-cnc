from __future__ import annotations

from math import ceil, sqrt

from .models import Bounds, FixtureComponent, GeometryAnalysis, ProcessPlan, SafetyConfiguration, Vec3


def _bounds(minimum: tuple[float, float, float], maximum: tuple[float, float, float]) -> Bounds:
    return Bounds(
        minimum=Vec3(x=minimum[0], y=minimum[1], z=minimum[2]),
        maximum=Vec3(x=maximum[0], y=maximum[1], z=maximum[2]),
        size=Vec3(x=maximum[0] - minimum[0], y=maximum[1] - minimum[1], z=maximum[2] - minimum[2]),
    )


def build_safety_configuration(
    analysis: GeometryAnalysis,
    clearance_mm: float = 3,
    vise_grip_height_mm: float = 1.5,
    support_thickness_mm: float = 3.0,
    setup_axes: list[tuple[str, Vec3]] | None = None,
) -> SafetyConfiguration:
    part = analysis.measurements["bounding_box"]
    assert not isinstance(part, float)
    jaw_overhang = 8.0
    jaw_depth = 12.0
    axes = setup_axes or [("SETUP-1", Vec3(x=0, y=0, z=1))]
    part_corners = _box_corners(part)
    fixtures: list[FixtureComponent] = []
    for setup_id, axis_value in axes:
        work_axis = (axis_value.x, axis_value.y, axis_value.z)
        reference = (0.0, 1.0, 0.0) if abs(work_axis[2]) > 0.9 else (0.0, 0.0, 1.0)
        local_x = (
            reference[1] * work_axis[2] - reference[2] * work_axis[1],
            reference[2] * work_axis[0] - reference[0] * work_axis[2],
            reference[0] * work_axis[1] - reference[1] * work_axis[0],
        )
        local_y = (
            work_axis[1] * local_x[2] - work_axis[2] * local_x[1],
            work_axis[2] * local_x[0] - work_axis[0] * local_x[2],
            work_axis[0] * local_x[1] - work_axis[1] * local_x[0],
        )
        frame = (local_x, local_y, work_axis)
        projected = [[_dot(corner, basis) for corner in part_corners] for basis in frame]
        minimum = [min(values) for values in projected]
        maximum = [max(values) for values in projected]
        stock_min = [minimum[0] - 3, minimum[1] - 3, minimum[2] - 2]
        stock_max = [maximum[0] + 3, maximum[1] + 3, maximum[2] + 2]
        jaw_top = min(maximum[2], minimum[2] + vise_grip_height_mm)
        setup_size = [maximum[index] - minimum[index] for index in range(3)]
        thin_setup = setup_size[2] <= max(3.5, min(setup_size[0], setup_size[1]) * 0.12)

        def local_box(local_minimum: tuple[float, float, float], local_maximum: tuple[float, float, float]) -> Bounds:
            world_corners = [
                tuple(x * local_x[index] + y * local_y[index] + z * work_axis[index] for index in range(3))
                for x in (local_minimum[0], local_maximum[0])
                for y in (local_minimum[1], local_maximum[1])
                for z in (local_minimum[2], local_maximum[2])
            ]
            return _bounds(
                tuple(min(corner[index] for corner in world_corners) for index in range(3)),
                tuple(max(corner[index] for corner in world_corners) for index in range(3)),
            )

        if thin_setup:
            support_margin = 4.0
            support_minimum = (
                minimum[0] - support_margin,
                minimum[1] - support_margin,
                minimum[2] - support_thickness_mm,
            )
            support_maximum = (
                maximum[0] + support_margin,
                maximum[1] + support_margin,
                minimum[2],
            )
            fixtures.extend([
                FixtureComponent(
                    id=f"{setup_id}-SACRIFICIAL-BACKING",
                    name=f"{setup_id} 牺牲垫板（允许受控切入）",
                    setup_id=setup_id,
                    work_axis=axis_value,
                    kind="sacrificial",
                    bounds=local_box(support_minimum, support_maximum),
                ),
                FixtureComponent(
                    id=f"{setup_id}-MACHINE-BED",
                    name=f"{setup_id} 牺牲垫板下方禁入区",
                    setup_id=setup_id,
                    work_axis=axis_value,
                    kind="machine",
                    bounds=local_box(
                        (support_minimum[0], support_minimum[1], support_minimum[2] - 5.0),
                        (support_maximum[0], support_maximum[1], support_minimum[2]),
                    ),
                ),
            ])
        else:
            for side, y_minimum, y_maximum, label in (
                ("FRONT", stock_min[1] - jaw_depth, stock_min[1], "前"),
                ("BACK", stock_max[1], stock_max[1] + jaw_depth, "后"),
            ):
                fixtures.append(FixtureComponent(
                    id=f"{setup_id}-VISE-JAW-{side}", name=f"{setup_id} 平口钳{label}钳口禁入区",
                    setup_id=setup_id, work_axis=axis_value,
                    bounds=local_box(
                        (stock_min[0] - jaw_overhang, y_minimum, stock_min[2]),
                        (stock_max[0] + jaw_overhang, y_maximum, jaw_top),
                    ),
                ))
    return SafetyConfiguration(
        clearance_mm=clearance_mm,
        vise_grip_height_mm=vise_grip_height_mm,
        fixture_strategy="sacrificial_plate" if any(item.kind == "sacrificial" for item in fixtures) else "vise",
        support_thickness_mm=support_thickness_mm,
        fixture_components=fixtures,
    )


def _circle_overlaps_box(x: float, y: float, radius: float, bounds: Bounds) -> bool:
    nearest_x = max(bounds.minimum.x, min(x, bounds.maximum.x))
    nearest_y = max(bounds.minimum.y, min(y, bounds.maximum.y))
    return (nearest_x - x) ** 2 + (nearest_y - y) ** 2 <= radius * radius


def _vertical_overlap(min_z: float, max_z: float, bounds: Bounds) -> bool:
    return min_z <= bounds.maximum.z and max_z >= bounds.minimum.z


def _dot(point: tuple[float, float, float], axis: tuple[float, float, float]) -> float:
    return sum(point[index] * axis[index] for index in range(3))


def _box_corners(bounds: Bounds) -> list[tuple[float, float, float]]:
    return [
        (x, y, z)
        for x in (bounds.minimum.x, bounds.maximum.x)
        for y in (bounds.minimum.y, bounds.maximum.y)
        for z in (bounds.minimum.z, bounds.maximum.z)
    ]


def _oriented_envelope_overlaps_box(
    tip: tuple[float, float, float], axis: tuple[float, float, float],
    start: float, end: float, radius: float, bounds: Bounds,
) -> bool:
    projections = [_dot(corner, axis) for corner in _box_corners(bounds)]
    envelope_min = _dot(tip, axis) + start
    envelope_max = _dot(tip, axis) + end
    if envelope_max < min(projections) or envelope_min > max(projections):
        return False
    segment_start = tuple(tip[index] + axis[index] * start for index in range(3))
    segment_end = tuple(tip[index] + axis[index] * end for index in range(3))
    expanded_minimum = tuple(getattr(bounds.minimum, name) - radius for name in ("x", "y", "z"))
    expanded_maximum = tuple(getattr(bounds.maximum, name) + radius for name in ("x", "y", "z"))
    direction = tuple(segment_end[index] - segment_start[index] for index in range(3))
    lower, upper = 0.0, 1.0
    for index in range(3):
        if abs(direction[index]) < 1e-9:
            if segment_start[index] < expanded_minimum[index] or segment_start[index] > expanded_maximum[index]:
                return False
            continue
        first = (expanded_minimum[index] - segment_start[index]) / direction[index]
        second = (expanded_maximum[index] - segment_start[index]) / direction[index]
        lower = max(lower, min(first, second))
        upper = min(upper, max(first, second))
        if lower > upper:
            return False
    return True


def detect_collisions(analysis: GeometryAnalysis, plan: ProcessPlan, cam_result: dict) -> dict[str, object]:
    part = analysis.measurements["bounding_box"]
    assert not isinstance(part, float)
    safety = plan.safety or build_safety_configuration(analysis)
    stock_allowance = plan.stock.get("allowance_mm", {})
    stock_xy = float(stock_allowance.get("xy", 3))
    stock_z = float(stock_allowance.get("z", 2))
    stock_bounds = _bounds(
        (part.minimum.x - stock_xy, part.minimum.y - stock_xy, part.minimum.z - stock_z),
        (part.maximum.x + stock_xy, part.maximum.y + stock_xy, part.maximum.z + stock_z),
    )
    operations = {operation.id: operation for setup in plan.setups for operation in setup.operations}
    collisions: list[dict[str, object]] = []
    collision_keys: set[tuple[str, str, str]] = set()
    low_rapids: list[dict[str, object]] = []
    default_axis = (0.0, 0.0, 1.0)
    default_required_rapid_z = stock_bounds.maximum.z + safety.clearance_mm
    support_penetration_mm = 0.0

    def record(kind: str, operation_id: str, target_id: str, x: float, y: float, z: float) -> None:
        key = (kind, operation_id, target_id)
        if key in collision_keys:
            return
        collision_keys.add(key)
        collisions.append({
            "kind": kind, "operation_id": operation_id, "target_id": target_id,
            "position": {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)},
        })

    for segment in cam_result.get("preview_segments", []):
        operation_id = str(segment.get("operation_id", ""))
        operation = operations.get(operation_id)
        if not operation:
            continue
        x1, y1, z1 = float(segment["x1"]), float(segment["y1"]), float(segment["z1"])
        x2, y2, z2 = float(segment["x2"]), float(segment["y2"]), float(segment["z2"])
        axis_payload = segment.get("work_axis") or {"x": 0, "y": 0, "z": 1}
        axis = (float(axis_payload["x"]), float(axis_payload["y"]), float(axis_payload["z"]))
        axis_length = sqrt(sum(value * value for value in axis)) or 1
        axis = tuple(value / axis_length for value in axis)
        required_rapid_z = max(_dot(corner, axis) for corner in _box_corners(stock_bounds)) + safety.clearance_mm
        local_z1 = float(segment.get("local_z1", _dot((x1, y1, z1), axis)))
        local_z2 = float(segment.get("local_z2", _dot((x2, y2, z2), axis)))
        distance = sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)
        axial_distance = abs((x2 - x1) * axis[0] + (y2 - y1) * axis[1] + (z2 - z1) * axis[2])
        transverse_distance = sqrt(max(0, distance * distance - axial_distance * axial_distance))
        if segment.get("motion") == "rapid" and transverse_distance > 0.01 and min(local_z1, local_z2) < required_rapid_z - 0.01:
            low_rapids.append({
                "operation_id": operation_id,
                "setup_id": segment.get("setup_id"),
                "minimum_z": round(min(local_z1, local_z2), 3),
                "required_z": round(required_rapid_z, 3),
            })
        sample_count = max(1, ceil(distance / max(operation.tool.diameter_mm * 0.25, 1)))
        for sample in range(sample_count + 1):
            ratio = sample / sample_count
            x = x1 + (x2 - x1) * ratio
            y = y1 + (y2 - y1) * ratio
            tip_z = z1 + (z2 - z1) * ratio
            tip = (x, y, tip_z)
            cutter_length = min(operation.tool.flute_length_mm, operation.tool.stickout_mm)
            holder_start = operation.tool.stickout_mm
            holder_end = holder_start + 60
            for fixture in safety.fixture_components:
                if fixture.setup_id and fixture.setup_id != segment.get("setup_id"):
                    continue
                if fixture.kind == "sacrificial":
                    fixture_top = max(_dot(corner, axis) for corner in _box_corners(fixture.bounds))
                    tip_projection = _dot(tip, axis)
                    if tip_projection < fixture_top:
                        support_penetration_mm = max(support_penetration_mm, fixture_top - tip_projection)
                    continue
                if _oriented_envelope_overlaps_box(tip, axis, 0, cutter_length, operation.tool.diameter_mm / 2, fixture.bounds):
                    record("tool_fixture", operation_id, fixture.id, x, y, tip_z)
                if _oriented_envelope_overlaps_box(tip, axis, holder_start, holder_end, operation.tool.holder_diameter_mm / 2, fixture.bounds):
                    record("holder_fixture", operation_id, fixture.id, x, y, tip_z)
            if _oriented_envelope_overlaps_box(tip, axis, holder_start, holder_end, operation.tool.holder_diameter_mm / 2, stock_bounds):
                record("holder_stock", operation_id, "STOCK", x, y, tip_z)

    requires_support = plan.stock.get("type") == "sheet"
    has_sacrificial_support = any(item.kind == "sacrificial" for item in safety.fixture_components)
    has_protected_base = any(item.kind == "machine" for item in safety.fixture_components)
    support_ok = not requires_support or (has_sacrificial_support and has_protected_base)
    checks = [
        {
            "id": "rapid_clearance", "status": "passed" if not low_rapids else "failed",
            "message": "所有装夹方向的横向快移均不低于各自安全平面" if not low_rapids else f"检测到 {len(low_rapids)} 条低于装夹安全平面的横向快移",
        },
        {
            "id": "tool_fixture", "status": "passed" if not any(item["kind"] == "tool_fixture" for item in collisions) else "failed",
            "message": "刀具扫掠未进入夹具禁入区" if not any(item["kind"] == "tool_fixture" for item in collisions) else "刀具扫掠与夹具禁入区相交",
        },
        {
            "id": "holder_clearance", "status": "passed" if not any(str(item["kind"]).startswith("holder_") for item in collisions) else "failed",
            "message": "刀柄与毛坯/夹具保持间隙" if not any(str(item["kind"]).startswith("holder_") for item in collisions) else "刀柄与毛坯或夹具相交",
        },
        {
            "id": "sheet_support", "status": "passed" if support_ok else "failed",
            "message": (
                f"薄板采用 {safety.support_thickness_mm:g} mm 牺牲垫板，最大受控切入 {support_penetration_mm:.2f} mm"
                if requires_support and support_ok
                else "薄板未配置牺牲垫板及其下方禁入区"
                if requires_support
                else "非薄板工件无需牺牲垫板"
            ),
        },
    ]
    status = "failed" if collisions or low_rapids or not support_ok else "passed"
    return {
        "schema_version": "0.8.0",
        "engine": "Seksun CNC swept-envelope collision checker",
        "status": status,
        "configuration": safety.model_dump(mode="json"),
        "checks": checks,
        "collisions": collisions,
        "low_rapids": low_rapids,
        "metrics": {
            "fixture_component_count": len(safety.fixture_components),
            "collision_count": len(collisions),
            "low_rapid_count": len(low_rapids),
            "required_rapid_z": round(default_required_rapid_z, 3),
            "support_penetration_mm": round(support_penetration_mm, 3),
        },
        "limitations": [
            "采用圆柱包络与轴对齐夹具禁入盒，复杂刀柄和夹具曲面会被保守近似。",
            "当前按各 Setup 的工作轴校核刀路，不包含机床各轴、主轴头和换刀动作。",
        ],
    }
