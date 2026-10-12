import json

import pytest

from app.workholding_evidence import assess_workholding, transfer_identity
from app.agent.l32_operation_trial import operation_signature
from test_l32_operation_trial import job, operation, rotational, snapshot


def back_operation():
    op = operation("BACK", 20, "turn_id_finishing")
    op.workpiece_side = "back"
    op.channel_id = "sub"
    op.spindle_id = "sub"
    return op


def test_front_only_does_not_require_transfer(tmp_path):
    result = assess_workholding(job(operation("FACE", 10, "turn_facing")), tmp_path, rotational(), snapshot())
    assert result["status"] == "not_required"


@pytest.mark.parametrize("late_cutoff", [False, True])
def test_back_geometry_cannot_substitute_for_prior_cutoff(tmp_path, late_cutoff):
    ops = [back_operation()]
    if late_cutoff:
        ops.append(operation("CUT", 30, "turn_cutoff"))
    result = assess_workholding(job(*ops), tmp_path, rotational(), snapshot())
    assert result["status"] == "blocked"
    assert any("转接顺序" in text for text in result["blockers"])
    assert result["production_ready"] is False


def test_old_transfer_artifact_and_malformed_evidence_are_not_confirmation(tmp_path):
    source = job(operation("CUT", 10, "turn_cutoff"), back_operation())
    (tmp_path / "turning-transfer-draft.json").write_text('{"status":"passed"}')
    assert assess_workholding(source, tmp_path, rotational(), snapshot())["status"] == "blocked"
    (tmp_path / "turning-transfer-evidence.json").write_text("{")
    assert assess_workholding(source, tmp_path, rotational(), snapshot())["status"] == "blocked"


def test_cutoff_edit_invalidates_bound_transfer_evidence(tmp_path):
    cut = operation("CUT", 10, "turn_cutoff")
    source, geometry, machine = job(cut, back_operation()), rotational(), snapshot()
    evidence = transfer_identity(source, geometry, machine)
    (tmp_path / "turning-transfer-evidence.json").write_text(json.dumps(evidence))
    cut.parameters["z_mm"] = -999
    result = assess_workholding(source, tmp_path, geometry, machine)
    assert any("已失效" in text for text in result["blockers"])


def test_direct_validation_stops_before_automatic_clamp_assumption(monkeypatch, tmp_path):
    import app.main as main
    import app.candidate_route as candidate
    source = job(back_operation())
    monkeypatch.setattr(main, "load_job", lambda _: source)
    monkeypatch.setattr(candidate, "get_candidate_route", lambda _: {"workholding": {
        "status": "blocked", "blockers": ["missing transfer"]}})
    monkeypatch.setattr(main, "_create_l32_whole_program_with_agent_loop", lambda _: pytest.fail("compiler must not run"))
    result = main.validate_l32_agent_plan(source.id)
    assert result["status"] == "blocked"
    assert result["completion_allowed"] is False


def test_current_transfer_draft_requires_accepted_cutoff_and_valid_state_order(tmp_path):
    from test_turning_draft import _transfer_request, _snapshot
    from app.rotational_features import RotationalFeatureAnalysis
    from app.turning_transfer import compile_synchronized_transfer_draft
    request = _transfer_request()
    request.operation.parameters["z_mm"] = -32
    machine = _snapshot()
    back = back_operation()
    back.sequence = request.operation.sequence + 10
    source = job(request.operation, back)
    source.machine_instance_id = machine.instance.id
    source.machine_configuration_hash = machine.configuration_hash
    geometry = RotationalFeatureAnalysis(source_file="shaft.step", axes=[], profiles=[request.profile], status="candidate")
    result = compile_synchronized_transfer_draft(source.id, request, machine)
    identity = transfer_identity(source, geometry, machine)
    evidence = {**identity, "profile_id": request.profile.id, "result": result.model_dump(mode="json")}
    path = tmp_path / "turning-transfer-evidence.json"
    path.write_text(json.dumps(evidence))
    missing = assess_workholding(source, tmp_path, geometry, machine)
    assert missing["status"] == "blocked"
    assert any("独立试算接受" in text for text in missing["blockers"])
    # Use the real compiler's material result as the accepted-prefix fixture.
    state = {"context_signature": identity["context_signature"], "accepted": [{
        "operation_id": request.operation.id, "operation_signature": operation_signature(request.operation),
        "cumulative_simulation": result.simulation.model_dump(mode="json")}]}
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(state))
    checked = assess_workholding(source, tmp_path, geometry, machine)
    assert checked["status"] == "draft_verified", checked
    assert checked["production_ready"] is False
    evidence["result"]["state_transitions"].reverse()
    path.write_text(json.dumps(evidence))
    assert assess_workholding(source, tmp_path, geometry, machine)["status"] == "blocked"
