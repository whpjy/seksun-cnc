from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .state import ValidationRemediationState


ProgressCallback = Callable[..., None]
ApplyCallback = Callable[[dict[str, Any]], dict[str, Any]]
RegenerateCallback = Callable[[int, int], dict[str, Any]]


def _analyze_defects(report: dict[str, Any]) -> dict[str, Any]:
    defects = [item for item in report.get("defects", []) if isinstance(item, dict)]
    actions = [item for item in report.get("actions", []) if isinstance(item, dict)]
    operation_ids = sorted({
        str(operation_id)
        for defect in defects
        for operation_id in defect.get("operation_ids", [])
        if operation_id
    })
    setup_ids = sorted({
        str(setup_id)
        for defect in defects
        for setup_id in defect.get("setup_ids", [])
        if setup_id
    })
    global_defects = [item for item in defects if not item.get("operation_ids") and not item.get("setup_ids")]
    severity_counts = {
        severity: sum(str(item.get("severity", "")) == severity for item in defects)
        for severity in ("critical", "high", "warning")
    }
    return {
        "defect_count": len(defects),
        "defect_ids": [str(item.get("id", "")) for item in defects],
        "operation_ids": operation_ids,
        "setup_ids": setup_ids,
        "global_defect_count": len(global_defects),
        "severity_counts": severity_counts,
        "action_count": len(actions),
        "auto_action_count": sum(item.get("auto_applicable") is True for item in actions),
    }


