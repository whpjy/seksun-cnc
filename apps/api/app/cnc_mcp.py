"""Compact MCP bridge from DeepSeek Harness to the CNC domain API.

The bridge deliberately returns summaries and evidence references instead of
large geometry/simulation JSON payloads.  Tool execution remains in the CNC
API, which is the single source of truth for machine constraints and artifacts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP, Image


DEFAULT_API_BASE_URL = "http://127.0.0.1:8090"


class CncApiError(RuntimeError):
    """A bounded error suitable for presentation to an agent."""


class CncApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("CNC_API_BASE_URL") or DEFAULT_API_BASE_URL).rstrip("/")
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise CncApiError(f"CNC API 请求超时：{path}") from exc
        except httpx.RequestError as exc:
            raise CncApiError(f"无法连接 CNC API {self.base_url}：{type(exc).__name__}") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = response.text
            raise CncApiError(f"CNC API {response.status_code}：{str(detail)[:1000]}")
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            return self.request(method, path, **kwargs).json()
        except ValueError as exc:
            raise CncApiError(f"CNC API 返回了无效 JSON：{path}") from exc

    def upload_step(
        self,
        source: Path,
        *,
        device_id: str,
        material: str,
    ) -> dict[str, Any]:
        with source.open("rb") as stream:
            response = self.request(
                "POST",
                "/api/v1/jobs/intake",
                files={"step": (source.name, stream, "application/step")},
                data={"device_id": device_id, "material": material},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CncApiError("CNC API 返回了无效的任务创建响应") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise CncApiError("CNC API 未返回有效的 job_id")
        return payload


def _client() -> CncApiClient:
    return CncApiClient()


_active_job_id = os.getenv("CNC_ACTIVE_JOB_ID", "").strip()


def _import_roots() -> list[Path]:
    configured = os.getenv("CNC_MCP_IMPORT_ROOTS", "").strip()
    if configured:
        return [Path(item).expanduser().resolve() for item in configured.split(os.pathsep) if item.strip()]
    repository = Path(__file__).resolve().parents[3]
    return [repository, repository.parent]


def resolve_step_import(step_path: str) -> Path:
    """Resolve a Harness attachment without granting arbitrary filesystem reads."""
    if not step_path.strip():
        raise CncApiError("step_path 不能为空；请传入 Harness 上传附件的本地路径")
    candidate = Path(step_path).expanduser().resolve()
    if not candidate.is_file():
        raise CncApiError(f"STEP 文件不存在：{candidate.name}")
    if candidate.suffix.lower() not in {".step", ".stp"}:
        raise CncApiError("仅支持 .step 或 .stp 文件")
    roots = _import_roots()
    if not any(candidate.is_relative_to(root) for root in roots):
        raise CncApiError(
            "STEP 文件不在允许导入的目录中；请设置 CNC_MCP_IMPORT_ROOTS 包含 Harness 上传目录"
        )
    maximum = int(os.getenv("CNC_MCP_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
    if candidate.stat().st_size > maximum:
        raise CncApiError(f"STEP 文件超过 Harness 导入上限 {maximum // 1024 // 1024} MB")
    return candidate


def _resolve_job_id(job_id: str = "") -> tuple[str, str]:
    """Resolve an explicit, configured, previously bound, or latest local job."""
    global _active_job_id
    requested = job_id.strip()
    if requested:
        _active_job_id = requested
        return requested, "explicit"
    if _active_job_id:
        return _active_job_id, "bound_context"
    jobs = _client().json("GET", "/api/v1/jobs")
    if not isinstance(jobs, list):
        raise CncApiError("CNC 任务列表响应格式无效")
    latest = next((item for item in jobs if isinstance(item, dict) and item.get("status") == "completed"), None)
    if latest is None or not latest.get("id"):
        raise CncApiError("没有可绑定的已完成 CNC 任务，请显式提供 job_id")
    _active_job_id = str(latest["id"])
    return _active_job_id, "latest_completed"


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("plan") or {}
    coverage = plan.get("coverage") or {}
    setups = [
        {
            "id": setup.get("id"),
            "name": setup.get("name"),
            "workpiece_side": (
                setup.get("workpiece_side")
                or ("back" if setup.get("id") == "SETUP-L32-SUB" else "front")
                if job.get("device_id") == "citizen-cincom-l32" else None
            ),
            "operation_count": len(setup.get("operations") or []),
        }
        for setup in plan.get("setups", [])
        if isinstance(setup, dict)
    ]
    operations = [
        {
            "id": operation.get("id"),
            "name": operation.get("name"),
            "type": operation.get("type"),
            "setup_id": setup.get("id"),
            "enabled": operation.get("enabled", True),
            "tool_id": (operation.get("tool") or {}).get("id"),
            "feature_ids": operation.get("feature_ids") or [],
            "reference_profile_id": operation.get("reference_profile_id"),
            "status": operation.get("status"),
        }
        for setup in plan.get("setups", [])
        for operation in setup.get("operations", [])
    ]
    return {
        "job_id": job.get("id"),
        "filename": job.get("filename"),
        "status": job.get("status"),
        "device_id": job.get("device_id"),
        "machine_instance_id": job.get("machine_instance_id"),
        "material": job.get("material"),
        "stock": plan.get("stock") or {},
        # Empty setups are decision inputs. Harness must copy an exact ID
        # instead of inventing aliases such as setup-1, front, or SU-1.
        "setups": setups,
        "setup_selection_rule": "add_process_operation.setup_id must exactly match one of setups[].id",
        "automation_status": plan.get("automation_status"),
        "coverage": {
            "status": coverage.get("status"),
            "target_count": coverage.get("target_count"),
            "covered_count": coverage.get("covered_count"),
            "review_count": coverage.get("review_count"),
            "production_ready": coverage.get("production_ready", False),
        },
        "warnings": (plan.get("warnings") or [])[:20],
        "blocking_reasons": (plan.get("blocking_reasons") or [])[:20],
        "operations": operations,
        "release_status": "DRAFT",
        "production_ready": False,
    }


def summarize_geometry(job: dict[str, Any], rotational: dict[str, Any] | None) -> dict[str, Any]:
    analysis = job.get("analysis") or {}
    feature_groups = {
        name: [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "confidence": item.get("confidence"),
                "review_state": item.get("review_state"),
                "diameter": item.get("diameter"),
                "length": item.get("length"),
                "depth": item.get("depth"),
            }
            for item in analysis.get(name, [])[:100]
        ]
        for name in (
            "planar_features", "cylindrical_features", "prismatic_features",
            "planar_machining_features", "internal_profile_features",
        )
    }
    profiles = []
    for profile in (rotational or {}).get("profiles", [])[:30]:
        points = profile.get("points") or []
        z_values = [float(point["z"]) for point in points if isinstance(point, dict) and "z" in point]
        radius_values = [float(point["radius"]) for point in points if isinstance(point, dict) and "radius" in point]
        profiles.append({
            "id": profile.get("id"),
            "side": profile.get("side"),
            "confidence": profile.get("confidence"),
            "review_state": profile.get("review_state"),
            "point_count": len(points),
            "z_range_mm": [min(z_values), max(z_values)] if z_values else None,
            "radius_range_mm": [min(radius_values), max(radius_values)] if radius_values else None,
            "warnings": (profile.get("warnings") or [])[:10],
        })
    derived_regions = []
    nonrotational_region = ((job.get("plan") or {}).get("stock") or {}).get(
        "nonrotational_region_z_mm"
    )
    if isinstance(nonrotational_region, list) and len(nonrotational_region) == 2:
        derived_regions.append({
            "id": "REGION-NONROTATIONAL-OUTER-1",
            "kind": "nonrotational_outer_region",
            "geometry_type": "solid",
            "z_range_mm": sorted(float(value) for value in nonrotational_region),
            "purpose": "live_tool_contour_roughing_or_finishing",
        })
    return {
        "job_id": job.get("id"),
        "topology": analysis.get("topology") or {},
        "measurements": analysis.get("measurements") or {},
        "feature_counts": {key: len(value) for key, value in feature_groups.items()},
        "features": feature_groups,
        "rotational_profiles": profiles,
        "derived_regions": derived_regions,
        "evidence_policy": "结构化几何是确定性证据；视觉结论必须通过 observe_model 交叉检查。",
    }


def collect_geometry_references(
    job: dict[str, Any], rotational: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build the exact geometry vocabulary accepted by operation creation."""
    analysis = job.get("analysis") or {}
    references: dict[str, dict[str, Any]] = {}

    def add(identifier: Any, geometry_type: str, kind: Any = None, state: Any = None) -> None:
        if identifier:
            references[str(identifier)] = {
                "id": str(identifier), "geometry_type": geometry_type,
                "kind": kind, "review_state": state,
            }

    for item in analysis.get("planar_features") or []:
        add(item.get("id"), "planar_face", "planar_face", item.get("review_state"))
    for item in analysis.get("cylindrical_features") or []:
        kind = item.get("kind")
        add(item.get("id"), "cylindrical_hole" if kind == "hole" else "cylindrical_face", kind, item.get("review_state"))
    for item in analysis.get("prismatic_features") or []:
        add(item.get("id"), f"prismatic_{item.get('kind')}", item.get("kind"), item.get("review_state"))
    for item in analysis.get("planar_machining_features") or []:
        add(item.get("id"), "planar_surface", item.get("kind"), item.get("review_state"))
    for item in analysis.get("internal_profile_features") or []:
        add(item.get("id"), "internal_profile", item.get("kind"), item.get("review_state"))
    for item in (rotational or {}).get("profiles") or []:
        add(
            item.get("id"),
            "inner_rotational_profile" if item.get("side") == "inner" else "outer_rotational_profile",
            item.get("side"), item.get("review_state"),
        )
    rotational_types = {
        "external_groove_candidate": "od_groove",
        "internal_groove_candidate": "id_groove",
        "cutoff_boundary": "cutoff_plane",
        "inner_bore": "inner_rotational_profile",
        "inner_taper": "inner_rotational_profile",
    }
    for item in (rotational or {}).get("features") or []:
        kind = item.get("kind")
        geometry_type = rotational_types.get(kind, "outer_rotational_profile")
        if kind == "thread_form_candidate":
            geometry_type = "internal_thread" if item.get("thread_side") == "internal" else "external_thread"
        add(item.get("id"), geometry_type, kind, item.get("review_state"))

    stock = ((job.get("plan") or {}).get("stock") or {})
    region = stock.get("nonrotational_region_z_mm")
    if isinstance(region, list) and len(region) == 2:
        references["REGION-NONROTATIONAL-OUTER-1"] = {
            "id": "REGION-NONROTATIONAL-OUTER-1",
            "geometry_type": "solid",
            "kind": "nonrotational_outer_region",
            "review_state": "derived_from_exact_step_and_stock_partition",
            "z_range_mm": sorted(float(value) for value in region),
        }
    return list(references.values())


