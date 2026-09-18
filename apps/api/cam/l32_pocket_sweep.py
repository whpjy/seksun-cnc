"""OCC cutter-sweep check of a machine-neutral indexed pocket draft.

Arguments (via CNC_CAM_ARGUMENTS_JSON): [STEP path, IndexedPocketDraft JSON].
No NC, job state, or target mesh is written by this script.
"""

from __future__ import annotations

import json
import os

import FreeCAD as App
import Part


def cutter_sweep(start, end, radius, flute_length):
    a = start["point"]
    b = end["point"]
    x0, y0, z0 = float(a["x"]), float(a["y"]), float(a["z"])
    x1, y1, z1 = float(b["x"]), float(b["y"]), float(b["z"])
    eps = 1e-7
    if abs(y0-y1) < eps and abs(z0-z1) < eps:
        return Part.makeCylinder(
            radius, abs(x1-x0) + flute_length,
            App.Vector(min(x0,x1) - flute_length, y0, z0), App.Vector(1,0,0),
        )
    if abs(x0-x1) > eps or (abs(y0-y1) > eps and abs(z0-z1) > eps):
        raise ValueError("only axis-aligned feed moves can be swept")
    base_x = x0 - flute_length
    first = Part.makeCylinder(radius, flute_length, App.Vector(base_x,y0,z0), App.Vector(1,0,0))
    second = Part.makeCylinder(radius, flute_length, App.Vector(base_x,y1,z1), App.Vector(1,0,0))
    if abs(y0-y1) > eps:
        bridge = Part.makeBox(
            flute_length, abs(y1-y0), 2*radius,
            App.Vector(base_x,min(y0,y1),z0-radius),
        )
    else:
        bridge = Part.makeBox(
            flute_length, 2*radius, abs(z1-z0),
            App.Vector(base_x,y0-radius,min(z0,z1)),
        )
    return first.fuse([second, bridge])


def main():
    source_path, draft_json = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    draft = json.loads(draft_json)
    if not draft["reference_only"] or draft["nc_generated"]:
        raise ValueError("this checker accepts reference-only geometric drafts")
    source = Part.read(source_path)
    solids = list(source.Solids)
    if len(solids) != 1:
        raise ValueError(f"expected one target solid, got {len(solids)}")
    target = solids[0]
    moves = draft["moves"]
    radius = float(draft["tool_diameter_mm"]) / 2
    flute_length = float(draft["provisional_flute_length_mm"])
    bounds = draft["floor_bounds"]
    depth = float(draft["pocket_depth_mm"])
    access = draft["access_direction"]
    if access != {"x": -1.0, "y": 0.0, "z": 0.0}:
        raise ValueError("this checker currently supports back-facing -X pockets only")
    lower = bounds["minimum"]
    upper = bounds["maximum"]
    region = Part.makeBox(
        depth, float(upper["y"])-float(lower["y"]), float(upper["z"])-float(lower["z"]),
        App.Vector(float(lower["x"])-depth, float(lower["y"]), float(lower["z"])),
    )
    target_inside_region = target.common(region).Volume
    remaining = region
    touched_target = 0.0
    stage = []
    previous = None
    for move in moves:
        if previous is not None and move["kind"] == "feed":
            sweep = cutter_sweep(previous, move, radius, flute_length)
            touched_target += target.common(sweep).Volume
            remaining = remaining.cut(sweep)
        if previous is not None and move["layer"] != previous["layer"]:
            stage.append({"layer": previous["layer"], "uncut_region_mm3": round(remaining.Volume,6)})
        previous = move
    if previous is not None:
        stage.append({"layer": previous["layer"], "uncut_region_mm3": round(remaining.Volume,6)})
    print("CNC_POCKET_SWEEP " + json.dumps({
        "target_solid_valid": target.isValid(),
        "target_volume_mm3": round(target.Volume, 6),
        "region_volume_mm3": round(region.Volume, 6),
        "target_material_inside_pocket_region_mm3": round(target_inside_region, 6),
        "summed_target_contact_mm3": round(touched_target, 6),
        "remaining_pocket_region_mm3": round(remaining.Volume, 6),
        "remaining_valid": remaining.isValid(),
        "stages": stage,
    }, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
