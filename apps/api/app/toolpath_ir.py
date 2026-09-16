from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator


ToolpathCommandType = Literal[
    "rapid_move", "feed_move", "arc_move", "dwell",
    "thread_cut", "drill_cycle", "tap_cycle",
    "select_tool", "set_geometry_offset", "set_wear_offset",
    "set_rpm", "set_constant_surface_speed",
    "set_feed_per_revolution", "set_feed_per_minute",
    "spindle_start", "spindle_stop", "spindle_orient",
    "coolant_on", "coolant_off", "chuck_open", "chuck_close",
    "guide_bushing_mode", "bar_feed", "sub_spindle_approach",
    "spindle_phase_sync", "pickoff", "cutoff", "channel_wait",
    "sync_barrier", "part_eject",
]

ParameterValue = float | int | str | bool
MOTION_COMMANDS = {"rapid_move", "feed_move", "arc_move"}


class ToolpathTrace(BaseModel):
    feature_ids: list[str] = Field(default_factory=list)
    source: str
    generator_version: str


class ToolpathCommand(BaseModel):
    sequence: int = Field(ge=1)
    type: ToolpathCommandType
    channel_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    axes: dict[str, float] = Field(default_factory=dict)
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    safety_requirements: list[str] = Field(default_factory=list)
    trace: ToolpathTrace

    @model_validator(mode="after")
    def validate_command_payload(self) -> "ToolpathCommand":
        if self.type in MOTION_COMMANDS and not self.axes:
            raise ValueError(f"{self.type} requires at least one target axis")
        if self.type == "arc_move" and not ({"i", "k"} <= set(self.parameters) or "radius" in self.parameters):
            raise ValueError("arc_move requires I/K center offsets or a radius")
        if self.type == "sync_barrier" and not str(self.parameters.get("barrier_id", "")).strip():
            raise ValueError("sync_barrier requires barrier_id")
        if self.type == "thread_cut":
            required = {"start_z_mm", "end_z_mm", "pitch_mm", "major_diameter_mm", "minor_diameter_mm", "pass_count"}
            if not required <= set(self.parameters):
                raise ValueError("thread_cut requires complete thread geometry and pass parameters")
        if self.type in {"drill_cycle", "tap_cycle"}:
            required = {"start_z_mm", "end_z_mm", "retract_z_mm"}
            if not required <= set(self.parameters):
                raise ValueError(f"{self.type} requires start, end, and retract positions")
        return self


class ToolpathChannel(BaseModel):
    id: str = Field(min_length=1)
    commands: list[ToolpathCommand]

    @model_validator(mode="after")
    def validate_sequence(self) -> "ToolpathChannel":
        sequences = [item.sequence for item in self.commands]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError(f"channel {self.id} command sequences must be unique and ordered")
        if any(item.channel_id != self.id for item in self.commands):
            raise ValueError(f"channel {self.id} contains a command assigned to another channel")
        return self


class ToolpathProgram(BaseModel):
    schema_version: str = "1.0.0"
    units: Literal["mm"] = "mm"
    coordinate_convention: Literal["diameter-x_z", "cartesian"]
    machine_snapshot_hash: str = Field(min_length=64, max_length=64)
    plan_revision: int = Field(ge=1)
    channels: list[ToolpathChannel]

    @model_validator(mode="after")
    def validate_channels_and_barriers(self) -> "ToolpathProgram":
        channel_ids = [item.id for item in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("toolpath channel ids must be unique")
        barriers: dict[str, set[str]] = {}
        for channel in self.channels:
            for command in channel.commands:
                if command.type != "sync_barrier":
                    continue
                barrier_id = str(command.parameters["barrier_id"])
                barriers.setdefault(barrier_id, set()).add(channel.id)
        unmatched = [barrier_id for barrier_id, participants in barriers.items() if len(participants) < 2]
        if unmatched:
            raise ValueError(f"unpaired synchronization barriers: {', '.join(sorted(unmatched))}")
        return self


def toolpath_program_hash(program: ToolpathProgram) -> str:
    canonical = json.dumps(
        program.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
