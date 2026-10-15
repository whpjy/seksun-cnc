import json
import pytest
from app.engines import resolve_executable,run_freecad_adapter

@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_cutoff_sweeps_and_retained_components_are_checked_on_real_solids(tmp_path):
    script=tmp_path/'check.py'
    script.write_text('''
import sys,json,copy
sys.path.insert(0,'/app/cam')
import Part,FreeCAD as App
from pathlib import Path
from continuous_cutoff import cutoff
from cutoff_safety import check_tool,check_retention,clusters
root=Path(ROOT)
stock=Part.makeCylinder(12,22,App.Vector(0,0,-20))
target=Part.makeCylinder(10,10,App.Vector(0,0,-10))
result,_=cutoff(stock,target,{'origin':dict(x=0,y=0,z=0),'direction':dict(x=0,y=0,z=1)},-12,2,12)
fixture=root/'fixture.step';Part.makeBox(1,1,1,App.Vector(100,100,100)).exportStep(str(fixture))
payload={'z_mm':-12,'cutting_width_mm':2,'cutoff_safety':{
    'trajectory':{'mapping':{'rotation':[[1,0,0],[0,1,0],[0,0,1]],'translation':[0,0,0],'axis_origin':[0,0,0]},'tip_points_machine_xyz':[[30,0,-12],[0,0,-12]]},
    'tool_geometry':{'cutting_reach_mm':25,'stickout_mm':30,'cutting_width_mm':2,'blade_height_mm':5,'holder_width_mm':10,'holder_height_mm':10,'holder_length_mm':20},
    'fixture_file':str(fixture),'fixture_attachment':'stationary',
    'configuration':{'grip_z_min_mm':-2,'grip_z_max_mm':0,'grip_diameter_mm':24,'minimum_grip_length_mm':1,
        'retained_side':'+Z','clamp_confirmed':True,'clamp_force_n':200,'required_clamp_force_n':100}}}
clear=check_tool(stock,payload);retained=check_retention(result,target,payload)
payload['cutoff_safety']['tool_geometry']['cutting_reach_mm']=5
short=check_tool(stock,payload)
payload['cutoff_safety']['tool_geometry']['cutting_reach_mm']=25
Part.makeBox(1,1,1,App.Vector(28,0,-12)).exportStep(str(fixture))
collision=check_tool(stock,payload)
payload['cutoff_safety']['configuration']['grip_diameter_mm']=26
wrong_grip=check_retention(result,target,payload)
payload['cutoff_safety']['configuration']['grip_diameter_mm']=24
payload['cutoff_safety']['configuration']['clamp_force_n']=50
weak=check_retention(result,target,payload)
touching=Part.makeCompound([Part.makeCylinder(2,5),Part.makeCylinder(2,5,App.Vector(0,0,5))])
edge_touch=Part.makeCompound([Part.makeBox(1,1,1),Part.makeBox(1,1,1,App.Vector(1,1,0))])
print('RESULT '+json.dumps({'clear':clear,'retained':retained,'short':short,'collision':collision,'wrong_grip':wrong_grip,'weak':weak,
    'shared_face_components':len(clusters(touching)),'edge_touch_components':len(clusters(edge_touch))}))
'''.replace('ROOT',repr(str(tmp_path))),encoding='utf-8')
    completed=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=120)
    r=json.loads(next(l.split('RESULT ',1)[1] for l in completed.stdout.splitlines() if 'RESULT ' in l))
    assert r['clear']['status']=='passed'
    assert r['short']['status']=='failed' and r['short']['non_cutting_stock_contact_mm3']>1
    assert r['collision']['status']=='failed' and r['collision']['fixture_contact_mm3']>.1
    assert r['retained']['geometry_status']=='passed' and r['retained']['target_component_count']==1
    assert r['retained']['bar_side_present'] and r['retained']['transfer_motion_status']=='not_run'
    assert len(r['retained']['state_transitions'])==5
    assert r['wrong_grip']['geometry_status']=='failed'
    assert r['weak']['force_confirmation_status']=='failed'
    assert r['shared_face_components']==1 and r['edge_touch_components']==2
