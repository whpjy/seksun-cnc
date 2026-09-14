from __future__ import annotations

import struct
from math import ceil, floor
from pathlib import Path
from typing import Any


def _local(point: tuple[float, float, float], frame: dict[str, dict[str, float]]) -> tuple[float, float, float]:
    return tuple(
        sum(point[index] * float(frame[name][axis]) for index, axis in enumerate("xyz"))
        for name in "xyz"
    )


def _world(point: tuple[float, float, float], frame: dict[str, dict[str, float]]) -> tuple[float, float, float]:
    return tuple(
        sum(point[index] * float(frame[name][axis]) for index, name in enumerate("xyz"))
        for axis in "xyz"
    )


def _point_segment_distance_squared(
    point: tuple[float, float, float],
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> float:
    direction = tuple(end[index] - start[index] for index in range(3))
    length_squared = sum(value * value for value in direction)
    if length_squared <= 1e-12:
        return sum((point[index] - start[index]) ** 2 for index in range(3))
    ratio = max(0.0, min(1.0, sum(
        (point[index] - start[index]) * direction[index] for index in range(3)
    ) / length_squared))
    nearest = tuple(start[index] + direction[index] * ratio for index in range(3))
    return sum((point[index] - nearest[index]) ** 2 for index in range(3))


def _binary_stl_triangles(path: Path, frame: dict[str, dict[str, float]]) -> list[list[tuple[float, float, float]]]:
    data = path.read_bytes()
    if len(data) < 84:
        raise ValueError("目标 STL 文件不完整")
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    if len(data) < 84 + triangle_count * 50:
        raise ValueError("目标 STL 三角面数据不完整")
    triangles = []
    for index in range(triangle_count):
        values = struct.unpack_from("<12fH", data, 84 + index * 50)
        triangles.append([
            _local(tuple(float(values[3 + vertex * 3 + axis]) for axis in range(3)), frame)
            for vertex in range(3)
        ])
    return triangles


def compare_stock_to_target_mesh(
    surface: dict[str, object],
    mesh_path: Path,
    maximum_samples: int = 320,
    toolpath_segments: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Compare cumulative stock and the target solid at matching XY samples.

    The comparison uses the target mesh's lower/upper intersections along the
    cumulative setup axis.  It therefore detects equal-volume errors where
    gouged target material and uncut stock would otherwise cancel each other.
    """
    columns, rows = int(surface["columns"]), int(surface["rows"])
    resolution = float(surface["resolution_mm"])
    origin = surface["origin"]
    frame = surface["frame"]
    upper, lower = surface["heights"], surface["lower_heights"]
    assert isinstance(origin, dict) and isinstance(frame, dict)
    assert isinstance(upper, list) and isinstance(lower, list)
    stride = max(1, ceil(max(columns, rows) / maximum_samples))
    sample_columns = max(1, (columns - 1) // stride)
    sample_rows = max(1, (rows - 1) // stride)
    step = resolution * stride
    origin_x, origin_y = float(origin["x"]), float(origin["y"])
    triangles = _binary_stl_triangles(mesh_path, frame)
    bins: list[list[list[tuple[float, float, float]]]] = [
        [] for _ in range(sample_columns * sample_rows)
    ]
    for triangle in triangles:
        minimum_x, maximum_x = min(point[0] for point in triangle), max(point[0] for point in triangle)
        minimum_y, maximum_y = min(point[1] for point in triangle), max(point[1] for point in triangle)
        column_min = max(0, floor((minimum_x - origin_x) / step))
        column_max = min(sample_columns - 1, floor((maximum_x - origin_x) / step))
        row_min = max(0, floor((minimum_y - origin_y) / step))
        row_max = min(sample_rows - 1, floor((maximum_y - origin_y) / step))
        for row in range(row_min, row_max + 1):
            for column in range(column_min, column_max + 1):
                bins[row * sample_columns + column].append(triangle)

    target_volume = stock_volume = overlap_volume = 0.0
    target_cells = stock_cells = 0
    defect_cells: dict[str, dict[tuple[int, int], dict[str, float | int]]] = {
        "overcut": {}, "excess_stock": {},
    }
    cell_area = step * step
    material_threshold = resolution * 0.25
    for row in range(sample_rows):
        y = origin_y + (row + 0.5) * step
        for column in range(sample_columns):
            x = origin_x + (column + 0.5) * step
            intersections: list[float] = []
            for triangle in bins[row * sample_columns + column]:
                (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = triangle
                denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
                if abs(denominator) < 1e-12:
                    continue
                weight_a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
                weight_b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
                weight_c = 1.0 - weight_a - weight_b
                if min(weight_a, weight_b, weight_c) >= -1e-7:
                    intersections.append(weight_a * z1 + weight_b * z2 + weight_c * z3)
            intersections.sort()
            unique: list[float] = []
            for value in intersections:
                if not unique or abs(value - unique[-1]) > 1e-4:
                    unique.append(value)
            target_interval = (unique[0], unique[-1]) if len(unique) >= 2 else None
            source_row = min(row * stride + stride // 2, rows - 1)
            source_column = min(column * stride + stride // 2, columns - 1)
            source_index = source_row * columns + source_column
            stock_interval = (float(lower[source_index]), float(upper[source_index]))
            if stock_interval[1] - stock_interval[0] <= material_threshold:
                stock_interval = None
            if target_interval:
                target_volume += (target_interval[1] - target_interval[0]) * cell_area
                target_cells += 1
            if stock_interval:
                stock_volume += (stock_interval[1] - stock_interval[0]) * cell_area
                stock_cells += 1
            if target_interval and stock_interval:
                overlap_volume += max(
                    0.0,
                    min(target_interval[1], stock_interval[1]) - max(target_interval[0], stock_interval[0]),
                ) * cell_area
            missing_ranges: list[tuple[float, float]] = []
            excess_ranges: list[tuple[float, float]] = []
            if target_interval:
                if not stock_interval:
                    missing_ranges.append(target_interval)
                else:
                    if stock_interval[0] > target_interval[0]:
                        missing_ranges.append((target_interval[0], min(stock_interval[0], target_interval[1])))
                    if stock_interval[1] < target_interval[1]:
                        missing_ranges.append((max(stock_interval[1], target_interval[0]), target_interval[1]))
            if stock_interval:
                if not target_interval:
                    excess_ranges.append(stock_interval)
                else:
                    if target_interval[0] > stock_interval[0]:
                        excess_ranges.append((stock_interval[0], min(target_interval[0], stock_interval[1])))
                    if target_interval[1] < stock_interval[1]:
                        excess_ranges.append((max(target_interval[1], stock_interval[0]), stock_interval[1]))
            for kind, ranges in (("overcut", missing_ranges), ("excess_stock", excess_ranges)):
                valid_ranges = [(start, end) for start, end in ranges if end > start + material_threshold]
                if not valid_ranges:
                    continue
                thickness = sum(end - start for start, end in valid_ranges)
                center_z = sum((start + end) * (end - start) / 2 for start, end in valid_ranges) / thickness
                defect_cells[kind][(row, column)] = {
                    "row": row, "column": column, "x": x, "y": y, "z": center_z,
                    "z_min": min(start for start, _ in valid_ranges),
                    "z_max": max(end for _, end in valid_ranges),
                    "deviation_mm": thickness, "volume_mm3": thickness * cell_area,
                }

    missing_volume = max(target_volume - overlap_volume, 0.0)
    excess_volume = max(stock_volume - overlap_volume, 0.0)
    overlap_percent = overlap_volume / max(target_volume, 1e-6) * 100
    missing_percent = missing_volume / max(target_volume, 1e-6) * 100
    excess_percent = excess_volume / max(target_volume, 1e-6) * 100
    if overlap_percent >= 95 and missing_percent <= 5 and excess_percent <= 8:
        status = "passed"
    elif overlap_percent >= 90 and missing_percent <= 10 and excess_percent <= 15:
        status = "warning"
    else:
        status = "failed"

    cut_segments = []
    for segment_index, segment in enumerate(toolpath_segments or []):
        if segment.get("motion") != "cut" or not segment.get("operation_id"):
            continue
        try:
            start = _local(tuple(float(segment[f"{axis}1"]) for axis in "xyz"), frame)
            end = _local(tuple(float(segment[f"{axis}2"]) for axis in "xyz"), frame)
        except (KeyError, TypeError, ValueError):
            continue
        cut_segments.append((segment_index, str(segment["operation_id"]), start, end))

    regions: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    for kind, cells in defect_cells.items():
        unvisited = set(cells)
        components: list[list[dict[str, float | int]]] = []
        while unvisited:
            seed = unvisited.pop()
            pending = [seed]
            component = []
            while pending:
                current = pending.pop()
                cell = cells[current]
                component.append(cell)
                row, column = current
                for neighbor in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
                    if neighbor in unvisited:
                        unvisited.remove(neighbor)
                        pending.append(neighbor)
            components.append(component)
        components.sort(key=lambda item: sum(float(cell["volume_mm3"]) for cell in item), reverse=True)
        for component_index, component in enumerate(components[:16], 1):
            volume = sum(float(cell["volume_mm3"]) for cell in component)
            weight = max(volume, 1e-9)
            center_local = tuple(
                sum(float(cell[axis]) * float(cell["volume_mm3"]) for cell in component) / weight
                for axis in "xyz"
            )
            center_world = _world(center_local, frame)
            local_bounds = (
                min(float(cell["x"]) - step / 2 for cell in component),
                min(float(cell["y"]) - step / 2 for cell in component),
                min(float(cell["z_min"]) for cell in component),
                max(float(cell["x"]) + step / 2 for cell in component),
                max(float(cell["y"]) + step / 2 for cell in component),
                max(float(cell["z_max"]) for cell in component),
            )
            world_corners = [
                _world((x_value, y_value, z_value), frame)
                for x_value in (local_bounds[0], local_bounds[3])
                for y_value in (local_bounds[1], local_bounds[4])
                for z_value in (local_bounds[2], local_bounds[5])
            ]
            nearest_by_operation: dict[str, tuple[float, int]] = {}
            for segment_index, operation_id, start, end in cut_segments:
                distance_squared = _point_segment_distance_squared(center_local, start, end)
                if operation_id not in nearest_by_operation or distance_squared < nearest_by_operation[operation_id][0]:
                    nearest_by_operation[operation_id] = (distance_squared, segment_index)
            attribution = []
            for operation_id, (distance_squared, segment_index) in sorted(
                nearest_by_operation.items(), key=lambda item: item[1][0],
            )[:3]:
                distance = distance_squared ** 0.5
                confidence = 1 / (1 + distance / max(step * 2, 1e-6))
                if kind == "excess_stock":
                    confidence *= 0.6
                attribution.append({
                    "operation_id": operation_id,
                    "segment_index": segment_index,
                    "distance_mm": round(distance, 3),
                    "confidence": round(confidence, 3),
                    "method": "nearest_cut_segment" if kind == "overcut" else "nearest_cut_context",
                })
            region_id = f"REGION-{'OVER' if kind == 'overcut' else 'REST'}-{component_index:03d}"
            regions.append({
                "id": region_id,
                "kind": kind,
                "severity": "critical" if kind == "overcut" else "high",
                "volume_mm3": round(volume, 3),
                "max_deviation_mm": round(max(float(cell["deviation_mm"]) for cell in component), 3),
                "sample_cell_count": len(component),
                "center": {axis: round(center_world[index], 4) for index, axis in enumerate("xyz")},
                "bounds": {
                    "minimum": {axis: round(min(point[index] for point in world_corners), 4) for index, axis in enumerate("xyz")},
                    "maximum": {axis: round(max(point[index] for point in world_corners), 4) for index, axis in enumerate("xyz")},
                },
                "attribution": attribution,
            })
            sample_stride = max(1, ceil(len(component) / 40))
            for cell in component[::sample_stride]:
                point = _world((float(cell["x"]), float(cell["y"]), float(cell["z"])), frame)
                samples.append({
                    "region_id": region_id, "kind": kind,
                    "position": {axis: round(point[index], 4) for index, axis in enumerate("xyz")},
                    "deviation_mm": round(float(cell["deviation_mm"]), 3),
                    "radius_mm": round(max(step * 0.42, 0.15), 3),
                })
    return {
        "status": status,
        "sample_resolution_mm": round(step, 4),
        "target_envelope_volume_mm3": round(target_volume, 2),
        "simulated_stock_volume_mm3": round(stock_volume, 2),
        "overlap_volume_mm3": round(overlap_volume, 2),
        "missing_target_volume_mm3": round(missing_volume, 2),
        "excess_stock_volume_mm3": round(excess_volume, 2),
        "target_overlap_percent": round(overlap_percent, 2),
        "missing_target_percent": round(missing_percent, 2),
        "excess_stock_percent": round(excess_percent, 2),
        "target_sample_cells": target_cells,
        "stock_sample_cells": stock_cells,
        "defect_regions": regions,
        "defect_samples": samples,
        "attribution_method": "nearest tool-center segment; geometric indication requiring CAM verification",
    }
