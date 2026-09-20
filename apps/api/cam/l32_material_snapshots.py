"""Build clean cumulative L32 IPW snapshots with bounded OCC booleans."""

from __future__ import annotations

import json
from math import cos, pi, sin
import os
from pathlib import Path

import FreeCAD as App
import MeshPart
import Part


def export_snapshot(shape, path):
    mesh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=0.065,
        AngularDeflection=0.42,
        Relative=False,
    )
    mesh.write(str(path))


def bounds_box(shape, axis_index, ratio):
    bounds = shape.BoundBox
    lower = [bounds.XMin - 0.1, bounds.YMin - 0.1, bounds.ZMin - 0.1]
    sizes = [bounds.XLength + 0.2, bounds.YLength + 0.2, bounds.ZLength + 0.2]
    sizes[axis_index] *= max(0.0, min(1.0, ratio))
    return Part.makeBox(*sizes, App.Vector(*lower))


def remaining_box(shape, axis_index, ratio):
    bounds = shape.BoundBox
    lower = [bounds.XMin - 0.1, bounds.YMin - 0.1, bounds.ZMin - 0.1]
    sizes = [bounds.XLength + 0.2, bounds.YLength + 0.2, bounds.ZLength + 0.2]
    lower[axis_index] += sizes[axis_index] * ratio
    sizes[axis_index] *= 1 - ratio
    return Part.makeBox(*sizes, App.Vector(*lower))


def split_volume(shape, rough_fraction=0.84):
    if shape.isNull() or shape.Volume <= 1e-8:
        return shape, shape
    bounds = shape.BoundBox
    lengths = [bounds.XLength, bounds.YLength, bounds.ZLength]
    axis_index = max(range(3), key=lengths.__getitem__)
    rough = shape.common(bounds_box(shape, axis_index, rough_fraction))
    return rough, shape.cut(rough)


def pocket_fill(draft):
    lower = draft["floor_bounds"]["minimum"]
    upper = draft["floor_bounds"]["maximum"]
    access = draft["access_direction"]
    depth = float(draft["pocket_depth_mm"])
    sizes = [
        max(float(upper["x"]) - float(lower["x"]), 1e-5),
        max(float(upper["y"]) - float(lower["y"]), 1e-5),
        max(float(upper["z"]) - float(lower["z"]), 1e-5),
    ]
    origin = [float(lower["x"]), float(lower["y"]), float(lower["z"])]
    values = (float(access["x"]), float(access["y"]), float(access["z"]))
    axis_index = max(range(3), key=lambda index: abs(values[index]))
    sizes[axis_index] = depth
    if values[axis_index] < 0:
        origin[axis_index] -= depth
    return Part.makeBox(*sizes, App.Vector(*origin))


def convex_hull(points):
    unique = sorted(set((round(float(x), 9), round(float(y), 9)) for x, y in points))
    if len(unique) <= 2:
        return unique

    def cross(origin, left, right):
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (right[0] - origin[0])

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def clip_positive_radius(polygon):
    if not polygon:
        return []
    output = []
    previous = polygon[-1]
    previous_inside = previous[1] >= 0
    for current in polygon:
        current_inside = current[1] >= 0
        if current_inside != previous_inside:
            delta = current[1] - previous[1]
            ratio = -previous[1] / delta if abs(delta) > 1e-12 else 0
            output.append((previous[0] + (current[0] - previous[0]) * ratio, 0.0))
        if current_inside:
            output.append(current)
        previous, previous_inside = current, current_inside
    return output


