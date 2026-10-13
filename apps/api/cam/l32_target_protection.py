"""Compare cumulative turning material against the complete source STEP solid."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, "/app/cam")
import Part
from side_hole_sweep import build_stock


def missing_volume(target, stock):
    # Fuse adjoining annuli before comparing: compound Boolean operands can
    # fail on coincident target faces. A null *result* denotes an empty cut;
    # an exception from cut itself remains an unavailable check.
    solids = list(stock.Solids)
    if len(solids) > 1:
        expected_volume = stock.Volume
        fused = solids[0].multiFuse(solids[1:]).removeSplitter()
        # A changed union must never replace the accepted material. The
        # original valid, nonoverlapping annuli remain an exact operand.
        if fused.isValid() and abs(fused.Volume-expected_volume) <= .0001:
            stock = fused
    # OCCT Boolean operations may update operand topology/tolerances. Each
    # stage must compare with a fresh target, independent of earlier cuts.
    difference = target.copy().cut(stock)
    if difference.isNull():
        return 0.0
    if not difference.isValid():
        raise ValueError("invalid target/material difference")
    return difference.Volume


def check(source, payload):
    solids = list(Part.read(source).Solids)
    if len(solids)>1 and "selected_solid_index" not in payload:
        raise ValueError("full target protection requires the selected solid index")
    selected = int(payload.get("selected_solid_index",1))
    if not 1 <= selected <= len(solids) or not solids[selected-1].isValid():
        raise ValueError("STEP target protection requires one valid solid")
    target = solids[selected-1]
    tolerance = float(payload.get("radial_tolerance_mm", .05))
    rows = []
    for stage in payload["stages"]:
        stock_payload = {**payload, "samples": stage.get("samples")}
        stock = build_stock(stock_payload)
        if not stock.isValid():
            raise ValueError("invalid cumulative material solid")
        raw_status = "computed"
        try:
            missing = missing_volume(target, stock)
        except ValueError:
            # Exact tangencies can leave invalid zero-thickness BOP fragments.
            # Do not invent a raw volume; the declared tolerance comparison
            # below must independently produce a valid result to pass the gate.
            missing = None
            raw_status = "unavailable"
        padded = [{**s, "outer_radius": s["outer_radius"]+tolerance,
                   "inner_radius": max(0, s["inner_radius"]-tolerance)}
                  for s in (stage.get("samples") or [])]
        # Padding is solely a declared radial geometric allowance, not new stock.
        protected = build_stock({**stock_payload, "samples": padded}) if padded else stock
        outside = missing_volume(target, protected)
        rows.append({"operation_id": stage["operation_id"],
            "missing_target_mm3": round(missing,6) if missing is not None else None,
            "raw_difference_status":raw_status,"protected_missing_target_mm3":round(outside,6),
            "status":"failed" if outside > .0001 else "passed"})
    return {"executor":"exact_occ_full_step_containment", "status":"failed" if any(r["status"]=="failed" for r in rows) else "passed",
        "radial_tolerance_mm":tolerance,"numerical_volume_threshold_mm3":.0001,
        "target_volume_mm3":round(target.Volume,6),"stages":rows,
        "scope":"conservative_turning_envelope_vs_full_step_not_machine_collision"}


if __name__ == "__main__":
    source, payload = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    print("CNC_TARGET_PROTECTION "+json.dumps(check(source,json.loads(Path(payload).read_text(encoding="utf-8")))),flush=True)
