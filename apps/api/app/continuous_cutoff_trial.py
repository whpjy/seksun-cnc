"""Formal inherited-solid cutoff diagnostic; retention and tool travel remain gates."""
import hashlib,json,shutil,subprocess,tempfile
from pathlib import Path
from .radial_drilling_trial import bound_solid,inherited_protection


def cutoff_trial_evidence(job,operation):
    from .main import job_directory,load_machine_snapshot,run_freecad_adapter,FREECAD_CMD,APP_ROOT,write_json
    from .rotational_features import RotationalFeatureAnalysis
    from .machining_geometry import job_axis
    from .agent.l32_operation_trial import evidence_context_signature,reconcile_state,operation_signature
    root=job_directory(job.id)
    geometry=RotationalFeatureAnalysis.model_validate_json((root/'rotational-features.json').read_text(encoding='utf-8'))
    context=evidence_context_signature(job,geometry,load_machine_snapshot(root/'machine-configuration.json'))
    path=root/'harness-l32-operation-state.json'
    state=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    accepted=reconcile_state(job,state,context)['accepted']
    if not accepted or not (accepted[-1].get('evidence') or {}).get('material_state'):return None
    base={'machine_capability_status':'review','target_protection_status':'not_run','material_removal_status':'not_run',
        'operation_coverage_status':'not_run','production_ready':False,'material_domain':'exact_3d_occ'}
    try:
        previous=accepted[-1];stock=bound_solid(root,previous,context)
        if operation.tool.kind!='cutoff':raise ValueError('继承实体切断需使用切断刀')
        width=operation.parameters.get('cutting_width_mm',operation.tool.cutting_width_mm)
        if operation.tool.cutting_width_mm is None or abs(width-operation.tool.cutting_width_mm)>1e-6:
            raise ValueError('刀缝宽度必须与切断刀实际刃宽一致')
        payload={'input_stock_brep':str(stock),'axis':job_axis(job.model_dump(mode='json'),geometry.model_dump(mode='json')),
            'z_mm':operation.parameters['z_mm'],'cutting_width_mm':width,'stock_radius_mm':job.plan.stock['diameter_mm']/2}
        with tempfile.TemporaryDirectory(dir=root) as scratch:
            response=run_freecad_adapter(FREECAD_CMD,APP_ROOT/'cam'/'continuous_cutoff.py',[root/job.filename,json.dumps(payload),scratch],timeout_seconds=180)
            native=json.loads(next(l.split('CNC_CONTINUOUS_CUTOFF ',1)[1] for l in response.stdout.splitlines() if 'CNC_CONTINUOUS_CUTOFF ' in l))
            digest=hashlib.sha256((Path(scratch)/'remaining-stock.brep').read_bytes()).hexdigest()
            output=root/'radial-material'/digest;output.mkdir(parents=True,exist_ok=True)
            for name in ('remaining-stock.brep','remaining-stock.stl','result.json'):shutil.copyfile(Path(scratch)/name,output/name)
            initial=inherited_protection(previous)
            result={**base,'target_protection_status':'passed' if initial['status']=='passed' else 'review',
                'material_removal_status':native['material_removal_status'],'operation_coverage_status':native['material_removal_status'],
                'initial_material_protection':initial,'native_result':native,'removed_volume_mm3':native['removed_volume_mm3'],
                'material_state':{'relative_path':str((output/'remaining-stock.brep').relative_to(root)),'sha256':digest,
                    'operation_signature':operation_signature(operation),'context_signature':context,
                    'continued_from_operation_id':previous['operation_id'],'input_sha256':hashlib.sha256(stock.read_bytes()).hexdigest()},
                'stock_mesh_url':f'/api/v1/jobs/{job.id}/radial-material/{digest}/remaining-stock.stl',
                'detail':'已在前序三维实体上完成刀缝去除；实刀行程、切断刀柄碰撞及分离后的夹持/接料证据尚未验证，不能接受。',
                'diagnostic_hints':['切断尚需验证实际切断刀位、刀柄与夹具间隙，以及分离后的接料或夹持；刀缝几何通过不能代替这些证据。']}
            write_json(output/'evidence.json',result);return result
    except subprocess.CalledProcessError as error:
        return {**base,'detail':(error.stderr or error.stdout or '连续实体切断执行器失败')[-2000:]}
    except (ValueError,KeyError,TypeError,OSError,StopIteration,subprocess.SubprocessError) as error:
        return {**base,'detail':str(error)}