def summarize_operation_catalog(
    payload: dict[str, Any], job: dict[str, Any], rotational: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only operations executable by the current agent loop and their legal geometry IDs."""
    l32 = job.get("device_id") == "citizen-cincom-l32"
    l32_nonrotational = {
        "pocket_roughing", "pocket_finishing",
        "live_tool_contour_roughing", "live_tool_contour_finishing",
    }
    references = collect_geometry_references(job, rotational)
    definitions = []
    unavailable = []
    for item in payload.get("definitions") or []:
        if not isinstance(item, dict):
            continue
        engine = item.get("engine") or {}
        agent_supported = (
            engine.get("provider") == "turning" or item.get("id") in l32_nonrotational
        ) if l32 else bool(item.get("manual_enabled"))
        accepts = set((item.get("geometry") or {}).get("accepts") or [])
        compatible = [reference for reference in references if reference["geometry_type"] in accepts]
        minimum = int((item.get("geometry") or {}).get("minimum_selection") or 1)
        if not agent_supported or len(compatible) < minimum:
            unavailable.append(item.get("id"))
            continue
        if item.get("id") in {"live_tool_contour_roughing", "live_tool_contour_finishing"}:
            compatible.sort(key=lambda value: value["id"] != "REGION-NONROTATIONAL-OUTER-1")
        definitions.append({
            "id": item.get("id"), "name": item.get("name"),
            "category": item.get("category"), "maturity": item.get("maturity"),
            "geometry": item.get("geometry"), "tool": item.get("tool"),
            "parameters": item.get("parameters"), "engine": engine,
            "valid_geometry_refs": compatible,
            "selection_rule": "feature_ids must be copied exactly from valid_geometry_refs[].id",
        })
    return {
        "schema_version": payload.get("schema_version"),
        "job_id": job.get("id"),
        "definitions": definitions,
        "unavailable_definition_ids": unavailable,
        "catalog_rule": (
            "Only definitions[] may be passed to add_process_operation. Never invent a geometry ID, "
            "and never call an unavailable definition."
        ),
    }


def summarize_progress(job: dict[str, Any], workspace: dict[str, Any] | None) -> dict[str, Any]:
    events = (workspace or {}).get("events") or []
    compact_events = []
    for event in events[-12:]:
        if not isinstance(event, dict):
            continue
        compact_events.append({
            "stage": event.get("stage"),
            "title": event.get("title") or event.get("agent_title"),
            "message": event.get("message"),
            "status": event.get("status") or event.get("agent_status"),
            "percent": event.get("percent"),
            "operation_id": event.get("operation_id"),
        })
    latest = compact_events[-1] if compact_events else None
    return {
        "job_id": job.get("id"),
        "status": job.get("status"),
        "filename": job.get("filename"),
        "device_id": job.get("device_id"),
        "latest_event": latest,
        "recent_events": compact_events,
        "error": job.get("error"),
        "ready_for_agent_review": job.get("status") == "completed" and bool(job.get("analysis")),
        "planning_source": (
            "deepseek_harness" if job.get("plan") is None
            or ((job.get("plan") or {}).get("ai_planning") or {}).get("planning_owner") == "deepseek_harness"
            else "cnc_backend_ai_assisted_baseline"
        ),
        "next_action": (
            "initialize_process_draft" if job.get("status") == "completed" and not job.get("plan")
            else "inspect_job_and_geometry" if job.get("status") == "completed"
            else "inspect_job_progress" if job.get("status") == "processing"
            else "inspect_failure"
        ),
    }


def summarize_tool_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """Return decision-grade inventory evidence without leaking storage payload noise."""
    tools = []
    for item in payload.get("tools") or []:
        if not isinstance(item, dict):
            continue
        tools.append({
            "inventory_id": item.get("inventory_id"),
            "name": item.get("catalog_tool_name") or item.get("custom_name"),
            "kind": item.get("tool_kind") or item.get("custom_kind"),
            "station": item.get("station"),
            "active": item.get("active", True),
            "verification_state": item.get("verification_state"),
            "measured_cutting_width_mm": item.get("measured_cutting_width_mm"),
            "measured_nose_radius_mm": item.get("measured_nose_radius_mm"),
            "measured_stickout_mm": item.get("measured_stickout_mm"),
            "groove_profile": item.get("groove_profile"),
            "axial_contouring_supported": item.get("axial_contouring_supported", False),
            "capability_verified_by": item.get("capability_verified_by"),
            "capability_verification_reference": item.get("capability_verification_reference"),
        })
    return {
        "machine_instance_id": payload.get("machine_instance_id"),
        "tool_count": len(tools),
        "active_tool_count": sum(1 for item in tools if item["active"]),
        "tools": tools,
        "evidence_policy": (
            "Measurements and capability evidence must come from physical inspection; "
            "the agent must never infer or fabricate them."
        ),
    }


def summarize_tool_catalog(
    payload: dict[str, Any],
    *,
    tool_kind: str = "",
    maximum_cutting_width_mm: float | None = None,
) -> dict[str, Any]:
    """Expose compact tool capabilities under the assumed-availability policy."""
    normalized_kind = tool_kind.strip().lower()
    if maximum_cutting_width_mm is not None and maximum_cutting_width_mm <= 0:
        raise CncApiError("maximum_cutting_width_mm must be greater than zero")
    tools: list[dict[str, Any]] = []
    for item in payload.get("tools") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").lower()
        width = item.get("cutting_width_mm")
        if normalized_kind and kind != normalized_kind:
            continue
        if maximum_cutting_width_mm is not None and (
            not isinstance(width, (int, float)) or float(width) > maximum_cutting_width_mm
        ):
            continue
        catalog_match = item.get("catalog_match", True) is True
        tools.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "diameter_mm": item.get("diameter_mm"),
            "cutting_width_mm": width,
            "nose_radius_mm": item.get("nose_radius_mm"),
            "flute_length_mm": item.get("flute_length_mm"),
            "stickout_mm": item.get("stickout_mm"),
            "holder_diameter_mm": item.get("holder_diameter_mm"),
            "hand": item.get("hand"),
            "orientation_code": item.get("orientation_code"),
            "groove_profile": item.get("groove_profile"),
            "axial_contouring_supported": item.get("axial_contouring_supported", False),
            "catalog_source_document": item.get("catalog_source_document"),
            "catalog_source_page": item.get("catalog_source_page"),
            "catalog_series": item.get("catalog_series"),
            "catalog_source": "validated_catalog" if catalog_match else "engineering_template",
            "draft_simulation_allowed": True,
            "resource_availability": "assumed_available",
        })
    return {
        "schema_version": payload.get("schema_version"),
        "tool_count": len(tools),
        "filters": {
            "tool_kind": normalized_kind or None,
            "maximum_cutting_width_mm": maximum_cutting_width_mm,
        },
        "tools": tools,
        "resource_availability_policy": payload.get("resource_availability_policy") or {
            "mode": "catalog_resources_assumed_available",
            "requires_physical_inventory_binding": False,
        },
        "usage_policy": {
            "draft": (
                "These catalog definitions and engineering templates may be used for deterministic "
                "DRAFT simulation and candidate comparison."
            ),
            "resource": "Treat every returned catalog or engineering tool as available for planning.",
            "agent_rule": (
                "Select tools from tools[].id instead of guessing dimensions. Never ask for stock counts, "
                "physical measurements, tool-station binding or proof that a returned resource exists."
            ),
        },
    }


def attach_cnc_evidence_contract(
    payload: dict[str, Any],
    *,
    follow_up_tools: list[str],
) -> dict[str, Any]:
    """Tell the agent how CNC evidence may be consumed and recovered.

    Artifact filenames written by the API are backend implementation details.  They
    are deliberately not mounted in the Harness workspace and are not MCP
    resources.  Making this contract part of every evidence-bearing result keeps
    the model from wasting turns guessing local ``.dsh`` or job artifact paths.
    """
    result = dict(payload)
    result["evidence_access"] = {
        "authoritative_source": "cnc_mcp_tool_results",
        "current_response_is_decision_grade": True,
        "backend_artifacts_are_workspace_files": False,
        "backend_artifacts_are_mcp_resources": False,
        "local_file_search_required": False,
        "prohibited_recovery_methods": [
            "list_mcp_resources",
            "read_mcp_resource",
            "search_workspace_for_job_files",
            "search_dot_dsh_for_job_files",
            "read_agent_l32_json_artifacts",
        ],
        "allowed_follow_up_tools": follow_up_tools,
        "recovery_rule": (
            "If more evidence is needed, call one of allowed_follow_up_tools with the same job_id. "
            "Never search the Harness filesystem or MCP resources for CNC job JSON."
        ),
    }
    return result


mcp = FastMCP(
    "Seksun CNC",
    instructions=(
        "DeepSeek Harness 是唯一的用户入口和工艺规划所有者。"
        "CNC 服务只提供确定性几何、工艺工具、刀路、仿真、证据和安全门禁。"
        "不得把规划所有权委托给 CNC 后台规划器。收到 STEP 附件后，必须依次调用 "
        "create_job_from_step、inspect_job_progress、initialize_process_draft，"
        "随后按 add_process_operation → trial_l32_operation → accept/repair 的闭环逐道规划。"
        "When an L32 exact-section rotational profile is awaiting review, first call "
        "inspect_l32_profile_review_request. If the geometric evidence is sufficient, "
        "call provisionally_accept_l32_profile with the smallest defensible Z range and "
        "explicit evidence references. This authorization is only for reversible per-operation "
        "DRAFT trials, never production release. Do not claim dimensions beyond the extracted "
        "profile, do not use partial authorization for whole-program validation, and ask a human "
        "only when the evidence is ambiguous or the requested machining lies outside that range. "
        "这是 CNC 工艺分析工具桥。用户提供 STEP 附件时先 create_job_from_step，"
        "再用 inspect_job_progress 跟踪任务；几何完成后调用 initialize_process_draft，"
        "然后查阅 inspect_operation_catalog 并用 add_process_operation 每次只建立一道工序。"
        "每次新增 L32 工序后必须立即调用 trial_l32_operation；只有 can_accept=true 时才能调用 "
        "accept_l32_operation_trial，并提交具体理由；warning 还必须显式 acknowledge_warning。"
        "试算被阻断时优先调用 auto_repair_l32_operation，让后端依据验证证据生成并试算安全候选；"
        "系统采用目录资源默认可用策略：机床、刀具、刀柄、筒夹和装夹资源只要出现在 CNC 目录或工程候选中，"
        "即视为可用于规划和验证。不得查询现场库存、要求实测刀具、绑定刀位或询问用户是否拥有对应资源；"
        "后端要求观察模型时再提出不超过五个有差异的修正候选并调用 evaluate_l32_operation_candidates；"
        "只可用 apply_l32_operation_candidate 应用已真实试算通过的候选，随后必须重新正式试算当前工序。"
        "若无候选通过，则修改候选、删除工序或补充观察，不能继续新增工序。"
        "没有任务编号时先 open_job_context，再按需 "
        "inspect_geometry/observe_model。inspect_l32_operation_trial_state 可恢复逐道规划进度。"
        "CNC 任务证据只存在于 CNC API 和本 MCP 工具返回值中；后端产物文件名只是内部实现引用，"
        "不是 Harness 工作区文件，也不是 MCP resource。严禁为读取 CNC 证据调用 list_mcp_resources、"
        "read_mcp_resource，或在工作区、.dsh、jobs 目录中搜索 agent-l32*.json。"
        "若当前返回值不足，只能携带相同 job_id 调用 inspect_l32_operation_trial_state、"
        "inspect_job_and_geometry、observe_model 或当前结果 evidence_access.allowed_follow_up_tools 中列出的 CNC 工具。"
        "Never search local files or MCP resources for CNC job evidence. "
        "Before concluding that a cutter is unavailable, call inspect_tool_catalog. "
        "Catalog definitions and engineering templates are assumed available for planning, deterministic "
        "simulation and candidate comparison. Never block on physical inventory or tool-station binding. "
        "A successful trial only validates the current operation; it never proves that the process route is complete. "
        "Treat manufacturing coverage targets as the completion criterion, not the number of operations currently in the draft. "
        "If finalize_harness_process_plan returns plan_incomplete, route_incomplete, must_continue_planning=true, "
        "or completion_allowed=false, this is a non-terminal continuation signal. Do not summarize the task as finished. "
        "Use next_target and its required_operation_types to add, trial, repair and accept the next missing operation, "
        "then call finalize_harness_process_plan again. A blocked operation stops only that operation: repair it, replace it, "
        "or remove it and continue with another uncovered target. Ask the user only when authoritative geometry is ambiguous; "
        "never stop because physical inventory, machine accessories or catalog resources are thought to be unavailable. "
        "旧的完整草案可使用 advance_l32_operation 逐道审核，"
        "新草案全部逐道接受后调用 finalize_harness_process_plan 检查覆盖率和整件连续仿真。"
        "当前工序返回 blocked 或 plan_blocked 后必须停止推进该工序，并调用 inspect_l32_repair_options；"
        "若该工序无法修复，则删除或替换它并继续下一个未覆盖制造目标，不得直接结束整个任务。"
        "只有用户明确确认且候选通过确定性安全门时，才调用 select_l32_repair_candidate。"
        "只有整体验证时才调用 validate_l32_plan。"
        "所有输出均为 DRAFT；"
        "不得把仿真等同于生产放行，不得声称生成了已认证 NC。"
    ),
)


@mcp.tool()
def create_job_from_step(
    step_path: str,
    device_id: str = "citizen-cincom-l32",
    material: str = "待确认",
) -> dict[str, Any]:
    """导入 Harness 附件；默认只解析几何，不调用后台工艺规划器。"""
    global _active_job_id
    source = resolve_step_import(step_path)
    payload = _client().upload_step(
        source, device_id=device_id, material=material,
    )
    _active_job_id = str(payload["id"])
    return {
        "job_id": _active_job_id,
        "filename": payload.get("filename") or source.name,
        "status": payload.get("status", "processing"),
        "device_id": payload.get("device_id") or device_id,
        "source_size_bytes": source.stat().st_size,
        "planning_source": "deepseek_harness",
        "next_action": "inspect_job_progress",
        "notice": (
            "CNC 只执行确定性几何解析。解析完成后，Harness 必须调用 "
            "initialize_process_draft，并负责后续每一道工序决策。"
        ),
    }


@mcp.tool()
def inspect_job_progress(job_id: str = "", wait_seconds: float = 0) -> dict[str, Any]:
    """读取模型解析/规划进度；可等待最多 30 秒，避免智能体盲目重复调用。"""
    resolved, _ = _resolve_job_id(job_id)
    deadline = time.monotonic() + min(max(wait_seconds, 0), 30)
    api = _client()
    while True:
        job = api.json("GET", f"/api/v1/jobs/{resolved}")
        if not isinstance(job, dict):
            raise CncApiError("任务响应格式无效")
        workspace: dict[str, Any] | None = None
        try:
            value = api.json("GET", f"/api/v1/jobs/{resolved}/agent/workspace")
            workspace = value if isinstance(value, dict) else None
        except CncApiError:
            workspace = None
        if job.get("status") != "processing" or time.monotonic() >= deadline:
            return summarize_progress(job, workspace)
        time.sleep(1)


@mcp.tool()
def initialize_process_draft(job_id: str = "") -> dict[str, Any]:
    """根据确定性毛坯/设备约束建立零工序 DRAFT；不会导入后台规划器的工序。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("POST", f"/api/v1/jobs/{resolved}/agent/plan/initialize")
    if not isinstance(payload, dict):
        raise CncApiError("工艺草案初始化响应格式无效")
    result = summarize_job(payload)
    result["planning_owner"] = "deepseek_harness"
    result["next_action"] = "inspect_operation_catalog_then_add_process_operation"
    return result


@mcp.tool()
def inspect_operation_catalog(job_id: str = "") -> dict[str, Any]:
    """读取当前任务真正可执行的工序，以及每种工序可直接复制的合法几何 ID。"""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    payload = api.json("GET", "/api/v1/operation-library")
    if not isinstance(payload, dict):
        raise CncApiError("工序库响应格式无效")
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict):
        raise CncApiError("任务响应格式无效")
    rotational = None
    if job.get("device_id") == "citizen-cincom-l32":
        try:
            value = api.json("GET", f"/api/v1/jobs/{resolved}/files/rotational-features.json")
            rotational = value if isinstance(value, dict) else None
        except CncApiError:
            rotational = None
    return summarize_operation_catalog(payload, job, rotational)


