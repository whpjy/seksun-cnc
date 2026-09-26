"""Verify a back-live face raster against the original STEP solid in OCC."""

from __future__ import annotations

import json
import math
import os

import FreeCAD as App
import Part


def vector(payload):
    return App.Vector(float(payload["x"]), float(payload["y"]), float(payload["z"]))


def main():
    source_path, axis_json, parameters_json = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    axis_payload = json.loads(axis_json)
    parameters = json.loads(parameters_json)
    shape = Part.read(source_path)
    target = shape.Solids[0] if len(shape.Solids) == 1 else shape.multiFuse(list(shape.Solids))
    origin = vector(axis_payload["origin"])
    axis = vector(axis_payload["direction"])
    axis.normalize()
    reference = App.Vector(0, 0, 1) if abs(axis.z) < 0.9 else App.Vector(0, 1, 0)
    u = axis.cross(reference)
    u.normalize()
    v = axis.cross(u)
    v.normalize()

    finished_z = float(parameters["finished_back_z_mm"])
    stock = float(parameters["axial_stock_mm"])
    stock_radius = float(parameters["stock_radius_mm"])
    tool_radius = float(parameters["tool_diameter_mm"]) / 2
    stepover = float(parameters["step_over_mm"])
    finished_plane = origin + axis * finished_z
    retained_plane = finished_plane - axis * stock
    cap = Part.makeCylinder(stock_radius, stock, retained_plane, axis)
    target_in_cap = target.common(cap).Volume

    sweep_radius = stock_radius + tool_radius
    pass_count = max(2, int(math.ceil(2 * sweep_radius / stepover)) + 1)
    actual_step = 2 * sweep_radius / (pass_count - 1)
    sampling = max(tool_radius * 0.7, 0.05)
    total_contact = 0.0
    maximum_contact = 0.0
    sample_count = 0
    # Stop just outside the nominal finished plane so tangency is not reported
    # as an OCC overlap. Coverage at the plane is proved separately by the
    # positive axial stock and bounded raster spacing.
    cutter_length = max(stock - 0.0001, stock * 0.999)
    for row in range(pass_count):
        y = -sweep_radius + row * actual_step
        chord = math.sqrt(max(sweep_radius * sweep_radius - y * y, 0))
        columns = max(2, int(math.ceil(2 * chord / sampling)) + 1)
        for column in range(columns):
            x = -chord + (2 * chord * column / (columns - 1))
            center = retained_plane + u * x + v * y
            cutter = Part.makeCylinder(tool_radius, cutter_length, center, axis)
            contact = target.common(cutter).Volume
            total_contact += contact
            maximum_contact = max(maximum_contact, contact)
            sample_count += 1

    result = {
        "target_solid_valid": bool(target.isValid()),
        "cap_solid_valid": bool(cap.isValid()),
        "cap_volume_mm3": round(cap.Volume, 6),
        "target_in_cap_volume_mm3": round(target_in_cap, 9),
        "summed_target_contact_mm3": round(total_contact, 9),
        "maximum_target_contact_mm3": round(maximum_contact, 9),
        "raster_pass_count": pass_count,
        "cutter_sample_count": sample_count,
        "actual_step_over_mm": round(actual_step, 6),
        "coverage_margin_mm": round(2 * tool_radius - actual_step, 6),
        "finished_plane_projection_mm": finished_z,
        "retained_plane_projection_mm": finished_z - stock,
    }
    result["status"] = "passed" if (
        result["target_solid_valid"]
        and result["cap_solid_valid"]
        and target_in_cap <= 1e-7
        and maximum_contact <= 1e-7
        and actual_step <= 2 * tool_radius + 1e-9
    ) else "failed"
    print("CNC_BACK_LIVE_FACE_SWEEP " + json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
