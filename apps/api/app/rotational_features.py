from __future__ import annotations

from itertools import product
from math import sqrt
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import Bounds, CylindricalFeature, GeometryAnalysis, ManufacturingRequirements, Vec3


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
        "external_groove_candidate", "thread_form_candidate",
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
    confidence = min(profile.confidence, 0.72 if profile.extraction_method == "exact_section" else 0.5)
    result: list[TurningProfileFeature] = []

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
            else:
                kind = "cylindrical_land"
                reasons = ["恒定外半径段来自截面包络，需与图纸尺寸绑定。"]
                previous_radius = points[index - 2].radius if index >= 2 else start.radius
                following_radius = points[index + 1].radius if index + 1 < len(points) else end.radius
                surrounding_radius = min(previous_radius, following_radius)
                if surrounding_radius - max(start.radius, end.radius) > radius_tolerance * 3:
                    kind = "external_groove_candidate"
                    depth = surrounding_radius - max(start.radius, end.radius)
                    reasons = ["低于两侧外径的轴向恒半径段，标记为外槽候选；槽宽、圆角和刀宽必须由图纸确认。"]
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
            and item.depth_mm >= max(maximum_radius * 0.005, 0.03)
            and item.width_mm >= 0.05
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
    exact_section = next(
        (
            section for section in analysis.rotational_sections
            if len(section.outer_profile) >= 2
            and any(
                item.id == section.source_feature_id
                or section.source_feature_id in item.source_face_ids
                for item in candidates
            )
        ),
        None,
    )
    if exact_section is not None:
        reference = next(
            item for item in candidates
            if item.id == exact_section.source_feature_id
            or exact_section.source_feature_id in item.source_face_ids
        )
    direction = _normalize_direction(exact_section.axis if exact_section else reference.axis)
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
        profile_points = [
            RotationalProfilePoint(z=item.z, radius=item.radius)
            for item in exact_section.outer_profile
        ]
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
    profiles = [outer_profile]
    if exact_section and len(exact_section.inner_profile) >= 2:
        profiles.append(RotationalProfile(
            id="RP-INNER-1",
            axis_id=axis.id,
            side="inner",
            extraction_method="exact_section",
            points=[
                RotationalProfilePoint(z=item.z, radius=item.radius)
                for item in exact_section.inner_profile
            ],
            confidence=min(reference.confidence, 0.65),
            review_state=analysis.rotational_profile_reviews.get("RP-INNER-1", "review"),
            review_reasons=[
                "内轮廓截面可能混入槽或横向孔交线，当前仅供审核，不自动生成内孔工序。",
                *exact_section.warnings,
            ],
        ))
    status_warnings = [
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
