from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.models import GeometryAnalysis, JobResponse, RotationalSectionCandidate
from app.planner import build_process_plan
from app.rotational_features import infer_rotational_features
from app.requirements_adapter import import_measurement_specification


client = TestClient(app)


def shaft_analysis(radius: float = 10.0) -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "shaft.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "volume": 10_000,
            "surface_area": 3_000,
            "bounding_box": {
                "minimum": {"x": -radius, "y": -radius, "z": -25},
                "maximum": {"x": radius, "y": radius, "z": 25},
                "size": {"x": radius * 2, "y": radius * 2, "z": 50},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "CF-SHAFT",
            "kind": "cylinder",
            "radius": radius,
            "diameter": radius * 2,
            "length": 50,
            "center": {"x": 0, "y": 0, "z": 0},
            "axis": {"x": 0, "y": 0, "z": 1},
            "angular_span_degrees": 360,
            "source_face_ids": ["F1"],
            "review_state": "accepted",
            "confidence": 0.95,
        }],
        "visual_edges": [],
    })


def threaded_shaft_analysis() -> GeometryAnalysis:
    source = shaft_analysis(radius=9.525)
    source.cylindrical_features[0].id = "HF-THREAD"
    source.cylindrical_features[0].source_face_ids = ["CF-THREAD"]
    points = [(0, 9.525), (0.5, 9.525)]
    for start in (0.5, 1.29375, 2.0875, 2.88125, 3.675):
        points.extend([
            (start, 8.8), (start + 0.3, 8.8), (start + 0.3, 9.525),
        ])
    points.append((4.5, 9.525))
    source.rotational_sections = [RotationalSectionCandidate.model_validate({
        "source_feature_id": "CF-THREAD",
        "axis_origin": {"x": 0, "y": 0, "z": 0},
        "axis": {"x": 0, "y": 0, "z": 1},
        "plane_normal": {"x": 0, "y": 1, "z": 0},
        "outer_profile": [
            {"z": z_value, "radius": radius} for z_value, radius in points
        ],
        "tolerance_mm": 0.005,
    })]
    return source


def bored_shaft_analysis() -> GeometryAnalysis:
    source = shaft_analysis(radius=10)
    source.cylindrical_features[0].id = "HF-BORE"
    source.cylindrical_features[0].source_face_ids = ["CF-BORE"]
    source.rotational_sections = [RotationalSectionCandidate.model_validate({
        "source_feature_id": "CF-BORE",
        "axis_origin": {"x": 0, "y": 0, "z": 0},
        "axis": {"x": 0, "y": 0, "z": 1},
        "plane_normal": {"x": 0, "y": 1, "z": 0},
        "outer_profile": [{"z": -25, "radius": 10}, {"z": 25, "radius": 10}],
        "inner_profile": [{"z": -20, "radius": 6}, {"z": 0, "radius": 8}],
        "tolerance_mm": 0.005,
    })]
    return source


def drawing_thread_requirements(verified: bool = True):
    return import_measurement_specification({
        "comparison_rows": [{
            "drawing_entity": {
                "id": "THREAD-DRAWING", "semantic_type": "thread",
                "source": {"raw_text": "外螺纹 3/4-16 UNF-2A"},
            },
            "mapping_status": "matched" if verified else "not_applicable",
            "verification_status": "verified_geometry" if verified else "recognized_only",
            "cad_feature_ids": ["HF-THREAD"] if verified else [],
            "confidence": 0.95,
        }],
    })


def test_l32_builds_formal_turning_plan_from_rotational_profile() -> None:
    plan = build_process_plan(shaft_analysis(), "S45C", "Citizen Cincom L32")

    assert plan.machine_profile is not None
    assert plan.machine_profile.id == "citizen-cincom-l32"
    assert plan.stock["type"] == "round_bar"
    assert plan.stock["diameter_mm"] == 22
    assert plan.stock["length_mm"] == 54
    assert plan.stock["rotational_profile_id"] == "RP-OUTER-1"
    assert plan.stock["profile_review_state"] == "review"
    assert [operation.type for operation in plan.setups[0].operations] == [
        "turn_facing",
        "turn_od_roughing",
        "turn_od_finishing",
        "turn_cutoff",
    ]
    assert [operation.id for operation in plan.setups[1].operations] == ["OP50", "OP60"]
    assert all(not operation.enabled for operation in plan.setups[1].operations)
    assert all(operation.channel_id == "sub" for operation in plan.setups[1].operations)
    assert all(operation.spindle_id == "sub" for operation in plan.setups[1].operations)
    assert all(
        operation.feature_ids == ["RP-OUTER-1"]
        for operation in plan.setups[0].operations
    )
    assert all(
        operation.channel_id == "main"
        and operation.spindle_id == "main"
        and operation.workpiece_side == "front"
        for operation in plan.setups[0].operations
    )
    assert plan.automation_status == "review"
    assert plan.coverage is not None
    assert plan.manufacturing_route is not None
    assert plan.manufacturing_route.part_family == "rotational"
    assert any("DRAFT" in item for item in plan.manufacturing_route.capability_gaps)


