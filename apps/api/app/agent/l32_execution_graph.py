"""Evidence-gated, per-operation review for an L32 whole-part DRAFT."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .state import L32OperationExecutionState


ProgressCallback = Callable[..., None]
ReviewCallback = Callable[[dict[str, Any]], dict[str, Any]]
_CUTTING_COMMANDS = {"feed_move", "arc_move", "thread_cut", "drill_cycle", "tap_cycle", "cutoff"}


def _operation_commands(
    state: L32OperationExecutionState, operation_id: str, channel_id: str,
) -> list[dict[str, Any]]:
    return [
        command
        for channel in state["toolpath"].get("channels", [])
        if str(channel.get("id", "")) == channel_id
        for command in channel.get("commands", [])
        if str(command.get("operation_id", "")) == operation_id
    ]


def _snapshot(state: L32OperationExecutionState, operation_id: str) -> dict[str, Any] | None:
    return next((
        item for item in state["continuous_simulation"].get("stage_snapshots", [])
        if isinstance(item, dict) and str(item.get("operation_id", "")) == operation_id
    ), None)


def build_l32_operation_execution_graph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    progress_callback: ProgressCallback | None = None,
    review_callback: ReviewCallback | None = None,
):
    def report(stage: str, message: str, **details: Any) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def select_operation(state: L32OperationExecutionState) -> L32OperationExecutionState:
        stage = state["stages"][int(state.get("cursor", 0))]
        operation_id = str(stage["operation_id"])
        report("select_operation", f"正在读取 {operation_id} 的 L32 刀路与材料状态", operation_id=operation_id)
        return {"status": "running", "current_stage": stage, "current_evidence": {}}

    def inspect_toolpath(state: L32OperationExecutionState) -> L32OperationExecutionState:
        stage = state["current_stage"]
        operation_id = str(stage["operation_id"])
        commands = _operation_commands(state, operation_id, str(stage.get("channel_id", "")))
        cutting = [item for item in commands if item.get("type") in _CUTTING_COMMANDS]
        evidence = {
            "toolpath_generated": bool(commands),
            "command_count": len(commands),
            "declared_command_count": int(stage.get("command_count", 0) or 0),
            "cutting_command_count": len(cutting),
            "channel_id": stage.get("channel_id"),
            "phase": stage.get("phase"),
            "toolpath_source": "turning-whole-program-ir.json",
        }
        report(
            "inspect_toolpath", f"{operation_id} 刀路证据已读取",
            operation_id=operation_id, command_count=len(commands), cutting_command_count=len(cutting),
        )
        return {"current_evidence": evidence}

    def inspect_material(state: L32OperationExecutionState) -> L32OperationExecutionState:
        operation_id = str(state["current_stage"]["operation_id"])
        snapshot = _snapshot(state, operation_id)
        metrics = dict(snapshot.get("metrics") or {}) if snapshot else {}
        initial = metrics.get("initial_volume_mm3")
        remaining = metrics.get("remaining_volume_mm3")
        removed = metrics.get("removed_volume_mm3")
        evidence = dict(state.get("current_evidence", {}))
        evidence.update({
            "material_snapshot_available": snapshot is not None,
            "material_state_source": "turning-continuous-simulation.json",
            "initial_volume_mm3": initial,
            "remaining_volume_mm3": remaining,
            "removed_volume_delta_mm3": removed,
            "removal_percent": metrics.get("removal_percent"),
            "source_frame": snapshot.get("source_frame") if snapshot else None,
            "sample_count": len(snapshot.get("after_samples", [])) if snapshot else 0,
        })
        report(
            "inspect_material", f"{operation_id} 连续余料状态已读取",
            operation_id=operation_id, removed_volume_delta_mm3=removed,
            remaining_volume_mm3=remaining,
        )
        return {"current_evidence": evidence}

    def verify_operation(state: L32OperationExecutionState) -> L32OperationExecutionState:
        stage = state["current_stage"]
        operation_id = str(stage["operation_id"])
        operation = state["operations"].get(operation_id, {})
        evidence = dict(state.get("current_evidence", {}))
        reasons: list[str] = []
        if not evidence.get("toolpath_generated"):
            reasons.append("missing_toolpath")
        if int(evidence.get("command_count") or 0) != int(evidence.get("declared_command_count") or 0):
            reasons.append("command_count_mismatch")
        if int(evidence.get("cutting_command_count") or 0) <= 0:
            reasons.append("missing_cutting_command")
        if not evidence.get("material_snapshot_available") or int(evidence.get("sample_count") or 0) < 2:
            reasons.append("missing_material_snapshot")
        initial = evidence.get("initial_volume_mm3")
        remaining = evidence.get("remaining_volume_mm3")
        if initial is None or remaining is None or float(remaining) > float(initial) + 1e-6:
            reasons.append("material_volume_increased")
        if float(evidence.get("removed_volume_delta_mm3") or 0) <= 1e-6:
            reasons.append("zero_measured_removal")
        verification_status = str(stage.get("verification_status", "not_applicable"))
        if verification_status == "failed":
            reasons.append("profile_verification_failed")
        status = "blocked" if any(reason in reasons for reason in {
            "missing_toolpath", "command_count_mismatch", "missing_cutting_command",
            "missing_material_snapshot", "material_volume_increased", "profile_verification_failed",
        }) else "action_required" if reasons or verification_status == "warning" else "passed"
        record = {
            "operation_id": operation_id,
            "operation_name": stage.get("operation_name") or operation.get("name"),
            "operation": operation,
            "channel_id": stage.get("channel_id"),
            "phase": stage.get("phase"),
            "status": status,
            "verification_status": verification_status,
            "evidence": evidence,
            "blocking_reasons": reasons,
            "release_status": "DRAFT",
            "production_ready": False,
            "viewer": {"kind": "operation", "operation_id": operation_id, "mode": "仿真"},
        }
        report(
            "verify_operation", f"{operation_id} 确定性审核：{status}",
            operation_id=operation_id, operation_status=status,
            verification_status=verification_status, blocking_reason_count=len(reasons),
        )
        return {"records": [*state.get("records", []), record]}

    def review_operation(state: L32OperationExecutionState) -> L32OperationExecutionState:
        records = list(state.get("records", []))
        record = dict(records[-1])
        operation_id = str(record["operation_id"])
        if record["status"] != "passed":
            review: dict[str, Any] = {"status": "skipped", "reason": "deterministic_gate_not_passed"}
        elif review_callback is None:
            review = {"status": "not_configured", "reason": "deterministic_draft_review"}
        else:
            try:
                review = review_callback(record)
            except Exception as error:
                review = {"status": "unavailable", "reason": str(error)[:500]}
            result = review.get("review") if isinstance(review, dict) else None
            verdict = str(result.get("verdict")) if isinstance(result, dict) else "insufficient_evidence"
            confidence = float(result.get("confidence") or 0) if isinstance(result, dict) else 0
            if verdict == "blocked":
                record["status"] = "blocked"
            elif verdict != "passed" or confidence < 0.65 or result.get("missing_evidence"):
                record["status"] = "action_required"
        record["ai_review"] = review
        records[-1] = record
        report(
            "review_operation", f"{operation_id} L32 工序审核：{record['status']}",
            operation_id=operation_id, operation_status=record["status"],
            ai_verdict=(review.get("review") or {}).get("verdict") if isinstance(review, dict) else None,
        )
        return {"records": records, "cursor": int(state.get("cursor", 0)) + 1}

    def route_next(state: L32OperationExecutionState) -> str:
        records = state.get("records", [])
        if records and records[-1].get("status") in {"blocked", "action_required"}:
            return "summarize"
        return "next" if int(state.get("cursor", 0)) < len(state["stages"]) else "summarize"

    def summarize(state: L32OperationExecutionState) -> L32OperationExecutionState:
        records = list(state.get("records", []))
        simulation = state["continuous_simulation"]
        global_failures = [
            item for item in simulation.get("checks", [])
            if isinstance(item, dict) and item.get("status") == "failed"
        ]
        counts = {name: sum(item.get("status") == name for item in records) for name in ("passed", "action_required", "blocked")}
        if counts["blocked"] or simulation.get("status") != "passed" or global_failures:
            status = "blocked"
        elif counts["action_required"] or len(records) != len(state["stages"]):
            status = "action_required"
        else:
            status = "passed"
        summary = {
            "schema_version": "1.0.0",
            "status": status,
            "release_status": "DRAFT",
            "production_ready": False,
            "operation_count": len(records),
            "expected_operation_count": len(state["stages"]),
            "skipped_operation_ids": [str(item["operation_id"]) for item in state["stages"][len(records):]],
            "counts": counts,
            "continuous_simulation_status": simulation.get("status"),
            "global_failure_ids": [str(item.get("id", "")) for item in global_failures],
            "machine_collision_status": "not_verified",
            "next_action": "human_review" if status != "passed" else "machine_level_validation",
        }
        report(
            "summarize_execution", f"L32 逐工序 DRAFT 审核完成：{status}",
            execution_status=status, operation_count=len(records), next_action=summary["next_action"],
        )
        return {"status": status, "summary": summary}

    graph = StateGraph(L32OperationExecutionState)
    graph.add_node("select_operation", select_operation)
    graph.add_node("inspect_toolpath", inspect_toolpath)
    graph.add_node("inspect_material", inspect_material)
    graph.add_node("verify_operation", verify_operation)
    graph.add_node("review_operation", review_operation)
    graph.add_node("summarize_execution", summarize)
    graph.add_edge(START, "select_operation")
    graph.add_edge("select_operation", "inspect_toolpath")
    graph.add_edge("inspect_toolpath", "inspect_material")
    graph.add_edge("inspect_material", "verify_operation")
    graph.add_edge("verify_operation", "review_operation")
    graph.add_conditional_edges("review_operation", route_next, {"next": "select_operation", "summarize": "summarize_execution"})
    graph.add_edge("summarize_execution", END)
    return graph.compile(checkpointer=checkpointer)


def run_l32_operation_execution_graph(
    *, job_id: str, stages: list[dict[str, Any]], operations: dict[str, dict[str, Any]],
    toolpath: dict[str, Any], continuous_simulation: dict[str, Any], settings: AgentSettings,
    progress_callback: ProgressCallback | None = None,
    review_callback: ReviewCallback | None = None,
) -> L32OperationExecutionState:
    if not stages:
        return {"job_id": job_id, "status": "blocked", "records": [], "summary": {
            "status": "blocked", "release_status": "DRAFT", "production_ready": False,
            "operation_count": 0, "expected_operation_count": 0, "next_action": "human_review",
        }}
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_l32_operation_execution_graph(
            settings, checkpointer, progress_callback=progress_callback, review_callback=review_callback,
        )
        return graph.invoke({
            "job_id": job_id, "mode": settings.mode, "stages": stages, "operations": operations,
            "toolpath": toolpath, "continuous_simulation": continuous_simulation,
            "cursor": 0, "records": [], "status": "running",
        }, {"configurable": {"thread_id": f"{job_id}:l32-operation-execution"}})
