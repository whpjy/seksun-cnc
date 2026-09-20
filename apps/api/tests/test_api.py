import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.catalogs import get_tool
from app.main import app
from app.models import GeometryAnalysis, JobResponse, Operation, ProcessPlan, Setup, Vec3


client = TestClient(app)


def _turning_operation(**updates) -> Operation:
    values = {
        "id": "OP10", "sequence": 10, "type": "turn_facing", "name": "端面",
        "feature_ids": ["RP-1"], "tool": get_tool("TURN-OD-R"),
        "parameters": {
            "spindle_mode": "constant_surface_speed", "cutting_speed_m_min": 100,
            "maximum_spindle_rpm": 8000, "feed_per_revolution_mm": 0.12,
            "face_z_mm": 0, "center_overtravel_mm": 0.2,
        },
        "rationale": ["test"], "confidence": 1,
    }
    values.update(updates)
    return Operation.model_validate(values)


def test_turning_sweep_segments_reuse_deterministic_toolpath_and_world_offset() -> None:
    segments, errors = main.build_turning_sweep_segments(
        _turning_operation(), None, stock_radius=12, axial_offset=30,
    )

    assert errors == []
    assert len(segments) == 2
    facing = segments[-1]
    assert facing["start_z"] == 30
    assert facing["end_z"] == 30
    assert facing["axial_width"] == 1.6
    assert facing["axial_center_offset"] == 0.8
    assert facing["radial_stock_envelope"] is False


def test_turning_sweep_segments_require_explicit_backside_cutoff_datum() -> None:
    segments, errors = main.build_turning_sweep_segments(
        _turning_operation(workpiece_side="back", spindle_id="sub", channel_id="sub"),
        None, stock_radius=12, axial_offset=0,
    )

    assert segments == []
    assert "切断坐标基准" in errors[0]


def test_turning_sweep_segments_transform_sub_spindle_z_to_main_world() -> None:
    operation = _turning_operation(
        workpiece_side="back", spindle_id="sub", channel_id="sub",
    )
    segments, errors = main.build_turning_sweep_segments(
        operation, None, stock_radius=12, axial_offset=100, backside_cutoff_z=-25,
    )

    assert errors == []
    facing = segments[-1]
    assert facing["start_z"] == 75
    assert facing["end_z"] == 75
    assert facing["axial_center_offset"] == -0.8


def test_part_state_chain_links_each_operation_to_previous_output(tmp_path) -> None:
    for name, content in (
        ("op10-before.stl", b"initial"),
        ("op10-after.stl", b"after-op10"),
        ("op20-before.stl", b"equivalent-after-op10"),
        ("op20-after.stl", b"after-op20"),
    ):
        (tmp_path / name).write_bytes(content)
    chain = main.build_part_state_chain("a" * 32, tmp_path, [
        {
            "operation_id": "OP10",
            "files": ["op10-before.stl", "op10-after.stl"],
            "input_state_volume_mm3": 100.0,
            "output_state_volume_mm3": 90.0,
            "removed_volume_mm3": 10.0,
            "previous_state_shape_delta_mm3": 0.0,
        },
        {
            "operation_id": "OP20",
            "files": ["op20-before.stl", "op20-after.stl"],
            "input_state_volume_mm3": 90.0,
            "output_state_volume_mm3": 75.0,
            "removed_volume_mm3": 15.0,
            "previous_state_shape_delta_mm3": 0.0,
        },
    ])

    assert chain.status == "continuous"
    assert chain.validation_level == "geometric_draft"
    assert [item.id for item in chain.states] == ["IPW-000", "IPW-001", "IPW-002"]
    assert chain.transitions[0].input_state_id == "IPW-000"
    assert chain.transitions[0].output_state_id == "IPW-001"
    assert chain.transitions[1].input_state_id == "IPW-001"
    assert chain.transitions[1].output_state_id == "IPW-002"
    assert chain.transitions[1].removed_volume_mm3 == 15.0
    assert chain.transitions[1].transition_verified is True


def test_part_state_chain_marks_shape_discontinuity_failed(tmp_path) -> None:
    (tmp_path / "before.stl").write_bytes(b"before")
    (tmp_path / "after.stl").write_bytes(b"after")

    chain = main.build_part_state_chain("b" * 32, tmp_path, [{
        "operation_id": "OP10",
        "files": ["before.stl", "after.stl"],
        "input_state_volume_mm3": 100.0,
        "output_state_volume_mm3": 99.0,
        "removed_volume_mm3": 1.0,
        "previous_state_shape_delta_mm3": 0.01,
    }])

    assert chain.status == "failed"
    assert chain.transitions[0].continuity_verified is False
    assert "相邻工序的中间毛坯实体不连续" in chain.transitions[0].blocking_reasons


