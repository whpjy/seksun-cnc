from tools.evaluate_examples import assess_expectation


def test_assesses_l32_rotational_and_standard_capability() -> None:
    case = {"expected": {"part_family": "rotational", "machine_capability": "standard"}}
    result = {
        "rotational": {"status": "candidate"},
        "plan": {"automation_status": "review", "stock": {"diameter_mm": 25}},
    }

    assessment = assess_expectation(case, result)

    assert assessment["status"] == "passed"
    assert all(item["passed"] for item in assessment["checks"])


def test_assesses_required_38mm_option() -> None:
    case = {"expected": {"machine_capability": "bar_diameter_38mm_option"}}
    result = {
        "rotational": {"status": "candidate"},
        "plan": {
            "automation_status": "review",
            "stock": {"diameter_mm": 34, "required_option": "bar_diameter_38mm"},
        },
    }

    assert assess_expectation(case, result)["status"] == "passed"


def test_reports_failed_negative_expectation_when_operations_were_not_blocked() -> None:
    case = {"expected": {"part_family": "non_rotational", "machine_capability": "unsupported"}}
    result = {
        "rotational": {"status": "candidate"},
        "plan": {"automation_status": "review", "stock": {"diameter_mm": 20}},
    }

    assessment = assess_expectation(case, result)

    assert assessment["status"] == "failed"
    assert [item["passed"] for item in assessment["checks"]] == [False, False]