@mcp.tool()
def inspect_tool_catalog(
    job_id: str = "",
    tool_kind: str = "",
    maximum_cutting_width_mm: float | None = None,
) -> dict[str, Any]:
    """Read tool capabilities that are assumed available for CNC planning and validation."""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("GET", "/api/v1/catalogs")
    if not isinstance(payload, dict):
        raise CncApiError("Invalid tool catalog response")
    result = summarize_tool_catalog(
        payload,
        tool_kind=tool_kind,
        maximum_cutting_width_mm=maximum_cutting_width_mm,
    )
    result["job_id"] = resolved
    result["next_action"] = "select_catalog_tool_then_add_or_repair_operation"
    return result


@mcp.tool()
def add_process_operation(
    setup_id: str,
    definition_id: str,
    feature_ids: list[str],
    parameters: dict[str, float | int | str | bool],
    job_id: str = "",
    tool_id: str = "",
    name: str = "",
    rationale: list[str] | None = None,
    insert_after_operation_id: str = "",
    channel_id: str = "",
    spindle_id: str = "",
    workpiece_side: str = "",
    reference_profile_id: str = "",
) -> dict[str, Any]:
    """由 Harness 向 DRAFT 增加一道候选工序；只建意图，不代表刀路或仿真通过。"""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict):
        raise CncApiError("Invalid job response")
    setups = (job.get("plan") or {}).get("setups") or []
    valid_setup_ids = [
        str(setup.get("id")) for setup in setups
        if isinstance(setup, dict) and setup.get("id")
    ]
    if setup_id not in valid_setup_ids:
        valid = ", ".join(valid_setup_ids) if valid_setup_ids else "(none; call initialize_process_draft first)"
        raise CncApiError(
            f"Unknown setup_id {setup_id!r}. Valid setup IDs for job {resolved}: {valid}. "
            "Copy an exact setups[].id returned by initialize_process_draft or inspect_job_and_geometry."
        )
    library = api.json("GET", "/api/v1/operation-library")
    if not isinstance(library, dict):
        raise CncApiError("工序库响应格式无效")
    rotational = None
    if job.get("device_id") == "citizen-cincom-l32":
        try:
            value = api.json("GET", f"/api/v1/jobs/{resolved}/files/rotational-features.json")
            rotational = value if isinstance(value, dict) else None
        except CncApiError:
            rotational = None
    catalog = summarize_operation_catalog(library, job, rotational)
    option = next((item for item in catalog["definitions"] if item["id"] == definition_id), None)
    if option is None:
        available = [item["id"] for item in catalog["definitions"]]
        raise CncApiError(
            f"工序 {definition_id!r} 对当前任务不可执行。不要用其他参数重试该工序；"
            f"请从 inspect_operation_catalog 返回的 definitions 中选择：{available}"
        )
    valid_geometry_ids = [item["id"] for item in option["valid_geometry_refs"]]
    invalid_geometry_ids = [item for item in feature_ids if item not in valid_geometry_ids]
    if invalid_geometry_ids:
        raise CncApiError(
            f"几何引用 {invalid_geometry_ids} 对工序 {definition_id} 无效。不要猜测 SOLID/BODY/FACE ID；"
            f"只能复制以下 valid_geometry_refs：{valid_geometry_ids}"
        )
    body: dict[str, Any] = {
        "definition_id": definition_id, "feature_ids": feature_ids,
        "parameters": parameters, "source": "recommendation",
        "rationale": rationale or ["Harness 按当前几何和制造状态逐道规划"],
    }
    if job.get("device_id") == "citizen-cincom-l32":
        default_context = (
            {"channel_id": "sub", "spindle_id": "sub", "workpiece_side": "back"}
            if setup_id == "SETUP-L32-SUB"
            else {"channel_id": "main", "spindle_id": "main", "workpiece_side": "front"}
        )
        channel_id = channel_id or default_context["channel_id"]
        spindle_id = spindle_id or default_context["spindle_id"]
        workpiece_side = workpiece_side or default_context["workpiece_side"]
    for key, value in {
        "tool_id": tool_id, "name": name,
        "insert_after_operation_id": insert_after_operation_id,
        "channel_id": channel_id, "spindle_id": spindle_id,
        "workpiece_side": workpiece_side,
        "reference_profile_id": reference_profile_id,
    }.items():
        if value:
            body[key] = value
    payload = api.json(
        "POST", f"/api/v1/jobs/{resolved}/setups/{setup_id}/operations", json=body,
    )
    if not isinstance(payload, dict):
        raise CncApiError("新增工序响应格式无效")
    result = summarize_job(payload)
    result["next_action"] = "trial_l32_operation"
    result["planning_rule"] = "新增后必须先试算并接受当前工序，才能继续规划下一道"
    return result


