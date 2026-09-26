from app.main import _l32_candidate_improvement, _l32_candidate_rank


def trial(*, status: str, can_accept: bool, excess: int = 0, volume: float = 0) -> dict:
    return {
        "status": status,
        "can_accept": can_accept,
        "evidence": {
            "command_count": 20,
            "reachability_status": "passed",
            "verification_metrics": {
                "overcut_sample_count": 0,
                "excess_stock_sample_count": excess,
                "maximum_overcut_mm": 0,
                "maximum_excess_stock_mm": 0.2 if excess else 0,
                "estimated_excess_stock_volume_mm3": volume,
            },
        },
    }


def test_failed_candidate_gets_diagnostic_score_but_is_never_eligible() -> None:
    ranking = _l32_candidate_rank(trial(status="warning", can_accept=False, excess=12, volume=8))

    assert ranking["rank_score"] == 0
    assert ranking["diagnostic_score"] > 0
    assert ranking["eligible_for_application"] is False


def test_clean_pass_ranks_above_acknowledgeable_warning() -> None:
    passed = _l32_candidate_rank(trial(status="passed", can_accept=True))
    warning = _l32_candidate_rank(trial(status="warning", can_accept=True, excess=2, volume=1))

    assert passed["eligible_for_application"] is True
    assert warning["eligible_for_application"] is True
    assert passed["rank_score"] > warning["rank_score"]


def test_improvement_reports_reduced_residual_without_claiming_acceptance() -> None:
    baseline = trial(status="warning", can_accept=False, excess=20, volume=12)
    candidate = trial(status="warning", can_accept=False, excess=8, volume=5)

    improvement = _l32_candidate_improvement(baseline, candidate)

    assert improvement["excess_stock_sample_reduction"] == 12
    assert improvement["excess_stock_volume_reduction_mm3"] == 7
    assert improvement["diagnostic_score_delta"] > 0
    assert improvement["became_acceptable"] is False


def test_nonrotational_residual_is_ranked_and_reported_as_improvement() -> None:
    baseline = trial(status="blocked", can_accept=False)
    candidate = trial(status="blocked", can_accept=False)
    baseline["evidence"]["remaining_feature_material_mm3"] = 3.0
    candidate["evidence"]["remaining_feature_material_mm3"] = 0.5

    improvement = _l32_candidate_improvement(baseline, candidate)

    assert improvement["nonrotational_residual_reduction_mm3"] == 2.5
    assert improvement["diagnostic_score_delta"] > 0
