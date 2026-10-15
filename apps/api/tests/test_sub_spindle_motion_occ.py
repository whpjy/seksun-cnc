import json
import pytest
from app.engines import run_freecad_adapter,resolve_executable
from app.cutoff_setup import SubSpindleMotion

def test_motion_requires_finite_coordinates_and_a_complete_envelope():
    with pytest.raises(ValueError):SubSpindleMotion(open_bore_diameter_mm=30,envelope_outer_diameter_mm=20)

@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_real_approach_clamp_rotation_and_withdrawal_sweeps(tmp_path):
    script=tmp_path/'motion.py';script.write_text('''
import sys,json,copy
sys.path.insert(0,'/app/cam')
import Part,FreeCAD as App
from pathlib import Path
from continuous_cutoff import cutoff
from sub_spindle_motion import check_transfer
root=Path(ROOT);fixture=root/'fixture.step'
Part.makeBox(1,1,1,App.Vector(100,100,100)).exportStep(str(fixture))
stock=Part.makeCylinder(12,22,App.Vector(0,0,-20));target=Part.makeCylinder(12,10,App.Vector(0,0,-10))
result,_=cutoff(stock,target,{'origin':dict(x=0,y=0,z=0),'direction':dict(x=0,y=0,z=1)},-12,2,12)
motion={'open_bore_diameter_mm':26,'envelope_outer_diameter_mm':36,'body_length_mm':5,'approach_distance_mm':15,'withdrawal_distance_mm':20,
    'z2_origin_mm':0,'z2_sign':1,'z2_limits':[-100,100],'x2_position_mm':0,'x2_limits':[-100,100],'envelope_confirmed':True}
p={'stock_radius_mm':12,'cutoff_safety':{'configuration':{'retained_side':'+Z','grip_z_min_mm':-2,'grip_z_max_mm':0,'grip_diameter_mm':24,'sub_spindle_motion':motion},
    'trajectory':{'mapping':{'rotation':[[1,0,0],[0,1,0],[0,0,1]],'translation':[0,0,0],'axis_origin':[0,0,0]},'tip_points_machine_xyz':[[30,0,-12],[0,0,-12]]},
    'tool_geometry':{'stickout_mm':30,'holder_length_mm':20,'blade_height_mm':5,'holder_height_mm':10,'holder_width_mm':10},
    'fixture_file':str(fixture),'fixture_attachment':'stationary'}}
clear=check_transfer(stock,result,target,p)
motion['z2_limits']=[-5,5];limited=check_transfer(stock,result,target,p);motion['z2_limits']=[-100,100]
Part.makeBox(1,1,1,App.Vector(15,0,8)).exportStep(str(fixture));blocked=check_transfer(stock,result,target,p)
motion['envelope_confirmed']=False;unconfirmed=check_transfer(stock,result,target,p)
print('RESULT '+json.dumps({'clear':clear,'limited':limited,'blocked':blocked,'unconfirmed':unconfirmed}))
'''.replace('ROOT',repr(str(tmp_path))),encoding='utf-8')
    completed=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=120)
    r=json.loads(next(l.split('RESULT ',1)[1] for l in completed.stdout.splitlines() if 'RESULT ' in l))
    assert r['clear']['status']=='passed',r
    assert len(r['clear']['stages'])==4
    assert r['limited']['status']=='failed' and any('Z2' in s for s in r['limited']['issues'])
    assert r['blocked']['status']=='failed'
    assert r['unconfirmed']['status']=='review'
