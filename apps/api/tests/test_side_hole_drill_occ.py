import json
import pytest
from app.engines import resolve_executable,run_freecad_adapter


@pytest.mark.skipif(not resolve_executable("FreeCADCmd"),reason="requires real OCC")
@pytest.mark.parametrize("tip_in_target,short_holder,retracted,overlapping",[(False,False,False,False),(True,False,False,False),(True,True,False,False),(False,False,True,False),(False,False,True,True)])
def test_drill_tip_gouge_and_non_cutting_holder_do_not_remove_stock(tmp_path,tip_in_target,short_holder,retracted,overlapping):
    geometry={"point_angle_deg":90,"full_diameter_extension_mm":0,"evidence":"test geometry"}
    if short_holder:
        geometry.update(flute_length_mm=1.5,stickout_from_tip_mm=2,shank_diameter_mm=2,holder_diameter_mm=6,holder_length_mm=10)
    draft={"holes":[{"feature_id":"H-1","diameter_mm":2,"cylindrical_segment_length_mm":1,"tool_diameter_mm":2,"entry":dict(x=12,y=0,z=5),"end":dict(x=5,y=0,z=5),"tip_end":dict(x=4,y=0,z=5),"tip_length_mm":1}],"drill_geometry":geometry}
    if retracted:
        geometry["full_diameter_extension_mm"]=-1
        draft["holes"][0]["end"]["x"]=6
        draft["holes"][0]["tip_end"]["x"]=5
    if overlapping: draft["holes"].append({**draft["holes"][0],"feature_id":"H-2"})
    payload={"axis":{"origin":dict(x=0,y=0,z=0),"direction":dict(x=0,y=0,z=1)},"stock_diameter_mm":20,"z_range_mm":[0,10],"draft":draft}
    script=tmp_path/"tip.py"
    script.write_text('''
import sys,json
sys.path.insert(0,"/app/cam")
import FreeCAD as App
import Part
from side_hole_sweep import check
stock=Part.makeCylinder(10,10)
cylinder=Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0))
tip=Part.makeCone(0,1,1,App.Vector(4,0,5),App.Vector(1,0,0))
target=stock.cut(cylinder.fuse(tip) if HAS_TIP else cylinder)
target.exportStep(SOURCE)
result=check(SOURCE,PAYLOAD,OUTPUT)
print("TEST_RESULT "+json.dumps(result),flush=True)
'''.replace("HAS_TIP",repr(tip_in_target)).replace("SOURCE",repr(str(tmp_path/"source.step"))).replace("PAYLOAD",repr(payload)).replace("OUTPUT",repr(str(tmp_path/"result"))),encoding="utf-8")
    completed=run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    result=json.loads(next(line.split("TEST_RESULT ",1)[1] for line in completed.stdout.splitlines() if "TEST_RESULT " in line))
    assert result["target_protection_status"]==("passed" if tip_in_target or retracted else "failed")
    assert result["stages"][0]["tip_target_contact_mm3"]==(0 if tip_in_target or retracted else pytest.approx(3.141592653589793/3,abs=1e-5))
    assert result["non_cutting_clearance_status"]==("failed" if short_holder else "not_run")
    # The same cutting sweep removes the same amount even when a colliding
    # holder is supplied. A holder must never become a milling cutter.
    assert result["removed_volume_mm3"]==pytest.approx(13.574397 if retracted else 16.715991,abs=.01)
    assert result["remaining_cylindrical_segments_mm3"]==pytest.approx(2.094395 if retracted else 0,abs=1e-5)
    if short_holder: assert result["non_cutting_stock_contact_mm3"]>1
