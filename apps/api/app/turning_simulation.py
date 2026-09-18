from __future__ import annotations

from math import ceil, hypot, pi, sqrt
from typing import Literal

from pydantic import BaseModel, Field

from .toolpath_ir import ToolpathProgram


class TurningStockSample(BaseModel):
    z: float
    outer_radius: float = Field(ge=0)
    inner_radius: float = Field(ge=0)


class TurningSimulationMetrics(BaseModel):
    initial_volume_mm3: float = Field(ge=0)
    remaining_volume_mm3: float = Field(ge=0)
    removed_volume_mm3: float = Field(ge=0)
    removal_percent: float = Field(ge=0, le=100)


class TurningSimulationResult(BaseModel):
    schema_version: str = "1.0.0"
    engine: str = "Seksun CNC Z-R turning simulator"
    approximation: Literal[
        "tool_centerline", "mixed_centerline_and_nose_circle", "thread_root_envelope",
    ] = "tool_centerline"
    status: Literal["completed", "failed"]
    resolution_mm: float = Field(gt=0)
    samples: list[TurningStockSample]
    metrics: TurningSimulationMetrics
    warnings: list[str] = Field(default_factory=list)


def _sample_count(z_min: float, z_max: float, resolution: float) -> int:
    return max(int(round((z_max - z_min) / resolution)) + 1, 2)


def _indices_between(z_values: list[float], start: float, end: float, half_width: float = 0) -> list[int]:
    minimum, maximum = sorted((start, end))
    minimum -= half_width
    maximum += half_width
    # Never map a narrow cut to a sample outside its programmed axial extent:
    # at a steep shoulder that invents material removal on the protected side.
    return [
        index for index, value in enumerate(z_values)
        if minimum - 1e-9 <= value <= maximum + 1e-9
    ]


def _sweep_nose_circle(
    z_values: list[float], outer: list[float], inner: list[float],
    *, start_x: float, start_z: float, end_x: float, end_z: float,
    nose_radius: float, cut_side: str, resolution: float,
) -> None:
    start_radius, end_radius = abs(start_x) / 2, abs(end_x) / 2
    path_length = hypot(end_z - start_z, end_radius - start_radius)
    sampling_step = max(min(resolution / 2, nose_radius / 8), 0.005)
    steps = max(1, min(int(ceil(path_length / sampling_step)), 20_000))
    for step_index in range(steps + 1):
        fraction = step_index / steps
        center_z = start_z + fraction * (end_z - start_z)
        center_radius = start_radius + fraction * (end_radius - start_radius)
        for index in _indices_between(z_values, center_z, center_z, nose_radius):
            axial_distance = abs(z_values[index] - center_z)
            if axial_distance > nose_radius + 1e-9:
                continue
            radial_reach = sqrt(max(nose_radius * nose_radius - axial_distance * axial_distance, 0))
            if cut_side == "external":
                surface_radius = max(center_radius - radial_reach, 0)
                outer[index] = max(min(outer[index], surface_radius), inner[index])
            else:
                surface_radius = center_radius + radial_reach
                inner[index] = min(max(inner[index], surface_radius), outer[index])


