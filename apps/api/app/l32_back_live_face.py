from __future__ import annotations

from math import ceil, sqrt
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .machine_models import MachineConfigurationSnapshot
from .models import Operation
from .toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, ToolpathTrace


class BackLiveFaceDraftRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    operation: Operation
    stock_radius_mm: float = Field(gt=0)
    axial_stock_mm: float = Field(gt=0, le=2)
    axial_clearance_mm: float = Field(default=1, gt=0, le=20)

    @model_validator(mode="after")
    def validate_operation(self) -> "BackLiveFaceDraftRequest":
        operation = self.operation
        if operation.type != "back_live_face_finishing":
            raise ValueError("back live face draft requires back_live_face_finishing")
        if operation.channel_id != "sub" or operation.spindle_id != "sub":
            raise ValueError("back live face operation must use the sub channel and spindle")
        if operation.workpiece_side != "back" or operation.tool.kind != "end_mill":
            raise ValueError("back live face operation requires a back-side end mill")
        return self


class BackLiveFaceCheck(BaseModel):
    id: str
    status: Literal["passed", "failed"]
    message: str
    measured_value: float | int | str | None = None


class BackLiveFaceDraftResult(BaseModel):
    schema_version: str = "1.0.0"
    release_status: Literal["DRAFT"] = "DRAFT"
    nc_generated: Literal[False] = False
    operation_id: str
    toolpath: ToolpathProgram
    raster_pass_count: int = Field(ge=1)
    axial_stock_mm: float = Field(gt=0)
    checks: list[BackLiveFaceCheck]
    status: Literal["passed", "failed"]
    warnings: list[str] = Field(default_factory=list)


def compile_back_live_face_draft(
    request: BackLiveFaceDraftRequest,
    snapshot: MachineConfigurationSnapshot,
) -> BackLiveFaceDraftResult:
    if request.machine_instance_id != snapshot.instance.id:
        raise ValueError("back live face request does not match the bound machine")
    if "back_live_tool_milling" not in snapshot.validation.capabilities:
        raise ValueError("machine configuration lacks capability: back_live_tool_milling")

    operation = request.operation
    tool_radius = operation.tool.diameter_mm / 2
    stepover = float(operation.parameters.get("step_over_mm", operation.tool.diameter_mm * 0.55))
    if stepover <= 0 or stepover > operation.tool.diameter_mm:
        raise ValueError("back live face stepover must be positive and no larger than the cutter diameter")
    feed = float(operation.parameters.get("feed_rate_mm_min", 0))
    plunge = float(operation.parameters.get("plunge_rate_mm_min", feed * 0.3))
    rpm = int(operation.parameters.get("spindle_rpm", 0))
    if min(feed, plunge, rpm) <= 0:
        raise ValueError("back live face cutting parameters must be positive")

    sweep_radius = request.stock_radius_mm + tool_radius
    pass_count = max(2, int(ceil((2 * sweep_radius) / stepover)) + 1)
    actual_step = (2 * sweep_radius) / (pass_count - 1)
    trace = ToolpathTrace(
        feature_ids=operation.feature_ids,
        source="agent_back_live_face_boundary_raster",
        generator_version="1.0.0",
    )
    commands: list[ToolpathCommand] = []

    def add(command_type: str, *, axes: dict[str, float] | None = None,
            parameters: dict[str, float | int | str | bool] | None = None,
            safety: list[str] | None = None) -> None:
        commands.append(ToolpathCommand(
            sequence=len(commands) + 1, type=command_type, channel_id="sub",
            operation_id=operation.id, axes=axes or {}, parameters=parameters or {},
            safety_requirements=safety or [], trace=trace,
        ))

    add("select_tool", parameters={"tool_id": operation.tool.id, "process": "back_live_face_milling"})
    add("set_rpm", parameters={"rpm": rpm, "spindle_role": "sub_live"})
    add("set_feed_per_minute", parameters={"feed_mm_min": feed})
    add("spindle_start", parameters={"direction": "clockwise", "spindle_role": "sub_live"})
    add("coolant_on")
    for index in range(pass_count):
        y = -sweep_radius + index * actual_step
        chord = sqrt(max(sweep_radius * sweep_radius - y * y, 0))
        start_x, end_x = (-chord, chord) if index % 2 == 0 else (chord, -chord)
        # X remains a diameter coordinate so the existing continuous-material
        # simulator can consume the same IR. Y records the real indexed raster.
        add("rapid_move", axes={"X": round(start_x * 2, 6), "Y": round(y, 6), "Z": request.axial_clearance_mm},
            safety=["sub_spindle_clamped", "back_live_tool_clearance_confirmed"])
        add("feed_move", axes={"X": round(start_x * 2, 6), "Y": round(y, 6), "Z": 0.0},
            parameters={"feed_rate_mm_min": plunge, "motion_role": "axial_entry"})
        add("feed_move", axes={"X": round(end_x * 2, 6), "Y": round(y, 6), "Z": 0.0},
            parameters={
                "feed_rate_mm_min": feed,
                "cut_side": "facing",
                "retain_direction": "negative_z",
                "process": "back_live_face_milling",
                "tool_diameter_mm": operation.tool.diameter_mm,
                "axial_stock_mm": request.axial_stock_mm,
            })
    add("rapid_move", axes={"X": 0.0, "Y": 0.0, "Z": request.axial_clearance_mm})
    add("coolant_off")
    add("spindle_stop", parameters={"spindle_role": "sub_live"})

    checks = [
        BackLiveFaceCheck(
            id="back_live_capability", status="passed",
            message="绑定设备具备背面动力刀具能力", measured_value=snapshot.instance.id,
        ),
        BackLiveFaceCheck(
            id="positive_back_face_stock", status="passed",
            message="切断工序为背面精加工保留了正余量", measured_value=request.axial_stock_mm,
        ),
        BackLiveFaceCheck(
            id="raster_stepover_bounded", status="passed",
            message="光栅步距不超过刀具直径", measured_value=round(actual_step, 6),
        ),
        BackLiveFaceCheck(
            id="finished_plane_respected", status="passed",
            message="所有切削轨迹止于背面成品基准，不进入成品实体", measured_value=0.0,
        ),
    ]
    toolpath = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash=snapshot.configuration_hash,
        plan_revision=operation.definition_version,
        channels=[ToolpathChannel(id="sub", commands=commands)],
    )
    return BackLiveFaceDraftResult(
        operation_id=operation.id, toolpath=toolpath,
        raster_pass_count=pass_count, axial_stock_mm=request.axial_stock_mm,
        checks=checks, status="passed",
        warnings=[
            "背面动力刀具刀路仍为 DRAFT，未生成控制器 NC。",
            "二维连续余料验证与三维 STEP 扫掠必须同时通过；夹头实体碰撞仍需机床级仿真。",
        ],
    )
