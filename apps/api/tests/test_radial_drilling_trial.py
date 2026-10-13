import hashlib
import json
import pytest
from app.radial_drilling_trial import bound_solid,radial_protection_status
from app.cnc_mcp import summarize_operation_catalog
from app.operation_library import operation_library_payload
from app.engines import resolve_executable,run_freecad_adapter


def test_known_target_gouge_is_not_hidden_by_unknown_initial_difference():
    assert radial_protection_status({'target_protection_status':'failed','initial_missing_target_mm3':None})=='failed'
    assert radial_protection_status({'target_protection_status':'passed','initial_missing_target_mm3':None})=='review'
    assert radial_protection_status({'target_protection_status':'passed','initial_missing_target_mm3':0})=='passed'


@pytest.mark.parametrize('protection',['failed','review'])
def test_blocked_radial_trial_never_promotes_hole_coverage(protection):
    from test_l32_operation_trial import job,operation,rotational,snapshot
    from app.agent.l32_operation_trial import run_independent_trial,accept_trial
    planned=operation('OP10',10,'radial_drilling')
    planned.feature_ids=['HF-1']
    source=job(planned)
    evidence={'machine_capability_status':'review','target_protection_status':protection,
        'material_removal_status':'passed','operation_coverage_status':'passed',
        'verified_hole_feature_ids':['HF-1']}
    trial,state=run_independent_trial(job=source,operation_id='OP10',rotational=rotational(),snapshot=snapshot(),state=None,nonrotational_evidence=evidence)
    assert not trial['can_accept'] and trial['evidence']['verified_hole_feature_ids']==[]
    with pytest.raises(ValueError,match='did not pass'):
        accept_trial(job=source,state=state,trial=trial,operation_id='OP10')


def test_material_hash_context_and_path_binding(tmp_path):
    path=tmp_path/'stock.brep';path.write_bytes(b'native material')
    record={'operation_signature':'op','evidence':{'material_state':{
        'relative_path':'stock.brep','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'context_signature':'context','operation_signature':'op'}}}
    assert bound_solid(tmp_path,record,'context')==path
    with pytest.raises(ValueError,match='provenance'):bound_solid(tmp_path,record,'changed')
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):bound_solid(tmp_path,record,'context')
    record['evidence']['material_state']['relative_path']='../other.brep'
    with pytest.raises(ValueError,match='outside'):bound_solid(tmp_path,record)


def test_radial_catalog_excludes_coaxial_slanted_and_unreviewed_holes():
    from app.catalogs import get_tool
    assert get_tool('DRILL-1.5').diameter_mm==1.5
    axis={'direction':dict(x=0,y=0,z=1),'origin':dict(x=0,y=0,z=0)}
    holes=[{'id':id,'kind':'hole','center':dict(x=5,y=0,z=1),'axis':normal,
        'diameter':1.5,'length':3,'review_state':state}
        for id,normal,state in [('RADIAL',dict(x=1,y=0,z=0),'accepted'),
            ('AXIAL',dict(x=0,y=0,z=1),'accepted'),('SLANTED',dict(x=1,y=0,z=1),'accepted'),
            ('REVIEW',dict(x=1,y=0,z=0),'review')]]
    catalog=summarize_operation_catalog(operation_library_payload(),{'device_id':'citizen-cincom-l32',
        'plan':{'stock':{'rotational_axis':axis}},'analysis':{'cylindrical_features':holes}},None)
    radial=next(d for d in catalog['definitions'] if d['id']=='radial_drilling')
    assert [r['id'] for r in radial['valid_geometry_refs']]==['RADIAL']
    assert 'drilling' in catalog['unavailable_definition_ids']


@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_brep_continuation_does_not_rebuild_already_drilled_material(tmp_path):
    script=tmp_path/'chain.py'
    script.write_text('''
import sys,json
sys.path.insert(0,'/app/cam')
import FreeCAD as App
import Part
from side_hole_sweep import check
from pathlib import Path
root=Path(ROOT)
outer=Part.makeCylinder(10,10)
cutter=Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0))
source=root/'target.step';outer.cut(cutter).exportStep(str(source))
payload={'axis':{'direction':dict(x=0,y=0,z=1),'origin':dict(x=0,y=0,z=0)},'stock_diameter_mm':20,'z_range_mm':[0,10],
    'draft':{'holes':[{'feature_id':'H1','tool_diameter_mm':2,'entry':dict(x=12,y=0,z=5),'end':dict(x=5,y=0,z=5)}]}}
first=check(str(source),payload,root/'first')
payload['input_stock_brep']=str(root/'first'/'remaining-stock.brep')
second=check(str(source),payload,root/'second')
print('RESULT '+json.dumps({'first':first,'second':second}))
'''.replace('ROOT',repr(str(tmp_path))),encoding='utf-8')
    result=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=90)
    data=json.loads(next(l.split('RESULT ',1)[1] for l in result.stdout.splitlines() if 'RESULT ' in l))
    assert data['second']['initial_stock_volume_mm3']==data['first']['remaining_stock_volume_mm3']
    assert data['second']['removed_volume_mm3']==pytest.approx(0,abs=.0001)
    assert data['second']['material_removal_status']=='failed'
