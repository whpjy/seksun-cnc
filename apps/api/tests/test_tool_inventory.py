from fastapi.testclient import TestClient

from app import main
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.main import app
from app.tool_inventory import ToolInventoryInput, physical_tool_fit_for_groove, record_physical_tool
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
