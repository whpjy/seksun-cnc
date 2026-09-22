import pytest

from app.models import Bounds, GeometryAnalysis, InternalProfileFeature, PrismaticFeature, Vec3
from app.planner import _extent_along_axis, build_process_plan
from app.recognizer import normalize_manufacturing_features


def test_setup_thickness_uses_active_tool_axis() -> None:
    bounds = Bounds(
        minimum=Vec3(x=0, y=0, z=0), maximum=Vec3(x=159, y=7.8, z=118.5),
        size=Vec3(x=159, y=7.8, z=118.5),
    )
    assert _extent_along_axis(bounds, (0, 1, 0)) == 7.8
    assert _extent_along_axis(bounds, (0, 0, -1)) == 118.5


def sample_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate(
        {
            "schema_version": "0.1.0",
            "source_file": "fixture.step",
            "topology": {"solids": 1, "faces": 8, "edges": 18},
            "measurements": {
                "surface_area": 12000.0,
                "volume": 50000.0,
                "bounding_box": {
                    "minimum": {"x": 0, "y": 0, "z": 0},
                    "maximum": {"x": 100, "y": 60, "z": 20},
                    "size": {"x": 100, "y": 60, "z": 20},
                },
            },
            "planar_features": [
                {
                    "id": "PF-1",
                    "area": 6000,
                    "center": {"x": 50, "y": 30, "z": 20},
                    "normal": {"x": 0, "y": 0, "z": 1},
                }
            ],
            "cylindrical_features": [
                {
                    "id": "CF-1",
                    "kind": "hole",
                    "radius": 5,
                    "diameter": 10,
                    "length": 20,
                    "center": {"x": 20, "y": 20, "z": 10},
                    "axis": {"x": 0, "y": 0, "z": 1},
                },
                {
                    "id": "CF-2",
                    "kind": "hole",
                    "radius": 5,
                    "diameter": 10,
                    "length": 20,
                    "center": {"x": 80, "y": 40, "z": 10},
                    "axis": {"x": 0, "y": 0, "z": 1},
                },
            ],
        }
    )


def test_groups_equal_holes_into_one_operation() -> None:
    plan = build_process_plan(sample_analysis(), "6061-T6", "VMC-850")
    assert plan.stock["size_mm"] == [106, 66, 24]
    assert len(plan.setups) == 1
    assert len(plan.setups[0].operations) == 2
    drilling = plan.setups[0].operations[1]
    assert drilling.type == "drilling"
    assert drilling.feature_ids == ["CF-1", "CF-2"]
    assert "2×Ø10.00" in drilling.name
    assert drilling.parameters["spindle_rpm"] == 3183
    assert drilling.parameters["feed_rate_mm_min"] == 477
    assert drilling.tool.catalog_match is True
    assert plan.machine_profile.id == "vmc-850"
    assert plan.machine_profile.postprocessor == "fanuc"
    assert plan.material_profile.id == "al-6061-t6"


def test_ai_process_kind_hint_can_route_plan_to_sheet_forming() -> None:
    plan = build_process_plan(
        sample_analysis(), "6061-T6", "VMC-850", process_kind_hint="sheet_forming",
    )

    assert plan.process_kind == "sheet_forming"
    assert [operation.type for operation in plan.setups[0].operations] == [
        "sheet_flat_pattern", "sheet_blanking", "sheet_preforming",
        "sheet_final_forming", "sheet_deburring", "sheet_inspection",
    ]


def test_through_drill_depth_includes_point_and_breakthrough() -> None:
    analysis = sample_analysis()
    for feature in analysis.cylindrical_features:
        feature.end_type = "through"
        feature.access_direction = Vec3(x=0, y=0, z=1)
        feature.review_state = "accepted"

    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    drilling = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "drilling"
    )

    assert drilling.parameters["feature_depth_mm"] == 20
    assert drilling.parameters["drill_tip_length_mm"] > 3
    assert drilling.parameters["breakthrough_mm"] == 0.5
    assert drilling.parameters["depth_mm"] > 23.5
    assert any("钻尖长度" in reason for reason in drilling.rationale)


