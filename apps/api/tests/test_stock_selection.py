import json

from fastapi.testclient import TestClient

import app.main as main
from app.stock_selection import StockSelectionRule, select_solid_bar
from app.agent.l32_operation_trial import accept_trial
from test_process_preview import prepare


def test_diameter_increment_is_diametral_and_rounding_never_reduces_stock():
    stock = select_solid_bar(24, -10, 10, StockSelectionRule())
    assert stock["diameter_mm"] == 24.2
    assert abs(stock["allowance_mm"]["radial"] - 0.1) < 1e-9
    assert stock["initial_bore_diameter_mm"] == 0 and stock["solid"]
    rounded = select_solid_bar(24.001, -10, 10, StockSelectionRule(diameter_round_up_mm=0.5, front_allowance_mm=1, back_allowance_mm=3))
    assert rounded["diameter_mm"] == 24.5
    assert rounded["z_min_mm"] == -13 and rounded["z_max_mm"] == 11
    assert rounded["length_mm"] == 24


def test_global_rule_persists_without_changing_existing_task_and_apply_invalidates_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("CNC_STORAGE_ROOT", str(tmp_path / "jobs"))
    source, trial, state = prepare(monkeypatch, tmp_path)
    accepted = accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(accepted))
    client = TestClient(main.app)
    path = f"/api/v1/jobs/{source.id}/stock-selection"
    response = client.put("/api/v1/process-rules/stock-selection", json={"diameter_increment_mm": 0.6, "front_allowance_mm": 1, "back_allowance_mm": 3})
    assert response.status_code == 200
    assert client.get("/api/v1/process-rules/stock-selection").json()["diameter_increment_mm"] == 0.6
    assert (tmp_path / "stock-selection-rule.json").is_file()
    data = client.get(path).json()
    assert data["stock"]["diameter_mm"] == 24
    assert data["proposed_stock"]["diameter_mm"] == 20.6
    assert all(sample["inner_radius"] == 0 for sample in data["view"]["before_samples"])
    assert main.get_l32_independent_operation_state(source.id)["accepted_count"] == 1
    response = client.post(path + "/apply-rule")
    assert response.status_code == 200, response.text
    selected = response.json()["plan"]["stock"]
    assert selected["diameter_mm"] == 20.6 and selected["selection"]["rule"]["revision"] == 2
    assert main.get_l32_independent_operation_state(source.id)["accepted_count"] == 0
    assert client.get(path).json()["view"]["before_samples"][0]["z"] == -33


def test_rule_rejects_negative_and_nonfinite_allowances():
    client = TestClient(main.app)
    response = client.put("/api/v1/process-rules/stock-selection", json={"diameter_increment_mm": -0.2})
    assert response.status_code == 422
