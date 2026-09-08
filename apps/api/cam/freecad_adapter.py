"""Generate editable native FreeCAD CAM jobs from an approved process plan.

The FCStd contains Path Job, ToolController and native operation proxy objects.
G-code and preview data are derived from those objects; no hand-written
Path::Feature toolpaths are used.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path as FilePath

import FreeCAD as App
import Import
import Part
import Path

# FreeCAD 1.0 moved CAM modules from the legacy PathScripts namespace into
# Path.*.  Keep the fallback so existing 0.20 developer installs remain usable.
try:
    import Path.Op.Deburr as PathDeburr
    import Path.Op.Drilling as PathDrilling
    import Path.Dressup.Tags as PathDressupHoldingTags
    import Path.Main.Job as PathJob
    import Path.Op.MillFace as PathMillFace
    import Path.Op.PocketShape as PathPocketShape
    import Path.Op.Profile as PathProfile
    import Path.Tool.Controller as PathToolController
    import Path.Post.scripts.grbl_post as grbl_post
    from Path.Tool.toolbit import ToolBit as NativeToolBit
except ImportError:
    from PathScripts import PathDeburr, PathDrilling, PathDressupHoldingTags, PathJob, PathMillFace
    from PathScripts import PathPocketShape, PathProfile, PathToolController
    from PathScripts.post import grbl_post
    NativeToolBit = None


def load_json(path):
    return json.loads(FilePath(path).read_text(encoding="utf-8"))


def cross(left, right):
    return (left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0])


def normalize(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if length < 1e-9:
        raise RuntimeError("Setup work axis must be non-zero")
    return tuple(value / length for value in vector)


def setup_frame(axis):
    work_z = normalize((float(axis["x"]), float(axis["y"]), float(axis["z"])))
    reference = (0.0, 1.0, 0.0) if abs(work_z[2]) > 0.9 else (0.0, 0.0, 1.0)
    work_x = normalize(cross(reference, work_z))
    return {"x": work_x, "y": normalize(cross(work_z, work_x)), "z": work_z}


def world_to_local(point, frame):
    values = (float(point["x"]), float(point["y"]), float(point["z"]))
    return {name: sum(values[i] * basis[i] for i in range(3)) for name, basis in frame.items()}


def local_to_world(point, frame):
    return {axis: sum(float(point[name]) * frame[name][i] for name in ("x", "y", "z"))
            for i, axis in enumerate(("x", "y", "z"))}


def transform_bounds(bounds, frame):
    corners = [world_to_local({"x": x, "y": y, "z": z}, frame)
               for x in (bounds["minimum"]["x"], bounds["maximum"]["x"])
               for y in (bounds["minimum"]["y"], bounds["maximum"]["y"])
               for z in (bounds["minimum"]["z"], bounds["maximum"]["z"])]
    return {"minimum": {a: min(p[a] for p in corners) for a in "xyz"},
            "maximum": {a: max(p[a] for p in corners) for a in "xyz"}}


def transform_features(features, frame):
    transformed = json.loads(json.dumps(features))
    for feature in transformed.values():
        feature["center"] = world_to_local(feature["center"], frame)
        if feature.get("bounds"):
            feature["bounds"] = transform_bounds(feature["bounds"], frame)
    return transformed


def transform_shape(shape, frame):
    matrix = App.Matrix()
    matrix.A11, matrix.A12, matrix.A13 = frame["x"]
    matrix.A21, matrix.A22, matrix.A23 = frame["y"]
    matrix.A31, matrix.A32, matrix.A33 = frame["z"]
    result = shape.copy()
    result.transformShape(matrix, True)
    return result


def top_face(model):
    candidates = []
    for index, face in enumerate(model.Shape.Faces):
        if type(face.Surface).__name__ != "Plane":
            continue
        u0, u1, v0, v1 = face.ParameterRange
        normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        if normal.z > 0.98:
            candidates.append((face.CenterOfMass.z, face.Area, index + 1))
    if not candidates:
        raise RuntimeError("No upward planar face is available for native CAM")
    return max(candidates, key=lambda item: (item[0], item[1]))


def set_if_present(obj, name, value):
    if hasattr(obj, name):
        try:
            obj.setExpression(name, None)
        except Exception:
            pass
        setattr(obj, name, value)


def remove_default_tools(document, job):
    for controller in list(job.Tools.Group):
        job.Tools.removeObject(controller)
        document.removeObject(controller.Name)


def make_tool_controller(job, operation):
    spec, params = operation["tool"], operation.get("parameters", {})
    if NativeToolBit is not None:
        shape_id = {
            "drill": "drill.fcstd",
            "chamfer_mill": "chamfer.fcstd",
        }.get(spec.get("kind"), "endmill.fcstd")
        bit = NativeToolBit.from_shape_id(shape_id, label=spec.get("name") or spec["id"])
        tool = bit.attach_to_doc(App.ActiveDocument, label=spec.get("name") or spec["id"])
        set_if_present(tool, "Diameter", float(spec["diameter_mm"]))
        set_if_present(tool, "CuttingEdgeHeight", float(spec.get("flute_length_mm", 20.0)))
        set_if_present(tool, "Material", "Carbide")
        if spec.get("kind") == "chamfer_mill":
            set_if_present(tool, "CuttingEdgeAngle", float(params.get("included_angle_deg", 90.0)))
    else:
        tool = Path.Tool()
        tool.Name = spec.get("name") or spec["id"]
        tool.Diameter = float(spec["diameter_mm"])
        tool.CuttingEdgeHeight = float(spec.get("flute_length_mm", 20.0))
        tool.ToolType = "Drill" if spec.get("kind") == "drill" else "EndMill"
        if spec.get("kind") == "chamfer_mill":
            tool.CuttingEdgeAngle = float(params.get("included_angle_deg", 90.0))
        tool.Material = "Carbide"
    controller = PathToolController.Create("TC_" + operation["id"], tool=tool,
        toolNumber=int(operation.get("sequence", 0) // 10 or 1), assignViewProvider=False)
    # FreeCAD Path stores these properties in mm/s; posts emit mm/min.
    controller.HorizFeed = float(params.get("feed_rate_mm_min", 300.0)) / 60.0
    controller.VertFeed = float(params.get("plunge_rate_mm_min", 90.0)) / 60.0
    controller.SpindleSpeed = float(params.get("spindle_rpm", 3000.0))
    controller.SpindleDir = "Forward"
    job.Proxy.addToolController(controller)
    return controller


def prepare_proxy(op, job):
    # FreeCAD 0.20 GUI normally fills these caches from the task panel.
    op.Proxy.job, op.Proxy.model, op.Proxy.stock = job, job.Model.Group, job.Stock


def common_parameters(op, controller, params, top_z, final_z, clearance_z):
    op.ToolController = controller
    set_if_present(op, "OpToolDiameter", controller.Tool.Diameter)
    set_if_present(op, "StartDepth", top_z)
    set_if_present(op, "FinalDepth", final_z)
    set_if_present(op, "ClearanceHeight", clearance_z)
    set_if_present(op, "SafeHeight", min(clearance_z, top_z + 2.0))
    set_if_present(op, "StepDown", max(float(params.get("step_down_mm", 1.0)), 0.05))
    set_if_present(op, "CoolantMode", "Flood" if params.get("coolant") == "flood" else "None")


def create_drilling(document, job, operation, controller, features, bounds, clearance_z):
    obj = document.addObject("Path::FeaturePython", operation["id"] + "_DRILLING")
    obj.Proxy = PathDrilling.ObjectDrilling(obj, operation["name"], job)
    prepare_proxy(obj, job)
    obj.Proxy.findAllHoles(obj)
    points, openings = [], []
    for feature_id in operation.get("feature_ids", []):
        feature = features.get(feature_id)
        if not feature:
            continue
        center = feature["center"]
        points.append(App.Vector(float(center["x"]), float(center["y"]), float(center["z"])))
        openings.append(float(center["z"]) + float(feature.get("length", 0.0)) / 2.0)
    if not points:
        return None
    params = operation.get("parameters", {})
    top_z = max(openings) if openings else float(bounds["maximum"]["z"])
    depth = max(float(params.get("depth_mm", params.get("feature_depth_mm", 1.0))), 0.05)
    common_parameters(obj, controller, params, top_z, top_z - depth, clearance_z)
    obj.Locations = points
    set_if_present(obj, "PeckEnabled", bool(params.get("peck", False)))
    set_if_present(obj, "PeckDepth", max(float(params.get("peck_depth_mm", depth / 3)), 0.1))
    set_if_present(obj, "DwellEnabled", False)
    set_if_present(obj, "RetractHeight", top_z + 1.0)
    obj.Proxy.execute(obj)
    return obj


def face_reference(model, face_number):
    return [(model, ["Face%d" % face_number])]


def create_profile(document, job, operation, controller, model, bounds, clearance_z):
    params = operation.get("parameters", {})
    face_number = top_face(model)[2]
    obj = PathProfile.Create(operation["id"] + "_PROFILE", parentJob=job)
    prepare_proxy(obj, job)
    top_z = float(bounds["maximum"]["z"])
    depth = max(float(params.get("depth_mm", top_z - float(bounds["minimum"]["z"]))), 0.05)
    start_z = top_z
    if "start_depth_from_bottom_mm" in params:
        start_z = min(top_z, float(bounds["minimum"]["z"]) + float(params["start_depth_from_bottom_mm"]))
    common_parameters(obj, controller, params, start_z, top_z - depth, clearance_z)
    obj.Base = face_reference(model, face_number)
    obj.Side, obj.Direction = "Outside", "CW"
    obj.processHoles, obj.processPerimeter = False, True
    set_if_present(obj, "UseComp", False)
    set_if_present(obj, "ExtraOffset", float(params.get("radial_allowance_mm", 0.0)))
    obj.Proxy.execute(obj)
    return obj


def create_deburr(document, job, operation, controller, model, bounds, clearance_z):
    params = operation.get("parameters", {})
    obj = PathDeburr.Create(operation["id"] + "_DEBURR", parentJob=job)
    prepare_proxy(obj, job)
    obj.ToolController = controller
    set_if_present(obj, "OpToolDiameter", controller.Tool.Diameter)
    set_if_present(obj, "ClearanceHeight", clearance_z)
    set_if_present(obj, "SafeHeight", float(bounds["maximum"]["z"]) + 2.0)
    obj.Base = face_reference(model, top_face(model)[2])
    obj.Width = max(float(params.get("chamfer_width_mm", 0.3)), 0.01)
    obj.ExtraDepth = max(float(params.get("extra_depth_mm", 0.05)), 0.0)
    obj.Direction = "CW"
    obj.Proxy.execute(obj)
    return obj


def create_pocket(document, job, operation, controller, model, features, bounds, clearance_z):
    feature_ids = operation.get("feature_ids", [])
    feature = features.get(feature_ids[0]) if feature_ids else None
    if not feature:
        return None
    try:
        face_number = int(feature.get("source_face_id", "").rsplit("-", 1)[1])
    except (IndexError, ValueError):
        face_number = top_face(model)[2]
    obj = PathPocketShape.Create(operation["id"] + "_POCKET", parentJob=job)
    prepare_proxy(obj, job)
    params, feature_bounds = operation.get("parameters", {}), feature.get("bounds") or bounds
    top_z, final_z = float(feature_bounds["maximum"]["z"]), float(feature_bounds["minimum"]["z"])
    common_parameters(obj, controller, params, top_z, final_z, clearance_z)
    obj.Base = face_reference(model, face_number)
    set_if_present(obj, "UseOutline", True)
    obj.Proxy.execute(obj)
    return obj


def create_facing(document, job, operation, controller, model, bounds, clearance_z):
    obj = PathMillFace.Create(operation["id"] + "_MILLFACE", parentJob=job)
    prepare_proxy(obj, job)
    params, top_z = operation.get("parameters", {}), float(bounds["maximum"]["z"])
    common_parameters(obj, controller, params, top_z,
        top_z - max(float(params.get("depth_mm", 0.2)), 0.01), clearance_z)
    obj.Base = face_reference(model, top_face(model)[2])
    obj.Proxy.execute(obj)
    return obj


def add_holding_tags(document, job, profile, operation):
    params = operation.get("parameters", {})
    count = max(0, int(params.get("tab_count", 0)))
    if not count:
        return profile
    dressup = PathDressupHoldingTags.Create(profile, operation["id"] + "_HOLDING_TAGS")
    dressup.Width = max(float(params.get("tab_width_mm", 4.0)), 0.1)
    dressup.Height = max(float(params.get("tab_height_mm", 0.6)), 0.05)
    dressup.Proxy.generateTags(dressup, count)
    dressup.Proxy.execute(dressup)
    return dressup


def linear_preview(path_object, frame, setup_id, work_axis):
    position, preview = {"x": 0.0, "y": 0.0, "z": 0.0}, []

    def append_segment(start, end, motion):
        world_start, world_end = local_to_world(start, frame), local_to_world(end, frame)
        preview.append({"motion": motion,
            "x1": world_start["x"], "y1": world_start["y"], "z1": world_start["z"],
            "x2": world_end["x"], "y2": world_end["y"], "z2": world_end["z"],
            "local_z1": start["z"], "local_z2": end["z"],
            "setup_id": setup_id, "work_axis": work_axis})

    for item in path_object.Path.Commands:
        command = item.Name.upper()
        if command in {"G81", "G82", "G83"}:
            initial_z = position["z"]
            cycle_xy = {**position,
                "x": float(item.Parameters.get("X", position["x"])),
                "y": float(item.Parameters.get("Y", position["y"]))}
            retract_z = float(item.Parameters.get("R", initial_z))
            cycle_start = {**cycle_xy, "z": retract_z}
            cycle_bottom = {**cycle_xy, "z": float(item.Parameters.get("Z", retract_z))}
            if any(abs(position[axis] - cycle_start[axis]) > 1e-9 for axis in "xyz"):
                append_segment(dict(position), cycle_start, "rapid")
            append_segment(cycle_start, cycle_bottom, "cut")
            # FreeCAD's drilling operation emits G98, so the cycle returns to the
            # initial plane rather than staying at the lower R plane between holes.
            cycle_return = {**cycle_xy, "z": initial_z}
            append_segment(cycle_bottom, cycle_return, "rapid")
            position = cycle_return
            continue
        if command not in {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"}:
            continue
        start = dict(position)
        for axis in "XYZ":
            if axis in item.Parameters:
                position[axis.lower()] = float(item.Parameters[axis])
        if command in {"G2", "G02", "G3", "G03"} and ("I" in item.Parameters or "J" in item.Parameters):
            center_x = start["x"] + float(item.Parameters.get("I", 0.0))
            center_y = start["y"] + float(item.Parameters.get("J", 0.0))
            radius = math.hypot(start["x"] - center_x, start["y"] - center_y)
            if radius > 1e-8:
                start_angle = math.atan2(start["y"] - center_y, start["x"] - center_x)
                end_angle = math.atan2(position["y"] - center_y, position["x"] - center_x)
                sweep = end_angle - start_angle
                clockwise = command in {"G2", "G02"}
                if clockwise and sweep >= -1e-9:
                    sweep -= 2 * math.pi
                if not clockwise and sweep <= 1e-9:
                    sweep += 2 * math.pi
                samples = max(2, int(math.ceil(abs(sweep) * radius / 0.4)))
                previous = start
                for sample in range(1, samples + 1):
                    ratio = sample / samples
                    angle = start_angle + sweep * ratio
                    current = {
                        "x": center_x + radius * math.cos(angle),
                        "y": center_y + radius * math.sin(angle),
                        "z": start["z"] + (position["z"] - start["z"]) * ratio,
                    }
                    if sample == samples:
                        current = dict(position)
                    append_segment(previous, current, "cut")
                    previous = current
                continue
        append_segment(start, dict(position), "rapid" if command in {"G0", "G00"} else "cut")
    return preview


def has_cutting_motion(path_object):
    """Reject native operations that FreeCAD created without an actual toolpath."""
    cutting_commands = {"G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83"}
    return any(command.Name.upper() in cutting_commands for command in path_object.Path.Commands)


def target_profile_points(shape, work_axis):
    axis, candidates = normalize(tuple(float(work_axis[a]) for a in "xyz")), []
    for face in shape.Faces:
        if type(face.Surface).__name__ != "Plane":
            continue
        u0, u1, v0, v1 = face.ParameterRange
        normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        if normal.x * axis[0] + normal.y * axis[1] + normal.z * axis[2] > 0.98:
            candidates.append(face)
    if not candidates:
        return []
    return [{"x": p.x, "y": p.y, "z": p.z}
            for p in max(candidates, key=lambda face: face.Area).OuterWire.discretize(Distance=0.6)]


def create_native_operation(document, job, operation, controller, model, features, bounds, clearance_z):
    kind = operation["type"]
    if kind == "drilling":
        return create_drilling(document, job, operation, controller, features, bounds, clearance_z)
    if kind in {"profile_contouring", "profile_roughing", "profile_finishing", "tab_removal"}:
        profile = create_profile(document, job, operation, controller, model, bounds, clearance_z)
        return add_holding_tags(document, job, profile, operation) if operation.get("parameters", {}).get("tab_count") else profile
    if kind == "edge_chamfer":
        return create_deburr(document, job, operation, controller, model, bounds, clearance_z)
    if kind in {"pocket_roughing", "slot_roughing", "pocket_finishing", "slot_finishing"}:
        return create_pocket(document, job, operation, controller, model, features, bounds, clearance_z)
    if kind == "face_milling":
        return create_facing(document, job, operation, controller, model, bounds, clearance_z)
    return None


def main():
    encoded_arguments = os.getenv("CNC_CAM_ARGUMENTS_JSON")
    arguments = json.loads(encoded_arguments) if encoded_arguments else sys.argv[1:]
    if len(arguments) != 6:
        raise SystemExit("usage: freecad_adapter.py source.step analysis.json plan.json output.FCStd output.nc result.json")
    source, analysis_path, plan_path, fcstd_path, nc_path, result_path = arguments
    analysis, plan = load_json(analysis_path), load_json(plan_path)
    operations = [item for setup in plan["setups"] for item in setup["operations"] if item.get("enabled", True)]
    if not operations or any(item.get("status") != "approved" for item in operations):
        raise RuntimeError("CAM generation requires every process operation to be approved")

    document = App.newDocument("SeksunCNC_CAM")
    Import.insert(source, document.Name)
    imported = [obj for obj in document.Objects if hasattr(obj, "Shape") and not obj.Shape.isNull()]
    if not imported:
        raise RuntimeError("FreeCAD could not import a solid from the source file")
    imported[0].Label = "Original STEP Reference"
    source_shape = Part.read(source)
    source_solids = list(source_shape.Solids)
    if source_solids:
        # Geometry analysis currently uses the largest solid as the default
        # workpiece.  Apply the same rule here so an assembly's pins, gasket or
        # reference components are visible in the source model but never become
        # accidental CAM stock/model geometry.
        source_shape = max(source_solids, key=lambda solid: abs(float(solid.Volume)))
    features = {item["id"]: item for item in
        [*analysis.get("cylindrical_features", []), *analysis.get("prismatic_features", [])]}
    bounds = analysis["measurements"]["bounding_box"]
    clearance = float((plan.get("safety") or {}).get("clearance_mm", 3.0))
    outputs, post_outputs, preview, generated, skipped, setup_results, profile_boundaries = [], [], [], [], [], [], []
    native_types = {}

    for setup_index, setup in enumerate(plan["setups"], 1):
        frame, local_features = setup_frame(setup["work_axis"]), transform_features(features, setup_frame(setup["work_axis"]))
        local_bounds = transform_bounds(bounds, frame)
        local_model = document.addObject("Part::Feature", "Setup%d_Model" % setup_index)
        local_model.Label, local_model.Shape = setup["name"] + " Workpiece", transform_shape(source_shape, frame)
        job = PathJob.Create("Setup%d_Job" % setup_index, [local_model])
        job.Label, job.PostProcessor = setup["name"] + " · Native CAM Job", "grbl"
        job.PostProcessorArgs = "--comments --no-line-numbers --precision=3"
        remove_default_tools(document, job)
        model, clearance_z, setup_generated = job.Model.Group[0], float(local_bounds["maximum"]["z"]) + clearance, []
        setup_outputs = []
        for operation in setup["operations"]:
            if not operation.get("enabled", True):
                continue
            controller = make_tool_controller(job, operation)
            try:
                native = create_native_operation(document, job, operation, controller, model,
                    local_features, local_bounds, clearance_z)
            except Exception as error:
                skipped.append("%s (%s) FreeCAD 原生工序生成失败: %s" %
                               (operation["id"], operation["type"], error))
                continue
            if native is None or not native.Path.Commands:
                skipped.append("%s (%s) 未生成 FreeCAD 原生刀路" %
                               (operation["id"], operation["type"]))
                continue
            if not has_cutting_motion(native):
                skipped.append("%s (%s) FreeCAD 仅返回定位快移，没有有效切削运动" %
                               (operation["id"], operation["type"]))
                continue
            native.Label = "%s %s" % (operation["id"], operation["name"])
            native.addProperty("App::PropertyString", "SeksunOperationId", "Seksun CNC")
            native.SeksunOperationId = operation["id"]
            native.addProperty("App::PropertyString", "SeksunSetupId", "Seksun CNC")
            native.SeksunSetupId = setup["id"]
            outputs.append(native)
            post_outputs.extend((controller, native))
            setup_outputs.extend((controller, native))
            preview.extend({"operation_id": operation["id"], **segment}
                           for segment in linear_preview(native, frame, setup["id"], setup["work_axis"]))
            generated.append(operation["id"])
            setup_generated.append(operation["id"])
            native_types[operation["id"]] = native.Proxy.__class__.__name__
            if operation["type"] in {"profile_contouring", "tab_removal"}:
                points = target_profile_points(source_shape, setup["work_axis"])
                if points:
                    profile_boundaries.append({"operation_id": operation["id"], "setup_id": setup["id"],
                        "work_axis": setup["work_axis"], "points": points})
        setup_program = None
        if setup_outputs:
            # ToolController path commands (T/M6, spindle and feed state) are
            # materialized during recompute and must exist before postprocessing.
            document.recompute()
            safe_setup_id = re.sub(r"[^A-Za-z0-9_-]+", "_", setup["id"])
            setup_program = "program-%s.nc" % safe_setup_id
            grbl_post.export(setup_outputs, str(FilePath(nc_path).with_name(setup_program)),
                             "--comments --no-line-numbers --no-show-editor --tool-change --precision=3")
        setup_results.append({"setup_id": setup["id"], "work_axis": setup["work_axis"],
            "clearance_z": clearance_z, "generated_operations": setup_generated, "native_job": job.Name,
            "program": setup_program,
            "local_bounds": local_bounds,
            "frame": {name: {axis: frame[name][index] for index, axis in enumerate("xyz")}
                      for name in ("x", "y", "z")},
            "work_coordinate_note": "Local +Z follows the setup work axis; reset work offset after re-fixturing."})

    if not outputs:
        raise RuntimeError("No supported native FreeCAD CAM operations were generated")
    document.recompute()
    document.saveAs(fcstd_path)
    grbl_post.export(post_outputs, nc_path,
                     "--comments --no-line-numbers --no-show-editor --tool-change --precision=3")
    result = {"engine": "FreeCAD Path (native operations)", "engine_version": ".".join(App.Version()[:3]),
        "operation_backend": "native", "postprocessor": "grbl", "generated_operations": generated,
        "native_operation_types": native_types, "setups": setup_results, "skipped": skipped,
        "path_command_count": sum(len(item.Path.Commands) for item in outputs), "preview_segments": preview,
        "profile_boundaries": profile_boundaries, "gcode_bytes": FilePath(nc_path).stat().st_size}
    FilePath(result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    App.closeDocument(document.Name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
