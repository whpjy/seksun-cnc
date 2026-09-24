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
    import Path.Op.Surface as PathSurface
    import Path.Op.Waterline as PathWaterline
    import Path.Tool.Controller as PathToolController
    import Path.Post.scripts.fanuc_post as fanuc_post
    import Path.Post.scripts.grbl_post as grbl_post
    from Path.Tool.toolbit import ToolBit as NativeToolBit
except ImportError:
    from PathScripts import PathDeburr, PathDrilling, PathDressupHoldingTags, PathJob, PathMillFace
    from PathScripts import PathPocketShape, PathProfile, PathSurface, PathToolController, PathWaterline
    from PathScripts.post import fanuc_post, grbl_post
    NativeToolBit = None


def load_json(path):
    return json.loads(FilePath(path).read_text(encoding="utf-8"))


def emit_progress(stage, message, **details):
    print(
        "CNC_PROGRESS " + json.dumps(
            {"stage": stage, "message": message, **details},
            ensure_ascii=False,
        ),
        flush=True,
    )


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
        for direction_key in ("normal", "axis", "access_direction"):
            if feature.get(direction_key):
                feature[direction_key] = world_to_local(feature[direction_key], frame)
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
            "ball_end_mill": "ballend.fcstd",
            "bull_end_mill": "bullnose.fcstd",
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


def referenced_planar_feature(operation, features):
    """Resolve an operation feature to the planar STEP face it came from."""
    feature_ids = operation.get("feature_ids", [])
    feature = features.get(feature_ids[0]) if feature_ids else None
    if feature and (feature.get("bottom_face_id") or feature.get("source_face_id")):
        feature = features.get(feature.get("bottom_face_id") or feature["source_face_id"])
    return feature if feature and feature.get("normal") else None


def planar_face_number(model, feature):
    """Match an analysis plane to a FreeCAD face by geometry, not PF-N text.

    PF identifiers enumerate planar analysis features, while Shape.Faces also
    contains cylinders and fillets.  Those two indices are therefore not
    interchangeable.  New analyses carry the original face index; old stored
    jobs remain supported through geometric matching.
    """
    normal = feature.get("normal") or {}
    center = feature.get("center") or {}
    expected_normal = normalize(tuple(float(normal[axis]) for axis in "xyz"))
    expected_center = tuple(float(center[axis]) for axis in "xyz")
    expected_area = max(float(feature.get("area", 0.0)), 1e-9)
    diagonal = max(float(model.Shape.BoundBox.DiagonalLength), 1.0)
    candidates = []
    for index, face in enumerate(model.Shape.Faces, 1):
        if type(face.Surface).__name__ != "Plane":
            continue
        u0, u1, v0, v1 = face.ParameterRange
        candidate_normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        face_normal = normalize((candidate_normal.x, candidate_normal.y, candidate_normal.z))
        alignment = sum(face_normal[i] * expected_normal[i] for i in range(3))
        face_center = face.CenterOfMass
        center_error = math.sqrt(sum((value - expected_center[i]) ** 2 for i, value in enumerate(
            (face_center.x, face_center.y, face_center.z)))) / diagonal
        area_error = abs(math.log(max(float(face.Area), 1e-9) / expected_area))
        score = center_error * 8.0 + area_error + (1.0 - alignment) * 4.0
        candidates.append((score, alignment, index))
    if not candidates:
        raise RuntimeError("No planar FreeCAD face is available for the referenced feature")
    score, alignment, matched_index = min(candidates)
    if alignment < 0.9 or score > 1.0:
        raise RuntimeError(
            "Referenced planar feature does not match a STEP face "
            "(best Face%d, score %.3f, normal %.3f)" % (matched_index, score, alignment)
        )
    return matched_index


def create_profile(document, job, operation, controller, model, bounds, clearance_z):
    params = operation.get("parameters", {})
    obj = PathProfile.Create(operation["id"] + "_PROFILE", parentJob=job)
    prepare_proxy(obj, job)
    top_z = float(bounds["maximum"]["z"])
    depth = max(float(params.get("depth_mm", top_z - float(bounds["minimum"]["z"]))), 0.05)
    start_z = top_z
    if "start_depth_from_bottom_mm" in params:
        start_z = min(top_z, float(bounds["minimum"]["z"]) + float(params["start_depth_from_bottom_mm"]))
    common_parameters(obj, controller, params, start_z, top_z - depth, clearance_z)
    outline = silhouette_face(document, model.Shape, top_z, operation["id"])
    obj.Base = face_reference(outline, 1)
    obj.Side, obj.Direction = "Outside", "CW"
    obj.processHoles, obj.processPerimeter = False, True
    # PathProfile only offsets the tool centre by its radius when UseComp is
    # enabled.  Without it a nominal outside profile places the cutter centre
    # on the target silhouette and overcuts the finished part by one radius.
    set_if_present(obj, "UseComp", True)
    set_if_present(obj, "OffsetExtra", float(params.get("radial_allowance_mm", 0.0)))
    obj.Proxy.execute(obj)
    return obj


