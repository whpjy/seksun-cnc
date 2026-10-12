import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from types import SimpleNamespace
from pathlib import Path
from app import main
from app.engines import resolve_executable, run_freecad_adapter
from app.side_hole_check import check_material_context
from app.agent.l32_operation_trial import run_independent_trial, accept_trial
from test_l32_operation_trial import job,operation,rotational,snapshot


def test_accepted_turning_state_is_used_without_mutating_it():
    source=job(operation("OP10",10,"turn_facing"))
    geometry,machine=rotational(),snapshot()
    trial,state=run_independent_trial(job=source,operation_id="OP10",rotational=geometry,snapshot=machine,state=None)
    accepted=accept_trial(job=source,state=state,trial=trial,operation_id="OP10")
    original=json.dumps(accepted,sort_keys=True)
    context=check_material_context(source,geometry,machine,accepted)
    assert context["stock_source"]=="accepted_turning_envelope"
    assert context["samples"]==trial["cumulative_simulation"]["samples"]
    assert context["accepted_operation_ids"]==["OP10"]
    assert json.dumps(accepted,sort_keys=True)==original
    source.plan.stock["diameter_mm"]=26
    changed=check_material_context(source,geometry,machine,accepted)
    assert changed["stock_source"]=="initial_solid_blank" and changed["samples"] is None


def test_nonrotational_prefix_cannot_be_replaced_by_round_stock(monkeypatch):
    source=job(operation("OP10",10,"pocket_finishing"))
    monkeypatch.setattr("app.agent.l32_operation_trial.reconcile_state",lambda *args:{"accepted":[{"operation_id":"OP10"}]})
    with pytest.raises(HTTPException) as error: check_material_context(source,rotational(),snapshot(),{})
    assert error.value.status_code==409


@pytest.mark.skipif(not resolve_executable("FreeCADCmd"),reason="requires real OCC/FreeCAD")
@pytest.mark.parametrize("with_hole,sampled",[(True,False),(False,False),(True,True)])
def test_exact_occ_detects_safe_removal_and_target_gouge(tmp_path,with_hole,sampled):
    script=tmp_path/"check.py"
    payload={"axis":{"origin":dict(x=0,y=0,z=0),"direction":dict(x=0,y=0,z=1)},"stock_diameter_mm":20,"z_range_mm":[0,10],"draft":{"holes":[{"feature_id":"H-1","tool_diameter_mm":2,"entry":dict(x=12,y=0,z=5),"end":dict(x=5,y=0,z=5)}]}}
    if sampled:
        payload["samples"]=[dict(z=z,outer_radius=10,inner_radius=0) for z in [0,5,10]]
    script.write_text('''
import sys,json
sys.path.insert(0,"/app/cam")
import FreeCAD as App
import Part
from side_hole_sweep import check
target=Part.makeCylinder(10,10)
cutter=Part.makeCylinder(1,7,App.Vector(5,0,5),App.Vector(1,0,0))
if WITH_HOLE: target=target.cut(cutter)
target.exportStep(SOURCE)
result=check(SOURCE,PAYLOAD,OUTPUT)
print("TEST_RESULT "+json.dumps(result),flush=True)
'''.replace("WITH_HOLE",repr(with_hole)).replace("SOURCE",repr(str(tmp_path/"target.step"))).replace("OUTPUT",repr(str(tmp_path/"output"))).replace("PAYLOAD",repr(payload)),encoding="utf-8")
    result=run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    data=json.loads(next(line.split("TEST_RESULT ",1)[1] for line in result.stdout.splitlines() if "TEST_RESULT " in line))
    assert data["removed_volume_mm3"]>10 and data["remaining_stock_valid"]
    assert data["initial_stock_volume_mm3"]-data["remaining_stock_volume_mm3"]==pytest.approx(data["removed_volume_mm3"],abs=1e-5)
    assert data["target_protection_status"]==("passed" if with_hole else "failed")
    assert (tmp_path/"output"/"remaining-stock.stl").stat().st_size>100
    assert (tmp_path/"output"/"remaining-stock.brep").is_file()


@pytest.mark.parametrize("changed_during_run",[False,True])
def test_endpoint_cache_and_mid_calculation_revision_guard(monkeypatch,tmp_path,changed_during_run):
    from test_drawing_review import source_job
    source=source_job(monkeypatch,tmp_path)
    (tmp_path/source.filename).write_bytes(b"STEP placeholder for mocked runner")
    calls=[]
    def runner(command,script,args,**kwargs):
        calls.append(args)
        output=Path(args[2])
        for name in ["remaining-stock.stl","remaining-stock.brep","result.json"]:
            (output/name).write_text("sandbox artifact")
        if changed_during_run: source.plan.stock["diameter_mm"]=26
        return SimpleNamespace(stdout='CNC_SIDE_HOLE_CHECK '+json.dumps({"target_protection_status":"passed","removed_volume_mm3":1.,"target_contact_mm3":0.}))
    monkeypatch.setattr(main,"run_freecad_adapter",runner)
    client=TestClient(main.app)
    url=f"/api/v1/jobs/{source.id}/side-hole-check?feature_ids=HF-14,HF-15,HF-16,HF-17"
    result=client.post(url)
    if changed_during_run:
        assert result.status_code==409
        assert not list((tmp_path/"side-hole-checks").glob("*/evidence.json"))
    else:
        assert result.status_code==200,result.text
        assert result.json()["can_accept"] is False
        assert client.get(result.json()["stock_mesh_url"]).status_code==200
        assert client.post(url).json()==result.json() and len(calls)==1
