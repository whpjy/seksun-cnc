from fastapi.testclient import TestClient

from app import main
from app.l32_configuration import snapshot_l32_instance
from app.l32_front_chain import FrontChainDraftRequest, compile_front_chain_draft
from app.main import app
from app.machine_models import MachineInstance
from app.models import GeometryAnalysis, JobResponse, PlanarFeature, PrismaticFeature, RotationalSectionCandidate, Vec3
from app.planner import build_process_plan
from app.rotational_features import RotationalFeatureAnalysis, clip_rotational_profile, infer_rotational_features
from app.requirements_adapter import import_measurement_specification
from app.turning_draft import TurningDraftRequest, compile_turning_draft
from app.turning_simulation import simulate_turning_stock
from app.turning_verification import verify_turning_profile


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


def test_l32_manual_turning_operation_can_be_inserted_with_machine_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main,"STORAGE_ROOT",tmp_path)
    job_id="1"*32
    directory=tmp_path/job_id
    directory.mkdir()
    analysis=shaft_analysis()
    plan=build_process_plan(analysis,"S45C","Citizen Cincom L32")
    main.save_job(directory,JobResponse(
        id=job_id,status="completed",filename="shaft.step",created_at=main.utc_now(),
        material="S45C",machine="Citizen Cincom L32",device_id="citizen-cincom-l32",
        analysis=analysis,plan=plan,
    ))
    setup=plan.setups[0]
    neighbour=next(item for item in setup.operations if item.id=="OP20")
    response=client.post(
        f"/api/v1/jobs/{job_id}/setups/{setup.id}/operations",
        json={
            "definition_id":"turn_od_finishing","feature_ids":neighbour.feature_ids,
            "tool_id":"TURN-OD-F","name":"人工插入精车",
            "parameters":{"radial_allowance_mm":0.05},"insert_after_operation_id":"OP20",
        },
    )
    assert response.status_code==200,response.text
    operations=response.json()["plan"]["setups"][0]["operations"]
    index=next(index for index,item in enumerate(operations) if item["name"]=="人工插入精车")
    created=operations[index]
    assert operations[index-1]["id"]=="OP20"
    assert created["source"]=="manual"
    assert created["channel_id"]==neighbour.channel_id
    assert created["spindle_id"]==neighbour.spindle_id
    assert created["workpiece_side"]==neighbour.workpiece_side
    assert [item["sequence"] for item in operations]==list(range(10,10*len(operations)+1,10))


def regional_shaft_analysis() -> GeometryAnalysis:
    source = shaft_analysis(radius=10)
    source.cylindrical_features[0].id = "HF-REGIONAL"
    source.cylindrical_features[0].source_face_ids = ["CF-REGIONAL"]
    source.rotational_sections = [RotationalSectionCandidate.model_validate({
        "source_feature_id": "CF-REGIONAL",
        "axis_origin": {"x": 0, "y": 0, "z": 0},
        "axis": {"x": 0, "y": 0, "z": 1},
        "plane_normal": {"x": 0, "y": 1, "z": 0},
        "outer_profile": [
            {"z": -20, "radius": 9},
            {"z": -10, "radius": 5},
            {"z": 0, "radius": 10},
        ],
        "tolerance_mm": 0.005,
    })]
    source.rotational_profile_reviews["RP-OUTER-1"] = "accepted"
    return source


def front_form_shaft_analysis() -> GeometryAnalysis:
    source = regional_shaft_analysis()
    source.rotational_sections[0].outer_profile = [
        source.rotational_sections[0].outer_profile[0].model_copy(
            update={"z": z_value, "radius": radius},
        )
        for z_value, radius in [
            (-8, 2), (-1.15, 10.05), (-0.95, 10.25),
            (0.95, 10.25), (1.15, 10.05), (1.216667, 2), (3.05, 0.5),
        ]
    ]
    return source


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
    source.cylindrical_features.append(source.cylindrical_features[0].model_copy(update={
        "id": "HF-INNER-BORE",
        "kind": "hole",
        "radius": 6,
        "diameter": 12,
        "length": 20,
        "center": Vec3(x=0, y=0, z=-10),
        "source_face_ids": ["CF-INNER-BORE"],
        "review_state": "accepted",
    }))
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


def grooved_shaft_analysis() -> GeometryAnalysis:
    source = shaft_analysis(radius=10)
    source.cylindrical_features[0].id = "HF-GROOVE"
    source.cylindrical_features[0].source_face_ids = ["CF-GROOVE"]
    source.rotational_sections = [RotationalSectionCandidate.model_validate({
        "source_feature_id": "CF-GROOVE",
        "axis_origin": {"x": 0, "y": 0, "z": 0},
        "axis": {"x": 0, "y": 0, "z": 1},
        "plane_normal": {"x": 0, "y": 1, "z": 0},
        "outer_profile": [
            {"z": -25, "radius": 10},
            {"z": -5, "radius": 10},
            {"z": -5, "radius": 8},
            {"z": 0, "radius": 8},
            {"z": 0, "radius": 10},
            {"z": 25, "radius": 10},
        ],
        "tolerance_mm": 0.005,
    })]
    source.rotational_profile_reviews["RP-OUTER-1"] = "accepted"
    return source


def internally_grooved_shaft_analysis() -> GeometryAnalysis:
    source = bored_shaft_analysis()
    point = source.rotational_sections[0].inner_profile[0]
    source.rotational_sections[0].inner_profile = [
        point.model_copy(update={"z": z_value, "radius": radius})
        for z_value, radius in [
            (-20, 6), (-12, 6), (-12, 7.5), (-9, 7.5), (-9, 6), (0, 6),
        ]
    ]
    source.rotational_profile_reviews["RP-INNER-1"] = "accepted"
    return source


