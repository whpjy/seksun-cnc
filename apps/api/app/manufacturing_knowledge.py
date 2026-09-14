from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import GeometryAnalysis, ProcessKnowledgeAssessment, ProcessPlan
from .operation_library import OPERATION_DEFINITIONS


DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "manufacturing_process_library.json"


@lru_cache(maxsize=1)
def load_manufacturing_library() -> dict[str, Any]:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    codes = [item["code"] for item in payload["processes"]]
    if len(codes) != len(set(codes)):
        raise RuntimeError("Manufacturing process catalog contains duplicate codes")
    if payload["summary"]["process_count"] != len(codes):
        raise RuntimeError("Manufacturing process catalog summary is inconsistent")
    return payload


def manufacturing_process_payload(
    *, family: str | None = None, query: str | None = None, compact: bool = False,
) -> dict[str, Any]:
    library = load_manufacturing_library()
    normalized_query = (query or "").strip().casefold()
    processes = [
        process
        for process in library["processes"]
        if (not family or process["family"] == family)
        and (
            not normalized_query
            or normalized_query in " ".join(
                str(process.get(key) or "")
                for key in ("code", "name", "category", "description", "equipment", "materials")
            ).casefold()
        )
    ]
    if compact:
        processes = [
            {
                "code": process["code"],
                "family": process["family"],
                "family_name": process["family_name"],
                "category": process["category"],
                "name": process["name"],
                "cam_operation_ids": process["cam_operation_ids"],
                "execution_state": process["execution_state"],
                "evidence_level": process["evidence_level"],
            }
            for process in processes
        ]
    return {
        "schema_version": library["schema_version"],
        "catalog_version": library["catalog_version"],
        "summary": {**library["summary"], "result_count": len(processes)},
        "families": [
            {"id": key, "name": name, "count": library["summary"]["family_counts"][key]}
            for name, key in (
                ("切削加工", "cutting"),
                ("特种加工", "special"),
                ("热处理与表面处理", "heat_surface"),
                ("检验质控", "quality"),
            )
        ],
        "processes": processes,
    }


def get_manufacturing_process(code: str) -> dict[str, Any]:
    normalized = code.strip().upper()
    for process in load_manufacturing_library()["processes"]:
        if process["code"] == normalized:
            return process
    raise ValueError(f"Unknown manufacturing process: {code}")


def planning_knowledge_context(process_codes: set[str] | None = None) -> dict[str, Any]:
    library = load_manufacturing_library()
    selected = [
        item for item in library["processes"]
        if process_codes is None or item["code"] in process_codes
    ]
    return {
        "catalog_version": library["catalog_version"],
        "retrieval": {
            "mode": "full" if process_codes is None else "route_and_family_relevant",
            "selected_process_count": len(selected),
            "catalog_process_count": library["summary"]["process_count"],
        },
        "processes": [
            {
                "code": item["code"],
                "family": item["family"],
                "category": item["category"],
                "name": item["name"],
                "cam_operation_ids": item["cam_operation_ids"],
                "evidence_level": item["evidence_level"],
            }
            for item in selected
        ],
        "typical_routes": [
            {
                key: route.get(key)
                for key in ("零件类型", "典型材料", "推荐工艺路线", "关键工序编码")
                if route.get(key) is not None
            }
            for route in library["typical_routes"]
        ],
    }


def assess_plan_knowledge(analysis: GeometryAnalysis, plan: ProcessPlan) -> ProcessKnowledgeAssessment:
    library = load_manufacturing_library()
    operation_mapping: dict[str, str] = library["cam_operation_mapping"]
    definitions = {item.id: item for item in OPERATION_DEFINITIONS}
    operations = [operation for setup in plan.setups for operation in setup.operations]
    unmapped: set[str] = set()
    nonvalidated: set[str] = set()
    route_codes: set[str] = set()
    executable_count = 0

    for operation in operations:
        code = operation_mapping.get(operation.type)
        operation.manufacturing_code = code
        if code:
            route_codes.add(code)
        else:
            unmapped.add(operation.type)
        definition = definitions.get(operation.type)
        if definition and definition.maturity not in {"planned", "experimental"}:
            executable_count += 1
        if not definition or definition.maturity not in {"validated", "production"}:
            nonvalidated.add(operation.type)

    if plan.manufacturing_route:
        route_codes.update(step.process_code for step in plan.manufacturing_route.steps)

    missing_information = (
        list(plan.manufacturing_route.missing_information)
        if plan.manufacturing_route
        else [
            "二维图纸尺寸、公差与基准体系尚未形成结构化输入",
            "表面粗糙度、热处理、表面处理及特殊特性要求尚未形成结构化输入",
            "毛坯状态、批量、实际设备/刀具/夹具库存与成本约束尚未确认",
        ]
    )
    decisions = [
        "确认材料牌号、供货和热处理状态",
        "确认关键尺寸、形位公差、粗糙度及检验放行方案",
        "确认毛坯、装夹基准、工序间余量和实际制造资源",
    ]
    if unmapped or (plan.manufacturing_route and plan.manufacturing_route.status == "incomplete"):
        status = "incomplete"
    else:
        status = "review"
    coverage_ready = bool(plan.coverage and plan.coverage.production_ready)
    production_ready = (
        status == "complete"
        and not nonvalidated
        and not missing_information
        and coverage_ready
    )
    return ProcessKnowledgeAssessment(
        catalog_version=library["catalog_version"],
        catalog_process_count=library["summary"]["process_count"],
        status=status,
        production_ready=production_ready,
        route_process_codes=sorted(route_codes),
        operation_count=len(operations),
        mapped_operation_count=len(operations) - sum(operation.type in unmapped for operation in operations),
        executable_operation_count=executable_count,
        unmapped_operation_ids=sorted(unmapped),
        nonvalidated_operation_ids=sorted(nonvalidated),
        missing_information=missing_information,
        required_engineer_decisions=decisions,
    )