@mcp.tool()
def trial_l32_operation(operation_id: str, job_id: str = "") -> dict[str, Any]:
    """独立编译并仿真下一道 L32 工序；从已接受的累计余料继续，不重置为新毛坯。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}/trial",
    )
    if not isinstance(payload, dict):
        raise CncApiError("单道 L32 工序试算响应格式无效")
    return attach_cnc_evidence_contract(payload, follow_up_tools=[
        "accept_l32_operation_trial",
        "auto_repair_l32_operation",
        "inspect_l32_operation_trial_state",
        "observe_model",
    ])


@mcp.tool()
def accept_l32_operation_trial(
    operation_id: str,
    rationale: str,
    job_id: str = "",
    acknowledge_warning: bool = False,
) -> dict[str, Any]:
    """接受已通过确定性门禁的单道试算证据；只推进 DRAFT，不代表生产放行。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}/trial/accept",
        json={"rationale": rationale, "acknowledge_warning": acknowledge_warning},
    )
    if not isinstance(payload, dict):
        raise CncApiError("接受单道 L32 工序试算响应格式无效")
    return payload


@mcp.tool()
def inspect_l32_operation_trial_state(job_id: str = "") -> dict[str, Any]:
    """读取 Harness 已接受的工序前缀、下一道工序和最近一次门禁结果。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "GET", f"/api/v1/jobs/{resolved}/agent/l32/operation-trials/state",
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 单道工序状态响应格式无效")
    return attach_cnc_evidence_contract(payload, follow_up_tools=[
        "trial_l32_operation",
        "accept_l32_operation_trial",
        "auto_repair_l32_operation",
        "observe_model",
        "add_process_operation",
        "finalize_harness_process_plan",
    ])


@mcp.tool()
def evaluate_l32_operation_candidates(
    operation_id: str,
    candidates: list[dict[str, Any]],
    job_id: str = "",
) -> dict[str, Any]:
    """隔离试算最多五个参数、刀具或几何绑定修正候选，不修改正式 DRAFT。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST",
        f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}/candidates/evaluate",
        json={"candidates": candidates},
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 工序修正候选试算响应格式无效")
    return attach_cnc_evidence_contract(payload, follow_up_tools=[
        "apply_l32_operation_candidate",
        "inspect_l32_operation_trial_state",
        "observe_model",
        "evaluate_l32_operation_candidates",
        "remove_process_operation",
    ])