def deep_bored_shaft_analysis() -> GeometryAnalysis:
    source = bored_shaft_analysis()
    bounds = source.measurements["bounding_box"]
    bounds.minimum.z = -65
    bounds.maximum.z = 0
    bounds.size.z = 65
    source.rotational_sections[0].outer_profile = [
        source.rotational_sections[0].outer_profile[0].model_copy(update={"z": -65}),
        source.rotational_sections[0].outer_profile[-1].model_copy(update={"z": 0}),
    ]
    source.rotational_sections[0].inner_profile = [
        source.rotational_sections[0].inner_profile[0].model_copy(update={"z": -65}),
        source.rotational_sections[0].inner_profile[-1].model_copy(update={"z": 0, "radius": 8}),
    ]
    return source


def stepped_bored_shaft_analysis() -> GeometryAnalysis:
    source = bored_shaft_analysis()
    bounds = source.measurements["bounding_box"]
    bounds.minimum.z = -40
    bounds.maximum.z = 0
    bounds.size.z = 40
    source.rotational_sections[0].outer_profile = [
        source.rotational_sections[0].outer_profile[0].model_copy(update={"z": -40}),
        source.rotational_sections[0].outer_profile[-1].model_copy(update={"z": 0}),
    ]
    point = source.rotational_sections[0].inner_profile[0]
    source.rotational_sections[0].inner_profile = [
        point.model_copy(update={"z": -40, "radius": 6}),
        point.model_copy(update={"z": -15, "radius": 6}),
        point.model_copy(update={"z": -15, "radius": 8}),
        point.model_copy(update={"z": 0, "radius": 8}),
    ]
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
    assert [operation.id for operation in plan.setups[1].operations] == ["OP50"]
    cutoff = next(item for item in plan.setups[0].operations if item.id == "OP40")
    assert cutoff.parameters["z_mm"] == -26.25
    assert cutoff.parameters["finished_back_datum_z_mm"] == -25
    assert cutoff.parameters["retained_material_min_z_mm"] == -25.25
    assert cutoff.parameters["back_face_allowance_mm"] == 0.25
    assert cutoff.parameters["sacrificial_extension_mm"] == 2.25
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


def test_partial_rotational_section_cannot_define_whole_part_cutoff() -> None:
    analysis = regional_shaft_analysis()
    analysis.prismatic_features.append(PrismaticFeature.model_validate({
        "id": "MF-1", "kind": "pocket", "source_face_id": "PF-1",
        "center": {"x": 0, "y": 0, "z": -20},
        "bounds": {
            "minimum": {"x": -1, "y": -1, "z": -20},
            "maximum": {"x": 1, "y": 1, "z": -20},
            "size": {"x": 2, "y": 2, "z": 0},
        },
        "access_direction": {"x": 0, "y": 0, "z": -1},
        "length": 2, "width": 2, "depth": 1, "review_state": "accepted",
    }))
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")

    assert plan.stock["profile_axial_complete"] is False
    assert plan.stock["length_mm"] == 54
    assert plan.stock["finished_back_z_mm"] == -25
    cutoff = next(item for item in plan.setups[0].operations if item.id == "OP40")
    assert cutoff.parameters["z_mm"] == -26.25
    assert cutoff.parameters["finished_back_datum_z_mm"] == -25
    assert plan.automation_status == "review"
    assert plan.coverage is not None
    assert next(target for target in plan.coverage.targets if target.kind == "outer_profile").state == "uncovered"


def test_l32_od_turning_stops_before_nonrotational_protrusion(tmp_path, monkeypatch) -> None:
    analysis = regional_shaft_analysis()
    analysis.planar_features.append(PlanarFeature.model_validate({
        "id": "PF-SIDE", "area": 12,
        "center": {"x": 11, "y": 0, "z": -17.5},
        "normal": {"x": 1, "y": 0, "z": 0},
        "bounds": {
            "minimum": {"x": 11, "y": -1, "z": -25},
            "maximum": {"x": 11, "y": 1, "z": -10},
            "size": {"x": 0, "y": 2, "z": 15},
        },
    }))
    analysis.prismatic_features.append(PrismaticFeature.model_validate({
        "id": "MF-1", "kind": "pocket", "source_face_id": "PF-SIDE",
        "center": {"x": 0, "y": 0, "z": -20},
        "bounds": {
            "minimum": {"x": -1, "y": -1, "z": -20},
            "maximum": {"x": 1, "y": 1, "z": -20},
            "size": {"x": 2, "y": 2, "z": 0},
        },
        "access_direction": {"x": 0, "y": 0, "z": -1},
        "length": 2, "width": 2, "depth": 1, "review_state": "accepted",
    }))

    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")

    assert plan.stock["nonrotational_turning_limit_z_mm"] == -10
    assert plan.stock["nonrotational_region_z_mm"] == [-25, -10]
    assert plan.stock["nonrotational_material_verified"] is False
    assert plan.coverage is not None
    operation_by_id = {
        operation.id: operation
        for setup in plan.setups for operation in setup.operations
    }
    assert {"OP34-NR-R", "OP36-NR-F", "OP52-P1-R", "OP53-P1-F"} <= set(operation_by_id)
    assert operation_by_id["OP34-NR-R"].tool.id == "EM-1"
    assert operation_by_id["OP34-NR-R"].parameters["required_module"] == "U30B"
    assert operation_by_id["OP52-P1-R"].channel_id == "sub"
    assert operation_by_id["OP52-P1-R"].parameters["required_module"] == "U151B"
    rotational_target = next(
        target for target in plan.coverage.targets
        if target.id == "TARGET-RP-OUTER-1"
    )
    assert rotational_target.state == "covered"
    external = next(
        target for target in plan.coverage.targets
        if target.id.startswith("TARGET-NONROTATIONAL-OUTER-")
    )
    assert external.state == "uncovered"
    assert external.covered_by == ["OP34-NR-R", "OP36-NR-F"]
    assert external.required_operation_types == [
        "live_tool_contour_finishing", "live_tool_contour_roughing",
    ]
    # Older stored jobs have no such target yet. A read must expose the new
    # geometry blocker without mutating the stored plan or machine binding.
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "b" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    plan.coverage = None
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="regional.step",
        created_at=main.utc_now(), material="S45C", machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32", analysis=analysis, plan=plan,
    ))
    original = (directory / "job.json").read_bytes()
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    targets = response.json()["plan"]["coverage"]["targets"]
    assert any(item["id"].startswith("TARGET-NONROTATIONAL-OUTER-") for item in targets)
    assert (directory / "job.json").read_bytes() == original
    for operation in plan.setups[0].operations:
        if operation.id in {"OP20", "OP30"}:
            assert operation.parameters["profile_z_min_mm"] == -10
            assert operation.parameters["profile_region_complete"] is False


