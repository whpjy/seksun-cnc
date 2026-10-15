"""Operation-bound measured cutoff tooling and declared sub-spindle retention."""
import hashlib,json,math
from typing import Literal
from pydantic import BaseModel,Field,model_validator
from fastapi import APIRouter,HTTPException
from .radial_setup import load_setup,frame

router=APIRouter()
EXECUTION_REVISION='cutoff-motion-retention-v2'

class SubSpindleMotion(BaseModel):
    open_bore_diameter_mm:float=Field(gt=0,allow_inf_nan=False)
    envelope_outer_diameter_mm:float=Field(gt=0,allow_inf_nan=False)
    body_length_mm:float=Field(gt=0,allow_inf_nan=False)
    approach_distance_mm:float=Field(gt=0,le=300,allow_inf_nan=False)
    withdrawal_distance_mm:float=Field(gt=0,le=300,allow_inf_nan=False)
    z2_origin_mm:float=Field(allow_inf_nan=False)
    z2_sign:Literal[-1,1]
    z2_limits:list[float]=Field(min_length=2,max_length=2)
    x2_position_mm:float=Field(allow_inf_nan=False)
    x2_limits:list[float]=Field(min_length=2,max_length=2)
    envelope_confirmed:bool=False
    evidence:str=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def valid(self):
        if self.envelope_outer_diameter_mm<=self.open_bore_diameter_mm:raise ValueError('背轴外包络直径必须大于张开内径')
        if not self.evidence.strip():raise ValueError('请填写背轴包络及坐标依据')
        for limits in (self.x2_limits,self.z2_limits):
            if not all(math.isfinite(v) for v in limits) or limits[0]>=limits[1]:raise ValueError('背轴行程边界必须有效')
        return self


class CutoffSetup(BaseModel):
    inventory_id:str=Field(min_length=1,max_length=64)
    station:str=Field(min_length=1,max_length=64)
    end_radius_mm:float=Field(ge=-2,le=0,allow_inf_nan=False)
    grip_z_min_mm:float=Field(allow_inf_nan=False)
    grip_z_max_mm:float=Field(allow_inf_nan=False)
    grip_diameter_mm:float=Field(gt=0,allow_inf_nan=False)
    minimum_grip_length_mm:float=Field(gt=0,allow_inf_nan=False)
    retained_side:Literal['+Z','-Z']
    synchronization_rpm:int=Field(gt=0,le=10000)
    clamp_force_n:float=Field(gt=0,allow_inf_nan=False)
    required_clamp_force_n:float=Field(gt=0,allow_inf_nan=False)
    clamp_confirmed:bool=False
    verified_by:str=Field(min_length=1,max_length=120)
    evidence:str=Field(min_length=1,max_length=1000)
    expected_revision:str|None=None
    sub_spindle_motion:SubSpindleMotion|None=None

    @model_validator(mode='after')
    def validate_geometry(self):
        if self.grip_z_min_mm>=self.grip_z_max_mm:raise ValueError('夹持轴向起点必须小于终点')
        if not all(v.strip() for v in (self.station,self.verified_by,self.evidence)):raise ValueError('请填写实际刀位、确认人和依据')
        return self


def tool_geometry(record):
    fields={'cutting_width_mm':'measured_cutting_width_mm','cutting_reach_mm':'measured_cutting_reach_mm',
        'blade_height_mm':'measured_blade_height_mm','stickout_mm':'measured_stickout_mm',
        'holder_width_mm':'measured_holder_width_mm','holder_height_mm':'measured_holder_height_mm','holder_length_mm':'measured_holder_length_mm'}
    if not record or not record.active or record.tool_kind!='cutoff' or not record.cutoff_geometry_verified:
        raise ValueError('请绑定已确认完整实测几何的在用切断刀')
    return {key:getattr(record,field) for key,field in fields.items()}


def path_for(job,operation_id):
    from .main import job_directory
    # Resolve IDs from the plan before constructing any path.
    if not job.plan or operation_id not in {o.id for s in job.plan.setups for o in s.operations if o.type=='turn_cutoff'}:
        raise ValueError('当前工艺中不存在该切断工序')
    if not all(c.isalnum() or c in '_-' for c in operation_id):raise ValueError('无效切断工序编号')
    return job_directory(job.id)/f'cutoff-setup-{operation_id}.json'


def load_cutoff_setup(job,operation):
    path=path_for(job,operation.id)
    if not path.is_file():return None
    data=json.loads(path.read_text(encoding='utf-8'));radial=load_setup(job)
    if not radial or data.get('radial_setup_revision')!=radial['revision']:
        raise ValueError('机床坐标或夹具改变，请重新确认切断接料配置')
    if data.get('execution_revision')!=EXECUTION_REVISION:raise ValueError('切断验证器版本改变，请重新确认并试算')
    canonical={k:v for k,v in data.items() if k!='revision'}
    if hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest()!=data.get('revision'):
        raise ValueError('切断接料配置内容与版本不一致')
    if operation.parameters.get('cutoff_setup_revision')!=data['revision']:raise ValueError('工序未绑定当前切断接料配置')
    return data


