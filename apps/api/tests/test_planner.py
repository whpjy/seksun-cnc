from app.models import Bounds, GeometryAnalysis, PrismaticFeature, Vec3
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

    assert normalized.schema_version == "0.4.0"
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
    assert normalized.schema_version == "0.4.0"
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
