import hashlib
import json

from app.agent.l32_operation_trial import (
    accept_trial,
    _downstream_groove_capability_gaps, _downstream_material_contracts,
    _profile_for_operation,
    evidence_context_signature,
    operation_signature,
    reconcile_state,
    run_independent_trial,
    state_summary,
)
from app.catalogs import get_tool
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.models import (
    GeometryAnalysis, JobResponse, L32OperationRepairCandidate,
    Operation, ProcessPlan, Setup, Vec3,
)
from app.rotational_features import (
    AIProvisionalProfileDecision,
    RotationalAxisCandidate,
    RotationalFeatureAnalysis,
    RotationalProfile,
    RotationalProfilePoint,
    TurningProfileFeature,
)
from types import SimpleNamespace


def analysis() -> GeometryAnalysis:
    return GeometryAnalysis.model_validate({
        "schema_version": "0.5.0",
        "source_file": "part.step",
        "topology": {"solids": 1, "faces": 8, "edges": 16},
        "measurements": {
            "volume": 1000,
            "surface_area": 600,
            "bounding_box": {
                "minimum": {"x": -10, "y": -10, "z": -30},
                "maximum": {"x": 10, "y": 10, "z": 0},
                "size": {"x": 20, "y": 20, "z": 30},
            },
        },
        "planar_features": [], "cylindrical_features": [], "visual_edges": [],
    })


def profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-30, radius=6),
            RotationalProfilePoint(z=-10, radius=6),
            RotationalProfilePoint(z=0, radius=10),
        ],
        confidence=1, review_state="accepted",
    )


def rotational() -> RotationalFeatureAnalysis:
    return RotationalFeatureAnalysis(
        source_file="part.step", status="candidate",
        axes=[RotationalAxisCandidate(
            id="RA-1", origin=Vec3(x=0, y=0, z=0), direction=Vec3(x=0, y=0, z=1),
            confidence=1, source_feature_ids=[], review_state="accepted",
        )],
        profiles=[profile()],
    )


def operation(operation_id: str, sequence: int, kind: str) -> Operation:
    common = {
        "spindle_mode": "constant_surface_speed",
        "cutting_speed_m_min": 100,
        "maximum_spindle_rpm": 8000,
        "feed_per_revolution_mm": 0.08,
    }
    if kind == "turn_facing":
        common.update({"face_z_mm": 0, "center_overtravel_mm": 0.2})
        tool = get_tool("TURN-OD-R")
    else:
        common.update({"radial_allowance_mm": 0})
        tool = get_tool("TURN-OD-F")
    return Operation(
        id=operation_id, sequence=sequence, type=kind, name=kind,
        feature_ids=["RP-1"], tool=tool, parameters=common,
        rationale=["test"], confidence=1, definition_id=kind,
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
    )


def job(*operations: Operation) -> JobResponse:
    plan = ProcessPlan(
        title="Harness draft", material="SUS303", machine="Citizen Cincom L32",
        stock={"diameter_mm": 24, "rotational_profile_id": "RP-1"},
        setups=[Setup(
            id="SETUP-1", name="front", work_axis=Vec3(x=0, y=0, z=1),
            datum_feature_id=None, fixture="guide bushing", operations=list(operations),
        )],
        warnings=[], assumptions=[], estimated_minutes=0,
        automation_status="review", ai_planning={"planning_owner": "deepseek_harness"},
    )
    return JobResponse(
        id="a" * 32, status="completed", filename="part.step",
        created_at="2026-01-01T00:00:00Z", material="SUS303", machine="Citizen Cincom L32",
        device_id="citizen-cincom-l32", machine_instance_id="l32-trial",
        analysis=analysis(), plan=plan,
    )


def snapshot():
    return snapshot_l32_instance(MachineInstance(
        id="l32-trial", definition_id="citizen-cincom-l32", name="trial",
        serial_number="SERIAL-TRIAL", manufacture_year=2020, variant="VIII",
        controller_revision="M70LPC-VU-site-1", operation_mode="guide_bushing",
        installed_modules=[], bar_diameter_mm=32,
    ))


