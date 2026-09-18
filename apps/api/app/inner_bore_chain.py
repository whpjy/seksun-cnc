from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cam.providers.turning import TurningContext, TurningProvider

from .machine_models import MachineConfigurationSnapshot
from .models import Operation, ProcessPlan
from .rotational_features import RotationalProfile
from .toolpath_ir import ToolpathChannel, ToolpathProgram, toolpath_program_hash
from .turning_reachability import assess_turning_reachability
from .turning_simulation import TurningSimulationResult, simulate_turning_stock
from .turning_verification import TurningVerificationResult, verify_turning_profile


class InnerBoreChainRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    profile_id: str = Field(min_length=1)
    stock_radius_mm: float = Field(gt=0)
    z_min_mm: float
    z_max_mm: float
    resolution_mm: float = Field(default=0.2, gt=0, le=2)


class InnerBoreChainCheck(BaseModel):
    id: str
    status: Literal["passed", "failed"]
    message: str
    measured_value: float | int | str | None = None


class InnerBoreChainStage(BaseModel):
    sequence: int = Field(ge=1)
    operation_id: str
    operation_type: str
    command_count: int = Field(ge=1)
    initial_volume_mm3: float = Field(ge=0)
    final_volume_mm3: float = Field(ge=0)
    removed_volume_mm3: float = Field(ge=0)
    verification_status: Literal["passed", "warning", "failed"]


class InnerBoreChainResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    status: Literal["passed", "failed"]
    machine_instance_id: str
    machine_configuration_hash: str
    program_hash: str
    profile_id: str
    toolpath: ToolpathProgram
    stages: list[InnerBoreChainStage]
    final_simulation: TurningSimulationResult
    final_verification: TurningVerificationResult
    checks: list[InnerBoreChainCheck]
    warnings: list[str] = Field(default_factory=list)


def _reviewed(operation: Operation) -> bool:
    return (
        operation.enabled
        and operation.parameters.get("engineering_review_status") == "verified_engineer"
    )