def bound_cutoff_inputs(job,operation):
    from .main import _load_tool_inventory,job_directory
    setup=load_cutoff_setup(job,operation)
    if not setup:raise ValueError('尚未配置切断刀运动与背轴夹持依据')
    cfg=CutoffSetup(**setup['configuration']);radial=load_setup(job)
    record=next((r for r in _load_tool_inventory(job.machine_instance_id) if r.inventory_id==cfg.inventory_id),None)
    if record and record.machine_instance_id!=job.machine_instance_id:raise ValueError('切断实刀记录不属于任务绑定的机床')
    geometry=tool_geometry(record)
    if operation.tool.inventory_id!=cfg.inventory_id or cfg.station!=record.station:
        raise ValueError('切断工序实刀编号或实际刀位已改变')
    if abs(operation.tool.cutting_width_mm-geometry['cutting_width_mm'])>1e-6:
        raise ValueError('切断工序刃宽与实测刀具不一致')
    if geometry!=setup['tool_geometry']:raise ValueError('切断刀实测尺寸改变，请重新确认并试算')
    fixture=radial.get('fixture')
    if not fixture:raise ValueError('需上传包含实际夹持部件的机床坐标夹具实体')
    root=job_directory(job.id).resolve();path=(root/fixture['relative_path']).resolve()
    if root not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=fixture['sha256']:
        raise ValueError('夹具实体文件与绑定版本不一致')
    return cfg,radial,geometry,str(path),setup['revision']


def travel_report(radial,axis,z_mm,end_radius,stock_radius):
    from .radial_setup import RadialSetup
    cfg=RadialSetup(**radial['configuration']);mapping=frame(cfg,axis)
    scale=2 if cfg.x_programming=='diameter' else 1
    origin=cfg.machine_origin;start=cfg.retract_x_mm/scale
    points=[[start,origin[1],origin[2]+z_mm],[origin[0]+end_radius,origin[1],origin[2]+z_mm]]
    errors=[]
    if start<=origin[0]+stock_radius+1:errors.append('切断入口X未在毛坯外保留安全距离')
    for p in points:
        for key,value in zip(('X1','Y1','Z1'),(p[0]*scale,p[1],p[2])):
            low,high=cfg.axis_limits[key]
            if not low<=value<=high:errors.append(f'切断{key}超出声明的有效行程')
    return {'status':'failed' if errors else 'passed','errors':errors,'mapping':mapping,
        'tip_points_machine_xyz':points,'commanded_points':[[p[0]*scale,p[1],p[2]] for p in points],
        'scope':'configured_radial_infeed_and_retract_not_controller_post_validation'}


@router.get('/api/v1/jobs/{job_id}/cutoff-setup/{operation_id}')
def get_setup(job_id:str,operation_id:str):
    from .main import load_job
    job=load_job(job_id)
    try:
        path=path_for(job,operation_id)
        # Return stale data for deliberate re-confirmation; no stale trial can pass.
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'status':'not_configured','production_ready':False}
    except ValueError as error:raise HTTPException(422,str(error)) from error


@router.put('/api/v1/jobs/{job_id}/cutoff-setup/{operation_id}')
def save_setup(job_id:str,operation_id:str,cfg:CutoffSetup):
    from .main import load_job,_load_tool_inventory,_save_manual_plan_change,write_json
    from .tool_inventory import bind_verified_inventory_tool
    job=load_job(job_id)
    try:
        path=path_for(job,operation_id)
        previous=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if cfg.expected_revision!=previous.get('revision'):raise HTTPException(409,'切断配置已改变，请刷新后保存')
        radial=load_setup(job)
        if not radial or not radial.get('fixture'):raise ValueError('请先保存机床坐标并上传实际夹具STEP')
        operation=next(o for s in job.plan.setups for o in s.operations if o.id==operation_id)
        if operation.workpiece_side=='back' or operation.spindle_id=='sub':raise ValueError('当前切断执行器只支持主轴正面切断')
        record=next((r for r in _load_tool_inventory(job.machine_instance_id) if r.inventory_id==cfg.inventory_id),None)
        if record and record.machine_instance_id!=job.machine_instance_id:raise ValueError('切断实刀记录不属于任务绑定的机床')
        geometry=tool_geometry(record)
        if cfg.station!=record.station:raise ValueError('配置刀位必须与实刀登记刀位一致')
        required=operation.tool.model_copy(update={'cutting_width_mm':geometry['cutting_width_mm']})
        tool=bind_verified_inventory_tool(record,required)
        data={'configuration':cfg.model_dump(exclude={'expected_revision'}),'radial_setup_revision':radial['revision'],
            'tool_geometry':geometry,'execution_revision':EXECUTION_REVISION,'production_ready':False}
        data['revision']=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        operation.tool=tool;operation.parameters.update(cutoff_setup_revision=data['revision'],cutting_width_mm=geometry['cutting_width_mm'])
        operation.status='proposed';operation.generation_state='dirty'
        write_json(path,data);_save_manual_plan_change(job_id,job)
        return data
    except (ValueError,TypeError) as error:raise HTTPException(422,str(error)) from error
