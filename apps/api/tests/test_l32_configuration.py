from fastapi.testclient import TestClient

from app import main
from app.l32_configuration import L32_DEFINITION, snapshot_l32_instance, validate_l32_instance
from app.machine_models import MachineInstance
from app.main import app
from app.models import JobResponse


client = TestClient(app)


def instance(**updates) -> MachineInstance:
    values = {
        "id": "l32-shop-01",
        "definition_id": "citizen-cincom-l32",
        "name": "L32 车间 01",
        "serial_number": "SERIAL-001",
        "manufacture_year": 2018,
        "variant": "XII",
        "controller_revision": "M70LPC-VU-site-1",
        "operation_mode": "guide_bushing",
        "installed_modules": ["U32B", "U120B", "U12B"],
        "enabled_options": [],
        "bar_diameter_mm": 32,
        "postprocessor_profile_id": "citizen-l32-m70-draft-v1",
    }
    values.update(updates)
    return MachineInstance.model_validate(values)


def test_l32_definition_derives_axes_for_all_four_variants() -> None:
    variants = {item.id: item for item in L32_DEFINITION.variants}

    assert set(variants) == {"VIII", "IX", "X", "XII"}
    assert "B" not in variants["VIII"].enabled_axes
    assert "B" in variants["IX"].enabled_axes
    assert "Y2" in variants["X"].enabled_axes
    assert {"B", "Y2"} <= set(variants["XII"].enabled_axes)


def test_public_device_record_is_derived_from_machine_definition() -> None:
    response = client.get("/api/v1/device-library/citizen-cincom-l32")

    assert response.status_code == 200
    record = response.json()
    assert record["machine_definition_id"] == L32_DEFINITION.id
    assert record["display_name"] == "Citizen Cincom L32"
    assert {item["id"] for item in record["variant_configurations"]} == {"VIII", "IX", "X", "XII"}
    assert next(item for item in record["axes"] if item["id"] == "B")["variants"] == ["IX", "XII"]
    assert {item["id"] for item in record["tooling"]["available_modules"]} >= {"U32B", "U12B"}
    binding_ids = {item["operation_id"] for item in record["operation_bindings"]}
    assert {"turn_facing", "turn_od_roughing", "turn_cutoff"} <= binding_ids


def test_operation_library_exposes_turning_provider_without_binding_it_to_virtual_mill() -> None:
    operations = client.get("/api/v1/operation-library").json()["definitions"]
    turning = [item for item in operations if item["engine"]["provider"] == "turning"]
    virtual = client.get("/api/v1/device-library/seksun-freecad-cam-standard").json()

    assert len(turning) == 10
    assert all(item["maturity"] == "experimental" and item["manual_enabled"] is False for item in turning)
    assert all(item["operation_id"] not in {definition["id"] for definition in turning} for item in virtual["operation_bindings"])


def test_valid_l32_xii_configuration_derives_module_capabilities() -> None:
    validation = validate_l32_instance(instance())

    assert validation.valid is True
    assert validation.production_ready is False
    assert any(item.code == "postprocessor_unqualified" for item in validation.issues)
    assert {"turning", "b_axis_indexed_machining", "y2_machining"} <= set(validation.capabilities)


def test_variant_rejects_module_when_required_axis_is_unavailable() -> None:
    validation = validate_l32_instance(instance(variant="VIII", installed_modules=["U32B"]))

    assert validation.valid is False
    assert {item.code for item in validation.issues} >= {"incompatible_module", "module_axis_missing"}


def test_optional_38mm_bar_requires_confirmed_option() -> None:
    blocked = validate_l32_instance(instance(bar_diameter_mm=38))
    enabled = validate_l32_instance(instance(bar_diameter_mm=38, enabled_options=["bar_diameter_38mm"]))

    assert blocked.valid is False
    assert any(item.code == "bar_option_missing" for item in blocked.issues)
    assert enabled.valid is True


def test_configuration_hash_is_stable_for_same_instance() -> None:
    first = snapshot_l32_instance(instance())
    second = snapshot_l32_instance(instance())

    assert first.configuration_hash == second.configuration_hash
    assert len(first.configuration_hash) == 64


def test_l32_instance_api_persists_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    payload = instance(id="l32-api-01").model_dump(mode="json")

    created = client.post("/api/v1/machines/l32/instances", json=payload)
    loaded = client.get("/api/v1/machines/l32/instances/l32-api-01")

    assert created.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["configuration_hash"] == created.json()["configuration_hash"]
    assert (tmp_path / ".machine-instances" / "l32-api-01.json").is_file()


def test_l32_instance_can_be_bound_to_job_as_immutable_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    job_id = "e" * 32
    directory = tmp_path / job_id
    directory.mkdir()
    main.save_job(directory, JobResponse(
        id=job_id,
        status="completed",
        filename="shaft.step",
        created_at=main.utc_now(),
        material="S45C",
        machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32",
    ))
    created = client.post(
        "/api/v1/machines/l32/instances",
        json=instance(id="l32-bound-01").model_dump(mode="json"),
    )

    bound = client.put(
        f"/api/v1/jobs/{job_id}/machine-instance",
        json={"machine_instance_id": "l32-bound-01"},
    )
    loaded = client.get(f"/api/v1/jobs/{job_id}/machine-instance")

    assert created.status_code == 200
    assert bound.status_code == 200
    assert bound.json()["machine_instance_id"] == "l32-bound-01"
    assert bound.json()["machine_configuration_hash"] == created.json()["configuration_hash"]
    assert loaded.status_code == 200
    assert loaded.json()["configuration_hash"] == created.json()["configuration_hash"]
    assert (directory / "machine-configuration.json").is_file()