def internal_profile_wire(model, feature):
    planar_feature = feature.get("source_planar_feature")
    if not planar_feature:
        raise RuntimeError("Internal profile has no source planar feature")
    face_number = planar_face_number(model, planar_feature)
    face = model.Shape.Faces[face_number - 1]
    expected = feature.get("bounds") or {}
    expected_center = feature.get("center") or {}
    expected_perimeter = max(float(feature.get("perimeter", 0.0)), 1e-9)
    diagonal = max(float(model.Shape.BoundBox.DiagonalLength), 1.0)
    candidates = []
    for wire in face.Wires:
        if wire.isSame(face.OuterWire):
            continue
        center = wire.CenterOfMass
        center_error = math.sqrt(sum(
            (float(value) - float(expected_center.get(axis, value))) ** 2
            for axis, value in zip("xyz", (center.x, center.y, center.z))
        )) / diagonal
        perimeter_error = abs(math.log(max(float(wire.Length), 1e-9) / expected_perimeter))
        box = wire.BoundBox
        expected_size = expected.get("size") or {}
        size_error = sum(
            abs(float(actual) - float(expected_size.get(axis, actual)))
            for axis, actual in zip("xyz", (box.XLength, box.YLength, box.ZLength))
        ) / diagonal
        candidates.append((center_error * 8 + perimeter_error + size_error, wire))
    if not candidates:
        raise RuntimeError("Referenced planar face has no internal wire")
    score, wire = min(candidates, key=lambda item: item[0])
    if score > 1.0:
        raise RuntimeError("Internal profile does not match a STEP wire (score %.3f)" % score)
    return wire


def create_internal_profile(document, job, operation, controller, model, features, bounds, clearance_z):
    feature_ids = operation.get("feature_ids", [])
    feature = features.get(feature_ids[0]) if feature_ids else None
    if not feature:
        return None
    source = features.get(feature.get("source_face_id"))
    if not source:
        raise RuntimeError("Internal profile source face is unavailable")
    feature = {**feature, "source_planar_feature": source}
    wire = internal_profile_wire(model, feature)
    outline = document.addObject("Part::Feature", operation["id"] + "_INTERNAL_OUTLINE")
    outline.Label = operation["id"] + " STEP internal profile"
    outline.Shape = Part.Face(wire)
    obj = PathProfile.Create(operation["id"] + "_INTERNAL_PROFILE", parentJob=job)
    prepare_proxy(obj, job)
    params = operation.get("parameters", {})
    top_z = float(bounds["maximum"]["z"])
    depth = max(float(params.get("depth_mm", feature.get("depth", 0.0))), 0.05)
    common_parameters(obj, controller, params, top_z, top_z - depth, clearance_z)
    obj.Base = face_reference(outline, 1)
    is_engraving = operation.get("type") == "engraving"
    obj.Side, obj.Direction = "Inside", "CCW"
    obj.processHoles, obj.processPerimeter = False, True
    set_if_present(obj, "UseComp", not is_engraving)
    set_if_present(obj, "OffsetExtra", 0.0 if is_engraving else float(params.get("radial_allowance_mm", 0.0)))
    obj.Proxy.execute(obj)
    return obj


def internal_profile_points(model, feature, frame):
    wire = internal_profile_wire(model, feature)
    points = wire.discretize(Deflection=0.12)
    return [local_to_world({"x": point.x, "y": point.y, "z": point.z}, frame) for point in points]


