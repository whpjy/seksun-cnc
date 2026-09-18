from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .machine_models import MachineConfigurationSnapshot
from .models import Operation
from .rotational_features import (
    RotationalProfile, RotationalProfilePoint,
    clip_rotational_profile, suppress_external_grooves,
)
from .turning_draft import TurningDraftRequest, TurningDraftResult, compile_turning_draft


class BacksideCoordinateTransform(BaseModel):
    schema_version: str = "1.0.0"
    source_frame: Literal["main_spindle"] = "main_spindle"
    target_frame: Literal["sub_spindle"] = "sub_spindle"
    source_cutoff_z_mm: float
    target_datum_z_mm: float = 0.0
    z_scale: Literal[-1] = -1
    radial_scale: Literal[1] = 1


class BacksideDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    source_profile_id: str = Field(min_length=1)
    operation: Operation
    source_cutoff_z_mm: float
    stock_radius_mm: float = Field(gt=0)
    resolution_mm: float = Field(default=0.1, gt=0, le=2)
    radial_clearance_mm: float = Field(default=2, gt=0, le=20)
    axial_clearance_mm: float = Field(default=2, gt=0, le=20)

    @model_validator(mode="after")
    def validate_back_operation(self) -> "BacksideDraftRequest":
        if self.operation.type not in {"turn_facing", "turn_od_roughing", "turn_od_finishing"}:
            raise ValueError("backside draft currently supports facing and OD contour turning")
        if self.operation.channel_id != "sub" or self.operation.spindle_id != "sub":
            raise ValueError("backside operation must be assigned to the sub channel and sub spindle")
        if self.operation.workpiece_side != "back":
            raise ValueError("backside operation must declare workpiece_side=back")
        return self


class BacksideDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    transform: BacksideCoordinateTransform
    derived_profile: RotationalProfile
    draft: TurningDraftResult
    warnings: list[str] = Field(default_factory=list)


def derive_backside_profile(
    source: RotationalProfile,
    source_cutoff_z_mm: float,
    *,
    cleanup_length_mm: float | None = None,
) -> tuple[BacksideCoordinateTransform, RotationalProfile]:
    minimum_z = min(point.z for point in source.points)
    maximum_z = max(point.z for point in source.points)
    if source_cutoff_z_mm < minimum_z - 1e-6 or source_cutoff_z_mm > maximum_z + 1e-6:
        raise ValueError("source cutoff Z lies outside the accepted rotational profile")
    transform = BacksideCoordinateTransform(source_cutoff_z_mm=source_cutoff_z_mm)
    converted = [
        RotationalProfilePoint(
            z=round(source_cutoff_z_mm - point.z, 6),
            radius=point.radius,
        )
        for point in source.points
    ]
    converted.sort(key=lambda point: (point.z, point.radius))
    if cleanup_length_mm is not None:
        if cleanup_length_mm <= 0:
            raise ValueError("back cleanup length must be positive")
        lower_z = -cleanup_length_mm
        local = [point for point in converted if lower_z - 1e-9 <= point.z <= 1e-9]
        if not local:
            nearest = min(converted, key=lambda point: abs(point.z))
            local = [RotationalProfilePoint(z=lower_z, radius=nearest.radius), RotationalProfilePoint(z=0, radius=nearest.radius)]
        elif len(local) == 1:
            local.insert(0, RotationalProfilePoint(z=lower_z, radius=local[0].radius))
        converted = local
    return transform, RotationalProfile(
        id=f"{source.id}-BACK",
        axis_id=f"{source.axis_id}-SUB",
        side=source.side,
        extraction_method=source.extraction_method,
        points=converted,
        confidence=source.confidence,
        review_state=source.review_state,
        review_reasons=[*source.review_reasons, "由已确认正面轮廓通过背轴坐标变换派生"],
    )


def compile_backside_draft(
    job_id: str,
    request: BacksideDraftRequest,
    source_profile: RotationalProfile,
    snapshot: MachineConfigurationSnapshot,
) -> BacksideDraftResult:
    if "back_turning" not in snapshot.validation.capabilities:
        raise ValueError("machine configuration lacks capability: back_turning")
    region_minimum = request.operation.parameters.get("source_region_z_min_mm")
    region_maximum = request.operation.parameters.get("source_region_z_max_mm")
    if (region_minimum is None) != (region_maximum is None):
        raise ValueError("backside source region requires both Z bounds")
    regional = region_minimum is not None
    if regional:
        if request.operation.type not in {"turn_od_roughing", "turn_od_finishing"}:
            raise ValueError("backside source region requires an OD contour operation")
        if request.operation.parameters.get("cut_direction", "negative_z") != "negative_z":
            raise ValueError("backside region must feed into the part in negative sub-spindle Z")
        if abs(float(region_minimum) - request.source_cutoff_z_mm) > 1e-6:
            raise ValueError("backside source region must begin at the cutoff datum")
        base_profile = suppress_external_grooves(source_profile)
        source_region = clip_rotational_profile(
            base_profile, float(region_minimum), float(region_maximum),
        )
    else:
        source_region = source_profile
    cleanup_length = (
        float(request.operation.parameters.get("back_cleanup_length_mm", 1.0))
        if request.operation.type == "turn_od_finishing" and not regional
        else None
    )
    transform, profile = derive_backside_profile(
        source_region, request.source_cutoff_z_mm,
        cleanup_length_mm=cleanup_length,
    )
    if profile.id not in request.operation.feature_ids:
        raise ValueError("backside operation does not reference the derived profile")
    z_values = [point.z for point in profile.points]
    draft_request = TurningDraftRequest(
        machine_instance_id=request.machine_instance_id,
        operation=request.operation,
        profile=profile,
        stock_radius_mm=request.stock_radius_mm,
        z_min_mm=min(z_values) - request.axial_clearance_mm,
        z_max_mm=max(z_values) + request.axial_clearance_mm,
        resolution_mm=request.resolution_mm,
        radial_clearance_mm=request.radial_clearance_mm,
        axial_clearance_mm=request.axial_clearance_mm,
    )
    draft = compile_turning_draft(job_id, draft_request, snapshot)
    return BacksideDraftResult(
        transform=transform,
        derived_profile=profile,
        draft=draft,
        warnings=[
            "背轴 Z 坐标由切断平面自动换算；实际机床工件坐标偏置必须通过对刀和首件确认。",
            "背面刀具安装方向、夹头伸出量及背轴退出路径尚需机床级碰撞验证。",
        ],
    )
