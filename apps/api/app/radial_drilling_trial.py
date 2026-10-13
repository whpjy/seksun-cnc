"""Bound radial drilling to the real next stock and explicit drill geometry.

Reference tool dimensions cannot satisfy machine/fixture acceptance gates.
"""
import hashlib
import json
import subprocess
from pathlib import Path
from .side_hole_draft import build_side_hole_draft
from .side_hole_tool import SideDrillGeometry,SideFlatGeometry,prepare_flat_geometry,prepare_drill_geometry,assess_side_hole_machine


def radial_protection_status(native):
    # A known gouge must not be downgraded by an unavailable raw diagnostic.
    if native.get('target_protection_status')!='passed':
        return 'failed'
    if native.get('initial_missing_target_mm3') is None:
        return 'review'
    return 'passed' if native['initial_missing_target_mm3'] <= .0001 else 'failed'


def bound_solid(directory, record, context=None):
    ref=(record.get('evidence') or {}).get('material_state') or {}
    root=directory.resolve()
    path=(root/ref.get('relative_path','')).resolve()
    if root not in path.parents or not path.is_file() or path.suffix!='.brep':
        raise ValueError('Continuous material solid is unavailable or outside this task')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=ref.get('sha256'):
        raise ValueError('Continuous material solid changed after trial')
    if ref.get('operation_signature') != record.get('operation_signature') or (context is not None and ref.get('context_signature')!=context):
        raise ValueError('Continuous material provenance does not match accepted operation')
    return path


def inherited_protection(previous):
    evidence=(previous or {}).get('evidence') or {}
    exact=evidence.get('exact_target_protection') or {}
    if exact.get('status')=='passed':
        return {'status':'passed','source_operation_id':previous['operation_id'],'radial_tolerance_mm':exact.get('radial_tolerance_mm',.05),'scope':'inherited_accepted_full_step_gate'}
    if evidence.get('target_protection_status')=='passed' and evidence.get('material_state'):
        return {**(evidence.get('initial_material_protection') or {}),'status':'passed','source_operation_id':previous['operation_id'],'scope':'inherited_bound_solid_and_zero_target_contact'}
    return {'status':'review','scope':'missing_initial_material_protection'}


def physical_tool_matches(job,operation,geometry,station):
    from .main import _load_tool_inventory
    record=next((r for r in _load_tool_inventory(job.machine_instance_id) if r.inventory_id==operation.tool.inventory_id),None)
    if not record or not record.active or not record.physical_geometry_verified or record.station!=station or record.tool_kind!=operation.tool.kind:return False
    pairs=[(record.measured_diameter_mm,operation.tool.diameter_mm),
        (record.measured_flute_length_mm,geometry.flute_length_mm),(record.measured_stickout_mm,geometry.stickout_from_tip_mm),
        (record.measured_shank_diameter_mm,geometry.shank_diameter_mm),(record.measured_holder_diameter_mm,geometry.holder_diameter_mm),
        (record.measured_holder_length_mm,geometry.holder_length_mm)]
    if operation.type=='radial_drilling':pairs.append((record.measured_point_angle_deg,geometry.point_angle_deg))
    if operation.type=='radial_hole_finishing' and not record.center_cutting_verified:return False
    return all(a is not None and b is not None and abs(a-b)<1e-6 for a,b in pairs)