def create_deburr(document, job, operation, controller, model, bounds, clearance_z):
    """Create a deterministic shallow profile for a 90-degree chamfer tool.

    FreeCAD's Deburr proxy offsets every wire on a complex face and can abort
    on small blended edges.  A profile operation over the setup-visible top
    face is more stable, retains its internal loops, and produces the same
    controlled chamfer depth for the conical cutter.
    """
    params = operation.get("parameters", {})
    obj = PathProfile.Create(operation["id"] + "_CHAMFER_PROFILE", parentJob=job)
    prepare_proxy(obj, job)
    top_z = float(bounds["maximum"]["z"])
    chamfer_depth = max(
        float(params.get("chamfer_width_mm", 0.3))
        + float(params.get("extra_depth_mm", 0.05)),
        0.05,
    )
    common_parameters(obj, controller, params, top_z, top_z - chamfer_depth, clearance_z)
    obj.Base = face_reference(model, top_face(model)[2])
    obj.Side, obj.Direction = "Outside", "CW"
    obj.processHoles, obj.processPerimeter = True, True
    set_if_present(obj, "UseComp", False)
    set_if_present(obj, "OffsetExtra", 0.0)
    obj.Proxy.execute(obj)
    return obj


def create_pocket(document, job, operation, controller, model, features, bounds, clearance_z):
    feature_ids = operation.get("feature_ids", [])
    feature = features.get(feature_ids[0]) if feature_ids else None
    if not feature:
        return None
    planar_feature = referenced_planar_feature(operation, features)
    if not planar_feature:
        raise RuntimeError("Pocket feature has no planar source face")
    face_number = planar_face_number(model, planar_feature)
    obj = PathPocketShape.Create(operation["id"] + "_POCKET", parentJob=job)
    prepare_proxy(obj, job)
    params = operation.get("parameters", {})
    floor_z = float(planar_feature["center"]["z"])
    requested_depth = max(float(params.get("depth_mm", feature.get("depth", 0.0))), 0.05)
    top_z = min(float(bounds["maximum"]["z"]), floor_z + requested_depth)
    final_z = min(top_z, floor_z + max(float(params.get("floor_allowance_mm", 0.0)), 0.0))
    if top_z - final_z < 0.05:
        raise RuntimeError("Pocket depth is outside the current setup stock bounds")
    common_parameters(obj, controller, params, top_z, final_z, clearance_z)
    obj.Base = face_reference(model, face_number)
    set_if_present(obj, "UseOutline", True)
    set_if_present(obj, "StepOver", int(round(min(95.0, max(1.0, float(params.get("step_over_percent", 50.0)))))))
    set_if_present(obj, "ExtraOffset", max(float(params.get("wall_allowance_mm", 0.0)), 0.0))
    obj.Proxy.execute(obj)
    return obj


def create_facing(document, job, operation, controller, model, features, bounds, clearance_z):
    obj = PathMillFace.Create(operation["id"] + "_MILLFACE", parentJob=job)
    prepare_proxy(obj, job)
    params, top_z = operation.get("parameters", {}), float(bounds["maximum"]["z"])
    common_parameters(obj, controller, params, top_z,
        top_z - max(float(params.get("depth_mm", 0.2)), 0.01), clearance_z)
    planar_feature = referenced_planar_feature(operation, features)
    face_number = planar_face_number(model, planar_feature) if planar_feature else top_face(model)[2]
    obj.Base = face_reference(model, face_number)
    obj.Proxy.execute(obj)
    return obj


def create_surface(document, job, operation, controller, model, bounds, stock_bounds, clearance_z):
    """Create a native OpenCAMLib drop-cutter surface operation."""
    obj = PathSurface.Create(operation["id"] + "_SURFACE", parentJob=job)
    prepare_proxy(obj, job)
    params = operation.get("parameters", {})
    top_z = (float(stock_bounds["maximum"]["z"])
             if operation["type"] == "surface_roughing" else float(bounds["maximum"]["z"]))
    bottom_z = float(bounds["minimum"]["z"])
    common_parameters(obj, controller, params, top_z, bottom_z, clearance_z)
    visible_faces = []
    for index, face in enumerate(model.Shape.Faces, 1):
        u0, u1, v0, v1 = face.ParameterRange
        try:
            normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        except Exception:
            continue
        # A three-axis drop-cutter operation can only reach the upper envelope
        # in setup-local +Z.  Back-facing surfaces belong to the flipped setup.
        if normal.z >= -0.05:
            visible_faces.append("Face%d" % index)
    if not visible_faces:
        raise RuntimeError("No setup-visible faces are available for 3D surface machining")
    obj.Base = [(model, visible_faces)]
    set_if_present(obj, "ScanType", "Planar")
    set_if_present(obj, "BoundBox", "BaseBoundBox")
    # Scan the complete model bounding region so stock in gaps between
    # disconnected visible faces is cleared as well.  Restricting the cutter
    # to every selected face independently leaves web-shaped islands of stock
    # on sparse, relief-like parts such as the registration sample.
    set_if_present(obj, "BoundaryEnforcement", bool(params.get("boundary_enforcement", False)))
    set_if_present(obj, "InternalFeaturesCut", True)
    set_if_present(obj, "CutPattern", "ZigZag")
    set_if_present(obj, "CutMode", "Climb")
    sample_interval = max(float(params.get("sample_interval_mm", 0.8)), 0.05)
    set_if_present(obj, "LinearDeflection", min(0.2, max(0.05, sample_interval * 0.1)))
    set_if_present(obj, "AngularDeflection", 0.25)
    set_if_present(obj, "SampleInterval", sample_interval)
    set_if_present(obj, "DepthOffset", max(float(params.get("depth_offset_mm", 0.0)), 0.0))
    if operation["type"] == "surface_roughing":
        set_if_present(obj, "LayerMode", "Multi-pass")
        set_if_present(obj, "StepDown", max(float(params.get("step_down_mm", 1.0)), 0.05))
        set_if_present(obj, "StepOver", min(90.0, max(5.0, float(params.get("step_over_percent", 45.0)))))
    else:
        set_if_present(obj, "LayerMode", "Single-pass")
        diameter = max(float(operation["tool"].get("diameter_mm", 1.0)), 0.1)
        step_over_mm = max(float(params.get("step_over_mm", 0.5)), 0.01)
        set_if_present(obj, "StepOver", min(90.0, max(1.0, step_over_mm / diameter * 100.0)))
    obj.Proxy.execute(obj)
    return obj


