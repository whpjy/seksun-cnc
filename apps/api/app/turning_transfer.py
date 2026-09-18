from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .l32_configuration import L32_DEFINITION
from .machine_models import MachineConfigurationSnapshot
from .toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, ToolpathTrace
from .models import Operation
from .rotational_features import RotationalProfile
from .turning_draft import TurningDraftRequest, compile_turning_draft
from .turning_reachability import TurningReachabilityResult
from .turning_simulation import TurningSimulationResult
from .turning_verification import TurningVerificationResult


class TurningTransferDraftRequest(TurningDraftRequest):
    approach_z_mm: float
    pickoff_z_mm: float
    grip_length_mm: float = Field(gt=0, le=100)
    synchronization_rpm: int = Field(gt=0)
    sub_spindle_clamp_confirmed: bool = False

    @model_validator(mode="after")
    def validate_transfer(self) -> "TurningTransferDraftRequest":
        if self.operation.type != "turn_cutoff":
            raise ValueError("synchronized transfer requires a turn_cutoff operation")
        if self.operation.channel_id not in {None, "main"}:
            raise ValueError("cutoff operation must run on the main channel")
        if self.operation.spindle_id not in {None, "main"}:
            raise ValueError("cutoff operation must reference the main spindle")
        if not self.sub_spindle_clamp_confirmed:
            raise ValueError("sub-spindle clamp pressure/force must be confirmed")
        return self


class WorkpieceTransferState(BaseModel):
    sequence: int = Field(ge=1)
    state: Literal[
        "main_spindle_held", "dual_spindle_clamped", "phase_synchronized",
        "part_separated", "sub_spindle_held",
    ]
    holding_spindles: list[Literal["main", "sub"]]
    barrier_id: str | None = None


class TurningTransferDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    job_id: str
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    machine_instance_id: str
    machine_configuration_hash: str
    cutoff_operation_id: str
    toolpath: ToolpathProgram
    state_transitions: list[WorkpieceTransferState]
    simulation: TurningSimulationResult
    verification: TurningVerificationResult | None = None
    reachability: TurningReachabilityResult | None = None
    warnings: list[str] = Field(default_factory=list)


def _command(
    sequence: int,
    command_type: str,
    channel_id: str,
    operation_id: str,
    *,
    axes: dict[str, float] | None = None,
    parameters: dict[str, float | int | str | bool] | None = None,
    safety_requirements: list[str] | None = None,
) -> ToolpathCommand:
    return ToolpathCommand.model_validate({
        "sequence": sequence,
        "type": command_type,
        "channel_id": channel_id,
        "operation_id": operation_id,
        "axes": axes or {},
        "parameters": parameters or {},
        "safety_requirements": safety_requirements or [],
        "trace": ToolpathTrace(
            feature_ids=[], source="l32_synchronized_transfer",
            generator_version="0.1.0",
        ),
    })


def _renumber(commands: list[ToolpathCommand], channel_id: str) -> list[ToolpathCommand]:
    return [
        command.model_copy(update={"sequence": index, "channel_id": channel_id})
        for index, command in enumerate(commands, 1)
    ]


def cutoff_kerf_intrusion_mm(operation: Operation, profile: RotationalProfile) -> float:
    """Amount of a centered parting kerf entering the accepted part profile."""
    width = float(operation.parameters.get(
        "cutting_width_mm", operation.tool.cutting_width_mm or 0,
    ))
    if width <= 0:
        raise ValueError("cutoff kerf width must be positive")
    if "z_mm" not in operation.parameters:
        raise ValueError("cutoff datum Z is missing")
    cutoff_z = float(operation.parameters["z_mm"])
    finished_minimum_z = min(point.z for point in profile.points)
    return max(cutoff_z + width / 2 - finished_minimum_z, 0)


