import pytest
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient

from app import main
from app.l32_side_milling import (
    _distance_to_segment, _inside_polygon,
    build_l32_exterior_clear_draft, build_l32_side_mill_draft,
)


def side_face() -> dict:
    return {
        "face_number": 17, "analysis_face_id": "PF-8", "normal_y": -1,
        "center_xyz_mm": [-1, -0.775, 0], "wire_count": 1,
        "points_xz_mm": [
            [-2, -1], [0, -1], [0, 1], [-2, 1], [-2, -1],
        ],
    }


def test_side_raster_stays_inside_exact_face_and_approaches_from_outside_stock() -> None:
    draft = build_l32_side_mill_draft(side_face(), stock_radius_mm=2.815)

    assert draft.required_module == "U30B"
    assert draft.nc_generated is False
    assert draft.tool_catalog_match is False
    assert draft.face_y_mm == -0.775
    assert draft.layer_count == 7
    assert draft.raster_count_per_layer > 0
    assert len(draft.moves) == draft.layer_count*draft.raster_count_per_layer*4
    assert all(-1.73 <= move.point.x <= -0.27 for move in draft.moves)
    assert all(-0.73 <= move.point.z <= 0.73 for move in draft.moves)
    assert all(move.point.y == -3.315 for move in draft.moves if move.kind == "rapid")
    assert any(move.point.y == -0.775 for move in draft.moves if move.kind == "feed")


def test_side_raster_rejects_tool_wider_than_face() -> None:
    with pytest.raises(ValueError, match="no cutter-center region"):
        build_l32_side_mill_draft(side_face(), stock_radius_mm=2.815, tool_diameter_mm=4)


def test_exterior_raster_stays_outside_target_silhouette() -> None:
    face = side_face()
    draft = build_l32_exterior_clear_draft(face,stock_radius_mm=2.815,access_sign=1)
    vertices = [tuple(point) for point in face["points_xz_mm"][:-1]]
    edges = list(zip(vertices,vertices[1:]+vertices[:1]))

    assert draft.mode == "exterior_clear"
    assert draft.layer_count == 8
    assert draft.moves
    assert any(move.point.y == 0 for move in draft.moves if move.kind == "feed")
    for move in draft.moves:
        x,z = move.point.x,move.point.z
        assert not _inside_polygon(x,z,vertices)
        assert min(_distance_to_segment(x,z,a,b) for a,b in edges) >= 0.25+0.019


def test_side_toolpaths_endpoint_keeps_reference_path_separate_from_machine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(main, "load_job", lambda _: SimpleNamespace(
        plan=SimpleNamespace(stock={"type": "round_bar", "diameter_mm": 5.63}),
        machine_instance_id=None,
        filename="side.step",
    ))
    monkeypatch.setattr(main, "get_l32_catalog_ear_geometry", lambda _: {"side_faces": [side_face()]})

    response = TestClient(main.app).get(
        "/api/v1/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/l32/catalog-ear-toolpaths"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reference_only"] is True
    assert payload["material_sweep_verified"] is False
    assert payload["bound_machine_has_required_module"] is None
    assert payload["side_drafts"][0]["analysis_face_id"] == "PF-8"
    assert payload["side_drafts"][0]["moves"]

    directory = tmp_path / ("a"*32)
    directory.mkdir()
    (directory / "side.step").write_text("test source", encoding="utf-8")
    checks = {"target_solid_valid": True, "checks": [{
        "analysis_face_id": "PF-8", "checked_feed_segments": 315,
        "contacting_segments": 0, "summed_target_contact_mm3": 0,
    }]}
    monkeypatch.setattr(main, "run_freecad_adapter", lambda *args, **kwargs: SimpleNamespace(
        stdout="CNC_SIDE_SWEEP " + json.dumps(checks),
    ))
    checked = TestClient(main.app).get(
        "/api/v1/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/l32/catalog-ear-sweep-check"
    )
    assert checked.status_code == 200
    assert checked.json()["target_gouge_check_passed"] is True
    assert checked.json()["whole_part_material_verified"] is False

    opposite = {**side_face(), "face_number": 20, "analysis_face_id": "PF-11",
                "normal_y": 1, "center_xyz_mm": [-1,0.775,0]}
    monkeypatch.setattr(main, "get_l32_catalog_ear_geometry", lambda _: {"side_faces": [side_face(),opposite]})
    exterior = TestClient(main.app).get(
        "/api/v1/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/l32/catalog-exterior-toolpaths"
    )
    assert exterior.status_code == 200
    assert len(exterior.json()["exterior_drafts"]) == 2
    assert all(item["mode"] == "exterior_clear" for item in exterior.json()["exterior_drafts"])
