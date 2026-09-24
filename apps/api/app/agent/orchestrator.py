from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .state import ManufacturingOrchestratorState
from .world_model import ManufacturingWorldModel, OpenQuestion, WorldOperation, utc_now


WorldTool = Callable[[ManufacturingWorldModel, dict[str, Any]], dict[str, Any]]
ProgressCallback = Callable[..., None]


@dataclass(frozen=True, slots=True)
class OrchestratorTools:
    perceive: WorldTool | None = None
    plan: WorldTool | None = None
    compile: WorldTool | None = None
    execute: WorldTool | None = None
    review: WorldTool | None = None
    repair: WorldTool | None = None


def _current_operation(world: ManufacturingWorldModel) -> WorldOperation | None:
    if world.current_operation_id:
        selected = next(
            (item for item in world.operations if item.id == world.current_operation_id),
            None,
        )
        if selected and selected.status != "committed":
            return selected
    return next((item for item in world.operations if item.status != "committed"), None)


def decide_next_action(world: ManufacturingWorldModel, *, max_attempts: int = 3) -> str:
    if world.lifecycle == "completed":
        return "complete"
    blocking_question = next(
        (item for item in world.open_questions if item.status == "open" and item.blocking),
        None,
    )
    if blocking_question:
        return "perceive"
    operation = _current_operation(world)
    if operation is None:
        return "plan" if not world.operations else "complete"
    if operation.status == "proposed":
        return "compile"
    if operation.status == "compiled":
        return "execute"
    if operation.status == "executed":
        return "review"
    if operation.status == "verified":
        return "commit"
    if operation.status == "action_required" and operation.attempts < max_attempts:
        return "repair"
    if operation.status in {"blocked", "action_required"}:
        return "human_review"
    return "plan"


def _record(
    state: ManufacturingOrchestratorState,
    action: str,
    status: str,
    summary: str,
    **details: Any,
) -> list[dict[str, Any]]:
    return [*state.get("trace", []), {
        "sequence": len(state.get("trace", [])) + 1,
        "at": utc_now(),
        "action": action,
        "status": status,
        "summary": summary,
        **details,
    }]


def _merge_evidence(world: ManufacturingWorldModel, result: dict[str, Any]) -> None:
    for item in result.get("evidence", []) or []:
        if not isinstance(item, dict):
            continue
        from .world_model import EvidenceReference
        evidence = EvidenceReference.model_validate(item)
        if not any(existing.id == evidence.id for existing in world.evidence):
            world.evidence.append(evidence)


