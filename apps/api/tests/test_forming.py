from app.forming import build_forming_preview
from app.planner import build_process_plan

from test_planner import sample_analysis


def test_forming_preview_covers_every_planned_stage_without_nc() -> None:
    analysis = sample_analysis()
    analysis.source_file = "0437S001ZU_20231121.stp"
    analysis.measurements["surface_area"] = 1712.56
    analysis.measurements["volume"] = 243.74
    bounds = analysis.measurements["bounding_box"]
    bounds.minimum.x, bounds.minimum.y, bounds.minimum.z = -21.2, 0, 25.6
    bounds.maximum.x, bounds.maximum.y, bounds.maximum.z = 21.2, 4.85, 50.9
    bounds.size.x, bounds.size.y, bounds.size.z = 42.4, 4.85, 25.3

    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    result, verification, simulation, collision = build_forming_preview(analysis, plan)

    assert result["process_kind"] == "sheet_forming"
    assert result["postprocessor"] == "none"
    assert result["path_command_count"] == 0
    assert len(result["forming_preview"]["stages"]) == 6
    assert result["forming_preview"]["production_output_available"] is False
    assert verification["status"] == "warning"
    assert simulation["method"] == "target_mesh_depth_morph"
    assert collision["status"] == "passed"
