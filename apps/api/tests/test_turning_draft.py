from fastapi.testclient import TestClient

from app import main
from app.catalogs import get_tool
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.models import GeometryAnalysis, JobResponse, Operation
from app.rotational_features import RotationalFeatureAnalysis, RotationalProfile, RotationalProfilePoint
from app.turning_draft import TurningDraftRequest, compile_turning_draft
from app.turning_transfer import (
    TurningTransferDraftRequest, compile_synchronized_transfer_draft, cutoff_kerf_intrusion_mm,
)


client = TestClient(main.app)


def _analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "shaft.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "volume": 1000,
            "surface_area": 600,
            "bounding_box": {
                "minimum": {"x": -10, "y": -10, "z": -30},
                "maximum": {"x": 10, "y": 10, "z": 0},
                "size": {"x": 20, "y": 20, "z": 30},
            },
        },
        "planar_features": [],
        "cylindrical_features": [],
        "visual_edges": [],
    })


def _snapshot(instance_id: str = "l32-draft-01"):
    return snapshot_l32_instance(MachineInstance(
        id=instance_id,
        definition_id="citizen-cincom-l32",
        name="L32 draft machine",
        serial_number="SERIAL-001",
        manufacture_year=2018,
        variant="VIII",
        controller_revision="M70LPC-VU-site-1",
        operation_mode="guide_bushing",
        installed_modules=[],
        bar_diameter_mm=32,
    ))


def _profile(review_state: str = "accepted") -> RotationalProfile:
    return RotationalProfile(
        id="RP-1",
        axis_id="RA-1",
        side="outer",
        extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-30, radius=6),
            RotationalProfilePoint(z=-10, radius=6),
            RotationalProfilePoint(z=0, radius=10),
        ],
        confidence=1,
        review_state=review_state,
    )


def _operation() -> Operation:
    return Operation(
        id="OP10",
        sequence=10,
        type="turn_od_finishing",
        name="OD finish",
        feature_ids=["RP-1"],
        tool=get_tool("TURN-OD-F"),
        parameters={
            "spindle_mode": "constant_surface_speed",
            "cutting_speed_m_min": 100,
            "maximum_spindle_rpm": 8000,
            "feed_per_revolution_mm": 0.08,
            "radial_allowance_mm": 0,
        },
        rationale=["draft test"],
        confidence=1,
        definition_id="turn_od_finishing",
    )


def _cutoff_operation() -> Operation:
    return Operation(
        id="OP40",
        sequence=40,
        type="turn_cutoff",
        name="Cutoff with sub-spindle pickoff",
        feature_ids=["RP-1"],
        tool=get_tool("TURN-CUTOFF-2"),
        parameters={
            "spindle_mode": "constant_surface_speed",
            "cutting_speed_m_min": 65,
            "maximum_spindle_rpm": 6000,
            "feed_per_revolution_mm": 0.05,
            "z_mm": -30,
            "cutting_width_mm": 2,
            "breakthrough_radius_mm": 0.1,
        },
        rationale=["synchronized transfer test"],
        confidence=1,
        definition_id="turn_cutoff",
        channel_id="main",
        spindle_id="main",
        workpiece_side="front",
        synchronization_group="TRANSFER-1",
    )


def _request(**updates) -> TurningDraftRequest:
    values = {
        "machine_instance_id": "l32-draft-01",
        "operation": _operation(),
        "profile": _profile(),
        "stock_radius_mm": 12,
        "z_min_mm": -32,
        "z_max_mm": 2,
        "resolution_mm": 0.5,
    }
    values.update(updates)
    return TurningDraftRequest.model_validate(values)


def _transfer_request(**updates) -> TurningTransferDraftRequest:
    values = {
        **_request(operation=_cutoff_operation()).model_dump(mode="json"),
        "approach_z_mm": -26,
        "pickoff_z_mm": -29,
        "grip_length_mm": 8,
        "synchronization_rpm": 1200,
        "sub_spindle_clamp_confirmed": True,
    }
    values.update(updates)
    return TurningTransferDraftRequest.model_validate(values)


def test_compile_turning_draft_is_controller_neutral_and_never_releases_nc() -> None:
    result = compile_turning_draft("a" * 32, _request(), _snapshot())

    assert result.release_status == "DRAFT"
    assert result.nc_generated is False
    assert result.toolpath.coordinate_convention == "diameter-x_z"
    assert result.toolpath.machine_snapshot_hash == result.machine_configuration_hash
    assert result.simulation.status == "completed"
    assert result.simulation.metrics.removed_volume_mm3 > 0
    assert result.verification is not None
    assert result.verification.metrics.evaluated_sample_count > 0
    assert result.verification.status == "passed"
    assert result.simulation.approximation == "mixed_centerline_and_nose_circle"
    assert result.reachability is not None
    assert result.reachability.status == "passed"
    assert any("未生成 NC" in warning for warning in result.warnings)


