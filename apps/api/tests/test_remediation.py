from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.preflight import verify_cam
from app.remediation import apply_automatic_remediation, build_remediation_report


def sample_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.8.0",
        "source_file": "remediation.step",
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
            "id": "PF-TOP", "area": 4000,
            "center": {"x": 50, "y": 30, "z": 3},
            "normal": {"x": 0, "y": 0, "z": 1}, "wire_count": 1,
        }],
        "cylindrical_features": [],
        "prismatic_features": [],
    })


def cam_result(plan) -> dict:
    return {
        "generated_operations": [
            operation.id for setup in plan.setups for operation in setup.operations
        ],
        "preview_segments": [],
        "postprocessor": plan.machine_profile.postprocessor,
        "skipped": [],
    }


def empty_collision() -> dict:
    return {"status": "passed", "collisions": [], "low_rapids": []}


def test_missing_toolpath_is_linked_to_operation_features_and_action() -> None:
    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    result = cam_result(plan)
    missing_operation = plan.setups[0].operations[-1]
    result["generated_operations"].remove(missing_operation.id)
    verification = verify_cam(plan, result)

    report = build_remediation_report(analysis, plan, result, verification, empty_collision())

    defect = next(item for item in report["defects"] if item["kind"] == "missing_toolpath")
    assert report["status"] == "action_required"
    assert defect["operation_ids"] == [missing_operation.id]
    assert defect["feature_ids"] == sorted(missing_operation.feature_ids)
    assert any(item["operation_id"] == missing_operation.id for item in report["actions"])


def test_spatial_overcut_blocks_automatic_replan_but_residual_adds_action() -> None:
    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    result = cam_result(plan)
    verification = verify_cam(plan, result)
    verification["metrics"].update({
        "missing_target_volume_mm3": 12.5,
        "excess_stock_volume_mm3": 31.25,
    })

    report = build_remediation_report(analysis, plan, result, verification, empty_collision())

    assert report["status"] == "blocked"
    assert report["can_auto_replan"] is False
    assert {item["kind"] for item in report["defects"]} >= {"overcut", "excess_stock"}
    assert any(item["kind"] == "add_rest_machining" for item in report["actions"])


def test_low_rapid_is_traced_to_operation_and_proposes_clearance_change() -> None:
    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    result = cam_result(plan)
    verification = verify_cam(plan, result)
    operation = plan.setups[0].operations[0]
    collision = {
        "status": "failed",
        "collisions": [],
        "low_rapids": [{
            "operation_id": operation.id,
            "setup_id": plan.setups[0].id,
            "minimum_z": 1.0,
            "required_z": 5.0,
        }],
    }

    report = build_remediation_report(analysis, plan, result, verification, collision)

    defect = next(item for item in report["defects"] if item["kind"] == "low_rapid")
    action = next(item for item in report["actions"] if item["kind"] == "adjust_safety")
    assert operation.id in defect["operation_ids"]
    assert report["can_auto_replan"] is True
    assert action["auto_applicable"] is True
    assert action["parameters"]["clearance_mm"] > plan.safety.clearance_mm


def test_automatic_remediation_applies_safe_clearance_and_invalidates_operations() -> None:
    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    operation = plan.setups[0].operations[0]
    for setup in plan.setups:
        for item in setup.operations:
            item.status = "approved"
            item.generation_state = "generated"
    original_clearance = plan.safety.clearance_mm
    report = build_remediation_report(
        analysis,
        plan,
        cam_result(plan),
        verify_cam(plan, cam_result(plan)),
        {
            "status": "failed",
            "collisions": [],
            "low_rapids": [{
                "operation_id": operation.id,
                "setup_id": plan.setups[0].id,
                "minimum_z": 1.0,
                "required_z": 5.0,
            }],
        },
    )

    applied = apply_automatic_remediation(plan, report)

    assert [item["kind"] for item in applied] == ["adjust_safety"]
    assert plan.safety.clearance_mm > original_clearance
    assert all(
        item.status == "proposed" and item.generation_state == "dirty"
        for setup in plan.setups for item in setup.operations if item.enabled
    )


def test_automatic_remediation_refuses_blocked_report() -> None:
    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    result = cam_result(plan)
    verification = verify_cam(plan, result)
    verification["metrics"]["missing_target_volume_mm3"] = 5.0
    report = build_remediation_report(analysis, plan, result, verification, empty_collision())

    assert apply_automatic_remediation(plan, report) == []
