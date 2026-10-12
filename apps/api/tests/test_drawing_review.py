import pytest
from fastapi.testclient import TestClient
from app import main
from app.drawing_intake import DrawingDimensionReview, review_drawing_dimension, drawing_requirements
from app.models import CylindricalFeature, ManufacturingRequirement
from app.coverage import evaluate_plan_coverage
from test_process_preview import prepare

AXIS = {"origin":dict(x=0,y=0,z=0),"direction":dict(x=0,y=0,z=1)}


def source_job(monkeypatch, tmp_path):
    job,_,_=prepare(monkeypatch,tmp_path)
    job.drawing_filename="drawing.pdf"
    job.drawing_analysis={"revision_counter":1,"maximum_outer_diameter_mm":24,"dimensions":[
        {"role":"radial_hole","nominal":1.5,"quantity":4,"page":1,"raw_text":"Ø1.50 +0.08/+0.03 (4)"}]}
    job.analysis.cylindrical_features=[CylindricalFeature(id=f"HF-{i+14}",kind="hole",radius=.75,diameter=1.5,length=1,
        center=dict(x=x,y=0,z=z),axis=dict(x=1,y=0,z=0),review_state="accepted") for i,(x,z) in enumerate([(6,8.1),(-6,8.1),(6,-8.1),(-6,-8.1)])]
    return job


def request(**changes):
    return DrawingDimensionReview(**{"revision_counter":1,"role":"radial_hole","nominal":1.5,"quantity":4,
        "tolerance_upper":.08,"tolerance_lower":.03,"cad_feature_ids":[f"HF-{14+i}" for i in range(4)],
        "reviewer":"工艺工程师","evidence":"第1页 B-B 引线核对",**changes})


def test_review_keeps_original_and_does_not_approve_quality(monkeypatch,tmp_path):
    job=source_job(monkeypatch,tmp_path)
    audit=review_drawing_dimension(job,"PDF-DIM-1",request(),confirmed_at="now",axis=AXIS)
    assert "dimension_reviews" not in job.drawing_analysis
    assert audit["revision_counter"]==2
    reqs=drawing_requirements(audit)
    row=reqs.requirements[0]
    assert row.minimum==pytest.approx(1.53) and row.maximum==pytest.approx(1.58)
    assert row.mapping_status=="matched" and row.verification_status=="pending_measurement"
    assert reqs.status=="review"
    job.plan.manufacturing_requirements=reqs
    assert not evaluate_plan_coverage(job.analysis,job.plan).production_ready


@pytest.mark.parametrize("changes",[{"revision_counter":2},{"quantity":3},{"nominal":2},
    {"role":"inner_diameter"},{"cad_feature_ids":["HF-14","HF-14"]},
    {"tolerance_upper":0,"tolerance_lower":.03},{"reviewer":" "}])
def test_invalid_or_stale_binding_rejected(monkeypatch,tmp_path,changes):
    job=source_job(monkeypatch,tmp_path)
    with pytest.raises(ValueError): review_drawing_dimension(job,"PDF-DIM-1",request(**changes),confirmed_at="now",axis=AXIS)


def test_partial_cylinder_cannot_be_confirmed_as_full_hole(monkeypatch,tmp_path):
    job=source_job(monkeypatch,tmp_path)
    job.analysis.cylindrical_features[0].angular_span_degrees=180
    with pytest.raises(ValueError): review_drawing_dimension(job,"PDF-DIM-1",request(),confirmed_at="now",axis=AXIS)


def test_endpoint_persists_audit_and_invalidates_old_operations(monkeypatch,tmp_path):
    job=source_job(monkeypatch,tmp_path)
    job.plan.manufacturing_requirements=drawing_requirements(job.drawing_analysis)
    job.plan.manufacturing_requirements.requirements.append(ManufacturingRequirement(id="INSPECT-1",type="roughness",mapping_status="not_applicable",verification_status="verified_measurement",source={"report":"inspection-001"}))
    job.plan.setups[0].operations[0].status="approved"
    client=TestClient(main.app)
    response=client.put(f"/api/v1/jobs/{job.id}/drawing-dimensions/PDF-DIM-1/review",json=request().model_dump())
    assert response.status_code==200,response.text
    assert (tmp_path/"manufacturing-requirements.json").is_file()
    assert response.json()["plan"]["setups"][0]["operations"][0]["status"]=="proposed"
    retained=next(r for r in response.json()["plan"]["manufacturing_requirements"]["requirements"] if r["id"]=="INSPECT-1")
    assert retained["verification_status"]=="verified_measurement" and retained["source"]["report"]=="inspection-001"
    assert client.put(f"/api/v1/jobs/{job.id}/drawing-dimensions/PDF-DIM-1/review",json=request().model_dump()).status_code==409