def test_independent_trial_enforces_order_and_carries_cumulative_stock() -> None:
    source = job(
        operation("OP10", 10, "turn_facing"),
        operation("OP20", 20, "turn_od_finishing"),
    )

    first, state = run_independent_trial(
        job=source, operation_id="OP10", rotational=rotational(), snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )
    assert first["status"] in {"passed", "warning"}
    assert first["can_accept"] is True
    accepted = accept_trial(job=source, state=state, trial=first, operation_id="OP10")

    second, _ = run_independent_trial(
        job=source, operation_id="OP20", rotational=rotational(), snapshot=snapshot(), state=accepted,
        resolution_mm=0.5,
    )
    assert second["can_accept"] is True
    assert second["evidence"]["continued_from_operation_id"] == "OP10"
    assert second["evidence"]["simulation_domain"] == first["evidence"]["simulation_domain"]
    assert (
        second["cumulative_simulation"]["metrics"]["remaining_volume_mm3"]
        <= first["cumulative_simulation"]["metrics"]["remaining_volume_mm3"]
    )
    assert state_summary(source, accepted)["next_operation_id"] == "OP20"


def test_facing_is_blocked_when_it_cuts_through_protected_profile() -> None:
    planned = operation("OP10", 10, "turn_facing")
    planned.parameters["face_z_mm"] = -10
    source = job(planned)

    trial, _ = run_independent_trial(
        job=source, operation_id="OP10", rotational=rotational(), snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )

    assert trial["status"] == "blocked"
    assert trial["can_accept"] is False
    assert trial["reason"] == "profile_overcut"
    assert trial["evidence"]["verification_status"] == "failed"
    assert trial["evidence"]["profile_protection_status"] == "failed"
    assert trial["evidence"]["profile_protection_metrics"]["overcut_sample_count"] > 0


def test_reconcile_discards_explicit_old_world_model_state() -> None:
    planned = operation("OP10", 10, "turn_facing")
    source = job(planned)
    stale_state = {
        "schema_version": "1.0.0",
        "accepted": [{
            "operation_id": "OP10",
            "operation_signature": operation_signature(planned),
            "cumulative_simulation": {"status": "completed", "samples": []},
        }],
    }

    reconciled = reconcile_state(source, stale_state)

    assert reconciled["schema_version"] == "1.1.0"
    assert reconciled["accepted"] == []


def test_accept_rejects_trial_from_old_world_model() -> None:
    source = job(operation("OP10", 10, "turn_facing"))
    trial, state = run_independent_trial(
        job=source, operation_id="OP10", rotational=rotational(), snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )
    trial["schema_version"] = "1.0.0"

    try:
        accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    except ValueError as error:
        assert "obsolete material world model" in str(error)
    else:
        raise AssertionError("expected obsolete trial evidence to be rejected")


def test_nonrotational_pocket_finish_promotes_feature_coverage_after_exact_evidence() -> None:
    pocket = operation("OP80", 80, "turn_od_finishing")
    pocket.type = "pocket_finishing"
    pocket.definition_id = "pocket_finishing"
    pocket.feature_ids = ["MF-1"]
    pocket.tool = get_tool("EM-1")
    pocket.parameters = {"depth_mm": 2.55, "step_down_mm": 0.3}
    source = job(pocket)
    evidence = {
        "executor": "exact_occ_indexed_pocket_sweep",
        "machine_capability_status": "passed",
        "target_protection_status": "passed",
        "material_removal_status": "passed",
        "operation_coverage_status": "passed",
        "removed_volume_mm3": 11.8,
        "remaining_feature_material_mm3": 0.0,
    }

    trial, state = run_independent_trial(
        job=source, operation_id="OP80", rotational=rotational(), snapshot=snapshot(),
        state=None, resolution_mm=0.5, nonrotational_evidence=evidence,
    )
    assert trial["can_accept"] is True
    assert trial["evidence"]["material_domain"] == "exact_3d_occ"
    assert trial["evidence"]["verified_prismatic_feature_ids"] == ["MF-1"]
    accepted = accept_trial(job=source, state=state, trial=trial, operation_id="OP80")
    assert state_summary(source, accepted)["verified_prismatic_feature_ids"] == ["MF-1"]


