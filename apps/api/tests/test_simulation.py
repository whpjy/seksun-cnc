from copy import deepcopy

from app.models import GeometryAnalysis, Setup, Vec3
from app.planner import build_process_plan
from app.simulation import _adaptive_grid_size, simulate_material_removal


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
        "cylindrical_features": [],
        "prismatic_features": [],
    })


def test_dense_toolpaths_use_bounded_adaptive_grid() -> None:
    assert _adaptive_grid_size(49_999, 640) == 640
    assert _adaptive_grid_size(50_000, 640) == 420
    assert _adaptive_grid_size(100_000, 640) == 318
    assert _adaptive_grid_size(100_000, 200) == 200


def test_height_field_removes_stock_along_flat_tool_sweep() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    operation.tool.diameter_mm = 2
    result = simulate_material_removal(analysis, plan, {
        "preview_segments": [{
            "operation_id": operation.id, "motion": "cut",
            "x1": 0, "y1": 5, "z1": 9,
            "x2": 10, "y2": 5, "z2": 9,
        }],
    }, maximum_grid_size=40)

    assert result["status"] == "completed"
    assert result["metrics"]["removed_volume_mm3"] > 0
    assert result["metrics"]["removed_percent"] < 100
    surface = result["surface"]
    assert len(surface["heights"]) == surface["columns"] * surface["rows"]
    assert min(surface["heights"]) == 9


def test_height_field_reports_warning_without_cutting_segments() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "S45C", "三轴立式加工中心")
    result = simulate_material_removal(analysis, plan, {"preview_segments": []})

    assert result["status"] == "warning"
    assert result["metrics"]["removed_volume_mm3"] == 0


def test_cumulative_height_field_ignores_feed_motion_above_stock() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    operation = plan.setups[0].operations[0]
    result = simulate_material_removal(analysis, plan, {
        "setups": [{
            "setup_id": plan.setups[0].id,
            "local_bounds": {"minimum": {"z": -2}, "maximum": {"z": 12}},
        }],
        "preview_segments": [{
            "operation_id": operation.id, "setup_id": plan.setups[0].id, "motion": "cut",
            "x1": 0, "y1": 5, "z1": 20, "x2": 10, "y2": 5, "z2": 20,
            "local_z1": 20, "local_z2": 20,
            "work_axis": {"x": 0, "y": 0, "z": 1},
        }],
    }, maximum_grid_size=40)

    assert result["metrics"]["removed_volume_mm3"] == 0


def test_cumulative_height_field_inherits_stock_across_flipped_setup() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    front_operation = plan.setups[0].operations[0]
    front_operation.tool.diameter_mm = 2
    back_operation = deepcopy(front_operation)
    back_operation.id = "OP20"
    back_operation.sequence = 20
    plan.setups.append(Setup(
        id="SETUP-2", name="back", work_axis=Vec3(x=0, y=0, z=-1),
        datum_feature_id=None, fixture="vise", operations=[back_operation],
    ))
    result = simulate_material_removal(analysis, plan, {"preview_segments": [
        {
            "operation_id": front_operation.id, "setup_id": "SETUP-1", "motion": "cut",
            "x1": 0, "y1": 5, "z1": 9, "x2": 10, "y2": 5, "z2": 9,
            "work_axis": {"x": 0, "y": 0, "z": 1},
        },
        {
            "operation_id": back_operation.id, "setup_id": "SETUP-2", "motion": "cut",
            "x1": 0, "y1": 5, "z1": 1, "x2": 10, "y2": 5, "z2": 1,
            "work_axis": {"x": 0, "y": 0, "z": -1},
        },
    ]}, maximum_grid_size=40)

    surface = result["surface"]
    assert surface["is_cumulative"] is True
    assert surface["included_setup_ids"] == ["SETUP-1", "SETUP-2"]
    assert min(surface["heights"]) == 9
    assert max(surface["lower_heights"]) == 1
    assert result["metrics"]["removed_volume_mm3"] > result["setup_surfaces"][0]["removed_volume_mm3"]


def test_height_field_simulates_side_setup_in_its_local_frame() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "S45C", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    result = simulate_material_removal(analysis, plan, {"preview_segments": [{
        "operation_id": operation.id, "motion": "cut",
        "x1": 9, "y1": 0, "z1": 0,
        "x2": 9, "y2": 10, "z2": 0,
        "work_axis": {"x": 1, "y": 0, "z": 0},
    }]})

    assert result["status"] == "completed"
    assert result["metrics"]["cut_segment_count"] == 1
    assert result["metrics"]["removed_volume_mm3"] > 0
    assert result["surface"]["work_axis"] == {"x": 1.0, "y": 0.0, "z": 0.0}


def test_closed_profile_removes_detached_outside_scrap() -> None:
    analysis = simple_analysis()
    plan = build_process_plan(analysis, "6061-T6 铝合金", "三轴立式加工中心")
    operation = plan.setups[0].operations[0]
    result = simulate_material_removal(analysis, plan, {
        "preview_segments": [{
            "operation_id": operation.id, "motion": "cut",
            "x1": 2, "y1": 2, "z1": 0,
            "x2": 8, "y2": 2, "z2": 0,
        }],
        "profile_boundaries": [{
            "operation_id": operation.id,
            "work_axis": {"x": 0, "y": 0, "z": 1},
            "points": [
                {"x": 2, "y": 2}, {"x": 8, "y": 2},
                {"x": 8, "y": 8}, {"x": 2, "y": 8}, {"x": 2, "y": 2},
            ],
        }],
    }, maximum_grid_size=40)

    assert result["metrics"]["removed_percent"] > 50
    assert any("外侧废料" in warning for warning in result["warnings"])