def test_compile_turning_draft_limits_toolpath_to_declared_front_region() -> None:
    profile = RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-20, radius=9),
            RotationalProfilePoint(z=-10, radius=5),
            RotationalProfilePoint(z=0, radius=10),
        ],
        confidence=1, review_state="accepted",
    )
    operation = _operation()
    operation.parameters.update({
        "cut_direction": "negative_z",
        "profile_z_min_mm": -10,
        "profile_z_max_mm": 0,
        "profile_region_complete": False,
    })

    result = compile_turning_draft(
        "a" * 32,
        _request(
            operation=operation,
            profile=profile,
            z_min_mm=-22,
            z_max_mm=2,
        ),
        _snapshot(),
    )

    feed_z = [
        command.axes["Z"]
        for command in result.toolpath.channels[0].commands
        if command.type == "feed_move" and "Z" in command.axes
    ]
    assert result.reachability is not None
    assert result.reachability.status == "passed"
    assert feed_z
    assert min(feed_z) >= -10 - operation.tool.nose_radius_mm
    assert max(feed_z) <= operation.tool.nose_radius_mm
    assert result.verification is not None


def test_regional_draft_rejects_oversized_nose_and_detected_overcut() -> None:
    profile = RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=1.15, radius=10.05),
            RotationalProfilePoint(z=1.216667, radius=2),
            RotationalProfilePoint(z=3.05, radius=0.5),
        ],
        confidence=1, review_state="accepted",
    )
    operation = _operation()
    operation.tool = get_tool("TURN-OD-L-MICRO-F")
    operation.tool.nose_radius_mm = 0.8
    operation.parameters.update({
        "cut_direction": "positive_z",
        "profile_z_min_mm": 1.15,
        "profile_z_max_mm": 3.05,
        "profile_region_complete": False,
        "maximum_finish_nose_radius_mm": 0.2,
    })
    request = _request(
        operation=operation, profile=profile,
        z_min_mm=-1, z_max_mm=5, resolution_mm=0.1,
    )

    try:
        compile_turning_draft("a" * 32, request, _snapshot())
    except ValueError as error:
        assert "permitted nose radius" in str(error)
    else:
        raise AssertionError("oversized tool nose must be blocked")

    del operation.parameters["maximum_finish_nose_radius_mm"]
    request = _request(
        operation=operation, profile=profile,
        z_min_mm=-1, z_max_mm=5, resolution_mm=0.1,
    )
    try:
        compile_turning_draft("a" * 32, request, _snapshot())
    except ValueError as error:
        assert "regional turning DRAFT verification failed" in str(error)
    else:
        raise AssertionError("regional overcut must be blocked")


def test_regional_finish_keeps_small_nose_outside_sharp_outward_shoulder() -> None:
    profile = RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-2.1, radius=10.25),
            RotationalProfilePoint(z=-1.9, radius=10.05),
            RotationalProfilePoint(z=-1.833333, radius=2.0),
            RotationalProfilePoint(z=0, radius=0.5),
        ],
        confidence=1, review_state="accepted",
    )
    operation = _operation()
    operation.tool = get_tool("TURN-OD-MICRO-F")
    operation.parameters.update({
        "cut_direction": "negative_z",
        "profile_z_min_mm": -2.1,
        "profile_z_max_mm": 0,
        "profile_region_complete": False,
        "maximum_finish_nose_radius_mm": 0.2,
    })

    result = compile_turning_draft(
        "a" * 32,
        _request(
            operation=operation, profile=profile, stock_radius_mm=11.25,
            z_min_mm=-4, z_max_mm=2, resolution_mm=0.1,
        ),
        _snapshot(),
    )

    assert result.verification is not None
    assert result.verification.metrics.maximum_overcut_mm <= 0.05


def test_cutoff_draft_does_not_claim_whole_profile_verification() -> None:
    result = compile_turning_draft(
        "a" * 32,
        _request(operation=_cutoff_operation()),
        _snapshot(),
    )

    assert result.release_status == "DRAFT"
    assert result.nc_generated is False
    assert result.verification is None
    assert result.reachability is not None


def test_compile_turning_draft_rejects_unaccepted_profile_and_oversize_stock() -> None:
    try:
        compile_turning_draft("a" * 32, _request(profile=_profile("review")), _snapshot())
    except ValueError as error:
        assert "must be accepted" in str(error)
    else:
        raise AssertionError("unaccepted profile should be rejected")

    try:
        compile_turning_draft("a" * 32, _request(stock_radius_mm=17), _snapshot())
    except ValueError as error:
        assert "stock diameter exceeds" in str(error)
    else:
        raise AssertionError("oversize stock should be rejected")


def test_compile_turning_draft_rejects_tampered_machine_snapshot() -> None:
    tampered = _snapshot().model_copy(update={"configuration_hash": "0" * 64})

    try:
        compile_turning_draft("a" * 32, _request(), tampered)
    except ValueError as error:
        assert "integrity check failed" in str(error)
    else:
        raise AssertionError("tampered machine snapshot should be rejected")