def simulate_turning_stock(
    program: ToolpathProgram,
    *,
    stock_radius_mm: float,
    z_min_mm: float,
    z_max_mm: float,
    resolution_mm: float = 0.1,
    initial_bore_radius_mm: float = 0,
    initial_samples: list[TurningStockSample] | None = None,
) -> TurningSimulationResult:
    if program.coordinate_convention != "diameter-x_z":
        raise ValueError("turning simulation requires diameter-x_z Toolpath IR")
    if stock_radius_mm <= 0 or initial_bore_radius_mm < 0 or initial_bore_radius_mm >= stock_radius_mm:
        raise ValueError("invalid turning stock radii")
    if resolution_mm <= 0 or z_max_mm <= z_min_mm:
        raise ValueError("invalid turning simulation range or resolution")

    count = _sample_count(z_min_mm, z_max_mm, resolution_mm)
    step = (z_max_mm - z_min_mm) / (count - 1)
    z_values = [z_min_mm + index * step for index in range(count)]
    if initial_samples:
        ordered_samples = sorted(initial_samples, key=lambda item: item.z)
        if len(ordered_samples) < 2:
            raise ValueError("initial stock state requires at least two samples")

        def interpolate(z_value: float, field: str) -> float:
            if z_value <= ordered_samples[0].z:
                return float(getattr(ordered_samples[0], field))
            if z_value >= ordered_samples[-1].z:
                return float(getattr(ordered_samples[-1], field))
            for left, right in zip(ordered_samples, ordered_samples[1:]):
                if left.z <= z_value <= right.z:
                    span = right.z - left.z
                    fraction = (z_value - left.z) / span if span else 0
                    return float(getattr(left, field)) + fraction * (
                        float(getattr(right, field)) - float(getattr(left, field))
                    )
            raise ValueError("initial stock interpolation failed")

        outer = [interpolate(z_value, "outer_radius") for z_value in z_values]
        inner = [interpolate(z_value, "inner_radius") for z_value in z_values]
        if any(in_radius > out_radius + 1e-9 for out_radius, in_radius in zip(outer, inner)):
            raise ValueError("initial stock state has inner radius outside outer radius")
    else:
        outer = [stock_radius_mm] * count
        inner = [initial_bore_radius_mm] * count
    initial_outer = list(outer)
    initial_inner = list(inner)
    used_nose_circle = False
    used_thread_envelope = False

    for channel in program.channels:
        position: dict[str, float] = {}
        for command in channel.commands:
            previous = dict(position)
            position.update({axis.upper(): float(value) for axis, value in command.axes.items()})
            if command.type == "thread_cut":
                start_z = float(command.parameters["start_z_mm"])
                end_z = float(command.parameters["end_z_mm"])
                target_diameter = float(command.parameters.get(
                    "target_diameter_mm", command.parameters["minor_diameter_mm"],
                ))
                target_radius = target_diameter / 2
                for index in _indices_between(z_values, start_z, end_z):
                    outer[index] = max(min(outer[index], target_radius), inner[index])
                used_thread_envelope = True
                continue
            if command.type == "drill_cycle":
                start_z = float(command.parameters["start_z_mm"])
                end_z = float(command.parameters["end_z_mm"])
                drill_radius = float(command.parameters.get("diameter_mm", 0)) / 2
                tip_length = max(float(command.parameters.get("drill_tip_length_mm", 0)), 0)
                direction = -1 if end_z < start_z else 1
                cylindrical_end = end_z - direction * tip_length
                for index in _indices_between(z_values, start_z, end_z):
                    z_value = z_values[index]
                    if direction * (z_value - cylindrical_end) <= 1e-9 or tip_length <= 1e-9:
                        local_radius = drill_radius
                    else:
                        distance_into_tip = abs(z_value - cylindrical_end)
                        local_radius = drill_radius * max(1 - distance_into_tip / tip_length, 0)
                    inner[index] = min(max(inner[index], local_radius), outer[index])
                continue
            if command.type != "feed_move" or "X" not in position or "Z" not in position:
                continue
            cut_side = str(command.parameters.get("cut_side", ""))
            if cut_side not in {"external", "internal", "facing"}:
                continue
            start_x = previous.get("X", position["X"])
            start_z = previous.get("Z", position["Z"])
            end_x, end_z = position["X"], position["Z"]
            half_width = max(float(command.parameters.get("axial_width_mm", 0)), 0) / 2

            if cut_side == "facing":
                retain = str(command.parameters.get("retain_direction", "negative_z"))
                for index, z_value in enumerate(z_values):
                    removed = z_value > end_z + 1e-9 if retain == "negative_z" else z_value < end_z - 1e-9
                    if removed:
                        outer[index] = inner[index]
                continue

            nose_radius = float(command.parameters.get("tool_nose_radius_mm", 0))
            if command.parameters.get("position_role") == "nose_center" and nose_radius > 0:
                _sweep_nose_circle(
                    z_values, outer, inner,
                    start_x=start_x, start_z=start_z, end_x=end_x, end_z=end_z,
                    nose_radius=nose_radius, cut_side=cut_side, resolution=resolution_mm,
                )
                used_nose_circle = True
                continue

            indices = _indices_between(z_values, start_z, end_z, half_width)
            for index in indices:
                if abs(end_z - start_z) <= 1e-12:
                    diameter = abs(end_x)
                else:
                    fraction = min(max((z_values[index] - start_z) / (end_z - start_z), 0), 1)
                    diameter = abs(start_x + fraction * (end_x - start_x))
                radius = diameter / 2
                if cut_side == "external":
                    outer[index] = max(min(outer[index], radius), inner[index])
                else:
                    inner[index] = min(max(inner[index], radius), outer[index])

    slice_length = (z_max_mm - z_min_mm) / (count - 1)
    initial_areas = [
        pi * max(out_radius * out_radius - in_radius * in_radius, 0)
        for out_radius, in_radius in zip(initial_outer, initial_inner)
    ]
    initial_volume = sum(
        (initial_areas[index] + initial_areas[index + 1]) * 0.5 * slice_length
        for index in range(count - 1)
    )
    areas = [pi * max(out_radius * out_radius - in_radius * in_radius, 0) for out_radius, in_radius in zip(outer, inner)]
    remaining_volume = sum((areas[index] + areas[index + 1]) * 0.5 * slice_length for index in range(count - 1))
    removed_volume = max(initial_volume - remaining_volume, 0)
    removal_percent = removed_volume / initial_volume * 100 if initial_volume else 0
    return TurningSimulationResult(
        approximation=(
            "thread_root_envelope" if used_thread_envelope
            else "mixed_centerline_and_nose_circle" if used_nose_circle
            else "tool_centerline"
        ),
        status="completed",
        resolution_mm=resolution_mm,
        samples=[
            TurningStockSample(z=z_value, outer_radius=out_radius, inner_radius=in_radius)
            for z_value, out_radius, in_radius in zip(z_values, outer, inner)
        ],
        metrics=TurningSimulationMetrics(
            initial_volume_mm3=round(initial_volume, 6),
            remaining_volume_mm3=round(remaining_volume, 6),
            removed_volume_mm3=round(removed_volume, 6),
            removal_percent=round(removal_percent, 6),
        ),
        warnings=[
            "螺纹循环按牙底旋转包络近似去除材料；未表达真实螺旋牙型和刀片成形轮廓。"
            if used_thread_envelope
            else "精车路径已扫掠圆形刀尖；粗车和专用循环仍使用中心线近似。"
            if used_nose_circle
            else "当前为刀具中心线近似，尚未扫掠刀尖圆弧和完整刀片几何",
            "尚未校核刀片象限、刀杆姿态和机床级干涉。",
            *( ["本阶段从上一通道转换后的离散材料状态继续仿真。"] if initial_samples else [] ),
        ],
    )
