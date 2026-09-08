from __future__ import annotations

from math import pi

from .models import MachineProfile, MaterialProfile, Operation, Tool
from .operation_library import operation_library_payload


MATERIALS = [
    MaterialProfile(id="al-6061-t6", name="6061-T6 铝合金", milling_speed_m_min=300, drilling_speed_m_min=100, mill_feed_per_tooth_mm=0.06, drill_feed_per_rev_mm=0.15),
    MaterialProfile(id="al-7075-t6", name="7075-T6 铝合金", milling_speed_m_min=240, drilling_speed_m_min=80, mill_feed_per_tooth_mm=0.05, drill_feed_per_rev_mm=0.12),
    MaterialProfile(id="s45c", name="S45C", milling_speed_m_min=120, drilling_speed_m_min=25, mill_feed_per_tooth_mm=0.04, drill_feed_per_rev_mm=0.10),
    MaterialProfile(id="sus304", name="SUS304", milling_speed_m_min=70, drilling_speed_m_min=18, mill_feed_per_tooth_mm=0.025, drill_feed_per_rev_mm=0.08),
]

MACHINES = [
    MachineProfile(id="vmc-850", name="三轴立式加工中心", axes=3, travel_mm=[800, 500, 500], max_spindle_rpm=10000, max_feed_mm_min=10000, max_tool_diameter_mm=80, postprocessor=None),
    MachineProfile(id="vmc-850-4a", name="四轴加工中心", axes=4, travel_mm=[800, 500, 500], max_spindle_rpm=12000, max_feed_mm_min=12000, max_tool_diameter_mm=80, postprocessor=None),
    MachineProfile(id="u500-5x", name="五轴加工中心", axes=5, travel_mm=[600, 500, 450], max_spindle_rpm=18000, max_feed_mm_min=15000, max_tool_diameter_mm=63, postprocessor=None),
]

TOOL_DEFINITIONS = [
    {"id": "FM-50", "name": "Ø50 面铣刀", "kind": "face_mill", "diameter_mm": 50.0, "flute_count": 5, "max_rpm": 8000, "flute_length_mm": 8, "stickout_mm": 20, "holder_diameter_mm": 40},
    {"id": "CM-6-90", "name": "Ø6 90°倒角刀", "kind": "chamfer_mill", "diameter_mm": 6.0, "flute_count": 3, "max_rpm": 12000, "flute_length_mm": 8, "stickout_mm": 20, "holder_diameter_mm": 20},
    *[
        {"id": f"DRILL-{diameter:.1f}", "name": f"Ø{diameter:.1f} 麻花钻", "kind": "drill", "diameter_mm": diameter, "flute_count": 2, "max_rpm": 12000, "flute_length_mm": max(15, diameter * 5), "stickout_mm": max(30, diameter * 6), "holder_diameter_mm": 25}
        for diameter in (2.0, 3.0, 4.0, 5.0, 6.0, 6.5, 7.0, 8.0, 10.0, 12.0, 16.0, 20.0)
    ],
    *[
        {"id": f"EM-{diameter}", "name": f"Ø{diameter} 平底立铣刀", "kind": "end_mill", "diameter_mm": float(diameter), "flute_count": 3, "max_rpm": 16000, "flute_length_mm": max(12, diameter * 2.5), "stickout_mm": max(25, diameter * 3), "holder_diameter_mm": 32}
        for diameter in (2, 3, 4, 6, 8, 10, 12, 16)
    ],
]


def resolve_material(value: str) -> MaterialProfile:
    normalized = value.strip().lower()
    for profile in MATERIALS:
        aliases = {profile.id.lower(), profile.name.lower(), profile.id.replace("al-", "").lower()}
        if normalized in aliases or any(alias in normalized for alias in aliases):
            return profile.model_copy(deep=True)
    return MATERIALS[0].model_copy(deep=True)


def resolve_machine(value: str) -> MachineProfile:
    normalized = value.strip().lower()
    for profile in MACHINES:
        if normalized in {profile.id.lower(), profile.name.lower()}:
            return profile.model_copy(deep=True)
    if "5" in normalized or "五轴" in value:
        return MACHINES[2].model_copy(deep=True)
    if "4" in normalized or "四轴" in value:
        return MACHINES[1].model_copy(deep=True)
    return MACHINES[0].model_copy(deep=True)


def enrich_tool(tool: Tool) -> Tool:
    match = min(
        (item for item in TOOL_DEFINITIONS if item["kind"] == tool.kind),
        key=lambda item: abs(float(item["diameter_mm"]) - tool.diameter_mm),
        default=None,
    )
    if match and abs(float(match["diameter_mm"]) - tool.diameter_mm) < 0.01:
        return Tool.model_validate(match)
    tool.flute_count = 2 if tool.kind == "drill" else 3
    tool.max_rpm = 10000
    tool.catalog_match = False
    tool.flute_length_mm = max(12, tool.diameter_mm * 2.5)
    tool.stickout_mm = max(25, tool.diameter_mm * 3)
    tool.holder_diameter_mm = 32
    return tool


def get_tool(tool_id: str) -> Tool:
    match = next((item for item in TOOL_DEFINITIONS if item["id"] == tool_id), None)
    if match is None:
        raise ValueError(f"未知刀具: {tool_id}")
    return Tool.model_validate(match)


def apply_cutting_parameters(operation: Operation, material: MaterialProfile, machine: MachineProfile) -> list[str]:
    operation.tool = enrich_tool(operation.tool)
    tool = operation.tool
    is_drill = tool.kind == "drill"
    cutting_speed = material.drilling_speed_m_min if is_drill else material.milling_speed_m_min
    calculated_rpm = cutting_speed * 1000 / (pi * tool.diameter_mm)
    spindle_rpm = min(calculated_rpm, machine.max_spindle_rpm, tool.max_rpm)
    if is_drill:
        calculated_feed = spindle_rpm * material.drill_feed_per_rev_mm
    else:
        calculated_feed = spindle_rpm * tool.flute_count * material.mill_feed_per_tooth_mm
    feed = min(calculated_feed, machine.max_feed_mm_min)
    operation.parameters.update({
        "cutting_speed_m_min": round(cutting_speed, 1),
        "spindle_rpm": int(round(spindle_rpm)),
        "feed_rate_mm_min": int(round(feed)),
        "plunge_rate_mm_min": int(round(feed if is_drill else max(60, feed * 0.3))),
        "coolant": operation.parameters.get("coolant", "flood"),
    })
    warnings: list[str] = []
    if spindle_rpm + 0.5 < calculated_rpm:
        warnings.append(f"{operation.id} 转速已按刀具/机床上限从 {calculated_rpm:.0f} 限制为 {spindle_rpm:.0f} rpm")
    if calculated_feed > machine.max_feed_mm_min:
        warnings.append(f"{operation.id} 进给已限制为机床上限 {machine.max_feed_mm_min:.0f} mm/min")
    if not tool.catalog_match:
        warnings.append(f"{operation.id} 使用临时刀具定义 {tool.name}，上机前需录入真实刀具参数")
    return warnings


def catalog_payload() -> dict[str, object]:
    return {
        "schema_version": "0.8.0",
        "materials": [item.model_dump(mode="json") for item in MATERIALS],
        "machines": [item.model_dump(mode="json") for item in MACHINES],
        "tools": TOOL_DEFINITIONS,
        "operations": operation_library_payload()["definitions"],
    }
