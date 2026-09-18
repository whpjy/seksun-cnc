from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .machine_models import MachineConfigurationSnapshot
from .models import ProcessPlan
from .rotational_features import (
    RotationalProfile, clip_rotational_profile, split_outer_profile_for_longitudinal_turning,
)
from .toolpath_ir import ToolpathChannel, ToolpathProgram, toolpath_program_hash
from .turning_draft import TurningDraftRequest, compile_turning_draft
from .turning_simulation import TurningSimulationResult, simulate_turning_stock
from .turning_verification import TurningVerificationResult, verify_turning_profile


class FrontChainDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    source_profile_id: str = Field(min_length=1)
    stock_radius_mm: float = Field(gt=0)
    resolution_mm: float = Field(default=0.1, gt=0, le=2)


class FrontChainStage(BaseModel):
    operation_id: str
    command_count: int = Field(ge=1)
    initial_volume_mm3: float = Field(ge=0)
    final_volume_mm3: float = Field(ge=0)
    removed_volume_mm3: float = Field(ge=0)
    verification_status: Literal["passed", "warning", "failed"]


class FrontChainCheck(BaseModel):
    id: str
    status: Literal["passed", "failed"]
    message: str
    measured_value: float | int | str | None = None


class FrontChainDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    status: Literal["passed", "failed"]
    machine_instance_id: str
    machine_configuration_hash: str
    source_profile_id: str
    program_hash: str
    toolpath: ToolpathProgram
    stages: list[FrontChainStage]
    final_simulation: TurningSimulationResult
    longitudinal_verification: TurningVerificationResult
    front_form_verification: TurningVerificationResult
    checks: list[FrontChainCheck]
    warnings: list[str] = Field(default_factory=list)


