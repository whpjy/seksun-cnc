from app.agent.l32_rolling_loop import advance_l32_rolling_loop


def _plan(*operation_ids: str) -> dict:
    return {
        "setups": [{
            "id": "SETUP-1",
            "operations": [
                {"id": operation_id, "name": operation_id, "type": "turn_od_roughing", "enabled": True}
                for operation_id in operation_ids
            ],
        }],
    }


def _validation(*statuses: tuple[str, str]) -> dict:
    return {
        "status": "blocked",
        "next_action": "human_review",
        "operations": [
            {"operation_id": operation_id, "status": status, "evidence": {"command_count": 3}}
            for operation_id, status in statuses
        ],
        "artifacts": ["turning-continuous-simulation.json"],
    }


def test_rolling_loop_accepts_exactly_one_operation_per_advance() -> None:
    plan = _plan("OP10", "OP20")
    validation = _validation(("OP10", "passed"), ("OP20", "passed"))

    first = advance_l32_rolling_loop(job_id="abc", plan=plan, validation=validation)
    second = advance_l32_rolling_loop(
        job_id="abc", plan=plan, validation=validation, previous=first,
    )

    assert first["accepted_operation_ids"] == ["OP10"]
    assert first["next_operation_id"] == "OP20"
    assert first["status"] == "ready"
    assert second["accepted_operation_ids"] == ["OP10", "OP20"]
    assert second["status"] == "completed"
    assert second["production_ready"] is False


def test_rolling_loop_stops_when_next_operation_has_no_evidence() -> None:
    result = advance_l32_rolling_loop(
        job_id="abc",
        plan=_plan("OP10", "OP20"),
        validation=_validation(("OP10", "passed")),
        previous=advance_l32_rolling_loop(
            job_id="abc",
            plan=_plan("OP10", "OP20"),
            validation=_validation(("OP10", "passed")),
        ),
    )

    assert result["status"] == "plan_blocked"
    assert result["decision"]["decision"] == "waiting_evidence"
    assert result["decision"]["blocked_scope"] == "plan"
    assert result["decision"]["reason"] == "missing_operation_evidence"
    assert result["accepted_operation_ids"] == ["OP10"]
    assert result["next_operation_id"] == "OP20"


def test_plan_change_invalidates_previous_acceptance() -> None:
    old = advance_l32_rolling_loop(
        job_id="abc", plan=_plan("OP10"), validation=_validation(("OP10", "passed")),
    )
    changed = advance_l32_rolling_loop(
        job_id="abc", plan=_plan("OP20"), validation=_validation(("OP20", "passed")), previous=old,
    )

    assert changed["accepted_operation_ids"] == ["OP20"]
    assert [item["operation_id"] for item in changed["history"]] == ["OP20"]
