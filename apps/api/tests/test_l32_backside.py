import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.catalogs import get_tool
from app.l32_backside import BacksideDraftRequest, compile_backside_draft, derive_backside_profile
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.models import GeometryAnalysis, JobResponse, Operation, PrismaticFeature
from app.planner import build_process_plan
from app.rotational_features import RotationalProfile, RotationalProfilePoint, infer_rotational_features


client = TestClient(main.app)


def _profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-OUTER-1",
        axis_id="RA-1",
        side="outer",
        extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-30, radius=6),
            RotationalProfilePoint(z=-10, radius=6),
            RotationalProfilePoint(z=0, radius=10),
        ],
        confidence=1,
        review_state="accepted",
    )


def _instance(instance_id: str = "l32-back-01", *, with_back_tooling: bool = True):
    return snapshot_l32_instance(MachineInstance(
        id=instance_id,
        definition_id="citizen-cincom-l32",
        name="L32 backside test",
        serial_number="SERIAL-BACK-01",
        variant="VIII",
        controller_revision="M70LPC-VU-site-1",
        operation_mode="guide_bushing",
        installed_modules=["U150B"] if with_back_tooling else [],
        bar_diameter_mm=32,
    ))


def _back_operation(operation_type: str = "turn_facing") -> Operation:
    parameters = {
        "spindle_mode": "constant_surface_speed",
        "cutting_speed_m_min": 80,
        "maximum_spindle_rpm": 6000,
        "feed_per_revolution_mm": 0.06,
        "stock_allowance_mm": 0,
        "depth_of_cut_mm": 0.25,
        "face_z_mm": 0,
        "center_overtravel_mm": 0.15,
    }
    if operation_type == "turn_od_finishing":
        parameters = {
            "spindle_mode": "constant_surface_speed",
            "cutting_speed_m_min": 80,
            "maximum_spindle_rpm": 6000,
            "feed_per_revolution_mm": 0.06,
            "radial_allowance_mm": 0,
            "axial_allowance_mm": 0,
            "back_cleanup_length_mm": 1,
        }
    return Operation(
        id="OP50" if operation_type == "turn_facing" else "OP60",
        sequence=50 if operation_type == "turn_facing" else 60,
        type=operation_type,
        name="Back facing" if operation_type == "turn_facing" else "Back OD cleanup",
        feature_ids=["RP-OUTER-1-BACK"],
        tool=get_tool("TURN-OD-F"),
        parameters=parameters,
        rationale=["backside test"],
        confidence=1,
        definition_id=operation_type,
        channel_id="sub",
        spindle_id="sub",
        workpiece_side="back",
        synchronization_group="TRANSFER-1",
    )


def _request(operation_type: str = "turn_facing") -> BacksideDraftRequest:
    return BacksideDraftRequest(
        machine_instance_id="l32-back-01",
        source_profile_id="RP-OUTER-1",
        operation=_back_operation(operation_type),
        source_cutoff_z_mm=-30,
        stock_radius_mm=11,
        resolution_mm=0.5,
    )


def _analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "shaft.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "volume": 10_000,
            "surface_area": 3_000,
            "bounding_box": {
                "minimum": {"x": -10, "y": -10, "z": -25},
                "maximum": {"x": 10, "y": 10, "z": 25},
                "size": {"x": 20, "y": 20, "z": 50},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "CF-SHAFT", "kind": "cylinder", "radius": 10, "diameter": 20,
            "length": 50, "center": {"x": 0, "y": 0, "z": 0},
            "axis": {"x": 0, "y": 0, "z": 1}, "confidence": 0.95,
        }],
        "visual_edges": [],
    })


def _regional_analysis() -> GeometryAnalysis:
    source = _analysis()
    source.cylindrical_features[0].source_face_ids = ["CF-REGIONAL"]
    source = GeometryAnalysis.model_validate({
        **source.model_dump(),
        "rotational_sections": [{
            "source_feature_id": "CF-REGIONAL",
            "axis_origin": {"x": 0, "y": 0, "z": 0},
            "axis": {"x": 0, "y": 0, "z": 1},
            "plane_normal": {"x": 0, "y": 1, "z": 0},
            "outer_profile": [
                {"z": z_value, "radius": radius}
                for z_value, radius in [
                    (-20, 9), (-19.5, 9), (-19.5, 5), (-17.5, 5),
                    (-17.5, 9), (-16, 9), (-10, 5), (0, 10),
                ]
            ],
            "tolerance_mm": 0.005,
        }],
    })
    source.rotational_profile_reviews["RP-OUTER-1"] = "accepted"
    return source


