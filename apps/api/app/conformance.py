from __future__ import annotations

import struct
from math import ceil, floor
from pathlib import Path


def _local(point: tuple[float, float, float], frame: dict[str, dict[str, float]]) -> tuple[float, float, float]:
    return tuple(
        sum(point[index] * float(frame[name][axis]) for index, axis in enumerate("xyz"))
        for name in "xyz"
    )


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


def compare_stock_to_target_mesh(surface: dict[str, object], mesh_path: Path, maximum_samples: int = 320) -> dict[str, object]:
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
            source_index = row * stride * columns + column * stride
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
    }
