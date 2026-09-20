from __future__ import annotations

from itertools import product
from math import sqrt
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import (
    Bounds,
    CylindricalFeature,
    GeometryAnalysis,
    ManufacturingRequirements,
    RotationalSectionCandidate,
    Vec3,
)


ReviewState = Literal["accepted", "review", "excluded"]


class RotationalAxisCandidate(BaseModel):
    id: str
    origin: Vec3
    direction: Vec3
    confidence: float = Field(ge=0, le=1)
    source_feature_ids: list[str]
    review_state: ReviewState = "review"
    review_reasons: list[str] = Field(default_factory=list)


class RotationalProfilePoint(BaseModel):
    z: float
    radius: float = Field(ge=0)


class RotationalProfile(BaseModel):
    id: str
    axis_id: str
    side: Literal["outer", "inner"]
    extraction_method: Literal["bounding_cylinder", "edge_projection_envelope", "exact_section"]
    points: list[RotationalProfilePoint]
    confidence: float = Field(ge=0, le=1)
    review_state: ReviewState = "review"
    review_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ordered_profile(self) -> "RotationalProfile":
        if len(self.points) < 2:
            raise ValueError("rotational profile requires at least two points")
        z_values = [item.z for item in self.points]
        if z_values != sorted(z_values):
            raise ValueError("rotational profile points must be ordered by Z")
        return self


class TurningProfileFeature(BaseModel):
    id: str
    profile_id: str
    kind: Literal[
        "cylindrical_land", "taper", "radial_transition",
        "external_groove_candidate", "internal_groove_candidate", "thread_form_candidate",
        "inner_bore", "inner_taper", "cutoff_boundary",
    ]
    z_start: float
    z_end: float
    radius_start: float
    radius_end: float
    width_mm: float = Field(ge=0)
    depth_mm: float = Field(default=0, ge=0)
    observed_repeat_mm: float | None = Field(default=None, gt=0)
    pitch_candidates_mm: list[float] = Field(default_factory=list)
    repeat_count: int = Field(default=1, ge=1)
    binding_state: Literal["unbound", "matched", "ambiguous"] = "unbound"
    drawing_requirement_ids: list[str] = Field(default_factory=list)
    resolved_pitch_mm: float | None = Field(default=None, gt=0)
    resolved_major_diameter_mm: float | None = Field(default=None, gt=0)
    thread_side: Literal["external", "internal", "unknown"] | None = None
    thread_form_angle_degrees: float | None = Field(default=None, gt=0)
    thread_designation: str | None = None
    source_point_indices: list[int]
    confidence: float = Field(ge=0, le=1)
    review_state: ReviewState = "review"
    review_reasons: list[str] = Field(default_factory=list)