def create_waterline(document, job, operation, controller, model, bounds, clearance_z):
    """Finish steep setup-visible faces with native OCL waterline paths."""
    obj = PathWaterline.Create(operation["id"] + "_WATERLINE", parentJob=job)
    prepare_proxy(obj, job)
    params = operation.get("parameters", {})
    top_z = float(bounds["maximum"]["z"])
    bottom_z = float(bounds["minimum"]["z"])
    common_parameters(obj, controller, params, top_z, bottom_z, clearance_z)
    visible_faces = []
    for index, face in enumerate(model.Shape.Faces, 1):
        u0, u1, v0, v1 = face.ParameterRange
        try:
            normal = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        except Exception:
            continue
        if normal.z >= -0.05:
            visible_faces.append("Face%d" % index)
    if not visible_faces:
        raise RuntimeError("No setup-visible faces are available for waterline finishing")
    obj.Base = [(model, visible_faces)]
    set_if_present(obj, "Algorithm", "OCL Dropcutter")
    set_if_present(obj, "BoundBox", "BaseBoundBox")
    set_if_present(obj, "BoundaryEnforcement", True)
    set_if_present(obj, "InternalFeaturesCut", True)
    set_if_present(obj, "HandleMultipleFeatures", "Collectively")
    set_if_present(obj, "LayerMode", "Multi-pass")
    set_if_present(obj, "CutPattern", "None")
    set_if_present(obj, "CutMode", "Climb")
    set_if_present(obj, "ClearLastLayer", "Off")
    sample_interval = max(float(params.get("sample_interval_mm", 0.5)), 0.05)
    set_if_present(obj, "LinearDeflection", min(0.12, max(0.05, sample_interval * 0.15)))
    set_if_present(obj, "AngularDeflection", 0.25)
    set_if_present(obj, "SampleInterval", sample_interval)
    set_if_present(obj, "DepthOffset", max(float(params.get("depth_offset_mm", 0.0)), 0.0))
    set_if_present(obj, "StepDown", max(float(params.get("step_down_mm", 0.4)), 0.05))
    set_if_present(obj, "StepOver", min(90.0, max(1.0, float(params.get("step_over_percent", 20.0)))))
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


