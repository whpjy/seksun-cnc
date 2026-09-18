from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .l32_backside import (
    BacksideCoordinateTransform, BacksideDraftRequest, compile_backside_draft,
)
from .machine_models import MachineConfigurationSnapshot
from .models import ProcessPlan
from .rotational_features import RotationalProfile, split_outer_profile_for_longitudinal_turning
from .toolpath_ir import ToolpathChannel, ToolpathProgram, toolpath_program_hash
from .turning_simulation import TurningSimulationResult, simulate_turning_stock
from .turning_verification import TurningVerificationResult, verify_turning_profile


class BacksideChainDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    source_profile_id: str = Field(min_length=1)
    stock_radius_mm: float = Field(gt=0)
    resolution_mm: float = Field(default=0.1, gt=0, le=2)


class BacksideChainStage(BaseModel):
    operation_id: str
    command_count: int = Field(ge=1)
    initial_volume_mm3: float = Field(ge=0)
    final_volume_mm3: float = Field(ge=0)
    removed_volume_mm3: float = Field(ge=0)
    verification_status: Literal["passed", "warning", "failed"]


class BacksideChainCheck(BaseModel):
    id: str
    status: Literal["passed", "failed"]
    message: str
    measured_value: float | int | str | None = None


class BacksideChainDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    status: Literal["passed", "failed"]
    machine_instance_id: str
    machine_configuration_hash: str
    source_profile_id: str
    program_hash: str
    transform: BacksideCoordinateTransform
    derived_profile: RotationalProfile
    toolpath: ToolpathProgram
    stages: list[BacksideChainStage]
    final_simulation: TurningSimulationResult
    final_verification: TurningVerificationResult
    checks: list[BacksideChainCheck]
    warnings: list[str] = Field(default_factory=list)


