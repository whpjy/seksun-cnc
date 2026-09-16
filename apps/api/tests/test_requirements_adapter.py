from __future__ import annotations

from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.requirements_adapter import (
    import_measurement_specification, parse_thread_specification, reconcile_requirement_bindings,
)


def measurement_specification() -> dict:
    return {
        "schema_version": "1.1.0",
        "source": {"drawing_number": "PART-001", "revision": "B"},
        "comparison_rows": [
            {
                "drawing_entity": {
                    "id": "D-01", "semantic_type": "diameter", "nominal": 6.0,
                    "tolerance": {"upper": 0.01, "lower": -0.01}, "quantity": 1,
                    "source": {"raw_text": "Ø6 ±0.01"},
                },
                "mapping_status": "matched", "verification_status": "verified_geometry",
                "cad_feature_ids": ["HF-1"], "confidence": 0.96,
            },
            {
                "drawing_entity": {
                    "id": "D-02", "semantic_type": "surface_roughness",
                    "parameter": "Ra", "maximum": 0.8,
                    "source": {"raw_text": "Ra 0.8"},
                },
                "mapping_status": "matched", "verification_status": "verified_geometry",
                "cad_feature_ids": ["PF-1"], "confidence": 0.92,
            },
            {
                "drawing_entity": {
                    "id": "D-03", "semantic_type": "gdt_feature_control_frame",
                    "subtype": "position", "nominal": 0.1,
                },
                "mapping_status": "matched", "verification_status": "verified_geometry",
                "cad_feature_ids": ["HF-1"], "confidence": 0.9,
            },
            {
                "drawing_entity": {"id": "D-04", "semantic_type": "linear_dimension", "nominal": 12},
                "mapping_status": "ambiguous", "verification_status": "needs_disambiguation",
                "cad_feature_ids": ["EDGE-1", "EDGE-2"], "confidence": 0.6,
            },
        ],
    }


def test_measurement_specification_is_normalized_with_traceability() -> None:
    requirements = import_measurement_specification(measurement_specification())
    assert requirements.drawing_number == "PART-001"
    assert requirements.revision == "B"
    assert requirements.status == "review"
    assert requirements.summary == {
        "total": 4, "matched": 3, "ambiguous": 1, "unmapped": 0, "recognized_only": 0,
    }
    diameter = requirements.requirements[0]
    assert diameter.tolerance_upper == 0.01
    assert diameter.tolerance_lower == -0.01
    assert diameter.cad_feature_ids == ["HF-1"]
    assert diameter.raw_text is not None
    assert requirements.unresolved_requirement_ids == ["D-04"]


def test_verified_drawing_requirements_change_the_manufacturing_route() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "part.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "surface_area": 1200, "volume": 4000,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 40, "y": 30, "z": 10},
                "size": {"x": 40, "y": 30, "z": 10},
            },
        },
        "planar_features": [{
            "id": "PF-1", "area": 1200, "center": {"x": 20, "y": 15, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [{
            "id": "HF-1", "kind": "hole", "radius": 3, "diameter": 6, "length": 10,
            "center": {"x": 20, "y": 15, "z": 5}, "axis": {"x": 0, "y": 0, "z": 1},
            "access_direction": {"x": 0, "y": 0, "z": 1}, "end_type": "through",
            "confidence": 0.95, "review_state": "accepted",
        }],
    })
    requirements = import_measurement_specification(measurement_specification())

    plan = build_process_plan(analysis, "6061-T6", "VMC850", requirements=requirements)

    assert plan.manufacturing_requirements is requirements
    route = plan.manufacturing_route
    assert route is not None
    steps = {step.process_code: step for step in route.steps}
    assert steps["GX-C-13"].selection == "required"
    assert steps["GX-Q-02"].selection == "required"
    assert steps["GX-Q-03"].selection == "required"
    assert any("4 项" in item for item in route.planning_basis)
    assert any("1 项图纸要求" in item for item in route.missing_information)


def test_foreign_feature_ids_are_rebound_or_downgraded() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "part.step",
        "topology": {"solids": 1},
        "measurements": {
            "surface_area": 100, "volume": 100,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 10, "y": 10, "z": 10},
                "size": {"x": 10, "y": 10, "z": 10},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "HF-CNC", "kind": "hole", "radius": 3, "diameter": 6, "length": 10,
            "center": {"x": 5, "y": 5, "z": 5}, "axis": {"x": 0, "y": 0, "z": 1},
            "confidence": 0.9, "review_state": "accepted",
        }],
    })
    specification = measurement_specification()
    specification["comparison_rows"] = specification["comparison_rows"][:2]
    requirements = import_measurement_specification(specification)

    reconciled = reconcile_requirement_bindings(requirements, analysis)

    diameter, roughness = reconciled.requirements
    assert diameter.cad_feature_ids == ["HF-CNC"]
    assert diameter.source["binding_method"] == "cnc_geometry_diameter_quantity_rebind"
    assert diameter.mapping_status == "matched"
    assert roughness.mapping_status == "ambiguous"
    assert roughness.verification_status == "needs_cross_system_rebinding"
    assert roughness.id in reconciled.unresolved_requirement_ids