def test_plan_always_contains_review_warnings() -> None:
    plan = build_process_plan(sample_analysis(), "S45C", "VMC")
    assert plan.warnings
    assert any("碰撞" in warning for warning in plan.warnings)


def test_cutting_data_changes_with_material_and_respects_machine_limit() -> None:
    aluminium = build_process_plan(sample_analysis(), "6061-T6 铝合金", "三轴立式加工中心")
    stainless = build_process_plan(sample_analysis(), "SUS304", "三轴立式加工中心")
    aluminium_drill = aluminium.setups[0].operations[1]
    stainless_drill = stainless.setups[0].operations[1]

    assert aluminium_drill.parameters["spindle_rpm"] > stainless_drill.parameters["spindle_rpm"]
    assert aluminium_drill.parameters["feed_rate_mm_min"] > stainless_drill.parameters["feed_rate_mm_min"]
    assert aluminium.setups[0].operations[0].parameters["spindle_rpm"] <= aluminium.machine_profile.max_spindle_rpm


def test_merges_connected_coaxial_cylinder_faces() -> None:
    analysis = sample_analysis()
    analysis.cylindrical_features[1].center.x = 20
    analysis.cylindrical_features[1].center.y = 20
    analysis.cylindrical_features[0].center.z = 5
    analysis.cylindrical_features[0].length = 10
    analysis.cylindrical_features[1].center.z = 15
    analysis.cylindrical_features[1].length = 10

    normalized = normalize_manufacturing_features(analysis)

    assert normalized.schema_version == "0.5.0"
    assert len(normalized.cylindrical_features) == 1
    feature = normalized.cylindrical_features[0]
    assert feature.segment_count == 2
    assert feature.length == 20
    assert feature.source_face_ids == ["CF-1", "CF-2"]


def test_excludes_partial_cylinders_from_hole_planning() -> None:
    analysis = sample_analysis()
    analysis.measurements["bounding_box"].size.z = 3
    analysis.measurements["bounding_box"].maximum.z = 3
    analysis.measurements["volume"] = 12000
    for feature in analysis.cylindrical_features:
        feature.length = 3
        feature.center.z = 1.5
        feature.angular_span_degrees = 180

    normalized = normalize_manufacturing_features(analysis)
    plan = build_process_plan(normalized, "6061-T6", "VMC")

    assert all(feature.review_state == "excluded" for feature in normalized.cylindrical_features)
    assert all("圆周仅覆盖" in feature.review_reasons[-1] for feature in normalized.cylindrical_features)
    assert plan.automation_status == "review"
    assert plan.knowledge_assessment is not None
    assert plan.knowledge_assessment.catalog_process_count == 98
    assert "GX-C-09" in plan.knowledge_assessment.route_process_codes
    assert all(
        operation.manufacturing_code
        for setup in plan.setups
        for operation in setup.operations
    )
    assert plan.manufacturing_route is not None
    assert plan.manufacturing_route.part_family == "freeform"
    assert plan.manufacturing_route.steps[0].process_code == "GX-Q-11"
    assert any(step.process_code == "GX-C-31" for step in plan.manufacturing_route.steps)
    assert any(
        step.process_code == "GX-C-09" and step.execution_mode == "cam"
        for step in plan.manufacturing_route.steps
    )
    assert plan.manufacturing_route.steps[-1].process_code == "GX-Q-14"
    assert any(
        operation.type == "profile_roughing"
        for setup in plan.setups
        for operation in setup.operations
    )
    operation_types = [operation.type for setup in plan.setups for operation in setup.operations]
    assert operation_types == [
        "surface_roughing", "surface_3d", "waterline",
        "profile_roughing", "profile_finishing", "tab_removal", "edge_chamfer",
        "surface_roughing", "surface_3d", "waterline", "edge_chamfer",
    ]
    assert len(plan.setups) == 2
    assert plan.setups[1].work_axis.z == -1
    profile_roughing = next(operation for operation in plan.setups[0].operations if operation.type == "profile_roughing")
    tab_removal = next(operation for operation in plan.setups[0].operations if operation.type == "tab_removal")
    assert profile_roughing.parameters["radial_allowance_mm"] == 0.2
    assert tab_removal.parameters["requires_secondary_retention"] is True
    assert all(
        operation.type != "drilling"
        for setup in plan.setups
        for operation in setup.operations
    )


