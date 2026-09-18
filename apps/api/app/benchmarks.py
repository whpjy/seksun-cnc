from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


DEFAULT_EXAMPLE_ROOT = Path("/var/lib/seksun-cnc/examples")


def example_root() -> Path:
    return Path(os.getenv("CNC_EXAMPLE_ROOT", str(DEFAULT_EXAMPLE_ROOT)))


def example_manifest() -> Path | None:
    value = os.getenv("CNC_EXAMPLE_MANIFEST", "").strip()
    return Path(value) if value else None


def _relative_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"benchmark manifest field {field} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"benchmark manifest field {field} must stay within the example root")
    return path


def load_example_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("benchmark manifest must contain a cases array")
    return payload


def _discover_manifest_cases(root: Path, manifest_path: Path) -> list[dict[str, object]]:
    manifest = load_example_manifest(manifest_path)
    dataset_root = _relative_path(manifest.get("dataset_root", "."), "dataset_root")
    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw_case in manifest["cases"]:
        if not isinstance(raw_case, dict):
            raise ValueError("benchmark manifest cases must be objects")
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen_ids:
            raise ValueError("benchmark manifest case ids must be unique non-empty strings")
        seen_ids.add(case_id)
        model = _relative_path(raw_case.get("model"), f"cases[{case_id}].model")
        drawing_value = raw_case.get("drawing")
        drawing = (
            _relative_path(drawing_value, f"cases[{case_id}].drawing")
            if drawing_value is not None else None
        )
        pairing_status = str(raw_case.get("pairing_status", "confirmed"))
        if pairing_status not in {"confirmed", "needs_review", "missing"}:
            raise ValueError(f"unsupported pairing_status for benchmark case {case_id}")
        relative_directory = dataset_root.as_posix()
        model_path = root / dataset_root / model
        drawing_path = root / dataset_root / drawing if drawing else None
        cases.append({
            "id": case_id,
            "label": str(raw_case.get("label") or case_id),
            "relative_directory": relative_directory,
            "primary_model": model.as_posix(),
            "model_files": [model.as_posix()],
            "drawing_files": [drawing.as_posix()] if drawing else [],
            "native_files": [],
            "measurement_files": [],
            "projection_manifests": [],
            "paired": bool(
                pairing_status == "confirmed"
                and model_path.is_file()
                and drawing_path is not None
                and drawing_path.is_file()
            ),
            "pairing_status": pairing_status,
            "model_available": model_path.is_file(),
            "drawing_available": bool(drawing_path and drawing_path.is_file()),
            "expected": raw_case.get("expected", {}),
            "tags": raw_case.get("tags", []),
            "manifest_id": manifest.get("id"),
            "manifest_version": manifest.get("version"),
        })
    return cases


def discover_example_cases(
    root: Path | None = None, manifest: Path | None = None,
) -> list[dict[str, object]]:
    explicit_root = root is not None
    resolved_root = root or example_root()
    if not resolved_root.is_dir():
        return []
    selected_manifest = (
        manifest if manifest is not None
        else None if explicit_root
        else example_manifest()
    )
    if selected_manifest is not None:
        return _discover_manifest_cases(resolved_root, selected_manifest)
    cases: list[dict[str, object]] = []
    directories = sorted(
        (item for item in resolved_root.iterdir() if item.is_dir()),
        key=lambda item: (
            int(match.group(1)) if (match := re.match(r"(\d+)", item.name)) else 10_000,
            item.name.casefold(),
        ),
    )
    for directory in directories:
        model_files = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".step", ".stp"}
        )
        drawing_files = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*.pdf") if path.is_file()
        )
        native_files = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*.prt") if path.is_file()
        )
        measurement_files = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*.csv") if path.is_file()
        )
        projection_manifests = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("manifest.json") if path.is_file()
        )
        cases.append({
            "id": directory.name,
            "label": re.sub(r"^\d+_", "", directory.name),
            "relative_directory": directory.name,
            "primary_model": model_files[0] if model_files else None,
            "model_files": model_files,
            "drawing_files": drawing_files,
            "native_files": native_files,
            "measurement_files": measurement_files,
            "projection_manifests": projection_manifests,
            "paired": bool(model_files and drawing_files),
        })
    return cases


def example_catalog_payload(
    root: Path | None = None, manifest: Path | None = None,
) -> dict[str, object]:
    explicit_root = root is not None
    resolved = root or example_root()
    selected_manifest = (
        manifest if manifest is not None
        else None if explicit_root
        else example_manifest()
    )
    cases = discover_example_cases(resolved, selected_manifest)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "available": resolved.is_dir(),
        "case_count": len(cases),
        "paired_case_count": sum(bool(item["paired"]) for item in cases),
        "cases": cases,
    }
    if selected_manifest is not None:
        manifest_payload = load_example_manifest(selected_manifest)
        payload["manifest"] = {
            "id": manifest_payload.get("id"),
            "version": manifest_payload.get("version"),
            "sha256": hashlib.sha256(selected_manifest.read_bytes()).hexdigest(),
            "path": str(selected_manifest),
        }
    return payload
