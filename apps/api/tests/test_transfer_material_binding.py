import pytest

from app.agent.l32_operation_trial import accept_trial, run_independent_trial
from app.turning_transfer import compile_synchronized_transfer_draft
from app.workholding_evidence import bind_transfer_stock
from test_l32_operation_trial import job, operation, rotational, snapshot
from test_turning_draft import _transfer_request


def fixture():
    face = operation("FACE", 10, "turn_facing")
    cut = _transfer_request().operation
    cut.sequence = 20
    cut.parameters["z_mm"] = -32
    source, geometry, machine = job(face, cut), rotational(), snapshot()
    request = _transfer_request(operation=cut, profile=geometry.profiles[0], machine_instance_id=machine.instance.id)
    return source, geometry, machine, request


def test_transfer_cannot_skip_unaccepted_preceding_cutting():
    source, geometry, machine, request = fixture()
    with pytest.raises(ValueError, match="preceding operations"):
        bind_transfer_stock(source, geometry, machine, request, None)


def test_transfer_uses_real_previous_stock_even_when_caller_supplies_hollow_blank():
    source, geometry, machine, request = fixture()
    trial, state = run_independent_trial(job=source, operation_id="FACE", rotational=geometry, snapshot=machine, state=None)
    assert trial["can_accept"], trial
    state = accept_trial(job=source, state=state, trial=trial, operation_id="FACE")
    request = request.model_copy(update={"stock_radius_mm": 100, "initial_bore_radius_mm": 99,
                                         "z_min_mm": -100, "z_max_mm": 100, "resolution_mm": 10})
    bound, material_source = bind_transfer_stock(source, geometry, machine, request, state)
    assert bound.initial_bore_radius_mm == 0
    assert bound.stock_radius_mm == source.plan.stock["diameter_mm"] / 2
    assert [sample.model_dump() for sample in bound.initial_samples] == state["accepted"][0]["cumulative_simulation"]["samples"]
    assert material_source == {"source": "accepted_cumulative_stock", "source_operation_id": "FACE"}
    result = compile_synchronized_transfer_draft(source.id, bound, machine)
    preceding_volume = state["accepted"][0]["cumulative_simulation"]["metrics"]["remaining_volume_mm3"]
    assert result.simulation.metrics.initial_volume_mm3 == pytest.approx(preceding_volume, abs=1e-5)
    assert result.simulation.metrics.remaining_volume_mm3 <= preceding_volume
    source.plan.setups[0].operations[0].parameters["face_z_mm"] = -2
    with pytest.raises(ValueError, match="preceding operations"):
        bind_transfer_stock(source, geometry, machine, request, state)


def test_first_cutoff_uses_solid_process_stock():
    _, geometry, machine, request = fixture()
    source = job(request.operation)
    request = request.model_copy(update={"initial_bore_radius_mm": 10, "initial_samples": []})
    bound, material_source = bind_transfer_stock(source, geometry, machine, request, None)
    assert bound.initial_samples is None and bound.initial_bore_radius_mm == 0
    assert material_source["source"] == "process_solid_stock"
