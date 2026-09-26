from app.agent.process_strategies import (
    BackLiveToolFaceStrategy,
    ValidationOutcome,
    evaluate_validation_contract,
    get_process_strategy,
    operations_for_role,
)
from app.catalogs import get_tool
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.models import ProcessPlan, Setup, Vec3
from app.operation_library import create_operation_instance


def _snapshot(with_back_live: bool = True):
    return snapshot_l32_instance(MachineInstance(
        id="l32-strategy-test",
        definition_id="citizen-cincom-l32",
        name="L32 strategy test",
        serial_number="STRATEGY-001",
        variant="VIII",
        controller_revision="M70LPC-VU-site-1",
        operation_mode="guide_bushing",
        installed_modules=["U151B"] if with_back_live else [],
        bar_diameter_mm=32,
    ))


def _plan(cutoff_id: str = "SEPARATE-PART") -> ProcessPlan:
    cutoff = create_operation_instance(
        id=cutoff_id, sequence=37, type="turn_cutoff", name="part separation",
        channel_id="main", spindle_id="main", workpiece_side="front",
        synchronization_group="SYNC-A", feature_ids=["PROFILE-A"],
        tool=get_tool("TURN-CUTOFF-2"),
        parameters={
            "cutting_width_mm": 2.0, "breakthrough_radius_mm": 0.1,
            "z_mm": -5.5, "spindle_rpm": 3000, "feed_per_revolution_mm": 0.05,
        },
        rationale=["test"], confidence=0.8,
    )
    return ProcessPlan(
        title="strategy test", material="S45C", machine="Citizen Cincom L32",
        stock={
            "diameter_mm": 8.0, "finished_back_z_mm": -4.5,
            "rotational_profile_id": "PROFILE-A",
        },
        setups=[
            Setup(
                id="PRIMARY-WORKHOLDING", name="main spindle", work_axis=Vec3(x=0, y=0, z=1),
                datum_feature_id="PROFILE-A", fixture="main collet", operations=[cutoff],
            ),
            Setup(
                id="REVERSE-WORKHOLDING", name="sub spindle back workholding",
                work_axis=Vec3(x=0, y=0, z=-1), datum_feature_id="BACK-DATUM",
                fixture="sub spindle collet", operations=[],
            ),
        ],
        warnings=[], assumptions=[], estimated_minutes=1,
    )


def test_back_live_strategy_binds_roles_without_fixed_operation_ids() -> None:
    original = _plan("CUT-FREEFORM-917")
    application = BackLiveToolFaceStrategy.apply(
        plan=original, snapshot=_snapshot(), material_name="S45C",
    )

    separation = operations_for_role(application.plan, "material_separation")[0][1]
    back_face = operations_for_role(application.plan, "back_face_finish")[0][1]
    assert application.role_bindings["material_separation"] == "CUT-FREEFORM-917"
    assert application.operation_id.startswith("AUTO-")
    assert application.operation_id != "OP50-ALT"
    assert back_face.id == application.operation_id
    assert back_face.parameters["dependency_operation_id"] == "CUT-FREEFORM-917"
    assert separation.parameters["back_face_allowance_mm"] == 0.25
    assert separation.parameters["retained_material_min_z_mm"] == -4.75
    assert original.stock.get("back_face_process") is None


def test_strategy_declares_draft_and_production_evidence_separately() -> None:
    candidate = BackLiveToolFaceStrategy.describe(capability_available=True)
    draft = {
        requirement.validator for requirement in candidate.validation_requirements
        if requirement.required_for_draft
    }
    production_only = {
        requirement.validator for requirement in candidate.validation_requirements
        if requirement.required_for_production and not requirement.required_for_draft
    }

    assert {"continuous_stock", "tool_sweep", "target_protection", "operation_evidence"} <= draft
    assert production_only == {"fixture_collision", "dry_run", "first_article"}


def test_strategy_rejects_machine_without_required_capability() -> None:
    try:
        BackLiveToolFaceStrategy.apply(
            plan=_plan(), snapshot=_snapshot(with_back_live=False), material_name="S45C",
        )
    except ValueError as error:
        assert "back_live_tool_milling" in str(error)
    else:
        raise AssertionError("expected back live-tool capability gate")


def test_strategy_registry_and_contract_require_all_declared_draft_evidence() -> None:
    strategy = get_process_strategy("back_live_tool_face_finish")
    candidate = strategy.describe(capability_available=True)
    evidence = {
        validator: ValidationOutcome(validator=validator, status="passed")
        for validator in (
            "machine_capability", "operation_dependency", "continuous_stock",
            "tool_sweep", "target_protection", "operation_evidence",
        )
    }
    passed = evaluate_validation_contract(candidate, evidence)
    missing = evaluate_validation_contract(candidate, {
        key: value for key, value in evidence.items() if key != "target_protection"
    })

    assert passed.draft_status == "passed"
    assert passed.production_status == "incomplete"
    assert passed.missing_production_evidence == [
        "fixture_collision", "dry_run", "first_article",
    ]
    assert missing.draft_status == "failed"
    assert missing.missing_draft_evidence == ["target_protection"]