def test_l32_plans_only_reachable_front_region_and_keeps_backside_uncovered() -> None:
    plan = build_process_plan(
        regional_shaft_analysis(), "S45C", "Citizen Cincom L32",
    )

    longitudinal = [
        operation for operation in plan.setups[0].operations
        if operation.type in {"turn_od_roughing", "turn_od_finishing"}
    ]
    assert len(longitudinal) == 2
    for operation in longitudinal:
        assert operation.parameters["cut_direction"] == "negative_z"
        assert operation.parameters["profile_z_min_mm"] == -10
        assert operation.parameters["profile_z_max_mm"] == 0
        assert operation.parameters["profile_region_complete"] is False

    unresolved = plan.stock["unresolved_turning_regions"]
    assert unresolved == [{
        "side": "back_candidate",
        "z_min_mm": -20,
        "z_max_mm": -10,
        "required_capability": "back_turning",
        "status": "uncovered",
    }]
    assert plan.coverage is not None
    outer = next(item for item in plan.coverage.targets if item.kind == "outer_profile")
    assert outer.state == "uncovered"
    assert plan.coverage.status == "incomplete"


def test_l32_separates_front_form_and_selects_small_nose_finish_tool() -> None:
    plan = build_process_plan(
        front_form_shaft_analysis(), "S45C", "Citizen Cincom L32",
    )

    finish = next(
        operation for operation in plan.setups[0].operations if operation.id == "OP30"
    )
    assert finish.parameters["profile_z_min_mm"] == -8
    assert finish.parameters["profile_z_max_mm"] == 1.15
    assert finish.parameters["profile_region_complete"] is False
    assert finish.parameters["maximum_finish_nose_radius_mm"] == 0.2
    assert finish.tool.id == "TURN-OD-MICRO-F"
    form_operations = {
        operation.id: operation for operation in plan.setups[0].operations
        if operation.id in {"OP21-FORM", "OP31-FORM"}
    }
    assert set(form_operations) == {"OP21-FORM", "OP31-FORM"}
    assert form_operations["OP21-FORM"].tool.id == "TURN-OD-L-R"
    assert form_operations["OP31-FORM"].tool.id == "TURN-OD-L-MICRO-F"
    assert all(
        operation.parameters["cut_direction"] == "positive_z"
        and operation.parameters["profile_z_min_mm"] == 1.15
        and operation.parameters["profile_z_max_mm"] == 3.05
        for operation in form_operations.values()
    )
    assert plan.stock["planned_turning_regions"] == [{
        "side": "front_form_candidate",
        "z_min_mm": 1.15,
        "z_max_mm": 3.05,
        "required_capability": "front_form_turning",
        "status": "draft_planned",
        "operation_ids": ["OP21-FORM", "OP31-FORM"],
    }]
    assert "unresolved_turning_regions" not in plan.stock


