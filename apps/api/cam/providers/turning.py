from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Literal

from app.models import Operation
from app.rotational_features import RotationalProfile, RotationalProfilePoint
from app.turning_compensation import compensate_profile_for_nose
from app.toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, ToolpathTrace


SUPPORTED_OPERATION_TYPES = {
    "turn_facing", "turn_od_roughing", "turn_od_finishing",
    "turn_id_roughing", "turn_id_finishing", "turn_grooving",
    "turn_threading", "axial_drilling", "axial_tapping", "turn_cutoff",
}


@dataclass(frozen=True)
class TurningContext:
    machine_snapshot_hash: str
    stock_radius_mm: float
    initial_bore_radius_mm: float = 0.0
    radial_clearance_mm: float = 2.0
    axial_clearance_mm: float = 2.0
    channel_id: str = "main"
    cut_direction: Literal["negative_z", "positive_z"] = "negative_z"
    generator_version: str = "0.1.0"

    def __post_init__(self) -> None:
        if len(self.machine_snapshot_hash) != 64:
            raise ValueError("machine_snapshot_hash must contain 64 characters")
        if self.stock_radius_mm <= 0:
            raise ValueError("stock_radius_mm must be positive")
        if self.initial_bore_radius_mm < 0 or self.initial_bore_radius_mm >= self.stock_radius_mm:
            raise ValueError("initial_bore_radius_mm must be non-negative and smaller than stock radius")
        if self.radial_clearance_mm <= 0 or self.axial_clearance_mm <= 0:
            raise ValueError("turning clearances must be positive")


class TurningProvider:
    id = "turning"
    version = "0.1.0"

    def supports(self, operation: Operation) -> bool:
        return operation.type in SUPPORTED_OPERATION_TYPES

    def generate(
        self,
        operation: Operation,
        context: TurningContext,
        profile: RotationalProfile | None = None,
    ) -> ToolpathProgram:
        if not self.supports(operation):
            raise ValueError(f"turning provider does not support {operation.type}")
        if operation.type in {"turn_od_roughing", "turn_od_finishing"}:
            if profile is None or profile.side != "outer":
                raise ValueError(f"{operation.type} requires an outer rotational profile")
        if operation.type in {"turn_id_roughing", "turn_id_finishing"}:
            if profile is None or profile.side != "inner":
                raise ValueError(f"{operation.type} requires an inner rotational profile")
            if context.initial_bore_radius_mm <= context.radial_clearance_mm:
                raise ValueError("inner turning requires an initial bore larger than radial clearance")

        builder = _CommandBuilder(operation, context)
        builder.add("select_tool", parameters={"tool_id": operation.tool.id})
        spindle_mode = str(operation.parameters.get("spindle_mode", "constant_surface_speed"))
        if spindle_mode == "constant_surface_speed":
            builder.add("set_constant_surface_speed", parameters={
                "surface_speed_m_min": _positive(operation, "cutting_speed_m_min"),
                "maximum_spindle_rpm": _positive(operation, "maximum_spindle_rpm"),
            })
        elif spindle_mode == "constant_rpm":
            builder.add("set_rpm", parameters={"rpm": _positive(operation, "spindle_rpm")})
        else:
            raise ValueError(f"unsupported spindle mode: {spindle_mode}")
        builder.add("set_feed_per_revolution", parameters={
            "feed_mm_rev": _positive(operation, "feed_per_revolution_mm"),
        })
        builder.add("spindle_start", parameters={
            "spindle_id": operation.spindle_id or str(operation.parameters.get("spindle_id", "main")),
            "direction": str(operation.parameters.get("spindle_direction", "clockwise")),
        })

        if operation.type == "turn_facing":
            _generate_facing(builder)
        elif operation.type == "turn_od_roughing":
            assert profile is not None
            _generate_od_roughing(builder, profile)
        elif operation.type == "turn_od_finishing":
            assert profile is not None
            _generate_od_finishing(builder, profile)
        elif operation.type == "turn_id_roughing":
            assert profile is not None
            _generate_id_roughing(builder, profile)
        elif operation.type == "turn_id_finishing":
            assert profile is not None
            _generate_id_finishing(builder, profile)
        elif operation.type == "turn_grooving":
            _generate_grooving(builder)
        elif operation.type == "turn_threading":
            _generate_threading(builder)
        elif operation.type == "axial_drilling":
            _generate_axial_cycle(builder, tapping=False)
        elif operation.type == "axial_tapping":
            _generate_axial_cycle(builder, tapping=True)
        elif operation.type == "turn_cutoff":
            _generate_cutoff(builder)

        builder.add("spindle_stop", parameters={
            "spindle_id": operation.spindle_id or str(operation.parameters.get("spindle_id", "main")),
        })
        return ToolpathProgram(
            coordinate_convention="diameter-x_z",
            machine_snapshot_hash=context.machine_snapshot_hash,
            plan_revision=max(operation.definition_version, 1),
            channels=[ToolpathChannel(id=context.channel_id, commands=builder.commands)],
        )


