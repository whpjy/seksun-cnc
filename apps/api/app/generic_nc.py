"""Deterministic, review-only Mitsubishi 700/70 (G-code list 3) postprocessor.

This does not register a certified L32 post or change any production gate.
Turning uses diameter X and the rotational profile's Z. Milling uses source
STEP Cartesian XYZ, in separate files. No machine-specific transfer is guessed.
"""
from __future__ import annotations

import hashlib
import json
from math import isfinite
from typing import Callable

from cam.providers.turning import SUPPORTED_OPERATION_TYPES, TurningContext, TurningProvider
from .agent.l32_operation_trial import _profile_for_operation, _scoped_trial_profile
from .l32_indexed_pocket import build_indexed_back_pocket_draft
from .l32_side_milling import build_l32_exterior_clear_draft
from .models import JobResponse, Operation
from .rotational_features import RotationalFeatureAnalysis, clip_rotational_profile
from .toolpath_ir import ToolpathProgram

VERSION = "mitsubishi-700-70-list3-generic-v1"
NOTICE = "通用程序，未完成实机适配，不可直接上机；刀号、零点、主副轴同步及装夹动作尚未验证。"
MANUAL_URL = "https://www.mitsubishielectric-cnc.com/wp-content/uploads/2021/02/700-70-Series-Programming-Manual-Lathe-System.pdf"


def signature(job: JobResponse, rotational: dict | None) -> str:
    payload = {"post": VERSION, "plan": job.plan.model_dump(mode="json") if job.plan else None,
               "analysis": job.analysis.model_dump(mode="json") if job.analysis else None,
               "rotational": rotational, "machine": job.machine_configuration_hash}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value: object, *, positive: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
        raise ValueError("坐标或加工参数不是有效有限数值")
    if positive and value <= 0:
        raise ValueError("进给、转速和切削步距必须大于零")
    if positive and value < 0.0000005:
        raise ValueError("参数小于通用程序的输出精度")
    return f"{value:.6f}".rstrip("0").rstrip(".") if abs(value) >= 0.0000005 else "0"


def comment(value: object) -> str:
    # Avoid program injection and keep NC compatible with ASCII-only controllers.
    return "".join(c if 32 <= ord(c) < 127 and c not in "()%" else "_" for c in str(value))[:100]


