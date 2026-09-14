from __future__ import annotations

from collections import OrderedDict

from .manufacturing_knowledge import load_manufacturing_library
from .models import GeometryAnalysis, ManufacturingRouteProposal, ManufacturingRouteStep, ProcessPlan


PHASE_BY_CODE = {
    "GX-C-07": "roughing",
    "GX-C-09": "roughing",
    "GX-C-11": "roughing",
    "GX-C-16": "roughing",
    "GX-C-42": "semi_finishing",
    "GX-C-08": "finishing",
    "GX-C-13": "finishing",
    "GX-C-17": "finishing",
    "GX-C-41": "finishing",
}


def _part_family(analysis: GeometryAnalysis, plan: ProcessPlan) -> str:
    if plan.process_kind == "sheet_forming":
        return "sheet_forming"
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)
    sizes = sorted((bounds.size.x, bounds.size.y, bounds.size.z))
    non_hole_cylinders = [
        item for item in analysis.cylindrical_features if item.kind in {"boss", "cylinder"}
    ]
    if non_hole_cylinders and sizes[-1] >= max(sizes[1] * 2.5, 20):
        return "mixed" if analysis.prismatic_features or analysis.internal_profile_features else "rotational"
    operation_types = {operation.type for setup in plan.setups for operation in setup.operations}
    if operation_types & {"surface_roughing", "surface_3d", "waterline"}:
        return "freeform"
    axes = {
        (
            round((feature.access_direction or feature.axis).x),
            round((feature.access_direction or feature.axis).y),
            round((feature.access_direction or feature.axis).z),
        )
        for feature in analysis.cylindrical_features
        if feature.kind == "hole" and feature.review_state != "excluded"
    }
    return "mixed" if len(axes) >= 3 else "prismatic"


