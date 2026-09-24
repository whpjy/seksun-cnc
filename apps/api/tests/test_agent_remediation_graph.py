from __future__ import annotations

from pathlib import Path

from app.agent.config import load_agent_settings
from app.agent.remediation_graph import run_validation_remediation_subgraph


def settings(tmp_path: Path, name: str):
    return load_agent_settings({
        "CNC_AGENT_MODE": "shadow",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / f"{name}.sqlite"),
        "CNC_AGENT_MAX_LOCAL_RETRIES": "3",
    })


def repairable_report(*, iteration: int = 0, max_iterations: int = 3) -> dict:
    return {
        "status": "action_required",
        "iteration": iteration,
        "max_iterations": max_iterations,
        "can_auto_replan": True,
        "defects": [{
            "id": "DEF-001",
            "severity": "critical",
            "operation_ids": ["OP10"],
            "setup_ids": ["SETUP-1"],
        }],
        "actions": [{
            "id": "ACT-001",
            "kind": "modify_operation",
            "operation_id": "OP10",
            "auto_applicable": True,
        }],
    }


def test_remediation_graph_applies_safe_repair_and_releases(tmp_path: Path) -> None:
    applied_reports: list[dict] = []
    events: list[str] = []

    def apply(report: dict) -> dict:
        applied_reports.append(report)
        return {
            "iteration": 1,
            "applied_actions": [{"id": "ACT-001", "label": "修正工序参数"}],
        }

    def regenerate(iteration: int, max_iterations: int) -> dict:
        assert (iteration, max_iterations) == (1, 3)
        return {"remediation": {
            "status": "clear", "iteration": 1, "max_iterations": 3,
            "can_auto_replan": False, "defects": [], "actions": [],
        }}

    result = run_validation_remediation_subgraph(
        job_id="job-remediation-pass",
        remediation=repairable_report(),
        settings=settings(tmp_path, "pass"),
        apply_remediation=apply,
        regenerate_cam=regenerate,
        progress_callback=lambda stage, _message, **_details: events.append(stage),
    )

    assert applied_reports and applied_reports[0]["defects"][0]["id"] == "DEF-001"
    assert result["status"] == "passed"
    assert result["summary"]["outcome"] == "passed"
    assert result["summary"]["next_action"] == "release"
    assert result["summary"]["cycle_count"] == 1
    assert result["cycles"][0]["repair_scope"] == "operation"
    assert events == [
        "attribute_defect", "choose_repair_scope", "replan_local",
        "regenerate_and_validate", "assess_revalidation", "finalize_remediation",
    ]


def test_remediation_graph_retries_only_while_report_allows_it(tmp_path: Path) -> None:
    application_count = 0

    def apply(_report: dict) -> dict:
        nonlocal application_count
        application_count += 1
        return {
            "iteration": application_count,
            "applied_actions": [{"id": f"ACT-{application_count:03d}"}],
        }

    def regenerate(iteration: int, _max_iterations: int) -> dict:
        if iteration == 1:
            return {"remediation": repairable_report(iteration=1)}
        return {"remediation": {
            "status": "clear", "iteration": 2, "max_iterations": 3,
            "can_auto_replan": False, "defects": [], "actions": [],
        }}

    result = run_validation_remediation_subgraph(
        job_id="job-remediation-retry",
        remediation=repairable_report(),
        settings=settings(tmp_path, "retry"),
        apply_remediation=apply,
        regenerate_cam=regenerate,
    )

    assert application_count == 2
    assert result["summary"]["outcome"] == "passed"
    assert result["summary"]["iteration"] == 2
    assert result["summary"]["cycle_count"] == 2


def test_remediation_graph_stops_when_retry_budget_is_exhausted(tmp_path: Path) -> None:
    application_count = 0

    def apply(_report: dict) -> dict:
        nonlocal application_count
        application_count += 1
        return {"iteration": application_count, "applied_actions": [{"id": "ACT-001"}]}

    def regenerate(iteration: int, _max_iterations: int) -> dict:
        return {"remediation": repairable_report(iteration=iteration, max_iterations=2)}

    result = run_validation_remediation_subgraph(
        job_id="job-remediation-budget",
        remediation=repairable_report(max_iterations=2),
        settings=settings(tmp_path, "budget"),
        apply_remediation=apply,
        regenerate_cam=regenerate,
    )

    assert application_count == 2
    assert result["status"] == "blocked"
    assert result["summary"]["outcome"] == "max_iterations"
    assert result["summary"]["next_action"] == "retry_budget_exhausted"
    assert result["summary"]["remaining_defect_count"] == 1


def test_remediation_graph_routes_unsafe_report_to_human_without_mutation(tmp_path: Path) -> None:
    called = False
    report = repairable_report()
    report.update({"status": "blocked", "can_auto_replan": False})

    def apply(_report: dict) -> dict:
        nonlocal called
        called = True
        return {}

    result = run_validation_remediation_subgraph(
        job_id="job-remediation-human",
        remediation=report,
        settings=settings(tmp_path, "human"),
        apply_remediation=apply,
        regenerate_cam=lambda _iteration, _maximum: {},
    )

    assert called is False
    assert result["status"] == "blocked"
    assert result["summary"]["outcome"] == "blocked"
    assert result["summary"]["next_action"] == "human_review"
    assert result.get("cycles", []) == []
