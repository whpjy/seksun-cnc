import pytest
from app.agent.l32_operation_trial import run_independent_trial
from app.rotational_features import AIProvisionalProfileDecision
from test_l32_operation_trial import job, rotational, snapshot
from test_turning_draft import _transfer_request


def fixture(z=-31.1, scope="full"):
    op = _transfer_request().operation
    op.feature_ids = ["RP-1"]
    op.parameters["z_mm"] = z
    geometry = rotational()
    profile = geometry.profiles[0]
    profile.review_state = "ai_provisional"
    profile.provisional_decision = AIProvisionalProfileDecision(scope=scope, z_min_mm=-30, z_max_mm=0,
        diameter_min_mm=12, diameter_max_mm=20, confidence=.9, rationale="exact complete section",
        evidence_refs=["RP-1"])
    return job(op), geometry, op


def test_ai_can_cut_sacrificial_stock_outside_finished_profile():
    source, geometry, op = fixture()
    trial, _ = run_independent_trial(job=source, operation_id=op.id, rotational=geometry, snapshot=snapshot(), state=None)
    assert trial["can_accept"], trial
    assert trial["profile_authorization"]["source"] == "ai"
    assert trial["profile_authorization"]["cutoff_blank_authorized"] is True
    assert trial["evidence"]["profile_protection_metrics"]["overcut_sample_count"] == 0


@pytest.mark.parametrize("z,scope,message", [(-35,"full","outside the current solid blank"),
    (-30.5,"full","protected finished profile"),(-31.1,"partial","full finished-profile protection")])
def test_blank_permission_cannot_bypass_protection(z, scope, message):
    source, geometry, op = fixture(z, scope)
    trial, _ = run_independent_trial(job=source, operation_id=op.id, rotational=geometry, snapshot=snapshot(), state=None)
    assert not trial["can_accept"]
    assert message in trial["detail"]