def test_backside_coordinate_transform_reverses_z_about_cutoff_datum() -> None:
    transform, profile = derive_backside_profile(_profile(), -30)
    _, cleanup = derive_backside_profile(_profile(), -30, cleanup_length_mm=1)

    assert transform.z_scale == -1
    assert transform.source_cutoff_z_mm == -30
    assert [point.z for point in profile.points] == [-30, -20, 0]
    assert [point.z for point in cleanup.points] == [-1, 0]


def test_backside_draft_uses_sub_channel_and_sub_spindle() -> None:
    result = compile_backside_draft(
        "a" * 32, _request(), _profile(), _instance(),
    )

    assert result.nc_generated is False
    assert result.draft.toolpath.channels[0].id == "sub"
    assert all(
        command.channel_id == "sub"
        for command in result.draft.toolpath.channels[0].commands
    )
    spindle_commands = [
        command for command in result.draft.toolpath.channels[0].commands
        if command.type in {"spindle_start", "spindle_stop"}
    ]
    assert all(command.parameters["spindle_id"] == "sub" for command in spindle_commands)


def test_backside_draft_requires_installed_back_turning_module() -> None:
    try:
        compile_backside_draft(
            "a" * 32, _request(), _profile(), _instance(with_back_tooling=False),
        )
    except ValueError as error:
        assert "back_turning" in str(error)
    else:
        raise AssertionError("backside operation should require back-turning tooling")


def test_rear_shoulder_drafts_bridge_groove_before_coordinate_transform() -> None:
    analysis = _regional_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    profile = next(
        item for item in infer_rotational_features(analysis).profiles
        if item.side == "outer"
    )
    rear_operations = {
        operation.id: operation
        for setup in plan.setups for operation in setup.operations
        if operation.id in {"OP55-BACK", "OP58-BACK"}
    }

    assert set(rear_operations) == {"OP55-BACK", "OP58-BACK"}
    assert not any(
        item.id == "OP60" for setup in plan.setups for item in setup.operations
    )
    assert all(not item.enabled for item in rear_operations.values())
    assert plan.stock["unresolved_turning_regions"][0]["status"] == "uncovered"
    for operation_id, expected_status in (
        ("OP55-BACK", "warning"), ("OP58-BACK", "passed"),
    ):
        result = compile_backside_draft(
            "a" * 32,
            BacksideDraftRequest(
                machine_instance_id="l32-back-01",
                source_profile_id=profile.id,
                operation=rear_operations[operation_id].model_copy(update={"enabled": True}),
                source_cutoff_z_mm=-20,
                stock_radius_mm=11,
                resolution_mm=0.1,
            ),
            profile,
            _instance(),
        )
        assert result.draft.reachability is not None
        assert result.draft.reachability.status == "passed"
        assert result.draft.verification is not None
        assert result.draft.verification.status == expected_status
        assert result.draft.verification.metrics.maximum_overcut_mm <= 0.05
        assert result.nc_generated is False


