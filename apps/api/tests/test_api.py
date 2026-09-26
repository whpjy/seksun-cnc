import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.models import GeometryAnalysis, JobResponse


client = TestClient(app)


def test_agent_architecture_exposes_subgraphs_and_tool_boundaries() -> None:
    response = client.get("/api/v1/agent/architecture")

    assert response.status_code == 200
    payload = response.json()
    assert payload["orchestrator"] == "langgraph"
    assert [item["id"] for item in payload["subgraphs"]] == [
        "manufacturing_orchestrator", "feature_recognition", "process_planning",
        "operation_execution", "validation_remediation",
    ]
    assert any(tool["id"] == "planning.review_with_qwen" and not tool["deterministic"] for tool in payload["tools"])
    assert any(tool["id"] == "validation.check_result" and tool["deterministic"] for tool in payload["tools"])
    assert any(tool["id"] == "world.commit" and tool["deterministic"] for tool in payload["tools"])
    assert any(tool["id"] == "review.simulation_with_ai" and not tool["deterministic"] for tool in payload["tools"])
    remediation = next(item for item in payload["subgraphs"] if item["id"] == "validation_remediation")
    assert remediation["status"] == "implemented"
    assert "regenerate_and_validate" in remediation["nodes"]


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


def test_job_start_rejects_unknown_device_before_processing(monkeypatch) -> None:
    monkeypatch.setattr(main, "HARNESS_ONLY_MODE", False)
    response = client.post(
        "/api/v1/jobs/start",
        files={"step": ("part.step", b"STEP", "application/octet-stream")},
        data={"device_id": "unknown-device"},
    )

    assert response.status_code == 400
    assert "未知设备" in response.json()["detail"]