def test_front_form_api_rejects_tampered_regional_boundaries(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = front_form_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    snapshot = snapshot_l32_instance(MachineInstance(
        id="l32-form-test", definition_id="citizen-cincom-l32",
        name="L32 form test", variant="VIII", operation_mode="guide_bushing",
        installed_modules=[], bar_diameter_mm=32,
    ))
    job = JobResponse(
        id=job_id, status="completed", filename="form.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    main.write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    operation = next(item for item in plan.setups[0].operations if item.id == "OP31-FORM")
    profile = next(item for item in rotational.profiles if item.id == "RP-OUTER-1")
    request = {
        "machine_instance_id": snapshot.instance.id,
        "operation": operation.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        "stock_radius_mm": plan.stock["diameter_mm"] / 2,
        "z_min_mm": -10,
        "z_max_mm": 5.05,
        "resolution_mm": 0.1,
    }

    accepted = client.post(f"/api/v1/jobs/{job_id}/turning/draft", json=request)
    request["operation"]["parameters"]["profile_z_min_mm"] = -8
    tampered = client.post(f"/api/v1/jobs/{job_id}/turning/draft", json=request)

    assert accepted.status_code == 200
    assert accepted.json()["verification"]["status"] == "passed"
    assert tampered.status_code == 409
    assert "exactly match" in tampered.json()["detail"]


def test_front_form_four_stage_chain_preserves_stock_without_overcut() -> None:
    analysis = front_form_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    profile = next(
        item for item in infer_rotational_features(analysis).profiles
        if item.side == "outer"
    )
    snapshot = snapshot_l32_instance(MachineInstance(
        id="l32-form-chain", definition_id="citizen-cincom-l32",
        name="L32 form chain", variant="VIII", operation_mode="guide_bushing",
        installed_modules=[], bar_diameter_mm=32,
    ))
    operations = {
        item.id: item for setup in plan.setups for item in setup.operations
    }
    formal_chain = compile_front_chain_draft(
        "a" * 32,
        FrontChainDraftRequest(
            machine_instance_id=snapshot.instance.id,
            source_profile_id=profile.id,
            stock_radius_mm=plan.stock["diameter_mm"] / 2,
            resolution_mm=0.1,
        ),
        plan, profile, snapshot,
    )
    assert formal_chain.status == "passed"
    assert formal_chain.nc_generated is False
    assert [item.operation_id for item in formal_chain.stages] == [
        "OP20", "OP21-FORM", "OP30", "OP31-FORM",
    ]
    assert formal_chain.stages[1].initial_volume_mm3 == formal_chain.stages[0].final_volume_mm3
    assert formal_chain.longitudinal_verification.status == "passed"
    assert formal_chain.front_form_verification.status == "passed"
    stock = None
    for operation_id in ("OP20", "OP21-FORM", "OP30", "OP31-FORM"):
        operation = operations[operation_id]
        request = TurningDraftRequest(
            machine_instance_id=snapshot.instance.id,
            operation=operation, profile=profile,
            stock_radius_mm=plan.stock["diameter_mm"] / 2,
            z_min_mm=-10, z_max_mm=5.05, resolution_mm=0.1,
        )
        draft = compile_turning_draft("a" * 32, request, snapshot)
        stock = simulate_turning_stock(
            draft.toolpath,
            stock_radius_mm=request.stock_radius_mm,
            z_min_mm=request.z_min_mm, z_max_mm=request.z_max_mm,
            resolution_mm=request.resolution_mm,
            initial_samples=stock.samples if stock else None,
        )
        regional = clip_rotational_profile(
            profile,
            operation.parameters["profile_z_min_mm"],
            operation.parameters["profile_z_max_mm"],
        )
        verification = verify_turning_profile(
            regional, stock,
            expected_allowance_mm=operation.parameters["radial_allowance_mm"],
        )
        assert verification.metrics.maximum_overcut_mm <= 0.05
        if operation_id in {"OP30", "OP31-FORM"}:
            assert verification.status == "passed"


def test_front_chain_api_requires_bound_machine_and_canonical_regions(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = front_form_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="front-form.step",
        created_at=main.utc_now(), material="S45C", machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32", analysis=analysis, plan=plan,
    ))
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    request = {
        "machine_instance_id": "l32-front-api",
        "source_profile_id": "RP-OUTER-1",
        "stock_radius_mm": plan.stock["diameter_mm"] / 2,
        "resolution_mm": 0.1,
    }
    unbound = client.post(f"/api/v1/jobs/{job_id}/turning/front-chain/draft", json=request)
    assert unbound.status_code == 409

    snapshot = snapshot_l32_instance(MachineInstance(
        id="l32-front-api", definition_id="citizen-cincom-l32",
        name="L32 front test", variant="VIII", operation_mode="guide_bushing",
        installed_modules=[], bar_diameter_mm=32,
    ))
    machine_path = tmp_path / ".machine-instances" / "l32-front-api.json"
    machine_path.parent.mkdir()
    main.write_json(machine_path, snapshot.model_dump(mode="json"))
    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": snapshot.instance.id},
    )
    assert bound.status_code == 200
    accepted = client.post(f"/api/v1/jobs/{job_id}/turning/front-chain/draft", json=request)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "passed"
    assert accepted.json()["nc_generated"] is False
    assert (directory / "turning-front-chain-draft.json").is_file()

    stale_job = main.load_job(job_id)
    stale_form = next(
        item for setup in stale_job.plan.setups for item in setup.operations
        if item.id == "OP31-FORM"
    )
    stale_form.parameters["profile_z_min_mm"] = 1.5
    main.save_job(directory, stale_job)
    stale = client.post(f"/api/v1/jobs/{job_id}/turning/front-chain/draft", json=request)
    assert stale.status_code == 422
    assert "region does not match" in stale.json()["detail"]


