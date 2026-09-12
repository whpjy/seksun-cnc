from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.benchmarks import discover_example_cases
from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.recognizer import normalize_manufacturing_features


def evaluate_case(root: Path, case: dict[str, object], analyzer: str) -> dict[str, object]:
    relative_model = case.get("primary_model")
    if not isinstance(relative_model, str):
        return {**case, "status": "failed", "error": "missing STEP/STP model"}
    source = root / str(case["relative_directory"]) / relative_model
    with tempfile.TemporaryDirectory(prefix="seksun-case-") as temporary:
        analysis_path = Path(temporary) / "analysis.json"
        model_path = Path(temporary) / "model.stl"
        subprocess.run(
            [analyzer, str(source), str(analysis_path), str(model_path)],
            check=True, capture_output=True, text=True, timeout=180,
        )
        analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        analysis = normalize_manufacturing_features(analysis)
        plan = build_process_plan(
            analysis, "6061-T6 铝合金",
            "VMC850 三轴立式加工中心（FANUC 0i-MF Plus）",
        )
    bounds = analysis.measurements["bounding_box"]
    coverage = plan.coverage
    return {
        **case,
        "status": "completed",
        "geometry": {
            "topology": analysis.topology,
            "size_mm": bounds.size.model_dump(mode="json"),
            "planar_feature_count": len(analysis.planar_features),
            "cylindrical_feature_count": len(analysis.cylindrical_features),
            "prismatic_feature_count": len(analysis.prismatic_features),
        },
        "plan": {
            "process_kind": plan.process_kind,
            "automation_status": plan.automation_status,
            "setup_count": len(plan.setups),
            "operation_count": sum(len(setup.operations) for setup in plan.setups),
            "operation_types": sorted({
                operation.type for setup in plan.setups for operation in setup.operations
            }),
            "blocking_reasons": plan.blocking_reasons,
        },
        "coverage": coverage.model_dump(mode="json") if coverage else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate paired 2D/3D manufacturing examples")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--analyzer", default=os.getenv("CNC_ANALYZER_BIN", "occt-analyzer"))
    arguments = parser.parse_args()
    cases = discover_example_cases(arguments.root)
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        try:
            results.append(evaluate_case(arguments.root, case, arguments.analyzer))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results.append({**case, "status": "failed", "error": str(error)[-1000:]})
    completed = [item for item in results if item["status"] == "completed"]
    coverages = [item["coverage"] for item in completed if isinstance(item.get("coverage"), dict)]
    report = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(arguments.root),
        "summary": {
            "case_count": len(results),
            "completed_count": len(completed),
            "failed_count": len(results) - len(completed),
            "paired_case_count": sum(bool(item.get("paired")) for item in results),
            "complete_coverage_count": sum(item["status"] == "complete" for item in coverages),
            "average_coverage_score": round(
                sum(float(item["score"]) for item in coverages) / max(len(coverages), 1), 4,
            ),
        },
        "cases": results,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if len(completed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
