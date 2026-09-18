from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .machine_models import (
    AxisDefinition,
    MachineConfigurationIssue,
    MachineConfigurationSnapshot,
    MachineConfigurationValidation,
    MachineDefinition,
    MachineInstance,
    MachineModuleDefinition,
    MachineVariantDefinition,
    SpindleDefinition,
)


L32_DEFINITION = MachineDefinition(
    id="citizen-cincom-l32",
    manufacturer="Citizen",
    family="Cincom L32",
    source_document="L32(1).pdf / Catalog No. 446",
    source_revision="2016-05",
    controller_family="CINCOM SYSTEM M70LPC-VU",
    standard_bar_diameter_mm=32,
    optional_bar_diameter_mm=38,
    maximum_length_per_chucking_mm=320,
    axes=[
        AxisDefinition(id="X1", kind="linear", channel_id="main", component="gang", maximum_rapid_mm_min=32000),
        AxisDefinition(id="Y1", kind="linear", channel_id="main", component="gang", maximum_rapid_mm_min=32000),
        AxisDefinition(id="Z1", kind="linear", channel_id="main", component="headstock", maximum_rapid_mm_min=32000),
        AxisDefinition(id="X2", kind="linear", channel_id="sub", component="sub_spindle", maximum_rapid_mm_min=32000),
        AxisDefinition(id="Z2", kind="linear", channel_id="sub", component="sub_spindle", maximum_rapid_mm_min=32000),
        AxisDefinition(
            id="B", kind="rotary", channel_id="main", component="gang",
            availability="variant", variants=["IX", "XII"], angular_range_degrees=[-45, 90],
            continuous_interpolation=False,
        ),
        AxisDefinition(
            id="Y2", kind="linear", channel_id="sub", component="back_tool_post",
            availability="variant", variants=["X", "XII"], maximum_rapid_mm_min=24000,
        ),
    ],
    spindles=[
        SpindleDefinition(id="main", name="正面主轴", channel_id="main", role="work", maximum_rpm=8000, motor_kw=[3.7, 7.5], standard_indexing_increment_degrees=1, continuous_c_axis_option_id="continuous_c_axis"),
        SpindleDefinition(id="sub", name="背面主轴", channel_id="sub", role="work", maximum_rpm=8000, motor_kw=[2.2, 3.7]),
        SpindleDefinition(id="gang_live", name="排刀旋转刀具", channel_id="main", role="live_tool", maximum_rpm=6000, rated_rpm=4500, motor_kw=[1.0]),
        SpindleDefinition(id="sub_live", name="背面旋转刀具", channel_id="sub", role="live_tool", maximum_rpm=6000, rated_rpm=3000, motor_kw=[1.0]),
    ],
    variants=[
        MachineVariantDefinition(id="VIII", model_code="L32-1M8", enabled_axes=["X1", "Y1", "Z1", "X2", "Z2"], minimum_tool_positions=19, maximum_tool_positions=30),
        MachineVariantDefinition(id="IX", model_code="L32-1M9", enabled_axes=["X1", "Y1", "Z1", "X2", "Z2", "B"], minimum_tool_positions=26, maximum_tool_positions=36),
        MachineVariantDefinition(id="X", model_code="L32-1M10", enabled_axes=["X1", "Y1", "Z1", "X2", "Z2", "Y2"], minimum_tool_positions=24, maximum_tool_positions=44),
        MachineVariantDefinition(id="XII", model_code="L32-1M12", enabled_axes=["X1", "Y1", "Z1", "X2", "Z2", "B", "Y2"], minimum_tool_positions=30, maximum_tool_positions=40),
    ],
    modules=[
        MachineModuleDefinition(id="U30B", name="排刀旋转刀具模块", station_group="gang", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["live_tool_milling", "radial_drilling"], live_tool_positions=4),
        MachineModuleDefinition(id="U31B", name="排刀角度可调旋转刀具模块", station_group="gang", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["live_tool_milling", "radial_drilling", "angled_drilling"], live_tool_positions=4, notes=["一个刀位可在 0–90° 范围内人工调整"]),
        MachineModuleDefinition(id="U32B", name="排刀 B 轴旋转刀具模块", station_group="gang", compatible_variants=["IX", "XII"], required_axes=["B"], capabilities=["live_tool_milling", "radial_drilling", "b_axis_indexed_machining"], live_tool_positions=4, notes=["B 轴范围 -45–90°；当前软件首版仅允许定位加工"]),
        MachineModuleDefinition(id="U120B", name="对向固定刀具模块", station_group="opposed", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["opposed_turning"], fixed_tool_positions=4),
        MachineModuleDefinition(id="U121B", name="对向旋转刀具模块", station_group="opposed", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["opposed_live_tool_milling"], live_tool_positions=3),
        MachineModuleDefinition(id="U150B", name="背面固定刀具模块", station_group="back", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["back_turning", "axial_drilling"], fixed_tool_positions=5),
        MachineModuleDefinition(id="U151B", name="背面复合刀具模块", station_group="back", compatible_variants=["VIII", "IX", "X", "XII"], capabilities=["back_turning", "back_live_tool_milling"], fixed_tool_positions=1, live_tool_positions=4),
        MachineModuleDefinition(id="U12B", name="Y2 背面刀具台模块", station_group="back", compatible_variants=["X", "XII"], required_axes=["Y2"], capabilities=["back_turning", "back_live_tool_milling", "y2_machining"], fixed_tool_positions=5, live_tool_positions=4),
    ],
)