def test_nonrotational_finish_blocks_incomplete_exact_coverage() -> None:
    finish = operation("OP80", 80, "turn_od_finishing")
    finish.type = "live_tool_contour_finishing"
    finish.definition_id = "live_tool_contour_finishing"
    finish.tool = get_tool("EM-1")
    source = job(finish)
    evidence = {
        "executor": "exact_occ_nonrotational_exterior_sweep",
        "machine_capability_status": "passed",
        "target_protection_status": "passed",
        "material_removal_status": "passed",
        "operation_coverage_status": "failed",
        "remaining_excess_region_mm3": 1.2,
    }

    trial, _ = run_independent_trial(
        job=source, operation_id="OP80", rotational=rotational(), snapshot=snapshot(),
        state=None, resolution_mm=0.5, nonrotational_evidence=evidence,
    )
    assert trial["status"] == "blocked"
    assert trial["reason"] == "nonrotational_verification_failed"
    assert trial["can_accept"] is False


def test_independent_trial_accepts_bounded_ai_provisional_profile_for_draft() -> None:
    source = job(operation("OP10", 10, "turn_facing"))
    geometry = rotational()
    geometry.profiles[0].review_state = "ai_provisional"
    geometry.profiles[0].provisional_decision = AIProvisionalProfileDecision(
        scope="partial", z_min_mm=-10, z_max_mm=0,
        diameter_min_mm=12, diameter_max_mm=20, confidence=0.82,
        rationale="Exact section and operation target agree within the bounded front region.",
        evidence_refs=["geometry:exact-section", "operation:OP10"],
    )

    trial, _ = run_independent_trial(
        job=source, operation_id="OP10", rotational=geometry, snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )

    assert trial["status"] in {"passed", "warning"}
    assert trial["profile_authorization"]["source"] == "ai"
    assert trial["profile_authorization"]["scope"] == "partial"
    assert trial["profile_authorization"]["production_ready"] is False


def test_independent_trial_blocks_target_outside_ai_provisional_profile_scope() -> None:
    planned = operation("OP10", 10, "turn_facing")
    planned.parameters["face_z_mm"] = 1
    source = job(planned)
    geometry = rotational()
    geometry.profiles[0].review_state = "ai_provisional"
    geometry.profiles[0].provisional_decision = AIProvisionalProfileDecision(
        scope="partial", z_min_mm=-10, z_max_mm=0,
        diameter_min_mm=12, diameter_max_mm=20, confidence=0.82,
        rationale="Only the extracted front region has sufficient evidence for a draft trial.",
        evidence_refs=["geometry:exact-section"],
    )

    trial, _ = run_independent_trial(
        job=source, operation_id="OP10", rotational=geometry, snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )

    assert trial["status"] == "blocked"
    assert trial["reason"] == "profile_review_required"
    assert "outside the AI-authorized profile scope" in trial["detail"]


def test_warning_trial_requires_explicit_reasoned_acknowledgement() -> None:
    source = job(operation("OP10", 10, "turn_facing"))
    trial, state = run_independent_trial(
        job=source, operation_id="OP10", rotational=rotational(), snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )
    trial["status"] = "warning"
    trial["can_accept"] = True
    trial["requires_warning_acknowledgement"] = True
    try:
        accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    except ValueError as error:
        assert "explicit acknowledgement" in str(error)
    else:
        raise AssertionError("expected warning acknowledgement gate")
    fully_accepted = accept_trial(
        job=source, state=state, trial=trial, operation_id="OP10",
        decision_rationale="roughing excess stock is intentionally left for the finishing pass",
        acknowledge_warning=True,
    )
    assert [item["operation_id"] for item in fully_accepted["accepted"]] == ["OP10"]


