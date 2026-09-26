"""Check radial side-milling cutter sweeps against the exact STEP target.

Arguments: [source STEP, JSON array of SideMillDraft objects]. This checks
target gouging only; it does not certify fixture or holder clearance.
"""

from __future__ import annotations

import json
import os

import FreeCAD as App
import Part


def sweep(start, end, radius, flute_length, sign):
    a, b = start["point"], end["point"]
    x0, y0, z0 = float(a["x"]), float(a["y"]), float(a["z"])
    x1, y1, z1 = float(b["x"]), float(b["y"]), float(b["z"])
    eps = 1e-7
    if abs(x0-x1) < eps and abs(z0-z1) < eps:
        base_y = min(y0,y1) if sign > 0 else min(y0,y1)-flute_length
        return Part.makeCylinder(
            radius, abs(y1-y0)+flute_length,
            App.Vector(x0,base_y,z0), App.Vector(0,1,0),
        )
    if abs(y0-y1) > eps or abs(z0-z1) > eps:
        raise ValueError("side-milling feed must be radial or parallel to X")
    base_y = y0 if sign > 0 else y0-flute_length
    first = Part.makeCylinder(radius,flute_length,App.Vector(x0,base_y,z0),App.Vector(0,1,0))
    second = Part.makeCylinder(radius,flute_length,App.Vector(x1,base_y,z0),App.Vector(0,1,0))
    bridge = Part.makeBox(abs(x1-x0),flute_length,2*radius,
                          App.Vector(min(x0,x1),base_y,z0-radius))
    return first.fuse([second,bridge])


def check_draft(target, stock, draft):
    sign = int(draft.get("access_sign",1 if float(draft["face_y_mm"]) > 0 else -1))
    radius = float(draft["tool_diameter_mm"])/2
    flute_length = float(draft["provisional_flute_length_mm"])
    checked = 0
    touching = 0
    summed = 0.0
    maximum = 0.0
    first_contacts = []
    removed_volume = 0.0
    previous = None
    for move in draft["moves"]:
        if previous is not None and move["kind"] == "feed":
            cutter = sweep(previous,move,radius,flute_length,sign)
            contact = target.common(cutter).Volume
            before = stock.Volume
            stock = stock.cut(cutter)
            removed_volume += max(before - stock.Volume, 0.0)
            checked += 1
            summed += contact
            maximum = max(maximum,contact)
            if contact > 0.0001:
                touching += 1
                if len(first_contacts) < 5:
                    first_contacts.append({
                        "move_index": checked, "layer": move["layer"],
                        "contact_mm3": round(contact,6),
                        "from_xyz_mm": previous["point"],
                        "to_xyz_mm": move["point"],
                    })
        previous = move
    return stock, {
        "analysis_face_id": draft["analysis_face_id"],
        "checked_feed_segments": checked,
        "contacting_segments": touching,
        "summed_target_contact_mm3": round(summed,6),
        "maximum_single_contact_mm3": round(maximum,6),
        "removed_stock_volume_mm3": round(removed_volume,6),
        "remaining_stock_volume_mm3": round(stock.Volume,6),
        "first_contacts": first_contacts,
    }


def main():
    source_path, drafts_json = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    drafts = json.loads(drafts_json)
    target_solids = Part.read(source_path).Solids
    if len(target_solids) != 1:
        raise ValueError(f"expected one STEP target solid, got {len(target_solids)}")
    target = target_solids[0]
    if not drafts:
        raise ValueError("at least one side-milling draft is required")
    bounds = target.BoundBox
    stock_radius = float(drafts[0]["stock_radius_mm"])
    if any(abs(float(draft["stock_radius_mm"]) - stock_radius) > 1e-9 for draft in drafts):
        raise ValueError("all side-milling drafts must share one stock radius")
    stock = Part.makeCylinder(
        stock_radius, bounds.XLength,
        App.Vector(bounds.XMin, 0, 0), App.Vector(1, 0, 0),
    )
    initial_stock_volume = stock.Volume
    x_values = [
        float(move["point"]["x"])
        for draft in drafts for move in draft.get("moves", [])
        if isinstance(move, dict) and isinstance(move.get("point"), dict)
    ]
    if not x_values:
        raise ValueError("side-milling drafts contain no cutter positions")
    region_min, region_max = min(x_values), max(x_values)
    machining_region = Part.makeBox(
        max(region_max-region_min, 0.001), 2*stock_radius+2, 2*stock_radius+2,
        App.Vector(region_min, -stock_radius-1, -stock_radius-1),
    )
    initial_excess_region = stock.cut(target).common(machining_region).Volume
    results = []
    for draft in drafts:
        stock, result = check_draft(target, stock, draft)
        results.append(result)
    print("CNC_SIDE_SWEEP " + json.dumps({
        "target_solid_valid": target.isValid(),
        "target_volume_mm3": round(target.Volume,6),
        "initial_stock_volume_mm3": round(initial_stock_volume,6),
        "remaining_stock_volume_mm3": round(stock.Volume,6),
        "removed_stock_volume_mm3": round(initial_stock_volume-stock.Volume,6),
        "missing_target_volume_mm3": round(target.cut(stock).Volume,6),
        "machining_region_x_mm": [round(region_min,6), round(region_max,6)],
        "initial_excess_region_mm3": round(initial_excess_region,6),
        "remaining_excess_region_mm3": round(stock.cut(target).common(machining_region).Volume,6),
        "checks": results,
    },separators=(",", ":")),flush=True)


if __name__ == "__main__":
    main()
