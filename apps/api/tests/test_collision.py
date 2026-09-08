from app.collision import detect_collisions
from app.models import GeometryAnalysis
from app.planner import build_process_plan


def simple_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.4.0",
        "source_file": "block.step",
        "topology": {"solids": 1, "faces": 6, "edges": 12},
        "measurements": {
            "surface_area": 600, "volume": 1000,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 10, "y": 10, "z": 10},
                "size": {"x": 10, "y": 10, "z": 10},
            },
        },
        "planar_features": [{
            "id": "PF-1", "area": 100,
            "center": {"x": 5, "y": 5, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [], "prismatic_features": [],
    })


def test_safe_path_passes_collision_checks() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    result = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "rapid",
        "x1": 0, "y1": 5, "z1": 15,
        "x2": 10, "y2": 5, "z2": 15,
    }]})
    assert result["status"] == "passed"
    assert result["metrics"]["required_rapid_z"] == 15


def test_low_horizontal_rapid_fails_clearance_check() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    result = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "rapid",
        "x1": 0, "y1": 5, "z1": 14,
        "x2": 10, "y2": 5, "z2": 14,
    }]})
    assert result["status"] == "failed"
    assert result["metrics"]["low_rapid_count"] == 1


def test_short_tool_stickout_detects_holder_stock_collision() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    operation.tool.stickout_mm = 1
    result = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "cut",
        "x1": 2, "y1": 5, "z1": 9,
        "x2": 8, "y2": 5, "z2": 9,
    }]})
    assert result["status"] == "failed"
    assert any(item["kind"] == "holder_stock" for item in result["collisions"])


def test_side_setup_uses_its_local_safety_plane() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    result = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "rapid",
        "x1": 14, "y1": 0, "z1": 0,
        "x2": 14, "y2": 10, "z2": 0,
        "local_z1": 14, "local_z2": 14,
        "setup_id": "SETUP-X", "work_axis": {"x": 1, "y": 0, "z": 0},
    }]})

    assert result["status"] == "failed"
    assert result["metrics"]["low_rapid_count"] == 1


def test_thin_sheet_uses_sacrificial_support_and_protects_machine_bed() -> None:
    analysis = simple_analysis()
    bounds = analysis.measurements["bounding_box"]
    bounds.maximum.z = 3
    bounds.size.z = 3
    analysis.measurements["volume"] = 250
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")

    assert plan.safety.fixture_strategy == "sacrificial_plate"
    assert any(component.kind == "sacrificial" for component in plan.safety.fixture_components)
    assert any(component.kind == "machine" for component in plan.safety.fixture_components)

    operation = plan.setups[0].operations[-1]
    safe = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "cut",
        "x1": 5, "y1": 5, "z1": -2.5,
        "x2": 6, "y2": 5, "z2": -2.5,
        "setup_id": "SETUP-1", "work_axis": {"x": 0, "y": 0, "z": 1},
    }]})
    assert safe["status"] == "passed"
    assert safe["metrics"]["support_penetration_mm"] == 2.5

    too_deep = detect_collisions(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "cut",
        "x1": 5, "y1": 5, "z1": -3.1,
        "x2": 6, "y2": 5, "z2": -3.1,
        "setup_id": "SETUP-1", "work_axis": {"x": 0, "y": 0, "z": 1},
    }]})
    assert too_deep["status"] == "failed"
    assert any(item["target_id"].endswith("MACHINE-BED") for item in too_deep["collisions"])
