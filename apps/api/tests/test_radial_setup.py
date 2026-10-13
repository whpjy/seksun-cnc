import hashlib,json
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.radial_setup import RadialSetup,frame,point,check_poses,save_config,load_setup
from app.side_hole_tool import SideFlatGeometry,prepare_flat_geometry
from test_l32_operation_trial import job,operation,rotational,snapshot

AXIS={'direction':dict(x=0,y=1,z=0),'origin':dict(x=2,y=3,z=4)}
def config(**updates):
    return RadialSetup(**{**dict(source_zero_direction='+X',machine_origin=[10,20,30],x_programming='radius',
        axis_limits={'X1':[-100,100],'Y1':[-100,100],'Z1':[-100,100]},retract_x_mm=40,
        module_id='U30B',station='T05',evidence='synthetic fixture only'),**updates})

def test_frame_is_right_handed_maps_axis_origin_and_preserves_offset():
    mapping=frame(config(),AXIS)
    assert point(AXIS['origin'],mapping)==[10,20,30]
    assert point(dict(x=2,y=5,z=4),mapping)==[10,20,32]
    assert point(dict(x=2,y=3,z=9.5),mapping)==[10,14.5,30]
    r=mapping['rotation'];cross=[r[0][1]*r[1][2]-r[0][2]*r[1][1],r[0][2]*r[1][0]-r[0][0]*r[1][2],r[0][0]*r[1][1]-r[0][1]*r[1][0]]
    assert cross==r[2]
    with pytest.raises(ValueError,match='垂直'):frame(config(source_zero_direction='+Y'),AXIS)

def holes():
    return {'holes':[dict(feature_id=f'H{i}',tool_diameter_mm=1.5,
        outward_direction=dict(x=sign,y=0,z=0),entry=dict(x=2+sign*13,y=3,z=9.5),end=dict(x=2+sign*6,y=3,z=9.5))
        for i,sign in enumerate((1,-1))]}

def test_indexing_keeps_two_opposite_offset_holes_and_diameter_units():
    report=check_poses(config(x_programming='diameter',retract_x_mm=80),AXIS,holes(),12)
    assert report['status']=='passed'
    a,b=report['poses']
    assert a['entry_machine_xyz']==pytest.approx([23,14.5,30])
    assert b['entry_machine_xyz']==pytest.approx([23,25.5,30])
    assert abs(b['index_rotation_deg'])==180
    assert a['commanded_points'][0]==[46,14.5,30]

def test_travel_limits_and_unsafe_inward_retract_fail():
    report=check_poses(config(axis_limits={'X1':[0,100],'Y1':[15,25],'Z1':[0,100]}),AXIS,holes(),12)
    assert report['status']=='failed' and any('Y1' in e for e in report['errors'])
    assert check_poses(config(retract_x_mm=20),AXIS,holes(),12)['status']=='failed'
    draft=holes();draft['holes'][0]['entry']['x']=60
    assert any('进给入口' in e for e in check_poses(config(),AXIS,draft,12)['errors'])

@pytest.mark.parametrize('updates',[dict(machine_origin=[float('nan'),0,0]),dict(axis_limits={'X1':[2,1]}),dict(evidence=' '),dict(operation_stations={'OP10':' '})])
def test_configuration_rejects_missing_or_invalid_customer_facts(updates):
    with pytest.raises(ValidationError):config(**updates)

def test_configuration_compare_and_swap_source_binding_and_selective_invalidation(tmp_path,monkeypatch):
    from app import main
    source=job(operation('OP10',10,'turn_facing'),operation('OP20',20,'radial_drilling'))
    source.machine_configuration_hash='snapshot'
    (tmp_path/source.filename).write_bytes(b'STEP source')
    (tmp_path/'rotational-features.json').write_text(rotational().model_dump_json())
    monkeypatch.setattr(main,'job_directory',lambda _:tmp_path)
    monkeypatch.setattr(main,'save_job',lambda *_:None)
    monkeypatch.setattr(main,'_save_manual_plan_change',lambda *_:source)
    first=save_config(source,config(source_zero_direction='+X'))
    assert source.plan.setups[0].operations[1].parameters['radial_setup_revision']==first['revision']
    assert 'radial_setup_revision' not in source.plan.setups[0].operations[0].parameters
    with pytest.raises(HTTPException) as conflict:save_config(source,config())
    assert conflict.value.status_code==409
    second=save_config(source,config(expected_revision=first['revision'],station='T06'))
    assert second['revision']!=first['revision'] and load_setup(source)==second
    (tmp_path/source.filename).write_bytes(b'changed source')
    with pytest.raises(ValueError,match='不一致'):load_setup(source)

