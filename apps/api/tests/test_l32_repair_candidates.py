from app.agent.l32_repair_candidates import propose_l32_operation_repairs
from app.catalogs import get_tool
from app.models import Operation, Vec3
from app.rotational_features import (
    RotationalAxisCandidate, RotationalFeatureAnalysis, RotationalProfile,
    RotationalProfilePoint, TurningProfileFeature,
)


def operation(tool_id: str = "TURN-OD-F", *, region_complete: bool = True) -> Operation:
    return Operation(
        id="OP30", sequence=30, type="turn_od_finishing", name="finish",
        feature_ids=["RP-1"], tool=get_tool(tool_id),
        parameters={
            "radial_allowance_mm": 0.0,
            "cut_direction": "negative_z",
            "profile_z_min_mm": -10.0,
            "profile_z_max_mm": 0.0,
            "profile_region_complete": region_complete,
            "maximum_finish_nose_radius_mm": 0.4,
        },
        rationale=["test"], confidence=1, definition_id="turn_od_finishing",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
    )


def profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-10, radius=4),
            RotationalProfilePoint(z=0, radius=6),
        ],
        confidence=1, review_state="accepted",
    )


def excess_trial() -> dict:
    return {
        "status": "warning",
        "verification": {
            "metrics": {"overcut_sample_count": 0, "excess_stock_sample_count": 2},
            "deviations": [
                {"z": -3.0, "kind": "excess_stock"},
                {"z": -2.0, "kind": "excess_stock"},
            ],
        },
    }


def test_proposes_smaller_nose_from_geometric_excess_stock() -> None:
    result = propose_l32_operation_repairs(operation(), excess_trial(), profile())

    candidate = next(item for item in result["candidates"] if item["id"] == "smaller-nose-tool")
    assert candidate["tool_id"] == "TURN-OD-MICRO-F"
    assert candidate["parameters"]["maximum_finish_nose_radius_mm"] == 0.2
    assert "Z [-3.000, -2.000]" in result["observations"][0]


def test_pocket_residual_proposes_smaller_catalog_tool_and_tighter_stepover() -> None:
    source = Operation(
        id="OP55", sequence=55, type="pocket_finishing", name="pocket finish",
        feature_ids=["MF-1"], tool=get_tool("EM-2"), parameters={},
        rationale=["test"], confidence=1, definition_id="pocket_finishing",
        source="recommendation", channel_id="sub", spindle_id="sub", workpiece_side="back",
    )
    result = propose_l32_operation_repairs(source, {
        "status": "blocked",
        "evidence": {"remaining_feature_material_mm3": 0.42},
    }, None)

    assert any(item["tool_id"] == "EM-1" for item in result["candidates"])
    assert any(item["id"] == "pocket-tighter-stepover" for item in result["candidates"])
    assert result["next_action"] == "evaluate_candidates"


def test_smallest_pocket_tool_reports_corner_cleanup_capability_gap() -> None:
    source = Operation(
        id="OP55", sequence=55, type="pocket_finishing", name="pocket finish",
        feature_ids=["MF-1"], tool=get_tool("EM-1"), parameters={},
        rationale=["test"], confidence=1, definition_id="pocket_finishing",
        source="recommendation", channel_id="sub", spindle_id="sub", workpiece_side="back",
    )
    result = propose_l32_operation_repairs(source, {
        "status": "blocked",
        "evidence": {"remaining_feature_material_mm3": 0.42},
    }, None)

    assert result["capability_requirements"][0]["required_strategy"] == (
        "sharp_corner_cleanup_or_accepted_internal_corner_radius"
    )


def test_exterior_residual_proposes_bounded_clearance_trials() -> None:
    source = Operation(
        id="OP58", sequence=58, type="live_tool_contour_finishing", name="exterior finish",
        feature_ids=["SF-1"], tool=get_tool("EM-2"),
        parameters={"silhouette_clearance_mm": 0.18},
        rationale=["test"], confidence=1, definition_id="live_tool_contour_finishing",
        source="recommendation", channel_id="sub", spindle_id="sub", workpiece_side="back",
    )
    result = propose_l32_operation_repairs(source, {
        "status": "blocked",
        "evidence": {"remaining_excess_region_mm3": 2.5, "coverage_tolerance_mm3": 0.01},
    }, None)

    clearances = {
        item["parameters"].get("silhouette_clearance_mm")
        for item in result["candidates"]
    }
    assert {0.09, 0.03, 0.0}.issubset(clearances)


