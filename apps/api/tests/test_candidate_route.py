import pytest
from fastapi import HTTPException
from app import candidate_route as route_module
from app.candidate_route import CandidateRoute, propose_route, route_progress, save_candidate_route
from app.models import CylindricalFeature, Vec3
from app.rotational_features import RotationalProfile, RotationalProfilePoint
from test_l32_operation_trial import job, operation, rotational


def source():
    source = job(operation("OP10", 10, "turn_facing"))
    geometry = rotational()
    geometry.profiles.append(RotationalProfile(id="RP-INNER", axis_id="RA-1", side="inner",
        extraction_method="exact_section", review_state="review", confidence=.9,
        points=[RotationalProfilePoint(z=-30, radius=4), RotationalProfilePoint(z=0, radius=4)]))
    source.analysis.cylindrical_features = [
        CylindricalFeature(id="HF-AXIAL", kind="hole", radius=4, diameter=8, length=30,
            center=Vec3(x=0, y=0, z=-15), axis=Vec3(x=0, y=0, z=1), review_state="accepted"),
        CylindricalFeature(id="HF-SIDE", kind="hole", radius=1, diameter=2, length=3,
            center=Vec3(x=3, y=0, z=-15), axis=Vec3(x=1, y=0, z=0), review_state="accepted"),
    ]
    return source, geometry


@pytest.mark.parametrize("step_id,name,side", [("BACK-MAIN", "后序", None), ("CUSTOM", "背面装夹后加工", None), ("CUSTOM", "后序", "back")])
def test_custom_back_step_never_matches_front_operation(step_id, name, side):
    source_job, geometry = source()
    route = CandidateRoute(steps=[dict(id=step_id, name=name, reason="back processing",
        feature_ids=["RP-1"], operation_types=["turn_facing"], workpiece_side=side)])
    result = route_progress(source_job, geometry, route, None)["steps"][0]
    assert result["status"] == "pending" and result["operation_ids"] == []
    assert result["workpiece_side"] == "back"
    source_job.plan.setups[0].operations[0].workpiece_side = "back"
    assert route_progress(source_job, geometry, route, None)["steps"][0]["operation_ids"] == ["OP10"]


def test_required_back_goal_without_operations_keeps_transfer_blocker(tmp_path, monkeypatch):
    from app import main
    from test_l32_operation_trial import snapshot
    source_job, geometry = source()
    route = CandidateRoute(steps=[dict(id="BACK-MAIN", name="Back goal", reason="required",
        feature_ids=["RP-1"],operation_types=["turn_facing"],conditional=False)])
    (tmp_path / "candidate-route.json").write_text(route.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(route_module,"_inputs",lambda _: (source_job,tmp_path,geometry))
    monkeypatch.setattr(main,"load_machine_snapshot",lambda _: snapshot())
    result = route_module.get_candidate_route("job")
    assert result["workholding"]["status"] == "blocked"
    assert result["steps"][0]["operation_ids"] == []


def test_route_keeps_unimplemented_side_holes_and_bore_prerequisites():
    source_job, geometry = source()
    proposal = propose_route(source_job, geometry)
    steps = {step.id: step for step in proposal.steps}
    assert steps["PILOT"].feature_ids == ["HF-AXIAL"]
    assert steps["INNER-FINISH"].prerequisite_ids == ["PILOT"]
    assert steps["SIDE-HOLES"].feature_ids == ["HF-SIDE"]
    assert steps["SIDE-HOLES"].blockers
    assert steps["BACK"].conditional
    status = {step["id"]: step for step in route_progress(source_job, geometry, proposal, None)["steps"]}
    assert status["FACE"]["status"] == "established"
    assert status["BACK"]["status"] == "pending"
    assert status["SIDE-HOLES"]["status"] == "pending"
    # Deleting an executable operation does not delete candidate goals.
    source_job.plan.setups[0].operations.clear()
    assert len(route_progress(source_job, geometry, proposal, None)["steps"]) == len(proposal.steps)


@pytest.mark.parametrize("parents", [["MISSING"], ["FACE"]])
def test_invalid_or_circular_dependencies_rejected(parents):
    with pytest.raises(ValueError):
        CandidateRoute(steps=[dict(id="FACE", name="Face", operation_types=["turn_facing"],
                                  reason="Basis", prerequisite_ids=parents)])


def test_save_retains_required_goals_and_persists_without_adding_operations(tmp_path, monkeypatch):
    source_job, geometry = source()
    monkeypatch.setattr(route_module, "_inputs", lambda _: (source_job, tmp_path, geometry))
    monkeypatch.setattr(route_module, "get_candidate_route", lambda _: {"saved": True})
    proposal = propose_route(source_job, geometry)
    omitted = proposal.model_copy(deep=True)
    omitted.steps = [step for step in omitted.steps if step.id != "SIDE-HOLES"]
    with pytest.raises(HTTPException) as error:
        save_candidate_route("job", omitted)
    assert error.value.status_code == 422
    assert not (tmp_path / "candidate-route.json").exists()
    save_candidate_route("job", proposal)
    saved = CandidateRoute.model_validate_json((tmp_path / "candidate-route.json").read_text())
    assert saved.source == "harness"
    assert len(source_job.plan.setups[0].operations) == 1


def test_excluded_feature_cannot_be_used_in_saved_route(tmp_path, monkeypatch):
    source_job, geometry = source()
    proposal = propose_route(source_job, geometry)
    source_job.analysis.cylindrical_features[1].review_state = "excluded"
    monkeypatch.setattr(route_module, "_inputs", lambda _: (source_job, tmp_path, geometry))
    with pytest.raises(HTTPException) as error:
        save_candidate_route("job", proposal)
    assert error.value.status_code == 422


def test_context_edit_removes_accepted_status_without_rewriting_route(tmp_path, monkeypatch):
    import json
    from app import main
    from app.agent.l32_operation_trial import run_independent_trial, accept_trial
    from test_l32_operation_trial import snapshot
    source_job, geometry = source()
    machine = snapshot()
    trial, state = run_independent_trial(job=source_job, operation_id="OP10", rotational=geometry, snapshot=machine, state=None)
    state = accept_trial(job=source_job, operation_id="OP10", trial=trial, state=state, context_signature=trial["context_signature"])
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(state))
    monkeypatch.setattr(route_module, "_inputs", lambda _: (source_job, tmp_path, geometry))
    monkeypatch.setattr(main, "load_machine_snapshot", lambda _: machine)
    before = route_module.get_candidate_route("job")
    assert next(step for step in before["steps"] if step["id"] == "FACE")["status"] == "trial_accepted"
    inner = next(profile for profile in before["material_profiles"] if profile["side"] == "inner")
    assert inner["excess_stock_sample_count"] > 0
    assert inner["remaining_z_ranges_mm"]
    assert inner["production_ready"] is False
    source_job.plan.stock["diameter_mm"] += 1
    after = route_module.get_candidate_route("job")
    assert next(step for step in after["steps"] if step["id"] == "FACE")["status"] == "established"
    assert after["material_profiles"] == []
    assert not (tmp_path / "candidate-route.json").exists()
