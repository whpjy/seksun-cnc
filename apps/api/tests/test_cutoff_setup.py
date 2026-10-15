import hashlib,json
import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from app.cutoff_setup import CutoffSetup,tool_geometry,travel_report,save_setup,load_cutoff_setup,bound_cutoff_inputs
from app.radial_setup import RadialSetup,save_config
from app.tool_inventory import ToolInventoryInput,record_physical_tool
from app.catalogs import get_tool
from test_l32_operation_trial import job,operation,rotational,snapshot


def config(**changes):
    return CutoffSetup(**{**dict(inventory_id='CUT-PHYSICAL',station='T07',end_radius_mm=0,
        grip_z_min_mm=-2,grip_z_max_mm=0,grip_diameter_mm=20,minimum_grip_length_mm=1,
        retained_side='+Z',synchronization_rpm=1000,clamp_force_n=200,required_clamp_force_n=100,
        clamp_confirmed=True,verified_by='synthetic tester',evidence='SYNTHETIC TEST ONLY'),**changes})


def physical(**changes):
    return ToolInventoryInput(**{**dict(inventory_id='CUT-PHYSICAL',catalog_tool_id='TURN-CUTOFF-2',station='T07',
        measured_cutting_width_mm=2,measured_cutting_reach_mm=25,measured_blade_height_mm=5,measured_stickout_mm=30,
        measured_holder_width_mm=10,measured_holder_height_mm=10,measured_holder_length_mm=20,cutoff_geometry_verified=True,
        capability_verified_by='synthetic tester',capability_verification_reference='SYNTHETIC TEST ONLY'),**changes})


@pytest.mark.parametrize('change',[dict(grip_z_min_mm=0),dict(grip_diameter_mm=float('nan')),dict(end_radius_mm=.1),dict(evidence=' ')])
def test_retention_fields_reject_invalid_geometry_or_missing_basis(change):
    with pytest.raises(ValidationError):config(**change)


@pytest.mark.parametrize('change',[dict(measured_cutting_reach_mm=40),dict(measured_holder_width_mm=1),dict(measured_holder_length_mm=None),dict(capability_verified_by=' ')])
def test_physical_cutoff_confirmation_requires_complete_coherent_measurements(change):
    with pytest.raises(ValidationError):physical(**change)


def test_cutoff_specific_verification_is_not_a_drill_or_catalog_confirmation():
    record=record_physical_tool('test',physical(),get_tool('TURN-CUTOFF-2'))
    assert tool_geometry(record)['cutting_reach_mm']==25
    record.cutoff_geometry_verified=False
    with pytest.raises(ValueError,match='实测'):tool_geometry(record)
    with pytest.raises(ValueError,match='仅用于'):record_physical_tool('test',physical(catalog_tool_id='DRILL-2.0'),get_tool('DRILL-2.0'))


def test_cutoff_diameter_programming_and_actual_machine_offsets():
    radial={'configuration':RadialSetup(source_zero_direction='+X',machine_origin=[3,4,5],x_programming='diameter',
        axis_limits={'X1':[-20,100],'Y1':[-10,10],'Z1':[-100,100]},retract_x_mm=50,module_id='U30B',station='T05',evidence='test').model_dump()}
    axis={'direction':dict(x=0,y=1,z=0),'origin':dict(x=0,y=0,z=0)}
    report=travel_report(radial,axis,-10,-.2,12)
    assert report['status']=='passed' and report['commanded_points']==[[50,4,-5],[5.6,4,-5]]
    radial['configuration']['axis_limits']['Z1']=[0,100]
    assert travel_report(radial,axis,-10,0,12)['status']=='failed'


def test_setup_binds_tool_source_and_coordinate_versions(tmp_path,monkeypatch):
    from app import main
    cut=operation('OP30',30,'turn_cutoff');cut.tool=get_tool('TURN-CUTOFF-2');source=job(cut)
    source.machine_configuration_hash=snapshot().configuration_hash
    (tmp_path/source.filename).write_bytes(b'STEP')
    (tmp_path/'rotational-features.json').write_text(rotational().model_dump_json())
    (tmp_path/'fixture.step').write_bytes(b'fixture')
    monkeypatch.setattr(main,'job_directory',lambda _:tmp_path);monkeypatch.setattr(main,'load_job',lambda _:source)
    monkeypatch.setattr(main,'_save_manual_plan_change',lambda *_:source)
    record=record_physical_tool(source.machine_instance_id,physical(),cut.tool)
    monkeypatch.setattr(main,'_load_tool_inventory',lambda _:[record])
    radial=save_config(source,RadialSetup(source_zero_direction='+X',machine_origin=[0,0,0],x_programming='radius',
        axis_limits={'X1':[-100,100],'Y1':[-100,100],'Z1':[-100,100]},retract_x_mm=30,module_id='U30B',station='T05',evidence='synthetic'),
        {'relative_path':'fixture.step','sha256':hashlib.sha256(b'fixture').hexdigest()})
    data=save_setup(source.id,cut.id,config())
    assert cut.tool.inventory_id==record.inventory_id
    assert load_cutoff_setup(source,cut)['revision']==data['revision']
    with pytest.raises(HTTPException) as error:save_setup(source.id,cut.id,config())
    assert error.value.status_code==409
    record.machine_instance_id='another-machine'
    with pytest.raises(ValueError,match='不属于'):bound_cutoff_inputs(source,cut)
    record.machine_instance_id=source.machine_instance_id
    record.measured_blade_height_mm=6
    with pytest.raises(ValueError,match='尺寸改变'):bound_cutoff_inputs(source,cut)
    record.measured_blade_height_mm=5
    (tmp_path/'fixture.step').write_bytes(b'changed')
    with pytest.raises(ValueError,match='夹具实体'):bound_cutoff_inputs(source,cut)
    payload=json.loads((tmp_path/'radial-setup.json').read_text());payload['revision']='changed'
    (tmp_path/'radial-setup.json').write_text(json.dumps(payload))
    with pytest.raises(ValueError,match='重新确认'):load_cutoff_setup(source,cut)
