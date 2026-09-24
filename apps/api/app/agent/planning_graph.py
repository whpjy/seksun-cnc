from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ..coverage import evaluate_plan_coverage
from ..models import GeometryAnalysis, ProcessPlan
from ..planner import build_process_plan
from ..qwen import QwenPlanningError, review_process_plan
from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .operation_audit import audit_operation_contract, summarize_operation_audit
from .plan_compiler import compile_agent_plan
from .state import ProcessPlanningState


ProgressCallback = Callable[..., None]
ReviewFunction = Callable[..., dict[str, Any]]
PlanFunction = Callable[..., ProcessPlan]


def _operation_count(plan: ProcessPlan) -> int:
    return sum(len(setup.operations) for setup in plan.setups)


def _risk_counts(review: dict[str, Any]) -> dict[str, int]:
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for risk in review.get("risks", []):
        severity = str(risk.get("severity", "")).lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def _evaluate_candidate(
    baseline: ProcessPlan,
    candidate: ProcessPlan,
    guidance: dict[str, Any],
    settings: AgentSettings,
    operation_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    review = guidance.get("review", {})
    risk_counts = _risk_counts(review)
    baseline_coverage = baseline.coverage.score if baseline.coverage else 0.0
    candidate_coverage = candidate.coverage.score if candidate.coverage else 0.0
    requires_review = bool(review.get("requires_engineer_review", False))
    blocking_reasons: list[str] = []
    if candidate.automation_status == "unsupported":
        blocking_reasons.append("候选方案包含当前系统不支持的制造能力")
    if candidate.blocking_reasons:
        blocking_reasons.extend(candidate.blocking_reasons)
    if candidate_coverage + 1e-9 < baseline_coverage:
        blocking_reasons.append("候选方案的特征覆盖率低于正式基线")
    if risk_counts["critical"] or risk_counts["high"]:
        blocking_reasons.append("AI 识别到高风险或严重制造风险")
    if requires_review and settings.human_review_enabled:
        blocking_reasons.append("AI 要求制造工程师复核")
    operation_audit = operation_audit or {}
    if operation_audit.get("status") == "blocked":
        blocked_ids = operation_audit.get("blocked_operation_ids", [])
        blocking_reasons.append(
            "逐工序能力校验未通过"
            + (f"：{'、'.join(str(item) for item in blocked_ids)}" if blocked_ids else "")
        )

    eligible = not blocking_reasons
    recommendations = list(review.get("operation_recommendations", []))
    return {
        "schema_version": "1.0.0",
        "mode": settings.mode,
        "eligible_for_promotion": eligible,
        "production_result_changed": settings.writes_production_results and eligible,
        "primary_plan_selected": settings.writes_production_results,
        "blocking_reasons": blocking_reasons,
        "risk_counts": risk_counts,
        "requires_engineer_review": requires_review,
        "confidence": float(review.get("confidence", 0) or 0),
        "baseline": {
            "process_kind": baseline.process_kind,
            "setup_count": len(baseline.setups),
            "operation_count": _operation_count(baseline),
            "coverage_score": baseline_coverage,
            "automation_status": baseline.automation_status,
        },
        "candidate": {
            "process_kind": candidate.process_kind,
            "setup_count": len(candidate.setups),
            "operation_count": _operation_count(candidate),
            "coverage_score": candidate_coverage,
            "automation_status": candidate.automation_status,
        },
        "differences": {
            "process_kind_changed": baseline.process_kind != candidate.process_kind,
            "setup_delta": len(candidate.setups) - len(baseline.setups),
            "operation_delta": _operation_count(candidate) - _operation_count(baseline),
            "coverage_delta": round(candidate_coverage - baseline_coverage, 6),
        },
        "recommendation_summary": {
            "total": len(recommendations),
            "by_action": {
                action: sum(item.get("action") == action for item in recommendations)
                for action in ("keep", "add", "modify", "remove", "reorder", "review")
            },
            "note": "结构化建议仅作为候选依据；缺少完整刀具和参数的数据不得直接写入正式工序。",
        },
        "operation_audit": operation_audit,
    }


def build_process_planning_subgraph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    progress_callback: ProgressCallback | None = None,
    reviewer: ReviewFunction = review_process_plan,
    planner: PlanFunction = build_process_plan,
):
    def report(stage: str, message: str, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def review_node(state: ProcessPlanningState) -> ProcessPlanningState:
        analysis = GeometryAnalysis.model_validate(state["analysis"])
        baseline = ProcessPlan.model_validate(state["baseline_plan"])
        report("agent_review", "LangGraph 正在调用 Qwen 研判工艺草案", agent_node="review_ai")
        try:
            guidance = reviewer(analysis, baseline, progress_callback=progress_callback)
        except QwenPlanningError as error:
            report("agent_fallback", "AI 研判失败，候选方案回退到正式基线", error=str(error))
            return {
                "status": "fallback",
                "error": str(error),
                "candidate_plan": baseline.model_dump(mode="json"),
                "evaluation": {
                    "schema_version": "1.0.0", "mode": settings.mode,
                    "eligible_for_promotion": False,
                    "production_result_changed": False,
                    "blocking_reasons": [f"AI 研判不可用：{error}"],
                },
            }
        return {"status": "reviewing", "guidance": guidance}

    def route_after_review(state: ProcessPlanningState) -> str:
        return "fallback" if state.get("status") == "fallback" else "synthesize_candidate"

    def synthesize_node(state: ProcessPlanningState) -> ProcessPlanningState:
        analysis = GeometryAnalysis.model_validate(state["analysis"])
        baseline = ProcessPlan.model_validate(state["baseline_plan"])
        guidance = state["guidance"]
        review = guidance.get("review", {})
        recommended_kind = str(review.get("recommended_process_kind", ""))
        confidence = float(review.get("confidence", 0) or 0)
        process_kind_hint = recommended_kind if recommended_kind == "sheet_forming" and confidence >= 0.7 else None
        report(
            "agent_synthesis", "正在将 AI 建议编译为隔离的候选方案",
            agent_node="synthesize_candidate", recommended_process_kind=recommended_kind,
            confidence=confidence,
        )
        candidate = baseline
        if process_kind_hint and baseline.process_kind != process_kind_hint:
            candidate = planner(
                analysis, material=state["material"], machine=state["machine"],
                process_kind_hint=process_kind_hint,
            )
        candidate = compile_agent_plan(
            analysis, candidate, guidance, agent_mode=settings.mode,
        )
        return {"status": "candidate_ready", "candidate_plan": candidate.model_dump(mode="json")}

    def validate_node(state: ProcessPlanningState) -> ProcessPlanningState:
        analysis = GeometryAnalysis.model_validate(state["analysis"])
        candidate = ProcessPlan.model_validate(state["candidate_plan"])
        report("agent_validation", "正在对候选方案执行确定性覆盖率校验", agent_node="validate_candidate")
        candidate.coverage = evaluate_plan_coverage(analysis, candidate)
        return {"candidate_plan": candidate.model_dump(mode="json")}

    def prepare_operation_audit_node(state: ProcessPlanningState) -> ProcessPlanningState:
        candidate = ProcessPlan.model_validate(state["candidate_plan"])
        queue = [
            {"setup_id": setup.id, "operation_id": operation.id}
            for setup in candidate.setups
            for operation in setup.operations
            if operation.enabled
        ]
        report(
            "agent_operation_audit_prepare",
            f"准备逐道验证 {len(queue)} 道 AI 工序",
            agent_node="prepare_operation_audit",
            operation_count=len(queue),
        )
        return {"operation_queue": queue, "operation_cursor": 0, "operation_records": []}

    def route_after_audit_prepare(state: ProcessPlanningState) -> str:
        return "audit_operation" if state.get("operation_queue") else "summarize_operation_audit"

    def audit_operation_node(state: ProcessPlanningState) -> ProcessPlanningState:
        analysis = GeometryAnalysis.model_validate(state["analysis"])
        candidate = ProcessPlan.model_validate(state["candidate_plan"])
        cursor = int(state.get("operation_cursor", 0))
        item = state["operation_queue"][cursor]
        setup = next(setup for setup in candidate.setups if setup.id == item["setup_id"])
        operation = next(operation for operation in setup.operations if operation.id == item["operation_id"])
        record = audit_operation_contract(analysis, setup, operation, state["machine"])
        report(
            "agent_operation_audit",
            f"{operation.id} {operation.name}：{record['status']}",
            agent_node="audit_operation",
            operation_id=operation.id,
            setup_id=setup.id,
            operation_status=record["status"],
            check_count=len(record["checks"]),
            blocker_count=len(record["blockers"]),
            warning_count=len(record["warnings"]),
            feature_ids=operation.feature_ids,
        )
        return {
            "operation_cursor": cursor + 1,
            "operation_records": [*state.get("operation_records", []), record],
        }

    def route_after_operation_audit(state: ProcessPlanningState) -> str:
        return (
            "audit_operation"
            if int(state.get("operation_cursor", 0)) < len(state.get("operation_queue", []))
            else "summarize_operation_audit"
        )

    def summarize_operation_audit_node(state: ProcessPlanningState) -> ProcessPlanningState:
        summary = summarize_operation_audit(state.get("operation_records", []))
        iteration = int(state.get("planning_iteration", 0))
        summary["iteration"] = iteration
        report(
            "agent_operation_audit_summary",
            "逐工序能力校验完成",
            agent_node="summarize_operation_audit",
            audit_status=summary["status"],
            operation_count=summary["operation_count"],
            passed=summary["counts"]["passed"],
            warning=summary["counts"]["warning"],
            blocked=summary["counts"]["blocked"],
        )
        return {
            "operation_audit": summary,
            "operation_audit_history": [*state.get("operation_audit_history", []), summary],
        }

    def route_after_operation_audit_summary(state: ProcessPlanningState) -> str:
        audit = state.get("operation_audit", {})
        iteration = int(state.get("planning_iteration", 0))
        return (
            "replan"
            if audit.get("status") == "blocked" and iteration < settings.max_global_replans
            else "decide"
        )

    def replan_from_audit_node(state: ProcessPlanningState) -> ProcessPlanningState:
        analysis = GeometryAnalysis.model_validate(state["analysis"])
        candidate = ProcessPlan.model_validate(state["candidate_plan"])
        iteration = int(state.get("planning_iteration", 0)) + 1
        report(
            "agent_replan_from_audit",
            f"第 {iteration} 轮：根据逐工序失败证据重新规划",
            agent_node="replan_from_operation_audit",
            iteration=iteration,
            blocked_operation_count=len(state.get("operation_audit", {}).get("blocked_operation_ids", [])),
        )
        try:
            guidance = reviewer(
                analysis,
                candidate,
                artifacts={"operation_audit": state.get("operation_audit", {})},
                progress_callback=progress_callback,
            )
        except QwenPlanningError as error:
            report(
                "agent_replan_from_audit",
                "AI 局部重规划失败，保留当前方案并进入安全门禁",
                agent_node="replan_from_operation_audit",
                iteration=iteration,
                error=str(error),
            )
            return {"planning_iteration": settings.max_global_replans, "error": str(error)}
        revised = compile_agent_plan(
            analysis, candidate, guidance, agent_mode=settings.mode,
        )
        return {
            "guidance": guidance,
            "candidate_plan": revised.model_dump(mode="json"),
            "planning_iteration": iteration,
            "status": "candidate_ready",
        }

    def decide_node(state: ProcessPlanningState) -> ProcessPlanningState:
        baseline = ProcessPlan.model_validate(state["baseline_plan"])
        candidate = ProcessPlan.model_validate(state["candidate_plan"])
        operation_audit = dict(state.get("operation_audit", {}))
        operation_audit["history"] = state.get("operation_audit_history", [])
        operation_audit["replan_count"] = int(state.get("planning_iteration", 0))
        evaluation = _evaluate_candidate(
            baseline, candidate, state["guidance"], settings, operation_audit,
        )
        status = "eligible" if evaluation["eligible_for_promotion"] else "blocked"
        report(
            "agent_decision", "候选方案晋级判断完成",
            agent_node="decide_promotion", eligible=evaluation["eligible_for_promotion"],
            blocking_count=len(evaluation["blocking_reasons"]),
        )
        return {"status": status, "evaluation": evaluation, "operation_audit": operation_audit}

    workflow = StateGraph(ProcessPlanningState)
    workflow.add_node("review_ai", review_node)
    workflow.add_node("synthesize_candidate", synthesize_node)
    workflow.add_node("validate_candidate", validate_node)
    workflow.add_node("prepare_operation_audit", prepare_operation_audit_node)
    workflow.add_node("audit_operation", audit_operation_node)
    workflow.add_node("summarize_operation_audit", summarize_operation_audit_node)
    workflow.add_node("replan_from_operation_audit", replan_from_audit_node)
    workflow.add_node("decide_promotion", decide_node)
    workflow.add_edge(START, "review_ai")
    workflow.add_conditional_edges(
        "review_ai", route_after_review,
        {"fallback": END, "synthesize_candidate": "synthesize_candidate"},
    )
    workflow.add_edge("synthesize_candidate", "validate_candidate")
    workflow.add_edge("validate_candidate", "prepare_operation_audit")
    workflow.add_conditional_edges(
        "prepare_operation_audit",
        route_after_audit_prepare,
        {
            "audit_operation": "audit_operation",
            "summarize_operation_audit": "summarize_operation_audit",
        },
    )
    workflow.add_conditional_edges(
        "audit_operation",
        route_after_operation_audit,
        {
            "audit_operation": "audit_operation",
            "summarize_operation_audit": "summarize_operation_audit",
        },
    )
    workflow.add_conditional_edges(
        "summarize_operation_audit",
        route_after_operation_audit_summary,
        {"replan": "replan_from_operation_audit", "decide": "decide_promotion"},
    )
    workflow.add_edge("replan_from_operation_audit", "validate_candidate")
    workflow.add_edge("decide_promotion", END)
    return workflow.compile(checkpointer=checkpointer)


def run_process_planning_subgraph(
    *,
    job_id: str,
    analysis: GeometryAnalysis,
    baseline_plan: ProcessPlan,
    material: str,
    machine: str,
    settings: AgentSettings,
    progress_callback: ProgressCallback | None = None,
) -> ProcessPlanningState:
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_process_planning_subgraph(
            settings, checkpointer, progress_callback=progress_callback,
        )
        return graph.invoke(
            {
                "job_id": job_id,
                "mode": settings.mode,
                "material": material,
                "machine": machine,
                "analysis": analysis.model_dump(mode="json"),
                "baseline_plan": baseline_plan.model_dump(mode="json"),
                "status": "reviewing",
                "planning_iteration": 0,
                "operation_audit_history": [],
            },
            {"configurable": {"thread_id": f"{job_id}:process-planning"}},
        )
