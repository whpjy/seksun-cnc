from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.preflight import verify_cam


def thin_surface_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.4.0",
        "source_file": "thin-surface.step",
        "topology": {"solids": 1, "faces": 2, "edges": 4},
        "measurements": {
            "surface_area": 12000.0,
            "volume": 9000.0,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 100, "y": 60, "z": 3},
                "size": {"x": 100, "y": 60, "z": 3},
            },
        },
        "planar_features": [{
            "id": "PF-TOP",
            "area": 4000,
            "center": {"x": 50, "y": 30, "z": 3},
            "normal": {"x": 0, "y": 0, "z": 1},
            "wire_count": 1,
        }],
        "cylindrical_features": [],
        "prismatic_features": [],
    })


def cam_result_for(plan) -> dict:
    return {
        "generated_operations": [
            operation.id for setup in plan.setups for operation in setup.operations
        ],
        "preview_segments": [],
        "postprocessor": plan.machine_profile.postprocessor,
        "skipped": [],
    }


def test_surface_strategy_coverage_requires_waterline_for_each_side() -> None:
    plan = build_process_plan(thin_surface_analysis(), "6061-T6", "VMC850")
    complete = verify_cam(plan, cam_result_for(plan))
    complete_check = next(
        check for check in complete["checks"]
        if check["id"] == "surface_strategy_coverage"
    )
    assert complete_check["status"] == "passed"

    plan.setups[0].operations = [
        operation for operation in plan.setups[0].operations
        if operation.type != "waterline"
    ]
    incomplete = verify_cam(plan, cam_result_for(plan))
    incomplete_check = next(
        check for check in incomplete["checks"]
        if check["id"] == "surface_strategy_coverage"
    )
    assert incomplete_check["status"] == "failed"
    assert incomplete["status"] == "failed"


def test_surface_strategy_coverage_requires_small_ball_rest_finishing() -> None:
    plan = build_process_plan(thin_surface_analysis(), "6061-T6", "VMC850")
    plan.setups[0].operations = [
        operation for operation in plan.setups[0].operations
        if operation.parameters.get("rest_machining") is not True
    ]
    result = verify_cam(plan, cam_result_for(plan))
    check = next(
        item for item in result["checks"]
        if item["id"] == "surface_strategy_coverage"
    )
    assert check["status"] == "failed"
    assert "small_ball_rest_finishing" in check["message"]