@mcp.tool()
def auto_repair_l32_operation(operation_id: str, job_id: str = "") -> dict[str, Any]:
    """Diagnose one blocked L32 operation, generate safe evidence-led repairs, and sandbox-test them."""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST",
        f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}/repairs/auto-evaluate",
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 automatic repair response is invalid")
    return attach_cnc_evidence_contract(payload, follow_up_tools=[
        "apply_l32_operation_candidate",
        "inspect_l32_operation_trial_state",
        "observe_model",
        "evaluate_l32_operation_candidates",
        "remove_process_operation",
    ])


def inspect_l32_candidate_tool_requirements(
    operation_id: str,
    candidate_id: str,
    job_id: str = "",
) -> dict[str, Any]:
    """Explain compatible inventory and the exact user-confirmed evidence needed for a candidate tool."""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "GET",
        (
            f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}"
            f"/candidates/{candidate_id}/tool-requirements"
        ),
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 candidate tool requirement response is invalid")
    return payload


def bind_l32_candidate_inventory_tool(
    operation_id: str,
    candidate_id: str,
    inventory_id: str,
    rationale: str,
    job_id: str = "",
) -> dict[str, Any]:
    """Bind measured, capability-verified machine inventory to a repair candidate and rerun its sandbox trial."""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST",
        (
            f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}"
            f"/candidates/{candidate_id}/bind-tool"
        ),
        json={"inventory_id": inventory_id, "rationale": rationale},
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 inventory binding response is invalid")
    return payload


