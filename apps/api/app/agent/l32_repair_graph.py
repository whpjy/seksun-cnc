"""Bounded diagnosis and repair decisions for failed L32 DRAFT compilation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .checkpoint import open_sqlite_checkpointer
from .config import AgentSettings
from .process_strategies import BackLiveToolFaceStrategy


class L32RepairState(TypedDict, total=False):
    job_id: str
    blocker: str
    failed_operations: list[str]
    profile_review_state: str
    coverage_status: str
    manufacturing_context: dict[str, Any]
    diagnosis: dict[str, Any]
    candidates: list[dict[str, Any]]
    decision: str
    status: str
    summary: dict[str, Any]


ProgressCallback = Callable[..., None]
CandidateValidator = Callable[[dict[str, Any]], dict[str, Any]]


def build_l32_repair_graph(
    settings: AgentSettings,
    checkpointer: BaseCheckpointSaver,
    *,
    validate_candidate: CandidateValidator | None = None,
    progress_callback: ProgressCallback | None = None,
):
    def report(stage: str, message: str, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, **details)

    def diagnose(state: L32RepairState) -> L32RepairState:
        blocker = state["blocker"]
        normalized_blocker = blocker.lower()
        missing_back_face = (
            ("missing operations" in normalized_blocker and "op50" in normalized_blocker)
            or "back-face finishing operation" in normalized_blocker
            or "back face finishing operation" in normalized_blocker
        )
        if missing_back_face:
            defect = "missing_back_face_process"
        elif "倒扣" in blocker or "undercut" in blocker.lower():
            defect = "profile_undercut"
        elif "左右手" in blocker or "tool hand" in blocker.lower():
            defect = "tool_hand_mismatch"
        elif "刀具" in blocker or "tool" in blocker.lower():
            defect = "tool_reachability"
        else:
            defect = "unclassified_compile_failure"
        diagnosis = {
            "defect": defect,
            "failed_operations": state.get("failed_operations", []),
            "profile_review_state": state.get("profile_review_state", "unknown"),
            "coverage_status": state.get("coverage_status", "unknown"),
            "manufacturing_context": state.get("manufacturing_context", {}),
            "safety_gate": "production_release_forbidden",
        }
        report(
            "diagnose_failure", "已将 L32 编译失败归因到具体工序与几何约束",
            defect=defect, failed_operations=diagnosis["failed_operations"],
        )
        return {"diagnosis": diagnosis, "status": "running"}

    def propose_repairs(state: L32RepairState) -> L32RepairState:
        defect = state["diagnosis"]["defect"]
        failed = state.get("failed_operations", [])
        context = state.get("manufacturing_context", {})
        if defect == "missing_back_face_process":
            protected_limit = context.get("nonrotational_turning_limit_z_mm")
            capabilities = set(context.get("machine_capabilities") or [])
            safe_back_turning = protected_limit is None and "back_turning" in capabilities
            live_tool_strategy = BackLiveToolFaceStrategy.describe(
                capability_available="back_live_tool_milling" in capabilities,
            )
            live_tool_candidate = live_tool_strategy.model_dump(mode="json")
            live_tool_candidate.update({
                "operation_ids": [],
                "auto_applicable": False,
                "capability_available": "back_live_tool_milling" in capabilities,
                "reason": "用背面动力刀具按非回转边界分区精加工切断端，避免车刀扫过成品实体。",
                "required_evidence": [
                    item.validator for item in live_tool_strategy.validation_requirements
                    if item.required_for_draft
                ],
            })
            candidates = [
                {
                    "id": "restore_op50_back_turning",
                    "kind": "operation_restore",
                    "operation_ids": ["OP50"],
                    "auto_applicable": safe_back_turning,
                    "reason": (
                        "零件没有背面非回转保护区且设备具备 back_turning，可恢复标准 OP50 后重新运行整件连续仿真。"
                        if safe_back_turning else
                        "检测到背面非回转保护区，车削整张端面可能切除成品；安全门禁止自动恢复 OP50。"
                    ),
                    "required_evidence": ["accepted_rotational_profile", "back_turning_capability", "no_nonrotational_protected_region"],
                },
                live_tool_candidate,
                {
                    "id": "secondary_setup_face_finish",
                    "kind": "external_process",
                    "operation_ids": ["OP50-EXT"],
                    "auto_applicable": False,
                    "reason": "将背面端面转移到二次装夹铣削或磨削；需要新增设备、基准和装夹验证。",
                    "required_evidence": ["secondary_machine", "datum_transfer", "fixture_plan", "inspection_plan"],
                },
            ]
        elif defect == "tool_hand_mismatch":
            candidates = [{
                "id": "replace_turning_tool_hand",
                "kind": "tool_substitution",
                "operation_ids": failed,
                "auto_applicable": True,
                "reason": "仅替换为与既定进给方向匹配的同类左右手车刀，并重新运行完整验证",
            }]
        elif defect == "profile_undercut":
            candidates = [
                {
                    "id": "reverse_backside_feed",
                    "kind": "direction_change",
                    "operation_ids": failed,
                    "auto_applicable": False,
                    "reason": "背轴区域必须从切断端进入；反向进给会从材料内部起刀，违反可达性约束",
                },
                {
                    "id": "split_monotonic_backside_regions",
                    "kind": "operation_split",
                    "operation_ids": failed,
                    "auto_applicable": False,
                    "reason": "需要先确认精确回转轮廓，并证明每个子区域均可从背轴端安全进入",
                },
                {
                    "id": "dedicated_grooving_or_form_tool",
                    "kind": "process_change",
                    "operation_ids": failed,
                    "auto_applicable": False,
                    "reason": "倒扣可能需要切槽刀、成形刀或动力刀具；当前实例尚无已验证刀具与碰撞包络",
                },
            ]
        else:
            candidates = [{
                "id": "engineering_review",
                "kind": "manual_review",
                "operation_ids": failed,
                "auto_applicable": False,
                "reason": "当前失败没有满足确定性自动修复所需的结构化证据",
            }]
        report(
            "propose_repairs", f"已形成 {len(candidates)} 个受约束修正候选",
            candidate_count=len(candidates), auto_candidate_count=sum(bool(item["auto_applicable"]) for item in candidates),
        )
        return {"candidates": candidates}

    def validate_repairs(state: L32RepairState) -> L32RepairState:
        evaluated: list[dict[str, Any]] = []
        defect = str((state.get("diagnosis") or {}).get("defect") or "")
        for candidate in state.get("candidates", []):
            item = dict(candidate)
            if not item.get("auto_applicable"):
                item["validation_status"] = (
                    "requires_human_confirmation"
                    if defect == "missing_back_face_process"
                    else "rejected_by_safety_gate"
                )
            elif validate_candidate is None:
                item["validation_status"] = "validator_unavailable"
            else:
                try:
                    result = validate_candidate(item)
                    item["validation"] = result
                    item["validation_status"] = str(result.get("status", "failed"))
                except Exception as error:
                    item["validation_status"] = "failed"
                    item["validation_error"] = str(error)[:500]
            evaluated.append(item)
        report(
            "validate_repairs", "修正候选已通过机床与几何安全门校验",
            passed_candidate_count=sum(item.get("validation_status") == "passed" for item in evaluated),
        )
        return {"candidates": evaluated}

    def decide(state: L32RepairState) -> L32RepairState:
        selected = next((
            item for item in state.get("candidates", [])
            if item.get("validation_status") == "passed"
        ), None)
        decision = "retry" if selected else "human_review"
        status = "repair_ready" if selected else "waiting"
        defect = str((state.get("diagnosis") or {}).get("defect") or "")
        summary = {
            "schema_version": "1.0.0",
            "status": status,
            "decision": decision,
            "selected_candidate": selected,
            "failed_operations": state.get("failed_operations", []),
            "candidate_count": len(state.get("candidates", [])),
            "next_action": (
                "recompile_and_simulate" if selected
                else "select_safe_back_face_process" if defect == "missing_back_face_process"
                else "confirm_profile_and_select_special_process"
            ),
            "diagnosis": state.get("diagnosis", {}),
            "production_ready": False,
        }
        report(
            "repair_decision",
            "已有安全修正候选，可进入重新编译" if selected else "没有通过安全门的自动修正，已转人工确认",
            decision=decision, next_action=summary["next_action"],
        )
        return {"decision": decision, "status": status, "summary": summary}

    graph = StateGraph(L32RepairState)
    graph.add_node("diagnose_failure", diagnose)
    graph.add_node("propose_repairs", propose_repairs)
    graph.add_node("validate_repairs", validate_repairs)
    graph.add_node("repair_decision", decide)
    graph.add_edge(START, "diagnose_failure")
    graph.add_edge("diagnose_failure", "propose_repairs")
    graph.add_edge("propose_repairs", "validate_repairs")
    graph.add_edge("validate_repairs", "repair_decision")
    graph.add_edge("repair_decision", END)
    return graph.compile(checkpointer=checkpointer)


def run_l32_repair_graph(
    *,
    job_id: str,
    blocker: str,
    failed_operations: list[str],
    profile_review_state: str,
    coverage_status: str,
    manufacturing_context: dict[str, Any] | None = None,
    settings: AgentSettings,
    validate_candidate: CandidateValidator | None = None,
    progress_callback: ProgressCallback | None = None,
) -> L32RepairState:
    with open_sqlite_checkpointer(settings.checkpoint_path) as checkpointer:
        graph = build_l32_repair_graph(
            settings, checkpointer,
            validate_candidate=validate_candidate,
            progress_callback=progress_callback,
        )
        return graph.invoke({
            "job_id": job_id,
            "blocker": blocker,
            "failed_operations": failed_operations,
            "profile_review_state": profile_review_state,
            "coverage_status": coverage_status,
            "manufacturing_context": manufacturing_context or {},
            "status": "running",
        }, {"configurable": {"thread_id": f"{job_id}:l32-repair"}})
