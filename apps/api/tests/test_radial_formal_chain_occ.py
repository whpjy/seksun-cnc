"""Real OCC + planning gate integration using explicitly synthetic setup/tool evidence."""
import json,math
import pytest
from app.engines import resolve_executable,run_freecad_adapter

@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
@pytest.mark.parametrize('complete_transfer',[False,True])
def test_measured_test_setup_accepts_pilot_then_flat_finish_and_blocks_unverified_cutoff(tmp_path,monkeypatch,complete_transfer):
    from app import main
    from app.models import CylindricalFeature,Vec3
    from app.catalogs import get_tool
    from app.l32_configuration import snapshot_l32_instance
    from app.tool_inventory import ToolInventoryInput,record_physical_tool,bind_verified_inventory_tool
    from app.radial_setup import RadialSetup,save_config
    from app.agent.l32_operation_trial import run_independent_trial,accept_trial,state_summary
    from app.exact_target_protection import guard_trial
    from test_l32_operation_trial import job,operation,rotational,snapshot
    drill=operation('OP10',10,'radial_drilling');flat=operation('OP20',20,'radial_hole_finishing');cut=operation('OP30',30,'turn_cutoff')
    drill.feature_ids=flat.feature_ids=['H1'];drill.tool=get_tool('DRILL-2.0');flat.tool=get_tool('EM-2');cut.tool=get_tool('TURN-CUTOFF-2')
    cut.parameters.update(z_mm=-11,cutting_width_mm=cut.tool.cutting_width_mm)
    source=job(drill,flat,cut);geometry=rotational()
    from app.rotational_features import RotationalProfilePoint
    geometry.profiles[0].points=[RotationalProfilePoint(z=-10,radius=10),RotationalProfilePoint(z=0,radius=10)]
    source.plan.stock.update(z_min_mm=-12,z_max_mm=2)
    if complete_transfer:
        from app.bar_holding import BarHolding,extend_stock
        source.plan.stock=extend_stock(source.plan.stock,cut,BarHolding(operation_id=cut.id,grip_length_mm=6,clamp_confirmed=True,
            verified_by='synthetic',evidence='SYNTHETIC ONLY',expected_stock_signature='test'))
    source.analysis.cylindrical_features=[CylindricalFeature(id='H1',kind='hole',radius=1,diameter=2,length=5,
        center=Vec3(x=7.5,y=0,z=-5),axis=Vec3(x=1,y=0,z=0),review_state='accepted')]
    instance=snapshot().instance.model_copy(update={'installed_modules':['U30B']})
    machine=snapshot_l32_instance(instance);assert machine.validation.valid
    source.machine_configuration_hash=machine.configuration_hash
    (tmp_path/'rotational-features.json').write_text(geometry.model_dump_json())
    (tmp_path/'machine-configuration.json').write_text(machine.model_dump_json())
    script=tmp_path/'model.py';script.write_text(f'''
import Part,FreeCAD as App
solid=Part.makeCylinder(10,10,App.Vector(0,0,-10)).cut(Part.makeCylinder(1,5,App.Vector(5,0,-5),App.Vector(1,0,0)))
solid.exportStep({str(tmp_path/source.filename)!r})
Part.makeBox(1,1,1,App.Vector(100,100,100)).exportStep({str(tmp_path/'fixture.step')!r})
''',encoding='utf-8')
    run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=90)
    monkeypatch.setattr(main,'load_job',lambda _:source);monkeypatch.setattr(main,'job_directory',lambda _:tmp_path)
    monkeypatch.setattr(main,'save_job',lambda *_:None)
    import hashlib
    fixture={'relative_path':'fixture.step','sha256':hashlib.sha256((tmp_path/'fixture.step').read_bytes()).hexdigest()}
    save_config(source,RadialSetup(source_zero_direction='+X',machine_origin=[0,0,0],x_programming='radius',
        axis_limits={'X1':[-100,100],'Y1':[-100,100],'Z1':[-100,100]},retract_x_mm=20,module_id='U30B',station='T05',
        operation_stations={'OP20':'T06'},evidence='SYNTHETIC TEST ONLY'),fixture)
    records=[]
    for op,station in [(drill,'T05'),(flat,'T06')]:
        record=record_physical_tool(source.machine_instance_id,ToolInventoryInput(inventory_id=op.id,catalog_tool_id=op.tool.id,
            station=station,measured_diameter_mm=2,measured_flute_length_mm=15,measured_stickout_mm=30,
            measured_shank_diameter_mm=2,measured_holder_diameter_mm=8,measured_holder_length_mm=20,
            measured_point_angle_deg=118 if op is drill else None,physical_geometry_verified=True,center_cutting_verified=op is flat,
            capability_verified_by='test fixture',capability_verification_reference='SYNTHETIC TEST ONLY'),op.tool)
        records.append(record);op.tool=bind_verified_inventory_tool(record,op.tool)
        op.parameters.update(flute_length_mm=15,stickout_from_tip_mm=30,shank_diameter_mm=2,holder_diameter_mm=8,holder_length_mm=20)
    drill.parameters.update(point_angle_deg=118,full_diameter_extension_mm=-1/math.tan(math.radians(59)))
    monkeypatch.setattr(main,'_load_tool_inventory',lambda _:records)
    state=None
    for op in (drill,flat):
        evidence=main._l32_nonrotational_trial_evidence(source.id,op,job=source)
        assert evidence['machine_capability_status']=='passed',evidence
        trial,state=run_independent_trial(job=source,operation_id=op.id,rotational=geometry,snapshot=machine,state=state,nonrotational_evidence=evidence)
        trial=guard_trial(source,geometry,trial,state)
        assert trial['can_accept'],trial
        if op is drill:assert trial['status']=='warning' and trial['evidence']['verified_hole_feature_ids']==[]
        else:assert trial['evidence']['native_result']['remaining_cylindrical_segments_mm3']<.0001
        state=accept_trial(job=source,state=state,trial=trial,operation_id=op.id,context_signature=trial['context_signature'],acknowledge_warning=True)
        (tmp_path/'harness-l32-operation-state.json').write_text(json.dumps(state))
    assert state_summary(source,state)['verified_hole_feature_ids']==['H1']
    assert not state_summary(source,state)['outstanding_deferred_material_contracts']
    records[-1].measured_shank_diameter_mm=3
    assert not guard_trial(source,geometry,trial,state)['can_accept']
    records[-1].measured_shank_diameter_mm=2
    evidence=main._l32_nonrotational_trial_evidence(source.id,cut,job=source)
    if 'native_result' not in evidence:pytest.fail(evidence['detail'])
    assert evidence['native_result']['source']=='inherited_brep'
    assert evidence['material_removal_status']=='passed'
    trial,_=run_independent_trial(job=source,operation_id=cut.id,rotational=geometry,snapshot=machine,state=state,nonrotational_evidence=evidence)
    assert not trial['can_accept'] and trial['evidence']['machine_capability_status']=='review'
    # A measured tool and a force declaration cannot bypass missing bar stock
    # or the independent dynamic transfer gate.
    from app.cutoff_setup import CutoffSetup,save_setup
    cut_record=record_physical_tool(source.machine_instance_id,ToolInventoryInput(inventory_id='CUT-SYNTHETIC',catalog_tool_id='TURN-CUTOFF-2',
        station='T07',measured_cutting_width_mm=2,measured_cutting_reach_mm=25,measured_blade_height_mm=5,measured_stickout_mm=30,
        measured_holder_width_mm=10,measured_holder_height_mm=10,measured_holder_length_mm=20,cutoff_geometry_verified=True,
        capability_verified_by='synthetic tester',capability_verification_reference='SYNTHETIC TEST ONLY'),cut.tool)
    records.append(cut_record)
    motion={'open_bore_diameter_mm':26,'envelope_outer_diameter_mm':36,'body_length_mm':5,'approach_distance_mm':15,'withdrawal_distance_mm':20,
        'z2_origin_mm':0,'z2_sign':1,'z2_limits':[-100,100],'x2_position_mm':0,'x2_limits':[-100,100],
        'envelope_confirmed':True,'evidence':'SYNTHETIC envelope only'} if complete_transfer else None
    save_setup(source.id,cut.id,CutoffSetup(inventory_id=cut_record.inventory_id,station='T07',end_radius_mm=0,
        grip_z_min_mm=-2,grip_z_max_mm=0,grip_diameter_mm=24,minimum_grip_length_mm=1,retained_side='+Z',
        synchronization_rpm=1000,clamp_force_n=200,required_clamp_force_n=100,clamp_confirmed=True,
        verified_by='synthetic tester',evidence='SYNTHETIC TEST ONLY',sub_spindle_motion=motion))
    evidence=main._l32_nonrotational_trial_evidence(source.id,cut,job=source)
    assert evidence['native_result']['tool_clearance']['status']=='passed',evidence
    assert evidence['native_result']['retention']['geometry_status']==('passed' if complete_transfer else 'failed')
    assert evidence['native_result']['retention']['force_confirmation_status']=='passed'
    assert evidence['native_result']['retention']['transfer_motion_status']==('passed' if complete_transfer else 'not_run')
    trial,_=run_independent_trial(job=source,operation_id=cut.id,rotational=geometry,snapshot=machine,state=state,nonrotational_evidence=evidence)
    trial=guard_trial(source,geometry,trial,state)
    assert trial['can_accept']==complete_transfer,trial
    if complete_transfer:
        final=accept_trial(job=source,state=state,trial=trial,operation_id=cut.id,context_signature=trial['context_signature'])
        assert state_summary(source,final)['accepted_count']==3
        assert final['production_ready'] is False
