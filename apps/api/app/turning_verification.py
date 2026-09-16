from __future__ import annotations

from math import pi
from typing import Literal

from pydantic import BaseModel, Field

from .rotational_features import RotationalProfile
from .turning_simulation import TurningSimulationResult


class TurningDeviationSample(BaseModel):
    z: float
    target_radius_mm: float = Field(ge=0)
    actual_radius_mm: float = Field(ge=0)
    deviation_mm: float
    kind: Literal["overcut", "excess_stock"]


class TurningVerificationMetrics(BaseModel):
    evaluated_sample_count: int = Field(ge=0)
    overcut_sample_count: int = Field(ge=0)
    excess_stock_sample_count: int = Field(ge=0)
    maximum_overcut_mm: float = Field(ge=0)
    maximum_excess_stock_mm: float = Field(ge=0)
    estimated_overcut_volume_mm3: float = Field(ge=0)
    estimated_excess_stock_volume_mm3: float = Field(ge=0)


class TurningVerificationResult(BaseModel):
    schema_version: str = "1.0.0"
    engine: str = "Seksun CNC Z-R profile verification"
    status: Literal["passed", "warning", "failed"]
    profile_id: str
    profile_side: Literal["outer", "inner"]
    tolerance_mm: float = Field(gt=0)
    expected_allowance_mm: float = Field(ge=0)
    metrics: TurningVerificationMetrics
    deviations: list[TurningDeviationSample] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _profile_nodes(profile: RotationalProfile) -> list[tuple[float, float]]:
    grouped: list[tuple[float, list[float]]] = []
    for point in profile.points:
        if grouped and abs(grouped[-1][0] - point.z) <= 1e-9:
            grouped[-1][1].append(point.radius)
        else:
            grouped.append((point.z, [point.radius]))
    # A radial shoulder has two radii at the same axial plane. The plane has
    # zero volume; selecting the material-side radius avoids false overcut at
    # the one discrete sample that lands exactly on it.
    return [
        (z_value, max(radii) if profile.side == "outer" else min(radii))
        for z_value, radii in grouped
    ]


def _interpolate(nodes: list[tuple[float, float]], z_value: float) -> float:
    if z_value <= nodes[0][0]:
        return nodes[0][1]
    if z_value >= nodes[-1][0]:
        return nodes[-1][1]
    for left, right in zip(nodes, nodes[1:]):
        if left[0] <= z_value <= right[0]:
            span = right[0] - left[0]
            if span <= 1e-12:
                return right[1]
            fraction = (z_value - left[0]) / span
            return left[1] + fraction * (right[1] - left[1])
    return nodes[-1][1]


def verify_turning_profile(
    profile: RotationalProfile,
    simulation: TurningSimulationResult,
    *,
    tolerance_mm: float = 0.05,
    expected_allowance_mm: float = 0,
) -> TurningVerificationResult:
    if profile.review_state != "accepted":
        raise ValueError("turning verification requires an accepted rotational profile")
    if tolerance_mm <= 0 or expected_allowance_mm < 0:
        raise ValueError("invalid turning verification tolerance or allowance")

    nodes = _profile_nodes(profile)
    z_min, z_max = nodes[0][0], nodes[-1][0]
    evaluated: list[tuple[float, float, float]] = []
    for sample in simulation.samples:
        if sample.z < z_min - 1e-9 or sample.z > z_max + 1e-9:
            continue
        target = _interpolate(nodes, sample.z)
        actual = sample.outer_radius if profile.side == "outer" else sample.inner_radius
        evaluated.append((sample.z, target, actual))

    deviations: list[TurningDeviationSample] = []
    maximum_overcut = 0.0
    maximum_excess = 0.0
    overcut_areas: list[float] = []
    excess_areas: list[float] = []
    for z_value, target, actual in evaluated:
        if profile.side == "outer":
            overcut = max(target - actual, 0)
            allowed_radius = target + expected_allowance_mm
            excess = max(actual - allowed_radius, 0)
            overcut_area = pi * max(target * target - actual * actual, 0)
            excess_area = pi * max(actual * actual - allowed_radius * allowed_radius, 0)
        else:
            overcut = max(actual - target, 0)
            allowed_radius = max(target - expected_allowance_mm, 0)
            excess = max(allowed_radius - actual, 0)
            overcut_area = pi * max(actual * actual - target * target, 0)
            excess_area = pi * max(allowed_radius * allowed_radius - actual * actual, 0)
        maximum_overcut = max(maximum_overcut, overcut)
        maximum_excess = max(maximum_excess, excess)
        overcut_areas.append(overcut_area if overcut > tolerance_mm else 0)
        excess_areas.append(excess_area if excess > tolerance_mm else 0)
        if overcut > tolerance_mm:
            deviations.append(TurningDeviationSample(
                z=z_value, target_radius_mm=target, actual_radius_mm=actual,
                deviation_mm=-overcut, kind="overcut",
            ))
        elif excess > tolerance_mm:
            deviations.append(TurningDeviationSample(
                z=z_value, target_radius_mm=target, actual_radius_mm=actual,
                deviation_mm=excess, kind="excess_stock",
            ))

    def integrate(areas: list[float]) -> float:
        if len(evaluated) < 2:
            return 0
        return sum(
            (areas[index] + areas[index + 1]) * 0.5 * (evaluated[index + 1][0] - evaluated[index][0])
            for index in range(len(evaluated) - 1)
        )

    overcut_count = sum(item.kind == "overcut" for item in deviations)
    excess_count = sum(item.kind == "excess_stock" for item in deviations)
    status = "failed" if overcut_count else "warning" if excess_count else "passed"
    return TurningVerificationResult(
        status=status,
        profile_id=profile.id,
        profile_side=profile.side,
        tolerance_mm=tolerance_mm,
        expected_allowance_mm=expected_allowance_mm,
        metrics=TurningVerificationMetrics(
            evaluated_sample_count=len(evaluated),
            overcut_sample_count=overcut_count,
            excess_stock_sample_count=excess_count,
            maximum_overcut_mm=round(maximum_overcut, 6),
            maximum_excess_stock_mm=round(maximum_excess, 6),
            estimated_overcut_volume_mm3=round(integrate(overcut_areas), 6),
            estimated_excess_stock_volume_mm3=round(integrate(excess_areas), 6),
        ),
        deviations=deviations,
        warnings=[
            "精车已采用圆形刀尖扫掠，但刀片象限、刀杆姿态和机床级干涉尚未验证，不作为生产放行依据。"
            if simulation.approximation == "mixed_centerline_and_nose_circle"
            else "当前验证基于刀具中心线 Z-R 近似，不作为生产放行依据。"
        ],
    )
