from __future__ import annotations

from collections import defaultdict
from math import radians, sqrt, tan

from .catalogs import apply_cutting_parameters, resolve_machine, resolve_material
from .coverage import evaluate_plan_coverage
from .collision import build_safety_configuration
from .models import GeometryAnalysis, Operation, ProcessPlan, SafetyConfiguration, Setup, Tool, Vec3
from .operation_library import create_operation_instance


def _dot(left: Vec3, right: Vec3) -> float:
    return left.x * right.x + left.y * right.y + left.z * right.z


def _axis_key(axis: Vec3) -> tuple[int, int, int]:
    values = [axis.x, axis.y, axis.z]
    dominant = max(range(3), key=lambda index: abs(values[index]))
    sign = 1 if values[dominant] >= 0 else -1
    result = [0, 0, 0]
    result[dominant] = sign
    return tuple(result)


def _vec_from_key(key: tuple[int, int, int]) -> Vec3:
    return Vec3(x=key[0], y=key[1], z=key[2])


def _axis_label(key: tuple[int, int, int]) -> str:
    labels = {(1, 0, 0): "+X", (-1, 0, 0): "-X", (0, 1, 0): "+Y", (0, -1, 0): "-Y", (0, 0, 1): "+Z", (0, 0, -1): "-Z"}
    return labels.get(key, "自定义方向")


def _extent_along_axis(bounds, key: tuple[int, int, int]) -> float:
    """Return material thickness along the active setup tool axis."""
    return abs(key[0]) * bounds.size.x + abs(key[1]) * bounds.size.y + abs(key[2]) * bounds.size.z


def _tool_for_hole(diameter: float) -> Tool:
    if diameter <= 20:
        return Tool(id=f"DRILL-{diameter:.1f}", name=f"Ø{diameter:.1f} 麻花钻", kind="drill", diameter_mm=diameter)
    return Tool(id="EM-12", name="Ø12 平底立铣刀", kind="end_mill", diameter_mm=12)


def _tool_for_prismatic(width: float) -> Tool:
    candidates = [2, 3, 4, 6, 8, 10, 12, 16]
    maximum = max(2, width * 0.45)
    diameter = max((item for item in candidates if item <= maximum), default=2)
    return Tool(id=f"EM-{diameter}", name=f"Ø{diameter} 平底立铣刀", kind="end_mill", diameter_mm=diameter)


def _surface_operations(sequence: int, axis_key: tuple[int, int, int], nonplanar_faces: int) -> tuple[int, list[Operation]]:
    """Plan roughing plus shallow- and steep-surface finishing for one side."""
    # OCL cost rises sharply with both face count and sampling density.  Dense
    # mould-like parts do not benefit from the tiny-part preview defaults, so
    # scale sampling while keeping the finish stepover below 0.8 mm.
    complexity_scale = 2.0 if nonplanar_faces >= 180 else 1.5 if nonplanar_faces >= 80 else 1.0
    rough_tool = Tool(
        id="EM-6-SURFACE", name="Ø6 平底立铣刀", kind="end_mill", diameter_mm=6,
        flute_length_mm=18, stickout_mm=25, holder_diameter_mm=20,
    )
    finish_tool = Tool(
        id="BM-2.5", name="Ø2.5 球头立铣刀", kind="ball_end_mill", diameter_mm=2.5,
        flute_length_mm=10, stickout_mm=18, holder_diameter_mm=10,
    )
    rest_tool = Tool(
        id="BM-1.5", name="Ø1.5 球头立铣刀", kind="ball_end_mill", diameter_mm=1.5,
        flute_length_mm=8, stickout_mm=18, holder_diameter_mm=10,
    )
    direction = _axis_label(axis_key)
    sequence += 10
    roughing = create_operation_instance(
        id=f"OP{sequence}", sequence=sequence, type="surface_roughing",
        name=f"{direction} 三维曲面分层粗加工", feature_ids=[f"SURFACE-SET-{direction}"],
        tool=rough_tool,
        parameters={"step_down_mm": 1.2, "step_over_percent": 50,
                    "depth_offset_mm": 0.3, "sample_interval_mm": 2.0},
        rationale=[f"目标包含约 {nonplanar_faces} 个非平面，2.5D 工序不能覆盖其表面包络",
                   "使用平底刀分层去除曲面上方毛坯并保留 0.30 mm 精加工余量"],
        confidence=0.68, status="warning",
    )
    sequence += 10
    finishing = create_operation_instance(
        id=f"OP{sequence}", sequence=sequence, type="surface_3d",
        name=f"{direction} 球刀三维曲面精加工", feature_ids=[f"SURFACE-SET-{direction}"],
        tool=finish_tool,
        parameters={
            "step_over_mm": round(min(0.8, 0.35 * complexity_scale), 3),
            "sample_interval_mm": round(min(1.0, 0.4 * complexity_scale), 3),
            "depth_offset_mm": 0.0,
        },
        rationale=["球头刀沿目标表面执行交错扫描，覆盖圆角、加强筋与自由曲面",
                   "当前为三轴可见面加工；倒扣和遮挡区域仍需可达性校核"],
        confidence=0.62, status="warning",
    )
    sequence += 10
    waterline = create_operation_instance(
        id=f"OP{sequence}", sequence=sequence, type="waterline",
        name=f"{direction} 小球刀陡壁等高线与圆角清根", feature_ids=[f"SURFACE-SET-{direction}"],
        tool=rest_tool.model_copy(deep=True),
        parameters={
            "depth_mm": 0.3,
            "step_down_mm": round(min(0.3, 0.18 * complexity_scale), 3),
            "step_over_percent": round(min(15, 12 * complexity_scale), 1),
            "sample_interval_mm": round(min(0.4, 0.2 * complexity_scale), 3),
            "depth_offset_mm": 0.0,
            "rest_machining": True,
        },
        rationale=[
            "平行球刀扫描主要覆盖缓坡曲面，陡峭侧壁需要等高线刀路清根并控制残留刀纹",
            "使用 Ø1.5 球刀按 0.18 mm 层高精加工，覆盖 R0.75 及以上圆角过渡",
            "小于 R0.75 的局部过渡需使用更高阶残料识别 CAM 或由制造工程师复核",
        ],
        confidence=0.66, status="warning",
    )
    return sequence, [roughing, finishing, waterline]