def test_rotational_part_is_routed_to_turning_without_claiming_cam_support() -> None:
    analysis = sample_analysis()
    bounds = analysis.measurements["bounding_box"]
    bounds.maximum.x = 120
    bounds.maximum.y = 20
    bounds.maximum.z = 20
    bounds.size.x = 120
    bounds.size.y = 20
    bounds.size.z = 20
    analysis.measurements["volume"] = 30000
    cylinder = analysis.cylindrical_features[0]
    cylinder.kind = "cylinder"
    cylinder.length = 120
    analysis.cylindrical_features = [cylinder]
    analysis.prismatic_features = []
    analysis.internal_profile_features = []

    plan = build_process_plan(analysis, "45 steel", "VMC850")

    assert plan.manufacturing_route is not None
    assert plan.manufacturing_route.part_family == "rotational"
    route_codes = [step.process_code for step in plan.manufacturing_route.steps]
    assert "GX-C-01" in route_codes
    assert "GX-C-02" in route_codes
    assert "GX-C-03" in route_codes
    assert any("车削" in item for item in plan.manufacturing_route.capability_gaps)
    assert plan.manufacturing_route.status == "incomplete"


def test_formed_sheet_part_is_routed_away_from_billet_milling() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.4.0",
        "source_file": "0437S001ZU_20231121.stp",
        "topology": {"solids": 1, "faces": 4, "edges": 16},
        "measurements": {
            "surface_area": 1712.56,
            "volume": 243.74,
            "bounding_box": {
                "minimum": {"x": -21.2, "y": 0, "z": 25.6},
                "maximum": {"x": 21.2, "y": 4.85, "z": 50.9},
                "size": {"x": 42.4, "y": 4.85, "z": 25.3},
            },
        },
        "planar_features": [{
            "id": "PF-FRONT", "area": 173.38,
            "center": {"x": 0, "y": 4.85, "z": 40},
            "normal": {"x": 0, "y": 1, "z": 0},
            "wire_count": 1,
        }],
        "cylindrical_features": [
            {
                "id": "HF-RADIUS", "kind": "boss", "radius": 20,
                "diameter": 40, "length": 20.8,
                "center": {"x": 0, "y": 2.4, "z": 40},
                "axis": {"x": 1, "y": 0, "z": 0},
                "angular_span_degrees": 80,
                "source_face_ids": ["CF-RADIUS"],
                "review_state": "accepted", "confidence": 0.9,
            },
            {
                "id": "HF-HOLE", "kind": "hole", "radius": 0.75,
                "diameter": 1.5, "length": 4.85,
                "center": {"x": 19.6, "y": 2.425, "z": 43.1},
                "axis": {"x": 0, "y": 1, "z": 0},
                "access_direction": {"x": 0, "y": 1, "z": 0},
                "angular_span_degrees": 360,
                "source_face_ids": ["CF-HOLE"],
                "end_type": "through", "review_state": "accepted", "confidence": 0.9,
            },
        ],
        "prismatic_features": [],
    })

    plan = build_process_plan(analysis, "6061-T6", "VMC850")
    assert plan.process_kind == "sheet_forming"
    assert plan.automation_status == "review"
    assert plan.stock["type"] == "sheet_blank_candidate"
    assert plan.stock["size_mm"] == [42.4, 0.3, 25.3]
    assert len(plan.setups) == 1
    assert [operation.type for operation in plan.setups[0].operations] == [
        "sheet_flat_pattern", "sheet_blanking", "sheet_preforming",
        "sheet_final_forming", "sheet_deburring", "sheet_inspection",
    ]
    assert plan.safety is None
    assert plan.manufacturing_route is not None
    assert plan.manufacturing_route.part_family == "sheet_forming"
    assert plan.manufacturing_route.status == "incomplete"
    assert any("冲压" in item for item in plan.manufacturing_route.capability_gaps)
    assert plan.blocking_reasons == []
    assert any("成形求解器" in warning for warning in plan.warnings)


