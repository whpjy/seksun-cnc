"""OCC check of provisional narrow radial-groove strips against original STEP.

The annular volumes model a rotating bar and full-width radial plunge. They
do not model insert edge shape, tool holder, fixture, or machine NC.
"""

from __future__ import annotations

import json
import os

import FreeCAD as App
import Part


def main():
    source_path, draft_json, axis_json = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    draft = json.loads(draft_json)
    axis = json.loads(axis_json)
    if not draft["reference_only"] or draft["nc_generated"] or draft["tool_catalog_match"]:
        raise ValueError("expected an unbound reference-only groove geometry draft")
    direction = axis["direction"]
    origin = axis["origin"]
    if (
        abs(float(direction["x"])-1) > 1e-8
        or abs(float(direction["y"])) > 1e-8
        or abs(float(direction["z"])) > 1e-8
        or abs(float(origin["y"])) > 1e-8
        or abs(float(origin["z"])) > 1e-8
    ):
        raise ValueError("groove sweep currently requires a +X axis through global Y=Z=0")
    solids = list(Part.read(source_path).Solids)
    if len(solids) != 1:
        raise ValueError(f"expected one original target solid, got {len(solids)}")
    target = solids[0]
    bounds = target.BoundBox
    radius = float(draft["stock_radius_mm"])
    stock = Part.makeCylinder(
        radius, bounds.XMax-bounds.XMin,
        App.Vector(bounds.XMin,0,0), App.Vector(1,0,0),
    )
    initial_volume = stock.Volume
    summed_contact = 0.0
    maximum_contact = 0.0
    for strip in draft["strips"]:
        left = float(origin["x"])+float(strip["z_min_mm"])
        width = float(strip["z_max_mm"])-float(strip["z_min_mm"])
        cut_to = float(strip["cut_to_radius_mm"])
        if width <= 0 or cut_to <= 0 or cut_to >= radius:
            raise ValueError("invalid groove cutter envelope")
        base = App.Vector(left,0,0)
        axis_vector = App.Vector(1,0,0)
        outer = Part.makeCylinder(radius+0.1,width,base,axis_vector)
        inner = Part.makeCylinder(cut_to,width,base,axis_vector)
        cutter = outer.cut(inner)
        contact = target.common(cutter).Volume
        summed_contact += contact
        maximum_contact = max(maximum_contact,contact)
        stock = stock.cut(cutter)
    print("CNC_FRONT_GROOVE_SWEEP " + json.dumps({
        "target_solid_valid": target.isValid(),
        "remaining_stock_valid": stock.isValid(),
        "strip_count": len(draft["strips"]),
        "initial_stock_volume_mm3": round(initial_volume,6),
        "removed_volume_mm3": round(initial_volume-stock.Volume,6),
        "remaining_stock_volume_mm3": round(stock.Volume,6),
        "summed_target_contact_mm3": round(summed_contact,9),
        "maximum_strip_target_contact_mm3": round(maximum_contact,9),
        "missing_target_volume_mm3": round(target.cut(stock).Volume,6),
        "excess_stock_volume_mm3": round(stock.cut(target).Volume,6),
        "nc_generated": False,
    },separators=(",", ":")),flush=True)


if __name__ == "__main__":
    main()