class _CommandBuilder:
    def __init__(self, operation: Operation, context: TurningContext):
        self.operation = operation
        self.context = context
        self.commands: list[ToolpathCommand] = []

    def add(
        self, command_type: str, *, axes: dict[str, float] | None = None,
        parameters: dict[str, float | int | str | bool] | None = None,
        safety_requirements: list[str] | None = None,
    ) -> None:
        self.commands.append(ToolpathCommand(
            sequence=(len(self.commands) + 1) * 10,
            type=command_type,
            channel_id=self.context.channel_id,
            operation_id=self.operation.id,
            axes=axes or {},
            parameters=parameters or {},
            safety_requirements=safety_requirements or [],
            trace=ToolpathTrace(
                feature_ids=self.operation.feature_ids,
                source=TurningProvider.id,
                generator_version=self.context.generator_version,
            ),
        ))

    @property
    def clearance_diameter(self) -> float:
        return 2 * (self.context.stock_radius_mm + self.context.radial_clearance_mm)

    @property
    def bore_clearance_diameter(self) -> float:
        return 2 * (self.context.initial_bore_radius_mm - self.context.radial_clearance_mm)


def _number(operation: Operation, key: str, default: float | None = None) -> float:
    raw = operation.parameters.get(key, default)
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{operation.type} requires numeric parameter {key}")
    return float(raw)


def _positive(operation: Operation, key: str) -> float:
    value = _number(operation, key)
    if value <= 0:
        raise ValueError(f"{operation.type} parameter {key} must be positive")
    return value


def _ordered_points(profile: RotationalProfile, direction: str) -> list[RotationalProfilePoint]:
    return sorted(profile.points, key=lambda item: item.z, reverse=direction == "negative_z")


def _approach_z(value: float, context: TurningContext) -> float:
    sign = 1 if context.cut_direction == "negative_z" else -1
    return value + sign * context.axial_clearance_mm


def _generate_facing(builder: _CommandBuilder) -> None:
    face_z = _number(builder.operation, "face_z_mm", 0)
    center_overtravel = _number(builder.operation, "center_overtravel_mm", 0.2)
    if center_overtravel < 0:
        raise ValueError("center_overtravel_mm cannot be negative")
    builder.add(
        "rapid_move", axes={"X": builder.clearance_diameter, "Z": _approach_z(face_z, builder.context)},
        safety_requirements=["work_spindle_running", "tool_offset_active"],
    )
    builder.add("feed_move", axes={"Z": face_z}, parameters={"cut_side": "external"})
    builder.add("feed_move", axes={"X": -2 * center_overtravel}, parameters={
        "cut_side": "facing",
        "retain_direction": "negative_z" if builder.context.cut_direction == "negative_z" else "positive_z",
    })
    builder.add("rapid_move", axes={"X": builder.clearance_diameter})
    builder.add("rapid_move", axes={"Z": _approach_z(face_z, builder.context)})


def _generate_od_finishing(builder: _CommandBuilder, profile: RotationalProfile) -> None:
    allowance = _number(builder.operation, "radial_allowance_mm", 0)
    if allowance < 0:
        raise ValueError("radial_allowance_mm cannot be negative")
    nose_radius = float(builder.operation.tool.nose_radius_mm or 0)
    points = sorted(
        compensate_profile_for_nose(
            profile, nose_radius_mm=nose_radius, allowance_mm=allowance,
        ),
        key=lambda item: item.z,
        reverse=builder.context.cut_direction == "negative_z",
    )
    first = points[0]
    builder.add(
        "rapid_move",
        axes={"X": builder.clearance_diameter, "Z": _approach_z(first.z, builder.context)},
        safety_requirements=["work_spindle_running", "tool_offset_active"],
    )
    nose_parameters = {
        "cut_side": "external", "position_role": "nose_center",
        "tool_nose_radius_mm": nose_radius,
    }
    builder.add("feed_move", axes={"X": 2 * first.radius, "Z": first.z}, parameters=nose_parameters)
    for point in points[1:]:
        builder.add("feed_move", axes={"X": 2 * point.radius, "Z": point.z}, parameters=nose_parameters)
    builder.add("rapid_move", axes={"X": builder.clearance_diameter})
    builder.add("rapid_move", axes={"Z": _approach_z(first.z, builder.context)})


