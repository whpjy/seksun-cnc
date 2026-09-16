from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.models import Bounds, GeometryAnalysis, JobResponse, Vec3
from app.rotational_features import (
    RotationalFeatureAnalysis, RotationalProfile, RotationalProfilePoint,
    _extract_profile_features, bind_thread_requirements, infer_rotational_features,
)
from app.requirements_adapter import import_measurement_specification


client = TestClient(app)


def analysis_with_cylinder(axis=None) -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "shaft.step",
        "topology": {"solids": 1, "faces": 3, "edges": 6},
        "measurements": {
            "volume": 1000,
            "surface_area": 800,
            "bounding_box": {
                "minimum": {"x": -10, "y": -10, "z": -50},
                "maximum": {"x": 10, "y": 10, "z": 50},
                "size": {"x": 20, "y": 20, "z": 100},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "CF-1", "kind": "cylinder", "radius": 10, "diameter": 20,
            "length": 100, "center": {"x": 0, "y": 0, "z": 0},
            "axis": axis or {"x": 0, "y": 0, "z": -1}, "confidence": 0.9,
        }],
    })


def test_infers_reviewable_axis_and_conservative_profile() -> None:
    result = infer_rotational_features(analysis_with_cylinder())

    assert result.status == "candidate"
    assert result.axes[0].direction.z == 1
    assert result.axes[0].review_state == "review"
    assert result.profiles[0].extraction_method == "bounding_cylinder"
    assert [item.z for item in result.profiles[0].points] == [-50, 50]
    assert all(item.radius == 10 for item in result.profiles[0].points)


def test_reports_not_detected_without_cylindrical_reference() -> None:
    source = analysis_with_cylinder()
    source.cylindrical_features = []

    result = infer_rotational_features(source)

    assert result.status == "not_detected"
    assert result.profiles == []


def test_requires_solid_selection_before_rotational_inference() -> None:
    source = analysis_with_cylinder()
    source.topology["source_solids"] = 3

    result = infer_rotational_features(source)

    assert result.status == "solid_selection_required"
    assert result.profiles == []
    assert result.evidence["source_solid_count"] == 3


def test_rejects_plate_like_transverse_envelope() -> None:
    source = analysis_with_cylinder(axis={"x": 1, "y": 0, "z": 0})
    source.measurements["bounding_box"] = Bounds.model_validate({
        "minimum": {"x": -50, "y": -20, "z": -2},
        "maximum": {"x": 50, "y": 20, "z": 2},
        "size": {"x": 100, "y": 40, "z": 4},
    })

    result = infer_rotational_features(source)

    assert result.status == "not_rotational"
    assert result.profiles == []
    assert result.evidence["transverse_aspect_ratio"] == 0.1


def test_keeps_moderately_flattened_turning_candidate_for_review() -> None:
    source = analysis_with_cylinder(axis={"x": 1, "y": 0, "z": 0})
    source.cylindrical_features[0].radius = 5
    source.cylindrical_features[0].diameter = 10
    source.measurements["bounding_box"] = Bounds.model_validate({
        "minimum": {"x": -10, "y": -3.25, "z": -5},
        "maximum": {"x": 10, "y": 3.25, "z": 5},
        "size": {"x": 20, "y": 6.5, "z": 10},
    })

    result = infer_rotational_features(source)

    assert result.status == "candidate"
    assert result.evidence["transverse_aspect_ratio"] == 0.65
    assert result.profiles


def test_projects_visual_edges_into_step_and_taper_profile() -> None:
    source = analysis_with_cylinder()
    source.visual_edges = [
        [Vec3(x=5, y=0, z=-30), Vec3(x=5, y=0, z=-10)],
        [Vec3(x=5, y=0, z=-10), Vec3(x=10, y=0, z=-10)],
        [Vec3(x=10, y=0, z=-10), Vec3(x=10, y=0, z=0)],
        [Vec3(x=10, y=0, z=0), Vec3(x=8, y=0, z=10)],
    ]

    result = infer_rotational_features(source)

    profile = result.profiles[0]
    assert profile.extraction_method == "edge_projection_envelope"
    assert [(round(item.z, 6), round(item.radius, 6)) for item in profile.points] == [
        (-30, 5), (-10, 5), (-10, 10), (0, 10), (10, 8),
    ]