def radial_trial_evidence(job, operation):
    from .main import job_directory, load_machine_snapshot, run_freecad_adapter, FREECAD_CMD, APP_ROOT, write_json
    from .rotational_features import RotationalFeatureAnalysis
    from .machining_geometry import job_axis
    from .agent.l32_operation_trial import evidence_context_signature,reconcile_state,_simulation_range,_reference_profile,NONROTATIONAL_OPERATION_TYPES,operation_signature
    base={'machine_capability_status':'not_run','target_protection_status':'not_run',
          'material_removal_status':'not_run','operation_coverage_status':'not_run',
          'verified_hole_feature_ids':[],'production_ready':False}
    directory=job_directory(job.id)
    geometry=RotationalFeatureAnalysis.model_validate_json((directory/'rotational-features.json').read_text(encoding='utf-8'))
    snapshot=load_machine_snapshot(directory/'machine-configuration.json')
    context=evidence_context_signature(job,geometry,snapshot)
    state_path=directory/'harness-l32-operation-state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else None
    accepted=reconcile_state(job,state,context)['accepted']
    try:
        features={f.id:f for f in job.analysis.cylindrical_features}
        selected=[features[i].model_dump(mode='json') for i in operation.feature_ids]
        draft=build_side_hole_draft(selected,job_axis(job.model_dump(mode='json'),geometry.model_dump(mode='json')),float(job.plan.stock['diameter_mm']),tool_diameter_mm=operation.tool.diameter_mm)
        finishing=operation.type=='radial_hole_finishing'
        if operation.tool.kind!=('end_mill' if finishing else 'drill'):raise ValueError('径向钻孔需钻头；平底精加工需平底铣刀')
        keys=('flute_length_mm','stickout_from_tip_mm','shank_diameter_mm','holder_diameter_mm','holder_length_mm')
        if not finishing:keys=('point_angle_deg',)+keys
        missing=[k for k in keys if k not in operation.parameters]
        if missing:
            return {**base,'detail':'缺少显式钻头几何：'+', '.join(missing),'diagnostic_hints':['请补齐钻尖、刃长、伸出和刀柄尺寸；不能把目录默认尺寸当作实刀证据。']}
        extra={} if finishing else {'full_diameter_extension_mm':operation.parameters.get('full_diameter_extension_mm',0)}
        tool_geometry=(SideFlatGeometry if finishing else SideDrillGeometry)(**{k:operation.parameters[k] for k in keys},**extra,
            station_group='back' if operation.workpiece_side=='back' else 'gang',
            evidence=str(operation.parameters.get('drill_geometry_evidence','工程参数，待实刀确认')))
        draft=(prepare_flat_geometry if finishing else prepare_drill_geometry)(draft,tool_geometry)
        from .radial_setup import load_setup,RadialSetup
        setup=load_setup(job)
        if setup and operation.parameters.get('radial_setup_revision')!=setup['revision']:
            return {**base,'detail':'当前工序未绑定最新径向配置，请保存配置后重新试算。'}
        machine=assess_side_hole_machine(job,snapshot,draft,tool_geometry.station_group,setup)
        previous=accepted[-1] if accepted else None
        input_solid=None
        if any(r['operation_id'] in {o.id for s in job.plan.setups for o in s.operations if o.type in NONROTATIONAL_OPERATION_TYPES} for r in accepted):
            input_solid=bound_solid(directory,previous,context)
        payload={'axis':job_axis(job.model_dump(mode='json'),geometry.model_dump(mode='json')),
            'stock_diameter_mm':float(job.plan.stock['diameter_mm']),
            'z_range_mm':list(_simulation_range(job,_reference_profile(job,geometry))),
            'samples':previous['cumulative_simulation']['samples'] if previous else None,'draft':draft}
        if input_solid:payload['input_stock_brep']=str(input_solid)
        if setup and setup.get('fixture'):
            fixture=setup['fixture'];path=(directory/fixture['relative_path']).resolve()
            if directory.resolve() not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=fixture['sha256']:raise ValueError('夹具实体文件与绑定哈希不一致')
            payload['radial_setup']={'mapping':machine['trajectory']['mapping'],'poses':machine['trajectory']['poses'],
                'fixture_file':str(path),'fixture_attachment':setup['configuration']['fixture_attachment']}
        import tempfile
        with tempfile.TemporaryDirectory(dir=directory) as scratch:
            result=run_freecad_adapter(FREECAD_CMD,APP_ROOT/'cam'/'side_hole_sweep.py',
                [directory/job.filename,json.dumps(payload),scratch],timeout_seconds=180)
            native=json.loads(next(l.split('CNC_SIDE_HOLE_CHECK ',1)[1] for l in result.stdout.splitlines() if 'CNC_SIDE_HOLE_CHECK ' in l))
            native['machine_assessment']=machine
            fixture_status=native.get('fixture_clearance_status','not_run')
            machine['checks'][-1]={'code':'fixture_clearance','status':fixture_status,'detail':'夹具进给及保守退刀/分度移动包络检查' if fixture_status!='not_run' else '未绑定完整夹具和刀柄几何'}
            station=(setup['configuration'].get('operation_stations',{}).get(operation.id,setup['configuration']['station']) if setup else '')
            physical_verified=bool(setup and physical_tool_matches(job,operation,tool_geometry,station))
            machine['checks'].append({'code':'physical_tool_geometry','status':'passed' if physical_verified else 'review',
                'detail':'实刀尺寸、刀位及中心切削能力已匹配' if physical_verified else '需绑定完整实测且已确认的现场刀具；尺寸与刀位必须匹配'})
            machine_passed=all(c['status']=='passed' for c in machine['checks']) and native['non_cutting_clearance_status']=='passed' and all(h['flute_reach_status']=='passed' for h in draft['holes'])
            machine['status']='passed' if machine_passed else 'failed' if any(c['status']=='failed' for c in machine['checks']) else 'review'
            covered=all(s.get('remaining_cylindrical_segment_mm3',1) <= .0001 and s.get('target_inside_cylindrical_segment_mm3',1) <= .0001 for s in native['stages'])
            protection_status=radial_protection_status(native)
            initial=inherited_protection(previous)
            if native['target_protection_status']=='passed' and initial['status']=='passed':protection_status='passed'
            protected=protection_status=='passed'
            contracts=[]
            if not finishing and not covered:
                operations=[o for s in job.plan.setups for o in s.operations if o.enabled]
                later=operations[next(i for i,o in enumerate(operations) if o.id==operation.id)+1:]
                responsible=next((o for o in later if o.type=='radial_hole_finishing' and set(operation.feature_ids)<=set(o.feature_ids)),None)
                if responsible:
                    contracts=[{'responsible_operation_id':responsible.id,'responsible_operation_signature':operation_signature(responsible),
                        'verification_version':'1.1.0','domain':'exact_3d_hole','feature_ids':operation.feature_ids,'contract':'clear_selected_cylindrical_segments'}]
            assigned=[c for r in accepted for c in r.get('deferred_material_contracts',[]) if c.get('responsible_operation_id')==operation.id and c.get('domain')=='exact_3d_hole']
            if any(not set(c['feature_ids'])<=set(operation.feature_ids) for c in assigned):covered=False
            digest=hashlib.sha256((Path(scratch)/'remaining-stock.brep').read_bytes()).hexdigest()
            output=directory/'radial-material'/digest;output.mkdir(parents=True,exist_ok=True)
            import shutil
            for name in ('remaining-stock.brep','remaining-stock.stl','result.json'):
                shutil.copyfile(Path(scratch)/name,output/name)
            evidence={**base,'machine_capability_status':'passed' if machine_passed else 'review',
                'target_protection_status':protection_status,
                'material_removal_status':native['material_removal_status'],
                'removed_volume_mm3':native['removed_volume_mm3'],
                'operation_coverage_status':'passed' if covered or contracts else 'failed',
                'deferred_material_contracts':contracts,'initial_material_protection':initial,
                'verified_hole_feature_ids':operation.feature_ids if machine_passed and protected and covered else [],
                'material_state':{'relative_path':str((output/'remaining-stock.brep').relative_to(directory)),
                    'operation_signature':operation_signature(operation),
                    'sha256':digest,'context_signature':context,'continued_from_operation_id':previous['operation_id'] if previous else None,
                    'input_sha256':hashlib.sha256(input_solid.read_bytes()).hexdigest() if input_solid else None},
                'stock_mesh_url':f'/api/v1/jobs/{job.id}/radial-material/{digest}/remaining-stock.stl',
                'native_result':native,'draft':draft,
                'detail':'径向孔实体试算已执行；必须补齐机床坐标、刀位行程、夹具与实刀证据，不能仅凭参考钻孔接受工序。',
                'diagnostic_hints':([f"切削体侵入STEP成品 {native['target_contact_mm3']:.6f} mm³；核对钻尖、孔底及钻深。"] if native['target_protection_status']!='passed' else [])+
                    [c['detail'] for c in machine['checks'] if c['status']!='passed']+
                    ([] if operation.tool.inventory_id else ['未绑定已验证的实体刀具库存记录；候选尺寸不代表实刀确认。'])}
            write_json(output/'evidence.json',evidence)
            return evidence
    except (ValueError,KeyError,StopIteration,OSError,subprocess.SubprocessError) as error:
        return {**base,'detail':str(error)}
