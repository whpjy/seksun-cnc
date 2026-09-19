"""Build clean cumulative L32 IPW snapshots with bounded OCC booleans."""

from __future__ import annotations

import json
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


def main():
    source_path, output_dir, stages_path = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    target = Part.read(source_path).Solids[0]
    payload = json.loads(Path(stages_path).read_text(encoding="utf-8"))
    stages = payload["stages"]
    context = payload["context"]
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    axis = App.Vector(*[float(context["axis_direction"][key]) for key in ("x", "y", "z")])
    axis.normalize()
    origin = App.Vector(*[float(context["axis_origin"][key]) for key in ("x", "y", "z")])
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
    front_stock = Part.makeCylinder(float(context["stock_radius"]), front_length, front_start, axis)
    rough_core = Part.makeCylinder(float(context["front_rough_radius"]), front_length, front_start, axis)
    groove_shapes = {
        operation_id: Part.makeCylinder(
            float(groove["radius"]),
            float(groove["maximum"]) - float(groove["minimum"]),
            origin + axis * float(groove["minimum"]),
            axis,
        ).cut(target)
        for operation_id, groove in context.get("grooves", {}).items()
    }
    cutoff_shapes = {
        operation_id: Part.makeCylinder(
            float(context["stock_radius"]),
            float(cutoff["maximum"]) - float(cutoff["minimum"]),
            origin + axis * float(cutoff["minimum"]),
            axis,
        ).cut(target)
        for operation_id, cutoff in context.get("cutoffs", {}).items()
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
    pocket_partitions = {
        feature_id: split_volume(shape.cut(target))
        for feature_id, shape in pocket_shapes.items()
    }

    stage_kinds = {(stage["kind"], stage.get("feature_id"), stage["rough"]) for stage in stages}
    stage_volumes = []
    for stage in stages:
        if stage["kind"] == "face":
            stage_volumes.append(face_cap.cut(target))
        elif stage["kind"] == "front":
            paired = ("front", None, not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (front_rough if stage["rough"] else front_finish) if paired else front_removal
            )
        elif stage["kind"] == "groove":
            stage_volumes.append(groove_shapes[stage["operation_id"]])
        elif stage["kind"] == "cutoff":
            stage_volumes.append(cutoff_shapes[stage["operation_id"]])
        elif stage["kind"] == "exterior":
            paired = ("exterior", None, not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (exterior_rough if stage["rough"] else exterior_finish) if paired else exterior_removal
            )
        elif stage["kind"] == "drill":
            stage_volumes.append(drill_shapes[stage["operation_id"]])
        else:
            pair = pocket_partitions[stage["feature_id"]]
            paired = ("pocket", stage["feature_id"], not stage["rough"]) in stage_kinds
            stage_volumes.append(
                (pair[0] if stage["rough"] else pair[1]) if paired
                else pocket_shapes[stage["feature_id"]].cut(target)
            )
    manifests = []
    previous_end_volume = None
    for stage_index, (stage, removal) in enumerate(zip(stages, stage_volumes)):
        files = []
        volumes = []
        frame_count = 20
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
        bounds = removal.BoundBox
        lengths = [bounds.XLength, bounds.YLength, bounds.ZLength]
        axis_index = max(range(3), key=lengths.__getitem__)
        future = stage_volumes[stage_index + 1:]
        previous_remaining = removal
        for frame in range(frame_count):
            ratio = frame / (frame_count - 1)
            if frame == 0:
                remaining = removal
            elif frame == frame_count - 1:
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
            name = f"l32-material-{stage['operation_id']}-{frame:02d}.stl"
            export_snapshot(frame_shape, output / name)
            files.append(name)
            volumes.append(round(volume, 6))
        previous_end_volume = volumes[-1]
        manifests.append({"operation_id": stage["operation_id"], "files": files, "volumes_mm3": volumes})
    print("CNC_L32_MATERIAL " + json.dumps({"operations": manifests}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
