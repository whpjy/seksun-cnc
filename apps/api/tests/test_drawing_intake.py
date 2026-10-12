import json
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app.catalogs import resolve_material
from app.cnc_mcp import CncApiClient, resolve_drawing_import, CncApiError
from app.drawing_intake import DrawingEvidence, drawing_requirements, validate_evidence
from app.process_rules import operation_policy_parameters
from app.stock_selection import StockSelectionRule, _select_for_job


def sample_evidence(**updates):
    return DrawingEvidence.model_validate({"material": "SUS316L", "maximum_outer_diameter_mm": 24,
        "maximum_outer_diameter_evidence": "第1页⑨ Ø24.0", "stock_note": "直径24棒材",
        "dimensions": [{"role": "outer_diameter", "nominal": 24, "page": 1},
                       {"role": "length", "nominal": 96, "page": 2},
                       {"role": "inner_diameter", "nominal": 20.2, "page": 1},
                       {"role": "radial_hole", "nominal": 1.5, "quantity": 4, "page": 1,
                        "tolerance_upper": .08, "tolerance_lower": .03}], **updates})


def test_stock_diameter_is_external_dimension_not_largest_pdf_number_or_stock_note():
    assert validate_evidence(sample_evidence(), 24)["stock_ready"]
    wrong = validate_evidence(sample_evidence(maximum_outer_diameter_mm=96), 24)
    assert not wrong["stock_ready"]
    mismatch = validate_evidence(sample_evidence(), 25, confirmed=True)
    assert not mismatch["stock_ready"]  # manual confirmation cannot hide incompatible geometry


def test_missing_quantities_remain_review_data_and_angles_keep_units():
    evidence = sample_evidence(dimensions=[{"role":"angle", "nominal":60, "page":1,"quantity":None,"label":None}])
    dimension = evidence.dimensions[0]
    assert dimension.quantity == 1 and not dimension.quantity_specified
    requirements = drawing_requirements(evidence.model_dump())
    assert requirements.requirements[0].unit == "deg"


def test_drawing_requirements_preserve_quantity_tolerance_and_do_not_pretend_verified():
    reqs = drawing_requirements(validate_evidence(sample_evidence(), 24))
    holes = next(r for r in reqs.requirements if r.subtype == "radial_hole")
    assert holes.quantity == 4
    assert holes.minimum == pytest.approx(1.53) and holes.maximum == pytest.approx(1.58)
    assert not holes.cad_feature_ids and holes.mapping_status == "unmapped"
    assert reqs.status == "review"


def test_pdf_in_same_upload_and_invalid_pdf_rolls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    class NoWorker:
        def __init__(self, **kw): pass
        def start(self): pass
    monkeypatch.setattr(main.threading, "Thread", NoWorker)
    client = TestClient(main.app)
    invalid = client.post("/api/v1/jobs/intake", files={"step": ("part.stp", b"step"), "drawing": ("part.pdf", b"not pdf")})
    assert invalid.status_code == 400 and not list(tmp_path.iterdir())
    corrupt = client.post("/api/v1/jobs/intake", files={"step": ("part.stp", b"step"), "drawing": ("part.pdf", b"%PDF-1.4\n")})
    assert corrupt.status_code == 422 and not list(tmp_path.iterdir())
    from pypdf import PdfWriter
    writer = PdfWriter(); writer.add_blank_page(width=600, height=400)
    buffer = io.BytesIO(); writer.write(buffer)
    valid = client.post("/api/v1/jobs/intake", files={"step": ("part.stp", b"step"), "drawing": ("part.pdf", buffer.getvalue())})
    assert valid.status_code == 200
    job = valid.json()
    assert job["drawing_filename"] == "part.pdf" and job["drawing_url"].endswith("drawing.pdf")
    assert (tmp_path / job["id"] / "drawing.pdf").read_bytes().startswith(b"%PDF-")


def test_mcp_paired_upload_and_pdf_path_boundary(monkeypatch, tmp_path):
    step, drawing = tmp_path / "part.step", tmp_path / "part.pdf"
    step.write_bytes(b"STEP"); drawing.write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("CNC_MCP_IMPORT_ROOTS", str(tmp_path))
    assert resolve_drawing_import(str(drawing)) == drawing
    monkeypatch.setenv("CNC_MCP_IMPORT_ROOTS", str(tmp_path / "allowed"))
    with pytest.raises(CncApiError): resolve_drawing_import(str(drawing))
    def handler(request):
        body = request.read()
        assert b'name="step"' in body and b'name="drawing"' in body
        return httpx.Response(200, json={"id": "paired"})
    client = CncApiClient(base_url="http://api", transport=httpx.MockTransport(handler))
    assert client.upload_step(step, drawing=drawing, device_id="citizen-cincom-l32", material="待确认")["id"] == "paired"