@mcp.tool()
def apply_l32_operation_candidate(
    operation_id: str,
    candidate_id: str,
    rationale: str,
    job_id: str = "",
    acknowledge_warning: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """应用已通过隔离试算的候选；必须明确确认，应用后仍需重新正式试算当前工序。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST",
        f"/api/v1/jobs/{resolved}/agent/l32/operations/{operation_id}/candidates/apply",
        json={
            "candidate_id": candidate_id, "rationale": rationale,
            "acknowledge_warning": acknowledge_warning, "confirmed": confirmed,
        },
    )
    if not isinstance(payload, dict):
        raise CncApiError("应用 L32 工序修正候选响应格式无效")
    return payload


@mcp.tool()
def finalize_harness_process_plan(job_id: str = "") -> dict[str, Any]:
    """在全部工序逐道接受后检查制造覆盖率，并执行整件 L32 连续材料仿真。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/agent/l32/plan/finalize",
    )
    if not isinstance(payload, dict):
        raise CncApiError("Harness L32 工艺方案收口响应格式无效")
    if payload.get("status") == "plan_incomplete" or payload.get("completion_allowed") is False:
        payload = {
            **payload,
            "agent_directive": {
                "task_may_end": False,
                "mandatory_action": "continue_planning_from_next_target",
                "prohibited_action": "final_answer_claiming_route_completion",
                "success_gate": "status_is_validated_draft_or_validated_draft_with_review",
            },
        }
    return attach_cnc_evidence_contract(
        payload,
        follow_up_tools=[
            "inspect_operation_catalog",
            "add_process_operation",
            "trial_l32_operation",
            "auto_repair_l32_operation",
            "remove_process_operation",
            "inspect_job_and_geometry",
        ],
    )