def compile_inner_bore_chain(
    job_id: str,
    request: InnerBoreChainRequest,
    plan: ProcessPlan,
    profile: RotationalProfile,
    snapshot: MachineConfigurationSnapshot,
) -> InnerBoreChainResult:
    if request.machine_instance_id != snapshot.instance.id:
        raise ValueError("inner-bore chain does not use the bound machine instance")
    if request.profile_id != profile.id or profile.side != "inner":
        raise ValueError("inner-bore chain requires the selected inner profile")
    if profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise ValueError("inner-bore chain requires an accepted exact inner profile")
    if request.z_max_mm <= request.z_min_mm:
        raise ValueError("inner-bore chain simulation range is invalid")
    if any(point.z < request.z_min_mm or point.z > request.z_max_mm for point in profile.points):
        raise ValueError("inner-bore chain simulation range does not contain the profile")
    if request.stock_radius_mm * 2 > snapshot.instance.bar_diameter_mm + 1e-9:
        raise ValueError("inner-bore chain stock exceeds the machine bar configuration")

    candidates = [
        item for setup in plan.setups for item in setup.operations
        if profile.id in item.feature_ids
        and (
            item.type in {"axial_drilling", "turn_id_roughing", "turn_id_finishing"}
            or (item.type == "turn_grooving" and item.parameters.get("groove_side") == "internal")
        )
    ]
    drills = sorted(
        (item for item in candidates if item.type == "axial_drilling"),
        key=lambda item: int(item.parameters.get("drilling_stage_index", 1)),
    )
    rough = [item for item in candidates if item.type == "turn_id_roughing"]
    finish = [item for item in candidates if item.type == "turn_id_finishing"]
    internal_grooves = sorted(
        (
            item for item in candidates
            if item.type == "turn_grooving" and item.parameters.get("groove_side") == "internal"
        ),
        key=lambda item: item.id,
    )
    if not drills or len(rough) != 1 or len(finish) != 1:
        raise ValueError("inner-bore chain requires planned drilling, rough-boring, and finish-boring operations")
    operations = [*drills, rough[0], finish[0], *internal_grooves]
    unreviewed = [item.id for item in operations if not _reviewed(item)]
    if unreviewed:
        raise ValueError("inner-bore chain contains unreviewed operations: " + ", ".join(unreviewed))

    stage_indexes = [int(item.parameters.get("drilling_stage_index", 1)) for item in drills]
    if stage_indexes != list(range(1, len(drills) + 1)):
        raise ValueError("pre-bore stage indexes must be contiguous and start at one")
    drill_diameters = [item.tool.diameter_mm for item in drills]
    drill_depths = [float(item.parameters.get("full_diameter_depth_mm", 0)) for item in drills]
    ordered = all(right > left for left, right in zip(drill_diameters, drill_diameters[1:]))
    ordered = ordered and all(right < left for left, right in zip(drill_depths, drill_depths[1:]))

    primary_bore_radius = drill_diameters[0] / 2
    current_samples = None
    stage_results: list[InnerBoreChainStage] = []
    programs: list[ToolpathProgram] = []
    verifications: list[TurningVerificationResult] = []
    volume_nonincrease = True
    for sequence, operation in enumerate(operations, 1):
        context = TurningContext(
            machine_snapshot_hash=snapshot.configuration_hash,
            stock_radius_mm=request.stock_radius_mm,
            initial_bore_radius_mm=(
                primary_bore_radius
                if operation.type.startswith("turn_id_")
                or operation.parameters.get("groove_side") == "internal"
                else 0
            ),
            radial_clearance_mm=float(operation.parameters.get("assembly_clearance_mm", 0.2)),
        )
        reachability = assess_turning_reachability(operation, profile, context)
        if reachability.status == "failed":
            raise ValueError(
                f"{operation.id} reachability failed: " + "; ".join(reachability.blocking_reasons)
            )
        program = TurningProvider().generate(operation, context, profile)
        simulation = simulate_turning_stock(
            program,
            stock_radius_mm=request.stock_radius_mm,
            initial_bore_radius_mm=0,
            z_min_mm=request.z_min_mm,
            z_max_mm=request.z_max_mm,
            resolution_mm=request.resolution_mm,
            initial_samples=current_samples,
        )
        verification = verify_turning_profile(
            profile,
            simulation,
            tolerance_mm=max(0.05, float(operation.tool.nose_radius_mm or 0) * 0.35),
            expected_allowance_mm=float(operation.parameters.get("radial_allowance_mm", 0)),
        )
        if simulation.metrics.remaining_volume_mm3 > simulation.metrics.initial_volume_mm3 + 1e-6:
            volume_nonincrease = False
        stage_results.append(InnerBoreChainStage(
            sequence=sequence,
            operation_id=operation.id,
            operation_type=operation.type,
            command_count=len(program.channels[0].commands),
            initial_volume_mm3=simulation.metrics.initial_volume_mm3,
            final_volume_mm3=simulation.metrics.remaining_volume_mm3,
            removed_volume_mm3=simulation.metrics.removed_volume_mm3,
            verification_status=verification.status,
        ))
        programs.append(program)
        verifications.append(verification)
        current_samples = simulation.samples

    commands = []
    for program in programs:
        commands.extend(program.channels[0].commands)
    combined = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=max(item.definition_version for item in operations),
        channels=[ToolpathChannel(
            id="main",
            commands=[
                item.model_copy(update={"sequence": index, "channel_id": "main"})
                for index, item in enumerate(commands, 1)
            ],
        )],
    )
    final_simulation = simulation
    final_verification = verifications[-1]
    checks = [
        InnerBoreChainCheck(
            id="stage_order",
            status="passed" if ordered else "failed",
            message="阶梯预孔必须按深层小径到浅层大径排列",
            measured_value=" -> ".join(item.id for item in drills),
        ),
        InnerBoreChainCheck(
            id="material_volume_nonincrease",
            status="passed" if volume_nonincrease else "failed",
            message="连续工序不得凭空增加材料体积",
            measured_value=len(stage_results),
        ),
        InnerBoreChainCheck(
            id="intermediate_overcut",
            status="passed" if all(item.status != "failed" for item in verifications) else "failed",
            message="任一预孔、镗孔或内槽阶段不得越过已确认内轮廓",
            measured_value=sum(item.status == "failed" for item in verifications),
        ),
        InnerBoreChainCheck(
            id="final_profile",
            status="passed" if final_verification.status == "passed" else "failed",
            message="最后一道内孔/内槽工序后连续材料状态必须满足最终内轮廓",
            measured_value=final_verification.status,
        ),
    ]
    status = "failed" if any(item.status == "failed" for item in checks) else "passed"
    return InnerBoreChainResult(
        job_id=job_id,
        status=status,
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        program_hash=toolpath_program_hash(combined),
        profile_id=profile.id,
        toolpath=combined,
        stages=stage_results,
        final_simulation=final_simulation,
        final_verification=final_verification,
        checks=checks,
        warnings=[
            "内孔链采用轴对称 Z-R 连续余料模型，尚未替代三维刀杆、夹头和冷却液流场仿真。",
            "结果仅为 DRAFT；未生成生产 NC。",
        ],
    )
