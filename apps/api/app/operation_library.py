from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import Operation, Tool


ParameterValue = float | int | str | bool


class ParameterDefinition(BaseModel):
    key: str
    label: str
    type: Literal["number", "integer", "boolean", "enum"]
    group: Literal["geometry", "cutting", "non_cutting", "strategy"] = "strategy"
    unit: str | None = None
    required: bool = False
    default: ParameterValue | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = Field(default_factory=list)


class GeometryRequirement(BaseModel):
    accepts: list[str]
    minimum_selection: int = 1
    maximum_selection: int | None = None


class ToolRequirement(BaseModel):
    accepts: list[str]
    default_tool_id: str


class EngineBinding(BaseModel):
    provider: Literal["freecad", "opencamlib", "turning"]
    operation: str
    modifiers: list[str] = Field(default_factory=list)


class OperationDefinition(BaseModel):
    id: str
    version: int = 1
    name: str
    category: str
    description: str
    maturity: Literal["planned", "experimental", "generated", "validated", "production"]
    manual_enabled: bool
    geometry: GeometryRequirement
    tool: ToolRequirement
    parameters: list[ParameterDefinition]
    engine: EngineBinding


def number(key: str, label: str, default: float, unit: str = "mm", minimum: float = 0,
           maximum: float | None = None, group: str = "strategy", required: bool = False) -> ParameterDefinition:
    return ParameterDefinition(key=key, label=label, type="number", default=default, unit=unit,
                               minimum=minimum, maximum=maximum, group=group, required=required)


def enum(key: str, label: str, default: str, choices: list[str], group: str = "strategy") -> ParameterDefinition:
    return ParameterDefinition(
        key=key, label=label, type="enum", default=default, choices=choices, group=group,
    )


COMMON_DEPTH = [
    number("depth_mm", "加工深度", 1.0, minimum=0.01, group="geometry", required=True),
    number("step_down_mm", "每层切深", 1.0, minimum=0.01, group="cutting"),
]
COMMON_MILLING = [
    number("spindle_rpm", "主轴转速", 8000, unit="rpm", minimum=1, group="cutting"),
    number("feed_rate_mm_min", "切削进给", 600, unit="mm/min", minimum=1, group="cutting"),
    number("plunge_rate_mm_min", "下刀进给", 180, unit="mm/min", minimum=1, group="cutting"),
]
COMMON_TURNING = [
    enum("spindle_mode", "主轴模式", "constant_surface_speed", ["constant_surface_speed", "constant_rpm"], "cutting"),
    number("cutting_speed_m_min", "切削速度", 100, unit="m/min", minimum=1, group="cutting"),
    number("maximum_spindle_rpm", "最高主轴转速", 8000, unit="rpm", minimum=1, group="cutting"),
    number("feed_per_revolution_mm", "每转进给", 0.12, unit="mm/rev", minimum=0.001, group="cutting"),
]


def definition(
    identifier: str, name: str, category: str, description: str, operation: str,
    accepts: list[str], tool_kinds: list[str], default_tool: str,
    parameters: list[ParameterDefinition], maturity: str = "generated",
    manual_enabled: bool = True, modifiers: list[str] | None = None,
    provider: Literal["freecad", "opencamlib", "turning"] = "freecad",
) -> OperationDefinition:
    return OperationDefinition(
        id=identifier, name=name, category=category, description=description,
        maturity=maturity, manual_enabled=manual_enabled,
        geometry=GeometryRequirement(accepts=accepts),
        tool=ToolRequirement(accepts=tool_kinds, default_tool_id=default_tool),
        parameters=parameters,
        engine=EngineBinding(provider=provider, operation=operation, modifiers=modifiers or []),
    )


