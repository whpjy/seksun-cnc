from app.catalogs import catalog_payload, get_tool


def test_manufacturer_catalog_resources_are_available_without_inventory_binding() -> None:
    payload = catalog_payload()

    policy = payload["resource_availability_policy"]
    assert policy["mode"] == "catalog_resources_assumed_available"
    assert policy["requires_physical_inventory_binding"] is False
    evidence_ids = {item["id"] for item in payload["manufacturer_evidence"]}
    assert {
        "citizen-l32-brochure",
        "tungaloy-drilling-dsm",
        "tungaloy-turning-jtter",
        "tungaloy-er-collet-system",
    } <= evidence_ids


def test_documented_micro_drills_and_narrow_groove_tool_are_selectable() -> None:
    micro_drill = get_tool("TUNGALOY-DSM-0.5")
    narrow_groove = get_tool("TUNGALOY-JTTER-1.2")

    assert micro_drill.kind == "drill"
    assert micro_drill.diameter_mm == 0.5
    assert narrow_groove.kind == "grooving"
    assert narrow_groove.cutting_width_mm == 1.2