def test_independent_trial_rejects_skipping_predecessor() -> None:
    source = job(
        operation("OP10", 10, "turn_facing"),
        operation("OP20", 20, "turn_od_finishing"),
    )
    try:
        run_independent_trial(
            job=source, operation_id="OP20", rotational=rotational(), snapshot=snapshot(), state=None,
        )
    except ValueError as error:
        assert "expects OP10" in str(error)
    else:
        raise AssertionError("expected sequence gate")


def test_reconcile_state_discards_evidence_after_accepted_operation_edit() -> None:
    source = job(operation("OP10", 10, "turn_facing"))
    trial, state = run_independent_trial(
        job=source, operation_id="OP10", rotational=rotational(), snapshot=snapshot(), state=None,
        resolution_mm=0.5,
    )
    accepted = accept_trial(job=source, state=state, trial=trial, operation_id="OP10")
    source.plan.setups[0].operations[0].parameters["feed_per_revolution_mm"] = 0.09

    reconciled = reconcile_state(source, accepted)

    assert reconciled["accepted"] == []


def test_groove_target_resolves_separate_reference_profile() -> None:
    source = operation("OP40", 40, "turn_od_finishing")
    source.type = "turn_grooving"
    source.definition_id = "turn_grooving"
    source.feature_ids = ["GF-1"]
    source.reference_profile_id = "RP-1"
    geometry = rotational()
    geometry.features = [TurningProfileFeature(
        id="GF-1", profile_id="RP-1", kind="external_groove_candidate",
        z_start=-5, z_end=-4, radius_start=5, radius_end=5,
        width_mm=1, depth_mm=1, source_point_indices=[0, 1],
        confidence=1, review_state="review",
    )]

    assert _profile_for_operation(source, geometry).id == "RP-1"


def test_reconcile_invalidates_contract_when_responsible_operation_changes() -> None:
    finish = operation("OP20", 20, "turn_od_finishing")
    groove = operation("OP40", 40, "turn_od_finishing")
    source = job(finish, groove)
    state = {
        "accepted": [{
            "operation_id": "OP20",
            "operation_signature": operation_signature(finish),
            "cumulative_simulation": {"status": "completed", "samples": []},
            "deferred_material_contracts": [{
                "responsible_operation_id": "OP40",
                "responsible_operation_signature": operation_signature(groove),
                "verification_version": "1.1.0",
            }],
        }],
    }
    groove.parameters["feed_per_revolution_mm"] = 0.11

    assert reconcile_state(source, state)["accepted"] == []


def test_reconcile_state_discards_evidence_after_stock_or_geometry_context_change() -> None:
    source = job(operation("OP10", 10, "turn_facing"))
    geometry = rotational()
    machine = snapshot()
    trial, state = run_independent_trial(
        job=source, operation_id="OP10", rotational=geometry, snapshot=machine, state=None,
        resolution_mm=0.5,
    )
    context = evidence_context_signature(source, geometry, machine)
    accepted = accept_trial(
        job=source, state=state, trial=trial, operation_id="OP10",
        context_signature=context,
    )
    source.plan.stock["diameter_mm"] = 25
    changed_context = evidence_context_signature(source, geometry, machine)

    reconciled = reconcile_state(source, accepted, changed_context)

    assert reconciled["accepted"] == []


