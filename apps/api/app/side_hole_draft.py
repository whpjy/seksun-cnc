"""Source-coordinate side-hole centerlines. These are not machining verification."""
from math import sqrt, isfinite


def _vector(value):
    result = [float(value[k]) for k in "xyz"]
    if not all(isfinite(v) for v in result):
        raise ValueError("孔位坐标无效")
    return result


def _unit(value):
    result = _vector(value)
    length = sqrt(sum(v*v for v in result))
    if length < 1e-9:
        raise ValueError("轴线方向无效")
    return [v/length for v in result]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def build_side_hole_draft(features: list[dict], axis: dict, stock_diameter_mm: float,
                          *, tool_diameter_mm: float | None = None, clearance_mm: float = 1.0) -> dict:
    if not features or len({f["id"] for f in features}) != len(features):
        raise ValueError("请选择不同的侧向孔特征")
    if not isfinite(stock_diameter_mm) or stock_diameter_mm <= 0 or not isfinite(clearance_mm) or clearance_mm <= 0:
        raise ValueError("毛坯直径和安全距离必须大于零")
    if tool_diameter_mm is not None and (not isfinite(tool_diameter_mm) or tool_diameter_mm <= 0):
        raise ValueError("刀具直径必须大于零")
    if axis.get("review_state") == "excluded":
        raise ValueError("主轴方向已排除")
    spindle, origin = _unit(axis["direction"]), _vector(axis["origin"])
    holes = []
    for feature in features:
        if feature.get("kind") != "hole" or feature.get("review_state") != "accepted" or feature.get("angular_span_degrees", 360) < 359.9:
            raise ValueError(f'{feature["id"]} 不是已确认的完整圆柱孔')
        direction, center = _unit(feature["axis"]), _vector(feature["center"])
        if abs(_dot(spindle, direction)) > .001:
            raise ValueError("当前草案仅支持孔轴与主轴垂直的侧向孔；斜孔和轴向孔需其他加工方式")
        length, diameter = float(feature["length"]), float(feature["diameter"])
        if not all(isfinite(v) and v > 0 for v in (length, diameter)):
            raise ValueError("孔直径和圆柱段长度必须大于零")
        tool = tool_diameter_mm if tool_diameter_mm is not None else diameter
        if tool > diameter + 1e-6:
            raise ValueError("刀具直径大于 CAD 孔名义直径，请先审核图纸公差与目标几何")
        delta = [c-o for c, o in zip(center, origin)]
        axial_position = _dot(delta, spindle)
        radial = [d-axial_position*s for d, s in zip(delta, spindle)]
        # Choose the nearby exterior, keeping the real offset from the spindle.
        if _dot(radial, direction) < 0:
            direction = [-d for d in direction]
        along = _dot(radial, direction)
        offset_squared = max(0., _dot(radial, radial)-along*along)
        # Account for the cutter radius: the entire drill must clear the stock.
        entry_radius = stock_diameter_mm/2 + tool/2 + clearance_mm
        exterior_t = sqrt(max(0., entry_radius*entry_radius-offset_squared))-along
        if offset_squared >= (stock_diameter_mm/2)**2 or exterior_t <= length/2:
            raise ValueError("孔段位置与当前毛坯不一致，请核对主轴和毛坯尺寸")
        def point(t):
            return dict(zip("xyz", [c+t*d for c, d in zip(center, direction)]))
        entry, end = point(exterior_t), point(-length/2)
        holes.append({"feature_id": feature["id"], "diameter_mm": diameter, "tool_diameter_mm": tool,
                      "axis_offset_mm": sqrt(offset_squared), "axial_position_mm": axial_position,
                      "requires_transverse_positioning": offset_squared > .05**2,
                      "outward_direction": dict(zip("xyz", direction)), "entry": entry, "end": end,
                      "centerline_feed_length_mm": exterior_t+length/2, "cylindrical_segment_length_mm": length,
                      "moves": [{"kind": "rapid", "point": entry}, {"kind": "feed", "point": end},
                                {"kind": "rapid", "point": entry}]})
    return {"schema_version": "1.0.0", "coordinate_frame": "source_step_xyz_mm", "reference_only": True,
            "nc_generated": False, "can_accept": False, "verification_status": "not_run", "holes": holes,
            "requirements": ["分度定位与动力钻削能力", "偏置孔所需的横向定位、行程和刀位能力", "装夹与刀柄可达性"],
            "limitations": ["仅为按 STEP 孔轴计算的中心线草案，尚未做实体材料去除、目标保护或夹具碰撞检查",
                            "圆柱段终点不是完整钻削深度；需审核钻尖长度、穿透及盲孔底形状",
                            "不含切削参数、机床坐标转换或 G 代码；不能作为已接受工序或孔覆盖证据"]}
