"""Explain reference-check blockers without granting manufacturing acceptance."""
from math import isfinite

TOLERANCE_MM3 = .0001  # numerical threshold, never a drawing tolerance


def side_hole_readiness(result, draft, machine, context):
    items = []

    def add(code, status, detail, action, feature_id=None):
        items.append(dict(code=code, status=status, detail=detail, action=action,
                          **({"feature_id": feature_id} if feature_id else {})))

    def metric(key):
        value = result.get(key)
        return float(value) if isinstance(value, (float, int)) and isfinite(value) and value >= 0 else None

    for key, code, detail, action in [
        ("initial_missing_target_mm3", "previous_target_damage", "起始材料未包含的成品体积", "核对毛坯位置和尺寸、前序过切及材料记录；先修正前序再重算侧孔"),
        ("target_contact_mm3", "cutting_target_contact", "本次切削体侵入成品体积", "核对图纸孔底、CAD 版本和实际钻尖；调整钻深或更换加工步骤后重算"),
        ("remaining_cylindrical_segments_mm3", "selected_hole_residual", "所选 CAD 圆柱段残料", "逐孔核对钻深与实刀几何，避免仅回退钻深消除过切而留下残料"),
    ]:
        value = metric(key)
        add(code, "review" if value is None else "blocked" if value > TOLERANCE_MM3 else "passed",
            f"{detail}：{value:.6f} mm³" if value is not None else f"{detail}：缺少有效检查结果", action)

    for stage in result.get("stages", []):
        residual = stage.get("remaining_cylindrical_segment_mm3")
        conflict = stage.get("target_inside_cylindrical_segment_mm3")
        if isinstance(conflict, (int, float)) and isfinite(conflict) and conflict > TOLERANCE_MM3:
            add("cad_region_conflict", "blocked", f"圆柱段中仍有 STEP 成品实体 {conflict:.6f} mm³",
                "核对孔特征范围和图纸与模型版本；不能直接将这一段全部钻空", stage["feature_id"])
        if isinstance(residual, (int, float)) and isfinite(residual) and residual > TOLERANCE_MM3:
            add("hole_residual", "blocked", f"最终圆柱段残料 {residual:.6f} mm³",
                "核对该孔的钻深、孔底和后续精加工步骤", stage["feature_id"])

    if not draft.get("drill_geometry"):
        add("drill_geometry", "review", "当前仅检查圆柱切削体", "提供实际钻尖角与钻深依据，加入钻尖检查")
    clearance = result.get("non_cutting_clearance_status", "not_run")
    add("tool_body_clearance", "blocked" if clearance == "failed" else "passed" if clearance == "passed" else "review",
        "刀杆与刀柄轴向进给干涉检查" if clearance != "not_run" else "尚未检查刀杆与刀柄",
        "干涉时核对伸出长度和刀柄尺寸；未检查时补齐实刀尺寸；孔间移动仍需单独验证")
    for hole in draft["holes"]:
        if hole.get("flute_reach_status") == "review":
            add("flute_reach", "review", "刃长低于包含空行程的保守长度上限",
                "核对实际壁厚、有效刃长和排屑条件", hole["feature_id"])
    for check in machine["checks"]:
        if check["status"] != "passed":
            add("machine_" + check["code"], "blocked" if check["status"] == "failed" else "review",
                check["detail"], "补齐对应机床、刀位、坐标、行程或夹具证据后重新验证")
    if context["stock_source"] == "initial_solid_blank":
        add("process_sequence", "review", "本次从初始毛坯开始，没有已接受前序",
            "先确定实际工序顺序；该试算不代表已经车削或钻孔的整件状态")
    elif context['stock_source']=='accepted_turning_envelope':
        add("continuous_solid", "review", "前序材料采用车削采样包络保守重建",
            "正式接受前接入可延续的连续实体材料记录")
    else:
        add('continuous_solid','passed','继承已接受且哈希匹配的连续实体材料','继续保留材料哈希与前序绑定')
    excess = metric("remaining_excess_stock_mm3")
    add("whole_part_geometry", "review", f"整件相对 STEP 仍有多余材料 {excess:.6f} mm³" if excess is not None else "尚缺整件多余材料结果",
        "逐项规划其余特征；本次侧孔检查不能证明整件制造目标全部覆盖")
    add("drawing_quality", "review", "孔口、孔底、公差、粗糙度及检测证据尚未由本次检查验证",
        "核对二维要求，补充对应精加工与检验步骤")
    return {"status": "blocked" if any(i["status"] == "blocked" for i in items) else "review",
            "checks": items, "can_accept": False, "production_ready": False,
            "scope": "reference_check_readiness", "numerical_contact_threshold_mm3": TOLERANCE_MM3}
