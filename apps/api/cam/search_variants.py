from __future__ import annotations

import argparse
import json
import shutil
from copy import deepcopy
from pathlib import Path

from app.conformance import compare_stock_to_target_mesh
from app.engines import run_freecad_adapter
from app.models import GeometryAnalysis, ProcessPlan
from app.simulation import simulate_material_removal


VARIANTS = {
    "no-rough-standard": {
        "surface_diameter": 2.5,
        "surface_stepover": 0.35,
        "waterline_diameter": 1.5,
        "waterline_stepover_percent": 12,
        "waterline_stepdown": 0.18,
    },
    "micro-1.0-0.5": {
        "surface_diameter": 1.0,
        "surface_stepover": 0.15,
        "waterline_diameter": 0.5,
        "waterline_stepover_percent": 10,
        "waterline_stepdown": 0.08,
    },
    "micro-0.5-0.3": {
        "surface_diameter": 0.5,
        "surface_stepover": 0.08,
        "waterline_diameter": 0.3,
        "waterline_stepover_percent": 10,
        "waterline_stepdown": 0.05,
    },
    "bounded-2.5-1.5": {
        "surface_diameter": 2.5,
        "surface_stepover": 0.25,
        "surface_boundary_enforcement": True,
        "waterline_diameter": 1.5,
        "waterline_stepover_percent": 10,
        "waterline_stepdown": 0.12,
    },
    "protected-rough-1.0": {
        "keep_roughing": True,
        "rough_diameter": 1.0,
        "rough_offset": 0.8,
        "rough_stepover_percent": 30,
        "rough_stepdown": 0.4,
        "surface_diameter": 2.5,
        "surface_stepover": 0.25,
        "surface_boundary_enforcement": True,
        "waterline_diameter": 1.5,
        "waterline_stepover_percent": 10,
        "waterline_stepdown": 0.12,
    },
    "balanced-3.0-1.5": {
        "surface_diameter": 3.0,
        "surface_stepover": 0.15,
        "waterline_diameter": 1.5,
        "waterline_stepover_percent": 8,
        "waterline_stepdown": 0.1,
    },
}


def configure_plan(base: dict[str, object], settings: dict[str, float]) -> dict[str, object]:
    plan = deepcopy(base)
    for setup in plan["setups"]:
        if not settings.get("keep_roughing", False):
            setup["operations"] = [
                operation
                for operation in setup["operations"]
                if operation["type"] != "surface_roughing"
            ]
        for operation in setup["operations"]:
            if operation["type"] == "surface_roughing":
                diameter = settings["rough_diameter"]
                operation["tool"].update({
                    "id": f"EM-{diameter:g}",
                    "name": f"Ø{diameter:g} protected-area end mill",
                    "diameter_mm": diameter,
                    "flute_length_mm": min(float(operation["tool"]["flute_length_mm"]), 5.0),
                    "stickout_mm": min(float(operation["tool"]["stickout_mm"]), 10.0),
                    "holder_diameter_mm": min(float(operation["tool"]["holder_diameter_mm"]), 6.0),
                    "catalog_match": False,
                })
                operation["parameters"].update({
                    "depth_offset_mm": settings["rough_offset"],
                    "step_over_percent": settings["rough_stepover_percent"],
                    "step_down_mm": settings["rough_stepdown"],
                    "boundary_enforcement": False,
                })
            elif operation["type"] == "surface_3d":
                diameter = settings["surface_diameter"]
                operation["tool"].update({
                    "id": f"BM-{diameter:g}",
                    "name": f"Ø{diameter:g} micro ball end mill",
                    "diameter_mm": diameter,
                    "flute_length_mm": min(float(operation["tool"]["flute_length_mm"]), 4.0),
                    "stickout_mm": min(float(operation["tool"]["stickout_mm"]), 8.0),
                    "holder_diameter_mm": min(float(operation["tool"]["holder_diameter_mm"]), 6.0),
                    "catalog_match": False,
                })
                operation["parameters"]["step_over_mm"] = settings["surface_stepover"]
                operation["parameters"]["boundary_enforcement"] = settings.get(
                    "surface_boundary_enforcement", False
                )
            elif operation["type"] == "waterline":
                diameter = settings["waterline_diameter"]
                operation["tool"].update({
                    "id": f"BM-{diameter:g}",
                    "name": f"Ø{diameter:g} micro ball end mill",
                    "diameter_mm": diameter,
                    "flute_length_mm": min(float(operation["tool"]["flute_length_mm"]), 3.0),
                    "stickout_mm": min(float(operation["tool"]["stickout_mm"]), 6.0),
                    "holder_diameter_mm": min(float(operation["tool"]["holder_diameter_mm"]), 4.0),
                    "catalog_match": False,
                })
                operation["parameters"]["step_over_percent"] = settings["waterline_stepover_percent"]
                operation["parameters"]["step_down_mm"] = settings["waterline_stepdown"]
    plan["automation_status"] = "review"
    plan["blocking_reasons"] = []
    return plan


def run_variant(
    job_dir: Path,
    output_root: Path,
    name: str,
    settings: dict[str, float],
    adapter_path: Path,
) -> dict[str, object]:
    variant_dir = output_root / name
    if variant_dir.exists():
        shutil.rmtree(variant_dir)
    variant_dir.mkdir(parents=True)
    source = next(job_dir.glob("*.stp"))
    for filename in (source.name, "analysis.json", "model.stl"):
        shutil.copy2(job_dir / filename, variant_dir / filename)
    base_plan = json.loads((job_dir / "plan.json").read_text(encoding="utf-8"))
    plan_data = configure_plan(base_plan, settings)
    plan_path = variant_dir / "plan.json"
    plan_path.write_text(json.dumps(plan_data, ensure_ascii=False, indent=2), encoding="utf-8")
    arguments = (
        variant_dir / source.name,
        variant_dir / "analysis.json",
        plan_path,
        variant_dir / "cam.FCStd",
        variant_dir / "program.nc",
        variant_dir / "toolpath.json",
    )
    print(f"START {name}", flush=True)
    run_freecad_adapter("FreeCADCmd", adapter_path, arguments, timeout_seconds=1800)
    analysis = GeometryAnalysis.model_validate_json((variant_dir / "analysis.json").read_text(encoding="utf-8"))
    plan = ProcessPlan.model_validate(plan_data)
    toolpath = json.loads((variant_dir / "toolpath.json").read_text(encoding="utf-8"))
    simulation = simulate_material_removal(analysis, plan, toolpath)
    spatial = compare_stock_to_target_mesh(simulation["surface"], variant_dir / "model.stl")
    result = {
        "variant": name,
        "settings": settings,
        "operation_count": len(toolpath["generated_operations"]),
        "path_command_count": toolpath["path_command_count"],
        "cut_segment_count": simulation["metrics"]["cut_segment_count"],
        "remaining_volume_mm3": simulation["metrics"]["remaining_volume_mm3"],
        **spatial,
    }
    (variant_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--adapter", type=Path, default=Path("/app/cam/freecad_adapter.py"))
    parser.add_argument("--variants", nargs="*", choices=VARIANTS, default=list(VARIANTS))
    args = parser.parse_args()
    output_root = args.job_dir / ".cam-search"
    output_root.mkdir(exist_ok=True)
    results = [
        run_variant(args.job_dir, output_root, name, VARIANTS[name], args.adapter)
        for name in args.variants
    ]
    (output_root / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