def turning_lines(program: ToolpathProgram, tool_numbers: dict[str, int]) -> list[str]:
    if program.coordinate_convention != "diameter-x_z" or len(program.channels) != 1:
        raise ValueError("通用车削后处理要求单通道直径 X 刀路")
    lines: list[str] = []
    position: dict[str, float] = {}
    for cmd in program.channels[0].commands:
        p, kind = cmd.parameters, cmd.type
        if kind == "select_tool":
            slot = tool_numbers[str(p["tool_id"])]
            lines.append(f"T{slot:02d}{slot:02d}")
        elif kind == "set_rpm": lines.append("G97 S" + number(p["rpm"], positive=True))
        elif kind == "set_constant_surface_speed":
            lines += ["G92 S" + number(p["maximum_spindle_rpm"], positive=True),
                      "G96 S" + number(p["surface_speed_m_min"], positive=True)]
        elif kind in {"set_feed_per_revolution", "set_feed_per_minute"}:
            key = "feed_mm_rev" if kind == "set_feed_per_revolution" else "feed_mm_min"
            lines.append(("G95" if kind == "set_feed_per_revolution" else "G94") + " F" + number(p[key], positive=True))
        elif kind == "spindle_start":
            direction = p.get("direction", "clockwise")
            if direction not in {"ccw", "reverse", "counterclockwise", "cw", "clockwise", "forward"}:
                raise ValueError("主轴旋转方向不合法")
            lines.append("M04" if direction in {"ccw", "reverse", "counterclockwise"} else "M03")
        elif kind == "spindle_stop": lines.append("M05")
        elif kind in {"coolant_on", "coolant_off"}: lines.append("M08" if kind == "coolant_on" else "M09")
        elif kind in {"rapid_move", "feed_move"}:
            if set(cmd.axes) - {"X", "Z"}: raise ValueError("车削刀路包含不支持的坐标轴")
            lines.append(("G00" if kind == "rapid_move" else "G01") + " " + " ".join(a + number(cmd.axes[a]) for a in ("X", "Z") if a in cmd.axes))
            position.update(cmd.axes)
        elif kind == "thread_cut":
            if "X" not in position: raise ValueError("螺纹刀路缺少退刀直径")
            clearance = number(position["X"])
            target = number(p["target_diameter_mm"])
            start, end = number(p["start_z_mm"]), number(p["end_z_mm"])
            lines += [f"G00 X{clearance}", f"G00 Z{start}", f"G00 X{target}",
                      f"G33 Z{end} F{number(p['pitch_mm'], positive=True)}", f"G00 X{clearance}", f"G00 Z{start}"]
        elif kind == "drill_cycle":
            # Explicit peck moves avoid machine-specific canned cycle variants.
            start, end, retract = (float(p[k]) for k in ("start_z_mm", "end_z_mm", "retract_z_mm"))
            step = float(number(p["peck_depth_mm"], positive=True))
            direction = 1 if end > start else -1
            depth = start
            count = 0
            while abs(end - depth) > 1e-8:
                depth += direction * min(step, abs(end - depth))
                lines += ["G01 Z" + number(depth), "G00 Z" + number(retract)]
                if abs(end - depth) > 1e-8: lines.append("G00 Z" + number(start))
                count += 1
                if count > 10000: raise ValueError("钻孔啄钻次数超过通用程序限制")
            position["Z"] = retract
        elif kind == "cutoff": lines.append("(PART SEPARATED - NO AUTOMATIC PICKOFF)")
        else:
            raise ValueError(f"通用后处理尚不支持指令 {kind}，未输出该工序")
    if not any(line.startswith(("G01 ", "G33 ")) for line in lines):
        raise ValueError("刀路没有可输出的切削运动")
    return lines


def wrap(lines: list[str], *, program_no: int, operation: Operation, frame: str) -> str:
    header = ["%", f"O{program_no:04d}", "(GENERIC REVIEW ONLY - NOT FOR MACHINE USE)",
              "(MITSUBISHI 700-70 G CODE LIST 3 - CMDTYP 4)",
              f"(OPERATION {comment(operation.id)} TYPE {comment(operation.type)})",
              f"(FRAME {frame})", "(TOOL NUMBERS AND M03 M04 M05 M08 M09 ARE GENERIC ASSUMPTIONS)",
              "(NO CHUCK BAR FEED PICKOFF OR CHANNEL SYNCHRONIZATION)",
              "G21 G90 G40 G80", "G18" if frame == "DIAMETER_X_PROFILE_Z" else "G17"]
    return "\n".join(header + lines + ["M09", "M05", "M30", "%", ""])