def test_compile_synchronized_transfer_pairs_barriers_across_two_channels() -> None:
    result = compile_synchronized_transfer_draft(
        "a" * 32, _transfer_request(), _snapshot(),
    )

    assert result.release_status == "DRAFT"
    assert result.nc_generated is False
    assert any("切断刀缝侵入已确认成品轮廓 1.000 mm" in item for item in result.warnings)
    assert [channel.id for channel in result.toolpath.channels] == ["main", "sub"]
    barrier_sets = [
        {str(command.parameters["barrier_id"]) for command in channel.commands if command.type == "sync_barrier"}
        for channel in result.toolpath.channels
    ]
    assert barrier_sets[0] == barrier_sets[1] == {
        "HANDOFF_CLAMPED", "SPINDLES_SYNCHRONIZED", "PART_SEPARATED",
    }
    assert any(command.type == "pickoff" for command in result.toolpath.channels[1].commands)
    assert [item.state for item in result.state_transitions] == [
        "main_spindle_held", "dual_spindle_clamped", "phase_synchronized",
        "part_separated", "sub_spindle_held",
    ]


def test_cutoff_kerf_intrusion_requires_sacrificial_stock_outside_profile() -> None:
    operation = _cutoff_operation()
    assert cutoff_kerf_intrusion_mm(operation, _profile()) == 1
    operation.parameters["z_mm"] = -31.1
    assert cutoff_kerf_intrusion_mm(operation, _profile()) == 0


def test_turning_draft_api_persists_reviewable_artifacts_without_nc(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "b" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    snapshot = _snapshot()
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at=main.utc_now(),
        material="SUS304",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        analysis=_analysis(),
    ))
    main.write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))
    rotational = RotationalFeatureAnalysis(
        source_file="shaft.step",
        axes=[],
        profiles=[_profile()],
        status="candidate",
    )
    main.write_json(directory / "rotational-features.json", rotational.model_dump(mode="json"))

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json=_request().model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["nc_generated"] is False
    assert (directory / "turning-toolpath-ir.json").is_file()
    assert (directory / "turning-simulation.json").is_file()
    assert (directory / "turning-verification.json").is_file()
    assert (directory / "turning-reachability.json").is_file()
    assert (directory / "turning-draft.json").is_file()
    assert len(list(directory.glob("turning-draft-cache-*.json"))) == 1
    assert not (directory / "program.nc").exists()
    assert client.get(f"/api/v1/jobs/{job_id}/files/turning-draft.json").status_code == 200

    def fail_if_regenerated(*_args, **_kwargs):
        raise AssertionError("a successful operation draft must be reused")

    monkeypatch.setattr(main, "compile_turning_draft", fail_if_regenerated)
    cached_response = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json=_request().model_dump(mode="json"),
    )
    assert cached_response.status_code == 200
    assert cached_response.json() == response.json()


def test_turning_draft_api_requires_the_job_bound_machine_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "f" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at=main.utc_now(),
        material="SUS304",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        analysis=_analysis(),
    ))

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json=_request().model_dump(mode="json"),
    )

    assert response.status_code == 409
    assert "Bind a validated L32 machine instance" in response.json()["detail"]


def test_synchronized_transfer_api_persists_controller_neutral_ir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "1" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    snapshot = _snapshot()
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at=main.utc_now(),
        material="SUS304",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        analysis=_analysis(),
    ))
    main.write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))
    main.write_json(directory / "rotational-features.json", RotationalFeatureAnalysis(
        source_file="shaft.step", axes=[], profiles=[_profile()], status="candidate",
    ).model_dump(mode="json"))

    response = client.post(
        f"/api/v1/jobs/{job_id}/turning/transfer/draft",
        json=_transfer_request().model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["nc_generated"] is False
    assert len(response.json()["toolpath"]["channels"]) == 2
    assert (directory / "turning-transfer-ir.json").is_file()
    assert (directory / "turning-transfer-draft.json").is_file()
    assert client.get(f"/api/v1/jobs/{job_id}/files/turning-transfer-draft.json").status_code == 200


def test_rotational_profile_review_is_persisted_and_required_by_draft_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "c" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    snapshot = _snapshot()
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="shaft.step", created_at=main.utc_now(),
        material="SUS304", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        analysis=_analysis(),
    ))
    main.write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))
    pending = RotationalFeatureAnalysis(
        source_file="shaft.step", axes=[], profiles=[_profile("review")], status="candidate",
    )
    main.write_json(directory / "rotational-features.json", pending.model_dump(mode="json"))

    blocked = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json=_request().model_dump(mode="json"),
    )
    accepted = client.patch(
        f"/api/v1/jobs/{job_id}/turning/profiles/RP-1",
        json={"review_state": "accepted"},
    )
    generated = client.post(
        f"/api/v1/jobs/{job_id}/turning/draft",
        json=_request().model_dump(mode="json"),
    )

    assert blocked.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["profiles"][0]["review_state"] == "accepted"
    assert generated.status_code == 200
