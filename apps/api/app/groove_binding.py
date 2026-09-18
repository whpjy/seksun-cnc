from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import ManufacturingRequirements
from .rotational_features import RotationalFeatureAnalysis, TurningProfileFeature


GrooveSide = Literal["external", "internal", "unknown"]
GrooveRequirementKind = Literal["external_groove", "internal_groove", "seal_groove"]


class DrawingGrooveRequirement(BaseModel):
    """Auditable drawing evidence used to bind a groove to STEP geometry.

    A manifest declaration proves manufacturing intent, but not dimensions.  It
    therefore remains ``recognized_only`` until dimensions have been reviewed.
    """

    id: str
    kind: GrooveRequirementKind
    side: GrooveSide = "unknown"
    width_mm: float | None = Field(default=None, gt=0)
    depth_mm: float | None = Field(default=None, gt=0)
    bottom_diameter_mm: float | None = Field(default=None, gt=0)
    verification_status: Literal["recognized_only", "verified_dimensions"] = "recognized_only"
    raw_text: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)


class GrooveCandidateEvidence(BaseModel):
    id: str
    profile_id: str
    side: Literal["external", "internal"]
    z_start_mm: float
    z_end_mm: float
    width_mm: float
    depth_mm: float
    bottom_diameter_mm: float
    confidence: float
    review_state: str


class GrooveRequirementBinding(BaseModel):
    requirement_id: str
    status: Literal["matched", "ambiguous", "unmapped"]
    candidate_ids: list[str] = Field(default_factory=list)
    matched_candidate_id: str | None = None
    reason: str


def groove_candidate_evidence(
    features: list[TurningProfileFeature],
) -> list[GrooveCandidateEvidence]:
    candidates: list[GrooveCandidateEvidence] = []
    for feature in features:
        if feature.kind not in {"external_groove_candidate", "internal_groove_candidate"}:
            continue
        side: Literal["external", "internal"] = (
            "internal" if feature.kind == "internal_groove_candidate" else "external"
        )
        bottom_radius = (
            max(feature.radius_start, feature.radius_end)
            if side == "internal"
            else min(feature.radius_start, feature.radius_end)
        )
        candidates.append(GrooveCandidateEvidence(
            id=feature.id,
            profile_id=feature.profile_id,
            side=side,
            z_start_mm=min(feature.z_start, feature.z_end),
            z_end_mm=max(feature.z_start, feature.z_end),
            width_mm=feature.width_mm,
            depth_mm=feature.depth_mm,
            bottom_diameter_mm=bottom_radius * 2,
            confidence=feature.confidence,
            review_state=feature.review_state,
        ))
    return candidates


def requirements_from_manifest(case: dict[str, object]) -> list[DrawingGrooveRequirement]:
    expected = case.get("expected")
    required = expected.get("required_features", []) if isinstance(expected, dict) else []
    if not isinstance(required, list):
        return []
    result: list[DrawingGrooveRequirement] = []
    for kind in ("external_groove", "internal_groove", "seal_groove"):
        if kind not in required:
            continue
        side: GrooveSide = (
            "external" if kind == "external_groove"
            else "internal" if kind == "internal_groove"
            else "unknown"
        )
        result.append(DrawingGrooveRequirement(
            id=f"{case.get('id', 'CASE')}-DRAWING-{kind.upper()}",
            kind=kind,  # type: ignore[arg-type]
            side=side,
            verification_status="recognized_only",
            source={
                "method": "benchmark_manifest_required_feature",
                "drawing_files": list(case.get("drawing_files", [])),
                "note": "图纸需求已由样例清单确认，但槽宽、槽深和槽底直径尚未结构化复核",
            },
        ))
    return result