OPERATION_DEFINITIONS = [
    definition("face_milling", "面铣", "平面加工", "加工基准面或毛坯顶面。", "MillFace",
               ["planar_face"], ["face_mill", "end_mill"], "FM-50",
               [number("stock_allowance_mm", "轴向余量", 0.0), number("step_down_mm", "每层切深", 0.5, minimum=0.01), *COMMON_MILLING], "validated"),
    definition("profile_contouring", "轮廓铣", "轮廓加工", "沿闭合边链加工内外轮廓。", "Profile",
               ["planar_face", "closed_edge_chain", "outer_loop"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("radial_allowance_mm", "径向余量", 0.0), *COMMON_MILLING], "validated"),
    definition("profile_roughing", "外轮廓粗铣", "轮廓加工", "分层加工外轮廓并保留精加工余量。", "Profile",
               ["planar_face", "outer_loop"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("radial_allowance_mm", "径向余量", 0.2),
                ParameterDefinition(key="tab_count", label="桥位数量", type="integer", default=0, minimum=0, maximum=20),
                number("tab_width_mm", "桥位宽度", 4.0), number("tab_height_mm", "桥位高度", 0.6), *COMMON_MILLING], "validated", modifiers=["Tag"]),
    definition("profile_finishing", "外轮廓精铣", "轮廓加工", "清除轮廓余量，可保留桥位。", "Profile",
               ["planar_face", "outer_loop"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("radial_allowance_mm", "径向余量", 0.0),
                ParameterDefinition(key="spring_pass", label="光刀", type="boolean", default=True),
                ParameterDefinition(key="tab_count", label="桥位数量", type="integer", default=0, minimum=0, maximum=20),
                number("tab_width_mm", "桥位宽度", 4.0), number("tab_height_mm", "桥位高度", 0.6), *COMMON_MILLING], "validated", modifiers=["Tag"]),
    definition("internal_profile_roughing", "异形内轮廓粗铣", "轮廓加工", "沿非圆闭合内轮廓分层切穿并保留侧壁余量。", "Profile",
               ["internal_profile", "closed_edge_chain"], ["end_mill"], "EM-3",
               [*COMMON_DEPTH, number("radial_allowance_mm", "径向余量", 0.2), *COMMON_MILLING], "generated"),
    definition("internal_profile_finishing", "异形内轮廓精铣", "轮廓加工", "精加工贯通异形孔侧壁。", "Profile",
               ["internal_profile", "closed_edge_chain"], ["end_mill"], "EM-3",
               [*COMMON_DEPTH, number("radial_allowance_mm", "径向余量", 0.0),
                ParameterDefinition(key="spring_pass", label="光刀", type="boolean", default=True), *COMMON_MILLING], "generated"),
    definition("pocket_roughing", "型腔粗加工", "型腔加工", "去除封闭型腔主体材料并保留余量。", "PocketShape",
               ["prismatic_pocket", "planar_face"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("step_over_percent", "径向步距", 45, unit="%", maximum=95),
                number("wall_allowance_mm", "侧壁余量", 0.2), number("floor_allowance_mm", "底面余量", 0.1), *COMMON_MILLING], "generated"),
    definition("pocket_finishing", "型腔精加工", "型腔加工", "精加工型腔侧壁与底面。", "PocketShape",
               ["prismatic_pocket", "planar_face"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("wall_allowance_mm", "侧壁余量", 0.0), number("floor_allowance_mm", "底面余量", 0.0),
                ParameterDefinition(key="spring_pass", label="光刀", type="boolean", default=True), *COMMON_MILLING], "generated"),
    definition("live_tool_contour_roughing", "动力刀具外形粗铣", "L32 车铣复合", "使用主轴侧动力刀具分层去除非回转外形余量。", "Profile",
               ["solid", "planar_face", "outer_loop"], ["end_mill"], "EM-1",
               [*COMMON_DEPTH, number("radial_allowance_mm", "轮廓余量", 0.15), *COMMON_MILLING], "experimental", False),
    definition("live_tool_contour_finishing", "动力刀具外形精铣", "L32 车铣复合", "沿精确侧面边界完成非回转外形精加工。", "Profile",
               ["solid", "planar_face", "outer_loop"], ["end_mill"], "EM-1",
               [*COMMON_DEPTH, number("radial_allowance_mm", "轮廓余量", 0.0),
                ParameterDefinition(key="spring_pass", label="光刀", type="boolean", default=True), *COMMON_MILLING], "experimental", False),
    definition("slot_roughing", "槽粗加工", "槽加工", "分层去除槽内材料。", "PocketShape",
               ["prismatic_slot", "planar_face"], ["end_mill"], "EM-3",
               [*COMMON_DEPTH, number("step_over_percent", "径向步距", 40, unit="%", maximum=95),
                number("wall_allowance_mm", "侧壁余量", 0.2), *COMMON_MILLING], "generated"),
    definition("slot_finishing", "槽精加工", "槽加工", "精加工槽壁与槽底。", "PocketShape",
               ["prismatic_slot", "planar_face"], ["end_mill"], "EM-3",
               [*COMMON_DEPTH, number("wall_allowance_mm", "侧壁余量", 0.0), *COMMON_MILLING], "generated"),
    definition("drilling", "钻孔", "孔加工", "加工通孔或盲孔，支持穿透和啄钻参数。", "Drilling",
               ["cylindrical_hole", "point"], ["drill"], "DRILL-6.0",
               [number("depth_mm", "编程深度", 5.0, minimum=0.01, group="geometry", required=True),
                number("breakthrough_mm", "穿透余量", 0.5, group="geometry"),
                ParameterDefinition(key="peck", label="启用啄钻", type="boolean", default=False), *COMMON_MILLING], "validated"),
    definition("helical_boring", "螺旋铣孔", "孔加工", "使用立铣刀加工大直径孔。", "Helix",
               ["cylindrical_hole", "circular_edge"], ["end_mill"], "EM-6",
               [*COMMON_DEPTH, number("finishing_allowance_mm", "精加工余量", 0.15), *COMMON_MILLING], "experimental", False),
    definition("tab_removal", "桥位切除", "轮廓加工", "二次固定后切除桥位并清理残根。", "Profile",
               ["planar_face", "outer_loop"], ["end_mill"], "EM-3",
               [*COMMON_DEPTH, number("start_depth_from_bottom_mm", "底部加工高度", 0.9),
                ParameterDefinition(key="requires_secondary_retention", label="要求二次固定", type="boolean", default=True), *COMMON_MILLING], "generated"),
    definition("edge_chamfer", "倒角与去毛刺", "边加工", "沿可达边链执行倒角或轻量去毛刺。", "Deburr",
               ["planar_face", "edge_chain", "circular_edge"], ["chamfer_mill"], "CM-6-90",
               [number("chamfer_width_mm", "倒角宽度", 0.3, minimum=0.01), number("extra_depth_mm", "附加深度", 0.05),
                number("included_angle_deg", "刀具夹角", 90, unit="°", minimum=1, maximum=179), *COMMON_MILLING], "generated"),
    definition("engraving", "雕刻", "边加工", "沿文字或开放边链雕刻。", "Engrave",
               ["edge_chain", "sketch", "internal_profile"], ["v_bit", "chamfer_mill"], "CM-6-90",
               [number("depth_mm", "雕刻深度", 0.2, minimum=0.01), *COMMON_MILLING], "generated", True),
    definition("adaptive_clearing", "自适应粗加工", "自适应加工", "保持刀具负载的区域清除策略。", "Adaptive",
               ["planar_face", "solid"], ["end_mill"], "EM-6", [*COMMON_DEPTH, *COMMON_MILLING], "planned", False),
    definition("surface_roughing", "3D曲面粗加工", "三维加工", "按目标曲面分层去除三维区域毛坯并保留轴向余量。", "Surface",
               ["surface_set", "solid"], ["end_mill", "bull_end_mill"], "EM-6",
               [number("step_down_mm", "每层切深", 1.0, minimum=0.05, group="cutting"),
                number("step_over_percent", "径向步距", 45, unit="%", minimum=5, maximum=90),
                number("depth_offset_mm", "曲面余量", 0.3, minimum=0),
                number("sample_interval_mm", "采样间距", 1.0, minimum=0.1), *COMMON_MILLING], "experimental", False),
    definition("surface_3d", "3D曲面精加工", "三维加工", "使用球头刀扫描自由曲面。", "Surface",
               ["surface_set", "solid"], ["ball_end_mill", "bull_end_mill"], "BM-4",
               [number("step_over_mm", "行距", 0.5, minimum=0.01),
                number("sample_interval_mm", "采样间距", 0.5, minimum=0.05),
                number("depth_offset_mm", "曲面余量", 0.0, minimum=0), *COMMON_MILLING], "experimental", False),
    definition("waterline", "等高/水线加工", "三维加工", "按固定Z层加工陡峭曲面。", "Waterline",
               ["surface_set", "solid"], ["ball_end_mill", "bull_end_mill", "end_mill"], "EM-6", [*COMMON_DEPTH, *COMMON_MILLING], "planned", False),
    definition("turn_facing", "车端面", "车削", "沿径向加工棒料或零件端面。", "Facing",
               ["rotational_face", "rotational_profile", "outer_rotational_profile"], ["turning_od"], "TURN-OD-R",
               [number("stock_allowance_mm", "轴向余量", 0.0), number("depth_of_cut_mm", "切深", 0.5, minimum=0.01, group="cutting"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_od_roughing", "外圆粗车", "车削", "按 Z-R 外轮廓分层去除外圆余量。", "ODRoughing",
               ["outer_rotational_profile"], ["turning_od"], "TURN-OD-R",
               [number("radial_allowance_mm", "径向余量", 0.3), number("axial_allowance_mm", "轴向余量", 0.15), number("depth_of_cut_mm", "径向切深", 1.0, minimum=0.01, group="cutting"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_od_finishing", "外圆精车", "车削", "精加工外圆、锥面和回转圆弧轮廓。", "ODFinishing",
               ["outer_rotational_profile"], ["turning_od"], "TURN-OD-F",
               [number("radial_allowance_mm", "径向余量", 0.0), number("axial_allowance_mm", "轴向余量", 0.0), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_id_roughing", "内孔粗车", "车削", "按内轮廓分层去除镗孔余量。", "IDRoughing",
               ["inner_rotational_profile"], ["turning_id"], "TURN-ID-R",
               [number("radial_allowance_mm", "径向余量", 0.25), number("depth_of_cut_mm", "径向切深", 0.5, minimum=0.01, group="cutting"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_id_finishing", "内孔精车", "车削", "精加工内孔回转轮廓。", "IDFinishing",
               ["inner_rotational_profile"], ["turning_id"], "TURN-ID-F",
               [number("radial_allowance_mm", "径向余量", 0.0), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_grooving", "车槽", "槽加工", "加工外槽、内槽或端面槽。", "Grooving",
               ["od_groove", "id_groove", "face_groove"], ["grooving", "internal_grooving"], "TURN-GROOVE-2",
               [number("groove_width_mm", "槽宽", 2.0, minimum=0.01, group="geometry", required=True), number("peck_depth_mm", "分层切入量", 0.5, minimum=0.01, group="cutting"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_threading", "车螺纹", "螺纹加工", "加工内外回转螺纹。", "Threading",
               ["external_thread", "internal_thread"], ["threading"], "TURN-THREAD-60",
               [number("start_z_mm", "螺纹起点", 0.0, group="geometry", required=True), number("end_z_mm", "螺纹终点", -10.0, minimum=None, group="geometry", required=True), number("major_diameter_mm", "大径", 10.0, minimum=0.01, group="geometry", required=True), number("minor_diameter_mm", "小径", 8.8, minimum=0, group="geometry", required=True), number("pitch_mm", "螺距", 1.0, minimum=0.01, group="geometry", required=True), number("thread_depth_mm", "牙深", 0.6, minimum=0.01, group="geometry", required=True), number("pass_count", "切削次数", 6, unit="次", minimum=1, group="strategy"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("axial_drilling", "轴向钻孔", "孔加工", "使用正面或背面固定刀位沿回转轴钻孔。", "AxialDrilling",
               ["axial_hole"], ["drill"], "DRILL-6.0",
               [number("depth_mm", "编程深度", 5.0, minimum=0.01, group="geometry", required=True), number("peck_depth_mm", "啄钻深度", 1.0, minimum=0.01, group="cutting"), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("axial_tapping", "轴向攻丝", "螺纹加工", "使用正面或背面固定刀位沿回转轴攻丝。", "AxialTapping",
               ["axial_thread"], ["tap"], "TAP-M6",
               [number("depth_mm", "螺纹深度", 5.0, minimum=0.01, group="geometry", required=True), number("pitch_mm", "螺距", 1.0, minimum=0.01, group="geometry", required=True), *COMMON_TURNING], "experimental", False, provider="turning"),
    definition("turn_cutoff", "切断", "切断", "将成品从棒料切离，并为后续接料状态提供明确语义。", "Cutoff",
               ["cutoff_plane", "outer_rotational_profile"], ["cutoff"], "TURN-CUTOFF-2",
               [number("cutting_width_mm", "刀宽", 2.0, minimum=0.01, group="geometry", required=True), number("breakthrough_radius_mm", "中心越过量", 0.1, minimum=0, group="geometry"), *COMMON_TURNING], "experimental", False, provider="turning"),
]

_BY_ID = {item.id: item for item in OPERATION_DEFINITIONS}


def get_operation_definition(identifier: str) -> OperationDefinition:
    try:
        return _BY_ID[identifier]
    except KeyError as error:
        raise ValueError(f"未知工序定义: {identifier}") from error


def operation_library_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "engine": "FreeCAD CAM",
        "definitions": [item.model_dump(mode="json") for item in OPERATION_DEFINITIONS],
    }


def validate_parameters(definition: OperationDefinition, supplied: dict[str, ParameterValue]) -> dict[str, ParameterValue]:
    definitions = {item.key: item for item in definition.parameters}
    values = {item.key: item.default for item in definition.parameters if item.default is not None}
    values.update(supplied)
    for item in definition.parameters:
        if item.required and item.key not in values:
            raise ValueError(f"{definition.name}缺少必填参数: {item.label}")
    for key, value in supplied.items():
        item = definitions.get(key)
        if item is None:
            # Runtime-derived feeds and geometry measurements remain forward compatible.
            continue
        if item.type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"参数{item.label}必须是布尔值")
        if item.type in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"参数{item.label}必须是数值")
            numeric = float(value)
            if item.minimum is not None and numeric < item.minimum:
                raise ValueError(f"参数{item.label}不能小于 {item.minimum}")
            if item.maximum is not None and numeric > item.maximum:
                raise ValueError(f"参数{item.label}不能大于 {item.maximum}")
        if item.type == "enum" and str(value) not in item.choices:
            raise ValueError(f"参数{item.label}不在允许范围内")
    return values


def create_operation_instance(*, type: str, tool: Tool, source: str = "automatic", **values: object) -> Operation:
    definition = get_operation_definition(type)
    parameters = validate_parameters(definition, dict(values.pop("parameters", {})))
    return Operation(
        type=type, definition_id=definition.id, definition_version=definition.version,
        source=source, tool=tool, parameters=parameters, **values,
    )
