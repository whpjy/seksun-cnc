from pathlib import Path

import json

import pytest

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


def test_discovers_flat_dataset_from_versioned_manifest(tmp_path: Path) -> None:
    dataset = tmp_path / "liquid"
    dataset.mkdir()
    (dataset / "part.step").touch()
    (dataset / "part.pdf").touch()
    (dataset / "unconfirmed.step").touch()
    (dataset / "unconfirmed.pdf").touch()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0.0",
        "id": "l32-liquid",
        "version": "1",
        "dataset_root": "liquid",
        "cases": [
            {
                "id": "PART-1", "model": "part.step", "drawing": "part.pdf",
                "pairing_status": "confirmed", "expected": {"machine_capability": "standard"},
            },
            {
                "id": "PART-2", "model": "unconfirmed.step", "drawing": "unconfirmed.pdf",
                "pairing_status": "needs_review",
            },
        ],
    }), encoding="utf-8")

    cases = discover_example_cases(tmp_path, manifest)
    payload = example_catalog_payload(tmp_path, manifest)

    assert [item["id"] for item in cases] == ["PART-1", "PART-2"]
    assert cases[0]["relative_directory"] == "liquid"
    assert cases[0]["paired"] is True
    assert cases[0]["expected"]["machine_capability"] == "standard"
    assert cases[1]["paired"] is False
    assert cases[1]["pairing_status"] == "needs_review"
    assert payload["case_count"] == 2
    assert payload["paired_case_count"] == 1
    assert payload["manifest"]["id"] == "l32-liquid"
    assert len(payload["manifest"]["sha256"]) == 64


def test_manifest_rejects_paths_outside_example_root(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "cases": [{"id": "unsafe", "model": "../outside.step"}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="stay within"):
        discover_example_cases(tmp_path, manifest)
