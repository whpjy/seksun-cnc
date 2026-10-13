"""Explicit reference drill geometry and necessary machine capability checks."""
from math import tan, radians
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class SideToolGeometry(BaseModel):
    # Lengths below are measured from the drill tip, not the full-diameter shoulder.
    flute_length_mm: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    stickout_from_tip_mm: float | None = Field(default=None, gt=0, le=500, allow_inf_nan=False)
    shank_diameter_mm: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    holder_diameter_mm: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False)
    holder_length_mm: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    station_group: Literal["gang", "back"] = "gang"
    evidence: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_body(self):
        if not self.evidence.strip():
            raise ValueError("请填写刀具几何及钻深依据")
        values=[self.flute_length_mm,self.stickout_from_tip_mm,self.shank_diameter_mm,self.holder_diameter_mm,self.holder_length_mm]
        if any(v is not None for v in values) and not all(v is not None for v in values):
            raise ValueError("检查刀杆和刀柄需同时提供刃长、伸出长度、刀杆直径、刀柄直径与长度")
        if self.flute_length_mm is not None and self.stickout_from_tip_mm < self.flute_length_mm:
            raise ValueError("从钻尖量起的伸出长度不能小于刃长")
        if self.holder_diameter_mm is not None and self.holder_diameter_mm < self.shank_diameter_mm:
            raise ValueError("本简化刀柄模型的直径不能小于刀杆直径")
        return self


class SideDrillGeometry(SideToolGeometry):
    point_angle_deg:float=Field(ge=90,le=150,allow_inf_nan=False)
    full_diameter_extension_mm:float=Field(default=0,ge=-20,le=20,allow_inf_nan=False)


class SideFlatGeometry(SideToolGeometry):
    flute_length_mm:float=Field(gt=0,le=300,allow_inf_nan=False)
    stickout_from_tip_mm:float=Field(gt=0,le=500,allow_inf_nan=False)
    shank_diameter_mm:float=Field(gt=0,le=100,allow_inf_nan=False)
    holder_diameter_mm:float=Field(gt=0,le=200,allow_inf_nan=False)
    holder_length_mm:float=Field(gt=0,le=300,allow_inf_nan=False)


def prepare_flat_geometry(draft,geometry):
    holes=[]
    for original in draft['holes']:
        h=dict(original)
        if geometry.shank_diameter_mm<h['tool_diameter_mm']:raise ValueError('刀杆不能小于本模型的切削直径')
        h.update(tip_end=h['end'],tip_entry=h['entry'],tip_length_mm=0,
            flute_reach_status='passed' if geometry.flute_length_mm>=h['centerline_feed_length_mm'] else 'review')
        holes.append(h)
    return {**draft,'holes':holes,'drill_geometry':geometry.model_dump(mode='json'),'geometry_scope':'flat_end_tool_body'}


def prepare_drill_geometry(draft:dict, geometry:SideDrillGeometry) -> dict:
    holes=[]
    for original in draft["holes"]:
        hole=dict(original)
        radius=hole["tool_diameter_mm"]/2
        tip_length=radius/tan(radians(geometry.point_angle_deg/2))
        if geometry.flute_length_mm is not None and geometry.flute_length_mm <= tip_length:
            raise ValueError("刃长必须大于钻尖锥体长度")
        if geometry.shank_diameter_mm is not None and geometry.shank_diameter_mm < hole["tool_diameter_mm"]:
            raise ValueError("本简化钻头模型要求刀杆直径不小于切削直径")
        direction=hole["outward_direction"]
        hole["end"]={k:hole["end"][k]-direction[k]*geometry.full_diameter_extension_mm for k in "xyz"}
        hole["tip_end"]={k:hole["end"][k]-direction[k]*tip_length for k in "xyz"}
        hole["tip_length_mm"]=tip_length
        hole["centerline_feed_length_mm"]+=geometry.full_diameter_extension_mm
        if hole["centerline_feed_length_mm"] <= 1e-6:
            raise ValueError("终点回退超过当前进给行程，无法形成有效切削路径")
        hole["tip_entry"]={k:hole["entry"][k]-direction[k]*tip_length for k in "xyz"}
        hole["moves"]=[{"kind":"rapid","point":hole["tip_entry"]},{"kind":"feed","point":hole["tip_end"]},{"kind":"rapid","point":hole["tip_entry"]}]
        # Entry-based upper bound includes air clearance; do not call it the
        # actual wall depth or a manufacturer-approved drilling depth.
        required=hole["centerline_feed_length_mm"]+tip_length
        hole["conservative_reach_length_mm"]=required
        hole["flute_reach_status"]=("not_checked" if geometry.flute_length_mm is None else "passed" if geometry.flute_length_mm >= required else "review")
        holes.append(hole)
    return {**draft,"holes":holes,"drill_geometry":geometry.model_dump(mode="json"),"geometry_scope":"conical_tip_and_cylindrical_body"}