def _threshold_intervals(
    points: list[RotationalProfilePoint], threshold_radius: float, allowance: float,
) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for left, right in zip(points, points[1:]):
        left_delta = left.radius + allowance - threshold_radius
        right_delta = right.radius + allowance - threshold_radius
        if left_delta <= 0 and right_delta <= 0:
            segment = (left.z, right.z)
        elif left_delta * right_delta < 0:
            fraction = -left_delta / (right_delta - left_delta)
            crossing = left.z + fraction * (right.z - left.z)
            segment = (left.z, crossing) if left_delta <= 0 else (crossing, right.z)
        else:
            continue
        start, end = sorted(segment)
        if end - start <= 1e-9:
            continue
        if intervals and abs(intervals[-1][1] - start) <= 1e-7:
            intervals[-1] = (intervals[-1][0], end)
        else:
            intervals.append((start, end))
    return intervals


def _generate_od_roughing(builder: _CommandBuilder, profile: RotationalProfile) -> None:
    allowance = _number(builder.operation, "radial_allowance_mm", 0.3)
    depth = _positive(builder.operation, "depth_of_cut_mm")
    if allowance < 0:
        raise ValueError("radial_allowance_mm cannot be negative")
    points = sorted(profile.points, key=lambda item: item.z)
    minimum_target = min(item.radius for item in points) + allowance
    if builder.context.stock_radius_mm <= minimum_target + 1e-9:
        raise ValueError("roughing profile does not remove material from the configured stock")
    radius = max(builder.context.stock_radius_mm - depth, minimum_target)
    generated_intervals = 0
    pass_count = 0
    while radius >= minimum_target - 1e-9:
        pass_count += 1
        if pass_count > 10_000:
            raise ValueError("roughing pass count exceeds the safety limit")
        intervals = _threshold_intervals(points, radius, allowance)
        for minimum_z, maximum_z in intervals:
            start_z, end_z = (
                (maximum_z, minimum_z)
                if builder.context.cut_direction == "negative_z"
                else (minimum_z, maximum_z)
            )
            builder.add(
                "rapid_move",
                axes={"X": builder.clearance_diameter, "Z": _approach_z(start_z, builder.context)},
                safety_requirements=["work_spindle_running", "tool_offset_active"],
            )
            builder.add("feed_move", axes={"X": 2 * radius, "Z": start_z}, parameters={"cut_side": "external"})
            builder.add("feed_move", axes={"X": 2 * radius, "Z": end_z}, parameters={"cut_side": "external"})
            builder.add("rapid_move", axes={"X": builder.clearance_diameter})
            generated_intervals += 1
        if radius <= minimum_target + 1e-9:
            break
        radius = max(radius - depth, minimum_target)
    if generated_intervals == 0:
        raise ValueError("roughing profile does not remove material from the configured stock")


def _threshold_intervals_above(
    points: list[RotationalProfilePoint], threshold_radius: float, allowance: float,
) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for left, right in zip(points, points[1:]):
        left_delta = threshold_radius - (left.radius - allowance)
        right_delta = threshold_radius - (right.radius - allowance)
        if left_delta <= 0 and right_delta <= 0:
            segment = (left.z, right.z)
        elif left_delta * right_delta < 0:
            fraction = -left_delta / (right_delta - left_delta)
            crossing = left.z + fraction * (right.z - left.z)
            segment = (left.z, crossing) if left_delta <= 0 else (crossing, right.z)
        else:
            continue
        start, end = sorted(segment)
        if end - start <= 1e-9:
            continue
        if intervals and abs(intervals[-1][1] - start) <= 1e-7:
            intervals[-1] = (intervals[-1][0], end)
        else:
            intervals.append((start, end))
    return intervals