def test_create_job_accepts_step_without_drawing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(main, "HARNESS_ONLY_MODE", False)
    monkeypatch.setattr(main, "_process_new_job", lambda job_id: main.load_job(job_id))

    response = client.post(
        "/api/v1/jobs",
        files={"step": ("part.step", b"STEP", "application/step")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["drawing_filename"] is None
    assert payload["drawing_url"] is None
    assert not (tmp_path / payload["id"] / "drawing.pdf").exists()


def test_start_job_accepts_step_without_drawing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(main, "HARNESS_ONLY_MODE", False)
    monkeypatch.setattr(
        main, "threading",
        SimpleNamespace(Thread=lambda **_kwargs: SimpleNamespace(start=lambda: None)),
    )

    response = client.post(
        "/api/v1/jobs/start",
        files={"step": ("part.stp", b"STEP", "application/step")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["drawing_filename"] is None
    assert payload["drawing_url"] is None
    directory = tmp_path / payload["id"]
    assert (directory / "part.stp").read_bytes() == b"STEP"
    assert not (directory / "drawing.pdf").exists()


def test_harness_only_mode_rejects_legacy_planning_upload(monkeypatch) -> None:
    monkeypatch.setattr(main, "HARNESS_ONLY_MODE", True)

    response = client.post(
        "/api/v1/jobs/start",
        files={"step": ("part.stp", b"STEP", "application/step")},
    )

    assert response.status_code == 409
    assert "DeepSeek Harness" in response.json()["detail"]


def test_harness_intake_upload_does_not_start_process_planning(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(
        main, "threading",
        SimpleNamespace(Thread=lambda **_kwargs: SimpleNamespace(start=lambda: None)),
    )

    response = client.post(
        "/api/v1/jobs/intake",
        files={"step": ("agent-part.stp", b"STEP", "application/step")},
        data={"device_id": "citizen-cincom-l32"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["plan"] is None
    events = json.loads((tmp_path / payload["id"] / "planning-events.json").read_text(encoding="utf-8"))
    assert events[-1]["evidence"][0]["value"] == "DeepSeek Harness"


def test_harness_initializes_empty_operation_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.5.0", "source_file": "part.step",
        "topology": {"solids": 1, "faces": 6, "edges": 12},
        "measurements": {"volume": 100, "surface_area": 160, "bounding_box": {
            "minimum": {"x": 0, "y": 0, "z": 0},
            "maximum": {"x": 10, "y": 10, "z": 10},
            "size": {"x": 10, "y": 10, "z": 10},
        }},
        "planar_features": [{
            "id": "PF-1", "area": 100, "center": {"x": 5, "y": 5, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [], "prismatic_features": [],
    })
    job_id = "a" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="part.step", created_at=main.utc_now(),
        material="6061-T6", machine="VMC850", analysis=analysis,
        model_url=f"/api/v1/jobs/{job_id}/files/model.stl",
    ))

    response = client.post(f"/api/v1/jobs/{job_id}/agent/plan/initialize")

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["ai_planning"]["planning_owner"] == "deepseek_harness"
    assert plan["ai_planning"]["baseline_operations_imported"] is False
    assert sum(len(setup["operations"]) for setup in plan["setups"]) == 0
    assert plan["coverage"]["production_ready"] is False


def test_planning_waits_for_archived_cam_validation_before_completion(tmp_path, monkeypatch) -> None:
    from app.agent import config as agent_config

    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(agent_config, "load_agent_settings", lambda: agent_config.AgentSettings(mode="disabled"))
    monkeypatch.setattr(main, "persist_rotational_analysis", lambda *_args: None)
    monkeypatch.setattr(main, "resolve_executable", lambda _command: "/usr/bin/FreeCADCmd")
    monkeypatch.setattr(main, "review_process_plan", lambda *_args, **_kwargs: (_ for _ in ()).throw(main.QwenPlanningError("offline")))
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.5.0", "source_file": "part.step",
        "topology": {"solids": 1, "faces": 6, "edges": 12},
        "measurements": {"volume": 100, "surface_area": 160, "bounding_box": {
            "minimum": {"x": 0, "y": 0, "z": 0},
            "maximum": {"x": 10, "y": 10, "z": 10},
            "size": {"x": 10, "y": 10, "z": 10},
        }},
        "planar_features": [{"id": "PF-1", "area": 100, "center": {"x": 5, "y": 5, "z": 10}, "normal": {"x": 0, "y": 0, "z": 1}}],
        "cylindrical_features": [], "prismatic_features": [],
    })
    monkeypatch.setattr(main, "run_geometry_analyzer", lambda *_args: analysis)
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    plan.automation_status = "ready"
    monkeypatch.setattr(main, "build_process_plan", lambda *_args, **_kwargs: plan.model_copy(deep=True))
    job_id = "c" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    (directory / "part.step").write_text("STEP", encoding="utf-8")
    main.save_job(directory, JobResponse(
        id=job_id, status="processing", filename="part.step", created_at=main.utc_now(),
        material="6061-T6", machine="VMC",
    ))
    observed_status = []

    def fake_cam(_job_id, progress_callback=None):
        observed_status.append(main.load_job(_job_id).status)
        for filename in ("toolpath.json", "simulation.json", "verification.json", "collision.json"):
            main.write_json(directory / filename, {})
        progress_callback({"stage": "completed", "message": "CAM complete", "percent": 100})
        return {"verification": {"status": "passed"}, "collision": {"status": "passed"}}

    monkeypatch.setattr(main, "_create_cam_with_agent_loop", fake_cam)
    events = []
    result = main._process_new_job(job_id, ai_assisted=True, progress_callback=lambda stage, message, percent, **details: events.append((stage, details)))

    assert observed_status == ["processing"]
    assert result.status == "completed"
    assert (directory / "simulation.json").is_file()
    assert any(stage == "cam_validation" and details.get("agent_status") == "completed" for stage, details in events)
    assert events[-1][0] == "completed"


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


def test_active_cam_run_automatically_enters_safe_remediation_loop(monkeypatch) -> None:
    monkeypatch.setenv("CNC_AGENT_MODE", "active")

    def fake_create(_job_id, progress_callback=None):
        assert progress_callback is not None
        progress_callback({"stage": "completed", "message": "初次仿真完成", "percent": 100})
        return {"remediation": {
            "status": "action_required",
            "can_auto_replan": True,
            "max_iterations": 3,
            "defects": [{"id": "DEF-001"}],
        }}

    def fake_loop(_job_id, progress_callback=None):
        assert progress_callback is not None
        progress_callback({
            "stage": "completed", "message": "自动纠错通过", "percent": 100,
            "outcome": "passed",
        })
        return {"remediation_loop": {"outcome": "passed", "iteration": 1}}

    monkeypatch.setattr(main, "_create_cam_artifact", fake_create)
    monkeypatch.setattr(main, "_run_cam_remediation_loop", fake_loop)
    events = []

    result = main._create_cam_with_agent_loop("a" * 32, progress_callback=events.append)

    assert result["remediation_loop"]["outcome"] == "passed"
    assert [item["stage"] for item in events] == ["agent_remediation_start", "completed"]
    assert all(item.get("message") != "初次仿真完成" for item in events)


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


def test_delete_job_removes_directory_and_progress_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "d" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="delete.step",
        created_at=main.utc_now(), material="6061-T6", machine="VMC-850",
    ))
    (directory / "model.stl").write_bytes(b"mesh")
    with main.JOB_EVENT_CONDITION:
        main.JOB_EVENT_LOGS[job_id] = [{"stage": "completed"}]

    response = client.delete(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "job_id": job_id}
    assert not directory.exists()
    assert job_id not in main.JOB_EVENT_LOGS


def test_delete_job_rejects_processing_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "f" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id, status="processing", filename="active.step",
        created_at=main.utc_now(), material="6061-T6", machine="VMC-850",
    ))

    response = client.delete(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 409
    assert directory.is_dir()


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
    main.publish_job_event(job_id, "geometry_analysis", "三维几何分析完成", 34, feature_count=12, agent_status="completed")
    main.publish_job_event(job_id, "completed", "工艺方案已生成", 100, operation_count=4)

    response = client.get(f"/api/v1/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"feature_count": 12' in response.text
    assert '"operation_count": 4' in response.text
    assert '"stage": "completed"' in response.text
    persisted = json.loads((directory / "planning-events.json").read_text(encoding="utf-8"))
    assert [item["stage"] for item in persisted] == ["geometry_analysis", "completed"]
    assert persisted[0]["schema_version"] == "1.0.0"
    assert persisted[0]["subgraph"] == "feature_recognition"
    assert persisted[0]["node_id"] == "extract_geometry"
    assert persisted[0]["metrics"]["feature_count"] == 12

    with main.JOB_EVENT_CONDITION:
        main.JOB_EVENT_LOGS.pop(job_id, None)
    history_response = client.get(f"/api/v1/jobs/{job_id}/planning-events")
    assert history_response.status_code == 200
    assert [item["stage"] for item in history_response.json()] == ["geometry_analysis", "completed"]

    (directory / "model.stl").write_bytes(b"solid test\nendsolid test\n")
    (directory / "analysis.json").write_text('{"topology": {"solids": 1}}', encoding="utf-8")
    workspace_response = client.get(f"/api/v1/jobs/{job_id}/agent/workspace")
    assert workspace_response.status_code == 200
    workspace = workspace_response.json()
    assert workspace["thread_id"] == job_id
    assert workspace["active_event_id"] == persisted[-1]["event_id"]
    assert [artifact["id"] for artifact in workspace["artifacts"]] == ["model", "analysis"]


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
        files={"step": ("notes.txt", b"not step", "text/plain")},
    )
    assert response.status_code == 400


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


def test_incomplete_coverage_cannot_be_approved(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "9" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "incomplete.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "surface_area": 1000, "volume": 1000,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 20, "y": 20, "z": 20},
                "size": {"x": 20, "y": 20, "z": 20},
            },
        },
        "planar_features": [],
        "cylindrical_features": [{
            "id": "HF-1", "kind": "hole", "radius": 2, "diameter": 4,
            "length": 20, "center": {"x": 10, "y": 10, "z": 10},
            "axis": {"x": 1, "y": 0, "z": 0}, "review_state": "accepted",
        }],
    })
    plan = main.build_process_plan(analysis, "6061-T6", "Citizen Cincom L32")
    assert plan.coverage is not None and plan.coverage.production_ready is False
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="incomplete.step",
        created_at=main.utc_now(), material="6061-T6", machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32", analysis=analysis, plan=plan,
    ))

    response = client.post(f"/api/v1/jobs/{job_id}/approve")

    assert response.status_code == 409
    assert "工艺覆盖门禁未通过" in response.json()["detail"]