def linear_preview(path_object, frame, setup_id, work_axis, local_stock_top_z, clearance_z):
    # G-code does not define a physical move from XYZ zero to its first modal
    # position.  Starting the preview at the enforced clearance plane avoids
    # inventing a retract through the stock and reporting a false collision.
    position, preview = {"x": 0.0, "y": 0.0, "z": float(clearance_z)}, []

    def append_segment(start, end, motion):
        world_start, world_end = local_to_world(start, frame), local_to_world(end, frame)
        preview.append({"motion": motion,
            "x1": world_start["x"], "y1": world_start["y"], "z1": world_start["z"],
            "x2": world_end["x"], "y2": world_end["y"], "z2": world_end["z"],
            "local_z1": start["z"], "local_z2": end["z"],
            "local_stock_top_z": local_stock_top_z,
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


def enforce_rapid_clearance(path_object, clearance_z):
    """Rewrite unsafe lateral G0 moves into lift, traverse, and descend moves.

    FreeCAD Surface may connect disconnected drop-cutter scan lines with a G0
    at cutting depth.  Such a move is not safe against uncut stock.  Keep the
    native operation object, but normalize its command stream before preview,
    postprocessing, collision checking, and export.
    """
    position = {"X": 0.0, "Y": 0.0, "Z": float(clearance_z)}
    rewritten = []
    for command in path_object.Path.Commands:
        name = command.Name.upper()
        parameters = dict(command.Parameters)
        target = dict(position)
        for axis in ("X", "Y", "Z"):
            if axis in parameters:
                target[axis] = float(parameters[axis])
        lateral = abs(target["X"] - position["X"]) > 1e-7 or abs(target["Y"] - position["Y"]) > 1e-7
        if name in {"G0", "G00"} and lateral and min(position["Z"], target["Z"]) < clearance_z - 1e-7:
            if position["Z"] < clearance_z - 1e-7:
                rewritten.append(Path.Command("G0", {"Z": float(clearance_z)}))
            traverse = {key: value for key, value in parameters.items() if key != "Z"}
            traverse["Z"] = float(clearance_z)
            rewritten.append(Path.Command("G0", traverse))
            if target["Z"] < clearance_z - 1e-7:
                rewritten.append(Path.Command("G0", {"Z": target["Z"]}))
        else:
            rewritten.append(command)
        position = target
    path_object.Path = Path.Path(rewritten)


def target_profile_points(shape, work_axis):
    """Return the whole solid silhouette, not one planar face's outer wire."""
    frame = setup_frame(work_axis)
    local_shape = transform_shape(shape, frame)
    local_points = silhouette_polygon(local_shape)
    points = []
    for point in local_points:
        world = local_to_world({"x": point[0], "y": point[1], "z": local_shape.BoundBox.ZMax}, frame)
        points.append(world)
    return points


def silhouette_polygon(shape, resolution=0.6):
    """Rasterize the tessellated solid projection and trace its outside loop."""
    vertices, facets = shape.tessellate(max(resolution * 0.35, 0.12))
    if not facets:
        raise RuntimeError("STEP solid cannot be tessellated for profile machining")
    box = shape.BoundBox
    x_origin, y_origin = box.XMin - resolution, box.YMin - resolution
    occupied = set()

    def signed_area(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for facet in facets:
        triangle = [(vertices[index].x, vertices[index].y) for index in facet]
        area = signed_area(triangle[0], triangle[1], triangle[2])
        if abs(area) < 1e-10:
            continue
        min_i = int(math.floor((min(point[0] for point in triangle) - x_origin) / resolution))
        max_i = int(math.ceil((max(point[0] for point in triangle) - x_origin) / resolution))
        min_j = int(math.floor((min(point[1] for point in triangle) - y_origin) / resolution))
        max_j = int(math.ceil((max(point[1] for point in triangle) - y_origin) / resolution))
        for j in range(min_j, max_j):
            y = y_origin + (j + 0.5) * resolution
            for i in range(min_i, max_i):
                x = x_origin + (i + 0.5) * resolution
                point = (x, y)
                signs = [signed_area(triangle[k], triangle[(k + 1) % 3], point) for k in range(3)]
                if all(value >= -1e-9 for value in signs) or all(value <= 1e-9 for value in signs):
                    occupied.add((i, j))
    if not occupied:
        raise RuntimeError("STEP projection did not contain any occupied profile cells")

    edges = set()
    for i, j in occupied:
        for edge in (((i, j), (i + 1, j)), ((i + 1, j), (i + 1, j + 1)),
                     ((i + 1, j + 1), (i, j + 1)), ((i, j + 1), (i, j))):
            reverse = (edge[1], edge[0])
            if reverse in edges:
                edges.remove(reverse)
            else:
                edges.add(edge)
    outgoing = {}
    for start, end in edges:
        outgoing.setdefault(start, []).append(end)
    loops = []
    unused = set(edges)
    while unused:
        start, current = next(iter(unused))
        loop = [start]
        edge = (start, current)
        while edge in unused:
            unused.remove(edge)
            loop.append(edge[1])
            choices = [candidate for candidate in outgoing.get(edge[1], []) if (edge[1], candidate) in unused]
            if not choices:
                break
            edge = (edge[1], choices[0])
        if len(loop) >= 4 and loop[-1] == loop[0]:
            loops.append(loop)
    if not loops:
        raise RuntimeError("STEP projection did not produce a closed outside profile")
    loop = max(loops, key=lambda points: abs(sum(
        points[index][0] * points[index + 1][1] - points[index + 1][0] * points[index][1]
        for index in range(len(points) - 1)
    )))
    return [(x_origin + i * resolution, y_origin + j * resolution) for i, j in loop]


def silhouette_face(document, shape, z_value, operation_id):
    points = silhouette_polygon(shape)
    vectors = [App.Vector(x, y, z_value) for x, y in points]
    if vectors[0].distanceToPoint(vectors[-1]) > 1e-7:
        vectors.append(vectors[0])
    outline = document.addObject("Part::Feature", operation_id + "_SILHOUETTE")
    outline.Label = operation_id + " STEP projected outside profile"
    outline.Shape = Part.Face(Part.makePolygon(vectors))
    return outline


def create_native_operation(document, job, operation, controller, model, features, bounds, stock_bounds, clearance_z):
    kind = operation["type"]
    if kind == "drilling":
        return create_drilling(document, job, operation, controller, features, bounds, clearance_z)
    if kind in {"profile_contouring", "profile_roughing", "profile_finishing", "tab_removal"}:
        profile = create_profile(document, job, operation, controller, model, bounds, clearance_z)
        return add_holding_tags(document, job, profile, operation) if operation.get("parameters", {}).get("tab_count") else profile
    if kind in {"internal_profile_roughing", "internal_profile_finishing", "engraving"}:
        return create_internal_profile(
            document, job, operation, controller, model, features, bounds, clearance_z,
        )
    if kind == "edge_chamfer":
        return create_deburr(document, job, operation, controller, model, bounds, clearance_z)
    if kind in {"pocket_roughing", "slot_roughing", "pocket_finishing", "slot_finishing"}:
        return create_pocket(document, job, operation, controller, model, features, bounds, clearance_z)
    if kind == "face_milling":
        return create_facing(document, job, operation, controller, model, features, bounds, clearance_z)
    if kind in {"surface_roughing", "surface_3d"}:
        return create_surface(document, job, operation, controller, model, bounds, stock_bounds, clearance_z)
    if kind == "waterline":
        return create_waterline(document, job, operation, controller, model, bounds, clearance_z)
    return None


def main():
    encoded_arguments = os.getenv("CNC_CAM_ARGUMENTS_JSON")
    arguments = json.loads(encoded_arguments) if encoded_arguments else sys.argv[1:]
    if len(arguments) != 6:
        raise SystemExit("usage: freecad_adapter.py source.step analysis.json plan.json output.FCStd output.nc result.json")
    source, analysis_path, plan_path, fcstd_path, nc_path, result_path = arguments
    analysis, plan = load_json(analysis_path), load_json(plan_path)
    operations = [item for setup in plan["setups"] for item in setup["operations"] if item.get("enabled", True)]
    trial_mode = os.getenv("CNC_CAM_TRIAL_MODE") == "1"
    if trial_mode:
        trial_dir = FilePath(result_path).resolve().parent
        output_paths = (FilePath(plan_path), FilePath(analysis_path), FilePath(fcstd_path), FilePath(nc_path), FilePath(result_path))
        if len(operations) != 1 or trial_dir.parent.name != "agent-trials" or any(path.resolve().parent != trial_dir for path in output_paths):
            raise RuntimeError("Trial mode requires one operation and isolated agent-trials output directory")
    elif not operations or any(item.get("status") != "approved" for item in operations):
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
        selected_solid_index = int(analysis.get("topology", {}).get("selected_solid_index", 0))
        selected_candidate = next(
            (item for item in analysis.get("solid_candidates", []) if item.get("selected")),
            None,
        )
        if selected_candidate:
            expected_volume = float(selected_candidate.get("volume", 0))
            expected_size = (selected_candidate.get("bounds") or {}).get("size") or {}

            def candidate_distance(solid):
                box = solid.BoundBox
                scale = max(expected_volume, 1.0)
                volume_error = abs(abs(float(solid.Volume)) - expected_volume) / scale
                size_error = sum(
                    abs(float(actual) - float(expected_size.get(axis, actual)))
                    for axis, actual in zip(("x", "y", "z"), (box.XLength, box.YLength, box.ZLength))
                ) / max(box.DiagonalLength, 1.0)
                return volume_error + size_error

            source_shape = min(source_solids, key=candidate_distance)
        elif 1 <= selected_solid_index <= len(source_solids):
            source_shape = source_solids[selected_solid_index - 1]
        else:
            # Legacy analysis files do not contain an explicit selection.
            source_shape = max(source_solids, key=lambda solid: abs(float(solid.Volume)))
    features = {item["id"]: item for item in [
        *analysis.get("planar_features", []),
        *analysis.get("cylindrical_features", []),
        *analysis.get("prismatic_features", []),
        *analysis.get("internal_profile_features", []),
    ]}
    bounds = analysis["measurements"]["bounding_box"]
    configured_postprocessor = str((plan.get("machine_profile") or {}).get("postprocessor") or "grbl").lower()
    postprocessor_name = "fanuc" if configured_postprocessor == "fanuc" else "grbl"
    postprocessor = fanuc_post if postprocessor_name == "fanuc" else grbl_post
    postprocessor_args = "--no-show-editor --precision=3" if postprocessor_name == "fanuc" else "--comments --no-line-numbers --no-show-editor --tool-change --precision=3"
    stock_size = [float(value) for value in plan.get("stock", {}).get("size_mm", [])]
    if len(stock_size) != 3:
        stock_size = [float(bounds["maximum"][axis]) - float(bounds["minimum"][axis]) for axis in "xyz"]
    stock_center = {axis: (float(bounds["minimum"][axis]) + float(bounds["maximum"][axis])) / 2.0
                    for axis in "xyz"}
    stock_bounds = {
        "minimum": {axis: stock_center[axis] - stock_size[index] / 2.0
                    for index, axis in enumerate("xyz")},
        "maximum": {axis: stock_center[axis] + stock_size[index] / 2.0
                    for index, axis in enumerate("xyz")},
    }
    clearance = float((plan.get("safety") or {}).get("clearance_mm", 3.0))
    outputs, post_outputs, preview, generated, skipped, setup_results, profile_boundaries = [], [], [], [], [], [], []
    native_types = {}

    completed_operation_count = 0
    for setup_index, setup in enumerate(plan["setups"], 1):
        emit_progress(
            "setup",
            "正在准备%s" % setup["name"],
            setup_id=setup["id"],
            setup_index=setup_index,
            setup_total=len(plan["setups"]),
            current=completed_operation_count,
            total=len(operations),
        )
        frame, local_features = setup_frame(setup["work_axis"]), transform_features(features, setup_frame(setup["work_axis"]))
        local_bounds = transform_bounds(bounds, frame)
        local_stock_bounds = transform_bounds(stock_bounds, frame)
        local_model = document.addObject("Part::Feature", "Setup%d_Model" % setup_index)
        local_model.Label, local_model.Shape = setup["name"] + " Workpiece", transform_shape(source_shape, frame)
        job = PathJob.Create("Setup%d_Job" % setup_index, [local_model])
        job.Label, job.PostProcessor = setup["name"] + " · Native CAM Job", postprocessor_name
        job.PostProcessorArgs = postprocessor_args
        remove_default_tools(document, job)
        model, clearance_z, setup_generated = job.Model.Group[0], float(local_stock_bounds["maximum"]["z"]) + clearance, []
        setup_outputs = []
        for operation in setup["operations"]:
            if not operation.get("enabled", True):
                continue
            emit_progress(
                "operation",
                "正在生成 %s %s" % (operation["id"], operation["name"]),
                setup_id=setup["id"],
                operation_id=operation["id"],
                operation_name=operation["name"],
                current=completed_operation_count,
                total=len(operations),
            )
            objects_before_operation = {item.Name for item in document.Objects}
            try:
                controller = make_tool_controller(job, operation)
                native = create_native_operation(document, job, operation, controller, model,
                    local_features, local_bounds, local_stock_bounds, clearance_z)
            except Exception as error:
                # Some native CAM proxies create document objects before their
                # geometry calculation fails. Remove the entire partial group;
                # otherwise a later recompute/postprocess fails the whole job.
                for item in reversed(list(document.Objects)):
                    try:
                        item_name = item.Name
                    except Exception:
                        # A failed native proxy may already have invalidated
                        # one of its temporary document objects.
                        continue
                    if item_name not in objects_before_operation:
                        document.removeObject(item_name)
                document.recompute()
                skipped.append("%s (%s) FreeCAD 原生工序生成失败: %s" %
                               (operation["id"], operation["type"], error))
                completed_operation_count += 1
                emit_progress(
                    "operation_skipped",
                    "%s 生成失败：%s" % (operation["id"], error),
                    setup_id=setup["id"], operation_id=operation["id"],
                    error=str(error),
                    current=completed_operation_count, total=len(operations),
                )
                continue
            if native is None or not native.Path.Commands:
                skipped.append("%s (%s) 未生成 FreeCAD 原生刀路" %
                               (operation["id"], operation["type"]))
                completed_operation_count += 1
                emit_progress(
                    "operation_skipped", "%s 未产生刀路" % operation["id"],
                    setup_id=setup["id"], operation_id=operation["id"],
                    current=completed_operation_count, total=len(operations),
                )
                continue
            enforce_rapid_clearance(native, clearance_z)
            if not has_cutting_motion(native):
                skipped.append("%s (%s) FreeCAD 仅返回定位快移，没有有效切削运动" %
                               (operation["id"], operation["type"]))
                completed_operation_count += 1
                emit_progress(
                    "operation_skipped", "%s 没有有效切削运动" % operation["id"],
                    setup_id=setup["id"], operation_id=operation["id"],
                    path_commands=[item.Name for item in native.Path.Commands[:20]],
                    current=completed_operation_count, total=len(operations),
                )
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
                           for segment in linear_preview(native, frame, setup["id"], setup["work_axis"],
                                                         float(local_stock_bounds["maximum"]["z"]), clearance_z))
            generated.append(operation["id"])
            setup_generated.append(operation["id"])
            completed_operation_count += 1
            emit_progress(
                "operation_completed",
                "%s %s 已完成" % (operation["id"], operation["name"]),
                setup_id=setup["id"], operation_id=operation["id"],
                operation_name=operation["name"],
                current=completed_operation_count, total=len(operations),
            )
            native_types[operation["id"]] = native.Proxy.__class__.__name__
            if operation["type"] in {
                "profile_contouring", "profile_roughing", "profile_finishing", "tab_removal",
            }:
                points = target_profile_points(source_shape, setup["work_axis"])
                if points:
                    profile_boundaries.append({"operation_id": operation["id"], "setup_id": setup["id"],
                        "work_axis": setup["work_axis"], "points": points})
            elif operation["type"] in {"internal_profile_roughing", "internal_profile_finishing"}:
                feature = local_features.get(operation.get("feature_ids", [None])[0])
                source = local_features.get(feature.get("source_face_id")) if feature else None
                if feature and source:
                    points = internal_profile_points(
                        model, {**feature, "source_planar_feature": source}, frame,
                    )
                    if points:
                        profile_boundaries.append({
                            "operation_id": operation["id"], "setup_id": setup["id"],
                            "work_axis": setup["work_axis"], "points": points,
                            "remove_side": "inside",
                        })
        setup_program = None
        if setup_outputs:
            # ToolController path commands (T/M6, spindle and feed state) are
            # materialized during recompute and must exist before postprocessing.
            document.recompute()
            safe_setup_id = re.sub(r"[^A-Za-z0-9_-]+", "_", setup["id"])
            setup_program = "program-%s.nc" % safe_setup_id
            postprocessor.export(setup_outputs, str(FilePath(nc_path).with_name(setup_program)),
                                 postprocessor_args)
        setup_results.append({"setup_id": setup["id"], "work_axis": setup["work_axis"],
            "clearance_z": clearance_z, "generated_operations": setup_generated, "native_job": job.Name,
            "program": setup_program,
            "local_bounds": local_stock_bounds,
            "frame": {name: {axis: frame[name][index] for index, axis in enumerate("xyz")}
                      for name in ("x", "y", "z")},
            "work_coordinate_note": "Local +Z follows the setup work axis; reset work offset after re-fixturing."})

    if not outputs:
        raise RuntimeError("No supported native FreeCAD CAM operations were generated")
    document.recompute()
    document.saveAs(fcstd_path)
    postprocessor.export(post_outputs, nc_path, postprocessor_args)
    result = {"engine": "FreeCAD Path (native operations)", "engine_version": ".".join(App.Version()[:3]),
        "trial_only": trial_mode,
        "operation_backend": "native", "postprocessor": postprocessor_name, "generated_operations": generated,
        "native_operation_types": native_types, "setups": setup_results, "skipped": skipped,
        "path_command_count": sum(len(item.Path.Commands) for item in outputs), "preview_segments": preview,
        "profile_boundaries": profile_boundaries, "gcode_bytes": FilePath(nc_path).stat().st_size}
    FilePath(result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_progress(
        "freecad_completed", "FreeCAD 原生刀路生成完成",
        current=len(operations), total=len(operations),
        generated=len(generated), skipped=len(skipped),
    )
    App.closeDocument(document.Name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
