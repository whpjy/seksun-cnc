from __future__ import annotations

from math import dist
from typing import Literal

from pydantic import BaseModel, Field

from .toolpath_ir import ToolpathCommand, ToolpathProgram


class ChannelTimelineEvent(BaseModel):
    channel_id: str
    command_sequence: int
    operation_id: str
    command_type: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    wait_seconds: float = Field(default=0, ge=0)
    barrier_id: str | None = None


class ChannelTimelineResult(BaseModel):
    schema_version: str = "1.0.0"
    status: Literal["scheduled"] = "scheduled"
    estimated_cycle_seconds: float = Field(ge=0)
    channel_end_seconds: dict[str, float]
    barrier_order: list[str]
    events: list[ChannelTimelineEvent]
    warnings: list[str] = Field(default_factory=list)


_FIXED_SECONDS = {
    "select_tool": 2.0,
    "spindle_start": 1.5,
    "spindle_stop": 1.0,
    "spindle_phase_sync": 1.0,
    "chuck_open": 1.0,
    "chuck_close": 1.5,
    "pickoff": 0.5,
    "cutoff": 0.1,
    "sub_spindle_approach": 0.5,
}


def _duration(
    command: ToolpathCommand,
    position: dict[str, float],
    modal: dict[str, float],
    *,
    rapid_mm_min: float,
) -> float:
    if command.type == "set_feed_per_revolution":
        modal["feed_mm_rev"] = float(command.parameters["feed_mm_rev"])
    elif command.type == "set_feed_per_minute":
        modal["feed_mm_min"] = float(command.parameters["feed_mm_min"])
    elif command.type == "set_rpm":
        modal["rpm"] = float(command.parameters["rpm"])
    elif command.type == "set_constant_surface_speed":
        modal["rpm"] = float(command.parameters["maximum_spindle_rpm"])
    if command.type == "dwell":
        return max(float(command.parameters.get("seconds", 0)), 0)
    if command.type not in {"rapid_move", "feed_move", "arc_move"}:
        return _FIXED_SECONDS.get(command.type, 0.05)
    axes = {key.upper(): float(value) for key, value in command.axes.items()}
    keys = sorted(set(position) | set(axes))
    start = [position.get(key, axes.get(key, 0)) for key in keys]
    end = [axes.get(key, position.get(key, 0)) for key in keys]
    travel = dist(start, end) if keys else 0
    position.update(axes)
    if command.type == "rapid_move":
        feed = rapid_mm_min
    else:
        feed = modal.get("feed_mm_min", 0)
        if feed <= 0:
            feed = modal.get("feed_mm_rev", 0) * modal.get("rpm", 0)
        if feed <= 0:
            raise ValueError(f"cannot estimate feed duration for {command.operation_id} sequence {command.sequence}")
    return travel / feed * 60 if travel > 0 else 0.01


def schedule_channel_timeline(
    program: ToolpathProgram,
    *,
    rapid_mm_min: float = 32_000,
) -> ChannelTimelineResult:
    if rapid_mm_min <= 0:
        raise ValueError("rapid rate must be positive")
    if len(program.channels) < 2:
        raise ValueError("multi-channel timeline requires at least two channels")
    barrier_orders = {
        channel.id: [
            str(command.parameters["barrier_id"])
            for command in channel.commands if command.type == "sync_barrier"
        ]
        for channel in program.channels
    }
    reference = barrier_orders[program.channels[0].id]
    mismatched = [channel_id for channel_id, order in barrier_orders.items() if order != reference]
    if mismatched:
        raise ValueError(
            "synchronization barrier order can deadlock: "
            + "; ".join(f"{channel_id}={barrier_orders[channel_id]}" for channel_id in barrier_orders)
        )

    clocks = {channel.id: 0.0 for channel in program.channels}
    positions = {channel.id: {} for channel in program.channels}
    modals = {channel.id: {} for channel in program.channels}
    events: list[ChannelTimelineEvent] = []
    indices = {channel.id: 0 for channel in program.channels}
    channel_commands = {channel.id: channel.commands for channel in program.channels}
    for barrier_id in reference:
        arrivals: dict[str, tuple[ToolpathCommand, float]] = {}
        for channel in program.channels:
            commands = channel_commands[channel.id]
            while indices[channel.id] < len(commands):
                command = commands[indices[channel.id]]
                indices[channel.id] += 1
                if command.type == "sync_barrier":
                    if str(command.parameters["barrier_id"]) != barrier_id:
                        raise ValueError("synchronization barrier order can deadlock")
                    arrivals[channel.id] = (command, clocks[channel.id])
                    break
                start = clocks[channel.id]
                duration = _duration(
                    command, positions[channel.id], modals[channel.id],
                    rapid_mm_min=rapid_mm_min,
                )
                clocks[channel.id] += duration
                events.append(ChannelTimelineEvent(
                    channel_id=channel.id, command_sequence=command.sequence,
                    operation_id=command.operation_id, command_type=command.type,
                    start_seconds=start, end_seconds=clocks[channel.id], duration_seconds=duration,
                ))
        release = max(arrival for _, arrival in arrivals.values())
        for channel_id, (command, arrival) in arrivals.items():
            events.append(ChannelTimelineEvent(
                channel_id=channel_id, command_sequence=command.sequence,
                operation_id=command.operation_id, command_type=command.type,
                start_seconds=arrival, end_seconds=release, duration_seconds=0,
                wait_seconds=release - arrival, barrier_id=barrier_id,
            ))
            clocks[channel_id] = release

    for channel in program.channels:
        commands = channel_commands[channel.id]
        while indices[channel.id] < len(commands):
            command = commands[indices[channel.id]]
            indices[channel.id] += 1
            if command.type == "sync_barrier":
                raise ValueError("unexpected synchronization barrier after scheduling")
            start = clocks[channel.id]
            duration = _duration(
                command, positions[channel.id], modals[channel.id],
                rapid_mm_min=rapid_mm_min,
            )
            clocks[channel.id] += duration
            events.append(ChannelTimelineEvent(
                channel_id=channel.id, command_sequence=command.sequence,
                operation_id=command.operation_id, command_type=command.type,
                start_seconds=start, end_seconds=clocks[channel.id], duration_seconds=duration,
            ))
    events.sort(key=lambda item: (item.start_seconds, item.channel_id, item.command_sequence))
    return ChannelTimelineResult(
        estimated_cycle_seconds=round(max(clocks.values()), 3),
        channel_end_seconds={key: round(value, 3) for key, value in clocks.items()},
        barrier_order=reference,
        events=events,
        warnings=[
            "时长为运动距离、模态进给和固定辅助时间的工程估算，不包含控制器加减速、换刀宏程序及现场延时。",
        ],
    )
