"""Extend initial blank for main-spindle retention; never add stock after acceptance."""
import hashlib,json
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
router=APIRouter()

class BarHolding(BaseModel):
    operation_id:str
    grip_length_mm:float=Field(gt=0,le=200,allow_inf_nan=False)
    clamp_confirmed:bool=False
    verified_by:str=Field(min_length=1,max_length=120)
    evidence:str=Field(min_length=1,max_length=1000)
    expected_stock_signature:str

def stock_signature(stock):
    return hashlib.sha256(json.dumps(stock,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def extend_stock(stock,cut,request):
    width=float(cut.parameters.get('cutting_width_mm',cut.tool.cutting_width_mm or 0));z=float(cut.parameters['z_mm'])
    if width<=0:raise ValueError('切断刀缝宽度缺失')
    if not request.verified_by.strip() or not request.evidence.strip():raise ValueError('请填写主轴夹持确认人和依据')
    old=stock.get('main_bar_holding') or {}
    low=float(old.get('base_z_min_mm',stock['z_min_mm']));high=float(stock['z_max_mm'])
    # Current front-cut executor retains the product on +Z and the bar on -Z.
    grip_high=z-width/2;grip_low=grip_high-request.grip_length_mm
    updated={**stock,'z_min_mm':min(low,grip_low),'z_max_mm':high}
    updated['length_mm']=high-updated['z_min_mm']
    if updated['length_mm']>320:raise ValueError('含夹持段的毛坯超出L32已知长度范围')
    updated['main_bar_holding']={**request.model_dump(exclude={'expected_stock_signature'}),'base_z_min_mm':low,
        'grip_z_min_mm':grip_low,'grip_z_max_mm':grip_high,'cutoff_z_mm':z,'cutting_width_mm':width,
        'scope':'initial_solid_bar_on_negative_axial_side'}
    for key in ('verified_rotational_profile_ids','verified_hole_feature_ids','verified_prismatic_feature_ids','nonrotational_material_verified'):updated.pop(key,None)
    return updated

@router.get('/api/v1/jobs/{job_id}/bar-holding-stock')
def get_holding(job_id:str):
    from .main import load_job
    job=load_job(job_id)
    if not job.plan:raise HTTPException(409,'请先建立工艺草案')
    return {'stock_signature':stock_signature(job.plan.stock),'configuration':job.plan.stock.get('main_bar_holding'),
        'length_mm':job.plan.stock.get('length_mm'),'production_ready':False}

@router.put('/api/v1/jobs/{job_id}/bar-holding-stock')
def save_holding(job_id:str,request:BarHolding):
    from .main import load_job,_save_manual_plan_change,L32_OPERATION_TRIALING_JOBS,L32_ROLLING_LOOP_JOBS
    job=load_job(job_id)
    if not job.plan or job.device_id!='citizen-cincom-l32':raise HTTPException(409,'需要L32工艺草案')
    if job_id in L32_OPERATION_TRIALING_JOBS or job_id in L32_ROLLING_LOOP_JOBS:raise HTTPException(409,'正在试算，请完成后再修改初始毛坯')
    if stock_signature(job.plan.stock)!=request.expected_stock_signature:raise HTTPException(409,'毛坯已改变，请刷新后保存')
    cut=next((o for s in job.plan.setups for o in s.operations if o.id==request.operation_id and o.type=='turn_cutoff'),None)
    if not cut or cut.workpiece_side=='back' or cut.spindle_id=='sub':raise HTTPException(422,'请选择主轴正面切断工序')
    try:job.plan.stock=extend_stock(job.plan.stock,cut,request)
    except (ValueError,KeyError,TypeError) as error:raise HTTPException(422,str(error)) from error
    for s in job.plan.setups:
        for o in s.operations:o.status='proposed';o.generation_state='dirty'
    return _save_manual_plan_change(job_id,job)