def _datum_for_axis(analysis: GeometryAnalysis, axis: Vec3):
    matching = [plane for plane in analysis.planar_features if abs(_dot(plane.normal, axis)) >= 0.98]
    return max(matching, key=lambda plane: plane.area, default=None)


def _sheet_forming_plan(
    analysis: GeometryAnalysis,
    material: str,
    machine: str,
    equivalent_thickness: float,
) -> ProcessPlan:
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)
    material_profile = resolve_material(material)
    machine_profile = resolve_machine(machine)
    nominal_thickness = round(max(0.05, round(equivalent_thickness / 0.05) * 0.05), 3)
    developed_area = round(float(analysis.measurements.get("volume", 0.0)) / nominal_thickness, 2)
    holes = [
        feature.id for feature in analysis.cylindrical_features
        if feature.kind == "hole" and feature.review_state != "excluded"
    ]
    process_tool = Tool(
        id="SHEET-PROCESS", name="薄板工艺装备（待选型）", kind="forming_equipment",
        diameter_mm=nominal_thickness, flute_count=0, max_rpm=0, catalog_match=False,
        flute_length_mm=0, stickout_mm=0, holder_diameter_mm=0,
    )
    operations = [
        Operation(
            id="OP10", sequence=10, type="sheet_flat_pattern", name="生成并复核板料展开",
            feature_ids=[], tool=process_tool.model_copy(deep=True),
            parameters={
                "nominal_thickness_mm": nominal_thickness,
                "estimated_developed_area_mm2": developed_area,
                "formation_start": 0.0, "formation_end": 0.0,
            },
            rationale=[
                f"实体体积/表面积推算名义板厚约 {nominal_thickness:.2f} mm",
                "当前动画采用压平预览；真实下料轮廓必须通过中性层展开并复核弯曲扣除",
            ], confidence=0.82, status="warning",
        ),
        Operation(
            id="OP20", sequence=20, type="sheet_blanking", name="平板下料与孔槽加工",
            feature_ids=holes, tool=Tool(
                id="LASER-FINE", name="精细激光/精密冲裁设备", kind="laser_or_punch",
                diameter_mm=0.1, flute_count=0, max_rpm=0, catalog_match=False,
                flute_length_mm=0, stickout_mm=0, holder_diameter_mm=0,
            ),
            parameters={
                "nominal_thickness_mm": nominal_thickness,
                "estimated_developed_area_mm2": developed_area,
                "recognized_hole_count": len(holes),
                "kerf_compensation_mm": 0.05,
                "formation_start": 0.0, "formation_end": 0.0,
            },
            rationale=["孔、窗口和外轮廓应在成形前加工，避免成形后定位和刀具干涉"],
            confidence=0.72, status="warning",
        ),
        Operation(
            id="OP30", sequence=30, type="sheet_preforming", name="预成形与对称折弯",
            feature_ids=[], tool=Tool(
                id="DIE-PREFORM", name="预成形模具/折弯工装", kind="forming_die",
                diameter_mm=nominal_thickness, flute_count=0, max_rpm=0, catalog_match=False,
                flute_length_mm=0, stickout_mm=0, holder_diameter_mm=0,
            ),
            parameters={
                "formation_start": 0.0, "formation_end": 0.55,
                "target_depth_mm": round(bounds.size.y * 0.55, 3),
                "springback_compensation": "pending_material_test",
            },
            rationale=["先完成约 55% 成形深度，降低 0.3 mm 薄壁一次成形的起皱和撕裂风险"],
            confidence=0.58, status="warning",
        ),
        Operation(
            id="OP40", sequence=40, type="sheet_final_forming", name="终成形至 STEP 目标形状",
            feature_ids=[], tool=Tool(
                id="DIE-FINAL", name="终成形模具", kind="forming_die",
                diameter_mm=nominal_thickness, flute_count=0, max_rpm=0, catalog_match=False,
                flute_length_mm=0, stickout_mm=0, holder_diameter_mm=0,
            ),
            parameters={
                "formation_start": 0.55, "formation_end": 1.0,
                "target_depth_mm": round(bounds.size.y, 3),
                "springback_compensation": "pending_material_test",
            },
            rationale=["以 STEP 成品包络作为终态；模具圆角、压边力和回弹补偿仍需材料参数"],
            confidence=0.55, status="warning",
        ),
        Operation(
            id="OP50", sequence=50, type="sheet_deburring", name="去毛刺、清洗与表面处理",
            feature_ids=[], tool=process_tool.model_copy(deep=True),
            parameters={"formation_start": 1.0, "formation_end": 1.0},
            rationale=["下料边缘和冲孔边缘需去毛刺，避免装配划伤与裂纹起点"],
            confidence=0.8, status="warning",
        ),
        Operation(
            id="OP60", sequence=60, type="sheet_inspection", name="尺寸、轮廓与回弹检测",
            feature_ids=[], tool=Tool(
                id="CMM-OPTICAL", name="影像/CMM 检测设备", kind="inspection",
                diameter_mm=0.0, flute_count=0, max_rpm=0, catalog_match=False,
                flute_length_mm=0, stickout_mm=0, holder_diameter_mm=0,
            ),
            parameters={
                "formation_start": 1.0, "formation_end": 1.0,
                "target_profile": "STEP",
                "forming_depth_mm": round(bounds.size.y, 3),
            },
            rationale=["终件必须与 STEP 轮廓比对，并用实测回弹修正模具或折弯参数"],
            confidence=0.76, status="warning",
        ),
    ]
    setup = Setup(
        id="FORMING-1", name="薄板下料与成形工艺线", work_axis=Vec3(x=0, y=1, z=0),
        datum_feature_id=None, fixture="展开定位 + 专用冲压/折弯模具（待工程师设计）",
        operations=operations,
    )
    return ProcessPlan(
        process_kind="sheet_forming",
        title=f"{analysis.source_file} 薄板成形工艺方案",
        material=material, machine=machine,
        material_profile=material_profile, machine_profile=machine_profile,
        safety=None,
        stock={
            "type": "sheet_blank_candidate",
            "size_mm": [round(bounds.size.x, 3), nominal_thickness, round(bounds.size.z, 3)],
            "nominal_thickness_mm": nominal_thickness,
            "estimated_developed_area_mm2": developed_area,
            "flat_pattern_status": "requires_unfolding_validation",
        },
        setups=[setup],
        warnings=[
            f"已识别为约 {nominal_thickness:.2f} mm 薄板成形件，不生成三轴铣削刀路。",
            "当前提供工艺顺序和几何变形预览；展开尺寸、模具、压边力及回弹尚未通过成形求解器验证。",
            "材料牌号、状态和轧制方向必须由制造工程师确认后才能设计生产模具。",
        ],
        assumptions=[
            "STEP 模型单位为毫米。",
            "使用体积/表面积推算名义板厚，并以 STEP 模型作为最终成形目标。",
            "概念动画仅表达工序状态，不代表应变、减薄、起皱或回弹有限元结果。",
        ],
        estimated_minutes=8.0,
        automation_status="review",
        blocking_reasons=[],
    )


