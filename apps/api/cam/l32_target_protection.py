"""Compare cumulative turning material against the complete source STEP solid."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, "/app/cam")
import Part
from side_hole_sweep import build_stock


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
        missing = target.cut(stock).Volume
        padded = [{**s, "outer_radius": s["outer_radius"]+tolerance,
                   "inner_radius": max(0, s["inner_radius"]-tolerance)}
                  for s in (stage.get("samples") or [])]
        # Padding is solely a declared radial geometric allowance, not new stock.
        protected = build_stock({**stock_payload, "samples": padded}) if padded else stock
        outside = target.cut(protected).Volume
        rows.append({"operation_id": stage["operation_id"],
            "missing_target_mm3": round(missing,6), "protected_missing_target_mm3":round(outside,6),
            "status":"failed" if outside > .0001 else "passed"})
    return {"executor":"exact_occ_full_step_containment", "status":"failed" if any(r["status"]=="failed" for r in rows) else "passed",
        "radial_tolerance_mm":tolerance,"numerical_volume_threshold_mm3":.0001,
        "target_volume_mm3":round(target.Volume,6),"stages":rows,
        "scope":"conservative_turning_envelope_vs_full_step_not_machine_collision"}


if __name__ == "__main__":
    source, payload = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    print("CNC_TARGET_PROTECTION "+json.dumps(check(source,json.loads(Path(payload).read_text(encoding="utf-8")))),flush=True)