def test_foreign_linear_dimension_binding_is_not_treated_as_verified() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "part.step",
        "topology": {"solids": 1},
        "measurements": {
            "surface_area": 100, "volume": 100,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 10, "y": 10, "z": 10},
                "size": {"x": 10, "y": 10, "z": 10},
            },
        },
        "planar_features": [], "cylindrical_features": [],
    })
    requirements = import_measurement_specification({
        "comparison_rows": [{
            "drawing_entity": {
                "id": "D-LINEAR", "semantic_type": "linear_dimension",
                "nominal": 10, "raw_text": "10 +/-0.1",
            },
            "mapping_status": "matched", "verification_status": "verified_geometry",
            "cad_feature_ids": ["MEAS-EDGE-42"], "confidence": 0.9,
        }],
    })

    reconciled = reconcile_requirement_bindings(requirements, analysis)

    requirement = reconciled.requirements[0]
    assert requirement.raw_text == "10 +/-0.1"
    assert requirement.mapping_status == "ambiguous"
    assert requirement.verification_status == "needs_cross_system_rebinding"
    assert reconciled.status == "incomplete"


def test_parses_unf_ocr_and_g_pipe_thread_designations() -> None:
    unf = parse_thread_specification("外螺纹 3 4-16 UNF-2A")
    bspp = parse_thread_specification("内螺纹 G1/8")

    assert unf is not None
    assert unf.designation == "3/4-16 UNF-2A"
    assert unf.side == "external"
    assert unf.major_diameter_mm == 19.05
    assert unf.pitch_mm == 1.5875
    assert unf.form_angle_degrees == 60
    assert bspp is not None
    assert bspp.designation == "G1/8"
    assert bspp.standard == "BSPP"
    assert bspp.side == "internal"
    assert bspp.major_diameter_mm == 9.728
    assert bspp.pitch_mm == 0.907143
    assert bspp.form_angle_degrees == 55


def test_thread_requirement_rebinds_by_side_and_major_diameter() -> None:
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "threaded.step",
        "topology": {"solids": 1},
        "measurements": {
            "surface_area": 100, "volume": 100,
            "bounding_box": {
                "minimum": {"x": -10, "y": -10, "z": -20},
                "maximum": {"x": 10, "y": 10, "z": 20},
                "size": {"x": 20, "y": 20, "z": 40},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "HF-THREAD", "kind": "boss", "radius": 9.525,
            "diameter": 19.05, "length": 15,
            "center": {"x": 0, "y": 0, "z": 0},
            "axis": {"x": 0, "y": 0, "z": 1},
            "confidence": 0.9, "review_state": "accepted",
        }],
    })
    requirements = import_measurement_specification({
        "comparison_rows": [{
            "drawing_entity": {
                "id": "THREAD-1", "semantic_type": "thread",
                "source": {"raw_text": "外螺纹 3/4-16 UNF-2A"},
            },
            "mapping_status": "matched", "verification_status": "verified_geometry",
            "cad_feature_ids": ["FOREIGN-THREAD"], "confidence": 0.95,
        }],
    })

    reconciled = reconcile_requirement_bindings(requirements, analysis)
    requirement = reconciled.requirements[0]

    assert requirement.thread is not None
    assert requirement.cad_feature_ids == ["HF-THREAD"]
    assert requirement.mapping_status == "matched"
    assert requirement.verification_status == "verified_geometry"
    assert requirement.source["binding_method"] == "cnc_thread_major_diameter_rebind"