def build_process_plan(
    analysis: GeometryAnalysis,
    material: str,
    machine: str,
    safety: SafetyConfiguration | None = None,
) -> ProcessPlan:
    material_profile = resolve_material(material)
    machine_profile = resolve_machine(machine)
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)

    usable_holes = [
        feature for feature in analysis.cylindrical_features
        if feature.kind == "hole" and feature.review_state != "excluded" and feature.confidence >= 0.5
    ]
    holes_by_direction: dict[tuple[int, int, int], list] = defaultdict(list)
    for hole in usable_holes:
        holes_by_direction[_axis_key(hole.access_direction or hole.axis)].append(hole)

    usable_prismatic = [
        feature for feature in analysis.prismatic_features
        # A review candidate is not yet trusted machining geometry.  Planning
        # it eagerly creates misleading setups and CAM failures for thin walls
        # that merely resemble deep rectangular pockets.  The review endpoint
        # rebuilds the plan as soon as an operator accepts the feature.
        if feature.review_state == "accepted" and feature.confidence >= 0.5
    ]
    prismatic_by_direction: dict[tuple[int, int, int], list] = defaultdict(list)
    for feature in usable_prismatic:
        prismatic_by_direction[_axis_key(feature.access_direction)].append(feature)

    direction_keys = set(holes_by_direction) | set(prismatic_by_direction)

    bbox_volume = max(bounds.size.x * bounds.size.y * bounds.size.z, 1e-6)
    part_volume = float(analysis.measurements.get("volume", bbox_volume))
    surface_area = float(analysis.measurements.get("surface_area", 0.0))
    fill_ratio = max(0.0, min(part_volume / bbox_volume, 1.0))
    ordered_sizes = sorted((bounds.size.x, bounds.size.y, bounds.size.z))
    equivalent_sheet_thickness = 2.0 * part_volume / max(surface_area, 1e-6)
    source_hint = analysis.source_file.casefold().replace("_", " ").replace("-", " ")
    tooling_intent = any(marker in source_hint for marker in (
        "tooling", "soft tool", "mould", "mold", "fixture", "jig",
        "工装", "模具", "夹具", "治具",
    ))
    # A folded thin sheet has a very small volume-to-area thickness while its
    # bounding box is many times deeper than the material itself.  Treating
    # such a part as a low-fill billet caused Surface paths to alternate
    # between gouging 0.3 mm walls and leaving large webs of stock.  Route it
    # to blanking/forming before spending minutes on predictably invalid CAM.
    formed_sheet_candidate = (
        not tooling_intent
        and surface_area > 0
        and equivalent_sheet_thickness <= 0.8
        and fill_ratio <= 0.15
        and ordered_sizes[0] >= max(1.5, equivalent_sheet_thickness * 4.0)
    )
    if formed_sheet_candidate:
        plan = _sheet_forming_plan(
            analysis, material, machine, equivalent_sheet_thickness,
        )
        plan.coverage = evaluate_plan_coverage(analysis, plan)
        return plan
    thin_plate = ordered_sizes[0] <= max(3.5, ordered_sizes[1] * 0.12)
    low_fill_profile = fill_ratio <= 0.72 and ordered_sizes[0] <= max(6.0, ordered_sizes[1] * 0.25)
    thin_axis_index = min(range(3), key=lambda index: (bounds.size.x, bounds.size.y, bounds.size.z)[index])
    profile_planes = [
        plane for plane in analysis.planar_features
        if abs((plane.normal.x, plane.normal.y, plane.normal.z)[thin_axis_index]) >= 0.98
    ]
    profile_plane = max(
        profile_planes,
        key=lambda plane: (
            (plane.normal.x, plane.normal.y, plane.normal.z)[thin_axis_index] > 0,
            plane.area,
        ),
        default=None,
    )
    profile_axis = _axis_key(profile_plane.normal) if profile_plane else (0, 0, 1)
    needs_outer_profile = (thin_plate or low_fill_profile) and fill_ratio < 0.98 and profile_plane is not None
    cylindrical_source_faces = sum(max(len(feature.source_face_ids), 1) for feature in analysis.cylindrical_features)
    residual_nonplanar_faces = max(
        0,
        int(analysis.topology.get("faces", 0))
        - len(analysis.planar_features)
        - cylindrical_source_faces,
    )
    # Partial cylinders and convex cylinders describe radii, fillets and open
    # contours.  They are deliberately excluded from hole drilling, but still
    # require 3D roughing/finishing instead of silently disappearing.
    curved_surface_face_count = sum(
        max(len(feature.source_face_ids), 1)
        for feature in analysis.cylindrical_features
        if feature.kind != "hole" or feature.angular_span_degrees < 355
    )
    nonplanar_face_count = residual_nonplanar_faces + curved_surface_face_count
    needs_surface_machining = needs_outer_profile and nonplanar_face_count > 0
    internal_wire_count = max(0, (profile_plane.wire_count if profile_plane else 1) - 1)
    recognized_profile_holes = len(holes_by_direction[profile_axis])
    uncovered_internal_profiles = max(0, internal_wire_count - recognized_profile_holes)
    blocking_reasons: list[str] = []
    if needs_outer_profile and uncovered_internal_profiles:
        blocking_reasons.extend([
            f"顶面包含 {internal_wire_count} 个内部轮廓，仅有 {recognized_profile_holes} 个可确认为标准孔。",
            "剩余异形内部轮廓尚未覆盖，已阻止生成不完整 CAM。",
        ])
    automation_status = "unsupported" if blocking_reasons else "review" if needs_outer_profile else "ready"

    if needs_outer_profile and not blocking_reasons:
        direction_keys.add(profile_axis)


    if not direction_keys and automation_status != "unsupported":
        primary_plane = max(analysis.planar_features, key=lambda plane: plane.area, default=None)
        primary_axis = _axis_key(primary_plane.normal) if primary_plane else (0, 0, 1)
        direction_keys.add(primary_axis)

    direction_groups = sorted(
        direction_keys,
        key=lambda key: (-(len(holes_by_direction[key]) + len(prismatic_by_direction[key])), key),
    )
    setups: list[Setup] = []
    sequence = 0
    cutting_distance = 0.0

    for setup_index, axis_key in enumerate(direction_groups, start=1):
        holes = holes_by_direction[axis_key]
        prismatic_features = prismatic_by_direction[axis_key]
        work_axis = _vec_from_key(axis_key)
        datum = _datum_for_axis(analysis, work_axis)
        operations: list[Operation] = []

        if not (needs_outer_profile and axis_key == profile_axis):
            sequence += 10
            operations.append(
                create_operation_instance(
                    id=f"OP{sequence}",
                    sequence=sequence,
                    type="face_milling",
                    name="建立装夹基准面" if setup_index == 1 else "复核并精加工定位面",
                    feature_ids=[datum.id] if datum else [],
                    tool=Tool(id="FM-50", name="Ø50 面铣刀", kind="face_mill", diameter_mm=50),
                    parameters={"stock_allowance_mm": 0.2, "step_down_mm": 0.5},
                    rationale=[f"选择与 {_axis_label(axis_key)} 加工方向平行的最大平面作为定位候选"],
                    confidence=0.9 if datum else 0.5,
                    status="proposed" if datum else "warning",
                )
            )

        for feature in sorted(prismatic_features, key=lambda item: (-item.depth, item.id)):
            tool = _tool_for_prismatic(feature.width)
            feature_name = "型腔" if feature.kind == "pocket" else "贯通槽"
            needs_review = feature.review_state == "review"
            sequence += 10
            operations.append(
                create_operation_instance(
                    id=f"OP{sequence}",
                    sequence=sequence,
                    type="pocket_roughing" if feature.kind == "pocket" else "slot_roughing",
                    name=f"粗铣 {feature_name} {feature.length:.1f}×{feature.width:.1f}",
                    feature_ids=[feature.id],
                    tool=tool,
                    parameters={
                        "depth_mm": round(feature.depth, 3),
                        "step_down_mm": round(min(tool.diameter_mm * 0.4, 3.0), 2),
                        "step_over_percent": 45,
                        "wall_allowance_mm": 0.2,
                        "floor_allowance_mm": 0.1,
                    },
                    rationale=[
                        f"底面几何判定为{feature_name}，从 {_axis_label(axis_key)} 方向可达",
                        "刀具直径按特征最小宽度的 45% 上限选择，保留侧壁和底面精加工余量",
                    ],
                    confidence=feature.confidence,
                    status="warning" if needs_review else "proposed",
                )
            )
            sequence += 10
            operations.append(
                create_operation_instance(
                    id=f"OP{sequence}",
                    sequence=sequence,
                    type="pocket_finishing" if feature.kind == "pocket" else "slot_finishing",
                    name=f"精铣 {feature_name} 侧壁与底面",
                    feature_ids=[feature.id],
                    tool=tool,
                    parameters={
                        "depth_mm": round(feature.depth, 3),
                        "wall_allowance_mm": 0.0,
                        "floor_allowance_mm": 0.0,
                        "spring_pass": True,
                    },
                    rationale=["粗加工后独立精加工侧壁与底面，便于控制尺寸和表面质量"],
                    confidence=max(0.5, feature.confidence - 0.03),
                    status="warning" if needs_review else "proposed",
                )
            )
            cutting_distance += feature.length * feature.width * max(feature.depth, 0.1) / max(tool.diameter_mm**2, 1)

        # Establish the target envelope before drilling; the profile remains
        # attached by tabs until every surface and hole operation is complete.
        if needs_surface_machining and axis_key == profile_axis:
            sequence, surface_operations = _surface_operations(sequence, axis_key, nonplanar_face_count)
            operations.extend(surface_operations)

        grouped: dict[tuple[float, str], list] = defaultdict(list)
        for hole in holes:
            grouped[(round(hole.diameter, 2), hole.end_type)].append(hole)

        for (diameter, end_type), features in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
            sequence += 10
            tool = _tool_for_hole(diameter)
            operation_type = "drilling" if tool.kind == "drill" else "helical_boring"
            end_name = {"through": "通孔", "blind": "盲孔", "unknown": "孔候选"}[end_type]
            confidence = min(feature.confidence for feature in features)
            needs_review = any(feature.review_state == "review" for feature in features)
            feature_depth = max(feature.length for feature in features)
            drill_point_angle = 118.0
            breakthrough = 0.5 if end_type == "through" else 0.0
            drill_tip_length = diameter / 2 / tan(radians(drill_point_angle / 2)) if tool.kind == "drill" else 0.0
            programmed_depth = feature_depth + drill_tip_length + breakthrough if end_type == "through" else feature_depth
            operations.append(
                create_operation_instance(
                    id=f"OP{sequence}",
                    sequence=sequence,
                    type=operation_type,
                    name=f"加工 {len(features)}×Ø{diameter:.2f} {end_name}",
                    feature_ids=[feature.id for feature in features],
                    tool=tool,
                    parameters={
                        "feature_depth_mm": round(feature_depth, 3),
                        "depth_mm": round(programmed_depth, 3),
                        "drill_point_angle_deg": drill_point_angle if tool.kind == "drill" else 0.0,
                        "drill_tip_length_mm": round(drill_tip_length, 3),
                        "breakthrough_mm": breakthrough,
                        "coolant": "flood",
                        "peck": end_type == "blind" and max(feature.length for feature in features) > diameter * 3,
                        "finishing_allowance_mm": 0.0 if tool.kind == "drill" else 0.15,
                    },
                    rationale=[
                        f"特征按 {_axis_label(axis_key)} 方向、同直径和同孔端类型归组",
                        "同轴同径的 OCCT 圆柱面已经合并为单一制造特征",
                        *([f"通孔深度已加入 {drill_tip_length:.2f} mm 钻尖长度和 {breakthrough:.1f} mm 穿透余量"] if end_type == "through" and tool.kind == "drill" else []),
                    ],
                    confidence=confidence,
                    status="warning" if needs_review else "proposed",
                )
            )
            cutting_distance += sum(
                sqrt(feature.length**2 + (2 * 3.14159 * feature.radius) ** 2)
                for feature in features
            )

        if needs_outer_profile and axis_key == profile_axis and automation_status != "unsupported":
            profile_thickness = _extent_along_axis(bounds, axis_key)
            profile_tool = Tool(
                id="EM-3", name="Ø3 平底立铣刀", kind="end_mill", diameter_mm=3,
                flute_length_mm=max(12, profile_thickness * 2), stickout_mm=max(20, profile_thickness * 3),
                holder_diameter_mm=20,
            )
            profile_feature_ids = [profile_plane.id] if profile_plane else []
            tab_parameters = {"tab_count": 4, "tab_width_mm": 4.0, "tab_height_mm": 0.6}
            sequence += 10
            operations.append(create_operation_instance(
                id=f"OP{sequence}", sequence=sequence, type="profile_roughing",
                name="外轮廓分层粗铣并保留桥位", feature_ids=profile_feature_ids,
                tool=profile_tool.model_copy(deep=True),
                parameters={
                    "depth_mm": round(profile_thickness + 0.2, 3),
                    "step_down_mm": round(min(1.0, max(0.4, profile_thickness / 3)), 2),
                    "radial_allowance_mm": 0.2,
                    **tab_parameters,
                },
                rationale=[
                    f"薄板目标实体占包围盒 {fill_ratio:.1%}，需要加工异形外轮廓",
                    "粗加工保留 0.20 mm 径向余量和 4 个桥位，避免提前释放工件",
                ], confidence=0.78, status="warning",
            ))
            sequence += 10
            operations.append(create_operation_instance(
                id=f"OP{sequence}", sequence=sequence, type="profile_finishing",
                name="外轮廓精铣并保留桥位", feature_ids=profile_feature_ids,
                tool=profile_tool.model_copy(deep=True),
                parameters={
                    "depth_mm": round(profile_thickness + 0.2, 3), "step_down_mm": 0.8,
                    "radial_allowance_mm": 0.0, "spring_pass": True, **tab_parameters,
                },
                rationale=["清除粗加工径向余量，桥位区域继续抬刀以保持零件固定"],
                confidence=0.75, status="warning",
            ))
            sequence += 10
            operations.append(create_operation_instance(
                id=f"OP{sequence}", sequence=sequence, type="tab_removal",
                name="桥位切除与残根清理", feature_ids=profile_feature_ids,
                tool=profile_tool.model_copy(deep=True),
                parameters={
                    "depth_mm": round(profile_thickness + 0.2, 3), "step_down_mm": 0.3,
                    "radial_allowance_mm": 0.0,
                    "start_depth_from_bottom_mm": 0.9,
                    "requires_secondary_retention": True,
                },
                rationale=["胶粘/压板二次固定后，仅加工底部 0.9 mm 区域以切除 0.6 mm 桥位"],
                confidence=0.68, status="warning",
            ))
            sequence += 10
            chamfer_tool = Tool(
                id="CM-6-90", name="Ø6 90°倒角刀", kind="chamfer_mill", diameter_mm=6,
                flute_length_mm=8, stickout_mm=20, holder_diameter_mm=20,
            )
            operations.append(create_operation_instance(
                id=f"OP{sequence}", sequence=sequence, type="edge_chamfer",
                name="上表面轮廓及孔口倒角去毛刺", feature_ids=profile_feature_ids,
                tool=chamfer_tool,
                parameters={"chamfer_width_mm": 0.3, "extra_depth_mm": 0.05, "included_angle_deg": 90.0},
                rationale=["使用 90°倒角刀加工顶面外轮廓及可达孔口，目标倒角宽度 0.30 mm"],
                confidence=0.72, status="warning",
            ))
            cutting_distance += 4 * (bounds.size.x + bounds.size.y) * max(1, bounds.size.z)

        setups.append(
            Setup(
                id=f"SETUP-{setup_index}",
                name=f"第 {setup_index} 次装夹：{_axis_label(axis_key)} 方向加工",
                work_axis=work_axis,
                datum_feature_id=datum.id if datum else None,
                fixture="牺牲垫板 + 胶粘/压板固定（低实体占比外轮廓候选）" if needs_outer_profile else "平口钳 + 平行垫铁（候选，需校核可达性）",
                operations=operations,
            )
        )

    if needs_outer_profile and automation_status != "unsupported":
        reverse_profile_key = tuple(-value for value in profile_axis)
        reverse_axis = _vec_from_key(reverse_profile_key)
        reverse_setup = next(
            (setup for setup in setups if _axis_key(setup.work_axis) == reverse_profile_key),
            None,
        )
        if reverse_setup is None:
            reverse_setup = Setup(
                id=f"SETUP-{len(setups) + 1}",
                name=f"第 {len(setups) + 1} 次装夹：翻面后 {_axis_label(reverse_profile_key)} 方向加工",
                work_axis=reverse_axis,
                datum_feature_id=profile_plane.id if profile_plane else None,
                fixture="翻面定位 + 牺牲垫板 + 胶粘/软爪固定（候选，需校核）",
                operations=[],
            )
            setups.append(reverse_setup)
        if needs_surface_machining:
            sequence, surface_operations = _surface_operations(sequence, reverse_profile_key, nonplanar_face_count)
            reverse_setup.operations.extend(surface_operations)
        sequence += 10
        reverse_setup.operations.append(create_operation_instance(
            id=f"OP{sequence}", sequence=sequence, type="edge_chamfer",
            name="翻面后底边倒角及桥位残根去毛刺",
            feature_ids=[profile_plane.id] if profile_plane else [],
            tool=Tool(
                id="CM-6-90", name="Ø6 90°倒角刀", kind="chamfer_mill", diameter_mm=6,
                flute_length_mm=8, stickout_mm=20, holder_diameter_mm=20,
            ),
            parameters={"chamfer_width_mm": 0.3, "extra_depth_mm": 0.05, "included_angle_deg": 90.0},
            rationale=["翻面重新找正后清理底边、桥位残根及底侧锐边"],
            confidence=0.65, status="warning",
        ))

    all_operations = [operation for setup in setups for operation in setup.operations]
    parameter_warnings = [
        warning
        for operation in all_operations
        for warning in apply_cutting_parameters(operation, material_profile, machine_profile)
    ]
    estimated_minutes = 0.0 if automation_status == "unsupported" else round(4.0 + len(all_operations) * 1.8 + cutting_distance / 450.0, 1)
    excluded = sum(feature.review_state == "excluded" for feature in analysis.cylindrical_features if feature.kind == "hole")
    review = sum(feature.review_state == "review" for feature in analysis.cylindrical_features if feature.kind == "hole")
    prismatic_review = sum(feature.review_state == "review" for feature in analysis.prismatic_features)
    prismatic_excluded = sum(feature.review_state == "excluded" for feature in analysis.prismatic_features)
    warnings = [
        *blocking_reasons,
        "碰撞预检采用刀具/刀柄圆柱包络与平口钳禁入区，结果仍需制造工程师复核。",
        "转速与进给已按内置材料/刀具/机床参数计算，仍需结合真实刀具伸出和机床刚性复核。",
        *parameter_warnings,
    ]
    source_solids = int(analysis.topology.get("source_solids", 1))
    if source_solids > 1:
        warnings.append(f"STEP 中包含 {source_solids} 个实体，当前自动选择体积最大的实体作为目标零件，请人工确认主体选择。")
    if review:
        warnings.append(f"有 {review} 个孔特征处于待复核状态，相关工序已标记警告。")
    if excluded:
        warnings.append(f"已从自动规划中排除 {excluded} 个非闭合或短圆柱伪特征候选。")
    if prismatic_review:
        warnings.append(f"有 {prismatic_review} 个型腔/槽特征处于待复核状态，需确认开放边界和刀具可达性。")
    if prismatic_excluded:
        warnings.append(f"已从自动规划中排除 {prismatic_excluded} 个型腔/槽候选。")
    if needs_outer_profile and automation_status == "review":
        warnings.append("低实体占比零件已规划双面曲面、外轮廓粗精加工、桥位切除和双面倒角；二次固定、翻面基准及切断顺序必须由制造工程师复核。")
    if len(setups) > 3:
        warnings.append("检测到超过三个主要加工方向，三轴机床可能需要专用夹具或改用四/五轴设备。")

    resolved_safety = safety
    expected_fixture_strategy = "sacrificial_plate" if needs_outer_profile else "vise"
    if resolved_safety is None or resolved_safety.fixture_strategy != expected_fixture_strategy:
        resolved_safety = build_safety_configuration(
            analysis,
            clearance_mm=resolved_safety.clearance_mm if resolved_safety else 3,
            vise_grip_height_mm=resolved_safety.vise_grip_height_mm if resolved_safety else 1.5,
            support_thickness_mm=resolved_safety.support_thickness_mm if resolved_safety else 3.0,
            setup_axes=[(setup.id, setup.work_axis) for setup in setups],
        )

    size_values = (bounds.size.x, bounds.size.y, bounds.size.z)
    profile_axis_index = max(
        range(3),
        key=lambda index: abs(profile_axis[index]),
    )
    if needs_outer_profile and automation_status != "unsupported":
        stock_size = [
            round(value + (2.0 if index == profile_axis_index else 8.0), 3)
            for index, value in enumerate(size_values)
        ]
    elif automation_status == "unsupported":
        stock_size = [round(value, 3) for value in size_values]
    else:
        stock_size = [
            round(bounds.size.x + 6, 3),
            round(bounds.size.y + 6, 3),
            round(bounds.size.z + 4, 3),
        ]

    plan = ProcessPlan(
        title=f"{analysis.source_file} 工艺方案",
        material=material,
        machine=machine,
        material_profile=material_profile,
        machine_profile=machine_profile,
        stock={
            "type": "sheet" if needs_outer_profile else "box",
            "size_mm": stock_size,
            "allowance_mm": {
                "xy": 4.0 if needs_outer_profile and automation_status != "unsupported" else 0.0 if automation_status == "unsupported" else 3.0,
                "z": 1.0 if needs_outer_profile and automation_status != "unsupported" else 0.0 if automation_status == "unsupported" else 2.0,
            },
        },
        setups=setups,
        warnings=warnings,
        assumptions=[
            "零件按三轴立式加工中心规划，每个主加工方向对应一次候选装夹。",
            "STEP 模型单位为毫米。",
            "完整薄板工艺包含外轮廓粗/精加工、桥位切除、顶面倒角和翻面底边去毛刺。",
            "高度场仅计算 +Z 平底刀材料去除；倒角及翻面工序保留完整原生刀路并单独回放。",
        ],
        safety=resolved_safety,
        estimated_minutes=estimated_minutes,
        automation_status=automation_status,
        blocking_reasons=blocking_reasons,
    )
    plan.coverage = evaluate_plan_coverage(analysis, plan)
    return plan