def test_part_state_chain_blocks_zero_removal_and_preserves_cutter_validation(tmp_path) -> None:
    (tmp_path / "before.stl").write_bytes(b"before")
    (tmp_path / "after.stl").write_bytes(b"after")

    chain = main.build_part_state_chain("c" * 32, tmp_path, [{
        "operation_id": "OP70-H1",
        "files": ["before.stl", "after.stl"],
        "input_state_volume_mm3": 100.0,
        "output_state_volume_mm3": 100.0,
        "removed_volume_mm3": 0.0,
        "previous_state_shape_delta_mm3": 0.0,
        "validation_level": "cutter_envelope_verified",
        "cutter_sweep_removed_volume_mm3": 0.0,
        "unreachable_removal_volume_mm3": 0.0,
        "overcut_volume_mm3": 0.0,
        "target_retained": True,
    }])

    transition = chain.transitions[0]
    assert chain.status == "failed"
    assert chain.validation_level == "cutter_envelope_verified"
    assert transition.effect_verified is False
    assert transition.transition_verified is False
    assert "切削工序未产生可测量的材料去除" in transition.blocking_reasons


def test_part_state_chain_reports_mixed_validation_levels(tmp_path) -> None:
    for name in ("before.stl", "middle.stl", "after.stl"):
        (tmp_path / name).write_bytes(name.encode())
    operations = [
        {
            "operation_id": "OP10", "files": ["before.stl", "middle.stl"],
            "input_state_volume_mm3": 100.0, "output_state_volume_mm3": 99.0,
            "removed_volume_mm3": 1.0, "previous_state_shape_delta_mm3": 0.0,
            "validation_level": "cutter_envelope_verified",
        },
        {
            "operation_id": "OP20", "files": ["middle.stl", "after.stl"],
            "input_state_volume_mm3": 99.0, "output_state_volume_mm3": 98.0,
            "removed_volume_mm3": 1.0, "previous_state_shape_delta_mm3": 0.0,
            "validation_level": "geometric_draft",
        },
    ]

    chain = main.build_part_state_chain("d" * 32, tmp_path, operations)

    assert chain.status == "continuous"
    assert chain.validation_level == "mixed"


def test_l32_part_state_build_runs_in_background_and_reports_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    operation = _turning_operation()
    plan = ProcessPlan(
        title="test", material="S45C", machine="Citizen Cincom L32",
        stock={"diameter_mm": 20},
        setups=[Setup(
            id="SETUP-1", name="main", work_axis=Vec3(x=0, y=0, z=1),
            datum_feature_id="RP-1", fixture="test", operations=[operation],
        )],
        warnings=[], assumptions=[], estimated_minutes=1,
    )
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="part.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        plan=plan,
    ))

    class ImmediateThread:
        def __init__(self, *, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main, "_l32_part_state_prerequisite_error", lambda *_args: None)
    monkeypatch.setattr(main, "get_l32_material_snapshots", lambda *_args, **_kwargs: {
        "operations": [{"operation_id": "OP10", "files": ["before.stl", "after.stl"]}],
        "part_state_chain": {"status": "continuous", "validation_level": "toolpath_sweep_verified"},
    })

    response = main.build_l32_part_states_in_background(job_id)
    assert response["state"] == "queued"
    status = main.get_l32_part_state_build_status(job_id)
    assert status["state"] == "ready"
    assert status["operation_count"] == 1
    assert status["chain_verified"] is True
    assert status["unverified_operation_count"] == 0
    assert status["active"] is False


def test_l32_part_state_build_reports_ready_artifacts_with_failed_validation(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    operation = _turning_operation()
    plan = ProcessPlan(
        title="test", material="S45C", machine="Citizen Cincom L32",
        stock={"diameter_mm": 20},
        setups=[Setup(
            id="SETUP-1", name="main", work_axis=Vec3(x=0, y=0, z=1),
            datum_feature_id="RP-1", fixture="test", operations=[operation],
        )],
        warnings=[], assumptions=[], estimated_minutes=1,
    )
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="part.step", created_at=main.utc_now(),
        material="S45C", machine="Citizen Cincom L32", device_id="citizen-cincom-l32",
        plan=plan,
    ))

    class ImmediateThread:
        def __init__(self, *, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main, "_l32_part_state_prerequisite_error", lambda *_args: None)
    monkeypatch.setattr(main, "get_l32_material_snapshots", lambda *_args, **_kwargs: {
        "operations": [{"operation_id": "OP10", "files": ["before.stl", "after.stl"]}],
        "part_state_chain": {
            "status": "failed",
            "validation_level": "geometric_draft",
            "transitions": [{"operation_id": "OP10", "transition_verified": False}],
        },
    })

    main.build_l32_part_states_in_background(job_id)
    status = main.get_l32_part_state_build_status(job_id)

    assert status["state"] == "ready"
    assert status["chain_verified"] is False
    assert status["unverified_operation_count"] == 1
    assert "1 道工序未通过" in status["message"]


