from __future__ import annotations

from math import ceil, floor, hypot, sqrt
from typing import Callable

from .models import GeometryAnalysis, ProcessPlan


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = sqrt(sum(value * value for value in vector))
    return tuple(value / length for value in vector)


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (left[1] * right[2] - left[2] * right[1], left[2] * right[0] - left[0] * right[2], left[0] * right[1] - left[1] * right[0])


def _frame(axis: dict[str, float]) -> dict[str, tuple[float, float, float]]:
    work_z = _normalize(tuple(float(axis[name]) for name in "xyz"))
    reference = (0.0, 1.0, 0.0) if abs(work_z[2]) > 0.9 else (0.0, 0.0, 1.0)
    work_x = _normalize(_cross(reference, work_z))
    return {"x": work_x, "y": _normalize(_cross(work_z, work_x)), "z": work_z}


def _local(point: dict, frame: dict[str, tuple[float, float, float]]) -> tuple[float, float, float]:
    values = tuple(float(point.get(name, 0.0)) for name in "xyz")
    return tuple(sum(values[index] * frame[name][index] for index in range(3)) for name in "xyz")


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _stock_grid(
    frame: dict[str, tuple[float, float, float]],
    stock_bounds: tuple[float, float, float, float, float, float],
    maximum_grid_size: int,
) -> tuple[float, float, float, float, float, float, float, int, int]:
    x0, y0, z0, x1, y1, z1 = stock_bounds
    corners = [_local({"x": x, "y": y, "z": z}, frame) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    min_x, max_x = min(p[0] for p in corners), max(p[0] for p in corners)
    min_y, max_y = min(p[1] for p in corners), max(p[1] for p in corners)
    bottom, top = min(p[2] for p in corners), max(p[2] for p in corners)
    width, height = max_x - min_x, max_y - min_y
    # Small, low-volume profile parts are highly sensitive to boundary-cell
    # quantization.  Keep enough samples to make target-volume validation and
    # the displayed in-process workpiece agree at sub-0.1 mm resolution.
    precision_floor = 0.06 if maximum_grid_size >= 600 and max(width, height) >= 40 else 0.18
    resolution = max(precision_floor, max(width, height) / maximum_grid_size)
    columns, rows = max(2, ceil(width / resolution) + 1), max(2, ceil(height / resolution) + 1)
    return min_x, min_y, bottom, top, width, height, resolution, columns, rows


def _surface_for_setup(setup_id: str, axis: dict[str, float], segments: list[dict], boundaries: list[dict], operations: dict, stock_bounds: tuple[float, float, float, float, float, float], maximum_grid_size: int) -> dict[str, object]:
    frame = _frame(axis)
    min_x, min_y, bottom, top, width, height, resolution, columns, rows = _stock_grid(frame, stock_bounds, maximum_grid_size)
    heights = [top] * (columns * rows)
    cut_count = 0
    unsupported_tools: set[str] = set()
    previous_cut_end: tuple[float, float, float] | None = None
    previous_cut_operation: str | None = None

    def remove_disk(x: float, y: float, cutting_z: float, radius: float) -> None:
        cutting_z = max(bottom, min(top, cutting_z))
        col_min = max(0, floor((x - radius - min_x) / resolution))
        col_max = min(columns - 1, ceil((x + radius - min_x) / resolution))
        row_min = max(0, floor((y - radius - min_y) / resolution))
        row_max = min(rows - 1, ceil((y + radius - min_y) / resolution))
        for row in range(row_min, row_max + 1):
            cell_y = min_y + row * resolution
            for column in range(col_min, col_max + 1):
                cell_x = min_x + column * resolution
                if (cell_x - x) ** 2 + (cell_y - y) ** 2 <= radius * radius:
                    index = row * columns + column
                    heights[index] = min(heights[index], cutting_z)

    for segment in segments:
        if segment.get("motion") != "cut":
            previous_cut_end = None
            previous_cut_operation = None
            continue
        operation_id = str(segment.get("operation_id"))
        operation = operations.get(operation_id)
        if not operation:
            previous_cut_end = None
            previous_cut_operation = None
            continue
        if operation.tool.kind not in {"face_mill", "end_mill", "drill", "ball_end_mill", "bull_end_mill"}:
            unsupported_tools.add(operation.tool.id)
            previous_cut_end = None
            previous_cut_operation = None
            continue
        start = _local({"x": segment["x1"], "y": segment["y1"], "z": segment["z1"]}, frame)
        end = _local({"x": segment["x2"], "y": segment["y2"], "z": segment["z2"]}, frame)
        sample_count = max(1, ceil(hypot(end[0] - start[0], end[1] - start[1]) / max(resolution * 0.5, 0.1)))
        continuous = previous_cut_operation == operation_id and previous_cut_end is not None and all(
            abs(previous_cut_end[index] - start[index]) <= 1e-7 for index in range(3)
        )
        for sample in range(1 if continuous else 0, sample_count + 1):
            ratio = sample / sample_count
            remove_disk(start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio, start[2] + (end[2] - start[2]) * ratio, operation.tool.diameter_mm / 2)
        cut_count += 1
        previous_cut_end = end
        previous_cut_operation = operation_id

    def inside_polygon(x: float, y: float, points: list[tuple[float, float, float]]) -> bool:
        inside, previous = False, points[-1]
        for current in points:
            if (previous[1] > y) != (current[1] > y):
                crossing_x = (current[0] - previous[0]) * (y - previous[1]) / (current[1] - previous[1]) + previous[0]
                if x < crossing_x:
                    inside = not inside
            previous = current
        return inside

    outer_boundaries = [
        boundary for boundary in boundaries
        if boundary.get("remove_side", "outside") == "outside"
        and len(boundary.get("points", [])) >= 3
    ]
    if outer_boundaries:
        boundary_points = [_local(point, frame) for point in outer_boundaries[0]["points"]]
        for row in range(rows):
            cell_y = min_y + row * resolution
            for column in range(columns):
                cell_x = min_x + column * resolution
                if not inside_polygon(cell_x, cell_y, boundary_points):
                    heights[row * columns + column] = bottom
    internal_boundaries = [
        [_local(point, frame) for point in boundary["points"]]
        for boundary in boundaries
        if boundary.get("remove_side") == "inside"
        and len(boundary.get("points", [])) >= 3
    ]
    for points in internal_boundaries:
        for row in range(rows):
            cell_y = min_y + row * resolution
            for column in range(columns):
                cell_x = min_x + column * resolution
                if inside_polygon(cell_x, cell_y, points):
                    heights[row * columns + column] = bottom

    cell_area = resolution * resolution
    removed_volume = sum((top - value) * cell_area for value in heights)
    local_volume = width * height * (top - bottom)
    removed_volume = min(removed_volume, local_volume)
    return {
        "setup_id": setup_id,
        "work_axis": {name: round(float(axis[name]), 6) for name in "xyz"},
        "frame": {name: {axis_name: round(value[index], 6) for index, axis_name in enumerate("xyz")} for name, value in frame.items()},
        "origin": {"x": round(min_x, 4), "y": round(min_y, 4)},
        "bottom_z": round(bottom, 4), "top_z": round(top, 4),
        "resolution_mm": round(resolution, 4), "columns": columns, "rows": rows,
        "heights": [round(value, 3) for value in heights], "cut_segment_count": cut_count,
        "removed_volume_mm3": round(removed_volume, 2), "stock_volume_mm3": round(local_volume, 2),
        "unsupported_tools": sorted(unsupported_tools),
    }


def _cumulative_surface(
    plan: ProcessPlan,
    segments: list[dict],
    boundaries: list[dict],
    operations: dict,
    operation_setup: dict[str, str],
    stock_bounds: tuple[float, float, float, float, float, float],
    maximum_grid_size: int,
    setup_local_bounds: dict[str, dict] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """Accumulate opposite-side setups into one double-sided height field.

    A regular height field only remembers material removed from its +Z side.  A
    flipped setup cuts the other side of the same blank, so the remaining stock
    is represented by an upper and a lower surface in the first setup's frame.
    """
    setup_axes = {setup.id: setup.work_axis.model_dump() for setup in plan.setups}
    reference_axis = next(
        (segment.get("work_axis") for segment in segments if segment.get("work_axis")),
        plan.setups[0].work_axis.model_dump() if plan.setups else {"x": 0, "y": 0, "z": 1},
    )
    frame = _frame(reference_axis)
    min_x, min_y, bottom, top, width, height, resolution, columns, rows = _stock_grid(frame, stock_bounds, maximum_grid_size)
    upper = [top] * (columns * rows)
    lower = [bottom] * (columns * rows)
    cut_count = 0
    unsupported_tools: set[str] = set()
    unsupported_setups: set[str] = set()
    previous_cut_end: tuple[float, float, float] | None = None
    previous_cut_key: tuple[str, str] | None = None
    setup_stock_tops = {}
    for setup in plan.setups:
        configured = (setup_local_bounds or {}).get(setup.id, {})
        maximum = configured.get("maximum") if isinstance(configured, dict) else None
        setup_stock_tops[setup.id] = (
            float(maximum["z"]) if isinstance(maximum, dict) and "z" in maximum
            else _stock_grid(_frame(setup.work_axis.model_dump()), stock_bounds, maximum_grid_size)[3]
        )

    def remove_disk(x: float, y: float, cutting_z: float, radius: float, from_top: bool, tool_kind: str) -> None:
        col_min = max(0, floor((x - radius - min_x) / resolution))
        col_max = min(columns - 1, ceil((x + radius - min_x) / resolution))
        row_min = max(0, floor((y - radius - min_y) / resolution))
        row_max = min(rows - 1, ceil((y + radius - min_y) / resolution))
        for row in range(row_min, row_max + 1):
            cell_y = min_y + row * resolution
            for column in range(col_min, col_max + 1):
                cell_x = min_x + column * resolution
                radial_squared = (cell_x - x) ** 2 + (cell_y - y) ** 2
                if radial_squared > radius * radius:
                    continue
                effective_z = cutting_z
                if tool_kind == "ball_end_mill":
                    ball_offset = radius - sqrt(max(radius * radius - radial_squared, 0.0))
                    effective_z = cutting_z + ball_offset if from_top else cutting_z - ball_offset
                effective_z = max(bottom, min(top, effective_z))
                index = row * columns + column
                if from_top:
                    upper[index] = max(lower[index], min(upper[index], effective_z))
                else:
                    lower[index] = min(upper[index], max(lower[index], effective_z))

    supported_tools = {"face_mill", "end_mill", "drill", "ball_end_mill", "bull_end_mill"}
    segment_total = len(segments)
    progress_interval = max(1000, segment_total // 24)
    for segment_index, segment in enumerate(segments, 1):
        if progress_callback and (segment_index == 1 or segment_index % progress_interval == 0):
            progress_callback(segment_index, segment_total)
        if segment.get("motion") != "cut":
            previous_cut_end = None
            previous_cut_key = None
            continue
        operation_id = str(segment.get("operation_id"))
        operation = operations.get(operation_id)
        if not operation:
            previous_cut_end = None
            previous_cut_key = None
            continue
        if operation.tool.kind not in supported_tools:
            unsupported_tools.add(operation.tool.id)
            previous_cut_end = None
            previous_cut_key = None
            continue
        setup_id = str(segment.get("setup_id") or operation_setup.get(operation_id, ""))
        axis_value = segment.get("work_axis") or setup_axes.get(setup_id) or reference_axis
        tool_axis = _normalize(tuple(float(axis_value[name]) for name in "xyz"))
        alignment = _dot(tool_axis, frame["z"])
        if abs(alignment) < 0.9:
            unsupported_setups.add(setup_id)
            previous_cut_end = None
            previous_cut_key = None
            continue
        start = _local({"x": segment["x1"], "y": segment["y1"], "z": segment["z1"]}, frame)
        end = _local({"x": segment["x2"], "y": segment["y2"], "z": segment["z2"]}, frame)
        sample_count = max(1, ceil(hypot(end[0] - start[0], end[1] - start[1]) / max(resolution * 0.5, 0.1)))
        local_z1 = float(segment.get("local_z1", _local({"x": segment["x1"], "y": segment["y1"], "z": segment["z1"]}, _frame(axis_value))[2]))
        local_z2 = float(segment.get("local_z2", _local({"x": segment["x2"], "y": segment["y2"], "z": segment["z2"]}, _frame(axis_value))[2]))
        cut_key = (operation_id, setup_id)
        continuous = previous_cut_key == cut_key and previous_cut_end is not None and all(
            abs(previous_cut_end[index] - start[index]) <= 1e-7 for index in range(3)
        )
        for sample in range(1 if continuous else 0, sample_count + 1):
            ratio = sample / sample_count
            local_cutting_z = local_z1 + (local_z2 - local_z1) * ratio
            if local_cutting_z > setup_stock_tops.get(setup_id, top) + 1e-6:
                continue
            remove_disk(
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
                start[2] + (end[2] - start[2]) * ratio,
                operation.tool.diameter_mm / 2,
                alignment > 0,
                operation.tool.kind,
            )
        cut_count += 1
        previous_cut_end = end
        previous_cut_key = cut_key
    if progress_callback:
        progress_callback(segment_total, segment_total)

    def polygon_area(points: list[tuple[float, float, float]]) -> float:
        return abs(sum(points[index][0] * points[(index + 1) % len(points)][1] - points[(index + 1) % len(points)][0] * points[index][1] for index in range(len(points))) / 2)

    def inside_polygon(x: float, y: float, points: list[tuple[float, float, float]]) -> bool:
        inside, previous = False, points[-1]
        for current in points:
            if (previous[1] > y) != (current[1] > y):
                crossing_x = (current[0] - previous[0]) * (y - previous[1]) / (current[1] - previous[1]) + previous[0]
                if x < crossing_x:
                    inside = not inside
            previous = current
        return inside

    candidate_boundaries = []
    internal_boundaries = []
    for boundary in boundaries:
        points = [_local(point, frame) for point in boundary.get("points", [])]
        if len(points) >= 3:
            if boundary.get("remove_side") == "inside":
                internal_boundaries.append(points)
            else:
                candidate_boundaries.append((polygon_area(points), points))
    if candidate_boundaries:
        _, outer_profile = max(candidate_boundaries, key=lambda item: item[0])
        for row in range(rows):
            cell_y = min_y + row * resolution
            for column in range(columns):
                cell_x = min_x + column * resolution
                if not inside_polygon(cell_x, cell_y, outer_profile):
                    index = row * columns + column
                    upper[index] = lower[index]
    for internal_profile in internal_boundaries:
        for row in range(rows):
            cell_y = min_y + row * resolution
            for column in range(columns):
                cell_x = min_x + column * resolution
                if inside_polygon(cell_x, cell_y, internal_profile):
                    index = row * columns + column
                    upper[index] = lower[index]

    # Heights are samples at grid vertices.  Using resolution² for every vertex
    # over-counts the outer row and column and can hide small removal volumes.
    cell_area = width * height / max(columns * rows, 1)
    remaining_volume = sum(max(0.0, upper[index] - lower[index]) * cell_area for index in range(len(upper)))
    stock_volume = width * height * (top - bottom)
    remaining_volume = min(remaining_volume, stock_volume)
    return {
        "setup_id": "CUMULATIVE",
        "is_cumulative": True,
        "included_setup_ids": [setup.id for setup in plan.setups],
        "work_axis": {name: round(float(reference_axis[name]), 6) for name in "xyz"},
        "frame": {name: {axis_name: round(value[index], 6) for index, axis_name in enumerate("xyz")} for name, value in frame.items()},
        "origin": {"x": round(min_x, 4), "y": round(min_y, 4)},
        "bottom_z": round(bottom, 4), "top_z": round(top, 4),
        "resolution_mm": round(resolution, 4), "columns": columns, "rows": rows,
        "heights": [round(value, 3) for value in upper],
        "lower_heights": [round(value, 3) for value in lower],
        "cut_segment_count": cut_count,
        "removed_volume_mm3": round(max(stock_volume - remaining_volume, 0), 2),
        "remaining_volume_mm3": round(remaining_volume, 2),
        "stock_volume_mm3": round(stock_volume, 2),
        "unsupported_tools": sorted(unsupported_tools),
        "unsupported_setups": sorted(item for item in unsupported_setups if item),
    }


def _adaptive_grid_size(segment_count: int, requested: int) -> int:
    """Keep dense production paths responsive without degrading small jobs.

    A 318-cell maximum produces approximately 0.5 mm samples for a 159 mm
    blank.  It also stays below the conformance check's 320-sample threshold,
    avoiding an unnecessary second stride that would lose more accuracy.
    """
    if segment_count >= 100_000:
        return min(requested, 318)
    if segment_count >= 50_000:
        return min(requested, 420)
    return requested


def simulate_material_removal(
    analysis: GeometryAnalysis,
    plan: ProcessPlan,
    cam_result: dict,
    maximum_grid_size: int = 640,
    progress_callback: Callable[[str, float], None] | None = None,
) -> dict[str, object]:
    """Approximate each setup and the cumulative stock left by all setups."""
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)
    stock_size = [float(value) for value in plan.stock["size_mm"]]
    center = ((bounds.minimum.x + bounds.maximum.x) / 2, (bounds.minimum.y + bounds.maximum.y) / 2, (bounds.minimum.z + bounds.maximum.z) / 2)
    stock_bounds = (center[0] - stock_size[0] / 2, center[1] - stock_size[1] / 2, center[2] - stock_size[2] / 2, center[0] + stock_size[0] / 2, center[1] + stock_size[1] / 2, center[2] + stock_size[2] / 2)
    operations = {operation.id: operation for setup in plan.setups for operation in setup.operations}
    operation_setup = {operation.id: setup.id for setup in plan.setups for operation in setup.operations}
    segments, boundaries = list(cam_result.get("preview_segments", [])), list(cam_result.get("profile_boundaries", []))
    cut_segment_count = sum(1 for segment in segments if segment.get("motion") == "cut")
    effective_grid_size = _adaptive_grid_size(cut_segment_count, maximum_grid_size)
    if progress_callback:
        progress_callback(
            f"已读取 {cut_segment_count:,} 条切削轨迹，自适应网格 {effective_grid_size}",
            0.03,
        )
    setup_surfaces = []
    for setup_index, setup in enumerate(plan.setups, 1):
        setup_segments = [segment for segment in segments if str(segment.get("setup_id") or operation_setup.get(str(segment.get("operation_id")), "")) == setup.id]
        setup_boundaries = [
            boundary for boundary in boundaries
            if str(boundary.get("setup_id") or operation_setup.get(str(boundary.get("operation_id")), "")) == setup.id
        ]
        axis = next((segment.get("work_axis") for segment in setup_segments if segment.get("work_axis")), None) or setup.work_axis.model_dump()
        # Per-setup previews are diagnostic views. Keep them lighter while
        # spending the available resolution on the cumulative final workpiece.
        setup_surfaces.append(_surface_for_setup(
            setup.id, axis, setup_segments, setup_boundaries, operations,
            stock_bounds, min(effective_grid_size, 220 if cut_segment_count >= 100_000 else 360),
        ))
        if progress_callback:
            progress_callback(
                f"已完成装夹余料 {setup_index}/{len(plan.setups)}",
                0.08 + setup_index / max(len(plan.setups), 1) * 0.24,
            )

    setup_local_bounds = {
        str(setup.get("setup_id")): setup.get("local_bounds")
        for setup in cam_result.get("setups", []) if isinstance(setup, dict)
    }
    cumulative = _cumulative_surface(
        plan, segments, boundaries, operations, operation_setup, stock_bounds,
        effective_grid_size, setup_local_bounds,
        progress_callback=(
            lambda current, total: progress_callback(
                f"正在累计双面轨迹 {current:,}/{total:,}",
                0.34 + current / max(total, 1) * 0.58,
            )
            if progress_callback else None
        ),
    )
    if progress_callback:
        progress_callback("正在汇总余料体积与显示网格", 0.96)
    cut_count = int(cumulative["cut_segment_count"])
    stock_volume = stock_size[0] * stock_size[1] * stock_size[2]
    removed_volume = float(cumulative["removed_volume_mm3"])
    unsupported_tools = sorted({tool for surface in setup_surfaces for tool in surface["unsupported_tools"]})
    warnings = [
        "正反向装夹已在统一工件坐标系中累计；非平行侧向装夹仍需体素或实体仿真。",
        "球刀采用球面包络；钻尖、圆鼻刀圆角和完整机床运动学尚未建模。",
    ]
    if unsupported_tools:
        warnings.append(f"未支持的刀具已跳过：{', '.join(unsupported_tools)}")
    if boundaries:
        warnings.append("闭合轮廓切透后按内外侧属性移除废料；桥位与废料实际脱落仍需现场复核。")
    return {
        "schema_version": "1.0.0", "engine": "Seksun CNC cumulative double-sided height-field simulator",
        "status": "completed" if cut_count else "warning", "method": "cumulative_double_sided_height_field",
        "metrics": {"initial_stock_volume_mm3": round(stock_volume, 2), "removed_volume_mm3": round(removed_volume, 2), "remaining_volume_mm3": round(max(stock_volume - removed_volume, 0), 2), "removed_percent": round(removed_volume / stock_volume * 100, 2) if stock_volume else 0, "cut_segment_count": cut_count, "resolution_mm": float(cumulative["resolution_mm"]), "requested_grid_size": maximum_grid_size, "effective_grid_size": effective_grid_size},
        "surface": cumulative, "setup_surfaces": setup_surfaces, "warnings": warnings,
    }
