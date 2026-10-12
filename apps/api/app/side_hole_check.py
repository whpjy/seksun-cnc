"""Isolated exact solid check; never promotes reference holes to accepted operations."""
import json
import re
import subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import FileResponse
from .side_hole_tool import SideDrillGeometry, prepare_drill_geometry, assess_side_hole_machine
from .side_hole_readiness import side_hole_readiness

router=APIRouter()


def check_material_context(job, rotational, snapshot, state):
    from .agent.l32_operation_trial import evidence_context_signature, reconcile_state, _simulation_range, _reference_profile, NONROTATIONAL_OPERATION_TYPES
    context=evidence_context_signature(job,rotational,snapshot)
    accepted=reconcile_state(job,state,context)["accepted"]
    operations={o.id:o for s in job.plan.setups for o in s.operations}
    if any(operations[r["operation_id"]].type in NONROTATIONAL_OPERATION_TYPES for r in accepted):
        raise HTTPException(409,"前序已有非回转加工，尚无可复用的连续实体状态；不能用回转包络补回已去除材料")
    samples=(accepted[-1].get("cumulative_simulation") or {}).get("samples") if accepted else None
    if accepted and not samples:
        raise HTTPException(409,"前序材料记录不完整，请先重算")
    return {"samples":samples,"z_range_mm":list(_simulation_range(job,_reference_profile(job,rotational))),
            "stock_source":"accepted_turning_envelope" if accepted else "initial_solid_blank",
            "bound_machine_instance_id":job.machine_instance_id,"bound_machine_configuration_hash":job.machine_configuration_hash,
            "accepted_operation_ids":[r["operation_id"] for r in accepted],"context_signature":context}


@router.post("/api/v1/jobs/{job_id}/side-hole-check")
def check_side_holes(job_id:str,feature_ids:str,tool_diameter_mm:float|None=None,drill_geometry:SideDrillGeometry|None=Body(default=None)):
    from .main import (job_directory, load_machine_snapshot, get_side_hole_draft, cached_preview_signature,
                       run_freecad_adapter, FREECAD_CMD, APP_ROOT,
                       L32_OPERATION_TRIALING_JOBS, L32_ROLLING_LOOP_JOBS, CAM_STREAMING_JOBS, AI_REVIEWING_JOBS)
    from .stock_selection import _job_stock
    if job_id in L32_OPERATION_TRIALING_JOBS or job_id in L32_ROLLING_LOOP_JOBS or job_id in CAM_STREAMING_JOBS or job_id in AI_REVIEWING_JOBS:
        raise HTTPException(409,"前序材料正在变化，请等待工序计算结束")
    job,rotational,_,axis=_job_stock(job_id)
    if job.status != "completed" or not job.plan:
        raise HTTPException(409,"请先建立毛坯和工艺草案")
    directory=job_directory(job_id)
    machine_path=directory/"machine-configuration.json"
    if not machine_path.is_file():
        raise HTTPException(409,"缺少任务的机床配置快照")
    state_path=directory/"harness-l32-operation-state.json"
    state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    snapshot=load_machine_snapshot(machine_path)
    context=check_material_context(job,rotational,snapshot,state)
    draft=get_side_hole_draft(job_id,feature_ids,tool_diameter_mm)
    if drill_geometry:
        try:
            draft=prepare_drill_geometry(draft,drill_geometry)
        except ValueError as error:
            raise HTTPException(422,str(error)) from error
    machine=assess_side_hole_machine(job,snapshot,draft,drill_geometry.station_group if drill_geometry else "gang")
    payload={**context,"draft":draft,"axis":axis,"stock_diameter_mm":float(job.plan.stock["diameter_mm"])}
    source=directory/job.filename
    if not source.is_file():
        raise HTTPException(404,"原始 STEP 不存在")
    signature=cached_preview_signature("side-hole-check-v4",source,payload)
    output=directory/"side-hole-checks"/signature
    cache=output/"evidence.json"
    if cache.is_file() and (output/"remaining-stock.stl").is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    # A unique sandbox prevents concurrent checks from sharing partial artifacts.
    import tempfile
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pending-",dir=output.parent) as sandbox:
        try:
            completed=run_freecad_adapter(FREECAD_CMD,APP_ROOT/"cam"/"side_hole_sweep.py",
                [source,json.dumps(payload),sandbox],timeout_seconds=180)
            line=next(line.split("CNC_SIDE_HOLE_CHECK ",1)[1] for line in completed.stdout.splitlines() if "CNC_SIDE_HOLE_CHECK " in line)
            result=json.loads(line)
        except subprocess.TimeoutExpired as error:
            raise HTTPException(504,"侧孔实体检查超时，尚未产生有效验证结果") from error
        except (OSError,subprocess.CalledProcessError,StopIteration,ValueError) as error:
            raise HTTPException(502,"侧孔实体检查失败，未更改工序接受状态") from error
        current_state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        current_job,current_rotational,_,_=_job_stock(job_id)
        current=check_material_context(current_job,current_rotational,load_machine_snapshot(machine_path),current_state)
        if current != context:
            raise HTTPException(409,"计算期间图纸、工序或前序材料已改变，请重新检查")
        result.update({"readiness":side_hole_readiness(result,draft,machine,context),"revision":signature,"coordinate_frame":"source_step_xyz_mm","stock_source":context["stock_source"],
            "geometry_scope":draft.get("geometry_scope","cylindrical_reference"),"drill_geometry":draft.get("drill_geometry"),
            "machine_assessment":machine,"hole_depths":[{k:h[k] for k in ("feature_id","end","tip_end","tip_length_mm","conservative_reach_length_mm","flute_reach_status") if k in h} for h in draft["holes"]],
            "accepted_operation_ids":context["accepted_operation_ids"],"can_accept":False,"production_ready":False,"nc_generated":False,
            "stock_mesh_url":f"/api/v1/jobs/{job_id}/side-hole-checks/{signature}/remaining-stock.stl",
            "limitations":["已加入参考锥形钻尖，刀杆与刀柄仅在尺寸完整时检查，夹具尚未验证" if drill_geometry else "圆柱刀具体积检查，不含钻尖、刀柄和夹具；不能作为工序接受或孔加工完成证据",
                "前序车削材料由已接受的采样包络保守重建，非完整连续实体求解" if context["samples"] else "本次从初始实心毛坯试算，未执行其他工序",
                "实际刀具几何、钻深、机床坐标及二维公差仍须确认；不计入正式接受和孔覆盖"]})
        import shutil
        output.mkdir(parents=True,exist_ok=True)
        for name in ("remaining-stock.stl","remaining-stock.brep","result.json"):
            shutil.copyfile(Path(sandbox)/name,output/name)
        completed_cache=Path(sandbox)/"evidence.json"
        completed_cache.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        completed_cache.replace(cache)
    return result


@router.get("/api/v1/jobs/{job_id}/side-hole-checks/{revision}/remaining-stock.stl")
def side_hole_stock_mesh(job_id:str,revision:str):
    from .main import job_directory
    if not re.fullmatch(r"[a-f0-9]{64}",revision):
        raise HTTPException(404,"材料快照不存在")
    path=job_directory(job_id)/"side-hole-checks"/revision/"remaining-stock.stl"
    if not path.is_file():
        raise HTTPException(404,"材料快照不存在")
    return FileResponse(path,media_type="model/stl",filename="side-hole-remaining-stock.stl")
