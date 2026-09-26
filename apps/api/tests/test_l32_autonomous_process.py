from app.agent.l32_autonomous_process import (
    budget_exhausted, classify_blocker, finish_state, new_state,
)


def test_capability_blocker_has_terminal_capability_outcome() -> None:
    assert classify_blocker({
        "status": "capability_required",
        "capability_requirements": [{"required_strategy": "edm"}],
    }) == "capability_unavailable"


def test_ambiguous_geometry_requires_engineer_review() -> None:
    assert classify_blocker({
        "status": "blocked", "reason": "rotational_profile_requires_review",
    }) == "engineer_review_required"


def test_budget_is_bounded_and_terminal_state_never_claims_production_release() -> None:
    state = new_state("job-1", {
        "max_seconds": 60, "max_tool_calls": 4, "max_operations": 2,
        "max_repair_attempts_per_operation": 1, "rebuild_plan": False,
    })
    state["usage"]["tool_calls"] = 4
    assert budget_exhausted(state, 1) == "tool_call_budget_exhausted"

    result = finish_state(
        state, "engineer_review_required", next_action="review_budget",
    )
    assert result["production_ready"] is False
    assert result["release_status"] == "DRAFT"
