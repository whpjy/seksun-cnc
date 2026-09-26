from __future__ import annotations

import hashlib
import json
from math import pi
from typing import Any

from cam.providers.turning import SUPPORTED_OPERATION_TYPES

from ..machine_models import MachineConfigurationSnapshot
from ..models import JobResponse, Operation
from ..rotational_features import RotationalFeatureAnalysis, RotationalProfile, clip_rotational_profile
from ..turning_draft import TurningDraftRequest, compile_turning_draft
from ..turning_simulation import TurningStockSample
from ..turning_verification import TurningVerificationResult, verify_turning_profile
from ..l32_front_groove import build_front_groove_geometry_draft


STATE_SCHEMA_VERSION = "1.1.0"
DEFERRED_CONTRACT_VERSION = "1.1.0"
NONROTATIONAL_OPERATION_TYPES = {
    "pocket_roughing", "pocket_finishing",
    "live_tool_contour_roughing", "live_tool_contour_finishing",
}


def operation_signature(operation: Operation) -> str:
    operation_payload = operation.model_dump(mode="json")
    # Preserve signatures produced before reference_profile_id existed. Only an
    # actual binding changes the evidence identity.
    if operation_payload.get("reference_profile_id") is None:
        operation_payload.pop("reference_profile_id", None)
    # Preserve signatures created before grooving-tool capability metadata was
    # introduced. Only explicit non-default capability data changes identity.
    tool_payload = operation_payload.get("tool")
    if isinstance(tool_payload, dict):
        if tool_payload.get("groove_profile") is None:
            tool_payload.pop("groove_profile", None)
        if tool_payload.get("axial_contouring_supported") is False:
            tool_payload.pop("axial_contouring_supported", None)
        if tool_payload.get("inventory_id") is None:
            tool_payload.pop("inventory_id", None)
    payload = json.dumps(
        operation_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_context_signature(
    job: JobResponse,
    rotational: RotationalFeatureAnalysis,
    snapshot: MachineConfigurationSnapshot,
) -> str:
    payload = {
        "analysis": job.analysis.model_dump(mode="json") if job.analysis else None,
        "stock": job.plan.stock if job.plan else None,
        "rotational": rotational.model_dump(mode="json"),
        "machine_configuration_hash": snapshot.configuration_hash,
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def enabled_operations(job: JobResponse) -> list[Operation]:
    if job.plan is None:
        return []
    return [
        operation
        for setup in job.plan.setups
        for operation in sorted(setup.operations, key=lambda item: item.sequence)
        if operation.enabled
    ]


def reconcile_state(
    job: JobResponse,
    state: dict[str, Any] | None,
    context_signature: str | None = None,
) -> dict[str, Any]:
    """Keep only the still-identical accepted prefix after a plan edit.

    Appending a new operation preserves evidence. Editing, removing or reordering
    an accepted operation invalidates that operation and everything after it.
    """
    operations = enabled_operations(job)
    accepted_source = (state or {}).get("accepted")
    state_schema_version = (state or {}).get("schema_version")
    # Explicitly versioned evidence from the old variable-domain simulator is
    # unsafe to reuse. Keep accepting unversioned fixtures/legacy imports so a
    # caller can still reconcile them by signatures and material contracts.
    if state_schema_version not in {None, STATE_SCHEMA_VERSION}:
        accepted_source = []
    if context_signature and accepted_source and (state or {}).get("context_signature") != context_signature:
        accepted_source = []
    accepted_source = accepted_source if isinstance(accepted_source, list) else []
    accepted: list[dict[str, Any]] = []
    operation_positions = {item.id: index for index, item in enumerate(operations)}
    for index, (operation, record) in enumerate(zip(operations, accepted_source)):
        if not isinstance(record, dict):
            break
        if record.get("operation_id") != operation.id:
            break
        if record.get("operation_signature") != operation_signature(operation):
            break
        if not isinstance(record.get("cumulative_simulation"), dict):
            break
        contracts = record.get("deferred_material_contracts") or []
        if any(
            not isinstance(contract, dict)
            or contract.get("verification_version") != DEFERRED_CONTRACT_VERSION
            or operation_positions.get(str(contract.get("responsible_operation_id")), -1) <= index
            or contract.get("responsible_operation_signature") != operation_signature(
                operations[operation_positions[str(contract.get("responsible_operation_id"))]]
            )
            for contract in contracts
        ):
            break
        accepted.append(record)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "job_id": job.id,
        "release_status": "DRAFT",
        "production_ready": False,
        "context_signature": context_signature or (state or {}).get("context_signature"),
        "accepted": accepted,
    }


def state_summary(job: JobResponse, state: dict[str, Any] | None) -> dict[str, Any]:
    reconciled = reconcile_state(job, state)
    operations = enabled_operations(job)
    accepted = reconciled["accepted"]
    accepted_ids = {item["operation_id"] for item in accepted}
    outstanding_contracts = [
        contract
        for item in accepted for contract in (item.get("deferred_material_contracts") or [])
        if contract.get("responsible_operation_id") not in accepted_ids
    ]
    unassigned_obligations = [
        obligation
        for item in accepted
        for obligation in (item.get("unassigned_material_obligations") or [])
    ]
    provisional_ids = [
        item["operation_id"] for item in accepted
        if item.get("acceptance_state") == "provisional"
    ]
    verified_prismatic_feature_ids = sorted({
        str(feature_id)
        for item in accepted
        for feature_id in (item.get("evidence") or {}).get("verified_prismatic_feature_ids", [])
    })
    nonrotational_material_verified = any(
        (item.get("evidence") or {}).get("nonrotational_material_verified") is True
        for item in accepted
    )
    next_operation = operations[len(accepted)] if len(accepted) < len(operations) else None
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "job_id": job.id,
        "status": "completed" if operations and len(accepted) == len(operations) else "in_progress",
        "accepted_operation_ids": [item["operation_id"] for item in accepted],
        "formally_accepted_operation_ids": [
            item["operation_id"] for item in accepted
            if item.get("acceptance_state", "accepted") == "accepted"
        ],
        "provisional_operation_ids": provisional_ids,
        "accepted_count": len(accepted),
        "operation_count": len(operations),
        "next_operation_id": next_operation.id if next_operation else None,
        "next_action": "add_next_operation" if next_operation is None else "trial_l32_operation",
        "outstanding_deferred_material_contracts": outstanding_contracts,
        "unassigned_material_obligations": unassigned_obligations,
        "verified_prismatic_feature_ids": verified_prismatic_feature_ids,
        "nonrotational_material_verified": nonrotational_material_verified,
        "release_status": "DRAFT",
        "production_ready": False,
    }