def test_tooling_intent_is_not_misclassified_as_sheet_forming() -> None:
    analysis = sample_analysis()
    analysis.source_file = "3285506818_soft tooling_V2.stp"
    analysis.measurements["surface_area"] = 27807.49
    analysis.measurements["volume"] = 10921.18
    bounds = analysis.measurements["bounding_box"]
    bounds.minimum.x, bounds.minimum.y, bounds.minimum.z = -74.0, -9.4, -55.45
    bounds.maximum.x, bounds.maximum.y, bounds.maximum.z = 77.0, -1.6, 63.05
    bounds.size.x, bounds.size.y, bounds.size.z = 151.0, 7.8, 118.5

    plan = build_process_plan(analysis, "6061-T6", "VMC850")

    assert plan.process_kind == "subtractive"


def test_splits_setups_by_hole_access_direction() -> None:
    analysis = sample_analysis()
    analysis.cylindrical_features[0].access_direction = Vec3(x=0, y=0, z=1)
    analysis.cylindrical_features[0].review_state = "accepted"
    analysis.cylindrical_features[1].axis = Vec3(x=1, y=0, z=0)
    analysis.cylindrical_features[1].access_direction = Vec3(x=1, y=0, z=0)
    analysis.cylindrical_features[1].review_state = "accepted"

    plan = build_process_plan(analysis, "6061-T6", "VMC")

    assert len(plan.setups) == 2
    assert any("+X" in setup.name for setup in plan.setups)
    assert any("+Z" in setup.name for setup in plan.setups)


def test_recognizes_rectangular_pocket_and_generates_rough_finish_operations() -> None:
    analysis = sample_analysis()
    analysis.planar_features[0].bounds = analysis.measurements["bounding_box"]
    analysis.planar_features.append(
        type(analysis.planar_features[0]).model_validate(
            {
                "id": "PF-2",
                "area": 800.0,
                "center": Vec3(x=50, y=30, z=14),
                "bounds": {
                    "minimum": {"x": 30, "y": 20, "z": 14},
                    "maximum": {"x": 70, "y": 40, "z": 14},
                    "size": {"x": 40, "y": 20, "z": 0},
                },
                "normal": {"x": 0, "y": 0, "z": 1},
                "adjacent_edge_count": 4,
                "rising_edge_count": 4,
            }
        )
    )

    normalized = normalize_manufacturing_features(analysis)
    assert normalized.schema_version == "0.5.0"
    assert len(normalized.prismatic_features) == 1
    pocket = normalized.prismatic_features[0]
    assert pocket.kind == "pocket"
    assert pocket.depth == 6
    assert pocket.review_state == "accepted"

    plan = build_process_plan(normalized, "6061-T6", "VMC")
    pocket_operations = [
        operation for setup in plan.setups for operation in setup.operations
        if pocket.id in operation.feature_ids
    ]
    assert [operation.type for operation in pocket_operations] == [
        "pocket_roughing", "pocket_finishing",
    ]