def test_edge_projection_uses_coordinates_relative_to_axis_origin() -> None:
    source = analysis_with_cylinder()
    source.cylindrical_features[0].center.z = 100
    source.visual_edges = [[
        Vec3(x=10, y=0, z=50),
        Vec3(x=10, y=0, z=150),
    ]]

    result = infer_rotational_features(source)

    assert [round(item.z) for item in result.profiles[0].points] == [-50, 50]


def test_prefers_exact_section_and_keeps_inner_profile_review_only() -> None:
    source = analysis_with_cylinder()
    source.cylindrical_features[0].id = "HF-1"
    source.cylindrical_features[0].source_face_ids = ["CF-1"]
    source = GeometryAnalysis.model_validate({
        **source.model_dump(),
        "rotational_sections": [{
        "source_feature_id": "CF-1",
        "axis_origin": {"x": 0, "y": 0, "z": 5},
        "axis": {"x": 0, "y": 0, "z": 1},
        "plane_normal": {"x": 0, "y": 1, "z": 0},
        "outer_profile": [
            {"z": -20, "radius": 10},
            {"z": -15, "radius": 10},
            {"z": -15, "radius": 8},
            {"z": -10, "radius": 8},
            {"z": -10, "radius": 10},
            {"z": 20, "radius": 10},
        ],
        "inner_profile": [
            {"z": -10, "radius": 3},
            {"z": 10, "radius": 3},
        ],
        "tolerance_mm": 0.005,
        "warnings": ["review inner envelope"],
        }],
    })
    source.visual_edges = [[Vec3(x=20, y=0, z=-50), Vec3(x=20, y=0, z=50)]]

    result = infer_rotational_features(source)

    assert result.status == "candidate"
    assert result.axes[0].origin.z == 5
    assert [profile.side for profile in result.profiles] == ["outer", "inner"]
    assert all(profile.extraction_method == "exact_section" for profile in result.profiles)
    assert [(point.z, point.radius) for point in result.profiles[0].points] == [
        (-20, 10), (-15, 10), (-15, 8), (-10, 8), (-10, 10), (20, 10),
    ]
    assert result.profiles[1].review_state == "review"
    assert result.evidence["profile_extraction_method"] == "exact_section"
    groove = next(feature for feature in result.features if feature.kind == "external_groove_candidate")
    assert groove.width_mm == 5
    assert groove.depth_mm == 2
    assert any(feature.kind == "cutoff_boundary" for feature in result.features)


def test_collapses_repeated_grooves_into_reviewable_thread_form() -> None:
    profile = RotationalProfile(
        id="RP-THREAD", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=z_value, radius=radius)
            for z_value, radius in [
                (0, 10), (1, 10), (1, 9), (1.4, 9), (1.4, 10),
                (2, 10), (2, 9), (2.4, 9), (2.4, 10),
                (3, 10), (3, 9), (3.4, 9), (3.4, 10),
                (4, 10), (4, 9), (4.4, 9), (4.4, 10),
                (5, 10), (5, 9), (5.4, 9), (5.4, 10), (6, 10),
            ]
        ],
        confidence=0.8,
    )

    features = _extract_profile_features(profile)
    thread = next(feature for feature in features if feature.kind == "thread_form_candidate")

    assert thread.repeat_count == 5
    assert round(thread.observed_repeat_mm or 0, 6) == 1
    assert [round(item, 6) for item in thread.pitch_candidates_mm] == [1, 2]
    assert thread.review_state == "review"


