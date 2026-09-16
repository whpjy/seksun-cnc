from __future__ import annotations

from .l32_configuration import L32_DEFINITION
from .operation_library import OPERATION_DEFINITIONS


DEVICES: list[dict[str, object]] = [
    {
        "id": "seksun-freecad-cam-standard",
        "record_kind": "virtual",
        "manufacturer": "SEKSUN",
        "brand": "SEKSUN CNC",
        "model": "FreeCAD CAM Standard",
        "name": "SEKSUN FreeCAD CAM 标准虚拟设备",
        "display_name": "FreeCAD CAM Standard",
        "category": "virtual_cartesian_machining_center",
        "category_label": "通用三轴 CAM 虚拟设备",
        "library_status": "supported",
        "configuration_status": "confirmed",
        "variants": ["Standard"],
        "controller": {
            "manufacturer": "SEKSUN",
            "model": "FreeCAD CAM 1.1",
        },
        "workpiece": {
            "stock_form": "block",
            "working_envelope_mm": [800, 500, 500],
        },
        "spindles": [
            {"id": "main", "name": "虚拟铣削主轴", "maximum_rpm": 10000, "motor_kw": []},
        ],
        "axes": [
            {"id": "X", "kind": "linear", "availability": "standard"},
            {"id": "Y", "kind": "linear", "availability": "standard"},
            {"id": "Z", "kind": "linear", "availability": "standard"},
        ],
        "tooling": {
            "catalog": "SEKSUN 当前刀具目录",
            "maximum_tool_diameter_mm": 80,
        },
        "capabilities": [
            {"code": "three_axis_milling", "name": "三轴铣削", "status": "supported"},
            {"code": "drilling", "name": "钻孔", "status": "supported"},
            {"code": "profile_pocket_slot", "name": "轮廓/型腔/槽", "status": "supported"},
            {"code": "surface_milling", "name": "3D曲面", "status": "conditional"},
        ],
        "operation_bindings": [
            {
                "operation_id": definition.id,
                "status": "adapting" if definition.maturity == "planned" else "supported",
            }
            for definition in OPERATION_DEFINITIONS
            if definition.engine.provider != "turning"
        ],
        "system_integration": {
            "status": "supported",
            "postprocessor": "fanuc / grbl",
            "kinematics_adapter": "Cartesian XYZ",
            "direct_nc_output": True,
            "production_release_requires_physical_machine": True,
            "compatible_operation_groups": sorted({definition.category for definition in OPERATION_DEFINITIONS}),
        },
        "source": {
            "document": "SEKSUN 内置 CAM 工序库",
            "title": "FreeCAD CAM 执行能力基线",
            "catalog_number": "internal",
            "document_date": "2026-09",
            "pages": [],
        },
        "required_confirmation": [],
    },
    {
        "id": "citizen-cincom-l32",
        "record_kind": "physical",
        "manufacturer": "Citizen",
        "brand": "Cincom",
        "model": "L32",
        "name": "Citizen Cincom L32",
        "display_name": "Citizen Cincom L32",
        "category": "sliding_headstock_mill_turn",
        "category_label": "主轴箱移动型 CNC 自动车床",
        "library_status": "adapting",
        "configuration_status": "unconfirmed",
        "variants": ["VIII", "IX", "X", "XII"],
        "controller": {
            "manufacturer": "Mitsubishi Electric",
            "model": "MELDAS M70LPC-VU",
        },
        "workpiece": {
            "stock_form": "bar",
            "maximum_diameter_mm": 32,
            "optional_maximum_diameter_mm": 38,
            "maximum_length_per_chucking_mm": 320,
        },
        "spindles": [
            {"id": "main", "name": "正面主轴", "maximum_rpm": 8000, "motor_kw": [3.7, 7.5]},
            {"id": "sub", "name": "背面主轴", "maximum_rpm": 8000, "motor_kw": [2.2, 3.7]},
            {"id": "gang_live", "name": "排刀旋转刀具", "maximum_rpm": 6000, "rated_rpm": 4500, "motor_kw": [1.0]},
            {"id": "sub_live", "name": "背面旋转刀具", "maximum_rpm": 6000, "rated_rpm": 3000, "motor_kw": [1.0]},
        ],
        "axes": [
            {"id": "X1", "kind": "linear", "availability": "standard"},
            {"id": "Y1", "kind": "linear", "availability": "standard"},
            {"id": "Z1", "kind": "linear", "availability": "standard"},
            {"id": "X2", "kind": "linear", "availability": "standard"},
            {"id": "Z2", "kind": "linear", "availability": "standard"},
            {"id": "B", "kind": "rotary", "availability": "variant", "variants": ["IX", "XII"]},
            {"id": "Y2", "kind": "linear", "availability": "variant", "variants": ["X", "XII"]},
        ],
        "tooling": {
            "maximum_tool_positions_by_variant": {"VIII": "19–30", "IX": "26–36", "X": "24–44", "XII": "30–40"},
            "main_maximum_drill_diameter_mm": 12,
            "main_maximum_tap": "M12",
            "gang_live_maximum_drill_diameter_mm": 10,
            "gang_live_maximum_tap": "M8",
            "sub_live_maximum_drill_diameter_mm": 8,
            "sub_live_maximum_tap": "M6",
        },
        "capabilities": [
            {"code": "turning", "name": "外圆/内孔/端面车削", "status": "supported"},
            {"code": "grooving_cutoff", "name": "切槽与切断", "status": "supported"},
            {"code": "threading_tapping", "name": "车螺纹与攻丝", "status": "supported"},
            {"code": "axial_drilling", "name": "轴向钻孔", "status": "supported"},
            {"code": "live_tool_milling", "name": "旋转刀具铣削", "status": "conditional"},
            {"code": "radial_drilling", "name": "侧向钻孔", "status": "conditional"},
            {"code": "slot_pocket_milling", "name": "槽与小型型腔", "status": "conditional"},
            {"code": "engraving", "name": "雕刻", "status": "conditional"},
            {"code": "surface_3d", "name": "连续多轴3D曲面", "status": "unverified"},
        ],
        "system_integration": {
            "status": "adapting",
            "postprocessor": None,
            "kinematics_adapter": None,
            "direct_nc_output": False,
            "compatible_operation_groups": ["钻孔", "倒角", "槽加工", "小型型腔", "雕刻"],
        },
        "source": {
            "document": "L32(1).pdf",
            "title": "Citizen Cincom L32 主轴箱移动型 CNC 自动车床",
            "catalog_number": "446",
            "document_date": "2016-05",
            "pages": [2, 3, 4],
        },
        "required_confirmation": [
            "设备具体型号（VIII / IX / X / XII）",
            "设备编号与出厂年份",
            "实际安装的排刀、对向刀具台和背面刀具台模块",
            "B轴、Y2轴、背面旋转刀具等选配情况",
            "NC 系统版本与现用程序格式",
            "刀具工位表、刀具夹持器与最大尺寸",
            "实机行程、限位、安全位和工件交接点",
            "客户已验证的样例程序",
        ],
    },
]


