from __future__ import annotations

from .models import GeometryAnalysis, ProcessPlan


def build_forming_preview(
    analysis: GeometryAnalysis,
    plan: ProcessPlan,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Build a reviewable, non-production sheet-forming process preview."""
    bounds = analysis.measurements["bounding_box"]
    assert not isinstance(bounds, float)
    operations = [operation for setup in plan.setups for operation in setup.operations if operation.enabled]
    stages = [
        {
            "operation_id": operation.id,
            "name": operation.name,
            "process": operation.type,
            "start_factor": float(operation.parameters.get("formation_start", 0.0)),
            "end_factor": float(operation.parameters.get("formation_end", 1.0)),
            "status": "concept_preview",
        }
        for operation in operations
    ]
    nominal_thickness = float(plan.stock.get("nominal_thickness_mm", 0.0))
    result = {
        "process_kind": "sheet_forming",
        "engine": "Seksun sheet-forming staged preview",
        "engine_version": "0.1.0",
        "operation_backend": "forming_preview",
        "postprocessor": "none",
        "generated_operations": [operation.id for operation in operations],
        "native_operation_types": {operation.id: operation.type for operation in operations},
        "skipped": [],
        "path_command_count": 0,
        "preview_segments": [],
        "profile_boundaries": [],
        "forming_preview": {
            "method": "target-mesh-depth-morph",
            "validation_level": "concept",
            "nominal_thickness_mm": nominal_thickness,
            "formed_depth_mm": round(bounds.size.y, 3),
            "stages": stages,
            "production_output_available": False,
        },
    }
    verification = {
        "schema_version": "0.1.0",
        "engine": "Seksun sheet-forming preflight",
        "status": "warning",
        "checks": [
            {
                "id": "sheet_classification",
                "status": "passed",
                "message": f"已识别薄板成形件，推算名义板厚 {nominal_thickness:.2f} mm",
            },
            {
                "id": "process_coverage",
                "status": "passed",
                "message": f"已覆盖展开、下料、预成形、终成形、去毛刺和检测，共 {len(operations)} 道工序",
            },
            {
                "id": "flat_pattern_validation",
                "status": "warning",
                "message": "当前为几何压平预览，尚未完成中性层展开和弯曲扣除校验",
            },
            {
                "id": "forming_physics",
                "status": "warning",
                "message": "尚未输入材料流变、轧制方向、模具间隙、压边力和摩擦参数",
            },
        ],
        "errors": [],
        "warnings": [
            "预览不能用于模具制造或生产放行；需要有限元成形分析和试模数据。",
        ],
        "metrics": {
            "minimum_mm": bounds.minimum.model_dump(),
            "maximum_mm": bounds.maximum.model_dump(),
            "extent_mm": bounds.size.model_dump(),
            "estimated_cycle_minutes": plan.estimated_minutes,
            "generated_operation_count": len(operations),
            "nominal_thickness_mm": nominal_thickness,
            "formed_depth_mm": round(bounds.size.y, 3),
            "stage_count": len(stages),
        },
        "limitations": [
            "未计算局部应变、厚度减薄、起皱、破裂和回弹。",
            "未生成生产用展开 DXF、激光 NC 或冲压设备程序。",
        ],
    }
    # Keep the response contract stable for existing clients. The surface is
    # deliberately empty; sheet-forming clients render the target-mesh morph.
    simulation = {
        "schema_version": "0.1.0",
        "engine": "Seksun sheet-forming staged preview",
        "status": "warning",
        "method": "target_mesh_depth_morph",
        "metrics": {
            "initial_stock_volume_mm3": round(float(analysis.measurements.get("volume", 0.0)), 2),
            "removed_volume_mm3": 0.0,
            "remaining_volume_mm3": round(float(analysis.measurements.get("volume", 0.0)), 2),
            "removed_percent": 0.0,
            "cut_segment_count": 0,
            "resolution_mm": nominal_thickness,
        },
        "surface": {
            "setup_id": "FORMING-1", "origin": {"x": 0.0, "y": 0.0},
            "bottom_z": 0.0, "top_z": 0.0, "resolution_mm": nominal_thickness,
            "columns": 2, "rows": 2, "heights": [0.0] * 4, "lower_heights": [0.0] * 4,
        },
        "forming_preview": result["forming_preview"],
        "warnings": verification["warnings"],
    }
    collision = {
        "schema_version": "0.1.0",
        "engine": "Seksun forming applicability check",
        "status": "passed",
        "configuration": None,
        "checks": [{
            "id": "cnc_collision_not_applicable", "status": "passed",
            "message": "薄板成形预览不包含 CNC 刀具运动，机床碰撞检查不适用",
        }],
        "collisions": [], "low_rapids": [],
        "metrics": {"fixture_component_count": 0, "collision_count": 0, "low_rapid_count": 0, "required_rapid_z": 0},
        "limitations": ["模具闭合、送料和机械手干涉需在选定冲压/折弯设备后校核。"],
    }
    return result, verification, simulation, collision
