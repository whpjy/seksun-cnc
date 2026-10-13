"""Task-bound radial coordinates and measured travel; no assumed fixture approval."""
import hashlib,json,math
from typing import Literal
from pydantic import BaseModel,Field,model_validator
from fastapi import APIRouter,HTTPException,UploadFile,File
router=APIRouter()


class RadialSetup(BaseModel):
    source_zero_direction:Literal['+X','-X','+Y','-Y','+Z','-Z']
    machine_origin:list[float]=Field(min_length=3,max_length=3)
    x_programming:Literal['radius','diameter']
    axis_limits:dict[str,list[float]]
    retract_x_mm:float=Field(gt=0,allow_inf_nan=False)
    module_id:str=Field(min_length=1,max_length=64)
    station:str=Field(min_length=1,max_length=64)
    operation_stations:dict[str,str]=Field(default_factory=dict)
    evidence:str=Field(min_length=1,max_length=1000)
    fixture_attachment:Literal['stationary','main_spindle']='stationary'
    expected_revision:str|None=None

    @model_validator(mode='after')
    def valid(self):
        if not self.evidence.strip() or not self.station.strip():raise ValueError('请填写刀位和坐标/行程依据')
        if any(not k.strip() or not v.strip() or len(v)>64 for k,v in self.operation_stations.items()):raise ValueError('工序刀位不能为空')
        if not all(math.isfinite(v) for v in self.machine_origin):raise ValueError('原点必须为有限数值')
        if set(self.axis_limits)!={'X1','Y1','Z1'}:raise ValueError('必须提供X1、Y1、Z1实测行程边界')
        for bounds in self.axis_limits.values():
            if len(bounds)!=2 or not all(math.isfinite(v) for v in bounds) or bounds[0]>=bounds[1]:raise ValueError('行程下限必须小于上限')
        return self


def frame(setup,axis):
    a=[float(axis['direction'][k]) for k in 'xyz'];norm=math.sqrt(sum(v*v for v in a))
    if norm<1e-9:raise ValueError('无效主轴方向')
    a=[v/norm for v in a]
    zero=[0.,0.,0.];zero['XYZ'.index(setup.source_zero_direction[1])]=1 if setup.source_zero_direction[0]=='+' else -1
    if abs(sum(x*y for x,y in zip(a,zero)))>.001:raise ValueError('零度径向必须与主轴垂直')
    y=[a[1]*zero[2]-a[2]*zero[1],a[2]*zero[0]-a[0]*zero[2],a[0]*zero[1]-a[1]*zero[0]]
    source=[axis['origin'][k] for k in 'xyz'];rotation=[zero,y,a]
    translation=[setup.machine_origin[i]-sum(rotation[i][j]*source[j] for j in range(3)) for i in range(3)]
    return {'rotation':rotation,'translation':translation,'axis_origin':setup.machine_origin}


def point(value,mapping):
    return [sum(mapping['rotation'][i][j]*value['xyz'[j]] for j in range(3))+mapping['translation'][i] for i in range(3)]


