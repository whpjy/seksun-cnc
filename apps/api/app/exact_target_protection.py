"""Full STEP containment gate in addition to one-dimensional profile checks."""
import json
import subprocess
import tempfile
from fastapi import APIRouter, HTTPException
from .machining_geometry import axial_hole, job_axis

router = APIRouter()


def required(job, geometry):
    axis = job_axis(job.model_dump(mode="json"), geometry.model_dump(mode="json"))
    return bool(job.analysis and (job.analysis.prismatic_features or any(
        f.kind == "hole" and not axial_hole(f.model_dump(mode="json"), axis)
        for f in job.analysis.cylindrical_features)))


def guard_compiled(job, geometry, result):
    if not required(job,geometry):
        return
    frame = next(f for f in result.coordinate_frames if f.channel_id == "sub")
    samples = [{"z":frame.source_cutoff_z_mm-s.z,"outer_radius":s.outer_radius,"inner_radius":s.inner_radius}
               for s in result.continuous_simulation.sub_frame_final.samples]
    samples.sort(key=lambda s:s["z"])
    report = check_material(job,geometry,[{"operation_id":"WHOLE_PROGRAM_FINAL","samples":samples}])
    if report["status"] != "passed":
        raise ValueError("full_step_target_protection_failed: final material is missing STEP target geometry")


def check_material(job, geometry, stages):
    from .main import job_directory, cached_preview_signature, run_freecad_adapter, FREECAD_CMD, APP_ROOT
    from .agent.l32_operation_trial import _simulation_range, _reference_profile
    directory = job_directory(job.id)
    source = directory / job.filename
    if not source.is_file():
        raise ValueError("Full STEP protection requires the original source solid")
    payload = {"axis":job_axis(job.model_dump(mode="json"),geometry.model_dump(mode="json")),
        "stock_diameter_mm":float(job.plan.stock["diameter_mm"]),
        "z_range_mm":list(_simulation_range(job,_reference_profile(job,geometry))),
        "radial_tolerance_mm":.05,"stages":stages}
    payload["selected_solid_index"] = int(job.analysis.topology.get("selected_solid_index",1))
    if not payload["axis"]:
        raise ValueError("Full STEP protection requires the bound rotational axis")
    signature = cached_preview_signature("full-step-protection-v6-preserved-material",source,payload)
    cache_dir = directory / "exact-target-protection"
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f"{signature}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(dir=cache_dir) as scratch:
        from pathlib import Path
        input_path = Path(scratch) / "input.json"
        input_path.write_text(json.dumps(payload),encoding="utf-8")
        completed = run_freecad_adapter(FREECAD_CMD,APP_ROOT/"cam"/"l32_target_protection.py",
            [source,input_path],timeout_seconds=240)
        report = json.loads(next(line.split("CNC_TARGET_PROTECTION ",1)[1] for line in completed.stdout.splitlines() if "CNC_TARGET_PROTECTION " in line))
        report["revision"] = signature
        result_path = Path(scratch) / "report.json"
        result_path.write_text(json.dumps(report,ensure_ascii=False),encoding="utf-8")
        result_path.replace(cache)
    return report