def test_candidate_sandbox_changes_a_copy_without_mutating_formal_plan() -> None:
    from app.main import _apply_l32_trial_candidate

    source = job(operation("OP10", 10, "turn_facing"))
    original_feed = source.plan.setups[0].operations[0].parameters["feed_per_revolution_mm"]
    candidate = L32OperationRepairCandidate(
        id="lower-feed", rationale="reduce cutting load after the blocked trial",
        parameters={"feed_per_revolution_mm": 0.04},
    )

    candidate_job, candidate_operation = _apply_l32_trial_candidate(source, "OP10", candidate)

    assert candidate_operation.parameters["feed_per_revolution_mm"] == 0.04
    assert candidate_job.plan.setups[0].operations[0].parameters["feed_per_revolution_mm"] == 0.04
    assert source.plan.setups[0].operations[0].parameters["feed_per_revolution_mm"] == original_feed
    assert "lower-feed" in candidate_operation.rationale[-1]


def test_candidate_sandbox_preserves_explicit_catalog_tool_identity() -> None:
    from app.main import _apply_l32_trial_candidate

    source = job(operation("OP10", 10, "turn_od_finishing"))
    source.plan.setups[0].operations[0].parameters["cut_direction"] = "negative_z"
    candidate = L32OperationRepairCandidate(
        id="micro-finish",
        rationale="Use the right-hand micro finishing insert",
        tool_id="TURN-OD-MICRO-F",
    )

    _, candidate_operation = _apply_l32_trial_candidate(source, "OP10", candidate)

    assert candidate_operation.tool.id == "TURN-OD-MICRO-F"
    assert candidate_operation.tool.hand == "right"


def test_candidate_can_repair_a_downstream_operation_without_mutating_current_one() -> None:
    from app.main import _apply_l32_trial_candidate

    current = operation("OP30", 30, "turn_od_finishing")
    downstream = operation("OP33-G1", 33, "turn_grooving")
    downstream.tool = get_tool("TURN-GROOVE-2")
    downstream.parameters.update({
        "groove_width_mm": 2.0, "groove_depth_mm": 1.0,
        "final_diameter_mm": 4.0, "z_mm": 0.0, "peck_depth_mm": 0.5,
    })
    source = job(current, downstream)
    candidate = L32OperationRepairCandidate(
        id="narrow-downstream-tool", target_operation_id="OP33-G1",
        rationale="Use a narrower verified groove tool for the exact shoulder envelope.",
        tool_id="TURN-GROOVE-0.8",
        parameters={"exact_groove_envelope": True},
    )

    candidate_job, candidate_operation = _apply_l32_trial_candidate(source, "OP30", candidate)

    assert candidate_operation.id == "OP33-G1"
    assert candidate_operation.tool.id == "TURN-GROOVE-0.8"
    assert candidate_operation.parameters["exact_groove_envelope"] is True
    unchanged_current = next(
        item for setup in candidate_job.plan.setups for item in setup.operations if item.id == "OP30"
    )
    assert unchanged_current.tool.id == current.tool.id


def groove_profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-G", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-2, radius=2),
            RotationalProfilePoint(z=-1, radius=2),
            RotationalProfilePoint(z=-0.45, radius=1),
            RotationalProfilePoint(z=0.45, radius=1),
            RotationalProfilePoint(z=1, radius=2),
            RotationalProfilePoint(z=2, radius=2),
        ], confidence=1, review_state="accepted",
    )


def test_default_grooving_capability_metadata_does_not_change_legacy_signature() -> None:
    source = operation("OP30", 30, "turn_od_finishing")
    payload = source.model_dump(mode="json")
    payload.pop("reference_profile_id", None)
    payload["tool"].pop("groove_profile", None)
    payload["tool"].pop("axial_contouring_supported", None)
    payload["tool"].pop("inventory_id", None)
    expected = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    assert operation_signature(source) == expected


