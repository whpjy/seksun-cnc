"""Compare nominal-CAD hole-bottom strategies without accepting operations."""
import hashlib
import json
import shutil
import subprocess
import tempfile
from math import tan,radians
from pathlib import Path
from fastapi import APIRouter,Body,HTTPException
from .side_hole_tool import SideDrillGeometry,prepare_drill_geometry
from .side_hole_check import check_material_context

router=APIRouter()


def strategy_drafts(draft,geometry):
    full=prepare_drill_geometry(draft,geometry)
    holes=[];offsets=[]
    for h in draft['holes']:
        backoff=h['tool_diameter_mm']/2/tan(radians(geometry.point_angle_deg/2))
        backed=prepare_drill_geometry({**draft,'holes':[h]},geometry.model_copy(update={'full_diameter_extension_mm':-backoff}))
        holes.extend(backed['holes']);offsets.append({'feature_id':h['feature_id'],'backoff_mm':backoff})
    backed={**full,'holes':holes,'drill_geometry':{**full['drill_geometry'],'full_diameter_extension_mm':0}}
    # Segment-end bookkeeping in the OCC checker must reverse each hole's
    # actual extension, including comparisons with mixed nominal diameters.
    for h,offset in zip(backed['holes'],offsets):h['full_diameter_extension_mm']=-offset['backoff_mm']
    flat={**draft,'geometry_scope':'flat_bottom_reference','holes':[dict(h) for h in draft['holes']]}
    return {'direct':full,'backoff':backed,'flat_finish':flat,'backoffs':offsets}


@router.post('/api/v1/jobs/{job_id}/side-hole-strategies')
def compare_side_hole_strategies(job_id:str,feature_ids:str,tool_diameter_mm:float|None=None,geometry:SideDrillGeometry=Body(...)):
    from .main import job_directory,load_machine_snapshot,get_side_hole_draft,cached_preview_signature,run_freecad_adapter,FREECAD_CMD,APP_ROOT,write_json
    from .stock_selection import _job_stock
    job,rotational,_,axis=_job_stock(job_id)
    directory=job_directory(job_id)
    state_path=directory/'harness-l32-operation-state.json'
    def current_context():
        current,current_rotational,_,_=_job_stock(job_id)
        state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else None
        return check_material_context(current,current_rotational,load_machine_snapshot(directory/'machine-configuration.json'),state)
    context=current_context()
    try: drafts=strategy_drafts(get_side_hole_draft(job_id,feature_ids,tool_diameter_mm),geometry)
    except ValueError as error:raise HTTPException(422,str(error)) from error
    payload={**context,'axis':axis,'stock_diameter_mm':float(job.plan.stock['diameter_mm']),'drafts':drafts}
    operations=[o for s in job.plan.setups for o in s.operations if o.enabled]
    kerf=next((o for o in operations if o.type=='turn_cutoff' and o.workpiece_side=='front'),None)
    if kerf and all(isinstance(kerf.parameters.get(k),(float,int)) for k in ('z_mm','cutting_width_mm')):
        payload['cutoff']={k:kerf.parameters[k] for k in ('z_mm','cutting_width_mm')}
    signature=cached_preview_signature('side-hole-strategies-v3-isolated-candidate-errors',directory/job.filename,payload)
    output=directory/'side-hole-strategies'/signature
    cache=output/'evidence.json'
    if cache.is_file():return json.loads(cache.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        try:
            completed=run_freecad_adapter(FREECAD_CMD,APP_ROOT/'cam'/'side_hole_strategy.py',[directory/job.filename,json.dumps(payload),scratch],timeout_seconds=240)
            result=json.loads(next(l.split('CNC_SIDE_HOLE_STRATEGY ',1)[1] for l in completed.stdout.splitlines() if 'CNC_SIDE_HOLE_STRATEGY ' in l))
        except (OSError,ValueError,StopIteration,subprocess.SubprocessError) as error:
            raise HTTPException(502,'孔底方案比较未产生有效实体证据，工序接受状态未改变') from error
        if current_context()!=context:raise HTTPException(409,'比较期间前序材料或任务上下文改变，请重算')
        output.mkdir(parents=True,exist_ok=True)
        records=[(r['id'],r) for r in result['strategies']]
        if result.get('cutoff_continuation',{}).get('status')=='passed':records.append(('cutoff',result['cutoff_continuation']))
        for name,row in records:
            path=Path(scratch)/name/'remaining-stock.brep'
            if row.get('geometry_status')=='unavailable':continue
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            material=directory/'radial-material'/digest;material.mkdir(parents=True,exist_ok=True)
            for file in ('remaining-stock.brep','remaining-stock.stl'):shutil.copyfile(path.parent/file,material/file)
            row['stock_mesh_url']=f'/api/v1/jobs/{job_id}/radial-material/{digest}/remaining-stock.stl'
        result.update(job_id=job_id,revision=signature,backoffs=drafts['backoffs'],can_accept=False,production_ready=False,
            limitations=['比较仅以CAD名义孔圆柱段为目标；不自动确认图纸上下偏差、孔底或钻深。',
                '平底精加工使用候选圆柱切削体，未验证平底刀刃形、中心切削能力、刀柄、夹具及机床坐标。',
                '初始材料保护、实刀与设备缺项仍需核对；比较结果不能提升工序接受或孔制造覆盖。',
                '继承实体的切断仍为候选材料试算，未接入正式工序接受、转接或夹持验证。'])
        write_json(cache,result)
        return result