def _synchronize_l32_catalog_record() -> None:
    """Keep the public device card derived from the versioned machine definition."""
    record = next(item for item in DEVICES if item["id"] == L32_DEFINITION.id)
    record["machine_definition_id"] = L32_DEFINITION.id
    record["variants"] = [item.id for item in L32_DEFINITION.variants]
    record["variant_configurations"] = [item.model_dump(mode="json") for item in L32_DEFINITION.variants]
    record["axes"] = [
        {
            "id": item.id,
            "kind": item.kind,
            "availability": item.availability,
            **({"variants": item.variants} if item.variants else {}),
        }
        for item in L32_DEFINITION.axes
    ]
    record["spindles"] = [
        {
            "id": item.id,
            "name": item.name,
            "maximum_rpm": item.maximum_rpm,
            "motor_kw": item.motor_kw,
            **({"rated_rpm": item.rated_rpm} if item.rated_rpm else {}),
        }
        for item in L32_DEFINITION.spindles
    ]
    record["workpiece"] = {
        "stock_form": "bar",
        "maximum_diameter_mm": L32_DEFINITION.standard_bar_diameter_mm,
        "optional_maximum_diameter_mm": L32_DEFINITION.optional_bar_diameter_mm,
        "maximum_length_per_chucking_mm": L32_DEFINITION.maximum_length_per_chucking_mm,
    }
    tooling = dict(record["tooling"])
    tooling["maximum_tool_positions_by_variant"] = {
        item.id: f"{item.minimum_tool_positions}–{item.maximum_tool_positions}"
        for item in L32_DEFINITION.variants
    }
    tooling["available_modules"] = [
        {
            "id": item.id,
            "name": item.name,
            "station_group": item.station_group,
            "compatible_variants": item.compatible_variants,
            "required_axes": item.required_axes,
            "capabilities": item.capabilities,
        }
        for item in L32_DEFINITION.modules
    ]
    record["tooling"] = tooling
    l32_existing_operation_ids = {
        "drilling", "edge_chamfer", "engraving",
        "slot_roughing", "slot_finishing", "pocket_roughing", "pocket_finishing",
    }
    record["operation_bindings"] = [
        {"operation_id": definition.id, "status": "adapting"}
        for definition in OPERATION_DEFINITIONS
        if definition.engine.provider == "turning" or definition.id in l32_existing_operation_ids
    ]


_synchronize_l32_catalog_record()


def device_library_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "devices": DEVICES,
    }


def get_device(device_id: str) -> dict[str, object]:
    match = next((item for item in DEVICES if item["id"] == device_id), None)
    if match is None:
        raise ValueError(f"未知设备: {device_id}")
    return match
