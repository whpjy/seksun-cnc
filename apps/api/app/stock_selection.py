"""Configurable solid-bar selection; no blank inventory is required."""
import json
import os
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
_rule_lock = RLock()


class StockSelectionRule(BaseModel):
    diameter_increment_mm: float = Field(default=0.2, ge=0, le=20, allow_inf_nan=False)
    front_allowance_mm: float = Field(default=2.0, ge=0, le=100, allow_inf_nan=False)
    back_allowance_mm: float = Field(default=2.0, ge=0, le=100, allow_inf_nan=False)
    diameter_round_up_mm: float = Field(default=0, ge=0, le=20, allow_inf_nan=False)
    revision: int = Field(default=1, ge=1)


def rule_path() -> Path:
    storage = Path(os.getenv("CNC_STORAGE_ROOT", Path(__file__).resolve().parents[1] / ".seksun-cnc" / "jobs"))
    return storage.parent / "stock-selection-rule.json"


def load_stock_rule() -> StockSelectionRule:
    path = rule_path()
    with _rule_lock:
        return StockSelectionRule.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else StockSelectionRule()


def select_solid_bar(maximum_diameter: float, z_min: float, z_max: float, rule: StockSelectionRule) -> dict:
    if maximum_diameter <= 0 or z_max <= z_min:
        raise ValueError("有效外径与轴向范围是毛坯选择的前提")
    diameter = Decimal(str(maximum_diameter)) + Decimal(str(rule.diameter_increment_mm))
    step = Decimal(str(rule.diameter_round_up_mm))
    if step > 0:
        diameter = (diameter / step).to_integral_value(rounding=ROUND_CEILING) * step
    diameter = float(diameter.quantize(Decimal("0.001"), rounding=ROUND_CEILING))
    low, high = z_min - rule.back_allowance_mm, z_max + rule.front_allowance_mm
    return {
        "type": "round_bar", "solid": True, "initial_bore_diameter_mm": 0,
        "diameter_mm": diameter, "length_mm": round(high - low, 6),
        "z_min_mm": low, "z_max_mm": high,
        "allowance_mm": {"radial": (diameter - maximum_diameter) / 2,
                         "axial": max(rule.front_allowance_mm, rule.back_allowance_mm),
                         "front": rule.front_allowance_mm, "back": rule.back_allowance_mm},
        "selection": {"rule": rule.model_dump(mode="json"),
                      "part_max_outer_diameter_mm": maximum_diameter,
                      "part_z_min_mm": z_min, "part_z_max_mm": z_max,
                      "diameter_formula": "最大外径 + 直径增量，按配置向上取整",
                      "length_formula": "零件轴向长度 + 正面余量 + 背面余量"},
    }


@router.get("/api/v1/process-rules/stock-selection")
def get_stock_rule():
    return load_stock_rule().model_dump(mode="json")