def _profile_for_operation(
    operation: Operation,
    rotational: RotationalFeatureAnalysis,
) -> RotationalProfile | None:
    profiles = {item.id: item for item in rotational.profiles}
    features = {item.id: item for item in rotational.features}
    if operation.reference_profile_id:
        return profiles.get(operation.reference_profile_id)
    for feature_id in operation.feature_ids:
        if feature_id in profiles:
            return profiles[feature_id]
        feature = features.get(feature_id)
        if feature and feature.profile_id in profiles:
            return profiles[feature.profile_id]
    return None


def _reference_profile(
    job: JobResponse,
    rotational: RotationalFeatureAnalysis,
) -> RotationalProfile | None:
    planned_id = str(job.plan.stock.get("rotational_profile_id") or "") if job.plan else ""
    return next((item for item in rotational.profiles if item.id == planned_id), None) or (
        rotational.profiles[0] if rotational.profiles else None
    )


def _authorize_profile_for_draft_trial(
    operation: Operation,
    profile: RotationalProfile,
) -> tuple[RotationalProfile | None, dict[str, Any] | None, str | None]:
    if profile.review_state == "accepted":
        return profile, {"source": "human", "scope": "full"}, None
    if profile.review_state != "ai_provisional" or profile.provisional_decision is None:
        return None, None, f"rotational profile {profile.id} requires review or a bounded AI provisional decision"
    decision = profile.provisional_decision
    try:
        scoped = clip_rotational_profile(profile, decision.z_min_mm, decision.z_max_mm)
    except ValueError as error:
        return None, None, str(error)
    target_z_keys = {
        "face_z_mm", "z_mm", "profile_z_min_mm", "profile_z_max_mm",
        "start_z_mm", "end_z_mm", "groove_start_z_mm", "groove_end_z_mm",
    }
    out_of_scope = [
        (key, float(value))
        for key, value in operation.parameters.items()
        if key in target_z_keys
        and isinstance(value, (int, float)) and not isinstance(value, bool)
        and not decision.z_min_mm - 1e-6 <= float(value) <= decision.z_max_mm + 1e-6
    ]
    if out_of_scope:
        return None, None, (
            "operation target lies outside the AI-authorized profile scope: "
            + ", ".join(f"{key}={value:g}" for key, value in out_of_scope)
        )
    authorized = scoped.model_copy(update={"review_state": "accepted"})
    return authorized, {
        "source": "ai", "state": "ai_provisional", "scope": decision.scope,
        "z_min_mm": decision.z_min_mm, "z_max_mm": decision.z_max_mm,
        "diameter_min_mm": decision.diameter_min_mm,
        "diameter_max_mm": decision.diameter_max_mm,
        "confidence": decision.confidence,
        "evidence_refs": decision.evidence_refs,
        "production_ready": False,
    }, None


