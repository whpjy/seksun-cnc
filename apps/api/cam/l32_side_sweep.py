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


def check_draft(target, draft):
    sign = int(draft.get("access_sign",1 if float(draft["face_y_mm"]) > 0 else -1))
    radius = float(draft["tool_diameter_mm"])/2
    flute_length = float(draft["provisional_flute_length_mm"])
    checked = 0
    touching = 0
    summed = 0.0
    maximum = 0.0
    first_contacts = []
    previous = None
    for move in draft["moves"]:
        if previous is not None and move["kind"] == "feed":
            cutter = sweep(previous,move,radius,flute_length,sign)
            contact = target.common(cutter).Volume
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
    return {
        "analysis_face_id": draft["analysis_face_id"],
        "checked_feed_segments": checked,
        "contacting_segments": touching,
        "summed_target_contact_mm3": round(summed,6),
        "maximum_single_contact_mm3": round(maximum,6),
        "first_contacts": first_contacts,
    }


def main():
    source_path, drafts_json = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    drafts = json.loads(drafts_json)
    target_solids = Part.read(source_path).Solids
    if len(target_solids) != 1:
        raise ValueError(f"expected one STEP target solid, got {len(target_solids)}")
    target = target_solids[0]
    results = [check_draft(target,draft) for draft in drafts]
    print("CNC_SIDE_SWEEP " + json.dumps({
        "target_solid_valid": target.isValid(),
        "target_volume_mm3": round(target.Volume,6),
        "checks": results,
    },separators=(",", ":")),flush=True)


if __name__ == "__main__":
    main()
