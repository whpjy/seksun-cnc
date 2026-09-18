"""Cumulative OCC stock subtraction for catalog side and back-pocket drafts.

This is geometry-only: turning, holders, fixture, transfer and NC are absent.
Arguments: [original STEP, JSON side drafts, JSON pocket draft].
"""

from __future__ import annotations

import json
import os
import sys
import base64
import zlib

import FreeCAD as App
import Part

sys.path.insert(0, "/app/cam")
from l32_side_sweep import sweep as side_sweep
from l32_pocket_sweep import cutter_sweep as pocket_sweep


def main():
    args = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    source_path, side_json, pocket_json = args[:3]
    def parse_payload(value):
        if value.startswith("zlib64:"):
            return json.loads(zlib.decompress(base64.b64decode(value[7:])).decode("utf-8"))
        return json.loads(value)
    sides = parse_payload(side_json)
    pocket = parse_payload(pocket_json)
    exteriors = parse_payload(args[4]) if len(args) > 4 else []
    if len(sides) != 2 or any(item.get("nc_generated") for item in sides) or pocket.get("nc_generated"):
        raise ValueError("expected two geometric side drafts and one back-pocket draft")
    if any(item.get("mode") != "exterior_clear" or item.get("nc_generated") for item in exteriors):
        raise ValueError("invalid exterior clearing draft")
    solids = list(Part.read(source_path).Solids)
    if len(solids) != 1:
        raise ValueError(f"expected one target solid, got {len(solids)}")
    target = solids[0]
    bounds = target.BoundBox
    radius = float(sides[0]["stock_radius_mm"])
    stock = Part.makeCylinder(
        radius, bounds.XMax-bounds.XMin,
        App.Vector(bounds.XMin,0,0),App.Vector(1,0,0),
    )
    initial_volume = stock.Volume
    initial_missing = target.cut(stock).Volume
    stages = []

    def apply_stage(name, moves, make_sweep):
        nonlocal stock
        before = stock.Volume
        feed_segments = 0
        previous = None
        for move in moves:
            if previous is not None and move["kind"] == "feed":
                tool = make_sweep(previous,move)
                stock = stock.cut(tool)
                feed_segments += 1
                if feed_segments % 100 == 0:
                    print("CNC_PROGRESS " + json.dumps({"stage":name,"feed_segments":feed_segments}),flush=True)
            previous = move
        if not stock.isValid() or stock.Volume > before + 1e-5:
            raise ValueError(f"invalid cumulative stock after {name}")
        stages.append({
            "operation": name,
            "feed_segments": feed_segments,
            "removed_volume_mm3": round(before-stock.Volume,6),
            "remaining_stock_volume_mm3": round(stock.Volume,6),
            "missing_target_volume_mm3": round(target.cut(stock).Volume,6),
            "excess_stock_volume_mm3": round(stock.cut(target).Volume,6),
            "solid_count": len(stock.Solids),
        })

    for side in [*exteriors,*sides]:
        sign = int(side.get("access_sign",1 if float(side["face_y_mm"]) > 0 else -1))
        radius_tool = float(side["tool_diameter_mm"])/2
        flute = float(side["provisional_flute_length_mm"])
        apply_stage(
            ("EXT-" if side.get("mode") == "exterior_clear" else "") + side["analysis_face_id"],side["moves"],
            lambda a,b: side_sweep(a,b,radius_tool,flute,sign),
        )
    radius_tool = float(pocket["tool_diameter_mm"])/2
    flute = float(pocket["provisional_flute_length_mm"])
    apply_stage(
        pocket["feature_id"],pocket["moves"],
        lambda a,b: pocket_sweep(a,b,radius_tool,flute),
    )
    slab_boundaries = (
        json.loads(args[3]) if len(args) > 3
        else [bounds.XMin, bounds.XMax]
    )
    if (
        len(slab_boundaries) < 2
        or abs(slab_boundaries[0]-bounds.XMin) > 0.001
        or abs(slab_boundaries[-1]-bounds.XMax) > 0.001
        or any(b <= a for a,b in zip(slab_boundaries,slab_boundaries[1:]))
    ):
        raise ValueError("slab boundaries must partition the complete target X span")
    final_excess = stock.cut(target)
    final_missing = target.cut(stock)
    axial_slabs = []
    for left,right in zip(slab_boundaries,slab_boundaries[1:]):
        slab = Part.makeBox(right-left,2*radius+2,2*radius+2,
                            App.Vector(left,-radius-1,-radius-1))
        axial_slabs.append({
            "x_min_mm":round(left,6),"x_max_mm":round(right,6),
            "target_volume_mm3":round(target.common(slab).Volume,6),
            "remaining_stock_volume_mm3":round(stock.common(slab).Volume,6),
            "excess_stock_volume_mm3":round(final_excess.common(slab).Volume,6),
            "missing_target_volume_mm3":round(final_missing.common(slab).Volume,6),
        })
    print("CNC_PARTIAL_CUMULATIVE " + json.dumps({
        "coordinate_frame":"source_step_xyz_mm",
        "initial_stock_volume_mm3":round(initial_volume,6),
        "target_volume_mm3":round(target.Volume,6),
        "initial_missing_target_volume_mm3":round(initial_missing,6),
        "stages":stages,
        "axial_slabs":axial_slabs,
        "remaining_stock_valid":stock.isValid(),
        "nc_generated":False,
    },separators=(",", ":")),flush=True)


if __name__ == "__main__":
    main()
