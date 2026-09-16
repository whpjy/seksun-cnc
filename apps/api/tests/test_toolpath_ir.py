import pytest
from pydantic import ValidationError

from app.toolpath_ir import (
    ToolpathChannel,
    ToolpathCommand,
    ToolpathProgram,
    ToolpathTrace,
    toolpath_program_hash,
)


def command(sequence: int, channel_id: str = "main", **updates) -> ToolpathCommand:
    values = {
        "sequence": sequence,
        "type": "feed_move",
        "channel_id": channel_id,
        "operation_id": "OP10",
        "axes": {"X": 20.0, "Z": -5.0},
        "trace": ToolpathTrace(source="turning", generator_version="0.1.0"),
    }
    values.update(updates)
    return ToolpathCommand.model_validate(values)


def test_single_channel_turning_program_is_valid_and_hashable() -> None:
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash="a" * 64,
        plan_revision=1,
        channels=[ToolpathChannel(id="main", commands=[command(10), command(20)])],
    )

    assert len(toolpath_program_hash(program)) == 64


def test_motion_command_requires_target_axes() -> None:
    with pytest.raises(ValidationError, match="requires at least one target axis"):
        command(10, axes={})


def test_channel_rejects_commands_assigned_to_another_channel() -> None:
    with pytest.raises(ValidationError, match="assigned to another channel"):
        ToolpathChannel(id="main", commands=[command(10, channel_id="sub")])


def test_program_rejects_unpaired_synchronization_barrier() -> None:
    barrier = command(
        10, type="sync_barrier", axes={}, parameters={"barrier_id": "SYNC-1"},
    )
    with pytest.raises(ValidationError, match="unpaired synchronization barriers"):
        ToolpathProgram(
            coordinate_convention="diameter-x_z",
            machine_snapshot_hash="b" * 64,
            plan_revision=1,
            channels=[ToolpathChannel(id="main", commands=[barrier])],
        )


def test_program_accepts_barrier_present_on_both_channels() -> None:
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash="c" * 64,
        plan_revision=1,
        channels=[
            ToolpathChannel(id="main", commands=[command(10, type="sync_barrier", axes={}, parameters={"barrier_id": "SYNC-1"})]),
            ToolpathChannel(id="sub", commands=[command(10, channel_id="sub", type="sync_barrier", axes={}, parameters={"barrier_id": "SYNC-1"})]),
        ],
    )

    assert len(program.channels) == 2


def test_thread_cycle_rejects_incomplete_geometry() -> None:
    with pytest.raises(ValidationError, match="complete thread geometry"):
        command(10, type="thread_cut", axes={}, parameters={"pitch_mm": 1})
