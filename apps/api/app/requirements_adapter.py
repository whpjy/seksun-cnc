from __future__ import annotations

import re
from typing import Any

from .models import (
    GeometryAnalysis, ManufacturingRequirement, ManufacturingRequirements, ThreadSpecification,
)


TYPE_ALIASES = {
    "hole_diameter": "diameter",
    "linear_distance": "linear_dimension",
    "geometric_tolerance": "gdt_feature_control_frame",
}


UNIFIED_FRACTION_RE = re.compile(
    r"(?<!\d)(?:(?P<whole>\d+)\s+)?(?P<num>\d+)\s*/\s*(?P<den>\d+)\s*-\s*"
    r"(?P<tpi>\d+(?:\.\d+)?)\s*(?P<series>UNF|UNC|UNEF)\b",
    re.IGNORECASE,
)
UNIFIED_OCR_RE = re.compile(
    r"(?<!\d)(?P<num>\d+)\s+(?P<den>\d+)\s*-\s*"
    r"(?P<tpi>\d+(?:\.\d+)?)\s*(?P<series>UNF|UNC|UNEF)\b",
    re.IGNORECASE,
)
BSPP_RE = re.compile(r"(?<![A-Z])G\s*(?P<num>\d+)\s*/\s*(?P<den>\d+)\b", re.IGNORECASE)
CLASS_FIT_RE = re.compile(r"\b(?P<class>[123][AB]|[45678][gGhH])\b")
BSPP_DIMENSIONS: dict[str, tuple[float, float]] = {
    "1/8": (9.728, 28),
    "1/4": (13.157, 19),
    "3/8": (16.662, 19),
    "1/2": (20.955, 14),
    "3/4": (26.441, 14),
    "1/1": (33.249, 11),
}


def _thread_side(text: str, class_fit: str | None) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("external", "male", "外螺纹", "外螺紋", "公牙")):
        return "external"
    if any(token in lowered for token in ("internal", "female", "内螺纹", "內螺紋", "母牙")):
        return "internal"
    if class_fit and class_fit.upper().endswith("A"):
        return "external"
    if class_fit and class_fit.upper().endswith("B"):
        return "internal"
    return "unknown"


def parse_thread_specification(text: str) -> ThreadSpecification | None:
    normalized = " ".join(text.replace("－", "-").replace("–", "-").split())
    class_match = CLASS_FIT_RE.search(normalized)
    class_fit = class_match.group("class") if class_match else None
    unified = UNIFIED_FRACTION_RE.search(normalized) or UNIFIED_OCR_RE.search(normalized)
    if unified:
        groups = unified.groupdict()
        denominator = int(groups["den"])
        if denominator <= 0:
            return None
        whole = int(groups.get("whole") or 0)
        numerator = int(groups["num"])
        inch_size = whole + numerator / denominator
        tpi = float(groups["tpi"])
        series = groups["series"].upper()
        nominal_size = f"{whole} {numerator}/{denominator}" if whole else f"{numerator}/{denominator}"
        designation = f"{nominal_size}-{tpi:g} {series}"
        if class_fit:
            designation += f"-{class_fit}"
        return ThreadSpecification(
            designation=designation,
            standard=series,
            side=_thread_side(normalized, class_fit),
            nominal_size=nominal_size,
            major_diameter_mm=round(inch_size * 25.4, 6),
            threads_per_inch=tpi,
            pitch_mm=round(25.4 / tpi, 6),
            form_angle_degrees=60,
            class_fit=class_fit,
            handedness="left" if re.search(r"\bLH\b|左旋", normalized, re.IGNORECASE) else "right",
        )

    bspp = BSPP_RE.search(normalized)
    if bspp:
        nominal_size = f"{int(bspp.group('num'))}/{int(bspp.group('den'))}"
        dimensions = BSPP_DIMENSIONS.get(nominal_size)
        if dimensions is None:
            return None
        major_diameter, tpi = dimensions
        designation = f"G{nominal_size}"
        if class_fit:
            designation += f"-{class_fit}"
        return ThreadSpecification(
            designation=designation,
            standard="BSPP",
            side=_thread_side(normalized, class_fit),
            nominal_size=nominal_size,
            major_diameter_mm=major_diameter,
            threads_per_inch=tpi,
            pitch_mm=round(25.4 / tpi, 6),
            form_angle_degrees=55,
            class_fit=class_fit,
            handedness="left" if re.search(r"\bLH\b|左旋", normalized, re.IGNORECASE) else "right",
        )
    return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_metadata(specification: dict[str, Any]) -> dict[str, Any]:
    source = specification.get("source") or {}
    if not isinstance(source, dict):
        return {}
    return source