def test_external_groove_requires_review_and_generates_multi_plunge_draft(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "7" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = grooved_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    groove = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "turn_grooving"
    )
    assert groove.id == "OP33-G1"
    assert groove.enabled is True
    assert groove.parameters["groove_width_mm"] == 5
    assert groove.parameters["final_diameter_mm"] == 16
    job = JobResponse(
        id=job_id, status="completed", filename="grooved.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    main.persist_rotational_analysis(directory, job, analysis)

    unconfirmed = client.post(
        f"/api/v1/jobs/{job_id}/turning/grooving-operations/{groove.id}/review",
        json={
            "confirmed_groove_width_mm": 5,
            "confirmed_final_diameter_mm": 16,
            "peck_depth_mm": 0.5,
            "groove_tool_id": "TURN-GROOVE-2",
            "groove_tool_inventory_id": "GROOVE-2-01",
            "profile_form_confirmed": False,
            "reviewer": "test-engineer",
        },
    )
    assert unconfirmed.status_code == 422

    groove_feature_id = next(item for item in groove.feature_ids if item.startswith("TPF-"))
    bound_groove = client.post(
        f"/api/v1/jobs/{job_id}/turning/groove-bindings/confirm",
        json={
            "groove_feature_id": groove_feature_id,
            "requirement_id": "DRAWING-GROOVE-REVIEW",
            "requirement_kind": "external_groove",
            "confirmed_groove_width_mm": 5,
            "confirmed_groove_depth_mm": 2,
            "confirmed_bottom_diameter_mm": 16,
            "reviewer": "test-engineer",
        },
    )
    assert bound_groove.status_code == 200

    reviewed = client.post(
        f"/api/v1/jobs/{job_id}/turning/grooving-operations/{groove.id}/review",
        json={
            "confirmed_groove_width_mm": 5,
            "confirmed_final_diameter_mm": 16,
            "peck_depth_mm": 0.5,
            "groove_tool_id": "TURN-GROOVE-2",
            "groove_tool_inventory_id": "GROOVE-2-01",
            "profile_form_confirmed": True,
            "reviewer": "test-engineer",
        },
    )
    assert reviewed.status_code == 200
    reviewed_operation = next(
        operation for setup in reviewed.json()["plan"]["setups"]
        for operation in setup["operations"] if operation["id"] == groove.id
    )
    assert reviewed_operation["enabled"] is True
    assert reviewed_operation["parameters"]["engineering_review_status"] == "verified_engineer"
    assert (directory / "grooving-review.json").is_file()

    machine = client.post("/api/v1/machines/l32/instances", json={
        "id": "l32-groove-test", "definition_id": "citizen-cincom-l32",
        "name": "L32 groove test", "variant": "VIII",
        "operation_mode": "guide_bushing", "installed_modules": [],
        "bar_diameter_mm": 32,
    })
    assert machine.status_code == 200
    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-groove-test"},
    )
    assert bound.status_code == 200
    profile = next(
        item for item in main.persist_rotational_analysis(directory, main.load_job(job_id), analysis).profiles
        if item.id == "RP-OUTER-1"
    )
    draft = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json={
            "machine_instance_id": "l32-groove-test",
            "operation": reviewed_operation,
            "profile": profile.model_dump(mode="json"),
            "stock_radius_mm": 11,
            "z_min_mm": -27,
            "z_max_mm": 27,
            "resolution_mm": 0.1,
        },
    )
    assert draft.status_code == 200
    commands = draft.json()["toolpath"]["channels"][0]["commands"]
    groove_cuts = [item for item in commands if item["type"] == "feed_move"]
    # Exact groove-envelope mode varies the plunge depth with the local profile.
    # This fixture needs three axial positions with two radial pecks each.
    assert len(groove_cuts) == 6
    assert {item["parameters"]["axial_width_mm"] for item in groove_cuts} == {2.0}
    assert draft.json()["verification"]["metrics"]["maximum_overcut_mm"] == 0


def test_engineer_dimensions_bind_unique_drawing_groove_to_step_candidate(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "5" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = grooved_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    groove = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "turn_grooving"
    )
    groove_feature_id = next(item for item in groove.feature_ids if item.startswith("TPF-"))
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="grooved.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    ))

    rejected = client.post(
        f"/api/v1/jobs/{job_id}/turning/groove-bindings/confirm",
        json={
            "groove_feature_id": groove_feature_id,
            "requirement_id": "DRAWING-GROOVE-1",
            "requirement_kind": "external_groove",
            "confirmed_groove_width_mm": 5,
            "confirmed_groove_depth_mm": 2,
            "confirmed_bottom_diameter_mm": 15,
            "reviewer": "test-engineer",
        },
    )
    assert rejected.status_code == 422

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/groove-bindings/confirm",
        json={
            "groove_feature_id": groove_feature_id,
            "requirement_id": "DRAWING-GROOVE-1",
            "requirement_kind": "external_groove",
            "confirmed_groove_width_mm": 5,
            "confirmed_groove_depth_mm": 2,
            "confirmed_bottom_diameter_mm": 16,
            "raw_text": "槽宽 5，槽底直径 16",
            "reviewer": "test-engineer",
        },
    )

    assert response.status_code == 200
    body = response.json()
    requirement = body["plan"]["manufacturing_requirements"]["requirements"][0]
    assert requirement["mapping_status"] == "matched"
    assert requirement["verification_status"] == "verified_engineer"
    assert requirement["cad_feature_ids"] == [groove_feature_id]
    rebound = next(
        operation for setup in body["plan"]["setups"] for operation in setup["operations"]
        if operation["type"] == "turn_grooving"
    )
    assert rebound["parameters"]["drawing_binding_status"] == "matched"
    assert rebound["parameters"]["drawing_requirement_id"] == "DRAWING-GROOVE-1"
    assert "DRAWING-GROOVE-1" in rebound["feature_ids"]
    assert (directory / "groove-binding.json").is_file()


