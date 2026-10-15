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
    base={'machine_capability_status':'review','target_protection_status':'not_run','material_removal_status':'not_run',
        'operation_coverage_status':'not_run','production_ready':False,'material_domain':'exact_3d_occ'}
    if not accepted or not (accepted[-1].get('evidence') or {}).get('material_state'):
        if operation.parameters.get('cutoff_setup_revision'):
            return {**base,'detail':'切断运动配置已启用，但尚无前序已接受三维材料；不能改用回转包络绕过切断接料验证。'}
        return None
    try:
        previous=accepted[-1];stock=bound_solid(root,previous,context)
        if operation.tool.kind!='cutoff':raise ValueError('继承实体切断需使用切断刀')
        width=operation.parameters.get('cutting_width_mm',operation.tool.cutting_width_mm)
        if operation.tool.cutting_width_mm is None or abs(width-operation.tool.cutting_width_mm)>1e-6:
            raise ValueError('刀缝宽度必须与切断刀实际刃宽一致')
        payload={'input_stock_brep':str(stock),'axis':job_axis(job.model_dump(mode='json'),geometry.model_dump(mode='json')),
            'z_mm':operation.parameters['z_mm'],'cutting_width_mm':width,'stock_radius_mm':job.plan.stock['diameter_mm']/2}
        from .cutoff_setup import bound_cutoff_inputs,travel_report
        configuration_checks=[]
        try:
            cfg,radial,tool,fixture_file,revision=bound_cutoff_inputs(job,operation)
            trajectory=travel_report(radial,payload['axis'],payload['z_mm'],cfg.end_radius_mm,payload['stock_radius_mm'])
            snapshot=load_machine_snapshot(root/'machine-configuration.json')
            if not snapshot.validation.valid or 'grooving_cutoff' not in snapshot.validation.capabilities:
                configuration_checks.append('绑定机床缺少有效切断能力')
            if not {'X2','Z2'}<=set(snapshot.validation.enabled_axes):configuration_checks.append('背轴接料缺少X2/Z2轴')
            from .l32_configuration import L32_DEFINITION
            limit=min(s.maximum_rpm for s in L32_DEFINITION.spindles if s.id in {'main','sub'})
            if cfg.synchronization_rpm>limit:configuration_checks.append('同步转速超过主轴或背轴能力上限')
            configuration_checks+=trajectory['errors']
            main_holding=job.plan.stock.get('main_bar_holding') or {}
            if cfg.retained_side!='+Z':configuration_checks.append('当前主轴棒料夹持段模型仅支持成品位于切断+Z侧')
            if not main_holding.get('clamp_confirmed'):configuration_checks.append('主轴棒料夹持段和夹紧依据尚未确认')
            if main_holding and (main_holding.get('operation_id')!=operation.id or main_holding.get('cutoff_z_mm')!=payload['z_mm'] or main_holding.get('cutting_width_mm')!=width):
                configuration_checks.append('棒料夹持范围与当前切断位置或刀缝不一致，请重新配置初始毛坯')
            payload['cutoff_safety']={'configuration':cfg.model_dump(exclude={'expected_revision'}),'tool_geometry':tool,
                'trajectory':trajectory,'fixture_file':fixture_file,'fixture_attachment':radial['configuration']['fixture_attachment'],
                'main_holding':main_holding}
        except (ValueError,TypeError,OSError) as error:configuration_checks.append(str(error))
        with tempfile.TemporaryDirectory(dir=root) as scratch:
            response=run_freecad_adapter(FREECAD_CMD,APP_ROOT/'cam'/'continuous_cutoff.py',[root/job.filename,json.dumps(payload),scratch],timeout_seconds=180)
            native=json.loads(next(l.split('CNC_CONTINUOUS_CUTOFF ',1)[1] for l in response.stdout.splitlines() if 'CNC_CONTINUOUS_CUTOFF ' in l))
            digest=hashlib.sha256((Path(scratch)/'remaining-stock.brep').read_bytes()).hexdigest()
            output=root/'radial-material'/digest;output.mkdir(parents=True,exist_ok=True)
            for name in ('remaining-stock.brep','remaining-stock.stl','result.json'):shutil.copyfile(Path(scratch)/name,output/name)
            initial=inherited_protection(previous)
            clearance=native.get('tool_clearance',{'status':'not_run'})
            retention=native.get('retention',{'geometry_status':'not_run','force_confirmation_status':'not_run','transfer_motion_status':'not_run',
                'issues':['未提供实际切断刀与夹持配置']})
            checks=[{'code':'cutoff_configuration','status':('failed' if payload.get('cutoff_safety') else 'review') if configuration_checks else 'passed','detail':'; '.join(configuration_checks) or '切断坐标、实刀、声明行程及同步转速已校验'},
                {'code':'cutoff_tool_clearance','status':clearance['status'],'detail':'切断进给/退刀中的非切削刀板、刀柄及夹具接触检查'},
                {'code':'retained_component','status':retention['geometry_status'],'detail':'切断后成品所在连通材料块及夹持圆柱面检查'},
                {'code':'clamp_force_confirmation','status':retention['force_confirmation_status'],'detail':'声明夹紧力与客户要求及确认依据校验'},
                {'code':'transfer_motion','status':retention['transfer_motion_status'],'detail':'背轴接近、夹紧与取件保守运动包络校验'}]
            result={**base,'target_protection_status':'passed' if initial['status']=='passed' else 'review',
                'material_removal_status':native['material_removal_status'],'operation_coverage_status':native['material_removal_status'],
                'initial_material_protection':initial,'native_result':native,'removed_volume_mm3':native['removed_volume_mm3'],
                'cutoff_checks':checks,'cutoff_setup_revision':operation.parameters.get('cutoff_setup_revision'),
                'cutoff_execution_scope':'conservative_cutoff_and_coaxial_transfer_envelopes',
                'machine_capability_status':'passed' if all(c['status']=='passed' for c in checks) else 'failed' if any(c['status']=='failed' for c in checks) else 'review',
                'material_state':{'relative_path':str((output/'remaining-stock.brep').relative_to(root)),'sha256':digest,
                    'operation_signature':operation_signature(operation),'context_signature':context,
                    'continued_from_operation_id':previous['operation_id'],'input_sha256':hashlib.sha256(stock.read_bytes()).hexdigest()},
                'stock_mesh_url':f'/api/v1/jobs/{job.id}/radial-material/{digest}/remaining-stock.stl',
                'detail':'已继承前序三维材料检查切断、夹持与已配置的背轴保守运动包络；各项通过后才可用于下一步草案规划，仍不代表控制器执行或生产放行。',
                'diagnostic_hints':configuration_checks+retention.get('issues',[])+(native.get('transfer_motion') or {}).get('issues',[])+
                    ([] if clearance['status']=='passed' else ['切断刀/夹具接触检查未通过或尚未运行。'])}
            write_json(output/'evidence.json',result);return result
    except subprocess.CalledProcessError as error:
        return {**base,'detail':(error.stderr or error.stdout or '连续实体切断执行器失败')[-2000:]}
    except (ValueError,KeyError,TypeError,OSError,StopIteration,subprocess.SubprocessError) as error:
        return {**base,'detail':str(error)}
