from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from cam.providers.turning import TurningContext, TurningProvider

from .l32_configuration import L32_DEFINITION, snapshot_l32_instance
from .machine_models import MachineConfigurationSnapshot
from .models import Operation
from .rotational_features import RotationalProfile, clip_rotational_profile
from .toolpath_ir import ToolpathProgram
from .turning_simulation import TurningSimulationResult, simulate_turning_stock
from .turning_verification import TurningVerificationResult, verify_turning_profile
from .turning_reachability import TurningReachabilityResult, assess_turning_reachability
from .threading_verification import ThreadingVerificationResult, verify_threading_cycle


class TurningDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    operation: Operation
    profile: RotationalProfile | None = None
    stock_radius_mm: float = Field(gt=0)
    initial_bore_radius_mm: float = Field(default=0, ge=0)
    z_min_mm: float
    z_max_mm: float
    resolution_mm: float = Field(default=0.1, gt=0, le=2)
    radial_clearance_mm: float = Field(default=2, gt=0, le=20)
    axial_clearance_mm: float = Field(default=2, gt=0, le=20)

    @model_validator(mode="after")
    def validate_stock(self) -> "TurningDraftRequest":
        if self.z_max_mm <= self.z_min_mm:
            raise ValueError("z_max_mm must be greater than z_min_mm")
        if self.initial_bore_radius_mm >= self.stock_radius_mm:
            raise ValueError("initial bore radius must be smaller than stock radius")
        return self


class TurningDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    machine_instance_id: str
    machine_configuration_hash: str
    operation_id: str
    toolpath: ToolpathProgram
    simulation: TurningSimulationResult
    verification: TurningVerificationResult | None = None
    thread_verification: ThreadingVerificationResult | None = None
    reachability: TurningReachabilityResult | None = None
    warnings: list[str] = Field(default_factory=list)


_CAPABILITY_BY_OPERATION = {
    "turn_grooving": "grooving_cutoff",
    "turn_cutoff": "grooving_cutoff",
    "turn_threading": "threading_tapping",
    "axial_tapping": "threading_tapping",
    "axial_drilling": "axial_drilling",
}


