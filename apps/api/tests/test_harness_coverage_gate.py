import json

import pytest

import app.main as main
from app.agent.l32_operation_trial import accept_trial, run_independent_trial
from app.models import CylindricalFeature, Vec3
from test_l32_operation_trial import job, operation, rotational, snapshot


@pytest.mark.parametrize("verified", [False, True])
def test_regional_finishing_needs_full_material_evidence_and_no_dummy_roughing(verified):
    from app.coverage import evaluate_plan_coverage
    planned = operation("OP10", 10, "turn_od_finishing")
    planned.parameters["profile_region_complete"] = False
    source = job(planned)
    source.plan.stock.update(type="round_bar", profile_review_state="ai_provisional",
        verified_rotational_profile_ids=["RP-1"] if verified else [])
    coverage = evaluate_plan_coverage(source.analysis, source.plan)
    target = next(target for target in coverage.targets if target.kind == "outer_profile")
    assert target.required_operation_types == ["turn_od_finishing"]
    assert (target.state == "covered") is verified


@pytest.mark.parametrize("coaxial", [False, True])
def test_finalization_distinguishes_available_boring_from_unsupported_radial_drilling(monkeypatch, tmp_path, coaxial):
    planned = operation("OP10", 10, "turn_od_finishing")
    source = job(planned)
    source.plan.stock.update(type="round_bar", profile_review_state="accepted")
    geometry = rotational()
    for point in geometry.profiles[0].points:
        point.radius = 6
    hole = CylindricalFeature(id="HF-1", kind="hole", diameter=4, radius=2, length=10,
        center=Vec3(x=0, y=0, z=-15), axis=Vec3(x=0, y=0, z=1) if coaxial else Vec3(x=1, y=0, z=0), review_state="accepted")
    source.analysis.cylindrical_features = [hole]
    machine = snapshot()
    trial, state = run_independent_trial(job=source, operation_id="OP10", rotational=geometry, snapshot=machine, state=None)
    assert trial["can_accept"], trial
    accepted = accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    for name, value in {"rotational-features.json": geometry.model_dump(mode="json"), "machine-configuration.json": machine.model_dump(mode="json"), "harness-l32-operation-state.json": accepted}.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(main, "job_directory", lambda _: tmp_path)
    monkeypatch.setattr(main, "load_job", lambda _: source)
    monkeypatch.setattr(main, "publish_job_event", lambda *args, **kwargs: None)
    result = main.finalize_harness_l32_process_plan(source.id)
    assert result["coverage"]["covered_count"] == 1
    assert result["coverage"]["target_count"] == 2
    assert result["completion_allowed"] is False
    assert result["terminal"] is False
    assert result["status"] == "plan_incomplete"
    assert result["must_continue_planning"] is True
    # Publishing derived coverage evidence must not invalidate the stock prefix.
    summary = main.get_l32_independent_operation_state(source.id)
    assert summary["accepted_operation_ids"] == ["OP10"]
    assert summary["planning_status"] == result["status"]


def test_successful_finalization_records_current_gcode_authorization(monkeypatch, tmp_path):
    source = job(operation("OP10", 10, "turn_od_finishing"))
    source.plan.stock.update(type="round_bar", profile_review_state="accepted")
    geometry, machine = rotational(), snapshot()
    for point in geometry.profiles[0].points:
        point.radius = 6
    trial, state = run_independent_trial(job=source, operation_id="OP10", rotational=geometry, snapshot=machine, state=None)
    assert trial["can_accept"], trial
    accepted = accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    for name, value in {"rotational-features.json": geometry.model_dump(mode="json"), "machine-configuration.json": machine.model_dump(mode="json"), "harness-l32-operation-state.json": accepted}.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(main, "job_directory", lambda _: tmp_path)
    monkeypatch.setattr(main, "load_job", lambda _: source)
    monkeypatch.setattr(main, "validate_l32_agent_plan", lambda _: {"status": "passed"})
    result = main.finalize_harness_l32_process_plan(source.id)
    assert result["completion_allowed"] is True
    assert result["context_signature"] == accepted["context_signature"]
    assert result["nc_source_signature"] == main._generic_nc_source(source.id)[2]
    assert main.get_gcode_planning_status(source.id)["allowed"] is True
    source.plan.setups[0].operations[0].parameters["face_z_mm"] = -1
    assert main.get_gcode_planning_status(source.id)["allowed"] is False