def build_validation_remediation_subgraph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    apply_remediation: ApplyCallback,
    regenerate_cam: RegenerateCallback,
    progress_callback: ProgressCallback | None = None,
):
    def report(stage: str, message: str, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def attribute_defect(state: ValidationRemediationState) -> ValidationRemediationState:
        analysis = _analyze_defects(state["current_report"])
        report("attribute_defect", "已完成缺陷归因与影响范围分析", **analysis)
        return {"status": "running", "defect_analysis": analysis}

    def choose_repair_scope(state: ValidationRemediationState) -> ValidationRemediationState:
        report_data = state["current_report"]
        analysis = state["defect_analysis"]
        iteration = int(report_data.get("iteration", state.get("iteration", 0)) or 0)
        max_iterations = max(
            int(report_data.get("max_iterations", state.get("max_iterations", settings.max_local_retries)) or settings.max_local_retries),
            1,
        )
        if not analysis["defect_count"]:
            scope, decision, outcome = "none", "finalize", "passed"
        elif report_data.get("status") == "blocked":
            scope, decision, outcome = "none", "finalize", "blocked"
        elif iteration >= max_iterations:
            scope, decision, outcome = "none", "finalize", "max_iterations"
        elif not report_data.get("can_auto_replan"):
            scope, decision, outcome = "none", "finalize", "manual_review"
        else:
            action_kinds = {
                str(item.get("kind", ""))
                for item in report_data.get("actions", [])
                if isinstance(item, dict) and item.get("auto_applicable") is True
            }
            if analysis["global_defect_count"] or "adjust_safety" in action_kinds:
                scope = "global"
            elif analysis["setup_ids"] and not analysis["operation_ids"]:
                scope = "setup"
            else:
                scope = "operation"
            decision, outcome = "repair", "manual_review"
        report(
            "choose_repair_scope",
            f"纠错范围判断完成：{scope}",
            repair_scope=scope, decision=decision, iteration=iteration,
            max_iterations=max_iterations, outcome=outcome,
        )
        return {
            "repair_scope": scope,
            "decision": decision,
            "outcome": outcome,
            "iteration": iteration,
            "max_iterations": max_iterations,
        }

    def route_after_scope(state: ValidationRemediationState) -> str:
        return "repair" if state.get("decision") == "repair" else "finalize"

    def replan_local(state: ValidationRemediationState) -> ValidationRemediationState:
        applied = apply_remediation(state["current_report"])
        iteration = int(applied.get("iteration", state.get("iteration", 0) + 1))
        actions = [
            item for item in applied.get("applied_actions", []) if isinstance(item, dict)
        ]
        cycle = {
            "iteration": iteration,
            "repair_scope": state.get("repair_scope", "operation"),
            "applied_actions": actions,
        }
        report(
            "replan_local",
            f"第 {iteration} 轮已应用 {len(actions)} 项安全修复",
            iteration=iteration, max_iterations=state["max_iterations"],
            repair_scope=state.get("repair_scope"), action_count=len(actions),
        )
        return {
            "applied": applied,
            "iteration": iteration,
            "cycles": [*state.get("cycles", []), cycle],
        }

    def regenerate_and_validate(state: ValidationRemediationState) -> ValidationRemediationState:
        iteration = int(state["iteration"])
        latest_result = regenerate_cam(iteration, int(state["max_iterations"]))
        next_report = latest_result.get("remediation")
        if not isinstance(next_report, dict):
            raise ValueError("复验未生成缺陷报告")
        report(
            "regenerate_and_validate",
            f"第 {iteration} 轮刀路、仿真与安全复验已完成",
            iteration=iteration, max_iterations=state["max_iterations"],
            remediation_status=next_report.get("status"),
            defect_count=len(next_report.get("defects", [])),
        )
        return {"latest_result": latest_result, "current_report": next_report}

    def assess_revalidation(state: ValidationRemediationState) -> ValidationRemediationState:
        report_data = state["current_report"]
        defects = [item for item in report_data.get("defects", []) if isinstance(item, dict)]
        iteration = int(state["iteration"])
        max_iterations = int(state["max_iterations"])
        if not defects:
            decision, outcome = "finalize", "passed"
        elif report_data.get("status") == "blocked":
            decision, outcome = "finalize", "blocked"
        elif report_data.get("can_auto_replan") and iteration < max_iterations:
            decision, outcome = "retry", "manual_review"
        elif iteration >= max_iterations:
            decision, outcome = "finalize", "max_iterations"
        else:
            decision, outcome = "finalize", "manual_review"
        report(
            "assess_revalidation",
            ("复验通过" if outcome == "passed" else
             "仍有可修复问题，准备继续迭代" if decision == "retry" else
             "复验未收敛，已进入安全门禁"),
            decision=decision, outcome=outcome, iteration=iteration,
            max_iterations=max_iterations, defect_count=len(defects),
        )
        return {"decision": decision, "outcome": outcome}

    def route_after_validation(state: ValidationRemediationState) -> str:
        return "retry" if state.get("decision") == "retry" else "finalize"

    def finalize_remediation(state: ValidationRemediationState) -> ValidationRemediationState:
        outcome = state.get("outcome", "manual_review")
        status = "passed" if outcome == "passed" else "blocked" if outcome in {"blocked", "max_iterations"} else "waiting"
        summary = {
            "schema_version": "1.0.0",
            "mode": settings.mode,
            "outcome": outcome,
            "status": status,
            "iteration": int(state.get("iteration", 0)),
            "max_iterations": int(state.get("max_iterations", settings.max_local_retries)),
            "cycle_count": len(state.get("cycles", [])),
            "repair_scopes": [item.get("repair_scope") for item in state.get("cycles", [])],
            "remaining_defect_count": len(state.get("current_report", {}).get("defects", [])),
            "next_action": (
                "release" if outcome == "passed"
                else "human_review" if outcome in {"blocked", "manual_review"}
                else "retry_budget_exhausted"
            ),
        }
        report(
            "finalize_remediation", "验证纠错子图已完成",
            outcome=outcome, status=status, iteration=summary["iteration"],
            remaining_defect_count=summary["remaining_defect_count"],
            next_action=summary["next_action"],
        )
        return {"status": status, "summary": summary}

    workflow = StateGraph(ValidationRemediationState)
    workflow.add_node("attribute_defect", attribute_defect)
    workflow.add_node("choose_repair_scope", choose_repair_scope)
    workflow.add_node("replan_local", replan_local)
    workflow.add_node("regenerate_and_validate", regenerate_and_validate)
    workflow.add_node("assess_revalidation", assess_revalidation)
    workflow.add_node("finalize_remediation", finalize_remediation)
    workflow.add_edge(START, "attribute_defect")
    workflow.add_edge("attribute_defect", "choose_repair_scope")
    workflow.add_conditional_edges(
        "choose_repair_scope", route_after_scope,
        {"repair": "replan_local", "finalize": "finalize_remediation"},
    )
    workflow.add_edge("replan_local", "regenerate_and_validate")
    workflow.add_edge("regenerate_and_validate", "assess_revalidation")
    workflow.add_conditional_edges(
        "assess_revalidation", route_after_validation,
        {"retry": "attribute_defect", "finalize": "finalize_remediation"},
    )
    workflow.add_edge("finalize_remediation", END)
    return workflow.compile(checkpointer=checkpointer)


def run_validation_remediation_subgraph(
    *,
    job_id: str,
    remediation: dict[str, Any],
    settings: AgentSettings,
    apply_remediation: ApplyCallback,
    regenerate_cam: RegenerateCallback,
    progress_callback: ProgressCallback | None = None,
) -> ValidationRemediationState:
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_validation_remediation_subgraph(
            settings,
            checkpointer,
            apply_remediation=apply_remediation,
            regenerate_cam=regenerate_cam,
            progress_callback=progress_callback,
        )
        return graph.invoke({
            "job_id": job_id,
            "mode": settings.mode,
            "current_report": remediation,
            "iteration": int(remediation.get("iteration", 0) or 0),
            "max_iterations": max(int(remediation.get("max_iterations", settings.max_local_retries) or settings.max_local_retries), 1),
            "cycles": [],
            "status": "running",
        }, {"configurable": {"thread_id": f"{job_id}:validation-remediation"}})