def build_manufacturing_orchestrator(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    tools: OrchestratorTools,
    progress_callback: ProgressCallback | None = None,
):
    def report(stage: str, message: str, **details: Any) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def decide(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        world = ManufacturingWorldModel.model_validate(state["world"])
        action = decide_next_action(world, max_attempts=settings.max_local_retries)
        operation = _current_operation(world)
        report(
            "orchestrator_decide", f"总协调器决定下一动作：{action}",
            action=action, operation_id=operation.id if operation else None,
            objective=world.current_objective,
        )
        return {
            "action": action,
            "status": "completed" if action == "complete" else "running",
            "halt": action in {"complete", "human_review"},
            "step_count": int(state.get("step_count", 0)) + 1,
            "trace": _record(
                state, "decide", "completed", f"选择 {action}",
                operation_id=operation.id if operation else None,
            ),
        }

    def route_action(state: ManufacturingOrchestratorState) -> str:
        if int(state.get("step_count", 0)) >= int(state.get("max_steps", 32)):
            return "human_review"
        return str(state.get("action", "human_review"))

    def invoke_tool(
        state: ManufacturingOrchestratorState,
        action: str,
        tool: WorldTool | None,
    ) -> ManufacturingOrchestratorState:
        world = ManufacturingWorldModel.model_validate(state["world"])
        operation = _current_operation(world)
        question = next(
            (item for item in world.open_questions if item.status == "open" and item.blocking),
            None,
        )
        context = {
            "operation": operation.model_dump(mode="json") if operation else None,
            "question": question.model_dump(mode="json") if question else None,
            "objective": world.current_objective,
        }
        if tool is None:
            report(
                f"orchestrator_{action}", f"等待工具执行：{action}",
                action=action, operation_id=operation.id if operation else None,
            )
            world.next_action = action  # type: ignore[assignment]
            return {
                "world": world.model_dump(mode="json"),
                "status": "waiting",
                "halt": True,
                "trace": _record(
                    state, action, "waiting", "所需工具尚未注入",
                    operation_id=operation.id if operation else None,
                ),
            }
        try:
            result = tool(world, context)
        except Exception as error:
            report(f"orchestrator_{action}", f"工具执行失败：{error}", action=action)
            return {
                "status": "blocked", "halt": True, "error": str(error),
                "trace": _record(state, action, "failed", str(error)),
            }
        _merge_evidence(world, result)
        if action == "perceive" and question:
            if result.get("resolved", False):
                question.status = "resolved"
                question.answer = str(result.get("answer") or "已由工具证据确认")
                question.evidence_ids.extend(str(item) for item in result.get("evidence_ids", []))
        elif action == "plan":
            for payload in result.get("operations", []) or []:
                candidate = WorldOperation.model_validate(payload)
                if not any(item.id == candidate.id for item in world.operations):
                    world.operations.append(candidate)
            if result.get("current_operation_id"):
                world.current_operation_id = str(result["current_operation_id"])
        elif operation is not None and action == "compile":
            operation.payload.update(dict(result.get("operation") or {}))
            operation.status = "compiled" if result.get("valid", True) else "blocked"
        elif operation is not None and action == "execute":
            operation.attempts += 1
            operation.status = "executed" if result.get("status") == "completed" else "blocked"
            operation.review = {"execution": result}
        elif operation is not None and action == "review":
            verdict = str(result.get("verdict", "blocked"))
            operation.review = {**(operation.review or {}), "review": result}
            operation.status = (
                "verified" if verdict == "passed"
                else "action_required" if verdict == "repair"
                else "blocked"
            )
        elif operation is not None and action == "repair":
            operation.payload.update(dict(result.get("operation") or {}))
            operation.status = "compiled" if result.get("applied", False) else "blocked"
        world.revision += 1
        world.updated_at = utc_now()
        world.decisions.append({
            "at": world.updated_at,
            "kind": f"orchestrator_{action}",
            "operation_id": operation.id if operation else None,
            "summary": result.get("summary", f"{action} 已执行"),
        })
        report(
            f"orchestrator_{action}", str(result.get("summary", f"{action} 已完成")),
            action=action, operation_id=operation.id if operation else None,
        )
        return {
            "world": world.model_dump(mode="json"),
            "status": "running",
            "halt": False,
            "trace": _record(
                state, action, "completed", str(result.get("summary", f"{action} 已完成")),
                operation_id=operation.id if operation else None,
            ),
        }

    def perceive(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        return invoke_tool(state, "perceive", tools.perceive)

    def plan(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        return invoke_tool(state, "plan", tools.plan)

    def compile_operation(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        if tools.compile is None:
            world = ManufacturingWorldModel.model_validate(state["world"])
            operation = _current_operation(world)
            if operation is None:
                return invoke_tool(state, "compile", None)
            operation.status = "compiled"
            world.revision += 1
            world.updated_at = utc_now()
            report("orchestrator_compile", "现有正式工序已通过结构编译", operation_id=operation.id)
            return {
                "world": world.model_dump(mode="json"), "status": "running", "halt": False,
                "trace": _record(state, "compile", "completed", "现有正式工序已通过结构编译", operation_id=operation.id),
            }
        return invoke_tool(state, "compile", tools.compile)

    def execute(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        return invoke_tool(state, "execute", tools.execute)

    def review(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        return invoke_tool(state, "review", tools.review)

    def repair(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        return invoke_tool(state, "repair", tools.repair)

    def commit(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        world = ManufacturingWorldModel.model_validate(state["world"])
        operation = _current_operation(world)
        if operation is None or operation.status != "verified":
            return {"status": "blocked", "halt": True, "error": "没有可提交的已验证工序"}
        operation.status = "committed"
        world.current_operation_id = None
        next_operation = _current_operation(world)
        world.current_operation_id = next_operation.id if next_operation else None
        world.lifecycle = "completed" if next_operation is None else "awaiting_execution"
        world.current_objective = (
            "所有滚动工序均已提交" if next_operation is None
            else f"编译、执行并验证 {next_operation.id} {next_operation.name}"
        )
        world.revision += 1
        world.updated_at = utc_now()
        world.decisions.append({
            "at": world.updated_at, "kind": "commit_operation",
            "operation_id": operation.id, "summary": "硬约束与审核通过，提交工序状态",
        })
        report("orchestrator_commit", f"{operation.id} 已提交", operation_id=operation.id)
        return {
            "world": world.model_dump(mode="json"), "status": "running", "halt": False,
            "trace": _record(state, "commit", "completed", f"{operation.id} 已提交", operation_id=operation.id),
        }

    def human_review(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        world = ManufacturingWorldModel.model_validate(state["world"])
        world.lifecycle = "waiting_human"
        world.next_action = "human_review"
        report("orchestrator_human_review", "自动闭环已暂停，等待工程师裁决")
        return {
            "world": world.model_dump(mode="json"), "status": "waiting", "halt": True,
            "trace": _record(state, "human_review", "waiting", "等待工程师裁决"),
        }

    def complete(state: ManufacturingOrchestratorState) -> ManufacturingOrchestratorState:
        world = ManufacturingWorldModel.model_validate(state["world"])
        world.lifecycle = "completed"
        world.next_action = "complete"
        return {
            "world": world.model_dump(mode="json"), "status": "completed", "halt": True,
            "trace": _record(state, "complete", "completed", "制造目标与停止条件已满足"),
        }

    def route_after_action(state: ManufacturingOrchestratorState) -> str:
        return "end" if state.get("halt") else "continue"

    workflow = StateGraph(ManufacturingOrchestratorState)
    workflow.add_node("decide", decide)
    workflow.add_node("perceive", perceive)
    workflow.add_node("plan", plan)
    workflow.add_node("compile", compile_operation)
    workflow.add_node("execute", execute)
    workflow.add_node("review", review)
    workflow.add_node("commit", commit)
    workflow.add_node("repair", repair)
    workflow.add_node("human_review", human_review)
    workflow.add_node("complete", complete)
    workflow.add_edge(START, "decide")
    workflow.add_conditional_edges("decide", route_action, {
        "perceive": "perceive", "plan": "plan", "compile": "compile",
        "execute": "execute", "review": "review", "commit": "commit",
        "repair": "repair", "human_review": "human_review", "complete": "complete",
    })
    for node in ("perceive", "plan", "compile", "execute", "review", "commit", "repair"):
        workflow.add_conditional_edges(node, route_after_action, {"continue": "decide", "end": END})
    workflow.add_edge("human_review", END)
    workflow.add_edge("complete", END)
    return workflow.compile(checkpointer=checkpointer)


def run_manufacturing_orchestrator(
    *,
    world: ManufacturingWorldModel,
    settings: AgentSettings,
    tools: OrchestratorTools,
    max_steps: int = 32,
    progress_callback: ProgressCallback | None = None,
) -> ManufacturingOrchestratorState:
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_manufacturing_orchestrator(
            settings, checkpointer, tools=tools, progress_callback=progress_callback,
        )
        return graph.invoke({
            "job_id": world.job_id,
            "mode": settings.mode,
            "world": world.model_dump(mode="json"),
            "status": "running",
            "halt": False,
            "step_count": 0,
            "max_steps": max_steps,
            "trace": [],
        }, {"configurable": {"thread_id": f"{world.job_id}:manufacturing-orchestrator"}})
