from fastapi.testclient import TestClient

from app import main
from app.catalogs import get_tool
from app.l32_backside import BacksideDraftRequest, compile_backside_draft, derive_backside_profile
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.models import GeometryAnalysis, JobResponse, Operation
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


def test_binding_back_tooling_enables_op50_op60_and_api_persists_draft(tmp_path, monkeypatch) -> None:
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
    assert [channel["id"] for channel in payload["toolpath"]["channels"]] == ["main", "sub"]
    assert [stage["operation_id"] for stage in payload["stages"]] == [
        "OP10", "OP20", "OP30", "OP40", "OP50", "OP60",
    ]
    assert len(payload["program_hash"]) == 64
    assert payload["timeline"]["status"] == "scheduled"
    assert payload["timeline"]["barrier_order"] == [
        "HANDOFF_CLAMPED", "SPINDLES_SYNCHRONIZED", "PART_SEPARATED",
    ]
    assert payload["timeline"]["estimated_cycle_seconds"] > 0
    assert payload["continuous_simulation"]["status"] == "passed"
    assert all(
        check["status"] == "passed"
        for check in payload["continuous_simulation"]["checks"]
    )
    assert (directory / "turning-whole-program-ir.json").is_file()
    assert (directory / "turning-whole-program-draft.json").is_file()
    assert (directory / "turning-whole-program-timeline.json").is_file()
    assert (directory / "turning-continuous-simulation.json").is_file()