def import_measurement_specification(
    specification: dict[str, Any], *, source_system: str = "seksun-meas",
) -> ManufacturingRequirements:
    rows = specification.get("comparison_rows") or []
    if not isinstance(rows, list):
        raise ValueError("manufacturing specification comparison_rows must be a list")

    if not rows:
        mappings = {
            str(item.get("drawing_entity_id")): item
            for item in specification.get("mappings") or []
            if isinstance(item, dict)
        }
        rows = [
            {
                "drawing_entity": entity,
                "mapping_status": (mappings.get(str(entity.get("id"))) or {}).get("status", "unmapped"),
                "verification_status": (mappings.get(str(entity.get("id"))) or {}).get("verification_status", "unmapped"),
                "cad_feature_ids": (mappings.get(str(entity.get("id"))) or {}).get("cad_feature_ids", []),
                "confidence": (mappings.get(str(entity.get("id"))) or {}).get("confidence", entity.get("confidence", 0)),
            }
            for entity in specification.get("drawing_entities") or []
            if isinstance(entity, dict)
        ]

    imported: list[ManufacturingRequirement] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        entity = row.get("drawing_entity") or {}
        if not isinstance(entity, dict):
            continue
        identifier = str(entity.get("id") or f"DRAWING-{index:04d}")
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        semantic_type = str(entity.get("semantic_type") or entity.get("type") or "unknown")
        semantic_type = TYPE_ALIASES.get(semantic_type, semantic_type)
        tolerance = entity.get("tolerance") or {}
        if not isinstance(tolerance, dict):
            tolerance = {}
        source = entity.get("source") or {}
        if not isinstance(source, dict):
            source = {}
        mapping_status = str(row.get("mapping_status") or "unmapped")
        if mapping_status not in {"matched", "ambiguous", "unmapped", "not_applicable"}:
            mapping_status = "unmapped"
        confidence = _number(row.get("confidence"))
        if confidence is None:
            confidence = _number(entity.get("confidence"))
        if confidence is None:
            confidence = _number((entity.get("extraction") or {}).get("confidence")) or 0.0
        raw_text = (
            str(source.get("raw_text") or entity.get("raw_text"))
            if source.get("raw_text") or entity.get("raw_text") else None
        )
        thread_text = " ".join(filter(None, (
            raw_text,
            str(entity.get("parameter") or ""),
            str(entity.get("subtype") or ""),
        )))
        thread = parse_thread_specification(thread_text) if semantic_type == "thread" else None
        imported.append(ManufacturingRequirement(
            id=identifier,
            type=semantic_type,
            subtype=str(entity.get("subtype")) if entity.get("subtype") else None,
            nominal=_number(entity.get("nominal")),
            minimum=_number(entity.get("minimum")),
            maximum=_number(entity.get("maximum")),
            tolerance_upper=_number(tolerance.get("upper")),
            tolerance_lower=_number(tolerance.get("lower")),
            unit=str(entity.get("unit") or "mm"),
            quantity=max(int(entity.get("quantity") or 1), 1),
            parameter=str(entity.get("parameter")) if entity.get("parameter") else None,
            cad_feature_ids=[str(item) for item in row.get("cad_feature_ids") or []],
            mapping_status=mapping_status,
            verification_status=str(row.get("verification_status") or "unknown"),
            confidence=max(0.0, min(confidence, 1.0)),
            raw_text=raw_text,
            thread=thread,
            source=source,
        ))

    unresolved = [
        item.id for item in imported
        if item.mapping_status in {"ambiguous", "unmapped"}
        or item.verification_status in {"needs_disambiguation", "unmapped"}
    ]
    matched = sum(item.mapping_status == "matched" for item in imported)
    status = "complete" if imported and not unresolved else "review" if matched else "incomplete"
    source = _source_metadata(specification)
    return ManufacturingRequirements(
        source_system=source_system,
        source_schema_version=str(specification.get("schema_version") or "") or None,
        drawing_number=source.get("drawing_number"),
        revision=source.get("revision"),
        status=status,
        requirements=imported,
        unresolved_requirement_ids=unresolved,
        summary={
            "total": len(imported),
            "matched": matched,
            "ambiguous": sum(item.mapping_status == "ambiguous" for item in imported),
            "unmapped": sum(item.mapping_status == "unmapped" for item in imported),
            "recognized_only": sum(item.mapping_status == "not_applicable" for item in imported),
        },
    )


