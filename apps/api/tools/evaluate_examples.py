from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.benchmarks import discover_example_cases, example_catalog_payload
from app.groove_binding import assess_case_grooves
from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.recognizer import normalize_manufacturing_features
from app.rotational_features import infer_rotational_features


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assess_expectation(case: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    expected = case.get("expected")
    if not isinstance(expected, dict) or not expected:
        return {"status": "not_defined", "checks": []}
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    stock = plan.get("stock") if isinstance(plan.get("stock"), dict) else {}
    rotational = result.get("rotational") if isinstance(result.get("rotational"), dict) else {}
    checks: list[dict[str, object]] = []

    def check(name: str, expected_value: object, actual_value: object, passed: bool) -> None:
        checks.append({
            "name": name,
            "expected": expected_value,
            "actual": actual_value,
            "passed": passed,
        })

    part_family = expected.get("part_family")
    if part_family == "non_rotational":
        check("part_family", part_family, rotational.get("status"), rotational.get("status") == "not_rotational")
    elif part_family == "requires_solid_selection":
        check(
            "part_family", part_family, rotational.get("status"),
            rotational.get("status") == "solid_selection_required",
        )
    elif part_family in {"rotational", "rotational_mill_turn"}:
        check("part_family", part_family, rotational.get("status"), rotational.get("status") == "candidate")

    capability = expected.get("machine_capability")
    if capability == "standard":
        check(
            "machine_capability", capability, {
                "plan_status": plan.get("automation_status"),
                "required_option": stock.get("required_option"),
            },
            plan.get("automation_status") != "unsupported" and not stock.get("required_option"),
        )
    elif capability == "bar_diameter_38mm_option":
        check(
            "machine_capability", capability, stock.get("required_option"),
            stock.get("required_option") == "bar_diameter_38mm",
        )
    elif capability == "unsupported":
        check(
            "machine_capability", capability, plan.get("automation_status"),
            plan.get("automation_status") == "unsupported",
        )

    return {
        "status": "passed" if checks and all(bool(item["passed"]) for item in checks) else "failed",
        "checks": checks,
    }


def evaluate_case(
    root: Path,
    case: dict[str, object],
    analyzer: str,
    material: str,
    machine: str,
) -> dict[str, object]:
    relative_model = case.get("primary_model")
    if not isinstance(relative_model, str):
        return {**case, "status": "failed", "error": "missing STEP/STP model"}
    source = root / str(case["relative_directory"]) / relative_model
    if not source.is_file():
        return {**case, "status": "failed", "error": f"model not found: {source}"}
    drawing_hashes: dict[str, str] = {}
    for drawing in case.get("drawing_files", []):
        drawing_path = root / str(case["relative_directory"]) / str(drawing)
        if drawing_path.is_file():
            drawing_hashes[str(drawing)] = file_sha256(drawing_path)
    with tempfile.TemporaryDirectory(prefix="seksun-case-") as temporary:
        analysis_path = Path(temporary) / "analysis.json"
        model_path = Path(temporary) / "model.stl"
        subprocess.run(
            [analyzer, str(source), str(analysis_path), str(model_path)],
            check=True, capture_output=True, text=True, timeout=180,
        )
        analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        analysis = normalize_manufacturing_features(analysis)
        plan = build_process_plan(analysis, material, machine)
    bounds = analysis.measurements["bounding_box"]
    coverage = plan.coverage
    result = {
        **case,
        "status": "completed",
        "input_hashes": {"model_sha256": file_sha256(source), "drawings": drawing_hashes},
        "geometry": {
            "topology": analysis.topology,
            "solid_candidate_count": len(analysis.solid_candidates),
            "solid_candidates": [
                candidate.model_dump(mode="json") for candidate in analysis.solid_candidates
            ],
            "size_mm": bounds.size.model_dump(mode="json"),
            "planar_feature_count": len(analysis.planar_features),
            "cylindrical_feature_count": len(analysis.cylindrical_features),
            "prismatic_feature_count": len(analysis.prismatic_features),
            "internal_profile_feature_count": len(analysis.internal_profile_features),
            "internal_profile_kinds": {
                kind: sum(feature.machining_kind == kind for feature in analysis.internal_profile_features)
                for kind in ("through_profile", "blind_pocket", "engraving", "unknown")
            },
        },
        "plan": {
            "process_kind": plan.process_kind,
            "automation_status": plan.automation_status,
            "setup_count": len(plan.setups),
            "operation_count": sum(len(setup.operations) for setup in plan.setups),
            "operation_types": sorted({
                operation.type for setup in plan.setups for operation in setup.operations
            }),
            "stock": plan.stock,
            "blocking_reasons": plan.blocking_reasons,
        },
        "coverage": coverage.model_dump(mode="json") if coverage else None,
    }
    if plan.machine_profile and plan.machine_profile.id == "citizen-cincom-l32":
        rotational = infer_rotational_features(analysis)
        result["rotational"] = {
            "status": rotational.status,
            "axis_count": len(rotational.axes),
            "profile_count": len(rotational.profiles),
            "profile_methods": sorted({item.extraction_method for item in rotational.profiles}),
            "profile_point_count": sum(len(item.points) for item in rotational.profiles),
            "feature_count": len(rotational.features),
            "feature_kinds": {
                kind: sum(item.kind == kind for item in rotational.features)
                for kind in sorted({item.kind for item in rotational.features})
            },
            "evidence": rotational.evidence,
            "warnings": rotational.warnings,
        }
        result["grooves"] = assess_case_grooves(case, rotational.features)
        result["expectation"] = assess_expectation(case, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate paired 2D/3D manufacturing examples")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--analyzer", default=os.getenv("CNC_ANALYZER_BIN", "occt-analyzer"))
    parser.add_argument("--material", default="6061-T6 铝合金")
    parser.add_argument("--machine", default="VMC850 三轴立式加工中心（FANUC 0i-MF Plus）")
    arguments = parser.parse_args()
    cases = discover_example_cases(arguments.root, arguments.manifest)
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        try:
            results.append(evaluate_case(
                arguments.root, case, arguments.analyzer, arguments.material, arguments.machine,
            ))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results.append({**case, "status": "failed", "error": str(error)[-1000:]})
    completed = [item for item in results if item["status"] == "completed"]
    coverages = [item["coverage"] for item in completed if isinstance(item.get("coverage"), dict)]
    expectations = [
        item["expectation"] for item in completed
        if isinstance(item.get("expectation"), dict)
        and item["expectation"].get("status") != "not_defined"
    ]
    groove_assessments = [
        item["grooves"] for item in completed if isinstance(item.get("grooves"), dict)
    ]
    catalog = example_catalog_payload(arguments.root, arguments.manifest)
    report = {
        "schema_version": "1.1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(arguments.root),
        "manifest": catalog.get("manifest"),
        "provenance": {
            "analyzer": arguments.analyzer,
            "machine": arguments.machine,
            "material": arguments.material,
            "code_revision": os.getenv("CNC_BUILD_REVISION") or None,
        },
        "summary": {
            "case_count": len(results),
            "completed_count": len(completed),
            "failed_count": len(results) - len(completed),
            "paired_case_count": sum(bool(item.get("paired")) for item in results),
            "expectation_passed_count": sum(item.get("status") == "passed" for item in expectations),
            "expectation_failed_count": sum(item.get("status") == "failed" for item in expectations),
            "groove_required_case_count": sum(
                item.get("status") != "not_required" for item in groove_assessments
            ),
            "groove_matched_case_count": sum(
                item.get("status") == "matched" for item in groove_assessments
            ),
            "groove_ambiguous_case_count": sum(
                item.get("status") == "ambiguous" for item in groove_assessments
            ),
            "groove_missing_case_count": sum(
                item.get("status") == "missing" for item in groove_assessments
            ),
            "external_groove_candidate_count": sum(
                int((item.get("summary") or {}).get("external_candidate_count", 0))
                for item in groove_assessments
            ),
            "internal_groove_candidate_count": sum(
                int((item.get("summary") or {}).get("internal_candidate_count", 0))
                for item in groove_assessments
            ),
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