def guard_trial(job, geometry, result, state):
    if not result.get("can_accept") or not required(job,geometry):
        return result
    evidence=result.get('evidence') or {}
    if evidence.get('material_domain')=='exact_3d_occ':
        try:
            from .main import job_directory
            from .radial_drilling_trial import bound_solid,inherited_protection,physical_tool_matches
            from .radial_setup import load_setup
            from .side_hole_tool import SideDrillGeometry,SideFlatGeometry
            path=bound_solid(job_directory(job.id),result,result.get('context_signature'))
            stored=json.loads((path.parent/'evidence.json').read_text(encoding='utf-8'))
            native=evidence.get('native_result') or {}
            if stored.get('native_result')!=native or stored.get('material_state')!=evidence.get('material_state'):
                raise ValueError('Stored native material evidence differs from the trial')
            previous=(state or {}).get('accepted') or []
            initial=inherited_protection(previous[-1] if previous else None)
            raw_initial=native.get('initial_missing_target_mm3')
            if initial['status']!='passed' and (raw_initial is None or raw_initial>.0001):
                raise ValueError('Initial 3D material protection has no valid evidence')
            if native.get('target_protection_status')!='passed' or native.get('target_contact_mm3',1)>.0001:
                raise ValueError('Native cutting sweep contacts the STEP target')
            operation=next(o for s in job.plan.setups for o in s.operations if o.id==result['operation_id'])
            setup=load_setup(job)
            if not setup or operation.parameters.get('radial_setup_revision')!=setup['revision']:
                raise ValueError('Radial coordinates changed or are missing')
            tool=(SideFlatGeometry if operation.type=='radial_hole_finishing' else SideDrillGeometry)(**evidence['draft']['drill_geometry'])
            station=setup['configuration'].get('operation_stations',{}).get(operation.id,setup['configuration']['station'])
            if not physical_tool_matches(job,operation,tool,station):
                raise ValueError('Physical tool confirmation changed or does not match')
            report={'status':'passed','scope':'bound_3d_solid_zero_target_contact_with_initial_protection',
                'initial_material_protection':initial,'raw_initial_missing_target_mm3':raw_initial,
                'target_contact_mm3':native['target_contact_mm3'],'material_sha256':evidence['material_state']['sha256']}
        except (OSError,ValueError,KeyError,StopIteration) as error:
            return {**result,'status':'blocked','can_accept':False,'reason':'bound_3d_target_protection_unavailable',
                'detail':str(error),'evidence':{**evidence,'exact_target_protection':{'status':'unavailable','scope':'exact_3d_occ'}}}
        return {**result,'evidence':{**evidence,'exact_target_protection':report}}
    material = result.get("cumulative_simulation")
    if not material:
        return result
    previous = (state or {}).get("accepted", [])
    stages = [{"operation_id": "BEFORE", "samples": previous[-1]["cumulative_simulation"]["samples"] if previous else None},
              {"operation_id":result["operation_id"],"samples":material["samples"]}]
    try:
        report = check_material(job,geometry,stages)
    except (OSError,ValueError,StopIteration,subprocess.SubprocessError) as error:
        report = {"status":"unavailable","detail":"完整 STEP 实体保护检查未产生有效证据。",
                  "executor_error_type":type(error).__name__}
    evidence = {**(result.get("evidence") or {}),"exact_target_protection":report}
    if report["status"] != "passed":
        earlier = any(s["operation_id"]=="BEFORE" and s["status"]=="failed" for s in report.get("stages",[]))
        return {**result,"evidence":evidence,"status":"blocked","can_accept":False,
            "reason":"full_step_target_protection_unavailable" if report["status"]=="unavailable" else "previous_step_target_damage" if earlier else "full_step_target_protection_failed",
            "detail":"完整 STEP 检查引擎未返回有效证据，不能判断是否过切；先复查执行引擎。" if report["status"]=="unavailable" else "前序材料已缺少 STEP 成品实体，先定位并重算前序。" if earlier else "二维轮廓试算不能证明完整 STEP 成品未被过切。",
            "next_action":"inspect_l32_material_protection_then_replan"}
    return {**result,"evidence":evidence}


@router.post("/api/v1/jobs/{job_id}/agent/l32/material-protection")
def audit_material(job_id: str):
    from .main import load_job,job_directory,load_machine_snapshot,write_json
    from .rotational_features import RotationalFeatureAnalysis
    from .agent.l32_operation_trial import reconcile_state,evidence_context_signature
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(409,"需要 L32 工艺及材料状态")
    directory = job_directory(job_id)
    geometry = RotationalFeatureAnalysis.model_validate_json((directory/"rotational-features.json").read_text(encoding="utf-8"))
    snapshot = load_machine_snapshot(directory/"machine-configuration.json")
    context = evidence_context_signature(job,geometry,snapshot)
    path = directory/"harness-l32-operation-state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    accepted = reconcile_state(job,state,context)["accepted"]
    if any((r.get('evidence') or {}).get('material_state') for r in accepted):
        # Never rebuild radial holes from an unchanged rotational envelope.
        try:
            from .radial_drilling_trial import bound_solid
            for r in accepted:
                if (r.get('evidence') or {}).get('material_state'):bound_solid(directory,r,context)
        except ValueError as error:raise HTTPException(409,str(error)) from error
        return {'status':'review','job_id':job_id,'context_signature':context,'production_ready':False,
            'scope':'exact_3d_occ','detail':'已存在三维连续材料；回转包络不能证明孔底及成品完整性。请核对各道已绑定的实体试算证据。',
            'stages':[{'operation_id':r['operation_id'],'status':(r.get('evidence') or {}).get('exact_target_protection',{}).get('status','review')} for r in accepted]}
    try:
        report = check_material(job,geometry,[{"operation_id":"INITIAL","samples":None}]+[
            {"operation_id":r["operation_id"],"samples":r["cumulative_simulation"]["samples"]} for r in accepted])
    except (OSError,ValueError,StopIteration,subprocess.SubprocessError) as error:
        raise HTTPException(502,"完整 STEP 材料检查未完成，未修改工序接受状态") from error
    report = {**report,"job_id":job_id,"context_signature":context,
        "accepted_prefix_signatures":[r["operation_signature"] for r in accepted],
        "first_failed_operation_id":next((s["operation_id"] for s in report["stages"] if s["status"]=="failed"),None),
        "production_ready":False}
    current_job = load_job(job_id)
    current_geometry = RotationalFeatureAnalysis.model_validate_json((directory/"rotational-features.json").read_text(encoding="utf-8"))
    current_context = evidence_context_signature(current_job,current_geometry,load_machine_snapshot(directory/"machine-configuration.json"))
    current_state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    current_prefix = reconcile_state(current_job,current_state,current_context)["accepted"]
    if current_context != context or [r["operation_signature"] for r in current_prefix] != report["accepted_prefix_signatures"]:
        raise HTTPException(409,"检查期间工序或材料改变，需要重新检查")
    write_json(directory/"exact-target-protection-latest.json",report)
    return report