def compile_front_chain_draft(
    job_id: str,
    request: FrontChainDraftRequest,
    plan: ProcessPlan,
    source_profile: RotationalProfile,
    snapshot: MachineConfigurationSnapshot,
) -> FrontChainDraftResult:
    if request.machine_instance_id != snapshot.instance.id:
        raise ValueError("front chain does not use the bound machine instance")
    if (
        source_profile.id != request.source_profile_id
        or source_profile.side != "outer"
        or source_profile.extraction_method != "exact_section"
        or source_profile.review_state != "accepted"
    ):
        raise ValueError("front chain requires the accepted exact outer profile")
    if plan.stock.get("rotational_profile_id") != source_profile.id:
        raise ValueError("front chain source profile does not match the formal plan")
    longitudinal, front_form, _ = split_outer_profile_for_longitudinal_turning(source_profile)
    if front_form is None:
        raise ValueError("accepted source profile has no separate front-form region")

    operations = {item.id: item for setup in plan.setups for item in setup.operations}
    ids = ("OP20", "OP21-FORM", "OP30", "OP31-FORM")
    if any(operation_id not in operations for operation_id in ids):
        raise ValueError("front chain requires planned OP20/OP21-FORM/OP30/OP31-FORM operations")
    if any(not operations[operation_id].enabled for operation_id in ids):
        raise ValueError("front chain contains disabled operations")
    expected = {
        "OP20": ("turn_od_roughing", longitudinal),
        "OP21-FORM": ("turn_od_roughing", front_form),
        "OP30": ("turn_od_finishing", longitudinal),
        "OP31-FORM": ("turn_od_finishing", front_form),
    }
    for operation_id, (operation_type, region) in expected.items():
        operation = operations[operation_id]
        if operation.type != operation_type or operation.workpiece_side != "front":
            raise ValueError(f"{operation_id} does not match the formal front sequence")
        minimum = operation.parameters.get("profile_z_min_mm")
        maximum = operation.parameters.get("profile_z_max_mm")
        if minimum is None or maximum is None or (
            abs(float(minimum) - min(point.z for point in region.points)) > 1e-6
            or abs(float(maximum) - max(point.z for point in region.points)) > 1e-6
        ):
            raise ValueError(f"{operation_id} region does not match the accepted source profile")

    z_min = min(point.z for point in source_profile.points) - 2
    z_max = max(point.z for point in source_profile.points) + 2
    stock = None
    stages: list[FrontChainStage] = []
    programs: list[ToolpathProgram] = []
    verifications: dict[str, TurningVerificationResult] = {}
    simulations: list[TurningSimulationResult] = []
    for operation_id in ids:
        operation = operations[operation_id]
        draft = compile_turning_draft(
            job_id,
            TurningDraftRequest(
                machine_instance_id=request.machine_instance_id,
                operation=operation,
                profile=source_profile,
                stock_radius_mm=request.stock_radius_mm,
                z_min_mm=z_min,
                z_max_mm=z_max,
                resolution_mm=request.resolution_mm,
            ),
            snapshot,
        )
        simulation = simulate_turning_stock(
            draft.toolpath,
            stock_radius_mm=request.stock_radius_mm,
            z_min_mm=z_min,
            z_max_mm=z_max,
            resolution_mm=request.resolution_mm,
            initial_samples=stock,
        )
        target = clip_rotational_profile(
            source_profile,
            float(operation.parameters["profile_z_min_mm"]),
            float(operation.parameters["profile_z_max_mm"]),
        )
        verification = verify_turning_profile(
            target, simulation,
            tolerance_mm=0.05,
            expected_allowance_mm=float(operation.parameters.get("radial_allowance_mm", 0)),
        )
        stages.append(FrontChainStage(
            operation_id=operation_id,
            command_count=len(draft.toolpath.channels[0].commands),
            initial_volume_mm3=simulation.metrics.initial_volume_mm3,
            final_volume_mm3=simulation.metrics.remaining_volume_mm3,
            removed_volume_mm3=simulation.metrics.removed_volume_mm3,
            verification_status=verification.status,
        ))
        programs.append(draft.toolpath)
        simulations.append(simulation)
        verifications[operation_id] = verification
        stock = simulation.samples

    commands = [command for program in programs for command in program.channels[0].commands]
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=max(operations[operation_id].definition_version for operation_id in ids),
        channels=[ToolpathChannel(id="main", commands=[
            command.model_copy(update={"sequence": index, "channel_id": "main"})
            for index, command in enumerate(commands, 1)
        ])],
    )
    volumes_nonincreasing = all(
        simulation.metrics.remaining_volume_mm3 <= simulation.metrics.initial_volume_mm3 + 1e-6
        for simulation in simulations
    )
    checks = [
        FrontChainCheck(
            id="material_volume_nonincrease",
            status="passed" if volumes_nonincreasing else "failed",
            message="前端连续加工不得凭空增加材料",
            measured_value=len(simulations),
        ),
        FrontChainCheck(
            id="intermediate_overcut",
            status="passed" if all(item.status != "failed" for item in verifications.values()) else "failed",
            message="任一纵车或成形阶段不得越过已确认轮廓",
            measured_value=sum(item.status == "failed" for item in verifications.values()),
        ),
        FrontChainCheck(
            id="longitudinal_final_profile",
            status="passed" if verifications["OP30"].status == "passed" else "failed",
            message="OP30 后主纵车区域必须满足最终轮廓",
            measured_value=verifications["OP30"].status,
        ),
        FrontChainCheck(
            id="front_form_final_profile",
            status="passed" if verifications["OP31-FORM"].status == "passed" else "failed",
            message="OP31-FORM 后前端成形区域必须满足最终轮廓",
            measured_value=verifications["OP31-FORM"].status,
        ),
    ]
    return FrontChainDraftResult(
        job_id=job_id,
        status="failed" if any(item.status == "failed" for item in checks) else "passed",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        source_profile_id=source_profile.id,
        program_hash=toolpath_program_hash(program),
        toolpath=program,
        stages=stages,
        final_simulation=simulations[-1],
        longitudinal_verification=verifications["OP30"],
        front_form_verification=verifications["OP31-FORM"],
        checks=checks,
        warnings=[
            "本链仅验证 OP20/OP21-FORM/OP30/OP31-FORM；不包含端面、孔槽、接料、切断及背面工序。",
            "轴对称 Z-R 余料模型不替代三维刀具和机床碰撞仿真；结果仅为 DRAFT，未生成生产 NC。",
        ],
    )