class RotationalFeatureAnalysis(BaseModel):
    schema_version: str = "1.0.0"
    source_file: str
    axes: list[RotationalAxisCandidate] = Field(default_factory=list)
    profiles: list[RotationalProfile] = Field(default_factory=list)
    features: list[TurningProfileFeature] = Field(default_factory=list)
    status: Literal["candidate", "not_detected", "not_rotational", "solid_selection_required"]
    evidence: dict[str, float | int | str | list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _normalize_direction(axis: Vec3) -> Vec3:
    length = sqrt(axis.x * axis.x + axis.y * axis.y + axis.z * axis.z)
    if length <= 1e-9:
        raise ValueError("cylindrical feature axis has zero length")
    values = [axis.x / length, axis.y / length, axis.z / length]
    dominant = max(range(3), key=lambda index: abs(values[index]))
    if values[dominant] < 0:
        values = [-value for value in values]
    return Vec3(x=values[0], y=values[1], z=values[2])


def _dot(left: Vec3, right: Vec3) -> float:
    return left.x * right.x + left.y * right.y + left.z * right.z


def _subtract(left: Vec3, right: Vec3) -> Vec3:
    return Vec3(x=left.x - right.x, y=left.y - right.y, z=left.z - right.z)


def _radial_distance(point: Vec3, origin: Vec3, direction: Vec3) -> float:
    delta = _subtract(point, origin)
    axial = _dot(delta, direction)
    radial_x = delta.x - axial * direction.x
    radial_y = delta.y - axial * direction.y
    radial_z = delta.z - axial * direction.z
    return sqrt(radial_x * radial_x + radial_y * radial_y + radial_z * radial_z)


def _cross(left: Vec3, right: Vec3) -> Vec3:
    return Vec3(
        x=left.y * right.z - left.z * right.y,
        y=left.z * right.x - left.x * right.z,
        z=left.x * right.y - left.y * right.x,
    )


def _bbox_transverse_extents(bounds: Bounds, direction: Vec3) -> tuple[float, float]:
    helper = Vec3(x=1, y=0, z=0) if abs(direction.x) < 0.8 else Vec3(x=0, y=1, z=0)
    transverse_u = _normalize_direction(_cross(direction, helper))
    transverse_v = _normalize_direction(_cross(direction, transverse_u))
    corners = [
        Vec3(x=x, y=y, z=z)
        for x, y, z in product(
            (bounds.minimum.x, bounds.maximum.x),
            (bounds.minimum.y, bounds.maximum.y),
            (bounds.minimum.z, bounds.maximum.z),
        )
    ]
    projections_u = [_dot(item, transverse_u) for item in corners]
    projections_v = [_dot(item, transverse_v) for item in corners]
    return max(projections_u) - min(projections_u), max(projections_v) - min(projections_v)


def _source_solid_count(analysis: GeometryAnalysis) -> int:
    return int(analysis.topology.get("source_solids", analysis.topology.get("solids", 1)))


def _simplify_profile(points: list[RotationalProfilePoint], tolerance: float) -> list[RotationalProfilePoint]:
    if len(points) <= 2:
        return points
    simplified = [points[0]]
    for current, following in zip(points[1:-1], points[2:]):
        previous = simplified[-1]
        left_dz = current.z - previous.z
        right_dz = following.z - current.z
        if abs(left_dz) <= tolerance or abs(right_dz) <= tolerance:
            simplified.append(current)
            continue
        left_slope = (current.radius - previous.radius) / left_dz
        right_slope = (following.radius - current.radius) / right_dz
        if abs(left_slope - right_slope) > tolerance:
            simplified.append(current)
    simplified.append(points[-1])
    return simplified


def _edge_projection_profile(
    analysis: GeometryAnalysis, origin: Vec3, direction: Vec3,
) -> list[RotationalProfilePoint]:
    projected: list[tuple[float, float]] = []
    hole_radii = [
        item.radius for item in analysis.cylindrical_features
        if item.kind == "hole" and item.review_state != "excluded"
    ]
    for edge in analysis.visual_edges:
        for point in edge:
            delta = _subtract(point, origin)
            axial = _dot(delta, direction)
            radius = _radial_distance(point, origin, direction)
            if radius <= 1e-7:
                continue
            if any(abs(radius - hole_radius) <= max(1e-4, hole_radius * 1e-4) for hole_radius in hole_radii):
                continue
            projected.append((axial, radius))
    if len(projected) < 2:
        return []

    axial_values = [item[0] for item in projected]
    span = max(axial_values) - min(axial_values)
    z_tolerance = max(span * 1e-5, 1e-5)
    radius_tolerance = max(max(item[1] for item in projected) * 1e-5, 1e-5)
    z_groups: list[list[tuple[float, float]]] = []
    for sample in sorted(projected):
        if not z_groups or abs(sample[0] - sum(item[0] for item in z_groups[-1]) / len(z_groups[-1])) > z_tolerance:
            z_groups.append([sample])
        else:
            z_groups[-1].append(sample)

    profile_points: list[RotationalProfilePoint] = []
    for group in z_groups:
        z_value = sum(item[0] for item in group) / len(group)
        distinct_radii: list[float] = []
        for radius in sorted((item[1] for item in group)):
            if not distinct_radii or abs(radius - distinct_radii[-1]) > radius_tolerance:
                distinct_radii.append(radius)
        # Multiple radii at one axial position preserve a radial shoulder. The
        # descending-radius order is obtained naturally when the profile is
        # traversed from the front in negative-Z direction.
        profile_points.extend(RotationalProfilePoint(z=z_value, radius=radius) for radius in distinct_radii)
    return _simplify_profile(profile_points, max(z_tolerance, radius_tolerance))


def _profile_radius_at_z(points: list[RotationalProfilePoint], z_value: float) -> float | None:
    radii = [point.radius for point in points if abs(point.z - z_value) <= 1e-7]
    for left, right in zip(points, points[1:]):
        delta = right.z - left.z
        if abs(delta) <= 1e-9:
            continue
        if left.z - 1e-7 <= z_value <= right.z + 1e-7:
            ratio = (z_value - left.z) / delta
            radii.append(left.radius + ratio * (right.radius - left.radius))
    return max(radii) if radii else None


def _repair_outer_profile_with_axial_holes(
    analysis: GeometryAnalysis,
    profile: RotationalProfile,
    origin: Vec3,
    direction: Vec3,
    coordinate_system: str,
) -> tuple[RotationalProfile, list[dict[str, float | str]], list[str]]:
    """Raise a sectional OD envelope only where full axial holes prove it too small.

    A complete cylindrical hole surface at radial offset R and hole radius r proves
    that the rotational outer envelope is at least R+r over that cylinder's axial
    span.  This repairs section-direction loss without inventing a radius larger
    than the maximum already observed on the source solid.
    """
    profile_min = min(point.z for point in profile.points)
    profile_max = max(point.z for point in profile.points)
    observed_max = max(point.radius for point in profile.points)
    constraints: list[dict[str, float | str]] = []
    rejected: list[str] = []
    for hole in analysis.cylindrical_features:
        if (
            hole.kind != "hole"
            or hole.review_state != "accepted"
            or hole.angular_span_degrees < 359.0
        ):
            continue
        hole_direction = _normalize_direction(hole.axis)
        parallel = abs(_dot(hole_direction, direction))
        if parallel < 0.999:
            continue
        delta = _subtract(hole.center, origin)
        axial_from_origin = _dot(delta, direction)
        radial_offset = _radial_distance(hole.center, origin, direction)
        center_z = (
            axial_from_origin
            if coordinate_system in {"local", "unknown"}
            else _dot(hole.center, direction)
        )
        half_length = hole.length * parallel / 2
        minimum = max(profile_min, center_z - half_length)
        maximum = min(profile_max, center_z + half_length)
        if maximum < minimum + 1e-7:
            continue
        required = radial_offset + hole.radius
        sample_z = {
            minimum, maximum,
            *(point.z for point in profile.points if minimum - 1e-7 <= point.z <= maximum + 1e-7),
        }
        available = min(
            radius for z_value in sample_z
            if (radius := _profile_radius_at_z(profile.points, z_value)) is not None
        )
        if required <= available + 0.05:
            continue
        if required > observed_max + 0.05:
            rejected.append(hole.id)
            continue
        constraints.append({
            "feature_id": hole.id,
            "z_min_mm": minimum,
            "z_max_mm": maximum,
            "required_radius_mm": required,
            "previous_min_radius_mm": available,
        })
    if not constraints:
        return profile, [], rejected

    repaired_points: list[RotationalProfilePoint] = []
    boundary_values = {
        float(item[key]) for item in constraints for key in ("z_min_mm", "z_max_mm")
    }
    source_points = [*profile.points]
    for boundary in sorted(boundary_values):
        radius = _profile_radius_at_z(profile.points, boundary)
        if radius is not None:
            source_points.append(RotationalProfilePoint(z=boundary, radius=radius))
    for point in sorted(source_points, key=lambda item: item.z):
        required = max(
            (
                float(item["required_radius_mm"])
                for item in constraints
                if float(item["z_min_mm"]) - 1e-7 <= point.z <= float(item["z_max_mm"]) + 1e-7
            ),
            default=point.radius,
        )
        candidate = RotationalProfilePoint(z=point.z, radius=max(point.radius, required))
        if not repaired_points or (
            abs(repaired_points[-1].z - candidate.z) > 1e-7
            or abs(repaired_points[-1].radius - candidate.radius) > 1e-7
        ):
            repaired_points.append(candidate)
    repaired = profile.model_copy(deep=True)
    repaired.points = repaired_points
    repaired.confidence = min(repaired.confidence, 0.68)
    repaired.review_reasons.append(
        "精确截面外包络已按完整轴向孔的实体包络下限修复；修复结果必须重新人工确认。"
    )
    return repaired, constraints, rejected


def _candidate_score(feature: CylindricalFeature) -> float:
    review_factor = 0 if feature.review_state == "excluded" else 1
    kind_factor = 1 if feature.kind in {"boss", "cylinder"} else 0.25
    return review_factor * kind_factor * feature.radius * max(feature.length, 0.1) * feature.confidence


def _extract_profile_features(profile: RotationalProfile) -> list[TurningProfileFeature]:
    points = profile.points
    if len(points) < 2:
        return []
    z_span = max(point.z for point in points) - min(point.z for point in points)
    maximum_radius = max(point.radius for point in points)
    z_tolerance = max(z_span * 1e-5, 1e-4)
    radius_tolerance = max(maximum_radius * 1e-4, 1e-3)
    minimum_groove_depth = max(maximum_radius * 0.005, 0.03)
    minimum_groove_width = 0.05
    confidence = min(profile.confidence, 0.72 if profile.extraction_method == "exact_section" else 0.5)
    result: list[TurningProfileFeature] = []

    def nearest_land_radius(segment_index: int, direction: int) -> float | None:
        """Return the nearest stable land beyond any rounded transition chords."""

        adjacent_index = segment_index - 1 if direction < 0 else segment_index + 1
        adjacent_reference: float | None = None
        if 0 <= adjacent_index < len(points) - 1:
            adjacent_left = points[adjacent_index]
            adjacent_right = points[adjacent_index + 1]
            adjacent_reference = (
                adjacent_left.radius if direction < 0 else adjacent_right.radius
            )
            if (
                abs(adjacent_right.z - adjacent_left.z) <= z_tolerance
                and abs(adjacent_right.radius - adjacent_left.radius) > radius_tolerance
            ):
                return adjacent_reference

        candidate_index = segment_index + direction
        while 0 <= candidate_index < len(points) - 1:
            left = points[candidate_index]
            right = points[candidate_index + 1]
            candidate_delta_z = abs(right.z - left.z)
            candidate_delta_radius = abs(right.radius - left.radius)
            if (
                candidate_delta_z > z_tolerance
                and candidate_delta_radius <= radius_tolerance
            ):
                stable_radius = (left.radius + right.radius) / 2
                if adjacent_reference is None:
                    return stable_radius
                return (
                    min(adjacent_reference, stable_radius)
                    if profile.side == "inner"
                    else max(adjacent_reference, stable_radius)
                )
            candidate_index += direction
        return adjacent_reference

    for index, (start, end) in enumerate(zip(points, points[1:]), start=1):
        delta_z = abs(end.z - start.z)
        delta_radius = abs(end.radius - start.radius)
        if delta_z <= z_tolerance and delta_radius <= radius_tolerance:
            continue
        kind: str
        depth = 0.0
        reasons: list[str]
        if delta_z <= z_tolerance:
            kind = "radial_transition"
            reasons = ["同一轴向位置出现半径变化，暂按台阶或槽壁候选处理。"]
        elif delta_radius <= radius_tolerance:
            if profile.side == "inner":
                kind = "inner_bore"
                reasons = ["恒定内半径段来自截面包络，需排除横向孔和局部槽交线。"]
                previous_radius = nearest_land_radius(index - 1, -1)
                following_radius = nearest_land_radius(index - 1, 1)
                surrounding_radius = (
                    max(previous_radius, following_radius)
                    if previous_radius is not None and following_radius is not None
                    else None
                )
                if (
                    surrounding_radius is not None
                    and min(start.radius, end.radius) - surrounding_radius
                    >= minimum_groove_depth
                    and delta_z >= minimum_groove_width
                ):
                    kind = "internal_groove_candidate"
                    depth = min(start.radius, end.radius) - surrounding_radius
                    reasons = ["局部恒定内半径段高于两侧最近稳定基孔；槽深以稳定基孔计算，槽宽、圆角和内槽刀仍须由图纸确认。"]
            else:
                kind = "cylindrical_land"
                reasons = ["恒定外半径段来自截面包络，需与图纸尺寸绑定。"]
                previous_radius = nearest_land_radius(index - 1, -1)
                following_radius = nearest_land_radius(index - 1, 1)
                surrounding_radius = (
                    min(previous_radius, following_radius)
                    if previous_radius is not None and following_radius is not None
                    else None
                )
                if (
                    surrounding_radius is not None
                    and surrounding_radius - max(start.radius, end.radius)
                    >= minimum_groove_depth
                    and delta_z >= minimum_groove_width
                ):
                    kind = "external_groove_candidate"
                    depth = surrounding_radius - max(start.radius, end.radius)
                    reasons = ["局部恒定外半径段低于两侧最近稳定外圆；槽深以稳定外圆计算，槽宽、圆角和刀宽仍须由图纸确认。"]
        else:
            kind = "inner_taper" if profile.side == "inner" else "taper"
            reasons = ["轴向与半径同时变化，暂按锥面或圆弧离散段处理。"]
        result.append(TurningProfileFeature(
            id=f"TPF-{profile.side.upper()}-{index}",
            profile_id=profile.id,
            kind=kind,
            z_start=start.z,
            z_end=end.z,
            radius_start=start.radius,
            radius_end=end.radius,
            width_mm=delta_z,
            depth_mm=max(0, depth),
            source_point_indices=[index - 1, index],
            confidence=confidence,
            review_state="review",
            review_reasons=reasons,
        ))

    if profile.side == "outer":
        groove_candidates = [
            item for item in result
            if item.kind == "external_groove_candidate"
            and item.depth_mm >= minimum_groove_depth
            and item.width_mm >= minimum_groove_width
        ]
        longest_run: list[TurningProfileFeature] = []
        for start_index, first in enumerate(groove_candidates):
            run = [first]
            spacings: list[float] = []
            for following in groove_candidates[start_index + 1:]:
                previous = run[-1]
                width_similar = abs(following.width_mm - previous.width_mm) <= max(
                    following.width_mm, previous.width_mm,
                ) * 0.2
                depth_similar = abs(following.depth_mm - previous.depth_mm) <= max(
                    following.depth_mm, previous.depth_mm,
                ) * 0.2
                previous_center = (previous.z_start + previous.z_end) / 2
                following_center = (following.z_start + following.z_end) / 2
                spacing = following_center - previous_center
                spacing_similar = (
                    not spacings
                    or abs(spacing - sum(spacings) / len(spacings))
                    <= max(abs(spacing), abs(sum(spacings) / len(spacings))) * 0.15
                )
                if not width_similar or not depth_similar or spacing <= z_tolerance or not spacing_similar:
                    break
                run.append(following)
                spacings.append(spacing)
            if len(run) > len(longest_run):
                longest_run = run
        if len(longest_run) >= 4:
            observed_repeat = sum(
                (right.z_start + right.z_end - left.z_start - left.z_end) / 2
                for left, right in zip(longest_run, longest_run[1:])
            ) / (len(longest_run) - 1)
            member_ids = {item.id for item in longest_run}
            result = [item for item in result if item.id not in member_ids]
            result.append(TurningProfileFeature(
                id=f"TPF-{profile.side.upper()}-THREAD-1",
                profile_id=profile.id,
                kind="thread_form_candidate",
                z_start=min(item.z_start for item in longest_run),
                z_end=max(item.z_end for item in longest_run),
                radius_start=max(item.radius_start for item in longest_run),
                radius_end=max(item.radius_end for item in longest_run),
                width_mm=max(item.z_end for item in longest_run) - min(item.z_start for item in longest_run),
                depth_mm=sum(item.depth_mm for item in longest_run) / len(longest_run),
                observed_repeat_mm=observed_repeat,
                pitch_candidates_mm=[observed_repeat, observed_repeat * 2],
                repeat_count=len(longest_run),
                source_point_indices=sorted({
                    point_index
                    for item in longest_run
                    for point_index in item.source_point_indices
                }),
                confidence=min(profile.confidence, 0.68),
                review_state="review",
                review_reasons=[
                    "检测到周期性重复齿形；截面外包络可能每个螺距出现两次交线，必须与图纸螺距绑定后确认。"
                ],
            ))
        cutoff_index = min(range(len(points)), key=lambda item: points[item].z)
        cutoff = points[cutoff_index]
        result.append(TurningProfileFeature(
            id=f"TPF-{profile.side.upper()}-CUTOFF",
            profile_id=profile.id,
            kind="cutoff_boundary",
            z_start=cutoff.z,
            z_end=cutoff.z,
            radius_start=cutoff.radius,
            radius_end=cutoff.radius,
            width_mm=0,
            source_point_indices=[cutoff_index],
            confidence=min(profile.confidence, 0.6),
            review_state="review",
            review_reasons=["按外轮廓最小 Z 端建立切断边界候选，接料方式和成品端面余量确认前不得执行。"],
        ))
    return result


def suppress_rectangular_internal_grooves(profile: RotationalProfile) -> RotationalProfile:
    """Return the base-bore contour with sharp local ID recesses removed.

    A longitudinal boring tool must leave a local recess for a dedicated ID
    grooving tool. Only the exact four-node rectangular form is suppressed;
    rounded or tapered recesses remain review-only geometry.
    """
    if profile.side != "inner" or len(profile.points) < 4:
        return profile.model_copy(deep=True)
    removed: set[int] = set()
    points = profile.points
    for index in range(1, len(points) - 2):
        left_base, groove_left, groove_right, right_base = points[index - 1:index + 3]
        if (
            abs(left_base.z - groove_left.z) <= 1e-9
            and groove_right.z > groove_left.z + 1e-9
            and abs(groove_right.z - right_base.z) <= 1e-9
            and abs(groove_left.radius - groove_right.radius) <= 1e-9
            and groove_left.radius > max(left_base.radius, right_base.radius) + 1e-6
        ):
            removed.update((index, index + 1))
    if not removed:
        return profile.model_copy(deep=True)
    return profile.model_copy(update={
        "points": [point.model_copy(deep=True) for index, point in enumerate(points) if index not in removed],
    })


def suppress_external_grooves(profile: RotationalProfile) -> RotationalProfile:
    """Return the longitudinal OD contour with dedicated grooves bridged.

    External groove candidates belong to a grooving operation, not to the
    ordinary OD roughing/finishing path.  The candidate itself may have sharp
    or rounded flanks, so expand from its bottom land to the nearest stable
    cylindrical land on each side and retain only those land boundary points.
    Interpolation between the retained boundaries represents the base OD that
    the longitudinal tool may safely prepare without entering the recess.
    """
    if profile.side != "outer" or len(profile.points) < 4:
        return profile.model_copy(deep=True)

    points = profile.points
    candidates = [
        item for item in _extract_profile_features(profile)
        if item.kind == "external_groove_candidate"
        and len(item.source_point_indices) == 2
    ]
    removed: set[int] = set()
    for candidate in candidates:
        bottom_left, bottom_right = candidate.source_point_indices

        left_land_end: int | None = None
        for segment in range(bottom_left - 1, -1, -1):
            left, right = points[segment], points[segment + 1]
            if abs(right.z - left.z) > 1e-6 and abs(right.radius - left.radius) <= 1e-6:
                left_land_end = segment + 1
                break

        right_land_start: int | None = None
        for segment in range(bottom_right + 1, len(points) - 1):
            left, right = points[segment], points[segment + 1]
            if abs(right.z - left.z) > 1e-6 and abs(right.radius - left.radius) <= 1e-6:
                right_land_start = segment
                break

        if left_land_end is None or right_land_start is None:
            continue
        if left_land_end >= right_land_start:
            continue
        baseline_minimum = min(
            points[left_land_end].radius,
            points[right_land_start].radius,
        )
        if max(points[bottom_left].radius, points[bottom_right].radius) >= baseline_minimum - 1e-6:
            continue
        removed.update(range(left_land_end + 1, right_land_start))

    if not removed:
        return profile.model_copy(deep=True)
    return profile.model_copy(update={
        "points": [
            point.model_copy(deep=True)
            for index, point in enumerate(points)
            if index not in removed
        ],
    })


def clip_rotational_profile(
    profile: RotationalProfile,
    minimum_z: float,
    maximum_z: float,
) -> RotationalProfile:
    """Clip a profile to point-aligned bounds used by a regional operation."""
    if maximum_z <= minimum_z:
        raise ValueError("profile region maximum Z must be greater than minimum Z")
    profile_minimum = min(point.z for point in profile.points)
    profile_maximum = max(point.z for point in profile.points)
    if minimum_z < profile_minimum - 1e-6 or maximum_z > profile_maximum + 1e-6:
        raise ValueError("profile region lies outside the accepted profile")
    points = [
        point.model_copy(deep=True)
        for point in profile.points
        if minimum_z - 1e-9 <= point.z <= maximum_z + 1e-9
    ]
    if len(points) < 2 or len({round(point.z, 9) for point in points}) < 2:
        raise ValueError("profile region must contain at least two axial positions")
    return profile.model_copy(update={"points": points})


def split_outer_profile_for_longitudinal_turning(
    profile: RotationalProfile,
) -> tuple[
    RotationalProfile,
    RotationalProfile | None,
    RotationalProfile | None,
]:
    """Split an OD profile into longitudinal, front-form and backside regions.

    The first result is reachable by ordinary negative-Z longitudinal turning.
    The optional second result contains a leading form separated by a steep
    radial face. The optional third result is a backside/alternate-direction
    candidate. Callers must not silently include either candidate in the
    longitudinal operation.
    """
    base = suppress_external_grooves(profile)
    if base.side != "outer":
        return base, None, None
    ordered = sorted(base.points, key=lambda item: item.z, reverse=True)
    minimum_z = min(point.z for point in base.points)
    maximum_z = max(point.z for point in base.points)

    longitudinal_maximum_z = maximum_z
    front_form: RotationalProfile | None = None
    for left, right in zip(ordered, ordered[1:]):
        radial_change = right.radius - left.radius
        if radial_change < -1e-6:
            break
        axial_change = left.z - right.z
        if radial_change > 0.1 and radial_change > axial_change * 1.5:
            longitudinal_maximum_z = right.z
            if maximum_z - right.z > 1e-6:
                front_form = clip_rotational_profile(base, right.z, maximum_z)
            break

    longitudinal_source = (
        clip_rotational_profile(base, minimum_z, longitudinal_maximum_z)
        if longitudinal_maximum_z < maximum_z - 1e-6
        else base
    )
    ordered = sorted(longitudinal_source.points, key=lambda item: item.z, reverse=True)
    entered_reduced_diameter = False
    split_z: float | None = None
    for left, right in zip(ordered, ordered[1:]):
        if right.radius < left.radius - 1e-6:
            entered_reduced_diameter = True
        elif entered_reduced_diameter and right.radius > left.radius + 1e-6:
            split_z = left.z
            break
    if split_z is None:
        return longitudinal_source, front_form, None
    return (
        clip_rotational_profile(longitudinal_source, split_z, longitudinal_maximum_z),
        front_form,
        clip_rotational_profile(longitudinal_source, minimum_z, split_z),
    )


def bind_thread_requirements(
    rotational: RotationalFeatureAnalysis,
    requirements: ManufacturingRequirements | None,
) -> RotationalFeatureAnalysis:
    """Bind periodic modelled tooth forms only when one verified drawing thread matches."""
    if requirements is None:
        return rotational
    result = rotational.model_copy(deep=True)
    thread_forms = [item for item in result.features if item.kind == "thread_form_candidate"]
    drawing_threads = [
        item for item in requirements.requirements
        if item.type == "thread"
        and item.thread is not None
        and item.thread.side in {"external", "unknown"}
    ]
    for requirement in drawing_threads:
        assert requirement.thread is not None
        pitch = requirement.thread.pitch_mm
        candidates = [
            feature for feature in thread_forms
            if any(
                abs(candidate - pitch) <= max(pitch * 0.02, 0.02)
                for candidate in feature.pitch_candidates_mm
            )
            and abs(
                (max(feature.radius_start, feature.radius_end) + feature.depth_mm) * 2
                - requirement.thread.major_diameter_mm
            ) <= max(pitch * 0.75, 0.3)
        ]
        verified = (
            requirement.mapping_status == "matched"
            and requirement.verification_status.startswith("verified")
        )
        if len(candidates) == 1 and verified:
            feature = candidates[0]
            feature.binding_state = "matched"
            feature.drawing_requirement_ids = [requirement.id]
            feature.resolved_pitch_mm = pitch
            feature.resolved_major_diameter_mm = requirement.thread.major_diameter_mm
            feature.thread_side = requirement.thread.side
            feature.thread_form_angle_degrees = requirement.thread.form_angle_degrees
            feature.thread_designation = requirement.thread.designation
            feature.confidence = min(feature.confidence, requirement.confidence)
            feature.review_reasons.append(
                "周期齿形已与唯一图纸螺纹要求匹配；起止位置、退刀槽和控制器循环仍需审核。"
            )
        elif candidates:
            for feature in candidates:
                feature.binding_state = "ambiguous"
                if requirement.id not in feature.drawing_requirement_ids:
                    feature.drawing_requirement_ids.append(requirement.id)
                feature.thread_designation = requirement.thread.designation
                feature.review_reasons.append(
                    "图纸螺纹要求尚未验证，或同一螺距匹配到多个周期齿形区；必须人工确认。"
                )
    return result


def infer_rotational_features(analysis: GeometryAnalysis) -> RotationalFeatureAnalysis:
    source_solid_count = _source_solid_count(analysis)
    if source_solid_count > 1:
        return RotationalFeatureAnalysis(
            source_file=analysis.source_file,
            status="solid_selection_required",
            evidence={
                "source_solid_count": source_solid_count,
                "decision_reasons": ["输入包含多个实体，尚未确认实际加工对象"],
            },
            warnings=["输入包含多个实体；确认目标实体前禁止生成 L32 工艺和刀路。"],
        )

    candidates = [item for item in analysis.cylindrical_features if _candidate_score(item) > 0]
    if not candidates:
        return RotationalFeatureAnalysis(
            source_file=analysis.source_file,
            status="not_detected",
            evidence={"source_solid_count": source_solid_count},
            warnings=["未找到可用于建立回转基准的圆柱特征"],
        )

    reference = max(candidates, key=_candidate_score)
    reference_direction = _normalize_direction(reference.axis)
    section_candidates: list[tuple[float, RotationalSectionCandidate, CylindricalFeature]] = []
    for section in analysis.rotational_sections:
        if len(section.outer_profile) < 2:
            continue
        source = next(
            (
                item for item in analysis.cylindrical_features
                if item.id == section.source_feature_id
                or section.source_feature_id in item.source_face_ids
            ),
            None,
        )
        if source is None or source.kind not in {"boss", "cylinder"}:
            continue
        section_direction = _normalize_direction(section.axis)
        if abs(_dot(section_direction, reference_direction)) < 0.98:
            continue
        axial_span = max(item.z for item in section.outer_profile) - min(
            item.z for item in section.outer_profile
        )
        if source.review_state == "excluded" and axial_span + 1e-6 < reference.length:
            continue
        radial_extent = max(item.radius for item in section.outer_profile)
        section_candidates.append((axial_span * radial_extent, section, source))
    exact_section = None
    if section_candidates:
        _, exact_section, reference = max(section_candidates, key=lambda item: item[0])
    direction = _normalize_direction(exact_section.axis if exact_section else reference.axis)
    section_axis_reversed = bool(
        exact_section is not None and _dot(exact_section.axis, direction) < 0
    )
    axis_origin = exact_section.axis_origin if exact_section else reference.center
    axis = RotationalAxisCandidate(
        id="RA-1",
        origin=axis_origin,
        direction=direction,
        confidence=min(reference.confidence, 0.85),
        source_feature_ids=[reference.id],
        review_state="review",
        review_reasons=["回转轴由圆柱特征推断，生成车削刀路前必须人工确认"],
    )
    projected_profile = (
        [] if exact_section else _edge_projection_profile(analysis, reference.center, direction)
    )
    start = -reference.length / 2
    end = reference.length / 2
    if exact_section:
        profile_points = sorted(
            (
                RotationalProfilePoint(
                    z=-item.z if section_axis_reversed else item.z,
                    radius=item.radius,
                )
                for item in exact_section.outer_profile
            ),
            key=lambda item: item.z,
        )
    else:
        profile_points = projected_profile or [
            RotationalProfilePoint(z=start, radius=reference.radius),
            RotationalProfilePoint(z=end, radius=reference.radius),
        ]
    projection_used = len(projected_profile) >= 2
    extraction_method = (
        "exact_section" if exact_section else
        "edge_projection_envelope" if projection_used else
        "bounding_cylinder"
    )
    bounds = analysis.measurements.get("bounding_box")
    evidence: dict[str, float | int | str | list[str]] = {
        "source_solid_count": source_solid_count,
        "reference_feature_id": reference.id,
        "profile_extraction_method": extraction_method,
        "profile_axis_reversed_from_section": "true" if section_axis_reversed else "false",
    }
    rejection_reasons: list[str] = []
    review_reasons: list[str] = []
    if isinstance(bounds, Bounds):
        transverse_a, transverse_b = _bbox_transverse_extents(bounds, direction)
        larger_transverse = max(transverse_a, transverse_b, 1e-9)
        aspect_ratio = min(transverse_a, transverse_b) / larger_transverse
        transverse_diagonal = sqrt(transverse_a * transverse_a + transverse_b * transverse_b)
        profile_diameter = 2 * max(point.radius for point in profile_points)
        diameter_diagonal_ratio = profile_diameter / max(transverse_diagonal, 1e-9)
        evidence.update({
            "transverse_extent_a_mm": round(transverse_a, 6),
            "transverse_extent_b_mm": round(transverse_b, 6),
            "transverse_aspect_ratio": round(aspect_ratio, 6),
            "profile_diameter_mm": round(profile_diameter, 6),
            "profile_to_transverse_diagonal_ratio": round(diameter_diagonal_ratio, 6),
        })
        if aspect_ratio < 0.45:
            rejection_reasons.append(
                f"候选轴横截面长宽比仅 {aspect_ratio:.3f}，主体更接近板件或偏置结构"
            )
        elif aspect_ratio < 0.65:
            review_reasons.append(
                f"候选轴横截面长宽比为 {aspect_ratio:.3f}，需确认铣平或非轴对称局部特征"
            )
        if diameter_diagonal_ratio > 1.1:
            rejection_reasons.append(
                f"投影轮廓直径超过横截面包络对角线 {diameter_diagonal_ratio:.3f} 倍，候选轴明显偏离主体"
            )
    evidence["decision_reasons"] = [*rejection_reasons, *review_reasons]
    if rejection_reasons:
        return RotationalFeatureAnalysis(
            source_file=analysis.source_file,
            status="not_rotational",
            evidence=evidence,
            warnings=[*rejection_reasons, "该零件未通过 L32 主体回转性门禁，不生成回转轮廓。"],
        )

    outer_profile = RotationalProfile(
        id="RP-OUTER-1",
        axis_id=axis.id,
        side="outer",
        extraction_method=extraction_method,
        points=profile_points,
        confidence=min(
            reference.confidence,
            0.8 if exact_section else 0.72 if projection_used else 0.6,
        ),
        review_state=analysis.rotational_profile_reviews.get("RP-OUTER-1", "review"),
        review_reasons=[
            "轮廓由可视边投影得到，必须排除内孔、横向孔和非回转特征后人工确认"
            if projection_used
            else "当前为圆柱包络候选，不代表阶梯、槽、锥面和圆弧的精确成品轮廓"
        ],
    )
    if exact_section:
        outer_profile.review_reasons = [
            "轮廓来自 OCCT 精确平面截线的外包络，生成刀路前仍须确认回转轴、截面方向和非回转特征。"
        ]
        outer_profile, envelope_repairs, rejected_repairs = _repair_outer_profile_with_axial_holes(
            analysis,
            outer_profile,
            axis.origin,
            axis.direction,
            exact_section.axial_coordinate_system,
        )
        if envelope_repairs:
            evidence["outer_envelope_repair_count"] = len(envelope_repairs)
            evidence["outer_envelope_repaired_feature_ids"] = [
                str(item["feature_id"]) for item in envelope_repairs
            ]
            evidence["outer_envelope_repairs"] = [
                json_value
                for item in envelope_repairs
                for json_value in [
                    f"{item['feature_id']}:{float(item['z_min_mm']):.6f}:"
                    f"{float(item['z_max_mm']):.6f}:{float(item['required_radius_mm']):.6f}"
                ]
            ]
            status_warnings_prefix = [
                "外回转包络已依据完整轴向孔的几何下限生成修复候选，必须重新人工确认。"
            ]
        else:
            status_warnings_prefix = []
        if rejected_repairs:
            evidence["outer_envelope_unrepairable_feature_ids"] = rejected_repairs
    else:
        status_warnings_prefix = []
    profiles = [outer_profile]
    if exact_section and len(exact_section.inner_profile) >= 2:
        profiles.append(RotationalProfile(
            id="RP-INNER-1",
            axis_id=axis.id,
            side="inner",
            extraction_method="exact_section",
            points=sorted(
                (
                    RotationalProfilePoint(
                        z=-item.z if section_axis_reversed else item.z,
                        radius=item.radius,
                    )
                    for item in exact_section.inner_profile
                ),
                key=lambda item: item.z,
            ),
            confidence=min(reference.confidence, 0.65),
            review_state=analysis.rotational_profile_reviews.get("RP-INNER-1", "review"),
            review_reasons=[
                "内轮廓截面可能混入槽或横向孔交线，当前仅供审核，不自动生成内孔工序。",
                *exact_section.warnings,
            ],
        ))
    status_warnings = [
        *status_warnings_prefix,
        "OCCT 精确截面已接入；当前结果仍保持人工审核状态，尚未直接驱动生产刀路。"
        if exact_section
        else (
            "当前 Z-R 轮廓来自 B-Rep 可视边投影，只能用于人工确认和刀路原型。"
            if projection_used
            else "未取得精确 Z-R 截面；该圆柱包络结果只能用于人工确认。"
        ),
        *(exact_section.warnings if exact_section else []),
        *review_reasons,
    ]
    turning_features = [
        feature
        for candidate_profile in profiles
        for feature in _extract_profile_features(candidate_profile)
    ]
    return RotationalFeatureAnalysis(
        source_file=analysis.source_file,
        axes=[axis],
        profiles=profiles,
        features=turning_features,
        status="candidate",
        evidence=evidence,
            warnings=status_warnings or [
                "当前 Z-R 轮廓来自 B-Rep 可视边投影，尚不是 OCCT 精确截面；只能用于人工确认和刀路原型"
                if projection_used
                else "精确 Z-R 截面算法尚未接入；该结果只能用于人工确认和后续识别开发",
                *review_reasons,
            ],
    )
