import json
import pytest
from app.engines import resolve_executable,run_freecad_adapter

@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_fixture_feed_and_rapid_collisions_block_and_flat_body_preserves_target(tmp_path):
    script=tmp_path/'fixture.py'
    script.write_text('''
import sys,json
sys.path.insert(0,'/app/cam')
import Part,FreeCAD as App
from pathlib import Path
from side_hole_sweep import check
root=Path(ROOT)
target=Part.makeCylinder(10,10).cut(Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0)))
source=root/'target.step';target.exportStep(str(source))
hole={'feature_id':'H1','diameter_mm':2,'tool_diameter_mm':2,'entry':dict(x=12,y=0,z=5),'end':dict(x=5,y=0,z=5),'tip_end':dict(x=5,y=0,z=5),'tip_length_mm':0,'cylindrical_segment_length_mm':7}
payload={'axis':{'direction':dict(x=0,y=0,z=1),'origin':dict(x=0,y=0,z=0)},'stock_diameter_mm':20,'z_range_mm':[0,10],
    'draft':{'holes':[hole],'drill_geometry':{'flute_length_mm':15,'stickout_from_tip_mm':30,'shank_diameter_mm':2,'holder_diameter_mm':8,'holder_length_mm':20}},
    'radial_setup':{'fixture_attachment':'stationary','mapping':{'rotation':[[1,0,0],[0,1,0],[0,0,1]],'translation':[0,0,0],'axis_origin':[0,0,0]},
        'poses':[{'feature_id':'H1','index_rotation_deg':0,'entry_machine_xyz':[12,0,5],'retract_machine_xyz':[20,0,5]}]}}
rows=[]
for name,pos in [('clear',[100,100,100]),('feed',[6,0,5]),('rapid',[65,0,5])]:
    fixture=root/f'{name}.step';Part.makeBox(1,1,1,App.Vector(*pos)).exportStep(str(fixture))
    payload['radial_setup']['fixture_file']=str(fixture)
    rows.append(check(str(source),payload,root/name))
print('RESULT '+json.dumps(rows))
'''.replace('ROOT',repr(str(tmp_path))),encoding='utf-8')
    result=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=90)
    clear,feed,rapid=json.loads(next(l.split('RESULT ',1)[1] for l in result.stdout.splitlines() if 'RESULT ' in l))
    assert clear['target_protection_status']=='passed' and clear['fixture_clearance_status']=='passed'
    assert clear['remaining_cylindrical_segments_mm3']<.0001
    assert feed['fixture_clearance_status']=='failed'
    assert rapid['fixture_clearance_status']=='failed' and rapid['stages'][0]['fixture_clearance']['rapid_contact_mm3']>.1