def compile_backside_chain_draft(
    job_id: str,
    request: BacksideChainDraftRequest,
    plan: ProcessPlan,
    source_profile: RotationalProfile,
    snapshot: MachineConfigurationSnapshot,
) -> BacksideChainDraftResult:
    if request.machine_instance_id != snapshot.instance.id:
        raise ValueError("backside chain does not use the bound machine instance")
    if (
        source_profile.id != request.source_profile_id
        or source_profile.side != "outer"
        or source_profile.extraction_method != "exact_section"
        or source_profile.review_state != "accepted"
    ):
        raise ValueError("backside chain requires the accepted exact outer profile")
    if plan.stock.get("rotational_profile_id") != source_profile.id:
        raise ValueError("backside chain source profile does not match the formal plan")

    operations = {item.id: item for setup in plan.setups for item in setup.operations}
    cutoff = operations.get("OP40")
    rough = operations.get("OP55-BACK")
    finish = operations.get("OP58-BACK")
    if cutoff is None or cutoff.type != "turn_cutoff" or "z_mm" not in cutoff.parameters:
        raise ValueError("backside chain requires a planned cutoff datum")
    if rough is None or finish is None:
        raise ValueError("backside chain requires planned OP55-BACK and OP58-BACK operations")
    if not rough.enabled or not finish.enabled:
        raise ValueError("backside chain contains operations disabled by the bound machine")
    if rough.type != "turn_od_roughing" or finish.type != "turn_od_finishing":
        raise ValueError("backside chain operation types do not match the formal sequence")
    if rough.sequence >= finish.sequence:
        raise ValueError("backside chain operation order is invalid")

    cutoff_z = float(cutoff.parameters.get(
        "finished_back_datum_z_mm", cutoff.parameters["z_mm"],
    ))
    _, _, expected_region = split_outer_profile_for_longitudinal_turning(source_profile)
    if expected_region is None:
        raise ValueError("accepted source profile has no backside turning region")
    expected_minimum = min(point.z for point in expected_region.points)
    expected_maximum = max(point.z for point in expected_region.points)
    if abs(cutoff_z - expected_minimum) > 1e-6:
        raise ValueError("planned cutoff datum does not match the backside turning region")
    for operation in (rough, finish):
        minimum = operation.parameters.get("source_region_z_min_mm")
        maximum = operation.parameters.get("source_region_z_max_mm")
        if minimum is None or maximum is None or (
            abs(float(minimum) - expected_minimum) > 1e-6
            or abs(float(maximum) - expected_maximum) > 1e-6
        ):
            raise ValueError(f"{operation.id} region does not match the accepted source profile")
    drafts = [
        compile_backside_draft(
            job_id,
            BacksideDraftRequest(
                machine_instance_id=request.machine_instance_id,
                source_profile_id=source_profile.id,
                operation=operation,
                source_cutoff_z_mm=cutoff_z,
                stock_radius_mm=request.stock_radius_mm,
                resolution_mm=request.resolution_mm,
            ),
            source_profile,
            snapshot,
        )
        for operation in (rough, finish)
    ]
    if drafts[0].derived_profile.model_dump() != drafts[1].derived_profile.model_dump():
        raise ValueError("backside rough and finish must use the same derived region")
    profile = drafts[0].derived_profile
    rough_simulation = drafts[0].draft.simulation
    finish_program = drafts[1].draft.toolpath
    finish_simulation = simulate_turning_stock(
        finish_program,
        stock_radius_mm=request.stock_radius_mm,
        z_min_mm=rough_simulation.samples[0].z,
        z_max_mm=rough_simulation.samples[-1].z,
        resolution_mm=request.resolution_mm,
        initial_samples=rough_simulation.samples,
    )
    rough_verification = verify_turning_profile(
        profile, rough_simulation, tolerance_mm=0.05,
        expected_allowance_mm=float(rough.parameters.get("radial_allowance_mm", 0)),
    )
    finish_verification = verify_turning_profile(
        profile, finish_simulation, tolerance_mm=0.05,
        expected_allowance_mm=float(finish.parameters.get("radial_allowance_mm", 0)),
    )
    stages = [
        BacksideChainStage(
            operation_id=operation.id,
            command_count=len(draft.draft.toolpath.channels[0].commands),
            initial_volume_mm3=simulation.metrics.initial_volume_mm3,
            final_volume_mm3=simulation.metrics.remaining_volume_mm3,
            removed_volume_mm3=simulation.metrics.removed_volume_mm3,
            verification_status=verification.status,
        )
        for operation, draft, simulation, verification in (
            (rough, drafts[0], rough_simulation, rough_verification),
            (finish, drafts[1], finish_simulation, finish_verification),
        )
    ]
    commands = [
        command
        for draft in drafts
        for command in draft.draft.toolpath.channels[0].commands
    ]
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=max(rough.definition_version, finish.definition_version),
        channels=[ToolpathChannel(id="sub", commands=[
            command.model_copy(update={"sequence": index, "channel_id": "sub"})
            for index, command in enumerate(commands, 1)
        ])],
    )
    checks = [
        BacksideChainCheck(
            id="intermediate_overcut",
            status="passed" if rough_verification.status != "failed" else "failed",
            message="背面粗车不得越过已确认的区域轮廓",
            measured_value=rough_verification.metrics.maximum_overcut_mm,
        ),
        BacksideChainCheck(
            id="material_volume_nonincrease",
            status="passed" if finish_simulation.metrics.remaining_volume_mm3 <=
            finish_simulation.metrics.initial_volume_mm3 + 1e-6 else "failed",
            message="背面精车必须承接粗车余料且不得增加材料",
            measured_value=finish_simulation.metrics.removed_volume_mm3,
        ),
        BacksideChainCheck(
            id="final_profile",
            status="passed" if finish_verification.status == "passed" else "failed",
            message="背面精车后连续余料必须满足已确认的区域轮廓",
            measured_value=finish_verification.status,
        ),
    ]
    return BacksideChainDraftResult(
        job_id=job_id,
        status="failed" if any(item.status == "failed" for item in checks) else "passed",
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        source_profile_id=source_profile.id,
        program_hash=toolpath_program_hash(program),
        transform=drafts[0].transform,
        derived_profile=profile,
        toolpath=program,
        stages=stages,
        final_simulation=finish_simulation,
        final_verification=finish_verification,
        checks=checks,
        warnings=[
            "背面连续余料仅使用轴对称 Z-R 模型；尚未包含切断交接、夹头覆盖区或三维刀杆干涉。",
            "结果仅为 DRAFT；未生成生产 NC。",
        ],
    )