def compile_turning_draft(
    job_id: str,
    request: TurningDraftRequest,
    snapshot: MachineConfigurationSnapshot,
) -> TurningDraftResult:
    if snapshot.instance.definition_id != L32_DEFINITION.id:
        raise ValueError("machine instance is not a Citizen Cincom L32")
    if snapshot.instance.id != request.machine_instance_id:
        raise ValueError("machine instance snapshot identity mismatch")
    if snapshot.definition_revision != L32_DEFINITION.source_revision:
        raise ValueError("machine instance snapshot uses an outdated L32 definition")
    current_snapshot = snapshot_l32_instance(snapshot.instance)
    if current_snapshot.configuration_hash != snapshot.configuration_hash:
        raise ValueError("machine instance snapshot integrity check failed")
    validation = current_snapshot.validation
    if not validation.valid:
        raise ValueError("machine instance configuration contains blocking issues")
    if not request.operation.enabled:
        raise ValueError("disabled operation cannot be generated")
    if request.profile is not None and request.profile.review_state != "accepted":
        raise ValueError("rotational profile must be accepted before draft generation")
    if request.profile is not None:
        if request.profile.id not in request.operation.feature_ids:
            raise ValueError("operation traceability does not reference the supplied profile")
        if any(
            point.z < request.z_min_mm - 1e-9 or point.z > request.z_max_mm + 1e-9
            for point in request.profile.points
        ):
            raise ValueError("simulation Z range does not contain the complete rotational profile")
    if request.stock_radius_mm * 2 > snapshot.instance.bar_diameter_mm + 1e-9:
        raise ValueError("stock diameter exceeds the configured L32 bar diameter")
    if request.z_max_mm - request.z_min_mm > L32_DEFINITION.maximum_length_per_chucking_mm + 1e-9:
        raise ValueError("stock length exceeds the L32 maximum length per chucking")

    required_capability = _CAPABILITY_BY_OPERATION.get(request.operation.type, "turning")
    if required_capability not in validation.capabilities:
        raise ValueError(f"machine configuration lacks capability: {required_capability}")

    spindle_id = request.operation.spindle_id or str(request.operation.parameters.get("spindle_id", "main"))
    spindle = next((item for item in L32_DEFINITION.spindles if item.id == spindle_id), None)
    if spindle is None or spindle.role != "work":
        raise ValueError(f"unknown work spindle: {spindle_id}")
    if request.operation.channel_id and request.operation.channel_id != spindle.channel_id:
        raise ValueError("operation channel does not match its work spindle")
    requested_rpm = request.operation.parameters.get("maximum_spindle_rpm")
    if isinstance(requested_rpm, (int, float)) and not isinstance(requested_rpm, bool):
        rpm_limit = min(spindle.maximum_rpm, request.operation.tool.max_rpm)
        if float(requested_rpm) > rpm_limit:
            raise ValueError(f"maximum_spindle_rpm exceeds configured spindle/tool limit: {rpm_limit}")

    cut_direction = str(request.operation.parameters.get("cut_direction", "negative_z"))
    if cut_direction not in {"negative_z", "positive_z"}:
        raise ValueError("turning operation has an invalid cut direction")
    if request.operation.type == "turn_od_finishing":
        maximum_nose_radius = request.operation.parameters.get("maximum_finish_nose_radius_mm")
        if maximum_nose_radius is not None and (
            isinstance(maximum_nose_radius, bool)
            or not isinstance(maximum_nose_radius, (int, float))
            or float(request.operation.tool.nose_radius_mm or 0) > float(maximum_nose_radius) + 1e-9
        ):
            raise ValueError("OD finishing tool exceeds the permitted nose radius")
    operation_profile = request.profile
    if request.profile is not None and (
        "profile_z_min_mm" in request.operation.parameters
        or "profile_z_max_mm" in request.operation.parameters
    ):
        operation_profile = clip_rotational_profile(
            request.profile,
            float(request.operation.parameters.get(
                "profile_z_min_mm", min(point.z for point in request.profile.points),
            )),
            float(request.operation.parameters.get(
                "profile_z_max_mm", max(point.z for point in request.profile.points),
            )),
        )

    context = TurningContext(
        machine_snapshot_hash=snapshot.configuration_hash,
        stock_radius_mm=request.stock_radius_mm,
        initial_bore_radius_mm=request.initial_bore_radius_mm,
        radial_clearance_mm=request.radial_clearance_mm,
        axial_clearance_mm=request.axial_clearance_mm,
        channel_id=spindle.channel_id,
        cut_direction=cut_direction,
    )
    reachability = None
    if operation_profile is not None:
        reachability = assess_turning_reachability(request.operation, operation_profile, context)
        if reachability.status == "failed":
            raise ValueError("turning tool is not reachable: " + "; ".join(reachability.blocking_reasons))
    toolpath = TurningProvider().generate(request.operation, context, operation_profile)
    simulation = simulate_turning_stock(
        toolpath,
        stock_radius_mm=request.stock_radius_mm,
        initial_bore_radius_mm=request.initial_bore_radius_mm,
        z_min_mm=request.z_min_mm,
        z_max_mm=request.z_max_mm,
        resolution_mm=request.resolution_mm,
    )
    verification = None
    thread_verification = None
    if request.operation.type == "turn_threading":
        thread_verification = verify_threading_cycle(request.operation, toolpath, simulation)
        if thread_verification.status == "failed":
            raise ValueError("threading DRAFT verification failed")
    elif operation_profile is not None and request.operation.type in {
        "turn_od_roughing", "turn_od_finishing",
        "turn_id_roughing", "turn_id_finishing",
        "turn_grooving",
    }:
        verification = verify_turning_profile(
            operation_profile,
            simulation,
            tolerance_mm=0.05,
            expected_allowance_mm=float(request.operation.parameters.get("radial_allowance_mm", 0)),
        )
        if (
            request.operation.parameters.get("profile_region_complete") is False
            and verification.status == "failed"
        ):
            raise ValueError("regional turning DRAFT verification failed: profile overcut")
    warnings = [
        "仅供 CAM 适配验证，未生成 NC，禁止直接用于机床生产。",
        "MELDAS/CINCOM 后处理器、通道同步、刀具补偿和机床碰撞尚未认证。",
    ]
    if not validation.production_ready:
        warnings.append("当前机床实例未达到生产发布条件。")
    return TurningDraftResult(
        job_id=job_id,
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        operation_id=request.operation.id,
        toolpath=toolpath,
        simulation=simulation,
        verification=verification,
        thread_verification=thread_verification,
        reachability=reachability,
        warnings=warnings,
    )