def _generate_id_roughing(builder: _CommandBuilder, profile: RotationalProfile) -> None:
    allowance = _number(builder.operation, "radial_allowance_mm", 0.25)
    depth = _positive(builder.operation, "depth_of_cut_mm")
    if allowance < 0:
        raise ValueError("radial_allowance_mm cannot be negative")
    points = sorted(profile.points, key=lambda item: item.z)
    maximum_target = max(item.radius for item in points) - allowance
    radius = builder.context.initial_bore_radius_mm + depth
    generated_intervals = 0
    pass_count = 0
    while radius <= maximum_target + 1e-9:
        pass_count += 1
        if pass_count > 10_000:
            raise ValueError("inner roughing pass count exceeds the safety limit")
        for minimum_z, maximum_z in _threshold_intervals_above(points, radius, allowance):
            start_z, end_z = (
                (maximum_z, minimum_z)
                if builder.context.cut_direction == "negative_z"
                else (minimum_z, maximum_z)
            )
            builder.add(
                "rapid_move",
                axes={"X": builder.bore_clearance_diameter, "Z": _approach_z(start_z, builder.context)},
                safety_requirements=["initial_bore_confirmed", "work_spindle_running", "tool_offset_active"],
            )
            builder.add("feed_move", axes={"X": 2 * radius, "Z": start_z}, parameters={"cut_side": "internal"})
            builder.add("feed_move", axes={"X": 2 * radius, "Z": end_z}, parameters={"cut_side": "internal"})
            builder.add("rapid_move", axes={"X": builder.bore_clearance_diameter})
            generated_intervals += 1
        radius += depth
    if generated_intervals == 0:
        raise ValueError("inner roughing profile does not remove material from the configured bore")


def _generate_id_finishing(builder: _CommandBuilder, profile: RotationalProfile) -> None:
    allowance = _number(builder.operation, "radial_allowance_mm", 0)
    if allowance < 0:
        raise ValueError("radial_allowance_mm cannot be negative")
    if any(point.radius < allowance for point in profile.points):
        raise ValueError("inner finishing allowance exceeds profile radius")
    nose_radius = float(builder.operation.tool.nose_radius_mm or 0)
    points = sorted(
        compensate_profile_for_nose(
            profile, nose_radius_mm=nose_radius, allowance_mm=allowance,
        ),
        key=lambda item: item.z,
        reverse=builder.context.cut_direction == "negative_z",
    )
    first = points[0]
    builder.add(
        "rapid_move",
        axes={"X": builder.bore_clearance_diameter, "Z": _approach_z(first.z, builder.context)},
        safety_requirements=["initial_bore_confirmed", "work_spindle_running", "tool_offset_active"],
    )
    nose_parameters = {
        "cut_side": "internal", "position_role": "nose_center",
        "tool_nose_radius_mm": nose_radius,
    }
    builder.add("feed_move", axes={"X": 2 * first.radius, "Z": first.z}, parameters=nose_parameters)
    for point in points[1:]:
        builder.add("feed_move", axes={"X": 2 * point.radius, "Z": point.z}, parameters=nose_parameters)
    builder.add("rapid_move", axes={"X": builder.bore_clearance_diameter})
    builder.add("rapid_move", axes={"Z": _approach_z(first.z, builder.context)})


def _generate_threading(builder: _CommandBuilder) -> None:
    start_z = _number(builder.operation, "start_z_mm")
    end_z = _number(builder.operation, "end_z_mm")
    pitch = _positive(builder.operation, "pitch_mm")
    major = _positive(builder.operation, "major_diameter_mm")
    minor = _number(builder.operation, "minor_diameter_mm")
    pass_count_value = _positive(builder.operation, "pass_count")
    pass_count = int(pass_count_value)
    if pass_count_value != pass_count:
        raise ValueError("turn_threading parameter pass_count must be an integer")
    if minor < 0 or minor >= major:
        raise ValueError("thread minor diameter must be non-negative and smaller than major diameter")
    thread_depth = (major - minor) / 2
    reviewed_depth = _number(builder.operation, "thread_depth_mm", thread_depth)
    if abs(reviewed_depth - thread_depth) > 1e-6:
        raise ValueError("thread depth does not agree with major and minor diameters")
    builder.add(
        "rapid_move",
        axes={"X": major + 2 * builder.context.radial_clearance_mm, "Z": _approach_z(start_z, builder.context)},
        safety_requirements=["work_spindle_running", "tool_offset_active", "spindle_encoder_ready"],
    )
    for pass_index in range(1, pass_count + 1):
        cumulative_depth = thread_depth * sqrt(pass_index / pass_count)
        target_diameter = major - 2 * cumulative_depth
        builder.add("thread_cut", parameters={
            "start_z_mm": start_z, "end_z_mm": end_z, "pitch_mm": pitch,
            "major_diameter_mm": major, "minor_diameter_mm": minor,
            "pass_count": pass_count, "pass_index": pass_index,
            "cumulative_depth_mm": round(cumulative_depth, 6),
            "target_diameter_mm": round(target_diameter, 6),
            "relief_strategy": str(builder.operation.parameters.get("relief_strategy", "unreviewed")),
            "relief_width_mm": _number(builder.operation, "relief_width_mm", 0),
        }, safety_requirements=["spindle_synchronization_required"])
    builder.add("rapid_move", axes={"X": major + 2 * builder.context.radial_clearance_mm})