def test_pairs_non_circular_face_wires_and_plans_internal_profile() -> None:
    analysis = sample_analysis()
    analysis.cylindrical_features = []
    bounds = analysis.measurements["bounding_box"]
    bounds.maximum.z = 3
    bounds.size.z = 3
    analysis.measurements["volume"] = 15000
    analysis.planar_features[0].center.z = 3
    analysis.planar_features[0].wire_count = 2
    analysis.planar_features.append(type(analysis.planar_features[0]).model_validate({
        "id": "PF-BOTTOM", "area": 5800,
        "center": {"x": 50, "y": 30, "z": 0},
        "normal": {"x": 0, "y": 0, "z": -1}, "wire_count": 2,
    }))
    shared = {
        "wire_index": 2, "edge_count": 8, "perimeter": 72,
        "bounds": {
            "minimum": {"x": 35, "y": 20, "z": 3},
            "maximum": {"x": 65, "y": 40, "z": 3},
            "size": {"x": 30, "y": 20, "z": 0},
        },
        "circular": False,
    }
    analysis.internal_profile_features = [InternalProfileFeature.model_validate(item) for item in [
        {
            "id": "RAW-TOP", "source_face_id": "PF-1", "source_face_index": 1,
            "center": {"x": 50, "y": 30, "z": 3},
            "access_direction": {"x": 0, "y": 0, "z": 1}, **shared,
        },
        {
            "id": "RAW-BOTTOM", "source_face_id": "PF-BOTTOM", "source_face_index": 2,
            "center": {"x": 50, "y": 30, "z": 0},
            "access_direction": {"x": 0, "y": 0, "z": -1},
            **{**shared, "bounds": {
                "minimum": {"x": 35, "y": 20, "z": 0},
                "maximum": {"x": 65, "y": 40, "z": 0},
                "size": {"x": 30, "y": 20, "z": 0},
            }},
        },
    ]]

    normalized = normalize_manufacturing_features(analysis)
    assert len(normalized.internal_profile_features) == 1
    profile = normalized.internal_profile_features[0]
    assert profile.end_type == "through"
    assert profile.paired_profile_id != profile.id
    assert profile.review_state == "accepted"
    assert profile.depth == 3

    plan = build_process_plan(normalized, "6061-T6", "VMC")
    profile_operations = [
        operation for setup in plan.setups for operation in setup.operations
        if profile.id in operation.feature_ids
    ]
    assert [operation.type for operation in profile_operations] == [
        "internal_profile_roughing", "internal_profile_finishing",
    ]
    operation_types = [operation.type for setup in plan.setups for operation in setup.operations]
    assert operation_types.index("internal_profile_finishing") < operation_types.index("profile_roughing")
    assert plan.automation_status == "review"
    assert plan.coverage is not None
    target = next(item for item in plan.coverage.targets if item.source_feature_ids == [profile.id])
    assert target.state == "covered"


def test_shallow_internal_profile_with_matching_floor_becomes_engraving() -> None:
    analysis = sample_analysis()
    analysis.cylindrical_features = []
    analysis.planar_features.append(type(analysis.planar_features[0]).model_validate({
        "id": "PF-MARK-FLOOR", "area": 1.6,
        "center": {"x": 20, "y": 15, "z": 19.95},
        "normal": {"x": 0, "y": 0, "z": 1},
        "bounds": {
            "minimum": {"x": 16, "y": 14.9, "z": 19.95},
            "maximum": {"x": 24, "y": 15.1, "z": 19.95},
            "size": {"x": 8, "y": 0.2, "z": 0},
        },
    }))
    analysis.internal_profile_features = [InternalProfileFeature.model_validate({
        "id": "RAW-MARK", "source_face_id": "PF-1", "source_face_index": 1,
        "wire_index": 2, "center": {"x": 20, "y": 15, "z": 20},
        "bounds": {
            "minimum": {"x": 16, "y": 14.9, "z": 20},
            "maximum": {"x": 24, "y": 15.1, "z": 20},
            "size": {"x": 8, "y": 0.2, "z": 0},
        },
        "access_direction": {"x": 0, "y": 0, "z": 1},
        "edge_count": 4, "perimeter": 16.4,
    })]

    normalized = normalize_manufacturing_features(analysis)
    feature = normalized.internal_profile_features[0]
    assert feature.end_type == "blind"
    assert feature.machining_kind == "engraving"
    assert feature.bottom_face_id == "PF-MARK-FLOOR"
    assert feature.depth == pytest.approx(0.05)
    assert feature.review_state == "accepted"

    plan = build_process_plan(normalized, "6061-T6", "VMC")
    operations = [operation for setup in plan.setups for operation in setup.operations]
    engraving = next(operation for operation in operations if feature.id in operation.feature_ids)
    assert engraving.type == "engraving"
    assert engraving.parameters["depth_mm"] == 0.05
    assert plan.coverage is not None
    target = next(item for item in plan.coverage.targets if item.source_feature_ids == [feature.id])
    assert target.state == "covered"