# Deliberately empty until a post profile completes offline replay, dry-run,
# trial-cut, and first-article qualification on a named physical machine.
CERTIFIED_L32_POSTPROCESSORS: set[str] = set()


def _issue(code: str, severity: str, field: str, message: str) -> MachineConfigurationIssue:
    return MachineConfigurationIssue(code=code, severity=severity, field=field, message=message)


def validate_l32_instance(instance: MachineInstance) -> MachineConfigurationValidation:
    issues: list[MachineConfigurationIssue] = []
    if instance.definition_id != L32_DEFINITION.id:
        issues.append(_issue("definition_mismatch", "blocking", "definition_id", f"L32 实例必须引用 {L32_DEFINITION.id}"))
    variants = {item.id: item for item in L32_DEFINITION.variants}
    modules = {item.id: item for item in L32_DEFINITION.modules}
    variant = variants.get(instance.variant)
    if variant is None:
        issues.append(_issue("unknown_variant", "blocking", "variant", f"未知 L32 机型: {instance.variant}"))
        enabled_axes: list[str] = []
    else:
        enabled_axes = list(variant.enabled_axes)

    selected_modules: list[MachineModuleDefinition] = []
    seen_groups: dict[str, str] = {}
    for module_id in instance.installed_modules:
        module = modules.get(module_id)
        if module is None:
            issues.append(_issue("unknown_module", "blocking", "installed_modules", f"未知 L32 刀具模块: {module_id}"))
            continue
        selected_modules.append(module)
        if variant and instance.variant not in module.compatible_variants:
            issues.append(_issue("incompatible_module", "blocking", "installed_modules", f"{module_id} 不适用于 L32 {instance.variant} 型"))
        missing_axes = sorted(set(module.required_axes) - set(enabled_axes))
        if missing_axes:
            issues.append(_issue("module_axis_missing", "blocking", "installed_modules", f"{module_id} 缺少所需轴: {', '.join(missing_axes)}"))
        existing = seen_groups.get(module.station_group)
        if existing:
            issues.append(_issue("station_group_conflict", "blocking", "installed_modules", f"{existing} 与 {module_id} 同属 {module.station_group} 刀具台，不能同时作为已安装模块"))
        else:
            seen_groups[module.station_group] = module_id

    if not instance.serial_number:
        issues.append(_issue("serial_number_unconfirmed", "warning", "serial_number", "设备编号尚未确认"))
    if not instance.controller_revision:
        issues.append(_issue("controller_revision_unconfirmed", "warning", "controller_revision", "控制器软件版本尚未确认"))
    if not instance.installed_modules:
        issues.append(_issue("tooling_layout_unconfirmed", "warning", "installed_modules", "实际刀具模块尚未确认"))
    if instance.bar_diameter_mm > L32_DEFINITION.standard_bar_diameter_mm:
        if instance.bar_diameter_mm > (L32_DEFINITION.optional_bar_diameter_mm or 0):
            issues.append(_issue("bar_diameter_exceeded", "blocking", "bar_diameter_mm", f"棒料直径 Ø{instance.bar_diameter_mm:g} 超过资料上限"))
        elif "bar_diameter_38mm" not in instance.enabled_options:
            issues.append(_issue("bar_option_missing", "blocking", "enabled_options", "超过 Ø32 的棒料要求确认 Ø38 选件"))
    if instance.postprocessor_profile_id not in CERTIFIED_L32_POSTPROCESSORS:
        issues.append(_issue(
            "postprocessor_unqualified", "warning", "postprocessor_profile_id",
            "尚未绑定并认证 L32 后处理器，仅允许生成 CAM 草案",
        ))

    base_capabilities = {"turning", "grooving_cutoff", "threading_tapping", "axial_drilling", "main_spindle_indexing_1deg"}
    if "continuous_c_axis" in instance.enabled_options:
        base_capabilities.add("continuous_c_axis")
    capabilities = sorted(base_capabilities | {capability for module in selected_modules for capability in module.capabilities})
    blocking = any(item.severity == "blocking" for item in issues)
    production_blocking_codes = {
        "serial_number_unconfirmed", "controller_revision_unconfirmed",
        "tooling_layout_unconfirmed", "postprocessor_unqualified",
    }
    production_ready = not blocking and not any(item.code in production_blocking_codes for item in issues)
    return MachineConfigurationValidation(
        valid=not blocking,
        production_ready=production_ready,
        enabled_axes=enabled_axes,
        capabilities=capabilities,
        issues=issues,
    )


