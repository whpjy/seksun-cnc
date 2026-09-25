from __future__ import annotations

from typing import Any, Literal, TypedDict


class EnvironmentValidationState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    runtime: str
    validation_count: int
    status: Literal["ready"]
    events: list[str]


class ProcessPlanningState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    material: str
    machine: str
    analysis: dict[str, Any]
    baseline_plan: dict[str, Any]
    guidance: dict[str, Any]
    candidate_plan: dict[str, Any]
    evaluation: dict[str, Any]
    operation_queue: list[dict[str, Any]]
    operation_cursor: int
    operation_records: list[dict[str, Any]]
    operation_audit: dict[str, Any]
    operation_audit_history: list[dict[str, Any]]
    planning_iteration: int
    status: Literal["reviewing", "candidate_ready", "fallback", "eligible", "blocked"]
    error: str


class OperationExecutionState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    operations: list[dict[str, Any]]
    cursor: int
    current_operation: dict[str, Any]
    cam_result: dict[str, Any]
    simulation: dict[str, Any]
    verification: dict[str, Any]
    collision: dict[str, Any]
    remediation: dict[str, Any]
    current_evidence: dict[str, Any]
    records: list[dict[str, Any]]
    summary: dict[str, Any]
    status: Literal["running", "passed", "action_required", "blocked"]


class L32OperationExecutionState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    stages: list[dict[str, Any]]
    operations: dict[str, dict[str, Any]]
    toolpath: dict[str, Any]
    continuous_simulation: dict[str, Any]
    cursor: int
    current_stage: dict[str, Any]
    current_evidence: dict[str, Any]
    records: list[dict[str, Any]]
    summary: dict[str, Any]
    status: Literal["running", "passed", "action_required", "blocked"]


class ValidationRemediationState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    current_report: dict[str, Any]
    defect_analysis: dict[str, Any]
    repair_scope: Literal["operation", "setup", "global", "none"]
    decision: Literal["repair", "retry", "finalize"]
    applied: dict[str, Any]
    latest_result: dict[str, Any]
    iteration: int
    max_iterations: int
    cycles: list[dict[str, Any]]
    outcome: Literal["passed", "blocked", "max_iterations", "manual_review"]
    summary: dict[str, Any]
    status: Literal["running", "passed", "blocked", "waiting"]


class ManufacturingOrchestratorState(TypedDict, total=False):
    job_id: str
    mode: Literal["disabled", "shadow", "active"]
    world: dict[str, Any]
    action: Literal[
        "perceive", "plan", "compile", "execute", "review", "commit",
        "repair", "human_review", "complete",
    ]
    status: Literal["running", "waiting", "completed", "blocked"]
    halt: bool
    step_count: int
    max_steps: int
    trace: list[dict[str, Any]]
    error: str
