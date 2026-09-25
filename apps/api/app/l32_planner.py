from __future__ import annotations

from math import pi, sqrt

from .catalogs import get_tool, resolve_machine, resolve_material
from .coverage import evaluate_plan_coverage
from .manufacturing_knowledge import assess_plan_knowledge
from .models import Bounds, GeometryAnalysis, ManufacturingRequirements, ProcessPlan, Setup, Tool, Vec3
from .operation_library import create_operation_instance
from .rotational_features import (
    bind_thread_requirements,
    clip_rotational_profile,
    infer_rotational_features,
    split_outer_profile_for_longitudinal_turning,
)
from .groove_binding import bind_verified_groove_requirements
from .route_planner import build_manufacturing_route


def _turning_parameters(feed_mm_rev: float, cutting_speed_m_min: float) -> dict[str, float | str]:
    return {
        "spindle_mode": "constant_surface_speed",
        "cutting_speed_m_min": round(cutting_speed_m_min, 1),
        "maximum_spindle_rpm": 8000,
        "feed_per_revolution_mm": feed_mm_rev,
    }


def _solid_axial_span(analysis: GeometryAnalysis, origin: Vec3, axis: Vec3) -> tuple[float, float] | None:
    """Project the complete selected solid, not a local turning section, onto the spindle."""
    bounds = analysis.measurements.get("bounding_box")
    if not isinstance(bounds, Bounds):
        return None
    values = [
        (x - origin.x) * axis.x + (y - origin.y) * axis.y + (z - origin.z) * axis.z
        for x in (bounds.minimum.x, bounds.maximum.x)
        for y in (bounds.minimum.y, bounds.maximum.y)
        for z in (bounds.minimum.z, bounds.maximum.z)
    ]
    return min(values), max(values)