def test_device_library_contains_citizen_l32() -> None:
    response = client.get("/api/v1/device-library")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1.0.0"
    assert len(payload["devices"]) == 2
    device = next(item for item in payload["devices"] if item["id"] == "citizen-cincom-l32")
    assert device["id"] == "citizen-cincom-l32"
    assert device["workpiece"]["maximum_diameter_mm"] == 32
    assert device["system_integration"]["direct_nc_output"] is False
    assert device["configuration_status"] == "unconfirmed"

    virtual_device = next(item for item in payload["devices"] if item["id"] == "seksun-freecad-cam-standard")
    assert virtual_device["record_kind"] == "virtual"
    assert len(virtual_device["operation_bindings"]) == 19
    assert virtual_device["system_integration"]["production_release_requires_physical_machine"] is True


def test_unknown_device_library_item_returns_404() -> None:
    response = client.get("/api/v1/device-library/unknown")

    assert response.status_code == 404


def test_job_start_rejects_unknown_device_before_processing() -> None:
    response = client.post(
        "/api/v1/jobs/start",
        files={
            "step": ("part.step", b"STEP", "application/octet-stream"),
            "drawing": ("drawing.pdf", b"PDF", "application/pdf"),
        },
        data={"device_id": "unknown-device"},
    )

    assert response.status_code == 400
    assert "未知设备" in response.json()["detail"]


def test_device_names_resolve_to_matching_machine_profiles() -> None:
    assert main.resolve_machine("SEKSUN FreeCAD CAM 标准虚拟设备").id == "vmc-850"
    l32 = main.resolve_machine("Citizen Cincom L32")
    assert l32.id == "citizen-cincom-l32"
    assert l32.max_spindle_rpm == 8000


def test_volume_conformance_is_based_on_finished_part_not_oversized_stock() -> None:
    status, target_error, stock_error = main.volume_conformance(243.74, 196.72, 11496.49)

    assert target_error > 19
    assert stock_error < 0.5
    assert status == "failed"
    assert main.volume_conformance(243.74, 218, 11496.49)[0] == "warning"
    assert main.volume_conformance(243.74, 230, 11496.49)[0] == "passed"
    assert main.volume_conformance(243.74, 5000, 11496.49)[0] == "failed"


