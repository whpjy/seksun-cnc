"""Bounded state helpers for the unified L32 autonomous planning executor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal


AutonomousOutcome = Literal[
    "verified_success", "engineer_review_required", "capability_unavailable",
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_state(job_id: str, limits: dict[str, int | bool]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "status": "running",
        "outcome": None,
        "phase": "manufacturability_assessment",
        "started_at": utc_now(),
        "finished_at": None,
        "limits": limits,
        "usage": {
            "tool_calls": 0,
            "operation_trials": 0,
            "repair_attempts": 0,
            "model_calls": 0,
            "model_tokens": 0,
        },
        "manufacturability": {},
        "operations": [],
        "coverage": None,
        "blockers": [],
        "capability_requirements": [],
        "next_action": "assess_part_and_machine",
        "release_status": "DRAFT",
        "production_ready": False,
    }


def classify_blocker(payload: dict[str, Any]) -> AutonomousOutcome:
    """Separate missing engineering evidence from a demonstrated capability gap."""
    status = str(payload.get("status") or "")
    reason = str(payload.get("reason") or "")
    if (
        status in {"capability_required", "tool_evidence_required"}
        or payload.get("capability_requirements")
        or "capability" in reason
        or "inventory" in reason
        or "unsupported" in reason
    ):
        return "capability_unavailable"
    return "engineer_review_required"


def finish_state(
    state: dict[str, Any], outcome: AutonomousOutcome, *,
    next_action: str, blocker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state["status"] = "completed"
    state["outcome"] = outcome
    state["phase"] = "finished"
    state["finished_at"] = utc_now()
    state["next_action"] = next_action
    if blocker:
        state["blockers"].append(blocker)
    return state


def budget_exhausted(state: dict[str, Any], elapsed_seconds: float) -> str | None:
    limits = state["limits"]
    usage = state["usage"]
    if elapsed_seconds >= float(limits["max_seconds"]):
        return "time_budget_exhausted"
    if int(usage["tool_calls"]) >= int(limits["max_tool_calls"]):
        return "tool_call_budget_exhausted"
    if int(usage["operation_trials"]) >= int(limits["max_operations"]):
        return "operation_trial_budget_exhausted"
    return None