def bind_groove_requirement(
    requirement: DrawingGrooveRequirement,
    candidates: list[GrooveCandidateEvidence],
    *,
    linear_tolerance_mm: float = 0.05,
    diameter_tolerance_mm: float = 0.05,
) -> GrooveRequirementBinding:
    side_candidates = [
        candidate for candidate in candidates
        if requirement.side == "unknown" or candidate.side == requirement.side
    ]
    if not side_candidates:
        return GrooveRequirementBinding(
            requirement_id=requirement.id,
            status="unmapped",
            reason="STEP 中未发现与图纸要求侧别一致的槽候选",
        )

    constraints = [
        requirement.width_mm is not None,
        requirement.depth_mm is not None,
        requirement.bottom_diameter_mm is not None,
    ]
    if requirement.verification_status != "verified_dimensions" or sum(constraints) < 2:
        return GrooveRequirementBinding(
            requirement_id=requirement.id,
            status="ambiguous",
            candidate_ids=[item.id for item in side_candidates],
            reason="图纸槽尺寸尚未完成结构化复核，STEP 候选不得自动绑定",
        )

    matches = []
    for candidate in side_candidates:
        if requirement.width_mm is not None and abs(candidate.width_mm - requirement.width_mm) > linear_tolerance_mm:
            continue
        if requirement.depth_mm is not None and abs(candidate.depth_mm - requirement.depth_mm) > linear_tolerance_mm:
            continue
        if (
            requirement.bottom_diameter_mm is not None
            and abs(candidate.bottom_diameter_mm - requirement.bottom_diameter_mm) > diameter_tolerance_mm
        ):
            continue
        matches.append(candidate)
    if len(matches) == 1:
        return GrooveRequirementBinding(
            requirement_id=requirement.id,
            status="matched",
            candidate_ids=[matches[0].id],
            matched_candidate_id=matches[0].id,
            reason="侧别及至少两项已复核图纸尺寸与唯一 STEP 槽候选一致",
        )
    if matches:
        return GrooveRequirementBinding(
            requirement_id=requirement.id,
            status="ambiguous",
            candidate_ids=[item.id for item in matches],
            reason="多项 STEP 槽候选同时满足图纸尺寸，仍需位置或视图绑定消歧",
        )
    return GrooveRequirementBinding(
        requirement_id=requirement.id,
        status="unmapped",
        candidate_ids=[item.id for item in side_candidates],
        reason="现有 STEP 槽候选均不满足已复核图纸尺寸",
    )


def assess_case_grooves(
    case: dict[str, object], features: list[TurningProfileFeature],
) -> dict[str, object]:
    candidates = groove_candidate_evidence(features)
    requirements = requirements_from_manifest(case)
    bindings = [bind_groove_requirement(item, candidates) for item in requirements]
    if not requirements:
        status = "not_required"
    elif any(item.status == "unmapped" for item in bindings):
        status = "missing"
    elif any(item.status == "ambiguous" for item in bindings):
        status = "ambiguous"
    else:
        status = "matched"
    return {
        "status": status,
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "bindings": [item.model_dump(mode="json") for item in bindings],
        "summary": {
            "requirement_count": len(requirements),
            "external_candidate_count": sum(item.side == "external" for item in candidates),
            "internal_candidate_count": sum(item.side == "internal" for item in candidates),
            "matched_count": sum(item.status == "matched" for item in bindings),
            "ambiguous_count": sum(item.status == "ambiguous" for item in bindings),
            "unmapped_count": sum(item.status == "unmapped" for item in bindings),
        },
    }


def bind_verified_groove_requirements(
    rotational: RotationalFeatureAnalysis,
    requirements: ManufacturingRequirements | None,
) -> RotationalFeatureAnalysis:
    """Attach only CNC-verified drawing groove requirements to STEP candidates."""

    if requirements is None:
        return rotational
    result = rotational.model_copy(deep=True)
    feature_index = {
        item.id: item for item in result.features
        if item.kind in {"external_groove_candidate", "internal_groove_candidate"}
    }
    for requirement in requirements.requirements:
        if (
            requirement.type not in {"external_groove", "internal_groove", "seal_groove"}
            or requirement.mapping_status != "matched"
            or requirement.verification_status != "verified_engineer"
            or requirement.source.get("binding_method")
            != "cnc_groove_dimensions_engineer_confirmation"
        ):
            continue
        if len(requirement.cad_feature_ids) != 1:
            continue
        feature = feature_index.get(requirement.cad_feature_ids[0])
        if feature is None:
            continue
        feature.binding_state = "matched"
        feature.drawing_requirement_ids = [requirement.id]
    return result
