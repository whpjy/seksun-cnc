from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .state import OperationExecutionState


ProgressCallback = Callable[..., None]


def _operation_defects(remediation: dict[str, Any], operation_id: str) -> list[dict[str, Any]]:
    return [
        item for item in remediation.get("defects", [])
        if isinstance(item, dict) and operation_id in item.get("operation_ids", [])
    ]


def _operation_collisions(collision: dict[str, Any], operation_id: str) -> list[dict[str, Any]]:
    return [
        item for item in collision.get("collisions", [])
        if isinstance(item, dict) and str(item.get("operation_id", "")) == operation_id
    ]


def build_operation_execution_subgraph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    progress_callback: ProgressCallback | None = None,
):
    def report(stage: str, message: str, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def select_operation(state: OperationExecutionState) -> OperationExecutionState:
        operation = state["operations"][int(state.get("cursor", 0))]
        report(
            "select_operation", f"正在检查 {operation['id']} {operation['name']}",
            operation_id=operation["id"], feature_ids=operation.get("feature_ids", []),
        )
        return {"status": "running", "current_operation": operation, "current_evidence": {}}

    def inspect_toolpath(state: OperationExecutionState) -> OperationExecutionState:
        operation = state["current_operation"]
        operation_id = str(operation["id"])
        generated = operation_id in {
            str(item) for item in state["cam_result"].get("generated_operations", [])
        }
        segments = [
            item for item in state["cam_result"].get("preview_segments", [])
            if isinstance(item, dict) and str(item.get("operation_id", "")) == operation_id
        ]
        cut_segments = sum(item.get("motion") == "cut" for item in segments)
        report(
            "inspect_toolpath", "真实刀路证据已读取",
            operation_id=operation_id, generated=generated,
            segment_count=len(segments), cut_segment_count=cut_segments,
        )
        return {"current_evidence": {
            "toolpath_generated": generated,
            "segment_count": len(segments),
            "cut_segment_count": cut_segments,
        }}

    def inspect_simulation(state: OperationExecutionState) -> OperationExecutionState:
        operation_id = str(state["current_operation"]["id"])
        simulation = state["simulation"]
        metrics = simulation.get("metrics", {}) if isinstance(simulation.get("metrics"), dict) else {}
        evidence = dict(state.get("current_evidence", {}))
        evidence.update({
            "simulation_status": simulation.get("status", "unavailable"),
            "removed_volume_mm3": metrics.get("removed_volume_mm3"),
            "remaining_volume_mm3": metrics.get("remaining_volume_mm3"),
            "simulation_scope": "cumulative",
        })
        report(
            "inspect_simulation", "累计材料仿真证据已关联到当前工序",
            operation_id=operation_id, simulation_status=evidence["simulation_status"],
        )
        return {"current_evidence": evidence}

    def verify_operation(state: OperationExecutionState) -> OperationExecutionState:
        operation = state["current_operation"]
        operation_id = str(operation["id"])
        evidence = dict(state.get("current_evidence", {}))
        defects = _operation_defects(state["remediation"], operation_id)
        collisions = _operation_collisions(state["collision"], operation_id)
        critical = any(str(item.get("severity")) == "critical" for item in defects)
        if not evidence.get("toolpath_generated") or collisions or critical:
            status = "blocked"
        elif defects:
            status = "action_required"
        else:
            status = "passed"
        actions = [
            action for action in state["remediation"].get("actions", [])
            if isinstance(action, dict) and str(action.get("operation_id", "")) == operation_id
        ]
        record = {
            "operation_id": operation_id,
            "operation_name": operation.get("name"),
            "setup_id": operation.get("setup_id"),
            "feature_ids": operation.get("feature_ids", []),
            "status": status,
            "evidence": evidence,
            "defects": defects,
            "collisions": collisions,
            "recommended_actions": actions,
            "viewer": {
                "kind": "operation", "operation_id": operation_id,
                "feature_ids": operation.get("feature_ids", []), "mode": "仿真",
            },
        }
        report(
            "verify_operation", f"{operation_id} 工序验证{('通过' if status == 'passed' else '需要处理')}",
            operation_id=operation_id, operation_status=status,
            defect_count=len(defects), collision_count=len(collisions),
            feature_ids=operation.get("feature_ids", []),
        )
        return {
            "records": [*state.get("records", []), record],
            "cursor": int(state.get("cursor", 0)) + 1,
        }

    def route_next(state: OperationExecutionState) -> str:
        return "next" if int(state.get("cursor", 0)) < len(state["operations"]) else "summarize"

    def summarize(state: OperationExecutionState) -> OperationExecutionState:
        records = state.get("records", [])
        counts = {
            status: sum(item.get("status") == status for item in records)
            for status in ("passed", "action_required", "blocked")
        }
        remediation = state["remediation"]
        remediation_status = str(remediation.get("status", "clear"))
        global_defects = [
            item for item in remediation.get("defects", [])
            if isinstance(item, dict) and not item.get("operation_ids")
        ]
        if counts["blocked"] or remediation_status == "blocked":
            status = "blocked"
        elif counts["action_required"] or remediation_status == "action_required":
            status = "action_required"
        else:
            status = "passed"
        summary = {
            "schema_version": "1.0.0",
            "mode": settings.mode,
            "status": status,
            "operation_count": len(records),
            "counts": counts,
            "verification_status": state["verification"].get("status"),
            "collision_status": state["collision"].get("status"),
            "remediation_status": remediation_status,
            "global_defect_count": len(global_defects),
            "global_defect_ids": [str(item.get("id", "")) for item in global_defects],
            "can_auto_replan": bool(remediation.get("can_auto_replan")),
            "iteration": int(remediation.get("iteration", 0) or 0),
            "max_iterations": int(remediation.get("max_iterations", settings.max_local_retries) or settings.max_local_retries),
            "next_action": (
                "manual_review" if status == "blocked"
                else "local_remediation" if status == "action_required" and remediation.get("can_auto_replan")
                else "engineering_review" if status == "action_required"
                else "release"
            ),
        }
        report(
            "summarize_execution", "逐工序智能体验证完成",
            execution_status=status, operation_count=len(records),
            passed=counts["passed"], action_required=counts["action_required"],
            blocked=counts["blocked"], next_action=summary["next_action"],
        )
        return {"status": status, "summary": summary}

    workflow = StateGraph(OperationExecutionState)
    workflow.add_node("select_operation", select_operation)
    workflow.add_node("inspect_toolpath", inspect_toolpath)
    workflow.add_node("inspect_simulation", inspect_simulation)
    workflow.add_node("verify_operation", verify_operation)
    workflow.add_node("summarize_execution", summarize)
    workflow.add_edge(START, "select_operation")
    workflow.add_edge("select_operation", "inspect_toolpath")
    workflow.add_edge("inspect_toolpath", "inspect_simulation")
    workflow.add_edge("inspect_simulation", "verify_operation")
    workflow.add_conditional_edges(
        "verify_operation", route_next,
        {"next": "select_operation", "summarize": "summarize_execution"},
    )
    workflow.add_edge("summarize_execution", END)
    return workflow.compile(checkpointer=checkpointer)


def run_operation_execution_subgraph(
    *,
    job_id: str,
    operations: list[dict[str, Any]],
    cam_result: dict[str, Any],
    simulation: dict[str, Any],
    verification: dict[str, Any],
    collision: dict[str, Any],
    remediation: dict[str, Any],
    settings: AgentSettings,
    progress_callback: ProgressCallback | None = None,
) -> OperationExecutionState:
    if not operations:
        return {
            "job_id": job_id, "mode": settings.mode, "status": "blocked",
            "records": [], "summary": {
                "schema_version": "1.0.0", "status": "blocked",
                "operation_count": 0, "next_action": "manual_review",
            },
        }
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_operation_execution_subgraph(
            settings, checkpointer, progress_callback=progress_callback,
        )
        return graph.invoke({
            "job_id": job_id,
            "mode": settings.mode,
            "operations": operations,
            "cursor": 0,
            "cam_result": cam_result,
            "simulation": simulation,
            "verification": verification,
            "collision": collision,
            "remediation": remediation,
            "records": [],
            "status": "running",
        }, {"configurable": {"thread_id": f"{job_id}:operation-execution"}})
