from __future__ import annotations

from app.manufacturing_knowledge import (
    get_manufacturing_process,
    load_manufacturing_library,
    manufacturing_process_payload,
    planning_knowledge_context,
)
from app.operation_library import OPERATION_DEFINITIONS


def test_catalog_import_is_complete_and_traceable() -> None:
    payload = load_manufacturing_library()
    assert payload["summary"]["process_count"] == 98
    assert payload["summary"]["family_counts"] == {
        "cutting": 43,
        "special": 10,
        "heat_surface": 29,
        "quality": 16,
    }
    assert len(payload["typical_routes"]) == 8
    assert len(payload["tolerance_roughness_rules"]) == 30
    assert len(payload["allowance_rules"]) == 21
    assert len(payload["standards_index"]) == 188
    assert len({item["code"] for item in payload["processes"]}) == 98
    assert len(payload["source"]["sha256"]) == 64


def test_every_process_has_planning_and_release_knowledge() -> None:
    payload = load_manufacturing_library()
    for process in payload["processes"]:
        assert process["name"]
        assert process["description"]
        assert process["quality_controls"]
        assert process["inspection"]
        assert process["applicability"]["applicable_conditions"]
        assert process["applicability"]["release_requirements"]
        assert process["approval_policy"] == "engineer_required"


def test_cam_mapping_only_references_real_operation_definitions() -> None:
    payload = load_manufacturing_library()
    operation_ids = {item.id for item in OPERATION_DEFINITIONS}
    mapping = payload["cam_operation_mapping"]
    assert set(mapping).issubset(operation_ids)
    assert len(mapping) == 25
    assert mapping["drilling"] == "GX-C-11"
    assert mapping["surface_3d"] == "GX-C-09"
    assert mapping["turn_od_roughing"] == "GX-C-01"
    assert mapping["turn_od_finishing"] == "GX-C-03"
    assert mapping["turn_threading"] == "GX-C-06"
    assert mapping["axial_drilling"] == "GX-C-11"


def test_catalog_can_be_filtered_and_compacted() -> None:
    payload = manufacturing_process_payload(family="special", query="EDM", compact=True)
    assert payload["summary"]["result_count"] >= 1
    assert all(item["family"] == "special" for item in payload["processes"])
    assert all("description" not in item for item in payload["processes"])
    assert get_manufacturing_process("gx-c-11")["name"] == "钻孔"


def test_ai_context_contains_route_and_cam_boundaries() -> None:
    context = planning_knowledge_context()
    assert len(context["processes"]) == 98
    assert len(context["typical_routes"]) == 8
    assert any(item["cam_operation_ids"] for item in context["processes"])