@mcp.tool()
def revise_process_operation(
    setup_id: str,
    operation_id: str,
    changes: dict[str, Any],
    job_id: str = "",
) -> dict[str, Any]:
    """修改一道 Harness DRAFT 工序的刀具、参数、几何或启用状态。"""
    resolved, _ = _resolve_job_id(job_id)
    allowed = {
        "name", "feature_ids", "reference_profile_id", "tool_id", "parameters", "enabled",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise CncApiError(f"不支持修改字段：{', '.join(sorted(unknown))}")
    payload = _client().json(
        "PATCH",
        f"/api/v1/jobs/{resolved}/setups/{setup_id}/operations/{operation_id}",
        json=changes,
    )
    if not isinstance(payload, dict):
        raise CncApiError("修改工序响应格式无效")
    return summarize_job(payload)


@mcp.tool()
def remove_process_operation(
    setup_id: str, operation_id: str, job_id: str = "",
) -> dict[str, Any]:
    """从 Harness DRAFT 删除一道尚未放行的工序。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "DELETE", f"/api/v1/jobs/{resolved}/setups/{setup_id}/operations/{operation_id}",
    )
    if not isinstance(payload, dict):
        raise CncApiError("删除工序响应格式无效")
    return summarize_job(payload)


@mcp.tool()
def reorder_process_operations(
    setup_id: str, operation_ids: list[str], job_id: str = "",
) -> dict[str, Any]:
    """重排一个装夹内的完整工序序列。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/setups/{setup_id}/operations/reorder",
        json={"operation_ids": operation_ids},
    )
    if not isinstance(payload, dict):
        raise CncApiError("重排工序响应格式无效")
    return summarize_job(payload)


@mcp.tool()
def open_job_context(job_id: str = "") -> dict[str, Any]:
    """绑定 CNC 任务上下文；省略 job_id 时绑定最近完成的本地任务。"""
    resolved, source = _resolve_job_id(job_id)
    payload = _client().json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(payload, dict):
        raise CncApiError("任务响应格式无效")
    result = summarize_job(payload)
    result["context_binding"] = {"source": source, "active_job_id": resolved}
    return result