def test_default_l32_instance_enables_supported_backside_operations(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = _regional_analysis()
    job = JobResponse(
        id=job_id,
        status="completed",
        filename="regional.step",
        created_at=main.utc_now(),
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=build_process_plan(analysis, "S45C", "Citizen Cincom L32"),
    )

    main.provision_default_l32_planning_instance(job, directory)

    back_operations = [
        operation
        for setup in job.plan.setups
        for operation in setup.operations
        if operation.workpiece_side == "back"
    ]
    assert [operation.id for operation in back_operations] == [
        "OP50", "OP55-BACK", "OP58-BACK",
    ]
    assert all(operation.enabled for operation in back_operations)


def test_material_snapshots_support_fully_rotational_plan_and_skip_backside_stages(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = _regional_analysis()
    rotational = infer_rotational_features(analysis)
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    plan.stock.pop("nonrotational_region_z_mm", None)
    job = JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at=main.utc_now(),
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=analysis,
        plan=plan,
    )
    (directory / job.filename).write_text("dummy STEP", encoding="utf-8")
    main.write_json(
        directory / "rotational-features.json",
        rotational.model_dump(mode="json"),
    )
    main.save_job(directory, job)

    def fake_freecad(_executable, _script, arguments, **_kwargs):
        output = Path(arguments[1])
        stages = json.loads(arguments[2].read_text(encoding="utf-8"))["stages"]
        operations = []
        for stage in stages:
            name = f"l32-material-{stage['operation_id']}-00.stl"
            (output / name).write_bytes(b"solid snapshot\n" + b"x" * 100)
            operations.append({
                "operation_id": stage["operation_id"],
                "files": [name],
                "volumes_mm3": [100.0],
            })
        return SimpleNamespace(
            stdout="CNC_L32_MATERIAL " + json.dumps({"operations": operations}),
        )

    monkeypatch.setattr(main, "run_freecad_adapter", fake_freecad)

    response = client.get(f"/api/v1/jobs/{job_id}/l32/material-snapshots")

    assert response.status_code == 200, response.text
    operation_ids = [item["operation_id"] for item in response.json()["operations"]]
    assert "OP20" in operation_ids
    assert not {"OP50", "OP55-BACK", "OP58-BACK"} & set(operation_ids)


def test_rear_shoulder_api_requires_module_and_exact_cutoff_datum(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "8" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = _regional_analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    assert not any(item.id == "OP60" for setup in plan.setups for item in setup.operations)
    # An older persisted plan may still contain the now-redundant cleanup operation.
    plan.setups[-1].operations.append(_back_operation("turn_od_finishing"))
    job = JobResponse(
        id=job_id, status="completed", filename="regional.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    )
    main.save_job(directory, job)
    rotational = infer_rotational_features(analysis)
    for axis in rotational.axes:
        axis.review_state = "accepted"
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))

    for snapshot in (_instance("l32-no-back", with_back_tooling=False), _instance("l32-has-back")):
        path = tmp_path / ".machine-instances" / f"{snapshot.instance.id}.json"
        path.parent.mkdir(exist_ok=True)
        main.write_json(path, snapshot.model_dump(mode="json"))

    no_module = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-no-back"},
    )
    assert no_module.status_code == 200
    blocked_operation = next(
        item for setup in no_module.json()["plan"]["setups"]
        for item in setup["operations"] if item["id"] == "OP55-BACK"
    )
    assert blocked_operation["enabled"] is False
    blocked_draft = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside/draft",
        json={
            "machine_instance_id": "l32-no-back",
            "source_profile_id": "RP-OUTER-1",
            "operation": blocked_operation,
            "source_cutoff_z_mm": -20,
            "stock_radius_mm": 11,
        },
    )
    assert blocked_draft.status_code == 409
    blocked_whole = client.post(
        f"/api/v1/jobs/{job_id}/turning/whole-program/draft",
        json={
            "machine_instance_id": "l32-no-back",
            "source_profile_id": "RP-OUTER-1",
            "stock_radius_mm": 11,
            "approach_z_mm": -16,
            "pickoff_z_mm": -19,
            "grip_length_mm": 8,
            "synchronization_rpm": 1200,
            "sub_spindle_clamp_confirmed": True,
        },
    )
    assert blocked_whole.status_code == 422
    assert "not covered" in blocked_whole.json()["detail"]
    blocked_chain = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside-chain/draft",
        json={
            "machine_instance_id": "l32-no-back",
            "source_profile_id": "RP-OUTER-1",
            "stock_radius_mm": 11,
            "resolution_mm": 0.1,
        },
    )
    assert blocked_chain.status_code == 422
    assert "disabled" in blocked_chain.json()["detail"]

    with_module = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-has-back"},
    )
    assert with_module.status_code == 200
    operation = next(
        item for setup in with_module.json()["plan"]["setups"]
        for item in setup["operations"] if item["id"] == "OP58-BACK"
    )
    assert operation["enabled"] is True
    legacy_cleanup = next(
        item for setup in with_module.json()["plan"]["setups"]
        for item in setup["operations"] if item["id"] == "OP60"
    )
    assert legacy_cleanup["enabled"] is False
    rejected_cleanup = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside/draft",
        json={
            "machine_instance_id": "l32-has-back",
            "source_profile_id": "RP-OUTER-1",
            "operation": legacy_cleanup,
            "source_cutoff_z_mm": -20,
            "stock_radius_mm": 11,
        },
    )
    assert rejected_cleanup.status_code == 409
    request = {
        "machine_instance_id": "l32-has-back",
        "source_profile_id": "RP-OUTER-1",
        "operation": operation,
        "source_cutoff_z_mm": -20,
        "stock_radius_mm": 11,
        "resolution_mm": 0.1,
    }
    accepted = client.post(f"/api/v1/jobs/{job_id}/turning/backside/draft", json=request)
    wrong_datum = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside/draft",
        json={**request, "source_cutoff_z_mm": -19},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["draft"]["verification"]["status"] == "passed"
    assert wrong_datum.status_code == 422
    assert "cutoff datum" in wrong_datum.json()["detail"]

    chain = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside-chain/draft",
        json={
            "machine_instance_id": "l32-has-back",
            "source_profile_id": "RP-OUTER-1",
            "stock_radius_mm": 11,
            "resolution_mm": 0.1,
        },
    )
    assert chain.status_code == 200, chain.text
    chain_result = chain.json()
    assert chain_result["status"] == "passed"
    assert chain_result["release_status"] == "DRAFT"
    assert chain_result["nc_generated"] is False
    assert [stage["operation_id"] for stage in chain_result["stages"]] == [
        "OP55-BACK", "OP58-BACK",
    ]
    assert chain_result["stages"][1]["initial_volume_mm3"] == chain_result["stages"][0]["final_volume_mm3"]
    assert chain_result["final_verification"]["status"] == "passed"
    assert chain_result["toolpath"]["channels"][0]["id"] == "sub"
    assert (directory / "turning-backside-chain-draft.json").is_file()

    whole = client.post(
        f"/api/v1/jobs/{job_id}/turning/whole-program/draft",
        json={
            "machine_instance_id": "l32-has-back",
            "source_profile_id": "RP-OUTER-1",
            "stock_radius_mm": 11,
            "approach_z_mm": -16,
            "pickoff_z_mm": -19,
            "grip_length_mm": 8,
            "synchronization_rpm": 1200,
            "sub_spindle_clamp_confirmed": True,
        },
    )
    # Regional drafts may be examined separately, but a partial rotational
    # profile cannot be reported as a complete whole-part program.
    assert whole.status_code == 422
    assert "not covered" in whole.json()["detail"]

    stale_job = main.load_job(job_id)
    stale_finish = next(
        item for setup in stale_job.plan.setups for item in setup.operations
        if item.id == "OP58-BACK"
    )
    stale_finish.parameters["source_region_z_max_mm"] = -15
    main.save_job(directory, stale_job)
    stale_chain = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside-chain/draft",
        json={
            "machine_instance_id": "l32-has-back",
            "source_profile_id": "RP-OUTER-1",
            "stock_radius_mm": 11,
        },
    )
    assert stale_chain.status_code == 422
    assert "region does not match" in stale_chain.json()["detail"]


