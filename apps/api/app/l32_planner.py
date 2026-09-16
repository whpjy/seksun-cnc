from __future__ import annotations

from math import pi

from .catalogs import get_tool, resolve_machine, resolve_material
from .coverage import evaluate_plan_coverage
from .manufacturing_knowledge import assess_plan_knowledge
from .models import GeometryAnalysis, ManufacturingRequirements, ProcessPlan, Setup
from .operation_library import create_operation_instance
from .rotational_features import bind_thread_requirements, infer_rotational_features
from .route_planner import build_manufacturing_route


def _turning_parameters(feed_mm_rev: float, cutting_speed_m_min: float) -> dict[str, float | str]:
    return {
        "spindle_mode": "constant_surface_speed",
        "cutting_speed_m_min": round(cutting_speed_m_min, 1),
        "maximum_spindle_rpm": 8000,
        "feed_per_revolution_mm": feed_mm_rev,
    }


def build_l32_process_plan(
    analysis: GeometryAnalysis,
    material: str,
    machine: str,
    requirements: ManufacturingRequirements | None = None,
) -> ProcessPlan:
    material_profile = resolve_material(material)
    machine_profile = resolve_machine(machine)
    rotational = infer_rotational_features(analysis)
    rotational = bind_thread_requirements(rotational, requirements)
    outer_profiles = [item for item in rotational.profiles if item.side == "outer"]
    accepted_inner_profiles = [
        item for item in rotational.profiles
        if item.side == "inner"
        and item.extraction_method == "exact_section"
        and item.review_state == "accepted"
    ]
    profile = max(
        outer_profiles,
        key=lambda item: (max(point.radius for point in item.points), item.confidence),
        default=None,
    )
    blocking_reasons: list[str] = []
    setups: list[Setup] = []
    stock: dict[str, object] = {
        "type": "round_bar",
        "diameter_mm": 0,
        "length_mm": 0,
        "allowance_mm": {"radial": 1.0, "axial": 2.0},
    }

    if profile is None:
        if rotational.status == "solid_selection_required":
            blocking_reasons.append("输入包含多个实体；确认实际加工对象前禁止生成 L32 车削工艺。")
        elif rotational.status == "not_rotational":
            blocking_reasons.append("零件未通过主体回转性门禁，禁止生成 L32 车削工艺。")
        else:
            blocking_reasons.append("未识别到可审查的外回转轮廓，无法生成 L32 车削工艺。")
    else:
        z_values = [point.z for point in profile.points]
        radii = [point.radius for point in profile.points]
        maximum_radius = max(radii)
        profile_length = max(z_values) - min(z_values)
        radial_allowance = max(1.0, round(maximum_radius * 0.05, 2))
        stock_radius = maximum_radius + radial_allowance
        stock_length = profile_length + 4.0
        stock = {
            "type": "round_bar",
            "diameter_mm": round(stock_radius * 2, 3),
            "length_mm": round(stock_length, 3),
            "allowance_mm": {"radial": radial_allowance, "axial": 2.0},
            "rotational_profile_id": profile.id,
            "profile_review_state": profile.review_state,
            "profile_extraction_method": profile.extraction_method,
        }
        if stock_radius > 19 + 1e-9:
            blocking_reasons.append(
                f"候选棒料直径 Ø{stock_radius * 2:.2f} 超过 L32 已知的 Ø38 选件能力。"
            )
        elif stock_radius > 16 + 1e-9:
            stock["required_option"] = "bar_diameter_38mm"
        if stock_length > 320 + 1e-9:
            blocking_reasons.append(
                f"候选棒料长度 {stock_length:.2f} mm 超过 L32 一次装夹 320 mm 资料上限。"
            )

        cutting_speed = min(material_profile.milling_speed_m_min, 120.0)
        common_rough = _turning_parameters(0.12, cutting_speed)
        common_finish = _turning_parameters(0.06, cutting_speed * 0.85)
        matched_threads = [
            item for item in rotational.features
            if item.kind == "thread_form_candidate"
            and item.binding_state == "matched"
            and item.resolved_pitch_mm is not None
            and item.resolved_major_diameter_mm is not None
            and item.thread_side in {"external", "unknown"}
        ]
        inner_profile = max(
            accepted_inner_profiles,
            key=lambda item: max(point.z for point in item.points) - min(point.z for point in item.points),
            default=None,
        )
        inner_operations = []
        if inner_profile is not None:
            inner_radii = [point.radius for point in inner_profile.points]
            inner_depth = max(point.z for point in inner_profile.points) - min(point.z for point in inner_profile.points)
            minimum_inner_radius = min(inner_radii)
            required_entry_diameter = get_tool("TURN-ID-R").holder_diameter_mm + 0.4
            maximum_prebore_diameter = max(minimum_inner_radius * 2 - 0.5, 0)
            drill_candidates = [
                get_tool(f"DRILL-{diameter:.1f}")
                for diameter in (12.0, 11.0, 10.5, 10.0, 8.0, 7.0, 6.5, 6.0, 5.0, 4.0, 3.0, 2.0)
                if required_entry_diameter <= diameter <= maximum_prebore_diameter
            ]
            prebore_tool = drill_candidates[0] if drill_candidates else None
            prebore_operation = []
            if prebore_tool is not None:
                drilling_rpm = min(
                    round(material_profile.drilling_speed_m_min * 1000 / (pi * prebore_tool.diameter_mm)),
                    prebore_tool.max_rpm,
                    machine_profile.max_spindle_rpm,
                )
                prebore_operation = [create_operation_instance(
                    id="OP22", sequence=22, type="axial_drilling", name="内孔镗削预孔草案",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False, feature_ids=[inner_profile.id], tool=prebore_tool,
                    parameters={
                        "spindle_mode": "constant_rpm",
                        "cutting_speed_m_min": material_profile.drilling_speed_m_min,
                        "maximum_spindle_rpm": drilling_rpm,
                        "feed_per_revolution_mm": material_profile.drill_feed_per_rev_mm,
                        "start_z_mm": max(point.z for point in inner_profile.points),
                        "depth_mm": inner_depth,
                        "peck_depth_mm": min(2.0, prebore_tool.diameter_mm * 0.5),
                        "profile_depth_mm": inner_depth,
                        "required_initial_bore_diameter_mm": required_entry_diameter,
                        "maximum_prebore_diameter_mm": maximum_prebore_diameter,
                    },
                    rationale=[
                        "预孔直径由粗镗杆入口包络与最小成品孔径共同约束",
                        "实际钻头、伸出、钻尖越程和排屑策略完成审核前保持禁用",
                    ],
                    confidence=min(inner_profile.confidence, 0.7), status="warning",
                )]
            inner_operations = [
                *prebore_operation,
                create_operation_instance(
                    id="OP25", sequence=25, type="turn_id_roughing", name="内孔轮廓粗镗草案",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False, feature_ids=[inner_profile.id], tool=get_tool("TURN-ID-R"),
                    parameters={
                        **_turning_parameters(0.08, cutting_speed * 0.65),
                        "radial_allowance_mm": 0.25,
                        "depth_of_cut_mm": 0.5,
                        "required_initial_bore_diameter_mm": required_entry_diameter,
                        "profile_depth_mm": inner_depth,
                    },
                    rationale=[
                        "内轮廓来自已人工确认的 OCCT 精确截面",
                        "初始孔、镗杆入口和隐藏倒扣完成审核前保持禁用",
                    ],
                    confidence=min(inner_profile.confidence, 0.7), status="warning",
                ),
                create_operation_instance(
                    id="OP28", sequence=28, type="turn_id_finishing", name="内孔轮廓精镗草案",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False, feature_ids=[inner_profile.id], tool=get_tool("TURN-ID-F"),
                    parameters={
                        **_turning_parameters(0.04, cutting_speed * 0.55),
                        "radial_allowance_mm": 0.0,
                        "required_initial_bore_diameter_mm": get_tool("TURN-ID-F").holder_diameter_mm + 0.4,
                        "minimum_finished_bore_diameter_mm": minimum_inner_radius * 2,
                        "profile_depth_mm": inner_depth,
                    },
                    rationale=[
                        "精镗草案沿已确认的内孔 Z-R 轮廓生成",
                        "刀杆伸出、入口孔和内轮廓可达性通过前保持禁用",
                    ],
                    confidence=min(inner_profile.confidence, 0.7), status="warning",
                ),
            ]
        operations = [] if blocking_reasons else [
            create_operation_instance(
                id="OP10", sequence=10, type="turn_facing", name="棒料端面建立 Z 基准",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool("TURN-OD-R"),
                parameters={
                    **common_rough,
                    "stock_allowance_mm": 0.0,
                    "depth_of_cut_mm": 0.5,
                    "face_z_mm": max(z_values),
                    "center_overtravel_mm": 0.2,
                },
                rationale=["在主轴侧建立回转加工轴向基准", "端面位置来自候选回转轮廓前端"],
                confidence=profile.confidence, status="warning",
            ),
            create_operation_instance(
                id="OP20", sequence=20, type="turn_od_roughing", name="外圆轮廓分层粗车",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool("TURN-OD-R"),
                parameters={
                    **common_rough,
                    "radial_allowance_mm": 0.3,
                    "axial_allowance_mm": 0.15,
                    "depth_of_cut_mm": min(1.0, radial_allowance),
                },
                rationale=["按已识别 Z-R 外轮廓分层去除棒料余量", "保留 0.30 mm 径向精车余量"],
                confidence=profile.confidence, status="warning",
            ),
            *inner_operations,
            create_operation_instance(
                id="OP30", sequence=30, type="turn_od_finishing", name="外圆轮廓精车",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool("TURN-OD-F"),
                parameters={**common_finish, "radial_allowance_mm": 0.0, "axial_allowance_mm": 0.0},
                rationale=["沿已识别 Z-R 外轮廓生成刀尖圆弧补偿精车路径"],
                confidence=profile.confidence, status="warning",
            ),
            *[
                create_operation_instance(
                    id=f"OP35-T{index}", sequence=35 + index - 1,
                    type="turn_threading",
                    name=f"车外螺纹草案 {thread.thread_designation or thread.id}",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False,
                    feature_ids=[profile.id, thread.id, *thread.drawing_requirement_ids],
                    tool=get_tool("TURN-THREAD-60"),
                    parameters={
                        **_turning_parameters(0.04, cutting_speed * 0.35),
                        "start_z_mm": max(thread.z_start, thread.z_end),
                        "end_z_mm": min(thread.z_start, thread.z_end),
                        "major_diameter_mm": thread.resolved_major_diameter_mm,
                        "minor_diameter_mm": max(
                            thread.resolved_major_diameter_mm - 2 * thread.depth_mm, 0.01,
                        ),
                        "pitch_mm": thread.resolved_pitch_mm,
                        "thread_depth_mm": thread.depth_mm,
                        "pass_count": max(6, min(12, round(thread.depth_mm / 0.1))),
                    },
                    rationale=[
                        "图纸螺纹要求已由制造工程师与 STEP 周期齿形确认绑定",
                        "工序默认禁用；螺纹起止位置、退刀槽、刀片与控制器循环确认后方可启用",
                    ],
                    confidence=min(thread.confidence, 0.8), status="warning",
                )
                for index, thread in enumerate(matched_threads, start=1)
            ],
            create_operation_instance(
                id="OP40", sequence=40, type="turn_cutoff", name="成品切断",
                channel_id="main", spindle_id="main", workpiece_side="front",
                synchronization_group="TRANSFER-1",
                feature_ids=[profile.id], tool=get_tool("TURN-CUTOFF-2"),
                parameters={
                    **_turning_parameters(0.05, cutting_speed * 0.65),
                    "z_mm": min(z_values),
                    "cutting_width_mm": 2.0,
                    "breakthrough_radius_mm": 0.1,
                },
                rationale=["在回转轮廓后端执行切断", "生成前必须确认接料或成品保持方式"],
                confidence=min(profile.confidence, 0.75), status="warning",
            ),
        ]
        if operations:
            setups.append(Setup(
                id="SETUP-L32-MAIN",
                name="L32 主轴正面顺序车削",
                work_axis=rotational.axes[0].direction,
                datum_feature_id=profile.axis_id,
                fixture="导套/主轴夹头配置待设备实例确认",
                operations=operations,
            ))
            back_axis = rotational.axes[0].direction.model_copy(update={
                "x": -rotational.axes[0].direction.x,
                "y": -rotational.axes[0].direction.y,
                "z": -rotational.axes[0].direction.z,
            })
            back_profile_id = f"{profile.id}-BACK"
            setups.append(Setup(
                id="SETUP-L32-SUB",
                name="L32 背轴接料后背面加工",
                work_axis=back_axis,
                datum_feature_id=f"{profile.id}-BACK-DATUM",
                fixture="背轴夹头；必须先完成同步接料和切断状态迁移",
                operations=[
                create_operation_instance(
                    id="OP50", sequence=50, type="turn_facing", name="背轴端面精车",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", enabled=False,
                    feature_ids=[back_profile_id], tool=get_tool("TURN-OD-F"),
                    parameters={
                        **common_finish,
                        "stock_allowance_mm": 0.0,
                        "depth_of_cut_mm": 0.25,
                        "face_z_mm": 0.0,
                        "center_overtravel_mm": 0.15,
                    },
                    rationale=["接料切断后，以切断端建立背轴 Z0", "绑定具备 back_turning 能力的设备实例后启用"],
                    confidence=min(profile.confidence, 0.8), status="warning",
                ),
                create_operation_instance(
                    id="OP60", sequence=60, type="turn_od_finishing", name="背面切断邻域外圆清根",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", enabled=False,
                    feature_ids=[back_profile_id], tool=get_tool("TURN-OD-F"),
                    parameters={
                        **common_finish,
                        "radial_allowance_mm": 0.0,
                        "axial_allowance_mm": 0.0,
                        "back_cleanup_length_mm": 1.0,
                    },
                    rationale=["仅精车背面切断邻域，不重复加工完整外圆", "绑定具备 back_turning 能力的设备实例后启用"],
                    confidence=min(profile.confidence, 0.75), status="warning",
                ),
                ],
            ))

    automation_status = "unsupported" if blocking_reasons else "review"
    warnings = [
        *blocking_reasons,
        "车削工序已进入正式工艺计划，但执行层仅允许生成控制器无关 DRAFT IR。",
        "设备变体、导套模式、刀具模块、刀位和控制器版本必须通过 L32 实例快照确认。",
        "MELDAS/CINCOM 后处理器未经认证，不生成生产 NC。",
    ]
    if profile is not None and profile.review_state != "accepted":
        warnings.append("回转轴与 Z-R 轮廓来自自动识别，必须在 L32 适配工作台中人工确认。")
    matched_threads = [
        item for item in rotational.features
        if item.kind == "thread_form_candidate" and item.binding_state == "matched"
    ]
    if matched_threads:
        warnings.append(
            f"已将 {len(matched_threads)} 个周期牙形与图纸螺纹要求唯一绑定；已加入默认禁用的车螺纹草案工序。"
        )
    if accepted_inner_profiles:
        warnings.append(
            f"已为 {len(accepted_inner_profiles)} 个确认后的精确内轮廓加入默认禁用的预孔及粗/精镗草案；钻头实物、钻尖越程、初始孔与镗杆可达性确认前不得启用。"
        )
    if stock.get("required_option") == "bar_diameter_38mm":
        warnings.append("候选棒料超过 Ø32；生成草案前必须选择并校验启用 Ø38 棒料选件的设备实例。")

    plan = ProcessPlan(
        title=f"{analysis.source_file} · L32 车削工艺方案",
        material=material,
        machine=machine,
        material_profile=material_profile,
        machine_profile=machine_profile,
        safety=None,
        stock=stock,
        setups=setups,
        warnings=warnings,
        assumptions=[
            "STEP 模型单位为毫米。",
            "首版按 L32 主轴单通道、从正面向负 Z 顺序加工。",
            "棒料预留径向余量和前后端轴向余量，实际棒料规格需人工确认。",
            "切削速度暂由现有材料库保守映射，尚未替代现场车削参数表。",
        ],
        estimated_minutes=round(4.0 + sum(len(item.operations) for item in setups) * 1.5, 1),
        automation_status=automation_status,
        blocking_reasons=blocking_reasons,
        manufacturing_requirements=requirements,
    )
    plan.coverage = evaluate_plan_coverage(analysis, plan)
    plan.manufacturing_route = build_manufacturing_route(analysis, plan)
    plan.knowledge_assessment = assess_plan_knowledge(analysis, plan)
    return plan