def test_l32_marks_38mm_option_without_rejecting_supported_stock() -> None:
    plan = build_process_plan(shaft_analysis(radius=18), "S45C", "Citizen Cincom L32")

    assert plan.stock["diameter_mm"] == 38
    assert plan.stock["required_option"] == "bar_diameter_38mm"
    assert plan.automation_status == "review"
    assert plan.blocking_reasons == []


def test_verified_thread_binding_adds_disabled_threading_draft() -> None:
    plan = build_process_plan(
        threaded_shaft_analysis(), "S45C", "Citizen Cincom L32",
        requirements=drawing_thread_requirements(),
    )

    threading = [
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "turn_threading"
    ]
    assert len(threading) == 1
    assert threading[0].enabled is False
    assert threading[0].parameters["pitch_mm"] == 1.5875
    assert threading[0].parameters["major_diameter_mm"] == 19.05
    assert "THREAD-DRAWING" in threading[0].feature_ids


def test_recognized_only_thread_does_not_add_threading_draft() -> None:
    plan = build_process_plan(
        threaded_shaft_analysis(), "S45C", "Citizen Cincom L32",
        requirements=drawing_thread_requirements(verified=False),
    )

    assert not any(
        operation.type == "turn_threading"
        for setup in plan.setups for operation in setup.operations
    )


def test_l32_rejects_stock_larger_than_known_38mm_option() -> None:
    plan = build_process_plan(shaft_analysis(radius=19), "S45C", "Citizen Cincom L32")

    assert plan.stock["diameter_mm"] == 40
    assert plan.automation_status == "unsupported"
    assert plan.blocking_reasons
    assert plan.setups == []


def test_l32_requires_a_reviewable_outer_profile() -> None:
    analysis = shaft_analysis()
    analysis.cylindrical_features = []

    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")

    assert plan.automation_status == "unsupported"
    assert plan.setups == []
    assert plan.blocking_reasons


def test_l32_requires_target_selection_for_multi_solid_input() -> None:
    analysis = shaft_analysis()
    analysis.topology["source_solids"] = 2

    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")

    assert plan.automation_status == "unsupported"
    assert plan.setups == []
    assert any("多个实体" in reason for reason in plan.blocking_reasons)


def test_profile_review_updates_the_formal_plan_and_invalidates_cam(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "c" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    job = JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at="2026-09-15T00:00:00+00:00",
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=plan,
    )
    main.save_job(directory, job)
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    (directory / "toolpath.json").write_text("{}", encoding="utf-8")

    response = client.patch(
        f"/api/v1/jobs/{job_id}/turning/profiles/RP-OUTER-1",
        json={"review_state": "accepted"},
    )

    assert response.status_code == 200
    persisted = main.load_job(job_id)
    assert persisted.plan is not None
    assert persisted.plan.stock["profile_review_state"] == "accepted"
    assert persisted.plan.coverage is not None
    assert persisted.plan.coverage.targets[-1].state == "covered"
    assert not (directory / "toolpath.json").exists()


