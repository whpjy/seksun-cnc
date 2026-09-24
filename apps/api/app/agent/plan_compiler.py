from __future__ import annotations

from collections import Counter
from typing import Any

from ..catalogs import get_tool
from ..models import GeometryAnalysis, Operation, ProcessPlan, Setup, Vec3
from ..operation_library import (
    create_operation_instance,
    get_operation_definition,
    validate_parameters,
)


def _known_feature_ids(analysis: GeometryAnalysis, baseline: ProcessPlan) -> set[str]:
    result = {
        item.id
        for items in (
            analysis.planar_features,
            analysis.cylindrical_features,
            analysis.prismatic_features,
            analysis.planar_machining_features,
            analysis.internal_profile_features,
        )
        for item in items
    }
    # L32 rotational profiles and synthetic surface sets are created by the
    # geometry tools and are not all represented directly on GeometryAnalysis.
    result.update(
        feature_id
        for setup in baseline.setups
        for operation in setup.operations
        for feature_id in operation.feature_ids
    )
    return result


def compile_agent_plan(
    analysis: GeometryAnalysis,
    baseline: ProcessPlan,
    guidance: dict[str, Any],
    *,
    agent_mode: str,
) -> ProcessPlan:
    """Compile AI decisions into a typed and deterministically verifiable plan.

    The deterministic plan supplies geometry-derived stock, setup frames and safe
    parameter templates. It is not copied wholesale: every retained operation
    must be selected explicitly by the AI agent.
    """

    review = guidance.get("review", {})
    recommendations = list(review.get("operation_recommendations") or [])
    candidate = baseline.model_copy(deep=True)
    setup_by_id = {setup.id: setup for setup in candidate.setups}
    baseline_operations = {
        operation.id: (setup.id, operation)
        for setup in baseline.setups
        for operation in setup.operations
    }
    known_features = _known_feature_ids(analysis, baseline)
    issues: list[str] = []

    def setup_axis(feature_ids: list[str]) -> tuple[Vec3, str | None]:
        for feature_id in feature_ids:
            planar = next((item for item in analysis.planar_features if item.id == feature_id), None)
            if planar is not None:
                return planar.normal.model_copy(deep=True), planar.id
            prismatic = next((item for item in analysis.prismatic_features if item.id == feature_id), None)
            if prismatic is not None:
                return prismatic.access_direction.model_copy(deep=True), prismatic.id
            cylindrical = next((item for item in analysis.cylindrical_features if item.id == feature_id), None)
            if cylindrical is not None:
                direction = cylindrical.access_direction or cylindrical.axis
                return direction.model_copy(deep=True), cylindrical.id
        return Vec3(x=0, y=0, z=1), None

    unknown_setup_ids = sorted({
        str(item.get("setup_id", ""))
        for item in recommendations
        if item.get("action") == "add" and str(item.get("setup_id", "")) not in setup_by_id
    })
    setup_strategy = list(review.get("setup_strategy") or [])
    for setup_id in unknown_setup_ids:
        related = [item for item in recommendations if str(item.get("setup_id", "")) == setup_id]
        feature_ids = [feature_id for item in related for feature_id in item.get("feature_ids", [])]
        axis, datum = setup_axis(feature_ids)
        strategy = next(
            (str(item) for item in setup_strategy if str(item).startswith(setup_id)),
            "AI 新增装夹，夹具与基准等待工程师确认",
        )
        setup = Setup(
            id=setup_id,
            name=f"AI 规划铣削装夹 {setup_id}",
            work_axis=axis,
            datum_feature_id=datum,
            fixture=strategy,
            operations=[],
            machine_id="vmc-850",
            machine_name="VMC850 三轴立式加工中心（AI 建议）",
        )
        candidate.setups.append(setup)
        setup_by_id[setup_id] = setup
        issues.append(f"AI 新建了装夹 {setup_id}，机床、夹具和基准仍需工程师确认")

    duplicate_ids = [
        operation_id
        for operation_id, count in Counter(
            str(item.get("operation_id", "")).strip()
            for item in recommendations
            if item.get("action") != "add"
        ).items()
        if operation_id and count > 1
    ]
    if duplicate_ids:
        issues.append("AI 对同一工序给出了重复决策：" + "、".join(sorted(duplicate_ids)))

    recommendation_by_operation: dict[str, dict[str, Any]] = {}
    for item in recommendations:
        if item.get("action") != "add":
            recommendation_by_operation.setdefault(str(item.get("operation_id", "")), item)

    compiled_by_setup: dict[str, list[tuple[int, Operation]]] = {
        setup.id: [] for setup in candidate.setups
    }
    for operation_id, (setup_id, baseline_operation) in baseline_operations.items():
        recommendation = recommendation_by_operation.get(operation_id)
        if recommendation is None:
            issues.append(f"AI 未对基线工序 {operation_id} 给出明确决策，该工序未进入 AI 方案")
            continue
        action = str(recommendation.get("action", "review"))
        if action == "remove":
            continue
        if action not in {"keep", "modify", "reorder", "review"}:
            issues.append(f"{operation_id} 的 AI 动作 {action} 无法编译")
            continue

        operation = baseline_operation.model_copy(deep=True)
        requested_type = str(recommendation.get("operation_type") or operation.type)
        if requested_type != operation.type:
            issues.append(
                f"{operation_id} 请求从 {operation.type} 改为 {requested_type}；"
                "类型变更必须使用 add/remove，已保留原类型并等待复核"
            )
        feature_ids = list(recommendation.get("feature_ids") or operation.feature_ids)
        unknown = sorted(set(feature_ids) - known_features)
        if unknown:
            issues.append(f"{operation_id} 引用了未知特征：{'、'.join(unknown)}")
        else:
            operation.feature_ids = feature_ids

        tool_id = recommendation.get("tool_id")
        if tool_id:
            try:
                tool = get_tool(str(tool_id))
                definition = get_operation_definition(operation.type)
                if tool.kind not in definition.tool.accepts:
                    raise ValueError(f"刀具类型 {tool.kind} 不适用于 {operation.type}")
                operation.tool = tool
            except ValueError as error:
                issues.append(f"{operation_id} 刀具编译失败：{error}")

        supplied_parameters = recommendation.get("parameters") or {}
        if supplied_parameters:
            try:
                operation.parameters = validate_parameters(
                    get_operation_definition(operation.type),
                    {**operation.parameters, **supplied_parameters},
                )
            except ValueError as error:
                issues.append(f"{operation_id} 参数编译失败：{error}")

        if recommendation.get("name"):
            operation.name = str(recommendation["name"])
        reason = str(recommendation.get("reason") or "AI 智能体选择该工序")
        operation.rationale = [f"AI 决策：{reason}", *operation.rationale]
        operation.source = "recommendation"
        operation.status = "warning" if action in {"modify", "review"} else "proposed"
        operation.generation_state = "dirty"
        compiled_by_setup[setup_id].append((int(recommendation.get("priority", 100)), operation))

    for recommendation in recommendations:
        if recommendation.get("action") != "add":
            continue
        setup_id = str(recommendation.get("setup_id", ""))
        setup = setup_by_id.get(setup_id)
        operation_id = str(recommendation.get("operation_id", "")).strip()
        if setup is None:
            issues.append(f"新增工序 {operation_id or '(未编号)'} 引用了无法创建的装夹 {setup_id}")
            continue
        if not operation_id or operation_id in baseline_operations:
            issues.append(f"新增工序编号无效或重复：{operation_id or '(空)'}")
            continue
        operation_type = str(recommendation.get("operation_type", ""))
        feature_ids = list(recommendation.get("feature_ids") or [])
        unknown = sorted(set(feature_ids) - known_features)
        if unknown:
            issues.append(f"新增工序 {operation_id} 引用了未知特征：{'、'.join(unknown)}")
            continue
        try:
            definition = get_operation_definition(operation_type)
            requested_tool_id = str(recommendation.get("tool_id") or definition.tool.default_tool_id)
            try:
                tool = get_tool(requested_tool_id)
            except ValueError:
                tool = get_tool(definition.tool.default_tool_id)
                issues.append(
                    f"新增工序 {operation_id} 请求的刀具 {requested_tool_id} 不在目录中，"
                    f"暂用 {definition.tool.default_tool_id} 并等待工程师确认"
                )
            if tool.kind not in definition.tool.accepts:
                raise ValueError(f"刀具类型 {tool.kind} 不适用于 {operation_type}")
            operation = create_operation_instance(
                id=operation_id,
                sequence=0,
                type=operation_type,
                name=str(recommendation.get("name") or definition.name),
                feature_ids=feature_ids,
                tool=tool,
                parameters=dict(recommendation.get("parameters") or {}),
                rationale=[f"AI 新增：{recommendation.get('reason') or '补足制造特征覆盖'}"],
                confidence=float(review.get("confidence", 0) or 0),
                status="warning",
                source="recommendation",
            )
        except ValueError as error:
            issues.append(f"新增工序 {operation_id} 编译失败：{error}")
            continue
        compiled_by_setup[setup.id].append((int(recommendation.get("priority", 100)), operation))
        baseline_operations[operation_id] = (setup.id, operation)

    for setup in candidate.setups:
        ordered = sorted(
            compiled_by_setup[setup.id],
            key=lambda item: (item[0], item[1].sequence),
        )
        setup.operations = [item[1] for item in ordered]
        for index, operation in enumerate(setup.operations, 1):
            operation.sequence = index * 10

    candidate.setups = [setup for setup in candidate.setups if setup.operations]

    candidate.ai_planning = {
        "status": "working_draft",
        "provider": guidance.get("provider"),
        "model": guidance.get("model"),
        "created_at": guidance.get("created_at"),
        "manufacturing_intent": review.get("manufacturing_intent"),
        "recommended_process_kind": review.get("recommended_process_kind"),
        "part_family": review.get("part_family"),
        "confidence": float(review.get("confidence", 0) or 0),
        "summary": review.get("summary"),
        "requires_engineer_review": review.get("requires_engineer_review", False),
        "agent_mode": agent_mode,
        "planner": "langgraph_ai_primary",
        "compiler_issue_count": len(issues),
        "compiler_issues": issues,
    }
    candidate.warnings = [warning for warning in candidate.warnings if "AI" not in warning]
    candidate.warnings.append("当前工艺路线由 AI 智能体规划，须通过确定性校验和工程师审批后方可生产")
    if issues:
        candidate.blocking_reasons = [*candidate.blocking_reasons, *issues]
        candidate.automation_status = "review"
    return candidate
