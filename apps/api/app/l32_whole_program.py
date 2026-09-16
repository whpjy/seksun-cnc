from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .l32_backside import BacksideDraftRequest, compile_backside_draft
from .channel_timeline import ChannelTimelineResult, schedule_channel_timeline
from .continuous_turning_simulation import (
    ContinuousTurningSimulationResult, simulate_continuous_whole_part,
)
from .machine_models import MachineConfigurationSnapshot
from .models import Operation, ProcessPlan
from .rotational_features import RotationalProfile
from .toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, toolpath_program_hash
from .turning_draft import TurningDraftRequest, compile_turning_draft
from .turning_transfer import (
    TurningTransferDraftRequest, WorkpieceTransferState,
    compile_synchronized_transfer_draft,
)


class WholePartDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    source_profile_id: str = Field(min_length=1)
    stock_radius_mm: float = Field(gt=0)
    initial_bore_radius_mm: float = Field(default=0, ge=0)
    resolution_mm: float = Field(default=0.2, gt=0, le=2)
    approach_z_mm: float
    pickoff_z_mm: float
    grip_length_mm: float = Field(gt=0, le=100)
    synchronization_rpm: int = Field(gt=0)
    sub_spindle_clamp_confirmed: bool = False


class WholePartCoordinateFrame(BaseModel):
    channel_id: Literal["main", "sub"]
    spindle_id: Literal["main", "sub"]
    datum: str
    z_scale_from_main: Literal[-1, 1]
    source_cutoff_z_mm: float | None = None


class WholePartStage(BaseModel):
    sequence: int = Field(ge=1)
    operation_id: str
    operation_name: str
    channel_id: Literal["main", "sub"]
    phase: Literal["front_turning", "synchronized_transfer", "back_turning"]
    command_start: int = Field(ge=1)
    command_end: int = Field(ge=1)
    command_count: int = Field(ge=1)
    verification_status: Literal["passed", "warning", "failed", "not_applicable"]


class WholePartDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    machine_instance_id: str
    machine_configuration_hash: str
    program_hash: str
    toolpath: ToolpathProgram
    coordinate_frames: list[WholePartCoordinateFrame]
    stages: list[WholePartStage]
    state_transitions: list[WorkpieceTransferState]
    timeline: ChannelTimelineResult
    continuous_simulation: ContinuousTurningSimulationResult
    warnings: list[str] = Field(default_factory=list)


def _renumber(commands: list[ToolpathCommand], channel_id: str) -> list[ToolpathCommand]:
    return [
        command.model_copy(update={"sequence": index, "channel_id": channel_id})
        for index, command in enumerate(commands, 1)
    ]


def _verification_status(result) -> str:
    return result.verification.status if result.verification is not None else "not_applicable"


def _operation_map(plan: ProcessPlan) -> dict[str, Operation]:
    return {
        operation.id: operation
        for setup in plan.setups
        for operation in setup.operations
    }


