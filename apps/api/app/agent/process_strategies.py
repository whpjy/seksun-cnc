"""Capability-driven, evidence-gated process strategy contracts.

The models in this module deliberately avoid machine-program operation IDs.
Strategies bind semantic roles in the current plan, derive their parameters,
and declare the evidence that must pass before a candidate plan is committed.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..catalogs import get_tool, resolve_material
from ..machine_models import MachineConfigurationSnapshot
from ..models import Operation, ProcessPlan, Setup
from ..operation_library import create_operation_instance


class MachiningIntent(BaseModel):
    id: str
    kind: Literal[
        "face_finish", "profile_finish", "material_separation",
        "hole_machining", "groove_machining", "surface_machining",
    ]
    workpiece_side: Literal["front", "back"]
    feature_ids: list[str] = Field(default_factory=list)
    desired_state: str
    geometry_references: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class ProcessDependency(BaseModel):
    relation: Literal["after", "before", "consumes_stock_from", "requires_setup"]
    operation_role: str
    resolved_operation_id: str | None = None


class ValidationRequirement(BaseModel):
    validator: Literal[
        "machine_capability", "operation_dependency", "continuous_stock",
        "tool_reachability", "tool_sweep", "target_protection",
        "holder_collision", "fixture_collision", "operation_evidence",
        "dry_run", "first_article",
    ]
    required_for_draft: bool = True
    required_for_production: bool = True
    description: str


class ProcessCandidate(BaseModel):
    schema_version: str = "1.0.0"
    id: str
    strategy_id: str
    kind: str
    intent: MachiningIntent
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[ProcessDependency] = Field(default_factory=list)
    validation_requirements: list[ValidationRequirement] = Field(default_factory=list)
    parameter_basis: dict[str, Any] = Field(default_factory=dict)
    applicability: Literal["applicable", "not_applicable", "requires_confirmation"]
    reason: str


class StrategyApplication(BaseModel):
    candidate: ProcessCandidate
    plan: ProcessPlan
    operation_id: str
    role_bindings: dict[str, str]
    derived_parameters: dict[str, float | int | str | bool]


class ValidationOutcome(BaseModel):
    validator: str
    status: Literal["passed", "failed", "not_run"]
    evidence: dict[str, Any] = Field(default_factory=dict)


class CandidateValidationSummary(BaseModel):
    draft_status: Literal["passed", "failed"]
    production_status: Literal["passed", "incomplete", "failed"]
    outcomes: list[ValidationOutcome]
    missing_draft_evidence: list[str] = Field(default_factory=list)
    missing_production_evidence: list[str] = Field(default_factory=list)


def evaluate_validation_contract(
    candidate: ProcessCandidate,
    evidence: dict[str, ValidationOutcome | dict[str, Any]],
) -> CandidateValidationSummary:
    """Evaluate declared gates uniformly; undeclared success cannot release a plan."""
    outcomes: list[ValidationOutcome] = []
    for requirement in candidate.validation_requirements:
        supplied = evidence.get(requirement.validator)
        outcome = (
            supplied if isinstance(supplied, ValidationOutcome)
            else ValidationOutcome.model_validate(supplied)
            if supplied is not None
            else ValidationOutcome(validator=requirement.validator, status="not_run")
        )
        if outcome.validator != requirement.validator:
            raise ValueError(
                f"validation evidence key {requirement.validator} does not match {outcome.validator}"
            )
        outcomes.append(outcome)
    by_validator = {item.validator: item for item in outcomes}
    missing_draft = [
        requirement.validator for requirement in candidate.validation_requirements
        if requirement.required_for_draft
        and by_validator[requirement.validator].status != "passed"
    ]
    missing_production = [
        requirement.validator for requirement in candidate.validation_requirements
        if requirement.required_for_production
        and by_validator[requirement.validator].status != "passed"
    ]
    production_failed = any(
        by_validator[requirement.validator].status == "failed"
        for requirement in candidate.validation_requirements
        if requirement.required_for_production
    )
    return CandidateValidationSummary(
        draft_status="failed" if missing_draft else "passed",
        production_status=(
            "failed" if production_failed else "incomplete" if missing_production else "passed"
        ),
        outcomes=outcomes,
        missing_draft_evidence=missing_draft,
        missing_production_evidence=missing_production,
    )


def operations_for_role(plan: ProcessPlan, role: str) -> list[tuple[Setup, Operation]]:
    """Resolve semantic manufacturing roles without relying on display IDs."""
    matches: list[tuple[Setup, Operation]] = []
    for setup in plan.setups:
        for operation in setup.operations:
            if not operation.enabled:
                continue
            if role == "material_separation" and operation.type == "turn_cutoff":
                matches.append((setup, operation))
            elif role == "back_workholding" and operation.workpiece_side == "back":
                matches.append((setup, operation))
            elif role == "back_face_finish" and (
                operation.type == "back_live_face_finishing"
                or (operation.type == "turn_facing" and operation.workpiece_side == "back")
            ):
                matches.append((setup, operation))
    return matches


def require_single_role(plan: ProcessPlan, role: str) -> tuple[Setup, Operation]:
    matches = operations_for_role(plan, role)
    if len(matches) != 1:
        raise ValueError(
            f"process strategy requires exactly one {role} operation; found {len(matches)}"
        )
    return matches[0]


def _stable_operation_id(strategy_id: str, intent_id: str, dependency_id: str) -> str:
    digest = hashlib.sha256(
        f"{strategy_id}:{intent_id}:{dependency_id}".encode("utf-8")
    ).hexdigest()[:10].upper()
    return f"AUTO-{digest}"


class BackLiveToolFaceStrategy:
    id = "back_live_tool_face_v1"
    candidate_id = "back_live_tool_face_finish"
    capability = "back_live_tool_milling"

    @classmethod
    def describe(
        cls,
        *,
        capability_available: bool,
        feature_ids: list[str] | None = None,
    ) -> ProcessCandidate:
        intent = MachiningIntent(
            id="finish_back_face_after_separation",
            kind="face_finish",
            workpiece_side="back",
            feature_ids=feature_ids or [],
            desired_state="remove sacrificial axial stock to the finished back-face boundary",
            geometry_references=["finished_back_datum", "exact_back_face_boundary"],
            constraints={
                "protect_target_solid": True,
                "preserve_nonrotational_region": True,
                "release_status": "DRAFT",
            },
        )
        requirements = [
            ValidationRequirement(
                validator="machine_capability", description="named machine supports back live-tool milling",
            ),
            ValidationRequirement(
                validator="operation_dependency", description="material separation preserves positive axial stock",
            ),
            ValidationRequirement(
                validator="continuous_stock", description="cross-spindle material state remains continuous",
            ),
            ValidationRequirement(
                validator="tool_sweep", description="3D cutter sweep is bounded by the face region",
            ),
            ValidationRequirement(
                validator="target_protection", description="cutter sweep does not remove target material",
            ),
            ValidationRequirement(
                validator="operation_evidence", description="generated path removes measurable stock",
            ),
            ValidationRequirement(
                validator="fixture_collision", required_for_draft=False,
                description="machine fixture and holder collision package",
            ),
            ValidationRequirement(
                validator="dry_run", required_for_draft=False, description="named-machine dry run",
            ),
            ValidationRequirement(
                validator="first_article", required_for_draft=False, description="first-article inspection",
            ),
        ]
        return ProcessCandidate(
            id=cls.candidate_id,
            strategy_id=cls.id,
            kind="special_process",
            intent=intent,
            required_capabilities=[cls.capability],
            dependencies=[
                ProcessDependency(relation="after", operation_role="material_separation"),
                ProcessDependency(relation="consumes_stock_from", operation_role="material_separation"),
                ProcessDependency(relation="requires_setup", operation_role="back_workholding"),
            ],
            validation_requirements=requirements,
            applicability="requires_confirmation" if capability_available else "not_applicable",
            reason=(
                "Use a back live tool to finish only the bounded back-face region after separation."
                if capability_available else
                "The bound machine does not provide back live-tool milling capability."
            ),
        )

    @classmethod
    def apply(
        cls,
        *,
        plan: ProcessPlan,
        snapshot: MachineConfigurationSnapshot,
        material_name: str,
    ) -> StrategyApplication:
        if cls.capability not in snapshot.validation.capabilities:
            raise ValueError(f"machine configuration lacks capability: {cls.capability}")
        candidate_plan = plan.model_copy(deep=True)
        separation_setup, separation = require_single_role(candidate_plan, "material_separation")
        back_setups = [setup for setup in candidate_plan.setups if setup.id != separation_setup.id and (
            any(
                operation.workpiece_side == "back" or operation.channel_id == "sub"
                for operation in setup.operations
            )
            or any(token in f"{setup.name} {setup.fixture}".lower() for token in (
                "back", "sub spindle", "sub-spindle", "背轴", "副轴",
            ))
        )]
        if len(back_setups) != 1:
            raise ValueError(f"process strategy requires exactly one back-workholding setup; found {len(back_setups)}")
        target_setup = back_setups[0]
        if "finished_back_z_mm" not in candidate_plan.stock:
            raise ValueError("process plan does not define finished_back_z_mm")
        finished_back = float(candidate_plan.stock["finished_back_z_mm"])
        cutting_width = float(
            separation.parameters.get("cutting_width_mm")
            or separation.tool.cutting_width_mm
            or 0
        )
        if cutting_width <= 0:
            raise ValueError("material-separation operation has no positive cutting width")

        tool = get_tool("EM-1")
        # Tool-relative allowance avoids a model-specific magic value while
        # remaining bounded for the validated L32 strategy envelope.
        allowance = round(min(0.5, max(0.15, tool.diameter_mm * 0.25)), 3)
        separation.parameters.update({
            "z_mm": finished_back - allowance - cutting_width / 2,
            "finished_back_datum_z_mm": finished_back,
            "retained_material_min_z_mm": finished_back - allowance,
            "back_face_allowance_mm": allowance,
            "sacrificial_extension_mm": cutting_width + allowance,
        })
        material = resolve_material(material_name)
        rpm = min(
            round(material.milling_speed_m_min * 1000 / (3.141592653589793 * tool.diameter_mm)),
            tool.max_rpm, 6000,
        )
        feed = round(rpm * tool.flute_count * material.mill_feed_per_tooth_mm, 1)
        feature_ids = [f"{candidate_plan.stock.get('rotational_profile_id', 'PART')}-BACK-DATUM"]
        candidate = cls.describe(capability_available=True, feature_ids=feature_ids)
        operation_id = _stable_operation_id(cls.id, candidate.intent.id, separation.id)
        existing_ids = {
            operation.id for setup in candidate_plan.setups for operation in setup.operations
            if operation.type == "back_live_face_finishing"
            or (operation.type == "turn_facing" and operation.workpiece_side == "back")
        }
        for setup in candidate_plan.setups:
            setup.operations = [
                operation for operation in setup.operations
                if operation.id not in existing_ids
            ]
        used_sequences = {
            operation.sequence for setup in candidate_plan.setups for operation in setup.operations
        }
        sequence = separation.sequence + 10
        while sequence in used_sequences:
            sequence += 1
        operation = create_operation_instance(
            id=operation_id, sequence=sequence, type="back_live_face_finishing",
            name="背面动力刀具端面精加工", channel_id="sub", spindle_id="sub",
            workpiece_side="back", synchronization_group=separation.synchronization_group,
            feature_ids=feature_ids, tool=tool, enabled=True,
            parameters={
                "stock_allowance_mm": allowance,
                "finished_back_datum_z_mm": finished_back,
                "step_over_mm": round(tool.diameter_mm * 0.55, 4),
                "axial_clearance_mm": 1.0,
                "spindle_rpm": rpm,
                "feed_rate_mm_min": feed,
                "plunge_rate_mm_min": round(feed * 0.3, 1),
                "required_module": "U151B",
                "source_geometry": "exact_back_face_boundary",
                "strategy_id": cls.id,
                "dependency_operation_id": separation.id,
            },
            rationale=[
                f"按刀具直径计算并保留 {allowance:.3f} mm 背面轴向余量",
                "按成品端面边界执行动力刀具光栅精加工",
            ],
            confidence=0.72, status="warning",
        )
        target_setup.operations.append(operation)
        target_setup.operations.sort(key=lambda item: item.sequence)
        candidate_plan.stock["back_face_process"] = "back_live_face_finishing"
        candidate_plan.stock["back_face_allowance_mm"] = allowance
        candidate.dependencies = [dependency.model_copy(update={
            "resolved_operation_id": (
                target_setup.id if dependency.operation_role == "back_workholding" else separation.id
            ),
        }) for dependency in candidate.dependencies]
        candidate.parameter_basis = {
            "allowance_rule": "clamp(tool_diameter_mm * 0.25, 0.15, 0.50)",
            "tool_diameter_mm": tool.diameter_mm,
            "material_id": material.id,
            "finished_back_datum_z_mm": finished_back,
        }
        return StrategyApplication(
            candidate=candidate,
            plan=candidate_plan,
            operation_id=operation_id,
            role_bindings={
                "material_separation": separation.id,
                "back_workholding": target_setup.id,
                "back_face_finish": operation_id,
            },
            derived_parameters={
                "stock_allowance_mm": allowance,
                "separation_z_mm": float(separation.parameters["z_mm"]),
                "spindle_rpm": rpm,
                "feed_rate_mm_min": feed,
            },
        )


PROCESS_STRATEGIES = {
    BackLiveToolFaceStrategy.candidate_id: BackLiveToolFaceStrategy,
}


def get_process_strategy(candidate_id: str):
    try:
        return PROCESS_STRATEGIES[candidate_id]
    except KeyError as error:
        raise ValueError(f"unknown process strategy candidate: {candidate_id}") from error