def test_internal_groove_runs_after_base_bore_and_passes_continuous_chain(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "6" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = internally_grooved_shaft_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    internal_groove = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "turn_grooving"
        and operation.parameters.get("groove_side") == "internal"
    )
    finish = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "turn_id_finishing"
    )
    assert internal_groove.id == "OP32-IG1"
    assert internal_groove.parameters["groove_width_mm"] == 3
    assert internal_groove.parameters["final_diameter_mm"] == 15
    assert internal_groove.parameters["groove_depth_mm"] == 1.5
    assert internal_groove.enabled is False
    assert finish.parameters["sharp_inner_shoulder_count"] == 0
    job = JobResponse(
        id=job_id, status="completed", filename="internal-groove.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    main.persist_rotational_analysis(directory, job, analysis)

    groove_feature_id = next(item for item in internal_groove.feature_ids if item.startswith("TPF-"))
    bound_groove = client.post(
        f"/api/v1/jobs/{job_id}/turning/groove-bindings/confirm",
        json={
            "groove_feature_id": groove_feature_id,
            "requirement_id": "DRAWING-INTERNAL-GROOVE-REVIEW",
            "requirement_kind": "internal_groove",
            "confirmed_groove_width_mm": 3,
            "confirmed_groove_depth_mm": 1.5,
            "confirmed_bottom_diameter_mm": 15,
            "reviewer": "test-engineer",
        },
    )
    assert bound_groove.status_code == 200

    groove_payload = {
        "confirmed_groove_width_mm": 3,
        "confirmed_final_diameter_mm": 15,
        "peck_depth_mm": 0.25,
        "groove_tool_id": "TURN-ID-GROOVE-1",
        "groove_tool_inventory_id": "ID-GROOVE-1-01",
        "confirmed_stickout_mm": 20,
        "assembly_clearance_mm": 0.2,
        "profile_form_confirmed": True,
        "reviewer": "test-engineer",
    }
    premature = client.post(
        f"/api/v1/jobs/{job_id}/turning/grooving-operations/{internal_groove.id}/review",
        json=groove_payload,
    )
    assert premature.status_code == 409
    assert "base-bore finishing" in premature.json()["detail"]

    drill = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "axial_drilling"
    )
    reviewed_drill = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/{drill.id}/review",
        json={
            "drill_tool_id": drill.tool.id,
            "confirmed_stickout_mm": 30,
            "drill_point_angle_deg": 118,
            "peck_depth_mm": 2,
            "bottom_condition": "blind_tip_allowance_confirmed",
            "tip_overtravel_allowance_mm": 4,
            "drill_inventory_id": "DRILL-11-IG-01",
            "reviewer": "test-engineer",
        },
    )
    assert reviewed_drill.status_code == 200
    for operation_id, inventory_id in (("OP25", "BAR-R-IG-01"), ("OP28", "BAR-F-IG-01")):
        reviewed_bore = client.post(
            f"/api/v1/jobs/{job_id}/turning/boring-operations/{operation_id}/review",
            json={
                "initial_bore_diameter_mm": 11,
                "confirmed_stickout_mm": 22,
                "assembly_clearance_mm": 0.2,
                "boring_bar_inventory_id": inventory_id,
                "reviewer": "test-engineer",
            },
        )
        assert reviewed_bore.status_code == 200

    reviewed_groove = client.post(
        f"/api/v1/jobs/{job_id}/turning/grooving-operations/{internal_groove.id}/review",
        json=groove_payload,
    )
    assert reviewed_groove.status_code == 200
    reviewed_operation = next(
        operation for setup in reviewed_groove.json()["plan"]["setups"]
        for operation in setup["operations"] if operation["id"] == internal_groove.id
    )
    assert reviewed_operation["tool"]["id"] == "TURN-ID-GROOVE-1"
    assert reviewed_operation["tool"]["stickout_mm"] == 20

    machine = client.post("/api/v1/machines/l32/instances", json={
        "id": "l32-internal-groove-test", "definition_id": "citizen-cincom-l32",
        "name": "L32 internal groove test", "variant": "VIII",
        "operation_mode": "guide_bushing", "installed_modules": [],
        "bar_diameter_mm": 32,
    })
    assert machine.status_code == 200
    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-internal-groove-test"},
    )
    assert bound.status_code == 200
    chain = client.post(
        f"/api/v1/jobs/{job_id}/turning/inner-bore-chain/draft",
        json={
            "machine_instance_id": "l32-internal-groove-test",
            "profile_id": "RP-INNER-1",
            "stock_radius_mm": 11,
            "z_min_mm": -22,
            "z_max_mm": 2,
            "resolution_mm": 0.1,
        },
    )
    assert chain.status_code == 200
    assert chain.json()["status"] == "passed"
    assert [item["operation_id"] for item in chain.json()["stages"]] == [
        drill.id, "OP25", "OP28", "OP32-IG1",
    ]
    assert chain.json()["final_verification"]["status"] == "passed"


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


def test_agent_profile_review_context_and_explicit_decision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a1" * 16
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = shaft_analysis()
    job = JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at="2026-09-25T00:00:00+00:00",
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=build_process_plan(analysis, "S45C", "Citizen Cincom L32"),
    )
    main.save_job(directory, job)
    rotational = infer_rotational_features(analysis)
    profile_id = rotational.profiles[0].id
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    main.write_json(directory / "agent-l32-incremental-trial.json", {
        "schema_version": "1.0.0",
        "status": "blocked",
        "whole_program_blocker": "轮廓存在标准纵向车刀无法从当前方向到达的倒扣",
        "records": [
            {"operation_id": "OP10", "status": "passed"},
            {"operation_id": "OP55-BACK", "status": "blocked", "reason": "倒扣"},
        ],
        "repair_candidates": [{
            "id": "dedicated_grooving_or_form_tool",
            "kind": "process_change",
            "operation_ids": ["OP55-BACK"],
            "auto_applicable": False,
            "validation_status": "rejected_by_safety_gate",
            "reason": "需要专用刀具",
        }],
        "next_action": "confirm_profile_and_select_special_process",
    })

    context = client.get(f"/api/v1/jobs/{job_id}/agent/l32/review")

    assert context.status_code == 200
    assert context.json()["status"] == "waiting_human"
    assert context.json()["recommended_profile_id"] == profile_id
    assert context.json()["passed_operation_count"] == 1
    assert context.json()["failed_operations"] == ["OP55-BACK"]
    assert context.json()["repair_candidates"][0]["label"] == "改用切槽刀、成形刀或动力刀具"

    decision = client.post(
        f"/api/v1/jobs/{job_id}/agent/l32/profile-decision",
        json={"profile_id": profile_id, "review_state": "accepted", "retry_validation": False},
    )

    assert decision.status_code == 200
    assert decision.json()["decision_status"] == "reviewed"
    persisted = main.load_job(job_id)
    assert persisted.analysis is not None
    assert persisted.analysis.rotational_profile_reviews[profile_id] == "accepted"


