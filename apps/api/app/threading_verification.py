from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import Operation
from .toolpath_ir import ToolpathProgram
from .turning_simulation import TurningSimulationResult


class ThreadingVerificationCheck(BaseModel):
    id: str
    status: Literal["passed", "warning", "failed"]
    message: str


class ThreadingVerificationMetrics(BaseModel):
    start_z_mm: float
    end_z_mm: float
    thread_length_mm: float = Field(gt=0)
    pitch_mm: float = Field(gt=0)
    major_diameter_mm: float = Field(gt=0)
    minor_diameter_mm: float = Field(gt=0)
    radial_depth_mm: float = Field(gt=0)
    pass_count: int = Field(ge=1)
    emitted_pass_count: int = Field(ge=0)


class ThreadingVerificationResult(BaseModel):
    schema_version: str = "1.0.0"
    engine: str = "Seksun CNC semantic thread-cycle verification"
    status: Literal["passed", "warning", "failed"]
    operation_id: str
    metrics: ThreadingVerificationMetrics
    checks: list[ThreadingVerificationCheck]
    warnings: list[str] = Field(default_factory=list)


def verify_threading_cycle(
    operation: Operation,
    program: ToolpathProgram,
    simulation: TurningSimulationResult,
    *,
    tolerance_mm: float = 0.01,
) -> ThreadingVerificationResult:
    if operation.type != "turn_threading":
        raise ValueError("threading verification requires a turn_threading operation")
    commands = [
        command for channel in program.channels for command in channel.commands
        if command.type == "thread_cut" and command.operation_id == operation.id
    ]
    start_z = float(operation.parameters["start_z_mm"])
    end_z = float(operation.parameters["end_z_mm"])
    pitch = float(operation.parameters["pitch_mm"])
    major = float(operation.parameters["major_diameter_mm"])
    minor = float(operation.parameters["minor_diameter_mm"])
    depth = float(operation.parameters["thread_depth_mm"])
    pass_count = int(operation.parameters["pass_count"])
    length = abs(start_z - end_z)
    checks: list[ThreadingVerificationCheck] = []

    pass_indices = [int(command.parameters.get("pass_index", 0)) for command in commands]
    pass_ok = len(commands) == pass_count and pass_indices == list(range(1, pass_count + 1))
    checks.append(ThreadingVerificationCheck(
        id="pass_sequence", status="passed" if pass_ok else "failed",
        message=(
            f"已按顺序生成 {pass_count} 刀螺纹切削"
            if pass_ok else f"要求 {pass_count} 刀，实际生成 {len(commands)} 刀或刀次不连续"
        ),
    ))

    depth_ok = abs((major - minor) / 2 - depth) <= tolerance_mm
    checks.append(ThreadingVerificationCheck(
        id="diameter_depth_consistency", status="passed" if depth_ok else "failed",
        message="大径、小径与径向牙深一致" if depth_ok else "大径、小径与径向牙深不一致",
    ))

    final_target = float(commands[-1].parameters.get("target_diameter_mm", major)) if commands else major
    final_ok = abs(final_target - minor) <= tolerance_mm
    checks.append(ThreadingVerificationCheck(
        id="final_root_diameter", status="passed" if final_ok else "failed",
        message=(
            f"末刀目标直径 {final_target:.3f} mm 到达牙底小径"
            if final_ok else f"末刀目标直径 {final_target:.3f} mm 未到达小径 {minor:.3f} mm"
        ),
    ))

    length_status: Literal["passed", "warning", "failed"] = (
        "failed" if length <= 0 else "warning" if length < pitch * 2 else "passed"
    )
    checks.append(ThreadingVerificationCheck(
        id="thread_length", status=length_status,
        message=f"螺纹长度 {length:.3f} mm，约 {length / pitch:.2f} 个螺距",
    ))

    relief_strategy = str(operation.parameters.get("relief_strategy", "unreviewed"))
    relief_width = float(operation.parameters.get("relief_width_mm", 0))
    relief_ok = relief_strategy in {"runout", "thread_to_end"} or (
        relief_strategy == "groove" and relief_width >= pitch * 0.5
    )
    checks.append(ThreadingVerificationCheck(
        id="relief_space", status="passed" if relief_ok else "failed",
        message=(
            f"退刀策略 {relief_strategy} 已确认"
            if relief_ok else "退刀策略未确认，或退刀槽宽度小于半个螺距"
        ),
    ))

    midpoint = (start_z + end_z) / 2
    root_sample = min(simulation.samples, key=lambda item: abs(item.z - midpoint))
    simulated_ok = abs(root_sample.outer_radius * 2 - minor) <= max(tolerance_mm, simulation.resolution_mm)
    checks.append(ThreadingVerificationCheck(
        id="simulated_root_envelope", status="passed" if simulated_ok else "failed",
        message=(
            f"仿真牙底包络直径 {root_sample.outer_radius * 2:.3f} mm"
            if simulated_ok else "仿真材料状态未到达审核后的小径"
        ),
    ))

    sample_min = simulation.samples[0].z
    sample_max = simulation.samples[-1].z
    range_ok = sample_min - tolerance_mm <= end_z <= sample_max + tolerance_mm and sample_min - tolerance_mm <= start_z <= sample_max + tolerance_mm
    checks.append(ThreadingVerificationCheck(
        id="simulation_range", status="passed" if range_ok else "failed",
        message="仿真范围完整覆盖螺纹起止位置" if range_ok else "仿真范围未覆盖完整螺纹",
    ))

    status: Literal["passed", "warning", "failed"] = (
        "failed" if any(item.status == "failed" for item in checks)
        else "warning" if any(item.status == "warning" for item in checks)
        else "passed"
    )
    return ThreadingVerificationResult(
        status=status,
        operation_id=operation.id,
        metrics=ThreadingVerificationMetrics(
            start_z_mm=start_z,
            end_z_mm=end_z,
            thread_length_mm=length,
            pitch_mm=pitch,
            major_diameter_mm=major,
            minor_diameter_mm=minor,
            radial_depth_mm=depth,
            pass_count=pass_count,
            emitted_pass_count=len(commands),
        ),
        checks=checks,
        warnings=[
            "当前校核使用旋转牙底包络，不表达真实螺旋牙型、刀片成形误差或控制器加减速。",
            "结果仅用于 DRAFT 工艺审查，不能替代量规检验、空运行和首件试切。",
        ],
    )
