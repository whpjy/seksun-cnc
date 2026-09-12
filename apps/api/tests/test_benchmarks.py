from pathlib import Path

from app.benchmarks import discover_example_cases, example_catalog_payload


def test_discovers_all_related_2d_and_3d_case_files(tmp_path: Path) -> None:
    case = tmp_path / "1_sample"
    case.mkdir()
    (case / "sample.stp").touch()
    (case / "drawing-a.pdf").touch()
    (case / "drawing-b.pdf").touch()
    (case / "native.prt").touch()
    (case / "measure.csv").touch()
    projections = case / "projection"
    projections.mkdir()
    (projections / "manifest.json").touch()

    cases = discover_example_cases(tmp_path)

    assert len(cases) == 1
    assert cases[0]["paired"] is True
    assert cases[0]["primary_model"] == "sample.stp"
    assert cases[0]["drawing_files"] == ["drawing-a.pdf", "drawing-b.pdf"]
    assert cases[0]["projection_manifests"] == ["projection/manifest.json"]
    assert example_catalog_payload(tmp_path)["paired_case_count"] == 1