def turning_segment_sweep(segment, origin, axis, radial_direction):
    start = (float(segment["start_z"]), float(segment["start_radius"]))
    end = (float(segment["end_z"]), float(segment["end_radius"]))
    axial_width = float(segment.get("axial_width", 0) or 0)
    if axial_width > 0:
        center_z = (start[0] + end[0]) / 2 + float(segment.get("axial_center_offset", 0) or 0)
        minimum_radius = max(0.0, min(start[1], end[1]))
        maximum_radius = max(abs(start[1]), abs(end[1]))
        outer = Part.makeCylinder(
            maximum_radius, axial_width,
            origin + axis * (center_z - axial_width / 2), axis,
        )
        if minimum_radius <= 1e-8:
            return outer
        inner = Part.makeCylinder(
            minimum_radius, axial_width,
            origin + axis * (center_z - axial_width / 2), axis,
        )
        return outer.cut(inner)
    tool_radius = float(segment.get("tool_radius", 0) or 0)
    if bool(segment.get("radial_stock_envelope", False)):
        minimum_z = min(start[0], end[0])
        maximum_z = max(start[0], end[0])
        if maximum_z - minimum_z <= 1e-8:
            minimum_z -= max(tool_radius, 0.05)
            maximum_z += max(tool_radius, 0.05)
        minimum_radius = max(0.0, min(start[1], end[1]))
        maximum_radius = max(abs(start[1]), abs(end[1]))
        outer = Part.makeCylinder(
            maximum_radius, maximum_z - minimum_z,
            origin + axis * minimum_z, axis,
        )
        if minimum_radius <= 1e-8:
            return outer
        inner = Part.makeCylinder(
            minimum_radius, maximum_z - minimum_z,
            origin + axis * minimum_z, axis,
        )
        return outer.cut(inner)
    if tool_radius <= 0:
        return None
    samples = []
    for center_z, center_radius in (start, end):
        for index in range(24):
            angle = 2 * pi * index / 24
            samples.append((
                center_z + tool_radius * cos(angle),
                center_radius + tool_radius * sin(angle),
            ))
    polygon = clip_positive_radius(convex_hull(samples))
    if len(polygon) < 3:
        return None
    vertices = [
        origin + axis * z_value + radial_direction * radius
        for z_value, radius in polygon
    ]
    wire = Part.makePolygon([*vertices, vertices[0]])
    return Part.Face(wire).revolve(origin, axis, 360)