def test_agent_evaluation_can_block_otherwise_ready_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "8" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "0.8.0", "source_file": "ready.step",
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
            "id": "PF-1", "area": 400, "center": {"x": 10, "y": 10, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [], "prismatic_features": [],
    })
    plan = main.build_process_plan(analysis, "6061-T6", "VMC")
    plan.automation_status = "ready"
    assert plan.coverage is not None
    plan.coverage.status = "complete"
    plan.coverage.score = 1.0
    plan.coverage.production_ready = True
    assert plan.coverage is not None and plan.coverage.production_ready is True
    main.save_job(directory, JobResponse(
        id=job_id, status="completed", filename="ready.step",
        created_at=main.utc_now(), material="6061-T6", machine="VMC",
        analysis=analysis, plan=plan,
    ))
    main.write_json(directory / "agent-evaluation.json", {
        "eligible_for_promotion": False,
        "blocking_reasons": ["AI 识别到高风险制造问题"],
    })

    response = client.post(f"/api/v1/jobs/{job_id}/approve")

    assert response.status_code == 409
    assert "智能体工艺门禁" in response.json()["detail"]


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
    assert (directory / "cam-manifest.json").is_file()

    changed_job = main.load_job(job_id)
    changed_job.plan.assumptions.append("Test plan changed after CAM generation")
    main.save_job(directory, changed_job)
    stale_response = client.get(f"/api/v1/jobs/{job_id}/cam")
    assert stale_response.status_code == 409
