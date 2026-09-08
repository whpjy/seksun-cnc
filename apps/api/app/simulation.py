from __future__ import annotations

from math import ceil, floor, hypot, sqrt

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


def _surface_for_setup(setup_id: str, axis: dict[str, float], segments: list[dict], boundaries: list[dict], operations: dict, stock_bounds: tuple[float, float, float, float, float, float], maximum_grid_size: int) -> dict[str, object]:
    frame = _frame(axis)
    x0, y0, z0, x1, y1, z1 = stock_bounds
    corners = [_local({"x": x, "y": y, "z": z}, frame) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    min_x, max_x = min(p[0] for p in corners), max(p[0] for p in corners)
    min_y, max_y = min(p[1] for p in corners), max(p[1] for p in corners)
    bottom, top = min(p[2] for p in corners), max(p[2] for p in corners)
    width, height = max_x - min_x, max_y - min_y
    resolution = max(0.18, max(width, height) / maximum_grid_size)
    columns, rows = max(2, ceil(width / resolution) + 1), max(2, ceil(height / resolution) + 1)
    heights = [top] * (columns * rows)
    cut_count = 0
    unsupported_tools: set[str] = set()

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
            continue
        operation = operations.get(str(segment.get("operation_id")))
        if not operation:
            continue
        if operation.tool.kind not in {"face_mill", "end_mill", "drill"}:
            unsupported_tools.add(operation.tool.id)
            continue
        start = _local({"x": segment["x1"], "y": segment["y1"], "z": segment["z1"]}, frame)
        end = _local({"x": segment["x2"], "y": segment["y2"], "z": segment["z2"]}, frame)
        sample_count = max(1, ceil(hypot(end[0] - start[0], end[1] - start[1]) / max(resolution * 0.5, 0.1)))
        for sample in range(sample_count + 1):
            ratio = sample / sample_count
            remove_disk(start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio, start[2] + (end[2] - start[2]) * ratio, operation.tool.diameter_mm / 2)
        cut_count += 1

    def inside_polygon(x: float, y: float, points: list[tuple[float, float, float]]) -> bool:
        inside, previous = False, points[-1]
        for current in points:
            if (previous[1] > y) != (current[1] > y):
                crossing_x = (current[0] - previous[0]) * (y - previous[1]) / (current[1] - previous[1]) + previous[0]
                if x < crossing_x:
                    inside = not inside
            previous = current
        return inside

    usable_boundaries = [boundary for boundary in boundaries if len(boundary.get("points", [])) >= 3]
    if usable_boundaries:
        boundary_points = [_local(point, frame) for point in usable_boundaries[0]["points"]]
        for row in range(rows):
            cell_y = min_y + row * resolution
            for column in range(columns):
                cell_x = min_x + column * resolution
                if not inside_polygon(cell_x, cell_y, boundary_points):
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


def simulate_material_removal(analysis: GeometryAnalysis, plan: ProcessPlan, cam_result: dict, maximum_grid_size: int = 520) -> dict[str, object]:
    """Approximate every setup in its own local +Z coordinate frame."""
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)
    stock_size = [float(value) for value in plan.stock["size_mm"]]
    center = ((bounds.minimum.x + bounds.maximum.x) / 2, (bounds.minimum.y + bounds.maximum.y) / 2, (bounds.minimum.z + bounds.maximum.z) / 2)
    stock_bounds = (center[0] - stock_size[0] / 2, center[1] - stock_size[1] / 2, center[2] - stock_size[2] / 2, center[0] + stock_size[0] / 2, center[1] + stock_size[1] / 2, center[2] + stock_size[2] / 2)
    operations = {operation.id: operation for setup in plan.setups for operation in setup.operations}
    operation_setup = {operation.id: setup.id for setup in plan.setups for operation in setup.operations}
    segments, boundaries = list(cam_result.get("preview_segments", [])), list(cam_result.get("profile_boundaries", []))
    setup_surfaces = []
    for setup in plan.setups:
        setup_segments = [segment for segment in segments if str(segment.get("setup_id") or operation_setup.get(str(segment.get("operation_id")), "")) == setup.id]
        setup_boundaries = [
            boundary for boundary in boundaries
            if str(boundary.get("setup_id") or operation_setup.get(str(boundary.get("operation_id")), "")) == setup.id
        ]
        axis = next((segment.get("work_axis") for segment in setup_segments if segment.get("work_axis")), None) or setup.work_axis.model_dump()
        setup_surfaces.append(_surface_for_setup(setup.id, axis, setup_segments, setup_boundaries, operations, stock_bounds, maximum_grid_size))

    cut_count = sum(int(surface["cut_segment_count"]) for surface in setup_surfaces)
    stock_volume = stock_size[0] * stock_size[1] * stock_size[2]
    removed_volume = max((float(surface["removed_volume_mm3"]) for surface in setup_surfaces), default=0.0)
    unsupported_tools = sorted({tool for surface in setup_surfaces for tool in surface["unsupported_tools"]})
    warnings = ["材料去除按各装夹的局部 +Z 独立计算；换装夹时重新定向工件与毛坯。", "钻尖、球刀/圆鼻刀轮廓和完整机床运动学尚未建模。"]
    if unsupported_tools:
        warnings.append(f"未支持的刀具已跳过：{', '.join(unsupported_tools)}")
    if boundaries:
        warnings.append("闭合外轮廓切透后隐藏外侧废料；桥位与废料实际脱落仍需现场复核。")
    fallback = setup_surfaces[0] if setup_surfaces else {"setup_id": "", "work_axis": {"x": 0, "y": 0, "z": 1}, "frame": {"x": {"x": 1, "y": 0, "z": 0}, "y": {"x": 0, "y": 1, "z": 0}, "z": {"x": 0, "y": 0, "z": 1}}, "origin": {"x": 0, "y": 0}, "bottom_z": 0, "top_z": 0, "resolution_mm": 1, "columns": 2, "rows": 2, "heights": [0, 0, 0, 0]}
    return {
        "schema_version": "0.9.0", "engine": "Seksun CNC multi-setup height-field simulator",
        "status": "completed" if cut_count else "warning", "method": "multi_setup_local_height_field",
        "metrics": {"initial_stock_volume_mm3": round(stock_volume, 2), "removed_volume_mm3": round(removed_volume, 2), "remaining_volume_mm3": round(max(stock_volume - removed_volume, 0), 2), "removed_percent": round(removed_volume / stock_volume * 100, 2) if stock_volume else 0, "cut_segment_count": cut_count, "resolution_mm": min((float(surface["resolution_mm"]) for surface in setup_surfaces), default=1)},
        "surface": fallback, "setup_surfaces": setup_surfaces, "warnings": warnings,
    }