def build_programs(job: JobResponse, rotational_payload: dict | None,
                   exterior_faces: Callable[[], list[dict]] | None = None) -> tuple[dict, dict[str, str]]:
    if job.plan is None: raise ValueError("当前任务尚未生成工艺方案")
    operations = [op for setup in job.plan.setups for op in sorted(setup.operations, key=lambda o: o.sequence) if op.enabled]
    if not operations: raise ValueError("当前方案没有启用的加工工序")
    if len(operations) > 999: raise ValueError("工序数量超过通用程序限制")
    tools = list(dict.fromkeys(op.tool.id for op in operations))
    if len(tools) > 99: raise ValueError("通用 T 刀号格式最多支持 99 把刀")
    mapping = {tool: i + 1 for i, tool in enumerate(tools)}
    rotational = RotationalFeatureAnalysis.model_validate(rotational_payload) if rotational_payload else None
    files: dict[str, str] = {}
    generated, missing = [], []
    for index, original in enumerate(operations, 1):
        op = original.model_copy(deep=True)
        assumptions: list[str] = []
        try:
            if op.type in SUPPORTED_OPERATION_TYPES:
                stock_radius = float(job.plan.stock["diameter_mm"]) / 2
                profile = _profile_for_operation(op, rotational) if rotational else None
                if op.type in {"turn_od_roughing", "turn_od_finishing", "turn_id_roughing", "turn_id_finishing"} and profile is None:
                    raise ValueError("缺少工序绑定的回转轮廓，请先识别并绑定制造特征")
                if profile:
                    if profile.review_state != "accepted": assumptions.append("轮廓尚未正式确认，按当前几何生成草案")
                    profile = profile.model_copy(update={"review_state": "accepted"})
                    profile = _scoped_trial_profile(op, profile, rotational, stock_radius)
                    if "profile_z_min_mm" in op.parameters or "profile_z_max_mm" in op.parameters:
                        profile = clip_rotational_profile(profile, float(op.parameters.get("profile_z_min_mm", profile.points[0].z)),
                                                          float(op.parameters.get("profile_z_max_mm", profile.points[-1].z)))
                    if op.type == "turn_grooving":
                        op.parameters["exact_groove_envelope"] = True
                        feature = next((f for f in rotational.features if f.id in op.feature_ids and f.kind == "external_groove_candidate"), None)
                        if feature:
                            op.parameters.setdefault("z_mm", (feature.z_start + feature.z_end) / 2)
                            op.parameters.setdefault("final_diameter_mm", 2 * min(feature.radius_start, feature.radius_end))
                            op.parameters.setdefault("groove_depth_mm", feature.depth_mm)
                # Preserve supplied cutting data; explicit draft defaults only if absent.
                defaults = {"spindle_mode": "constant_rpm", "spindle_rpm": 1000,
                            "cutting_speed_m_min": 80, "maximum_spindle_rpm": 3000, "feed_per_revolution_mm": 0.05}
                for key, value in defaults.items():
                    if key not in op.parameters:
                        op.parameters[key] = value
                        assumptions.append(f"缺省 {key}={value}")
                channel = op.channel_id or op.spindle_id or "main"
                initial_bore = float(op.parameters.get("initial_bore_radius_mm", 0))
                if op.type in {"turn_id_roughing", "turn_id_finishing"} and initial_bore <= 0:
                    initial_bore = min(point.radius for point in profile.points) - 0.1
                    assumptions.append("内孔初始半径按当前轮廓最小半径减 0.1 mm 估算")
                direction = str(op.parameters.get("cut_direction", "negative_z"))
                if direction not in {"negative_z", "positive_z"}: raise ValueError("切削方向不合法")
                context = TurningContext(machine_snapshot_hash=signature(job, rotational_payload), stock_radius_mm=stock_radius,
                                         initial_bore_radius_mm=initial_bore, radial_clearance_mm=0.05 if initial_bore > 0 else 2,
                                         channel_id=channel, cut_direction=direction)
                program = TurningProvider().generate(op, context, profile)
                lines = turning_lines(program, mapping)
                frame = "DIAMETER_X_PROFILE_Z"
            elif op.type in {"pocket_roughing", "pocket_finishing", "live_tool_contour_roughing", "live_tool_contour_finishing"}:
                if job.analysis is None: raise ValueError("缺少几何分析")
                diameter = float(number(op.tool.diameter_mm, positive=True))
                step = float(op.parameters.get("step_down_mm") or 0.3)
                over = float(op.parameters.get("step_over_mm") or diameter * float(op.parameters.get("step_over_percent") or 35) / 100)
                if op.type.startswith("pocket_"):
                    features = [f for f in job.analysis.prismatic_features if f.id in op.feature_ids]
                    if not features: raise ValueError("缺少工序绑定的型腔特征")
                    drafts = [build_indexed_back_pocket_draft(job.analysis, f, tool_diameter_mm=diameter,
                              depth_step_mm=step, stepover_mm=over) for f in features]
                else:
                    if exterior_faces is None: raise ValueError("缺少非回转外形刀路")
                    faces = exterior_faces()
                    if not faces: raise ValueError("未找到非回转外形侧面")
                    # Only use faces bound to this operation, never all CAD faces.
                    bound_faces = set(op.feature_ids)
                    for feature in job.analysis.planar_machining_features:
                        if feature.id in op.feature_ids: bound_faces.update(feature.source_face_ids)
                    selected = [f for f in faces if str(f.get("analysis_face_id")) in bound_faces or
                                str(f.get("feature_id")) in op.feature_ids]
                    if "REGION-NONROTATIONAL-OUTER-1" in op.feature_ids:
                        selected = faces
                        assumptions.append("非回转外形区域使用已提取的相对侧面清料刀路")
                    if not selected: raise ValueError("外形工序的特征与可用侧面刀路不匹配")
                    drafts = [build_l32_exterior_clear_draft(f, stock_radius_mm=float(job.plan.stock["diameter_mm"]) / 2,
                              access_sign=1 if f["normal_y"] > 0 else -1, tool_diameter_mm=diameter,
                              radial_step_mm=step, stepover_mm=over,
                              silhouette_clearance_mm=float(op.parameters.get("silhouette_clearance_mm", 0.18))) for f in selected]
                rpm = op.parameters.get("spindle_rpm", 1000)
                feed = op.parameters.get("feed_rate_mm_min", op.parameters.get("feed_mm_min", 100))
                assumptions.append("铣削 XYZ 使用 STEP 模型坐标，尚未转换为机床坐标；通用主轴/刀号为示意")
                if "spindle_rpm" not in op.parameters: assumptions.append("缺省铣削转速 1000 rpm")
                if "feed_mm_min" not in op.parameters and "feed_rate_mm_min" not in op.parameters: assumptions.append("缺省铣削进给 100 mm/min")
                slot = mapping[op.tool.id]
                lines = [f"T{slot:02d}{slot:02d}", "G97 S" + number(rpm, positive=True), "M03", "G94 F" + number(feed, positive=True)]
                for draft in drafts:
                    for move in draft.moves:
                        lines.append(("G00" if move.kind == "rapid" else "G01") + " " + " ".join(
                            a.upper() + number(getattr(move.point, a)) for a in ("x", "y", "z")))
                if not any(line.startswith("G01 ") for line in lines): raise ValueError("没有有效铣削刀路")
                frame = "SOURCE_STEP_XYZ_MM"
                channel = op.channel_id or "main"
            else:
                raise ValueError(f"尚不支持 {op.type} 的通用程序生成")
            filename = f"generic-{index:03d}-{channel}.nc"
            content = wrap(lines, program_no=1000 + index, operation=op, frame=frame)
            files[filename] = content
            generated.append({"filename": filename, "operation_id": op.id, "name": op.name, "channel": channel,
                              "coordinate_frame": frame, "line_count": len(content.splitlines()), "assumptions": assumptions,
                              "sha256": hashlib.sha256(content.encode()).hexdigest()})
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            missing.append({"operation_id": op.id, "name": op.name, "reason": str(error)})
    return {"schema_version": "1.0.0", "postprocessor": VERSION, "production_ready": False,
            "notice": NOTICE, "manual_url": MANUAL_URL, "status": "generated" if files and not missing else "partial" if files else "blocked",
            "operation_count": len(operations), "generated_count": len(generated), "programs": generated, "missing": missing,
            "tool_mapping": [{"tool_id": t, "tool_number": n, "offset_number": n} for t, n in mapping.items()],
            "source_signature": signature(job, rotational_payload)}, files
