from __future__ import annotations

from math import dist

from .catalogs import resolve_machine
from .models import ProcessPlan


def verify_cam(plan: ProcessPlan, cam_result: dict) -> dict[str, object]:
    machine = plan.machine_profile or resolve_machine(plan.machine)
    operations = {operation.id: operation for setup in plan.setups for operation in setup.operations}
    segments = cam_result.get("preview_segments", [])
    checks: list[dict[str, str]] = []
    warnings: list[str] = list(cam_result.get("skipped", []))
    errors: list[str] = []

    points = [
        (float(segment[key_x]), float(segment[key_y]), float(segment[key_z]))
        for segment in segments
        for key_x, key_y, key_z in (("x1", "y1", "z1"), ("x2", "y2", "z2"))
    ]
    if points:
        minimum = [min(point[index] for point in points) for index in range(3)]
        maximum = [max(point[index] for point in points) for index in range(3)]
        extent = [maximum[index] - minimum[index] for index in range(3)]
    else:
        minimum = maximum = extent = [0.0, 0.0, 0.0]
        errors.append("刀路不包含可验证的直线运动")

    travel_ok = all(extent[index] <= machine.travel_mm[index] + 1e-6 for index in range(3))
    checks.append({
        "id": "machine_travel",
        "status": "passed" if travel_ok else "failed",
        "message": f"刀路包络 {extent[0]:.1f} × {extent[1]:.1f} × {extent[2]:.1f} mm；机床行程 {' × '.join(str(value) for value in machine.travel_mm)} mm",
    })
    if not travel_ok:
        errors.append("刀路包络超过目标机床行程")

    parameter_ok = True
    tool_ok = True
    for operation in operations.values():
        spindle = float(operation.parameters.get("spindle_rpm", 0))
        feed = float(operation.parameters.get("feed_rate_mm_min", 0))
        if spindle <= 0 or spindle > machine.max_spindle_rpm or feed <= 0 or feed > machine.max_feed_mm_min:
            parameter_ok = False
        if operation.tool.diameter_mm > machine.max_tool_diameter_mm:
            tool_ok = False
    checks.append({"id": "cutting_parameters", "status": "passed" if parameter_ok else "failed", "message": "所有转速与进给均在机床参数范围内" if parameter_ok else "存在缺失或超出机床范围的切削参数"})
    checks.append({"id": "tool_diameter", "status": "passed" if tool_ok else "failed", "message": f"刀具直径未超过机床上限 Ø{machine.max_tool_diameter_mm:g} mm" if tool_ok else "存在超过机床上限的刀具"})
    if not parameter_ok:
        errors.append("切削参数安全校验失败")
    if not tool_ok:
        errors.append("刀具直径安全校验失败")

    drilling_operations = [operation for operation in operations.values() if operation.type == "drilling"]
    through_depth_ok = all(
        float(operation.parameters.get("depth_mm", 0)) + 1e-6
        >= float(operation.parameters.get("feature_depth_mm", operation.parameters.get("depth_mm", 0)))
        + float(operation.parameters.get("drill_tip_length_mm", 0))
        + float(operation.parameters.get("breakthrough_mm", 0))
        for operation in drilling_operations
    )
    checks.append({
        "id": "through_hole_depth",
        "status": "passed" if through_depth_ok else "failed",
        "message": "通孔深度已包含钻尖长度和穿透余量" if through_depth_ok else "存在未覆盖钻尖长度或穿透余量的通孔",
    })
    if not through_depth_ok:
        errors.append("通孔编程深度不足")

    generated_post = str(cam_result.get("postprocessor", ""))
    post_configured = bool(machine.postprocessor)
    post_ok = post_configured and generated_post == machine.postprocessor
    checks.append({
        "id": "postprocessor_compatibility",
        "status": "passed" if post_ok else "warning",
        "message": (
            f"后处理器 {generated_post} 与机床配置一致"
            if post_ok
            else f"当前输出为 {generated_post or '未知'} 草案；目标机床控制器尚未配置，禁止直接上机"
        ),
    })
    if not post_ok:
        warnings.append("目标机床控制器/后处理器尚未绑定")

    generated = set(cam_result.get("generated_operations", []))
    expected = {operation.id for operation in operations.values()}
    coverage_ok = generated == expected
    checks.append({
        "id": "operation_coverage",
        "status": "passed" if coverage_ok else "warning",
        "message": f"已生成 {len(generated)}/{len(expected)} 道批准工序",
    })
    if not coverage_ok:
        warnings.append("部分已批准工序未生成刀路")

    cycle_seconds = len(generated) * 20.0
    for segment in segments:
        length = dist(
            (float(segment["x1"]), float(segment["y1"]), float(segment["z1"])),
            (float(segment["x2"]), float(segment["y2"]), float(segment["z2"])),
        )
        operation = operations.get(str(segment.get("operation_id")))
        if segment.get("motion") == "rapid":
            rate = machine.max_feed_mm_min
        else:
            axial_move = abs(float(segment["z2"]) - float(segment["z1"]))
            planar_move = dist(
                (float(segment["x1"]), float(segment["y1"])),
                (float(segment["x2"]), float(segment["y2"])),
            )
            parameter = "plunge_rate_mm_min" if axial_move > planar_move else "feed_rate_mm_min"
            rate = float(operation.parameters.get(parameter, 1)) if operation else 1.0
        cycle_seconds += length / max(rate, 1) * 60

    status = "failed" if errors else "warning" if warnings else "passed"
    return {
        "schema_version": "0.8.0",
        "engine": "Seksun CNC static preflight",
        "status": status,
        "machine": machine.model_dump(mode="json"),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "minimum_mm": dict(zip(("x", "y", "z"), (round(value, 3) for value in minimum))),
            "maximum_mm": dict(zip(("x", "y", "z"), (round(value, 3) for value in maximum))),
            "extent_mm": dict(zip(("x", "y", "z"), (round(value, 3) for value in extent))),
            "estimated_cycle_minutes": round(cycle_seconds / 60, 2),
            "generated_operation_count": len(generated),
        },
        "limitations": [
            "静态预检不等于材料去除仿真。",
            "侧向 Setup 已转换为各自局部 +Z 工作坐标；换装夹与工件坐标重设必须人工执行。",
        ],
    }