def test_selecting_assembly_solid_reanalyzes_and_replans(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.5.0", "source_file": "assembly.step",
        "topology": {
            "source_solids": 2, "selected_solid_index": 1,
            "selection_confirmed": 0, "selected_solids": 1, "solids": 1,
            "faces": 6, "edges": 12,
        },
        "measurements": {
            "volume": 100, "surface_area": 160,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 10, "y": 10, "z": 10},
                "size": {"x": 10, "y": 10, "z": 10},
            },
        },
        "solid_candidates": [
            {"index": 1, "volume": 100, "surface_area": 160, "center": {"x": 5, "y": 5, "z": 5},
             "bounds": {"minimum": {"x": 0, "y": 0, "z": 0}, "maximum": {"x": 10, "y": 10, "z": 10}, "size": {"x": 10, "y": 10, "z": 10}}, "selected": True},
            {"index": 2, "volume": 50, "surface_area": 100, "center": {"x": 22.5, "y": 2.5, "z": 2.5},
             "bounds": {"minimum": {"x": 20, "y": 0, "z": 0}, "maximum": {"x": 25, "y": 5, "z": 5}, "size": {"x": 5, "y": 5, "z": 5}}, "selected": False},
        ],
        "planar_features": [], "cylindrical_features": [],
    })
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="assembly.step",
        created_at=main.utc_now(), material="6061-T6", machine="VMC",
        analysis=analysis, plan=plan,
    ))
    (directory / "assembly.step").write_text("STEP", encoding="utf-8")

    def fake_analyzer(_source, _analysis_path, _model_path, solid_index=None):
        assert solid_index == 2
        updated = analysis.model_copy(deep=True)
        updated.topology["selected_solid_index"] = 2
        updated.topology["selection_confirmed"] = 1
        for candidate in updated.solid_candidates:
            candidate.selected = candidate.index == 2
        return updated

    monkeypatch.setattr(main, "run_geometry_analyzer", fake_analyzer)
    response = client.patch(f"/api/v1/jobs/{job_id}/solid", json={"solid_index": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis"]["topology"]["selected_solid_index"] == 2
    assert payload["analysis"]["topology"]["selection_confirmed"] == 1
    assert not any("目标实体尚未由用户确认" in item for item in payload["plan"]["coverage"]["issues"])


def test_browser_preview_compaction_preserves_operation_boundaries() -> None:
    segments = [
        {"operation_id": "OP10", "motion": "cut", "x1": index}
        for index in range(60)
    ] + [
        {"operation_id": "OP20", "motion": "cut", "x1": index}
        for index in range(60, 120)
    ]

    compacted = main.compact_preview_segments(segments, maximum=20)

    assert len(compacted) < len(segments)
    assert compacted[0] == segments[0]
    assert compacted[-1] == segments[-1]
    assert segments[59] in compacted
    assert segments[60] in compacted


def test_cam_stream_emits_incremental_progress(monkeypatch) -> None:
    monkeypatch.setattr(main, "load_job", lambda _job_id: SimpleNamespace(plan=object()))

    def fake_create(_job_id, progress_callback=None):
        assert progress_callback is not None
        progress_callback({
            "stage": "operation", "message": "正在生成 OP10",
            "percent": 25, "operation_id": "OP10", "current": 0, "total": 2,
        })
        progress_callback({
            "stage": "completed", "message": "CAM 刀路与仿真已完成", "percent": 100,
        })
        return {}

    monkeypatch.setattr(main, "_create_cam_artifact", fake_create)
    response = client.get(f"/api/v1/jobs/{'f' * 32}/cam/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"operation_id": "OP10"' in response.text
    assert '"stage": "completed"' in response.text


def test_job_history_lists_latest_jobs_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    for job_id, filename, created_at in (
        ("1" * 32, "older.step", "2026-01-01T08:00:00+00:00"),
        ("2" * 32, "latest.step", "2026-01-02T08:00:00+00:00"),
    ):
        directory = tmp_path / job_id
        directory.mkdir()
        main.save_job(directory, JobResponse(
            id=job_id, status="completed", filename=filename,
            created_at=created_at, material="6061-T6", machine="VMC-850",
        ))

    response = client.get("/api/v1/jobs")

    assert response.status_code == 200
    assert [item["filename"] for item in response.json()] == ["latest.step", "older.step"]
    assert response.json()[0]["operation_count"] == 0


def test_clear_job_history_preserves_current_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    preserved_id = "3" * 32
    deleted_id = "4" * 32
    for job_id, filename in ((preserved_id, "current.step"), (deleted_id, "old.step")):
        directory = tmp_path / job_id
        directory.mkdir()
        main.save_job(directory, JobResponse(
            id=job_id, status="completed", filename=filename,
            created_at="2026-01-01T08:00:00+00:00", material="6061-T6", machine="VMC-850",
        ))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    with main.JOB_EVENT_CONDITION:
        main.JOB_EVENT_LOGS[deleted_id] = [{"stage": "completed"}]

    response = client.delete("/api/v1/jobs", params={"preserve_job_id": preserved_id})

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 1}
    assert (tmp_path / preserved_id / "job.json").is_file()
    assert not (tmp_path / deleted_id).exists()
    assert (unrelated / "keep.txt").is_file()
    assert deleted_id not in main.JOB_EVENT_LOGS


def test_job_progress_stream_replays_structured_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id, status="processing", filename="stream.step",
        created_at=main.utc_now(), material="6061-T6", machine="VMC-850",
    ))
    with main.JOB_EVENT_CONDITION:
        main.JOB_EVENT_LOGS[job_id] = []
    main.publish_job_event(job_id, "geometry_analysis", "三维几何分析完成", 34, feature_count=12)
    main.publish_job_event(job_id, "completed", "工艺方案已生成", 100, operation_count=4)

    response = client.get(f"/api/v1/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"feature_count": 12' in response.text
    assert '"operation_count": 4' in response.text
    assert '"stage": "completed"' in response.text


def test_safe_cam_remediation_updates_plan_and_records_iteration(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "repair.step",
        "topology": {"solids": 1, "faces": 6, "edges": 12},
        "measurements": {
            "surface_area": 100, "volume": 100,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 20, "y": 20, "z": 10},
                "size": {"x": 20, "y": 20, "z": 10},
            },
        },
        "planar_features": [{
            "id": "PF-1", "area": 400,
            "center": {"x": 10, "y": 10, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [], "prismatic_features": [],
    })
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    for setup in plan.setups:
        for operation in setup.operations:
            operation.status = "approved"
            operation.generation_state = "generated"
    original_clearance = plan.safety.clearance_mm
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="repair.step", created_at=main.utc_now(),
        material="6061-T6", machine="VMC", analysis=analysis, plan=plan,
    ))
    main.write_json(directory / "toolpath.json", {"stale": True})
    main.write_json(directory / "remediation.json", {
        "status": "action_required", "can_auto_replan": True,
        "defects": [{"id": "DEF-001"}],
        "actions": [{
            "id": "ACT-001", "kind": "adjust_safety", "label": "提高安全平面",
            "parameters": {"clearance_mm": original_clearance + 4}, "auto_applicable": True,
        }],
    })

    response = client.post(f"/api/v1/jobs/{job_id}/cam/remediation/apply")

    assert response.status_code == 200
    payload = response.json()
    assert payload["requires_approval"] is True
    assert payload["iteration"] == 1
    assert payload["job"]["plan"]["safety"]["clearance_mm"] == original_clearance + 4
    assert all(
        operation["status"] == "proposed" and operation["generation_state"] == "dirty"
        for setup in payload["job"]["plan"]["setups"] for operation in setup["operations"]
        if operation["enabled"]
    )
    assert not (directory / "toolpath.json").exists()
    assert not (directory / "remediation.json").exists()
    assert json.loads((directory / "remediation-history.json").read_text(encoding="utf-8"))[0]["iteration"] == 1