def test_review_pocket_is_not_planned_until_operator_accepts_it() -> None:
    analysis = sample_analysis()
    analysis.prismatic_features = [
        PrismaticFeature.model_validate({
            "id": "MF-REVIEW", "kind": "pocket", "source_face_id": "PF-1",
            "center": {"x": 50, "y": 30, "z": 10},
            "bounds": {
                "minimum": {"x": 20, "y": 20, "z": 10},
                "maximum": {"x": 80, "y": 40, "z": 10},
                "size": {"x": 60, "y": 20, "z": 0},
            },
            "access_direction": {"x": 0, "y": 0, "z": 1},
            "length": 60, "width": 20, "depth": 10,
            "confidence": 0.72, "review_state": "review",
        })
    ]

    review_plan = build_process_plan(analysis, "6061-T6", "VMC")
    assert all(
        "MF-REVIEW" not in operation.feature_ids
        for setup in review_plan.setups for operation in setup.operations
    )

    analysis.prismatic_features[0].review_state = "accepted"
    accepted_plan = build_process_plan(analysis, "6061-T6", "VMC")
    assert [
        operation.type for setup in accepted_plan.setups for operation in setup.operations
        if "MF-REVIEW" in operation.feature_ids
    ] == ["pocket_roughing", "pocket_finishing"]


def test_recognizes_slot_only_when_floor_crosses_opposite_part_edges() -> None:
    analysis = sample_analysis()
    analysis.planar_features[0].bounds = analysis.measurements["bounding_box"]
    analysis.planar_features.append(
        type(analysis.planar_features[0]).model_validate(
            {
                "id": "PF-2",
                "area": 1000.0,
                "center": Vec3(x=50, y=30, z=16),
                "bounds": {
                    "minimum": {"x": 0, "y": 25, "z": 16},
                    "maximum": {"x": 100, "y": 35, "z": 16},
                    "size": {"x": 100, "y": 10, "z": 0},
                },
                "normal": {"x": 0, "y": 0, "z": 1},
                "adjacent_edge_count": 4,
                "rising_edge_count": 2,
            }
        )
    )

    normalized = normalize_manufacturing_features(analysis)

    assert len(normalized.prismatic_features) == 1
    assert normalized.prismatic_features[0].kind == "slot"
    assert normalized.prismatic_features[0].open_sides == 2


def test_rejects_lower_boss_top_when_adjacent_walls_fall_away() -> None:
    analysis = sample_analysis()
    analysis.planar_features[0].bounds = analysis.measurements["bounding_box"]
    analysis.planar_features.append(
        type(analysis.planar_features[0]).model_validate(
            {
                "id": "PF-BOSS",
                "area": 600.0,
                "center": {"x": 50, "y": 30, "z": 14},
                "normal": {"x": 0, "y": 0, "z": 1},
                "bounds": {
                    "minimum": {"x": 35, "y": 20, "z": 14},
                    "maximum": {"x": 65, "y": 40, "z": 14},
                    "size": {"x": 30, "y": 20, "z": 0},
                },
                "adjacent_edge_count": 4,
                "rising_edge_count": 0,
                "falling_edge_count": 4,
            }
        )
    )

    normalized = normalize_manufacturing_features(analysis)

    assert normalized.prismatic_features == []


def test_exposes_complex_outer_planes_as_reviewable_milling_regions() -> None:
    analysis = sample_analysis()
    analysis.planar_features[0].bounds = Bounds(
        minimum=Vec3(x=0, y=0, z=20),
        maximum=Vec3(x=100, y=60, z=20),
        size=Vec3(x=100, y=60, z=0),
    )
    analysis.planar_features[0].falling_edge_count = 6
    analysis.planar_features[0].adjacent_edge_count = 8

    normalized = normalize_manufacturing_features(analysis)

    assert len(normalized.planar_machining_features) == 1
    feature = normalized.planar_machining_features[0]
    assert feature.kind == "planar_surface"
    assert feature.source_face_ids == ["PF-1"]
    assert feature.length == 100
    assert feature.width == 60
    assert feature.review_state == "review"
