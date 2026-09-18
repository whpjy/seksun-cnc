from math import pi
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.l32_indexed_pocket import build_indexed_back_pocket_draft
from app.models import GeometryAnalysis, JobResponse


def pocket_analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "back-pocket.step",
        "topology": {"solids": 1, "faces": 1, "edges": 4},
        "measurements": {"volume": 30},
        "planar_features": [{
            "id": "PF-14", "source_face_index": 23, "area": 4.65,
            "center": {"x": -3.25, "y": 0, "z": 0},
            "normal": {"x": -1, "y": 0, "z": 0},
            "bounds": {
                "minimum": {"x": -3.25, "y": -0.775, "z": -1.5},
                "maximum": {"x": -3.25, "y": 0.775, "z": 1.5},
                "size": {"x": 0, "y": 1.55, "z": 3},
            },
            "wire_count": 1, "adjacent_edge_count": 4,
        }],
        "cylindrical_features": [],
        "prismatic_features": [{
            "id": "MF-1", "kind": "pocket", "source_face_id": "PF-14",
            "center": {"x": -3.25, "y": 0, "z": 0},
            "bounds": {
                "minimum": {"x": -3.25, "y": -0.775, "z": -1.5},
                "maximum": {"x": -3.25, "y": 0.775, "z": 1.5},
                "size": {"x": 0, "y": 1.55, "z": 3},
            },
            "access_direction": {"x": -1, "y": 0, "z": 0},
            "length": 3, "width": 1.55, "depth": 2.55,
            "review_state": "accepted",
        }],
    })


def test_pocket_draft_uses_actual_face_and_never_claims_complete_material_removal() -> None:
    analysis = pocket_analysis()
    draft = build_indexed_back_pocket_draft(analysis, analysis.prismatic_features[0])

    assert draft.source_face_index == 23
    assert draft.required_module == "U151B"
    assert draft.reference_only is True
    assert draft.nc_generated is False
    assert draft.tool_catalog_match is False
    assert draft.depth_layers == 9
    assert draft.rectangular_target_volume_mm3 == pytest.approx(11.8575)
    assert draft.minimum_uncut_corner_volume_mm3 == pytest.approx((4 - pi) * 0.25 * 2.55, abs=1e-6)
    assert any(move.point.x == -3.25 for move in draft.moves)
    assert all(-6.3 <= move.point.x <= -3.25 for move in draft.moves)
    assert all(-0.275 <= move.point.y <= 0.275 for move in draft.moves)
    assert all(-1 <= move.point.z <= 1 for move in draft.moves)
    assert all(move.kind != "rapid" or move.point.x == -6.3 for move in draft.moves)


def test_pocket_draft_rejects_overwide_tool_and_nonrectangular_face() -> None:
    analysis = pocket_analysis()
    with pytest.raises(ValueError, match="diameter exceeds"):
        build_indexed_back_pocket_draft(analysis, analysis.prismatic_features[0], tool_diameter_mm=2)

    analysis.planar_features[0].area = 4
    with pytest.raises(ValueError, match="not an exact axis-aligned rectangle"):
        build_indexed_back_pocket_draft(analysis, analysis.prismatic_features[0])


def test_catalog_pocket_endpoint_is_read_only_and_does_not_change_bound_module(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "a" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    job = JobResponse(
        id=job_id, status="completed", filename="back-pocket.step",
        created_at=main.utc_now(), material="S45C", machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32", analysis=pocket_analysis(),
        machine_instance_id="real-shop-u150b",
        machine_configuration_hash="f" * 64,
    )
    main.save_job(directory, job)
    original = (directory / "job.json").read_bytes()

    response = TestClient(main.app).get(
        f"/api/v1/jobs/{job_id}/l32/catalog-back-pocket/MF-1/draft"
    )

    assert response.status_code == 200
    assert response.json()["required_module"] == "U151B"
    assert response.json()["nc_generated"] is False
    assert (directory / "job.json").read_bytes() == original
    assert not (directory / "machine-configuration.json").exists()

    (directory / "back-pocket.step").write_text("test source", encoding="utf-8")
    result = {
        "region_volume_mm3": 11.8575,
        "target_material_inside_pocket_region_mm3": 0,
        "summed_target_contact_mm3": 0,
        "remaining_pocket_region_mm3": 0.556176,
        "stages": [{"layer": 9, "uncut_region_mm3": 0.556176}],
    }

    def fake_freecad(*args, **kwargs):
        assert args[1].name == "l32_pocket_sweep.py"
        assert args[2][0] == directory / "back-pocket.step"
        return SimpleNamespace(stdout="CNC_POCKET_SWEEP " + json.dumps(result))

    monkeypatch.setattr(main, "run_freecad_adapter", fake_freecad)
    checked = TestClient(main.app).get(
        f"/api/v1/jobs/{job_id}/l32/catalog-back-pocket/MF-1/sweep-check"
    )
    assert checked.status_code == 200
    assert checked.json()["pocket_region_status"] == "residual"
    assert checked.json()["remaining_pocket_region_mm3"] == pytest.approx(0.556176)
    assert checked.json()["removed_pocket_region_mm3"] == pytest.approx(11.301324)
    assert checked.json()["nc_generated"] is False
    assert (directory / "job.json").read_bytes() == original