def test_ai_can_provisionally_authorize_exact_profile_without_production_release(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a2" * 16
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = regional_shaft_analysis()
    analysis.rotational_profile_reviews["RP-OUTER-1"] = "review"
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="regional.step",
        created_at="2026-09-26T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=None,
    ))
    rotational = infer_rotational_features(analysis)
    profile = next(item for item in rotational.profiles if item.id == "RP-OUTER-1")
    assert profile.extraction_method == "exact_section"
    assert profile.review_state == "review"
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))

    response = client.post(
        f"/api/v1/jobs/{job_id}/agent/l32/profile-provisional-decision",
        json={
            "profile_id": profile.id, "scope": "partial",
            "z_min_mm": -10, "z_max_mm": 0, "confidence": 0.82,
            "rationale": "Exact section is bounded to the front machining region for reversible trials.",
            "evidence_refs": ["rotational-features.json#RP-OUTER-1", "view:isometric"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_status"] == "ai_provisional"
    assert body["status"] == "ready_for_draft"
    assert body["next_action"] == "initialize_process_draft"
    assert body["production_ready"] is False
    persisted = main.load_job(job_id)
    assert persisted.plan is None
    assert persisted.analysis is not None
    assert persisted.analysis.rotational_profile_reviews[profile.id] == "ai_provisional"
    decision = persisted.analysis.rotational_profile_decisions[profile.id]
    assert decision["scope"] == "partial"
    assert decision["production_ready"] is False
    stored = RotationalFeatureAnalysis.model_validate_json(
        (directory / "rotational-features.json").read_text(encoding="utf-8"),
    )
    stored_profile = next(item for item in stored.profiles if item.id == profile.id)
    assert stored_profile.review_state == "ai_provisional"
    assert stored_profile.provisional_decision is not None
    assert (directory / "agent-l32-profile-decisions.json").is_file()


def test_ai_provisional_profile_scope_must_stay_inside_exact_extraction(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a3" * 16
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = regional_shaft_analysis()
    analysis.rotational_profile_reviews["RP-OUTER-1"] = "review"
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="regional.step",
        created_at="2026-09-26T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=None,
    ))
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))

    response = client.post(
        f"/api/v1/jobs/{job_id}/agent/l32/profile-provisional-decision",
        json={
            "profile_id": "RP-OUTER-1", "scope": "partial",
            "z_min_mm": -30, "z_max_mm": 0, "confidence": 0.8,
            "rationale": "This deliberately exceeds the extracted region and must be rejected.",
            "evidence_refs": ["rotational-features.json#RP-OUTER-1"],
        },
    )

    assert response.status_code == 422
    assert "outside the extracted profile" in response.json()["detail"]


def test_rotational_child_feature_review_persists_and_invalidates_cam(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = shaft_analysis()
    job = JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at="2026-09-22T00:00:00+00:00",
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=build_process_plan(analysis, "S45C", "Citizen Cincom L32"),
    )
    main.save_job(directory, job)
    rotational = infer_rotational_features(analysis)
    assert rotational.features
    feature_id = rotational.features[0].id
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    (directory / "toolpath.json").write_text("{}", encoding="utf-8")

    response = client.patch(
        f"/api/v1/jobs/{job_id}/turning/features/{feature_id}",
        json={"review_state": "accepted"},
    )

    assert response.status_code == 200
    reviewed = next(item for item in response.json()["features"] if item["id"] == feature_id)
    assert reviewed["review_state"] == "accepted"
    assert reviewed["confidence"] >= 0.9
    persisted = RotationalFeatureAnalysis.model_validate_json(
        (directory / "rotational-features.json").read_text(encoding="utf-8"),
    )
    assert next(item for item in persisted.features if item.id == feature_id).review_state == "accepted"
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


def test_deep_prebore_requires_deep_hole_strategy_and_confirmed_coolant(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = deep_bored_shaft_analysis()
    analysis.rotational_profile_reviews["RP-INNER-1"] = "accepted"
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="deep-bored.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    ))
    main.persist_rotational_analysis(directory, main.load_job(job_id), analysis)
    prebore = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "axial_drilling"
    )
    assert prebore.parameters["deep_hole_review_required"] is True
    assert prebore.parameters["maximum_bore_length_to_diameter_ratio"] > 5

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/{prebore.id}/review",
        json={
            "drill_tool_id": prebore.tool.id,
            "confirmed_stickout_mm": 100,
            "drill_point_angle_deg": 118,
            "peck_depth_mm": 2,
            "bottom_condition": "through",
            "chip_evacuation_strategy": "standard_peck",
            "through_tool_coolant_confirmed": False,
            "drill_inventory_id": "DEEP-DRILL-01",
            "reviewer": "test-engineer",
        },
    )
    assert response.status_code == 422
    assert "deep-hole peck" in response.json()["detail"]

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/{prebore.id}/review",
        json={
            "drill_tool_id": prebore.tool.id,
            "confirmed_stickout_mm": 100,
            "drill_point_angle_deg": 118,
            "peck_depth_mm": 2,
            "bottom_condition": "through",
            "chip_evacuation_strategy": "deep_hole_peck",
            "through_tool_coolant_confirmed": False,
            "drill_inventory_id": "DEEP-DRILL-01",
            "reviewer": "test-engineer",
        },
    )
    assert response.status_code == 422
    assert "coolant" in response.json()["detail"]


