import struct

from app.conformance import compare_stock_to_target_mesh


def _write_cube_stl(path) -> None:
    vertices = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    data = bytearray(80) + struct.pack("<I", len(faces))
    for face in faces:
        coordinates = [value for index in face for value in vertices[index]]
        data.extend(struct.pack("<12fH", 0, 0, 0, *coordinates, 0))
    path.write_bytes(data)


def _surface(upper_value: float) -> dict[str, object]:
    return {
        "columns": 11, "rows": 11, "resolution_mm": 0.1,
        "origin": {"x": 0, "y": 0},
        "frame": {
            "x": {"x": 1, "y": 0, "z": 0},
            "y": {"x": 0, "y": 1, "z": 0},
            "z": {"x": 0, "y": 0, "z": 1},
        },
        "heights": [upper_value] * 121,
        "lower_heights": [0.0] * 121,
    }


def test_spatial_conformance_accepts_matching_stock(tmp_path) -> None:
    mesh = tmp_path / "cube.stl"
    _write_cube_stl(mesh)

    result = compare_stock_to_target_mesh(_surface(1.0), mesh)

    assert result["status"] == "passed"
    assert result["target_overlap_percent"] == 100
    assert result["missing_target_volume_mm3"] == 0


def test_spatial_conformance_rejects_equal_volume_in_wrong_place(tmp_path) -> None:
    mesh = tmp_path / "cube.stl"
    _write_cube_stl(mesh)
    surface = _surface(1.0)
    surface["heights"] = [0.5] * 121
    surface["lower_heights"] = [-0.5] * 121

    result = compare_stock_to_target_mesh(surface, mesh, toolpath_segments=[{
        "operation_id": "OP20", "motion": "cut",
        "x1": 0, "y1": 0.5, "z1": 0.5,
        "x2": 1, "y2": 0.5, "z2": 0.5,
    }])

    assert result["status"] == "failed"
    assert result["target_overlap_percent"] == 50
    assert result["missing_target_volume_mm3"] == result["excess_stock_volume_mm3"]
    assert {item["kind"] for item in result["defect_regions"]} == {"overcut", "excess_stock"}
    assert result["defect_samples"]
    overcut = next(item for item in result["defect_regions"] if item["kind"] == "overcut")
    assert overcut["attribution"][0]["operation_id"] == "OP20"
    assert overcut["attribution"][0]["method"] == "nearest_cut_segment"
    assert overcut["bounds"]["minimum"]["z"] >= 0.5
