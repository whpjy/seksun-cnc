from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .toolpath_ir import ToolpathChannel, ToolpathProgram
from .turning_simulation import (
    TurningSimulationMetrics, TurningSimulationResult, TurningStockSample,
    simulate_turning_stock,
)


class ContinuousMaterialCheck(BaseModel):
    id: str
    status: Literal["passed", "failed"]
    message: str
    measured_value: float | int | str | None = None


class TurningStageSnapshot(BaseModel):
    operation_id: str
    channel_id: Literal["main", "sub"]
    source_frame: Literal["main", "sub"]
    before_samples: list[TurningStockSample]
    after_samples: list[TurningStockSample]
    metrics: TurningSimulationMetrics


class ContinuousTurningSimulationResult(BaseModel):
    schema_version: str = "1.0.0"
    status: Literal["passed", "failed"]
    main_frame_after_cutoff: TurningSimulationResult
    transferred_sub_frame_samples: list[TurningStockSample]
    sub_frame_final: TurningSimulationResult
    stage_snapshots: list[TurningStageSnapshot] = Field(default_factory=list)
    checks: list[ContinuousMaterialCheck]
    initial_volume_mm3: float = Field(ge=0)
    transferred_volume_mm3: float = Field(ge=0)
    final_volume_mm3: float = Field(ge=0)
    total_removed_volume_mm3: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


def _single_channel(program: ToolpathProgram, channel_id: str) -> ToolpathProgram:
    channel = next((item for item in program.channels if item.id == channel_id), None)
    if channel is None:
        raise ValueError(f"whole-part program is missing channel: {channel_id}")
    commands = [item for item in channel.commands if item.type != "sync_barrier"]
    return ToolpathProgram(
        coordinate_convention=program.coordinate_convention,
        machine_snapshot_hash=program.machine_snapshot_hash,
        plan_revision=program.plan_revision,
        channels=[ToolpathChannel(id=channel_id, commands=commands)],
    )


def _operation_program(
    program: ToolpathProgram,
    channel_id: str,
    operation_id: str,
) -> ToolpathProgram:
    channel = next((item for item in program.channels if item.id == channel_id), None)
    if channel is None:
        raise ValueError(f"whole-part program is missing channel: {channel_id}")
    commands = [
        item for item in channel.commands
        if item.operation_id == operation_id and item.type != "sync_barrier"
    ]
    return ToolpathProgram(
        coordinate_convention=program.coordinate_convention,
        machine_snapshot_hash=program.machine_snapshot_hash,
        plan_revision=program.plan_revision,
        channels=[ToolpathChannel(id=channel_id, commands=commands)],
    )


def _operation_ids(program: ToolpathProgram, channel_id: str) -> list[str]:
    channel = next((item for item in program.channels if item.id == channel_id), None)
    if channel is None:
        return []
    return list(dict.fromkeys(
        item.operation_id for item in channel.commands if item.type != "sync_barrier"
    ))


def _simulate_stage_snapshots(
    program: ToolpathProgram,
    *,
    channel_id: Literal["main", "sub"],
    stock_radius_mm: float,
    initial_bore_radius_mm: float,
    z_min_mm: float,
    z_max_mm: float,
    resolution_mm: float,
    initial_samples: list[TurningStockSample] | None = None,
    excluded_operation_ids: set[str] | None = None,
) -> tuple[list[TurningStageSnapshot], list[TurningStockSample]]:
    excluded = excluded_operation_ids or set()
    state = initial_samples
    snapshots: list[TurningStageSnapshot] = []
    for operation_id in _operation_ids(program, channel_id):
        if operation_id in excluded:
            continue
        stage_program = _operation_program(program, channel_id, operation_id)
        before_result = simulate_turning_stock(
            _operation_program(program, channel_id, "__empty__"),
            stock_radius_mm=stock_radius_mm,
            initial_bore_radius_mm=initial_bore_radius_mm,
            z_min_mm=z_min_mm,
            z_max_mm=z_max_mm,
            resolution_mm=resolution_mm,
            initial_samples=state,
        )
        result = simulate_turning_stock(
            stage_program,
            stock_radius_mm=stock_radius_mm,
            initial_bore_radius_mm=initial_bore_radius_mm,
            z_min_mm=z_min_mm,
            z_max_mm=z_max_mm,
            resolution_mm=resolution_mm,
            initial_samples=before_result.samples,
        )
        snapshots.append(TurningStageSnapshot(
            operation_id=operation_id,
            channel_id=channel_id,
            source_frame=channel_id,
            before_samples=before_result.samples,
            after_samples=result.samples,
            metrics=result.metrics,
        ))
        state = result.samples
    return snapshots, state or []


def _volume(samples: list[TurningStockSample]) -> float:
    from math import pi

    ordered = sorted(samples, key=lambda item: item.z)
    total = 0.0
    for left, right in zip(ordered, ordered[1:]):
        left_area = pi * max(left.outer_radius ** 2 - left.inner_radius ** 2, 0)
        right_area = pi * max(right.outer_radius ** 2 - right.inner_radius ** 2, 0)
        total += (left_area + right_area) * 0.5 * (right.z - left.z)
    return max(total, 0)


