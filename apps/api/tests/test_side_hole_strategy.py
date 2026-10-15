import json
from pathlib import Path
import pytest
from app.side_hole_strategy import strategy_drafts
from app.side_hole_tool import SideDrillGeometry
from app.engines import resolve_executable,run_freecad_adapter

@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_invalid_direct_candidate_does_not_hide_valid_continuous_flat_finish(tmp_path):
    drafts=strategy_drafts(draft(),SideDrillGeometry(point_angle_deg=118,evidence='candidate only'))
    payload={'axis':{'direction':dict(x=0,y=0,z=1),'origin':dict(x=0,y=0,z=0)},'stock_diameter_mm':20,'z_range_mm':[0,10],'drafts':drafts}
    script=tmp_path/'isolated.py';script.write_text('''
import sys,json
sys.path.insert(0,'/app/cam')
import Part,FreeCAD as App
import side_hole_strategy as strategy
from pathlib import Path
root=Path(ROOT);source=root/'target.step'
Part.makeCylinder(10,10).cut(Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0))).exportStep(str(source))
original=strategy.check
def failed_direct(source,payload,output):
    if Path(output).name=='direct':raise ValueError('invalid material subtraction')
    return original(source,payload,output)
strategy.check=failed_direct
print('RESULT '+json.dumps(strategy.compare(str(source),PAYLOAD,root/'output')))
'''.replace('ROOT',repr(str(tmp_path))).replace('PAYLOAD',repr(payload)),encoding='utf-8')
    response=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=90)
    r=json.loads(next(l.split('RESULT ',1)[1] for l in response.stdout.splitlines() if 'RESULT ' in l))
    assert r['strategies'][0]['geometry_status']=='unavailable'
    assert r['strategies'][0]['target_contact_mm3'] is None
    assert r['strategies'][2]['geometry_status']=='passed'
    assert r['can_accept'] is False


def draft():
    return {'holes':[{'feature_id':'H1','diameter_mm':2,'tool_diameter_mm':2,
        'entry':dict(x=12,y=0,z=5),'end':dict(x=5,y=0,z=5),'outward_direction':dict(x=1,y=0,z=0),
        'centerline_feed_length_mm':7,'cylindrical_segment_length_mm':7}]}


def test_tip_backoff_uses_nominal_bottom_and_keeps_flat_finish_separate():
    data=strategy_drafts(draft(),SideDrillGeometry(point_angle_deg=118,full_diameter_extension_mm=.3,evidence='test'))
    assert data['backoff']['holes'][0]['tip_end']['x']==pytest.approx(5)
    assert data['direct']['holes'][0]['end']['x']==pytest.approx(4.7)
    assert data['flat_finish']['holes'][0]['end']['x']==5
    assert 'tip_end' not in data['flat_finish']['holes'][0]
    assert data['backoff']['holes'][0]['full_diameter_extension_mm']<0


@pytest.mark.skipif(not resolve_executable('FreeCADCmd'),reason='requires real OCC')
def test_real_flat_bottom_comparison_blocks_drilling_and_backoff_but_clears_nominal_segment(tmp_path):
    drafts=strategy_drafts(draft(),SideDrillGeometry(point_angle_deg=118,evidence='candidate only'))
    payload={'axis':{'direction':dict(x=0,y=0,z=1),'origin':dict(x=0,y=0,z=0)},'stock_diameter_mm':20,'z_range_mm':[-5,10],'drafts':drafts,
        'cutoff':{'z_mm':-2,'cutting_width_mm':1}}
    script=tmp_path/'compare.py'
    script.write_text('''
import sys,json
sys.path.insert(0,'/app/cam')
import Part,FreeCAD as App
from side_hole_strategy import compare
from pathlib import Path
root=Path(ROOT)
shape=Part.makeCylinder(10,10).cut(Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0)))
source=root/'target.step';shape.exportStep(str(source))
result=compare(str(source),PAYLOAD,root/'output')
print('RESULT '+json.dumps(result))
'''.replace('ROOT',repr(str(tmp_path))).replace('PAYLOAD',repr(payload)),encoding='utf-8')
    response=run_freecad_adapter('FreeCADCmd',script,[],timeout_seconds=90)
    result=json.loads(next(l.split('RESULT ',1)[1] for l in response.stdout.splitlines() if 'RESULT ' in l))
    direct,backed,finished=result['strategies']
    assert direct['target_contact_mm3']>.1
    assert backed['target_contact_mm3']<.0001 and backed['remaining_hole_stock_mm3']>.1
    assert finished['geometry_status']=='passed' and finished['remaining_hole_stock_mm3']<.0001
    assert finished['native']['initial_stock_volume_mm3']==backed['native']['remaining_stock_volume_mm3']
    assert result['can_accept'] is False and all(not r['can_accept'] for r in result['strategies'])
    assert result['cutoff_continuation']['status']=='passed'
    assert result['cutoff_continuation']['source']=='inherited_brep'
    # The final kerf solid must retain the already cut radial hole.
    extra=tmp_path/'verify-cut.py'
    extra.write_text(f'''
import Part,FreeCAD as App,json
solid=Part.read({str(tmp_path/'output'/'cutoff'/'remaining-stock.brep')!r})
region=Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0))
print('VOLUME '+json.dumps(solid.common(region).Volume))
''',encoding='utf-8')
    checked=run_freecad_adapter('FreeCADCmd',extra,[],timeout_seconds=90)
    assert float(next(l.split('VOLUME ',1)[1] for l in checked.stdout.splitlines() if 'VOLUME ' in l))<.0001