def assess_side_hole_machine(job, snapshot, draft:dict, station_group="gang",setup=None) -> dict:
    capabilities=set(snapshot.validation.capabilities)
    axes=set(snapshot.validation.enabled_axes)
    side_axis="Y1" if station_group=="gang" else "Y2"
    offset=any(h["requires_transverse_positioning"] for h in draft["holes"])
    bound=snapshot.instance.id==job.machine_instance_id and snapshot.configuration_hash==job.machine_configuration_hash
    checks=[{"code":"bound_configuration","status":"passed" if bound else "failed","detail":"任务绑定的机床实例及配置哈希一致" if bound else "任务与机床配置快照不一致"},
            {"code":"configuration_valid","status":"passed" if snapshot.validation.valid else "failed","detail":"机床配置的必要条件检查"},
            {"code":"radial_drilling_module","status":"passed" if station_group=="gang" and "radial_drilling" in capabilities else "review" if station_group=="back" else "failed",
             "detail":"正面侧向动力钻削模块已配置" if station_group=="gang" and "radial_drilling" in capabilities else "需确认背面侧向动力钻削刀位" if station_group=="back" else "缺少配置中支持侧向动力钻削的模块"},
            {"code":"offset_positioning_axis","status":"passed" if not offset or side_axis in axes else "failed","detail":f"偏置孔需核对 {side_axis} 的定位能力、实际方向与行程" if offset else "孔轴无横向偏置"},
            {"code":"spindle_indexing","status":"passed" if station_group=="gang" and "main_spindle_indexing_1deg" in capabilities else "review","detail":"正面主轴基础分度能力已配置，实际分度角仍需坐标转换检查" if station_group=="gang" else "需确认背面主轴分度能力"},
            {"code":"station_and_travel","status":"review","detail":"缺少源模型到机床坐标转换、刀位映射及有效行程证明"},
            {"code":"fixture_clearance","status":"not_run","detail":"尚未绑定可验证的夹具实体与装夹位置"}]
    if setup:
        from .radial_setup import RadialSetup,check_poses
        from .machining_geometry import job_axis
        from .main import job_directory
        from .rotational_features import RotationalFeatureAnalysis
        geometry=RotationalFeatureAnalysis.model_validate_json((job_directory(job.id)/'rotational-features.json').read_text(encoding='utf-8'))
        cfg=RadialSetup(**setup['configuration']);axis=job_axis(job.model_dump(mode='json'),geometry.model_dump(mode='json'))
        trajectory=check_poses(cfg,axis,draft,float(job.plan.stock['diameter_mm'])/2)
        if cfg.module_id not in snapshot.instance.installed_modules:trajectory['errors'].append('所选模块未安装在任务绑定设备上')
        from .l32_configuration import L32_DEFINITION
        module=next((m for m in L32_DEFINITION.modules if m.id==cfg.module_id),None)
        if not module or module.station_group!=station_group or 'radial_drilling' not in module.capabilities:
            trajectory['errors'].append('所选模块不支持当前侧向加工刀组')
        if station_group!='gang':trajectory['errors'].append('背面径向坐标与Y2行程执行器尚未验证')
        if any(abs(p['index_rotation_deg']-round(p['index_rotation_deg']))>1e-6 for p in trajectory['poses']):trajectory['errors'].append('分度角超出已验证的1°基础分度网格')
        if trajectory['errors']:trajectory['status']='failed'
        checks[-2]={'code':'station_and_travel','status':trajectory['status'],
            'detail':'; '.join(trajectory['errors']) if trajectory['errors'] else f'刀位{cfg.station}，源坐标、分度及声明行程已计算'}
        result_trajectory=trajectory
    return {"status":"failed" if any(c["status"]=="failed" for c in checks) else "review",
            "station_group":station_group,"required_offset_axis":side_axis if offset else None,
            "installed_modules":list(snapshot.instance.installed_modules),"checks":checks,
            "scope":"configured_necessary_capabilities_only","can_accept":False,
            **({'trajectory':result_trajectory} if setup else {})}
