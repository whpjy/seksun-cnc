from __future__ import annotations

import os
import re
from pathlib import Path


DEFAULT_EXAMPLE_ROOT = Path("/var/lib/seksun-cnc/examples")


def example_root() -> Path:
    return Path(os.getenv("CNC_EXAMPLE_ROOT", str(DEFAULT_EXAMPLE_ROOT)))


def discover_example_cases(root: Path | None = None) -> list[dict[str, object]]:
    root = root or example_root()
    if not root.is_dir():
        return []
    cases: list[dict[str, object]] = []
    directories = sorted(
        (item for item in root.iterdir() if item.is_dir()),
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


def example_catalog_payload(root: Path | None = None) -> dict[str, object]:
    resolved = root or example_root()
    cases = discover_example_cases(resolved)
    return {
        "schema_version": "1.0.0",
        "available": resolved.is_dir(),
        "case_count": len(cases),
        "paired_case_count": sum(bool(item["paired"]) for item in cases),
        "cases": cases,
    }