def check_poses(setup,axis,draft,stock_radius):
    mapping=frame(setup,axis);poses=[];errors=[]
    factor=2 if setup.x_programming=='diameter' else 1
    retract=setup.retract_x_mm/factor
    if retract < setup.machine_origin[0]+stock_radius+max(h['tool_diameter_mm']/2 for h in draft['holes'])+1:
        errors.append('退刀X未超过毛坯与刀具的保守安全半径')
    for h in draft['holes']:
        d=h['outward_direction'];normal=[sum(mapping['rotation'][i][j]*d['xyz'[j]] for j in range(3)) for i in range(3)]
        angle=-math.degrees(math.atan2(normal[1],normal[0]));c,s=math.cos(math.radians(angle)),math.sin(math.radians(angle))
        def indexed(v):
            p=point(v,mapping);o=setup.machine_origin;x,y=p[0]-o[0],p[1]-o[1]
            return [o[0]+c*x-s*y,o[1]+s*x+c*y,p[2]]
        entry=indexed(h.get('tip_entry',h['entry']));end=indexed(h.get('tip_end',h['end']))
        if retract<entry[0]:errors.append(f"{h['feature_id']} 退刀X不能小于进给入口X")
        commanded=[[p[0]*factor,p[1],p[2]] for p in (entry,end)]+[[setup.retract_x_mm,entry[1],entry[2]]]
        for p in commanded:
            for name,value in zip(('X1','Y1','Z1'),p):
                low,high=setup.axis_limits[name]
                if not low<=value<=high:errors.append(f"{h['feature_id']} {name}超出已配置行程")
        poses.append({'feature_id':h['feature_id'],'index_rotation_deg':angle,'entry_machine_xyz':entry,'end_machine_xyz':end,
            'retract_machine_xyz':[retract,entry[1],entry[2]],'commanded_points':commanded})
    return {'status':'failed' if errors else 'passed','errors':list(dict.fromkeys(errors)),'mapping':mapping,'poses':poses,
        'scope':'configured_tip_coordinates_and_travel_not_controller_post_validation'}


def load_setup(job):
    from .main import job_directory
    d=job_directory(job.id);path=d/'radial-setup.json'
    if not path.is_file():return None
    data=json.loads(path.read_text(encoding='utf-8'))
    if data['machine_configuration_hash']!=job.machine_configuration_hash or data['source_sha256']!=hashlib.sha256((d/job.filename).read_bytes()).hexdigest():
        raise ValueError('径向配置与当前STEP或机床快照不一致，请重新核对')
    return data


@router.get('/api/v1/jobs/{job_id}/radial-setup')
def get_setup(job_id:str):
    from .main import load_job
    try:data=load_setup(load_job(job_id))
    except ValueError as error:raise HTTPException(409,str(error)) from error
    return data or {'status':'not_configured','production_ready':False}


def save_config(job,setup,fixture=None):
    from .main import job_directory,save_job,write_json
    from .machining_geometry import job_axis
    from .rotational_features import RotationalFeatureAnalysis
    d=job_directory(job.id);path=d/'radial-setup.json'
    existing=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    if setup.expected_revision != (existing or {}).get('revision'):raise HTTPException(409,'径向配置已改变，请刷新后保存')
    g=RotationalFeatureAnalysis.model_validate_json((d/'rotational-features.json').read_text(encoding='utf-8'))
    axis=job_axis(job.model_dump(mode='json'),g.model_dump(mode='json'))
    frame(setup,axis)
    if not set(setup.operation_stations)<={o.id for s in job.plan.setups for o in s.operations}:
        raise ValueError('刀位映射引用了不存在的工序')
    source_hash=hashlib.sha256((d/job.filename).read_bytes()).hexdigest()
    retained=(existing or {}).get('fixture') if (existing or {}).get('source_sha256')==source_hash and (existing or {}).get('machine_configuration_hash')==job.machine_configuration_hash else None
    data={'configuration':setup.model_dump(exclude={'expected_revision'}),'fixture':fixture if fixture is not None else retained,
        'machine_instance_id':job.machine_instance_id,'machine_configuration_hash':job.machine_configuration_hash,
        'source_sha256':source_hash,'production_ready':False}
    data['revision']=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    for s in job.plan.setups:
        for o in s.operations:
            if o.type in {'radial_drilling','radial_hole_finishing','turn_cutoff'}:o.parameters['radial_setup_revision']=data['revision']
    write_json(d/'radial-setup.json',data)
    from .main import _save_manual_plan_change
    _save_manual_plan_change(job.id,job)
    return data


@router.put('/api/v1/jobs/{job_id}/radial-setup')
def put_setup(job_id:str,setup:RadialSetup):
    from .main import load_job
    job=load_job(job_id)
    if not job.plan or job.device_id!='citizen-cincom-l32':raise HTTPException(409,'需要L32工艺草案')
    try:return save_config(job,setup)
    except ValueError as error:raise HTTPException(422,str(error)) from error


