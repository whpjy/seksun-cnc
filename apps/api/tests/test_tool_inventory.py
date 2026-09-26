from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app import main
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.main import app
from app.tool_inventory import (
    ToolInventoryInput,
    bind_verified_inventory_tool,
    inventory_binding_evidence_request,
    physical_tool_fit_for_groove,
    record_physical_tool,
)
from app.catalogs import get_tool


client = TestClient(app)


def _machine(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    instance = MachineInstance(
        id="l32-inventory-test",definition_id="citizen-cincom-l32",name="L32 test",
        variant="VIII",operation_mode="guide_bushing",installed_modules=["U150B"],
        bar_diameter_mm=32,
    )
    snapshot = snapshot_l32_instance(instance)
    path = main.machine_instance_path(instance.id)
    path.parent.mkdir(parents=True)
    main.write_json(path,snapshot.model_dump(mode="json"))
    return instance.id


def test_physical_tool_inventory_is_persisted_per_machine_without_verification(tmp_path, monkeypatch):
    instance_id = _machine(tmp_path,monkeypatch)
    path = f"/api/v1/machines/l32/instances/{instance_id}/tools"
    assert client.get(path).json()["tools"] == []
    payload = {
        "inventory_id":"GROOVE-01","catalog_tool_id":"",
        "custom_name":"0.8 mm external grooving tool","custom_kind":"grooving",
        "station":"T05","measured_cutting_width_mm":0.8,"measured_stickout_mm":18,
    }
    created = client.post(path,json=payload)
    assert created.status_code == 201
    assert created.json()["verification_state"] == "recorded"
    assert created.json()["measured_cutting_width_mm"] == 0.8
    assert client.post(path,json=payload).status_code == 409
    assert client.get(path).json()["tools"][0]["inventory_id"] == "GROOVE-01"
    assert client.post(path,json={**payload,"inventory_id":"BAD-2MM","catalog_tool_id":"TURN-GROOVE-2"}).status_code == 422
    assert client.get(path.replace(instance_id,"missing-machine")).status_code == 404
    updated = client.put(f"{path}/GROOVE-01",json={**payload,"active":False})
    assert updated.status_code == 200
    assert updated.json()["active"] is False
    assert len(client.get(path).json()["tools"]) == 1


def test_groove_width_screening_requires_measured_active_external_tool():
    source = ToolInventoryInput(
        inventory_id="G1",custom_name="narrow groove",custom_kind="grooving",measured_cutting_width_mm=0.8,
    )
    record = record_physical_tool("M1",source,None)
    assert physical_tool_fit_for_groove(record,0.9)
    assert not physical_tool_fit_for_groove(record,0.7)
    assert not physical_tool_fit_for_groove(record.model_copy(update={"active":False}),0.9)
    assert not physical_tool_fit_for_groove(record.model_copy(update={"measured_cutting_width_mm":None}),0.9)


def test_verified_full_radius_inventory_can_bind_engineering_candidate():
    source = ToolInventoryInput(
        inventory_id="FULL-R-04",
        custom_name="Measured 0.4 mm full-radius grooving tool",
        custom_kind="grooving",
        measured_cutting_width_mm=0.4,
        measured_nose_radius_mm=0.2,
        groove_profile="full_radius",
        axial_contouring_supported=True,
        capability_verified_by="process-engineer",
        capability_verification_reference="inspection-report-2026-09-26",
    )
    record = record_physical_tool("L32-01", source, None)

    assert record.verification_state == "capability_verified"
    bound = bind_verified_inventory_tool(
        record, get_tool("ENGINEERING-GROOVE-FULL-R-0.4"),
    )
    assert bound.catalog_match is True
    assert bound.inventory_id == "FULL-R-04"
    assert bound.id == "INV-FULL-R-04"
    assert bound.groove_profile == "full_radius"
    assert bound.axial_contouring_supported is True


def test_axial_contouring_inventory_requires_measured_capability_evidence():
    with pytest.raises(ValidationError):
        ToolInventoryInput(
            inventory_id="UNVERIFIED",
            custom_name="Unverified contour tool",
            custom_kind="grooving",
            measured_cutting_width_mm=0.4,
            measured_nose_radius_mm=0.2,
            groove_profile="full_radius",
            axial_contouring_supported=True,
        )


def test_verified_inventory_binding_rejects_geometry_mismatch():
    source = ToolInventoryInput(
        inventory_id="FULL-R-06",
        custom_name="Measured 0.6 mm full-radius grooving tool",
        custom_kind="grooving",
        measured_cutting_width_mm=0.6,
        measured_nose_radius_mm=0.3,
        groove_profile="full_radius",
        axial_contouring_supported=True,
        capability_verified_by="process-engineer",
        capability_verification_reference="inspection-report-06",
    )
    record = record_physical_tool("L32-01", source, None)

    with pytest.raises(ValueError, match="width"):
        bind_verified_inventory_tool(
            record, get_tool("ENGINEERING-GROOVE-FULL-R-0.4"),
        )


def test_binding_evidence_request_lists_exact_missing_physical_evidence():
    request = inventory_binding_evidence_request(
        get_tool("ENGINEERING-GROOVE-FULL-R-0.4"), [],
    )

    assert request["status"] == "user_evidence_required"
    assert request["next_action"] == "request_user_tool_measurement"
    fields = {item["field"]: item for item in request["required_evidence"]}
    assert fields["measured_cutting_width_mm"]["expected_value"] == 0.4
    assert fields["measured_nose_radius_mm"]["expected_value"] == 0.2
    assert fields["axial_contouring_supported"]["expected_value"] is True
    assert "capability_verified_by" in fields
    assert "capability_verification_reference" in fields


def test_binding_evidence_request_returns_compatible_inventory_without_questions():
    source = ToolInventoryInput(
        inventory_id="FULL-R-04",
        custom_name="Measured full radius tool",
        custom_kind="grooving",
        measured_cutting_width_mm=0.4,
        measured_nose_radius_mm=0.2,
        groove_profile="full_radius",
        axial_contouring_supported=True,
        capability_verified_by="engineer",
        capability_verification_reference="inspection-42",
    )
    record = record_physical_tool("L32-01", source, None)

    request = inventory_binding_evidence_request(
        get_tool("ENGINEERING-GROOVE-FULL-R-0.4"), [record],
    )

    assert request["status"] == "ready_to_bind"
    assert request["next_action"] == "bind_l32_candidate_inventory_tool"
    assert request["compatible_inventory"][0]["inventory_id"] == "FULL-R-04"