def test_cam_remediation_loop_regenerates_and_reports_convergence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "b" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.write_json(directory / "remediation.json", {
        "status": "action_required", "can_auto_replan": True,
        "iteration": 0, "max_iterations": 3,
        "defects": [{"id": "DEF-001"}], "actions": [],
    })
    monkeypatch.setattr(main, "_apply_cam_remediation", lambda _job_id, auto_approve=False: {
        "iteration": 1,
        "applied_actions": [{"label": "提高安全平面"}],
        "requires_approval": not auto_approve,
    })

    def fake_create(_job_id, progress_callback=None):
        assert progress_callback is not None
        progress_callback({"stage": "simulation", "message": "正在复验", "percent": 90})
        return {"remediation": {
            "status": "clear", "can_auto_replan": False,
            "iteration": 1, "max_iterations": 3, "defects": [],
        }}

    monkeypatch.setattr(main, "_create_cam_artifact", fake_create)
    events = []

    result = main._run_cam_remediation_loop(job_id, progress_callback=events.append)

    assert result["remediation_loop"] == {"outcome": "passed", "iteration": 1}
    assert any(item["stage"] == "remediation_applied" for item in events)
    assert any(item["stage"] == "simulation" and item["iteration"] == 1 for item in events)
    assert events[-1]["stage"] == "completed"
    assert events[-1]["outcome"] == "passed"


def test_failed_spatial_verification_blocks_gcode_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    (directory / "program.nc").write_text("G1 X1", encoding="utf-8")
    (directory / "verification.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8",
    )

    response = client.get(f"/api/v1/jobs/{job_id}/files/program.nc")

    assert response.status_code == 409
    assert "G-code" in response.json()["detail"]


def test_health_reports_adapter_state() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "cam_status" in response.json()