@router.post('/api/v1/jobs/{job_id}/radial-setup/fixture')
async def upload_fixture(job_id:str,file:UploadFile=File(...)):
    from .main import load_job,job_directory
    job=load_job(job_id)
    try:current=load_setup(job)
    except ValueError as error:raise HTTPException(409,str(error)) from error
    if not current:raise HTTPException(409,'请先保存径向坐标和刀位配置')
    if not (file.filename or '').lower().endswith(('.step','.stp')):raise HTTPException(422,'夹具必须是STEP实体，坐标为机床XYZ毫米')
    content=await file.read(20*1024*1024+1)
    if not content or len(content)>20*1024*1024:raise HTTPException(422,'夹具文件需为非空且不超过20MB')
    sha=hashlib.sha256(content).hexdigest();d=job_directory(job_id);dest=d/'radial-fixtures';dest.mkdir(exist_ok=True)
    path=dest/f'{sha}.step';path.write_bytes(content)
    setup=RadialSetup(**current['configuration'],expected_revision=current['revision'])
    return save_config(job,setup,{'relative_path':str(path.relative_to(d)),'sha256':sha,'coordinate_frame':'machine_xyz_mm'})


class ToolBinding(BaseModel):
    inventory_id:str=Field(min_length=1,max_length=64)
    expected_revision:str


@router.put('/api/v1/jobs/{job_id}/radial-setup/operations/{operation_id}/tool')
def bind_tool(job_id:str,operation_id:str,request:ToolBinding):
    from .main import load_job,job_directory,_load_tool_inventory,_save_manual_plan_change
    from .tool_inventory import bind_verified_inventory_tool
    from .side_hole_tool import SideDrillGeometry,SideFlatGeometry
    job=load_job(job_id)
    try:setup=load_setup(job)
    except ValueError as error:raise HTTPException(409,str(error)) from error
    if not setup or setup['revision']!=request.expected_revision:raise HTTPException(409,'请先刷新并保存当前侧孔机床配置')
    operation=next((o for s in job.plan.setups for o in s.operations if o.id==operation_id),None)
    if not operation or operation.type not in {'radial_drilling','radial_hole_finishing'}:raise HTTPException(422,'仅支持当前侧孔钻削与平底修整工序')
    record=next((r for r in _load_tool_inventory(job.machine_instance_id) if r.inventory_id==request.inventory_id),None)
    station=setup['configuration'].get('operation_stations',{}).get(operation_id,setup['configuration']['station'])
    if not record or not record.physical_geometry_verified or record.station!=station:raise HTTPException(422,'实刀必须已确认完整几何，且刀位与当前工序配置一致')
    if operation.type=='radial_hole_finishing' and not record.center_cutting_verified:raise HTTPException(422,'平底修整刀具必须确认可中心切削')
    try:
        tool=bind_verified_inventory_tool(record,operation.tool)
        values={'flute_length_mm':record.measured_flute_length_mm,'stickout_from_tip_mm':record.measured_stickout_mm,
            'shank_diameter_mm':record.measured_shank_diameter_mm,'holder_diameter_mm':record.measured_holder_diameter_mm,
            'holder_length_mm':record.measured_holder_length_mm}
        if operation.type=='radial_drilling':values['point_angle_deg']=record.measured_point_angle_deg
        (SideDrillGeometry if operation.type=='radial_drilling' else SideFlatGeometry)(**values,evidence=record.capability_verification_reference)
        operation.tool=tool;operation.parameters.update(values,radial_setup_revision=setup['revision'],
            drill_geometry_evidence=f'{record.capability_verified_by}: {record.capability_verification_reference}')
        operation.status='proposed';operation.generation_state='dirty'
        return _save_manual_plan_change(job_id,job)
    except ValueError as error:raise HTTPException(422,str(error)) from error
