import pytest

from app.continuous_turning_simulation import simulate_continuous_whole_part
from app.toolpath_ir import ToolpathCommand
from app.turning_transfer import compile_synchronized_transfer_draft
from test_turning_draft import _transfer_request, _snapshot


def program(cutoff_id="SEPARATE-80"):
    request = _transfer_request()
    request.operation.id = cutoff_id
    request.operation.parameters["z_mm"] = -32
    compiled = compile_synchronized_transfer_draft("a" * 32, request, _snapshot())
    result = compiled.toolpath.model_copy(deep=True)
    sub = result.channels[1]
    next_sequence = sub.commands[-1].sequence + 1
    # OP40 is deliberately a real back operation, not the transfer operation.
    for offset, (kind, axes, parameters) in enumerate([
        ("rapid_move", {"X": 24, "Z": 2}, {}),
        ("feed_move", {"X": 0, "Z": 0}, {"cut_side": "facing", "retain_direction": "negative_z"}),
    ]):
        sub.commands.append(ToolpathCommand(sequence=next_sequence + offset, type=kind,
            channel_id="sub", operation_id="OP40", axes=axes, parameters=parameters,
            trace=sub.commands[0].trace))
    return result


def simulate(source):
    return simulate_continuous_whole_part(source, stock_radius_mm=12, initial_bore_radius_mm=0,
        main_z_min_mm=-34, main_z_max_mm=2, transfer_datum_z_mm=-30, resolution_mm=.1)


def test_actual_cutoff_id_and_back_operation_are_preserved():
    result = simulate(program())
    assert result.status == "passed"
    assert result.cutoff_operation_id == "SEPARATE-80"
    assert [s.operation_id for s in result.stage_snapshots] == ["SEPARATE-80", "OP40"]
    for before, transferred in zip(result.stage_snapshots[-1].before_samples, result.transferred_sub_frame_samples, strict=True):
        assert (before.z, before.outer_radius, before.inner_radius) == pytest.approx(
            (transferred.z, transferred.outer_radius, transferred.inner_radius), abs=1e-6)
    assert result.stage_snapshots[-1].metrics.initial_volume_mm3 == pytest.approx(result.transferred_volume_mm3, abs=1e-5)
    assert result.final_volume_mm3 <= result.transferred_volume_mm3
    assert result.transferred_volume_mm3 <= result.main_frame_after_cutoff.metrics.remaining_volume_mm3 + 1e-6
    assert next(c for c in result.checks if c.id == "transfer_preserves_cut_stock").status == "passed"


def test_renaming_cutoff_does_not_change_material_result():
    old, renamed = simulate(program("OP40")), simulate(program("CUSTOM-CUT"))
    assert old.sub_frame_final.samples == renamed.sub_frame_final.samples
    assert old.final_volume_mm3 == renamed.final_volume_mm3


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_separation_is_rejected(count):
    source = program()
    channel = source.channels[0]
    cutoff = next(c for c in channel.commands if c.type == "cutoff")
    if count == 0:
        channel.commands = [c for c in channel.commands if c.type != "cutoff"]
    else:
        channel.commands.append(cutoff.model_copy(update={"sequence": channel.commands[-1].sequence + 1, "operation_id": "SECOND-CUT"}))
    with pytest.raises(ValueError, match="exactly one cutoff"):
        simulate(source)
