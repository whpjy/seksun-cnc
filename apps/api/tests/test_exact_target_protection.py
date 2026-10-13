import json
import pytest
from app import exact_target_protection as protection
from test_l32_operation_trial import job,operation,rotational


@pytest.mark.parametrize("previous_failed",[False,True])
def test_step_damage_blocks_even_when_profile_trial_passes(monkeypatch,previous_failed):
    source=job(operation("OP10",10,"turn_facing"))
    monkeypatch.setattr(protection,"required",lambda *_:True)
    monkeypatch.setattr(protection,"check_material",lambda *_:{"status":"failed","stages":[
        {"operation_id":"BEFORE","status":"failed" if previous_failed else "passed"},
        {"operation_id":"OP10","status":"failed"}]})
    result=protection.guard_trial(source,rotational(),{"operation_id":"OP10","can_accept":True,"status":"passed",
        "cumulative_simulation":{"samples":[]}},None)
    assert result["can_accept"] is False
    assert result["reason"]==("previous_step_target_damage" if previous_failed else "full_step_target_protection_failed")


def test_missing_exact_executor_cannot_silently_accept(monkeypatch):
    source=job(operation("OP10",10,"turn_facing"))
    monkeypatch.setattr(protection,"required",lambda *_:True)
    def unavailable(*_):raise FileNotFoundError("missing engine")
    monkeypatch.setattr(protection,"check_material",unavailable)
    result=protection.guard_trial(source,rotational(),{"operation_id":"OP10","can_accept":True,
        "cumulative_simulation":{"samples":[]}},None)
    assert result["can_accept"] is False and result["evidence"]["exact_target_protection"]["status"]=="unavailable"
    assert result["reason"] == "full_step_target_protection_unavailable"
    assert result["evidence"]["exact_target_protection"]["executor_error_type"] == "FileNotFoundError"


def test_real_occ_stock_merges_equal_adjacent_annuli_without_changing_volume(tmp_path):
    from app.engines import resolve_executable,run_freecad_adapter
    if not resolve_executable("FreeCADCmd"):pytest.skip("requires real OCC")
    script=tmp_path/"merged_stock.py"
    script.write_text('''
import sys,json,math
sys.path.insert(0,"/app/cam")
from side_hole_sweep import build_stock
from l32_target_protection import missing_volume
import Part
payload={"axis":{"origin":{"x":0,"y":0,"z":0},"direction":{"x":0,"y":0,"z":1}},
"samples":[{"z":i/10,"outer_radius":10 if i<50 else 9,"inner_radius":4} for i in range(101)]}
stock=build_stock(payload)
target=Part.makeCylinder(8,10).cut(Part.makeCylinder(5,10))
print("TEST_RESULT "+json.dumps({"valid":stock.isValid(),"solids":len(stock.Solids),"volume":stock.Volume,"expected":math.pi*745,"contained_missing":missing_volume(target,stock)}),flush=True)
''',encoding="utf-8")
    completed=run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    result=json.loads(next(s.split("TEST_RESULT ",1)[1] for s in completed.stdout.splitlines() if "TEST_RESULT " in s))
    assert result["valid"] and result["solids"]==2
    assert result["volume"]==pytest.approx(result["expected"],abs=1e-7)
    assert result["contained_missing"]==pytest.approx(0,abs=1e-7)


def test_real_occ_protection_detects_body_loss_despite_matching_2d_profile(tmp_path):
    from app.engines import resolve_executable,run_freecad_adapter
    if not resolve_executable("FreeCADCmd"):pytest.skip("requires real OCC")
    script=tmp_path/"protection.py"
    script.write_text('''
import sys,json
sys.path.insert(0,"/app/cam")
import FreeCAD as App
import Part
from l32_target_protection import check
target=Part.makeCylinder(10,10)
Part.makeCompound([target,Part.makeCylinder(3,3,App.Vector(50,0,0))]).exportStep(SOURCE)
payload={"axis":{"origin":{"x":0,"y":0,"z":0},"direction":{"x":0,"y":0,"z":1}},
"selected_solid_index":1,"stock_diameter_mm":22,"z_range_mm":[0,10],"stages":[{"operation_id":"SAFE","samples":[{"z":0,"outer_radius":10,"inner_radius":0},{"z":10,"outer_radius":10,"inner_radius":0}]},
{"operation_id":"UNSAFE","samples":[{"z":0,"outer_radius":9,"inner_radius":0},{"z":10,"outer_radius":9,"inner_radius":0}]}]}
print("TEST_RESULT "+json.dumps(check(SOURCE,payload)),flush=True)
'''.replace("SOURCE",repr(str(tmp_path/"source.step"))),encoding="utf-8")
    completed=run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    result=json.loads(next(s.split("TEST_RESULT ",1)[1] for s in completed.stdout.splitlines() if "TEST_RESULT " in s))
    assert result["stages"][0]["status"]=="passed"
    assert result["stages"][1]["status"]=="failed"
    assert result["stages"][1]["protected_missing_target_mm3"]>500


def test_raw_difference_failure_is_unknown_and_cannot_hide_toleranced_damage(tmp_path):
    from app.engines import resolve_executable,run_freecad_adapter
    if not resolve_executable("FreeCADCmd"):pytest.skip("requires real OCC")
    script=tmp_path/"raw_failure.py"
    script.write_text('''
import sys,json
sys.path.insert(0,"/app/cam")
import Part
import l32_target_protection as protection
Part.makeCylinder(10,10).exportStep(SOURCE)
original=protection.missing_volume
calls=[0]
def fail_only_raw(target,stock):
    calls[0]+=1
    if calls[0]%2==1:raise ValueError("invalid raw Boolean fragment")
    return original(target,stock)
protection.missing_volume=fail_only_raw
payload={"axis":{"origin":{"x":0,"y":0,"z":0},"direction":{"x":0,"y":0,"z":1}},
"selected_solid_index":1,"stock_diameter_mm":22,"z_range_mm":[0,10],"stages":[
{"operation_id":"SAFE","samples":[{"z":0,"outer_radius":10,"inner_radius":0},{"z":10,"outer_radius":10,"inner_radius":0}]},
{"operation_id":"UNSAFE","samples":[{"z":0,"outer_radius":9,"inner_radius":0},{"z":10,"outer_radius":9,"inner_radius":0}]}]}
print("TEST_RESULT "+json.dumps(protection.check(SOURCE,payload)),flush=True)
'''.replace("SOURCE",repr(str(tmp_path/"source.step"))),encoding="utf-8")
    completed=run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    result=json.loads(next(s.split("TEST_RESULT ",1)[1] for s in completed.stdout.splitlines() if "TEST_RESULT " in s))
    assert all(s["missing_target_mm3"] is None and s["raw_difference_status"]=="unavailable" for s in result["stages"])
    assert result["stages"][0]["status"]=="passed"
    assert result["stages"][1]["status"]=="failed" and result["stages"][1]["protected_missing_target_mm3"]>500