def test_binding_back_tooling_uses_sacrificial_cutoff_and_persists_whole_draft(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "2" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = _analysis()
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="shaft.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    ))
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    accepted = client.patch(
        f"/api/v1/jobs/{job_id}/turning/profiles/{rotational.profiles[0].id}",
        json={"review_state": "accepted"},
    )
    assert accepted.status_code == 200
    rotational.profiles[0].review_state = "accepted"
    snapshot = _instance(instance_id="l32-back-api")
    global_path = tmp_path / ".machine-instances" / "l32-back-api.json"
    global_path.parent.mkdir()
    main.write_json(global_path, snapshot.model_dump(mode="json"))

    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-back-api"},
    )
    assert bound.status_code == 200
    back_operations = [
        operation for setup in bound.json()["plan"]["setups"] for operation in setup["operations"]
        if operation["workpiece_side"] == "back"
    ]
    assert [operation["id"] for operation in back_operations] == ["OP50", "OP60"]
    assert all(operation["enabled"] for operation in back_operations)

    operation = back_operations[0]
    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/backside/draft",
        json={
            "machine_instance_id": "l32-back-api",
            "source_profile_id": rotational.profiles[0].id,
            "operation": operation,
            "source_cutoff_z_mm": -25,
            "stock_radius_mm": 11,
            "resolution_mm": 0.5,
        },
    )

    assert response.status_code == 200
    assert response.json()["draft"]["toolpath"]["channels"][0]["id"] == "sub"
    assert (directory / "turning-backside-ir.json").is_file()
    assert (directory / "turning-backside-draft.json").is_file()

    whole = client.post(
        f"/api/v1/jobs/{job_id}/turning/whole-program/draft",
        json={
            "machine_instance_id": "l32-back-api",
            "source_profile_id": rotational.profiles[0].id,
            "stock_radius_mm": 11,
            "initial_bore_radius_mm": 0,
            "resolution_mm": 0.5,
            "approach_z_mm": -21,
            "pickoff_z_mm": -24,
            "grip_length_mm": 8,
            "synchronization_rpm": 1200,
            "sub_spindle_clamp_confirmed": True,
        },
    )

    assert whole.status_code == 200, whole.text
    payload = whole.json()
    assert payload["nc_generated"] is False
    assert [stage["operation_id"] for stage in payload["stages"]] == [
        "OP10", "OP20", "OP30", "OP40", "OP50", "OP60",
    ]
    assert payload["coordinate_frames"][1]["source_cutoff_z_mm"] == -25
    assert payload["continuous_simulation"]["status"] == "passed"
    snapshots = payload["continuous_simulation"]["stage_snapshots"]
    assert [item["operation_id"] for item in snapshots] == [
        "OP10", "OP20", "OP30", "OP40", "OP50", "OP60",
    ]
    assert snapshots[0]["channel_id"] == "main"
    assert snapshots[-1]["channel_id"] == "sub"
    assert snapshots[0]["before_samples"] != snapshots[0]["after_samples"]
    assert snapshots[-1]["metrics"]["remaining_volume_mm3"] <= snapshots[-1]["metrics"]["initial_volume_mm3"]
    assert (directory / "turning-whole-program-draft.json").is_file()

    # A later enabled operation must not silently disappear from a regenerated
    # whole-part artifact, even when it has no new coverage target.
    omitted_job = main.load_job(job_id)
    front_setup = omitted_job.plan.setups[0]
    extra = next(item for item in front_setup.operations if item.id == "OP20")
    front_setup.operations.append(extra.model_copy(deep=True, update={
        "id": "OP99", "sequence": 99, "feature_ids": [],
    }))
    main.save_job(directory, omitted_job)
    omitted = client.post(
        f"/api/v1/jobs/{job_id}/turning/whole-program/draft",
        json={
            "machine_instance_id": "l32-back-api",
            "source_profile_id": rotational.profiles[0].id,
            "stock_radius_mm": 11,
            "initial_bore_radius_mm": 0,
            "resolution_mm": 0.5,
            "approach_z_mm": -21,
            "pickoff_z_mm": -24,
            "grip_length_mm": 8,
            "synchronization_rpm": 1200,
            "sub_spindle_clamp_confirmed": True,
        },
    )
    assert omitted.status_code == 422, omitted.text
    assert "omits enabled operations: OP99" in omitted.json()["detail"]