def reconcile_requirement_bindings(
    requirements: ManufacturingRequirements, analysis: GeometryAnalysis,
) -> ManufacturingRequirements:
    """Rebind foreign feature IDs before requirements are allowed to drive planning."""
    result = requirements.model_copy(deep=True)
    known_ids = {
        item.id
        for item in (
            *analysis.planar_features,
            *analysis.cylindrical_features,
            *analysis.prismatic_features,
            *analysis.internal_profile_features,
        )
    }
    holes = [
        item for item in analysis.cylindrical_features
        if item.kind == "hole" and item.review_state != "excluded"
    ]
    external_cylinders = [
        item for item in analysis.cylindrical_features
        if item.kind in {"boss", "cylinder"} and item.review_state != "excluded"
    ]
    spatial_types = {
        "diameter", "radius", "thickness", "linear_dimension", "angle",
        "surface_roughness", "gdt_feature_control_frame", "thread",
    }
    for requirement in result.requirements:
        if requirement.mapping_status != "matched":
            continue
        if requirement.cad_feature_ids and set(requirement.cad_feature_ids).issubset(known_ids):
            continue
        if requirement.type == "diameter" and requirement.nominal is not None:
            explicit_band = max(
                abs(requirement.tolerance_upper or 0),
                abs(requirement.tolerance_lower or 0),
                0.03,
            )
            candidates = [
                item for item in holes
                if abs(item.diameter - requirement.nominal) <= explicit_band
            ]
            if len(candidates) == requirement.quantity:
                requirement.cad_feature_ids = [item.id for item in candidates]
                requirement.verification_status = "verified_geometry"
                requirement.confidence = min(requirement.confidence, min(item.confidence for item in candidates))
                requirement.source["binding_method"] = "cnc_geometry_diameter_quantity_rebind"
                continue
        if requirement.type == "thread" and requirement.thread is not None:
            thread = requirement.thread
            pool = (
                holes if thread.side == "internal"
                else external_cylinders if thread.side == "external"
                else [*holes, *external_cylinders]
            )
            diameter_tolerance = max(thread.pitch_mm * 0.75, 0.3)
            candidates = [
                item for item in pool
                if abs(item.diameter - thread.major_diameter_mm) <= diameter_tolerance
            ]
            if len(candidates) == 1:
                requirement.cad_feature_ids = [candidates[0].id]
                requirement.verification_status = "verified_geometry"
                requirement.confidence = min(requirement.confidence, candidates[0].confidence)
                requirement.source["binding_method"] = "cnc_thread_major_diameter_rebind"
                continue
        if requirement.type in spatial_types:
            requirement.mapping_status = "ambiguous" if requirement.cad_feature_ids else "unmapped"
            requirement.verification_status = "needs_cross_system_rebinding"
            requirement.confidence = min(requirement.confidence, 0.6)

    result.unresolved_requirement_ids = [
        item.id for item in result.requirements
        if item.mapping_status in {"ambiguous", "unmapped"}
        or item.verification_status in {
            "needs_disambiguation", "unmapped", "needs_cross_system_rebinding",
        }
    ]
    result.summary = {
        "total": len(result.requirements),
        "matched": sum(item.mapping_status == "matched" for item in result.requirements),
        "ambiguous": sum(item.mapping_status == "ambiguous" for item in result.requirements),
        "unmapped": sum(item.mapping_status == "unmapped" for item in result.requirements),
        "recognized_only": sum(item.mapping_status == "not_applicable" for item in result.requirements),
    }
    result.status = (
        "complete" if result.requirements and not result.unresolved_requirement_ids
        else "review" if result.summary["matched"] else "incomplete"
    )
    return result
