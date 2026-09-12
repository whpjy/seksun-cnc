from app.models import GeometryAnalysis
from app.planner import build_process_plan


def test_coverage_exposes_unclassified_internal_profile_and_missing_outer_profile() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.4.0",
        "source_file": "multi-loop-plate.step",
        "topology": {"source_solids": 2, "selected_solids": 1, "solids": 1, "faces": 8},
        "measurements": {
            "volume": 800,
            "surface_area": 1200,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 40, "y": 2, "z": 20},
                "size": {"x": 40, "y": 2, "z": 20},
            },
        },
        "planar_features": [{
            "id": "PF-TOP", "area": 700, "center": {"x": 20, "y": 2, "z": 10},
            "normal": {"x": 0, "y": 1, "z": 0}, "wire_count": 3,
        }],
        "cylindrical_features": [{
            "id": "HF-1", "kind": "hole", "radius": 2.5, "diameter": 5,
            "length": 2, "center": {"x": 10, "y": 1, "z": 10},
            "axis": {"x": 0, "y": 1, "z": 0}, "end_type": "through",
            "access_direction": {"x": 0, "y": 1, "z": 0},
            "confidence": 0.95, "review_state": "accepted",
        }],
        "prismatic_features": [],
    })

    plan = build_process_plan(analysis, "6061-T6 铝合金", "VMC850 三轴立式加工中心")

    assert plan.coverage is not None
    assert plan.coverage.status == "incomplete"
    assert plan.coverage.production_ready is False
    assert plan.coverage.unresolved_count == 1
    states = {target.id: target.state for target in plan.coverage.targets}
    assert states["TARGET-HF-1"] == "covered"
    assert states["TARGET-OUTER-PROFILE"] == "uncovered"
    assert states["TARGET-UNRESOLVED-INTERNAL-1"] == "unresolved"
    assert any("2 个实体" in issue for issue in plan.coverage.issues)