def test_whole_program_rejects_uncovered_nonrotational_feature_without_writing_artifact(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = _analysis()
    analysis.prismatic_features.append(PrismaticFeature.model_validate({
        "id": "MF-1", "kind": "pocket", "source_face_id": "PF-14",
        "center": {"x": 0, "y": 0, "z": 0},
        "bounds": {
            "minimum": {"x": -1, "y": -1, "z": 0},
            "maximum": {"x": 1, "y": 1, "z": 0},
            "size": {"x": 2, "y": 2, "z": 0},
        },
        "access_direction": {"x": 0, "y": 0, "z": 1},
        "length": 2, "width": 2, "depth": 1,
        "review_state": "accepted",
    }))
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="mixed.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        analysis=analysis, plan=plan,
    ))
    rotational = infer_rotational_features(analysis)
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))
    profile_id = rotational.profiles[0].id
    accepted = client.patch(
        f"/api/v1/jobs/{job_id}/turning/profiles/{profile_id}",
        json={"review_state": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["axes"][0]["review_state"] == "accepted"
    snapshot = _instance(instance_id="l32-mixed-feature")
    instance_path = tmp_path / ".machine-instances" / "l32-mixed-feature.json"
    instance_path.parent.mkdir()
    main.write_json(instance_path, snapshot.model_dump(mode="json"))
    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": snapshot.instance.id},
    )
    assert bound.status_code == 200, bound.text
    result = client.post(
        f"/api/v1/jobs/{job_id}/turning/whole-program/draft",
        json={
            "machine_instance_id": snapshot.instance.id,
            "source_profile_id": profile_id,
            "stock_radius_mm": 11,
            "approach_z_mm": 27,
            "pickoff_z_mm": -24,
            "grip_length_mm": 8,
            "synchronization_rpm": 1200,
            "sub_spindle_clamp_confirmed": True,
        },
    )
    assert result.status_code == 422, result.text
    assert "manufacturing targets are not covered" in result.json()["detail"]
    assert "pocket" in result.json()["detail"]
    assert not (directory / "turning-whole-program-draft.json").exists()