def _simulation_range(
    job: JobResponse,
    reference_profile: RotationalProfile | None,
) -> tuple[float, float]:
    """Return one immutable stock domain for the whole operation loop.

    A cumulative material state cannot be compared when each operation chooses
    a range from its own parameters or a feature-clipped profile. The domain is
    therefore derived only from job-level geometry/stock evidence.
    """
    values: list[float] = []
    if reference_profile:
        values.extend(point.z for point in reference_profile.points)
    stock = job.plan.stock if job.plan else {}
    finished_back_z = stock.get("finished_back_z_mm")
    if isinstance(finished_back_z, (int, float)) and not isinstance(finished_back_z, bool):
        values.append(float(finished_back_z))
    nonrotational_range = stock.get("nonrotational_region_z_mm")
    if isinstance(nonrotational_range, (list, tuple)):
        values.extend(
            float(value) for value in nonrotational_range
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    if values:
        return min(values) - 2.0, max(values) + 2.0
    bounds = job.analysis.measurements.get("bounding_box") if job.analysis else None
    if bounds is None:
        raise ValueError("missing geometry range for independent operation trial")
    sizes = [float(bounds.size.x), float(bounds.size.y), float(bounds.size.z)]
    length = max(sizes)
    return -length - 2.0, 2.0


def _assert_matching_material_grid(
    samples: list[TurningStockSample] | None,
    *, z_min_mm: float, z_max_mm: float, resolution_mm: float,
) -> None:
    if not samples:
        return
    expected_count = max(int(round((z_max_mm - z_min_mm) / resolution_mm)) + 1, 2)
    if (
        len(samples) != expected_count
        or abs(samples[0].z - z_min_mm) > 1e-6
        or abs(samples[-1].z - z_max_mm) > 1e-6
    ):
        raise ValueError(
            "cumulative material state uses a different simulation domain or grid; "
            "reset the operation loop and retrial from the first operation"
        )


def _assert_material_did_not_increase(
    previous: list[dict[str, Any]], current: list[dict[str, Any]],
) -> None:
    if len(previous) != len(current):
        raise ValueError("cumulative material grid changed after the previous accepted operation")
    for before, after in zip(previous, current):
        if abs(float(before["z"]) - float(after["z"])) > 1e-6:
            raise ValueError("cumulative material grid changed after the previous accepted operation")
        if float(after["outer_radius"]) > float(before["outer_radius"]) + 1e-6:
            raise ValueError("cumulative simulation added external material after machining")
        if float(after["inner_radius"]) < float(before["inner_radius"]) - 1e-6:
            raise ValueError("cumulative simulation added internal material after machining")


def _verification_evidence(verification: TurningVerificationResult | None) -> dict[str, Any] | None:
    if verification is None:
        return None
    deviation_groups: dict[str, list[float]] = {"overcut": [], "excess_stock": []}
    for deviation in verification.deviations:
        deviation_groups[deviation.kind].append(deviation.z)
    return {
        "profile_id": verification.profile_id,
        "profile_side": verification.profile_side,
        "expected_allowance_mm": verification.expected_allowance_mm,
        "maximum_overcut_mm": verification.metrics.maximum_overcut_mm,
        "maximum_excess_stock_mm": verification.metrics.maximum_excess_stock_mm,
        "overcut_sample_count": verification.metrics.overcut_sample_count,
        "excess_stock_sample_count": verification.metrics.excess_stock_sample_count,
        "estimated_overcut_volume_mm3": verification.metrics.estimated_overcut_volume_mm3,
        "estimated_excess_stock_volume_mm3": verification.metrics.estimated_excess_stock_volume_mm3,
        "deviation_z_ranges_mm": {
            kind: [round(min(values), 6), round(max(values), 6)]
            for kind, values in deviation_groups.items() if values
        },
    }


def _initial_samples(state: dict[str, Any]) -> list[TurningStockSample] | None:
    accepted = state.get("accepted") or []
    if not accepted:
        return None
    simulation = accepted[-1].get("cumulative_simulation") or {}
    samples = simulation.get("samples")
    if not isinstance(samples, list):
        return None
    return [TurningStockSample.model_validate(item) for item in samples]


def _initial_bore_radius(operation: Operation, samples: list[TurningStockSample] | None) -> float:
    explicit = operation.parameters.get("initial_bore_diameter_mm")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return max(float(explicit) / 2, 0)
    if not samples:
        return 0
    return max((sample.inner_radius for sample in samples), default=0)


def _unchanged_rotational_envelope(
    *, state: dict[str, Any], stock_radius_mm: float,
    z_min_mm: float, z_max_mm: float, resolution_mm: float,
) -> dict[str, Any]:
    """Carry the Z-R envelope through an indexed 3D operation unchanged.

    Indexed milling changes the exact 3D material domain but cannot be encoded
    by an axisymmetric Z-R field. Keeping this envelope unchanged prevents the
    two representations from fabricating material in one another's domain.
    """
    accepted = state.get("accepted") or []
    if accepted:
        simulation = accepted[-1].get("cumulative_simulation")
        if isinstance(simulation, dict):
            return simulation
    count = max(int(round((z_max_mm - z_min_mm) / resolution_mm)) + 1, 2)
    step = (z_max_mm - z_min_mm) / (count - 1)
    samples = [
        {"z": z_min_mm + index * step, "outer_radius": stock_radius_mm, "inner_radius": 0.0}
        for index in range(count)
    ]
    volume = pi * stock_radius_mm * stock_radius_mm * (z_max_mm - z_min_mm)
    return {
        "schema_version": "1.0.0",
        "engine": "Seksun CNC Z-R turning simulator",
        "approximation": "tool_centerline",
        "status": "completed",
        "resolution_mm": resolution_mm,
        "samples": samples,
        "metrics": {
            "initial_volume_mm3": round(volume, 6),
            "remaining_volume_mm3": round(volume, 6),
            "removed_volume_mm3": 0.0,
            "removal_percent": 0.0,
        },
        "warnings": [
            "Indexed 3D removal is tracked by exact OCC evidence and is not projected into the Z-R envelope."
        ],
    }


def _run_nonrotational_trial(
    *, base: dict[str, Any], reconciled: dict[str, Any], operation: Operation,
    evidence: dict[str, Any] | None, stock_radius_mm: float,
    z_min_mm: float, z_max_mm: float, resolution_mm: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not evidence:
        return ({
            **base, "status": "blocked", "can_accept": False,
            "reason": "missing_nonrotational_executor",
            "detail": "No exact 3D tool-sweep evidence was supplied for this indexed operation.",
            "next_action": "run_exact_nonrotational_trial",
        }, reconciled)
    required = {
        "machine_capability_status", "target_protection_status",
        "material_removal_status", "operation_coverage_status",
    }
    missing = sorted(required - set(evidence))
    failed = sorted(
        key for key in required if evidence.get(key) != "passed"
    )
    can_accept = not missing and not failed
    cumulative = _unchanged_rotational_envelope(
        state=reconciled, stock_radius_mm=stock_radius_mm,
        z_min_mm=z_min_mm, z_max_mm=z_max_mm, resolution_mm=resolution_mm,
    )
    feature_ids = list(dict.fromkeys(operation.feature_ids)) if can_accept else []
    result = {
        **base,
        "status": "passed" if can_accept else "blocked",
        "acceptance_state": "accepted",
        "can_accept": can_accept,
        "requires_warning_acknowledgement": False,
        "reason": None if can_accept else (
            "incomplete_nonrotational_evidence" if missing else "nonrotational_verification_failed"
        ),
        "detail": evidence.get("detail"),
        "next_action": "accept_l32_operation_trial" if can_accept else "revise_or_remove_operation",
        "decision_options": (
            ["accept", "revise_parameters", "change_tool", "remove_operation"]
            if can_accept else ["revise_parameters", "change_tool", "observe_model", "remove_operation"]
        ),
        "diagnostic_hints": evidence.get("diagnostic_hints") or [],
        "evidence": {
            **evidence,
            "material_domain": "exact_3d_occ",
            "simulation_status": "completed" if can_accept else "failed",
            "simulation_domain": {
                "kind": "exact_3d_occ_tool_sweep",
                "turning_envelope_unchanged": True,
            },
            "verification_status": "passed" if can_accept else "failed",
            "continued_from_operation_id": (
                reconciled["accepted"][-1]["operation_id"] if reconciled["accepted"] else None
            ),
            "verified_prismatic_feature_ids": (
                feature_ids if operation.type == "pocket_finishing" and can_accept else []
            ),
            "nonrotational_material_verified": bool(
                operation.type == "live_tool_contour_finishing" and can_accept
            ),
            "deferred_material_contracts": [],
            "unassigned_material_obligations": [],
            "downstream_capability_gaps": [],
        },
        "cumulative_simulation": cumulative,
        "nonrotational_verification": evidence,
        "warnings": evidence.get("warnings") or [],
    }
    return result, reconciled


def _downstream_material_contracts(
    *, operation: Operation, operations: list[Operation], rotational: RotationalFeatureAnalysis,
    verification: object, stock_radius_mm: float,
) -> list[dict[str, Any]]:
    """Assign excess-stock samples only to explicit, executable downstream geometry.

    Overcut can never be deferred. At present the strongest safe contract is an
    exact-profile groove whose cutter strips cover every residual sample.
    """
    deviations = [
        item for item in verification.deviations
        if item.kind == "excess_stock"
    ]
    if not deviations or any(item.kind == "overcut" for item in verification.deviations):
        return []
    try:
        current_index = next(index for index, item in enumerate(operations) if item.id == operation.id)
    except StopIteration:
        return []
    profiles = {item.id: item for item in rotational.profiles}
    features = {item.id: item for item in rotational.features}
    intervals: list[tuple[float, float, float, Operation, str]] = []
    for downstream in operations[current_index + 1:]:
        if downstream.type != "turn_grooving" or not downstream.enabled:
            continue
        feature = next((
            features.get(feature_id) for feature_id in downstream.feature_ids
            if features.get(feature_id) is not None
            and features[feature_id].kind == "external_groove_candidate"
        ), None)
        profile = profiles.get(feature.profile_id) if feature else None
        cutting_width = downstream.tool.cutting_width_mm
        if feature is None or profile is None or not cutting_width:
            continue
        try:
            groove = build_front_groove_geometry_draft(
                profile, feature, stock_radius_mm=stock_radius_mm,
                actual_planned_tool_width_mm=float(cutting_width),
                proposed_tool_width_mm=float(cutting_width),
            )
        except ValueError:
            continue
        if not groove.actual_tool_fits_floor or not groove.strips:
            continue
        for strip in groove.strips:
            intervals.append((
                strip.z_min_mm, strip.z_max_mm, strip.cut_to_radius_mm,
                downstream, feature.id,
            ))
    if not intervals or not all(
        any(
            lower - 1e-6 <= deviation.z <= upper + 1e-6
            and cut_to_radius <= deviation.target_radius_mm + 0.05
            for lower, upper, cut_to_radius, _, _ in intervals
        )
        for deviation in deviations
    ):
        return []
    responsible: dict[str, dict[str, Any]] = {}
    for lower, upper, cut_to_radius, downstream, feature_id in intervals:
        if any(
            lower - 1e-6 <= item.z <= upper + 1e-6
            and cut_to_radius <= item.target_radius_mm + 0.05
            for item in deviations
        ):
            record = responsible.setdefault(downstream.id, {
                "responsible_operation_id": downstream.id,
                "responsible_operation_signature": operation_signature(downstream),
                "responsible_operation_type": downstream.type,
                "feature_ids": [],
                "z_min_mm": lower,
                "z_max_mm": upper,
                "sample_count": 0,
            })
            if feature_id not in record["feature_ids"]:
                record["feature_ids"].append(feature_id)
            record["z_min_mm"] = min(float(record["z_min_mm"]), lower)
            record["z_max_mm"] = max(float(record["z_max_mm"]), upper)
    for record in responsible.values():
        record["sample_count"] = sum(
            float(record["z_min_mm"]) - 1e-6 <= item.z <= float(record["z_max_mm"]) + 1e-6
            for item in deviations
        )
        record["contract"] = "remove_deferred_excess_stock_before_plan_finalization"
        record["verification_version"] = DEFERRED_CONTRACT_VERSION
    return list(responsible.values())


def _downstream_groove_capability_gaps(
    *, operation: Operation, operations: list[Operation], rotational: RotationalFeatureAnalysis,
    verification: object, stock_radius_mm: float,
) -> list[dict[str, Any]]:
    """Explain residual material that overlaps a later groove but exceeds its cutter envelope."""
    deviations = [item for item in verification.deviations if item.kind == "excess_stock"]
    if not deviations:
        return []
    try:
        current_index = next(index for index, item in enumerate(operations) if item.id == operation.id)
    except StopIteration:
        return []
    profiles = {item.id: item for item in rotational.profiles}
    features = {item.id: item for item in rotational.features}
    gaps: list[dict[str, Any]] = []
    for downstream in operations[current_index + 1:]:
        if downstream.type != "turn_grooving" or not downstream.enabled:
            continue
        feature = next((
            features.get(feature_id) for feature_id in downstream.feature_ids
            if features.get(feature_id) is not None
            and features[feature_id].kind == "external_groove_candidate"
        ), None)
        profile = profiles.get(feature.profile_id) if feature else None
        cutting_width = downstream.tool.cutting_width_mm
        if feature is None or profile is None or not cutting_width:
            continue
        try:
            groove = build_front_groove_geometry_draft(
                profile, feature, stock_radius_mm=stock_radius_mm,
                actual_planned_tool_width_mm=float(cutting_width),
                proposed_tool_width_mm=float(cutting_width),
            )
        except ValueError:
            continue
        if not groove.strips:
            continue
        z_min = min(strip.z_min_mm for strip in groove.strips)
        z_max = max(strip.z_max_mm for strip in groove.strips)
        relevant = [item for item in deviations if z_min - 1e-6 <= item.z <= z_max + 1e-6]
        uncovered = [
            item for item in relevant
            if not any(
                strip.z_min_mm - 1e-6 <= item.z <= strip.z_max_mm + 1e-6
                and strip.cut_to_radius_mm <= item.target_radius_mm + 0.05
                for strip in groove.strips
            )
        ]
        if relevant and uncovered:
            gaps.append({
                "responsible_operation_id": downstream.id,
                "feature_id": feature.id,
                "tool_id": downstream.tool.id,
                "tool_width_mm": float(cutting_width),
                "overlapping_sample_count": len(relevant),
                "uncovered_sample_count": len(uncovered),
                "uncovered_z_range_mm": [
                    round(min(item.z for item in uncovered), 6),
                    round(max(item.z for item in uncovered), 6),
                ],
                "reason": "cutter_envelope_cannot_reach_exact_groove_profile",
                "next_action": "select_narrower_groove_tool_or_split_contour_grooving_strategy",
            })
    return gaps


def _scoped_trial_profile(
    operation: Operation,
    profile: RotationalProfile | None,
    rotational: RotationalFeatureAnalysis,
    stock_radius_mm: float,
) -> RotationalProfile | None:
    """Limit feature-specific verification to the material owned by that operation."""
    if profile is None or operation.type != "turn_grooving":
        return profile
    feature = next((
        item for item in rotational.features
        if item.id in operation.feature_ids and item.kind == "external_groove_candidate"
    ), None)
    width = operation.tool.cutting_width_mm
    if feature is None or not width:
        return profile
    groove = build_front_groove_geometry_draft(
        profile, feature, stock_radius_mm=stock_radius_mm,
        actual_planned_tool_width_mm=float(width),
    )
    if not groove.actual_tool_fits_floor or not groove.strips:
        return profile
    return clip_rotational_profile(
        profile,
        min(item.z_min_mm for item in groove.strips),
        max(item.z_max_mm for item in groove.strips),
    )


def run_independent_trial(
    *,
    job: JobResponse,
    operation_id: str,
    rotational: RotationalFeatureAnalysis,
    snapshot: MachineConfigurationSnapshot,
    state: dict[str, Any] | None,
    resolution_mm: float = 0.1,
    nonrotational_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if job.plan is None or job.analysis is None:
        raise ValueError("completed geometry and a process draft are required")
    context_signature = evidence_context_signature(job, rotational, snapshot)
    reconciled = reconcile_state(job, state, context_signature)
    operations = enabled_operations(job)
    accepted_count = len(reconciled["accepted"])
    if accepted_count >= len(operations):
        raise ValueError("all current operations have already passed the planning gate")
    expected = operations[accepted_count]
    if operation_id != expected.id:
        raise ValueError(
            f"operation order gate expects {expected.id}; {operation_id} cannot run before its predecessors"
        )
    operation = expected
    signature = operation_signature(operation)
    prefix_signatures = [item["operation_signature"] for item in reconciled["accepted"]]
    base: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "job_id": job.id,
        "operation_id": operation.id,
        "operation_signature": signature,
        "context_signature": context_signature,
        "accepted_prefix_signatures": prefix_signatures,
        "release_status": "DRAFT",
        "production_ready": False,
    }
    if operation.type not in SUPPORTED_OPERATION_TYPES | NONROTATIONAL_OPERATION_TYPES:
        result = {
            **base,
            "status": "blocked",
            "can_accept": False,
            "reason": "unsupported_independent_trial",
            "detail": f"independent L32 trial does not support operation type {operation.type}",
            "next_action": "revise_or_remove_operation",
        }
        return result, reconciled

    stock_diameter = job.plan.stock.get("diameter_mm")
    if not isinstance(stock_diameter, (int, float)) or isinstance(stock_diameter, bool):
        raise ValueError("process draft is missing a numeric stock diameter")
    reference_profile = _reference_profile(job, rotational)
    z_min, z_max = _simulation_range(job, reference_profile)
    if operation.type in NONROTATIONAL_OPERATION_TYPES:
        return _run_nonrotational_trial(
            base=base, reconciled=reconciled, operation=operation,
            evidence=nonrotational_evidence, stock_radius_mm=float(stock_diameter) / 2,
            z_min_mm=z_min, z_max_mm=z_max, resolution_mm=resolution_mm,
        )

    profile = _profile_for_operation(operation, rotational)
    profile_required = operation.type in {
        "turn_facing", "turn_cutoff", "turn_od_roughing", "turn_od_finishing",
        "turn_id_roughing", "turn_id_finishing", "turn_grooving", "turn_threading",
    }
    if profile_required and profile is None:
        result = {
            **base,
            "status": "blocked", "can_accept": False,
            "reason": "missing_rotational_profile",
            "detail": "operation does not reference a compatible rotational profile or profile feature",
            "next_action": "revise_process_operation",
        }
        return result, reconciled
    if profile is not None:
        authorized_profile, authorization, authorization_error = _authorize_profile_for_draft_trial(
            operation, profile,
        )
        if authorization_error or authorized_profile is None:
            result = {
                **base,
                "status": "blocked", "can_accept": False,
                "reason": "profile_review_required",
                "detail": authorization_error,
                "next_action": "inspect_or_provisionally_decide_profile",
            }
            return result, reconciled
        profile = authorized_profile
        base["profile_authorization"] = authorization

    try:
        trial_profile = _scoped_trial_profile(
            operation, profile, rotational, float(stock_diameter) / 2,
        )
    except ValueError as error:
        result = {
            **base,
            "status": "blocked", "can_accept": False,
            "reason": "feature_scope_resolution_failed", "detail": str(error),
            "next_action": "review_feature_or_tool",
        }
        return result, reconciled
    # The world/material grid belongs to the job, never to a feature-scoped
    # operation. A groove may use a clipped verification profile while still
    # running inside the same stock domain as facing and finishing.
    samples = _initial_samples(reconciled)
    try:
        _assert_matching_material_grid(
            samples, z_min_mm=z_min, z_max_mm=z_max, resolution_mm=resolution_mm,
        )
    except ValueError as error:
        result = {
            **base,
            "status": "blocked", "can_accept": False,
            "reason": "material_state_domain_mismatch", "detail": str(error),
            "next_action": "reset_operation_loop",
        }
        return result, reconciled
    initial_bore = _initial_bore_radius(operation, samples)
    trial_operation = operation
    if operation.type == "turn_grooving" and trial_profile is not None and trial_profile is not profile:
        trial_operation = operation.model_copy(deep=True)
        trial_operation.parameters["exact_groove_envelope"] = True
        groove_feature = next((
            item for item in rotational.features
            if item.id in operation.feature_ids and item.kind == "external_groove_candidate"
        ), None)
        if groove_feature is not None:
            # Feature geometry is authoritative for these required provider
            # parameters. Harness may still supply explicit reviewed values.
            trial_operation.parameters.setdefault(
                "z_mm", (groove_feature.z_start + groove_feature.z_end) / 2,
            )
            trial_operation.parameters.setdefault(
                "final_diameter_mm",
                2 * min(groove_feature.radius_start, groove_feature.radius_end),
            )
            trial_operation.parameters.setdefault("groove_depth_mm", groove_feature.depth_mm)
    request = TurningDraftRequest(
        machine_instance_id=snapshot.instance.id,
        operation=trial_operation,
        profile=trial_profile,
        stock_radius_mm=float(stock_diameter) / 2,
        initial_bore_radius_mm=initial_bore,
        z_min_mm=z_min,
        z_max_mm=z_max,
        resolution_mm=resolution_mm,
        initial_samples=samples,
    )
    try:
        draft = compile_turning_draft(job.id, request, snapshot)
    except (ValueError, OSError) as error:
        result = {
            **base,
            "status": "blocked", "can_accept": False,
            "reason": "deterministic_trial_failed", "detail": str(error),
            "next_action": "revise_or_remove_operation",
        }
        return result, reconciled

    verification_status = draft.verification.status if draft.verification else None
    protection_verification = (
        verify_turning_profile(profile, draft.simulation, tolerance_mm=0.05, expected_allowance_mm=0)
        if profile is not None else None
    )
    protection_failed = bool(
        protection_verification
        and protection_verification.metrics.overcut_sample_count > 0
    )
    # Facing/cutoff did not previously emit operation-specific verification.
    # The full-profile protection check supplies the missing hard safety gate.
    effective_verification_status = verification_status or (
        "failed" if protection_failed else "passed" if protection_verification else None
    )
    thread_status = draft.thread_verification.status if draft.thread_verification else None
    reachability_status = draft.reachability.status if draft.reachability else "not_required"
    failure_sources = [
        label for label, status in (
            ("profile_verification", verification_status),
            ("thread_verification", thread_status),
            ("reachability", reachability_status),
        ) if status == "failed"
    ]
    if protection_failed:
        failure_sources.append("profile_overcut_protection")
    warning_sources = [
        label for label, status in (
            ("profile_verification", verification_status),
            ("thread_verification", thread_status),
            ("reachability", reachability_status),
        ) if status == "warning"
    ]
    gate_status = "blocked" if failure_sources else "warning" if warning_sources else "passed"
    downstream_contracts = (
        _downstream_material_contracts(
            operation=operation, operations=operations, rotational=rotational,
            verification=draft.verification, stock_radius_mm=float(stock_diameter) / 2,
        )
        if draft.verification is not None and draft.verification.status == "warning"
        else []
    )
    downstream_capability_gaps = (
        _downstream_groove_capability_gaps(
            operation=operation, operations=operations, rotational=rotational,
            verification=draft.verification, stock_radius_mm=float(stock_diameter) / 2,
        )
        if draft.verification is not None and draft.verification.status == "warning"
        and not downstream_contracts
        else []
    )
    warning_can_be_acknowledged = (
        gate_status == "warning"
        and (
            bool(downstream_contracts)
            or operation.type in {"turn_od_roughing", "turn_id_roughing"}
        )
        and warning_sources == ["profile_verification"]
    )
    provisional_acceptance = (
        warning_can_be_acknowledged
        and not downstream_contracts
        and operation.type in {"turn_od_roughing", "turn_id_roughing"}
    )
    can_accept = gate_status == "passed" or warning_can_be_acknowledged
    verification_evidence = _verification_evidence(draft.verification)
    protection_evidence = _verification_evidence(protection_verification)
    diagnostic_hints: list[str] = []
    if draft.verification is not None and draft.verification.status != "passed":
        if draft.verification.metrics.overcut_sample_count:
            diagnostic_hints.append(
                "Overcut is geometric: inspect profile binding, tool nose radius and regional Z limits."
            )
        if draft.verification.metrics.excess_stock_sample_count:
            diagnostic_hints.append(
                "Excess stock remains: inspect radial allowance, tool nose compensation and profile coverage."
            )
        diagnostic_hints.append(
            "Feed or spindle speed alone does not change the deterministic Z-R material envelope."
        )
    if downstream_contracts:
        diagnostic_hints.append(
            "Residual material is explicitly assigned to downstream operations; acceptance creates a deferred-material contract."
        )
    if downstream_capability_gaps:
        gap_ids = ", ".join(item["responsible_operation_id"] for item in downstream_capability_gaps)
        diagnostic_hints.append(
            f"Residual material overlaps downstream groove operation(s) {gap_ids}, but the current cutter envelope "
            "cannot reach the exact shoulder profile. Select a narrower grooving tool or split a validated contour-grooving strategy."
        )
    if reachability_status == "warning":
        diagnostic_hints.append("Change tool assembly or approach direction before accepting reachability risk.")
    if protection_failed:
        diagnostic_hints.append(
            "The cumulative stock crosses the protected finished profile; revise the operation geometry before continuing."
        )
    full_result = {
        **base,
        "status": gate_status,
        "acceptance_state": "provisional" if provisional_acceptance else "accepted",
        "can_accept": can_accept,
        "requires_warning_acknowledgement": gate_status == "warning" and can_accept,
        "reason": (
            None if can_accept else "profile_overcut" if protection_failed
            else "deterministic_verification_failed" if failure_sources
            else "unresolved_trial_warning"
        ),
        "next_action": (
            "accept_l32_operation_trial" if can_accept
            else "change_downstream_tool_or_strategy" if downstream_capability_gaps
            else "revise_or_remove_operation"
        ),
        "decision_options": (
            ["accept", "revise_parameters", "change_tool", "remove_operation"]
            if can_accept else (
                ["change_downstream_tool", "split_contour_grooving", "observe_model", "remove_operation"]
                if downstream_capability_gaps
                else ["revise_parameters", "change_tool", "remove_operation", "observe_model"]
            )
        ),
        "diagnostic_hints": diagnostic_hints,
        "evidence": {
            "command_count": sum(len(channel.commands) for channel in draft.toolpath.channels),
            "simulation_status": draft.simulation.status,
            "simulation_domain": {
                "z_min_mm": z_min, "z_max_mm": z_max,
                "resolution_mm": resolution_mm,
                "sample_count": len(draft.simulation.samples),
            },
            "removed_volume_mm3": draft.simulation.metrics.removed_volume_mm3,
            "remaining_volume_mm3": draft.simulation.metrics.remaining_volume_mm3,
            "verification_status": effective_verification_status,
            "verification_metrics": verification_evidence,
            "profile_protection_status": "failed" if protection_failed else (
                "passed" if protection_verification else "not_required"
            ),
            "profile_protection_metrics": protection_evidence,
            "thread_verification_status": thread_status,
            "reachability_status": reachability_status,
            "continued_from_operation_id": (
                reconciled["accepted"][-1]["operation_id"] if reconciled["accepted"] else None
            ),
            "deferred_material_contracts": downstream_contracts,
            "unassigned_material_obligations": ([{
                "source_operation_id": operation.id,
                "kind": "excess_stock",
                "maximum_excess_stock_mm": (
                    verification_evidence or {}
                ).get("maximum_excess_stock_mm"),
                "z_ranges_mm": (
                    (verification_evidence or {}).get("deviation_z_ranges_mm") or {}
                ).get("excess_stock", []),
                "status": "unassigned",
                "next_action": "add_responsible_operation_then_reset_and_retrial_source",
            }] if provisional_acceptance else []),
            "downstream_capability_gaps": downstream_capability_gaps,
        },
        "toolpath": draft.toolpath.model_dump(mode="json"),
        "cumulative_simulation": draft.simulation.model_dump(mode="json"),
        "verification": draft.verification.model_dump(mode="json") if draft.verification else None,
        "thread_verification": (
            draft.thread_verification.model_dump(mode="json") if draft.thread_verification else None
        ),
        "reachability": draft.reachability.model_dump(mode="json") if draft.reachability else None,
        "warnings": draft.warnings,
    }
    return full_result, reconciled


def public_trial_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in result.items()
        if key not in {"toolpath", "cumulative_simulation", "verification", "thread_verification", "reachability"}
    }


def accept_trial(
    *, job: JobResponse, state: dict[str, Any] | None, trial: dict[str, Any], operation_id: str,
    context_signature: str | None = None,
    decision_rationale: str = "deterministic trial passed",
    acknowledge_warning: bool = False,
) -> dict[str, Any]:
    reconciled = reconcile_state(job, state, context_signature)
    operations = enabled_operations(job)
    index = len(reconciled["accepted"])
    if index >= len(operations) or operations[index].id != operation_id:
        raise ValueError("operation is not the next unaccepted operation")
    operation = operations[index]
    if trial.get("operation_id") != operation_id:
        raise ValueError("latest trial belongs to a different operation")
    if trial.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError("trial uses an obsolete material world model; run a new trial")
    if trial.get("operation_signature") != operation_signature(operation):
        raise ValueError("operation changed after trial; run a new trial")
    if context_signature and trial.get("context_signature") != context_signature:
        raise ValueError("geometry, stock or machine context changed after trial; run a new trial")
    if trial.get("accepted_prefix_signatures") != [
        item["operation_signature"] for item in reconciled["accepted"]
    ]:
        raise ValueError("accepted material-state prefix changed after trial")
    if not trial.get("can_accept") or trial.get("status") not in {"passed", "warning"}:
        raise ValueError("deterministic trial did not pass the planning gate")
    if trial.get("status") == "warning" and not acknowledge_warning:
        raise ValueError("warning trial requires explicit acknowledgement and rationale")
    simulation = trial.get("cumulative_simulation")
    if not isinstance(simulation, dict) or simulation.get("status") != "completed":
        raise ValueError("trial does not contain completed cumulative material evidence")
    if reconciled["accepted"]:
        previous_simulation = reconciled["accepted"][-1].get("cumulative_simulation") or {}
        previous_samples = previous_simulation.get("samples")
        current_samples = simulation.get("samples")
        if not isinstance(previous_samples, list) or not isinstance(current_samples, list):
            raise ValueError("cumulative material evidence is missing its sample grid")
        _assert_material_did_not_increase(previous_samples, current_samples)
        previous_remaining = (previous_simulation.get("metrics") or {}).get("remaining_volume_mm3")
        current_remaining = (simulation.get("metrics") or {}).get("remaining_volume_mm3")
        if (
            isinstance(previous_remaining, (int, float))
            and isinstance(current_remaining, (int, float))
            and float(current_remaining) > float(previous_remaining) + 1e-6
        ):
            raise ValueError("cumulative simulation volume increased after machining")
    reconciled["accepted"].append({
        "operation_id": operation_id,
        "operation_signature": trial["operation_signature"],
        "accepted_at_planning_gate": True,
        "acceptance_state": trial.get("acceptance_state", "accepted"),
        "trial_status": trial["status"],
        "decision_rationale": decision_rationale,
        "warning_acknowledged": bool(acknowledge_warning),
        "evidence": trial.get("evidence") or {},
        "deferred_material_contracts": (trial.get("evidence") or {}).get("deferred_material_contracts") or [],
        "unassigned_material_obligations": (
            (trial.get("evidence") or {}).get("unassigned_material_obligations") or []
        ),
        "cumulative_simulation": simulation,
    })
    return reconciled