def compile_synchronized_transfer_draft(
    job_id: str,
    request: TurningTransferDraftRequest,
    snapshot: MachineConfigurationSnapshot,
) -> TurningTransferDraftResult:
    if not {"X2", "Z2"} <= set(snapshot.validation.enabled_axes):
        raise ValueError("machine configuration lacks the sub-spindle X2/Z2 axes")
    sub_spindle = next(item for item in L32_DEFINITION.spindles if item.id == "sub")
    main_spindle = next(item for item in L32_DEFINITION.spindles if item.id == "main")
    rpm_limit = min(main_spindle.maximum_rpm, sub_spindle.maximum_rpm)
    if request.synchronization_rpm > rpm_limit:
        raise ValueError(f"synchronization_rpm exceeds the spindle limit: {rpm_limit}")

    base_request = TurningDraftRequest.model_validate(request.model_dump())
    base = compile_turning_draft(job_id, base_request, snapshot)
    source_main = base.toolpath.channels[0].commands
    cutting_index = next((
        index for index, item in enumerate(source_main)
        if "part_retention_confirmed" in item.safety_requirements
    ), None)
    cutoff_index = next((
        index for index, item in enumerate(source_main) if item.type == "cutoff"
    ), None)
    if cutting_index is None or cutoff_index is None or cutoff_index < cutting_index:
        raise ValueError("cutoff toolpath does not expose a safe transfer insertion point")

    operation_id = request.operation.id
    main_commands = [*source_main[:cutting_index]]
    main_commands.extend([
        _command(1, "sync_barrier", "main", operation_id, parameters={"barrier_id": "HANDOFF_CLAMPED"}),
        _command(
            1, "spindle_phase_sync", "main", operation_id,
            parameters={"master_spindle_id": "main", "slave_spindle_id": "sub", "rpm": request.synchronization_rpm},
            safety_requirements=["dual_spindle_clamp_confirmed", "spindle_phase_reference_confirmed"],
        ),
        _command(1, "sync_barrier", "main", operation_id, parameters={"barrier_id": "SPINDLES_SYNCHRONIZED"}),
    ])
    main_commands.extend(source_main[cutting_index:cutoff_index + 1])
    main_commands.append(_command(
        1, "sync_barrier", "main", operation_id,
        parameters={"barrier_id": "PART_SEPARATED"},
    ))
    main_commands.extend(source_main[cutoff_index + 1:])

    sub_commands = [
        _command(
            1, "sub_spindle_approach", "sub", operation_id,
            axes={"Z2": request.approach_z_mm},
            parameters={"target_z_mm": request.pickoff_z_mm, "grip_length_mm": request.grip_length_mm},
            safety_requirements=["sub_spindle_path_clear", "chuck_open_confirmed"],
        ),
        _command(
            2, "chuck_close", "sub", operation_id,
            parameters={"spindle_id": "sub", "grip_length_mm": request.grip_length_mm},
            safety_requirements=["clamp_pressure_confirmed", "part_contact_confirmed"],
        ),
        _command(3, "sync_barrier", "sub", operation_id, parameters={"barrier_id": "HANDOFF_CLAMPED"}),
        _command(
            4, "spindle_phase_sync", "sub", operation_id,
            parameters={"master_spindle_id": "main", "slave_spindle_id": "sub", "rpm": request.synchronization_rpm},
            safety_requirements=["dual_spindle_clamp_confirmed", "spindle_phase_reference_confirmed"],
        ),
        _command(5, "sync_barrier", "sub", operation_id, parameters={"barrier_id": "SPINDLES_SYNCHRONIZED"}),
        _command(6, "sync_barrier", "sub", operation_id, parameters={"barrier_id": "PART_SEPARATED"}),
        _command(
            7, "pickoff", "sub", operation_id,
            parameters={"from_spindle_id": "main", "to_spindle_id": "sub"},
            safety_requirements=["cutoff_complete", "sub_spindle_clamped"],
        ),
        _command(
            8, "rapid_move", "sub", operation_id,
            axes={"Z2": request.approach_z_mm},
            safety_requirements=["part_held_by_sub_spindle", "retract_path_clear"],
        ),
    ]
    toolpath = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=max(request.operation.definition_version, 1),
        channels=[
            ToolpathChannel(id="main", commands=_renumber(main_commands, "main")),
            ToolpathChannel(id="sub", commands=_renumber(sub_commands, "sub")),
        ],
    )
    states = [
        WorkpieceTransferState(sequence=1, state="main_spindle_held", holding_spindles=["main"]),
        WorkpieceTransferState(sequence=2, state="dual_spindle_clamped", holding_spindles=["main", "sub"], barrier_id="HANDOFF_CLAMPED"),
        WorkpieceTransferState(sequence=3, state="phase_synchronized", holding_spindles=["main", "sub"], barrier_id="SPINDLES_SYNCHRONIZED"),
        WorkpieceTransferState(sequence=4, state="part_separated", holding_spindles=["sub"], barrier_id="PART_SEPARATED"),
        WorkpieceTransferState(sequence=5, state="sub_spindle_held", holding_spindles=["sub"]),
    ]
    kerf_intrusion = (
        cutoff_kerf_intrusion_mm(request.operation, request.profile)
        if request.profile is not None else 0
    )
    return TurningTransferDraftResult(
        job_id=job_id,
        machine_instance_id=snapshot.instance.id,
        machine_configuration_hash=snapshot.configuration_hash,
        cutoff_operation_id=operation_id,
        toolpath=toolpath,
        state_transitions=states,
        simulation=base.simulation,
        verification=base.verification,
        reachability=base.reachability,
        warnings=[
            *base.warnings,
            *([f"切断刀缝侵入已确认成品轮廓 {kerf_intrusion:.3f} mm；整件草案必须阻断，需增加牺牲余料并重新定义切断/背轴基准。"]
              if kerf_intrusion > 0.05 else []),
            "双通道同步仅为控制器无关草案；同步代码、夹紧确认信号和等待号尚未映射到 MELDAS/CINCOM。",
            "接料位置、夹持长度、夹紧力与退出路径必须在命名机床上完成干运行和首件验证。",
        ],
    )
