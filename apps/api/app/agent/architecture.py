from __future__ import annotations

from typing import Any


TOOLS: tuple[dict[str, Any], ...] = (
    {"id": "world.read", "group": "orchestrator", "label": "读取制造世界模型", "deterministic": True},
    {"id": "world.commit", "group": "orchestrator", "label": "提交工序与毛坯状态", "deterministic": True},
    {"id": "perception.ask", "group": "multimodal", "label": "提出最小必要感知问题", "deterministic": False},
    {"id": "perception.render_evidence", "group": "multimodal", "label": "生成几何证据视图", "deterministic": True},
    {"id": "review.simulation_with_ai", "group": "validation", "label": "AI 多模态仿真审核", "deterministic": False},
    {"id": "geometry.analyze_step", "group": "geometry", "label": "STEP 几何分析", "deterministic": True},
    {"id": "features.normalize", "group": "geometry", "label": "制造特征归一化", "deterministic": True},
    {"id": "vision.review_features", "group": "multimodal", "label": "多模态特征复核", "deterministic": False},
    {"id": "planning.build_draft", "group": "planning", "label": "确定性工艺草案", "deterministic": True},
    {"id": "planning.review_with_qwen", "group": "planning", "label": "Qwen 工艺研判", "deterministic": False},
    {"id": "planning.compile", "group": "planning", "label": "工艺方案编译", "deterministic": True},
    {"id": "cam.generate_toolpath", "group": "execution", "label": "刀路生成", "deterministic": True},
    {"id": "simulation.remove_material", "group": "execution", "label": "材料移除仿真", "deterministic": True},
    {"id": "validation.detect_collision", "group": "validation", "label": "逐工序碰撞与快移检查", "deterministic": True},
    {"id": "validation.check_result", "group": "validation", "label": "成品与安全校验", "deterministic": True},
    {"id": "remediation.propose", "group": "remediation", "label": "缺陷归因与修正建议", "deterministic": False},
    {"id": "human.request_review", "group": "governance", "label": "人工确认", "deterministic": True},
)

SUBGRAPHS: tuple[dict[str, Any], ...] = (
    {
        "id": "manufacturing_orchestrator",
        "label": "制造任务总协调器",
        "status": "implemented",
        "nodes": [
            "decide", "perceive", "plan", "compile", "execute", "review",
            "commit", "repair", "human_review", "complete",
        ],
        "tools": [
            "world.read", "perception.ask", "perception.render_evidence",
            "planning.compile", "cam.generate_toolpath", "simulation.remove_material",
            "review.simulation_with_ai", "world.commit", "human.request_review",
        ],
    },
    {
        "id": "feature_recognition", "label": "特征识别子图",
        "status": "planned",
        "nodes": ["extract_geometry", "normalize_features", "multimodal_review", "validate_features"],
        "tools": ["geometry.analyze_step", "features.normalize", "vision.review_features"],
    },
    {
        "id": "process_planning", "label": "工艺规划子图",
        "status": "implemented",
        "nodes": [
            "review_ai", "synthesize_candidate", "validate_candidate",
            "prepare_operation_audit", "audit_operation", "summarize_operation_audit",
            "replan_from_operation_audit", "decide_promotion",
        ],
        "tools": ["planning.build_draft", "planning.review_with_qwen", "planning.compile"],
    },
    {
        "id": "operation_execution", "label": "逐工序执行子图",
        "status": "implemented",
        "nodes": [
            "select_operation", "inspect_toolpath", "inspect_simulation",
            "inspect_collision", "verify_operation", "summarize_execution",
        ],
        "tools": [
            "cam.generate_toolpath", "simulation.remove_material",
            "validation.detect_collision", "validation.check_result",
        ],
    },
    {
        "id": "validation_remediation", "label": "验证纠错子图",
        "status": "implemented",
        "nodes": [
            "attribute_defect", "choose_repair_scope", "replan_local",
            "regenerate_and_validate", "assess_revalidation", "finalize_remediation",
        ],
        "tools": [
            "validation.check_result", "remediation.propose", "cam.generate_toolpath",
            "simulation.remove_material", "human.request_review",
        ],
    },
)


def agent_architecture() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "orchestrator": "langgraph",
        "execution_mode": "shadow-first",
        "principles": [
            "AI 负责意图判断、策略选择和异常归因",
            "几何、CAM 与验证结果必须来自确定性工具",
            "每个节点输出证据、产物与右侧视图焦点",
            "局部问题优先局部重规划，高风险决策进入人工确认",
        ],
        "subgraphs": list(SUBGRAPHS),
        "tools": list(TOOLS),
        "edges": [
            ["manufacturing_orchestrator", "feature_recognition", "on_demand_perception"],
            ["manufacturing_orchestrator", "process_planning", "plan_or_replan"],
            ["manufacturing_orchestrator", "operation_execution", "execute_next_window"],
            ["manufacturing_orchestrator", "validation_remediation", "repair"],
            ["feature_recognition", "manufacturing_orchestrator", "evidence"],
            ["process_planning", "manufacturing_orchestrator", "candidate"],
            ["operation_execution", "manufacturing_orchestrator", "execution_evidence"],
            ["validation_remediation", "manufacturing_orchestrator", "repair_result"],
            ["manufacturing_orchestrator", "delivery", "stop_conditions_passed"],
        ],
    }