def test_catalogs_expose_versioned_material_machine_and_tool_data() -> None:
    response = client.get("/api/v1/catalogs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "0.8.0"
    assert len(payload["materials"]) == 4
    assert len(payload["machines"]) == 4
    assert any(tool["id"] == "FM-50" for tool in payload["tools"])
    assert any(operation["id"] == "profile_finishing" for operation in payload["operations"])


def test_operation_library_exposes_capability_and_parameter_schema() -> None:
    response = client.get("/api/v1/operation-library")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1.0.0"
    drilling = next(item for item in payload["definitions"] if item["id"] == "drilling")
    assert drilling["manual_enabled"] is True
    assert drilling["engine"]["operation"] == "Drilling"
    assert any(parameter["key"] == "breakthrough_mm" for parameter in drilling["parameters"])
    surface = next(item for item in payload["definitions"] if item["id"] == "surface_3d")
    assert surface["manual_enabled"] is False


def test_manufacturing_process_library_exposes_route_knowledge() -> None:
    response = client.get("/api/v1/manufacturing-processes?family=cutting&compact=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["process_count"] == 98
    assert payload["summary"]["result_count"] == 43
    assert any(item["code"] == "GX-C-11" for item in payload["processes"])

    detail = client.get("/api/v1/manufacturing-processes/GX-C-11")
    assert detail.status_code == 200
    assert detail.json()["name"] == "钻孔"
    assert detail.json()["applicability"]["release_requirements"]

    missing = client.get("/api/v1/manufacturing-processes/GX-X-999")
    assert missing.status_code == 404


def test_manual_operation_can_be_created_edited_and_deleted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "manual.step",
        "topology": {"solids": 1, "faces": 3, "edges": 4},
        "measurements": {
            "surface_area": 100, "volume": 100,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 20, "y": 20, "z": 10},
                "size": {"x": 20, "y": 20, "z": 10},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "HF-1", "kind": "hole", "radius": 3, "diameter": 6,
            "length": 10, "center": {"x": 10, "y": 10, "z": 5},
            "axis": {"x": 0, "y": 0, "z": 1},
            "access_direction": {"x": 0, "y": 0, "z": 1},
            "confidence": 0.9, "review_state": "accepted",
        }],
    })
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="manual.step", created_at=main.utc_now(),
        material="6061-T6", machine="VMC", analysis=analysis, plan=plan,
    ))
    setup_id = plan.setups[0].id

    created = client.post(
        f"/api/v1/jobs/{job_id}/setups/{setup_id}/operations",
        json={"definition_id": "drilling", "feature_ids": ["HF-1"], "tool_id": "DRILL-6.0",
              "parameters": {"depth_mm": 10.8, "breakthrough_mm": 0.8}},
    )
    assert created.status_code == 200
    manual = created.json()["plan"]["setups"][0]["operations"][-1]
    assert manual["source"] == "manual"
    assert manual["definition_id"] == "drilling"
    assert manual["parameters"]["breakthrough_mm"] == 0.8

    updated = client.patch(
        f"/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{manual['id']}",
        json={"parameters": {"breakthrough_mm": 1.0}},
    )
    assert updated.status_code == 200
    edited = updated.json()["plan"]["setups"][0]["operations"][-1]
    assert edited["parameters"]["breakthrough_mm"] == 1.0
    assert edited["generation_state"] == "dirty"

    invalid = client.patch(
        f"/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{manual['id']}",
        json={"parameters": {"depth_mm": -1}},
    )
    assert invalid.status_code == 422

    deleted = client.delete(f"/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{manual['id']}")
    assert deleted.status_code == 200
    assert all(
        operation["id"] != manual["id"]
        for operation in deleted.json()["plan"]["setups"][0]["operations"]
    )


def test_config_exposes_session_sharing() -> None:
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    assert response.json()["session_sharing"] is True


def test_rejects_non_step_upload() -> None:
    response = client.post(
        "/api/v1/jobs",
        files={
            "step": ("notes.txt", b"not step", "text/plain"),
            "drawing": ("part.pdf", b"%PDF-1.4", "application/pdf"),
        },
    )
    assert response.status_code == 400


def test_new_job_requires_pdf_and_step() -> None:
    response = client.post(
        "/api/v1/jobs",
        files={"step": ("part.step", b"STEP", "application/step")},
    )
    assert response.status_code == 422


