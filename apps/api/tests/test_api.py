import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.models import GeometryAnalysis, JobResponse


client = TestClient(app)


def test_volume_conformance_is_based_on_finished_part_not_oversized_stock() -> None:
    status, target_error, stock_error = main.volume_conformance(243.74, 196.72, 11496.49)

    assert target_error > 19
    assert stock_error < 0.5
    assert status == "failed"
    assert main.volume_conformance(243.74, 218, 11496.49)[0] == "warning"
    assert main.volume_conformance(243.74, 230, 11496.49)[0] == "passed"
    assert main.volume_conformance(243.74, 5000, 11496.49)[0] == "failed"


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
    assert len(payload["machines"]) == 3
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


def test_cam_requires_approval_and_returns_artifact_links(tmp_path, monkeypatch) -> None:
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

    assert client.post(f"/api/v1/jobs/{job_id}/cam").status_code == 409
    assert client.post(f"/api/v1/jobs/{job_id}/approve").status_code == 200
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
    assert client.post(f"/api/v1/jobs/{job_id}/cam").status_code == 409
    assert client.post(f"/api/v1/jobs/{job_id}/approve").status_code == 200

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
    restored = client.get(f"/api/v1/jobs/{job_id}/cam")
    assert restored.status_code == 200
    assert restored.json()["path_command_count"] == payload["path_command_count"]
    assert restored.json()["simulation"] == payload["simulation"]