def test_stepped_bore_plans_and_reviews_deepest_drilling_stage_first(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = stepped_bored_shaft_analysis()
    analysis.rotational_profile_reviews["RP-INNER-1"] = "accepted"
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    job = JobResponse(
        id=job_id, status="completed", filename="stepped-bore.step",
        created_at="2026-09-16T00:00:00+00:00", material="S45C",
        machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    main.persist_rotational_analysis(directory, job, analysis)
    stages = [
        operation for setup in plan.setups for operation in setup.operations
        if operation.type == "axial_drilling"
    ]
    assert [item.id for item in stages] == ["OP22-S1", "OP22-S2"]
    assert [item.tool.id for item in stages] == ["DRILL-11.0", "DRILL-12.0"]
    assert [item.parameters["profile_depth_mm"] for item in stages] == [40, 15]
    assert [item.parameters["target_bore_diameter_mm"] for item in stages] == [12, 16]

    payload = {
        "drill_tool_id": "DRILL-12.0",
        "confirmed_stickout_mm": 30,
        "drill_point_angle_deg": 118,
        "peck_depth_mm": 2,
        "bottom_condition": "blind_tip_allowance_confirmed",
        "tip_overtravel_allowance_mm": 4,
        "drill_inventory_id": "DRILL-12-01",
        "reviewer": "test-engineer",
    }
    out_of_order = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/OP22-S2/review",
        json=payload,
    )
    assert out_of_order.status_code == 409

    first = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/OP22-S1/review",
        json={**payload, "drill_tool_id": "DRILL-11.0", "confirmed_stickout_mm": 50,
              "drill_inventory_id": "DRILL-11-01"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/jobs/{job_id}/turning/axial-drilling-operations/OP22-S2/review",
        json=payload,
    )
    assert second.status_code == 200

    finish_operation = next(
        operation for setup in plan.setups for operation in setup.operations
        if operation.id == "OP28"
    )
    assert finish_operation.parameters["sharp_inner_shoulder_count"] > 0
    assert finish_operation.parameters["maximum_finish_nose_radius_mm"] == 0.05

    rejected_finish = client.post(
        f"/api/v1/jobs/{job_id}/turning/boring-operations/OP28/review",
        json={
            "initial_bore_diameter_mm": 11,
            "confirmed_stickout_mm": 45,
            "assembly_clearance_mm": 0.2,
            "boring_bar_inventory_id": "BAR-F-01",
            "reviewer": "test-engineer",
        },
    )
    assert rejected_finish.status_code == 422
    assert "small_nose_tool" in rejected_finish.json()["detail"]

    oversized_nose = client.post(
        f"/api/v1/jobs/{job_id}/turning/boring-operations/OP28/review",
        json={
            "initial_bore_diameter_mm": 11,
            "confirmed_stickout_mm": 45,
            "assembly_clearance_mm": 0.2,
            "finishing_tool_id": "TURN-ID-F",
            "shoulder_strategy": "small_nose_tool",
            "boring_bar_inventory_id": "BAR-F-01",
            "reviewer": "test-engineer",
        },
    )
    assert oversized_nose.status_code == 422
    assert "nose radius <= 0.050 mm" in oversized_nose.json()["detail"]

    for operation_id, inventory_id in (("OP25", "BAR-R-01"), ("OP28", "BAR-MICRO-F-01")):
        finish_fields = {
            "finishing_tool_id": "TURN-ID-MICRO-F",
            "shoulder_strategy": "small_nose_tool",
        } if operation_id == "OP28" else {}
        reviewed_boring = client.post(
            f"/api/v1/jobs/{job_id}/turning/boring-operations/{operation_id}/review",
            json={
                "initial_bore_diameter_mm": 11,
                "confirmed_stickout_mm": 45,
                "assembly_clearance_mm": 0.2,
                "boring_bar_inventory_id": inventory_id,
                "reviewer": "test-engineer",
                **finish_fields,
            },
        )
        assert reviewed_boring.status_code == 200
        if operation_id == "OP28":
            reviewed_finish = next(
                operation for setup in reviewed_boring.json()["plan"]["setups"]
                for operation in setup["operations"] if operation["id"] == "OP28"
            )
            assert reviewed_finish["tool"]["id"] == "TURN-ID-MICRO-F"
            assert reviewed_finish["parameters"]["shoulder_strategy"] == "small_nose_tool"

    machine = client.post("/api/v1/machines/l32/instances", json={
        "id": "l32-stepped-test", "definition_id": "citizen-cincom-l32",
        "name": "L32 stepped test", "variant": "VIII",
        "operation_mode": "guide_bushing", "installed_modules": [],
        "bar_diameter_mm": 32,
    })
    assert machine.status_code == 200
    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-stepped-test"},
    )
    assert bound.status_code == 200
    chain = client.post(
        f"/api/v1/jobs/{job_id}/turning/inner-bore-chain/draft",
        json={
            "machine_instance_id": "l32-stepped-test",
            "profile_id": "RP-INNER-1",
            "stock_radius_mm": 11,
            "z_min_mm": -42,
            "z_max_mm": 2,
            "resolution_mm": 0.2,
        },
    )
    assert chain.status_code == 200
    chain_payload = chain.json()
    assert chain_payload["status"] == "passed"
    checks = {item["id"]: item for item in chain_payload["checks"]}
    assert checks["stage_order"]["status"] == "passed"
    assert checks["material_volume_nonincrease"]["status"] == "passed"
    assert checks["intermediate_overcut"]["status"] == "passed"
    assert checks["final_profile"]["status"] == "passed"
    assert chain_payload["final_verification"]["metrics"]["maximum_overcut_mm"] <= 0.05
    assert [item["operation_id"] for item in chain_payload["stages"]] == [
        "OP22-S1", "OP22-S2", "OP25", "OP28",
    ]
    assert (directory / "turning-inner-bore-chain-draft.json").is_file()


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