def test_unrecognized_material_is_not_aluminium_and_policy_only_applies_after_confirmation():
    material = resolve_material("SUS316L")
    assert material.name == "SUS316L" and material.id != "al-6061-t6"
    assert material.parameter_status == "unconfirmed"
    draft = {"finishing_allowance_mm": .05, "customer_confirmed": False}
    assert "stock_allowance_mm" not in operation_policy_parameters("turn_id_roughing", {}, draft)
    confirmed = {**draft, "customer_confirmed": True, "evidence": "客户工艺卡"}
    assert operation_policy_parameters("turn_id_roughing", {}, confirmed)["stock_allowance_mm"] == .05
    assert operation_policy_parameters("turn_od_roughing", {"stock_allowance_mm": .02}, confirmed)["stock_allowance_mm"] == .02


def test_drawing_revision_and_material_edit_invalidate_accepted_trial_evidence(monkeypatch, tmp_path):
    from test_process_preview import prepare
    from test_l32_operation_trial import rotational, snapshot
    from app.agent.l32_operation_trial import evidence_context_signature
    source, _, _ = prepare(monkeypatch, tmp_path)
    before = evidence_context_signature(source, rotational(), snapshot())
    source.drawing_analysis = {"sha256": "drawing-a", "revision_counter": 1}
    one = evidence_context_signature(source, rotational(), snapshot())
    source.drawing_analysis["revision_counter"] = 2
    two = evidence_context_signature(source, rotational(), snapshot())
    source.material = "SUS316L"
    three = evidence_context_signature(source, rotational(), snapshot())
    assert len({before, one, two, three}) == 4


def test_geometric_coverage_does_not_approve_unverified_pdf_tolerances(monkeypatch, tmp_path):
    from test_process_preview import prepare
    from app.coverage import evaluate_plan_coverage
    source, _, _ = prepare(monkeypatch, tmp_path)
    source.plan.manufacturing_requirements = drawing_requirements(validate_evidence(sample_evidence(), 24))
    coverage = evaluate_plan_coverage(source.analysis, source.plan)
    assert not coverage.production_ready
    assert any("二维图纸" in issue for issue in coverage.issues)


def test_engineer_confirmation_updates_requirements_and_invalidates_old_plan(monkeypatch, tmp_path):
    from test_process_preview import prepare
    source, _, _ = prepare(monkeypatch, tmp_path)
    source.drawing_filename = "part.pdf"
    source.drawing_analysis = validate_evidence(sample_evidence(), 24)
    before = source.plan.setups[0].operations[0]
    before.status = "approved"
    response = TestClient(main.app).put(f"/api/v1/jobs/{source.id}/drawing-analysis/confirm",
        json={"maximum_outer_diameter_mm":24,"material":"SUS316L","maximum_outer_diameter_evidence":"第1页⑨经核对"})
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["drawing_analysis"]["revision_counter"] == 1
    assert changed["drawing_analysis"]["confirmed_by_engineer"]
    assert changed["plan"]["stock"]["drawing_evidence_changed"]
    assert changed["plan"]["setups"][0]["operations"][0]["status"] == "proposed"
    assert changed["plan"]["material_profile"]["parameter_status"] == "unconfirmed"
    assert changed["plan"]["manufacturing_requirements"]["status"] == "review"


def test_pdf_stock_selection_carries_evidence_and_blocks_missing_evidence(monkeypatch):
    monkeypatch.setattr("app.l32_planner._solid_axial_span", lambda *args: None)
    axis = {"origin": {"x":0,"y":0,"z":0},"direction":{"x":0,"y":0,"z":1}}
    profile = SimpleNamespace(points=[SimpleNamespace(z=0, radius=12),SimpleNamespace(z=19.7,radius=12)])
    audit = validate_evidence(sample_evidence(),24)
    job = SimpleNamespace(analysis=None,drawing_filename="part.pdf",drawing_analysis=audit)
    stock = _select_for_job(job,profile,axis,StockSelectionRule())
    assert stock["diameter_mm"] == 24.2 and stock["solid"]
    assert stock["selection"]["diameter_source"] == "drawing"
    assert stock["selection"]["stock_note"] == "直径24棒材"
    job.drawing_analysis=None
    with pytest.raises(HTTPException): _select_for_job(job,profile,axis,StockSelectionRule())
    job.drawing_analysis={**audit,"maximum_outer_diameter_mm":23.98}
    with pytest.raises(HTTPException): _select_for_job(job,profile,axis,StockSelectionRule(diameter_increment_mm=0))