def test_binds_unique_periodic_tooth_form_to_verified_drawing_thread() -> None:
    thread_form = _extract_profile_features(RotationalProfile(
        id="RP-THREAD", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=z_value, radius=radius)
            for z_value, radius in [
                (0, 10), (0.5, 10),
                (0.5, 9), (0.8, 9), (0.8, 10),
                (1.29375, 10), (1.29375, 9), (1.59375, 9), (1.59375, 10),
                (2.0875, 10), (2.0875, 9), (2.3875, 9), (2.3875, 10),
                (2.88125, 10), (2.88125, 9), (3.18125, 9), (3.18125, 10),
                (3.675, 10), (3.675, 9), (3.975, 9), (3.975, 10), (4.5, 10),
            ]
        ], confidence=0.8,
    ))
    rotational = RotationalFeatureAnalysis(
        source_file="thread.step", status="candidate", features=thread_form,
    )
    requirements = import_measurement_specification({
        "comparison_rows": [{
            "drawing_entity": {
                "id": "THREAD-DRAWING", "semantic_type": "thread",
                "source": {"raw_text": "外螺纹 3/4-16 UNF-2A"},
            },
            "mapping_status": "matched", "verification_status": "verified_geometry",
            "cad_feature_ids": ["HF-THREAD"], "confidence": 0.95,
        }],
    })

    bound = bind_thread_requirements(rotational, requirements)
    feature = next(item for item in bound.features if item.kind == "thread_form_candidate")

    assert feature.binding_state == "matched"
    assert feature.drawing_requirement_ids == ["THREAD-DRAWING"]
    assert feature.resolved_pitch_mm == 1.5875
    assert feature.resolved_major_diameter_mm == 19.05
    assert feature.thread_side == "external"
    assert feature.thread_form_angle_degrees == 60
    assert feature.thread_designation == "3/4-16 UNF-2A"


def test_unverified_drawing_thread_is_only_an_ambiguous_suggestion() -> None:
    feature = _extract_profile_features(RotationalProfile(
        id="RP-THREAD", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=z_value, radius=radius)
            for z_value, radius in [
                (0, 10), (0.5, 10),
                (0.5, 9), (0.8, 9), (0.8, 10),
                (1.29375, 10), (1.29375, 9), (1.59375, 9), (1.59375, 10),
                (2.0875, 10), (2.0875, 9), (2.3875, 9), (2.3875, 10),
                (2.88125, 10), (2.88125, 9), (3.18125, 9), (3.18125, 10),
                (3.675, 10), (3.675, 9), (3.975, 9), (3.975, 10), (4.5, 10),
            ]
        ], confidence=0.8,
    ))
    rotational = RotationalFeatureAnalysis(
        source_file="thread.step", status="candidate", features=feature,
    )
    requirements = import_measurement_specification({
        "comparison_rows": [{
            "drawing_entity": {
                "id": "THREAD-OCR", "semantic_type": "thread", "parameter": "3/4-16 UNF",
                "raw_text": "3/4-16 UNF",
            },
            "mapping_status": "not_applicable", "verification_status": "recognized_only",
            "confidence": 0.96,
        }],
    })

    bound = bind_thread_requirements(rotational, requirements)
    suggestion = next(item for item in bound.features if item.kind == "thread_form_candidate")

    assert suggestion.binding_state == "ambiguous"
    assert suggestion.drawing_requirement_ids == ["THREAD-OCR"]
    assert suggestion.thread_designation == "3/4-16 UNF"
    assert suggestion.resolved_pitch_mm is None


def test_l32_job_persists_and_exposes_rotational_analysis(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = analysis_with_cylinder()
    job = JobResponse(
        id=job_id, status="completed", filename="shaft.step",
        created_at="2026-09-15T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis,
    )
    main.save_job(directory, job)

    response = client.post(f"/api/v1/jobs/{job_id}/turning/analyze")
    artifact = client.get(f"/api/v1/jobs/{job_id}/files/rotational-features.json")

    assert response.status_code == 200
    assert artifact.status_code == 200
    assert response.json()["axes"][0]["id"] == "RA-1"
    assert (directory / "rotational-features.json").is_file()


def test_non_l32_job_rejects_rotational_analysis_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "b" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    job = JobResponse(
        id=job_id, status="completed", filename="part.step",
        created_at="2026-09-15T00:00:00+00:00", material="S45C",
        machine="VMC850", device_id="seksun-freecad-cam-standard",
        analysis=analysis_with_cylinder(),
    )
    main.save_job(directory, job)

    response = client.post(f"/api/v1/jobs/{job_id}/turning/analyze")

    assert response.status_code == 409