def main():
    source_path, output_dir, stages_path = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    target = Part.read(source_path).Solids[0]
    payload = json.loads(Path(stages_path).read_text(encoding="utf-8"))
    stages = payload["stages"]
    context = payload["context"]
    frame_count = max(2, int(payload.get("frame_count", 20)))
    filename_prefix = str(payload.get("filename_prefix", "l32-material"))
    animation_operation_id = payload.get("animation_operation_id")
    precise_validation = bool(payload.get("precise_validation", False))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    axis = App.Vector(*[float(context["axis_direction"][key]) for key in ("x", "y", "z")])
    axis.normalize()
    origin = App.Vector(*[float(context["axis_origin"][key]) for key in ("x", "y", "z")])
    reference = App.Vector(1, 0, 0) if abs(axis.x) < 0.9 else App.Vector(0, 1, 0)
    radial_direction = axis.cross(reference)
    radial_direction.normalize()
    start = origin + axis * float(context["region_min"])
    regional_stock = Part.makeCylinder(
        float(context["stock_radius"]),
        float(context["region_max"]) - float(context["region_min"]),
        start,
        axis,
    )
    front_min = float(context["front_region_min"])
    front_max = float(context["front_region_max"])
    front_length = front_max - front_min
    front_start = origin + axis * front_min
    face_cap = Part.makeCylinder(
        float(context["stock_radius"]),
        float(context["face_overhang"]),
        origin + axis * front_max,
        axis,
    )
    back_face = context.get("back_face", {})
    back_face_minimum = float(back_face.get("minimum", 0))
    back_face_maximum = float(back_face.get("maximum", 0))
    back_face_cap = (
        Part.makeCylinder(
            float(context["stock_radius"]),
            back_face_maximum - back_face_minimum,
            origin + axis * back_face_minimum,
            axis,
        )
        if back_face_maximum > back_face_minimum + 1e-8 else None
    )
    front_stock = Part.makeCylinder(float(context["stock_radius"]), front_length, front_start, axis)
    rough_core = Part.makeCylinder(float(context["front_rough_radius"]), front_length, front_start, axis)
    groove_sweeps = {}
    groove_shapes = {}
    for operation_id, groove in context.get("grooves", {}).items():
        length = float(groove["maximum"]) - float(groove["minimum"])
        start_point = origin + axis * float(groove["minimum"])
        outer = Part.makeCylinder(float(groove["radius"]), length, start_point, axis)
        inner = Part.makeCylinder(float(groove["floor_radius"]), length, start_point, axis)
        groove_sweeps[operation_id] = outer.cut(inner)
        groove_shapes[operation_id] = groove_sweeps[operation_id].cut(target)
    cutoff_sweeps = {
        operation_id: Part.makeCylinder(
            float(context["stock_radius"]),
            float(cutoff["maximum"]) - float(cutoff["minimum"]),
            origin + axis * float(cutoff["minimum"]),
            axis,
        )
        for operation_id, cutoff in context.get("cutoffs", {}).items()
    }
    cutoff_shapes = {
        operation_id: sweep.cut(target)
        for operation_id, sweep in cutoff_sweeps.items()
    }
    groove_fill = Part.makeCompound(list(groove_shapes.values())) if groove_shapes else None
    front_removal = front_stock.cut(target)
    if groove_fill is not None:
        front_removal = front_removal.cut(groove_fill)
    front_rough = front_removal.cut(rough_core)
    front_finish = front_removal.cut(front_rough)

    pocket_shapes = {
        feature_id: pocket_fill(draft)
        for feature_id, draft in context.get("pockets", {}).items()
    }
    drill_shapes = {
        operation_id: Part.makeCylinder(
            float(drill["diameter"]) / 2,
            float(drill["depth"]),
            App.Vector(*[float(value) for value in drill["entry"]]),
            App.Vector(*[float(value) for value in drill["axis"]]),
        )
        for operation_id, drill in context.get("drills", {}).items()
    }
    protected_pockets = list(pocket_shapes.values())
    protected_target = target.fuse(protected_pockets).removeSplitter() if protected_pockets else target
    exterior_removal = regional_stock.cut(protected_target)
    exterior_rough, exterior_finish = split_volume(exterior_removal)
    back_exterior_shapes = {
        operation_id: Part.makeCylinder(
            float(context["stock_radius"]),
            float(region["maximum"]) - float(region["minimum"]),
            origin + axis * float(region["minimum"]),
            axis,
        ).cut(target)
        for operation_id, region in context.get("back_regions", {}).items()
    }
    back_exterior_partitions = {
        operation_id: split_volume(shape)
        for operation_id, shape in back_exterior_shapes.items()
    }
    pocket_partitions = {
        feature_id: split_volume(shape.cut(target))
        for feature_id, shape in pocket_shapes.items()
    }

    stage_kinds = {(stage["kind"], stage.get("feature_id"), stage["rough"]) for stage in stages}
    stage_volumes = []
    stage_sweeps = []
    for stage in stages:
        if stage["kind"] == "face":
            if stage.get("workpiece_side") == "back":
                if back_face_cap is None:
                    stage_volumes.append(Part.Shape())
                    stage_sweeps.append(None)
                else:
                    stage_volumes.append(back_face_cap.cut(target))
                    stage_sweeps.append(back_face_cap)
            else:
                stage_volumes.append(face_cap.cut(target))
                stage_sweeps.append(face_cap)
        elif stage["kind"] == "front":
            paired = ("front", None, not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (front_rough if stage["rough"] else front_finish) if paired else front_removal
            )
            stage_sweeps.append(None)
        elif stage["kind"] == "groove":
            stage_volumes.append(groove_shapes[stage["operation_id"]])
            stage_sweeps.append(groove_sweeps[stage["operation_id"]])
        elif stage["kind"] == "cutoff":
            stage_volumes.append(cutoff_shapes[stage["operation_id"]])
            stage_sweeps.append(cutoff_sweeps[stage["operation_id"]])
        elif stage["kind"] == "exterior":
            paired = ("exterior", None, not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (exterior_rough if stage["rough"] else exterior_finish) if paired else exterior_removal
            )
            stage_sweeps.append(None)
        elif stage["kind"] == "back_exterior":
            pair = back_exterior_partitions[stage["operation_id"]]
            paired = ("back_exterior", None, not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (pair[0] if stage["rough"] else pair[1]) if paired
                else back_exterior_shapes[stage["operation_id"]]
            )
            stage_sweeps.append(None)
        elif stage["kind"] == "drill":
            stage_volumes.append(drill_shapes[stage["operation_id"]])
            stage_sweeps.append(drill_shapes[stage["operation_id"]])
        else:
            pair = pocket_partitions[stage["feature_id"]]
            paired = ("pocket", stage["feature_id"], not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (pair[0] if stage["rough"] else pair[1]) if paired
                else pocket_shapes[stage["feature_id"]].cut(target)
            )
            stage_sweeps.append(None)
    manifests = []
    previous_end_volume = None
    previous_end_shape = None
    previous_end_file = None
    for stage_index, (stage, removal, cutter_sweep) in enumerate(zip(stages, stage_volumes, stage_sweeps)):
        emit_stage = animation_operation_id is None or stage["operation_id"] == animation_operation_id
        stage_frame_count = frame_count if emit_stage else 2
        files = []
        volumes = []
        continuity_delta = 0.0
        validation_level = str(stage.get("validation_level", "geometric_draft"))
        blocking_reasons = [str(item) for item in stage.get("blocking_reasons", [])]
        cutter_sweep_removed_volume = None
        unreachable_removal_volume = None
        overcut_volume = None
        target_retained = True
        empty_removal = removal.isNull() or removal.Volume <= 1e-8
        if not precise_validation:
            # Fast playback intentionally follows the original L32 material
            # animation path: interpolate the planned removal volume without
            # constructing or validating per-segment cutter sweep solids.
            validation_level = "geometric_draft"
            blocking_reasons = []
        elif empty_removal and validation_level == "toolpath_sweep_candidate":
            validation_level = "geometric_draft"
            blocking_reasons.append("计划工序没有可供真实刀路去除的实体材料")
        elif validation_level == "toolpath_sweep_candidate":
            cutter_sweeps = [
                shape for shape in (
                    turning_segment_sweep(segment, origin, axis, radial_direction)
                    for segment in stage.get("toolpath_sweep_segments", [])
                )
                if shape is not None and not shape.isNull()
            ]
            if not cutter_sweeps:
                validation_level = "geometric_draft"
                blocking_reasons.append("真实刀路未形成有效刀具扫掠实体")
            else:
                unreachable = removal
                for sweep in cutter_sweeps:
                    unreachable = unreachable.cut(sweep)
                cutter_sweep_removed_volume = max(0.0, removal.Volume - unreachable.Volume)
                unreachable_removal_volume = max(0.0, unreachable.Volume)
                overcut_volume = max(sweep.common(target).Volume for sweep in cutter_sweeps)
                target_retained = overcut_volume <= 0.002
                if unreachable_removal_volume > 0.002:
                    blocking_reasons.append("真实刀路扫掠未覆盖全部计划去除区域")
                if not target_retained:
                    blocking_reasons.append("真实刀路扫掠切入目标保留实体")
                validation_level = "toolpath_sweep_verified" if not blocking_reasons else "geometric_draft"
        elif validation_level == "cutter_envelope_verified":
            if cutter_sweep is None or cutter_sweep.isNull():
                blocking_reasons.append("刀具包络实体不可用")
            else:
                cutter_sweep_removed_volume = removal.common(cutter_sweep).Volume
                unreachable_removal_volume = removal.cut(cutter_sweep).Volume
                overcut_volume = cutter_sweep.common(target).Volume
                target_retained = overcut_volume <= 0.002
                if unreachable_removal_volume > 0.002:
                    blocking_reasons.append("计划去除区域超出刀具包络")
                if not target_retained:
                    blocking_reasons.append("刀具包络切入目标保留实体")
        radial_limits = None
        if stage["kind"] == "face":
            radial_limits = (0.0, float(context["stock_radius"]), front_max, front_max + float(context["face_overhang"]))
        elif stage["kind"] == "front":
            paired = ("front", None, not stage["rough"]) in stage_kinds
            minimum = float(context["front_rough_radius"]) if paired and stage["rough"] else float(context["front_floor_radius"])
            maximum = float(context["front_rough_radius"]) if paired and not stage["rough"] else float(context["stock_radius"])
            radial_limits = (minimum, maximum, front_min, front_max)
        elif stage["kind"] == "groove":
            groove = context["grooves"][stage["operation_id"]]
            radial_limits = (
                float(groove["floor_radius"]), float(groove["radius"]),
                float(groove["minimum"]), float(groove["maximum"]),
            )
        elif stage["kind"] == "cutoff":
            cutoff = context["cutoffs"][stage["operation_id"]]
            radial_limits = (
                0.0, float(context["stock_radius"]),
                float(cutoff["minimum"]), float(cutoff["maximum"]),
            )
        bounds = removal.BoundBox if not empty_removal else None
        lengths = [bounds.XLength, bounds.YLength, bounds.ZLength] if bounds is not None else [0, 0, 0]
        axis_index = max(range(3), key=lengths.__getitem__)
        future = [
            shape for shape in stage_volumes[stage_index + 1:]
            if not shape.isNull() and shape.Volume > 1e-8
        ]
        previous_remaining = removal
        for frame in range(stage_frame_count):
            ratio = frame / (stage_frame_count - 1)
            if empty_removal:
                remaining = None
            elif frame == 0:
                remaining = removal
            elif frame == stage_frame_count - 1:
                remaining = None
            else:
                if radial_limits is not None:
                    min_radius, max_radius, min_z, max_z = radial_limits
                    keep_radius = max_radius - (max_radius - min_radius) * ratio
                    keep_cylinder = Part.makeCylinder(
                        keep_radius, max_z - min_z + 0.2,
                        origin + axis * (min_z - 0.1), axis,
                    )
                    candidates = [removal.common(keep_cylinder)]
                else:
                    candidates = [
                        removal.cut(bounds_box(removal, axis_index, ratio)),
                        removal.common(remaining_box(removal, axis_index, ratio)),
                    ]
                valid = [
                    candidate for candidate in candidates
                    if not candidate.isNull() and 1e-8 < candidate.Volume <= previous_remaining.Volume + 1e-8
                ]
                remaining = max(valid, key=lambda candidate: candidate.Volume) if valid else previous_remaining
            if remaining is not None:
                previous_remaining = remaining
            uncut = [*([remaining] if remaining is not None and remaining.Volume > 1e-8 else []), *future]
            # The fill volumes are already clipped against the target and
            # disjointly partitioned. OCC fuse at their shared tangencies can
            # corrupt volume or erase a thin pocket; a compound preserves each
            # valid solid while the renderer displays one tessellated mesh.
            frame_shape = Part.makeCompound([target, *uncut]) if uncut else target
            volume = frame_shape.Volume
            if frame_shape.isNull() or volume < target.Volume - 0.001:
                raise ValueError(f"{stage['operation_id']} frame {frame}: invalid material volume {volume}")
            if volumes and volume > volumes[-1] + 0.002:
                raise ValueError(f"{stage['operation_id']} frame {frame}: material volume increased {volumes[-1]} -> {volume}")
            if frame == 0 and previous_end_volume is not None and abs(volume - previous_end_volume) > 0.002:
                raise ValueError(f"{stage['operation_id']}: previous operation's final material differs")
            if precise_validation and frame == 0 and previous_end_shape is not None:
                missing = previous_end_shape.cut(frame_shape).Volume
                added = frame_shape.cut(previous_end_shape).Volume
                continuity_delta = max(missing, added)
                if continuity_delta > 0.002:
                    raise ValueError(
                        f"{stage['operation_id']}: previous operation's material shape differs "
                        f"by {continuity_delta:.6f} mm3"
                    )
            # A two-frame PartState chain has N+1 unique solids, not 2N.
            # Reuse the preceding operation's final mesh as this operation's
            # input mesh. Besides preserving identity across the transition,
            # this avoids tessellating and writing the same OCC shape twice.
            if emit_stage:
                if stage_frame_count == 2 and frame == 0 and previous_end_file is not None:
                    name = previous_end_file
                else:
                    name = f"{filename_prefix}-{stage['operation_id']}-{frame:02d}.stl"
                    export_snapshot(frame_shape, output / name)
                files.append(name)
            volumes.append(round(volume, 6))
        previous_end_volume = volumes[-1]
        previous_end_shape = frame_shape
        previous_end_file = files[-1] if files else None
        if not emit_stage:
            continue
        manifests.append({
            "operation_id": stage["operation_id"],
            "files": files,
            "volumes_mm3": volumes,
            "input_state_volume_mm3": volumes[0],
            "output_state_volume_mm3": volumes[-1],
            "removed_volume_mm3": round(volumes[0] - volumes[-1], 6),
            "previous_state_shape_delta_mm3": round(continuity_delta, 6),
            "validation_level": validation_level,
            "cutter_sweep_removed_volume_mm3": (
                round(cutter_sweep_removed_volume, 6) if cutter_sweep_removed_volume is not None else None
            ),
            "unreachable_removal_volume_mm3": (
                round(unreachable_removal_volume, 6) if unreachable_removal_volume is not None else None
            ),
            "overcut_volume_mm3": round(overcut_volume, 6) if overcut_volume is not None else None,
            "target_retained": target_retained,
            "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        })
        if animation_operation_id is not None:
            break
    print("CNC_L32_MATERIAL " + json.dumps({"operations": manifests}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