def test_excess_stock_can_be_assigned_to_exact_downstream_groove() -> None:
    finish = operation("OP30", 30, "turn_od_finishing")
    finish.feature_ids = ["RP-G"]
    groove_tool = get_tool("TURN-GROOVE-0.8").model_copy(deep=True)
    groove_tool.id = "TURN-GROOVE-0.25-TEST"
    groove_tool.diameter_mm = 0.25
    groove_tool.cutting_width_mm = 0.25
    groove = Operation(
        id="OP33-G1", sequence=33, type="turn_grooving", name="groove",
        feature_ids=["RP-G", "GF-1"], tool=groove_tool,
        parameters={"groove_width_mm": 0.9, "peck_depth_mm": 0.5},
        rationale=["test"], confidence=1, definition_id="turn_grooving",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
    )
    geometry = rotational()
    geometry.profiles = [groove_profile()]
    geometry.features = [TurningProfileFeature(
        id="GF-1", profile_id="RP-G", kind="external_groove_candidate",
        z_start=-0.45, z_end=0.45, radius_start=1, radius_end=1,
        width_mm=0.9, depth_mm=1, source_point_indices=[2, 3],
        confidence=1, review_state="review",
    )]
    verification = SimpleNamespace(deviations=[
        SimpleNamespace(kind="excess_stock", z=-0.3, target_radius_mm=1.0),
        SimpleNamespace(kind="excess_stock", z=0.3, target_radius_mm=1.0),
    ])

    contracts = _downstream_material_contracts(
        operation=finish, operations=[finish, groove], rotational=geometry,
        verification=verification, stock_radius_mm=3,
    )

    assert contracts[0]["responsible_operation_id"] == "OP33-G1"
    assert contracts[0]["sample_count"] == 2


def test_wide_downstream_groove_tool_is_reported_as_capability_gap() -> None:
    finish = operation("OP30", 30, "turn_od_finishing")
    finish.feature_ids = ["RP-G"]
    groove = Operation(
        id="OP33-G1", sequence=33, type="turn_grooving", name="groove",
        feature_ids=["RP-G", "GF-1"], tool=get_tool("TURN-GROOVE-0.8"),
        parameters={"groove_width_mm": 0.9, "peck_depth_mm": 0.5},
        rationale=["test"], confidence=1, definition_id="turn_grooving",
        source="recommendation", channel_id="main", spindle_id="main", workpiece_side="front",
        enabled=True,
    )
    geometry = rotational()
    geometry.profiles = [groove_profile()]
    geometry.features = [TurningProfileFeature(
        id="GF-1", profile_id="RP-G", kind="external_groove_candidate",
        z_start=-0.45, z_end=0.45, radius_start=1, radius_end=1,
        width_mm=0.9, depth_mm=1, source_point_indices=[2, 3],
        confidence=1, review_state="review",
    )]
    verification = SimpleNamespace(deviations=[
        SimpleNamespace(kind="excess_stock", z=-0.8, target_radius_mm=1.64),
        SimpleNamespace(kind="excess_stock", z=0.8, target_radius_mm=1.64),
    ])

    gaps = _downstream_groove_capability_gaps(
        operation=finish, operations=[finish, groove], rotational=geometry,
        verification=verification, stock_radius_mm=3,
    )

    assert gaps[0]["responsible_operation_id"] == "OP33-G1"
    assert gaps[0]["tool_width_mm"] == 0.8
    assert gaps[0]["uncovered_sample_count"] == 2
    assert gaps[0]["next_action"] == "select_narrower_groove_tool_or_split_contour_grooving_strategy"


def test_reconcile_invalidates_deferred_evidence_if_responsible_operation_is_removed() -> None:
    finish = operation("OP30", 30, "turn_od_finishing")
    groove = operation("OP33-G1", 33, "turn_grooving")
    source = job(finish, groove)
    state = {
        "accepted": [{
            "operation_id": "OP30",
            "operation_signature": operation_signature(finish),
            "cumulative_simulation": {"status": "completed", "samples": []},
            "deferred_material_contracts": [{
                "responsible_operation_id": "OP33-G1",
                "verification_version": "1.1.0",
            }],
        }],
    }
    source.plan.setups[0].operations = [finish]

    assert reconcile_state(source, state)["accepted"] == []
