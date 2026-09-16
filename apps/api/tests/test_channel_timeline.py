import pytest

from app.channel_timeline import schedule_channel_timeline
from app.toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, ToolpathTrace


def _command(sequence: int, channel: str, command_type: str, barrier: str | None = None):
    return ToolpathCommand(
        sequence=sequence,
        type=command_type,
        channel_id=channel,
        operation_id="OP40",
        axes={"Z": -10} if command_type == "rapid_move" else {},
        parameters={"barrier_id": barrier} if barrier else {},
        trace=ToolpathTrace(source="test", generator_version="1"),
    )


def test_timeline_synchronizes_channels_and_records_wait_time() -> None:
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash="a" * 64,
        plan_revision=1,
        channels=[
            ToolpathChannel(id="main", commands=[
                _command(1, "main", "rapid_move"),
                _command(2, "main", "sync_barrier", "SYNC-1"),
            ]),
            ToolpathChannel(id="sub", commands=[
                _command(1, "sub", "sync_barrier", "SYNC-1"),
            ]),
        ],
    )

    timeline = schedule_channel_timeline(program)

    barriers = [item for item in timeline.events if item.barrier_id == "SYNC-1"]
    assert timeline.status == "scheduled"
    assert timeline.barrier_order == ["SYNC-1"]
    assert len(barriers) == 2
    assert next(item for item in barriers if item.channel_id == "sub").wait_seconds > 0


def test_timeline_rejects_opposite_barrier_order_as_deadlock() -> None:
    program = ToolpathProgram(
        coordinate_convention="diameter-x_z",
        machine_snapshot_hash="b" * 64,
        plan_revision=1,
        channels=[
            ToolpathChannel(id="main", commands=[
                _command(1, "main", "sync_barrier", "A"),
                _command(2, "main", "sync_barrier", "B"),
            ]),
            ToolpathChannel(id="sub", commands=[
                _command(1, "sub", "sync_barrier", "B"),
                _command(2, "sub", "sync_barrier", "A"),
            ]),
        ],
    )

    with pytest.raises(ValueError, match="deadlock"):
        schedule_channel_timeline(program)