def test_does_not_expand_incomplete_regional_profile() -> None:
    result = propose_l32_operation_repairs(
        operation("TURN-OD-MICRO-F", region_complete=False), excess_trial(), profile(),
    )

    assert result["candidates"] == []
    assert result["next_action"] == "observe_model_or_split_operation"
    assert result["fallback_next_action"] == "observe_model_or_split_operation"
    assert any("non-rotational" in item for item in result["observations"])


def test_wrong_hand_failure_proposes_direction_compatible_tool() -> None:
    source = operation("TURN-OD-L-MICRO-F")
    trial = {
        "status": "blocked",
        "detail": "turning tool hand does not match cutting direction",
    }

    result = propose_l32_operation_repairs(source, trial, profile())

    candidate = next(item for item in result["candidates"] if item["id"] == "correct-tool-hand")
    assert candidate["tool_id"] == "TURN-OD-MICRO-F"


def test_traceability_failure_proposes_separate_reference_profile() -> None:
    source = operation()
    source.type = "turn_grooving"
    source.definition_id = "turn_grooving"
    source.feature_ids = ["GF-1"]
    result = propose_l32_operation_repairs(
        source,
        {
            "status": "blocked",
            "detail": "operation traceability does not reference the supplied profile",
        },
        profile(),
    )

    candidate = next(item for item in result["candidates"] if item["id"] == "bind-reference-profile")
    assert candidate["reference_profile_id"] == "RP-1"
    assert candidate["feature_ids"] is None


def test_groove_traceability_failure_also_sandboxes_geometry_repairs() -> None:
    groove_profile = RotationalProfile(
        id="RP-G", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-2, radius=2), RotationalProfilePoint(z=-1, radius=2),
            RotationalProfilePoint(z=-0.45, radius=1), RotationalProfilePoint(z=0.45, radius=1),
            RotationalProfilePoint(z=1, radius=2), RotationalProfilePoint(z=2, radius=2),
        ], confidence=1, review_state="accepted",
    )
    feature = TurningProfileFeature(
        id="GF-1", profile_id="RP-G", kind="external_groove_candidate",
        z_start=-0.45, z_end=0.45, radius_start=1, radius_end=1,
        width_mm=0.9, depth_mm=1, source_point_indices=[2, 3],
        confidence=1, review_state="review",
    )
    rotational = RotationalFeatureAnalysis(
        schema_version="1.0.0", source_file="part.step", status="candidate",
        axes=[], profiles=[groove_profile], features=[feature], warnings=[],
    )
    source = Operation(
        id="OP40", sequence=40, type="turn_grooving", name="groove",
        feature_ids=["GF-1"], tool=get_tool("TURN-GROOVE-2"),
        parameters={"groove_width_mm": 0.9, "peck_depth_mm": 0.5},
        rationale=["test"], confidence=1, definition_id="turn_grooving",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
    )

    result = propose_l32_operation_repairs(
        source,
        {"status": "blocked", "detail": "operation traceability does not reference the supplied profile"},
        groove_profile, operations=[source], rotational=rotational,
    )

    assert {item["id"] for item in result["candidates"]} == {
        "bind-reference-profile", "groove-width-0.8", "full-radius-contour-grooving",
    }