def test_flat_tool_has_no_drill_tip_and_requires_all_physical_dimensions():
    data=holes();h=data['holes'][0]
    for item in data['holes']:item.update(centerline_feed_length_mm=7,cylindrical_segment_length_mm=3)
    geometry=SideFlatGeometry(flute_length_mm=15,stickout_from_tip_mm=30,shank_diameter_mm=1.5,holder_diameter_mm=25,holder_length_mm=20,evidence='synthetic')
    prepared=prepare_flat_geometry(data,geometry)
    assert prepared['holes'][0]['tip_length_mm']==0
    assert prepared['holes'][0]['tip_end']==h['end']
    assert 'tip_end' not in h

def test_pre_drill_contract_cannot_complete_holes_and_finisher_must_clear_them():
    from app.agent.l32_operation_trial import run_independent_trial,accept_trial,operation_signature,state_summary
    drill=operation('OP10',10,'radial_drilling');finish=operation('OP20',20,'radial_hole_finishing')
    drill.feature_ids=finish.feature_ids=['HF-1'];source=job(drill,finish)
    contract={'verification_version':'1.1.0','domain':'exact_3d_hole','feature_ids':['HF-1'],
        'responsible_operation_id':'OP20','responsible_operation_signature':operation_signature(finish)}
    proof={k:'passed' for k in ('machine_capability_status','target_protection_status','material_removal_status','operation_coverage_status')}
    trial,state=run_independent_trial(job=source,operation_id='OP10',rotational=rotational(),snapshot=snapshot(),state=None,nonrotational_evidence={**proof,'deferred_material_contracts':[contract]})
    assert trial['status']=='warning' and trial['evidence']['verified_hole_feature_ids']==[]
    with pytest.raises(ValueError,match='acknowledgement'):accept_trial(job=source,state=state,trial=trial,operation_id='OP10')
    state=accept_trial(job=source,state=state,trial=trial,operation_id='OP10',acknowledge_warning=True)
    assert state_summary(source,state)['outstanding_deferred_material_contracts']==[contract]
    failed,_=run_independent_trial(job=source,operation_id='OP20',rotational=rotational(),snapshot=snapshot(),state=state,nonrotational_evidence=proof)
    assert not failed['can_accept']
    passed,reconciled=run_independent_trial(job=source,operation_id='OP20',rotational=rotational(),snapshot=snapshot(),state=state,nonrotational_evidence={**proof,'verified_hole_feature_ids':['HF-1']})
    final=accept_trial(job=source,state=reconciled,trial=passed,operation_id='OP20')
    assert state_summary(source,final)['verified_hole_feature_ids']==['HF-1']
    assert state_summary(source,final)['outstanding_deferred_material_contracts']==[]
    finish.parameters['depth']=5
    assert state_summary(source,final)['accepted_count']==0

def test_physical_drill_requires_point_angle_and_complete_measures():
    from app.tool_inventory import ToolInventoryInput,record_physical_tool
    from app.catalogs import get_tool
    data=dict(inventory_id='DRILL-TEST',catalog_tool_id='DRILL-1.5',station='T05',measured_diameter_mm=1.5,
        measured_flute_length_mm=15,measured_stickout_mm=30,measured_shank_diameter_mm=1.5,
        measured_holder_diameter_mm=25,measured_holder_length_mm=20,physical_geometry_verified=True,
        capability_verified_by='synthetic',capability_verification_reference='test only')
    with pytest.raises(ValueError,match='钻尖角'):record_physical_tool('test',ToolInventoryInput(**data),get_tool('DRILL-1.5'))
    data['measured_point_angle_deg']=118
    record=record_physical_tool('test',ToolInventoryInput(**data),get_tool('DRILL-1.5'))
    assert record.physical_geometry_verified and record.verification_state=='capability_verified'