@mcp.tool()
def inspect_job(job_id: str = "") -> dict[str, Any]:
    """读取一个 CNC 任务的紧凑工艺上下文、覆盖状态和工序清单。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(payload, dict):
        raise CncApiError("任务响应格式无效")
    return summarize_job(payload)


@mcp.tool()
def inspect_geometry(job_id: str = "") -> dict[str, Any]:
    """按需读取紧凑几何、制造特征和回转轮廓证据，不返回大型网格。"""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict):
        raise CncApiError("任务响应格式无效")
    rotational: dict[str, Any] | None = None
    if job.get("device_id") == "citizen-cincom-l32":
        try:
            value = api.json("GET", f"/api/v1/jobs/{resolved}/files/rotational-features.json")
            rotational = value if isinstance(value, dict) else None
        except CncApiError:
            rotational = None
    return summarize_geometry(job, rotational)


@mcp.tool()
def inspect_l32_profile_review_request(job_id: str = "") -> dict[str, Any]:
    """Read compact profile-review evidence before deciding whether bounded AI DRAFT use is safe."""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("GET", f"/api/v1/jobs/{resolved}/agent/l32/review")
    if not isinstance(payload, dict):
        raise CncApiError("L32 profile review response is invalid")
    profiles = []
    for raw in payload.get("profiles") or []:
        if not isinstance(raw, dict):
            continue
        profiles.append({
            "id": raw.get("id"), "side": raw.get("side"), "method": raw.get("method"),
            "confidence": raw.get("confidence"), "review_state": raw.get("review_state"),
            "review_reasons": raw.get("review_reasons", []),
            "point_count": raw.get("point_count"),
            "z_min_mm": raw.get("z_min_mm"), "z_max_mm": raw.get("z_max_mm"),
            "diameter_min_mm": raw.get("diameter_min_mm"),
            "diameter_max_mm": raw.get("diameter_max_mm"),
            "undercut_spans": raw.get("undercut_spans", []),
            "provisional_decision": raw.get("provisional_decision"),
        })
    return {
        "schema_version": payload.get("schema_version"), "job_id": resolved,
        "status": payload.get("status"),
        "recommended_profile_id": payload.get("recommended_profile_id"),
        "blocker": payload.get("blocker"), "profiles": profiles,
        "policy": {
            "ai_provisional_requires": [
                "exact_section profile", "bounded Z scope", "explicit evidence references",
                "DRAFT-only use",
            ],
            "partial_scope_cannot_validate_whole_program": True,
            "production_ready": False,
        },
        "next_action": payload.get("next_action"),
    }


@mcp.tool()
def provisionally_accept_l32_profile(
    profile_id: str,
    z_min_mm: float,
    z_max_mm: float,
    confidence: float,
    rationale: str,
    evidence_refs: list[str],
    job_id: str = "",
    scope: str = "partial",
) -> dict[str, Any]:
    """Authorize an exact bounded profile for reversible AI-led DRAFT trials, never production release."""
    if scope not in {"full", "partial"}:
        raise CncApiError("scope must be full or partial")
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/agent/l32/profile-provisional-decision",
        json={
            "profile_id": profile_id, "scope": scope,
            "z_min_mm": z_min_mm, "z_max_mm": z_max_mm,
            "confidence": confidence, "rationale": rationale,
            "evidence_refs": evidence_refs,
        },
    )
    if not isinstance(payload, dict):
        raise CncApiError("AI provisional profile decision response is invalid")
    return {
        "job_id": resolved, "decision_status": payload.get("decision_status"),
        "profile_id": payload.get("authorized_profile_id"),
        "authorized_scope": payload.get("authorized_scope"),
        "status": payload.get("status"), "next_action": payload.get("next_action"),
        "release_status": "DRAFT", "production_ready": False,
    }


@mcp.tool()
def inspect_machine(job_id: str = "") -> dict[str, Any]:
    """Read the bound machine configuration; catalog resources are available by default."""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict):
        raise CncApiError("任务响应格式无效")
    if job.get("machine_instance_id"):
        snapshot = api.json("GET", f"/api/v1/jobs/{resolved}/machine-instance")
        configuration_source = "job_configuration"
    else:
        snapshot = api.json("GET", "/api/v1/machines/l32/definitions")
        configuration_source = "default_catalog_template"
    return {
        "job_id": resolved,
        "configuration_hash": job.get("machine_configuration_hash"),
        "machine": snapshot,
        "configuration_source": configuration_source,
        "resource_availability": "catalog_resources_assumed_available",
        "physical_inventory_binding_required": False,
        "release_status": "DRAFT",
    }


def inspect_l32_tool_inventory(job_id: str = "") -> dict[str, Any]:
    """List measured physical tools for the job's bound L32 machine and their verification evidence."""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict) or not job.get("machine_instance_id"):
        raise CncApiError("The job is not bound to a verifiable machine instance")
    payload = api.json(
        "GET", f"/api/v1/machines/l32/instances/{job['machine_instance_id']}/tools",
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 physical tool inventory response is invalid")
    result = summarize_tool_inventory(payload)
    result["job_id"] = resolved
    return result


def record_l32_physical_tool(
    inventory: dict[str, Any],
    job_id: str = "",
) -> dict[str, Any]:
    """Record a measured physical tool; every value must come from user-confirmed inspection evidence."""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    job = api.json("GET", f"/api/v1/jobs/{resolved}")
    if not isinstance(job, dict) or not job.get("machine_instance_id"):
        raise CncApiError("The job is not bound to a verifiable machine instance")
    required_identity = str(inventory.get("inventory_id") or "").strip()
    if not required_identity:
        raise CncApiError("inventory_id is required")
    if inventory.get("axial_contouring_supported") and not (
        str(inventory.get("capability_verified_by") or "").strip()
        and str(inventory.get("capability_verification_reference") or "").strip()
    ):
        raise CncApiError(
            "Axial contour capability requires a named verifier and inspection evidence reference"
        )
    payload = api.json(
        "POST",
        f"/api/v1/machines/l32/instances/{job['machine_instance_id']}/tools",
        json=inventory,
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 physical tool creation response is invalid")
    return {
        "job_id": resolved,
        "machine_instance_id": job["machine_instance_id"],
        "tool": summarize_tool_inventory({"tools": [payload]})["tools"][0],
        "next_action": "bind_l32_candidate_inventory_tool",
        "release_status": "DRAFT",
        "production_ready": False,
    }


@mcp.tool()
def observe_model(job_id: str = "", refresh: bool = False) -> Image:
    """返回由真实 STL 确定性渲染的四视图图片；refresh=true 时强制重新渲染。"""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    if refresh:
        api.json("POST", f"/api/v1/jobs/{resolved}/agent/model-view")
    try:
        response = api.request("GET", f"/api/v1/jobs/{resolved}/files/agent-perception-contact-sheet.png")
    except CncApiError:
        api.json("POST", f"/api/v1/jobs/{resolved}/agent/model-view")
        response = api.request("GET", f"/api/v1/jobs/{resolved}/files/agent-perception-contact-sheet.png")
    return Image(data=response.content, format="png")


@mcp.tool()
def inspect_validation(job_id: str = "") -> dict[str, Any]:
    """读取已有 L32 逐工序仿真、审核、阻断和修正证据，不重新计算。"""
    resolved, _ = _resolve_job_id(job_id)
    api = _client()
    review = api.json("GET", f"/api/v1/jobs/{resolved}/agent/l32/review")
    return review if isinstance(review, dict) else {"job_id": resolved, "status": "unavailable"}


@mcp.tool()
def validate_l32_plan(job_id: str = "") -> dict[str, Any]:
    """真实编译并逐工序仿真当前 L32 草案；失败会返回阻断证据而非伪造成功。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("POST", f"/api/v1/jobs/{resolved}/agent/l32/validate")
    if not isinstance(payload, dict):
        raise CncApiError("L32 验证响应格式无效")
    return payload


@mcp.tool()
def inspect_l32_operation_loop(job_id: str = "") -> dict[str, Any]:
    """读取当前 L32 滚动工序闭环；不会触发编译或仿真。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("GET", f"/api/v1/jobs/{resolved}/agent/l32/loop")
    if not isinstance(payload, dict):
        raise CncApiError("L32 滚动闭环响应格式无效")
    return payload


@mcp.tool()
def advance_l32_operation(job_id: str = "", reset: bool = False) -> dict[str, Any]:
    """真实编译/仿真并只审核推进一道 L32 工序；失败或证据缺失时停止。"""
    resolved, _ = _resolve_job_id(job_id)
    suffix = "?reset=true" if reset else ""
    payload = _client().json(
        "POST", f"/api/v1/jobs/{resolved}/agent/l32/loop/advance{suffix}",
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 单工序推进响应格式无效")
    return payload


@mcp.tool()
def inspect_l32_repair_options(job_id: str = "") -> dict[str, Any]:
    """读取最新 L32 阻断诊断和安全工艺候选；不会修改工艺方案。"""
    resolved, _ = _resolve_job_id(job_id)
    payload = _client().json("GET", f"/api/v1/jobs/{resolved}/agent/l32/repair-options")
    if not isinstance(payload, dict):
        raise CncApiError("L32 修正候选响应格式无效")
    return payload


@mcp.tool()
def select_l32_repair_candidate(
    candidate_id: str, job_id: str = "", confirmed: bool = False,
) -> dict[str, Any]:
    """选择 L32 修正候选；只有通过确定性门禁的候选才会修改 DRAFT 工艺方案。"""
    resolved, _ = _resolve_job_id(job_id)
    suffix = "?confirmed=true" if confirmed else ""
    payload = _client().json(
        "POST",
        f"/api/v1/jobs/{resolved}/agent/l32/repair-options/{candidate_id}/select{suffix}",
    )
    if not isinstance(payload, dict):
        raise CncApiError("L32 修正候选选择响应格式无效")
    return payload


if __name__ == "__main__":
    mcp.run(transport="stdio")