def compile_whole_part_draft(
    job_id: str,
    request: WholePartDraftRequest,
    plan: ProcessPlan,
    source_profile: RotationalProfile,
    snapshot: MachineConfigurationSnapshot,
) -> WholePartDraftResult:
    operations = _operation_map(plan)
    required_ids = ["OP10", "OP20", "OP30", "OP40", "OP50", "OP60"]
    missing = [operation_id for operation_id in required_ids if operation_id not in operations]
    disabled = [operation_id for operation_id in required_ids if operation_id in operations and not operations[operation_id].enabled]
    if missing:
        raise ValueError("formal L32 process plan is missing operations: " + ", ".join(missing))
    if disabled:
        raise ValueError("formal L32 process plan has disabled operations: " + ", ".join(disabled))
    if plan.stock.get("profile_review_state") != "accepted":
        raise ValueError("formal L32 rotational profile must be accepted")
    if source_profile.id != request.source_profile_id or source_profile.review_state != "accepted":
        raise ValueError("whole-part draft requires the accepted source profile")

    z_values = [point.z for point in source_profile.points]
    z_min = min(z_values) - 2
    z_max = max(z_values) + 2
    front_results = []
    for operation_id in ["OP10", "OP20", "OP30"]:
        front_results.append(compile_turning_draft(
            job_id,
            TurningDraftRequest(
                machine_instance_id=request.machine_instance_id,
                operation=operations[operation_id],
                profile=source_profile,
                stock_radius_mm=request.stock_radius_mm,
                initial_bore_radius_mm=request.initial_bore_radius_mm,
                z_min_mm=z_min,
                z_max_mm=z_max,
                resolution_mm=request.resolution_mm,
            ),
            snapshot,
        ))

    transfer = compile_synchronized_transfer_draft(
        job_id,
        TurningTransferDraftRequest(
            machine_instance_id=request.machine_instance_id,
            operation=operations["OP40"],
            profile=source_profile,
            stock_radius_mm=request.stock_radius_mm,
            initial_bore_radius_mm=request.initial_bore_radius_mm,
            z_min_mm=z_min,
            z_max_mm=z_max,
            resolution_mm=request.resolution_mm,
            approach_z_mm=request.approach_z_mm,
            pickoff_z_mm=request.pickoff_z_mm,
            grip_length_mm=request.grip_length_mm,
            synchronization_rpm=request.synchronization_rpm,
            sub_spindle_clamp_confirmed=request.sub_spindle_clamp_confirmed,
        ),
        snapshot,
    )

    cutoff_z = float(operations["OP40"].parameters["z_mm"])
    finished_radius = max(point.radius for point in source_profile.points)
    backside_stock_radius = min(request.stock_radius_mm, finished_radius + 0.2)
    backside_results = []
    for operation_id in ["OP50", "OP60"]:
        backside_results.append(compile_backside_draft(
            job_id,
            BacksideDraftRequest(
                machine_instance_id=request.machine_instance_id,
                source_profile_id=source_profile.id,
                operation=operations[operation_id],
                source_cutoff_z_mm=cutoff_z,
                stock_radius_mm=backside_stock_radius,
                resolution_mm=request.resolution_mm,
            ),
            source_profile,
            snapshot,
        ))

    main_groups = [result.toolpath.channels[0].commands for result in front_results]
    main_groups.append(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "main"))
    sub_groups = [next(channel.commands for channel in transfer.toolpath.channels if channel.id == "sub")]
    sub_groups.extend(result.draft.toolpath.channels[0].commands for result in backside_results)
    main_commands = _renumber([command for group in main_groups for command in group], "main")
    sub_commands = _renumber([command for group in sub_groups for command in group], "sub")
    toolpath = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=max(operation.definition_version for operation in operations.values()),
        channels=[
            ToolpathChannel(id="main", commands=main_commands),
            ToolpathChannel(id="sub", commands=sub_commands),
        ],
    )
    timeline = schedule_channel_timeline(toolpath)
    continuous_simulation = simulate_continuous_whole_part(
        toolpath,
        stock_radius_mm=request.stock_radius_mm,
        initial_bore_radius_mm=request.initial_bore_radius_mm,
        main_z_min_mm=z_min,
        main_z_max_mm=z_max,
        transfer_datum_z_mm=cutoff_z,
        resolution_mm=request.resolution_mm,
    )
    if continuous_simulation.status == "failed":
        failures = [
            item.message for item in continuous_simulation.checks if item.status == "failed"
        ]
        raise ValueError("continuous material simulation failed: " + "; ".join(failures))

    stages: list[WholePartStage] = []
    main_cursor = 1
    for operation, result in zip((operations[item] for item in ["OP10", "OP20", "OP30"]), front_results):
        count = len(result.toolpath.channels[0].commands)
        stages.append(WholePartStage(
            sequence=len(stages) + 1, operation_id=operation.id, operation_name=operation.name,
            channel_id="main", phase="front_turning", command_start=main_cursor,
            command_end=main_cursor + count - 1, command_count=count,
            verification_status=_verification_status(result),
        ))
        main_cursor += count
    transfer_count = len(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "main"))
    stages.append(WholePartStage(
        sequence=4, operation_id="OP40", operation_name=operations["OP40"].name,
        channel_id="main", phase="synchronized_transfer", command_start=main_cursor,
        command_end=main_cursor + transfer_count - 1, command_count=transfer_count,
        verification_status=_verification_status(transfer),
    ))
    sub_cursor = len(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "sub")) + 1
    for operation, result in zip((operations[item] for item in ["OP50", "OP60"]), backside_results):
        count = len(result.draft.toolpath.channels[0].commands)
        stages.append(WholePartStage(
            sequence=len(stages) + 1, operation_id=operation.id, operation_name=operation.name,
            channel_id="sub", phase="back_turning", command_start=sub_cursor,
            command_end=sub_cursor + count - 1, command_count=count,
            verification_status=_verification_status(result.draft),
        ))
        sub_cursor += count

    warnings = list(dict.fromkeys([
        *(warning for result in front_results for warning in result.warnings),
        *transfer.warnings,
        *(warning for result in backside_results for warning in result.warnings),
        "整件程序仅完成控制器无关的工序与同步编排；各阶段仿真尚未合并为跨坐标系连续材料状态。",
        "不得将本草案直接转换或发送到机床，必须完成后处理器映射、机床级仿真、干运行和首件验证。",
    ]))
    return WholePartDraftResult(
        job_id=job_id,
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        program_hash=toolpath_program_hash(toolpath),
        toolpath=toolpath,
        coordinate_frames=[
            WholePartCoordinateFrame(
                channel_id="main", spindle_id="main", datum="accepted front rotational profile",
                z_scale_from_main=1,
            ),
            WholePartCoordinateFrame(
                channel_id="sub", spindle_id="sub", datum="OP40 cutoff plane",
                z_scale_from_main=-1, source_cutoff_z_mm=cutoff_z,
            ),
        ],
        stages=stages,
        state_transitions=transfer.state_transitions,
        timeline=timeline,
        continuous_simulation=continuous_simulation,
        warnings=warnings,
    )