def _generate_axial_cycle(builder: _CommandBuilder, *, tapping: bool) -> None:
    start_z = _number(builder.operation, "start_z_mm", 0)
    depth = _positive(builder.operation, "depth_mm")
    end_z = start_z - depth if builder.context.cut_direction == "negative_z" else start_z + depth
    retract_z = _approach_z(start_z, builder.context)
    parameters: dict[str, float | int | str | bool] = {
        "start_z_mm": start_z,
        "end_z_mm": end_z,
        "retract_z_mm": retract_z,
        "spindle_id": builder.operation.spindle_id or str(builder.operation.parameters.get("spindle_id", "main")),
    }
    command_type = "tap_cycle" if tapping else "drill_cycle"
    if tapping:
        parameters["pitch_mm"] = _positive(builder.operation, "pitch_mm")
    else:
        parameters["peck_depth_mm"] = _positive(builder.operation, "peck_depth_mm")
        parameters["diameter_mm"] = builder.operation.tool.diameter_mm
        parameters["drill_tip_length_mm"] = _number(builder.operation, "drill_tip_length_mm", 0)
    builder.add(
        "rapid_move", axes={"X": 0, "Z": retract_z},
        safety_requirements=["axial_tool_alignment_confirmed", "tool_offset_active"],
    )
    builder.add(
        command_type, parameters=parameters,
        safety_requirements=["spindle_synchronization_required"] if tapping else ["chip_evacuation_confirmed"],
    )


def _generate_grooving(builder: _CommandBuilder) -> None:
    z_value = _number(builder.operation, "z_mm")
    final_diameter = _number(builder.operation, "final_diameter_mm")
    if final_diameter < 0:
        raise ValueError("final_diameter_mm cannot be negative")
    builder.add(
        "rapid_move", axes={"X": builder.clearance_diameter, "Z": z_value},
        safety_requirements=["work_spindle_running", "tool_offset_active"],
    )
    groove_width = _number(builder.operation, "groove_width_mm", builder.operation.tool.cutting_width_mm)
    if groove_width <= 0:
        raise ValueError("turn_grooving groove width must be positive")
    builder.add("feed_move", axes={"X": final_diameter, "Z": z_value}, parameters={
        "cut_side": "external", "axial_width_mm": groove_width,
    })
    builder.add("rapid_move", axes={"X": builder.clearance_diameter})


def _generate_cutoff(builder: _CommandBuilder) -> None:
    z_value = _number(builder.operation, "z_mm")
    overtravel = _number(builder.operation, "breakthrough_radius_mm", 0.1)
    if overtravel < 0:
        raise ValueError("breakthrough_radius_mm cannot be negative")
    builder.add(
        "rapid_move", axes={"X": builder.clearance_diameter, "Z": z_value},
        safety_requirements=["work_spindle_running", "part_retention_confirmed", "tool_offset_active"],
    )
    cutting_width = _number(builder.operation, "cutting_width_mm", builder.operation.tool.cutting_width_mm)
    if cutting_width <= 0:
        raise ValueError("turn_cutoff cutting width must be positive")
    builder.add("feed_move", axes={"X": -2 * overtravel, "Z": z_value}, parameters={
        "cut_side": "external", "axial_width_mm": cutting_width,
    })
    builder.add("cutoff", parameters={"part_state": "separated", "z_mm": z_value})
    builder.add("rapid_move", axes={"X": builder.clearance_diameter})