def simulate_continuous_whole_part(
    program: ToolpathProgram,
    *,
    stock_radius_mm: float,
    initial_bore_radius_mm: float,
    main_z_min_mm: float,
    main_z_max_mm: float,
    transfer_datum_z_mm: float,
    resolution_mm: float,
) -> ContinuousTurningSimulationResult:
    main_program = _single_channel(program, "main")
    sub_program = _single_channel(program, "sub")
    main_result = simulate_turning_stock(
        main_program,
        stock_radius_mm=stock_radius_mm,
        initial_bore_radius_mm=initial_bore_radius_mm,
        z_min_mm=main_z_min_mm,
        z_max_mm=main_z_max_mm,
        resolution_mm=resolution_mm,
    )
    main_stage_snapshots, _ = _simulate_stage_snapshots(
        program,
        channel_id="main",
        stock_radius_mm=stock_radius_mm,
        initial_bore_radius_mm=initial_bore_radius_mm,
        z_min_mm=main_z_min_mm,
        z_max_mm=main_z_max_mm,
        resolution_mm=resolution_mm,
    )
    retained_interior = [
        TurningStockSample(
            z=round(transfer_datum_z_mm - sample.z, 6),
            outer_radius=sample.outer_radius,
            inner_radius=sample.inner_radius,
        )
        for sample in main_result.samples
        if sample.z > transfer_datum_z_mm + 1e-9
    ]
    retained_interior.sort(key=lambda item: item.z)
    if len(retained_interior) < 2:
        raise ValueError("cutoff transfer produced fewer than two retained material samples")
    datum_side = max(retained_interior, key=lambda item: item.z)
    retained = [
        *retained_interior,
        TurningStockSample(
            z=0,
            outer_radius=datum_side.outer_radius,
            inner_radius=datum_side.inner_radius,
        ),
    ]
    retained.sort(key=lambda item: item.z)
    sub_result = simulate_turning_stock(
        sub_program,
        stock_radius_mm=max(item.outer_radius for item in retained),
        initial_bore_radius_mm=min(item.inner_radius for item in retained),
        z_min_mm=retained[0].z,
        z_max_mm=retained[-1].z,
        resolution_mm=resolution_mm,
        initial_samples=retained,
    )
    sub_stage_snapshots, _ = _simulate_stage_snapshots(
        program,
        channel_id="sub",
        stock_radius_mm=max(item.outer_radius for item in retained),
        initial_bore_radius_mm=min(item.inner_radius for item in retained),
        z_min_mm=retained[0].z,
        z_max_mm=retained[-1].z,
        resolution_mm=resolution_mm,
        initial_samples=retained,
        excluded_operation_ids={"OP40"},
    )
    initial_volume = main_result.metrics.initial_volume_mm3
    transferred_volume = _volume(retained)
    final_volume = sub_result.metrics.remaining_volume_mm3
    checks = [
        ContinuousMaterialCheck(
            id="transfer_volume_nonincrease",
            status="passed" if transferred_volume <= initial_volume + 1e-6 else "failed",
            message="接料材料体积不得超过初始棒料体积",
            measured_value=round(transferred_volume - initial_volume, 6),
        ),
        ContinuousMaterialCheck(
            id="backside_volume_nonincrease",
            status="passed" if final_volume <= transferred_volume + 1e-6 else "failed",
            message="背面加工不得凭空增加材料",
            measured_value=round(final_volume - transferred_volume, 6),
        ),
        ContinuousMaterialCheck(
            id="retained_part_nonempty",
            status="passed" if final_volume > 1e-6 else "failed",
            message="接料与背面加工后必须保留非零工件体积",
            measured_value=round(final_volume, 6),
        ),
        ContinuousMaterialCheck(
            id="radial_topology_valid",
            status="passed" if all(
                item.inner_radius <= item.outer_radius + 1e-9
                for item in sub_result.samples
            ) else "failed",
            message="所有连续材料采样必须满足内半径不大于外半径",
            measured_value=len(sub_result.samples),
        ),
    ]
    status = "failed" if any(item.status == "failed" for item in checks) else "passed"
    return ContinuousTurningSimulationResult(
        status=status,
        main_frame_after_cutoff=main_result,
        transferred_sub_frame_samples=retained,
        sub_frame_final=sub_result,
        stage_snapshots=[*main_stage_snapshots, *sub_stage_snapshots],
        checks=checks,
        initial_volume_mm3=round(initial_volume, 6),
        transferred_volume_mm3=round(transferred_volume, 6),
        final_volume_mm3=round(final_volume, 6),
        total_removed_volume_mm3=round(max(initial_volume - final_volume, 0), 6),
        warnings=[
            "连续材料状态采用轴对称 Z-R 离散模型；切断刀缝、夹头覆盖区与弹性变形仍为近似。",
            "背轴坐标转换保留材料拓扑与体积守恒检查，但不替代三维机床运动学和碰撞仿真。",
        ],
    )
