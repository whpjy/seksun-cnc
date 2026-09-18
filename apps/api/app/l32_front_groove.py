"""Read-only radial-groove geometry for an accepted exact turning profile.

This is a cutter-envelope study, not a bound tool or a machine program.
Every strip uses the *maximum* target radius under the entire tool width;
sampling only its centre would gouge the rounded groove shoulders.
"""

from __future__ import annotations

from math import ceil, pi

from pydantic import BaseModel, Field

from .rotational_features import RotationalProfile, TurningProfileFeature


class GrooveStrip(BaseModel):
    z_min_mm: float
    z_max_mm: float
    target_envelope_radius_mm: float = Field(ge=0)
    cut_to_radius_mm: float = Field(ge=0)
    ideal_removed_volume_mm3: float = Field(ge=0)


class FrontGrooveGeometryDraft(BaseModel):
    schema_version: str = "1.0.0"
    profile_id: str
    feature_id: str
    reference_only: bool = True
    nc_generated: bool = False
    tool_catalog_match: bool = False
    material_sweep_verified: bool = False
    proposed_tool_width_mm: float = Field(gt=0)
    actual_planned_tool_width_mm: float = Field(gt=0)
    actual_tool_fits_floor: bool
    groove_floor_width_mm: float = Field(gt=0)
    allowance_mm: float = Field(gt=0)
    stock_radius_mm: float = Field(gt=0)
    strips: list[GrooveStrip]
    warnings: list[str]


def _radius_at(profile: RotationalProfile, z: float) -> float:
    points = profile.points
    if z < points[0].z - 1e-9 or z > points[-1].z + 1e-9:
        raise ValueError("groove strip extends beyond the exact profile")
    for left, right in zip(points, points[1:]):
        if left.z - 1e-9 <= z <= right.z + 1e-9:
            if right.z - left.z <= 1e-12:
                return max(left.radius, right.radius)
            fraction = max(0.0, min(1.0, (z-left.z)/(right.z-left.z)))
            return left.radius + fraction*(right.radius-left.radius)
    return points[-1].radius


def _maximum_radius(profile: RotationalProfile, minimum: float, maximum: float) -> float:
    return max(
        _radius_at(profile, minimum),
        _radius_at(profile, maximum),
        *(point.radius for point in profile.points if minimum <= point.z <= maximum),
    )


def build_front_groove_geometry_draft(
    profile: RotationalProfile,
    feature: TurningProfileFeature,
    *,
    stock_radius_mm: float,
    actual_planned_tool_width_mm: float,
    proposed_tool_width_mm: float = 0.25,
    allowance_mm: float = 0.02,
) -> FrontGrooveGeometryDraft:
    if profile.side != "outer" or profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise ValueError("an accepted exact outer profile is required")
    if feature.kind != "external_groove_candidate" or feature.profile_id != profile.id:
        raise ValueError("feature is not an external groove of this profile")
    if min(stock_radius_mm, actual_planned_tool_width_mm, proposed_tool_width_mm, allowance_mm) <= 0:
        raise ValueError("stock, widths and clearance must be positive")
    if proposed_tool_width_mm > feature.width_mm + 1e-9:
        raise ValueError("proposed tool is wider than the exact groove floor")
    if len(feature.source_point_indices) != 2:
        raise ValueError("groove floor source points are missing")
    left_index, right_index = feature.source_point_indices
    if not 0 < left_index < right_index < len(profile.points)-1:
        raise ValueError("groove floor indices are invalid")
    baseline = max(feature.radius_start, feature.radius_end) + feature.depth_mm
    left_lands = [
        index for index in range(left_index)
        if profile.points[index].radius >= baseline - 1e-4
    ]
    right_lands = [
        index for index in range(right_index+1, len(profile.points))
        if profile.points[index].radius >= baseline - 1e-4
    ]
    if not left_lands or not right_lands:
        raise ValueError("groove shoulders cannot be bounded by exact outer lands")
    z_min = profile.points[left_lands[-1]].z
    z_max = profile.points[right_lands[0]].z
    if z_max-z_min < proposed_tool_width_mm:
        raise ValueError("proposed tool does not fit the full groove region")
    # Half-width overlap eliminates unswept axial bands between plunges.
    count = max(1, ceil(2*(z_max-z_min)/proposed_tool_width_mm)-1)
    centre_min = z_min + proposed_tool_width_mm/2
    centre_max = z_max - proposed_tool_width_mm/2
    strips: list[GrooveStrip] = []
    for index in range(count+1):
        centre = centre_min + (centre_max-centre_min)*index/count
        left = centre-proposed_tool_width_mm/2
        right = centre+proposed_tool_width_mm/2
        envelope = _maximum_radius(profile,left,right)
        cut_to = envelope+allowance_mm
        if cut_to >= stock_radius_mm:
            continue
        strips.append(GrooveStrip(
            z_min_mm=round(left,9),
            z_max_mm=round(right,9),
            target_envelope_radius_mm=round(envelope,9),
            cut_to_radius_mm=round(cut_to,9),
            ideal_removed_volume_mm3=round(pi*(stock_radius_mm**2-cut_to**2)*(right-left),9),
        ))
    if not strips:
        raise ValueError("groove draft removes no round-bar material")
    fits = actual_planned_tool_width_mm <= feature.width_mm + 1e-9
    return FrontGrooveGeometryDraft(
        profile_id=profile.id,
        feature_id=feature.id,
        proposed_tool_width_mm=proposed_tool_width_mm,
        actual_planned_tool_width_mm=actual_planned_tool_width_mm,
        actual_tool_fits_floor=fits,
        groove_floor_width_mm=feature.width_mm,
        allowance_mm=allowance_mm,
        stock_radius_mm=stock_radius_mm,
        strips=strips,
        warnings=[
            f"Planned grooving tool width {actual_planned_tool_width_mm:g} mm exceeds {feature.width_mm:g} mm floor; OP33 cannot cut this groove."
        ] if not fits else [],
    )