def test_accepted_exact_inner_profile_persists_and_adds_disabled_boring_drafts(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "f" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = bored_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    job = JobResponse(
        id=job_id, status="completed", filename="bored.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    main.persist_rotational_analysis(directory, job, analysis)

    response = client.patch(
        f"/api/v1/jobs/{job_id}/turning/profiles/RP-INNER-1",
        json={"review_state": "accepted"},
    )

    assert response.status_code == 200
    assert next(
        item for item in response.json()["profiles"] if item["id"] == "RP-INNER-1"
    )["review_state"] == "accepted"
    persisted = main.load_job(job_id)
    assert persisted.analysis is not None
    assert persisted.analysis.rotational_profile_reviews["RP-INNER-1"] == "accepted"
    assert persisted.plan is not None
    boring = [
        operation for setup in persisted.plan.setups for operation in setup.operations
        if operation.type in {"turn_id_roughing", "turn_id_finishing"}
    ]
    assert [operation.id for operation in boring] == ["OP25", "OP28"]
    assert all(not operation.enabled for operation in boring)
    assert boring[0].parameters["required_initial_bore_diameter_mm"] == 10.4
    prebore = next(
        operation for setup in persisted.plan.setups for operation in setup.operations
        if operation.type == "axial_drilling"
    )
    assert prebore.id == "OP22"
    assert prebore.enabled is False
    assert prebore.tool.id == "DRILL-11.0"
    assert prebore.parameters["maximum_prebore_diameter_mm"] == 11.5

    short_drill = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/OP22/review",
        json={
            "drill_tool_id": "DRILL-11.0",
            "confirmed_stickout_mm": 20.0,
            "drill_point_angle_deg": 118,
            "peck_depth_mm": 2.0,
            "bottom_condition": "blind_tip_allowance_confirmed",
            "tip_overtravel_allowance_mm": 4.0,
            "drill_inventory_id": "DRILL-11-01",
            "reviewer": "test-engineer",
        },
    )
    assert short_drill.status_code == 422

    reviewed_drill = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/OP22/review",
        json={
            "drill_tool_id": "DRILL-11.0",
            "confirmed_stickout_mm": 30.0,
            "drill_point_angle_deg": 118,
            "peck_depth_mm": 2.0,
            "bottom_condition": "blind_tip_allowance_confirmed",
            "tip_overtravel_allowance_mm": 4.0,
            "drill_inventory_id": "DRILL-11-01",
            "reviewer": "test-engineer",
        },
    )
    assert reviewed_drill.status_code == 200
    reviewed_prebore = next(
        operation
        for setup in reviewed_drill.json()["plan"]["setups"]
        for operation in setup["operations"]
        if operation["id"] == "OP22"
    )
    assert reviewed_prebore["enabled"] is True
    assert reviewed_prebore["parameters"]["full_diameter_depth_mm"] == 20.0
    assert reviewed_prebore["parameters"]["engineering_review_status"] == "verified_engineer"
    assert (directory / "axial-drilling-review.json").is_file()

    rejected = client.post(
        f"/api/v1/jobs/{job_id}/turning/boring-operations/OP25/review",
        json={
            "initial_bore_diameter_mm": 8.0,
            "confirmed_stickout_mm": 22.0,
            "assembly_clearance_mm": 0.2,
            "boring_bar_inventory_id": "BAR-ID-R-01",
            "reviewer": "test-engineer",
        },
    )
    assert rejected.status_code == 422

    reviewed = client.post(
        f"/api/v1/jobs/{job_id}/turning/boring-operations/OP25/review",
        json={
            "initial_bore_diameter_mm": 11.0,
            "confirmed_stickout_mm": 22.0,
            "assembly_clearance_mm": 0.2,
            "boring_bar_inventory_id": "BAR-ID-R-01",
            "reviewer": "test-engineer",
        },
    )
    assert reviewed.status_code == 200
    reviewed_operation = next(
        operation
        for setup in reviewed.json()["plan"]["setups"]
        for operation in setup["operations"]
        if operation["id"] == "OP25"
    )
    assert reviewed_operation["enabled"] is True
    assert reviewed_operation["tool"]["stickout_mm"] == 22.0
    assert reviewed_operation["parameters"]["initial_bore_diameter_mm"] == 11.0
    assert reviewed_operation["parameters"]["boring_bar_inventory_id"] == "BAR-ID-R-01"
    assert reviewed_operation["parameters"]["engineering_review_status"] == "verified_engineer"
    assert (directory / "boring-reachability.json").is_file()

    drill_bypass = client.patch(
        f"/api/v1/jobs/{job_id}/setups/SETUP-L32-MAIN/operations/OP22",
        json={"enabled": False},
    )
    assert drill_bypass.status_code == 409

    bypass = client.patch(
        f"/api/v1/jobs/{job_id}/setups/SETUP-L32-MAIN/operations/OP25",
        json={"enabled": False},
    )
    assert bypass.status_code == 409

    refreshed = client.post(f"/api/v1/jobs/{job_id}/turning/analyze")
    assert refreshed.status_code == 200
    assert next(
        item for item in refreshed.json()["profiles"] if item["id"] == "RP-INNER-1"
    )["review_state"] == "accepted"