def build_manufacturing_route(
    analysis: GeometryAnalysis, plan: ProcessPlan,
) -> ManufacturingRouteProposal:
    library = load_manufacturing_library()
    by_code = {item["code"]: item for item in library["processes"]}
    operation_mapping: dict[str, str] = library["cam_operation_mapping"]
    operations = [operation for setup in plan.setups for operation in setup.operations if operation.enabled]
    requirements = plan.manufacturing_requirements
    recognized_requirements = requirements.requirements if requirements else []
    matched_requirements = [
        item for item in recognized_requirements if item.mapping_status == "matched"
    ]
    verified_requirements = [
        item for item in matched_requirements
        if item.verification_status == "verified_geometry" and item.confidence >= 0.75
    ]
    grouped: OrderedDict[str, list] = OrderedDict()
    unmapped_operation_types: set[str] = set()
    for operation in operations:
        code = operation_mapping.get(operation.type)
        if code:
            grouped.setdefault(code, []).append(operation)
        else:
            unmapped_operation_types.add(operation.type)

    family = _part_family(analysis, plan)
    missing = []
    if not matched_requirements:
        missing.append("二维图纸/PMI中的尺寸公差、形位公差和基准")
    if not any(item.type == "surface_roughness" for item in matched_requirements):
        missing.append("表面粗糙度要求")
    if not any(item.type in {"heat_treatment", "coating", "surface_treatment"} for item in recognized_requirements):
        missing.append("热处理、表面处理及特殊特性")
    missing.extend([
        "毛坯形式与供货状态",
        "批量、实际设备、刀具、夹具、外协和成本约束",
    ])
    if requirements and requirements.unresolved_requirement_ids:
        missing.append(f"{len(requirements.unresolved_requirement_ids)} 项图纸要求仍未唯一绑定到三维特征")
    capability_gaps: list[str] = []
    step_specs: list[dict] = []

    def add(
        code: str, phase: str, selection: str, reason: str, *,
        source_operations: list | None = None,
        blocking: list[str] | None = None,
    ) -> None:
        existing = next((item for item in step_specs if item["code"] == code), None)
        if existing:
            if selection == "required" and existing["selection"] == "conditional":
                existing["selection"] = "required"
                existing["reason"] = reason
                existing["blocking"] = blocking or []
            return
        if code not in by_code:
            return
        step_specs.append({
            "code": code,
            "phase": phase,
            "selection": selection,
            "reason": reason,
            "source_operations": source_operations or [],
            "blocking": blocking or [],
        })

    add(
        "GX-Q-11", "incoming", "conditional",
        "材料牌号或炉批证明不足时进行化学成分复验。",
        blocking=["材料牌号、炉批和材质证明"],
    )

    if family == "sheet_forming":
        add(
            "GX-S-06", "blank", "conditional",
            "作为薄板展开后的下料候选；必须先完成可靠展开和热影响评估。",
            blocking=["展开尺寸、切割质量等级和热影响限制"],
        )
        capability_gaps.append("工序库尚缺冲压、折弯、拉深、整形、模具试模等薄板成形专用工序定义")
        capability_gaps.extend(f"当前执行层未映射工序：{item}" for item in sorted(unmapped_operation_types))
    else:
        add(
            "GX-C-31", "blank", "required",
            "建立可追溯毛坯并为装夹与粗加工保留余量。",
            blocking=["毛坯类型、尺寸、锻造/铸造状态与单边余量"],
        )
        if family == "rotational":
            add("GX-C-01", "roughing", "required", "主外形为长径比明显的回转体，优先采用粗车建立回转基准并去除余量。")
            add(
                "GX-C-02", "semi_finishing", "conditional",
                "精度、热处理或磨削余量需要分阶段稳定时增加半精车。",
                blocking=["轴颈公差、热处理路线、磨削余量和变形控制要求"],
            )
            add("GX-C-03", "finishing", "required", "完成回转表面和端面的最终切削尺寸。")
            for code in ("GX-C-11", "GX-C-13", "GX-C-41"):
                if code in grouped:
                    add(
                        code, PHASE_BY_CODE.get(code, "finishing"), "required",
                        "由回转体上的已识别孔或边缘特征得到。",
                        source_operations=grouped[code],
                    )
            capability_gaps.append("当前 CAM 执行层尚未接入数控车削；回转体路线只能规划，不能自动生成车削刀路")
        else:
            if family == "mixed":
                add(
                    "GX-C-04", "roughing", "conditional",
                    "同时存在回转与非回转特征时，车铣复合可减少重复装夹和基准转换。",
                    blocking=["主回转轴、车铣复合设备能力、动力刀具与副主轴配置"],
                )
            for code, source_operations in grouped.items():
                add(
                    code,
                    PHASE_BY_CODE.get(code, "finishing"),
                    "required",
                    "由已识别几何特征和确定性 CAM 工序映射得到。",
                    source_operations=source_operations,
                )

        selected_codes = {item["code"] for item in step_specs}
        if "GX-C-07" in selected_codes and "GX-C-08" in selected_codes:
            rough_index = next(index for index, item in enumerate(step_specs) if item["code"] == "GX-C-07")
            finish_index = next(index for index, item in enumerate(step_specs) if item["code"] == "GX-C-08")
            if finish_index == rough_index + 1:
                step_specs.insert(finish_index, {
                    "code": "GX-C-42", "phase": "semi_finishing", "selection": "conditional",
                    "reason": "高精度、薄壁或粗加工变形风险存在时，用半精铣稳定余量和基准。",
                    "source_operations": [],
                    "blocking": ["最终公差、粗糙度、壁厚和粗加工后变形"],
                })
        diameter_requirements = [
            item for item in matched_requirements if item.type == "diameter"
        ]
        tight_hole_requirements = [
            item for item in diameter_requirements
            if item.tolerance_upper is not None and item.tolerance_lower is not None
            and item.tolerance_upper - item.tolerance_lower <= 0.05
        ]
        verified_tight_holes = [item for item in tight_hole_requirements if item in verified_requirements]
        if "GX-C-11" in grouped:
            add(
                "GX-C-13", "finishing", "conditional",
                "孔公差或表面质量超过钻削稳定能力时增加铰孔。",
                blocking=["孔径公差、圆度、位置度和粗糙度"],
            )
        if tight_hole_requirements:
            add(
                "GX-C-13", "finishing",
                "required" if verified_tight_holes else "conditional",
                "图纸孔径公差带不大于 0.05 mm，需要铰孔或等效精孔工艺保证尺寸。",
                blocking=[] if verified_tight_holes else ["孔标注与三维孔特征仍需确认唯一对应"],
            )
        position_requirements = [
            item for item in matched_requirements
            if item.type == "gdt_feature_control_frame" and item.subtype in {"position", "true_position"}
        ]
        if position_requirements:
            add(
                "GX-C-18", "finishing", "conditional",
                "图纸包含位置度要求，精孔与基准加工应尽量保持统一坐标基准。",
                blocking=["完整基准引用、最大实体要求和位置度公差带定义"],
            )
        if family == "freeform" and len(plan.setups) >= 3:
            add(
                "GX-C-10", "finishing", "conditional",
                "复杂曲面需要多方向装夹时，将多轴联动作为减少接刀痕和不可达区的候选。",
                blocking=["曲面公差、刀轴可达性、机床运动学和后处理器"],
            )
        blind_internal_profiles = [
            feature for feature in analysis.internal_profile_features
            if feature.machining_kind == "blind_pocket" and feature.review_state != "excluded"
        ]
        if blind_internal_profiles:
            add(
                "GX-S-01", "special", "conditional",
                "盲型腔存在铣刀无法形成的窄深区域或尖锐内角时，使用成形电火花作为补充候选。",
                blocking=["最小内圆角、深宽比、材料导电性、热影响和电极设计"],
            )

        bounds = analysis.measurements["bounding_box"]
        assert not isinstance(bounds, float)
        sizes = sorted((bounds.size.x, bounds.size.y, bounds.size.z))
        steel_like = any(token in plan.material.casefold() for token in ("steel", "cr", "45", "钢"))
        if steel_like and sizes[0] <= sizes[-1] * 0.15:
            add(
                "GX-H-25", "stabilization", "conditional",
                "钢制薄壁或大去除量零件存在残余应力变形风险时，在精加工前去应力。",
                blocking=["材料状态、去除率、壁厚、热处理变形与后续余量"],
            )
        if unmapped_operation_types:
            capability_gaps.extend(f"当前执行层未映射工序：{item}" for item in sorted(unmapped_operation_types))

    add("GX-C-41", "finishing", "required", "所有切削或下料边缘完成倒角、去毛刺和锐边处理。")
    if "6061" in plan.material or "7075" in plan.material or "铝" in plan.material:
        add(
            "GX-H-19", "surface_treatment", "conditional",
            "铝合金零件按图纸防护、外观和耐蚀要求选择阳极氧化。",
            blocking=["膜层类型、厚度、颜色、遮蔽区域和尺寸补偿"],
        )
    add("GX-H-29", "surface_treatment", "required", "终检前清洁加工介质和残屑，并按交付要求防护。")
    add("GX-Q-01", "inspection", "required", "首件验证工艺、装夹、程序和关键特征后方可批量生产。")
    gdt_requirements = [item for item in matched_requirements if item.type == "gdt_feature_control_frame"]
    add(
        "GX-Q-02", "inspection", "required" if gdt_requirements else "conditional",
        "图纸包含形位公差，需按基准体系使用 CMM 或等效方法检验。" if gdt_requirements
        else "复杂轮廓、基准关联或形位公差需要时采用 CMM。",
        blocking=[] if gdt_requirements else ["基准体系、被测特征、公差和测量不确定度要求"],
    )
    roughness_requirements = [item for item in matched_requirements if item.type == "surface_roughness"]
    add(
        "GX-Q-03", "inspection", "required" if roughness_requirements else "conditional",
        "图纸已识别表面粗糙度要求，必须配置粗糙度检测。" if roughness_requirements
        else "图纸规定表面粗糙度时配置对应测量方向、截止波长和评价参数。",
        blocking=[] if roughness_requirements else ["粗糙度参数、限值、测量方向和取样规则"],
    )
    add("GX-Q-14", "release", "required", "汇总尺寸、外观、处理和追溯记录，完成终检与批次放行。")

    phase_order = {
        "incoming": 0, "blank": 1, "roughing": 2, "stabilization": 3,
        "semi_finishing": 4, "special": 5, "finishing": 6,
        "surface_treatment": 7, "inspection": 8, "release": 9,
    }
    step_specs.sort(key=lambda item: phase_order[item["phase"]])
    steps: list[ManufacturingRouteStep] = []
    previous_required_code: str | None = None
    for index, spec in enumerate(step_specs, start=1):
        process = by_code[spec["code"]]
        source_operations = spec["source_operations"]
        mapped_cam_ids = sorted({operation.type for operation in source_operations})
        if mapped_cam_ids:
            execution_mode = "cam"
        elif process["family"] == "quality":
            execution_mode = "inspection"
        elif process["family"] in {"heat_surface", "special"}:
            execution_mode = "external"
        else:
            execution_mode = "manual"
        prerequisites = [previous_required_code] if previous_required_code else []
        steps.append(ManufacturingRouteStep(
            sequence=index * 10,
            process_code=process["code"],
            name=process["name"],
            phase=spec["phase"],
            selection=spec["selection"],
            execution_mode=execution_mode,
            execution_state=process["execution_state"],
            cam_operation_ids=mapped_cam_ids,
            source_operation_ids=[operation.id for operation in source_operations],
            source_feature_ids=sorted({feature_id for operation in source_operations for feature_id in operation.feature_ids}),
            prerequisite_codes=prerequisites,
            reason=spec["reason"],
            confidence=0.82 if spec["selection"] == "required" else 0.55,
            blocking_missing_information=spec["blocking"],
        ))
        if spec["selection"] == "required":
            previous_required_code = process["code"]

    if not grouped and family != "sheet_forming":
        capability_gaps.append("未从当前几何规划中获得任何可映射的切削工序")
    route_incomplete = bool(capability_gaps) or bool(
        requirements and requirements.status == "incomplete"
    )
    return ManufacturingRouteProposal(
        catalog_version=library["catalog_version"],
        status="incomplete" if route_incomplete else "review",
        part_family=family,
        planning_basis=[
            "OCCT/STEP 几何与制造特征识别",
            "确定性 CAM 工序及其制造工序编码映射",
            f"制造工序库 {library['catalog_version']} 的适用条件、衔接、质量和标准知识",
            *([f"已导入 {len(recognized_requirements)} 项二维图纸/PMI结构化要求"] if requirements else []),
        ],
        steps=steps,
        capability_gaps=capability_gaps,
        missing_information=missing,
        alternative_process_codes=["GX-S-08"] if family == "sheet_forming" else ["GX-S-01", "GX-S-04", "GX-C-10"],
    )