def _nonrotational_turning_limit(
    analysis: GeometryAnalysis, origin: Vec3, axis: Vec3, maximum_profile_radius: float,
) -> float | None:
    """Protect material protruding beyond a local revolved section from OD turning."""
    if not any(item.review_state == "accepted" for item in analysis.prismatic_features):
        return None
    candidates: list[float] = []
    for face in analysis.planar_features:
        if face.bounds is None or face.area < 0.1:
            continue
        normal_dot_axis = face.normal.x * axis.x + face.normal.y * axis.y + face.normal.z * axis.z
        if abs(normal_dot_axis) > 0.05:
            continue
        corners = [
            Vec3(x=x, y=y, z=z)
            for x in (face.bounds.minimum.x, face.bounds.maximum.x)
            for y in (face.bounds.minimum.y, face.bounds.maximum.y)
            for z in (face.bounds.minimum.z, face.bounds.maximum.z)
        ]
        axial = [
            (p.x - origin.x) * axis.x + (p.y - origin.y) * axis.y + (p.z - origin.z) * axis.z
            for p in corners
        ]
        if max(axial) - min(axial) < 0.1:
            continue
        radial_max = max(
            sqrt(max(
                (p.x - origin.x) ** 2 + (p.y - origin.y) ** 2 + (p.z - origin.z) ** 2
                - projected ** 2, 0.0,
            ))
            for p, projected in zip(corners, axial)
        )
        if radial_max > maximum_profile_radius + 0.05:
            candidates.append(max(axial))
    return max(candidates) if candidates else None


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
    rotational = bind_verified_groove_requirements(rotational, requirements)
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
        spindle_axis = next((item for item in rotational.axes if item.id == profile.axis_id), None)
        protected_limit = (
            _nonrotational_turning_limit(
                analysis, spindle_axis.origin, spindle_axis.direction, maximum_radius,
            ) if spindle_axis is not None else None
        )
        solid_span = (
            _solid_axial_span(analysis, spindle_axis.origin, spindle_axis.direction)
            if spindle_axis is not None else None
        )
        finished_back_z = min(z_values)
        axial_profile_complete = True
        if solid_span is not None:
            axial_profile_complete = (
                min(z_values) <= solid_span[0] + 0.05
                and max(z_values) >= solid_span[1] - 0.05
            )
            if analysis.prismatic_features and any(
                item.review_state == "accepted" for item in analysis.prismatic_features
            ):
                finished_back_z = min(finished_back_z, solid_span[0])
        radial_allowance = max(1.0, round(maximum_radius * 0.05, 2))
        stock_radius = maximum_radius + radial_allowance
        stock_length = max(
            profile_length,
            (solid_span[1] - solid_span[0]) if solid_span and finished_back_z < min(z_values) else 0,
        ) + 4.0
        stock = {
            "type": "round_bar",
            "diameter_mm": round(stock_radius * 2, 3),
            "length_mm": round(stock_length, 3),
            "allowance_mm": {"radial": radial_allowance, "axial": 2.0},
            "rotational_profile_id": profile.id,
            "profile_review_state": profile.review_state,
            "profile_extraction_method": profile.extraction_method,
            "profile_axial_complete": axial_profile_complete,
            "finished_back_z_mm": round(finished_back_z, 6),
        }
        if protected_limit is not None:
            stock["nonrotational_turning_limit_z_mm"] = round(protected_limit, 6)
            stock["nonrotational_region_z_mm"] = [
                round(min(finished_back_z, protected_limit), 6),
                round(max(finished_back_z, protected_limit), 6),
            ]
            # A turning section cannot prove the material state of this region.
            # Only a later 3D cutter-sweep comparison may set this to true.
            stock["nonrotational_material_verified"] = False
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
        turning_profile = profile
        if protected_limit is not None and protected_limit > min(z_values) + 0.05:
            safe_points = sorted({point.z for point in profile.points if point.z >= protected_limit - 1e-6})
            if len(safe_points) < 2:
                blocking_reasons.append("非回转实体占据回转轮廓末端，无法确定安全的外圆车削区间。")
            else:
                turning_profile = clip_rotational_profile(profile, safe_points[0], max(z_values))
        front_turning_profile, front_form_candidate, backside_turning_candidate = (
            split_outer_profile_for_longitudinal_turning(turning_profile)
        )
        front_region_parameters: dict[str, float | str | bool] = {
            "cut_direction": "negative_z",
            "profile_z_min_mm": min(point.z for point in front_turning_profile.points),
            "profile_z_max_mm": max(point.z for point in front_turning_profile.points),
            "profile_region_complete": (
                front_form_candidate is None and backside_turning_candidate is None
                and protected_limit is None
            ),
        }
        unresolved_turning_regions = []
        if front_form_candidate is not None:
            stock["planned_turning_regions"] = [{
                "side": "front_form_candidate",
                "z_min_mm": min(point.z for point in front_form_candidate.points),
                "z_max_mm": max(point.z for point in front_form_candidate.points),
                "required_capability": "front_form_turning",
                "status": "draft_planned",
                "operation_ids": ["OP21-FORM", "OP31-FORM"],
            }]
        if backside_turning_candidate is not None:
            unresolved_turning_regions.append({
                "side": "back_candidate",
                "z_min_mm": min(point.z for point in backside_turning_candidate.points),
                "z_max_mm": max(point.z for point in backside_turning_candidate.points),
                "required_capability": "back_turning",
                "status": "uncovered",
            })
        if unresolved_turning_regions:
            stock["unresolved_turning_regions"] = unresolved_turning_regions

        front_points = sorted(front_turning_profile.points, key=lambda item: item.z)
        sharp_outer_transitions = [
            (left, right)
            for left, right in zip(front_points, front_points[1:])
            if abs(right.radius - left.radius) > max(abs(right.z - left.z), 0.1)
        ]
        finish_tool_id = "TURN-OD-MICRO-F" if sharp_outer_transitions else "TURN-OD-F"
        front_form_parameters: dict[str, float | str | bool] = {}
        if front_form_candidate is not None:
            front_form_parameters = {
                "cut_direction": "positive_z",
                "profile_z_min_mm": min(point.z for point in front_form_candidate.points),
                "profile_z_max_mm": max(point.z for point in front_form_candidate.points),
                "profile_region_complete": False,
            }
        matched_threads = [
            item for item in rotational.features
            if item.kind == "thread_form_candidate"
            and item.binding_state == "matched"
            and item.resolved_pitch_mm is not None
            and item.resolved_major_diameter_mm is not None
            and item.thread_side in {"external", "unknown"}
        ]
        groove_features = [
            item for item in rotational.features
            if item.profile_id == profile.id
            and item.kind == "external_groove_candidate"
            and profile.extraction_method == "exact_section"
            and profile.review_state == "accepted"
        ]
        accepted_pockets = [
            item for item in analysis.prismatic_features
            if item.kind == "pocket"
            and item.review_state == "accepted"
            and min(item.length, item.width) >= 1.0
        ]
        live_tool = get_tool("EM-1")
        live_tool_rpm = min(
            round(material_profile.milling_speed_m_min * 1000 / (pi * live_tool.diameter_mm)),
            live_tool.max_rpm,
            6000,
        )
        live_tool_feed = round(
            live_tool_rpm * live_tool.flute_count * material_profile.mill_feed_per_tooth_mm,
            1,
        )
        nonrotational_operations = []
        if protected_limit is not None:
            region_start = min(finished_back_z, protected_limit)
            region_end = max(finished_back_z, protected_limit)
            shared_parameters = {
                "depth_mm": round(stock_radius, 3),
                "step_down_mm": 0.4,
                "spindle_rpm": live_tool_rpm,
                "feed_rate_mm_min": live_tool_feed,
                "plunge_rate_mm_min": round(live_tool_feed * 0.3, 1),
                "required_module": "U30B",
                "source_geometry": "exact_side_faces",
                "region_z_min_mm": round(region_start, 6),
                "region_z_max_mm": round(region_end, 6),
                "indexed_spindle_increment_deg": 1,
            }
            nonrotational_operations = [
                create_operation_instance(
                    id="OP34-NR-R", sequence=34, type="live_tool_contour_roughing",
                    name="非回转外形分层粗铣", channel_id="main", spindle_id="main",
                    workpiece_side="front", feature_ids=[profile.id], tool=live_tool,
                    parameters={**shared_parameters, "radial_allowance_mm": 0.15},
                    rationale=["使用 U30B 动力刀具从正反两个径向侧面分层去除圆棒余料", "刀路边界取自原始 STEP 精确侧面轮廓"],
                    confidence=min(profile.confidence, 0.8), status="warning",
                ),
                create_operation_instance(
                    id="OP36-NR-F", sequence=36, type="live_tool_contour_finishing",
                    name="非回转外形轮廓精铣", channel_id="main", spindle_id="main",
                    workpiece_side="front", feature_ids=[profile.id], tool=live_tool,
                    parameters={**shared_parameters, "radial_allowance_mm": 0.0, "spring_pass": True},
                    rationale=["沿原始 STEP 精确侧面边界清除粗加工余量", "两侧刀具扫掠必须分别通过目标实体过切检查"],
                    confidence=min(profile.confidence, 0.8), status="warning",
                ),
            ]
        pocket_operations = []
        for index, pocket in enumerate(accepted_pockets, start=1):
            common_pocket = {
                "depth_mm": pocket.depth,
                "step_down_mm": min(0.3, pocket.depth),
                "spindle_rpm": live_tool_rpm,
                "feed_rate_mm_min": live_tool_feed,
                "plunge_rate_mm_min": round(live_tool_feed * 0.25, 1),
                "required_module": "U151B",
                "source_face_id": pocket.source_face_id,
                "access_x": pocket.access_direction.x,
                "access_y": pocket.access_direction.y,
                "access_z": pocket.access_direction.z,
            }
            pocket_operations.extend([
                create_operation_instance(
                    id=f"OP52-P{index}-R", sequence=52 + (index - 1) * 2,
                    type="pocket_roughing", name=f"背面型腔分层粗铣 {index}",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", feature_ids=[pocket.id], tool=live_tool,
                    parameters={
                        **common_pocket, "step_over_percent": 35,
                        "wall_allowance_mm": 0.1, "floor_allowance_mm": 0.05,
                    },
                    rationale=["使用 U151B 背面动力刀具按型腔深度逐层清料", "加工边界来自已接受的精确型腔底面"],
                    confidence=min(pocket.confidence, 0.8), status="warning",
                ),
                create_operation_instance(
                    id=f"OP53-P{index}-F", sequence=53 + (index - 1) * 2,
                    type="pocket_finishing", name=f"背面型腔侧壁与底面精铣 {index}",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", feature_ids=[pocket.id], tool=live_tool,
                    parameters={
                        **common_pocket, "step_down_mm": min(0.2, pocket.depth),
                        "wall_allowance_mm": 0.0, "floor_allowance_mm": 0.0,
                        "spring_pass": True,
                    },
                    rationale=["清除型腔侧壁和底面余量", "矩形尖角剩余材料由实际扫掠体积检查报告"],
                    confidence=min(pocket.confidence, 0.8), status="warning",
                ),
            ])
        inner_profile = max(
            accepted_inner_profiles,
            key=lambda item: max(point.z for point in item.points) - min(point.z for point in item.points),
            default=None,
        )
        inner_operations = []
        inner_groove_operations = []
        if inner_profile is not None:
            inner_radii = [point.radius for point in inner_profile.points]
            inner_depth = max(point.z for point in inner_profile.points) - min(point.z for point in inner_profile.points)
            minimum_inner_radius = min(inner_radii)
            entry_z = max(point.z for point in inner_profile.points)
            bore_features = sorted(
                (
                    item for item in rotational.features
                    if item.profile_id == inner_profile.id and item.kind == "inner_bore"
                ),
                key=lambda item: entry_z - min(item.z_start, item.z_end),
                reverse=True,
            )
            internal_groove_features = [
                item for item in rotational.features
                if item.profile_id == inner_profile.id
                and item.kind == "internal_groove_candidate"
            ]
            internal_groove_planes = {
                round(value, 6)
                for item in internal_groove_features
                for value in (item.z_start, item.z_end)
            }
            sharp_inner_shoulders = [
                item for item in rotational.features
                if item.profile_id == inner_profile.id
                and item.kind == "radial_transition"
                and abs(item.radius_end - item.radius_start) > 0.05
                and round(item.z_start, 6) not in internal_groove_planes
            ]
            maximum_finish_nose_radius = (
                0.05 if sharp_inner_shoulders else get_tool("TURN-ID-F").nose_radius_mm
            )
            bore_stage_count = max(len({round(item.radius_start * 2, 3) for item in bore_features}), 1)
            maximum_bore_ratio = inner_depth / max(minimum_inner_radius * 2, 0.001)
            required_entry_diameter = get_tool("TURN-ID-R").holder_diameter_mm + 0.4
            stage_specs = [
                {
                    "feature_id": item.id,
                    "target_diameter_mm": item.radius_start * 2,
                    "depth_mm": entry_z - min(item.z_start, item.z_end),
                }
                for item in bore_features
            ] or [{
                "feature_id": None,
                "target_diameter_mm": minimum_inner_radius * 2,
                "depth_mm": inner_depth,
            }]
            planned_stages: list[tuple[dict[str, float | str | None], Tool]] = []
            used_drill_diameters: set[float] = set()
            for stage in stage_specs:
                maximum_stage_diameter = max(float(stage["target_diameter_mm"]) - 0.5, 0)
                candidates = [
                    get_tool(f"DRILL-{diameter:.1f}")
                    for diameter in (12.0, 11.0, 10.5, 10.0, 8.0, 7.0, 6.5, 6.0, 5.0, 4.0, 3.0, 2.0)
                    if diameter <= maximum_stage_diameter
                    and (planned_stages or diameter >= required_entry_diameter)
                    and (not planned_stages or diameter > planned_stages[-1][1].diameter_mm)
                    and diameter not in used_drill_diameters
                ]
                if candidates:
                    planned_stages.append((stage, candidates[0]))
                    used_drill_diameters.add(candidates[0].diameter_mm)
                elif not planned_stages:
                    break
                if len(planned_stages) >= 3:
                    break
            prebore_operation = []
            for stage_index, (stage, prebore_tool) in enumerate(planned_stages, start=1):
                drilling_rpm = min(
                    round(material_profile.drilling_speed_m_min * 1000 / (pi * prebore_tool.diameter_mm)),
                    prebore_tool.max_rpm,
                    machine_profile.max_spindle_rpm,
                )
                operation_id = "OP22" if len(planned_stages) == 1 else f"OP22-S{stage_index}"
                stage_depth = float(stage["depth_mm"])
                maximum_stage_diameter = max(float(stage["target_diameter_mm"]) - 0.5, 0)
                prebore_operation.append(create_operation_instance(
                    id=operation_id, sequence=21 + stage_index, type="axial_drilling",
                    name=f"阶梯孔预孔 {stage_index}/{len(planned_stages)}" if len(planned_stages) > 1 else "内孔镗削预孔草案",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False,
                    feature_ids=[inner_profile.id, *([str(stage["feature_id"])] if stage["feature_id"] else [])],
                    tool=prebore_tool,
                    parameters={
                        "spindle_mode": "constant_rpm",
                        "spindle_rpm": drilling_rpm,
                        "cutting_speed_m_min": material_profile.drilling_speed_m_min,
                        "maximum_spindle_rpm": drilling_rpm,
                        "feed_per_revolution_mm": material_profile.drill_feed_per_rev_mm,
                        "start_z_mm": entry_z,
                        "depth_mm": stage_depth,
                        "peck_depth_mm": min(2.0, prebore_tool.diameter_mm * 0.5),
                        "profile_depth_mm": stage_depth,
                        "bore_stage_count": bore_stage_count,
                        "planned_drilling_stage_count": len(planned_stages),
                        "drilling_stage_index": stage_index,
                        "target_bore_diameter_mm": round(float(stage["target_diameter_mm"]), 3),
                        "stage_length_to_diameter_ratio": round(stage_depth / prebore_tool.diameter_mm, 3),
                        "maximum_bore_length_to_diameter_ratio": round(maximum_bore_ratio, 3),
                        "deep_hole_review_required": stage_depth / prebore_tool.diameter_mm > 5,
                        "required_initial_bore_diameter_mm": required_entry_diameter if stage_index == 1 else 0,
                        "maximum_prebore_diameter_mm": maximum_stage_diameter,
                    },
                    rationale=[
                        "预孔直径由对应孔级成品直径与精加工余量约束",
                        "深孔级优先、浅层大径级随后，避免大钻头越过更小的深层孔段",
                        "实际钻头、伸出、钻尖越程和排屑策略完成审核前保持禁用",
                    ],
                    confidence=min(inner_profile.confidence, 0.7), status="warning",
                ))
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
                        "bore_stage_count": bore_stage_count,
                        "maximum_bore_length_to_diameter_ratio": round(maximum_bore_ratio, 3),
                        "sharp_inner_shoulder_count": len(sharp_inner_shoulders),
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
                        "bore_stage_count": bore_stage_count,
                        "maximum_bore_length_to_diameter_ratio": round(maximum_bore_ratio, 3),
                        "sharp_inner_shoulder_count": len(sharp_inner_shoulders),
                        "shoulder_strategy_required": bool(sharp_inner_shoulders),
                        "maximum_finish_nose_radius_mm": maximum_finish_nose_radius or 0,
                    },
                    rationale=[
                        "精镗草案沿已确认的内孔 Z-R 轮廓生成",
                        "刀杆伸出、入口孔和内轮廓可达性通过前保持禁用",
                        *(["检测到尖锐内肩；精镗必须选择刀尖半径不大于 0.05 mm 的实物刀具并重新验证连续余料链"] if sharp_inner_shoulders else []),
                    ],
                    confidence=min(inner_profile.confidence, 0.7), status="warning",
                ),
            ]
            inner_groove_operations = [
                create_operation_instance(
                    id=f"OP32-IG{index}", sequence=32,
                    type="turn_grooving", name=f"内槽加工草案 {index}",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=False,
                    feature_ids=[inner_profile.id, groove.id, *groove.drawing_requirement_ids],
                    tool=get_tool("TURN-ID-GROOVE-1"),
                    parameters={
                        **_turning_parameters(0.025, cutting_speed * 0.45),
                        "groove_side": "internal",
                        "z_mm": round((groove.z_start + groove.z_end) / 2, 6),
                        "groove_start_z_mm": min(groove.z_start, groove.z_end),
                        "groove_end_z_mm": max(groove.z_start, groove.z_end),
                        "groove_width_mm": groove.width_mm,
                        "final_diameter_mm": max(groove.radius_start, groove.radius_end) * 2,
                        "groove_depth_mm": groove.depth_mm,
                        "peck_depth_mm": min(0.25, groove.depth_mm),
                        "entry_z_mm": entry_z,
                        "groove_reach_depth_mm": entry_z - max(groove.z_start, groove.z_end),
                        "required_initial_bore_diameter_mm": get_tool("TURN-ID-GROOVE-1").holder_diameter_mm + 0.4,
                        "drawing_binding_status": groove.binding_state,
                        "drawing_requirement_id": (
                            groove.drawing_requirement_ids[0]
                            if groove.drawing_requirement_ids else ""
                        ),
                    },
                    rationale=[
                        "局部内槽从已接受的精确内轮廓分离，OP28 仅加工基孔轮廓",
                        "内槽刀杆入口、轴向伸出、槽形和现场实物完成审核前保持禁用",
                    ],
                    confidence=min(groove.confidence, 0.7), status="warning",
                )
                for index, groove in enumerate(internal_groove_features, start=1)
            ]
        back_face_allowance_mm = 0.25
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
                    **front_region_parameters,
                    "radial_allowance_mm": 0.3,
                    "axial_allowance_mm": 0.15,
                    "depth_of_cut_mm": min(1.0, radial_allowance),
                },
                rationale=["按已识别 Z-R 外轮廓分层去除棒料余量", "保留 0.30 mm 径向精车余量"],
                confidence=profile.confidence, status="warning",
            ),
            *([create_operation_instance(
                id="OP21-FORM", sequence=21, type="turn_od_roughing",
                name="前端成形区反向分层粗车",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool("TURN-OD-L-R"),
                parameters={
                    **common_rough,
                    **front_form_parameters,
                    "radial_allowance_mm": 0.3,
                    "axial_allowance_mm": 0.15,
                    "depth_of_cut_mm": min(1.0, radial_allowance),
                },
                rationale=[
                    "陡峭前端成形区与负 Z 主纵车区分离",
                    "使用左手外圆刀沿正 Z 方向逐层去除余量",
                ],
                confidence=min(profile.confidence, 0.75), status="warning",
            )] if front_form_candidate is not None else []),
            *inner_operations,
            create_operation_instance(
                id="OP30", sequence=30, type="turn_od_finishing", name="外圆轮廓精车",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool(finish_tool_id),
                parameters={
                    **common_finish,
                    **front_region_parameters,
                    "radial_allowance_mm": 0.0,
                    "axial_allowance_mm": 0.0,
                    "maximum_finish_nose_radius_mm": (
                        0.2 if sharp_outer_transitions else get_tool("TURN-OD-F").nose_radius_mm
                    ),
                },
                rationale=["沿已识别 Z-R 外轮廓生成刀尖圆弧补偿精车路径"],
                confidence=profile.confidence, status="warning",
            ),
            *([create_operation_instance(
                id="OP31-FORM", sequence=31, type="turn_od_finishing",
                name="前端成形区反向精车",
                channel_id="main", spindle_id="main", workpiece_side="front",
                feature_ids=[profile.id], tool=get_tool("TURN-OD-L-MICRO-F"),
                parameters={
                    **common_finish,
                    **front_form_parameters,
                    "radial_allowance_mm": 0.0,
                    "axial_allowance_mm": 0.0,
                    "maximum_finish_nose_radius_mm": 0.2,
                },
                rationale=[
                    "使用左手小刀尖刀具沿正 Z 方向完成前端回转成形轮廓",
                    "仅输出控制器无关 DRAFT，刀位与刀片实物仍需现场确认",
                ],
                confidence=min(profile.confidence, 0.75), status="warning",
            )] if front_form_candidate is not None else []),
            *inner_groove_operations,
            *[
                create_operation_instance(
                    id=f"OP33-G{index}", sequence=32 + index,
                    type="turn_grooving", name=f"前端细颈槽加工 {index}",
                    channel_id="main", spindle_id="main", workpiece_side="front",
                    enabled=float(get_tool("TURN-GROOVE-0.8" if groove.width_mm < 2.0 else "TURN-GROOVE-2").cutting_width_mm or 0) <= groove.width_mm + 1e-9,
                    feature_ids=[profile.id, groove.id, *groove.drawing_requirement_ids],
                    tool=get_tool("TURN-GROOVE-0.8" if groove.width_mm < 2.0 else "TURN-GROOVE-2"),
                    parameters={
                        **_turning_parameters(0.04, cutting_speed * 0.55),
                        "z_mm": round((groove.z_start + groove.z_end) / 2, 6),
                        "groove_start_z_mm": min(groove.z_start, groove.z_end),
                        "groove_end_z_mm": max(groove.z_start, groove.z_end),
                        "groove_width_mm": groove.width_mm,
                        "final_diameter_mm": min(groove.radius_start, groove.radius_end) * 2,
                        "groove_depth_mm": groove.depth_mm,
                        "peck_depth_mm": min(0.5, groove.depth_mm),
                        "drawing_binding_status": groove.binding_state,
                        "drawing_requirement_id": (
                            groove.drawing_requirement_ids[0]
                            if groove.drawing_requirement_ids else ""
                        ),
                    },
                    rationale=[
                        "外槽位置、宽度和槽底直径来自已接受的精确外轮廓",
                        "目录刀宽适配槽底，逐带切入路径由原始 STEP 实体扫掠校核",
                    ],
                    confidence=min(groove.confidence, 0.75), status="warning",
                )
                for index, groove in enumerate(groove_features, start=1)
            ],
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
            *nonrotational_operations,
            create_operation_instance(
                id="OP40", sequence=40, type="turn_cutoff", name="成品切断",
                channel_id="main", spindle_id="main", workpiece_side="front",
                synchronization_group="TRANSFER-1",
                feature_ids=[profile.id], tool=get_tool("TURN-CUTOFF-2"),
                parameters={
                    **_turning_parameters(0.05, cutting_speed * 0.65),
                    "z_mm": (
                        finished_back_z
                        - back_face_allowance_mm
                        - float(get_tool("TURN-CUTOFF-2").cutting_width_mm or 0) / 2
                    ),
                    "cutting_width_mm": 2.0,
                    "breakthrough_radius_mm": 0.1,
                    "finished_back_datum_z_mm": finished_back_z,
                    "retained_material_min_z_mm": finished_back_z - back_face_allowance_mm,
                    "back_face_allowance_mm": back_face_allowance_mm,
                    "sacrificial_extension_mm": (
                        float(get_tool("TURN-CUTOFF-2").cutting_width_mm or 0)
                        + back_face_allowance_mm
                    ),
                },
                rationale=[
                    "切断刀中心向成品背面外偏移半个刀宽，使完整刀缝落在牺牲余料内",
                    "成品背面基准与切断刀中心分离；生成前必须确认接料和背轴 Z0",
                ],
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
                *([create_operation_instance(
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
                )] if protected_limit is None else []),
                *pocket_operations,
                *([create_operation_instance(
                    id="OP55-BACK", sequence=55, type="turn_od_roughing",
                    name="背面大肩部区域分层粗车",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", enabled=False,
                    feature_ids=[back_profile_id], tool=get_tool("TURN-OD-R"),
                    parameters={
                        **common_rough,
                        "source_region_z_min_mm": min(point.z for point in backside_turning_candidate.points),
                        "source_region_z_max_mm": max(point.z for point in backside_turning_candidate.points),
                        "cut_direction": "negative_z",
                        "profile_region_complete": False,
                        "radial_allowance_mm": 0.3,
                        "axial_allowance_mm": 0.15,
                        "depth_of_cut_mm": min(1.0, radial_allowance),
                    },
                    rationale=[
                        "从切断平面向背轴内部加工原正面不可达的大肩部区域",
                        "仅在确认背面刀具模块后允许生成 DRAFT；实物刀位和接料仍需审核",
                    ],
                    confidence=min(profile.confidence, 0.7), status="warning",
                ), create_operation_instance(
                    id="OP58-BACK", sequence=58, type="turn_od_finishing",
                    name="背面大肩部区域轮廓精车",
                    channel_id="sub", spindle_id="sub", workpiece_side="back",
                    synchronization_group="TRANSFER-1", enabled=False,
                    feature_ids=[back_profile_id], tool=get_tool("TURN-OD-MICRO-F"),
                    parameters={
                        **common_finish,
                        "source_region_z_min_mm": min(point.z for point in backside_turning_candidate.points),
                        "source_region_z_max_mm": max(point.z for point in backside_turning_candidate.points),
                        "cut_direction": "negative_z",
                        "profile_region_complete": False,
                        "radial_allowance_mm": 0.0,
                        "axial_allowance_mm": 0.0,
                        "maximum_finish_nose_radius_mm": 0.2,
                    },
                    rationale=[
                        "槽形在正面截面中先行剥离，再换算为背轴轮廓；槽仍保留给专用工序",
                        "控制器无关 DRAFT，不代表背轴刀架和夹头实物干涉已认证",
                    ],
                    confidence=min(profile.confidence, 0.7), status="warning",
                )] if backside_turning_candidate is not None else []),
                ],
            ))

    automation_status = "unsupported" if blocking_reasons else "review"
    warnings = [
        *blocking_reasons,
        "车削工序已进入正式工艺计划，但执行层仅允许生成控制器无关 DRAFT IR。",
        "设备变体、导套模式、刀具模块、刀位和控制器版本必须通过 L32 实例快照确认。",
        "MELDAS/CINCOM 后处理器未经认证，不生成生产 NC。",
    ]
    if stock.get("profile_axial_complete") is False:
        warnings.append("已识别的回转轮廓只覆盖局部轴向范围；整件程序必须补全非回转工序并重新验证切断位置。")
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
