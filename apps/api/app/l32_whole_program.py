from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .l32_backside import BacksideDraftRequest, compile_backside_draft
from .l32_backside_chain import (
    BacksideChainDraftRequest, compile_backside_chain_draft,
)
from .l32_back_live_face import BackLiveFaceDraftRequest, compile_back_live_face_draft
from .l32_front_chain import FrontChainDraftRequest, compile_front_chain_draft
from .channel_timeline import ChannelTimelineResult, schedule_channel_timeline
from .continuous_turning_simulation import (
    ContinuousMaterialCheck, ContinuousTurningSimulationResult, simulate_continuous_whole_part,
)
from .machine_models import MachineConfigurationSnapshot
from .models import Operation, ProcessPlan
from .rotational_features import RotationalProfile, clip_rotational_profile
from .toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, toolpath_program_hash
from .turning_draft import TurningDraftRequest, compile_turning_draft
from .turning_verification import verify_turning_profile
from .turning_transfer import (
    TurningTransferDraftRequest, WorkpieceTransferState,
    compile_synchronized_transfer_draft, cutoff_kerf_intrusion_mm,
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
    phase: Literal["front_turning", "synchronized_transfer", "back_turning", "back_milling"]
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
    has_front_form = all(item in operations for item in ("OP21-FORM", "OP31-FORM"))
    has_back_region = all(item in operations for item in ("OP55-BACK", "OP58-BACK"))
    separation_operations = [
        operation for operation in operations.values()
        if operation.enabled and operation.type == "turn_cutoff"
    ]
    if len(separation_operations) != 1:
        raise ValueError(
            "formal L32 process plan requires exactly one material-separation operation; "
            f"found {len(separation_operations)}"
        )
    separation = separation_operations[0]
    back_face_operations = [
        operation for operation in operations.values()
        if operation.enabled and (
            operation.type == "back_live_face_finishing"
            or (operation.type == "turn_facing" and operation.workpiece_side == "back")
        )
    ]
    if len(back_face_operations) != 1:
        raise ValueError(
            "formal L32 process plan requires exactly one back-face finishing operation; "
            f"found {len(back_face_operations)}"
        )
    back_face_operation = back_face_operations[0]
    required_ids = ["OP10", "OP20", "OP30", separation.id, back_face_operation.id]
    required_ids.extend(["OP21-FORM", "OP31-FORM"] if has_front_form else [])
    required_ids.extend(["OP55-BACK", "OP58-BACK"] if has_back_region else [])
    missing = [operation_id for operation_id in required_ids if " or " in operation_id or operation_id not in operations]
    disabled = [operation_id for operation_id in required_ids if operation_id in operations and not operations[operation_id].enabled]
    if missing:
        raise ValueError("formal L32 process plan is missing operations: " + ", ".join(missing))
    if disabled:
        raise ValueError("formal L32 process plan has disabled operations: " + ", ".join(disabled))
    if plan.stock.get("profile_review_state") != "accepted":
        raise ValueError("formal L32 rotational profile must be accepted")
    if source_profile.id != request.source_profile_id or source_profile.review_state != "accepted":
        raise ValueError("whole-part draft requires the accepted source profile")
    kerf_intrusion = cutoff_kerf_intrusion_mm(separation, source_profile)
    if kerf_intrusion > 0.05:
        raise ValueError(
            f"cutoff kerf overlaps the accepted finished profile by {kerf_intrusion:.3f} mm; "
            "add sacrificial stock and redefine the cutoff/sub-spindle datum before whole-part DRAFT"
        )
    z_values = [point.z for point in source_profile.points]
    z_min = min(z_values) - 2
    z_max = max(z_values) + 2
    front_results = []
    for operation_id in ["OP10", *([] if has_front_form else ["OP20", "OP30"])]:
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
    front_chain = (
        compile_front_chain_draft(
            job_id,
            FrontChainDraftRequest(
                machine_instance_id=request.machine_instance_id,
                source_profile_id=source_profile.id,
                stock_radius_mm=request.stock_radius_mm,
                resolution_mm=request.resolution_mm,
            ),
            plan, source_profile, snapshot,
        ) if has_front_form else None
    )

    transfer = compile_synchronized_transfer_draft(
        job_id,
        TurningTransferDraftRequest(
            machine_instance_id=request.machine_instance_id,
            operation=separation,
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

    cutoff_z = float(separation.parameters["z_mm"])
    finished_back_datum_z = float(separation.parameters.get(
        "finished_back_datum_z_mm", cutoff_z,
    ))
    finished_radius = max(point.radius for point in source_profile.points)
    backside_stock_radius = min(request.stock_radius_mm, finished_radius + 0.2)
    backside_results = []
    for operation_id in (
        [back_face_operation.id] if back_face_operation.type == "turn_facing" else []
    ):
        backside_results.append(compile_backside_draft(
            job_id,
            BacksideDraftRequest(
                machine_instance_id=request.machine_instance_id,
                source_profile_id=source_profile.id,
                operation=operations[operation_id],
                source_cutoff_z_mm=finished_back_datum_z,
                stock_radius_mm=backside_stock_radius,
                resolution_mm=request.resolution_mm,
            ),
            source_profile,
            snapshot,
        ))
    back_live_face = None
    if back_face_operation.type == "back_live_face_finishing":
        axial_stock = float(back_face_operation.parameters.get(
            "stock_allowance_mm", separation.parameters.get("back_face_allowance_mm", 0),
        ))
        back_live_face = compile_back_live_face_draft(
            BackLiveFaceDraftRequest(
                machine_instance_id=request.machine_instance_id,
                operation=back_face_operation,
                stock_radius_mm=backside_stock_radius,
                axial_stock_mm=axial_stock,
                axial_clearance_mm=float(back_face_operation.parameters.get("axial_clearance_mm", 1)),
            ),
            snapshot,
        )
    backside_chain = (
        compile_backside_chain_draft(
            job_id,
            BacksideChainDraftRequest(
                machine_instance_id=request.machine_instance_id,
                source_profile_id=source_profile.id,
                stock_radius_mm=backside_stock_radius,
                resolution_mm=request.resolution_mm,
            ),
            plan, source_profile, snapshot,
        ) if has_back_region else None
    )

    main_groups = [result.toolpath.channels[0].commands for result in front_results]
    if front_chain is not None:
        main_groups.append(front_chain.toolpath.channels[0].commands)
    main_groups.append(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "main"))
    sub_groups = [next(channel.commands for channel in transfer.toolpath.channels if channel.id == "sub")]
    sub_groups.extend(result.draft.toolpath.channels[0].commands for result in backside_results)
    if back_live_face is not None:
        sub_groups.append(back_live_face.toolpath.channels[0].commands)
    if backside_chain is not None:
        sub_groups.append(backside_chain.toolpath.channels[0].commands)
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
        transfer_datum_z_mm=finished_back_datum_z,
        resolution_mm=request.resolution_mm,
    )
    if front_chain is not None:
        for operation_id, label in (
            ("OP30", "主纵车"), ("OP31-FORM", "前端成形"),
        ):
            operation = operations[operation_id]
            target = clip_rotational_profile(
                source_profile,
                float(operation.parameters["profile_z_min_mm"]),
                float(operation.parameters["profile_z_max_mm"]),
            )
            verification = verify_turning_profile(
                target, continuous_simulation.main_frame_after_cutoff, tolerance_mm=0.05,
            )
            continuous_simulation.checks.append(ContinuousMaterialCheck(
                id=f"{operation_id.lower()}_final_profile",
                status="passed" if verification.status == "passed" else "failed",
                message=f"{label}区域在接料切断后必须保持最终轮廓",
                measured_value=verification.metrics.maximum_overcut_mm,
            ))
    if backside_chain is not None:
        verification = verify_turning_profile(
            backside_chain.derived_profile,
            continuous_simulation.sub_frame_final,
            tolerance_mm=0.05,
        )
        continuous_simulation.checks.append(ContinuousMaterialCheck(
            id="op58_back_final_profile",
            status="passed" if verification.status == "passed" else "failed",
            message="背面区域在接料、端面和粗精车后必须满足最终轮廓",
            measured_value=verification.metrics.maximum_overcut_mm,
        ))
    if any(item.status == "failed" for item in continuous_simulation.checks):
        continuous_simulation.status = "failed"
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
    if front_chain is not None:
        for item in front_chain.stages:
            operation = operations[item.operation_id]
            stages.append(WholePartStage(
                sequence=len(stages) + 1,
                operation_id=operation.id,
                operation_name=operation.name,
                channel_id="main",
                phase="front_turning",
                command_start=main_cursor,
                command_end=main_cursor + item.command_count - 1,
                command_count=item.command_count,
                verification_status=item.verification_status,
            ))
            main_cursor += item.command_count
    transfer_count = len(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "main"))
    stages.append(WholePartStage(
        sequence=len(stages) + 1, operation_id=separation.id, operation_name=separation.name,
        channel_id="main", phase="synchronized_transfer", command_start=main_cursor,
        command_end=main_cursor + transfer_count - 1, command_count=transfer_count,
        verification_status=_verification_status(transfer),
    ))
    sub_cursor = len(next(channel.commands for channel in transfer.toolpath.channels if channel.id == "sub")) + 1
    backside_stage_ids = [back_face_operation.id] if back_face_operation.type == "turn_facing" else []
    for operation, result in zip(
        (operations[item] for item in backside_stage_ids), backside_results,
    ):
        count = len(result.draft.toolpath.channels[0].commands)
        stages.append(WholePartStage(
            sequence=len(stages) + 1, operation_id=operation.id, operation_name=operation.name,
            channel_id="sub", phase="back_turning", command_start=sub_cursor,
            command_end=sub_cursor + count - 1, command_count=count,
            verification_status=_verification_status(result.draft),
        ))
        sub_cursor += count
    if back_live_face is not None:
        operation = back_face_operation
        count = len(back_live_face.toolpath.channels[0].commands)
        stages.append(WholePartStage(
            sequence=len(stages) + 1, operation_id=operation.id,
            operation_name=operation.name, channel_id="sub", phase="back_milling",
            command_start=sub_cursor, command_end=sub_cursor + count - 1,
            command_count=count, verification_status=back_live_face.status,
        ))
        sub_cursor += count
    if backside_chain is not None:
        for item in backside_chain.stages:
            operation = operations[item.operation_id]
            stages.append(WholePartStage(
                sequence=len(stages) + 1,
                operation_id=operation.id,
                operation_name=operation.name,
                channel_id="sub",
                phase="back_turning",
                command_start=sub_cursor,
                command_end=sub_cursor + item.command_count - 1,
                command_count=item.command_count,
                verification_status=item.verification_status,
            ))
            sub_cursor += item.command_count

    warnings = list(dict.fromkeys([
        *(warning for result in front_results for warning in result.warnings),
        *transfer.warnings,
        *(warning for result in backside_results for warning in result.warnings),
        *(front_chain.warnings if front_chain is not None else []),
        *(backside_chain.warnings if backside_chain is not None else []),
        *(back_live_face.warnings if back_live_face is not None else []),
        "整件程序已合并轴对称跨坐标系连续材料状态；仍未替代三维机床运动学与碰撞验证。",
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
                z_scale_from_main=-1, source_cutoff_z_mm=finished_back_datum_z,
            ),
        ],
        stages=stages,
        state_transitions=transfer.state_transitions,
        timeline=timeline,
        continuous_simulation=continuous_simulation,
        warnings=warnings,
    )