@router.put("/api/v1/process-rules/stock-selection")
def save_stock_rule(request: StockSelectionRule):
    with _rule_lock:
        saved = request.model_copy(update={"revision": load_stock_rule().revision + 1})
        path = rule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(saved.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
    return saved.model_dump(mode="json")


def _job_stock(job_id):
    from .main import job_directory, load_job
    from .rotational_features import RotationalFeatureAnalysis
    from .machining_geometry import job_axis
    job = load_job(job_id)
    path = job_directory(job_id) / "rotational-features.json"
    if job.device_id != "citizen-cincom-l32" or not job.analysis or not path.is_file():
        raise HTTPException(409, "当前毛坯选择需要 L32 回转几何")
    geometry = RotationalFeatureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    profile_id = (job.plan.stock if job.plan else {}).get("rotational_profile_id")
    profile = next((item for item in geometry.profiles if item.id == profile_id and item.review_state != "excluded"), None)
    profile = profile or max((item for item in geometry.profiles if item.side == "outer" and item.review_state != "excluded"), key=lambda item: max(p.radius for p in item.points), default=None)
    if not profile:
        raise HTTPException(409, "尚未识别到可用于毛坯选择的外轮廓")
    axis = job_axis(job.model_dump(mode="json"), geometry.model_dump(mode="json"))
    if not axis:
        raise HTTPException(409, "尚未确定毛坯回转轴")
    return job, geometry, profile, axis


def _select_for_job(job, profile, axis, rule):
    from .l32_planner import _solid_axial_span
    from .models import Vec3
    span = _solid_axial_span(job.analysis, Vec3.model_validate(axis["origin"]), Vec3.model_validate(axis["direction"]))
    low, high = min(p.z for p in profile.points), max(p.z for p in profile.points)
    if span:
        low, high = min(low, span[0]), max(high, span[1])
    model_diameter = max(p.radius for p in profile.points) * 2
    audit = job.drawing_analysis
    if job.drawing_filename and (not audit or not audit.get("stock_ready")):
        raise HTTPException(409, "请先在二维图纸依据面板核对最大外径、材料及图模差异，再选择毛坯")
    diameter = audit["maximum_outer_diameter_mm"] if audit else model_diameter
    selected = select_solid_bar(diameter, low, high, rule)
    if selected["diameter_mm"] + .001 < model_diameter:
        raise HTTPException(409, "所选毛坯不能包容 STEP 最大外径，请核对图模差异及直径余量")
    selected["selection"].update({"diameter_source": "drawing" if audit else "step_geometry",
        "drawing_filename": job.drawing_filename, "drawing_sha256": (audit or {}).get("sha256"),
        "drawing_revision_counter": (audit or {}).get("revision_counter"),
        "drawing_evidence": (audit or {}).get("maximum_outer_diameter_evidence"),
        "stock_note": (audit or {}).get("stock_note"), "model_max_outer_diameter_mm": model_diameter})
    return selected


@router.get("/api/v1/jobs/{job_id}/stock-selection")
def get_job_stock(job_id: str):
    from .agent.l32_operation_trial import _simulation_range
    job, geometry, profile, axis = _job_stock(job_id)
    rule = load_stock_rule()
    proposed = _select_for_job(job, profile, axis, rule)
    stock = job.plan.stock if job.plan and job.plan.stock.get("diameter_mm") else proposed
    low, high = _simulation_range(job, profile) if job.plan else (proposed["z_min_mm"], proposed["z_max_mm"])
    samples = [{"z": z, "outer_radius": float(stock["diameter_mm"]) / 2, "inner_radius": 0} for z in (low, high)]
    return {"stock": stock, "current_rule": rule.model_dump(mode="json"), "proposed_stock": proposed,
            "view": {"operation_id": "stock", "channel_id": "main", "axis_origin": axis["origin"],
                     "axis_direction": axis["direction"], "before_samples": samples, "after_samples": samples}}


@router.post("/api/v1/jobs/{job_id}/stock-selection/apply-rule")
def apply_job_stock_rule(job_id: str):
    from .main import _save_manual_plan_change, L32_OPERATION_TRIALING_JOBS, L32_ROLLING_LOOP_JOBS
    job, geometry, profile, axis = _job_stock(job_id)
    if not job.plan:
        raise HTTPException(409, "工艺草案尚未建立")
    if job_id in L32_OPERATION_TRIALING_JOBS or job_id in L32_ROLLING_LOOP_JOBS:
        raise HTTPException(409, "工序正在计算，请计算完成后修改毛坯")
    selected = _select_for_job(job, profile, axis, load_stock_rule())
    if selected["diameter_mm"] > 38 or selected["length_mm"] > 320:
        raise HTTPException(422, "所选毛坯超出 L32 已知加工尺寸范围")
    job.plan.stock.update(selected)
    job.plan.stock.pop("drawing_evidence_changed", None)
    job.plan.stock["rotational_axis"] = axis
    for key in ("verified_rotational_profile_ids", "verified_hole_feature_ids", "verified_prismatic_feature_ids", "nonrotational_material_verified"):
        job.plan.stock.pop(key, None)
    for setup in job.plan.setups:
        for operation in setup.operations:
            operation.status = "proposed"
            operation.generation_state = "dirty"
    return _save_manual_plan_change(job_id, job)