def test_create_job_imports_pdf_requirements(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(main, "MEAS_API_BASE_URL", "http://measurement:8080")
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
        "planar_features": [],
        "cylindrical_features": [{
            "id": "HF-1", "kind": "hole", "radius": 3, "diameter": 6, "length": 10,
            "center": {"x": 20, "y": 15, "z": 5}, "axis": {"x": 0, "y": 0, "z": 1},
            "confidence": 0.95, "review_state": "accepted",
        }],
    })
    monkeypatch.setattr(main, "run_geometry_analyzer", lambda *_args, **_kwargs: analysis)
    specification = {
        "schema_version": "1.1.0",
        "source": {"drawing_number": "PART-001", "revision": "A"},
        "comparison_rows": [{
            "drawing_entity": {
                "id": "D-01", "semantic_type": "diameter", "nominal": 6.0,
                "tolerance": {"upper": 0.01, "lower": -0.01}, "quantity": 1,
            },
            "mapping_status": "matched", "verification_status": "verified_geometry",
            "cad_feature_ids": ["HF-1"], "confidence": 0.96,
        }],
    }
    monkeypatch.setattr(
        main, "analyze_pdf_step",
        lambda *_args, **_kwargs: (specification, {"measurement_job_id": "meas-001"}),
    )

    response = client.post(
        "/api/v1/jobs",
        files={
            "step": ("part.step", b"STEP", "application/step"),
            "drawing": ("part.pdf", b"%PDF-1.4", "application/pdf"),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["drawing_filename"] == "part.pdf"
    assert payload["measurement_job_id"] == "meas-001"
    assert payload["material"].startswith("待确认")
    assert payload["machine"].startswith("待确认")
    assert payload["plan"]["manufacturing_requirements"]["summary"]["matched"] == 1
    route = {step["process_code"]: step for step in payload["plan"]["manufacturing_route"]["steps"]}
    assert route["GX-C-13"]["selection"] == "required"
    directory = tmp_path / payload["id"]
    assert (directory / "drawing.pdf").is_file()
    assert (directory / "manufacturing-specification.json").is_file()


def test_excluding_feature_rebuilds_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    analysis = GeometryAnalysis.model_validate(
        {
            "schema_version": "0.2.0",
            "source_file": "review.step",
            "topology": {"solids": 1, "faces": 3, "edges": 4},
            "measurements": {
                "surface_area": 100,
                "volume": 100,
                "bounding_box": {
                    "minimum": {"x": 0, "y": 0, "z": 0},
                    "maximum": {"x": 20, "y": 20, "z": 10},
                    "size": {"x": 20, "y": 20, "z": 10},
                },
            },
            "planar_features": [],
            "cylindrical_features": [
                {
                    "id": "HF-1", "kind": "hole", "radius": 2.5,
                    "diameter": 5, "length": 10,
                    "center": {"x": 10, "y": 10, "z": 5},
                    "axis": {"x": 0, "y": 0, "z": 1},
                    "access_direction": {"x": 0, "y": 0, "z": 1},
                    "confidence": 0.9, "review_state": "accepted",
                }
            ],
        }
    )
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    main.save_job(
        directory,
        JobResponse(
            id="a" * 32, status="completed", filename="review.step",
            created_at=main.utc_now(), material="6061-T6", machine="VMC",
            analysis=analysis,
        ),
    )

    response = client.patch(
        f"/api/v1/jobs/{'a' * 32}/features/HF-1",
        json={"review_state": "excluded"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis"]["cylindrical_features"][0]["review_state"] == "excluded"
    assert all(
        "HF-1" not in operation["feature_ids"]
        for setup in payload["plan"]["setups"]
        for operation in setup["operations"]
    )


def test_accepting_prismatic_feature_rebuilds_milling_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    analysis = GeometryAnalysis.model_validate(
        {
            "schema_version": "0.3.0",
            "source_file": "pocket.step",
            "topology": {"solids": 1, "faces": 10, "edges": 24},
            "measurements": {
                "surface_area": 1000, "volume": 5000,
                "bounding_box": {
                    "minimum": {"x": 0, "y": 0, "z": 0},
                    "maximum": {"x": 50, "y": 40, "z": 20},
                    "size": {"x": 50, "y": 40, "z": 20},
                },
            },
            "planar_features": [],
            "cylindrical_features": [],
            "prismatic_features": [{
                "id": "MF-1", "kind": "pocket", "source_face_id": "PF-2",
                "center": {"x": 25, "y": 20, "z": 15},
                "bounds": {
                    "minimum": {"x": 10, "y": 10, "z": 15},
                    "maximum": {"x": 40, "y": 30, "z": 15},
                    "size": {"x": 30, "y": 20, "z": 0},
                },
                "access_direction": {"x": 0, "y": 0, "z": 1},
                "length": 30, "width": 20, "depth": 5,
                "confidence": 0.7, "review_state": "review",
            }],
        }
    )
    directory = tmp_path / ("b" * 32)
    directory.mkdir()
    main.save_job(
        directory,
        JobResponse(
            id="b" * 32, status="completed", filename="pocket.step",
            created_at=main.utc_now(), material="6061-T6", machine="VMC",
            analysis=analysis,
        ),
    )

    response = client.patch(
        f"/api/v1/jobs/{'b' * 32}/features/MF-1",
        json={"review_state": "accepted"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis"]["prismatic_features"][0]["confidence"] == 0.9
    assert any(
        operation["type"] == "pocket_roughing"
        for setup in payload["plan"]["setups"]
        for operation in setup["operations"]
    )


def test_unsupported_plan_cannot_be_approved(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate(
        {
            "schema_version": "0.4.0", "source_file": "profile.step",
            "topology": {"solids": 1, "faces": 4, "edges": 8},
            "measurements": {
                "surface_area": 1000, "volume": 1000,
                "bounding_box": {
                    "minimum": {"x": 0, "y": 0, "z": 0},
                    "maximum": {"x": 100, "y": 50, "z": 3},
                    "size": {"x": 100, "y": 50, "z": 3},
                },
            },
            "planar_features": [{
                "id": "PF-1", "area": 1000,
                "center": {"x": 50, "y": 25, "z": 3},
                "normal": {"x": 0, "y": 0, "z": 1},
                "wire_count": 2,
            }],
            "cylindrical_features": [{
                "id": "CF-1", "kind": "hole", "radius": 5, "diameter": 10,
                "length": 3, "center": {"x": 0, "y": 25, "z": 1.5},
                "axis": {"x": 0, "y": 0, "z": 1}, "angular_span_degrees": 180,
            }],
        }
    )
    analysis = main.normalize_manufacturing_features(analysis)
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    main.save_job(
        directory,
        JobResponse(
            id=job_id, status="completed", filename="profile.step",
            created_at=main.utc_now(), material="6061-T6", machine="VMC",
            analysis=analysis, plan=plan,
        ),
    )

    response = client.post(f"/api/v1/jobs/{job_id}/approve")

    assert response.status_code == 409
    assert "不可批准" in response.json()["detail"]


def test_cam_without_approval_returns_artifact_links(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "c" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate(
        {
            "schema_version": "0.4.0", "source_file": "cam.step",
            "topology": {"solids": 1, "faces": 6, "edges": 12},
            "measurements": {
                "surface_area": 100, "volume": 100,
                "bounding_box": {
                    "minimum": {"x": 0, "y": 0, "z": 0},
                    "maximum": {"x": 20, "y": 20, "z": 10},
                    "size": {"x": 20, "y": 20, "z": 10},
                },
            },
            "planar_features": [{
                "id": "PF-1", "area": 400,
                "center": {"x": 10, "y": 10, "z": 10},
                "normal": {"x": 0, "y": 0, "z": 1},
            }],
            "cylindrical_features": [], "prismatic_features": [],
        }
    )
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    main.save_job(
        directory,
        JobResponse(
            id=job_id, status="completed", filename="cam.step",
            created_at=main.utc_now(), material="6061-T6", machine="VMC",
            analysis=analysis, plan=plan,
        ),
    )
    main.write_json(directory / "analysis.json", analysis.model_dump(mode="json"))
    main.write_json(directory / "plan.json", plan.model_dump(mode="json"))
    (directory / "cam.step").write_text("STEP", encoding="utf-8")

    safety_response = client.patch(
        f"/api/v1/jobs/{job_id}/safety",
        json={"clearance_mm": 4, "vise_grip_height_mm": 2},
    )
    assert safety_response.status_code == 200
    assert safety_response.json()["plan"]["safety"]["clearance_mm"] == 4
    assert all(
        operation["status"] == "proposed"
        for setup in safety_response.json()["plan"]["setups"]
        for operation in setup["operations"]
    )
    monkeypatch.setattr(main, "resolve_executable", lambda command: "/usr/bin/FreeCADCmd" if "FreeCAD" in command else None)

    def fake_freecad(command, adapter_script, arguments, timeout_seconds=600):
        _, _, _, fcstd_path, nc_path, result_path = arguments
        Path(fcstd_path).write_bytes(b"FCStd")
        Path(nc_path).write_text("G21\nG0 Z15\n", encoding="utf-8")
        Path(result_path).write_text(json.dumps({
            "engine": "FreeCAD Path", "engine_version": "test",
            "postprocessor": "grbl", "generated_operations": ["OP10"],
            "skipped": [], "path_command_count": 2,
            "preview_segments": [{
                "operation_id": "OP10", "motion": "cut",
                "x1": 0, "y1": 0, "z1": 10,
                "x2": 20, "y2": 20, "z2": 10,
            }], "gcode_bytes": 12,
        }), encoding="utf-8")
        return subprocess.CompletedProcess([command], 0, stdout="ok", stderr="")

    monkeypatch.setattr(main, "run_freecad_adapter", fake_freecad)
    response = client.post(f"/api/v1/jobs/{job_id}/cam")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["files"]["gcode"].endswith("program.nc")
    # A CAM response may still carry downloadable diagnostics when the new
    # target-volume gate proves that the generated toolpath is incomplete.
    assert payload["verification"]["status"] == "failed"
    assert any(
        check["id"] == "target_volume_conformance"
        for check in payload["verification"]["checks"]
    )
    assert payload["files"]["verification"].endswith("verification.json")
    assert payload["simulation"]["status"] == "completed"
    assert payload["files"]["simulation"].endswith("simulation.json")
    assert payload["collision"]["status"] == "passed"
    assert payload["files"]["collision"].endswith("collision.json")
    assert client.get(payload["files"]["preview"]).status_code == 200
    assert client.get(payload["files"]["verification"]).status_code == 200
    assert client.get(payload["files"]["simulation"]).status_code == 200
    assert client.get(payload["files"]["collision"]).status_code == 200
    (directory / "remediation.json").unlink()
    restored = client.get(f"/api/v1/jobs/{job_id}/cam")
    assert restored.status_code == 200
    assert restored.json()["path_command_count"] == payload["path_command_count"]
    assert restored.json()["simulation"] == payload["simulation"]
    assert restored.json()["remediation"] is not None
    assert (directory / "remediation.json").is_file()