def test_engineer_confirmation_rebuilds_plan_with_disabled_threading_draft(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = threaded_shaft_analysis()
    requirements = drawing_thread_requirements(verified=False)
    plan = build_process_plan(
        analysis, "S45C", "Citizen Cincom L32", requirements=requirements,
    )
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="threaded-shaft.step",
        created_at="2026-09-15T00:00:00+00:00",
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=plan,
    ))
    (directory / "turning-toolpath-ir.json").write_text("{}", encoding="utf-8")

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/thread-bindings/confirm",
        json={
            "thread_feature_id": "TPF-OUTER-THREAD-1",
            "requirement_id": "THREAD-DRAWING",
        },
    )

    assert response.status_code == 200
    body = response.json()
    confirmed = body["plan"]["manufacturing_requirements"]["requirements"][0]
    assert confirmed["mapping_status"] == "matched"
    assert confirmed["verification_status"] == "verified_engineer"
    assert confirmed["cad_feature_ids"] == ["TPF-OUTER-THREAD-1"]
    threading = [
        operation
        for setup in body["plan"]["setups"]
        for operation in setup["operations"]
        if operation["type"] == "turn_threading"
    ]
    assert len(threading) == 1
    assert threading[0]["enabled"] is False
    assert threading[0]["parameters"]["pitch_mm"] == 1.5875
    assert not (directory / "turning-toolpath-ir.json").exists()
    stored_rotational = client.get(
        f"/api/v1/jobs/{job_id}/files/rotational-features.json",
    ).json()
    bound = next(
        item for item in stored_rotational["features"]
        if item["kind"] == "thread_form_candidate"
    )
    assert bound["binding_state"] == "matched"
    assert bound["resolved_pitch_mm"] == 1.5875

    invalid_review = client.post(
        f"/api/v1/jobs/{job_id}/turning/thread-operations/OP35-T1/review",
        json={
            "start_z_mm": 0.5,
            "end_z_mm": 3.975,
            "thread_depth_mm": 0.7,
            "pass_count": 8,
            "relief_strategy": "groove",
            "relief_width_mm": 0.2,
            "tool_insert_id": "16ER-16UN",
            "controller_cycle_id": "L32-G92-DRAFT",
            "reviewer": "test-engineer",
        },
    )
    assert invalid_review.status_code == 422

    review = client.post(
        f"/api/v1/jobs/{job_id}/turning/thread-operations/OP35-T1/review",
        json={
            "start_z_mm": 3.975,
            "end_z_mm": 0.5,
            "thread_depth_mm": 0.7,
            "pass_count": 8,
            "relief_strategy": "groove",
            "relief_width_mm": 1.0,
            "tool_insert_id": "16ER-16UN",
            "controller_cycle_id": "L32-G92-DRAFT",
            "reviewer": "test-engineer",
        },
    )

    assert review.status_code == 200
    reviewed_thread = next(
        operation
        for setup in review.json()["plan"]["setups"]
        for operation in setup["operations"]
        if operation["id"] == "OP35-T1"
    )
    assert reviewed_thread["enabled"] is True
    assert reviewed_thread["parameters"]["engineering_review_status"] == "verified_engineer"
    assert reviewed_thread["parameters"]["tool_insert_id"] == "16ER-16UN"
    assert reviewed_thread["parameters"]["controller_cycle_id"] == "L32-G92-DRAFT"
    assert reviewed_thread["parameters"]["minor_diameter_mm"] == 17.65
    assert "RP-OUTER-1" in reviewed_thread["feature_ids"]

    bypass = client.patch(
        f"/api/v1/jobs/{job_id}/setups/SETUP-L32-MAIN/operations/OP35-T1",
        json={"parameters": {"thread_depth_mm": 0.2}},
    )
    assert bypass.status_code == 409


def test_excluded_rotational_profile_is_not_reported_as_covered() -> None:
    analysis = shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    plan.stock["profile_review_state"] = "excluded"

    plan.coverage = main.evaluate_plan_coverage(analysis, plan)

    assert plan.coverage.targets[-1].state == "uncovered"


def test_generic_cam_endpoint_is_blocked_for_l32(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = shaft_analysis()
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at="2026-09-15T00:00:00+00:00",
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=build_process_plan(analysis, "S45C", "Citizen Cincom L32"),
    ))

    response = client.post(f"/api/v1/jobs/{job_id}/cam")

    assert response.status_code == 409
    assert "DRAFT Toolpath IR" in response.json()["detail"]