def snapshot_l32_instance(instance: MachineInstance) -> MachineConfigurationSnapshot:
    validation = validate_l32_instance(instance)
    canonical = json.dumps(
        instance.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return MachineConfigurationSnapshot(
        instance=instance,
        definition_revision=L32_DEFINITION.source_revision,
        configuration_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        captured_at=datetime.now(timezone.utc),
        validation=validation,
    )


def l32_definition_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "definition": L32_DEFINITION.model_dump(mode="json"),
    }


def l32_viii_live_tool_catalog_reference() -> dict[str, object]:
    """A read-only catalog scenario, never a registered or bound shop machine."""
    reference = MachineInstance(
        id="catalog-viii-u30b-u151b",
        definition_id=L32_DEFINITION.id,
        name="L32 VIII + U30B + U151B catalog reference",
        variant="VIII",
        operation_mode="guide_bushing",
        installed_modules=["U30B", "U151B"],
        enabled_options=[],
        bar_diameter_mm=32,
    )
    validation = validate_l32_instance(reference)
    return {
        "catalog_only": True,
        "bindable": False,
        "source_document": L32_DEFINITION.source_document,
        "source_revision": L32_DEFINITION.source_revision,
        "variant": reference.variant,
        "modules": reference.installed_modules,
        "standard_main_spindle_indexing_degrees": 1,
        "continuous_c_axis_assumed": False,
        "capabilities": validation.capabilities,
        "configuration_valid": validation.valid,
        "production_ready": False,
        "unconfirmed": ["physical_module_installation", "tool_station_mapping", "tool_holder_clearance", "controller_revision", "postprocessor"],
    }
