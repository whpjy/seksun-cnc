import json
from pathlib import Path

import httpx

from app.cnc_mcp import (
    CncApiClient, CncApiError, resolve_step_import, summarize_geometry,
    summarize_job, summarize_progress, summarize_tool_inventory,
)


def test_summarize_job_returns_compact_operations_and_draft_boundary() -> None:
    payload = {
        "id": "abc", "filename": "part.step", "status": "completed",
        "device_id": "citizen-cincom-l32", "machine_instance_id": "l32-1",
        "material": "SUS303",
        "plan": {
            "stock": {"diameter_mm": 12}, "automation_status": "review",
            "coverage": {"status": "incomplete", "target_count": 3, "covered_count": 2},
            "warnings": ["w"], "blocking_reasons": ["b"],
            "setups": [{"id": "S1", "name": "Main spindle", "workpiece_side": "front", "operations": [{
                "id": "OP10", "name": "粗车", "type": "turn_od_roughing",
                "tool": {"id": "TURN-OD-R"}, "feature_ids": ["F1"],
                "reference_profile_id": "RP-1",
            }]}],
        },
    }

    result = summarize_job(payload)

    assert result["operations"] == [{
        "id": "OP10", "name": "粗车", "type": "turn_od_roughing", "setup_id": "S1",
        "enabled": True, "tool_id": "TURN-OD-R", "feature_ids": ["F1"],
        "reference_profile_id": "RP-1", "status": None,
    }]
    assert result["setups"] == [{
        "id": "S1", "name": "Main spindle", "workpiece_side": "front", "operation_count": 1,
    }]
    assert "setups[].id" in result["setup_selection_rule"]
    assert result["release_status"] == "DRAFT"
    assert result["production_ready"] is False


def test_summarize_geometry_omits_visual_edges_and_profile_points() -> None:
    job = {"id": "abc", "analysis": {
        "topology": {"faces": 20}, "measurements": {"volume": 10},
        "visual_edges": [[{"x": 1, "y": 2, "z": 3}]],
        "cylindrical_features": [{"id": "HF-1", "kind": "hole", "diameter": 4}],
    }}
    rotational = {"profiles": [{
        "id": "RP-1", "side": "outer", "confidence": 0.9, "review_state": "review",
        "points": [{"z": -2, "radius": 3}, {"z": 4, "radius": 5}],
    }]}

    result = summarize_geometry(job, rotational)

    assert "visual_edges" not in json.dumps(result)
    assert result["feature_counts"]["cylindrical_features"] == 1
    assert result["rotational_profiles"][0]["point_count"] == 2
    assert result["rotational_profiles"][0]["z_range_mm"] == [-2.0, 4.0]


def test_cnc_api_client_surfaces_bounded_api_error() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(409, json={"detail": "validation already running"}),
    )
    client = CncApiClient("http://cnc.test", transport=transport)

    try:
        client.json("POST", "/api/v1/jobs/abc/agent/l32/validate")
    except RuntimeError as exc:
        assert str(exc) == "CNC API 409：validation already running"
    else:
        raise AssertionError("expected CncApiError")


def test_cnc_api_client_uploads_step_as_multipart(tmp_path: Path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"ISO-10303-21;ENDSEC;")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert request.url.path == "/api/v1/jobs/intake"
        assert b'filename="part.step"' in body
        assert b"citizen-cincom-l32" in body
        return httpx.Response(200, json={"id": "new-job", "status": "processing"})

    client = CncApiClient("http://cnc.test", transport=httpx.MockTransport(handler))
    result = client.upload_step(
        source, device_id="citizen-cincom-l32", material="SUS303",
    )

    assert result["id"] == "new-job"


def test_cnc_api_client_never_requests_backend_autonomous_planning(tmp_path: Path) -> None:
    source = tmp_path / "part.step"
    source.write_bytes(b"ISO-10303-21;ENDSEC;")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert request.url.path == "/api/v1/jobs/intake"
        assert b'autonomous' not in body
        return httpx.Response(200, json={"id": "harness-job", "status": "processing"})

    client = CncApiClient("http://cnc.test", transport=httpx.MockTransport(handler))
    result = client.upload_step(
        source, device_id="citizen-cincom-l32", material="SUS303",
    )

    assert result["id"] == "harness-job"


def test_resolve_step_import_enforces_roots(tmp_path: Path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "part.stp"
    source.write_text("STEP", encoding="utf-8")
    monkeypatch.setenv("CNC_MCP_IMPORT_ROOTS", str(allowed))

    assert resolve_step_import(str(source)) == source.resolve()

    outside = tmp_path / "outside.step"
    outside.write_text("STEP", encoding="utf-8")
    try:
        resolve_step_import(str(outside))
    except CncApiError as exc:
        assert "不在允许导入的目录" in str(exc)
    else:
        raise AssertionError("expected import root rejection")


def test_summarize_progress_marks_completed_job_ready() -> None:
    result = summarize_progress(
        {"id": "abc", "status": "completed", "filename": "part.step", "analysis": {"faces": 1}},
        {"events": [{"stage": "completed", "message": "done", "percent": 100}]},
    )

    assert result["ready_for_agent_review"] is True
    assert result["next_action"] == "initialize_process_draft"
    assert result["latest_event"]["message"] == "done"


def test_summarize_tool_inventory_keeps_measurement_and_capability_evidence() -> None:
    result = summarize_tool_inventory({
        "machine_instance_id": "l32-1",
        "tools": [{
            "inventory_id": "FULL-R-04",
            "catalog_tool_name": "Measured full-radius tool",
            "tool_kind": "grooving",
            "station": "T05",
            "active": True,
            "verification_state": "capability_verified",
            "measured_cutting_width_mm": 0.4,
            "measured_nose_radius_mm": 0.2,
            "groove_profile": "full_radius",
            "axial_contouring_supported": True,
            "capability_verified_by": "engineer",
            "capability_verification_reference": "inspection-42",
            "notes": "not needed by planning context",
        }],
    })

    assert result["tool_count"] == 1
    assert result["active_tool_count"] == 1
    assert result["tools"][0]["inventory_id"] == "FULL-R-04"
    assert result["tools"][0]["measured_nose_radius_mm"] == 0.2
    assert result["tools"][0]["capability_verification_reference"] == "inspection-42"
    assert "notes" not in result["tools"][0]