def test_reports_verified_tool_requirement_when_downstream_catalog_has_no_fit() -> None:
    source = operation("TURN-OD-MICRO-F")
    groove_profile = RotationalProfile(
        id="RP-G", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-2, radius=2), RotationalProfilePoint(z=-1, radius=2),
            RotationalProfilePoint(z=-0.45, radius=1), RotationalProfilePoint(z=0.45, radius=1),
            RotationalProfilePoint(z=1, radius=2), RotationalProfilePoint(z=2, radius=2),
        ], confidence=1, review_state="accepted",
    )
    feature = TurningProfileFeature(
        id="GF-1", profile_id="RP-G", kind="external_groove_candidate",
        z_start=-0.45, z_end=0.45, radius_start=1, radius_end=1,
        width_mm=0.9, depth_mm=1, source_point_indices=[2, 3],
        confidence=1, review_state="review",
    )
    rotational = RotationalFeatureAnalysis(
        schema_version="1.0.0", source_file="part.step", status="candidate",
        axes=[RotationalAxisCandidate(
            id="RA-1", origin=Vec3(x=0, y=0, z=0), direction=Vec3(x=0, y=0, z=1),
            confidence=1, source_feature_ids=[], review_state="accepted",
        )], profiles=[groove_profile], features=[feature], warnings=[],
    )
    downstream = Operation(
        id="OP33-G1", sequence=33, type="turn_grooving", name="groove",
        feature_ids=["RP-G", "GF-1"], tool=get_tool("TURN-GROOVE-0.8"),
        parameters={"groove_width_mm": 0.9, "peck_depth_mm": 0.5},
        rationale=["test"], confidence=1, definition_id="turn_grooving",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
        enabled=True,
    )
    trial = {
        "status": "warning",
        "evidence": {"downstream_capability_gaps": [{
            "responsible_operation_id": "OP33-G1", "feature_id": "GF-1",
            "uncovered_z_range_mm": [-0.8, 0.8],
        }]},
        "verification": {
            "metrics": {"overcut_sample_count": 0, "excess_stock_sample_count": 2},
            "deviations": [
                {"z": -0.8, "kind": "excess_stock", "target_radius_mm": 1.64},
                {"z": 0.8, "kind": "excess_stock", "target_radius_mm": 1.64},
            ],
        },
    }

    result = propose_l32_operation_repairs(
        source, trial, groove_profile, operations=[source, downstream], rotational=rotational,
    )

    assert not any(item.get("target_operation_id") == "OP33-G1" for item in result["candidates"])
    requirement = result["capability_requirements"][0]
    assert requirement["target_operation_id"] == "OP33-G1"
    assert requirement["catalog_match"] is False
    assert 0 < requirement["maximum_cutting_width_mm"] < 0.8
    assert result["next_action"] == "catalog_and_bind_verified_tool_or_define_special_process"
    assert result["fallback_next_action"] == "catalog_and_bind_verified_tool_or_define_special_process"


def test_exact_groove_excess_proposes_narrower_catalogued_cutter() -> None:
    groove_profile = RotationalProfile(
        id="RP-G", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-2, radius=2), RotationalProfilePoint(z=-1, radius=2),
            RotationalProfilePoint(z=-0.45, radius=1), RotationalProfilePoint(z=0.45, radius=1),
            RotationalProfilePoint(z=1, radius=2), RotationalProfilePoint(z=2, radius=2),
        ], confidence=1, review_state="accepted",
    )
    feature = TurningProfileFeature(
        id="GF-1", profile_id="RP-G", kind="external_groove_candidate",
        z_start=-0.45, z_end=0.45, radius_start=1, radius_end=1,
        width_mm=0.9, depth_mm=1, source_point_indices=[2, 3],
        confidence=1, review_state="review",
    )
    rotational = RotationalFeatureAnalysis(
        schema_version="1.0.0", source_file="part.step", status="candidate",
        axes=[RotationalAxisCandidate(
            id="RA-1", origin=Vec3(x=0, y=0, z=0), direction=Vec3(x=0, y=0, z=1),
            confidence=1, source_feature_ids=[], review_state="accepted",
        )], profiles=[groove_profile], features=[feature], warnings=[],
    )
    source = Operation(
        id="OP40", sequence=40, type="turn_grooving", name="groove",
        feature_ids=["GF-1"], reference_profile_id="RP-G", tool=get_tool("TURN-GROOVE-2"),
        parameters={"groove_width_mm": 0.9, "peck_depth_mm": 0.5},
        rationale=["test"], confidence=1, definition_id="turn_grooving",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
    )

    result = propose_l32_operation_repairs(
        source, excess_trial(), groove_profile, operations=[source], rotational=rotational,
    )

    candidate = next(item for item in result["candidates"] if item["id"] == "groove-width-0.8")
    assert candidate["tool_id"] == "TURN-GROOVE-0.8"
    assert candidate["reference_profile_id"] == "RP-G"
    assert candidate["parameters"]["exact_groove_envelope"] is True
    contour = next(item for item in result["candidates"] if item["id"] == "full-radius-contour-grooving")
    assert contour["tool_id"] == "ENGINEERING-GROOVE-FULL-R-0.4"
    assert contour["parameters"]["groove_strategy"] == "full_radius_contour"
    requirement = next(
        item for item in result["capability_requirements"]
        if item["required_tool_kind"] == "full_radius_contour_grooving"
    )
    assert requirement["catalog_match"] is False
